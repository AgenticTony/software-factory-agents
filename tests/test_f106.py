"""F-106: a run that records a merge acts on it in the same run.

``run_once`` closes merged tasks and syncs readiness BEFORE the pick;
the sweep stamps ``links.merged`` AFTER it (F-26/F-93's order). A
dependent of a freshly merged task therefore idled once — the loop
stopped ``queue_empty`` and the next ``factory loop`` was the one that
ran it. F-106: a run reports how many NEW merge stamps its sweep wrote
(``RunResult.merges_stamped``), and ``run_loop`` treats an idle run
with new stamps as "go again": the next pass's own close-merged and
``sync_readiness`` read the STAMPED QUEUE (F-93's one ``_is_merged``
definition, never the sweep's in-memory view) and pick the dependents.
The extra pass follows only a NEW stamp, and a stamp is terminal (never
re-polled), so a gh repeating the same merged PR costs exactly one
extra pass.

Real ``TaskQueue`` files, the F-72 fixture project (real git, scripted
seats and gh seams), no network.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from _support.f63 import ledger_approve
from _support.f72 import OFF_PEAK, Env72, Seats, _task

from factory import orchestrator, state
from factory.mergeorder import CIState, Protection
from factory.orchestrator import run_loop
from factory.state import TaskQueue

STOCKHOLM = ZoneInfo("Europe/Stockholm")
PEAK = datetime(2026, 10, 19, 9, 0, tzinfo=STOCKHOLM)  # Monday 09:00
PR_A = "https://github.com/fixture/fixture/pull/201"
PR_OLD = "https://github.com/fixture/fixture/pull/199"
PR_D = "https://github.com/fixture/fixture/pull/202"
DEPLOY = {"deploy": {"mechanism": "render (fixture api) + cloudflare-pages",
                     "auto_on_merge": True}}

_GREEN = lambda remote, sha: CIState("success")  # noqa: E731


def _dep(task_id: str, pr: str, *, merged: str | None = None,
         **overrides) -> dict:
    """A task waiting in ``in_review`` on a PR the scripted gh reports."""
    links: dict = {"pr": pr, "branch": f"factory/{task_id}"}
    if merged is not None:
        links["merged"] = merged
    base = {"status": "in_review", "links": links}
    base.update(overrides)
    return _task(task_id, **base)


def _child(task_id: str, deps: list[str], *, status_: str = "blocked") -> dict:
    task = _task(task_id, status=status_, depends_on=list(deps))
    if status_ == "draft":
        # an unapproved draft: no frozen-contract markers to honour
        for key in ("acceptance_hash", "approved_at", "approved_by"):
            task.pop(key, None)
    return task


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _hold_open_pr(env: Env72, task_id: str) -> None:
    """Make a task's open PR cheap for the sweep: the branch exists on
    the remote at the base SHA and the revalidated cache matches it, so
    the ls-remote pre-check reports it ready without a clone."""
    _git(env.remote, "update-ref", f"refs/heads/factory/{task_id}",
         env.base_sha)
    queue = TaskQueue(env.queue_path)
    queue.get(task_id)["links"]["revalidated"] = {
        "pr": env.base_sha, "default": env.base_sha}
    queue.save()


def _loop(env: Env72, seats: Seats, *, pr_state, now=None):
    ledger_approve(env.root, env.queue_path)
    return run_loop(
        env.queue_path, factory_root=env.root, session_runner=seats,
        open_pr=env.open_pr,
        now=now or (lambda: OFF_PEAK),
        protection=lambda r, b: Protection(checked=True, strict=True),
        pr_state=pr_state, ci_state=_GREEN,
    )


def _spin_capped(monkeypatch, cap: int) -> dict:
    """Fail a runaway loop instead of hanging: wrap run_once, refusing
    past ``cap`` passes (AC-2's bound, proven as a failure not a hang —
    a mutation that unbinds the extra pass must die here)."""
    real = orchestrator.run_once
    calls = {"n": 0}

    def counting(queue_path, *args, **kwargs):
        calls["n"] += 1
        assert calls["n"] <= cap, f"the loop spun: {calls['n']} passes"
        return real(queue_path, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_once", counting)
    return calls


# --- AC-1: the loop continues after a sweep stamps a merge ---------------------


def test_ac_1(tmp_path, monkeypatch):
    # (a) B depends on A; A's PR is merged on the scripted gh. ONE
    # `factory loop` stamps A and runs B — no second invocation.
    _spin_capped(monkeypatch, cap=6)  # a spun loop fails, never hangs
    env = Env72(tmp_path / "a",
                tasks=[_dep("A", PR_A), _child("B", ["A"])],
                profile_extra=DEPLOY)
    seats = Seats()
    loop = _loop(env, seats,
                 pr_state=lambda url: "merged" if url == PR_A else "open")
    assert "implementer" in seats.seats(), seats.seats()  # B ran
    b = env.task("B")
    assert b["status"] == "in_review" and (b.get("links") or {}).get("pr")
    a = env.task("A")
    assert a["links"]["merged"] == PR_A  # the parent's stamp
    assert a["status"] == "in_review"  # deploy profile: done is F-14's
    assert loop.stop_reason == "queue_empty"
    assert [r.outcome for r in loop.results] == ["idle", "pr_opened", "idle"]
    assert loop.results[0].merges_stamped == 1  # the field AC-1 names
    assert loop.results[-1].merges_stamped == 0  # nothing new: it stops

    # (b) A's PR still open: exactly today's behaviour — one pass, no
    # stamp, no pick, queue_empty, and B never runs.
    env2 = Env72(tmp_path / "b",
                 tasks=[_dep("A", PR_A), _child("B", ["A"])],
                 profile_extra=DEPLOY)
    _hold_open_pr(env2, "A")  # current and green: open is the only fact
    seats2 = Seats()
    loop2 = _loop(env2, seats2, pr_state=lambda url: "open")
    assert loop2.stop_reason == "queue_empty"
    assert len(loop2.results) == 1
    assert "implementer" not in seats2.seats()
    assert env2.task("B")["status"] == "blocked"
    assert env2.task("A").get("links", {}).get("merged") is None


# --- AC-2: bounded — one extra pass, then it stops -----------------------------


def test_ac_2(tmp_path, monkeypatch):
    # A gh that reports the same merged PR on every call: the stamp is
    # terminal (skip_merged: never re-polled), so the extra pass sees
    # nothing new — exactly one extra pass, then queue_empty.
    env = Env72(tmp_path, tasks=[_dep("A", PR_A)], profile_extra=DEPLOY)
    calls = _spin_capped(monkeypatch, cap=4)
    loop = _loop(env, Seats(), pr_state=lambda url: "merged")
    assert loop.stop_reason == "queue_empty"
    assert calls["n"] == 2  # one extra pass, no more
    assert [r.merges_stamped for r in loop.results] == [1, 0]


# --- the hostile pass ----------------------------------------------------------


def test_attack_stale_stamp_plus_new_open_pr_keeps_child_blocked(tmp_path):
    # The wrong-pick side: A merged an OLD PR, then opened a new one —
    # links.merged != links.pr (F-93's one test), so the dependency is
    # unmet whatever gh remembers about the old merge. No new stamp can
    # be written for an open PR, so the loop stops after one pass.
    dep = _dep("A", PR_OLD, merged=PR_OLD)
    dep["links"]["pr"] = PR_A  # the task's CURRENT PR, still open
    env = Env72(tmp_path,
                tasks=[dep, _child("B", ["A"])], profile_extra=DEPLOY)
    _hold_open_pr(env, "A")  # current and green: never the reason it blocks
    seats = Seats()
    loop = _loop(env, seats, pr_state=lambda url: "open")
    assert loop.stop_reason == "queue_empty"
    assert len(loop.results) == 1  # no stamp: no extra pass
    assert "implementer" not in seats.seats()
    assert env.task("B")["status"] == "blocked"


def test_attack_child_draft_is_never_picked(tmp_path):
    # A merged but B draft (not approved): the extra pass reads the
    # queue, it cannot approve anything — no pick, and the loop stops.
    env = Env72(tmp_path,
                tasks=[_dep("A", PR_A), _child("B", ["A"], status_="draft")],
                profile_extra=DEPLOY)
    seats = Seats()
    loop = _loop(env, seats,
                 pr_state=lambda url: "merged" if url == PR_A else "open")
    assert loop.stop_reason == "queue_empty"
    assert [r.outcome for r in loop.results] == ["idle", "idle"]
    assert [r.merges_stamped for r in loop.results] == [1, 0]
    assert "implementer" not in seats.seats()
    assert env.task("B")["status"] == "draft"  # untouched, unapproved


def test_attack_extra_pass_inside_the_peak_window_runs_no_work(tmp_path):
    # A merge stamped just before the window: the extra pass still
    # refuses work by the window rule — B is picked, then refused, and
    # the loop stops peak_window with no session started.
    env = Env72(tmp_path,
                tasks=[_dep("A", PR_A), _child("B", ["A"])],
                profile_extra=DEPLOY)
    seats = Seats()

    def now() -> datetime:
        # the clock enters the window once A is stamped (between passes)
        links = (TaskQueue(env.queue_path).get("A").get("links") or {})
        return PEAK if state._is_merged(links) else OFF_PEAK

    loop = _loop(env, seats,
                 pr_state=lambda url: "merged" if url == PR_A else "open",
                 now=now)
    assert loop.stop_reason == "peak_window"
    assert [r.outcome for r in loop.results] == ["idle", "peak_window"]
    assert loop.results[1].task_id == "B"  # it was ready — and refused
    assert "implementer" not in seats.seats()
    assert env.task("B")["status"] == "ready"  # untouched by the refusal
    assert env.task("B").get("attempts") == []


def test_attack_two_dependents_both_run(tmp_path):
    # Two dependents unblocked at once: both run, one per pass, exactly
    # as the loop already schedules — the owner merges B's PR while C
    # waits, so the concurrency slot frees and C's pass follows.
    env = Env72(tmp_path,
                tasks=[_dep("A", PR_A), _child("B", ["A"]), _child("C", ["A"])],
                profile_extra=DEPLOY)
    merged = {PR_A}
    original_open = env.open_pr

    def open_pr(pr):
        url = original_open(pr)
        merged.add(url)  # the owner merges each published PR
        return url

    env.open_pr = open_pr
    seats = Seats()
    loop = _loop(env, seats,
                 pr_state=lambda url: "merged" if url in merged else "open")
    assert loop.stop_reason == "queue_empty"
    assert seats.seats().count("implementer") == 2  # B and C both ran
    for task_id in ("B", "C"):
        task = env.task(task_id)
        assert task["status"] == "in_review"
        assert (task.get("links") or {}).get("pr")
    # pass 1 stamps A (idle→again); B and C run one per pass; the pass
    # that stamps B is C's run, not an idle; the last stamping idle
    # (C's merge) buys exactly one more empty pass, then it stops.
    assert [r.outcome for r in loop.results] == [
        "idle", "pr_opened", "pr_opened", "idle", "idle"]
    assert loop.results[-1].merges_stamped == 0


def test_attack_concurrency_wait_stops_the_loop(tmp_path):
    # A stamp and a concurrency_wait in the same loop: the wait is a
    # stop, the extra pass is for idle runs only — the owner merges or
    # closes the named PR; the loop must not grind passes behind it.
    holder = _dep("D", PR_D)  # an open, current, green PR holding the slot
    env = Env72(tmp_path,
                tasks=[_dep("A", PR_A), holder, _child("B", ["A"])],
                profile_extra=DEPLOY)
    _hold_open_pr(env, "D")
    seats = Seats()
    loop = _loop(env, seats,
                 pr_state=lambda url: "merged" if url == PR_A else "open")
    assert loop.stop_reason == "concurrency_wait"
    assert len(loop.results) == 2  # the stamp pass + the wait: no third
    assert loop.results[0].merges_stamped == 1
    assert loop.results[1].task_id == "B"
    assert "implementer" not in seats.seats()  # B waits, named, visible
    assert loop.results[1].open_prs == [PR_D]
