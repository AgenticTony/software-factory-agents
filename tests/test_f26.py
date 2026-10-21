"""F-26 acceptance tests — BUILD-ORDER.md, work order F-26 (merges in order).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f26.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the fixture project's git repo and bare "GitHub" remote the
  orchestrator pushes to, the clone manager, the queue and run lease,
  L0 and L1 through the real pinned ``srt`` sandbox, the merge-order
  sweep (fetch + merge + re-validation on a scratch clone).
* **Stubbed:** the model (seat stubs, the f10/f25 pattern — no API spend),
  ``gh pr create`` (an ``open_pr`` recorder) and the ``gh api`` branch
  protection call (a ``protection`` fake; the real classifier is unit
  tested against canned gh output).
"""

import json
import shlex
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml

from factory.contract import ContractError, acceptance_hash, load_profile
from factory.mergeorder import (
    MergeOrderError,
    Protection,
    admission,
    classify_protection,
    gh_branch_protection,
)
from factory.orchestrator import run_loop, run_once
from factory.progress import ProgressLog
from factory.publish import PullRequest
from factory.session import SessionRecord
from factory.state import ALLOWED_TRANSITIONS, TaskQueue

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

# The protection fake's default: a repo whose default branch requires
# status checks with strict (up-to-date) enabled — admission passes.
PROTECTED_STRICT = Protection(checked=True, strict=True)
PLAN_LIMIT = Protection(checked=False, strict=False, plan_limit=True,
                        detail="403 upgrade to GitHub Pro")


def _protection(result: Protection):
    """A ``protection`` seam fake returning ``result`` for any repo."""
    return lambda remote, branch: result


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", *, ref: str = REF, **overrides) -> dict:
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
            "paths": {
                "risk": {"payments": ["src/pay/**"]},
                "protected": [".factory/**"],
            },
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        self.profile_path = factory_dir / "project.yaml"
        self.profile_path.write_text(yaml.safe_dump(self.profile))
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

    def set_repo_key(self, key, value) -> None:
        """Rewrite the profile with ``repo.<key> = value`` (tmp-path values)."""
        self.profile["repo"][key] = value
        self.profile_path.write_text(yaml.safe_dump(self.profile))

    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        return TaskQueue(self.queue_path).get(task_id)

    def log(self, task_id: str = "T-1") -> ProgressLog:
        return ProgressLog.for_task(self.root, "fixture", task_id)

    def run(self, session_runner, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", _protection(PROTECTED_STRICT))
        return run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )

    def loop(self, session_runner, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", _protection(PROTECTED_STRICT))
        return run_loop(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )

    def advance_remote_main(self, impl_body: str | None, *, extra_file: str | None = None,
                            message: str = "remote moves on") -> str:
        """Commit to the remote's main and return its new head sha.

        ``impl_body`` rewrites impl.py (pass a conflicting body to force a
        merge conflict); ``extra_file`` adds an unrelated file instead.
        """
        scratch = self.tmp / f"advance-{message.replace(' ', '-')}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(scratch)], check=True)
        if impl_body is not None:
            (scratch / "impl.py").write_text(impl_body)
        if extra_file is not None:
            (scratch / extra_file).write_text("unrelated remote change\n")
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", message)
        _git(scratch, "push", "-q", "origin", "HEAD:main")
        return _git(self.remote, "rev-parse", "refs/heads/main")


class Seats:
    """Stub seats with ``run_session_sandboxed``'s signature (f10 pattern)."""

    def __init__(self, review=REVIEW_PASS):
        self.review = review
        self.calls: list[tuple[str, str]] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        from pathlib import Path

        self.calls.append((seat, prompt))
        run_dir = Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests").mkdir(exist_ok=True)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(seat=seat, status="completed", num_turns=3,
                                 model="stub-model", result="Implemented answer().")
        return SessionRecord(seat=seat, status="completed", num_turns=1,
                             model="stub-model", result=self.review)


# ------------------------------------------------------------------ AC-1 ---


