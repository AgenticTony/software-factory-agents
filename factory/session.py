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
additionally gets the edit tools disallowed outright. The reviewer runs
headless too, so it shares the same permission bypass: with nobody to
answer permission prompts, a default-mode reviewer could never run the
read-only Bash checks its spec promises — its read-only-ness comes from
the disallowed edit tools, the PreToolUse hooks and the sandbox, not from
prompts.

Seats:

* ``implementer`` — writes code. Prompt-bypassed, sandbox-confined, hooks
  enforce protected paths and the push/gh ban.
* ``reviewer`` — reads, never writes, enforced three ways: the edit tools
  are disallowed at the options layer; its sandbox is **read-only**
  (``build_settings(read_only=True)`` — Bash writes land only in the
  git-excluded ``.home``/``.tmp`` scratch, never the repo); and the
  orchestrator verifies HEAD and the working tree after every review,
  restoring and failing the review on any change. Headless, so
  prompt-bypassed like the implementer (a default-mode Bash call would
  wait forever for an approval nobody can give).

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
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Sequence

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
)
from claude_agent_sdk import query as sdk_query

from factory.runs import GIT_HARDENING
from factory.sandbox import (
    build_settings,
    default_toolchain,
    run_sandboxed,
    write_settings,
)

DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_TIMEOUT_MS = 600_000
KEYCHAIN_SERVICE = "factory-zai"
# The pinned SDK version (pyproject pins claude-agent-sdk exactly); used
# in refusal messages so the fix names the pin to reinstall.
_SDK_VERSION = importlib.metadata.version("claude-agent-sdk")

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


# --------------------------------------------------- reviewer integrity --
# The reviewer must never change what it reviews (owner review round 2 on
# F-09): a reviewer that can write the repo can "fix" the code, watch its
# own fix pass, and report PASS on a diff that isn't the implementer's —
# and its commit would ride the branch F-10 pushes. Two layers hold that
# line: the reviewer's sandbox is read-only (build_settings(read_only=True)
# — writes confined to the git-excluded .home/.tmp scratch), and this
# orchestrator-side guard verifies HEAD and the working tree after every
# reviewer session regardless, restoring them and failing the review if
# anything moved. The guard is the backstop for any future path that lets
# a write through (a settings bug, an srt regression); the verdict text of
# a mutating review is never trusted.

REVIEWER_MUTATION = "reviewer_mutated_workspace"


class _GitState(NamedTuple):
    """Everything a reviewer could change that F-10 might later ship.

    HEAD's sha and ``status --porcelain`` alone miss a new branch or tag,
    a branch switch onto the same commit, a stash, and files written into
    gitignored paths. All of those are compared here (review round 4).
    """

    symref: str  # the branch HEAD points at, or "" when detached
    head: str
    porcelain: str
    refs: tuple[tuple[str, str], ...]  # (refname, sha), stash included
    ignored: tuple[str, ...]  # ignored files present, listed individually


def _git(run_dir: Path, *args: str, check_exit: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(run_dir), *args],
        capture_output=True,
        text=True,
    )
    if check_exit and proc.returncode != 0:
        raise SessionConfigError(
            f"git {args[0]} failed in the run dir: {proc.stderr.strip()[:300]}"
        )
    return proc


