"""Shared harness moved from tests/test_f30.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f30.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

import json
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from _support.f63 import ledger_approve
from factory.contract import acceptance_hash
from factory.mergeorder import Protection
from factory.orchestrator import PullRequest, run_once
from factory.session import SessionRecord
from factory.trace import trace_path

STOCKHOLM = ZoneInfo("Europe/Stockholm")


OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)


REF = "tests/test_feature.py::test_ac_1"


PROTECTED_STRICT = Protection(checked=True, strict=True)


TEST_SOURCE = textwrap.dedent(
    """
    def test_ac_1():
        from impl import answer
        assert answer() == 42
    """
)


REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})


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
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 30},
        "attempts": [],
    }
    task.update(overrides)
    return task


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
                "test": f"{sys.executable} -m pytest -q -p no:cacheprovider",
            },
            "paths": {"risk": {}, "protected": [".factory/**"]},
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
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

    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        doc = json.loads(self.queue_path.read_text())
        return {t["id"]: t for t in doc["tasks"]}[task_id]

    def trace_path(self, run_id: str) -> Path:
        return trace_path(self.root, "fixture", run_id)

    def trace_events(self, run_id: str) -> list[dict]:
        lines = self.trace_path(run_id).read_text().splitlines()
        return [json.loads(line) for line in lines]

    def run(self, session_runner, **kwargs):
        # F-63: record the fixture's owner approvals before the run.
        ledger_approve(self.root, self.queue_path)
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


class Seats:
    """Stub seats with ``run_session_sandboxed``'s signature (f10 pattern)."""

    def __init__(self, answer=42, result="Implemented answer().", review=REVIEW_PASS,
                 implementer=None):
        self.answer = answer
        self.result = result
        self.review = review
        self.implementer = implementer

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        if seat == "implementer":
            if self.implementer is not None:
                return self.implementer(prompt, run_dir, kwargs)
            (run_dir / "impl.py").write_text(f"def answer():\n    return {self.answer}\n")
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40}, result=self.result,
            )
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10}, result=self.review,
        )
