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
  dependency rule, so an approved-but-blocked task can never be picked.

``--reconcile-bootstrap`` (AC-4) turns the bootstrap ledger
(``.factory/BOOTSTRAP.md``) into queue state: each entry names a task,
its criterion markers and the status the evidence supports. After
interactive confirmation per entry, the task is approved and advanced
through the F-03 allowed-transition table to that recorded status, one
legal transition at a time. A criterion marked pending — or not met, or
never mentioned — can never produce ``done``: the entry is refused and
the task stays ``draft``.
"""

import getpass
import re
import subprocess
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path

from factory.contract import NON_EXECUTABLE_KINDS, ContractError, acceptance_hash
from factory.state import TaskQueue, now_iso, unmet_dependencies

# Ledger grammar (.factory/BOOTSTRAP.md):
#   ## <task-id> — <title>                     starts an entry
#   - **AC-n** proven|pending|not met: ...     criterion markers
#   **Status to set at reconcile:** `<status>`  the status evidence supports
_CRITERION_RE = re.compile(
    r"^\s*-\s+\*\*(AC-\d+)\*\*\s+(proven|pending|not\s+met)\b", re.MULTILINE
)
_RECORDED_STATUS_RE = re.compile(r"\*\*Status to set at reconcile:\*\*\s*`([a-z_]+)`")

# The forward lifecycle after approval, in order. Reconcile walks a task
# through these one transition at a time so every step is a legal F-03
# edge that validates and persists on its own. ``awaiting_owner`` sits
# off the direct path: done comes straight from deploying when the owner
# steps are closed, per the transition table.
_FORWARD_CHAIN = ("ready", "in_progress", "in_review", "deploying")
_WALK_CHAIN = _FORWARD_CHAIN[1:]  # approve_task already reached "ready"

# One reconcile outcome per ledger entry: what happened, and why.
ReconcileResult = namedtuple("ReconcileResult", ["task_id", "action", "detail"])


class ApprovalError(ContractError):
    """An approval or reconcile was refused. The message is for humans."""


class ApprovalDeclined(ApprovalError):
    """The human answered no to the confirmation prompt."""


def render_contract(task: dict) -> str:
    """Render the contract a human is about to approve.

    Everything the approval decision depends on, nothing else: identity,
    risk, dependencies, the acceptance items with their checks, owner
    steps and slices.

    Args:
        task: The task record.

    Returns:
        The contract as plain text, one fact per line.
    """
    budget = task.get("budget") or {}
    risk = task.get("risk") or []
    deps = task.get("depends_on") or []
    lines = [
        f"{task.get('id')} — {task.get('title')}",
        "  kind: {}   priority: {}   budget: {} (max {} attempts)".format(
            task.get("kind"), task.get("priority"), budget.get("size", "?"),
            budget.get("max_attempts", "?"),
        ),
        f"  source: {task.get('source')}",
        f"  risk: {', '.join(risk) if risk else '(none)'}",
        f"  depends_on: {', '.join(deps) if deps else '(none)'}",
        "  acceptance (frozen on approval):",
    ]
    for item in task.get("acceptance") or []:
        check = item.get("check", {})
        lines.append(f"    {item.get('id')}: {item.get('text')}")
        if check.get("ref"):
            lines.append(f"      check: {check.get('type')} {check.get('ref')}")
    for step in task.get("owner_steps") or []:
        mark = "x" if step.get("done") else " "
        lines.append(f"  owner step [{mark}]: {step.get('text')}")
    for slice_ in task.get("slices") or []:
        lines.append(
            f"  slice {slice_.get('id')} [{slice_.get('status')}]: {slice_.get('text')}"
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


def _input_confirm(question: str) -> bool:
    """Ask a yes/no question on the terminal; anything but y/yes declines."""
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def approve_task(
    queue: TaskQueue,
    task_id: str,
    *,
    approved_by: str,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] = _input_confirm,
    echo: Callable[[str], None] = print,
) -> str:
    """Print a draft task's contract, confirm, freeze it, and route it.

    Refused — :class:`ApprovalError`, nothing written — when the task is
    not a draft, is of a non-executable kind, has no acceptance items
    (AC-1), or is risk-flagged while ``assume_yes`` is set (AC-3). On
    confirmation the contract is frozen (``acceptance_hash``,
    ``approved_at``, ``approved_by``) and the task moves
    ``draft -> ready`` or ``draft -> blocked`` per the picker's
    dependency rule (AC-2).

    Args:
        queue: The queue to write through (its only writer path).
        task_id: Task to approve.
        approved_by: Name recorded as the approver.
        assume_yes: Skip the confirmation prompt; never allowed for
            risk-flagged tasks.
        confirm: Interactive confirmation callable; injected in tests.
        echo: Output callable for the contract; injected in tests.

    Returns:
        The new status: ``ready`` or ``blocked``.

    Raises:
        ApprovalError: On any refusal, or an unknown task.
        ApprovalDeclined: When the human answers no.
        ContractError: If the resulting queue wouldn't validate.
    """
    task = queue.get(task_id)  # StateError (a ContractError) if unknown
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
    risk = task.get("risk") or []
    if assume_yes and risk:
        raise ApprovalError(
            f"{task_id}: risk flags ({', '.join(risk)}) require interactive "
            "approval; --yes is not accepted for risk-flagged tasks"
        )
    echo(render_contract(task))
    if not assume_yes and not confirm(f"Approve {task_id}?"):
        raise ApprovalDeclined(f"{task_id}: approval declined")
    task["acceptance_hash"] = acceptance_hash(task["acceptance"])
    task["approved_at"] = now_iso()
    task["approved_by"] = approved_by
    new_status = (
        "blocked" if unmet_dependencies(task, queue.doc.get("tasks", [])) else "ready"
    )
    queue.transition(task_id, new_status)
    return new_status


def parse_bootstrap_ledger(text: str) -> list:
    """Parse a BOOTSTRAP.md ledger into reconcile entries.

    A ``## <task-id> ...`` heading starts an entry; within its section,
    criterion bullets ``- **AC-n** proven|pending|not met`` are collected
    (``not met`` normalised to its spaced form), and the first backticked
    status after ``**Status to set at reconcile:**`` is the recorded
    status. Sections with neither criteria nor a recorded status are not
    ledger entries and are ignored.

    Args:
        text: The ledger's full text.

    Returns:
        Entries in file order, ``{id, criteria: {AC-id: marker}, status}``;
        ``status`` is None when the entry records none.
    """
    entries = []
    sections = re.split(r"^##\s+", text, flags=re.MULTILINE)
    for section in sections[1:]:
        first_line, _, body = section.partition("\n")
        words = first_line.split()
        entry_id = words[0] if words else ""
        criteria = {
            match.group(1): re.sub(r"\s+", " ", match.group(2))
            for match in _CRITERION_RE.finditer(body)
        }
        status_match = _RECORDED_STATUS_RE.search(body)
        if entry_id and (criteria or status_match):
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


def _reconcile_path(target: str) -> list:
    """The legal statuses to walk *after* approval, target included.

    ``approve_task`` has already moved the task ``draft -> ready`` (or
    ``blocked``), so the walk starts at ``in_progress``.
    """
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


def _reconcile_entry(
    queue: TaskQueue,
    entry: dict,
    approved_by: str,
    assume_yes: bool,
    confirm: Callable[[str], bool],
    echo: Callable[[str], None],
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
    if target == "done" and (bad := _unproven_criteria(task, entry)):
        return ReconcileResult(
            tid,
            "failed",
            "a criterion marked pending (or not proven) can never produce "
            f"done: {', '.join(bad)}",
        )
    if target != "blocked" and (open_deps := unmet_dependencies(
        task, queue.doc.get("tasks", [])
    )):
        return ReconcileResult(
            tid,
            "failed",
            f"dependency not met ({', '.join(open_deps)}); reconcile entries "
            "in dependency order",
        )
    risk = task.get("risk") or []
    if assume_yes and risk:
        return ReconcileResult(
            tid,
            "failed",
            f"risk flags ({', '.join(risk)}) require interactive approval; "
            "--yes is not accepted for risk-flagged tasks",
        )
    echo(render_contract(task))
    markers = ", ".join(f"{ac}={m}" for ac, m in sorted(entry["criteria"].items()))
    echo(f"Ledger evidence: {markers or '(no criterion markers)'}")
    echo(f"Recorded status to set: {target}")
    if not assume_yes and not confirm(f"Approve {tid} and advance it to {target}?"):
        return ReconcileResult(tid, "skipped", "declined")
    try:
        # The entry's own echo/confirm above was the interactive step;
        # approve_task is called with its prompt suppressed.
        approve_task(
            queue, tid, approved_by=approved_by, assume_yes=True, echo=lambda _text: None
        )
        current = queue.get(tid).get("status")
        if current == "blocked" and target != "blocked":
            raise ApprovalError(
                "dependency not met ("
                + ", ".join(unmet_dependencies(task, queue.doc.get("tasks", [])))
                + "); reconcile entries in dependency order"
            )
        for step_status in _reconcile_path(target):
            queue.transition(tid, step_status)
    except ContractError as exc:
        return ReconcileResult(tid, "failed", str(exc))
    return ReconcileResult(tid, "reconciled", f"approved and advanced to {target}")


def reconcile_bootstrap(
    queue: TaskQueue,
    ledger_path: Path | str,
    *,
    approved_by: str,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] = _input_confirm,
    echo: Callable[[str], None] = print,
) -> list:
    """Approve and advance the tasks named by the bootstrap ledger (AC-4).

    For each ledger entry, in file order: print the contract and the
    entry's evidence markers, ask for confirmation (``assume_yes`` is
    subject to the same AC-3 rule as plain approval), approve the task,
    then walk it forward one legal transition at a time to its recorded
    status. A recorded ``done`` additionally requires every acceptance
    criterion of the task to be marked ``proven`` in the entry —
    pending, not met, or unmentioned criteria can never produce ``done``.
    Entries whose task is no longer draft are skipped, so re-running
    after a partial reconcile is safe. One entry's failure never stops
    the others; every outcome is reported in the returned summary.

    Args:
        queue: The queue to write through.
        ledger_path: Path to ``.factory/BOOTSTRAP.md``.
        approved_by: Name recorded as the approver of every entry.
        assume_yes: Skip the per-entry prompts for non-risk-flagged tasks.
        confirm: Interactive confirmation callable; injected in tests.
        echo: Output callable for contracts and summaries; injected in
            tests.

    Returns:
        One :class:`ReconcileResult` per entry, in ledger order; action
        is ``reconciled``, ``skipped`` or ``failed``.

    Raises:
        ApprovalError: If the ledger itself cannot be read.
    """
    try:
        text = Path(ledger_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ApprovalError(
            f"{ledger_path}: cannot read bootstrap ledger: {exc}"
        ) from exc
    return [
        _reconcile_entry(queue, entry, approved_by, assume_yes, confirm, echo)
        for entry in parse_bootstrap_ledger(text)
    ]
