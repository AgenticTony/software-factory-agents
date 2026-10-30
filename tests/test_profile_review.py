"""Profile invariants from the PR #50-#62 review (F-28 profile follow-up).

The F-28 step-1 protected list matched the BUILD-ORDER text exactly, and
the text itself was incomplete. These tests pin what the review found:

* #52 finding 1: the other modules that judge or gate agents
  (sentinels, the corpus replay, merge-order admission, the validating
  loader, the human approve/tick gates, the state machine, the PR-body
  evidence) are protected, so a session cannot weaken them.
* #52 finding 2 / #56 finding 3: ``conftest.py`` (any, new or existing)
  and the tool-config files that override pytest/ruff settings are
  protected, so a session cannot deselect or skip protected contract
  tests without touching a protected file.
* #52 finding 3: every done task's contract test file is protected —
  checked against ``tasks.json``, not a frozen list, so the PR that marks
  the next work order done fails this test until it adds the file.
* #53 finding 1: ``node_modules`` is a declared setup output, so L1's
  between-phase ``git clean -fdx`` keeps the srt runtime setup installed.

Protection must never block a future work order from creating its own
new ``tests/test_fNN.py`` red-first: the session hook denies file
creation under a protected glob too.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

# Modules that judge, gate or report on agents beyond the F-28 step-1 ten
# (tests/test_f28.py JUDGE_MODULES). Each is something a session could
# weaken to pass its own judging.
MORE_JUDGE_MODULES = [
    "factory/sentinels.py",   # F-25 built-in sentinels L0 delegates to
    "factory/replay.py",      # F-33 corpus gate over the sentinels
    "factory/mergeorder.py",  # F-26 admission + self-enforced up-to-date
    "factory/contract.py",    # F-02 validating loader, acceptance_hash
    "factory/approve.py",     # F-04 human spec-approval gate
    "factory/tick.py",        # F-29 human owner-step gate
    "factory/state.py",       # F-03 allowed-transition table, picker
    "factory/prbody.py",      # F-10 PR evidence built from validator facts
    "factory/toolkit.py",     # F-22 vet/index gate on what sessions load
]

# Files that change how pytest collects or ruff lints without touching a
# test file. pytest.ini / tox.ini / setup.cfg and ruff.toml / .ruff.toml
# take precedence over pyproject.toml when created.
TEST_CONFIG_FILES = [
    "conftest.py",
    "tests/conftest.py",
    "tests/sub/conftest.py",
    "factory/conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "ruff.toml",
    ".ruff.toml",
    "tests/test_profile_review.py",
]

# Merged on main while tasks.json still says in_review (F-28 closeout
# left them unreconciled). Pinned by name: checking every in_review task
# would also catch a fix round's own branch-only test file, and the
# session cannot edit .factory/** to satisfy it.
# tests/test_f22.py: merges while F-22 is still draft (it closes via
# BOOTSTRAP.md + factory approve --reconcile-bootstrap, like F-11/F-12).
MERGED_UNRECONCILED = ["tests/test_f11.py", "tests/test_f12.py",
                       "tests/test_f22.py"]


def _profile() -> dict:
    return yaml.safe_load((REPO / ".factory" / "project.yaml").read_text())


def _protected() -> list:
    return _profile()["paths"]["protected"]


def _hook_denies(path: str) -> bool:
    """The session hook's own (case-insensitive, path-form) matcher."""
    from factory.session import _matches_protected

    return _matches_protected(path, str(REPO), tuple(_protected())) is not None


@pytest.mark.parametrize("path", MORE_JUDGE_MODULES)
def test_every_judge_module_is_protected(path):
    from factory.gates import matches

    assert (REPO / path).is_file(), f"{path} is listed as a judge but missing"
    assert matches(path, _protected()), (
        f"{path} judges or gates agents but is not in paths.protected: a "
        "session could weaken it with no protected_path_touched finding"
    )


@pytest.mark.parametrize("path", TEST_CONFIG_FILES)
def test_conftest_and_test_config_are_protected(path):
    from factory.gates import matches

    assert matches(path, _protected()), (
        f"{path} is not protected: a session could deselect or skip the "
        "protected contract tests through it without touching a test file"
    )
    assert _hook_denies(path), f"the session hook would allow writing {path}"


def test_done_tasks_contract_tests_are_protected():
    """Every contract test file of a done task is protected, read from the
    queue itself. Marking a task done is a human-reviewed tasks.json
    change under .factory/**, so that same PR adds the task's contract
    test here; this test fails it otherwise."""
    from factory.contract import load_tasks
    from factory.gates import contract_test_files, matches

    tasks = load_tasks(REPO / ".factory" / "tasks.json")["tasks"]
    protected = _protected()
    checked = 0
    for task in tasks:
        if task["status"] != "done":
            continue
        for path in sorted(contract_test_files(task)):
            assert (REPO / path).is_file(), (
                f"{task['id']} is done but its contract test {path} is missing"
            )
            assert matches(path, protected), (
                f"{task['id']}'s contract test {path} is not in "
                "paths.protected: a later session could weaken the test "
                "that defines that task's done"
            )
            checked += 1
    assert checked >= 18, f"expected every done task checked, got {checked}"


@pytest.mark.parametrize("path", MERGED_UNRECONCILED)
def test_merged_unreconciled_contract_tests_are_protected(path):
    from factory.gates import matches

    assert (REPO / path).is_file()
    assert matches(path, _protected()), (
        f"{path} is merged on main but not in paths.protected"
    )


@pytest.mark.parametrize(
    "path", ["tests/test_f13.py", "tests/test_f31.py", "tests/test_f99.py"]
)
def test_future_work_orders_can_create_their_contract_tests(path):
    """No glob over new test files: the hook denies creation too."""
    from factory.gates import matches

    assert not matches(path, _protected()), (
        f"{path} matches paths.protected: a future work order could not "
        "write its own red-first contract test"
    )
    assert not _hook_denies(path)


def test_profile_still_validates():
    from factory.contract import load_profile

    load_profile(REPO / ".factory" / "project.yaml")


def test_l1_clean_keeps_the_setup_installed_srt(tmp_path):
    """L1's between-phase clean, driven by the real profile, keeps
    node_modules/.bin/srt (setup's npm ci) and still drops other
    untracked files."""
    from factory.l1 import _clean, _setup_outputs

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True)

    git("init", "-q")
    (tmp_path / ".gitignore").write_text("node_modules/\n__pycache__/\n")
    git("add", ".gitignore")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    srt = tmp_path / "node_modules" / ".bin" / "srt"
    srt.parent.mkdir(parents=True)
    srt.write_text("#!/bin/sh\n")
    stray = tmp_path / "__pycache__" / "x.pyc"
    stray.parent.mkdir()
    stray.write_text("")

    _clean(tmp_path, _setup_outputs(_profile()))

    assert srt.is_file(), "L1's git clean deleted the srt runtime setup installed"
    assert not stray.exists(), "L1's clean must still drop other untracked files"
    assert "node_modules" in _profile()["paths"]["setup_outputs"]
