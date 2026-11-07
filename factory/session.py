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
* ``verifier`` (F-38) — reads, never writes, same three ways: it votes
  reviewer findings in or out, a judge of judges, so it carries the
  reviewer's entire read-only contract (edit tools disallowed, read-only
  sandbox, the same integrity guard).

Every seat's PreToolUse layer also denies a Bash call that sets
``dangerouslyDisableSandbox`` (F-42): the srt sandbox IS the confinement,
so the flag asks to skip the one boundary that holds — the deny fires
under ``bypassPermissions``, names the rule ("the sandbox is not
negotiable"), and each denial is counted (attempt record, ``factory
status``'s weekly total) and marked on its F-30 trace event. The CLI's
own commit/PR attribution is switched off in every seat's settings
(:data:`ATTRIBUTION_OFF`), and L0/publish refuse the pattern list in
:mod:`factory.attribution` besides — a trailer cannot leave the factory
through the branch, the PR text, or the CLI's defaults.

Every seat carries one extra tool (F-27): ``vent(category, text)``, an
in-process SDK tool (``factory.vent``) the CLI exposes as
``mcp__vent__vent``, listed under that name in ``allowed_tools``. The
handler runs inside this sandboxed runner, so it only appends to an
in-memory collector; the vents ride the record out and the orchestrator
flushes them into the task's progress log. Venting never touches the
outcome, the attempt record or the budget.

Toolkit (F-22): a project may give its sessions a vetted, version-
pinned toolkit (skills, plugins, read-only MCP servers) chosen in
``project.yaml`` and admitted in ``toolkit/index.yaml``. The session
gets it from a **staged read-only copy outside the clone** (the
runner-staging pattern): skills ride one local plugin so
``setting_sources=[]`` keeps holding — nothing loads from the owner's
personal Claude setup — and MCP servers spawn from the staged
descriptor as children of this sandboxed runner, each tool granted one
by one. The record's ``skills`` lists what loaded, ``name@hash``, for
F-19's attribution.

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

Record transport (F-56): the sandboxed entry writes its record to a
FILE the orchestrator names in the spec — ``<settings>-record/
record.json`` (:func:`record_path_for`), a per-session dir outside the
clone, carved out of the sandbox's denies for writing on every seat
shape (the WRITE bind is what persists it under the read-denied factory
root on Linux; the read bind rides along as belt-and-braces — review
round 1, finding 4), wiped fresh before the run and written atomically
(temp-in-dir, fsync, rename). stdout carries only ``factory-trace``
lines; a record-looking stdout line is a foreign line the forwarder
ignores. The record crossed the srt stdout pipe as one JSON line until
F-56 — three of four real jobfinderos importer runs lost a completed
session to a truncated line (the fourth's line was exactly 81,920
bytes, cut inside ``permission_denials``). A record that does not
arrive whole is a :class:`SessionRecordError` that names the srt exit
code, the signal if the process died by one, srt's own kill report when
it made one, a seam-scrubbed stderr tail, and where the full streams
were saved; ``permission_denials`` is bounded at record-build time
(:data:`DENIAL_LIST_MAX` entries, :data:`DENIAL_TOOL_INPUT_MAX_CHARS`
per tool_input, the dropped count on the record).

Secrets: the session env is exactly three names — the Z.ai base URL,
the Z.ai token (the one secret the sandbox holds, DESIGN D2/D3) and the
API timeout. ``setting_sources=[]`` so no user/project Claude config is
loaded. Nothing else is read from the ambient environment, ever.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
)
from claude_agent_sdk import query as sdk_query

from factory.egress import check_secrets, scrub_session_text
from factory.runs import FACTORY_ROOT_DEFAULT, GIT_HARDENING, RunError, new_run_id
from factory.sandbox import (
    build_settings,
    default_toolchain,
    escapee_event,
    run_sandboxed,
    write_settings,
)
from factory.state import _atomic_write_json, now_iso
from factory.toolkit import (
    ENV_SCRUB,
    STAGED_PLACEHOLDER,
    TOOLKIT_PLUGIN_NAME,
    stage_toolkit,
)
from factory.trace import STREAM_PREFIX, TraceWriter, scrub_literal, snippet
from factory.vent import VENT_QUALIFIED_NAME, VENT_TOOL_NAME, VentCollector, vent_server

DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_TIMEOUT_MS = 600_000
KEYCHAIN_SERVICE = "factory-zai"
# The pinned SDK version (pyproject pins claude-agent-sdk exactly); used
# in refusal messages so the fix names the pin to reinstall.
_SDK_VERSION = importlib.metadata.version("claude-agent-sdk")

# F-42 AC-3: the CLI's own attribution default, switched off at the
# source. The pinned CLI's settings schema reads ``attribution`` (the
# boolean key ``includeCoAuthoredBy`` is its deprecated form — "Use
# attribution instead" in the binary's own schema text): resolver order
# verified in the bundled binary — ``attribution.commitTrailers: false``
# is the explicit "disabled" verdict that stops the CLI appending a
# ``Co-Authored-By`` trailer to the session's ``git commit`` calls, and
# the ``commit``/``pr`` texts ("Empty string hides attribution", per the
# schema) suppress the commit attribution line and the "Generated with"
# PR footer. Delivered via the SDK's ``settings`` field (``--settings``
# inline JSON), which applies with ``setting_sources=[]`` — that flag
# only shuts filesystem settings discovery off.
ATTRIBUTION_OFF = {
    "attribution": {"commitTrailers": False, "commit": "", "pr": ""},
}

# F-42 AC-4: the reason every seat's Bash bypass denial carries — it
# tells the session the rule, not just the refusal.
SANDBOX_DENY_REASON = (
    "dangerouslyDisableSandbox is not available: the sandbox is not "
    "negotiable — run the command without it"
)

# F-56 AC-1: the record file's name inside the session's own record dir
# (see :func:`record_path_for`). One fixed name, one dir per session.
RECORD_FILE_NAME = "record.json"


def record_path_for(settings_path: Path) -> Path:
    """The record file this session's result travels by (F-56 AC-1).

    ``<settings>-record/record.json`` — a sibling of the srt settings,
    OUTSIDE the clone (nothing there can ride a diff), named per session
    (the fan-out's per-session settings give every seat its own), and a
    path the orchestrator both wipes fresh and reads back through
    :func:`_read_record_file`. The DIR is the sandbox carve-out
    (``record_write`` in :func:`factory.sandbox.build_settings`): the
    entry's atomic write (temp in the same dir, rename) needs the dir
    writable, and on Linux only a bound path persists on the host.
    """
    settings_path = Path(settings_path)
    return (settings_path.parent
            / (settings_path.name + "-record")
            / RECORD_FILE_NAME)


# F-56 AC-3: permission_denials bounds, applied where the record is BUILT
# (the entry's _carry, before anything is written or sent) so the file and
# every consumer see the bounded form.
#
# The real jobfinderos evidence (2026-11-05): the reviewer's denied Bash
# commands were echoed back in full and the fourth run's record line was
# exactly 81,920 bytes, cut inside permission_denials. A denial's
# tool_input is evidence of WHAT was denied, not a second copy of the
# command stream: 2000 characters name any real command (the F-42 deny
# reasons are ~140 chars; a 2000-char command is already pathological),
# and the heaviest real session recorded single-digit denials, so 100
# entries is an order of magnitude of headroom while capping the whole
# list at ~100 x (2000 + entry overhead) — well under a quarter megabyte
# of record no matter what the session did.
DENIAL_TOOL_INPUT_MAX_CHARS = 2000
DENIAL_LIST_MAX = 100
# The per-ENTRY bound (review round 1, finding 3): any kept entry —
# whatever its shape — serialises to at most the tool_input cap plus a
# 400-char envelope for the entry's own keys (tool_name and kin), so the
# total claim holds for non-dict entries and large non-tool_input fields
# too: DENIAL_LIST_MAX x DENIAL_ENTRY_MAX_CHARS ~= 240 KB of denial list,
# whatever the session did.
DENIAL_ENTRY_MAX_CHARS = DENIAL_TOOL_INPUT_MAX_CHARS + 400

# F-56 review round 1 (finding 2): the record READ is bounded. The record
# file lives in the session's own writable carve-out, so a hostile or
# wedged session can plant a file of any size; the orchestrator reads at
# most RECORD_MAX_BYTES+1 and refuses (through the same SessionRecordError)
# past it. 64 MiB is two orders of magnitude above any real record now
# that permission_denials is bounded (AC-3): a legitimate record is a few
# MB at most (a 2 MB result was the AC-1 test), so anything past this is
# planted or broken, and the bound is refused, never silently truncated.
RECORD_MAX_BYTES = 64 * 1024 * 1024

# F-56 AC-2: how much of the (already seam-scrubbed) stderr tail the
# record-failure message carries — the same cap the pre-F-56 no-record
# message used, kept so a 50 KB stderr cannot flood the terminal or the
# attempt notes; the full streams are in the saved file beside it.
RECORD_STDERR_TAIL_CHARS = 2000

