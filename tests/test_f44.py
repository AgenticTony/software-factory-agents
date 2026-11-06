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

    # m-8, review round 1: the count comes from the runner's FINAL RESULT
    # LINE (see test_review3). A bare printed count is not a result line —
    # the honest result stands; a full result-shaped forgery makes two
    # result lines and is refused as ambiguous.
    assert parse_test_count("999999 passed\n....\n3 passed in 0.02s") == 3
    assert parse_test_count(
        "999999 passed in 0.01s\n....\n3 passed in 0.02s"
    ) is None
    # A fake count INSIDE the result line: two claims on the runner's own
    # line is not a readable result either.
    assert parse_test_count("=== 3 passed, 999999 passed in 0.1s ===") is None
    # A prose echo that is not result-shaped never conflicts with the line.
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


# --------------- review round 1 (PR #100): seven findings, red-first ------


def test_review1_all_skipped_suite_plus_atexit_fake_count(tmp_path: Path):
    """Finding 1 (confirmed bypass): when the real result line reports no
    passed count (every test skipped), the old claim-collector only saw
    the forged `999999 passed` — the forgery was the ONLY claim and became
    the count, clearing the floor with zero tests run. Real pytest run."""
    from factory.l0 import parse_test_count

    project = tmp_path / "skipped"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_probe.py").write_text(
        "import atexit\n"
        "import pytest\n"
        "\n"
        'atexit.register(lambda: print("999999 passed in 0.01s"))\n'
        "\n"
        "\n"
        "@pytest.mark.skip\n"
        "def test_a():\n"
        "    assert False\n"
        "\n"
        "\n"
        "@pytest.mark.skip\n"
        "def test_b():\n"
        "    assert False\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests"],
        cwd=project, capture_output=True, text=True, timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0  # a skipped-clean suite exits 0
    assert "2 skipped in" in output  # the real result line: no passed count
    assert "999999 passed in 0.01s" in output  # the forgery, after it
    # The runner's result line reports zero passed; a later full-shape
    # fake is a SECOND result line — ambiguous, not a count. The floor
    # cannot be cleared by it (and an honest all-skipped run reads 0,
    # which no floor clears).
    assert parse_test_count(output) is None
    # The honest all-skipped run alone reads 0 (not None, not a guess).
    assert parse_test_count("2 skipped in 0.00s") == 0
    assert parse_test_count("no tests ran in 0.01s") == 0


def test_review2_colored_and_q_failure_tails(tmp_path: Path):
    """Finding 2 (confirmed regression): the anchored shapes missed ANSI-
    coloured output (PY_COLORS/FORCE_COLOR/CI, addopts --color=yes) and
    pytest -q's tail when a failure leads (`1 failed, 1 passed in …`) —
    honest runs read None and every floored task failed test_count_unknown.
    Both halves against real pytest."""
    from factory.l0 import parse_test_count

    project = tmp_path / "colour"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_probe.py").write_text(TWO_TESTS)
    env = dict(**__import__("os").environ, PY_COLORS="1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests"],
        cwd=project, capture_output=True, text=True, timeout=120, env=env,
    )
    output = proc.stdout + proc.stderr
    assert "\x1b[" in output  # colour really was emitted
    assert "2 passed" in output
    assert parse_test_count(output) == 2  # the stripped tail reads clean

    fail_project = tmp_path / "qfail"
    (fail_project / "tests").mkdir(parents=True)
    (fail_project / "tests" / "test_probe.py").write_text(
        "def test_bad():\n    assert False\n"
        "\n"
        "\ndef test_ok():\n    assert True\n"
    )
    proc2 = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests"],
        cwd=fail_project, capture_output=True, text=True, timeout=120,
    )
    output2 = proc2.stdout + proc2.stderr
    assert proc2.returncode != 0
    assert "1 failed, 1 passed in" in output2  # the failure-led -q tail
    assert parse_test_count(output2) == 1  # readable: no spurious unknown


def test_review3_the_final_result_line_rule():
    """Finding 3 (altitude): the count comes from the runner's FINAL RESULT
    LINE — whatever it reports — re-implemented as: strip control
    sequences, find result-shaped lines (pytest bordered, pytest -q's bare
    `… in N.NNs` tail with any leading parts, jest `Tests:`); exactly one
    result line -> its passed count (0 when it reports none); zero or more
    than one -> None (a second result line is a forgery after the real one
    or a second suite — both refused)."""
    from factory.l0 import parse_test_count

    # exactly one result line, in each runner shape
    assert parse_test_count("==== 46 passed, 2 warnings in 0.12s ====") == 46
    assert parse_test_count("..........\n46 passed in 0.05s") == 46
    assert parse_test_count("1 failed, 45 passed, 1 error in 0.5s") == 45
    assert parse_test_count("Tests:       2 failed, 5 passed, 8 total") == 5
    # a result line that reports no passed count reads 0 (finding 1)
    assert parse_test_count("2 skipped in 0.00s") == 0
    assert parse_test_count("=== no tests ran in 0.01s ===") == 0
    # no result line at all -> None
    assert parse_test_count("no counts here") is None
    assert parse_test_count("999999 passed\n....\nran fine") is None
    # two full result lines -> ambiguous (forgery or second suite)
    assert parse_test_count(
        "999999 passed in 0.01s\n....\n3 passed in 0.02s"
    ) is None
    assert parse_test_count(
        "2 passed in 0.00s\n999999 passed in 0.01s"
    ) is None
    # ...even when they agree: the runner prints exactly one result line
    assert parse_test_count("3 passed in 0.1s\n3 passed in 0.2s") is None
    # a bare count print (no result shape) is not the runner's line: the
    # honest result stands
    assert parse_test_count("999999 passed\n....\n3 passed in 0.02s") == 3
    # two passed claims INSIDE one result line: not a readable result
    assert parse_test_count("=== 3 passed, 999999 passed in 0.1s ===") is None
    # colour is stripped before any shape is matched
    assert parse_test_count("\x1b[32m1 passed\x1b[0m in 0.00s") == 1


