"""F-85 — one repair turn for an unparseable review verdict.

Evidence: run ``20261114T184258Z-140cfb`` (and ``20261114T134530Z-f49bb7``),
trace event ``reviewer_retry`` — a trailing comma in reviewer 2's verdict
voided the parse ("Illegal trailing comma before end of object") and the
WHOLE reviewer session was re-run: 13 turns, about $0.34, for a
formatting slip. The parse stays exactly as strict. The recovery gets
cheaper: ONE follow-up turn on the SAME session (the SDK's resume —
same session id, same transcript), carrying the parse error, the
required schema and "re-emit the verdict only; do not change any
finding". A repair that parses and does not shrink what the malformed
text carried is the seat's verdict; anything else falls back to today's
full re-run, counted against today's retry budget.
"""

import json
from pathlib import Path

from _support.f23 import Env, Scripted, _finding, _verdict

from factory.config import FactoryConfig
from factory.l2 import L2Finding, parse_verdict, parse_verifier_vote
from factory.session import SessionRecord

# The run's shape: verdicts and votes that are COMPLETE except for a
# trailing comma — every finding, vote and declared verdict extractable,
# only the parse fails.
BAD_PASS = '{"verdict": "pass", "findings": [],}'
BAD_FAIL_BLOCKER = json.dumps(
    {"verdict": "fail",
     "findings": [{"id": "R-1", "axis": "spec", "severity": "blocking",
                   "file": "impl.py", "line": 2, "text": "the defect"}]},
)[:-1] + ",}"
BAD_FAIL_EMPTY = '{"verdict": "fail", "findings": [],}'
BAD_VOTE_KEEP = ('{"votes": [{"id": "U-1", "keep": true, '
                 '"reason": "confirmed", "tag": "important"}],}')
BAD_VOTE_DROP = ('{"votes": [{"id": "U-1", "keep": false, '
                 '"reason": "not real: impl.py already returns 42"}],}')
PROSE = "I had another look at the diff; it all seems fine to me."
SID = "resume-sid-r1"

# The one instruction each repair turn carries (AC-1's exact demand).
REVIEWER_INSTRUCTION = "Re-emit the verdict only; do not change any finding."
VERIFIER_INSTRUCTION = "Re-emit the vote only; do not change any vote."

_U1 = [L2Finding(id="U-1", text="the defect", axis="spec",
                 severity="blocking", file="impl.py", line=2)]


def _completed(result, *, turns=1, session_id=None, tokens=None, cost=None,
               seat="reviewer", status="completed", reason=None):
    tokens = tokens if tokens is not None else {
        "input_tokens": 60, "output_tokens": 10}
    return SessionRecord(
        seat=seat, status=status, reason=reason, num_turns=turns,
        session_id=session_id, model="stub-model",
        tokens=tokens,
        usage={"input": tokens.get("input_tokens", 0),
               "cache_read": 0, "cache_write": 0,
               "output": tokens.get("output_tokens", 0)},
        total_cost_usd=cost, result=result,
    )


def _usable_repair(original_text, record):
    """Mirror of the parent's repair acceptance (parse + no shrink), so
    the harness knows whether a full re-run follows a scripted repair."""
    from factory.l2 import extract_verdict_marks
    if record.status != "completed":
        return False
    report = parse_verdict(record.result)
    if report.parse_error is not None:
        return False
    marks, declared = extract_verdict_marks(original_text)
    repaired = {(f.id, f.severity) for f in report.findings}
    if marks - repaired:
        return False
    return not (declared is not None and report.declared is not None
                and declared != report.declared)


