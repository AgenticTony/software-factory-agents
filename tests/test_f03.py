"""F-03 acceptance tests — BUILD-ORDER.md, work order F-03.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f03.py::test_ac_N``
means a test function with exactly that name. The extra descriptive tests
below cover paths the acceptance refs don't name: one test per illegal edge
class (AC-1), the legal lifecycle branches, slice selection, dependency
handling and the append-only enforcement on ``attempts``.
"""

import copy
import json
import os
from datetime import datetime

import pytest

from factory.contract import ContractError, acceptance_hash
from factory.state import (
    ALLOWED_TRANSITIONS,
    CONTRACT_FROZEN_STATUSES,
    STATUSES,
    ConcurrentWriteError,
    RunInProgressError,
    StateError,
    TaskQueue,
    TransitionError,
    check_transition,
    pick_next,
)

ACCEPTANCE = [
    {
        "id": "AC-1",
        "text": "it works",
        "check": {"type": "test", "ref": "tests/x.py::test_works"},
    }
]
HASH = acceptance_hash(ACCEPTANCE)


def _task(**overrides):
    base = {
        "id": "T-1",
        "title": "fixture task",
        "source": "docs/fixture.md#t-1",
        "kind": "build",
        "priority": "P2",
        "status": "ready",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": copy.deepcopy(ACCEPTANCE),
        "acceptance_hash": HASH,
        "approved_at": "2026-10-16T00:00:00Z",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 80, "wall_clock_min": 60},
    }
    base.update(overrides)
    return base


def _queue_file(tmp_path, tasks, name="tasks.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"version": 1, "project": "fixture", "tasks": tasks}, indent=2)
    )
    return path


_ATTEMPT = {
    "run_id": "r1",
    "slice_id": "S-1",
    "started": "2026-10-16T01:00:00Z",
    "ended": "2026-10-16T01:05:00Z",
    "outcome": "fail",
    "rounds": 3,
    "prompt_versions": {"implementer": "abc123"},
    "skills": [],
}


# --- AC-1: illegal transitions -------------------------------------------


def test_ac_1(tmp_path):
    # Exhaustive: every (from, to) pair of statuses. Pairs in the table are
    # accepted and return their reason; every other pair raises.
    for current in sorted(STATUSES):
        for new in sorted(STATUSES):
            if (current, new) in ALLOWED_TRANSITIONS:
                assert check_transition(current, new)
            else:
                with pytest.raises(TransitionError, match="illegal transition"):
                    check_transition(current, new)

    # The store enforces the same table, and a refusal leaves both the
    # in-memory task and the file untouched.
    path = _queue_file(tmp_path, [_task(id="T-1", status="ready")])
    before = path.read_text()
    queue = TaskQueue(path)
    with pytest.raises(TransitionError) as exc:
        queue.transition("T-1", "done")
    assert "illegal transition ready -> done" in str(exc.value)
    assert "allowed from ready: blocked, draft, in_progress" in str(exc.value)
    assert queue.get("T-1")["status"] == "ready"
    assert path.read_text() == before


# One representative test per class of illegal edge (AC-1).
ILLEGAL_EDGE_CLASSES = {
    "self": [("draft", "draft"), ("ready", "ready"), ("needs_human", "needs_human")],
    "forward_skip": [
        ("draft", "in_progress"),
        ("draft", "done"),
        ("ready", "in_review"),
        ("ready", "done"),
        ("in_progress", "deploying"),
    ],
    "backward_without_contract_change": [
        ("deploying", "in_review"),
        ("deploying", "in_progress"),
        ("awaiting_owner", "deploying"),
        ("done", "awaiting_owner"),
        ("in_review", "ready"),
    ],
    "lateral": [
        # in_review -> needs_human is legal since F-26 (stale base failed
        # re-validation on the new default head); tested in test_f26.
        ("in_review", "awaiting_owner"),
        ("blocked", "in_progress"),
        ("blocked", "done"),
    ],
    "exit_from_needs_human": [
        ("needs_human", "ready"),
        ("needs_human", "done"),
        # in_review / ready left the general table as F-26 recovery
        # edges (in_progress left entirely in round 5): legal only for a
        # stale_base/pr_closed escalation holding a PR link —
        # queue.transition enforces the gate; tested in tests/test_f26.py.
        ("needs_human", "in_review"),
        ("needs_human", "in_progress"),
    ],
    # not_buildable -> done exists since F-29, guarded to owner_only tasks
    # with every step ticked (tests/test_f29.py); every other exit is illegal.
    "exit_from_not_buildable": [
        ("not_buildable", "draft"),
        ("not_buildable", "ready"),
        ("not_buildable", "in_progress"),
    ],
}


