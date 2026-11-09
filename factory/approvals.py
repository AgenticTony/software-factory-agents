"""The owner-held approval ledger (F-63).

Approval truth moves out of the repos the factory works on and into a
file no repo or session can write: ``<factory_root>/approvals/
<project>.jsonl``, append-only and hash-chained exactly the way
:mod:`factory.progress` chains, validated line by line against
``factory/data/schema/approvals.schema.json`` on every load (the F-02
rule). A rewritten, reordered, malformed or foreign-project line refuses
the load with the line named; nothing under ``approvals/`` is inside any
sandbox's read or write surface (the factory root itself is denied both
ways by :func:`factory.sandbox.build_settings`).

Two kinds of entry:

* ``task`` — the owner approved THIS contract: the task id plus
  :func:`factory.contract.contract_hash` over the whole contract minus
  the lifecycle fields the factory itself writes after approval. The
  factory acts on a task only while its current hash matches an entry.
* ``profile`` — the owner approved the profile's security-relevant
  content, hashed per group so a refusal can name exactly what changed.
  No session or gate runs under content that doesn't match the latest
  profile entry (:func:`require_profile_approval`).

Known properties (fail-safe, stated): the hash chain detects a rewrite
of any line except the last (nothing chains to the last line's content);
tail truncation only ever REMOVES approvals, which demotes or refuses —
never widens. The chain is integrity-of-record against corruption and
transit, not a signature; the security boundary is the file's location,
writable only by the owner's own machine user.

Eval and rehearsal fixtures (factory-owned profiles defined in factory
source, changeable only by a reviewed PR — the same trust chain as the
schema consts) record their own approval via
:func:`ensure_fixture_profile_approval`; no production path calls it.
The fixture root DEFAULTS to the real factory root (``~/factory`` — the
owner's ``factory eval`` writes there by design), so the append REFUSES
to land a fixture entry in any ledger that already holds owner entries
(:attr:`ApprovalLedger.FIXTURE_APPROVER`): a future client named like a
fixture cannot have its latest owner approval superseded by a fixture
entry, and owner ledgers stay owner-only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path

from factory.contract import (
    SCHEMA_DIR,
    ContractError,
    contract_hash,
    validate_against_schema,
)
from factory.progress import ProgressLog, ProgressLogError
from factory.runs import FACTORY_ROOT_DEFAULT, project_component
from factory.state import now_iso

# The security-relevant profile content AC-2 covers: group name -> the
# key path it is read from. A missing key hashes as null, so REMOVING a
# group is a change. F-59 will add "provider" and "data_owner" here (its
# work order says so); an approval that predates a new group never
# matches it — re-approval, never silent acceptance.
PROFILE_SECURITY_GROUPS: dict[str, tuple[str, ...]] = {
    "commands": ("commands",),
    "sentinels": ("sentinels",),
    "network_allowlist": ("network_allowlist",),
    "docs_allowlist": ("docs_allowlist",),
    "sandbox": ("sandbox",),
    "repo.remote": ("repo", "remote"),
    # R1 fix 1: repo.path decides WHICH checkout every run clones
    # (orchestrator → create_run's source). Unapproved, an approved
    # profile could point at another client's repo — sessions over its
    # code, its history pushed under this project's approved remote.
    # Hashed on the EXPANDED+RESOLVED absolute path so ~/x and
    # /Users/<u>/x are one approval and no spelling change dodges it.
    "repo.path": ("repo", "path"),
    "repo.default_branch": ("repo", "default_branch"),
    "repo.branch_protection": ("repo", "branch_protection"),
    "paths.protected": ("paths", "protected"),
    "paths.risk": ("paths", "risk"),
    "live_checks": ("live_checks",),
    "toolkit": ("toolkit",),
}

_APPROVALS_SCHEMA = SCHEMA_DIR / "approvals.schema.json"


class ApprovalLedgerError(ContractError):
    """The approval ledger is corrupt, foreign or unapproved. The message
    is for humans and names the offending line or group."""


def _canonical(value) -> str:
    """Canonical JSON for hashing: sorted keys, fixed separators, literal
    non-ASCII (:func:`factory.contract.canonical_json` shape plus
    ``ensure_ascii=False`` so every script's entries hash identically to
    their rendered form)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _group_hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


# Groups whose value is a filesystem path: hashed (and rendered) on the
# expanded, resolved absolute form, so a spelling change cannot force or
# dodge re-approval (R1 fix 1).
_PATH_GROUPS = ("repo.path",)


