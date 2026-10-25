"""F-11 acceptance tests — BUILD-ORDER.md, work order F-11 (factory status).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f11.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the queue document (validated by ``load_tasks`` against the
  real schema), the metrics math over ``attempts``, the markdown page,
  the F-27 progress log and ``weeks_vents`` seam, and a real git repo
  for the merge/revert history.
* **Stubbed:** nothing. The clock is pinned with ``now=``; the pure
  metric tests pass a hand-built commit list instead of a repo.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from factory.contract import load_tasks
from factory.progress import ProgressLog
from factory.status import (
    StatusError,
    compute_metrics,
    git_commits,
    render_status,
)
from factory.vent import VENT_CATEGORIES, flush_vents

# A fixed clock: a Sunday afternoon, inside every vent's window below.
NOW = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
RUN_ID = "20260101T000000Z-ab12cd"

PR_A = "https://github.com/fixture/repo/pull/7"
PR_C = "https://github.com/fixture/repo/pull/9"

# Hand-built merge history for the pure metric math: T-A's merge commit is
# later reverted (a revert body names it), T-C's is not.
MERGE_A_SHA = "a" * 40
MERGE_C_SHA = "c" * 40
COMMIT_FIXTURE = [
    {
        "sha": "d" * 40,
        "subject": "unrelated later work",
        "body": "",
        "at": "2026-10-08T09:00:00Z",
    },
    {
        "sha": "0" * 40,
        "subject": 'Revert "Task T-A (#7)"',
        "body": f"This reverts commit {MERGE_A_SHA}.",
        "at": "2026-10-03T09:00:00Z",
    },
    {"sha": MERGE_C_SHA, "subject": "Task T-C (#9)", "body": "",
     "at": "2026-10-07T12:00:00Z"},
    {"sha": MERGE_A_SHA, "subject": "Task T-A (#7)", "body": "",
     "at": "2026-10-02T09:00:00Z"},
]


# ------------------------------------------------------------- fixture --


def _attempt(
    run_id: str,
    outcome: str = "pass",
    *,
    rounds: int = 0,
    in_tok: int = 0,
    out_tok: int = 0,
    escalation: str | None = None,
    note: str | None = None,
) -> dict:
    """One attempt record, the shape the schema's ``attempt`` requires."""
    record = {
        "run_id": run_id,
        "slice_id": "",
        "started": "2026-10-16T09:00:00Z",
        "ended": "2026-10-16T10:00:00Z",
        "outcome": outcome,
        "rounds": rounds,
        "turns": 10,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "prompt_versions": {"implementer": "deadbeef"},
        "skills": [],
    }
    if escalation:
        record["escalation"] = escalation
    if note:
        record["note"] = note
    return record


def _task(
    tid: str,
    status: str,
    *,
    attempts: list[dict] | None = None,
    approved_at: str = "2026-10-01T09:00:00Z",
    **extra,
) -> dict:
    """One task with every field the queue schema requires."""
    task = {
        "id": tid,
        "title": f"Task {tid}",
        "source": f"BUILD-ORDER.md#{tid.lower()}",
        "kind": "build",
        "priority": "P1",
        "status": status,
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [
            {
                "id": "AC-1",
                "text": "it works",
                "check": {"type": "test", "ref": "tests/test_x.py::test_ac_1"},
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
        "acceptance_hash": tid.lower() * 64,
        "approved_by": "owner",
        "approved_at": approved_at,
        "attempts": attempts or [],
    }
    task.update(extra)
    return task


def _doc() -> dict:
    """The fixture queue, with the numbers test_ac_2 hand-computes from."""
    return {
        "version": 1,
        "project": "fixture",
        "updated_at": "2026-10-18T15:00:00Z",
        "tasks": [
            # merged: 2 attempts, 3 review rounds, 1000 tokens
            _task(
                "T-A", "done",
                attempts=[
                    _attempt("r1", rounds=1, in_tok=100, out_tok=200),
                    _attempt("r2", rounds=2, in_tok=300, out_tok=400),
                ],
                links={"pr": PR_A, "merged": PR_A},
            ),
            # needs you: escalated on its last attempt
            _task(
                "T-B", "needs_human",
                attempts=[
                    _attempt("r3", "fail", rounds=3, in_tok=1000, out_tok=1000),
                    _attempt("r4", "escalated", rounds=2, in_tok=2000, out_tok=500,
                             escalation="attempts_exhausted",
                             note="3 attempts failed L0"),
                ],
                approved_at="2026-10-02T09:00:00Z",
            ),
            # merged (stamped terminal in in_review, the F-26 pattern):
            # 1 attempt, 1000 tokens, approved -> merge = 2.5 days
            _task(
                "T-C", "in_review",
                attempts=[_attempt("r5", rounds=1, in_tok=500, out_tok=500)],
                approved_at="2026-10-05T00:00:00Z",
                links={"pr": PR_C, "merged": PR_C},
            ),
            # needs you for a different reason
            _task(
                "T-D", "needs_human",
                attempts=[
                    _attempt("r6", "escalated", rounds=1, in_tok=10, out_tok=10,
                             escalation="credential_added",
                             note="key-shaped string added"),
                ],
                approved_at="2026-10-06T09:00:00Z",
            ),
            # awaiting owner: one step ticked, one open
            _task(
                "T-E", "awaiting_owner",
                attempts=[_attempt("r7", rounds=2, in_tok=40, out_tok=60)],
                approved_at="2026-10-03T09:00:00Z",
                owner_steps=[
                    {"text": "step one", "done": True,
                     "done_at": "2026-10-04T09:00:00Z", "done_by": "owner",
                     "evidence": "checked"},
                    {"text": "step two", "done": False},
                ],
            ),
            # ready, never attempted
            _task("T-F", "ready", approved_at="2026-10-07T09:00:00Z"),
        ],
    }


def _write_queue(tmp_path: Path) -> dict:
    """The fixture queue on disk, validated by the real loader."""
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(_doc(), indent=2))
    return load_tasks(path)


def _vent(**overrides) -> dict:
    """One vent record, the shape the collector stamps per call (F-27)."""
    record = {
        "run_id": RUN_ID,
        "task_id": "T-B",
        "seat": "implementer",
        "turn": 3,
        "category": "missing_tool",
        "text": "no ripgrep in the sandbox PATH",
    }
    record.update(overrides)
    return record


def _git(repo: Path, *args: str, at: str | None = None) -> str:
    """Run git in the fixture repo; ``at`` pins the commit dates."""
    env = dict(os.environ)
    if at:
        env["GIT_AUTHOR_DATE"] = at
        env["GIT_COMMITTER_DATE"] = at
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=env,
    )
    return proc.stdout.strip()


