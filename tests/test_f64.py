"""F-64 acceptance tests — parent-side budget accounting.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f64.py::test_ac_N``
means a test function with exactly that name.

AC-1 carries the owner's 2026-11-09 amendment (option D — fixed per-seat
caps): a seat's turn cap is a constant the PARENT grants, never a number a
session's record funds; review seats are never refused or shrunk on turns;
wall clock is the parent-measured total bound with a reserved review
window; ``record.num_turns`` is telemetry only.

Real vs stubbed: the F-38 split — the git fixture, the clone manager, the
queue and the L0/L1 sandboxes are real; only the model seats are
scripted. The one real-runner case is the outer-timeout arm (AC-1): that
implementer seat runs through the real ``run_session_sandboxed`` with only
its ``run_sandboxed`` monkeypatched (plant a record file, hang past the
wall clock) — the ``_planted_timeout_run`` pattern from tests/test_f57.py.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _support.f23 import _finding, _task
from _support.f38 import OFF_PEAK, TEST_SOURCE, Env, Fanout, _verdict
from _support.f63 import ledger_approve

from factory.approvals import ApprovalLedger
from factory.approve import render_contract
from factory.config import DEFAULT_REVIEWERS
from factory.contract import ContractError, contract_hash, load_tasks
from factory.mergeorder import Protection
from factory.orchestrator import run_loop
from factory.session import SessionRecord, record_path_for, run_session_sandboxed
from factory.state import TaskQueue, attempt_budget_used

QUOTA_REASON = "api_error_429: usage quota exceeded"


def _pass_with_a_finding() -> str:
    """A reviewer verdict that passes with one minor finding — the shape
    that runs the verifier too (findings to vote on)."""
    return _verdict(
        _finding("rename helper _do to _render", severity="minor"),
        declared="pass",
    )


def _trace_events(env: Env) -> list[dict]:
    events: list[dict] = []
    for path in sorted((env.root / "trace" / "fixture").glob("*.jsonl")):
        events += [json.loads(line) for line in path.read_text().splitlines()]
    return events


def _planted(seats: Fanout, seat_name: str, value) -> "callable":
    """Wrap a Fanout so ``seat_name``'s record carries a planted
    ``num_turns`` — the forged-count channel AC-1 is about."""
    base = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        record = base(seat, prompt, run_dir, profile, **kwargs)
        if seat == seat_name:
            record.num_turns = value
        return record

    return runner


class _StepClock:
    """A monotonic clock the scripted seat arms: each armed step advances
    the clock exactly once (the next ``monotonic()`` call), so a session's
    parent-measured duration is whatever its runner armed (the
    tests/test_l2_retry_budgets.py pattern)."""

    def __init__(self):
        self._now = 1000.0
        self._step = 0.0

    def monotonic(self) -> float:
        self._now += self._step
        self._step = 0.0
        return self._now

    def arm(self, seconds: float) -> None:
        self._step = seconds


# --- AC-1: turns accounted from granted caps (amended: fixed per-seat caps) --


def test_ac_1(tmp_path: Path, monkeypatch):
    # The fixed per-seat caps and the review wall reserve (F-64 AC-1's
    # named constants — imported here so each AC carries its own red).
    from factory.orchestrator import (
        REVIEW_WALL_RESERVE_MIN,
        REVIEWER_TURN_CAP,
        VERIFIER_TURN_CAP,
    )

    # A planted num_turns on the implementer record — 0 (funds seats) and
    # 10**9 (drains them) — changes no later seat's cap: every reviewer is
    # CALLED with REVIEWER_TURN_CAP and the verifier with
    # VERIFIER_TURN_CAP, whatever the record claimed.
    for planted_turns in (0, 10**9):
        env = Env(tmp_path / f"impl-{planted_turns}")
        seats = Fanout(reviews=_pass_with_a_finding())
        result = env.run(_planted(seats, "implementer", planted_turns))
        assert result.outcome == "pr_opened", result
        assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
            REVIEWER_TURN_CAP
        }
        assert seats.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP
        # Telemetry still shows the session-reported count, raw — it funds
        # nothing (the caps above did not move).
        assert env.task()["attempts"][-1]["turns"] >= planted_turns

    # The same plant on a REVIEWER record: the other reviewer and the
    # verifier are called at their caps regardless.
    for planted_turns in (0, 10**9):
        env = Env(tmp_path / f"rev-{planted_turns}")
        seats = Fanout(reviews=_pass_with_a_finding())
        result = env.run(_planted(seats, "reviewer", planted_turns))
        assert result.outcome == "pr_opened", result
        assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
            REVIEWER_TURN_CAP
        }
        assert seats.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP

    # Forged counts on stub records, one bad value per seat (round 1,
    # X14): the attempt's telemetry sum ignores -5 / True / 1.5 / "999"
    # entirely — never raw, never subtracted, never a crash — and the
    # one VALID count (the implementer's 3) is all that renders.
    from factory.orchestrator import _session_turns

    for bad in (-5, True, "999", 1.5):
        assert _session_turns(SessionRecord(
            seat="x", status="completed", num_turns=bad)) == 0
    assert _session_turns(SessionRecord(
        seat="x", status="completed", num_turns=7)) == 7

    env = Env(tmp_path / "garbage")
    seats = Fanout(reviews=_pass_with_a_finding())
    base = seats.__call__
    bad_by_seat = {"reviewer": -5, "verifier": 1.5}  # reviewer 2: True

    def garbage_runner(seat, prompt, run_dir, profile, **kwargs):
        record = base(seat, prompt, run_dir, profile, **kwargs)
        if seat == "reviewer":
            seen = sum(1 for s, *_ in seats.calls if s == "reviewer")
            record.num_turns = bad_by_seat["reviewer"] if seen == 1 else True
        elif seat == "verifier":
            record.num_turns = bad_by_seat["verifier"]
        return record

    result = env.run(garbage_runner)
    assert result.outcome == "pr_opened", result
    assert env.task()["attempts"][-1]["turns"] == 3  # the valid count only

    # An implementer that "used" the whole turn budget still gets both
    # reviewers and the verifier at their FULL caps — the quality
    # guarantee (new behaviour: review is never squeezed out).
    env = Env(tmp_path / "spent")
    seats = Fanout(reviews=_pass_with_a_finding(), implementer_turns=10**9)
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert seats.seats().count("reviewer") == DEFAULT_REVIEWERS
    assert seats.seats().count("verifier") == 1
    assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }
    assert seats.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP
    assert seats.kwargs("implementer")[0]["max_turns"] == 20  # budget cap

    # The per-attempt bound on GRANTED turns: the parent granted at most
    # max_turns (implementer) + one reviewer cap each + one verifier cap.
    granted = sum(
        kw["max_turns"]
        for seat in ("implementer", "reviewer", "verifier")
        for kw in seats.kwargs(seat)
    )
    bound = 20 + DEFAULT_REVIEWERS * REVIEWER_TURN_CAP + VERIFIER_TURN_CAP
    assert granted <= bound

    # A reviewer re-run (invalid verdict, one retry) is granted the SAME
    # fixed reviewer cap — never a remainder.
    env = Env(tmp_path / "rerun")
    garbage = "Reviewer notes: the diff looks fine (no JSON)."
    seats = Fanout(reviews=garbage)
    scripts = [garbage, garbage, _pass_with_a_finding(), _pass_with_a_finding()]
    state = {"n": 0}

    def pick_review():
        with seats.lock:
            state["n"] += 1
            return scripts[min(state["n"] - 1, 3)]

    seats.on_reviewer = pick_review
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert seats.seats().count("reviewer") == 4  # fan-out + one re-run each
    assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }

    # A record delivered on the OUTER-TIMEOUT path (the real TimeoutExpired
    # arm, a planted record file — not a flag): the session's work and
    # record are kept, the use is visible in the trace, and no later
    # seat's cap moves.
    env = Env(tmp_path / "timeout")
    seats = Fanout(reviews=_pass_with_a_finding())
    base = seats.__call__
    import factory.session as session_mod

    def plant_and_hang(command, run_dir, settings_path, **kwargs):
        # The session "finished" before the runner hung: its work is in
        # the tree, its record (with a forged turn count) is on disk.
        run_dir = Path(run_dir)
        (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
        (run_dir / "tests").mkdir(exist_ok=True)
        (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
        path = record_path_for(Path(settings_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "seat": "implementer", "status": "completed", "result": "done",
            "num_turns": 10**9, "duration_ms": 500,
        }))
        raise subprocess.TimeoutExpired(cmd=command, timeout=99.0)

    def timeout_runner(seat, prompt, run_dir, profile, **kwargs):
        if seat != "implementer":
            return base(seat, prompt, run_dir, profile, **kwargs)
        return run_session_sandboxed(
            seat, prompt, Path(run_dir), profile,
            factory_root=env.root, max_turns=kwargs["max_turns"],
            timeout_min=kwargs["timeout_min"],
            protected=kwargs.get("protected", ()),
            system_prompt=kwargs.get("system_prompt"),
            run_id=kwargs.get("run_id"), task_id=kwargs.get("task_id"),
            trace_sink=kwargs.get("trace_sink"),
        )

    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "f64-timeout-token"
                        if service == "factory-zai" else None)
    monkeypatch.setattr(session_mod, "run_sandboxed", plant_and_hang)
    result = env.run(timeout_runner)
    monkeypatch.undo()
    assert result.outcome == "pr_opened", result
    assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }
    assert seats.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP
    # The timeout-path record was USED and named, never silently.
    assert any(e.get("type") == "record_used_after_timeout"
               for e in _trace_events(env))
    # Its forged count is telemetry (raw, session-reported) and funds
    # nothing — the caps above did not move.
    assert env.task()["attempts"][-1]["turns"] >= 10**9

    # Wall clock: the implementer's granted window is the budget MINUS the
    # review reserve; a session that exceeds its window leaves the reserve
    # for review (parent-measured time throughout).
    env = Env(tmp_path / "wall", tasks=[_task(budget={
        "size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 45,
    })])
    clock = _StepClock()
    monkeypatch.setattr("factory.orchestrator.time", clock)
    seats = Fanout(reviews=_pass_with_a_finding())
    base = seats.__call__

    impl_window = 45 - REVIEW_WALL_RESERVE_MIN

    def arming_runner(seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            # one minute PAST its window: the reserve minus one remains
            clock.arm((impl_window + 1) * 60.0)
        return base(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(arming_runner)
    assert result.outcome == "pr_opened", result
    assert seats.kwargs("implementer")[0]["timeout_min"] == pytest.approx(
        impl_window
    )
    assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }
    assert seats.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP
    reviewer_timeouts = [kw["timeout_min"] for kw in seats.kwargs("reviewer")]
    # review runs in what the overrunning implementer left: reserve - 1
    assert all(0 < t <= REVIEW_WALL_RESERVE_MIN - 1 for t in reviewer_timeouts)

    # A wall_clock_min too small to hold the reserve plus a minimal
    # implementer window refuses at attempt start, before any seat starts.
    too_small = int(REVIEW_WALL_RESERVE_MIN) - 1  # below the reserve itself
    env = Env(tmp_path / "too-small", tasks=[_task(budget={
        "size": "S", "max_attempts": 3, "max_turns": 20,
        "wall_clock_min": too_small,
    })])
    seats = Fanout(reviews=_pass_with_a_finding())
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "budget_exhausted"
    assert seats.seats() == []  # no session ever started
    attempt = env.task()["attempts"][-1]
    assert attempt["escalation"] == "budget_exhausted"
    assert str(too_small) in attempt["note"]
    assert f"{REVIEW_WALL_RESERVE_MIN:g}" in attempt["note"]

    # A LATER round (round 1, X7): the static pre-check only covers the
    # run's first round — a fix round that arrives with less than the
    # reserve remaining must refuse THERE, with the named note, and run
    # no implementer for that round.
    env = Env(tmp_path / "late-wall", tasks=[_task(budget={
        "size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 45,
    })])
    late_clock = _StepClock()
    monkeypatch.setattr("factory.orchestrator.time", late_clock)
    seats = Fanout(reviews=_pass_with_a_finding())
    fail_round1 = _verdict(
        _finding("wrong value returned", severity="major"), declared="fail")
    state = {"n": 0}

    def pick_and_burn():
        with seats.lock:
            state["n"] += 1
            n = state["n"]
        if n <= 2:  # round 1's fan-out: FAIL, and its sessions "take"
            # all but (reserve - 1) of the 45 minutes — under the reserve.
            late_clock.arm((45 - (REVIEW_WALL_RESERVE_MIN - 1)) * 60.0)
            return fail_round1
        return _pass_with_a_finding()  # round 2's fan-out (never reached)

    seats.on_reviewer = pick_and_burn
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "budget_exhausted"
    assert seats.seats().count("implementer") == 1  # round 2 ran none
    assert seats.seats().count("reviewer") == 2
    attempt = env.task()["attempts"][-1]
    assert attempt["escalation"] == "budget_exhausted"
    assert "review reserve" in attempt["note"]


# --- AC-2: free quota requeues are bounded (per UTC day and lifetime) -------


def _quota_runner(seats: Fanout) -> "callable":
    """Every implementer session dies on quota (a planted 429 record);
    review seats stay scripted — the run aborts before they ever run.
    ``runner.implementer_runs`` counts the implementer starts (the seat
    never reaches the Fanout's call log, which only records scripted
    seats)."""
    base = seats.__call__
    seen = {"implementer": 0}

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            seen["implementer"] += 1
            with seats.lock:  # record the call the way Fanout.__call__ does
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
            return SessionRecord(
                seat=seat, status="failed", reason=QUOTA_REASON, num_turns=1,
            )
        return base(seat, prompt, run_dir, profile, **kwargs)

    runner.implementer_runs = seen
    return runner


def _quota_attempt(started: str, slice_id: str = "T-1") -> dict:
    """One quota-classified attempt record, the shape the orchestrator's
    quota branch writes (outcome aborted, escalation quota_exhausted)."""
    return {
        "run_id": "planted", "slice_id": slice_id, "started": started,
        "ended": started, "outcome": "aborted", "rounds": 0, "turns": 0,
        "input_tokens": 0, "output_tokens": 0, "prompt_versions": {},
        "skills": [], "escalation": "quota_exhausted", "note": QUOTA_REASON,
    }


def test_ac_2(tmp_path: Path, monkeypatch):
    # Repeated planted 429s on ONE UTC day: the first PER_DAY requeue for
    # free, every later one counts — the task reaches attempts_exhausted
    # within max_attempts + PER_DAY runs.
    from factory.state import FREE_QUOTA_REQUEUES_PER_DAY as per_day
    from factory.state import FREE_QUOTA_REQUEUES_TOTAL

    assert FREE_QUOTA_REQUEUES_TOTAL >= per_day  # one day never exceeds it
    env = Env(tmp_path / "same-day")
    seats = Fanout(reviews=_pass_with_a_finding())
    runner = _quota_runner(seats)
    results = []
    for _ in range(10):
        result = env.run(runner)
        results.append(result)
        if result.escalation == "attempts_exhausted":
            break
    assert results[-1].escalation == "attempts_exhausted", [
        r.outcome for r in results
    ]
    # The free requeues spend no attempt number: the counting quota
    # aborts run as attempts 1, 2, 3 — max_attempts + per_day session
    # runs in total, then the next tick refuses before any seat starts.
    assert runner.implementer_runs["implementer"] == 3 + per_day
    assert len(seats.prompts("implementer")) == 3 + per_day
    assert len(results) == 3 + per_day + 1  # ... plus the refusal tick
    quota_attempts = [a for a in env.task()["attempts"]
                      if a.get("escalation") == "quota_exhausted"]
    # every one recorded, none re-run away
    assert len(quota_attempts) == 3 + per_day
    assert env.task()["status"] == "needs_human"

    # The counting is the ONE filter's: attempt_budget_used on the live
    # queue, from the parent-written started stamps. 4 = the 3 counting
    # quota aborts (the budget they spent) + the session-less
    # attempts_exhausted marker the refusal tick records.
    task = TaskQueue(env.queue_path).get("T-1")
    assert attempt_budget_used(task, "T-1") == 4

    # `factory loop` still stops on the first quota hit — no spin.
    env_loop = Env(tmp_path / "loop")
    seats_loop = Fanout(reviews=_pass_with_a_finding())
    ledger_approve(env_loop.root, env_loop.queue_path)
    loop = run_loop(
        env_loop.queue_path, factory_root=env_loop.root,
        session_runner=_quota_runner(seats_loop), open_pr=env_loop.open_pr,
        now=lambda: OFF_PEAK,
        protection=lambda r, b: Protection(checked=True, strict=True),
        pr_state=lambda url: "open",
    )
    assert loop.stop_reason == "quota_exhausted"
    assert len(loop.results) == 1  # stopped, did not spin

    # The UTC day boundary, both sides, on the counting function itself.
    day1 = "2026-11-09T23:59:59Z"
    day2 = "2026-11-10T00:00:00Z"
    task = {
        "attempts": (
            [_quota_attempt(day1)] * per_day    # day 1: all free
            + [_quota_attempt(day1)]            # day 1: counts
            + [_quota_attempt(day2)] * per_day  # day 2: all free
        )
    }
    assert attempt_budget_used(task, "T-1") == 1
    task["attempts"].append(_quota_attempt(day2))  # day 2: counts
    assert attempt_budget_used(task, "T-1") == 2

    # A NON-quota abort (the peak-window shape) is unaffected: free, and
    # it never consumes a day's quota allowance.
    task["attempts"] = (
        [_quota_attempt(day1)] * per_day
        + [{**_quota_attempt(day1), "escalation": None, "note": None}]
        + [_quota_attempt(day1)]  # counts: day 1's allowance is spent
    )
    assert attempt_budget_used(task, "T-1") == 1

    # The LIFETIME ceiling (round 1, R1-1): a per-day allowance alone is
    # unbounded across days — a daily loop claiming 429 every run would
    # requeue free forever. per_day quota aborts a day over enough days
    # to pass the total: the first FREE_QUOTA_REQUEUES_TOTAL are free,
    # every later one counts.
    n_days = FREE_QUOTA_REQUEUES_TOTAL // per_day + 2
    days = [f"2026-11-{d:02d}T10:00:00Z" for d in range(1, n_days + 1)]
    task = {"attempts": [
        _quota_attempt(day) for day in days for _ in range(per_day)
    ]}
    assert len(task["attempts"]) > FREE_QUOTA_REQUEUES_TOTAL
    # FREE_QUOTA_REQUEUES_TOTAL of them free: the rest count.
    assert attempt_budget_used(task, "T-1") == (
        len(task["attempts"]) - FREE_QUOTA_REQUEUES_TOTAL
    )
    # An owner reset restores the LIFETIME allowance with the budget
    # (the count starts at the reset floor, like every other attempt).
    task["attempt_resets"] = [{
        "at": days[-1], "evidence": "owner granted more", "done_by": "o",
        "slice_id": "T-1", "prior_attempts": len(task["attempts"]),
    }]
    assert attempt_budget_used(task, "T-1") == 0

    # Stamp shapes (round 1, X11): a quota attempt whose started stamp is
    # NAIVE, missing or garbage counts — fail closed. An unprovable day
    # is never a free day, and a naive stamp is never read as local time
    # (which would let a hand-shifted stamp mint fresh days).
    task = {"attempts": [_quota_attempt(day1)] * per_day + [  # the frees
        _quota_attempt("2026-11-15"),                 # naive: counts
        _quota_attempt(None),                         # missing: counts
        _quota_attempt("not a timestamp"),            # garbage: counts
    ]}
    assert attempt_budget_used(task, "T-1") == 3

    # The day boundary on the REAL run path: the parent-written started
    # stamps cross UTC midnight, and the new day's allowance is restored.
    env2 = Env(tmp_path / "midnight")
    seats2 = Fanout(reviews=_pass_with_a_finding())
    runner2 = _quota_runner(seats2)
    # Start so day 1's per_day ticks (30 s apart) all land before midnight.
    midnight = datetime(2026, 11, 10, tzinfo=timezone.utc)
    clock = {"now": midnight - timedelta(seconds=30 * per_day - 10)}

    def fake_now_iso():
        return clock["now"].isoformat(timespec="seconds").replace("+00:00", "Z")

    monkeypatch.setattr("factory.orchestrator.now_iso", fake_now_iso)

    def tick(seconds: float):
        clock["now"] += timedelta(seconds=seconds)
        return env2.run(runner2)

    # Day 1: per_day free requeues, the last 20 s before midnight...
    assert tick(0).outcome == "quota_exhausted"
    for _ in range(per_day - 1):
        assert tick(30).outcome == "quota_exhausted"
    assert clock["now"] < midnight
    # ... midnight crosses; day 2 opens a fresh allowance: per_day more
    # are free again, and the two aborts after them count.
    for _ in range(per_day + 2):
        assert tick(30).outcome == "quota_exhausted"
    assert clock["now"].date() == midnight.date()
    task2 = TaskQueue(env2.queue_path).get("T-1")
    assert attempt_budget_used(task2, "T-1") == 2
    # The run numbering every downstream number flows from: the free
    # requeues (both days) were all "attempt 1 of 3"; only the counting
    # aborts move it.
    impl_prompts = seats2.prompts("implementer")
    # every quota run across the two days
    assert len(impl_prompts) == 2 * per_day + 2
    assert "This is attempt 1 of 3." in impl_prompts[0]
    assert "This is attempt 1 of 3." in impl_prompts[-2]
    assert "This is attempt 2 of 3." in impl_prompts[-1]


# --- AC-3: budgets visible and bounded at approval ---------------------------


def test_ac_3(tmp_path: Path):
    # The rendered contract shows max_turns, wall_clock_min and
    # max_attempts beside size (distinctive values, so a stale render
    # cannot pass by coincidence).
    task = _task(budget={
        "size": "S", "max_attempts": 2, "max_turns": 37, "wall_clock_min": 45,
    })
    rendered = render_contract(task)
    for number in ("37", "45", "2"):
        assert number in rendered, f"{number} missing from the contract"

    # Schema maxima: 200 turns / 150 wall minutes pass, 201/151 refuse
    # with the field named.
    def queue_with(max_turns: int, wall_clock_min: int) -> Path:
        doc = {"version": 1, "project": "fixture", "tasks": [_task(budget={
            "size": "S", "max_attempts": 3,
            "max_turns": max_turns, "wall_clock_min": wall_clock_min,
        })]}
        path = tmp_path / f"q-{max_turns}-{wall_clock_min}.json"
        path.write_text(json.dumps(doc, indent=2) + "\n")
        return path

    TaskQueue(queue_with(200, 150))  # the maxima themselves load
    for (max_turns, wall), field in (((201, 150), "max_turns"),
                                     ((200, 151), "wall_clock_min")):
        with pytest.raises(ContractError) as excinfo:
            TaskQueue(queue_with(max_turns, wall))
        assert field in str(excinfo.value), excinfo.value

    # The factory's own queue and the JobFinderOS draft still load.
    repo_root = Path(__file__).resolve().parents[1]
    assert TaskQueue(repo_root / ".factory" / "tasks.json").doc[
        "project"] == "software-factory"
    assert load_tasks(repo_root / "projects" / "jobfinderos.tasks.json")[
        "project"] == "jobfinderos"

    # F-63 interaction: the render is display-only. A ledger-approved
    # fixture task is rendered, hashed and run — the approval still
    # matches and the task is still picked, not demoted.
    env = Env(tmp_path / "ledger")
    seats = Fanout(reviews=_pass_with_a_finding())
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert result.task_id == "T-1"
    assert "T-1" not in result.unledgered
    task = env.task()
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    assert ledger.has_contract("T-1", contract_hash(task))
    before = contract_hash(task)
    render_contract(task)  # rendering mutates nothing
    assert contract_hash(task) == before


# ------------------------------------------------------ focused (round 1) --


def test_trace_turns_render_unknown_for_forged_counts(tmp_path: Path):
    """Round 1 (X15): a forged turn count in a trace ``session_end``
    event renders as None ("turns unknown"), never raw — telemetry only,
    the same validation the attempt record's sum applies. A valid count
    still renders as the integer."""
    from factory.trace import TraceWriter

    writer = TraceWriter(tmp_path, "probe", "20261110T000000Z-probe1", "T-1")
    events: list[dict] = []
    writer.append = events.append
    for bad in (-5, True, "999", 1.5):
        writer.session_end(
            SessionRecord(seat="reviewer", status="completed", num_turns=bad),
            "reviewer",
        )
    writer.session_end(
        SessionRecord(seat="reviewer", status="completed", num_turns=7),
        "reviewer",
    )
    assert [e["turns"] for e in events] == [None, None, None, None, 7]


def test_owner_confirmed_budget_values():
    # The F-64 policy numbers the owner confirmed on 2026-11-10. The
    # behavioural tests above derive from the constants, so this pin is
    # what makes a change to any of them a visible, reviewed edit — an
    # implementer quietly loosening a quota or a review floor fails here.
    from factory.orchestrator import (
        _MIN_IMPLEMENTER_WALL_MIN,
        REVIEW_WALL_RESERVE_MIN,
        REVIEWER_TURN_CAP,
        VERIFIER_TURN_CAP,
    )
    from factory.state import (
        FREE_QUOTA_REQUEUES_PER_DAY,
        FREE_QUOTA_REQUEUES_TOTAL,
    )

    assert REVIEWER_TURN_CAP == 20
    assert VERIFIER_TURN_CAP == 10
    assert REVIEW_WALL_RESERVE_MIN == 15.0
    assert _MIN_IMPLEMENTER_WALL_MIN == 1.0
    assert FREE_QUOTA_REQUEUES_PER_DAY == 3
    assert FREE_QUOTA_REQUEUES_TOTAL == 9
