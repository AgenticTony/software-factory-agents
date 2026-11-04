"""The reviewer fan-out's ONE retry, and the eval's clean-diff aggregate
(PR #90 round 2, issues 3 and 4).

Issue 3: one reviewer seat whose output fails to parse no longer voids
every reviewer's findings outright — it gets exactly one re-run of the
same seat with the same prompt. Fail-closed throughout: a still-invalid
retry fails the union exactly as before (invalid_verdict_json naming the
reviewer), no reviewer is ever dropped, and a fan-out never passes with
fewer reviewers than configured. The policy lives in
:func:`factory.l2.parse_reviews_with_retry`; the orchestrator's two
fan-out sites and the eval's review path all parse through it.

Issue 4: the eval report says how many known-clean diffs drew an L2 FAIL
for any reason (a blocking finding or an invalid verdict) — the
false-positive aggregate the catch-rate table lacks — and whether any
reviewer retry fired.
"""

import json
import re
from pathlib import Path

from factory.eval import (
    CLEAN_DIFFS,
    build_soloadventurer_fixture,
    catch_rate_by_layer,
    render_report,
    run_seed,
    run_seeded_eval,
)
from factory.l2 import parse_reviews_with_retry
from factory.session import SessionRecord

GARBAGE = "Reviewer notes: the diff looks fine to me (no JSON)."
VALID_PASS = json.dumps({"verdict": "pass", "findings": []})
VALID_FAIL = json.dumps(
    {
        "verdict": "fail",
        "findings": [
            {
                "id": "R-1",
                "axis": "spec",
                "severity": "blocking",
                "file": "src/a.py",
                "line": 3,
                "text": "the defect",
            }
        ],
    }
)

# The verifier prompt embeds the union findings as a fenced JSON array
# (same shape the F-16 scripted seats read).
_FINDINGS_ARRAY = re.compile(r"```json\n(\[\s*\{.*?\}\s*\])\n```", re.S)


def _session(result, seat="reviewer", status="completed"):
    return SessionRecord(seat=seat, status=status, result=result, num_turns=1)


# --- the shared helper: one retry per seat that failed to parse ----------


def test_invalid_then_valid_uses_the_retry_and_counts_it():
    reruns = []

    def rerun(seat_no, session, parse_error):
        reruns.append((seat_no, session.status, parse_error))
        return _session(VALID_FAIL)

    reports, retries = parse_reviews_with_retry(
        [_session(GARBAGE), _session(VALID_PASS)], rerun=rerun
    )
    assert retries == 1
    # exactly one re-run, not a loop — told WHICH seat slipped and why
    assert [r[0] for r in reruns] == [1]
    assert reruns[0][1] == "completed"
    assert reruns[0][2] and "no valid verdict JSON" in reruns[0][2]
    assert len(reports) == 2  # never fewer reviewers than configured
    assert all(r.parse_error is None for r in reports)
    assert reports[0].verdict == "fail"  # the retry's verdict is the seat's
    assert [f.id for f in reports[0].findings] == ["R-1"]
    assert reports[1].verdict == "pass"


def test_invalid_twice_still_fails_closed():
    reports, retries = parse_reviews_with_retry(
        [_session(GARBAGE)],
        rerun=lambda seat_no, session, parse_error: _session(
            GARBAGE + " Again."),
    )
    assert retries == 1
    assert reports[0].parse_error is not None


def test_all_valid_never_retries():
    def explode(seat_no, session, parse_error):
        raise AssertionError("no seat needed a retry")

    reports, retries = parse_reviews_with_retry(
        [_session(VALID_PASS), _session(VALID_PASS)], rerun=explode
    )
    assert retries == 0
    assert len(reports) == 2
    assert all(r.verdict == "pass" for r in reports)


def test_a_seat_that_produced_no_text_is_retried_once():
    """An aborted/failed session fails to parse like garbage does — one
    retry, then the seat's report stays invalid."""
    reports, retries = parse_reviews_with_retry(
        [_session(None, status="aborted")],
        rerun=lambda seat_no, session, parse_error: _session(VALID_PASS),
    )
    assert retries == 1
    assert reports[0].parse_error is None
    assert reports[0].verdict == "pass"


