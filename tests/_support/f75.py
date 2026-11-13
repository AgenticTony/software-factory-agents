"""F-75 test support: scripted CI states, gh run actions, clock, drafting.

The F-38/F-72 split holds: git, the clone manager, the queue, the L0/L1
sandboxes and the push to the local bare remote are real; only the model
seats, the CI seams and the gh read/write seams are scripted. What this
harness adds over ``_support.f72``:

* a ``CI`` ci_state seam fake — per-SHA canned :class:`CIState` answers
  (a LIST answer is consumed one entry per poll, the last repeating),
  recording every ``(remote, sha)`` call;
* an ``Actions`` CIActions fake — the failed-Actions-runs listing, the
  ``gh run rerun --failed`` and the ``gh run view --log-failed`` seams,
  with call records and scriptable failures;
* a ``FakeClock`` — the wait's clock/sleep seams in one object (sleep
  advances the clock, so no test ever really sleeps);
* ``Env75.draft`` — the ``gh pr ready --undo`` recorder (the exhausted
  path converts the run's own PR to a draft).

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _support.f72 import Env72
from factory.mergeorder import CIState
from factory.state import TaskQueue

__all__ = [
    "Actions",
    "CI",
    "Env75",
    "FakeClock",
    "ci_state",
]


def ci_state(state: str, checks=(("Backend (3.11)", "failure"),),
             detail: str = "", summaries=()) -> CIState:
    """One canned CI answer (a real CIState — the orchestrator reads
    .state/.checks/.detail/.summaries only)."""
    return CIState(state, tuple(checks), detail, tuple(summaries))


class CI:
    """The ci_state seam fake: per-SHA answers, every call recorded.

    A LIST value for a SHA is consumed one entry per call (the last
    repeats) — the pending-then-green and red-then-green-after-rerun
    scripts. An EXCEPTION instance is raised (the seam failing — the
    G4 first-poll degrade). Anything else is returned as-is every call.
    """

    def __init__(self, by_sha: dict | None = None, default=None):
        self.by_sha = by_sha or {}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, remote, sha):
        self.calls.append(sha)
        answer = self.by_sha.get(sha, self.default)
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Actions:
    """The CIActions fake: failed runs per SHA, logs per run id.

    ``failed``: ``{sha: [{"id": 101, "name": ..., "conclusion":
    "failure"}, ...]}`` — the runs ``gh run list --commit <sha>`` would
    answer. ``failed_default``: the answer for every SHA ``failed`` does
    not key (a scripted run does not know its pushed SHAs up front).
    ``gate``: a callable — failed_runs answers ``failed_default`` only
    while it is False (the R1-F1 shape: [] while the workflow run is
    still in progress, the failed run once every job has completed —
    keyed on the CI seam's poll count, which the arm wires in).
    ``logs``: ``{run_id: raw --log-failed text}``. ``rerun_error`` /
    ``log_error``: exceptions the seam raises (gh failing to start).
    """

    def __init__(self, failed: dict | None = None, logs: dict | None = None,
                 rerun_error: Exception | None = None,
                 log_error: Exception | None = None,
                 failed_default: list | None = None,
                 failed_script: list | None = None,
                 gate=None):
        self.failed = failed or {}
        self.failed_default = failed_default
        self.failed_script = list(failed_script or [])
        self.gate = gate
        self.logs = logs or {}
        self.rerun_error = rerun_error
        self.log_error = log_error
        self.rerun_calls: list[int] = []
        self.log_calls: list[int] = []
        self.list_calls = 0

    def failed_runs(self, remote, sha) -> list[dict]:
        self.list_calls += 1
        if self.failed_script:
            if len(self.failed_script) > 1:
                return list(self.failed_script.pop(0))
            return list(self.failed_script[0])
        if self.gate is not None and not self.gate():
            return []
        if sha in self.failed:
            return list(self.failed[sha])
        return list(self.failed_default or [])

    def rerun(self, remote, run_id) -> None:
        self.rerun_calls.append(run_id)
        if self.rerun_error is not None:
            raise self.rerun_error

    def log_failed(self, remote, run_id) -> str:
        self.log_calls.append(run_id)
        if self.log_error is not None:
            raise self.log_error
        return self.logs.get(run_id, "")


class FakeClock:
    """The wait's clock+sleep seams: sleep advances the clock by the
    asked interval, so a scripted window expires in microseconds."""

    def __init__(self, start: float = 1000.0):
        self.t = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, sec: float) -> None:
        self.sleeps.append(sec)
        self.t += sec


class Env75(Env72):
    """Env72 plus the F-75 seams: the draft converter recorder and a
    force-in_review helper for the sweep (ready-rule) arms."""

    def __init__(self, tmp_path: Path, tasks: list | None = None,
                 profile_extra: dict | None = None):
        super().__init__(tmp_path, tasks=tasks, profile_extra=profile_extra)
        self.draft_calls: list[str] = []

    def draft(self, url: str) -> None:
        """The gh pr ready --undo seam: records the URL (monkeypatched
        over factory.orchestrator.gh_pr_draft)."""
        self.draft_calls.append(url)

    def force_in_review(self, branch: str, task_id: str = "T-1",
                        pr_url: str | None = None) -> str:
        """Push ``branch`` (main's head plus one commit — a real PR head
        differs from main, and the CI scripts key on that), link it, and
        put the task in_review: the sweep's subject (the test_f58 shape).
        The fixture commit carries its own identity (CI has none)."""
        scratch = self.tmp / f"branch-{branch.replace('/', '-')}"
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(scratch)],
            check=True, capture_output=True)
        (scratch / "impl.py").write_text("def answer():\n    return 42\n")
        for args in (
            ("add", "-A"),
            ("commit", "-qm", "pr branch"),
        ):
            subprocess.run(
                ["git", "-C", str(scratch),
                 "-c", "user.email=fixture@factory.local",
                 "-c", "user.name=Fixture", *args],
                check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(scratch), "push", "-q", "origin",
             f"HEAD:refs/heads/{branch}"], check=True, capture_output=True)
        sha = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse",
             f"refs/heads/{branch}"],
            check=True, capture_output=True, text=True).stdout.strip()
        url = pr_url or "https://github.com/fixture/fixture/pull/9"
        queue = TaskQueue(self.queue_path)
        queue.transition(task_id, "in_progress")
        queue.transition(task_id, "in_review")
        task = queue.get(task_id)
        task["links"] = {"pr": url, "branch": branch}
        queue.save()
        return sha