# F-56 AC-4: srt's own words when IT killed the command, quoted from the
# pinned cli.js (node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js,
# pinned 0.0.79) — matched against the RAW stderr (ASCII, srt-authored)
# and reported as a distilled fact beside the raw tail by
# :func:`_srt_kill_fact`. The factory's own spawn never passes
# --control-fd (run_sandboxed builds ``srt --settings … -c …``), so the
# dead-channel kill cannot fire in a factory run — pinned by
# tests/test_f56.py; the signature is still recognised because a future
# caller of run_sandboxed (or a changed srt) must not turn it back into a
# silent truncation.
_SRT_KILL_SIGNATURES = (
    # cli.js onControlError (line 315): the control channel died before
    # delivering a config update, so srt SIGTERM'd the command (SIGKILL
    # after KILL_GRACE_MS) and exits 1 — the status it died with is not
    # the run's result.
    "failed before delivering an update",
    # cli.js child-exit handler (line 458): the command died by a signal
    # srt observed (any but SIGINT/SIGTERM, which srt maps to exit 0).
    "Process killed by signal:",
)


class BypassDenials:
    """F-42 AC-5: shared state between the deny hook and the F-30 tap.

    The hook is the authority on what it denied (the tool_use_id it saw,
    and the running count); the tap marks the denial onto the call's
    ``tool_call`` trace event — closed or flushed open at stream end — so
    each denial lands in the F-30 trace instead of being lost in a tool
    result. ``run_session`` stamps ``count`` onto the
    :class:`SessionRecord`, and the orchestrator sums it onto the attempt
    record for ``factory status``'s weekly total.
    """

    def __init__(self) -> None:
        self.denied: set[str] = set()
        self.count: int = 0

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


class SessionRecordError(SessionConfigError):
    """The sandboxed entry ran, but the record it owed the orchestrator
    at :func:`record_path_for` is missing, unreadable, or not a valid
    record (F-56 AC-2).

    Subclasses :class:`SessionConfigError` (the orchestrator-side fault
    that must be loud) while naming the narrower fact — the session
    STARTED, so callers with salvage to run before cleanup (the
    importer's ``$TMPDIR/proposal.json``) key on this class. The message
    is built by the one record-failure helper and names the srt exit
    code, the signal if the process died by one, a scrubbed stderr tail,
    whether srt itself reported killing the command, and the path the
    full stdout and stderr were saved to.

    ``exit_code`` is srt's returncode (None on the wall-clock path,
    where the orchestrator's own kill left no code to read).
    """

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code



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
    home, tmp = _scratch_dirs(scratch)
    for name, target in ((".home", home), (".tmp", tmp)):
        link = run_dir / name
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)  # a previous non-symlink scratch dir
        elif link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    return home, tmp


