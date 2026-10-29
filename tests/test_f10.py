"""F-10 acceptance tests — BUILD-ORDER.md, work order F-10.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f10.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the fixture project's git repo and a bare "GitHub" remote the
  orchestrator pushes to, the F-05 clone manager, the F-03 queue and run
  lease, F-07 L0 and F-08 L1 — and every project command they run goes
  through the real pinned ``srt`` sandbox (AC-11).
* **Stubbed:** the model. Each seat is a ``session_runner`` stub with
  ``run_session_sandboxed``'s signature: the "implementer" writes files into
  the run dir the way a session would, the "reviewer" returns verdict JSON.
  No API spend. ``gh pr create`` is replaced by an ``open_pr`` recorder,
  except in the tests of the real gh wrapper, which use a fake ``gh`` binary.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from factory import l0 as l0_module
from factory import l1 as l1_module
from factory.config import ConfigError, PeakWindow, in_peak_window, load_config
from factory.contract import ContractError, acceptance_hash
from factory.gates import CommandResult
from factory.l1 import L1Report
from factory.mergeorder import Protection
from factory.orchestrator import (
    PullRequest,
    reclaim_runs,
    run_loop,
    run_once,
)
from factory.prbody import (
    REQUIRED_SECTIONS,
    PRBodyError,
    blast_radius,
    build_evidence,
    check_pr_body,
    door,
    how_to_revert,
)
from factory.progress import ProgressLog, ProgressLogError
from factory.prompts import prompt_versions
from factory.publish import PushRefused, gh_pr_create, push_branch
from factory.runs import GIT_HARDENING, RunError, create_run
from factory.session import SessionRecord
from factory.state import TaskQueue

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)
REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = textwrap.dedent(
    """
    from impl import answer


    def test_ac_1():
        assert answer() == 42
    """
)
REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})
# F-26: the default protection fake — a repo whose default branch
# requires status checks with strict. The fixture remote is a local
# path, so the real gh seam could never answer for it.
PROTECTED_STRICT = Protection(checked=True, strict=True)


# ----------------------------------------------------------------- fixtures --


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", *, risk=(), ref: str = REF, **overrides) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42", "check": {"type": "test", "ref": ref}}
    ]
    task = {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": list(risk),
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-18T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 10},
        "attempts": [],
    }
    task.update(overrides)
    return task


class Env:
    """One fixture project: local repo + bare remote + factory root."""

    def __init__(self, tmp_path: Path, tasks: list | None = None, profile_extra=None):
        self.tmp = tmp_path
        self.root = tmp_path / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
        self.holdout = self.root / "holdouts" / "fixture" / "scenario.md"
        self.holdout.write_text("HOLDOUT-SECRET-CANARY\n")
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
        profile = {
            "name": "fixture",
            "repo": {
                "path": str(self.repo),
                "default_branch": "main",
                "remote": str(self.remote),
                "merge_method": "squash",
            },
            "commands": {
                "setup": "true",
                "test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
            },
            "paths": {
                "risk": {"payments": ["src/pay/**"]},
                "protected": [".factory/**"],
            },
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        profile.update(profile_extra or {})
        self.profile = profile
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        self.profile_path = factory_dir / "project.yaml"
        self.profile_path.write_text(yaml.safe_dump(profile))
        self.queue_path = factory_dir / "tasks.json"
        doc = {"version": 1, "project": "fixture", "tasks": tasks or [_task()]}
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)], check=True
        )
        self.prs: list[PullRequest] = []

    # The open_pr seam: record, never call gh.
    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def queue(self) -> TaskQueue:
        return TaskQueue(self.queue_path)

    def task(self, task_id: str = "T-1") -> dict:
        return self.queue().get(task_id)

    def remote_branches(self) -> set[str]:
        out = _git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads")
        return set(out.split())

    def runs_left(self) -> list[Path]:
        runs = self.root / "runs" / "fixture"
        return sorted(runs.iterdir()) if runs.exists() else []

    def sandbox_left(self) -> list[Path]:
        sandbox = self.root / "sandbox"
        return sorted(sandbox.iterdir()) if sandbox.exists() else []

    def run(self, session_runner, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", lambda remote, branch: PROTECTED_STRICT)
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )

    def loop(self, session_runner, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", lambda remote, branch: PROTECTED_STRICT)
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_loop(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )


class Seats:
    """Stub seats with ``run_session_sandboxed``'s signature.

    The implementer writes ``impl.py`` (returning ``answer``) plus the
    contract test into the run dir; the reviewer answers ``review``.
    """

    def __init__(self, answer=42, result="Implemented answer().", review=REVIEW_PASS,
                 extra_files=None, implementer=None):
        self.answer = answer
        self.result = result
        self.review = review
        self.extra_files = extra_files or {}
        self.implementer = implementer
        self.calls: list[tuple[str, str, Path, dict]] = []

    def prompts(self, seat: str) -> list[str]:
        return [prompt for s, prompt, _, _ in self.calls if s == seat]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        self.calls.append((seat, prompt, run_dir, kwargs))
        if seat == "implementer":
            if self.implementer is not None:
                return self.implementer(prompt, run_dir, kwargs)
            (run_dir / "impl.py").write_text(f"def answer():\n    return {self.answer}\n")
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            for rel, text in self.extra_files.items():
                target = run_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40}, result=self.result,
            )
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10}, result=self.review,
        )


def _section(body: str, name: str) -> str:
    """The text of one top-level ``## name`` section of a PR body."""
    match = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    assert match, f"section {name!r} missing from body:\n{body}"
    return match.group(1)


# --- AC-1: end to end, stub model makes a correct change → PR, in_review ----


def test_ac_1(tmp_path: Path):
    env = Env(tmp_path)
    seats = Seats()
    result = env.run(seats)

    assert result.outcome == "pr_opened", result
    assert len(env.prs) == 1
    pr = env.prs[0]
    task = env.task()
    assert task["status"] == "in_review"
    assert len(task["attempts"]) == 1
    attempt = task["attempts"][0]
    assert attempt["outcome"] == "pass"
    assert attempt["pr"] == result.pr == "https://github.com/fixture/fixture/pull/1"
    assert task["links"]["pr"] == result.pr
    assert task["links"]["branch"] == pr.head
    # The branch really reached the remote, carrying the implementer's change.
    assert f"refs/heads/{pr.head}" in env.remote_branches()
    shown = _git(env.remote, "show", f"refs/heads/{pr.head}:impl.py")
    assert "return 42" in shown
    assert pr.base == "main"
    # Each seat ran exactly once: implementer then reviewer.
    assert [c[0] for c in seats.calls] == ["implementer", "reviewer"]
    # The validator report is stored outside the run dirs, and every gate passed.
    report = json.loads(Path(attempt["validator_report"]).read_text())
    assert [report[k]["verdict"] for k in ("L0", "L1", "L2")] == ["pass", "pass", "pass"]


# --- AC-2: three failures → needs_human, attempts_exhausted, no PR ---------


def test_ac_2(tmp_path: Path):
    env = Env(tmp_path)
    seats = Seats(answer=41)  # wrong every time: the contract test stays red
    result = env.run(seats)

    assert result.outcome == "needs_human"
    task = env.task()
    assert task["status"] == "needs_human"
    assert len(task["attempts"]) == 3
    assert all(a["outcome"] != "pass" for a in task["attempts"])
    assert task["attempts"][-1]["escalation"] == "attempts_exhausted"
    assert env.prs == []
    assert env.remote_branches() == {"refs/heads/main"}  # nothing was pushed
    # Three implementer rounds, no reviewer (L0's test gate failed: the line stopped).
    assert [c[0] for c in seats.calls] == ["implementer"] * 3
    # Findings went back: round 2's prompt carries round 1's numbered L0 finding.
    assert "1. [L0 " in seats.prompts("implementer")[1]
    assert "tests/test_feature.py" in seats.prompts("implementer")[1]


