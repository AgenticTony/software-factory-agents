"""F-95 contract tests: the eval measures what F-94 added.

F-94 gave L2 a defect hunt: seat 2 reports other sites of a defect
class, and a finding the reviewer flags ``sibling`` on a file outside
the diff never blocks. No eval case covered it, and the eval could not
even score a correct sibling find — a layer only "caught" a seed by
failing, and a sibling must never fail the round.

F-95 adds three diffs shaped like misses the factory actually had,
each on its OWN base (a setup commit off the shared fixture, which
stays as it is):

* ``sibling-second-site`` — JobFinderOS run 4 (#176): one defect class
  at two sites in two files, the diff fixes one. Caught only when L2
  PASSES and a surviving finding flagged sibling names the untouched
  site's file and a marker.
* ``caller-break`` — #163 round 1: the diff changes a function's return
  shape and leaves an unchanged caller in another file broken. Caught
  only on a FAILED round with a surviving finding on the caller's file.
* ``clean-beside-old-site`` — a clean diff whose base already carries
  an old site of the class: a sibling finding there is no false fail.

And the report gains the honest numbers: per seed ``caught k/N
repeats``, the pass^k line, "never caught", and per seed and repeat
"found, then dropped" (a seat reported the seeded defect and the
verifier dropped it — F-96's baseline).

Real: the fixture repo, the seed branches, L0/L1. Scripted: every L2
seat. No network, no model session.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from _support.f16 import MOCK_SEED_FINDINGS
from _support.f95 import Seats, SplitSeats, finding, normalised

from factory.eval import (
    CLEAN_DIFFS,
    SOLOADVENTURER_SEEDS,
    CleanSpec,
    SeedSpec,
    build_soloadventurer_fixture,
    render_report,
    run_seed,
    run_seeded_eval,
    seed_by_id,
)

SIBLING = "sibling-second-site"
SIBLING_CONTRACT = seed_by_id(SIBLING).slot  # F-99: slot, not id
FIXED_FILE = "app/owners.py"     # the site the seed diff fixes
SIBLING_FILE = "app/gear.py"     # the untouched second site
CALLER = "caller-break"
CALLER_CONTRACT = seed_by_id(CALLER).slot
CALLER_FILE = "app/report.py"    # the unchanged, broken caller
CHANGED_FILE = "app/api.py"      # where the contract changed
CLEAN_OLD = "clean-beside-old-site"

# The F-16 bug-class set as it stood before F-95: its records must not
# move by a byte when the F-95 diffs run beside them.
F16_SEEDS = ("phantom-table", "removed-rls-filter", "off-by-one",
             "deleted-await", "mocks-what-it-tests", "tautological-test")
F16_CLEAN = ("clean-docstring", "clean-new-helper")


def _fixture(tmp_path: Path, name: str = "soloadventurer") -> Path:
    return build_soloadventurer_fixture(tmp_path / name)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _sibling_finding(**over) -> dict:
    base = dict(fid="R-2", file=SIBLING_FILE,
                text="gear_for_owner accepts a whitespace-only owner_id: "
                     "`not owner_id` passes '   ' (same class as the fix)",
                sibling=True)
    base.update(over)
    fid = base.pop("fid")
    return finding(fid, **base)


def _run_sibling(repo: Path, findings: list | None, *, reviewers: int = 2,
                 drops: set[str] | None = None, seats=None) -> dict:
    seats = seats or Seats(
        {SIBLING_CONTRACT: findings or []},
        drops={SIBLING_CONTRACT: drops or set()},
    )
    return run_seed(repo, seed_by_id(SIBLING), session_runner=seats,
                    reviewers=reviewers)


def _surviving(record: dict) -> list:
    return record["layers"]["L2"]["findings"]


# --------------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """The sibling seed, run-4 shaped: two sites of one class in two
    files on the seed's own base, the diff fixes one. Caught only on a
    PASSING round with a surviving sibling on the untouched site's file
    naming a marker; every other shape scores zero and the record says
    why. The shared fixture and every other diff's record are untouched."""
    from factory.eval import SiblingSeed, found_then_dropped

    repo = _fixture(tmp_path)
    seed = seed_by_id(SIBLING)
    assert isinstance(seed, SiblingSeed) and seed.layer == "L2"
    assert seed.sibling_file == SIBLING_FILE

    # a) the defect hunt reports the second site, flagged: caught, passes.
    hit = _run_sibling(repo, [_sibling_finding()])
    l2 = hit["layers"]["L2"]
    assert l2["verdict"] == "pass", l2
    assert hit["caught_by"] == ["L2"]
    assert hit["sibling_check"]["caught"] is True
    [kept] = _surviving(hit)
    assert kept["sibling"] is True and kept["file"] == SIBLING_FILE

    # The seed's shape on its own base: the setup commit plants both
    # sites; the diff touches the fixed site only; the shared fixture
    # (main) has neither file.
    base = hit["base"]
    assert base != "main"
    changed = _git(repo, "diff", "--name-only",
                   f"{base}...eval/{seed_by_id(SIBLING).slot}").stdout.split()
    assert changed == [FIXED_FILE], changed
    for rel in (FIXED_FILE, SIBLING_FILE):
        assert "if not owner_id:" in _git(repo, "show",
                                          f"{base}:{rel}").stdout, rel
        assert _git(repo, "cat-file", "-e", f"main:{rel}").returncode != 0
    assert "if not owner_id:" in _git(
        repo, "show",
        f"eval/{seed_by_id(SIBLING).slot}:{SIBLING_FILE}").stdout

    # b) the same finding WITHOUT the flag blocks: the round fails, and a
    # blocked round is never a sibling catch.
    blocked = _run_sibling(repo, [_sibling_finding(sibling=None)])
    assert blocked["layers"]["L2"]["verdict"] == "fail"
    assert blocked["caught_by"] == []
    assert blocked["sibling_check"]["caught"] is False
    assert "fail" in blocked["sibling_check"]["reason"]

    # c) no finding at all.
    silent = _run_sibling(repo, [])
    assert silent["layers"]["L2"]["verdict"] == "pass"
    assert silent["caught_by"] == []
    assert "no surviving finding" in silent["sibling_check"]["reason"]

    # d) the flagged finding on the FIXED site's file (inside the diff):
    # the flag lowers nothing there — the round fails, not caught.
    wrong_file = _run_sibling(repo, [_sibling_finding(file=FIXED_FILE)])
    assert wrong_file["layers"]["L2"]["verdict"] == "fail"
    assert wrong_file["caught_by"] == []

    # e) a sibling on the right file that names no marker.
    vague = _run_sibling(repo, [_sibling_finding(text="style nit here")])
    assert vague["layers"]["L2"]["verdict"] == "pass"
    assert vague["caught_by"] == []
    assert "marker" in vague["sibling_check"]["reason"]

    # f) a sibling on a third, unrelated untouched file.
    elsewhere = _run_sibling(
        repo, [_sibling_finding(file="app/store.py")])
    assert elsewhere["caught_by"] == []

    # f2) the second site spelled as an absolute path inside the clone:
    # the parent rules it a sibling (F-94), and the catch counts it too.
    absolute = _run_sibling(
        repo, [_sibling_finding(file=str(repo / SIBLING_FILE))])
    assert absolute["layers"]["L2"]["verdict"] == "pass"
    assert absolute["caught_by"] == ["L2"]

    # g) the verifier drops the sibling (reported by the hunt seat only,
    # so not unanimous and the drop stands): only survivors count — not
    # caught, and recorded as found, then dropped.
    dropped = _run_sibling(repo, None, seats=SplitSeats(
        SIBLING_CONTRACT, first=[], rest=[_sibling_finding()],
        drops={"U-1"}))  # the union's id: merge_reviews renumbers
    assert dropped["layers"]["L2"]["findings"] == []
    assert dropped["caught_by"] == []
    assert "dropped" in dropped["sibling_check"]["reason"]
    [entry] = found_then_dropped(dropped, seed)
    assert entry["file"] == SIBLING_FILE and entry["id"] == "U-1"

    # g2) both seats report the major sibling and the verifier votes to
    # drop it: the unanimous rule refuses the drop — it survives, is
    # caught, and is NOT found-then-dropped (it was never dropped).
    refused = _run_sibling(repo, [_sibling_finding()], drops={"U-1"})
    assert refused["caught_by"] == ["L2"]
    assert any(str(e.get("reason", "")).startswith("override_refused")
               for e in refused["layers"]["L2"]["verifier_dropped"])
    assert found_then_dropped(refused, seed) == []

    # g3) the verifier drops a finding that names a marker but sits on
    # the FIXED site's file: not the seeded defect's second site, so not
    # found-then-dropped (R2-2).
    wrong_drop = _run_sibling(repo, None, seats=SplitSeats(
        SIBLING_CONTRACT, first=[],
        rest=[_sibling_finding(file=FIXED_FILE, severity="minor")],
        drops={"U-1"}))
    assert wrong_drop["layers"]["L2"]["verifier_dropped"], wrong_drop
    assert found_then_dropped(wrong_drop, seed) == []

    # h) the round FAILED with the sibling present (the other seat
    # blocked on an in-diff finding): not a sibling catch.
    in_diff = finding("R-7", FIXED_FILE, "require_owner raises the wrong "
                      "error type", severity="blocking")
    split = _run_sibling(repo, None, seats=SplitSeats(
        SIBLING_CONTRACT, first=[in_diff], rest=[_sibling_finding()]))
    assert split["layers"]["L2"]["verdict"] == "fail"
    assert any(f.get("sibling") for f in _surviving(split))
    assert split["caught_by"] == []
    assert "fail" in split["sibling_check"]["reason"]

    # i) one seat carries both lenses: still catchable.
    solo = _run_sibling(repo, [_sibling_finding()], reviewers=1)
    assert solo["caught_by"] == ["L2"]

    # j) no L2 at all: not caught, and the record says L2 never ran.
    no_l2 = run_seed(repo, seed)
    assert no_l2["caught_by"] == []
    assert "L2" in no_l2["sibling_check"]["reason"]

    _isolation(tmp_path)