@pytest.mark.parametrize("edge_class", sorted(ILLEGAL_EDGE_CLASSES))
def test_illegal_edge_class_raises(edge_class):
    for current, new in ILLEGAL_EDGE_CLASSES[edge_class]:
        with pytest.raises(TransitionError, match="illegal transition"):
            check_transition(current, new)


# --- Legal lifecycle branches --------------------------------------------


def test_full_lifecycle_happy_path(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="draft")])
    queue = TaskQueue(path)
    for status in ["ready", "in_progress", "in_review", "deploying", "done"]:
        queue.transition("T-1", status)
    assert TaskQueue(path).get("T-1")["status"] == "done"


def test_blocked_round_trip_then_escalation_is_legal(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="draft")])
    queue = TaskQueue(path)
    queue.transition("T-1", "blocked")
    queue.transition("T-1", "ready")
    queue.transition("T-1", "in_progress")
    queue.transition("T-1", "needs_human")
    with pytest.raises(TransitionError):  # no automatic exit from needs_human
        queue.transition("T-1", "ready")


def test_l4_branches_from_deploying(tmp_path):
    # live check FAIL -> needs_human; PASS with open owner steps ->
    # awaiting_owner -> done (steps ticked; the guard refuses done
    # while a step is open)
    path = _queue_file(tmp_path, [_task(id="T-FAIL", status="deploying")])
    TaskQueue(path).transition("T-FAIL", "needs_human")

    path = _queue_file(
        tmp_path,
        [
            _task(
                id="T-OWNER",
                status="deploying",
                owner_steps=[{"text": "flip the flag", "done": False}],
            )
        ],
        name="tasks2.json",
    )
    queue = TaskQueue(path)
    queue.transition("T-OWNER", "awaiting_owner")
    queue.get("T-OWNER")["owner_steps"][0].update(
        done=True, done_at="2026-10-19T00:00:00Z", done_by="Tester", evidence="ticked"
    )  # F-29: a tick carries its audit fields
    queue.transition("T-OWNER", "done")
    assert TaskQueue(path).get("T-OWNER")["status"] == "done"


def test_owner_step_guards(tmp_path):
    # done asserts every owner step is ticked; awaiting_owner with none
    # open would wait on nothing (added in the F-04 review round —
    # reconcile must not close a task its owner steps hold open).
    path = _queue_file(
        tmp_path,
        [
            _task(
                id="T-OPEN",
                status="deploying",
                owner_steps=[{"text": "answer Z.ai", "done": False}],
            )
        ],
    )
    queue = TaskQueue(path)
    with pytest.raises(TransitionError, match="owner step"):
        queue.transition("T-OPEN", "done")
    queue.transition("T-OPEN", "awaiting_owner")  # legal: a step is open
    with pytest.raises(TransitionError, match="never done"):
        TaskQueue(path).transition("T-OPEN", "done")
    queue.get("T-OPEN")["owner_steps"][0].update(
        done=True, done_at="2026-10-19T00:00:00Z", done_by="Tester", evidence="ticked"
    )  # F-29: a tick carries its audit fields
    queue.transition("T-OPEN", "done")
    assert TaskQueue(path).get("T-OPEN")["status"] == "done"

    path = _queue_file(tmp_path, [_task(id="T-CLOSED", status="deploying")], name="t2.json")
    with pytest.raises(TransitionError, match="would wait on nothing"):
        TaskQueue(path).transition("T-CLOSED", "awaiting_owner")
    TaskQueue(path).transition("T-CLOSED", "done")


# --- Multi-edge transitions (F-04 reconcile) ------------------------------


