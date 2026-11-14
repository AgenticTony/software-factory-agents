"""``factory approve`` — the human spec-approval gate (F-04).

Approval is the moment a draft becomes a contract: a human reads the
acceptance items, confirms, and the queue freezes them —
``acceptance_hash``, ``approved_at``, ``approved_by`` (TASK-CONTRACT
§2). Any later edit to the acceptance text changes the hash and F-03
sends the task back to ``draft``; re-approving then overwrites the stale
fields.

The command is deliberately human-paced:

- the contract is printed before anything is asked,
- a risk-flagged task can never be approved with ``--yes`` (AC-3) — the
  point of a risk flag is that a human looks at the task,
- approval routes to ``ready`` or ``blocked`` strictly per the picker's
  dependency rule, so an approved-but-blocked task can never be picked,
- EOF on the prompt (no TTY, stdin closed) counts as a decline, never a
  crash.

``--reconcile-bootstrap`` (AC-4) turns the bootstrap ledger
(``.factory/BOOTSTRAP.md``) into queue state: each entry names a task,
its criterion markers and the status the evidence supports. Every entry
is confirmed interactively — there is no ``--yes`` for reconcile,
because the ledger is plain markdown and this command turns it into
``done`` tasks. On confirmation the entry's final status is computed and
applied through :meth:`TaskQueue.transition_path`: the whole path is
validated as legal F-03 transitions and written in **one** atomic save,
so a refused entry writes nothing (no half-approved task stranded at an
intermediate status, and never a persisted ``in_progress`` without the
run lease). A criterion marked pending — or not met, or never mentioned
— can never produce a status that asserts the build passed validation
(``in_review`` and beyond); the entry is refused and the task stays
``draft``. A recorded ``done`` whose owner steps are still open is
routed to ``awaiting_owner``, which is what the evidence supports.
"""

import getpass
import json
import re
import subprocess
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path

from factory.approvals import (
    PROFILE_SECURITY_GROUPS,
    ApprovalLedger,
    ApprovalLedgerError,
)
from factory.contract import (
    NON_EXECUTABLE_KINDS,
    ContractError,
    acceptance_hash,
    budget_floor,
    load_profile,
)
from factory.egress import escape_approval_text
from factory.gates import (
    GateError,
    contract_test_files,
    default_branch_files,
    scope_risk_flags,
)
from factory.gates import (
    matches as _glob_match,
)
from factory.gates import (
    ref_path as _ref_path,
)
from factory.gates import (
    scope_matches as _scope_match,
)
from factory.mergeorder import gh_pr_state
from factory.progress import ProgressLog, ProgressLogError
from factory.publish import PublishError, PushRefused
from factory.runs import FACTORY_ROOT_DEFAULT
from factory.state import (
    ELIGIBLE_KINDS,
    RunInProgressError,
    TaskQueue,
    attempts_reset_eligible,
    credential_recovery_eligible,
    now_iso,
    open_owner_steps,
    recovery_task_eligible,
    scope_recovery_eligible,
    unmet_dependencies,
)

# Ledger grammar (.factory/BOOTSTRAP.md):
#   ## <task-id> — <title>                     starts an entry
#   - **AC-n** proven|pending|not met: ...     criterion markers
#   **Status to set at reconcile:** `<status>`  the status evidence supports
_STATUS_MARKER = "Status to set at reconcile"
_CRITERION_RE = re.compile(
    r"^\s*-\s+\*\*(AC-\d+)\*\*\s+(proven|pending|not\s+met)\b", re.MULTILINE
)
_RECORDED_STATUS_RE = re.compile(
    r"\*\*Status to set at reconcile:\*\*\s*`([a-z_]+)`"
)

# The forward lifecycle after approval, in order. A reconcile path is
# replayed history validated edge by edge, so every step is a legal F-03
# transition; ``awaiting_owner`` sits off the direct path: done comes
# straight from deploying when the owner steps are closed.
_FORWARD_CHAIN = ("ready", "in_progress", "in_review", "deploying")
_WALK_CHAIN = _FORWARD_CHAIN[1:]  # approval already reached "ready"

# Recorded statuses that assert the build passed validation. Reconciling
# to any of these requires every acceptance criterion to be proven in the
# ledger entry; needs_human is exempt because escalation is exactly where
# unproven criteria honestly land.
_PROVEN_REQUIRED_TARGETS = frozenset(
    {"in_review", "deploying", "awaiting_owner", "done"}
)

# One reconcile outcome per ledger entry: what happened, and why.
ReconcileResult = namedtuple("ReconcileResult", ["task_id", "action", "detail"])


class ApprovalError(ContractError):
    """An approval or reconcile was refused. The message is for humans."""


class ApprovalDeclined(ApprovalError):
    """The human answered no to the confirmation prompt."""


def render_contract(task: dict, *, warnings: list | None = None) -> str:
    """Render the contract a human is about to approve.

    Everything the approval decision depends on, nothing else: identity,
    risk, dependencies, the acceptance items with their checks, owner
    steps and slices. Open owner steps list first — they cap how far the
    task can ever be advanced. F-71 adds the declared dependencies (the
    pre-spend policy they trigger) and, when the caller computed them,
    the protected-ref WARNING block (AC-2: a criterion whose test file
    the scope cannot touch can only go green through a protected-path
    change).

    Args:
        task: The task record.
        warnings: :func:`protected_ref_warnings` results, when the
            caller ran them (single approval and runnable reconcile
            targets); None renders none.

    Returns:
        The contract as plain text, one fact per line.
    """
    budget = task.get("budget") or {}
    risk = task.get("risk") or []
    deps = task.get("depends_on") or []
    dependencies = task.get("dependencies") or []
    dependencies_shown = ", ".join(
        "{}:{}".format(entry.get("ecosystem"), entry.get("name"))
        for entry in dependencies if isinstance(entry, dict)
    ) if dependencies else "(none declared)"

    def show(value) -> str:
        # F-63 R2-1: every interpolated value escapes its control
        # characters — a value that can rewrite the terminal can rewrite
        # what the owner believes they approved.
        return escape_approval_text(str(value))

    lines = [
        f"{show(task.get('id'))} — {show(task.get('title'))}",
        # F-64 AC-3: the whole budget beside the size — max_attempts,
        # max_turns and wall_clock_min are what the run will spend, so
        # the approval decision sees all three (through the same
        # escaping as every other interpolated value).
        "  kind: {}   priority: {}   budget: {} (max {} attempts, "
        "{} turns, {} min wall clock)".format(
            show(task.get("kind")),
            show(task.get("priority")),
            show(budget.get("size", "?")),
            show(budget.get("max_attempts", "?")),
            show(budget.get("max_turns", "?")),
            show(budget.get("wall_clock_min", "?")),
        ),
        f"  source: {show(task.get('source'))}",
        f"  risk: {show(', '.join(risk) if risk else '(none)')}",
        f"  depends_on: {show(', '.join(deps) if deps else '(none)')}",
        # F-71 AC-4: the declared dependencies are the pre-spend policy
        # — a direct manifest addition must be listed here, and listing
        # any refuses --yes (a human looks at what gets installed).
        f"  dependencies (F-71; --yes refused while set): "
        f"{show(dependencies_shown)}",
        # F-65 AC-1: the file scope is half of what the owner is
        # approving — L0 fails anything outside these globs, and widening
        # them later is a new contract needing re-approval.
        f"  scope (files this contract may change): "
        f"{show(', '.join(_scope_paths(task)) if _scope_paths(task) else '(none declared)')}",
        "  acceptance (frozen on approval):",
    ]
    for item in task.get("acceptance") or []:
        check = item.get("check", {})
        lines.append(f"    {show(item.get('id'))}: {show(item.get('text'))}")
        if check.get("ref"):
            lines.append(
                f"      check: {show(check.get('type'))} {show(check.get('ref'))}"
            )
    # F-71 AC-2: the protected-ref warnings render INSIDE the contract,
    # right where the criteria they name sit — the owner answers the
    # same y/N prompt with them in view.
    for criterion, file in warnings or []:
        lines.append(
            f"  WARNING — {show(criterion)}: its test file {show(file)} is "
            "a protected path this contract's scope cannot touch: the "
            "criterion can only go green through a protected-path change "
            "(L0 fails it). Confirm with the risk in view."
        )
    for step in task.get("owner_steps") or []:
        mark = "x" if step.get("done") else " "
        lines.append(f"  owner step [{mark}]: {show(step.get('text'))}")
    for slice_ in task.get("slices") or []:
        lines.append(
            f"  slice {show(slice_.get('id'))} [{show(slice_.get('status'))}]: "
            f"{show(slice_.get('text'))}"
        )
    return "\n".join(lines)


def default_approver() -> str:
    """Best-effort identity for ``approved_by``.

    Returns:
        ``git config user.name`` in the current repository, else the OS
        user name. Overridable with ``--approved-by``.
    """
    try:
        result = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, check=True
        )
        name = result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        name = ""
    return name or getpass.getuser()


def input_confirm(question: str) -> bool:
    """Ask a yes/no question on the terminal; anything but y/yes declines.

    EOF (no TTY, stdin closed or piped) also declines: approval without
    an interactive human is a refusal, not a crash.
    """
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def check_v0_gates(task: dict) -> None:
    """Refuse a contract whose merge/deploy gate is not ``human`` (F-67
    AC-1).

    TASK-CONTRACT §gates: in v0 everything is ``human``. The importer
    normalises what it writes, but a hand-edited queue — or one written
    before F-67 — can still carry an auto gate, and approving it would
    let a run merge or deploy on its own. ``spec_approval`` is NEVER
    judged here: an owner-only task's ``spec_approval: false`` is the
    landed queues' own shape (the pilot's carries it on 24 of 32 tasks).

    Raises:
        ApprovalError: ``gates.merge`` or ``gates.deploy`` is not the
            string ``"human"`` — naming the task and the field.
    """
    gates = task.get("gates")
    values = gates if isinstance(gates, dict) else {}
    for field in ("merge", "deploy"):
        if values.get(field) != "human":
            raise ApprovalError(
                f"{task.get('id')}: refusing to approve a contract whose "
                f"gates.{field} is {values.get(field)!r} — in v0 every "
                "gate is human (the owner merges and deploys; F-67); fix "
                "the queue before approving"
            )


