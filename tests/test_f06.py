"""F-06 — session runner acceptance (BUILD-ORDER F-06, contract in
.factory/tasks.json).

The runner wraps ``claude_agent_sdk.query()`` per seat, inside the F-05
sandbox: per-seat options (setting_sources=[], Z.ai env, tool lists),
in-process PreToolUse hooks, a wall clock around the message stream, and a
SessionRecord distilled from the ResultMessage. ``query`` is injected, so
AC-1/2/5 (and AC-3's callback half) run against fakes. The real-session
halves of AC-3/AC-4 spend money on the Z.ai model, so since F-51 (m-21)
they run ONLY on the explicit ``FACTORY_LIVE_TESTS=1`` opt-in — never
inferred from a credential being present (an org runner standardizing on
the Z.ai gateway must not silently flip them to real spend). Without the
opt-in they skip with a reason naming the variable; with the opt-in and
no credential they fail loudly. The refusal-without-credential half is
mandatory and hermetic on every machine (the credential lookup is
monkeypatched to None): a credential that must never leave the Mac is not
a broken stack, and the refusal is real, tested behaviour.
"""

import ast
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

import pytest
from _support import live_session_gate
from claude_agent_sdk import CLINotFoundError, ResultMessage

from factory.session import (
    DEFAULT_BASE_URL,
    SEATS,
    SessionConfigError,
    SessionRecord,
    bash_guard_hook,
    build_options,
    protected_path_hook,
    run_session,
    run_session_sandboxed,
    seat_token,
    session_env,
)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers --

def _result(**overrides) -> ResultMessage:
    """A ResultMessage with sane defaults; overrides for the case under test."""
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={
            "glm-5.3": {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.001,
                "contextWindow": 200000,
                "maxOutputTokens": 8192,
            }
        },
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


def _results_query(*messages):
    """An injected query that yields exactly ``messages`` then ends."""

    async def _query(*, prompt, options, transport=None):
        for message in messages:
            yield message

    return _query


def _run(query_fn, **kwargs):
    kwargs.setdefault("cwd", REPO)
    kwargs.setdefault("max_turns", 5)
    kwargs.setdefault("timeout_min", 10)
    seat = kwargs.pop("seat", "implementer")
    return asyncio.run(run_session(seat, "do the task", query_fn=query_fn, **kwargs))


def _pre_input(tool: str, tool_input: dict) -> dict:
    return {
        "session_id": "s",
        "transcript_path": "t",
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "u1",
    }


def _decision(callback, tool: str, tool_input: dict) -> str | None:
    """Run one hook callback; return its permissionDecision (None = no opinion)."""
    out = asyncio.run(callback(_pre_input(tool, tool_input), None, {"signal": None}))
    specific = out.get("hookSpecificOutput") or {}
    return specific.get("permissionDecision")


# ------------------------------------------------------- F-06 AC-1: clock --

def test_ac_1():
    """A query that never finishes is cut off at the wall clock and reported
    as ``aborted`` — even though it keeps yielding messages, so the clock
    bounds the *stream*, not just silence."""

    async def hanging_query(*, prompt, options, transport=None):
        while True:
            yield None  # non-ResultMessage frames the runner must skip over
            await asyncio.sleep(0.01)

    started = time.monotonic()
    record = _run(hanging_query, timeout_min=0.05)
    elapsed = time.monotonic() - started
    assert record.status == "aborted", record
    assert record.reason == "wall_clock"
    # The clock fired near its 3s deadline — not instantly, not unbounded.
    assert 2.0 <= elapsed < 15.0, elapsed


# ------------------------------------------- F-06 AC-2: failures recorded --

def test_ac_2():
    """Error results and SDK exceptions are recorded as failed attempts with
    a reason; none of them crashes the runner."""

    cases = [
        (_result(subtype="error_max_turns", is_error=True), "error_max_turns"),
        (
            _result(subtype="error_during_execution", is_error=True),
            "error_during_execution",
        ),
        # is_error with an API status: the reason must carry the status.
        (_result(subtype="success", is_error=True, api_error_status=429), "429"),
    ]
    for message, expected_reason in cases:
        record = _run(_results_query(message))
        assert record.status == "failed", (record, expected_reason)
        assert expected_reason in (record.reason or ""), record

    # An SDK exception mid-stream is recorded, not raised.
    async def exploding_query(*, prompt, options, transport=None):
        raise CLINotFoundError("Claude Code not found")
        yield  # pragma: no cover — makes this an async generator

    record = _run(exploding_query)
    assert record.status == "failed"
    assert "CLINotFoundError" in record.reason


# ----------------------------------------------- F-06 AC-3: blocking hooks --

