"""F-79 — Fix-round work survives, and any PR park resumes.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f79.py::test_ac_N``
means a test function with exactly that name.

Three acceptance criteria (BUILD-ORDER §F-79):

- AC-1 a fix round on a task's normal PR that passes L0 and L1 and then
  does not publish keeps its work LOCALLY (one parent-built commit on
  the pushed head, bundled under the factory root, recorded as
  ``links.wip`` by the dedicated writer — never pushed). The next fix
  round starts from it only while its parent is still the pushed head
  on the remote; its prompt says so and carries the previous round's
  review findings; publish sees the whole range. Cleared on publish,
  merge, close and a fresh-run re-queue.
- AC-2 ``factory recover <task> --reset-attempts --evidence "…"`` keeps
  the PR for ANY park holding a normal PR link GitHub reports open
  (classes with their own gate keep it); no writer lands a task
  ``ready`` while it holds a normal open PR.
- AC-3 a hand-merged PR is recognised on any park: the sweep takes the
  merged recovery edge whatever the escalation, and clears any WIP.

Real vs stubbed: the F-76/F-77/F-78 harness (``tests/_support``) — git,
bundles, the clone manager, the queue, the L0/L1 sandboxes and the push
to the local bare remote are real; the model seats and the CI/gh seams
are scripted. The ledger is the real ``ApprovalLedger``.
"""

import subprocess
from pathlib import Path

import pytest
from _support.f63 import ledger_approve
from _support.f72 import SEAT_TOKEN
from _support.f76 import IN_PEAK_NOW, OFF_PEAK_NOW
from _support.f78 import Parked, append_escalation
from _support.f79 import (
    EVIDENCE,
    FAIL,
    FINDING_TEXT,
    IMPL_DONE,
    IMPL_WIP,
    PASS,
    ObservingSeats,
    fix_round,
    git_out,
    kept_park,
    recover_open,
    wip_dir,
)

import factory.orchestrator as orch
from factory import wip as wipstore
from factory.approvals import ApprovalLedger
from factory.approve import ApprovalError, recover_task
from factory.contract import ContractError
from factory.publish import PublishError
from factory.state import TransitionError

FOREIGN = "def answer():\n    return 42  # a hand fix on the PR branch\n"


def _recover(p: Parked, state, *, reset: bool = True, readied=None,
             echoed=None):
    """``factory recover`` with a scripted GitHub PR state (a word, or
    an exception the seam raises)."""
    def read(url):
        if isinstance(state, Exception):
            raise state
        return state

    return recover_task(
        p.queue(), "T-1", reset_attempts=reset, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root, pr_state=read,
        pr_ready=(readied.append if readied is not None
                  else (lambda url: None)),
        confirm=lambda question: True,
        echo=(echoed.append if echoed is not None else (lambda text: None)))


def _park_as(tmp: Path, monkeypatch, escalation: str | None) -> Parked:
    """A real ``ci_failed`` park holding a normal open PR, then (when
    named) a later attempt that escalated ``escalation`` — the record a
    fix round on that PR writes when it ends there."""
    p = Parked(tmp, monkeypatch)
    if escalation is not None:
        append_escalation(p.queue(), escalation)
    ledger_approve(p.env.root, p.env.queue_path)
    return p


# ============================================================= AC-1 =========