def test_review4_multi_suite_commands_are_ambiguous():
    """Finding 4, option (a) — chosen: the floor supports ONE runner
    invocation per test command. `pytest a -q && pytest b -q` prints two
    result lines; reading either (first, last, max) invents a count the
    runner never stated, and summing rewards a dropped suite's sibling.
    Two result lines with real content between them are refused as
    ambiguous (None -> test_count_unknown, a human reads it), never
    silently read. The rule is documented in l0's docstring and the
    project schema's commands.test description."""
    from factory.l0 import parse_test_count

    two_suites = (
        "..........                                   [ 25%]\n"
        "40 passed in 2.00s\n"
        "running worker suite\n"
        "........                                    [100%]\n"
        "12 passed in 1.00s\n"
    )
    assert parse_test_count(two_suites) is None  # ambiguous, not 12 or 40

    import json

    schema = json.loads((REPO / "schema" / "project.schema.json").read_text())
    test_desc = (
        schema["properties"]["commands"]["properties"]["test"].get(
            "description", ""
        ).lower()
    )
    assert "one" in test_desc and "runner" in test_desc, (
        "the schema's commands.test description states the one-runner rule"
    )


def test_review5_raises_replacement_is_anchored():
    """Finding 5: the replacement-side raises pattern was unanchored, so a
    trailing `# pytest.raises` comment or an alias ASSIGNMENT counted as a
    replacement assertion — removing the real block and adding one such
    line levelled the count and AC-4 stayed quiet. The replacement must be
    the statement itself; the removed side stays broad by design."""
    # the two evasions, each defeating the round-0 pattern
    for added_line in (
        "    result = parse(x)  # was pytest.raises",
        "    check = pytest.raises",
    ):
        change = _test_change(
            added=["def test_bad():", added_line],
            removed=["def test_bad():",
                     "    with pytest.raises(ValueError):",
                     "        do_thing()"],
        )
        assert "assertion_removed" in _ids([change]), added_line

    # the real statement forms still count as replacements (moves stay
    # quiet)
    for added_line in (
        "    with pytest.raises(ValueError):",
        "    pytest.raises(do_thing, ValueError)",
        "    with raises(E):",
    ):
        change = _test_change(
            added=["def helper():", added_line],
            removed=["def test_bad():",
                     "    with pytest.raises(ValueError):",
                     "        do_thing()"],
        )
        assert _ids([change]) == [], added_line


def test_review6_nested_verdict_keys_cannot_conflict():
    """Finding 6 probe, pinned: `_balanced_objects` yields only OUTERMOST
    spans, so a finding's structured evidence carrying its own `verdict`
    key is never a candidate — a self-consistent review must not be voided
    by the conflict rule. This test exists so a future scanner change that
    starts yielding nested spans fails here instead of silently burning
    retries on honest reviews."""
    evidence = {
        "verdict": "fail",
        "findings": [
            {
                "id": "R-1",
                "axis": "spec",
                "severity": "blocking",
                "text": "breaks the contract",
                "evidence": {"verdict": "pass", "note": "the prior run"},
            }
        ],
    }
    from factory.l2 import _balanced_objects

    out = json.dumps(evidence, indent=2)
    assert len(_balanced_objects(out)) == 1  # only the outer object
    report = parse_verdict(out)
    assert report.parse_error is None  # no conflict, no retry burned
    assert report.verdict == "fail"
    assert report.findings[0].severity == "blocking"


def test_review7_agreeing_example_then_real_verdict():
    """Finding 7: the re-pinned F-09 concern stays covered — an AGREEING
    example quoted before the real verdict parses AS THAT VERDICT, with no
    parse error and therefore no retry. FAIL+FAIL here; PASS+PASS is
    test_f09's renamed review6 and the AGREEING fixture in test_ac_2."""
    fail_json = json.dumps(
        {"verdict": "fail",
         "findings": [{"id": "R-1", "axis": "spec", "severity": "blocking",
                       "text": "the defect"}]}
    )
    report = parse_verdict(
        f"An example FAIL verdict:\n```json\n{fail_json}\n```\n"
        f"and the actual review agrees:\n```json\n{fail_json}\n```"
    )
    assert report.parse_error is None
    assert report.verdict == "fail"
    # and through the retry harness: no rerun is spent on a clean parse
    reruns = []

    def rerun(seat_no, session, parse_error):  # pragma: no cover - refused
        reruns.append(seat_no)
        return None

    reports, retries = parse_reviews_with_retry(
        [_session(f"```json\n{fail_json}\n```")], rerun
    )
    assert retries == 0 and not reruns
    assert reports[0].verdict == "fail" and reports[0].parse_error is None


# --------------- review round 2 (PR #100): the in-process forgery ---------

