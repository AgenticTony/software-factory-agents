"""F-04 acceptance tests — BUILD-ORDER.md, work order F-04.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f04.py::test_ac_N``
means a test function with exactly that name. The extra descriptive tests
below cover paths the acceptance refs don't name: non-draft and
non-executable kinds, declined confirmation, cross-repo dependencies at
approval, the ledger parser against this repo's real BOOTSTRAP.md, and
reconcile edge cases (idempotency, declining, intermediate targets,
unmentioned criteria).
"""

import copy
import json
import shutil
from pathlib import Path

import pytest

from factory.approve import (
    ApprovalDeclined,
    ApprovalError,
    approve_task,
    parse_bootstrap_ledger,
    reconcile_bootstrap,
)
from factory.cli import main
from factory.contract import acceptance_hash
from factory.state import TaskQueue

REPO = Path(__file__).resolve().parent.parent

ACCEPTANCE = [
    {
        "id": "AC-1",
        "text": "it works",
        "check": {"type": "test", "ref": "tests/x.py::test_works"},
    },
    {
        "id": "AC-2",
        "text": "it is nice",
        "check": {"type": "command", "ref": "make check"},
    },
]


def _task(**overrides):
    base = {
        "id": "T-1",
        "title": "fixture task",
        "source": "docs/fixture.md#t-1",
        "kind": "build",
        "priority": "P2",
        "status": "draft",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": copy.deepcopy(ACCEPTANCE),
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 80, "wall_clock_min": 60},
    }
    base.update(overrides)
    return base


def _queue_file(tmp_path, tasks, name="tasks.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"version": 1, "project": "fixture", "tasks": tasks}, indent=2)
    )
    return path


def _ledger_file(tmp_path, text, name="BOOTSTRAP.md"):
    path = tmp_path / name
    path.write_text(text)
    return path


def say_yes(_question):
    return True


def say_no(_question):
    return False


LEDGER = """# Bootstrap ledger

Intro prose that must be ignored by the parser.

---

## F-1 — first task

- **Commits:** `abc123` on main.
- **Built:** 2026-10-16.
- **AC-1** proven: `tests/test_x.py::test_ac_1` passes.
- **AC-2** proven: CI green on PR #1.
- **Status to set at reconcile:** `done`. No deploy target.

---

## F-2 — second task

- **AC-1** proven: `tests/test_x.py::test_ac_1` passes.
- **AC-2** proven: CI green on PR #2.
- **Status to set at reconcile:** `done`.

---

## F-3 — pending task

- **AC-1** proven: passes.
- **AC-2** pending: waiting on CI.
- **Status to set at reconcile:** `done`.
"""


# --- AC-1: no acceptance items -> refused --------------------------------


def test_ac_1(tmp_path):
    path = _queue_file(tmp_path, [_task(acceptance=[])])
    before = path.read_text()
    queue = TaskQueue(path)
    with pytest.raises(ApprovalError, match="no acceptance items"):
        approve_task(queue, "T-1", approved_by="Tester", assume_yes=True)
    assert queue.get("T-1")["status"] == "draft"
    assert path.read_text() == before


# --- AC-2: ready or blocked per dependencies -----------------------------


def test_ac_2(tmp_path):
    # No dependencies -> ready, with the contract frozen on approval.
    path = _queue_file(tmp_path, [_task()])
    queue = TaskQueue(path)
    assert approve_task(queue, "T-1", approved_by="Tester", assume_yes=True) == "ready"
    task = TaskQueue(path).get("T-1")
    assert task["status"] == "ready"
    assert task["acceptance_hash"] == acceptance_hash(ACCEPTANCE)
    assert task["approved_by"] == "Tester"
    assert task["approved_at"]

    # An open same-repo dependency -> blocked.
    path = _queue_file(
        tmp_path,
        [_task(id="T-DEP", status="draft"), _task(id="T-2", depends_on=["T-DEP"])],
    )
    queue = TaskQueue(path)
    assert approve_task(queue, "T-2", approved_by="Tester", assume_yes=True) == "blocked"
    assert TaskQueue(path).get("T-2")["status"] == "blocked"

    # A done dependency -> ready.
    path = _queue_file(
        tmp_path,
        [
            _task(id="T-DEP", status="done", acceptance_hash="x", approved_at="y"),
            _task(id="T-2", depends_on=["T-DEP"]),
        ],
    )
    queue = TaskQueue(path)
    assert approve_task(queue, "T-2", approved_by="Tester", assume_yes=True) == "ready"


# --- AC-3: --yes refused for risk-flagged tasks --------------------------


