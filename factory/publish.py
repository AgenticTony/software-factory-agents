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

import base64
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from factory.attribution import attribution_match, title_attribution_match
from factory.egress import check_secrets
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
        TypeError: ``secrets`` is a bare ``str``
            (:func:`factory.egress.check_secrets` — round 2's class fix).
    """
    secrets = check_secrets(secrets)
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


def _b64_core(raw: bytes, alignment: int,
              encoder=base64.b64encode) -> str:
    """The base64 chars that encode ``raw``'s bits and nothing else,
    when ``raw`` starts ``alignment`` bytes (0/1/2) into the stream.

    A secret embedded mid-stream is not the base64 of the secret: the
    chars around its byte range mix in the neighbours' bits. A char is
    pure (depends only on ``raw``) iff its 6-bit window [6j, 6j+6) lies
    inside the secret's bit range [8·alignment, 8·(alignment+len)) —
    so the core starts at char ⌈8·alignment/6⌉ and ends at char
    ⌊8·(alignment+len)/6⌋. The three cores together cover every
    alignment a stream can present; each is long enough to match only
    on the secret (MIN_SECRET_LEN=8 keeps the shortest core ≥ 10
    chars)."""
    encoded = encoder(b"\x00" * alignment + raw).decode("ascii")
    start = -(-8 * alignment // 6)  # ceil
    end = (8 * (alignment + len(raw))) // 6
    return encoded[start:end]


def _secret_forms(secret: str) -> "list[tuple[str, str]]":
    """Every ``(label, needle)`` the push-content fence matches.

    The literal and the simple encodings — hex (both cases),
    URL-percent-encoded, base64 and base64url at all three byte
    alignments. Shape scanning is inherently bypassable (gzip,
    reversal, splitting defeat it); the literal plus the trivially
    computable encodings are the boundary, and the refusal names which
    form it caught so the owner can judge the neighbourhood."""
    raw = secret.encode("utf-8")
    forms = [
        ("literal", secret),
        ("hex", raw.hex()),
        ("hex", raw.hex().upper()),
        ("url-encoded", urllib.parse.quote(secret, safe="")),
    ]
    for encoder, label in (
        (base64.b64encode, "base64"),
        (base64.urlsafe_b64encode, "base64url"),
    ):
        for alignment in range(3):
            core = _b64_core(raw, alignment, encoder)
            if core:
                forms.append((label, core))
    return forms


# F-62 round 1: the push-content scan reads object bytes in chunks of
# this size (streamed, never one whole string), carrying an overlap of
# longest-needle − 1 bytes between chunks so a needle cannot straddle a
# boundary unseen. Public so the boundary-straddling test can aim at it.
PUSH_SCAN_CHUNK = 1 << 20  # 1 MiB


def check_push_content(
    repo: Path,
    base_sha: str,
    head_sha: str,
    *,
    secrets: "Sequence[str]" = (),
) -> None:
    """Refuse a push whose OBJECTS carry a session secret (F-62 AC-2)
    — the fence :func:`check_pr_text` gives the PR TEXT, applied to the
    PR's main content: everything the push transfers.

    The orchestrator calls this directly after ``check_pr_text`` and
    before :func:`push_branch``, with the same ``egress_secrets`` tuple
    (the seat token today). Nothing is pushed: the refusal is the same
    :class:`PushRefused` the PR-text fence raises, so the existing
    escalation path (``publish_refused``) handles it unchanged.

    What is scanned — exactly what leaves. ``git push`` transfers every
    object reachable from ``head`` that the remote lacks: intermediate
    commits' trees and blobs (round 1: an add-then-delete pair or an
    orphan branch merged ``-s ours`` publishes objects the NET DIFF of
    base..head never shows) and every parent of a merge. So the scan is
    over the object set — ``git rev-list --objects base..head`` — with
    every object streamed through ONE hardened ``git cat-file
    --batch``: blobs raw, trees raw (an entry NAME carrying the secret
    is caught in the tree's bytes), commits raw (message AND
    author/committer fields). ``cat-file`` serves object bytes; it
    never applies a textconv or diff driver (a repo ``.gitattributes``
    or the owner's global diff config cannot change what the scan
    sees), so diff-side flags are moot by construction.

    Objects are scanned STREAMED, in :data:`PUSH_SCAN_CHUNK` chunks
    with a longest-needle − 1 byte overlap (a needle cannot straddle a
    chunk boundary unseen); nothing is buffered whole — a 50 MB publish
    costs kilobytes, not the diff text times six.

    Secrets shorter than the scrub floor are ignored
    (:func:`factory.sentinels.is_scrubbable_secret` — the same rule the
    PR-text fence and the egress seam apply; ``egress_secrets`` is
    ``(seat_token() or "",)``, and an empty token must match nothing).

    The refusal names the object kind and its path (or the commit SHA)
    and the encoding — never the secret or the matched bytes: the
    message travels into escalation notes the owner reads. A path that
    ITSELF carries a secret form is needle-redacted before naming.

    Fail closed: a failed rev-list/cat-file, a missing object, a stream
    that ends early, or a scan that exceeds the remote-call timeout is
    a refusal — an unreadable corpus is never an OK one.

    Args:
        repo: The clone holding the validated ``head_sha``.
        base_sha: The merge-base the branch grew from.
        head_sha: The commit L0/L1/L2 validated.
        secrets: The run's egress secrets (see
            :func:`factory.egress.check_secrets` for the shape rules).

    Raises:
        PushRefused: A secret (in a matched form) appears in an object
            being published; or the object corpus could not be read
            (fail closed).
        TypeError: ``secrets`` is a bare ``str`` or holds a non-str
            element (:func:`factory.egress.check_secrets`).
    """
    secrets = check_secrets(secrets)
    needles: list[tuple[str, str, bytes]] = [
        (label, needle, needle.encode("utf-8"))
        for secret in secrets
        if is_scrubbable_secret(secret)
        for label, needle in _secret_forms(secret)
    ]
    if not needles:
        return
    longest = max(len(encoded) for _label, _needle, encoded in needles)

    def redact_needles(text: str) -> str:
        """Blank every needle span — the refusal names the file, and a
        file whose NAME carries the secret must not echo it."""
        for _label, needle, _encoded in needles:
            text = text.replace(needle, "[redacted]")
        return text

    def refuse(where: str, kind: str, label: str) -> "PushRefused":
        return PushRefused(
            f"refusing to push: {redact_needles(where)} "
            f"(a {kind} in the published range "
            f"{base_sha[:12]}..{head_sha[:12]}) carries a literal "
            f"session credential in its {label} form (not echoed here) "
            "— nothing was pushed"
        )

    repo = Path(repo)
    listing = remote_call(
        [
            "git", *GIT_HARDENING, "-C", str(repo),
            "rev-list", "--objects", f"{base_sha}..{head_sha}",
        ],
        what="secret scan object listing of the published range",
    )
    if listing.returncode != 0:
        raise PushRefused(
            "cannot list the published objects for the secret scan "
            f"(git rev-list {base_sha[:12]}..{head_sha[:12]} failed: "
            f"{listing.stderr.strip()[:300]}) — nothing was pushed"
        )
    objects: list[tuple[str, str]] = []
    for line in listing.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, path = line.partition(" ")
        objects.append((sha, path.strip()))
    if not objects:
        return

    deadline = time.monotonic() + NET_TIMEOUT_SEC
    batch = subprocess.Popen(
        ["git", *GIT_HARDENING, "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**os.environ, **GIT_NET_ENV},
    )
    assert batch.stdin is not None and batch.stdout is not None
    try:
        for sha, path in objects:
            where = path or sha[:12]
            try:
                batch.stdin.write((sha + "\n").encode())
                batch.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise PushRefused(
                    "cannot scan the published objects for session "
                    f"secrets (cat-file died: {exc}) — nothing was pushed"
                ) from exc
            header = _batch_read_line(batch.stdout, deadline)
            # A served object answers "<sha> <kind> <size>"; a missing
            # one answers "<sha> missing" (two tokens — caught by the
            # length guard, with the kind check naming it for the
            # reader). Empty/garbage headers fail closed the same way.
            parts = header.split() if header else []
            if len(parts) < 3 or parts[1] == b"missing":
                raise PushRefused(
                    f"cannot scan the published objects: {sha[:12]} "
                    "is missing from the local object store — nothing "
                    "was pushed"
                )
            kind = parts[1].decode(errors="replace")
            try:
                size = int(parts[2])
            except ValueError:
                raise PushRefused(
                    f"cannot scan the published objects: unreadable "
                    f"cat-file header for {sha[:12]} — nothing was pushed"
                ) from None
            # Stream the object bytes: chunked, with an overlap of
            # longest-needle − 1 bytes so no needle straddles unseen.
            carry = b""
            remaining = size
            while remaining > 0:
                want = min(PUSH_SCAN_CHUNK, remaining)
                data = _batch_read_exactly(batch.stdout, want, deadline)
                remaining -= want
                window = carry + data
                for label, _needle, encoded in needles:
                    if encoded in window:
                        raise refuse(where, kind, label)
                carry = window[-(longest - 1):] if longest > 1 else b""
            _batch_read_exactly(batch.stdout, 1, deadline)  # trailing \n
        try:
            batch.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        returncode = batch.wait(
            timeout=max(1.0, deadline - time.monotonic()))
        if returncode != 0:
            raise PushRefused(
                "cannot scan the published objects: git cat-file exited "
                f"{returncode} — nothing was pushed"
            )
    finally:
        if batch.poll() is None:
            batch.kill()
            batch.wait()


def _batch_read_exactly(stream, count: int, deadline: float) -> bytes:
    """Read exactly ``count`` bytes from cat-file's stdout, bounded by
    ``deadline``; EOF or a stall is a PushRefused (fail closed)."""
    buffer = bytearray()
    fd = stream.fileno()
    while len(buffer) < count:
        left = deadline - time.monotonic()
        if left <= 0:
            raise PushRefused(
                "cannot scan the published objects: git cat-file "
                "stalled — nothing was pushed"
            )
        ready, _, _ = select.select([fd], [], [], min(left, 5.0))
        if not ready:
            continue
        chunk = os.read(fd, count - len(buffer))
        if not chunk:
            raise PushRefused(
                "cannot scan the published objects: the stream ended "
                "early — nothing was pushed"
            )
        buffer += chunk
    return bytes(buffer)


def _batch_read_line(stream, deadline: float) -> bytes:
    """One ``\\n``-terminated cat-file header line, bounded by
    ``deadline``; b'' on EOF (the caller fails closed)."""
    line = bytearray()
    while True:
        byte = _batch_read_exactly(stream, 1, deadline)
        if byte == b"\n":
            return bytes(line)
        line += byte
        if len(line) > 4096:  # a header is a sha, a kind, a size
            raise PushRefused(
                "cannot scan the published objects: unreadable cat-file "
                "header — nothing was pushed"
            )


class PublishError(Exception):
    """git push or gh failed."""


class RemoteCallTimeout(PublishError):
    """A prompt-disabled remote call exceeded its timeout (F-45 AC-3).

    Subclasses :class:`PublishError` so the publish paths, the concurrency
    count and the CLI all keep their existing error currency; the
    orchestrator's fetch sites re-wrap it as ``RunError``.
    """


# F-45 AC-3 (M-7): every remote call — publish-side push, the ls-remote
# readback, ``gh pr create``, and the orchestrator's fetches/ls-remote —
# runs through :func:`remote_call`, prompt-disabled and time-bounded. A
# private HTTPS remote with an expired credential helper would otherwise
# sit git on a username prompt (invisible: /dev/tty) while the run lease
# is held.
GIT_NET_ENV = {"GIT_TERMINAL_PROMPT": "0"}
# gh does NOT honour GIT_TERMINAL_PROMPT. gh's own switch is
# GH_PROMPT_DISABLED (``gh help environment``: "set to any value to
# disable interactive prompting in the terminal"), which kills its
# interactive auth/device-flow prompts; GIT_TERMINAL_PROMPT stays set for
# any git credential helper gh itself shells out to. The wrapper's timeout
# is the provable backstop: whatever gh does, the call cannot outlive it.
GH_NET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
NET_TIMEOUT_SEC = 300


def remote_call(cmd: Sequence[str], *, env_extra: dict | None = None,
                timeout_sec: int | None = None,
                what: str | None = None) -> subprocess.CompletedProcess:
    """Run ONE remote git/gh call, prompt-disabled and time-bounded.

    The single wrapper every remote call goes through (F-45 AC-3, the
    mechanism ``_fetch_sha`` pioneered in round 3 of the F-26 review — one
    mechanism, not one per call site). ``GIT_TERMINAL_PROMPT=0`` by default;
    gh callers pass ``env_extra=GH_NET_ENV``. The default timeout resolves
    from :data:`NET_TIMEOUT_SEC` at CALL time (tests shrink it by patching
    the module attribute).

    Args:
        cmd: The command to run (git/gh and arguments).
        env_extra: Env overrides merged over ``os.environ`` (default
            :data:`GIT_NET_ENV`).
        timeout_sec: Timeout in seconds (default :data:`NET_TIMEOUT_SEC`).
        what: A human label for the timeout message (default: the first
            three argv words).

    Returns:
        The completed process (captured stdout/stderr).

    Raises:
        RemoteCallTimeout: The call exceeded ``timeout_sec`` — a hung
            credential helper, an auth flow or a server that never
            answered, never a prompt waiting for the owner.
    """
    timeout = NET_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    env = {**os.environ, **(GIT_NET_ENV if env_extra is None else env_extra)}
    label = what or " ".join(str(part) for part in list(cmd)[:3])
    try:
        return subprocess.run(
            [str(part) for part in cmd], capture_output=True, text=True,
            env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteCallTimeout(
            f"{label} timed out after {timeout}s — prompts were disabled on "
            "the call (GIT_TERMINAL_PROMPT=0 and, for gh, GH_PROMPT_DISABLED=1), "
            "so this is a hung credential helper, an auth flow or a server "
            "that never answered, not a prompt waiting for you"
        ) from exc


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
            PR may be opened about it). A call that exceeds its timeout
            raises :class:`RemoteCallTimeout` (a PublishError) — the run
            lease is never held open by a hung remote (F-45 AC-3).
    """
    ref = check_branch(branch, created=created, default_branch=default_branch)
    if not _SHA_RE.match(sha or ""):
        raise PushRefused(
            f"refusing to push {branch!r}: the validated commit must be a full "
            f"sha, got {sha!r}"
        )
    proc = remote_call(
        [
            "git", *GIT_HARDENING, "-C", str(repo), "push", "--no-follow-tags",
            "--porcelain", remote_url, f"{sha}:{ref}",
        ],
        what=f"git push of {ref}",
    )
    if proc.returncode != 0:
        raise PublishError(f"git push of {ref} failed: {proc.stderr.strip()[:1000]}")
    probe = remote_call(
        ["git", *GIT_HARDENING, "-C", str(repo), "ls-remote", remote_url, ref],
        what=f"ls-remote readback of {ref}",
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
        PublishError: gh is missing or fails. gh runs prompt-disabled
            (``GH_PROMPT_DISABLED=1`` + ``GIT_TERMINAL_PROMPT=0``) and
            time-bounded through :func:`remote_call` (F-45 AC-3): an auth
            prompt or a hung API cannot hold the run lease.
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
        proc = remote_call(
            [
                gh, "pr", "create", "--repo", slug, "--base", pr.base, "--head", pr.head,
                "--title", pr.title, "--body-file", str(body_path),
            ],
            env_extra=GH_NET_ENV, what=f"gh pr create ({pr.head} into {pr.base})",
        )
    finally:
        body_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise PublishError(f"gh pr create failed: {proc.stderr.strip()[:1000]}")
    urls = re.findall(r"https://\S+/pull/\d+", proc.stdout)
    if not urls:
        raise PublishError(f"gh pr create printed no PR URL: {proc.stdout.strip()[:500]}")
    return urls[-1]
