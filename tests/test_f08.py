"""F-08 acceptance tests — BUILD-ORDER.md, work order F-08.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f08.py::test_ac_N``
means a test function with exactly that name. The extra tests below cover
paths the acceptance refs don't name: refs that don't name a test, a green
phase that still fails, a test committed before its implementation, command
timeouts, template overrides (path/name placeholders, shell braces), a base
that moved past the merge-base, stale bytecode between phases, "not found"
text inside failing output, conftest as test infrastructure, renames across
the test boundary, repo restoration after the gate, and the schema
accepting ``commands.test_ref``.
"""

import subprocess
from pathlib import Path

from factory.gates import ref_path
from factory.l1 import contract_test_refs, run_l1

PROFILE = {"name": "fixture", "commands": {}}

TEST_SOURCE = "def test_answer():\n    from impl import answer\n\n    assert answer() == 42\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


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


def _advance_main(repo: Path, files: dict[str, str], delete: tuple[str, ...] = ()) -> None:
    """Move ``main`` past the branch point after the slice exists."""
    _git(repo, "checkout", "main")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for rel in delete:
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main moves on")
    _git(repo, "checkout", "feature")


def _task(*refs: str) -> dict:
    """A task whose acceptance names each ref as a ``test`` check."""
    return {
        "id": "T-1",
        "risk": [],
        "acceptance": [
            {"id": f"AC-{i}", "text": "it works", "check": {"type": "test", "ref": ref}}
            for i, ref in enumerate(refs, start=1)
        ],
    }


def _red_first_repo(tmp_path: Path, name: str) -> Path:
    """A proper red-first fixture: base fails the test, the diff fixes it."""
    repo = _repo(tmp_path, name, {"impl.py": "def answer():\n    return 0\n"})
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return 42\n",
            "tests/test_feature.py": TEST_SOURCE,
        },
    )
    return repo


def _run(repo: Path, task: dict, profile=None, **kwargs):
    return run_l1(
        profile or dict(PROFILE), repo, "main", "feature", task=task, **kwargs
    )


def _result(report, name):
    return next(r for r in report.command_results if r.name == name)


# --- AC-1: named test already passes on base -> FAIL ("proves nothing") ----


def test_ac_1(tmp_path):
    repo = _repo(tmp_path, "ac1", {"impl.py": "def answer():\n    return 42\n"})
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return 42  # documented\n",
            "tests/test_feature.py": TEST_SOURCE,
        },
    )
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "fail"
    assert any(f.id == "test_proves_nothing" for f in report.findings)
    # The red phase really ran the test on base + the diff's test files,
    # and it passed there — that is the failure being reported.
    assert _result(report, "red:tests/test_feature.py::test_answer").exit_code == 0


# --- AC-2: proper red-first fixture -> PASS --------------------------------


def test_ac_2(tmp_path):
    repo = _red_first_repo(tmp_path, "ac2")
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    assert report.findings == []
    assert _result(report, "red:tests/test_feature.py::test_answer").exit_code != 0
    assert _result(report, "green:tests/test_feature.py::test_answer").exit_code == 0
    as_dict = report.to_dict()
    assert as_dict["layer"] == "L1"
    assert as_dict["verdict"] == "pass"
    assert [c["name"] for c in as_dict["commands"]] == [
        "red:tests/test_feature.py::test_answer",
        "green:tests/test_feature.py::test_answer",
    ]


# --- AC-3: named test doesn't exist -> FAIL with contract_ambiguous --------


def test_ac_3(tmp_path):
    # The named test file is absent from the branch entirely: caught before
    # any run.
    repo = _repo(tmp_path, "ac3a", {"impl.py": "def answer():\n    return 0\n"})
    _change(repo, files={"impl.py": "def answer():\n    return 42\n"})
    report = _run(repo, _task("tests/test_missing.py::test_nothing"))
    assert report.verdict == "fail"
    assert any(f.id == "contract_ambiguous" for f in report.findings)
    assert report.command_results == []

    # The file exists but the named test doesn't: caught at the green phase,
    # where the runner reports the test wasn't found.
    repo_b = _red_first_repo(tmp_path, "ac3b")
    report_b = _run(repo_b, _task("tests/test_feature.py::test_absent"))
    assert report_b.verdict == "fail"
    assert any(f.id == "contract_ambiguous" for f in report_b.findings)
    assert not any(f.id == "test_not_green" for f in report_b.findings)


