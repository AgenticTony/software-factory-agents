"""A reviewer's harmless formatting slip must not void a review.

Round 1 (found by the 2026-11-04 seeded eval: 3 of 8 diffs failed L2
solely on it): the reviewer prompt shows ``missed_earlier`` in its
example JSON, so a first-round review echoes the key back blank. The
parser treated that blank as a shape violation and voided the reviewer's
entire verdict — an invalid_verdict_json FAIL on correct code, burning a
fix round. The field only carries meaning in delta mode, where
apply_convergence already drops an out-of-delta finding without a real
reason; there a blank must keep behaving exactly like an absent key.

Round 2 (PR #90 review): the same class of slip in two more fields.
``axis`` was exact-matched, so "Spec" or " spec" voided the verdict; it
is normalised exactly as severity is, and the canonical value stored. A
quoted line number ("42") voided the verdict, and a JSON ``true`` line
passed as int 1 (bool IS int in Python) — reaching the ±5-line location
merge as a real place; both are refused now, negatives included.

Also here: the eval's per-diff line for a known-clean diff must carry
L2's verdict and any invalid_verdict_json, so a clean diff whose review
was voided can never read as clean ("0 blocking finding(s)").
"""

import json

import pytest

from factory.eval import render_report
from factory.l2 import apply_convergence, merge_reviews, parse_verdict


def _verdict(finding: dict, declared: str = "fail") -> str:
    return json.dumps({"verdict": declared, "findings": [finding]})


# --- 1. the parser: blank is an absent reason, not a shape violation ---


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_missed_earlier_is_stored_as_none(blank):
    output = _verdict(
        {
            "id": "R-1",
            "axis": "spec",
            "severity": "major",
            "file": "src/a.py",
            "line": 3,
            "text": "the off-by-one survives",
            "missed_earlier": blank,
        }
    )
    report = parse_verdict(output)
    assert report.parse_error is None
    assert report.declared == "fail"
    assert report.verdict == "fail"
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.missed_earlier is None
    # The rest of the verdict survives intact.
    assert finding.id == "R-1"
    assert finding.axis == "spec"
    assert finding.severity == "major"
    assert finding.file == "src/a.py"
    assert finding.line == 3
    assert finding.text == "the off-by-one survives"


def test_real_missed_earlier_reason_survives():
    output = _verdict(
        {
            "id": "R-1",
            "axis": "spec",
            "severity": "major",
            "file": "src/a.py",
            "text": "the off-by-one survives",
            "missed_earlier": "round 1 read the diff, not the called helper",
        }
    )
    report = parse_verdict(output)
    assert report.parse_error is None
    assert report.findings[0].missed_earlier == (
        "round 1 read the diff, not the called helper"
    )


@pytest.mark.parametrize("bad", [3, 1.5, True, ["why"], {"why": "it"}])
def test_non_string_missed_earlier_is_still_invalid(bad):
    output = _verdict(
        {
            "axis": "spec",
            "severity": "major",
            "text": "the off-by-one survives",
            "missed_earlier": bad,
        }
    )
    report = parse_verdict(output)
    assert report.verdict == "fail"
    assert report.parse_error is not None
    assert "missed_earlier" in report.parse_error
    assert report.findings[0].id == "invalid_verdict_json"


# --- round 2: axis casing and a quoted line are slips, not violations ---


@pytest.mark.parametrize("axis", ["Spec", "STANDARDS", " spec ", "Standards"])
def test_axis_casing_is_normalised_to_canonical(axis):
    """Severity is already matched case-insensitively (.strip().lower());
    axis must not be stricter than its own sibling field. The finding
    stores the canonical value, so finding_key and every downstream
    consumer see one spelling."""
    output = _verdict(
        {
            "axis": axis,
            "severity": "Major",  # severity's own tolerance, unchanged
            "file": "src/a.py",
            "text": "the off-by-one survives",
        }
    )
    report = parse_verdict(output)
    assert report.parse_error is None
    assert report.findings[0].axis == axis.strip().lower()
    assert report.findings[0].axis in ("spec", "standards")


