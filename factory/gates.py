"""Shared primitives for the validator gates (VALIDATOR-SPEC.md).

Every deterministic layer (L0, L1) speaks the same language — findings,
command results, git access, diff parsing, test-file recognition and the
contract's named-test refs — so that language lives here once instead of
drifting between layer modules. The layer modules (``factory.l0``,
``factory.l1``) own their policies; this module owns the machinery.
"""

import fnmatch
import functools
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar

from factory.runs import GIT_HARDENING

# Conventional test-path globs, matched with fnmatch semantics (``*``
# crosses ``/``). ``conftest.py`` at any depth is test infrastructure:
# L1's red phase must apply it with the diff's test files, or a diff that
# ships a new fixture can turn a test that would pass on base into an
# honest-looking red (the exact false green L1 exists to catch). Projects
# with unusual layouts should name their test files in the contract
# (which protects them) until per-project overrides exist.
TEST_GLOBS = (
    "tests/*",
    "*/tests/*",
    "test/*",
    "*/test/*",
    "*__tests__/*",
    "test_*.py",
    "*/test_*.py",
    "*_test.py",
    "conftest.py",
    "*/conftest.py",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.js",
)

# Pin git diff output format: user config (color, noprefix, external diff
# drivers) must not change what the parsers see.
_DIFF_FORMAT = ("--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/")


class GateError(Exception):
    """A gate cannot run as configured (git failure, bad refs, bad state)."""


@dataclass
class Finding:
    """A deterministic gate finding. FAIL-class findings decide the verdict."""

    id: str
    text: str
    file: str | None = None
    line: int | None = None


@dataclass
class CommandResult:
    """One executed command."""

    name: str
    command: str
    exit_code: int | None
    output: str
    timed_out: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output,
            "timed_out": self.timed_out,
        }


@dataclass
class FileChange:
    """One changed path from ``git diff --name-status -M``.

    ``added_lines``/``removed_lines`` carry ``(line_number, text)`` pairs
    from the unified diff (new-file / old-file numbering respectively);
    name-status-only diffs leave them empty. For renames, ``path`` is the
    new path, ``old_path`` the previous one.
    """

    path: str
    status: str
    old_path: str | None = None
    added_lines: list = field(default_factory=list)
    removed_lines: list = field(default_factory=list)


@dataclass
class GateReport:
    """The fields every gate report carries; layers subclass with extras.

    Keeping the shared serialisation here means the layer reports F-09
    and F-10 consume cannot drift apart in shape.
    """

    layer: ClassVar[str] = ""  # set by each subclass
    verdict: str
    findings: list = field(default_factory=list)
    command_results: list = field(default_factory=list)

    def findings_as_dicts(self) -> list:
        return [dict(f.__dict__) for f in self.findings]

    def commands_as_dicts(self) -> list:
        return [c.to_dict() for c in self.command_results]


def matches(path: str, patterns) -> bool:
    """True when the posix relpath matches any glob (fnmatch, case-sensitive)."""
    return any(fnmatch.fnmatchcase(path, pat) for pat in patterns)


def scope_matches(path: str, patterns) -> bool:
    """True when ``path`` matches any DECLARED-SCOPE glob (F-65).

    Scope semantics — deliberately stricter than :func:`matches`:

    * ``*`` and ``?`` never cross ``/``: a scope is a list of places,
      and ``src/*.py`` must not reach ``src/deep/x.py``;
    * ``**`` as a whole segment matches zero or more whole directories;
    * matching is case-sensitive on the exact path string git reports —
      a case variant or Unicode-normalisation look-alike of an in-scope
      path is OUT of scope.

    :func:`matches` (protected/risk paths) keeps fnmatch semantics on
    purpose: there ``*`` crossing ``/`` is the broader, fail-safe
    reading — a protected or risk glob should over-match, while a scope
    glob is an allowlist and must under-match. Character classes
    (``[...]``) are NOT supported here; the load-time refusals
    (:func:`factory.contract.scope_pattern_error`) reject them so a
    declared scope never silently means something else.

    Args:
        path: The repo-relative posix path git reported (exact bytes,
            already through ``core.quotePath`` decoding).
        patterns: The scope globs (task ``scope.paths`` or profile
            ``paths.generated``).

    Returns:
        Whether any pattern matches the whole path.
    """
    segments = path.split("/")
    return any(
        _scope_glob_match(tuple(pat.split("/")), tuple(segments))
        for pat in patterns
    )