def test_without_a_rerun_it_behaves_exactly_as_today():
    reports, retries = parse_reviews_with_retry([_session(GARBAGE)])
    assert retries == 0
    assert reports[0].parse_error is not None


# --- the eval path: scripted fan-out seats with per-contract flakiness ---


class Seats:
    """Scripted reviewer/verifier seats keyed by the eval contract id in
    the prompt (the F-16 ScriptedSeats pattern). Reviewers return the
    scripted findings for their diff (an empty list is a declared pass);
    the verifier keeps every finding. ``garbage_contracts`` make the
    FIRST reviewer call for a contract unparseable (the retry then sees
    the same prompt); ``always_garbage`` every call."""

    def __init__(self, findings=None, garbage_contracts=(), always_garbage=(),
                 quota_contracts=()):
        self.findings = dict(findings or {})
        self.garbage_contracts = set(garbage_contracts)
        self.always_garbage = set(always_garbage)
        self.quota_contracts = set(quota_contracts)
        self.calls: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.seen: dict[str, int] = {}

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        match = re.search(r'"id": "(EVAL-[^"]+)"', prompt)
        assert match, f"no eval contract id in the {seat} prompt"
        contract = match.group(1)
        self.prompts.append(prompt)
        self.calls.append((seat, contract))
        if seat == "reviewer":
            seen = self.seen.get(contract, 0) + 1
            self.seen[contract] = seen
            if contract in self.quota_contracts:
                return SessionRecord(
                    seat=seat, status="failed",
                    reason="usage limit reached (429)",
                )
            if contract in self.always_garbage or (
                contract in self.garbage_contracts and seen == 1
            ):
                return SessionRecord(
                    seat=seat, status="completed", num_turns=1, result=GARBAGE
                )
            findings = self.findings.get(contract, [])
            verdict = "fail" if findings else "pass"
            return SessionRecord(
                seat=seat,
                status="completed",
                num_turns=1,
                result=json.dumps({"verdict": verdict, "findings": findings}),
            )
        found = json.loads(_FINDINGS_ARRAY.search(prompt).group(1))
        votes = [
            {
                "id": f["id"],
                "keep": True,
                "reason": "confirmed against the diff",
                "tag": "important",
            }
            for f in found
        ]
        return SessionRecord(
            seat=seat,
            status="completed",
            num_turns=1,
            result=json.dumps({"votes": votes}),
        )


def _repo(tmp_path: Path):
    return build_soloadventurer_fixture(tmp_path / "soloadventurer")


def _clean_spec(clean_id: str):
    return next(d for d in CLEAN_DIFFS if d.id == clean_id)


def _reviewer_calls(seats):
    return [
        i for i, (seat, _) in enumerate(seats.calls) if seat == "reviewer"
    ]


def test_eval_review_retries_a_garbage_seat_once(tmp_path):
    repo = _repo(tmp_path)
    seats = Seats(garbage_contracts={"EVAL-clean-docstring"})
    record = run_seed(
        repo, _clean_spec("clean-docstring"), session_runner=seats, reviewers=1
    )
    l2 = record["layers"]["L2"]
    assert l2["review_retries"] == 1
    assert l2["verdict"] == "pass"  # the retry's verdict is the seat's
    assert l2["parse_error"] is None
    calls = _reviewer_calls(seats)
    assert len(calls) == 2  # the seat plus exactly one retry
    assert seats.prompts[calls[0]] == seats.prompts[calls[1]]  # same prompt


def test_eval_review_garbage_twice_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    seats = Seats(always_garbage={"EVAL-clean-docstring"})
    record = run_seed(
        repo, _clean_spec("clean-docstring"), session_runner=seats, reviewers=1
    )
    l2 = record["layers"]["L2"]
    assert l2["verdict"] == "fail"
    assert l2["parse_error"] is not None
    assert "reviewer 1" in l2["parse_error"]
    assert l2["review_retries"] == 1
    assert len(_reviewer_calls(seats)) == 2
    # The invalid union never reaches the verifier.
    assert all(seat != "verifier" for seat, _ in seats.calls)


def test_eval_all_valid_no_retry_same_seat_count(tmp_path):
    repo = _repo(tmp_path)
    seats = Seats()
    record = run_seed(
        repo, _clean_spec("clean-docstring"), session_runner=seats, reviewers=2
    )
    l2 = record["layers"]["L2"]
    assert l2["review_retries"] == 0
    assert l2["verdict"] == "pass"
    assert len(_reviewer_calls(seats)) == 2  # no third, hidden seat