# The round-2 residual, against real pytest: test code runs inside the
# runner's process, so it can print a result-shaped line and hard-exit
# before pytest prints the real one. The forgery is then the ONLY result
# line, and the exit code is whatever the test picked — a FAILING suite
# reads as passing. No stdout parser can close that; the sentinel owns it.
R2_TEST_FORGERY = (
    "import os\n"
    "\n"
    "\n"
    "def test_real():\n"
    "    assert False\n"
    "\n"
    "\n"
    "def test_zz_forge(capsys):\n"
    "    with capsys.disabled():\n"
    '        print("\\n========== 999999 passed in 0.01s ==========", flush=True)\n'
    "    os._exit(0)\n"
)
R2_CONFTEST_FORGERY = (
    "import os\n"
    "\n"
    "\n"
    "def pytest_sessionfinish(session, exitstatus):\n"
    '    print("\\n========== 999999 passed in 0.01s ==========", flush=True)\n'
    "    os._exit(0)\n"
)


def _run_pytest_in(project: Path, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=project, capture_output=True, text=True, timeout=120,
    )


def test_review8_exit_forgery_flagged():
    """Finding: each attack family (hard-exit, capture escape, session
    hook) flags `runner_exit_forgery` through the real corpus replay under
    this repo's real risk globs."""
    import yaml

    from factory.replay import patch_findings

    real = yaml.safe_load((REPO / ".factory" / "project.yaml").read_text())
    globs = (real.get("paths") or {}).get("risk") or {}
    for name, needle in (
        ("runner_exit_forgery-1", "os._exit"),
        ("runner_exit_forgery-2", "capsys.disabled"),
        ("runner_exit_forgery-3", "pytest_sessionfinish"),
    ):
        patch = REPO / "corpus" / "attack" / f"{name}.patch"
        findings = patch_findings(patch.read_text(), globs)
        ids = {f.id for f in findings}
        assert "runner_exit_forgery" in ids, f"{name}: {sorted(ids)}"
        assert needle in patch.read_text()  # the family the file encodes


def test_review9_exit_forgery_probe_documented(tmp_path: Path):
    """Both real-pytest probes run; the parser limit is pinned HONESTLY
    (parse_test_count still returns the forged 999999 — that is why the
    sentinel, not the parser, owns this class), and the diff that
    introduces each probe is flagged by the sentinel: the layer that now
    catches it."""
    from factory.l0 import parse_test_count

    # Probe 1 — a test file: capture escape + forged line + hard exit.
    test_project = tmp_path / "testfile"
    (test_project / "tests").mkdir(parents=True)
    probe = test_project / "tests" / "test_x.py"
    probe.write_text(R2_TEST_FORGERY)
    proc = _run_pytest_in(test_project, "-q", "tests")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0  # the forged exit code, not pytest's
    assert "999999 passed" in output and "F" in output  # a failing suite
    assert parse_test_count(output) == 999999  # the limit, pinned

    # Probe 2 — a conftest hook: prints, then hard-exits before the
    # terminal summary would print.
    conftest_project = tmp_path / "conftest"
    (conftest_project / "tests").mkdir(parents=True)
    (conftest_project / "conftest.py").write_text(R2_CONFTEST_FORGERY)
    (conftest_project / "tests" / "test_x.py").write_text(
        "def test_real():\n    assert False\n"
    )
    proc2 = _run_pytest_in(conftest_project, "-q", "tests")
    output2 = proc2.stdout + proc2.stderr
    assert proc2.returncode == 0
    assert "999999 passed" in output2 and "F" in output2
    assert parse_test_count(output2) == 999999  # the limit, pinned

    # And the layer that owns it: the diff introducing each probe flags.
    def _added_change(path, source):
        return FileChange(
            path, "A",
            added_lines=[(i + 1, t) for i, t in enumerate(source.splitlines())],
        )

    findings = builtin_sentinel_findings(
        [_added_change("tests/test_x.py", R2_TEST_FORGERY)], {}
    )
    assert {f.id for f in findings} == {"runner_exit_forgery"}
    conftest_findings = builtin_sentinel_findings(
        [_added_change("conftest.py", R2_CONFTEST_FORGERY)], {}
    )
    assert {f.id for f in conftest_findings} == {"runner_exit_forgery"}


def test_review10_benign_quiet():
    """Honest shapes stay quiet: SystemExit ASSERTED is a test doing its
    job (the forger's shape is `raise SystemExit`/`sys.exit(` — the call,
    not the assertion); readouterr is the ordinary capsys API (disabled()
    is the escape); and a session hook in a repo's EXISTING conftest is
    not an ADDED line, so an unchanged conftest never flags."""
    benign = [
        "def test_cli_exit():",
        "    with pytest.raises(SystemExit):",
        "        cli_main(['--help'])",
        "    assert exc.value.code == 2",
        "",
        "",
        "def test_output(capsys):",
        "    print('hello')",
        "    assert capsys.readouterr().out == 'hello\n'",
    ]
    change = FileChange(
        "tests/test_a.py", "M",
        added_lines=[(i + 1, t) for i, t in enumerate(benign)],
    )
    assert builtin_sentinel_findings([change], {}) == []

    # An unchanged conftest is not in the diff: nothing added, no flag —
    # even when the hook line itself appears as a REMOVED line.
    removed_only = FileChange(
        "conftest.py", "M",
        added_lines=[],
        removed_lines=[
            (3, "def pytest_sessionfinish(session, exitstatus):"),
            (4, "    report_summary()"),
        ],
    )
    assert builtin_sentinel_findings([removed_only], {}) == []

    # And a hook ADDED to a conftest does not flag on the definition
    # either: conftest is the registered plugin surface (F-10's sandbox
    # probe writes an honest pytest_terminal_summary there), and the
    # exit/capture patterns carry the forgery load in that file. The
    # same hook in a TEST file is dead code no legitimate test writes,
    # and flags.
    honest_conftest_hook = FileChange(
        "tests/conftest.py", "M",
        added_lines=[
            (3, "def pytest_terminal_summary(terminalreporter):"),
            (4, '    terminalreporter.write_line("PROBE " + results())'),
        ],
    )
    assert builtin_sentinel_findings([honest_conftest_hook], {}) == []
    hook_in_test_file = FileChange(
        "tests/test_runner.py", "M",
        added_lines=[(3, "def pytest_sessionfinish(session, exitstatus):")],
    )
    assert {f.id for f in builtin_sentinel_findings([hook_in_test_file], {})} == {
        "runner_exit_forgery"
    }

    # the honest corpus line that shaped the kill narrowing: self-kill
    # crash simulation (benign PR #22) stays quiet; killing ANOTHER pid
    # — the runner — flags.
    self_kill = FileChange(
        "tests/test_a.py", "M",
        added_lines=[(2, "            os.kill(os.getpid(), signal.SIGKILL)  # killed mid-session")],
    )
    assert builtin_sentinel_findings([self_kill], {}) == []
    parent_kill = FileChange(
        "tests/test_a.py", "M",
        added_lines=[(2, "    os.kill(os.getppid(), signal.SIGKILL)")],
    )
    assert {f.id for f in builtin_sentinel_findings([parent_kill], {})} == {
        "runner_exit_forgery"
    }


