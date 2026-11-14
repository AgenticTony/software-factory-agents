"""F-77 — A ``ci_failed`` PR gets more budget without losing its work.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f77.py::test_ac_N``
means a test function with exactly that name.

Three acceptance criteria (BUILD-ORDER §F-77):

- AC-1 ``factory recover <task-id> --reset-attempts --evidence "…"`` on a
  task parked ``ci_failed`` whose PR GitHub reports OPEN keeps the PR:
  ``needs_human → in_review`` with the usual ``attempt_resets`` marker,
  links untouched, the draft marked ready again; a CLOSED PR keeps
  today's exit (``ready``/``blocked``); MERGED or unreadable refuses and
  writes nothing; ``attempts_exhausted`` is unchanged; the gate is one
  state-layer writer and the generic transition still refuses;
- AC-2 the next sweep runs F-75's CI fix round on that same PR — no
  second flake re-run for a head already re-run, and a fresh budget
  spent with CI still red parks ``ci_failed`` again (drafted);
- AC-3 a sweep park's result names the task's PR, so ``factory loop``'s
  ``parked:`` line (and ``factory run``) print it; "no PR" only when the
  task holds no PR link.

Real vs stubbed: the F-76 AC-3 harness as it stands (tests/_support/
f76.py) — git, the clone manager, the queue, the L0/L1 sandboxes and the
push to the local bare remote are real; the model seats and the CI/gh
seams are scripted. No new fake GitHub.
"""

import json
from pathlib import Path

import pytest
from _support.f72 import Seats, _task
from _support.f75 import CI, Actions, FakeClock, ci_state
from _support.f76 import Env76

import factory.orchestrator as orch
from factory.approve import ApprovalDeclined, ApprovalError, recover_task
from factory.config import FactoryConfig
from factory.progress import ProgressLog
from factory.publish import PublishError
from factory.state import (
    StateError,
    TaskQueue,
    TransitionError,
    attempt_budget_used,
)

CHECK_NAME = "Backend (3.11)"
FAILED_RUN = {"id": 101, "name": CHECK_NAME, "conclusion": "failure"}
LOG_TAIL = (
    f"{CHECK_NAME}\tRun tests\t2026-11-14T09:00:00.000Z\t"
    "FAILED tests/test_doc.py::test_ac_2 - AssertionError: extracted "
    "text differs on the CI runner\n"
)
RED = ci_state("failed", checks=((CHECK_NAME, "failure"),))
PENDING = ci_state("pending", checks=((CHECK_NAME, "in_progress"),))
GREEN = ci_state("success", checks=((CHECK_NAME, "success"),))
# The sweep's not-ready poll, settle's first wait, the flake re-run's
# observation poll, then red again (the last repeats).
RED_SCRIPT = [RED, RED, PENDING, RED]
IMPL_FIXED = "def answer():\n    return 42  # CI fix round\n"
EVIDENCE = "read the failing log; a one-line test fix, the diff is fine"
ONE_ATTEMPT = {"size": "S", "max_attempts": 1, "max_turns": 20,
               "wall_clock_min": 30}


def _seams(monkeypatch, env: Env76, actions: Actions | None = None) -> Actions:
    """Patch the F-75 orchestrator seams; returns the Actions fake."""
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

    def __init__(self, tmp: Path, monkeypatch, tasks: list | None = None,
                 seats: Seats | None = None):
        self.env = Env76(tmp, tasks=tasks or [_task(budget=ONE_ATTEMPT)])
        _seams(monkeypatch, self.env)
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

    def queue(self) -> TaskQueue:
        return TaskQueue(self.env.queue_path)

    def snapshot(self) -> str:
        return json.dumps(self.env.task(), sort_keys=True)

    def log(self) -> list:
        return ProgressLog.for_task(self.env.root, "fixture", "T-1").entries()

    def recover(self, *, state="open", **kwargs):
        """`factory recover T-1 --reset-attempts --evidence …` with the
        gh seams scripted; returns (status, echoed text, readied urls)."""
        echoed: list[str] = []
        readied: list[str] = []

        def pr_state(url):
            if isinstance(state, BaseException):
                raise state
            return state

        kwargs.setdefault("evidence", EVIDENCE)
        kwargs.setdefault("confirm", lambda question: True)
        kwargs.setdefault("pr_ready", readied.append)
        status = recover_task(
            self.queue(), "T-1", reset_attempts=True, done_by="owner",
            factory_root=self.env.root, pr_state=pr_state,
            echo=echoed.append, **kwargs)
        return status, "\n".join(echoed), readied


