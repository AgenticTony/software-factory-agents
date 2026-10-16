"""Task state machine and picker (F-03).

TASK-CONTRACT §3 draws the task lifecycle as a diagram; this module turns
it into an explicit allowed-transition table (:data:`ALLOWED_TRANSITIONS`)
plus the §4 picker (:func:`pick_next`).

The orchestrator is the only writer of ``tasks.json`` (§1 — agents never
write it), and every write goes through :class:`TaskQueue`, which

- refuses any transition not in the table,
- validates the whole queue (F-02) before writing, so invalid state is
  never persisted,
- only ever appends to ``attempts``, refusing rewrites, edits and
  removals (TASK-CONTRACT §2),
- writes atomically — temp file in the same directory, then rename — so
  a crash at any point leaves the previous file intact.

The ``X -> draft`` edges exist for one reason only: the approved contract
changed (TASK-CONTRACT §2 — the recomputed acceptance hash no longer
matches ``acceptance_hash``). :meth:`TaskQueue.demote_changed_contracts`
is their intended caller; the task then needs human re-approval (F-04)
before it can run again, so a demotion can never auto-retry work.

``not_buildable`` is set by the importer at task-creation time (§3), not
by an orchestrator transition, so it has no outgoing edges here.
"""

import copy
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from factory.contract import (
    POST_APPROVAL_STATUSES,
    acceptance_hash,
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

# The TASK-CONTRACT §3 diagram as an explicit table.
# (from, to) -> why the transition is legal.
ALLOWED_TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "ready"): "spec + contract approved, dependencies met",
    ("draft", "blocked"): "spec + contract approved, a dependency is open",
    ("blocked", "ready"): "dependencies met",
    ("blocked", "draft"): "contract changed",
    ("ready", "in_progress"): "orchestrator picked the task",
    ("ready", "draft"): "contract changed",
    ("in_progress", "in_review"): "validator L0-L3 passed, PR open",
    ("in_progress", "needs_human"): "escalated (attempts/ambiguity/risk)",
    ("in_progress", "draft"): "contract changed",
    ("in_review", "deploying"): "PR merged",
    ("in_review", "draft"): "contract changed",
    ("deploying", "needs_human"): "L4 live check failed (reverted)",
    ("deploying", "awaiting_owner"): "L4 passed, owner steps still open",
    ("deploying", "done"): "L4 passed, no open owner steps",
    ("deploying", "draft"): "contract changed",
    ("awaiting_owner", "done"): "owner steps ticked",
    ("awaiting_owner", "draft"): "contract changed",
    ("needs_human", "draft"): "contract changed",
    ("done", "draft"): "contract changed",
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
# approval. A hash mismatch in any of these sends the task back to draft.
CONTRACT_FROZEN_STATUSES = frozenset(POST_APPROVAL_STATUSES | {"blocked", "needs_human"})


class StateError(Exception):
    """Invalid queue-state operation (unknown task, append-only violation)."""


class TransitionError(StateError):
    """A status transition not in the allowed-transition table."""


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
    eligible = sorted(
        (
            task
            for task in tasks
            if task.get("status") == "ready"
            and task.get("kind") in ELIGIBLE_KINDS
            and _deps_met(task, tasks)
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


def _deps_met(task: dict, tasks: list) -> bool:
    """Check ``depends_on`` against this queue (TASK-CONTRACT §2).

    A ``repo:id`` dependency (anything with a colon) is cross-repo and is
    never met automatically — a human must mark it met. A same-repo
    dependency is met only when the referenced task is ``done`` in this
    queue; a dependency on an id not in the queue counts as unmet.
    """
    by_id = {other.get("id"): other for other in tasks}
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


def _atomic_write_json(path: Path, doc: dict) -> None:
    """Write ``doc`` to ``path`` atomically: temp file, then rename.

    The temp file lives in the same directory so the rename never crosses
    filesystems. Any failure removes the temp file and re-raises; the
    previous contents of ``path`` are untouched either way.

    Args:
        path: Destination file.
        doc: Document to write.

    Raises:
        OSError: If writing or renaming fails (the destination is intact).
    """
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
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
        old_doc: The queue as last written.
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


class TaskQueue:
    """The orchestrator's handle on a ``tasks.json`` — its only writer.

    Loads through F-02 (schema + semantics) on construction. Every mutation
    (``transition``, ``append_attempt``, ``demote_changed_contracts``)
    validates the resulting queue and writes it atomically; a failed write
    rolls the in-memory document back to what is on disk, so the instance
    never holds state that outlives a refused save.
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
            TransitionError: If the edge isn't in the table.
            ContractError: If the resulting queue wouldn't validate.
        """
        task = self.get(task_id)
        check_transition(task.get("status"), new_status)
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
        """
        task = self.get(task_id)
        task.setdefault("attempts", []).append(attempt)
        self.save()

    def demote_changed_contracts(self) -> list[str]:
        """Send tasks whose approved contract changed back to ``draft``.

        A task in a contract-frozen status whose recomputed acceptance hash
        no longer matches its stored ``acceptance_hash`` goes back to
        ``draft`` through the contract-changed edge, and the demotion is
        persisted. The task then needs human re-approval (F-04) before it
        can run again. The stale hash and approval fields are left in place
        as history; re-approval overwrites them.

        Returns:
            The ids of the demoted tasks, in queue order.
        """
        demoted: list[str] = []
        for task in self.doc.get("tasks", []):
            status = task.get("status")
            frozen_hash = task.get("acceptance_hash")
            if status in CONTRACT_FROZEN_STATUSES and frozen_hash:
                if acceptance_hash(task.get("acceptance", [])) != frozen_hash:
                    check_transition(status, "draft")
                    task["status"] = "draft"
                    demoted.append(task.get("id"))
        if demoted:
            self.save()
        return demoted

    def pick(self) -> Pick | None:
        """Pick the next task to work on (see :func:`pick_next`)."""
        return pick_next(self.doc.get("tasks", []))

    def save(self) -> None:
        """Validate and atomically write the queue.

        Stamps ``updated_at``, validates the whole document (F-02), checks
        that ``attempts`` only grew, then writes temp-file + rename. On any
        failure the in-memory document is rolled back to what is on disk,
        which the failure left intact.

        Raises:
            ContractError: If the new document wouldn't validate.
            StateError: If ``attempts`` shrank or was rewritten.
            OSError: If the write fails; the previous file is intact.
        """
        candidate = copy.deepcopy(self.doc)
        candidate["updated_at"] = _now_iso()
        try:
            validate_queue(candidate, str(self.path), self._schema_path)
            _check_attempts_append_only(self._disk_doc, candidate)
            _atomic_write_json(self.path, candidate)
        except BaseException:
            self.doc = copy.deepcopy(self._disk_doc)
            raise
        self.doc = candidate
        self._disk_doc = copy.deepcopy(candidate)
