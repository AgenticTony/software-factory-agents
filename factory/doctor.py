"""``factory doctor`` — one read-only pass over every onboarded project
(F-66 AC-2/AC-3).

The F-13 pilot met three rule changes (F-64's caps, F-65's scope,
F-59's provider/allowlist rules) as single bare refusals, because every
loader stops at the first error and nothing knew the other repos
existed. Doctor fixes both halves:

* it collects ALL violations through all-errors modes of the REAL
  loaders (:func:`factory.contract.iter_schema_violations`,
  ``_semantic_violations``, ``_profile_semantic_violations``), and
* it enumerates every repo the F-63 approval ledger records (each
  profile approval carries ``repo_path`` from F-66) plus any ``--repo``
  given, so an onboarded project is checked before it breaks mid-run.

Every finding names its epoch, the work order that introduced the rule,
and the exact fix — read from the epoch register
(``factory/data/schema/epochs.json``), never re-typed here.

The report is a LIST OF NAMED CHECKS (in :data:`CHECKS` order) by
design: a later work order adds one entry (F-100's model-stack
deprecation note is one more check, not a rewrite).

Safety shape: doctor starts no session, writes nothing anywhere (the
factory root, the repos, stdout only), and the command exits non-zero
on any finding. ``run``/``loop``/``import`` refuse on the FATAL subset
through :func:`preflight_run` / the import gate — a stale-but-valid
epoch (healed by the next write's re-stamp) and draft tasks approval
would refuse (an owner decision, not a run blocker) are reported, never
fatal. Profile-approval mismatches keep their existing enforcement
(:func:`factory.approvals.require_profile_approval`) whose message
already names the changed groups and the re-approve command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from factory.contract import (
    SCHEMA_DIR,
    ContractError,
    _profile_semantic_violations,
    _semantic_violations,
    apply_profile_defaults,
    iter_schema_violations,
    read_file,
)
from factory.epochs import EpochError, load_register
from factory.runs import FACTORY_ROOT_DEFAULT

# The named checks, in report order. One entry per check is the whole
# point of this shape (see module docstring).
CHECKS = (
    "profile-parse",        # the profile reads and is a YAML mapping
    "profile-schema",       # project.schema.json, all violations
    "profile-semantics",    # the F-59/F-63/F-65/F-71 semantic rules
    "model-deprecation",    # F-100: provider.models still pins seats
    "profile-approval",     # the F-63 ledger's group hashes still match
    "queue-parse",          # the queue reads and is JSON
    "queue-schema",         # tasks.schema.json, all violations
    "queue-semantics",      # the queue semantic rules, all violations
    "queue-epoch",          # the recorded epoch against the current one
    "task-approvability",   # what `factory approve` would refuse
    "ledger-repo-path",     # the ledger records repo paths (F-66)
)

# ELIGIBLE_KINDS lives in factory.state (which imports this module's
# siblings, not this module); the deferred import keeps the cycle out
# of import time.


def _eligible_kinds() -> frozenset:
    from factory.state import ELIGIBLE_KINDS
    return ELIGIBLE_KINDS


@dataclass(frozen=True)
class Finding:
    """One doctor finding: the check that produced it, the human
    message, the rule's epoch attribution (from the register, through
    the check tag) and the fix. ``fatal`` marks the subset ``run``/
    ``loop``/``import`` refuse on — everything else is reported for the
    owner to act on."""

    repo: str
    check: str
    message: str
    tag: str = "baseline"
    fatal: bool = True
    epoch: int = 1
    wo: str = ""
    change: str = ""
    fix: str | None = None


@dataclass
class Report:
    """One repo's findings (empty list = clean)."""

    repo: str
    findings: list[Finding] = field(default_factory=list)


