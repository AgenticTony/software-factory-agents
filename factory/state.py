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
from typing import cast

from factory.contract import (
    NON_EXECUTABLE_KINDS,
    POST_APPROVAL_STATUSES,
    ContractError,
    acceptance_hash,
    canonical_json,
    contract_hash,
    load_tasks,
    validate_queue,
)

# All task statuses. MIRRORS the schema's status enum
# (factory/data/schema/tasks.schema.json $defs/status); keep both in sync.
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
    ("in_review", "needs_human"): (
        "stale base failed re-validation on the new default head (F-26), "
        "or the PR's CI stayed red through the fix rounds (F-75 ci_failed)"
    ),
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

# Outcome of a contract audit: which tasks were demoted to draft, which
# done tasks have a stale hash and need your review, and which tasks the
# owner ledger does not cover (F-63: a ready one of those was demoted —
# the list names every unledgered task, whatever its status, so the run
# can report the not-acted-on reason for the rest).
ContractAudit = namedtuple(
    "ContractAudit", ["demoted", "flagged_done", "unledgered"], defaults=([],)
)


def contract_change_action(status: str) -> str:
    """What happens to a task in ``status`` whose approved CONTRACT
    changed (acceptance, risk or gates — F-03's one X→draft reason).

    ONE rule, two callers (F-57 review round 2): the queue audit
    (:meth:`TaskQueue.demote_changed_contracts`, a stale hash) and the
    importer's merge (:func:`factory.importer.plan_import`, a proposal
    that changes a contract field) — never a second copy.

    * ``"demote"`` — a contract-frozen status whose ``(status, draft)``
      edge is legal: back to ``draft`` through the contract-changed
      edge. The stale hash and approval fields STAY as history;
      re-approval overwrites them.
    * ``"flag"`` — ``done``: merged work is flagged for the owner's
      review; reopening verified work is a human decision, never
      automatic (the module docstring, TASK-CONTRACT's acceptance row).
    * ``"leave"`` — anything else: ``draft`` itself, and
      ``not_buildable`` (never executable; the change is visible in the
      import diff and the task is already in human hands).

    Raises:
        TransitionError: A contract-frozen status without a legal
            ``(status, draft)`` edge — an impossible state in the table,
            loud rather than silently demoted.
    """
    if status == "done":
        return "flag"
    if status in CONTRACT_FROZEN_STATUSES:
        check_transition(status, "draft")
        return "demote"
    return "leave"


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


ATTEMPTS_RESET_ESCALATIONS = ("attempts_exhausted", "ci_failed")

# F-72 AC-4: the two escalation classes the owner answers with
# `factory recover --accept` — both are "the work is fine, the contract's
# policy question is mine", which is exactly what an escalation draft PR
# plus a one-command acceptance settles. Every other escalation keeps its
# current exit (credential_added rotates, scope_change_needed re-approves,
# protected_path_touched/sentinel_failed mean the diff breaks a standing
# rule an accept cannot waive).
ACCEPT_RECOVERY_ESCALATIONS = ("new_dependency", "risk_path_touched")


def escalation_accept_eligible(task: dict) -> bool:
    """Whether the owner's accept/reject word may move this task (F-72).

    True only for a ``needs_human`` task whose last attempt escalated
    ``new_dependency``/``risk_path_touched`` and which still holds an
    ESCALATION draft PR (``links.pr`` + ``links.escalation_pr``) — the
    accept re-runs L0 on that PR's exact head, so no PR means nothing to
    accept. The sweep never consults this predicate: it is `factory
    recover`'s gate alone.
    """
    attempts = task.get("attempts") or []
    links = task.get("links") or {}
    return (task.get("status") == "needs_human"
            and bool(attempts)
            and attempts[-1].get("escalation") in ACCEPT_RECOVERY_ESCALATIONS
            and bool(links.get("pr"))
            and links.get("escalation_pr") is True)

# F-65 AC-3: the declared-scope escalations. Both park a task with no
# GitHub signal at all (nothing was pushed; the task never reached a
# PR), so like credential_added the only honest exit is the owner's:
# widen (or drop) the scope, re-approve the new contract into the F-63
# ledger, and take the re-queue through
# :meth:`TaskQueue.recover_scope` — the dedicated state-layer writer
# `factory recover` drives with an interactive y/N and the ledger check
# (a widened scope that is not re-approved does not move).
SCOPE_RECOVERY_ESCALATIONS = ("scope_change_needed", "scope_missing")