@pytest.mark.parametrize("axis", ["performance", "", "   ", 5, ["spec"], None])
def test_unknown_axis_is_still_invalid(axis):
    output = _verdict(
        {"axis": axis, "severity": "major", "text": "something"}
    )
    report = parse_verdict(output)
    assert report.verdict == "fail"
    assert report.parse_error is not None
    assert "axis" in report.parse_error
    assert report.findings[0].id == "invalid_verdict_json"


@pytest.mark.parametrize("line,expected", [("42", 42), (" 7 ", 7), ("0", 0)])
def test_quoted_line_number_is_stored_as_int(line, expected):
    """Models quote line numbers; "42" is the same place as 42 and the
    location merge needs the int."""
    output = _verdict(
        {
            "axis": "spec",
            "severity": "major",
            "file": "src/a.py",
            "line": line,
            "text": "the off-by-one survives",
        }
    )
    report = parse_verdict(output)
    assert report.parse_error is None
    assert report.findings[0].line == expected
    assert isinstance(report.findings[0].line, int)


@pytest.mark.parametrize(
    "line", [True, False, 1.5, "12-15", "abc", "-3", -3, [12], {"n": 1}]
)
def test_bad_line_values_are_still_invalid(line):
    output = _verdict(
        {
            "axis": "spec",
            "severity": "major",
            "file": "src/a.py",
            "line": line,
            "text": "the off-by-one survives",
        }
    )
    report = parse_verdict(output)
    assert report.verdict == "fail"
    assert report.parse_error is not None
    assert "line" in report.parse_error
    assert report.findings[0].id == "invalid_verdict_json"


def test_bool_line_never_reaches_the_location_merge():
    """A JSON ``true`` line is bool, and bool IS int in Python: accepted,
    it would enter the ±5-line location merge as line 1 and could swallow
    a neighbouring finding into its reporter's entry. The parser must
    void it before any L2Finding exists."""
    bool_line = _verdict(
        {
            "axis": "spec",
            "severity": "major",
            "file": "src/a.py",
            "line": True,
            "text": "the off-by-one survives",
        }
    )
    report = parse_verdict(bool_line)
    assert report.parse_error is not None
    assert report.findings[0].id == "invalid_verdict_json"
    # Fail-closed at the union too: a second, valid seat cannot rescue
    # the seat that could not speak a place — the bool line never
    # becomes an L2Finding the merge could locate.
    valid = parse_verdict(
        _verdict(
            {
                "axis": "spec",
                "severity": "major",
                "file": "src/a.py",
                "line": 1,
                "text": "the off-by-one survives",
            }
        )
    )
    union = merge_reviews([report, valid])
    assert union.verdict == "fail"
    assert union.parse_error is not None


# --- 2. convergence: a blank reason drops exactly like an absent key ---


def _out_of_delta_output(missed_earlier: str | None, declared: str = "pass"):
    finding = {
        "id": "R-1",
        "axis": "spec",
        "severity": "blocking",
        "file": "src/untouched.py",
        "line": 2,
        "text": "a new finding outside the delta",
    }
    if missed_earlier is not None:
        finding["missed_earlier"] = missed_earlier
    return _verdict(finding, declared=declared)


@pytest.mark.parametrize("blank", ["", "   "])
def test_out_of_delta_finding_with_blank_reason_is_dropped(blank):
    classified = apply_convergence(
        parse_verdict(_out_of_delta_output(blank)),
        prior={},
        delta_files={"src/changed.py"},
        delta_mode=True,
    )
    assert classified.findings == []
    assert [d["reason"] for d in classified.dropped] == [
        "out_of_delta_without_missed_earlier"
    ]
    # The declared pass was never overridden by the dropped blocking.
    assert classified.verdict == "pass"


