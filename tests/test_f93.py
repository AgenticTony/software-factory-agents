"""F-93: a merged dependency unblocks its dependents.

On a profile with a deploy target, a merged task waits in ``in_review``
for F-14's live verification, and the dependency rule used to accept
only ``done`` — so every ``depends_on`` there waited forever. A
same-repo dependency is now also met when its target is merged:
status ``in_review``/``deploying``, ``links.merged == links.pr`` (the
one ``_is_merged`` test) and every slice ``merged``. ``done`` keeps
its F-14 meaning. Meeting a dependency early is the costlier failure,
so every doubtful shape stays unmet (AC-2).

Real ``TaskQueue`` files, the F-72 fixture project (real git, real
L0/L1 sandboxes, scripted seats), no network.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
from pathlib import Path

import pytest
from _support.f72 import Env72, Seats, _task

from factory import orchestrator, state, status
from factory.approve import approve_task
from factory.orchestrator import _close_merged_tasks
from factory.state import TaskQueue, _requeue_target, unmet_dependencies
from factory.status import render_status, render_task_status

REPO_ROOT = Path(__file__).resolve().parents[1]
PR_OLD = "https://github.com/fixture/fixture/pull/174"
PR_A = "https://github.com/fixture/fixture/pull/175"
PR_B = "https://github.com/fixture/fixture/pull/176"
DEPLOY = {"deploy": {"mechanism": "render (fixture api) + cloudflare-pages",
                     "auto_on_merge": True}}
NO_DEPLOY_PROFILE = {"deploy": {"mechanism": "none", "auto_on_merge": False},
                     "live_checks": {}}


def _merged_dep(task_id="DEP", **overrides) -> dict:
    """A task whose PR GitHub reported merged, waiting for F-14."""
    links = {"pr": PR_A, "branch": f"factory/{task_id}", "merged": PR_A}
    base = {"status": "in_review", "links": links}
    base.update(overrides)
    return _task(task_id, **base)


def _child(task_id="CHILD", status_="blocked", deps=("DEP",)) -> dict:
    return _task(task_id, status=status_, depends_on=list(deps))


def _queue(tmp_path: Path, tasks: list, name="tasks.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": tasks}, indent=2) + "\n")
    return path


def _statuses(path: Path) -> dict:
    return {t["id"]: t["status"] for t in TaskQueue(path).doc["tasks"]}


# --- AC-1: a merged dependency is met ----------------------------------------


def test_ac_1(tmp_path):
    # (a) The real loop on a deploy-target profile. Run 1: the child is
    # blocked on DEP's open PR; the sweep reads DEP's PR merged and
    # stamps it — and on this profile DEP stays in_review (F-14's gate).
    dep = _task("DEP", status="in_review",
                links={"pr": PR_A, "branch": "factory/DEP"})
    env = Env72(tmp_path / "loop", tasks=[dep, _child()], profile_extra=DEPLOY)
    first = env.run(Seats(), pr_state=lambda url: "merged")
    assert first.task_id != "CHILD"
    assert env.task("DEP")["status"] == "in_review"
    assert env.task("DEP")["links"]["merged"] == PR_A  # the parent's stamp
    # Run 2: sync_readiness reads the merge, the picker takes the child.
    second = env.run(Seats(), pr_state=lambda url: "open")
    assert second.task_id == "CHILD", second
    assert second.outcome == "pr_opened", second
    assert env.task("DEP")["status"] == "in_review"  # done is untouched

    # (b) Every reader of the rule sees it — sync_readiness and pick.
    path = _queue(tmp_path, [_merged_dep(), _child()])
    queue = TaskQueue(path)
    assert queue.sync_readiness() == ["CHILD"]
    assert TaskQueue(path).get("CHILD")["status"] == "ready"
    assert TaskQueue(path).pick().task["id"] == "CHILD"
    assert TaskQueue(path).get("DEP")["status"] == "in_review"

    # (c) A deploying dependency whose every slice merged is met too.
    sliced = _merged_dep(status="deploying", slices=[
        {"id": "S1", "text": "one", "status": "merged"},
        {"id": "S2", "text": "two", "status": "merged"},
    ])
    assert unmet_dependencies(_child(), [sliced]) == []

    # (d) Approval routing: a draft dependent approves straight to ready.
    draft = _child("DRAFT", status_="draft")
    draft["budget"] = {**draft["budget"], "max_turns": 60}  # F-71's floor
    for key in ("acceptance_hash", "approved_at", "approved_by"):
        draft.pop(key, None)
    env2 = Env72(tmp_path / "approve", tasks=[_merged_dep(), draft],
                 profile_extra=DEPLOY)
    routed = approve_task(TaskQueue(env2.queue_path), "DRAFT",
                          approved_by="owner", confirm=lambda _: True,
                          echo=lambda _: None, factory_root=env2.root)
    assert routed == "ready"

    # (e) The recovery re-queue writer lands it ready, not blocked.
    parked = _child("PARKED", status_="needs_human")
    assert _requeue_target(parked, [_merged_dep(), parked], keep_pr=False,
                           pr_closed=False, what="test") == "ready"

    # (f) One definition of the merged test: the orchestrator's is the
    # state module's own object, and status's verdict is its verdict.
    assert orchestrator._is_merged is state._is_merged
    assert "def _is_merged" not in inspect.getsource(orchestrator)
    assert "def _is_merged" in inspect.getsource(state)
    for links in ({"pr": PR_A, "merged": PR_A}, {"pr": PR_B, "merged": PR_A},
                  {"merged": PR_A}, {}):
        assert status.is_merged({"links": links}) == state._is_merged(links)


# --- AC-2: nothing else counts as merged -------------------------------------


def _owner_steps():
    return [{"text": "rotate the key", "done": False}]


NOT_MERGED = {
    "open PR": _task("DEP", status="in_review",
                     links={"pr": PR_A, "branch": "factory/DEP"}),
    "merged stamp names an older PR": _task(
        "DEP", status="in_review",
        links={"pr": PR_B, "branch": "factory/DEP", "merged": PR_OLD}),
    "merged stamp, no pr link": _task(
        "DEP", status="in_review", links={"merged": PR_A}),
    "awaiting_owner": _merged_dep(status="awaiting_owner",
                                  owner_steps=_owner_steps()),
    "needs_human, stale stamp": _merged_dep(status="needs_human"),
    "draft, stale stamp": _merged_dep(status="draft"),
    "ready, stale stamp": _merged_dep(status="ready"),
    # its own cross-repo dependency keeps it blocked: the readiness pass
    # must not move the dependency itself either
    "blocked, stale stamp": _merged_dep(status="blocked",
                                        depends_on=["other-repo:x"]),
    "in_progress, stale stamp": _merged_dep(status="in_progress"),
    "one slice not merged": _merged_dep(slices=[
        {"id": "S1", "text": "one", "status": "merged"},
        {"id": "S2", "text": "two", "status": "in_review"},
    ]),
    "deploying, one slice todo": _merged_dep(status="deploying", slices=[
        {"id": "S1", "text": "one", "status": "todo"},
    ]),
}


@pytest.mark.parametrize("case", sorted(NOT_MERGED))
def test_ac_2(tmp_path, case):
    dep = copy.deepcopy(NOT_MERGED[case])
    path = _queue(tmp_path, [dep, _child()])
    before = _statuses(path)
    queue = TaskQueue(path)
    assert unmet_dependencies(queue.get("CHILD"), queue.doc["tasks"]) == ["DEP"]
    assert queue.sync_readiness() == []
    pick = queue.pick()
    assert pick is None or pick.task["id"] != "CHILD"  # a ready DEP may run
    assert _statuses(path) == before  # nothing moved, DEP included
    # done keeps its meaning: a deploy-target profile never closes it.
    assert _close_merged_tasks(TaskQueue(path), DEPLOY, tmp_path / "root") == []
    assert _statuses(path) == before


def test_ac_2_ids_outside_the_rule(tmp_path):
    # A cross-repo dependency is never automatic, even when a task with
    # that literal id sits merged in this queue; an unknown id is unmet.
    foreign = _merged_dep("other-repo:DEP")
    for deps, open_ in ((["other-repo:DEP"], ["other-repo:DEP"]),
                        (["NOPE"], ["NOPE"])):
        child = _child(deps=deps)
        path = _queue(tmp_path, [foreign, child], name=f"q-{open_[0][:5]}.json")
        queue = TaskQueue(path)
        assert unmet_dependencies(queue.get("CHILD"), queue.doc["tasks"]) == open_
        assert queue.sync_readiness() == []
        assert queue.pick() is None


def test_ac_2_no_deploy_profile_unchanged(tmp_path):
    # On a no-deploy profile the close pass takes a merged in_review
    # task to done BEFORE the readiness pass, so the merged branch of the
    # rule never decides anything there: the old done-only rule and the
    # new rule agree on every task after the close.
    env = Env72(tmp_path, tasks=[_merged_dep(), _child()],
                profile_extra=NO_DEPLOY_PROFILE)
    queue = TaskQueue(env.queue_path)
    assert _close_merged_tasks(queue, env.profile, env.root) == [("DEP", "done")]
    by_id = {t["id"]: t for t in queue.doc["tasks"]}
    for task in queue.doc["tasks"]:
        old = [d for d in task.get("depends_on", [])
               if ":" in d or (by_id.get(d) or {}).get("status") != "done"]
        assert unmet_dependencies(task, queue.doc["tasks"]) == old


# --- AC-3: the owner can see why ----------------------------------------------


def test_ac_3(tmp_path):
    tasks = [
        _merged_dep("DEP"),
        _task("OPEN", status="in_review",
              links={"pr": PR_B, "branch": "factory/OPEN"}),
        _merged_dep("OWNER", status="awaiting_owner",
                    owner_steps=_owner_steps()),
        _child("CHILD", status_="ready", deps=["DEP"]),
        _child("WAITS", deps=["DEP", "OPEN", "OWNER", "other-repo:9b", "NOPE"]),
    ]
    page = render_status({"version": 1, "project": "fixture", "tasks": tasks},
                         project="fixture", factory_root=tmp_path)
    assert re.search(r"CHILD .*DEP: merged #175, not verified live", page), page
    waits = next(line for line in page.splitlines() if "WAITS" in line
                 and "OPEN" in line)
    # each open dependency, with its status; the met one says merged
    assert "DEP: merged #175, not verified live" in waits
    assert "OPEN: open (in_review, PR #176 not merged)" in waits
    assert "OWNER: open (awaiting_owner" in waits
    assert "other-repo:9b: open (cross-repo, mark met by hand)" in waits
    assert "NOPE: open (not in this queue)" in waits

    one = render_task_status({"version": 1, "project": "fixture",
                              "tasks": tasks}, "WAITS", project="fixture")
    assert "DEP: merged #175, not verified live" in one
    assert "OPEN: open (in_review, PR #176 not merged)" in one

    contract = (REPO_ROOT / "TASK-CONTRACT.md").read_text()
    assert "met by merge" in contract
    assert "`done` is unchanged" in contract
