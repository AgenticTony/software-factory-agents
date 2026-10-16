"""Shared primitives for the validator gates (VALIDATOR-SPEC.md).

Every deterministic layer (L0, L1) speaks the same language — findings,
command results, git access, diff parsing, test-file recognition and the
contract's named-test refs — so that language lives here once instead of
drifting between layer modules. The layer modules (``factory.l0``,
``factory.l1``) own their policies; this module owns the machinery.
"""

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

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


def git(repo: Path, *args: str) -> str:
    """Run git in the repo, raising GateError on any failure."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise GateError(
            f"git {' '.join(args[:2])} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout


def _text(data) -> str:
    """Normalise captured output: POSIX ``TimeoutExpired`` carries raw bytes
    (or ``None``) even when the run asked for text."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def run_command(name: str, command: str, repo: Path, timeout_sec: int):
    """Run one shell command in the repo with a wall-clock cap."""
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


def _name_status_rows(status_out: str, head_ref: str) -> list:
    """Parse ``git diff --name-status -M`` rows into bare FileChanges."""
    changes = []
    for row in status_out.splitlines():
        if not row.strip():
            continue
        parts = row.split("\t")
        status = re.match(r"^[A-Z]+", parts[0]).group()
        if status == "U":
            raise GateError(f"unmerged path in diff of {head_ref}: {row}")
        path = parts[-1]
        old_path = parts[1] if status == "R" and len(parts) == 3 else None
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


def _parse_unified_diff(text: str) -> dict:
    """Key a unified diff by its b-side path, tracking line numbers.

    Context lines advance both counters, or every location after the
    first context line drifts.

    Returns:
        ``{path: (added, removed)}`` where each list holds
        ``(line_number, line_text)`` (new-file / old-file numbering).
    """
    header = re.compile(r"^diff --git a/(.*) b/(.*)$")
    hunk = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    result: dict[str, tuple[list, list]] = {}
    key = None
    in_hunk = False
    new_no = old_no = 0
    for line in text.splitlines():
        m = header.match(line)
        if m:
            key = m.group(2)
            in_hunk = False
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
