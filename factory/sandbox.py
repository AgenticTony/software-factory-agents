"""The session sandbox: srt, pinned, with settings built from the profile.

Isolation is Anthropic's ``srt`` (``@anthropic-ai/sandbox-runtime``) —
Seatbelt on macOS, bubblewrap on Linux — installed at an exact version in
this repo's ``package.json`` (see BUILD-ORDER F-05: pin the srt version).
The factory refuses to sandbox anything unless ``srt --version`` matches
: data:`SRT_PIN`, so a drifted or broken install stops the factory instead
of quietly weakening the sandbox.

The settings file (DESIGN-LOCAL-FACTORY D3) allows writes only inside the
run dir, denies reads of the holdouts, ``~/.ssh``, ``~/.config/gh`` and
every other run, and allows network only to the model endpoint plus the
project profile's ``docs_allowlist`` and ``network_allowlist``. Credentials
are defence-in-depth: the environment is scrubbed before the child is
spawned *and* the settings file denies the same names inside srt, so a
caller mistake can't leak a GitHub token or a keychain-derived variable.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

SRT_PIN = "0.0.79"
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Structurally blocked env names: nothing a build session does needs GitHub
# credentials, so they never cross the sandbox boundary in either layer.
_FORBIDDEN_ENV_EXACT = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
)
_FORBIDDEN_ENV_PREFIXES = ("GH_", "GITHUB_")
_ENV_BASE = ("PATH", "HOME", "LANG", "TZ")


class SandboxError(Exception):
    """The sandbox cannot be used: srt missing, wrong version, or unusable."""


def srt_bin() -> Path:
    """Path to the pinned srt binary (``node_modules/.bin/srt``).

    Overridable with ``FACTORY_SRT_BIN`` so tests can inject fakes; the
    override must still report the pinned version or :func:`ensure_srt`
    refuses it.
    """
    override = os.environ.get("FACTORY_SRT_BIN")
    if override:
        return Path(override)
    return _REPO_ROOT / "node_modules" / ".bin" / "srt"


def ensure_srt(srt_bin_path: Path | None = None) -> str:
    """Check ``srt --version`` against the pin and return the version string.

    Args:
        srt_bin_path: Binary to check (default: the repo-pinned install, or
            ``FACTORY_SRT_BIN``).

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
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
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
    """Build a minimal child env: base vars, run-dir TMPDIR, scrubbed extras.

    The child sees nothing inherited from the orchestrator's environment
    except PATH/HOME/LANG/TZ — so an interactive orchestrator shell (with
    its own logins and tokens) can't leak into a session by accident.
    TMPDIR points inside the run dir because writes are allowed nowhere
    else; tools that need scratch space get it there.
    """
    run_dir = Path(run_dir)
    tmp = run_dir / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    base = {key: os.environ[key] for key in _ENV_BASE if key in os.environ}
    base["TMPDIR"] = str(tmp)
    if extra:
        base.update(extra)
    return scrub_env(base, forbidden)


def build_settings(
    profile: dict,
    run_dir: Path,
    *,
    model_endpoint: str,
    forbidden_env: tuple[str, ...] = (),
) -> dict:
    """Build the srt settings dict for one run from the project profile.

    Layout assumption: ``run_dir`` is ``<factory_root>/runs/<project>/<run-id>``
    (what :func:`factory.runs.create_run` produces), so the factory root and
    runs root are derived from it.

    Args:
        profile: The loaded ``project.yaml`` (``docs_allowlist`` /
            ``network_allowlist`` keys are read; both optional here).
        run_dir: The session's run dir — the only writable, self-readable
            path inside the sandbox.
        model_endpoint: The seat's model endpoint (Z.ai base URL or bare
            host). Its hostname joins the network allowlist.
        forbidden_env: Keychain-derived env names denied in addition to the
            structural GitHub set (same list the orchestrator scrubs).

    Returns:
        A settings dict accepted by ``srt --settings``.
    """
    run_dir = Path(run_dir).resolve()
    runs_root = run_dir.parent.parent
    factory_root = runs_root.parent
    allowed_domains = [
        _endpoint_host(model_endpoint),
        *profile.get("docs_allowlist", []),
        *profile.get("network_allowlist", []),
    ]
    denied_names = sorted(
        set(_FORBIDDEN_ENV_EXACT) | {name for name in forbidden_env if name}
    )
    return {
        "network": {"allowedDomains": allowed_domains, "deniedDomains": []},
        "filesystem": {
            "denyRead": [
                str(factory_root / "holdouts"),
                str(Path("~/.ssh").expanduser()),
                str(Path("~/.config/gh").expanduser()),
                str(runs_root),
            ],
            "allowRead": [str(run_dir)],
            "allowWrite": [str(run_dir)],
            "denyWrite": [],
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
) -> subprocess.CompletedProcess:
    """Run ``cmd`` under srt confined to ``run_dir``.

    Args:
        cmd: The command (argv list) to run inside the sandbox.
        run_dir: The run dir: the child's cwd and TMPDIR, and the only path
            it may write.
        settings_path: srt settings file (from :func:`build_settings` +
            :func:`write_settings`).
        env: Explicit child env. It is **always** scrubbed of the structural
            GitHub names; scrub keychain-derived names yourself (via
            :func:`sandbox_env`) — the settings file denies them as the
            backstop layer.
        timeout: Wall-clock seconds for the child (propagates
            ``subprocess.TimeoutExpired``).
        cwd: Working dir (default: the run dir).

    Returns:
        The completed process (text mode, output captured).

    Raises:
        SandboxError: srt is missing or not at the pinned version — checked
            before the child is spawned.
    """
    ensure_srt()
    run_dir = Path(run_dir)
    tmp = run_dir / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    if env is None:
        child_env = sandbox_env(run_dir)
    else:
        child_env = scrub_env(env)
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
    # argv across that boundary without quoting accidents.
    return subprocess.run(
        [
            str(srt_bin()),
            "--settings",
            str(settings_path),
            "-c",
            command,
        ],
        env=child_env,
        cwd=str(cwd if cwd else run_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _endpoint_host(endpoint: str) -> str:
    """Hostname of a URL or bare-host endpoint (``https://api.z.ai/api`` → ``api.z.ai``)."""
    if "://" in endpoint:
        return urlsplit(endpoint).hostname or endpoint
    return endpoint
