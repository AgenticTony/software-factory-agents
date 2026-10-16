"""Clone manager: one disposable run dir per session (DESIGN-LOCAL-FACTORY D3).

Each session works in ``<factory_root>/runs/<project>/<run-id>/``, a
``git clone --filter=blob:none`` of the local repo (blobs stream on demand,
so clones are cheap). A warm dependency cache can be seeded into the run
dir so sessions don't pay a fresh install every time. Everything a run
produces is disposable: ``cleanup_run`` deletes the clone and its sandbox
settings without a trace.
"""

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FACTORY_ROOT_DEFAULT = Path.home() / "factory"


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
    dest = runs_root / project / _new_run_id()
    while dest.exists():  # uuid suffix makes this near-impossible; stay safe
        dest = runs_root / project / _new_run_id()

    result = subprocess.run(
        ["git", "clone", "--filter=blob:none", source.as_uri(), str(dest)],
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
            ["git", "-C", str(dest), "checkout", "--detach", commit],
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
        try:
            shutil.copytree(warm_cache, dest, dirs_exist_ok=True)
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise RunError(f"warm cache copy from {warm_cache} into {dest} failed: {exc}")

    settings_path = root / "sandbox" / f"{project}-{dest.name}.json"
    return RunDir(project=project, run_id=dest.name, root=dest, settings_path=settings_path)


def cleanup_run(run: RunDir) -> None:
    """Delete the run's clone and settings file. Idempotent: a missing run
    (already cleaned, or never finished creating) is not an error."""
    shutil.rmtree(run.root, ignore_errors=True)
    run.settings_path.unlink(missing_ok=True)


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _resolve_commit(repo: Path, ref: str) -> str | None:
    """First of ``ref`` / ``origin/<ref>`` that resolves to a commit, else None."""
    for candidate in (ref, f"origin/{ref}"):
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return None
