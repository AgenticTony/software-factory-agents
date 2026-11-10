"""F-38 acceptance tests — parallel review with verifier voting.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f38.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed: same split as the F-10/F-23 tests — the
git fixture (local repo + bare remote + factory root), the clone manager,
the queue, L0 and L1 through the real pinned srt sandbox are real; only the
model seats are scripted (AC-4: no real model anywhere in this file). The
fan-out calls the scripted seats concurrently, exactly as it calls the real
runner, so the concurrency is itself under test.

The verifier seats are scripted *by finding text*, not by union id: which
reviewer's session registers a finding first is scheduling-dependent, so a
vote keyed on ``U-n`` would be flaky. The stub reads the findings array out
of the verifier prompt and votes per text.
"""

import json
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _support.f38 import (
    Env,
    Fanout,
    _git,
    _one_round_task,
    _verdict,
    _vote,
    _votes_payload,
)

from factory.config import ConfigError, FactoryConfig, load_config
from factory.contract import ContractError, load_profile
from factory.session import SessionRecord
from factory.state import TaskQueue

# A Sunday afternoon: outside every default peak window, so runs may start.

def _write_stub_record(settings_path, payload):
    """F-56 re-seat: the stub entry writes the record by FILE (the stdout
    record line is gone); payload is the record dict."""
    from factory.session import record_path_for

    record_path = record_path_for(Path(settings_path))
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(payload))


def _finding(text="scope creep", file="impl.py", axis="spec",
             severity="blocking", **extra) -> dict:
    item = {"id": "R-1", "axis": axis, "severity": severity, "file": file, "text": text}
    item.update(extra)
    return item


def _report_of(env: Env, attempt_no: int = -1) -> dict:
    """The stored validator report of an attempt (F-10's _store layout)."""
    task = env.task()
    return json.loads(
        Path(task["attempts"][attempt_no]["validator_report"]).read_text()
    )


def _by_text(report: dict, text: str) -> dict:
    matches = [
        f for f in report.get("findings") or []
        if text in f.get("text", "")
    ]
    assert len(matches) == 1, f"expected exactly one finding matching {text!r}"
    return matches[0]


# --- AC-1: N reviewers in parallel, then a verifier vote over the union -----


def test_ac_1(tmp_path: Path):
    from factory.prompts import prompt_versions

    assert FactoryConfig().reviewers == 2  # the config default (no file at all)

    # Reviewer scripts: both report A (same defect, so the union merges it);
    # one adds B, the other C. Scheduling decides which registers first, so
    # every assertion is order-independent. One attempt: the round's own
    # record is the thing under test.
    env = Env(tmp_path, tasks=[_one_round_task()])
    reviews = _verdict(
        _finding("empty-state row renders unknown", severity="major"),
        _finding("rename helper _do to _render", severity="minor"),
        declared="fail",
    )
    other = _verdict(
        _finding("empty-state row renders unknown", severity="major"),
        _finding("dead import os predates this change", severity="minor",
                 file="other.py"),
        declared="fail",
    )
    seats = Fanout(reviews=reviews, vote_by_text={
        "empty-state": _vote(tag="important",
                             reason="the offline row lies: unknown, not disabled"),
        "rename helper": _vote(tag="nit", reason="real, small"),
        "dead import": _vote(tag="pre-existing",
                             reason="the import predates the diff"),
    })
    # Distinct scripts per reviewer: whichever session starts first plays
    # `reviews`; the other plays `other` (returned by the hook under the
    # stub's lock, so a concurrent second call can never read the first
    # call's script).
    scripts = [reviews, other]
    state = {"n": 0}

    def pick_review():
        with seats.lock:
            state["n"] += 1
            return scripts[min(state["n"] - 1, 1)]

    seats.on_reviewer = pick_review
    result = env.run(seats)

    assert result.outcome == "needs_human", result  # declared fail stands
    seat_calls = seats.seats()
    assert seat_calls.count("implementer") == 1
    assert seat_calls.count("reviewer") == 2  # the config default fan-out
    assert seat_calls.count("verifier") == 1
    # Both reviewers got the same diff: identical prompts, fresh contexts.
    r1, r2 = seats.prompts("reviewer")
    assert r1 == r2
    assert "## Diff (base...head)" in r1
    # The verifier saw the union, including which reviewers reported what.
    verifier_prompt = seats.prompts("verifier")[0]
    for text in ("empty-state row renders unknown", "rename helper",
                 "dead import os"):
        assert text in verifier_prompt
    assert "reported_by" in verifier_prompt

    report = _report_of(env)
    findings = report["L2"]["findings"]
    assert len(findings) == 3  # A merged, B and C unique
    a = _by_text(report["L2"], "empty-state")
    assert a["reported_by"] == [1, 2]  # both reviewers, one entry
    assert a["verifier_reason"] == "the offline row lies: unknown, not disabled"
    # Ranked: severity first, then tag (important < nit < pre-existing).
    assert [f["text"] for f in findings] == [
        "empty-state row renders unknown",
        "rename helper _do to _render",
        "dead import os predates this change",
    ]
    assert [f["tag"] for f in findings] == ["important", "nit", "pre-existing"]
    assert report["L2"]["verdict"] == "fail"  # the declared fail stands
    # Every session's prompt hash is recorded per seat (AC-5 asserts this
    # fully; here just the shape the fan-out produced). F-46 AC-4 re-seat:
    # the expected versions come from the profile as the orchestrator
    # loads it (load_profile unions the schema-level runner-config
    # defaults into paths.protected, which the prompts embed).
    attempt = env.task()["attempts"][-1]
    assert attempt["prompt_versions"] == prompt_versions(
        load_profile(env.queue_path.parent / "project.yaml"))


# --- AC-2: style-only dropped before the verdict; the nit budget is enforced -


