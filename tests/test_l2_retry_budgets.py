"""The reviewer retry's budgets and abort paths (PR #90 round 3).

The round-2 retry re-ran a seat unconditionally: it could exceed the
fan-out's own turn invariant (sessions' summed turns never exceed
``budget.max_turns``), spend a stale pre-fan-out wall-clock window once
per invalid seat, turn a quota hit into an invalid_verdict_json FAIL
(the fan-out's own quota sessions abort the attempt, task ready, no
attempt burned), and leave "[session <status>]" notes in the PR body for
seats its retry had already rescued.

Unit half: the budget-fit policy and the helper's refusal semantics.
Integration half: the orchestrator's wiring, driven through the F-23
Env/Scripted harness (same fake session runners, no real model).
"""

import json
from pathlib import Path

from test_f23 import Env, Scripted, _review_rounds, _verdict

from factory.l2 import parse_reviews_with_retry
from factory.orchestrator import _retry_budget
from factory.session import SessionRecord

GARBAGE = "Reviewer notes: the diff looks fine (no JSON)."
ABORT_REASON = "error_during_execution"


def _session(result, seat="reviewer", status="completed", turns=1):
    return SessionRecord(
        seat=seat, status=status, result=result, num_turns=turns,
        reason=None if status == "completed" else ABORT_REASON,
    )


# --- 1/2. the budget-fit policy: turns and wall clock --------------------


def test_retry_budget_caps_at_share_and_remaining_and_floors():
    budget = {"max_turns": 40, "wall_clock_min": 10.0}
    records = [SessionRecord(seat="implementer", status="completed",
                             num_turns=30)]
    # min(share 8, remaining 10) turns; the wall clock left after spend.
    assert _retry_budget(budget, records, 5.0 * 60, 8) == (8, 5.0)


def test_retry_budget_refused_below_the_turn_floor():
    budget = {"max_turns": 40, "wall_clock_min": 10.0}
    records = [SessionRecord(seat="implementer", status="completed",
                             num_turns=37)]
    # min(share 8, remaining 3) is above the floor of 2: allowed …
    assert _retry_budget(budget, records, 0.0, 8) == (3, 10.0)
    # … and one more turn spent leaves 2 (still the floor) …
    records.append(SessionRecord(seat="reviewer", status="completed",
                                 num_turns=1))
    assert _retry_budget(budget, records, 0.0, 8) == (2, 10.0)
    # … but below it: refused — a review that cannot take a tool turn
    # and answer is not a review (the fan-out's own start rule).
    records.append(SessionRecord(seat="reviewer", status="completed",
                                 num_turns=1))
    assert _retry_budget(budget, records, 0.0, 8) is None


def test_retry_budget_refused_without_wall_clock():
    budget = {"max_turns": 40, "wall_clock_min": 10.0}
    records = []
    assert _retry_budget(budget, records, 10.0 * 60, 8) is None
    assert _retry_budget(budget, records, 10.5 * 60, 8) is None


def test_three_invalid_seats_spend_at_most_the_window():
    """The round-2 bug: every retry reused the pre-fan-out window, so N
    invalid seats could spend N windows. Recomputed per retry, each
    re-run gets only what is left — only the retries that fit run, and
    the granted windows can never sum past the budget."""
    budget = {"max_turns": 100, "wall_clock_min": 10.0}
    granted: list[float] = []
    spent = {"seconds": 9.5 * 60, "records": []}

    def rerun(seat_no, session, parse_error):
        fit = _retry_budget(budget, spent["records"], spent["seconds"], 10)
        if fit is None:
            return None
        turns, wall = fit
        granted.append(wall)
        # the retry consumes its whole granted window (worst case)
        spent["seconds"] += wall * 60.0
        spent["records"].append(SessionRecord(
            seat="reviewer", status="completed", num_turns=1))
        return _session(GARBAGE)

    reports, retries = parse_reviews_with_retry([_session(GARBAGE)] * 3,
                                                rerun)
    assert 1 <= retries <= 2  # the first fits; the window runs out
    assert len(granted) == retries
    assert sum(granted) <= 10.0  # never more than the window
    assert all(r.parse_error is not None for r in reports)  # fail-closed


# --- 4. a refused re-run is not a retry -----------------------------------


def test_a_refused_rerun_is_not_counted_as_a_retry():
    calls = []

    def rerun(seat_no, session, parse_error):
        # The closure learns WHICH seat slipped and why, so its trace
        # event can name both.
        calls.append((seat_no, session.status, parse_error))
        return None  # the budget cannot fit one more seat

    reports, retries = parse_reviews_with_retry(
        [_session(GARBAGE), _session(ABORT_REASON, status="aborted")],
        rerun,
    )
    assert retries == 0
    assert [c[0] for c in calls] == [1, 2]
    assert calls[1][1] == "aborted"
    assert calls[0][2] == reports[0].parse_error  # the original failure
    assert all(r.parse_error is not None for r in reports)


# --- 3. a quota hit on the retry aborts like the fan-out ------------------