def _append_escalation(queue: TaskQueue, escalation: str) -> None:
    queue.append_attempt("T-1", {
        "run_id": "20261114T000000Z-f77f77",
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


# ============================================================= AC-1 =========


def test_ac_1(tmp_path: Path, monkeypatch, capsys):
    """The keep-the-PR exit: an OPEN ci_failed PR returns to in_review
    with a fresh budget and its links untouched; CLOSED keeps today's
    exit; MERGED/unreadable refuse writing nothing; attempts_exhausted
    is unchanged; the gate is the state layer's."""
    # (1) OPEN: needs_human → in_review, the marker, links untouched,
    # the draft marked ready again, the log entry, the echo names the
    # outcome BEFORE the y/N.
    p = Parked(tmp_path / "open", monkeypatch)
    before = p.env.task()
    asked: list[str] = []
    seen_at_prompt: list[str] = []
    echoed: list[str] = []
    readied: list[str] = []

    def confirm(question: str) -> bool:
        asked.append(question)
        seen_at_prompt.append("\n".join(echoed))
        return True

    status = recover_task(
        p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root,
        pr_state=lambda url: "open", pr_ready=readied.append,
        confirm=confirm, echo=echoed.append)
    assert status == "in_review"
    task = p.env.task()
    assert task["status"] == "in_review"
    assert task["links"] == before["links"], "links are untouched"
    assert task["attempts"] == before["attempts"], "history is kept"
    marker = task["attempt_resets"][-1]
    assert marker["prior_attempts"] == len(before["attempts"])
    assert marker["evidence"] == EVIDENCE
    assert marker["done_by"] == "owner"
    assert marker["slice_id"] == "T-1"
    assert len(task["attempt_resets"]) == 1, "ONE marker per recovery"
    assert attempt_budget_used(before, "T-1") >= 1
    assert attempt_budget_used(task, "T-1") == 0, "the budget starts at zero"
    assert readied == [p.url], "the drafted PR is marked ready again"
    assert asked and len(seen_at_prompt) == 1
    assert p.url in seen_at_prompt[0], "the echo names the PR before the y/N"
    assert "keeps PR" in seen_at_prompt[0] \
        and "fix round on that PR" in seen_at_prompt[0], seen_at_prompt[0]
    assert "will re-implement" not in seen_at_prompt[0]
    entry = p.log()[-1]
    assert entry["outcome"] == "recovered"
    assert entry["escalation"] == "ci_failed"
    assert entry["evidence"] == EVIDENCE
    assert entry["done_by"] == "owner"
    assert entry["attempts_reset"] == len(before["attempts"])
    assert "in_review" in entry["note"] and p.url in entry["note"]

    # (2) CLOSED: today's exit — ready, the marker, no gh ready call;
    # the echo says the work is re-implemented from main.
    p = Parked(tmp_path / "closed", monkeypatch)
    before = p.env.task()
    status, text, readied = p.recover(state="closed")
    assert status == "ready"
    task = p.env.task()
    assert task["status"] == "ready"
    assert task["links"] == before["links"]
    assert task["attempt_resets"][-1]["prior_attempts"] == len(
        before["attempts"])
    assert readied == [], "a closed PR is never marked ready"
    assert "will re-implement" in text and "keeps PR" not in text, text
    entry = p.log()[-1]
    assert entry["outcome"] == "recovered"
    assert entry["escalation"] == "ci_failed"
    assert "ready" in entry["note"]

    # (2b) CLOSED with an open dependency routes blocked, as today.
    p = Parked(tmp_path / "closed-blocked", monkeypatch, tasks=[
        _task(budget=ONE_ATTEMPT), _task("T-0", status="draft")])
    q = p.queue()
    q.get("T-1")["depends_on"] = ["T-0"]   # an open dependency
    q.save()
    status, text, readied = p.recover(state="closed")
    assert status == "blocked" and p.env.task()["status"] == "blocked"

    # (3) MERGED, unreadable (gh raised) and an unknown answer refuse,
    # the reason named, NOTHING written (queue, log, gh).
    for name, state, word in (
            ("merged", "merged", "merged"),
            ("unreadable", PublishError("gh: connection refused"),
             "unreachable"),
            ("unknown", "weird", "weird")):
        p = Parked(tmp_path / name, monkeypatch)
        snap, log_before = p.snapshot(), len(p.log())
        confirmed: list[str] = []
        with pytest.raises(ApprovalError, match=word):
            p.recover(state=state,
                      confirm=lambda q, c=confirmed: c.append(q) or True)
        assert p.snapshot() == snap, f"{name}: the refusal wrote the queue"
        assert len(p.log()) == log_before, f"{name}: the refusal logged"
        assert confirmed == [], f"{name}: refused before the prompt"

    # (4) attempts_exhausted holding an OPEN normal PR link: today's
    # behaviour — ready, and GitHub is never even asked.
    p = Parked(tmp_path / "exhausted", monkeypatch)
    q = p.queue()
    _append_escalation(q, "attempts_exhausted")
    q.save()
    assert p.env.task()["attempts"][-1]["escalation"] == "attempts_exhausted"

    def never(url):
        raise AssertionError("attempts_exhausted must not read PR state")

    readied = []
    status = recover_task(
        p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root, pr_state=never,
        pr_ready=readied.append, confirm=lambda question: True,
        echo=lambda text: None)
    assert status == "ready" and p.env.task()["status"] == "ready"
    assert readied == []
    assert p.log()[-1]["escalation"] == "attempts_exhausted"
    # … and the state layer refuses the in_review target for it outright
    p = Parked(tmp_path / "exhausted-state", monkeypatch)
    q = p.queue()
    _append_escalation(q, "attempts_exhausted")
    q.save()
    snap = p.snapshot()
    with pytest.raises(TransitionError, match="ci_failed"):
        p.queue().recover_attempts("T-1", EVIDENCE, keep_pr=True)
    assert p.snapshot() == snap

    # (5) the gate lives in the state layer: the generic transition
    # refuses every exit of a ci_failed park; the in_review target
    # refuses an escalation draft PR, a missing PR link, a merged stamp
    # and blank evidence — each writing nothing.
    p = Parked(tmp_path / "state", monkeypatch)
    snap = p.snapshot()
    for target in ("in_review", "ready", "blocked"):
        with pytest.raises(TransitionError):
            p.queue().transition("T-1", target)
    with pytest.raises(StateError, match="evidence"):
        p.queue().recover_attempts("T-1", "   ", keep_pr=True)
    assert p.snapshot() == snap
    for mutate in (
            lambda links: links.update(escalation_pr=True),
            lambda links: links.pop("pr"),
            lambda links: links.update(merged=links["pr"])):
        q = p.queue()
        saved = dict(q.get("T-1")["links"])
        mutate(q.get("T-1")["links"])
        q.save()
        shaped = p.snapshot()
        with pytest.raises(TransitionError, match="normal PR"):
            p.queue().recover_attempts("T-1", EVIDENCE, keep_pr=True)
        assert p.snapshot() == shaped
        q = p.queue()
        q.get("T-1")["links"] = saved
        q.save()
    # the same writer still moves the eligible task (the control)
    assert p.queue().recover_attempts(
        "T-1", EVIDENCE, done_by="owner", keep_pr=True) == "in_review"
    assert p.env.task()["status"] == "in_review"

    # (5b) a ci_failed park holding an ESCALATION draft PR never takes
    # the in_review target through the command either: today's exit.
    p = Parked(tmp_path / "escalation-pr", monkeypatch)
    q = p.queue()
    q.get("T-1")["links"]["escalation_pr"] = True
    q.save()
    status, text, readied = p.recover(state="open")
    assert status == "ready" and readied == []
    assert "keeps PR" not in text

    # (6) the human-paced gates stay as they are: --reset-attempts,
    # evidence, no --yes, a decline — each writing nothing.
    p = Parked(tmp_path / "gates", monkeypatch)
    snap, log_before = p.snapshot(), len(p.log())
    with pytest.raises(ApprovalError, match="reset-attempts"):
        recover_task(p.queue(), "T-1", evidence=EVIDENCE,
                     factory_root=p.env.root, pr_state=lambda u: "open",
                     confirm=lambda question: True, echo=lambda t: None)
    for blank in (None, "", "   "):
        with pytest.raises(ApprovalError, match="evidence"):
            p.recover(evidence=blank)
    with pytest.raises(ApprovalError, match="interactive") as caught:
        p.recover(assume_yes=True)
    assert "ci_failed recovery" in str(caught.value), \
        "the refusal names the escalation the task is parked on"
    with pytest.raises(ApprovalDeclined):
        p.recover(confirm=lambda question: False)
    assert p.snapshot() == snap and len(p.log()) == log_before
    from factory.cli import main

    assert main(["recover", "T-1", "--queue", str(p.env.queue_path),
                 "--factory-root", str(p.env.root), "--reset-attempts",
                 "--yes", "--evidence", "x"]) == 1
    assert p.snapshot() == snap

    # (7) the run lease: while a run holds it the keep-PR move refuses
    # cleanly and writes nothing.
    holder = p.queue()
    with holder.idle_lease():
        with pytest.raises(ApprovalError, match="run lease is held"):
            p.recover()
    assert p.snapshot() == snap and len(p.log()) == log_before

    # (7b) the PR state is re-read under the lease: a PR that stopped
    # being open while the owner sat at the prompt refuses, nothing
    # written.
    answers = ["open", "merged"]
    with pytest.raises(ApprovalError, match="merged"):
        recover_task(
            p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
            factory_root=p.env.root,
            pr_state=lambda url: answers.pop(0),
            pr_ready=lambda url: None, confirm=lambda question: True,
            echo=lambda text: None)
    assert p.snapshot() == snap and len(p.log()) == log_before

    # (8) `gh pr ready` failing AFTER the state write: the state is
    # right, the command still succeeds, and it says so loudly (echo and
    # the log note).
    def ready_fails(url):
        raise PublishError("gh pr ready failed: HTTP 502")

    status, text, _ = p.recover(pr_ready=ready_fails)
    assert status == "in_review" and p.env.task()["status"] == "in_review"
    assert "WARNING" in text and "HTTP 502" in text and "draft" in text, text
    assert "HTTP 502" in p.log()[-1]["note"]

    # (9) recover twice in a row: the second finds an in_review task and
    # refuses; one marker only.
    snap = p.snapshot()
    with pytest.raises(ApprovalError, match="not recoverable"):
        p.recover()
    assert p.snapshot() == snap
    assert len(p.env.task()["attempt_resets"]) == 1

    # (10) the CLI end to end (interactive y, real seams patched at the
    # module): exit 0, and the line names the keep-PR outcome — never
    # the F-72 "escalation accepted" wording.
    import factory.approve as approve_mod

    p = Parked(tmp_path / "cli", monkeypatch)
    readied = []
    monkeypatch.setattr(approve_mod, "gh_pr_state", lambda url: "open")
    monkeypatch.setattr(approve_mod, "gh_pr_ready", readied.append)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    capsys.readouterr()
    assert main(["recover", "T-1", "--queue", str(p.env.queue_path),
                 "--factory-root", str(p.env.root), "--reset-attempts",
                 "--evidence", EVIDENCE]) == 0
    out = capsys.readouterr().out
    assert "T-1: needs_human -> in_review" in out
    assert "PR kept" in out and "escalation accepted" not in out, out
    assert readied == [p.url]
    assert p.env.task()["status"] == "in_review"
    help_text = main_help(capsys)
    assert "ci_failed" in help_text and "keeps" in help_text


def main_help(capsys) -> str:
    """`factory recover --help`, whitespace-normalised."""
    from factory.cli import main

    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["recover", "--help"])
    return " ".join(capsys.readouterr().out.split())


# ============================================================= AC-2 =========


def test_ac_2(tmp_path: Path, monkeypatch):
    """After the AC-1 recovery the next sweep runs F-75's fix round on
    the SAME PR: the failing check rides the prompt, the head already on
    ``links.ci_rerun_heads`` is not re-run, and a fresh budget spent
    with CI still red parks ``ci_failed`` again, drafted."""
    p = Parked(tmp_path / "compose", monkeypatch)
    env, seats, url, head = p.env, p.seats, p.url, p.head
    # the control: parked, the sweep leaves it alone — no session, no CI
    # read, whatever CI says.
    ci = CI(by_sha={head: [RED, RED]}, default=GREEN)
    result = env.run_once(seats, ci_state=ci,
                          config=FactoryConfig(ci_wait_min=5))
    assert result.outcome == "idle", result
    assert len(seats.prompts("implementer")) == 1
    assert ci.calls == [], "a parked ci_failed PR is not the sweep's"

    status, _, readied = p.recover(state="open")
    assert status == "in_review" and readied == [url]
    attempts_before = len(env.task()["attempts"])

    # the next run: the sweep proves the PR current, finds its CI red on
    # the factory's own pushed head, skips the flake re-run (once per
    # head, already spent) and starts the fix round on the same PR.
    actions = _seams(monkeypatch, env)
    result = env.run_once(
        seats, ci_state=CI(by_sha={head: [RED, RED]}, default=GREEN),
        config=FactoryConfig(ci_wait_min=5))
    assert result.outcome == "pr_opened", result
    assert result.pr == url, "the fix round updates the same PR"
    assert len(env.prs) == 1, "no second PR was opened"
    assert actions.rerun_calls == [], \
        "a head already re-run once is never re-run again"
    task = env.task()
    assert task["status"] == "in_review"
    assert task["links"]["pr"] == url
    assert len(task["attempts"]) == attempts_before + 1
    assert len(seats.prompts("implementer")) == 2
    prompt = seats.prompts("implementer")[-1]
    assert "## CI findings to fix (untrusted CI output" in prompt
    assert CHECK_NAME in prompt
    assert "FAILED tests/test_doc.py::test_ac_2" in prompt
    assert "This is attempt 1 of 1." in prompt, \
        "the reset budget counts from zero"
    head2 = env.remote_branch_sha(p.branch)
    assert head2 != head and task["links"]["pushed_head"] == head2
    assert env.draft_calls == [url], "no new draft conversion on the fix"

    # the fresh budget is spent too and CI is red on the fixed head: the
    # sweep re-runs that NEW head once, then parks ci_failed again,
    # session-less, the PR drafted.
    actions = _seams(monkeypatch, env)
    result = env.run_once(
        seats, ci_state=CI(by_sha={head2: list(RED_SCRIPT)}, default=RED),
        config=FactoryConfig(ci_wait_min=5))
    assert result.outcome == "needs_human", result
    assert result.escalation == "ci_failed"
    assert len(seats.prompts("implementer")) == 2, "no session: budget spent"
    assert len(actions.rerun_calls) == 1, "the new head gets its one re-run"
    task = env.task()
    assert task["status"] == "needs_human"
    assert task["attempts"][-1]["escalation"] == "ci_failed"
    assert env.draft_calls == [url, url], "parked again, drafted again"
    assert len(env.prs) == 1


# ============================================================= AC-3 =========


def test_ac_3(tmp_path: Path, monkeypatch, capsys):
    """A sweep park's result names the task's PR — ``ci_failed``,
    ``stale_base`` and ``pr_closed`` alike — so ``factory loop``'s
    ``parked:`` line and ``factory run`` print it; "no PR" only when
    the task holds no PR link. No GitHub re-read for the line."""
    import factory.cli as cli
    from factory.orchestrator import LoopResult, RunResult

    # (1) ci_failed, parked by the sweep
    p = Parked(tmp_path / "ci-failed", monkeypatch)
    assert p.park_result.escalation == "ci_failed"
    assert p.park_result.pr == p.url, "the sweep park names the PR"
    ci_failed_result = p.park_result

    # (2) stale_base, parked by the sweep (a real merge conflict)
    env = Env76(tmp_path / "stale")
    _seams(monkeypatch, env)
    assert env.run_once(Seats(), ci_state=CI(default=GREEN)).outcome \
        == "pr_opened"
    stale_url = env.task()["links"]["pr"]
    env.advance_remote_main("def answer():\n    return 99\n",
                            message="conflicting remote change")
    stale_result = env.run_once(Seats(), ci_state=CI(default=GREEN))
    assert stale_result.outcome == "needs_human", stale_result
    assert stale_result.escalation == "stale_base"
    assert stale_result.pr == stale_url

    # (3) pr_closed, parked by the sweep
    env = Env76(tmp_path / "closed")
    _seams(monkeypatch, env)
    assert env.run_once(Seats(), ci_state=CI(default=GREEN)).outcome \
        == "pr_opened"
    closed_url = env.task()["links"]["pr"]
    closed_result = env.run_once(Seats(), ci_state=CI(default=GREEN),
                                 pr_state=lambda url: "closed")
    assert closed_result.escalation == "pr_closed"
    assert closed_result.pr == closed_url

    # (4) the loop's parked lines, printed by the real CLI from those
    # results — plus a park with no link, the only "no PR".
    bare = RunResult("needs_human", "T-9", escalation="attempts_exhausted")
    parked = [ci_failed_result, stale_result, closed_result, bare]

    def no_gh(*a, **kw):
        raise AssertionError("the parked line re-read GitHub")

    monkeypatch.setattr(cli, "run_loop", lambda *a, **kw: LoopResult(
        "queue_empty", list(parked), parked=list(parked)))
    monkeypatch.setattr("factory.mergeorder.gh_pr_state", no_gh)
    capsys.readouterr()
    code = cli.main(["loop", "--queue", str(p.env.queue_path),
                     "--factory-root", str(p.env.root)])
    out = capsys.readouterr().out
    assert code == 1, "a parked batch is not a clean finish"
    assert f"  parked: T-1 (ci_failed) — {p.url}" in out, out
    assert f"  parked: T-1 (stale_base) — {stale_url}" in out, out
    assert f"  parked: T-1 (pr_closed) — {closed_url}" in out, out
    assert "  parked: T-9 (attempts_exhausted) — no PR" in out, out
    assert out.count("no PR") == 1, out

    # (5) `factory run` agrees: the reason AND the PR on one line.
    monkeypatch.setattr(cli, "run_once", lambda *a, **kw: ci_failed_result)
    capsys.readouterr()
    cli.main(["run", "--queue", str(p.env.queue_path),
              "--factory-root", str(p.env.root)])
    out = capsys.readouterr().out
    assert "needs_human T-1" in out and "ci_failed" in out and p.url in out, \
        out
