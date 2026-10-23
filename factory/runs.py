"""Clone manager: one disposable run dir per session (DESIGN-LOCAL-FACTORY D3).

Each session works in ``<factory_root>/runs/<project>/<run-id>/``, a
``git clone --filter=blob:none`` of the local repo (blobs stream on demand,
so clones are cheap). A warm dependency cache can be seeded into the run
dir so sessions don't pay a fresh install every time. Everything a run
produces is disposable: ``cleanup_run`` deletes the clone and its sandbox
settings without a trace.
"""

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FACTORY_ROOT_DEFAULT = Path.home() / "factory"

# The run-id shape (:func:`new_run_id`). The trace reader (F-30) checks its
# argument against this too, so a trace path can never be argued outside
# ``<factory_root>/trace/<project>/`` by argument spelling.
RUN_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")

# Every git command the orchestrator runs outside the sandbox disables hooks
# and fsmonitor: a hook planted inside a run (or a stale one from a bypassed
# deny) must never execute with the orchestrator's credentials. Sessions
# inside the sandbox can't rewrite .git/hooks or .git/config either (the
# sandbox settings deny both), so this is the outer layer of the same rule.
GIT_HARDENING = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


class RunError(Exception):
    """A run dir cannot be created or cleaned up as requested."""


@dataclass(frozen=True)
class RunDir:
    """A single session's clone plus its sibling srt settings file."""

    project: str
    run_id: str
    root: Path
    settings_path: Path