def _epoch_context(tag: str, *, wo: str | None = None
                   ) -> tuple[int, str, str, str | None]:
    """(epoch, wo, change, fix) for a violation tag, from the register.
    ``wo`` overrides the tag lookup for findings that belong to one
    named work order rather than a rule epoch (the ledger's repo_path
    field, the epoch ceiling)."""
    try:
        register = load_register()
    except EpochError as exc:  # pragma: no cover - register ships valid
        raise ContractError(str(exc)) from exc
    if wo is not None:
        entries = [entry for entry in register.entries if entry.wo == wo]
        entry = entries[0] if entries else register.entries[0]
        epoch = entry.epoch
    else:
        epoch = register.epoch_for_check(tag)
        entry = register.entry_for_epoch(epoch)
    fix = None
    if entry.migration.kind == "owner_action":
        fix = entry.migration.command
        if entry.migration.note:
            fix = f"{fix} — {entry.migration.note}"
    elif entry.migration.kind == "automatic":
        fix = f"automatic: {entry.migration.name}"
        if entry.migration.note:
            fix = f"{fix} ({entry.migration.note})"
    return epoch, entry.wo, entry.change, fix


# The action that clears a ledger repo-path finding: a profile approval
# run from the project's own repo records its real path.
REAPPROVE_FIX = ("`factory approve --profile`, run in the project's repo "
                 "(records its real path)")


def _finding(repo: str, check: str, message: str, *, tag: str = "baseline",
             fatal: bool = True, wo: str | None = None,
             fix: str | None = None) -> Finding:
    """One finding, its rule line from the epoch register. A finding
    attributed to a named work order (``wo``) belongs to that work
    order, not to a rule epoch's migration: its fix line is ``fix`` or
    none — never the attributed epoch's migration, which clears
    nothing here (F-66's automatic re-stamp printed as the fix for a
    missing ledger repo path)."""
    epoch, wo_resolved, change, epoch_fix = _epoch_context(tag, wo=wo)
    if fix is None and wo is None:
        fix = epoch_fix
    return Finding(repo=repo, check=check, message=message, tag=tag,
                   fatal=fatal, epoch=epoch, wo=wo_resolved, change=change,
                   fix=fix)