def test_session_failure_counts_as_a_failed_attempt(tmp_path: Path):
    env = Env(tmp_path)

    def broken(prompt, run_dir, kwargs):
        return SessionRecord(seat="implementer", status="failed",
                             reason="error_during_execution")

    result = env.run(Seats(implementer=broken))
    task = env.task()
    assert result.outcome == "needs_human"
    assert len(task["attempts"]) == 3
    assert task["attempts"][-1]["escalation"] == "attempts_exhausted"
    assert env.prs == []


def test_budget_counts_earlier_attempts(tmp_path: Path):
    """A re-queued task runs under the same attempt budget (TASK-CONTRACT §3)."""
    earlier = {
        "run_id": "old", "slice_id": "T-1", "started": "2026-10-17T10:00:00Z",
        "outcome": "fail", "prompt_versions": {"implementer": "x"}, "skills": [],
    }
    env = Env(tmp_path, tasks=[_task(attempts=[dict(earlier), dict(earlier, run_id="old2")])])
    seats = Seats(answer=41)
    env.run(seats)
    task = env.task()
    assert len(task["attempts"]) == 3  # one more round, then exhausted
    assert task["attempts"][-1]["escalation"] == "attempts_exhausted"
    assert len(seats.prompts("implementer")) == 1


def test_reviewer_fail_is_retried_then_exhausted(tmp_path: Path):
    """A reviewer FAIL goes back with numbered findings until the budget is
    spent. The implementer writes different (still green) content each
    round so the review delta is real, and the findings name impl.py —
    the common file-named-retry path, in the delta, exercised as such
    (an identical round-2 finding on the untouched file would rightly
    escalate review_not_converging instead: F-23)."""
    env = Env(tmp_path)
    seats = Seats()

    def varying(seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            seats.calls.append((seat, prompt, Path(run_dir), kwargs))
            round_no = sum(1 for s, *_ in seats.calls if s == "implementer")
            (Path(run_dir) / "impl.py").write_text(
                f"def answer():\n    return 42  # fix round {round_no}\n")
            (Path(run_dir) / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                result="Implemented answer().",
            )
        seats.calls.append((seat, prompt, Path(run_dir), kwargs))
        call_no = sum(1 for s, *_ in seats.calls if s == "reviewer")
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            result=json.dumps({"verdict": "fail", "findings": [
                {"id": "R1", "axis": "spec", "severity": "blocking",
                 "file": "impl.py", "text": f"scope creep {call_no}"}]}),
        )

    result = env.run(varying)
    assert result.outcome == "needs_human"
    assert [c[0] for c in seats.calls] == ["implementer", "reviewer"] * 3
    assert "scope creep 1" in seats.prompts("implementer")[1]
    assert env.task()["attempts"][-1]["escalation"] == "attempts_exhausted"
    assert env.prs == []


def test_l0_escalation_stops_immediately(tmp_path: Path):
    """Protected path, new dependency, risk path: no retries (VALIDATOR-SPEC)."""
    env = Env(tmp_path)
    seats = Seats(extra_files={"src/pay/charge.py": "AMOUNT = 1\n"})
    result = env.run(seats)
    task = env.task()
    assert result.outcome == "needs_human"
    assert len(task["attempts"]) == 1
    assert task["attempts"][0]["escalation"] == "risk_path_touched"
    assert "payments" in task["risk"]  # the dynamic flag is written onto the task
    assert task["gates"]["merge"] == "human"
    assert env.prs == []


# --- AC-3: quota/429 stops the loop, marks quota_exhausted, no retry -------


def test_ac_3(tmp_path: Path):
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])

    def quota(prompt, run_dir, kwargs):
        return SessionRecord(seat="implementer", status="failed", is_error=True,
                             reason="api_error_429", subtype="success")

    seats = Seats(implementer=quota)
    result = env.loop(seats)

    assert result.stop_reason == "quota_exhausted"
    assert len(seats.calls) == 1  # no retry, and T-2 never started
    t1, t2 = env.task("T-1"), env.task("T-2")
    assert len(t1["attempts"]) == 1
    assert t1["attempts"][0]["escalation"] == "quota_exhausted"
    assert t1["attempts"][0]["outcome"] == "aborted"
    assert t1["status"] == "ready"  # quota is not the task's fault: it re-queues
    assert t2["status"] == "ready" and t2["attempts"] == []
    assert env.prs == []


@pytest.mark.parametrize(
    "record",
    [
        SessionRecord(seat="reviewer", status="failed", reason="api_error_429"),
        SessionRecord(seat="reviewer", status="failed",
                      reason="ProcessError: API Error: 429 rate limit exceeded"),
    ],
)
def test_reviewer_quota_also_stops(tmp_path: Path, record):
    env = Env(tmp_path)
    seats = Seats()
    base = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if seat == "reviewer":
            seats.calls.append((seat, prompt, Path(run_dir), kwargs))
            return record
        return base(seat, prompt, run_dir, profile, **kwargs)

    result = env.loop(runner)
    assert result.stop_reason == "quota_exhausted"
    assert env.task()["attempts"][-1]["escalation"] == "quota_exhausted"
    assert env.prs == []


# --- AC-4: no session during the peak window; the window is config ---------


def test_ac_4(tmp_path: Path):
    monday_0930 = datetime(2026, 10, 19, 9, 30, tzinfo=STOCKHOLM)
    env = Env(tmp_path)
    seats = Seats()
    result = env.loop(seats, now=lambda: monday_0930)
    assert result.stop_reason == "peak_window"
    assert seats.calls == []
    assert env.task()["status"] == "ready"

    # The window comes from config: move it, and the same instant is allowed.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"peak_window": {
        "timezone": "Europe/Stockholm", "days": ["sat"], "start": "13:00", "end": "14:00"}}))
    config = load_config(config_path)
    result = env.loop(seats, now=lambda: monday_0930, config=config)
    assert result.stop_reason == "queue_empty"
    assert env.task()["status"] == "in_review"


def test_peak_window_boundaries_and_dst():
    window = load_config(None).peak_window  # the default: 08-12 Mon-Fri Stockholm
    assert window == PeakWindow("Europe/Stockholm", ("mon", "tue", "wed", "thu", "fri"),
                                "08:00", "12:00")
    at = lambda *a: datetime(*a, tzinfo=STOCKHOLM)  # noqa: E731
    assert in_peak_window(at(2026, 10, 19, 8, 0), window)
    assert in_peak_window(at(2026, 10, 19, 11, 59), window)
    assert not in_peak_window(at(2026, 10, 19, 7, 59), window)
    assert not in_peak_window(at(2026, 10, 19, 12, 0), window)
    assert not in_peak_window(at(2026, 10, 18, 9, 0), window)  # Sunday
    # Swedish time, whatever the caller's zone: 07:30 UTC is 09:30 CEST
    # in summer (in the window) but 08:30 CET after the switch (still in),
    # and 06:30 UTC is 08:30 CEST but 07:30 CET (out) — DST is honoured.
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    assert in_peak_window(utc(2026, 10, 23, 6, 30), window)       # Fri, CEST
    assert not in_peak_window(utc(2026, 10, 26, 6, 30), window)   # Mon, CET
    with pytest.raises(ConfigError):
        in_peak_window(datetime(2026, 10, 19, 9, 0), window)  # naive: refuse


def test_bad_config_is_refused(tmp_path: Path):
    for bad in (
        {"peak_window": {"timezone": "Mars/Base", "days": ["mon"], "start": "08:00",
                         "end": "12:00"}},
        {"peak_window": {"timezone": "Europe/Stockholm", "days": ["funday"],
                         "start": "08:00", "end": "12:00"}},
        {"peak_window": {"timezone": "Europe/Stockholm", "days": ["mon"],
                         "start": "8am", "end": "12:00"}},
        {"peak_window": {"timezone": "Europe/Stockholm", "days": ["mon"],
                         "start": "12:00", "end": "08:00"}},
        {"unknown_key": 1},
    ):
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.safe_dump(bad))
        with pytest.raises(ConfigError):
            load_config(path)
    # a binary config (UnicodeDecodeError, a ValueError) refuses the
    # same way — never a raw traceback out of the CLI (PR #63 review
    # round: the F-12 R-2 class at this read).
    binary = tmp_path / "bin.yaml"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError):
        load_config(binary)