def test_ac_3(tmp_path: Path, monkeypatch):
    """The protected-path hook denies Write/Edit/NotebookEdit on protected
    paths (unit half on the callback), the runner refuses to start
    without a credential (refusal half — hermetic on every machine since
    F-51/m-21), and a REAL session in a REAL sandbox runs only on the
    explicit ``FACTORY_LIVE_TESTS=1`` opt-in — a credential being present
    never turns the live half on.
    """
    hook = protected_path_hook(["schema/**", ".git/config"])
    callback = hook.hooks[0]
    assert hook.matcher == "Write|Edit|NotebookEdit"

    # Deny matrix: each edit tool, relative and absolute forms.
    deny_cases = [
        ("Write", {"file_path": "schema/tasks.schema.json"}),
        ("Edit", {"file_path": "/repo/schema/project.schema.json"}),
        ("NotebookEdit", {"file_path": "schema/notebook.ipynb"}),
        ("Edit", {"file_path": ".git/config"}),
        ("Write", {"file_path": "/repo/.git/config"}),
    ]
    for tool, tool_input in deny_cases:
        assert _decision(callback, tool, tool_input) == "deny", (tool, tool_input)

    # Unprotected paths get no opinion ({} → the tool proceeds).
    assert _decision(callback, "Write", {"file_path": "src/app.py"}) is None
    assert _decision(callback, "Read", {"file_path": "schema/tasks.schema.json"}) is None

    # The bash guard denies push/gh/config-gh, allows ordinary commands.
    guard = bash_guard_hook().hooks[0]
    for command in [
        "git push origin main",
        "git  push",
        "gh pr create",
        "/usr/bin/gh auth login",
        "cat ~/.config/gh/hosts.yml",
        "git config --file ~/.config/gh/hosts.yml user.name x",
    ]:
        assert _decision(guard, "Bash", {"command": command}) == "deny", command
    for command in ["git status", "git commit -m x", "echo gh-gh-gh", "ls .config"]:
        assert _decision(guard, "Bash", {"command": command}) is None, command

    # --- refusal half (mandatory, hermetic since F-51/m-21) ------------------
    # The CI shape on EVERY machine: with no credential the runner must
    # refuse loudly rather than spawn a session that cannot authenticate.
    # Monkeypatched to None so the owner's real keychain item cannot answer
    # first — and so the refusal is proven even with the live opt-in set.
    import factory.session as session_mod

    monkeypatch.setattr(session_mod, "seat_token", lambda: None)
    with pytest.raises(SessionConfigError, match="factory-zai"):
        run_session_sandboxed(
            seat="implementer",
            prompt="blocked",
            run_dir=tmp_path,
            profile={},
            factory_root=tmp_path,
            max_turns=2,
            timeout_min=1,
            protected=[".git/config"],
            token=None,
            srt_bin_path=None,
        )
    monkeypatch.undo()

    # --- live half (explicit opt-in only; never credential-inferred) ---------
    live_session_gate()
    have_token = seat_token() is not None
    if not have_token:
        pytest.fail(
            "FACTORY_LIVE_TESTS=1 is set but no factory-zai credential is "
            "available: the opted-in live session cannot authenticate. Loud "
            "by design — never a silent skip. (F-51/m-21)"
        )

    # The owner's Mac: a real session in a real sandbox is ordered to
    # create a file at a protected path. If the hook failed, the Write
    # would succeed and the file would exist — a positive control, which
    # is why the target is an ordinary new file rather than something the
    # sandbox also denies (a backstop would mask a hook failure). The
    # 2026-10-24 flake (INC-004) proved the old attempt evidence —
    # num_turns >= 2 — was no evidence at all: the model can simply never
    # call Write. The attempt is now proven directly by the F-30 trace tap
    # (verified empirically 2026-10-24): a PreToolUse deny closes its
    # tool_call event with ok False and result "PreToolUse:Write hook
    # error: … matches protected path 'protected.md'", with the file_path
    # in args resolved to its absolute sandbox path. The bounded retry
    # below treats "model didn't try" as a retried outcome, not a flake.
    from factory.runs import cleanup_run, create_run
    from factory.sandbox import ensure_srt

    ensure_srt()

    sessions_used: list[int] = []

    def session(attempt: int):
        """One fresh sandboxed session, its trace events, and whether the
        write landed (checked before cleanup deletes the run dir)."""
        sessions_used.append(attempt)
        run = create_run(f"f06-ac3-{attempt}", REPO, factory_root=tmp_path)
        try:
            events: list[dict] = []
            record = run_session_sandboxed(
                seat="implementer",
                prompt=(
                    "Create a file named protected.md in the working "
                    "directory, containing the single word hello. Use the "
                    "Write tool for this."
                ),
                run_dir=run.root,
                profile={},
                factory_root=tmp_path,
                max_turns=4,
                timeout_min=3,
                protected=["protected.md"],
                trace_sink=events.append,
            )
            return record, events, (run.root / "protected.md").exists()
        finally:
            cleanup_run(run)

    denied = _expect_hook_denied_write(session, sessions=3)
    assert denied["tool"] == "Write"
    assert denied["ok"] is False
    assert _HOOK_DENY_TEXT in denied["result"]
    # The run's own evidence line (shown with pytest -rP/-s): how many
    # sessions the budget burned and the deny text that ended them.
    print(
        f"[f06-ac3] {len(sessions_used)} session(s); "
        f"denied result: {denied['result']}"
    )


# ------------------------------- F-06 AC-3: the bounded-retry helper (fakes) --
# The loop behind the real half, unit-tested with fakes so CI (no token)
# still proves all of its outcomes. A session that ends without the model
# attempting the Write is retried (fresh session, own run) up to a budget;
# the file existing, or a Write of it being allowed, is the hard failure;
# no denied Write inside the budget is the inconclusive failure — the loop
# never passes without direct evidence of a denial.


# The protected-path hook's own deny text (factory.session's _deny reason:
# "<path> matches protected path '<pattern>'") — a denial is an event whose
# result carries it. Any other Write failure (quota, disk) is not proof.
_HOOK_DENY_TEXT = "matches protected path"


def _expect_hook_denied_write(
    session_fn: Callable[[int], tuple[SessionRecord, list[dict], bool]],
    *,
    filename: str = "protected.md",
    sessions: int = 3,
) -> dict:
    """Run real-half sessions until one shows the hook denying the Write.

    Cost: every session is a real sandboxed model call, so the budget is
    money — the worst case is ``sessions`` of them (3 by default: three
    short sessions, tens of seconds and well under a dollar apiece on the
    2026-10 stack) before the inconclusive failure.

    Args:
        session_fn: Starts one fresh sandboxed session (the 1-based attempt
            number, so each session gets its own run) and returns the
            session's record, the F-30 tool_call events its trace_sink
            collected, and whether the target file existed when the session
            ended — captured before cleanup removes the run dir.
        filename: The file the sessions are ordered to Write; a Write
            matches when the parsed ``file_path``'s basename is it.
        sessions: The total session budget before declaring inconclusive.

    Returns:
        The denied tool_call event — tool Write, ``filename`` as the parsed
        file_path's name, ``ok`` False, and the hook's deny text in the
        result: direct evidence the hook (not something else) denied it.

    Raises:
        AssertionError: ``hook did not deny the write`` when the file
            exists after a session or a Write of it was allowed (the
            positive control failed — checked before any denial in the
            same session, for the hook cannot have both); ``inconclusive``
            when no completed session produced a denied Write inside the
            budget. Only a ``completed`` session is a valid attempt: a
            session that ended in error is retried, and the inconclusive
            message names those statuses instead of claiming the model
            never attempted anything.
    """

    def writes(events: list[dict]) -> list[dict]:
        """The tool_call events of a Write aimed at filename — args parsed
        as JSON, the file_path's basename compared, never a substring
        match ("unprotected.md" contains "protected.md"). An unparsable
        snippet (truncated past the path) is not a match."""
        out = []
        for event in events:
            if event.get("type") != "tool_call" or event.get("tool") != "Write":
                continue
            try:
                file_path = json.loads(str(event.get("args") or "{}")).get(
                    "file_path", ""
                )
            except json.JSONDecodeError:
                continue
            if Path(str(file_path)).name == filename:
                out.append(event)
        return out

    summary: list[str] = []
    for attempt in range(1, sessions + 1):
        record, events, target_exists = session_fn(attempt)
        if target_exists:
            raise AssertionError(
                f"hook did not deny the write: {filename} exists after "
                f"session {attempt} of {sessions}"
            )
        if record.status != "completed":
            # Not a valid attempt: an errored or aborted session's stream
            # is partial evidence, so its events are not scanned (the
            # existence check above still applies — a written file is a
            # written file however the session ended). The status lands in
            # the summary so the inconclusive message says why.
            summary.append(
                record.status + (f":{record.reason}" if record.reason else "")
            )
            continue
        writes_here = writes(events)
        if any(e.get("ok") is True for e in writes_here):
            raise AssertionError(
                f"hook did not deny the write: session {attempt} shows an "
                f"allowed Write of {filename} ({writes_here[0]})"
            )
        denied = next(
            (
                e
                for e in writes_here
                if e.get("ok") is False and _HOOK_DENY_TEXT in str(e.get("result") or "")
            ),
            None,
        )
        if denied is not None:
            return denied
        # ok None (a call the stream never closed — an aborted session's
        # pending flush) or an attempt denied by nothing in particular:
        # a fresh session. Both still count as attempted, so the final
        # message never claims the model didn't try when it did.
        summary.append("attempted" if writes_here else "no attempt")
    if summary and all(entry == "no attempt" for entry in summary):
        raise AssertionError(
            f"inconclusive: the model never attempted the Write in "
            f"{sessions} sessions"
        )
    raise AssertionError(
        f"inconclusive: no denied Write of {filename} within {sessions} "
        f"sessions (sessions: {', '.join(summary)})"
    )