def test_ac_3(tmp_path):
    path = _queue_file(tmp_path, [_task(risk=["auth", "payments"])])
    before = path.read_text()
    queue = TaskQueue(path)
    with pytest.raises(ApprovalError, match="auth, payments"):
        approve_task(queue, "T-1", approved_by="Tester", assume_yes=True)
    assert queue.get("T-1")["status"] == "draft"
    assert path.read_text() == before

    # Interactive confirmation is the only way to approve a risk-flagged task.
    assert (
        approve_task(queue, "T-1", approved_by="Tester", confirm=say_yes) == "ready"
    )
    assert TaskQueue(path).get("T-1")["status"] == "ready"


# --- AC-4: --reconcile-bootstrap ------------------------------------------


def test_ac_4(tmp_path):
    tasks = [
        _task(id="F-1"),
        _task(id="F-2", depends_on=["F-1"]),
        _task(id="F-3", depends_on=["F-2"]),
    ]
    path = _queue_file(tmp_path, tasks)
    ledger = _ledger_file(tmp_path, LEDGER)
    asked: list[str] = []

    def confirm(question):
        asked.append(question)
        return True

    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=confirm
    )

    # One interactive confirmation per entry that gets that far: the
    # pending entry is refused before any question is asked.
    assert len(asked) == 2

    # F-1 and F-2 reconcile through legal transitions to done.
    assert [r.action for r in results] == ["reconciled", "reconciled", "failed"]
    on_disk = {t["id"]: t for t in json.loads(path.read_text())["tasks"]}
    assert on_disk["F-1"]["status"] == "done"
    assert on_disk["F-2"]["status"] == "done"
    for tid in ("F-1", "F-2"):
        assert on_disk[tid]["acceptance_hash"] == acceptance_hash(ACCEPTANCE)
        assert on_disk[tid]["approved_by"] == "Tester"

    # A criterion marked pending can never produce done: the task stays
    # draft, untouched.
    assert on_disk["F-3"]["status"] == "draft"
    assert "acceptance_hash" not in on_disk["F-3"]
    assert "pending" in results[2].detail


# --- Extra: approval refusals ----------------------------------------------


def test_non_draft_task_cannot_be_reapproved(tmp_path):
    path = _queue_file(
        tmp_path,
        [
            _task(
                status="ready",
                acceptance_hash=acceptance_hash(ACCEPTANCE),
                approved_at="2026-10-16T00:00:00Z",
                approved_by="Before",
            )
        ],
    )
    before = path.read_text()
    queue = TaskQueue(path)
    with pytest.raises(ApprovalError, match="only draft"):
        approve_task(queue, "T-1", approved_by="Tester", assume_yes=True)
    assert path.read_text() == before


def test_non_executable_kind_cannot_be_approved(tmp_path):
    path = _queue_file(tmp_path, [_task(kind="decision")])
    with pytest.raises(ApprovalError, match="decision"):
        approve_task(TaskQueue(path), "T-1", approved_by="Tester", assume_yes=True)


def test_declined_confirmation_writes_nothing(tmp_path):
    path = _queue_file(tmp_path, [_task()])
    before = path.read_text()
    queue = TaskQueue(path)
    with pytest.raises(ApprovalDeclined):
        approve_task(queue, "T-1", approved_by="Tester", confirm=say_no)
    assert path.read_text() == before


def test_cross_repo_dependency_blocks(tmp_path):
    path = _queue_file(tmp_path, [_task(depends_on=["other-repo:WO-9"])])
    queue = TaskQueue(path)
    assert (
        approve_task(queue, "T-1", approved_by="Tester", assume_yes=True) == "blocked"
    )


def test_contract_is_printed_before_confirmation(tmp_path):
    path = _queue_file(tmp_path, [_task()])
    printed: list[str] = []
    approve_task(
        TaskQueue(path),
        "T-1",
        approved_by="Tester",
        assume_yes=True,
        echo=printed.append,
    )
    text = "".join(printed)
    assert "T-1" in text and "fixture task" in text
    assert "AC-1" in text and "it works" in text and "tests/x.py::test_works" in text


# --- Extra: ledger parsing --------------------------------------------------


def test_parser_reads_this_repos_ledger():
    entries = parse_bootstrap_ledger((REPO / ".factory" / "BOOTSTRAP.md").read_text())
    by_id = {e["id"]: e for e in entries}
    assert set(by_id) >= {"F-01", "F-02", "F-03"}
    for entry_id in ("F-01", "F-02", "F-03"):
        assert by_id[entry_id]["status"] == "done"
        assert by_id[entry_id]["criteria"]
        assert all(
            marker == "proven" for marker in by_id[entry_id]["criteria"].values()
        ), f"{entry_id} should be fully proven by now"


