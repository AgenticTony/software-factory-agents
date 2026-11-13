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
from _support.f63 import ledger_approve

from factory.contract import SCHEMA_DIR, ContractError, acceptance_hash, load_profile
from factory.mergeorder import (
    CIState,
    MergeOrderError,
    Protection,
    admission,
    classify_protection,
    gh_branch_protection,
)
from factory.orchestrator import run_loop, run_once
from factory.progress import ProgressLog
from factory.publish import PullRequest
from factory.runs import RunError
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
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 30},
        "scope": {"paths": ["impl.py", "tests/test_feature.py"]},
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
        # F-63: record the fixture's approvals before each run (tests
        # mutate the profile/tasks between runs; approval precedes the
        # run, as in production).
        ledger_approve(self.root, self.queue_path)
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", _protection(PROTECTED_STRICT))
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
        kwargs.setdefault("protection", _protection(PROTECTED_STRICT))
        kwargs.setdefault("pr_state", lambda url: "open")
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
    new legal edge). Up to date → no re-validation. Pass → still ready.

    A second ready task rides along: the sweep runs on the way to the
    next pick (an idle queue starts nothing and judges nothing — round-2
    finding 8), and the open PR then holds the concurrency slot, so the
    pass/no-op scenarios end in concurrency_wait, not idle."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    branch = env.task("T-1")["links"]["branch"]
    assert env.task("T-1")["status"] == "in_review"

    # Up to date: the sweep is a no-op — no new attempt, no new log entry.
    before_attempts = len(env.task("T-1")["attempts"])
    before_entries = len(env.log().entries())
    assert env.run(Seats()).outcome == "concurrency_wait"  # T-2 waits on T-1
    assert len(env.task("T-1")["attempts"]) == before_attempts
    assert len(env.log().entries()) == before_entries

    # Stale but clean (an unrelated remote advance): re-validation runs,
    # passes on the merged tree, and the PR stays ready for merge.
    env.advance_remote_main(None, extra_file="remote_only.txt")
    result = env.run(Seats())
    assert result.outcome == "concurrency_wait"
    task = env.task("T-1")
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
    task = env.task("T-1")
    assert task["status"] == "needs_human"
    last = task["attempts"][-1]
    assert last["escalation"] == "stale_base"
    assert last["outcome"] == "escalated"
    assert "conflict" in (last.get("note") or "").lower()
    assert task["links"]["branch"] == branch  # the PR is still named

    # The escalation exists in the schema enum (red-first) and the
    # in_review -> needs_human edge is legal (lateral targets unchanged).
    schema = json.loads((SCHEMA_DIR / "tasks.schema.json").read_text())
    assert "stale_base" in schema["$defs"]["escalation"]["enum"]
    assert ("in_review", "needs_human") in ALLOWED_TRANSITIONS
    assert ("in_review", "awaiting_owner") not in ALLOWED_TRANSITIONS


def test_stale_revalidation_catches_a_broken_merge(tmp_path: Path):
    """A stale merge that *conflicts-free* merges but fails the gates also
    escalates stale_base: the re-validation runs the real L0/L1, not just
    git-merge."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
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
    assert env.task("T-1")["status"] == "needs_human"


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


# --------------------------------------------- owner review round (10 findings) --


def _force_in_review(env: Env, task_id: str = "T-1", *, branch: str,
                     pr_url: str = "https://github.com/fixture/fixture/pull/9") -> None:
    """Hand-craft a task sitting in_review with PR + branch links."""
    q = TaskQueue(env.queue_path)
    q.transition(task_id, "in_progress")
    q.transition(task_id, "in_review")
    q.get(task_id)["links"] = {"pr": pr_url, "branch": branch}
    q.save()


def _push_remote_branch(env: Env, branch: str, impl_body: str) -> None:
    scratch = env.tmp / f"branch-{branch.replace('/', '-')}"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(scratch)], check=True)
    (scratch / "impl.py").write_text(impl_body)
    _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
    _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "pr branch")
    _git(scratch, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")


def test_review1_a_merged_pr_is_never_revalidated(tmp_path: Path):
    """Finding 1: a squash-merged PR must be stamped and skipped by the
    sweep, never escalated stale_base — the contract tests already pass
    on the new main, so re-validation would fail. On this no-deploy
    fixture profile the stamp also closes the task (the fix): done in
    the same run, still with no attempt spent."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="merged the pr on github")
    before = len(env.task("T-1")["attempts"])
    entries = len(env.log().entries())

    result = env.run(Seats(), pr_state=lambda url: "merged")

    # The merged PR is skipped AND frees its slot: the next task runs.
    assert result.outcome == "pr_opened"
    task = env.task("T-1")
    assert task["status"] == "done"        # stamped, then closed (no deploy)
    assert len(task["attempts"]) == before          # no stale_base attempt
    assert len(env.log().entries()) == entries + 1  # only the close evidence


def test_review2_a_deleted_branch_after_merge_does_not_wedge(tmp_path: Path):
    """Finding 2: with auto-delete head branches, the PR's branch is gone
    from the remote while the task sits in_review — run must not raise.
    The merged stamp (and the close it triggers) happens before any
    branch fetch, so the missing branch is never fetched."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    _force_in_review(env, branch="factory/t-1-gone")
    # The branch is deliberately never pushed: fetching it would fail.

    result = env.run(Seats(), pr_state=lambda url: "merged")

    assert result.outcome == "pr_opened"          # the next task is not wedged
    assert env.task("T-1")["status"] == "done"    # stamped + closed, no fetch


def test_review3_the_open_pr_limit_counts_open_prs(tmp_path: Path):
    """Finding 3: an escalated task's PR stays open on GitHub — it must
    still count against the limit, or the next run opens a second PR."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    result = env.run(Seats())  # T-1 goes stale -> needs_human, PR stays open
    assert result.outcome == "needs_human"
    assert result.escalation == "stale_base"
    assert env.task("T-1")["status"] == "needs_human"

    # T-2 is ready, but T-1's PR is still OPEN on GitHub: the limit holds.
    seats = Seats()
    result = env.run(seats)
    assert result.outcome == "concurrency_wait"
    assert seats.calls == []
    assert env.task("T-2")["status"] == "ready"

    # Once GitHub reports that PR merged, the slot frees.
    result = env.run(Seats(), pr_state=lambda url: "merged")
    assert result.outcome == "pr_opened"


def test_review4_revalidation_is_cached_per_base(tmp_path: Path):
    """Finding 4: an unchanged (pr branch, default head) pair must not be
    re-validated — no second full L0/L1, no duplicate log entry."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main(None, extra_file="remote_only.txt")
    assert env.run(Seats()).outcome == "concurrency_wait"  # re-validated, T-2 waits
    task = env.task("T-1")
    cached = task["links"].get("revalidated")
    assert cached and cached["default"] == _git(env.remote, "rev-parse", "refs/heads/main")
    entries = len(env.log().entries())

    result = env.run(Seats())  # main has not moved: nothing to redo

    assert result.outcome == "concurrency_wait"
    assert len(env.log().entries()) == entries
    assert env.task("T-1")["links"]["revalidated"] == cached


def test_review5_the_peak_window_skips_the_sweep(tmp_path: Path):
    """Round-1 finding 5 (kept meaningful under round-2 finding 8's
    ordering): with a task pickable, the sandboxed gate work must not run
    during the peak window — the run returns peak_window before any
    re-validation."""
    from datetime import datetime

    monday_0930 = datetime(2026, 10, 19, 9, 30, tzinfo=STOCKHOLM)
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main(None, extra_file="remote_only.txt")
    entries = len(env.log().entries())

    result = env.run(Seats(), now=lambda: monday_0930)

    assert result.outcome == "peak_window"
    assert env.task("T-2")["status"] == "ready"  # untouched, no session
    assert len(env.log().entries()) == entries   # the sweep never ran


def test_review6_merge_failure_classification(tmp_path: Path):
    """Round-1 finding 6, superseded by round-2 findings 2+3: conflicts
    are detected STRUCTURALLY (git diff --diff-filter=U — no locale can
    change it) and the merge subprocess runs under LC_ALL=C so any quoted
    git text is deterministic. A conflicted repo lists its paths; a clean
    tree lists none (an infrastructure failure would leave none either,
    and raises RunError instead of stale_base)."""
    from factory.orchestrator import _conflict_paths

    repo = tmp_path / "conflicted"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-q",
         "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-qb", "other")
    (repo / "f").write_text("pr\n")
    _git(repo, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
    _git(repo, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "pr")
    _git(repo, "checkout", "-q", "main")
    (repo / "f").write_text("main\n")
    _git(repo, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
    _git(repo, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "moved")
    # Identity + LC_ALL=C mirror the sweep's merge invocation: a machine
    # without an ambient git identity (CI's fresh runners) refuses the
    # merge with rc=128 "Committer identity unknown" BEFORE conflicting,
    # leaving no unmerged paths — the production merge always carries
    # _IDENTITY, and so does the fixture.
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=x@x", "-c", "user.name=x",
         "merge", "--no-edit", "other"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "LC_ALL": "C", "LANG": "C"})
    assert proc.returncode == 1  # a conflict, not an identity refusal
    assert _conflict_paths(repo) == ["f"]


def test_review7_escalation_survives_a_task_without_attempts(tmp_path: Path):
    """Finding 7: an in_review task with links but no logged attempts
    (hand-reconciled or migrated state) must escalate cleanly. A second
    ready task keeps the sweep reachable (round-2 finding 8: an idle
    queue starts nothing and judges nothing)."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    branch = "factory/t-1-manual"
    _push_remote_branch(env, branch, "def answer():\n    return 42\n")
    _force_in_review(env, branch=branch)
    assert env.task()["attempts"] == []
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")

    result = env.run(Seats())

    assert result.outcome == "needs_human"
    assert result.escalation == "stale_base"
    attempt = env.task("T-1")["attempts"][-1]
    assert attempt["slice_id"] == "T-1"   # no IndexError; falls back to the id


