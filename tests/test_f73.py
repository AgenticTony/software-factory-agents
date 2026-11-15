"""F-73: ready clones — the factory's identity and setup before the session.

Contract tests for BUILD-ORDER §F-73 (AC-1, AC-2). The harness family's
split holds: git, the clone manager, the queue, the L0/L1 sandboxes and
the push to the local bare remote are real; the seats and the CI/gh
seams are scripted (``_support.f73``).
"""

from __future__ import annotations

import yaml
from _support.f72 import _finding, _task, _verdict
from _support.f73 import (
    DIRTY_SETUP,
    FACTORY_EMAIL,
    FACTORY_NAME,
    FAIL_SETUP,
    VENV_SETUP,
    Env73,
    GitSeats,
    git_out,
    identities_of,
)
from _support.f78 import IMPL_FIXED, ONE_ATTEMPT, Parked
from _support.f79 import fix_round, recover_open

from factory.mergeorder import CIState
from factory.orchestrator import _implementer_prompt
from factory.state import attempt_budget_used

FACTORY = (FACTORY_NAME, FACTORY_EMAIL, FACTORY_NAME, FACTORY_EMAIL)
GREEN_CI = lambda remote, sha: CIState("success")  # noqa: E731
# The implementer's own commit, under the repo-local identity the parent
# configured — the AC-1 "commit as the factory identity" arm.
PLAIN_COMMIT = "git add -A && git commit -q -m 'session commit'"
# The same commit made as someone else — the AC-2 arm (wo21's c8a8ead).
JANE_COMMIT = ("git add -A && git -c user.name='Jane Devel' "
               "-c user.email=jane@example.com commit -q -m 'session commit'")
FAIL_VERDICT = _verdict(_finding(text="the guard is unwired",
                                 severity="major", axis="spec"))


def _factory_only(env, branch: str) -> None:
    """Assert every identity in base..branch on the REMOTE is the
    factory's (the assertion message carries the offenders)."""
    ids = identities_of(env.remote, env.base_sha, env.remote_branch_sha(branch))
    foreign = [tuple(i) for i in ids if tuple(i) != FACTORY]
    assert not foreign, f"pushed history carries foreign identities: {foreign}"


def test_ac_1(tmp_path):
    """The clone is ready before the session: identity + setup in the run
    clone, the prompt says so, and a broken setup starts no session."""
    # ---- green arm: setup builds a gitignored venv; the implementer
    # commits as the factory identity and sees the venv.
    env = Env73(tmp_path / "green", profile_extra={"commands": {"setup": VENV_SETUP}})
    seats = GitSeats(git_commands=[PLAIN_COMMIT])
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "pr_opened", result
    seen = seats.clone_seen[0]
    assert seen["venv_marker"], "the setup-created venv must exist in the clone"
    assert seen["user.name"] == FACTORY_NAME, seen
    assert seen["user.email"] == FACTORY_EMAIL, seen
    prompt = seats.impl_prompts[0]
    assert "Setup has already run" in prompt, prompt[-800:]
    assert VENV_SETUP in prompt, "the prompt must name the setup command"
    assert f"{FACTORY_NAME} <{FACTORY_EMAIL}>" in prompt, prompt[-800:]
    # The session's own commit — a plain git commit, no identity flags —
    # landed as the factory identity, and so did everything pushed.
    assert (seats.commit_ids[0]["an"], seats.commit_ids[0]["ae"]) == \
        (FACTORY_NAME, FACTORY_EMAIL), seats.commit_ids[0]
    _factory_only(env, env.task()["links"]["branch"])
    assert [a["outcome"] for a in env.task()["attempts"]] == ["pass"]

    # ---- failing setup: no session, setup_failed, no budget spent.
    env = Env73(tmp_path / "fail", profile_extra={"commands": {"setup": FAIL_SETUP}})
    seats = GitSeats()
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "error", result
    assert result.escalation == "setup_failed", result
    assert seats.prompts("implementer") == [], "a failing setup starts no session"
    task = env.task()
    attempt = task["attempts"][-1]
    assert attempt["outcome"] == "fail"
    assert attempt["escalation"] == "setup_failed"
    assert "boom-setup" in (attempt.get("note") or ""), attempt.get("note")
    assert task["status"] == "ready", task["status"]  # re-queued for the next run
    assert attempt_budget_used(task, "T-1") == 0, "setup_failed spends no budget"
    # The budget is REALLY unchanged: with the setup fixed, the next run
    # is attempt 1 of its budget and publishes.
    profile_path = env.repo / ".factory" / "project.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["commands"]["setup"] = "true"
    profile_path.write_text(yaml.safe_dump(profile))
    fixed = GitSeats(git_commands=[PLAIN_COMMIT])
    result = env.run(fixed, ci_state=GREEN_CI)
    assert result.outcome == "pr_opened", result
    attempts = env.task()["attempts"]
    assert len(attempts) == 2, [a.get("escalation") for a in attempts]
    assert attempts[-1]["rounds"] == 1, attempts[-1]
    assert attempts[-1]["outcome"] == "pass"

    # ---- dirty setup: an un-ignored file refuses, naming it.
    env = Env73(tmp_path / "dirty", profile_extra={"commands": {"setup": DIRTY_SETUP}})
    seats = GitSeats()
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "error", result
    assert result.escalation == "setup_failed", result
    assert seats.prompts("implementer") == [], "a dirty setup starts no session"
    note = env.task()["attempts"][-1].get("note") or ""
    assert "stray-untracked.txt" in note, note


