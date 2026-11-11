"""Reconcile of already-built work, and the bootstrap ledger's grammar.

Two guards, found when the F-43..F-65 entries would not reconcile
(2026-11-10):

* F-65 made approval refuse a build/docs contract without a declared
  file scope. Reconcile runs the same approval check, so it refused
  work that was ALREADY built and merged — work no session will ever run
  again. The scope is a run-time boundary: it is demanded when a
  reconcile makes a task runnable (``ready``/``approved``/``blocked``),
  not when it records finished work (``in_review`` and beyond). A
  finished task that is later re-opened still meets F-65's run-time
  refusal (``scope_missing``) before any session starts.
* Every bootstrap entry since F-45 drifted from the ledger grammar
  (status line and per-criterion markers), so ``factory approve
  --reconcile-bootstrap`` failed them all and nothing noticed. The
  repo's own ledger is now reconciled in a dry run on every suite run:
  a copy of the real queue, a throwaway approval ledger, every entry
  confirmed — and no entry may fail.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from factory.approve import parse_bootstrap_ledger, reconcile_bootstrap
from factory.contract import NON_EXECUTABLE_KINDS
from factory.state import TaskQueue

REPO = Path(__file__).resolve().parent.parent


def _task(status: str = "draft", **extra) -> dict:
    task = {
        "id": "T-1",
        "title": "built work",
        "source": "BUILD-ORDER.md#t-1",
        "kind": "build",
        "priority": "P1",
        "status": status,
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [
            {"id": "AC-1", "text": "one",
             "check": {"type": "test", "ref": "tests/test_t1.py::test_ac_1"}},
        ],
        "attempts": [],
        "links": {},
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }
    task.update(extra)
    return task


def _reconcile(tmp_path: Path, ledger_text: str, task: dict):
    queue_path = tmp_path / "tasks.json"
    queue_path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [task]}, indent=2) + "\n")
    ledger = tmp_path / "BOOTSTRAP.md"
    ledger.write_text(ledger_text)
    results = reconcile_bootstrap(
        TaskQueue(queue_path), ledger, approved_by="owner",
        confirm=lambda _q: True, echo=lambda _t: None,
        factory_root=tmp_path / "root",
    )
    return results, TaskQueue(queue_path).get("T-1")


LEDGER = (
    "## T-1 — built work\n\n"
    "- **AC-1** proven: tests/test_t1.py::test_ac_1 green\n\n"
    "**Status to set at reconcile:** `{status}`\n"
)


@pytest.mark.parametrize("target", ["done", "in_review"])
def test_scopeless_built_work_reconciles(tmp_path: Path, target: str):
    # Finished work records history: no session will run it, so the
    # F-65 run-time boundary is not demanded here.
    results, task = _reconcile(tmp_path, LEDGER.format(status=target), _task())
    assert [r.action for r in results] == ["reconciled"], results
    assert task["status"] == target


@pytest.mark.parametrize("target", ["ready", "approved"])
def test_scopeless_reconcile_to_runnable_refuses(tmp_path: Path, target: str):
    # A reconcile that makes the task runnable is an approval of future
    # work: F-65's scope rule holds exactly as for `factory approve`.
    results, task = _reconcile(tmp_path, LEDGER.format(status=target), _task())
    assert [r.action for r in results] == ["failed"], results
    assert "scope" in results[0].detail
    assert task["status"] == "draft"


def test_scoped_reconcile_to_runnable_passes(tmp_path: Path):
    results, task = _reconcile(
        tmp_path, LEDGER.format(status="ready"),
        _task(scope={"paths": ["src/**"]}),
    )
    assert [r.action for r in results] == ["reconciled"], results
    assert task["status"] == "ready"


# Entries that are honestly NOT reconcilable yet, each waiting on the
# owner — named here so they can never be forgotten silently. Remove an
# id once its owner step lands (the test then demands it reconciles).
OWNER_PENDING = {
    "F-48": "AC-5 is manual: the F-16 seeded eval re-run and new baseline",
    "F-57": "AC-4 is manual: one real (paid) `factory eval --importer` run",
    "F-58": "the stored AC text predates the 2026-11-08 rescope (26.04 "
            "deferred to F-61); the owner amends the contract first",
    "F-59": "AC-7 is manual: one real (paid) bedrock session on the "
            "rehearsal fixture (BOOTSTRAP §F-59 runbook)",
}


def test_the_repo_ledger_reconciles_cleanly(tmp_path: Path):
    # The real .factory/BOOTSTRAP.md against a copy of the real queue,
    # with a throwaway approval ledger: every entry either reconciles or
    # is skipped (already past draft) — none fails, except the named
    # owner-pending ones, which must fail ONLY on their pending
    # criterion. A new entry that drifts from the grammar (status line,
    # per-criterion markers) fails here, in CI, instead of at the
    # owner's reconcile.
    queue_path = tmp_path / "tasks.json"
    shutil.copy(REPO / ".factory" / "tasks.json", queue_path)
    results = reconcile_bootstrap(
        TaskQueue(queue_path), REPO / ".factory" / "BOOTSTRAP.md",
        approved_by="dry-run", confirm=lambda _q: True,
        echo=lambda _t: None, factory_root=tmp_path / "root",
    )
    failed = {r.task_id: r.detail for r in results if r.action == "failed"}
    unexpected = {tid: d for tid, d in failed.items() if tid not in OWNER_PENDING}
    assert not unexpected, unexpected
    for tid, detail in failed.items():
        assert "pending" in detail, (tid, detail)
    # A listed id that now reconciles (or is past draft) must leave the
    # list: the exception never outlives its reason.
    stale = sorted(set(OWNER_PENDING) - set(failed))
    assert not stale, f"no longer owner-pending, drop from OWNER_PENDING: {stale}"


def test_every_task_heading_is_a_ledger_entry():
    # A `## F-NN` section with neither a status line nor criterion
    # markers is parsed as prose and silently dropped — F-48, F-49,
    # F-51, F-57 and F-58 sat merged-but-draft that way. Every heading
    # naming a real task must parse as an entry with a recorded status.
    # owner_only (and the other non-executable kinds) close through
    # `factory tick`, never reconcile, so their sections carry no status.
    task_ids = {
        t["id"] for t in json.loads(
            (REPO / ".factory" / "tasks.json").read_text())["tasks"]
        if t.get("kind") not in NON_EXECUTABLE_KINDS
    }
    text = (REPO / ".factory" / "BOOTSTRAP.md").read_text()
    entries = {e["id"]: e for e in parse_bootstrap_ledger(text)}
    headed = {
        match.group(1) for match in re.finditer(r"^##\s+(\S+)", text, re.M)
        if match.group(1) in task_ids
    }
    missing = sorted(tid for tid in headed
                     if tid not in entries or entries[tid]["status"] is None)
    assert not missing, f"task sections with no parseable status: {missing}"