def test_ac3_retry_refuses_twice_then_denied_passes():
    """The 2026-10-24 flake shape, then success: two sessions end without
    the model attempting the Write, the third attempts it and the hook
    denies — the loop passes on the denied event (direct evidence) and
    returns it."""
    calls: list[int] = []
    denied_event = {
        "type": "tool_call",
        "tool": "Write",
        "args": '{"file_path": "protected.md", "content": "hello"}',
        "ok": False,
        "result": "PreToolUse:deny: protected.md matches protected path",
    }

    def session(attempt: int):
        calls.append(attempt)
        if attempt < 3:
            return SessionRecord(seat="implementer", status="completed"), [], False
        return (
            SessionRecord(seat="implementer", status="completed"),
            [denied_event],
            False,
        )

    returned = _expect_hook_denied_write(session)
    assert returned is denied_event
    assert calls == [1, 2, 3]


def test_ac3_retry_never_attempted_is_inconclusive():
    """Three sessions, none attempting the Write — and neither a Bash call
    nor a Write aimed at another file counts as an attempt: the loop fails
    with the distinct never-attempted message, never a silent pass."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        events = [
            {"type": "tool_call", "tool": "Bash",
             "args": '{"command": "ls"}', "ok": True},
            {"type": "tool_call", "tool": "Write",
             "args": '{"file_path": "other.md"}', "ok": True},
        ]
        return SessionRecord(seat="implementer", status="completed"), events, False

    with pytest.raises(
        AssertionError,
        match="inconclusive: the model never attempted the Write in 3 sessions",
    ):
        _expect_hook_denied_write(session)
    assert calls == [1, 2, 3]


def test_ac3_retry_created_file_fails_immediately():
    """The hard failure: the file exists after a session — the hook did not
    deny the write — and the loop fails on the spot without spending the
    remaining sessions."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        return SessionRecord(seat="implementer", status="completed"), [], True

    with pytest.raises(AssertionError, match="hook did not deny the write"):
        _expect_hook_denied_write(session)
    assert calls == [1]


def test_ac3_retry_allowed_write_fails_immediately():
    """A Write of the target the trace shows as allowed (ok True), file or
    no file, is a broken positive control — hard failure, no retry, no
    pass."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        events = [
            {"type": "tool_call", "tool": "Write",
             "args": '{"file_path": "protected.md", "content": "hello"}',
             "ok": True, "result": "wrote 1 line"},
        ]
        return SessionRecord(seat="implementer", status="completed"), events, False

    with pytest.raises(AssertionError, match="hook did not deny the write"):
        _expect_hook_denied_write(session)
    assert calls == [1]


# --- owner review round on the flake fix: five findings, each red-first. ---


def test_ac3_retry_unrelated_write_error_does_not_pass():
    """Finding 1: a Write of the target that fails for a reason that is not
    the hook (here: a disk quota error) is not a denial — the loop must
    not pass on it. It still counts as an attempt, so the outcome is the
    inconclusive failure, never a silent pass."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        events = [
            {"type": "tool_call", "tool": "Write",
             "args": '{"file_path": "protected.md", "content": "hello"}',
             "ok": False, "result": "Write failed: disk quota exceeded"},
        ]
        return SessionRecord(seat="implementer", status="completed"), events, False

    with pytest.raises(AssertionError, match="inconclusive"):
        _expect_hook_denied_write(session)
    assert calls == [1, 2, 3]


def test_ac3_retry_target_matched_by_parsed_file_path():
    """Finding 2: the target is matched by parsing args as JSON and
    comparing Path(file_path).name — not by substring. An allowed Write of
    ``unprotected.md`` (whose name contains "protected.md" as a substring)
    is not a target write: no hard failure, just the never-attempted
    inconclusive after the budget."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        events = [
            {"type": "tool_call", "tool": "Write",
             "args": '{"file_path": "unprotected.md", "content": "hi"}',
             "ok": True, "result": "wrote 1 line"},
        ]
        return SessionRecord(seat="implementer", status="completed"), events, False

    with pytest.raises(
        AssertionError,
        match="inconclusive: the model never attempted the Write in 3 sessions",
    ):
        _expect_hook_denied_write(session)
    assert calls == [1, 2, 3]


def test_ac3_retry_allowed_write_wins_over_later_denial():
    """Finding 3: an allowed Write of the target is a hard failure even
    when the same session also shows a denial — the hook cannot have both
    denied and allowed the same file. The allowed check runs first."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        events = [
            {"type": "tool_call", "tool": "Write",
             "args": '{"file_path": "protected.md", "content": "hello"}',
             "ok": True, "result": "wrote 1 line"},
            {"type": "tool_call", "tool": "Write",
             "args": '{"file_path": "protected.md", "content": "hello"}',
             "ok": False,
             "result": "PreToolUse:Write hook error: protected.md matches "
                       "protected path 'protected.md'"},
        ]
        return SessionRecord(seat="implementer", status="completed"), events, False

    with pytest.raises(AssertionError, match="hook did not deny the write"):
        _expect_hook_denied_write(session)
    assert calls == [1]