# --- Extra coverage: paths the acceptance refs don't name ------------------


def test_ref_without_a_test_name_is_ambiguous(tmp_path):
    repo = _red_first_repo(tmp_path, "nocolons")
    report = _run(repo, _task("tests/test_feature.py"))
    assert report.verdict == "fail"
    assert any(
        f.id == "contract_ambiguous" and "tests/test_feature.py" in f.text
        for f in report.findings
    )
    assert report.command_results == []


def test_green_failure_fails(tmp_path):
    """Red is honest but the diff doesn't fix the code: not green."""
    repo = _repo(tmp_path, "notgreen", {"impl.py": "def answer():\n    return 0\n"})
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return 0  # still broken\n",
            "tests/test_feature.py": TEST_SOURCE,
        },
    )
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "fail"
    assert any(f.id == "test_not_green" for f in report.findings)
    assert not any(f.id == "test_proves_nothing" for f in report.findings)


def test_test_committed_before_impl_is_valid_red_first(tmp_path):
    """The test landed in an earlier commit; the diff only fixes the code."""
    repo = _repo(
        tmp_path,
        "tdd",
        {"impl.py": "def answer():\n    return 0\n", "tests/test_feature.py": TEST_SOURCE},
    )
    _change(repo, files={"impl.py": "def answer():\n    return 42\n"})
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    assert report.findings == []


def test_command_timeout_is_a_failure(tmp_path):
    repo = _red_first_repo(tmp_path, "slow")
    profile = {"name": "fixture", "commands": {"test_ref": "sleep 5 # {ref}"}}
    report = _run(
        repo, _task("tests/test_feature.py::test_answer"), profile=profile, timeout_sec=1
    )
    assert report.verdict == "fail"
    assert any(f.id == "command_timeout" for f in report.findings)


def test_custom_test_ref_template(tmp_path):
    repo = _red_first_repo(tmp_path, "custom")
    profile = {
        "name": "fixture",
        "commands": {"test_ref": "{python} -m pytest -q {ref} -p no:cacheprovider"},
    }
    report = _run(repo, _task("tests/test_feature.py::test_answer"), profile=profile)
    assert report.verdict == "pass"
    assert "-p no:cacheprovider" in _result(
        report, "red:tests/test_feature.py::test_answer"
    ).command


def test_template_path_and_name_placeholders(tmp_path):
    """jest-style runners take the file and the test name as separate
    arguments; {path} and {name} exist for them."""
    repo = _red_first_repo(tmp_path, "jestish")
    profile = {
        "name": "fixture",
        "commands": {"test_ref": "{python} -m pytest -q {path} -k {name}"},
    }
    report = _run(repo, _task("tests/test_feature.py::test_answer"), profile=profile)
    assert report.verdict == "pass"
    red = _result(report, "red:tests/test_feature.py::test_answer").command
    assert "tests/test_feature.py" in red and "-k" in red and "test_answer" in red


def test_template_shell_braces_pass_through(tmp_path):
    """${VAR} is shell syntax, not a template placeholder: literal
    replacement means it reaches the command untouched (no GateError)."""
    repo = _red_first_repo(tmp_path, "braces")
    profile = {
        "name": "fixture",
        "commands": {"test_ref": "{python} -m pytest -q {ref} # ${HOME} literal"},
    }
    report = _run(repo, _task("tests/test_feature.py::test_answer"), profile=profile)
    assert report.verdict == "pass"
    assert "${HOME}" in _result(report, "red:tests/test_feature.py::test_answer").command


def test_no_test_checks_is_a_pass_with_nothing_to_prove(tmp_path):
    repo = _repo(tmp_path, "nochecks", {"impl.py": "def answer():\n    return 42\n"})
    _change(repo, files={"impl.py": "def answer():\n    return 42  # doc\n"})
    task = {
        "id": "T-2",
        "risk": [],
        "acceptance": [
            {"id": "AC-1", "text": "builds", "check": {"type": "command", "ref": "make check"}}
        ],
    }
    report = _run(repo, task)
    assert report.verdict == "pass"
    assert report.findings == []
    assert report.command_results == []


# --- Red phase targets the merge-base, not base's tip ----------------------


def test_main_fixing_impl_after_branch_does_not_fake_red(tmp_path):
    """main moved past the branch point and fixed impl itself: red at
    main's tip would pass and falsely report test_proves_nothing. The red
    proof runs at the merge-base the diff was computed against."""
    repo = _red_first_repo(tmp_path, "mbfix")
    _advance_main(repo, files={"impl.py": "def answer():\n    return 42  # fixed on main\n"})
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    assert report.findings == []


