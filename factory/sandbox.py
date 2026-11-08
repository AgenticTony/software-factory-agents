"""The session sandbox: srt, pinned, with settings built from the profile.

Isolation is Anthropic's ``srt`` (``@anthropic-ai/sandbox-runtime``) —
Seatbelt on macOS, bubblewrap on Linux — installed at an exact version in
this repo's ``package.json`` (see BUILD-ORDER F-05: pin the srt version).
The factory refuses to sandbox anything unless ``srt --version`` matches
: data:`SRT_PIN`, so a drifted or broken install stops the factory instead
of quietly weakening the sandbox.

The settings file (DESIGN-LOCAL-FACTORY D3) allows writes only inside the
run dir (minus its ``.git/hooks`` and ``.git/config`` — see GIT_HARDENING
in :mod:`factory.runs`), denies reads of the entire home directory (the
run dir is re-allowed), the factory root itself (F-47: trace, progress,
sessions, settings — whatever ``--factory-root`` is), the holdouts, and
every other run, and allows network only to the model endpoint plus the
project profile's ``docs_allowlist`` and ``network_allowlist``. (On Linux
srt read-denies a directory with a tmpfs over it: a write under a denied
root lands in that tmpfs and never reaches the host — the host is the
invariant, on both platforms.) Credentials are defence-in-depth: the
environment is scrubbed before the child is spawned (allowlist, then the
whole ``GH_*``/``GITHUB_*`` prefix), *and* the settings file denies GitHub
names inside srt as a backstop. srt's deny takes exact names only, so the
backstop lists the well-known token names plus every ``GH_*``/``GITHUB_*``
variable present when the settings are built, plus the caller's
keychain-derived names — a prefixed secret that appears only *after* the
settings were built is caught by the scrub alone.
"""

import ctypes
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Sequence, cast
from urllib.parse import urlsplit

SRT_PIN = "0.0.79"
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Structurally blocked env names: nothing a build session does needs GitHub
# credentials, so they never cross the sandbox boundary in either layer.
_FORBIDDEN_ENV_EXACT = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
)
_FORBIDDEN_ENV_PREFIXES = ("GH_", "GITHUB_")

# Names a session may ever see, split by where they may come from:
#
# * AMBIENT_* — inherited from the orchestrator's environment. Deliberately
#   excludes every seat/secret/git name: the orchestrator's own
#   ANTHROPIC_* keys, or GIT_CONFIG_* set by CI, must never ride into a
#   session by inheritance (a real Anthropic key reaching a Z.ai seat,
#   exec-capable git config arriving unasked).
# * SESSION_* — allowed ONLY from the caller's explicit ``env=``: the model
#   seat (base URL, the one secret the sandbox holds, API timeout) and the
#   git identity (the sandbox can't read ~/.gitconfig).
#   GIT_CONFIG_* is not on any list — env-injected git config is
#   exec-capable; `git -c` on the command line is the controlled route.
#   PYTHONPATH is deliberately NOT on any list either: the session runner
#   bootstraps its imports with an explicit ``sys.path`` insert instead, so
#   no path-injection surface exists at all.
#
# Adding a name to either list is a deliberate, reviewable act.
AMBIENT_ENV_ALLOWLIST = frozenset({"PATH", "LANG", "TZ"})
AMBIENT_ENV_PREFIXES = ("LC_",)
SESSION_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "TZ",
        "TMPDIR",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "API_TIMEOUT_MS",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    }
)
SESSION_ENV_PREFIXES = ("LC_",)

# Seconds to wait for output after the process group is killed: pipe buffer
# drains fast when every writer is dead; a process that escaped the group
# (setsid) and holds the pipes is force-closed after this grace instead of
# hanging the orchestrator.
_DRAIN_GRACE_SEC = 10.0

# Binaries a session may need to exec that can live under $HOME: resolved
# and re-allowed for reading by build_settings (the home-wide denyRead would
# otherwise make the session's own toolchain unexecutable — rc=126).
TOOLCHAIN_BINS = ("node", "npm", "npx", "claude", "git", "curl")

# Well-known GitHub credential names, always on the settings backstop even
# when absent from the environment at build time.
_GITHUB_TOKEN_NAMES = frozenset(
    {"GH_APP_TOKEN", "GH_PAT", "GITHUB_PAT", "GITHUB_APP_PRIVATE_KEY"}
)


class SandboxError(Exception):
    """The sandbox cannot be used: srt missing, wrong version, or unusable."""


def srt_bin() -> Path:
    """Path to the pinned srt binary: always ``node_modules/.bin/srt`` in
    this repo. There is deliberately **no environment override** — an env
    var the orchestrator happens to carry must never be able to hand the
    sandbox to a wrapper. Tests inject fakes through the ``srt_bin_path``
    parameters of :func:`ensure_srt` / :func:`run_sandboxed` instead.
    """
    return _REPO_ROOT / "node_modules" / ".bin" / "srt"


def ensure_srt(srt_bin_path: Path | None = None) -> str:
    """Check ``srt --version`` against the pin and return the version string.

    The check is cached per binary state (path + mtime + size): it costs a
    Node boot, and the binary cannot change mid-process without the
    fingerprint changing. Failures are never cached.

    Args:
        srt_bin_path: Binary to check (default: the repo-pinned install, or
            ``FACTORY_SRT_BIN`` under pytest).

    Returns:
        The reported version, which equals :data:`SRT_PIN`.

    Raises:
        SandboxError: srt is missing, won't run, or reports any version other
            than the pin. The message says how to fix the install.
    """
    binary = Path(srt_bin_path) if srt_bin_path else srt_bin()
    if not binary.exists():
        raise SandboxError(
            f"srt not found at {binary}. Install the pinned sandbox runtime "
            f"with `npm ci` (package.json pins @anthropic-ai/sandbox-runtime "
            f"at {SRT_PIN}), or point FACTORY_SRT_BIN at the right binary."
        )
    stat = binary.stat()
    return _verified_srt(str(binary), (stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=None)
def _verified_srt(binary: str, fingerprint: tuple[int, int]) -> str:
    """Run the actual version check once per binary state. ``fingerprint``
    (mtime, size) busts the cache if the binary is replaced on disk."""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"srt at {binary} could not be executed: {exc}") from exc
    if proc.returncode != 0:
        raise SandboxError(
            f"srt --version exited {proc.returncode} at {binary}: {proc.stderr.strip()}"
        )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    version = lines[-1].split()[-1] if lines else ""
    if version != SRT_PIN:
        raise SandboxError(
            f"srt version mismatch: expected {SRT_PIN} (pinned in package.json), "
            f"got {version!r} from {binary}. Run `npm ci` and retry; the factory "
            "refuses to run sessions against an unpinned sandbox."
        )
    return version