def test_ac3_retry_denial_in_a_failed_session_does_not_pass():
    """Finding 4: only a completed session is a valid attempt. A session
    that ended in error (here: a 429) carrying a perfectly shaped denial
    event must not pass — its stream is partial evidence. The loop retries,
    and the inconclusive message names the error statuses rather than
    claiming the model never attempted anything."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        events = [
            {"type": "tool_call", "tool": "Write",
             "args": '{"file_path": "protected.md", "content": "hello"}',
             "ok": False,
             "result": "PreToolUse:Write hook error: protected.md matches "
                       "protected path 'protected.md'"},
        ]
        return (
            SessionRecord(seat="implementer", status="failed", reason="api_error_429"),
            events,
            False,
        )

    with pytest.raises(AssertionError, match="inconclusive.*failed.*api_error_429"):
        _expect_hook_denied_write(session)
    assert calls == [1, 2, 3]


def test_ac3_retry_budget_bounds_and_is_documented():
    """Finding 5: the session budget is an explicit parameter of the loop —
    a budget of 2 runs exactly two sessions — and the helper's docstring
    discloses the worst-case cost (each session is a real sandboxed model
    call, so the budget is money)."""
    calls: list[int] = []

    def session(attempt: int):
        calls.append(attempt)
        return SessionRecord(seat="implementer", status="completed"), [], False

    with pytest.raises(
        AssertionError,
        match="inconclusive: the model never attempted the Write in 2 sessions",
    ):
        _expect_hook_denied_write(session, sessions=2)
    assert calls == [1, 2]
    assert "worst case" in (_expect_hook_denied_write.__doc__ or "")


# ----------------------------------------------- F-06 AC-4: real smoke run --

def test_ac_4(tmp_path: Path, monkeypatch):
    """A real session against Z.ai completes a trivial prompt; the recorded
    model name comes from model_usage. Like AC-3: the refusal without a
    credential is the mandatory, hermetic half; the real session runs only
    on the explicit ``FACTORY_LIVE_TESTS=1`` opt-in (F-51/m-21) and fails
    loudly when the opt-in is set but the credential is missing.
    """
    import factory.session as session_mod

    monkeypatch.setattr(session_mod, "seat_token", lambda: None)
    with pytest.raises(SessionConfigError, match="factory-zai"):
        run_session_sandboxed(
            seat="implementer",
            prompt="ok",
            run_dir=tmp_path,
            profile={},
            factory_root=tmp_path,
            max_turns=2,
            timeout_min=1,
        )
    monkeypatch.undo()

    live_session_gate()
    have_token = seat_token() is not None
    if not have_token:
        pytest.fail(
            "FACTORY_LIVE_TESTS=1 is set but no factory-zai credential is "
            "available: the opted-in live session cannot authenticate. Loud "
            "by design — never a silent skip. (F-51/m-21)"
        )

    from factory.runs import cleanup_run, create_run
    from factory.sandbox import ensure_srt

    ensure_srt()
    run = create_run("f06-ac4", REPO, factory_root=tmp_path)
    try:
        record = run_session_sandboxed(
            seat="implementer",
            prompt="Reply with exactly the word ok and nothing else.",
            run_dir=run.root,
            profile={},
            factory_root=tmp_path,
            max_turns=2,
            timeout_min=3,
        )
        assert record.status == "completed", record
        assert record.model, record
        assert record.model in record.models
        assert (record.result or "").strip(), record
        assert record.num_turns and record.num_turns >= 1
    finally:
        cleanup_run(run)


# ------------------------------------------------ F-06 AC-5: options + env --

def test_ac_5(tmp_path: Path, monkeypatch):
    """setting_sources == [] on every seat; the session env carries exactly
    the Z.ai seat names and no GitHub/keychain secret — regardless of what
    the ambient environment holds."""
    monkeypatch.setenv("GH_TOKEN", "gh-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "github-leak")
    monkeypatch.setenv("JFO_SUPABASE_ANON_KEY", "keychain-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient.example")

    for seat in SEATS:
        options = build_options(
            seat,
            cwd=tmp_path,
            max_turns=7,
            base_url=DEFAULT_BASE_URL,
            token="zai-token",
            timeout_ms=123000,
            protected=["schema/**"],
        )
        assert options.setting_sources == []
        assert options.max_turns == 7
        assert str(tmp_path) == str(options.cwd)
        # The session env is exactly the three Z.ai seat names — nothing
        # inherited, nothing extra.
        assert set(options.env) == {
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "API_TIMEOUT_MS",
        }
        assert options.env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == "zai-token"
        assert options.env["API_TIMEOUT_MS"] == "123000"
        # Hooks are wired: protected-path + bash guard.
        assert options.hooks and set(options.hooks) == {"PreToolUse"}

    # The reviewer seat cannot write; the implementer seat can.
    assert set(SEATS["reviewer"].disallowed_tools) >= {"Write", "Edit"}
    assert not SEATS["implementer"].disallowed_tools

    # session_env never reads the ambient environment at all.
    env = session_env(DEFAULT_BASE_URL, "tok", 1000)
    assert env == {
        "ANTHROPIC_BASE_URL": DEFAULT_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "API_TIMEOUT_MS": "1000",
    }


# ------------------------------------------------------------------ extras --

def test_record_carries_result_message_facts():
    """The record is distilled from the ResultMessage: turns, subtype,
    terminal_reason, tokens, and the model name out of model_usage (the
    entry with the most tokens when several ran)."""
    message = _result(
        num_turns=9,
        subtype="success",
        terminal_reason="completed",
        model_usage={
            "glm-5.3-flash": {
                "inputTokens": 10,
                "outputTokens": 1,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.0,
                "contextWindow": 128000,
                "maxOutputTokens": 4096,
            },
            "glm-5.3": {
                "inputTokens": 500,
                "outputTokens": 50,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.01,
                "contextWindow": 200000,
                "maxOutputTokens": 8192,
            },
        },
    )
    record = _run(_results_query(message))
    assert record.status == "completed"
    assert record.num_turns == 9
    assert record.subtype == "success"
    assert record.terminal_reason == "completed"
    assert record.tokens == {"input_tokens": 100, "output_tokens": 20}
    assert record.model == "glm-5.3"
    assert set(record.models) == {"glm-5.3", "glm-5.3-flash"}


def test_stream_without_result_message_is_a_failed_attempt():
    """The CLI dying before any ResultMessage is a recorded failure, not a
    crash and not a silent success."""

    async def yield_nothing(*, prompt, options, transport=None):
        return
        yield  # pragma: no cover

    record = _run(yield_nothing)
    assert record.status == "failed"
    assert record.reason == "no_result_message"


def test_unknown_seat_is_a_configuration_error():
    with pytest.raises(SessionConfigError):
        _run(_results_query(_result()), seat="wizard")


def test_entry_refuses_to_start_without_a_token(tmp_path: Path, monkeypatch):
    """The in-sandbox entry point (the -c bootstrap) refuses (exit 2, fix
    message on stderr) when the Z.ai token is absent. This is the
    CI-provable half of the real-session path: it runs the real entry,
    real SDK import and all, with no credential and no model."""
    import subprocess

    from factory.sandbox import scrub_env
    from factory.session import _entry_bootstrap, _stage_runner

    runner_dir = _stage_runner(tmp_path / "runner")

    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "seat": "implementer",
                "prompt": "hi",
                "cwd": str(tmp_path),
                "max_turns": 1,
                "timeout_min": 0.5,
                "base_url": DEFAULT_BASE_URL,
                "timeout_ms": 60000,
                "protected": [],
            }
        )
    )
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    env = scrub_env(dict(os.environ), ("JFO_SUPABASE_ANON_KEY",))

    proc = subprocess.run(
        [sys.executable, "-c", _entry_bootstrap(runner_dir), str(spec)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 2
    assert "ANTHROPIC_AUTH_TOKEN" in proc.stderr or "factory-zai" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_entry_bootstrap_ignores_a_factory_package_in_the_cwd(tmp_path: Path):
    """The entry runs the staged (orchestrator's) factory code, never the
    clone's: a ``factory/`` package sitting in the entry's cwd (a
    self-hosting clone, or a target project that happens to have one) must
    not shadow the control plane. This is the exact mechanism that made a
    repo-path import fall through to the clone: python must be able to
    *list* the directory holding the package, and only the staged runner
    dir is listable inside the sandbox."""
    import subprocess

    from factory.session import _entry_bootstrap, _stage_runner

    runner_dir = _stage_runner(tmp_path / "runner")

    poison = tmp_path / "cwd"
    (poison / "factory").mkdir(parents=True)
    (poison / "factory" / "__init__.py").write_text("raise RuntimeError('poisoned')\n")

    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import factory; print(factory.__file__); "
        "import factory.session; print(hasattr(factory.session, 'run_session'))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(runner_dir)],
        capture_output=True,
        text=True,
        cwd=poison,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert str(runner_dir / "factory" / "__init__.py") in proc.stdout
    assert "poisoned" not in proc.stderr + proc.stdout
    assert "True" in proc.stdout
    # The real bootstrap points at the staged copy, not the repo.
    assert repr(str(runner_dir)) in _entry_bootstrap(runner_dir)


def test_stage_runner_copies_the_package_without_caches(tmp_path: Path):
    """The staged copy is the factory package (importable), without
    __pycache__/pyc baggage, and staging is idempotent."""
    import shutil as _shutil

    from factory.session import _stage_runner

    stale = tmp_path / "runner" / "factory"
    stale.mkdir(parents=True)
    (stale / "leftover.py").write_text("x = 1\n")

    runner_dir = _stage_runner(tmp_path / "runner")
    pkg = runner_dir / "factory"
    assert (pkg / "session.py").exists()
    assert (pkg / "sandbox.py").exists()
    assert not (pkg / "leftover.py").exists()  # restaged clean
    assert not list(pkg.rglob("__pycache__"))
    assert not list(pkg.rglob("*.pyc"))
    _shutil.rmtree(pkg)  # a second stage after removal also works
    _stage_runner(runner_dir)
    assert (pkg / "session.py").exists()


def test_cleanup_run_removes_the_staged_runner(tmp_path: Path):
    """cleanup_run removes the -runner sibling together with the settings
    file, so staged copies don't accumulate."""
    from factory.runs import RunDir, cleanup_run

    run = RunDir(
        project="p",
        run_id="r",
        root=tmp_path / "runs" / "p" / "r",
        settings_path=tmp_path / "sandbox" / "p-r.json",
    )
    run.root.mkdir(parents=True)
    runner = run.settings_path.with_name(run.settings_path.name + "-runner")
    runner.mkdir(parents=True)
    (runner / "factory").mkdir()
    run.settings_path.parent.mkdir(parents=True, exist_ok=True)
    run.settings_path.write_text("{}")
    cleanup_run(run)
    assert not run.settings_path.exists()
    assert not runner.exists()


def test_sandboxed_settings_allow_the_factory_package(tmp_path: Path):
    """run_session_sandboxed's settings re-allow exactly the factory package
    dir (the in-sandbox entry imports it) — not the whole repo, so prompts,
    schemas and validator docs stay unreadable to the session."""
    from factory.sandbox import build_settings

    run_dir = tmp_path / "runs" / "proj" / "run-1"
    run_dir.mkdir(parents=True)
    settings = build_settings(
        {},
        run_dir,
        model_endpoint=DEFAULT_BASE_URL,
        factory_root=tmp_path,
        allow_read=[REPO / "factory"],
    )
    allowed = settings["filesystem"]["allowRead"]
    assert str(REPO / "factory") in allowed
    assert str(REPO) not in allowed


def test_pythonpath_never_crosses_into_the_sandbox(tmp_path: Path):
    """PYTHONPATH is on no allowlist: neither an ambient value nor an
    explicitly-passed one reaches the sandboxed child. The runner
    bootstraps imports with a sys.path insert instead, so there is no
    path-injection surface at all."""
    from factory.sandbox import sandbox_env

    child = sandbox_env(
        tmp_path, extra={"PYTHONPATH": "/evil", "ANTHROPIC_AUTH_TOKEN": "t"}
    )
    assert "PYTHONPATH" not in child
    assert child["ANTHROPIC_AUTH_TOKEN"] == "t"


def test_hook_output_shape_matches_sdk_contract():
    """The deny dict is exactly the SDK's SyncHookJSONOutput shape for
    PreToolUse (unit-guarded so an SDK rename fails here, not in prod)."""
    hook = protected_path_hook(["schema/**"])
    out = asyncio.run(
        hook.hooks[0](
            _pre_input("Write", {"file_path": "schema/x.json"}), None, {"signal": None}
        )
    )
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"]
    # The allow path returns an empty dict — no decision, tool proceeds.
    out = asyncio.run(
        hook.hooks[0](
            _pre_input("Write", {"file_path": "src/x.py"}), None, {"signal": None}
        )
    )
    assert out == {}


def test_sessionrecord_round_trips_through_json():
    """to_dict/from_dict survive a JSON round trip (the sandboxed entry
    prints JSON; the orchestrator rebuilds the record)."""
    from factory.session import SessionRecord

    record = SessionRecord(
        seat="implementer",
        status="completed",
        reason=None,
        num_turns=3,
        subtype="success",
        is_error=False,
        terminal_reason="completed",
        duration_ms=1500,
        session_id="s",
        model="glm-5.3",
        models=["glm-5.3"],
        tokens={"input_tokens": 5},
        total_cost_usd=0.01,
        result="ok",
        permission_denials=[],
    )
    assert SessionRecord.from_dict(json.loads(json.dumps(record.to_dict()))) == record


# ===========================================================================
# Owner review round on PR #18 — nine findings, each red-first here.
# ===========================================================================


# --- Finding 1: a direct run_session call leaked the orchestrator's whole
# environment (the SDK merges ALL of os.environ into the CLI subprocess). --

def test_review1_direct_call_does_not_leak_ambient_environment(monkeypatch):
    """What the SDK's spawn would see: during the session the process env
    is windowed to the runner allowlist + the seat names. GH_TOKEN, an AWS
    key and a foreign Anthropic key planted in os.environ are absent for
    the whole stream, and the ambient environment is restored afterwards.
    """
    planted = {
        "GH_TOKEN": "gh-leak",
        "AWS_SECRET_ACCESS_KEY": "aws-leak",
        "ANTHROPIC_API_KEY": "foreign-key",
        "ZAI_RANDOM_SECRET": "leak",
    }
    for key, value in planted.items():
        monkeypatch.setenv(key, value)

    seen: dict[str, str] = {}

    async def probing_query(*, prompt, options, transport=None):
        for key in list(planted) + ["PATH", "ANTHROPIC_BASE_URL"]:
            seen[key] = os.environ.get(key)
        yield _result()
        # Still windowed after the first message.
        seen["GH_TOKEN_LATE"] = os.environ.get("GH_TOKEN")

    record = _run(probing_query, timeout_min=5)
    assert record.status == "completed"
    for key in planted:
        assert seen[key] is None, key
    assert seen["PATH"], "PATH must survive the window"
    assert seen["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL
    assert seen["GH_TOKEN_LATE"] is None
    # Restored: the orchestrator process continues with its own env.
    for key, value in planted.items():
        assert os.environ[key] == value


# --- Finding 2: the default settings path was one level too shallow
# (<factory>/runs/sandbox/…), so cleanup_run never removed it. ---------- --

def _sandbox_stub(monkeypatch, captured):
    """Stub run_sandboxed inside factory.session: capture the spawn args
    (spec path = last argv element) and the written settings, and return a
    completed-session record without spawning anything. F-56 re-seat: the
    record now travels by the file the spec names (written here as the
    in-sandbox entry would), and stdout carries no record line."""

    from factory import session as session_mod

    def fake_run_sandboxed(cmd, run_dir, settings_path, **kwargs):
        captured["cmd"] = cmd
        captured["run_dir"] = run_dir
        captured["settings_path"] = settings_path
        captured["env"] = kwargs.get("env")
        captured["spec_path"] = cmd[-1]
        record = SessionRecord(seat="implementer", status="completed")
        record_path = session_mod.record_path_for(Path(settings_path))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record.to_dict()))
        import subprocess as sp

        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(session_mod, "run_sandboxed", fake_run_sandboxed)