def test_parser_normalises_not_met_marker(tmp_path):
    ledger = _ledger_file(
        tmp_path,
        "## W-1 — x\n\n- **AC-1** not met: reproduced.\n"
        "- **Status to set at reconcile:** `needs_human`.\n",
    )
    entry = parse_bootstrap_ledger(ledger.read_text())[0]
    assert entry["criteria"] == {"AC-1": "not met"}
    assert entry["status"] == "needs_human"


# --- Extra: reconcile edge cases -------------------------------------------


def test_reconcile_unmentioned_criterion_never_reaches_done(tmp_path):
    # AC-2 exists on the task but the ledger entry never mentions it: the
    # criterion is not proven, so done is refused.
    ledger = _ledger_file(
        tmp_path,
        "## T-1 — x\n\n- **AC-1** proven: yes.\n"
        "- **Status to set at reconcile:** `done`.\n",
    )
    path = _queue_file(tmp_path, [_task()])
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert results[0].action == "failed"
    assert TaskQueue(path).get("T-1")["status"] == "draft"


def test_reconcile_declined_entry_is_skipped_and_dependents_fail(tmp_path):
    tasks = [
        _task(id="F-1"),
        _task(id="F-2", depends_on=["F-1"]),
        _task(id="F-3", depends_on=["F-2"]),
    ]
    path = _queue_file(tmp_path, tasks)
    ledger = _ledger_file(tmp_path, LEDGER)

    def confirm(question):
        return "F-2" not in question

    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=confirm
    )
    assert [r.action for r in results] == ["reconciled", "skipped", "failed"]
    on_disk = {t["id"]: t["status"] for t in json.loads(path.read_text())["tasks"]}
    assert on_disk == {"F-1": "done", "F-2": "draft", "F-3": "draft"}


def test_reconcile_is_idempotent_on_a_second_run(tmp_path):
    tasks = [
        _task(id="F-1"),
        _task(id="F-2", depends_on=["F-1"]),
        _task(id="F-3", depends_on=["F-2"]),
    ]
    path = _queue_file(tmp_path, tasks)
    ledger = _ledger_file(tmp_path, LEDGER)
    reconcile_bootstrap(TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes)
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert [r.action for r in results] == ["skipped", "skipped", "failed"]
    on_disk = {t["id"]: t["status"] for t in json.loads(path.read_text())["tasks"]}
    assert on_disk == {"F-1": "done", "F-2": "done", "F-3": "draft"}


def test_reconcile_walks_to_an_intermediate_status(tmp_path):
    ledger = _ledger_file(
        tmp_path,
        "## T-1 — x\n\n- **AC-1** proven: yes.\n- **AC-2** proven: yes.\n"
        "- **Status to set at reconcile:** `in_review`.\n",
    )
    path = _queue_file(tmp_path, [_task()])
    reconcile_bootstrap(TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes)
    task = TaskQueue(path).get("T-1")
    assert task["status"] == "in_review"
    assert task["approved_by"] == "Tester"


def test_reconcile_risk_flagged_entry_confirms_interactively(tmp_path):
    # Reconcile has no --yes at all (see the CLI test below); a
    # risk-flagged entry reconciles after its interactive confirmation,
    # mirroring AC-3's interactive path for single approvals.
    ledger = _ledger_file(
        tmp_path,
        "## T-1 — x\n\n- **AC-1** proven: yes.\n- **AC-2** proven: yes.\n"
        "- **Status to set at reconcile:** `done`.\n",
    )
    path = _queue_file(tmp_path, [_task(risk=["payments"])])
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert results[0].action == "reconciled"
    assert TaskQueue(path).get("T-1")["status"] == "done"


# --- Extra: review-round regressions (PR #7 findings) ----------------------


def test_reconcile_failure_writes_nothing(tmp_path):
    # Reviewer repro: an entry refused partway (an unmerged slice can't
    # reach done) must leave the file byte-identical — no approved task
    # stranded at an intermediate status that a re-run then skips.
    task = _task(slices=[{"id": "S-1", "text": "only slice", "status": "todo"}])
    path = _queue_file(tmp_path, [task])
    ledger = _ledger_file(
        tmp_path,
        "## T-1 — x\n\n- **AC-1** proven: yes.\n- **AC-2** proven: yes.\n"
        "- **Status to set at reconcile:** `done`.\n",
    )
    before = path.read_text()
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert results[0].action == "failed"
    assert "slice" in results[0].detail
    assert path.read_text() == before