def project_report(
    profile_path: Path | str,
    queue_path: Path | str,
    *,
    factory_root: Path | str | None = None,
    queue_required: bool = True,
    profile_required: bool = True,
    approval_check: bool = True,
) -> Report:
    """Every finding for ONE project, through the real loaders in
    all-errors mode. Read-only.

    Args:
        profile_path: The repo's ``project.yaml``.
        queue_path: The repo's ``.factory/tasks.json``.
        factory_root: Where ``approvals/`` lives (approval check only).
        queue_required: False when an absent queue is expected (a
            first import) — then no queue-parse finding is raised.
        profile_required: False to mirror the importer's existing-queue
            tolerance (a missing profile file is not a finding; the
            real-session path keeps its own strict load).
        approval_check: False to leave profile-approval enforcement to
            :func:`factory.approvals.require_profile_approval` (the
            run/import paths — its refusal already names the changed
            groups and the fix command).
    """
    profile_path = Path(profile_path)
    queue_path = Path(queue_path)
    repo = str(queue_path.parent.parent
               if queue_path.parent.name == ".factory" else queue_path.parent)
    report = Report(repo=repo)
    add = report.findings.append

    # ------------------------------------------------------ profile side --
    profile: dict | None = None
    origin = str(profile_path)
    if profile_path.is_file():
        try:
            text = read_file(profile_path, origin)
        except ContractError as exc:
            add(_finding(repo, "profile-parse", str(exc)))
        else:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                add(_finding(
                    repo, "profile-parse", f"{origin}: invalid YAML: {exc}"))
                data = None
            if data is not None and not isinstance(data, dict):
                add(_finding(
                    repo, "profile-parse",
                    f"{origin}: profile must be a YAML mapping, got "
                    f"{type(data).__name__}"))
                data = None
            if isinstance(data, dict):
                for violation in iter_schema_violations(
                        data, SCHEMA_DIR / "project.schema.json"):
                    add(_finding(
                        repo, "profile-schema",
                        f"{origin}: schema violation at {violation}"))
                for tag, message in _profile_semantic_violations(
                        data, origin):
                    add(_finding(repo, "profile-semantics", message, tag=tag))
                # F-100: the provider.models deprecation note —
                # informational (never fatal): the block still overrides
                # the stack for one release, but a registry edit cannot
                # reach a seat pinned there.
                from factory.models import deprecation_notes

                for note in deprecation_notes(data):
                    add(_finding(repo, "model-deprecation", note,
                                 tag="model_deprecation", fatal=False,
                                 fix="move the project to model_stack and "
                                 "re-approve (`factory approve --profile`) "
                                 "— a registry edit cannot reach a seat "
                                 "pinned by provider.models"))
                if not [f for f in report.findings
                        if f.check.startswith("profile-")]:
                    profile = apply_profile_defaults(data)
    elif profile_required:
        add(_finding(
            repo, "profile-parse",
            f"{profile_path}: cannot read file: no profile at this path "
            "— a run needs project.yaml next to the queue"))

    if approval_check and profile is not None:
        from factory.approvals import (
            ApprovalLedger,
            ApprovalLedgerError,
            changed_groups,
        )
        root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
        try:
            ledger = ApprovalLedger.for_project(root, profile["name"])
            approval = ledger.latest_profile_approval()
        except (ApprovalLedgerError, KeyError) as exc:
            add(_finding(
                repo, "profile-approval",
                f"{origin}: the approval ledger refused to load — {exc}"))
        else:
            if approval is None:
                add(_finding(
                    repo, "profile-approval",
                    f"{origin}: no profile approval on record for project "
                    f"{profile['name']!r} — run `factory approve --profile` "
                    "once so the owner has seen the security-relevant "
                    "content (commands, sentinels, network/docs allowlists, "
                    "sandbox, repo, paths, live_checks, toolkit, provider, "
                    "data_owner)"))
            else:
                changed = changed_groups(profile, approval)
                if changed:
                    add(_finding(
                        repo, "profile-approval",
                        f"{origin}: the profile changed since its owner "
                        f"approval and no session or gate may run under it "
                        f"— changed: {', '.join(changed)}; re-approve with "
                        "`factory approve --profile`"))

    # -------------------------------------------------------- queue side --
    doc: dict | None = None
    if not queue_path.exists():
        if queue_required:
            add(_finding(
                repo, "queue-parse",
                f"{queue_path}: cannot read file: no queue at this path — "
                "a run needs .factory/tasks.json (factory import builds "
                "one from the planning docs)"))
    else:
        origin = str(queue_path)
        try:
            import json
            doc = json.loads(read_file(queue_path, origin))
            if not isinstance(doc, dict):
                add(_finding(
                    repo, "queue-parse",
                    f"{origin}: the queue is not a JSON object "
                    f"(got {type(doc).__name__})"))
                doc = None
        except json.JSONDecodeError as exc:
            add(_finding(repo, "queue-parse",
                         f"{origin}: invalid JSON: {exc}"))
            doc = None
        except ContractError as exc:
            add(_finding(repo, "queue-parse", str(exc)))
            doc = None
        if doc is not None:
            from factory.contract import budget_caps
            from factory.epochs import current_epoch
            for violation in iter_schema_violations(
                    doc, SCHEMA_DIR / "tasks.schema.json"):
                tag = "baseline"
                path = violation.split(":", 1)[0]
                parts = path.split("/")
                if (len(parts) >= 4 and parts[0] == "tasks"
                        and parts[2] == "budget"
                        and parts[3] in ("max_turns", "wall_clock_min")):
                    tag = "budget"  # F-64's caps
                add(_finding(
                    repo, "queue-schema",
                    f"{origin}: schema violation at {violation}", tag=tag))
            for tag, message in _semantic_violations(
                    doc.get("tasks"), origin):
                add(_finding(repo, "queue-semantics", message, tag=tag))
            version = doc.get("version")
            if isinstance(version, int) and not isinstance(version, bool):
                current = current_epoch()
                if version < current:
                    add(_finding(
                        repo, "queue-epoch",
                        f"{origin}: the queue records contract epoch "
                        f"{version}; the current epoch is {current} — the "
                        "rules it was last written under. The epoch gap "
                        "alone violates nothing; the next factory write "
                        "re-stamps it (the registered automatic rewrite)",
                        tag="baseline", fatal=False))
                elif version > current:
                    from factory.epochs import version_ceiling_error
                    add(_finding(
                        repo, "queue-epoch",
                        f"{origin}: "
                        f"{version_ceiling_error(version)}",
                        tag="baseline", fatal=True, wo="F-66",
                        fix="upgrade the factory (git pull), or restore "
                            "the queue this factory wrote"))
            # What approval would refuse (reported; an owner decision):
            caps = budget_caps()
            for task in doc.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                kind = task.get("kind")
                tid = task.get("id") if isinstance(
                    task.get("id"), str) else "<missing id>"
                scope_paths = ((task.get("scope") or {}).get("paths")
                               if isinstance(task.get("scope"), dict)
                               else None) or []
                # Only a draft can be approved (approve refuses any other
                # status), so only a draft's missing scope is an approval
                # the owner still owes; a task past draft is history.
                if (kind in _eligible_kinds() and not scope_paths
                        and task.get("status") == "draft"):
                    add(_finding(
                        repo, "task-approvability",
                        f"{origin}: task {tid} is a {kind} contract with no "
                        "declared file scope — approving it would refuse "
                        "(add scope.paths: the repo-relative globs this "
                        "task may change; F-65)",
                        tag="scope", fatal=False))
                budget = task.get("budget")
                if isinstance(budget, dict):
                    for name, cap in caps.items():
                        value = budget.get(name)
                        if isinstance(value, int) \
                                and not isinstance(value, bool) \
                                and value > cap:
                            add(_finding(
                                repo, "task-approvability",
                                f"{origin}: task {tid} budget.{name} is "
                                f"{value}, over the cap of {cap} — the "
                                "queue does not load, and approving it "
                                "would refuse (F-64)",
                                tag="budget", fatal=False))
    return report


