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
    schema = json.loads((REPO_ROOT / "schema" / "tasks.schema.json").read_text())
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
    """Finding 1: a squash-merged PR (task still in_review until reconcile)
    must be skipped by the sweep, not escalated stale_base — the contract
    tests already pass on the new main, so re-validation would fail."""
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
    assert task["status"] == "in_review"          # reconcile's story, not ours
    assert len(task["attempts"]) == before        # no stale_base attempt
    assert len(env.log().entries()) == entries    # no re-validation ran


def test_review2_a_deleted_branch_after_merge_does_not_wedge(tmp_path: Path):
    """Finding 2: with auto-delete head branches, the PR's branch is gone
    from the remote while the task sits in_review — run must not raise."""
    env = Env(tmp_path, tasks=[_task("T-1"), _task("T-2")])
    _force_in_review(env, branch="factory/t-1-gone")
    # The branch is deliberately never pushed: fetching it would fail.

    result = env.run(Seats(), pr_state=lambda url: "merged")

    assert result.outcome == "pr_opened"          # the next task is not wedged
    assert env.task("T-1")["status"] == "in_review"


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
    instead of quoting an empty stderr."""
    import types

    import factory.orchestrator as orch

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

    monkeypatch.setattr(
        orch, "subprocess",
        types.SimpleNamespace(run=spy, TimeoutExpired=subprocess.TimeoutExpired))

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
    schema = json.loads((REPO_ROOT / "schema" / "tasks.schema.json").read_text())
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
    lease on a username prompt."""
    import types

    import factory.orchestrator as orch

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

    monkeypatch.setattr(
        orch, "subprocess",
        types.SimpleNamespace(run=spy, TimeoutExpired=subprocess.TimeoutExpired))

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
    assert env.run(Seats()).outcome == "pr_opened"
    assert env.run(Seats(), pr_state=lambda url: "merged").outcome == "pr_opened"
    assert env.task("T-1")["status"] == "in_review"
    assert env.task("T-2")["status"] == "in_review"

    closed = lambda url: "closed"  # noqa: E731
    first = env.run(Seats(), pr_state=closed)
    assert first.escalation == "pr_closed"
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
    """Round-3 finding 8 (rewritten for round-4 finding 2's gate): the
    recovery edges are legal for an escalated task holding a PR link —
    applied through the queue, which enforces the gate — and
    TASK-CONTRACT carries both rows."""
    env = Env(tmp_path, tasks=[_task("T-1")])
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # stale_base, PR linked
    TaskQueue(env.queue_path).transition("T-1", "in_progress")  # eligible

    env2 = Env(tmp_path / "b", tasks=[_task("T-1")])
    assert env2.run(Seats()).outcome == "pr_opened"
    assert env2.run(Seats(), pr_state=lambda url: "closed").escalation \
        == "pr_closed"
    TaskQueue(env2.queue_path).transition("T-1", "in_review")  # eligible

    doc = (REPO_ROOT / "TASK-CONTRACT.md").read_text()
    assert "`needs_human → in_review`" in doc
    assert "`needs_human → in_progress`" in doc


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
    """Round-4 findings 1+7: the stale_base fix-round remedy is the sweep
    reading your rejection (the closed PR). The round re-runs from the
    current default head — no owner branch update is promised or used."""
    env = Env(tmp_path, tasks=[_task("T-1")])  # 3 attempts: run + escalation + fix round
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    assert env.run(Seats()).outcome == "needs_human"  # stale_base; PR open
    old_pr = env.task()["links"]["pr"]

    rejected = env.run(Seats(), pr_state=lambda url: "closed")

    assert rejected.outcome == "idle"  # pick ran before the sweep: none
    assert env.task()["status"] == "in_progress"  # sweep queued the fix round
    assert any(e.get("outcome") == "recovered" for e in env.log().entries())

    result = env.run(Seats(), pr_state=lambda url: "closed")  # reclaim→ready
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
        check_transition("needs_human", "in_progress")

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

    TaskQueue(env.queue_path).transition("T-1", "in_progress")  # eligible
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
    exit-2 message, never a Python traceback."""
    import factory.orchestrator as orch
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

    monkeypatch.setattr(orch, "subprocess", SimpleNamespace(
        run=hang_merge, TimeoutExpired=subprocess.TimeoutExpired))
    with pytest.raises(RunError, match="timed out"):
        env.run(Seats())

    # Half 2: the run's initial fetch of the remote default branch
    # (_build_on_remote_default — blob-filtered, unlike the sweep's full
    # fetches).
    env2 = Env(tmp_path / "b", tasks=[_task("T-1")])

    def hang_build_fetch(args, **kwargs):
        if "fetch" in args and "--filter=blob:none" in args:
            raise subprocess.TimeoutExpired(args, 300)
        return real_run(args, **kwargs)

    monkeypatch.setattr(orch, "subprocess", SimpleNamespace(
        run=hang_build_fetch, TimeoutExpired=subprocess.TimeoutExpired))
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