def test_ac_2(tmp_path):
    """Nothing is published under another identity: a foreign-authored
    commit is repaired by the squash (tree unchanged) and named in the
    attempt record; an all-factory run keeps its round commits."""
    # ---- the foreign arm: the session commits as Jane Devel.
    env = Env73(tmp_path / "foreign")
    seats = GitSeats(git_commands=[JANE_COMMIT])
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "pr_opened", result
    assert (seats.commit_ids[0]["an"], seats.commit_ids[0]["ae"]) == \
        ("Jane Devel", "jane@example.com"), seats.commit_ids[0]
    branch = env.task()["links"]["branch"]
    _factory_only(env, branch)
    assert env.pushed_commit_count(branch) == 1, \
        "a foreign identity rebuilds the branch as one commit on the base"
    # The validated tree is unchanged, byte for byte: the pushed head's
    # tree equals the tree the session's (foreign) commit held.
    pushed = git_out(env.remote, "rev-parse",
                     f"{env.remote_branch_sha(branch)}^{{tree}}")
    assert pushed == seats.commit_ids[0]["tree"], \
        "the repair must publish the exact validated tree"
    note = env.task()["attempts"][-1].get("note") or ""
    assert "jane@example.com" in note, note
    assert "Jane Devel" in note, note

    # ---- the all-factory arm: the round commits are kept as they are.
    env = Env73(tmp_path / "factory")
    seats = GitSeats(git_commands=[
        "git add impl.py && git commit -q -m 'session commit'"])
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "pr_opened", result
    branch = env.task()["links"]["branch"]
    _factory_only(env, branch)
    assert env.pushed_commit_count(branch) == 2, \
        "an all-factory history keeps its round commits (session + parent)"
    assert not (env.task()["attempts"][-1].get("note") or ""), \
        "no identity repair ran, so no repair note"


def test_escalation_push_reattributes(tmp_path):
    """The F-72 escalation push carries only the factory identity too: a
    foreign-authored round history is re-attributed by the single-commit
    snapshot and named in the attempt note."""
    env = Env73(tmp_path / "esc", tasks=[_task(budget=ONE_ATTEMPT)])
    seats = GitSeats(git_commands=[JANE_COMMIT], reviews=(FAIL_VERDICT,))
    result = env.run(seats, ci_state=GREEN_CI)
    assert result.outcome == "needs_human", result
    assert result.pr, result
    assert env.prs and env.prs[0].draft, env.prs
    branch = env.task()["links"]["branch"]
    _factory_only(env, branch)
    assert env.pushed_commit_count(branch) == 1
    note = env.task()["attempts"][-1].get("note") or ""
    assert "jane@example.com" in note, note