def _ref_runnable_error(ref: str) -> str | None:
    """Why a ``test`` check ref is not runnable as written (F-71 AC-2),
    or None. The exact form is ``<repo-relative path>::<name>``, one or
    more non-empty, unpadded ``::`` name segments — the same split L1
    uses (:func:`factory.gates.ref_path`)."""
    if "::" not in ref:
        return f"ref {ref!r} is not <path>::<name> — the runner cannot collect it"
    path = _ref_path(ref)
    if path is None or not path.strip():
        return f"ref {ref!r} names no test-file path"
    if path != path.strip():
        return f"ref {ref!r} pads the file path"
    if path.startswith("/"):
        return f"ref {ref!r} is absolute — refs are repo-relative"
    if ".." in path.split("/"):
        return f"ref {ref!r} climbs out of the repo (..)"
    names = ref.split("::")[1:]
    if not names or any(not name.strip() for name in names):
        return f"ref {ref!r} names an empty test name"
    if any(name != name.strip() for name in names):
        return f"ref {ref!r} pads a test name with whitespace"
    return None


def _check_refs_runnable(task: dict) -> None:
    """Refuse a contract whose checks cannot run as written (F-71 AC-2).

    A prose ref or an empty command is a paid run discovering a broken
    contract mid-spend — the exact waste this work order exists to
    prevent. Applied wherever an approval makes the task runnable
    (the ``require_scope`` boundary: finished work's refs were proven
    by the run that validated it).
    """
    task_id = task.get("id")
    for item in task.get("acceptance") or []:
        check = item.get("check") if isinstance(item, dict) else None
        if not isinstance(check, dict):
            continue
        criterion = item.get("id", "?")
        kind = check.get("type")
        if kind == "test":
            ref = check.get("ref")
            if not isinstance(ref, str):
                continue  # the schema's own refusal names the shape
            reason = _ref_runnable_error(ref)
            if reason is not None:
                raise ApprovalError(
                    f"{task_id}: {criterion}'s check is not runnable — "
                    f"{reason}… name the exact test "
                    "(tests/test_<task>.py::test_ac_N for a new file)"
                )
        elif kind == "command":
            command = check.get("ref")
            if isinstance(command, str) and not command.strip():
                raise ApprovalError(
                    f"{task_id}: {criterion}'s command check is empty — "
                    "a paid run cannot prove it"
                )


def _approval_profile(queue: TaskQueue) -> dict:
    """The profile an approval judges against (F-71): loaded from the
    same default as ``approve --profile`` — next to the queue.

    Fail closed twice: a runnable kind's approval without a loadable
    profile is refused, the path named — and a profile naming another
    project than the queue is refused too (the F-62 pair rule: judging
    one project's contract against another project's risk globs is how
    a paid run gets wasted). The profile carries the risk globs and the
    budget floor the pre-spend checks run on.
    """
    path = Path(queue.path).parent / "project.yaml"
    try:
        profile = load_profile(path)
    except ContractError as exc:
        raise ApprovalError(
            f"cannot approve a runnable contract without a loadable "
            f"profile next to the queue — {path}: {exc}"
        ) from exc
    queue_project = (queue.doc or {}).get("project")
    if profile.get("name") != queue_project:
        raise ApprovalError(
            f"cannot approve a runnable contract against a mismatched "
            f"profile: the queue names project {queue_project!r} but "
            f"{path} names {profile.get('name')!r} (F-62 pair rule)"
        )
    return profile


def _tracked_files(repo: Path, default_branch: str) -> list[str]:
    """The tracked files of the branch the RUN builds on (F-71 AC-1,
    R1-3): the remote default after a fetch when the repo has an origin
    (:func:`factory.gates.default_branch_files`), else the local
    default — never the working tree (an untracked file is not a place
    the contract reaches; a deleted-at-base file is not either)."""
    try:
        return default_branch_files(repo, default_branch)
    except GateError as exc:
        raise ApprovalError(
            f"cannot compute the scope's risk flags: {exc}"
        ) from exc


def protected_ref_warnings(task: dict, profile: dict) -> list:
    """The protected-ref warnings (F-71 AC-2): ``(criterion, file)``
    for every test ref whose file sits under the profile's protected
    paths while the declared scope cannot touch that file.

    Not a refusal: the criterion can still go green through a
    protected-path change the owner knowingly allows (the wo19
    calibration-pin case). The warning block renders in the contract
    and ``--yes`` is refused for a task carrying one — a human answers
    the prompt with the risk in view.
    """
    scope_paths = _scope_paths(task)
    protected = (profile.get("paths") or {}).get("protected") or []
    warnings = []
    for item in task.get("acceptance") or []:
        check = item.get("check") if isinstance(item, dict) else None
        if not (isinstance(check, dict) and check.get("type") == "test"):
            continue
        ref = check.get("ref")
        if not isinstance(ref, str):
            continue
        file = _ref_path(ref)
        if not file:
            continue
        if _glob_match(file, protected) and not _scope_match(file, scope_paths):
            warnings.append((item.get("id", "?"), file))
    return warnings


def _prespend_checks(task: dict, queue: TaskQueue) -> list:
    """The F-71 pre-spend checks of a contract a session WILL run
    (AC-1 risk coverage, AC-3 budget floor; AC-2's ref refusals ride
    :func:`_check_approvable`). Returns the protected-ref warnings for
    the contract render.

    Fail closed on every doubt: a missing profile, an unreadable
    default branch, an uncovered risk flag, a starved budget — each
    refuses, because each is exactly the shape of a paid run wasted
    before any validation layer runs (the pilot's two failed runs).
    """
    task_id = task.get("id")
    if task.get("kind") not in ELIGIBLE_KINDS:
        return []
    profile = _approval_profile(queue)
    # AC-1: the risk flags the declared scope can reach must be
    # declared — computed on the branch the run builds on, plus L0's
    # implicit allowances (the task's own contract test files, the
    # profile's generated globs) and the literal scope paths, through
    # the same matcher L0 uses (gates.scope_risk_flags).
    repo = Path(profile.get("repo", {}).get("path", ".")).expanduser()
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    tracked = _tracked_files(
        repo, profile.get("repo", {}).get("default_branch", "main"))
    computed = scope_risk_flags(
        _scope_paths(task), risk_globs, tracked,
        generated_globs=(profile.get("paths") or {}).get("generated") or (),
        extra_files=contract_test_files(task),
    )
    declared = set(task.get("risk") or [])
    missing = {flag: paths for flag, paths in computed.items()
               if flag not in declared}
    if missing:
        detail = "; ".join(
            f"{flag} (reached via {', '.join(paths[:3])}"
            + (f", +{len(paths) - 3} more" if len(paths) > 3 else "") + ")"
            for flag, paths in sorted(missing.items())
        )
        raise ApprovalError(
            f"{task_id}: the declared scope reaches risk paths the "
            f"contract's risk flags do not declare — {detail}. Declare "
            "them (the run escalates risk_path_touched on the diff "
            "otherwise); F-71"
        )
    # AC-3: a runnable task below the profile's budget floor starves
    # before any validation layer runs (the pilot's run 1 died twice
    # inside 30 turns).
    floor = budget_floor(profile)
    budget = task.get("budget") or {}
    below = {
        field: (budget.get(field), floor[field])
        for field in ("max_turns", "wall_clock_min")
        if isinstance(budget.get(field), int) and budget[field] < floor[field]
    }
    if below:
        detail = ", ".join(
            f"{field} {value} is below the floor {flr}"
            for field, (value, flr) in sorted(below.items())
        )
        raise ApprovalError(
            f"{task_id}: refusing to approve a budget that starves a run — "
            f"{detail} (the profile's budget floor; F-71)"
        )
    return protected_ref_warnings(task, profile)


def _check_approvable(task: dict, *, require_scope: bool = True) -> None:
    """Refusals that apply to any approval, single or reconcile.

    Args:
        task: The task record.
        require_scope: Demand the F-65 declared file scope (and, with
            it, F-71's runnable-check refusals). True for every
            approval of future work (``factory approve``, a reconcile
            to a runnable status). False only when a reconcile records
            work that was ALREADY built and validated (``in_review``
            and beyond): no session will run it, and a finished task
            that is ever re-opened still meets the run-time
            ``scope_missing`` refusal before a session starts.

    Raises:
        ApprovalError: When the task is not a draft, is of a
            non-executable kind, has no acceptance items (AC-1), carries
            a non-human merge/deploy gate (:func:`check_v0_gates`,
            F-67), — F-65 — is of a kind the picker runs but declares
            no file scope, or — F-71 AC-2 — names a check a runner
            cannot run (on the runnable boundary).
    """
    task_id = task.get("id")
    kind = task.get("kind")
    if kind in NON_EXECUTABLE_KINDS:
        raise ApprovalError(
            f"{task_id}: kind={kind} is never executed, so approving it has "
            "no meaning — it is surfaced to you, not run"
        )
    status = task.get("status")
    if status != "draft":
        raise ApprovalError(
            f"{task_id}: only draft contracts can be approved; it is {status}"
        )
    if not task.get("acceptance"):
        raise ApprovalError(
            f"{task_id}: refusing to approve a contract with no acceptance "
            "items — there is nothing to hold the work to"
        )
    # F-67 AC-1: v0 gates are human wherever the queue came from — a
    # hand-edited or pre-F-67 queue cannot carry an auto gate into a run.
    check_v0_gates(task)
    # F-65 AC-1: a task a session will build must declare the files it
    # may change — approving a scope-less build/docs contract would
    # approve an unbounded write surface. Non-executable kinds run no
    # session and are exempt.
    if require_scope and kind in ELIGIBLE_KINDS and not _scope_paths(task):
        raise ApprovalError(
            f"{task_id}: refusing to approve a {kind} contract with no "
            "declared file scope — add scope.paths (the repo-relative "
            "globs this task may change); the run refuses scope-less "
            "tasks (F-65)"
        )
    # F-71 AC-2 rides the same runnable boundary as the scope: a ref a
    # runner cannot run is a paid run discovering the break mid-spend.
    if require_scope:
        _check_refs_runnable(task)


