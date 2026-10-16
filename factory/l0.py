"""L0 deterministic gates (F-07) — VALIDATOR-SPEC.md, layer L0.

Runs a project's ``project.yaml`` commands on a checkout of the slice's
branch and applies the deterministic diff policy to ``git diff
<base>...<head>``. No model is involved: every rule here is checkable from
the diff and the command output alone.

Baseline file format (repo-relative path named by ``profile["baselines"]``)::

    {
      "known_errors": {"lib/foo.py": [10, 20]},
      "min_tests": 46
    }

``known_errors`` ports SoloAdventurer's verifier rules verbatim
(``.claude/agents/verifier.md``): a reported error is pre-existing ONLY at
an exact ``file:line`` listed there — never for a whole file. A new error
in a file that has known-red lines, at a line NOT in the list, is
diff-attributable and FAILs; treating whole files as exempt reopens the
false-PASS door. ``min_tests`` is the passing-test floor read from the
``test`` command output; a drop FAILs unless a human accepts a stated
removal reason — L0 cannot verify intent, so it errs on the side of FAIL
and lets the human override.

Heuristics kept deliberately conservative (default verdict FAIL; evidence
flips it, per the verifier's standing rule):

- Test files are recognised by conventional globs (``TEST_GLOBS``); the
  exact test files named in the task's approved contract are additionally
  treated as protected paths, locked at approval.
- Skip markers and removed assertions are line heuristics over the diff's
  added/removed lines in test files. A moved assertion trips the rule; a
  human reviews the FAIL.
- Command output is scanned for ``file:line`` errors (gcc/ruff and tsc
  styles). A failing command whose output yields no parseable location is
  a FAIL with the raw output attached — an error we cannot attribute is
  not pre-existing.

Deferred beyond v0 (needs the F-05 sandbox network allowlist): the
``new_dependency`` registry checks (package exists, older than 30 days,
not a near-miss of a popular name). L0 records the escalation; the checks
run when there is a sandbox to run them in.
"""

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Conventional test-path globs, matched with fnmatch semantics (``*``
# crosses ``/``). Projects with unusual layouts should name their test
# files in the contract (which protects them) until per-project overrides
# exist.
TEST_GLOBS = (
    "tests/*",
    "test/*",
    "*__tests__/*",
    "test_*.py",
    "*_test.py",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.js",
)

# Added lines in test files matching any of these -> skip_marker_added.
SKIP_MARKER_PATTERNS = (
    re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)"),
    re.compile(r"\bpytest\.skip\("),
    re.compile(r"\bskipTest\("),
    re.compile(r"@unittest\.skip"),
    re.compile(r"\bthis\.skip\("),
    re.compile(r"\b(?:it|describe|test|context)\.skip\("),
    re.compile(r"\b(?:xit|xdescribe)\("),
    re.compile(r"\b(?:it|describe|test)\.only\("),
)

# Removed lines in test files matching any of these -> assertion_removed.
# Substring-level on purpose: catches assertEqual/expect(...) variants;
# comments mentioning "assert" can false-positive — a human reviews.
ASSERTION_PATTERNS = (
    re.compile(r"\bassert"),
    re.compile(r"\bexpect\("),
)

# Reported-error locations in command output. A failing command with no
# parseable location is an unattributable error -> FAIL.
ERROR_LINE_PATTERNS = (
    # gcc / ruff / pytest-traceback style: path:line[:col]: message
    re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+)(?::\d+)?:\s"),
    # TypeScript style: path(line,col): error TSxxxx: message
    re.compile(r"^(?P<file>[^\s(]+)\((?P<line>\d+),\d+\):\s*error"),
)

_TEST_COUNT = re.compile(r"(\d+)\s+passed")

# Gate order after setup; extras are appended sorted for determinism.
_GATE_ORDER = ("lint", "typecheck", "test", "build")


class GateError(Exception):
    """L0 cannot run as configured (git failure, bad refs, bad state)."""


@dataclass
class Finding:
    """A deterministic L0 finding. FAIL-class findings decide the verdict."""

    id: str
    text: str
    file: str | None = None
    line: int | None = None