def scrub_env(env: dict[str, str], forbidden: tuple[str, ...] = ()) -> dict[str, str]:
    """Drop GitHub credentials and caller-named secrets from ``env``.

    Removes the structural GitHub token names, every ``GH_*``/``GITHUB_*``
    variable, and the names in ``forbidden`` (keychain-derived variables —
    the orchestrator knows which those are; the sandbox doesn't). The Z.ai
    model token is not on any of those lists: it is the one secret the
    sandbox is allowed to hold (DESIGN D3, rule of two).
    """
    forbidden_upper = {name.upper() for name in forbidden}
    scrubbed: dict[str, str] = {}
    for key, value in env.items():
        upper = key.upper()
        if upper in _FORBIDDEN_ENV_EXACT or upper in forbidden_upper:
            continue
        if any(upper.startswith(prefix) for prefix in _FORBIDDEN_ENV_PREFIXES):
            continue
        scrubbed[key] = value
    return scrubbed


def sandbox_env(
    run_dir: Path,
    *,
    extra: dict[str, str] | None = None,
    forbidden: tuple[str, ...] = (),
    venv: Path | None = None,
    scratch: "tuple[Path, Path] | None" = None,
) -> dict[str, str]:
    """Build the child env: ambient base + allowlisted extras, HOME inside
    the run.

    ``scratch`` (F-38 review finding 1): when given, a ``(home, tmp)``
    pair of writable dirs the child's HOME / TMPDIR / CLAUDE_CODE_TMPDIR /
    GIT_TEMPLATE_DIR point at INSTEAD of the run dir's ``.home``/``.tmp``
    — the review fan-out gives each concurrent session its own scratch,
    so no session can read or rewrite another's state. None is the
    single-session shape every other caller uses, unchanged.

    Three rules, applied on every path:

    * ambient inheritance is limited to :data:`AMBIENT_ENV_ALLOWLIST`
      (PATH/LANG/TZ and ``LC_*``) — the orchestrator's own seat keys and
      git config never enter a session unasked;
    * the caller's ``extra`` is limited to :data:`SESSION_ENV_ALLOWLIST`
      (and ``LC_*``) — ``extra=dict(os.environ)`` cannot smuggle an
      unlisted secret across, because os.environ's secret names are not on
      the session list;
    * the GitHub/forbidden scrub runs on top of both.

    HOME points at ``<run_dir>/.home`` and TMPDIR at ``<run_dir>/.tmp`` —
    writable scratch (the real home is denyRead; writes are allowed nowhere
    outside the run). Both are git-excluded by ``create_run``.

    CLAUDE_CODE_TMPDIR points at the same scratch. The pinned Claude CLI
    builds its per-uid scratch root — ``<root>/claude-<uid>/<cwd-slug>``,
    home of the Bash tool's shell snapshots — from CLAUDE_CODE_TMPDIR or
    a hardcoded ``/tmp`` (the bundled binary's ``sm()``; TMPDIR is never
    consulted for that root). Unset, every sandboxed session's Bash call
    died on ``EPERM mkdir /private/tmp/claude-<uid>/…`` (run
    20261029T145356Z-b1875d: 0/21 Bash calls, Read/Write/Edit fine). The
    redirect keeps the root inside the run — allowWrite never widens to
    /tmp.

    GIT_TEMPLATE_DIR points at an empty dir under that scratch (INC-006):
    ``git init``/``git clone`` copy hook samples from git's installation
    templates into ``.git/hooks/`` of the new repo, and srt blocks writes
    into every ``**/.git/hooks/**`` beneath the child's cwd — so the
    project's own suite, which builds a fixture repo per test inside the
    L0 sandbox, could never init. With empty templates nothing is copied
    into the blocked path and init is a config write (allowed by
    ``allowGitConfig`` in :func:`build_settings`). Hook samples are dead
    weight here anyway: this factory's git runs hardened, hooks off.

    Args:
        run_dir: The run the child is confined to.
        extra: Caller extras, filtered by :data:`SESSION_ENV_ALLOWLIST`.
        forbidden: Secret names to scrub on top of the structural ones.
        venv: A run-local venv (``<run_dir>/.venv``, PEP 405). When given,
            its ``bin/`` is prepended to PATH and VIRTUAL_ENV is set, so
            ``pip``/``python`` resolve into the run's venv — never the
            owner's terminal venv or a host conda env (run
            20261029T145356Z-b1875d: setup resolved the owner's venv,
            tried to uninstall its ``bin/factory``, failed, and every
            remaining gate was skipped). The rest of PATH survives after
            the prefix.
    """
    run_dir = Path(run_dir)
    if scratch is None:
        home = run_dir / ".home"
        home.mkdir(parents=True, exist_ok=True)
        tmp = run_dir / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
    else:
        home, tmp = (Path(scratch[0]), Path(scratch[1]))
        home.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
    git_templates = home / ".git-templates"
    git_templates.mkdir(parents=True, exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in AMBIENT_ENV_ALLOWLIST or key.startswith(AMBIENT_ENV_PREFIXES)
    }
    if "PATH" in env:
        env["PATH"] = _path_outside_home(env["PATH"])
    if extra:
        for key, value in extra.items():
            if key in SESSION_ENV_ALLOWLIST or key.startswith(SESSION_ENV_PREFIXES):
                env[key] = value
    if venv is not None:
        env["PATH"] = f"{Path(venv) / 'bin'}:{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(venv)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["CLAUDE_CODE_TMPDIR"] = str(tmp)
    env["GIT_TEMPLATE_DIR"] = str(git_templates)
    return scrub_env(env, forbidden)