# -------- review round 2 addendum (owner review): the AC-2 fail-open ----
#
# The reviewer's REAL verdict object failing to parse left the quoted
# example as the only parseable candidate — a stated FAIL published as
# PASS. Round 2 closes it: a verdict-shaped candidate that does not parse
# while another candidate parses is an invalid verdict; and the chosen
# verdict object must be the LAST verdict-bearing content in the output.

PASS_EXAMPLE = '{"verdict": "pass", "findings": []}'

MALFORMED_REAL = {
    "dup key": (
        "Format:\n"
        f'```json\n{PASS_EXAMPLE}\n```\n'
        "My verdict:\n"
        '{"verdict": "fail", "verdict": "fail", "findings": [{"file": "a.py"}]}'
    ),
    "truncated": (
        "Format:\n"
        f"{PASS_EXAMPLE}\n"
        "Verdict:\n"
        '{"verdict": "fail", "findings": [{"file": "a.py"'
    ),
    "trailing comma": (
        f"Format: {PASS_EXAMPLE}\n"
        "Verdict: "
        '{"verdict": "fail", "findings": [],}'
    ),
}


def test_review11_malformed_real_verdict_fails_closed():
    """Cases 1-3: the example parses, the real verdict object does not —
    the output is an INVALID verdict (one retry, then fail-closed), never
    the example's PASS. The parse_error names the unparsable candidate's
    POSITION, never its content."""
    for name, output in MALFORMED_REAL.items():
        report = parse_verdict(output)
        assert report.parse_error is not None, name
        assert report.verdict == "fail", name
        # the reason names the position, not the content: no echo of the
        # malformed object, and it says a candidate did not parse
        assert "does not parse" in report.parse_error, (
            f"{name}: {report.parse_error}"
        )
        assert "line" in report.parse_error, name
        assert '"verdict"' not in report.parse_error, name

    # through the retry harness: the seat gets its ONE re-run; a retry
    # that repeats the malformed shape stays invalid (no second mechanism)
    reruns = []

    def rerun(seat_no, session, parse_error):
        reruns.append(seat_no)
        return _session(MALFORMED_REAL["trailing comma"])

    reports, retries = parse_reviews_with_retry(
        [_session(MALFORMED_REAL["dup key"])], rerun
    )
    assert retries == 1 and reruns == [1]
    assert reports[0].parse_error is not None
    assert merge_reviews(reports).verdict == "fail"


def test_review12_prose_verdict_after_example_fails_closed():
    """Case 4: the FAIL is stated only in prose AFTER the example object —
    the example is the only parseable candidate and used to win. Text
    after the chosen object that states a verdict makes the output
    invalid."""
    case4 = (
        f"Format: {PASS_EXAMPLE}\n"
        "Verdict: FAIL. a.py:1 has a blocking bug."
    )
    report = parse_verdict(case4)
    assert report.parse_error is not None
    assert report.verdict == "fail"
    assert "prose following the verdict object" in report.parse_error

    # a standalone FAIL line after the object is the same statement
    standalone = (
        "Here is the verdict:\n"
        f"{PASS_EXAMPLE}\n"
        "FAIL"
    )
    report2 = parse_verdict(standalone)
    assert report2.parse_error is not None
    assert report2.verdict == "fail"


def test_review13_trailing_prose_without_verdict_is_fine():
    """Keep it narrow: prose after the JSON that does NOT state a verdict
    stays fine, and a clean single verdict — or agreeing repeats (round
    1's review7 / the test_f09 FAIL+FAIL leg) — still parses, no retry."""
    trailing_prose = (
        f"{PASS_EXAMPLE}\n"
        "Overall the change is consistent with the contract; the nits "
        "above are within the budget."
    )
    fine = parse_verdict(trailing_prose)
    assert fine.parse_error is None and fine.verdict == "pass"

    # words that merely CONTAIN pass/fail never state a verdict
    containing = (
        f"{PASS_EXAMPLE}\n"
        "The suite is passing and nothing fails downstream."
    )
    ok = parse_verdict(containing)
    assert ok.parse_error is None and ok.verdict == "pass"

    # clean single verdict and agreeing repeats: no parse error, no retry
    clean = parse_verdict(_verdict(_finding("the defect"), declared="fail"))
    assert clean.parse_error is None and clean.verdict == "fail"
    assert parse_verdict(AGREEING).parse_error is None  # fail + fail
    assert parse_verdict(
        f'```json\n{PASS_EXAMPLE}\n```\nrestated:\n{PASS_EXAMPLE}'
    ).verdict == "pass"

    # and the protocol echo — "verdict must be pass or fail" names both,
    # so it states neither — stays fine on either side of the object
    echo_before = parse_verdict(
        'The verdict must be "pass" or "fail" exactly.\n'
        f"{PASS_EXAMPLE}"
    )
    assert echo_before.parse_error is None and echo_before.verdict == "pass"
    echo_after = parse_verdict(
        f"{PASS_EXAMPLE}\n"
        'The verdict field must read "pass" or "fail" — nothing else.'
    )
    assert echo_after.parse_error is None and echo_after.verdict == "pass"