@dataclass
class CommandResult:
    """One executed profile command."""

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
    from the unified diff (new-file / old-file numbering respectively).
    For renames, ``path`` is the new path, ``old_path`` the previous one.
    """

    path: str
    status: str
    old_path: str | None = None
    added_lines: list = field(default_factory=list)
    removed_lines: list = field(default_factory=list)


@dataclass
class L0Report:
    """The L0 verdict plus everything F-09/F-10 consume downstream.

    ``escalations`` name the immediate-stop classes in VALIDATOR-SPEC
    (protected path, risk path, new dependency): they need a human even
    when the verdict itself is a pass.
    """

    verdict: str
    findings: list = field(default_factory=list)
    escalations: list = field(default_factory=list)
    risk_flags_added: list = field(default_factory=list)
    command_results: list = field(default_factory=list)
    preexisting_notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": "L0",
            "verdict": self.verdict,
            "findings": [f.__dict__ | {} for f in self.findings],
            "escalations": self.escalations,
            "risk_flags_added": self.risk_flags_added,
            "commands": [c.to_dict() for c in self.command_results],
            "preexisting_notes": self.preexisting_notes,
        }


def _matches(path: str, patterns) -> bool:
    """True when the posix relpath matches any glob (fnmatch, case-sensitive)."""
    return any(fnmatch.fnmatchcase(path, pat) for pat in patterns)


def _is_test_file(path: str) -> bool:
    return _matches(path, TEST_GLOBS)


def contract_test_files(task: dict) -> set:
    """Collect the test files named in a task's approved acceptance checks.

    Every ``check.type: test`` ref of the form ``path::name`` contributes
    ``path``. These files are protected paths for the task's diff, locked
    at approval (VALIDATOR-SPEC L0): a worker cannot weaken the tests that
    define "done".

    Args:
        task: A task record (may be empty).

    Returns:
        The set of repo-relative test file paths.
    """
    files: set[str] = set()
    for item in task.get("acceptance") or []:
        check = item.get("check") if isinstance(item, dict) else None
        if isinstance(check, dict) and check.get("type") == "test":
            ref = check.get("ref")
            if isinstance(ref, str) and "::" in ref:
                files.add(ref.split("::", 1)[0].strip())
    return files


def _git(repo: Path, *args: str) -> str:
    """Run git in the repo, raising GateError on any failure."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise GateError(
            f"git {' '.join(args[:2])} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout


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
    status_out = _git(repo, "diff", "--name-status", "-M", f"{base_ref}...{head_ref}")
    diff_out = _git(repo, "diff", "-M", f"{base_ref}...{head_ref}")
    hunks = _parse_unified_diff(diff_out)

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
        added, removed = hunks.get(path, ([], []))
        changes.append(FileChange(path, status, old_path, added, removed))
    return changes


