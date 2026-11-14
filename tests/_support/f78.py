"""F-78 test support: the scope-park harness.

Composed from the same ``_support`` primitives test_f77's ``Parked``
uses (F-51 AC-7: no test module imports another — the shared harness
lives in ``_support``; test_f77.py is protected and pinned, so its
fixture is re-composed here rather than hoisted. A future WO may hoist
``Parked`` into ``_support`` properly and both test modules import it).

Everything here is as in test_f77, byte-for-byte in behaviour:

- ``seams`` — the F-75 orchestrator CI/gh seam patcher (``_seams``);
- ``Parked`` — a task parked ``ci_failed`` by the REAL sweep: published
  green, CI settled red on the pushed head, the one-attempt budget
  spent, the PR drafted, the head on ``links.ci_rerun_heads``;
- ``append_escalation`` — append a later escalated attempt record
  (the shape every session-less or mid-round park writes);
- ``GREEN``/``RED``/``RED_SCRIPT``/``CHECK_NAME``/``LOG_TAIL`` — the
  scripted CI states and the failing check's log tail.
"""

import factory.orchestrator as orch
from _support.f72 import Seats, _task
from _support.f75 import CI, Actions, ci_state
from _support.f76 import Env76
from factory.config import FactoryConfig

CHECK_NAME = "Backend (3.11)"
FAILED_RUN = {"id": 101, "name": CHECK_NAME, "conclusion": "failure"}
LOG_TAIL = (
    f"{CHECK_NAME}\tRun tests\t2026-11-14T09:00:00.000Z\t"
    "FAILED tests/test_doc.py::test_ac_2 - AssertionError: extracted "
    "text differs on the CI runner\n"
)
GREEN = ci_state("success", checks=((CHECK_NAME, "success"),))
RED = ci_state("failed", checks=((CHECK_NAME, "failure"),))
PENDING = ci_state("pending", checks=((CHECK_NAME, "in_progress"),))
# The sweep's not-ready poll, settle's first wait, the flake re-run's
# observation poll, then red again (the last repeats).
RED_SCRIPT = [RED, RED, PENDING, RED]
IMPL_FIXED = "def answer():\n    return 42  # CI fix round\n"
ONE_ATTEMPT = {"size": "S", "max_attempts": 1, "max_turns": 20,
               "wall_clock_min": 30}


def seams(monkeypatch, env: Env76, actions: Actions | None = None) -> Actions:
    """Patch the F-75 orchestrator seams; returns the Actions fake."""
    from _support.f75 import FakeClock

    actions = actions or Actions(failed_default=[FAILED_RUN],
                                 logs={101: LOG_TAIL})
    clock = FakeClock()
    monkeypatch.setattr(orch, "ci_clock", clock)
    monkeypatch.setattr(orch, "ci_sleep", clock.sleep)
    monkeypatch.setattr(orch, "CIActions", lambda: actions)
    monkeypatch.setattr(orch, "gh_pr_draft", env.draft)
    return actions


class Parked:
    """One task parked ``ci_failed`` by the REAL sweep: published green,
    CI then settled red on the pushed head, the one-attempt budget was
    spent, the PR was drafted. ``head`` is on ``links.ci_rerun_heads``
    (the flake guard ran) and equals ``links.pushed_head``."""

    def __init__(self, tmp, monkeypatch, tasks: list | None = None,
                 seats: Seats | None = None):
        self.env = Env76(tmp, tasks=tasks or [_task(budget=ONE_ATTEMPT)])
        seams(monkeypatch, self.env)
        self.seats = seats or Seats(impl_files=[{}, {"impl.py": IMPL_FIXED}])
        assert self.env.run_once(
            self.seats, ci_state=CI(default=GREEN)).outcome == "pr_opened"
        links = self.env.task()["links"]
        self.url = links["pr"]
        self.branch = links["branch"]
        self.head = self.env.remote_branch_sha(self.branch)
        self.park_result = self.env.run_once(
            self.seats,
            ci_state=CI(by_sha={self.head: list(RED_SCRIPT)}, default=RED),
            config=FactoryConfig(ci_wait_min=5))
        task = self.env.task()
        assert self.park_result.outcome == "needs_human", self.park_result
        assert task["status"] == "needs_human"
        assert task["attempts"][-1]["escalation"] == "ci_failed"
        assert task["links"]["pushed_head"] == self.head
        assert task["links"]["ci_rerun_heads"] == [self.head]
        assert self.env.draft_calls == [self.url]

    def queue(self):
        from factory.state import TaskQueue

        return TaskQueue(self.env.queue_path)

    def snapshot(self) -> str:
        import json

        return json.dumps(self.env.task(), sort_keys=True)

    def log(self) -> list:
        from factory.progress import ProgressLog

        return ProgressLog.for_task(self.env.root, "fixture", "T-1").entries()


def append_escalation(queue, escalation: str) -> None:
    """Append a later escalated attempt record — the shape every
    session-less sweep park or mid-round request writes (as test_f77's
    ``_append_escalation``)."""
    queue.append_attempt("T-1", {
        "run_id": "20261114T000000Z-f78f78",
        "slice_id": "T-1",
        "started": "2026-11-14T00:00:00Z",
        "ended": "2026-11-14T00:00:00Z",
        "outcome": "escalated",
        "rounds": 0, "turns": 0,
        "input_tokens": 0, "output_tokens": 0,
        "prompt_versions": {}, "skills": [],
        "escalation": escalation,
        "note": "parked for the test",
    })