def test_review2_default_settings_path_uses_the_factory_root(tmp_path: Path):
    """With no explicit factory_root, settings are derived from the
    canonical layout to <factory>/sandbox/<project>-<run-id>.json — not
    <factory>/runs/sandbox/… — so create_run/cleanup_run and the runner
    agree on one location."""
    from factory import session as session_mod

    factory_root = tmp_path
    run_root = factory_root / "runs" / "proj" / "20260101T000000Z-ab12cd"
    run_root.mkdir(parents=True)
    captured: dict = {}
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        _sandbox_stub(mp, captured)
        session_mod.run_session_sandboxed(
            seat="implementer",
            prompt="p",
            run_dir=run_root,
            profile={},
            max_turns=1,
            timeout_min=1,
            token="stub-token",
        )
    assert captured["settings_path"] == (
        factory_root / "sandbox" / "proj-20260101T000000Z-ab12cd.json"
    ), captured["settings_path"]
    assert not (factory_root / "runs" / "sandbox").exists()


def test_review2_settings_and_runner_are_cleaned_up(tmp_path: Path):
    """End to end with the real create_run/cleanup_run: after a session,
    nothing remains under <factory>/sandbox for that run (settings file,
    staged runner copy, spec)."""
    from factory import session as session_mod
    from factory.runs import cleanup_run, create_run

    run = create_run("proj", REPO, factory_root=tmp_path)
    captured: dict = {}
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        _sandbox_stub(mp, captured)
        session_mod.run_session_sandboxed(
            seat="implementer",
            prompt="p",
            run_dir=run.root,
            profile={},
            max_turns=1,
            timeout_min=1,
            token="stub-token",
        )
    assert captured["settings_path"] == run.settings_path
    cleanup_run(run)
    assert not run.settings_path.exists()
    assert not run.settings_path.with_name(run.settings_path.name + "-runner").exists()


