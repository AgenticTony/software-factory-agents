"""F-72 test support: escalation-round seats, escalation-PR capture, gh seams.

The F-38/F-23 split holds: git, the clone manager, the queue and the L0/L1
sandboxes are real; only the model seats (and, for AC-4, the gh read/write
seams) are scripted. What this harness adds over ``_support.f23``:

* per-round implementer FILE scripts — a round may write extra files
  (a protected path, a manifest) or delete earlier ones (the revert
  round AC-1 exists for);
* an ``arbiter`` seat scripted with a rulings payload;
* the escalation-PR capture (``Env72.prs`` records the PullRequest the
  orchestrator asked to open, draft flag and label included);
* gh seams for AC-4: ``pr_head`` (URL -> head SHA, scriptable per call),
  ``pr_ready`` / ``pr_close`` call recorders.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from _support.f63 import ledger_approve
from _support.tokens import fake_aws_key, fake_token
from factory.contract import acceptance_hash
from factory.orchestrator import PullRequest, run_once
from factory.session import SessionRecord
from factory.state import TaskQueue

STOCKHOLM = ZoneInfo("Europe/Stockholm")
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)

REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = (
    "from impl import answer\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    "    assert answer() == 42\n"
)

# A credential the L0 sentinel flags — built at runtime (the F-58 AC-G
# rule: no credential-shaped literal in the tests tree), exactly the
# "AWS access key id" shape (AKIA + 16 chars).
CREDENTIAL_LINE = f'key = "{fake_aws_key("f72-cred")}"'

# A literal that is NOT credential-shaped (no sentinel pattern matches a
# bare reference line) but IS a run secret for the push fence when the
# test pins run_secrets to it — the seat-token-in-history arm.
SEAT_TOKEN = fake_token("f72-seat")

IMPL_GOOD = "def answer():\n    return 42\n"
IMPL_BAD = "def answer():\n    return 0\n"  # the contract test stays red


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", **overrides) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42",
         "check": {"type": "test", "ref": REF}}
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
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
        "scope": {"paths": ["impl.py", "tests/test_feature.py"]},
        "attempts": [],
    }
    task.update(overrides)
    return task


def _finding(text="the helper is misnamed", file="impl.py", axis="standards",
             severity="minor", **extra) -> dict:
    item = {"id": "R-1", "axis": axis, "severity": severity, "file": file,
            "text": text}
    item.update(extra)
    return item


def _verdict(*findings: dict, declared: str | None = None) -> str:
    payload = {"verdict": declared or ("fail" if findings else "pass"),
               "findings": list(findings)}
    return json.dumps(payload)


def rulings_payload(*rulings: dict) -> str:
    return json.dumps({"rulings": list(rulings)})


def _prompt_findings(prompt: str) -> list[dict]:
    """The findings array out of an arbiter prompt (test-side parse)."""
    match = re.search(
        r"## Repeated findings.*?```json\n(\[.*?\])\n```", prompt, re.S)
    assert match, "no findings array in the arbiter prompt"
    return json.loads(match.group(1))


def _ruling(id_="R-1", ruling="invalid", reason="not a defect") -> dict:
    return {"id": id_, "ruling": ruling, "reason": reason}


class Env72:
    """One fixture project: local repo + bare remote + factory root.

    ``profile_extra`` deep-merges into the profile (sentinels, risk globs)
    before it is written; ``task_scope`` overrides the fixture task's
    default scope.
    """

    def __init__(self, tmp_path: Path, tasks: list | None = None,
                 profile_extra: dict | None = None):
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
        for key, value in (profile_extra or {}).items():
            if isinstance(value, dict) and isinstance(self.profile.get(key), dict):
                self.profile[key] = {**self.profile[key], **value}
            else:
                self.profile[key] = value
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        (factory_dir / "project.yaml").write_text(yaml.safe_dump(self.profile))
        self.queue_path = factory_dir / "tasks.json"
        doc = {"version": 1, "project": "fixture", "tasks": tasks or [_task()]}
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        self.base_sha = _git(self.repo, "rev-parse", "HEAD")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)],
            check=True,
        )
        self.prs: list[PullRequest] = []
        self.open_pr_error: Exception | None = None

    # ------------------------------------------------------ run + PR seams --

    def open_pr(self, pr: PullRequest) -> str:
        if self.open_pr_error is not None:
            raise self.open_pr_error
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        return TaskQueue(self.queue_path).get(task_id)

    def run(self, session_runner, **kwargs):
        ledger_approve(self.root, self.queue_path)
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

    # -------------------------------------------------------- remote truth --

    def remote_branches(self) -> list[str]:
        out = _git(self.remote, "for-each-ref", "--format=%(refname:short)",
                   "refs/heads/")
        return [line for line in out.splitlines() if line.strip()]

    def remote_branch_sha(self, branch: str) -> str:
        return _git(self.remote, "rev-parse", f"refs/heads/{branch}")

    def pushed_paths(self, branch: str) -> set[str]:
        """Every path ANY commit in base..branch on the remote touches —
        the AC-1 history test: a reverted protected change must leave no
        trace in the published history, not only the final tree."""
        out = _git(self.remote, "log", "--name-only", "--format=%H",
                   f"{self.base_sha}..refs/heads/{branch}")
        paths: set[str] = set()
        for line in out.splitlines():
            line = line.strip()
            if not line or re.fullmatch(r"[0-9a-f]{40}", line):
                continue  # the commit header (%H) or a blank separator
            paths.add(line)
        return paths

    def pushed_commit_count(self, branch: str) -> int:
        out = _git(self.remote, "rev-list", "--count",
                   f"{self.base_sha}..refs/heads/{branch}")
        return int(out or 0)


class GhSeams:
    """Scripted gh read/write seams for AC-4.

    ``head_answers`` is a list of head SHAs served per call (the last
    repeats) — the force-push-between-y-and-recheck probe serves one SHA
    then another. ``pr_ready`` / ``pr_close`` record their calls.
    """

    def __init__(self, head_answers: list[str]):
        self.head_answers = list(head_answers)
        self.head_calls: list[str] = []
        self.ready_calls: list[str] = []
        self.close_calls: list[str] = []

    def pr_head(self, url: str) -> str:
        self.head_calls.append(url)
        if not self.head_answers:
            raise AssertionError("no head answer scripted")
        answer = self.head_answers.pop(0) if len(self.head_answers) > 1 \
            else self.head_answers[0]
        return answer

    def pr_ready(self, url: str) -> None:
        self.ready_calls.append(url)

    def pr_close(self, url: str) -> None:
        self.close_calls.append(url)


class Seats:
    """Scripted seats for the escalation scenarios.

    ``impl_files``: one dict per implementer round mapping a repo-relative
    path to content to WRITE (None = DELETE the path — the revert round).
    Beside the script, every round writes the green impl.py and the
    contract test file (a round may override either by naming it).

    ``reviews``: the verdict JSON each ROUND's review returns (the last
    repeats); ``final``: the F-41 final review's verdict (default pass).

    ``arbiter``: the payload the arbiter seat returns — a rulings JSON
    string, or ``SeatError`` to fail the session (a failed arbiter
    escalates), or any malformed text.

    ``impl_result``: the implementer's result text (the no-notes-in-prompt
    arm plants a marker in it).
    """

    def __init__(self, impl_files: list | None = None,
                 reviews: tuple | None = None, final: str | None = None,
                 arbiter: str | None = None, impl_result: str = "Implemented.",
                 implementer_turns: int = 3):
        self.impl_files = list(impl_files or [])
        self.reviews = list(reviews or (_verdict(),))
        self.final = final
        self.arbiter = arbiter
        self.impl_result = impl_result
        self.implementer_turns = implementer_turns
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Path, dict]] = []
        self.round_reviews = 0
        self.round_passed: dict[int, bool] = {}

    class SeatError(Exception):
        """Raised by a scripted seat to fail that session."""

    def seats(self) -> list[str]:
        return [seat for seat, *_ in self.calls]

    def prompts(self, seat: str) -> list[str]:
        return [prompt for s, prompt, _, _ in self.calls if s == seat]

    def kwargs(self, seat: str) -> list[dict]:
        return [kw for s, _, _, kw in self.calls if s == seat]

    def _script(self, run_dir: Path, round_no: int) -> None:
        script = self.impl_files[min(round_no - 1, len(self.impl_files) - 1)] \
            if self.impl_files else {}
        wrote = set()
        for rel, content in script.items():
            target = run_dir / rel
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            wrote.add(rel)
        for rel, content in (("impl.py", IMPL_GOOD),
                             ("tests/test_feature.py", TEST_SOURCE)):
            if rel in wrote:
                continue
            target = run_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

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
            self._script(run_dir, round_no)
            return SessionRecord(
                seat=seat, status="completed",
                num_turns=self.implementer_turns, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                usage={"input": 120, "cache_read": 0, "cache_write": 0,
                       "output": 40},
                result=self.impl_result,
            )
        if seat == "arbiter":
            if isinstance(self.arbiter, self.SeatError):
                return SessionRecord(
                    seat=seat, status="failed", num_turns=1,
                    model="stub-model", reason=str(self.arbiter),
                )
            payload = self.arbiter
            if isinstance(payload, dict):
                # An auto-script: rule EVERY finding the prompt carries
                # (the real seat reads the ids from the prompt — the
                # union renumbers them, so tests must not hardcode ids).
                ids = [f["id"] for f in _prompt_findings(prompt)]
                payload = rulings_payload(*(
                    _ruling(i, payload.get("ruling", "invalid"),
                            payload.get("reason", "scripted"))
                    for i in ids
                ))
            return SessionRecord(
                seat=seat, status="completed", num_turns=2, model="stub-model",
                tokens={"input_tokens": 70, "output_tokens": 12},
                usage={"input": 70, "cache_read": 0, "cache_write": 0,
                       "output": 12},
                result=payload if payload is not None
                else rulings_payload(_ruling()),
            )
        # review-family seats: the round's verdict, the F-41 final after a
        # passing round review (the _support.f23.Scripted routing).
        if self.round_reviews > 2 and self.round_passed.get(round_no):
            verdict = self.final if self.final is not None else _verdict()
        else:
            verdict = self.reviews[min(round_no - 1, len(self.reviews) - 1)]
            try:
                passed = json.loads(verdict).get("verdict") == "pass"
            except ValueError:
                passed = False
            if passed:
                self.round_passed[round_no] = True
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            usage={"input": 60, "cache_read": 0, "cache_write": 0,
                   "output": 10},
            result=verdict,
        )
