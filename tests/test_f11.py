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
    _review_git(repo, "init", "-b", "main")
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


# ------------------------------ review round (8 findings, 2026-10-26) -------


def _review_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _review_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _review_git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("base\n")
    _review_git(repo, "add", "-A")
    _review_git(repo, "-c", "user.email=s@x", "-c", "user.name=s",
         "commit", "-q", "-m", "base")
    _review_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "g").write_text("later\n")
    _review_git(repo, "add", "-A")
    _review_git(repo, "-c", "user.email=s@x", "-c", "user.name=s",
         "commit", "-q", "-m", "work on feature")
    _review_git(repo, "checkout", "-q", "main")
    (repo / "m").write_text("merged\n")
    _review_git(repo, "add", "-A")
    _review_git(repo, "-c", "user.email=s@x", "-c", "user.name=s",
         "commit", "-q", "-m", "Task T-A (#7)")
    _review_git(repo, "checkout", "-q", "feature")
    return repo


def test_review_f1_history_spans_all_branches(tmp_path: Path):
    """F1: git_commits read HEAD only, so a clone checked out on a
    feature branch (the orchestrator's clones always are) missed every
    merge on main — approval-to-merge and reverts silently degraded to
    nothing. History must reach the default branch from any checkout
    (since review round 2: the default branch only, not every ref)."""
    repo = _review_repo(tmp_path)
    subjects = [c["subject"] for c in git_commits(repo)]
    assert "Task T-A (#7)" in subjects, (
        f"merge on main invisible from the feature checkout: {subjects}"
    )


def test_review_f4_merge_commit_style_prs_are_dated(tmp_path: Path):
    """F4: only squash-merge subjects '(#N)' were recognised; repos
    merged with merge commits ('Merge pull request #N from ...') were
    never dated and their reverts never detected — the measure quietly
    reads 0 there (the JobFinderOS pilot merges this way)."""
    from factory.status import _merge_commit_for

    commits = [
        {"sha": "b" * 40, "subject": "Merge pull request #7 from owner/branch",
         "body": "", "at": "2026-10-02T09:00:00Z"},
    ]
    found = _merge_commit_for(commits, "7")
    assert found is not None and found["sha"] == "b" * 40
    doc = _doc()
    metrics = compute_metrics(doc, commits=commits)
    assert metrics["approval_to_merge_days"] is not None


def test_review_f2_awaiting_owner_all_done_is_truthful(tmp_path: Path):
    """F2: an awaiting_owner task with every step ticked rendered 'no
    owner steps recorded' — a false statement hiding a stuck task. The
    page must distinguish all-done from none-exist."""
    steps_done = [
        {"text": "do the thing", "done": True,
         "done_at": "2026-10-02T00:00:00Z", "done_by": "owner",
         "evidence": "did it"},
    ]
    queue = {
        "version": 1,
        "tasks": [
            _task("T-D", "awaiting_owner", owner_steps=steps_done),
            _task("T-E", "awaiting_owner", owner_steps=[]),
        ],
    }
    page = render_status(queue, project="fixture",
                         factory_root=tmp_path, now=NOW)
    assert "no owner steps recorded" in page  # T-E: none exist
    assert "all owner steps done" in page, (
        "all-done renders as 'no owner steps recorded': "
        "a false statement hiding a stuck task\n" + page
    )


def test_review_f5b_unparsable_started_counts_as_a_note(tmp_path: Path):
    """F5(b): attempts whose started stamp cannot be parsed were
    silently dropped from the week's quota — spend hidden with no
    signal, inconsistent with the vents seam's keep-and-note posture."""
    bad = _attempt("20260101T000000Z-xx11", in_tok=500, out_tok=50)
    bad["started"] = "not-a-stamp"
    queue = {"version": 1, "tasks": [_task("T-F", "done", attempts=[bad])]}
    page = render_status(queue, project="fixture",
                         factory_root=tmp_path, now=NOW)
    assert "1 attempt(s) without a parsable started stamp" in page, (
        f"unparsable spend hidden silently:\n{page}"
    )


