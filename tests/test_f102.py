"""F-102 contract tests: every number in the eval report means what
its label says.

The 2026-11-18 baseline's "Drops that cost a catch: 0 of 1" counted
every dropped seed-naming finding on a missed run — including drops
that could never have produced the catch (a minor, a non-blocking
finding on a seed whose catch needs a blocking one). That is the
number F-96 is meant to move, so its before/after would blame or
credit the verifier for misses it did not cause. F-102 judges each
drop by the seed's OWN catch rule with that one finding restored, and
counts only the drops that pass it; a restore that cannot be judged
counts, with the reason named — the costlier error is an eval number
that flatters the review pipeline. The report also gains a slot → seed
table (a slot in a trace resolves without reading eval.py), the
deleted-await contract stops REQUESTING the defect, VALIDATOR-SPEC
names the gates' own output as an allowed marker source, and each
eval verifier session is capped at SEAT_TIMEOUT_MIN while the pool
window stays the admission check (the F-96 round-6 B item).

Real: the fixture repo, the seed branches, L0/L1. Scripted: every L2
seat. No network, no model session.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from _support.f16 import ScriptedSeats
from _support.f95 import Seats, SplitSeats, finding

from factory.eval import (
    CLEAN_DIFFS,
    FIXTURE_FILES,
    SEAT_TIMEOUT_MIN,
    SOLOADVENTURER_SEEDS,
    _clean_review_summary,
    build_soloadventurer_fixture,
    found_then_dropped,
    render_report,
    run_seed,
    run_seeded_eval,
    seed_by_id,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

CALLER = "caller-break"
CALLER_CONTRACT = seed_by_id(CALLER).slot
CALLER_FILE = "app/report.py"
SIBLING = "sibling-second-site"
SIBLING_CONTRACT = seed_by_id(SIBLING).slot
SIBLING_FILE = "app/gear.py"
MOCKS = "mocks-what-it-tests"
MOCKS_CONTRACT = seed_by_id(MOCKS).slot
DELETED_AWAIT = "deleted-await"

# The sentence AC-4 pins in VALIDATOR-SPEC's F-99 paragraph (the same
# pin-by-exact-line rule test_f96 applies to its prompt lines).
MARKER_SOURCE_PIN = (
    'The one exemption is the gates\' own output over the code: a '
    'marker in L0/L1\'s recorded evidence (Python\'s "coroutine" in a '
    'failing test\'s output) is the gate\'s finding, never the eval '
    'describing the defect.'
)

NEW_AWAIT_TITLE = "tidy the checkin-count fetch"


def _fixture(tmp_path: Path, name: str = "soloadventurer") -> Path:
    return build_soloadventurer_fixture(tmp_path / name)


def _caller_finding(**over) -> dict:
    base = dict(fid="R-3", file=CALLER_FILE,
                text="pack_summary still reads i['item'] but pack_list "
                     "now returns plain names: TypeError on every call",
                severity="blocking")
    base.update(over)
    fid = base.pop("fid")
    return finding(fid, **base)


def _sibling_finding(**over) -> dict:
    base = dict(fid="R-2", file=SIBLING_FILE,
                text="gear_for_owner accepts a whitespace-only owner_id: "
                     "`not owner_id` passes '   ' (same class as the fix)",
                sibling=True)
    base.update(over)
    fid = base.pop("fid")
    return finding(fid, **base)


def _entry(repo: Path, record: dict, seed) -> dict:
    """The found_then_dropped entry exactly as run_seeded_eval stamps
    it (F-95's shape, F-99's verdict, F-102's per-drop judgement)."""
    return {"id": seed.id, "repeat": 1,
            "dropped": found_then_dropped(record, seed, root=repo),
            "seed_caught": bool(record.get("caught_by"))}


def _report(records: list, entries: list) -> dict:
    """A renderable report over real run records (the render path only
    needs the F-95 keys; the slot table is proven on the real report in
    test_ac_2)."""
    return {
        "fixture": "soloadventurer", "base_ref": "main", "l2": "sessions",
        "repeats": 1, "seeds": records, "clean": [], "l2_quota_voided": 0,
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 0.0},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 0},
        "found_then_dropped": entries,
    }


