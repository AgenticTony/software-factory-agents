"""F-16 acceptance tests — seeded-bug validator eval.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f16.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed: the SoloAdventurer fixture repo, the
seeded branches on it and the L0/L1 gates are real (the fixture is
factory-authored code, so the gates run unconfined, like the F-07/F-08
fixtures). The L2 seats are scripted — the monthly eval's real model
sessions are AC-3's owner step, and nothing here may drive one. The
expected catch rates come from the seed table's declared ``layer`` (the
spec), never from recomputing what the eval reports.
"""

import json
import re
from pathlib import Path

import pytest

from factory.eval import (
    CLEAN_DIFFS,
    SOLOADVENTURER_SEEDS,
    build_soloadventurer_fixture,
    render_report,
    run_seed,
    run_seeded_eval,
    seed_by_id,
)
from factory.session import SessionRecord


def _fixture(tmp_path: Path):
    repo = build_soloadventurer_fixture(tmp_path / "soloadventurer")
    return repo


def test_ac_2(tmp_path: Path):
    """The phantom-table seed is caught by L0 on the SoloAdventurer fixture.

    The seed renames the table a write path targets to one the committed
    schema doesn't know, in code no test covers — exactly the case the
    schema-refs ratchet exists for (VALIDATOR-SPEC L0: SoloAdventurer's
    ``check-schema-refs.py``). The fixture's own tests stay green, so the
    catch must be the ratchet's, not the test gate's.
    """
    repo = _fixture(tmp_path)
    seed = seed_by_id("phantom-table")
    record = run_seed(repo, seed)
    l0 = record["layers"]["L0"]
    assert l0["verdict"] == "fail"
    assert "L0" in record["caught_by"]
    # The catch names the phantom table, from the ratchet's own output.
    sentinels = [
        s for s in l0["sentinel_results"] if s.get("verdict") == "fail"
    ]
    assert len(sentinels) == 1
    assert "archived_trips" in sentinels[0]["detail"]


# --- the scripted L2 seats (no real model anywhere in this file) ------------

_FINDINGS_ARRAY = re.compile(r"```json\n(\[\s*\{.*?\}\s*\])\n```", re.S)


def _finding():
    return {
        "id": "R-1",
        "axis": "standards",
        "severity": "blocking",
        "file": "tests/test_api.py",
        "text": "the test mocks checkin_count — the function it claims to test",
    }


class ScriptedSeats:
    """Scripted reviewer/verifier seats, keyed by the eval contract id
    in the prompt. Reviewers return the scripted findings for their diff
    (an empty list is a declared pass); the verifier keeps every finding.
    Same seat signature the F-38 fan-out calls the real runner with.
    """

    def __init__(self, findings_by_contract: dict[str, list] | None = None):
        self.findings_by_contract = findings_by_contract or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        match = re.search(r'"id": "(EVAL-[^"]+)"', prompt)
        assert match, f"no eval contract id in the {seat} prompt"
        contract_id = match.group(1)
        self.calls.append((seat, contract_id))
        if seat == "reviewer":
            findings = self.findings_by_contract.get(contract_id, [])
            verdict = "fail" if findings else "pass"
            return SessionRecord(
                seat=seat, status="completed", num_turns=2, model="stub-model",
                tokens={"input_tokens": 10, "output_tokens": 5},
                result=json.dumps({"verdict": verdict, "findings": findings}),
            )
        found = json.loads(_FINDINGS_ARRAY.search(prompt).group(1))
        votes = [
            {"id": f["id"], "keep": True, "reason": "confirmed against the diff",
             "tag": "important"}
            for f in found
        ]
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 8, "output_tokens": 4},
            result=json.dumps({"votes": votes}),
        )


MOCK_SEED_FINDINGS = {"EVAL-mocks-what-it-tests": [_finding()]}


# --- AC-1 -------------------------------------------------------------------


def test_ac_1(tmp_path: Path):
    """The report shows the catch rate per layer for the seeded fixture set.

    The expected rates come from the seed table's declared layer (the
    spec), never from recomputing the report: L0 owns the four code-bug
    classes (the ratchet and the test gate), L1 owns the proves-nothing
    contract test, L2 owns the self-mocking test — and each layer must
    catch exactly its own class, nothing more.
    """
    repo = _fixture(tmp_path)
    report = run_seeded_eval(repo, session_runner=ScriptedSeats(MOCK_SEED_FINDINGS))
    assert report["catch_rate"] == {
        "L0": pytest.approx(4 / len(SOLOADVENTURER_SEEDS)),
        "L1": pytest.approx(1 / len(SOLOADVENTURER_SEEDS)),
        "L2": pytest.approx(1 / len(SOLOADVENTURER_SEEDS)),
    }
    for seed in SOLOADVENTURER_SEEDS:
        record = next(r for r in report["seeds"] if r["id"] == seed.id)
        assert record["caught_by"] == [seed.layer], seed.id
    for clean in report["clean"]:
        assert clean["blocking_findings"] == 0, clean["id"]