def test_blank_and_absent_key_classify_identically():
    blank = apply_convergence(
        parse_verdict(_out_of_delta_output("   ")),
        prior={},
        delta_files={"src/changed.py"},
        delta_mode=True,
    )
    absent = apply_convergence(
        parse_verdict(_out_of_delta_output(None)),
        prior={},
        delta_files={"src/changed.py"},
        delta_mode=True,
    )
    assert blank.verdict == absent.verdict == "pass"
    assert blank.dropped == absent.dropped
    assert blank.findings == absent.findings == []


def test_real_reason_keeps_out_of_delta_finding_alive():
    classified = apply_convergence(
        parse_verdict(
            _out_of_delta_output("round 1 never opened this helper")
        ),
        prior={},
        delta_files={"src/changed.py"},
        delta_mode=True,
    )
    assert classified.dropped == []
    assert len(classified.findings) == 1
    assert classified.verdict == "fail"


# --- 3. the eval report: a clean diff's line shows the L2 verdict ---


def _report(clean_record: dict) -> dict:
    return {
        "fixture": "soloadventurer",
        "base_ref": "main",
        "l2": "sessions",
        "seeds": [],
        "clean": [clean_record],
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 0.0},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 0},
    }


def _clean_line(clean_record: dict) -> str:
    rendered = render_report(_report(clean_record))
    return next(
        line
        for line in rendered.splitlines()
        if line.startswith(f"- clean {clean_record['id']}:")
    )


def test_clean_line_reports_l2_verdict():
    # F-48 re-seat: the line names all three layers now (m-11); the
    # record is the real run_seed shape (L0/L1 always recorded).
    record = {
        "id": "clean-new-helper",
        "title": "New helper, no defect",
        "blocking_findings": 0,
        "layers": {"L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
                   "L2": {"verdict": "pass", "parse_error": None,
                          "findings": []}},
    }
    assert _clean_line(record) == (
        "- clean clean-new-helper: L0 pass, L1 pass, "
        "L2 pass, 0 blocking finding(s) "
        "— New helper, no defect"
    )


def test_clean_line_reports_invalid_verdict_json():
    # The 2026-11-04 eval shape: verdict fail, parse_error set, the sole
    # finding the parser's invalid_verdict_json — and 0 blocking. F-48
    # re-seat: the fail is marked L2's false positive, naming why.
    record = {
        "id": "clean-docstring",
        "title": "Docstring-only change",
        "blocking_findings": 0,
        "layers": {"L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
                   "L2": {
                       "verdict": "fail",
                       "parse_error": (
                           "reviewer 2: finding 0 missed_earlier must be a "
                           "non-empty string explaining why the earlier "
                           "round missed it"
                       ),
                       "findings": [{"id": "invalid_verdict_json",
                                     "severity": None}],
                   }},
    }
    line = _clean_line(record)
    assert line.startswith(
        "- clean clean-docstring: L0 pass, L1 pass, "
        "L2 fail (false positive, invalid_verdict_json)")
    assert "invalid_verdict_json" in line
    assert line.endswith("— Docstring-only change")


def test_clean_line_reports_l2_fail_without_parse_error():
    record = {
        "id": "clean-fail",
        "title": "Clean diff a reviewer flagged",
        "blocking_findings": 1,
        "layers": {"L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
                   "L2": {"verdict": "fail", "parse_error": None,
                          "findings": [{"id": "R-1", "severity": "blocking"}]}},
    }
    assert _clean_line(record) == (
        "- clean clean-fail: L0 pass, L1 pass, "
        "L2 fail (false positive), 1 blocking finding(s) "
        "— Clean diff a reviewer flagged"
    )


def test_clean_line_keeps_l2_skipped_wording():
    record = {
        "id": "clean-skipped",
        "title": "No sessions",
        "blocking_findings": None,
        "layers": {"L0": {"verdict": "pass"}, "L1": {"verdict": "pass"}},
    }
    assert _clean_line(record) == (
        "- clean clean-skipped: L0 pass, L1 pass, L2 n/a (skipped) — No sessions"
    )