def _scope_paths(task: dict) -> list:
    """The task's declared scope globs (empty when none declared)."""
    scope = task.get("scope")
    paths = scope.get("paths") if isinstance(scope, dict) else None
    return list(paths) if isinstance(paths, list) and paths else []


def _freeze_contract(task: dict, approved_by: str) -> None:
    """Stamp the three contract-freezing fields (TASK-CONTRACT §2)."""
    task["acceptance_hash"] = acceptance_hash(task["acceptance"])
    task["approved_at"] = now_iso()
    task["approved_by"] = approved_by


def approve_task(
    queue: TaskQueue,
    task_id: str,
    *,
    approved_by: str,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] = input_confirm,
    echo: Callable[[str], None] = print,
    factory_root: Path | None = None,
) -> str:
    """Print a draft task's contract, confirm, freeze it, and route it.

    Refused — :class:`ApprovalError`, nothing written — when the task is
    not a draft, is of a non-executable kind, has no acceptance items
    (AC-1), or is risk-flagged while ``assume_yes`` is set (AC-3). On
    confirmation the contract is frozen (``acceptance_hash``,
    ``approved_at``, ``approved_by``), the approval is recorded in the
    owner ledger (F-63 AC-1: the task's whole-contract hash, before the
    status moves — a ledger entry for a task that never left draft is
    harmless history, while an approved task with no entry would be
    demoted straight back), and the task moves ``draft -> ready`` or
    ``draft -> blocked`` per the picker's dependency rule (AC-2) in a
    single validated save.

    Args:
        queue: The queue to write through (its only writer path).
        task_id: Task to approve.
        approved_by: Name recorded as the approver.
        assume_yes: Skip the confirmation prompt; never allowed for
            risk-flagged tasks.
        confirm: Interactive confirmation callable; injected in tests.
        echo: Output callable for the contract; injected in tests.
        factory_root: Factory root for the approval ledger (default
            ``~/factory``).

    Returns:
        The new status: ``ready`` or ``blocked``.

    Raises:
        ApprovalError: On any refusal, or an unknown task — or when the
            ledger append failed, in which case NOTHING was written to
            the queue (approve loudly refuses to produce an approved task
            the ledger doesn't cover).
        ApprovalDeclined: When the human answers no (or the prompt hits
            EOF).
        ContractError: If the resulting queue wouldn't validate.
    """
    task = queue.get(task_id)  # StateError (a ContractError) if unknown
    _check_approvable(task)
    # F-71: the pre-spend checks (risk coverage over the default branch,
    # the budget floor) and the protected-ref warnings, on the same
    # runnable boundary the scope rides — a contract a session WILL run
    # is checked before the session is spent on it.
    warnings = _prespend_checks(task, queue)
    risk = task.get("risk") or []
    if assume_yes and risk:
        raise ApprovalError(
            f"{task_id}: risk flags ({', '.join(risk)}) require interactive "
            "approval; --yes is not accepted for risk-flagged tasks"
        )
    # F-71 AC-2: a protected-ref warning is a refusal of --yes, not of
    # the approval — the owner can still answer y at the prompt.
    if assume_yes and warnings:
        detail = ", ".join(
            f"{criterion} ({file})" for criterion, file in warnings)
        raise ApprovalError(
            f"{task_id}: {detail} — a test file the scope cannot touch is "
            "protected, so its criterion can only go green through a "
            "protected-path change; --yes is not accepted while a "
            "contract carries that warning (F-71)"
        )
    # F-71 AC-4: a contract that declares dependencies installs them;
    # a human looks at what gets installed.
    if assume_yes and task.get("dependencies"):
        shown = ", ".join(
            "{}:{}".format(entry.get("ecosystem"), entry.get("name"))
            for entry in task["dependencies"] if isinstance(entry, dict)
        )
        raise ApprovalError(
            f"{task_id}: the contract declares dependencies ({shown}) "
            "— --yes is not accepted for contracts that install "
            "packages (F-71)"
        )
    echo(render_contract(task, warnings=warnings))
    if not assume_yes and not confirm(f"Approve {task_id}?"):
        raise ApprovalDeclined(f"{task_id}: approval declined")
    _freeze_contract(task, approved_by)
    ledger = ApprovalLedger.for_project(
        factory_root or FACTORY_ROOT_DEFAULT,
        queue.doc.get("project") or "unknown",
    )
    try:
        ledger.record_task_for(task, approved_by=approved_by,
                               approved_at=task["approved_at"])
    except ApprovalLedgerError as exc:
        raise ApprovalError(
            f"{task_id}: the approval ledger refused the entry and nothing "
            f"was written to the queue — {exc}"
        ) from exc
    new_status = (
        "blocked" if unmet_dependencies(task, queue.doc.get("tasks", [])) else "ready"
    )
    queue.transition(task_id, new_status)
    return new_status