def test_parallel_deletion_on_main_does_not_crash_the_red_phase(tmp_path):
    """main deleted (after the branch point) a file the slice also deleted:
    the diff's D must apply against the merge-base tree, where the file
    still exists."""
    repo = _repo(
        tmp_path,
        "mbdel",
        {
            "impl.py": "def answer():\n    return 0\n",
            "tests/test_old.py": "def test_old():\n    assert True\n",
        },
    )
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return 42\n",
            "tests/test_feature.py": TEST_SOURCE,
        },
        delete=("tests/test_old.py",),
    )
    _advance_main(repo, files={}, delete=("tests/test_old.py",))
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    assert report.findings == []


# --- Phase isolation --------------------------------------------------------


def test_red_bytecode_cannot_leak_into_green(tmp_path):
    """A same-size source edit is exactly the case where Python's
    mtime+size check would reuse base's .pyc: the phases are cleaned
    between runs, and nothing is left in the tree afterwards."""
    repo = _repo(tmp_path, "pyc", {"impl.py": "def answer():\n    return 40 + 1\n"})
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return 41 + 1\n",  # same size as base
            "tests/test_feature.py": TEST_SOURCE,
        },
    )
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    leftovers = [
        p.name for p in repo.rglob("*") if p.name in ("__pycache__", ".pytest_cache")
    ]
    assert leftovers == []


def test_not_found_text_in_failure_output_is_not_ambiguous(tmp_path):
    """A real failure whose output says 'Not Found' is a worker problem
    (test_not_green), not a missing test (contract_ambiguous)."""
    repo = _repo(tmp_path, "nf", {"impl.py": "def answer():\n    return '404 Not Found'\n"})
    test = (
        "def test_answer():\n"
        "    from impl import answer\n\n"
        "    assert answer() == 'ok'\n"
    )
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return '404 Not Found: /x'\n",
            "tests/test_feature.py": test,
        },
    )
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "fail"
    assert any(f.id == "test_not_green" for f in report.findings)
    assert not any(f.id == "contract_ambiguous" for f in report.findings)


# --- Test-infrastructure and rename handling in the red phase --------------


def test_diff_conftest_applies_in_red_phase(tmp_path):
    """A conftest.py shipped by the diff is test infrastructure: red
    applies it, so a test that would pass on base (given the fixture) is
    caught as proving nothing instead of reading as a red fixture error."""
    repo = _repo(tmp_path, "conf", {"impl.py": "def answer():\n    return 42\n"})
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return 42  # documented\n",
            "conftest.py": "import pytest\n\n\n@pytest.fixture\ndef seed():\n    return 1\n",
            "tests/test_feature.py": (
                "def test_answer(seed):\n"
                "    from impl import answer\n\n"
                "    assert seed + answer() == 43\n"
            ),
        },
    )
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "fail"
    assert any(f.id == "test_proves_nothing" for f in report.findings)


