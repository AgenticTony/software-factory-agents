"""``factory run`` / ``factory loop`` — the orchestrator (BUILD-ORDER F-10).

One run:

    reclaim orphans → pick → [lease + in_progress] → clone → branch →
    round: implementer session → orchestrator commit → clean clone →
           L0 + L1 (every project command sandboxed) → L2 review fan-out:
           N reviewer sessions in parallel (config, default 2) over the
           same diff → verifier session votes the merged findings in/out
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
* **Sessions can vent** (F-27, ``factory.vent``): each seat's
  ``vent(category, text)`` calls are flushed into the task's progress log
  the moment the session returns — telemetry for `factory status` (F-11)
  and the F-19 review, never part of the attempt record or the budget.

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
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

from factory.config import CONFIG_FILE, FactoryConfig, in_peak_window, load_config
from factory.contract import load_profile
from factory.egress import scrub_session_text, scrub_session_value
from factory.gates import CommandResult, diff_name_status, git_rc
from factory.gates import git as hardened_git
from factory.l0 import run_l0
from factory.l1 import run_l1
from factory.l2 import (
    L2Finding,
    apply_convergence,
    apply_vote,
    final_review_verdict,
    merge_reviews,
    parse_reviews_with_retry,
    parse_verifier_vote,
)
from factory.mergeorder import admission, gh_branch_protection, gh_pr_state
from factory.prbody import PRBodyError, build_pr_body, check_pr_body
from factory.progress import ProgressLog
from factory.prompts import (
    assemble_implementer_prompt,
    assemble_reviewer_prompt,
    assemble_verifier_prompt,
    prompt_versions,
)
from factory.publish import (
    GIT_NET_ENV,
    PublishError,
    PullRequest,
    PushRefused,
    RemoteCallTimeout,
    check_pr_text,
    gh_pr_create,
    push_branch,
    remote_call,
)
from factory.runs import (
    FACTORY_ROOT_DEFAULT,
    GIT_HARDENING,
    RUN_ID_RE,
    RunDir,
    RunError,
    cleanup_run,
    create_run,
    new_run_id,
)
from factory.sandbox import (
    build_settings,
    escapee_event,
    run_sandboxed,
    write_settings,
)
from factory.session import (
    DEFAULT_BASE_URL,
    SessionRecord,
    probe_session_interpreter,
    run_session_sandboxed,
    run_sessions_parallel,
    seat_token,
)
from factory.state import (
    RunInProgressError,
    TaskQueue,
    attempt_budget_used,
    now_iso,
    open_owner_steps,
    recovery_task_eligible,
    unmet_dependencies,
)
from factory.toolkit import resolve_toolkit
from factory.trace import TraceWriter, prune_traces
from factory.vent import flush_vents

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
# F-38 review finding 3: the smallest per-session turn cap a review seat
# may start with. Below it the fan-out / verifier refuses to start and the
# round escalates budget_exhausted — a review that cannot take a tool turn
# and answer is not a review.
_REVIEW_TURN_FLOOR = 2

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
    # F-26 AC-3 report (round-7 finding 2): every open PR this run's
    # sweep proved current — up to date with, or re-validated on, the
    # remote default head — as ``<url> against <branch>@<sha12>``. Set on
    # every outcome reached after the sweep; empty when it didn't run.
    ready_prs: list[str] = field(default_factory=list)


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


def _create_gate_venv(run_dir: Path) -> Path:
    """Create ``<run_dir>/.venv`` from the orchestrator's base interpreter.

    Gate commands (setup, lint, test) must resolve ``pip``/``python``
    inside the run clone, never an ambient environment: run
    20261029T145356Z-b1875d's setup resolved the owner's terminal venv,
    "satisfied" its requirements from the main checkout's site-packages,
    tried to uninstall its ``bin/factory`` until the sandbox blocked it,
    and the failed setup skipped every remaining gate. The venv uses the
    PEP 405 shape ``runs._relocate_venv`` expects of a run-local venv
    (``pyvenv.cfg`` at the root, ``bin/`` beside it) — created in place,
    so no cache relocation applies — and from ``sys._base_executable``
    (the interpreter behind the orchestrator's own venv, if any), so a
    venv-of-venv never chains through the owner's checkout.

    The venv is anchored in ``.git/info/exclude`` (idempotently — one
    ``/.venv/`` line however often creation re-runs): the gates diff
    refs, but they run commands on the checkout, and L0 refuses a tree
    that isn't exactly ``head_ref`` — an untracked ``.venv`` would read
    as dirty. ``.git/info`` is sandbox-denyWritten, so the session can't
    undo it (same anchoring ``create_run`` seeds for ``.home``/``.tmp``).

    A ``.venv`` that already exists in the clone is REFUSED (review
    round on #74): an implementer can ``git add -f .venv/…``, and
    ``python -m venv`` over an existing directory keeps its
    site-packages — the gates would run planted code. The refusal fires
    at construction, before a single gate command executes; L0 flags the
    tracked paths one layer below.

    Raises:
        RunError: venv creation failed (no partial dir is kept), or a
            ``.venv`` already exists in the clone (planted environment);
            the run cannot gate hermetically, so it refuses to gate.
    """
    venv = run_dir / ".venv"
    if venv.exists():
        raise RunError(
            f"refusing to create the gate venv: {venv} already exists in "
            "the clone — a committed .venv is planted environment "
            "(git add -f), never factory state; the run is refused before "
            "any gate command executes"
        )
    base = Path(getattr(sys, "_base_executable", sys.executable))
    try:
        proc = subprocess.run(
            [str(base), "-m", "venv", str(venv)],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(venv, ignore_errors=True)
        raise RunError(f"gate venv creation in {venv} failed: {exc}") from exc
    if proc.returncode != 0:
        shutil.rmtree(venv, ignore_errors=True)
        raise RunError(
            f"gate venv creation in {venv} failed (exit {proc.returncode}): "
            f"{(proc.stdout + proc.stderr).strip()[:300]}"
        )
    exclude = run_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if "/.venv/" not in lines:
        with exclude.open("a") as fh:
            fh.write("/.venv/\n")
    return venv


def sandboxed_runner(
    run_dir: Path, settings_path: Path, *, srt_bin_path: Path | None = None,
    on_escapee: "Callable[[dict], None] | None" = None,
):
    """A gate command runner that executes every command under srt.

    The returned callable has :func:`factory.gates.run_command`'s signature
    and refuses to run anywhere but ``run_dir`` — the clean clone the
    settings at ``settings_path`` confine. Every command runs with a
    run-local venv (``<run_dir>/.venv``, :func:`_create_gate_venv`) first
    on PATH and ``VIRTUAL_ENV`` set, so the profile's ``pip install``
    lands in the run — never the owner's venv or a host env.

    ``on_escapee`` (F-47, I-9): where a command whose drain force-closed
    pipes (a process escaped the group and outlived the kill) is
    reported — one :func:`~factory.sandbox.escapee_event` labelled
    ``gate:<name>``, on the normal and the timeout path alike. The run
    passes its trace writer's ``append``; with no sink (a caller that
    owns no trace, e.g. the eval) the event is printed to stderr — never
    silently dropped.
    """
    run_dir = Path(run_dir)
    venv = _create_gate_venv(run_dir)

    def report(name: str, outcome, timed_out: bool) -> None:
        streams = getattr(outcome, "force_closed_pipes", ()) or ()
        if not streams:
            return
        event = escapee_event(f"gate:{name}", streams, timed_out=timed_out)
        if on_escapee is not None:
            on_escapee(event)
        else:
            print(f"factory: sandbox escapee — {json.dumps(event)} "
                  f"(run dir {run_dir}; no trace to record it in)",
                  file=sys.stderr)

    def run(name: str, command: str, repo: Path, timeout_sec: int) -> CommandResult:
        if Path(repo).resolve() != run_dir.resolve():
            raise RuntimeError(
                f"sandboxed runner is bound to {run_dir}, refusing to run in {repo}"
            )
        try:
            proc = run_sandboxed(
                ["/bin/sh", "-c", command], run_dir, settings_path,
                timeout=timeout_sec, srt_bin_path=srt_bin_path, venv=venv,
            )
        except subprocess.TimeoutExpired as exc:
            report(name, exc, True)
            output = (exc.output or "") + (exc.stderr or "")
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            return CommandResult(name, command, None, output, timed_out=True)
        report(name, proc, False)
        return CommandResult(name, command, proc.returncode, proc.stdout + proc.stderr)

    return run


def _gate_settings(profile: dict, clean: RunDir, factory_root: Path) -> None:
    """srt settings for the gate commands: the clean clone only, no token.

    The model endpoint is on the allowlist only because ``build_settings``
    always adds it; the gate environment carries no credential for it.

    This is the one seat allowed to request local binding when the
    profile opts in (``sandbox.allow_local_binding``, review decision on
    #76): the L0/L1 commands and the stale-base re-validation clone that
    share these settings run the project's fixed test suite — port
    binding is exactly what server tests do. The session seats never get
    it: srt's macOS rules would also hand them outbound connect to every
    host loopback port (see build_settings' docstring).
    """
    settings = build_settings(
        profile, clean.root, model_endpoint=DEFAULT_BASE_URL,
        factory_root=factory_root,
        local_binding=bool((profile.get("sandbox") or {}).get("allow_local_binding")),
    )
    write_settings(settings, clean.settings_path)


# ------------------------------------------------------------- housekeeping --

_RUN_ID = RUN_ID_RE.pattern


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
            # Every suffix either WO creates hangs off the run's own
            # settings name: -runner/-scratch/-toolkit (F-06/F-09/F-22) and
            # the F-38 fan-out's per-session spellings (-r1.json and its
            # own -runner/-toolkit/-scratch siblings). The run id is
            # unique, so anything past the base name is this run's
            # artifact (the same argument cleanup_run's glob makes).
            stray = re.compile(
                rf"^{re.escape(project)}-{_RUN_ID}\.json(-[\w.-]+)?$"
            )
            for entry in sorted(sandbox.iterdir()):
                if stray.match(entry.name):
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
        # Traces outlive their runs but not the retention window: they
        # hold repo content (PII on some projects), so they are aged out
        # here, under the same lease that says no run can be appending.
        prune_traces(factory_root, project)
    finally:
        lease.__exit__(None, None, None)
    return removed


# ---------------------------------------------------------------- helpers --


def _branch_name(task_id: str, run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-") or "task"
    return f"factory/{slug}-{run_id.lower()}"


def _log_vents(log: ProgressLog, record: SessionRecord, seat: str,
               run_id: str, task_id: str, secrets: Sequence[str] = ()) -> None:
    """Flush a session's vents into the task's progress log (F-27).

    Telemetry, never control flow (AC-3, review finding 5): a flush that
    fails — a broken log chain, a full disk — costs the vents, reported on
    stderr, never the attempt record or the run. The orchestrator stamps
    the run, task and seat it actually ran; the record's own claims about
    them are untrusted (they crossed the sandbox boundary). F-43: the
    flush hands over the run's ``secrets`` (the seat token), so the vent
    text passes the egress seam again outside the sandbox — the in-sandbox
    collector already scrubbed with the same token.
    """
    try:
        flush_vents(log, record.vents, run_id=run_id, task_id=task_id,
                    seat=seat, secrets=secrets)
    except Exception as exc:  # noqa: BLE001 — telemetry must not crash a run
        print(
            f"factory: {task_id} {seat} vents not logged "
            f"({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )


def _progress_best_effort(log: ProgressLog, entry: dict, task_id: str) -> None:
    """Append one progress-log entry as telemetry, never control flow
    (F-45 AC-2/AC-4, M-6).

    The queue and the attempt records are the state machine; the progress
    log is the human-facing projection of them (the same rule
    :func:`_log_vents` already applies to vents). An append that fails —
    a full disk (OSError) or a rewritten/corrupt hash chain
    (ProgressLogError, an anticipated class) — costs one log line, which
    is reported on stderr, never the transition it sits next to: before
    F-45, a failing append in ``_sweep_escalation`` left the task in
    ``in_review`` with its attempt recorded, and every later run
    re-validated, failed at the same append and crashed again — an
    unbudgeted, self-perpetuating wedge (the escalation's note and
    findings live on in the attempt record and the stored validator
    report, so the projection is reconstructable).
    """
    try:
        log.append(entry)
    except Exception as exc:  # noqa: BLE001 — telemetry must not crash a run
        print(
            f"factory: {task_id} progress log entry not written "
            f"({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )


def _is_quota(record: SessionRecord) -> bool:
    return record.status != "completed" and bool(_QUOTA.search(record.reason or ""))


def _tokens(record: SessionRecord | None, key: str) -> int:
    usage = (record.tokens if record else None) or {}
    value = usage.get(key) or 0
    return int(value) if isinstance(value, (int, float)) else 0


def _turns_spent(records: list) -> int:
    """Turns the attempt's sessions have actually spent so far (F-38
    review finding 3): the implementer included — the fan-out shares the
    REMAINDER of the task's turn budget, so every session's spend must be
    counted before the next one is capped."""
    total = 0
    for record in records:
        turns = getattr(record, "num_turns", None) if record else None
        if isinstance(turns, (int, float)) and not isinstance(turns, bool):
            total += int(turns)
    return total


class RetryFit(NamedTuple):
    """One reviewer re-run's budget fit: the turns and wall-clock
    minutes it may have, or — when the attempt's budget cannot fit one —
    ``refusal`` naming WHICH budget refused (the note a refused seat
    gets must name its refuser). Exactly one of the two shapes is
    meaningful: a fit carries no refusal, a refusal no numbers."""

    turns: int | None
    wall: float | None
    refusal: str | None = None


def _retry_budget(
    budget: dict, records: list, session_seconds: float, reviewer_share: int
) -> RetryFit:
    """The turns and wall-clock minutes ONE reviewer re-run may have, or
    the refusal that denies it (PR #90 rounds 3-5: ONE source of truth
    for the fit and the refusal's name — the two can never drift).

    The fan-out's own invariant is that its sessions' summed turns never
    exceed ``budget["max_turns"]``; a re-run joins that invariant, so it
    is capped at ``min(its per-seat share, what actually remains)`` and
    refused below :data:`_REVIEW_TURN_FLOOR` — the same floor at which
    the fan-out itself refuses to start. Since round 4 the re-run also
    RESERVES the verifier's floor: it may only take turns that leave at
    least :data:`_REVIEW_TURN_FLOOR` for the verifier pass after it, so
    a flaky reviewer can never escalate (budget_exhausted ->
    needs_human) a task that would otherwise have failed a normal
    round. The wall clock is recomputed from ``session_seconds`` per
    re-run (re-runs are sequential; reusing the pre-fan-out window would
    let N invalid seats spend N windows) and must be positive, the same
    fit the review path itself requires — the existing verifier fit
    check has no positive wall floor (only ``<= 0``), so none is
    reserved here.
    """
    remaining_turns = budget["max_turns"] - _turns_spent(records)
    turns = min(reviewer_share, remaining_turns - _REVIEW_TURN_FLOOR)
    remaining_wall = budget["wall_clock_min"] - session_seconds / 60.0
    if remaining_wall <= 0:
        return RetryFit(None, None, "wall-clock budget")
    if turns < _REVIEW_TURN_FLOOR:
        return RetryFit(None, None, "turn budget")
    return RetryFit(turns, remaining_wall)


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
    file into the next delta. The same goes for the F-38 verifier's drops:
    a voted-out finding is noise by the review's own record, not work.
    The full list (default) is the round's record: log, PR evidence, the
    human's read.
    """
    lines: list[str] = list(extra)
    # F-41: a round that ran the final review stores its delta review as
    # "L2-delta" (empty findings by then — it passed — but its drops are
    # the record's to keep, exactly like the L2 list).
    for layer in ("L0", "L1", "L2", "L2-delta"):
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
        # F-38: the verifier's drops ride the record the same way — the
        # reviewer noise the vote removed, with the reason, never as work.
        for entry in report.get("verifier_dropped") or []:
            finding = entry.get("finding") or {}
            lines.append(
                f"[{layer} voted out {finding.get('id')}] {finding.get('text')} "
                f"({entry.get('reason')}) — for the record, not to fix)"
            )
    return [f"{i}. {text}" for i, text in enumerate(lines, start=1)]


def _review_drops(l2) -> list | None:
    """The attempt record's ``review_drops`` (F-38 AC-3): the verifier's
    and the nit budget's drops, flattened to finding fields + reason —
    the reviewer noise the verifier removed, where F-19 can count it.
    Absent fields are omitted (the schema has no nullable keys); None
    when nothing was dropped (the field stays absent, not empty)."""
    if l2 is None or not getattr(l2, "verifier_dropped", None):
        return None
    drops = []
    for entry in l2.verifier_dropped:
        finding = entry.get("finding") or {}
        drop = {"text": finding.get("text"), "reason": entry.get("reason")}
        for key in ("id", "axis", "severity", "file", "line", "reported_by"):
            if finding.get(key) is not None:
                drop[key] = finding[key]
        drops.append(drop)
    return drops


def _final_review_field(l2, repeat: bool) -> dict:
    """The attempt record's ``final_review`` summary (F-41 AC-5): the
    final full review's verdict, its findings, and whether it was a
    repeat after a failed one — the attempt record's own proof the final
    review ran, alongside the trace's ``L2-final`` phase. Absent fields
    are omitted (the schema has no nullable keys); a parse-error round
    carries its verdict with no findings, the parse_error naming why."""
    findings = []
    for finding in l2.findings:
        if not isinstance(finding, L2Finding):
            continue
        entry = {"text": finding.text}
        for key in ("id", "axis", "severity", "file", "line", "reported_by"):
            if getattr(finding, key, None) is not None:
                entry[key] = getattr(finding, key)
        findings.append(entry)
    return {"verdict": l2.verdict, "repeat": repeat, "findings": findings}


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
        "every L2 finding was dropped — out of scope, or voted out by "
        "the verifier — and the declared fail stands with nothing left "
        "to fix"
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


def _verifier_prompt(task: dict, diff: str, union: list, delta: str | None = None) -> str:
    """The verifier session's user prompt (F-38): the contract, the diff
    the reviewers saw, and the union of their findings — one entry per
    merged finding, carrying which reviewers reported it. The verifier
    reads the code itself (its seat is read-only in the same clone); this
    prompt hands it the claims to check, nothing else."""
    contract = {
        "id": task["id"],
        "title": task.get("title"),
        "acceptance": task.get("acceptance", []),
        "out_of_scope": task.get("out_of_scope", []),
    }
    findings = [
        {
            "id": f.id,
            "axis": f.axis,
            "severity": f.severity,
            "file": f.file,
            "line": f.line,
            "text": f.text,
            "reported_by": f.reported_by,
        }
        for f in union
        if isinstance(f, L2Finding)
    ]
    if len(diff) > _REVIEW_DIFF_CAP:
        diff = diff[:_REVIEW_DIFF_CAP] + f"\n[... diff cut at {_REVIEW_DIFF_CAP} chars ...]\n"
    parts = [
        "Vote on these review findings. Return only the vote JSON your "
        "instructions define.",
        "",
        "## Contract",
        "",
        "```json",
        json.dumps(contract, indent=2, ensure_ascii=False),
        "```",
    ]
    if delta is not None:
        if len(delta) > _REVIEW_DIFF_CAP:
            delta = (
                delta[:_REVIEW_DIFF_CAP]
                + f"\n[... delta cut at {_REVIEW_DIFF_CAP} chars ...]\n"
            )
        parts += [
            "",
            "## Delta since the last review (what changed since the "
            "reviewers looked)",
            "",
            "```diff",
            delta,
            "```",
        ]
    parts += [
        "",
        "## Full diff (base...head)",
        "",
        "```diff",
        diff,
        "```",
        "",
        "## Findings to verify (merged across the reviewer fan-out)",
        "",
        "```json",
        json.dumps(findings, indent=2, ensure_ascii=False),
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
    sdk_probe: Callable | None = None,
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
        sdk_probe: Session-interpreter probe seam (default: auto —
            :func:`factory.session.probe_session_interpreter` when real
            sessions would run, i.e. the session runner is the default;
            None when tests inject stub seats). Called with the profile
            once a task is picked, just before the session starts; a
            raising probe refuses the run before anything is recorded.

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
    # Before the pick, and needing no gh round-trip: close merged tasks
    # on a no-deploy profile (links.merged is stamped terminal, so this
    # also unstucks tasks merged before the fix), then re-route
    # ready/blocked against the dependencies those closes just met —
    # sync_readiness is F-03's own reconciliation, wired here so a run
    # sees the post-merge queue instead of idling on it.
    _close_merged_tasks(queue, profile, factory_root)
    queue.sync_readiness()
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
    ready: list[str] = []
    # F-45 AC-1 (M-5): the sweep creates run dirs (its scratch and clean
    # clones), so it runs under the run lease — reclaim_runs trusts the
    # invariant "every run dir under runs/<project>/ is created or deleted
    # only while the run lease is held", and the sweep was the one writer
    # that escaped it: a concurrent invocation's reclaim deleted a live
    # sweep's clones mid-flight. Under the lease a concurrent invocation
    # refuses here (RunInProgressError, the lease's normal message)
    # instead of racing. Nothing inside the sweep re-takes the lease (the
    # queue's writes serialise on their own lock), and the sweep's dirs
    # are cleaned up in its own finally blocks before the lease releases.
    with queue.idle_lease():
        stale = _sweep_stale_prs(
            queue, profile, factory_root=factory_root,
            srt_bin_path=srt_bin_path, gate_timeout_sec=gate_timeout_sec,
            states=states, recovery=recovery, ready=ready, config=config,
        )
    if stale is not None:
        stale.ready_prs = ready
        return stale
    if pick is None:
        return RunResult("idle", ready_prs=ready)
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
                         open_prs=open_urls, unresolved_prs=unresolved_urls,
                         ready_prs=ready)

    # The F-31 incident (run 20261029T130912Z-1e4eb9): a venv interpreter
    # whose own files the sandbox denied started as its bare base, and
    # the session entry died on ModuleNotFoundError one full session too
    # late — after the attempt was spent. One sandboxed import probe,
    # here and only here — a picked task is about to spawn a session, so
    # only a picked run pays the probe: idle ticks spawn no sandbox, and
    # a broken interpreter never blocks the sweep or the no-deploy close
    # pass. Still before anything is recorded or leased: an unrunnable
    # interpreter refuses the run as a clean environment error and spends
    # no attempt. Automatic only when real sessions would run (tests
    # inject stub seats and no probe); an explicit probe seam always runs.
    probe = sdk_probe or (
        probe_session_interpreter
        if session_runner in (None, run_session_sandboxed) else None
    )
    if probe is not None:
        probe(profile, srt_bin_path=srt_bin_path)

    ctx = _Ctx(
        queue=queue, task_id=task_id, profile=profile, factory_root=factory_root,
        session_runner=session_runner or run_session_sandboxed,
        open_pr=open_pr or gh_pr_create, config=config, now=now,
        srt_bin_path=srt_bin_path, gate_timeout_sec=gate_timeout_sec,
    )
    with queue.begin_run(task_id):
        try:
            result = (_finish_unrecorded_publish(queue, task_id, pick.slice)
                      or _run_task(ctx, pick.slice))
            result.ready_prs = ready
            return result
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
    GIT_TERMINAL_PROMPT=0 and a timeout (round-3 finding 3; since F-45
    through :func:`factory.publish.remote_call`, the ONE wrapper every
    remote call shares): a private remote with an expired credential
    helper fails fast instead of hanging the run lease on a username
    prompt.
    """
    try:
        proc = remote_call(
            ["git", *GIT_HARDENING, "-C", str(repo), "fetch", "--quiet", "--no-tags",
             remote, ref],
            what=f"fetch of {ref!r} from {remote}",
        )
    except RemoteCallTimeout as exc:
        raise RunError(str(exc)) from exc
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

# Remote git (ls-remote, fetches) must never sit on a credential prompt:
# a private HTTPS remote with an expired helper would hold the run lease
# forever on a username prompt (round-3 finding 3). Since F-45 every
# remote call — these and the publish-side push/ls-remote/gh — goes
# through factory.publish.remote_call (ONE prompt-disabled, timed
# wrapper); the env and timeout constants live there.
# Round-5 finding 5: the sweep's merge is LOCAL — both commits were
# already fetched — so its timeout is its own constant and its failure
# wording names the local pathology, never the network.
_MERGE_TIMEOUT_SEC = 300


def _is_merged(links: dict) -> bool:
    """Whether ``links.pr`` is the PR GitHub reported merged.

    ``links.merged`` holds the merged PR's URL, not a flag (round-7
    finding 1): publishing keeps a task's other links, so a boolean
    stamp would carry over to the task's next PR (a rebuilt task, a
    later slice), which then would never be polled, counted or
    re-validated. A new PR on the task is simply not merged.
    """
    return bool(links.get("pr")) and links.get("merged") == links.get("pr")


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
        if skip_merged and _is_merged(task.get("links") or {}):
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
        if _is_merged(links):
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
    check. Runs prompt-disabled and timed through the shared
    :func:`factory.publish.remote_call` wrapper (round-3 finding 3, F-45)
    so it can never sit on a credential prompt.
    """
    try:
        proc = remote_call(
            ["git", *GIT_HARDENING, "ls-remote", remote,
             *(f"refs/heads/{ref}" for ref in refs)],
            what=f"ls-remote of {remote}",
        )
    except RemoteCallTimeout:
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
    config: FactoryConfig | None = None,
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
        # The prompt versions in force (no session ran): the fan-out shape
        # matches what a session-bearing attempt of the same run records,
        # so F-19's credit assignment reads one consistent ledger.
        "prompt_versions": dict(prompt_versions(
            profile,
            reviewers=(config or FactoryConfig()).reviewers,
        )),
        "skills": [],  # no session ran: nothing was loaded
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
    _progress_best_effort(
        ProgressLog.for_task(factory_root, profile["name"], task_id),
        {"run_id": run_id, "outcome": "escalated",
         "escalation": escalation, "findings": findings},
        task_id,
    )
    queue.transition(task_id, "needs_human")
    return RunResult("needs_human", task_id, escalation=escalation, reports=reports)


def _no_deploy_target(profile: dict) -> bool:
    """Whether the profile declares nothing to deploy or verify live.

    ``deploy.mechanism`` is free text in the schema (project.schema.json),
    so the none-form is matched exactly: ``none``, or ``none`` followed
    by optional whitespace and an opening parenthesis (``none (…)``).
    Anything else — ``nonexistent-host``, ``nonetheless (vercel)`` — is
    a deploy target, so a merged task there keeps waiting for F-14: the
    close fails closed, never open on prose that merely starts with
    "none". An empty ``live_checks`` means no L4 exists to wait for.
    Both must hold — either one alone leaves post-merge work for F-14
    (``verify-live``), and until F-14 exists a merged task on such a
    profile stays ``in_review``.
    """
    deploy = profile.get("deploy") or {}
    mechanism = str(deploy.get("mechanism") or "").strip().lower()
    return (
        mechanism == "none" or re.match(r"none\s*\(", mechanism) is not None
    ) and not (profile.get("live_checks") or {})


def _close_merged_tasks(
    queue: TaskQueue, profile: dict, factory_root: Path,
) -> list[tuple[str, str]]:
    """Close merged ``in_review`` tasks on a no-deploy profile (the fix).

    A merged PR on a profile with no deploy target has nothing left to
    wait for: nothing will ever deploy, and no L4 live check exists to
    pass — so the task advances through the F-03 state machine
    (``in_review -> deploying -> done``, or ``awaiting_owner`` while
    owner steps are open, exactly as F-03 requires) via
    :meth:`TaskQueue.transition_path`, never a hand edit, and the
    progress log records the merged PR as the evidence. Session-less,
    like every sweep transition: no attempt is spent.

    Called before the pick in :func:`run_once` — where it needs no gh
    round-trip at all, because ``links.merged`` is stamped terminal and
    never re-polled, which is how tasks merged before this fix
    (F-11/#58, F-12/#61) unstuck — and again from the sweep after a
    fresh merge stamp (recovery loop or open-PR loop), so the run that
    observes the merge also closes it. Every call is the same
    idempotent scan. A task with unmerged slices is left alone: slice
    continuation is the slice loop's edge (``deploying -> ready``),
    not this close.

    Args:
        queue: The loaded queue (its only writer).
        profile: The loaded ``project.yaml`` mapping.
        factory_root: Progress logs live under it.

    Returns:
        ``[(task_id, final_status)]`` for every task closed, in queue
        order; empty when the profile has a deploy target or nothing
        was stuck.
    """
    if not _no_deploy_target(profile):
        return []
    closed: list = []
    for task in queue.doc.get("tasks", []):
        links = task.get("links") or {}
        if task.get("status") != "in_review" or not _is_merged(links):
            continue
        if any(s.get("status") != "merged" for s in (task.get("slices") or [])):
            continue
        target = "awaiting_owner" if open_owner_steps(task) else "done"
        queue.transition_path(task["id"], ["deploying", target])
        _progress_best_effort(
            ProgressLog.for_task(factory_root, profile["name"], task["id"]),
            {"run_id": new_run_id(),
             "outcome": "closed",
             "note": (
                 f"{links['merged']} is merged and the profile has no deploy "
                 f"target (deploy.mechanism none, live_checks empty): nothing "
                 f"to deploy or verify live — {task['id']} in_review → "
                 f"deploying → {target}, the merge itself the evidence"
             )},
            task["id"],
        )
        closed.append((task["id"], target))
    return closed


def _sweep_stale_prs(
    queue: TaskQueue, profile: dict, *, factory_root: Path,
    srt_bin_path: Path | None, gate_timeout_sec: int, states: dict,
    recovery: list[dict], ready: list[str],
    config: FactoryConfig | None = None,
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
    for the F-14 deploy gate like every merged factory PR on a
    deploy-target profile, stamped terminal — round-5 finding 4 +
    round-6 findings 1+2; on a no-deploy profile the close pass takes
    it on to done/awaiting_owner in this same run); a closed
    stale_base PR was your rejection — the task goes straight to
    ``ready`` (or ``blocked`` when a dependency is open — round-6
    finding 4) for its fix round (round-5 finding 3: no lease-less
    ``in_progress`` hop for the next run's reclaim to clean up). A
    pr_closed task's PR was
    already closed at escalation, so the sweep reads no new intent from
    it — its fix-round word is ``factory recover``.

    Every open PR proven current against the remote default head — up
    to date with it, re-validated on it now, or cached as either for
    exactly this head — is appended to ``ready`` as ``<url> against
    <branch>@<sha12>`` (round-7 finding 2): the report AC-3 and F-28's
    owner step ("merge only after `factory run` has reported them
    ready against the current main") need. A PR with an unknown state,
    or one that fails re-validation, is never reported ready.

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
            # in_review, exactly where every merged factory PR on a
            # deploy-target profile waits for the F-14 deploy gate
            # (nothing in F-26 advances a merged task there; reconcile
            # skips non-draft tasks by design). The stamp makes merged
            # terminal: later runs never gh-poll the link, count it, or
            # re-validate it. On a no-deploy profile the close pass
            # below takes it on to done/awaiting_owner in this same
            # run — when no slice remains unmerged; a sliced task with
            # unmerged slices stays in_review for its next slice. The
            # note claims only the recovery: whatever the close pass
            # then does, its own "closed" entry is the record.
            new_status = "in_review"
            task["links"]["merged"] = task["links"]["pr"]
            note = (f"GitHub shows {task['links']['pr']} merged: "
                    f"{escalation} recovery — back to in_review"
                    + ("" if _no_deploy_target(profile)
                       else " for the F-14 deploy gate"))
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
        _progress_best_effort(
            ProgressLog.for_task(factory_root, project, task["id"]),
            {"run_id": new_run_id(), "outcome": "recovered", "note": note},
            task["id"],
        )
    # Recovery stamps are merged-terminal: on a no-deploy profile the
    # tasks they returned to in_review close now, in this run.
    _close_merged_tasks(queue, profile, factory_root)
    open_prs = [
        task for task in queue.doc.get("tasks", [])
        if task.get("status") == "in_review"
        and (task.get("links") or {}).get("pr")
        and (task.get("links") or {}).get("branch")
        and not _is_merged(task.get("links") or {})
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
            # be auto-deleted. Round-6 findings 1+2: the stamp makes
            # merged terminal — later runs never gh-poll this link
            # again. The task waits in in_review for the F-14 deploy
            # gate, except on a no-deploy profile, where the close pass
            # takes it to done/awaiting_owner in this same run.
            links["merged"] = links["pr"]
            queue.save()
            _close_merged_tasks(queue, profile, factory_root)
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
                run_id=new_run_id(), config=config,
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
            ready.append(f"{links['pr']} against "
                         f"{default_branch}@{cached['default'][:12]}")
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
                ready.append(f"{links['pr']} against "
                             f"{default_branch}@{default_sha[:12]}")
                continue  # the PR branch already contains the default head
            if links.get("revalidated") == {"pr": pr_sha, "default": default_sha}:
                ready.append(f"{links['pr']} against "
                             f"{default_branch}@{default_sha[:12]}")
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
                # other failure is infrastructure, not the PR. F-45: the
                # call runs through the shared remote_call wrapper with
                # the merge's own (local) timeout.
                merge = remote_call(
                    ["git", *GIT_HARDENING, "-C", str(work.root), *_IDENTITY,
                     "merge", "--no-edit", default_sha],
                    env_extra={"LC_ALL": "C", "LANG": "C", **GIT_NET_ENV},
                    timeout_sec=_MERGE_TIMEOUT_SEC,
                    what=f"git merge of {branch} onto "
                         f"{default_branch}@{default_sha[:12]}",
                )
            except RemoteCallTimeout as exc:
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
                    # F-47: the sweep owns no attempt trace; an escapee
                    # here opens this re-validation's own trace file
                    # (trace/<project>/<work run id>.jsonl) — the store
                    # `factory status` counts from — only when it fires.
                    runner = sandboxed_runner(
                        clean.root, clean.settings_path,
                        srt_bin_path=srt_bin_path,
                        on_escapee=_sweep_trace_sink(
                            factory_root, project, work.run_id, task_id),
                    )
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
                    report_path=report_path, reports=reports, config=config,
                )
            # Pass: no attempt (no session ran, no budget spent); the
            # readiness record is the log entry plus the stored report,
            # and links.revalidated caches the pair so an unchanged base
            # is not re-paid on every later run.
            queue.get(task_id)["links"]["revalidated"] = {
                "pr": pr_sha, "default": default_sha}
            queue.save()
            _progress_best_effort(
                ProgressLog.for_task(factory_root, project, task_id),
                {"run_id": work.run_id, "outcome": "pass",
                 "note": (f"stale-base re-validation: L0+L1 pass on {branch} "
                          f"merged onto {default_branch}@{default_sha[:12]} "
                          f"({report_path.name})")},
                task_id,
            )
            ready.append(f"{links['pr']} against "
                         f"{default_branch}@{default_sha[:12]}")
        finally:
            cleanup_run(work)
    return None


def _sweep_trace_sink(
    factory_root: Path, project: str, run_id: str, task_id: str
) -> "Callable[[dict], None]":
    """A trace sink for the stale-PR sweep's gate runner (F-47, I-9).

    The sweep runs L0/L1 outside any attempt, so no :class:`TraceWriter`
    exists for it; the writer is created on the first event (an escapee
    is the only thing this sink is handed), so a clean sweep leaves no
    trace file behind."""
    writer: list[TraceWriter] = []

    def sink(event: dict) -> None:
        if not writer:
            writer.append(TraceWriter(factory_root, project, run_id, task_id,
                                      extra_secrets=(seat_token() or "",)))
        writer[0].append(event)

    return sink


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
        RunError: The fetch failed or timed out (F-45: through the shared
            :func:`factory.publish.remote_call` wrapper). Raised before
            any session starts.
    """
    try:
        proc = remote_call(
            ["git", *GIT_HARDENING, "-C", str(work.root), "fetch", "--quiet",
             "--no-tags", "--filter=blob:none", remote, default_branch],
            what=f"fetch of {default_branch!r} from {remote}",
        )
    except RemoteCallTimeout as exc:
        # Round-4 finding 5: a slow remote is the usual exit-2 message,
        # never a raw traceback.
        raise RunError(str(exc)) from exc
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
    # The one budget filter (factory.state.attempt_budget_used): same-slice
    # attempts, not aborted, not session-less sweep escalations (round-5
    # finding 1), and only those at or after the latest reset marker — an
    # owner-granted reset (factory recover --reset-attempts) restores the
    # budget without deleting any attempt record.
    used = attempt_budget_used(task, slice_id)
    versions = prompt_versions(profile, reviewers=ctx.config.reviewers)
    log = ProgressLog.for_task(ctx.factory_root, project, task_id)
    protected = list((profile.get("paths") or {}).get("protected") or [])
    implementer_system = assemble_implementer_prompt(profile)
    reviewer_system = assemble_reviewer_prompt(profile)
    verifier_system = assemble_verifier_prompt(profile)
    session_seconds = 0.0
    # F-22: the project's vetted toolkit, per seat (a seat section in the
    # profile narrows the reviewer to a read-only subset). Resolved once,
    # against the loaded index, before any session starts — an index that
    # drifted since load_profile refuses here, not mid-attempt.
    toolkit_implementer = resolve_toolkit(profile, "implementer")
    toolkit_reviewer = resolve_toolkit(profile, "reviewer")

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
    # F-41: the final full review's state. ``final_repeats_left`` bounds
    # how often a failed final review may send its findings to one more
    # implementer round and repeat (config, default 1); when it is spent
    # the task escalates ``review_not_converging`` — nothing publishes
    # with an unresolved blocking or major finding. ``final_pending``
    # marks the round that owes the repeat: its review IS the repeat,
    # over the full diff, not another delta pass.
    final_repeats_left = ctx.config.final_review_repeats
    final_pending = False
    round_no = 0
    # F-45 AC-4 (m-13): whether this round's finish() already recorded its
    # attempt. The error path below must never append a second attempt for
    # a round whose first one landed (one failure = exactly one attempt);
    # the flag resets with every round.
    round_attempt_landed = False

    work = create_run(project, source, factory_root=ctx.factory_root, ref=default_branch)
    # F-43: the run's egress secrets — every literal this run holds that
    # session text must never carry out (the seat token today; F-59 adds
    # per-provider credentials). One tuple feeds the trace writer AND the
    # egress seam below, so no channel scrubs a different set.
    egress_secrets = (seat_token() or "",)
    # F-30: this run's live trace — one writer per run, appended to as the
    # events happen and never allowed to change the run itself (AC-5's
    # failure policy lives in the writer). The seat token is handed over
    # as a literal secret: orchestrator-written events (error reasons,
    # gate notes) bypass the session-side tap, and the writer scrubs at
    # every boundary — the token in a shape F-25 cannot match included.
    trace = TraceWriter(ctx.factory_root, project, work.run_id, task_id,
                        extra_secrets=egress_secrets)
    try:
        with trace.phase("setup"):
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
                # F-22 AC-8: what the round's sessions actually loaded,
                # name@content-hash, off the session records themselves.
                "skills": sorted({
                    skill
                    for record in records if record is not None
                    for skill in (record.skills or [])
                }),
                # F-42 AC-5: Bash calls this round's sessions made with
                # dangerouslyDisableSandbox, denied by the PreToolUse hook
                # — summed off the session records so repeated bypass
                # attempts count on the attempt, not only in the trace.
                "sandbox_bypass_denials": sum(
                    getattr(r, "sandbox_bypass_denials", 0) or 0
                    for r in records if r is not None
                ),
            }
            models = [f"{r.seat}={r.model}" for r in records if r and r.model]
            if models:
                record["model"] = " ".join(models)
            if report_path:
                record["validator_report"] = str(report_path)
            record.update({k: v for k, v in extra.items() if v is not None})
            return record

        def _rerun_review_seat(prompt, spend, retry_records, outcomes,
                               seat_no, session, parse_error):
            """ONE retry of one reviewer seat whose verdict failed to
            parse (PR #90 round 2): the same prompt through the same
            fan-out machinery. :func:`factory.l2.parse_reviews_with_retry`
            drives it and decides whether the retry's verdict is used; a
            still-invalid retry keeps the fail-closed union FAIL.

            Budget-fitted (round 3): the re-run is capped at what the
            attempt's turn and wall-clock budgets actually have left
            (:func:`_retry_budget`, the verifier's turn floor reserved
            since round 4) and REFUSED — ``None``, counted as no retry —
            when neither can fit one; the refusal's budget is named in
            ``outcomes`` for the seat's note. A re-run that dies on
            quota is not a refusal: its record lands in
            ``retry_records`` (and the caller aborts the attempt exactly
            like the fan-out's own quota branch — one shared path,
            :func:`_quota_exhausted` below).

            The retry's session is recorded like any reviewer seat —
            records, spend, vents, trace — plus its own
            ``reviewer_retry`` trace event naming the role, the 1-based
            seat number and the original failure (the attempt record is
            schema-locked, so the trace is the retry's ledger)."""
            nonlocal session_seconds
            fit = _retry_budget(
                budget, records, session_seconds, reviewer_share)
            if fit.refusal is not None:
                outcomes[seat_no] = {"refused": fit.refusal}
                return None
            retry_turns, retry_wall = fit.turns, fit.wall
            t0 = time.monotonic()
            record = run_sessions_parallel(
                [("reviewer", prompt)], clean.root, profile,
                runner=ctx.session_runner, factory_root=ctx.factory_root,
                max_turns=retry_turns, timeout_min=retry_wall,
                protected=protected, system_prompt=reviewer_system,
                srt_bin_path=ctx.srt_bin_path,
                run_id=work.run_id, task_id=task_id,
                trace_sink=trace.append, toolkit=toolkit_reviewer,
            )[0]
            session_seconds += time.monotonic() - t0
            retry_records.append(record)
            outcomes[seat_no] = {"retry": record}
            records.append(record)
            spend.add(record)
            _log_vents(log, record, "reviewer", work.run_id, task_id,
                       egress_secrets)
            trace.session_end(record, "reviewer")
            trace.append({
                "type": "reviewer_retry",
                "seat": "reviewer",
                "seat_no": seat_no,
                "status": record.status,
                "reason": record.reason,
                "original_status": session.status,
                "original_reason": session.reason or parse_error,
            })
            return record

        def _quota_exhausted(quota_record):
            """The quota abort the fan-out's own sessions take (task
            back to ready, no attempt burned) — and, since PR #90
            round 3, the path a QUOTA-HITTING RETRY takes too: a re-run
            that dies on quota must abort like the fan-out, not void
            the round as invalid_verdict_json and burn an attempt. One
            path for the round review, the final review and their
            retries."""
            attempt = attempt_record(
                attempt_no, started, "aborted", records, None,
                escalation="quota_exhausted", note=quota_record.reason,
            )
            finish(attempt, "ready", [f"1. quota: {quota_record.reason}"])
            return RunResult("quota_exhausted", task_id,
                             escalation="quota_exhausted", reports=reports)

        def _collect_verdicts(reviews, prompt, spend, label):
            """The fan-out collection BOTH reviews run (round 4 finding
            5: one block, not two copies): parse the seats with one
            budget-fitted, quota-stopping re-run each; abort the attempt
            (returning the RunResult) when a re-run hit quota; else
            return the per-seat verdicts with the notes appended — one
            note per STILL-INVALID seat, naming the original failure,
            whether a re-run happened or was refused (and by which
            budget), and the re-run's outcome."""
            retry_records: list = []
            outcomes: dict[int, dict] = {}

            def rerun(seat_no, session, parse_error):
                return _rerun_review_seat(
                    prompt, spend, retry_records, outcomes,
                    seat_no, session, parse_error)

            verdicts, _ = parse_reviews_with_retry(
                reviews, rerun, stop=_is_quota)
            quota = next(
                (r for r in retry_records if _is_quota(r)), None
            )
            if quota is not None:
                return [], _quota_exhausted(quota)
            for seat_no, (session, verdict) in enumerate(
                    zip(reviews, verdicts), start=1):
                if verdict.parse_error is None:
                    continue  # valid, or rescued by its re-run: no note
                outcome = outcomes.get(seat_no)
                if session.status == "completed" and not outcome:
                    # Chose: a seat whose session completed (unparseable
                    # output) with no re-run offered stays silent — in
                    # _run_task every invalid seat IS offered one, so
                    # this arm is the helper-without-a-rerun case.
                    continue
                original = (
                    f"session {session.status} ({session.reason})"
                    if session.status != "completed"
                    else "unparseable verdict output"
                )
                retry = (outcome or {}).get("retry")
                if retry is not None:
                    reran = (f"re-run ended: session {retry.status} "
                             f"({retry.reason})")
                elif outcome and "refused" in outcome:
                    reran = f"re-run refused: {outcome['refused']}"
                else:
                    reran = "no re-run offered"
                extra_findings.append(
                    f"[L2 {label} seat {seat_no}] {original}; {reran}")
            return verdicts, None

        # F-30: the attempt every trace event of this round is stamped with,
        # and the attempt outcome mapped to --follow's terminal marker.
        _RUN_END = {"pass": "published", "fail": "failed",
                    "escalated": "failed", "aborted": "aborted"}

        def finish(attempt, status, findings, finding_keys=None, **result):
            nonlocal round_attempt_landed
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
            # F-45 AC-4 (m-13): the attempt is on disk — anything failing
            # after this point must not let the round handler below append
            # a second one for the same failure.
            round_attempt_landed = True
            _progress_best_effort(log, entry, task_id)
            if status is not None:
                # The run ends here (a ``None`` status is a failed round
                # that continues into the next attempt): one terminal
                # marker per run, and it is the run's last event.
                trace.run_end(_RUN_END.get(attempt["outcome"], "failed"),
                              escalation=attempt.get("escalation"))
                queue.transition(task_id, status)
            return result

        while True:
            attempt_no = used + 1
            trace.attempt = attempt_no
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
                trace.run_end("aborted")  # no attempt recorded: the run stopped
                return RunResult("peak_window", task_id)

            # This round's state, initialised before anything can raise: the
            # error path below records its attempt from whatever exists.
            round_no += 1
            round_attempt_landed = False
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
            delta = None  # this round's review delta (None = full diff)
            final_field = None  # the final review's attempt-record summary
            final_exhausted = False  # a failed final review, repeats spent
            try:
                # ---- implementer
                # The fix block below carries the newest entry's findings,
                # so that entry renders without them (read once, not
                # twice — the entry itself still renders, F-10 AC-10) and
                # no entry ever renders the seeding hashes.
                with trace.phase("implementer") as impl_spend:
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
                        run_id=work.run_id, task_id=task_id,
                        trace_sink=trace.append,  # F-30: live tool_call events
                        toolkit=toolkit_implementer,  # F-22: vetted toolkit
                    )
                    session_seconds += time.monotonic() - t0
                    records = [impl]
                    impl_spend.add(impl)
                    # F-27: the session's vents reach the task's progress log
                    # the moment the session returns — before any gate can
                    # crash, and whatever the session's outcome was.
                    _log_vents(log, impl, "implementer", work.run_id, task_id,
                               egress_secrets)
                    trace.session_end(impl, "implementer")  # F-30
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
                                                  srt_bin_path=ctx.srt_bin_path,
                                                  on_escapee=trace.append)
                        with trace.phase("L0"):
                            l0 = run_l0(profile, clean.root, base_sha, head_sha, task=task,
                                        runner=runner, timeout_sec=ctx.gate_timeout_sec)
                            reports["L0"] = l0.to_dict()
                            trace.gate_checks(reports["L0"])  # F-30 AC-3
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
                            with trace.phase("L1"):
                                l1 = run_l1(profile, clean.root, base_sha, head_sha, task=task,
                                            runner=runner, timeout_sec=ctx.gate_timeout_sec)
                                reports["L1"] = l1.to_dict()
                                trace.gate_checks(reports["L1"])  # F-30 AC-3
                        # A round that owes a final-review repeat (F-41 AC-3)
                        # skips its regular review entirely: the repeat is
                        # the round's review, over the full diff.
                        if (l1 is not None and l1.verdict == "pass"
                                and not final_pending):
                            # ---- reviewer (a session start: the checks apply)
                            with trace.phase("L2") as review_spend:
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
                                # F-38 review finding 3: the fan-out shares the
                                # REMAINDER of the attempt's turn budget —
                                # each reviewer session capped at
                                # remaining // N, so the sessions' summed
                                # turns can never exceed max_turns. A share
                                # below the floor refuses to start (a review
                                # that cannot take a tool turn and answer is
                                # not a review), escalating exactly like the
                                # wall-clock path.
                                remaining_turns = (
                                    budget["max_turns"] - _turns_spent(records)
                                )
                                reviewer_share = (
                                    remaining_turns // ctx.config.reviewers
                                )
                                if reviewer_share < _REVIEW_TURN_FLOOR:
                                    path = _store(reports_dir, work.run_id,
                                                  attempt_no, reports)
                                    attempt = attempt_record(
                                        attempt_no, started, "escalated", records,
                                        path, escalation="budget_exhausted",
                                        note=(f"turn budget spent before the review "
                                              f"fan-out: {remaining_turns} of "
                                              f"{budget['max_turns']} turns left, "
                                              f"{reviewer_share} per reviewer session"),
                                    )
                                    finish(attempt, "needs_human", [
                                        f"1. turn budget spent before the review "
                                        f"fan-out ({remaining_turns} turns left for "
                                        f"{ctx.config.reviewers} reviewer sessions)",
                                    ])
                                    return RunResult(
                                        "needs_human", task_id,
                                        escalation="budget_exhausted",
                                        reports=reports,
                                    )
                                # F-38: N reviewer sessions over the same diff,
                                # each a fresh context, in parallel — one
                                # integrity guard spanning every one of them
                                # (one baseline before, one workspace check
                                # after all finish; a mutation by any session
                                # fails the whole fan-out), each session with
                                # its own settings and scratch, and all inside
                                # the attempt's turn and wall-clock budgets.
                                reviews = run_sessions_parallel(
                                    [("reviewer", review_prompt)]
                                    * ctx.config.reviewers,
                                    clean.root, profile,
                                    runner=ctx.session_runner,
                                    factory_root=ctx.factory_root,
                                    max_turns=reviewer_share,
                                    timeout_min=remaining_review,
                                    protected=protected,
                                    system_prompt=reviewer_system,
                                    srt_bin_path=ctx.srt_bin_path,
                                    # The reviewers sit in the clean gate clone,
                                    # but their vents belong to the task's run.
                                    run_id=work.run_id, task_id=task_id,
                                    trace_sink=trace.append,  # F-30
                                    toolkit=toolkit_reviewer,  # F-22: narrowed
                                )
                                session_seconds += time.monotonic() - t0
                                records.extend(reviews)
                                for review in reviews:
                                    review_spend.add(review)
                                    _log_vents(log, review, "reviewer",  # F-27
                                               work.run_id, task_id, egress_secrets)
                                    trace.session_end(review, "reviewer")  # F-30
                                quota = next(
                                    (r for r in reviews if _is_quota(r)), None
                                )
                                if quota is not None:
                                    return _quota_exhausted(quota)
                                # F-38: merge the verdicts into the union,
                                # vote it, then converge — in that order. A
                                # verdict still invalid after its one retry
                                # fails the whole review (the single-reviewer
                                # rule, unchanged), and the verifier only
                                # runs when there are findings to vote on.
                                verdicts, quota_result = _collect_verdicts(
                                    reviews, review_prompt, review_spend,
                                    "reviewer")
                                if quota_result is not None:
                                    return quota_result
                                union = merge_reviews(verdicts)
                                l2 = union
                                if union.parse_error is None and union.findings:
                                    remaining_verifier = (
                                        budget["wall_clock_min"]
                                        - session_seconds / 60.0
                                    )
                                    verifier_turns = (
                                        budget["max_turns"] - _turns_spent(records)
                                    )
                                    if remaining_verifier <= 0 or (
                                        verifier_turns < _REVIEW_TURN_FLOOR
                                    ):
                                        spent = (
                                            "wall-clock" if remaining_verifier <= 0
                                            else "turn"
                                        )
                                        path = _store(reports_dir, work.run_id,
                                                      attempt_no, reports)
                                        attempt = attempt_record(
                                            attempt_no, started, "escalated", records,
                                            path, escalation="budget_exhausted",
                                            note=f"{spent} budget spent before "
                                                 "the verifier pass",
                                        )
                                        finish(attempt, "needs_human", [
                                            f"1. {spent} budget spent before "
                                            "the verifier pass",
                                        ])
                                        return RunResult(
                                            "needs_human", task_id,
                                            escalation="budget_exhausted",
                                            reports=reports,
                                        )
                                    verifier_prompt = _verifier_prompt(
                                        task, diff, union.findings, delta=delta)
                                    t0 = time.monotonic()
                                    verifier = ctx.session_runner(
                                        "verifier", verifier_prompt,
                                        clean.root, profile,
                                        factory_root=ctx.factory_root,
                                        max_turns=verifier_turns,
                                        timeout_min=remaining_verifier,
                                        protected=protected,
                                        system_prompt=verifier_system,
                                        srt_bin_path=ctx.srt_bin_path,
                                        run_id=work.run_id, task_id=task_id,
                                        trace_sink=trace.append,  # F-30
                                        # F-22 × F-38: the verifier gets the
                                        # REVIEWER's narrowed toolkit, not the
                                        # implementer's — it judges the
                                        # reviewers' findings against the same
                                        # read-only evidence surface they had
                                        # (docs lookups, vetted skills); there
                                        # is no "verifier" seat in the toolkit
                                        # config, and giving it none would let
                                        # reviewers cite evidence the verifier
                                        # cannot open.
                                        toolkit=toolkit_reviewer,
                                    )
                                    session_seconds += time.monotonic() - t0
                                    records.append(verifier)
                                    review_spend.add(verifier)
                                    _log_vents(log, verifier, "verifier",  # F-27
                                               work.run_id, task_id, egress_secrets)
                                    trace.session_end(verifier, "verifier")  # F-30
                                    if _is_quota(verifier):
                                        attempt = attempt_record(
                                            attempt_no, started, "aborted", records,
                                            None, escalation="quota_exhausted",
                                            note=verifier.reason,
                                        )
                                        finish(attempt, "ready",
                                               [f"1. quota: {verifier.reason}"])
                                        return RunResult(
                                            "quota_exhausted", task_id,
                                            escalation="quota_exhausted",
                                            reports=reports,
                                        )
                                    if verifier.status != "completed":
                                        extra_findings.append(
                                            f"[session {verifier.status}] the "
                                            f"verifier session ended: "
                                            f"{verifier.reason} — nothing was "
                                            "dropped; every finding stands"
                                        )
                                    l2 = apply_vote(
                                        union,
                                        parse_verifier_vote(
                                            verifier.result
                                            if verifier.status == "completed"
                                            else None,
                                            union.findings,
                                        ),
                                        reviewers=ctx.config.reviewers,
                                    )
                                l2 = apply_convergence(
                                    l2,
                                    prior,
                                    delta_files if delta_files is not None else frozenset(),
                                    delta_mode=delta is not None,
                                )
                                reports["L2"] = l2.to_dict()
                                trace.gate_checks(reports["L2"])  # F-30 AC-3
                                # Only a review where every reviewer session
                                # completed and the merged verdict parsed moves
                                # the frontier. A crashed, timed-out or invalid
                                # review reviewed nothing: the next round's
                                # delta must still cover this head, or code no
                                # reviewer ever saw would become "outside the
                                # delta" and its findings dropped. (A failed
                                # verifier session doesn't un-review the round:
                                # its vote dropped nothing, every finding
                                # stands.)
                                reviewed = (
                                    all(r.status == "completed" for r in reviews)
                                    and l2.parse_error is None
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
                        # ---- F-41: the final full review ----
                        # A fix round's review scopes to the delta (F-23), so
                        # a defect in a file the fix rounds never touched —
                        # flagged by nobody in round one, invisible to L0/L1
                        # because nothing tests it — can ride a passing delta
                        # review all the way to publish (F-16's run did).
                        # When this attempt has already reviewed once and the
                        # round's delta review passed, ONE more full review
                        # runs before anything publishes (AC-1); a round that
                        # owes a repeat (AC-3) runs it as its only review. A
                        # round whose review was already full — the attempt's
                        # first, or the history-rewrite fallback — needs no
                        # second one.
                        if final_pending or (
                            reviewed and l2 is not None
                            and l2.verdict == "pass" and delta is not None
                        ):
                            with trace.phase("L2-final") as final_spend:
                                if in_peak_window(ctx.now(), ctx.config.peak_window):
                                    # Same rule as the regular review: the
                                    # window stops the next session start, the
                                    # attempt is recorded (aborted — the
                                    # window is nobody's fault) and the next
                                    # session is told the change existed and
                                    # was discarded with the run.
                                    path = _store(reports_dir, work.run_id,
                                                  attempt_no, reports)
                                    attempt = attempt_record(
                                        attempt_no, started, "aborted", records,
                                        path,
                                        note=(f"peak window before the final review; "
                                              f"the change (head {head_sha[:12]}, "
                                              f"passed L0, L1 and the delta review) "
                                              f"was discarded with the run"),
                                    )
                                    finish(attempt, "ready", [
                                        f"1. peak window stopped attempt "
                                        f"{attempt_no} before the final review: its "
                                        f"change (head {head_sha[:12]}) had passed "
                                        f"L0, L1 and the delta review and was "
                                        f"discarded with the run — redo it",
                                    ])
                                    return RunResult("peak_window", task_id,
                                                     reports=reports)
                                remaining_final = (
                                    budget["wall_clock_min"]
                                    - session_seconds / 60.0
                                )
                                if remaining_final <= 0:
                                    # AC-4: a run never skips the final review
                                    # to publish in time — the budget that
                                    # cannot fit it ends the task instead.
                                    path = _store(reports_dir, work.run_id,
                                                  attempt_no, reports)
                                    attempt = attempt_record(
                                        attempt_no, started, "escalated", records,
                                        path, escalation="budget_exhausted",
                                        note="wall-clock budget spent before the "
                                             "final review",
                                    )
                                    finish(attempt, "needs_human", [
                                        "1. wall-clock budget spent before the "
                                        "final review",
                                    ])
                                    return RunResult("needs_human", task_id,
                                                     escalation="budget_exhausted",
                                                     reports=reports)
                                diff = hardened_git(clean.root, "diff", "--no-color",
                                                     "--no-ext-diff",
                                                     f"{base_sha}...{head_sha}")
                                # AC-1: the complete diff is the scope — no
                                # delta section, no carried-findings
                                # suppression. This is the round-one prompt
                                # shape, in fresh contexts, at the end.
                                final_prompt = _review_prompt(
                                    task, diff, reports["L0"], reports["L1"])
                                remaining_turns = (
                                    budget["max_turns"] - _turns_spent(records)
                                )
                                reviewer_share = (
                                    remaining_turns // ctx.config.reviewers
                                )
                                if reviewer_share < _REVIEW_TURN_FLOOR:
                                    path = _store(reports_dir, work.run_id,
                                                  attempt_no, reports)
                                    attempt = attempt_record(
                                        attempt_no, started, "escalated", records,
                                        path, escalation="budget_exhausted",
                                        note=(f"turn budget spent before the final "
                                              f"review fan-out: {remaining_turns} "
                                              f"of {budget['max_turns']} turns left, "
                                              f"{reviewer_share} per reviewer session"),
                                    )
                                    finish(attempt, "needs_human", [
                                        f"1. turn budget spent before the final "
                                        f"review fan-out ({remaining_turns} turns "
                                        f"left for {ctx.config.reviewers} reviewer "
                                        f"sessions)",
                                    ])
                                    return RunResult(
                                        "needs_human", task_id,
                                        escalation="budget_exhausted",
                                        reports=reports,
                                    )
                                t0 = time.monotonic()
                                final_reviews = run_sessions_parallel(
                                    [("reviewer", final_prompt)]
                                    * ctx.config.reviewers,
                                    clean.root, profile,
                                    runner=ctx.session_runner,
                                    factory_root=ctx.factory_root,
                                    max_turns=reviewer_share,
                                    timeout_min=remaining_final,
                                    protected=protected,
                                    system_prompt=reviewer_system,
                                    srt_bin_path=ctx.srt_bin_path,
                                    run_id=work.run_id, task_id=task_id,
                                    trace_sink=trace.append,  # F-30
                                    toolkit=toolkit_reviewer,  # F-22: narrowed
                                )
                                session_seconds += time.monotonic() - t0
                                records.extend(final_reviews)
                                for review in final_reviews:
                                    final_spend.add(review)
                                    _log_vents(log, review, "reviewer",  # F-27
                                               work.run_id, task_id, egress_secrets)
                                    trace.session_end(review, "reviewer")  # F-30
                                quota = next(
                                    (r for r in final_reviews if _is_quota(r)), None
                                )
                                if quota is not None:
                                    return _quota_exhausted(quota)
                                verdicts, quota_result = _collect_verdicts(
                                    final_reviews, final_prompt, final_spend,
                                    "final-review")
                                if quota_result is not None:
                                    return quota_result
                                union = merge_reviews(verdicts)
                                l2 = union
                                if union.parse_error is None and union.findings:
                                    remaining_verifier = (
                                        budget["wall_clock_min"]
                                        - session_seconds / 60.0
                                    )
                                    verifier_turns = (
                                        budget["max_turns"] - _turns_spent(records)
                                    )
                                    if remaining_verifier <= 0 or (
                                        verifier_turns < _REVIEW_TURN_FLOOR
                                    ):
                                        spent = (
                                            "wall-clock" if remaining_verifier <= 0
                                            else "turn"
                                        )
                                        path = _store(reports_dir, work.run_id,
                                                      attempt_no, reports)
                                        attempt = attempt_record(
                                            attempt_no, started, "escalated", records,
                                            path, escalation="budget_exhausted",
                                            note=f"{spent} budget spent before the "
                                                 "final review's verifier pass",
                                        )
                                        finish(attempt, "needs_human", [
                                            f"1. {spent} budget spent before the "
                                            "final review's verifier pass",
                                        ])
                                        return RunResult(
                                            "needs_human", task_id,
                                            escalation="budget_exhausted",
                                            reports=reports,
                                        )
                                    verifier_prompt = _verifier_prompt(
                                        task, diff, union.findings)
                                    t0 = time.monotonic()
                                    verifier = ctx.session_runner(
                                        "verifier", verifier_prompt,
                                        clean.root, profile,
                                        factory_root=ctx.factory_root,
                                        max_turns=verifier_turns,
                                        timeout_min=remaining_verifier,
                                        protected=protected,
                                        system_prompt=verifier_system,
                                        srt_bin_path=ctx.srt_bin_path,
                                        run_id=work.run_id, task_id=task_id,
                                        trace_sink=trace.append,  # F-30
                                        # The verifier judges the reviewers'
                                        # findings against the same read-only
                                        # evidence surface they had (F-22 x
                                        # F-38): the reviewer's narrowed
                                        # toolkit, never the implementer's.
                                        toolkit=toolkit_reviewer,
                                    )
                                    session_seconds += time.monotonic() - t0
                                    records.append(verifier)
                                    final_spend.add(verifier)
                                    _log_vents(log, verifier, "verifier",  # F-27
                                               work.run_id, task_id, egress_secrets)
                                    trace.session_end(verifier, "verifier")  # F-30
                                    if _is_quota(verifier):
                                        attempt = attempt_record(
                                            attempt_no, started, "aborted", records,
                                            None, escalation="quota_exhausted",
                                            note=verifier.reason,
                                        )
                                        finish(attempt, "ready",
                                               [f"1. quota: {verifier.reason}"])
                                        return RunResult(
                                            "quota_exhausted", task_id,
                                            escalation="quota_exhausted",
                                            reports=reports,
                                        )
                                    if verifier.status != "completed":
                                        extra_findings.append(
                                            f"[session {verifier.status}] the "
                                            f"final review's verifier session ended: "
                                            f"{verifier.reason} — nothing was "
                                            "dropped; every finding stands"
                                        )
                                    l2 = apply_vote(
                                        union,
                                        parse_verifier_vote(
                                            verifier.result
                                            if verifier.status == "completed"
                                            else None,
                                            union.findings,
                                        ),
                                        reviewers=ctx.config.reviewers,
                                    )
                                # AC-2: the F-23 delta rules do not apply —
                                # the full diff is the scope, nothing is out
                                # of it and no missed_earlier is owed — so
                                # convergence runs in full-diff mode; only a
                                # surviving blocking or major finding fails
                                # the final review (a declared fail alone
                                # does not: minors reach the PR body within
                                # the nit budget).
                                l2 = final_review_verdict(apply_convergence(
                                    l2, prior, frozenset(), delta_mode=False))
                                if reports.get("L2") is not None:
                                    # The round's delta review stays on the
                                    # record; the final review is the
                                    # authoritative L2 from here (AC-5).
                                    reports["L2-delta"] = reports["L2"]
                                reports["L2"] = l2.to_dict()
                                trace.gate_checks(reports["L2"])  # F-30 AC-3
                                reviewed = (
                                    all(r.status == "completed"
                                        for r in final_reviews)
                                    and l2.parse_error is None
                                )
                                final_field = _final_review_field(
                                    l2, repeat=final_pending)
                                if reviewed:
                                    last_valid_l2 = l2.to_dict()
                                if l2.verdict == "pass":
                                    final_pending = False
                                else:
                                    # The findings are the next round's work
                                    # (AC-3): seed the convergence state
                                    # exactly as a failed review round does,
                                    # and either spend one of the bounded
                                    # repeats or end the task — a loop of
                                    # failed final reviews is the F-23
                                    # bounce, one layer up.
                                    if reviewed:
                                        round_keys = [
                                            f.key for f in l2.findings
                                            if isinstance(f, L2Finding) and f.key
                                        ]
                                        raised = [
                                            {"key": f.key, "file": f.file,
                                             "severity": f.severity,
                                             "text": f.text}
                                            for f in l2.findings
                                            if isinstance(f, L2Finding)
                                        ]
                                        for key in round_keys:
                                            prior[key] = head_sha
                                        last_reviewed = head_sha
                                    if final_repeats_left > 0:
                                        final_repeats_left -= 1
                                        final_pending = True
                                    else:
                                        final_exhausted = True
                        changed = [c.path for c in diff_name_status(clean.root, base_sha, head_sha)]
                    finally:
                        cleanup_run(clean)

                path = _store(reports_dir, work.run_id, attempt_no, reports)
                findings = _numbered_findings(reports, extra_findings)
                passed = all(x is not None and x.verdict == "pass" for x in (l0, l1, l2))

                if passed:
                    with trace.phase("publish"):
                        task = queue.get(task_id)
                        # F-43 (C-1): the PR body's session-text inputs
                        # pass the egress seam BEFORE the body is built —
                        # the implementer's notes and the whole validator
                        # report (every L2 finding text, the verdict JSON)
                        # — scrubbed first, THEN capped (redact-then-cap,
                        # the F-27 invariant: a cap landing inside a
                        # credential would leak its other half). The stored
                        # reports are not touched: this is a projection for
                        # the body. Scrubbed ONCE, before the shrink loop
                        # (review round 1, finding 9): compaction only
                        # shortens already-scrubbed text, so re-scrubbing
                        # per tail walked and regex-scrubbed the whole
                        # report up to three times for the same result.
                        scrubbed_reports = {
                            key: scrub_session_value(value, egress_secrets)
                            for key, value in reports.items()
                        }
                        scrubbed_notes = scrub_session_text(
                            impl.result or "", egress_secrets)[:_PR_NOTES_CAP]
                        for tail in (_PR_OUTPUT_TAIL, 200, 0):  # shrink outputs to fit GitHub
                            body = build_pr_body(
                                task, profile, branch=branch, base_sha=base_sha, head_sha=head_sha,
                                changed_paths=changed,
                                reports={k: _compact(v, tail)
                                         for k, v in scrubbed_reports.items()},
                                l1_report=l1, extra_flags=l0.risk_flags_added,
                                implementer_notes=scrubbed_notes,
                                attempt=attempt_no,
                            )
                            if len(body) <= _PR_BODY_LIMIT:
                                break
                        check_pr_body(body)  # no PR without every section
                        # F-42 review finding 2: the attribution refusal
                        # belongs BEFORE the push — raised after it, the
                        # branch is already public and the task escalates
                        # pr_create_failed with a stranded branch. Free
                        # text only (fences skipped): quoted trailers in
                        # the validator report are evidence, not
                        # attribution. F-43 AC-2: the same call is the
                        # literal-token fence — an independent second
                        # check that assumes the seam above may have
                        # failed, so the secrets travel with it.
                        pr_title = f"{task_id}: {task.get('title', '')}"
                        check_pr_text(pr_title, body, secrets=egress_secrets)
                        push_branch(work.root, repo_cfg["remote"], branch, sha=head_sha,
                                    created=created, default_branch=default_branch)
                        pushed = True
                        url = ctx.open_pr(PullRequest(
                            remote=repo_cfg["remote"], base=default_branch, head=branch,
                            title=pr_title, body=body,
                        ))
                        opened = True
                        attempt = attempt_record(attempt_no, started, "pass", records, path,
                                                 pr=url, sentinels=list(l0.sentinel_results),
                                                 review_drops=_review_drops(l2),
                                                 final_review=final_field)
                        queue.append_attempt(task_id, attempt)
                        # A passed round's findings were accepted as non-blocking:
                        # recording their keys would poison a later fix round
                        # after a rejected PR (only failed reviews seed).
                        _progress_best_effort(
                            log,
                            {"run_id": work.run_id, "attempt": attempt_no,
                             "outcome": "pass", "pr": url, "findings": findings},
                            task_id,
                        )
                        task = queue.get(task_id)
                        task["links"] = {**(task.get("links") or {}), "pr": url, "branch": branch}
                        queue.transition(task_id, "in_review")
                    trace.run_end("published")  # after phase_end: the last line
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
                        review_drops=_review_drops(l2),
                        final_review=final_field,
                    )
                    finish(attempt, "needs_human", findings, finding_keys=round_keys)
                    return RunResult("needs_human", task_id,
                                     escalation="review_not_converging", reports=reports)

                # F-41 AC-3: a failed final review whose repeat budget is
                # spent escalates here — before the attempt accounting,
                # because another implementer round is exactly what the
                # bound exists to refuse. Nothing is published with an
                # unresolved blocking or major finding.
                if final_exhausted:
                    attempt = attempt_record(
                        attempt_no, started, "escalated", records, path,
                        escalation="review_not_converging",
                        note=("the final full review failed and its repeat "
                              "budget is spent (final_review_repeats="
                              f"{ctx.config.final_review_repeats}): an "
                              "unresolved blocking or major finding stands, "
                              "so nothing was published"),
                        sentinels=list(l0.sentinel_results) if l0 is not None else None,
                        review_drops=_review_drops(l2),
                        final_review=final_field,
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
                        review_drops=_review_drops(l2),
                        final_review=final_field,
                    )
                    finish(attempt, "needs_human", findings, finding_keys=round_keys)
                    return RunResult("needs_human", task_id, escalation="attempts_exhausted",
                                     reports=reports)
                attempt = attempt_record(
                    attempt_no, started, "fail", records, path,
                    sentinels=list(l0.sentinel_results) if l0 is not None else None,
                    review_drops=_review_drops(l2),
                    final_review=final_field,
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
                if not round_attempt_landed:
                    # F-45 AC-4 (m-13): the round's own finish() did NOT
                    # record an attempt (it failed at or before
                    # append_attempt) — the error is this round's one
                    # attempt. When the flag IS set, finish() already
                    # recorded it, and a second append here would
                    # double-spend the budget for one failure.
                    attempt = attempt_record(attempt_no, started, "escalated", records,
                                             path, escalation=escalation, note=note)
                    findings = _numbered_findings(reports, [f"[{escalation}] {note}"])
                    queue.append_attempt(task_id, attempt)
                    _progress_best_effort(
                        log,
                        {"run_id": work.run_id, "attempt": attempt_no,
                         "outcome": attempt["outcome"], "escalation": escalation,
                         "findings": findings},
                        task_id,
                    )
                trace.run_end("failed", escalation=escalation)  # F-30
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
    except BaseException as exc:
        # F-30: a run that ends without a recorded outcome still gets its
        # terminal marker — idempotent, so a re-raise after a recorded
        # outcome (the opened-PR path above) never rewrites it. Operator
        # stops (Ctrl-C, SystemExit) are aborts, not failures: metrics
        # read "failed" as "the factory or the agent broke".
        trace.run_end(
            "aborted" if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else "failed")
        raise
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
