"""``factory run`` / ``factory loop`` — the orchestrator (BUILD-ORDER F-10).

One run:

    reclaim orphans → pick → [lease + in_progress] → clone → branch →
    round: implementer session → orchestrator commit → clean clone →
           L0 + L1 (every project command sandboxed) → L2 reviewer session
           FAIL → findings into the progress log, next round (≤ max_attempts)
           PASS → PR body (gated) → push the run's own branch → gh pr create
    → in_review | needs_human | ready (quota / peak window / crash)
    → cleanup_run on every run dir, on every outcome

``loop`` repeats runs until the queue is empty, a quota error, the peak
window, or an escalation. **The factory never merges** — merging is the
owner's act.

Trust boundaries, each enforced in code rather than prose:

* **Project commands are untrusted code.** L0 and L1 run the profile's
  commands and the contract test refs, which execute the agent's
  ``conftest.py`` and tests. They run on a fresh clone of the validated
  head, through :func:`sandboxed_runner` (``run_sandboxed`` under the F-05
  settings for that clone) — never in the orchestrator's environment.
* **The agent owns its clone, including its refs.** The orchestrator reads
  HEAD after the session, commits leftovers with hardened git, points *its*
  branch at the result and pushes exactly that ref (``factory.publish``):
  never the default branch, never a ref the run didn't create. Every
  orchestrator-side git call carries ``GIT_HARDENING``.
* **Model text is not evidence.** Evidence, Door, Blast Radius and How to
  revert are built from validator facts (``factory.prbody``); the
  implementer's account is rendered as inert fenced text below them.
* **Fresh sessions get the task's progress log** (``factory.progress``),
  entries 1..N, read-only, from outside the clone.

Escalation rules (VALIDATOR-SPEC "Rounds and escalation"): L0's
credential, protected-path, sentinel-failure, new-dependency and
risk-path hits escalate immediately — retrying can't make them safe (a
credential found by L0 sits in the run branch's LOCAL history: nothing
is pushed before a passing round, but a passing retry would push that
earlier commit). A failed round goes back to the implementer
with numbered findings until ``budget.max_attempts`` attempts (counted
across runs; ``aborted`` ones excluded) are spent, then ``needs_human`` with
``attempts_exhausted``. A FAIL the next round cannot act on escalates
``review_not_converging`` instead of spending another round (F-23: re-review
scopes to the delta since the last real review, repeats are carried rather
than re-raised, and the fix prompt says to fix the defect class, not the
instance; the stop fires when every finding is a carried repeat from this
run on untouched code, or everything was dropped as out of scope). A
quota/429 error is not the task's fault: the attempt is recorded ``aborted``
with escalation ``quota_exhausted``, the task re-queues (``ready``) and
``loop`` stops without retrying.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from factory.config import CONFIG_FILE, FactoryConfig, in_peak_window, load_config
from factory.contract import load_profile
from factory.gates import CommandResult, diff_name_status, git_rc
from factory.gates import git as hardened_git
from factory.l0 import run_l0
from factory.l1 import run_l1
from factory.l2 import L2Finding, apply_convergence, parse_verdict
from factory.mergeorder import admission, gh_branch_protection, gh_pr_state
from factory.prbody import PRBodyError, build_pr_body, check_pr_body
from factory.progress import ProgressLog
from factory.prompts import (
    assemble_implementer_prompt,
    assemble_reviewer_prompt,
    prompt_versions,
)
from factory.publish import PublishError, PullRequest, PushRefused, gh_pr_create, push_branch
from factory.runs import (
    FACTORY_ROOT_DEFAULT,
    GIT_HARDENING,
    RunDir,
    RunError,
    cleanup_run,
    create_run,
    new_run_id,
)
from factory.sandbox import build_settings, run_sandboxed, write_settings
from factory.session import DEFAULT_BASE_URL, SessionRecord, run_session_sandboxed
from factory.state import (
    RECOVERY_ESCALATIONS,
    RunInProgressError,
    TaskQueue,
    now_iso,
    recovery_task_eligible,
    unmet_dependencies,
)

__all__ = [
    "LoopResult",
    "PullRequest",
    "RunResult",
    "reclaim_runs",
    "run_loop",
    "run_once",
    "sandboxed_runner",
]

SessionRunner = Callable[..., SessionRecord]
OpenPR = Callable[[PullRequest], str]

# A failed session whose reason names HTTP 429, a rate limit or a quota.
# (?<!\d)429(?!\d), not \b429\b: the runner's reason is "api_error_429",
# and "_" is a word character.
_QUOTA = re.compile(r"(?<!\d)429(?!\d)|rate[ _-]?limit|quota", re.IGNORECASE)

# L0 escalation classes, most severe first (one escalation per attempt).
# credential_added leads: the model provider saw the token (rotate it), and
# a passing retry would push the branch whose local history still contains
# the credential commit — nothing is pushed at find time.
_L0_ESCALATION_ORDER = (
    "credential_added", "protected_path_touched", "sentinel_failed",
    "new_dependency", "risk_path_touched",
)

# Caps on what travels into prompts and the PR body (GitHub refuses bodies
# over 65,536 characters). The full reports stay on disk.
_PR_OUTPUT_TAIL = 1500
_PR_NOTES_CAP = 8000
_PR_BODY_LIMIT = 65_000
_REVIEW_OUTPUT_TAIL = 6000
_REVIEW_DIFF_CAP = 200_000

_IDENTITY = ("-c", "user.name=Software Factory", "-c", "user.email=factory@localhost",
             "-c", "commit.gpgsign=false")


@dataclass
class RunResult:
    """The outcome of one ``run``.

    ``outcome``: ``idle`` (nothing eligible), ``pr_opened``,
    ``needs_human``, ``quota_exhausted``, ``peak_window`` (no session was
    started, or the run stopped before its next session),
    ``concurrency_wait`` (F-26: the repo's open-PR limit is taken, the
    ready task waits untouched), or ``error`` (the orchestrator itself
    failed mid-round; the attempt is recorded, the task is re-queued, and
    a persistent failure escalates via the budget).
    """

    outcome: str
    task_id: str | None = None
    pr: str | None = None
    escalation: str | None = None
    reports: dict = field(default_factory=dict)
    # F-26 round-4 finding 3: a fail-closed concurrency_wait carries its
    # cause — the PR URLs GitHub reports open, and those whose state
    # couldn't be resolved (presumed open) — so a stall can't look like a
    # healthy idle tick. Empty for every other outcome.
    open_prs: list[str] = field(default_factory=list)
    unresolved_prs: list[str] = field(default_factory=list)


@dataclass
class LoopResult:
    """Why ``loop`` stopped, plus each run's result.

    ``stop_reason``: ``queue_empty`` | ``quota_exhausted`` | ``peak_window``
    | ``escalation`` | ``concurrency_wait`` (F-26: the repo's open-PR
    limit is taken) | ``error``.
    """

    stop_reason: str  # queue_empty | concurrency_wait | quota_exhausted |
    #                 peak_window | escalation | error
    results: list = field(default_factory=list)


# ------------------------------------------------------------ the sandbox --


def sandboxed_runner(
    run_dir: Path, settings_path: Path, *, srt_bin_path: Path | None = None
):
    """A gate command runner that executes every command under srt.

    The returned callable has :func:`factory.gates.run_command`'s signature
    and refuses to run anywhere but ``run_dir`` — the clean clone the
    settings at ``settings_path`` confine.
    """
    run_dir = Path(run_dir)

    def run(name: str, command: str, repo: Path, timeout_sec: int) -> CommandResult:
        if Path(repo).resolve() != run_dir.resolve():
            raise RuntimeError(
                f"sandboxed runner is bound to {run_dir}, refusing to run in {repo}"
            )
        try:
            proc = run_sandboxed(
                ["/bin/sh", "-c", command], run_dir, settings_path,
                timeout=timeout_sec, srt_bin_path=srt_bin_path,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.output or "") + (exc.stderr or "")
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            return CommandResult(name, command, None, output, timed_out=True)
        return CommandResult(name, command, proc.returncode, proc.stdout + proc.stderr)

    return run


def _gate_settings(profile: dict, clean: RunDir, factory_root: Path) -> None:
    """srt settings for the gate commands: the clean clone only, no token.

    The model endpoint is on the allowlist only because ``build_settings``
    always adds it; the gate environment carries no credential for it.
    """
    settings = build_settings(
        profile, clean.root, model_endpoint=DEFAULT_BASE_URL, factory_root=factory_root
    )
    write_settings(settings, clean.settings_path)


# ------------------------------------------------------------- housekeeping --

_RUN_ID = r"\d{8}T\d{6}Z-[0-9a-f]{6}"


def reclaim_runs(queue: TaskQueue, project: str, factory_root: Path) -> list[Path]:
    """Remove run dirs (and their sandbox files) no live run lease holds.

    One lease guards one queue, and every run dir of the project is
    created while that lease is held. So if the lease is free, no session
    of this project is alive and every run dir under
    ``<factory_root>/runs/<project>/`` is an orphan of a killed or crashed
    run. If the lease is held, nothing is touched.

    Returns:
        The run dirs removed (empty while a run is live).
    """
    factory_root = Path(factory_root)
    runs = factory_root / "runs" / project
    sandbox = factory_root / "sandbox"
    removed: list[Path] = []
    try:
        lease = queue.idle_lease()
        lease.__enter__()
    except RunInProgressError:
        return []
    try:
        if runs.is_dir():
            for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
                cleanup_run(RunDir(project, run_dir.name, run_dir,
                                   sandbox / f"{project}-{run_dir.name}.json"))
                removed.append(run_dir)
        if sandbox.is_dir():
            stray = re.compile(rf"^{re.escape(project)}-{_RUN_ID}\.json(-runner|-scratch)?$")
            for entry in sorted(sandbox.iterdir()):
                if stray.match(entry.name):
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
    finally:
        lease.__exit__(None, None, None)
    return removed


# ---------------------------------------------------------------- helpers --


def _branch_name(task_id: str, run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-") or "task"
    return f"factory/{slug}-{run_id.lower()}"


def _is_quota(record: SessionRecord) -> bool:
    return record.status != "completed" and bool(_QUOTA.search(record.reason or ""))


def _tokens(record: SessionRecord | None, key: str) -> int:
    usage = (record.tokens if record else None) or {}
    value = usage.get(key) or 0
    return int(value) if isinstance(value, (int, float)) else 0


def _compact(report: dict | None, tail: int) -> dict | None:
    """A report dict with each command output cut to its last ``tail`` chars."""
    if report is None:
        return None
    report = json.loads(json.dumps(report))
    for command in report.get("commands") or []:
        out = command.get("output") or ""
        if len(out) > tail:
            command["output"] = f"[... {len(out) - tail} chars cut ...]\n" + out[-tail:]
    return report


def _numbered_findings(reports: dict, extra: list[str],
                       include_dropped: bool = True) -> list[str]:
    """The round's numbered finding lines.

    ``include_dropped=False`` builds the implementer's fix list: dropped
    findings are out of scope by the orchestrator's own ruling — telling
    the implementer to "fix the defect class" of one would pull edits
    into code the round ruled untouchable, and those edits would put the
    file into the next delta. The full list (default) is the round's
    record: log, PR evidence, the human's read.
    """
    lines: list[str] = list(extra)
    for layer in ("L0", "L1", "L2"):
        report = reports.get(layer) or {}
        for finding in report.get("findings") or []:
            where = ""
            if finding.get("file"):
                where = f" ({finding['file']}:{finding.get('line') or ''})"
            lines.append(f"[{layer} {finding.get('id')}] {finding.get('text')}{where}")
        if not include_dropped:
            continue
        # Dropped L2 findings ride the record marked, not actionable: the
        # log (and the human reading it) sees what the reviewer flagged
        # even though the orchestrator ruled it out of scope.
        for entry in report.get("dropped") or []:
            finding = entry.get("finding") or {}
            lines.append(
                f"[{layer} dropped {finding.get('id')}] {finding.get('text')} "
                f"({finding.get('file') or 'no file'}; out of scope, no "
                "missed_earlier — for the record, not to fix)"
            )
    return [f"{i}. {text}" for i, text in enumerate(lines, start=1)]


def _fix_findings(reports: dict, extra: list[str], l2, reviewed: bool,
                  last_valid_l2: dict | None) -> list[str]:
    """The next round's fix list: actionable findings only.

    Dropped findings are excluded (out of scope — fixing their defect
    class would pull edits into code the round ruled untouchable, and
    those edits would put the file into the next delta).

    Whenever this round produced no verdict of its own — the review
    failed to parse, **or L2 never ran because L0/L1 stopped the round** —
    the last valid review's findings are still the open work and are
    substituted for this round's L2 lines, keeping the fix list and the
    reviewer's carried list describing the same findings. A round whose
    review session ran but produced no verdict also prepends an explicit
    note: the implementer must hear that the change went unreviewed, not
    silently replay the older findings as if they had been accepted.
    """
    if reviewed:
        return _numbered_findings(reports, extra, include_dropped=False)
    if l2 is not None:  # a review ran, but produced no verdict
        extra = [
            "[L2 unreviewed] this round's review produced no verdict — the "
            "change was not reviewed; do not assume it was accepted",
            *extra,
        ]
    fix_reports = {**reports, "L2": last_valid_l2}
    return _numbered_findings(fix_reports, extra, include_dropped=False)


# The stop's note comes from the classifier's reason, never re-derived
# here: if apply_convergence grows a third branch, the note follows it.
_NO_PROGRESS_NOTES = {
    "carried_repeats": (
        "every L2 finding is a carried repeat from an earlier round of "
        "this run, on files untouched since: another round has nothing "
        "new to fix"
    ),
    "all_dropped": (
        "every L2 finding was dropped as out of scope; the declared fail "
        "stands with nothing left to fix"
    ),
}


def _implementer_prompt(task: dict, slice_: dict | None, branch: str, history: str,
                        attempt: int, max_attempts: int,
                        fix_findings: list[str] | None = None) -> str:
    contract = {
        "acceptance": task.get("acceptance", []),
        "out_of_scope": task.get("out_of_scope", []),
    }
    parts = [
        f"# Task {task['id']}: {task.get('title', '')}",
        "",
        f"The spec lives in the repo at `{task.get('source', '')}` — read it.",
    ]
    if slice_:
        parts += ["", f"Build only slice `{slice_['id']}`: {slice_.get('text', '')}"]
    parts += [
        "",
        "## The validation contract (approved, frozen)",
        "",
        "```json",
        json.dumps(contract, indent=2, ensure_ascii=False),
        "```",
        "",
        "## How this is judged",
        "",
        "- Every `test` check names a test by its exact node id. Write it there, red "
        "first: L1 runs it on the base commit with only your test files (it must "
        "fail) and on your branch (it must pass).",
        "- L0 runs the project's commands on a clean clone; an L2 reviewer reads the "
        "contract and the diff, never your account of it.",
        f"- You are on branch `{branch}` in this directory. Commit as you go if you "
        "like; the orchestrator commits whatever the working tree holds when you stop.",
        "- Never push, never run `gh`, never touch protected paths.",
        "",
        f"This is attempt {attempt} of {max_attempts}.",
    ]
    if fix_findings:
        parts += [
            "",
            "## Findings to fix",
            "",
            *fix_findings,
            "",
            "Each finding above names one instance of a defect class. Fix the class, "
            "not the instance: search the codebase for sibling sites with the same "
            "defect class and fix them in this round too. The next review "
            "explicitly checks the siblings.",
        ]
    if history:
        parts += [
            "",
            "## Progress log (read-only; what earlier attempts did and why they failed)",
            "",
            history,
        ]
    return "\n".join(parts) + "\n"


def _review_prompt(task: dict, diff: str, l0: dict, l1: dict, delta: str | None = None,
                   carried: list[dict] | None = None) -> str:
    contract = {
        "id": task["id"],
        "title": task.get("title"),
        "acceptance": task.get("acceptance", []),
        "out_of_scope": task.get("out_of_scope", []),
        "risk": task.get("risk", []),
    }
    if len(diff) > _REVIEW_DIFF_CAP:
        diff = diff[:_REVIEW_DIFF_CAP] + f"\n[... diff cut at {_REVIEW_DIFF_CAP} chars ...]\n"
    parts = [
        "Review this change. Return only the verdict JSON your instructions define.",
        "",
        "## Contract",
        "",
        "```json",
        json.dumps(contract, indent=2, ensure_ascii=False),
        "```",
        "",
        "## L0 report",
        "",
        "```json",
        json.dumps(_compact(l0, _REVIEW_OUTPUT_TAIL), indent=2, ensure_ascii=False),
        "```",
        "",
        "## L1 report (red_reasons: how each contract test went red on base)",
        "",
        "```json",
        json.dumps(_compact(l1, _REVIEW_OUTPUT_TAIL), indent=2, ensure_ascii=False),
        "```",
    ]
    if delta is None and not carried:
        parts += ["", "## Diff (base...head)", "", "```diff", diff, "```", ""]
        return "\n".join(parts)
    # A fix round (F-23): findings from earlier rounds are listed so the
    # reviewer carries them instead of re-raising them, and the delta (when
    # the implementer didn't rewrite the reviewed history) is the primary
    # scope with the full diff as reference. Without a delta — the history
    # was rewritten, or the last review never produced a verdict — the
    # full diff stays the scope and nothing is out of it.
    if delta is not None and len(delta) > _REVIEW_DIFF_CAP:
        delta = delta[:_REVIEW_DIFF_CAP] + f"\n[... delta cut at {_REVIEW_DIFF_CAP} chars ...]\n"
    carried_lines = [
        f"- [{c.get('severity')}] {c.get('file') or '(no file)'}: {c['text']}"
        for c in carried or []
    ] or ["- (none stood in this run's earlier rounds)"]
    parts += [
        "",
        "## Findings from earlier rounds (already recorded — do not re-raise)",
        "",
        "These were raised in an earlier round and are already recorded. Do not "
        "re-raise them as new findings; if the changes below changed the code "
        "they concern and they still stand, raise them again and say what "
        "changed. The implementer was told to fix each finding's defect class, "
        "not just the named instance — explicitly check the sibling sites of "
        "every finding below.",
        "",
        *carried_lines,
    ]
    if delta is None:
        parts += ["", "## Diff (base...head)", "", "```diff", diff, "```", ""]
        return "\n".join(parts)
    parts += [
        "",
        "## Delta since the last review (primary scope)",
        "",
        "A new finding on a file outside this delta must carry `missed_earlier`: "
        "why the earlier round missed it. Without that, the orchestrator drops "
        "the finding as out of scope.",
        "",
        "```diff",
        delta,
        "```",
        "",
        "## Full diff (base...head, reference only)",
        "",
        "```diff",
        diff,
        "```",
        "",
    ]
    return "\n".join(parts)


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Whether ``ancestor`` is reachable from ``descendant`` in ``repo``.

    rc 0 = yes, rc 1 = no; anything else — including the object being
    absent from a ``--filter=blob:none`` clone after the implementer
    amended or reset the round it names — also reads as no, because the
    caller's fallback (full-diff review scope) is always safe. Runs
    through the hardened, timeout-bounded :func:`factory.gates.git_rc`.
    """
    return git_rc(repo, "merge-base", "--is-ancestor", ancestor, descendant) == 0


