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
from factory.runs import GIT_HARDENING, create_run
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
        return run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )

    def loop(self, session_runner, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
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
    review = json.dumps({"verdict": "fail", "findings": [
        {"id": "R1", "axis": "spec", "severity": "blocking", "text": "scope creep"}]})
    env = Env(tmp_path)
    seats = Seats(review=review)
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert [c[0] for c in seats.calls] == ["implementer", "reviewer"] * 3
    assert "scope creep" in seats.prompts("implementer")[1]
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
        re.compile(r"""["']merge["']\s*,\s*["']--"""),
    ]
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
    with pytest.raises(PRBodyError):
        env2.run(Seats())
    assert env2.prs == []
    assert env2.remote_branches() == {"refs/heads/main"}
    assert env2.task()["status"] == "ready"  # the aborted run re-queues
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


# --- AC-12: every run dir is cleaned up; loop reclaims orphans --------------


def test_ac_12(tmp_path: Path):
    # PR opened, needs_human, quota stop, exception: nothing is left behind.
    def quota(prompt, run_dir, kwargs):
        return SessionRecord(seat="implementer", status="failed", reason="api_error_429")

    def crash(prompt, run_dir, kwargs):
        raise RuntimeError("orchestrator-side bug")

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
    with pytest.raises(RuntimeError, match="orchestrator-side bug"):
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
        from pathlib import Path
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from factory.orchestrator import run_once

        def seat(seat, prompt, run_dir, profile, **kwargs):
            Path({str(marker)!r}).write_text(str(run_dir))
            os.kill(os.getpid(), signal.SIGKILL)  # killed mid-session

        run_once(Path({str(env.queue_path)!r}), factory_root=Path({str(env.root)!r}),
                 session_runner=seat, open_pr=lambda pr: "never")
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
            push_branch(work.root, str(env.remote), branch, created=created,
                        default_branch="main")
    assert spawned == [], "a refused push must not start any git process"

    main_before = _git(env.remote, "rev-parse", "refs/heads/main")
    push_branch(work.root, str(env.remote), "factory/t-1-x", created={"factory/t-1-x"},
                default_branch="main")
    (push,) = [argv for argv in spawned if "push" in argv]
    assert list(push[1:5]) == list(GIT_HARDENING)
    assert push[-1] == "refs/heads/factory/t-1-x:refs/heads/factory/t-1-x"
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
    assert pushes[0][-1] == f"refs/heads/{env2.prs[0].head}:refs/heads/{env2.prs[0].head}"
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