class RepairSeats:
    """Scripted seats where round 1's first reviewer seat (and, when a
    scenario says so, the round's verifier) emits a malformed verdict or
    vote from a session that CAN be resumed (``session_id`` set). The
    repair turn — a runner call carrying ``resume`` — is scripted
    separately from the full re-run (a plain reviewer call that follows a
    repair the parent refused). Every other reviewer call (seat 2, later
    rounds' fan-outs, the final review) returns the base script; the
    fan-out prompt contract is asserted by the base harness untouched.
    Every scripted session reports a cost so the attempt's cost total is
    known, never poisoned to null."""

    def __init__(self, seats, seat1_result=BAD_PASS, sid=SID,
                 repairs=(), retries=(), seat2_result=None,
                 verifier_result=None, verifier_sid=None,
                 verifier_repairs=()):
        self.base = seats
        self.seat1_result = seat1_result
        self.sid = sid
        self.repairs = list(repairs)
        self.retries = list(retries)
        self.seat2_result = seat2_result
        self.verifier_result = verifier_result
        self.verifier_sid = verifier_sid
        self.verifier_repairs = list(verifier_repairs)
        self.seat_calls = 0
        self.repair_calls: list[tuple[str, str, dict]] = []
        self.retry_calls: list[str] = []
        self.retry_index = 0
        self._pending_retry = False
        self.verifier_calls = 0
        self.verifier_repair_calls: list[tuple[str, str, dict]] = []

    def _priced(self, record, cost=0.01):
        if record.total_cost_usd is None:
            record.total_cost_usd = cost
        if record.usage is None and isinstance(record.tokens, dict):
            # F-83's shape: the attempt's token_usage sums the record's
            # usage block, not the raw SDK tokens.
            record.usage = {
                "input": record.tokens.get("input_tokens", 0),
                "cache_read": 0, "cache_write": 0,
                "output": record.tokens.get("output_tokens", 0),
            }
        return record

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        resume = kwargs.get("resume")
        if seat == "reviewer" and resume is not None:
            self.repair_calls.append((resume, prompt, kwargs))
            index = len(self.repair_calls) - 1
            record = (self.repairs[index] if index < len(self.repairs)
                      else _completed(PROSE, turns=2))
            self._pending_retry = not _usable_repair(self.seat1_result,
                                                     record)
            return record
        if seat == "verifier" and resume is not None:
            self.verifier_repair_calls.append((resume, prompt, kwargs))
            index = len(self.verifier_repair_calls) - 1
            return (self.verifier_repairs[index]
                    if index < len(self.verifier_repairs)
                    else _completed(PROSE, turns=2, seat="verifier"))
        if seat == "reviewer" and self._pending_retry:
            # The full re-run of the seat whose repair was refused: the
            # same prompt (the fan-out contract), the scripted verdict.
            self._pending_retry = False
            self.retry_calls.append(prompt)
            result = self.retries[min(self.retry_index,
                                      len(self.retries) - 1)]
            self.retry_index += 1
            self.base(seat, prompt, run_dir, profile, **kwargs)
            return self._priced(_completed(result, turns=1))
        if seat == "reviewer":
            self.seat_calls += 1
            record = self.base(seat, prompt, run_dir, profile, **kwargs)
            if self.seat_calls == 1:
                record.result = self.seat1_result
                record.session_id = self.sid
            elif self.seat_calls == 2 and self.seat2_result is not None:
                record.result = self.seat2_result
            return self._priced(record)
        if seat == "verifier":
            self.verifier_calls += 1
            record = self.base(seat, prompt, run_dir, profile, **kwargs)
            if self.verifier_calls == 1 and self.verifier_result is not None:
                record.result = self.verifier_result
                record.session_id = self.verifier_sid
            return self._priced(record)
        return self._priced(self.base(seat, prompt, run_dir, profile,
                                      **kwargs))


def _trace_events(env) -> list[dict]:
    trace_dir = env.root / "trace" / "fixture"
    files = sorted(trace_dir.glob("*.jsonl"))
    assert files, "no trace written"
    events = []
    for line in files[-1].read_text().splitlines():
        events.append(json.loads(line))
    return events


def _repairs(events, seat=None):
    return [e for e in events if e.get("type") == "reviewer_repair"
            and (seat is None or e.get("seat") == seat)]


def _retries(events):
    return [e for e in events if e.get("type") == "reviewer_retry"]


def _round_report(env, attempt_no=0) -> dict:
    report = json.loads(Path(
        env.task()["attempts"][attempt_no]["validator_report"]).read_text())
    return report.get("L2-delta") or report["L2"]


# --- AC-1: one repair turn on the SAME session, then today's retry -------


