"""F-48 acceptance tests — eval parity and variance.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f48.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed: the SoloAdventurer fixture, the seeded
branches and the L0/L1 gates are real (the test_f16 convention); every L2
seat is scripted — no real model session anywhere in this file (the
real-session run is AC-5's owner step). test_ac_1's orchestrator leg
drives one real ``run_once`` round over the test_f38 fixture project
with that suite's stub seats.
"""

import json
import re
import statistics
import threading
from pathlib import Path

import pytest
from test_f16 import (
    _FINDINGS_ARRAY,
    MOCK_SEED_FINDINGS,
    ScriptedSeats,
)

from factory.eval import (
    CLEAN_DIFFS,
    SOLOADVENTURER_SEEDS,
    EvalError,
    build_soloadventurer_fixture,
    render_report,
    run_seed,
    run_seeded_eval,
    seed_by_id,
)
from factory.session import SessionRecord


def _fixture(tmp_path: Path) -> Path:
    return build_soloadventurer_fixture(tmp_path / "soloadventurer")


def _quota_seat(seat: str = "reviewer") -> SessionRecord:
    """A seat record whose session died on quota (the shapes
    ``orchestrator._QUOTA`` matches: 429 / rate limit)."""
    return SessionRecord(
        seat=seat, status="failed", reason="HTTP 429 rate limit exceeded",
        num_turns=1, model="stub-model",
        tokens={"input_tokens": 5, "output_tokens": 1},
    )


def _contract_id_of(seat: str, prompt: str) -> str:
    match = re.search(r'"id": "(EVAL-[^"]+)"', prompt)
    assert match, f"no eval contract id in the {seat} prompt"
    return match.group(1)


class QuotaReviewers(ScriptedSeats):
    """Every reviewer seat dies on quota; the verifier (never reached)
    keeps the base script. The call is still recorded — the fan-out DID
    run the seats; only their sessions died."""

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "reviewer":
            self.calls.append((seat, _contract_id_of(seat, prompt)))
            return _quota_seat("reviewer")
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


class QuotaVerifier(ScriptedSeats):
    """Reviewers speak the protocol; the verifier seat dies on quota."""

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "verifier":
            return _quota_seat("verifier")
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


class VetoVerifier(ScriptedSeats):
    """The M-3 scenario, split the way production's own pin splits it
    (test_f38's veto round): reviewer 1 declares fail with the finding,
    reviewer 2 declares pass — the union's finding is NOT unanimous, so
    the one verifier MAY drop it, and does: every finding voted out."""

    def __init__(self, findings_by_contract: dict | None = None):
        super().__init__(findings_by_contract)
        self._lock = threading.Lock()
        self._reviewer_calls: dict[str, int] = {}

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "reviewer":
            contract = _contract_id_of(seat, prompt)
            with self._lock:
                n = self._reviewer_calls.get(contract, 0)
                self._reviewer_calls[contract] = n + 1
            if n == 0:  # the first seat reports; the second stays clean
                return super().__call__(seat, prompt, run_dir, profile,
                                        **kwargs)
            return SessionRecord(
                seat=seat, status="completed", num_turns=2,
                model="stub-model",
                tokens={"input_tokens": 10, "output_tokens": 5},
                result=json.dumps({"verdict": "pass", "findings": []}),
            )
        if seat == "verifier":
            found = json.loads(_FINDINGS_ARRAY.search(prompt).group(1))
            votes = [
                {"id": f["id"], "keep": False, "reason": "not a real defect"}
                for f in found
            ]
            return SessionRecord(
                seat=seat, status="completed", num_turns=1, model="stub-model",
                tokens={"input_tokens": 8, "output_tokens": 4},
                result=json.dumps({"votes": votes}),
            )
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