@functools.lru_cache(maxsize=4096)
def _scope_glob_match(pat_segs: tuple, path_segs: tuple) -> bool:
    """Segment-wise glob match: ``**`` eats zero or more path segments,
    every other segment matches within itself (``*``/``?`` never see a
    ``/`` because the comparison is per-segment). Memoised on the
    (pattern, path) segment tuples: a pattern stacking ``**`` segments
    would otherwise revisit the same suffix pairs exponentially (the
    regex-CPU finding's cousin — patterns are owner-approved, but the
    bound costs nothing). The cache is BOUNDED: a long-lived `factory
    loop` or dashboard process matches unbounded distinct paths, and an
    unbounded memo would grow with them."""
    if not pat_segs:
        return not path_segs
    head, rest = pat_segs[0], pat_segs[1:]
    if head == "**":
        # zero or more directories: try consuming 0..len(path) segments
        return any(
            _scope_glob_match(rest, path_segs[i:])
            for i in range(len(path_segs) + 1)
        )
    if not path_segs or not fnmatch.fnmatchcase(path_segs[0], head):
        return False
    return _scope_glob_match(rest, path_segs[1:])


def scope_risk_flags(scope_paths, risk_globs: dict, tracked_files, *,
                     generated_globs=(), extra_files=()) -> dict:
    """The risk flags a contract's declared scope can reach (F-71 AC-1).

    ONE implementation, shared by approval (:mod:`factory.approve`) and
    the importer's normalisation (:mod:`factory.importer`) — the same
    rule that judges a scope at approval judges the proposal the
    importer writes. Candidates:

    * every tracked file on the repo's DEFAULT branch that matches any
      scope glob (:func:`scope_matches` — the caller supplies
      :func:`default_branch_files`, never the working tree: a file that
      exists only untracked is not a place this contract reaches);
    * L0's implicit allowances (R1-7): the tracked files matching the
      profile's ``paths.generated`` globs, plus every named contract
      test file (``extra_files`` — files the task will create), so a
      risk glob covering what the run may legitimately touch demands
      its flag at approval exactly as L0's matcher will at run time;
    * plus every literal (glob-free) scope path, which covers files the
      task will create that do not exist yet.

    Each candidate goes through the SAME risk matcher L0 uses
    (:func:`matches` via :func:`factory.sentinels.matched_risk_flags`'s
    rule — fnmatch, ``*`` crossing ``/``, over-match on purpose), never
    a second matcher.

    Args:
        scope_paths: The task's ``scope.paths`` globs.
        risk_globs: The profile's ``paths.risk`` mapping.
        tracked_files: The default branch's tracked repo-relative paths.
        generated_globs: The profile's ``paths.generated`` globs.
        extra_files: The task's own contract test files (repo-relative).

    Returns:
        ``{flag: [reaching paths, in candidate order]}`` for every flag
        at least one candidate reaches; flags with no reach are absent.
    """
    scope_paths = list(scope_paths or [])
    literals = [p for p in scope_paths if isinstance(p, str)
                and not any(c in p for c in "*?")]
    candidates: list[str] = []
    for path in tracked_files:
        if (scope_matches(path, scope_paths)
                or (generated_globs and scope_matches(path, generated_globs))) \
                and path not in candidates:
            candidates.append(path)
    for extra in extra_files:
        if isinstance(extra, str) and extra and extra not in candidates:
            candidates.append(extra)
    for literal in literals:
        if literal not in candidates:
            candidates.append(literal)
    reaching: dict[str, list[str]] = {}
    for flag, globs in (risk_globs or {}).items():
        paths = [p for p in candidates if matches(p, globs)]
        if paths:
            reaching[flag] = paths
    return reaching