def _group_value(profile: dict, path: tuple[str, ...]) -> object:
    value: object = profile
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def profile_group_hashes(profile: dict) -> dict[str, str]:
    """Hash each security-relevant group of the (already loaded and
    defaulted) profile.

    The SAME in-memory dict that builds settings and runs gates is what
    gets hashed — callers load once, check once, and never re-read the
    file (the TOCTOU rule). A missing key hashes as ``null``'s digest, so
    removing a key is a change distinct from empty content. Path groups
    (``repo.path``) hash their expanded+resolved absolute form.
    """
    hashes: dict[str, str] = {}
    for group, path in PROFILE_SECURITY_GROUPS.items():
        value = _group_value(profile, path)
        if group in _PATH_GROUPS and isinstance(value, str):
            value = str(Path(value).expanduser().resolve())
        hashes[group] = _group_hash(value)
    return hashes


class ApprovalLedger(ProgressLog):
    """One project's append-only, hash-chained approval ledger.

    Reuses :class:`factory.progress.ProgressLog`'s chaining and append
    mechanism (numbered entries, ``prev`` digests, flock + fsync) rather
    than forking a second one; what this subclass adds is the per-line
    schema validation and the project binding on load, plus the
    task/profile match rules.
    """

    def __init__(self, path: Path | str, project: str) -> None:
        super().__init__(path)
        self.project = project_component(project)

    @classmethod
    def for_project(
        cls, factory_root: Path | str | None, project: str
    ) -> "ApprovalLedger":
        """The canonical ledger location: ``<root>/approvals/<project>.jsonl``
        (``None`` means the default factory root, ``~/factory``)."""
        root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
        return cls(root / "approvals" / f"{project_component(project)}.jsonl",
                   project)

    def _read(self) -> "tuple[list[dict], str]":
        """Chain-verify (the progress mechanism), then schema-validate
        every line and check the project binding — the line named. The
        progress layer's chain failures surface as the ledger's own
        error type so consumers catch one thing."""
        try:
            entries, prev = super()._read()
        except ProgressLogError as exc:
            raise ApprovalLedgerError(str(exc)) from exc
        for number, record in enumerate(entries, start=1):
            try:
                validate_against_schema(record, _APPROVALS_SCHEMA,
                                        f"{self.path}: line {number}")
            except ContractError as exc:
                raise ApprovalLedgerError(str(exc)) from exc
            if record.get("project") != self.project:
                raise ApprovalLedgerError(
                    f"{self.path}: line {number} approves project "
                    f"{record.get('project')!r} but this ledger is "
                    f"{self.project!r} — a ledger copied from another "
                    "project's approvals refuses to load"
                )
        return entries, prev

    def entries(self) -> "list[dict]":
        """All entries, oldest first, chain- and schema-verified.

        Takes a SHARED lock on the ledger's lock file so a concurrent
        append (an owner approving while a run reads) can never be
        observed half-written: the writer holds the exclusive lock across
        its write+fsync. A missing lock file (no append has ever run) is
        not an error — an empty or absent ledger is the no-approvals
        state every consumer handles.
        """
        if not self.path.exists():
            return []
        lock = self.path.with_name(self.path.name + ".lock")
        if not lock.exists():
            # No writer has ever locked: nothing can be mid-append.
            return self._read()[0]
        with lock.open("r") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._read()[0]
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------ matching --

    def has_contract(self, task_id: str, chash: str) -> bool:
        """Whether the owner ever approved this exact contract of this
        task (any matching entry — re-approvals append, never replace)."""
        return any(
            record.get("kind") == "task"
            and record.get("task_id") == task_id
            and record.get("contract_hash") == chash
            for record in self.entries()
        )

    def latest_profile_approval(self) -> "dict | None":
        """The newest ``profile`` entry (None when none exists). The
        latest entry defines the approved state: groups changed since it
        are named in the refusal, and reverting to an older approved
        shape still needs a fresh approval — fail closed."""
        latest = None
        for record in self.entries():
            if record.get("kind") == "profile":
                latest = record
        return latest

    # ------------------------------------------------------------- writers --

    # F-63 R2-3: fixture approvals (the eval/rehearsal helpers) never
    # mix into a ledger holding owner entries. The fixture paths default
    # to the REAL factory root (~/factory) — an owner ledger there must
    # not gain (or be superseded by) a factory-fixture entry.
    FIXTURE_APPROVER = "factory-fixture"

    def _append(self, entry: dict) -> dict:
        """Append one entry, mapping the progress log's failures to the
        ledger's own error type and refusing a fixture approval into an
        owner ledger."""
        if entry.get("approved_by") == self.FIXTURE_APPROVER:
            existing = self.entries()
            if any(record.get("approved_by") != self.FIXTURE_APPROVER
                   for record in existing):
                raise ApprovalLedgerError(
                    f"{self.path}: refusing to append a factory-fixture "
                    "approval into a ledger that holds OWNER approvals — "
                    "fixture approvals (eval/rehearsal) may only land in "
                    "their own fresh ledger; give the fixture its own "
                    "--factory-root"
                )
        try:
            return self.append(entry)
        except ProgressLogError as exc:
            raise ApprovalLedgerError(str(exc)) from exc

    def record_task_for(self, task: dict, *, approved_by: str = "owner",
                        approved_at: str | None = None) -> dict:
        """Record approval of this task's CURRENT contract (hash computed
        here, over the whole contract minus the lifecycle fields)."""
        return self._append({
            "kind": "task",
            "project": self.project,
            "task_id": task["id"],
            "contract_hash": contract_hash(task),
            "approved_by": approved_by,
            "approved_at": approved_at or now_iso(),
        })

    def record_profile_for(self, profile: dict, *,
                           approved_by: str = "owner") -> dict:
        """Record approval of the profile's security-relevant content
        (the SAME loaded-and-defaulted dict the run will use)."""
        return self._append({
            "kind": "profile",
            "project": self.project,
            "groups": profile_group_hashes(profile),
            "approved_by": approved_by,
            "approved_at": now_iso(),
        })