class FlakySeats(ScriptedSeats):
    """Reviewers that catch the self-mocking test only on a contract's
    FIRST repeat. Deterministic: run_seeded_eval runs diffs sequentially
    and each repeat makes exactly ``reviewers`` reviewer calls per
    contract, so call ``k`` belongs to repeat ``k // reviewers`` (with
    reviewers=1 the call index IS the repeat index)."""

    def __init__(self, findings_by_contract: dict | None = None, reviewers: int = 1):
        super().__init__(findings_by_contract or MOCK_SEED_FINDINGS)
        self._reviewers = reviewers
        self._lock = threading.Lock()
        self._calls_per_contract: dict[str, int] = {}

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "reviewer":
            match = re.search(r'"id": "(EVAL-[^"]+)"', prompt)
            assert match, f"no eval contract id in the {seat} prompt"
            with self._lock:
                seen = self._calls_per_contract.get(match.group(1), 0)
                self._calls_per_contract[match.group(1)] = seen + 1
            if seen // self._reviewers >= 1:  # repeat 2+: this reviewer misses
                return SessionRecord(
                    seat=seat, status="completed", num_turns=2,
                    model="stub-model",
                    tokens={"input_tokens": 10, "output_tokens": 5},
                    result=json.dumps({"verdict": "pass", "findings": []}),
                )
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


# --- AC-1 (M-3): one shared verdict function -------------------------------


def test_ac_1(tmp_path: Path, monkeypatch):
    """The eval's L2 verdict is computed by the ONE post-review chain
    production renders through — same symbol, called on both real paths:
    the orchestrator's review round and the eval's ``run_l2_review``.

    The orchestrator leg doubles as the mode argument: a one-round task
    (first attempt, review passes, publish) renders its verdict through
    the shared function in FIRST-ROUND mode — no final review runs for a
    task that never had a delta round.
    """
    from factory import eval as eval_module
    from factory import l2 as l2_module
    from factory import orchestrator as orchestrator_module

    # One function object, bound in the module that owns the verdict
    # primitives and in both consumers.
    shared = l2_module.settle_review_verdict
    assert eval_module.settle_review_verdict is shared
    assert orchestrator_module.settle_review_verdict is shared

    # The real orchestrator path: one scripted pass round through
    # run_once (the test_f38 harness), the shared symbol spied where the
    # orchestrator bound it, delegating to the real function.
    from test_f38 import Env, Fanout, _one_round_task, _verdict

    orch_calls: list[dict] = []
    real = orchestrator_module.settle_review_verdict

    def orch_spy(union, prior, delta_files, **kwargs):
        orch_calls.append(kwargs)
        return real(union, prior, delta_files, **kwargs)

    monkeypatch.setattr(orchestrator_module, "settle_review_verdict", orch_spy)
    env = Env(tmp_path / "orch", tasks=[_one_round_task()])
    result = env.run(Fanout(reviews=_verdict()))  # a clean one-round pass
    assert result.outcome == "pr_opened", result
    assert len(orch_calls) == 1, orch_calls  # one round, no final review
    assert orch_calls[0]["delta_mode"] is False
    assert orch_calls[0].get("final", False) is False
    assert orch_calls[0]["vote"] is None  # no findings -> no verifier ran

    # The real eval path: one scripted seed review through run_seed.
    monkeypatch.setattr(orchestrator_module, "settle_review_verdict", real)
    eval_calls: list[dict] = []

    def eval_spy(union, prior, delta_files, **kwargs):
        eval_calls.append(kwargs)
        return real(union, prior, delta_files, **kwargs)

    monkeypatch.setattr(eval_module, "settle_review_verdict", eval_spy)
    repo = _fixture(tmp_path / "evalfix")
    record = run_seed(repo, seed_by_id("mocks-what-it-tests"),
                      session_runner=ScriptedSeats(MOCK_SEED_FINDINGS))
    assert record["layers"]["L2"]["verdict"] == "fail"  # findings kept: fail
    assert len(eval_calls) == 1, eval_calls
    assert eval_calls[0]["delta_mode"] is False
    assert eval_calls[0].get("final", False) is False
    assert eval_calls[0]["vote"] is not None  # the verifier voted the union
    assert eval_calls[0]["reviewers"] == 2


