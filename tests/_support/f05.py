"""Shared harness moved from tests/test_f05.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f05.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _fixture_repo(tmp_path: Path) -> Path:
    """A tiny local repo to clone from (the real clone source for runs)."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# fixture\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@factory.local")
    _git(repo, "config", "user.name", "factory test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _child(script: str, args: list[str]) -> list[str]:
    return [sys.executable, "-c", script, *args]