def changed_groups(profile: dict, approval: dict) -> "list[str]":
    """The security groups whose current hash differs from the approval's
    (empty when the approval covers the profile exactly)."""
    current = profile_group_hashes(profile)
    approved = approval.get("groups") or {}
    return [group for group in current if approved.get(group) != current[group]]


def require_profile_approval(profile: dict, factory_root: Path | str, *,
                             origin: str = "profile") -> None:
    """Refuse unless the latest profile approval covers this profile.

    Args:
        profile: The loaded, defaulted profile mapping — the SAME dict the
            caller will build settings and run gates from (hash-once,
            never re-read: the TOCTOU rule).
        factory_root: Where ``approvals/`` lives.
        origin: Name for the refusal message (the profile's path).

    Raises:
        ApprovalLedgerError: No profile approval is on record, or the
            groups that changed since the latest one are named in the
            message. Also raised when the ledger itself refuses to load.
    """
    ledger = ApprovalLedger.for_project(factory_root, profile["name"])
    approval = ledger.latest_profile_approval()
    if approval is None:
        raise ApprovalLedgerError(
            f"{origin}: no profile approval on record for project "
            f"{profile['name']!r} — run `factory approve --profile` once "
            "so the owner has seen the security-relevant content (commands, "
            "sentinels, network/docs allowlists, sandbox, repo, paths, "
            "live_checks, toolkit)"
        )
    changed = changed_groups(profile, approval)
    if changed:
        raise ApprovalLedgerError(
            f"{origin}: the profile changed since its owner approval and "
            f"no session or gate may run under it — changed: "
            f"{', '.join(changed)}; re-approve with "
            f"`factory approve --profile`"
        )


def ensure_fixture_profile_approval(factory_root: Path | str,
                                    profile: dict) -> None:
    """Record the FACTORY-OWNED fixture profile's approval, when not
    already covered.

    Only for the in-code fixture profiles (eval's, the rehearsal's):
    their content is factory source, admitted by the same reviewed-PR
    trust chain as the schema consts. The root is whatever the
    eval/rehearsal was given (DEFAULTS to ``~/factory`` for an owner-run
    ``factory eval``) — the append itself refuses to mix a fixture entry
    into a ledger holding owner entries (R2-3), so the default root is
    safe exactly until a real client shares the fixture's project name,
    at which point the refusal names it. Production paths (``run_once``,
    the importer) call :func:`require_profile_approval` and never this.
    """
    ledger = ApprovalLedger.for_project(factory_root, profile["name"])
    approval = ledger.latest_profile_approval()
    if approval is not None and not changed_groups(profile, approval):
        return
    ledger.record_profile_for(profile, approved_by="factory-fixture")