def test_review9_not_protected_with_declaration_names_the_excuse(tmp_path):
    """Finding 9: a 404 with `branch_protection: unavailable` declared must
    say the declaration was considered and does not excuse the repo."""
    profile = {
        "repo": {"path": "/tmp/x", "default_branch": "main",
                 "remote": "https://github.com/o/r.git",
                 "branch_protection": "unavailable"}
    }
    with pytest.raises(MergeOrderError, match="does not excuse"):
        admission(profile, _protection(
            Protection(checked=False, strict=False,
                       detail="branch protection is off (404 not protected)")))


def test_review10_a_404_names_the_other_causes():
    """Finding 10: 404 also means a missing repo/branch or a token without
    access — the message must not send the owner to enable protection that
    already exists."""
    got = classify_protection(1, "", "gh: Not Found (HTTP 404)")
    assert not got.checked and not got.plan_limit
    assert "does not exist" in got.detail or "token" in got.detail


# --------------------------------------- owner review round 2 (8 findings) --


def test_review11_a_fix_round_waits_for_its_old_pr_to_close(tmp_path: Path):
    """Round-4 finding 4 (supersedes round-2 finding 1's exclusion): the
    picked task's own PR counts while GitHub reports it open — excluding
    it let the fix round open a second PR and orphan the rejected one
    (two open factory PRs is exactly what AC-2 forbids). The wait names
    the PR (round-4 finding 3); close it and the fix round runs."""
    env = Env(tmp_path)
    seats = Seats()
    assert env.run(seats).outcome == "pr_opened"
    worked = len(seats.calls)

    # You reject the PR: the task returns for a fix round, links.pr kept,
    # the PR itself still open on GitHub.
    q = TaskQueue(env.queue_path)
    q.transition("T-1", "in_progress")
    q.transition("T-1", "ready")

    blocked = env.run(seats)  # default fake: the old PR is still open

    assert blocked.outcome == "concurrency_wait"
    assert len(seats.calls) == worked  # no session while the old PR holds the slot
    assert len(env.prs) == 1
    assert env.task()["links"]["pr"] in blocked.open_prs  # the stall names it

    result = env.run(seats, pr_state=lambda url: "closed")
    assert result.outcome == "pr_opened"
    assert len(env.prs) == 2  # the fix round's PR; the old one is closed


def test_review12_conflicts_are_structural_locale_pinned_and_named(
        tmp_path: Path, monkeypatch):
    """Round-2 findings 2+3 (blocking + note): conflict detection reads
    git's unmerged-file index (no locale can change it), the merge runs
    under LC_ALL=C, and the stale_base note names the conflicting paths
    instead of quoting an empty stderr.

    F-45: the merge now runs through factory.publish.remote_call, so the
    pin observes the subprocess boundary that wrapper drives (the same
    env dict the process actually runs under)."""
    from factory import publish as publish_mod

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")

    merge_envs: list = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        if "merge" in args and "--no-edit" in args:
            merge_envs.append(kwargs.get("env"))
        return real_run(args, **kwargs)

    monkeypatch.setattr(publish_mod.subprocess, "run", spy)

    result = env.run(Seats())

    assert result.outcome == "needs_human"
    assert result.escalation == "stale_base"
    assert len(merge_envs) == 1
    assert merge_envs[0] and merge_envs[0].get("LC_ALL") == "C"
    note = env.task("T-1")["attempts"][-1]["note"]
    assert "impl.py" in note          # the conflicting file is named
    assert "()" not in note           # never an empty parenthetical


def test_review13_an_unresolvable_pr_link_does_not_abort_the_run(tmp_path: Path):
    """Round-2 finding 4 (no-crash half; the limit half moved to
    test_review20 under round-3 finding 2): one rotten PR URL on an old
    task must stop neither the run nor the queue — nothing raises, exit
    is a clean wait, not 2."""
    from factory.publish import PublishError

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # T-1 escalated, PR open

    # The escalated task's link rots (repo transferred/renamed).
    q = TaskQueue(env.queue_path)
    q.get("T-1")["links"]["pr"] = "https://github.com/gone/fixture/pull/1"
    q.save()

    def flaky(url):
        if "gone" in url:
            raise PublishError("gh pr view failed: HTTP 404")
        return "open"

    result = env.run(Seats(), pr_state=flaky)  # must not raise
    assert result.outcome in ("concurrency_wait", "pr_opened")
    assert env.task("T-2")["status"] in ("ready", "in_review")


def test_review14_a_closed_pr_escalates_pr_closed(tmp_path: Path):
    """Round-2 finding 5: a PR closed without merging must surface its
    task (needs_human / pr_closed), never leave it silently in_review."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"

    result = env.run(Seats(), pr_state=lambda url: "closed")

    assert result.outcome == "needs_human"
    assert result.escalation == "pr_closed"
    task = env.task("T-1")
    assert task["status"] == "needs_human"
    last = task["attempts"][-1]
    assert last["escalation"] == "pr_closed"
    assert last["outcome"] == "escalated"
    schema = json.loads((SCHEMA_DIR / "tasks.schema.json").read_text())
    assert "pr_closed" in schema["$defs"]["escalation"]["enum"]


def test_review15_pr_state_is_resolved_once_per_run(tmp_path: Path):
    """Round-2 finding 6: the sweep and the limit share one gh lookup per
    PR URL per run — no double subprocess round-trips."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main(None, extra_file="remote_only.txt")

    calls: dict[str, int] = {}

    def counting(url):
        calls[url] = calls.get(url, 0) + 1
        return "open"

    result = env.run(Seats(), pr_state=counting)

    assert result.outcome == "concurrency_wait"
    assert calls and set(calls.values()) == {1}   # each URL exactly once


def test_review16_a_cache_hit_needs_no_clone(tmp_path: Path, monkeypatch):
    """Round-2 finding 7: ls-remote decides the revalidated-cache hit
    before any scratch clone or full fetch."""
    import factory.orchestrator as orch

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main(None, extra_file="remote_only.txt")
    assert env.run(Seats()).outcome == "concurrency_wait"  # re-validated + cached

    real_create_run = orch.create_run
    made: list = []

    def counting(project, source, **kwargs):
        made.append(project)
        return real_create_run(project, source, **kwargs)

    monkeypatch.setattr(orch, "create_run", counting)
    result = env.run(Seats())  # unchanged main: the cache must decide

    assert result.outcome == "concurrency_wait"
    assert made == []  # no clone, no fetch — ls-remote answered first


def test_review17_an_idle_queue_in_the_peak_window_returns_idle(tmp_path: Path):
    """Round-2 finding 8: an empty queue during the peak window reports
    idle / queue_empty, not peak_window — a drained queue is 'done',
    whatever the clock says."""
    from datetime import datetime

    monday_0930 = datetime(2026, 10, 19, 9, 30, tzinfo=STOCKHOLM)
    env = Env(tmp_path)
    assert env.run(Seats()).outcome == "pr_opened"  # queue now drained

    result = env.run(Seats(), now=lambda: monday_0930)
    assert result.outcome == "idle"

    from factory.orchestrator import run_loop

    loop = run_loop(
        env.queue_path, factory_root=env.root, session_runner=Seats(),
        open_pr=env.open_pr, now=lambda: monday_0930,
        protection=_protection(PROTECTED_STRICT), pr_state=lambda u: "open",
    )
    assert loop.stop_reason == "queue_empty"


# --------------------------------------- owner review round 3 (8 findings) --


def test_review18_the_sweep_runs_with_no_pickable_task(tmp_path: Path):
    """Round-3 finding 1: a run with nothing to pick must still re-validate
    open PRs (AC-3's 'every run') — the F-27-waits-on-F-26 shape."""
    env = Env(tmp_path)  # one task; after its PR the queue has no pick
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")

    result = env.run(Seats())  # pick is None — the sweep must still run

    assert result.outcome == "needs_human"
    assert result.escalation == "stale_base"
    assert env.task()["status"] == "needs_human"


def test_review19_a_closed_pr_surfaces_with_no_pickable_task(tmp_path: Path):
    """Round-3 finding 1, closed half: pr_closed must surface even when no
    task is pickable."""
    env = Env(tmp_path)
    assert env.run(Seats()).outcome == "pr_opened"

    result = env.run(Seats(), pr_state=lambda url: "closed")

    assert result.outcome == "needs_human"
    assert result.escalation == "pr_closed"
    assert env.task()["status"] == "needs_human"


def test_review20_an_unknown_pr_state_fails_closed_on_the_limit(
        tmp_path: Path):
    """Round-3 finding 2 (supersedes round-2 finding 4's 'runs anyway'
    half, keeping its no-crash half): a gh lookup that fails is presumed
    OPEN for the concurrency limit — fail closed — while the run itself
    still refuses nothing and raises nothing."""
    from factory.publish import PublishError

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # T-1 escalated, PR open
    q = TaskQueue(env.queue_path)
    q.get("T-1")["links"]["pr"] = "https://github.com/gone/fixture/pull/1"
    q.save()

    def flaky(url):
        if "gone" in url:
            raise PublishError("gh pr view failed: secondary rate limit")
        return "open"

    seats = Seats()
    result = env.run(seats, pr_state=flaky)

    # No crash (round-2 finding 4), but the unknown PR holds its slot
    # (round-3 finding 2): the picked task waits, a second PR never opens.
    assert result.outcome == "concurrency_wait"
    assert seats.calls == []
    assert env.task("T-2")["status"] == "ready"


