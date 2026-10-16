"""F-06 — session runner acceptance (BUILD-ORDER F-06, contract in
.factory/tasks.json).

The runner wraps ``claude_agent_sdk.query()`` per seat, inside the F-05
sandbox: per-seat options (setting_sources=[], Z.ai env, tool lists),
in-process PreToolUse hooks, a wall clock around the message stream, and a
SessionRecord distilled from the ResultMessage. ``query`` is injected, so
AC-1/2/5 (and AC-3's callback half) run against fakes; the real-session
halves of AC-3/AC-4 need the Z.ai token, which by design (DESIGN D2, Rule
of Two) exists only on the orchestrator's Mac. Where the token is absent —
the Linux CI runner — those tests prove the runner's refusal path instead
of skipping: a credential that must never leave the Mac is not a broken
stack, and the refusal is real, tested behaviour.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from claude_agent_sdk import CLINotFoundError, ResultMessage

from factory.session import (
    DEFAULT_BASE_URL,
    SEATS,
    SessionConfigError,
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

def test_ac_3(tmp_path: Path):
    """The protected-path hook denies Write/Edit/NotebookEdit on protected
    paths (unit half on the callback), and blocks a write inside a real
    session (real half, token-gated; refusal path where there is no token).
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

    # --- real half -----------------------------------------------------------
    token = seat_token()
    if token is None:
        # The CI shape: no credential exists here by design. The runner must
        # refuse loudly rather than spawn a session that cannot authenticate.
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
        return

    # The owner's Mac: a real session in a real sandbox is ordered to
    # create a file at a protected path. If the hook failed, the Write
    # would succeed and the file would exist — a positive control, which
    # is why the target is an ordinary new file rather than something the
    # sandbox also denies (a backstop would mask a hook failure). The
    # attempt itself is evidenced by the turn count: the denied Write
    # costs a turn before the model reports back.
    from factory.runs import cleanup_run, create_run
    from factory.sandbox import ensure_srt

    ensure_srt()
    run = create_run("f06-ac3", REPO, factory_root=tmp_path)
    try:
        record = run_session_sandboxed(
            seat="implementer",
            prompt=(
                "Create a file named protected.md containing the word "
                "pwned. You must use the Write tool for this. Do not ask "
                "questions, do not offer alternatives, just call the Write "
                "tool now."
            ),
            run_dir=run.root,
            profile={},
            factory_root=tmp_path,
            max_turns=4,
            timeout_min=3,
            protected=["protected.md"],
        )
        assert record.status == "completed", record
        assert not (run.root / "protected.md").exists()
        # The session really attempted the write (denied tool call costs a
        # turn). Note: the CLI does not record PreToolUse hook denials in
        # permission_denials — verified empirically; that field is only
        # populated for can_use_tool denials.
        assert record.num_turns and record.num_turns >= 2, record
    finally:
        cleanup_run(run)


# ----------------------------------------------- F-06 AC-4: real smoke run --

def test_ac_4(tmp_path: Path):
    """A real session against Z.ai completes a trivial prompt; the recorded
    model name comes from model_usage. Token-gated like AC-3's real half:
    without a credential the runner's refusal is the assertion (CI shape).
    """
    token = seat_token()
    if token is None:
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
        return

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
