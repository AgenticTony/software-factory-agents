"""The session sandbox: srt, pinned, with settings built from the profile.

Isolation is Anthropic's ``srt`` (``@anthropic-ai/sandbox-runtime``) —
Seatbelt on macOS, bubblewrap on Linux — installed at an exact version in
this repo's ``package.json`` (see BUILD-ORDER F-05: pin the srt version).
The factory refuses to sandbox anything unless ``srt --version`` matches
: data:`SRT_PIN`, so a drifted or broken install stops the factory instead
of quietly weakening the sandbox.

The settings file (DESIGN-LOCAL-FACTORY D3) allows writes only inside the
run dir (minus its ``.git/hooks`` and ``.git/config`` — see GIT_HARDENING
in :mod:`factory.runs`), denies reads of the entire home directory (the run
dir is re-allowed), the holdouts, and every other run, and allows network
only to the model endpoint plus the project profile's ``docs_allowlist``
and ``network_allowlist``. Credentials are defence-in-depth: the
environment is scrubbed before the child is spawned *and* the settings file
denies the same names inside srt, so a caller mistake can't leak a GitHub
token or a keychain-derived variable.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

SRT_PIN = "0.0.79"
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Structurally blocked env names: nothing a build session does needs GitHub
# credentials, so they never cross the sandbox boundary in either layer.
_FORBIDDEN_ENV_EXACT = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
)
_FORBIDDEN_ENV_PREFIXES = ("GH_", "GITHUB_")

# The only variable names a sandboxed session may ever see. Everything else
# is dropped on every path — ambient os.environ never crosses, and neither
# does a caller's dict: `env={**os.environ, token}` (the obvious F-06 call)
# leaks nothing because only these names survive the merge. Adding a name
# here is a deliberate, reviewable act; that is the point.
SESSION_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "TZ",
        "TMPDIR",
        # the model seat (F-06): base URL, the one secret the sandbox holds
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "API_TIMEOUT_MS",
        # git identity: the sandbox cannot read ~/.gitconfig (home is denied),
        # so identity arrives explicitly per session
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    }
)
SESSION_ENV_PREFIXES = ("LC_", "GIT_CONFIG_")  # locales; `git -c` via env

# Seconds to wait for output after the process group is killed: pipe buffer
# drains fast when every writer is dead; a process that escaped the group
# (setsid) and holds the pipes is force-closed after this grace instead of
# hanging the orchestrator.
_DRAIN_GRACE_SEC = 10.0

# Binaries a session may need to exec that can live under $HOME: resolved
# and re-allowed for reading by build_settings (the home-wide denyRead would
# otherwise make the session's own toolchain unexecutable — rc=126).
TOOLCHAIN_BINS = ("node", "npm", "npx", "claude", "git", "curl")


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
) -> dict[str, str]:
    """Build the child env: allowlisted names only, HOME inside the run.

    Two rules, applied to every path (ambient *and* caller-supplied):

    * only names in :data:`SESSION_ENV_ALLOWLIST` (or the ``LC_``/
      ``GIT_CONFIG_`` prefixes) survive — so ``extra=dict(os.environ)``
      cannot smuggle an unlisted secret across, and neither can ambient
      inheritance;
    * the GitHub/forbidden scrub runs on top of the allowlist.

    HOME points at ``<run_dir>/.home`` — a writable scratch, because the
    real home is denyRead and every tool that touches ``~/.cache`` or
    ``~/.config`` would otherwise fail. TMPDIR points at ``<run_dir>/.tmp``
    (writes are allowed nowhere else).
    """
    run_dir = Path(run_dir)
    home = run_dir / ".home"
    home.mkdir(parents=True, exist_ok=True)
    tmp = run_dir / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {}
    merged = dict(os.environ)
    if extra:
        merged.update(extra)
    for key, value in merged.items():
        if key in SESSION_ENV_ALLOWLIST or key.startswith(SESSION_ENV_PREFIXES):
            env[key] = value
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    return scrub_env(env, forbidden)


def build_settings(
    profile: dict,
    run_dir: Path,
    *,
    model_endpoint: str,
    forbidden_env: tuple[str, ...] = (),
    factory_root: Path | None = None,
    toolchain: "Sequence[Path] | None" = None,
) -> dict:
    """Build the srt settings dict for one run from the project profile.

    Args:
        profile: The loaded ``project.yaml`` (``docs_allowlist`` /
            ``network_allowlist`` keys are read; both optional here).
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
            pythons, nvm node. Each is resolved and its directory chain
            below home is re-allowed for reading. Default: this Python plus
            :data:`TOOLCHAIN_BINS` found on PATH.

    Returns:
        A settings dict accepted by ``srt --settings``.

    Raises:
        SandboxError: ``run_dir`` doesn't follow the canonical layout and no
            explicit ``factory_root`` was given.
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
    if toolchain is None:
        toolchain = [sys.executable]
        toolchain += [Path(found) for name in TOOLCHAIN_BINS
                      if (found := shutil.which(name))]
    allowed_domains = [
        _endpoint_host(model_endpoint),
        *profile.get("docs_allowlist", []),
        *profile.get("network_allowlist", []),
    ]
    denied_names = sorted(
        set(_FORBIDDEN_ENV_EXACT) | {name for name in forbidden_env if name}
    )
    # srt's Linux sandbox runs its vendor helpers (apply-seccomp, the java
    # agent jar) from INSIDE the bwrap namespace. On hosts where this repo
    # sits under $HOME — CI runners check out under /home/runner — the
    # home-wide denyRead below would hide them and every sandboxed command
    # would die at startup. Re-allow exactly the runtime's own package dir;
    # the factory's source (and everything else in the repo) stays denied.
    srt_package = _REPO_ROOT / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    home = Path("~").expanduser()
    toolchain_reads = sorted(
        _toolchain_allowread(home, toolchain), key=str
    )
    return {
        "network": {"allowedDomains": allowed_domains, "deniedDomains": []},
        "filesystem": {
            "denyRead": [
                str(home),
                str(factory_root / "holdouts"),
                str(Path("~/.ssh").expanduser()),
                str(Path("~/.config/gh").expanduser()),
                str(runs_root),
            ],
            "allowRead": [str(run_dir), str(srt_package)]
            + [str(entry) for entry in toolchain_reads],
            "allowWrite": [str(run_dir)],
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


def run_sandboxed(
    cmd: list[str],
    run_dir: Path,
    settings_path: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
    srt_bin_path: Path | None = None,
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
        timeout: Wall-clock seconds for the leader. On expiry — and also on
            a normal leader exit, so stragglers never outlive the run — the
            whole process group is SIGKILLed; output drains for at most
            :data:`_DRAIN_GRACE_SEC`, after which any pipe still held by a
            process that escaped the group (setsid) is force-closed. Such an
            escapee may itself survive (killing by pid namespace is srt's
            side, via bwrap's ``--die-with-parent``); the orchestrator never
            waits for it, and its model credentials die with the run dir.
        cwd: Working dir (default: the run dir).
        srt_bin_path: Test injection for the srt binary (still pin-checked).
            There is no production override path.

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
    tmp = run_dir / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    child_env = sandbox_env(run_dir, extra=env)
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
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    # Kill every remaining group member: on timeout the leader is one of
    # them; on a normal exit this reaps background children the session
    # left behind (`npm run dev &`, test servers) so nothing outlives the
    # run holding its pipes or its credentials.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # pid == pgid (session leader)
    try:
        out, err = proc.communicate(timeout=_DRAIN_GRACE_SEC)
    except subprocess.TimeoutExpired:
        out, err = _force_close(proc)
    if timed_out:
        raise subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _force_close(proc: subprocess.Popen) -> tuple[str, str]:
    """A process that escaped the group holds the pipes past the drain
    grace. Close them and reap the leader: the orchestrator returns with
    whatever output made it out, never hanging on the escapee."""
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            with suppress(OSError):
                stream.close()
    proc.wait()
    return "", ""


def _toolchain_allowread(home: Path, toolchain: "Sequence[Path]") -> set[Path]:
    """Directories below ``home`` that must stay readable so the session's
    toolchain can exec: the resolved binary's own directory and every
    ancestor up to (excluding) home — which, for a ``bin/`` directory,
    automatically includes the enclosing version prefix (pyenv's
    ``versions/3.12``, nvm's ``versions/node/v22``, uv's tool dir) where the
    interpreter's ``lib/`` and ``site-packages`` live.
    """
    entries: set[Path] = set()
    for raw in toolchain:
        binary = Path(raw).expanduser()
        try:
            binary = binary.resolve()
        except OSError:
            continue
        directory = binary.parent
        while directory != home and home in directory.parents:
            entries.add(directory)
            directory = directory.parent
    return entries


def _endpoint_host(endpoint: str) -> str:
    """Hostname of a URL or bare-host endpoint (``https://api.z.ai/api`` → ``api.z.ai``)."""
    if "://" in endpoint:
        return urlsplit(endpoint).hostname or endpoint
    return endpoint
