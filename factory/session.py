"""The session runner (BUILD-ORDER F-06).

One agent seat = one Claude Code session, started through
``claude_agent_sdk.query()`` with per-seat :class:`ClaudeAgentOptions`,
inside the F-05 sandbox. The sandbox wraps the *runner process* itself
(:func:`run_session_sandboxed` execs ``python -m factory.session`` under
srt), so the Claude Code subprocess the SDK spawns — and everything it
spawns — inherits the confinement. The srt sandbox is the permission
system (DESIGN D3): the implementer seat therefore runs with Claude
Code's own permission prompts bypassed (``bypassPermissions``) — prompts
are meaningless unattended, the run dir is the only writable path, and
the in-process PreToolUse hooks below are the in-session policy layer.
A deny hook still blocks under ``bypassPermissions``; the reviewer seat
additionally gets the edit tools disallowed outright.

Seats:

* ``implementer`` — writes code. Prompt-bypassed, sandbox-confined, hooks
  enforce protected paths and the push/gh ban.
* ``reviewer`` — reads, never writes: ``Write``/``Edit``/``NotebookEdit``
  are disallowed at the options layer on top of the same hooks.

Clock: the wall clock (:func:`asyncio.wait_for` around each message
receive, budgeted by the remaining deadline) covers **only** the message
stream — setup and record-building sit outside it, so a session's budget
isn't eaten by orchestrator work around it. A stream that never ends is
cut off at the deadline and reported ``aborted``.

Failure handling: error results (``error_max_turns``,
``error_during_execution``, ``is_error`` with an ``api_error_status``)
and SDK exceptions are recorded as failed attempts with the reason —
the runner never crashes on a session outcome. A bug in *this* code
still raises: only :class:`claude_agent_sdk.ClaudeSDKError` and session
outcomes are recorded.

Secrets: the session env is exactly three names — the Z.ai base URL,
the Z.ai token (the one secret the sandbox holds, DESIGN D2/D3) and the
API timeout. ``setting_sources=[]`` so no user/project Claude config is
loaded. Nothing else is read from the ambient environment, ever.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
)
from claude_agent_sdk import query as sdk_query

from factory.sandbox import (
    build_settings,
    default_toolchain,
    run_sandboxed,
    write_settings,
)

DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_TIMEOUT_MS = 600_000
KEYCHAIN_SERVICE = "factory-zai"

_FACTORY_PKG = Path(__file__).resolve().parent
_REPO_ROOT = _FACTORY_PKG.parent


def _stage_runner(runner_dir: Path) -> Path:
    """Stage a copy of the factory package for the sandboxed entry point.

    Why a copy rather than importing from the repo: python's import needs
    to *list* the directory that holds the package (FileFinder), and the
    sandbox denies listing the repo — so ``import factory`` from the repo
    path silently falls through to a ``factory/`` package in the target
    clone (untrusted shadowing; verified empirically). The staged copy
    lives next to the sandbox settings, outside the run dir: the session
    can read exactly it and can write nothing there.
    """
    runner_dir.mkdir(parents=True, exist_ok=True)
    dest = runner_dir / "factory"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        _FACTORY_PKG, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    return runner_dir


def _entry_bootstrap(runner_dir: Path) -> str:
    """The in-sandbox entry: the staged copy is first on sys.path, ahead of
    the cwd (the target clone), so the session always runs the
    orchestrator's code, never a ``factory/`` package that happens to sit
    in the clone."""
    return (
        "import sys; sys.path.insert(0, {runner!r}); "
        "from factory.session import _main; sys.exit(_main(sys.argv[1:]))"
    ).format(runner=str(runner_dir))


class SessionConfigError(Exception):
    """The session cannot be configured: unknown seat, missing token, or an
    unusable sandboxed-entry result. Raised before any session starts."""


class _WallClock(Exception):
    """Internal: the session's wall-clock budget ran out."""


# ------------------------------------------------------------------ seats --

@dataclass(frozen=True)
class SeatConfig:
    """Per-seat options. ``system_prompt`` is the seat prompt; F-09 supplies
    the real ones, so it is empty until then."""

    name: str
    system_prompt: str | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    permission_mode: str | None = None