def test_ac_1(tmp_path: Path, monkeypatch):
    """Validated fix-round work is kept locally and the next round
    starts from it — only while its parent is still the pushed head."""
    # (1) the round that broke: L0 and L1 passed, review failed, the
    # budget was spent. Nothing reached the PR; the work is kept.
    p = kept_park(tmp_path / "kept", monkeypatch)
    env, seats = p.env, p.seats
    task = env.task()
    assert env.remote_branch_sha(p.branch) == p.head, \
        "kept work must never be pushed to the PR branch"
    wip = task["links"].get("wip")
    assert wip, "the validated round's work was not kept"
    assert wip["parent"] == p.head == task["links"]["pushed_head"]
    assert wip["run_id"] == task["attempts"][-1]["run_id"]
    bundle = wip_dir(p) / f"{wip['run_id']}.bundle"
    assert bundle.is_file(), "the kept commit is not on disk"
    heads = git_out(env.repo, "bundle", "list-heads", str(bundle))
    assert heads.split()[0] == wip["sha"], heads
    # the owner's checkout was never written: no ref, no object
    assert "factory" not in git_out(env.repo, "for-each-ref")
    assert subprocess.run(
        ["git", "-C", str(env.repo), "cat-file", "-e", wip["sha"]],
        capture_output=True).returncode != 0

    # (2) one owner command, and the next sweep's fix round starts FROM
    # the kept commit: the clone holds it before the session writes.
    assert recover_open(p) == "in_review"
    assert env.task()["links"]["wip"] == wip, "the reset keeps the WIP"
    rounds_before = len(seats.seen)
    result = fix_round(p, monkeypatch)
    assert result.outcome == "pr_opened", result
    assert result.pr == p.url and len(env.prs) == 1, "same PR, no second"
    assert len(seats.seen) == rounds_before + 1
    started = seats.seen[-1]
    assert started["impl"] == IMPL_WIP, \
        "the resumed round did not start from the kept work"
    assert started["head"] == wip["sha"]
    prompt = seats.prompts("implementer")[-1]
    assert "## Continuing kept work" in prompt
    assert wip["sha"][:12] in prompt
    assert FINDING_TEXT in prompt, \
        "the previous round's review finding did not reach the prompt"
    assert "## CI findings to fix" in prompt, "the CI findings still ride"
    # publish saw and pushed the WHOLE range: pushed head → kept commit
    # → this round, a fast-forward on the same branch.
    head2 = env.remote_branch_sha(p.branch)
    chain = git_out(env.remote, "rev-list", f"{p.head}..{head2}").split()
    assert chain == [head2, wip["sha"]], chain
    assert git_out(env.remote, "show", f"{head2}:impl.py") + "\n" == IMPL_DONE
    assert git_out(env.remote, "log", "-1", "--format=%an <%ae>",
                   wip["sha"]) == "Software Factory <factory@localhost>"
    # cleared on publish: the link and the files
    task = env.task()
    assert task["status"] == "in_review"
    assert task["links"]["pushed_head"] == head2
    assert "wip" not in task["links"], "a published round clears the WIP"
    assert not wip_dir(p).exists()

    # (3) the parent is no longer the pushed head: the WIP is ignored,
    # said so, and cleared — the round builds on the head that is there.
    p = kept_park(tmp_path / "moved", monkeypatch)
    env, seats = p.env, p.seats
    wip = env.task()["links"]["wip"]
    assert recover_open(p) == "in_review"
    moved = env.push_to_branch(p.branch, FOREIGN)
    # (3a) a head the factory did not push: no round at all (F-76), and
    # the kept work is left exactly as it was.
    rounds_before = len(seats.seen)
    result = fix_round(p, monkeypatch, head=moved, rerun=True)
    assert len(seats.seen) == rounds_before, "built on a foreign head"
    assert any("head moved outside the factory" in line
               for line in result.not_ready_prs), result.not_ready_prs
    assert env.task()["links"]["wip"] == wip
    # (3b) the owner vouches for the moved head: the round runs on it,
    # WITHOUT the kept commit (its parent is not the pushed head now).
    p.queue().vouch_pushed_head("T-1", pr_sha=moved)
    result = fix_round(p, monkeypatch, head=moved, rerun=True)
    assert len(seats.seen) == rounds_before + 1, result
    started = seats.seen[-1]
    assert started["head"] == moved and started["impl"] == FOREIGN
    assert "## Continuing kept work" not in seats.prompts("implementer")[-1]
    assert "wip" not in env.task()["links"]
    assert not wip_dir(p).exists()
    ignored = [e for e in p.log() if "ignored" in (e.get("note") or "")]
    assert ignored and wip["sha"][:12] in ignored[-1]["note"], p.log()[-3:]

    # (4) a fresh-run re-queue clears it (the PR was closed). First,
    # on the same real bundle: restore takes ONLY the recorded commit in
    # the recorded shape — a link naming another sha, or another parent,
    # restores nothing.
    p = kept_park(tmp_path / "closed", monkeypatch)
    wip = p.env.task()["links"].get("wip")
    assert wip
    scratch = tmp_path / "closed" / "scratch"
    subprocess.run(["git", "clone", "-q", str(p.env.remote), str(scratch)],
                   check=True)
    where = {"factory_root": p.env.root, "project": "fixture",
             "task_id": "T-1"}
    assert wipstore.restore(scratch, wip=wip, **where) == wip["sha"]
    with pytest.raises(wipstore.WipError, match="not the recorded"):
        wipstore.restore(scratch, wip={**wip, "sha": "b" * 40}, **where)
    with pytest.raises(wipstore.WipError, match="not one commit on"):
        wipstore.restore(
            scratch, wip={**wip, "parent": p.env.base_sha}, **where)
    assert _recover(p, "closed") == "ready"
    assert "wip" not in p.env.task()["links"]
    assert not wip_dir(p).exists()

    # (5) kept work the publish fence refuses is not kept for another
    # try: a kept commit whose blob carries a run secret — the resumed
    # round removes it from the tree and review passes, but the fence
    # scans every object in the range. Refused, nothing pushed, and the
    # kept commit is gone (resuming from it could never publish).
    monkeypatch.setattr(orch, "run_secrets", lambda profile, config=None: (SEAT_TOKEN,))
    leaky = f"def answer():\n    return 42  # ref {SEAT_TOKEN}\n"
    p = Parked(tmp_path / "fence", monkeypatch, seats=ObservingSeats(
        impl_files=[{}, {"impl.py": leaky}, {"impl.py": IMPL_WIP}],
        reviews=(PASS, FAIL, PASS)))
    assert recover_open(p) == "in_review"
    assert fix_round(p, monkeypatch).escalation == "attempts_exhausted"
    wip = p.env.task()["links"]["wip"]  # L0 does not know run secrets
    assert recover_open(p) == "in_review"
    result = fix_round(p, monkeypatch)
    assert p.seats.seen[-1]["head"] == wip["sha"]
    assert result.escalation == "publish_refused", result
    assert p.env.remote_branch_sha(p.branch) == p.head, "the secret left"
    assert "wip" not in p.env.task()["links"] and not wip_dir(p).exists()
    monkeypatch.undo()

    # (6) the record is the dedicated writer's alone, and gated: only an
    # in-progress fix round on a normal PR whose pushed head is the
    # parent may record; the schema refuses anything that is not a sha.
    p = _park_as(tmp_path / "writer", monkeypatch, None)
    q = p.queue()
    sha = "a" * 40
    with pytest.raises(TransitionError, match="in_progress"):
        q.record_wip("T-1", sha=sha, parent=p.head,
                     run_id="20261114T000000Z-abcdef")
    q = p.queue()
    q.get("T-1")["links"]["wip"] = {"sha": "not-a-sha", "parent": p.head,
                                    "run_id": "20261114T000000Z-abcdef"}
    with pytest.raises(ContractError):
        q.save()
    assert "wip" not in p.env.task()["links"]


