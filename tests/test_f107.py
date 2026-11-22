"""F-107 — a contract re-approved after a reject or a scope widen starts clean.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f107.py::test_ac_N``
means a test function with exactly that name.

Two acceptance criteria (BUILD-ORDER §F-107):

- AC-1 the REJECT itself writes one schema-valid ``attempt_resets`` marker
  in the same queue write that returns the task to draft (evidence
  "escalation rejected; the contract must be re-approved before any run",
  ``done_by`` the owner, ``prior_attempts`` the current count, the
  slice) — and nothing else writes one: a plain first approval and
  ``--redraft`` add no marker, and a second reject cycle grants a fresh
  budget, never a cumulative one. A reject followed by the owner's
  re-approval gives a task that RUNS (attempt 1 of N, not parked
  attempts_exhausted at 0 turns), and ``factory status`` shows the reset
  with its evidence.
- AC-2 ``approve --adopt`` of a task whose current acceptance differs
  from its stored ``acceptance_hash`` also writes the new
  ``acceptance_hash``/``approved_at``/``approved_by`` in the same write
  as the ledger record; an unchanged contract (byte-identical
  acceptance) is not rewritten. The J-4 sequence — park, widen scope
  AND change AC text, adopt, recover, loop — runs the task instead of
  demoting it to draft.

Real vs stubbed: the F-72/F-78 harness (``tests/_support``) — git, the
queue, the clone manager, the L0/L1 sandboxes and the push to the local
bare remote are real; the model seats and the gh/CI seams are scripted.
The ledger is the real :class:`~factory.approvals.ApprovalLedger`.
"""

import json
from pathlib import Path

import pytest
from _support.f72 import (
    IMPL_BAD,
    Env72,
    GhSeams,
    Seats,
    _task,
)
from _support.f75 import CI
from _support.f78 import GREEN, Parked, append_escalation

import factory.orchestrator as orch
from factory.approvals import ApprovalLedger
from factory.approve import (
    ApprovalError,
    adopt_queue,
    approve_task,
    recover_task,
)
from factory.contract import acceptance_hash, contract_hash, load_tasks
from factory.state import (
    TaskQueue,
    attempt_budget_floor,
    attempt_budget_used,
)
from factory.status import render_task_status

# The budget the approval pre-spend checks accept (the F-71 floor is
# 60 turns / 30 min): three attempts, so a reset means "attempt 1 of 3".
BUDGET = {"size": "S", "max_attempts": 3, "max_turns": 60,
          "wall_clock_min": 30}
# One attempt at the floor: the Parked fixture's shape, legal for the
# refreeze's pre-spend checks.
ONE_AT_FLOOR = {"size": "S", "max_attempts": 1, "max_turns": 60,
                "wall_clock_min": 30}
RESET_EVIDENCE = (
    "escalation rejected; the contract must be re-approved before any run"
)
# A hand-minted escalated attempt (the shape the parks carry; the same
# fields test_f72 mints). attempts_exhausted: the J-3 park class.
ESCALATED = {
    "run_id": "20261121T000000Z-f107aa", "slice_id": "T-1",
    "started": "2026-11-21T00:00:00Z", "ended": "2026-11-21T00:00:00Z",
    "outcome": "escalated", "rounds": 0, "turns": 0,
    "input_tokens": 0, "output_tokens": 0,
    "prompt_versions": {"implementer": "x"},
    "skills": [], "escalation": "attempts_exhausted",
}


def _noop(text) -> None:
    return None


def _reject(env: Env72, task_id: str = "T-1") -> str:
    """The owner's reject: confirm, scripted close, no terminal."""
    gh = GhSeams([])
    return recover_task(
        TaskQueue(env.queue_path), task_id, reject=True,
        factory_root=env.root, pr_close=gh.pr_close, done_by="owner",
        confirm=lambda question: True, echo=_noop,
    )


def _approve(env: Env72, task_id: str = "T-1") -> str:
    """The owner's re-approval of a draft task."""
    return approve_task(
        TaskQueue(env.queue_path), task_id, approved_by="owner",
        assume_yes=True, factory_root=env.root, echo=_noop,
    )


