"""Publishing a validated branch: the push and ``gh pr create`` (F-10 AC-13).

These are the only two places the factory acts on the outside world with
the orchestrator's credentials, so both are fenced in code:

* :func:`push_branch` pushes exactly one ref — the run's own branch, by an
  explicit ``refs/heads/X:refs/heads/X`` refspec, to the profile's remote
  URL (never a configured remote name, so no ``remote.*.push``/``mirror``
  config applies), with no force and no tags. It refuses — **before any
  git process starts** — the default branch, any branch outside the
  ``factory/`` namespace, any branch the run did not create, and anything
  that isn't a plain branch name (refspec syntax, ``+`` force, ``HEAD``).
  The factory's own repo can't have branch protection on a free private
  plan, so this check is the only guard on its ``main``.
* :func:`gh_pr_create` opens the PR. The factory never merges: merging is
  the owner's act (TASK-CONTRACT §3, v0 everything is ``merge: human``).

Every git call uses :data:`factory.runs.GIT_HARDENING` (no hooks, no
fsmonitor): the run's clone was written by an agent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from factory.runs import GIT_HARDENING

BRANCH_PREFIX = "factory/"
_BRANCH_RE = re.compile(r"^factory/[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class PushRefused(Exception):
    """The orchestrator refused to push or open a PR for this ref."""


class PublishError(Exception):
    """git push or gh failed."""


@dataclass(frozen=True)
class PullRequest:
    """What the orchestrator asks the PR seam to open."""

    remote: str
    base: str
    head: str
    title: str
    body: str


def check_branch(branch: str, *, created: set[str] | frozenset[str], default_branch: str) -> str:
    """Validate a branch the orchestrator is about to publish.

    Returns:
        The full ref name ``refs/heads/<branch>``.

    Raises:
        PushRefused: The branch is the default branch, outside the
            ``factory/`` namespace, not created by this run, or not a plain
            branch name.
    """
    if not isinstance(branch, str) or not branch:
        raise PushRefused(f"refusing to publish an empty branch name: {branch!r}")
    if branch in (default_branch, f"refs/heads/{default_branch}") or branch.endswith(
        f":refs/heads/{default_branch}"
    ):
        raise PushRefused(f"refusing to push the default branch {default_branch!r}")
    if not _BRANCH_RE.match(branch) or ".." in branch or branch.endswith((".lock", ".")):
        raise PushRefused(
            f"refusing to push {branch!r}: the orchestrator only pushes plain branch "
            f"names under {BRANCH_PREFIX!r} (no refspecs, no force, no HEAD)"
        )
    if branch not in created:
        raise PushRefused(f"refusing to push {branch!r}: this run did not create it")
    return f"refs/heads/{branch}"


def push_branch(
    repo: Path,
    remote_url: str,
    branch: str,
    *,
    created: set[str] | frozenset[str],
    default_branch: str,
) -> None:
    """Push the run's own branch, and nothing else, to the remote URL.

    Args:
        repo: The clone holding ``refs/heads/<branch>``.
        remote_url: The profile's ``repo.remote`` URL (or a path, in tests).
        branch: The branch to publish (``factory/...``).
        created: Branch names this run created; anything else is refused.
        default_branch: ``repo.default_branch`` — never pushed.

    Raises:
        PushRefused: See :func:`check_branch`; raised before git runs.
        PublishError: The push itself failed.
    """
    ref = check_branch(branch, created=created, default_branch=default_branch)
    proc = subprocess.run(
        [
            "git", *GIT_HARDENING, "-C", str(repo), "push", "--no-follow-tags",
            "--porcelain", remote_url, f"{ref}:{ref}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise PublishError(f"git push of {ref} failed: {proc.stderr.strip()[:1000]}")


def github_slug(remote_url: str) -> str:
    """``owner/repo`` from a GitHub remote URL.

    Raises:
        PublishError: The remote isn't a GitHub URL.
    """
    match = _GITHUB_REMOTE.match(remote_url.strip())
    if not match:
        raise PublishError(f"cannot open a PR: {remote_url!r} is not a GitHub remote")
    return match.group("slug")


def gh_pr_create(pr: PullRequest, *, gh_bin: Path | str | None = None) -> str:
    """Open the PR with ``gh pr create`` and return its URL.

    The body travels through a temp file outside every clone (argv has a
    size limit and shows up in ``ps``).

    Raises:
        PushRefused: ``pr.head`` is not a factory branch or is the base.
        PublishError: gh is missing or fails.
    """
    if pr.head == pr.base or not _BRANCH_RE.match(pr.head):
        raise PushRefused(f"refusing to open a PR from {pr.head!r} into {pr.base!r}")
    gh = str(gh_bin) if gh_bin else shutil.which("gh")
    if not gh:
        raise PublishError("gh is not installed: the factory opens PRs with `gh pr create`")
    slug = github_slug(pr.remote)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(pr.body)
        body_path = Path(fh.name)
    try:
        proc = subprocess.run(
            [
                gh, "pr", "create", "--repo", slug, "--base", pr.base, "--head", pr.head,
                "--title", pr.title, "--body-file", str(body_path),
            ],
            capture_output=True,
            text=True,
        )
    finally:
        body_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise PublishError(f"gh pr create failed: {proc.stderr.strip()[:1000]}")
    urls = re.findall(r"https://\S+/pull/\d+", proc.stdout)
    if not urls:
        raise PublishError(f"gh pr create printed no PR URL: {proc.stdout.strip()[:500]}")
    return urls[-1]
