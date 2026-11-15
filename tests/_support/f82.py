"""F-82 test support: the open-findings publish harness.

Composed from the same ``_support`` primitives as F-77/F-78/F-79 (F-51
AC-7: no test module imports another — support modules compose instead):
git, the clone manager, the queue, the L0/L1 sandboxes and the push to
the local bare remote are real; the model seats and the CI/gh seams are
scripted. What this adds:

* ``FINDING_A`` / ``FINDING_B`` — two minor findings, the wo21 shape (a
  review that declares pass and still carries them);
* ``Published`` — one task published green with ``open`` still open and
  no park (the round-1 half of every scenario);
* ``edited_round`` — round 1 published with A open, CI settled red, the
  owner reset keeping the PR, and the sweep's fix round published with
  ``open`` instead; returns every ``(url, body)`` the PR-body refresh
  seam was handed (AC-1's refresh arm);
* ``rewrite_followup`` — a test-side queue edit standing in for the
  owner's hand on the draft (an edit, or an approval's status move).

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

from pathlib import Path

from _support.f72 import Seats, _finding, _task, _verdict
from _support.f75 import CI
from _support.f78 import GREEN, ONE_ATTEMPT, Parked, seams

__all__ = [
    "FINDING_A", "FINDING_B", "FLOOR", "OPEN_A", "OPEN_B", "Published",
    "edited_round", "followup_task", "rewrite_followup",
]

FINDING_A = _finding(
    text="the download path shows send-context wording on a manual "
         "download — nothing was being sent",
    file="impl.py", severity="minor", axis="spec")
FINDING_B = _finding(
    text="the whitespace-collapse helper exists three times; extract "
         "one shared helper",
    file="impl.py", severity="minor", axis="standards")
# The two round verdicts: pass, still carrying the finding (the wo21
# shape — a no-findings pass is `_verdict()`, what every "adds nothing"
# arm uses).
OPEN_A = _verdict(FINDING_A, declared="pass")
OPEN_B = _verdict(FINDING_B, declared="pass")
# The fixture profile declares no budget_floor, so the effective floor is
# the F-71 default — what a follow-up task's budget must equal.
FLOOR = {"max_turns": 60, "wall_clock_min": 30}


class Published:
    """The task published green with findings still open — no park, the
    PR ready. ``result`` is the run's RunResult; ``env``/``queue()``/
    ``log()`` reach the fixture around it."""

    def __init__(self, tmp: Path, monkeypatch, open_verdict=OPEN_A,
                 tasks: "list[dict] | None" = None):
        from _support.f76 import Env76

        self.env = Env76(tmp, tasks=tasks or [_task(budget=ONE_ATTEMPT)])
        seams(monkeypatch, self.env)
        self.seats = Seats(reviews=(open_verdict, OPEN_B))
        self.result = self.env.run_once(self.seats, ci_state=CI(default=GREEN))
        assert self.result.outcome == "pr_opened", self.result
        self.url = self.env.task()["links"]["pr"]

    def queue(self):
        from factory.state import TaskQueue

        return TaskQueue(self.env.queue_path)

    def log(self) -> list:
        from factory.progress import ProgressLog

        return ProgressLog.for_task(
            self.env.root, "fixture", "T-1").entries()


def followup_task(p) -> "dict | None":
    """The queue's T-1-followup record, or None when there is none."""
    return next(
        (t for t in p.queue().doc.get("tasks", [])
         if t.get("id") == "T-1-followup"), None)


def edited_round(tmp: Path, monkeypatch, round1=OPEN_A, round2=OPEN_B,
                 after_publish=None) -> "tuple[Parked, object, list[tuple[str, str]]]":
    """Round 1 publishes with ``round1`` open; CI settles red and the
    one-attempt budget parks ``ci_failed``; the owner resets keeping
    the PR; the sweep's fix round publishes with ``round2`` open.
    ``after_publish(p)`` runs between the park and the reset (the
    owner's hand on the draft, in the edited/approved arms).
    Returns ``(parked, fix_result, edited)`` — the Parked fixture, the
    fix round's RunResult and every ``(url, body)`` handed to the
    PR-body refresh seam."""
    import factory.orchestrator as orch
    from _support.f78 import IMPL_FIXED
    from _support.f79 import fix_round, recover_open

    edited: "list[tuple[str, str]]" = []

    def record_edit(url, body):
        edited.append((url, body))

    monkeypatch.setattr(orch, "gh_pr_edit", record_edit)
    p = Parked(tmp, monkeypatch,
               tasks=[_task(budget=ONE_ATTEMPT)],
               seats=Seats(impl_files=[{}, {"impl.py": IMPL_FIXED}],
                           reviews=(round1, round2)))
    if after_publish is not None:
        after_publish(p)
    assert recover_open(p) == "in_review"
    result = fix_round(p, monkeypatch)
    return p, result, edited


def rewrite_followup(p, mutate) -> None:
    """Apply ``mutate(task_dict)`` to the follow-up draft in the queue
    file — the test-side stand-in for the owner's hand on their queue
    (an edit, or an approval's status move)."""
    queue = p.queue()
    mutate(queue.get("T-1-followup"))
    queue.save()