def _parse_unified_diff(text: str) -> dict:
    """Key a unified diff by its b-side path, tracking line numbers.

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
        if not in_hunk or key is None or line[:1] not in "+-":
            continue
        added, removed = result[key]
        if line.startswith("+"):
            added.append((new_no, line[1:]))
            new_no += 1
        else:
            removed.append((old_no, line[1:]))
            old_no += 1
    return result


def parse_errors(output: str) -> list:
    """Extract reported ``file:line`` error locations from command output.

    Args:
        output: Combined stdout+stderr of a failed command.

    Returns:
        Sorted, de-duplicated ``[(file, line), ...]``.
    """
    found = set()
    for line in output.splitlines():
        for pattern in ERROR_LINE_PATTERNS:
            m = pattern.match(line)
            if m:
                found.add((m.group("file"), int(m.group("line"))))
                break
    return sorted(found)


def parse_test_count(output: str) -> int | None:
    """Read the passing-test count from test output (pytest/jest summaries).

    Takes the highest ``N passed`` in the output: suites print running
    totals above the summary, and only the floor comparison matters.
    """
    counts = [int(n) for n in _TEST_COUNT.findall(output)]
    return max(counts) if counts else None


def check_diff_policy(profile: dict, changes: list, task: dict | None = None):
    """Apply the L0 diff policy to a set of changed paths.

    FAIL classes (findings): protected path touched (modified or deleted),
    test file deleted, skip marker added, assertion removed. Escalation
    classes (no FAIL by themselves): undeclared risk path touched
    (flag added; forces a human merge), dependency manifest changed.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        changes: ``FileChange`` list from ``diff_file_changes``.
        task: The task record (its ``risk`` and acceptance test refs).

    Returns:
        ``(findings, escalations, risk_flags_added)``.
    """
    task = task or {}
    paths = profile.get("paths") or {}
    protected = list(paths.get("protected") or []) + sorted(contract_test_files(task))
    risk_globs = paths.get("risk") or {}
    manifests = profile.get("dependency_manifests") or []

    findings: list[Finding] = []
    escalations: set[str] = set()
    matched_flags: set[str] = set()

    def events(change: FileChange):
        """(path, verb) pairs; a rename deletes the old path, modifies the new."""
        if change.status == "R":
            return [(change.old_path, "deleted"), (change.path, "modified")]
        verb = {"A": "added", "M": "modified", "D": "deleted", "T": "modified",
                "C": "added"}.get(change.status)
        return [(change.path, verb)] if verb else []

    for change in changes:
        for path, verb in events(change):
            if verb in ("modified", "deleted") and _matches(path, protected):
                findings.append(
                    Finding(
                        "protected_path_touched",
                        f"protected path {verb}: {path}",
                        file=path,
                    )
                )
                escalations.add("protected_path_touched")
            for flag, globs in risk_globs.items():
                if _matches(path, globs):
                    matched_flags.add(flag)
            if _matches(path, manifests):
                escalations.add("new_dependency")

        if change.status == "D" and _is_test_file(change.path):
            findings.append(
                Finding("test_deleted", f"test file deleted: {change.path}", file=change.path)
            )

        scan_path = change.old_path if _is_test_file(change.old_path or "") else change.path
        if not _is_test_file(scan_path):
            continue
        for no, text in change.added_lines:
            if any(p.search(text) for p in SKIP_MARKER_PATTERNS):
                findings.append(
                    Finding(
                        "skip_marker_added",
                        f"skip/only marker added in {scan_path}:{no}: {text.strip()}",
                        file=scan_path,
                        line=no,
                    )
                )
        for no, text in change.removed_lines:
            if any(p.search(text) for p in ASSERTION_PATTERNS):
                findings.append(
                    Finding(
                        "assertion_removed",
                        f"assertion removed in {scan_path}:{no}: {text.strip()}",
                        file=scan_path,
                        line=no,
                    )
                )

    risk_flags_added = sorted(matched_flags - set(task.get("risk") or []))
    if risk_flags_added:
        escalations.add("risk_path_touched")

    findings.sort(key=lambda f: (f.id, f.file or "", f.line or 0))
    return findings, sorted(escalations), risk_flags_added


def _load_baseline(profile: dict, repo: Path):
    """Load the baseline file named by the profile, if any.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        repo: Repository the baseline path is relative to.

    Returns:
        ``(baseline, missing_finding)`` — the parsed mapping (or ``{}``)
        and a FAIL finding when a declared baseline file can't be read.
    """
    declared = profile.get("baselines")
    if not declared:
        return {}, None
    path = repo / declared
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {}, Finding(
            "baseline_missing",
            f"baseline file {declared} unreadable ({exc}); every error is new",
            file=declared,
        )
    if not isinstance(data, dict):
        return {}, Finding(
            "baseline_missing", f"baseline file {declared} is not a JSON object", file=declared
        )
    return data, None


def _run_command(name: str, command: str, repo: Path, timeout_sec: int):
    """Run one profile command in the repo with a wall-clock cap."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return CommandResult(name, command, proc.returncode, proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        output = out.decode() if isinstance(out, bytes) else (out or "")
        return CommandResult(name, command, None, output, timed_out=True)