# --------------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """A drop costs a catch only if keeping it would have caught the
    seed: each found-then-dropped finding on a missed run is judged by
    the seed's own catch rule with that ONE finding restored — _blocks
    for caller-break, the sibling rule for the sibling seed, F-16's
    marker rule plus a failing verdict for the rest — and the count
    line counts only the drops that pass it. A restore that cannot be
    judged COUNTS with the reason named; a drop on a caught run never
    enters the line. Old reports (no field) render as today."""
    repo = _fixture(tmp_path)
    seed = seed_by_id(CALLER)

    # a) a dropped MINOR on a missed caller-break run: restoring it can
    # never block the round — not counted.
    minor = run_seed(repo, seed, session_runner=SplitSeats(
        CALLER_CONTRACT, first=[],
        rest=[_caller_finding(severity="minor")], drops={"U-1"}))
    assert minor["caught_by"] == []
    [row] = _entry(repo, minor, seed)["dropped"]
    assert row["would_have_caught"] is False, row

    # b) a dropped BLOCKING caller finding on a missed run: the round
    # would have failed on the caller — counted.
    blocking = run_seed(repo, seed, session_runner=SplitSeats(
        CALLER_CONTRACT, first=[],
        rest=[_caller_finding(severity="blocking")], drops={"U-1"}))
    assert blocking["caught_by"] == []
    [row] = _entry(repo, blocking, seed)["dropped"]
    assert row["would_have_caught"] is True, row

    # c) a dropped flagged sibling on the untouched site of a missed
    # sibling run: the round passes, the sibling survives — counted.
    # The same finding UNFLAGGED restores to a blocked round, and a
    # blocked round is never a sibling catch — not counted.
    sib_seed = seed_by_id(SIBLING)
    flagged = run_seed(repo, sib_seed, session_runner=SplitSeats(
        SIBLING_CONTRACT, first=[], rest=[_sibling_finding()],
        drops={"U-1"}))
    assert flagged["caught_by"] == []
    [row] = _entry(repo, flagged, sib_seed)["dropped"]
    assert row["would_have_caught"] is True, row
    unflagged = run_seed(repo, sib_seed, session_runner=SplitSeats(
        SIBLING_CONTRACT, first=[],
        rest=[_sibling_finding(sibling=None, severity="blocking")],
        drops={"U-1"}))
    [row] = _entry(repo, unflagged, sib_seed)["dropped"]
    assert row["would_have_caught"] is False, row

    # d) two drops on one missed run, only one of which restores to a
    # catch: the count is per drop, N=1 of 2. (Distinct texts: the
    # union dedupes same-content findings into one.)
    two = run_seed(repo, seed, session_runner=SplitSeats(
        CALLER_CONTRACT, first=[],
        rest=[_caller_finding(fid="R-1", severity="blocking"),
              _caller_finding(fid="R-2", severity="minor",
                              text="pack_summary TypeError: pack_list "
                                   "returns plain names now")],
        drops={"U-1", "U-2"}))
    rows = {d["id"]: d["would_have_caught"]
            for d in _entry(repo, two, seed)["dropped"]}
    assert rows == {"U-1": True, "U-2": False}, rows

    # e) a drop on a CAUGHT run (the surviving twin caught the seed):
    # rendered with its parenthetical, never in the count line.
    caught = run_seed(repo, seed, session_runner=Seats(
        {CALLER_CONTRACT: [_caller_finding(fid="R-1"),
                           _caller_finding(fid="R-2", severity="minor",
                                           text="pack_summary TypeError: "
                                                "pack_list returns plain "
                                                "names now")]},
        drops={CALLER_CONTRACT: {"U-2"}}))
    assert caught["caught_by"] == ["L2"]
    caught_entry = _entry(repo, caught, seed)
    assert caught_entry["seed_caught"] is True
    assert len(caught_entry["dropped"]) == 1

    # f) a restore that cannot be judged COUNTS, reason named — the
    # flattering 0 is the costlier error. Both shapes: a drop with no
    # file on a seed whose catch needs one, and a drop with no severity
    # on a passing round. (Hand-stored records: a parsed finding always
    # carries both — these are the report-read path's shapes.)
    hand_nofile = {
        "id": CALLER, "title": CALLER, "expected_layer": "L2",
        "layers": {"L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
                   "L2": {"verdict": "pass", "findings": [],
                          "verifier_dropped": [{
                              "reason": "not a real defect",
                              "finding": {
                                  "id": "U-1",
                                  "text": "pack_summary still reads "
                                          "i['item'] but pack_list now "
                                          "returns plain names",
                                  "severity": "minor"}}]}},
        "caught_by": [],
    }
    hand_nosev = {
        "id": CALLER, "title": CALLER, "expected_layer": "L2",
        "layers": {"L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
                   "L2": {"verdict": "pass", "findings": [],
                          "verifier_dropped": [{
                              "reason": "not a real defect",
                              "finding": {
                                  "id": "U-1", "file": CALLER_FILE,
                                  "text": "pack_summary still reads "
                                          "i['item'] but pack_list now "
                                          "returns plain names"}}]}},
        "caught_by": [],
    }
    [row] = found_then_dropped(hand_nofile, seed)
    assert row["would_have_caught"] is True and row["undecidable"], row
    [row] = found_then_dropped(hand_nosev, seed)
    assert row["would_have_caught"] is True and row["undecidable"], row

    # g) the rendered report: N counts only would_have_caught over the
    # MISSED runs' drops (the caught run's drop never enters the line);
    # each undecidable row names why it counted.
    entries = [
        _entry(repo, minor, seed), _entry(repo, blocking, seed),
        _entry(repo, two, seed), _entry(repo, flagged, sib_seed),
        _entry(repo, unflagged, sib_seed), caught_entry,
        {"id": CALLER, "repeat": 1,
         "dropped": found_then_dropped(hand_nofile, seed),
         "seed_caught": False},
        {"id": CALLER, "repeat": 1,
         "dropped": found_then_dropped(hand_nosev, seed),
         "seed_caught": False},
    ]
    text = render_report(_report(
        [minor, blocking, two, flagged, unflagged, caught], entries))
    # 8 drops on missed runs (1+1+2+1+1+1+1), 5 of which restore to a
    # catch (blocking, U-1 of the pair, the flagged sibling, both
    # undecidables); the caught run's drop is not in the line at all.
    assert "Drops that cost a catch: 5 of 8" in text, (
        [line for line in text.splitlines() if "cost a catch" in line])
    assert "(seed still caught)" in text and "(seed missed)" in text
    undecidable_lines = [line for line in text.splitlines()
                         if "counted undecidable" in line]
    assert len(undecidable_lines) == 2, undecidable_lines

    # h) old reports render as today: F-99-shaped entries (seed_caught,
    # no would_have_caught) keep the F-99 count over ALL shown drops;
    # pre-F-99 entries render no parenthetical and no count line.
    f99_shaped = _report([], [
        {"id": CALLER, "repeat": 1,
         "dropped": [{"id": "R-1", "file": CALLER_FILE,
                      "text": "the defect", "reason": "not a real defect"}],
         "seed_caught": False},
        {"id": CALLER, "repeat": 1,
         "dropped": [{"id": "R-2", "file": CALLER_FILE,
                      "text": "the defect", "reason": "not a real defect"}],
         "seed_caught": True},
    ])
    f99_text = render_report(f99_shaped)
    assert "Drops that cost a catch: 1 of 2" in f99_text
    pre_f99 = _report([], [
        {"id": CALLER, "repeat": 1,
         "dropped": [{"id": "R-1", "file": CALLER_FILE,
                      "text": "the defect", "reason": "not a real defect"}]},
    ])
    pre_text = render_report(pre_f99)
    assert "cost a catch" not in pre_text
    assert "(seed missed)" not in pre_text

    # i) a hand-edited MIXED report: a missed-run drop with no verdict
    # COUNTS (undecided is the costly side), a judged False does not.
    mixed = _report([], [
        {"id": CALLER, "repeat": 1,
         "dropped": [{"id": "R-1", "file": CALLER_FILE,
                      "text": "the defect", "reason": "not a real defect"}],
         "seed_caught": False},
        {"id": CALLER, "repeat": 2,
         "dropped": [{"id": "R-2", "file": CALLER_FILE,
                      "text": "the defect", "reason": "not a real defect",
                      "would_have_caught": False}],
         "seed_caught": False},
    ])
    assert "Drops that cost a catch: 1 of 2" in render_report(mixed)


# --------------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path: Path):
    """The report maps slots to seeds: EVAL-07 in a session trace
    resolves to its seed id without reading eval.py. The table rides
    the report OUTSIDE the F-16 record shape (one top-level `slots`
    key — the only new key — the per-diff records stay byte-for-byte),
    and an old report without it renders exactly as before."""
    repo = _fixture(tmp_path)
    report = run_seeded_eval(repo, session_runner=ScriptedSeats())
    expected = {spec.slot: spec.id
                for spec in [*SOLOADVENTURER_SEEDS, *CLEAN_DIFFS]}
    assert len(expected) == 11, expected
    assert report["slots"] == expected
    # The new top-level keys: F-102's slots and F-100's role_overrides
    # (the eval's --role record); everything else is the F-99 shape.
    assert set(report) - {"slots", "role_overrides"} == {
        "fixture", "base_ref", "l2", "repeats", "seeds", "clean",
        "false_fail_on_clean", "false_fail_by_repeat", "false_fail_stats",
        "l2_quota_voided", "catch_rate", "caught_counts",
        "catch_rate_by_repeat", "catch_rate_stats", "per_seed",
        "found_then_dropped"}
    assert json.loads(json.dumps(report["slots"])) == report["slots"]

    text = render_report(report)
    table = [line for line in text.splitlines()
             if re.fullmatch(r"- EVAL-\d{2} → \S+", line)]
    assert len(table) == 11, table
    assert sorted(line.split()[1] for line in table) == sorted(expected)
    assert "- EVAL-07 → sibling-second-site" in text
    assert "- EVAL-08 → caller-break" in text
    assert "- EVAL-11 → clean-beside-old-site" in text
    assert "- EVAL-04 → deleted-await" in text

    # An old report (no slots key) renders without the section — the
    # baseline-shaped JSON stays byte-identical under the new renderer.
    old = {k: v for k, v in report.items() if k != "slots"}
    old_text = render_report(old)
    assert "Slot" not in old_text and "EVAL-07 →" not in old_text


# --------------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path):
    """deleted-await's contract stops REQUESTING the change: the title
    becomes neutral intent — never naming the defect, never asking for
    it, no marker the diff and fixture don't themselves carry — and the
    F-99 leak sweep stays green on the new title (the owner compares
    this seed by hand against the baseline, where L0 caught it 3/3)."""
    seed = seed_by_id(DELETED_AWAIT)
    title = seed.contract_title
    assert title == NEW_AWAIT_TITLE, title
    # Never names the defect class, never requests it.
    for word in ("await", "async", "synchron", "coroutine", "drop"):
        assert word not in title.lower(), (word, title)
    assert title.strip() and title != seed.title

    # The pipeline prompts with it, and nothing about the defect leaks:
    # the seed's id, the eval's own label and the OLD requesting title
    # are absent from every seat's prompt (user and system), and no
    # marker rides the prompt unless the fixture, the diff or the layer
    # evidence already carries it (the F-99 rule, this seed only).
    repo = _fixture(tmp_path)
    captured: list[tuple[str, str, str]] = []

    class _Capture(ScriptedSeats):
        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            captured.append((seat, prompt, str(kwargs.get("system_prompt")
                                               or "")))
            return super().__call__(seat, prompt, run_dir, profile,
                                    **kwargs)

    generic = {"id": "R-1", "axis": "standards", "severity": "minor",
               "file": "app/api.py",
               "text": "a usage example in the docstring would help"}
    record = run_seed(repo, seed, session_runner=_Capture(
        {seed.slot: [generic]}))
    assert captured, "no seat ran"
    allowed = "\n".join(
        list(FIXTURE_FILES.values())
        + [old + "\n" + new for old, new in seed.edits]
        + [json.dumps(record["layers"]["L0"]),
           json.dumps(record["layers"]["L1"])])
    for seat, prompt, system in captured:
        for text in (prompt, system):
            assert seed.id not in text, (seat, seed.id)
            assert seed.title not in text, (seat, seed.title[:30])
            assert "make the checkin-count fetch synchronous" not in text
            for marker in seed.markers:
                if marker not in allowed:
                    assert marker not in text, (seat, marker)
    reviewer_prompt = next(p for seat, p, _ in captured
                           if seat == "reviewer")
    assert f'"title": "{title}"' in reviewer_prompt


# --------------------------------------------------------------------- AC-4 --


def test_ac_4():
    """VALIDATOR-SPEC's F-99 paragraph names the gates' own output as
    an allowed marker source (Python's "coroutine" in a failing test's
    output is the gate's evidence, never the eval describing the
    defect) — pinned by this doc line, the same way test_f96 pins its
    prompt lines."""
    doc = (REPO_ROOT / "VALIDATOR-SPEC.md").read_text()
    assert MARKER_SOURCE_PIN in doc


# --------------------------------------------------------------------- AC-5 --


def test_ac_5(tmp_path: Path, monkeypatch):
    """Each eval verifier session's timeout is capped at
    SEAT_TIMEOUT_MIN (one hung session can no longer run the whole
    pool window) while the WINDOW — SEAT_TIMEOUT_MIN x sessions —
    stays the admission check, so a starved pool still voids the
    review; and the voided line reads "L2 voided (no verdict)
    (<reason>)" for quota and starvation alike (the old line called a
    starved window a quota void)."""
    repo = _fixture(tmp_path)
    seed = seed_by_id(MOCKS)
    minors = [finding(f"F102-{i}", "tests/test_api.py",
                      f"wording nit {i}", severity="minor")
              for i in range(1, 13)]

    # a) a normal pool: 12 findings, one verifier session each, every
    # session's timeout exactly the per-seat budget — never the window.
    captured: list[tuple[str, float | None]] = []

    class _Capture(Seats):
        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            captured.append((seat, kwargs.get("timeout_min")))
            return super().__call__(seat, prompt, run_dir, profile,
                                    **kwargs)

    record = run_seed(repo, seed, session_runner=_Capture(
        {MOCKS_CONTRACT: minors}))
    assert record["layers"]["L2"]["verdict"], record["layers"]["L2"]
    verifier_timeouts = [t for seat, t in captured if seat == "verifier"]
    assert len(verifier_timeouts) == 12, captured
    assert all(t == SEAT_TIMEOUT_MIN for t in verifier_timeouts), \
        verifier_timeouts
    assert all(t is not None and t <= SEAT_TIMEOUT_MIN
               for _, t in captured), captured

    # b) the starved arm still voids: the pool window stays the
    # admission check, so a clock that starves it refuses the next
    # session and the review is void with its reason on the record.
    from factory import orchestrator

    class _Burn:
        def __init__(self):
            self.step = 0.0
            self._now = 1000.0

        def monotonic(self):
            self._now += self.step
            return self._now

    fake = _Burn()
    monkeypatch.setattr(orchestrator, "time", fake)
    verifier_calls: list[int] = []

    class _StarveAfterFirst(ScriptedSeats):
        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            seat_record = super().__call__(seat, prompt, run_dir, profile,
                                           **kwargs)
            if seat == "verifier":
                verifier_calls.append(len(verifier_calls) + 1)
                if len(verifier_calls) == 1:
                    fake.step = 1e9
            return seat_record

    voided = run_seed(repo, seed, session_runner=_StarveAfterFirst(
        {MOCKS_CONTRACT: minors}))
    l2 = voided["layers"]["L2"]
    assert l2["quota_voided"] is True, l2
    assert l2["void_reason"] == "verifier_window_starved", l2
    assert verifier_calls, "the starved arm never ran a verifier session"
    monkeypatch.setattr(orchestrator, "time", __import__("time"))

    # c) the voided line: "L2 voided (no verdict) (<reason>)" — the
    # same shape for a quota void and a starved one.
    quota_line = _clean_review_summary({"layers": {
        "L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
        "L2": {"quota_voided": True, "void_reason": "verifier_quota"}}})
    assert quota_line == ("L0 pass, L1 pass, L2 voided (no verdict) "
                          "(verifier_quota)"), quota_line
    starved_line = _clean_review_summary({"layers": {
        "L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
        "L2": {"quota_voided": True,
               "void_reason": "verifier_window_starved"}}})
    assert starved_line == ("L0 pass, L1 pass, L2 voided (no verdict) "
                            "(verifier_window_starved)"), starved_line
