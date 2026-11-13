"""Merge-order admission — ``factory run`` refuses unsafe merge setups (F-26).

Two factory PRs that each pass on their own base can break ``main``
together. The labs' answer is a merge queue; the small-scale answer
(BUILD-ORDER F-26) is "the default branch must require status checks with
``strict`` (branches up to date) before merging" — GitHub then refuses to
merge a stale branch and re-runs the required checks on the updated
branch, which is exactly the ordering guarantee the factory needs before
it opens another PR on the same repo.

:func:`admission` is the gate. It asks GitHub about the default branch's
protection through :func:`gh_branch_protection` (a thin ``gh api``
wrapper; tests inject the whole callable, or unit-test
:func:`classify_protection` against canned gh output) and:

* protection with required status checks and ``strict`` → admitted;
* protection without them, or none at all → :exc:`MergeOrderError`
  naming the missing setting, and nothing runs;
* GitHub's plan-limit 403 (a private repo on a free plan cannot have
  branch protection at all) → admitted **only** when the profile declares
  ``repo.branch_protection: unavailable`` (a schema-validated enum). The
  declaration never excuses a repo that could have protection and
  doesn't: a not-protected answer still refuses. For a declared repo the
  factory enforces the equivalent itself — the orchestrator's stale-base
  sweep re-validates every open factory PR against the current remote
  default head before reporting it ready (see
  :func:`factory.orchestrator.run_once`).

The orchestrator calls ``admission`` before it touches the queue, so a
refusal starts no session, clones nothing and spends no budget.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from factory.contract import ContractError
from factory.publish import PublishError, github_slug

__all__ = [
    "CIState",
    "MergeOrderError",
    "Protection",
    "admission",
    "classify_ci",
    "classify_protection",
    "gh_branch_protection",
    "gh_ci_state",
    "gh_pr_state",
]

# gh api error text markers. The plan-limit refusal is how GitHub answers
# the protection call for a private repo on a free plan ("Upgrade to
# GitHub Pro or make this repository public"); the phrasing is matched
# loosely on purpose — the seam (Protection) is what tests pin, and a
# 403 that doesn't read like a plan limit must refuse, not excuse.
_NOT_FOUND = re.compile(r"\b404\b|not found", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b403\b|forbidden", re.IGNORECASE)
_PLAN_LIMIT = re.compile(r"upgrade to github|make this repository public", re.IGNORECASE)


class MergeOrderError(ContractError):
    """The repo's merge setup can't keep PRs ordered; the factory refuses
    to run. Subclasses ContractError so the CLI's existing handling
    (message, exit 2) applies with no new wiring."""


@dataclass(frozen=True)
class Protection:
    """What GitHub answered about the default branch's protection.

    ``checked``: the call succeeded and protection exists. ``strict``:
    required status checks include "branches up to date" (the ordering
    guarantee). ``plan_limit``: GitHub's 403 saying this plan cannot have
    protection at all. Everything else (no protection, strict off, other
    errors) is a refusal with the reason in ``detail``.
    """

    checked: bool = False
    strict: bool = False
    plan_limit: bool = False
    detail: str = ""


def classify_protection(returncode: int, stdout: str, stderr: str) -> Protection:
    """Classify one ``gh api …/protection`` result.

    Args:
        returncode: gh's exit status (0 = the protection document).
        stdout: gh's stdout (the JSON document on 0).
        stderr: gh's stderr (the error text otherwise).

    Returns:
        The :class:`Protection` classification.
    """
    if returncode == 0:
        try:
            doc = json.loads(stdout)
        except json.JSONDecodeError:
            return Protection(detail=f"gh api printed no JSON: {stdout.strip()[:200]}")
        if not isinstance(doc, dict):
            # F-45 AC-5 (amended): rc-0 with non-object JSON (`null`, a
            # list, a bare string/number) is an unresolvable answer, not
            # an AttributeError — the same crash shape m-14 named for
            # `gh pr view`.
            return Protection(
                detail=f"gh api printed non-object JSON "
                       f"({type(doc).__name__}): {stdout.strip()[:200]}"
            )
        checks = doc.get("required_status_checks")
        if isinstance(checks, dict) and checks.get("strict") is True:
            return Protection(checked=True, strict=True)
        if checks is None:
            return Protection(
                checked=True, strict=False,
                detail="branch protection has no required status checks",
            )
        return Protection(
            checked=True, strict=False,
            detail="required status checks do not require branches up to date (strict)",
        )
    text = f"{stderr}\n{stdout}"
    if _PLAN_LIMIT.search(text) and _FORBIDDEN.search(text):
        return Protection(plan_limit=True, detail=text.strip()[:300])
    if _NOT_FOUND.search(text):
        # A 404 is ambiguous: protection off, a missing repo/branch/slug,
        # or a token that cannot see a private repo. Say all three —
        # "protection is off" alone sends the owner to enable protection
        # that may already exist behind an auth problem.
        return Protection(
            detail="GitHub answered 404 for the protection call: branch "
            "protection may be off, or the repo or branch does not exist, "
            "or the token cannot see this repo"
        )
    return Protection(detail=text.strip()[:300] or f"gh api exited {returncode}")


def gh_branch_protection(
    remote_url: str, default_branch: str, *, gh_bin: str | None = None,
    _run: Callable = subprocess.run,
) -> Protection:
    """Ask GitHub about ``default_branch``'s protection via ``gh api``.

    Args:
        remote_url: The profile's ``repo.remote`` URL.
        default_branch: ``repo.default_branch``.
        gh_bin: gh path override (default: ``shutil.which``).
        _run: subprocess seam for tests (canned gh output).

    Returns:
        The classified :class:`Protection`.

    Raises:
        factory.publish.PublishError: gh is missing, or the remote isn't
            a GitHub URL — both mean protection can't be checked, which
            is a refusal, never a silent pass.
    """
    gh = gh_bin or shutil.which("gh")
    if not gh:
        raise PublishError(
            "cannot check branch protection: gh is not installed (the factory "
            "checks the default branch's protection with `gh api`)"
        )
    slug = github_slug(remote_url)
    proc = _run(
        [gh, "api", f"repos/{slug}/branches/{default_branch}/protection"],
        capture_output=True, text=True,
    )
    return classify_protection(proc.returncode, proc.stdout, proc.stderr)


def gh_pr_state(
    pr_url: str, *, gh_bin: str | None = None, _run: Callable = subprocess.run
) -> str:
    """Ask GitHub whether a PR is still open: ``open`` | ``merged`` | ``closed``.

    The stale-base sweep and the open-PR limit must follow GitHub's truth,
    not the queue's: a squash-merged PR leaves its task ``in_review`` until
    reconcile (F-14), and its head branch may already be auto-deleted —
    re-validating or fetching either would blame a shipped PR. gh emits
    ``OPEN``/``MERGED``/``CLOSED``; the answer is lowercased here.

    Raises:
        PublishError: gh is missing, or ``gh pr view`` failed — an unknown
            PR state is never guessed.
    """
    gh = gh_bin or shutil.which("gh")
    if not gh:
        raise PublishError(
            "cannot check PR state: gh is not installed (the factory checks "
            "open PRs with `gh pr view`)"
        )
    proc = _run(
        [gh, "pr", "view", pr_url, "--json", "state"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise PublishError(
            f"cannot check PR state for {pr_url}: {proc.stderr.strip()[:300]}"
        )
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        doc = None
    # F-45 AC-5 (m-14): rc-0 with non-object JSON (`null`, a list, a bare
    # string/number), an object without a usable "state", or empty stdout
    # all map to the documented unresolvable state — PublishError — never
    # an AttributeError. The sweep skips an unresolvable PR and the
    # concurrency limit counts it open (fail closed).
    state = doc.get("state") if isinstance(doc, dict) else None
    if not isinstance(state, str) or not state.strip():
        raise PublishError(
            f"gh pr view {pr_url} printed no usable state "
            f"(stdout {proc.stdout[:200]!r}): the PR's state is "
            "unresolvable, never guessed"
        )
    return state.strip().lower()


# --- F-58 AC-5: the self-enforced green-CI check ----------------------------
#
# A repo that declares ``repo.branch_protection: unavailable`` (a private
# repo on a free plan, F-26 AC-1's exception) has nothing on GitHub's
# side forcing CI green before a merge — the 2026-10-24 incident: main
# red for 11 days while PRs #90–#95 merged. The factory enforces the
# equivalent itself, the same way it already enforces up-to-date: the
# default branch's checks must be green (or pending — a run merges
# nothing) before ``factory run`` starts, and an open PR is never
# reported ready while its checks fail or run. ALL checks count,
# live-network and GitGuardian included (F-51 open question 1): the
# message names the job, so "endpoint down" reads as ``live-network``
# failing, not as a broken PR.

# A completed check's non-green conclusions that mean FAILED.
_CI_FAILED_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "stale"}
)
# A completed check's green conclusions.
_CI_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# A not-yet-completed check's statuses — queued/in_progress, and the
# awaiting states (waiting/requested/pending, e.g. a required
# environment approval). Any other is unknown → refuse.
_CI_PENDING_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "requested", "pending"}
)
# A status ENTRY's failed states (the combined API's per-context truth).
_CI_STATUS_FAILED = frozenset({"failure", "error"})

_CI_PER_PAGE = 100
_CI_MAX_PAGES = 30  # 3000 checks; beyond that the answer is refused, not cut
# F-75: how much of a check's output summary the state carries — the
# CI-feedback findings quote it for checks that have no Actions log.
_CI_SUMMARY_CAP = 2000


@dataclass(frozen=True)
class CIState:
    """GitHub's answer about ONE commit SHA's checks (F-58 AC-5).

    ``state``: ``success`` (every check green), ``failed`` (any failure,
    cancelled, timed_out, action_required or stale), ``pending`` (checks
    still running), ``none`` (no checks reported at all), or
    ``unresolvable`` — gh failed or printed junk (the F-45 rule: never
    guessed, never success).

    ``checks``: every check as ``(name, conclusion-or-state)`` — what the
    refusal messages name, so "live-network failed" is legible.

    ``summaries`` (F-75): the per-check output text GitHub holds — a
    check-run's ``output.summary``, a legacy status entry's
    ``description`` — as ``(name, text)``. Only what exists; the F-58
    consumers (refusal messages, the ready rule) read ``checks`` and are
    unchanged. A summary longer than the fetch cap is cut at it, never
    dropped (it is evidence, not a status).
    """

    state: str
    checks: "tuple[tuple[str, str], ...]" = ()
    detail: str = ""
    summaries: "tuple[tuple[str, str], ...]" = ()


def classify_ci(
    check_runs: "Sequence[dict]", status_doc: "dict | None"
) -> CIState:
    """Classify one SHA's fetched facts (pure; unit-tested directly).

    Args:
        check_runs: The check-runs API's ``check_runs`` entries — the
            LATEST attempt per name (``filter=latest``, so a re-run that
            went green is green; the older failed attempt is history).
        status_doc: The combined commit-status document — judged by its
            ENTRIES (``statuses``), never its rollup ``state``: GitHub
            answers ``{"state": "pending", "statuses": [], …}`` for
            every commit with no legacy statuses, so reading the rollup
            would call every green commit pending (review round 1,
            R1-1). Zero entries contribute nothing.

    Returns:
        The :class:`CIState`.

    Unknown is never ok: a completed check with no conclusion, an
    unrecognised conclusion or status, a malformed status entry (not a
    dict, or without a context/state), or an unrecognised entry state
    all return ``unresolvable``.
    """
    checks: list[tuple[str, str]] = []
    summaries: list[tuple[str, str]] = []
    failed = False
    pending = False
    for run in check_runs:
        if not isinstance(run, dict):
            return CIState("unresolvable",
                           detail=f"a check run is not an object: {run!r}"[:200])
        name = run.get("name")
        if not isinstance(name, str) or not name:
            return CIState("unresolvable",
                           detail="a check run has no usable name")
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status == "completed":
            if not isinstance(conclusion, str):
                return CIState(
                    "unresolvable",
                    detail=f"check {name!r} completed with no conclusion",
                )
            if conclusion in _CI_FAILED_CONCLUSIONS:
                failed = True
            elif conclusion not in _CI_GREEN_CONCLUSIONS:
                return CIState(
                    "unresolvable",
                    detail=f"check {name!r} has unknown conclusion {conclusion!r}",
                )
            checks.append((name, conclusion))
        else:
            if status not in _CI_PENDING_STATUSES:
                return CIState(
                    "unresolvable",
                    detail=f"check {name!r} has unknown status {status!r}",
                )
            checks.append((name, str(status)))
            pending = True
        # F-75: the check's own output summary, when GitHub holds one
        # (Actions writes it; other apps may too). A non-dict output or a
        # non-str summary is absence, not unresolvable — the summary is
        # evidence beside the conclusion, never the conclusion itself.
        output = run.get("output")
        summary = output.get("summary") if isinstance(output, dict) else None
        if isinstance(summary, str) and summary.strip():
            summaries.append((name, summary[:_CI_SUMMARY_CAP]))
    if status_doc is not None:
        entries = status_doc.get("statuses")
        if entries is None:
            entries = []  # no statuses field: nothing reported this way
        if not isinstance(entries, list):
            return CIState("unresolvable",
                           detail="the status answer's statuses is not a list")
        for entry in entries:
            if not isinstance(entry, dict):
                return CIState("unresolvable",
                               detail=f"a status entry is not an object: {entry!r}"[:200])
            context, state = entry.get("context"), entry.get("state")
            if not isinstance(context, str) or not context:
                return CIState("unresolvable",
                               detail="a status entry has no usable context")
            if not isinstance(state, str):
                return CIState("unresolvable",
                               detail=f"status entry {context!r} has no state")
            checks.append((context, state))
            if isinstance(entry.get("description"), str) \
                    and entry["description"].strip():
                summaries.append((context, entry["description"][:_CI_SUMMARY_CAP]))
            if state in _CI_STATUS_FAILED:
                failed = True
            elif state == "pending":
                pending = True
            elif state != "success":
                return CIState(
                    "unresolvable",
                    detail=f"status entry {context!r} has unknown state {state!r}",
                )
    if failed:
        return CIState("failed", tuple(checks), summaries=tuple(summaries))
    if pending:
        return CIState("pending", tuple(checks), summaries=tuple(summaries))
    if not checks:
        return CIState("none")
    return CIState("success", tuple(checks), summaries=tuple(summaries))


def _gh_api(gh: str, args: list[str], _run: Callable) -> subprocess.CompletedProcess:
    """One ``gh api`` call, with gh's own failures refused loudly."""
    try:
        return _run([gh, "api", *args], capture_output=True, text=True)
    except OSError as exc:
        raise PublishError(
            f"cannot check CI state: gh could not be executed ({exc}) — "
            "the self-enforced green-CI check needs gh"
        ) from exc


