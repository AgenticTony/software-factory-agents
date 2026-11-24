"""Replay benchmark of merged tasks with hidden tests (F-104).

F-100 made swapping any model a one-line registry edit, and the eval
(F-16/F-95/F-99) measures only the reviewers, on seeded defects.
Nothing measured the implementer, so "GLM 5.3 → 5.4" was a leap of
faith whose first real evidence would be a client PR. This module is
that measurement: replay a merged task's approved contract against a
clone of the repo as it was BEFORE the work, run the real production
attempt path over it, then — outside the session — copy the merged
task's own tests in and run them on whatever the replay produced.

The costlier failure is a benchmark the model can cheat by seeing the
merged answer, so leak prevention comes first and every item proves
itself before it counts:

* **AC-1** (:func:`bench_add`) — the hidden tests must FAIL on the base
  commit and PASS on the merged head three times (the flake guard),
  through the REAL L1 red-first proof; a contract that quotes the
  merged diff refuses (F-99's rule); the store under
  ``<factory_root>/bench/<project>/`` is schema-validated on load and
  lives outside every sandbox.
* **AC-2** (:func:`sanitize_origin`) — each replay runs in a clone made
  from the base commit ALONE: one branch, no remote, no tags, packs
  rebuilt, and VERIFIED (``git cat-file --batch-all-objects`` holds no
  commit from base..head) before any session starts. The hidden tests
  never enter the run dir or a prompt: they live in the item store, are
  checked against everything the parent has built BEFORE any session
  starts (:func:`_leak_check` — after the session a correct implementer
  converges toward them by design, so only the pre-session state can be
  judged), and are copied into a scoring clone only after the attempt
  settles. Task ids, branches and commit messages run on opaque
  ``BENCH-NN`` slots, as the eval's ``EVAL-NN`` do.
* **AC-3** (:func:`_replay_one`) — the replay is the REAL pipeline:
  ``factory.orchestrator._run_task`` with publishing replaced by
  scoring (the PR seam records "would publish"; the push lands on the
  sanitized local origin, never on GitHub), with the project's own
  profile and the owner's own registry config. A replay's clean merge
  needs BOTH the hidden tests passing AND the factory would have
  published — an escalation is never a clean merge, whatever the
  hidden tests say on its kept head.
* **AC-4** (:func:`bench_run`, :func:`compare_reports`) — the report
  gives per item k/N clean merges, the yield figure (clean merges per
  unit cost), cost per clean merge, the models that ran per role, and
  ``--against`` lists regressions (pass → fail) first. A ``--role``
  override rides the config in memory only (F-100 AC-5's rules).
* **AC-5** (:func:`bench_swap_note`) — ``factory doctor`` notes a
  registry whose implementer model differs from the one the latest
  bench report ran; the README and the registry docs state the rule.

Paid runs are the owner's: ``factory bench run`` drives real model
sessions (the default seat runner) and is never called by anything but
this command, exactly like ``factory eval``. Everything in the test
suite is scripted.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from factory.approvals import ApprovalLedger
from factory.config import CONFIG_FILE, FactoryConfig, load_config
from factory.contract import (
    SCHEMA_DIR as _CONTRACT_SCHEMA_DIR,
)
from factory.contract import (
    ContractError,
    acceptance_hash,
    iter_schema_violations,
    load_profile,
    load_tasks,
)
from factory.epochs import current_epoch
from factory.gates import (
    GateError,
    contract_test_files,
    contract_test_refs,
)
from factory.gates import (
    git as hardened_git,
)

# The l1 privates are deliberate reach-ins, the eval's precedent
# (eval.py's orchestrator imports): the add-time proof and the scoring
# run the SAME per-ref command L1 runs, never a copy that could drift.
from factory.l1 import DEFAULT_TEST_REF, _ref_command, not_collected_evidence

# The orchestrator/session names below are bound HERE so the tests (and
# only the tests) can swap them at this module's globals — the
# call-time seam lesson (F-51): a from-import at the consumer keeps the
# patch surface alive. A1: bench_run's default seat runner is the REAL
# sandboxed session runner, exactly run_once's default. A4: the
# add-time proofs and the hidden-test scoring run project commands
# through the sandboxed GATE runner — the replay head is agent code
# (gates.run_command is the fixtures-only shape its own docstring
# disclaims for production).
from factory.orchestrator import _gate_settings, sandboxed_runner
from factory.runs import (
    FACTORY_ROOT_DEFAULT,
    GIT_HARDENING,
    RunDir,
    cleanup_run,
    create_run,
    new_run_id,
    sandbox_settings_path,
)
from factory.session import probe_session_interpreter, run_session_sandboxed

__all__ = [
    "BENCH_ITEM_SCHEMA",
    "BenchError",
    "BenchItem",
    "ADD_GREEN_RUNS",
    "bench_add",
    "bench_run",
    "bench_swap_note",
    "compare_reports",
    "latest_bench_report",
    "load_items",
    "render_report",
    "sanitize_origin",
]

# The item schema ships in the package (the F-51 rule) beside the
# queue/profile schemas; the epochs fingerprint covers exactly those
# two, so adding this file needs no epochs entry.
BENCH_ITEM_SCHEMA = _CONTRACT_SCHEMA_DIR / "bench-item.schema.json"

# The bench task's budget: the schema's own ceilings minus headroom. A
# replay must not truncate an implementer the real task let finish (the
# score would measure the budget, not the model), and must stay inside
# the caps every queue validates.
BENCH_MAX_TURNS = 100
BENCH_MAX_ATTEMPTS = 2
BENCH_WALL_CLOCK_MIN = 150

# AC-1's flake guard: the hidden tests run green on the merged head
# this many times at add (L1's own green pass is the first).
ADD_GREEN_RUNS = 3

# AC-1's quote check: an added diff line at least this long (stripped)
# appearing verbatim in the contract text is a quoted answer.
QUOTE_MIN_LINE = 12

# Per-command cap for the add-time proofs and the post-replay scoring.
_PROOF_TIMEOUT_SEC = 900
# The setup window (A6): a cold venv + npm install outlasts a gate
# command; this is a floor on the owner's patience, not a ceiling on
# correctness.
_SETUP_TIMEOUT_SEC = 1800


class BenchError(RuntimeError):
    """The bench cannot run as asked (bad item, git failure, a leak)."""


# ------------------------------------------------------------- the store --


@dataclass(frozen=True)
class BenchItem:
    """One loaded, schema-validated bench item."""

    slot: str
    project: str
    source_task: str
    source_pr: int
    repo_path: str
    default_branch: str
    base: str
    head: str
    added: str
    contract: dict
    hidden_refs: tuple
    hidden_files: tuple

    @classmethod
    def from_dict(cls, raw: dict) -> "BenchItem":
        return cls(
            slot=str(raw["slot"]), project=str(raw["project"]),
            source_task=str(raw["source_task"]),
            source_pr=int(raw["source_pr"]),
            repo_path=str(raw["repo_path"]),
            default_branch=str(raw["default_branch"]),
            base=str(raw["base"]), head=str(raw["head"]),
            added=str(raw["added"]), contract=dict(raw["contract"]),
            hidden_refs=tuple(raw["hidden_refs"]),
            hidden_files=tuple(raw["hidden_files"]),
        )

    def item_dir(self, factory_root: Path) -> Path:
        return bench_dir(factory_root) / self.project / self.slot

    def hidden_dir(self, factory_root: Path) -> Path:
        return self.item_dir(factory_root) / "hidden"

    def hidden_files_text(self, factory_root: Path) -> dict:
        """The hidden tests' bytes from the store: path → text."""
        out: dict[str, str] = {}
        root = self.hidden_dir(factory_root)
        for rel in self.hidden_files:
            path = root / rel
            if not path.is_file():
                raise BenchError(
                    f"{self.slot}: the store is missing the hidden file "
                    f"{rel} ({path}) — re-add the item"
                )
            out[rel] = path.read_text(encoding="utf-8")
        return out