def _git_state(run_dir: Path) -> _GitState:
    """Snapshot the run dir's git state via hardened git."""
    symref = _git(run_dir, "symbolic-ref", "-q", "HEAD", check_exit=False)
    refs = _git(run_dir, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    ignored = _git(
        run_dir, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    ).stdout
    return _GitState(
        symref=symref.stdout.strip() if symref.returncode == 0 else "",
        head=_git(run_dir, "rev-parse", "HEAD").stdout.strip(),
        porcelain=_git(run_dir, "status", "--porcelain").stdout,
        refs=tuple(
            tuple(line.split(" ", 1)) for line in refs.splitlines() if line.strip()
        ),
        ignored=tuple(sorted(name for name in ignored.split("\0") if name)),
    )


def _restore_workspace(run_dir: Path, baseline: _GitState) -> None:
    """Undo everything :class:`_GitState` can see changing.

    Refs are put back first (created ones deleted, moved or deleted ones
    restored), then HEAD is re-pointed at its branch (or its detached sha),
    then the tracked tree is reset and untracked non-ignored files cleaned.
    Ignored files are removed only if they are new since the baseline, so a
    warm cache or other pre-existing ignored content survives.
    """
    before = dict(baseline.refs)
    after = dict(_git_state(run_dir).refs)
    for ref in after.keys() - before.keys():
        _git(run_dir, "update-ref", "-d", ref)
    for ref, sha in before.items():
        if after.get(ref) != sha:
            _git(run_dir, "update-ref", ref, sha)
    if baseline.symref:
        _git(run_dir, "symbolic-ref", "HEAD", baseline.symref)
    else:
        _git(run_dir, "update-ref", "--no-deref", "HEAD", baseline.head)
    _git(run_dir, "reset", "--hard", baseline.head)
    _git(run_dir, "clean", "-fd")
    kept = set(baseline.ignored)
    for name in _git_state(run_dir).ignored:
        if name not in kept:
            (Path(run_dir) / name).unlink(missing_ok=True)


def _reviewer_scratch(settings_path: Path, run_dir: Path) -> tuple[Path, Path]:
    """Create the reviewer's writable scratch OUTSIDE the run dir and point
    ``run/.home``/``run/.tmp`` at it as symlinks.

    Why outside: on Linux, the runs_root read-deny makes srt mount a
    read-only bind of the run dir AFTER the writable sub-directory binds,
    burying them — scratch inside the run dir is read-only there (proven on
    bwrap 0.8.0). Outside the run dir the scratch's own writable bind stays
    live; the symlinks keep the HOME/TMPDIR spellings unchanged, and the
    F-05 git excludes (`/.home`, `/.tmp`, no trailing slash) cover a link
    as well as a directory. Both spellings ride in
    ``allowWrite`` so macOS, whose backend doesn't stack that way, accepts
    either.
    """
    scratch = settings_path.with_name(settings_path.name + "-scratch")
    home = scratch / "home"
    tmp = scratch / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    for name, target in ((".home", home), (".tmp", tmp)):
        link = run_dir / name
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)  # a previous non-symlink scratch dir
        elif link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    return home, tmp


def _teardown_reviewer_scratch(run_dir: Path) -> None:
    """Inverse of :func:`_reviewer_scratch`: replace the scratch symlinks
    with the plain ``.home``/``.tmp`` directories the F-05 flow expects.

    The links must not outlive the review: a later implementer run in the
    same run dir would write ``$HOME``/``$TMPDIR`` through links pointing
    OUTSIDE its sandbox — denied there, breaking pip/npm/pytest and Claude
    Code's own state (owner review round 3). Runs on every reviewer exit
    path, including aborts, so the run dir is always left in standard shape.
    """
    for name in (".home", ".tmp"):
        path = run_dir / name
        if path.is_symlink():
            path.unlink()
        path.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ seats --

@dataclass(frozen=True)
class SeatConfig:
    """Per-seat options. ``system_prompt`` stays empty on the seat itself:
    the real prompts are assembled per run from ``factory.prompts`` (F-09)
    and passed in — the prompt files live in the repo, which is unreadable
    inside the sandbox, so the orchestrator reads and assembles them
    outside and hands over the text."""

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
        # Review round finding 8: the default permission mode asks someone
        # to approve each Bash call, and a headless session has nobody to
        # ask — the reviewer could never run the read-only checks its spec
        # promises. Same D3 reasoning as the implementer: the srt sandbox
        # is the permission system; read-only-ness comes from the
        # disallowed edit tools above plus the PreToolUse hooks (protected
        # paths, push/gh ban) and the sandbox's own write/network
        # confinement — not from prompts no one can answer.
        permission_mode="bypassPermissions",
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

