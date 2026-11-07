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
from pathlib import Path

import pytest
from _support.f16 import (
    MOCK_SEED_FINDINGS,
    ScriptedSeats,
    _finding,
)

from factory.eval import (
    CLEAN_DIFFS,
    SOLOADVENTURER_SEEDS,
    build_soloadventurer_fixture,
    render_report,
    run_seed,
    run_seeded_eval,
    seed_by_id,
)


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


def test_l2_catch_requires_a_finding_that_names_the_seed(tmp_path: Path):
    """AC-3: the report says whether the SURVIVING findings caught the
    seed — not whether the layer's verdict failed. An L2 review that
    fails over findings unrelated to the seeded defect (fixture style,
    the ratchet script, anything) is a false positive the known-clean
    diffs exist to dial, and the report must not book it as a catch.
    The same seed with a finding that names the defect IS caught."""
    repo = _fixture(tmp_path)
    seed = seed_by_id("mocks-what-it-tests")
    unrelated = dict(
        _finding(), id="R-9", file="scripts/check_schema_refs.py",
        text="this script should use pathlib APIs consistently",
    )
    miss = run_seed(
        repo, seed,
        session_runner=ScriptedSeats({"EVAL-mocks-what-it-tests": [unrelated]}),
    )
    assert miss["layers"]["L2"]["verdict"] == "fail"
    assert miss["caught_by"] == [], miss["caught_by"]
    hit = run_seed(
        repo, seed,
        session_runner=ScriptedSeats({"EVAL-mocks-what-it-tests": [_finding()]}),
    )
    assert hit["caught_by"] == ["L2"]


def test_clean_diff_without_l2_is_reported_na_not_zero(tmp_path: Path):
    """With L2 skipped (no session runner) no review ran, so a known-clean
    diff drew no measured blocking findings — the report says n/a, never
    a 0 that implies a clean review that never happened."""
    repo = _fixture(tmp_path)
    report = run_seeded_eval(repo)
    assert report["l2"] == "skipped"
    for record in report["clean"]:
        assert record["blocking_findings"] is None, record["id"]
    text = render_report(report)
    assert "n/a" in text
    assert "0 blocking" not in text


def test_l0_catch_requires_evidence_that_names_the_seed(tmp_path: Path):
    """The same rule for L0: a failing layer is a catch only when its own
    recorded evidence names the seeded defect. The phantom-table seed's
    L0 evidence is the ratchet's output naming ``archived_trips``; a
    seed table whose failure output named nothing would not be caught."""
    repo = _fixture(tmp_path)
    record = run_seed(repo, seed_by_id("phantom-table"))
    assert record["caught_by"] == ["L0"]
    assert "archived_trips" in json.dumps(record["layers"]["L0"])


def test_factory_eval_runs_at_the_configured_reviewer_width(tmp_path, monkeypatch):
    """``factory eval``'s help promises "(default: the factory config)" —
    it must read <factory-root>/config.yaml the way the orchestrator
    does, not the dataclass defaults: the eval certifies the catch rate
    for the fan-out width the factory actually runs (AC-3)."""
    from factory import cli

    root = tmp_path / "factory-root"
    root.mkdir()
    (root / "config.yaml").write_text("reviewers: 3\n")
    seen = {}

    def fake_run(factory_root, out_path, *, reviewers, repeats, keep_fixture):
        seen["factory_root"] = factory_root
        seen["reviewers"] = reviewers
        rendered = tmp_path / "rendered.md"
        rendered.write_text("report")
        report = tmp_path / "report.json"
        report.write_text("{}")
        return {"report_path": report, "rendered_path": rendered}

    monkeypatch.setattr(cli, "run_on_demand_eval", fake_run)
    args = cli.build_parser().parse_args(["eval", "--factory-root", str(root)])
    assert cli._run_eval(args) == 0
    assert seen["reviewers"] == 3
    assert seen["factory_root"] == root


def test_factory_eval_config_default_and_flag_override(tmp_path, monkeypatch):
    """No config file: the documented default (2). ``--reviewers`` beats
    both."""
    from factory import cli

    root = tmp_path / "factory-root"
    root.mkdir()
    seen = []

    def fake_run(factory_root, out_path, *, reviewers, repeats, keep_fixture):
        seen.append(reviewers)
        rendered = tmp_path / "rendered.md"
        rendered.write_text("report")
        report = tmp_path / "report.json"
        report.write_text("{}")
        return {"report_path": report, "rendered_path": rendered}

    monkeypatch.setattr(cli, "run_on_demand_eval", fake_run)
    args = cli.build_parser().parse_args(["eval", "--factory-root", str(root)])
    assert cli._run_eval(args) == 0
    args = cli.build_parser().parse_args(
        ["eval", "--factory-root", str(root), "--reviewers", "4"]
    )
    assert cli._run_eval(args) == 0
    assert seen == [2, 4]


def test_factory_eval_refuses_a_malformed_config(tmp_path, monkeypatch, capsys):
    """A malformed config.yaml is the promised exit 2 with a message, not
    a traceback (cli contract) — and no eval runs on unreadable config."""
    from factory import cli

    root = tmp_path / "factory-root"
    root.mkdir()
    (root / "config.yaml").write_text("reviewers: 0\n")

    def boom(*args, **kwargs):
        raise AssertionError("no eval may run on unreadable config")

    monkeypatch.setattr(cli, "run_on_demand_eval", boom)
    parsed = cli.build_parser().parse_args(["eval", "--factory-root", str(root)])
    assert cli._run_eval(parsed) == 2
    err = capsys.readouterr().err
    assert "factory eval" in err and "reviewers" in err


def test_monthly_owner_step_is_documented():
    """AC-3: the eval runs on demand AND monthly. The monthly cadence is
    an owner step, so the repo has to record it — the README's operating
    notes name the command and the cadence."""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "factory eval" in readme
    assert "monthly" in readme.lower()


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
        factory_root, session_runner=ScriptedSeats(MOCK_SEED_FINDINGS),
        repeats=1,  # F-48 re-seat: this test's subject is the plumbing,
                    # not the (now defaulted) repeat spread.
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
        repeats=1,  # F-48 re-seat: plumbing test, single repeat is its subject
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
