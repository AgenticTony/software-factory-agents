"""``factory status`` — the daily summary and outcome metrics (F-11).

One read-only markdown page per project:

* the queue: done, in review, needs you (with escalation reasons),
  awaiting owner;
* the quota used this week (tokens spent by this factory's attempts —
  a spend figure, not a success measure);
* the week's vents by category, read through the F-27
  :func:`factory.vent.weeks_vents` seam (no second implementation);
* the outcome measures only (RESEARCH-SOFTWARE-FACTORY §9): interventions
  per task, review rounds per attempt, tokens per merged PR, time from
  approval to merge, and the share of merged PRs later reverted. Lines of
  code and PR counts are deliberately absent — output metrics lie
  (RESEARCH-SOFTWARE-FACTORY §6), so this page reports none.

The measures, computed from the queue's ``attempts`` (the append-only
record the orchestrator writes):

* **interventions per task** — attempts that ended ``escalated`` (a human
  had to step in), divided by the number of tasks that have at least one
  attempt.
* **review rounds per attempt** — the sum of the attempts' ``rounds``
  divided by the number of attempts.
* **tokens per merged PR** — input + output tokens over all attempts of
  merged tasks, divided by the number of merged tasks (``links.merged``
  equals ``links.pr``; orchestrator-stamped, F-26).
* **time from approval to merge** — per merged task, ``approved_at`` to
  the merge commit's time (from git history, or a caller-supplied
  ``merge_times`` map); reported as the median.
* **merged PRs later reverted** — of the merged PRs, the share whose
  merge commit is named by a later revert commit's
  ``This reverts commit <sha>`` body (the canonical ``git revert``
  message: the revert names the PR's merge commit).

Everything here reads; nothing writes.
"""

from __future__ import annotations

import re
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from factory.vent import VENT_CATEGORIES, weeks_vents

WEEK_DAYS = 7


class StatusError(Exception):
    """A status input could not be read (e.g. the git history)."""


# A GitHub PR url's number: .../pull/7, .../pull-requests/7, or a
# gitlab-style .../-/merge_requests/7 (the same idea).
_PR_NUMBER = re.compile(r"/(?:pull|pull-requests|merge_requests)/(\d+)")
# A squash-merge commit names its PR: "<title> (#7)". The number varies,
# so the subject pattern is compiled per lookup in _merge_commit_for.
# The canonical git revert body names the commit it reverts — for a
# squash merge, that commit IS the PR's merge commit.
_REVERTS = "This reverts commit "