def test_peak_window_reached_mid_run_stops_before_next_session(tmp_path: Path):
    env = Env(tmp_path)
    clock = iter([OFF_PEAK, OFF_PEAK] + [datetime(2026, 10, 19, 9, 0, tzinfo=STOCKHOLM)] * 50)
    seats = Seats(answer=41)
    result = env.run(seats, now=lambda: next(clock))
    assert result.outcome == "peak_window"
    assert len(seats.prompts("implementer")) == 1
    task = env.task()
    assert task["status"] == "ready" and len(task["attempts"]) == 1
    assert env.runs_left() == []


# --- AC-5: no `gh pr merge` anywhere in the code (grep test) ----------------


def test_ac_5():
    patterns = [
        re.compile(r"gh\s+pr\s+merge"),
        re.compile(r"""["']pr["']\s*,\s*["']merge["']"""),
        re.compile(r"pulls/[^\s\"']*/merge"),
    ]
    # A fourth pattern, ["'merge"' followed by a flagged arg, was removed
    # for F-26: the stale-base sweep runs a LOCAL `git merge --no-edit`
    # on a scratch clone to validate what a merge would produce — the
    # simulation this AC exists to allow. The three patterns above still
    # catch every real PR-merge mechanism (shell form, argv form, REST
    # endpoint); merging a PR remains the owner's act.
    offenders = []
    files = sorted((REPO_ROOT / "factory").rglob("*.py")) + sorted(
        (REPO_ROOT / "prompts").rglob("*.md")
    )
    assert files
    for path in files:
        text = path.read_text()
        for pattern in patterns:
            for match in pattern.finditer(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)!r}")
    assert offenders == [], "the factory never merges a PR:\n" + "\n".join(offenders)


# --- AC-6: every attempt records prompt_versions and skills ----------------


def test_ac_6(tmp_path: Path):
    env = Env(tmp_path)
    env.run(Seats(answer=41))  # three attempts, every outcome path
    env2 = Env(tmp_path / "pass")
    env2.run(Seats())
    profile = yaml.safe_load(env.profile_path.read_text())
    attempts = env.task()["attempts"] + env2.task()["attempts"]
    assert len(attempts) == 4
    for attempt in attempts:
        assert attempt["prompt_versions"] == prompt_versions(profile)
        assert set(attempt["prompt_versions"]) == {"implementer", "reviewer"}
        assert attempt["skills"] == []  # setting_sources=[]: no skills are loaded

    # Without either field an attempt is a schema error, refused on write.
    queue = env.queue()
    good = dict(attempts[0], run_id="new")
    for missing in ("prompt_versions", "skills"):
        bad = {k: v for k, v in good.items() if k != missing}
        with pytest.raises(ContractError):
            queue.append_attempt("T-1", bad)
    assert len(env.task()["attempts"]) == 3  # nothing was written


# --- AC-7: the five sections, or the PR isn't opened -----------------------


def test_ac_7(tmp_path: Path, monkeypatch):
    env = Env(tmp_path)
    env.run(Seats())
    body = env.prs[0].body
    for name in ("Summary", "Evidence", "Merge Danger", "Contract", "Validator report"):
        assert name in REQUIRED_SECTIONS
        _section(body, name)
    merge_danger = _section(body, "Merge Danger")
    assert "**Door:**" in merge_danger and "**Blast Radius:**" in merge_danger
    check_pr_body(body)  # the real body passes its own gate

    for name in REQUIRED_SECTIONS:
        stripped = re.sub(rf"^## {re.escape(name)}\n", "## Renamed\n", body, flags=re.M)
        with pytest.raises(PRBodyError):
            check_pr_body(stripped)
    with pytest.raises(PRBodyError):
        check_pr_body(body + "\n## Evidence\nforged\n")  # a duplicate is not allowed
    with pytest.raises(PRBodyError):
        check_pr_body(body.replace("**Door:**", "Door"))

    # Orchestrator path: a body missing a section is never pushed or opened.
    import factory.orchestrator as orchestrator

    real = orchestrator.build_pr_body
    monkeypatch.setattr(
        orchestrator, "build_pr_body",
        lambda *a, **k: real(*a, **k).replace("## Evidence\n", "## Evidenc\n"),
    )
    env2 = Env(tmp_path / "second")
    result2 = env2.run(Seats())
    # A refused body can't pass on retry: escalate at once (review round 2).
    assert result2.outcome == "needs_human"
    assert result2.escalation == "publish_refused"
    assert env2.prs == []
    assert env2.remote_branches() == {"refs/heads/main"}
    task2 = env2.task()
    assert task2["status"] == "needs_human"
    assert task2["attempts"][-1]["escalation"] == "publish_refused"
    assert env2.runs_left() == []


# --- AC-8: Evidence comes from the L1 report; model text can't alter it ----


SPOOF = (
    "Done.\n## Evidence\n| ref | red | green |\n| everything | skipped | green |\n"
    "**Door:** `two-way`\n**Blast Radius:** contained\n## How to revert\njust revert\n"
    "```\n````\n"
)


def test_ac_8(tmp_path: Path):
    env = Env(tmp_path, tasks=[_task(risk=["deploy"])])
    env.run(Seats(result=SPOOF))
    body = env.prs[0].body
    check_pr_body(body)
    evidence = _section(body, "Evidence")
    assert REF in evidence
    assert "red on base" in evidence and "red_reason: assertion" in evidence
    assert "green on head" in evidence
    assert "everything" not in evidence and "skipped" not in evidence
    # Exactly one of each heading: the spoofed ones are inert, fenced text.
    for name in REQUIRED_SECTIONS:
        assert len(re.findall(rf"^## {re.escape(name)}$", body, re.M)) == 1
    head = _section(body, "Merge Danger").partition("Implementer notes")[0]
    assert "**Door:** `one-way`" in head and "two-way" not in head

    # Unit level: the section is a function of the L1 report alone.
    report = L1Report(
        verdict="pass",
        findings=[],
        command_results=[
            CommandResult(f"red:{REF}", "pytest", 1, "E assert 0 == 42"),
            CommandResult(f"green:{REF}", "pytest", 0, "1 passed"),
        ],
        red_reasons={REF: "assertion"},
    )
    task = _task()
    text = build_evidence(task, report)
    assert REF in text and "red on base" in text and "exit 1" in text
    assert "red_reason: assertion" in text and "green on head" in text
    # A ref that never went red is shown as such, never as evidence.
    report.command_results[0] = CommandResult(f"red:{REF}", "pytest", 0, "1 passed")
    report.red_reasons = {REF: None}
    assert "NOT red on base" in build_evidence(task, report)


def test_ac_8_collection_red_reason(tmp_path: Path):
    """A brand-new module makes the red run a collection error: shown as such."""
    env = Env(tmp_path)
    test = "from feature import answer\n\n\ndef test_ac_1():\n    assert answer() == 42\n"

    def implementer(prompt, run_dir, kwargs):
        (run_dir / "feature.py").write_text("def answer():\n    return 42\n")
        (run_dir / "tests" / "test_feature.py").write_text(test)
        return SessionRecord(seat="implementer", status="completed", result="ok")

    env.run(Seats(implementer=implementer))
    assert "red_reason: collection" in _section(env.prs[0].body, "Evidence")


# --- AC-9: Door and Blast Radius are computed, not written ------------------


