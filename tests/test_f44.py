"""F-44 acceptance tests — BUILD-ORDER.md, work order F-44.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f44.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the verdict parser, the merge/convergence/vote pipeline and the
  retry harness (``parse_reviews_with_retry``) over scripted seat records —
  no model anywhere. The F-33 corpus replay over this repo's real risk
  globs (AC-4). The AC-3 hostile probes run REAL ``pytest`` subprocesses:
  the probe suites are two trivial tests, and the probe measures the
  runner's actual captured output — a fake ``N passed`` line printed from
  inside a test (mid-run, ``-s``) and one printed from ``atexit`` / a
  session-finish hook (after the real summary) — not a hand-typed copy of
  what such output looks like.
* **Stubbed:** nothing model-shaped — every session record is built
  directly, the way ``tests/test_l2_retry_budgets.py`` builds them.
"""

import json
import subprocess
import sys
from pathlib import Path

from factory.eval import _count_blocking
from factory.gates import FileChange
from factory.l2 import (
    apply_convergence,
    apply_vote,
    merge_reviews,
    parse_reviews_with_retry,
    parse_verdict,
    parse_verifier_vote,
)
from factory.sentinels import builtin_sentinel_findings
from factory.session import SessionRecord

REPO = Path(__file__).resolve().parent.parent

# --- shared verdict fixtures -------------------------------------------------

CONFLICTING = (
    "The change breaks the contract.\n"
    '```json\n{"verdict": "fail", "findings": []}\n```\n'
    "For reference, a well-formed verdict object looks like:\n"
    '```json\n{"verdict": "pass", "findings": []}\n```\n'
)
# The same two objects, the reverse order: the example first, the stated
# verdict last (the case _extract_json's docstring used to optimise for).
CONFLICTING_REVERSED = (
    "For reference, a well-formed verdict object looks like:\n"
    '```json\n{"verdict": "pass", "findings": []}\n```\n'
    "The change breaks the contract.\n"
    '```json\n{"verdict": "fail", "findings": []}\n```\n'
)
AGREEING = (
    '```json\n{"verdict": "fail", "findings": []}\n```\n'
    "Restated: fail.\n"
    '{"verdict": "fail", "findings": []}\n'
)


def _finding(text="scope creep", file="impl.py", axis="spec",
             severity="blocking", **extra) -> dict:
    item = {"id": "R-1", "axis": axis, "severity": severity,
            "file": file, "text": text}
    item.update(extra)
    return item


def _verdict(*findings: dict, declared: str | None = None) -> str:
    payload = {"verdict": declared or ("fail" if findings else "pass"),
               "findings": list(findings)}
    return json.dumps(payload)


def _session(result, seat="reviewer", status="completed"):
    return SessionRecord(seat=seat, status=status, result=result, num_turns=1)


# --- AC-1: severity canonicalised once, at parse ------------------------------


def test_ac_1():
    # Parse canonicalises: whatever casing/spacing the reviewer used, the
    # STORED finding carries the canonical value (axis already did).
    report = parse_verdict(_verdict(
        _finding("the defect", severity="  Blocking "),
        declared="pass",
    ))
    assert report.parse_error is None
    assert report.findings[0].severity == "blocking"
    assert report.findings[0].axis == "spec"  # the precedent, unchanged

    # The M-1 scenario, both merge orders: reviewer 1 rates the shared
    # defect "Blocking", reviewer 2 "minor", both declare pass — the merged
    # finding is blocking and the union FAILS. Before F-44 the raw
    # "Blocking" ranked 99 (unknown) and softened the union to minor/pass.
    for order in (0, 1):
        reports = [
            parse_verdict(_verdict(_finding("the defect", severity="Blocking"),
                                  declared="pass")),
            parse_verdict(_verdict(_finding("the defect", severity="minor"),
                                  declared="pass")),
        ]
        if order:
            reports.reverse()
        union = merge_reviews(reports)
        assert union.findings[0].severity == "blocking", order
        assert union.verdict == "fail", order
        assert len(union.findings) == 1, order  # one entry, both reporters
        assert sorted(union.findings[0].reported_by) == [1, 2], order

    # The stored-report reader stays case-insensitive: records written
    # before F-44 may hold "Blocking" (the exact == "blocking" miss was
    # M-1's second half, factory/eval.py).
    assert _count_blocking({"findings": [{"severity": "Blocking"},
                                         {"severity": "minor"}]}) == 1
    assert _count_blocking({"findings": [{"severity": "blocking"}]}) == 1
    assert _count_blocking({"findings": [{"severity": "note"}]}) == 0
    assert _count_blocking(None) == 0