def _round_stuck(l0, l1, l2) -> bool:
    """Whether the no-progress stop applies to this round (F-23).

    It is not enough that ``l2.no_progress`` is set: the stop may only
    spend a round the deterministic gates passed. Today L2 runs solely
    after L0 and L1 pass, but that ordering is a property of the wiring,
    not of the data — a future advisory L1 or a reordered pipeline would
    otherwise escalate `review_not_converging` on a round whose L0/L1
    findings are fresh, actionable work.
    """
    return bool(
        l2 is not None
        and l2.no_progress
        and l0 is not None
        and l0.verdict == "pass"
        and l1 is not None
        and l1.verdict == "pass"
    )


def _git_out(repo: Path, *args: str) -> str:
    return hardened_git(repo, *args).strip()


def _commit_work(
    work: RunDir, branch: str, base_sha: str, message: str
) -> tuple[str | None, str | None]:
    """Commit the working tree, point the run's branch at HEAD, check out it.

    Returns:
        ``(head_sha, None)``, or ``(None, finding)`` when the agent rewrote
        history so the head no longer descends from the base.
    """
    hardened_git(work.root, "add", "-A")
    staged = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(work.root), "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if staged.returncode == 1:
        hardened_git(work.root, *_IDENTITY, "commit", "-q", "--no-verify", "-m", message)
    head = _git_out(work.root, "rev-parse", "--verify", "HEAD^{commit}")
    ancestor = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(work.root), "merge-base", "--is-ancestor",
         base_sha, head],
        capture_output=True,
    )
    if ancestor.returncode != 0:
        return None, (
            f"history_rewritten: HEAD {head[:12]} does not descend from the base "
            f"{base_sha[:12]}; the round is void"
        )
    hardened_git(work.root, "update-ref", f"refs/heads/{branch}", head)
    hardened_git(work.root, "checkout", "-q", "--force", branch)
    return head, None