def test_review21_network_git_never_prompts_and_is_bounded(
        tmp_path: Path, monkeypatch):
    """Round-3 finding 3: every remote git call (ls-remote, both fetches,
    the merge) runs with GIT_TERMINAL_PROMPT=0 and a timeout — a private
    remote with an expired credential helper must fail, not hang the
    lease on a username prompt.

    F-45: the sweep's remote calls (and publish-side push/ls-remote/gh)
    now all run through factory.publish.remote_call, so the pin observes
    the subprocess boundary that ONE wrapper drives — the same env and
    timeout the processes actually run under."""
    from factory import publish as publish_mod

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main(None, extra_file="remote_only.txt")

    seen: list[tuple[tuple, dict]] = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        joined = tuple(str(a) for a in args)
        if any(k in joined for k in ("ls-remote", "fetch", "merge")):
            seen.append((joined, kwargs))
        return real_run(args, **kwargs)

    monkeypatch.setattr(publish_mod.subprocess, "run", spy)

    result = env.run(Seats())  # drives ls-remote + fetches (+ maybe merge)

    assert result.outcome in ("concurrency_wait", "needs_human")
    remote_calls = [(a, kw) for a, kw in seen
                    if "ls-remote" in a or ("fetch" in a and "clone" not in a)]
    assert remote_calls, "expected remote git calls in the sweep"
    for args, kwargs in remote_calls:
        env_kw = kwargs.get("env") or {}
        assert env_kw.get("GIT_TERMINAL_PROMPT") == "0"
        assert kwargs.get("timeout"), f"no timeout on {args}"


def test_review22_pr_closed_run_ids_are_unique(tmp_path: Path):
    """Round-3 finding 4: pr_closed escalations carry real generated run
    ids — no shared literal 'pr-closed' across tasks."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"   # T-1's PR

    closed = lambda url: "closed"  # noqa: E731
    first = env.run(Seats(), pr_state=closed)  # T-1 closed → escalated
    assert first.escalation == "pr_closed"
    assert env.run(Seats(), pr_state=closed).outcome == "pr_opened"  # T-2's
    assert env.task("T-1")["status"] == "needs_human"
    assert env.task("T-2")["status"] == "in_review"

    second = env.run(Seats(), pr_state=closed)
    assert second.escalation == "pr_closed"

    run_id_re = __import__("re").compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
    ids = [env.task(t)["attempts"][-1]["run_id"] for t in ("T-1", "T-2")]
    assert ids[0] != ids[1]
    for rid in ids:
        assert run_id_re.match(rid), rid


def test_review23_an_up_to_date_pr_is_cached_too(tmp_path: Path, monkeypatch):
    """Round-3 finding 6: the up-to-date path also stamps
    links.revalidated, so the ls-remote pre-check skips the common
    no-clone case on every later tick."""
    import factory.orchestrator as orch

    env = Env(tmp_path)  # single task: the sweep runs with no pick
    assert env.run(Seats()).outcome == "pr_opened"

    result = env.run(Seats())  # up to date — must stamp, no escalation
    assert result.outcome == "idle"
    cached = env.task()["links"].get("revalidated")
    assert cached, "the up-to-date path must stamp the cache"
    assert cached["pr"] == _git(env.remote, "rev-parse",
                                f"refs/heads/{env.task()['links']['branch']}")
    assert cached["default"] == _git(env.remote, "rev-parse", "refs/heads/main")

    real_create_run = orch.create_run
    made: list = []

    def counting(project, source, **kwargs):
        made.append(project)
        return real_create_run(project, source, **kwargs)

    monkeypatch.setattr(orch, "create_run", counting)
    assert env.run(Seats()).outcome == "idle"  # unchanged heads
    assert made == []  # ls-remote answered; no clone, no fetch


def test_review24_an_idle_tick_only_queries_open_prs(tmp_path: Path):
    """Round-3 finding 7: with nothing to pick, the run resolves only the
    in_review links — a build-up of needs_human tasks with old PR links
    adds no gh round-trips."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"

    # T-2 becomes a needs_human task holding an old PR link (hand-set:
    # the transition path from ready would be a no-op edge error; only
    # the sweep's gh behavior is under test here).
    q = TaskQueue(env.queue_path)
    q.get("T-2")["status"] = "needs_human"
    q.get("T-2")["links"] = {"pr": "https://github.com/fixture/fixture/pull/99"}
    q.save()

    calls: list[str] = []

    def counting(url):
        calls.append(url)
        return "open"

    result = env.run(Seats(), pr_state=counting)  # idle tick (no pick)

    assert result.outcome == "idle"
    t1_pr = env.task("T-1")["links"]["pr"]
    assert calls == [t1_pr]  # only the open PR's link, never T-2's


def test_review25_recovery_edges_are_legal_and_documented(tmp_path: Path):
    """Round-3 finding 8 (rewritten for round-4 f2's gate and round-5 f3's
    ready target): the recovery edges are legal for an escalated task
    holding a PR link — applied through the queue, which enforces the
    gate — and TASK-CONTRACT carries both rows."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # stale_base, PR linked
    TaskQueue(env.queue_path).transition("T-1", "ready")  # eligible (f3 target)

    env2 = Env(tmp_path / "b", tasks=[_task("T-1")])
    assert env2.run(Seats()).outcome == "pr_opened"
    assert env2.run(Seats(), pr_state=lambda url: "closed").escalation \
        == "pr_closed"
    TaskQueue(env2.queue_path).transition("T-1", "in_review")  # eligible

    doc = (REPO_ROOT / "TASK-CONTRACT.md").read_text()
    assert "`needs_human → in_review`" in doc
    assert "`needs_human → ready`" in doc


# --------------------------- round 4 (owner review) ------------------------


def test_review26_a_reopened_pr_is_recovered_by_the_sweep(tmp_path: Path):
    """Round-4 finding 1: the pr_closed remedy is reachable through the
    tooling — no hand-edit, no reconcile (which skips non-draft tasks).
    You re-open the PR on GitHub; the next run's sweep moves the task
    back to in_review and says so in the progress log."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    assert env.run(Seats()).outcome == "pr_opened"
    assert env.run(Seats(), pr_state=lambda url: "closed").escalation \
        == "pr_closed"

    result = env.run(Seats())  # you re-opened it; the default fake: open

    assert result.outcome == "idle"  # nothing to pick; the recovery is the story
    assert env.task()["status"] == "in_review"
    assert any(e.get("outcome") == "recovered" for e in env.log().entries())


def test_review27_a_rejected_stale_pr_returns_for_a_fix_round(tmp_path: Path):
    """Round-4 f1 + round-5 f3: the stale_base fix-round remedy is the sweep
    reading your rejection (the closed PR), and it re-queues the task
    READY directly — no lease-less in_progress hop for the next run's
    reclaim to clean up. The round re-runs from the current default
    head; no owner branch update is promised or used."""
    env = Env(tmp_path, tasks=[_task("T-1")])  # 3 attempts: run + escalation + fix round
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # stale_base; PR open
    old_pr = env.task()["links"]["pr"]

    rejected = env.run(Seats(), pr_state=lambda url: "closed")

    assert rejected.outcome == "idle"  # pick ran before the sweep: none
    assert env.task()["status"] == "ready"  # round-5 f3: ready, not in_progress
    assert any(e.get("outcome") == "recovered" for e in env.log().entries())

    result = env.run(Seats(), pr_state=lambda url: "closed")  # no reclaim hop
    assert result.outcome == "pr_opened"
    assert len(env.prs) == 2
    assert env.task()["links"]["pr"] != old_pr


def test_review28_recovery_edges_are_gated_to_f26_escalations(tmp_path: Path):
    """Round-4 finding 2: the recovery edges left the general table —
    check_transition refuses them — and the queue applies them only to a
    stale_base/pr_closed escalation that still holds its PR link. A
    credential_added task with a PR link must not exit through them."""
    from factory.state import TransitionError, check_transition

    with pytest.raises(TransitionError):
        check_transition("needs_human", "in_review")
    with pytest.raises(TransitionError):
        check_transition("needs_human", "in_progress")  # left entirely in round 5
    with pytest.raises(TransitionError):
        check_transition("needs_human", "ready")  # gated recovery edge

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # T-1 stale_base

    # A credential_added escalation holding a PR link: gated out (its PR
    # was never validated — in_review would assert a pass that never
    # happened).
    q = TaskQueue(env.queue_path)
    t2 = q.get("T-2")
    t2["status"] = "needs_human"
    t2["links"] = {"pr": "https://github.com/fixture/fixture/pull/7"}
    t2["attempts"] = [{
        "run_id": "r1", "slice_id": "T-2", "started": "2026-10-18T10:00:00Z",
        "outcome": "escalated", "escalation": "credential_added",
        "prompt_versions": {}, "skills": [],
    }]
    q.save()
    for target in ("in_review", "in_progress"):
        with pytest.raises(TransitionError):
            TaskQueue(env.queue_path).transition("T-2", target)

    TaskQueue(env.queue_path).transition("T-1", "ready")  # eligible (f3)
    q = TaskQueue(env.queue_path)
    q.get("T-1")["status"] = "needs_human"  # back for the no-link half
    q.get("T-1")["links"] = {}
    q.save()
    with pytest.raises(TransitionError):
        TaskQueue(env.queue_path).transition("T-1", "in_review")


