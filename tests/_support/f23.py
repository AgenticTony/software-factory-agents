"""Shared harness moved from tests/test_f23.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f23.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

import json
import shlex
import subprocess
import sys
import textwrap
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from _support.f63 import ledger_approve
from factory.contract import acceptance_hash
from factory.orchestrator import PullRequest, run_once
from factory.progress import ProgressLog
from factory.session import SessionRecord
from factory.state import TaskQueue

STOCKHOLM = ZoneInfo("Europe/Stockholm")


OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)


REF = "tests/test_feature.py::test_ac_1"


TEST_SOURCE = textwrap.dedent(
    """
    from impl import answer


    def test_ac_1():
        assert answer() == 42
    """
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", **overrides) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42", "check": {"type": "test", "ref": REF}}
    ]
    task = {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-18T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 30},
        "scope": {"paths": ["impl.py", "tests/test_feature.py"]},
        "attempts": [],
    }
    task.update(overrides)
    return task


def _finding(text="scope creep", file="impl.py", axis="spec",
             severity="blocking", **extra) -> dict:
    item = {"id": "R-1", "axis": axis, "severity": severity, "file": file, "text": text}
    item.update(extra)
    return item


def _verdict(*findings: dict, declared: str | None = None) -> str:
    payload = {"verdict": declared or ("fail" if findings else "pass"),
               "findings": list(findings)}
    return json.dumps(payload)


class Env:
    """One fixture project: local repo + bare remote + factory root."""

    def __init__(self, tmp_path: Path, tasks: list | None = None):
        self.tmp = tmp_path
        self.root = tmp_path / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
        self.profile = {
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
            "paths": {"risk": {}, "protected": [".factory/**"]},
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        import yaml

        (factory_dir / "project.yaml").write_text(yaml.safe_dump(self.profile))
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

    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        return TaskQueue(self.queue_path).get(task_id)

    def log(self) -> ProgressLog:
        return ProgressLog.for_task(self.root, "fixture", "T-1")

    def report(self, attempt_no: int) -> dict:
        """The stored validator report of one attempt (F-10's _store layout)."""
        task = self.task()
        return json.loads(Path(task["attempts"][attempt_no - 1]["validator_report"]).read_text())

    def run(self, session_runner, **kwargs):
        # F-63: record the fixture's owner approvals (profile + frozen
        # tasks) before the run — the ledger every run consults.
        ledger_approve(self.root, self.queue_path)
        from factory.mergeorder import Protection

        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", lambda r, b: Protection(checked=True, strict=True))
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_once(
            self.queue_path, factory_root=self.root, session_runner=session_runner,
            open_pr=self.open_pr, **kwargs,
        )


class Scripted:
    """Stub seats with per-round scripted behaviour.

    ``impls`` holds one impl.py body per implementer round (the last
    repeats). The contract test must pass for L2 to run at all, so every
    body returns 42; the *content* differs when a scenario needs a moving
    head (a non-empty review delta) and is identical when it needs an
    unmoved one (an empty delta). Every review seat of a round — the F-38
    fan-out's two reviewer sessions and the verifier — returns
    ``reviews[round-1]`` (the last verdict repeats), so a scenario scripts
    exactly what each round's review says — EXCEPT the round's second
    fan-out, the F-41 final review, which returns ``final`` (default: a
    clean pass — the scenarios here script the round's own review, and a
    final that echoed the delta's droppable findings back would test F-41,
    not F-23; tests/test_f41.py does that). For the verifier that text is
    an invalid vote shape, which by design drops nothing: every finding
    survives the vote and F-23's convergence alone decides the round.
    Indexing is by IMPLEMENTER round (the implementer runs alone, before
    the fan-out) and the calls log is appended under a lock — a raw
    reviewer-call count is racy under the concurrent fan-out.
    """

    SAME = ("def answer():\n    return 42\n",)  # identical rounds: no new commit
    MOVING = (  # green every round, different content: the head moves each round
        "def answer():\n    return 42\n",
        "def answer():\n    return 42  # fix round\n",
        "def answer():\n    return 42  # fix round 2\n",
    )

    def __init__(self, impls=SAME, reviews=(_verdict(),), result="Implemented.",
                 final=None):
        self.impls = list(impls)
        self.reviews = list(reviews)
        self.final = final  # the F-41 final review's script (default: clean pass)
        self.result = result
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Path, dict]] = []
        self.round_reviews = 0  # reviewer calls since this round's implementer
        # PR #90 round 2: rounds whose scripted verdict passes — the F-41
        # final full review only ever follows a passing round review, so
        # a call beyond the round's own fan-out routes to `final` only
        # then. A round whose verdict fails to parse never sets this, so
        # the seats' ONE re-run each is fed the round script again (the
        # retry policy's own contract: same seat, same prompt).
        self.round_passed: dict[int, bool] = {}
        # PR #90 round 3: every reviewer prompt, bucketed per (round,
        # fan-out) as this harness ROUTED it — the fan-out contract
        # (every seat of one fan-out, re-runs included, gets the same
        # prompt) is asserted over these by _review_rounds.
        self.fanout_prompts: dict[tuple[int, str], set[str]] = {}
        self.fanout_order: list[tuple[int, str]] = []

    def note_fanout_prompt(self, round_no: int, prompt: str,
                           phase: str = "round"):
        """Record a reviewer prompt into its (round, fan-out) bucket —
        also the route for scenarios that bypass __call__ (review1b's
        dead seats), so the fan-out contract still sees them."""
        key = (round_no, phase)
        self.fanout_prompts.setdefault(key, set()).add(prompt)
        if key not in self.fanout_order:
            self.fanout_order.append(key)

    def prompts(self, seat: str) -> list[str]:
        return [prompt for s, prompt, _, _ in self.calls if s == seat]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        with self.lock:
            self.calls.append((seat, prompt, run_dir, kwargs))
            round_no = sum(1 for s, *_ in self.calls if s == "implementer")
            if seat == "implementer":
                self.round_reviews = 0
            else:
                self.round_reviews += 1
        if seat == "implementer":
            body = self.impls[min(round_no - 1, len(self.impls) - 1)]
            (run_dir / "impl.py").write_text(body)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40}, result=self.result,
            )
        if self.round_reviews > 2 and self.round_passed.get(round_no):
            # The round's second fan-out: the F-41 final review (the
            # fan-out is 2 wide in every scenario here). Only reachable
            # once the round's own review passed — a failing round never
            # runs one, and an unparseable round's reviewer re-runs stay
            # on the round script below.
            if seat == "reviewer":
                self.note_fanout_prompt(round_no, prompt, phase="final")
            verdict = self.final if self.final is not None else _verdict()
        else:
            if seat == "reviewer":
                self.note_fanout_prompt(round_no, prompt)
            verdict = self.reviews[min(round_no - 1, len(self.reviews) - 1)]
            if seat == "reviewer" and _passes_review(verdict):
                self.round_passed[round_no] = True
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10}, result=verdict,
        )