def bench_dir(factory_root: Path | str | None) -> Path:
    """The bench root: ``<factory_root>/bench/``."""
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    return root / "bench"


def _validate_item_doc(doc, origin: str) -> None:
    violations = list(iter_schema_violations(doc, BENCH_ITEM_SCHEMA))
    if violations:
        raise BenchError(
            f"{origin}: bench item schema violation at {violations[0]} "
            f"({len(violations)} total) — the store refuses to load; "
            "re-add the item or restore it from a backup"
        )


def load_items(factory_root: Path | str | None, project: str) -> list:
    """Every item of one project, schema-validated on load (the F-02
    rule), sorted by slot. Refuses the whole store on any violation —
    a benchmark over an unvalidated item measures nothing."""
    root = bench_dir(factory_root) / project
    if not root.is_dir():
        raise BenchError(
            f"no bench items for project {project!r} under {root} — add "
            "one first (`factory bench add <project> <task-id | pr-number>`)"
        )
    items: list[BenchItem] = []
    seen_heads: set[str] = set()
    for item_dir in sorted(root.iterdir()):
        doc_path = item_dir / "item.json"
        if not doc_path.is_file():
            continue
        try:
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BenchError(f"{doc_path}: cannot read the item: {exc}") \
                from exc
        _validate_item_doc(doc, str(doc_path))
        if doc["project"] != project:
            raise BenchError(
                f"{doc_path}: names project {doc['project']!r} but lives "
                f"under {project!r}"
            )
        if doc["head"] in seen_heads:
            raise BenchError(
                f"{doc_path}: two items share the merged head "
                f"{doc['head'][:12]} — the store refuses to load"
            )
        seen_heads.add(doc["head"])
        _check_hidden_paths(str(doc_path), doc["hidden_files"])
        items.append(BenchItem.from_dict(doc))
    if not items:
        raise BenchError(
            f"no bench items for project {project!r} under {root} — add "
            "one first (`factory bench add <project> <task-id | pr-number>`)"
        )
    return items


def _resolve_project_repo(factory_root: Path | str | None,
                          project: str) -> Path:
    """The project's repo, from the approval ledger the way doctor reads
    it (F-66): the latest profile approval's recorded repo path."""
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    ledger = ApprovalLedger.for_project(root, project)
    approval = ledger.latest_profile_approval()
    repo_path = (approval or {}).get("repo_path")
    if not isinstance(repo_path, str) or not repo_path:
        raise BenchError(
            f"project {project!r} has no profile approval recording its "
            f"repo path under {root}/approvals/ — run `factory approve "
            "--profile` from the project's repo first"
        )
    repo = Path(repo_path).expanduser()
    if not (repo / ".git").exists():
        raise BenchError(
            f"project {project!r} records the repo path {repo_path}, which "
            "is not a git repository (moved? cloned elsewhere?) — "
            "re-approve the profile from its new location"
        )
    return repo


