"""Shared harness moved from tests/test_f07.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f07.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

import subprocess
from pathlib import Path

from factory.l0 import run_l0

BASE_PROFILE = {
    "name": "fixture",
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
}


TASK = {"id": "T-1", "risk": []}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """Build a fixture git repo with one base commit on ``main``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _change(
    repo: Path,
    files: dict[str, str] | None = None,
    delete: tuple[str, ...] = (),
    rename: tuple[tuple[str, str], ...] = (),
) -> None:
    """Add a ``feature`` branch commit with the given changes."""
    _git(repo, "checkout", "-b", "feature")
    for rel, text in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for old, new in rename:
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", old, new)
    for rel in delete:
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")


def _run(repo: Path, profile=None, task=None, baseline=None, **kwargs):
    return run_l0(
        profile or dict(BASE_PROFILE),
        repo,
        "main",
        "feature",
        task=task or dict(TASK),
        baseline=baseline,
        **kwargs,
    )
