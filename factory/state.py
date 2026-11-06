"""Task state machine and picker (F-03).

TASK-CONTRACT §3 defines the task lifecycle as a diagram plus a recovery
and continuation edge table; this module turns both into an explicit
allowed-transition table (:data:`ALLOWED_TRANSITIONS`) and implements the
§4 picker (:func:`pick_next`).

The orchestrator is the only writer of ``tasks.json`` (§1 — agents never
write it), and every write goes through :class:`TaskQueue`, which

- refuses any transition not in the table,
- validates the whole queue (F-02) before writing, so invalid state is
  never persisted,
- only ever appends to ``attempts``, refusing rewrites, edits and
  removals (TASK-CONTRACT §2),
- writes atomically — temp file in the same directory, then rename — so
  a crash at any point leaves the previous file intact,
- serialises writers through a lock file and re-reads the queue under
  the lock, refusing to overwrite changes made by another writer
  (``factory loop`` and a human-run ``factory approve`` are two such
  writers).

The ``X -> draft`` edges exist for one reason only: the approved contract
changed (TASK-CONTRACT §2 — the recomputed acceptance hash no longer
matches ``acceptance_hash``). :meth:`TaskQueue.demote_changed_contracts`
is their intended caller; the task then needs human re-approval (F-04)
before it can run again, so a demotion can never auto-retry work. A
``done`` task is never auto-demoted — merged work (live-verified where
the profile has a deploy target; the merge is the evidence on a
no-deploy profile) is flagged for your review instead, and reopening
it (``done -> draft``) stays a human decision.

``not_buildable`` is set by the importer at task-creation time (§3), not
by an orchestrator transition, so it has no outgoing edges here.
"""

import contextlib
import copy
import fcntl
import getpass
import json
import os
import re
import uuid
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from factory.contract import (
    NON_EXECUTABLE_KINDS,
    POST_APPROVAL_STATUSES,
    ContractError,
    acceptance_hash,
    canonical_json,
    load_tasks,
    validate_queue,
)

# All task statuses. MIRRORS the schema's status enum
# (schema/tasks.schema.json $defs/status); keep both in sync.
STATUSES = frozenset(
    {
        "draft",
        "blocked",
        "ready",
        "in_progress",
        "in_review",
        "deploying",
        "awaiting_owner",
        "needs_human",
        "done",
        "not_buildable",
    }
)

# The TASK-CONTRACT §3 diagram plus its recovery/continuation edge table,
# as one explicit table. (from, to) -> why the transition is legal.
ALLOWED_TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "ready"): "spec + contract approved, dependencies met",
    ("draft", "blocked"): "spec + contract approved, a dependency is open",
    ("blocked", "ready"): "dependencies met",
    ("blocked", "draft"): "contract changed",
    ("ready", "in_progress"): "orchestrator picked the task",
    ("ready", "blocked"): "a dependency regressed",
    ("ready", "draft"): "contract changed",
    ("in_progress", "in_review"): "validator L0-L3 passed, PR open",
    ("in_progress", "needs_human"): "escalated (attempts/ambiguity/risk)",
    ("in_progress", "ready"): "run aborted with no PR/outcome (crash recovery)",
    ("in_progress", "draft"): "contract changed",
    ("in_review", "deploying"): "PR merged",
    ("in_review", "in_progress"): "you rejected the PR; fix round",
    ("in_review", "needs_human"): "stale base failed re-validation on the new default head (F-26)",
    ("in_review", "draft"): "contract changed",
    ("deploying", "needs_human"): "L4 live check failed (reverted)",
    ("deploying", "awaiting_owner"): "L4 passed, owner steps still open",
    ("deploying", "done"): "L4 passed, no open owner steps",
    ("deploying", "ready"): "previous slice merged; more slices remain",
    ("deploying", "draft"): "contract changed",
    ("awaiting_owner", "done"): "owner steps ticked",
    ("draft", "not_buildable"): "non-executable kind surfaced to the owner (F-29)",
    ("not_buildable", "done"): "owner-only task: every owner step ticked (F-29)",
    ("awaiting_owner", "draft"): "contract changed",
    ("needs_human", "draft"): "contract changed",
    ("done", "draft"): "contract changed (human reopen)",
}
# F-26 recovery edges (rounds 4+5) sit OUTSIDE the general table on
# purpose: the table is status-pair-only, and these edges are legal only
# for a task whose last attempt escalated stale_base/pr_closed and which
# still holds links.pr. The writers are the stale-PR sweep — which
# reads the owner's GitHub actions: a re-opened (or merged) pr_closed /
# stale_base PR → in_review; a closed stale_base PR was the rejection →
# ready, directly, with no lease-less in_progress hop (round-5 finding
# 3) — and `factory recover`, the owner's explicit fix-round word for a
# pr_closed task (round-5 finding 2): its PR was already closed at
# escalation, so GitHub has no signal left to distinguish re-open from
# reject. The one exception to "no other escalation": credential_added
# (F-31 run blocker) may exit through the needs_human → ready/blocked
# edge ONLY via TaskQueue.recover_credential — the dedicated state-layer
# writer `factory recover --evidence …` drives with an interactive y/N
# (no --yes), TASK-CONTRACT's "a human confirms and re-queues". The
# generic transition refuses credential tasks (check_recovery_transition
# is stale_base/pr_closed only), the sweep never writes the edge, and no
# other needs_human task (protected_path_touched, attempts_exhausted, …)
# may exit through them.
RECOVERY_ESCALATIONS = ("stale_base", "pr_closed")
CREDENTIAL_RECOVERY_ESCALATIONS = ("credential_added",)
RECOVERY_TRANSITIONS: dict[tuple[str, str], str] = {
    ("needs_human", "in_review"):
        "owner re-opened (or merged) the escalated PR (F-26 recovery, "
        "sweep-applied)",
    ("needs_human", "ready"):
        "owner rejected the escalated PR, or confirmed a credential "
        "escalation was a false positive / rotated (F-26 recovery, "
        "sweep- or recover-applied)",
    ("needs_human", "blocked"):
        "the recovery fix round queued, but a dependency is open — same "
        "routing approval uses (F-26 recovery, round-6 finding 4)",
}