def _path_outside_home(path: str, keep: "Sequence[Path] | None" = None) -> str:
    """The inherited PATH minus every entry under the owner's home, except
    the toolchain install dirs the sandbox re-allows.

    The sandbox denies reads under ``$HOME``, but an interactive PATH
    starts with home dirs (``~/.local/bin``, ``~/.pyenv/shims``, ...). A
    search that probes a denied dir first can fail outright with EPERM
    instead of moving on: npm's lifecycle-script spawn died this way in
    the JobFinderOS readiness run (2026-10-18). Entries under home (by
    either spelling of it) and literal ``~`` entries are dropped; order is
    kept. A caller's explicit ``env={"PATH": ...}`` is left as given:
    it's a deliberate choice, e.g. a symlinked toolchain re-allowed by
    ``build_settings``.

    An entry under home that is itself in ``keep`` (compared resolved) is
    kept. ``keep`` defaults to :func:`home_path_keep` — the reads
    :func:`build_settings` re-allows for :func:`default_toolchain` (every
    caller's toolchain includes it), the same set the F-24 env-guard
    accepts,
    so a node/npm installed by nvm, fnm or volta
    (``~/.nvm/versions/node/vX/bin``) stays findable — the dir is readable,
    so a lookup there cannot EPERM — and the L0 setup's ``npm ci`` does not
    exit 127 (review of #53). A dir that only holds a re-allowed *link*
    (``~/.local/bin/claude``) is not in ``keep`` and is still dropped.
    """
    if keep is None:
        keep_resolved = home_path_keep()
    else:
        keep_resolved = _resolved(keep)

    def kept(entry: str) -> bool:
        try:
            return Path(entry).resolve() in keep_resolved
        except OSError:
            return False

    homes = {str(Path.home())}
    with suppress(OSError):
        homes.add(str(Path.home().resolve()))

    def under_home(entry: str) -> bool:
        return entry.startswith("~") or any(
            entry == home or entry.startswith(home.rstrip("/") + "/") for home in homes
        )

    return ":".join(
        entry for entry in path.split(":")
        if entry and (not under_home(entry) or kept(entry))
    )


def home_path_keep() -> frozenset[Path]:
    """The one rule for which PATH entries under the owner's home survive
    into a sandboxed child: the (resolved) reads :func:`build_settings`
    re-allows for :func:`default_toolchain`. A home entry is kept iff it
    resolves to one of these — an nvm/fnm/volta ``bin/`` that holds the
    session's node, say.

    :func:`_path_outside_home` filters by it, and the F-24 rehearsal's
    env-guard bakes it in (``factory.rehearsal._env_guard_source``), so the
    guard refuses exactly the home entries the sandbox would have dropped
    — never the ones it deliberately keeps.
    """
    return _resolved(
        _toolchain_allowread(Path("~").expanduser(), default_toolchain())
    )


def _resolved(paths: "Sequence[Path] | set[Path]") -> frozenset[Path]:
    """``paths`` resolved, skipping any that cannot be."""
    found = set()
    for entry in paths:
        with suppress(OSError):
            found.add(Path(entry).resolve())
    return frozenset(found)


def default_toolchain(extra: "Sequence[Path] | None" = None) -> list[Path]:
    """The binaries :func:`build_settings` re-allows for reading by default:
    this interpreter, the real interpreters behind ``python3``/``python``,
    and the standard toolchain names found on PATH — plus caller ``extra``
    entries (the session runner adds the resolved Claude CLI, which is not
    necessarily on the orchestrator's non-login PATH).
    """
    toolchain: list[Path] = [Path(sys.executable), *_python_toolchain()]
    toolchain += [
        Path(found) for name in TOOLCHAIN_BINS if (found := shutil.which(name))
    ]
    for path in extra or ():
        if path and Path(path) not in toolchain:
            toolchain.append(Path(path))
    return toolchain


def _allow_write(
    run_dir: Path,
    read_only: bool,
    writable_roots: "Sequence[Path]",
    scratch_write: "Sequence[Path] | None",
    record_write: Path | None = None,
) -> list[str]:
    """The settings' allowWrite list, by session shape:

    * a fan-out review session (``scratch_write``): EXACTLY its own
      scratch dirs — nothing inside the readable clone, so no session can
      leave anything for another (F-38 review finding 1);
    * a single-session reviewer (``read_only``): the run dir's
      ``.home``/``.tmp`` spellings plus the caller's outside-the-run
      scratch dirs;
    * the implementer/gates: the run dir itself.

    ``record_write`` (F-56): the session's own record dir, appended to
    EVERY shape — the record file is the one thing every seat must be
    able to write outside the clone (its atomic temp-and-rename lives
    there), on a path the orchestrator named before the session started.
    """
    if read_only:
        if scratch_write is not None:
            writable = [str(Path(root)) for root in scratch_write]
        else:
            writable = [str(run_dir / ".home"), str(run_dir / ".tmp")] + [
                str(Path(root)) for root in writable_roots
            ]
    else:
        writable = [str(run_dir)]
    if record_write is not None:
        writable.append(str(Path(record_write)))
    return writable


# System dirs every sandboxed command needs to read (exec, libc, the
# dynamic loader, /etc, devices). A factory root at or above one of them
# would deny it wholesale: refused (F-47), never silently carved out.
_SYSTEM_READ_ROOTS = (
    "/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/dev", "/proc",
    "/sys", "/System", "/Library", "/private/etc", "/private/var/db",
)


def _refuse_unsafe_factory_root(factory_root: Path) -> None:
    """Refuse a factory root whose read-deny would take the system with it.

    F-47 denies the (resolved) factory root itself. That is sound for any
    root that holds factory state — ``~/factory``, ``/srv/factory``, a
    root equal to ``$HOME`` or above it (``/Users``: the deny only widens
    over other homes), even one that contains this checkout — because
    srt's package, the toolchain, the run and the caller's reads are
    carved out explicitly (proven with real srt in tests/test_f47.py).
    It is not sound for ``/`` or a root at or above a system read root
    (``/usr``, ``/etc`` — ``/private`` on macOS): libc, the loader or
    ``/etc`` would be hidden and every command would die at exec. Those
    are refused, naming why — never silently carved out.

    Raises:
        SandboxError: ``factory_root`` is one of those shapes.
    """
    root = Path(factory_root)
    if root == Path(root.anchor):
        raise SandboxError(
            f"refusing factory root {root}: it is the filesystem root, and "
            "the sandbox denies reads of the whole factory root (F-47) — "
            "the session could read nothing, not even /usr. Point "
            "--factory-root at a dedicated directory."
        )
    for system in _SYSTEM_READ_ROOTS:
        spellings = {Path(system)}
        with suppress(OSError):
            spellings.add(Path(system).resolve())
        for spelling in spellings:
            if root == spelling or root in spelling.parents:
                raise SandboxError(
                    f"refusing factory root {root}: it contains the system "
                    f"read root {spelling}, and the sandbox denies reads of "
                    "the whole factory root (F-47) — every sandboxed command "
                    "would lose its interpreter or libraries. Point "
                    "--factory-root at a dedicated directory."
                )