def test_transition_path_applies_the_final_status_in_one_save(tmp_path, monkeypatch):
    saves = []
    original = TaskQueue.save

    def counting_save(self):
        saves.append(1)
        original(self)

    monkeypatch.setattr(TaskQueue, "save", counting_save)
    path = _queue_file(tmp_path, [_task(id="T-1", status="draft")])
    TaskQueue(path).transition_path("T-1", ["ready", "in_progress", "in_review"])
    assert TaskQueue(path).get("T-1")["status"] == "in_review"
    assert len(saves) == 1


def test_transition_path_refuses_illegal_or_guarded_paths_without_writing(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="draft")])
    before = path.read_text()
    queue = TaskQueue(path)
    with pytest.raises(TransitionError, match="illegal transition ready -> in_review"):
        queue.transition_path("T-1", ["ready", "in_review"])
    with pytest.raises(StateError):
        queue.transition_path("T-1", [])
    assert path.read_text() == before

    # Guards run on the final status: done needs every slice merged.
    sliced = _task(
        id="T-SLICED",
        status="draft",
        slices=[{"id": "S-1", "text": "only slice", "status": "todo"}],
    )
    path = _queue_file(tmp_path, [sliced], name="tasks2.json")
    before = path.read_text()
    with pytest.raises(TransitionError, match="slice"):
        TaskQueue(path).transition_path(
            "T-SLICED",
            ["ready", "in_progress", "in_review", "deploying", "done"],
        )
    assert path.read_text() == before


@pytest.mark.parametrize("start", sorted(CONTRACT_FROZEN_STATUSES))
def test_contract_change_edge_returns_to_draft(tmp_path, start):
    # The X -> draft edges exist solely for "contract changed" (§2); they are
    # legal from every status that implies an approved contract.
    path = _queue_file(tmp_path, [_task(id="T-1", status=start)])
    TaskQueue(path).transition("T-1", "draft")


# --- AC-2: single writer --------------------------------------------------


def test_ac_2(tmp_path):
    tasks = [
        _task(id="T-BUSY", status="in_progress"),
        _task(id="T-READY", status="ready"),
    ]
    assert pick_next(tasks) is None
    path = _queue_file(tmp_path, tasks)
    assert TaskQueue(path).pick() is None


# --- AC-3: priority, queue order, eligibility -----------------------------


def test_ac_3():
    # priority first: a P1 task later in the queue beats a P2 task earlier
    tasks = [
        _task(id="T-P2-FIRST", priority="P2"),
        _task(id="T-P1-LATER", priority="P1"),
    ]
    assert pick_next(tasks).task["id"] == "T-P1-LATER"

    # ties broken by queue order
    tasks = [_task(id="T-A", priority="P1"), _task(id="T-B", priority="P1")]
    assert pick_next(tasks).task["id"] == "T-A"

    # non-ready statuses are skipped; non-executable kinds are skipped
    # (defensively: the schema can't produce decision+ready, but the picker
    # is specified to filter on both conditions); docs is executable
    tasks = [
        _task(id="T-DRAFT", status="draft"),
        _task(id="T-BLOCKED", status="blocked"),
        _task(id="T-IN-REVIEW", status="in_review"),
        _task(id="T-DECISION", kind="decision"),
        _task(id="T-RESEARCH", kind="research"),
        _task(id="T-OWNER", kind="owner_only"),
        _task(id="T-DOCS", kind="docs"),
    ]
    assert pick_next(tasks).task["id"] == "T-DOCS"


# --- AC-4: cross-repo dependencies ----------------------------------------


def test_ac_4():
    done_dep = _task(id="DEP", status="done")
    task = _task(id="T-X", depends_on=["soloadventurer-mobile:9b"])
    assert pick_next([done_dep, task]) is None
    # even when a task with that literal id exists and is done in this queue:
    # a ":" dep is cross-repo by construction and never met automatically
    foreign = _task(id="soloadventurer-mobile:9b", status="done")
    assert pick_next([foreign, task]) is None
    # a same-repo dep on a done task IS met
    ok = _task(id="T-OK", depends_on=["DEP"])
    assert pick_next([done_dep, ok]).task["id"] == "T-OK"