LEGAL_TARGETS: dict[str, frozenset[str]] = {
    status: frozenset(to for (frm, to) in ALLOWED_TRANSITIONS if frm == status)
    for status in STATUSES
}

# §4: only build and docs are executed.
ELIGIBLE_KINDS = frozenset({"build", "docs"})

# §2: P1 is the highest priority; ties break by queue order.
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}

# Statuses that imply a frozen contract: the schema requires the hash for
# POST_APPROVAL_STATUSES, and blocked / needs_human only arise after
# approval. A hash mismatch in any of these sends the task back to draft —
# except done, which is flagged for review instead (see module docstring).
CONTRACT_FROZEN_STATUSES = frozenset(POST_APPROVAL_STATUSES | {"blocked", "needs_human"})

# Outcome of a contract audit: which tasks were demoted to draft, and
# which done tasks have a stale hash and need your review.
ContractAudit = namedtuple("ContractAudit", ["demoted", "flagged_done"])


class StateError(ContractError):
    """Invalid queue-state operation (unknown task, append-only or
    concurrency violation). Subclasses ContractError so callers that
    catch contract problems also catch state problems."""


class TransitionError(StateError):
    """A status transition not in the allowed-transition table."""


class ConcurrentWriteError(StateError):
    """tasks.json changed on disk since this queue was loaded."""


class RunInProgressError(StateError):
    """Another session holds the run lease for this queue."""


def check_transition(current: str, new: str) -> str:
    """Validate one status transition against the table.

    Args:
        current: The task's current status.
        new: The requested status.

    Returns:
        The reason the transition is legal.

    Raises:
        TransitionError: If ``(current, new)`` is not in the table; the
            message names both statuses and the legal targets.
    """
    reason = ALLOWED_TRANSITIONS.get((current, new))
    if reason is None:
        targets = ", ".join(sorted(LEGAL_TARGETS.get(current, frozenset()))) or "none"
        raise TransitionError(
            f"illegal transition {current} -> {new}; allowed from {current}: {targets}"
        )
    return reason


def recovery_task_eligible(task: dict) -> bool:
    """Whether the F-26 recovery edges may move this task (round-4 f2).

    True only for a ``needs_human`` task whose last attempt escalated
    ``stale_base``/``pr_closed`` and which still holds ``links.pr`` —
    the sweep and the transition gate share this one predicate, so the
    writer can never widen the edges on its own.
    """
    attempts = task.get("attempts") or []
    return (task.get("status") == "needs_human"
            and bool(attempts)
            and attempts[-1].get("escalation") in RECOVERY_ESCALATIONS
            and bool((task.get("links") or {}).get("pr")))


def credential_recovery_eligible(task: dict) -> bool:
    """Whether the owner's credential re-queue word may move this task.

    True only for a ``needs_human`` task whose last attempt escalated
    ``credential_added`` — the F-25 sentinel's false-positive/false-negative
    trap has no GitHub signal (nothing was pushed), so the only honest
    exit is a human confirming with evidence. The sweep never consults
    this predicate: it is `factory recover`'s gate alone.
    """
    attempts = task.get("attempts") or []
    return (task.get("status") == "needs_human"
            and bool(attempts)
            and attempts[-1].get("escalation") in CREDENTIAL_RECOVERY_ESCALATIONS)


ATTEMPTS_RESET_ESCALATIONS = ("attempts_exhausted",)


def attempts_reset_eligible(task: dict) -> bool:
    """Whether the owner's budget-reset word may move this task.

    True only for a ``needs_human`` task whose last attempt escalated
    ``attempts_exhausted``. Before the F-31 fix round that escalation had
    no exit at all — the attempts are genuinely spent, so a plain re-queue
    would park the task straight back; the only honest exit is a human
    granting a fresh budget with evidence (three F-31 attempts died to a
    sandbox bug the tasks could never fix). The sweep never consults this
    predicate: it is `factory recover --reset-attempts`'s gate alone.
    """
    attempts = task.get("attempts") or []
    return (task.get("status") == "needs_human"
            and bool(attempts)
            and attempts[-1].get("escalation") in ATTEMPTS_RESET_ESCALATIONS)


def attempt_budget_floor(task: dict, slice_id: str) -> int:
    """The position in ``task['attempts']`` the slice's budget counts from.

    The latest ``attempt_resets`` marker for ``slice_id`` names how many
    attempts existed when the budget was reset; attempts are append-only,
    so that position stays valid. 0 (the whole log counts) when the slice
    was never reset.
    """
    floor = 0
    for marker in task.get("attempt_resets") or []:
        if marker.get("slice_id") == slice_id:
            floor = max(floor, int(marker.get("prior_attempts") or 0))
    return floor


def attempt_budget_used(task: dict, slice_id: str) -> int:
    """How many attempts of ``slice_id``'s budget are spent.

    The orchestrator's one budget filter: same-slice attempts, not
    aborted, not session-less sweep escalations (round-5 finding 1), and
    — after a reset — only those at or after the reset floor. Every
    budget-relative number downstream (the run's attempt numbering, the
    implementer prompt's "attempt N of M", the PR body's attempt line)
    flows from this count.
    """
    floor = attempt_budget_floor(task, slice_id)
    return sum(
        1 for i, a in enumerate(task.get("attempts") or [])
        if i >= floor
        and a.get("slice_id") == slice_id
        and a.get("outcome") != "aborted"
        and a.get("escalation") not in RECOVERY_ESCALATIONS
    )