def test_ac_1(tmp_path):
    from factory.orchestrator import REPAIR_TURN_CAP, REVIEWER_TURN_CAP

    # (a) The repair rescues the seat: no full re-run, the repaired
    # verdict is the seat's, the trace records reviewer_repair.
    seats = Scripted(impls=Scripted.MOVING)
    wrapped = RepairSeats(seats, repairs=[
        _completed(_verdict(), turns=2, tokens={
            "input_tokens": 500, "output_tokens": 80}, cost=0.05)])
    env = Env(tmp_path / "a")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert len(wrapped.repair_calls) == 1
    resume, prompt, kwargs = wrapped.repair_calls[0]
    assert resume == SID  # the SAME session, resumed — not a new one
    assert kwargs["max_turns"] == REPAIR_TURN_CAP  # a tight cap, not a seat's
    assert REVIEWER_TURN_CAP > REPAIR_TURN_CAP
    # The repair runs under the SEAT's own fan-out settings, so the real
    # sandbox HOME is the one holding that session's transcript.
    assert kwargs["settings_path"].name.endswith("-r1.json"), kwargs
    assert parse_verdict(BAD_PASS).parse_error in prompt  # the parse error
    assert REVIEWER_INSTRUCTION in prompt
    assert '"verdict"' in prompt and '"findings"' in prompt  # the schema
    assert not wrapped.retry_calls  # no full re-run happened
    events = _trace_events(env)
    assert len(_repairs(events, "reviewer")) == 1
    assert _repairs(events, "reviewer")[0]["outcome"] == "used"
    assert _repairs(events, "reviewer")[0]["seat_no"] == 1
    assert not _retries(events)

    # (b) A repair that returns prose falls back to today's full re-run:
    # one re-run of the same prompt, counted once, trace records both.
    seats = Scripted(impls=Scripted.MOVING)
    wrapped = RepairSeats(seats, repairs=[_completed(PROSE, turns=2)],
                          retries=[_verdict()])
    env = Env(tmp_path / "b")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert len(wrapped.repair_calls) == 1
    assert len(wrapped.retry_calls) == 1
    fanout_prompt = next(p for s, p, *_ in seats.calls if s == "reviewer")
    assert wrapped.retry_calls[0] == fanout_prompt  # same seat, same prompt
    events = _trace_events(env)
    assert _repairs(events, "reviewer")[0]["outcome"] == "fell_back"
    assert len(_retries(events)) == 1
    assert _retries(events)[0]["seat_no"] == 1

    # (c) A repair session that dies (the turn cap, an SDK error) is a
    # fallback too — today's full re-run decides.
    seats = Scripted(impls=Scripted.MOVING)
    wrapped = RepairSeats(
        seats,
        repairs=[_completed(None, turns=2, status="failed",
                            reason="error_max_turns")],
        retries=[_verdict()])
    env = Env(tmp_path / "c")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert len(wrapped.retry_calls) == 1
    events = _trace_events(env)
    assert _repairs(events, "reviewer")[0]["outcome"] == "fell_back"

    # (d) The verifier's malformed vote gets the same ONE repair turn:
    # the repaired vote is used (its drop is the recorded drop). F-96
    # gives a serious finding TWO verifier sessions (a drop needs both),
    # which would test F-96, not the repair: the finding is a MINOR —
    # one session, the repair path alone under test.
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=(_verdict(_finding(severity="minor")),
                              _verdict()))
    repaired_vote = json.dumps({"votes": [
        {"id": "U-1", "keep": False,
         "reason": "not real: impl.py already returns 42"}]})
    wrapped = RepairSeats(
        seats, seat1_result=_verdict(_finding(severity="minor")),
        seat2_result=_verdict(),
        repairs=[_verdict()], retries=[_verdict(_finding(severity="minor"))],
        verifier_result=BAD_VOTE_DROP, verifier_sid="resume-sid-v1",
        verifier_repairs=[_completed(repaired_vote, turns=2,
                                     seat="verifier")])
    env = Env(tmp_path / "d")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert len(wrapped.verifier_repair_calls) == 1
    vresume, vprompt, _ = wrapped.verifier_repair_calls[0]
    assert vresume == "resume-sid-v1"
    assert VERIFIER_INSTRUCTION in vprompt
    assert '"votes"' in vprompt  # the vote schema
    events = _trace_events(env)
    assert len(_repairs(events, "verifier")) == 1
    assert _repairs(events, "verifier")[0]["outcome"] == "used"
    # The repaired vote was USED: its drop is the verifier's recorded drop.
    round_l2 = _round_report(env)
    assert round_l2.get("verifier_dropped"), "the drop was not recorded"
    assert round_l2["verifier_dropped"][0]["finding"]["id"] == "U-1"
    assert "already returns 42" in round_l2["verifier_dropped"][0]["reason"]
    assert round_l2.get("verifier_error") is None


# --- AC-2: the repair cannot change the review ---------------------------