def test_orchestrator_wires_the_retry_into_both_fan_outs():
    """Both orchestrator fan-outs — the round review and the final full
    review — parse through the shared helper with a re-run closure, and
    the retry leaves a trace event. A source pin in the F-16 style: the
    policy must not live in a second copy."""
    source = Path("factory/orchestrator.py").read_text()
    # import + the two fan-out sites
    assert source.count("parse_reviews_with_retry") >= 3
    assert '"reviewer_retry"' in source


# --- round 5: quota voids render honestly ------------------------------------


def _voided_report() -> dict:
    """Both seeds quota-voided on L2 (one caught by L0 anyway, one
    judged by nothing): the render must say so and never invent a
    verdict, a catch or a rate."""
    return {
        "fixture": "soloadventurer", "base_ref": "main", "l2": "sessions",
        "seeds": [
            {
                "id": "phantom-table", "title": "A phantom table",
                "expected_layer": "L0", "caught_by": ["L0"],
                "layers": {"L2": {"quota_voided": True,
                                  "review_retries": 1}},
            },
            {
                "id": "mocks-what-it-tests", "title": "A self-mocking test",
                "expected_layer": "L2", "caught_by": [],
                "layers": {"L2": {"quota_voided": True,
                                  "review_retries": 0}},
            },
        ],
        "clean": [],
        "l2_false_fail_on_clean": None,
        "l2_quota_voided": 2,
        "catch_rate": {"L0": 1.0, "L1": 0.0, "L2": None},
        "caught_counts": {"L0": 1, "L1": 0, "L2": 0},
    }


def test_a_quota_voided_seed_never_reads_not_caught():
    text = render_report(_voided_report())
    assert "NOT CAUGHT" not in text
    # Caught by another layer: the void is a parenthetical, not a miss.
    assert ("- seed phantom-table: caught by L0 (L2 quota-voided) "
            "(expected L0)") in text
    # Judged by nothing: the void IS the story.
    assert ("- seed mocks-what-it-tests: L2 quota-voided, not judged "
            "(expected L2)") in text


def test_a_layer_with_no_judged_seeds_is_na_not_a_zero_percent():
    rates = catch_rate_by_layer([
        {"caught_by": ["L0"],
         "layers": {"L0": {"verdict": "fail"},
                    "L2": {"quota_voided": True}}},
        {"caught_by": [],
         "layers": {"L0": {"verdict": "pass"},
                    "L2": {"quota_voided": True}}},
    ])
    assert rates["L2"] is None  # no denominator, not a 0% clean bill
    assert rates["L0"] == 0.5  # caught 1 of the 2 it judged
    text = render_report(_voided_report())
    assert "L2: n/a (0 judged)" in text
    assert "L2: 0%" not in text


# --- round 4 / finding 7: a quota-hit review is void, never a verdict ------


def test_eval_reports_a_quota_hit_review_as_void(tmp_path):
    """A diff whose reviewer seats (or re-runs) hit quota is VOID for
    L2: no verdict, excluded from the catch-rate and false-fail
    denominators, counted on its own line — F-16's tick evidence must
    not carry quota noise."""
    repo = _repo(tmp_path)
    seats = Seats(quota_contracts={"EVAL-clean-docstring"})
    record = run_seed(
        repo, _clean_spec("clean-docstring"), session_runner=seats,
        reviewers=1,
    )
    l2 = record["layers"]["L2"]
    assert l2.get("quota_voided") is True
    assert "verdict" not in l2  # never a verdict
    assert record["blocking_findings"] is None
    text = render_report(_report_with(record))
    assert "L2 quota-voided" in text


def _report_with(record: dict) -> dict:
    """A renderable report carrying one real run_seed record."""
    others = [
        {
            "id": "clean-new-helper", "title": "New helper",
            "blocking_findings": 0,
            "layers": {"L2": {"verdict": "pass", "parse_error": None,
                              "findings": [], "review_retries": 0}},
        }
    ]
    report = {
        "fixture": "soloadventurer", "base_ref": "main",
        "l2": "sessions", "seeds": [],
        "clean": [record, *others],
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 0.0},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 0},
    }
    return report