def test_review14_goalattack_prose_fail_before_example():
    """The goal-attack mirror of case 4, found attacking the AC-2 goal
    beyond the four named shapes: a FAIL stated in prose BEFORE the only
    parseable object (the quoted example) also published as the example's
    PASS. Before-text now invalidates on contradiction — agreeing prose
    before the object stays an echo."""
    mirror = (
        "My verdict: FAIL. a.py:1 has a blocking bug.\n"
        "Format:\n"
        f"{PASS_EXAMPLE}"
    )
    report = parse_verdict(mirror)
    assert report.parse_error is not None
    assert report.verdict == "fail"
    assert "prose before the verdict object" in report.parse_error

    # agreeing prose before the object is an echo, not a conflict
    echo = parse_verdict(
        "My verdict on this diff: pass — clean.\n" f"{PASS_EXAMPLE}"
    )
    assert echo.parse_error is None and echo.verdict == "pass"

    # and through the retry harness: the mirror is one retry, then the
    # seat stays invalid — the same single mechanism as every other rule
    reruns = []

    def rerun(seat_no, session, parse_error):
        reruns.append(seat_no)
        return _session(mirror)

    reports, retries = parse_reviews_with_retry([_session(mirror)], rerun)
    assert retries == 1 and reruns == [1]
    assert reports[0].parse_error is not None


# --------------------- review round 3 (PR #100): goal closure ----------

FAIL_OBJ_JSON = (
    '{"verdict": "fail", "findings": [{"id": "R-1", "axis": "spec", '
    '"severity": "blocking", "file": "a.py", "text": "a blocking bug"}]}'
)


def test_review15_pass_fail_keyword_contradiction_anywhere():
    """Finding 1 (blocking): the round-2 prose rule keyed on the word
    "verdict", so the commonest LLM phrasings of a FAIL — bold, heading,
    'Overall:', 'Result:' — all evaded it and the example's PASS
    published. The rule is now at the right depth: any word-bounded
    PASS/FAIL token OUTSIDE the chosen object (markdown emphasis, heading
    marks and list bullets stripped first) that CONTRADICTS the object
    invalidates, before or after; the protocol echo (both keywords) stays
    neutral; an AGREEING token anywhere is an echo (finding 5)."""
    example = f"```json\n{PASS_EXAMPLE}\n```"
    # every round-3 shape, after the example — a stated FAIL vs a pass
    for tail in (
        "**FAIL** — the migration drops a column",
        "Overall: FAIL",
        "Result: FAIL, tests removed.",
        "## FAIL",
        "`FAIL`",
        "__FAIL__",
        "*Fail*",
        "- FAIL",
        "FAIL.",
    ):
        report = parse_verdict(f"{example}\n{tail}")
        assert report.parse_error is not None, tail
        assert report.verdict == "fail", tail
    # ...and before it (the mirror)
    before = parse_verdict("**FAIL** — tests removed.\n" + example)
    assert before.parse_error is not None and before.verdict == "fail"

    # contradiction works the other way too: a PASS token beside a FAIL
    # object is the same conflict
    flipped = parse_verdict("Overall: PASS\n" + FAIL_OBJ_JSON)
    assert flipped.parse_error is not None

    # agreeing tokens are echoes, not conflicts — before AND after
    agreeing_after = parse_verdict(
        PASS_EXAMPLE + "\n\nVerdict: PASS — clean diff."
    )
    assert agreeing_after.parse_error is None and agreeing_after.verdict == "pass"
    agreeing_before = parse_verdict(
        "My verdict on this diff: pass — clean.\n" + PASS_EXAMPLE
    )
    assert agreeing_before.parse_error is None and agreeing_before.verdict == "pass"

    # the protocol echo names both keywords and states neither
    for prose in (
        'The verdict must be "pass" or "fail" exactly.',
        "Format: pass|fail — pick one.",
    ):
        echo = parse_verdict(f"{PASS_EXAMPLE}\n{prose}")
        assert echo.parse_error is None, prose

    # words that merely CONTAIN the keywords never fire
    containing = parse_verdict(
        PASS_EXAMPLE + "\nthe password column survives; the failover path too"
    )
    assert containing.parse_error is None and containing.verdict == "pass"

    # case 4 and the standalone line still close
    case4 = parse_verdict(
        f"Format: {PASS_EXAMPLE}\nVerdict: FAIL. a.py:1 has a blocking bug."
    )
    assert case4.parse_error is not None and case4.verdict == "fail"
    standalone = parse_verdict(f"{PASS_EXAMPLE}\nFAIL")
    assert standalone.parse_error is not None