def test_review29_a_fail_closed_stall_names_its_cause(tmp_path: Path):
    """Round-4 finding 3: a fail-closed stall (an unresolvable PR link)
    still exits 0 as concurrency_wait, but the RunResult and the CLI line
    name the link — scheduled ticks can't look healthy while nothing
    builds."""
    from factory.cli import _run_extra
    from factory.publish import PublishError

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"
    q = TaskQueue(env.queue_path)
    rotten = "https://github.com/gone/fixture/pull/1"
    q.get("T-1")["links"]["pr"] = rotten
    q.save()

    def flaky(url):
        if "gone" in url:
            raise PublishError("gh pr view failed: secondary rate limit")
        return "open"

    result = env.run(Seats(), pr_state=flaky)

    assert result.outcome == "concurrency_wait"
    assert result.unresolved_prs == [rotten]
    assert rotten in _run_extra(result)


def test_review30_timeouts_surface_as_run_errors(tmp_path: Path, monkeypatch):
    """Round-4 finding 5: the sweep's merge and the build's initial fetch
    wrap TimeoutExpired into RunError — a slow remote is the usual
    exit-2 message, never a Python traceback.

    F-45: both calls run through factory.publish.remote_call now; the
    timeout is hung at the subprocess boundary that wrapper drives and
    must still surface as the same RunError."""
    from factory import publish as publish_mod
    from factory.runs import RunError

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")

    real_run = subprocess.run

    def hang_merge(args, **kwargs):
        if "merge" in args and "--no-edit" in args:
            raise subprocess.TimeoutExpired(args, 300)
        return real_run(args, **kwargs)

    monkeypatch.setattr(publish_mod.subprocess, "run", hang_merge)
    with pytest.raises(RunError, match="timed out"):
        env.run(Seats())
    monkeypatch.undo()

    # Half 2: the run's initial fetch of the remote default branch
    # (_build_on_remote_default — blob-filtered, unlike the sweep's full
    # fetches).
    env2 = Env(tmp_path / "b", tasks=[_task("T-1")])

    def hang_build_fetch(args, **kwargs):
        if "fetch" in args and "--filter=blob:none" in args:
            raise subprocess.TimeoutExpired(args, 300)
        return real_run(args, **kwargs)

    monkeypatch.setattr(publish_mod.subprocess, "run", hang_build_fetch)
    with pytest.raises(RunError, match="timed out"):
        env2.run(Seats())


def test_review31_run_ids_come_from_the_public_api():
    """Round-4 finding 6: runs.py exposes new_run_id() as the seam for
    session-less records (its docstring owns the no-run-dir fact), and
    the orchestrator no longer imports the private _new_run_id."""
    import inspect
    import re

    from factory import runs

    assert not hasattr(runs, "_new_run_id")
    assert re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$", runs.new_run_id())
    assert "no run dir" in runs.new_run_id.__doc__
    src = inspect.getsource(__import__("factory.orchestrator",
                                       fromlist=["orchestrator"]))
    assert "_new_run_id" not in src


def test_review32_the_documented_remedies_are_the_sweeps():
    """Round-4 findings 1+7: TASK-CONTRACT's recovery rows name the sweep
    as their writer (reconcile never applies them), and the stale_base
    remedy no longer tells the owner to update a branch the fix round
    would discard."""
    doc = (REPO_ROOT / "TASK-CONTRACT.md").read_text()
    assert "replayed via reconcile" not in doc
    assert "updates the branch" not in doc
    assert "is not reused" in doc
    assert "the next run's sweep moves the task back" in doc


# --------------------------- round 5 (owner review) ------------------------


def test_review33_sweep_escalations_do_not_burn_the_budget(tmp_path: Path):
    """Round-5 finding 1: stale_base/pr_closed attempts are session-less
    sweep records, not spent work — the recovery fix round starts with
    its slot unspent. max_attempts=2: pass (1) + stale_base escalation
    must still leave the fix round attempt 2, not attempt 3."""
    env = Env(tmp_path, tasks=[_task(
        "T-1", budget={"size": "S", "max_attempts": 2, "max_turns": 20,
                       "wall_clock_min": 30})])
    assert env.run(Seats()).outcome == "pr_opened"        # attempt 1 (pass)
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"      # sweep attempt 2
    # You rejected the PR; queue the fix round through the gated edge
    # (what the sweep applies on a closed stale_base PR — isolated here
    # so only the budget is under test).
    TaskQueue(env.queue_path).transition("T-1", "ready")

    result = env.run(Seats(), pr_state=lambda url: "closed")  # old PR closed

    assert result.outcome == "pr_opened"  # red: attempts_exhausted before


def test_review34_a_pr_closed_task_can_be_recovered_for_a_fix_round(
        tmp_path: Path):
    """Round-5 finding 2: closing a healthy in_review PR escalates
    pr_closed, and GitHub then has no signal left to mean 'fix round'
    (the PR was already closed at escalation) — `factory recover` is the
    owner's explicit fix-round word, and it refuses anything not gated
    eligible."""
    from factory.approve import ApprovalError, recover_task
    from factory.cli import main

    closed = lambda url: "closed"  # noqa: E731  (recover's round-6 gate)
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    assert env.run(Seats(), pr_state=lambda url: "closed").escalation \
        == "pr_closed"

    # A credential_added escalation with a PR link: recover refuses.
    q = TaskQueue(env.queue_path)
    t2 = q.get("T-2")
    t2["status"] = "needs_human"
    t2["links"] = {"pr": "https://github.com/fixture/fixture/pull/7"}
    t2["attempts"] = [{
        "run_id": "r1", "slice_id": "T-2", "started": "2026-10-18T10:00:00Z",
        "outcome": "escalated", "escalation": "credential_added",
        "prompt_versions": {}, "skills": [],
    }]
    q.save()
    with pytest.raises(ApprovalError):
        recover_task(TaskQueue(env.queue_path), "T-2", assume_yes=True,
                     pr_state=closed)
    assert main(["recover", "T-2", "--queue", str(env.queue_path),
                 "--yes"]) == 1

    import factory.approve as approve_mod
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(approve_mod, "gh_pr_state", closed)
    try:
        assert main(["recover", "T-1", "--queue", str(env.queue_path),
                     "--yes"]) == 0
    finally:
        monkeypatch.undo()
    assert env.task("T-1")["status"] == "ready"

    result = env.run(Seats(), pr_state=lambda url: "closed")
    assert result.outcome == "pr_opened"  # the fix round actually runs
    assert len(env.prs) == 2
    doc = (REPO_ROOT / "TASK-CONTRACT.md").read_text()
    assert "factory recover" in doc  # the remedy is documented


# --- F-31 run blocker: credential_added re-queues through recover ----------


def _park_escalated(env, task_id: str, escalation: str) -> None:
    """Move a fixture task to needs_human with one escalated attempt."""
    q = TaskQueue(env.queue_path)
    task = q.get(task_id)
    task["status"] = "needs_human"
    task["attempts"] = [{
        "run_id": "r1", "slice_id": task_id, "started": "2026-10-29T10:00:00Z",
        "ended": "2026-10-29T11:00:00Z", "outcome": "escalated",
        "escalation": escalation, "prompt_versions": {}, "skills": [],
    }]
    q.save()


def test_credential_added_requeues_with_evidence(tmp_path: Path):
    """TASK-CONTRACT §credential_added says "a human confirms and
    re-queues" — but recover was gated to stale_base/pr_closed, so F-31
    sat in needs_human with no exit. The owner's word is
    ``factory recover <id> --evidence …`` + an interactive y/N: it writes
    the gated needs_human → ready edge, resets nothing (the remaining
    attempt budget is the human's call, made with the evidence in hand),
    and an open dependency routes to blocked exactly like approval."""
    from factory.approve import recover_task

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2", depends_on=["T-3"]),
                               _task("T-3")])
    for task_id in ("T-1", "T-2"):
        _park_escalated(env, task_id, "credential_added")

    said = {}

    def echo(text: str) -> None:
        said["summary"] = said.get("summary", "") + text + "\n"

    confirm = lambda question: said.update(prompt=question) or True  # noqa: E731
    assert recover_task(TaskQueue(env.queue_path), "T-1",
                        evidence="test fixture, not a real credential",
                        confirm=confirm, echo=echo) == "ready"
    assert env.task("T-1")["status"] == "ready"
    assert len(env.task("T-1")["attempts"]) == 1, "recover must not reset attempts"
    assert "credential_added" in said["summary"]
    assert "test fixture, not a real credential" in said["summary"]
    # a dependency that is open routes to blocked, same as approval
    assert recover_task(TaskQueue(env.queue_path), "T-2",
                        evidence="rotated",
                        confirm=lambda question: True) == "blocked"
    assert env.task("T-2")["status"] == "blocked"


