"""F-30 acceptance tests — BUILD-ORDER.md, work order F-30.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f30.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed (the f10 harness pattern):

* **Real:** the fixture project's git repo and bare "GitHub" remote, L0/L1
  through the real pinned srt sandbox, the queue/state machinery, and —
  new in F-30 — the trace writer, the sandboxed stdout event transport and
  the ``factory trace`` CLI.
* **Stubbed:** the model. Seat stubs carry ``run_session_sandboxed``'s
  signature; the session-level tests drive ``run_session`` with fake SDK
  streams (``query_fn``) exactly as the f06/f27 tests do.
"""

import asyncio
import contextlib
import io
import json
import os
import shlex
import time as time_mod
from pathlib import Path
from types import SimpleNamespace

import pytest
from _support.f30 import (
    REF,
    Env,
    Seats,
    _task,
)
from _support.tokens import fake_token
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from factory import trace as trace_module
from factory.cli import main as cli_main
from factory.orchestrator import reclaim_runs
from factory.runs import cleanup_run, create_run
from factory.sandbox import build_settings, run_sandboxed, write_settings
from factory.session import SessionRecord, run_session
from factory.state import TaskQueue
from factory.trace import TraceWriter, trace_path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_ID = "20260101T000000Z-ab12cd"
RUN_ID_2 = "20260101T000001Z-cd34ef"

REVIEW_FAIL = json.dumps({
    "verdict": "fail",
    "findings": [{"axis": "spec", "severity": "blocking", "file": "impl.py",
                  "line": 1, "text": "answer() computes 42 by hardcoding"}],
})
# The seat token for the redaction tests: deliberately NOT shaped like any
# F-25 credential pattern, so only the token-literal scrub can catch it.
# Built at runtime (F-58 AC-G): the shape — prefix + 24-char body, 35
# chars — is preserved; no high-entropy literal lives in the source.
SEAT_TOKEN = fake_token("f30-seat")

EVENT_TYPES = {"phase_start", "phase_end", "tool_call", "gate_check",
               "session_end", "run_end",
               # F-75: the publish-path CI wait's events — a per-poll
               # note (window ended, unreadable state) and the settled
               # verdict with its polls/detail.
               "ci_wait", "ci_settled"}
PHASES = {"setup", "implementer", "L0", "L1", "L2", "publish"}


# ----------------------------------------------------------------- fixtures --


def _one_attempt_task() -> dict:
    task = _task()
    task["budget"]["max_attempts"] = 1
    return task


def _result_message(**overrides):
    """A ResultMessage with sane defaults (the f06 helper shape)."""
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={},
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


def _use(use_id: str, name: str, tool_input: dict) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=use_id, name=name, input=tool_input)],
        model="glm-5.3", message_id=f"m-{use_id}",
    )


def _result(use_id: str, content, is_error: bool = False) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=use_id, content=content, is_error=is_error)]
    )


def _sandbox_write_refused(env: "Env") -> None:
    """AC-1's session-side assertion: a write to the trace path from inside
    a session sandbox (real pinned srt) fails — the trace lives outside
    every clone and outside ``allowWrite``."""
    run = create_run("fixture", env.repo, factory_root=env.root)
    target = trace_path(env.root, "fixture", run.run_id)
    try:
        settings = build_settings(env.profile, run.root, model_endpoint="https://api.z.ai")
        write_settings(settings, run.settings_path)
        proc = run_sandboxed(
            ["/bin/sh", "-c", f"echo leaked >> {shlex.quote(str(target))}"],
            run.root, run.settings_path, timeout=60,
        )
        assert not target.exists(), "a session wrote to the trace path"
        assert proc.returncode != 0
    finally:
        assert not target.exists()
        cleanup_run(run)


# --------------------------------------------------- AC-1: one file per run --