def _park_exhausted(env: Env72) -> None:
    """Run until the budget is spent: the attempts_exhausted park with
    its kept escalation draft PR (the J-3 shape)."""
    result = env.run(Seats(impl_files=[{"impl.py": IMPL_BAD}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "attempts_exhausted", result.escalation
    assert result.pr, "the park kept no escalation draft PR"


# ============================================================= AC-1 =========


def test_ac_1(tmp_path: Path, monkeypatch):
    """The reject writes the budget-reset marker itself: re-approval
    then runs, nothing else writes a marker, and a second reject cycle
    grants a fresh budget, never a cumulative one."""
    # The re-run's publish supersedes the old escalation draft PR with a
    # best-effort close — scripted like every other gh write here, so
    # the suite never calls the real gh.
    superseded: list[str] = []
    monkeypatch.setattr(orch, "gh_pr_close",
                        lambda url: superseded.append(url))
    # (1) J-3: three attempts spent -> park; the owner rejects, cuts the
    # contract down and re-approves; the next run starts attempt 1 of 3.
    env = Env72(tmp_path / "j3", tasks=[_task(budget=BUDGET)])
    _park_exhausted(env)
    spent = len(env.task()["attempts"])

    # A reject alone parks nothing in motion: draft, nothing runs.
    status = _reject(env)
    assert status == "draft"
    task = env.task()
    assert task["status"] == "draft"
    assert "acceptance_hash" not in task  # approval markers cleared (F-72)
    markers = task.get("attempt_resets") or []
    assert len(markers) == 1, "the reject wrote no attempt_resets marker"
    marker = markers[0]
    assert marker["evidence"] == RESET_EVIDENCE
    assert marker["done_by"] == "owner"
    assert marker["prior_attempts"] == spent == 3
    assert marker["slice_id"] == "T-1"
    load_tasks(env.queue_path)  # schema-valid: the queue still validates

    idle = env.run(Seats())
    assert idle.outcome == "idle", (
        f"a rejected task ran without re-approval: {idle}")

    # The owner cuts the contract down and re-approves (J-3's fix).
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-1")
    task["acceptance"] = [dict(task["acceptance"][0],
                               text="answer() returns 42 (cut down)")]
    queue.save()
    assert _approve(env) == "ready"
    task = env.task()
    assert len(task.get("attempt_resets") or []) == 1, (
        "the approval wrote a second marker")
    load_tasks(env.queue_path)

    # The re-approved task RUNS: attempt 1 of 3, published — not parked
    # attempts_exhausted at 0 turns on the budget the reject kept.
    seats = Seats()
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert "This is attempt 1 of 3." in seats.prompts("implementer")[-1]

    # `factory status` shows the reset with its evidence.
    page = render_task_status(load_tasks(env.queue_path), "T-1",
                              project="fixture")
    assert RESET_EVIDENCE in page, "the reset is not on the status page"
    assert "attempt reset" in page

    # (2) a plain first approval adds no marker.
    env = Env72(tmp_path / "plain",
                tasks=[_task(status="draft", budget=BUDGET)])
    assert _approve(env) == "ready"
    task = env.task()
    assert not task.get("attempt_resets"), (
        "a plain first approval wrote a reset marker")

    # (3) --redraft adds no marker (the reject is the only writer).
    env = Env72(tmp_path / "redraft", tasks=[_task(
        status="needs_human", budget=BUDGET,
        attempts=[dict(ESCALATED)],
        links={"branch": "factory/t-1-x"},
    )])
    TaskQueue(env.queue_path).redraft("T-1", evidence="checked by hand")
    task = env.task()
    assert task["status"] == "draft"
    assert not task.get("attempt_resets"), "--redraft wrote a reset marker"

    # (4) the costlier side, the full cycle: park -> reject -> approve ->
    # park again -> reject again. The second reject resets AGAIN: the
    # budget is a fresh 3 from that point, never 5 (never cumulative).
    env = Env72(tmp_path / "cycle", tasks=[_task(budget=BUDGET)])
    _park_exhausted(env)          # 3 spent
    _reject(env)
    _approve(env)
    _park_exhausted(env)          # 3 more spent (6 total)
    _reject(env)
    task = env.task()
    assert [m["prior_attempts"] for m in task["attempt_resets"]] == [3, 6], (
        "each reject did not write its own marker")
    # The floor is the SECOND reject's position: nothing spent counts.
    assert attempt_budget_used(task, "T-1") == 0, (
        "the second reject did not reset the budget")
    assert attempt_budget_floor(task, "T-1") == 6
    load_tasks(env.queue_path)
    _approve(env)
    seats = Seats()
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert "This is attempt 1 of 3." in seats.prompts("implementer")[-1]

    # (5) a reject on a task with no attempts is refused by eligibility
    # itself (a park IS an escalated last attempt — the command's gate
    # answers before the state writer), so no marker is written and the
    # task does not move: the writer never sees prior_attempts 0.
    env = Env72(tmp_path / "no-attempts", tasks=[_task(
        status="needs_human", budget=BUDGET, attempts=[],
        links={"pr": "https://github.com/fixture/fixture/pull/9",
               "escalation_pr": True},
    )])
    with pytest.raises(ApprovalError, match="escalation draft PR"):
        _reject(env)
    task = env.task()
    assert task["status"] == "needs_human"
    assert not task.get("attempt_resets")


# ============================================================= AC-2 =========


def test_ac_2(tmp_path: Path, monkeypatch):
    """Adopt refreezes what it records: the J-4 sequence runs the task
    instead of demoting it; an unchanged contract is not rewritten; a
    declined task is never touched."""

    # (1) the J-4 replay: a real ci_failed park, a later scope
    # escalation, the owner widens the scope AND changes the AC text,
    # adopts, recovers — and the next loop RUNS the task (the sweep
    # reports the PR) instead of demoting it to draft on the stale hash.
    p = Parked(tmp_path / "j4", monkeypatch, tasks=[_task(budget=ONE_AT_FLOOR)])
    append_escalation(p.queue(), "scope_change_needed")
    queue = p.queue()
    task = queue.get("T-1")
    task["scope"]["paths"].append("widened.py")
    task["acceptance"] = [dict(task["acceptance"][0],
                               text=task["acceptance"][0]["text"]
                               + " (widened: also cover widened.py)")]
    queue.save()
    env = p.env

    questions: list[str] = []
    results = adopt_queue(TaskQueue(env.queue_path), env.root,
                          approved_by="owner",
                          confirm=lambda question: (questions.append(question)
                                                    or True),
                          echo=_noop)
    assert [r.action for r in results] == ["adopted"], results
    task = env.task()
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    assert ledger.has_contract("T-1", contract_hash(task)), \
        "adopt recorded no ledger entry for the widened contract"
    # R1 nit: the prompt SAYS it re-approves when the acceptance changed.
    assert len(questions) == 1 and "re-approve its changed acceptance" \
        in questions[0], questions
    # F-107: the adopt also refroze the task's own fields — the ledger
    # and the task agree, so the loop's contract audit leaves it alone.
    assert task["acceptance_hash"] == acceptance_hash(task["acceptance"]), (
        "adopt left the task's acceptance_hash stale")
    assert task["approved_by"] == "owner"
    assert task["approved_at"] != "2026-10-18T10:00:00Z"
    load_tasks(env.queue_path)

    # recover: the park holds a normal open PR — the reset keeps it.
    status = recover_task(
        TaskQueue(env.queue_path), "T-1", reset_attempts=True,
        evidence="the widened scope is re-approved; resume the PR",
        done_by="owner", factory_root=env.root,
        pr_state=lambda url: "open", pr_ready=lambda url: None,
        confirm=lambda question: True, echo=_noop)
    assert status == "in_review"

    # the loop: green CI, the sweep reports the PR ready — the task is
    # NOT demoted to draft on the hash adopt refroze.
    sweep = env.run_once(p.seats, ci_state=CI(default=GREEN))
    task = env.task()
    assert task["status"] == "in_review", (
        f"the adopted task was demoted: {task['status']}")
    assert any("pull" in line for line in (sweep.ready_prs or [])), (
        "the sweep did not report the adopted task's PR")

    # (2) adopt of an UNCHANGED contract: the ledger entry is recorded,
    # the task record itself is byte-identical (no new approved_at), and
    # the prompt stays the plain adoption question.
    env = Env72(tmp_path / "unchanged", tasks=[_task("T-9")])
    before = json.dumps(env.task("T-9"), sort_keys=True)
    questions = []
    results = adopt_queue(TaskQueue(env.queue_path), env.root,
                          approved_by="owner",
                          confirm=lambda question: (questions.append(question)
                                                    or True),
                          echo=_noop)
    assert [r.action for r in results] == ["adopted"], results
    assert json.dumps(env.task("T-9"), sort_keys=True) == before, (
        "adopt rewrote an unchanged contract")
    assert "re-approve" not in questions[0], questions
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    assert ledger.has_contract("T-9", contract_hash(env.task("T-9")))

    # (3) scope widened but acceptance unchanged: the ledger records the
    # new WHOLE-contract hash, the task's frozen fields are not
    # rewritten (demote_changed_contracts compares acceptance hashes, so
    # the task is safe without a refreeze).
    env = Env72(tmp_path / "scope-only", tasks=[_task("T-5")])
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-5")
    old_at = task["approved_at"]
    task["scope"]["paths"].append("widened.py")
    queue.save()
    adopt_queue(TaskQueue(env.queue_path), env.root, approved_by="owner",
                confirm=lambda question: True, echo=_noop)
    task = env.task("T-5")
    assert task["approved_at"] == old_at, (
        "adopt rewrote approved_at on an acceptance-unchanged contract")
    assert task["acceptance_hash"] == acceptance_hash(task["acceptance"])
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    assert ledger.has_contract("T-5", contract_hash(task))

    # (4) confirmed for one task, declined for another: only the
    # confirmed one is refrozen (both at the budget floor — the
    # refreeze runs the pre-spend checks).
    env = Env72(tmp_path / "mixed", tasks=[
        _task("T-1", acceptance_hash="0" * 64, budget=BUDGET),
        _task("T-2", acceptance_hash="0" * 64, budget=BUDGET),
    ])
    results = adopt_queue(TaskQueue(env.queue_path), env.root,
                          approved_by="owner",
                          confirm=lambda question: question.startswith(
                              "Adopt T-1"),
                          echo=_noop)
    assert [(r.task_id, r.action) for r in results] == [
        ("T-1", "adopted"), ("T-2", "skipped")], results
    confirmed, declined = env.task("T-1"), env.task("T-2")
    assert confirmed["acceptance_hash"] == \
        acceptance_hash(confirmed["acceptance"]), \
        "the confirmed task was not refrozen"
    assert declined["acceptance_hash"] == "0" * 64, \
        "the declined task was rewritten"
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    assert ledger.has_contract("T-1", contract_hash(confirmed))
    assert not ledger.has_contract("T-2", contract_hash(declined))

    # (5) R1-A: the refreeze is an approval — a changed acceptance that
    # `factory approve` would refuse is refused by adopt too, with
    # NOTHING recorded (no ledger entry, no queue write, the task record
    # byte-identical). Before the fix, adopt refroze it runnable
    # unchecked.
    # (5a) a test ref that cannot run as written (F-71 AC-2).
    env = Env72(tmp_path / "bad-ref", tasks=[_task("T-7", budget=BUDGET)])
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-7")
    task["acceptance"] = [dict(task["acceptance"][0],
                               check={"type": "test",
                                      "ref": "not-a-pytest-ref"})]
    queue.save()
    before = json.dumps(env.task("T-7"), sort_keys=True)
    results = adopt_queue(TaskQueue(env.queue_path), env.root,
                          approved_by="owner",
                          confirm=lambda question: True, echo=_noop)
    assert [(r.task_id, r.action) for r in results] == [("T-7", "failed")], \
        results
    assert "not runnable" in results[0].detail, results[0].detail
    assert json.dumps(env.task("T-7"), sort_keys=True) == before, (
        "a refused adopt still wrote the task")
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    assert not ledger.has_contract(
        "T-7", contract_hash(env.task("T-7"))), (
        "a refused adopt still recorded the ledger entry")

    # (5b) a changed acceptance on a scope-less contract (F-65: the
    # pre-F-65 shape carries no scope key at all).
    env = Env72(tmp_path / "no-scope", tasks=[_task("T-8", budget=BUDGET)])
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-8")
    task.pop("scope", None)
    task["acceptance"] = [dict(task["acceptance"][0],
                               text="answer() returns 42 (widen)")]
    queue.save()
    results = adopt_queue(TaskQueue(env.queue_path), env.root,
                          approved_by="owner",
                          confirm=lambda question: True, echo=_noop)
    assert [r.action for r in results] == ["failed"], results
    assert "file scope" in results[0].detail, results[0].detail

    # (5c) a budget below the floor (the _prespend_checks half — a
    # refreeze that would make a starving contract runnable).
    env = Env72(tmp_path / "starved", tasks=[_task(
        "T-6", budget={"size": "S", "max_attempts": 3, "max_turns": 20,
                       "wall_clock_min": 30})])
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-6")
    task["acceptance"] = [dict(task["acceptance"][0],
                               text="answer() returns 42 (widen)")]
    queue.save()
    results = adopt_queue(TaskQueue(env.queue_path), env.root,
                          approved_by="owner",
                          confirm=lambda question: True, echo=_noop)
    assert [r.action for r in results] == ["failed"], results
    assert "budget" in results[0].detail, results[0].detail

    # (5d) a protected test ref outside the scope is a WARNING, not a
    # refusal: it renders in the contract before the y/N and the adopt
    # proceeds on the owner's confirm.
    env = Env72(tmp_path / "protected-ref", tasks=[_task("T-4",
                                                         budget=BUDGET)])
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-4")
    task["acceptance"] = [dict(task["acceptance"][0],
                               check={"type": "test",
                                      "ref": ".factory/test_probe.py::"
                                             "test_ac_1"})]
    queue.save()
    echoed: list[str] = []
    results = adopt_queue(TaskQueue(env.queue_path), env.root,
                          approved_by="owner",
                          confirm=lambda question: True, echo=echoed.append)
    assert [r.action for r in results] == ["adopted"], results
    assert any(".factory/test_probe.py" in line for line in echoed), (
        "the protected-ref warning did not render before the y/N")