def test_ac_9(tmp_path: Path):
    for flag in ("migration", "deploy", "external_service", "payments", "privacy"):
        assert door({"risk": [flag]}) == "one-way"
    for flags in ([], ["auth"], ["rls"], ["safety"], ["new_dependency"], ["factory_control"]):
        assert door({"risk": flags}) == "two-way"
    assert door({"risk": []}, extra_flags=["payments"]) == "one-way"

    profile = {"paths": {"risk": {"payments": ["src/pay/**"], "auth": ["src/auth/*"]}}}
    assert blast_radius({"risk": []}, profile, ["impl.py"]) == "contained"
    radius = blast_radius({"risk": ["deploy"]}, profile, ["src/pay/x.py", "impl.py"])
    assert "`deploy`" in radius and "`payments`" in radius
    assert "`src/pay/**`" in radius and "src/pay/x.py" in radius
    assert "contained" not in radius

    # In the PR: computed values first; the implementer's prose only below them.
    env = Env(tmp_path, tasks=[_task(risk=["external_service"])])
    env.run(Seats(result="**Door:** `two-way`\n**Blast Radius:** contained"))
    merge_danger = _section(env.prs[0].body, "Merge Danger")
    head, _, notes = merge_danger.partition("Implementer notes")
    assert "**Door:** `one-way`" in head
    assert "`external_service`" in head and "contained" not in head
    assert "two-way" not in head
    assert "two-way" in notes  # kept, but inert and below the values

    env2 = Env(tmp_path / "two")
    env2.run(Seats())
    head2 = _section(env2.prs[0].body, "Merge Danger").partition("Implementer notes")[0]
    assert "**Door:** `two-way`" in head2 and "**Blast Radius:** contained" in head2


# --- AC-10: fresh sessions get the progress log; the log is append-only -----


def test_ac_10(tmp_path: Path):
    env = Env(tmp_path)
    log = ProgressLog.for_task(env.root, "fixture", "T-1")
    log.append({"run_id": "r1", "outcome": "fail", "findings": ["1. FIRST-ENTRY-MARK"]})
    log.append({"run_id": "r2", "outcome": "aborted", "findings": ["1. SECOND-ENTRY-MARK"]})

    seats = Seats()
    env.run(seats)
    prompt = seats.prompts("implementer")[0]
    first, second = prompt.find("FIRST-ENTRY-MARK"), prompt.find("SECOND-ENTRY-MARK")
    assert 0 <= first < second, prompt
    assert "entry 1" in prompt and "entry 2" in prompt
    # The run appended its own entry (3), and the log lives outside any clone.
    entries = log.entries()
    assert [e["n"] for e in entries] == [1, 2, 3]
    assert entries[2]["outcome"] == "pass"
    assert env.root / "runs" not in log.path.parents
    assert env.repo not in log.path.parents

    # Rewriting an earlier entry raises — on read and on the next append.
    lines = log.path.read_text().splitlines()
    lines[0] = lines[0].replace("FIRST-ENTRY-MARK", "REWRITTEN")
    log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ProgressLogError):
        log.entries()
    with pytest.raises(ProgressLogError):
        log.append({"run_id": "r4", "outcome": "fail"})
    # So does dropping one.
    log.path.write_text("\n".join(log.path.read_text().splitlines()[1:]) + "\n")
    with pytest.raises(ProgressLogError):
        log.entries()


def test_progress_log_reaches_every_round(tmp_path: Path):
    env = Env(tmp_path)
    seats = Seats(answer=41)
    env.run(seats)
    prompts = seats.prompts("implementer")
    assert "entry 1" not in prompts[0]
    assert "entry 1" in prompts[1] and "entry 2" not in prompts[1]
    assert "entry 1" in prompts[2] and "entry 2" in prompts[2]


# --- AC-11: L0 and L1 run project commands inside the sandbox ---------------


def _probe_conftest(outside: Path, holdout: Path) -> str:
    return textwrap.dedent(
        f"""
        import pathlib
        RESULTS = []
        try:
            pathlib.Path({str(outside)!r}).write_text("pwned")
            RESULTS.append("WRITE-ALLOWED")
        except OSError:
            RESULTS.append("WRITE-DENIED")
        try:
            RESULTS.append("READ-ALLOWED:" + pathlib.Path({str(holdout)!r}).read_text())
        except OSError:
            RESULTS.append("READ-DENIED")


        def pytest_terminal_summary(terminalreporter):
            terminalreporter.write_line("PROBE " + " ".join(RESULTS))
        """
    )


def test_ac_11(tmp_path: Path, monkeypatch):
    env = Env(tmp_path)
    outside = tmp_path / "outside" / "pwned.txt"
    outside.parent.mkdir()
    conftest = _probe_conftest(outside, env.holdout)

    # Control: the probe is real — run unsandboxed, it writes and reads.
    control = tmp_path / "control"
    (control / "tests").mkdir(parents=True)
    (control / "tests" / "conftest.py").write_text(conftest)
    (control / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          cwd=control, capture_output=True, text=True)
    assert "PROBE WRITE-ALLOWED READ-ALLOWED:HOLDOUT-SECRET-CANARY" in proc.stdout
    outside.unlink()

    # Any unsandboxed project command during the run is a test failure.
    def forbidden(*args, **kwargs):
        raise AssertionError(f"project command ran outside the sandbox: {args}")

    monkeypatch.setattr(l0_module, "_run_command", forbidden)
    monkeypatch.setattr(l1_module, "run_command", forbidden)

    seats = Seats(extra_files={"tests/conftest.py": conftest})
    result = env.run(seats)
    assert result.outcome == "pr_opened", result

    commands = {c["name"]: c["output"] for c in result.reports["L0"]["commands"]}
    commands.update({c["name"]: c["output"] for c in result.reports["L1"]["commands"]})
    gated = ["test", f"red:{REF}", f"green:{REF}"]
    for name in gated:
        assert "PROBE WRITE-DENIED READ-DENIED" in commands[name], (name, commands[name])
    assert all("HOLDOUT-SECRET-CANARY" not in out for out in commands.values())
    assert not outside.exists()


# --- F-31 run blocker: gate commands run on a run-local venv ----------------


def test_gate_runner_uses_a_run_local_venv_not_ambient_pip(
        tmp_path: Path, monkeypatch):
    """Run 20261029T145356Z-b1875d: L0's setup resolved the OWNER's
    terminal venv (its pip "satisfied" requirements from the main
    checkout's site-packages and tried to uninstall the owner's
    ``bin/factory`` until the sandbox blocked it), setup failed, every
    remaining gate was skipped. The gate runner must instead create a
    venv inside the run clone and run every command with its ``bin/``
    first on PATH plus VIRTUAL_ENV — a decoy venv planted first on the
    ambient PATH is then never resolved, and ``pip``/``python`` land in
    the run's venv. The venv is git-excluded, so the clean-tree gate
    still sees a clean checkout."""
    import factory.orchestrator as orch
    from factory.sandbox import build_settings, write_settings

    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# fixture\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=f@x", "-c", "user.name=f",
         "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    # A decoy venv sitting FIRST on the ambient PATH, outside $HOME so
    # the home-entry filter would keep it: its pip/python record the hit
    # and fail — proof the gate commands never resolved them.
    decoy = tmp_path / "decoy-venv" / "bin"
    decoy.mkdir(parents=True)
    marker = tmp_path / "decoy-hit"
    for name in ("pip", "python", "python3"):
        (decoy / name).write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        (decoy / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{decoy}:{os.environ.get('PATH', '')}")

    root = tmp_path / "factory-root"
    (root / "holdouts").mkdir(parents=True)
    run = create_run("fixture", repo, factory_root=root)
    settings = build_settings(
        {"docs_allowlist": [], "network_allowlist": []},
        run.root, model_endpoint="api.z.ai",
    )
    write_settings(settings, run.settings_path)

    runner = orch.sandboxed_runner(run.root, run.settings_path)
    result = runner(
        "probe",
        'pip --version && python -c "import sys; print(\'prefix:\' + sys.prefix)"',
        run.root, 120,
    )
    assert result.exit_code == 0, result.output
    assert not marker.exists(), "a decoy venv binary executed — the gate resolved an ambient venv"
    assert f"{run.root}/.venv" in result.output, result.output  # pip + prefix
    assert (run.root / ".venv" / "pyvenv.cfg").is_file()
    # the venv is excluded, not invisible to the gate's clean-tree check
    status = subprocess.run(
        ["git", "-C", str(run.root), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    assert status.stdout.strip() == "", status.stdout


def test_committed_venv_is_refused_and_flagged(tmp_path: Path):
    """Review round on #74: an implementer can ``git add -f .venv/…``; the
    clean clone would carry it, ``python -m venv`` keeps existing
    site-packages, and the gates would run planted code. Two layers: L0
    flags any tracked path under ``.venv/`` as protected_path_touched
    (factory infrastructure inside the clone is never agent-writable),
    and the gate venv creation refuses outright when ``<run>/.venv``
    already exists — before a single gate command can run through it."""
    import factory.orchestrator as orch
    from factory.runs import RunError

    planted = ".venv/lib/python3.11/site-packages/evil.pth"
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# fixture\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=f@x", "-c", "user.name=f",
         "commit", "-q", "-m", "base"], check=True)
    target = repo / planted
    target.parent.mkdir(parents=True)
    target.write_text("import evil\n")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".venv"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=f@x", "-c", "user.name=f",
         "commit", "-q", "-m", "plant"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"],
                          capture_output=True, text=True, check=True).stdout.strip()

    # L0 layer: the planted path is a protected-path finding + escalation
    from factory.l0 import run_l0
    report = run_l0({"commands": {"setup": "true"}, "paths": {}},
                    repo, base, "HEAD")
    assert "protected_path_touched" in report.escalations, report.escalations
    assert any(f.file == planted for f in report.findings), \
        [f.id for f in report.findings]

    # runner layer: the venv creation refuses, so no command ever runs
    root = tmp_path / "factory-root"
    (root / "holdouts").mkdir(parents=True)
    run = create_run("fixture", repo, factory_root=root, ref="HEAD")
    assert (run.root / ".venv").is_dir(), "fixture setup error: no planted venv"
    with pytest.raises(RunError, match="already exists"):
        orch.sandboxed_runner(run.root, run.settings_path)