# --- AC-2: one verdict per output; differing objects are invalid ----------------


def test_ac_2():
    # FAIL stated, then an example pass-JSON quoted: m-10's shape — the
    # last-wins parse read it as PASS. Refused now: parse_error, verdict
    # fail, never a pass. Both object orders refuse.
    for output in (CONFLICTING, CONFLICTING_REVERSED):
        report = parse_verdict(output)
        assert report.parse_error is not None, output[:40]
        assert "conflict" in report.parse_error
        assert report.verdict == "fail"

    # Repeated objects with the SAME verdict stay accepted.
    agreeing = parse_verdict(AGREEING)
    assert agreeing.parse_error is None
    assert agreeing.verdict == "fail"

    # The existing retry path is the only mechanism: a conflicting output
    # is retried once like any other invalid verdict (parse_reviews_with_
    # retry, PR #90 round 2 — no second retry exists), and a still-
    # conflicting retry keeps the seat invalid.
    reruns = []

    def rerun(seat_no, session, parse_error):
        reruns.append((seat_no, parse_error))
        return _session(CONFLICTING)  # the retry conflicts too

    reports, retries = parse_reviews_with_retry(
        [_session(CONFLICTING), _session(_verdict(declared="pass"))], rerun
    )
    assert retries == 1
    assert [r[0] for r in reruns] == [1]
    assert "conflict" in reruns[0][1]  # the reason names the class
    assert reports[0].parse_error is not None  # survived its retry
    assert reports[1].parse_error is None

    # Fail-closed through the union and the round logic: an invalid verdict
    # that survived its retry never counts as PASS anywhere downstream.
    union = merge_reviews(reports)
    assert union.verdict == "fail"
    assert union.parse_error is not None
    converged = apply_convergence(
        union, prior={}, delta_files=set(), delta_mode=False
    )
    assert converged.verdict == "fail"
    vote = parse_verifier_vote(json.dumps({"votes": []}), union.findings)
    voted = apply_vote(union, vote)
    assert voted.verdict == "fail"  # parse_error survives the vote
    assert voted.verifier_error is None  # the vote itself was well-formed


# --- AC-3: the count comes from the runner's final summary line ----------------


def test_ac_3(tmp_path: Path):
    from factory.l0 import parse_test_count

    # The three runner summary shapes _TEST_COUNT supports.
    assert parse_test_count("==== 46 passed, 2 warnings in 0.12s ====") == 46
    assert parse_test_count("..........\n46 passed in 0.05s") == 46
    assert parse_test_count("Tests:       2 failed, 5 passed, 8 total") == 5

    # No recognisable summary -> None (not 0, not a guess).
    assert parse_test_count("no counts here") is None

    # m-8: a printed fake count must not clear the floor — the max
    # anywhere in the output is not the runner's count.
    assert parse_test_count("999999 passed\n....\n3 passed in 0.02s") is None
    # A fake count INSIDE the summary line: two claims on the runner's own
    # line is not a recognisable summary either.
    assert parse_test_count("=== 3 passed, 999999 passed in 0.1s ===") is None
    # Repeated agreeing summaries are one count (an echo that agrees with
    # the runner is not a forgery).
    assert parse_test_count("3 passed\n3 passed in 0.02s") == 3

    # The floor treats None as NOT cleared (verified, not assumed): a
    # min_tests floor with no readable count fails with test_count_unknown.
    from test_f07 import BASE_PROFILE, _change, _repo, _run

    repo = _repo(tmp_path, "count", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "echo 'nothing counted'"}
    report = _run(repo, profile, baseline={"min_tests": 3})
    assert report.verdict == "fail"
    assert any(f.id == "test_count_unknown" for f in report.findings)

    # And the m-8 defeat at the FLOOR level: a fake 999999 printed before
    # the real summary must not clear a floor the real count misses. (Red
    # on pre-F-44 code: max() read 999999 and this gate PASSED.)
    profile2 = dict(BASE_PROFILE)
    profile2["commands"] = {
        "setup": "true",
        "test": "printf '999999 passed\\n....\\n3 passed in 0.02s\\n'",
    }
    defeated = _run(repo, profile2, baseline={"min_tests": 100})
    assert defeated.verdict == "fail"
    assert any(
        f.id in ("test_count_unknown", "test_count_dropped")
        for f in defeated.findings
    )