def recover_task(
    queue: TaskQueue,
    task_id: str,
    *,
    assume_yes: bool = False,
    evidence: str | None = None,
    reset_attempts: bool = False,
    accept: bool = False,
    reject: bool = False,
    revalidate: bool = False,
    head: str | None = None,
    done_by: str | None = None,
    factory_root: Path | None = None,
    pr_state: Callable[[str], str] | None = None,
    pr_head: "Callable[[str], str] | None" = None,
    pr_ready: "Callable[[str], None] | None" = None,
    pr_close: "Callable[[str], None] | None" = None,
    session_runner: Callable | None = None,
    srt_bin_path: Path | None = None,
    gate_timeout_sec: int = 600,
    confirm: Callable[[str], bool] = input_confirm,
    echo: Callable[[str], None] = print,
) -> str:
    """Queue a fix round for a parked escalation (F-26 round-5 finding 2;
    credential_added re-queue per TASK-CONTRACT §credential_added;
    attempts_exhausted budget reset per the F-31 fix review; and — F-72
    AC-4 — the one-command answer to an escalation draft PR:
    ``--accept --evidence`` re-validates the PR's exact head with one
    escalation class waived and, on pass, marks it ready and moves the
    task ``needs_human → in_review``; ``--reject`` closes the PR and
    returns the task to ``draft`` with its approval markers cleared.

    ``factory recover`` is the owner's explicit fix-round word — the one
    recovery intent GitHub cannot express for a ``pr_closed`` task: its
    PR was already closed at escalation, so no later GitHub state
    distinguishes "re-open it" from "reject it". The sweep reads what
    GitHub can say (a re-opened or merged PR goes back to ``in_review``;
    a closed ``stale_base`` PR re-queues ``ready`` on its own); this
    command writes the gated ``needs_human → ready`` edge directly.

    A task parked on ``credential_added`` (nothing was pushed, so GitHub
    has no signal at all) takes the same edge through the owner's
    confirmation instead: required ``evidence`` saying what was checked
    (e.g. "test fixture, not a real credential" or "rotated"), an
    interactive y/N with NO ``--yes`` — the point is that a human looked
    — and no attempt reset: the remaining budget is spent with the
    finding in hand. The confirmation is PERSISTED: the task's progress
    log gains an entry (outcome ``recovered``, the escalation, the
    evidence, ``done_by``, a timestamp) written after the state move —
    the audit trail, not just the terminal echo (review round on #74).

    A task parked on ``attempts_exhausted`` (F-31: three attempts died
    to a sandbox bug the code could never fix) exits the same
    human-paced way, plus ``--reset-attempts``: the budget is spent, so
    a plain re-queue would park the task straight back on the pre-run
    guard. The reset deletes nothing — every attempt record stays — it
    appends an ``attempt_resets`` marker and the budget counts only
    attempts after it (:func:`factory.state.attempt_budget_used`).

    Refused — :class:`ApprovalError`, nothing written — when the task
    isn't parked on a recoverable escalation (the same gates the
    transitions enforce), or when GitHub does not report the PR
    **closed** (round-6 finding 3): an open PR holds the concurrency
    slot, so queueing a fix round under it would park the task in an
    endless named ``concurrency_wait``. A dependency that regressed
    while the task was parked routes the move to ``blocked`` instead
    (round-6 finding 4), exactly as approval would.

    Args:
        queue: The queue to write through (its only writer path).
        task_id: Task to recover.
        assume_yes: Skip the confirmation prompt (never allowed for a
            credential_added, attempts_exhausted, scope or accept/reject
            recovery).
        evidence: Required for a credential_added, attempts_exhausted or
            scope task, and for ``--accept``: what the owner checked;
            persisted in the task's progress log and the accept waiver.
        reset_attempts: Required for an attempts_exhausted task: the
            explicit budget grant. Without it the command refuses and
            says why — the flag is the owner's acknowledgment that new
            attempts are being spent.
        accept: The F-72 one-command answer to an escalation draft PR
            (new_dependency/risk_path_touched only).
        reject: The F-72 rejection: close the PR, task → draft.
        done_by: Who confirmed a recovery (default: the git user, like
            ``factory tick``); recorded in the log entry and the reset
            marker.
        factory_root: Factory root for the progress log (default
            ``~/factory``).
        pr_state: GitHub PR-state seam (default
            :func:`factory.mergeorder.gh_pr_state`); injected in tests.
        pr_head: PR head-SHA seam (default
            :func:`factory.publish.gh_pr_head_sha`); the accept path only.
        pr_ready: PR ready seam (default :func:`factory.publish.
            gh_pr_ready`); the accept path only.
        pr_close: PR close seam (default :func:`factory.publish.
            gh_pr_close`); the reject path only.
        session_runner: Seat runner for the accept round's review fan-out
            (default :func:`factory.session.run_session_sandboxed`);
            injected in tests.
        confirm: Interactive confirmation callable; injected in tests.
        echo: Output callable for the summary; injected in tests.

    Returns:
        The new status (``ready``, ``blocked``, ``in_review`` or
        ``draft``).

    Raises:
        ApprovalError: On any refusal, or an unknown task.
        ApprovalDeclined: When the human answers no (or the prompt hits
            EOF).
        ContractError: If the resulting queue wouldn't validate.
    """
    task = queue.get(task_id)  # StateError (a ContractError) if unknown
    if head is not None and not revalidate:
        raise ApprovalError(
            f"{task_id}: --head names the head an in_review --revalidate "
            "vouch is for — it combines with nothing else"
        )
    if revalidate and (accept or reject or reset_attempts
                       or evidence is not None):
        raise ApprovalError(
            f"{task_id}: --revalidate answers a stale_base park on its "
            "own — combine it with none of --accept, --reject, "
            "--reset-attempts or --evidence"
        )
    if revalidate:
        return _recover_revalidate_task(
            queue, task_id, task, done_by=done_by, head=head,
            factory_root=factory_root, pr_state=pr_state,
            session_runner=session_runner, srt_bin_path=srt_bin_path,
            gate_timeout_sec=gate_timeout_sec, echo=echo,
        )
    if accept or reject:
        return _recover_escalation_pr(
            queue, task_id, task, accept=accept, reject=reject,
            evidence=evidence, assume_yes=assume_yes, done_by=done_by,
            factory_root=factory_root, pr_head=pr_head, pr_ready=pr_ready,
            pr_close=pr_close, session_runner=session_runner,
            confirm=confirm, echo=echo,
        )
    if credential_recovery_eligible(task):
        return _recover_credential_task(
            queue, task_id, task, evidence=evidence, assume_yes=assume_yes,
            done_by=done_by, factory_root=factory_root,
            confirm=confirm, echo=echo,
        )
    if attempts_reset_eligible(task):
        return _recover_attempts_task(
            queue, task_id, task, evidence=evidence, reset_attempts=reset_attempts,
            assume_yes=assume_yes, done_by=done_by, factory_root=factory_root,
            confirm=confirm, echo=echo,
        )
    if scope_recovery_eligible(task):
        return _recover_scope_task(
            queue, task_id, task, evidence=evidence, assume_yes=assume_yes,
            done_by=done_by, factory_root=factory_root,
            confirm=confirm, echo=echo,
        )
    if not recovery_task_eligible(task):
        raise ApprovalError(
            f"{task_id}: not recoverable — only a stale_base/pr_closed "
            "escalation holding a PR link, a credential_added escalation "
            "with --evidence, or an attempts_exhausted escalation with "
            "--evidence and --reset-attempts, can take the fix-round edge. "
            "Re-open the PR on GitHub (the sweep returns it to in_review), "
            "confirm the credential finding, grant a fresh attempt budget, "
            "or change the contract and re-approve."
        )
    url = task["links"]["pr"]
    state: str | None
    try:
        state = (pr_state or gh_pr_state)(url)
    except PublishError:
        state = None  # unreachable: fail closed like the concurrency limit
    if state != "closed":
        shown = state if state is not None else "unreachable (gh failed)"
        raise ApprovalError(
            f"{task_id}: recover queues a fix round, but GitHub reports "
            f"the PR {shown} — close the PR first (an open PR holds the "
            "concurrency slot and the fix round would wait on it forever; "
            "for a stale_base task, just closing it lets the next run's "
            "sweep queue the fix round on its own)."
        )
    attempts = task.get("attempts") or []
    echo(
        f"{task_id}: {task.get('title')}\n"
        f"  escalation: {attempts[-1].get('escalation')}\n"
        f"  PR: {url} (closed)\n"
        "  recover queues a fix round: the next run re-implements from "
        "the current default head on a fresh branch (work pushed to the "
        "old PR branch is not reused)."
    )
    if not assume_yes and not confirm(f"Queue fix round for {task_id}?"):
        raise ApprovalDeclined(f"{task_id}: recovery declined")
    new_status = (
        "blocked" if unmet_dependencies(task, queue.doc.get("tasks", []))
        else "ready"
    )
    queue.transition(task_id, new_status)  # the gated edge (state enforces)
    return new_status


def _recover_revalidate_task(
    queue: TaskQueue,
    task_id: str,
    task: dict,
    *,
    done_by: str | None,
    head: str | None = None,
    factory_root: Path | None = None,
    pr_state: Callable[[str], str] | None,
    session_runner: Callable | None,
    srt_bin_path: Path | None,
    gate_timeout_sec: int,
    echo: Callable[[str], None],
) -> str:
    """F-76 AC-2: the owner's re-validate word for a parked stale_base PR.

    Eligible only for a ``needs_human`` task whose last attempt
    escalated ``stale_base`` and which holds a PR link GitHub reports
    **open** — a closed PR was the rejection (the sweep's close edge
    owns it), a merged one the sweep's merged writer, any other
    escalation keeps its own exit, and a gh that cannot answer fails
    closed. Refusals write NOTHING. On the owner's word the command
    re-validates the PR's CURRENT remote head against the current
    remote default head through the sweep's OWN code path (the shared
    :func:`factory.orchestrator.revalidate_stale_pr` — the one re-run
    included, never a copy), under the run lease like every queue
    writer. A pass needs no confirmation prompt: the passing
    re-validation IS the evidence — the task moves
    ``needs_human → in_review`` through the dedicated
    :meth:`TaskQueue.recover_revalidated` writer (the generic
    transition refuses a stale_base task), stamping exactly the pair
    that passed. A fail keeps the task parked with a NEW stored report
    and refuses naming the failing step. No model session runs and no
    attempt is spent: the re-validation is session-less L0/L1, and
    ``session_runner`` is accepted only so a caller can prove it is
    never called.
    """
    from factory.approvals import require_profile_approval
    from factory.contract import contract_hash, load_profile
    from factory.orchestrator import revalidate_stale_pr
    from factory.publish import PublishError as _PublishError

    factory_root = factory_root or FACTORY_ROOT_DEFAULT
    attempts = task.get("attempts") or []
    escalation = attempts[-1].get("escalation") if attempts else None
    if task.get("status") == "in_review":
        # R2-1 + R3-1: the in_review VOUCH — the way back every guard
        # refusal line names. No gates run (the owner's word on a head
        # they NAME is the vouch; the fix round that may build on the
        # vouched head runs the full gate stack itself) — which is
        # exactly why the head must be named: the vouch covers the head
        # the owner judged, never whatever the remote happens to hold
        # when the command runs.
        return _vouch_revalidate_head(
            queue, task_id, task, done_by=done_by, head=head,
            factory_root=factory_root, pr_state=pr_state, echo=echo)
    if head is not None:
        raise ApprovalError(
            f"{task_id}: --head belongs to the in_review head vouch — "
            "the stale_base re-validation judges the head it reads "
            "itself and names no SHA"
        )
    if (task.get("status") != "needs_human" or escalation != "stale_base"
            or not (task.get("links") or {}).get("pr")):
        raise ApprovalError(
            f"{task_id}: --revalidate answers a stale_base park — or, for "
            "an in_review PR whose head the sweep reported moved or "
            "unrecorded, vouches for the current head. Everything else "
            "keeps its own exit; a pr_closed task exits through the "
            "sweep's re-open or merge signal."
        )
    url = task["links"]["pr"]
    try:
        state = (pr_state or gh_pr_state)(url)
    except _PublishError:
        state = None  # gh failed: fail closed, never guessed
    if state != "open":
        shown = state if state is not None else "unreachable (gh failed)"
        raise ApprovalError(
            f"{task_id}: --revalidate needs GitHub to report the PR open — "
            f"it reports {shown}. A closed stale_base PR was your "
            "rejection (the sweep queues the fix round on its own); a "
            "merged one is the sweep's merged recovery; nothing is "
            "guessed when gh cannot answer."
        )
    # F-63: the re-validation runs the project's gates, which is acting
    # on the task — its current contract needs an owner approval, and
    # the profile's security-relevant content an approval on record
    # (the recover_accept_round precedent).
    ledger = ApprovalLedger.for_project(
        factory_root, queue.doc.get("project") or "unknown")
    chash = contract_hash(task)
    if not ledger.has_contract(task_id, chash):
        raise ApprovalError(
            f"{task_id}: the current contract (hash {chash[:12]}) has no "
            "owner approval in the ledger — a re-validation runs gates, "
            "which is acting on the task; re-approve (`factory adopt`) "
            "first"
        )
    profile_path = Path(queue.path).parent / "project.yaml"
    profile = load_profile(profile_path)
    require_profile_approval(profile, factory_root, origin=str(profile_path))
    who = done_by or default_approver()
    echo(
        f"{task_id}: {task.get('title')}\n"
        f"  escalation: {escalation}\n"
        f"  PR: {url} (open)\n"
        "  --revalidate merges the PR's current remote head onto the "
        "current remote default head and re-runs L0 and L1 (one re-run "
        "on a fresh clone if the first pass fails a command); a pass "
        "moves the task back to in_review with the pair stamped — no "
        "prompt: the passing re-validation is the evidence."
    )
    try:
        with queue.idle_lease():
            rv = revalidate_stale_pr(
                queue, task_id, profile, factory_root=factory_root,
                srt_bin_path=srt_bin_path,
                gate_timeout_sec=gate_timeout_sec, ledger=ledger)
            if rv.note is not None:
                raise ApprovalError(
                    f"{task_id}: --revalidate failed — {rv.note}; "
                    f"the report is stored at {rv.report_path} and the "
                    "task stays needs_human (its PR stays open; close it "
                    "to queue a fix round, or fix the branch and "
                    "revalidate again)"
                )
            queue.recover_revalidated(
                task_id, pr_sha=rv.pair[0], default_sha=rv.pair[1])
    except RunInProgressError as exc:
        raise ApprovalError(
            f"{task_id}: the run lease is held ({exc}) — another run or "
            "housekeeping pass owns the queue right now; retry once it "
            "finishes. Nothing was written."
        ) from exc
    try:
        ProgressLog.for_task(
            factory_root, queue.doc.get("project") or "unknown", task_id,
        ).append({
            "outcome": "recovered",
            "escalation": escalation,
            "done_by": who,
            "recovered_at": now_iso(),
            "note": (
                f"stale_base re-validated by the owner's word: the PR's "
                f"current head {rv.pair[0][:12]} merged onto the default "
                f"head {rv.pair[1][:12]} passed L0+L1 "
                f"({rv.report_path.name}); {task_id} → in_review"
                + (" — failed once, passed on re-run (first run: "
                   + "; ".join(rv.flaked) + ")" if rv.flaked else "")
            ),
        })
    except ProgressLogError as exc:
        raise ApprovalError(
            f"{task_id}: moved to in_review, but the progress log could "
            f"not be written — the recovery is NOT durably recorded: {exc}"
        ) from exc
    return "in_review"