def test_gate_venv_exclude_line_is_written_once(tmp_path: Path):
    """Review round on #74: the exclude anchor is appended idempotently —
    a recreated venv (the only way creation runs twice: an existing
    ``.venv`` refuses) must not stack duplicate ``/.venv/`` lines in
    ``.git/info/exclude``."""
    import shutil as _shutil

    import factory.orchestrator as orch

    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# fixture\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=f@x", "-c", "user.name=f",
         "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    root = tmp_path / "factory-root"
    (root / "holdouts").mkdir(parents=True)
    run = create_run("fixture", repo, factory_root=root)

    orch._create_gate_venv(run.root)
    _shutil.rmtree(run.root / ".venv")  # the one way creation can re-run
    orch._create_gate_venv(run.root)
    exclude = (run.root / ".git" / "info" / "exclude").read_text()
    assert exclude.count("/.venv/\n") == 1, exclude


# --- AC-12: every run dir is cleaned up; loop reclaims orphans --------------


def test_ac_12(tmp_path: Path):
    # PR opened, needs_human, quota stop, exception: nothing is left behind.
    def quota(prompt, run_dir, kwargs):
        return SessionRecord(seat="implementer", status="failed", reason="api_error_429")

    def crash(prompt, run_dir, kwargs):
        # BaseException: an ordinary Exception is now caught, recorded as an
        # orchestrator_error attempt and returned (review finding 2) — the
        # exception-path cleanup proof uses the class that still escapes.
        raise KeyboardInterrupt("orchestrator-side bug")

    for name, seats, expected in (
        ("pr", Seats(), "pr_opened"),
        ("human", Seats(answer=41), "needs_human"),
        ("quota", Seats(implementer=quota), "quota_exhausted"),
    ):
        env = Env(tmp_path / name)
        assert env.run(seats).outcome == expected
        assert env.runs_left() == [], name
        assert env.sandbox_left() == [], name

    env = Env(tmp_path / "crash")
    with pytest.raises(KeyboardInterrupt, match="orchestrator-side bug"):
        env.run(Seats(implementer=crash))
    assert env.runs_left() == [] and env.sandbox_left() == []
    assert env.task()["status"] == "ready"

    # loop reclaims run dirs no live lease holds — and never a live run's.
    env = Env(tmp_path / "orphans")
    orphan = create_run("fixture", env.repo, factory_root=env.root)
    orphan.settings_path.parent.mkdir(parents=True, exist_ok=True)
    orphan.settings_path.write_text("{}")
    runner_dir = orphan.settings_path.with_name(orphan.settings_path.name + "-runner")
    runner_dir.mkdir()
    queue = env.queue()
    with queue.begin_run("T-1"):  # a live run holds the lease
        assert reclaim_runs(env.queue(), "fixture", env.root) == []
        assert orphan.root.exists()
    # The holder above "died" in_progress; the lease is free now.
    result = env.loop(Seats())
    assert not orphan.root.exists()
    assert not orphan.settings_path.exists() and not runner_dir.exists()
    assert result.stop_reason == "queue_empty"
    assert env.task()["status"] == "in_review"  # reclaimed to ready, then run
    assert env.runs_left() == [] and env.sandbox_left() == []


def test_ac_12_killed_run_is_cleaned_by_next_loop(tmp_path: Path):
    env = Env(tmp_path)
    marker = tmp_path / "session-started"
    script = tmp_path / "killed_run.py"
    script.write_text(textwrap.dedent(
        f"""
        import os, signal, sys
        from datetime import datetime
        from pathlib import Path
        from zoneinfo import ZoneInfo
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from factory.mergeorder import Protection
        from factory.orchestrator import run_once

        # Fixed off-peak clock: on the real clock a weekday morning falls in the
        # default peak window, so no session would start and nothing gets killed.
        OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=ZoneInfo("Europe/Stockholm"))

        def seat(seat, prompt, run_dir, profile, **kwargs):
            Path({str(marker)!r}).write_text(str(run_dir))
            os.kill(os.getpid(), signal.SIGKILL)  # killed mid-session

        run_once(Path({str(env.queue_path)!r}), factory_root=Path({str(env.root)!r}),
                 session_runner=seat, open_pr=lambda pr: "never", now=lambda: OFF_PEAK,
                 protection=lambda r, b: Protection(checked=True, strict=True),
                 pr_state=lambda u: "open")
        """
    ))
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    killed_dir = Path(marker.read_text())
    assert killed_dir.exists() and killed_dir in env.runs_left()
    assert env.task()["status"] == "in_progress"

    result = env.loop(Seats())
    assert not killed_dir.exists()
    assert env.runs_left() == [] and env.sandbox_left() == []
    assert result.stop_reason == "queue_empty"
    assert env.task()["status"] == "in_review"


# --- AC-13: never push the default branch; every git call is hardened ------