# --- AC-4: removing a pytest.raises block is an assertion removal --------------


def _ids(changes):
    return sorted(f.id for f in builtin_sentinel_findings(changes, {}))


def _test_change(added, removed, path="tests/test_a.py"):
    return FileChange(
        path, "M",
        added_lines=[(i + 1, t) for i, t in enumerate(added)],
        removed_lines=[(i + 1, t) for i, t in enumerate(removed)],
    )


def test_ac_4():
    # The with-form (I-11): the block's body carries no assert of its own,
    # so the with-line is the only line that names the assertion. Removing
    # the whole block silently greens a test that used to demand failure.
    with_form = _test_change(
        added=["def test_bad():", "    do_thing()"],
        removed=["def test_bad():",
                 "    with pytest.raises(ValueError):",
                 "        do_thing()"],
    )
    assert "assertion_removed" in _ids([with_form])

    # The call form: pytest.raises(fn, ...) as a statement.
    call_form = _test_change(
        added=["def test_bad():", "    pass"],
        removed=["def test_bad():",
                 "    pytest.raises(do_thing, ValueError)"],
    )
    assert "assertion_removed" in _ids([call_form])

    # unittest's assertRaises/assertRaisesRegex are ALREADY covered: the
    # broad `\bassert` pattern matches the "assert" inside them (the '.'
    # before it is a word boundary). Stated here so the decision is pinned.
    for line in ("    self.assertRaises(ValueError, do_thing)",
                 "    with self.assertRaisesRegex(RuntimeError, 'boom'):",
                 "    self.assertRaisesRegex(ValueError, 'msg', do_thing)"):
        assert "assertion_removed" in _ids([_test_change(
            added=["def test_bad():", "    pass"],
            removed=["def test_bad():", line],
        )]), line

    # A same-file MOVE stays quiet: the with-line re-added elsewhere in the
    # file levels the count exactly as a moved assert does (the F-25/F-33
    # arithmetic; the pattern must sit on BOTH sides of it).
    moved = _test_change(
        added=["def helper():", "    with pytest.raises(ValueError):",
               "        do_thing()"],
        removed=["def test_bad():", "    with pytest.raises(ValueError):",
                 "        do_thing()"],
    )
    assert _ids([moved]) == []

    # The F-33 attack diff: removal through the real corpus replay, under
    # this repo's real risk globs.
    import yaml

    from factory.replay import patch_findings

    real = yaml.safe_load((REPO / ".factory" / "project.yaml").read_text())
    globs = (real.get("paths") or {}).get("risk") or {}
    attack = REPO / "corpus" / "attack" / "assertion_removed-5.patch"
    findings = patch_findings(attack.read_text(), globs)
    assert "assertion_removed" in {f.id for f in findings}, (
        "the pytest.raises attack diff does not flag assertion_removed"
    )


# ---------------------- hostile pass: the probes the ACs name, at the seam ----