def _scratch_dirs(scratch: Path) -> tuple[Path, Path]:
    """The (home, tmp) writable dirs under one scratch root, created.

    The single-session reviewer path and every fan-out session create
    their scratch the same way (F-38 review finding 1); only the root
    differs — each fan-out session gets its OWN ``<settings>-scratch``,
    never a shared one.
    """
    home = scratch / "home"
    tmp = scratch / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
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
        # Both seats carry the vent tool (F-27 AC-1): a grant-list entry
        # (allowed_tools auto-allows without prompting; it restricts
        # nothing), with the in-process server attached per session by
        # build_options from the run's collector. The grant names the tool
        # as the CLI sees it (``mcp__vent__vent``), so it still holds if a
        # seat ever leaves bypassPermissions (review finding 4).
        allowed_tools=(VENT_QUALIFIED_NAME,),
        # See the module docstring: the sandbox is the permission system;
        # prompts are bypassed, hooks + srt enforce the policy.
        permission_mode="bypassPermissions",
    ),
    "reviewer": SeatConfig(
        name="reviewer",
        allowed_tools=(VENT_QUALIFIED_NAME,),
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
    "verifier": SeatConfig(
        name="verifier",
        allowed_tools=(VENT_QUALIFIED_NAME,),
        # F-38: the verifier votes reviewer findings in or out, so it is
        # every bit the judge the reviewer is — same read-only contract
        # (no edit tools, same integrity guard in run_session_sandboxed).
        # A verifier that could write the workspace could "fix" the code a
        # finding names and then vote the finding out on what it wrote.
        disallowed_tools=("Write", "Edit", "NotebookEdit"),
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
# GIT_TEMPLATE_DIR is process mechanics too (INC-006): sandbox_env points it
# at an empty dir, and without it every `git init`/`git clone` the agent's
# Bash runs beneath the srt cwd dies copying hook samples into the blocked
# .git/hooks. It carries a path, never a credential. CLAUDE_CODE_TMPDIR is
# the same class (F-31 run blocker): the CLI builds its per-uid Bash-tool
# scratch root from it or a hardcoded /tmp — sandbox_env points it into the
# run, and a window that strips it kills every Bash call again.
_RUNNER_ENV_KEEP = frozenset(
    {
        "PATH", "LANG", "TZ", "HOME", "TMPDIR", "GIT_TEMPLATE_DIR",
        "CLAUDE_CODE_TMPDIR",
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
    keychain = _keychain_token()
    if keychain is not None:
        return keychain
    ambient_url = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if ambient_url != DEFAULT_BASE_URL:
        return None
    return os.environ.get("ANTHROPIC_AUTH_TOKEN") or None


def _keychain_token() -> str | None:
    """The ``factory-zai`` keychain item's secret, or None where absent.

    Its own function so tests can stub the keychain step and exercise the
    env-fallback rules hermetically on a Mac that has the item.
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # no macOS keychain here (or it hung) — try the env
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


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


def sandbox_bypass_hook(denials: BypassDenials | None = None) -> HookMatcher:
    """PreToolUse hook over Bash: deny any call whose input sets
    ``dangerouslyDisableSandbox`` (F-42 AC-4).

    Every seat gets it — the implementer and, through the same
    :func:`build_options`, every fan-out reviewer and the verifier — and
    it fires under ``bypassPermissions`` (a deny hook still blocks there,
    F-06's established fact; the sandbox is the confinement, so a flag
    that asks the CLI to skip ITS sandbox layer is asking to leave the
    one boundary that holds). The outer srt sandbox confined the F-31
    attempts anyway (the bind failed with the flag set) — this hook makes
    the attempt itself visible instead of an invisible habit: each denial
    increments ``denials`` (F-30 trace + attempt record, AC-5) and the
    reason states the rule so the session learns it in-turn.
    """

    state = denials if denials is not None else BypassDenials()

    async def _hook(
        input: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        tool_input = input.get("tool_input") or {}
        # Truthy, not ``is True``: the deny-layer over-matches on purpose
        # (a stringy "true" is not a legitimate spelling of the flag — the
        # tool schema says boolean). ``false`` passes: explicitly unset.
        if not tool_input.get("dangerouslyDisableSandbox"):
            return {}
        if tool_use_id is not None:
            state.denied.add(str(tool_use_id))
        state.count += 1
        return _deny(SANDBOX_DENY_REASON)

    return HookMatcher(matcher="Bash", hooks=[_hook])


# ----------------------------------------------------------- options/record --


def mcp_guard_hook(granted: "frozenset[tuple[str, str]]") -> HookMatcher:
    """PreToolUse hook over MCP tools: the enforcement under
    ``bypassPermissions`` (F-22 review, PR #77 finding 2).

    Both seats run prompt-bypassed (nobody can answer a permission
    prompt), and under bypass the CLI's ``allowed_tools`` only
    *pre-approves* — it restricts nothing: a live turn proved it (a
    stub server's UNLISTED write tool ran unchallenged, empty
    ``permission_denials``). A deny hook still blocks under
    ``bypassPermissions`` (F-06's established fact), so THIS hook is
    what makes AC-5's "tools enabled one by one" real: every
    ``mcp__<server>__<tool>`` call whose pair the seat's toolkit never
    granted — an unlisted tool of a listed server, or any tool of a
    server that is not in the toolkit at all — is denied. Built-in
    tools never match the hook's matcher and pass untouched; the vent
    tool is granted like any other in-process server. Fail closed: a
    malformed ``mcp__`` name matches no grant.
    """

    async def _hook(
        input: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        name = str(input.get("tool_name") or "")
        if not name.startswith("mcp__"):
            return {}  # the matcher routes only mcp__ names; defence anyway
        parts = name.split("__")
        if len(parts) != 3 or (parts[1], parts[2]) not in granted:
            return _deny(
                f"MCP tool {name} is not granted to this seat's toolkit — "
                "admitted servers expose their tools one by one; an "
                "unlisted tool is never callable"
            )
        return {}

    return HookMatcher(matcher="mcp__.*", hooks=[_hook])


def _apply_toolkit(options: ClaudeAgentOptions, toolkit: dict) -> None:
    """Wire a resolved, staged toolkit into the options (F-22).

    Skills ride ONE local plugin — the staged wrapper
    ``factory-toolkit`` — so the CLI discovers them through
    ``--plugin-dir`` while ``setting_sources`` stays ``[]``: nothing is
    loaded from the owner's personal setup or from the clone (AC-7).
    Plugin items keep their own manifests (their own ``--plugin-dir``).
    MCP servers spawn from the staged descriptor's command/args/env
    with the ``{staged}`` placeholder substituted by the staged root —
    they run the vendored, hash-verified copy (PR #77 finding 3), as
    children of this sandboxed runner, so srt confines them too (AC-5).
    Each tool is granted in ``allowed_tools`` one by one, and
    :func:`mcp_guard_hook` enforces exactly that set — under
    ``bypassPermissions`` the grant list alone pre-approves and
    restricts nothing (finding 2). ``strict_mcp_config`` shuts the
    CLI's config-file MCP discovery off whenever a toolkit server is
    attached (defence in depth under ``setting_sources=[]``).
    """
    staged = Path(toolkit["staged"])
    plugin_dirs: list[dict] = []
    skills: list[str] = []
    mcp_servers: dict[str, dict] = {}
    granted: set[tuple[str, str]] = set()
    allowed = list(options.allowed_tools)

    def staged_path(token: str) -> str:
        return token.replace(STAGED_PLACEHOLDER, str(staged))

    for item in toolkit.get("items") or []:
        if item["kind"] == "skill":
            skills.append(f"{TOOLKIT_PLUGIN_NAME}:{item['name']}")
        elif item["kind"] == "plugin":
            plugin_dirs.append({"type": "local", "path": str(staged / item["dir"])})
        elif item["kind"] == "mcp_server":
            descriptor = item["mcp"]
            mcp_servers[item["name"]] = {
                "type": "stdio",
                "command": staged_path(str(descriptor["command"])),
                "args": [staged_path(str(arg))
                         for arg in descriptor.get("args") or []],
                # Review round 3: the CLI merges this env over its own
                # for the child (config wins — the SDK passes this dict
                # verbatim via --mcp-config), so ENV_SCRUB's inert
                # neutralizers beat any control variable that rode the
                # session env, and the descriptor's admitted (inert)
                # keys ride on top. Exactly these names cross: scrub +
                # admitted, nothing else.
                "env": {**ENV_SCRUB, **dict(descriptor.get("env") or {})},
            }
            for tool in descriptor.get("tools") or []:
                allowed.append(f"mcp__{item['name']}__{tool['name']}")
                granted.add((item["name"], tool["name"]))
    if skills:
        # The wrapper plugin (skills' on-disk home) rides first; plugin
        # items keep their own manifests.
        plugin_dirs.insert(0, {"type": "local", "path": str(staged)})
    options.plugins = plugin_dirs
    if skills:
        options.skills = skills
    if mcp_servers:
        options.mcp_servers = {**(options.mcp_servers or {}), **mcp_servers}
        options.strict_mcp_config = True
    options.allowed_tools = allowed
    # Finding 2: the granted pairs are the enforced set, not a wish.
    if VENT_TOOL_NAME in (options.mcp_servers or {}):
        granted.add((VENT_TOOL_NAME, VENT_TOOL_NAME))
    options.hooks["PreToolUse"].append(mcp_guard_hook(frozenset(granted)))


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
    vent_collector: VentCollector | None = None,
    toolkit: dict | None = None,
    bypass_denials: BypassDenials | None = None,
) -> ClaudeAgentOptions:
    """Build the per-seat ClaudeAgentOptions (WO F-06 options list).

    ``setting_sources=[]`` — no user/project/local Claude settings, so a
    session can't pick up personal config — and ``settings`` is exactly
    :data:`ATTRIBUTION_OFF` (F-42 AC-3): the pinned CLI's own attribution
    default, switched off through its real settings key. ``env`` is
    exactly the three session names (see :func:`session_env`). Hooks:
    protected-path + bash-guard + sandbox-bypass PreToolUse callbacks,
    in-process via HookMatcher — the bypass deny on EVERY seat, so the
    fan-out reviewers and the verifier carry it too. ``system_prompt`` is
    the assembled seat prompt (F-09's ``factory.prompts``); it overrides
    the seat's static default, which stays empty because the prompt
    *files* are unreadable inside the sandbox — the orchestrator
    assembles them outside and passes the text.

    ``vent_collector`` (F-27): attach the in-process ``vent`` tool closed
    over this run's collector. Both seats list the tool in
    ``allowed_tools`` (a grant, no restriction); without a collector no
    server is attached and the grant is inert — the option-shape tests
    build this way.

    ``toolkit`` (F-22): the resolved, staged toolkit spec (``{"staged":
    <path>, "items": [...]}`` — see
    :func:`factory.toolkit.resolve_toolkit`/:func:`stage_toolkit`).
    Skills load from the staged read-only copy as one local plugin, MCP
    servers attach with their tools granted one by one; without it a
    session gets exactly the pre-F-22 shape (no skills, no servers).

    ``bypass_denials`` (F-42 AC-5): the shared denial state the hook
    counts into and the F-30 tap reads; ``run_session`` passes the one it
    stamps onto the record. ``None`` still denies — the hook makes its
    own state — but only the count that rides a record reaches the
    attempt accounting.
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
        settings=json.dumps(ATTRIBUTION_OFF),
        allowed_tools=list(seat_config.allowed_tools),
        disallowed_tools=list(seat_config.disallowed_tools),
        permission_mode=seat_config.permission_mode,
        hooks={
            "PreToolUse": [
                protected_path_hook(protected),
                bash_guard_hook(protected),
                sandbox_bypass_hook(bypass_denials),
            ]
        },
    )
    if vent_collector is not None:
        options.mcp_servers = {VENT_TOOL_NAME: vent_server(vent_collector)}
    if toolkit is not None:
        _apply_toolkit(options, toolkit)
    if cli_path is not None:
        options.cli_path = str(Path(cli_path).expanduser().resolve())
    if model is not None:
        options.model = model
    return options


@dataclass
class SessionRecord:
    """What a session recorded, distilled from the ResultMessage (WO F-06
    "Record" list). F-10 writes this into ``attempts[]``.

    ``vents`` (F-27) is the one field that never reaches ``attempts[]``:
    the vent tool's records, gathered in the sandboxed runner and carried
    out on this record's stdout channel; the orchestrator flushes them
    into the task's progress log. Empty when a session never vented —
    including every record built before F-27 (``from_dict`` defaults it).

    ``skills`` (F-22 AC-8) records the toolkit this session actually
    loaded, as ``<name>@<content-hash>`` — the staged, hash-verified
    items the session was configured with (the CLI offers no
    acknowledgement channel for skill *use*; hash-verified-at-load is
    the honest maximum, and it is what F-19 attributes failures
    against). Empty when the project declares no toolkit — as every
    record built before F-22 is (``from_dict`` defaults it).

    ``sandbox_bypass_denials`` (F-42 AC-5) counts the Bash calls this
    session made with ``dangerouslyDisableSandbox`` that the PreToolUse
    hook denied — the per-session number the orchestrator sums onto the
    attempt record and ``factory status`` totals for the week. Zero on
    every record built before F-42 (``from_dict`` defaults it).

    ``permission_denials_dropped`` (F-56 AC-3) counts the denials past
    :data:`DENIAL_LIST_MAX` that did not fit the bounded list — with the
    per-entry truncation marked on each kept entry whose
    ``tool_input`` exceeded :data:`DENIAL_TOOL_INPUT_MAX_CHARS`. Zero on
    every record built before F-56 (``from_dict`` defaults it); the
    bounded form is what the entry writes and every consumer sees.
    """

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
    permission_denials_dropped: int = 0
    vents: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    sandbox_bypass_denials: int = 0

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


def _bound_permission_denials(record: SessionRecord) -> SessionRecord:
    """Bound ``permission_denials`` in place (F-56 AC-3) and return the
    record, so every exit path that carries a record ships the bounded
    form — the entry writes it, the orchestrator reads it, no consumer
    can be fed an unbounded list.

    The CLI reports each permission denial as a free-form entry (the
    SDK types it ``list[Any]``); a dict entry carrying ``tool_input``
    gets that ONE field capped at :data:`DENIAL_TOOL_INPUT_MAX_CHARS`
    (serialised first when it is not a string) and a visible
    ``tool_input_truncated: true`` marker beside it. Entries of any
    other shape pass through untouched — the list cap
    (:data:`DENIAL_LIST_MAX`) is the bound for them, with the overflow
    counted on ``permission_denials_dropped``. A denial list at or under
    both caps rides through byte-identical.
    """
    denials = record.permission_denials
    if not denials:
        record.permission_denials_dropped = 0
        return record
    bounded: list[Any] = []
    dropped = 0
    for entry in denials:
        if len(bounded) >= DENIAL_LIST_MAX:
            dropped += 1
            continue
        if isinstance(entry, dict) and "tool_input" in entry:
            entry = dict(entry)
            value = entry["tool_input"]
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, default=str)
            if len(text) > DENIAL_TOOL_INPUT_MAX_CHARS:
                entry["tool_input"] = (
                    text[:DENIAL_TOOL_INPUT_MAX_CHARS])
                entry["tool_input_truncated"] = True
        # The per-entry bound covers EVERY shape (review round 1, finding
        # 3): a non-dict entry, or a dict whose other fields carry the
        # bulk, is replaced by its capped serialisation with the visible
        # marker — the total claim is then true for any input.
        text = json.dumps(entry, ensure_ascii=False, default=str)
        if len(text) > DENIAL_ENTRY_MAX_CHARS:
            entry = {
                "entry_truncated": True,
                "text": text[:DENIAL_TOOL_INPUT_MAX_CHARS],
            }
        bounded.append(entry)
    record.permission_denials = bounded
    record.permission_denials_dropped = dropped
    return record


# --------------------------------------------------- the F-30 stream tap --

def _content_text(content: Any) -> str:
    """Tool-result content as flat text: a string as-is, a block list by
    its text fields, anything else as JSON."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False, default=str)


def _tap_tool_calls(
    message: Any,
    pending: dict[str, dict[str, Any]],
    seat: str,
    token: str | None,
    sink: Callable[[dict[str, Any]], None],
    denied: "set[str] | None" = None,
) -> None:
    """Turn one SDK stream message into tool_call trace events (F-30).

    A ``tool_use`` block (id, name, input) opens a pending call with its
    arrival time; the matching ``tool_result`` block (tool_use_id) closes
    it as one event — tool name, args snippet, result snippet, ok,
    started/ended. Duck-typed like the vent tap: blocks are identified by
    the fields they carry, not by class, so server tool blocks trace too.
    The sink is never allowed to change the run (AC-5): a raising sink
    costs the event, never the session.

    ``denied`` (F-42 AC-5) is the bypass hook's set of denied
    tool_use_ids: a closed or flushed-open call whose id sits in it is
    stamped ``denied: "sandbox_bypass"``, so each denial lands in the
    trace as an explicit fact instead of being lost in a tool result.
    The id leaves the set as its event is written — one mark per denial.
    """
    stamp = now_iso()
    for block in getattr(message, "content", None) or []:
        use_id = getattr(block, "tool_use_id", None)
        if use_id is not None:  # a tool_result closes its call
            use = pending.pop(str(use_id), None)
            if use is None:
                continue
            is_error = bool(getattr(block, "is_error", False))
            event = {
                "type": "tool_call",
                "seat": seat,
                "tool": use["tool"],
                "args": use["args"],
                "result": snippet(
                    _content_text(getattr(block, "content", None)), token),
                "ok": not is_error,
                "started": use["started"],
                "ended": stamp,
            }
            if denied is not None and str(use_id) in denied:
                event["denied"] = "sandbox_bypass"
                denied.discard(str(use_id))
            with contextlib.suppress(Exception):
                sink(event)
            continue
        name = getattr(block, "name", None)
        block_id = getattr(block, "id", None)
        if name is not None and block_id is not None:  # a tool_use opens one
            pending[str(block_id)] = {
                "tool": str(name),
                "args": snippet(
                    json.dumps(getattr(block, "input", None) or {},
                               ensure_ascii=False, default=str),
                    token,
                ),
                "started": stamp,
            }


def stdout_trace_sink() -> Callable[[dict[str, Any]], None]:
    """The in-sandbox half of the F-30 transport: one prefixed, flushed
    stdout line per event, crossing the srt pipe as it happens (the trace
    path itself is not writable from inside the sandbox, by AC-1's
    design). The final record line is printed un-prefixed after these."""

    def sink(event: dict[str, Any]) -> None:
        print(STREAM_PREFIX + json.dumps(event, ensure_ascii=False, default=str),
              flush=True)

    return sink


def trace_line_forwarder(
    sink: Callable[[dict[str, Any]], None], *, secret: str | None = None
) -> Callable[[str], None]:
    """The orchestrator-side half of the transport: prefixed stdout lines
    back to events. The secret (the seat token) is scrubbed from the raw
    line before parsing; foreign lines (the record, stray output) and
    malformed ones are ignored, never raised — transport can only lose
    events, never crash a run. Only ``tool_call`` crosses: the pipe is
    the sandbox's stdout, where anything inside can print a prefixed
    line, so no other event type — no forged terminal marker, no
    re-stamped phase — is taken on trust."""

    def forward(line: str) -> None:
        if not line.startswith(STREAM_PREFIX):
            return
        if secret:
            line = scrub_literal(line, secret)
        try:
            event = json.loads(line[len(STREAM_PREFIX):])
        except json.JSONDecodeError:
            return
        if isinstance(event, dict) and event.get("type") == "tool_call":
            sink(event)

    return forward


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
    run_id: str | None = None,
    task_id: str | None = None,
    vent_collector: VentCollector | None = None,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    toolkit: dict | None = None,
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
        run_id/task_id: What a vent is recorded against (F-27 AC-2): the
            task's run, stamped into every ``vent(category, text)`` call
            this session's tool collects. Both seats get the tool whether
            or not the ids are known.
        vent_collector: Tests may pass the collector the session should
            use; default is a fresh one stamped with ``run_id``/``task_id``.
        trace_sink: F-30's live tap — a callable receiving one event dict
            per tool call as the stream yields it (before the result
            message; see :func:`_tap_tool_calls`). The sink is called
            defensively: a raising sink loses events, never the session.
            The sandboxed entry point passes :func:`stdout_trace_sink`;
            in-process callers pass a :class:`~factory.trace.TraceWriter`'s
            append (or anything else) directly.
        toolkit: F-22's staged toolkit spec (``{"staged": <path>,
            "items": [...]}``); staged and hash-verified outside, read
            here only. The record's ``skills`` lists what this session
            loaded as ``<name>@<content-hash>`` (AC-8), on every exit
            path.

    Returns:
        The record: ``completed`` / ``failed`` (with reason) / ``aborted``
        (wall clock). Never raises on a session outcome. Whatever the
        outcome, ``record.vents`` carries every vent collected up to that
        point (AC-3: venting never changes the outcome, and an aborted
        session's friction still reaches the log).
    """
    _seat(seat)  # unknown seat → SessionConfigError before anything runs
    if query_fn is None:
        query_fn = sdk_query
    if vent_collector is None:
        # F-43: the default collector carries this session's token as a
        # literal secret, so every vent is seam-scrubbed in-sandbox before
        # it crosses the boundary (the flush scrubs again outside).
        vent_collector = VentCollector(
            run_id=run_id or "", task_id=task_id or "", seat=seat,
            secrets=(token,) if token else (),
        )
    # F-42 AC-5: the denial state shared by the bypass hook (which counts
    # into it), this tap (which marks each denial onto its trace event)
    # and the record below (which carries the count out of the sandbox).
    bypass_denials = BypassDenials()
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
        vent_collector=vent_collector,
        toolkit=toolkit,
        bypass_denials=bypass_denials,
    )

    def _carry(record: SessionRecord) -> SessionRecord:
        # Every exit path ships the vents (F-27 AC-3): outcome untouched,
        # friction never lost — including aborts.
        record.vents = list(vent_collector.vents)
        # ...and the bounded denial list (F-56 AC-3): the bound is applied
        # where the record is BUILT, so whatever carries it — the file the
        # entry writes, the in-process return — is already the bounded
        # form every consumer sees.
        _bound_permission_denials(record)
        # ...and the loaded-toolkit stamp (F-22 AC-8): what the session
        # was configured with rides out whatever the outcome, so F-19
        # can attribute failures to toolkit versions.
        record.skills = [
            f"{item['name']}@{item['content_hash']}"
            for item in (toolkit or {}).get("items") or []
        ]
        # ...and the bypass-denial count (F-42 AC-5): whatever the
        # outcome, the attempts the session made to leave the sandbox
        # reach the attempt record, not only the trace.
        record.sandbox_bypass_denials = bypass_denials.count
        return record

    result_message: ResultMessage | None = None
    stream = None
    # F-30: tool_use blocks seen in the stream, not yet closed by their
    # tool_result — flushed as open-call events on every exit path below.
    pending_calls: dict[str, dict[str, Any]] = {}
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
                    if isinstance(message, AssistantMessage):
                        # The turn a vent lands in (F-27): distinct message
                        # ids counted, and each vent re-stamped with the
                        # turn of its own tool_use block (VentCollector).
                        vent_collector.observe(message)
                    if trace_sink is not None:
                        # F-30: the stream is tapped as it flows — a
                        # tool_call event is on the wire before the
                        # stream yields its ResultMessage (AC-2); a call
                        # the bypass hook denied is marked as it closes
                        # (F-42 AC-5).
                        _tap_tool_calls(message, pending_calls, seat,
                                        token, trace_sink,
                                        denied=bypass_denials.denied)
                    if isinstance(message, ResultMessage):
                        result_message = message
            except _WallClock:
                return _carry(SessionRecord(
                    seat=seat,
                    status="aborted",
                    reason="wall_clock",
                    duration_ms=int((time.monotonic() - started) * 1000),
                ))
            except ClaudeSDKError as exc:
                return _carry(SessionRecord(
                    seat=seat,
                    status="failed",
                    reason=f"{type(exc).__name__}: {exc}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                ))
            finally:
                # Every exit path closes the stream (review round 2): the
                # three explicit calls missed non-SDK raises, which
                # propagate by design — a bug must not also leak the CLI
                # subprocess. The close runs while the window is still up;
                # a refusal from an unwinding generator is suppressed.
                # First, F-30: a stream that ended (or died) without a
                # result for every call still traces the calls it opened —
                # started, no result, ok unknown. An aborted turn's
                # evidence rides out with everything else.
                if trace_sink is not None and pending_calls:
                    for use_id, use in pending_calls.items():
                        event = {
                            "type": "tool_call",
                            "seat": seat,
                            "tool": use["tool"],
                            "args": use["args"],
                            "result": None,
                            "ok": None,
                            "started": use["started"],
                            "ended": None,
                        }
                        if use_id in bypass_denials.denied:
                            # F-42 AC-5: a stream that died before the
                            # result still traces the denial — the attempt
                            # is the fact, the result only decorates it.
                            event["denied"] = "sandbox_bypass"
                            bypass_denials.denied.discard(use_id)
                        with contextlib.suppress(Exception):
                            trace_sink(event)
                await _close_stream(stream)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
    finally:
        _WINDOW_OPEN = False

    duration_ms = int((time.monotonic() - started) * 1000)
    if result_message is None:
        return _carry(SessionRecord(
            seat=seat,
            status="failed",
            reason="no_result_message",
            duration_ms=duration_ms,
        ))
    return _carry(_record_from(result_message, seat, duration_ms))


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


def probe_session_interpreter(
    profile: dict,
    *,
    srt_bin_path: Path | None = None,
    timeout_sec: float = 60.0,
) -> None:
    """One sandboxed proof that the session interpreter can import the SDK.

    The session entry runs ``sys.executable -c`` inside srt with the home
    denied. A venv interpreter whose own files the settings don't
    re-allow starts as its bare base interpreter and dies on
    ``ModuleNotFoundError: No module named 'claude_agent_sdk'`` one full
    session too late — after the attempt is spent (run
    20261029T130912Z-1e4eb9: F-31 lost 1 of 3 attempts to exactly
    that). This probe runs the same import, through the same
    settings-builder a real session uses — the project profile (its
    docs/network allowlists) and a toolchain that includes the bundled
    CLI — BEFORE anything is recorded: a failure raises
    :class:`~factory.runs.RunError` naming the interpreter and the fix —
    a clean environment refusal that spends no attempt, the same class
    the orchestrator already uses for environment failures (network git
    timeouts, merge pathologies), caught and printed by the CLI.

    What the probe deliberately does NOT replicate: the staged runner
    dir (the entry bootstrap's ``allow_read``) — the SDK lives in the
    interpreter's own site-packages, not the runner, and that was the
    failing import; and the seat token/base URL, which reach the child
    as env and select no filesystem surface. The endpoint in the
    settings is :data:`DEFAULT_BASE_URL`, the same default a session
    gets when no override is passed.

    Args:
        profile: The loaded ``project.yaml`` mapping (allowlists).
        srt_bin_path: srt override for tests (still pin-checked).
        timeout_sec: Wall-clock cap for the probe.

    Raises:
        RunError: The sandboxed interpreter cannot import
            ``claude_agent_sdk`` — the message names the interpreter and
            the fix.
        SandboxError: srt is missing or unpinned (from :func:`ensure_srt`
            inside :func:`run_sandboxed`).
    """
    with tempfile.TemporaryDirectory(prefix="factory-sdk-probe-") as tmp:
        root = Path(tmp)
        # Canonical runs layout so build_settings derives the factory
        # root (its holdouts deny must name a real path shape).
        run_dir = root / "runs" / "probe" / "sdk-import"
        run_dir.mkdir(parents=True)
        settings = build_settings(
            profile, run_dir, model_endpoint=DEFAULT_BASE_URL,
            toolchain=default_toolchain(extra=[_bundled_cli()]),
        )
        settings_path = run_dir / "probe-settings.json"
        write_settings(settings, settings_path)
        proc = run_sandboxed(
            [sys.executable, "-c", "import claude_agent_sdk"],
            run_dir,
            settings_path,
            timeout=timeout_sec,
            srt_bin_path=srt_bin_path,
        )
    if proc.returncode != 0:
        raise RunError(
            f"the sandboxed session interpreter cannot import "
            f"claude_agent_sdk ({sys.executable}, exit {proc.returncode}): "
            f"{proc.stderr.strip()[:800] or proc.stdout.strip()[:800]} — "
            "the sandbox denies $HOME, so a venv interpreter needs its own "
            "pyvenv.cfg and site-packages re-allowed "
            "(factory.sandbox._toolchain_allowread); run factory from the "
            "venv with the SDK installed, or reinstall it there"
        )


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
    run_id: str | None = None,
    task_id: str | None = None,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    toolkit: dict | None = None,
    scratch_files: "Mapping[str, str] | None" = None,
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
        trace_sink: F-30's live trace — every tool_call event the runner
            observes inside the sandbox is handed to this callable as its
            prefixed stdout line arrives (see :func:`trace_line_forwarder`);
            None runs the session untraced.
        toolkit: F-22 — the seat's resolved toolkit (from
            :func:`factory.toolkit.resolve_toolkit`). Staged into a
            read-only copy next to the settings (outside the clone, the
            runner-staging pattern) and carried in the spec; None gives
            the pre-F-22 session shape (no skills, no MCP servers).
        scratch_files: ``{bare filename: text}`` staged into the
            session's writable scratch — its ``$TMPDIR`` — before the
            session starts (F-12 round 2: the importer stages the
            narrowed proposal schema there, because the session's clone
            is the target repo and has no schema/ of its own). Guard
            seats only (reviewer/verifier): without the guard the
            writable scratch is the clone's own ``run/.tmp``, and a
            staged file could ride that seat's diff — refused. The
            scratch is cleaned with the run; names must be bare
            filenames (no path), or the session refuses to start.

    Returns:
        The session's record (any status — completed/failed/aborted are
        all recorded outcomes). For the reviewer seat the record is forced
        to ``failed`` with reason :data:`REVIEWER_MUTATION` if HEAD or the
        working tree changed during the session (the workspace is restored
        first, so nothing the reviewer wrote survives).

    Raises:
        SessionConfigError: No token, no bundled CLI, an underivable
            layout, or an unreadable run-dir git state (reviewer seat).
        SessionRecordError: The entry ran but its record file is missing,
            unreadable, or not a valid record (F-56 AC-2) — the message
            names the srt exit code, the signal if any, srt's own kill
            report if it made one, a scrubbed stderr tail, and the saved
            full streams. A subclass of SessionConfigError, so every
            existing except still catches it.
        subprocess.TimeoutExpired: The outer wall clock expired — the
            runner process itself hung past its teardown grace. A record
            already written by then is returned instead (a completed
            session is never lost to what happened around it); only a
            recordless timeout raises, carrying the failure helper's
            facts as a note.
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
    factory_root, settings_path = _derive_settings_path(
        run_dir, factory_root, settings_path
    )

    # Reviewer integrity (owner review rounds 2 and 4): the reviewer — and,
    # since F-38, the verifier, which judges the reviewers' findings —
    # gets a read-only sandbox and a workspace guard (branch, HEAD, tree,
    # refs, ignored files; see _GitState). The scratch links exist
    # BEFORE the baseline so their (untracked) presence can never look
    # like a mutation between the two snapshots.
    review_guard = seat in ("reviewer", "verifier")
    reviewer_writable: tuple[Path, Path] = ()
    baseline: _GitState | None = None
    record: SessionRecord | None = None
    if scratch_files and any(Path(name).name != name for name in scratch_files):
        raise SessionConfigError(
            f"scratch file names must be bare filenames, got "
            f"{sorted(scratch_files)!r} — staging must never write outside "
            "the session's tmp."
        )
    if scratch_files and not review_guard:
        # Round-3 review nit: staging into run/.tmp — the clone itself —
        # for a non-guard seat would put orchestrator files inside that
        # seat's workspace, where they could ride its diff. No caller
        # does this today; refuse so the path never exists.
        raise SessionConfigError(
            "scratch_files needs the reviewer/verifier guard: without it "
            "the writable scratch is the clone's own run/.tmp, and a "
            "staged file could ride that seat's diff."
        )
    # Everything from the scratch symlinks on sits inside try/finally (review
    # round 4): a setup failure must still tear the links down, and the
    # integrity backstop must run on EVERY exit path, timeouts and missing
    # records included, not only after a record parses.
    try:
        if review_guard:
            reviewer_writable = _reviewer_scratch(Path(settings_path), run_dir)
            baseline = _git_state(run_dir)
            _stage_scratch_files(scratch_files, reviewer_writable[1])
        record = _run_prepared(
            seat, prompt, run_dir, profile, settings_path,
            factory_root=factory_root, max_turns=max_turns,
            timeout_min=timeout_min, base_url=base_url, timeout_ms=timeout_ms,
            protected=protected, model=model, system_prompt=system_prompt,
            token=token, cli_path=cli_path, srt_bin_path=srt_bin_path,
            teardown_grace_sec=teardown_grace_sec,
            read_only=review_guard, writable_roots=reviewer_writable,
            run_id=run_id, task_id=task_id, trace_sink=trace_sink,
            toolkit=toolkit,
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


def _stage_scratch_files(
    files: "Mapping[str, str] | None", tmp: Path
) -> None:
    """Stage orchestrator-side files into the session's writable scratch.

    The session's ``$TMPDIR`` (run_sandboxed prefixes it onto its
    command) is the reviewer scratch — outside the clone, so nothing
    staged here can trip the workspace-integrity guard, and cleaned with
    the run. The importer stages its narrowed proposal schema here
    (F-12 round 2): the session's clone is the *target* repo and has no
    schema/ of its own, and the self-check the prompt commands must
    read the file from where the prompt says it is.

    Names are validated bare by the caller; the write is plain text —
    staging runs before the sandbox exists, orchestrator-side.
    """
    if not files:
        return
    tmp.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (tmp / name).write_text(content, encoding="utf-8")


def _derive_settings_path(
    run_dir: Path, factory_root: Path | None, settings_path: Path | None
) -> "tuple[Path | None, Path]":
    """The run's base settings location: ``(factory_root, settings_path)``.

    run_session_sandboxed's derivation in one place, so the fan-out
    derives the same sibling layout (scratch/runner/settings names hang
    off the settings path's name). ``factory_root`` stays None when it was
    None and an explicit settings path was given (callers downstream
    derive their own).
    """
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
            Path(factory_root) / "sandbox"
            / f"{run_dir.parent.name}-{run_dir.name}.json"
        )
    return (
        Path(factory_root) if factory_root is not None else None,
        Path(settings_path),
    )


def run_sessions_parallel(
    specs: "Sequence[tuple[str, str]]",
    run_dir: Path,
    profile: dict,
    *,
    runner: "Callable[..., SessionRecord] | None" = None,
    factory_root: Path | None = None,
    settings_path: Path | None = None,
    **session_kwargs,
) -> list[SessionRecord]:
    """Run the review fan-out: N independent sessions over one diff (F-38).

    Every spec is a ``(seat, prompt)`` pair; every seat must be a guard
    seat (``reviewer`` or ``verifier``). The sessions run **concurrently**
    (one thread each — each real session is a sandboxed subprocess, so
    the parallelism is real wall-clock, not cooperative).

    With the real :func:`run_session_sandboxed` the fan-out wraps every
    session in ONE integrity guard instead of per-session ones: the git
    baseline is taken once, BEFORE any session starts, and the workspace
    is verified once after all of them finish — a mutation by ANY session
    fails the whole fan-out (no verdict from a review that touched its own
    workspace is trusted; attribution inside the fan-out is impossible by
    design, and the conservative rule is to trust none of them). Each
    session gets its OWN srt settings, staged runner and writable scratch
    (``<base>-r1.json`` / ``<base>-r1-scratch/``, ``<base>-r2.json`` / …,
    the seat's ordinal — review finding 1: no shared HOME, TMPDIR,
    CLAUDE_CODE_TMPDIR or cwd slug, so no session can read or rewrite
    another's state, and nothing inside the readable clone is writable),
    and the fan-out removes them all on exit.

    With any other runner (the orchestrator's test stubs), the seats are
    called with the same signature through the same thread pool — the
    concurrency is the behaviour under test, and there is no shared
    sandbox state to guard.

    Args:
        specs: ``(seat, prompt)`` pairs, fan-out order (spec 0 is reviewer
            1 for id/reporting purposes).
        run_dir: The run dir (the clean gate clone) every session reads.
        profile: The project profile.
        runner: The seat runner; default :func:`run_session_sandboxed`.
        factory_root / settings_path: The base srt settings location (the
            same derivation as run_session_sandboxed when omitted).
        **session_kwargs: Everything each session takes (max_turns,
            timeout_min, protected, system_prompt, …) — handed to the
            runner unchanged.

    Returns:
        One :class:`SessionRecord` per spec, in spec order.

    Raises:
        SessionConfigError: A seat that is not a guard seat, no token, or
            any per-session configuration fault (re-raised from its thread
            after the guard has torn down).
    """
    seats = [seat for seat, _ in specs]
    for seat in seats:
        if seat not in ("reviewer", "verifier"):
            raise SessionConfigError(
                f"run_sessions_parallel is the review fan-out: seat {seat!r} "
                "is not a guard seat (reviewer/verifier)"
            )
    if not specs:
        return []
    if runner is None or runner is run_session_sandboxed:
        return _fanout_sandboxed(
            specs, run_dir, profile,
            factory_root=factory_root, settings_path=settings_path,
            **session_kwargs,
        )
    # A test (or alternative) runner: same signature, same concurrency,
    # none of the shared sandbox state. factory_root rides through so a
    # stub sees the kwargs a real call would have.
    if factory_root is not None:
        session_kwargs.setdefault("factory_root", factory_root)
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [
            pool.submit(runner, seat, prompt, run_dir, profile, **session_kwargs)
            for seat, prompt in specs
        ]
        return [future.result() for future in futures]


def _fanout_sandboxed(
    specs: "Sequence[tuple[str, str]]",
    run_dir: Path,
    profile: dict,
    *,
    factory_root: Path | None = None,
    settings_path: Path | None = None,
    **session_kwargs,
) -> list[SessionRecord]:
    """The real-runner fan-out: one shared guard, per-session settings and
    per-session scratch. Each session goes through :func:`_run_prepared`
    — the SAME per-session prepare/run/parse step :func:`run_session_sandboxed`
    uses — so a per-session option threads through ``session_kwargs`` into
    one place: F-22's ``toolkit=`` rides here unchanged, staged per session
    into its own ``<settings>-toolkit`` dir (each reviewer and the verifier
    get their own copy — hash-verified by ``stage_toolkit``, the MCP guard
    hook granted per the resolved items, ``allow_read`` covering exactly
    that session's staged dir). Only the guard (baseline + workspace check
    spanning every session) and the scratch naming are the fan-out's own.
    Per-session artifacts (settings, staged runner, staged toolkit,
    scratch) are removed on every exit path; ``cleanup_run``'s sweep
    remains the crash backstop for all of them."""
    token = session_kwargs.get("token")
    if token is None:
        token = seat_token()
        session_kwargs["token"] = token
    if not token:
        raise SessionConfigError(
            "no Z.ai token available: create the keychain item with "
            f"`security add-generic-password -s {KEYCHAIN_SERVICE} -w <key>` "
            "(or run where ANTHROPIC_AUTH_TOKEN is set). The factory refuses "
            "to start a session that cannot authenticate."
        )

    run_dir = Path(run_dir)
    factory_root, base = _derive_settings_path(run_dir, factory_root, settings_path)
    base.parent.mkdir(parents=True, exist_ok=True)
    # Per-session settings AND scratch, named by spec order (spec 0 is
    # reviewer 1 whatever the scheduling does): concurrent sessions share
    # no spec file, no staged runner and — review finding 1 — no writable
    # scratch. Each session's HOME/TMPDIR/CLAUDE_CODE_TMPDIR point at its
    # own scratch (see _run_prepared), and its settings' allowWrite lists
    # exactly that scratch: nothing inside the readable clone is writable,
    # so no session can leave a note where another reads.
    counts: dict[str, int] = {}
    per_session: list[Path] = []
    per_scratch: list[tuple[Path, Path]] = []
    for seat, _ in specs:
        counts[seat] = counts.get(seat, 0) + 1
        suffix = ("r" if seat == "reviewer" else "v") + str(counts[seat])
        settings = base.with_name(f"{base.name}-{suffix}.json")
        per_session.append(settings)
        per_scratch.append(
            _scratch_dirs(settings.with_name(settings.name + "-scratch"))
        )

    # ONE baseline before any session starts: the workspace integrity
    # guard spans the whole fan-out (a mutation by ANY session fails every
    # record — see the docstring for why none of them is trusted).
    baseline = _git_state(run_dir)
    records: list[SessionRecord | None] = [None] * len(specs)
    try:
        def run_one(index: int, seat: str, prompt: str) -> SessionRecord:
            return _run_prepared(
                seat, prompt, run_dir, profile, per_session[index],
                factory_root=factory_root,
                read_only=True, scratch=per_scratch[index],
                **session_kwargs,
            )

        with ThreadPoolExecutor(max_workers=len(specs)) as pool:
            futures = [pool.submit(run_one, i, seat, prompt)
                       for i, (seat, prompt) in enumerate(specs)]
            for i, future in enumerate(futures):
                records[i] = future.result()
    finally:
        try:
            # The integrity backstop spans the WHOLE fan-out: whatever any
            # session did, the workspace must be exactly what it was before
            # the fan-out. A mutation fails every record and is undone, so
            # nothing a reviewer wrote can ride the branch F-10 pushes.
            if baseline is not None and _git_state(run_dir) != baseline:
                _restore_workspace(run_dir, baseline)
                for record in records:
                    if record is not None:
                        record.status = "failed"
                        record.reason = REVIEWER_MUTATION
        finally:
            for settings, (home, _) in zip(per_session, per_scratch):
                settings.unlink(missing_ok=True)
                shutil.rmtree(settings.with_name(settings.name + "-runner"),
                              ignore_errors=True)
                shutil.rmtree(settings.with_name(settings.name + "-toolkit"),
                              ignore_errors=True)  # F-22 sibling
                shutil.rmtree(settings.with_name(settings.name + "-record"),
                              ignore_errors=True)  # F-56 sibling
                shutil.rmtree(home.parent, ignore_errors=True)
    return [record for record in records if record is not None]


def _run_prepared(
    seat: str,
    prompt: str,
    run_dir: Path,
    profile: dict,
    settings_path: Path,
    *,
    factory_root: Path | None = None,
    max_turns: int,
    timeout_min: float,
    base_url: str = DEFAULT_BASE_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    protected: "Sequence[str]" = (),
    model: str | None = None,
    system_prompt: str | None = None,
    token: str = "",
    cli_path: Path | None = None,
    srt_bin_path: Path | None = None,
    teardown_grace_sec: float = 120.0,
    read_only: bool = False,
    writable_roots: "Sequence[Path]" = (),
    scratch: "tuple[Path, Path] | None" = None,
    run_id: str | None = None,
    task_id: str | None = None,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    toolkit: dict | None = None,
) -> SessionRecord:
    """Stage the runner, write settings, run the sandboxed entry and parse
    its record — the ONE per-session prepare/run/parse step every sandboxed
    seat goes through: :func:`run_session_sandboxed` wraps it in the
    single-session reviewer guard, and the F-38 fan-out calls it once per
    session under its spanning guard. This is the seam a per-session
    option threads through exactly once: F-22's ``toolkit=`` (not built
    here) grows this signature, its staging lands beside the runner
    staging below, and both callers pick it up from their ``**kwargs``
    unchanged.

    ``scratch`` (F-38 review finding 1): the session's own ``(home, tmp)``
    dirs — HOME/TMPDIR/CLAUDE_CODE_TMPDIR point there and the settings'
    writable list is exactly it. None is the single-session shape (the
    run dir's .home/.tmp plus ``writable_roots``).
    """
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
    # F-56: the record file's dir — fresh for THIS session (a fan-out
    # retry reuses the per-session settings, so a stale record from the
    # first attempt must never be read as the retry's), outside the
    # clone, and the one dir beside the scratch the settings carve out
    # for writing on every seat shape (build_settings' record_write).
    record_path = record_path_for(settings_path)
    shutil.rmtree(record_path.parent, ignore_errors=True)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    # F-22: the toolkit stages the same way — a read-only copy OUTSIDE
    # the clone (the runner-staging pattern), next to the settings. The
    # sandbox re-allows reading exactly this dir; nothing under it is
    # writable, so the session can consume its toolkit but never edit
    # what it was given.
    toolkit_spec: dict | None = None
    allow_read: list[Path] = [runner_dir]
    if toolkit is not None:
        staged_toolkit = stage_toolkit(
            toolkit, settings_path.with_name(settings_path.name + "-toolkit")
        )
        toolkit_spec = {"staged": str(staged_toolkit), "items": toolkit["items"]}
        allow_read.append(staged_toolkit)
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
                # F-27: what a vent is recorded against — the in-sandbox
                # collector stamps both on every vent(category, text) call.
                "run_id": run_id,
                "task_id": task_id,
                # F-22: the staged toolkit — the in-sandbox runner wires
                # it into the session's options (see _apply_toolkit).
                "toolkit": toolkit_spec,
                # F-56 AC-1: where the entry writes its record — named by
                # the orchestrator before the session started, wiped fresh
                # above, read back once below. stdout carries only
                # factory-trace lines from here on.
                "record_path": str(record_path),
            }
        )
    )

    settings = build_settings(
        profile,
        run_dir,
        model_endpoint=base_url,
        factory_root=factory_root,
        toolchain=default_toolchain(extra=[cli]),
        allow_read=allow_read,
        read_only=read_only,
        writable_roots=writable_roots,
        scratch_write=scratch,
        record_write=record_path.parent,
    )
    write_settings(settings, settings_path)

    try:
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
            scratch=scratch,
            # F-30: tool_call events stream out as prefixed lines while the
            # session runs. Since F-56 that is ALL stdout carries — the
            # record travels by the file named in the spec, and a stray
            # record-looking stdout line is just a foreign line the
            # forwarder ignores (transport can only lose events, never
            # crash a run; the file is the one record).
            on_stdout_line=(
                trace_line_forwarder(trace_sink, secret=token)
                if trace_sink is not None else None
            ),
        )
    except subprocess.TimeoutExpired as exc:
        _report_escapee(seat, exc, True, trace_sink, run_dir, factory_root,
                        run_id, task_id, token)
        # The outer wall clock fired. The record file is still read
        # first: a session that finished and delivered its record before
        # the runner hung (teardown, a straggler in the drain) is a
        # session the factory keeps — the goal is never to lose a
        # completed session, whatever happened to the process around it.
        saved_record = _read_record_quietly(record_path)
        if saved_record is not None:
            # Review round 1 (the classify item): the record is USED but
            # never silently — one event names the fact, so a record that
            # only exists because the runner hung (and the file was
            # planted rather than written) is observable, not buried in
            # the return value. Same tiers as _report_escapee: the sink,
            # else the run's own trace file, else stderr.
            _report_timeout_record(
                seat, saved_record, trace_sink, run_dir, factory_root,
                run_id, task_id)
            return saved_record
        tail = scrub_session_text(exc.stderr or "", (token,))[:RECORD_STDERR_TAIL_CHARS]
        saved = _save_session_stdout(
            exc.output or "", run_dir, factory_root,
            secrets=(token,), stderr=exc.stderr or "",
        )
        exc.add_note(_record_failure_text(
            "the sandboxed session entry was killed at the orchestrator's "
            "wall clock before its record was written",
            exit_code=None, stderr_tail=tail,
            kill_fact=_srt_kill_fact(tail), saved=saved,
        ))
        raise
    _report_escapee(seat, proc, False, trace_sink, run_dir, factory_root,
                    run_id, task_id, token)

    return _read_record(record_path, proc, run_dir, factory_root, token)


def _open_record_once(path: Path) -> "tuple[str | None, str | None]":
    """Open the record path ONCE and read it to EOF: ``(text, None)`` or
    ``(None, why)`` (F-56 AC-1).

    One open, no re-open: there is no stat-then-read window a session
    could race. ``O_NOFOLLOW`` refuses a symlink at the path;
    ``O_NONBLOCK`` plus a regular-file :func:`os.fstat` check refuses a
    FIFO (a plain open would block until a reader-planting session chose
    to speak) or a directory without reading a byte of either; the single
    read is the one copy the orchestrator judges.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "is missing"
    except OSError as exc:
        # ELOOP (symlink), EACCES, … — name the kernel's own words.
        return None, f"cannot be opened ({exc.strerror or exc})"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "is not a regular file (symlink, FIFO or directory)"
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # fdopen owns the descriptor from here
            # Bounded (review round 1, finding 2): read at most the cap
            # plus one byte — a planted huge file is refused by the size
            # check below, never read whole into orchestrator memory.
            raw = handle.read(RECORD_MAX_BYTES + 1)
        if len(raw) > RECORD_MAX_BYTES:
            return None, (
                f"exceeds the record size bound ({RECORD_MAX_BYTES} bytes)")
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "is not valid UTF-8"


def _read_record(
    record_path: Path,
    proc: subprocess.CompletedProcess,
    run_dir: Path,
    factory_root: Path | None,
    token: str,
) -> SessionRecord:
    """Read and parse the session's record file; every failure is the ONE
    record-failure error (F-56 AC-2), naming the srt exit code, the
    signal if the process died by one, srt's own kill report if it made
    one, a seam-scrubbed stderr tail, and where the full streams were
    saved."""
    text, why = _open_record_once(record_path)
    if why is None and not text.strip():
        why = "is empty"
    if why is None:
        try:
            return SessionRecord.from_dict(json.loads(text))
        except (json.JSONDecodeError, TypeError, ValueError,
                RecursionError) as exc:
            # RecursionError: a planted record can nest arbitrarily deep
            # (the F-49 parse-boundary rule — the record file is
            # session-writable, so its parse must refuse, never crash).
            why = f"does not hold a valid record ({exc})"
    # The raw streams pass the seam HERE (the tail for the message, the
    # pair for the saved files) — nothing session-derived reaches the
    # error unscrubbed, and the srt-kill signatures are read from the
    # already-scrubbed tail (srt's own words are plain ASCII; the seam
    # keeps ordinary text and only a literal secret or a control byte
    # could alter them, neither of which srt emits).
    tail = scrub_session_text(proc.stderr or "", (token,))[:RECORD_STDERR_TAIL_CHARS]
    saved = _save_session_stdout(
        proc.stdout, run_dir, factory_root,
        secrets=(token,), stderr=proc.stderr or "",
    )
    raise SessionRecordError(
        _record_failure_text(
            f"the sandboxed session entry's record file ({record_path}) {why}",
            exit_code=proc.returncode, stderr_tail=tail,
            kill_fact=_srt_kill_fact(tail), saved=saved,
        ),
        exit_code=proc.returncode,
    )


def _read_record_quietly(record_path: Path) -> SessionRecord | None:
    """The timeout path's read: a record that opens and parses is the
    session's delivered result (used, never discarded); anything else
    returns None — the timeout itself stays the failure being raised."""
    text, why = _open_record_once(record_path)
    if why is not None or not text.strip():
        return None
    try:
        return SessionRecord.from_dict(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None


def _record_failure_text(
    what: str, *, exit_code: int | None, stderr_tail: str,
    kill_fact: str | None, saved: str,
) -> str:
    """The ONE record-failure message (F-56 AC-2), used by every branch:
    the read/parse failure, and (as the note on the timeout) the
    wall-clock kill. Names the srt exit code, the signal if the process
    died by one, srt's own kill report when it made one, the scrubbed
    stderr tail, and where the full streams are saved — everything an
    owner needs to diagnose or salvage the session."""
    parts = [what]
    if exit_code is None:
        parts.append("srt exit: unknown (the orchestrator killed the "
                     "process group at its wall clock)")
    elif exit_code < 0:
        try:
            name = signal.Signals(-exit_code).name
        except ValueError:
            name = f"signal {-exit_code}"
        parts.append(f"srt exit {exit_code} (died by signal {name})")
    else:
        parts.append(f"srt exit {exit_code}")
    if kill_fact:
        parts.append(kill_fact)
    if stderr_tail.strip():
        parts.append(f"stderr tail: {stderr_tail}")
    if saved.startswith("<"):
        parts.append(f"full streams not saved ({saved})")
    else:
        # The pair's stderr spelling (Path.suffix would only see ".txt"):
        # swap the stdout file's own suffix in the NAME — and name the
        # stderr file only when it exists (review round 1, finding 1): a
        # failed stderr write must leave the message pointing only at
        # files that are there.
        stderr_name = saved[: -len(".stdout.txt")] + ".stderr.txt"
        if os.path.exists(stderr_name):
            parts.append(
                f"full stdout and stderr saved to {saved} and {stderr_name}")
        else:
            parts.append(
                f"full stdout saved to {saved} "
                "(the stderr copy was not written)")
    return "; ".join(parts)


def _srt_kill_fact(stderr_tail: str) -> str | None:
    """The distilled fact when srt's own stderr says IT killed the
    command (F-56 AC-4), or None. The signatures are quoted from the
    pinned cli.js in :data:`_SRT_KILL_SIGNATURES`; the fact lands in the
    message beside the raw tail so the owner reads the mechanism, not
    only srt's words."""
    if "failed before delivering an update" in stderr_tail:
        return ("srt reported killing the command over a dead control "
                "channel")
    if "Process killed by signal:" in stderr_tail:
        return "srt reported the command was killed by a signal"
    return None


def _write_record_file(record: SessionRecord, path: Path) -> None:
    """The entry's half of the F-56 transport: write the record
    atomically through the factory's ONE atomic-write helper
    (:func:`factory.state._atomic_write_json` — same-dir temp, fsync,
    rename, dir fsync; F-45 AC-6's rule that no module renames on its
    own holds here too). A torn write cannot be observed: the reader
    sees either no record or the whole record, never a prefix — the
    failure mode the stdout line had.

    (Private import, like importer.py's — F-55's promotion list grows by
    one consumer; the helper is protected and unchanged.)
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, record.to_dict())


def _report_escapee(
    seat: str, outcome: Any, timed_out: bool,
    trace_sink: "Callable[[dict[str, Any]], None] | None",
    run_dir: Path, factory_root: Path | None,
    run_id: str | None, task_id: str | None, token: str,
) -> None:
    """F-47 (I-9): a session whose drain force-closed pipes left a process
    alive outside the killed group — holding the seat token. One
    :func:`~factory.sandbox.escapee_event` (``session:<seat>``) goes to
    the run's trace sink; a caller with no sink but a run id (the
    importer) gets the run's own trace file, the store ``factory status``
    counts; with neither, stderr. Never raises: the alarm must not
    replace the outcome it rides on."""
    streams = getattr(outcome, "force_closed_pipes", ()) or ()
    if not streams:
        return
    event = escapee_event(f"session:{seat}", streams, timed_out=timed_out)
    try:
        if trace_sink is not None:
            trace_sink(event)
            return
        root = Path(factory_root) if factory_root is not None else None
        if root is not None and run_id:
            TraceWriter(root, run_dir.parent.name, run_id, task_id or "-",
                        extra_secrets=(token,) if token else ()).append(event)
            return
    except Exception:  # noqa: BLE001 — the alarm never steers the run
        pass
    print(f"factory: sandbox escapee — {json.dumps(event)} (run dir "
          f"{run_dir}; no trace to record it in)", file=sys.stderr)


def _report_timeout_record(
    seat: str, record: SessionRecord,
    trace_sink: "Callable[[dict[str, Any]], None] | None",
    run_dir: Path, factory_root: Path | None,
    run_id: str | None, task_id: str | None,
) -> None:
    """Review round 1 (the classify item): the timeout path ACCEPTS
    whatever record the file holds — if the entry never wrote one, the
    file can only be one the session planted. The use is correct (a
    delivered record is the session's result; the gates still judge the
    diff), but it must be VISIBLE: one ``record_used_after_timeout``
    event through the same tiers as :func:`_report_escapee` (the trace
    sink, else the run's own trace file when the ids are known, else
    stderr). Never raises — the record is already the outcome."""
    event = {
        "type": "record_used_after_timeout",
        "seat": seat,
        "status": record.status,
    }
    try:
        if trace_sink is not None:
            trace_sink(event)
            return
        root = Path(factory_root) if factory_root is not None else None
        if root is not None and run_id:
            TraceWriter(root, run_dir.parent.name, run_id, task_id or "-",
                        ).append(event)
            return
    except Exception:  # noqa: BLE001 — the alarm never steers the run
        pass
    print(f"factory: record used after the wall-clock kill — "
          f"{json.dumps(event)} (run dir {run_dir}; no trace to record it "
          f"in)", file=sys.stderr)


def _save_session_stdout(
    stdout: str, run_dir: Path, factory_root: Path | None,
    *, secrets: "Sequence[str]" = (), stderr: str = "",
) -> str:
    """Keep the full raw stdout — and, since F-56, the stderr beside it —
    of a session whose record did not arrive whole.

    The once-observed "Unterminated string" record (2026-11-01, 56 turns
    lost) left nothing to diagnose: the run dir is deleted on every exit
    path, and the truncated tail in the error message said nothing about
    what the whole stream looked like. F-56: a missing record needs the
    stderr just as much (the srt exit path's own words — including its
    kill reports), so both streams are saved as a pair,
    ``unparsable-record-<id>.stdout.txt`` / ``.stderr.txt``, one id, one
    dir. Each copy passes the F-43 egress seam
    (:func:`factory.egress.scrub_session_text` with the session's
    ``secrets`` — the M-8 gap was the literal token, which pattern
    redaction alone never caught — then the control-sequence strip), is
    written with mode 0600, create-only (O_EXCL), under
    ``<factory-root>/sessions/`` — beside the runs, outside the clone that
    is about to disappear.

    Returns the saved stdout path as text, or ``<not saved: …>`` — the
    save is diagnostics for a SessionRecordError already on its way out,
    and a failed save must never mask it (the stderr half failing leaves
    the stdout path in hand: half the evidence beats none). A
    :class:`TypeError` from the secrets guard (round 3: a bare ``str`` or
    a non-str element) is a programming error and deliberately wins —
    raised while the caller handles the original failure, the original
    stays reachable as the ``__context__``.
    """
    secrets = check_secrets(secrets)
    try:
        root = Path(factory_root) if factory_root is not None else None
        if root is None:
            runs_root = run_dir.parent.parent
            root = runs_root.parent if runs_root.name == "runs" else FACTORY_ROOT_DEFAULT
        directory = root / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id()
        path = directory / f"unparsable-record-{run_id}.stdout.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(scrub_session_text(stdout, secrets))
        # The stderr half is ALWAYS written (review round 1, finding 1):
        # empty is honest — the pair the error names exists as a pair. A
        # failed stderr write is swallowed (half the evidence beats none,
        # the stdout path stands); _record_failure_text names the stderr
        # file only when it exists, so the message never points at a
        # path that is not there.
        try:
            err_path = directory / f"unparsable-record-{run_id}.stderr.txt"
            err_fd = os.open(
                err_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(err_fd, "w", encoding="utf-8") as handle:
                handle.write(scrub_session_text(stderr, secrets))
        except OSError:
            pass
        return str(path)
    except OSError as exc:
        return f"<not saved: {exc}>"


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
        if not spec.get("record_path"):
            raise SessionConfigError(
                "the spec names no record_path — the orchestrator and the "
                "staged runner disagree on the F-56 record transport; "
                "re-stage the runner (the factory package copies itself "
                "per session, so both halves always match)"
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
                run_id=spec.get("run_id"),
                task_id=spec.get("task_id"),
                toolkit=spec.get("toolkit"),
                # F-30: tool_call events ride stdout as prefixed lines;
                # the orchestrator's on_stdout_line callback appends them
                # to the trace (the trace path is not writable in here).
                trace_sink=stdout_trace_sink(),
            )
        )
        # F-56 AC-1: the record travels by FILE, not stdout. Stdout
        # carries only factory-trace lines from here on — the pipe can be
        # cut at any byte without touching the record, and the atomic
        # write (temp in the same carve-out dir, fsync, rename) means the
        # orchestrator reads either nothing or the whole record, never a
        # torn prefix. The 2026-11-05 jobfinderos losses (a record line
        # cut at exactly 81,920 bytes inside permission_denials) were the
        # stdout transport's last word.
        _write_record_file(record, Path(spec["record_path"]))
    except (OSError, json.JSONDecodeError, KeyError, SessionConfigError) as exc:
        print(f"factory.session: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