def test_ac_13(tmp_path: Path, monkeypatch):
    env = Env(tmp_path)
    work = create_run("fixture", env.repo, factory_root=env.root, ref="main")
    _git(work.root, "checkout", "-q", "-b", "factory/t-1-x")
    _git(work.root, "-c", "user.name=t", "-c", "user.email=t@x", "commit",
         "--allow-empty", "-q", "-m", "x")

    # Every child process — subprocess.run included — is spawned through Popen.
    spawned: list = []
    real_popen = subprocess.Popen

    class RecordPopen(real_popen):
        def __init__(self, *args, **kwargs):
            spawned.append(args[0] if args else kwargs.get("args"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", RecordPopen)

    refusals = [
        ("main", {"main", "factory/t-1-x"}),               # the default branch
        ("factory/other", {"factory/t-1-x"}),              # a ref the run didn't create
        ("refs/heads/main", {"refs/heads/main"}),          # spelled as a full ref
        ("factory/t-1-x:refs/heads/main", {"factory/t-1-x:refs/heads/main"}),  # refspec
        ("+factory/t-1-x", {"+factory/t-1-x"}),            # force
        ("HEAD", {"HEAD"}),
        ("feature/x", {"feature/x"}),                      # outside the factory namespace
    ]
    for branch, created in refusals:
        with pytest.raises(PushRefused):
            push_branch(work.root, str(env.remote), branch, sha="0" * 40,
                        created=created, default_branch="main")
    assert spawned == [], "a refused push must not start any git process"

    main_before = _git(env.remote, "rev-parse", "refs/heads/main")
    head = _git(work.root, "rev-parse", "HEAD")
    push_branch(work.root, str(env.remote), "factory/t-1-x", sha=head,
                created={"factory/t-1-x"}, default_branch="main")
    (push,) = [argv for argv in spawned if "push" in argv]
    assert list(push[1:5]) == list(GIT_HARDENING)
    assert push[-1] == f"{head}:refs/heads/factory/t-1-x"  # the object, not the branch
    assert not any(a.startswith("+") or a in ("--force", "-f", "--mirror", "--all",
                                               "--tags", "--follow-tags") for a in push)
    assert env.remote_branches() == {"refs/heads/main", "refs/heads/factory/t-1-x"}
    assert _git(env.remote, "rev-parse", "refs/heads/main") == main_before

    # A whole run: every orchestrator-side git process carries GIT_HARDENING.
    env2 = Env(tmp_path / "full")
    spawned.clear()  # only the run's own processes from here on
    assert env2.run(Seats()).outcome == "pr_opened"
    gits = [list(argv) for argv in spawned
            if isinstance(argv, (list, tuple)) and Path(str(argv[0])).name == "git"]
    assert gits, "expected git calls during a run"
    unhardened = [argv for argv in gits if argv[1:5] != list(GIT_HARDENING)]
    assert unhardened == []
    pushes = [argv for argv in gits if "push" in argv]
    assert len(pushes) == 1
    refspec = pushes[0][-1]
    src, dst = refspec.split(":", 1)
    assert dst == f"refs/heads/{env2.prs[0].head}"
    assert re.fullmatch(r"[0-9a-f]{40}", src)  # the object, not the branch
    assert _git(env2.remote, "rev-parse", dst) == src  # remote holds exactly it
    assert env2.prs[0].head != "main"


def test_agent_moving_the_default_branch_in_its_clone_is_never_pushed(tmp_path: Path):
    """The agent owns its clone's refs; the orchestrator pushes only its branch."""
    env = Env(tmp_path)
    seats = Seats()
    base = seats.__call__

    def implementer(seat, prompt, run_dir, profile, **kwargs):
        record = base(seat, prompt, run_dir, profile, **kwargs)
        if seat == "implementer":
            _git(run_dir, "-c", "user.name=a", "-c", "user.email=a@x", "commit",
                 "-qam", "agent commit")
            _git(run_dir, "branch", "-f", "main", "HEAD")
        return record

    main_before = _git(env.remote, "rev-parse", "refs/heads/main")
    assert env.run(implementer).outcome == "pr_opened"
    assert _git(env.remote, "rev-parse", "refs/heads/main") == main_before
    assert len(env.remote_branches()) == 2


def test_gh_pr_create_wrapper(tmp_path: Path):
    """The real gh seam: `gh pr create` with the body from a file, nothing else."""
    log = tmp_path / "gh-args"
    fake = tmp_path / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        f"for a in \"$@\"; do printf '%s\\n' \"$a\" >> {shlex.quote(str(log))}; done\n"
        "echo https://github.com/AgenticTony/x/pull/7\n"
    )
    fake.chmod(0o755)
    pr = PullRequest(remote="https://github.com/AgenticTony/x.git", base="main",
                     head="factory/t-1-x", title="T-1: thing", body="## Summary\nbody\n")
    url = gh_pr_create(pr, gh_bin=fake)
    assert url == "https://github.com/AgenticTony/x/pull/7"
    args = log.read_text().splitlines()
    assert args[:2] == ["pr", "create"]
    assert args[args.index("--repo") + 1] == "AgenticTony/x"
    assert args[args.index("--base") + 1] == "main"
    assert args[args.index("--head") + 1] == "factory/t-1-x"
    assert "--body-file" in args
    with pytest.raises(PushRefused):
        gh_pr_create(PullRequest(remote="https://github.com/a/b", base="main", head="main",
                                 title="t", body="b"), gh_bin=fake)


# --- AC-14: How to revert, built by the orchestrator ------------------------


def test_ac_14(tmp_path: Path):
    two = how_to_revert({"risk": []}, {"repo": {"merge_method": "squash"}})
    assert "revert the squash-merge commit" in two.lower()
    one = how_to_revert({"risk": ["deploy", "privacy"]}, {"repo": {"merge_method": "squash"}})
    assert "does not undo" in one
    assert "`deploy`" in one and "`privacy`" in one
    one_extra = how_to_revert({"risk": []}, {}, extra_flags=["payments"])
    assert "`payments`" in one_extra

    env = Env(tmp_path, tasks=[_task(risk=["migration"])])
    env.run(Seats(result="## How to revert\nnothing to undo, all two-way"))
    section = _section(env.prs[0].body, "How to revert")
    assert "does not undo" in section and "`migration`" in section
    assert "nothing to undo" not in section

    env2 = Env(tmp_path / "two")
    env2.run(Seats())
    assert "squash-merge commit" in _section(env2.prs[0].body, "How to revert")

    body = env2.prs[0].body
    with pytest.raises(PRBodyError):
        check_pr_body(re.sub(r"^## How to revert\n", "", body, flags=re.M))


def test_default_seat_runner_signature_matches(tmp_path: Path, monkeypatch):
    """The default seat runner is the real run_session_sandboxed: every call
    the orchestrator makes must bind to its signature."""
    import inspect

    import factory.orchestrator as orchestrator
    from factory.session import run_session_sandboxed

    signature = inspect.signature(run_session_sandboxed)
    seats = Seats()

    def checked(*args, **kwargs):
        signature.bind(*args, **kwargs)  # TypeError on any drift
        return seats(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_session_sandboxed", checked)
    env = Env(tmp_path)
    result = run_once(env.queue_path, factory_root=env.root, open_pr=env.open_pr,
                      protection=lambda r, b: PROTECTED_STRICT,
                      pr_state=lambda u: "open",
                      now=lambda: OFF_PEAK)
    assert result.outcome == "pr_opened"
    assert [c[0] for c in seats.calls] == ["implementer", "reviewer"]


def test_huge_output_still_fits_a_pr_body(tmp_path: Path):
    env = Env(tmp_path, profile_extra={"commands": {
        "setup": "true",
        "test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
        "extra": {"noisy": f"{shlex.quote(sys.executable)} -c \"print('x' * 200000)\""},
    }})
    env.run(Seats(result="n" * 100_000))
    body = env.prs[0].body
    assert len(body) <= 65_536
    check_pr_body(body)
    with pytest.raises(PRBodyError):
        check_pr_body(body + "x" * 70_000)


# --------------------------------------------------------------- CLI wiring --


def test_cli_run_and_loop_are_wired(tmp_path: Path, monkeypatch):
    import factory.cli as cli

    seen = {}

    def fake_once(queue_path, **kwargs):
        seen["once"] = (Path(queue_path), kwargs)
        from factory.orchestrator import RunResult
        return RunResult(outcome="idle")

    def fake_loop(queue_path, **kwargs):
        seen["loop"] = (Path(queue_path), kwargs)
        from factory.orchestrator import LoopResult
        return LoopResult(stop_reason="queue_empty", results=[])

    monkeypatch.setattr(cli, "run_once", fake_once)
    monkeypatch.setattr(cli, "run_loop", fake_loop)
    queue = tmp_path / "tasks.json"
    assert cli.main(["run", "--queue", str(queue)]) == 0
    assert cli.main(["loop", "--queue", str(queue)]) == 0
    assert seen["once"][0] == queue and seen["loop"][0] == queue
    assert "run" not in cli.STUBS and "loop" not in cli.STUBS


def test_nothing_to_run_is_idle(tmp_path: Path):
    env = Env(tmp_path, tasks=[_task(status="draft")])
    result = env.run(Seats())
    assert result.outcome == "idle"
    assert env.runs_left() == []


# --- F-10 owner review round (PR #22; 5 findings; red before each fix) ------


def test_review1_push_publishes_the_validated_sha_not_the_branch(tmp_path, monkeypatch):
    """Finding 1: the branch can move between validation and push.

    A surviving implementer process (F-06 documents setsid escapees)
    commits to the work clone after L0/L1/L2 validated head_sha. What the
    remote receives must be the validated commit, not whatever the branch
    points at now.
    """
    from factory import orchestrator as orch

    env = Env(tmp_path)
    real_push = orch.push_branch

    def sneaky_push(repo, remote_url, branch, **kwargs):
        repo = Path(repo)
        (repo / "impl.py").write_text("def answer():\n    return 'UNVALIDATED'\n")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=sneaky", "-c", "user.email=s@x",
             "commit", "-q", "-m", "escapee moves the branch")
        # committing on the current branch already moves it — no -f needed
        return real_push(repo, remote_url, branch, **kwargs)

    monkeypatch.setattr(orch, "push_branch", sneaky_push)
    env.run(Seats())

    branch = env.task()["links"]["branch"]
    assert f"refs/heads/{branch}" in env.remote_branches()
    published = _git(env.remote, "show", f"refs/heads/{branch}:impl.py")
    assert "return 42" in published, "the remote holds the validated change"
    assert "UNVALIDATED" not in published, "the remote holds the escapee's rewrite"


def test_review1_push_confirms_the_remote_ref_or_refuses(tmp_path, monkeypatch):
    """Finding 1 (transport half): after the push, ls-remote must agree.

    If the remote ref does not read back as the validated sha, publishing
    is a PublishError — never a PR about an unconfirmed commit.
    """
    from factory import publish as pub
    from factory.publish import PublishError

    env = Env(tmp_path)
    work = create_run("fixture", env.repo, factory_root=env.root, ref="main")
    try:
        _git(work.root, "checkout", "-q", "-b", "factory/t-1-x")
        _git(work.root, "-c", "user.name=t", "-c", "user.email=t@x",
             "commit", "--allow-empty", "-q", "-m", "x")
        sha = _git(work.root, "rev-parse", "HEAD")
        real_run = subprocess.run

        def lying_ls_remote(argv, **kwargs):
            if "ls-remote" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=f"{'0' * 40}\trefs/heads/factory/t-1-x\n", stderr=""
                )
            return real_run(argv, **kwargs)

        monkeypatch.setattr(pub.subprocess, "run", lying_ls_remote)
        with pytest.raises(PublishError):
            push_branch(work.root, str(env.remote), "factory/t-1-x", sha=sha,
                        created={"factory/t-1-x"}, default_branch="main")
    finally:
        from factory.runs import cleanup_run
        cleanup_run(work)