def check_recovery_transition(task: dict, new: str) -> str:
    """Validate one F-26 recovery transition for this specific task.

    The stale_base/pr_closed edges only (sweep or ``factory recover``).
    The ``credential_added`` re-queue is NOT one of them: it refuses
    here and moves only through :meth:`TaskQueue.recover_credential`,
    the dedicated state-layer writer that demands the owner's evidence
    (review round on #74: a gate that lived only in the command left
    every other ``transition`` caller able to move a credential task).

    Raises:
        TransitionError: If the edge isn't a recovery edge, or the task
            isn't a stale_base/pr_closed escalation holding a PR link.
    """
    reason = RECOVERY_TRANSITIONS.get((task.get("status"), new))
    if reason is None or not recovery_task_eligible(task):
        raise TransitionError(
            f"recovery transition {task.get('status')} -> {new} needs a "
            "stale_base/pr_closed escalation and a PR link (F-26; a "
            "credential_added task moves only through "
            "TaskQueue.recover_credential, and an attempts_exhausted task "
            "only through TaskQueue.recover_attempts)"
        )
    return reason


@dataclass(frozen=True)
class Pick:
    """What the picker hands a session: the task, and its slice if sliced.

    ``slice`` is None for an unsliced task (the whole task is the unit);
    otherwise it is the task's first slice that isn't merged.
    """

    task: dict
    slice: dict | None


def pick_next(tasks: list) -> Pick | None:
    """Pick the next task to work on (TASK-CONTRACT §4).

    eligible = status == ready
               and kind in {build, docs}
               and every depends_on is done
               and no other writer task in_progress for this repo

    v0 allows one writer task per repo, and one queue is one repo, so any
    ``in_progress`` task blocks the whole queue. Eligible tasks are ordered
    by priority (P1 first), ties by position in the queue; the pick is the
    first eligible task's first incomplete slice.

    Args:
        tasks: The queue's ``tasks`` array (validated by F-02 on load).

    Returns:
        The pick, or None when nothing is eligible.
    """
    if any(task.get("status") == "in_progress" for task in tasks):
        return None
    by_id = {task.get("id"): task for task in tasks}
    eligible = sorted(
        (
            task
            for task in tasks
            if task.get("status") == "ready"
            and task.get("kind") in ELIGIBLE_KINDS
            and _deps_met(task, by_id)
        ),
        key=lambda task: PRIORITY_ORDER.get(task.get("priority"), len(PRIORITY_ORDER)),
    )  # sorted() is stable, so equal priorities keep queue order
    for task in eligible:
        slices = task.get("slices") or []
        if not slices:
            return Pick(task=task, slice=None)
        for slice_ in slices:
            if slice_.get("status") != "merged":
                return Pick(task=task, slice=slice_)
        # every slice merged: not runnable — fall through to the next task
    return None


def _unmerged_slices(task: dict) -> list:
    """Return the task's slices that aren't merged yet."""
    return [s for s in (task.get("slices") or []) if s.get("status") != "merged"]


def _unmet_by_id(task: dict, by_id: dict) -> list:
    """The task's open dependency ids against a prebuilt {id: task} index.

    Implements the TASK-CONTRACT §2/§4 dependency rule: a ``repo:id``
    dependency (anything with a colon) is cross-repo and is never met
    automatically — a human must mark it met; a same-repo dependency is
    met only when the referenced task is ``done`` in this queue; a
    dependency on an id not in the queue counts as unmet.

    Args:
        task: The task whose dependencies to check.
        by_id: Queue index {id: task}, built once by the caller.
    """
    open_: list = []
    for dep in task.get("depends_on", []):
        if not isinstance(dep, str) or ":" in dep:
            open_.append(dep)
            continue
        target = by_id.get(dep)
        if target is None or target.get("status") != "done":
            open_.append(dep)
    return open_


def _deps_met(task: dict, by_id: dict) -> bool:
    """True when every ``depends_on`` is met (see ``_unmet_by_id``)."""
    return not _unmet_by_id(task, by_id)


def unmet_dependencies(task: dict, tasks: list) -> list:
    """The task's open dependencies against a plain task list.

    Public read-only view of the picker's dependency rule, used by F-04
    to route approval to ``ready`` or ``blocked`` and to name what a
    task is waiting on.

    Args:
        task: The task whose dependencies to check.
        tasks: The queue's ``tasks`` array.

    Returns:
        The unmet dependency ids, in declared order.
    """
    return _unmet_by_id(task, {t.get("id"): t for t in tasks})


def open_owner_steps(task: dict) -> list:
    """The task's owner steps that aren't ticked yet (TASK-CONTRACT §2).

    Args:
        task: The task record.

    Returns:
        The open ``owner_steps`` entries, in declared order.
    """
    return [step for step in (task.get("owner_steps") or []) if not step.get("done")]


def now_iso() -> str:
    """UTC now in the queue's timestamp style (second precision, Z).

    Public so every writer stamps timestamps identically (F-04 approval
    fields, queue ``updated_at``).
    """
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )



def _atomic_write_json(path: Path, doc: dict) -> None:
    """Write ``doc`` to ``path`` atomically: temp file, then rename, then
    fsync of the parent DIRECTORY.

    The temp file lives in the same directory so the rename never crosses
    filesystems, and non-ASCII characters are written literally so the
    queue stays review-friendly in diffs. The file is fsynced before the
    rename, and the parent directory is fsynced AFTER it (F-45 AC-6, I-3):
    a rename is only durable once the directory entry that records it is
    on disk — without the dir fsync, a power loss between the rename and
    the next incidental flush can lose the write (the old file comes
    back), which is a crash-consistency hole in a file the whole state
    machine rides on. Any failure removes the temp file and re-raises; the
    previous contents of ``path`` are untouched up to and including a
    failed rename (a failed dir fsync leaves the NEW contents in place —
    written, just not yet proven durable — and still re-raises so the
    caller hears about it).

    Args:
        path: Destination file.
        doc: Document to write.

    Raises:
        OSError: If writing, renaming or fsyncing fails (up to the rename,
            the destination is intact).
    """
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _check_attempts_append_only(old_doc: dict, new_doc: dict) -> None:
    """Refuse any change to ``attempts``/``attempt_resets`` other than
    appending (§2).

    Every task present in both documents must keep its old attempts (and
    reset markers) as an exact prefix of the new list; a task that
    disappears must have had none, or its log would be lost.

    Args:
        old_doc: The queue as it currently is on disk.
        new_doc: The queue about to be written.

    Raises:
        StateError: If an existing attempt or reset entry would be
            modified, removed, or dropped along with its task.
    """
    old_logs = {
        task.get("id"): {
            "attempts": task.get("attempts", []),
            "attempt_resets": task.get("attempt_resets", []),
        }
        for task in old_doc.get("tasks", [])
        if isinstance(task, dict)
    }
    for task in new_doc.get("tasks", []):
        if not isinstance(task, dict):
            continue
        old = old_logs.pop(task.get("id"), None)
        if old is None:
            continue
        for field in ("attempts", "attempt_resets"):
            kept, grown = old[field], task.get(field, [])
            if grown[: len(kept)] != kept:
                why = ("(TASK-CONTRACT §2)" if field == "attempts" else
                       "— a removed reset marker would silently re-spend a "
                       "budget the owner granted")
                raise StateError(
                    f"{task.get('id')}: {field} is append-only; existing "
                    f"entries cannot be modified or removed {why}"
                )
    for task_id, logs in old_logs.items():
        for field in ("attempts", "attempt_resets"):
            if logs[field]:
                raise StateError(
                    f"{task_id}: task removed from the queue, which would "
                    f"drop its append-only {field} log"
                )


def _check_slice_rules(task: dict, new_status: str) -> None:
    """Enforce the slicing rules on task-level transitions.

    ``done`` and ``awaiting_owner`` mean the whole task is finished, so
    every slice must be merged first. ``deploying -> ready`` is slice
    continuation, so it requires at least one unmerged slice; owner-step
    and ``done`` evaluation waits until the last slice merges.

    Args:
        task: The task record.
        new_status: The requested status.

    Raises:
        TransitionError: If the slice state contradicts the transition.
    """
    tid = task.get("id")
    unmerged = _unmerged_slices(task)
    if new_status in ("done", "awaiting_owner") and unmerged:
        ids = ", ".join(s.get("id", "?") for s in unmerged)
        raise TransitionError(
            f"{tid}: slice(s) not merged: {ids}; the task cannot be "
            f"{new_status} until every slice is merged"
        )
    if new_status == "ready" and task.get("status") == "deploying" and not unmerged:
        raise TransitionError(
            f"{tid}: no unmerged slices remain; deploying must resolve to "
            "done, awaiting_owner or needs_human, not ready"
        )


def check_tick(task: dict, step_numbers: list, *, evidence: str, done_by: str) -> None:
    """Refusals for ticking owner steps, checked before anything changes (F-29).

    Args:
        task: The task record.
        step_numbers: 1-based owner-step numbers.
        evidence: What the owner checked.
        done_by: Who is ticking.

    Raises:
        StateError: Blank evidence or done_by, no steps, a duplicate step,
            a step out of range, or a step already ticked.
    """
    task_id = task.get("id")
    steps = task.get("owner_steps") or []
    if not evidence or not evidence.strip():
        raise StateError(f"{task_id}: evidence must say what you checked; it is blank")
    if not done_by or not done_by.strip():
        raise StateError(f"{task_id}: done_by is blank")
    if not step_numbers:
        raise StateError(f"{task_id}: name at least one owner step to tick")
    if len(set(step_numbers)) != len(step_numbers):
        raise StateError(f"{task_id}: a step is named twice")
    for number in step_numbers:
        if not 1 <= number <= len(steps):
            raise StateError(
                f"{task_id}: owner step {number} is out of range "
                f"(the task has {len(steps)})"
            )
        if steps[number - 1].get("done"):
            raise StateError(f"{task_id}: owner step {number} is already ticked")


def _check_kind_rules(task: dict, current: str, new_status: str) -> None:
    """Guard the two edges that only non-executable kinds may take (F-29).

    ``draft -> not_buildable`` is how a decision, research or owner-only
    task is parked as "surfaced to you, never run"; an executable kind
    (``build``, ``docs``) parked there could never be picked. And
    ``not_buildable -> done`` closes an owner-only task, whose whole
    content is its owner steps — so it needs at least one step, all
    ticked. Without the guard a build task could skip validation
    entirely by going ``draft -> not_buildable -> done``.

    Args:
        task: The task record.
        current: The status the edge starts from.
        new_status: The status the edge goes to.

    Raises:
        TransitionError: If the task's kind or owner steps forbid the edge.
    """
    tid = task.get("id")
    kind = task.get("kind")
    if new_status == "not_buildable" and kind not in NON_EXECUTABLE_KINDS:
        raise TransitionError(
            f"{tid}: kind={kind} is executable; only decision, research or "
            "owner_only tasks may be not_buildable"
        )
    if current == "not_buildable" and new_status == "done":
        if kind != "owner_only":
            raise TransitionError(
                f"{tid}: not_buildable -> done closes owner_only tasks only; "
                f"this one is kind={kind}"
            )
        if not task.get("owner_steps"):
            raise TransitionError(
                f"{tid}: an owner_only task needs at least one owner step to "
                "be done — there is nothing to show it was"
            )


