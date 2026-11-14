"""F-78 — A scope-changed PR keeps its work: the keep-the-PR reset for
scope parks.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f78.py::test_ac_N``
means a test function with exactly that name.

Three acceptance criteria (BUILD-ORDER §F-78, as amended by the build —
the drafted AC-2 "`factory adopt` exists" was a misdiagnosis: the
command exists as ``factory approve --adopt`` (F-63 ``adopt_queue``:
render contract, confirm, ``record_task_for``); what the scope park
lacks is the RESET that survives it):

- AC-1 ``factory recover <task-id> --reset-attempts --evidence "…"`` on
  a task parked ``scope_change_needed`` that holds a normal PR link
  keeps the PR through the ledger gate: the task's current (widened)
  whole-contract hash must be owner-approved — unapproved refuses
  naming ``factory approve --adopt``, GitHub not read — then GitHub
  decides: open → ``needs_human → in_review`` (the F-77 shape: marker,
  links untouched, the drafted PR marked ready again), closed → the
  fresh-run re-queue with the same marker, merged/unreadable → refused.
  ``attempts_exhausted`` and ``scope_missing`` never take ``in_review``;
  the no-flag scope exit re-queues without a marker, as today.
- AC-2 the ledger step is today's command: ``factory approve --adopt``
  records the widened hash for a parked task, and the reset's gate
  accepts exactly what it records (before: refuse; after: proceed).
- AC-3 the next sweep runs the CI fix round on that PR under the
  widened scope and the reset budget — same PR, no re-implementation,
  and the resumed round's L0 accepts a diff touching the widened path.

Real vs stubbed: the F-76/F-77 harness as it stands (tests/_support/
f76.py, the ``Parked`` fixture from test_f77) — git, the clone manager,
the queue, the L0/L1 sandboxes and the push to the local bare remote
are real; the model seats and the CI/gh seams are scripted. The ledger
is the real ``ApprovalLedger`` on the fixture root.
"""

import subprocess
from pathlib import Path

import pytest
from _support.f63 import ledger_approve
from _support.f72 import Seats
from _support.f75 import CI
from _support.f78 import GREEN, RED, Parked, append_escalation, seams

from factory.approvals import ApprovalLedger
from factory.approve import (
    ApprovalError,
    adopt_queue,
    recover_task,
)
from factory.config import FactoryConfig
from factory.contract import contract_hash
from factory.publish import PublishError
from factory.state import TransitionError

WIDENED_FILE = "widened.py"
WIDENED_BODY = "WIDENED = True  # the requested path\n"
EVIDENCE = "the session's reason was sound; widened to cover the request"


def _scope_park(tmp: Path, monkeypatch, *, widen: bool = True,
                reapprove: bool = True,
                seats: Seats | None = None) -> Parked:
    """A task parked ``scope_change_needed`` holding a normal open PR:
    the real-sweep ``ci_failed`` park (the ``Parked`` fixture), then a
    later fix round whose implementer requested a path outside scope
    (appended as the last attempt record, exactly as the real park
    writes it). ``widen`` adds the requested path to ``scope.paths``;
    ``reapprove`` records the CURRENT (widened) hash into the ledger —
    the fixture's own ``run_once`` approved the pre-widening hash, so
    skipping this leaves the widened contract unapproved."""
    p = Parked(tmp, monkeypatch, seats=seats)
    q = p.queue()
    append_escalation(q, "scope_change_needed")
    if widen:
        task = q.get("T-1")
        task.setdefault("scope", {"paths": []})["paths"].append(WIDENED_FILE)
        q.save()
    if reapprove:
        ledger_approve(p.env.root, p.env.queue_path)
    assert p.env.task()["status"] == "needs_human"
    assert p.env.task()["attempts"][-1]["escalation"] == "scope_change_needed"
    return p


def _pr_state(state):
    """A pr_state seam from a word or an exception to raise."""
    def read(url):
        if isinstance(state, Exception):
            raise state
        return state
    return read


# ============================================================= AC-1 =========