def _run_pytest(project: Path, *args: str) -> str:
    """Run pytest in a probe project; the combined captured output."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=project, capture_output=True, text=True, timeout=120,
    )
    return proc.stdout + proc.stderr


TWO_TESTS = (
    "def test_one():\n    assert 1 == 1\n\n\ndef test_two():\n    assert 2 == 2\n"
)


def test_hostile_fake_count_printed_mid_run(tmp_path: Path):
    """m-8 at the real seam: a test that PRINTS a fake summary line during
    the run (visible with -s, the jest default, any runner without
    capture). The max-anywhere read counted 999999; the final-summary read
    must not."""
    from factory.l0 import parse_test_count

    project = tmp_path / "midrun"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_probe.py").write_text(
        "def test_prints_fake():\n"
        "    print('999999 passed in 0.01s')\n"
        "    assert True\n"
        "\n"
        "\ndef test_quiet():\n"
        "    assert True\n"
    )
    output = _run_pytest(project, "-q", "-s", "tests")
    assert "2 passed" in output  # the real summary ran
    assert "999999 passed" in output  # and the fake line is really there
    assert parse_test_count(output) is None  # neither count is trusted


def test_hostile_fake_count_printed_after_the_summary(tmp_path: Path):
    """The after-summary probe: atexit runs AFTER pytest's terminal
    summary, so the fake line is the LAST summary-shaped line in the
    output — reading 'the last one' would take the forgery. Disagreeing
    summary claims void the count (None), and the floor stays uncleared.
    The session-finish hook is probed the same way: wherever its print
    lands relative to the summary, the claims disagree."""
    from factory.l0 import parse_test_count

    atexit_project = tmp_path / "atexit"
    (atexit_project / "tests").mkdir(parents=True)
    (atexit_project / "conftest.py").write_text(
        "import atexit\n\n"
        "atexit.register(lambda: print('999999 passed in 0.01s'))\n"
    )
    (atexit_project / "tests" / "test_probe.py").write_text(TWO_TESTS)
    output = _run_pytest(atexit_project, "-q", "tests")
    assert "2 passed" in output
    assert "999999 passed" in output
    assert output.strip().endswith("999999 passed in 0.01s"), (
        f"the atexit print did not land after the summary:\n{output[-200:]}"
    )
    assert parse_test_count(output) is None

    # A session-finish hook print lands BEFORE pytest's own summary (the
    # terminal reporter writes last) and, under -q, glues onto the
    # progress-dots line — not a summary-shaped line, so it is ignored and
    # the REAL count stands. Both hook outcomes are fail-closed for the
    # forger: an ill-shaped fake is ignored (the honest count is read), a
    # summary-shaped one disagrees and voids the count. The one thing that
    # can never happen is the fake count becoming the floor's count.
    hook_project = tmp_path / "sessionfinish"
    (hook_project / "tests").mkdir(parents=True)
    (hook_project / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    print('999999 passed in 0.01s')\n"
    )
    (hook_project / "tests" / "test_probe.py").write_text(TWO_TESTS)
    hook_output = _run_pytest(hook_project, "-q", "tests")
    assert "2 passed" in hook_output
    assert "999999 passed" in hook_output  # the hook print is visible
    assert parse_test_count(hook_output) in (None, 2)
    assert parse_test_count(hook_output) != 999999


def test_hostile_severity_shapes():
    """Mixed case and whitespace severities canonicalise; an UNKNOWN one
    (the model inventing a scale) stays an invalid verdict, never a
    silently-accepted 'minor'."""
    for raw in ("Blocking", "BLOCKING", "  blocking\t", "Minor", " NOTE "):
        report = parse_verdict(_verdict(_finding("x", severity=raw),
                                        declared="pass"))
        assert report.parse_error is None, raw
        assert report.findings[0].severity == raw.strip().lower(), raw
    for unknown in ("critical", "blocker", "high", "urgent", "info"):
        report = parse_verdict(_verdict(_finding("x", severity=unknown),
                                        declared="pass"))
        assert report.parse_error is not None, unknown
        assert report.verdict == "fail", unknown


def test_hostile_verdict_json_inside_a_fenced_example():
    """A fenced example verdict that DISAGREES with the stated verdict is
    the m-10 conflict (refused); one that AGREES is an echo, not a
    conflict — both pinned so neither direction is an accident."""
    fenced_example = (
        "The contract holds.\n"
        '```json\n{"verdict": "pass", "findings": []}\n```\n'
        "(Example of a FAIL verdict for contrast:)\n"
        '```json\n{"verdict": "fail", "findings": []}\n```\n'
    )
    refused = parse_verdict(fenced_example)
    assert refused.parse_error is not None
    assert refused.verdict == "fail"
    echo = parse_verdict(AGREEING)
    assert echo.parse_error is None and echo.verdict == "fail"


def test_hostile_raises_removal_rewritten(tmp_path: Path):
    """Evading the new pattern: `from pytest import raises` drops the
    `pytest.` prefix; a rename inside the call keeps the token. The
    from-import form is caught (it is the same assertion, imported
    differently); plain indentation and helper moves are line-text
    unchanged, so the substring scan holds (the move case is test_ac_4's)."""
    from_import = _test_change(
        added=["def test_bad():", "    do_thing()"],
        removed=["def test_bad():", "    with raises(ValueError):",
                 "        do_thing()"],
    )
    assert "assertion_removed" in _ids([from_import])

    indented = _test_change(
        added=["def test_bad():", "            do_thing()"],
        removed=["def test_bad():",
                 "                with pytest.raises(ValueError):",
                 "                    do_thing()"],
    )
    assert "assertion_removed" in _ids([indented])