def _isolation(tmp_path: Path):
    """AC-1's pin: the setup commits stay on their seeds' own bases. Run
    in either order, every F-16 record is identical, and no other
    seed's branch (nor main) carries a setup file."""
    def run(repo: Path, order: list) -> dict:
        seats = Seats(MOCK_SEED_FINDINGS)
        out = {}
        for spec in order:
            out[spec.id] = normalised(
                run_seed(repo, spec, session_runner=seats), root=repo)
        return out

    old = [s for s in SOLOADVENTURER_SEEDS if s.id in F16_SEEDS] + [
        d for d in CLEAN_DIFFS if d.id in F16_CLEAN]
    new = [s for s in SOLOADVENTURER_SEEDS + CLEAN_DIFFS
           if s.id not in F16_SEEDS + F16_CLEAN]
    assert {s.id for s in new} == {SIBLING, CALLER, CLEAN_OLD}

    first = _fixture(tmp_path, "a")
    alone = run(first, old)
    run(first, new)
    again = run(first, list(reversed(old)))
    assert again == alone

    second = _fixture(tmp_path, "b")
    run(second, new)
    after = run(second, old)
    assert after == alone

    for repo in (first, second):
        for spec in old:
            tree = _git(repo, "ls-tree", "-r", "--name-only",
                        f"eval/{spec.slot}").stdout.split()
            for rel in (FIXED_FILE, SIBLING_FILE, CALLER_FILE):
                assert rel not in tree, (spec.id, rel)
        main_tree = _git(repo, "ls-tree", "-r", "--name-only",
                         "main").stdout.split()
        assert not {FIXED_FILE, SIBLING_FILE, CALLER_FILE} & set(main_tree)