# --- Finding 3: the protected-path hook missed ../, ./, case and symlink-
# prefix forms, and Bash writes were not checked at all. ---------------- --

def _deny_paths(hook, tool_inputs):
    callback = hook.hooks[0]
    denied = []
    for tool, tool_input in tool_inputs:
        decision = _decision(callback, tool, tool_input)
        if decision == "deny":
            denied.append((tool, tool_input))
    return denied


def test_review3_protected_path_traversal_and_case_are_denied(tmp_path: Path):
    """`tests/../schema/x`, `./schema/x`, `Schema/x` (case), and an
    absolute path that resolves through macOS's /var → /private/var symlink
    prefix are all denied for Write/Edit."""
    hook = protected_path_hook(["schema/**"])
    real_root = Path(tmp_path).resolve()  # /private/var/... on macOS
    cases = [
        ("Write", {"file_path": "tests/../schema/tasks.schema.json"}),
        ("Edit", {"file_path": "./schema/tasks.schema.json"}),
        ("Write", {"file_path": "Schema/tasks.schema.json"}),
        ("Edit", {"file_path": "schema/../schema/tasks.schema.json"}),
    ]
    denied = _deny_paths(hook, cases)
    assert len(denied) == 4, denied

    # Symlink prefix: the same directory reached via its unresolved path.
    import os as _os

    if _os.path.realpath(tmp_path) != str(tmp_path):  # macOS /var layout
        unresolved = "/var" + str(real_root)[len("/private/var"):]
        hook2 = protected_path_hook([f"{real_root}/schema/**"])
        decision = _decision(
            hook2.hooks[0],
            "Write",
            {"file_path": f"{unresolved}/schema/x.json"},
        )
        assert decision == "deny"


def test_review3_bash_writes_to_protected_paths_are_denied():
    """Redirections and the common write utilities targeting a protected
    path are denied; ordinary commands and writes elsewhere are not."""
    guard = bash_guard_hook(["schema/**"])
    denied = _deny_paths(
        guard,
        [
            ("Bash", {"command": "echo x > schema/tasks.schema.json"}),
            ("Bash", {"command": "echo x >>schema/x.json"}),
            ("Bash", {"command": "mv good.py schema/good.py"}),
            ("Bash", {"command": "cp a schema/a"}),
            ("Bash", {"command": "rm schema/old.json"}),
            ("Bash", {"command": "tee schema/x.json"}),
            ("Bash", {"command": "sed -i s/a/b/ schema/x.json"}),
        ],
    )
    assert len(denied) == 7, denied
    allowed = [
        ("Bash", {"command": "echo x > src/app.py"}),
        ("Bash", {"command": "cp a b"}),
        ("Bash", {"command": "mv schema/x src/x"}),
        ("Bash", {"command": "cat schema/x.json"}),
    ]
    for tool, tool_input in allowed:
        assert _decision(guard.hooks[0], tool, tool_input) is None, tool_input


# --- Finding 4: the bash guard missed `git -C . push`, quoted/"$(gh …)"
# command substitutions. ------------------------------------------------- --

def test_review4_bash_guard_covers_optioned_and_quoted_forms():
    guard = bash_guard_hook()
    denied = _deny_paths(
        guard,
        [
            ("Bash", {"command": "git -C . push origin main"}),
            ("Bash", {"command": "git --git-dir=.git push"}),
            ("Bash", {"command": "git -c core.editor=true push --all"}),
            ("Bash", {"command": '"gh" auth login'}),
            ("Bash", {"command": "echo $(gh auth status)"}),
            ("Bash", {"command": "`gh` pr list"}),
            ("Bash", {"command": "V=$(gh pr view 1) && echo $V"}),
        ],
    )
    assert len(denied) == 7, denied


