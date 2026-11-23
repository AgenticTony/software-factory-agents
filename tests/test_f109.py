"""F-109 — Command and session text never crashes the queue it is recorded in.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f109.py::test_ac_N``
means a test function with exactly that name.

Three tests (BUILD-ORDER §F-109):

- AC-1 the attempt writer escapes: a failing project sentinel whose
  command prints a newline, an ANSI clear-screen and a bidi override
  ends ``sentinel_failed`` (never ``orchestrator_error``), the queue
  loads, and the stored detail shows the escapes as text;
- AC-2 every other queue writer of session- or command-derived text
  stores the same hostile text escaped, so the queue it writes loads;
- the loader still refuses a raw control character in a hand-written
  queue — the writer-side escape is never an excuse to relax it.

Real vs stubbed: AC-1 runs the real orchestrator on the F-72 harness
(real git, real L0 sandbox, scripted seats); AC-2 drives the state
layer's writers directly, the way the run and the recover command do.
"""

import json

import pytest
from _support.f72 import Env72, Seats, _task

from factory.contract import ContractError
from factory.state import TaskQueue, TransitionError

# The AC-1 text: a newline (the multi-line output that started this WO),
# an ANSI clear-screen, and a RIGHT-TO-LEFT OVERRIDE — built with chr()
# so the bidi control never sits raw in this source file.
HOSTILE = "line1\nline2\x1b[2J" + chr(0x202E)
# The stored form: newline, ESC and the bidi control as visible text
# (F-109: escapes — never stripped, never passed through).
ESCAPED_HOSTILE = "line1\\nline2\\x1b[2J\\u202e"

_SHA = "a" * 40
_PR = "https://github.com/fixture/fixture/pull/1"
_FLOOR = {"max_turns": 30, "wall_clock_min": 45}


def _attempt(**extra) -> dict:
    """One schema-valid escalated attempt record for a fixture task."""
    record = {
        "run_id": "20261123T000000Z-f109",
        "slice_id": "T-1",
        "started": "2026-11-23T00:00:00Z",
        "outcome": "escalated",
        "rounds": 1,
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "prompt_versions": {},
        "skills": [],
    }
    record.update(extra)
    return record


def _queue(tmp_path, task):
    """A one-task queue file on disk, plus its loaded handle.

    Returns ``(path, queue)``: every writer case re-loads through
    :class:`TaskQueue` afterwards — the load IS the assertion.
    """
    path = tmp_path / "tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "project": "fixture", "tasks": [task]}
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path, TaskQueue(path)


def test_ac_1(tmp_path):
    """A failing sentinel's multi-line output is stored escaped: the run
    reports ``sentinel_failed`` (never ``orchestrator_error``), the saved
    queue loads, and the stored detail shows the escapes as text."""
    env = Env72(
        tmp_path / "sentinel-hostile",
        tasks=[_task(risk=["safety"],
                     scope={"paths": ["impl.py", "tests/test_feature.py",
                                      "evil.py"]})],
        profile_extra={
            "paths": {"risk": {"safety": ["evil.py"]}},
            "sentinels": {"safety": [{
                # The hostile text, printable without carrying any raw
                # control in the profile itself (the profile loader would
                # refuse it — F-63): printf renders \\n, \\033 and the
                # UTF-8 bytes of U+202E, then exits non-zero.
                "command":
                    "printf 'line1\\nline2\\033[2J\\342\\200\\256'; exit 1",
                "description": "no-evil invariant",
            }]},
        },
    )
    seats = Seats(impl_files=[{"evil.py": "x = 1\n"}])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "sentinel_failed"
    task = TaskQueue(env.queue_path).get("T-1")  # the saved queue LOADS
    details = [
        sentinel.get("detail")
        for sentinel in task["attempts"][-1]["sentinels"]
        if sentinel.get("verdict") == "fail"
    ]
    assert details == [ESCAPED_HOSTILE]