def _vouch_revalidate_head(
    queue: TaskQueue,
    task_id: str,
    task: dict,
    *,
    done_by: str | None,
    head: str | None,
    factory_root: Path | None,
    pr_state: Callable[[str], str] | None,
    echo: Callable[[str], None],
) -> str:
    """R2-1: the owner's head vouch for an in_review PR (F-76).

    The sweep's CI fix round builds only on a head the factory pushed
    (``links.pushed_head``) or the owner vouched for; its refusal lines
    name ``factory recover --revalidate`` as the way back, so the
    command accepts the task the line was printed for: an ``in_review``
    task holding an open NORMAL PR whose head moved or was never
    recorded. No gates run — the vouch is the owner's word on that
    exact head (a gate-red head is the shape the line names; the fix
    round that may build on it runs the full gate stack itself). The
    head is read from the REMOTE twice — it must not move between the
    reads; a moved head refuses with nothing written. Refused too,
    writing nothing: an escalation draft PR, a merged PR, any PR state
    gh cannot answer, or a contract without an owner approval (the
    vouch unblocks a round that will run gates — acting).
    """
    from factory.approvals import ApprovalLedger, require_profile_approval
    from factory.contract import contract_hash, load_profile
    from factory.orchestrator import _ls_remote_heads
    from factory.publish import PublishError as _PublishError

    factory_root = factory_root or FACTORY_ROOT_DEFAULT
    links = task.get("links") or {}
    url = links.get("pr")
    if (not url or links.get("escalation_pr")
            or links.get("merged") == url):
        raise ApprovalError(
            f"{task_id}: a head vouch needs an in_review task holding an "
            "open normal PR (no escalation draft, not merged) — answer an "
            "escalation draft PR with --accept/--reject, and let the sweep "
            "read a merge."
        )
    try:
        state = (pr_state or gh_pr_state)(url)
    except _PublishError:
        state = None  # gh failed: fail closed, never guessed
    if state != "open":
        shown = state if state is not None else "unreachable (gh failed)"
        raise ApprovalError(
            f"{task_id}: a head vouch needs GitHub to report the PR open — "
            f"it reports {shown}. Nothing is guessed when gh cannot answer."
        )
    ledger = ApprovalLedger.for_project(
        factory_root, queue.doc.get("project") or "unknown")
    chash = contract_hash(task)
    if not ledger.has_contract(task_id, chash):
        raise ApprovalError(
            f"{task_id}: the current contract (hash {chash[:12]}) has no "
            "owner approval in the ledger — a vouch unblocks a round that "
            "runs gates, which is acting on the task; re-approve "
            "(`factory adopt`) first"
        )
    profile_path = Path(queue.path).parent / "project.yaml"
    profile = load_profile(profile_path)
    require_profile_approval(profile, factory_root, origin=str(profile_path))
    who = done_by or default_approver()
    branch = links.get("branch")
    if not branch:
        raise ApprovalError(
            f"{task_id}: a head vouch needs the task's PR branch link "
            "(links.branch) — the queue holds no branch to read the head "
            "from"
        )
    import re as _re

    if head is None or not _re.fullmatch(r"[0-9a-f]{12,40}", head):
        current = _ls_remote_heads(profile["repo"]["remote"],
                                   (branch,)).get(branch, "")
        raise ApprovalError(
            f"{task_id}: an in_review head vouch must NAME the head you "
            f"judged — run: factory recover {task_id} --revalidate "
            f"--head {current or '<the PR head you saw>'} (the full SHA, "
            "or an unambiguous >=12-char prefix). Nothing was written."
        )
    echo(
        f"{task_id}: {task.get('title')}\n"
        f"  PR: {url} (open, in_review)\n"
        f"  vouching for head {head}\n"
        "  --revalidate vouches for exactly the head you NAMED (R3-1): "
        "the sweep's CI fix round may then build on that head. No gates "
        "run — your word on the head you saw is the vouch; the round "
        "itself runs the full gate stack before anything is pushed."
    )
    from factory.state import RunInProgressError as _RunInProgressError
    try:
        with queue.idle_lease():
            first = _ls_remote_heads(profile["repo"]["remote"],
                                     (branch,)).get(branch, "")
            second = _ls_remote_heads(profile["repo"]["remote"],
                                      (branch,)).get(branch, "")
            if not first or not second or first != second:
                shown = (first or "nothing", second or "nothing")
                raise ApprovalError(
                    f"{task_id}: the PR head moved under the vouch "
                    f"(read {shown[0][:12]}, then {shown[1][:12]}) — "
                    "nothing was written; re-read the PR and vouch again"
                )
            for read in (first, second):
                if not read.startswith(head):
                    raise ApprovalError(
                        f"{task_id}: the vouch names {head[:12]}, not the "
                        f"head the remote holds ({read[:12]}) — nothing "
                        "was written; re-read the PR and vouch for the "
                        "head you see"
                    )
            queue.vouch_pushed_head(task_id, pr_sha=second)
    except _RunInProgressError as exc:
        raise ApprovalError(
            f"{task_id}: the run lease is held ({exc}) — another run or "
            "housekeeping pass owns the queue right now; retry once it "
            "finishes. Nothing was written."
        ) from exc
    try:
        ProgressLog.for_task(
            factory_root, queue.doc.get("project") or "unknown", task_id,
        ).append({
            "outcome": "recovered",
            "done_by": who,
            "recovered_at": now_iso(),
            "note": (
                f"owner vouched for the PR's current head {second[:12]} "
                f"({url}): the sweep's CI fix round may build on exactly "
                "that head; no gates ran — the round's own gates are the "
                "net"
            ),
        })
    except ProgressLogError as exc:
        raise ApprovalError(
            f"{task_id}: the vouch landed but the progress log could not "
            f"be written — it is NOT durably recorded: {exc}"
        ) from exc
    return "in_review"