def test_reconcile_routes_open_owner_steps_to_awaiting_owner(tmp_path):
    # Reviewer repro: a recorded done with owner steps still open is
    # awaiting_owner (TASK-CONTRACT §3), never done.
    task = _task(owner_steps=[{"text": "answer Z.ai before scheduling", "done": False}])
    path = _queue_file(tmp_path, [task])
    ledger = _ledger_file(
        tmp_path,
        "## T-1 — x\n\n- **AC-1** proven: yes.\n- **AC-2** proven: yes.\n"
        "- **Status to set at reconcile:** `done`.\n",
    )
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert results[0].action == "reconciled"
    assert "owner steps are open" in results[0].detail
    on_disk = TaskQueue(path).get("T-1")
    assert on_disk["status"] == "awaiting_owner"
    assert on_disk["acceptance_hash"] == acceptance_hash(ACCEPTANCE)


@pytest.mark.parametrize(
    "target", ["in_review", "deploying", "awaiting_owner", "done"]
)
def test_reconcile_pending_criteria_block_post_build_targets(tmp_path, target):
    # Every recorded status that asserts the build passed validation
    # requires all criteria proven — not just done.
    path = _queue_file(tmp_path, [_task()])
    ledger = _ledger_file(
        tmp_path,
        "## T-1 — x\n\n- **AC-1** proven: yes.\n- **AC-2** pending: not yet.\n"
        f"- **Status to set at reconcile:** `{target}`.\n",
    )
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert results[0].action == "failed"
    assert "pending" in results[0].detail
    assert TaskQueue(path).get("T-1")["status"] == "draft"


def test_reconcile_pending_criteria_can_still_escalate(tmp_path):
    # needs_human is the honest recorded status for unproven work.
    path = _queue_file(tmp_path, [_task()])
    ledger = _ledger_file(
        tmp_path,
        "## T-1 — x\n\n- **AC-1** proven: yes.\n- **AC-2** pending: not yet.\n"
        "- **Status to set at reconcile:** `needs_human`.\n",
    )
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert results[0].action == "reconciled"
    assert TaskQueue(path).get("T-1")["status"] == "needs_human"


def test_reconcile_writes_once_per_entry(tmp_path, monkeypatch):
    # One atomic save per entry: no intermediate statuses hit disk (the
    # crash window the reviewer's stranding repro exploited) and no
    # transient in_progress without the run lease.
    tasks = [_task(id="F-1"), _task(id="F-2", depends_on=["F-1"])]
    path = _queue_file(tmp_path, tasks)
    ledger = _ledger_file(tmp_path, LEDGER)
    saves = []
    original = TaskQueue.save

    def counting_save(self):
        saves.append(1)
        original(self)

    monkeypatch.setattr(TaskQueue, "save", counting_save)
    results = reconcile_bootstrap(
        TaskQueue(path), ledger, approved_by="Tester", confirm=say_yes
    )
    assert [r.action for r in results[:2]] == ["reconciled", "reconciled"]
    assert TaskQueue(path).get("F-2")["status"] == "done"
    assert len(saves) == 2


def test_parser_cleans_heading_punctuation():
    entries = parse_bootstrap_ledger(
        "## F-05: Clone manager\n\n- **AC-1** proven: yes.\n"
        "- **Status to set at reconcile:** `done`.\n"
    )
    assert entries[0]["id"] == "F-05"
    assert entries[0]["status"] == "done"


def test_parser_surfaces_a_malformed_status_line():
    # A status marker without parseable backticks still counts as an
    # entry (status None), so reconcile fails it visibly instead of the
    # work order silently staying draft.
    entries = parse_bootstrap_ledger(
        "## W-9 — x\n\n- **Status to set at reconcile:** done, no backticks\n"
    )
    assert entries and entries[0]["id"] == "W-9"
    assert entries[0]["status"] is None


def test_eof_on_prompt_declines(tmp_path, monkeypatch):
    # No TTY / closed stdin is a decline, never an EOFError traceback.
    def raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    path = _queue_file(tmp_path, [_task()])
    queue = TaskQueue(path)
    with pytest.raises(ApprovalDeclined):
        approve_task(queue, "T-1", approved_by="Tester")
    assert TaskQueue(path).get("T-1")["status"] == "draft"