def _parse_time(text: Any) -> datetime | None:
    """Parse an ISO stamp, assuming UTC when the string carries no zone."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def is_merged(task: dict) -> bool:
    """Whether the task's PR is the one GitHub reported merged (F-26).

    ``links.merged`` holds the merged PR's URL and is terminal for that
    PR only, so the stamp is the URL equality, never a flag.
    """
    links = task.get("links") or {}
    return bool(links.get("pr")) and links.get("merged") == links.get("pr")


def pr_number(url: str) -> str | None:
    """The PR number in a PR url, or None when the url names no number."""
    match = _PR_NUMBER.search(url)
    return match.group(1) if match else None


def git_commits(repo: Path | str) -> list[dict[str, str]]:
    """The repo's commit history as ``{sha, subject, body, at}`` dicts.

    One subprocess, read-only. ``at`` is the committer date in ISO-8601 —
    the time a merge (or its revert) actually landed on the branch.
    ``--all`` spans every ref: the caller may sit on a feature branch
    (the orchestrator's clones always do) while the merges live on the
    default branch, and the measures must not silently degrade to zero
    there.

    Raises:
        StatusError: ``repo`` is not a git repository (or has no commits,
            or git is missing) — the merge measures degrade to a note on
            the page instead of guessing.
    """
    try:
        proc = subprocess.run(
            [
                "git", "-C", str(repo), "log", "--all",
                "--pretty=format:%H%x1f%s%x1f%b%x1f%cI%x1e",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise StatusError(f"cannot read git history in {repo}: {exc}") from exc
    commits: list[dict[str, str]] = []
    for record in proc.stdout.split("\x1e"):
        record = record.removeprefix("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) < 4 or not parts[0]:
            continue
        commits.append(
            {"sha": parts[0], "subject": parts[1], "body": parts[2], "at": parts[3]}
        )
    return commits


def _merge_commit_for(
    commits: list[dict[str, str]], number: str
) -> dict[str, str] | None:
    """The newest commit whose subject names the PR.

    Both GitHub merge styles: a squash merge ends its subject with
    ``(#N)``; a merge commit reads ``Merge pull request #N from ...``.
    Repos that merge with merge commits (the JobFinderOS pilot does)
    must date merges and detect reverts exactly like squash-merged ones.
    """
    squash = re.compile(rf"\(#{re.escape(number)}\)\s*$")
    merge_commit = re.compile(rf"^Merge pull request #{re.escape(number)} from ")
    for commit in commits:
        if squash.search(commit["subject"]) or merge_commit.match(
            commit["subject"]
        ):
            return commit
    return None


def _is_reverted(commits: list[dict[str, str]], merge_sha: str) -> bool:
    """Whether some commit's body names ``merge_sha`` as the reverted one."""
    return any(_REVERTS + merge_sha in commit["body"] for commit in commits)


def _tokens(attempts: list[dict]) -> int:
    """Input + output tokens over attempts; missing fields count zero."""
    return sum(
        (attempt.get("input_tokens") or 0) + (attempt.get("output_tokens") or 0)
        for attempt in attempts
    )


def compute_metrics(
    queue: dict,
    *,
    commits: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The outcome measures (RESEARCH-SOFTWARE-FACTORY §9) from ``attempts``.

    Args:
        queue: The parsed, validated queue document.
        commits: Optional commit history (:func:`git_commits` shape) used
            both to date merges (squash ``(#N)`` and merge-commit
            ``Merge pull request #N`` subjects) and to detect reverts.

    Returns:
        A dict with, per the module docstring: ``interventions_per_task``,
        ``review_rounds_per_attempt``, ``tokens_per_merged_pr``,
        ``approval_to_merge_days`` (median), ``approval_to_merge_detail``
        (per-task ``(id, days)``), ``revert_share`` plus ``reverted`` /
        ``merged`` counts, the raw ``interventions`` / ``attempted_tasks``
        / ``attempts`` / ``rounds`` counts, ``merges_undated`` (merged
        tasks whose merge time could not be established) and
        ``merges_anomalous`` (a merge stamp older than the approval —
        a wrong subject match or a hand-edited stamp, never missing
        history). Measures with no data are None, never invented.
    """
    tasks = queue.get("tasks") or []
    attempts = [a for task in tasks for a in task.get("attempts") or []]
    attempted_tasks = [task for task in tasks if task.get("attempts")]
    interventions = [a for a in attempts if a.get("outcome") == "escalated"]
    rounds = sum(a.get("rounds") or 0 for a in attempts)
    merged = [task for task in tasks if is_merged(task)]

    durations: list[tuple[str, float]] = []
    undated = 0
    anomalous = 0
    merge_shas: list[str] = []
    for task in merged:
        url = (task.get("links") or {}).get("pr") or ""
        number = pr_number(url)
        commit = _merge_commit_for(commits, number) if commits and number else None
        if commit is not None:
            merge_shas.append(commit["sha"])
        approved = _parse_time(task.get("approved_at"))
        merged_at = _parse_time((commit or {}).get("at"))
        if approved is None or merged_at is None:
            undated += 1
            continue
        if merged_at < approved:
            anomalous += 1
            continue
        durations.append((task.get("id", "?"), (merged_at - approved).total_seconds() / 86400))

    reverted = sum(1 for sha in merge_shas if _is_reverted(commits or [], sha))
    return {
        "interventions_per_task": (
            len(interventions) / len(attempted_tasks) if attempted_tasks else None
        ),
        "review_rounds_per_attempt": rounds / len(attempts) if attempts else None,
        "tokens_per_merged_pr": (
            round(sum(_tokens(t.get("attempts") or []) for t in merged) / len(merged))
            if merged
            else None
        ),
        "approval_to_merge_days": (
            statistics.median(days for _, days in durations) if durations else None
        ),
        "approval_to_merge_detail": durations,
        "revert_share": reverted / len(merged) if merged and commits is not None else None,
        "reverted": reverted,
        "merged": len(merged),
        "interventions": len(interventions),
        "attempted_tasks": len(attempted_tasks),
        "attempts": len(attempts),
        "rounds": rounds,
        "merges_undated": undated,
        "merges_anomalous": anomalous,
    }


def _escalation_of(task: dict) -> tuple[str | None, str | None]:
    """The task's escalation reason and note: the most recent attempt
    that carries one (the orchestrator appends the escalation to the
    attempt that produced it)."""
    for attempt in reversed(task.get("attempts") or []):
        if attempt.get("escalation"):
            return attempt["escalation"], attempt.get("note")
    return None, None


def needs_human_tasks(queue: dict) -> list[dict[str, Any]]:
    """Every ``needs_human`` task with its escalation reason."""
    out: list[dict[str, Any]] = []
    for task in queue.get("tasks") or []:
        if task.get("status") != "needs_human":
            continue
        escalation, note = _escalation_of(task)
        out.append(
            {
                "id": task.get("id", "?"),
                "title": task.get("title", ""),
                "escalation": escalation,
                "note": note,
            }
        )
    return out


def _fmt_days(days: float) -> str:
    return f"{days:.2f}".rstrip("0").rstrip(".") + "d"


def _render_metrics(metrics: dict[str, Any], notes: list[str]) -> list[str]:
    """The five outcome-measure bullets — and nothing else."""
    lines = ["## Metrics", ""]
    lines.append("Outcome measures only (RESEARCH-SOFTWARE-FACTORY §9) —")
    lines.append("no output-volume counters, which Goodhart turns into the goal (§6).")
    lines.append("")

    if metrics["interventions_per_task"] is None:
        lines.append("- interventions per task: no attempts yet")
    else:
        lines.append(
            f"- interventions per task: {metrics['interventions_per_task']:.2f} "
            f"({metrics['interventions']} escalated attempts across "
            f"{metrics['attempted_tasks']} attempted tasks)"
        )

    if metrics["review_rounds_per_attempt"] is None:
        lines.append("- review rounds per attempt: no attempts yet")
    else:
        lines.append(
            "- review rounds per attempt: "
            f"{metrics['review_rounds_per_attempt']:.2f} "
            f"({metrics['rounds']} review rounds across {metrics['attempts']} attempts)"
        )

    if metrics["tokens_per_merged_pr"] is None:
        lines.append("- tokens per merged PR: no merged PRs yet")
    else:
        lines.append(
            f"- tokens per merged PR: {metrics['tokens_per_merged_pr']:,} "
            f"({metrics['merged']} merged PRs)"
        )

    if metrics["merged"] == 0:
        lines.append("- time from approval to merge: no merged PRs yet")
    elif metrics["approval_to_merge_days"] is None:
        lines.append("- time from approval to merge: no merge times recorded")
    else:
        detail = "; ".join(
            f"{tid} {_fmt_days(days)}"
            for tid, days in metrics["approval_to_merge_detail"]
        )
        lines.append(
            "- time from approval to merge: median "
            f"{_fmt_days(metrics['approval_to_merge_days'])} ({detail})"
        )
    if metrics["merges_undated"]:
        notes.append(
            f"{metrics['merges_undated']} merged PR(s) without a merge time "
            "(no git history for the repo, or no (#N) merge commit)"
        )
    if metrics["merges_anomalous"]:
        notes.append(
            f"{metrics['merges_anomalous']} merged PR(s) stamped before "
            "approval (wrong subject match or hand-edited stamp — not "
            "missing history)"
        )

    if metrics["merged"] == 0:
        lines.append("- merged PRs later reverted: no merged PRs yet")
    elif metrics["revert_share"] is None:
        lines.append(
            "- merged PRs later reverted: needs the repo's git history"
        )
    else:
        lines.append(
            f"- merged PRs later reverted: {metrics['reverted']} of "
            f"{metrics['merged']} ({metrics['revert_share']:.0%})"
        )
    return lines


def render_status(
    queue: dict,
    *,
    project: str,
    factory_root: Path | str,
    now: datetime | None = None,
    repo: Path | str | None = None,
) -> str:
    """Render the status page for one project.

    Args:
        queue: The parsed, validated queue document.
        project: The project name (vents are read per project).
        factory_root: The factory root holding ``progress/``.
        now: The clock for the week windows; defaults to UTC now.
        repo: The project repo, for merge times and revert detection;
            unreadable history degrades to a note, never a crash.

    Returns:
        The markdown page. Read-only: nothing under ``factory_root`` or
        ``repo`` is touched.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    horizon = moment - timedelta(days=WEEK_DAYS)
    tasks = queue.get("tasks") or []

    lines = [
        f"# factory status — {project} — {moment:%Y-%m-%d %H:%M %Z}",
        "",
    ]

    # -- Queue ------------------------------------------------------------
    by_status: dict[str, list[dict]] = {}
    for task in tasks:
        by_status.setdefault(task.get("status", "?"), []).append(task)
    lines.append("## Queue")
    lines.append("")
    for status, label in (
        ("done", "done"),
        ("in_review", "in review"),
        ("needs_human", "needs you"),
        ("awaiting_owner", "awaiting owner"),
    ):
        group = by_status.pop(status, [])
        if not group:
            continue
        if status == "needs_human":
            lines.append(f"- needs you ({len(group)}):")
            for task in group:
                escalation, note = _escalation_of(task)
                reason = escalation or "no escalation recorded"
                suffix = f": {note}" if note else ""
                lines.append(f"  - {task.get('id', '?')} — {reason}{suffix}")
        elif status == "awaiting_owner":
            lines.append(f"- awaiting owner ({len(group)}):")
            for task in group:
                steps = task.get("owner_steps") or []
                open_steps = [s for s in steps if not s.get("done")]
                if not steps:
                    lines.append(
                        f"  - {task.get('id', '?')} — no owner steps recorded"
                    )
                elif open_steps:
                    names = "; ".join(f'"{s.get("text", "?")}"' for s in open_steps)
                    lines.append(
                        f"  - {task.get('id', '?')} — "
                        f"{len(open_steps)} of {len(steps)} owner steps open: {names}"
                    )
                else:
                    # Every step done and still awaiting_owner: the closing
                    # transition has not run (or was refused). Saying "no
                    # steps" would be a false statement hiding a stuck task.
                    lines.append(
                        f"  - {task.get('id', '?')} — all owner steps done "
                        "(still awaiting the done transition)"
                    )
        else:
            ids = ", ".join(t.get("id", "?") for t in group)
            lines.append(f"- {label} ({len(group)}): {ids}")
    for status in sorted(by_status):
        group = by_status[status]
        ids = ", ".join(t.get("id", "?") for t in group)
        lines.append(f"- {status} ({len(group)}): {ids}")

    # -- Quota used this week ---------------------------------------------
    notes: list[str] = []
    week_attempts = []
    unparsable = 0
    for task in tasks:
        for attempt in task.get("attempts") or []:
            started = _parse_time(attempt.get("started"))
            if started is None:
                # Keep the gap visible (the vents seam's posture): a
                # malformed stamp must not hide spend from the owner.
                unparsable += 1
            elif started >= horizon:
                week_attempts.append(attempt)
    if unparsable:
        notes.append(
            f"{unparsable} attempt(s) without a parsable started stamp, "
            "excluded from the week's quota"
        )
    lines += ["", "## Quota used this week", ""]
    lines.append(
        f"{_tokens(week_attempts):,} tokens across {len(week_attempts)} attempt(s) "
        f"in the trailing {WEEK_DAYS} days (spend, not a success measure)"
    )

    # -- The week's vents (through the F-27 seam) --------------------------
    unreadable: list[str] = []
    grouped = weeks_vents(factory_root, project, now=moment, unreadable=unreadable)
    lines += ["", "## The week's vents", ""]
    for category in VENT_CATEGORIES:
        vents = grouped.get(category, [])
        lines.append(f"- {category}: {len(vents)}")
        for vent in vents:
            lines.append(
                f"  - [{vent.get('task_id', '?')}/{vent.get('seat', '?')}, "
                f"turn {vent.get('turn', 0)}] {vent.get('text', '')}"
            )
    if unreadable:
        notes.append(
            f"{len(unreadable)} progress log(s) unreadable, skipped: "
            + "; ".join(unreadable)
        )

    # -- Metrics -----------------------------------------------------------
    commits: list[dict[str, str]] | None = None
    if repo is not None:
        try:
            commits = git_commits(repo)
        except StatusError as exc:
            notes.append(str(exc))
    metrics = compute_metrics(queue, commits=commits)
    lines += [""] + _render_metrics(metrics, notes)
    if notes:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in notes]
    lines.append("")
    return "\n".join(lines)
