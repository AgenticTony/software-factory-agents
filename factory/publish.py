"""Publishing a validated branch: the push and ``gh pr create`` (F-10 AC-13).

These are the only two places the factory acts on the outside world with
the orchestrator's credentials, so both are fenced in code:

* :func:`push_branch` pushes exactly one ref — the run's own branch,
  pointed at **the validated commit**, by an explicit
  ``<sha>:refs/heads/X`` refspec, to the profile's remote URL (never a
  configured remote name, so no ``remote.*.push``/``mirror`` config
  applies), with no force and no tags. The source is the object name, not
  the branch: the branch lives in the agent-owned work clone and a
  surviving implementer process (F-06 documents setsid escapees) could
  move it after L0/L1/L2 validated the head — pushing the sha publishes
  the validated tree whatever the branch points at now. After the push,
  ``ls-remote`` must read the ref back as exactly that sha, or the
  publish fails before any PR opens. It refuses — **before any git
  process starts** — the default branch, any branch outside the
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
from typing import Sequence

from factory.attribution import attribution_match, title_attribution_match
from factory.runs import GIT_HARDENING
from factory.sentinels import is_scrubbable_secret

BRANCH_PREFIX = "factory/"
_BRANCH_RE = re.compile(r"^factory/[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
# Fenced code blocks in a PR body (:func:`factory.prbody.fence` output —
# a fence line of N backticks closed by another of exactly N). The
# implementer's notes and the validator report's JSON ride inside them
# and may legitimately QUOTE a trailer — a reviewer finding that quotes
# "Co-Authored-By: Claude …" is evidence, not attribution. An unclosed
# fence never matches, so its content stays scanned (fail closed).
_FENCE_RE = re.compile(r"^(`{3,})[^\n]*\n[\s\S]*?^\1[ \t]*$", re.M)


def check_pr_text(title: str, body: str, *, secrets: Sequence[str] = ()) -> None:
    """Refuse a PR whose title or body carries a literal session secret
    (F-43 AC-2, C-1), or whose FREE TEXT carries an AI attribution (F-42
    AC-2; PR #89 review finding 2).

    The literal check scans title AND body — fences included — for every
    caller-known secret (the orchestrator passes the seat token): it is a
    second, independent fence that assumes the egress seam
    (:func:`factory.egress.scrub_session_text`) may have FAILED, so it
    never depends on the body having been scrubbed. The refusal names
    itself without echoing the token (the message travels into escalation
    notes the owner reads).

    Fenced blocks are skipped by the attribution half only (quoted
    material is evidence, not attribution); the title and every unfenced
    line are the PR's own words and may not carry
    :data:`factory.attribution.ATTRIBUTION_PATTERNS`. A squash commit
    builds its message from this title and the branch's commits — L0
    already gates the commits — so refusing the free text closes the
    inheritance path. The orchestrator calls this BEFORE
    :func:`push_branch`; :func:`gh_pr_create` calls it again as the
    attribution seam's backstop (without secrets — it has none; the
    literal fence is the orchestrator-side call).

    Raises:
        PushRefused: A literal secret appears in the title or body (named,
            not echoed), or the free text matched an attribution pattern.
            Raised before any git or gh process starts.
    """
    for secret in secrets:
        # The ONE rule for what counts as a secret (review round 1,
        # finding 10): shared with scrub_literal via
        # :func:`factory.sentinels.is_scrubbable_secret`, so F-59 cannot
        # change the seam's rule and leave the fence's behind.
        if is_scrubbable_secret(secret) and (secret in title or secret in body):
            raise PushRefused(
                "refusing to open a PR whose title or body carries a "
                "literal session credential (a seat token; not echoed "
                "here) — the egress seam should have scrubbed it; "
                "nothing was pushed"
            )
    match = title_attribution_match(title) or attribution_match(
        _FENCE_RE.sub("", body)
    )
    if match is not None:
        name, line = match
        raise PushRefused(
            f"refusing to open a PR whose title/body carries an AI "
            f"attribution ({name}): {line[:200]!r} — remove it; a human "
            "Co-Authored-By passes, machine attribution does not"
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
    sha: str,
    created: set[str] | frozenset[str],
    default_branch: str,
) -> None:
    """Push the validated commit, and nothing else, to the remote URL.

    Args:
        repo: The clone holding the validated ``sha``.
        remote_url: The profile's ``repo.remote`` URL (or a path, in tests).
        branch: The branch to publish (``factory/...``).
        sha: The commit L0/L1/L2 validated — the object actually pushed;
            the branch name is only the remote label it lands on.
        created: Branch names this run created; anything else is refused.
        default_branch: ``repo.default_branch`` — never pushed.

    Raises:
        PushRefused: See :func:`check_branch`, or ``sha`` isn't a full
            object name; raised before git runs.
        PublishError: The push failed, or the remote ref did not read
            back as exactly ``sha`` (the publish is unconfirmed and no
            PR may be opened about it).
    """
    ref = check_branch(branch, created=created, default_branch=default_branch)
    if not _SHA_RE.match(sha or ""):
        raise PushRefused(
            f"refusing to push {branch!r}: the validated commit must be a full "
            f"sha, got {sha!r}"
        )
    proc = subprocess.run(
        [
            "git", *GIT_HARDENING, "-C", str(repo), "push", "--no-follow-tags",
            "--porcelain", remote_url, f"{sha}:{ref}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise PublishError(f"git push of {ref} failed: {proc.stderr.strip()[:1000]}")
    probe = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), "ls-remote", remote_url, ref],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise PublishError(
            f"could not confirm {ref} on the remote (ls-remote failed): "
            f"{probe.stderr.strip()[:500]}"
        )
    fields = probe.stdout.split()
    got = fields[0] if fields else ""
    if got != sha:
        raise PublishError(
            f"remote {ref} reads {got or 'absent'}, not the validated commit "
            f"{sha}: refusing to open a PR about an unconfirmed publish"
        )


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
        PushRefused: ``pr.head`` is not a factory branch or is the base, or
            the title or body's free text carries an AI-attribution
            pattern (:func:`check_pr_text` — F-42 AC-2, review finding 2:
            fenced blocks are skipped, and the orchestrator already
            refused before the push; this is the seam's backstop, before
            gh runs).
        PublishError: gh is missing or fails.
    """
    if pr.head == pr.base or not _BRANCH_RE.match(pr.head):
        raise PushRefused(f"refusing to open a PR from {pr.head!r} into {pr.base!r}")
    check_pr_text(pr.title, pr.body)
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