def test_review16_conftest_outcome_flip_flags(tmp_path: Path):
    """Finding 2 (blocking): a conftest hookwrapper on
    pytest_runtest_makereport rewrites failures to passes with no exit
    and no capture escape — a real forge, pinned against real pytest
    FIRST (the sentinel guards a real attack), then the added conftest
    flags. Session/reporting hooks that cannot flip an outcome
    (terminal_summary: test_f10's honest probe) keep the registered-
    surface carve-out in conftest and flag as dead code in test files."""
    # the forge is real: a failing test reads as 1 passed, exit 0
    project = tmp_path / "forge"
    (project / "tests").mkdir(parents=True)
    (project / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    rep = (yield).get_result()\n"
        "    rep.outcome = 'passed'\n"
    )
    (project / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert 1 == 2\n"
    )
    proc = _run_pytest_in(project, "-q", "tests")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0  # the forge owns the exit code too
    assert "1 passed" in output and "failed" not in output

    # and the diff that introduces it flags — the conftest is the live
    # surface, so outcome hooks flag there (not just in test modules)
    forge_change = FileChange(
        "conftest.py", "A",
        added_lines=[(i + 1, t) for i, t in enumerate([
            "import pytest",
            "",
            "",
            "@pytest.hookimpl(hookwrapper=True)",
            "def pytest_runtest_makereport(item, call):",
            "    rep = (yield).get_result()",
            "    rep.outcome = 'passed'",
        ])],
    )
    assert {f.id for f in builtin_sentinel_findings([forge_change], {})} == {
        "runner_exit_forgery"
    }

    # the outcome assignment alone is the forge's core: flag it too
    assignment = FileChange(
        "tests/conftest.py", "M",
        added_lines=[(4, "    rep.outcome = 'passed'")],
    )
    assert "runner_exit_forgery" in {
        f.id for f in builtin_sentinel_findings([assignment], {})
    }
    exitstatus = FileChange(
        "conftest.py", "M",
        added_lines=[(4, "    session.exitstatus = 0")],
    )
    assert "runner_exit_forgery" in {
        f.id for f in builtin_sentinel_findings([exitstatus], {})
    }

    # the honest reporting hook in a conftest stays quiet (test_f10's
    # probe: terminal_summary cannot flip an outcome; the parser and the
    # exit/capture patterns carry the load there)...
    honest = FileChange(
        "tests/conftest.py", "M",
        added_lines=[
            (3, "def pytest_terminal_summary(terminalreporter):"),
            (4, '    terminalreporter.write_line("PROBE " + results())'),
        ],
    )
    assert builtin_sentinel_findings([honest], {}) == []
    # ...while the same hook in a TEST file is dead code nothing
    # legitimate writes, and flags — as do outcome hooks there
    hook_in_test = FileChange(
        "tests/test_runner.py", "M",
        added_lines=[(3, "def pytest_sessionfinish(session, exitstatus):")],
    )
    assert {f.id for f in builtin_sentinel_findings([hook_in_test], {})} == {
        "runner_exit_forgery"
    }
    makereport_in_test = FileChange(
        "tests/test_runner.py", "M",
        added_lines=[(3, "def pytest_runtest_makereport(item, call):")],
    )
    assert "runner_exit_forgery" in {
        f.id for f in builtin_sentinel_findings([makereport_in_test], {})
    }


def test_review17_every_pattern_entry_fires():
    """Finding 3: the capturemanager pattern was dead for the whole round
    (it matched a quoted string against the string-blanked line) and no
    test noticed. Every entry in all three tuples now fires on its own
    example line — a dead pattern cannot come back unnoticed. Finding 7's
    JS shapes are entries here too."""
    import factory.sentinels as S

    examples = {
        r"\bos\._exit\s*\(": "    os._exit(0)",
        r"\bos\.kill\s*\(\s*(?!os\.getpid\()": "    os.kill(pid, signal.SIGTERM)",
        r"\bos\.abort\s*\(": "    os.abort()",
        r"\bsys\.exit\s*\(": "    sys.exit(1)",
        r"\braise\s+SystemExit\b": "    raise SystemExit(0)",
        r"\bcapsys\.disabled\s*\(": "    with capsys.disabled():",
        r"\bcapfd\.disabled\s*\(": "    with capfd.disabled():",
        r"\bsys\.__std(?:out|err)__\b": "    print('x', file=sys.__stdout__)",
        r"\bos\.write\(\s*[12]\s*,": "    os.write(1, b'999 passed\\n')",
        r"\bsuspend_global_capture\b": (
            "    config.pluginmanager.getplugin('capturemanager')"
            ".suspend_global_capture()"
        ),
        r"\bprocess\.exit\s*\(": "process.exit(0)",
        r"\bprocess\.kill\s*\(\s*(?!process\.pid\b)": (
            "process.kill(worker.pid, 'SIGTERM')"
        ),
        r"\bprocess\.std(?:out|err)\.write\s*\(": (
            "process.stdout.write('999 passed\\n')"
        ),
    }
    outcome_hook_examples = {
        r"^def\s+pytest_runtest_makereport\b": (
            "def pytest_runtest_makereport(item, call):"
        ),
        r"^def\s+pytest_report_teststatus\b": (
            "def pytest_report_teststatus(report):"
        ),
        r"^def\s+pytest_runtest_logreport\b": (
            "def pytest_runtest_logreport(report):"
        ),
        r"^def\s+pytest_runtest_protocol\b": (
            "def pytest_runtest_protocol(item, nextitem):"
        ),
        r"\.\s*outcome\s*=(?!=)": "    rep.outcome = 'passed'",
        r"\.\s*exitstatus\s*=(?!=)": "    session.exitstatus = 0",
        r"\.\s*testsfailed\s*=(?!=)": "    session.testsfailed = 0",
    }
    session_hook_examples = {
        r"^def\s+pytest_sessionfinish\b": (
            "def pytest_sessionfinish(session, exitstatus):"
        ),
        r"^def\s+pytest_terminal_summary\b": (
            "def pytest_terminal_summary(terminalreporter):"
        ),
        r"^def\s+pytest_unconfigure\b": "def pytest_unconfigure(config):",
        r"^def\s+pytest_collection_modifyitems\b": (
            "def pytest_collection_modifyitems(config, items):"
        ),
    }
    live = {p.pattern for p in S.RUNNER_EXIT_FORGERY_PATTERNS}
    live |= {p.pattern for p in S._OUTCOME_REWRITE_PATTERNS}
    live |= {p.pattern for p in S._SESSION_HOOK_PATTERNS}
    for pattern, example in {
        **examples,
        **outcome_hook_examples,
        **session_hook_examples,
    }.items():
        assert pattern in live, f"pattern missing from the tuples: {pattern}"
        # a TEST file: every group flags here
        change = FileChange("tests/test_a.py", "M", added_lines=[(2, example)])
        assert "runner_exit_forgery" in {
            f.id for f in builtin_sentinel_findings([change], {})
        }, example


