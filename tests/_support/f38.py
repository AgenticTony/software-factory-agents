"""Shared harness moved from tests/test_f38.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f38.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

import json
import re
import shlex
import subprocess
import sys
import textwrap
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from factory.contract import acceptance_hash
from factory.orchestrator import PullRequest, run_once
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
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 10},
        "attempts": [],
    }
    task.update(overrides)
    return task


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
        (self.repo / "tests" / "test_base.py").write_text(
            "def test_base():\n    assert True\n"
        )
        import yaml

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

    def run(self, session_runner, **kwargs):
        from factory.mergeorder import Protection

        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault(
            "protection", lambda r, b: Protection(checked=True, strict=True)
        )
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_once(
            self.queue_path, factory_root=self.root, session_runner=session_runner,
            open_pr=self.open_pr, **kwargs,
        )


def _vote(keep=True, reason="confirmed against the diff", tag=None) -> dict:
    vote: dict = {"keep": keep, "reason": reason}
    if keep:
        vote["tag"] = tag or "important"
    return vote


def _votes_payload(*votes: dict) -> str:
    return json.dumps({"votes": list(votes)})


def _findings_array(prompt: str) -> list[dict]:
    """The union findings array out of a verifier prompt (test-side parse:
    the stub votes by id, but scripts key on text — see the module doc)."""
    match = re.search(r"```json\n(\[\s*\{.*?\}\s*\])\n```", prompt, re.S)
    assert match, f"no findings array in the verifier prompt:\n{prompt[:2000]}"
    return json.loads(match.group(1))


class Fanout:
    """Scripted seats for the parallel-review pipeline.

    ``reviews`` is the verdict JSON every reviewer session returns (F-38
    reviewers share one prompt, so one script per round). ``vote_by_text``
    maps a finding-text substring to its verifier vote; findings with no
    entry are kept with the default tag. Thread-safe: the fan-out calls
    seats concurrently. ``on_reviewer`` runs inside the reviewer branch
    (the budget test flips its fake clock there). ``echo_turn_cap`` (the
    review-round turn-budget tests): True → a review seat reports half its
    max_turns cap as turns, False → its full cap; None → fixed counts.
    """

    def __init__(self, reviews: str = _verdict(), vote_by_text: dict | None = None,
                 on_reviewer=None, implementer_turns: int = 3):
        self.reviews = reviews
        self.vote_by_text = vote_by_text or {}
        self.on_reviewer = on_reviewer
        self.implementer_turns = implementer_turns
        self.echo_turn_cap = None
        self.stamp_toolkit_skills = False
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Path, dict]] = []

    def _skills(self, kwargs: dict) -> list:
        """The F-22 pattern: a stub mimics the runner's record stamping
        from the toolkit kwarg the session was actually handed."""
        if not self.stamp_toolkit_skills:
            return []
        tk = kwargs.get("toolkit")
        if not tk:
            return []
        return sorted(f"{i['name']}@{i['content_hash']}" for i in tk["items"])

    def _review_turns(self, seat: str, kwargs: dict) -> int:
        if self.echo_turn_cap is None:
            return 2 if seat != "verifier" else 1
        cap = int(kwargs.get("max_turns") or 1)
        return max(1, cap // 2) if self.echo_turn_cap else cap

    def seats(self) -> list[str]:
        return [seat for seat, *_ in self.calls]

    def prompts(self, seat: str) -> list[str]:
        return [prompt for s, prompt, *_ in self.calls if s == seat]

    def kwargs(self, seat: str) -> list[dict]:
        return [kw for s, _, _, kw in self.calls if s == seat]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        with self.lock:
            self.calls.append((seat, prompt, Path(run_dir), kwargs))
        if seat == "implementer":
            run_dir = Path(run_dir)
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=self.implementer_turns,
                model="stub-model", tokens={"input_tokens": 120, "output_tokens": 40},
                result="Implemented answer().", skills=self._skills(kwargs),
            )
        if seat == "reviewer":
            result = self.reviews
            if self.on_reviewer is not None:
                scripted = self.on_reviewer()
                if scripted is not None:
                    result = scripted
            return SessionRecord(
                seat=seat, status="completed",
                num_turns=self._review_turns(seat, kwargs), model="stub-model",
                tokens={"input_tokens": 60, "output_tokens": 10}, result=result,
                skills=self._skills(kwargs),
            )
        if seat == "verifier":
            votes = []
            for finding in _findings_array(prompt):
                spec = next(
                    (v for text, v in self.vote_by_text.items()
                     if text in finding.get("text", "")),
                    None,
                )
                if spec is None:
                    votes.append({"id": finding["id"], **_vote()})
                elif spec.get("drop"):
                    votes.append(
                        {"id": finding["id"], "keep": False,
                         "reason": spec["reason"]}
                    )
                else:
                    votes.append({"id": finding["id"], **_vote(**spec)})
            return SessionRecord(
                seat=seat, status="completed",
                num_turns=self._review_turns(seat, kwargs), model="stub-model",
                tokens={"input_tokens": 55, "output_tokens": 5},
                result=_votes_payload(*votes), skills=self._skills(kwargs),
            )
        raise AssertionError(f"unexpected seat {seat!r}")


def _one_round_task() -> dict:
    """A task whose budget allows a single attempt, for the tests that
    script one failing review round and assert on exactly that round's
    record (with retries allowed, a declared-fail verdict would run the
    whole fan-out again every round)."""
    task = _task()
    task["budget"] = dict(task["budget"], max_attempts=1)
    return task