def default_branch_files(repo, default_branch: str, *,
                         fetch_timeout_sec: int = 60) -> list:
    """The tracked files of the branch a RUN will build on (R1-3).

    The run builds on the REMOTE default, not the owner checkout's
    local branch (the pilot's checkout sat 11 commits behind origin and
    on another branch) — so when the repo has an ``origin`` remote, it
    is fetched (forced refspec, timed out, prompt-disabled) and the
    tree read from ``refs/remotes/origin/<default>``. A repo with no
    ``origin`` (a fixture, a local-only repo) is its own truth and
    reads its local default branch. Never the working tree.

    Raises:
        GateError: the fetch or the tree listing failed (a fetch
            failure is fail closed — the remote truth is what the run
            will build on, and approving against a stale or unreadable
            one is the exact waste F-71 exists to prevent).
    """
    repo = Path(repo)
    remote = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), "remote", "get-url",
         "origin"],
        capture_output=True, text=True,
    )
    ref = default_branch
    if remote.returncode == 0 and remote.stdout.strip():
        tracking = f"refs/remotes/origin/{default_branch}"
        try:
            subprocess.run(
                ["git", *GIT_HARDENING, "-C", str(repo), "fetch", "-q",
                 "origin", f"+refs/heads/{default_branch}:{tracking}"],
                capture_output=True, text=True, timeout=fetch_timeout_sec,
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError) as exc:
            stderr = getattr(exc, "stderr", "")
            detail = stderr.strip() if isinstance(stderr, str) else str(exc)
            raise GateError(
                f"cannot fetch origin/{default_branch} in {repo} — the run "
                "builds on the remote default and approval judges against "
                f"it: {detail or exc}"
            ) from exc
        ref = tracking
    listing = git(repo, "ls-tree", "-r", "--name-only", ref)
    return [line for line in listing.splitlines() if line.strip()]


def is_test_file(path: str) -> bool:
    return matches(path, TEST_GLOBS)


def ref_path(ref: str) -> str | None:
    """The file part of a ``path::name`` ref, stripped; None without ``::``.

    One shared spelling so L0's protected set and L1's file lookups can
    never disagree about where a (possibly padded) ref points.
    """
    path, sep, _ = ref.partition("::")
    return path.strip() if sep else None


def contract_test_refs(task: dict) -> list:
    """Collect the named-test refs from a task's acceptance checks.

    Every ``check.type: test`` ref, in acceptance order, de-duplicated.
    Malformed refs are kept: they become ``contract_ambiguous`` findings
    in L1 instead of being silently dropped.

    Args:
        task: A task record (may be empty).

    Returns:
        The ordered list of raw ref strings.
    """
    refs: list[str] = []
    for item in task.get("acceptance") or []:
        check = item.get("check") if isinstance(item, dict) else None
        if isinstance(check, dict) and check.get("type") == "test":
            ref = check.get("ref")
            if isinstance(ref, str) and ref and ref not in refs:
                refs.append(ref)
    return refs


def contract_test_files(task: dict) -> set:
    """Collect the test files named in a task's approved acceptance checks.

    Every well-formed ``check.type: test`` ref of the form ``path::name``
    contributes its (stripped) ``path``. These files are protected paths
    for the task's diff, locked at approval (VALIDATOR-SPEC L0): a worker
    cannot weaken the tests that define "done".

    Args:
        task: A task record (may be empty).

    Returns:
        The set of repo-relative test file paths.
    """
    return {p for p in (ref_path(r) for r in contract_test_refs(task)) if p}