def test_credential_added_recover_refusals(tmp_path: Path):
    """The credential edge is deliberately human-paced: no --yes (the
    point is that a human looked), evidence is required and must say
    something, a declined prompt writes nothing — and every other
    needs_human reason (protected paths, risk paths) still refuses:
    only credential_added and attempts_exhausted (its own gated reset,
    test_attempts_exhausted_recover_refusals) have an owner word."""
    from factory.approve import ApprovalDeclined, ApprovalError, recover_task
    from factory.cli import main

    others = [("T-5", "protected_path_touched"),
              ("T-6", "risk_path_touched")]
    # fresh tasks per escalation: attempts are append-only, so each
    # refusal parks its own task
    env = Env(tmp_path, tasks=[_task("T-1")] + [_task(tid) for tid, _ in others])
    _park_escalated(env, "T-1", "credential_added")
    queue = lambda: TaskQueue(env.queue_path)  # noqa: E731

    with pytest.raises(ApprovalError, match="evidence"):
        recover_task(queue(), "T-1", confirm=lambda question: True)
    with pytest.raises(ApprovalError, match="evidence"):
        recover_task(queue(), "T-1", evidence="   ",
                     confirm=lambda question: True)
    with pytest.raises(ApprovalError, match="interactive"):
        recover_task(queue(), "T-1", assume_yes=True,
                     evidence="test fixture, not a real credential")
    assert main(["recover", "T-1", "--queue", str(env.queue_path), "--yes",
                 "--evidence", "test fixture"]) == 1
    assert main(["recover", "T-1", "--queue", str(env.queue_path)]) == 1
    with pytest.raises(ApprovalDeclined):
        recover_task(queue(), "T-1", evidence="rotated",
                     confirm=lambda question: False)
    assert env.task("T-1")["status"] == "needs_human"

    for task_id, escalation in others:
        _park_escalated(env, task_id, escalation)
        with pytest.raises(ApprovalError, match="not recoverable"):
            recover_task(queue(), task_id, evidence="looks fine",
                         confirm=lambda question: True)
        assert env.task(task_id)["status"] == "needs_human", task_id


def test_credential_added_recover_cli_confirm_and_f26_path_unchanged(
        tmp_path: Path, monkeypatch):
    """End to end at the CLI: the y/N prompt is answered through stdin
    (EOF declines), and the pre-existing stale_base/pr_closed flow —
    closed-PR check, --yes allowed there — is untouched by the new
    credential branch."""
    import io

    import factory.approve as approve_mod
    from factory.cli import main

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    _park_escalated(env, "T-1", "credential_added")
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert main(["recover", "T-1", "--queue", str(env.queue_path),
                 "--evidence", "test fixture, not a real credential"]) == 0
    assert env.task("T-1")["status"] == "ready"

    # the F-26 path still checks GitHub for a closed PR and takes --yes
    _park_escalated(env, "T-2", "pr_closed")
    q = TaskQueue(env.queue_path)
    q.get("T-2")["links"] = {"pr": "https://github.com/fixture/fixture/pull/9"}
    q.save()
    monkeypatch.setattr(approve_mod, "gh_pr_state", lambda url: "open")
    assert main(["recover", "T-2", "--queue", str(env.queue_path),
                 "--yes"]) == 1  # open PR: refused
    monkeypatch.setattr(approve_mod, "gh_pr_state", lambda url: "closed")
    assert main(["recover", "T-2", "--queue", str(env.queue_path),
                 "--yes"]) == 0
    assert env.task("T-2")["status"] == "ready"


def test_credential_recover_persists_evidence_in_the_progress_log(
        tmp_path: Path):
    """Review round on #74: the evidence was only echoed to the terminal —
    the audit trail lost the one thing the whole gate exists to capture.
    A completed recover appends a progress-log entry (outcome
    ``recovered``, the escalation, the evidence, who, when); a declined
    or refused recover writes nothing."""
    from factory.approve import ApprovalDeclined, recover_task

    env = Env(tmp_path, tasks=[_task("T-1")])
    _park_escalated(env, "T-1", "credential_added")
    queue = lambda: TaskQueue(env.queue_path)  # noqa: E731

    before = len(env.log("T-1").entries())
    with pytest.raises(ApprovalDeclined):
        recover_task(queue(), "T-1", evidence="rotated",
                     confirm=lambda question: False, done_by="owner",
                     factory_root=env.root)
    assert len(env.log("T-1").entries()) == before, "a decline wrote a log entry"

    assert recover_task(queue(), "T-1",
                        evidence="test fixture, not a real credential",
                        confirm=lambda question: True, done_by="owner",
                        factory_root=env.root) == "ready"
    entries = env.log("T-1").entries()
    recovered = [e for e in entries if e.get("outcome") == "recovered"]
    assert len(recovered) == 1, entries
    entry = recovered[0]
    assert entry["escalation"] == "credential_added"
    assert entry["evidence"] == "test fixture, not a real credential"
    assert entry["done_by"] == "owner"
    assert entry["recovered_at"]


def test_credential_edge_is_state_gated_not_command_gated(tmp_path: Path):
    """Review round on #74: the credential exit was enforced only inside
    the recover command — any other writer of ``transition`` could move a
    credential task. The generic edge now refuses credential_added tasks;
    the dedicated state method is the only writer, and it refuses blank
    evidence at the state layer too."""
    from factory.state import TransitionError

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2", depends_on=["T-3"]),
                               _task("T-3")])
    for task_id in ("T-1", "T-2"):
        _park_escalated(env, task_id, "credential_added")

    with pytest.raises(TransitionError):
        TaskQueue(env.queue_path).transition("T-1", "ready")

    queue = TaskQueue(env.queue_path)
    with pytest.raises(Exception, match="evidence"):
        queue.recover_credential("T-1", "   ")
    assert env.task("T-1")["status"] == "needs_human", "blank evidence wrote"

    assert TaskQueue(env.queue_path).recover_credential(
        "T-1", "test fixture, not a real credential") == "ready"
    assert env.task("T-1")["status"] == "ready"
    # open dependency routes blocked, same rule as every recovery writer
    assert TaskQueue(env.queue_path).recover_credential(
        "T-2", "rotated") == "blocked"
    assert env.task("T-2")["status"] == "blocked"

    # the command still works through the new state method
    from factory.approve import recover_task
    _park_escalated(env, "T-1", "credential_added")
    assert recover_task(TaskQueue(env.queue_path), "T-1", evidence="rotated",
                        confirm=lambda question: True,
                        factory_root=env.root) == "ready"


# --- F-31 attempt 3: evidence-gated reset for attempts_exhausted -------------