def test_ac_1(tmp_path: Path):
    """One JSONL event log per run, outside every clone and outside the
    sandbox write allowlist; the full event vocabulary is present on a
    passing run and every event is fully identified."""
    env = Env(tmp_path)
    result = env.run(Seats())
    assert result.outcome == "pr_opened", result
    attempt = env.task()["attempts"][0]

    path = env.trace_path(attempt["run_id"])
    assert path == env.root / "trace" / "fixture" / f"{attempt['run_id']}.jsonl"
    assert (env.root / "runs") not in path.parents  # outside every clone
    lines = path.read_text().splitlines()
    assert lines  # one event per line, each line one event
    events = [json.loads(line) for line in lines]
    assert len(events) == len(lines)

    # Every event: run id, task id, attempt number, sequence number, type.
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    for event in events:
        assert event["type"] in EVENT_TYPES, event
        assert event["run_id"] == attempt["run_id"]
        assert event["task_id"] == "T-1"
        assert isinstance(event["attempt"], int) and event["attempt"] >= 1
        assert event["ts"]

    # The six phases of a passing round, in order, paired start/end.
    phase_events = [e for e in events if e["type"].startswith("phase_")]
    assert {e["phase"] for e in phase_events} == PHASES
    starts = [e["phase"] for e in phase_events if e["type"] == "phase_start"]
    ends = [e["phase"] for e in phase_events if e["type"] == "phase_end"]
    assert starts == ["setup", "implementer", "L0", "L1", "L2", "publish"] == ends

    # gate_check rows carry layer, item, ok and a note of what was looked at.
    gates = [e for e in events if e["type"] == "gate_check"]
    assert gates
    for gate in gates:
        assert gate["layer"] in {"L0", "L1", "L2"}
        assert isinstance(gate["item"], str) and gate["item"]
        assert isinstance(gate["ok"], bool)
        assert isinstance(gate["note"], str) and gate["note"]
    assert {g["layer"] for g in gates} == {"L0", "L1", "L2"}

    # session_end rows carry the record's spend, turns and model per seat.
    # F-38: the review is a two-session fan-out (this passing verdict has
    # no findings, so no verifier session runs).
    sessions = [e for e in events if e["type"] == "session_end"]
    assert [s["seat"] for s in sessions] == [
        "implementer", "reviewer", "reviewer",
    ]
    for session in sessions:
        assert session["status"] == "completed"
        assert isinstance(session["tokens"], dict)
        assert session["tokens"]["input_tokens"] > 0
        assert isinstance(session["turns"], int)
        assert session["model"] == "stub-model"
        assert "cost_usd" in session

    # The run ends with a terminal marker for --follow.
    assert events[-1]["type"] == "run_end"
    assert events[-1]["outcome"] == "published"

    # And the path itself is not writable from a session (real srt).
    _sandbox_write_refused(env)


# -------------------------------------------- AC-2: written as they happen --


def test_ac_2(tmp_path: Path):
    """A tool_call line is on disk before the stream yields its
    ResultMessage — events cross the sink as they happen, not at session
    end."""
    writer = TraceWriter(tmp_path, "fixture", RUN_ID, "T-1")
    seen: dict = {}

    async def _query(*, prompt, options, transport=None):
        yield _use("t1", "Bash", {"command": "pytest -q"})
        yield _result("t1", "3 passed in 0.01s")
        # The tool_call event must already be on disk HERE: the result
        # message has not been yielded.
        text = writer.path.read_text() if writer.path.exists() else ""
        seen["tool_call"] = [json.loads(line) for line in text.splitlines()]
        yield _result_message()

    record = asyncio.run(run_session(
        "implementer", "do the task", REPO_ROOT, 5, 10,
        query_fn=_query, base_url="https://api.z.ai/api/anthropic", token="t",
        trace_sink=writer.append,
    ))
    assert record.status == "completed"
    calls = [e for e in seen["tool_call"] if e["type"] == "tool_call"]
    assert len(calls) == 1, seen["tool_call"]
    call = calls[0]
    assert call["seat"] == "implementer"
    assert call["tool"] == "Bash"
    assert "pytest -q" in call["args"]
    assert "3 passed" in call["result"]
    assert call["ok"] is True
    assert call["started"] and call["ended"]


# ------------------------------------------------- AC-3: gate event evidence --