def _git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise BenchError(
            f"git {' '.join(args[:2])} failed in {repo}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout.strip()


def _git_raw(repo: Path, *args: str) -> str:
    """Like :func:`_git_out` but WITHOUT the strip: a blob's exact bytes
    (the hidden tests are stored verbatim)."""
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise BenchError(
            f"git {' '.join(args[:2])} failed in {repo}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True, text=True,
    ).returncode == 0


# --------------------------------------------------------------- AC-1 add --


def _added_lines(repo: Path, base: str, head: str) -> list:
    """The diff's added lines (``base...head``), stripped."""
    diff = _git_out(repo, "diff", "--no-color", "--no-ext-diff",
                    f"{base}...{head}")
    lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            if len(stripped) >= QUOTE_MIN_LINE:
                lines.append(stripped)
    return lines


def _contract_quote(contract_text: str, added: list) -> str | None:
    """F-99's rule at add: the contract states the task, never the
    answer. A stripped added line of the merged diff appearing verbatim
    in the contract text is a quoted answer — name it."""
    for line in added:
        if line in contract_text:
            return line
    return None


def bench_add(factory_root: Path | str | None, project: str, source: str,
              *, command_runner=None) -> dict:
    """Add one benchmark item from a merged task (AC-1).

    ``source`` is the task id or the merged PR number. The item is the
    task's approved contract plus its hidden tests (the files its
    ``test`` acceptance refs name), and it proves itself before it
    counts: through the REAL L1 red-first proof in a disposable clone —
    the hidden tests must fail on the base commit and pass on the
    merged head, ``ADD_GREEN_RUNS`` times (L1's green pass plus the
    extra runs below). A contract that quotes the merged diff refuses
    (checked first — the cheapest and the most decisive). The project
    repo is only read (the proofs run in a clone under the bench root).

    Returns:
        ``{"slot", "item", "item_dir", "hidden_dir"}``.

    Raises:
        BenchError: every refusal, named (unmerged task, missing links,
            a base that cannot be derived, hidden tests that pass on
            base or fail on head, a quoted contract, a duplicated head).
    """
    runner = command_runner
    repo = _resolve_project_repo(factory_root, project)
    queue_path = repo / ".factory" / "tasks.json"
    try:
        queue = load_tasks(queue_path)
    except ContractError as exc:
        raise BenchError(f"the project queue refused to load: {exc}") \
            from exc
    task = _find_task(queue, source)
    tid = task.get("id") or source

    links = task.get("links") or {}
    pr_url = links.get("pr")
    if not pr_url or links.get("merged") != pr_url:
        raise BenchError(
            f"{tid}: not a merged task — the bench replays work that "
            "reached the default branch (links.merged must equal links.pr; "
            "an open or unmerged PR has no proven hidden-test pass)"
        )
    head = links.get("pushed_head")
    # The branch NAME in the links is informational only: GitHub
    # deletes merged branches, so no local ref may reach the head (A2).
    # The pushed-head COMMIT existing is the real precondition; the
    # proof clone below fetches it explicitly, reachable or not.
    if not head or not _git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        raise BenchError(
            f"{tid}: the recorded pushed head {head!r} is not a commit in "
            "the repo — the item cannot be proven"
        )
    # One item per merged head: a duplicated item double-weights the
    # same work in every aggregate. The existing store still validates.
    store = bench_dir(factory_root) / project
    if store.is_dir() and any(
            (entry / "item.json").is_file() for entry in store.iterdir()):
        for other in load_items(factory_root, project):
            if other.head == head:
                raise BenchError(
                    f"{tid}: there is already an item for this merged head "
                    f"({other.slot}, from {other.source_task}) — one item "
                    "per merged task"
                )
    profile = load_profile(repo / ".factory" / "project.yaml")
    default_branch = (profile.get("repo") or {}).get(
        "default_branch") or "main"
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{default_branch}"):
        raise BenchError(
            f"{tid}: the repo has no branch {default_branch!r} to derive "
            "the base from"
        )
    _refuse_stale_default(repo, default_branch, tid)
    base = _derive_base(repo, head, default_branch)
    if base == head:
        raise BenchError(
            f"{tid}: the default branch descends from the pushed head in a "
            "way no rule below derives a base from — refusing rather than "
            "guessing the fork point"
        )
    if base == _git_out(repo, "rev-parse", f"refs/heads/{default_branch}"):
        raise BenchError(
            f"{tid}: no work between the base and the merged head — the "
            "item would replay an empty task"
        )

    refs = contract_test_refs(task)
    if not refs:
        raise BenchError(
            f"{tid}: the contract names no test-checked acceptance — "
            "there are no hidden tests to score a replay with"
        )
    files = sorted(contract_test_files(task))
    _check_hidden_paths(tid, files)
    for path in files:
        if not _git_ok(repo, "cat-file", "-e", f"{head}:{path}"):
            raise BenchError(
                f"{tid}: the hidden test file {path} is not on the merged "
                f"head {head[:12]} — the acceptance ref names a file the "
                "merged work does not carry"
            )
    source_path = str(task.get("source") or "").partition("#")[0]
    if not source_path or not _git_ok(repo, "cat-file", "-e",
                                      f"{base}:{source_path}"):
        raise BenchError(
            f"{tid}: the task's spec doc ({task.get('source')!r}) is not "
            "on the base commit — the merged diff carried its own spec, so "
            "a replay could not hand the implementer what the real run "
            "handed it"
        )

    scope = task.get("scope")
    scope_paths = (scope.get("paths")
                   if isinstance(scope, dict) else None) or []
    contract = {
        "title": str(task.get("title") or ""),
        "source": str(task.get("source") or ""),
        "acceptance": copy.deepcopy(task.get("acceptance") or []),
        "scope": list(scope_paths),
        "risk": list(task.get("risk") or []),
        "out_of_scope": list(task.get("out_of_scope") or []),
        "dependencies": copy.deepcopy(task.get("dependencies") or []),
    }
    contract_text = "\n".join(
        [contract["title"]]
        + [str(item.get("text") or "")
           for item in contract["acceptance"]
           if isinstance(item, dict)]
        + contract["out_of_scope"]
    )
    quoted = _contract_quote(
        contract_text, _added_lines(repo, base, head))
    if quoted is not None:
        raise BenchError(
            f"{tid}: the contract quotes the merged diff (F-99's rule: it "
            f"must state the task, never the answer) — the added line "
            f"{quoted[:80]!r} appears verbatim in the contract text; "
            "rewrite the criterion as intent and re-run bench add"
        )

    # The proofs run in a disposable clone: the parent never checks the
    # project's own checkout out. The head is fetched into the clone BY
    # SHA (A2): GitHub deletes merged branches, so no ref may reach it —
    # the upload-pack override serves the exact commit the task record
    # names, whatever reachability says. The commands run through the
    # sandboxed gate runner (A4): the hidden tests are project code.
    stamp = new_run_id()
    add_root = bench_dir(factory_root) / project / f".add-{stamp}"
    add_root.mkdir(parents=True)
    settings_path = sandbox_settings_path(
        bench_dir(factory_root).parent, project, stamp)
    try:
        val = _proof_clone(repo, add_root / "val", head)
        gate = RunDir(project=project, run_id=stamp, root=val,
                      settings_path=settings_path)
        _gate_settings(profile, gate, bench_dir(factory_root).parent)
        proof_runner = runner or sandboxed_runner(
            val, settings_path, srt_bin_path=None)
        # A6: the proofs need the environment the profile's setup builds
        # (a venv, generated modules) — run it on the merged head's tree
        # before L1, through the same gate runner. L1's between-phase
        # cleans keep declared `paths.setup_outputs` and the gate venv
        # exactly as in production, so one run carries red, green and
        # the extra flake-guard greens.
        hardened_git(val, "checkout", "-q", "--force", head)
        setup_failure = _run_setup(profile, proof_runner, val, "add")
        if setup_failure is not None:
            raise BenchError(
                f"{tid}: {setup_failure} on the merged head — the item "
                "cannot be proven without the project's environment; fix "
                "the setup command or the repo and re-run bench add"
            )
        # L1 IS the add-time proof: red on the merge-base with only the
        # diff's test files, green on the head, through the profile's own
        # per-ref command.
        try:
            report = _run_l1_for_add(profile, val, base, head, task,
                                     proof_runner)
        except GateError as exc:
            raise BenchError(
                f"{tid}: the add-time proof failed: {exc}") from exc
        findings = [f for f in report.get("findings") or []]
        if findings:
            proving = [f for f in findings
                       if f.get("id") == "test_proves_nothing"]
            if proving:
                raise BenchError(
                    f"{tid}: the hidden tests PASS on the base commit — "
                    f"they prove nothing ({_finding_texts(proving)}); an "
                    "item must fail on base and pass on the merged head"
                )
            raise BenchError(
                f"{tid}: the hidden tests fail on the merged head "
                f"({_finding_texts(findings)}); an item must fail on base "
                "and pass on the merged head"
            )
        # The flake guard: L1's green pass is run 1; ADD_GREEN_RUNS - 1
        # more green passes on the exact merged head.
        hardened_git(val, "checkout", "--force", head)
        template = ((profile.get("commands") or {}).get("test_ref")
                    or DEFAULT_TEST_REF)
        for ref in refs:
            for _ in range(ADD_GREEN_RUNS - 1):
                result = proof_runner(f"green:{ref}",
                                      _ref_command(template, ref),
                                      val, _PROOF_TIMEOUT_SEC)
                evidence = not_collected_evidence(result, ref)
                if result.timed_out or result.exit_code != 0 \
                        or evidence is not None:
                    raise BenchError(
                        f"{tid}: the hidden test {ref} did not pass every "
                        "green run on the merged head (a flaky hidden test "
                        "cannot score a replay)"
                    )
        hidden = {
            path: _git_raw(repo, "show", f"{head}:{path}")
            for path in files
        }
    finally:
        shutil.rmtree(add_root, ignore_errors=True)
        settings_path.unlink(missing_ok=True)

    # N3 (my call: refuse): hidden tests with no assert lines leave the
    # leak check nothing to hunt — the item would replay unchecked.
    if not _leak_markers(hidden):
        raise BenchError(
            f"{tid}: the hidden tests carry no assert lines — no leak "
            "marker can be computed, and the bench refuses to replay an "
            "item it cannot check (assert-style hidden tests, e.g. "
            "pytest, are the supported shape; jest/vitest-style items "
            "are future work)"
        )

    # The slot: the next free BENCH-NN. Allocated by mkdir itself (N2):
    # two parallel adds cannot take the same slot — the directory is the
    # reservation, so a collision retries on the next number.
    project_root = bench_dir(factory_root) / project
    project_root.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        slot = f"BENCH-{number:02d}"
        try:
            item_dir = project_root / slot
            item_dir.mkdir(exist_ok=False)
            break
        except FileExistsError:
            number += 1
            if number > 9999:
                raise BenchError(
                    f"no free bench slot under {project_root} (tried "
                    "BENCH-0001..BENCH-9999)"
                ) from None

    doc = {
        "slot": slot,
        "project": project,
        "source_task": str(tid),
        "source_pr": _pr_number(pr_url),
        "repo_path": str(repo),
        "default_branch": default_branch,
        "base": base,
        "head": head,
        "added": datetime.now(timezone.utc).isoformat(),
        "contract": contract,
        "hidden_refs": list(refs),
        "hidden_files": list(files),
    }
    _validate_item_doc(doc, "new item")
    try:
        (item_dir / "item.json").write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        for path, text in hidden.items():
            target = item_dir / "hidden" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    except BaseException:
        # release the reservation a failed write holds
        shutil.rmtree(item_dir, ignore_errors=True)
        raise
    return {
        "slot": slot,
        "item": BenchItem.from_dict(doc),
        "item_dir": item_dir,
        "hidden_dir": item_dir / "hidden",
    }


def _check_hidden_paths(where: str, paths) -> None:
    """N1: every hidden-file path is a plain repo-relative path — not
    absolute, no ``..`` segment. The paths are written under the
    store's ``hidden/`` and copied into the score clone; a climbing or
    absolute path would write outside them.

    Raises:
        BenchError: the first offending path, named with its shape.
    """
    for path in paths:
        text = str(path)
        if not text or text.strip() != text:
            raise BenchError(
                f"{where}: the hidden file path {text!r} is empty or "
                "padded")
        if Path(text).is_absolute():
            raise BenchError(
                f"{where}: the hidden file path {text!r} is absolute — "
                "hidden files are repo-relative")
        if ".." in Path(text).parts:
            raise BenchError(
                f"{where}: the hidden file path {text!r} climbs (..) — "
                "hidden files are repo-relative and cannot escape the "
                "store's hidden/ or the score clone")


def _refuse_stale_default(repo: Path, default_branch: str, tid: str) \
        -> None:
    """A7: refuse to derive a base from a stale local default branch.

    The local checkout can lag the remote (JobFinderOS: 16 commits).
    Deriving the base from it silently picks the wrong fork point — a
    merge commit that exists only on the remote falls back to a plain
    merge-base far in the past, and the replay would start before other
    tasks' merged work, scoring something meaningless with no error. So
    when the repo tracks a remote default branch and the local one does
    not contain it (behind), refuse, naming the count — the owner pulls
    the project repo; the bench never fetches.
    """
    names = _git_out(repo, "remote").split()
    if not names:
        return
    remote = "origin" if "origin" in names else (
        names[0] if len(names) == 1 else None)
    if remote is None:
        return
    tracking = f"refs/remotes/{remote}/{default_branch}"
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", tracking):
        return
    if _git_ok(repo, "merge-base", "--is-ancestor", tracking,
               f"refs/heads/{default_branch}"):
        return  # the local branch is current or ahead
    behind = _git_out(repo, "rev-list", "--count",
                      f"refs/heads/{default_branch}..{tracking}").strip()
    raise BenchError(
        f"{tid}: the local {default_branch} is {behind} commit(s) behind "
        f"{remote}/{default_branch} — the base would be derived from "
        "stale history (a merge that exists only on the remote sends the "
        "fork point far into the past, and the replay would start in the "
        "wrong place); pull the project repo and re-run bench add (the "
        "bench never fetches)"
    )


def _derive_base(repo: Path, head: str, default_branch: str) -> str:
    """The commit the replay starts from: the fork point of the pushed
    head and the default branch.

    A merge-commit merge (``--no-ff``) puts the pushed commits ON the
    default branch, so the plain merge-base IS the head — the base is
    the fork point with main's PRE-MERGE tip instead: the newest
    first-parent merge commit that contains the head but whose first
    parent does not (a later merge that merely descends from the one
    that brought the head in is skipped exactly by that second half).
    Squash merges (and anything else that leaves no ancestry) keep the
    plain merge-base: the fork point, directly.
    """
    default_ref = f"refs/heads/{default_branch}"
    for merge_sha in _git_out(repo, "rev-list", "--first-parent",
                              "--merges", default_ref).split():
        if not _git_ok(repo, "merge-base", "--is-ancestor", head,
                       merge_sha):
            continue
        if _git_ok(repo, "merge-base", "--is-ancestor", head,
                   f"{merge_sha}^1"):
            continue  # a later merge descending from the real one
        parent = _git_out(repo, "rev-parse", f"{merge_sha}^1")
        return _git_out(repo, "merge-base", head, parent)
    return _git_out(repo, "merge-base", head, default_ref)


def _proof_clone(repo: Path, dest: Path, head: str) -> Path:
    """A disposable clone of the project repo holding the pushed head,
    fetched BY SHA (A2: merged branches get deleted, so no ref may
    reach the head; the upload-pack override serves the exact commit
    the task record names)."""
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "clone", "--quiet", str(repo), str(dest)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot clone {repo} for the add-time proofs: "
            f"{proc.stderr.strip()[:400]}"
        )
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(dest), "fetch", "--quiet",
         "--no-tags",
         "--upload-pack",
         "git -c uploadpack.allowAnySHA1InWant=true upload-pack",
         str(repo), head], capture_output=True, text=True)
    if proc.returncode != 0 or not _git_ok(dest, "cat-file", "-e",
                                           f"{head}^{{commit}}"):
        raise BenchError(
            f"cannot fetch the pushed head {head[:12]} into the proof "
            f"clone: {proc.stderr.strip()[:300]}"
        )
    return dest