def test_ac_2(tmp_path):
    from factory.l2 import extract_verdict_marks, extract_vote_marks

    # The parsers are untouched — no lenient mode: the malformed shapes
    # this WO recovers from still fail the parse exactly as before.
    for bad in (BAD_PASS, BAD_FAIL_BLOCKER, BAD_FAIL_EMPTY):
        assert parse_verdict(bad).parse_error is not None
    assert parse_verifier_vote(BAD_VOTE_KEEP, _U1).error is not None
    assert parse_verifier_vote(BAD_VOTE_DROP, _U1).error is not None

    # Best-effort marks from the unparseable text.
    marks, declared = extract_verdict_marks(BAD_FAIL_BLOCKER)
    assert marks == {("R-1", "blocking")}
    assert declared == "fail"
    marks, declared = extract_verdict_marks(BAD_PASS)
    assert marks == set()
    assert declared == "pass"
    marks, declared = extract_verdict_marks(PROSE)
    assert marks == set()
    assert declared is None
    assert extract_vote_marks(BAD_VOTE_KEEP) == {"U-1": True}
    assert extract_vote_marks(BAD_VOTE_DROP) == {"U-1": False}
    assert extract_vote_marks(PROSE) == {}

    # A repair that drops a blocker is refused: the full re-run decides.
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=(_verdict(_finding()), _verdict()))
    wrapped = RepairSeats(
        seats, seat1_result=BAD_FAIL_BLOCKER,
        repairs=[_completed(_verdict(), turns=2)],  # pass, no findings
        retries=[_verdict(_finding())])  # the re-run keeps the finding
    env = Env(tmp_path / "a")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert len(wrapped.repair_calls) == 1
    assert len(wrapped.retry_calls) == 1  # the full re-run decided
    events = _trace_events(env)
    repair = _repairs(events, "reviewer")[0]
    assert repair["outcome"] == "fell_back"
    assert repair.get("shrunk") is True
    # The finding survived through the RETRY's verdict, not the repair's.
    assert any(f.get("text") == "scope creep"
               for f in _round_report(env).get("findings") or [])

    # A repair that flips the declared verdict (fail -> pass) is refused.
    seats = Scripted(impls=Scripted.MOVING, reviews=(_verdict(), _verdict()))
    wrapped = RepairSeats(
        seats, seat1_result=BAD_FAIL_EMPTY,
        repairs=[_completed(_verdict(), turns=2)],  # now declares pass
        retries=[_verdict()])
    env = Env(tmp_path / "b")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert len(wrapped.retry_calls) == 1
    events = _trace_events(env)
    assert _repairs(events, "reviewer")[0]["outcome"] == "fell_back"

    # A repair that flips a vote (keep -> drop) is refused: today's
    # vote-error path keeps every finding. (Minor finding — one verifier
    # session, as in (d): the repair is under test, not the F-96 fold.)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=(_verdict(_finding(severity="minor")),
                              _verdict()))
    repaired_vote = json.dumps({"votes": [
        {"id": "U-1", "keep": False, "reason": "changed my mind"}]})
    wrapped = RepairSeats(
        seats, seat1_result=_verdict(_finding()), seat2_result=_verdict(),
        repairs=[_verdict()], retries=[_verdict(_finding())],
        verifier_result=BAD_VOTE_KEEP, verifier_sid="resume-sid-v1",
        verifier_repairs=[_completed(repaired_vote, turns=2,
                                     seat="verifier")])
    env = Env(tmp_path / "c")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    events = _trace_events(env)
    assert _repairs(events, "verifier")[0]["outcome"] == "fell_back"
    round_l2 = _round_report(env)
    assert not round_l2.get("verifier_dropped")  # nothing was dropped
    assert round_l2.get("verifier_error")  # today's invalid-vote path

    # An obedient repair — same findings, same declared verdict, only the
    # comma fixed — is used (no re-run).
    fixed = json.dumps({"verdict": "fail", "findings": [
        {"id": "R-1", "axis": "spec", "severity": "blocking",
         "file": "impl.py", "line": 2, "text": "the defect"}]})
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=(_verdict(_finding()), _verdict()))
    wrapped = RepairSeats(seats, seat1_result=BAD_FAIL_BLOCKER,
                          repairs=[_completed(fixed, turns=2)],
                          retries=[_verdict(_finding())])
    env = Env(tmp_path / "d")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert not wrapped.retry_calls
    events = _trace_events(env)
    assert _repairs(events, "reviewer")[0]["outcome"] == "used"


# --- AC-3: the repair's tokens and turns ride the attempt ----------------