def test_review_f6_merge_before_approval_is_its_own_note(tmp_path: Path):
    """F6: a merge stamp older than the approval was conflated with 'no
    merge time'; neither cause was true. The anomaly gets its own arm."""
    doc = _doc()
    commits = [
        {"sha": "a" * 40, "subject": "Task T-A (#7)", "body": "",
         "at": "2026-09-01T09:00:00Z"},  # before approved_at (10-01)
        {"sha": "c" * 40, "subject": "Task T-C (#9)", "body": "",
         "at": "2026-10-07T12:00:00Z"},
    ]
    metrics = compute_metrics(doc, commits=commits)
    assert metrics["merges_anomalous"] == 1
    assert metrics["merges_undated"] == 0


def test_review_f5_dead_merge_times_parameter_is_gone():
    """F5: compute_metrics' merge_times parameter was dead code with a
    docstring claiming tests used it — they never did."""
    import inspect

    params = inspect.signature(compute_metrics).parameters
    assert "merge_times" not in params


def test_review_f7_render_uses_the_group_it_already_has(tmp_path):
    """F7: render re-walked the whole queue through needs_human_tasks
    although by_status already held exactly the needs-human tasks. The
    render must not depend on the second walk."""
    import factory.status as status_module

    queue = {
        "version": 1,
        "tasks": [
            _task("T-G", "needs_human",
                  attempts=[_attempt("20260101T000000Z-xx12",
                                     outcome="escalated",
                                     escalation="attempts_exhausted",
                                     note="stub failed thrice")]),
        ],
    }
    original = status_module.needs_human_tasks

    def _boom(queue):
        raise AssertionError("render re-walked the queue (F7)")

    status_module.needs_human_tasks = _boom
    try:
        page = render_status(queue, project="fixture",
                             factory_root=tmp_path, now=NOW)
    finally:
        status_module.needs_human_tasks = original
    assert "T-G" in page and "attempts_exhausted" in page


# ------------------- review round 2 (PRs #58/#59 findings, 2026-10-27) -------


def _commit(repo: Path, message: str, at: str, *extra: str) -> str:
    """A pinned-date commit in a fixture repo; returns its sha."""
    (repo / "f.txt").write_text(message + "\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=s@x", "-c", "user.name=s",
         "commit", "-q", "--allow-empty", "-m", message, *extra, at=at)
    return _git(repo, "rev-parse", "HEAD")


def test_review2_58_f1_revert_share_is_unknown_without_merge_commits(
    tmp_path: Path,
):
    """#58 F1: with history present but no merge commit found for any
    merged PR, revert_share divided by the merged count and the page
    claimed '0 of 2 (0%)' — a confident figure for an unchecked measure."""
    commits = [
        {"sha": "e" * 40, "subject": "rebase-merged work, no PR number",
         "body": "", "at": "2026-10-02T09:00:00Z"},
        {"sha": "f" * 40, "subject": "Revert something",
         "body": f"This reverts commit {'e' * 40}.",
         "at": "2026-10-03T09:00:00Z"},
    ]
    metrics = compute_metrics(_doc(), commits=commits)
    assert metrics["revert_share"] is None, metrics["revert_share"]
    assert metrics["merges_unresolved"] == 2

    repo = tmp_path / "norepo-match"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "unrelated work", "2026-10-02T09:00:00Z")
    page = render_status(_doc(), project="fixture", factory_root=tmp_path,
                         now=NOW, repo=repo)
    assert "0 of 2" not in page, page
    assert ("- merged PRs later reverted: unknown — no merge commit found "
            "for any of the 2 merged PRs") in page, page


def test_review2_58_f1_revert_share_counts_only_resolved_merges():
    """#58 F1 (partial): an unresolved merge must not dilute the share —
    the denominator is the merges actually checked, and the gap is said."""
    commits = [c for c in COMMIT_FIXTURE if c["subject"] != "Task T-C (#9)"]
    metrics = compute_metrics(_doc(), commits=commits)
    assert metrics["revert_share"] == pytest.approx(1.0)
    assert (metrics["reverted"], metrics["merges_unresolved"]) == (1, 1)
    notes: list[str] = []
    from factory.status import _render_metrics

    lines = _render_metrics(metrics, notes)
    assert ("- merged PRs later reverted: 1 of 1 checked (100%); 1 merged "
            "PR(s) with no merge commit found, not counted") in lines, lines