def test_reconcile_on_this_repos_real_files(tmp_path):
    # The real BOOTSTRAP.md against a copy of the real queue: the merged,
    # fully-proven prefix F-01..F-03 reconciles to done in dependency
    # order. Entries beyond that depend on where the bootstrap stands
    # (their PR may not be merged yet), so their outcomes aren't pinned;
    # F-00 (owner_only, never in the ledger) is left exactly as it was —
    # draft before its steps are ticked, done after (F-29).
    queue_path = tmp_path / "tasks.json"
    shutil.copy(REPO / ".factory" / "tasks.json", queue_path)
    f00_before = next(t for t in json.loads(queue_path.read_text())["tasks"] if t["id"] == "F-00")
    ledger = tmp_path / "BOOTSTRAP.md"
    shutil.copy(REPO / ".factory" / "BOOTSTRAP.md", ledger)
    results = reconcile_bootstrap(
        TaskQueue(queue_path), ledger, approved_by="Tester", confirm=say_yes
    )
    # On a fresh queue the prefix reconciles; once the real queue has been
    # reconciled (2026-10-19) the same entries skip as already done. Either
    # way they are handled in dependency order and end done.
    actions = {r.task_id: r.action for r in results}
    for tid in ("F-01", "F-02", "F-03"):
        assert actions[tid] in ("reconciled", "skipped"), (tid, actions[tid])
    handled = [r.task_id for r in results if r.action in ("reconciled", "skipped")]
    assert handled[:3] == ["F-01", "F-02", "F-03"]
    on_disk = {t["id"]: t for t in json.loads(queue_path.read_text())["tasks"]}
    assert on_disk["F-01"]["status"] == "done"
    assert on_disk["F-02"]["status"] == "done"
    assert on_disk["F-03"]["status"] == "done"
    assert on_disk["F-00"] == f00_before


# --- Extra: CLI ------------------------------------------------------------


def test_cli_approves_with_yes(tmp_path, capsys):
    path = _queue_file(tmp_path, [_task()])
    code = main(["approve", "T-1", "--queue", str(path), "--yes", "--approved-by", "CI"])
    assert code == 0
    task = TaskQueue(path).get("T-1")
    assert task["status"] == "ready" and task["approved_by"] == "CI"
    assert "approved -> ready" in capsys.readouterr().out


def test_cli_refuses_yes_on_risk(tmp_path, capsys):
    path = _queue_file(tmp_path, [_task(risk=["auth"])])
    code = main(["approve", "T-1", "--queue", str(path), "--yes"])
    assert code == 1
    assert "auth" in capsys.readouterr().err
    assert TaskQueue(path).get("T-1")["status"] == "draft"


def test_cli_reconcile_bootstrap(tmp_path, capsys, monkeypatch):
    queue_path = tmp_path / "tasks.json"
    shutil.copy(REPO / ".factory" / "tasks.json", queue_path)
    ledger = tmp_path / "BOOTSTRAP.md"
    shutil.copy(REPO / ".factory" / "BOOTSTRAP.md", ledger)
    f00_before = next(t for t in json.loads(queue_path.read_text())["tasks"] if t["id"] == "F-00")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    code = main(
        [
            "approve",
            "--reconcile-bootstrap",
            "--queue",
            str(queue_path),
            "--ledger",
            str(ledger),
        ]
    )
    # Exit is 0 unless some entry failed; entries beyond the merged
    # F-01..F-03 prefix depend on bootstrap state, so only the prefix
    # and the never-reconciled owner task are pinned.
    tasks = {t["id"]: t for t in json.loads(queue_path.read_text())["tasks"]}
    assert tasks["F-01"]["status"] == "done" and tasks["F-03"]["status"] == "done"
    assert tasks["F-00"] == f00_before
    out = capsys.readouterr().out
    for tid in ("F-01", "F-03"):
        assert f"{tid}: reconciled" in out or f"{tid}: skipped — already done" in out
    assert code == (1 if ": failed" in out else 0)


def test_cli_requires_a_task_id_or_reconcile():
    with pytest.raises(SystemExit):
        main(["approve"])
    with pytest.raises(SystemExit):
        main(["approve", "T-1", "--reconcile-bootstrap"])


def test_cli_refuses_yes_with_reconcile_bootstrap():
    # AC-4: reconcile confirms every entry interactively; --yes would
    # turn plain markdown into done tasks with nobody reading them.
    with pytest.raises(SystemExit):
        main(["approve", "--reconcile-bootstrap", "--yes"])


def test_cli_eof_on_prompt_exits_cleanly(tmp_path, capsys, monkeypatch):
    def raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    path = _queue_file(tmp_path, [_task()])
    code = main(["approve", "T-1", "--queue", str(path)])
    assert code == 1
    assert "declined" in capsys.readouterr().err
    assert TaskQueue(path).get("T-1")["status"] == "draft"