def _passes_review(verdict: str) -> bool:
    """Whether a scripted round verdict makes the round's review pass —
    i.e. whether the F-41 final full review may follow it. Every seat
    returns the same text, so this is a property of the script: a
    declared ``pass`` passes (the dropped-finding scenarios carry heavy
    findings on a declared pass, and convergence drops them out of
    delta — the pipeline's own call), anything else fails or never
    parses. The optimism is safe: a round whose review really fails
    never runs the second fan-out this flag would route."""
    try:
        data = json.loads(verdict)
    except ValueError:
        return False
    return data.get("verdict") == "pass"


def _review_rounds(seats) -> list[str]:
    """The round reviews' prompts, one per round. Every seat of one
    fan-out — and each seat's re-run, which carries its prompt verbatim
    — must receive the SAME prompt: the F-38 contract the old pairwise
    parity check guarded, asserted here per (round, fan-out) bucket
    (positional pairing died with retries; the harness routes every
    reviewer call, so its buckets know the fan-out a prompt belonged
    to)."""
    for key in seats.fanout_order:
        prompts = seats.fanout_prompts[key]
        assert len(prompts) == 1, (
            f"fan-out {key} sent its seats different prompts: "
            f"{sorted(prompts)}"
        )
    seen: set[int] = set()
    rounds: list[str] = []
    for round_no, phase in seats.fanout_order:
        if phase != "round" or round_no in seen:
            continue
        seen.add(round_no)
        rounds.append(next(iter(seats.fanout_prompts[(round_no, "round")])))
    return rounds