def gh_ci_state(
    remote_url: str, sha: str, *, gh_bin: str | None = None,
    _run: Callable = subprocess.run,
) -> CIState:
    """Ask GitHub about every check reported for exactly ``sha``.

    Reads the check-runs API for the exact SHA — latest attempt per
    check name (``filter=latest``), paginated to 100 per page so a repo
    with more checks than one page is judged on ALL of them — and the
    combined commit-status API, since a repo can report through either.

    Args:
        remote_url: The profile's ``repo.remote`` URL.
        sha: The exact commit the answer must be about (never a branch
            name: a moving head cannot be judged).
        gh_bin: gh path override (default: ``shutil.which``).
        _run: subprocess seam for tests (canned gh output).

    Returns:
        The classified :class:`CIState`. gh errors, rc-0 junk and
        non-object JSON return ``unresolvable`` — never guessed.

    Raises:
        factory.publish.PublishError: gh is missing or cannot be
            executed — the CI answer is required, not optional.
    """
    gh = gh_bin or shutil.which("gh")
    if not gh:
        raise PublishError(
            "cannot check CI state: gh is not installed (the factory reads "
            "a SHA's checks with `gh api` for the self-enforced green-CI rule)"
        )
    slug = github_slug(remote_url)
    runs: list[dict] = []
    for page in range(1, _CI_MAX_PAGES + 1):
        proc = _gh_api(
            gh,
            ["-X", "GET", f"repos/{slug}/commits/{sha}/check-runs",
             "-f", "filter=latest", "-f", "per_page=100", "-f", f"page={page}"],
            _run,
        )
        if proc.returncode != 0:
            return CIState(
                "unresolvable",
                detail=f"gh api (check-runs) exited {proc.returncode}: "
                       f"{proc.stderr.strip()[:200]}",
            )
        try:
            doc = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return CIState(
                "unresolvable",
                detail=f"gh api printed no JSON: {proc.stdout.strip()[:120]}",
            )
        if not isinstance(doc, dict):
            return CIState(
                "unresolvable",
                detail=f"gh api printed non-object JSON "
                       f"({type(doc).__name__}): {proc.stdout.strip()[:120]}",
            )
        batch = doc.get("check_runs")
        if not isinstance(batch, list):
            return CIState("unresolvable",
                           detail="the check-runs answer has no check_runs list")
        runs.extend(batch)
        if len(batch) < _CI_PER_PAGE:
            break
    else:
        return CIState(
            "unresolvable",
            detail=f"more than {_CI_MAX_PAGES * _CI_PER_PAGE} check runs for "
                   f"{sha[:12]} — refusing to judge a subset",
        )
    proc = _gh_api(gh, [f"repos/{slug}/commits/{sha}/status"], _run)
    if proc.returncode != 0:
        return CIState(
            "unresolvable",
            detail=f"gh api (status) exited {proc.returncode}: "
                   f"{proc.stderr.strip()[:200]}",
        )
    try:
        status_doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return CIState(
            "unresolvable",
            detail=f"gh api (status) printed no JSON: {proc.stdout.strip()[:120]}",
        )
    if not isinstance(status_doc, dict):
        return CIState(
            "unresolvable",
            detail=f"gh api (status) printed non-object JSON "
                   f"({type(status_doc).__name__}): {proc.stdout.strip()[:120]}",
        )
    return classify_ci(runs, status_doc)