def test_dependency_must_be_done_in_this_queue():
    open_dep = _task(id="DEP", status="in_review")
    assert pick_next([open_dep, _task(id="T", depends_on=["DEP"])]) is None
    assert pick_next([_task(id="T", depends_on=["NOPE"])]) is None


# --- AC-5: changed contract goes back to draft -----------------------------


def test_ac_5(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="in_review")])
    # an external edit changes the acceptance text without updating the hash
    doc = json.loads(path.read_text())
    doc["tasks"][0]["acceptance"][0]["text"] = "it works differently"
    path.write_text(json.dumps(doc, indent=2))

    queue = TaskQueue(path)
    audit = queue.demote_changed_contracts()
    assert audit.demoted == ["T-1"]
    assert audit.flagged_done == []
    assert TaskQueue(path).get("T-1")["status"] == "draft"
    # a matching hash leaves the task alone
    path = _queue_file(tmp_path, [_task(id="T-2", status="ready")])
    queue = TaskQueue(path)
    audit = queue.demote_changed_contracts()
    assert audit.demoted == []
    assert audit.flagged_done == []
    assert queue.get("T-2")["status"] == "ready"


def test_done_task_with_changed_contract_is_flagged_not_demoted(tmp_path):
    # merged, live-verified work is never auto-reopened: a stale hash on a
    # done task surfaces in flagged_done and the status stays done
    path = _queue_file(tmp_path, [_task(id="T-DONE", status="done")])
    doc = json.loads(path.read_text())
    doc["tasks"][0]["acceptance"][0]["text"] = "it works differently"
    path.write_text(json.dumps(doc, indent=2))

    audit = TaskQueue(path).demote_changed_contracts()
    assert audit.demoted == []
    assert audit.flagged_done == ["T-DONE"]
    assert TaskQueue(path).get("T-DONE")["status"] == "done"


# --- AC-6: crash mid-write -------------------------------------------------


def test_ac_6(tmp_path, monkeypatch):
    path = _queue_file(tmp_path, [_task(id="T-1", status="ready")])
    before = path.read_text()
    queue = TaskQueue(path)

    def crash(src, dst):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        queue.transition("T-1", "in_progress")
    monkeypatch.undo()

    assert path.read_text() == before
    assert TaskQueue(path).get("T-1")["status"] == "ready"
    assert not list(tmp_path.glob(".tasks.json.tmp-*"))


def test_crash_during_serialisation_leaves_file_intact(tmp_path, monkeypatch):
    import factory.state as state_mod

    path = _queue_file(tmp_path, [_task(id="T-1", status="ready")])
    before = path.read_text()
    queue = TaskQueue(path)

    def broken_dump(*args, **kwargs):
        raise ValueError("simulated serialisation crash")

    monkeypatch.setattr(state_mod.json, "dump", broken_dump)
    with pytest.raises(ValueError, match="simulated serialisation crash"):
        queue.transition("T-1", "in_progress")

    assert path.read_text() == before
    assert not list(tmp_path.glob(".tasks.json.tmp-*"))


# --- Picker: slices --------------------------------------------------------


def test_picker_takes_first_incomplete_slice():
    task = _task(
        id="T",
        slices=[
            {"id": "S-1", "text": "a", "status": "merged"},
            {"id": "S-2", "text": "b", "status": "todo"},
            {"id": "S-3", "text": "c", "status": "todo"},
        ],
    )
    assert pick_next([task]).slice["id"] == "S-2"


def test_picker_falls_through_a_fully_merged_task():
    merged = _task(
        id="T-MERGED",
        priority="P1",
        slices=[{"id": "S-1", "text": "a", "status": "merged"}],
    )
    nxt = _task(id="T-NEXT", priority="P2")
    assert pick_next([merged, nxt]).task["id"] == "T-NEXT"


def test_picker_treats_an_unsliced_task_as_one_unit():
    pick = pick_next([_task(id="T")])
    assert pick.task["id"] == "T"
    assert pick.slice is None


# --- Store: persistence and invariants --------------------------------------