class _FailingSeats:
    """Seats that never fix the bug: the contract test exists but stays
    red every round, so the run burns its whole attempt budget and
    escalates attempts_exhausted (the f10 test_ac_2 stub shape, inlined
    here so T-1's budget is the only variable under test)."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        from pathlib import Path

        self.calls.append(seat)
        if seat == "implementer":
            run_dir = Path(run_dir)
            (run_dir / "impl.py").write_text("def answer():\n    return 41\n")
            (run_dir / "tests").mkdir(exist_ok=True)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
        return SessionRecord(seat=seat, status="completed", num_turns=1,
                             model="stub-model", result="no fix")


def _park_exhausted(env, task_id: str, *, attempts: int = 2) -> None:
    """Park a task on attempts_exhausted with `attempts` spent rounds."""
    q = TaskQueue(env.queue_path)
    task = q.get(task_id)
    task["status"] = "needs_human"
    task["attempts"] = [
        {"run_id": f"r{i}", "slice_id": task_id,
         "started": f"2026-10-29T10:0{i}:00Z", "ended": f"2026-10-29T11:0{i}:00Z",
         "outcome": "fail", "rounds": i + 1, "prompt_versions": {}, "skills": []}
        for i in range(attempts)
    ] + [{
        "run_id": "rX", "slice_id": task_id,
        "started": "2026-10-29T12:00:00Z", "ended": "2026-10-29T12:01:00Z",
        "outcome": "escalated", "escalation": "attempts_exhausted",
        "rounds": attempts + 1, "prompt_versions": {}, "skills": [],
    }]
    q.save()


def test_attempts_exhausted_recover_refusals(tmp_path: Path):
    """The reset is deliberately human-paced, exactly like the credential
    exit: --evidence required, --reset-attempts required (without it the
    command must refuse AND say why — a plain re-queue would leave the
    budget spent and the next run would park straight back), interactive
    y/N with no --yes, a decline writes nothing, and every other
    escalation still has no owner word."""
    from factory.approve import ApprovalDeclined, ApprovalError, recover_task
    from factory.cli import main

    others = [("T-4", "quota_exhausted"), ("T-5", "protected_path_touched")]
    env = Env(tmp_path, tasks=[_task("T-1")]
              + [_task(tid) for tid, _ in others])
    _park_exhausted(env, "T-1")
    for task_id, escalation in others:
        _park_escalated(env, task_id, escalation)
    queue = lambda: TaskQueue(env.queue_path)  # noqa: E731

    # without --reset-attempts: refused, and the message must say why
    with pytest.raises(ApprovalError, match="reset-attempts"):
        recover_task(queue(), "T-1", evidence="environment bug, now fixed",
                     confirm=lambda question: True)
    # without evidence: refused
    with pytest.raises(ApprovalError, match="evidence"):
        recover_task(queue(), "T-1", reset_attempts=True,
                     confirm=lambda question: True)
    with pytest.raises(ApprovalError, match="evidence"):
        recover_task(queue(), "T-1", reset_attempts=True, evidence="  ",
                     confirm=lambda question: True)
    # --yes: refused — the confirmation IS the gate
    with pytest.raises(ApprovalError, match="interactive"):
        recover_task(queue(), "T-1", reset_attempts=True, assume_yes=True,
                     evidence="environment bug, now fixed")
    assert main(["recover", "T-1", "--queue", str(env.queue_path),
                 "--reset-attempts", "--yes",
                 "--evidence", "x"]) == 1
    # a decline writes nothing: status, history, no marker, no log entry
    before_log = len(env.log("T-1").entries())
    with pytest.raises(ApprovalDeclined):
        recover_task(queue(), "T-1", reset_attempts=True,
                     evidence="environment bug, now fixed",
                     confirm=lambda question: False)
    task = env.task("T-1")
    assert task["status"] == "needs_human"
    assert not task.get("attempt_resets"), "a decline wrote a reset marker"
    assert len(task["attempts"]) == 3
    assert len(env.log("T-1").entries()) == before_log

    # every other escalation still refuses
    for task_id, _ in others:
        with pytest.raises(ApprovalError, match="not recoverable"):
            recover_task(queue(), task_id, reset_attempts=True,
                         evidence="looks fine", confirm=lambda question: True)
        assert env.task(task_id)["status"] == "needs_human", task_id


def test_attempts_reset_restores_budget_and_keeps_history(tmp_path: Path):
    """The full story, end to end: a task burns max_attempts=2 and parks on
    attempts_exhausted; the owner recovers it with evidence + --reset-attempts;
    the next run actually RUNS (the budget counts only attempts after the
    reset marker) and every pre-reset attempt record is still there."""
    from factory.approve import recover_task
    from factory.state import attempt_budget_used

    env = Env(tmp_path, tasks=[_task(
        "T-1", budget={"size": "S", "max_attempts": 2, "max_turns": 20,
                       "wall_clock_min": 30})])
    assert env.run(_FailingSeats()).outcome == "needs_human"
    task = env.task("T-1")
    assert task["attempts"][-1]["escalation"] == "attempts_exhausted"
    spent = len(task["attempts"])
    assert attempt_budget_used(task, "T-1") == spent  # the filter this fixes

    assert recover_task(
        TaskQueue(env.queue_path), "T-1", reset_attempts=True,
        evidence="all 3 attempts died to the sandbox bind bug (PR #NN)",
        confirm=lambda question: True, done_by="owner", factory_root=env.root,
    ) == "ready"

    task = env.task("T-1")
    marker = task["attempt_resets"][-1]
    assert marker["prior_attempts"] == spent
    assert marker["evidence"] == "all 3 attempts died to the sandbox bind bug (PR #NN)"
    assert marker["done_by"] == "owner"
    assert marker["slice_id"] == "T-1"
    assert len(task["attempts"]) == spent, "history was deleted"
    assert attempt_budget_used(task, "T-1") == 0, "budget not restored"

    # the audit trail: the progress log records evidence + who + how many
    entry = [e for e in env.log("T-1").entries()
             if e.get("outcome") == "recovered"][-1]
    assert entry["escalation"] == "attempts_exhausted"
    assert entry["attempts_reset"] == spent

    # and the run loop proves it: the task runs again instead of parking
    # on "attempt budget already spent before this run"
    result = env.run(Seats())
    assert result.outcome == "pr_opened", result
    task = env.task("T-1")
    assert len(task["attempts"]) == spent + 1  # history kept, one new attempt
    assert task["attempts"][-1]["rounds"] == 1  # numbering restarted


def test_attempts_reset_routes_blocked_and_is_state_gated(tmp_path: Path):
    """The reset edge lives at the state layer, not the command: the
    generic transition refuses an attempts_exhausted task, the dedicated
    TaskQueue method is its only writer (blank evidence refused there
    too), and routing matches every recovery writer (blocked on an open
    dependency). The reset marker is append-only like attempts — a save
    that would delete one is refused."""
    from factory.state import TransitionError

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2", depends_on=["T-3"]),
                               _task("T-3")])
    for task_id in ("T-1", "T-2"):
        _park_exhausted(env, task_id)

    with pytest.raises(TransitionError):
        TaskQueue(env.queue_path).transition("T-1", "ready")

    queue = TaskQueue(env.queue_path)
    with pytest.raises(Exception, match="evidence"):
        queue.recover_attempts("T-1", "   ")
    assert env.task("T-1")["status"] == "needs_human", "blank evidence wrote"

    assert TaskQueue(env.queue_path).recover_attempts(
        "T-1", "env bug fixed", done_by="owner") == "ready"
    assert env.task("T-1")["status"] == "ready"
    # open dependency routes blocked, same rule as every recovery writer
    assert TaskQueue(env.queue_path).recover_attempts(
        "T-2", "env bug fixed", done_by="owner") == "blocked"
    assert env.task("T-2")["status"] == "blocked"

    # the marker is append-only: removing it (a budget-stealing rewrite)
    # is refused exactly like editing an attempt record
    q = TaskQueue(env.queue_path)
    q.get("T-1")["attempt_resets"] = []
    from factory.state import StateError
    with pytest.raises(StateError, match="append-only"):
        q.save()

    # the ineligible still refuse at the state layer
    _park_escalated(env, "T-3", "protected_path_touched")
    with pytest.raises(TransitionError):
        TaskQueue(env.queue_path).recover_attempts("T-3", "no")


def test_attempt_resets_marker_schema(tmp_path: Path):
    """Red-first schema addition: the marker is a first-class task field —
    a well-formed marker loads through F-02, a malformed one (missing
    evidence, or a stray key) refuses the whole queue."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    _park_exhausted(env, "T-1")

    marker = {"at": "2026-10-29T22:00:00Z", "evidence": "env bug fixed",
              "done_by": "owner", "slice_id": "T-1", "prior_attempts": 3}
    q = TaskQueue(env.queue_path)
    q.get("T-1")["attempt_resets"] = [marker]
    q.save()  # validates: a legal marker persists

    from factory.contract import ContractError, load_tasks
    bad = json.loads(env.queue_path.read_text())
    bad["tasks"][0]["attempt_resets"] = [{**marker, "evidence": ""}]
    env.queue_path.write_text(json.dumps(bad))
    with pytest.raises(ContractError):
        load_tasks(env.queue_path)
    bad["tasks"][0]["attempt_resets"] = [{**marker, "surprise": 1}]
    env.queue_path.write_text(json.dumps(bad))
    with pytest.raises(ContractError):
        load_tasks(env.queue_path)


def test_status_counts_attempts_across_a_reset(tmp_path: Path):
    """F-11's measures are spend and history, not budget: a reset restores
    attempts without hiding them — every pre-reset attempt still counts in
    interventions, rounds and the raw attempt count."""
    from factory.status import compute_metrics

    env = Env(tmp_path, tasks=[_task("T-1")])
    _park_exhausted(env, "T-1")
    q = TaskQueue(env.queue_path)
    task = q.get("T-1")
    task["status"] = "ready"
    task["attempt_resets"] = [{
        "at": "2026-10-29T22:00:00Z", "evidence": "env bug fixed",
        "done_by": "owner", "slice_id": "T-1",
        "prior_attempts": len(task["attempts"]),
    }]
    q.save()

    metrics = compute_metrics({"tasks": [env.task("T-1")]})
    assert metrics["attempts"] == 3
    assert metrics["rounds"] == sum(
        a.get("rounds") or 0 for a in env.task("T-1")["attempts"])


def test_review35_a_merged_escalated_pr_returns_to_review(tmp_path: Path):
    """Round-5 finding 4: an owner who resolves a stale_base conflict on
    GitHub and merges leaves the task parked needs_human with shipped
    code — the merged recovery moves it back in_review, and on this
    no-deploy fixture profile the close pass finishes the job to done
    in the same run, instead of stranding it and every task depending
    on it."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # stale_base

    # You resolve the conflict in GitHub's UI and merge the PR.
    result = env.run(Seats(), pr_state=lambda url: "merged")

    assert result.outcome == "idle"  # nothing to pick; recovery is the story
    assert env.task("T-1")["status"] == "done"  # recovered, then closed
    entries = env.log().entries()
    assert any("merged" in (e.get("note") or "") for e in entries)


def test_review36_the_merge_timeout_names_the_local_pathology(
        tmp_path: Path, monkeypatch):
    """Round-5 finding 5: the sweep's merge is local (both commits were
    already fetched) — a timeout there means a pathological merge or a
    disk problem, and the RunError must say so, not blame credentials
    or the network.

    F-45: the merge runs through factory.publish.remote_call with its own
    (local) timeout; the pin hangs the subprocess boundary and holds the
    message to the same standard."""
    from factory import publish as publish_mod
    from factory.runs import RunError

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")

    real_run = subprocess.run

    def hang_merge(args, **kwargs):
        if "merge" in args and "--no-edit" in args:
            raise subprocess.TimeoutExpired(args, 300)
        return real_run(args, **kwargs)

    monkeypatch.setattr(publish_mod.subprocess, "run", hang_merge)
    with pytest.raises(RunError) as excinfo:
        env.run(Seats())
    assert "local merge" in str(excinfo.value)
    # and it must not send the owner debugging the wrong layer
    assert "credential" not in str(excinfo.value)


def test_review37_the_recovery_scan_runs_once_per_run(tmp_path, monkeypatch):
    """Round-5 finding 6: the eligible set is computed once per run and
    passed into the sweep — no second scan whose answer could drift from
    the resolved URLs if the queue changed in between."""
    import factory.orchestrator as orch

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"

    real_recovery = orch._recovery_tasks
    calls: list = []

    def counting(queue):
        calls.append(queue.doc["project"])
        return real_recovery(queue)

    monkeypatch.setattr(orch, "_recovery_tasks", counting)
    assert env.run(Seats()).outcome in ("idle", "concurrency_wait")
    assert len(calls) == 1


# --------------------------- round 6 (owner review) ------------------------


def test_review38_a_merged_park_names_its_real_wait(tmp_path: Path):
    """Round-6 finding 1, narrowed by the no-deploy close: on a profile
    WITH a deploy target nothing advances a merged task from in_review
    (in_review → deploying is F-14, unbuilt) — the docs and the recovery
    note must say so. On a no-deploy profile there is no wait to name:
    the close pass takes the merged task to done in the same run."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    env.profile["deploy"]["mechanism"] = "vercel"  # a real deploy target
    env.profile_path.write_text(yaml.safe_dump(env.profile))
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # stale_base

    result = env.run(Seats(), pr_state=lambda url: "merged")

    assert result.outcome == "idle"
    assert env.task("T-1")["status"] == "in_review"
    note = next(e.get("note") or "" for e in reversed(env.log().entries())
                if e.get("outcome") == "recovered")
    assert "F-14" in note  # the wait is named, not "for reconcile"

    doc = (REPO_ROOT / "TASK-CONTRACT.md").read_text()
    assert "reconcile owns it from there" not in doc
    assert "ride the standard merged (reconcile) story" not in doc
    assert "waits in in_review for the F-14 deploy gate" in doc


