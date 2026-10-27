"""Review fixes for factory/l0.py (prs-50-62 review, PR #54 finding 1).

A command that exits non-zero is a failure unless the baseline explains
it. ``repo_owned`` drops framework/foreign locations from new_error
findings (INC-005), but when that filter removes *every* parsed location
and no failure marker fired, nothing explains the non-zero exit: it must
still produce ``command_failed``, never a pass (fail-closed).
"""

from test_f07 import BASE_PROFILE, _change, _repo, _run

from factory.gates import CommandResult


def _profile(**commands):
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "true", **commands}
    return profile


def _runner(outputs: dict[str, tuple[int, str]]):
    def fake_runner(name, command, repo_path, timeout_sec):
        code, out = outputs.get(name, (0, ""))
        return CommandResult(name, command, code, out)

    return fake_runner


def _repo_with_change(tmp_path, name):
    repo = _repo(tmp_path, name, {"lib/foo.py": "x = 1\n"})
    _change(repo, files={"lib/foo.py": "x = 2\n"})
    return repo


def test_failed_command_with_only_foreign_absolute_location_fails(tmp_path):
    """Lint exits 1 printing only an absolute stdlib/site-packages
    location: every parsed location is non-repo, no marker fires."""
    repo = _repo_with_change(tmp_path, "foreign")
    out = "/usr/lib/python3.12/site-packages/pkg/plugin.py:12: UserWarning: boom\n"
    report = _run(
        repo,
        _profile(lint="ruff check ."),
        runner=_runner({"lint": (1, out)}),
    )
    assert report.verdict == "fail"
    failed = [f for f in report.findings if f.id == "command_failed"]
    assert len(failed) == 1, report.findings
    assert failed[0].text.startswith("lint exited 1")
    assert "UserWarning: boom" in failed[0].text
    # The framework location is still never a new_error (INC-005).
    assert not [f for f in report.findings if f.id == "new_error"]


def test_failed_typecheck_with_only_site_packages_location_fails(tmp_path):
    """mypy exits 1 on a stub inside .venv site-packages; 'error:' is
    mid-line and 'Found 1 error in 1 file' matches no marker."""
    repo = _repo_with_change(tmp_path, "venv")
    out = (
        ".venv/lib/python3.12/site-packages/foo/__init__.pyi:3: error: "
        'Name "Bar" is not defined  [name-defined]\n'
        "Found 1 error in 1 file (checked 4 source files)\n"
    )
    report = _run(
        repo,
        _profile(typecheck="mypy ."),
        runner=_runner({"typecheck": (1, out)}),
    )
    assert report.verdict == "fail"
    assert [f.id for f in report.findings] == ["command_failed"]
    assert report.findings[0].text.startswith("typecheck exited 1")


def test_failed_command_with_only_home_and_parent_escape_locations_fails(tmp_path):
    """The sandbox HOME scratch and '..' escapes are the same non-repo
    class: filtered from new_error, but the exit still fails."""
    repo = _repo_with_change(tmp_path, "home")
    out = (
        ".home/Library/Python/3.12/lib/python/site-packages/x.py:5: warn\n"
        "../elsewhere/mod.py:9: boom\n"
    )
    report = _run(
        repo,
        _profile(lint="ruff check ."),
        runner=_runner({"lint": (1, out)}),
    )
    assert report.verdict == "fail"
    assert [f.id for f in report.findings] == ["command_failed"]


def test_baselined_repo_error_with_framework_frames_still_passes(tmp_path):
    """Control: the fallback must not weaken baselines. A failed lint
    whose only repo-owned location is baselined (plus framework noise)
    is explained by the baseline and passes, as before."""
    repo = _repo_with_change(tmp_path, "baselined")
    out = (
        "lib/foo.py:10:1: E501 line too long\n"
        "/usr/lib/python3.12/site-packages/pkg/plugin.py:12: UserWarning: boom\n"
    )
    report = _run(
        repo,
        _profile(lint="ruff check ."),
        baseline={"known_errors": {"lib/foo.py": [10]}},
        runner=_runner({"lint": (1, out)}),
    )
    assert report.verdict == "pass", report.findings
    assert any("lib/foo.py:10" in n for n in report.preexisting_notes)


def test_foreign_only_with_marker_reports_one_command_failed(tmp_path):
    """When a failure marker already fires, the fallback does not add a
    second command_failed for the same command."""
    repo = _repo_with_change(tmp_path, "marker")
    out = (
        "/usr/lib/python3.12/site-packages/pkg/plugin.py:12: in run\n"
        "FAILED tests/test_x.py::test_y - AssertionError\n"
        "1 failed in 0.1s\n"
    )
    report = _run(
        repo,
        _profile(lint="ruff check ."),
        runner=_runner({"lint": (1, out)}),
    )
    assert report.verdict == "fail"
    assert [f.id for f in report.findings] == ["command_failed"]