def create_run(
    project: str,
    source: Path,
    *,
    factory_root: Path | None = None,
    ref: str | None = None,
    warm_cache: Path | None = None,
) -> RunDir:
    """Clone ``source`` into a fresh run dir and return its :class:`RunDir`.

    Args:
        project: Project slug (``project.yaml`` ``name``); the run lives under
            ``<factory_root>/runs/<project>/``.
        source: Path to the local git repo to clone from.
        factory_root: Factory root (default ``~/factory``). Tests point this
            at a temp dir; production never overrides it.
        ref: Optional branch/tag/commit to detach-checkout after cloning.
        warm_cache: Optional directory copied into the run dir verbatim
            (prebuilt venv, ``node_modules``) so sessions start warm.

    Returns:
        The created run: its ``root`` is the clone, ``settings_path`` is where
        the sandbox settings for this run belong (written by the caller).

    Raises:
        RunError: The source repo doesn't exist, or git/copy failed. The
            message names the offending step; no partial dir is kept.
    """
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    source = Path(source).resolve()
    if not (source / ".git").exists():
        raise RunError(
            f"cannot clone run for {project!r}: {source} is not a git repository"
        )

    runs_root = root / "runs"
    dest = runs_root / project / new_run_id()
    while dest.exists():  # uuid suffix makes this near-impossible; stay safe
        dest = runs_root / project / new_run_id()

    # uploadpack.allowFilter: a local file:// server refuses blob filters by
    # default, git then WARNS ("filtering not recognized by server, ignoring")
    # and silently does a full clone — every run would copy all blobs of all
    # history, defeating D3's "clones are cheap". Overriding the upload-pack
    # command turns the filter on for the local source too.
    result = subprocess.run(
        [
            "git", *GIT_HARDENING, "clone", "--filter=blob:none",
            "--upload-pack", "git -c uploadpack.allowFilter=true upload-pack",
            source.as_uri(), str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise RunError(
            f"git clone --filter=blob:none failed for {project!r} "
            f"from {source}: {result.stderr.strip()}"
        )

    if ref is not None:
        # A branch name usually exists in the clone only as origin/<name>;
        # `git checkout --detach <name>` would then take the DWIM branch-
        # creation path, which rejects --detach. Resolve to a commit first.
        commit = _resolve_commit(dest, ref)
        if commit is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise RunError(f"ref {ref!r} not found in clone of {source} (run {dest})")
        result = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(dest), "checkout", "--detach", commit],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise RunError(
                f"checkout of ref {ref!r} ({commit}) failed in run {dest}: "
                f"{result.stderr.strip()}"
            )

    if warm_cache is not None:
        _refuse_tracked_collisions(dest, warm_cache)
        try:
            # symlinks=True: prebuilt venvs/node_modules are full of links.
            # Following them aborts the run on any dangling or circular link
            # (pnpm-style `self -> ..`) and copies a venv's bin/python as a
            # full interpreter file. Links are kept as links; absolute links
            # into the cache are then retargeted into the run.
            shutil.copytree(warm_cache, dest, symlinks=True, dirs_exist_ok=True)
            _retarget_cache_links(Path(warm_cache), dest)
            _relocate_venv(Path(warm_cache), dest)
            _seed_run_excludes(dest, warm_cache)
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise RunError(f"warm cache copy from {warm_cache} into {dest} failed: {exc}")
    else:
        _seed_run_excludes(dest, None)

    settings_path = root / "sandbox" / f"{project}-{dest.name}.json"
    return RunDir(project=project, run_id=dest.name, root=dest, settings_path=settings_path)


def cleanup_run(run: RunDir) -> None:
    """Delete the run's clone, settings file, staged runner copy
    (``<settings>-runner/``, written by the F-06 session runner) and
    reviewer scratch (``<settings>-scratch/``, written by the F-09
    read-only reviewer flow). Idempotent: a missing run (already cleaned,
    or never finished creating) is not an error."""
    shutil.rmtree(run.root, ignore_errors=True)
    run.settings_path.unlink(missing_ok=True)
    shutil.rmtree(run.settings_path.with_name(run.settings_path.name + "-runner"),
                  ignore_errors=True)
    shutil.rmtree(run.settings_path.with_name(run.settings_path.name + "-scratch"),
                  ignore_errors=True)


def new_run_id() -> str:
    """One run id (``<UTC-stamp>-<6 hex>``).

    ``create_run`` ids name a run dir; ids minted outside a run (F-26's
    session-less sweep escalations and recoveries) have no run dir —
    audit joins key on the id itself, and uniqueness is the contract
    (round-4 finding 6).
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _refuse_tracked_collisions(dest: Path, warm_cache: Path) -> None:
    """Refuse a cache that would overwrite tracked files: a stray
    package-lock.json in the cache would silently mutate the checkout the
    agent is supposed to diff against (phantom lockfile changes, L0
    new_dependency escalations, cache content in the PR)."""
    tracked = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(dest), "ls-files"],
        check=True, capture_output=True, text=True,
    ).stdout
    top_tracked = {
        line.split("/", 1)[0] for line in tracked.splitlines() if line.strip()
    }
    collisions = sorted(
        {entry.name for entry in Path(warm_cache).iterdir()} & top_tracked
    )
    if collisions:
        shutil.rmtree(dest, ignore_errors=True)
        raise RunError(
            f"warm cache would overwrite tracked files in the run: "
            f"{', '.join(collisions)}"
        )


def _retarget_cache_links(cache: Path, dest: Path) -> None:
    """Point absolute symlinks that lead into the cache at the run's copy.

    A link copied verbatim with an absolute cache target would still reach
    into the cache — which the sandbox may not even let the session read.
    Relative links and links to anything outside the cache are untouched.
    """
    prefixes = sorted({str(cache), str(cache.resolve())}, key=len, reverse=True)
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        for name in (*dirnames, *filenames):
            link = Path(dirpath) / name
            if not link.is_symlink():
                continue
            target = os.readlink(link)
            if not os.path.isabs(target):
                continue
            for prefix in prefixes:
                if target == prefix or target.startswith(prefix + os.sep):
                    link.unlink()
                    os.symlink(str(dest) + target[len(prefix):], link)
                    break


def _relocate_venv(cache: Path, dest: Path) -> None:
    """Rewrite a copied venv's absolute cache paths to the run dir.

    A venv is not relocatable as-is: ``bin/*`` shebangs and ``activate``
    embed the venv's absolute path, ``pyvenv.cfg`` points at its home, and
    ``.pth`` files reference install locations. Left as the cache path,
    ``.venv/bin/pip`` executes the *cache's* interpreter and installs target
    the cache — writes the sandbox denies. The venv embeds the path it was
    *created through*, so both the given path and its resolved form (a
    symlinked cache: ``/tmp`` vs ``/private/tmp`` on macOS) are rewritten.
    Best-effort by prefix substitution; only venv-looking trees are touched.
    """
    for venv_name in (".venv", "venv"):
        venv = dest / venv_name
        if not (venv / "pyvenv.cfg").exists():
            continue
        olds = {str(cache / venv_name), str(cache.resolve() / venv_name)}
        new = str(venv)
        targets = [venv / "pyvenv.cfg"]
        bin_dir = venv / "bin"
        if bin_dir.is_dir():
            # Never rewrite through a symlink: it may point outside the run.
            targets.extend(
                p for p in bin_dir.iterdir() if p.is_file() and not p.is_symlink()
            )
        targets.extend(venv.glob("lib*/python*/site-packages/*.pth"))
        for path in targets:
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue  # binaries (symlinked interpreters) have no text to fix
            if any(old in text for old in olds):
                for old in olds:
                    text = text.replace(old, new)
                path.write_text(text)


def _seed_run_excludes(dest: Path, warm_cache: Path | None) -> None:
    """Anchor the run's scratch dirs — and the warm cache's top-level
    entries, if any — in ``.git/info/exclude``.

    Keeps a session's ``git add -A`` from committing ``node_modules`` /
    ``.venv`` when the target repo's .gitignore doesn't cover them, and —
    always — from committing the sandbox's own scratch: ``.home`` (pip/npm
    caches, Claude Code session state and transcripts) and ``.tmp``.
    Entries are anchored with a leading ``/`` so they match only at the run
    root, and the scratch entries carry NO trailing slash: a trailing slash
    matches directories only, and the reviewer seat's scratch is a SYMLINK
    (F-09 `_reviewer_scratch`) — with a slash it shows as untracked and
    ``git add -A`` would commit the dangling link. ``.git/info`` is
    sandbox-denyWritten, so the agent can't undo it.
    """
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    entries = ["/.home", "/.tmp"]
    if warm_cache is not None:
        entries += [
            f"/{entry.name}" + ("/" if entry.is_dir() else "")
            for entry in sorted(Path(warm_cache).iterdir())
        ]
    with exclude.open("a") as fh:
        fh.write("\n# factory: run scratch (seeded by create_run)\n")
        fh.write("\n".join(entries) + "\n")


def _resolve_commit(repo: Path, ref: str) -> str | None:
    """First of ``ref`` / ``origin/<ref>`` that resolves to a commit, else None."""
    for candidate in (ref, f"origin/{ref}"):
        result = subprocess.run(
            [
                "git", *GIT_HARDENING, "-C", str(repo),
                "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return None