def _repo_report(repo: Path, factory_root: Path | None) -> Report:
    """One repo's report at the standard paths."""
    factory_dir = repo / ".factory"
    return project_report(
        factory_dir / "project.yaml", factory_dir / "tasks.json",
        factory_root=factory_root,
    )


def factory_report(
    factory_root: Path | str | None, *, extra_repos: list[Path] | None = None,
) -> list[Report]:
    """Reports for every repo the approval ledger records, plus any
    ``extra_repos``. Read-only: nothing is written anywhere."""
    from factory.approvals import ApprovalLedger, ApprovalLedgerError

    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    reports: list[Report] = []
    seen: set[Path] = set()
    ledger_dir = root / "approvals"
    if ledger_dir.is_dir():
        for ledger_file in sorted(ledger_dir.glob("*.jsonl")):
            project = ledger_file.stem
            report = Report(repo=project)
            repo_path: str | None = None
            try:
                ledger = ApprovalLedger(ledger_file, project)
                approvals = [entry for entry in ledger.entries()
                             if entry.get("kind") == "profile"]
                repo_path = (approvals[-1].get("repo_path")
                             if approvals else None)
            except (ApprovalLedgerError, OSError) as exc:
                report.findings.append(_finding(
                    project, "ledger-repo-path",
                    f"{ledger_file}: the approval ledger refused to load — "
                    f"{exc}", wo="F-66",
                    fix=f"inspect {ledger_file} (append-only, hash-chained): "
                        "restore it from a backup, then "
                        + REAPPROVE_FIX))
            if repo_path is None:
                report.findings.append(_finding(
                    project, "ledger-repo-path",
                    f"{ledger_file}: no profile approval with a recorded "
                    "repo path — approvals from before F-66 lack the field; "
                    "re-approve the profile so doctor can check this "
                    "project's repo (`factory approve --profile`)",
                    fatal=False, wo="F-66", fix=REAPPROVE_FIX))
                reports.append(report)
                continue
            resolved = _resolve_repo(repo_path)
            if resolved is None:
                report.findings.append(_finding(
                    project, "ledger-repo-path",
                    f"{project}: the recorded repo path {repo_path!r} does "
                    "not exist (moved or cloned elsewhere?) — re-approve "
                    "the profile from its new location "
                    "(`factory approve --profile`)",
                    fatal=False, wo="F-66", fix=REAPPROVE_FIX))
                reports.append(report)
                continue
            if Path(repo_path).is_symlink():
                report.findings.append(_finding(
                    project, "ledger-repo-path",
                    f"{project}: the recorded repo path {repo_path!r} is a "
                    f"symlink to {str(resolved)!r} — checking the target; "
                    "record the real path at the next profile approval",
                    fatal=False, wo="F-66", fix=REAPPROVE_FIX))
            if resolved in seen:
                continue
            seen.add(resolved)
            repo_report = _repo_report(resolved, root)
            repo_report.findings[0:0] = report.findings
            reports.append(repo_report)
    for raw in extra_repos or []:
        resolved = _resolve_repo(str(raw))
        label = str(raw)
        findings: list[Finding] = []
        if Path(raw).is_symlink():
            findings.append(_finding(
                label, "ledger-repo-path",
                f"{label}: the repo path is a symlink to "
                f"{str(resolved)!r} — checking the target; record the "
                "real path at the next profile approval",
                fatal=False, wo="F-66", fix=REAPPROVE_FIX))
        if resolved is None:
            findings.append(_finding(
                label, "ledger-repo-path",
                f"{label}: the repo path does not exist", fatal=False,
                wo="F-66", fix="check the --repo path"))
            reports.append(Report(repo=label, findings=findings))
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        repo_report = _repo_report(resolved, root)
        repo_report.findings[0:0] = findings
        reports.append(repo_report)
    return reports