def test_ac_3(tmp_path: Path):
    """Gate events carry evidence on pass and on fail: L0 rows per command
    and sentinel, L1 rows per red/green proof, L2 rows per finding — each
    with a note saying what was looked at."""
    # --- pass: every layer's rows are ok with substantive notes -----------
    env = Env(tmp_path / "pass")
    result = env.run(Seats())
    assert result.outcome == "pr_opened", result
    events = env.trace_events(env.task()["attempts"][0]["run_id"])
    gates = [e for e in events if e["type"] == "gate_check"]
    assert all(g["ok"] for g in gates), gates
    by_layer = {layer: [g for g in gates if g["layer"] == layer]
                for layer in ("L0", "L1", "L2")}
    # L0: one row per command (setup, test) plus the sentinel and diff rows.
    assert {"setup", "test"} <= {g["item"] for g in by_layer["L0"]}
    assert any(g["item"].startswith("sentinel:") for g in by_layer["L0"])
    assert any(g["item"] == "diff_policy" for g in by_layer["L0"])
    # L1: the red and green proof of the contract ref, each naming the ref.
    l1_items = {g["item"] for g in by_layer["L1"]}
    assert l1_items == {f"red:{REF}", f"green:{REF}"}
    for gate in by_layer["L1"]:
        assert REF in gate["note"]
    # L2: the verdict row.
    assert any(g["item"] == "verdict" for g in by_layer["L2"])

    # --- L0 fails: its failing command is a gate_check with ok=false ------
    env = Env(tmp_path / "l0", tasks=[_one_attempt_task()])
    result = env.run(Seats(answer=41))  # the test command fails
    assert result.outcome == "needs_human", result
    events = env.trace_events(env.task()["attempts"][0]["run_id"])
    failed = [g for g in events if g["type"] == "gate_check" and not g["ok"]]
    assert any(g["layer"] == "L0" and g["item"] == "test" for g in failed), failed

    # --- L1 fails: the red proof that proved nothing is a row ok=false ----
    def proves_nothing(prompt, run_dir, kwargs):
        (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
        # Already green on base with only the test files: proves nothing.
        (run_dir / "tests" / "test_feature.py").write_text(
            "def test_ac_1():\n    assert True\n")
        return SessionRecord(
            seat="implementer", status="completed", num_turns=2, model="stub-model",
            tokens={"input_tokens": 120, "output_tokens": 40}, result="done",
        )

    env = Env(tmp_path / "l1", tasks=[_one_attempt_task()])
    result = env.run(Seats(implementer=proves_nothing))
    assert result.outcome == "needs_human", result
    events = env.trace_events(env.task()["attempts"][0]["run_id"])
    failed = [g for g in events if g["type"] == "gate_check" and not g["ok"]]
    assert any(g["layer"] == "L1" and g["item"] == f"red:{REF}" for g in failed), failed

    # --- L2 fails: each finding is one row ok=false carrying its text -----
    env = Env(tmp_path / "l2", tasks=[_one_attempt_task()])
    result = env.run(Seats(review=REVIEW_FAIL))
    assert result.outcome == "needs_human", result
    events = env.trace_events(env.task()["attempts"][0]["run_id"])
    failed = [g for g in events if g["type"] == "gate_check" and not g["ok"]]
    l2_rows = [g for g in failed if g["layer"] == "L2"]
    assert any("hardcoding" in g["note"] for g in l2_rows), l2_rows


# ------------------------------------------------ AC-4: secrets never land --


def test_ac_4(tmp_path: Path):
    """The seat token value and credential-shaped strings (F-25 patterns)
    are replaced with [redacted] before any line is written, and args and
    result snippets are length-capped."""
    writer = TraceWriter(tmp_path, "fixture", RUN_ID, "T-1")

    async def _query(*, prompt, options, transport=None):
        # The tool args echo the seat token; the result carries both the
        # token and a GitHub-token-shaped string.
        yield _use("t1", "Bash", {
            "command": f"echo $ANTHROPIC_AUTH_TOKEN  # {SEAT_TOKEN}",
            "pad": "x" * 5000,
        })
        yield _result(
            "t1",
            f"token is {SEAT_TOKEN} and key ghp_{'0123456789' * 4}",
            is_error=True,
        )
        yield _result_message()

    asyncio.run(run_session(
        "implementer", "do the task", REPO_ROOT, 5, 10,
        query_fn=_query, base_url="https://api.z.ai/api/anthropic", token=SEAT_TOKEN,
        trace_sink=writer.append,
    ))

    text = writer.path.read_text()
    assert SEAT_TOKEN not in text
    assert "ghp_" not in text
    assert "[redacted]" in text
    events = [json.loads(line) for line in text.splitlines()]
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["ok"] is False  # the is_error result
    assert len(call["args"]) < 5000  # the 5000-char arg value was capped
    assert len(call["result"]) < 300
    for line in text.splitlines():
        assert len(line) < 10_000


# ----------------------------------- AC-5: tracing never changes a run ------


_VOLATILE = {"run_id", "started", "ended", "validator_report"}


def test_ac_5(tmp_path: Path, capsys):
    """A trace write that fails is reported once and the run continues
    unchanged: the attempt record is identical to an untraced run's."""
    # The untraced baseline.
    clean = Env(tmp_path / "clean")
    result = clean.run(Seats())
    assert result.outcome == "pr_opened", result
    baseline = clean.task()["attempts"][0]

    # Every write raises: the trace dir exists but is unwritable.
    broken = Env(tmp_path / "broken")
    trace_dir = broken.root / "trace" / "fixture"
    trace_dir.mkdir(parents=True)
    trace_dir.chmod(0o555)
    try:
        result = broken.run(Seats())
    finally:
        trace_dir.chmod(0o755)
    assert result.outcome == "pr_opened", result

    sabotaged = broken.task()["attempts"][0]
    def norm(attempt):
        return {k: v for k, v in attempt.items() if k not in _VOLATILE}

    assert norm(sabotaged) == norm(baseline)

    # The failure was reported exactly once.
    stderr = capsys.readouterr().err
    reports = [line for line in stderr.splitlines() if "trace" in line]
    assert len(reports) == 1, stderr


# ------------------------------------------------------ AC-6: the reader ---


def _write_fixture_trace(root: Path, *, run_id: str = RUN_ID,
                         terminal: bool = True) -> list[str]:
    writer = TraceWriter(root, "fixture", run_id, "T-1")
    writer.append({"type": "phase_start", "phase": "setup"})
    writer.append({"type": "tool_call", "seat": "implementer", "tool": "Bash",
                   "args": "pytest -q", "result": "1 failed", "ok": False,
                   "started": "2026-10-23T10:00:00Z", "ended": "2026-10-23T10:00:01Z"})
    if terminal:
        writer.run_end("published")
    return writer.path.read_text().splitlines()


def test_ac_6(tmp_path: Path, capsys, monkeypatch):
    """``factory trace <run-id>`` prints the trace in order, one line per
    event; ``--follow`` keeps printing until the attempt ends; it reads
    only."""
    env = Env(tmp_path)
    lines = _write_fixture_trace(env.root, terminal=True)

    code = cli_main([
        "trace", RUN_ID, "--queue", str(env.queue_path),
        "--factory-root", str(env.root),
    ])
    assert code == 0
    assert capsys.readouterr().out.splitlines() == lines

    # A run id with no trace is a clean refusal.
    code = cli_main([
        "trace", "19990101T000000Z-none", "--queue", str(env.queue_path),
        "--factory-root", str(env.root),
    ])
    assert code == 1
    assert "trace" in capsys.readouterr().err

    # --follow keeps printing new events until the attempt ends.
    lines = _write_fixture_trace(env.root, run_id=RUN_ID_2, terminal=False)
    path = env.trace_path(RUN_ID_2)
    # Reads only: the file's bytes and the directory listing are untouched
    # by reading (snapshotted before the CLI runs).
    before = path.read_text()
    listing = sorted(p.name for p in (env.root / "trace" / "fixture").iterdir())
    assert listing == [f"{RUN_ID}.jsonl", f"{RUN_ID_2}.jsonl"]
    real_sleep = time_mod.sleep
    slept = {"n": 0}

    def fake_sleep(seconds):
        slept["n"] += 1
        if slept["n"] == 1:  # mid-follow: the run finishes
            path.open("a").write(json.dumps({
                "type": "run_end", "outcome": "failed", "seq": 3,
                "ts": "2026-10-23T10:00:02Z", "run_id": RUN_ID_2, "task_id": "T-1",
                "attempt": 1,
            }) + "\n")
        real_sleep(0.01)

    monkeypatch.setattr(trace_module, "time", SimpleNamespace(sleep=fake_sleep))
    code = cli_main([
        "trace", RUN_ID_2, "--follow", "--queue", str(env.queue_path),
        "--factory-root", str(env.root),
    ])
    monkeypatch.undo()
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3 and "run_end" in out[-1]
    assert slept["n"] >= 1  # it really waited before the event landed

    # Reads only: the file holds exactly what was written into it (the two
    # fixture events plus the appended terminal event), and the directory
    # listing is unchanged — reading created nothing.
    assert path.read_text().startswith(before)
    assert len(path.read_text().splitlines()) == 3
    assert sorted(p.name for p in (env.root / "trace" / "fixture").iterdir()) == listing


# ------------------------------------------- AC-7: one source of spend truth --


def test_ac_7(tmp_path: Path):
    """Per-phase token totals in the trace sum to the attempt's token
    totals in tasks.json — no second source of truth for spend."""
    env = Env(tmp_path)
    result = env.run(Seats())
    assert result.outcome == "pr_opened", result
    attempt = env.task()["attempts"][0]
    events = env.trace_events(attempt["run_id"])

    phase_totals = [
        e["tokens"] for e in events
        if e["type"] == "phase_end" and e.get("tokens")
    ]
    # The two session phases (implementer, L2) carry the seats' spend —
    # with F-38 the L2 phase holds the fan-out's two reviewer sessions.
    assert {e["phase"] for e in events
            if e["type"] == "phase_end" and e.get("tokens")} == {"implementer", "L2"}
    assert sum(t["input_tokens"] for t in phase_totals) == attempt["input_tokens"]
    assert sum(t["output_tokens"] for t in phase_totals) == attempt["output_tokens"]
    assert attempt["input_tokens"] == 240 and attempt["output_tokens"] == 60


# --------------------------------------- beyond the ACs: the live transport --


def test_open_tool_calls_are_traced_when_the_session_dies(tmp_path: Path):
    """A stream that dies mid-call (aborted, crashed) still traces the
    calls it opened: started, no result, ok unknown — an aborted turn's
    evidence rides out with everything else."""
    from claude_agent_sdk import ClaudeSDKError

    async def _query(*, prompt, options, transport=None):
        yield _use("t1", "Bash", {"command": "pytest -q"})
        raise ClaudeSDKError("boom")

    events: list[dict] = []
    record = asyncio.run(run_session(
        "implementer", "do the task", REPO_ROOT, 5, 10,
        query_fn=_query, base_url="https://api.z.ai/api/anthropic", token="t",
        trace_sink=events.append,
    ))
    assert record.status == "failed"
    calls = [e for e in events if e["type"] == "tool_call"]
    assert len(calls) == 1, events
    assert calls[0]["tool"] == "Bash"
    assert calls[0]["result"] is None and calls[0]["ok"] is None
    assert calls[0]["ended"] is None and calls[0]["started"]


def test_one_terminal_marker_per_run(tmp_path: Path):
    """A run whose rounds fail and retry has ONE run_end, at its true end:
    failed rounds that continue to the next attempt are not terminal, or
    --follow would stop while the run is still working."""
    env = Env(tmp_path)
    result = env.run(Seats(answer=41))  # three failed rounds
    assert result.outcome == "needs_human", result
    events = env.trace_events(env.task()["attempts"][-1]["run_id"])
    ends = [e for e in events if e["type"] == "run_end"]
    assert len(ends) == 1
    assert ends[0]["outcome"] == "failed"
    assert events[-1]["type"] == "run_end"  # the last line of the run
    # every round's events still carry their own attempt number
    assert {e["attempt"] for e in events} == {1, 2, 3}


def test_sandboxed_stdout_events_cross_the_pipe(tmp_path: Path):
    """The sandbox transport halves: prefixed event lines ride the runner's
    stdout live (run_sandboxed hands each line out as it arrives), and the
    orchestrator-side forwarder scrubs the seat token, ignores foreign
    lines and passes the event on."""
    from factory.session import stdout_trace_sink, trace_line_forwarder

    received: list[dict] = []
    forward = trace_line_forwarder(received.append, secret=SEAT_TOKEN)
    forward(f'factory-trace {{"type": "tool_call", "args": "echo {SEAT_TOKEN}"}}')
    forward("not a trace line")  # the record line, stray output: ignored
    forward("factory-trace {not json")  # garbage: ignored, never raised
    assert len(received) == 1
    assert SEAT_TOKEN not in received[0]["args"]
    assert received[0]["args"] == "echo [redacted]"

    # The in-sandbox sink prints exactly one prefixed, flushed line.
    sink = stdout_trace_sink()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        sink({"type": "tool_call", "tool": "Bash"})
    assert buffer.getvalue() == 'factory-trace {"type": "tool_call", "tool": "Bash"}\n'


def test_run_sandboxed_streams_lines_to_the_callback(tmp_path: Path):
    """run_sandboxed's on_stdout_line sees complete lines as they arrive
    (before the child exits), and the captured output is unchanged."""
    from _support.srt import write_srt

    fake = write_srt(
        tmp_path / "fake-srt", version="0.0.79",
        body=(
            'process.stdout.write("factory-trace {\\"a\\": 1}\\n");\n'
            'process.stdout.write("{\\"seat\\": \\"implementer\\"}\\n");\n'
            "process.exit(0);\n"
        ),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    settings = tmp_path / "settings.json"
    write_settings({"network": {"allowedDomains": [], "deniedDomains": []},
                    "filesystem": {"denyRead": [], "allowRead": [], "allowWrite": [],
                                   "denyWrite": []}}, settings)
    seen: list[str] = []
    proc = run_sandboxed(["true"], run_dir, settings, timeout=30,
                         srt_bin_path=fake, on_stdout_line=seen.append)
    # Every complete line, prefixed or not (filtering is the callback's
    # business — the sandbox layer knows no protocol), in arrival order.
    assert seen == ['factory-trace {"a": 1}', '{"seat": "implementer"}']
    assert 'factory-trace {"a": 1}' in proc.stdout
    assert '"seat": "implementer"' in proc.stdout


# --------------------------------------------- PR #45 review: 8 findings ---

# Forged-stamp fixture: a sandbox-side event trying to pass itself off as
# orchestrator text. Only its tool_call half may cross the forwarder, and
# the writer's stamps must win over any key it carries.
_FORGED_STAMPS = {"seq": 99, "ts": "1970-01-01T00:00:00Z", "run_id": RUN_ID_2,
                  "task_id": "T-9", "attempt": 9}


def test_review1_sandbox_events_cannot_forge_stamps_or_terminal(tmp_path: Path):
    """Only tool_call crosses the sandbox boundary, and the writer's
    seq/ts/run/task/attempt stamps win over event payloads: nothing inside
    a sandbox can forge a terminal marker or re-stamp an event."""
    from factory.session import trace_line_forwarder

    received: list[dict] = []
    forward = trace_line_forwarder(received.append, secret=SEAT_TOKEN)
    forged_end = json.dumps(
        {"type": "run_end", "outcome": "published", **_FORGED_STAMPS})
    forged_session = json.dumps(
        {"type": "session_end", "reason": "forged", **_FORGED_STAMPS})
    forged_call = json.dumps(
        {"type": "tool_call", "tool": "Bash", **_FORGED_STAMPS})
    forward(f"factory-trace {forged_end}")
    forward(f"factory-trace {forged_session}")
    forward("factory-trace {not json")
    forward(f"factory-trace {forged_call}")
    assert [event["type"] for event in received] == ["tool_call"]

    writer = TraceWriter(tmp_path, "fixture", RUN_ID, "T-1")
    for event in received:
        writer.append(event)
    events = [json.loads(line) for line in writer.path.read_text().splitlines()]
    assert [event["type"] for event in events] == ["tool_call"]
    call = events[0]
    assert call["seq"] == 1
    assert call["ts"] != _FORGED_STAMPS["ts"]
    assert call["run_id"] == RUN_ID
    assert call["task_id"] == "T-1"
    assert call["attempt"] == 1


def test_review2_plain_read_prints_events_after_run_end(tmp_path: Path, capsys):
    """A non-follow read prints the whole file — a run_end line (cleanup
    events can follow one) never truncates `factory trace <run-id>`."""
    writer = TraceWriter(tmp_path, "fixture", RUN_ID, "T-1")
    writer.append({"type": "phase_start", "phase": "publish"})
    writer.run_end("published")
    writer.append({"type": "phase_end", "phase": "publish"})
    lines = writer.path.read_text().splitlines()

    trace_module.read_trace(writer.path)
    assert capsys.readouterr().out.splitlines() == lines


def test_review3_follow_stops_when_no_run_end_can_come(tmp_path: Path, capsys,
                                                       monkeypatch):
    """--follow stops (and says why) when the run can no longer write its
    terminal marker — the run dir gone for a grace period (killed run,
    disabled writer) or the trace file itself pruned under the reader."""
    root = tmp_path / "root"
    _write_fixture_trace(root, terminal=False)
    path = trace_path(root, "fixture", RUN_ID)
    real_sleep = time_mod.sleep
    slept = {"n": 0}

    def fake_sleep(seconds):
        slept["n"] += 1
        real_sleep(0.01)

    monkeypatch.setattr(trace_module, "GONE_POLLS", 2)
    monkeypatch.setattr(trace_module, "time", SimpleNamespace(sleep=fake_sleep))
    # The run dir is already gone: after the grace polls the reader gives
    # up instead of polling forever.
    trace_module.read_trace(path, follow=True, live=lambda: False)
    out = capsys.readouterr()
    assert len(out.out.splitlines()) == 2  # the fixture's two events printed
    assert "no run_end" in out.err
    assert slept["n"] >= 1  # it really polled before giving up

    # A live run resets the gone counter: it never stops on the grace
    # clock while alive, and stops only once it actually dies.
    state = {"live": True}
    slept["n"] = 0

    def alive_then_die(seconds):
        slept["n"] += 1
        if slept["n"] > trace_module.GONE_POLLS:
            state["live"] = False
        real_sleep(0.01)

    monkeypatch.setattr(trace_module, "time",
                        SimpleNamespace(sleep=alive_then_die))
    trace_module.read_trace(path, follow=True, live=lambda: state["live"])
    assert slept["n"] > trace_module.GONE_POLLS  # outlived the grace window
    out = capsys.readouterr()
    assert len(out.out.splitlines()) == 2 and "no run_end" in out.err

    # The trace file itself deleted mid-follow (retention under the reader).
    slept["n"] = 0

    def delete_during_sleep(seconds):
        slept["n"] += 1
        if slept["n"] == 1:
            path.unlink()
        real_sleep(0.01)

    monkeypatch.setattr(trace_module, "time",
                        SimpleNamespace(sleep=delete_during_sleep))
    trace_module.read_trace(path, follow=True)
    out = capsys.readouterr()
    assert "removed" in out.err


def test_review4_operator_stop_traces_aborted_not_failed(tmp_path: Path):
    """Ctrl-C during a run traces run_end aborted (an operator stop), not
    failed — metrics read failed as "the factory or the agent broke"."""

    def interrupted(prompt, run_dir, kwargs):
        raise KeyboardInterrupt

    env = Env(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        env.run(Seats(implementer=interrupted))
    traces = sorted((env.root / "trace" / "fixture").glob("*.jsonl"))
    assert len(traces) == 1
    events = [json.loads(line) for line in traces[0].read_text().splitlines()]
    assert events[-1]["type"] == "run_end"
    assert events[-1]["outcome"] == "aborted"


def test_review5_trace_files_are_private_and_expire(tmp_path: Path):
    """Trace files are owner-only (0600 under a 0700 dir) — they carry
    capped snippets of everything a run read and wrote — and do not live
    forever: prune_traces/reclaim_runs age them out past the retention
    window while fresh ones stay."""
    from factory.trace import prune_traces

    env = Env(tmp_path)
    result = env.run(Seats())
    assert result.outcome == "pr_opened", result
    fresh = env.trace_path(env.task()["attempts"][0]["run_id"])
    assert fresh.stat().st_mode & 0o777 == 0o600
    assert (env.root / "trace" / "fixture").stat().st_mode & 0o777 == 0o700

    stale = env.trace_path(RUN_ID)
    writer = TraceWriter(env.root, "fixture", RUN_ID, "T-1")
    writer.append({"type": "phase_start", "phase": "setup"})
    old = time_mod.time() - (trace_module.RETENTION_DAYS + 1) * 86400
    os.utime(stale, (old, old))

    assert prune_traces(env.root, "fixture") == [stale]
    assert not stale.exists() and fresh.exists()

    # reclaim_runs prunes on its own (the lease-held housekeeping path).
    os.utime(fresh, (old, old))
    reclaim_runs(TaskQueue(env.queue_path), "fixture", env.root)
    assert not fresh.exists()


def test_review6_pump_handles_lines_split_across_chunks(tmp_path: Path):
    """A line arriving in several reads is delivered once, whole — the
    pump accumulates the partial without recopying it per chunk."""
    from _support.srt import write_srt

    fake = write_srt(
        tmp_path / "fake-srt", version="0.0.79",
        body=(
            'process.stdout.write("factory-trace {\\"a\\": ");\n'
            "setTimeout(() => {\n"
            '  process.stdout.write("1}\\n");\n'
            '  process.stdout.write("{\\"seat\\": \\"implementer\\"}\\n");\n'
            "  process.exit(0);\n"
            "}, 400);\n"
        ),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    settings = tmp_path / "settings.json"
    write_settings({"network": {"allowedDomains": [], "deniedDomains": []},
                    "filesystem": {"denyRead": [], "allowRead": [], "allowWrite": [],
                                   "denyWrite": []}}, settings)
    seen: list[str] = []
    run_sandboxed(["true"], run_dir, settings, timeout=30,
                  srt_bin_path=fake, on_stdout_line=seen.append)
    assert seen == ['factory-trace {"a": 1}', '{"seat": "implementer"}']


def test_review7_follow_stops_on_a_killed_run_and_waits_on_a_held_lease(
        tmp_path: Path, capsys, monkeypatch):
    """Review round 2: a SIGKILLed orchestrator leaves its run dir behind
    (only the next ``loop`` reclaims it), so "run dir exists" is no live
    signal. The run lease is: the OS releases ``<queue>.run.lock`` the
    moment its holder dies. --follow stops once the lease is free, and
    keeps waiting while a holder has it — without ever taking it."""
    import fcntl

    env = Env(tmp_path)
    _write_fixture_trace(env.root, run_id=RUN_ID_2, terminal=False)
    # The killed run's dir is still on disk; nothing holds the lease.
    (env.root / "runs" / "fixture" / RUN_ID_2).mkdir(parents=True)
    real_sleep = time_mod.sleep
    slept = {"n": 0}

    def guarded_sleep(seconds):
        slept["n"] += 1
        if slept["n"] > 200:
            raise AssertionError("--follow is still polling a killed run")
        real_sleep(0.001)

    monkeypatch.setattr(trace_module, "GONE_POLLS", 3)
    monkeypatch.setattr(trace_module, "time", SimpleNamespace(sleep=guarded_sleep))
    code = cli_main([
        "trace", RUN_ID_2, "--follow", "--queue", str(env.queue_path),
        "--factory-root", str(env.root),
    ])
    out = capsys.readouterr()
    assert code == 0
    assert len(out.out.splitlines()) == 2 and "no run_end" in out.err
    assert slept["n"] <= 10  # stopped on the grace window, not the guard

    # A live holder (another open file description, as another process
    # would have) keeps it following; releasing the lease ends it.
    lock_path = env.queue_path.with_name(env.queue_path.name + ".run.lock")
    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    slept["n"] = 0

    def held_then_released(seconds):
        slept["n"] += 1
        if slept["n"] == 20:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        if slept["n"] > 200:
            raise AssertionError("--follow never noticed the lease was released")
        real_sleep(0.001)

    monkeypatch.setattr(trace_module, "time", SimpleNamespace(sleep=held_then_released))
    try:
        code = cli_main([
            "trace", RUN_ID_2, "--follow", "--queue", str(env.queue_path),
            "--factory-root", str(env.root),
        ])
    finally:
        holder.close()
    out = capsys.readouterr()
    assert code == 0 and "no run_end" in out.err
    assert slept["n"] >= 20  # waited while the lease was held

    # Probing never takes the lease in a way a run would notice: the lock
    # file's holder line is untouched, and a missing lock file reads as
    # "no run" without being created.
    lock_path.write_text("T-1")
    from factory.state import run_lease_held
    assert run_lease_held(env.queue_path) is False
    assert lock_path.read_text() == "T-1"
    lock_path.unlink()
    assert run_lease_held(env.queue_path) is False
    assert not lock_path.exists()
