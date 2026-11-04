"""The reviewer retry's budgets and abort paths (PR #90 rounds 3-4).

Round 3: the retry re-ran a seat unconditionally — it could exceed the
fan-out's own turn invariant, spend a stale pre-fan-out wall-clock
window once per invalid seat, turn a quota hit into an
invalid_verdict_json FAIL, and leave failure notes for seats its retry
had already rescued.

Round 4: a quota'd re-run stops the fan-out's remaining re-runs (the
attempt is aborting — more re-runs only burn tokens); a re-run reserves
the verifier's turn floor (a flaky reviewer must never escalate a task
that would otherwise fail a normal round); every still-invalid seat gets
ONE note naming the original failure, whether a re-run happened or was
refused (and by which budget), and the re-run's outcome; the trace event
carries seat:"reviewer" plus a 1-based seat_no; and the orchestrator's
two collection blocks are one shared closure.

Unit half: the budget-fit policy and the helper's refusal semantics.
Integration half: the orchestrator's wiring, driven through the F-23
Env/Scripted harness (same fake session runners, no real model).
"""

import json
from pathlib import Path

from test_f23 import Env, Scripted, _finding, _review_rounds, _verdict

from factory.l2 import parse_reviews_with_retry
from factory.orchestrator import _is_quota, _retry_budget
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
    assert _retry_budget(budget, records, 5.0 * 60, 8) == (8, 5.0, None)


def test_retry_budget_reserves_the_verifiers_turn_floor():
    """Round 4 finding 2: a re-run may only take turns that leave the
    verifier's floor for the pass after it — min(share, remaining -
    floor), refused below the floor. Both numbers are the existing
    _REVIEW_TURN_FLOOR; nothing new is invented."""
    budget = {"max_turns": 40, "wall_clock_min": 10.0}
    records = [SessionRecord(seat="implementer", status="completed",
                             num_turns=36)]
    # remaining 4: min(share 8, 4 - 2 reserved) = 2, exactly the floor
    assert _retry_budget(budget, records, 0.0, 8) == (2, 10.0, None)
    # remaining 3: min(8, 3 - 2) = 1, below the floor — the verifier's
    # floor wins; the refusal names its refuser (round 5: the fit and
    # the name come from ONE function).
    records.append(SessionRecord(seat="reviewer", status="completed",
                                 num_turns=1))
    assert _retry_budget(budget, records, 0.0, 8) == (
        None, None, "turn budget")
    # … and the share still caps: remaining 20, share 5 → 5.
    records.clear()
    records.append(SessionRecord(seat="implementer", status="completed",
                                 num_turns=20))
    assert _retry_budget(budget, records, 0.0, 5) == (5, 10.0, None)


def test_retry_budget_refused_without_wall_clock():
    budget = {"max_turns": 40, "wall_clock_min": 10.0}
    records = []
    assert _retry_budget(budget, records, 10.0 * 60, 8) == (
        None, None, "wall-clock budget")
    assert _retry_budget(budget, records, 10.5 * 60, 8) == (
        None, None, "wall-clock budget")


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
        if fit.refusal is not None:
            return None
        granted.append(fit.wall)
        # the retry consumes its whole granted window (worst case)
        spent["seconds"] += fit.wall * 60.0
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


# --- round 4 / finding 1: a quota'd re-run stops the re-runs ---------------


def test_a_quota_re_run_stops_the_fanouts_retries():
    """After one re-run comes back quota-limited, no further seat is
    re-run: the attempt is aborting on it, and every later re-run would
    only burn tokens against a dead quota."""
    quota_record = SessionRecord(seat="reviewer", status="failed",
                                 reason="usage limit reached (429)")
    reruns = []

    def rerun(seat_no, session, parse_error):
        reruns.append(seat_no)
        return quota_record if seat_no == 1 else _session(GARBAGE + " more")

    reports, retries = parse_reviews_with_retry(
        [_session(GARBAGE)] * 3, rerun, stop=_is_quota
    )
    assert reruns == [1]  # seats 2 and 3 are never re-run
    assert retries == 1   # the quota re-run ran, and counts
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
    # The FIRST quota'd re-run stops the re-runs (round 4): seat 2 is
    # never re-run — the attempt aborts on quota instead.
    assert retried["n"] == 1
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


# --- round 4 / finding 2: a re-run never starves the verifier -------------