def test_review_declared_fail_vetoed_to_pass_in_eval(tmp_path: Path):
    """The real M-3 symptom: a declared-fail review whose findings the
    verifier rejects renders PASS in the eval — apply_convergence's
    ``vote_rejected_fail`` branch, exactly as production renders it
    (pinned orchestrator-side by test_f38's verifier-veto round, where
    the verdict flips to pass and the PR opens)."""
    repo = _fixture(tmp_path)
    record = run_seed(
        repo, seed_by_id("mocks-what-it-tests"),
        session_runner=VetoVerifier(MOCK_SEED_FINDINGS),
    )
    l2 = record["layers"]["L2"]
    assert l2["verdict"] == "pass", l2  # the vetoed declared fail passes
    assert l2["verifier_dropped"], l2  # the veto stays on the record
    # A pass review means no L2 catch — and no false fail either.
    assert record["caught_by"] == []


# --- AC-2 (m-12): quota before parsing --------------------------------------


def test_ac_2(tmp_path: Path):
    """A seat whose FIRST run died on quota is never parsed, never
    re-run, never counted as a retry — quota is checked before any
    parse, production's order — and the diff's review is voided."""
    repo = _fixture(tmp_path)
    seats = QuotaReviewers(MOCK_SEED_FINDINGS)
    record = run_seed(repo, seed_by_id("mocks-what-it-tests"),
                      session_runner=seats, reviewers=2)
    l2 = record["layers"]["L2"]
    assert l2["review_retries"] == 0, l2  # RED today: the quota seat is re-run
    assert l2.get("quota_voided") is True, l2
    reviewer_calls = [c for c in seats.calls if c[0] == "reviewer"]
    assert len(reviewer_calls) == 2, seats.calls  # the fan-out only: no re-run
    assert not any(c[0] == "verifier" for c in seats.calls), seats.calls


def test_review_verifier_quota_voids_the_review(tmp_path: Path):
    """The same class on the verifier: production aborts on a quota-hit
    verifier before parsing its output; the eval must void the review,
    not parse the dead session as a no-vote (which keeps every finding
    and scores a fail)."""
    repo = _fixture(tmp_path)
    record = run_seed(
        repo, seed_by_id("mocks-what-it-tests"),
        session_runner=QuotaVerifier(MOCK_SEED_FINDINGS),
    )
    assert record["layers"]["L2"] == {"quota_voided": True,
                                      "review_retries": 0}


def test_review_parse_failed_seat_keeps_its_one_rerun(tmp_path: Path):
    """No regression from the quota-order fix: a seat that COMPLETED but
    spoke unparseable output (not quota) still gets exactly one re-run,
    and the retry's verdict is the seat's verdict."""
    repo = _fixture(tmp_path)

    class GarbageFirstTime(ScriptedSeats):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._lock = threading.Lock()
            self._reviewer_calls: dict[str, int] = {}

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            if seat == "reviewer":
                match = re.search(r'"id": "(EVAL-[^"]+)"', prompt)
                assert match
                with self._lock:
                    calls = self._reviewer_calls.get(match.group(1), 0)
                    self._reviewer_calls[match.group(1)] = calls + 1
                if calls == 0:  # the seat's first verdict: protocol garbage
                    return SessionRecord(
                        seat=seat, status="completed", num_turns=2,
                        model="stub-model",
                        tokens={"input_tokens": 10, "output_tokens": 5},
                        result="I reviewed it, but here is no JSON.",
                    )
            return super().__call__(seat, prompt, run_dir, profile, **kwargs)

    record = run_seed(repo, seed_by_id("mocks-what-it-tests"),
                      session_runner=GarbageFirstTime(MOCK_SEED_FINDINGS),
                      reviewers=1)
    l2 = record["layers"]["L2"]
    assert l2["review_retries"] == 1, l2
    assert l2["verdict"] == "fail"  # the retry's verdict: findings kept
    assert record["caught_by"] == ["L2"]


# --- AC-3 (m-11): the clean line shows every layer --------------------------