# -------------------------------------------------------------- the run --


@dataclass
class _Ctx:
    queue: TaskQueue
    task_id: str
    profile: dict
    factory_root: Path
    session_runner: SessionRunner
    open_pr: OpenPR
    config: FactoryConfig
    now: Callable[[], datetime]
    srt_bin_path: Path | None
    gate_timeout_sec: int


def run_once(
    queue_path: Path | str,
    profile_path: Path | str | None = None,
    *,
    factory_root: Path | str | None = None,
    session_runner: SessionRunner | None = None,
    open_pr: OpenPR | None = None,
    config: FactoryConfig | None = None,
    now: Callable[[], datetime] | None = None,
    srt_bin_path: Path | None = None,
    gate_timeout_sec: int = 600,
    protection: Callable | None = None,
    pr_state: Callable | None = None,
) -> RunResult:
    """Pick the next task and take it to a PR, an escalation or a stop.

    Args:
        queue_path: The project's ``.factory/tasks.json``.
        profile_path: Its ``project.yaml`` (default: next to the queue).
        factory_root: Default ``~/factory``: runs, sandbox settings,
            progress logs, reports and ``config.yaml`` live there.
        session_runner: Seat runner with ``run_session_sandboxed``'s
            signature (the default). Tests inject stub seats.
        open_pr: Opens the PR and returns its URL (default
            :func:`factory.publish.gh_pr_create`).
        config: Factory config (default: ``<factory_root>/config.yaml``).
        now: Clock for the peak-window checks (aware datetimes).
        srt_bin_path: srt override for tests (still pin-checked).
        gate_timeout_sec: Per-command cap for L0/L1.
        protection: Branch-protection seam (default
            :func:`factory.mergeorder.gh_branch_protection`); F-26 tests
            inject a fake. The admission check runs before the queue is
            even read: a repo whose merges can't be ordered starts
            nothing.
        pr_state: Open-PR seam (default
            :func:`factory.mergeorder.gh_pr_state`); the stale-base sweep
            and the open-PR limit follow GitHub's truth, not the queue's.

    Returns:
        The :class:`RunResult`.

    Raises:
        Whatever the orchestrator itself hits (git, PR body gate, gh). The
        task goes back to ``ready`` and every run dir is removed first.
    """
    queue_path = Path(queue_path)
    profile_path = Path(profile_path) if profile_path else queue_path.parent / "project.yaml"
    profile = load_profile(profile_path)
    factory_root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    config = config if config is not None else load_config(factory_root / CONFIG_FILE)
    now = now or (lambda: datetime.now(timezone.utc))
    admission(profile, protection or gh_branch_protection)
    pr_state = pr_state or gh_pr_state

    queue = TaskQueue(queue_path)
    reclaim_runs(queue, profile["name"], factory_root)
    queue.reclaim_abandoned()
    queue.demote_changed_contracts()
    pick = queue.pick()
    if in_peak_window(now(), config.peak_window):
        # Round-2 finding 8: a drained queue is done whatever the clock
        # says — idle/queue_empty, never peak_window — and the window
        # still gates the sweep itself (round-1 finding 5): no gate work,
        # no gh lookups, no re-validation escalations inside it.
        return RunResult("idle" if pick is None else "peak_window",
                         task_id=pick.task["id"] if pick else None)
    # Round-3 findings 1+7: the sweep runs on EVERY off-peak run, with or
    # without pickable work (AC-3's "every run" — F-27 waiting on F-26's
    # open PR must still see it go stale or closed), and an idle tick
    # resolves only the open-PR (in_review) links plus the
    # recovery-eligible needs_human ones (round-4 finding 1: the owner's
    # GitHub action must be readable even when nothing is pickable) — a
    # build-up of other old links adds no gh round-trips. The eligible
    # set is computed ONCE and handed to the sweep (round-5 finding 6):
    # one scan, and the URLs resolved are exactly the set swept.
    recovery = _recovery_tasks(queue)
    states = _resolve_pr_states(list(dict.fromkeys(
        _pr_urls(queue, ("in_review",), skip_merged=True)
        + [task["links"]["pr"] for task in recovery])), pr_state)
    stale = _sweep_stale_prs(
        queue, profile, factory_root=factory_root,
        srt_bin_path=srt_bin_path, gate_timeout_sec=gate_timeout_sec,
        states=states, recovery=recovery,
    )
    if stale is not None:
        return stale
    if pick is None:
        return RunResult("idle")
    task_id = pick.task["id"]
    # F-26 AC-2: at most `repo.concurrency` (default 1) open factory PRs
    # per repo — counted on GitHub's word, not the queue's statuses: an
    # escalated task's PR stays open (round-1 finding 3), and a task whose
    # PR was merged or closed frees its slot even before reconcile. The
    # picked task's own PR counts too (round-4 finding 4, superseding
    # round-2 finding 1's exclusion): a fix round whose rejected PR is
    # still open would open a second factory PR and orphan the first —
    # exactly what this limit exists to prevent — so it waits, naming the
    # PR, until you close the rejected one. A failed lookup is presumed
    # open — the limit fails closed (round-3 finding 2) and the wait
    # names the unresolvable link (round-4 finding 3); the count's extra
    # links are resolved only now that a task is picked.
    pending = [url for url in _pr_urls(queue, _PR_STATES_STATUSES,
                                       skip_merged=True)
               if url not in states]
    states.update(_resolve_pr_states(pending, pr_state))
    open_urls, unresolved_urls = _count_open_prs(queue, states)
    limit = (profile.get("repo") or {}).get("concurrency", 1)
    if len(open_urls) + len(unresolved_urls) >= limit:
        return RunResult("concurrency_wait", task_id=task_id,
                         open_prs=open_urls, unresolved_prs=unresolved_urls)

    ctx = _Ctx(
        queue=queue, task_id=task_id, profile=profile, factory_root=factory_root,
        session_runner=session_runner or run_session_sandboxed,
        open_pr=open_pr or gh_pr_create, config=config, now=now,
        srt_bin_path=srt_bin_path, gate_timeout_sec=gate_timeout_sec,
    )
    with queue.begin_run(task_id):
        try:
            recovered = _finish_unrecorded_publish(queue, task_id, pick.slice)
            if recovered is not None:
                return recovered
            return _run_task(ctx, pick.slice)
        except BaseException:
            # The run didn't reach a recorded end state: re-queue it (the
            # aborted-run edge) so the queue isn't wedged until the next
            # reclaim. If a PR was already opened, its pass attempt is on
            # disk and the next run's _finish_unrecorded_publish completes
            # the bookkeeping instead of opening a second PR.
            with contextlib.suppress(Exception):
                fresh = TaskQueue(queue_path)
                if fresh.get(task_id).get("status") == "in_progress":
                    fresh.transition(task_id, "ready")
            raise