def scope_recovery_eligible(task: dict) -> bool:
    """Whether the owner's scope re-queue word may move this task.

    True only for a ``needs_human`` task whose last attempt escalated
    ``scope_change_needed``/``scope_missing`` (F-65). The ledger half of
    the gate — the CURRENT contract hash must be owner-approved — lives
    in :meth:`TaskQueue.recover_scope`, which demands the ledger.
    """
    attempts = task.get("attempts") or []
    return (task.get("status") == "needs_human"
            and bool(attempts)
            and attempts[-1].get("escalation") in SCOPE_RECOVERY_ESCALATIONS)

# F-64 AC-2: how many quota-classified attempts (outcome "aborted",
# escalation "quota_exhausted") requeue for FREE per task per UTC
# calendar day — and, since the round-1 amendment, per task LIFETIME
# (FREE_QUOTA_REQUEUES_TOTAL). A quota error is not the task's fault —
# but a session that can emit "429" at will must not requeue forever on
# the owner's shared token: the per-day bound alone still admits a
# daily-cron task its free sessions every day indefinitely, which is the
# same "a record's reason grants more", merely rate-limited. A quota
# abort is free only while BOTH allowances remain; every one past the
# day's allowance counts toward max_attempts, and so does every one
# past the lifetime total (counted since the current attempt-budget
# floor, so an owner reset restores it). The day is read from the
# attempt's PARENT-written ``started`` stamp (now_iso()), never a
# session field. Owner-confirmed 2026-11-10: 3 a day (a GLM quota window
# is ~5 h, so a genuine task rarely hits more) and 9 in total (three
# full days) — past either, a quota abort spends a normal attempt.
FREE_QUOTA_REQUEUES_PER_DAY = 3
FREE_QUOTA_REQUEUES_TOTAL = 9


