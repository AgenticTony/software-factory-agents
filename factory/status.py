"""``factory status`` — the daily summary and outcome metrics (F-11).

One read-only markdown page per project:

* the queue: done, in review, needs you (with escalation reasons),
  awaiting owner;
* the quota used this week (tokens spent by this factory's attempts —
  a spend figure, not a success measure);
* the week's sandbox-bypass attempts denied (F-42): Bash calls the
  sessions made with ``dangerouslyDisableSandbox`` and the PreToolUse
  hook refused — a security signal over the same week window;
* the week's sandbox escapees (F-47): processes that escaped a sandboxed
  command's process group and were force-closed by the drain, counted
  from the ``sandbox_escapee`` events in the project's run traces;
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
  the time of its merge commit on the default branch (a squash merge's
  ``<title> (#N)`` or a merge commit's ``Merge pull request #N from ...``
  subject); reported as the median.
* **merged PRs later reverted** — of the merged PRs whose merge commit
  was found, the share named by a revert commit's
  ``This reverts commit <sha>`` body on the default branch (the canonical
  ``git revert`` message, for either merge style). Merged PRs with no
  merge commit found are not counted as unreverted: when none is found
  the share is unknown, and a partial gap is said on the page.

Everything here reads; nothing writes.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from factory.config import (
    CONFIG_FILE,
    ConfigError,
    FactoryConfig,
    load_config,
)
from factory.egress import strip_control_sequences
from factory.runs import project_component
from factory.vent import VENT_CATEGORIES, weeks_vents

WEEK_DAYS = 7


class StatusError(Exception):
    """A status input could not be read (e.g. the git history)."""


# A GitHub PR url's number: .../pull/7, .../pull-requests/7, or a
# gitlab-style .../-/merge_requests/7 (the same idea).
_PR_NUMBER = re.compile(r"/(?:pull|pull-requests|merge_requests)/(\d+)")
# A merge names its PR: a squash merge's "<title> (#7)" subject or a
# merge commit's "Merge pull request #7 from ..." subject. The number
# varies, so the patterns are compiled per lookup in _merge_commit_for.
# The canonical git revert body names the commit it reverts — the PR's
# squash commit or its merge commit (``git revert -m 1`` keeps the line).
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


def _git_out(repo: Path | str, *args: str) -> str | None:
    """A git query's stdout, stripped; None when git refuses it."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return proc.stdout.strip()


def _default_branch_refs(repo: Path | str) -> list[str]:
    """The refs that hold the repo's default branch, never other branches.

    The default branch is the one ``origin/HEAD`` names (``git clone``
    sets it), else a local ``main`` or ``master``. Both its local branch
    and its ``origin/`` tracking ref are read — the caller may sit on a
    feature branch with no local default branch (an orchestrator clone),
    or on a checkout whose local default is ahead of the remote. With no
    default branch found, ``HEAD`` is the only honest choice left.

    Stashes, unmerged branches (GitHub's ``revert-N``) and backport
    branches are deliberately excluded: a stash subject ends in the
    stashed commit's ``(#N)`` and a revert that never landed is no revert.
    """
    name: str | None = None
    origin_head = _git_out(
        repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"
    )
    if origin_head and origin_head.startswith("origin/"):
        name = origin_head.removeprefix("origin/")
    else:
        for candidate in ("main", "master"):
            if _git_out(repo, "rev-parse", "-q", "--verify",
                        f"refs/heads/{candidate}^{{commit}}") is not None:
                name = candidate
                break
    if name is None:
        return ["HEAD"]
    refs = [
        ref for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}")
        if _git_out(repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
        is not None
    ]
    return refs or ["HEAD"]