def test_ac_3(tmp_path):
    # Reviewer repair: per-seat turns and usage include the repair.
    seats = Scripted(impls=Scripted.MOVING)
    wrapped = RepairSeats(seats, repairs=[
        _completed(_verdict(), turns=2, tokens={
            "input_tokens": 500, "output_tokens": 80}, cost=0.05)])
    env = Env(tmp_path / "a")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    attempt = env.task()["attempts"][0]
    # A one-round publish: 2 fan-out seats at 1 turn each, plus the
    # repair's 2.
    assert attempt["turns_by_seat"]["reviewer"] == 4
    usage = attempt["token_usage"]
    # implementer (120/40) + 2 reviewer seats (60/10) + the repair (500/80)
    assert usage["input"] == 120 + 60 * 2 + 500
    assert usage["output"] == 40 + 10 * 2 + 80
    # every session priced 0.01, the repair 0.05
    assert abs(attempt["cost_usd"] - (0.01 * 3 + 0.05)) < 1e-9
    events = _trace_events(env)
    ends = [e for e in events if e.get("type") == "session_end"
            and e.get("seat") == "reviewer"]
    assert len(ends) == 3  # 2 sessions + the repair

    # Verifier repair: the verifier seat's numbers include it. (Minor
    # finding — one verifier session, so the seat's numbers are the
    # session's plus the repair's, nothing else.)
    seats = Scripted(impls=Scripted.MOVING,
                     reviews=(_verdict(_finding(severity="minor")),
                              _verdict()))
    repaired_vote = json.dumps({"votes": [
        {"id": "U-1", "keep": True, "reason": "confirmed",
         "tag": "important"}]})
    wrapped = RepairSeats(
        seats, seat1_result=_verdict(_finding(severity="minor")),
        seat2_result=_verdict(),
        repairs=[_verdict()], retries=[_verdict(_finding(severity="minor"))],
        verifier_result=BAD_VOTE_KEEP, verifier_sid="resume-sid-v1",
        verifier_repairs=[_completed(
            repaired_vote, turns=2, seat="verifier",
            tokens={"input_tokens": 300, "output_tokens": 40}, cost=0.02)])
    env = Env(tmp_path / "b")
    result = env.run(wrapped)
    assert result.outcome == "pr_opened"
    assert len(wrapped.verifier_repair_calls) == 1
    # Attempt 1 is the failed round that carried the verifier repair.
    attempt = env.task()["attempts"][0]
    assert attempt["turns_by_seat"]["verifier"] == 3  # 1 + the repair's 2
    # implementer + 2 reviewers + verifier (120/120/60/60 in) + repair 300
    assert attempt["token_usage"]["input"] == 120 + 60 * 2 + 60 + 300
    assert abs(attempt["cost_usd"] - (0.01 * 4 + 0.02)) < 1e-9
    events = _trace_events(env)
    assert [e for e in events if e.get("type") == "session_end"
            and e.get("seat") == "verifier"
            and e.get("turns") == 2], "the repair session was not traced"


# --- the session layer: the repair really resumes, in the seat's HOME ----


def _sandbox_repo(tmp_path: Path):
    """A minimal repo + profile for direct session-level tests (the F-38
    pattern: real git, scripted sandbox entry)."""
    import subprocess

    import yaml

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "fixture@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name",
                    "Fixture"], check=True, capture_output=True)
    (repo / "src" / "mod.py").write_text("x = 1\n")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'p'\nversion = '0'\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"],
                   check=True, capture_output=True)
    profile = {
        "name": "p",
        # F-100: the standard stack's anthropic seats are admitted.
        "providers_allowed": ["anthropic"],
        "repo": {"path": str(repo), "default_branch": "main",
                 "remote": str(tmp_path / "r.git")},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {}, "docs_allowlist": [],
    }
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile))
    return repo, profile


def _write_stub_record(settings_path, payload):
    """The scripted sandbox entry writes the record by FILE (the F-56
    transport)."""
    from factory.session import record_path_for

    record_path = record_path_for(Path(settings_path))
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(payload))


