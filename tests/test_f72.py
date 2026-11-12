"""F-72 — Escalations keep the work, retry first, and never stop the batch.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f72.py::test_ac_N``
means a test function with exactly that name.

Five acceptance criteria (BUILD-ORDER §F-72):

- AC-1 guarded changes (protected paths, failing sentinels) retry before
  they escalate; the same key twice escalates; a reverted guarded change
  leaves no trace in the published history (squash-on-retry);
- AC-2 an arbiter seat settles a non-converging review before the owner;
- AC-3 an escalation after a session keeps the work as a draft PR through
  the existing push fence (never for credential_added);
- AC-4 the owner answers an escalation in one command (``recover
  --accept``/``--reject``), the waiver keyed to (task, class, SHA);
- AC-5 ``factory loop`` parks an escalation and carries on; the exit code
  stays honest (parked ⇒ 1).

Real vs stubbed: the F-38 split — git, clone manager, queue, L0/L1
sandboxes and the push to the local bare remote are real; only the model
seats and the gh read/write seams are scripted (tests/_support/f72.py).
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from _support.f63 import ledger_approve
from _support.f72 import (
    CREDENTIAL_LINE,
    IMPL_BAD,
    SEAT_TOKEN,
    Env72,
    GhSeams,
    Seats,
    _finding,
    _git,
    _ruling,
    _task,
    _verdict,
    rulings_payload,
)

from factory.approvals import ApprovalLedger
from factory.approve import ApprovalError, recover_task
from factory.contract import contract_hash, load_profile
from factory.orchestrator import LoopResult, run_loop
from factory.provider import resolve_provider
from factory.publish import PublishError
from factory.state import TaskQueue

# The escalation PR's visible marks (F-72 AC-3): a title prefix, a label.
ESCALATION_TITLE_PREFIX = "[escalation]"
ESCALATION_LABEL = "factory-escalation"

PYPROJECT_WITH_DEP = (
    "[project]\nname = 'fixture'\nversion = '0.1'\ndependencies = ['requests']\n"
)


def _run_once(env: Env72):
    from _support.f72 import OFF_PEAK

    from factory.mergeorder import Protection
    from factory.orchestrator import run_once

    return run_once(
        env.queue_path, factory_root=env.root, session_runner=Seats(),
        open_pr=env.open_pr, now=lambda: OFF_PEAK,
        protection=lambda r, b: Protection(checked=True, strict=True),
        pr_state=lambda url: "open",
    )


def _pushed_branch(env: Env72, task_id: str = "T-1") -> str:
    links = env.task(task_id).get("links") or {}
    branch = links.get("branch")
    assert branch, f"no branch link on the task: {links}"
    return branch


# --------------------------------------------------------------------------
# AC-1 — guarded changes retry before they escalate
# --------------------------------------------------------------------------


def test_ac_1(tmp_path: Path):
    from factory.orchestrator import ARBITER_TURN_CAP  # noqa: F401 — the seat exists

    # (1) touch protected in r1, revert in r2 → PR, and no commit in the
    # pushed history touches the protected path (the round history is
    # squashed when a guarded retry happened).
    env = Env72(tmp_path / "revert")
    seats = Seats(impl_files=[
        {".factory/evil.yaml": "boom: true\n"},
        {".factory/evil.yaml": None},
    ])
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    branch = _pushed_branch(env)
    pushed = env.pushed_paths(branch)
    assert ".factory/evil.yaml" not in pushed, (
        f"a reverted protected change must leave no trace in the published "
        f"history; pushed paths: {sorted(pushed)}"
    )
    # Proven on the commit history, not only the tree: one squashed commit.
    assert env.pushed_commit_count(branch) == 1
    # The r1 round was a FAIL round of the same attempt sequence (not an
    # escalation), its finding carried the revert instruction, and the
    # next implementer round saw it.
    task = env.task()
    assert [a["outcome"] for a in task["attempts"]] == ["fail", "pass"]
    fix_prompt = seats.prompts("implementer")[1]
    assert ".factory/evil.yaml" in fix_prompt
    assert "revert this" in fix_prompt

    # (2) touch it in r1 and r2 → protected_path_touched within 2 rounds,
    # note present. R2-F1 part 1: a guarded class keeps NO work on GitHub
    # — no push, no PR, the reports stay on disk and the attempt note
    # names the stored report path (the owner judges from the queue, and
    # the refused content never reaches a runner).
    env = Env72(tmp_path / "twice")
    seats = Seats(impl_files=[{".factory/evil.yaml": "boom: true\n"}])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "protected_path_touched"
    task = env.task()
    assert task["attempts"][-1]["escalation"] == "protected_path_touched"
    note = task["attempts"][-1].get("note") or ""
    assert "contract or the project rule" in note
    assert task["status"] == "needs_human"
    assert env.prs == []  # the guarded class never opens a PR
    assert env.remote_branches() == ["main"]  # and never pushes
    assert not (task.get("links") or {}).get("pr")
    report_ref = note.split("L0 report: ")[-1].strip()
    assert report_ref.startswith("/") and Path(report_ref).is_file(), note
    stored = json.loads(Path(report_ref).read_text())
    assert stored["L0"]["escalations"] == ["protected_path_touched"]

    # (3) a different path in round 2 is a NEW first strike, not a reset
    # of the original count: A, B, A escalates (the alternation attack).
    env = Env72(tmp_path / "ab")
    seats = Seats(impl_files=[
        {".factory/evil-a.yaml": "a\n"},
        {".factory/evil-a.yaml": None, ".factory/evil-b.yaml": "b\n"},
        {".factory/evil-b.yaml": None, ".factory/evil-a.yaml": "a2\n"},
    ],
        reviews=(_verdict(),) * 3)
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "protected_path_touched"

    # (4) path A in r1, path B in r2, clean r3 → PR (the round limit
    # allows it: max_attempts 3, the third round publishes).
    env = Env72(tmp_path / "ab-clean")
    seats = Seats(impl_files=[
        {".factory/evil-a.yaml": "a\n"},
        {".factory/evil-a.yaml": None, ".factory/evil-b.yaml": "b\n"},
        {".factory/evil-b.yaml": None},
    ],
        reviews=(_verdict(),) * 3)
    result = env.run(seats)
    assert result.outcome == "pr_opened", result

    # (5) a failing sentinel follows the same pattern: strike → finding,
    # revert → PR, twice → sentinel_failed.
    sentinel_profile = {
        "paths": {"risk": {"safety": ["evil.py"]}},
        "sentinels": {"safety": [{"command": "test ! -f evil.py",
                                  "description": "no-evil invariant"}]},
    }
    env = Env72(tmp_path / "sentinel-revert",
                tasks=[_task(risk=["safety"],
                             scope={"paths": ["impl.py", "tests/test_feature.py",
                                              "evil.py"]})],
                profile_extra=sentinel_profile)
    seats = Seats(impl_files=[{"evil.py": "x = 1\n"}, {"evil.py": None}])
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    env = Env72(tmp_path / "sentinel-twice",
                tasks=[_task(risk=["safety"],
                             scope={"paths": ["impl.py", "tests/test_feature.py",
                                              "evil.py"]})],
                profile_extra=sentinel_profile)
    seats = Seats(impl_files=[{"evil.py": "x = 1\n"}])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "sentinel_failed"
    sentinel_note = env.task()["attempts"][-1].get("note") or ""
    assert "contract or the project rule" in sentinel_note
    # R2-F1 part 1: the sentinel class keeps no work either — no push,
    # no PR, the report path named in the note.
    assert env.prs == [] and env.remote_branches() == ["main"]
    sentinel_report = sentinel_note.split("L0 report: ")[-1].strip()
    assert sentinel_report.startswith("/") and Path(sentinel_report).is_file(), \
        sentinel_note

    # (6) a credential escalates in round 1 — never retried, never pushed.
    env = Env72(tmp_path / "credential")
    seats = Seats(impl_files=[{"creds.py": CREDENTIAL_LINE + "\n"}])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "credential_added"
    assert seats.seats().count("implementer") == 1  # no retry
    assert env.prs == []  # nothing opened
    assert env.remote_branches() == ["main"]  # nothing pushed
    assert not (env.task().get("links") or {}).get("pr")

    # (7) bounded: the guarded retry never adds rounds beyond the budget —
    # with max_attempts 2, A-then-clean publishes on the second round and
    # A-then-A-then-anything can only escalate or exhaust.
    env = Env72(tmp_path / "bounded",
                tasks=[_task(budget={"size": "S", "max_attempts": 2,
                                     "max_turns": 20, "wall_clock_min": 30})])
    seats = Seats(impl_files=[{".factory/evil.yaml": "x\n"},
                              {".factory/evil.yaml": None}])
    result = env.run(seats)
    assert result.outcome == "pr_opened", result

    # (8) R1-F2: a guarded strike on the LAST round is never converted —
    # converting promises a next round that cannot run, would mask the
    # class behind attempts_exhausted, and would push the refused
    # content. It escalates as its own class, and nothing is pushed.
    env = Env72(tmp_path / "last-round",
                tasks=[_task(budget={"size": "S", "max_attempts": 1,
                                     "max_turns": 20, "wall_clock_min": 30})])
    seats = Seats(impl_files=[{".factory/evil.yaml": "boom: true\n"}])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "protected_path_touched", result.escalation
    pushed = set()
    for b in env.remote_branches():
        if b != "main":
            pushed |= env.pushed_paths(b)
    assert ".factory/evil.yaml" not in pushed, sorted(pushed)
    assert env.prs == []  # the masked-class push is refused

    # (9) R1-F3: a converted strike that a later round REVERTS still must
    # leave no trace in an ESCALATION PR's pushed history — _keep_work
    # squashes exactly as the publish path does. Round 2 reverts the
    # protected file but stays wrong (the contract test fails), so the
    # budget exhausts and the work is kept — squashed.
    env = Env72(tmp_path / "retry-then-exhaust",
                tasks=[_task(budget={"size": "S", "max_attempts": 2,
                                     "max_turns": 20, "wall_clock_min": 30})])
    seats = Seats(impl_files=[
        {".factory/evil.yaml": "boom: true\n"},
        {".factory/evil.yaml": None,
         "impl.py": "def answer():\n    return 0\n"},
    ])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "attempts_exhausted", result.escalation
    branch = next(b for b in env.remote_branches() if b != "main")
    assert ".factory/evil.yaml" not in env.pushed_paths(branch), (
        "a reverted guarded change rode the escalation PR's history")
    assert env.pushed_commit_count(branch) == 1  # the squashed snapshot
    assert env.prs and env.prs[0].draft


# --------------------------------------------------------------------------
# AC-2 — an arbiter settles a non-converging review before the owner
# --------------------------------------------------------------------------


def test_ac_2(tmp_path: Path):
    from factory.orchestrator import ARBITER_TURN_CAP

    # The no-progress shape: identical rounds (no new commit), the same
    # finding both rounds — round 2 is a carried repeat on untouched code.
    failing = _verdict(_finding())

    # (1) all `invalid` → PR, with the rulings in the report.
    env = Env72(tmp_path / "invalid")
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter={"ruling": "invalid", "reason": "noise"},
                  impl_result="Implemented. IMPLNOTE-qx7-mark")
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    attempts = env.task()["attempts"]
    l2_report = json.loads(Path(attempts[-1]["validator_report"]).read_text())["L2"]
    assert l2_report["arbiter_rulings"][0]["ruling"] == "invalid"
    # The dropped finding never reaches the PR body as work, but the drop
    # is recorded (mutation: invalid drops without recording).
    assert l2_report["arbiter_dropped"][0]["id"] == l2_report[
        "arbiter_rulings"][0]["id"]

    # (2) `valid` → exactly one more round; the same finding repeating
    # after the arbiter's round escalates with the rulings attached.
    env = Env72(tmp_path / "valid")
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter={"ruling": "valid", "reason": "real defect"})
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "review_not_converging"
    assert seats.seats().count("implementer") == 3  # r1, r2, arbiter's round
    attempts = env.task()["attempts"]
    l2_report = json.loads(Path(attempts[-1]["validator_report"]).read_text())["L2"]
    assert l2_report["arbiter_rulings"][0]["ruling"] == "valid"
    assert "arbiter" in (attempts[-1].get("note") or "")

    # (2b) NOTE (mutation M9's equivalence, honestly): a second
    # arbiter-eligible stop cannot happen inside one run — the tasks
    # schema caps budget.max_attempts at 3, the first stop can only
    # arrive at round 2 (no_progress needs a prior carry), and the stop
    # after the arbiter's extra round is always the LAST attempt, which
    # R1-F10 skips. The `if arbiter_used` guard is layered defence for
    # the day either bound moves; arm (6)'s count==1 pins the observable
    # behaviour.

    # (3) `contract` → escalation with the ruling shown.
    env = Env72(tmp_path / "contract")
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter={"ruling": "contract",
                           "reason": "criterion is self-contradictory"})
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "review_not_converging"
    attempts = env.task()["attempts"]
    l2_report = json.loads(Path(attempts[-1]["validator_report"]).read_text())["L2"]
    assert l2_report["arbiter_rulings"][0]["ruling"] == "contract"
    assert "criterion is self-contradictory" in (attempts[-1].get("note") or "")

    # (4) an arbiter session error → escalation.
    env = Env72(tmp_path / "error")
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter=Seats.SeatError("session died"))
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "review_not_converging"

    # (5) a ruling for an unknown id → treated as a failure → escalation.
    env = Env72(tmp_path / "unknown")
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter=rulings_payload(_ruling("R-99", "invalid", "no such")))
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "review_not_converging"
    assert "unknown finding id" in (
        env.task()["attempts"][-1].get("note") or "")

    # (6) the arbiter's prompt: contract + diff + ONLY the repeated
    # findings; no implementer note text (the planted marker), no reviewer
    # argument beyond the finding's own id and text.
    env = Env72(tmp_path / "prompt")
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter={"ruling": "invalid"},
                  impl_result="Implemented. IMPLNOTE-qx7-mark")
    env.run(seats)
    prompt = seats.prompts("arbiter")[0]
    assert "IMPLNOTE-qx7-mark" not in prompt
    assert "acceptance" in prompt
    assert "```diff" in prompt
    assert "the helper is misnamed" in prompt  # the finding text, as data
    # The seat ran under its own fixed cap (F-64 class), at most once.
    assert seats.kwargs("arbiter")[0]["max_turns"] == ARBITER_TURN_CAP
    assert seats.seats().count("arbiter") == 1
    # The arbiter's tokens count in the attempt's usage, seat-labelled.
    attempts = env.task()["attempts"]
    assert "arbiter=stub-model" in (attempts[-1].get("model") or "")
    assert attempts[-1]["token_usage"]["input"] >= 70  # the arbiter's stub tokens

    # (8) R1-F4: a control character in the arbiter's reason neither
    # breaks the attempt write nor orphans the PR — every session-written
    # string is escaped before it reaches a note or the PR body, and the
    # links are durable before anything later can fail.
    hostile_reason = "bad\x1b[2J\x1b]0;pwned\x07 appro‭ved: evil"
    env = Env72(tmp_path / "hostile-reason")
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter={"ruling": "contract", "reason": hostile_reason})
    result = env.run(seats)
    assert result.outcome == "needs_human", result  # never an error
    assert result.escalation == "review_not_converging"
    task = env.task()
    assert task["links"].get("pr") == result.pr  # the PR is linked, not orphaned
    note = task["attempts"][-1].get("note") or ""
    assert "\x1b" not in note and "‭" not in note
    # the composed seam: the ESC/OSC attack sequence is STRIPPED (the
    # F-43 precedent), the bidi override renders as its visible escape
    assert "\\u202d" in note
    body = env.prs[-1].body
    assert "\x1b" not in body and "‭" not in body
    assert "\\u202d" in body

    # (9) R1-F10: a `valid` ruling on the LAST attempt never gets its
    # round — the arbiter is skipped there and the round escalates as
    # before (no wasted session, no hidden rulings).
    env = Env72(tmp_path / "valid-last-attempt",
                tasks=[_task(budget={"size": "S", "max_attempts": 2,
                                     "max_turns": 20, "wall_clock_min": 30})])
    seats = Seats(impl_files=[{}], reviews=(failing, failing),
                  arbiter={"ruling": "valid", "reason": "real defect"})
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "review_not_converging"
    assert seats.seats().count("arbiter") == 0  # skipped on the last attempt

    # (7) seat_providers.arbiter resolves; the default is the reviewer's
    # provider and model.
    base = {
        "name": "fixture2",
        "repo": {"path": "/tmp/x", "default_branch": "main", "remote": "origin",
                 "merge_method": "squash"},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
        "provider": {"kind": "anthropic",
                     "models": {"implementer": "m-i", "reviewer": "m-r",
                                "verifier": "m-v", "importer": "m-m"}},
    }
    assert resolve_provider(base, "arbiter").model == "m-r"  # reviewer default
    assert resolve_provider(base, "arbiter").kind == "anthropic"
    override = {**base, "providers_allowed": ["bedrock"],
                "seat_providers": {"arbiter": {"kind": "bedrock",
                                               "model": "arn:x", "region": "us-east-1"}}}
    spec = resolve_provider(override, "arbiter")
    assert (spec.kind, spec.model) == ("bedrock", "arn:x")
    # The override is loadable profile content (the schema admits the seat).
    import yaml

    profile_path = tmp_path / "project.yaml"
    profile_path.write_text(yaml.safe_dump(override))
    loaded = load_profile(profile_path)
    assert resolve_provider(loaded, "arbiter").kind == "bedrock"


# --------------------------------------------------------------------------
# AC-3 — an escalation after a session keeps the work as a draft PR
# --------------------------------------------------------------------------


def _dependency_task(task_id: str = "T-1", **overrides) -> dict:
    return _task(task_id, scope={"paths": ["impl.py", "tests/test_feature.py",
                                            "pyproject.toml"]}, **overrides)


def test_ac_3(tmp_path: Path, monkeypatch):
    # (1) a scripted new_dependency escalation opens ONE draft PR with the
    # reason, links and label; links.pr records it.
    env = Env72(tmp_path / "dep", tasks=[_dependency_task()])
    seats = Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "new_dependency"
    assert len(env.prs) == 1
    pr = env.prs[0]
    assert pr.draft is True
    assert pr.label == ESCALATION_LABEL
    assert pr.title.startswith(ESCALATION_TITLE_PREFIX)
    assert "new_dependency" in pr.body
    assert "CI is suppressed" in pr.body and "recover" in pr.body
    task = env.task()
    assert task["status"] == "needs_human"
    assert task["links"]["pr"] == result.pr
    assert task["links"]["escalation_pr"] is True
    assert task["links"]["branch"] in env.remote_branches()
    attempt = task["attempts"][-1]
    assert attempt["escalation"] == "new_dependency"
    assert attempt.get("pr") == result.pr
    # R2-F1 part 2: the pushed branch is ONE squashed commit on the base
    # whose message carries [skip ci] — no runner sees the content until
    # the owner's accept releases a clean commit.
    branch = task["links"]["branch"]
    assert env.pushed_commit_count(branch) == 1
    head_msg = subprocess.run(
        ["git", "--git-dir", str(env.remote), "log", "-1",
         "--format=%B", f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True).stdout
    assert "[skip ci]" in head_msg, head_msg

    # R2-F1 guard 1: a base workflow that `pull_request_target` triggers
    # is exactly what [skip ci] cannot suppress — the push refuses.
    def _with_workflow(tmp: Path, name: str, workflow: str) -> Env72:
        env = Env72(tmp, tasks=[_dependency_task()])
        wf_dir = env.repo / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / name).write_text(workflow)
        _git(env.repo, "add", "-A")
        _git(env.repo, "commit", "-qm", "add workflow")
        subprocess.run(
            ["git", "-C", str(env.repo), "push", "-q", str(env.remote),
             "main"], check=True)
        env.base_sha = _git(env.repo, "rev-parse", "HEAD")
        return env

    env = _with_workflow(
        tmp_path / "wf-ppt",
        "deploy.yml",
        "name: deploy\non:\n  pull_request_target:\n    branches: [main]\n"
        "jobs:\n  deploy:\n    uses: ./.github/actions/deploy.yml\n")
    result = env.run(Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused"
    assert "pull_request_target" in (env.task()["attempts"][-1].get("note") or "")
    assert env.prs == [] and env.remote_branches() == ["main"]

    # ...and `workflow_run` refuses the same way.
    env = _with_workflow(
        tmp_path / "wf-run",
        "after.yml",
        "name: after\non: [workflow_run]\njobs:\n  x:\n"
        "    runs-on: ubuntu-latest\n    steps: [{run: echo hi}]\n")
    result = env.run(Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused"
    assert "workflow_run" in (env.task()["attempts"][-1].get("note") or "")

    # ...while a plain push + pull_request workflow pushes (suppressed
    # by [skip ci] on the push, and draft PRs from the factory carry the
    # marker too) — the fixture's own arm (1) above, restated with an
    # explicit plain workflow so the scan has something to clear.
    env = _with_workflow(
        tmp_path / "wf-plain",
        "ci.yml",
        "name: ci\non:\n  push:\n  pull_request:\njobs:\n  test:\n"
        "    runs-on: ubuntu-latest\n    steps: [{run: echo hi}]\n")
    result = env.run(Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "new_dependency"
    assert env.prs and env.prs[0].draft

    # The clone is cleaned only AFTER the push: the branch is on the remote
    # and no run dir survives.
    assert not (env.root / "runs" / "fixture").exists() or \
        list((env.root / "runs" / "fixture").iterdir()) == []

    # (1d) R2-1: the base workflow scan (guard 1) cannot see workflows
    # the BRANCH itself adds or edits — and [skip ci] does not suppress
    # pull_request_review on same-repo PRs. So an escalation PR never
    # carries CI/runner config at all: any diff base..head touching a
    # path under .github/ refuses the push. In scope (P5) or not (P6),
    # deleted or renamed — all refuse; a non-.github escalation still
    # pushes.
    EVIL_WF = ("on:\n  pull_request_review:\n    types: [submitted]\n"
               "jobs:\n  x:\n    runs-on: ubuntu-latest\n"
               "      - run: echo pwn\n")
    # P5: the workflow path IN scope — the diff carries it legitimately
    # and still refuses.
    env = Env72(tmp_path / "gh-in-scope",
                tasks=[_task(budget={"size": "S", "max_attempts": 1,
                                     "max_turns": 20, "wall_clock_min": 30},
                             scope={"paths": ["impl.py", "tests/test_feature.py",
                                              ".github/workflows/review.yml"]})])
    result = env.run(Seats(impl_files=[
        {".github/workflows/review.yml": EVIL_WF, "impl.py": IMPL_BAD}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused", result.escalation
    assert env.prs == [] and env.remote_branches() == ["main"]
    assert ".github/workflows/review.yml" in (
        env.task()["attempts"][-1].get("note") or "")
    assert env.task()["attempts"][-1].get("validator_report")  # reports stay

    # P6: the path NOT in scope — the push refuses the same way (the
    # out-of-scope finding rode the round; the escalation is what would
    # have published it).
    env = Env72(tmp_path / "gh-out-of-scope",
                tasks=[_task(budget={"size": "S", "max_attempts": 1,
                                     "max_turns": 20, "wall_clock_min": 30})])
    result = env.run(Seats(impl_files=[
        {".github/workflows/review.yml": EVIL_WF, "impl.py": IMPL_BAD}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused", result.escalation
    assert env.prs == [] and env.remote_branches() == ["main"]

    # a .github/ file DELETED by the branch refuses too (the net diff
    # names it); so does a rename INTO .github/.
    env = Env72(tmp_path / "gh-deleted", tasks=[_dependency_task()])
    (env.repo / ".github" / "workflows").mkdir(parents=True)
    (env.repo / ".github" / "workflows" / "old.yml").write_text(
        "on: [push]\njobs:\n  x:\n    runs-on: ubuntu-latest\n"
        "    steps: [{run: echo hi}]\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-qm", "add base workflow")
    _git(env.repo, "push", "-q", str(env.remote), "main")
    env.base_sha = _git(env.repo, "rev-parse", "HEAD")
    result = env.run(Seats(impl_files=[
        {".github/workflows/old.yml": None, "pyproject.toml": PYPROJECT_WITH_DEP}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused", result.escalation
    assert ".github/workflows/old.yml" in (
        env.task()["attempts"][-1].get("note") or "")
    assert env.prs == [] and env.remote_branches() == ["main"]

    env = Env72(tmp_path / "gh-renamed",
                tasks=[_task(scope={"paths": ["impl.py", "tests/test_feature.py",
                                              "keep.yml", ".github/**"]})])
    (env.repo / "keep.yml").write_text(
        "on: [push]\njobs:\n  x:\n    runs-on: ubuntu-latest\n"
        "    steps: [{run: echo hi}]\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-qm", "add keep.yml")
    _git(env.repo, "push", "-q", str(env.remote), "main")
    env.base_sha = _git(env.repo, "rev-parse", "HEAD")

    class RenameIntoGh(Seats):
        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            if seat == "implementer":
                (Path(run_dir) / ".github").mkdir(exist_ok=True)
                (Path(run_dir) / "keep.yml").rename(
                    Path(run_dir) / ".github" / "keep.yml")
            return super().__call__(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(RenameIntoGh(
        impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused", result.escalation
    assert env.prs == [] and env.remote_branches() == ["main"]

    # a non-.github escalation still pushes (arm 1 proved the happy
    # shape; restated beside the refusals so the rule's boundary is in
    # one place).
    env = Env72(tmp_path / "gh-clean", tasks=[_dependency_task()])
    result = env.run(Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]))
    assert result.outcome == "needs_human", result
    assert result.escalation == "new_dependency", result.escalation
    assert env.prs and env.prs[0].draft

    # (2) credential_added pushes nothing (AC-1 arm 6 proved the run side;
    # here: the same env shape, explicitly no PR and no branch).
    env = Env72(tmp_path / "cred", tasks=[_dependency_task()])
    seats = Seats(impl_files=[{"creds.py": CREDENTIAL_LINE + "\n"}])
    result = env.run(seats)
    assert result.escalation == "credential_added"
    assert env.prs == [] and env.remote_branches() == ["main"]

    # (3) a secret planted in an EARLIER round's commit (then removed)
    # refuses the push — the scan is over the branch's whole history.
    # Round 1 carries both the manifest (the new_dependency escalation)
    # and the secret; round 2 removes the file, so the net diff is clean
    # while the r1 commit still holds the secret: the escalation PR's
    # push fence refuses, and the refusal is recorded as publish_refused
    # with the original escalation in the note.
    import factory.orchestrator as orch

    monkeypatch.setattr(orch, "run_secrets", lambda profile: (SEAT_TOKEN,))
    env = Env72(tmp_path / "history-secret", tasks=[_dependency_task()])
    seats = Seats(impl_files=[
        {"notes.txt": f"build ref {SEAT_TOKEN}\n",
         "pyproject.toml": PYPROJECT_WITH_DEP},
        {"notes.txt": None, "pyproject.toml": None},
    ])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused"
    attempt = env.task()["attempts"][-1]
    assert "new_dependency" in (attempt.get("note") or "")
    assert "refusing to push" in (attempt.get("note") or "")
    assert attempt.get("validator_report")  # reports stay on disk
    assert env.prs == []
    assert env.remote_branches() == ["main"]
    monkeypatch.undo()

    # (4) a stubbed push failure records pr_create_failed and keeps the
    # reports (the original escalation in the note).
    env = Env72(tmp_path / "push-fail", tasks=[_dependency_task()])
    seats = Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}])

    def broken_push(*args, **kwargs):
        raise PublishError("git push of refs/heads/factory/x failed: boom")

    monkeypatch.setattr(orch, "push_branch", broken_push)
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "pr_create_failed"
    attempt = env.task()["attempts"][-1]
    assert "new_dependency" in (attempt.get("note") or "")
    assert attempt.get("validator_report")
    assert env.prs == []
    monkeypatch.undo()

    # (6) R1-F6: a failed gh after a successful push keeps the REASON —
    # the pr_create_failed attempt's note carries the original escalation
    # and the failure, never None.
    env = Env72(tmp_path / "note-kept", tasks=[_dependency_task()])
    seats = Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}])
    env.open_pr_error = PublishError("gh exploded")
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "pr_create_failed"
    attempt = env.task()["attempts"][-1]
    assert attempt.get("note") and "new_dependency" in attempt["note"]
    assert "gh exploded" in attempt["note"]

    # (7) R1-F7: a superseded escalation PR is closed — a re-queued task's
    # next run leaves exactly one open PR per task. Here: the task parks
    # with a draft PR, is re-queued (the owner's reset word, simulated the
    # state-layer way), and its next run opens a NEW draft PR: the old one
    # is closed through the close seam, best-effort and recorded.
    import factory.orchestrator as orch_mod

    closed: list[str] = []
    original_close = orch_mod.gh_pr_close

    def recording_close(url):
        closed.append(url)
        original_close(url)

    monkeypatch.setattr(orch_mod, "gh_pr_close", recording_close)
    env = Env72(tmp_path / "superseded",
                tasks=[_task(budget={"size": "S", "max_attempts": 1,
                                     "max_turns": 20, "wall_clock_min": 30})])
    seats = Seats(impl_files=[{"impl.py": IMPL_BAD}])
    first = env.run(seats)
    assert first.escalation == "attempts_exhausted", first.escalation
    old_url = first.pr
    assert old_url
    # the owner grants a fresh budget and the task runs again
    TaskQueue(env.queue_path).recover_attempts(
        "T-1", evidence="re-queue for the supersession arm", done_by="owner")
    ledger_approve(env.root, env.queue_path)
    second = env.run(Seats(impl_files=[{"impl.py": IMPL_BAD}]))
    assert second.escalation == "attempts_exhausted"
    assert second.pr and second.pr != old_url
    assert closed == [old_url], closed
    assert env.task()["links"]["pr"] == second.pr  # the new one stands
    # ...and a third run that PASSES closes the second draft too: the
    # validated PR supersedes it, and the task's links lose the
    # escalation marker (a validated PR is re-validated and reported
    # ready, never skipped as an escalation PR).
    TaskQueue(env.queue_path).recover_attempts(
        "T-1", evidence="re-queue for the publish supersession", done_by="owner")
    ledger_approve(env.root, env.queue_path)
    third = env.run(Seats())
    assert third.outcome == "pr_opened", third
    assert closed == [old_url, second.pr], closed
    links = env.task()["links"]
    assert links["pr"] == third.pr and "escalation_pr" not in links
    monkeypatch.undo()

    # (5) the sweep skips the escalation PR for re-validation, and
    # `factory run` never reports it ready — including the crash-window
    # shape of an ESCALATION-marked task that reached in_review with the
    # marker still set: no gh lookup, no re-validation, no ready line.
    env = Env72(tmp_path / "sweep", tasks=[_dependency_task()])
    seats = Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}])
    first = env.run(seats)
    assert first.escalation == "new_dependency"
    pr_url = first.pr
    asked: list[str] = []

    def recording_pr_state(url: str) -> str:
        asked.append(url)
        return "open"

    # The crash-window shape: the task's links hold the escalation PR and
    # the marker, and the status is in_review (a crash between the
    # links save and the accept, or any hand-minted equivalent).
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-1")
    task["status"] = "in_review"
    queue.save()
    second = env.run(Seats(), pr_state=recording_pr_state)
    assert second.outcome == "idle", second
    assert all(pr_url not in line for line in (second.ready_prs or []))
    assert pr_url not in asked, "the run resolved an escalation PR's state"
    reports = list((env.root / "reports" / "fixture").glob("*revalidate*"))
    assert reports == [], "the sweep re-validated an escalation PR"


# --------------------------------------------------------------------------
# AC-4 — the owner answers an escalation in one command
# --------------------------------------------------------------------------


def _parked_dependency_env(tmp_path: Path) -> tuple[Env72, str]:
    """A needs_human task holding an escalation draft PR on the remote."""
    env = Env72(tmp_path, tasks=[_dependency_task()])
    seats = Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}])
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    branch = _pushed_branch(env)
    head = env.remote_branch_sha(branch)
    return env, head


def test_ac_4(tmp_path: Path):
    # (1) accepted new_dependency on an unchanged head → in_review, the
    # ledger entry written, the PR marked ready. R2-F1 guard 2: the
    # accept RELEASES the work — one new commit, same parent, SAME TREE
    # (asserted), message without [skip ci], force-pushed with a lease
    # on the accepted SHA; the released SHA rides the waiver so the F5
    # sweep still matches it.
    env, head = _parked_dependency_env(tmp_path / "accept")
    gh = GhSeams([head])
    queue = TaskQueue(env.queue_path)
    accepted_tree = subprocess.run(
        ["git", "--git-dir", str(env.remote), "rev-parse",
         f"{head}^{{tree}}"],
        capture_output=True, text=True, check=True).stdout.strip()
    status = recover_task(
        queue, "T-1", accept=True,
        evidence="read the diff; requests is the documented client",
        factory_root=env.root, pr_head=gh.pr_head, pr_ready=gh.pr_ready,
        session_runner=Seats(),
        confirm=lambda q: True, echo=lambda text: None,
    )
    assert status == "in_review"
    task = env.task()
    assert task["status"] == "in_review"
    assert gh.ready_calls == [task["links"]["pr"]]
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    assert ledger.has_escalation_waiver("T-1", "new_dependency", head)
    # The waiver is keyed to the exact SHA and class (below).
    branch = task["links"]["branch"]
    released = env.remote_branch_sha(branch)
    assert released != head  # a new commit was released
    released_msg = subprocess.run(
        ["git", "--git-dir", str(env.remote), "log", "-1",
         "--format=%B", f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True).stdout
    assert "[skip ci]" not in released_msg, released_msg
    assert "released" in released_msg.lower()
    released_tree = subprocess.run(
        ["git", "--git-dir", str(env.remote), "rev-parse",
         f"{released}^{{tree}}"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert released_tree == accepted_tree  # the proven tree, unchanged
    released_parent = subprocess.run(
        ["git", "--git-dir", str(env.remote), "rev-parse",
         f"{released}^"],
        capture_output=True, text=True, check=True).stdout.strip()
    accepted_parent = subprocess.run(
        ["git", "--git-dir", str(env.remote), "rev-parse",
         f"{head}^"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert released_parent == accepted_parent  # one commit on the same base
    # the sweep's waiver match accepts the released SHA
    assert ledger.has_escalation_waiver("T-1", "new_dependency", released)

    # (1b) R2-F1 guard 2's lease: the remote moving between the accept's
    # re-run and the release refuses — the release push carries a lease
    # on the accepted SHA, so a concurrent push can never be overwritten.
    env, head = _parked_dependency_env(tmp_path / "lease")
    branch = _pushed_branch(env)
    gh = GhSeams([head])
    outer_env = env
    moved = {"done": False}

    def moving_runner(seat, prompt, run_dir, profile, **kwargs):
        record = Seats()(seat, prompt, run_dir, profile, **kwargs)
        if not moved["done"] and seat == "reviewer":
            moved["done"] = True
            # a concurrent push lands on the PR branch mid-accept
            c = tmp_path / "lease" / "racer"
            subprocess.run(
                ["git", "clone", "-q", str(outer_env.remote), str(c)],
                check=True)
            subprocess.run(
                ["git", "-C", str(c), "checkout", "-q", branch], check=True)
            (c / "impl.py").write_text("def answer():\n    return 45\n")
            subprocess.run(
                ["git", "-C", str(c), "-c", "user.name=Fixture",
                 "-c", "user.email=fixture@example.com",
                 "commit", "-qam", "race"], check=True)
            subprocess.run(
                ["git", "-C", str(c), "push", "-q", "origin", branch],
                check=True)
        return record

    with pytest.raises(ApprovalError, match="lease|moved|stale"):
        recover_task(
            TaskQueue(env.queue_path), "T-1", accept=True,
            evidence="checked", factory_root=env.root,
            pr_head=gh.pr_head, pr_ready=gh.pr_ready,
            session_runner=moving_runner,
            confirm=lambda q: True, echo=lambda text: None,
        )
    assert env.task()["status"] == "needs_human"  # refused, PR stays draft
    assert gh.ready_calls == []
    assert env.remote_branch_sha(branch) != head  # the racer's commit stands

    # (2) the same accept after a new commit → refused: the head moved.
    # (2a) a force-push between the y and the re-run voids the waiver
    # (the head is re-checked before the PR is marked ready).
    env, head = _parked_dependency_env(tmp_path / "moved")
    branch = _pushed_branch(env)
    clone = tmp_path / "moved" / "clone"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", branch], check=True)
    (clone / "impl.py").write_text("def answer():\n    return 43\n")
    subprocess.run(["git", "-C", str(clone), "-c", "user.name=Fixture",
                   "-c", "user.email=fixture@example.com",
                   "commit", "-qam", "move head"],
                  check=True)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", branch],
                   check=True)
    moved_head = env.remote_branch_sha(branch)
    gh = GhSeams([head, moved_head])  # read at start, re-check before ready
    with pytest.raises(ApprovalError, match="void|new commit|moved"):
        recover_task(
            TaskQueue(env.queue_path), "T-1", accept=True,
            evidence="checked", factory_root=env.root,
            pr_head=gh.pr_head, pr_ready=gh.pr_ready,
            session_runner=Seats(),
            confirm=lambda q: True, echo=lambda text: None,
        )
    assert env.task()["status"] == "needs_human"  # refused, PR stays draft
    assert gh.ready_calls == []
    # (2b) after a COMPLETED accept the task is in_review: a second accept
    # on the moved branch refuses (no longer an escalated task).
    env, head = _parked_dependency_env(tmp_path / "moved-after")
    gh = GhSeams([head])
    recover_task(
        TaskQueue(env.queue_path), "T-1", accept=True,
        evidence="checked", factory_root=env.root,
        pr_head=gh.pr_head, pr_ready=gh.pr_ready,
        session_runner=Seats(),
        confirm=lambda q: True, echo=lambda text: None,
    )
    assert env.task()["status"] == "in_review"
    with pytest.raises(ApprovalError):
        recover_task(
            TaskQueue(env.queue_path), "T-1", accept=True,
            evidence="checked", factory_root=env.root,
            pr_head=gh.pr_head, pr_ready=gh.pr_ready,
            session_runner=Seats(),
            confirm=lambda q: True, echo=lambda text: None,
        )

    # (3) a second escalation class on that SHA → still escalates (the
    # waiver covers exactly one class). The run added the gained risk
    # flag to the task (today's behaviour), which defuses
    # risk_path_touched at a plain re-run — so the honest second-class
    # shape is a queue edited after the escalation: the flag removed
    # again, the re-run's L0 finds risk_path_touched standing beside the
    # waived new_dependency and refuses.
    env = Env72(tmp_path / "two-classes",
                tasks=[_task(scope={"paths": ["impl.py", "tests/test_feature.py",
                                              "pyproject.toml"]})],
                profile_extra={"paths": {
                    "risk": {"external_service": ["impl.py"]}}})
    seats = Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}])
    first = env.run(seats)
    assert first.escalation == "new_dependency"  # L0 order: dep before risk
    task = env.task()
    assert "external_service" in task["risk"]  # the run recorded the flag
    queue = TaskQueue(env.queue_path)
    queue.get("T-1")["risk"] = []  # the owner's post-escalation queue edit
    queue.save()
    ledger_approve(env.root, env.queue_path)  # the edited contract's approval
    branch = _pushed_branch(env, "T-1")
    head = env.remote_branch_sha(branch)
    gh = GhSeams([head])
    with pytest.raises(ApprovalError, match="risk_path_touched"):
        recover_task(
            TaskQueue(env.queue_path), "T-1", accept=True,
            evidence="checked", factory_root=env.root,
            pr_head=gh.pr_head, pr_ready=gh.pr_ready,
            session_runner=Seats(),
            confirm=lambda q: True, echo=lambda text: None,
        )
    assert env.task()["status"] == "needs_human"
    assert gh.ready_calls == []

    # (4) --accept on protected_path_touched → refused, the supported
    # reasons named.
    env = Env72(tmp_path / "wrong-class")
    task = _task()
    task["status"] = "needs_human"
    task["attempts"] = [{
        "run_id": "20261112T000000Z-aaaaaa", "slice_id": "T-1",
        "started": "2026-11-12T00:00:00Z", "ended": "2026-11-12T00:00:00Z",
        "outcome": "escalated", "rounds": 1, "turns": 0,
        "input_tokens": 0, "output_tokens": 0,
        "prompt_versions": {"implementer": "x"},
        "skills": [], "escalation": "protected_path_touched",
        "pr": "https://github.com/fixture/fixture/pull/1",
    }]
    task["links"] = {"pr": "https://github.com/fixture/fixture/pull/1",
                     "branch": "factory/t-1-x", "escalation_pr": True}
    env.queue_path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [task]}, indent=2) + "\n")
    with pytest.raises(ApprovalError, match="new_dependency.*risk_path_touched"):
        recover_task(TaskQueue(env.queue_path), "T-1", accept=True,
                     evidence="x", factory_root=env.root,
                     confirm=lambda q: True, echo=lambda text: None)

    # (5) --reject → PR closed, task draft, the old approval no longer
    # authorises a changed contract.
    env, head = _parked_dependency_env(tmp_path / "reject")
    gh = GhSeams([head])
    status = recover_task(
        TaskQueue(env.queue_path), "T-1", reject=True,
        factory_root=env.root, pr_close=gh.pr_close,
        confirm=lambda q: True, echo=lambda text: None,
    )
    assert status == "draft"
    task = env.task()
    assert task["status"] == "draft"
    assert gh.close_calls == [task["links"]["pr"]]
    assert "acceptance_hash" not in task  # approval markers cleared
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    # Prove F-63's hash check: a contract changed after the reject has no
    # ledger entry, so the old approval authorises nothing.
    changed = json.loads(json.dumps(task))
    changed["acceptance"] = [{"id": "AC-1", "text": "answer() returns 43",
                              "check": {"type": "test", "ref": "tests/test_feature.py::test_ac_1"}}]
    assert not ledger.has_contract("T-1", contract_hash(changed))

    # (6) --yes refused for both.
    env, head = _parked_dependency_env(tmp_path / "no-yes")
    with pytest.raises(ApprovalError, match="--yes"):
        recover_task(TaskQueue(env.queue_path), "T-1", accept=True,
                     evidence="x", assume_yes=True, factory_root=env.root,
                     confirm=lambda q: True, echo=lambda text: None)
    with pytest.raises(ApprovalError, match="--yes"):
        recover_task(TaskQueue(env.queue_path), "T-1", reject=True,
                     assume_yes=True, factory_root=env.root,
                     confirm=lambda q: True, echo=lambda text: None)

    # (8) R1-F5: an accepted PR survives the default branch moving — the
    # sweep's re-validation consults the waiver (task, class, PR head
    # SHA, contract), so the accepted class does not bounce the task to
    # stale_base on the first unrelated merge. A NEW commit on the PR
    # branch still voids it.
    env, head = _parked_dependency_env(tmp_path / "sweep-waiver")
    gh = GhSeams([head])
    recover_task(
        TaskQueue(env.queue_path), "T-1", accept=True,
        evidence="read the diff; requests is the documented client",
        factory_root=env.root, pr_head=gh.pr_head, pr_ready=gh.pr_ready,
        session_runner=Seats(),
        confirm=lambda q: True, echo=lambda text: None,
    )
    assert env.task()["status"] == "in_review"
    # advance main on the remote (an unrelated merge)
    clone = tmp_path / "sweep-waiver" / "advance"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(clone)],
                   check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"],
                   check=True)
    (clone / "unrelated.txt").write_text("moved\n")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "-c", "user.name=Fixture",
                   "-c", "user.email=fixture@example.com",
                   "commit", "-qm", "advance main"],
                  check=True)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"],
                   check=True)
    ledger_approve(env.root, env.queue_path)
    sweep = _run_once(env)
    task = env.task()
    assert task["status"] == "in_review", (
        f"the accepted PR bounced: {task['status']} "
        f"({task['attempts'][-1].get('escalation')})")
    assert any("pull" in line for line in (sweep.ready_prs or []))
    # ...and a new commit on the PR branch voids the waiver: the sweep
    # re-validation then escalates stale_base as it does for every PR.
    branch = _pushed_branch(env)
    pr_clone = tmp_path / "sweep-waiver" / "pr-clone"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(pr_clone)],
                   check=True)
    subprocess.run(["git", "-C", str(pr_clone), "checkout", "-q", branch],
                   check=True)
    (pr_clone / "impl.py").write_text("def answer():\n    return 44\n")
    subprocess.run(["git", "-C", str(pr_clone), "-c", "user.name=Fixture",
                   "-c", "user.email=fixture@example.com",
                   "commit", "-qam", "move pr"],
                  check=True)  # impl.py is tracked: -am stages it
    subprocess.run(["git", "-C", str(pr_clone), "push", "-q", "origin",
                    branch], check=True)
    ledger_approve(env.root, env.queue_path)
    _run_once(env)
    moved = env.task()
    assert moved["status"] == "needs_human", moved["status"]
    assert moved["attempts"][-1].get("escalation") == "stale_base"

    # (9) R1-F8: --reject writes the state FIRST — a close failure leaves
    # a draft task with an open PR (visible, closable by hand), never a
    # needs_human task holding a PR gh already closed.
    env, head = _parked_dependency_env(tmp_path / "reject-order")
    gh = GhSeams([head])

    def failing_close(url):
        raise PublishError("gh pr close failed")

    with pytest.raises(ApprovalError, match="gh pr close failed"):
        recover_task(
            TaskQueue(env.queue_path), "T-1", reject=True,
            factory_root=env.root, pr_close=failing_close,
            confirm=lambda q: True, echo=lambda text: None,
        )
    assert env.task()["status"] == "draft"  # the state moved before the close

    # (10) R1-F9: a failed accept re-run STORES its report — the error
    # names the path, and the file exists with the re-run's findings.
    env, head = _parked_dependency_env(tmp_path / "accept-report")
    gh = GhSeams([head])
    failing_review = Seats(reviews=(_verdict(_finding(severity="blocking"),),))
    with pytest.raises(ApprovalError) as exc_info:
        recover_task(
            TaskQueue(env.queue_path), "T-1", accept=True,
            evidence="checked", factory_root=env.root,
            pr_head=gh.pr_head, pr_ready=gh.pr_ready,
            session_runner=failing_review,
            confirm=lambda q: True, echo=lambda text: None,
        )
    path_match = re.search(r"accept round report: (\S+)", str(exc_info.value))
    assert path_match, str(exc_info.value)
    stored_path = Path(path_match.group(1))
    assert stored_path.is_file()
    stored = json.loads(stored_path.read_text())
    assert stored.get("L2", {}).get("verdict") == "fail"

    # (7) the accept re-run does not skip review: a failing re-review
    # keeps the task needs_human and the PR draft.
    env, head = _parked_dependency_env(tmp_path / "failing-rereview")
    gh = GhSeams([head])
    failing_review = Seats(reviews=(_verdict(_finding(severity="blocking"),),))
    with pytest.raises(ApprovalError):
        recover_task(
            TaskQueue(env.queue_path), "T-1", accept=True,
            evidence="checked", factory_root=env.root,
            pr_head=gh.pr_head, pr_ready=gh.pr_ready,
            session_runner=failing_review,
            confirm=lambda q: True, echo=lambda text: None,
        )
    assert env.task()["status"] == "needs_human"
    assert gh.ready_calls == []


# --------------------------------------------------------------------------
# AC-5 — the loop parks an escalation and carries on
# --------------------------------------------------------------------------


class _ByTask:
    """Route scripted seats by task: the implementer prompt names the task
    (``# Task <id>:``); the review-family seats of a run belong to the
    task whose implementer ran last (runs are sequential)."""

    def __init__(self, by_task: dict):
        self.by_task = by_task
        self.current = "?"
        self.calls: list[tuple[str, str]] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            match = re.search(r"^# Task ([\w.-]+):", prompt, re.M)
            assert match, f"no task header in the implementer prompt:\n{prompt[:400]}"
            self.current = match.group(1)
        self.calls.append((self.current, seat))
        return self.by_task[self.current](seat, prompt, run_dir, profile, **kwargs)


def test_ac_5(tmp_path: Path, monkeypatch, capsys):
    # (1) three tasks, the first escalates → the second and third run,
    # the summary prints the first, exit 1. (concurrency 2: T-2's open PR
    # must not keep T-3 from running — the escalation PR holds no slot.)
    env = Env72(tmp_path / "loop", tasks=[
        _dependency_task("T-1"),
        _task("T-2"),
        _task("T-3"),
    ], profile_extra={"repo": {"concurrency": 2}})
    runner = _ByTask({
        "T-1": Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]),
        "T-2": Seats(),
        "T-3": Seats(),
    })
    ledger_approve(env.root, env.queue_path)
    from _support.f72 import OFF_PEAK

    from factory.mergeorder import Protection

    loop = run_loop(
        env.queue_path, factory_root=env.root, session_runner=runner,
        open_pr=env.open_pr, now=lambda: OFF_PEAK,
        protection=lambda r, b: Protection(checked=True, strict=True),
        pr_state=lambda url: "open",
    )
    assert loop.stop_reason == "queue_empty"
    assert [r.outcome for r in loop.results] == [
        "needs_human", "pr_opened", "pr_opened", "idle"]
    assert [t for t, _ in runner.calls if _ == "implementer"] == ["T-1", "T-2", "T-3"]
    assert len(loop.parked) == 1
    parked = loop.parked[0]
    assert parked.task_id == "T-1"
    assert parked.escalation == "new_dependency"
    assert parked.pr and "pull" in parked.pr  # the escalation PR URL

    # The CLI maps a parked loop to exit 1 and prints one line per parked
    # task (id, reason, PR URL or "no PR").
    import factory.cli as cli

    monkeypatch.setattr(
        cli, "run_loop",
        lambda *a, **kw: LoopResult(loop.stop_reason, loop.results,
                                    parked=loop.parked))
    code = cli.main(["loop", "--queue", str(env.queue_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "parked: T-1" in out and "new_dependency" in out and parked.pr in out
    # ...and an unparked queue_empty loop stays exit 0.
    monkeypatch.setattr(
        cli, "run_loop",
        lambda *a, **kw: LoopResult("queue_empty", loop.results[-1:], parked=[]))
    assert cli.main(["loop", "--queue", str(env.queue_path)]) == 0

    # (2) an orchestrator error still stops the loop. T-1 parks (its
    # escalation PR holds no slot), T-2's round crashes a gate — the
    # error outcome ends the loop, never drains the queue.
    env = Env72(tmp_path / "error",
                tasks=[_dependency_task("T-1"), _task("T-2")])
    runner = _ByTask({
        "T-1": Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]),
        "T-2": Seats(),
    })
    ledger_approve(env.root, env.queue_path)
    import factory.orchestrator as orch
    from factory.runs import RunError

    real_l1 = orch.run_l1
    state = {"calls": 0}

    def l1_once(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] > 1:  # never reached: the loop stops on the first
            return real_l1(*args, **kwargs)
        raise RunError("gate crash (scripted)")

    monkeypatch.setattr(orch, "run_l1", l1_once)
    loop = run_loop(
        env.queue_path, factory_root=env.root, session_runner=runner,
        open_pr=env.open_pr, now=lambda: OFF_PEAK,
        protection=lambda r, b: Protection(checked=True, strict=True),
        pr_state=lambda url: "open",
    )
    assert loop.stop_reason == "error"
    monkeypatch.undo()

    # (3) an open escalation PR does not block the next pick (it does not
    # hold the writer slot, and the sweep never re-validates it).
    env = Env72(tmp_path / "slot", tasks=[_dependency_task("T-1"), _task("T-2")])
    by_task = _ByTask({
        "T-1": Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]),
        "T-2": Seats(),
    })
    first = env.run(by_task)
    assert first.outcome == "needs_human" and first.task_id == "T-1"
    second = env.run(_ByTask({
        "T-1": Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}]),
        "T-2": Seats(),
    }))
    assert second.outcome == "pr_opened", second
    assert second.task_id == "T-2"

    # (4) every task escalating: the loop ends (queue_empty), parking all
    # three — never spins.
    env = Env72(tmp_path / "all", tasks=[
        _dependency_task("T-1"), _dependency_task("T-2"),
        _dependency_task("T-3"),
    ])
    runner = _ByTask({
        f"T-{n}": Seats(impl_files=[{"pyproject.toml": PYPROJECT_WITH_DEP}])
        for n in (1, 2, 3)
    })
    ledger_approve(env.root, env.queue_path)
    loop = run_loop(
        env.queue_path, factory_root=env.root, session_runner=runner,
        open_pr=env.open_pr, now=lambda: OFF_PEAK,
        protection=lambda r, b: Protection(checked=True, strict=True),
        pr_state=lambda url: "open",
    )
    assert loop.stop_reason == "queue_empty"
    assert len(loop.parked) == 3
    assert all(p.escalation == "new_dependency" for p in loop.parked)

    # (5) an escalated task is never re-picked in the same loop: covered
    # by (1) — T-1 appears exactly once among the implementer calls.