def git_commits(repo: Path | str) -> list[dict[str, str]]:
    """The default branch's commit history as ``{sha, subject, body, at}``.

    Read-only. ``at`` is the committer date in ISO-8601 — the time a
    merge (or its revert) actually landed on the branch. The history is
    the default branch's (:func:`_default_branch_refs`), not the checked
    out one: the caller may sit on a feature branch (the orchestrator's
    clones always do) while the merges live on the default branch — and
    not every ref either, since a stash or an unmerged revert branch
    would date merges wrongly and count reverts that never landed.

    Raises:
        StatusError: ``repo`` is not a git repository (or has no commits,
            or git is missing) — the merge measures degrade to a note on
            the page instead of guessing.
    """
    refs = _default_branch_refs(repo)
    try:
        proc = subprocess.run(
            [
                "git", "-C", str(repo), "log", *refs,
                "--pretty=format:%H%x1f%s%x1f%b%x1f%cI%x1e", "--",
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
    """The newest commit whose subject names the PR (in ``commits``,
    which :func:`git_commits` limits to the default branch).

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


def _bypass_denials(attempt: dict) -> int:
    """One attempt's sandbox-bypass denial count; malformed counts zero
    (the queue is schema-validated, so a non-int here is a hand edit —
    shown as absent, never crashed on)."""
    value = attempt.get("sandbox_bypass_denials")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


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
            None means no history was read; an empty list means history
            was read and holds nothing.

    Returns:
        A dict with, per the module docstring: ``interventions_per_task``,
        ``review_rounds_per_attempt``, ``tokens_per_merged_pr``,
        ``approval_to_merge_days`` (median), ``approval_to_merge_detail``
        (per-task ``(id, days)``), ``revert_share`` (over the merges
        whose commit was found; None when none was, or with no history)
        plus ``reverted`` / ``merged`` counts and ``merges_unresolved``
        (merged tasks with no merge commit found; None with no history),
        the raw ``interventions`` / ``attempted_tasks``
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
    # The share is over the merges actually checked: a merged PR whose
    # merge commit was never found is unknown, not "not reverted".
    unresolved = len(merged) - len(merge_shas) if commits is not None else None
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
        "revert_share": reverted / len(merge_shas) if merge_shas else None,
        "reverted": reverted,
        "merged": len(merged),
        "merges_unresolved": unresolved,
        "interventions": len(interventions),
        "attempted_tasks": len(attempted_tasks),
        "attempts": len(attempts),
        "rounds": rounds,
        "merges_undated": undated,
        "merges_anomalous": anomalous,
    }


def _week_escapees(
    factory_root: Path | str, project: str, horizon: datetime,
    notes: list[str],
) -> tuple[int, list[str]]:
    """Count the project's ``sandbox_escapee`` trace events since
    ``horizon`` (F-47, I-9) and name the runs they came from.

    Only a top-level ``type`` counts — a session's forwarded ``tool_call``
    cannot carry one (the forwarder passes ``tool_call`` only), and a
    nested field never matches. A malformed line or an unparsable stamp
    is skipped, and an unreadable trace file is named in the notes —
    the gap stays visible, like the vents seam's. The project is a
    validated slug (F-62 AC-3): this glob is a read, and an
    unvalidated component turns it into a cross-project /
    arbitrary-directory trace read."""
    directory = Path(factory_root) / "trace" / project_component(project)
    count = 0
    runs: list[str] = []
    unreadable: list[str] = []
    try:
        files = sorted(directory.glob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            unreadable.append(f"{path.name} ({exc.strerror or exc})")
            continue
        if "sandbox_escapee" not in text:
            continue
        for line in text.splitlines():
            if "sandbox_escapee" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "sandbox_escapee":
                continue
            stamp = _parse_time(event.get("ts"))
            if stamp is None or stamp < horizon:
                continue
            count += 1
            run_id = str(event.get("run_id") or path.stem)
            if run_id not in runs:
                runs.append(run_id)
    if unreadable:
        notes.append(
            f"{len(unreadable)} trace file(s) unreadable, escapee count may be "
            "low: " + "; ".join(unreadable)
        )
    return count, runs


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
            "(no git history for the repo, no '(#N)' or 'Merge pull "
            "request #N' commit on its default branch, or no approved_at)"
        )
    if metrics["merges_anomalous"]:
        notes.append(
            f"{metrics['merges_anomalous']} merged PR(s) stamped before "
            "approval (wrong subject match or hand-edited stamp — not "
            "missing history)"
        )

    if metrics["merged"] == 0:
        lines.append("- merged PRs later reverted: no merged PRs yet")
    elif metrics["merges_unresolved"] is None:
        lines.append(
            "- merged PRs later reverted: needs the repo's git history"
        )
    elif metrics["revert_share"] is None:
        lines.append(
            "- merged PRs later reverted: unknown — no merge commit found "
            f"for any of the {metrics['merged']} merged PRs"
        )
    elif metrics["merges_unresolved"]:
        checked = metrics["merged"] - metrics["merges_unresolved"]
        lines.append(
            f"- merged PRs later reverted: {metrics['reverted']} of "
            f"{checked} checked ({metrics['revert_share']:.0%}); "
            f"{metrics['merges_unresolved']} merged PR(s) with no merge "
            "commit found, not counted"
        )
    else:
        lines.append(
            f"- merged PRs later reverted: {metrics['reverted']} of "
            f"{metrics['merged']} ({metrics['revert_share']:.0%})"
        )
    return lines


# F-59 AC-4: the four usage fields the attempt records carry (validated
# at the record boundary; an unknown count is None here, never 0).
_USAGE_FIELDS = ("input", "cache_read", "cache_write", "output")


def _fmt_millions(count: int | None) -> str:
    """One usage count as human tokens: 1.2M / 890k / unknown."""
    if count is None:
        return "unknown"
    if count >= 1_000_000:
        value = count / 1_000_000
        return f"{value:.2f}M" if value < 10 else f"{value:.1f}M"
    if count >= 10_000:
        return f"{round(count / 1_000)}k"
    return f"{count:,}"


def _provider_rows(queue: dict) -> "list[tuple[tuple[str, str], dict]]":
    """Aggregate (kind, model) usage over every attempt that carries an
    F-59 provider record, in first-seen order. R1-5: an attempt with the
    per-(kind, model) breakdown contributes each row of it (the records'
    own parent-stamped providers); an attempt with only the primary
    provider contributes there. Attempts without any provider record
    (before F-59, or stub-runner rounds) count once as ``legacy``.

    R1-6: a column with ANY unknown count renders unknown — collected
    values per column, never a partial sum presented as the total."""
    rows: "dict[tuple[str, str], dict]" = {}
    legacy = 0

    def bucket(kind: str, model: str) -> dict:
        key = (kind, model)
        if key not in rows:
            rows[key] = {name: [] for name in _USAGE_FIELDS}
        return rows[key]

    def add(kind: str, model: str, usage: dict) -> None:
        cells = bucket(kind, model)
        for name in _USAGE_FIELDS:
            value = usage.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                value = None
            if value is not None and value < 0:
                value = None
            cells[name].append(value)

    for task in queue.get("tasks") or []:
        for attempt in task.get("attempts") or []:
            breakdown = attempt.get("provider_usage")
            primary = attempt.get("provider")
            if isinstance(breakdown, list) and breakdown:
                for row in breakdown:
                    if not isinstance(row, dict) or not row.get("kind"):
                        continue
                    add(str(row["kind"]), str(row.get("model") or "?"),
                        row.get("usage") or {})
            elif isinstance(primary, dict) and primary.get("kind"):
                add(str(primary["kind"]),
                    str(primary.get("model") or "?"),
                    attempt.get("token_usage") or {})
            else:
                legacy += 1
    if legacy:
        rows[("legacy", "(attempts before F-59)")] = {
            "attempts": legacy, **{name: [None] for name in _USAGE_FIELDS}}
    return list(rows.items())


def _column(values: "list | None") -> "int | None":
    """One column's total: None (unknown) when ANY collected value is
    unknown (R1-6 — never a partial sum), else the sum."""
    values = values or []
    if any(value is None for value in values):
        return None
    return sum(values)


def _estimate_cost(rates: "dict[str, float] | None",
                   row: dict) -> "str":
    """The estimated cost of one provider row from its per-million
    rates; ``n/a`` when any part is unknown or unpriced — never $0.00."""
    if rates is None:
        return "n/a"
    total = 0.0
    for name in _USAGE_FIELDS:
        value = _column(row.get(name) or [])
        if value is None:
            return "n/a"
        total += (value / 1_000_000) * rates[name]
    return f"${total:.2f}"


def _import_session_rows(
    factory_root: "Path | str | None", project: str,
) -> "tuple[list[dict], list[str]]":
    """The import session records under ``<factory-root>/import/`` (F-67
    AC-2), filtered to this project: one row per record (the sessions
    the importer and its repairs ran). Malformed files degrade to a
    note — status is read-only and never crashes on them."""
    if factory_root is None:
        return [], []
    directory = Path(factory_root) / "import"
    if not directory.is_dir():
        return [], []
    rows: list[dict] = []
    notes: list[str] = []
    for path in sorted(directory.glob("*.record.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            notes.append(f"{path.name}: unreadable import record, skipped")
            continue
        if not isinstance(data, dict) or data.get("project") != project:
            continue
        rows.append(data)
    return rows, notes


def _render_import_sessions(
    factory_root: "Path | str | None", project: str,
    config: "FactoryConfig | None",
) -> "list[str]":
    """The F-67 AC-2 import-session table under the Providers section:
    kind, model, usage and an estimated cost under the same price rule
    as the attempts above — one row per recorded session, unknown counts
    rendered unknown, never a partial sum and never 0."""
    rows, notes = _import_session_rows(factory_root, project)
    lines: list[str] = []
    if not rows and not notes:
        return lines
    lines += ["", "Import sessions (import/ records):", ""]
    if not rows:
        lines.append("- none recorded")
    else:
        lines.append("| session | provider | model | in | cache read | "
                     "cache write | out | est. cost |")
        lines.append("|---|---|---|---|---|---|---|---|")
        prices = (config.prices if config is not None else {}) or {}
        for row in rows:
            provider = row.get("provider")
            provider = provider if isinstance(provider, dict) else {}
            kind = str(provider.get("kind") or "unknown")
            model = str(row.get("model") or "unknown")
            usage = row.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            cells = {
                name: [usage.get(name)] for name in _USAGE_FIELDS
            }

            def cell(name: str, _cells=cells) -> str:
                value = _column(_cells.get(name))
                return _fmt_millions(value)

            rates = prices.get(kind, {}).get(model)
            lines.append(
                f"| {row.get('run_id', '?')} ({row.get('outcome', '?')}) "
                f"| {kind} | {model} "
                f"| {cell('input')} | {cell('cache_read')} "
                f"| {cell('cache_write')} | {cell('output')} "
                f"| {_estimate_cost(rates, cells)} |")
    lines.append("Import sessions are the importer seat's own records; "
                 "unknown counts stay unknown, never 0.")
    lines.extend(f"- {note}" for note in notes)
    return lines


def _render_providers(
    queue: dict,
    config: "FactoryConfig | None",
    *,
    factory_root: "Path | str | None" = None,
    project: str = "",
) -> "list[str]":
    """The F-59 AC-4 provider section: per kind+model token totals and
    an estimated cost from the owner-maintained price table. A missing
    price is n/a; an invalid usage count is unknown — neither is 0.
    F-67 appends the import sessions' own rows (one per recorded
    importer/repair session)."""
    rows = _provider_rows(queue)
    lines = ["## Providers", ""]
    if not rows:
        lines.append("- no provider records yet (no attempts recorded)")
        lines.extend(_render_import_sessions(factory_root, project, config))
        return lines
    lines.append("| provider | model | in | cache read | cache write | out | est. cost |")
    lines.append("|---|---|---|---|---|---|---|")
    prices = (config.prices if config is not None else {}) or {}
    for (kind, model), row in rows:
        if kind == "legacy":
            lines.append(f"| (no provider record) | — | {row.get('attempts')} "
                         "attempts before F-59 | | | | n/a |")
            continue
        rates = prices.get(kind, {}).get(model)
        lines.append(
            f"| {kind} | {model} "
            f"| {_fmt_millions(_column(row.get('input'))) } "
            f"| {_fmt_millions(_column(row.get('cache_read')))} "
            f"| {_fmt_millions(_column(row.get('cache_write')))} "
            f"| {_fmt_millions(_column(row.get('output')))} "
            f"| {_estimate_cost(rates, row)} |")
    lines.append("")
    lines.append("Estimates from the owner-maintained price table "
                 "(config.yaml `prices`); a missing price shows n/a.")
    lines.extend(_render_import_sessions(factory_root, project, config))
    return lines


def render_status(
    queue: dict,
    *,
    project: str,
    factory_root: Path | str,
    now: datetime | None = None,
    repo: Path | str | None = None,
    config: "FactoryConfig | None" = None,
) -> str:
    """Render the status page for one project.

    Args:
        queue: The parsed, validated queue document.
        project: The project name (vents are read per project).
        factory_root: The factory root holding ``progress/`` (vents) and
            ``trace/`` (sandbox escapees, F-47).
        now: The clock for the week windows; defaults to UTC now.
        repo: The project repo, for merge times and revert detection;
            unreadable history degrades to a note, never a crash.
        config: The factory config (F-59). None loads it from
            ``<factory_root>/config.yaml``; a caller that already holds
            one passes it through.

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
    # F-42 AC-5: the week's sandbox-bypass attempts, off the same week
    # window as the quota — repeated attempts to leave the sandbox are a
    # security signal the owner reads next to the spend they rode on, not
    # an outcome measure (a high count is never "productivity").
    bypass = sum(_bypass_denials(a) for a in week_attempts)
    lines += ["", "## Sandbox bypass attempts this week", ""]
    lines.append(
        f"{bypass} Bash call(s) setting dangerouslyDisableSandbox, denied by "
        "the seats' PreToolUse hook (each lands in the run's trace; the "
        "sandbox is not negotiable)"
    )

    # F-47 (I-9): processes that escaped a sandboxed command's process
    # group and were force-closed by the drain — read from the run traces
    # (the store the callers write ``sandbox_escapee`` events to; no second
    # store). Same week window; per project, with the run ids, because
    # each one is a process that may still hold the seat token.
    escapees, escapee_runs = _week_escapees(factory_root, project, horizon,
                                            notes)
    lines += ["", "## Sandbox escapees this week", ""]
    lines.append(
        f"{escapees} force-closed sandbox escapee(s) — a process that left "
        "its command's process group and outlived the kill, holding the "
        "seat token while it lives (consider rotating it)"
        + (f": {', '.join(escapee_runs)}" if escapee_runs else "")
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
    # -- Providers (F-59 AC-4) -------------------------------------------
    if config is None:
        try:
            config = load_config(Path(factory_root) / CONFIG_FILE)
        except ConfigError as exc:
            notes.append(f"factory config unreadable, no cost estimates: {exc}")
            config = None
    lines += [""] + _render_providers(
        queue, config, factory_root=factory_root, project=project)
    if notes:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in notes]
    lines.append("")
    # F-43 (m-16): the rendered page is what the owner's terminal (and the
    # dashboard) executes as well as reads — strip at this, the one render
    # seam, not at write: the progress log keeps what was stored. Vents
    # were already seam-scrubbed at the flush; this covers anything else
    # that reaches the page.
    return strip_control_sequences("\n".join(lines))