def test_repair_resumes_the_seats_own_home(tmp_path, monkeypatch):
    """The repair call runs a REAL sandboxed session whose spec carries
    ``resume`` (the session id the SDK continues — same session, not a
    fork) and whose settings are the SEAT's own fan-out settings, so the
    sandbox HOME is the one holding that seat's transcript. A repair
    that got the derivation wrong would resume (or fail) in a HOME that
    never saw the session."""
    import factory.session as session_module
    from factory.session import fanout_seat_settings, run_session_sandboxed

    repo, profile = _sandbox_repo(tmp_path)
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "t"
                        if service == "factory-zai" else None)
    specs = []

    def fake_run(cmd, run_dir, settings_path, **kwargs):
        settings_path = Path(settings_path)
        spec = json.loads((settings_path.with_name(
            settings_path.name + "-runner") / "spec.json").read_text())
        specs.append(spec)
        _write_stub_record(settings_path, {
            "seat": spec["seat"], "status": "completed",
            "session_id": "sid-original", "num_turns": 13,
            "result": "ok"})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    import subprocess

    monkeypatch.setattr(session_module, "run_sandboxed", fake_run)
    base = tmp_path / "sandbox" / "p-run.json"
    base.parent.mkdir(parents=True)
    seat_settings = fanout_seat_settings(base, "reviewer", 1)
    record = run_session_sandboxed(
        "reviewer", "repair: re-emit the verdict only", repo, profile,
        factory_root=tmp_path, settings_path=seat_settings,
        resume="sid-original", max_turns=4, timeout_min=1,
        cli_path=Path("/bin/echo"),
        config=FactoryConfig(),
    )
    assert record.status == "completed"
    assert len(specs) == 1
    # The spec the in-sandbox entry ran with: the session id continued,
    # the tight cap, the seat's own settings beside whose scratch the
    # HOME sits.
    assert specs[0]["resume"] == "sid-original"
    assert specs[0]["max_turns"] == 4
    assert specs[0]["seat"] == "reviewer"
    assert specs[0]["cwd"] == str(repo)
    # The SDK options layer: resume SET, fork OFF (the same session
    # continues — never a new id).
    from factory.session import build_options

    options = build_options(
        "reviewer", repo, 4, base_url="https://example.invalid",
        resume="sid-original")
    assert options.resume == "sid-original"
    assert options.fork_session is False
    plain = build_options("reviewer", repo, 4,
                          base_url="https://example.invalid")
    assert plain.resume is None


def test_fanout_keeps_scratch_only_when_asked(tmp_path, monkeypatch):
    """The F-38 teardown contract and its F-85 exception: default removes
    every per-session artifact; ``keep_scratch=True`` leaves exactly the
    scratch roots (the HOMES a repair resumes in) for the caller, and
    :func:`factory.session.sweep_fanout_scratch` restores the teardown."""
    import subprocess

    import factory.session as session_module
    from factory.session import (
        fanout_seat_settings,
        run_sessions_parallel,
        sweep_fanout_scratch,
    )

    repo, profile = _sandbox_repo(tmp_path)
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "t"
                        if service == "factory-zai" else None)

    def fake_run(cmd, run_dir, settings_path, **kwargs):
        _write_stub_record(Path(settings_path), {
            "seat": "reviewer", "status": "completed",
            "session_id": "sid", "result":
                '{"verdict": "pass", "findings": []}'})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(session_module, "run_sandboxed", fake_run)
    base = tmp_path / "sandbox" / "p-run.json"
    base.parent.mkdir(parents=True)

    def run_fanout(keep):
        for path in base.parent.glob("p-run.json-*"):
            path.unlink(missing_ok=True)

        def rm(path):
            import shutil

            shutil.rmtree(path, ignore_errors=True)

        [rm(p) for p in base.parent.glob("p-run.json-*")]
        return run_sessions_parallel(
            [("reviewer", "one"), ("reviewer", "two")],
            repo, profile, factory_root=tmp_path, settings_path=base,
            max_turns=2, timeout_min=1, cli_path=Path("/bin/echo"),
            keep_scratch=keep,
            config=FactoryConfig(),
        )

    records = run_fanout(True)
    assert all(r.status == "completed" for r in records)
    sandbox = base.parent
    assert sorted(p.name for p in sandbox.glob("p-run.json-r*-scratch")) == [
        "p-run.json-r1.json-scratch", "p-run.json-r2.json-scratch"]
    assert not list(sandbox.glob("p-run.json-r*.json"))  # settings gone
    assert not list(sandbox.glob("p-run.json-r*-runner"))
    # The derivation the orchestrator's repair uses names the same homes.
    assert fanout_seat_settings(base, "reviewer", 1).name == \
        "p-run.json-r1.json"
    sweep_fanout_scratch(base)
    assert not list(sandbox.glob("p-run.json-r*-scratch"))
    # Default: the F-38 contract — everything gone, sweep is a no-op.
    run_fanout(False)
    assert not list(sandbox.glob("p-run.json-r*-scratch"))
    sweep_fanout_scratch(base)  # idempotent