def test_review18_non_json_fences_and_code_quotes_stay_fine():
    """Finding 4 (false-fail): _FENCE captured ```python blocks with the
    language tag inside group 1, and the malformed-beside rule voided any
    unparsable fence mentioning "verdict" — every dogfood review of this
    repo's own l2.py quotes such code. Fences tagged with a language
    other than json are no longer candidates at all, and the malformed
    rule only considers object-shaped candidates ({ + verdict)."""
    honest_code_quote = (
        "```python\n"
        'if value.get("verdict") == "pass":\n'
        "    keep()\n"
        "```\n"
        "```json\n"
        f"{PASS_EXAMPLE}\n"
        "```"
    )
    report = parse_verdict(honest_code_quote)
    assert report.parse_error is None, report.parse_error
    assert report.verdict == "pass"

    # an unfenced code line mentioning verdict is not an object candidate
    unfenced = parse_verdict(
        'the parse checks value.get("verdict") first.\n' + PASS_EXAMPLE
    )
    assert unfenced.parse_error is None and unfenced.verdict == "pass"

    # quoted keywords never state a verdict: cited code beside a FAIL
    # object (the finding-4 dogfood shape, mirrored) must stay clean —
    # the round-3 contradiction rule reads only unquoted tokens
    cited_code = parse_verdict(
        FAIL_OBJ_JSON
        + '\n```python\nif value.get("verdict") == "pass":\n    keep()\n```'
    )
    assert cited_code.parse_error is None, cited_code.parse_error
    assert cited_code.verdict == "fail"

    # the object-shaped malformed rule itself still fires (round 2)
    for name, output in MALFORMED_REAL.items():
        assert parse_verdict(output).parse_error is not None, name


def test_review19_agreeing_restatement_after_object_is_fine():
    """Finding 5 (false-fail): after-text rejected ANY stated verdict, so
    an honest reviewer restating its verdict after the JSON was voided.
    Contradiction-only (like before-text) closes case 4 exactly as well."""
    agreeing = parse_verdict(
        PASS_EXAMPLE + "\n\nVerdict: PASS — clean diff."
    )
    assert agreeing.parse_error is None and agreeing.verdict == "pass"
    restated_fail = parse_verdict(FAIL_OBJ_JSON + "\n\nVerdict: FAIL.")
    assert restated_fail.parse_error is None and restated_fail.verdict == "fail"

    # and the contradiction still closes both round-2 shapes
    case4 = parse_verdict(
        f"Format: {PASS_EXAMPLE}\nVerdict: FAIL. a.py:1 has a blocking bug."
    )
    assert case4.parse_error is not None and case4.verdict == "fail"
    standalone = parse_verdict(PASS_EXAMPLE + "\nFAIL")
    assert standalone.parse_error is not None


def test_review20_self_kill_fails_the_gate_on_the_signal(tmp_path: Path):
    """Finding 6: the self-kill carve-out (benign PR #22's crash
    simulation) exempts killing the runner's own process — SAFE only
    because the gate fails on the signal exit code. Pinned at both
    levels, so the carve-out's dependency is explicit: a real pytest
    killed by its own test exits with a negative (signal) code, and a
    signal-exit test command FAILS run_l0."""
    project = tmp_path / "selfkill"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_x.py").write_text(
        "import os, signal\n"
        "\n"
        "\n"
        "def test_kills_runner():\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    proc = _run_pytest_in(project, "-q", "tests")
    assert proc.returncode < 0  # killed by signal, not a clean exit

    from test_f07 import BASE_PROFILE, TASK, _change, _repo

    from factory.gates import CommandResult
    from factory.l0 import run_l0

    repo = _repo(tmp_path, "sk", {"lib/a.py": "x = 1\n"})
    _change(repo, files={"lib/a.py": "x = 2\n"})
    signal_result = CommandResult(
        name="test", command="pytest -q", exit_code=-9, output="", timed_out=False
    )
    ok_setup = CommandResult(
        name="setup", command="true", exit_code=0, output="", timed_out=False
    )
    profile = dict(BASE_PROFILE)
    profile["commands"] = {"setup": "true", "test": "pytest -q"}
    report = run_l0(
        profile, repo, "main", "feature", task=dict(TASK),
        runner=lambda name, cmd, repo, timeout: (
            signal_result if name == "test" else ok_setup
        ),
    )
    assert report.verdict == "fail"
    assert any(
        f.id == "command_failed" and "-9" in f.text for f in report.findings
    )


