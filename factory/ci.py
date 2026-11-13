"""CI feedback (BUILD-ORDER F-75): the publish-path wait, the flake
re-run, and the findings a red CI becomes.

The F-13 pilot's first passing run (2026-11-12) opened a PR whose
project CI then failed on GitHub — a font difference between the
factory's host and the CI runner. The factory never read its PR's CI:
"done" meant "green on the factory's host", and the owner received an
unmergeable PR. F-75 closes that: after a validated publish opens or
updates a PR, :func:`settle` waits for the checks on the EXACT pushed
SHA (the one CI reader is :func:`factory.mergeorder.gh_ci_state` — this
module never re-reads checks), re-runs the failed GitHub Actions jobs
once per head (a flake is real; only a red that survives its re-run
counts), and hands the orchestrator one verdict:

* **green** — the run ends ``pr_opened`` as today;
* **pending / none at the window** — ``pr_opened`` with the note
  ``CI pending`` / ``no CI reported`` (an ``unresolvable`` answer is
  pending, never green — the F-45 rule);
* **red** — a fix round on the same branch and PR, carrying one finding
  per failing check: its name and the capped tail of its failing log
  (``gh run view --log-failed``) or, for a check no Actions run backs,
  its output summary.

The window is the caller's: ``min(ci_wait_min, the run's remaining wall
clock)`` — the orchestrator computes it and adds the wait's elapsed
seconds to its wall accounting. Tests inject ``clock`` and ``sleep``
seams; nothing here really sleeps.

**CI logs are attacker-controlled** — they are produced by code a
session wrote. This module caps every tail (per check and in total) and
returns the text RAW for the caller's egress seam
(:func:`factory.orchestrator` escapes and scrubs it before any prompt,
note or report); the caps here bound the bytes that cross at all.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from typing import Callable

from factory.mergeorder import (
    _CI_FAILED_CONCLUSIONS,
    _CI_PENDING_STATUSES,
    _CI_STATUS_FAILED,
    CIState,
)
from factory.publish import GH_NET_ENV, PublishError, github_slug, remote_call

__all__ = [
    "CIActions",
    "CIHelperError",
    "CI_POLL_SEC",
    "CI_TAIL_PER_CHECK",
    "CI_TAIL_TOTAL",
    "CISettled",
    "CheckFailure",
    "settle",
]

# The wait's poll interval (seconds). A module constant, not config: it
# trades gh API calls against reaction time inside the owner-set window.
CI_POLL_SEC = 30
# How much of one failing check's log the findings carry, and the total
# across checks — the bound on attacker-controlled bytes crossing into
# prompts, notes and stored reports (the caller escapes on top).
CI_TAIL_PER_CHECK = 4000
CI_TAIL_TOTAL = 12000
# One gh-side listing page of runs (bounded; a head with more failed
# runs than this has a problem no re-run fixes).
_RUN_PAGE_LIMIT = 100


class CIHelperError(Exception):
    """A gh run action failed (list, re-run or log). Never fatal: the
    caller degrades — a red that cannot be re-run is real at once, a
    tail that cannot be read is absent — and the message rides the
    note so the degradation is visible."""


@dataclass(frozen=True)
class CheckFailure:
    """One failing check and its capped evidence (raw, unescaped).

    ``tail``: the failing Actions log's tail for this check (grouped by
    job name), capped at :data:`CI_TAIL_PER_CHECK` and inside
    :data:`CI_TAIL_TOTAL`. ``summary``: the check's own output summary
    (CIState.summaries). At least one is empty — a check an Actions run
    backs gets its tail; any other gets its summary.
    """

    name: str
    conclusion: str
    tail: str = ""
    summary: str = ""


@dataclass(frozen=True)
class CISettled:
    """The wait's verdict for one pushed SHA.

    ``outcome``: ``green``, ``red`` (a red that survived its one
    re-run, or one no Actions run backs), ``pending`` or ``none`` (the
    window ended unsettled — the note says which). ``note``: the
    ``pr_opened`` note for the non-green, non-red outcomes (None for
    green and red). ``detail``: what happened, for the trace. ``report``:
    the stored-report payload on red — the capped tails, verbatim.
    """

    outcome: str
    note: str | None = None
    detail: str = ""
    failing: "tuple[CheckFailure, ...]" = ()
    report: dict | None = None
    elapsed_sec: float = 0.0
    polls: int = 0
    reran: bool = field(default=False)
    # R1-F1: how many checks were still running at a red verdict — the
    # failing checks stand, but their logs could not be read (the run
    # never completed); the findings say so.
    partial: int = 0


# ---------------------------------------------------------------- the wait --


def _pending_siblings(state: CIState) -> int:
    """How many of the SHA's checks are not yet completed (queued,
    in_progress, waiting, requested, pending).

    R1-F1: a red with siblings still running is NOT settled. GitHub
    reports no conclusion for a workflow run that is in progress, so
    while siblings run, ``gh run list`` answers nothing to re-run and
    ``--log-failed`` refuses — settling the red there (the #163 matrix
    shape: one job fails fast) would skip the flake guard and ship an
    empty tail. The verdict waits for every check to complete, bounded
    by the window.
    """
    return sum(1 for _name, status in state.checks
               if status in _CI_PENDING_STATUSES)


def _wait(remote: str, sha: str, *, ci_state: Callable, window_sec: float,
          clock: Callable[[], float], sleep: Callable[[float], None],
          sink: Callable | None = None, require_observation: bool = False,
          ) -> "tuple[CIState, str | None, int, bool]":
    """Poll ``ci_state`` for exactly ``sha`` until it settles or the
    window ends.

    Returns ``(state, note, polls, settled)`` — ``settled`` False only
    for the window-ended outcomes, whose ``note`` is set (``CI
    pending`` / ``no CI reported``; an ``unresolvable`` answer is
    pending, never green). A FAILED state settles only when every
    check has completed (R1-F1); at the window a failed-with-siblings
    is still RED (``settled`` True — the failing checks stand; their
    logs may be missing, which :class:`CISettled.partial` names).

    ``require_observation`` (the post-re-run wait, R1-F2): a FAILED
    state counts only after the re-run has been OBSERVED — any check
    seen non-completed since the re-run was issued. The check-runs API
    (``filter=latest``) can keep answering the OLD failed attempt until
    GitHub registers the new one, so an immediate poll can read a stale
    failure and void the flake guard; a stale failure that is never
    observed within the window ends the wait PENDING (``settled``
    False), never red.

    A :class:`PublishError` from the FIRST poll (gh missing, a
    non-GitHub remote) ends the wait at once as pending: those
    conditions are permanent, and burning the window would only delay
    the same note. A seam that worked and then raised mid-wait
    propagates — the orchestrator's crash path records it honestly.
    """
    deadline = clock() + max(0.0, window_sec)
    polls = 0
    observed = False
    while True:
        polls += 1
        try:
            state = ci_state(remote, sha)
        except PublishError as exc:
            if polls == 1:
                if sink is not None:
                    sink({"type": "ci_wait", "sha": sha, "unreadable":
                          str(exc)[:200]})
                return CIState("unresolvable", detail=str(exc)[:300]), \
                    "CI pending", polls, False
            raise
        if _pending_siblings(state):
            observed = True  # the run (or its re-run) is taking effect
        if state.state == "success":
            return state, None, polls, True
        if state.state == "failed" and not _pending_siblings(state):
            if observed or not require_observation:
                return state, None, polls, True
            # R1-F2: a stale pre-re-run failure — not evidence. Keep
            # polling; the re-run must be observed before red counts.
        if clock() >= deadline:
            if state.state == "failed" and (observed or not require_observation):
                # R1-F1: failed plus siblings still running at the window
                # is red — PARTIAL: the failing checks stand, their logs
                # may be missing (the run never completed).
                return state, None, polls, True
            note = "no CI reported" if state.state == "none" else "CI pending"
            if sink is not None:
                sink({"type": "ci_wait", "sha": sha, "state": state.state,
                      "note": note, "polls": polls})
            return state, note, polls, False
        sleep(CI_POLL_SEC)


def _failing_checks(state: CIState) -> "list[tuple[str, str]]":
    """The failing checks as ``(name, conclusion)`` — the Actions
    failure conclusions plus the legacy status failure states, the same
    two sets :func:`factory.mergeorder.classify_ci` judged the SHA by."""
    return [
        (name, conclusion)
        for name, conclusion in state.checks
        if conclusion in _CI_FAILED_CONCLUSIONS
        or conclusion in _CI_STATUS_FAILED
    ]


def _tail(text: str, cap: int) -> str:
    """The last ``cap`` characters of ``text``, cut at a line start —
    a half first line is noise, and the END of a failing log is where
    the failure is. ``cap <= 0`` returns "" (R1-F3: ``text[-0:]`` is
    the WHOLE text, so a spent budget once uncapped its check)."""
    if cap <= 0:
        return ""
    text = text.rstrip("\n")
    if len(text) <= cap:
        return text
    cut = text[-cap:]
    newline = cut.find("\n")
    return cut[newline + 1:] if 0 <= newline < len(cut) - 1 else cut


def _log_buckets(text: str) -> dict:
    """``gh run view --log-failed`` output grouped by job name.

    Each line is ``job\\tstep\\ttimestamp\\tmessage``; the job name is
    the check-runs API's check name for an Actions check. Lines without
    a tab (a format change, or the empty log) are kept under "".
    """
    buckets: dict[str, list[str]] = {}
    for line in text.splitlines():
        job, _, rest = line.partition("\t")
        job = job.strip()
        buckets.setdefault(job or "", []).append(rest.rstrip())
    return buckets


# ------------------------------------------------------------------ settle --


def settle(remote: str, sha: str, *, ci_state: Callable,
           actions: "CIActions", window_sec: float,
           clock: Callable[[], float], sleep: Callable[[float], None],
           rerun_heads: set, sink: Callable | None = None) -> CISettled:
    """Wait for the PR's CI on ``sha``; re-run a flake once; verdict it.

    Args:
        remote: The profile's ``repo.remote`` (passed to the seams).
        sha: The EXACT pushed commit the answer must be about.
        ci_state: The CI reader seam (:func:`gh_ci_state`); only checks
            about ``sha`` count because only ``sha`` is ever asked.
        actions: The gh run actions (failed-run listing, re-run, logs).
        window_sec: The wait bound — the caller's
            ``min(ci_wait_min, remaining wall clock)``. The re-run's
            second wait uses what remains of it.
        clock/sleep: time seams (tests never really sleep).
        rerun_heads: The run's set of SHAs whose failed runs were already
            re-run — the re-run is ONCE per head, never twice.
        sink: Optional trace sink (``trace.append``).

    Returns:
        The :class:`CISettled` verdict. Red means a red that survived
        its one re-run (or one no Actions run backs — re-runnable
        checks only, never a status). Pending/none mean the window
        ended unsettled. Green is green.
    """
    started = clock()
    state, note, polls, settled = _wait(
        remote, sha, ci_state=ci_state, window_sec=window_sec,
        clock=clock, sleep=sleep, sink=sink)
    detail = f"first wait: {state.state}"
    if state.state != "failed":
        return CISettled(
            outcome="green" if state.state == "success"
            else ("none" if state.state == "none" else "pending"),
            note=note, detail=detail, elapsed_sec=clock() - started,
            polls=polls)
    # Red (settled, or failed-with-siblings at the window — R1-F1's
    # partial red). The flake guard: re-run the FAILED Actions jobs for
    # this head once — only a red that survives (or cannot take) the
    # re-run is real. A head already re-run is real at once. ``runs``
    # carries the listing to the findings builder (None = list there; a
    # listing that already failed passes [] so it is not retried).
    runs: "list | None" = None
    if sha in rerun_heads:
        detail += "; head already re-run once — red is real"
    else:
        try:
            runs = actions.failed_runs(remote, sha)
        except (CIHelperError, PublishError) as exc:
            detail += f"; failed-run listing failed ({exc}) — red is real"
            runs = []
        if runs:
            rerun_errors = []
            for run in runs:
                try:
                    actions.rerun(remote, run["id"])
                except (CIHelperError, PublishError) as exc:
                    rerun_errors.append(str(exc))
            rerun_heads.add(sha)  # once per head, whatever the re-run did
            if rerun_errors:
                detail += (f"; the one-per-head re-run could not start "
                           f"({'; '.join(rerun_errors)[:200]}) — red is real")
            else:
                detail += f"; re-ran {len(runs)} failed run(s) once"
                # R1-F2: give the re-run one poll interval to register
                # before the first poll (the API can still answer the
                # OLD attempt at once), then require OBSERVATION — any
                # check seen non-completed — before a failed counts.
                sleep(CI_POLL_SEC)
                remaining = window_sec - (clock() - started)
                state, note, polls2, settled2 = _wait(
                    remote, sha, ci_state=ci_state, window_sec=remaining,
                    clock=clock, sleep=sleep, sink=sink,
                    require_observation=True)
                polls += polls2
                detail += f"; second wait: {state.state}"
                if state.state == "success":
                    return CISettled(
                        outcome="green", detail=detail,
                        elapsed_sec=clock() - started, polls=polls,
                        reran=True)
                if not settled2:
                    # The window ended without the re-run ever being
                    # observed (a stale pre-re-run failure, or checks
                    # still running): NOT a red verdict — the note rides
                    # pr_opened and the ready rule (F-58, extended by
                    # F-75 AC-3) decides readiness later.
                    return CISettled(
                        outcome="none" if state.state == "none" else "pending",
                        note=note, detail=detail,
                        elapsed_sec=clock() - started, polls=polls,
                        reran=True)
                runs = None  # the re-run replaced the runs; re-list for tails
    failing = _failing_checks(state)
    failures, report = _findings(
        remote, sha, state, failing, actions, sink, runs)
    partial = _pending_siblings(state)
    if partial:
        detail += (f"; {partial} check(s) still running at the verdict — "
                   "their logs are missing from the findings")
    detail += f"; {len(failures)} failing check(s)"
    return CISettled(
        outcome="red", detail=detail, failing=failures, report=report,
        elapsed_sec=clock() - started, polls=polls, reran=sha in rerun_heads,
        partial=partial)


def _findings(remote: str, sha: str, state: CIState,
              failing: "list[tuple[str, str]]", actions: "CIActions",
              sink: Callable | None, runs: "list | None",
              ) -> "tuple[tuple[CheckFailure, ...], dict]":
    """One :class:`CheckFailure` per failing check, tails capped.

    Tails come from the failed Actions runs' ``--log-failed`` output,
    grouped by job (check) name; a failing check no bucket covers (a
    status check, or a listing/log failure) falls back to the check's
    output summary. Caps: per check :data:`CI_TAIL_PER_CHECK`, total
    :data:`CI_TAIL_TOTAL` — a check past the total keeps its name and
    conclusion with an empty tail, never its uncapped bytes.

    ``runs``: the failed-run listing settle already holds (None = list
    here; ``[]`` = a listing that already failed, do not retry).
    """
    summaries = dict(state.summaries)
    buckets: dict[str, str] = {}
    try:
        listed = runs if runs is not None else actions.failed_runs(remote, sha)
        for run in listed:
            text = actions.log_failed(remote, run["id"])
            for job, lines in _log_buckets(text).items():
                merged = buckets.get(job, "")
                buckets[job] = (merged + ("\n" if merged else "")
                                + "\n".join(lines))
    except (CIHelperError, PublishError) as exc:
        if sink is not None:
            sink({"type": "ci_wait", "sha": sha,
                  "logs_unavailable": str(exc)[:200]})
        buckets = {}
    failures: list[CheckFailure] = []
    budget = CI_TAIL_TOTAL
    for name, conclusion in failing:
        tail = _tail(buckets.get(name, ""), min(CI_TAIL_PER_CHECK, budget))
        budget -= len(tail)
        # R1-F3: the summary FALLBACK counts against the same total
        # budget (and is only used when there is no tail to read).
        summary = "" if tail else summaries.get(name, "")
        if summary:
            summary = summary[:max(0, budget)]
            budget -= len(summary)
        failures.append(CheckFailure(
            name=name, conclusion=conclusion, tail=tail,
            summary=summary))
    report = {
        "sha": sha,
        "checks": [
            {"name": f.name, "conclusion": f.conclusion,
             "tail": f.tail, "summary": f.summary}
            for f in failures
        ],
    }
    return tuple(failures), report


# ------------------------------------------------------- the gh run actions --


class CIActions:
    """The gh-backed Actions operations: which failed runs report for a
    SHA, re-running a run's failed jobs, and a run's failing log.

    Everything goes through :func:`factory.publish.remote_call`
    (prompt-disabled, timed) with the gh env, and the remote must be a
    GitHub URL (:func:`factory.publish.github_slug`) — the same wrapper
    and refusal the publish path itself uses. A missing gh or a failed
    call raises :class:`CIHelperError` (or PublishError for the
    slug/gh-missing shapes); the caller degrades, never guesses.
    """

    def __init__(self, gh_bin: str | None = None):
        self.gh_bin = gh_bin

    def _gh(self) -> str:
        gh = self.gh_bin or shutil.which("gh")
        if not gh:
            raise PublishError(
                "cannot act on CI runs: gh is not installed (the factory "
                "re-runs failed jobs and reads failing logs with `gh run`)"
            )
        return gh

    def failed_runs(self, remote: str, sha: str) -> "list[dict]":
        """The FAILED GitHub Actions runs that reported for exactly
        ``sha``: ``[{id, name, conclusion}]``."""
        gh = self._gh()
        slug = github_slug(remote)
        proc = remote_call(
            [gh, "run", "list", "--repo", slug, "--commit", sha,
             "--limit", str(_RUN_PAGE_LIMIT),
             "--json", "databaseId,name,conclusion"],
            env_extra=GH_NET_ENV,
            what=f"gh run list (failed runs for {sha[:12]})",
        )
        if proc.returncode != 0:
            raise CIHelperError(
                f"gh run list for {sha[:12]} exited {proc.returncode}: "
                f"{proc.stderr.strip()[:200]}")
        try:
            doc = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise CIHelperError(
                f"gh run list for {sha[:12]} printed no JSON") from None
        if not isinstance(doc, list):
            raise CIHelperError(
                f"gh run list for {sha[:12]} printed "
                f"{type(doc).__name__}, not a list")
        runs = []
        for run in doc:
            if not isinstance(run, dict):
                continue
            run_id = run.get("databaseId")
            if not isinstance(run_id, int):
                continue
            if run.get("conclusion") in _CI_FAILED_CONCLUSIONS:
                runs.append({"id": run_id,
                             "name": str(run.get("name") or run_id),
                             "conclusion": str(run.get("conclusion"))})
        return runs

    def rerun(self, remote: str, run_id: int) -> None:
        """Re-run one run's FAILED jobs (``gh run rerun <id> --failed``)."""
        gh = self._gh()
        slug = github_slug(remote)
        proc = remote_call(
            [gh, "run", "rerun", str(run_id), "--repo", slug, "--failed"],
            env_extra=GH_NET_ENV,
            what=f"gh run rerun {run_id} --failed",
        )
        if proc.returncode != 0:
            raise CIHelperError(
                f"gh run rerun {run_id} --failed exited "
                f"{proc.returncode}: {proc.stderr.strip()[:200]}")

    def log_failed(self, remote: str, run_id: int) -> str:
        """One run's failing log (``gh run view <id> --log-failed``)."""
        gh = self._gh()
        slug = github_slug(remote)
        proc = remote_call(
            [gh, "run", "view", str(run_id), "--repo", slug, "--log-failed"],
            env_extra=GH_NET_ENV,
            what=f"gh run view {run_id} --log-failed",
        )
        if proc.returncode != 0:
            raise CIHelperError(
                f"gh run view {run_id} --log-failed exited "
                f"{proc.returncode}: {proc.stderr.strip()[:200]}")
        return proc.stdout