def admission(
    profile: dict, protection: Callable[[str, str], Protection] = gh_branch_protection
) -> None:
    """Refuse to run on a repo whose merge setup can't keep PRs ordered.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        protection: The protection seam (default :func:`gh_branch_protection`;
            tests inject a fake).

    Returns:
        None — admission granted.

    Raises:
        MergeOrderError: The default branch's protection doesn't require
            status checks with ``strict``; or GitHub's plan-limit 403
            arrived without ``repo.branch_protection: unavailable``. The
            message names the missing setting in both cases.
    """
    repo = profile.get("repo") or {}
    branch = repo.get("default_branch", "main")
    declared = repo.get("branch_protection") == "unavailable"
    state = protection(repo.get("remote", ""), branch)
    setting = (
        f"default branch {branch!r} must require status checks with "
        "'strict' (branches up to date) before merging"
    )
    if state.checked and state.strict:
        return
    if state.plan_limit:
        if declared:
            return  # the factory enforces the equivalent itself (sweep)
        raise MergeOrderError(
            f"{setting}: GitHub answers the protection call with its plan "
            "limit (403, e.g. a private repo on a free plan). Declare "
            "`repo.branch_protection: unavailable` in project.yaml to have "
            "the factory enforce up-to-date itself, or enable protection."
        )
    raise MergeOrderError(
        f"{setting}: {state.detail or 'branch protection is not verifiable'}"
        + (" (repo.branch_protection: unavailable does not excuse a repo "
           "that could have protection)"
           if declared else "")
    )