def _finish_unrecorded_publish(
    queue: TaskQueue, task_id: str, slice_: dict | None
) -> RunResult | None:
    """Complete a publish whose PR opened but whose bookkeeping didn't land.

    The pass attempt, carrying the PR URL, is saved before the task's links
    and its ``in_review`` transition. If that second save failed, the task
    was re-queued with a PR already open. Its signature on disk: the slice's
    latest attempt is a ``pass`` with a ``pr`` that ``links.pr`` doesn't
    match. (A fix round after a rejected PR has ``links.pr`` equal to the
    attempt's, so it isn't caught here.)

    Returns:
        A ``pr_opened`` result after moving the task to ``in_review``, or
        None when there is nothing to recover.
    """
    task = queue.get(task_id)
    slice_id = slice_["id"] if slice_ else task_id
    ours = [a for a in task.get("attempts") or [] if a.get("slice_id") == slice_id]
    if not ours:
        return None
    last = ours[-1]
    url = last.get("pr")
    if last.get("outcome") != "pass" or not url or (task.get("links") or {}).get("pr") == url:
        return None
    task["links"] = {**(task.get("links") or {}), "pr": url}
    queue.transition(task_id, "in_review")
    return RunResult("pr_opened", task_id, pr=url)


def _fetch_sha(repo: Path, remote: str, ref: str) -> str:
    """Fetch ``ref`` from ``remote`` (full, not blob-filtered) → its sha.

    Full on purpose: the sweep's scratch clone must be able to check out
    and merge both the new default head and the PR branch, and the clone
    is a ``--filter=blob:none`` partial whose lazy blob fetches go to the
    local source repo — which may hold neither. Two refs of full objects
    cost far less than a merge that dies mid-checkout. Runs with
    GIT_TERMINAL_PROMPT=0 and a timeout (round-3 finding 3): a private
    remote with an expired credential helper fails fast instead of
    hanging the run lease on a username prompt.
    """
    try:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(repo), "fetch", "--quiet", "--no-tags",
             remote, ref],
            capture_output=True, text=True,
            env={**os.environ, **_GIT_NET_ENV}, timeout=_NET_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunError(
            f"fetch of {ref!r} from {remote} timed out after {_NET_TIMEOUT_SEC}s "
            "(GIT_TERMINAL_PROMPT=0 was set: a hung credential helper or an "
            "unreachable remote, not a prompt waiting for you)"
        ) from exc
    if proc.returncode != 0:
        raise RunError(
            f"cannot fetch {ref!r} from {remote}: {proc.stderr.strip()[:500]}. "
            "The run refuses to judge an open PR against an unknown base."
        )
    return _git_out(repo, "rev-parse", "--verify", "FETCH_HEAD")


def _slice_id_of(task: dict) -> str:
    """The slice the task's logged attempts belong to (unsliced: the id).

    Empty ``attempts`` (a hand-reconciled or migrated in_review task) is
    expected: the escalation path must survive it, not index-error.
    """
    attempts = task.get("attempts") or []
    return (attempts[-1].get("slice_id") or task["id"]) if attempts else task["id"]


def _conflict_paths(repo: Path) -> list[str]:
    """Unmerged paths after a failed merge — the locale-independent truth.

    ``git diff --name-only --diff-filter=U`` reads the index, not git's
    translated messages: a real conflict lists its files under any
    ``LANG`` (round-2 finding 2), and an infrastructure failure (missing
    objects, unrelated histories) lists none.
    """
    return [p for p in _git_out(repo, "diff", "--name-only",
                                "--diff-filter=U").splitlines() if p.strip()]


_PR_STATES_STATUSES = ("ready", "in_progress", "in_review", "needs_human")

# Remote git (ls-remote, fetches, the sweep's merge) must never sit on a
# credential prompt: a private HTTPS remote with an expired helper would
# hold the run lease forever on a username prompt (round-3 finding 3).
_GIT_NET_ENV = {"GIT_TERMINAL_PROMPT": "0"}
_NET_TIMEOUT_SEC = 300
# Round-5 finding 5: the sweep's merge is LOCAL — both commits were
# already fetched — so its timeout is its own constant and its failure
# wording names the local pathology, never the network.
_MERGE_TIMEOUT_SEC = 300


def _pr_urls(queue: TaskQueue, statuses: tuple,
             *, skip_merged: bool = False) -> list[str]:
    """Every PR URL of tasks in ``statuses`` (unique, queue order).

    Resolution is lazy by design (round-3 finding 7): the sweep needs
    only ``in_review`` links (plus the recovery-eligible needs_human
    ones); the count's remaining links are resolved only once a task is
    actually picked, so idle ticks don't pay for a build-up of old
    links. ``skip_merged`` drops ``links.merged`` tasks (round-6
    finding 2): merged is terminal — stamped once by the sweep, never
    gh-polled or counted again — so a merged park adds no per-run call.
    """
    urls: list[str] = []
    for task in queue.doc.get("tasks", []):
        if task.get("status") not in statuses:
            continue
        if skip_merged and (task.get("links") or {}).get("merged"):
            continue
        url = (task.get("links") or {}).get("pr")
        if url and url not in urls:
            urls.append(url)
    return urls


def _recovery_tasks(queue: TaskQueue) -> list[dict]:
    """needs_human tasks the sweep can recover (round-4 finding 1).

    Eligibility is state.recovery_task_eligible — the same predicate the
    gated transitions enforce — so the writer can never widen the F-26
    recovery edges on its own.
    """
    return [task for task in queue.doc.get("tasks", [])
            if recovery_task_eligible(task)]


def _resolve_pr_states(urls: list[str], pr_state: Callable) -> dict:
    """One ``pr_state`` lookup per URL; a failed lookup is None, never a
    raise (round-2 finding 4: a rotten link stops neither the run nor
    the queue — the run proceeds; round-3 finding 2 decides what None
    *means* per consumer).

    ``None`` is distinct from every GitHub answer: the concurrency limit
    treats it as open (fail closed — a permission check never fails
    open), while the sweep skips the task (advisory re-validation is not
    judged on an unknown state).
    """
    states: dict[str, str | None] = {}
    for url in urls:
        try:
            states[url] = pr_state(url)
        except PublishError:
            states[url] = None
    return states


def _count_open_prs(queue: TaskQueue, states: dict) -> tuple[list[str], list[str]]:
    """(open PR URLs, unresolvable PR URLs) per the run's resolved states.

    Statuses whose PR can still be open: ``in_review`` (awaiting merge),
    ``needs_human`` (escalated — the PR stays open until you reject it)
    and ``in_progress``/``ready`` around a fix round (GitHub answers
    either way). Post-merge statuses are excluded: ``in_review →
    deploying`` means the PR merged. Nothing is excluded for the picked
    task (round-4 finding 4, superseding round-2 finding 1): its own PR
    counts while GitHub reports it open, so a fix round whose rejected
    PR was never closed waits — naming the PR — instead of opening a
    second factory PR and orphaning the first. An unknown state (a
    failed gh lookup) counts as open too — the limit fails closed
    (round-3 finding 2) — and is returned separately so the stall can
    name its cause (round-4 finding 3).
    """
    open_urls: list[str] = []
    unresolved_urls: list[str] = []
    for task in queue.doc.get("tasks", []):
        if task.get("status") not in _PR_STATES_STATUSES:
            continue
        links = task.get("links") or {}
        url = links.get("pr")
        if not url or url in open_urls or url in unresolved_urls:
            continue
        if links.get("merged"):
            continue  # stamped terminal (round-6 finding 2): not open
        if states.get(url, None) is None:
            unresolved_urls.append(url)
        elif states[url] == "open":
            open_urls.append(url)
    return open_urls, unresolved_urls