def test_review39_a_merged_park_is_stamp_terminal(tmp_path: Path):
    """Round-6 finding 2: merged is terminal — the sweep stamps
    links.merged once, and later runs neither gh-poll the link nor let
    the unknown (unresolved) state fail the concurrency limit closed."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    assert env.run(Seats()).outcome == "pr_opened"

    merged = env.run(Seats(), pr_state=lambda url: "merged")
    assert merged.outcome == "idle"
    links = env.task("T-1")["links"]
    assert links["merged"] == links["pr"]  # stamped once, naming the PR

    calls: list[str] = []

    def counting(url):
        calls.append(url)
        return "open"

    later = env.run(Seats(), pr_state=counting)  # every later tick

    assert calls == []  # the merged link is never re-asked
    assert later.outcome == "idle"  # and never counted open (fail-closed)


def test_review40_recover_requires_a_closed_pr(tmp_path: Path, monkeypatch):
    """Round-6 finding 3: recover is the fix-round word — it refuses
    unless GitHub reports the PR closed. An open PR would hold the
    concurrency slot forever: every later run returns concurrency_wait
    naming it and the fix round never starts."""
    import factory.approve as approve_mod
    from factory.approve import ApprovalError, recover_task
    from factory.cli import main

    env = Env(tmp_path, tasks=[_task("T-1")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # stale_base; PR open

    with pytest.raises(ApprovalError, match="close the PR first"):
        recover_task(TaskQueue(env.queue_path), "T-1", assume_yes=True,
                     pr_state=lambda url: "open")
    monkeypatch.setattr(approve_mod, "gh_pr_state", lambda url: "open")
    assert main(["recover", "T-1", "--queue", str(env.queue_path),
                 "--yes"]) == 1
    assert env.task("T-1")["status"] == "needs_human"  # nothing moved

    # A re-opened pr_closed PR is the same refusal: the sweep returns it
    # to in_review, recover must not fight that.
    assert recover_task(TaskQueue(env.queue_path), "T-1", assume_yes=True,
                        pr_state=lambda url: "closed") == "ready"

    doc = (REPO_ROOT / "TASK-CONTRACT.md").read_text()
    assert "recover requires the PR closed" in doc


def test_review41_recovery_routes_blocked_on_unmet_deps(tmp_path: Path):
    """Round-6 finding 4: the fix-round edge lands the task in the same
    place approval would — blocked when a dependency is open, not a
    silently-unpickable ready."""
    from factory.approve import recover_task

    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # T-1 stale_base

    q = TaskQueue(env.queue_path)  # the dependency regressed to draft
    q.get("T-1")["depends_on"] = ["T-2"]  # not hashed: no contract demotion
    q.get("T-2")["status"] = "draft"
    q.save()

    swept = env.run(Seats(), pr_state=lambda url: "closed")
    assert swept.outcome == "idle"
    assert env.task("T-1")["status"] == "blocked"  # not a dead ready

    # And the command routes the same way.
    q = TaskQueue(env.queue_path)
    q.get("T-1")["status"] = "needs_human"  # back for the command half
    q.save()
    assert recover_task(TaskQueue(env.queue_path), "T-1", assume_yes=True,
                        pr_state=lambda url: "closed") == "blocked"


# --------------------------- round 7 (owner review) ------------------------


def test_review42_merged_stamp_does_not_carry_to_a_new_pr(tmp_path: Path):
    """Round-7 finding 1: ``links.merged`` names the PR that merged, not
    the task. Publishing keeps the task's other links, so a boolean
    stamp carried over to the task's NEXT PR (a rebuilt task, a later
    slice) — that PR was never gh-polled, never counted against the
    limit and never re-validated: a second open factory PR (AC-2)."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    assert env.run(Seats()).outcome == "pr_opened"
    first = env.task("T-1")["links"]["pr"]
    assert env.run(Seats(), pr_state=lambda url: "merged").outcome == "idle"
    assert env.task("T-1")["links"]["merged"] == first  # names the PR

    # The task comes back for another PR (rebuilt after a contract
    # change, or its next slice) — its links still carry the stamp.
    q = TaskQueue(env.queue_path)
    q.get("T-1")["status"] = "ready"
    q.save()
    states = {first: "merged"}
    assert env.run(Seats(), pr_state=lambda url: states.get(url, "open")
                   ).outcome == "pr_opened"
    second = env.task("T-1")["links"]["pr"]
    assert second != first

    # A new ready task: the second PR is open and must hold the slot.
    q = TaskQueue(env.queue_path)
    q.doc["tasks"].append(_task("T-2"))
    q.save()
    asked: list[str] = []

    def state(url):
        asked.append(url)
        return states.get(url, "open")

    result = env.run(Seats(), pr_state=state)

    assert result.outcome == "concurrency_wait"
    assert result.open_prs == [second]
    assert second in asked  # the new PR is polled, not stamp-skipped
    assert first not in asked  # the merged one stays terminal
    assert len(env.prs) == 2  # no third PR