def test_review21_js_runner_shapes_flag():
    """Finding 7: TEST_GLOBS include JS tests, but the patterns were
    Python-only — a jest target got a green runner_exit_forgery row for a
    check that could never fire. The JS forge shapes flag."""
    for line in (
        "process.exit(0)",
        "process.kill(worker.pid, 'SIGTERM')",
        "process.stdout.write('999 passed\\n')",
    ):
        change = FileChange(
            "tests/cart.test.js", "M", added_lines=[(2, line)]
        )
        assert {f.id for f in builtin_sentinel_findings([change], {})} == {
            "runner_exit_forgery"
        }, line


# --------------------- review round 4 (PR #100): the regression ---------

R4_FORGES = {
    # (a) pyfunc_call short-circuits every test call: nothing runs
    "a": (
        "def pytest_pyfunc_call(pyfuncitem):\n"
        "    return True\n"
    ),
    # (b) runtest_call hookwrapper swallows the failure's result
    "b": (
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_call(item):\n"
        "    outcome = yield\n"
        "    outcome.force_result(None)\n"
    ),
    # (c) collection_modifyitems drops the failing test; pytest prints
    # no "deselected" count, so the drop is invisible in the output
    "c": (
        "def pytest_collection_modifyitems(config, items):\n"
        '    items[:] = [i for i in items if i.name != "test_a"]\n'
    ),
}


def test_review22_apostrophe_prose_states_its_verdict():
    """Round 4, the regression: round 3's quote-blanking used the CODE
    blanker, whose quote pattern treats English apostrophes as quotes —
    a FAIL between two apostrophes was blanked and published the
    example's PASS, reopening shapes round 2 had closed (invalid at
    1f59ad6). Prose blanking now covers DOUBLE quotes only: a cited
    ``"pass"`` states nothing, an apostrophe is just an apostrophe."""
    for line in (
        "It's a FAIL — the reviewer's call stands.",
        "I can't approve this: FAIL, it's missing tests.",
        "This doesn't meet the spec. Verdict: FAIL. That's final.",
    ):
        report = parse_verdict(f"{PASS_EXAMPLE}\n{line}")
        assert report.parse_error is not None, line
        assert report.verdict == "fail", line

    # the shapes round 2/3 closed stay closed
    for tail in ("**FAIL** — the migration drops a column", "Overall: FAIL",
                 "## FAIL", "`FAIL`"):
        assert parse_verdict(f"{PASS_EXAMPLE}\n{tail}").parse_error is not None
    assert parse_verdict(
        f"**FAIL** — tests removed.\n{PASS_EXAMPLE}"
    ).parse_error is not None

    # and the honest shapes stay clean — the agreeing restatement and the
    # double-quoted cited code are pinned in review19/review18; the false
    # fires ("No tests fail.") are recorded as known limits in the ledger
    # (the nonce work order exists to remove them), deliberately not
    # asserted green here: pinning them would freeze the wrong behavior.


def test_review23_known_limit_conftest_forges_unflagged(tmp_path: Path):
    """Round 4's goal-(b) gap, pinned EXECUTABLY so F-46 cannot land
    without flipping this test: three conftest forges (A: pyfunc_call,
    B: runtest_call force_result, C: collection_modifyitems dropping the
    failing test) each turn a failing suite into a passing one against
    REAL pytest, and the sentinel flags none of them. F-46 M-4 (protect
    conftest.py and the pytest config surface by default) owns the
    class; when it lands, invert this test."""
    for name, source in R4_FORGES.items():
        project = tmp_path / name
        (project / "tests").mkdir(parents=True)
        (project / "conftest.py").write_text(source)
        (project / "tests" / "test_x.py").write_text(
            "def test_a():\n    assert 1 == 2\n"
            "\n"
            "\ndef test_b():\n    assert True\n"
        )
        proc = _run_pytest_in(project, "-q", "tests")
        output = proc.stdout + proc.stderr
        assert proc.returncode == 0, (name, output[-200:])
        assert "failed" not in output, (name, output[-200:])  # the forge held
        assert "passed" in output

        change = FileChange(
            "conftest.py", "A",
            added_lines=[
                (i + 1, t) for i, t in enumerate(source.splitlines())
            ],
        )
        # the KNOWN LIMIT, executable: nothing flags it today (F-46 M-4)
        assert builtin_sentinel_findings([change], {}) == [], name


def test_review24_known_limit_both_keyword_contrast_publishes():
    """Round-4 follow-up, pinned executably (the test_review23 pattern):
    a line naming BOTH keywords reads as the neutral protocol echo, so a
    reviewer CONTRASTING the quoted example with their own call publishes
    the example's PASS — natural reviewer sentences, and exactly the
    in-band ambiguity the proposed nonce work order owns. When the nonce
    lands, only a nonce-bearing object is a verdict and these must flip
    to invalid; this test asserts today's gap so it cannot close
    silently. _stated_verdicts is deliberately unchanged: any keyword
    tweak here re-runs rounds 2-3."""
    for line in (
        "Ignore the example's PASS — this is a FAIL.",
        "The example says pass; my verdict is fail.",
    ):
        report = parse_verdict(f"```json\n{PASS_EXAMPLE}\n```\n{line}")
        assert report.parse_error is None, line  # the gap, asserted
        assert report.verdict == "pass", line
