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
from typing import Callable

from factory.contract import ContractError
from factory.publish import github_slug

__all__ = [
    "MergeOrderError",
    "Protection",
    "admission",
    "classify_protection",
    "gh_branch_protection",
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
        return Protection(detail="branch protection is off (404 not protected)")
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
        from factory.publish import PublishError

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
           if declared and state.checked else "")
    )