def run_l0(
    profile: dict,
    repo: Path | str,
    base_ref: str,
    head_ref: str,
    task: dict | None = None,
    baseline: dict | None = None,
    timeout_sec: int = 600,
) -> L0Report:
    """Run the full L0 gate on a checkout of the slice's branch.

    The caller (F-10) passes a clean clone; any repo holding both refs
    works, so tests run fixtures directly. Commands are shell strings from
    ``project.yaml`` and run with the orchestrator's environment — the
    F-05 sandbox is what confines them in production.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        repo: Repository holding ``base_ref`` and ``head_ref``.
        base_ref: Base ref (compared from the merge-base).
        head_ref: The slice's branch ref.
        task: The task record (risk flags, contract test refs).
        baseline: Baseline mapping; when ``None``, loaded from the
            profile's ``baselines`` path inside ``repo``.
        timeout_sec: Per-command wall-clock cap.

    Returns:
        The ``L0Report``; verdict is ``"fail"`` iff any finding exists.

    Raises:
        GateError: If git can't produce the diff (bad refs, git missing).
    """
    repo = Path(repo)
    changes = diff_file_changes(repo, base_ref, head_ref)
    findings, escalations, risk_flags_added = check_diff_policy(profile, changes, task)

    if baseline is None:
        baseline, missing = _load_baseline(profile, repo)
        if missing:
            findings.append(missing)
    known_errors: dict[str, set[int]] = {}
    raw_known = baseline.get("known_errors") or {}
    if not isinstance(raw_known, dict) or not all(
        isinstance(lines, list) for lines in raw_known.values()
    ):
        findings.append(
            Finding(
                "baseline_invalid",
                "baseline known_errors must map file -> [line, ...]; "
                "no error is exempted",
            )
        )
    else:
        known_errors = {
            file: {int(line) for line in lines} for file, lines in raw_known.items()
        }
    min_tests = baseline.get("min_tests")
    if min_tests is not None and not isinstance(min_tests, (int, float)):
        findings.append(
            Finding(
                "baseline_invalid",
                f"baseline min_tests must be a number, got {min_tests!r}; "
                "test-count floor not applied",
            )
        )
        min_tests = None

    commands = profile.get("commands") or {}
    ordered = [("setup", commands.get("setup"))]
    ordered += [(g, commands.get(g)) for g in _GATE_ORDER]
    ordered += sorted((f"extra:{k}", v) for k, v in (commands.get("extra") or {}).items())

    results: list[CommandResult] = []
    preexisting: list[str] = []
    for name, command in ordered:
        if not command:
            continue
        result = _run_command(name, command, repo, timeout_sec)
        results.append(result)
        if name == "setup" and (result.timed_out or result.exit_code != 0):
            findings.append(
                Finding(
                    "setup_failed",
                    f"setup failed (exit {result.exit_code}); remaining gates skipped",
                )
            )
            break
        if result.exit_code == 0 and not result.timed_out:
            continue
        if result.timed_out:
            findings.append(
                Finding(
                    "command_timeout",
                    f"{name} exceeded {timeout_sec}s: {result.command}",
                )
            )
            continue
        errors = parse_errors(result.output)
        if not errors:
            findings.append(
                Finding(
                    "command_failed",
                    f"{name} exited {result.exit_code} with no parsable error location: "
                    f"{result.output.strip()[:500]}",
                )
            )
            continue
        for file, line in errors:
            if line in known_errors.get(file, set()):
                preexisting.append(f"{file}:{line} pre-existing (baselined), not a failure")
            else:
                findings.append(
                    Finding(
                        "new_error",
                        f"{name}: new error at {file}:{line} (not in baseline)",
                        file=file,
                        line=line,
                    )
                )

    if min_tests is not None:
        test_result = next((r for r in results if r.name == "test"), None)
        count = parse_test_count(test_result.output) if test_result else None
        if count is not None and count < min_tests:
            findings.append(
                Finding(
                    "test_count_dropped",
                    f"passing tests dropped: {count} < baseline {min_tests}",
                )
            )

    findings.sort(key=lambda f: (f.id, f.file or "", f.line or 0))
    return L0Report(
        verdict="fail" if findings else "pass",
        findings=findings,
        escalations=escalations,
        risk_flags_added=risk_flags_added,
        command_results=results,
        preexisting_notes=sorted(set(preexisting)),
    )
