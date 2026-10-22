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
import re
import subprocess
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path

from factory.contract import NON_EXECUTABLE_KINDS, ContractError, acceptance_hash
from factory.mergeorder import gh_pr_state
from factory.publish import PublishError
from factory.state import (
    TaskQueue,
    now_iso,
    open_owner_steps,
    recovery_task_eligible,
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


def render_contract(task: dict) -> str:
    """Render the contract a human is about to approve.

    Everything the approval decision depends on, nothing else: identity,
    risk, dependencies, the acceptance items with their checks, owner
    steps and slices. Open owner steps list first — they cap how far the
    task can ever be advanced.

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
            task.get("kind"),
            task.get("priority"),
            budget.get("size", "?"),
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


def _check_approvable(task: dict) -> None:
    """Refusals that apply to any approval, single or reconcile.

    Args:
        task: The task record.

    Raises:
        ApprovalError: When the task is not a draft, is of a
            non-executable kind, or has no acceptance items (AC-1).
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
) -> str:
    """Print a draft task's contract, confirm, freeze it, and route it.

    Refused — :class:`ApprovalError`, nothing written — when the task is
    not a draft, is of a non-executable kind, has no acceptance items
    (AC-1), or is risk-flagged while ``assume_yes`` is set (AC-3). On
    confirmation the contract is frozen (``acceptance_hash``,
    ``approved_at``, ``approved_by``) and the task moves
    ``draft -> ready`` or ``draft -> blocked`` per the picker's
    dependency rule (AC-2) in a single validated save.

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
        ApprovalDeclined: When the human answers no (or the prompt hits
            EOF).
        ContractError: If the resulting queue wouldn't validate.
    """
    task = queue.get(task_id)  # StateError (a ContractError) if unknown
    _check_approvable(task)
    risk = task.get("risk") or []
    if assume_yes and risk:
        raise ApprovalError(
            f"{task_id}: risk flags ({', '.join(risk)}) require interactive "
            "approval; --yes is not accepted for risk-flagged tasks"
        )
    echo(render_contract(task))
    if not assume_yes and not confirm(f"Approve {task_id}?"):
        raise ApprovalDeclined(f"{task_id}: approval declined")
    _freeze_contract(task, approved_by)
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
    pr_state: Callable[[str], str] | None = None,
    confirm: Callable[[str], bool] = input_confirm,
    echo: Callable[[str], None] = print,
) -> str:
    """Queue a fix round for a parked escalation (F-26 round-5 finding 2).

    ``factory recover`` is the owner's explicit fix-round word — the one
    recovery intent GitHub cannot express for a ``pr_closed`` task: its
    PR was already closed at escalation, so no later GitHub state
    distinguishes "re-open it" from "reject it". The sweep reads what
    GitHub can say (a re-opened or merged PR goes back to ``in_review``;
    a closed ``stale_base`` PR re-queues ``ready`` on its own); this
    command writes the gated ``needs_human → ready`` edge directly.

    Refused — :class:`ApprovalError`, nothing written — when the task
    isn't parked on a ``stale_base``/``pr_closed`` escalation holding a
    PR link (the same gate the sweep's transitions enforce), or when
    GitHub does not report the PR **closed** (round-6 finding 3): an
    open PR holds the concurrency slot, so queueing a fix round under
    it would park the task in an endless named ``concurrency_wait``.
    A dependency that regressed while the task was parked routes the
    move to ``blocked`` instead (round-6 finding 4), exactly as
    approval would.

    Args:
        queue: The queue to write through (its only writer path).
        task_id: Task to recover.
        assume_yes: Skip the confirmation prompt.
        pr_state: GitHub PR-state seam (default
            :func:`factory.mergeorder.gh_pr_state`); injected in tests.
        confirm: Interactive confirmation callable; injected in tests.
        echo: Output callable for the summary; injected in tests.

    Returns:
        The new status: ``ready`` or ``blocked``.

    Raises:
        ApprovalError: On any refusal, or an unknown task.
        ApprovalDeclined: When the human answers no (or the prompt hits
            EOF).
        ContractError: If the resulting queue wouldn't validate.
    """
    task = queue.get(task_id)  # StateError (a ContractError) if unknown
    if not recovery_task_eligible(task):
        raise ApprovalError(
            f"{task_id}: not recoverable — only a stale_base/pr_closed "
            "escalation holding a PR link can take the fix-round edge. "
            "Re-open the PR on GitHub (the sweep returns it to in_review), "
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
    echo(render_contract(task))
    markers = ", ".join(f"{ac}={m}" for ac, m in sorted(entry["criteria"].items()))
    echo(f"Ledger evidence: {markers or '(no criterion markers)'}")
    echo(f"Recorded status to set: {target}")
    if not confirm(f"Approve {tid} and advance it to {target}?"):
        return ReconcileResult(tid, "skipped", "declined")
    try:
        # One atomic save: freeze the contract, then move through the
        # whole validated path to the final status. A refusal anywhere
        # leaves the file untouched — no half-approved task.
        _check_approvable(task)
        _freeze_contract(task, approved_by)
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
) -> list:
    """Approve and advance the tasks named by the bootstrap ledger (AC-4).

    For each ledger entry, in file order: print the contract and the
    entry's evidence markers, confirm interactively (reconcile has no
    ``--yes`` — the ledger is plain markdown and this command turns it
    into ``done`` tasks), then freeze the contract and apply the entry's
    final status through :meth:`TaskQueue.transition_path` — the whole
    path validated as legal F-03 transitions, written in one atomic
    save, never persisting intermediate statuses. A recorded status that
    asserts the build passed validation (``in_review`` and beyond)
    requires every acceptance criterion to be marked ``proven``;
    pending, not met, or unmentioned criteria refuse the entry. A
    recorded ``done`` with owner steps still open reconciles to
    ``awaiting_owner``. Entries whose task is no longer draft are
    skipped, so re-running after a partial reconcile is safe. One
    entry's failure never stops the others; every outcome is reported in
    the returned summary.

    Args:
        queue: The queue to write through.
        ledger_path: Path to ``.factory/BOOTSTRAP.md``.
        approved_by: Name recorded as the approver of every entry.
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
        _reconcile_entry(queue, entry, approved_by, confirm, echo)
        for entry in parse_bootstrap_ledger(text)
    ]