def test_review43_run_reports_each_ready_pr_against_main(tmp_path: Path):
    """Round-7 finding 2: AC-3's "before reporting it ready" — and F-28's
    owner step "merge factory PRs only after `factory run` has reported
    them ready against the current main" — need a visible report. Every
    open PR the sweep found up to date (or re-validated) on the current
    remote head is named on the result: ``<url> against main@<sha12>``.
    F-75 AC-3: "ready" also means CI green on the head — this arm's PR
    is green by construction (a green ci_state seam)."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    green = lambda r, sha: CIState("success")  # noqa: E731
    assert env.run(Seats(), ci_state=green).outcome == "pr_opened"
    url = env.task("T-1")["links"]["pr"]
    head = _git(env.remote, "rev-parse", "refs/heads/main")

    up_to_date = env.run(Seats(), ci_state=green)
    assert up_to_date.outcome == "idle"
    assert up_to_date.ready_prs == [f"{url} against main@{head[:12]}"]

    # main moves (no conflict): re-validated on the new head, then ready.
    moved = env.advance_remote_main(None, extra_file="unrelated.txt")
    revalidated = env.run(Seats(), ci_state=green)
    assert revalidated.outcome == "idle"
    assert revalidated.ready_prs == [f"{url} against main@{moved[:12]}"]

    # The ls-remote cache hit still reports it ready (nothing re-run).
    cached = env.run(Seats(), ci_state=green)
    assert cached.ready_prs == [f"{url} against main@{moved[:12]}"]

    # Not ready: a stale PR that fails re-validation, a PR whose state
    # is unknown — neither may be reported ready.
    unknown = env.run(Seats(), ci_state=green, pr_state=lambda u: None)
    assert unknown.ready_prs == []
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    stale = env.run(Seats())
    assert stale.outcome == "needs_human"
    assert stale.ready_prs == []


def test_review44_cli_prints_the_ready_lines(tmp_path: Path, monkeypatch, capsys):
    """Round-7 finding 2, CLI half: `factory run` and `factory loop`
    print one `ready:` line per ready PR, so the owner sees it without
    reading progress logs."""
    import factory.cli as cli
    from factory.orchestrator import LoopResult, RunResult

    line = "https://github.com/o/r/pull/7 against main@0123456789ab"
    monkeypatch.setattr(cli, "run_once", lambda *a, **k: RunResult(
        "idle", ready_prs=[line]))
    assert cli.main(["run", "--queue", str(tmp_path / "tasks.json")]) == 0
    assert f"  ready: {line}" in capsys.readouterr().out.splitlines()

    monkeypatch.setattr(cli, "run_loop", lambda *a, **k: LoopResult(
        "queue_empty", results=[RunResult("idle", ready_prs=[line])]))
    assert cli.main(["loop", "--queue", str(tmp_path / "tasks.json")]) == 0
    assert f"    ready: {line}" in capsys.readouterr().out.splitlines()


# ------------------------------- no-deploy merged close (fix, F-26 surface) ---

# The bug this fixes (2026-10-29): F-11/#58 and F-12/#61 merged, the sweep
# stamped links.merged, and both tasks then waited in in_review forever —
# only F-14 (L4 live checks, not built) would have advanced them, so the
# queue drained: F-31 stayed blocked on F-11 and `factory run` went idle.
# On a profile with NO deploy target (deploy.mechanism none-form AND
# live_checks empty) the merge is the whole story: nothing will ever
# deploy or be verified live, so the task closes through the state
# machine — in_review -> deploying -> done (awaiting_owner with open
# owner steps), the merged PR recorded as the evidence.


def _stamped_in_review(env: Env, task_id: str = "T-1", *, branch: str,
                       pr_url: str = "https://github.com/fixture/fixture/pull/9"):
    """Hand-craft a task in_review whose PR the sweep stamped merged."""
    _force_in_review(env, task_id, branch=branch, pr_url=pr_url)
    q = TaskQueue(env.queue_path)
    q.get(task_id)["links"]["merged"] = q.get(task_id)["links"]["pr"]
    q.save()


def test_merged_closes_done_on_a_no_deploy_project(tmp_path: Path):
    """No deploy target: a stamped-merged in_review task reaches done
    through the F-03 path in one run, the merge the evidence — no gh
    round-trip (merged links are terminal), no attempt spent."""
    env = Env(tmp_path)
    _stamped_in_review(env, branch="factory/t-1-merged")
    attempts = len(env.task()["attempts"])
    pr = env.task("T-1")["links"]["pr"]

    result = env.run(Seats())  # pr_state default "open" is never asked

    assert result.outcome == "idle"              # nothing else to pick
    task = env.task("T-1")
    assert task["status"] == "done"              # in_review -> deploying -> done
    assert len(task["attempts"]) == attempts     # session-less close
    notes = [e.get("note", "") for e in env.log().entries()]
    assert any(pr in note and "done" in note for note in notes)  # evidence


def test_merged_stays_in_review_with_a_deploy_target(tmp_path: Path):
    """A deploy target (a real mechanism, or live checks to run) keeps
    the merged task in in_review: merged and deployed are separate
    facts, and until F-14 exists nothing may close it."""
    for slug, mechanism, checks in (
        ("mechanism", "vercel", {}),
        ("live-checks", "none",
         {"probe": {"command": "curl -fsS https://example.com/health",
                    "when": "every_deploy"}}),
    ):
        env = Env(tmp_path / slug)
        _stamped_in_review(env, branch="factory/t-1-merged")
        env.profile["deploy"]["mechanism"] = mechanism
        env.profile["live_checks"] = checks
        env.profile_path.write_text(yaml.safe_dump(env.profile))

        result = env.run(Seats())

        assert result.outcome == "idle"
        assert env.task("T-1")["status"] == "in_review"
        assert not any("closed" in e.get("note", "")
                       for e in env.log().entries())


def test_merged_with_open_owner_steps_awaits_owner(tmp_path: Path):
    """Owner steps still gate done (F-03): a merged no-deploy task with
    an open step parks in awaiting_owner, and ticking it closes the task."""
    env = Env(tmp_path, tasks=[_task(
        "T-1", owner_steps=[{"text": "check the merged dashboard", "done": False}])])
    _stamped_in_review(env, branch="factory/t-1-merged")

    assert env.run(Seats()).outcome == "idle"
    assert env.task("T-1")["status"] == "awaiting_owner"

    status = TaskQueue(env.queue_path).tick_owner_steps(
        "T-1", [1], evidence="rendered the dashboard by hand", done_by="owner")
    assert status == "done"


def test_an_open_pr_is_unchanged_by_the_close_pass(tmp_path: Path):
    """A PR GitHub still reports open is not merged: the task stays
    in_review, no stamp, no close."""
    env = Env(tmp_path)
    _force_in_review(env, branch="factory/t-1-open")
    _push_remote_branch(env, "factory/t-1-open", "def answer():\n    return 42\n")

    result = env.run(Seats())  # pr_state "open", branch up to date

    assert result.outcome == "idle"
    task = env.task("T-1")
    assert task["status"] == "in_review"
    assert "merged" not in task["links"]
    assert not any("closed" in e.get("note", "")
                   for e in env.log().entries())


def test_the_close_pass_unblocks_a_dependent_in_the_same_run(tmp_path: Path):
    """The F-31 story end to end: closing the merged task and syncing
    readiness happen BEFORE the pick, so one run closes the dependency
    and starts the dependent that was blocked on it."""
    env = Env(tmp_path, tasks=[
        _task("T-1"),
        _task("T-2", depends_on=["T-1"], status="blocked"),
    ])
    _stamped_in_review(env, branch="factory/t-1-merged")

    result = env.run(Seats())

    assert env.task("T-1")["status"] == "done"
    assert result.outcome == "pr_opened"          # T-2 picked this run
    assert env.task("T-2")["status"] == "in_review"


def test_a_merged_recovery_escalation_closes_too(tmp_path: Path):
    """A stale_base task whose PR you merged on GitHub: the recovery
    edge returns it to in_review stamped, and the close pass takes it
    to done in the same run — no extra attempt, no re-validation."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"       # stale_base
    attempts = len(env.task("T-1")["attempts"])

    result = env.run(Seats(), pr_state=lambda url: "merged")

    assert result.outcome == "pr_opened"          # T-2: the slot freed
    task = env.task("T-1")
    assert task["status"] == "done"
    assert len(task["attempts"]) == attempts      # no re-validation ran
    notes = [e.get("note", "") for e in env.log().entries()]
    assert any(e.get("outcome") == "recovered" for e in env.log().entries())
    assert any(task["links"]["pr"] in note and "done" in note for note in notes)


# --------------------------- review fixes on the no-deploy close (PR #72) ---


@pytest.mark.parametrize("mechanism,expected", [
    ("none", True),
    ("None", True),
    ("NONE (local CLI; nothing ships)", True),
    ("none (local CLI; nothing ships)", True),
    ("none(x)", True),
    ("", False),
    ("vercel", False),
    ("nonexistent-host", False),          # fails open under startswith("none")
    ("nonetheless (vercel)", False),      # same
    ("none-but-actually-vercel", False),  # hyphenated prose is not the form
])
def test_no_deploy_target_matches_only_the_none_form(mechanism, expected):
    """Review finding 2: the none-form is exactly ``none``, or ``none``
    followed by optional whitespace and an opening paren — anything else
    (``nonexistent-host``, ``nonetheless (vercel)``) is a deploy target,
    so a merged task there must keep waiting for F-14, never close."""
    from factory.orchestrator import _no_deploy_target

    assert _no_deploy_target({
        "deploy": {"mechanism": mechanism},
        "live_checks": {},
    }) is expected


@pytest.mark.parametrize("mechanism", ["nonexistent-host", "nonetheless (vercel)"])
def test_fail_open_mechanism_text_keeps_the_task_in_review(tmp_path: Path,
                                                            mechanism):
    """Review finding 2, behaviorally: a stamped-merged task on a profile
    whose mechanism merely starts with "none" stays in_review — the
    classifier may not fail open into closing it."""
    env = Env(tmp_path / mechanism.replace(" ", "-").replace("(", "").replace(")", ""))
    _stamped_in_review(env, branch="factory/t-1-merged")
    env.profile["deploy"]["mechanism"] = mechanism
    env.profile_path.write_text(yaml.safe_dump(env.profile))

    assert env.run(Seats()).outcome == "idle"
    assert env.task("T-1")["status"] == "in_review"
    assert not any(e.get("outcome") == "closed" for e in env.log().entries())


def test_a_merged_recovery_with_an_unmerged_slice_is_not_claimed_closed(
        tmp_path: Path):
    """Review finding 3: the recovery note may only say what happened.
    A no-deploy profile whose recovered task still has an unmerged
    slice: the close pass skips it (slice continuation), so the task
    stays in_review and no log entry claims a close."""
    env = Env(tmp_path, tasks=[_task("T-1", status="ready", slices=[
        {"id": "S-1", "text": "first slice", "status": "todo"},
        {"id": "S-2", "text": "second slice", "status": "todo"},
    ])])
    q = TaskQueue(env.queue_path)
    q.transition("T-1", "in_progress")
    q.transition("T-1", "in_review")
    q.get("T-1")["links"] = {
        "pr": "https://github.com/fixture/fixture/pull/9",
        "branch": "factory/t-1-merged",
    }
    q.append_attempt("T-1", {
        "run_id": "run-t1-r1-at1", "slice_id": "S-1",
        "started": "2026-10-18T10:00:00Z", "outcome": "escalated",
        "prompt_versions": {}, "skills": [],
        "escalation": "stale_base", "note": "fixture escalation",
    })
    q.transition("T-1", "needs_human")

    result = env.run(Seats(), pr_state=lambda url: "merged")

    assert result.outcome == "idle"
    task = env.task("T-1")
    assert task["status"] == "in_review"        # close skipped: S-2 unmerged
    assert task["links"]["merged"] == task["links"]["pr"]  # stamp happened
    recovered = [e.get("note") or "" for e in env.log().entries()
                 if e.get("outcome") == "recovered"]
    assert recovered and "closed" not in recovered[-1]
    assert not any(e.get("outcome") == "closed" for e in env.log().entries())


def test_an_idle_run_with_a_failing_probe_still_sweeps_and_closes(tmp_path: Path):
    """Review fix on PR #73: the interpreter probe runs only when a task
    was picked. An idle tick with a broken interpreter still sweeps — a
    merged no-deploy task still closes — and returns idle; the probe
    must not sit in front of the sweep or the close pass."""
    env = Env(tmp_path)
    _stamped_in_review(env, branch="factory/t-1-merged")

    def broken(*args, **kwargs):
        raise RunError(
            "the sandboxed session interpreter cannot import "
            "claude_agent_sdk (.venv/bin/python, exit 1)")

    result = env.run(Seats(), sdk_probe=broken)

    assert result.outcome == "idle"
    assert env.task("T-1")["status"] == "done"   # the close pass still ran