def _run_setup(profile: dict, runner, clone: Path, where: str) \
        -> "str | None":
    """Run the profile's ``commands.setup`` through the gate runner
    (A6) — the proofs and the scoring need the environment the project
    builds (a venv, node_modules, generated modules), which a fresh
    clone does not have. Mirrors L0's own invocation (its ``setup``
    command, its failure classes); a heavy project setup gets a longer
    window than a gate command (a cold npm install can outlast one).

    Returns None on success (or when the profile sets no setup), a
    failure description otherwise — the caller refuses (add) or records
    (scoring), never crashes."""
    command = ((profile.get("commands") or {}).get("setup") or "").strip()
    if not command:
        return None
    result = runner(f"setup:{where}", command, clone, _SETUP_TIMEOUT_SEC)
    if result.timed_out:
        return (f"setup failed: timed out after {_SETUP_TIMEOUT_SEC}s")
    if result.exit_code != 0:
        tail = (result.output or "").strip()[-300:]
        return (f"setup failed (exit {result.exit_code})"
                + (f": {tail}" if tail else ""))
    return None


def _run_l1_for_add(profile, val, base, head, task, runner) -> dict:
    from factory.l1 import run_l1

    report = run_l1(profile, val, base, head, task=task, runner=runner,
                    timeout_sec=_PROOF_TIMEOUT_SEC)
    return report.to_dict()


def _finding_texts(findings) -> str:
    texts = [
        f"{f.get('id')}: {(f.get('text') or '')[:120]}"
        for f in findings if isinstance(f, dict)
    ][:3]
    return "; ".join(texts) or "no recorded finding"