def test_a_retry_never_starves_the_verifier(tmp_path):
    """The fan-out leaves 3 turns: an unreserved re-run would take them
    (min(share, 3) on the round-3 code), its rescue would hand the union
    findings, and the verifier fit check would then escalate
    budget_exhausted -> needs_human. The re-run must reserve the
    verifier's floor instead: refused, the round fails as a NORMAL
    invalid-verdict round and the next attempt ships the PR."""
    env = Env(tmp_path)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=(_verdict(_finding()), _verdict(), _verdict()))
    base_call = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if seat == "reviewer":
            with seats.lock:
                seen = sum(1 for s, *_ in seats.calls if s == "reviewer")
            if seen < 2:
                # The seats die, 7 turns each; with the implementer's 3
                # that drains 17 of the 20-turn budget — 3 remain.
                with seats.lock:
                    seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                    seats.round_reviews += 1
                    seats.note_fanout_prompt(_current_round(seats), prompt)
                return SessionRecord(seat=seat, status="aborted",
                                     reason=ABORT_REASON, num_turns=7)
            record = base_call(seat, prompt, run_dir, profile, **kwargs)
            record.num_turns = 3  # re-runs and later fan-outs
            return record
        return base_call(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(runner)
    assert result.outcome == "pr_opened"  # NOT needs_human/budget_exhausted
    assert result.escalation is None


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
    assert "[L2 reviewer seat" not in _pr_body(env)  # no note at all
    # The retry is traceable to the seat that slipped: the role string,
    # a 1-based seat_no, and the original failure status and reason.
    events = _trace_events(env)
    retries = [e for e in events if e.get("type") == "reviewer_retry"]
    assert len(retries) == 2
    assert all(e.get("seat") == "reviewer" for e in retries)
    assert {e.get("seat_no") for e in retries} == {1, 2}
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
    # broken, naming BOTH failures (original and re-run).
    fix2 = seats.prompts("implementer")[1]
    assert fix2.count("[L2 reviewer seat") == 1
    assert "[L2 reviewer seat 2]" in fix2
    assert "session aborted (error_during_execution)" in fix2
    assert "re-run ended: session aborted" in fix2


def test_a_completed_garbage_seat_with_a_dying_retry_gets_its_note(tmp_path):
    """Finding 3a: a seat that COMPLETED with unparseable text and whose
    re-run then died used to get no note at all (the note was keyed on
    the original session's status). It gets one note, and the note says
    what the re-run returned."""
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
        if seen in (2, 3):
            # Both re-runs die (the seats themselves completed fine —
            # their output was garbage, not their sessions).
            with seats.lock:
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                seats.round_reviews += 1
                seats.note_fanout_prompt(_current_round(seats), prompt)
            return SessionRecord(seat=seat, status="failed",
                                 reason="timeout_during_execution")
        return base_call(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(runner)
    assert result.outcome == "pr_opened"
    fix2 = seats.prompts("implementer")[1]
    assert fix2.count("[L2 reviewer seat") == 2
    assert "unparseable verdict output" in fix2  # the original failure
    assert fix2.count("re-run ended: session failed "
                      "(timeout_during_execution)") == 2


def test_an_aborted_seat_with_a_timed_out_retry_names_both(tmp_path):
    """Finding 3b: the aborted original AND the timed-out re-run, both
    in the one note."""
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
        if seen in (2, 3):
            with seats.lock:
                seats.calls.append((seat, prompt, Path(run_dir), kwargs))
                seats.round_reviews += 1
                seats.note_fanout_prompt(_current_round(seats), prompt)
            return SessionRecord(seat=seat, status="failed",
                                 reason="timeout_during_execution")
        return base_call(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(runner)
    assert result.outcome == "pr_opened"
    fix2 = seats.prompts("implementer")[1]
    assert fix2.count("[L2 reviewer seat") == 2
    assert "session aborted (error_during_execution)" in fix2
    assert fix2.count("re-run ended: session failed "
                      "(timeout_during_execution)") == 2


# --- round 4 / finding 4: the real wall-clock accounting drives refusals ---


class _StepClock:
    """A monotonic clock the scripted reviewer arms per session (f38's
    _FakeTime pattern, steerable per call): each armed step advances the
    clock exactly once — the next monotonic() call — so a session's
    measured duration is whatever its runner armed, capped by the
    timeout it was granted."""

    def __init__(self):
        self._now = 1000.0
        self._step = 0.0

    def monotonic(self) -> float:
        self._now += self._step
        self._step = 0.0
        return self._now

    def arm(self, seconds: float) -> None:
        self._step = seconds


def test_the_real_session_seconds_refuses_later_re_runs(
        tmp_path, monkeypatch):
    """Round 1 seats and their re-runs each "take" four minutes of the
    10-minute window, capped by the wall each was granted. The grants
    must shrink with the REAL accounting — 10 for the fan-out, 6 for
    the first re-run, 2 for the second — which only holds if
    session_seconds grows inside the re-run, not after the loop."""
    env = Env(tmp_path)
    clock = _StepClock()
    monkeypatch.setattr("factory.orchestrator.time", clock)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=("not verdict json at all",))
    base_call = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        record = base_call(seat, prompt, run_dir, profile, **kwargs)
        if seat == "reviewer":
            # Four minutes, never past the granted timeout.
            clock.arm(min(240.0, float(kwargs["timeout_min"]) * 60))
        return record

    env.run(runner)
    timeouts = [
        c[3]["timeout_min"] for c in seats.calls if c[0] == "reviewer"
    ]
    # fan-out (2 seats), then the two re-runs — attempt 1's sequence.
    assert timeouts[:4] == [10.0, 10.0, 6.0, 2.0]


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
