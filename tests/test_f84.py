"""F-84 — a session installs nothing and searches nothing the contract
did not allow.

Three acceptance criteria:

* AC-1 — no open web search: no seat's session can call WebSearch; the
  refusal sits at the options layer (``disallowed_tools``) AND in a
  PreToolUse deny hook (deny hooks still block under
  ``bypassPermissions``). WebFetch keeps today's bound — the profile's
  ``docs_allowlist`` stays on the session's network allowlist.
* AC-2 — installs come from the lock, before the session: the session's
  srt settings DROP the profile's ``network_allowlist`` (the package
  registries) unless the task's contract declares ``dependencies``
  (F-71); setup and the gates keep them. Proven on the real sandbox
  runner: ``pip install``/``npm install`` fail for a task with no
  declared dependencies and succeed for one with them.
* AC-3 — the refusal is visible: a denied install or search lands in
  the F-30 trace (a ``denied`` tag on the tool_call event), in a
  ``sandbox_bypass_denials``-style counter on the session record and
  the attempt record, and the implementer prompt says, in one line,
  that installs come from the lock and what to do when a tool is
  missing.

Scripted seats, real git, no model calls. The real-runner arms use the
real srt (gated by the suite's nested-sandbox guard, like the F-05
tests); the registry-reach arms are ``live_network`` (the named CI job)
because a SUCCESSFUL install needs the public internet — the refusal
arms run everywhere, the proxy's refusal is local.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from _support.f72 import _task
from _support.f73 import Env73, GitSeats
from claude_agent_sdk import ResultMessage

from factory.mergeorder import CIState
from factory.sandbox import build_settings, write_settings

REPO_ROOT = Path(__file__).resolve().parent.parent

# The fixture profile: docs host for WebFetch grounding, registries on
# the network allowlist exactly the way both real profiles carry them.
DOCS_HOST = "docs.python.org"
REGISTRIES = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]
F84_PROFILE = {
    "docs_allowlist": [DOCS_HOST],
    "network_allowlist": list(REGISTRIES),
}
MODEL_ENDPOINT = "api.z.ai"

# The one-line policy the implementer prompt must carry (AC-3): the
# words the AC names — the lock, and the request-not-work-around rule.
PROMPT_LOCK_PAT = re.compile(
    r"[Ii]nstalls? (come|comes) from the lock.*finding", re.DOTALL,
)
PROMPT_WORKAROUND_PAT = re.compile(r"do not (install|work around)")


# ---------------------------------------------------------------- helpers --


STOCKHOLM = ZoneInfo("Europe/Stockholm")
GREEN_CI = lambda remote, sha: CIState("success")  # noqa: E731


def _result(**overrides) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={},
        total_cost_usd=0.001,
        result="done",
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


def _pre_input(tool: str, tool_input: dict, tool_use_id: str = "u1") -> dict:
    """A PreToolUse payload the SDK would hand a hook callback."""
    return {
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "cwd": "/run",
    }


async def _decision(callback, tool: str, tool_input: dict,
                    tool_use_id: str = "u1") -> "tuple[str | None, str]":
    """Run one hook callback and reduce its output to (decision, reason)."""
    out = await callback(_pre_input(tool, tool_input, tool_use_id),
                         tool_use_id, {})
    specific = (out.get("hookSpecificOutput") or {}) if out else {}
    return (
        specific.get("permissionDecision"),
        specific.get("permissionDecisionReason", ""),
    )


def _options(seat: str, **kwargs):
    from factory.session import build_options

    return build_options(
        seat, REPO_ROOT, 5, base_url="https://api.z.ai/api/anthropic",
        token="t", **kwargs,
    )


def _run_dir(tmp_path: Path, name: str = "run1") -> tuple[Path, Path]:
    """A canonical factory root + run dir pair (build_settings derives
    the root from exactly this layout)."""
    root = tmp_path / "factory-root"
    (root / "holdouts").mkdir(parents=True, exist_ok=True)
    run_dir = root / "runs" / "fixture" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return root, run_dir


def _session_settings(tmp_path: Path, *, declared: bool,
                      name: str = "run1") -> tuple[Path, Path]:
    """The settings a SESSION gets (the _run_prepared shape): the
    registries are dropped unless the task's contract declares its
    dependencies."""
    from factory.sandbox import run_sandboxed  # noqa: F401 — the guard wraps it

    root, run_dir = _run_dir(tmp_path, name)
    settings = build_settings(
        F84_PROFILE, run_dir, model_endpoint=MODEL_ENDPOINT,
        factory_root=root,
        drop_network_allowlist=not declared,
    )
    path = run_dir / "settings.json"
    write_settings(settings, path)
    return run_dir, path


PIP_INSTALL = [
    sys.executable, "-m", "pip", "install", "--no-input", "--quiet",
    "--disable-pip-version-check", "--retries", "1", "--timeout", "20",
    "--target", "deps", "six",
]
NPM_INSTALL = [
    "npm", "install", "--no-audit", "--no-fund",
    "--fetch-retries", "1", "--fetch-timeout", "20000",
    "--prefix", "npmdeps", "lodash",
]


# ------------------------------------------------------------------ AC-1 --


def test_ac_1(tmp_path):
    """No seat's session can call WebSearch: the options layer refuses
    the tool (``disallowed_tools``) and the PreToolUse hook denies it
    under ``bypassPermissions``, for every seat in SEATS; WebFetch
    stays bounded to the docs allowlist as today."""
    from factory.session import SEATS, EgressDenials, web_search_hook

    for seat in SEATS:
        options = _options(seat)
        # (a) the options layer: the tool is not available to the seat.
        assert "WebSearch" in options.disallowed_tools, (
            f"{seat}: WebSearch must be in disallowed_tools"
        )
        # (b) the hook layer: a deny hook still blocks under
        # bypassPermissions (F-06's established fact).
        state = EgressDenials()
        hook = web_search_hook(state)
        assert hook.matcher == "WebSearch"
        decision, reason = asyncio.run(_decision(
            hook.hooks[0], "WebSearch", {"query": "pypi slopsquatting"},
            "web-1"))
        assert decision == "deny", f"{seat}: {decision}"
        assert "WebSearch" in reason and "session" in reason.lower(), reason
        assert state.count == 1 and state.denied.get("web-1") == "web_search"
        # (c) the options actually wire the hook (the real build path,
        # not just the constructor): every seat's PreToolUse set holds
        # the WebSearch matcher.
        wired = _options(seat, egress_denials=state)
        matchers = [m.matcher for m in wired.hooks["PreToolUse"]]
        assert "WebSearch" in matchers, f"{seat}: hook not wired: {matchers}"

    # (d) WebFetch is untouched: no hook denies it and the session's
    # settings still carry the docs allowlist (the bound that refused
    # github.com in run 140cfb).
    options = _options("implementer")
    for matcher in options.hooks["PreToolUse"]:
        if "WebFetch" in matcher.matcher:
            decision, _ = asyncio.run(_decision(
                matcher.hooks[0], "WebFetch",
                {"url": f"https://{DOCS_HOST}/3/"}, "wf-1"))
            assert decision is None, "WebFetch must keep today's bound"
    _, settings_path = _session_settings(tmp_path / "docs", declared=False)
    settings = json.loads(settings_path.read_text())
    domains = settings["network"]["allowedDomains"]
    assert MODEL_ENDPOINT in domains and DOCS_HOST in domains
    assert not set(REGISTRIES) & set(domains), (
        f"an undeclared-deps session's settings must not carry the "
        f"registries: {domains}"
    )

    # (e) the denial is traceable: the tap stamps the refused call.
    from factory.session import _tap_tool_calls

    state = EgressDenials()
    state.denied["w9"] = "web_search"
    events: list[dict] = []
    pending: dict = {}
    use = SimpleNamespace(name="WebSearch", id="w9",
                          input={"query": "anything"})
    result = SimpleNamespace(tool_use_id="w9", is_error=True,
                             content="denied")
    _tap_tool_calls(SimpleNamespace(content=[use]), pending, "implementer",
                    None, events.append, egress_denied=state.denied)
    _tap_tool_calls(SimpleNamespace(content=[result]), pending, "implementer",
                    None, events.append, egress_denied=state.denied)
    assert events and events[0]["denied"] == "web_search"
    assert not state.denied, "a traced denial leaves the map"


# ------------------------------------------------------------------ AC-2 --


def test_ac_2_settings_shape(tmp_path):
    """The settings shape: drop_network_allowlist removes exactly the
    profile's network_allowlist domains (registries included) and keeps
    the model endpoint and docs allowlist; the default (gates, setup)
    keeps everything, as today."""
    root, run_dir = _run_dir(tmp_path)

    gate = build_settings(F84_PROFILE, run_dir,
                          model_endpoint=MODEL_ENDPOINT, factory_root=root)
    domains = gate["network"]["allowedDomains"]
    assert set(domains) == {MODEL_ENDPOINT, DOCS_HOST, *REGISTRIES}

    session = build_settings(
        F84_PROFILE, run_dir, model_endpoint=MODEL_ENDPOINT,
        factory_root=root, drop_network_allowlist=True,
    )
    domains = session["network"]["allowedDomains"]
    assert set(domains) == {MODEL_ENDPOINT, DOCS_HOST}, domains


def test_ac_2_session_seam(tmp_path, monkeypatch):
    """The session path threads the contract fact: run_session_sandboxed
    writes settings without the registries and a spec whose
    ``declared_dependencies`` reaches the in-sandbox entry — False for a
    plain task, True for one whose contract declares dependencies."""
    import factory.session as session_module
    from factory import provider
    from factory.session import run_session_sandboxed

    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True,
                   capture_output=True)
    profile = {
        "name": "p",
        "repo": {"path": str(repo), "default_branch": "main",
                 "remote": str(tmp_path / "r.git")},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        **F84_PROFILE,
    }
    monkeypatch.setattr(provider, "_keychain_token",
                        lambda service: "t"
                        if service == "factory-zai" else None)
    captured: list[dict] = []

    def fake_run(cmd, run_dir, settings_path, **kwargs):
        settings_path = Path(settings_path)
        captured.append({
            "settings": json.loads(settings_path.read_text()),
            "spec": json.loads((settings_path.with_name(
                settings_path.name + "-runner") / "spec.json").read_text()),
        })
        from factory.session import record_path_for

        record_path = record_path_for(settings_path)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps({
            "seat": "implementer", "status": "completed",
            "num_turns": 3, "result": "ok"}))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(session_module, "run_sandboxed", fake_run)

    for declared in (False, True):
        captured.clear()
        settings_path = tmp_path / f"root{int(declared)}" / "sandbox" / (
            "p-run.json")
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        run_session_sandboxed(
            "implementer", "work", repo, profile,
            factory_root=settings_path.parent,
            settings_path=settings_path,
            max_turns=5, timeout_min=1, cli_path=Path("/bin/echo"),
            declared_dependencies=declared,
        )
        assert len(captured) == 1
        domains = set(captured[0]["settings"]["network"]["allowedDomains"])
        assert (REGISTRIES[0] in domains) is declared, (
            f"declared={declared}: registry egress must follow the "
            f"contract: {domains}"
        )
        assert DOCS_HOST in domains, "docs grounding stays either way"
        assert captured[0]["spec"]["declared_dependencies"] is declared


def _six_installed(run_dir: Path) -> bool:
    """six lands as the single-module ``six.py`` (or a package dir on
    some versions) — both spellings count."""
    deps = run_dir / "deps"
    return (deps / "six.py").exists() or (deps / "six").exists()


def _assert_not_installed(run_dir: Path, label: str, out: str) -> None:
    assert not _six_installed(run_dir), (
        f"{label}: six landed despite the refusal\n{out}"
    )
    assert not (run_dir / "npmdeps" / "node_modules" / "lodash").exists(), (
        f"{label}: lodash landed despite the refusal\n{out}"
    )


def test_ac_2_real_runner_refuses_undeclared_installs(tmp_path):
    """On the REAL sandbox runner, with a session's settings (no
    declared dependencies), pip and npm installs fail: the registries
    are not on the allowlist, and nothing lands in the run dir. The
    pinned proxy signature (srt's proxy answering 403) is what tells
    the REFUSAL apart from an offline-looking failure — the F-05 AC-3
    lesson, applied to both package managers."""
    from factory.sandbox import run_sandboxed

    run_dir, settings = _session_settings(tmp_path / "refuse", declared=False)
    refusal = {
        "pip": "Tunnel connection failed: 403 Forbidden",
        "npm": "403 Forbidden - GET https://registry.npmjs.org/",
    }
    for label, cmd in (("pip", PIP_INSTALL), ("npm", NPM_INSTALL)):
        proc = run_sandboxed(cmd, run_dir, settings, timeout=180)
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0, (
            f"{label} install succeeded for an undeclared-deps session\n"
            f"{out}"
        )
        assert refusal[label] in out, (
            f"{label}: the failure is not the proxy's registry refusal — "
            f"an offline-looking error is not the deny being proven\n{out}"
        )
        _assert_not_installed(run_dir, label, out)


@pytest.mark.live_network
def test_ac_2_real_runner_allows_declared_installs(tmp_path):
    """The allow half, live-network (a successful install needs the real
    registries): with declared dependencies the session's settings carry
    them, and pip/npm installs succeed — L0's F-71 check judges the
    manifest afterwards, as today. The GATE shape (setup, L0) keeps the
    registries unconditionally: an install during setup still works."""
    from factory.sandbox import run_sandboxed

    # (a) declared dependencies: the session may install.
    run_dir, settings = _session_settings(tmp_path / "allow", declared=True)
    domains = set(json.loads(settings.read_text())["network"]["allowedDomains"])
    assert set(REGISTRIES) <= domains
    for label, cmd in (("pip", PIP_INSTALL), ("npm", NPM_INSTALL)):
        proc = run_sandboxed(cmd, run_dir, settings, timeout=300)
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, f"{label} install failed\n{out}"
    assert _six_installed(run_dir)
    assert (run_dir / "npmdeps" / "node_modules" / "lodash").exists()

    # (b) the gate/setup shape keeps the registries: L0's own setup
    # install (F-73) must still work.
    root, gate_dir = _run_dir(tmp_path / "allow", "gate")
    gate_settings = build_settings(
        F84_PROFILE, gate_dir, model_endpoint=MODEL_ENDPOINT,
        factory_root=root,
    )
    gate_path = write_settings(gate_settings, gate_dir / "gate.json")
    proc = run_sandboxed(PIP_INSTALL, gate_dir, gate_path, timeout=300)
    assert proc.returncode == 0, (
        f"an install during L0's own setup must still work\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert _six_installed(gate_dir)


# ------------------------------------------------------------------ AC-3 --


def test_ac_3_hooks_and_record(tmp_path):
    """The refusal is visible: the install guard denies install-class
    Bash commands (registry, URL, git and local-wheel sources alike) for
    an undeclared session, allows them when the contract declares
    dependencies, counts every denial, and the count rides the session
    record out of run_session."""
    from factory.session import (
        EgressDenials,
        install_guard_hook,
        run_session,
    )

    state = EgressDenials()
    hook = install_guard_hook(False, state)
    assert hook.matcher == "Bash"
    denials = [
        ("pip install six", "pip install"),
        ("pip install ./evil.whl", "pip install"),
        ("pip install git+https://github.com/a/b.git", "pip install"),
        ("pip install https://example.com/x.whl", "pip install"),
        ("pip download six -d /tmp/x", "pip download"),
        ("python -m pip install --user six", "pip install"),
        ("python3 -m ensurepip --upgrade", "ensurepip"),
        ("npm install lodash", "npm install"),
        ("npm ci", "npm install"),
        ("npm --prefix npmdeps install lodash", "npm install"),
        ("npx cowsay hi", "npx"),
        ("uv pip install six", "uv install"),
        ("sh -c 'pip install six'", "pip install"),
        ("env PIP_QUIET=1 pip download six", "pip download"),
    ]
    for i, (command, _) in enumerate(denials):
        decision, reason = asyncio.run(_decision(
            hook.hooks[0], "Bash", {"command": command}, f"i{i}"))
        assert decision == "deny", f"must deny: {command}"
        assert "lock" in reason.lower() and "finding" in reason.lower(), (
            f"the refusal must say the policy and the way out: {reason}"
        )
    assert state.count == len(denials)

    # Not installs — the guard has no opinion (the sandbox, L0's diff
    # policy and the other hooks judge these). ``pip install --help`` is
    # deliberately NOT here: it shares the install subcommand, and
    # over-matching is the safe direction for a deny-layer.
    for command in ("pip list", "pip uninstall six", "npm ls",
                    "python -m pytest -q", "npm run build", "uv venv"):
        decision, _ = asyncio.run(_decision(
            hook.hooks[0], "Bash", {"command": command}, "ok1"))
        assert decision is None, f"must not deny: {command}"

    # Declared dependencies: the registry is reachable, L0 judges the
    # manifest — the hook is inert.
    allowed = install_guard_hook(True, EgressDenials())
    decision, _ = asyncio.run(_decision(
        allowed.hooks[0], "Bash", {"command": "pip install six"}, "ok2"))
    assert decision is None

    # The count rides the record out of the real in-process wiring: the
    # hooks build_options wired fire as the SDK would, and the completed
    # record carries both the install and the search denial.
    async def query(*, prompt, options, transport=None):
        for matcher in options.hooks["PreToolUse"]:
            for callback in matcher.hooks:
                if matcher.matcher == "Bash":
                    await callback(_pre_input(
                        "Bash", {"command": "pip install six"}, "q1"),
                        "q1", {})
                elif matcher.matcher == "WebSearch":
                    await callback(_pre_input(
                        "WebSearch", {"query": "x"}, "q2"), "q2", {})
        yield _result()

    record = asyncio.run(run_session(
        "implementer", "do it", tmp_path, 5, 10, query_fn=query,
        base_url="https://api.z.ai/api/anthropic", token="t"))
    assert record.status == "completed"
    assert record.egress_denials == 2
    assert record.sandbox_bypass_denials == 0


def test_ac_3_attempt_prompt_and_trace(tmp_path):
    """The refusal reaches the owner: the attempt record sums the
  counter across the round's sessions, the trace's session row carries
  it, factory status prints it, and every implementer prompt says, in
  one line, that installs come from the lock and a missing tool is a
  finding — never a workaround. The contract fact reaches the seats:
  an undeclared task's sessions carry declared_dependencies=False, a
  declared task's True."""
    from factory.state import TaskQueue
    from factory.status import render_status

    class CountingSeats(GitSeats):
        """GitSeats whose implementer also reports one denied install,
        the way a real session's record would."""

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            record = super().__call__(seat, prompt, run_dir, profile,
                                      **kwargs)
            if seat == "implementer":
                record.egress_denials = 1
            return record

    # (a) an undeclared task: sessions see declared_dependencies False.
    env = Env73(tmp_path / "plain")
    seats = CountingSeats()
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "pr_opened", result
    for seat_kwargs in seats.kwargs("implementer"):
        assert seat_kwargs.get("declared_dependencies") is False, seat_kwargs
    attempt = env.task()["attempts"][-1]
    assert attempt["egress_denials"] == 1, attempt.get("egress_denials")
    prompt = seats.impl_prompts[0]
    assert PROMPT_LOCK_PAT.search(prompt), "the lock line is missing"
    assert PROMPT_WORKAROUND_PAT.search(prompt), (
        "the prompt must forbid working around a missing tool"
    )
    trace = env.root / "trace" / "fixture" / f"{attempt['run_id']}.jsonl"
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    session_rows = [row for row in rows if row.get("type") == "session_end"
                    and row.get("seat") == "implementer"]
    assert session_rows and session_rows[0]["egress_denials"] == 1
    page = render_status(TaskQueue(env.queue_path).doc, project="fixture",
                         factory_root=env.root,
                         now=datetime(2026, 11, 15, 12, tzinfo=STOCKHOLM))
    assert "egress denial" in page.lower(), (
        "the weekly summary must show the refused installs/searches"
    )

    # (b) a task whose contract declares a dependency: True reaches the
    # seats (the registries are on that session's allowlist).
    env = Env73(
        tmp_path / "declared",
        tasks=[_task(dependencies=[{"ecosystem": "pypi", "name": "pypdf"}])],
    )
    seats = CountingSeats()
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "pr_opened", result
    for seat_kwargs in seats.kwargs("implementer"):
        assert seat_kwargs.get("declared_dependencies") is True, seat_kwargs