def _recover_escalation_pr(
    queue: TaskQueue,
    task_id: str,
    task: dict,
    *,
    accept: bool,
    reject: bool,
    evidence: str | None,
    assume_yes: bool,
    done_by: str | None,
    factory_root: Path | None,
    pr_head: "Callable[[str], str] | None",
    pr_ready: "Callable[[str], None] | None",
    pr_close: "Callable[[str], None] | None",
    session_runner: Callable | None,
    confirm: Callable[[str], bool],
    echo: Callable[[str], None],
) -> str:
    """The F-72 one-command answer to an escalation draft PR.

    Eligible only for a ``new_dependency``/``risk_path_touched``
    escalation holding an escalation draft PR (the state layer's
    :func:`~factory.state.escalation_accept_eligible` — re-checked there,
    the credential/attempts precedent). Both paths are interactive only:
    ``--yes`` is refused, the confirmation IS the gate.

    ``--accept --evidence``: read the PR's head SHA from GitHub and show
    it with the escalation and the L0 report summary; on y, record the
    waiver (task, escalation, head SHA, evidence, who, when) in the
    approval ledger, re-run L0 on that exact SHA with ONLY that class
    waived and continue L1 and review as a normal round
    (:func:`factory.orchestrator.recover_accept_round`); on pass, mark
    the PR ready and move the task ``needs_human → in_review``. Any new
    commit on the branch voids the waiver (the head is re-read before
    the ready mark). A failed re-run keeps the task ``needs_human`` with
    the new findings and the PR draft.

    ``--reject``: close the escalation PR, move the task
    ``needs_human → draft`` and clear the contract's approval markers —
    the contract can now change, and the old approval authorises nothing
    (the F-63 hash check).
    """
    from factory.publish import gh_pr_close, gh_pr_head_sha, gh_pr_ready
    from factory.runs import RunError
    from factory.state import escalation_accept_eligible

    if accept and reject:
        raise ApprovalError(
            f"{task_id}: give --accept or --reject, not both"
        )
    if not escalation_accept_eligible(task):
        raise ApprovalError(
            f"{task_id}: --accept/--reject answer an escalation draft PR — "
            "a needs_human task whose last attempt escalated "
            "new_dependency or risk_path_touched and which holds one "
            "(links.pr + links.escalation_pr). The supported reasons are "
            "exactly those two; every other escalation keeps its current "
            "exit (re-open or close the PR on GitHub, re-approve a "
            "changed contract, or grant a budget reset)."
        )
    if assume_yes:
        raise ApprovalError(
            f"{task_id}: escalation acceptance is interactive only — "
            "--yes is not accepted; the point is that a human judged the "
            "work on the PR"
        )
    attempts = task.get("attempts") or []
    escalation = attempts[-1].get("escalation")
    url = task["links"]["pr"]
    who = done_by or default_approver()

    if reject:
        echo(
            f"{task_id}: {task.get('title')}\n"
            f"  escalation: {escalation}\n"
            f"  PR: {url} (draft)\n"
            "  reject closes the PR and returns the task to draft with "
            "its approval markers cleared — the contract can change, and "
            "re-approval is required before anything runs again."
        )
        if not confirm(f"Reject the escalation PR for {task_id}?"):
            raise ApprovalDeclined(f"{task_id}: escalation rejection declined")
        # R1-F8: the STATE moves first — a close failure then leaves a
        # draft task with an open PR (visible, closable by hand), never a
        # needs_human task holding a PR gh already closed (which a retry
        # would refuse on).
        new_status = queue.reject_escalation(task_id)
        try:
            (pr_close or gh_pr_close)(url)
        except PublishError as exc:
            raise ApprovalError(
                f"{task_id}: rejected to draft, but closing {url} failed "
                f"({exc}) — close it by hand; the task no longer holds it"
            ) from exc
        try:
            ProgressLog.for_task(
                factory_root or FACTORY_ROOT_DEFAULT,
                queue.doc.get("project") or "unknown", task_id,
            ).append({
                "outcome": "recovered",
                "escalation": escalation,
                "done_by": who,
                "recovered_at": now_iso(),
                "note": f"escalation PR {url} closed; {task_id} rejected "
                        f"to draft by {who} (approval markers cleared)",
            })
        except ProgressLogError as exc:
            raise ApprovalError(
                f"{task_id}: moved to draft, but the progress log could "
                f"not be written — the rejection is NOT durably recorded: "
                f"{exc}"
            ) from exc
        return new_status

    # --accept
    if evidence is None or not evidence.strip():
        raise ApprovalError(
            f"{task_id}: escalation acceptance requires --evidence — what "
            "you checked on the PR before accepting it (e.g. 'read the "
            "diff; requests is the documented client')"
        )
    head = (pr_head or gh_pr_head_sha)(url)
    # The stored L0 report's summary, beside the head the evidence covers.
    summary = ""
    report_path = attempts[-1].get("validator_report")
    if report_path and Path(report_path).is_file():
        try:
            l0 = json.loads(Path(report_path).read_text()).get("L0") or {}
            summary = "escalations: " + ", ".join(l0.get("escalations") or [])
        except (ValueError, OSError):
            summary = ""
    echo(
        f"{task_id}: {task.get('title')}\n"
        f"  escalation: {escalation} {summary}\n"
        f"  PR: {url}\n"
        f"  PR head: {head}\n"
        f"  evidence: {evidence.strip()}\n"
        + render_contract(task)
        + "\n  accept records the waiver in the ledger, re-runs L0 on that "
        "exact head with only this escalation class waived, continues L1 "
        "and review, and — on pass — marks the PR ready and moves the "
        "task to in_review. Any new commit on the branch voids the waiver."
    )
    if not confirm(f"Accept {escalation} on {task_id} at {head[:12]}?"):
        raise ApprovalDeclined(f"{task_id}: escalation acceptance declined")
    from factory.orchestrator import recover_accept_round

    try:
        new_status = recover_accept_round(
            queue, task_id,
            ledger=ApprovalLedger.for_project(
                factory_root or FACTORY_ROOT_DEFAULT,
                queue.doc.get("project") or "unknown",
            ),
            sha=head, evidence=evidence.strip(), done_by=who,
            factory_root=(factory_root or FACTORY_ROOT_DEFAULT),
            session_runner=session_runner,
            pr_head=pr_head or gh_pr_head_sha,
            pr_ready=pr_ready or gh_pr_ready,
        )
    except RunError as exc:
        raise ApprovalError(str(exc)) from exc
    except (PublishError, PushRefused) as exc:
        # R2-F1 guard 2: the lease refusal (the remote moved between the
        # acceptance and the release) and any push/ready failure arrive
        # as PublishError — and, since F-75's lease PRE-CHECK, a moved
        # remote refuses as PushRefused BEFORE any git push runs — the
        # owner hears both as refusals, never a traceback.
        raise ApprovalError(
            f"{task_id}: the acceptance's release failed ({exc}) — the "
            "task stays needs_human and the PR stays draft; re-read the "
            "PR head and accept again"
        ) from exc
    try:
        ProgressLog.for_task(
            factory_root or FACTORY_ROOT_DEFAULT,
            queue.doc.get("project") or "unknown", task_id,
        ).append({
            "outcome": "recovered",
            "escalation": escalation,
            "evidence": evidence.strip(),
            "done_by": who,
            "recovered_at": now_iso(),
            "note": f"{escalation} accepted at head {head[:12]} by {who} "
                    f"({url} marked ready, {task_id} → in_review): "
                    f"{evidence.strip()}",
        })
    except ProgressLogError as exc:
        raise ApprovalError(
            f"{task_id}: moved to {new_status}, but the progress log could "
            f"not be written — the acceptance is NOT durably recorded: {exc}"
        ) from exc
    return new_status


def _recover_credential_task(
    queue: TaskQueue,
    task_id: str,
    task: dict,
    *,
    evidence: str | None,
    assume_yes: bool,
    done_by: str | None,
    factory_root: Path | None,
    confirm: Callable[[str], bool],
    echo: Callable[[str], None],
) -> str:
    """The credential_added re-queue: evidence + interactive y/N, the
    state layer's dedicated edge (:meth:`TaskQueue.recover_credential`),
    and the confirmation persisted to the task's progress log (outcome
    ``recovered``, escalation, evidence, ``done_by``, timestamp).
    Session-less, no attempt reset (the remaining budget is the owner's
    call, made with the finding named in ``evidence``). Refuses before
    any write on a missing or empty evidence, on ``--yes`` (the
    confirmation IS the gate), and on a declined prompt."""
    # Same refusal as `(evidence or "").strip()`, written so the type
    # narrows to str for every use below (F-51/A-7: narrowing, no
    # behaviour change — None and whitespace-only both stop here).
    if evidence is None or not evidence.strip():
        raise ApprovalError(
            f"{task_id}: credential_added recovery requires --evidence — "
            "what you checked (e.g. 'test fixture, not a real credential' "
            "or 'rotated')"
        )
    if assume_yes:
        raise ApprovalError(
            f"{task_id}: credential_added recovery is interactive only — "
            "--yes is not accepted; the point is that a human looked at "
            "the finding"
        )
    attempts = task.get("attempts") or []
    echo(
        f"{task_id}: {task.get('title')}\n"
        f"  escalation: {attempts[-1].get('escalation')} "
        "(nothing was pushed; the finding is local to the run branch)\n"
        f"  evidence: {evidence.strip()}\n"
        "  recover re-queues the task on a fresh run (the old run branch "
        "is discarded); attempts are NOT reset."
    )
    if not confirm(f"Re-queue {task_id} (credential confirmed handled)?"):
        raise ApprovalDeclined(f"{task_id}: credential recovery declined")
    new_status = queue.recover_credential(task_id, evidence)
    who = done_by or default_approver()
    # The audit trail: the state has moved, so a log failure is loud —
    # the operator must know the one durable record of the confirmation
    # is missing, not discover it in an incident review.
    try:
        entry = ProgressLog.for_task(
            factory_root or FACTORY_ROOT_DEFAULT,
            queue.doc.get("project") or "unknown",
            task_id,
        ).append({
            "outcome": "recovered",
            "escalation": "credential_added",
            "evidence": evidence.strip(),
            "done_by": who,
            "recovered_at": now_iso(),
            "note": f"credential_added re-queued ({new_status}) by "
                    f"{who}: {evidence.strip()}",
        })
    except ProgressLogError as exc:
        raise ApprovalError(
            f"{task_id}: moved to {new_status}, but the progress log could "
            f"not be written — the confirmation is NOT durably recorded: {exc}"
        ) from exc
    assert entry.get("outcome") == "recovered"
    return new_status