def _ls_remote_heads(remote: str, refs: tuple[str, ...]) -> dict[str, str]:
    """``{refname: sha}`` for the named refs via one ``git ls-remote``.

    An optimization seam only (round-2 finding 7): on any failure —
    including a timeout — the answer is empty and the caller falls
    through to the clone path, whose fetches remain the authoritative
    check. Runs with GIT_TERMINAL_PROMPT=0 and a timeout so it can never
    sit on a credential prompt (round-3 finding 3).
    """
    try:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "ls-remote", remote,
             *(f"refs/heads/{ref}" for ref in refs)],
            capture_output=True, text=True,
            env={**os.environ, **_GIT_NET_ENV}, timeout=_NET_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {}
    if proc.returncode != 0:
        return {}
    heads: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, _, ref = line.partition("\t")
        heads[ref.removeprefix("refs/heads/")] = sha
    return heads


def _sweep_escalation(
    queue: TaskQueue, factory_root: Path, profile: dict, task_id: str, *,
    escalation: str, note: str, run_id: str,
    report_path: Path | None = None, reports: dict | None = None,
) -> RunResult:
    """Record one sweep escalation and move the task to needs_human.

    The one builder for every sweep path (round-3 finding 5): the
    escalated attempt, its progress-log entry and the transition are one
    shape, so a future required attempt field lands in one place, not
    three — a schema refusal inside the escalation path is the failure
    this hoists out.
    """
    attempt = {
        "run_id": run_id,
        "slice_id": _slice_id_of(queue.get(task_id)),
        "started": now_iso(),
        "ended": now_iso(),
        "outcome": "escalated",
        "rounds": 0,
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "prompt_versions": dict(prompt_versions(profile)),
        "skills": [],  # setting_sources=[]: no session ran
        "escalation": escalation,
        "note": note,
    }
    if report_path is not None:
        attempt["validator_report"] = str(report_path)
    findings = _numbered_findings(
        reports if reports is not None else {"L0": None, "L1": None, "L2": None},
        [f"[{escalation}] {note}"],
    )
    queue.append_attempt(task_id, attempt)
    ProgressLog.for_task(factory_root, profile["name"], task_id).append({
        "run_id": run_id, "outcome": "escalated",
        "escalation": escalation, "findings": findings,
    })
    queue.transition(task_id, "needs_human")
    return RunResult("needs_human", task_id, escalation=escalation, reports=reports)


def _sweep_stale_prs(
    queue: TaskQueue, profile: dict, *, factory_root: Path,
    srt_bin_path: Path | None, gate_timeout_sec: int, states: dict,
    recovery: list[dict],
) -> RunResult | None:
    """F-26 AC-3: bring every open factory PR's readiness up to date.

    For each task ``in_review`` with PR and branch links, fetch the
    remote's default-branch head and the PR branch into a scratch clone.
    When the PR branch already contains the default head there is nothing
    to do. Otherwise the PR is stale: merge it onto the new base and
    re-run L0 and L1 on the merged tree — locally proving the merge a
    strict branch-protection setting (or the ``branch_protection:
    unavailable`` self-enforcement, AC-1) would force before it happens.
    A failure — merge conflict or gates — escalates ``stale_base``: the
    PR was only ever validated on the base it was built on. A pass is
    recorded in the progress log; the task stays ``in_review``, its
    readiness now proven against the current default head. Runs
    unconditionally: for a strictly-protected repo this merely front-runs
    what GitHub enforces at merge time.

    Round-4 finding 1: before any of that, the sweep also applies the
    F-26 recovery edges from GitHub's PR state — one of the two writers
    they have (reconcile skips non-draft tasks; tasks.json is never
    hand-edited; the other is `factory recover`). A re-opened or merged
    escalated PR goes back to ``in_review`` (a merged one waits there
    for the F-14 deploy gate like every merged factory PR, stamped
    terminal — round-5 finding 4 + round-6 findings 1+2); a closed
    stale_base PR was your rejection — the task goes straight to
    ``ready`` (or ``blocked`` when a dependency is open — round-6
    finding 4) for its fix round (round-5 finding 3: no lease-less
    ``in_progress`` hop for the next run's reclaim to clean up). A
    pr_closed task's PR was
    already closed at escalation, so the sweep reads no new intent from
    it — its fix-round word is ``factory recover``.

    Returns:
        A ``needs_human`` result when a stale PR failed re-validation, or
        None (all open PRs up to date, or none open).

    Raises:
        RunError: The remote (or the PR branch on it) can't be fetched.
    """
    repo_cfg = profile["repo"]
    project = profile["name"]
    source = Path(repo_cfg["path"]).expanduser()
    default_branch = repo_cfg["default_branch"]
    for task in recovery:
        state = states.get(task["links"]["pr"])
        escalation = task["attempts"][-1].get("escalation")
        if state == "merged":
            # Round-5 finding 4 + round-6 findings 1+2: you resolved the
            # conflict on GitHub and merged — the task returns to
            # in_review, exactly where every merged factory PR waits for
            # the F-14 deploy gate (nothing in F-26 advances a merged
            # task; reconcile skips non-draft tasks by design). The
            # stamp makes merged terminal: later runs never gh-poll the
            # link, count it, or re-validate it.
            new_status = "in_review"
            task["links"]["merged"] = True
            note = (f"GitHub shows {task['links']['pr']} merged: "
                    f"{escalation} recovery — back to in_review, where "
                    f"every merged PR waits in in_review for the F-14 "
                    f"deploy gate")
        elif state == "open" and escalation == "pr_closed":
            new_status = "in_review"
            note = (f"GitHub shows {task['links']['pr']} open again: "
                    f"{escalation} recovery — {task['id']} → in_review")
        elif state == "closed" and escalation == "stale_base":
            # Round-6 finding 4: the fix round lands where approval
            # would — blocked when a dependency is open, never a
            # silently-unpickable ready.
            new_status = (
                "blocked" if unmet_dependencies(task, queue.doc.get("tasks", []))
                else "ready"
            )
            note = (f"GitHub shows {task['links']['pr']} closed (your "
                    f"rejection): {escalation} recovery — {task['id']} → "
                    f"{new_status} for a fix round")
        else:
            continue  # no owner action to read
        queue.transition(task["id"], new_status)
        ProgressLog.for_task(factory_root, project, task["id"]).append({
            "run_id": new_run_id(),
            "outcome": "recovered",
            "note": note,
        })
    open_prs = [
        task for task in queue.doc.get("tasks", [])
        if task.get("status") == "in_review"
        and (task.get("links") or {}).get("pr")
        and (task.get("links") or {}).get("branch")
        and not (task.get("links") or {}).get("merged")
    ]
    if not open_prs:
        return None
    for task in open_prs:
        task_id = task["id"]
        links = task["links"]
        state = states.get(links["pr"])
        if state == "merged":
            # A shipped PR: re-validating it would fail L1's red-first
            # proof on code that already landed, and its head branch may
            # be auto-deleted. Round-6 findings 1+2: the task waits in
            # in_review for the F-14 deploy gate (nothing here advances
            # it), and the stamp makes merged terminal — later runs
            # never gh-poll this link again.
            links["merged"] = True
            queue.save()
            continue
        if state == "closed":
            # Round-2 finding 5: closed without merging — surface the
            # task instead of stranding it silently in in_review. The
            # run id is a real generated one (round-3 finding 4): no
            # session ran, but audit joins by run_id must not collide
            # across tasks or escalations.
            return _sweep_escalation(
                queue, factory_root, profile, task_id,
                escalation="pr_closed",
                note=(f"GitHub reports the PR closed without merging: "
                      f"{links['pr']} — re-open it on GitHub and the "
                      f"next run's sweep moves this task back to "
                      f"in_review"),
                run_id=new_run_id(),
            )
        if state != "open":
            continue  # unknown (gh failed): not ours to judge or to wedge on
        branch = links["branch"]
        # Round-2 finding 7: ls-remote decides the cache hit before any
        # scratch clone or full fetch. Refinement only — the clone path
        # below remains the authoritative check.
        heads = _ls_remote_heads(repo_cfg["remote"], (default_branch, branch))
        cached = links.get("revalidated")
        if (cached and heads.get(branch) == cached["pr"]
                and heads.get(default_branch) == cached["default"]):
            continue  # cached pair unchanged on the remote: nothing to redo
        work = create_run(project, source, factory_root=factory_root,
                          ref=default_branch)
        try:
            default_sha = _fetch_sha(work.root, repo_cfg["remote"], default_branch)
            pr_sha = _fetch_sha(work.root, repo_cfg["remote"], branch)
            if git_rc(work.root, "merge-base", "--is-ancestor",
                      default_sha, pr_sha) == 0:
                # Up to date — and that conclusion is cached too (round-3
                # finding 6): same pair ⇒ same ancestry, so later ticks
                # skip at the ls-remote pre-check without cloning.
                if links.get("revalidated") != {"pr": pr_sha,
                                                "default": default_sha}:
                    links["revalidated"] = {"pr": pr_sha, "default": default_sha}
                    queue.save()
                continue  # the PR branch already contains the default head
            if links.get("revalidated") == {"pr": pr_sha, "default": default_sha}:
                continue  # already re-validated against exactly this base

            # Stale: merge the PR onto the new base; a conflict is itself
            # a re-validation failure (the merge a human would attempt).
            hardened_git(work.root, "checkout", "-q", "--detach", pr_sha)
            try:
                # LC_ALL=C (deterministic note text), prompts disabled as
                # defense in depth: the scratch clone is blobless, and an
                # unexpectedly missing blob would trigger a lazy fetch
                # from the local source — prompt-free keeps even that
                # path safe. Conflicts are read from the index, not from
                # git's localized messages (round-2 finding 2); any
                # other failure is infrastructure, not the PR.
                merge = subprocess.run(
                    ["git", *GIT_HARDENING, "-C", str(work.root), *_IDENTITY,
                     "merge", "--no-edit", default_sha],
                    capture_output=True, text=True,
                    env={**os.environ, "LC_ALL": "C", "LANG": "C",
                         **_GIT_NET_ENV},
                    timeout=_MERGE_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired as exc:
                # Round-4 finding 5 + round-5 finding 5: a timeout is a
                # clean RunError (exit 2, message), never a traceback —
                # and this merge is local, so the message names the local
                # pathology, not credentials or the network.
                raise RunError(
                    f"git merge of {branch} onto "
                    f"{default_branch}@{default_sha[:12]} timed out after "
                    f"{_MERGE_TIMEOUT_SEC}s — a local merge (both commits "
                    "were already fetched): pathological merge or disk "
                    "problem, not the network"
                ) from exc
            reports: dict = {"L0": None, "L1": None, "L2": None}
            note: str | None = None
            merged_sha: str | None = None
            if merge.returncode != 0:
                conflicts = _conflict_paths(work.root)
                if not conflicts:
                    raise RunError(
                        f"git merge of {branch} onto {default_branch}@"
                        f"{default_sha[:12]} failed with no unmerged paths "
                        f"(infrastructure, not the PR): "
                        f"{(merge.stdout + merge.stderr).strip()[:300]}"
                    )
                note = (
                    f"merge conflict: {branch} does not merge onto "
                    f"{default_branch}@{default_sha[:12]} (conflicts in: "
                    f"{', '.join(conflicts)}; git: "
                    f"{merge.stdout.strip()[:200]})"
                )
            else:
                merged_sha = _git_out(work.root, "rev-parse", "--verify", "HEAD")
                clean = create_run(project, work.root, factory_root=factory_root,
                                   ref=merged_sha)
                try:
                    _gate_settings(profile, clean, factory_root)
                    runner = sandboxed_runner(clean.root, clean.settings_path,
                                              srt_bin_path=srt_bin_path)
                    live = queue.get(task_id)
                    l0 = run_l0(profile, clean.root, default_sha, merged_sha,
                                task=live, runner=runner, timeout_sec=gate_timeout_sec)
                    reports["L0"] = l0.to_dict()
                    l1 = None
                    if l0.verdict == "pass" and not l0.escalations:
                        l1 = run_l1(profile, clean.root, default_sha, merged_sha,
                                    task=live, runner=runner,
                                    timeout_sec=gate_timeout_sec)
                        reports["L1"] = l1.to_dict()
                    if l0.verdict != "pass":
                        note = f"L0 failed on {branch} merged onto {default_sha[:12]}"
                    elif l0.escalations:
                        note = ("L0 escalations on the merged tree: "
                                + ", ".join(l0.escalations))
                    elif l1 is None or l1.verdict != "pass":
                        note = f"L1 failed on {branch} merged onto {default_sha[:12]}"
                finally:
                    cleanup_run(clean)

            reports_dir = (factory_root / "reports" / project
                           / re.sub(r"[^\w.-]", "_", task_id))
            reports_dir.mkdir(parents=True, exist_ok=True)
            report_path = reports_dir / f"{work.run_id}-revalidate.json"
            report_path.write_text(
                json.dumps(reports, indent=2, ensure_ascii=False) + "\n")

            if note is not None:
                return _sweep_escalation(
                    queue, factory_root, profile, task_id,
                    escalation="stale_base", note=note, run_id=work.run_id,
                    report_path=report_path, reports=reports,
                )
            # Pass: no attempt (no session ran, no budget spent); the
            # readiness record is the log entry plus the stored report,
            # and links.revalidated caches the pair so an unchanged base
            # is not re-paid on every later run.
            queue.get(task_id)["links"]["revalidated"] = {
                "pr": pr_sha, "default": default_sha}
            queue.save()
            ProgressLog.for_task(factory_root, project, task_id).append({
                "run_id": work.run_id, "outcome": "pass",
                "note": (f"stale-base re-validation: L0+L1 pass on {branch} "
                         f"merged onto {default_branch}@{default_sha[:12]} "
                         f"({report_path.name})"),
            })
        finally:
            cleanup_run(work)
    return None


def _build_on_remote_default(work: RunDir, remote: str, default_branch: str) -> None:
    """Point the work clone at the remote's default branch before anything runs.

    The clone comes from the local checkout, whose ``main`` can be days
    behind the remote (JobFinderOS readiness run, 2026-10-18: local
    ``b0e2417`` against remote ``9daedd1``). Building on it would give the
    agent stale code and the PR a wrong base. So the run fetches the
    remote's default branch with hardened git and detaches there. If the
    remote can't be fetched, the run refuses: an unknown base is worse
    than no run.

    Raises:
        RunError: The fetch failed or timed out. Raised before any
            session starts.
    """
    try:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(work.root), "fetch", "--quiet",
             "--no-tags", "--filter=blob:none", remote, default_branch],
            capture_output=True, text=True,
            env={**os.environ, **_GIT_NET_ENV}, timeout=_NET_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        # Round-4 finding 5: a slow remote is the usual exit-2 message,
        # never a raw TimeoutExpired traceback.
        raise RunError(
            f"fetch of {default_branch!r} from {remote} timed out after "
            f"{_NET_TIMEOUT_SEC}s (GIT_TERMINAL_PROMPT=0 was set: a hung "
            "credential helper or an unreachable remote, not a prompt "
            "waiting for you)"
        ) from exc
    if proc.returncode != 0:
        raise RunError(
            f"cannot fetch {default_branch!r} from {remote}: {proc.stderr.strip()[:500]}. "
            "The run refuses to build on a possibly stale local checkout."
        )
    hardened_git(work.root, "checkout", "-q", "--detach", "FETCH_HEAD")