def build_settings(
    profile: dict,
    run_dir: Path,
    *,
    model_endpoint: str,
    forbidden_env: tuple[str, ...] = (),
    factory_root: Path | None = None,
    toolchain: "Sequence[Path] | None" = None,
    allow_read: "Sequence[Path]" = (),
    read_only: bool = False,
    writable_roots: "Sequence[Path]" = (),
    scratch_write: "Sequence[Path] | None" = None,
    record_write: Path | None = None,
    local_binding: bool = False,
    deny_read: "Sequence[Path]" = (),
) -> dict:
    """Build the srt settings dict for one run from the project profile.

    Args:
        profile: The loaded ``project.yaml`` (``docs_allowlist`` /
            ``network_allowlist`` / ``sandbox`` keys are read; all
            optional here).
        run_dir: The session's run dir — the only writable, self-readable
            path inside the sandbox.
        model_endpoint: The seat's model endpoint (Z.ai base URL or bare
            host). Its hostname joins the network allowlist.
        forbidden_env: Keychain-derived env names denied in addition to the
            structural GitHub set (same list the orchestrator scrubs).
        factory_root: Explicit factory root. When omitted it is derived from
            the canonical ``<factory_root>/runs/<project>/<run-id>`` layout —
            and that derivation is *asserted*, never guessed silently: a run
            dir with a different nesting raises instead of quietly pointing
            the holdouts deny at a wrong path (which would leave the real
            holdouts readable).
        toolchain: Binaries the session must be able to exec even though
            they live under the (denied) home dir — Claude Code, pyenv/uv
            pythons, nvm node. Each gets its resolved install dir, the
            sibling ``lib/`` of a ``bin/`` install dir, and the symlink
            files on the way re-allowed for reading — never ancestor
            directories (see :func:`_toolchain_allowread`). Default:
            :func:`default_toolchain` (this Python plus the standard names
            on PATH).
        allow_read: Extra paths re-allowed for reading on top of the run dir
            and the toolchain. The session runner passes exactly the factory
            package dir here — the in-sandbox entry point
            (``python -m factory.session``) imports it. Deliberately narrow:
            the wider repo (prompts, schemas, validator docs) stays
            unreadable to the session.
        local_binding: The caller's explicit request for srt's
            ``network.allowLocalBinding`` — granted only when the profile
            also opts in (``sandbox.allow_local_binding: true``). The GATE
            RUNNER's settings alone may pass it (review decision on #76);
            the session seats (implementer, reviewer, the importer's
            reviewer seat) and the SDK probe must never ask, whatever the
            profile says — see below for why.
        deny_read: Extra paths denied for reading (resolved). F-57's
            repair seat denies the source repo and its docs dir — the
            session must not re-read what it is repairing, wherever the
            repo lives (the home-wide deny says nothing about a repo
            outside $HOME).
        scratch_write: F-38 review finding 1 — the per-session scratch
            dirs of a fan-out session. When given (with ``read_only``),
            the writable list is EXACTLY these paths: not the run dir's
            ``.home``/``.tmp`` (a shared writable dir inside the readable
            clone would be a channel between "independent" reviewers) and
            not any other session's scratch (each session's settings name
            only its own). None is the single-session shape.
        record_write: F-56 — the session's own record dir (the
            ``<settings>-record/`` sibling). Appended to ``allowWrite``
            in EVERY seat shape and to ``allowRead`` like the scratch.
            What the binds do (review round 1, finding 4, proven by the
            reviewer's Linux mutation): the WRITE bind is what persists
            the file under the read-denied factory root on Linux —
            without it a write "succeeds" into the deny's tmpfs and never
            reaches the host (F-47 round 1, finding 1). The read bind is
            belt-and-braces: nothing inside the sandbox needs to READ
            the record (the entry writes it, the orchestrator reads it
            outside), but on macOS a denyRead root also refuses writes
            under it (the platform semantics in this module's
            docstring), so the pair is kept together on both platforms.
            The entry's atomic write (temp in the same dir, rename)
            needs the dir writable, not just the file. The session can
            write a record of its choosing there either way — the trust
            boundary is the entry process, unchanged from the stdout
            line it replaces (see session.py's F-56 notes).

    Returns:
        A settings dict accepted by ``srt --settings``.

    Raises:
        SandboxError: ``run_dir`` doesn't follow the canonical layout and no
            explicit ``factory_root`` was given; or the factory root is one
            whose read-deny would take the system with it (``/`` or a
            root at or above a system read root — see
            :func:`_refuse_unsafe_factory_root`).

    ``network.allowLocalBinding`` is granted only by the profile opt-in
    AND the caller's explicit request — two hands on one switch, because
    the flag is broader than it sounds. What it actually permits, read
    from the pinned srt source (node_modules/@anthropic-ai/sandbox-
    runtime/dist/sandbox/macos-sandbox-utils.js):

    - macOS (Seatbelt), three added rules:
      ``(allow network-bind (local ip "*:*"))`` — bind() on ANY local
      address/port, not loopback-only. The ``*`` is forced: Seatbelt
      cannot express "localhost" for IPv4-mapped IPv6 sockets
      (::ffff:127.0.0.1), so ``*:*`` is the only token that admits them.
      ``(allow network-inbound (local ip "*:*"))`` — inbound accepted on
      any local address/port; Seatbelt matches the local endpoint only,
      so the ORIGIN is unrestricted (host processes, and off-host hosts
      if the sandboxed code binds a non-loopback interface).
      ``(allow network-outbound (remote ip "localhost:*"))`` — connect()
      to 127.0.0.1/::1 on ANY port: every host loopback service becomes
      reachable (a logged-in browser's DevTools ports, local databases,
      dev and MCP servers). External-domain egress stays allowlisted
      through the proxy in all cases.
    - Linux (bubblewrap): the flag is never read — a no-op. The sandbox
      is its own network namespace; TCP bind inside it already works and
      nothing outside the namespace can reach those sockets.

    That outbound-to-loopback reach is why the scope is the gate runner
    only: gate commands are the profile's fixed commands, run after L0's
    diff policy, while the session seats execute model-written code with
    a model-chosen goal — they must not be able to touch the owner's
    loopback services even when the project's tests need port binding at
    the gate.
    """
    run_dir = Path(run_dir).resolve()
    runs_root = run_dir.parent.parent
    if factory_root is None:
        if runs_root.name != "runs":
            raise SandboxError(
                f"cannot derive the factory root from {run_dir}: its layout is not "
                "runs/<project>/<run-id>. Pass factory_root explicitly, or the "
                "holdouts deny would name a path that doesn't exist."
            )
        factory_root = runs_root.parent
    else:
        factory_root = Path(factory_root).resolve()
    _refuse_unsafe_factory_root(factory_root)
    if toolchain is None:
        toolchain = default_toolchain()
    allowed_domains = [
        _endpoint_host(model_endpoint),
        *profile.get("docs_allowlist", []),
        *profile.get("network_allowlist", []),
    ]
    denied_names = sorted(
        set(_FORBIDDEN_ENV_EXACT)
        | _GITHUB_TOKEN_NAMES
        | {k for k in os.environ if k.upper().startswith(_FORBIDDEN_ENV_PREFIXES)}
        | {name for name in forbidden_env if name}
    )
    # srt's Linux sandbox runs its vendor helpers (apply-seccomp, the java
    # agent jar) from INSIDE the bwrap namespace. On hosts where this repo
    # sits under $HOME — CI runners check out under /home/runner — the
    # home-wide denyRead below would hide them and every sandboxed command
    # would die at startup. Re-allow exactly the runtime's own package dir;
    # the factory's source (and everything else in the repo) stays denied.
    srt_package = _REPO_ROOT / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    if read_only and scratch_write is None:
        # The scratch dirs must exist for the reviewer's tooling (TMPDIR/
        # HOME land here); sandbox_env also creates them, but settings
        # built read-only are self-sufficient either way. A fan-out session
        # (scratch_write given) uses its own scratch dirs instead — nothing
        # inside the run dir is created or writable for it.
        for scratch in (run_dir / ".home", run_dir / ".tmp"):
            scratch.mkdir(parents=True, exist_ok=True)
    home = Path("~").expanduser()
    # F-47 (M-9): the factory root is a denied root like home, so a
    # toolchain installed under it (a repo-local venv when the root
    # contains the checkout) gets the same narrow carve-outs there.
    toolchain_reads = sorted(
        _toolchain_allowread(home, toolchain)
        | _toolchain_allowread(factory_root, toolchain),
        key=str,
    )
    network: dict = {"allowedDomains": allowed_domains, "deniedDomains": []}
    if local_binding and (profile.get("sandbox") or {}).get("allow_local_binding"):
        # Two hands on one switch (review decision on #76): the profile
        # opts in AND the caller explicitly asks — only the gate runner's
        # settings ever ask, so the session seats keep srt's default deny
        # (see the docstring for exactly what the flag permits).
        network["allowLocalBinding"] = True
    return {
        "network": network,
        "filesystem": {
            # F-47 (M-9): the factory root ITSELF is denied — trace/,
            # progress/, sessions/, other runs, the settings dir — so
            # isolation never depends on the root happening to live
            # under $HOME. The carve-outs are the explicit allowRead
            # entries below (own run, srt's package, the toolchain, the
            # caller's allow_read, the session's scratch); runs/ and
            # holdouts stay listed on their own (defence in depth).
            # Platform semantics of a read-denied DIRECTORY: Seatbelt
            # (macOS) refuses reads and writes under it; srt on Linux
            # mounts a tmpfs over it, so a write under a denied root
            # "succeeds" into that tmpfs and never reaches the host
            # (review round 1; tests/test_f05.py::test_ac_1 asserts the
            # host on both platforms). denyWrite does not change that.
            "denyRead": [
                str(home),
                str(factory_root),
                str(factory_root / "holdouts"),
                str(Path("~/.ssh").expanduser()),
                str(Path("~/.config/gh").expanduser()),
                str(runs_root),
                # F-57 (review round 1, F3): caller-named extra denies —
                # resolved, so a symlinked spelling cannot slip past. The
                # repair seat denies the SOURCE repo it must not re-read
                # (the empty workspace is the structural half; this is the
                # settings half, and it holds wherever the repo lives —
                # outside $HOME included, where the home-wide deny above
                # says nothing).
                *[str(Path(entry).resolve()) for entry in deny_read],
            ],
            "allowRead": [str(run_dir), str(srt_package)]
            + [str(entry) for entry in toolchain_reads]
            + [str(Path(entry).resolve()) for entry in allow_read]
            # A review seat's scratch sits under the denied home: being
            # writable does not make it readable, and the CLI dies at startup
            # reading its own $HOME (runs 20261030T120742Z-75e6df, fan-out;
            # 20261030T134446Z-f57b83, the single-session verifier).
            # The F-56 record dir joins the same carve-out list: the WRITE
            # bind is what persists it on Linux (review round 1, finding 4);
            # the read bind is belt-and-braces kept beside it.
            + [str(Path(entry).resolve())
               for entry in (*(scratch_write or ()), *writable_roots)]
            + ([str(Path(record_write).resolve())] if record_write else []),
            # Read-only mode (the reviewer seat): the run dir itself is NOT
            # writable — only scratch. A reviewer that can write the repo it
            # reviews can "fix" the code, watch its own fix pass, and report
            # PASS on a diff that isn't the implementer's; its commit would
            # ride the branch F-10 pushes. The writable roots are the
            # run-dir .home/.tmp spellings PLUS the caller's outside-the-run
            # scratch dirs (the session runner symlinks run/.home and
            # run/.tmp at them): on Linux, srt mounts a read-only bind of
            # the run dir AFTER the writable sub-directory binds — the
            # runs_root read-deny forces that restore — burying them, so
            # only the outside spellings stay live there; macOS is happy
            # with either. The denyWrite .git entries below stay for both
            # modes (defence in depth). Cache writes tools make in the tree
            # (__pycache__, .pytest_cache) fail closed: Python and pytest
            # degrade to warnings on unwritable caches, so read-only checks
            # still run.
            "allowWrite": _allow_write(
                run_dir, read_only, writable_roots, scratch_write,
                record_write,
            ),
            # The run's git control files are not the agent's to edit: a hook
            # or core.hooksPath/fsmonitor redirect planted here executes later,
            # OUTSIDE the sandbox, when the orchestrator commits and pushes
            # with its own credentials. srt already protects the repo under
            # the child's cwd; these absolute denies make that guarantee ours,
            # independent of cwd. `.git/info` holds the exclude file the
            # orchestrator seeds with warm-cache entries (create_run) — the
            # agent must not undo those excludes either.
            "denyWrite": [
                str(run_dir / ".git" / "hooks"),
                str(run_dir / ".git" / "config"),
                str(run_dir / ".git" / "info"),
            ],
            # srt blocks .git/config writes beneath cwd by default; init
            # and clone must write config, so relax the built-in rule —
            # the RUN's own .git/config stays denied by the explicit
            # denyWrite above (hook/core.hooksPath planting is still
            # impossible in the clone the orchestrator commits from).
            # What this re-allows is config for repos the WORK creates
            # (test fixtures, the agent's scratch repos), which die with
            # the run dir (INC-006).
            "allowGitConfig": True,
        },
        "credentials": {
            "envVars": [{"mode": "deny", "name": name} for name in denied_names]
        },
    }