def _recover_attempts_task(
    queue: TaskQueue,
    task_id: str,
    task: dict,
    *,
    evidence: str | None,
    reset_attempts: bool,
    assume_yes: bool,
    done_by: str | None,
    factory_root: Path | None,
    confirm: Callable[[str], bool],
    echo: Callable[[str], None],
) -> str:
    """The attempts_exhausted re-queue: evidence + --reset-attempts +
    interactive y/N, the state layer's dedicated edge
    (:meth:`TaskQueue.recover_attempts`), history kept (the reset appends
    an ``attempt_resets`` marker; the budget counts only attempts after
    it) and the confirmation persisted to the task's progress log
    (outcome ``recovered``, the escalation, the evidence, ``done_by``,
    how many attempts were reset, a timestamp). Refuses before any write
    on a missing or empty evidence, on a missing --reset-attempts (with
    the reason), on ``--yes``, and on a declined prompt."""
    if not reset_attempts:
        raise ApprovalError(
            f"{task_id}: attempts_exhausted recovery requires "
            "--reset-attempts — the budget is spent, so re-queueing alone "
            "would park the task straight back on the pre-run guard. Pass "
            "the flag to grant a fresh budget (history is kept; every "
            "attempt record stays) with --evidence saying what you checked "
            "(e.g. 'all attempts died to the sandbox bind bug, fixed in "
            "PR #NN')."
        )
    # Same refusal as `(evidence or "").strip()`, written so the type
    # narrows to str for every use below (F-51/A-7: narrowing, no
    # behaviour change — None and whitespace-only both stop here).
    if evidence is None or not evidence.strip():
        raise ApprovalError(
            f"{task_id}: attempts_exhausted recovery requires --evidence — "
            "what you checked before granting more attempts (the three "
            "burned attempts are the reason this gate exists)"
        )
    if assume_yes:
        raise ApprovalError(
            f"{task_id}: attempts_exhausted recovery is interactive only — "
            "--yes is not accepted; spending a fresh budget is a human "
            "decision, not a default"
        )
    attempts = task.get("attempts") or []
    echo(
        f"{task_id}: {task.get('title')}\n"
        f"  escalation: {attempts[-1].get('escalation')} "
        f"({len(attempts)} attempt record(s) on file)\n"
        f"  evidence: {evidence.strip()}\n"
        "  recover re-queues the task with a fresh attempt budget; every "
        "existing attempt record is kept (the budget counts only attempts "
        "after the reset marker)."
    )
    if not confirm(f"Re-queue {task_id} with a fresh attempt budget?"):
        raise ApprovalDeclined(f"{task_id}: attempt reset declined")
    who = done_by or default_approver()
    new_status = queue.recover_attempts(task_id, evidence, done_by=who)
    marker = (queue.get(task_id).get("attempt_resets") or [{}])[-1]
    # The audit trail: the state has moved, so a log failure is loud —
    # same posture as the credential exit.
    try:
        entry = ProgressLog.for_task(
            factory_root or FACTORY_ROOT_DEFAULT,
            queue.doc.get("project") or "unknown",
            task_id,
        ).append({
            "outcome": "recovered",
            "escalation": "attempts_exhausted",
            "evidence": evidence.strip(),
            "done_by": who,
            "recovered_at": now_iso(),
            "attempts_reset": marker.get("prior_attempts"),
            "note": f"attempts_exhausted re-queued ({new_status}) with a "
                    f"fresh budget by {who}; {marker.get('prior_attempts')} "
                    f"attempt(s) kept on file: {evidence.strip()}",
        })
    except ProgressLogError as exc:
        raise ApprovalError(
            f"{task_id}: moved to {new_status}, but the progress log could "
            f"not be written — the confirmation is NOT durably recorded: {exc}"
        ) from exc
    assert entry.get("outcome") == "recovered"
    return new_status


def _recover_scope_task(
    queue: TaskQueue,
    task_id: str,
    task: dict,
    *,
    evidence: str | None,
    assume_yes: bool,
    done_by: str | None,
    factory_root: Path | None,
    confirm: Callable[[str], bool],
    echo: Callable[[str], None],
) -> str:
    """The F-65 scope re-queue: evidence + interactive y/N + the ledger
    check, through the state layer's dedicated edge
    (:meth:`TaskQueue.recover_scope`), with the confirmation persisted
    to the task's progress log like the credential exit. The gate that
    distinguishes it: the task's CURRENT contract hash (the widened
    scope included) must already be owner-approved in the F-63 ledger —
    recover moves nothing the owner has not re-approved (class-2
    amendment: the spec said "the existing approval path", but no
    existing path moved a needs_human scope escalation back to ready)."""
    if evidence is None or not evidence.strip():
        raise ApprovalError(
            f"{task_id}: scope recovery requires --evidence — what you "
            "checked before re-queueing (e.g. 'the session's reason was "
            "sound; widened to cover tests/util.py')"
        )
    if assume_yes:
        raise ApprovalError(
            f"{task_id}: scope recovery is interactive only — --yes is not "
            "accepted; widening a contract is a human decision, not a "
            "default"
        )
    attempts = task.get("attempts") or []
    scope_now = ", ".join(_scope_paths(task)) or "(none declared)"
    echo(
        f"{task_id}: {task.get('title')}\n"
        f"  escalation: {attempts[-1].get('escalation')} "
        "(nothing was pushed; the request or refusal is in the attempt "
        "note)\n"
        f"  scope now declared: {scope_now}\n"
        f"  evidence: {evidence.strip()}\n"
        "  recover re-queues the task under the CURRENT contract — widen "
        "its scope in the queue and record the re-approval first "
        "(`factory adopt` shows the contract and records the new hash; "
        "`factory approve` refuses non-draft tasks); a scope the ledger "
        "does not cover does not move. Attempts are NOT reset."
    )
    if not confirm(f"Re-queue {task_id} under its re-approved scope?"):
        raise ApprovalDeclined(f"{task_id}: scope recovery declined")
    who = done_by or default_approver()
    root = factory_root or FACTORY_ROOT_DEFAULT
    ledger = ApprovalLedger.for_project(
        root, queue.doc.get("project") or "unknown"
    )
    new_status = queue.recover_scope(
        task_id, ledger=ledger, evidence=evidence.strip(), done_by=who
    )
    # The audit trail: the state has moved, so a log failure is loud —
    # same posture as the credential and attempts exits.
    try:
        entry = ProgressLog.for_task(
            root,
            queue.doc.get("project") or "unknown",
            task_id,
        ).append({
            "outcome": "recovered",
            "escalation": attempts[-1].get("escalation"),
            "evidence": evidence.strip(),
            "done_by": who,
            "recovered_at": now_iso(),
            "note": f"scope escalation re-queued ({new_status}) by {who} "
                    f"under the re-approved contract: {evidence.strip()}",
        })
    except ProgressLogError as exc:
        raise ApprovalError(
            f"{task_id}: moved to {new_status}, but the progress log could "
            f"not be written — the confirmation is NOT durably recorded: {exc}"
        ) from exc
    assert entry.get("outcome") == "recovered"
    return new_status


def _clean_entry_id(token: str) -> str:
    """Strip heading punctuation from a ledger entry id.

    ``## F-05: Clone manager`` yields the token ``F-05:``; the trailing
    colon is heading punctuation, not part of the id.
    """
    return token.rstrip(":,.").rstrip("：，。")


def parse_bootstrap_ledger(text: str) -> list:
    """Parse a BOOTSTRAP.md ledger into reconcile entries.

    A ``## <task-id> ...`` heading starts an entry (trailing punctuation
    on the id token is stripped); within its section, criterion bullets
    ``- **AC-n** proven|pending|not met`` are collected (``not met``
    normalised to its spaced form), and the first backticked status
    after ``**Status to set at reconcile:**`` is the recorded status.

    Sections with criteria, a parseable status, or a status marker that
    failed to parse (backticks missing, say) are entries — the last kind
    carries ``status=None`` so reconcile reports it as a failure instead
    of silently dropping a work order the ledger meant to record. Pure
    prose sections (no criteria, no status marker) are not entries.

    Args:
        text: The ledger's full text.

    Returns:
        Entries in file order, ``{id, criteria: {AC-id: marker}, status}``;
        ``status`` is None when the entry records none or unparseably.
    """
    entries = []
    sections = re.split(r"^##\s+", text, flags=re.MULTILINE)
    for section in sections[1:]:
        first_line, _, body = section.partition("\n")
        words = first_line.split()
        entry_id = _clean_entry_id(words[0]) if words else ""
        criteria = {
            match.group(1): re.sub(r"\s+", " ", match.group(2))
            for match in _CRITERION_RE.finditer(body)
        }
        status_match = _RECORDED_STATUS_RE.search(body)
        if entry_id and (criteria or status_match or _STATUS_MARKER in body):
            entries.append(
                {
                    "id": entry_id,
                    "criteria": criteria,
                    "status": status_match.group(1) if status_match else None,
                }
            )
    return entries


def _unproven_criteria(task: dict, entry: dict) -> list:
    """Task acceptance ids not marked ``proven`` in the ledger entry.

    A criterion the entry never mentions is not proven either — evidence
    per criterion is what the ledger exists to record.
    """
    markers = entry.get("criteria", {})
    return [
        item.get("id", "?")
        for item in task.get("acceptance") or []
        if markers.get(item.get("id")) != "proven"
    ]


def _walk_path(target: str) -> list:
    """The legal statuses between approval (``ready``/``blocked``) and target."""
    if target in ("ready", "blocked"):
        return []
    if target in _WALK_CHAIN:
        return list(_WALK_CHAIN[: _WALK_CHAIN.index(target) + 1])
    if target in ("awaiting_owner", "done"):
        return [*_WALK_CHAIN, target]
    if target == "needs_human":
        return ["in_progress", "needs_human"]
    raise ApprovalError(
        f"cannot reconcile to {target!r}: not a status the ledger may record"
    )


def _reconcile_route(task: dict, tasks: list, target: str) -> tuple:
    """Compute one entry's final status and the legal path to it.

    A recorded ``done`` with owner steps still open is routed to
    ``awaiting_owner`` — the F-03 owner-step guard refuses ``done`` in
    that case, and awaiting_owner is what the evidence supports until
    you tick the steps. The path starts at the approval routing
    (``ready``, or ``blocked`` when a dependency is open) and continues
    along the forward chain; only its final status is ever persisted
    (:meth:`TaskQueue.transition_path`).

    Args:
        task: The task record.
        tasks: The queue's ``tasks`` array.
        target: The ledger-recorded status.

    Returns:
        ``(final_status, path)``; the path lists every status to move
        through, starting from the approval routing.
    """
    final = target
    if target == "done" and open_owner_steps(task):
        final = "awaiting_owner"
    start = "blocked" if unmet_dependencies(task, tasks) else "ready"
    return final, [start, *_walk_path(final)]