def test_ac_2(tmp_path: Path):
    # A passing round whose only findings are nits: the PR opens, and the
    # budget caps what reaches the body.
    env = Env(tmp_path)
    nits = [
        _finding(f"nit {i}: wording of message {i}", severity="minor")
        for i in range(1, 5)
    ] + [
        _finding(f"note {i}: trailing whitespace {i}", severity="note")
        for i in range(5, 7)
    ]
    seats = Fanout(reviews=_verdict(*nits, declared="pass"))
    result = env.run(seats)
    assert result.outcome == "pr_opened", result

    body = env.prs[0].body
    report = _report_of(env)
    l2_findings = report["L2"]["findings"]
    minor_note = [f for f in l2_findings if f["severity"] in ("minor", "note")]
    assert len(minor_note) == 3  # at most three nits survive
    assert len(l2_findings) == 3
    capped = [f"nit {i}" for i in (4,)] + [f"note {i}" for i in (5, 6)]
    for text in capped:
        assert text not in body, f"the capped nit reached the PR body: {text}"
    drops = env.task()["attempts"][-1]["review_drops"]
    assert {d["reason"] for d in drops} == {"nit_budget"}
    assert len(drops) == 3
    for text in capped:
        assert any(text in d["text"] for d in drops), f"{text} not in drops"
    assert all(d["severity"] in ("minor", "note") for d in drops)

    # A failing round: blocking and major survive the budget untouched, and
    # the verifier's style-only / diagnostic drops happen before the verdict.
    env2 = Env(tmp_path / "fail", tasks=[_one_round_task()])
    heavy = [
        _finding("writes to a table that does not exist", severity="blocking"),
        _finding("missing empty-state handling", severity="major"),
        *(
            _finding(f"nit {i}: wording of message {i}", severity="minor")
            for i in range(1, 6)
        ),
    ]
    seats2 = Fanout(reviews=_verdict(*heavy), vote_by_text={
        "nit 4": {"drop": True,
                  "reason": "style-only: import order is the linter's job"},
        "nit 5": {"drop": True,
                  "reason": "diagnostic: restates the L0 gate output"},
    })
    result2 = env2.run(seats2)
    assert result2.outcome == "needs_human", result2

    report2 = _report_of(env2)
    findings2 = report2["L2"]["findings"]
    assert _by_text(report2["L2"], "writes to a table")["severity"] == "blocking"
    assert any(
        f["severity"] == "major" and "empty-state" in f["text"]
        for f in findings2
    )
    survivors = [f for f in findings2 if f["severity"] in ("minor", "note")]
    assert len(survivors) == 3  # nits 1-3; 4 and 5 dropped, not budget-capped
    assert not any("nit 4" in f["text"] or "nit 5" in f["text"] for f in findings2)
    drops2 = env2.task()["attempts"][-1]["review_drops"]
    by_reason = {}
    for d in drops2:
        by_reason.setdefault(d["reason"].split(":", 1)[0], []).append(d["text"])
    assert "style-only" in by_reason and any("nit 4" in t for t in by_reason["style-only"])
    assert "diagnostic" in by_reason and any("nit 5" in t for t in by_reason["diagnostic"])
    assert report2["L2"]["verdict"] == "fail"  # blocking survived the vote


# --- AC-3: drops and reasons land in the attempt record ----------------------