def _check_owner_step_rules(task: dict, new_status: str) -> None:
    """Enforce the owner-step semantics of ``done`` / ``awaiting_owner``.

    TASK-CONTRACT §3: ``deploying -> awaiting_owner`` is "L4 passed,
    owner steps still open", ``deploying -> done`` is "L4 passed, no
    open owner steps", and ``awaiting_owner -> done`` is "owner steps
    ticked" — so ``done`` asserts every step is ticked, and
    ``awaiting_owner`` exists only while at least one is open (with none
    open it would wait on nothing; the task resolves to ``done``).

    Args:
        task: The task record.
        new_status: The requested status.

    Raises:
        TransitionError: If the owner-step state contradicts the status.
    """
    tid = task.get("id")
    open_steps = open_owner_steps(task)
    if new_status == "done" and open_steps:
        texts = "; ".join(step.get("text", "?") for step in open_steps)
        raise TransitionError(
            f"{tid}: owner step(s) still open: {texts}; the task is "
            "awaiting_owner until you tick them, never done"
        )
    if new_status == "awaiting_owner" and not open_steps:
        raise TransitionError(
            f"{tid}: no owner steps are open; awaiting_owner would wait on "
            "nothing — resolve the task to done instead"
        )


def run_lease_held(queue_path: Path | str) -> bool:
    """Whether a live process holds the run lease of this queue (F-30).

    The lease is an exclusive ``flock`` on ``<queue>.run.lock`` that the OS
    drops the moment its holder dies, so this is the liveness signal a
    killed run can't fake (a left-behind run dir can). The probe opens the
    lock file read-only — never creating it, never writing the holder
    line — and tries a non-blocking *shared* lock: refused means an
    exclusive holder is alive. On success the shared lock is released at
    once; for those few microseconds a ``begin_run`` racing the probe would
    see the lease busy and refuse with :class:`RunInProgressError` (its
    normal, retryable message), which is why readers call this only while
    a trace is idle, never in a hot loop.

    Args:
        queue_path: The queue file whose lease to probe.

    Returns:
        True if the lease is held; False if it is free or the lock file
        doesn't exist (no run ever took it).
    """
    path = Path(queue_path)
    lock_path = path.with_name(path.name + ".run.lock")
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# F-45 AC-7 (I-4): the lease holder line carries the holder's pid
# (``<holder> pid=<pid>``) so a contender can ask the kernel whether that
# process is alive before naming it as the holder.
_PID_IN_LEASE = re.compile(r"\bpid=(\d+)\b")


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process, per the kernel.

    ``os.kill(pid, 0)`` sends no signal — it only probes. EPERM means the
    process EXISTS and belongs to another user: alive is alive, whoever
    owns it. Any other OSError conservatively reads as not alive (the
    error message must then never present the pid as the holder).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lease_busy_message(queue_path: Path, holder_line: str) -> str:
    """The RunInProgressError text, naming the holder only when alive.

    The flock is held by definition (the kernel drops it when its holder
    dies), so the FILE's content is the only thing that can be stale: a
    previous holder's leftover line (this repo's own run.lock carried
    ``reclaim`` for weeks) or the acquire/write race of the current one.
    A line whose pid is alive names the holder; anything else — a dead
    pid, pre-pid content, garbage, an empty file — is reported as stale,
    never presented as the holder (I-4).
    """
    match = _PID_IN_LEASE.search(holder_line) if holder_line else None
    if match and _pid_alive(int(match.group(1))):
        return (
            f"{queue_path}: a run is in progress (lease held by "
            f"{holder_line}); one writer task per repo"
        )
    shown = holder_line or "(empty)"
    return (
        f"{queue_path}: a run is in progress, but the lease file's holder "
        f"line is stale ({shown!r}) — it names no live process, so the "
        "live holder cannot be named; one writer task per repo"
    )