def write_settings(settings: dict, path: Path) -> Path:
    """Write a settings dict to ``path`` (parent dirs created) as JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path


def process_is_sandboxed() -> bool:
    """Whether the kernel reports this process as already running under a
    macOS sandbox — the independent nesting signal (INC-007 review).

    Asked through libSystem's ``sandbox_check(pid, NULL, 0)``, which
    answers nonzero for a sandboxed process. Unlike the seatbelt probe it
    does not try (and fail) to apply anything, so a broken or slow
    ``sandbox-exec`` cannot make it lie.

    Returns:
        True inside a macOS sandbox; False otherwise, and always False
        off darwin.

    Raises:
        SandboxError: on darwin, when libSystem's ``sandbox_check`` cannot
            be called — the nesting answer is unknown, and an unknown must
            fail loudly rather than pass for "not nested" or "nested".
    """
    if sys.platform != "darwin":
        return False
    try:
        check = ctypes.CDLL("/usr/lib/libSystem.dylib").sandbox_check
    except (OSError, AttributeError) as exc:
        raise SandboxError(
            f"cannot ask the kernel whether this process is sandboxed: {exc}"
        ) from exc
    check.restype = ctypes.c_int
    check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    return check(os.getpid(), None, 0) != 0


@lru_cache(maxsize=1)
def can_apply_seatbelt() -> bool:
    """Whether this process may apply a macOS seatbelt profile.

    The nesting probe (INC-007): macOS refuses ``sandbox_apply`` from
    inside an existing sandbox — a sandboxed process that tries to
    launch another srt gets ``sandbox-exec: sandbox_apply: Operation not
    permitted`` no matter what the settings allow (the wall was probed
    through five layers: template reads, .git writes, unix-socket bind,
    loopback listen, then this — the last one is the kernel's, not
    srt's). The project's own suite runs inside the L0 sandbox and its
    sandbox tests spawn srt again, so they cannot run nested; they skip
    on this probe instead (tests/conftest.py) and run for real wherever
    the seatbelt stack is available — CI and any unsandboxed run.

    The probe is the truth, not an env var: nothing an agent does from
    inside its sandbox can make applying a second profile succeed, so
    the skip cannot be forged from within.

    "Nested" is answered only for the one shape nesting has: the probe
    refused with ``sandbox_apply`` AND :func:`process_is_sandboxed`
    confirming this process already runs under a sandbox. Every other
    failure — sandbox-exec missing (OSError), a timeout under load, a
    different refusal, or the refusal text on an unsandboxed process —
    is a broken host stack and raises: answering False there would turn
    the whole sandbox suite into silent skips (review of #56/#57; the
    F-05 tests fail, never skip). Only answers are cached; a raise is
    re-probed on the next call.

    Returns:
        True where a seatbelt profile can be applied (or the platform
        has no seatbelt — non-darwin answers True and lets srt's own
        Linux path decide), False inside a macOS sandbox.

    Raises:
        SandboxError: the probe failed and this is not nesting.
    """
    if sys.platform != "darwin":
        return True
    try:
        proc = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)",
             "/usr/bin/true"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(
            f"seatbelt probe could not run ({exc!r}): the macOS sandbox stack "
            "is unusable on this host — refusing to report it as nesting"
        ) from exc
    if proc.returncode == 0:
        return True
    refused = "sandbox_apply" in proc.stderr
    if refused and process_is_sandboxed():
        return False
    raise SandboxError(
        f"seatbelt probe failed (rc={proc.returncode}, stderr="
        f"{proc.stderr.strip()!r}, process sandboxed="
        f"{process_is_sandboxed()}): not the nested refusal — the macOS "
        "sandbox stack is broken on this host"
    )


def run_sandboxed(
    cmd: list[str],
    run_dir: Path,
    settings_path: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
    srt_bin_path: Path | None = None,
    venv: Path | None = None,
    scratch: "tuple[Path, Path] | None" = None,
    on_stdout_line=None,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` under srt confined to ``run_dir``.

    Args:
        cmd: The command (argv list) to run inside the sandbox.
        run_dir: The run dir: the child's cwd, HOME (``.home``) and TMPDIR
            (``.tmp``), and the only path it may write.
        settings_path: srt settings file (from :func:`build_settings` +
            :func:`write_settings`).
        env: Names to add to the child env on top of the base. The session
            allowlist (:data:`SESSION_ENV_ALLOWLIST`) applies to the whole
            result regardless of what is passed — ``env={**os.environ,
            token}`` cannot smuggle an unlisted secret across. Scrub
            keychain-derived names yourself (via :func:`sandbox_env`) — the
            settings file denies them as the backstop layer.
        venv: A run-local venv prepended to the child's PATH with
            VIRTUAL_ENV set (see :func:`sandbox_env`) — the gate commands'
            ``pip``/``python`` resolve into the run, never an ambient env.
        scratch: F-38 review finding 1 — a per-session ``(home, tmp)``
            pair (see :func:`sandbox_env`); the command's TMPDIR prefix
            follows it. None is the single-session shape.
        timeout: Wall-clock seconds for the leader. While it runs, the
            pipes are pumped by a single-threaded ``select`` loop over
            non-blocking fds (``proc.wait`` on PIPEd output would deadlock
            past the ~64KB pipe buffer); nothing in this process ever blocks
            on a pipe. On expiry — and also on a normal leader exit, so
            stragglers never outlive the run — the whole process group is
            SIGKILLed, then the pipes drain for at most
            :data:`_DRAIN_GRACE_SEC`; after that, pipes still held by a
            process that escaped the group (setsid) are closed, with the
            output read so far preserved. Such an escapee may itself
            survive (killing by pid namespace is srt's side, via bwrap's
            ``--die-with-parent``): the orchestrator stops waiting for it,
            and it loses *network* because srt's filtering proxy died — but
            it keeps the model token in its memory and environment for as
            long as it lives; deleting the run dir revokes nothing. Treat
            escaped processes as a prompt for the owner to rotate the seat
            token if abuse is suspected. F-47: the force-close is reported
            as ``force_closed_pipes`` (the names of the pipes still open
            at the drain deadline; empty when none) on the returned
            CompletedProcess AND on a raised TimeoutExpired; callers that
            own a trace emit :func:`escapee_event` and ``factory status``
            counts them.
        cwd: Working dir (default: the run dir).
        srt_bin_path: Test injection for the srt binary (still pin-checked).
            There is no production override path.
        on_stdout_line: Called with each complete stdout line as it arrives
            — before the child exits (F-30's live event transport; the
            captured output is unchanged). Exceptions from the callback are
            suppressed: transport observes, it never steers.

    Returns:
        The completed process (text mode, output captured).

    Raises:
        SandboxError: srt is missing or not at the pinned version — checked
            before the child is spawned.
        subprocess.TimeoutExpired: the wall clock ran out; the process group
            is dead (or force-detached) by the time it propagates.
    """
    binary = Path(srt_bin_path) if srt_bin_path else srt_bin()
    ensure_srt(srt_bin_path=binary)
    run_dir = Path(run_dir)
    if scratch is None:
        tmp = run_dir / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
    else:
        tmp = Path(scratch[1])
        tmp.mkdir(parents=True, exist_ok=True)
    child_env = sandbox_env(run_dir, extra=env, venv=venv, scratch=scratch)
    # srt's Linux network bridges create unix sockets in node's tmpdir().
    # A TMPDIR deep inside the run dir overflows the kernel's ~108-byte
    # sun_path limit and the bridges silently never come up ("Failed to
    # create bridge sockets"). So srt itself runs with no TMPDIR (node falls
    # back to the system temp, which is short), and the *child* gets its
    # run-dir TMPDIR as a shell assignment prefix instead — same environment
    # for the command, short socket paths for srt.
    child_env.pop("TMPDIR", None)
    command = f"TMPDIR={shlex.quote(str(tmp))} {shlex.join(cmd)}"
    # srt runs the command through a shell itself; `-c` takes one command
    # string verbatim ("no escaping applied"), so shlex.join carries our
    # argv across that boundary without quoting accidents. srt becomes a
    # session leader (start_new_session), so one killpg reaches the whole
    # tree — shell, session processes, socat bridges.
    proc = subprocess.Popen(
        [str(binary), "--settings", str(settings_path), "-c", command],
        env=child_env,
        cwd=str(cwd if cwd else run_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    # Pump the pipes with non-blocking reads while the leader runs: waiting
    # on the process with PIPEd output deadlocks the moment a command writes
    # past the pipe buffer (~64KB) — pytest -v, npm install logs, Claude
    # Code streams all blow past it. Nothing in this process ever blocks on
    # a pipe: every read goes through select with a bounded timeout.
    # Both streams are PIPEd above; the assert states it for the type
    # checker (Popen's stubs keep stdout/stderr Optional).
    assert proc.stdout is not None and proc.stderr is not None
    streams = {"out": proc.stdout.fileno(), "err": proc.stderr.fileno()}
    buffers = {"out": bytearray(), "err": bytearray()}
    for fd in streams.values():
        os.set_blocking(fd, False)
    timed_out = False
    deadline = None if timeout is None else time.monotonic() + timeout
    # F-30: complete stdout lines are handed to the caller as they arrive.
    # A partial line waits for its newline; a UTF-8 character split across
    # chunks is completed by the chunk carrying its rest, which precedes
    # the newline by construction — only whole lines are decoded.
    partials = {"out": bytearray()}

    # Phase 1: pump while the leader runs. Polling (not wait()) so a leader
    # that exits while a background child still holds the pipes is noticed
    # within 0.5s and the stragglers are killed instead of stalling us.
    while proc.poll() is None:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        if deadline is None:
            wait = 0.5
        else:
            wait = max(0.0, min(0.5, deadline - time.monotonic()))
        _pump(streams, buffers, wait,
              on_stdout_line=on_stdout_line, partials=partials)

    # Kill every remaining group member: on timeout the leader is one of
    # them; on a normal exit this reaps background children the session
    # left behind (`npm run dev &`, test servers) so nothing outlives the
    # run holding its pipes or its credentials.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # pid == pgid (session leader)
    proc.wait()  # reap the leader

    # Phase 2: bounded drain — whatever the killed tree left in the pipes,
    # plus EOF now that its writers are dead. A process that escaped the
    # group (setsid) keeps its write-end open; the grace expiry is what
    # stops us waiting for it. The raw fds are then simply closed (nothing
    # of ours is blocked on them) and the output read so far is kept.
    drain_deadline = time.monotonic() + _DRAIN_GRACE_SEC
    while any(fd >= 0 for fd in streams.values()):
        if time.monotonic() >= drain_deadline:
            break
        _pump(streams, buffers, max(0.0, drain_deadline - time.monotonic()),
              on_stdout_line=on_stdout_line, partials=partials)
    # F-47 (I-9): a pipe still open at the drain deadline has a writer
    # that outlived the group kill — a process that escaped it (setsid).
    # That fact rides out on what this returns or raises; the callers that
    # own a trace turn it into a ``sandbox_escapee`` event. This layer
    # stays free of trace I/O.
    force_closed = tuple(
        label for name, label in (("out", "stdout"), ("err", "stderr"))
        if streams[name] >= 0
    )
    # Close the owning file objects exactly once each (single-threaded here,
    # so nothing can be mid-read on them): raw os.close would race the
    # buffered objects' own close at GC time and leak unraisable warnings.
    for stream in (proc.stdout, proc.stderr):
        with suppress(OSError, ValueError):
            stream.close()
    out = buffers["out"].decode(errors="replace")
    err = buffers["err"].decode(errors="replace")
    if timed_out:
        exc = _ForcedCloseTimeoutExpired(
            proc.args, cast(float, timeout), output=out, stderr=err
        )
        exc.force_closed_pipes = force_closed
        raise exc
    completed = _ForcedCloseCompletedProcess(proc.args, proc.returncode, out, err)
    completed.force_closed_pipes = force_closed
    return completed


def escapee_event(label: str, force_closed: "Sequence[str]", *,
                  timed_out: bool = False) -> dict:
    """The one ``sandbox_escapee`` trace event shape (F-47, I-9).

    Built here, beside the signal it reports, so every caller that owns a
    trace (the gate runner, the session seats, the stale-PR sweep) emits
    the same fields; the trace writer stamps run id, task, attempt and
    time. ``label`` names what ran (``gate:<check>``,
    ``session:<seat>``)."""
    return {
        "type": "sandbox_escapee",
        "label": label,
        "streams": list(force_closed),
        "timed_out": bool(timed_out),
    }


class _ForcedCloseTimeoutExpired(subprocess.TimeoutExpired):
    """TimeoutExpired carrying the F-47 drain fact (force_closed_pipes)
    as a declared attribute instead of a dynamic one — same exception
    identity for every except TimeoutExpired handler (F-51/A-7)."""

    force_closed_pipes: "tuple[str, ...]" = ()


class _ForcedCloseCompletedProcess(subprocess.CompletedProcess):
    """CompletedProcess with the same declared drain fact."""

    force_closed_pipes: "tuple[str, ...]" = ()


def _pump(streams: dict, buffers: dict, wait: float, *,
          on_stdout_line=None, partials: dict | None = None) -> None:
    """One bounded select+read round over the open pipes. EOF marks the
    stream closed by setting its fd entry to -1. Complete stdout lines are
    dispatched to ``on_stdout_line`` as they arrive (suppressed errors:
    observation never steers)."""
    open_fds = [fd for fd in streams.values() if fd >= 0]
    if not open_fds:
        return
    ready, _, _ = select.select(open_fds, [], [], wait)
    for name, fd in streams.items():
        if fd < 0 or fd not in ready:
            continue
        try:
            chunk = os.read(fd, 1 << 16)
        except BlockingIOError:
            continue
        if chunk:
            buffers[name] += chunk
            if on_stdout_line is not None and name == "out":
                # Callers pair on_stdout_line with partials (both or
                # neither); the assert keeps that loud instead of None.
                assert partials is not None
                # Search only the new chunk for a newline; the accumulated
                # partial is copied just once, when a line is actually
                # emitted — a long newline-free line over many small reads
                # must not cost a full copy per read.
                idx = chunk.rfind(b"\n")
                if idx < 0:
                    partials["out"] += chunk
                    continue
                data = bytes(partials["out"]) + chunk[: idx + 1]
                partials["out"] = bytearray(chunk[idx + 1:])
                for raw in data.split(b"\n")[:-1]:
                    if raw.strip():
                        with suppress(Exception):
                            on_stdout_line(raw.decode(errors="replace"))
        else:  # EOF: every writer on this pipe is gone
            streams[name] = -1


@lru_cache(maxsize=None)
def _python_toolchain() -> tuple[Path, ...]:
    """The real interpreters behind ``python3``/``python`` on PATH.

    pyenv (and friends) put a *script* named ``python3`` on PATH, not a
    symlink, so ``shutil.which`` alone names the shim — re-allowing the shim
    dir leaves the interpreter it runs unreadable. Each found command is
    asked for its own ``sys.executable`` instead (outside the sandbox, once
    per process). A command that fails or hangs is skipped, not fatal.
    """
    found: list[Path] = []
    for name in ("python3", "python"):
        command = shutil.which(name)
        if not command:
            continue
        try:
            proc = subprocess.run(
                [command, "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        executable = proc.stdout.strip()
        if proc.returncode == 0 and executable:
            real = Path(os.path.realpath(executable))
            if real not in found:
                found.append(real)
    return tuple(found)


def _toolchain_allowread(home: Path, toolchain: "Sequence[Path]") -> set[Path]:
    """Paths below ``home`` that must stay readable so the session's
    toolchain can exec:

    * the resolved binary's own directory;
    * when that directory is a ``bin/``, its sibling ``lib/`` — where an
      interpreter's stdlib and site-packages live (pyenv's
      ``versions/3.12/lib``, nvm's ``versions/node/v22/lib``). Never the
      whole parent: for ``~/.local/bin`` that would re-open all of
      ``~/.local`` (``~/.local/share`` holds uv's credential store,
      cursor-agent, Copilot shims);
    * when the binary is a venv interpreter (PEP 405: a ``pyvenv.cfg``
      sits beside its ``bin/``), the venv's own ``pyvenv.cfg``, ``bin/``
      and ``lib*/python*/site-packages``. ``.resolve()`` follows past
      the venv to the base install (covered by the rules above), but
      without the venv's own files Python silently starts as the bare
      base interpreter — ``sys.prefix`` collapses, the venv's
      site-packages (claude_agent_sdk, …) vanish from ``sys.path``
      (run 20261029T130912Z-1e4eb9). Exactly the venv's subpaths, never
      the venv directory's parent: that is the repo the venv lives in,
      with the checkout and any secrets in it;
    * every symlink on the way to the binary, as the literal link *file*
      (never its directory). The native Claude Code install is
      ``~/.local/bin/claude -> ~/.local/share/claude/versions/X``; without
      the link itself, exec by that path or by PATH lookup is denied
      (rc=126 / rc=127) even though the target is readable.

    Ancestors are deliberately NOT re-allowed: allowing the chain up to
    ``$HOME`` would re-open whole subtrees like ``~/.local``.
    """
    entries: set[Path] = set()
    # Both spellings of home: link paths come in as given (under the
    # unresolved home), binaries are .resolve()d (under the real one). A
    # HOME with a symlink component (/tmp, /var -> /private on macOS) would
    # otherwise put nothing "below home" and emit no reads at all.
    homes = {home}
    with suppress(OSError):
        homes.add(home.resolve())

    def below_home(path: Path) -> bool:
        return any(path != h and h in path.parents for h in homes)

    def venv_reads(prefix: Path) -> None:
        """A PEP 405 venv rooted at ``prefix``: its own files, read-only."""
        entries.add(prefix / "pyvenv.cfg")
        entries.add(prefix / "bin")
        entries.update(
            site
            for lib in prefix.glob("lib*")
            if lib.is_dir()
            for site in lib.glob("python*/site-packages")
        )

    for raw in toolchain:
        link = Path(raw).expanduser()
        if not link.is_absolute():
            continue
        given = link
        for _ in range(40):  # same hop limit as the kernel's ELOOP
            if not link.is_symlink():
                break
            if below_home(link):
                entries.add(link)
            link = link.parent / os.readlink(link)
        try:
            binary = Path(raw).expanduser().resolve()
        except OSError:
            continue
        # The venv check runs on BOTH the given path's prefix and the
        # resolved binary's: a venv python is itself a symlink to the
        # base install, so only the given spelling names the venv.
        for prefix in {given.parent.parent, binary.parent.parent}:
            if below_home(prefix) and (prefix / "pyvenv.cfg").is_file():
                venv_reads(prefix)
        directory = binary.parent
        if not below_home(directory):
            continue
        entries.add(directory)
        if directory.name == "bin" and directory.parent not in homes:
            entries.add(directory.parent / "lib")
    return entries


def _endpoint_host(endpoint: str) -> str:
    """Hostname of a URL or bare-host endpoint (``https://api.z.ai/api`` → ``api.z.ai``)."""
    if "://" in endpoint:
        return urlsplit(endpoint).hostname or endpoint
    return endpoint