def test_quota_on_the_retry_aborts_like_the_fanout(tmp_path):
    env = Env(tmp_path)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=("not verdict json at all", _verdict(),
                              _verdict()))
    base_call = seats.__call__
    retried = {"n": 0}

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if seat != "reviewer":
            return base_call(seat, prompt, run_dir, profile, **kwargs)
        with seats.lock:
            seen = sum(1 for s, *_ in seats.calls if s == "reviewer")
        if seen >= 2:
            # A retry (the fan-out is 2 wide, so a call entering with
            # both seats logged is a re-run): it dies on quota exactly
            # like a fan-out session can.
            with seats.lock:
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                seats.round_reviews += 1
                seats.note_fanout_prompt(
                    _current_round(seats), prompt)
            retried["n"] += 1
            return SessionRecord(
                seat=seat, status="failed",
                reason="usage limit reached (429)",
            )
        return base_call(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(runner)
    assert retried["n"] == 2  # each invalid seat got its one re-run
    # NOT an invalid_verdict_json round that burns an attempt: the same
    # abort the fan-out's own quota branch takes.
    assert result.outcome == "quota_exhausted"
    assert result.escalation == "quota_exhausted"


def _current_round(seats) -> int:
    return sum(1 for s, *_ in seats.calls if s == "implementer")


# --- 1 (wiring). a turn-refused retry: no third seat, fail closed ---------


def test_turn_refused_retry_never_runs_a_third_seat(tmp_path):
    env = Env(tmp_path)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=("not verdict json at all",))
    base_call = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        record = base_call(seat, prompt, run_dir, profile, **kwargs)
        if seat == "reviewer":
            # The default budget is 20 turns; the implementer's 3 plus
            # two 8-turn seats drain 19 — one turn remains, below the
            # review floor, so no retry can fit.
            record.num_turns = 8
        return record

    result = env.run(runner)
    # Every attempt's review is void (invalid union, retry refused — the
    # turn budget resets per attempt but is 19/20 spent by each fan-out)
    # so the task runs out of attempts — and NO attempt's fan-out ever
    # got a third seat: 3 attempts x 2 seats, never a re-run.
    assert result.escalation == "attempts_exhausted"
    reviewer_calls = [
        c for c in seats.calls if c[0] == "reviewer"
    ]
    assert len(reviewer_calls) == 6


# --- 6. the session-ended note: only for seats the retry did not rescue ---


def _pr_body(env) -> str:
    return env.prs[0].body


def test_a_rescued_seat_leaves_no_session_ended_note(tmp_path):
    """Round 2 appended the note before the retry: a seat whose re-run
    rescued it still read as failed in the PR body. The note may only
    survive for a seat whose FINAL report is still invalid."""
    env = Env(tmp_path)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=(_verdict(), _verdict(), _verdict()))
    base_call = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if seat != "reviewer":
            return base_call(seat, prompt, run_dir, profile, **kwargs)
        with seats.lock:
            seen = sum(1 for s, *_ in seats.calls if s == "reviewer")
        if seen < 2:
            # The round's two seats die; their re-runs go to the round
            # script — a clean pass, so both seats are rescued.
            with seats.lock:
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                seats.round_reviews += 1
                seats.note_fanout_prompt(_current_round(seats), prompt)
            return _session(None, status="aborted")
        return base_call(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(runner)
    assert result.outcome == "pr_opened"  # both seats rescued
    assert "[session aborted]" not in _pr_body(env)
    # The retry is traceable to the seat that slipped: 1-based seat,
    # the original failure status and reason in the trace event.
    events = _trace_events(env)
    retries = [e for e in events if e.get("type") == "reviewer_retry"]
    assert len(retries) == 2
    assert {e.get("seat") for e in retries} == {1, 2}
    assert all(e.get("original_status") == "aborted" for e in retries)
    assert all(e.get("original_reason") == ABORT_REASON for e in retries)


def test_an_unrescued_seat_still_gets_its_note(tmp_path):
    """The other half: seat 1's retry rescues it, seat 2's dies again —
    exactly ONE session-ended note, for the seat that stayed broken."""
    env = Env(tmp_path)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=("not verdict json at all", _verdict(),
                              _verdict()))
    base_call = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if seat != "reviewer":
            return base_call(seat, prompt, run_dir, profile, **kwargs)
        with seats.lock:
            seen = sum(1 for s, *_ in seats.calls if s == "reviewer")
        if seen < 2:
            with seats.lock:
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                seats.round_reviews += 1
                seats.note_fanout_prompt(_current_round(seats), prompt)
            return _session(None, status="aborted")
        if seen == 2:
            # Seat 1's re-run parses: a clean pass instead of the
            # round's garbage script.
            with seats.lock:
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                seats.round_reviews += 1
                seats.note_fanout_prompt(_current_round(seats), prompt)
            return _session(_verdict())
        if seen == 3:
            # Seat 2's re-run dies again.
            with seats.lock:
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                seats.round_reviews += 1
                seats.note_fanout_prompt(_current_round(seats), prompt)
            return _session(None, status="aborted")
        return base_call(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(runner)
    assert result.outcome == "pr_opened"
    # The failed attempt never publishes, so the note rides the next
    # attempt's fix list — exactly once, for the seat that stayed
    # broken (round 2's pre-retry code noted both dead seats).
    fix2 = seats.prompts("implementer")[1]
    assert fix2.count("[session aborted]") == 1


def _trace_events(env) -> list[dict]:
    trace_dir = env.root / "trace" / "fixture"
    files = sorted(trace_dir.glob("*.jsonl"))
    assert files, "no trace written"
    events = []
    for line in files[-1].read_text().splitlines():
        events.append(json.loads(line))
    return events


# --- 5. the restored fan-out prompt contract ------------------------------


def test_the_fanout_prompt_contract_is_asserted_again():
    """Round 2's _review_rounds collapsed duplicate prompts and dropped
    the old pairwise identity check — a fan-out sending its seats
    different prompts passed silently. The restored buckets must catch
    it (per fan-out: one distinct prompt, seats and re-runs alike)."""
    seats = Scripted()
    seats.note_fanout_prompt(1, "prompt A")
    seats.note_fanout_prompt(1, "prompt A")  # the seat's re-run: same
    seats.note_fanout_prompt(1, "prompt B")  # a divergent seat: caught
    try:
        _review_rounds(seats)
    except AssertionError as exc:
        assert "different prompts" in str(exc)
    else:
        raise AssertionError("the divergent fan-out was not caught")