def attempts_reset_eligible(task: dict) -> bool:
    """Whether the owner's budget-reset word may move this task.

    True only for a ``needs_human`` task whose last attempt escalated
    ``attempts_exhausted`` — or, F-75, ``ci_failed``: the same shape (a
    run whose rounds are genuinely spent, published work parked on a
    red PR), the same honest exit — a human granting a fresh budget with
    evidence. Before the F-31 fix round that escalation had
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


def _quota_utc_day(attempt: dict) -> str | None:
    """The UTC calendar day of a quota attempt's PARENT-written
    ``started`` stamp ("2026-11-09T22:15:33Z" → "2026-11-09"), or None
    when it is missing or unparseable. F-64 AC-2: the day boundary is
    UTC midnight, judged from the orchestrator's own stamp — never a
    session-written field, which a hostile record could shift to keep
    every abort on a "fresh" day."""
    started = attempt.get("started")
    if not isinstance(started, str):
        return None
    try:
        stamp = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        # Naive: no defined UTC day (the schema's date-time format never
        # admits one through the sanctioned writers) — not provably on
        # any day, so not provably free. Counts, fail closed.
        return None
    return stamp.astimezone(timezone.utc).date().isoformat()


def attempt_budget_used(task: dict, slice_id: str) -> int:
    """How many attempts of ``slice_id``'s budget are spent.

    The orchestrator's one budget filter: same-slice attempts, not
    session-less sweep escalations (round-5 finding 1), not
    ``scope_missing`` refusals (F-65: the pre-seat refusal spent no
    session — a queue-wide fail-closed period must not burn every
    task's budget before the owner adds scopes), and — after a
    reset — only those at or after the reset floor. Every
    budget-relative number downstream (the run's attempt numbering, the
    implementer prompt's "attempt N of M", the PR body's attempt line)
    flows from this count.

    Aborted attempts stay free (a peak-window or crashed run is nobody's
    fault) with TWO bounds (F-64 AC-2, the round-1 amendment adds the
    second): quota-classified aborts (outcome ``aborted``, escalation
    ``quota_exhausted``) requeue for free at most
    :data:`FREE_QUOTA_REQUEUES_PER_DAY` times per UTC calendar day AND
    at most :data:`FREE_QUOTA_REQUEUES_TOTAL` times since the current
    attempt-budget floor — a quota abort is free only while BOTH
    allowances remain. The day is read from each attempt's own
    PARENT-written ``started`` stamp; the total is the same loop's count
    of frees (no second counter): because the loop sees only post-floor
    attempts, an owner reset restores the lifetime allowance with the
    budget. A quota stamp that is missing, unparseable or offset-less
    counts (fail closed: an unprovable day is not a free day). Non-quota
    aborts never consume a quota allowance.
    """
    floor = attempt_budget_floor(task, slice_id)
    freed_by_day: dict[str, int] = {}
    freed_total = 0
    used = 0
    for i, a in enumerate(task.get("attempts") or []):
        if i < floor or a.get("slice_id") != slice_id:
            continue
        if a.get("escalation") in RECOVERY_ESCALATIONS:
            continue
        if a.get("escalation") == "scope_missing":
            continue  # F-65: session-less pre-seat refusal, no budget
        if a.get("outcome") == "aborted":
            if a.get("escalation") != "quota_exhausted":
                continue  # a non-quota abort (peak window, crash): free
            day = _quota_utc_day(a)
            if (day is not None
                    and freed_by_day.get(day, 0) < FREE_QUOTA_REQUEUES_PER_DAY
                    and freed_total < FREE_QUOTA_REQUEUES_TOTAL):
                freed_by_day[day] = freed_by_day.get(day, 0) + 1
                freed_total += 1
                continue  # both allowances cover it
            # beyond a day's allowance, past the lifetime total, or an
            # unprovable day: counts
        used += 1
    return used


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
    reason = RECOVERY_TRANSITIONS.get((_status_of(task), new))
    if reason is None or not recovery_task_eligible(task):
        raise TransitionError(
            f"recovery transition {task.get('status')} -> {new} needs a "
            "stale_base/pr_closed escalation and a PR link (F-26; a "
            "credential_added task moves only through "
            "TaskQueue.recover_credential, and an attempts_exhausted task "
            "only through TaskQueue.recover_attempts)"
        )
    return reason


def _status_of(task: dict) -> str:
    """The task's status, typed: schema-required on every task
    (tasks.schema.json is validated on load — F-02), so this is a typed
    read, not a presence check (F-51/A-7, a cast with its reason)."""
    return cast(str, task.get("status"))


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



@contextlib.contextmanager
def queue_writer_lock(queue_path: Path):
    """Hold the exclusive writer lock on ``<queue>.lock`` — the ONE
    primitive every writer of a queue file takes (F-49 AC-4, m-6).

    ``TaskQueue.save`` serialises through it (:meth:`TaskQueue
    ._exclusive_lock` delegates here), and the importer's fresh-queue
    write takes it across its "no queue exists" check and its write, so
    two concurrent fresh imports serialise: the second sees the first's
    queue under the lock and refuses instead of overwriting it. The lock
    file is never renamed, so every cooperating writer serialises on the
    same path; ``tasks.json`` itself is replaced by rename and can't
    carry a stable lock.

    The lock is a blocking ``flock`` the OS releases when the holder
    dies, so a crashed writer never wedges the queue; the caller is
    responsible for re-checking queue state under the lock (that is the
    point of taking it).

    Args:
        queue_path: The queue file path (the lock sits beside it). The
            parent directory must already exist.
    """
    lock_path = queue_path.with_name(queue_path.name + ".lock")
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        current = _status_of(task)
        if (current, new_status) in RECOVERY_TRANSITIONS:
            # F-26 recovery edges: gated on the task, not just the pair
            # (round-4 finding 2) — only the sweep and `factory recover`
            # write them, and only for an eligible escalation.
            check_recovery_transition(task, new_status)
        else:
            check_transition(current, new_status)
        _check_kind_rules(task, _status_of(task), new_status)
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
        reason = RECOVERY_TRANSITIONS.get((_status_of(task), new_status))
        assert reason is not None  # needs_human → ready/blocked is in the table
        _check_kind_rules(task, _status_of(task), new_status)
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
                "last attempt escalated attempts_exhausted (or ci_failed, "
                "F-75)"
            )
        slice_id = task["attempts"][-1].get("slice_id") or task_id
        new_status = (
            "blocked" if unmet_dependencies(task, self.doc.get("tasks", []))
            else "ready"
        )
        reason = RECOVERY_TRANSITIONS.get((_status_of(task), new_status))
        assert reason is not None  # needs_human → ready/blocked is in the table
        _check_kind_rules(task, _status_of(task), new_status)
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

    def recover_scope(self, task_id: str, *, ledger, evidence: str,
                      done_by: str | None = None) -> str:
        """Re-queue a scope-escalated task after the owner re-approves
        its (possibly widened) contract: the one writer of that edge
        (F-65 AC-3 — the minimal path this WO had to build; the spec
        said "the existing approval path", but the contract audit never
        demoted a ``needs_human`` task whose scope changed, and
        ``approve`` refuses non-draft tasks, so nothing moved it).

        Two gates, both HERE (the credential/attempts precedent: the
        command cannot be the only gate): the task must be parked on
        ``scope_change_needed``/``scope_missing``
        (:func:`scope_recovery_eligible`), and its CURRENT whole-contract
        hash must be owner-approved in the F-63 ledger — the widened
        scope IS the contract change, so a hash with no ledger entry
        means the owner has not re-approved it yet, and the task does
        not move. Routing matches every recovery writer: ``blocked``
        when a dependency is open, else ``ready``; attempts are never
        reset (the pre-F-65 ``scope_missing`` refusals spent no budget
        anyway — :func:`attempt_budget_used` skips them).

        Args:
            task_id: Task parked on a scope escalation.
            ledger: The :class:`~factory.approvals.ApprovalLedger` the
                re-approval was recorded into (required, never None:
                without the ledger there is no proof of re-approval).
            evidence: What the owner checked; must not be blank.
            done_by: Who confirmed; recorded by the command's log entry.

        Returns:
            The new status: ``ready`` or ``blocked``.

        Raises:
            StateError: Unknown task, blank evidence, or a missing
                ledger.
            TransitionError: The task isn't parked on a scope
                escalation, or its current contract hash has no owner
                approval on record.
        """
        if not (evidence or "").strip():
            raise StateError(f"{task_id}: scope recovery requires evidence")
        if ledger is None:
            raise StateError(
                f"{task_id}: scope recovery requires the approval ledger — "
                "the re-approved contract hash is the gate"
            )
        task = self.get(task_id)
        if not scope_recovery_eligible(task):
            raise TransitionError(
                f"{task_id}: scope recovery needs a needs_human task whose "
                "last attempt escalated scope_change_needed or scope_missing"
            )
        chash = contract_hash(task)
        if not ledger.has_contract(task_id, chash):
            raise TransitionError(
                f"{task_id}: the current contract (hash {chash[:12]}) has no "
                "owner approval in the ledger — widen the scope in the "
                "queue, record the re-approval (`factory adopt` shows the "
                "contract and records the new hash; `factory approve` "
                "refuses non-draft tasks), then recover; a widened scope "
                "the owner has not re-approved does not move"
            )
        new_status = (
            "blocked" if unmet_dependencies(task, self.doc.get("tasks", []))
            else "ready"
        )
        reason = RECOVERY_TRANSITIONS.get((_status_of(task), new_status))
        assert reason is not None  # needs_human → ready/blocked is in the table
        _check_kind_rules(task, _status_of(task), new_status)
        _check_slice_rules(task, new_status)
        _check_owner_step_rules(task, new_status)
        task["status"] = new_status
        self.save()
        return new_status

    def recover_accept(self, task_id: str, *, ledger, sha: str,
                       evidence: str, done_by: str | None = None) -> str:
        """Move an accepted escalation to ``in_review``: the one writer
        of the F-72 accept edge (``needs_human → in_review`` beside the
        F-26 re-open edge — same pair, different gate).

        The gate lives HERE, not in the command (the recover_credential
        precedent): the task must be parked on a
        ``new_dependency``/``risk_path_touched`` escalation holding an
        escalation draft PR (:func:`escalation_accept_eligible`), and the
        owner ledger must hold the waiver for EXACTLY this (task,
        escalation, SHA) judged against the task's CURRENT contract hash
        — a different SHA (any new commit on the branch), a different
        class, or a contract changed since the acceptance moves nothing.
        On the move, ``links.escalation_pr`` clears: the PR is now a
        normal ``in_review`` PR (marked ready), re-validated and reported
        by the sweep like every factory PR.

        Args:
            task_id: Task parked on an accept-able escalation.
            ledger: The :class:`~factory.approvals.ApprovalLedger` holding
                the waiver (required, never None).
            sha: The PR head SHA the waiver covers — the caller re-reads
                it from GitHub right before this call, so a force-push
                between the y and the move refuses HERE rather than
                accepting a different commit.
            evidence: What the owner checked; must not be blank.
            done_by: Who accepted (recorded by the command's log entry).

        Returns:
            ``"in_review"``.

        Raises:
            StateError: Unknown task, blank evidence, or a missing ledger.
            TransitionError: The task isn't accept-eligible, or the
                ledger holds no waiver for exactly this key.
        """
        if not (evidence or "").strip():
            raise StateError(f"{task_id}: escalation acceptance requires evidence")
        if ledger is None:
            raise StateError(
                f"{task_id}: escalation acceptance requires the approval "
                "ledger — the waiver is the gate"
            )
        task = self.get(task_id)
        if not escalation_accept_eligible(task):
            raise TransitionError(
                f"{task_id}: escalation acceptance needs a needs_human task "
                "whose last attempt escalated new_dependency or "
                "risk_path_touched and which holds an escalation draft PR "
                "(links.pr + links.escalation_pr) — the supported reasons "
                "are exactly those two"
            )
        escalation = task["attempts"][-1]["escalation"]
        chash = contract_hash(task)
        if not ledger.has_escalation_waiver(
                task_id, escalation, sha, contract_hash_value=chash):
            raise TransitionError(
                f"{task_id}: no owner waiver for ({escalation}, head "
                f"{sha[:12]}) against the current contract — the waiver is "
                "keyed to the exact SHA and contract; a new commit on the "
                "branch or a changed contract voids it (re-run "
                "recover --accept)"
            )
        reason = RECOVERY_TRANSITIONS.get((_status_of(task), "in_review"))
        assert reason is not None  # the pair is in the recovery table
        _check_kind_rules(task, _status_of(task), "in_review")
        _check_slice_rules(task, "in_review")
        _check_owner_step_rules(task, "in_review")
        task["status"] = "in_review"
        links = {k: v for k, v in (task.get("links") or {}).items()
                 if k != "escalation_pr"}
        task["links"] = links  # the PR is a normal in_review PR now
        self.save()
        return "in_review"

    def reject_escalation(self, task_id: str) -> str:
        """Reject an escalation draft PR: the one writer of the F-72
        reject edge (``needs_human → draft`` — the contract-changed edge's
        pair, gated here on an accept-eligible escalation instead).

        The rejection clears the contract's approval markers
        (``acceptance_hash``/``approved_at``/``approved_by``): the whole
        point is that the contract can now change, and a changed contract
        needs fresh approval — the old approval authorises nothing (the
        F-63 hash check: a changed contract has no ledger entry). Every
        attempt record stays (append-only).

        Args:
            task_id: Task parked on an accept-able escalation holding an
                escalation draft PR.

        Returns:
            ``"draft"``.

        Raises:
            TransitionError: The task isn't accept-eligible (the same two
                escalation classes, the same PR link).
        """
        task = self.get(task_id)
        if not escalation_accept_eligible(task):
            raise TransitionError(
                f"{task_id}: escalation rejection needs a needs_human task "
                "whose last attempt escalated new_dependency or "
                "risk_path_touched and which holds an escalation draft PR "
                "— the supported reasons are exactly those two"
            )
        reason = ALLOWED_TRANSITIONS.get((_status_of(task), "draft"))
        assert reason is not None  # needs_human → draft is in the table
        _check_kind_rules(task, _status_of(task), "draft")
        _check_slice_rules(task, "draft")
        _check_owner_step_rules(task, "draft")
        task["status"] = "draft"
        # The approval markers go: a draft carries no frozen contract, and
        # the next approval re-freezes fresh fields over the stale ones.
        for marker in ("acceptance_hash", "approved_at", "approved_by"):
            task.pop(marker, None)
        self.save()
        return "draft"

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
        current = _status_of(task)
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
        current = _status_of(candidate)
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

    def demote_changed_contracts(self, ledger=None) -> ContractAudit:
        """Audit approved contracts against their stored hashes — and,
        when a ledger is given, against the owner's recorded approvals
        (F-63 AC-1).

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

        With ``ledger`` (an :class:`factory.approvals.ApprovalLedger`),
        the same pass also checks every frozen-contract task's CURRENT
        :func:`factory.contract.contract_hash` against the ledger: a
        ``ready`` task with no matching entry is demoted to ``draft``
        (the forged-approval case — a queue that mints its own approved
        task mints no owner approval), and EVERY unledgered task's id is
        returned in ``unledgered`` so the caller can report the
        not-acted-on reason for the statuses it must not touch.

        Args:
            ledger: Optional approval ledger; without one this is exactly
                the pre-F-63 hash audit (the direct state-layer callers).

        Returns:
            ``ContractAudit(demoted, flagged_done, unledgered)`` — task
            ids in queue order, split by what happened to them.
        """
        demoted: list[str] = []
        flagged_done: list[str] = []
        unledgered: list[str] = []
        for task in self.doc.get("tasks", []):
            status = task.get("status")
            frozen_hash = task.get("acceptance_hash")
            if status not in CONTRACT_FROZEN_STATUSES or not frozen_hash:
                continue
            changed = acceptance_hash(task.get("acceptance", [])) != frozen_hash
            unapproved = (
                ledger is not None
                and not ledger.has_contract(task.get("id", ""),
                                            contract_hash(task))
            )
            if unapproved and status != "done":
                # done needs no entry (merged work rests on the owner's
                # merge, not on a pickable approval); every other frozen
                # status is reported so the run can name what it refused
                # to act on.
                unledgered.append(task.get("id"))
            if changed or (unapproved and status == "ready"):
                action = contract_change_action(status)
                if action == "flag":
                    if changed:
                        flagged_done.append(task.get("id"))
                    continue
                # the shared rule's check_transition has already asserted
                # the edge is legal for every frozen, non-done status
                task["status"] = "draft"
                demoted.append(task.get("id"))
        if demoted:
            self.save()
        return ContractAudit(demoted=demoted, flagged_done=flagged_done,
                             unledgered=unledgered)

    @contextlib.contextmanager
    def begin_run(self, task_id: str, ledger=None):
        """Take the run lease and move a task to ``in_progress``.

        The run lease is an exclusive OS lock on ``<queue>.run.lock``,
        held for the whole session. The OS releases it the moment the
        holding process exits, crashed or not, so liveness never depends
        on timestamps. A session that dies leaves its task ``in_progress``
        with the lease free, which :meth:`reclaim_abandoned` detects.
        The lease is taken *before* the transition, so no window exists
        where a task is ``in_progress`` without a live holder.

        With ``ledger`` (F-63 AC-1), run start itself requires a matching
        owner approval for the task's CURRENT contract hash — the
        enforcement point behind the picker, re-reading the ledger so an
        approval cannot have been the product of a race.

        Args:
            task_id: Task to start (must be ``ready``).
            ledger: Optional approval ledger (the run-entry check).

        Yields:
            None. The lease is released when the block exits; the task's
            next status is the caller's to set.

        Raises:
            RunInProgressError: If another session holds the lease.
            StateError: If the task doesn't exist, or (with ``ledger``)
                its contract has no owner approval on record.
            TransitionError: If the task isn't ``ready``.
        """
        if ledger is not None:
            task = self.get(task_id)
            chash = contract_hash(task)
            if not ledger.has_contract(task_id, chash):
                raise StateError(
                    f"{task_id}: refusing to start — its current contract "
                    "has no owner approval in the ledger; approve it with "
                    "`factory approve` (or adopt it: `factory approve "
                    "--adopt`)"
                )
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

    def pick(self, ledger=None) -> Pick | None:
        """Pick the next task to work on (see :func:`pick_next`).

        With ``ledger`` (F-63 AC-1), only tasks whose CURRENT
        :func:`factory.contract.contract_hash` matches an owner ledger
        entry are eligible: the picker itself enforces the approval rule,
        not just the demote pass before it (a task that reached ``ready``
        between the two — a dependency met by ``sync_readiness`` — is
        still refused here, and the next run's audit demotes it).
        """
        tasks = self.doc.get("tasks", [])
        if ledger is not None:
            tasks = [
                task for task in tasks
                if ledger.has_contract(task.get("id", ""),
                                       contract_hash(task))
                or task.get("status") not in CONTRACT_FROZEN_STATUSES
            ]
        return pick_next(tasks)

    @contextlib.contextmanager
    def _exclusive_lock(self):
        """Hold an exclusive advisory lock on ``<queue>.lock``.

        Delegates to :func:`queue_writer_lock` — the one primitive every
        queue writer takes (``TaskQueue.save`` and the importer's
        fresh-queue write serialise on the same lock file; F-49 AC-4).
        """
        with queue_writer_lock(self.path):
            yield

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