# --- Finding 5: sessions ran the auto-updating ~/.local CLI instead of
# the CLI bundled with the pinned SDK. ----------------------------------- --

def test_review5_sandboxed_sessions_use_the_sdk_bundled_cli(tmp_path: Path):
    """The spec handed to the sandboxed entry names the CLI bundled inside
    the pinned claude_agent_sdk package, and that binary's directory joins
    the sandbox allowRead (toolchain) entries. ~/.local/bin/claude is
    never auto-picked: sandboxed sessions require the pinned CLI."""
    import claude_agent_sdk

    from factory import session as session_mod

    bundled = (
        Path(claude_agent_sdk.__file__).parent / "_bundled" /
        ("claude.exe" if sys.platform == "win32" else "claude")
    )
    run_root = tmp_path / "runs" / "proj" / "20260101T000000Z-ab12cd"
    run_root.mkdir(parents=True)
    captured: dict = {}
    import pytest as _pytest

    real_build_settings = session_mod.build_settings

    def capture_settings(profile, run_dir, **kwargs):
        captured["toolchain"] = kwargs.get("toolchain")
        return real_build_settings(profile, run_dir, **kwargs)

    with _pytest.MonkeyPatch.context() as mp:
        _sandbox_stub(mp, captured)
        mp.setattr(session_mod, "build_settings", capture_settings)
        session_mod.run_session_sandboxed(
            seat="implementer",
            prompt="p",
            run_dir=run_root,
            profile={},
            max_turns=1,
            timeout_min=1,
            token="stub-token",
        )
    spec = json.loads(Path(captured["spec_path"]).read_text())
    assert spec["cli_path"] == str(bundled)
    assert bundled in captured["toolchain"]
    # The auto-updating native install is never referenced.
    assert "~" not in spec["cli_path"]


# --- Finding 6: .session-spec.json landed in the clone as an untracked
# file a `git add -A` would commit. --------------------------------------- --

def test_review6_spec_never_lands_in_the_clone(tmp_path: Path):
    """After a sandboxed session is prepared, the clone's working tree is
    byte-identical to a fresh clone: the spec lives in the staged runner
    dir outside the run dir."""
    import subprocess

    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run("proj", REPO, factory_root=tmp_path)
    captured: dict = {}
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        _sandbox_stub(mp, captured)
        session_mod.run_session_sandboxed(
            seat="implementer",
            prompt="p",
            run_dir=run.root,
            profile={},
            max_turns=1,
            timeout_min=1,
            token="stub-token",
        )
    assert not (run.root / ".session-spec.json").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=run.root,
        capture_output=True, text=True,
    )
    assert status.stdout.strip() == "", status.stdout
    # The spec exists — outside the clone, inside the staged runner dir.
    spec_path = Path(captured["spec_path"])
    assert spec_path.exists()
    assert run.root not in spec_path.parents


# --- Finding 7: the ambient ANTHROPIC_AUTH_TOKEN fallback could send a
# non-Z.ai key to Z.ai. --------------------------------------------------- --

def test_review7_ambient_token_requires_the_zai_base_url(monkeypatch):
    """The ambient token is only used when the ambient base URL names the
    Z.ai endpoint — a token exported for a different provider (or with no
    base URL at all, i.e. api.anthropic.com) is not ours to send.

    Hermetic: the keychain step is stubbed to "not found" so the owner's
    real factory-zai item cannot answer first. Each result is reduced to a
    bool before asserting, so a failure never prints a credential.
    """
    from factory import session as session_mod

    monkeypatch.setattr(session_mod, "_keychain_token", lambda: None)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "an-anthropic-key")
    got_none = seat_token() is None
    assert got_none, "foreign base URL: returned a value (redacted)"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL)
    got_ambient = seat_token() == "an-anthropic-key"
    assert got_ambient, "Z.ai base URL: did not return the ambient token (redacted)"

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    got_none = seat_token() is None
    assert got_none, "no base URL: returned a value (redacted)"


# --- Secret hygiene: no test may surface a real credential. -------------- --

# Calls whose return value is (or may be) the owner's real Z.ai credential.
_CREDENTIAL_CALLS = frozenset({"seat_token", "_keychain_token"})


def _credential_leaks(source: str) -> list[int]:
    """Line numbers where a credential call's value can reach test output.

    Flags a credential call — or a name bound directly to one — inside an
    ``assert`` (pytest's rewrite prints its operands on failure), a
    ``print(...)`` or an f-string. Reducing to a bool first
    (``ok = seat_token() is None``) is the sanctioned shape and passes.
    """
    tree = ast.parse(source)

    def is_credential_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        return name in _CREDENTIAL_CALLS

    tainted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and is_credential_call(node.value):
            tainted.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def exposes(node: ast.AST) -> bool:
        return any(
            is_credential_call(sub)
            or (isinstance(sub, ast.Name) and sub.id in tainted)
            for sub in ast.walk(node)
        )

    hits = []
    for node in ast.walk(tree):
        sink = (
            isinstance(node, (ast.Assert, ast.JoinedStr))
            or (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print")
        )
        if sink and exposes(node):
            hits.append(node.lineno)
    return sorted(set(hits))


def test_no_test_can_print_a_real_credential():
    """Guard: no test file asserts on, prints or formats the output of
    seat_token()/_keychain_token() (or a name bound to it). A failing
    ``assert seat_token() is None`` once printed the owner's real token."""
    offenders = {
        str(path.relative_to(REPO)): lines
        for path in sorted((REPO / "tests").rglob("*.py"))
        if "fixtures" not in path.parts
        and (lines := _credential_leaks(path.read_text()))
    }
    assert not offenders, f"credential-exposing test lines: {offenders}"


def test_credential_guard_catches_the_known_shapes():
    """The guard's own positive controls: each historical/obvious leak
    shape is flagged, the bool-first shape is not."""
    assert _credential_leaks("assert seat_token() is None\n") == [1]
    assert _credential_leaks("assert x == session.seat_token()\n") == [1]
    assert _credential_leaks("t = seat_token()\nassert t, t\n") == [2]
    assert _credential_leaks("t = _keychain_token()\nprint(t)\n") == [2]
    assert _credential_leaks("msg = f'{seat_token()}'\n") == [1]
    assert _credential_leaks("ok = seat_token() is None\nassert ok, 'redacted'\n") == []


# --- Finding 8: a wall-clock abort never closed the SDK stream. ----------- --

def test_review8_abort_closes_the_stream():
    """Both the wall-clock abort and the SDK-error path close the stream
    (the SDK's aclose tears down its subprocess). The fakes are plain
    iterators, not async generators — loop shutdown cannot auto-close
    them — so their closed flags prove run_session itself called aclose."""

    class FakeStream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.01)
            return None

        async def aclose(self) -> None:
            self.closed = True

    hanging: list[FakeStream] = []

    def hanging_query(*, prompt, options, transport=None):
        stream = FakeStream()
        hanging.append(stream)
        return stream

    record = _run(hanging_query, timeout_min=0.03)
    assert record.status == "aborted"
    assert hanging[0].closed, "the aborted session's stream was not closed"

    class ExplodingStream(FakeStream):
        async def __anext__(self):
            raise CLINotFoundError("gone")

    exploding: list[ExplodingStream] = []

    def exploding_query(*, prompt, options, transport=None):
        stream = ExplodingStream()
        exploding.append(stream)
        return stream

    record = _run(exploding_query)
    assert record.status == "failed"
    assert exploding[0].closed, "the failed session's stream was not closed"