def test_ac_2(tmp_path):
    """Every listed queue writer of outside text stores the hostile text
    escaped, so the queue each of them writes still loads."""
    # record_followup — the round's L2 findings become the follow-up
    # draft's acceptance criteria (the finding text is session-derived).
    path, queue = _queue(tmp_path / "followup", _task(status="in_review"))
    queue.record_followup(
        "T-1",
        findings=[{"id": "R-1", "severity": "minor", "file": "impl.py",
                   "text": HOSTILE}],
        floor=_FLOOR, remembered=None)
    followup = TaskQueue(path).get("T-1-followup")
    criterion = followup["acceptance"][0]["text"]
    # the one-line renderer collapses the newline; the refused classes
    # must be visible as escapes
    assert "\\x1b[2J\\u202e" in criterion

    # record_wip — kept fix-round work: the run id is run-derived text.
    # The wip schema already patterns sha/parent/run_id, so the hostile
    # text refuses at the save (loud, nothing written) — the writer's
    # escape is belt-and-braces under today's schema.
    path, queue = _queue(
        tmp_path / "wip",
        _task(status="in_progress",
              links={"pr": _PR, "branch": "factory/t-1",
                     "pushed_head": _SHA}))
    with pytest.raises(ContractError):
        queue.record_wip("T-1", sha=_SHA, parent=_SHA, run_id=HOSTILE)
    loaded = TaskQueue(path).get("T-1")
    assert loaded["links"].get("wip") is None

    # recover_revalidated — the pair the sweep proved, read from the
    # remote, is command-derived.
    path, queue = _queue(
        tmp_path / "revalidated",
        _task(status="needs_human",
              attempts=[_attempt(escalation="stale_base")],
              links={"pr": _PR}))
    queue.recover_revalidated("T-1", pr_sha=HOSTILE, default_sha=HOSTILE)
    links = TaskQueue(path).get("T-1")["links"]
    assert links["revalidated"] == {"pr": ESCAPED_HOSTILE,
                                    "default": ESCAPED_HOSTILE}
    assert links["pushed_head"] == ESCAPED_HOSTILE

    # vouch_pushed_head — the owner's vouched head, read from the remote.
    path, queue = _queue(
        tmp_path / "vouch",
        _task(status="in_review", links={"pr": _PR}))
    queue.vouch_pushed_head("T-1", pr_sha=HOSTILE)
    links = TaskQueue(path).get("T-1")["links"]
    assert links["pushed_head"] == ESCAPED_HOSTILE

    # recover_pr_merged — gate-pinned: merged_url must equal links.pr, so
    # the hostile URL is refused as a foreign PR before anything is
    # stored; the queue is untouched and still loads.
    path, queue = _queue(
        tmp_path / "merged",
        _task(status="needs_human",
              attempts=[_attempt(escalation="stale_base")],
              links={"pr": _PR}))
    with pytest.raises(TransitionError):
        queue.recover_pr_merged("T-1", merged_url=HOSTILE)
    assert TaskQueue(path).get("T-1")["links"].get("merged") is None

    # recover_attempts — the owner's reset evidence lands in
    # attempt_resets (a loader free-text key: its newline stays raw, the
    # other refused classes escape).
    path, queue = _queue(
        tmp_path / "reset",
        _task(status="needs_human",
              attempts=[_attempt(escalation="attempts_exhausted")]))
    queue.recover_attempts("T-1", HOSTILE, done_by="owner")
    evidence = TaskQueue(path).get("T-1")["attempt_resets"][-1]["evidence"]
    assert evidence == "line1\nline2\\x1b[2J\\u202e"

    # reject_escalation — the F-107 reset marker, with the owner's name.
    path, queue = _queue(
        tmp_path / "reject",
        _task(status="needs_human",
              attempts=[_attempt(escalation="budget_exhausted")],
              links={"pr": _PR, "escalation_pr": True}))
    queue.reject_escalation("T-1", done_by=HOSTILE)
    marker = TaskQueue(path).get("T-1")["attempt_resets"][-1]
    assert marker["done_by"] == ESCAPED_HOSTILE

    # tick_owner_steps — the owner's evidence and name in owner_steps.
    path, queue = _queue(
        tmp_path / "tick",
        _task(status="awaiting_owner",
              owner_steps=[{"text": "check the diff", "done": False}]))
    queue.tick_owner_steps("T-1", [1], evidence=HOSTILE, done_by="owner")
    step = TaskQueue(path).get("T-1")["owner_steps"][0]
    assert step["evidence"] == "line1\nline2\\x1b[2J\\u202e"
    assert step["done_by"] == "owner"


def test_loader_still_refuses_raw_control(tmp_path):
    """The loader's refusal stands: a hand-written queue carrying a raw
    control character is refused at load (F-63), whatever the writers
    escape — the escape never becomes an excuse to relax it."""
    raw_esc = _task()
    raw_esc["title"] = "Make answer\x1b() return 42"
    path = tmp_path / "raw-esc.json"
    path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [raw_esc]}) + "\n")
    with pytest.raises(ContractError) as excinfo:
        TaskQueue(path)
    assert "U+001B" in str(excinfo.value)

    # a newline in a NON-free-text field refuses too — only note,
    # evidence and text may carry one
    raw_lf = _task()
    raw_lf["title"] = "two\nlines"
    path = tmp_path / "raw-lf.json"
    path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [raw_lf]}) + "\n")
    with pytest.raises(ContractError) as excinfo:
        TaskQueue(path)
    assert "U+000A" in str(excinfo.value)