def test_ac_1(tmp_path: Path, monkeypatch):
    """The keep-the-PR reset for a scope park: the ledger gate first,
    then GitHub decides; the state layer gates the parks that may keep;
    the no-flag scope exit and the fresh-run targets behave as today."""
    # (1) approved + OPEN: needs_human → in_review, the marker, links
    # untouched, the draft marked ready, the log entry naming the real
    # escalation, the echo naming the outcome BEFORE the y/N.
    p = _scope_park(tmp_path / "open", monkeypatch)
    before = p.env.task()
    echoed: list[str] = []
    seen_at_prompt: list[str] = []
    readied: list[str] = []

    def confirm(question: str) -> bool:
        seen_at_prompt.append("\n".join(echoed))
        return True

    status = recover_task(
        p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root,
        pr_state=_pr_state("open"), pr_ready=readied.append,
        confirm=confirm, echo=echoed.append)
    assert status == "in_review"
    task = p.env.task()
    assert task["status"] == "in_review"
    assert task["links"] == before["links"], "links are untouched"
    assert task["attempts"] == before["attempts"], "history is kept"
    marker = task["attempt_resets"][-1]
    assert marker["prior_attempts"] == len(before["attempts"])
    assert marker["evidence"] == EVIDENCE
    assert len(task["attempt_resets"]) == 1, "ONE marker per recovery"
    assert readied == [p.url], "the drafted PR is marked ready again"
    assert seen_at_prompt and "keeps PR" in seen_at_prompt[0] \
        and "fix round on that PR" in seen_at_prompt[0], seen_at_prompt[0]
    assert "will re-implement" not in seen_at_prompt[0]
    entry = p.log()[-1]
    assert entry["outcome"] == "recovered"
    assert entry["escalation"] == "scope_change_needed"
    assert "in_review" in entry["note"] and p.url in entry["note"]

    # (2) the widened hash NOT ledger-approved: refuse BEFORE the prompt
    # and BEFORE GitHub — the remedy names today's adopt command.
    p = _scope_park(tmp_path / "unapproved", monkeypatch, reapprove=False)
    snap, log_before = p.snapshot(), len(p.log())
    confirmed: list[str] = []
    with pytest.raises(ApprovalError, match="approve --adopt"):
        recover_task(
            p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
            done_by="owner", factory_root=p.env.root,
            pr_state=_pr_state(AssertionError(
                "an unapproved contract must not read GitHub")),
            confirm=lambda q, c=confirmed: c.append(q) or True,
            echo=lambda text: None)
    assert p.snapshot() == snap, "the refusal wrote the queue"
    assert len(p.log()) == log_before, "the refusal logged"
    assert confirmed == [], "refused before the prompt"

    # (3) approved + CLOSED: the fresh-run re-queue WITH the marker — the
    # budget the round spent is granted back for the fresh run too.
    p = _scope_park(tmp_path / "closed", monkeypatch)
    echoed = []
    readied = []
    status = recover_task(
        p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root,
        pr_state=_pr_state("closed"), pr_ready=readied.append,
        confirm=lambda question: True, echo=echoed.append)
    assert status == "ready" and p.env.task()["status"] == "ready"
    assert p.env.task()["attempt_resets"], "the closed reset marks too"
    assert readied == []
    assert "will re-implement" in "\n".join(echoed)
    assert p.log()[-1]["escalation"] == "scope_change_needed"

    # (4) merged / unreadable / unknown: refused, nothing written, no
    # ready call — the F-77 refusals, unchanged, for the scope park.
    for name, state, word in (
            ("merged", "merged", "merged"),
            ("unreadable", PublishError("gh: connection refused"),
             "unreachable"),
            ("unknown", "weird", "weird")):
        p = _scope_park(tmp_path / name, monkeypatch)
        snap = p.snapshot()
        with pytest.raises(ApprovalError, match=word):
            recover_task(
                p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
                done_by="owner", factory_root=p.env.root,
                pr_state=_pr_state(state),
                pr_ready=lambda url: pytest.fail("ready called"),
                confirm=lambda question: True, echo=lambda text: None)
        assert p.snapshot() == snap, f"{name}: the refusal wrote the queue"

    # (5) the state layer: the keep-PR target is the scope/ci_failed
    # parks' alone; scope_missing and attempts_exhausted are refused
    # (no ledger demanded for those — the gate is the park, not the
    # contract).
    p = _scope_park(tmp_path / "state", monkeypatch)
    ledger = ApprovalLedger.for_project(p.env.root, "fixture")
    new_status = p.queue().recover_attempts(
        "T-1", EVIDENCE, done_by="owner", keep_pr=True, ledger=ledger)
    assert new_status == "in_review"
    for escalation in ("scope_missing", "attempts_exhausted"):
        p = _scope_park(tmp_path / f"state-{escalation}", monkeypatch)
        q = p.queue()
        append_escalation(q, escalation)
        q.save()
        with pytest.raises(TransitionError):
            q.recover_attempts("T-1", EVIDENCE, done_by="owner",
                               keep_pr=True, ledger=ledger)

    # (5b) F-78 review finding: the F-65 gate is the WRITER's, not only
    # the command's — a DIRECT recover_attempts call on a scope park
    # refuses without a ledger, and with a ledger that lacks the widened
    # hash, for BOTH targets, writing nothing (the fixture's own
    # approvals covered only the pre-widening contract).
    p = _scope_park(tmp_path / "state-ledger", monkeypatch, reapprove=False)
    q = p.queue()
    snap = p.snapshot()
    for kwargs in ({"keep_pr": True}, {"keep_pr": False}):
        with pytest.raises(TransitionError, match="ledger"):
            q.recover_attempts("T-1", EVIDENCE, done_by="owner", **kwargs)
        with pytest.raises(TransitionError, match="ledger"):
            q.recover_attempts(
                "T-1", EVIDENCE, done_by="owner",
                ledger=ApprovalLedger.for_project(p.env.root, "fixture"),
                **kwargs)
    assert p.snapshot() == snap, "the ledger refusals wrote the queue"
    # …and with the widened hash recorded, the direct call succeeds
    # (the ledger is the gate, never the blocker).
    ledger_approve(p.env.root, p.env.queue_path)
    assert q.recover_attempts(
        "T-1", EVIDENCE, done_by="owner", keep_pr=True,
        ledger=ApprovalLedger.for_project(p.env.root, "fixture")
    ) == "in_review"

    # (6) --yes is refused for the scope reset, as for every reset.
    p = _scope_park(tmp_path / "yes", monkeypatch)
    with pytest.raises(ApprovalError, match="interactive only"):
        recover_task(
            p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
            assume_yes=True, factory_root=p.env.root,
            confirm=lambda question: True, echo=lambda text: None)

    # (7) the no-flag scope exit is unchanged: re-queue, NO marker.
    p = _scope_park(tmp_path / "noflag", monkeypatch)
    status = recover_task(
        p.queue(), "T-1", evidence="widened and checked",
        done_by="owner", factory_root=p.env.root,
        confirm=lambda question: True, echo=lambda text: None)
    assert status == "ready"
    assert not p.env.task().get("attempt_resets"), \
        "the no-flag scope exit never resets the budget"