def test_ac_1(tmp_path: Path):
    """Admission: run/loop refuse without strict protection; the plan-limit
    403 plus a ``branch_protection: unavailable`` declaration is the one
    exception; the schema validates the new ``repo`` keys (red-first)."""
    profile = {
        "repo": {"path": "/tmp/x", "default_branch": "main",
                 "remote": "https://github.com/o/r.git"}
    }

    # Strict required checks: admitted, with or without any declaration.
    admission(profile, _protection(PROTECTED_STRICT))
    admission({**profile, "repo": {**profile["repo"], "branch_protection": "unavailable"}},
              _protection(PROTECTED_STRICT))

    # Protected but the setting is missing: refuse, naming it.
    for bad, named in (
        (Protection(checked=True, strict=False,
                    detail="required status checks do not require branches up to date"),
         "strict"),
        (Protection(checked=True, strict=False, detail="no required status checks"),
         "required status checks"),
        (Protection(checked=False, strict=False,
                    detail="404 branch protection is off"), "branch protection"),
    ):
        with pytest.raises(MergeOrderError, match=named):
            admission(profile, _protection(bad))
        # The declaration never excuses a repo that could have protection.
        with pytest.raises(MergeOrderError, match=named):
            admission({**profile,
                       "repo": {**profile["repo"], "branch_protection": "unavailable"}},
                      _protection(bad))

    # Plan-limit 403: admitted only with the declaration.
    admission({**profile, "repo": {**profile["repo"], "branch_protection": "unavailable"}},
              _protection(PLAN_LIMIT))
    with pytest.raises(MergeOrderError, match="branch_protection: unavailable"):
        admission(profile, _protection(PLAN_LIMIT))

    # Anything else gh might answer (other 403, gh missing): refuse.
    with pytest.raises(MergeOrderError):
        admission(profile, _protection(
            Protection(checked=False, strict=False, detail="403 forbidden")))

    # Schema (red-first: both keys were unknown to repo's closed object).
    base = {
        "name": "fixture",
        "repo": {"path": "/tmp/x", "default_branch": "main",
                 "remote": "https://github.com/o/r.git",
                 "branch_protection": "unavailable", "concurrency": 2},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(base))
    assert load_profile(path)["repo"]["concurrency"] == 2
    for bad_value in ("enabled", 1, None):
        bad = json.loads(json.dumps(base))
        bad["repo"]["branch_protection"] = bad_value
        path.write_text(yaml.safe_dump(bad))
        with pytest.raises(ContractError):
            load_profile(path)
    for bad_value in (0, -1, "2", 1.5):
        bad = json.loads(json.dumps(base))
        bad["repo"]["concurrency"] = bad_value
        path.write_text(yaml.safe_dump(bad))
        with pytest.raises(ContractError):
            load_profile(path)

    # End to end: the refusal fires before anything runs.
    env = Env(tmp_path / "e2e")
    seats = Seats()
    with pytest.raises(MergeOrderError, match="strict"):
        env.run(seats, protection=_protection(
            Protection(checked=True, strict=False, detail="strict is off")))
    assert env.task()["status"] == "ready"   # nothing picked, nothing spent
    assert env.prs == []
    assert seats.calls == []                 # no session ever started


# ------------------------------------------------------------------ AC-2 ---


