"""L0 deterministic gates (F-07) — VALIDATOR-SPEC.md, layer L0.

Runs a project's ``project.yaml`` commands on a checkout of the slice's
branch and applies the deterministic diff policy to ``git diff
<base>...<head>``. No model is involved: every rule here is checkable from
the diff and the command output alone. The shared gate primitives
(findings, command results, git access, diff parsing, test-file and
contract-ref recognition) live in ``factory.gates``.

Baseline file format (repo-relative path named by ``profile["baselines"]``,
read as committed at ``base_ref`` — never from the slice's own branch, so a
worker cannot exempt its own errors by editing it)::

    {
      "known_errors": {"lib/foo.py": [10, 20]},
      "min_tests": 46
    }

SoloAdventurer's ``.claude/state/baselines.json`` schema is read too:
``known_typecheck_errors.files`` (``{file: [line, ...]}``, the same exact
``file:line`` list) and ``jest.tests`` (the passing-test floor). Both
spellings may appear; error lists merge and the higher floor wins. Other
top-level keys (``ci_status``, notes) are ignored. A baseline with none of
the recognised sections, or with a recognised section of the wrong shape
(non-int line, bool/float floor), is ``baseline_invalid`` and exempts
nothing. A floor that is declared but cannot be read from the ``test``
output is ``test_count_unknown`` — the floor is never skipped silently.

Error paths are compared repo-relative. Commands of the form
``cd <dir> && ...`` (the pilot's ``cd backend && ...``) report paths
relative to ``<dir>``, so parsed paths are joined onto that leading ``cd``
target and normalised (which also strips ``./``); absolute paths inside
the repo are made relative. Only a single unquoted leading ``cd`` is
recognised — anything else is left as reported, so it fails to match the
baseline and FAILs rather than being wrongly exempted.

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

- Test files are recognised by the conventional globs in
  ``gates.TEST_GLOBS`` (nested test dirs and ``conftest.py`` at any depth
  included — conftest is test infrastructure); the exact test files named
  in the task's approved contract are additionally treated as protected
  paths, locked at approval.
- Skip markers and removed assertions are line heuristics over the diff's
  added/removed lines in test files. A moved assertion trips the rule; a
  human reviews the FAIL.
- Command output is scanned for ``file:line`` errors (gcc/ruff and tsc
  styles). A failing command whose output yields no parseable location —
  or that reports a failure no baseline entry can cover (a failed test, a
  traceback, a tool crash) — is a FAIL with the raw output attached: an
  error we cannot attribute is not pre-existing.

Deferred beyond v0 (needs the F-05 sandbox network allowlist): the
``new_dependency`` registry checks (package exists, older than 30 days,
not a near-miss of a popular name). L0 records the escalation; the checks
run when there is a sandbox to run them in.
"""

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from factory.gates import (
    CommandResult,
    Finding,
    GateError,
    GateReport,
    contract_test_files,
    diff_file_changes,
)
from factory.gates import git as _git
from factory.gates import is_test_file as _is_test_file
from factory.gates import matches as _matches
from factory.gates import run_command as _run_command

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

# Lines that report a failure without a ``file:line`` we could baseline.
# Baseline exemptions cover lint/typecheck diagnostics only (verifier.md's
# precision rule); a failing test or a tool crash is never pre-existing, so
# any of these in a failed command's output FAILs even when every parsed
# location is baselined.
FAILURE_MARKERS = (
    re.compile(r"^(?:FAILED|ERROR)\s+\S"),  # pytest short summary
    re.compile(r"\b[1-9]\d* failed\b"),  # pytest / jest totals
    re.compile(r"^not ok\b"),  # TAP, node --test
    re.compile(r"^#\s*fail\s+[1-9]"),  # node --test totals
    re.compile(r"^Traceback \(most recent call last\):"),  # Python crash
    re.compile(r"^(?:error|fatal)\b"),  # tool-level error, no location
)

_TEST_COUNT = re.compile(r"(\d+)\s+passed")

# A leading ``cd <dir> &&`` sets the directory error paths are relative to.
_CD_PREFIX = re.compile(r"^\s*cd\s+(?P<dir>[^\s;&|'\"]+)\s*&&")

# Gate order after setup; extras are appended sorted for determinism.
_GATE_ORDER = ("lint", "typecheck", "test", "build")