def test_append_attempt_persists_and_can_only_grow(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="in_progress")])
    queue = TaskQueue(path)
    queue.append_attempt("T-1", dict(_ATTEMPT))
    assert TaskQueue(path).get("T-1")["attempts"] == [_ATTEMPT]
    queue = TaskQueue(path)
    queue.append_attempt("T-1", {**_ATTEMPT, "run_id": "r2"})
    assert len(TaskQueue(path).get("T-1")["attempts"]) == 2


def test_attempt_rewrites_are_refused(tmp_path):
    path = _queue_file(
        tmp_path, [_task(id="T-1", status="in_progress", attempts=[dict(_ATTEMPT)])]
    )
    queue = TaskQueue(path)
    queue.get("T-1")["attempts"][0]["outcome"] = "pass"
    with pytest.raises(StateError, match="append-only"):
        queue.save()
    assert TaskQueue(path).get("T-1")["attempts"][0]["outcome"] == "fail"


def test_attempt_removal_is_refused(tmp_path):
    path = _queue_file(
        tmp_path, [_task(id="T-1", status="in_progress", attempts=[dict(_ATTEMPT)])]
    )
    queue = TaskQueue(path)
    queue.get("T-1")["attempts"].clear()
    with pytest.raises(StateError, match="append-only"):
        queue.save()


def test_removing_a_task_with_attempts_is_refused(tmp_path):
    path = _queue_file(
        tmp_path,
        [
            _task(id="T-1", status="in_progress", attempts=[dict(_ATTEMPT)]),
            _task(id="T-2", status="ready"),
        ],
    )
    queue = TaskQueue(path)
    queue.doc["tasks"] = [t for t in queue.doc["tasks"] if t["id"] != "T-1"]
    with pytest.raises(StateError, match="T-1"):
        queue.save()


def test_append_attempt_is_schema_validated(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="in_progress")])
    queue = TaskQueue(path)
    with pytest.raises(ContractError):
        queue.append_attempt("T-1", {"run_id": "r1"})


def test_save_refuses_invalid_state(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="ready")])
    queue = TaskQueue(path)
    queue.get("T-1")["status"] = "bogus"
    with pytest.raises(ContractError):
        queue.save()
    assert TaskQueue(path).get("T-1")["status"] == "ready"


def test_unknown_task_is_refused(tmp_path):
    path = _queue_file(tmp_path, [_task()])
    queue = TaskQueue(path)
    with pytest.raises(StateError, match="unknown task"):
        queue.transition("NOPE", "ready")
    with pytest.raises(StateError, match="unknown task"):
        queue.append_attempt("NOPE", dict(_ATTEMPT))
    with pytest.raises(TransitionError, match="not_a_status"):
        queue.transition("T-1", "not_a_status")


def test_save_stamps_updated_at(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="draft")])
    TaskQueue(path).transition("T-1", "ready")
    doc = json.loads(path.read_text())
    assert doc["updated_at"].endswith("Z")
    datetime.fromisoformat(doc["updated_at"].replace("Z", "+00:00"))


# --- Review fixes: concurrency --------------------------------------------


def test_concurrent_writer_cannot_silently_overwrite(tmp_path):
    # factory loop and a human-run factory approve are two writers on one
    # tasks.json; the second writer must be refused, not lose the first's
    # attempt
    path = _queue_file(tmp_path, [_task(id="T-1", status="in_progress")])
    writer_a = TaskQueue(path)
    writer_b = TaskQueue(path)

    writer_a.append_attempt("T-1", dict(_ATTEMPT))
    with pytest.raises(ConcurrentWriteError, match="changed on disk"):
        writer_b.append_attempt("T-1", {**_ATTEMPT, "run_id": "r2"})

    # A's attempt survives; a reloaded writer sees both orders
    attempts = TaskQueue(path).get("T-1")["attempts"]
    assert [a["run_id"] for a in attempts] == ["r1"]
    TaskQueue(path).append_attempt("T-1", {**_ATTEMPT, "run_id": "r2"})
    attempts = TaskQueue(path).get("T-1")["attempts"]
    assert [a["run_id"] for a in attempts] == ["r1", "r2"]


def test_concurrent_writer_cannot_overwrite_a_transition(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="ready")])
    writer_a = TaskQueue(path)
    writer_b = TaskQueue(path)

    writer_a.transition("T-1", "in_progress")
    # legal in B's stale view (it still sees ready), refused at the disk check
    with pytest.raises(ConcurrentWriteError):
        writer_b.transition("T-1", "in_progress")
    assert TaskQueue(path).get("T-1")["status"] == "in_progress"