def _failing_test_command_runner():
    """A command runner whose ``test`` gate fails — the L0 false positive
    a known-clean diff must render as."""
    from factory.gates import CommandResult, run_command

    def runner(name, command, repo, timeout_sec):
        if name == "test":
            return CommandResult(name=name, command=command, exit_code=1,
                                 output="assert 0\n1 failed")
        return run_command(name, command, Path(repo), timeout_sec)

    return runner


def test_ac_3(tmp_path: Path):
    """A clean diff's report line names L0, L1 AND L2: a clean diff that
    failed L0 renders as an L0 false positive, one that passed all three
    renders clean, a quota-voided L2 still reads no-verdict — and the
    false-fail metric is per layer, not L2-only."""
    from factory.eval import _clean_review_summary

    repo = _fixture(tmp_path)
    clean = next(d for d in CLEAN_DIFFS if d.id == "clean-docstring")

    # A clean diff whose test gate failed: L0's false positive, on the line.
    record = run_seed(repo, clean, command_runner=_failing_test_command_runner(),
                      session_runner=ScriptedSeats())
    assert record["layers"]["L0"]["verdict"] == "fail"
    line = _clean_review_summary(record)
    assert "L0 fail" in line and "false positive" in line, line  # RED today
    assert "L1 pass" in line, line
    assert "L2 pass" in line, line

    # A clean diff that passed everything renders clean, unmarked.
    passing = run_seed(repo, clean, session_runner=ScriptedSeats())
    clean_line = _clean_review_summary(passing)
    assert "L0 pass" in clean_line and "L1 pass" in clean_line, clean_line
    assert "L2 pass" in clean_line and "0 blocking" in clean_line, clean_line
    assert "false positive" not in clean_line, clean_line

    # A quota-voided L2 is still no verdict — never a pass or a fail.
    voided = run_seed(repo, clean, session_runner=QuotaReviewers())
    voided_line = _clean_review_summary(voided)
    assert "no verdict" in voided_line and "quota-voided" in voided_line, (
        voided_line)
    assert "L0 pass" in voided_line and "L1 pass" in voided_line, voided_line

    # The false-fail aggregate is per layer: L0's two failed clean runs
    # are counted against L0, not folded into an L2-only number.
    report = run_seeded_eval(
        repo, command_runner=_failing_test_command_runner(),
        session_runner=ScriptedSeats(),
    )
    false_fail = report["false_fail_on_clean"]
    assert false_fail["L0"] == {"failed": 2, "judged": 2}, false_fail
    assert false_fail["L1"] == {"failed": 0, "judged": 2}, false_fail
    assert false_fail["L2"] == {"failed": 0, "judged": 2}, false_fail
    text = render_report(report)
    assert "False-fail on clean diffs" in text
    false_fail_section = text.split("False-fail on clean diffs", 1)[1]
    assert "L0" in false_fail_section and "2" in false_fail_section


# --- AC-4 (I-12): repeats with spread ---------------------------------------