# ============================================================= AC-2 =========


def test_ac_2(tmp_path: Path, monkeypatch):
    """One resume rule: the reset keeps an open PR for any park that
    holds a normal PR link; classes with their own gate keep it; and no
    writer lands a task ``ready`` while it holds a normal open PR."""
    # (1) every class the old per-class list left without an exit.
    for escalation in ("attempts_exhausted", "review_not_converging",
                       "budget_exhausted", "sentinel_failed",
                       "protected_path_touched", "orchestrator_error"):
        p = _park_as(tmp_path / escalation, monkeypatch, escalation)
        before = p.env.task()
        readied: list[str] = []
        echoed: list[str] = []
        assert _recover(p, "open", readied=readied,
                        echoed=echoed) == "in_review", escalation
        task = p.env.task()
        assert task["status"] == "in_review"
        assert task["links"] == before["links"], "links are untouched"
        assert task["attempts"] == before["attempts"]
        assert len(task["attempt_resets"]) == 1
        assert task["attempt_resets"][-1]["prior_attempts"] == \
            len(before["attempts"])
        assert readied == [p.url], "the drafted PR is marked ready again"
        assert "keeps PR" in "\n".join(echoed)
        entry = p.log()[-1]
        assert entry["outcome"] == "recovered"
        assert entry["escalation"] == escalation
        assert "in_review" in entry["note"] and p.url in entry["note"]

    # (2) GitHub decides: closed → the fresh-run re-queue with the
    # marker; merged / unreadable → refused, nothing written.
    p = _park_as(tmp_path / "closed", monkeypatch, "review_not_converging")
    assert _recover(p, "closed") == "ready"
    assert p.env.task()["attempt_resets"]
    for name, state, word in (
            ("merged", "merged", "merged"),
            ("unreadable", PublishError("gh: connection refused"),
             "unreachable")):
        p = _park_as(tmp_path / name, monkeypatch, "budget_exhausted")
        snap = p.snapshot()
        with pytest.raises(ApprovalError, match=word):
            _recover(p, state)
        assert p.snapshot() == snap, f"{name}: the refusal wrote the queue"

    # (3) classes with their own gate keep it, and it runs first.
    # stale_base: --reset-attempts refuses, naming --revalidate.
    p = _park_as(tmp_path / "stale", monkeypatch, "stale_base")
    snap = p.snapshot()
    with pytest.raises(ApprovalError, match="--revalidate"):
        _recover(p, "open")
    assert p.snapshot() == snap
    # credential_added: never the reset — the evidence exit only, which
    # follows the same PR rule and grants NO budget.
    p = _park_as(tmp_path / "cred-open", monkeypatch, "credential_added")
    snap = p.snapshot()
    with pytest.raises(ApprovalError, match="evidence exit"):
        _recover(p, "open")  # --reset-attempts: refused, nothing written
    assert p.snapshot() == snap
    readied = []
    assert _recover(p, "open", reset=False, readied=readied) == "in_review"
    assert not p.env.task().get("attempt_resets"), \
        "a credential exit never grants a budget"
    assert readied == [p.url]
    assert p.log()[-1]["escalation"] == "credential_added"
    p = _park_as(tmp_path / "cred-closed", monkeypatch, "credential_added")
    assert _recover(p, "closed", reset=False) == "ready"
    p = _park_as(tmp_path / "cred-unread", monkeypatch, "credential_added")
    snap = p.snapshot()
    with pytest.raises(ApprovalError, match="unreachable"):
        _recover(p, PublishError("gh: connection refused"), reset=False)
    assert p.snapshot() == snap
    with pytest.raises(TransitionError, match="credential"):
        p.queue().recover_attempts("T-1", EVIDENCE, keep_pr=True)
    # scope_change_needed: the ledger first (F-78) — and its no-flag
    # exit, which only ever re-queues a fresh run, refuses an open PR.
    p = _park_as(tmp_path / "scope", monkeypatch, "scope_change_needed")
    snap = p.snapshot()
    with pytest.raises(ApprovalError, match="--reset-attempts"):
        _recover(p, "open", reset=False)
    assert p.snapshot() == snap
    assert _recover(p, "closed", reset=False) == "ready"
    # an escalation DRAFT PR stays with --accept/--reject.
    p = _park_as(tmp_path / "draft", monkeypatch, "review_not_converging")
    q = p.queue()
    q.get("T-1")["links"]["escalation_pr"] = True
    q.save()
    snap = p.snapshot()
    with pytest.raises(ApprovalError, match="--accept"):
        _recover(p, "open")
    assert p.snapshot() == snap

    # (4) the gate lives in the writers: called directly, none of them
    # re-queues a task holding a normal PR link without GitHub's word.
    ledger_of = lambda p: ApprovalLedger.for_project(p.env.root, "fixture")  # noqa: E731
    p = _park_as(tmp_path / "direct", monkeypatch, "attempts_exhausted")
    snap = p.snapshot()
    with pytest.raises(TransitionError, match="GitHub"):
        p.queue().recover_attempts("T-1", EVIDENCE)
    with pytest.raises(TransitionError):
        p.queue().recover_attempts("T-1", EVIDENCE, keep_pr=True,
                                   pr_closed=True)
    for target in ("ready", "blocked", "in_review"):
        with pytest.raises(TransitionError):
            p.queue().transition("T-1", target)
    assert p.snapshot() == snap
    assert p.queue().recover_attempts(
        "T-1", EVIDENCE, pr_closed=True) == "ready"
    p = _park_as(tmp_path / "direct-scope", monkeypatch,
                 "scope_change_needed")
    snap = p.snapshot()
    with pytest.raises(TransitionError, match="GitHub"):
        p.queue().recover_scope("T-1", ledger=ledger_of(p),
                                evidence=EVIDENCE)
    assert p.snapshot() == snap
    p = _park_as(tmp_path / "direct-cred", monkeypatch, "credential_added")
    snap = p.snapshot()
    with pytest.raises(TransitionError, match="GitHub"):
        p.queue().recover_credential("T-1", EVIDENCE)
    assert p.snapshot() == snap
    assert p.queue().recover_credential(
        "T-1", EVIDENCE, keep_pr=True) == "in_review"

    # (5) the in-run writer: a fix round the peak window stops before
    # its review returns to in_review (never ready under its own open
    # PR) — and its validated change is kept, not discarded.
    p = Parked(tmp_path / "peak", monkeypatch, seats=ObservingSeats(
        impl_files=[{}, {"impl.py": IMPL_WIP}]))
    assert recover_open(p) == "in_review"
    rounds_before = len(p.seats.seen)
    result = fix_round(
        p, monkeypatch,
        now=lambda: (IN_PEAK_NOW() if len(p.seats.seen) > rounds_before
                     else OFF_PEAK_NOW()))
    assert result.outcome == "peak_window", result
    task = p.env.task()
    assert task["status"] == "in_review", \
        "a stopped fix round re-queued ready under its own open PR"
    assert task["links"].get("wip"), "the validated change was discarded"
    assert "kept" in task["attempts"][-1]["note"]


