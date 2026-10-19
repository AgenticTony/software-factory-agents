"""F-29 acceptance tests — BUILD-ORDER.md, work order F-29 (owner-step tick).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f29.py::test_ac_N``
means a test function with exactly that name. Extra tests cover the CLI
wiring and the reconcile interaction on this repo's real files.
"""

import copy
import json
import shutil
from pathlib import Path

import pytest

from factory.approve import reconcile_bootstrap
from factory.cli import main
from factory.contract import ContractError, acceptance_hash, load_tasks
from factory.state import TaskQueue, TransitionError
from factory.tick import TickError, tick_steps

REPO = Path(__file__).resolve().parent.parent

ACCEPTANCE = [
    {"id": "AC-1", "text": "it works", "check": {"type": "test", "ref": "tests/x.py::test_works"}},
]


def _steps(*texts):
    return [{"text": text, "done": False} for text in texts]


def _task(**overrides):
    base = {
        "id": "T-1",
        "title": "fixture task",
        "source": "docs/fixture.md#t-1",
        "kind": "build",
        "priority": "P2",
        "status": "awaiting_owner",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": copy.deepcopy(ACCEPTANCE),
        "acceptance_hash": acceptance_hash(ACCEPTANCE),
        "approved_at": "2026-10-19T00:00:00Z",
        "approved_by": "Tester",
        "owner_steps": _steps("deploy it", "confirm a real send"),
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 80, "wall_clock_min": 60},
    }
    base.update(overrides)
    return base


def _owner_only(**overrides):
    base = {
        "id": "O-1",
        "title": "owner prerequisites",
        "source": "docs/fixture.md#o-1",
        "kind": "owner_only",
        "priority": "P1",
        "status": "draft",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [],
        "owner_steps": _steps("email support", "turn on branch protection"),
        "budget": {"size": "S", "max_attempts": 1, "max_turns": 80, "wall_clock_min": 60},
    }
    base.update(overrides)
    return base


def _queue_file(tmp_path, tasks):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"version": 1, "project": "fixture", "tasks": tasks}, indent=2))
    return path


def _tick(path, task_id, steps, evidence="checked 2026-10-19", confirm=lambda _q: True):
    return tick_steps(TaskQueue(path), task_id, steps, evidence=evidence,
                      done_by="Tester", confirm=confirm)


def _on_disk(path, task_id):
    return next(t for t in json.loads(path.read_text())["tasks"] if t["id"] == task_id)


def test_ac_1(tmp_path, monkeypatch, capsys):
    """AC-1: a confirmed tick records done, done_at, done_by and evidence."""
    path = _queue_file(tmp_path, [_task(status="in_review")])
    asked = []
    _tick(path, "T-1", [2], confirm=lambda q: asked.append(q) or True)
    assert len(asked) == 1 and "confirm a real send" in asked[0]
    step1, step2 = _on_disk(path, "T-1")["owner_steps"]
    assert step1 == {"text": "deploy it", "done": False}
    assert step2["done"] is True
    assert step2["done_by"] == "Tester"
    assert step2["evidence"] == "checked 2026-10-19"
    assert step2["done_at"].endswith("Z")

    # done: true without the audit fields is refused at load.
    bare_dir = tmp_path / "bare"
    bare_dir.mkdir()
    bare = _task(owner_steps=[{"text": "deploy it", "done": True}], status="in_review")
    with pytest.raises(ContractError):
        load_tasks(_queue_file(bare_dir, [bare]))

    # CLI: one confirmation per named step, evidence required, no --yes.
    path = _queue_file(tmp_path, [_task(status="in_review")])
    answers = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    code = main(["tick", "T-1", "1", "2", "--queue", str(path),
                 "--evidence", "both done", "--done-by", "Owner"])
    assert code == 0
    assert all(s["done"] and s["done_by"] == "Owner" for s in _on_disk(path, "T-1")["owner_steps"])
    with pytest.raises(SystemExit):
        main(["tick", "T-1", "1", "--queue", str(path)])  # --evidence missing
    with pytest.raises(SystemExit):
        main(["tick", "T-1", "1", "--queue", str(path), "--evidence", "x", "--yes"])


@pytest.mark.parametrize(
    ("task_id", "steps", "evidence", "answer", "needle"),
    [
        ("NOPE", [1], "e", True, "unknown task"),
        ("T-1", [3], "e", True, "out of range"),
        ("T-1", [0], "e", True, "out of range"),
        ("T-1", [1], "   ", True, "evidence"),
        ("T-1", [1], "e", False, "declined"),
        ("T-1", [1, 3], "e", True, "out of range"),  # one bad step writes none
    ],
)
def test_ac_2(tmp_path, task_id, steps, evidence, answer, needle):
    """AC-2: every refusal leaves tasks.json byte-identical, names the problem."""
    path = _queue_file(tmp_path, [_task(status="in_review")])
    before = path.read_bytes()
    with pytest.raises(TickError, match=needle):
        _tick(path, task_id, steps, evidence=evidence, confirm=lambda _q: answer)
    assert path.read_bytes() == before