def _reconcile_entry(
    queue: TaskQueue,
    entry: dict,
    approved_by: str,
    confirm: Callable[[str], bool],
    echo: Callable[[str], None],
    ledger: ApprovalLedger | None = None,
) -> ReconcileResult:
    """Reconcile one ledger entry; never raises, failures become results."""
    tid = entry["id"]
    target = entry["status"]
    try:
        task = queue.get(tid)
    except ContractError as exc:
        return ReconcileResult(tid, "failed", f"no such task: {exc}")
    if target is None:
        return ReconcileResult(tid, "failed", "ledger entry records no status to set")
    if task.get("status") != "draft":
        return ReconcileResult(
            tid, "skipped", f"already {task.get('status')}; nothing to reconcile"
        )
    if target in _PROVEN_REQUIRED_TARGETS and (bad := _unproven_criteria(task, entry)):
        return ReconcileResult(
            tid,
            "failed",
            f"a criterion marked pending (or not proven) cannot produce "
            f"{target}: {', '.join(bad)}",
        )
    if target != "blocked" and (
        open_deps := unmet_dependencies(task, queue.doc.get("tasks", []))
    ):
        return ReconcileResult(
            tid,
            "failed",
            f"dependency not met ({', '.join(open_deps)}); reconcile entries "
            "in dependency order",
        )
    # F-71: an entry whose target makes the task runnable takes the
    # same approval checks as `factory approve` — the F-65 scope and
    # F-71 ref refusals first, then the pre-spend checks (profile, risk
    # coverage, floor), whose warnings render in the contract. The
    # refusal becomes a failed RESULT here (reconcile never raises);
    # proven-criteria targets record built work and take none of these.
    warnings: list = []
    if target not in _PROVEN_REQUIRED_TARGETS:
        try:
            _check_approvable(task, require_scope=True)
            warnings = _prespend_checks(task, queue)
        except ContractError as exc:
            return ReconcileResult(tid, "failed", str(exc))
    echo(render_contract(task, warnings=warnings))
    markers = ", ".join(
        f"{escape_approval_text(str(ac))}={escape_approval_text(str(m))}"
        for ac, m in sorted(entry["criteria"].items())
    )
    echo(f"Ledger evidence: {markers or '(no criterion markers)'}")
    echo(f"Recorded status to set: {target}")
    if not confirm(f"Approve {tid} and advance it to {target}?"):
        return ReconcileResult(tid, "skipped", "declined")
    try:
        # One atomic save: record the owner approval, freeze the
        # contract, then move through the whole validated path to the
        # final status. A refusal anywhere leaves the file untouched —
        # no half-approved task. The ledger entry is written BEFORE the
        # move (the approve_task order): an entry for a task that never
        # moved is harmless history, an approved task the ledger doesn't
        # cover would be demoted straight back (F-63).
        # The F-65 scope (and F-71's pre-spend checks, run above for the
        # render) is a run-time boundary: demanded when this reconcile
        # makes the task runnable, not when it records work already
        # built and validated (the proven-criteria targets).
        _check_approvable(
            task, require_scope=target not in _PROVEN_REQUIRED_TARGETS)
        _freeze_contract(task, approved_by)
        if ledger is not None:
            ledger.record_task_for(task, approved_by=approved_by,
                                   approved_at=task["approved_at"])
        final, path = _reconcile_route(task, queue.doc.get("tasks", []), target)
        queue.transition_path(tid, path)
    except ContractError as exc:
        return ReconcileResult(tid, "failed", str(exc))
    detail = f"approved and advanced to {final}"
    if final != target:
        detail += f" (recorded {target}, but owner steps are open)"
    return ReconcileResult(tid, "reconciled", detail)


def reconcile_bootstrap(
    queue: TaskQueue,
    ledger_path: Path | str,
    *,
    approved_by: str,
    confirm: Callable[[str], bool] = input_confirm,
    echo: Callable[[str], None] = print,
    factory_root: Path | None = None,
) -> list:
    """Approve and advance the tasks named by the bootstrap ledger (AC-4).

    For each ledger entry, in file order: print the contract and the
    entry's evidence markers, confirm interactively (reconcile has no
    ``--yes`` — the ledger is plain markdown and this command turns it
    into ``done`` tasks), then freeze the contract, record the approval
    in the owner ledger (F-63 AC-1) and apply the entry's final status
    through :meth:`TaskQueue.transition_path` — the whole path validated
    as legal F-03 transitions, written in one atomic save, never
    persisting intermediate statuses. A recorded status that asserts the
    build passed validation (``in_review`` and beyond) requires every
    acceptance criterion to be marked ``proven``; pending, not met, or
    unmentioned criteria refuse the entry. A recorded ``done`` with
    owner steps still open reconciles to ``awaiting_owner``. Entries
    whose task is no longer draft are skipped, so re-running after a
    partial reconcile is safe. One entry's failure never stops the
    others; every outcome is reported in the returned summary.

    Args:
        queue: The queue to write through.
        ledger_path: Path to ``.factory/BOOTSTRAP.md``.
        approved_by: Name recorded as the approver of every entry.
        confirm: Interactive confirmation callable; injected in tests.
        echo: Output callable for contracts and summaries; injected in
            tests.
        factory_root: Factory root for the approval ledger (default
            ``~/factory``).

    Returns:
        One :class:`ReconcileResult` per entry, in ledger order; action
        is ``reconciled``, ``skipped`` or ``failed``.

    Raises:
        ApprovalError: If the ledger itself cannot be read.
    """
    try:
        text = Path(ledger_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ApprovalError(
            f"{ledger_path}: cannot read bootstrap ledger: {exc}"
        ) from exc
    ledger = ApprovalLedger.for_project(
        factory_root or FACTORY_ROOT_DEFAULT,
        queue.doc.get("project") or "unknown",
    )
    return [
        _reconcile_entry(queue, entry, approved_by, confirm, echo, ledger)
        for entry in parse_bootstrap_ledger(text)
    ]


def render_profile_security(profile: dict) -> str:
    """Render the profile's security-relevant groups for the owner.

    One block per group (the F-63 AC-2 set), network entries one per
    line; a missing group renders as ``(absent)`` so REMOVAL is as
    visible as a change.
    """
    lines = [f"profile {profile.get('name')} — security-relevant content:"]
    for group in PROFILE_SECURITY_GROUPS:
        value: object = profile
        for key in PROFILE_SECURITY_GROUPS[group]:
            value = value.get(key) if isinstance(value, dict) else None
        # The two groups that name WHAT the factory clones or pushes to
        # render with every non-ASCII character escaped too — a
        # homoglyph remote reads as its codepoints, not as the real host.
        homoglyph_sensitive = group in ("repo.path", "repo.remote")
        if group == "repo.path" and isinstance(value, str):
            # The approved form: what the run will actually clone.
            value = str(Path(value).expanduser().resolve())
        if value is None:
            lines.append(f"  {group}: (absent)")
        elif isinstance(value, list):
            lines.append(f"  {group}:")
            entries = [
                f"    - {escape_approval_text(str(entry))}" for entry in value
            ]
            lines += entries or ["    (empty)"]
        elif isinstance(value, str):
            lines.append(
                f"  {group}: "
                f"{escape_approval_text(value, escape_non_ascii=homoglyph_sensitive)}"
            )
        else:
            lines.append(f"  {group}: {escape_approval_text(str(value))}")
    return "\n".join(lines)


def approve_profile(
    profile: dict,
    factory_root: Path | str | None = None,
    *,
    approved_by: str,
    confirm: Callable[[str], bool] = input_confirm,
    echo: Callable[[str], None] = print,
) -> None:
    """Show the profile's security-relevant content, confirm, record it.

    F-63 AC-2: the approval prompt prints the covered groups — network
    entries one per line — and asks interactively. There is no ``--yes``
    for this command at all: the whole point is that a human read the
    egress allowlist, the commands, the sandbox knobs. The recorded
    entry hashes the SAME dict the caller will run with (load once,
    approve what you loaded).

    Raises:
        ApprovalError: On a declined prompt or a ledger refusal
            (nothing recorded).
        ApprovalDeclined: When the human answers no (or EOF).
    """
    echo(render_profile_security(profile))
    if not confirm(f"Approve this profile's security content for "
                   f"{profile.get('name')}?"):
        raise ApprovalDeclined(
            f"{profile.get('name')}: profile approval declined"
        )
    ledger = ApprovalLedger.for_project(factory_root, profile["name"])
    try:
        ledger.record_profile_for(profile, approved_by=approved_by)
    except ApprovalLedgerError as exc:
        raise ApprovalError(
            f"{profile.get('name')}: the approval ledger refused the entry "
            f"and nothing was approved — {exc}"
        ) from exc


def adopt_queue(
    queue: TaskQueue,
    factory_root: Path | str | None = None,
    *,
    approved_by: str,
    confirm: Callable[[str], bool] = input_confirm,
    echo: Callable[[str], None] = print,
) -> list:
    """Adopt an existing queue into the owner ledger (F-63 AC-1).

    For every task in an approvable-or-later status whose CURRENT
    contract hash has no ledger entry — a queue that predates the
    ledger, or one assembled where it lives today (the client repo) —
    show the contract and record the approval on confirm. Always
    interactive (there is no ``--yes`` path here at all, so a
    risk-flagged task can never be adopted non-interactively — the
    approve rule). Done tasks are skipped: merged work needs no entry.

    Returns:
        One :class:`ReconcileResult` per task, action ``adopted``,
        ``skipped`` or ``failed``.
    """
    from factory.contract import contract_hash
    from factory.state import CONTRACT_FROZEN_STATUSES

    ledger = ApprovalLedger.for_project(
        factory_root or FACTORY_ROOT_DEFAULT,
        queue.doc.get("project") or "unknown",
    )
    results: list = []
    for task in queue.doc.get("tasks", []):
        status = task.get("status")
        if status not in CONTRACT_FROZEN_STATUSES or status == "done":
            continue
        if ledger.has_contract(task.get("id", ""), contract_hash(task)):
            continue
        echo(render_contract(task))
        if not confirm(f"Adopt {task.get('id')} (currently {status}) "
                       "into the owner ledger?"):
            results.append(ReconcileResult(task.get("id"), "skipped", "declined"))
            continue
        try:
            ledger.record_task_for(task, approved_by=approved_by)
        except ApprovalLedgerError as exc:
            results.append(ReconcileResult(task.get("id"), "failed", str(exc)))
            continue
        results.append(
            ReconcileResult(task.get("id"), "adopted",
                            f"recorded at status {status}")
        )
    return results
