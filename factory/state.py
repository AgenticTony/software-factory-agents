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
``done`` task is never auto-demoted — merged, live-verified work is
flagged for your review instead, and reopening it (``done -> draft``)
stays a human decision.

``not_buildable`` is set by the importer at task-creation time (§3), not
by an orchestrator transition, so it has no outgoing edges here.
"""

import contextlib
import copy
import fcntl
import json
import os
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
    """Write ``doc`` to ``path`` atomically: temp file, then rename.

    The temp file lives in the same directory so the rename never crosses
    filesystems, and non-ASCII characters are written literally so the
    queue stays review-friendly in diffs. Any failure removes the temp
    file and re-raises; the previous contents of ``path`` are untouched
    either way.

    Args:
        path: Destination file.
        doc: Document to write.

    Raises:
        OSError: If writing or renaming fails (the destination is intact).
    """
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _check_attempts_append_only(old_doc: dict, new_doc: dict) -> None:
    """Refuse any change to ``attempts`` other than appending (§2).

    Every task present in both documents must keep its old attempts as an
    exact prefix of the new list; a task that disappears must have had no
    attempts, or its log would be lost.

    Args:
        old_doc: The queue as it currently is on disk.
        new_doc: The queue about to be written.

    Raises:
        StateError: If an existing attempt entry would be modified,
            removed, or dropped along with its task.
    """
    old_attempts = {
        task.get("id"): task.get("attempts", [])
        for task in old_doc.get("tasks", [])
        if isinstance(task, dict)
    }
    for task in new_doc.get("tasks", []):
        if not isinstance(task, dict):
            continue
        old = old_attempts.pop(task.get("id"), None)
        if old is None:
            continue
        new = task.get("attempts", [])
        if new[: len(old)] != old:
            raise StateError(
                f"{task.get('id')}: attempts is append-only; existing entries "
                "cannot be modified or removed (TASK-CONTRACT §2)"
            )
    for task_id, attempts in old_attempts.items():
        if attempts:
            raise StateError(
                f"{task_id}: task removed from the queue, which would drop "
                "its append-only attempts log"
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
                or owner-step state contradicts it.
            ContractError: If the resulting queue wouldn't validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        task = self.get(task_id)
        check_transition(task.get("status"), new_status)
        _check_kind_rules(task, task.get("status"), new_status)
        _check_slice_rules(task, new_status)
        _check_owner_step_rules(task, new_status)
        task["status"] = new_status
        self.save()

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
        can run again. A ``done`` task is never demoted — merged,
        live-verified work is returned in ``flagged_done`` for your
        review, and reopening it stays your decision. The stale hash and
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
            holder: Recorded in the lock file, for diagnostics.

        Raises:
            RunInProgressError: If the lease is already held; the message
                names the holder.
        """
        lease_path = self.path.with_name(self.path.name + ".run.lock")
        with lease_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                current = lease_path.read_text().strip() or "unknown"
                raise RunInProgressError(
                    f"{self.path}: a run is in progress (lease held by {current}); "
                    "one writer task per repo"
                ) from exc
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(holder)
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