def test_ac_4(tmp_path: Path):
    """``--repeats N`` runs each seed N times; every rate metric carries
    the mean and the spread over the repeats (stdev and min–max), N=1
    renders spread n/a — never a 0 that fakes a measurement."""
    repo = _fixture(tmp_path)
    report = run_seeded_eval(repo, session_runner=FlakySeats(reviewers=1),
                             reviewers=1, repeats=2)
    assert report["repeats"] == 2
    # Per-run records: the flaky seed ran twice, once per repeat.
    flaky = [r for r in report["seeds"] if r["id"] == "mocks-what-it-tests"]
    assert [r["repeat"] for r in flaky] == [1, 2]
    assert flaky[0]["caught_by"] == ["L2"]
    assert flaky[1]["caught_by"] == []

    # L2 caught 1/6 in repeat 1, 0/6 in repeat 2: mean 1/12, spread over
    # exactly those two per-repeat rates.
    assert report["catch_rate_by_repeat"]["L2"] == [
        pytest.approx(1 / 6), pytest.approx(0.0),
    ]
    stats = report["catch_rate_stats"]["L2"]
    assert stats["mean"] == pytest.approx((1 / 6 + 0.0) / 2)
    assert stats["stdev"] == pytest.approx(statistics.stdev([1 / 6, 0.0]))
    assert stats["min"] == pytest.approx(0.0)
    assert stats["max"] == pytest.approx(1 / 6)
    # A deterministic layer measures a real zero spread at N=2 — 0, not n/a.
    assert report["catch_rate_by_repeat"]["L0"] == [
        pytest.approx(4 / 6), pytest.approx(4 / 6),
    ]
    assert report["catch_rate_stats"]["L0"]["stdev"] == pytest.approx(0.0)

    text = render_report(report)
    assert "2 repeats" in text
    assert "stdev" in text
    assert "spread n/a" not in text  # every layer judged both repeats

    # N=1 (the library default): no spread to state — n/a, never 0.
    single = run_seeded_eval(repo, session_runner=ScriptedSeats(MOCK_SEED_FINDINGS))
    assert single["repeats"] == 1
    for layer in ("L0", "L1", "L2"):
        stats = single["catch_rate_stats"][layer]
        assert stats is not None and stats["stdev"] is None, (layer, stats)
    assert "spread n/a" in render_report(single)


def test_review_repeats_validation_in_the_runner(tmp_path: Path):
    """The runner refuses a repeats below 1 (and a bool) up front — the
    CLI flag and the library seam share the rule."""
    repo = _fixture(tmp_path)
    for bad in (0, -1):
        with pytest.raises(EvalError):
            run_seeded_eval(repo, repeats=bad)


def test_review_cli_repeats_flag_reaches_the_runner(tmp_path, monkeypatch):
    """The flag reaches the runner: default 3 (the contract's default,
    owner to confirm), ``--repeats`` overrides."""
    from factory import cli
    from factory.eval import DEFAULT_REPEATS

    assert DEFAULT_REPEATS == 3
    root = tmp_path / "factory-root"
    root.mkdir()
    seen: list[int] = []

    def fake_run(factory_root, out_path, *, reviewers, repeats, keep_fixture):
        seen.append(repeats)
        rendered = tmp_path / "rendered.md"
        rendered.write_text("report")
        report = tmp_path / "report.json"
        report.write_text("{}")
        return {"report_path": report, "rendered_path": rendered}

    monkeypatch.setattr(cli, "run_on_demand_eval", fake_run)

    def run(*extra):
        return cli._run_eval(cli.build_parser().parse_args(
            ["eval", "--factory-root", str(root), *extra]))

    assert run() == 0 and seen[-1] == 3
    assert run("--repeats", "5") == 0 and seen[-1] == 5
    assert run("--repeats", "1") == 0 and seen[-1] == 1


def test_review_cli_repeats_refused_below_one(tmp_path, monkeypatch, capsys):
    """``--repeats 0`` / ``-1`` / non-integer: exit 2 with a message, and
    no eval runs on a refused value."""
    from factory import cli

    root = tmp_path / "factory-root"
    root.mkdir()

    def boom(*args, **kwargs):
        raise AssertionError("no eval may run on a refused --repeats")

    monkeypatch.setattr(cli, "run_on_demand_eval", boom)
    for bad in ("0", "-1"):
        parsed = cli.build_parser().parse_args(
            ["eval", "--factory-root", str(root), "--repeats", bad])
        assert cli._run_eval(parsed) == 2
        assert "--repeats" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(
            ["eval", "--factory-root", str(root), "--repeats", "two"])
    assert exc.value.code == 2  # argparse's own refusal, message on stderr


# --- hostile-pass pins -------------------------------------------------------