def git(repo: Path, *args: str, errors: str = "strict") -> str:
    """Run hardened git in the repo, raising GateError on any failure.

    ``GIT_HARDENING`` (no hooks, no fsmonitor) on every call: the gates
    run on a clone whose history an agent wrote. ``errors`` passes
    through to the decode exactly as :class:`subprocess.Popen` takes it
    — the default strict decode refuses non-UTF-8 output; the one
    caller that must read arbitrary attacker bytes (the per-commit
    attribution scan, F-46) passes ``"replace"`` so no commit message
    can crash or be skipped, while every future hardening of this one
    invocation reaches it too.
    """
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True, text=True, errors=errors,
    )
    if proc.returncode != 0:
        raise GateError(
            f"git {' '.join(args[:2])} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout


def git_rc(repo: Path, *args: str, timeout_sec: int = 60) -> int:
    """Hardened git whose answer IS the exit code — it never raises.

    For the questions git encodes in its status (``merge-base
    --is-ancestor``: 0 yes, 1 no), where raising on non-zero would make
    the "no" answer unrepresentable. Any non-zero — including a missing
    object's fatal — comes back as the code; the caller decides which
    codes mean what. ``timeout_sec`` bounds the call so a stalled object
    fetch can never hang the round (124 on timeout, like ``sh``).
    """
    try:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return 124
    return proc.returncode


def _text(data) -> str:
    """Normalise captured output: POSIX ``TimeoutExpired`` carries raw bytes
    (or ``None``) even when the run asked for text."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def run_command(name: str, command: str, repo: Path, timeout_sec: int):
    """Run one shell command in the repo with a wall-clock cap.

    Unconfined: the orchestrator's own environment. Fixture tests use it;
    F-10 passes the gates a sandboxed :data:`CommandRunner` instead, because
    a project's commands execute the agent's code (``conftest.py``).
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=repo,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        return CommandResult(name, command, proc.returncode, proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired as exc:
        output = _text(exc.stdout) + _text(exc.stderr)
        return CommandResult(name, command, None, output, timed_out=True)


# (name, shell command, repo, timeout_sec) -> CommandResult. The seam L0 and
# L1 run every project command through; F-10 injects the sandboxed one.
CommandRunner = Callable[[str, str, Path, int], "CommandResult"]


def _name_status_rows(status_out: str, head_ref: str) -> list:
    """Parse ``git diff --name-status -M`` rows into bare FileChanges."""
    changes = []
    for row in status_out.splitlines():
        if not row.strip():
            continue
        parts = row.split("\t")
        status_match = re.match(r"^[A-Z]+", parts[0])
        assert status_match is not None  # a git status row starts with letters
        status = status_match.group()
        if status == "U":
            raise GateError(f"unmerged path in diff of {head_ref}: {row}")
        # Exotic paths arrive quoted (single-quoted C style); decode so
        # the rows merge with the hunk parser's decoded paths.
        path = _unquote_git_path(parts[-1])
        old_path = (
            _unquote_git_path(parts[1]) if status == "R" and len(parts) == 3
            else None
        )
        changes.append(FileChange(path, status, old_path))
    return changes


def diff_name_status(repo: Path, base_ref: str, head_ref: str) -> list:
    """Compute the changed files of ``base...head`` without line detail.

    Args:
        repo: Repository (or worktree/clone) holding both refs.
        base_ref: Base ref (compared from the merge-base).
        head_ref: Head ref carrying the slice.

    Returns:
        ``FileChange`` list (path/status/old_path only); renames detected.

    Raises:
        GateError: If git fails or the refs don't resolve.
    """
    status_out = git(
        repo, "diff", *_DIFF_FORMAT, "--name-status", "-M", f"{base_ref}...{head_ref}"
    )
    return _name_status_rows(status_out, head_ref)


def diff_file_changes(repo: Path, base_ref: str, head_ref: str) -> list:
    """Compute the changed files of ``base...head`` with diff line detail.

    Args:
        repo: Repository (or worktree/clone) holding both refs.
        base_ref: Base ref (compared from the merge-base).
        head_ref: Head ref carrying the slice.

    Returns:
        ``FileChange`` list; renames detected (``-M``).

    Raises:
        GateError: If git fails or the refs don't resolve.
    """
    status_out = git(
        repo, "diff", *_DIFF_FORMAT, "--name-status", "-M", f"{base_ref}...{head_ref}"
    )
    diff_out = git(repo, "diff", *_DIFF_FORMAT, "-M", f"{base_ref}...{head_ref}")
    hunks = _parse_unified_diff(diff_out)

    changes = _name_status_rows(status_out, head_ref)
    for change in changes:
        added, removed = hunks.get(change.path, ([], []))
        change.added_lines, change.removed_lines = added, removed
    return changes


def _unquote_git_path(token: str) -> str:
    """Decode a git-quoted path token; unquoted tokens pass through.

    Git quotes paths holding non-ASCII or special characters and emits
    the bytes as C escapes: ``"t\\303\\253st.py"`` is ``tëst.py``.
    Diff headers quote with double quotes, ``--name-status`` rows with
    single; both decode the same way. Octal escapes are raw UTF-8
    bytes; ``\\"``, ``\\\\``, ``\\n`` and ``\\t`` are their literals.
    """
    if len(token) < 2 or token[0] not in "\"'" or token[-1] != token[0]:
        return token
    body = token[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            esc = body[i + 1]
            if esc in ('"', "\\"):
                out += esc.encode()
                i += 2
                continue
            if esc == "n":
                out += b"\n"
                i += 2
                continue
            if esc == "t":
                out += b"\t"
                i += 2
                continue
            digits = body[i + 1:i + 4]
            if len(digits) == 3 and all(c in "01234567" for c in digits):
                out.append(int(digits, 8))
                i += 4
                continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _diff_path(token: str, prefix: str) -> str | None:
    """The path from an ``---``/``+++`` token; None for ``/dev/null``.

    The token may be git-quoted; the ``a/``/``b/`` prefix is stripped
    after decoding (kept when the producer emitted none).
    """
    path = _unquote_git_path(token.strip())
    if path == "/dev/null":
        return None
    if path.startswith(f"{prefix}/"):
        return path[2:]
    return path


def _parse_unified_diff(text: str) -> dict:
    """Key a unified diff by its b-side path, tracking line numbers.

    The ``+++`` line is the authoritative file name: git quotes exotic
    paths (``+++ "b/t\\303\\253st.py"``) and an unquoted path can itself
    contain `` b/``, which the ``diff --git`` header cannot express
    unambiguously (PR #47 review finding 7). A deletion keys by its
    a-side path — ``+++ /dev/null`` names no file, and
    :func:`diff_file_changes` merges hunks under that same path.
    Context lines advance both counters, or every location after the
    first context line drifts.

    Returns:
        ``{path: (added, removed)}`` where each list holds
        ``(line_number, line_text)`` (new-file / old-file numbering).
    """
    hunk = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    result: dict[str, tuple[list, list]] = {}
    key = None
    pending_old = None
    in_hunk = False
    new_no = old_no = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            key = None
            pending_old = None
            in_hunk = False
            continue
        if not in_hunk and key is None and line.startswith("--- "):
            path = _diff_path(line[4:], "a")
            if path is not None:
                pending_old = path
            continue
        if not in_hunk and key is None and line.startswith("+++ "):
            path = _diff_path(line[4:], "b")
            key = path if path is not None else pending_old
            if key is not None:
                result.setdefault(key, ([], []))
            continue
        m = hunk.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            in_hunk = True
            continue
        if not in_hunk or key is None:
            continue
        if line.startswith(" "):
            old_no += 1
            new_no += 1
            continue
        if line[:1] not in ("+", "-"):
            continue
        added, removed = result[key]
        if line.startswith("+"):
            added.append((new_no, line[1:]))
            new_no += 1
        else:
            removed.append((old_no, line[1:]))
            old_no += 1
    return result


def changes_from_patch(text: str) -> list:
    """``FileChange`` list parsed from a unified-diff patch, no repo.

    The F-33 corpus replay reads ``gh pr diff`` output through here.
    Statuses come from the git headers (``new file mode``, ``deleted
    file mode``, ``rename from``/``rename to``, ``copy from``/``copy
    to`` — anything else is a modification). Paths come from the
    authoritative lines, decoded when git quotes them (PR #47 review
    finding 7): ``+++ b/x`` first — a ``/dev/null`` falls back to the
    ``--- a/x`` of a deletion — then ``rename to``/``copy to`` for the
    no-hunk blocks, and only as a last resort the ``diff --git``
    header, which cannot express a quoted or `` b/``-containing path
    unambiguously. Line detail reuses :func:`_parse_unified_diff`, so
    a replayed patch and :func:`diff_file_changes` can never disagree
    about hunk line numbers.

    Args:
        text: The patch text (``git diff`` / ``gh pr diff`` output).

    Returns:
        ``FileChange`` list, one per ``diff --git`` block, in patch order.
    """
    changes: list[FileChange] = []
    headers: list[str] = []
    pending_old = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            changes.append(FileChange("", "M"))
            headers.append(line)
            pending_old = None
            continue
        if not changes:
            continue
        change = changes[-1]
        if line.startswith("new file mode"):
            change.status = "A"
        elif line.startswith("deleted file mode"):
            change.status = "D"
        elif line.startswith("rename from "):
            change.old_path = _unquote_git_path(line[len("rename from "):].strip())
            change.status = "R"
        elif line.startswith("rename to "):
            change.path = _unquote_git_path(line[len("rename to "):].strip())
        elif line.startswith("copy from "):
            change.old_path = _unquote_git_path(line[len("copy from "):].strip())
            change.status = "C"
        elif line.startswith("copy to "):
            change.path = _unquote_git_path(line[len("copy to "):].strip())
        elif line.startswith("--- "):
            path = _diff_path(line[4:], "a")
            if path is not None:
                pending_old = path
        elif line.startswith("+++ "):
            path = _diff_path(line[4:], "b")
            if path is not None:
                change.path = path
            elif not change.path and pending_old is not None:
                change.path = pending_old  # a deletion's +++ is /dev/null
    hunks = _parse_unified_diff(text)
    named = []
    for change, header in zip(changes, headers):
        if not change.path:
            change.path = _header_paths(header)[1] or change.old_path or ""
        if not change.path:
            continue  # nothing named the file and it has no hunks
        change.added_lines, change.removed_lines = hunks.get(
            change.path, ([], [])
        )
        named.append(change)
    return named


def _header_paths(header: str) -> tuple:
    """Best-effort a/b paths from a ``diff --git`` header line.

    Unreliable BY DESIGN of the format when a path is quoted or contains
    `` b/`` — the ``---``/``+++`` lines are authoritative, and this only
    backs up blocks that carry neither (no hunks, no rename lines).
    """
    tail = header[len("diff --git "):].strip()
    quoted = re.fullmatch(r'("(?:\\.|[^"\\])*") ("(?:\\.|[^"\\])*")', tail)
    if quoted:
        a, b = _unquote_git_path(quoted.group(1)), _unquote_git_path(quoted.group(2))
    elif " b/" in tail:
        a, _, b = tail.rpartition(" b/")
        b = f"b/{b}" if not b.startswith("b/") else b
    else:
        return "", ""  # not a two-path header; nothing to fall back to
    return (
        a[2:] if a.startswith("a/") else a,
        b[2:] if b.startswith("b/") else b,
    )