# What a session's subprocess environment may keep from the ambient one
# while the stream is live (see _window_environ). The SDK merges ALL of
# os.environ into the Claude Code subprocess, so a direct (unsandboxed)
# run_session call would otherwise hand the CLI every orchestrator secret:
# GH_TOKEN, AWS keys, a foreign ANTHROPIC_API_KEY. The window keeps only
# process mechanics (PATH/LANG/TZ/HOME/TMPDIR, LC_*) plus the proxy names
# — inside the sandbox those carry srt's authenticated proxy, which is the
# session's only network route — and the seat names ride in via options.env.
_RUNNER_ENV_KEEP = frozenset(
    {
        "PATH", "LANG", "TZ", "HOME", "TMPDIR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    }
)
_RUNNER_ENV_PREFIXES = ("LC_",)

# Whether a session's environment window is currently open in this process
# (review round 2). One window at a time, ever: os.environ is a single
# process-wide resource, so overlapping windows would restore each other's
# stripped copies and permanently delete the orchestrator's credentials.
# The orchestrator is single-threaded asyncio; the flag is checked and set
# at non-await points, so no interleaving can race it within one loop.
_WINDOW_OPEN = False


def _window_environ(seat_env: dict[str, str]) -> dict[str, str]:
    """Replace os.environ with the runner allowlist + ``seat_env`` for the
    session's life; returns the saved environment for restoration. The
    orchestrator process is single-session, so windowing the process env
    for the stream's duration is the one pin-independent way to control
    what the SDK's spawn sees (the SDK offers no replace-env option)."""

    def keep(key: str) -> bool:
        upper = key.upper()
        return (
            key in _RUNNER_ENV_KEEP
            or upper in _RUNNER_ENV_KEEP
            or key.startswith(_RUNNER_ENV_PREFIXES)
            or upper.startswith(_RUNNER_ENV_PREFIXES)
        )

    saved = dict(os.environ)
    os.environ.clear()
    for key, value in saved.items():
        if keep(key):
            os.environ[key] = value
    os.environ.update(seat_env)
    return saved


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
    path per DESIGN D2), then the ambient ``ANTHROPIC_AUTH_TOKEN`` — but
    only when the ambient ``ANTHROPIC_BASE_URL`` names the Z.ai endpoint:
    a token exported for another provider (or with no base URL, i.e.
    api.anthropic.com) belongs to that provider and must never be sent to
    Z.ai. On the Linux CI runner neither exists and this returns None,
    which callers must treat as "no session can authenticate here".
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
    ambient_url = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if ambient_url != DEFAULT_BASE_URL:
        return None
    return os.environ.get("ANTHROPIC_AUTH_TOKEN") or None


# ----------------------------------------------------------------- hooks --

# De-quoting for the guard's checks: strip quotes, backticks AND
# backslashes before matching, so `"gh"`, `` `gh` ``, `$()` forms and
# backslash-split words (`g\it pu\sh`, round 2) are all seen as their
# bare selves. Over-matching is the safe direction for a deny-layer.
_BASH_QUOTES = str.maketrans("", "", "\"'`\\")
# Command separators: a new invocation starts after each of these —
# statement ends, pipes, &&, subshells/command substitutions (() and
# backticks are already stripped), and redirections (their targets are
# not programs).
_BASH_SEPARATORS = re.compile(r"[\n;&|()<>]")
# Shell programs whose `-c` payload is itself a command to scan.
_BASH_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"})
# Wrappers that prefix a real command (their own flags / VAR=val tokens
# are skipped before recursing).
_BASH_PREFIX_RUNNERS = frozenset(
    {"env", "command", "exec", "xargs", "nice", "nohup"}
)
# git global options that consume the NEXT token as their value when it
# is not attached with '=' (attached forms are single '-…' tokens).
_GIT_VALUE_OPTS = frozenset(
    {"-C", "--git-dir", "--work-tree", "--namespace", "--exec-path",
     "--super-prefix", "-c"}
)
_BASH_GH_CONFIG = ".config/gh"
# Redirection targets: `> file`, `>> file`, `2>file` (the fd digit and any
# spacing between the brackets is optional).
_BASH_REDIRECT = re.compile(r"(?:^|[\s;&|)])(?:\d)?>>?\s*([^\s;&|<>]+)")
# Utilities whose arguments write files; value = how many trailing args
# are destinations (mv/cp/install: src… dest — only the last one writes).
_BASH_WRITE_CMDS = {
    "tee": "all",
    "rm": "all",
    "truncate": "all",
    "shred": "all",
    "unlink": "all",
    "mv": "last",
    "cp": "last",
    "install": "last",
    "sed": "all_after_script",  # sed -i SCRIPT files…
}
_BASH_OUTPUT_FLAGS = {"-o", "--output", "-O", "--output-document"}