# --- Finding 9: _primary_model crashed on null token counts. --------------- --

def test_review9_primary_model_tolerates_null_counts():
    """model_usage entries with null token counts (seen from the CLI) must
    not crash the record; weight falls back to zero."""
    message = _result(
        model_usage={
            "glm-5.3": {
                "inputTokens": None,
                "outputTokens": None,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.0,
                "contextWindow": 200000,
                "maxOutputTokens": 8192,
            }
        }
    )
    record = _run(_results_query(message))
    assert record.status == "completed"
    assert record.model == "glm-5.3"


# ===========================================================================
# Owner review round 2 on PR #18 — concurrent-window strip, close-on-any-exit,
# guard residue, commit-subject convention.
# ===========================================================================


def test_review10_overlapping_sessions_refuse_and_keep_the_env(monkeypatch):
    """Round 2, new bug: two overlapping sessions in one process corrupted
    the environment — the second window saved the first's STRIPPED copy and
    restored it last, permanently deleting the orchestrator's GH_TOKEN. The
    fix refuses a second window while one is open (sessions in one process
    are serial; the F-10 loop is), so the refusal is loud and the env
    survives intact."""
    monkeypatch.setenv("GH_TOKEN", "precious")

    class GatedStream:
        def __init__(self, gate: asyncio.Event) -> None:
            self.gate = gate
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.gate.wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    first_window_open = asyncio.Event()
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()

    def first_query(*, prompt, options, transport=None):
        first_window_open.set()  # the window is open by the time query runs
        return GatedStream(gate_a)

    def second_query(*, prompt, options, transport=None):
        return GatedStream(gate_b)

    async def first():
        return await run_session(
            "implementer", "p", REPO, 5, 10, query_fn=first_query
        )

    async def second():
        await first_window_open.wait()
        try:
            await run_session(
                "implementer", "p", REPO, 5, 10, query_fn=second_query
            )
            return "ran"
        except SessionConfigError:
            return "refused"

    async def main():
        first_task = asyncio.create_task(first())
        await first_window_open.wait()  # first's window is open, parked
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0.02)
        # First finishes BEFORE the second's window would close — the exact
        # ordering that used to restore the stripped copy last.
        gate_a.set()
        await first_task
        gate_b.set()  # no-op once the second session was refused
        second_outcome = await second_task
        return second_outcome

    second_outcome = asyncio.run(main())
    assert second_outcome == "refused"
    assert os.environ.get("GH_TOKEN") == "precious"


def test_review11_non_sdk_errors_still_close_the_stream():
    """Round 2: the close lived on three exit paths; a non-SDK error (a
    bug propagating by design) skipped it. The close now belongs to the
    finally, so every exit path — completion, abort, SDK error, and
    unexpected raises — closes the stream and restores the env."""

    class BugStream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("runner bug")

        async def aclose(self) -> None:
            self.closed = True

    streams: list[BugStream] = []

    def bug_query(*, prompt, options, transport=None):
        stream = BugStream()
        streams.append(stream)
        return stream

    with pytest.raises(RuntimeError, match="runner bug"):
        asyncio.run(run_session("implementer", "p", REPO, 5, 5, query_fn=bug_query))
    assert streams[0].closed, "a propagating error must still close the stream"


def test_review12_bash_guard_backslash_and_sh_c_quoting():
    """Round 2 residue: `sh -c 'git pu''sh'` and `g\\it push` passed. Both
    are caught by running the git/push check on the de-quoted command with
    backslashes stripped, like the gh check already did."""
    guard = bash_guard_hook()
    denied = _deny_paths(
        guard,
        [
            ("Bash", {"command": "sh -c 'git pu''sh' origin main"}),
            ("Bash", {"command": "g\\it push"}),
            ("Bash", {"command": "echo ok > /tmp/f && g\\h pr create"}),
        ],
    )
    assert len(denied) == 3, denied


# ===========================================================================
# Owner review round 3 on PR #18 — the word-anywhere guard was too broad.
# ===========================================================================


def test_review13_push_or_gh_mentions_in_ordinary_commands_pass():
    """Round 3: the git/push word-anywhere check blocked truthful work — a
    commit message about push notifications, --grep=push, code search, even
    `echo gh is the cli`. The guard now splits the command into its
    invocations (after de-quoting, on ;, |, &&, (), redirections) and
    denies only when an invocation's program actually runs `git push` or
    `gh` — so these all pass:"""
    guard = bash_guard_hook()
    allowed = [
        ("Bash", {"command": "git commit -m 'fix push notification retry'"}),
        ("Bash", {"command": "git log --grep=push"}),
        ("Bash", {"command": "grep -rn push src/"}),
        ("Bash", {"command": "echo gh is the cli"}),
        ("Bash", {"command": "rg 'def push' src/ && git commit -m 'add push()'"}),
        ("Bash", {"command": "git help push"}),
        ("Bash", {"command": "grep gh README.md"}),
    ]
    for tool, tool_input in allowed:
        assert _decision(guard.hooks[0], tool, tool_input) is None, tool_input


def test_review13_real_invocations_still_deny():
    """The narrower scan keeps every evasion form from rounds 1–2 denied:
    optioned git push, wrapped gh (quoted, backticked, $()), sh -c with
    quote-split or backslash-split words, env/command/xargs prefixes."""
    guard = bash_guard_hook()
    denied = _deny_paths(
        guard,
        [
            ("Bash", {"command": "git push origin main"}),
            ("Bash", {"command": "git -C . push origin main"}),
            ("Bash", {"command": "git --git-dir=.git push"}),
            ("Bash", {"command": "git -c core.editor=true push --all"}),
            ("Bash", {"command": "/usr/bin/git push"}),
            ("Bash", {"command": "gh pr create"}),
            ("Bash", {"command": "/usr/bin/gh auth login"}),
            ("Bash", {"command": '"gh" auth login'}),
            ("Bash", {"command": "echo $(gh auth status)"}),
            ("Bash", {"command": "V=$(gh pr view 1) && echo $V"}),
            ("Bash", {"command": "sh -c 'git pu''sh' origin main"}),
            ("Bash", {"command": "g\\it push"}),
            ("Bash", {"command": "g\\h pr create"}),
            ("Bash", {"command": "env git push"}),
            ("Bash", {"command": "command gh auth login"}),
            ("Bash", {"command": "bash -c 'gh auth login'"}),
        ],
    )
    assert len(denied) == 16, denied