def _find_task(queue: dict, source: str) -> dict:
    """The merged task by id, or by PR number (``…/pull/<n>``)."""
    tasks = queue.get("tasks") or []
    for task in tasks:
        if isinstance(task, dict) and task.get("id") == source:
            return task
    if re.fullmatch(r"[0-9]+", source):
        number = int(source)
        suffix = f"/pull/{number}"
        matches = [task for task in tasks if isinstance(task, dict)
                   and str((task.get("links") or {}).get("pr") or "")
                   .endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise BenchError(
                f"PR #{number} matches {len(matches)} tasks — name the "
                "task id instead"
            )
    raise BenchError(
        f"no task {source!r} in the project queue — bench add takes a "
        "task id or a merged PR number"
    )


def _pr_number(pr_url: str) -> int:
    match = re.search(r"/pull/([0-9]+)$", str(pr_url))
    if not match:
        raise BenchError(f"the task's PR link is not a pull-request URL: "
                         f"{pr_url!r}")
    return int(match.group(1))


# ------------------------------------------------------- AC-2 the future --


def sanitize_origin(source: Path, dest: Path, base: str,
                    default_branch: str) -> None:
    """Build the sanitized origin: a repo holding the base commit ALONE.

    Clone, reduce to one branch at the base, drop the remote, every
    other ref and every tag, expire the reflogs and rebuild the packs
    (``gc --prune=now``) — then VERIFY, fail closed, before any session
    could see it: exactly one ref (the default branch, at the base), no
    remote, and EVERY object in the store reachable from the base
    (``cat-file --batch-all-objects`` ⊆ ``rev-list --objects <base>``
    — the general check, not a base..head list: a squash commit, a
    merge commit or any straggler fails it, whatever it is). A leak
    here is the benchmark's costlier failure: the model could read the
    merged answer out of the object database.

    Raises:
        BenchError: the construction or the verification failed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "clone", "--quiet", str(source),
         str(dest)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot clone {source} for the replay: "
            f"{proc.stderr.strip()[:400]}"
        )
    hardened_git(dest, "checkout", "-q", "--detach", base)
    hardened_git(dest, "branch", "-f", default_branch, base)
    refnames = [
        line.strip()
        for line in _git_out(dest, "for-each-ref", "--format=%(refname)")
        .splitlines() if line.strip()
    ]
    keep = f"refs/heads/{default_branch}"
    for ref in refnames:
        if ref != keep:
            hardened_git(dest, "update-ref", "-d", ref)
    if (dest / ".git" / "refs" / "remotes").exists():
        shutil.rmtree(dest / ".git" / "refs" / "remotes",
                      ignore_errors=True)
    for name in ("ORIG_HEAD", "FETCH_HEAD"):
        (dest / ".git" / name).unlink(missing_ok=True)
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(dest), "remote", "remove",
         "origin"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot remove the clone's remote: {proc.stderr.strip()[:300]}"
        )
    hardened_git(dest, "reflog", "expire", "--expire=now", "--all")
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(dest), "gc", "--prune=now",
         "--quiet"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot rebuild the origin's packs: {proc.stderr.strip()[:300]}"
        )
    hardened_git(dest, "checkout", "-q", default_branch)

    # The verification (fail closed BEFORE any session starts).
    refnames = [
        line.strip()
        for line in _git_out(dest, "for-each-ref", "--format=%(refname)")
        .splitlines() if line.strip()
    ]
    if refnames != [keep]:
        raise BenchError(
            f"the sanitized origin holds refs {refnames} — only "
            f"{keep} at the base may exist; refusing the replay"
        )
    if _git_out(dest, "rev-parse", f"refs/heads/{default_branch}") != base:
        raise BenchError(
            "the sanitized origin's branch does not point at the base "
            "commit; refusing the replay"
        )
    if _git_out(dest, "remote") != "":
        raise BenchError(
            "the sanitized origin kept a remote — the future must be "
            "unreachable; refusing the replay"
        )
    objects = _git_out(dest, "cat-file", "--batch-all-objects",
                       "--batch-check")
    have = {line.split(" ", 1)[0] for line in objects.splitlines()
            if line.strip()}
    allowed = {
        line.split(" ", 1)[0]
        for line in _git_out(dest, "rev-list", "--objects", base)
        .splitlines() if line.strip()
    }
    extra = sorted(have - allowed)
    if extra:
        raise BenchError(
            f"the sanitized origin holds {len(extra)} object(s) not "
            f"reachable from the base ({extra[0][:12]} …) — the merged "
            "answer must be unreachable; refusing the replay"
        )


def _leak_markers(hidden: dict) -> list:
    """The hidden tests' assert lines — the strings whose appearance in
    parent-built state before the session means the answer leaked.
    Trivial lines (imports, defs) are excluded: they collide with what
    any test file legitimately carries. The check itself runs BEFORE
    the session (see :func:`_replay_one`): after it, a correct
    implementer's own tree converges toward the hidden tests and every
    gate echoes what it ran, so a post-hoc walk could not tell a leak
    from the work."""
    markers: list[str] = []
    for text in hidden.values():
        for line in text.splitlines():
            stripped = line.strip()
            if "assert" in stripped and len(stripped) >= 8 \
                    and stripped not in markers:
                markers.append(stripped)
    return markers[:20]


def _base_absent_markers(origin: Path, markers: list) -> list:
    """A8: only the markers the BASE tree does not already hold.

    Common assert lines live in sibling test files on the base commit
    (JobFinderOS: every PR'd task shares one with some pre-existing
    file) — whatever the base already holds, the real run saw too, so
    it is not a leak. Each marker is grep'd fixed-string against the
    sanitized origin (= the base tree); a grep that itself errors keeps
    the marker (fail closed)."""
    kept: list[str] = []
    for marker in markers:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(origin), "grep", "-q",
             "-I", "-F", "--", marker], capture_output=True, text=True)
        if proc.returncode == 0:
            continue  # the base tree already holds it
        kept.append(marker)
    return kept


def _leak_check(paths, markers: list) -> None:
    """Refuse (named, with the paths) when parent-built state carries
    any hidden-test marker. Runs before any session starts and before
    the hidden tests are copied anywhere near the replay's outputs."""
    if not markers:
        return
    offending: list[str] = []
    for root in paths:
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8",
                                      errors="replace")
            except OSError:  # pragma: no cover - unreadable artefact
                continue
            hits = [m for m in markers if m in text]
            if hits:
                offending.append(f"{path} ({hits[0][:60]}…)")
                if len(offending) >= 5:
                    break
        if len(offending) >= 5:
            break
    if offending:
        raise BenchError(
            "leak: the hidden tests' text reached a run artefact before "
            "scoring — " + "; ".join(offending)
        )


# ---------------------------------------------------------- AC-3 the run --


class _ScorePR:
    """The replay's PR seam: publishing replaced by scoring. Records
    that the factory would have published, and returns an opaque
    bench:// URL no GitHub path will ever accept."""

    def __init__(self, project: str, slot: str):
        self.project = project
        self.slot = slot
        self.calls: list[dict] = []

    def __call__(self, pr) -> str:
        self.calls.append({
            "title": pr.title, "draft": bool(pr.draft),
            "label": getattr(pr, "label", None),
        })
        return f"bench://{self.project}/{self.slot}"