def _deny(reason: str) -> dict[str, Any]:
    """A PreToolUse deny in the SDK's SyncHookJSONOutput shape."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _path_forms(path_text: str) -> set[str]:
    """Every textual form worth matching: as given, normalised (``./x``,
    ``a/../x`` collapsed) and fully resolved (symlinks — on macOS the run
    dir's /private/var prefix vs its /var spelling). Resolution of a
    not-yet-existing file is fine (non-strict)."""
    forms = {path_text}
    try:
        forms.add(os.path.normpath(path_text))
    except (OSError, ValueError):
        pass
    try:
        forms.add(str(Path(path_text).resolve()))
    except (OSError, ValueError):
        pass
    return forms


def _path_candidates(file_path: str, cwd: str) -> set[str]:
    """The forms a file_path is matched against, crossed with the forms of
    its cwd: as-given, absolute (cwd-joined, from both the given and the
    resolved cwd), and cwd-relative (an absolute path inside the run).
    """
    candidates: set[str] = set()
    raw = Path(file_path)
    cwds = _path_forms(cwd) if cwd else set()
    candidates |= _path_forms(file_path)
    if raw.is_absolute():
        for cwd_text in cwds:
            cwd_path = Path(cwd_text)
            for form in _path_forms(file_path):
                try:
                    candidates.add(str(Path(form).relative_to(cwd_path)))
                except ValueError:
                    pass
    else:
        for cwd_text in cwds:
            candidates |= _path_forms(str(Path(cwd_text) / file_path))
    return candidates


def _matches_protected(file_path: str, cwd: str, patterns: tuple[str, ...]) -> str | None:
    """The first pattern the path matches, case-insensitively (macOS
    filesystems are case-insensitive; lowercasing both sides also
    over-denies `Schema/x` on case-sensitive Linux, which errs safe), or
    None."""
    if not file_path or not patterns:
        return None
    candidates = _path_candidates(file_path, cwd)
    lowered = {form.lower() for form in candidates}
    for pattern in patterns:
        if any(fnmatch.fnmatchcase(form, pattern.lower()) for form in lowered):
            return pattern
    return None


def protected_path_hook(protected: "Sequence[str]") -> HookMatcher:
    """PreToolUse hook: deny Write/Edit/NotebookEdit into ``protected``
    globs (``paths.protected`` from the project profile). Paths are
    normalised (``./``, ``a/../``) and resolved (symlink prefixes such as
    macOS's /var → /private/var) before matching, case-insensitively —
    traversal, dot-relative, case and symlink spellings of a protected
    path all deny. Unprotected paths get no opinion (``{}`` — the tool
    proceeds)."""

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
        cwd = str(input.get("cwd") or "")
        matched = _matches_protected(file_path, cwd, patterns)
        if matched:
            return _deny(f"{file_path} matches protected path {matched!r}")
        return {}

    return HookMatcher(matcher="Write|Edit|NotebookEdit", hooks=[_hook])


def _bash_write_targets(command: str) -> list[str]:
    """Best-effort file paths a Bash command writes to: redirection
    targets, the argument patterns of the common write utilities, and the
    value following an output flag (-o/--output/-O/--output-document).
    No shell parsing — this is a deny-layer, not a parser; the
    authoritative protected-path gate is L0's diff policy, which inspects
    the actual diff after the session."""
    targets = [m.group(1) for m in _BASH_REDIRECT.finditer(command)]
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if tokens:
        head = Path(tokens[0]).name
        args = tokens[1:]
        mode = _BASH_WRITE_CMDS.get(head)
        if mode == "last" and args:
            targets.append(args[-1])
        elif mode == "all":
            targets.extend(a for a in args if not a.startswith("-"))
        elif mode == "all_after_script" and "-i" in args:
            rest = args[args.index("-i") + 1:]
            targets.extend(rest[1:] if rest else [])
        elif head in {"curl", "wget"}:
            for index, flag in enumerate(args):
                if flag in _BASH_OUTPUT_FLAGS and index + 1 < len(args):
                    targets.append(args[index + 1])
                for prefix in tuple(_BASH_OUTPUT_FLAGS):
                    if flag.startswith(prefix + "="):
                        targets.append(flag.split("=", 1)[1])
    return targets


def _bash_invocations(dequoted: str) -> list[list[str]]:
    """The command's invocations as token lists: the de-quoted command
    split on shell separators (;, |, &, newlines, subshells/command
    substitutions, redirections), each part whitespace-tokenised. Empty
    parts (between adjacent separators) drop out."""
    return [
        part.split()
        for part in _BASH_SEPARATORS.split(dequoted)
        if part.split()
    ]


def _invocation_violation(tokens: list[str]) -> str | None:
    """What an invocation actually runs: "git push", "gh", or None.

    Denies only when the invocation's *program* is git with `push` as its
    subcommand (global options like ``-C .``/``-c k=v`` skipped first), or
    gh itself — so a commit message about push notifications, ``git log
    --grep=push`` or ``grep -rn push src/`` pass (round 3). Shells'
    ``-c`` payloads and prefix wrappers (env/command/exec/xargs…) are
    recursed into, and the de-quoted token stream already merges
    quote-split and backslash-split words (round 2)."""
    if not tokens:
        return None
    program = Path(tokens[0]).name
    if program == "git":
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in _GIT_VALUE_OPTS:
                index += 2  # the option and its separate value
                continue
            if token.startswith("-"):
                index += 1  # a flag; '='-attached values are one token
                continue
            return "git push" if token == "push" else None
        return None
    if program == "gh":
        return "gh"
    if program in _BASH_SHELLS:
        try:
            payload = tokens.index("-c", 1) + 1
        except ValueError:
            return None
        return _invocation_violation(tokens[payload:])
    if program in _BASH_PREFIX_RUNNERS:
        rest = [
            token
            for token in tokens[1:]
            if not token.startswith("-") and "=" not in token
        ]
        return _invocation_violation(rest)
    return None


def bash_guard_hook(protected: "Sequence[str]" = ()) -> HookMatcher:
    """PreToolUse hook over Bash: deny invocations that actually run
    ``git push`` (any git global options first) or the ``gh`` CLI —
    including quoted, backticked, ``$(…)``, ``sh -c`` and backslash-split
    spellings, reached by de-quoting and splitting the command into its
    invocations (round 3's fix: a command that merely *mentions* push or
    gh — commit messages, ``--grep=push``, code search — passes). Also
    denies anything touching ``~/.config/gh`` and best-effort writes
    (redirections, tee/mv/cp/rm/sed -i/curl -o…) into the protected
    globs. The orchestrator (outside the sandbox, with its own
    credentials) pushes and opens PRs — the WO's rule that a session
    never does. Residual (documented, best-effort deny-layer — NOT an
    evasion-proof boundary): pipes feeding a shell's stdin
    (``echo git push | sh``), git aliases, and unenumerated wrappers
    still slip through; the structural barriers underneath are that the
    sandbox holds no GitHub token and has no network route to
    github.com."""

    patterns = tuple(protected)

    async def _hook(
        input: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        command = str((input.get("tool_input") or {}).get("command", ""))
        # All checks run on the de-quoted command (round 2): backslash-split
        # and quote-concatenated spellings (`g\it push`, `sh -c 'git pu''sh'`)
        # must be seen as their bare selves.
        dequoted = command.translate(_BASH_QUOTES)
        for invocation in _bash_invocations(dequoted):
            violation = _invocation_violation(invocation)
            if violation == "git push":
                return _deny("git push is not allowed inside a session")
            if violation == "gh":
                return _deny("the gh CLI is not allowed inside a session")
        if _BASH_GH_CONFIG in dequoted:
            return _deny("~/.config/gh is not accessible inside a session")
        if patterns:
            cwd = str(input.get("cwd") or "")
            for target in _bash_write_targets(dequoted):
                matched = _matches_protected(target, cwd, patterns)
                if matched:
                    return _deny(
                        f"writing {target} matches protected path {matched!r}"
                    )
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
    system_prompt: str | None = None,
) -> ClaudeAgentOptions:
    """Build the per-seat ClaudeAgentOptions (WO F-06 options list).

    ``setting_sources=[]`` — no user/project/local Claude settings, so a
    session can't pick up personal config. ``env`` is exactly the three
    session names (see :func:`session_env`). Hooks: protected-path +
    bash-guard PreToolUse callbacks, in-process via HookMatcher.
    ``system_prompt`` is the assembled seat prompt (F-09's
    ``factory.prompts``); it overrides the seat's static default, which
    stays empty because the prompt *files* are unreadable inside the
    sandbox — the orchestrator assembles them outside and passes the text.
    """
    seat_config = _seat(seat)
    options = ClaudeAgentOptions(
        cwd=str(Path(cwd)),
        env=session_env(base_url, token, timeout_ms),
        max_turns=max_turns,
        system_prompt=system_prompt
        if system_prompt is not None
        else seat_config.system_prompt,
        setting_sources=[],
        allowed_tools=list(seat_config.allowed_tools),
        disallowed_tools=list(seat_config.disallowed_tools),
        permission_mode=seat_config.permission_mode,
        hooks={
            "PreToolUse": [
                protected_path_hook(protected),
                bash_guard_hook(protected),
            ]
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
    input+output tokens when the session touched several. Counts may come
    back null from the CLI; they weigh zero rather than crash."""

    def weight(name: str) -> int:
        entry = model_usage.get(name) or {}
        return int(entry.get("inputTokens") or 0) + int(
            entry.get("outputTokens") or 0
        )

    return max(model_usage or {}, key=weight, default=None)


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
    system_prompt: str | None = None,
) -> SessionRecord:
    """Run one seat's session and return what it recorded.

    Args:
        seat: Seat name (see :data:`SEATS`).
        prompt: The task prompt (the user message). The seat's *system*
            prompt is the assembled seat prompt (F-09) via ``system_prompt``,
            falling back to the (empty) seat config default.
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
        system_prompt: The assembled seat prompt (F-09), overriding the
            seat's static default.

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
        system_prompt=system_prompt,
    )

    result_message: ResultMessage | None = None
    stream = None
    # One window per process, ever (review round 2): a second session
    # opening while the first's window is live would save the first's
    # STRIPPED environment and restore it last — permanently deleting the
    # orchestrator's credentials. Refuse loudly instead; sessions in one
    # process are serial (the F-10 loop is).
    global _WINDOW_OPEN  # noqa: PLW0603 — module-level window ownership
    if _WINDOW_OPEN:
        raise SessionConfigError(
            "another session's environment window is already open in this "
            "process — sessions must run serially in one process (the F-10 "
            "loop does); overlapping them would corrupt the orchestrator "
            "environment."
        )
    _WINDOW_OPEN = True
    try:
        # Window the process env for the session's life: the SDK merges ALL
        # of os.environ into the Claude Code subprocess, so without this a
        # direct (unsandboxed) call hands the CLI every orchestrator secret.
        # Restored in the finally — the orchestrator process continues
        # unchanged. Inside the sandbox the ambient env is already scrubbed;
        # the window is then a no-op that still holds (and keeps the proxy
        # vars the session needs).
        saved_env = _window_environ(options.env)
        try:
            started = time.monotonic()
            deadline = started + timeout_min * 60.0
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
            finally:
                # Every exit path closes the stream (review round 2): the
                # three explicit calls missed non-SDK raises, which
                # propagate by design — a bug must not also leak the CLI
                # subprocess. The close runs while the window is still up;
                # a refusal from an unwinding generator is suppressed.
                await _close_stream(stream)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
    finally:
        _WINDOW_OPEN = False

    duration_ms = int((time.monotonic() - started) * 1000)
    if result_message is None:
        return SessionRecord(
            seat=seat,
            status="failed",
            reason="no_result_message",
            duration_ms=duration_ms,
        )
    return _record_from(result_message, seat, duration_ms)


async def _close_stream(stream: Any) -> None:
    """Close the SDK stream (its aclose tears down the CLI subprocess).
    An already-closed or unwinding generator may refuse; that refusal is
    suppressed — closing is cleanup, never the failure being reported."""
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await asyncio.wait_for(aclose(), timeout=10)
    except (Exception, asyncio.CancelledError):
        pass


# ------------------------------------------------- the sandboxed entry point --

def _bundled_cli() -> Path | None:
    """The Claude Code CLI bundled inside the pinned claude_agent_sdk
    package (platform wheels ship the native binary; verified for macOS
    and manylinux). This is THE pinned CLI: the SDK pin fixes its version,
    unlike the auto-updating native install on PATH. Sandboxed sessions
    run it and nothing else; an explicit ``cli_path`` still wins for
    tests, but is never resolved from PATH or ~/.local/bin implicitly."""
    import claude_agent_sdk

    name = "claude.exe" if sys.platform == "win32" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return bundled if bundled.exists() else None


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
    system_prompt: str | None = None,
    token: str | None = None,
    cli_path: Path | None = None,
    srt_bin_path: Path | None = None,
    teardown_grace_sec: float = 120.0,
) -> SessionRecord:
    """Run one session with the whole runner process inside the F-05 sandbox.

    Execs the entry bootstrap (see :func:`_entry_bootstrap`) under srt —
    the SDK's Claude Code subprocess inherits the confinement, and it is
    the **bundled, version-pinned CLI** (finding 5): an explicit
    ``cli_path`` wins for tests, but nothing is ever auto-discovered from
    PATH or ~/.local/bin. The settings come from the project profile plus
    one deliberate carve-out: the staged runner dir (holding the factory
    package copy and the session spec) is re-allowed for reading — the
    rest of the repo (prompts, schemas, validator docs) stays unreadable
    to the session, and the spec never touches the clone (finding 6).

    Args:
        run_dir: The run dir (the F-05 clone); the session's cwd.
        profile: The project profile (docs/network allowlists).
        factory_root: Explicit factory root; when omitted it is derived
            (with the layout asserted) from
            ``<root>/runs/<project>/<run-id>``.
        settings_path: Where the srt settings go. Default: the RunDir
            convention ``<factory_root>/sandbox/<project>-<run-id>.json``
            — the same path ``cleanup_run`` removes, whether factory_root
            was passed or derived (finding 2).
        max_turns/timeout_min: Handed to the session. The *outer*
            run_sandboxed wall clock is ``timeout_min`` plus
            ``teardown_grace_sec`` so the in-session clock fires first.
        system_prompt: The assembled seat prompt (F-09) — text, never a
            path: the repo (prompts included) is unreadable inside the
            sandbox, so the orchestrator assembles it outside.
        token: The Z.ai token; default :func:`seat_token`. None and no
            credential available → :class:`SessionConfigError` (refuses to
            start a session that cannot authenticate).
        cli_path: Explicit CLI override (tests). Default: the SDK's
            bundled binary; missing bundle → SessionConfigError rather
            than silently running an unpinned CLI.

    Returns:
        The session's record (any status — completed/failed/aborted are
        all recorded outcomes). For the reviewer seat the record is forced
        to ``failed`` with reason :data:`REVIEWER_MUTATION` if HEAD or the
        working tree changed during the session (the workspace is restored
        first, so nothing the reviewer wrote survives).

    Raises:
        SessionConfigError: No token, no bundled CLI, an underivable
            layout, an unreadable run-dir git state (reviewer seat), or an
            entry that produced no parsable record (an orchestrator-side
            fault that must be loud).
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
    # Settings path first (derived once): the reviewer's scratch sibling and
    # the staged runner both hang off its name.
    if settings_path is None:
        if factory_root is None:
            runs_root = run_dir.parent.parent
            if runs_root.name != "runs":
                raise SessionConfigError(
                    f"cannot derive the factory root from {run_dir}: its layout "
                    "is not runs/<project>/<run-id>. Pass factory_root "
                    "explicitly, or the settings would land in a directory "
                    "cleanup_run never looks at."
                )
            factory_root = runs_root.parent
        settings_path = (
            Path(factory_root) / "sandbox" / f"{run_dir.parent.name}-{run_dir.name}.json"
        )

    # Reviewer integrity (owner review rounds 2 and 4): the reviewer gets a
    # read-only sandbox and a workspace guard (branch, HEAD, tree, refs,
    # ignored files; see _GitState). The scratch links exist
    # BEFORE the baseline so their (untracked) presence can never look
    # like a mutation between the two snapshots.
    review_guard = seat == "reviewer"
    reviewer_writable: tuple[Path, Path] = ()
    baseline: _GitState | None = None
    record: SessionRecord | None = None
    # Everything from the scratch symlinks on sits inside try/finally (review
    # round 4): a setup failure must still tear the links down, and the
    # integrity backstop must run on EVERY exit path, timeouts and missing
    # records included, not only after a record parses.
    try:
        if review_guard:
            reviewer_writable = _reviewer_scratch(Path(settings_path), run_dir)
            baseline = _git_state(run_dir)
        record = _run_prepared(
            seat, prompt, run_dir, profile, settings_path,
            factory_root=factory_root, max_turns=max_turns,
            timeout_min=timeout_min, base_url=base_url, timeout_ms=timeout_ms,
            protected=protected, model=model, system_prompt=system_prompt,
            token=token, cli_path=cli_path, srt_bin_path=srt_bin_path,
            teardown_grace_sec=teardown_grace_sec,
            read_only=review_guard, writable_roots=reviewer_writable,
        )
    finally:
        try:
            # The reviewer-integrity backstop: whatever happened in the
            # session, the workspace must be exactly what it was before. A
            # mutation fails the review (the verdict text of a reviewer that
            # changed the code is not evidence) and is undone, so nothing it
            # wrote can ride the branch F-10 pushes. On an exception path
            # the restore runs and the original exception still propagates.
            if baseline is not None and _git_state(run_dir) != baseline:
                _restore_workspace(run_dir, baseline)
                if record is not None:
                    record.status = "failed"
                    record.reason = REVIEWER_MUTATION
        finally:
            if review_guard:
                _teardown_reviewer_scratch(run_dir)
    return record