def test_seeded_eval_excludes_quota_voids_from_its_rates(tmp_path):
    """One seed and one clean diff hit quota: the seed leaves L2's catch
    denominator (0 of 5, not 0 of 6), the clean diff leaves the
    false-fail count (0 of 1, not 0 of 2), and both are counted on the
    quota-voided line instead — never as verdicts."""
    repo = _repo(tmp_path)
    report = run_seeded_eval(
        repo,
        session_runner=Seats(quota_contracts={
            "EVAL-tautological-test", "EVAL-clean-docstring"}),
    )
    assert report["l2_quota_voided"] == 2
    text = render_report(report)
    assert "L2 quota-voided: 2" in text
    assert "(0 of 5 seeds)" in text      # L2's rate over non-void seeds
    assert "L2 false-fail on clean diffs: 0 of 1" in text
    # The voided records themselves carry no verdict.
    voided = [r for r in report["seeds"] + report["clean"]
              if (r.get("layers") or {}).get("L2", {}).get("quota_voided")]
    assert len(voided) == 2
    assert all("verdict" not in r["layers"]["L2"] for r in voided)


# --- issue 4: the false-fail aggregate on clean diffs --------------------


def _report(clean_l2_by_id: dict) -> dict:
    clean = []
    for diff in CLEAN_DIFFS:
        l2 = clean_l2_by_id.get(diff.id)
        clean.append(
            {
                "id": diff.id,
                "title": diff.title,
                "blocking_findings": (
                    None if l2 is None
                    else sum(
                        1
                        for f in l2.get("findings") or []
                        if f.get("severity") == "blocking"
                    )
                ),
                "layers": {} if l2 is None else {"L2": l2},
            }
        )
    return {
        "fixture": "soloadventurer",
        "base_ref": "main",
        "l2": "sessions" if clean_l2_by_id else "skipped",
        "seeds": [],
        "clean": clean,
        "l2_false_fail_on_clean": None,
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 0.0},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 0},
    }


def test_render_counts_l2_false_fails_on_clean_diffs():
    report = _report(
        {
            "clean-docstring": {
                "verdict": "pass",
                "parse_error": None,
                "findings": [],
                "review_retries": 1,
            },
            "clean-new-helper": {
                "verdict": "fail",
                "parse_error": None,
                "findings": [{"id": "R-1", "severity": "blocking"}],
                "review_retries": 0,
            },
        }
    )
    text = render_report(report)
    assert "L2 false-fail on clean diffs: 1 of 2" in text
    assert "1 reviewer retry" in text  # the report says a retry fired


def test_render_false_fail_line_reads_zero_when_clean_passes():
    report = _report(
        {
            "clean-docstring": {
                "verdict": "pass", "parse_error": None,
                "findings": [], "review_retries": 0,
            },
            "clean-new-helper": {
                "verdict": "pass", "parse_error": None,
                "findings": [], "review_retries": 0,
            },
        }
    )
    text = render_report(report)
    assert "L2 false-fail on clean diffs: 0 of 2" in text
    assert "retry" not in text  # and none fired


def test_render_false_fail_line_is_na_when_l2_skipped():
    text = render_report(_report({}))
    assert "L2 false-fail on clean diffs: n/a" in text


def test_seeded_eval_json_counts_false_fails(tmp_path):
    repo = _repo(tmp_path)
    findings = {
        "EVAL-clean-docstring": [
            {
                "id": "R-1",
                "axis": "standards",
                "severity": "blocking",
                "file": "src/style.py",
                "text": "a style complaint on a known-clean diff",
            }
        ]
    }
    report = run_seeded_eval(repo, session_runner=Seats(findings))
    assert report["l2_false_fail_on_clean"] == 1
    assert "L2 false-fail on clean diffs: 1 of 2" in render_report(report)


def test_seeded_eval_json_false_fail_zero_and_na(tmp_path):
    repo = _repo(tmp_path)
    report = run_seeded_eval(repo, session_runner=Seats())
    assert report["l2_false_fail_on_clean"] == 0
    assert "L2 false-fail on clean diffs: 0 of 2" in render_report(report)
    skipped = run_seeded_eval(repo)
    assert skipped["l2_false_fail_on_clean"] is None
    assert "L2 false-fail on clean diffs: n/a" in render_report(skipped)