# ============================================================= AC-3 =========


def test_ac_3(tmp_path: Path, monkeypatch):
    """A PR the owner merged by hand is recognised on ANY park: the
    sweep takes the merged recovery edge and clears any kept work."""
    merged = lambda url: "merged"  # noqa: E731
    for escalation in (None, "scope_change_needed", "attempts_exhausted",
                       "review_not_converging", "credential_added"):
        name = escalation or "ci_failed"
        p = _park_as(tmp_path / name, monkeypatch, escalation)
        result = p.env.run_once(p.seats, pr_state=merged)
        assert result.outcome == "idle", (name, result)
        task = p.env.task()
        # the fixture profile has no deploy target: the merged recovery
        # returns the task to in_review and the close pass finishes it.
        assert task["links"]["merged"] == p.url, name
        assert task["status"] == "done", (name, task["status"])
        notes = [e.get("note") or "" for e in p.log()
                 if e.get("outcome") == "recovered"]
        assert any("merged" in n and name in n for n in notes), (name, notes)

    # a park holding kept work: the merge clears it, link and files.
    p = kept_park(tmp_path / "wip", monkeypatch)
    assert p.env.task()["links"].get("wip") and wip_dir(p).exists()
    p.env.run_once(p.seats, pr_state=merged)
    task = p.env.task()
    assert task["status"] == "done" and task["links"]["merged"] == p.url
    assert "wip" not in task["links"]
    assert not wip_dir(p).exists()

    # not the sweep's to move: an escalation draft PR, an open PR, a
    # state gh cannot answer.
    p = _park_as(tmp_path / "draft", monkeypatch, "review_not_converging")
    q = p.queue()
    q.get("T-1")["links"]["escalation_pr"] = True
    q.save()
    snap = p.snapshot()
    p.env.run_once(p.seats, pr_state=merged)
    assert p.snapshot() == snap, "an escalation draft PR was moved"

    def unreadable(url):
        raise PublishError("gh: connection refused")

    for name, seam in (("open", lambda url: "open"),
                       ("unreadable", unreadable)):
        p = _park_as(tmp_path / f"not-{name}", monkeypatch,
                     "attempts_exhausted")
        snap = p.snapshot()
        p.env.run_once(p.seats, pr_state=seam)
        assert p.snapshot() == snap, f"{name}: the park was moved"

    # the writer's own gate: only this task's exact PR URL, only a park.
    with pytest.raises(TransitionError):
        p.queue().recover_pr_merged(
            "T-1", merged_url="https://github.com/fixture/fixture/pull/99")
    assert p.snapshot() == snap
