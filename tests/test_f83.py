"""F-83 — Run numbers and loop lines say what happened.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f83.py::test_ac_N``
means a test function with exactly that name.

Four acceptance criteria (docs/next-work-orders.md §F-83), each from a
real JobFinderOS run (traces in ``~/factory/trace/jobfinderos/``):

- AC-1 each attempt records ``turns_by_seat`` beside ``turns``, and
  ``turns`` is the IMPLEMENTER's count (the number ``max_turns``
  bounds); the PR body's attempt line and ``factory status`` show the
  implementer's turns against the cap;
- AC-2 every session's usage is recorded whatever the outcome — an SDK
  raise after a delivered ResultMessage keeps the message's telemetry
  (F-74 AC-1's usage half, taken over), a failed/escalated/aborted
  attempt records ``token_usage``/``provider_usage`` and a ``cost_usd``
  total, ``provider_usage``'s model is never ``?`` and
  ``factory status <task>`` prints per-attempt and per-task totals;
- AC-3 a CI wait that ended red (after the one re-run) says red —
  ``factory.ci.settle`` returns ``pending`` only when checks were still
  running or unreadable at the window's end; the note, the loop line
  and the trace agree, and the in-run fix-round decision is unchanged;
- AC-4 a ``not ready`` line is printed only for the head the PR holds
  when the run ends — a sweep line about a head the same run then
  replaced is dropped.

Real vs stubbed: the F-38/F-72/F-75 split — git, the clone manager, the
queue, the L0/L1 sandboxes and the push to the local bare remote are
real; only the model seats, the CI seams and the gh read/write seams
are scripted (tests/_support/f75.py, tests/_support/f76.py,
tests/_support/f83.py).
"""

import asyncio
import json
from pathlib import Path

from _support.f72 import Seats, _finding, _task, _verdict
from _support.f75 import CI, Actions, FakeClock, ci_state
from _support.f76 import Env76
from _support.f83 import FAILED_RUN, GREEN_CHECK, LOG_TAIL, TelemetrySeats
from claude_agent_sdk import ResultError, ResultMessage

import factory.orchestrator as orch
from factory.ci import settle as ci_settle
from factory.cli import main as cli_main
from factory.config import FactoryConfig
from factory.contract import load_tasks
from factory.provider import ZAI_BASE_URL as DEFAULT_BASE_URL
from factory.session import SessionRecord, run_session
from factory.status import render_task_status

REPO = Path(__file__).resolve().parent.parent

RED = ci_state("failed", checks=((GREEN_CHECK, "failure"),))
PENDING = ci_state("pending", checks=((GREEN_CHECK, "in_progress"),))
GREEN = ci_state("success", checks=((GREEN_CHECK, "success"),))
# A PASSING round review that still carries a minor finding: the union
# has findings, so the verifier seat runs (the 140cfb shape — impl, two
# reviewers, verifier) and the round still publishes.
PASS_WITH_FINDING = json.dumps(
    {"verdict": "pass", "findings": [_finding(severity="minor")]})
# A red that stays red on every post-re-run poll — the 2026-11-14 run
# 140cfb shape: the re-run completed red between polls, so no poll ever
# SAW a non-completed check after it (R1-F2's observation gate never
# opened) and the window ended on a failed read.
STAY_RED = [RED, RED, RED]
# A stale pre-re-run failure the re-run then observably turns green —
# the flake F-75's guard exists for (the mid-window protection AC-3
# must not lose).
FLAKE_GREEN = [RED, RED, PENDING, GREEN]
# The sweep's fix-round script: the not-ready read, settle's first
# wait, the re-run's observation poll, then the settled red.
RED_SCRIPT = [RED, RED, PENDING, RED]
IMPL_FIXED = "def answer():\n    return 42  # CI fix round\n"


def _seams(monkeypatch, env: Env76, actions: Actions | None = None):
    """Patch the F-75 orchestrator seams; returns the clock."""
    clock = FakeClock()
    monkeypatch.setattr(orch, "ci_clock", clock)
    monkeypatch.setattr(orch, "ci_sleep", clock.sleep)
    monkeypatch.setattr(orch, "CIActions", lambda: actions or Actions())
    monkeypatch.setattr(orch, "gh_pr_draft", env.draft)
    return clock