# --- Review fixes: file format and validation ------------------------------


def test_non_ascii_survives_a_save(tmp_path):
    # the reviewed queue file uses literal em-dashes and arrows; escaping
    # them would put every such line into every future diff
    path = _queue_file(tmp_path, [_task(id="T-1", title="ship — done → prod ✓")])
    TaskQueue(path).transition("T-1", "blocked")
    raw = path.read_text(encoding="utf-8")
    assert "— done → prod ✓" in raw
    assert "\\u2014" not in raw


def test_duplicate_task_ids_are_rejected(tmp_path):
    path = tmp_path / "tasks.json"
    doc = {"version": 1, "project": "fixture", "tasks": [_task(id="WO-15"), _task(id="WO-15")]}
    path.write_text(json.dumps(doc))
    with pytest.raises(ContractError, match="duplicate task id WO-15"):
        TaskQueue(path)


# --- Review fixes: error hierarchy -----------------------------------------


def test_state_errors_are_contract_errors():
    # callers wrap queue operations in `except ContractError`; state and
    # concurrency failures must not fly past that handler
    assert issubclass(StateError, ContractError)
    assert issubclass(TransitionError, ContractError)
    assert issubclass(ConcurrentWriteError, ContractError)


def test_illegal_transition_is_caught_as_contract_error(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="ready")])
    with pytest.raises(ContractError):
        TaskQueue(path).transition("T-1", "done")


# --- Review fixes: recovery and continuation edges --------------------------


def test_rejected_pr_returns_for_a_fix_round(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="in_review")])
    TaskQueue(path).transition("T-1", "in_progress")
    assert TaskQueue(path).get("T-1")["status"] == "in_progress"


def test_aborted_run_returns_to_ready(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="in_progress")])
    TaskQueue(path).transition("T-1", "ready")
    assert TaskQueue(path).get("T-1")["status"] == "ready"


def test_slice_continuation_from_deploying(tmp_path):
    slices = [
        {"id": "S-1", "text": "a", "status": "merged"},
        {"id": "S-2", "text": "b", "status": "todo"},
    ]
    path = _queue_file(tmp_path, [_task(id="T-1", status="deploying", slices=slices)])
    queue = TaskQueue(path)
    queue.transition("T-1", "ready")  # previous slice merged; S-2 remains
    assert TaskQueue(path).get("T-1")["status"] == "ready"

    # done/awaiting_owner require every slice merged
    path = _queue_file(tmp_path, [_task(id="T-1", status="deploying", slices=slices)])
    queue = TaskQueue(path)
    with pytest.raises(TransitionError, match="S-2"):
        queue.transition("T-1", "done")
    with pytest.raises(TransitionError, match="S-2"):
        queue.transition("T-1", "awaiting_owner")

    # all slices merged: terminal edges legal, continuation refused
    merged = [dict(s, status="merged") for s in slices]
    path = _queue_file(tmp_path, [_task(id="T-1", status="deploying", slices=merged)])
    queue = TaskQueue(path)
    with pytest.raises(TransitionError, match="no unmerged slices"):
        queue.transition("T-1", "ready")
    queue.transition("T-1", "done")

    # an unsliced task has no continuation either
    path = _queue_file(tmp_path, [_task(id="T-1", status="deploying")])
    queue = TaskQueue(path)
    with pytest.raises(TransitionError, match="no unmerged slices"):
        queue.transition("T-1", "ready")
    queue.transition("T-1", "done")


def test_reclaim_abandoned_runs(tmp_path):
    # No run lease is held, so every in_progress task is abandoned — whether
    # its last attempt is old, recent, or (crash before the first attempt
    # record) missing entirely. Reclaim no longer guesses from timestamps.
    stale = {**_ATTEMPT, "started": "2026-10-16T10:00:00Z"}
    fresh = {**_ATTEMPT, "started": "2026-10-16T11:55:00Z"}
    path = _queue_file(
        tmp_path,
        [
            _task(id="T-STALE", status="in_progress", attempts=[stale]),
            _task(id="T-FRESH", status="in_progress", attempts=[fresh]),
            _task(id="T-NOATTEMPT", status="in_progress"),
        ],
    )
    reclaimed = TaskQueue(path).reclaim_abandoned()
    assert reclaimed == ["T-STALE", "T-FRESH", "T-NOATTEMPT"]
    reloaded = TaskQueue(path)
    for tid in reclaimed:
        assert reloaded.get(tid)["status"] == "ready"


