"""F-99 contract tests: the eval stops telling the reviewers the answer.

``eval._seed_task`` handed each seed's own eval description to the
reviewers as the task's title, so L2's catch rate measured reading, not
reviewing (the sibling seed's leaked title read as an unmet contract and
the reviewers blocked on the second site they had correctly found). One
"clean" diff carried a docstring claiming behaviour the fixture
disproves (the row "moves" to trip_archive — ``store.insert`` returns a
dict and moves nothing), which is where both of L2's clean-diff false
fails came from. And a "found, then dropped" entry could not say whether
the drop cost a catch.

F-99: every seed and clean diff carries a required ``contract_title``
(the developer's task, stated as intent, never naming the defect); the
eval's ``title`` stays a report-only label. The proof is on the
assembled prompts every seat actually receives, not on which field
``_seed_task`` reads.

Real: the fixture repo, the seed branches, L0/L1 (the claim lexicon in
AC-2 is checked against the fixture by EXECUTING its ``archive_trip``
and ``store.insert``). Scripted: every L2 seat. No network, no model
session.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from _support.f16 import _FINDINGS_ARRAY, ScriptedSeats

from factory.eval import (
    CLEAN_DIFFS,
    FIXTURE_FILES,
    SCHEMA_TABLES,
    SOLOADVENTURER_SEEDS,
    build_soloadventurer_fixture,
    render_report,
    run_seed,
    run_seeded_eval,
    seed_by_id,
)
from factory.session import SessionRecord

SIBLING = "sibling-second-site"
CLEAN_OLD = "clean-beside-old-site"
MOCKS = "mocks-what-it-tests"

# A generic, marker-free nit every contract's reviewers report, so every
# diff draws a union and the VERIFIER's prompt assembles too (the seat
# that must not see the answer includes the verifier).
_GENERIC_FINDING = {
    "id": "R-1", "axis": "standards", "severity": "minor",
    "file": "app/api.py",
    "text": "a usage example in the docstring would help",
}

ALL_SPECS = tuple(SOLOADVENTURER_SEEDS) + tuple(CLEAN_DIFFS)
ALL_IDS = tuple(spec.id for spec in ALL_SPECS)
ALL_TITLES = tuple(spec.title for spec in ALL_SPECS)


def _contract_key(spec) -> str:
    """The contract id a seat's prompt carries for this spec: the
    opaque slot (post-fix), or whatever the current code routes on."""
    return str(getattr(spec, "slot", "") or f"EVAL-{spec.id}")


def _fixture(tmp_path: Path, name: str = "soloadventurer") -> Path:
    return build_soloadventurer_fixture(tmp_path / name)


def _contract_id_of(prompt: str) -> str:
    match = re.search(r'"id": "(EVAL-[^"]+)"', prompt)
    assert match, "no eval contract id in the prompt"
    return match.group(1)


class CapturingSeats(ScriptedSeats):
    """ScriptedSeats that record every seat's full prompt per contract —
    each reviewer seat's user prompt and system prompt, and the
    verifier's — so the leak check runs on exactly what each seat
    received, not on a field name."""

    def __init__(self):
        super().__init__({_contract_key(s): [dict(_GENERIC_FINDING)]
                          for s in ALL_SPECS})
        self.captured: dict[str, list[dict]] = {}

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        cid = _contract_id_of(prompt)
        self.captured.setdefault(cid, []).append({
            "seat": seat, "prompt": prompt,
            "system": str(kwargs.get("system_prompt") or ""),
        })
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


def _allowed_text(spec) -> str:
    """Everything the fixture repo itself can put in front of a seat for
    this diff: the shared fixture files, the spec's own setup files and
    new files, and both sides of every edit (the diff). A marker found
    only here is the CODE's own text — the reviewers can find it in the
    repo, so it is not the eval describing the defect."""
    parts = (list(FIXTURE_FILES.values()) + list(spec.setup_files.values())
             + list(spec.files.values())
             + [old + "\n" + new for old, new in spec.edits])
    return "\n".join(parts)


def _sweep(spec, captured: list[dict], allowed: str) -> None:
    """No seat's prompt (user or system) carries the eval's own naming
    of the diff — NO spec's id and NO spec's title, anywhere in the
    prompt, with no layer-report exemption (the ids name the bug class:
    "mocks-what-it-tests" in a contract id, a branch name or a sentinel
    description is the answer). Markers are the one exemption, and only
    for text the diff, the fixture or the layer reports over them
    already carry."""
    markers = tuple(getattr(spec, "markers", ()) or ())
    for seat in captured:
        where = f"{spec.id}/{seat['seat']}"
        for text in (seat["prompt"], seat["system"]):
            for other_id in ALL_IDS:
                assert other_id not in text, (where, other_id)
            for title in ALL_TITLES:
                assert title not in text, (where, title[:40])
            for marker in markers:
                if marker in allowed:
                    continue  # the diff, fixture or layer evidence has it
                assert marker not in text, (where, marker)


# --------------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """A contract the reviewers see, separate from the eval's label.

    Every seed and clean diff runs through the real pipeline (real
    fixture, real L0/L1, scripted L2 at a three-seat fan-out so every
    seat's prompt is captured, verifier included): no assembled prompt
    contains ANY spec's id or title — the ids name the bug class
    ("mocks-what-it-tests"), so the contract id, the branch names and
    the sentinel descriptions all run on an opaque slot instead — and no
    marker the diff, fixture or layer evidence doesn't itself carry (the
    tautological seed's acceptance text follows the same rule). Every
    spec carries a required, non-empty ``contract_title`` distinct from
    the label; ``_seed_task`` uses it, and the git surface (branches,
    setup base, commit messages) is slot-named. A real task's prompt is
    unchanged: its own title still renders in the contract block of both
    prompt builders, verbatim (F-48 parity).
    """
    from factory import orchestrator
    from factory.eval import SeedSpec, _seed_task

    # a) the leak, on the assembled prompts every seat receives.
    repo = _fixture(tmp_path)
    seats = CapturingSeats()
    report = run_seeded_eval(repo, session_runner=seats, reviewers=3)
    records = {r["id"]: r for r in report["seeds"] + report["clean"]}
    for spec in ALL_SPECS:
        seen = seats.captured[_contract_key(spec)]
        kinds = [c["seat"] for c in seen]
        assert kinds.count("reviewer") == 3, (spec.id, kinds)
        assert "verifier" in kinds, (spec.id, kinds)
        # The layer reports embedded in the prompt are the pipeline's own
        # output over this code (command output, sentinel detail, red
        # reasons) — allowed for MARKERS only: a marker there is the
        # layer's own evidence over the code (Python's "coroutine" in a
        # failing test's output), never the eval describing the defect.
        # Ids and titles get no such exemption, layer reports included:
        # an id reaching a seat is the answer, whatever carried it.
        rec = records[spec.id]
        allowed = (_allowed_text(spec)
                   + "\n" + json.dumps(rec["layers"]["L0"])
                   + "\n" + json.dumps(rec["layers"]["L1"]))
        _sweep(spec, seen, allowed)
        # The tautological seed's acceptance text rides the contract
        # block: the same rule, checked on the text itself.
        contract_text = str((getattr(spec, "contract", None) or {}).get("text")
                            or "")
        if contract_text:
            assert spec.title not in contract_text, spec.id
            for other_id in ALL_IDS:
                assert other_id not in contract_text, (spec.id, other_id)
            for marker in spec.markers:
                if marker not in allowed:
                    assert marker not in contract_text, (spec.id, marker)

    # b) the contract title: required at construction, non-empty, never
    # the eval's label; _seed_task uses it. The contract's id is the
    # spec's opaque slot: position-numbered, unique, carrying no part of
    # the spec's id — the task id, the branch names and the setup/seed
    # commit messages all use it, and `seed.id` stays on the record and
    # in the report only.
    for spec in ALL_SPECS:
        title = spec.contract_title
        assert title and title.strip(), spec.id
        assert title != spec.title, spec.id
    slots = [spec.slot for spec in ALL_SPECS]
    assert len(set(slots)) == len(slots)
    for spec in ALL_SPECS:
        assert re.fullmatch(r"EVAL-\d{2}", spec.slot), spec.slot
        assert spec.id not in spec.slot, (spec.id, spec.slot)
    # Required independently: each construction omits exactly one of the
    # two, so a default slipped onto either field alone still refuses.
    with pytest.raises(TypeError):
        SeedSpec(id="x", slot="EVAL-99", layer="L0",
                 title="an eval label")  # no contract_title
    with pytest.raises(TypeError):
        SeedSpec(id="x", layer="L0", title="an eval label",
                 contract_title="a task")  # no slot
    for spec in ALL_SPECS:
        task = _seed_task(spec)
        assert task["title"] == spec.contract_title, spec.id
        assert task["title"] != spec.title
        assert task["id"] == spec.slot, spec.id
    # The eval's label stays on the record — report-only, never a prompt.
    assert records[SIBLING]["title"] == seed_by_id(SIBLING).title

    # c) a one-seat fan-out (both lenses in one seat): the same rule,
    #    and the git surface is slot-named too — branch, setup base and
    #    the record's `base` (which L1's red_reason echoes into prompts).
    solo_repo = _fixture(tmp_path, "solo-one")
    solo = CapturingSeats()
    solo_seed = seed_by_id(SIBLING)
    solo_record = run_seed(solo_repo, solo_seed, session_runner=solo,
                           reviewers=1)
    seen = solo.captured[_contract_key(solo_seed)]
    assert [c["seat"] for c in seen].count("reviewer") == 1
    _sweep(solo_seed, seen, _allowed_text(solo_seed))
    assert solo_record["base"] == f"eval-base/{solo_seed.slot}"
    branches = subprocess.run(
        ["git", "-C", str(solo_repo), "branch", "--format", "%(refname:short)"],
        capture_output=True, text=True, check=True).stdout.split()
    assert f"eval/{solo_seed.slot}" in branches, branches
    assert not any(solo_seed.id in b for b in branches), branches
    log = subprocess.run(
        ["git", "-C", str(solo_repo), "log", "--format=%s",
         f"eval/{solo_seed.slot}"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    assert f"seed: {solo_seed.slot}" in log
    assert f"setup: {solo_seed.slot}" in log
    assert not any(solo_seed.id in line for line in log), log

    # d) the captured prompt IS the orchestrator's own builder output
    # over the eval's task (no private copy), and a real task's prompt is
    # unchanged: its own title renders in the contract block verbatim.
    seed = seed_by_id(SIBLING)
    contract_block = json.dumps(
        {"id": seed.slot, "title": seed.contract_title,
         "acceptance": [], "out_of_scope": [], "risk": []},
        indent=2, ensure_ascii=False)
    reviewer_prompt = next(c["prompt"] for c in seats.captured[
        _contract_key(seed)] if c["seat"] == "reviewer")
    assert contract_block in reviewer_prompt
    real = {"id": "T-1", "title": "Wire the packing summary report",
            "acceptance": [], "out_of_scope": [], "risk": []}
    diff = "diff --git a/app/report.py b/app/report.py\n"
    for prompt in (orchestrator._review_prompt(real, diff, {}, {}),
                   orchestrator._verifier_prompt(real, diff, [])):
        assert '"title": "Wire the packing summary report"' in prompt


# --------------------------------------------------------------------- AC-2 --

# Behaviour claims checked against the fixture BY EXECUTION: the probe in
# test_ac_2 snapshots the store's tables around a real archive_trip call,
# so a claim from these lists is allowed only if the executed behaviour
# proves it (nothing moves and nothing persists — both fail).
MOVE_WORDS = ("moves", "transfers", "migrates", "removes", "deletes")
PERSIST_WORDS = ("persists", "writes", "stores", "keeps", "saves")


def _fixture_modules(repo: Path):
    """Import the fixture's app.api/app.store from its checkout. Any
    previously-imported ``app`` is popped BEFORE the import too, so an
    xdist worker that already imported another ``app`` (or this test's
    earlier use) can never hand AC-2's probe the wrong module; popped
    after again so nothing leaks past the test."""
    for name in ("app.api", "app.store", "app"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(repo))
    try:
        return (importlib.import_module("app.api"),
                importlib.import_module("app.store"))
    finally:
        sys.path.remove(str(repo))
        for name in ("app.api", "app.store", "app"):
            sys.modules.pop(name, None)


def _snapshot(store) -> tuple:
    return (json.dumps(store.TRIPS), json.dumps(store.GEAR),
            json.dumps(store.CHECKINS))


def test_ac_2(tmp_path: Path):
    """The clean diffs are clean: clean-beside-old-site's docstring
    describes what archive_trip actually does, verified against the
    fixture by EXECUTION (the probe), not judgement — a docstring
    claiming the row moves fails, because the probe shows nothing moves
    and nothing persists. Every clean diff's changed text is swept with
    the same claim lexicon where it states behaviour, and every table it
    names is in the committed schema.
    """
    repo = _fixture(tmp_path)
    clean = next(d for d in CLEAN_DIFFS if d.id == CLEAN_OLD)
    record = run_seed(repo, clean, session_runner=ScriptedSeats())
    # The changed text runs the real gates: ratchet and tests green on it.
    assert record["layers"]["L0"]["verdict"] == "pass", record["layers"]["L0"]

    # The probe: what archive_trip / store.insert actually do.
    api = store = None
    try:
        api, store = _fixture_modules(repo)
        before = _snapshot(store)
        returned = api.archive_trip("t1")
        assert returned == {"table": "trip_archive", "trip_id": "t1"}
        assert _snapshot(store) == before  # nothing moved, nothing stored
        assert store.insert("trip_archive", {"trip_id": "t9"}) == {
            "table": "trip_archive", "trip_id": "t9"}
        assert _snapshot(store) == before
    finally:
        for name in ("app.api", "app.store", "app"):
            sys.modules.pop(name, None)

    # The docstring at head, read from the repo (not from the spec
    # constant): its state claims must be ones the probe proves, and it
    # must describe the function — the table its body targets, by the
    # call its body makes.
    source = (repo / "app" / "api.py").read_text()
    fn = next(node for node in ast.parse(source).body
              if isinstance(node, ast.FunctionDef) and node.name
              == "archive_trip")
    docstring = ast.get_docstring(fn) or ""
    assert docstring.strip(), "archive_trip lost its docstring"
    body = ast.get_source_segment(source, fn) or ""
    for word in MOVE_WORDS + PERSIST_WORDS:
        assert word not in docstring.lower(), (
            f"the docstring claims '{word}' and the executed probe "
            "disproves it")
    assert "trip_archive" in docstring, "the docstring names no table"
    assert 'store.insert("trip_archive"' in body
    assert "insert" in docstring.lower(), (
        "the docstring does not describe the call the body makes")
    named = [t for t in SCHEMA_TABLES if t in docstring]
    assert named == ["trip_archive"]

    # Every clean diff's changed text, where it states behaviour: the
    # same claim lexicon, and no table outside the committed schema.
    for diff in CLEAN_DIFFS:
        changed = "\n".join([new for _, new in diff.edits]
                            + list(diff.files.values())).lower()
        for word in MOVE_WORDS + PERSIST_WORDS:
            assert word not in changed, (diff.id, word)
        for table in re.findall(r"\btrip_archive\b|\btrips\b|\bgear\b"
                                r"|\bcheckins\b", changed):
            assert table in [t.lower() for t in SCHEMA_TABLES], (
                diff.id, table)


# --------------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path):
    """A drop says whether it cost anything: each found-then-dropped
    entry carries ``seed_caught`` (was the seed still caught by any
    layer on that run); the report renders "(seed still caught)" or
    "(seed missed)" and counts the drops that cost a catch. A drop on a
    caught run is not costly; a drop on a missed run is. Old reports
    without the field still render, with no verdict and no count.
    """
    from _support.f95 import finding

    catch = finding("R-1", "tests/test_api.py",
                    "the test mocks checkin_count — the function it "
                    "claims to test", severity="blocking")
    dup = finding("R-2", "tests/test_api.py",
                  "mock indirection hides the real fetch",
                  severity="major")
    sibling = finding("R-3", "app/gear.py",
                      "gear_for_owner accepts a whitespace-only owner_id",
                      severity="minor", sibling=True)
    caller = finding("R-4", "app/report.py",
                     "pack_summary still reads i['item'] but pack_list "
                     "now returns plain names", severity="blocking")

    class MixedSeats(ScriptedSeats):
        """Per-contract split seats (the SplitSeats pattern, for two
        contracts at once): the first reviewer call on a split contract
        returns ``first``, later calls ``rest``; the verifier drops the
        listed UNION ids (a finding one seat reported is not unanimous,
        so the drop stands). Other contracts use the scripted table."""

        def __init__(self, split: dict, drops: dict):
            super().__init__({seed_by_id("caller-break").slot: [caller]})
            self._split = split
            self._drops = drops
            self._lock = threading.Lock()
            self._n: dict[str, int] = {}

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            cid = _contract_id_of(prompt)
            if seat == "reviewer" and cid in self._split:
                with self._lock:
                    n = self._n.get(cid, 0)
                    self._n[cid] = n + 1
                found = self._split[cid][0] if n == 0 else self._split[cid][1]
                self.calls.append((seat, cid))
                return SessionRecord(
                    seat=seat, status="completed", num_turns=2,
                    model="stub-model",
                    tokens={"input_tokens": 10, "output_tokens": 5},
                    result=json.dumps({"verdict": "fail" if found else "pass",
                                       "findings": found}),
                )
            if seat == "verifier":
                self.calls.append((seat, cid))
                found = json.loads(_FINDINGS_ARRAY.search(prompt).group(1))
                dropped = self._drops.get(cid, set())
                votes = [
                    {"id": f["id"], "keep": False,
                     "reason": "duplicate of the kept finding"}
                    if f["id"] in dropped else
                    {"id": f["id"], "keep": True, "reason": "confirmed",
                     "tag": "important"}
                    for f in found
                ]
                return SessionRecord(
                    seat=seat, status="completed", num_turns=1,
                    model="stub-model",
                    tokens={"input_tokens": 8, "output_tokens": 4},
                    result=json.dumps({"votes": votes}),
                )
            return super().__call__(seat, prompt, run_dir, profile, **kwargs)

    repo = _fixture(tmp_path)
    seats = MixedSeats(
        split={seed_by_id(MOCKS).slot: ([catch], [catch, dup]),
               seed_by_id(SIBLING).slot: ([], [sibling])},
        drops={seed_by_id(MOCKS).slot: {"U-2"},
               seed_by_id(SIBLING).slot: {"U-1"}},
    )
    report = run_seeded_eval(repo, session_runner=seats, reviewers=2)

    # The runs themselves: the mocks seed caught by its kept finding and
    # still carrying a dropped duplicate; the sibling seed missed with
    # its only matching finding dropped.
    seeds = {r["id"]: r for r in report["seeds"]}
    assert seeds[MOCKS]["caught_by"] == ["L2"]
    assert seeds[SIBLING]["caught_by"] == []
    entries = {(e["id"], e["repeat"]): e
               for e in report["found_then_dropped"]}
    mocks_entry = entries[(MOCKS, 1)]
    sib_entry = entries[(SIBLING, 1)]
    assert mocks_entry["dropped"], mocks_entry
    assert mocks_entry["seed_caught"] is True
    assert sib_entry["dropped"], sib_entry
    assert sib_entry["seed_caught"] is False

    # The rendered report: the parenthetical per entry, the count line
    # below them — a caught run's drop is not costly, a missed run's is.
    text = render_report(report)
    mocks_line = next(line for line in text.splitlines()
                      if line.startswith(f"- seed {MOCKS}:")
                      and "found, then dropped" in line)
    sib_line = next(line for line in text.splitlines()
                    if line.startswith(f"- seed {SIBLING}:")
                    and "found, then dropped" in line)
    assert mocks_line.endswith("(seed still caught)"), mocks_line
    assert sib_line.endswith("(seed missed)"), sib_line
    assert "Drops that cost a catch: 1 of 2" in text

    # An old report (pre-F-99, no seed_caught on the entries) renders
    # the old line shape — no parenthetical, no count line, no crash.
    old = {
        "fixture": "soloadventurer", "base_ref": "main", "l2": "sessions",
        "repeats": 1,
        "seeds": [{"id": "never", "title": "never", "expected_layer": "L2",
                   "repeat": 1,
                   "layers": {"L0": {"verdict": "pass"},
                              "L1": {"verdict": "pass"},
                              "L2": {"verdict": "pass", "findings": [],
                                     "verifier_dropped": [],
                                     "review_retries": 0}},
                   "caught_by": []}],
        "clean": [], "l2_quota_voided": 0,
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 0.0},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 0},
        "found_then_dropped": [
            {"id": "never", "repeat": 1, "dropped": [
                {"id": "R-1", "file": "app/x.py", "text": "the defect",
                 "reason": "not a real defect"}]},
        ],
    }
    old_text = render_report(old)
    assert ("- seed never: found, then dropped by the verifier: R-1 "
            "(app/x.py) — not a real defect") in old_text
    assert "(seed missed)" not in old_text
    assert "(seed still caught)" not in old_text
    assert "cost a catch" not in old_text