def _run_task(ctx: _Ctx, slice_: dict | None) -> RunResult:
    profile, queue, task_id = ctx.profile, ctx.queue, ctx.task_id
    project = profile["name"]
    repo_cfg = profile["repo"]
    default_branch = repo_cfg["default_branch"]
    source = Path(repo_cfg["path"]).expanduser()
    task = queue.get(task_id)
    slice_id = slice_["id"] if slice_ else task_id
    budget = task["budget"]
    max_attempts = budget["max_attempts"]
    used = sum(
        1 for a in task.get("attempts") or []
        if a.get("slice_id") == slice_id and a.get("outcome") != "aborted"
        # Round-5 finding 1: stale_base/pr_closed attempts are the
        # sweep's session-less records, not spent work — the recovery
        # fix round starts with its slot unspent (a stale_base
        # escalation after a passing round must not exhaust the budget
        # before the fix round's session can run).
        and a.get("escalation") not in RECOVERY_ESCALATIONS
    )
    versions = prompt_versions(profile)
    log = ProgressLog.for_task(ctx.factory_root, project, task_id)
    protected = list((profile.get("paths") or {}).get("protected") or [])
    implementer_system = assemble_implementer_prompt(profile)
    reviewer_system = assemble_reviewer_prompt(profile)
    session_seconds = 0.0

    # F-23 review-convergence state, living across this run's rounds. The
    # prior finding keys seed from the task's progress log (an earlier run
    # or a rejected-PR fix round), so a finding raised there is carried
    # here; their raise shas are gone with the old clone, hence None.
    prior: dict[str, str | None] = {}
    for entry in log.entries():
        for key in entry.get("finding_keys") or []:
            prior.setdefault(key, None)
    raised: list[dict] = []  # this run's findings, for the next round's prompt
    last_reviewed: str | None = None  # the head sha L2 last saw
    last_valid_l2: dict | None = None  # the last parseable verdict's report
    prev_findings: list[str] | None = None  # the failed round's fix list
    round_no = 0

    work = create_run(project, source, factory_root=ctx.factory_root, ref=default_branch)
    try:
        _build_on_remote_default(work, repo_cfg["remote"], default_branch)
        branch = _branch_name(task_id, work.run_id)
        created = frozenset({branch})
        hardened_git(work.root, "checkout", "-q", "-b", branch)
        base_sha = _git_out(work.root, "rev-parse", "--verify", "HEAD^{commit}")
        reports_dir = ctx.factory_root / "reports" / project / re.sub(r"[^\w.-]", "_", task_id)
        reports_dir.mkdir(parents=True, exist_ok=True)

        def attempt_record(attempt_no, started, outcome, records, report_path, **extra):
            record = {
                "run_id": work.run_id,
                "slice_id": slice_id,
                "started": started,
                "ended": now_iso(),
                "outcome": outcome,
                "rounds": attempt_no,
                "turns": sum((r.num_turns or 0) for r in records if r),
                "input_tokens": sum(_tokens(r, "input_tokens") for r in records),
                "output_tokens": sum(_tokens(r, "output_tokens") for r in records),
                "prompt_versions": dict(versions),
                "skills": [],  # setting_sources=[]: sessions load no skills
            }
            models = [f"{r.seat}={r.model}" for r in records if r and r.model]
            if models:
                record["model"] = " ".join(models)
            if report_path:
                record["validator_report"] = str(report_path)
            record.update({k: v for k, v in extra.items() if v is not None})
            return record

        def finish(attempt, status, findings, finding_keys=None, **result):
            entry = {
                "run_id": attempt["run_id"],
                "attempt": attempt["rounds"],
                "outcome": attempt["outcome"],
                "escalation": attempt.get("escalation"),
                "findings": findings,
            }
            if finding_keys:
                # F-23: the stable keys of this round's L2 findings, so the
                # next run seeds its carried set from the log.
                entry["finding_keys"] = finding_keys
            queue.append_attempt(task_id, attempt)
            log.append(entry)
            if status is not None:
                queue.transition(task_id, status)
            return result

        while True:
            attempt_no = used + 1
            started = now_iso()
            if attempt_no > max_attempts:
                attempt = attempt_record(
                    attempt_no - 1, started, "escalated", [], None,
                    escalation="attempts_exhausted",
                    note="attempt budget already spent before this run",
                )
                finish(attempt, "needs_human", ["1. attempt budget already spent"])
                return RunResult("needs_human", task_id, escalation="attempts_exhausted")
            remaining_min = budget["wall_clock_min"] - session_seconds / 60.0
            if remaining_min <= 0:
                attempt = attempt_record(attempt_no, started, "escalated", [], None,
                                         escalation="budget_exhausted")
                finish(attempt, "needs_human", ["1. wall-clock budget spent"])
                return RunResult("needs_human", task_id, escalation="budget_exhausted")
            if in_peak_window(ctx.now(), ctx.config.peak_window):
                queue.transition(task_id, "ready")
                return RunResult("peak_window", task_id)

            # This round's state, initialised before anything can raise: the
            # error path below records its attempt from whatever exists.
            round_no += 1
            records: list = []
            reports: dict = {"L0": None, "L1": None, "L2": None}
            extra_findings: list[str] = []
            l0 = l1 = l2 = None
            head_sha = None
            changed: list[str] = []
            path = None
            pushed = False
            opened = False
            round_keys: list = []  # this round's L2 finding keys (F-23)
            reviewed = False  # did this round's review produce a verdict?
            try:
                # ---- implementer
                # The fix block below carries the newest entry's findings,
                # so that entry renders without them (read once, not
                # twice — the entry itself still renders, F-10 AC-10) and
                # no entry ever renders the seeding hashes.
                prompt = _implementer_prompt(
                    task, slice_, branch,
                    log.render(omit_last_findings=prev_findings is not None),
                    attempt_no, max_attempts, fix_findings=prev_findings)
                t0 = time.monotonic()
                impl = ctx.session_runner(
                    "implementer", prompt, work.root, profile,
                    factory_root=ctx.factory_root, max_turns=budget["max_turns"],
                    timeout_min=remaining_min, protected=protected,
                    system_prompt=implementer_system, srt_bin_path=ctx.srt_bin_path,
                )
                session_seconds += time.monotonic() - t0
                records = [impl]
                if _is_quota(impl):
                    attempt = attempt_record(attempt_no, started, "aborted", records, None,
                                             escalation="quota_exhausted", note=impl.reason)
                    finish(attempt, "ready", [f"1. quota: {impl.reason}"])
                    return RunResult("quota_exhausted", task_id, escalation="quota_exhausted")

                if impl.status != "completed":
                    extra_findings.append(
                        f"[session {impl.status}] the implementer session ended: {impl.reason}"
                    )
                else:
                    head_sha, void = _commit_work(
                        work, branch, base_sha, f"{task_id}: attempt {attempt_no}"
                    )
                    if void:
                        extra_findings.append(void)

                if head_sha is not None:
                    clean = create_run(project, work.root, factory_root=ctx.factory_root,
                                       ref=head_sha)
                    try:
                        _gate_settings(profile, clean, ctx.factory_root)
                        runner = sandboxed_runner(clean.root, clean.settings_path,
                                                  srt_bin_path=ctx.srt_bin_path)
                        l0 = run_l0(profile, clean.root, base_sha, head_sha, task=task,
                                    runner=runner, timeout_sec=ctx.gate_timeout_sec)
                        reports["L0"] = l0.to_dict()
                        if l0.escalations:
                            escalation = next(e for e in _L0_ESCALATION_ORDER
                                              if e in l0.escalations)
                            if l0.risk_flags_added:
                                task["risk"] = sorted(set(task.get("risk") or [])
                                                      | set(l0.risk_flags_added))
                                task["gates"]["merge"] = "human"
                            path = _store(reports_dir, work.run_id, attempt_no, reports)
                            attempt = attempt_record(
                                attempt_no, started, "escalated", records, path,
                                escalation=escalation,
                                note="L0 escalations: " + ", ".join(l0.escalations),
                                sentinels=list(l0.sentinel_results),
                            )
                            finish(attempt, "needs_human",
                                   _numbered_findings(reports, [f"[L0 escalation] {escalation}"]))
                            return RunResult("needs_human", task_id, escalation=escalation,
                                             reports=reports)
                        if l0.verdict == "pass":
                            l1 = run_l1(profile, clean.root, base_sha, head_sha, task=task,
                                        runner=runner, timeout_sec=ctx.gate_timeout_sec)
                            reports["L1"] = l1.to_dict()
                        if l1 is not None and l1.verdict == "pass":
                            # ---- reviewer (a session start: the checks apply)
                            if in_peak_window(ctx.now(), ctx.config.peak_window):
                                # The round is validated but the window stops the
                                # next session start. The tokens and the attempt
                                # are recorded (aborted — the window is nobody's
                                # fault) and the next session is told the change
                                # existed and was discarded with the run.
                                path = _store(reports_dir, work.run_id, attempt_no, reports)
                                attempt = attempt_record(
                                    attempt_no, started, "aborted", records, path,
                                    note=f"peak window before the L2 review; the change "
                                         f"(head {head_sha[:12]}, passed L0 and L1) was "
                                         f"discarded with the run",
                                )
                                finish(attempt, "ready", [
                                    f"1. peak window stopped attempt {attempt_no} before "
                                    f"the L2 review: its change (head {head_sha[:12]}) had "
                                    f"passed L0 and L1 and was discarded with the run — "
                                    f"redo it",
                                ])
                                return RunResult("peak_window", task_id, reports=reports)
                            remaining_review = budget["wall_clock_min"] - session_seconds / 60.0
                            if remaining_review <= 0:
                                path = _store(reports_dir, work.run_id, attempt_no, reports)
                                attempt = attempt_record(
                                    attempt_no, started, "escalated", records, path,
                                    escalation="budget_exhausted",
                                    note="wall-clock budget spent before the L2 review",
                                )
                                finish(attempt, "needs_human",
                                       ["1. wall-clock budget spent before the L2 review"])
                                return RunResult("needs_human", task_id,
                                                 escalation="budget_exhausted", reports=reports)
                            diff = hardened_git(clean.root, "diff", "--no-color",
                                                "--no-ext-diff",
                                                f"{base_sha}...{head_sha}")
                            # F-23: from this run's second review on, the
                            # delta since the last reviewed sha is the
                            # reviewer's primary scope and the convergence
                            # rules (carried repeats, out-of-delta drops)
                            # apply — but only if that sha still exists in
                            # this clone: an implementer that amended or
                            # reset the reviewed round's commit makes it
                            # unreachable, and this round falls back to the
                            # full diff instead of dying on a bad object.
                            delta = delta_files = None
                            if last_reviewed is not None and _is_ancestor(
                                    clean.root, last_reviewed, head_sha):
                                delta = hardened_git(
                                    clean.root, "diff", "--no-color", "--no-ext-diff",
                                    f"{last_reviewed}...{head_sha}")
                                delta_files = frozenset(
                                    path
                                    for change in diff_name_status(
                                        clean.root, last_reviewed, head_sha)
                                    for path in (change.path, change.old_path)
                                    if path
                                )
                            t0 = time.monotonic()
                            review_prompt = _review_prompt(
                                task, diff, reports["L0"], reports["L1"],
                                delta=delta, carried=raised)
                            review = ctx.session_runner(
                                "reviewer", review_prompt,
                                clean.root, profile, factory_root=ctx.factory_root,
                                max_turns=budget["max_turns"], timeout_min=remaining_review,
                                protected=protected, system_prompt=reviewer_system,
                                srt_bin_path=ctx.srt_bin_path,
                            )
                            session_seconds += time.monotonic() - t0
                            records.append(review)
                            if _is_quota(review):
                                attempt = attempt_record(
                                    attempt_no, started, "aborted", records, None,
                                    escalation="quota_exhausted", note=review.reason,
                                )
                                finish(attempt, "ready", [f"1. quota: {review.reason}"])
                                return RunResult("quota_exhausted", task_id,
                                                 escalation="quota_exhausted", reports=reports)
                            l2 = apply_convergence(
                                parse_verdict(
                                    review.result if review.status == "completed"
                                    else None),
                                prior,
                                delta_files if delta_files is not None else frozenset(),
                                delta_mode=delta is not None,
                            )
                            if review.status != "completed":
                                extra_findings.append(
                                    f"[session {review.status}] the reviewer session ended: "
                                    f"{review.reason}"
                                )
                            reports["L2"] = l2.to_dict()
                            # Only a review that produced a parseable verdict
                            # moves the frontier. A crashed, timed-out or
                            # invalid review reviewed nothing: the next
                            # round's delta must still cover this head, or
                            # code no reviewer ever saw would become "outside
                            # the delta" and its findings dropped.
                            reviewed = (
                                review.status == "completed" and l2.parse_error is None
                            )
                            if reviewed:
                                last_valid_l2 = l2.to_dict()
                                round_keys = [
                                    f.key for f in l2.findings
                                    if isinstance(f, L2Finding) and f.key
                                ]
                                raised = [
                                    {"key": f.key, "file": f.file,
                                     "severity": f.severity, "text": f.text}
                                    for f in l2.findings if isinstance(f, L2Finding)
                                ]
                                for key in round_keys:
                                    prior[key] = head_sha
                                last_reviewed = head_sha
                        changed = [c.path for c in diff_name_status(clean.root, base_sha, head_sha)]
                    finally:
                        cleanup_run(clean)

                path = _store(reports_dir, work.run_id, attempt_no, reports)
                findings = _numbered_findings(reports, extra_findings)
                passed = all(x is not None and x.verdict == "pass" for x in (l0, l1, l2))

                if passed:
                    task = queue.get(task_id)
                    for tail in (_PR_OUTPUT_TAIL, 200, 0):  # shrink outputs to fit GitHub
                        body = build_pr_body(
                            task, profile, branch=branch, base_sha=base_sha, head_sha=head_sha,
                            changed_paths=changed,
                            reports={k: _compact(v, tail) for k, v in reports.items()},
                            l1_report=l1, extra_flags=l0.risk_flags_added,
                            implementer_notes=(impl.result or "")[:_PR_NOTES_CAP],
                            attempt=attempt_no,
                        )
                        if len(body) <= _PR_BODY_LIMIT:
                            break
                    check_pr_body(body)  # no PR without every section
                    push_branch(work.root, repo_cfg["remote"], branch, sha=head_sha,
                                created=created, default_branch=default_branch)
                    pushed = True
                    url = ctx.open_pr(PullRequest(
                        remote=repo_cfg["remote"], base=default_branch, head=branch,
                        title=f"{task_id}: {task.get('title', '')}", body=body,
                    ))
                    opened = True
                    attempt = attempt_record(attempt_no, started, "pass", records, path,
                                             pr=url, sentinels=list(l0.sentinel_results))
                    queue.append_attempt(task_id, attempt)
                    # A passed round's findings were accepted as non-blocking:
                    # recording their keys would poison a later fix round
                    # after a rejected PR (only failed reviews seed).
                    log.append({"run_id": work.run_id, "attempt": attempt_no, "outcome": "pass",
                                "pr": url, "findings": findings})
                    task = queue.get(task_id)
                    task["links"] = {**(task.get("links") or {}), "pr": url, "branch": branch}
                    queue.transition(task_id, "in_review")
                    return RunResult("pr_opened", task_id, pr=url, reports=reports)

                # F-23 no-progress stop: apply_convergence decided, where
                # the prior set was in hand, that this FAIL gives the next
                # round nothing it can fix — every finding is a carried
                # repeat from this run on untouched code (the bounce the
                # stop exists for; a carry seeded from an earlier run does
                # not count, and a repeat on a file the fix touched is an
                # attempt in progress), or every finding was dropped as
                # out of scope and the declared fail stands with nothing
                # left. Escalate instead of spending the round — and only
                # when the round's own L0/L1 passed (_round_stuck): fresh
                # deterministic findings are actionable work, whatever the
                # review repeats.
                if _round_stuck(l0, l1, l2):
                    note = _NO_PROGRESS_NOTES[l2.no_progress_reason]
                    attempt = attempt_record(
                        attempt_no, started, "escalated", records, path,
                        escalation="review_not_converging", note=note,
                        sentinels=list(l0.sentinel_results) if l0 is not None else None,
                    )
                    finish(attempt, "needs_human", findings, finding_keys=round_keys)
                    return RunResult("needs_human", task_id,
                                     escalation="review_not_converging", reports=reports)

                used += 1
                if attempt_no >= max_attempts:
                    attempt = attempt_record(
                        attempt_no, started, "escalated", records, path,
                        escalation="attempts_exhausted",
                        sentinels=list(l0.sentinel_results) if l0 is not None else None,
                    )
                    finish(attempt, "needs_human", findings, finding_keys=round_keys)
                    return RunResult("needs_human", task_id, escalation="attempts_exhausted",
                                     reports=reports)
                attempt = attempt_record(
                    attempt_no, started, "fail", records, path,
                    sentinels=list(l0.sentinel_results) if l0 is not None else None,
                )
                finish(attempt, None, findings, finding_keys=round_keys)
                prev_findings = _fix_findings(
                    reports, extra_findings, l2, reviewed, last_valid_l2)
                task = queue.get(task_id)
            except Exception as exc:
                # The orchestrator itself failed mid-round (gh after the push,
                # a gate crash, a reviewer timeout, ...). The attempt is
                # recorded so the budget is spent and a persistent failure
                # escalates instead of re-running both seats forever (PR #22
                # review finding 2). Once the branch is public the failure is
                # the owner's to resolve: needs_human with the branch kept.
                if opened:
                    # The PR is open and its pass attempt is saved; run_once
                    # re-queues and the next run finishes the bookkeeping
                    # (_finish_unrecorded_publish) without a second PR.
                    raise
                deterministic = not pushed and isinstance(exc, (PushRefused, PRBodyError))
                path = None
                with contextlib.suppress(Exception):
                    path = _store(reports_dir, work.run_id, attempt_no, reports)
                note = f"{type(exc).__name__}: {exc}"[:500]
                # A fence or PR-body refusal can't pass on retry (like L0's
                # protected-path class): escalate at once instead of
                # spending the budget re-running both seats.
                if pushed:
                    escalation = "pr_create_failed"
                elif deterministic:
                    escalation = "publish_refused"
                else:
                    escalation = "orchestrator_error"
                attempt = attempt_record(attempt_no, started, "escalated", records, path,
                                         escalation=escalation, note=note)
                findings = _numbered_findings(reports, [f"[{escalation}] {note}"])
                queue.append_attempt(task_id, attempt)
                log.append({"run_id": work.run_id, "attempt": attempt_no,
                            "outcome": attempt["outcome"], "escalation": escalation,
                            "findings": findings})
                if pushed:
                    task = queue.get(task_id)
                    task["links"] = {**(task.get("links") or {}), "branch": branch}
                    queue.transition(task_id, "needs_human")
                    return RunResult("needs_human", task_id,
                                     escalation="pr_create_failed", reports=reports)
                if deterministic:
                    queue.transition(task_id, "needs_human")
                    return RunResult("needs_human", task_id,
                                     escalation="publish_refused", reports=reports)
                queue.transition(task_id, "ready")
                return RunResult("error", task_id,
                                 escalation="orchestrator_error", reports=reports)
    finally:
        cleanup_run(work)


def _store(reports_dir: Path, run_id: str, attempt_no: int, reports: dict) -> Path:
    path = reports_dir / f"{run_id}-a{attempt_no}.json"
    path.write_text(json.dumps(reports, indent=2, ensure_ascii=False) + "\n")
    return path


# -------------------------------------------------------------- the loop --


def run_loop(queue_path: Path | str, profile_path: Path | str | None = None,
             **kwargs) -> LoopResult:
    """Run until the queue is empty, quota, the peak window, or an escalation.

    Accepts :func:`run_once`'s keyword arguments. Each run starts by
    reclaiming orphaned run dirs and abandoned ``in_progress`` tasks.
    """
    results: list[RunResult] = []
    stops = {"idle": "queue_empty", "quota_exhausted": "quota_exhausted",
             "peak_window": "peak_window", "needs_human": "escalation",
             # F-26: the open-PR limit is taken; the ready task waits.
             "concurrency_wait": "concurrency_wait",
             # A broken orchestrator stops the loop: it must not drain the
             # attempt budget unattended (each error round spends a session).
             "error": "error"}
    while True:
        result = run_once(queue_path, profile_path, **kwargs)
        results.append(result)
        if result.outcome in stops:
            return LoopResult(stops[result.outcome], results)