def _history_repo(tmp_path: Path) -> Path:
    """A real repo whose history merges PRs 7 and 9 and reverts PR 7."""
    repo = tmp_path / "history"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@factory.local")
    _git(repo, "config", "user.name", "factory test")
    (repo / "f.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Task T-A (#7)", at="2026-10-02T09:00:00Z")
    sha_a = _git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("c\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Task T-C (#9)", at="2026-10-07T12:00:00Z")
    # What `git revert` of the squash merge produces, verbatim shape: the
    # subject quotes it, the body names the merge commit.
    _git(
        repo, "commit", "--allow-empty",
        "-m", 'Revert "Task T-A (#7)"',
        "-m", f"This reverts commit {sha_a}.",
        at="2026-10-08T09:00:00Z",
    )
    return repo


# ----------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """AC-1: the summary lists every needs_human task with its escalation
    reason."""
    queue = _write_queue(tmp_path)
    page = render_status(queue, project="fixture", factory_root=tmp_path, now=NOW)

    assert "- needs you (2):" in page
    # Each task with the reason its last attempt was escalated for — the
    # escalation name and the note that says why.
    assert "  - T-B — attempts_exhausted: 3 attempts failed L0" in page
    assert "  - T-D — credential_added: key-shaped string added" in page
    # Both of them, and nothing else, under "needs you".
    section = page.split("- needs you (2):", 1)[1].split("\n- ", 1)[0]
    listed = [line.strip() for line in section.splitlines() if line.strip()]
    assert [line.split(" —")[0] for line in listed] == ["- T-B", "- T-D"]


# ----------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path: Path):
    """AC-2: metrics computed from attempts match hand-computed values.

    Hand computation over the fixture queue (see ``_doc``):

    * 7 attempts across 5 attempted tasks; 2 ended ``escalated``
      -> interventions per task = 2/5 = 0.4
    * rounds 1+2+3+2+1+1+2 = 12 over 7 attempts -> 12/7 = 1.71
    * merged T-A (100+200+300+400 = 1000 tokens) and T-C (500+500 = 1000)
      -> 2000/2 = 1000 tokens per merged PR
    * T-A: approved 10-01T09:00, merge commit 10-02T09:00 -> 1.0d;
      T-C: approved 10-05T00:00, merge commit 10-07T12:00 -> 2.5d;
      median(1.0, 2.5) = 1.75d
    * of the 2 merged PRs, T-A's merge commit is named by a revert
      commit's body -> 1 of 2 = 50%
    """
    metrics = compute_metrics(_doc(), commits=COMMIT_FIXTURE)
    assert metrics["interventions_per_task"] == pytest.approx(0.4)
    assert metrics["review_rounds_per_attempt"] == pytest.approx(12 / 7)
    assert metrics["tokens_per_merged_pr"] == 1000
    assert metrics["approval_to_merge_days"] == pytest.approx(1.75)
    assert dict(metrics["approval_to_merge_detail"]) == {"T-A": 1.0, "T-C": 2.5}
    assert metrics["revert_share"] == pytest.approx(0.5)
    assert (metrics["reverted"], metrics["merged"]) == (1, 2)

    # The page reports the same numbers, dated from a real git history.
    page = render_status(
        _doc(), project="fixture", factory_root=tmp_path, now=NOW,
        repo=_history_repo(tmp_path),
    )
    assert ("- interventions per task: 0.40 "
            "(2 escalated attempts across 5 attempted tasks)") in page
    assert "- review rounds per attempt: 1.71 (12 review rounds across 7 attempts)" in page
    assert "- tokens per merged PR: 1,000 (2 merged PRs)" in page
    assert "- time from approval to merge: median 1.75d (T-A 1d; T-C 2.5d)" in page
    assert "- merged PRs later reverted: 1 of 2 (50%)" in page


def test_merge_history_dates_merges_and_finds_reverts(tmp_path: Path):
    """The git collector dates a squash merge by its (#N) subject and
    detects a revert by its body naming the merge commit."""
    metrics = compute_metrics(_doc(), commits=git_commits(_history_repo(tmp_path)))
    assert metrics["approval_to_merge_days"] == pytest.approx(1.75)
    assert metrics["revert_share"] == pytest.approx(0.5)


def test_unreadable_history_degrades_to_a_note(tmp_path: Path):
    """No git history: the measures say so, the page still renders."""
    page = render_status(
        _doc(), project="fixture", factory_root=tmp_path, now=NOW,
        repo=tmp_path / "no-such-repo",
    )
    assert "- time from approval to merge: no merge times recorded" in page
    assert "- merged PRs later reverted: needs the repo's git history" in page
    assert "## Notes" in page


def test_missing_repo_raises_status_error(tmp_path: Path):
    with pytest.raises(StatusError):
        git_commits(tmp_path / "no-such-repo")


# ----------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path):
    """AC-3: outcome measures only — exactly the five, and no gameable
    output metric anywhere on the page."""
    queue = _write_queue(tmp_path)
    page = render_status(queue, project="fixture", factory_root=tmp_path, now=NOW)

    rest = page.split("## Metrics", 1)[1]
    rest = rest.split("## Notes", 1)[0]
    labels = [
        line.split(":", 1)[0].lstrip("- ").strip()
        for line in rest.splitlines() if line.startswith("- ")
    ]
    assert labels == [
        "interventions per task",
        "review rounds per attempt",
        "tokens per merged PR",
        "time from approval to merge",
        "merged PRs later reverted",
    ]
    # Lines of code and PR counts are the classic gameable outputs
    # (RESEARCH-SOFTWARE-FACTORY §6): they appear nowhere, as a measure
    # or otherwise.
    assert "lines of code" not in page.lower()
    assert not re.search(r"\bloc\b", page, re.IGNORECASE)
    assert "PRs opened" not in page
    assert "PR count" not in page


# ----------------------------------------------------------------- AC-4 --


def test_ac_4(tmp_path: Path):
    """AC-4: the week's vents, grouped by category, read through the F-27
    ``weeks_vents`` seam — no second implementation."""
    flush_vents(
        ProgressLog.for_task(tmp_path, "fixture", "T-B"),
        [_vent(category="missing_tool", text="no ripgrep in the sandbox PATH")],
        at="2026-10-16T15:00:00Z",
    )
    flush_vents(
        ProgressLog.for_task(tmp_path, "fixture", "T-D"),
        [_vent(task_id="T-D", seat="reviewer", turn=2,
               category="environment", text="sandbox broke again")],
        at="2026-10-17T15:00:00Z",
    )
    queue = _write_queue(tmp_path)
    page = render_status(queue, project="fixture", factory_root=tmp_path, now=NOW)

    section = page.split("## The week's vents", 1)[1].split("## Metrics", 1)[0]
    # Every category lists, empty ones included, with its count.
    assert "- missing_tool: 1" in section
    assert "- environment: 1" in section
    assert "- unclear_contract: 0" in section
    assert "- flaky_check: 0" in section
    assert "- other: 0" in section
    # Each vent carries its context.
    assert "[T-B/implementer, turn 3] no ripgrep in the sandbox PATH" in section
    assert "[T-D/reviewer, turn 2] sandbox broke again" in section

    # One seam, no second implementation: the page reads the F-27
    # function itself, not a re-typed copy.
    import factory.status as status_module
    import factory.vent as vent_module

    assert status_module.weeks_vents is vent_module.weeks_vents
    # The page's category groups are exactly the seam's five, in order.
    rendered = [
        line.split(":", 1)[0].lstrip("- ").strip()
        for line in section.splitlines() if line.startswith("- ")
    ]
    assert rendered == list(VENT_CATEGORIES)