def _trace_events(env: Env76, kind: str) -> list[dict]:
    """Every ``kind`` event across the project's run traces."""
    out: list[dict] = []
    for path in sorted((env.root / "trace" / "fixture").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == kind:
                out.append(event)
    return out


def _result_message(**overrides) -> ResultMessage:
    """A ResultMessage with sane defaults; overrides for the case."""
    base = dict(
        subtype="success", duration_ms=10, duration_api_ms=8,
        is_error=False, num_turns=2, session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={"glm-5.3": {"inputTokens": 100, "outputTokens": 20}},
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


def _run_session(query_fn, **kwargs):
    kwargs.setdefault("base_url", DEFAULT_BASE_URL)
    kwargs.setdefault("cwd", REPO)
    kwargs.setdefault("max_turns", 5)
    kwargs.setdefault("timeout_min", 10)
    return asyncio.run(run_session(
        "implementer", "do the task", query_fn=query_fn, **kwargs))


# ============================================================= AC-1 =========


def test_ac_1(tmp_path: Path, monkeypatch):
    """Turns per seat: ``turns`` is the implementer's count (the number
    ``max_turns`` bounds), ``turns_by_seat`` carries every seat's, old
    records without the field still load, and the PR body's attempt
    line shows the implementer's turns against the cap."""
    # (1) a passing round whose review carries a minor finding (the
    # verifier seat runs, the 140cfb shape): the attempt records
    # per-seat turns and the implementer's alone as ``turns`` — the
    # seats sum to 12+8+2 = 22, but ``turns`` is 12, the number the
    # 20-turn budget bounds.
    env = Env76(tmp_path / "turns")
    _seams(monkeypatch, env)
    seats = TelemetrySeats(
        seat_turns={"implementer": 12, "reviewer": 4, "verifier": 2},
        reviews=(PASS_WITH_FINDING,))
    result = env.run_once(seats, ci_state=CI(default=GREEN),
                          config=FactoryConfig(ci_wait_min=0))
    assert result.outcome == "pr_opened", result
    attempt = env.task()["attempts"][-1]
    assert attempt["turns"] == 12, attempt
    assert attempt["turns_by_seat"] == {
        "implementer": 12, "reviewer": 8, "verifier": 2}, attempt
    # the PR body's attempt line: the implementer's turns against the
    # budget's cap (the fixture task's max_turns is 20).
    assert "implementer turns 12 of 20" in env.prs[0].body, \
        env.prs[0].body[:400]

    # (2) unit: reviewer re-runs sum into their seat, a bare record
    # (no message delivered) renders 0 for its seat — never a crash,
    # never a forged non-int count.
    records = [
        SessionRecord(seat="implementer", status="completed", num_turns=60),
        SessionRecord(seat="reviewer", status="completed", num_turns=9),
        SessionRecord(seat="reviewer", status="failed", num_turns=11),
        SessionRecord(seat="verifier", status="failed", num_turns=None),
        SessionRecord(seat="arbiter", status="completed",
                      num_turns="many"),  # the forged shape renders 0
    ]
    by_seat = orch._turns_by_seat(records)
    assert by_seat == {"implementer": 60, "reviewer": 20, "verifier": 0,
                       "arbiter": 0}, by_seat

    # (3) old records without the field still load (the queue schema
    # keeps the field optional) and render through the per-task page.
    old = _task()
    old["attempts"] = [{
        "run_id": "20261112T184652Z-b6bbdc", "slice_id": "T-1",
        "started": "2026-11-12T18:46:52Z", "ended": "2026-11-12T19:00:00Z",
        "outcome": "pass", "rounds": 1, "turns": 31,
        "input_tokens": 100, "output_tokens": 10,
        "prompt_versions": {"implementer": "h"}, "skills": [],
    }]
    env_old = Env76(tmp_path / "old", tasks=[old])
    queue = load_tasks(env_old.queue_path)  # schema-validates on load
    assert queue["tasks"][0]["attempts"][0]["turns"] == 31
    page = render_task_status(queue, "T-1", project="fixture")
    assert "31 of 20" in page and "pre-F-83" in page, page

    # (4) the per-task page shows the new-shape attempt's implementer
    # turns against the cap too.
    page = render_task_status(load_tasks(env.queue_path), "T-1",
                              project="fixture")
    assert "12 of 20" in page, page


# ============================================================= AC-2 =========


def test_ac_2(tmp_path: Path, monkeypatch):
    """Every session's usage is recorded, whatever the outcome. An SDK
    raise after a delivered ResultMessage keeps the message's turns,
    model, usage and cost (the f49bb7 verifier's slip, F-74 AC-1's
    usage half); an aborted attempt records its sessions' usage and
    cost; provider_usage's model is the seat's resolved (or reported)
    model, never ``?``; the attempt carries a cost_usd total that goes
    unknown — never silently lower — when a session reported no cost;
    ``factory status <task>`` prints per-attempt and per-task totals."""
    # (1) the SDK raises AFTER delivering an error ResultMessage: the
    # record is built from the message (status failed, the reason, the
    # turns/model/usage/cost), not the bare pre-F-83 shape.
    message = _result_message(
        subtype="error_max_turns", is_error=True, num_turns=60,
        usage={"input_tokens": 47710, "output_tokens": 6110,
               "cache_read_input_tokens": 5288704,
               "cache_creation_input_tokens": 0},
        total_cost_usd=0.6632224)

    async def raise_after_result(*, prompt, options, transport=None):
        yield message
        raise ResultError(
            "Claude Code returned an error result: Reached maximum "
            "number of turns (60)", exit_code=1)

    record = _run_session(raise_after_result)
    assert record.status == "failed", record
    assert record.num_turns == 60, record
    assert record.model == "glm-5.3", record
    assert record.usage == {"input": 47710, "cache_read": 5288704,
                            "cache_write": 0, "output": 6110}, record
    assert record.total_cost_usd == 0.6632224, record
    assert "error_max_turns" in (record.reason or ""), record

    # a raise with NO message delivered keeps today's bare record — its
    # usage renders unknown downstream, never 0.
    async def raise_only(*, prompt, options, transport=None):
        raise ResultError("transport died before any result", exit_code=1)
        yield  # pragma: no cover — makes this an async generator

    record = _run_session(raise_only)
    assert record.status == "failed"
    assert record.usage is None and record.num_turns is None

    # (2) an attempt that ABORTS on quota records the session that did
    # run: token_usage, provider_usage, cost_usd and the implementer's
    # turns — the budget's own number.
    env = Env76(tmp_path / "quota")
    _seams(monkeypatch, env)
    seats = TelemetrySeats(
        impl_failed=("failed", "API error: 429 rate limit reached"),
        seat_turns={"implementer": 7},
        seat_costs={"implementer": 1.25})
    result = env.run_once(seats, ci_state=CI(default=GREEN),
                          config=FactoryConfig(ci_wait_min=0))
    assert result.outcome == "quota_exhausted", result
    attempt = env.task()["attempts"][-1]
    assert attempt["outcome"] == "aborted", attempt
    assert attempt["token_usage"]["input"] == 120, attempt
    assert attempt["cost_usd"] == 1.25, attempt
    assert attempt["turns"] == 7, attempt
    assert attempt["turns_by_seat"] == {"implementer": 7}, attempt
    assert attempt["provider_usage"], attempt
    assert all(row["model"] != "?" for row in attempt["provider_usage"]), \
        attempt["provider_usage"]

    # (3) an attempt that ESCALATES after its sessions record them too
    # (the exhausted round carries implementer + reviewers + verifier).
    env = Env76(tmp_path / "exhaust", tasks=[_task(
        "T-1", budget={"size": "S", "max_attempts": 1, "max_turns": 20,
                       "wall_clock_min": 30})])
    _seams(monkeypatch, env)
    seats = TelemetrySeats(reviews=(_verdict(_finding()),))
    result = env.run_once(seats, ci_state=CI(default=GREEN),
                          config=FactoryConfig(ci_wait_min=0))
    assert result.outcome == "needs_human", result
    assert result.escalation == "attempts_exhausted", result
    attempt = env.task()["attempts"][-1]
    assert attempt["outcome"] == "escalated", attempt
    assert attempt["token_usage"]["input"] > 0, attempt
    assert all(row["model"] != "?" for row in attempt["provider_usage"]), \
        attempt["provider_usage"]

    # (4) a passing attempt (verifier seat included, the (1) shape)
    # sums every seat's cost; one session without a cost makes the
    # total unknown (None), never a partial sum.
    env = Env76(tmp_path / "costs")
    _seams(monkeypatch, env)
    seats = TelemetrySeats(
        reviews=(PASS_WITH_FINDING,),
        seat_costs={"implementer": 1.5, "reviewer": 0.25, "verifier": 0.1})
    assert env.run_once(seats, ci_state=CI(default=GREEN),
                        config=FactoryConfig(ci_wait_min=0)).outcome \
        == "pr_opened"
    attempt = env.task()["attempts"][-1]
    assert attempt["cost_usd"] == 2.1, attempt
    env = Env76(tmp_path / "partial-cost")
    _seams(monkeypatch, env)
    seats = TelemetrySeats(seat_costs={"implementer": 1.5})  # seats unknown
    assert env.run_once(seats, ci_state=CI(default=GREEN),
                        config=FactoryConfig(ci_wait_min=0)).outcome \
        == "pr_opened"
    attempt = env.task()["attempts"][-1]
    assert attempt["cost_usd"] is None, attempt

    # (5) unit: the provider_usage model chain — the parent-stamped
    # block's model, else the session's reported model, else unknown;
    # never ``?`` (the 140cfb rows all read `zai ?`).
    records = [
        SessionRecord(seat="implementer", status="completed",
                      usage={"input": 1, "cache_read": 0, "cache_write": 0,
                             "output": 1},
                      provider={"kind": "zai", "model": "glm-4.7"}),
        SessionRecord(seat="reviewer", status="completed", model="opus-5",
                      usage={"input": 1, "cache_read": 0, "cache_write": 0,
                             "output": 1},
                      provider={"kind": "anthropic"}),  # block names no model
        SessionRecord(seat="verifier", status="completed", model="opus-5",
                      usage={"input": 1, "cache_read": 0, "cache_write": 0,
                             "output": 1}),  # no block: primary, no model
        SessionRecord(seat="arbiter", status="failed"),  # nothing at all
    ]
    rows = orch._attempt_provider_usage(
        records, {"kind": "zai", "endpoint_host": "api.z.ai"})
    assert [(row["kind"], row["model"]) for row in rows] == [
        ("zai", "glm-4.7"), ("anthropic", "opus-5"),
        ("zai", "opus-5"), ("zai", "unknown")], rows
    assert "?" not in json.dumps(rows)

    # (6) `factory status <task>`: per-attempt rows and a task total;
    # unknown tokens/cost render unknown, never 0.
    queue = {"version": 1, "project": "fixture", "tasks": [_task(
        "T-9", attempts=[
            {"run_id": "r1", "slice_id": "T-9",
             "started": "2026-11-14T18:00:00Z",
             "ended": "2026-11-14T18:26:00Z", "outcome": "pass",
             "rounds": 1, "turns": 60, "turns_by_seat": {
                 "implementer": 60, "reviewer": 29, "verifier": 3},
             "input_tokens": 226868, "output_tokens": 61170,
             "token_usage": {"input": 226868, "cache_read": 4005248,
                             "cache_write": 0, "output": 61170},
             "cost_usd": 3.2,
             "prompt_versions": {}, "skills": []},
            {"run_id": "r2", "slice_id": "T-9",
             "started": "2026-11-14T13:45:30Z",
             "ended": "2026-11-14T14:10:00Z", "outcome": "fail",
             "rounds": 1, "turns": 32, "turns_by_seat": {
                 "implementer": 32, "reviewer": 28, "verifier": 0},
             "input_tokens": 80080, "output_tokens": 31194,
             "token_usage": {"input": None, "cache_read": None,
                             "cache_write": None, "output": None},
             "cost_usd": None,
             "prompt_versions": {}, "skills": []},
        ])]}
    page = render_task_status(queue, "T-9", project="fixture")
    assert "r1" in page and "r2" in page, page
    assert "60 of 20" in page and "32 of 20" in page, page
    assert "$3.20" in page, page
    assert "unknown" in page, page  # r2's usage and the task totals
    assert "Task total" in page, page

    # through the CLI: `factory status <task>` prints the page; an
    # unknown task exits 1 with a message, never a traceback.
    doc = env.tmp / "cli-queue"
    doc.mkdir()
    path = doc / "tasks.json"
    path.write_text(json.dumps(queue, indent=2) + "\n")
    code = cli_main(["status", "T-9", "--queue", str(path),
                     "--factory-root", str(env.root)])
    assert code == 0
    code = cli_main(["status", "nope", "--queue", str(path),
                     "--factory-root", str(env.root)])
    assert code == 1


# ============================================================= AC-3 =========


def test_ac_3(tmp_path: Path, monkeypatch):
    """A CI wait that ended red says red. ``settle`` returns red when
    the last completed read at the window's end was red after the one
    re-run (the 140cfb shape: the re-run completed red between polls,
    so the observation gate never opened); pending only when checks
    were still running or unreadable at the window's end; the flake
    guard's mid-window protection and the in-run fix-round decision are
    unchanged (a stale failure the re-run observably turns green stays
    green)."""
    actions = Actions(failed_default=[FAILED_RUN], logs={101: LOG_TAIL})

    # (1) unit: red on every post-re-run poll, window ends on a failed
    # read — RED (today this is the false "pending"), with the failing
    # checks and their tails.
    clock = FakeClock()
    heads: set = set()
    settled = ci_settle(
        "remote", "a" * 40, ci_state=CI(default=list(STAY_RED)),
        actions=actions, window_sec=60, clock=clock, sleep=clock.sleep,
        rerun_heads=heads)
    assert settled.outcome == "red", settled
    assert [f.name for f in settled.failing] == [GREEN_CHECK], settled
    assert "second wait: failed" in settled.detail, settled.detail
    assert actions.rerun_calls == [101], actions.rerun_calls

    # (2) unit: checks still running at the window's end — pending.
    clock = FakeClock()
    settled = ci_settle(
        "remote", "b" * 40,
        ci_state=CI(default=ci_state(
            "pending", checks=((GREEN_CHECK, "in_progress"),))),
        actions=actions, window_sec=60, clock=clock, sleep=clock.sleep,
        rerun_heads=set())
    assert settled.outcome == "pending", settled
    assert settled.note == "CI pending", settled

    # (3) unit: unreadable at the window's end — pending, never green.
    from factory.publish import PublishError

    clock = FakeClock()
    settled = ci_settle(
        "remote", "c" * 40,
        ci_state=CI(default=PublishError("gh is not installed")),
        actions=actions, window_sec=60, clock=clock, sleep=clock.sleep,
        rerun_heads=set())
    assert settled.outcome == "pending", settled
    assert settled.note == "CI pending", settled

    # (4) unit: the flake — a stale failure the re-run OBSERVABLY turns
    # green mid-window — stays green (F-75's guard, mid-window half).
    clock = FakeClock()
    flake_actions = Actions(failed_default=[FAILED_RUN],
                            logs={101: LOG_TAIL})
    settled = ci_settle(
        "remote", "d" * 40, ci_state=CI(default=list(FLAKE_GREEN)),
        actions=flake_actions, window_sec=600, clock=clock,
        sleep=clock.sleep, rerun_heads=set())
    assert settled.outcome == "green", settled
    assert flake_actions.rerun_calls == [101], flake_actions.rerun_calls

    # (5) run-level, the 140cfb shape: red on both heads, the second
    # wait's window ends on failed reads — the run takes its fix round
    # (today it printed "CI pending" and stopped), exhausts to
    # ci_failed, and the trace's ci_settled events say red.
    env = Env76(tmp_path / "stay-red", tasks=[_task(
        "T-1", budget={"size": "S", "max_attempts": 2, "max_turns": 20,
                       "wall_clock_min": 30})])
    run_actions = Actions(failed_default=[FAILED_RUN], logs={101: LOG_TAIL})
    _seams(monkeypatch, env, run_actions)
    seats = Seats(impl_files=[{}, {"impl.py": IMPL_FIXED}])
    result = env.run_once(seats, ci_state=CI(default=list(STAY_RED)),
                          config=FactoryConfig(ci_wait_min=1))
    assert result.outcome == "needs_human", result
    assert result.escalation == "ci_failed", result
    assert result.note is None, result.note
    assert len(seats.prompts("implementer")) == 2, \
        "the red after its re-run must start the fix round"
    attempts = env.task()["attempts"]
    assert attempts[-1]["escalation"] == "ci_failed", attempts[-1]
    assert "CI pending" not in (attempts[-1].get("note") or "")
    events = _trace_events(env, "ci_settled")
    assert events and all(e["outcome"] == "red" for e in events), events

    # (6) the in-run decision is unchanged for the settled-mid-window
    # red: the fix round still starts on the SAME publish (F-75's
    # behavior — the sweep-started and in-run rounds keep today's
    # trigger, tests/test_f75.py's arms stay green).
    env = Env76(tmp_path / "settled-red", tasks=[_task(
        "T-1", budget={"size": "S", "max_attempts": 2, "max_turns": 20,
                       "wall_clock_min": 30})])
    run_actions = Actions(failed_default=[FAILED_RUN], logs={101: LOG_TAIL})
    _seams(monkeypatch, env, run_actions)
    seats = Seats(impl_files=[{}, {"impl.py": IMPL_FIXED}])
    result = env.run_once(seats, ci_state=CI(default=list(RED_SCRIPT)),
                          config=FactoryConfig(ci_wait_min=5))
    assert result.outcome == "needs_human", result
    assert result.escalation == "ci_failed", result
    assert len(seats.prompts("implementer")) == 2


# ============================================================= AC-4 =========


def test_ac_4(tmp_path: Path, monkeypatch):
    """No stale line: a ``not ready`` line is printed only for the head
    the PR holds when the run ends. The sweep's line about the OLD head
    of the PR a fix round then replaced is dropped; a line about a PR
    the run did not touch survives; a fix round that never publishes
    (the old head still stands) keeps its line."""
    # (1) two red PRs, one fix round: the fixed PR's stale not-ready
    # line is dropped (today it printed beside the new head's publish);
    # the other red PR's line stays.
    env = Env76(
        tmp_path / "two", tasks=[_task("T-1"), _task("T-2")],
        profile_extra={"repo": {"concurrency": 2}})
    _seams(monkeypatch, env, Actions(failed_default=[FAILED_RUN],
                                     logs={101: LOG_TAIL}))
    seats = Seats(impl_files=[{}, {}, {"impl.py": IMPL_FIXED}])
    assert env.run_once(seats, ci_state=CI(default=GREEN)).outcome \
        == "pr_opened"  # T-1's publish
    assert env.run_once(seats, ci_state=CI(default=GREEN)).outcome \
        == "pr_opened"  # T-2's publish
    t1_url = env.task("T-1")["links"]["pr"]
    t2_url = env.task("T-2")["links"]["pr"]
    heads = {t: env.remote_branch_sha(env.task(t)["links"]["branch"])
             for t in ("T-1", "T-2")}
    ci = CI(by_sha={heads["T-1"]: list(RED_SCRIPT),
                    heads["T-2"]: list(RED_SCRIPT)}, default=GREEN)
    result = env.run_once(seats, ci_state=ci,
                          config=FactoryConfig(ci_wait_min=5))
    assert result.outcome == "pr_opened", result
    assert len(seats.prompts("implementer")) == 3, \
        "two publishes plus exactly one sweep fix round"
    # the fix round replaced T-1's head: its sweep line about the OLD
    # head must be gone; T-2's line (a PR this run never touched) stays.
    assert not any(t1_url in line for line in result.not_ready_prs), \
        result.not_ready_prs
    assert any(t2_url in line and "CI red" in line
               for line in result.not_ready_prs), result.not_ready_prs
    # the drop is OBSERVABLE, not silent: the run's trace names the PR
    # and how many lines went.
    drops = [e for e in _trace_events(env, "not_ready_dropped")
             if e.get("pr") == t1_url]
    assert drops and drops[-1]["dropped"] >= 1, drops

    # (2) the control: a fix round that never publishes (its review
    # failed, the budget spent) leaves the OLD head standing — the
    # not-ready line about that head is still true and still printed.
    env = Env76(tmp_path / "kept", tasks=[_task(
        "T-1", budget={"size": "S", "max_attempts": 2, "max_turns": 20,
                       "wall_clock_min": 30})])
    _seams(monkeypatch, env, Actions(failed_default=[FAILED_RUN],
                                     logs={101: LOG_TAIL}))
    seats = Seats(impl_files=[{}, {"impl.py": IMPL_FIXED}],
                  reviews=(_verdict(), _verdict(_finding())))
    assert env.run_once(seats, ci_state=CI(default=GREEN)).outcome \
        == "pr_opened"
    url = env.task("T-1")["links"]["pr"]
    head = env.remote_branch_sha(env.task("T-1")["links"]["branch"])
    result = env.run_once(seats, ci_state=CI(
        by_sha={head: list(RED_SCRIPT)}, default=GREEN),
        config=FactoryConfig(ci_wait_min=5))
    assert result.outcome == "needs_human", result
    assert env.remote_branch_sha(env.task("T-1")["links"]["branch"]) == head, \
        "the control never replaced the head"
    assert any(url in line and "CI red" in line
               for line in result.not_ready_prs), result.not_ready_prs
    assert len(seats.prompts("implementer")) == 2, \
        "the publish plus the (failed) fix round"