def test_ac_2_already_ticked(tmp_path):
    """AC-2: an already-ticked step is refused and nothing is written."""
    path = _queue_file(tmp_path, [_task(status="in_review")])
    _tick(path, "T-1", [1])
    before = path.read_bytes()
    with pytest.raises(TickError, match="already ticked"):
        _tick(path, "T-1", [1])
    assert path.read_bytes() == before


def test_ac_2_cli_refusal_exits_nonzero(tmp_path, monkeypatch, capsys):
    path = _queue_file(tmp_path, [_task(status="in_review")])
    before = path.read_bytes()
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["tick", "T-1", "1", "--queue", str(path), "--evidence", "e"]) == 1
    assert "declined" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_ac_3(tmp_path):
    """AC-3: the last open step of awaiting_owner closes the task; others never move."""
    path = _queue_file(tmp_path, [_task()])
    assert _tick(path, "T-1", [1]) == "awaiting_owner"
    assert _on_disk(path, "T-1")["status"] == "awaiting_owner"
    assert _tick(path, "T-1", [2]) == "done"
    assert _on_disk(path, "T-1")["status"] == "done"

    for status in ("ready", "in_progress", "in_review", "deploying"):
        path = _queue_file(tmp_path, [_task(status=status)])
        assert _tick(path, "T-1", [1, 2]) == status
        assert _on_disk(path, "T-1")["status"] == status


def test_ac_4(tmp_path):
    """AC-4: owner_only closes draft -> not_buildable -> done; build/docs never can."""
    path = _queue_file(tmp_path, [_owner_only()])
    assert _tick(path, "O-1", [1]) == "draft"
    assert _tick(path, "O-1", [2]) == "done"
    assert _on_disk(path, "O-1")["status"] == "done"

    path = _queue_file(tmp_path, [_owner_only(status="not_buildable")])
    assert _tick(path, "O-1", [1, 2]) == "done"


@pytest.mark.parametrize("kind", ["build", "docs"])
def test_ac_4_executable_kind_cannot_enter_not_buildable(tmp_path, kind):
    task = _task(kind=kind, status="draft", owner_steps=[])
    for field in ("acceptance_hash", "approved_at", "approved_by"):
        task.pop(field)
    queue = TaskQueue(_queue_file(tmp_path, [task]))
    with pytest.raises(TransitionError, match="not_buildable"):
        queue.transition("T-1", "not_buildable")


@pytest.mark.parametrize("kind", ["build", "docs"])
def test_ac_4_executable_kind_cannot_leave_not_buildable_to_done(tmp_path, kind):
    # Hand-written state the importer would never produce: an executable
    # kind parked in not_buildable must still not be able to reach done.
    task = _task(kind=kind, status="not_buildable", owner_steps=[])
    queue = TaskQueue(_queue_file(tmp_path, [task]))
    with pytest.raises(TransitionError, match="owner_only"):
        queue.transition("T-1", "done")


def test_ac_4_owner_only_done_on_disk_needs_ticked_steps(tmp_path):
    # Written by hand, an owner_only task marked done with an open step (or
    # no steps) is refused at load: done has to mean every step was ticked.
    for steps in (_steps("email support"), []):
        path = _queue_file(tmp_path, [_owner_only(status="done", owner_steps=steps)])
        with pytest.raises(ContractError):
            load_tasks(path)


def test_ac_4_owner_only_needs_every_step_ticked(tmp_path):
    queue = TaskQueue(_queue_file(tmp_path, [_owner_only(status="not_buildable")]))
    with pytest.raises(TransitionError):
        queue.transition("O-1", "done")
    queue = TaskQueue(_queue_file(tmp_path, [_owner_only(status="not_buildable", owner_steps=[])]))
    with pytest.raises(TransitionError, match="at least one"):
        queue.transition("O-1", "done")


def test_ac_5(tmp_path):
    """AC-5: reconcile on the real files leaves owner_only tasks exactly as they were."""
    queue_path = tmp_path / "tasks.json"
    shutil.copy(REPO / ".factory" / "tasks.json", queue_path)
    ledger = tmp_path / "BOOTSTRAP.md"
    shutil.copy(REPO / ".factory" / "BOOTSTRAP.md", ledger)
    before = {t["id"]: t for t in json.loads(queue_path.read_text())["tasks"]
              if t["kind"] == "owner_only"}
    assert "F-00" in before
    reconcile_bootstrap(TaskQueue(queue_path), ledger, approved_by="Tester",
                        confirm=lambda _q: True)
    after = {t["id"]: t for t in json.loads(queue_path.read_text())["tasks"]
             if t["kind"] == "owner_only"}
    assert after == before

    # The F-04 real-files tests no longer pin F-00 to draft.
    f04 = (REPO / "tests" / "test_f04.py").read_text()
    assert 'on_disk["F-00"]["status"] == "draft"' not in f04
    assert 'statuses["F-00"] == "draft"' not in f04