def test_review2_pr_failure_after_push_escalates_instead_of_requeueing(tmp_path):
    """Finding 2 (pushed half): gh failing after the push must not loop.

    The branch is already public; re-queueing to `ready` with no attempt
    spends two more sessions per loop invocation forever and orphans
    branches. It is `needs_human` with the attempt and branch recorded.
    """
    from factory.publish import PublishError

    env = Env(tmp_path)

    def failing_open_pr(pr):
        raise PublishError("gh pr create failed: HTTP 422")

    result = run_once(env.queue_path, factory_root=env.root, session_runner=Seats(),
                      protection=lambda r, b: PROTECTED_STRICT,
                      pr_state=lambda u: "open",
                      open_pr=failing_open_pr, now=lambda: OFF_PEAK)

    assert result.outcome == "needs_human"
    assert result.escalation == "pr_create_failed"
    task = env.task()
    assert task["status"] == "needs_human"
    attempts = task["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "escalated"
    assert attempts[0]["escalation"] == "pr_create_failed"
    assert "HTTP 422" in attempts[0]["note"]
    assert f"refs/heads/{task['links']['branch']}" in env.remote_branches()  # kept, named


def test_review2_orchestrator_error_records_attempts_and_bounds_the_loop(
    tmp_path, monkeypatch
):
    """Finding 2 (general half): a mid-round error must spend the budget.

    A persistent L1 crash used to propagate with no attempt recorded, so
    every loop invocation re-ran both seats forever. With the attempt
    recorded, the loop is bounded by attempts_exhausted.
    """
    from factory import orchestrator as orch

    env = Env(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(orch, "run_l1", boom)
    # Review round 2: each loop stops at the first error, so a persistent
    # crash is bounded across invocations by the attempt budget (3 here).
    stops = [env.loop(Seats()).stop_reason for _ in range(4)]

    assert stops == ["error", "error", "error", "escalation"]
    task = env.task()
    assert task["status"] == "needs_human"
    attempts = task["attempts"]
    assert attempts[-1]["escalation"] == "attempts_exhausted"
    assert any(a.get("escalation") == "orchestrator_error" for a in attempts)
    assert all(a.get("escalation") != "quota_exhausted" for a in attempts)
    # nothing was ever pushed: the remote still holds only its cloned main
    assert not any(b.startswith("refs/heads/factory/")
                   for b in env.remote_branches())


def test_review2_new_escalations_are_schema_valid(tmp_path):
    """Finding 2: `pr_create_failed`/`orchestrator_error` join the enum.

    Red against the pre-round schema, which rejects both values (the
    F-09 schema-change pattern: the test is the proof the enum grew).
    """
    base = _task()
    for escalation in ("pr_create_failed", "orchestrator_error"):
        doc = {"version": 1, "project": "fixture",
               "tasks": [dict(base, attempts=[{
                   "run_id": "r1", "slice_id": "T-1", "started": "2026-10-18T10:00:00Z",
                   "ended": "2026-10-18T10:01:00Z", "outcome": "escalated",
                   "escalation": escalation,
                   "prompt_versions": {"implementer": "0" * 64}, "skills": [],
               }])]}
        path = tmp_path / f"queue-{escalation}.json"
        path.write_text(json.dumps(doc))
        TaskQueue(path).get("T-1")  # loads and validates


def test_review3_peak_window_before_review_records_the_aborted_round(tmp_path):
    """Finding 3: a validated round stopped by the window is accounted.

    The implementer's tokens and the attempt must be recorded (aborted —
    the window is nobody's fault), and the next session must be told the
    change existed, passed L0 and L1, and was discarded.
    """
    env = Env(tmp_path)
    clock = iter([OFF_PEAK, OFF_PEAK] + [datetime(2026, 10, 19, 9, 0, tzinfo=STOCKHOLM)] * 50)
    seats = Seats()  # a CORRECT change: validation passes, reviewer is next
    result = env.run(seats, now=lambda: next(clock))

    assert result.outcome == "peak_window"
    assert len(seats.prompts("implementer")) == 1
    assert seats.prompts("reviewer") == []
    task = env.task()
    assert task["status"] == "ready"
    attempts = task["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "aborted"
    assert attempts[0]["input_tokens"] == 120  # the implementer's spend, kept
    assert "passed L0 and L1" in attempts[0]["note"]
    log = ProgressLog.for_task(env.root, "fixture", "T-1")
    rendered = log.render()
    assert "passed L0 and L1" in rendered  # the next session learns it


def test_review4_reviewer_not_started_when_the_budget_is_spent(tmp_path, monkeypatch):
    """Finding 4: a spent wall clock escalates before the reviewer runs.

    The old `max(remaining, 1.0)` floor let the reviewer start (and run a
    minute past the budget) whenever the implementer had used it all.
    """
    from factory import orchestrator as orch

    env = Env(tmp_path, tasks=[_task(budget={"size": "S", "max_attempts": 3,
                                             "max_turns": 20, "wall_clock_min": 1})])
    ticks = [0.0, 61.0]

    def fake_monotonic():
        return ticks.pop(0) if ticks else 62.0

    monkeypatch.setattr(orch.time, "monotonic", fake_monotonic)
    seats = Seats()
    result = env.run(seats)

    assert result.outcome == "needs_human"
    assert result.escalation == "budget_exhausted"
    assert seats.prompts("reviewer") == []  # no session past the budget
    attempts = env.task()["attempts"]
    assert attempts[-1]["escalation"] == "budget_exhausted"


# --- review round 2 (PR #22) ------------------------------------------------


def test_review5_bookkeeping_failure_after_pr_never_opens_a_second_pr(
    tmp_path, monkeypatch
):
    """Once a PR is open, a failure recording it must not re-queue the task.

    The pass attempt (with the PR URL) is saved before the in_review
    transition; if that transition fails, the next run must finish the
    bookkeeping instead of running both seats again and opening a second
    PR for the same task.
    """
    from factory.state import StateError, TaskQueue

    env = Env(tmp_path)
    real_transition = TaskQueue.transition
    fail_once = {"left": 1}

    def flaky(self, task_id, new_status):
        if new_status == "in_review" and fail_once["left"]:
            fail_once["left"] -= 1
            raise StateError("disk full")
        return real_transition(self, task_id, new_status)

    monkeypatch.setattr(TaskQueue, "transition", flaky)
    seats = Seats()
    with pytest.raises(StateError):
        env.run(seats)
    first_url = env.prs and "https://github.com/fixture/fixture/pull/1"
    assert len(env.prs) == 1

    result = env.run(seats)

    assert len(env.prs) == 1, "a second PR was opened for the same task"
    assert len(seats.calls) == 2, "the seats ran again after the PR was open"
    assert result.outcome == "pr_opened"
    assert result.pr == first_url
    task = env.task()
    assert task["status"] == "in_review"
    assert task["links"]["pr"] == first_url


def test_review6_push_fence_refusal_escalates_immediately(tmp_path, monkeypatch):
    """A fence refusal is deterministic: retrying can't make it pass, so it
    escalates on the first occurrence instead of spending the budget."""
    from factory import orchestrator as orch
    from factory.publish import PushRefused

    env = Env(tmp_path)

    def refuse(*args, **kwargs):
        raise PushRefused("refusing to push: fence")

    monkeypatch.setattr(orch, "push_branch", refuse)
    seats = Seats()
    result = env.run(seats)

    assert result.outcome == "needs_human"
    assert result.escalation == "publish_refused"
    task = env.task()
    assert task["status"] == "needs_human"
    assert [a["escalation"] for a in task["attempts"]] == ["publish_refused"]
    assert "fence" in task["attempts"][0]["note"]
    assert len(seats.calls) == 2


def test_review6_pr_body_refusal_escalates_immediately(tmp_path, monkeypatch):
    from factory import orchestrator as orch
    from factory.prbody import PRBodyError

    env = Env(tmp_path)

    def refuse(body):
        raise PRBodyError("missing section: Evidence")

    monkeypatch.setattr(orch, "check_pr_body", refuse)
    result = env.run(Seats())

    assert (result.outcome, result.escalation) == ("needs_human", "publish_refused")
    assert env.task()["status"] == "needs_human"
    assert not env.prs


def test_review6_loop_stops_on_an_orchestrator_error(tmp_path, monkeypatch):
    """A broken orchestrator must not drain the budget unattended: loop
    stops on the first error, with the attempt recorded."""
    from factory import orchestrator as orch

    env = Env(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(orch, "run_l1", boom)
    seats = Seats()
    result = env.loop(seats)

    assert result.stop_reason == "error"
    assert len(result.results) == 1
    assert len(seats.calls) == 1  # implementer only; L1 crashed before review
    task = env.task()
    assert task["status"] == "ready"
    assert [a["escalation"] for a in task["attempts"]] == ["orchestrator_error"]


def test_review7_task_contract_lists_every_escalation_reason():
    """Mechanical sweep: every escalation the schema allows is documented
    in TASK-CONTRACT.md's escalation-reasons line."""
    repo = Path(__file__).resolve().parent.parent
    schema = json.loads((repo / "schema" / "tasks.schema.json").read_text())
    reasons = schema["$defs"]["escalation"]["enum"]
    doc = (repo / "TASK-CONTRACT.md").read_text()
    line = next(ln for ln in doc.splitlines() if ln.startswith("Escalation reasons"))
    missing = [r for r in reasons if f"`{r}`" not in line]
    assert not missing, f"TASK-CONTRACT.md doesn't list: {missing}"


# --- rehearsal blocker (JobFinderOS readiness, 2026-10-18) --------------------


def test_rehearsal_run_builds_on_the_remote_default_branch(tmp_path):
    """The run used to clone the local checkout's `main`, which can be days
    behind GitHub; the agent then built on stale code and the PR's base was
    wrong. The run now fetches the remote's default branch and builds on it."""
    env = Env(tmp_path)
    scratch = tmp_path / "scratch"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(scratch)], check=True)
    (scratch / "remote_only.txt").write_text("landed on GitHub, not pulled locally\n")
    _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
    _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "remote moves on")
    _git(scratch, "push", "-q", "origin", "HEAD:main")
    remote_main = _git(env.remote, "rev-parse", "refs/heads/main").strip()

    result = env.run(Seats())

    assert result.outcome == "pr_opened", result
    head = env.prs[0].head
    subprocess.run(
        ["git", "-C", str(env.remote), "merge-base", "--is-ancestor", remote_main,
         f"refs/heads/{head}"],
        check=True,
    )


def test_rehearsal_run_refuses_when_the_remote_is_unreachable(tmp_path):
    """No fetch, no run: building on an unknown base is worse than not
    running. Nothing starts a session."""
    env = Env(tmp_path, profile_extra={})
    profile = yaml.safe_load(env.profile_path.read_text())
    profile["repo"]["remote"] = str(tmp_path / "does-not-exist.git")
    env.profile_path.write_text(yaml.safe_dump(profile))
    seats = Seats()
    with pytest.raises(Exception, match="(?i)fetch"):
        env.run(seats)
    assert seats.calls == []
    assert env.task()["status"] == "ready"


# --- an unrunnable session interpreter refuses before anything is spent -----


def test_an_unimportable_sdk_refuses_the_run_before_any_attempt(tmp_path: Path):
    """The F-31 incident: a venv interpreter the sandbox silently degraded
    to its base died on ModuleNotFoundError one full session too late,
    and the orchestrator_error attempt it recorded spent 1 of 3. The
    interpreter probe refuses the run as a clean environment error
    BEFORE anything is picked or recorded: no attempt, no lease, the
    task stays ready, no session ever starts."""
    env = Env(tmp_path)
    seats = Seats()
    probe_calls: list[tuple] = []

    def broken(*args, **kwargs):
        probe_calls.append((args, kwargs))
        raise RunError(
            "the sandboxed session interpreter cannot import "
            "claude_agent_sdk (.venv/bin/python, exit 1): "
            "ModuleNotFoundError — the sandbox denies $HOME, so a venv "
            "interpreter needs its own pyvenv.cfg and site-packages "
            "re-allowed")

    with pytest.raises(RunError, match="claude_agent_sdk"):
        env.run(seats, sdk_probe=broken)

    assert probe_calls, "the probe never ran"
    assert len(probe_calls[0][0]) == 1  # the profile, positionally
    task = TaskQueue(env.queue_path).get("T-1")
    assert task["attempts"] == []
    assert task["status"] == "ready"
    assert seats.calls == []
    assert env.prs == []


def test_the_probe_matches_real_session_settings(monkeypatch):
    """Review fix on PR #73: the probe builds its settings from the
    project profile and a toolchain that includes the bundled CLI — the
    same surfaces a real session gets — not from an empty profile."""
    import factory.session as session_module

    seen: dict = {}
    real_build = session_module.build_settings

    def spy_build(profile, run_dir, **kwargs):
        seen["profile"] = profile
        seen["toolchain"] = kwargs.get("toolchain")
        return real_build(profile, run_dir, **kwargs)

    def spy_toolchain(extra=None):
        seen["extra"] = list(extra or [])
        return [sys.executable, *[p for p in (extra or []) if p]]

    monkeypatch.setattr(session_module, "build_settings", spy_build)
    monkeypatch.setattr(session_module, "default_toolchain", spy_toolchain)
    monkeypatch.setattr(
        session_module, "run_sandboxed",
        lambda *a, **k: subprocess.CompletedProcess(["srt"], 0, "", ""),
    )

    probe_profile = {"docs_allowlist": ["docs.example"]}
    session_module.probe_session_interpreter(probe_profile)

    assert seen["profile"] is probe_profile
    assert seen["extra"] == [session_module._bundled_cli()]
    assert seen["toolchain"][0] == sys.executable