def test_ac_2(tmp_path: Path):
    """Concurrency: one open factory PR per repo by default; a ready task
    waits. ``concurrency`` in project.yaml lifts the cap."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])

    first = env.run(Seats())
    assert first.outcome == "pr_opened"
    assert env.task("T-1")["status"] == "in_review"

    # Default (concurrency absent = 1): T-2 waits, nothing runs.
    seats = Seats()
    second = env.run(seats)
    assert second.outcome == "concurrency_wait"
    assert second.task_id == "T-2"
    assert seats.calls == []
    t2 = env.task("T-2")
    assert t2["status"] == "ready" and t2["attempts"] == []
    assert len(env.prs) == 1

    # The loop stops on the wait instead of spinning.
    loop = env.loop(Seats())
    assert loop.stop_reason == "concurrency_wait"
    assert len(env.prs) == 1

    # concurrency: 2 — the second PR opens.
    env2 = Env(tmp_path / "two", tasks=[_task("T-1"), _task("T-2")])
    env2.set_repo_key("concurrency", 2)
    assert env2.run(Seats()).outcome == "pr_opened"
    assert env2.run(Seats()).outcome == "pr_opened"
    assert env2.task("T-1")["status"] == "in_review"
    assert env2.task("T-2")["status"] == "in_review"
    assert len(env2.prs) == 2


# ------------------------------------------------------------------ AC-3 ---


def test_ac_3(tmp_path: Path):
    """Stale base: the default branch moved past an open PR's base →
    re-run L0 and L1 on the PR merged onto the new base. Failure →
    needs_human / stale_base (schema enum, red-first; the transition is a
    new legal edge). Up to date → no re-validation. Pass → still ready."""
    env = Env(tmp_path)
    assert env.run(Seats()).outcome == "pr_opened"
    branch = env.task()["links"]["branch"]
    assert env.task()["status"] == "in_review"

    # Up to date: the sweep is a no-op — no new attempt, no new log entry.
    before_attempts = len(env.task()["attempts"])
    before_entries = len(env.log().entries())
    assert env.run(Seats()).outcome == "idle"
    assert len(env.task()["attempts"]) == before_attempts
    assert len(env.log().entries()) == before_entries

    # Stale but clean (an unrelated remote advance): re-validation runs,
    # passes on the merged tree, and the PR stays ready for merge.
    env.advance_remote_main(None, extra_file="remote_only.txt")
    result = env.run(Seats())
    assert result.outcome == "idle"
    task = env.task()
    assert task["status"] == "in_review"
    assert len(task["attempts"]) == before_attempts  # no budget spent
    entries = env.log().entries()
    assert len(entries) == before_entries + 1        # the re-validation record
    assert entries[-1]["outcome"] == "pass"

    # Stale and conflicting: the merge itself fails → stale_base.
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    result = env.run(Seats())
    assert result.outcome == "needs_human"
    assert result.escalation == "stale_base"
    task = env.task()
    assert task["status"] == "needs_human"
    last = task["attempts"][-1]
    assert last["escalation"] == "stale_base"
    assert last["outcome"] == "escalated"
    assert "conflict" in (last.get("note") or "").lower()
    assert task["links"]["branch"] == branch  # the PR is still named

    # The escalation exists in the schema enum (red-first) and the
    # in_review -> needs_human edge is legal (lateral targets unchanged).
    schema = json.loads((REPO_ROOT / "schema" / "tasks.schema.json").read_text())
    assert "stale_base" in schema["$defs"]["escalation"]["enum"]
    assert ("in_review", "needs_human") in ALLOWED_TRANSITIONS
    assert ("in_review", "awaiting_owner") not in ALLOWED_TRANSITIONS


def test_stale_revalidation_catches_a_broken_merge(tmp_path: Path):
    """A stale merge that *conflicts-free* merges but fails the gates also
    escalates stale_base: the re-validation runs the real L0/L1, not just
    git-merge."""
    env = Env(tmp_path)
    assert env.run(Seats()).outcome == "pr_opened"

    # The remote deletes impl.py's test dependency: the PR's own test file
    # still passes on its branch, but merged onto a main where a NEW test
    # demands a different answer, the merge fails the suite.
    scratch = tmp_path / "break-merge"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(scratch)], check=True)
    (scratch / "tests" / "test_remote.py").write_text(
        "from impl import answer\n\n\ndef test_remote_wants_zero():\n    assert answer() == 0\n"
    )
    _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
    _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "main moves on")
    _git(scratch, "push", "-q", "origin", "HEAD:main")

    result = env.run(Seats())
    assert result.outcome == "needs_human"
    assert result.escalation == "stale_base"
    assert env.task()["status"] == "needs_human"


# ----------------------------------------------- the gh api classifier ------


def _gh(returncode: int, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_classifier_matrix():
    ok = json.dumps({"required_status_checks": {"strict": True, "contexts": ["ci"]}})
    assert classify_protection(0, ok, "") == Protection(checked=True, strict=True)
    lax = json.dumps({"required_status_checks": {"strict": False, "contexts": ["ci"]}})
    got = classify_protection(0, lax, "")
    assert got.checked and not got.strict
    none = json.dumps({"required_status_checks": None})
    got = classify_protection(0, none, "")
    assert got.checked and not got.strict
    got = classify_protection(1, "", "gh: Not Found (HTTP 404)")
    assert not got.checked and not got.strict and not got.plan_limit
    got = classify_protection(
        1, "", "gh: 403 Upgrade to GitHub Pro or make this repository public (HTTP 403)")
    assert got.plan_limit and not got.checked
    got = classify_protection(1, "", "gh: 403 forbidden (HTTP 403)")
    assert not got.plan_limit and not got.checked


def test_gh_branch_protection_builds_the_call(tmp_path: Path):
    """The real seam runs gh api against the slug and feeds the classifier."""
    argv: list = []

    def fake_run(args, **kwargs):
        argv.extend(args)
        return _gh(0, json.dumps({"required_status_checks": {"strict": True}}))

    got = gh_branch_protection("https://github.com/o/r.git", "main", _run=fake_run)
    assert got == Protection(checked=True, strict=True)
    assert argv[1:4] == ["api", "repos/o/r/branches/main/protection"]


def test_gh_branch_protection_refuses_non_github_remote():
    from factory.publish import PublishError

    with pytest.raises(PublishError):
        gh_branch_protection("/tmp/not-github", "main", _run=lambda *a, **k: _gh(0))