def test_l2_runs_the_f38_fanout(tmp_path: Path):
    """Every seeded diff with findings gets N parallel reviewers and one
    verifier vote over the union — F-38's pipeline, not a single reviewer."""
    repo = _fixture(tmp_path)
    seats = ScriptedSeats(MOCK_SEED_FINDINGS)
    report = run_seeded_eval(repo, session_runner=seats)
    mock_seed = next(r for r in report["seeds"] if r["id"] == "mocks-what-it-tests")
    assert mock_seed["layers"]["L2"]["verdict"] == "fail"
    reviewers = [c for c in seats.calls if c[0] == "reviewer"]
    verifiers = [c for c in seats.calls if c[0] == "verifier"]
    assert len(reviewers) == 2 * (len(SOLOADVENTURER_SEEDS) + len(CLEAN_DIFFS))
    assert ("verifier", "EVAL-mocks-what-it-tests") in verifiers


def test_render_report_shows_the_per_diff_table(tmp_path: Path):
    """The rendered report the owner records as tick evidence shows, per
    diff, whether the surviving findings caught the seed and how many
    blocking findings each known-clean diff drew."""
    repo = _fixture(tmp_path)
    report = run_seeded_eval(repo, session_runner=ScriptedSeats(MOCK_SEED_FINDINGS))
    text = render_report(report)
    for seed in SOLOADVENTURER_SEEDS:
        assert seed.id in text
    for clean in CLEAN_DIFFS:
        assert clean.id in text
    assert "catch rate" in text.lower()
    for layer in ("L0", "L1", "L2"):
        assert layer in text


# --- AC-3 (manual check; these hold the mechanism to its constraints) -------


def test_ac_3(tmp_path: Path):
    """The eval is on-demand and monthly, never on a routine path.

    AC-3 drives real model sessions, so its evidence is the owner's
    recorded run (a `manual` check). What a test CAN hold: nothing on
    the routine path — no gate, no orchestrator round, no CI workflow —
    reaches the eval; only the explicit ``factory eval`` command does.
    """
    repo = Path(__file__).resolve().parent.parent
    for path in (repo / ".github").glob("workflows/*.y*ml"):
        assert "factory eval" not in path.read_text(), path
    for path in (repo / "factory").glob("*.py"):
        if path.name in ("eval.py", "cli.py"):
            continue
        src = path.read_text()
        assert "factory.eval" not in src and "run_on_demand_eval" not in src, path


def test_factory_eval_command_is_not_a_stub():
    """``factory eval`` dispatches the real driver, unlike the stub
    subcommands."""
    from factory.cli import STUBS, build_parser

    assert "eval" not in STUBS
    parser = build_parser()
    args = parser.parse_args(["eval"])
    assert args.command == "eval"


def test_on_demand_driver_reports_and_cleans_up(tmp_path: Path):
    """One on-demand run, end to end: the real gate path (the pinned srt
    sandbox runs every L0/L1 command), the L2 seats scripted, the report
    written where the owner records it, the scratch removed."""
    from factory.eval import run_on_demand_eval

    factory_root = tmp_path / "factory-root"
    result = run_on_demand_eval(
        factory_root, session_runner=ScriptedSeats(MOCK_SEED_FINDINGS)
    )
    report = result["report"]
    assert result["report_path"].exists()
    assert json.loads(result["report_path"].read_text()) == report
    assert result["rendered_path"].exists()
    assert result["rendered_path"].read_text() == render_report(report)
    for record in report["seeds"]:
        assert record["caught_by"] == [record["expected_layer"]], record["id"]
    for record in report["clean"]:
        assert record["blocking_findings"] == 0, record["id"]
    # The scratch is gone; the evidence stays.
    assert not (factory_root / "eval" / "soloadventurer").exists()


def test_on_demand_driver_plumbing_with_unconfined_gates(tmp_path: Path):
    """The driver's own plumbing — fresh fixture, report files, cleanup —
    with the gates unconfined (INC-007 keeps the sandboxed variant for a
    host that can apply a profile; the assertions are the same)."""
    from factory.eval import run_on_demand_eval
    from factory.gates import run_command

    factory_root = tmp_path / "factory-root"
    result = run_on_demand_eval(
        factory_root,
        session_runner=ScriptedSeats(MOCK_SEED_FINDINGS),
        command_runner=run_command,
    )
    report = result["report"]
    assert json.loads(result["report_path"].read_text()) == report
    assert result["rendered_path"].read_text() == render_report(report)
    for record in report["seeds"]:
        assert record["caught_by"] == [record["expected_layer"]], record["id"]
    for record in report["clean"]:
        assert record["blocking_findings"] == 0, record["id"]
    assert not (factory_root / "eval" / "soloadventurer").exists()
    assert list((factory_root / "eval").glob("srt-settings-*.json")) == []
