"""Shared harness moved from tests/test_f12.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f12.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

from pathlib import Path


def _profile_yaml() -> str:
    """The smallest profile the project schema accepts."""
    return (
        "name: proj\n"
        "repo:\n"
        "  path: .\n"
        "  default_branch: main\n"
        "  remote: https://example.invalid/proj.git\n"
        "commands:\n"
        '  setup: "true"\n'
        '  test: "true"\n'
        "paths:\n"
        "  risk: {}\n"
        "  protected: []\n"
        "deploy:\n"
        "  mechanism: none\n"
        "  auto_on_merge: false\n"
        "live_checks: {}\n"
        "docs_allowlist: []\n"
    )


def _repo(tmp_path: Path, *, oos: dict[str, str] | None = None) -> Path:
    """A fixture project: planning docs, a profile, optionally the
    out-of-scope register.

    Idempotent: a test may call it twice against the same ``tmp_path``
    (to add the register to a project it already built) — every
    directory is created ``exist_ok`` so the second call extends the
    first instead of raising ``FileExistsError``."""
    repo = tmp_path / "proj"
    docs = repo / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "PLAN.md").write_text(
        "# Plan\n\n## WO-1: add search\n\nAcceptance: search finds a word.\n\n"
        "## WO-2: billing page\n\nAcceptance: the page renders an invoice.\n"
    )
    factory_dir = repo / ".factory"
    factory_dir.mkdir(exist_ok=True)
    (factory_dir / "project.yaml").write_text(_profile_yaml())
    if oos:
        register = factory_dir / "out-of-scope"
        register.mkdir(exist_ok=True)
        for name, text in oos.items():
            (register / name).write_text(text)
    return repo


def _git_repo(tmp_path: Path) -> Path:
    """The fixture project, committed: the sandboxed importer session
    reads a disposable clone of the repo's HEAD, never the checkout."""
    import subprocess

    repo = _repo(tmp_path)
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "init"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    return repo