def test_review2_58_f2_stash_is_not_taken_as_the_merge_commit(tmp_path: Path):
    """#58 F2 / #59 F1(a): `git log --all` read refs/stash, whose subject
    'WIP on main: <sha> Task T-A (#7)' ends in (#7) and is newest, so the
    stash became PR #7's merge commit — wrong merge date, and a real
    revert of the merge was missed."""
    from factory.status import _merge_commit_for

    repo = tmp_path / "stashed"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    merge = _commit(repo, "Task T-A (#7)", "2026-10-02T09:00:00Z")
    # Stash while HEAD is the squash merge: subject 'WIP on main: <short>
    # Task T-A (#7)', newer than the merge.
    (repo / "f.txt").write_text("dirty\n")
    _git(repo, "-c", "user.email=s@x", "-c", "user.name=s", "stash",
         at="2026-10-06T09:00:00Z")
    _commit(repo, 'Revert "Task T-A (#7)"', "2026-10-07T09:00:00Z",
            "-m", f"This reverts commit {merge}.")
    commits = git_commits(repo)
    found = _merge_commit_for(commits, "7")
    assert found is not None and found["sha"] == merge, found
    doc = {"version": 1, "tasks": [
        _task("T-A", "done", links={"pr": PR_A, "merged": PR_A}),
    ]}
    metrics = compute_metrics(doc, commits=commits)
    assert metrics["reverted"] == 1, metrics
    assert dict(metrics["approval_to_merge_detail"]) == {"T-A": 1.0}


def test_review2_59_f1_unmerged_revert_branch_is_not_a_revert(tmp_path: Path):
    """#59 F1(b): a revert that lives only on an unmerged branch (GitHub's
    Revert button pushes revert-N) was counted, so the PR showed reverted
    while the revert PR was still open. Only the default branch counts."""
    repo = tmp_path / "revert-branch"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    merge = _commit(repo, "Task T-A (#7)", "2026-10-02T09:00:00Z")
    _git(repo, "checkout", "-q", "-b", "revert-7")
    _commit(repo, 'Revert "Task T-A (#7)"', "2026-10-05T09:00:00Z",
            "-m", f"This reverts commit {merge}.")
    # A cherry-picked backport elsewhere must not outrank the real merge.
    _git(repo, "checkout", "-q", "-b", "release", "main")
    backport = _commit(repo, "Task T-A (#7)", "2026-10-06T09:00:00Z")
    _git(repo, "checkout", "-q", "main")
    commits = git_commits(repo)
    shas = {c["sha"] for c in commits}
    assert backport not in shas
    doc = {"version": 1, "tasks": [
        _task("T-A", "done", links={"pr": PR_A, "merged": PR_A}),
    ]}
    metrics = compute_metrics(doc, commits=commits)
    assert metrics["reverted"] == 0, "revert on an unmerged branch counted"
    assert metrics["revert_share"] == 0


def test_review2_59_f1_clone_on_feature_branch_reads_origin_default(
    tmp_path: Path,
):
    """#59 F1 keeps the #59 F1 guarantee: a clone checked out on a feature
    branch, with no local default branch, still reads the merges from
    origin's default branch (origin/HEAD) — and not the feature's own
    unmerged revert."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    merge = _commit(upstream, "Task T-A (#7)", "2026-10-02T09:00:00Z")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "checkout", "-q", "-b", "feature")
    _git(clone, "branch", "-q", "-D", "main")
    _commit(clone, "feature revert", "2026-10-05T09:00:00Z",
            "-m", f"This reverts commit {merge}.")
    doc = {"version": 1, "tasks": [
        _task("T-A", "done", links={"pr": PR_A, "merged": PR_A}),
    ]}
    metrics = compute_metrics(doc, commits=git_commits(clone))
    assert dict(metrics["approval_to_merge_detail"]) == {"T-A": 1.0}
    assert metrics["reverted"] == 0


def test_review2_59_f2_texts_describe_current_behaviour():
    """#59 F2: the module docstring still cited the removed merge_times
    map, and the undated note named only '(#N)' squash commits although
    'Merge pull request #N' subjects are matched too."""
    import factory.status as status_module
    from factory.status import _render_metrics

    assert "merge_times" not in (status_module.__doc__ or "")
    metrics = compute_metrics(_doc(), commits=[])
    notes: list[str] = []
    _render_metrics(metrics, notes)
    undated = [n for n in notes if "without a merge time" in n]
    assert undated and "Merge pull request #N" in undated[0], notes
