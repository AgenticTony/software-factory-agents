"""F-07 acceptance tests — BUILD-ORDER.md, work order F-07.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f07.py::test_ac_N``
means a test function with exactly that name. The extra descriptive tests
below cover paths the acceptance refs don't name: deleted protected files,
contract test files as protected, declared risk flags, rename handling,
JS-only skip markers, removed assertions, unparsable failing output,
test-count floors, setup failure, command timeouts, tsc-style error
parsing and a missing baseline file.
"""

import json
import subprocess
from pathlib import Path

import pytest

from factory.l0 import (
    GateError,
    contract_test_files,
    diff_file_changes,
    parse_errors,
    parse_test_count,
    repo_owned,
    run_l0,
)

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


# --- AC-1: modified protected file -> FAIL with protected_path_touched ---


def test_ac_1(tmp_path):
    repo = _repo(tmp_path, "ac1", {"schema/tasks.schema.json": "{}"})
    _change(repo, files={"schema/tasks.schema.json": '{"x": 1}'})
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": {}, "protected": ["schema/**"]}
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert "protected_path_touched" in report.escalations
    assert any(
        f.id == "protected_path_touched" and f.file == "schema/tasks.schema.json"
        for f in report.findings
    )


# --- AC-2: changed dependency manifest -> escalation new_dependency --------


def test_ac_2(tmp_path):
    repo = _repo(tmp_path, "ac2", {"pyproject.toml": "[project]\n"})
    _change(repo, files={"pyproject.toml": "[project]\ndep = 'x'\n"})
    profile = dict(BASE_PROFILE)
    profile["dependency_manifests"] = ["pyproject.toml"]
    report = _run(repo, profile)
    assert "new_dependency" in report.escalations
    assert report.verdict == "pass"
    assert not report.findings


# --- AC-3: undeclared touched risk path -> flag added + escalation ---------


def test_ac_3(tmp_path):
    repo = _repo(tmp_path, "ac3", {"src/pay/charge.py": "x = 1\n"})
    _change(repo, files={"src/pay/charge.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": {"payments": ["src/pay/**"]}, "protected": []}
    report = _run(repo, profile)
    assert report.risk_flags_added == ["payments"]
    assert "risk_path_touched" in report.escalations
    assert report.verdict == "pass"


# --- AC-4: deleted test, or added skip marker -> FAIL ----------------------


def test_ac_4(tmp_path):
    repo_a = _repo(
        tmp_path, "ac4a", {"tests/test_alpha.py": "def test_a():\n    assert True\n"}
    )
    _change(repo_a, delete=("tests/test_alpha.py",))
    report_a = _run(repo_a)
    assert report_a.verdict == "fail"
    assert any(f.id == "test_deleted" for f in report_a.findings)

    repo_b = _repo(
        tmp_path, "ac4b", {"tests/test_beta.py": "def test_b():\n    assert True\n"}
    )
    _change(
        repo_b,
        files={
            "tests/test_beta.py": (
                "import pytest\n\n\n@pytest.mark.skip(reason='later')\n"
                "def test_b():\n    assert True\n"
            )
        },
    )
    report_b = _run(repo_b)
    assert report_b.verdict == "fail"
    assert any(f.id == "skip_marker_added" for f in report_b.findings)


# --- AC-5: baseline file:line precision rule -------------------------------


def test_ac_5(tmp_path):
    repo = _repo(tmp_path, "ac5", {"lib/foo.py": "x = 1\n"})
    _change(repo, files={"lib/foo.py": "x = 2\n"})

    both = "printf 'lib/foo.py:10:1: E999 pre-existing\\nlib/foo.py:20:1: E999 fresh\\n'; exit 1"
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "lint": both, "test": "true"}
    report = _run(repo, profile, baseline={"known_errors": {"lib/foo.py": [10]}})
    assert report.verdict == "fail"
    new = [f for f in report.findings if f.id == "new_error"]
    assert [(f.file, f.line) for f in new] == [("lib/foo.py", 20)]
    assert any(
        "lib/foo.py:10" in note and "pre-existing" in note
        for note in report.preexisting_notes
    )

    only_known = "printf 'lib/foo.py:10:1: E999 pre-existing\\n'; exit 1"
    profile_ok = dict(BASE_PROFILE)
    profile_ok["commands"] = {"setup": "true", "lint": only_known, "test": "true"}
    report_ok = _run(repo, profile_ok, baseline={"known_errors": {"lib/foo.py": [10]}})
    assert report_ok.verdict == "pass"


# --- Extra coverage: paths the acceptance refs don't name ------------------


def test_deleted_protected_file_fails(tmp_path):
    repo = _repo(tmp_path, "delprot", {"schema/tasks.schema.json": "{}"})
    _change(repo, delete=("schema/tasks.schema.json",))
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": {}, "protected": ["schema/**"]}
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert "protected_path_touched" in report.escalations


def test_rename_out_of_protected_path_fails(tmp_path):
    repo = _repo(tmp_path, "renprot", {"schema/keep.json": '{"a": 1}\n'})
    _change(repo, rename=(("schema/keep.json", "elsewhere/keep.json"),))
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": {}, "protected": ["schema/**"]}
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert "protected_path_touched" in report.escalations


def test_contract_test_file_is_protected(tmp_path):
    """The task's named test files are locked at approval (VALIDATOR-SPEC L0)."""
    repo = _repo(tmp_path, "ctest", {"tests/test_thing.py": "def test_t():\n    assert 1\n"})
    _change(repo, files={"tests/test_thing.py": "def test_t():\n    assert 2\n"})
    task = {
        "id": "T-9",
        "risk": [],
        "acceptance": [
            {
                "id": "AC-1",
                "text": "it works",
                "check": {"type": "test", "ref": "tests/test_thing.py::test_t"},
            }
        ],
    }
    report = _run(repo, task=task)
    assert report.verdict == "fail"
    assert "protected_path_touched" in report.escalations


def test_declared_risk_flag_does_not_escalate(tmp_path):
    repo = _repo(tmp_path, "riskknown", {"src/pay/charge.py": "x = 1\n"})
    _change(repo, files={"src/pay/charge.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": {"payments": ["src/pay/**"]}, "protected": []}
    report = _run(repo, profile, task={"id": "T", "risk": ["payments"]})
    assert report.risk_flags_added == []
    assert "risk_path_touched" not in report.escalations
    assert report.verdict == "pass"


def test_js_only_and_pending_markers_fail(tmp_path):
    repo = _repo(
        tmp_path,
        "jsonly",
        {"src/app.test.ts": "it('works', () => {\n  expect(1).toBe(1);\n});\n"},
    )
    _change(
        repo,
        files={
            "src/app.test.ts": (
                "it.only('works', () => {\n  expect(1).toBe(1);\n});\n"
                "xit('broken', () => {\n  expect(1).toBe(2);\n});\n"
            )
        },
    )
    report = _run(repo)
    assert report.verdict == "fail"
    assert any(f.id == "skip_marker_added" for f in report.findings)


def test_removed_assertion_fails(tmp_path):
    repo = _repo(
        tmp_path,
        "delassert",
        {"tests/test_gone.py": "def test_g():\n    assert 1 == 1\n    print('x')\n"},
    )
    _change(repo, files={"tests/test_gone.py": "def test_g():\n    print('x')\n"})
    report = _run(repo)
    assert report.verdict == "fail"
    assert any(f.id == "assertion_removed" for f in report.findings)


def test_unparsable_failing_output_fails(tmp_path):
    repo = _repo(tmp_path, "opaque", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {
        "setup": "true",
        "build": "echo 'something exploded spectacularly'; exit 3",
        "test": "true",
    }
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert any(f.id == "command_failed" for f in report.findings)


def test_tsc_style_errors_are_parsed(tmp_path):
    repo = _repo(tmp_path, "tsc", {"src/x.ts": "const a = 1\n"})
    _change(repo, files={"src/x.ts": "const a = 2\n"})
    lint = "printf 'src/x.ts(12,5): error TS2322: bad type\\n'; exit 1"
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "typecheck": lint, "test": "true"}
    report = _run(repo, profile, baseline={"known_errors": {"src/x.ts": [12]}})
    assert report.verdict == "pass"
    report_new = _run(repo, profile)
    assert report_new.verdict == "fail"
    assert any(
        f.id == "new_error" and f.file == "src/x.ts" and f.line == 12
        for f in report_new.findings
    )


def test_test_count_floor(tmp_path):
    repo = _repo(tmp_path, "count", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "echo '3 passed in 0.01s'"}
    dropped = _run(repo, profile, baseline={"min_tests": 5})
    assert dropped.verdict == "fail"
    assert any(f.id == "test_count_dropped" for f in dropped.findings)
    met = _run(repo, profile, baseline={"min_tests": 3})
    assert met.verdict == "pass"


def test_setup_failure_skips_remaining_gates(tmp_path):
    repo = _repo(tmp_path, "setupfail", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "exit 1", "test": "true"}
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert any(f.id == "setup_failed" for f in report.findings)
    assert [r.name for r in report.command_results] == ["setup"]


def test_command_timeout_fails(tmp_path):
    repo = _repo(tmp_path, "slow", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "sleep 5"}
    report = _run(repo, profile, timeout_sec=1)
    assert report.verdict == "fail"
    assert any(f.id == "command_timeout" for f in report.findings)


def test_missing_baseline_file_fails(tmp_path):
    repo = _repo(tmp_path, "nobaseline", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["baselines"] = "baselines.json"
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert any(f.id == "baseline_missing" for f in report.findings)


def test_malformed_baseline_structure_fails(tmp_path):
    """A baseline we can't trust exempts nothing and doesn't crash."""
    repo = _repo(tmp_path, "badbaseline", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    lint = "printf 'lib/a.py:3:1: E999 error\\n'; exit 1"
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "lint": lint, "test": "echo 1 passed"}
    report = _run(
        repo, profile, baseline={"known_errors": "nope", "min_tests": "many"}
    )
    assert report.verdict == "fail"
    assert any(f.id == "baseline_invalid" for f in report.findings)
    # No exemption applied: the reported error is new.
    assert any(f.id == "new_error" and f.file == "lib/a.py" for f in report.findings)


def test_clean_diff_green_commands_pass(tmp_path):
    base_test = "def test_a():\n    assert True\n"
    repo = _repo(tmp_path, "clean", {"lib/a.py": "x = 1\n", "tests/test_a.py": base_test})
    _change(repo, files={"lib/a.py": "x = 2\n", "tests/test_a.py": base_test + "    assert x\n"})
    report = _run(repo)
    assert report.verdict == "pass"
    assert report.findings == []
    assert report.escalations == []
    assert [r.name for r in report.command_results] == ["setup", "test"]


# --- Pure-function units ----------------------------------------------------


def test_contract_test_files_extracts_paths():
    task = {
        "acceptance": [
            {"check": {"type": "test", "ref": "tests/test_f07.py::test_ac_1"}},
            {"check": {"type": "command", "ref": "make check"}},
            {"check": {"type": "test", "ref": "tests/test_extra.py::test_x"}},
        ]
    }
    assert contract_test_files(task) == {"tests/test_f07.py", "tests/test_extra.py"}
    assert contract_test_files({}) == set()


def test_parse_errors_and_test_count():
    out = (
        "lib/a.py:10:1: E999 first\n"
        "src/x.ts(12,5): error TS2322: bad\n"
        "noise without location\n"
        "=== 46 passed, 2 failed in 0.1s ===\n"
    )
    assert parse_errors(out) == [
        ("lib/a.py", 10, "E999 first"),
        ("src/x.ts", 12, "TS2322: bad"),
    ]
    assert parse_test_count(out) == 46
    assert parse_test_count("no counts here") is None


# --- INC-005: findings must name the repo's line, with its message ------------


def test_parse_errors_carries_the_diagnostic_message():
    """The message after the location reaches the finding text, so a fix
    prompt says *what* is wrong at the line, not only where."""
    out = (
        "tests/test_f11.py:59:41: SyntaxError: Expected one or more "
        "keyword parameter after '*' separator\n"
        "bare/location.py:7: \n"
    )
    assert parse_errors(out) == [
        ("bare/location.py", 7, ""),
        ("tests/test_f11.py", 59,
         "SyntaxError: Expected one or more keyword parameter after '*' separator"),
    ]


def test_repo_owned_rejects_framework_locations():
    """A location is repo-owned only when the repo's baseline can speak
    about it. Interpreter, stdlib and installed-package frames — the
    sandbox HOME scratch user-site, the framework Python, anything
    absolute outside the repo, parent escapes, frozen modules — are
    someone else's code (INC-005)."""
    assert repo_owned("tests/test_f11.py")
    assert repo_owned("factory/l0.py")
    assert not repo_owned(
        ".home/Library/Python/3.12/lib/python/site-packages/_pytest/python.py")
    assert not repo_owned(
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/pytest")
    assert not repo_owned(
        "/Library/Frameworks/Python.framework/Versions/3.12/lib/"
        "python3.12/importlib/__init__.py")
    assert not repo_owned("../outside/the/repo.py")
    assert not repo_owned("<frozen importlib._bootstrap>")
    assert not repo_owned("venv/lib/site-packages/pkg/mod.py")


def test_l0_new_error_findings_name_the_repo_line_with_its_message(tmp_path):
    """The incident end to end: a failing lint+test whose output carries
    framework traceback frames plus one repo line produces exactly one
    new_error finding — the repo line, message included — and no
    finding mentions a framework path."""
    from factory.gates import CommandResult

    repo = _repo(tmp_path, "inc005", {"tests/test_f11.py": "def ok(): pass\n"})
    _change(repo, files={"tests/test_f11.py": "def broken(:\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {
        "setup": "true",
        "lint": "ruff check .",
        "test": "pytest -q",
    }
    lint_out = (
        "tests/test_f11.py:59:41: SyntaxError: Expected one or more "
        "keyword parameter after '*' separator\n"
        "   |\n"
        "59 |     def _task(task_id: str, status: str, *, **overrides) -> dict:\n"
    )
    test_out = (
        "==================================== ERRORS ============================\n"
        "____________________ ERROR collecting tests/test_f11.py ________________\n"
        ".home/Library/Python/3.12/lib/python/site-packages/_pytest/python.py:508:"
        " in importtestmodule\n"
        ".home/Library/Python/3.12/lib/python/site-packages/_pytest/pathlib.py:596:"
        " in import_path\n"
        "/Library/Frameworks/Python.framework/Versions/3.12/lib/python3.12/"
        "importlib/__init__.py:90: in import_module\n"
        "/Library/Frameworks/Python.framework/Versions/3.12/lib/python3.12/"
        "ast.py:52: in parse\n"
        "E   SyntaxError: invalid syntax\n"
        "ERROR tests/test_f11.py\n"
    )

    def fake_runner(name, command, repo_path, timeout_sec):
        if name == "setup":
            return CommandResult(name, command, 0, "")
        outputs = {"lint": lint_out, "test": test_out}
        return CommandResult(name, command, 1, outputs[name])

    report = _run(repo, profile, runner=fake_runner)

    assert report.verdict == "fail"
    new_errors = [f for f in report.findings if f.id == "new_error"]
    assert [(f.file, f.line) for f in new_errors] == [("tests/test_f11.py", 59)]
    assert "SyntaxError" in new_errors[0].text
    assert "keyword parameter after '*' separator" in new_errors[0].text
    for f in report.findings:
        assert "site-packages" not in f.text, f.text
        assert "/Library/" not in f.text, f.text
        assert ".home/" not in f.text, f.text


def test_diff_file_changes_reads_git(tmp_path):
    repo = _repo(
        tmp_path,
        "diffpure",
        {"a.py": "one\n", "tests/test_b.py": "t\n", "gone.py": "g\n"},
    )
    _change(
        repo,
        files={"a.py": "two\n", "new.py": "n\n"},
        delete=("gone.py",),
        rename=(("tests/test_b.py", "tests/test_renamed.py"),),
    )
    changes = {c.path: c for c in diff_file_changes(repo, "main", "feature")}
    assert set(changes) == {"a.py", "new.py", "gone.py", "tests/test_renamed.py"}
    assert changes["a.py"].status == "M"
    assert [text for _, text in changes["a.py"].added_lines] == ["two"]
    assert [text for _, text in changes["a.py"].removed_lines] == ["one"]
    assert changes["new.py"].status == "A"
    assert changes["gone.py"].status == "D"
    # Deleted files have no b-side content: only removals, no phantom
    # "+++ /dev/null" added line.
    assert changes["gone.py"].added_lines == []
    assert [text for _, text in changes["gone.py"].removed_lines] == ["g"]
    renamed = changes["tests/test_renamed.py"]
    assert renamed.status == "R"
    assert renamed.old_path == "tests/test_b.py"


# --- Review fixes (PR #8 review) -------------------------------------------


def test_timeout_with_partial_stdout_does_not_crash(tmp_path):
    """POSIX TimeoutExpired carries raw bytes stdout and None stderr."""
    repo = _repo(tmp_path, "slowout", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "echo partial; sleep 5"}
    report = _run(repo, profile, timeout_sec=1)
    assert report.verdict == "fail"
    assert any(f.id == "command_timeout" for f in report.findings)
    test_result = next(r for r in report.command_results if r.name == "test")
    assert "partial" in test_result.output


def test_nested_test_dir_is_recognised(tmp_path):
    """Monorepo layouts (backend/tests/...) are test files too."""
    repo = _repo(
        tmp_path,
        "nested",
        {
            "backend/tests/test_api.py": "def test_a():\n    assert True\n",
            "backend/tests/test_gone.py": "def test_g():\n    assert True\n",
            "pkg/sub/test_colocated.py": "def test_c():\n    assert True\n",
        },
    )
    _change(
        repo,
        files={
            "backend/tests/test_api.py": (
                "import pytest\n\n\n@pytest.mark.skip(reason='later')\n"
                "def test_a():\n    assert True\n"
            ),
            "pkg/sub/test_colocated.py": "def test_c():\n    pass\n",
        },
        delete=("backend/tests/test_gone.py",),
    )
    report = _run(repo)
    ids = {(f.id, f.file) for f in report.findings}
    assert ("test_deleted", "backend/tests/test_gone.py") in ids
    assert ("skip_marker_added", "backend/tests/test_api.py") in ids
    assert ("assertion_removed", "pkg/sub/test_colocated.py") in ids


def test_diff_line_numbers_count_context_lines(tmp_path):
    base = "a\nb\nc\n    assert x\ne\n"
    repo = _repo(tmp_path, "ctx", {"tests/test_ctx.py": base})
    _change(repo, files={"tests/test_ctx.py": "a\nb\nc\n    pytest.skip()\ne\n"})
    (change,) = diff_file_changes(repo, "main", "feature")
    assert change.added_lines == [(4, "    pytest.skip()")]
    assert change.removed_lines == [(4, "    assert x")]


def test_baseline_is_read_from_base_not_head(tmp_path):
    """A worker can't exempt its own errors by editing the baseline file."""
    base_line = json.dumps({"known_errors": {"lib/foo.py": [10]}})
    repo = _repo(tmp_path, "basefile", {"lib/foo.py": "x = 1\n", "baselines.json": base_line})
    widened = json.dumps({"known_errors": {"lib/foo.py": [10, 20]}})
    _change(repo, files={"lib/foo.py": "x = 2\n", "baselines.json": widened})
    lint = "printf 'lib/foo.py:10:1: E1 old\\nlib/foo.py:20:1: E2 new\\n'; exit 1"
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "lint": lint, "test": "true"}
    profile["baselines"] = "baselines.json"
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert [(f.file, f.line) for f in report.findings if f.id == "new_error"] == [
        ("lib/foo.py", 20)
    ]


def test_baselined_location_cannot_mask_unattributed_failure(tmp_path):
    """A red command with an unparsed failure FAILs even if every parsed hit is known."""
    repo = _repo(tmp_path, "mask", {"lib/foo.py": "x = 1\n"})
    _change(repo, files={"lib/foo.py": "x = 2\n"})
    test_cmd = (
        "printf 'lib/foo.py:10: AssertionError\\n"
        "FAILED tests/test_new.py::test_n - assert 1 == 2\\n"
        "1 failed, 4 passed in 0.1s\\n'; exit 1"
    )
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": test_cmd}
    report = _run(repo, profile, baseline={"known_errors": {"lib/foo.py": [10]}})
    assert report.verdict == "fail"
    assert any(f.id == "command_failed" for f in report.findings)


# --- Deferred review fixes (PR #8 review, findings 6-10) -------------------


def test_cwd_relative_error_paths_match_repo_baseline(tmp_path):
    """``cd backend && ...`` reports backend-relative paths; the baseline is repo-relative."""
    repo = _repo(tmp_path, "cwd", {"backend/app/x.py": "x = 1\n", "lib/foo.py": "y = 1\n"})
    _change(repo, files={"backend/app/x.py": "x = 2\n"})
    lint = (
        "cd backend && printf 'app/x.py:3:1: F401 old\\napp/x.py:4:1: F401 new\\n'; exit 1"
    )
    root = "printf './lib/foo.py:7:1: E1 old\\n'; exit 1"
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "lint": lint, "typecheck": root, "test": "true"}
    report = _run(
        repo,
        profile,
        baseline={"known_errors": {"backend/app/x.py": [3], "lib/foo.py": [7]}},
    )
    assert [(f.file, f.line) for f in report.findings if f.id == "new_error"] == [
        ("backend/app/x.py", 4)
    ]
    assert any("backend/app/x.py:3" in n for n in report.preexisting_notes)
    assert any("lib/foo.py:7" in n for n in report.preexisting_notes)


def test_soloadventurer_baseline_schema_is_supported(tmp_path):
    """SoloAdventurer's real keys: known_typecheck_errors.files + jest.tests."""
    baseline = {
        "jest": {"tests": 5, "suites": 2, "command": "npm test -- --ci"},
        "known_typecheck_errors": {"count": 1, "files": {"src/x.ts": [12]}},
        "ci_status": {"overall": "GREEN"},
    }
    repo = _repo(
        tmp_path,
        "solo",
        {"src/x.ts": "const a = 1\n", ".claude/state/baselines.json": json.dumps(baseline)},
    )
    _change(repo, files={"src/x.ts": "const a = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["baselines"] = ".claude/state/baselines.json"
    profile["commands"] = {
        "setup": "true",
        "typecheck": "printf 'src/x.ts(12,5): error TS2322: old\\n'; exit 1",
        "test": "echo 'Tests: 3 passed, 3 total'",
    }
    report = _run(repo, profile)
    ids = {f.id for f in report.findings}
    assert "new_error" not in ids
    assert "baseline_invalid" not in ids
    assert "test_count_dropped" in ids
    assert any("src/x.ts:12" in n for n in report.preexisting_notes)


def test_baseline_without_recognised_keys_fails(tmp_path):
    repo = _repo(
        tmp_path,
        "foreign",
        {"lib/a.py": "x = 1\n", "baselines.json": json.dumps({"ci_status": {"x": 1}})},
    )
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["baselines"] = "baselines.json"
    report = _run(repo, profile)
    assert report.verdict == "fail"
    assert any(f.id == "baseline_invalid" for f in report.findings)


def test_unreadable_test_count_does_not_skip_floor(tmp_path):
    repo = _repo(tmp_path, "nocount", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "echo all good"}
    report = _run(repo, profile, baseline={"min_tests": 5})
    assert report.verdict == "fail"
    assert any(f.id == "test_count_unknown" for f in report.findings)


@pytest.mark.parametrize(
    "baseline",
    [
        {"known_errors": {"lib/a.py": ["ten"]}},
        {"known_errors": {"lib/a.py": [True]}},
        {"known_errors": {"lib/a.py": [3.5]}},
        {"min_tests": True},
        {"min_tests": 4.5},
        {"known_typecheck_errors": {"files": {"lib/a.py": ["x"]}}},
        {"jest": {"tests": "many"}},
    ],
)
def test_bad_baseline_entries_are_invalid(tmp_path, baseline):
    repo = _repo(tmp_path, "badentries", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "echo '9 passed'"}
    report = _run(repo, profile, baseline=baseline)
    assert report.verdict == "fail"
    assert any(f.id == "baseline_invalid" for f in report.findings)


def test_checkout_not_at_head_ref_raises(tmp_path):
    repo = _repo(tmp_path, "wronghead", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    _git(repo, "checkout", "main")
    with pytest.raises(GateError, match="head_ref"):
        _run(repo)


def test_dirty_working_tree_raises(tmp_path):
    repo = _repo(tmp_path, "dirty", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    (repo / "stray.txt").write_text("uncommitted\n")
    with pytest.raises(GateError, match="clean"):
        _run(repo)


def test_rename_test_to_non_test_path_is_deleted(tmp_path):
    repo = _repo(
        tmp_path, "renout", {"tests/test_keep.py": "def test_k():\n    assert True\n"}
    )
    _change(repo, rename=(("tests/test_keep.py", "lib/keep_helpers.py"),))
    report = _run(repo)
    assert report.verdict == "fail"
    assert any(
        f.id == "test_deleted" and f.file == "tests/test_keep.py" for f in report.findings
    )