def test_ac_3(tmp_path: Path):
    env = Env(tmp_path)
    seats = Fanout(
        reviews=_verdict(
            _finding("rename helper _do to _render", severity="minor"),
            _finding("claim lacks evidence: no gate output cited", severity="minor"),
            declared="pass",
        ),
        vote_by_text={
            "claim lacks evidence": {
                "drop": True, "reason": "not real: the test output is cited at line 9",
            },
            "rename helper": _vote(tag="nit", reason="real, small"),
        },
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened", result

    attempt = env.task()["attempts"][-1]
    drops = attempt["review_drops"]
    assert len(drops) == 1
    drop = drops[0]
    assert drop["text"] == "claim lacks evidence: no gate output cited"
    assert drop["reason"] == "not real: the test output is cited at line 9"
    assert drop["severity"] == "minor"
    assert drop["axis"] == "spec"
    # The stored validator report carries the same drop (audit trail).
    stored = _report_of(env)
    assert stored["L2"]["verifier_dropped"][0]["reason"].startswith("not real:")
    # The drop never reaches the PR body.
    assert "claim lacks evidence" not in env.prs[0].body

    # Schema-refused shapes: the attempt schema owns the field. A drop
    # without a reason is refused on write (the red-first half: before the
    # schema addition, ANY review_drops entry was refused).
    queue = TaskQueue(env.queue_path)
    good = dict(attempt, run_id="schema-check")
    queue.append_attempt("T-1", good)  # the full shape validates
    bad = dict(good, run_id="schema-check-2", review_drops=[{"text": "x"}])
    with pytest.raises(ContractError):
        queue.append_attempt("T-1", bad)


# --- AC-4: scripted sessions, one case per bullet -----------------------------


def test_ac_4(tmp_path: Path):
    # Case 1 — findings two reviewers both report merge into ONE union entry.
    env = Env(tmp_path, tasks=[_one_round_task()])
    seats = Fanout(
        reviews=_verdict(
            _finding("scope creep: answer() also logs", severity="major"),
            _finding("unique to this reviewer", severity="minor"),
            declared="fail",
        ),
    )
    # Second reviewer: the shared defect in different spacing/case (the
    # stable key must merge it) and nothing of its own.
    variant = _finding("scope   CREEP: answer() also logs", severity="major")
    scripts = [seats.reviews, _verdict(variant, declared="fail")]
    state = {"n": 0}

    def pick_review():
        with seats.lock:
            state["n"] += 1
            return scripts[min(state["n"] - 1, 1)]

    seats.on_reviewer = pick_review
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    report = _report_of(env)
    findings = report["L2"]["findings"]
    assert len(findings) == 2  # the duplicate merged; the unique kept
    scope = [f for f in findings if "creep" in f["text"].casefold()]
    assert len(scope) == 1
    assert scope[0]["reported_by"] == [1, 2]  # both reviewers, one entry
    unique = [f for f in findings if "unique to this reviewer" in f["text"]]
    assert len(unique) == 1 and unique[0]["reported_by"] == [1]

    # Case 2 — a finding the verifier drops never reaches the verdict or the
    # PR body. The dropped finding is the only blocking one and only ONE
    # reviewer reported it (a unanimous one would survive the vote — the
    # review round's unanimous rule), the reviewers declared pass: the
    # verdict flips to pass and the PR opens.
    env2 = Env(tmp_path / "case2")
    seats2 = Fanout(
        reviews=_verdict(
            _finding("blocking claim: mock hides a missing table", severity="blocking"),
            _finding("rename helper _do to _render", severity="minor"),
            declared="pass",
        ),
        vote_by_text={
            "blocking claim": {
                "drop": True,
                "reason": "not real: the table exists in the real schema",
            },
            "rename helper": _vote(tag="nit", reason="real, small"),
        },
    )
    scripts2 = [seats2.reviews, _verdict(declared="pass")]  # reviewer 2: clean
    state2 = {"n": 0}

    def pick_review2():
        with seats2.lock:
            state2["n"] += 1
            return scripts2[min(state2["n"] - 1, 1)]

    seats2.on_reviewer = pick_review2
    result2 = env2.run(seats2)
    assert result2.outcome == "pr_opened", result2
    report2 = _report_of(env2)
    assert report2["L2"]["verdict"] == "pass"
    assert not any(
        "blocking claim" in f["text"] for f in report2["L2"]["findings"]
    )
    body = env2.prs[0].body
    assert "blocking claim" not in body  # never reaches the PR body
    assert "rename helper" in body  # the kept survivor does
    drops = env2.task()["attempts"][-1]["review_drops"]
    assert any(d["reason"].startswith("not real:") for d in drops)


# --- AC-5: prompt hashes per seat; the fan-out stays inside the budgets -------


class _FakeTime:
    """A monotonic clock the scripted reviewer flips: after the reviewers
    return, every monotonic() call steps an hour, so the wall-clock budget
    reads as spent before the verifier would start."""

    def __init__(self):
        self.step = 0.0
        self._now = 1000.0

    def monotonic(self) -> float:
        self._now += self.step
        return self._now


def test_ac_5(tmp_path: Path, monkeypatch):
    from factory.prompts import assemble_verifier_prompt, prompt_hash, prompt_versions

    env = Env(tmp_path)
    seats = Fanout(
        reviews=_verdict(
            _finding("rename helper _do to _render", severity="minor"),
            declared="pass",
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened", result

    # Every reviewer and verifier prompt hash, one entry per session.
    # (F-46 AC-4 re-seat: expected hashes from the loaded profile — the
    # prompts embed the runner-config-default-unioned protected list.)
    attempt = env.task()["attempts"][-1]
    versions = attempt["prompt_versions"]
    assert set(versions) == {"implementer", "reviewer-1", "reviewer-2", "verifier"}
    loaded = load_profile(env.queue_path.parent / "project.yaml")
    assert versions == prompt_versions(loaded)
    assert versions["reviewer-1"] == versions["reviewer-2"]
    assert versions["verifier"] == prompt_hash(assemble_verifier_prompt(loaded))
    assert versions["verifier"] != versions["reviewer-1"]

    # F-64 AC-1 superseded F-38 finding 3's remainder sharing (owner
    # decision 2026-11-09, option D): every seat runs at its FIXED
    # parent-granted cap — reviewers at REVIEWER_TURN_CAP, the verifier
    # at VERIFIER_TURN_CAP — whatever the implementer's record claimed
    # (3 turns here). The attempt's recorded turns stay the sum over ALL
    # sessions, session-REPORTED telemetry only.
    from factory.orchestrator import REVIEWER_TURN_CAP, VERIFIER_TURN_CAP

    assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }
    assert seats.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP
    assert attempt["turns"] == 3 + 2 + 2 + 1  # impl + reviewer x2 + verifier

    # And inside the wall-clock budget: the verifier inherits the remaining
    # time after the reviewers, and never starts once it is spent.
    timeouts = {
        seat: [kw["timeout_min"] for kw in seats.kwargs(seat)]
        for seat in ("reviewer", "verifier")
    }
    assert all(0 < t <= 30 for t in timeouts["reviewer"])
    assert timeouts["verifier"][0] <= min(timeouts["reviewer"])

    env2 = Env(tmp_path / "spent")
    fake = _FakeTime()
    monkeypatch.setattr("factory.orchestrator.time", fake)

    def burn_wall_clock():
        fake.step = 3600.0  # the reviewers' session "takes" an hour

    seats2 = Fanout(
        reviews=_verdict(
            _finding("rename helper _do to _render", severity="minor"),
            declared="pass",
        ),
        on_reviewer=burn_wall_clock,
    )
    result2 = env2.run(seats2)
    assert result2.outcome == "needs_human", result2
    assert result2.escalation == "budget_exhausted"
    assert "verifier" not in seats2.seats()  # it never started
    attempt2 = env2.task()["attempts"][-1]
    assert attempt2["escalation"] == "budget_exhausted"
    assert "verifier" in attempt2["note"]


# ------------------------------------------------------------- focused --


def test_reviewer_count_comes_from_config(tmp_path: Path):
    env = Env(tmp_path)
    seats = Fanout(reviews=_verdict(declared="pass"))
    result = env.run(seats, config=FactoryConfig(reviewers=1))
    assert result.outcome == "pr_opened", result
    assert seats.seats().count("reviewer") == 1
    assert set(env.task()["attempts"][-1]["prompt_versions"]) == {
        "implementer", "reviewer-1", "verifier",
    }

    env3 = Env(tmp_path / "three")
    seats3 = Fanout(reviews=_verdict(declared="pass"))
    result3 = env3.run(seats3, config=FactoryConfig(reviewers=3))
    assert result3.outcome == "pr_opened", result3
    assert seats3.seats().count("reviewer") == 3


def test_config_reviewers_is_validated(tmp_path: Path):
    assert FactoryConfig().reviewers == 2
    assert FactoryConfig(peak_window=None, reviewers=4).reviewers == 4
    config = tmp_path / "config.yaml"
    config.write_text("reviewers: 3\n")
    assert load_config(config).reviewers == 3
    config.write_text("reviewers: 0\n")
    with pytest.raises(ConfigError):
        load_config(config)
    config.write_text("reviewers: two\n")
    with pytest.raises(ConfigError):
        load_config(config)
    config.write_text("reviewers: 2.5\n")
    with pytest.raises(ConfigError):
        load_config(config)
    config.write_text("reviewers: true\n")
    with pytest.raises(ConfigError):
        load_config(config)


def test_fanout_sessions_run_concurrently(tmp_path: Path):
    """The fan-out is really parallel: two sessions that must both be
    in flight before either returns (a barrier — sequential calls time out
    and raise BrokenBarrierError)."""
    from factory.session import run_sessions_parallel

    barrier = threading.Barrier(2, timeout=30)

    def session(seat, prompt, run_dir, profile, **kwargs):
        barrier.wait()  # only passes if the other session is also running
        return SessionRecord(seat=seat, status="completed", num_turns=1)

    records = run_sessions_parallel(
        [("reviewer", "one"), ("reviewer", "two")],
        tmp_path, {}, runner=session, max_turns=5, timeout_min=1,
    )
    assert [r.status for r in records] == ["completed", "completed"]
    assert [r.seat for r in records] == ["reviewer", "reviewer"]


def test_fanout_guard_covers_every_reviewer_session(tmp_path, monkeypatch):
    """The reviewer integrity guard spans the whole fan-out: a mutation by
    ANY reviewer session fails every record of the fan-out (no verdict from
    a fan-out that touched the workspace is trusted), restores the
    workspace, and tears the scratch down."""
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    calls = {"n": 0}

    def mutating_run(cmd, run_dir, settings_path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the first reviewer mutates: a smuggled file
            (repo / "src" / "smuggled.py").write_text("injected = True\n")
        _write_stub_record(
            settings_path,
            {"seat": "reviewer", "status": "completed",
             "result": '{"verdict": "pass", "findings": []}'})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(session_module, "run_sandboxed", mutating_run)
    records = session_module.run_sessions_parallel(
        [("reviewer", "one"), ("reviewer", "two")],
        repo, profile,
        factory_root=tmp_path, settings_path=tmp_path / "base.json",
        max_turns=5, timeout_min=1, token="t", cli_path=Path("/bin/echo"),
    )
    assert calls["n"] == 2
    assert all(r.status == "failed" for r in records)
    assert all(r.reason == session_module.REVIEWER_MUTATION for r in records)
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert head_after == head_before, "the workspace must be restored"
    porcelain = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == "", f"tree not clean after restore: {porcelain!r}"
    assert not (repo / "src" / "smuggled.py").exists()
    # Per-session scratch (review finding 1): nothing was ever created or
    # symlinked inside the clone — no .home/.tmp at all.
    assert not (repo / ".home").exists() and not (repo / ".tmp").exists()
    # Every per-session artifact is gone: settings, staged runner, scratch.
    sandbox_dir = (tmp_path / "base.json").parent
    assert not list(sandbox_dir.glob("base.json-r*.json"))
    assert not list(sandbox_dir.glob("base.json-r*-runner"))
    assert not list(sandbox_dir.glob("base.json-r*-scratch"))


def _sandbox_repo(tmp_path: Path):
    """A minimal repo + profile for direct session-level tests (the F-09
    review-round pattern: real git, scripted sandbox)."""
    import yaml

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git_init(repo)
    (repo / "src" / "mod.py").write_text("x = 1\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'p'\nversion = '0'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    profile = {
        "name": "p", "repo": {"path": str(repo), "default_branch": "main",
                              "remote": str(tmp_path / "r.git")},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {}, "docs_allowlist": [],
    }
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile))
    return repo, profile


def _git_init(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")


def test_parse_verifier_vote_shapes():
    from factory.l2 import L2Finding, parse_verifier_vote

    def union_finding(uid: str, text: str) -> L2Finding:
        f = L2Finding(id=uid, text=text, axis="spec", severity="minor")
        f.key = text
        return f

    findings = [union_finding("U-1", "real defect"), union_finding("U-2", "noise")]
    ok = _votes_payload(
        {"id": "U-1", "keep": True, "reason": "confirmed", "tag": "important"},
        {"id": "U-2", "keep": False, "reason": "style-only: linter owns it"},
    )
    kept, dropped, error = parse_verifier_vote(ok, findings)
    assert error is None
    assert [f.id for f in kept] == ["U-1"]
    assert kept[0].tag == "important" and kept[0].verifier_reason == "confirmed"
    assert [d["finding"]["id"] for d in dropped] == ["U-2"]
    assert dropped[0]["reason"] == "style-only: linter owns it"

    # Not text / not JSON / wrong shape: nothing is dropped (fail-open on
    # keeps — an unverifiable vote may not remove a finding), error set.
    for bad in (None, "", "not json", '{"votes": "no"}',
                '{"votes": [{"id": "U-9", "keep": false, "reason": "x"}]}',
                '{"votes": [{"id": "U-1", "keep": true, "reason": "x"}]}',
                '{"votes": [{"id": "U-1", "keep": true, "reason": "x", "tag": "big"}]}',
                '{"votes": [{"id": "U-1", "keep": false}]}',
                '{"votes": [{"id": "U-1", "keep": false, "reason": "x"}], "votes": []}'):
        kept, dropped, error = parse_verifier_vote(bad, findings)
        assert error is not None, f"expected an error for {bad!r}"
        assert dropped == []
        assert len(kept) == 2  # everything survives an invalid vote

    # A finding nobody voted on is kept, untagged by the vote (the default
    # tag comes from severity at vote-application time).
    partial = _votes_payload(
        {"id": "U-2", "keep": False, "reason": "noise"},
    )
    kept, dropped, error = parse_verifier_vote(partial, findings)
    assert error is None
    assert [f.id for f in kept] == ["U-1"]
    assert kept[0].verifier_reason is None


def test_apply_vote_ranks_and_tags():
    from factory.l2 import apply_vote, merge_reviews, parse_verdict, parse_verifier_vote

    verdict = parse_verdict(_verdict(
        _finding("blocking bug", severity="blocking"),
        _finding("pre-existing oddity", severity="major", file="old.py"),
        _finding("small nit", severity="minor"),
        _finding("tidy note", severity="note"),
    ))
    union = merge_reviews([verdict])
    votes = _votes_payload(
        {"id": union.findings[0].id, "keep": True, "reason": "real",
         "tag": "pre-existing"},
        {"id": union.findings[1].id, "keep": True, "reason": "predates",
         "tag": "pre-existing"},
        {"id": union.findings[2].id, "keep": True, "reason": "real",
         "tag": "important"},
        {"id": union.findings[3].id, "keep": True, "reason": "real",
         "tag": "nit"},
    )
    outcome = parse_verifier_vote(votes, union.findings)
    assert outcome.error is None and outcome.dropped == []
    report = apply_vote(union, outcome)
    # Ranked by severity first, then tag; a blocking finding is never
    # tagged below important even when the voter tried.
    assert [f.text for f in report.findings] == [
        "blocking bug", "pre-existing oddity", "small nit", "tidy note",
    ]
    assert [f.tag for f in report.findings] == [
        "important", "pre-existing", "important", "nit",
    ]
    assert report.verdict == "fail"  # the blocking finding carries the verdict


def test_verifier_dropped_section_never_reaches_the_pr_body():
    from factory.prbody import _validator_section

    reports = {"L2": {
        "layer": "L2", "verdict": "pass", "findings": [],
        "commands": [], "dropped": [], "verifier_dropped": [
            {"finding": {"id": "U-1", "text": "voted-out noise zqk"}, "reason": "x"},
        ],
    }}
    section = _validator_section(reports)
    assert "voted-out noise zqk" not in section
    assert "verifier_dropped" not in section
    assert reports["L2"]["verifier_dropped"]  # the input dict is not mutated


def test_no_progress_when_the_verifier_drops_everything():
    """Review round finding 4 split this case: on a delta round (2+) the
    F-23 all_dropped stop fires exactly as before; on the FIRST round the
    reviewers' declared fail lost its whole substance to the vote, so the
    round passes (see test_review4)."""
    from factory.l2 import (
        apply_convergence,
        apply_vote,
        merge_reviews,
        parse_verdict,
        parse_verifier_vote,
    )

    verdict = parse_verdict(_verdict(
        _finding("voted out", severity="minor"), declared="fail",
    ))
    union = merge_reviews([verdict])
    votes = _votes_payload(
        {"id": union.findings[0].id, "keep": False, "reason": "not real"},
    )
    voted = apply_vote(union, parse_verifier_vote(votes, union.findings))
    final = apply_convergence(voted, prior={}, delta_files=set(), delta_mode=True)
    assert final.verdict == "fail"  # the declared fail stands
    assert final.no_progress and final.no_progress_reason == "all_dropped"


def test_two_real_sandboxes_run_concurrently(tmp_path: Path):
    """The fan-out's substrate: two srt invocations at once, each under
    its own settings file, both confined and both returning — the real
    sandbox stack (not the monkeypatched one the guard test uses) must
    tolerate concurrent profiles.

    Each session is built exactly as ``_fanout_sandboxed`` builds it:
    read-only over the shared run dir, writable only in its OWN scratch.
    The shape matters on Linux: srt stubs every absent deny path under a
    WRITABLE dir with a bwrap mount point and deletes the stubs when it
    exits, counting only its own sandboxes — two writable srt processes
    sharing one dir delete each other's stubs mid-start ("bwrap: Can't
    find source path …/.git"). The fan-out never shares a writable dir,
    so neither may this test, or it tests a shape production never runs
    and flakes under load (CI run 42236785271)."""
    from factory.sandbox import build_settings, run_sandboxed, write_settings
    from factory.session import _scratch_dirs

    repo, profile = _sandbox_repo(tmp_path)
    root = tmp_path / "factory-root"
    runs = root / "runs" / "p" / "20261030T000000Z-aaaaaa"
    runs.mkdir(parents=True)
    sandbox_dir = root / "sandbox"
    sandbox_dir.mkdir(parents=True)
    script = "import sys; print(sys.argv[1] + ':ok')"

    def one(name: str):
        settings = sandbox_dir / f"{name}.json"
        scratch = _scratch_dirs(settings.with_name(settings.name + "-scratch"))
        write_settings(
            build_settings(
                profile, runs, factory_root=root,
                model_endpoint="https://api.z.ai/api/anthropic",
                read_only=True, scratch_write=scratch,
            ),
            settings,
        )
        return run_sandboxed(
            [sys.executable, "-c", script, name], runs, settings,
            timeout=120, scratch=scratch,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        procs = [
            future.result()
            for future in (pool.submit(one, "r1"), pool.submit(one, "r2"))
        ]
    outputs = sorted(proc.stdout.split() for proc in procs)
    assert outputs == [["r1:ok"], ["r2:ok"]], (
        f"a concurrent sandbox died:\n{[p.stdout + p.stderr for p in procs]}"
    )


# ------------------------------------------------- owner review round (PR #78) --


def test_review1_fanout_scratch_is_per_session(tmp_path, monkeypatch):
    """Finding 1: every fan-out session gets its OWN writable scratch —
    no shared .home/.tmp, so no session can read or rewrite another's
    state. Unit half: sandbox_env points HOME/TMPDIR/CLAUDE_CODE_TMPDIR/
    GIT_TEMPLATE_DIR at the given scratch; wiring half: the fan-out hands
    each session a distinct scratch and a settings file whose allowWrite
    lists only that session's scratch (never run_dir/.home — a shared
    writable dir would be a covert channel — and never the other
    session's)."""
    import factory.session as session_module
    from factory.sandbox import sandbox_env

    home1, tmp1 = tmp_path / "s1" / "home", tmp_path / "s1" / "tmp"
    env = sandbox_env(tmp_path / "run", scratch=(home1, tmp1))
    assert env["HOME"] == str(home1)
    assert env["TMPDIR"] == str(tmp1)
    assert env["CLAUDE_CODE_TMPDIR"] == str(tmp1)
    assert env["GIT_TEMPLATE_DIR"].startswith(str(home1))
    assert home1.is_dir() and tmp1.is_dir()

    repo, profile = _sandbox_repo(tmp_path / "repo-root")
    captured: list[tuple[tuple, list]] = []  # (scratch, allowWrite)

    def fake_run(cmd, run_dir, settings_path, *, scratch=None, **kwargs):
        allow_write = json.loads(
            Path(settings_path).read_text()
        )["filesystem"]["allowWrite"]
        captured.append((scratch, allow_write))
        _write_stub_record(
            settings_path,
            {"seat": "reviewer", "status": "completed", "result": "{}"})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(session_module, "run_sandboxed", fake_run)
    session_module.run_sessions_parallel(
        [("reviewer", "one"), ("reviewer", "two")],
        repo, profile,
        factory_root=tmp_path / "repo-root", settings_path=tmp_path / "base.json",
        max_turns=5, timeout_min=1, token="t", cli_path=Path("/bin/echo"),
    )
    assert len(captured) == 2
    scratches = [scratch for scratch, _ in captured]
    assert scratches[0] is not None and scratches[1] is not None
    assert scratches[0] != scratches[1]  # distinct per session
    allow_writes = [writes for _, writes in captured]
    assert not any(str(repo / ".home") in w for w in allow_writes), (
        "the run dir's .home must not be fan-out-writable: a shared writable "
        "dir is a channel between 'independent' reviewers"
    )
    (scratch_a, writes_a), (scratch_b, writes_b) = captured
    for scratch, writes in ((scratch_a, writes_a), (scratch_b, writes_b)):
        assert str(scratch[0]) in writes and str(scratch[1]) in writes
    assert str(scratch_b[0]) not in writes_a and str(scratch_b[1]) not in writes_a
    assert str(scratch_a[0]) not in writes_b and str(scratch_a[1]) not in writes_b
    # Every per-session scratch (and settings, and staged runner) is gone.
    sandbox_dir = (tmp_path / "base.json").parent
    assert not list(sandbox_dir.glob("base.json-r*"))


def test_review1_real_sandboxes_isolate_their_homes(tmp_path: Path):
    """Finding 1, the real-sandbox half: two concurrent srt children, each
    with its own scratch, each writes a marker into its $HOME and lists
    $HOME — each sees exactly its own file. A shared HOME would show two."""
    from factory.sandbox import build_settings, run_sandboxed, write_settings

    repo, profile = _sandbox_repo(tmp_path)
    root = tmp_path / "factory-root"
    runs = root / "runs" / "p" / "20261030T000000Z-bbbbbb"
    runs.mkdir(parents=True)
    sandbox_dir = root / "sandbox"
    sandbox_dir.mkdir(parents=True)
    script = textwrap.dedent(
        """
        import os, pathlib
        home = pathlib.Path(os.environ["HOME"])
        (home / "marker").write_text("mine")
        files = sorted(p.name for p in home.iterdir() if not p.name.startswith("."))
        print("files:" + ",".join(files))
        """
    )

    def one(name: str):
        scratch = (sandbox_dir / f"{name}-scratch" / "home",
                   sandbox_dir / f"{name}-scratch" / "tmp")
        settings = sandbox_dir / f"{name}.json"
        write_settings(
            build_settings(
                profile, runs, factory_root=root, read_only=True,
                scratch_write=scratch,
                model_endpoint="https://api.z.ai/api/anthropic",
            ),
            settings,
        )
        return run_sandboxed(
            [sys.executable, "-c", script], runs, settings, timeout=120,
            scratch=scratch,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        procs = [pool.submit(one, "r1").result(), pool.submit(one, "r2").result()]
    markers = sorted(
        line for proc in procs for line in proc.stdout.splitlines()
        if line.startswith("files:")
    )
    assert markers == ["files:marker", "files:marker"], (
        f"a session saw the other's HOME file:\n{[p.stdout + p.stderr for p in procs]}"
    )


def test_real_fanout_scratch_reads_back_under_a_denied_home(tmp_path, monkeypatch):
    """A session must READ what it writes to its own scratch. The real
    factory root sits under $HOME, which the sandbox denies for reading,
    so a scratch only on allowWrite kills every real reviewer at CLI
    startup (F-16 run 20261030T120742Z-75e6df: ProcessError exit 1 in 5s,
    no model call). The earlier real-sandbox test missed it because its
    tmp_path scratch sat outside the denied home. Here the factory root
    is under a denied HOME, as in production, and the other session's
    scratch stays unreadable."""
    from factory.sandbox import build_settings, run_sandboxed, write_settings
    from factory.session import _scratch_dirs

    repo, profile = _sandbox_repo(tmp_path)
    home = tmp_path / "home"
    root = home / "factory"
    run = root / "runs" / "p" / "20261030T000000Z-aaaaaa"
    run.mkdir(parents=True)
    sandbox_dir = root / "sandbox"
    sandbox_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    mine = _scratch_dirs(sandbox_dir / "base.json-r1.json-scratch")
    other = _scratch_dirs(sandbox_dir / "base.json-r2.json-scratch")
    (other[0] / "secret.txt").write_text("other session\n")

    settings = sandbox_dir / "base.json-r1.json"
    built = build_settings(
        profile, run, factory_root=root,
        model_endpoint="https://api.z.ai/api/anthropic",
        read_only=True, scratch_write=mine,
    )
    write_settings(built, settings)
    assert str(mine[0].resolve()) in built["filesystem"]["allowRead"]
    assert str(other[0].resolve()) not in built["filesystem"]["allowRead"]

    script = textwrap.dedent("""
        import os, pathlib, sys
        for var in ("HOME", "TMPDIR"):
            probe = pathlib.Path(os.environ[var]) / "probe.txt"
            probe.write_text("x")
            assert probe.read_text() == "x"
        try:
            pathlib.Path(sys.argv[1]).read_text()
        except OSError:
            print("isolated")
        else:
            print("LEAK")
        print("readback-ok")
    """)
    proc = run_sandboxed(
        [sys.executable, "-c", script, str(other[0] / "secret.txt")],
        run, settings, timeout=120, scratch=mine,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.split() == ["isolated", "readback-ok"], proc.stdout + proc.stderr


def test_real_single_session_scratch_reads_back_under_a_denied_home(
    tmp_path, monkeypatch,
):
    """The verifier runs as one guarded session, not through the fan-out,
    so its scratch arrives as ``writable_roots`` through the run dir's
    ``.home``/``.tmp`` links. Writable alone still kills the CLI at startup
    under a denied home (F-16 run 20261030T134446Z-f57b83: the verifier
    failed in 5s in both rounds while the fan-out reviewers completed)."""
    from factory.sandbox import build_settings, run_sandboxed, write_settings
    from factory.session import _reviewer_scratch, _teardown_reviewer_scratch

    repo, profile = _sandbox_repo(tmp_path)
    home = tmp_path / "home"
    root = home / "factory"
    run = root / "runs" / "p" / "20261030T000000Z-aaaaaa"
    run.mkdir(parents=True)
    sandbox_dir = root / "sandbox"
    sandbox_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    settings = sandbox_dir / "verifier.json"
    writable = _reviewer_scratch(settings, run)
    built = build_settings(
        profile, run, factory_root=root,
        model_endpoint="https://api.z.ai/api/anthropic",
        read_only=True, writable_roots=writable,
    )
    write_settings(built, settings)
    script = textwrap.dedent("""
        import os, pathlib
        for var in ("HOME", "TMPDIR"):
            probe = pathlib.Path(os.environ[var]) / "probe.txt"
            probe.write_text("x")
            assert probe.read_text() == "x"
        print("readback-ok")
    """)
    try:
        proc = run_sandboxed(
            [sys.executable, "-c", script], run, settings, timeout=120,
        )
    finally:
        _teardown_reviewer_scratch(run)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.split() == ["readback-ok"], proc.stdout + proc.stderr


def test_review2_a_unanimous_blocking_finding_survives_the_vote():
    """Finding 2: a blocking or major finding that EVERY reviewer reported
    cannot be dropped by one verifier — it survives tagged important, and
    the refused drop lands in the ledger as override_refused. A finding
    fewer than all reviewers reported keeps today's semantics (droppable,
    verdict and all)."""
    from factory.l2 import apply_vote, merge_reviews, parse_verdict, parse_verifier_vote

    unanimous = parse_verdict(_verdict(
        _finding("all reviewers saw this bug", severity="blocking"),
    ))
    union = merge_reviews([unanimous, parse_verdict(_verdict(
        _finding("all reviewers saw this bug", severity="blocking"),
    ))])
    assert union.findings[0].reported_by == [1, 2]
    votes = _votes_payload(
        {"id": union.findings[0].id, "keep": False, "reason": "not real"},
    )
    report = apply_vote(union, parse_verifier_vote(votes, union.findings), reviewers=2)
    assert [f.text for f in report.findings] == ["all reviewers saw this bug"]
    assert report.findings[0].tag == "important"
    assert report.verdict == "fail"  # the round still fails
    assert len(report.verifier_dropped) == 1
    assert report.verifier_dropped[0]["reason"].startswith("override_refused:")

    # Fewer than all reviewers: droppable, exactly as before.
    minority = parse_verdict(_verdict(
        _finding("only one reviewer saw this bug", severity="blocking"),
        declared="pass",
    ))
    union2 = merge_reviews([minority, parse_verdict(_verdict(declared="pass"))])
    votes2 = _votes_payload(
        {"id": union2.findings[0].id, "keep": False, "reason": "not real"},
    )
    report2 = apply_vote(
        union2, parse_verifier_vote(votes2, union2.findings), reviewers=2
    )
    assert not report2.findings and report2.verdict == "pass"


def test_review2_unanimous_blocking_still_fails_the_round(tmp_path: Path):
    """Finding 2, integration: both reviewers report the same blocking
    finding, the verifier tries to drop it, the round still fails and the
    refused drop is recorded in the attempt record."""
    env = Env(tmp_path, tasks=[_one_round_task()])
    blocking = _finding("phantom table blocks the release", severity="blocking")
    seats = Fanout(
        reviews=_verdict(blocking, declared="fail"),
        vote_by_text={
            "phantom table": {"drop": True, "reason": "not real: schema has it"},
        },
    )
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    report = _report_of(env)
    assert _by_text(report["L2"], "phantom table")["severity"] == "blocking"
    assert report["L2"]["verdict"] == "fail"
    drops = env.task()["attempts"][-1]["review_drops"]
    assert any(
        d["reason"].startswith("override_refused:") for d in drops
    ), f"the refused drop is not in the ledger: {drops}"


def test_review3_turn_caps_are_fixed_not_remainder(tmp_path: Path):
    """F-38 finding 3 as AMENDED by F-64 (owner decision 2026-11-09,
    option D): the seats' turn caps are the parent's FIXED constants.
    The stub echoes half its cap as turns: implementer 3, reviewers at
    REVIEWER_TURN_CAP (spending 10 each), verifier at VERIFIER_TURN_CAP
    (spending 5) — and a budget the implementer's record claims to have
    spent (19, or 15 with the reviewers claiming their full caps) still
    runs the whole fan-out at full caps: review is the quality guarantee,
    never squeezed out, and no record funds or drains a seat."""
    from factory.orchestrator import REVIEWER_TURN_CAP, VERIFIER_TURN_CAP

    env = Env(tmp_path)
    seats = Fanout(
        reviews=_verdict(_finding("rename helper", severity="minor"), declared="pass"),
    )
    seats.echo_turn_cap = True
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }
    assert seats.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP
    attempt = env.task()["attempts"][-1]
    assert attempt["turns"] == 3 + 2 * (REVIEWER_TURN_CAP // 2) + (
        VERIFIER_TURN_CAP // 2)  # impl + reviewer echoes + verifier echo

    # The pre-F-64 refusal cases are gone: an implementer that "spent" 19
    # of the 20 turns, and reviewers claiming their full caps, still get
    # the complete fan-out at full caps — no turn-budget escalation.
    env2 = Env(tmp_path / "spent", tasks=[_one_round_task()])
    seats2 = Fanout(
        reviews=_verdict(declared="pass"),
        implementer_turns=19,
    )
    result2 = env2.run(seats2)
    assert result2.outcome == "pr_opened", result2
    assert seats2.seats().count("reviewer") == 2
    assert {kw["max_turns"] for kw in seats2.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }

    env3 = Env(tmp_path / "verifier-floor", tasks=[_one_round_task()])
    seats3 = Fanout(
        reviews=_verdict(_finding("rename helper", severity="minor"),
                         declared="pass"),
        implementer_turns=15,
    )
    seats3.echo_turn_cap = False  # reviewers claim their FULL caps
    result3 = env3.run(seats3)
    assert result3.outcome == "pr_opened", result3
    assert seats3.seats().count("verifier") == 1
    assert seats3.kwargs("verifier")[0]["max_turns"] == VERIFIER_TURN_CAP


def test_review4_round_one_fail_voted_out_passes(tmp_path: Path):
    """Finding 4: on the FIRST review, a declared fail whose every finding
    the verifier voted out is an unsupported fail — the round passes on
    L2, the PR opens, and the PR body says the reviewers' fail was voted
    out. Delta rounds keep the F-23 all_dropped stop (unit half in
    test_no_progress_when_the_verifier_drops_everything)."""
    from factory.l2 import (
        apply_convergence,
        apply_vote,
        merge_reviews,
        parse_verdict,
        parse_verifier_vote,
    )

    verdict = parse_verdict(_verdict(
        _finding("voted out", severity="minor"), declared="fail",
    ))
    union = merge_reviews([verdict])
    votes = _votes_payload(
        {"id": union.findings[0].id, "keep": False, "reason": "not real"},
    )
    voted = apply_vote(union, parse_verifier_vote(votes, union.findings))
    first = apply_convergence(voted, prior={}, delta_files=set(), delta_mode=False)
    assert first.verdict == "pass"  # the unsupported fail does not stand
    assert not first.no_progress

    env = Env(tmp_path)
    seats = Fanout(
        reviews=_verdict(
            _finding("minor quibble the vote removes", severity="minor"),
            declared="fail",
        ),
        vote_by_text={"minor quibble": {"drop": True, "reason": "style-only: noise"}},
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    report = _report_of(env)
    assert report["L2"]["declared"] == "fail" and report["L2"]["verdict"] == "pass"
    assert report["L2"]["verifier_dropped"]
    body = env.prs[0].body
    assert "reviewers declared FAIL" in body and "voted out" in body


def test_review5_fanout_forwards_per_session_kwargs(tmp_path: Path):
    """Finding 5 (structural guard): the fan-out forwards unknown per-session
    kwargs through the shared _run_prepared step unchanged — so F-22's
    toolkit= threads through session_kwargs with no fan-out change when
    that PR rebases onto this one (or vice versa)."""
    seen: list[dict] = []

    def runner(seat, prompt, run_dir, profile, **kwargs):
        seen.append(kwargs)
        return SessionRecord(seat=seat, status="completed", num_turns=1)

    from factory.session import run_sessions_parallel

    run_sessions_parallel(
        [("reviewer", "one")], tmp_path, {},
        runner=runner, max_turns=5, timeout_min=1,
        toolkit={"staged": "/tmp/fake-toolkit", "items": []},
    )
    assert seen[0].get("toolkit") == {"staged": "/tmp/fake-toolkit", "items": []}


# ------------------------------- review round 2 (PR #78): the toolkit through the fan-out --


def test_review6_fanout_sessions_carry_the_toolkit(tmp_path, monkeypatch):
    """F-22 × F-38 (a): every fan-out session gets toolkit_reviewer through
    _run_prepared exactly as a single reviewer did — staged per session
    into its OWN -toolkit dir, allow_read covering exactly that staged
    dir, and the built options carrying the plugin dir, the MCP server
    and the mcp guard hook. The options half drives run_session per seat
    with the spec the fan-out's own staging produced (the f22 ac_7
    pattern); the plumbing half captures specs/settings per session from
    a monkeypatched sandbox."""
    from _support.f22 import _toolkit_with

    import factory.session as session_module
    from factory.toolkit import resolve_toolkit

    toolkit_root, skill, mcp = _toolkit_with(tmp_path / "tk")
    repo, profile = _sandbox_repo(tmp_path / "repo-root")
    profile["docs_allowlist"] = ["docs.example.com"]  # the MCP server's domain
    profile["toolkit"] = {
        "skills": ["demo-skill"], "plugins": [],
        "mcp_servers": ["docs"],
        "seats": {"reviewer": {"skills": ["demo-skill"],
                               "mcp_servers": ["docs"]}},
    }
    resolved = resolve_toolkit(profile, "reviewer", toolkit_root=toolkit_root)
    assert [i["name"] for i in resolved["items"]] == ["demo-skill", "docs"]

    specs_seen: list[dict] = []

    def fake_run(cmd, run_dir, settings_path, *, scratch=None, **kwargs):
        spec = json.loads(Path(cmd[-1]).read_text())
        staged = Path(spec["toolkit"]["staged"])
        # Inside the fake: the staged tree exists while the session runs
        # (the fan-out's cleanup removes it before the test can look).
        specs_seen.append({
            "spec": spec,
            "settings": json.loads(Path(settings_path).read_text()),
            "staged": staged,
            "staged_files_ok": (
                (staged / "skills" / "demo-skill" / "SKILL.md").is_file()
                and (staged / "mcp" / "docs" / "server.js").is_file()
            ),
            "scratch": scratch,
        })
        _write_stub_record(
            settings_path,
            {"seat": "reviewer", "status": "completed", "result": "{}"})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(session_module, "run_sandboxed", fake_run)
    session_module.run_sessions_parallel(
        [("reviewer", "one"), ("reviewer", "two")],
        repo, profile,
        factory_root=tmp_path / "repo-root", settings_path=tmp_path / "base.json",
        max_turns=5, timeout_min=1, token="t", cli_path=Path("/bin/echo"),
        toolkit=resolved,
    )
    assert len(specs_seen) == 2
    staged_roots = []
    for entry in specs_seen:
        staged = entry["staged"]
        staged_roots.append(staged)
        assert staged.name.endswith("-toolkit")
        assert entry["staged_files_ok"], "the staged copy is incomplete"
        # allow_read covers exactly this session's staged dir…
        written = entry["settings"]
        assert str(staged) in written["filesystem"]["allowRead"]
        # …and nothing is writable there (read-only copy).
        assert not any(
            Path(w) == staged or staged in Path(w).parents
            for w in written["filesystem"]["allowWrite"]
        )
    assert staged_roots[0] != staged_roots[1]  # per-session staging, no sharing

    # The options half: with one fan-out session's staged spec, the built
    # options carry the plugin dir, the MCP server and the guard hook —
    # for the reviewer seat AND the verifier seat (both get
    # toolkit_reviewer; the verifier judges the reviewers' findings, so it
    # needs the same read-only evidence surface — see the orchestrator's
    # verifier call).
    import asyncio

    async def query(*, prompt, options, transport=None):
        entry["options"] = options
        yield type("M", (), {"is_error": False, "subtype": None, "num_turns": 1})()

    for seat in ("reviewer", "verifier"):
        captured: dict = {}

        async def q(*, prompt, options, transport=None):
            captured["options"] = options
            yield type("M", (), {"is_error": False, "subtype": None,
                                 "num_turns": 1})()

        asyncio.run(session_module.run_session(
            seat, "p", tmp_path, 2, 1,
            query_fn=q, base_url="https://api.z.ai/api/anthropic", token="t",
            toolkit={"staged": str(staged_roots[0]),
                     "items": resolved["items"]},
        ))
        options = captured["options"]
        assert options.plugins and options.plugins[0]["path"] == str(staged_roots[0])
        assert "docs" in options.mcp_servers
        assert any(
            hm.matcher and hm.matcher.startswith("mcp__")
            for hm in options.hooks["PreToolUse"]
        ), f"the {seat} seat has no MCP guard hook"


def test_review7_toolkit_dirs_torn_down_even_on_exception(tmp_path, monkeypatch):
    """F-22 × F-38 (b): each session's -toolkit dir (with its settings,
    runner and scratch) is gone after the fan-out — on the happy path AND
    when a session raises mid-fan-out."""
    from _support.f22 import _toolkit_with

    import factory.session as session_module
    from factory.toolkit import resolve_toolkit

    def run(exc: bool):
        toolkit_root, _, _ = _toolkit_with(tmp_path / f"tk{int(exc)}")
        repo, profile = _sandbox_repo(tmp_path / f"repo{int(exc)}")
        profile["docs_allowlist"] = ["docs.example.com"]
        profile["toolkit"] = {"skills": ["demo-skill"], "plugins": [],
                              "mcp_servers": []}
        resolved = resolve_toolkit(profile, "reviewer",
                                   toolkit_root=toolkit_root)
        calls = {"n": 0}

        def fake_run(cmd, run_dir, settings_path, *, scratch=None, **kwargs):
            calls["n"] += 1
            if exc and calls["n"] == 2:
                raise RuntimeError("session 2 blew up mid-fan-out")
            _write_stub_record(
                settings_path,
                {"seat": "reviewer", "status": "completed", "result": "{}"})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(session_module, "run_sandboxed", fake_run)

        base = tmp_path / f"base{int(exc)}.json"
        try:
            session_module.run_sessions_parallel(
                [("reviewer", "one"), ("reviewer", "two")],
                repo, profile,
                factory_root=tmp_path, settings_path=base,
                max_turns=5, timeout_min=1, token="t",
                cli_path=Path("/bin/echo"), toolkit=resolved,
            )
        except RuntimeError:
            pass
        leftovers = sorted(
            p.name for p in base.parent.glob(base.name + "*")
            if p.name != base.name  # the caller's own base file is not ours
        )
        assert not [n for n in leftovers if "-toolkit" in n], leftovers
        assert not leftovers, f"fan-out artifacts survived: {leftovers}"

    run(exc=False)  # happy path: everything staged, everything gone
    run(exc=True)   # session 2 raises: staged toolkits still torn down


def test_review8_attempt_skills_union_across_the_fanout(tmp_path, monkeypatch):
    """F-22 × F-38 (c): the attempt's skills list carries every loaded
    item exactly once — three fan-out sessions (two reviewers + the
    verifier, all on toolkit_reviewer) each stamp their record, and the
    attempt's sorted set unions them (name@content-hash, no duplicates)."""
    from _support.f22 import _toolkit_with

    import factory.toolkit as toolkit_mod

    toolkit_root, skill, mcp = _toolkit_with(tmp_path / "tk")
    monkeypatch.setattr(toolkit_mod, "default_toolkit_dir",
                        lambda: toolkit_root)
    expected = sorted([
        f"{skill['name']}@{skill['content_hash']}",
        f"{mcp['name']}@{mcp['content_hash']}",
    ])

    env = Env(tmp_path)
    env.profile["docs_allowlist"] = ["docs.example.com"]
    env.profile["toolkit"] = {
        "skills": ["demo-skill"], "plugins": [], "mcp_servers": ["docs"],
    }
    import yaml

    (env.repo / ".factory" / "project.yaml").write_text(
        yaml.safe_dump(env.profile)
    )
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-m", "toolkit profile")

    seats = Fanout(
        reviews=_verdict(
            _finding("rename helper _do to _render", severity="minor"),
            declared="pass",
        ),
    )
    seats.stamp_toolkit_skills = True
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    attempt = env.task()["attempts"][-1]
    assert attempt["skills"] == expected  # once, from three stamping sessions
    toolkits = [tuple(sorted(i["name"] for i in kw["toolkit"]["items"]))
                for kw in seats.kwargs("reviewer") + seats.kwargs("verifier")
                if kw.get("toolkit")]
    assert toolkits == [("demo-skill", "docs")] * 3  # every session got it


def test_review9_reclaim_regex_sweeps_every_suffix(tmp_path: Path):
    """F-22 × F-38 (d): the F-26 stray sweep matches every suffix either
    PR creates — the base settings, -runner/-scratch/-toolkit (F-06/F-09/
    F-22) and the fan-out's per-session spellings with their own
    siblings — while a foreign project's and a non-run-id's files stay."""
    from factory.orchestrator import reclaim_runs
    from factory.state import TaskQueue

    run_id = "20261030T000000Z-abc123"
    base = f"demo-{run_id}.json"
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True)
    ours = [
        base,
        base + "-runner",
        base + "-scratch",
        base + "-toolkit",           # F-22
        base + "-r1.json",           # F-38 per-session settings
        base + "-r1.json-runner",
        base + "-r1.json-toolkit",
        base + "-r1-scratch",
        base + "-v.json-toolkit",
    ]
    for name in ours:
        target = sandbox / name
        if name.endswith(".json"):
            target.write_text("{}")
        else:
            target.mkdir()
    foreign = sandbox / "otherproj-20261030T000000Z-abc123.json"
    foreign.write_text("{}")
    not_a_run = sandbox / "demo-not-a-run-id.json"
    not_a_run.write_text("{}")

    queue_path = tmp_path / "tasks.json"
    queue_path.write_text(json.dumps(
        {"version": 1, "project": "demo", "tasks": []}
    ))
    reclaim_runs(TaskQueue(queue_path), "demo", tmp_path)

    for name in ours:
        assert not (sandbox / name).exists(), f"{name} was left behind"
    assert foreign.exists() and not_a_run.exists()