@dataclass
class L0Report(GateReport):
    """The L0 verdict plus everything F-09/F-10 consume downstream.

    ``escalations`` name the immediate-stop classes in VALIDATOR-SPEC
    (protected path, risk path, new dependency): they need a human even
    when the verdict itself is a pass.
    """

    layer: ClassVar[str] = "L0"
    escalations: list = field(default_factory=list)
    risk_flags_added: list = field(default_factory=list)
    preexisting_notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "verdict": self.verdict,
            "findings": self.findings_as_dicts(),
            "escalations": self.escalations,
            "risk_flags_added": self.risk_flags_added,
            "commands": self.commands_as_dicts(),
            "preexisting_notes": self.preexisting_notes,
        }


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


def _failure_marker(output: str) -> str | None:
    """Return the first output line reporting an unbaselinable failure."""
    for line in output.splitlines():
        if any(p.search(line) for p in FAILURE_MARKERS):
            return line.strip()
    return None


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

    def events(change):
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
        # Renaming a test out of the test paths stops it being collected:
        # a deletion, even when the content is unchanged (R100, no hunks).
        if (
            change.status == "R"
            and _is_test_file(change.old_path or "")
            and not _is_test_file(change.path)
        ):
            findings.append(
                Finding(
                    "test_deleted",
                    f"test file renamed out of test paths: {change.old_path} -> {change.path}",
                    file=change.old_path,
                )
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


def _load_baseline(profile: dict, repo: Path, base_ref: str):
    """Load the baseline file named by the profile, as committed at base.

    Reading it from ``base_ref`` rather than the checkout means the slice's
    own edits to the baseline (widened ``known_errors``, a lowered
    ``min_tests``) cannot exempt the slice. Bumps land via a human merge.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        repo: Repository holding ``base_ref``.
        base_ref: The trusted ref the baseline is read from.

    Returns:
        ``(baseline, missing_finding)`` — the parsed mapping (``None`` when
        no baseline is declared, ``{}`` when unreadable) and a FAIL finding
        when a declared baseline file can't be read.
    """
    declared = profile.get("baselines")
    if not declared:
        return None, None
    try:
        data = json.loads(_git(repo, "show", f"{base_ref}:{declared}"))
    except (GateError, json.JSONDecodeError) as exc:
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


def _is_int(value) -> bool:
    """True for real ints; ``bool`` is an ``int`` subclass and is rejected."""
    return isinstance(value, int) and not isinstance(value, bool)


def _error_map(raw) -> dict | None:
    """Validate a ``{file: [line, ...]}`` map; ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    result: dict[str, set[int]] = {}
    for file, lines in raw.items():
        if not isinstance(lines, list) or not all(_is_int(n) for n in lines):
            return None
        result.setdefault(posixpath.normpath(file), set()).update(lines)
    return result


def normalise_baseline(baseline: dict):
    """Reduce a baseline mapping to the exemptions and floor L0 applies.

    Accepts the factory keys (``known_errors``, ``min_tests``) and
    SoloAdventurer's (``known_typecheck_errors.files``, ``jest.tests``).

    Args:
        baseline: The parsed baseline mapping.

    Returns:
        ``(known_errors, min_tests, findings, notes)``. Any malformed
        section yields a ``baseline_invalid`` finding and exempts nothing
        (``known_errors`` is emptied); the floor is kept if it parsed.
    """
    findings: list[Finding] = []
    notes: list[str] = []

    def invalid(text: str) -> None:
        findings.append(Finding("baseline_invalid", f"{text}; no error is exempted"))

    if not isinstance(baseline, dict):
        invalid("baseline must be a JSON object")
        return {}, None, findings, notes

    error_maps = []
    if "known_errors" in baseline:
        error_maps.append(("known_errors", baseline["known_errors"]))
    if "known_typecheck_errors" in baseline:
        section = baseline["known_typecheck_errors"]
        files = section.get("files") if isinstance(section, dict) else None
        error_maps.append(("known_typecheck_errors.files", files))

    floors = []
    if "min_tests" in baseline:
        floors.append(("min_tests", baseline["min_tests"]))
    if "jest" in baseline:
        section = baseline["jest"]
        floors.append(("jest.tests", section.get("tests") if isinstance(section, dict) else None))

    if not error_maps and not floors:
        invalid(
            "baseline has none of known_errors, known_typecheck_errors, "
            f"min_tests, jest (keys: {sorted(baseline)})"
        )
        return {}, None, findings, notes

    known_errors: dict[str, set[int]] = {}
    for key, raw in error_maps:
        parsed = _error_map(raw)
        if parsed is None:
            invalid(f"baseline {key} must map file -> [int line, ...], got {raw!r}")
            continue
        for file, lines in parsed.items():
            known_errors.setdefault(file, set()).update(lines)
    if findings:
        known_errors = {}

    min_tests = None
    for key, value in floors:
        if not _is_int(value) or value < 0:
            invalid(f"baseline {key} must be a non-negative int, got {value!r}")
            continue
        min_tests = value if min_tests is None else max(min_tests, value)
    if not floors:
        notes.append("baseline declares no test-count floor; floor not applied")
    return known_errors, min_tests, findings, notes


def command_cwd(command: str) -> str:
    """The directory a ``cd <dir> && ...`` command runs in, else ``""``."""
    m = _CD_PREFIX.match(command)
    return m.group("dir") if m else ""


def repo_relative(file: str, cwd: str, repo: Path) -> str:
    """Map a reported error path onto the repo-relative baseline key space.

    Args:
        file: The path as the tool reported it.
        cwd: The command's working directory (``command_cwd``), repo-relative
            or absolute.
        repo: The repository root.

    Returns:
        A normalised repo-relative posix path, or the normalised input when
        it lies outside the repo (it then matches no baseline entry).
    """
    joined = posixpath.normpath(posixpath.join(cwd, file))
    if posixpath.isabs(joined):
        try:
            return Path(joined).resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return joined
    return joined


def _assert_checkout(repo: Path, head_ref: str) -> None:
    """Refuse to gate a working tree that isn't exactly ``head_ref``.

    The diff is computed from refs but the commands run on the checkout;
    if they disagree, the verdict would describe code nobody reviewed.

    Raises:
        GateError: HEAD isn't ``head_ref``'s commit, or the tree is dirty.
    """
    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    want = _git(repo, "rev-parse", "--verify", f"{head_ref}^{{commit}}").strip()
    if head != want:
        raise GateError(
            f"checkout HEAD {head[:12]} is not head_ref {head_ref} ({want[:12]})"
        )
    dirty = _git(repo, "status", "--porcelain").strip()
    if dirty:
        raise GateError(
            f"working tree is not clean at head_ref {head_ref}:\n{dirty[:500]}"
        )


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

    The caller (F-10) passes a clean clone checked out at ``head_ref``:
    the diff comes from refs but the commands run on the working tree, so
    a checkout at any other commit, or with uncommitted or untracked
    files, raises ``GateError`` instead of gating code nobody reviewed.
    Commands are shell strings from
    ``project.yaml`` and run with the orchestrator's environment — the
    F-05 sandbox is what confines them in production.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        repo: Repository holding ``base_ref`` and ``head_ref``.
        base_ref: Base ref (compared from the merge-base).
        head_ref: The slice's branch ref.
        task: The task record (risk flags, contract test refs).
        baseline: Baseline mapping; when ``None``, loaded from the
            profile's ``baselines`` path as committed at ``base_ref``.
        timeout_sec: Per-command wall-clock cap.

    Returns:
        The ``L0Report``; verdict is ``"fail"`` iff any finding exists.

    Raises:
        GateError: If git can't produce the diff (bad refs, git missing),
            or the checkout isn't a clean tree at ``head_ref``.
    """
    repo = Path(repo)
    _assert_checkout(repo, head_ref)
    changes = diff_file_changes(repo, base_ref, head_ref)
    findings, escalations, risk_flags_added = check_diff_policy(profile, changes, task)

    preexisting: list[str] = []
    missing = None
    if baseline is None:
        baseline, missing = _load_baseline(profile, repo, base_ref)
    known_errors: dict[str, set[int]] = {}
    min_tests = None
    if missing:
        findings.append(missing)
    elif baseline is not None:
        known_errors, min_tests, invalid, notes = normalise_baseline(baseline)
        findings.extend(invalid)
        preexisting.extend(notes)

    commands = profile.get("commands") or {}
    ordered = [("setup", commands.get("setup"))]
    ordered += [(g, commands.get(g)) for g in _GATE_ORDER]
    ordered += sorted((f"extra:{k}", v) for k, v in (commands.get("extra") or {}).items())

    results: list[CommandResult] = []
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
        cwd = command_cwd(command)
        errors = sorted({(repo_relative(f, cwd, repo), n) for f, n in errors})
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
        marker = _failure_marker(result.output)
        if marker:
            findings.append(
                Finding(
                    "command_failed",
                    f"{name} exited {result.exit_code} reporting a failure no "
                    f"baseline entry covers: {marker}",
                )
            )

    if min_tests is not None:
        test_result = next((r for r in results if r.name == "test"), None)
        count = parse_test_count(test_result.output) if test_result else None
        if count is None:
            findings.append(
                Finding(
                    "test_count_unknown",
                    f"baseline floor is {min_tests} passing tests but no "
                    "'N passed' count was read from the test command",
                )
            )
        elif count < min_tests:
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