def test_review_quota_on_the_rerun_still_voids(tmp_path: Path):
    """A seat whose FIRST verdict is unparseable (not quota) gets its one
    re-run, and the re-run dies on quota: the re-run WAS spent (counted —
    tokens burned), and the review is still voided — quota noise from any
    seat, first run or re-run, is never a verdict."""
    repo = _fixture(tmp_path)

    class GarbageThenQuota(ScriptedSeats):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._lock = threading.Lock()
            self._calls: dict[str, int] = {}

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            if seat == "reviewer":
                contract = _contract_id_of(seat, prompt)
                with self._lock:
                    n = self._calls.get(contract, 0)
                    self._calls[contract] = n + 1
                if n == 0:
                    return SessionRecord(
                        seat=seat, status="completed", num_turns=2,
                        model="stub-model",
                        tokens={"input_tokens": 10, "output_tokens": 5},
                        result="reviewed, but no JSON",
                    )
                return _quota_seat("reviewer")
            return super().__call__(seat, prompt, run_dir, profile, **kwargs)

    record = run_seed(repo, seed_by_id("mocks-what-it-tests"),
                      session_runner=GarbageThenQuota(MOCK_SEED_FINDINGS),
                      reviewers=1)
    assert record["layers"]["L2"] == {"quota_voided": True,
                                      "review_retries": 1}


def test_review_every_repeat_quota_voided_renders_na(tmp_path: Path):
    """Repeats where EVERY L2 run is quota-voided: no denominator, so no
    rate, no mean, no spread — n/a lines, the quota count separate, and
    never a false 0% for L2. (L0's stdev 0% at two repeats is a REAL
    measured zero — the two rules must not be confused.)"""
    repo = _fixture(tmp_path)
    report = run_seeded_eval(
        repo, session_runner=QuotaReviewers(MOCK_SEED_FINDINGS), repeats=2)
    assert report["l2_quota_voided"] == 2 * (len(SOLOADVENTURER_SEEDS)
                                             + len(CLEAN_DIFFS))
    assert report["catch_rate_stats"]["L2"] is None
    assert report["catch_rate_by_repeat"]["L2"] == [None, None]
    text = render_report(report)
    assert "- L2: n/a (0 judged)" in text
    assert "L2: 0%" not in text  # a voided judge is never a 0% catch rate
    assert "quota-voided: 16" in text


def test_review_render_tolerates_a_pre_f48_report_json():
    """A report JSON written before this change (no ``repeats``, no
    ``repeat`` on records, the old L2-only false-fail aggregate) still
    renders — as a 1-repeat report with spread n/a — never a crash or a
    refusal."""
    old = {
        "fixture": "soloadventurer",
        "base_ref": "main",
        "l2": "sessions",
        "seeds": [
            {
                "id": "mocks-what-it-tests",
                "title": "a test that mocks the function it claims to test",
                "expected_layer": "L2",
                "layers": {"L0": {"verdict": "pass"},
                           "L1": {"verdict": "pass"},
                           "L2": {"verdict": "fail", "review_retries": 0}},
                "caught_by": ["L2"],
            },
        ],
        "clean": [
            {
                "id": "clean-docstring",
                "title": "a clearer docstring",
                "layers": {"L0": {"verdict": "pass"},
                           "L1": {"verdict": "pass"},
                           "L2": {"verdict": "pass"}},
                "blocking_findings": 0,
            },
        ],
        "l2_false_fail_on_clean": 0,  # the old key, deliberately present
        "l2_quota_voided": 0,
        "catch_rate": {"L0": 0.0, "L1": 0.0, "L2": 1.0},
        "caught_counts": {"L0": 0, "L1": 0, "L2": 1},
    }
    text = render_report(old)
    assert "clean-docstring" in text
    assert "spread n/a" in text
    assert "no verdict" not in text  # the L2 verdict is rendered, not voided


def test_review_seed_ids_stay_unique():
    """A duplicate seed id would double-count one diff in every rate —
    the fixture table is factory-authored, so this pins the invariant the
    report's by-id lookups lean on."""
    ids = [seed.id for seed in SOLOADVENTURER_SEEDS]
    assert len(set(ids)) == len(ids)
    assert len(set(d.id for d in CLEAN_DIFFS)) == len(CLEAN_DIFFS)