def test_rename_out_of_tests_dir_does_not_leak_impl_into_red(tmp_path):
    """tests/magic.py -> src/magic.py: only the test half applies in red.
    Checking head's implementation file out into red would make the test
    pass there and mask the proof with a false test_proves_nothing."""
    repo = _repo(
        tmp_path,
        "moveout",
        {
            "impl.py": "def answer():\n    return 0\n",
            "tests/magic.py": "def answer():\n    return 0\n",
        },
    )
    _git(repo, "checkout", "-b", "feature")
    (repo / "src").mkdir()
    _git(repo, "mv", "tests/magic.py", "src/magic.py")
    (repo / "src" / "magic.py").write_text("def answer():\n    return 42\n")
    (repo / "tests" / "test_feature.py").write_text(
        "from src.magic import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    assert report.findings == []


def test_rename_into_tests_dir_keeps_base_implementation(tmp_path):
    """src/keep.py -> tests/keep.py: red checks out the new test file but
    must NOT delete the base implementation file the rename moved away."""
    repo = _repo(
        tmp_path,
        "movein",
        {
            "impl.py": "def answer():\n    return 0\n",
            "src/keep.py": "tag = 1\n",
        },
    )
    _change(
        repo,
        rename=(("src/keep.py", "tests/keep.py"),),
        files={
            "impl.py": "def answer():\n    return 42\n",
            "tests/test_feature.py": TEST_SOURCE,
        },
    )
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    assert report.findings == []


def test_deleted_and_renamed_test_files_apply_in_red_phase(tmp_path):
    """The red phase stages the diff's test-file side: deletions really
    delete, test-to-test renames really move; neither crashes the gate."""
    repo = _repo(
        tmp_path,
        "applytests",
        {
            "impl.py": "def answer():\n    return 0\n",
            "tests/test_old.py": "def test_old():\n    assert True\n",
            "tests/test_seed.py": "def test_seed():\n    assert True\n",
        },
    )
    _change(
        repo,
        files={
            "impl.py": "def answer():\n    return 42\n",
            "tests/test_feature.py": TEST_SOURCE,
        },
        delete=("tests/test_old.py",),
        rename=(("tests/test_seed.py", "tests/test_renamed.py"),),
    )
    report = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert report.verdict == "pass"
    assert report.findings == []


def test_repo_left_on_slice_branch_after_run(tmp_path):
    repo = _red_first_repo(tmp_path, "restore")
    passing = _run(repo, _task("tests/test_feature.py::test_answer"))
    assert passing.verdict == "pass"
    assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert _git_out(repo, "status", "--porcelain", "-uno") == ""

    # Also after a FAIL verdict, the checkout comes back clean.
    failing = _run(repo, _task("tests/test_feature.py::test_absent"))
    assert failing.verdict == "fail"
    assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert _git_out(repo, "status", "--porcelain", "-uno") == ""


# --- Pure-function units and schema ----------------------------------------


def test_contract_test_refs_ordered_and_deduped():
    task = _task(
        "tests/test_a.py::test_x",
        "tests/test_a.py::test_x",
        "tests/test_b.py::test_y",
    )
    task["acceptance"].append(
        {"id": "AC-9", "text": "builds", "check": {"type": "command", "ref": "make check"}}
    )
    assert contract_test_refs(task) == [
        "tests/test_a.py::test_x",
        "tests/test_b.py::test_y",
    ]
    assert contract_test_refs({}) == []


def test_ref_path_strips_the_file_half():
    assert ref_path("tests/test_x.py::test_a") == "tests/test_x.py"
    assert ref_path("tests/test_x.py ::test_a") == "tests/test_x.py"
    assert ref_path("tests/test_x.py") is None


def test_schema_accepts_test_ref(tmp_path):
    from factory.contract import load_profile

    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "name: fixture\n"
        "repo:\n"
        "  path: /tmp/fixture\n"
        "  default_branch: main\n"
        "  remote: https://example.com/fixture.git\n"
        "commands:\n"
        "  setup: 'true'\n"
        "  test: pytest -q\n"
        "  test_ref: '{python} -m pytest -q {ref}'\n"
        "paths:\n"
        "  risk: {}\n"
        "  protected: []\n"
        "deploy:\n"
        "  mechanism: none\n"
        "  auto_on_merge: false\n"
        "live_checks: {}\n"
        "docs_allowlist: []\n"
    )
    loaded = load_profile(profile)
    assert loaded["commands"]["test_ref"] == "{python} -m pytest -q {ref}"


# --- rehearsal blocker (JobFinderOS readiness, 2026-10-18) --------------------


def test_rehearsal_clean_keeps_declared_setup_outputs(tmp_path):
    """L1's inter-phase `git clean -fdx` used to delete what setup installed
    (backend/.venv, frontend/node_modules), so every ref failed with exit
    127 and a healthy test read as test_not_green. Directories the profile
    declares as setup outputs survive the clean; everything else untracked
    is still removed."""
    import sys as _sys

    repo = _red_first_repo(tmp_path, "setup-outputs")
    deps = repo / ".deps"
    deps.mkdir()
    shim = deps / "py"
    shim.write_text(f'#!/bin/sh\nexec "{_sys.executable}" -m pytest -p no:cacheprovider "$@"\n')
    shim.chmod(0o755)
    profile = {
        "name": "fixture",
        "commands": {"test_ref": "./.deps/py -q {ref}"},
        "paths": {"setup_outputs": [".deps"]},
    }
    report = _run(repo, _task("tests/test_feature.py::test_answer"), profile=profile)
    green = _result(report, "green:tests/test_feature.py::test_answer")
    assert green.exit_code == 0, green.output[-500:]
    assert report.verdict == "pass", [f.id for f in report.findings]
    assert shim.exists(), "a declared setup output was cleaned away"