SEATS: dict[str, SeatConfig] = {
    "implementer": SeatConfig(
        name="implementer",
        # See the module docstring: the sandbox is the permission system;
        # prompts are bypassed, hooks + srt enforce the policy.
        permission_mode="bypassPermissions",
    ),
    "reviewer": SeatConfig(
        name="reviewer",
        # WO: "the reviewer gets no Write/Edit". NotebookEdit is the same
        # class of tool, so it goes too — a reviewer that cannot mutate.
        disallowed_tools=("Write", "Edit", "NotebookEdit"),
    ),
}


def _seat(seat: str) -> SeatConfig:
    try:
        return SEATS[seat]
    except KeyError:
        raise SessionConfigError(
            f"unknown seat {seat!r}: expected one of {sorted(SEATS)}"
        ) from None


# ------------------------------------------------------------------- env --

def session_env(base_url: str, token: str, timeout_ms: int) -> dict[str, str]:
    """The session env: exactly the three Z.ai seat names.

    Deliberately literal — this function reads nothing from the ambient
    environment, so no GitHub token, keychain secret or inherited
    ``ANTHROPIC_*`` value can appear in a session by construction.
    """
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "API_TIMEOUT_MS": str(int(timeout_ms)),
    }


def seat_token() -> str | None:
    """The Z.ai model token, or None where no credential exists.

    Lookup order: the macOS keychain item ``factory-zai`` (the production
    path per DESIGN D2), then the ambient ``ANTHROPIC_AUTH_TOKEN`` (the
    owner's current bootstrap setup — the same env the interactive Claude
    Code uses). On the Linux CI runner neither exists and this returns
    None, which callers must treat as "no session can authenticate here".
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None  # no macOS keychain here (or it hung) — try the env
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return os.environ.get("ANTHROPIC_AUTH_TOKEN") or None


# ----------------------------------------------------------------- hooks --

_BASH_GIT_PUSH = re.compile(r"\bgit\s+push\b")
# The standalone word `gh` as a command: preceded by start/space/;&|/ and
# followed by whitespace or end. `\bgh\b` alone would also hit "gh-gh-gh"
# (a hyphen is a word boundary), and the WO bans the gh CLI, not the string.
_BASH_GH = re.compile(r"(?:^|[\s;&|/])gh(?:\s|$)")
_BASH_GH_CONFIG = ".config/gh"


def _deny(reason: str) -> dict[str, Any]:
    """A PreToolUse deny in the SDK's SyncHookJSONOutput shape."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _path_candidates(file_path: str, cwd: str) -> set[str]:
    """The forms a file_path is matched against: as given, absolute (cwd
    joined), and cwd-relative (an absolute path inside the repo)."""
    candidates = {file_path}
    path = Path(file_path)
    if path.is_absolute():
        if cwd:
            try:
                candidates.add(str(path.relative_to(cwd)))
            except ValueError:
                pass
    elif cwd:
        candidates.add(str(Path(cwd) / path))
    return candidates