# --------------------------------------------------------------------- AC-2 --


def _run_caller(repo: Path, findings: list, **kw) -> dict:
    return run_seed(repo, seed_by_id(CALLER),
                    session_runner=Seats({CALLER_CONTRACT: findings}), **kw)


def _caller_finding(**over) -> dict:
    base = dict(fid="R-3", file=CALLER_FILE,
                text="pack_summary still reads i['item'] but pack_list now "
                     "returns plain names: TypeError on every call",
                severity="blocking")
    base.update(over)
    fid = base.pop("fid")
    return finding(fid, **base)


def test_ac_2(tmp_path: Path):
    """The caller-break seed (#163 round 1): an ordinary L2 SeedSpec,
    caught only on a FAILED round with a surviving finding on the
    caller's file naming a marker. The tests still pass — the caller is
    untested — so L0 must not be what catches it. Every run says why
    on ``caller_check`` (R2-1), the F-94 residual rendered visibly."""
    from factory.eval import found_then_dropped

    repo = _fixture(tmp_path)
    seed = seed_by_id(CALLER)
    assert type(seed) is SeedSpec and seed.layer == "L2"

    hit = _run_caller(repo, [_caller_finding()])
    assert hit["layers"]["L0"]["verdict"] == "pass", hit["layers"]["L0"]
    assert hit["layers"]["L2"]["verdict"] == "fail"
    assert hit["caught_by"] == ["L2"]
    base = hit["base"]
    changed = _git(repo, "diff", "--name-only",
                   f"{base}...eval/{seed_by_id(CALLER).slot}").stdout.split()
    assert CALLER_FILE not in changed and CHANGED_FILE in changed, changed
    assert "pack_summary" in _git(repo, "show",
                                  f"{base}:{CALLER_FILE}").stdout

    # The forged flag on an IN-DIFF finding lowers nothing (F-94's
    # two-condition rule): the round still fails.
    forged_in_diff = _run_caller(repo, [
        _caller_finding(fid="R-4", file=CHANGED_FILE, sibling=True,
                        text="pack_list changed its return shape; "
                             "pack_summary is not updated")])
    assert forged_in_diff["layers"]["L2"]["verdict"] == "fail"
    # ... but a finding on the changed file is not the caller catch.
    assert forged_in_diff["caught_by"] == []

    # A finding naming the caller, filed on the changed file only:
    # the round fails, not caught (the catch needs the caller's file).
    on_changed = _run_caller(repo, [_caller_finding(file=CHANGED_FILE)])
    assert on_changed["layers"]["L2"]["verdict"] == "fail"
    assert on_changed["caught_by"] == []

    # No finding: L2 passes, nothing caught.
    silent = _run_caller(repo, [])
    assert silent["caught_by"] == []

    # The forged flag ON THE CALLER (an untouched file) is F-94's named
    # residual: flag + outside-the-diff makes a sibling, so the round
    # passes — and a passing round is never a caller-break catch.
    forged = _run_caller(repo, [_caller_finding(sibling=True)])
    assert forged["layers"]["L2"]["verdict"] == "pass"
    assert forged["caught_by"] == []

    # R2-1: every caller-break run says why it was (not) caught — the
    # residual above above all, which the live eval must tell apart from
    # "never saw it" and "found, then dropped".
    assert hit["caller_check"] == {
        "caught": True,
        "reason": "the round failed on a finding on the caller"}
    assert forged["caller_check"]["caught"] is False
    assert forged["caller_check"]["residual"] is True
    assert "ruled sibling" in forged["caller_check"]["reason"]
    assert "F-94 residual" in forged["caller_check"]["reason"]
    assert "no surviving finding" in silent["caller_check"]["reason"]
    assert "no surviving finding" in on_changed["caller_check"]["reason"]
    vague = _run_caller(repo, [_caller_finding(text="a long line here")])
    assert vague["caught_by"] == []
    assert "names no marker" in vague["caller_check"]["reason"]
    assert "residual" not in vague["caller_check"]
    assert "caller_check" not in run_seed(
        repo, seed_by_id("off-by-one"))  # F-16 records untouched
    no_l2 = run_seed(repo, seed)
    assert "L2" in no_l2["caller_check"]["reason"]

    # Found, then dropped: one seat reports the break, the verifier
    # votes it out — not caught, and the reason says so.
    dropped = run_seed(repo, seed, session_runner=SplitSeats(
        CALLER_CONTRACT, first=[], rest=[_caller_finding()],
        drops={"U-1"}))
    assert dropped["caught_by"] == []
    assert "dropped" in dropped["caller_check"]["reason"]
    assert [d["id"] for d in found_then_dropped(dropped, seed)] == ["U-1"]
    # R2-2: a dropped finding naming the caller but filed on the CHANGED
    # file is not the seeded defect — not found-then-dropped.
    wrong_drop = run_seed(repo, seed, session_runner=SplitSeats(
        CALLER_CONTRACT, first=[],
        rest=[_caller_finding(file=CHANGED_FILE)], drops={"U-1"}))
    assert wrong_drop["layers"]["L2"]["verifier_dropped"], wrong_drop
    assert found_then_dropped(wrong_drop, seed) == []

    # R3: a caller break that did not itself block is never the catch,
    # even when something ELSE failed the round. (a) an unrelated major
    # on the changed file fails the round, and the caller break is ruled
    # sibling (non-blocking); (b) the same unrelated fail, the caller
    # break rated minor. Both: not caught, with the reason that fits.
    unrelated = finding("R-9", CHANGED_FILE,
                        "archive_trip lacks a docstring example",
                        severity="major")
    masked = _run_caller(repo, [unrelated, _caller_finding(
        fid="R-3", severity="major", sibling=True)])
    assert masked["layers"]["L2"]["verdict"] == "fail"
    assert masked["caught_by"] == []
    assert masked["caller_check"]["residual"] is True
    assert "F-94 residual" in masked["caller_check"]["reason"]
    minor = _run_caller(repo, [unrelated, _caller_finding(
        fid="R-3", severity="minor")])
    assert minor["layers"]["L2"]["verdict"] == "fail"
    assert minor["caught_by"] == []
    assert "did not fail" in minor["caller_check"]["reason"]
    assert "minor" in minor["caller_check"]["reason"]
    # The positive control: the caller break as an unflagged major beside
    # the same unrelated major — it fails the round itself: caught.
    both = _run_caller(repo, [unrelated, _caller_finding(
        fid="R-3", severity="major")])
    assert both["caught_by"] == ["L2"]
    assert both["caller_check"]["caught"] is True

    # The residual renders visibly in the per-seed block.
    forged["repeat"] = 2
    text = render_report({
        "fixture": "soloadventurer", "base_ref": "main", "l2": "sessions",
        "repeats": 2, "seeds": [forged], "clean": [], "l2_quota_voided": 0,
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 0.0},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 0},
        "found_then_dropped": [],
    })
    assert (f"- seed {CALLER} [r2]: reported as sibling, did not block "
            "(the F-94 residual)") in text