class TaskQueue:
    """The orchestrator's handle on a ``tasks.json`` — its only writer.

    Loads through F-02 (schema + semantics) on construction. Every mutation
    (``transition``, ``append_attempt``, ``demote_changed_contracts``)
    validates the resulting queue and writes it atomically under an
    exclusive lock, re-reading the file first and refusing if another
    writer changed it since this instance loaded. A failed write rolls the
    in-memory document back to what is on disk, so the instance never
    holds state that outlives a refused save.
    """

    def __init__(self, path: Path | str, schema_path: Path | None = None) -> None:
        """Load and validate the queue.

        Args:
            path: Path to the queue file (e.g. ``.factory/tasks.json``).
            schema_path: Optional override for the tasks schema path.

        Raises:
            ContractError: If the file is unreadable or invalid (F-02).
        """
        self.path = Path(path)
        self._schema_path = schema_path
        self.doc = load_tasks(self.path, schema_path)
        self._disk_doc = copy.deepcopy(self.doc)

    def get(self, task_id: str) -> dict:
        """Return the live task record.

        Args:
            task_id: Task id.

        Returns:
            The task dict; mutations are visible to ``save``.

        Raises:
            StateError: If no task has that id.
        """
        for task in self.doc.get("tasks", []):
            if task.get("id") == task_id:
                return task
        raise StateError(f"unknown task: {task_id}")

    def transition(self, task_id: str, new_status: str) -> None:
        """Move a task to a new status through the allowed-transition table.

        Args:
            task_id: Task id.
            new_status: Requested status.

        Raises:
            StateError: If the task doesn't exist.
            TransitionError: If the edge isn't in the table, or the slice
                or owner-step state contradicts it — including a
                credential_added task asked to leave ``needs_human``
                (that edge is :meth:`recover_credential`'s alone).
            ContractError: If the resulting queue wouldn't validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        task = self.get(task_id)
        current = task.get("status")
        if (current, new_status) in RECOVERY_TRANSITIONS:
            # F-26 recovery edges: gated on the task, not just the pair
            # (round-4 finding 2) — only the sweep and `factory recover`
            # write them, and only for an eligible escalation.
            check_recovery_transition(task, new_status)
        else:
            check_transition(current, new_status)
        _check_kind_rules(task, task.get("status"), new_status)
        _check_slice_rules(task, new_status)
        _check_owner_step_rules(task, new_status)
        task["status"] = new_status
        self.save()

    def recover_credential(self, task_id: str, evidence: str) -> str:
        """Re-queue a ``credential_added`` task: the one writer of the
        ``needs_human → ready/blocked`` edge for that escalation.

        The credential exit is gated HERE, not in the command (review
        round on #74): the generic :meth:`transition` refuses a
        credential task, so no other writer can move it. Evidence is
        demanded again at this layer — the command already required it,
        and the state layer must hold on its own. Routing matches every
        recovery writer: ``blocked`` when a dependency is open, else
        ``ready``; attempts are never reset (the remaining budget is the
        owner's call, made with the finding named in the evidence).

        Args:
            task_id: Task parked on a ``credential_added`` escalation.
            evidence: What the owner checked; must not be blank.

        Returns:
            The new status: ``ready`` or ``blocked``.

        Raises:
            StateError: Unknown task, or blank evidence.
            TransitionError: The task isn't parked on a
                ``credential_added`` escalation (including via the
                generic transition, which never moves one).
            ContractError: If the resulting queue wouldn't validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        if not (evidence or "").strip():
            raise StateError(f"{task_id}: credential recovery requires evidence")
        task = self.get(task_id)
        if not credential_recovery_eligible(task):
            raise TransitionError(
                f"{task_id}: credential recovery needs a needs_human task "
                "whose last attempt escalated credential_added"
            )
        new_status = (
            "blocked" if unmet_dependencies(task, self.doc.get("tasks", []))
            else "ready"
        )
        reason = RECOVERY_TRANSITIONS.get((task.get("status"), new_status))
        assert reason is not None  # needs_human → ready/blocked is in the table
        _check_kind_rules(task, task.get("status"), new_status)
        _check_slice_rules(task, new_status)
        _check_owner_step_rules(task, new_status)
        task["status"] = new_status
        self.save()
        return new_status

    def recover_attempts(self, task_id: str, evidence: str, *,
                         done_by: str | None = None) -> str:
        """Re-queue an ``attempts_exhausted`` task with a fresh budget:
        the one writer of that edge, and the only writer of
        ``attempt_resets`` markers.

        Like the credential exit, the gate lives HERE, not in the command:
        the generic :meth:`transition` refuses an attempts_exhausted task,
        so no other writer can move it or spend a budget the owner never
        granted. Evidence is demanded again at this layer. History is
        never deleted — the marker records how many attempts existed
        (``prior_attempts``), and :func:`attempt_budget_used` counts only
        attempts at or after that position, so the fresh budget starts at
        zero while every old attempt record stays in the log and in the
        F-11 measures. Routing matches every recovery writer: ``blocked``
        when a dependency is open, else ``ready``.

        Args:
            task_id: Task parked on an ``attempts_exhausted`` escalation.
            evidence: What the owner checked; must not be blank.
            done_by: Who granted the reset; recorded in the marker.

        Returns:
            The new status: ``ready`` or ``blocked``.

        Raises:
            StateError: Unknown task, or blank evidence.
            TransitionError: The task isn't parked on
                ``attempts_exhausted`` (including via the generic
                transition, which never moves one).
            ContractError: If the resulting queue wouldn't validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        if not (evidence or "").strip():
            raise StateError(f"{task_id}: attempt reset requires evidence")
        task = self.get(task_id)
        if not attempts_reset_eligible(task):
            raise TransitionError(
                f"{task_id}: attempt reset needs a needs_human task whose "
                "last attempt escalated attempts_exhausted"
            )
        slice_id = task["attempts"][-1].get("slice_id") or task_id
        new_status = (
            "blocked" if unmet_dependencies(task, self.doc.get("tasks", []))
            else "ready"
        )
        reason = RECOVERY_TRANSITIONS.get((task.get("status"), new_status))
        assert reason is not None  # needs_human → ready/blocked is in the table
        _check_kind_rules(task, task.get("status"), new_status)
        _check_slice_rules(task, new_status)
        _check_owner_step_rules(task, new_status)
        task["status"] = new_status
        task.setdefault("attempt_resets", []).append({
            "at": now_iso(),
            "evidence": evidence.strip(),
            "done_by": (done_by or "").strip() or getpass.getuser(),
            "slice_id": slice_id,
            "prior_attempts": len(task.get("attempts") or []),
        })
        self.save()
        return new_status

    def transition_path(self, task_id: str, statuses: list) -> None:
        """Move a task through a sequence of legal transitions in one save.

        Every consecutive edge is validated against the table, and the
        slice and owner-step guards run on the final status, before
        anything is written; only the final status is persisted.
        Intermediate statuses in a path are history being replayed, not
        state to publish: persisting each would mean a full locked save
        per step, a crash window that strands a half-moved task, and a
        transient ``in_progress`` that never held the run lease.
        Reconcile (F-04) is the intended caller.

        Args:
            task_id: Task id.
            statuses: The statuses to move through, in order; the last
                one is the resulting status. Must be non-empty.

        Raises:
            StateError: If the task doesn't exist or ``statuses`` is empty.
            TransitionError: If any consecutive edge isn't in the table,
                or the guards refuse the final status.
            ContractError: If the resulting queue wouldn't validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        if not statuses:
            raise StateError(f"{task_id}: transition_path needs at least one status")
        task = self.get(task_id)
        current = task.get("status")
        for new_status in statuses:
            check_transition(current, new_status)
            _check_kind_rules(task, current, new_status)
            current = new_status
        _check_slice_rules(task, statuses[-1])
        _check_owner_step_rules(task, statuses[-1])
        task["status"] = statuses[-1]
        self.save()

    def tick_owner_steps(
        self, task_id: str, step_numbers: list, *, evidence: str, done_by: str
    ) -> str:
        """Tick owner steps and close the task if that was the last one (F-29).

        Every step is checked before anything changes, and the ticks plus
        any resulting status change go out in one save: a refusal on any
        step writes nothing. The last open step of an ``awaiting_owner``
        task moves it to ``done``; an ``owner_only`` task closes through
        ``draft -> not_buildable -> done``. A task in any other status
        records the tick and keeps its status.

        Args:
            task_id: Task id.
            step_numbers: 1-based owner-step numbers, in any order.
            evidence: What the owner checked; must not be blank.
            done_by: Who ticked the steps.

        Returns:
            The task's status after the save.

        Raises:
            StateError: Unknown task, step out of range, step already
                ticked, duplicate step, or blank evidence/done_by.
            TransitionError: If the closing transition is refused.
            ContractError: If the resulting queue wouldn't validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        task = self.get(task_id)
        check_tick(task, step_numbers, evidence=evidence, done_by=done_by)
        candidate = copy.deepcopy(task)
        stamp = now_iso()
        for number in step_numbers:
            candidate["owner_steps"][number - 1].update(
                done=True, done_at=stamp, done_by=done_by, evidence=evidence.strip()
            )
        current = candidate.get("status")
        path: list = []
        if not open_owner_steps(candidate):
            if current == "awaiting_owner":
                path = ["done"]
            elif candidate.get("kind") == "owner_only" and current in ("draft", "not_buildable"):
                path = ["not_buildable", "done"] if current == "draft" else ["done"]
        for new_status in path:
            check_transition(current, new_status)
            _check_kind_rules(candidate, current, new_status)
            current = new_status
        if path:
            _check_slice_rules(candidate, current)
            _check_owner_step_rules(candidate, current)
            candidate["status"] = current
        task.clear()
        task.update(candidate)
        self.save()
        return current

    def append_attempt(self, task_id: str, attempt: dict) -> None:
        """Append one attempt record to a task's append-only log.

        Args:
            task_id: Task id.
            attempt: Attempt record; must satisfy the schema's ``attempt``
                (``run_id``, ``slice_id``, ``started``, ``outcome``,
                ``prompt_versions``, ``skills``, …).

        Raises:
            StateError: If the task doesn't exist.
            ContractError: If the attempt or resulting queue wouldn't
                validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        task = self.get(task_id)
        task.setdefault("attempts", []).append(attempt)
        self.save()

    def demote_changed_contracts(self) -> ContractAudit:
        """Audit approved contracts against their stored hashes.

        A task in a contract-frozen status whose recomputed acceptance
        hash no longer matches its stored ``acceptance_hash`` goes back to
        ``draft`` through the contract-changed edge, and the demotion is
        persisted; the task then needs human re-approval (F-04) before it
        can run again. A ``done`` task is never demoted — merged work
        (live-verified where the profile has a deploy target; the merge
        is the evidence on a no-deploy profile) is returned in
        ``flagged_done`` for your review, and reopening it stays your
        decision. The stale hash and
        approval fields are left in place as history; re-approval
        overwrites them.

        Returns:
            ``ContractAudit(demoted, flagged_done)`` — task ids in queue
            order whose contract changed, split by what happened to them.
        """
        demoted: list[str] = []
        flagged_done: list[str] = []
        for task in self.doc.get("tasks", []):
            status = task.get("status")
            frozen_hash = task.get("acceptance_hash")
            if status not in CONTRACT_FROZEN_STATUSES or not frozen_hash:
                continue
            if acceptance_hash(task.get("acceptance", [])) != frozen_hash:
                if status == "done":
                    flagged_done.append(task.get("id"))
                    continue
                check_transition(status, "draft")
                task["status"] = "draft"
                demoted.append(task.get("id"))
        if demoted:
            self.save()
        return ContractAudit(demoted=demoted, flagged_done=flagged_done)

    @contextlib.contextmanager
    def begin_run(self, task_id: str):
        """Take the run lease and move a task to ``in_progress``.

        The run lease is an exclusive OS lock on ``<queue>.run.lock``,
        held for the whole session. The OS releases it the moment the
        holding process exits, crashed or not, so liveness never depends
        on timestamps. A session that dies leaves its task ``in_progress``
        with the lease free, which :meth:`reclaim_abandoned` detects.
        The lease is taken *before* the transition, so no window exists
        where a task is ``in_progress`` without a live holder.

        Args:
            task_id: Task to start (must be ``ready``).

        Yields:
            None. The lease is released when the block exits; the task's
            next status is the caller's to set.

        Raises:
            RunInProgressError: If another session holds the lease.
            StateError: If the task doesn't exist.
            TransitionError: If the task isn't ``ready``.
        """
        with self._run_lease(holder=task_id):
            self.transition(task_id, "in_progress")
            yield

    def reclaim_abandoned(self) -> list[str]:
        """Return ``in_progress`` tasks abandoned by a dead session to ``ready``.

        If the run lease is held, a session is alive and nothing is
        reclaimed, however long it has been running. If the lease is
        free, no session is alive, so every ``in_progress`` task was
        abandoned (crash, killed process, laptop sleep), including one that
        died before recording its first attempt. Each goes back to
        ``ready`` through the aborted-run edge and re-enters the queue
        under the same attempt budget.

        Returns:
            The ids of reclaimed tasks, in queue order; empty while a run
            is live.
        """
        try:
            lease = self._run_lease(holder="reclaim")
            lease.__enter__()
        except RunInProgressError:
            return []
        try:
            reclaimed: list[str] = []
            for task in self.doc.get("tasks", []):
                if task.get("status") == "in_progress":
                    check_transition("in_progress", "ready")
                    task["status"] = "ready"
                    reclaimed.append(task.get("id"))
            if reclaimed:
                self.save()
            return reclaimed
        finally:
            lease.__exit__(None, None, None)

    @contextlib.contextmanager
    def idle_lease(self):
        """Hold the run lease for orchestrator housekeeping between runs.

        While held, no session can start (``begin_run`` refuses), so work
        that must never race a live run — reclaiming orphaned run dirs
        (F-10) — is safe inside the block.

        Raises:
            RunInProgressError: If a live run holds the lease.
        """
        with self._run_lease(holder="housekeeping"):
            yield

    @contextlib.contextmanager
    def _run_lease(self, holder: str):
        """Hold the non-blocking exclusive run lease on ``<queue>.run.lock``.

        Args:
            holder: Recorded in the lock file (with this process's pid),
                for diagnostics.

        Raises:
            RunInProgressError: If the lease is already held; the message
                names the holder only when the recorded pid is alive and
                reports stale content otherwise (F-45 AC-7, I-4).
        """
        lease_path = self.path.with_name(self.path.name + ".run.lock")
        with lease_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunInProgressError(
                    _lease_busy_message(self.path, lease_path.read_text().strip())
                ) from exc
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(f"{holder} pid={os.getpid()}")
                handle.flush()
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def sync_readiness(self) -> list[str]:
        """Reconcile ``ready``/``blocked`` with dependencies.

        A ``ready`` task whose dependencies are no longer met (a
        dependency regressed — reopened or demoted) goes to ``blocked``;
        a ``blocked`` task whose dependencies are all met goes to
        ``ready``. Cross-repo dependencies are never met automatically,
        so a task blocked on one stays blocked. The picker re-checks
        dependencies anyway; this keeps the status truthful for the daily
        summary (F-11) and for you.

        Returns:
            The ids of tasks whose status changed, in queue order.
        """
        by_id = {task.get("id"): task for task in self.doc.get("tasks", [])}
        changed: list[str] = []
        for task in self.doc.get("tasks", []):
            status = task.get("status")
            if status not in ("ready", "blocked"):
                continue
            met = _deps_met(task, by_id)
            if status == "ready" and not met:
                check_transition("ready", "blocked")
                task["status"] = "blocked"
                changed.append(task.get("id"))
            elif status == "blocked" and met:
                check_transition("blocked", "ready")
                task["status"] = "ready"
                changed.append(task.get("id"))
        if changed:
            self.save()
        return changed

    def pick(self) -> Pick | None:
        """Pick the next task to work on (see :func:`pick_next`)."""
        return pick_next(self.doc.get("tasks", []))

    @contextlib.contextmanager
    def _exclusive_lock(self):
        """Hold an exclusive advisory lock on ``<queue>.lock``.

        The lock file is never renamed, so every cooperating writer
        serialises on it; ``tasks.json`` itself is replaced by rename and
        can't carry a stable lock.
        """
        lock_path = self.path.with_name(self.path.name + ".lock")
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self) -> None:
        """Validate and atomically write the queue.

        Stamps ``updated_at``, validates the whole document (F-02), then
        under an exclusive lock re-reads the file and refuses if another
        writer changed it since this instance loaded, checks that
        ``attempts`` only grew against the queue now on disk, and writes
        temp-file + rename. On any failure the in-memory document is
        rolled back to what is on disk, which the failure left intact.

        Raises:
            ContractError: If the new document wouldn't validate.
            StateError: If ``attempts`` shrank or was rewritten.
            ConcurrentWriteError: If the file changed on disk since this
                queue was loaded; reload and retry.
            OSError: If the write fails; the previous file is intact.
        """
        candidate = copy.deepcopy(self.doc)
        candidate["updated_at"] = now_iso()
        try:
            validate_queue(candidate, str(self.path), self._schema_path)
            with self._exclusive_lock():
                disk_now = load_tasks(self.path, self._schema_path)
                if canonical_json(disk_now) != canonical_json(self._disk_doc):
                    raise ConcurrentWriteError(
                        f"{self.path}: changed on disk since this queue was "
                        "loaded (another writer?); reload the queue and retry"
                    )
                _check_attempts_append_only(disk_now, candidate)
                _atomic_write_json(self.path, candidate)
        except BaseException:
            self.doc = copy.deepcopy(self._disk_doc)
            raise
        self.doc = candidate
        self._disk_doc = copy.deepcopy(candidate)