def _resolve_repo(raw: str) -> Path | None:
    """The resolved repo path when it names an existing directory."""
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError:  # pragma: no cover - defensive
        return None
    return resolved if resolved.is_dir() else None


def render(reports: "list[Report]") -> str:
    """The doctor report, one block per repo, every finding with its
    epoch, WO and fix."""
    from factory.epochs import current_epoch

    lines = [f"# factory doctor — epoch {current_epoch()}"]
    if not reports:
        lines.append("no repos recorded in the approval ledger and none "
                     "given with --repo")
        return "\n".join(lines) + "\n"
    for report in reports:
        if report.findings:
            lines.append(f"\n{report.repo}: {len(report.findings)} finding(s)")
            for finding in report.findings:
                lines.append(f"  [{finding.check}] {finding.message}")
                lines.append(
                    f"      rule: epoch {finding.epoch}, {finding.wo}: "
                    f"{finding.change}"
                )
                if finding.fix:
                    lines.append(f"      fix: {finding.fix}")
        else:
            lines.append(f"\n{report.repo}: clean")
    return "\n".join(lines) + "\n"


def preflight_run(
    queue_path: Path | str, profile_path: Path | str | None,
    factory_root: Path | str | None = None,
) -> None:
    """The run/loop pre-run check (F-66 AC-2): every rule this repo
    must meet, together, BEFORE any single loader's first-error
    refusal. Refuses (ContractError carrying doctor's FULL report —
    every finding, fatal or not) on the fatal subset: a profile or
    queue that cannot load, or schema/semantic violations. A stale
    epoch on an otherwise-valid queue and draft tasks approval would
    refuse are reported findings, never run-refusing: the first is
    healed by the next write's re-stamp, the second is the owner's
    decision to make. Profile-approval is enforced where it already
    was, with its own equally-named message."""
    from factory.epochs import EpochError

    profile_path = (Path(profile_path) if profile_path
                    else Path(queue_path).parent / "project.yaml")
    try:
        report = project_report(
            profile_path, queue_path, factory_root=factory_root,
            approval_check=False,
        )
    except EpochError as exc:
        raise ContractError(str(exc)) from exc
    fatal = [finding for finding in report.findings if finding.fatal]
    if fatal:
        raise ContractError(
            "this repo does not meet the current contract rules — "
            "factory doctor's full report:\n"
            + render([report]).rstrip()
        )