def test_fix_round_update_reattributes(tmp_path, monkeypatch):
    """A CI fix round whose session commits as someone else publishes
    through the leased UPDATE push: the identity repair re-roots the
    branch on the base, which the fast-forward rule must allow exactly
    as F-72's integrity squash does."""
    seats = GitSeats(impl_files=[{}, {"impl.py": IMPL_FIXED}],
                     git_commands=[JANE_COMMIT])
    p = Parked(tmp_path / "fix", monkeypatch, seats=seats)
    assert recover_open(p) == "in_review"
    result = fix_round(p, monkeypatch)
    assert result.outcome == "pr_opened", result
    _factory_only(p.env, p.branch)
    assert p.env.pushed_commit_count(p.branch) == 1, \
        "the repair rebuilds the whole PR range as one commit on the base"
    assert p.env.task()["attempts"][-1].get("note"), \
        "the identity repair is named in the attempt record"


def test_resume_kept_factory_identity(tmp_path, monkeypatch):
    """F-79's kept-work commit stays under the factory identity through a
    resume: the kept commit is parent-built, and the resumed round's
    plain session commits ride the repo-local identity — everything that
    publishes is factory-authored. (kept_park's shape, driven directly
    so the seats can write real commits.)"""
    from _support.f79 import IMPL_DONE, IMPL_WIP

    seats = GitSeats(impl_files=[{}, {"impl.py": IMPL_WIP},
                                 {"impl.py": IMPL_DONE}],
                     reviews=(_verdict(), FAIL_VERDICT, _verdict()),
                     git_commands=[PLAIN_COMMIT])
    p = Parked(tmp_path / "wip", monkeypatch, seats=seats)
    assert recover_open(p) == "in_review"
    # The fix round passes the gates, fails review, spends the one
    # attempt: the work is kept locally (the wo21 shape).
    park = fix_round(p, monkeypatch)
    assert park.outcome == "needs_human", park
    assert park.escalation == "attempts_exhausted", park
    wip = dict(p.env.task()["links"].get("wip") or {})
    assert wip, "the round's work was kept"
    assert recover_open(p) == "in_review"
    result = fix_round(p, monkeypatch)
    assert result.outcome == "pr_opened", result
    _factory_only(p.env, p.branch)
    # Every seat saw the repo-local factory identity, including the
    # round that resumed the kept commit.
    assert seats.clone_seen
    for seen in seats.clone_seen:
        assert (seen["user.name"], seen["user.email"]) == \
            (FACTORY_NAME, FACTORY_EMAIL), seen


def test_setup_failed_spends_no_budget_unit():
    """The budget filter skips setup_failed attempts (state-layer unit)."""
    task = _task()
    task["attempts"] = [{
        "run_id": "20261115T000000Z-f73f73", "slice_id": "T-1",
        "started": "2026-11-15T00:00:00Z", "ended": "2026-11-15T00:00:00Z",
        "outcome": "fail", "rounds": 1, "turns": 0,
        "input_tokens": 0, "output_tokens": 0,
        "prompt_versions": {}, "skills": [],
        "escalation": "setup_failed", "note": "setup failed (exit 3)",
    }]
    assert attempt_budget_used(task, "T-1") == 0
    task["attempts"].append({
        "run_id": "20261115T000001Z-f73f74", "slice_id": "T-1",
        "started": "2026-11-15T00:01:00Z", "ended": "2026-11-15T00:01:00Z",
        "outcome": "pass", "rounds": 1, "turns": 3,
        "input_tokens": 0, "output_tokens": 0,
        "prompt_versions": {}, "skills": [],
    })
    assert attempt_budget_used(task, "T-1") == 1


def test_prompt_names_setup_and_identity():
    """The implementer prompt: setup named when one ran, the identity
    line always; no setup claim when the profile has no setup."""
    task = _task()
    with_setup = _implementer_prompt(
        task, None, "feat/x", "", 1, 3, setup_command="pip install -e .")
    assert "Setup has already run" in with_setup
    assert "pip install -e ." in with_setup
    assert f"{FACTORY_NAME} <{FACTORY_EMAIL}>" in with_setup
    without = _implementer_prompt(task, None, "feat/x", "", 1, 3,
                                  setup_command=None)
    assert "Setup has already run" not in without
    assert f"{FACTORY_NAME} <{FACTORY_EMAIL}>" in without