def _bench_task(item: BenchItem) -> dict:
    """The replay's task dict: the item's contract on an opaque slot,
    under the bench's own budget."""
    acceptance = copy.deepcopy(item.contract["acceptance"])
    task = {
        "id": item.slot,
        "title": item.contract["title"],
        "source": item.contract.get("source") or "bench",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": list(item.contract.get("risk") or []),
        "gates": {"spec_approval": True, "merge": "human",
                  "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": item.added,
        "approved_by": "factory bench",
        "budget": {"size": "M", "max_attempts": BENCH_MAX_ATTEMPTS,
                   "max_turns": BENCH_MAX_TURNS,
                   "wall_clock_min": BENCH_WALL_CLOCK_MIN},
        "scope": {"paths": list(item.contract["scope"])},
        "attempts": [],
    }
    if item.contract.get("out_of_scope"):
        task["out_of_scope"] = list(item.contract["out_of_scope"])
    if item.contract.get("dependencies"):
        task["dependencies"] = copy.deepcopy(
            item.contract["dependencies"])
    return task


def _replay_one(item: BenchItem, factory_root: Path, run_root: Path,
                config: FactoryConfig, *, session_runner,
                command_runner, repeat: int) -> dict:
    """One replay of one item: the real attempt path over a sanitized
    clone of the base, then hidden-test scoring on the produced head."""
    from factory import orchestrator
    from factory.state import TaskQueue

    started = time.monotonic()
    repo = Path(item.repo_path).expanduser()
    if not _git_ok(repo, "cat-file", "-e", f"{item.base}^{{commit}}") \
            or not _git_ok(repo, "cat-file", "-e",
                           f"{item.head}^{{commit}}"):
        raise BenchError(
            f"{item.slot}: the recorded repo {item.repo_path} no longer "
            "holds the item's base or head commit (history rewritten?) — "
            "re-add the item"
        )

    origin = run_root / "origin"
    sanitize_origin(repo, origin, item.base, item.default_branch)

    scratch = run_root / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    queue_dir = run_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "tasks.json"
    queue_path.write_text(json.dumps(
        {"version": current_epoch(), "project": item.project,
         "tasks": [_bench_task(item)]}, indent=2) + "\n",
        encoding="utf-8")

    profile = copy.deepcopy(load_profile(
        repo / ".factory" / "project.yaml", config=config))
    # The replay repoints the profile's repo at the sanitized origin:
    # the project's own commands, paths, sentinels and stack run; the
    # remote is the local origin, so a push can never leave the bench.
    profile["repo"] = {**profile["repo"], "path": str(origin),
                       "remote": str(origin),
                       "default_branch": item.default_branch}

    score_pr = _ScorePR(item.project, item.slot)
    # A1: the default seat runner is the REAL sandboxed session runner,
    # exactly run_once's default — the owner's paid baseline runs
    # models. The interpreter probe runs with it, and only with it
    # (tests inject stub seats and no probe), mirroring run_once.
    seat_runner = session_runner or run_session_sandboxed
    if seat_runner is run_session_sandboxed:
        probe_session_interpreter(profile, srt_bin_path=None)
    ctx = orchestrator._Ctx(
        queue=TaskQueue(queue_path), task_id=item.slot,
        profile=profile, factory_root=scratch,
        session_runner=seat_runner, open_pr=score_pr, config=config,
        now=lambda: datetime.now(timezone.utc), srt_bin_path=None,
        gate_timeout_sec=600,
    )
    # AC-2's artefact check, run BEFORE any session starts: everything
    # the parent has built so far (the queue, the scratch, the origin)
    # must carry nothing of the hidden tests. This is the moment the
    # check is meaningful — a post-hoc walk over the replay's outputs
    # could not tell a leak from a CORRECT implementer's own work (its
    # tree converges toward the hidden tests, and every gate echoes
    # what it ran); before the session, any hit is the parent's.
    hidden = item.hidden_files_text(factory_root)
    # A8: only markers the base tree does not already hold count — the
    # origin IS the base tree, so the filter runs against it before any
    # scan.
    _leak_check([scratch, queue_dir, origin],
                _base_absent_markers(origin, _leak_markers(hidden)))

    escalation = None
    outcome_error = None
    try:
        with ctx.queue.begin_run(item.slot):
            result = orchestrator._run_task(ctx, None)
        outcome = result.outcome
        escalation = getattr(result, "escalation", None)
    except Exception as exc:  # noqa: BLE001 — recorded, never silent
        outcome_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome = "error"

    task = ctx.queue.get(item.slot)

    # The replay head: the validated pushed head, else the kept branch
    # an escalation push left on the origin.
    links = task.get("links") or {}
    head = links.get("pushed_head")
    if not head and links.get("branch"):
        head = _origin_branch_tip(origin, links["branch"])
    hidden_pass, hidden_note = _score_hidden(
        origin, head, hidden, item, profile, command_runner, scratch)

    if outcome_error:
        factory_outcome = f"error:{outcome_error}"
    elif outcome == "pr_opened":
        factory_outcome = "would_publish"
    else:
        factory_outcome = f"escalated:{escalation or outcome}"
    attempts = task.get("attempts") or []
    last = attempts[-1] if attempts else {}
    by_role: dict = {}
    for attempt in attempts:
        for role, usage in _trace_roles(
                scratch, item.project, attempt.get("run_id")).items():
            bucket = by_role.setdefault(role, {
                "sessions": 0, "turns": 0, "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0, "models": []})
            for key in ("sessions", "turns", "input_tokens",
                        "output_tokens"):
                bucket[key] += usage.get(key, 0)
            bucket["cost_usd"] += usage.get("cost_usd", 0.0)
            for model in usage.get("models") or []:
                if model not in bucket["models"]:
                    bucket["models"].append(model)
    record = {
        "repeat": repeat,
        "clean": bool(hidden_pass
                      and factory_outcome == "would_publish"),
        "hidden_pass": hidden_pass,
        "hidden_note": hidden_note,
        "factory_outcome": factory_outcome,
        "escalation": escalation,
        "head": head,
        "pr": links.get("pr"),
        "rounds": last.get("rounds", 0),
        "turns": last.get("turns", 0),
        "turns_by_seat": dict(last.get("turns_by_seat") or {}),
        "input_tokens": last.get("input_tokens", 0),
        "output_tokens": last.get("output_tokens", 0),
        "cost_usd": last.get("cost_usd"),
        "attempts": len(attempts),
        "wall_sec": round(time.monotonic() - started, 1),
        "models": (last.get("model") or "").split(),
        "by_role": by_role,
    }
    if score_pr.calls:
        record["pr_calls"] = score_pr.calls
    return record


def _origin_branch_tip(origin: Path, branch: str) -> "str | None":
    if _git_ok(origin, "rev-parse", "--verify", "--quiet",
               f"refs/heads/{branch}"):
        return _git_out(origin, "rev-parse", f"refs/heads/{branch}")
    return None


def _score_hidden(origin: Path, head: "str | None", hidden: dict,
                  item: BenchItem, profile: dict, command_runner,
                  scratch: Path) -> "tuple[bool, str | None]":
    """Copy the hidden tests into a fresh clone of the replay head and
    run their refs — outside every session, after the attempt settled.

    The commands run through the sandboxed gate runner by default (A4):
    the replay head is agent-written code, and ``gates.run_command`` is
    the fixtures-only shape its own docstring disclaims for production.
    The clone is built the merge-time re-validation's way (create_run +
    _gate_settings + sandboxed_runner), inside the replay's scratch so
    it dies with it."""
    if not head:
        return False, "no replay head (nothing was pushed)"
    score = create_run(item.project, origin, factory_root=scratch,
                       ref=head)
    try:
        runner = command_runner or _sandbox_gate_runner(
            profile, score, scratch, item.project)
        for path, text in hidden.items():
            target = score.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        # A6: the hidden tests may need the environment the profile's
        # setup builds. A failure is RECORDED (hidden_pass False, the
        # note naming it) — the score of a replay whose head broke its
        # own setup, never a crash.
        setup_failure = _run_setup(profile, runner, score.root, "score")
        if setup_failure is not None:
            return False, f"setup failed on the replay head: " \
                          f"{setup_failure}"
        template = ((profile.get("commands") or {}).get("test_ref")
                    or DEFAULT_TEST_REF)
        for ref in item.hidden_refs:
            result = runner(f"score:{ref}", _ref_command(template, ref),
                            score.root, _PROOF_TIMEOUT_SEC)
            evidence = not_collected_evidence(result, ref)
            if result.timed_out:
                return False, f"{ref} timed out on the replay head"
            if evidence is not None:
                return False, f"{ref} was not collected ({evidence[1]})"
            if result.exit_code != 0:
                return False, f"{ref} fails on the replay head"
        return True, None
    finally:
        cleanup_run(score)


def _sandbox_gate_runner(profile: dict, run: RunDir, factory_root: Path,
                         project: str):
    """The merge-time re-validation's gate shape (orchestrator's
    ``create_run`` → ``_gate_settings`` → ``sandboxed_runner``), for
    parent-run project commands over the replay's code. ``project`` is
    the validated slug the settings path already carries."""
    _gate_settings(profile, run, factory_root)
    return sandboxed_runner(run.root, run.settings_path,
                            srt_bin_path=None)


def _trace_roles(scratch: Path, project: str,
                 run_id: "str | None") -> dict:
    """Per-role usage off the run's own trace (F-30's session_end
    events: role, model, tokens, cost) — the attempt record aggregates
    the attempt; the trace keeps the seats apart."""
    if not run_id:
        return {}
    path = scratch / "trace" / project / f"{run_id}.jsonl"
    if not path.is_file():
        return {}
    roles: dict = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover - unreadable trace
        return {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "session_end":
            continue
        role = event.get("role") or event.get("seat") or "?"
        bucket = roles.setdefault(role, {
            "sessions": 0, "turns": 0, "input_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0, "models": []})
        bucket["sessions"] += 1
        turns = event.get("turns")
        if isinstance(turns, int) and not isinstance(turns, bool):
            bucket["turns"] += turns
        tokens = event.get("tokens") or {}
        for name in ("input_tokens", "output_tokens"):
            value = tokens.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                bucket[name] += value
        cost = event.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            bucket["cost_usd"] += float(cost)
        model = event.get("model") or event.get("requested_model")
        if model and model not in bucket["models"]:
            bucket["models"].append(model)
    return roles


# --------------------------------------------------------- AC-4 the report --


def bench_run(factory_root: Path | str | None, project: str,
              *, repeats: int = 1, role_overrides: dict | None = None,
              against: "str | Path | None" = None, session_runner=None,
              command_runner=None, config: FactoryConfig | None = None,
              keep: bool = False) -> dict:
    """Run the replay benchmark (AC-4): every item, ``repeats`` times
    each, through the real pipeline with publishing replaced by
    scoring. Writes the report under ``<factory_root>/bench/<project>/
    reports/`` and returns ``{"report", "report_path",
    "rendered_path", "run_root"}``.

    ``role_overrides`` rides the config in memory ONLY (F-100 AC-5):
    never written to any profile, the registry or the item store, named
    in the report. ``against`` diffs this report against a named earlier
    one, regressions first.

    Raises:
        BenchError: no items, a bad ``repeats``, an unverifiable store,
            or a leak the run itself refused to score over.
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int) \
            or repeats < 1:
        raise BenchError(f"repeats must be an integer >= 1, got {repeats!r}")
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    items = load_items(root, project)
    loaded = config if config is not None else load_config(root / CONFIG_FILE)
    if role_overrides:
        loaded = dataclasses.replace(loaded,
                                     role_overrides=dict(role_overrides))
    # The replay's config: the owner's registry and knobs, except the
    # publish-path CI wait — a bench PR has no checks to wait for, and
    # ci_wait_min=0 is the documented pre-F-75 shape (no CI read in the
    # publish path at all). Nothing else about the pipeline changes.
    run_config = dataclasses.replace(loaded, ci_wait_min=0)

    stamp = new_run_id()
    run_root = bench_dir(root) / project / ".runs" / stamp
    run_root.mkdir(parents=True)
    try:
        records: dict[str, list] = {}
        for item in items:
            for repeat in range(1, repeats + 1):
                one_root = run_root if (len(items) == 1 and repeats == 1) \
                    else run_root / f"{item.slot}-r{repeat}"
                one_root.mkdir(parents=True, exist_ok=True)
                # A9: one failing replay never aborts the run — the
                # paid replays already done must survive. The error is
                # recorded (never clean, hidden_pass False, the reason
                # on the outcome) and the run goes on; the report is
                # always written.
                try:
                    record = _replay_one(
                        item, root, one_root, run_config,
                        session_runner=session_runner,
                        command_runner=command_runner, repeat=repeat)
                except Exception as exc:  # noqa: BLE001 — recorded
                    record = {
                        "repeat": repeat, "clean": False,
                        "hidden_pass": False,
                        "hidden_note": f"replay error: {str(exc)[:300]}",
                        "factory_outcome":
                            f"error:{type(exc).__name__}: "
                            f"{str(exc)[:300]}",
                        "head": None, "rounds": 0, "turns": 0,
                        "turns_by_seat": {}, "input_tokens": 0,
                        "output_tokens": 0, "cost_usd": None,
                        "attempts": 0, "wall_sec": 0.0, "models": [],
                        "by_role": {},
                    }
                records.setdefault(item.slot, []).append(record)
        report = {
            "kind": "bench",
            "project": project,
            "stamp": stamp,
            "repeats": repeats,
            "role_overrides": dict(getattr(run_config,
                                           "role_overrides", None) or {}),
            "roles": _report_roles(records),
            "items": [
                {"slot": item.slot,
                 "source_task": item.source_task,
                 "source_pr": item.source_pr,
                 "repeats": records[item.slot],
                 "clean": sum(1 for r in records[item.slot] if r["clean"]),
                 "judged": len(records[item.slot])}
                for item in items
            ],
        }
        report["errors"] = [
            item["slot"] for item in report["items"]
            if any(str(r.get("factory_outcome", "")).startswith("error:")
                   for r in item["repeats"])]
        report["totals"] = _totals(report["items"])
        clean = report["totals"]["clean"]
        cost = report["totals"]["cost_usd"]
        report["yield_clean_per_usd"] = (
            round(clean / cost, 4)
            if cost is not None and cost > 0 else None)
        report["cost_per_clean_usd"] = (
            round(cost / clean, 4)
            if cost is not None and clean else None)
        report["against"] = None
        if against is not None:
            before = _load_against(against)
            report["against"] = {
                "path": str(against),
                **compare_reports(before, report),
            }
        reports_dir = bench_dir(root) / project / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"bench-{stamp}.json"
        report_path.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
        rendered_path = report_path.with_suffix(".md")
        rendered_path.write_text(render_report(report), encoding="utf-8")
        return {"report": report, "report_path": report_path,
                "rendered_path": rendered_path, "run_root": run_root}
    finally:
        if not keep:
            shutil.rmtree(run_root, ignore_errors=True)


def _load_against(against: "str | Path") -> dict:
    path = Path(against)
    try:
        before = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError(f"cannot read the --against report {path}: {exc}") \
            from exc
    if not isinstance(before, dict) or before.get("kind") != "bench":
        raise BenchError(
            f"{path} is not a bench report — --against takes a report "
            "written by an earlier `factory bench run`"
        )
    return before


def compare_reports(before: dict, after: dict) -> dict:
    """The item-level diff two reports are compared by: an item PASSED a
    report when it has at least one clean merge over judged repeats.
    Regressions (passed → failed) come FIRST and every section is
    sorted by slot; an item on only one side never counts as either."""
    def passed(items, slot: str) -> "bool | None":
        for item in items or []:
            if isinstance(item, dict) and item.get("slot") == slot:
                if not item.get("judged"):
                    return None
                return item.get("clean", 0) > 0
        return None

    before_items = before.get("items") or []
    after_items = after.get("items") or []

    def _slots(items) -> set:
        return {str(item.get("slot")) for item in items
                if isinstance(item, dict)}

    slots = sorted(_slots(after_items) | _slots(before_items))
    regressions: list[str] = []
    improvements: list[str] = []
    unchanged: list[str] = []
    for slot in slots:
        was = passed(before_items, slot)
        now = passed(after_items, slot)
        if was is None or now is None:
            continue
        if was and not now:
            regressions.append(str(slot))
        elif now and not was:
            improvements.append(str(slot))
        else:
            unchanged.append(str(slot))
    return {"regressions": regressions, "improvements": improvements,
            "unchanged": unchanged}


def _report_roles(records: dict) -> dict:
    roles: dict = {}
    for repeats in records.values():
        for record in repeats:
            for role, usage in (record.get("by_role") or {}).items():
                for model in usage.get("models") or []:
                    roles.setdefault(role, [])
                    if model not in roles[role]:
                        roles[role].append(model)
    return roles


def _totals(items: list) -> dict:
    clean = sum(item.get("clean", 0) for item in items)
    judged = sum(item.get("judged", 0) for item in items)
    cost = None
    tokens_in = tokens_out = 0
    wall = 0.0
    for item in items:
        for record in item.get("repeats") or []:
            value = record.get("cost_usd")
            if value is not None:
                cost = (cost or 0.0) + float(value)
            tokens_in += record.get("input_tokens", 0) or 0
            tokens_out += record.get("output_tokens", 0) or 0
            wall += record.get("wall_sec", 0.0) or 0.0
    return {"clean": clean, "judged": judged, "cost_usd": cost,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "wall_sec": round(wall, 1)}


def _fmt_cost(value) -> str:
    if value is None:
        return "n/a (no session reported a cost)"
    return f"${value:.2f}"


def render_report(report: dict) -> str:
    """The owner-facing bench report: per item k/N clean merges with
    each repeat's outcome, the yield figure, cost per clean merge, the
    models that ran per role, the overrides this run carried, and the
    ``--against`` diff with regressions first."""
    repeats = int(report.get("repeats") or 1)
    lines = [
        f"Replay benchmark — project: {report['project']} "
        f"({len(report.get('items') or [])} item(s), {repeats} repeat"
        + ("s" if repeats != 1 else "") + ")",
        "",
    ]
    overrides = report.get("role_overrides") or {}
    if overrides:
        lines.append(
            "Role overrides (this run only, never written): "
            + ", ".join(f"{role}=`{model}`"
                        for role, model in sorted(overrides.items())))
        lines.append("")
    for item in report.get("items") or []:
        lines.append(
            f"- {item['slot']}: {item.get('clean', 0)}/{item.get('judged', 0)} "
            f"clean merges (from {item.get('source_task')}, PR #"
            f"{item.get('source_pr')})")
        for record in item.get("repeats") or []:
            note = record.get("hidden_note")
            tail = f" — {note}" if note else ""
            lines.append(
                f"  [r{record.get('repeat')}] hidden tests "
                f"{'pass' if record.get('hidden_pass') else 'FAIL'}; "
                f"{record.get('factory_outcome')}; {record.get('rounds', 0)} "
                f"round(s), {record.get('turns', 0)} implementer turn(s), "
                f"{_fmt_cost(record.get('cost_usd'))}{tail}")
    totals = report.get("totals") or {}
    lines += ["", "## Totals", ""]
    lines.append(
        f"- clean merges: {totals.get('clean', 0)} of "
        f"{totals.get('judged', 0)} judged replays")
    lines.append(
        f"- total cost: {_fmt_cost(totals.get('cost_usd'))} "
        f"({totals.get('input_tokens', 0)} in / "
        f"{totals.get('output_tokens', 0)} out tokens, "
        f"{totals.get('wall_sec', 0)}s wall)")
    yield_value = report.get("yield_clean_per_usd")
    if yield_value is None:
        yield_text = "n/a (no cost reported)"
    else:
        yield_text = f"{yield_value:.4f} clean merges per USD"
    lines.append(f"- yield: {yield_text}")
    per_clean = report.get("cost_per_clean_usd")
    if per_clean is None:
        per_clean_text = "n/a (0 clean merges)"
    else:
        per_clean_text = f"${per_clean:.2f}"
    lines.append(f"- cost per clean merge: {per_clean_text}")
    roles = report.get("roles") or {}
    if roles:
        lines += ["", "## Models that ran, per role", ""]
        for role in sorted(roles):
            lines.append(f"- {role}: {', '.join(sorted(roles[role]))}")
    against = report.get("against")
    if against:
        lines += ["", f"## Against {against.get('path')}", ""]
        regressions = against.get("regressions") or []
        lines.append(
            "Regressions (pass → fail): "
            + (", ".join(regressions) if regressions else "none"))
        improvements = against.get("improvements") or []
        if improvements:
            lines.append(f"Improvements (fail → pass): "
                         f"{', '.join(improvements)}")
        unchanged = against.get("unchanged") or []
        if unchanged:
            lines.append(f"Unchanged: {', '.join(unchanged)}")
        if not regressions:
            lines.append(
                "No item regressed: the swap's evidence is the yield and "
                "cost lines above.")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------- AC-5 doctor --


def latest_bench_report(factory_root: Path | str | None) -> "dict | None":
    """The newest bench report under ``<factory_root>/bench/*/reports/``
    (by the stamp in its file name), or None when no report exists."""
    root = bench_dir(factory_root)
    if not root.is_dir():
        return None
    best: "tuple[str, Path] | None" = None
    for path in sorted(root.glob("*/reports/bench-*.json")):
        match = re.search(r"bench-(\d{8}T\d{6}Z)", path.name)
        if not match:
            continue
        if best is None or match.group(1) > best[0]:
            best = (match.group(1), path)
    if best is None:
        return None
    try:
        report = json.loads(best[1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def bench_swap_note(factory_root: Path | str | None) -> "str | None":
    """AC-5's doctor note: the registry's implementer model differs from
    the one the latest bench report ran. Informational (the swap rule
    is the owner's to follow); names both models and the command."""
    from factory.models import (
        DEFAULT_REGISTRY,
        DEFAULT_STACK_NAME,
        expand_alias,
    )

    report = latest_bench_report(factory_root)
    if report is None:
        return None
    ran = (report.get("roles") or {}).get("implementer") or []
    if not ran:
        return None
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    try:
        config = load_config(root / CONFIG_FILE)
        registry = getattr(config, "models", None) or DEFAULT_REGISTRY
    except Exception:  # noqa: BLE001 — doctor reports, never crashes
        registry = DEFAULT_REGISTRY
    stack = registry.stacks.get(DEFAULT_STACK_NAME)
    if stack is None or "implementer" not in stack.roles:
        return None
    try:
        current = expand_alias(registry,
                               stack.roles["implementer"].model)
    except Exception:  # noqa: BLE001 — a broken registry has its own note
        return None
    if current in ran:
        return None
    return (
        f"the registry's implementer model is {current}, but the latest "
        f"bench report ({report.get('stamp', '?')}, project "
        f"{report.get('project', '?')}) ran {', '.join(ran)} — an "
        "implementer model change is preceded by a bench run with the new "
        "model against the current baseline (F-104): run `factory bench "
        "run <project> --role implementer=<new model> --against <latest "
        "report>` and read the diff before approving the registry edit"
    )