def _run_prepared(
    seat: str,
    prompt: str,
    run_dir: Path,
    profile: dict,
    settings_path: Path,
    *,
    factory_root: Path | None,
    max_turns: int,
    timeout_min: float,
    base_url: str,
    timeout_ms: int,
    protected: "Sequence[str]",
    model: str | None,
    system_prompt: str | None,
    token: str,
    cli_path: Path | None,
    srt_bin_path: Path | None,
    teardown_grace_sec: float,
    read_only: bool,
    writable_roots: "Sequence[Path]",
) -> SessionRecord:
    """Stage the runner, write settings, run the sandboxed entry and parse
    its record. :func:`run_session_sandboxed` wraps this in the reviewer
    guard; this function knows nothing about seats beyond ``read_only``."""
    # The pinned CLI (finding 5): the SDK's bundled binary, or an explicit
    # test override. An auto-discovered ~/.local/bin/claude is never used —
    # it updates itself out from under the pin.
    cli = Path(cli_path).expanduser() if cli_path is not None else _bundled_cli()
    if cli is None:
        raise SessionConfigError(
            "the pinned claude-agent-sdk wheel bundles no Claude CLI for this "
            "platform — reinstall it (`pip install --force-reinstall "
            "claude-agent-sdk==" + _SDK_VERSION + "`). Sandboxed sessions run "
            "the bundled, version-pinned CLI and never an auto-updating one."
        )

    runner_dir = _stage_runner(settings_path.with_name(settings_path.name + "-runner"))
    # The spec lives inside the staged runner dir — OUTSIDE the clone
    # (finding 6): a stray file in the run dir would ride into the PR via
    # `git add -A`, leaking the prompt and the orchestrator's paths.
    spec_path = runner_dir / "spec.json"
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
                "system_prompt": system_prompt,
                "cli_path": str(cli),
            }
        )
    )

    settings = build_settings(
        profile,
        run_dir,
        model_endpoint=base_url,
        factory_root=factory_root,
        toolchain=default_toolchain(extra=[cli]),
        allow_read=[runner_dir],
        read_only=read_only,
        writable_roots=writable_roots,
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
        record = SessionRecord.from_dict(json.loads(lines[-1]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise SessionConfigError(
            "the sandboxed session entry produced an unparsable record "
            f"({exc}): {lines[-1][:500]}"
        ) from exc

    return record


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
                system_prompt=spec.get("system_prompt"),
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
