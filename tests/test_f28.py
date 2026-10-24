"""F-28 step 1: this repo's profile protects what judges agents.

The dogfood pilot (BUILD-ORDER F-28) points the factory at this repo.
Before the first session runs, everything that judges a session is a
protected path, and the repo declares its free-plan branch-protection
limit so F-26 admission accepts the factory's own up-to-date check in
place of GitHub's (which the plan cannot offer a private free repo).

The existing contract tests are enumerated, not globbed: the session
hook denies any write matching a protected pattern, including file
creation, so a ``tests/test_f*.py`` glob would stop the F-11/F-12
implementers from writing their own red-first contract tests.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

# What judges agents, frozen at F-28 (BUILD-ORDER F-28 step 1).
JUDGE_MODULES = [
    "factory/l0.py",
    "factory/l1.py",
    "factory/l2.py",
    "factory/gates.py",
    "factory/sandbox.py",
    "factory/session.py",
    "factory/publish.py",
    "factory/orchestrator.py",
    "factory/prompts.py",
    "factory/trace.py",
]

# The contract tests existing when F-28 ran, plus this file. Frozen by
# design: a live glob here would fail the moment the dogfood runs merge
# tests/test_f11.py / tests/test_f12.py, which are deliberately absent.
EXISTING_TEST_FILES = [
    f"tests/test_f{n:02d}.py" for n in
    (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 23, 24, 25, 26, 27, 29, 30, 33, 28)
]


def _profile() -> dict:
    return yaml.safe_load((REPO / ".factory" / "project.yaml").read_text())


def test_protected_covers_what_judges_agents():
    """Every judge module and the prompts are protected paths, so a
    dogfood session cannot edit what judges it (Principle 5)."""
    from factory.gates import matches

    protected = _profile()["paths"]["protected"]
    for path in JUDGE_MODULES + ["prompts/implementer.md", "prompts/reviewer.md"]:
        assert matches(path, protected), (
            f"{path} is not covered by paths.protected: a dogfood session "
            "could edit what judges it"
        )


def test_protected_enumerates_existing_contract_tests():
    """Each existing tests/test_f*.py is protected by its own entry, and
    the set is an enumeration, never a glob."""
    from factory.gates import matches

    protected = _profile()["paths"]["protected"]
    for path in EXISTING_TEST_FILES:
        assert matches(path, protected), (
            f"{path} is not covered by paths.protected: a later session "
            "could weaken the test that defines another task's done"
        )
    assert "tests/test_f*.py" not in protected, (
        "a tests/test_f*.py glob would deny creating tests/test_f11.py and "
        "tests/test_f12.py red-first: the hook matches file creation too"
    )


def test_enumerated_test_files_exist():
    """No stale entries: every enumerated test file is on disk."""
    for path in EXISTING_TEST_FILES:
        assert (REPO / path).is_file(), f"{path} is protected but missing"


def test_setup_provisions_the_pinned_sandbox_runtime():
    """A cold clone must become fully runnable from the profile's own
    setup command. F-28 step 2 found the gap for real: a clean clone
    passed setup and lint, then failed 142 suite tests, every one
    tracing to ``srt not found at <repo>/node_modules/.bin/srt``. The
    sandbox tests resolve srt inside the repo, so setup must run
    ``npm ci`` alongside pip — the documented setup (README, CI) — and
    the sandbox network allowlist must let it reach the npm registry."""
    profile = _profile()
    setup = profile["commands"]["setup"]
    assert "pip install" in setup and "npm ci" in setup, (
        "commands.setup must provision both halves of the documented "
        'setup (pip install -e ".[dev]" and npm ci): a cold clone fails '
        "the sandbox tests without node_modules"
    )
    assert "registry.npmjs.org" in profile["network_allowlist"], (
        "setup runs inside the sandboxed gate runner; npm ci cannot "
        "fetch without registry.npmjs.org in network_allowlist"
    )


def test_branch_protection_declared_unavailable():
    """The repo declares the free-plan protection limit (F-26 AC-1's
    exception), so admission accepts the factory's own up-to-date check.
    The profile loads through the canonical validating loader: schema
    and semantics both accept the declaration."""
    from factory.contract import load_profile

    profile = load_profile(REPO / ".factory" / "project.yaml")
    assert profile["repo"]["branch_protection"] == "unavailable"