# --------------------------------------------------------------------- AC-3 --


def _rec(seed_id: str, repeat: int, caught: bool, *, voided: bool = False,
         layer: str = "L2") -> dict:
    l2 = ({"quota_voided": True, "review_retries": 0} if voided
          else {"verdict": "fail" if caught else "pass", "findings": [],
                "verifier_dropped": [], "review_retries": 0})
    return {
        "id": seed_id, "title": seed_id, "expected_layer": layer,
        "repeat": repeat,
        "layers": {"L0": {"verdict": "pass"}, "L1": {"verdict": "pass"},
                   "L2": l2},
        "caught_by": [layer] if caught else [],
    }


def test_ac_3(tmp_path: Path):
    """Honest numbers: per seed caught k/N repeats (a quota-voided
    repeat leaves N, never a miss), the pass^k line, the never-caught
    line, found-then-dropped per seed and repeat, and the clean diff
    beside an old same-class site — whose sibling finding is no false
    fail."""
    from factory.eval import per_seed_counts

    # Scripted records: always (3/3), some (2/3), never (0/3), and one
    # whose middle repeat was quota-voided (2/2, not 2/3).
    records = (
        [_rec("always", r, True) for r in (1, 2, 3)]
        + [_rec("some", 1, True), _rec("some", 2, False),
           _rec("some", 3, True)]
        + [_rec("never", r, False) for r in (1, 2, 3)]
        + [_rec("voided", 1, True), _rec("voided", 2, False, voided=True),
           _rec("voided", 3, True)]
        + [_rec("dark", r, False, voided=True) for r in (1, 2, 3)]
    )
    counts = per_seed_counts(records)
    assert counts["always"] == {"caught": 3, "judged": 3, "voided": 0}
    assert counts["some"] == {"caught": 2, "judged": 3, "voided": 0}
    assert counts["never"] == {"caught": 0, "judged": 3, "voided": 0}
    assert counts["voided"] == {"caught": 2, "judged": 2, "voided": 1}
    assert counts["dark"] == {"caught": 0, "judged": 0, "voided": 3}

    report = {
        "fixture": "soloadventurer", "base_ref": "main", "l2": "sessions",
        "repeats": 3, "seeds": records, "clean": [],
        "l2_quota_voided": 4,
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 7 / 11},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 7},
        "found_then_dropped": [
            {"id": "never", "repeat": 2, "dropped": [
                {"id": "R-1", "file": "app/x.py", "text": "the defect",
                 "reason": "not a real defect"}]},
        ],
    }
    text = render_report(report)
    assert "- seed always: caught 3/3 repeats" in text
    assert "- seed some: caught 2/3 repeats" in text
    assert "- seed never: caught 0/3 repeats" in text
    assert "- seed voided: caught 2/2 repeats (1 quota-voided)" in text
    assert "- seed dark: not judged (3 quota-voided)" in text
    passk = next(line for line in text.splitlines()
                 if line.startswith("pass^k"))
    assert "2 of 4 judged seeds" in passk, passk
    assert "always" in passk and "voided" in passk
    assert "some" not in passk and "never" not in passk
    never = next(line for line in text.splitlines()
                 if line.startswith("Never caught"))
    assert "never" in never and "check the seed or grader first" in never
    assert "always" not in never and "dark" not in never
    assert "- seed never [r2]: found, then dropped by the verifier: R-1 " \
           "(app/x.py) — not a real defect" in text

    # No seed caught on every repeat, and none never caught.
    none_report = dict(report, seeds=[r for r in records
                                      if r["id"] in ("some",)],
                       found_then_dropped=[])
    none_text = render_report(none_report)
    assert "pass^k (caught on every judged repeat): 0 of 1 judged seeds" \
        in none_text
    assert "Never caught: none" in none_text
    assert "found, then dropped by the verifier: none" in none_text

    # The real eval end to end: the F-95 diffs in the report, the clean
    # diff beside the old site drawing a sibling that is NOT a false
    # fail, and found-then-dropped carried per seed and repeat.
    repo = _fixture(tmp_path)
    clean = next(d for d in CLEAN_DIFFS if d.id == CLEAN_OLD)
    assert isinstance(clean, CleanSpec) and clean.setup_files
    # blocking: the severity the clean diff's blocking count reads — a
    # sibling must not be counted at any severity.
    old_site = _sibling_finding(fid="R-5", severity="blocking")
    findings = dict(MOCK_SEED_FINDINGS)
    findings[next(d for d in CLEAN_DIFFS
                  if d.id == CLEAN_OLD).slot] = [old_site]
    # minor: not unanimous-protected, so the verifier's drop stands.
    findings[SIBLING_CONTRACT] = [_sibling_finding(severity="minor")]
    findings[CALLER_CONTRACT] = [_caller_finding()]
    seats = Seats(findings, drops={SIBLING_CONTRACT: {"U-1"}})
    full = run_seeded_eval(repo, session_runner=seats, repeats=2)
    clean_runs = [r for r in full["clean"] if r["id"] == CLEAN_OLD]
    assert len(clean_runs) == 2
    for record in clean_runs:
        assert record["layers"]["L2"]["verdict"] == "pass"
        assert record["blocking_findings"] == 0
        assert [f["sibling"] for f in record["layers"]["L2"]["findings"]] \
            == [True]
    assert full["false_fail_on_clean"]["L2"]["failed"] == 0
    caller_runs = [r for r in full["seeds"] if r["id"] == CALLER]
    assert [r["caught_by"] for r in caller_runs] == [["L2"], ["L2"]]
    drops = [e for e in full["found_then_dropped"] if e["id"] == SIBLING]
    assert [e["repeat"] for e in drops] == [1, 2]
    assert all(e["dropped"][0]["id"] == "U-1" for e in drops)
    # Every seed run has its entry (empty when nothing was dropped).
    assert len(full["found_then_dropped"]) == len(full["seeds"])
    assert full["per_seed"][CALLER] == {"caught": 2, "judged": 2,
                                        "voided": 0}
    assert full["per_seed"][SIBLING] == {"caught": 0, "judged": 2,
                                         "voided": 0}
    text = render_report(full)
    assert f"- seed {CALLER}: caught 2/2 repeats" in text
    assert f"- seed {SIBLING}: caught 0/2 repeats" in text
    assert SIBLING in next(line for line in text.splitlines()
                           if line.startswith("Never caught"))
    assert json.loads(json.dumps(full)) == full  # the report stays JSON