# ============================================================= AC-2 =========


def test_ac_2(tmp_path: Path, monkeypatch):
    """The ledger step is today's command: ``factory approve --adopt``
    renders the widened contract and records exactly the hash the
    reset's gate then accepts. Before it: refuse; after: proceed."""
    p = _scope_park(tmp_path, monkeypatch, reapprove=False)

    # the widened hash is NOT on the ledger → the reset refuses.
    ledger = ApprovalLedger.for_project(p.env.root, "fixture")
    assert not ledger.has_contract("T-1", contract_hash(p.env.task()))
    with pytest.raises(ApprovalError, match="approve --adopt"):
        recover_task(
            p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
            done_by="owner", factory_root=p.env.root,
            pr_state=_pr_state("open"),
            confirm=lambda question: True, echo=lambda text: None)

    # today's adopt command records it: the real adopt_queue over the
    # fixture queue — interactive, renders the contract (the widened
    # scope visible), records the CURRENT hash.
    echoed: list[str] = []
    results = adopt_queue(
        p.queue(), p.env.root, approved_by="owner",
        confirm=lambda question: True, echo=echoed.append)
    adopted = [r for r in results if r.task_id == "T-1"]
    assert adopted and adopted[0].action == "adopted", results
    shown = "\n".join(echoed)
    assert WIDENED_FILE in shown, "the rendered contract shows the widen"
    assert ledger.has_contract("T-1", contract_hash(p.env.task()))

    # and now the reset proceeds: open PR → in_review.
    status = recover_task(
        p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root,
        pr_state=_pr_state("open"), pr_ready=lambda url: None,
        confirm=lambda question: True, echo=lambda text: None)
    assert status == "in_review"


# ============================================================= AC-3 =========


def test_ac_3(tmp_path: Path, monkeypatch):
    """After the AC-1 recovery the next sweep runs the CI fix round on
    the SAME PR under the widened scope and the reset budget: the
    implementer's diff touches the widened path, L0 accepts it, the
    push updates the same branch, and no re-implementation happens."""
    impl = {"impl.py": "def answer():\n    return 42  # CI fix round\n",
            WIDENED_FILE: WIDENED_BODY}
    p = _scope_park(
        tmp_path / "compose", monkeypatch, reapprove=False,
        seats=Seats(impl_files=[{}, impl]))  # round 2 = the resumed fix
    # the real adopt step, then the reset (AC-1 + AC-2 composed)
    adopt_queue(p.queue(), p.env.root, approved_by="owner",
                confirm=lambda question: True, echo=lambda text: None)
    status = recover_task(
        p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root,
        pr_state=_pr_state("open"), pr_ready=lambda url: None,
        confirm=lambda question: True, echo=lambda text: None)
    assert status == "in_review"
    env, seats, url, head = p.env, p.seats, p.url, p.head
    attempts_before = len(env.task()["attempts"])
    prompts_before = len(seats.prompts("implementer"))

    seams(monkeypatch, env)
    result = env.run_once(
        seats, ci_state=CI(by_sha={head: [RED, RED]}, default=GREEN),
        config=FactoryConfig(ci_wait_min=5))
    assert result.outcome == "pr_opened", result
    assert result.pr == url, "the fix round updates the same PR"
    assert len(env.prs) == 1, "no second PR was opened"
    task = env.task()
    assert task["status"] == "in_review"
    assert len(task["attempts"]) == attempts_before + 1
    assert len(seats.prompts("implementer")) == prompts_before + 1
    prompt = seats.prompts("implementer")[-1]
    assert "This is attempt 1 of 1." in prompt, \
        "the reset budget counts from zero"
    head2 = env.remote_branch_sha(p.branch)
    assert head2 != head and task["links"]["pushed_head"] == head2
    # the widened path's content is on the branch: the diff the round
    # pushed touches the path the original scope refused.
    out = subprocess.run(
        ["git", "-C", str(env.remote), "show", f"{head2}:{WIDENED_FILE}"],
        capture_output=True, text=True)
    assert out.returncode == 0 and "WIDENED = True" in out.stdout, out.stderr
