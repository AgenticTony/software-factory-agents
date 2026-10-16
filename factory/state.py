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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factory.contract import (
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
    ("in_review", "draft"): "contract changed",
    ("deploying", "needs_human"): "L4 live check failed (reverted)",
    ("deploying", "awaiting_owner"): "L4 passed, owner steps still open",
    ("deploying", "done"): "L4 passed, no open owner steps",
    ("deploying", "ready"): "previous slice merged; more slices remain",
    ("deploying", "draft"): "contract changed",
    ("awaiting_owner", "done"): "owner steps ticked",
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


def _deps_met(task: dict, by_id: dict) -> bool:
    """Check ``depends_on`` against this queue (TASK-CONTRACT §2).

    A ``repo:id`` dependency (anything with a colon) is cross-repo and is
    never met automatically — a human must mark it met. A same-repo
    dependency is met only when the referenced task is ``done`` in this
    queue; a dependency on an id not in the queue counts as unmet.

    Args:
        task: The task whose dependencies to check.
        by_id: Queue index {id: task}, built once by the caller.
    """
    for dep in task.get("depends_on", []):
        if not isinstance(dep, str) or ":" in dep:
            return False
        target = by_id.get(dep)
        if target is None or target.get("status") != "done":
            return False
    return True


def _now_iso() -> str:
    """UTC now in the queue's timestamp style (second precision, Z)."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _parse_dt(value: str) -> datetime | None:
    """Parse an ISO-8601 date-time; naive values are read as UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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
                state contradicts it (see ``_check_slice_rules``).
            ContractError: If the resulting queue wouldn't validate.
            ConcurrentWriteError: If another writer changed the file.
        """
        task = self.get(task_id)
        check_transition(task.get("status"), new_status)
        _check_slice_rules(task, new_status)
        task["status"] = new_status
        self.save()

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

    def reclaim_abandoned(self, now: datetime) -> list[str]:
        """Return ``in_progress`` tasks whose run blew the wall clock to
        ``ready``.

        A session that dies mid-run (orchestrator crash, killed process,
        laptop sleep) leaves its task ``in_progress``, which blocks the
        whole queue. A task whose last attempt started more than
        ``budget.wall_clock_min`` ago is stale: it goes back to ``ready``
        through the aborted-run edge and re-enters the queue under the
        same attempt budget. Attempt records themselves are owned by the
        session runner (F-06); this method only moves the status. A task
        with no attempts is treated as freshly picked and left alone.

        Args:
            now: Current time (timezone-aware).

        Returns:
            The ids of reclaimed tasks, in queue order.
        """
        reclaimed: list[str] = []
        for task in self.doc.get("tasks", []):
            if task.get("status") != "in_progress":
                continue
            attempts = task.get("attempts") or []
            if not attempts:
                continue
            started = _parse_dt(attempts[-1].get("started", ""))
            limit = task.get("budget", {}).get("wall_clock_min")
            if started is None or not isinstance(limit, int):
                continue
            if now - started > timedelta(minutes=limit):
                check_transition("in_progress", "ready")
                task["status"] = "ready"
                reclaimed.append(task.get("id"))
        if reclaimed:
            self.save()
        return reclaimed

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
        candidate["updated_at"] = _now_iso()
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