def protected_path_hook(protected: "Sequence[str]") -> HookMatcher:
    """PreToolUse hook: deny Write/Edit/NotebookEdit into ``protected``
    globs (``paths.protected`` from the project profile). fnmatch treats
    ``*`` as spanning ``/``, which errs on the protective side. Unprotected
    paths get no opinion (``{}`` — the tool proceeds)."""

    patterns = tuple(protected)

    async def _hook(
        input: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        # The matcher already routes only Write|Edit|NotebookEdit here; the
        # tool-name guard is defence in depth for direct callback use.
        if input.get("tool_name") not in {"Write", "Edit", "NotebookEdit"}:
            return {}
        tool_input = input.get("tool_input") or {}
        file_path = str(tool_input.get("file_path", ""))
        if not file_path or not patterns:
            return {}
        cwd = str(input.get("cwd") or "")
        candidates = _path_candidates(file_path, cwd)
        for pattern in patterns:
            if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
                return _deny(f"{file_path} matches protected path {pattern!r}")
        return {}

    return HookMatcher(matcher="Write|Edit|NotebookEdit", hooks=[_hook])


def bash_guard_hook() -> HookMatcher:
    """PreToolUse hook over Bash: deny ``git push``, the ``gh`` CLI, and
    anything touching ``~/.config/gh``. The orchestrator (outside the
    sandbox, with its own credentials) pushes and opens PRs — the WO's
    rule that a session never does."""

    async def _hook(
        input: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        command = str((input.get("tool_input") or {}).get("command", ""))
        if _BASH_GIT_PUSH.search(command):
            return _deny("git push is not allowed inside a session")
        if _BASH_GH.search(command):
            return _deny("the gh CLI is not allowed inside a session")
        if _BASH_GH_CONFIG in command:
            return _deny("~/.config/gh is not accessible inside a session")
        return {}

    return HookMatcher(matcher="Bash", hooks=[_hook])


# ----------------------------------------------------------- options/record --

def build_options(
    seat: str,
    cwd: Path,
    max_turns: int,
    *,
    base_url: str,
    token: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    protected: "Sequence[str]" = (),
    cli_path: Path | None = None,
    model: str | None = None,
) -> ClaudeAgentOptions:
    """Build the per-seat ClaudeAgentOptions (WO F-06 options list).

    ``setting_sources=[]`` — no user/project/local Claude settings, so a
    session can't pick up personal config. ``env`` is exactly the three
    session names (see :func:`session_env`). Hooks: protected-path +
    bash-guard PreToolUse callbacks, in-process via HookMatcher.
    """
    seat_config = _seat(seat)
    options = ClaudeAgentOptions(
        cwd=str(Path(cwd)),
        env=session_env(base_url, token, timeout_ms),
        max_turns=max_turns,
        system_prompt=seat_config.system_prompt,
        setting_sources=[],
        allowed_tools=list(seat_config.allowed_tools),
        disallowed_tools=list(seat_config.disallowed_tools),
        permission_mode=seat_config.permission_mode,
        hooks={
            "PreToolUse": [protected_path_hook(protected), bash_guard_hook()]
        },
    )
    if cli_path is not None:
        options.cli_path = str(Path(cli_path).expanduser().resolve())
    if model is not None:
        options.model = model
    return options


@dataclass
class SessionRecord:
    """What a session recorded, distilled from the ResultMessage (WO F-06
    "Record" list). F-10 writes this into ``attempts[]``."""

    seat: str
    status: str  # completed | failed | aborted
    reason: str | None = None
    num_turns: int | None = None
    subtype: str | None = None
    is_error: bool = False
    terminal_reason: str | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    model: str | None = None
    models: list[str] = field(default_factory=list)
    tokens: dict[str, Any] | None = None
    total_cost_usd: float | None = None
    result: str | None = None
    permission_denials: list[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        return cls(**data)


def _primary_model(model_usage: dict[str, Any] | None) -> str | None:
    """The model name out of ``model_usage`` — the entry with the most
    input+output tokens when the session touched several."""
    if not model_usage:
        return None

    def weight(name: str) -> int:
        entry = model_usage[name]
        return int(entry.get("inputTokens", 0)) + int(entry.get("outputTokens", 0))

    return max(model_usage, key=weight)


def _failure_reason(message: ResultMessage) -> str:
    if message.subtype and message.subtype.startswith("error"):
        return message.subtype
    status = getattr(message, "api_error_status", None)
    if status:
        return f"api_error_{status}"
    return message.subtype or "is_error"


def _record_from(
    message: ResultMessage, seat: str, duration_ms: int
) -> SessionRecord:
    failed = message.is_error or (message.subtype or "").startswith("error")
    return SessionRecord(
        seat=seat,
        status="failed" if failed else "completed",
        reason=_failure_reason(message) if failed else None,
        num_turns=message.num_turns,
        subtype=message.subtype,
        is_error=message.is_error,
        terminal_reason=getattr(message, "terminal_reason", None),
        duration_ms=duration_ms,
        session_id=message.session_id,
        model=_primary_model(message.model_usage),
        models=sorted((message.model_usage or {}).keys()),
        tokens=message.usage,
        total_cost_usd=message.total_cost_usd,
        result=message.result,
        permission_denials=getattr(message, "permission_denials", None),
    )


# -------------------------------------------------------------- the runner --

async def run_session(
    seat: str,
    prompt: str,
    cwd: Path,
    max_turns: int,
    timeout_min: float,
    *,
    query_fn: Any = None,
    base_url: str = DEFAULT_BASE_URL,
    token: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    protected: "Sequence[str]" = (),
    cli_path: Path | None = None,
    model: str | None = None,
) -> SessionRecord:
    """Run one seat's session and return what it recorded.

    Args:
        seat: Seat name (see :data:`SEATS`).
        prompt: The task prompt (the user message). The seat's *system*
            prompt comes from the seat config (F-09 supplies those).
        cwd: The run dir — the session's working directory.
        max_turns: Turn cap handed to the SDK.
        timeout_min: Wall-clock budget in minutes, enforced around the
            message stream only (paused outside the session: option
            building and record extraction don't consume it).
        query_fn: Injected ``query`` for tests (a fake async generator
            function with the SDK's keyword signature). Default: the real
            ``claude_agent_sdk.query``.
        base_url/token/timeout_ms: The Z.ai seat env (see
            :func:`session_env`). ``token`` may be empty for in-process
            fake runs that spawn no subprocess; the real entry point
            refuses earlier when no token exists.
        protected: Protected-path globs for the hook.
        cli_path: Explicit Claude Code CLI path (inside the sandbox the
            ambient PATH can't be relied on).
        model: Optional model override.

    Returns:
        The record: ``completed`` / ``failed`` (with reason) / ``aborted``
        (wall clock). Never raises on a session outcome.
    """
    _seat(seat)  # unknown seat → SessionConfigError before anything runs
    if query_fn is None:
        query_fn = sdk_query
    options = build_options(
        seat,
        cwd,
        max_turns,
        base_url=base_url,
        token=token or "",
        timeout_ms=timeout_ms,
        protected=protected,
        cli_path=cli_path,
        model=model,
    )

    started = time.monotonic()
    deadline = started + timeout_min * 60.0
    result_message: ResultMessage | None = None
    try:
        stream = query_fn(prompt=prompt, options=options)
        iterator = stream.__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WallClock()
            try:
                message = await asyncio.wait_for(
                    iterator.__anext__(), timeout=remaining
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise _WallClock() from exc
            if isinstance(message, ResultMessage):
                result_message = message
    except _WallClock:
        return SessionRecord(
            seat=seat,
            status="aborted",
            reason="wall_clock",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except ClaudeSDKError as exc:
        return SessionRecord(
            seat=seat,
            status="failed",
            reason=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    if result_message is None:
        return SessionRecord(
            seat=seat,
            status="failed",
            reason="no_result_message",
            duration_ms=duration_ms,
        )
    return _record_from(result_message, seat, duration_ms)


# ------------------------------------------------- the sandboxed entry point --

def _resolve_cli(cli_path: Path | None) -> Path | None:
    """The Claude CLI the session will exec: explicit, on PATH, or the
    native installer's default location."""
    if cli_path is not None:
        return Path(cli_path).expanduser()
    found = shutil.which("claude")
    if found:
        return Path(found)
    default = Path("~/.local/bin/claude").expanduser()
    return default if default.exists() else None


def run_session_sandboxed(
    seat: str,
    prompt: str,
    run_dir: Path,
    profile: dict,
    *,
    factory_root: Path | None = None,
    settings_path: Path | None = None,
    max_turns: int,
    timeout_min: float,
    base_url: str = DEFAULT_BASE_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    protected: "Sequence[str]" = (),
    model: str | None = None,
    token: str | None = None,
    cli_path: Path | None = None,
    srt_bin_path: Path | None = None,
    teardown_grace_sec: float = 120.0,
) -> SessionRecord:
    """Run one session with the whole runner process inside the F-05 sandbox.

    Execs the entry bootstrap (see :func:`_entry_bootstrap`) under srt —
    the SDK's Claude Code subprocess inherits the confinement — with the
    settings built from the project profile plus one deliberate
    carve-out: the factory *package* dir is re-allowed for reading so the
    entry point can import it — the rest of the repo (prompts, schemas,
    validator docs) stays unreadable to the session.

    Args:
        run_dir: The run dir (the F-05 clone); the session's cwd.
        profile: The project profile (docs/network allowlists).
        factory_root: Explicit factory root; when omitted it is derived
            from the canonical ``<root>/runs/<project>/<run-id>`` layout.
        settings_path: Where the srt settings go. Default: the RunDir
            convention ``<factory_root>/sandbox/<project>-<run-id>.json``.
        max_turns/timeout_min: Handed to the session. The *outer*
            run_sandboxed wall clock is ``timeout_min`` plus
            ``teardown_grace_sec`` so the in-session clock fires first.
        token: The Z.ai token; default :func:`seat_token`. None and no
            credential available → :class:`SessionConfigError` (refuses to
            start a session that cannot authenticate).

    Returns:
        The session's record (any status — completed/failed/aborted are
        all recorded outcomes).

    Raises:
        SessionConfigError: No token, or the entry produced no parsable
            record (an orchestrator-side fault that must be loud).
        subprocess.TimeoutExpired: The outer wall clock expired — the
            runner process itself hung past its teardown grace.
    """
    token = token if token is not None else seat_token()
    if not token:
        raise SessionConfigError(
            "no Z.ai token available: create the keychain item with "
            f"`security add-generic-password -s {KEYCHAIN_SERVICE} -w <key>` "
            "(or run where ANTHROPIC_AUTH_TOKEN is set). The factory refuses "
            "to start a session that cannot authenticate."
        )

    run_dir = Path(run_dir)
    cli = _resolve_cli(cli_path)
    spec_path = run_dir / ".session-spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "seat": seat,
                "prompt": prompt,
                "cwd": str(run_dir),
                "max_turns": max_turns,
                "timeout_min": timeout_min,
                "base_url": base_url,
                "timeout_ms": timeout_ms,
                "protected": list(protected),
                "model": model,
                "cli_path": str(cli) if cli else None,
            }
        )
    )

    if settings_path is None:
        root = Path(factory_root) if factory_root else run_dir.parent.parent
        settings_path = root / "sandbox" / f"{run_dir.parent.name}-{run_dir.name}.json"
    runner_dir = _stage_runner(settings_path.with_name(settings_path.name + "-runner"))

    settings = build_settings(
        profile,
        run_dir,
        model_endpoint=base_url,
        factory_root=factory_root,
        toolchain=default_toolchain(extra=[cli] if cli else None),
        allow_read=[runner_dir],
    )
    write_settings(settings, settings_path)

    proc = run_sandboxed(
        [sys.executable, "-c", _entry_bootstrap(runner_dir), str(spec_path)],
        run_dir,
        settings_path,
        env={
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": base_url,
            "API_TIMEOUT_MS": str(int(timeout_ms)),
        },
        timeout=timeout_min * 60.0 + teardown_grace_sec,
        srt_bin_path=srt_bin_path,
    )

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise SessionConfigError(
            "the sandboxed session entry produced no record "
            f"(exit {proc.returncode}): {proc.stderr.strip()[:2000]}"
        )
    try:
        return SessionRecord.from_dict(json.loads(lines[-1]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise SessionConfigError(
            "the sandboxed session entry produced an unparsable record "
            f"({exc}): {lines[-1][:500]}"
        ) from exc


# python -c <bootstrap> <spec.json> — the in-sandbox entry point (see
# :func:`_entry_bootstrap` / :func:`_stage_runner`). The Z.ai token arrives
# via the environment (the orchestrator's run_sandboxed call); everything
# else is in the spec file.
def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m factory.session <spec.json>", file=sys.stderr)
        return 2
    try:
        spec = json.loads(Path(argv[0]).read_text())
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if not token:
            raise SessionConfigError(
                "no ANTHROPIC_AUTH_TOKEN in the environment — the orchestrator "
                f"passes the Z.ai token via the sandbox env (keychain item "
                f"{KEYCHAIN_SERVICE!r})"
            )
        record = asyncio.run(
            run_session(
                spec["seat"],
                spec["prompt"],
                Path(spec["cwd"]),
                spec["max_turns"],
                spec["timeout_min"],
                base_url=spec.get("base_url") or DEFAULT_BASE_URL,
                token=token,
                timeout_ms=spec.get("timeout_ms") or DEFAULT_TIMEOUT_MS,
                protected=spec.get("protected") or (),
                model=spec.get("model"),
                cli_path=Path(spec["cli_path"]) if spec.get("cli_path") else None,
            )
        )
    except (OSError, json.JSONDecodeError, KeyError, SessionConfigError) as exc:
        print(f"factory.session: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