def test_reclaim_never_takes_a_live_run(tmp_path):
    # A live session must not be reclaimed, however long it runs (setup and
    # L0-L3 validation are outside the session clock, so elapsed time says
    # nothing about liveness).
    path = _queue_file(tmp_path, [_task(id="T-1")])
    runner = TaskQueue(path)
    with runner.begin_run("T-1"):
        old = {**_ATTEMPT, "started": "2000-01-01T00:00:00Z"}
        runner.append_attempt("T-1", old)
        assert TaskQueue(path).reclaim_abandoned() == []
        assert TaskQueue(path).get("T-1")["status"] == "in_progress"


def test_reclaim_frees_a_crash_before_the_first_attempt(tmp_path):
    # Picked (in_progress) but the session died before recording an attempt.
    path = _queue_file(tmp_path, [_task(id="T-1")])
    runner = TaskQueue(path)
    with pytest.raises(RuntimeError):
        with runner.begin_run("T-1"):
            raise RuntimeError("session died")
    assert TaskQueue(path).get("T-1")["status"] == "in_progress"
    assert TaskQueue(path).reclaim_abandoned() == ["T-1"]
    assert TaskQueue(path).get("T-1")["status"] == "ready"


def test_begin_run_refuses_a_second_concurrent_run(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1"), _task(id="T-2")])
    with TaskQueue(path).begin_run("T-1"):
        with pytest.raises(RunInProgressError, match="T-1"):
            with TaskQueue(path).begin_run("T-2"):
                pass
    assert TaskQueue(path).get("T-2")["status"] == "ready"


def test_run_lease_is_a_contract_error():
    assert issubclass(RunInProgressError, ContractError)


def test_sync_readiness_reconciles_with_dependencies(tmp_path):
    path = _queue_file(
        tmp_path,
        [
            _task(id="DEP", status="in_review"),
            _task(id="T-REGRESSED", depends_on=["DEP"]),
            _task(id="DEP-2", status="done"),
            _task(id="T-UNBLOCKED", status="blocked", depends_on=["DEP-2"]),
            _task(id="T-CROSSREPO", status="blocked", depends_on=["soloadventurer-mobile:9b"]),
        ],
    )
    changed = TaskQueue(path).sync_readiness()
    assert changed == ["T-REGRESSED", "T-UNBLOCKED"]
    reloaded = TaskQueue(path)
    assert reloaded.get("T-REGRESSED")["status"] == "blocked"
    assert reloaded.get("T-UNBLOCKED")["status"] == "ready"
    # cross-repo deps are never met automatically: stays blocked
    assert reloaded.get("T-CROSSREPO")["status"] == "blocked"


def test_ready_to_blocked_edge_is_legal(tmp_path):
    path = _queue_file(tmp_path, [_task(id="T-1", status="ready")])
    TaskQueue(path).transition("T-1", "blocked")
    assert TaskQueue(path).get("T-1")["status"] == "blocked"


def test_lease_is_freed_when_the_holding_process_dies(tmp_path):
    # The point of an OS lock: a process that dies without cleanup (here
    # os._exit, no finally blocks run) still releases it.
    import subprocess
    import sys

    path = _queue_file(tmp_path, [_task(id="T-1")])
    script = (
        "import os, sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from factory.state import TaskQueue\n"
        "q = TaskQueue(sys.argv[1])\n"
        "ctx = q.begin_run('T-1'); ctx.__enter__()\n"
        "os._exit(0)\n"
    )
    repo = str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    subprocess.run([sys.executable, "-c", script, str(path), repo], check=True)
    assert TaskQueue(path).get("T-1")["status"] == "in_progress"
    assert TaskQueue(path).reclaim_abandoned() == ["T-1"]
