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
  paths, locked at approval. Adding a file under ``paths.protected`` is a
  touch (a new ``conftest.py`` can disable the contract tests); only the
  task's own contract test files may be added, since they are written
  red-first by the implementer.
- Skip markers and removed assertions are line heuristics over the diff's
  added/removed lines in test files. A moved assertion trips the rule; a
  human reviews the FAIL. All four built-in sentinels (F-25) scan in
  ``factory.sentinels`` — these two, plus the credential and
  lint-suppression scanners — which also runs the profile's
  ``sentinels.<risk flag>`` commands after setup and records one result
  per sentinel on the report.
- Command output is scanned for ``file:line`` errors (gcc/ruff and tsc
  styles). A failing command whose output yields no parseable location,
  or only locations outside the repo (framework/site-packages frames,
  which never become ``new_error``) —
  or that reports a failure no baseline entry can cover (a failed test, a
  traceback, a tool crash) — is a FAIL with the raw output attached: an
  error we cannot attribute is not pre-existing.

Deferred beyond v0 (needs the F-05 sandbox network allowlist): the
``new_dependency`` registry checks (package exists, older than 30 days,
not a near-miss of a popular name). L0 records the escalation; the checks
run when there is a sandbox to run them in.
"""

import copy
import json
import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from factory.attribution import attribution_match
from factory.gates import (
    CommandResult,
    CommandRunner,
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
from factory.sentinels import (
    builtin_sentinel_pass,
    matched_risk_flags,
    run_project_sentinels,
    skipped_project_results,
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
    sentinel_results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "verdict": self.verdict,
            "findings": self.findings_as_dicts(),
            "escalations": self.escalations,
            "risk_flags_added": self.risk_flags_added,
            "commands": self.commands_as_dicts(),
            "preexisting_notes": self.preexisting_notes,
            "sentinel_results": self.sentinel_results,
        }


def parse_errors(output: str) -> list:
    """Extract reported ``file:line`` error locations with their messages.

    Args:
        output: Combined stdout+stderr of a failed command.

    Returns:
        Sorted, de-duplicated ``[(file, line, message), ...]``. The
        message is the rest of the matched line, stripped and capped
        ("" when the tool printed a bare location). Findings carry it so
        a fix prompt says what is wrong at the line, not only where
        (INC-005: the agent knew ``tests/test_f11.py:59`` and spent two
        rounds failing to see a syntax error the message would have
        named).
    """
    found: dict[tuple[str, int], str] = {}
    for line in output.splitlines():
        for pattern in ERROR_LINE_PATTERNS:
            m = pattern.match(line)
            if m:
                key = (m.group("file"), int(m.group("line")))
                found.setdefault(key, line[m.end():].strip()[:240])
                break
    return [(file, line, found[(file, line)]) for file, line in sorted(found)]


def repo_owned(path: str) -> bool:
    """Whether an error location is this repo's code, so its baseline
    can speak about it.

    Traceback frames in the interpreter, the standard library or
    installed packages are locations in someone else's code: they can
    never appear in this repo's baseline, so as new_error findings they
    are pure noise — noise that sorts before the repo's own line and
    buried it (INC-005). The sandbox's HOME scratch (``<run>/.home``,
    where a user-site install lands), absolute paths outside the repo,
    parent escapes and frozen modules are the same class.

    Args:
        path: A repo-relative (or not) posix path, already through
            :func:`repo_relative`.

    Returns:
        True when the path names a repo location.
    """
    if posixpath.isabs(path):
        return False
    parts = posixpath.normpath(path).split("/")
    return (
        parts[0] not in ("", "..", ".home")
        and "site-packages" not in parts
        and not parts[0].startswith("<frozen")
    )


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


# Dotted paths inside a parsed pyproject.toml that are INERT for the
# dependency surface: they may differ between merge-base and head without
# escalating ``new_dependency``. The comparison is inverted on purpose
# (review round on #74: enumerating the dependency-carrying tables was
# fail-open — [dependency-groups], dynamic dependencies, [tool.uv.sources]
# and [tool.poetry.*] all slipped past a three-table allowlist), so every
# entry here must earn its place:
#
# * ``project.name/description/readme/version/urls/scripts`` — artifact
#   metadata and console entry points: describe the thing built, never
#   what is imported to build or run it. (``scripts`` executes the
#   project's own code, which the diff already reviews; it cannot pull
#   a new package in.)
# * ``tool.ruff`` / ``tool.pytest`` / ``tool.coverage`` / ``tool.mypy`` —
#   dev-tool configuration for tools the factory itself pins and runs;
#   none of them can name an import that enters the built environment.
# * ``tool.setuptools.package-data`` — which existing files ship in the
#   wheel; the F-31 case (a py.typed line), and a file that exists in
#   the diff either way.
#
# Anything else — an unknown ``tool.*`` table included — compares, and a
# difference escalates: a new table class that can carry dependencies
# fails closed until a human allowlists it here.
_INERT_PYPROJECT_PATHS = (
    "project.name",
    "project.description",
    "project.readme",
    "project.version",
    "project.urls",
    "project.scripts",
    "tool.ruff",
    "tool.pytest",
    "tool.coverage",
    "tool.mypy",
    "tool.setuptools.package-data",
)


def _strip_inert(doc: dict) -> dict:
    """A deep copy of a parsed pyproject with every :data:`_INERT_PYPROJECT_PATHS`
    entry removed, for whole-document comparison. Tables the stripping
    empties are pruned (``[tool.ruff]`` alone leaves ``tool: {}`` on one
    side and no ``tool`` key on the other — equal in meaning, unequal as
    dicts); an empty table carries no dependency, so pruning it is not a
    hole."""
    stripped = copy.deepcopy(doc)
    for dotted in _INERT_PYPROJECT_PATHS:
        parts = dotted.split(".")
        node: Any = stripped
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    return _prune_empty(stripped)


def _prune_empty(node: Any) -> Any:
    """Recursively drop dict entries whose value is (or prunes to) an
    empty dict; scalars, lists and empty lists pass through untouched."""
    if not isinstance(node, dict):
        return node
    pruned = {}
    for key, value in node.items():
        value = _prune_empty(value)
        if isinstance(value, dict) and not value:
            continue
        pruned[key] = value
    return pruned


def _pyproject_surface_unchanged(change, path: str, *, repo, base_ref, head_ref) -> bool:
    """Whether a manifest touch leaves pyproject.toml's dependency surface
    untouched — the one structurally parsed manifest (F-31 run blocker:
    a ``[tool.setuptools.package-data]``-only edit falsely escalated
    ``new_dependency`` and skipped every remaining gate).

    The WHOLE parsed document compares, minus the inert allowlist
    (:data:`_INERT_PYPROJECT_PATHS`) — dependency-bearing tables outside
    the obvious three (PEP 735 dependency-groups, dynamic dependencies,
    tool.uv.sources, tool.poetry.*) escalate rather than slipping through.

    True ONLY for a plain modification (status M) of a root
    ``pyproject.toml`` with refs supplied, whose merge-base and head
    sides both parse and compare equal. Every doubt — another manifest
    path, an added/deleted/renamed pyproject, missing refs, a git or
    TOML failure — answers False, and the caller escalates exactly as
    the path-match rule always did.
    """
    if path != "pyproject.toml" or change.status != "M":
        return False
    if repo is None or not base_ref or not head_ref:
        return False
    try:
        merge_base = _git(Path(repo), "merge-base", base_ref, head_ref).strip()
        before = tomllib.loads(_git(Path(repo), "show", f"{merge_base}:pyproject.toml"))
        after = tomllib.loads(_git(Path(repo), "show", f"{head_ref}:pyproject.toml"))
        return _strip_inert(before) == _strip_inert(after)
    except (GateError, tomllib.TOMLDecodeError, TypeError, UnicodeDecodeError):
        return False


def check_diff_policy(profile: dict, changes: list, task: dict | None = None,
                      matched_flags: set | None = None, *,
                      repo: Path | str | None = None,
                      base_ref: str | None = None,
                      head_ref: str | None = None):
    """Apply the L0 diff policy to a set of changed paths.

    FAIL classes (findings): protected path touched (added, modified or
    deleted — adding one of the task's own contract test files is allowed),
    test file deleted. Escalation classes (no FAIL by themselves):
    undeclared risk path touched (flag added; forces a human merge),
    dependency manifest changed. The skip-marker, assertion, credential
    and lint-suppression sentinels (F-25) scan in
    :func:`factory.sentinels.builtin_sentinel_findings`, not here — one
    home for the standing set.

    A modified ``pyproject.toml`` is diff-aware when refs are supplied:
    the escalation fires only if the dependency surface —
    ``[project].dependencies``, ``[project].optional-dependencies``,
    ``[build-system].requires`` — actually changed between the
    merge-base and head (F-31 run blocker: a bare
    ``[tool.setuptools.package-data]`` table falsely escalated and
    skipped every remaining gate). Everything else fails closed: any
    other manifest, an added/deleted/renamed pyproject, a parse error
    on either side, or a direct call without refs, escalates exactly as
    the path-match rule always did.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        changes: ``FileChange`` list from ``diff_file_changes``.
        task: The task record (its ``risk`` and acceptance test refs).
        matched_flags: The diff's matched risk flags, when the caller
            already computed them (:func:`factory.sentinels.matched_risk_flags`
            — computed once per L0 run and passed through); ``None``
            computes them here for direct callers.

    Returns:
        ``(findings, escalations, risk_flags_added, matched_flags)``.
    """
    task = task or {}
    paths = profile.get("paths") or {}
    contract_files = contract_test_files(task)
    protected = list(paths.get("protected") or []) + sorted(contract_files)
    manifests = profile.get("dependency_manifests") or []

    findings: list[Finding] = []
    escalations: set[str] = set()

    def events(change):
        """(path, verb) pairs; a rename deletes the old path, modifies the new."""
        if change.status == "R":
            return [(change.old_path, "deleted"), (change.path, "modified")]
        verb = {"A": "added", "M": "modified", "D": "deleted", "T": "modified",
                "C": "added"}.get(change.status)
        return [(change.path, verb)] if verb else []

    for change in changes:
        for path, verb in events(change):
            # The gate runner's venv (factory/orchestrator._create_gate_venv)
            # is factory infrastructure inside the clone: a tracked path
            # under .venv/ means an implementer `git add -f`-ed planted
            # environment (a .pth is imported code) into the tree the gates
            # would run through. protected_path_touched — the standing
            # class for paths an agent must never write — independently of
            # the profile's list, like the venv-creation refusal one layer
            # up (review round on #74).
            if path == ".venv" or path.startswith(".venv/"):
                findings.append(
                    Finding(
                        "protected_path_touched",
                        f"tracked gate-venv path {verb}: {path} — a committed "
                        ".venv is planted environment, never project source",
                        file=path,
                    )
                )
                escalations.add("protected_path_touched")
            # Adding counts too: a new conftest.py or a new file under a
            # protected glob changes how the contract tests run. The task's
            # own contract test files are the exception — the implementer
            # must create them (red-first); once they exist, edits are locked.
            touched = verb in ("modified", "deleted") or (
                verb == "added" and path not in contract_files
            )
            if touched and _matches(path, protected):
                findings.append(
                    Finding(
                        "protected_path_touched",
                        f"protected path {verb}: {path}",
                        file=path,
                    )
                )
                escalations.add("protected_path_touched")
            if _matches(path, manifests) and not _pyproject_surface_unchanged(
                change, path, repo=repo, base_ref=base_ref, head_ref=head_ref,
            ):
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

    if matched_flags is None:
        matched_flags = matched_risk_flags(paths.get("risk") or {}, changes)
    risk_flags_added = sorted(matched_flags - set(task.get("risk") or []))
    if risk_flags_added:
        escalations.add("risk_path_touched")

    findings.sort(key=lambda f: (f.id, f.file or "", f.line or 0))
    return findings, sorted(escalations), risk_flags_added, matched_flags


def attribution_findings(repo: Path, base_ref: str, head_ref: str) -> list:
    """FAIL findings for AI-attribution trailers in the branch's own
    commits (F-42 AC-1).

    The commit messages between the merge-base and head are matched
    against :data:`factory.attribution.ATTRIBUTION_PATTERNS` — the ONE
    list the publish step refuses PR text with, so a trailer blocked here
    is also blocked at the PR seam and a squash commit can't inherit one.
    Only the branch's own commits are scanned: trailers already sitting on
    the base (this repo's ``main`` carries five) belong to the base's
    history, which F-42 deliberately does not rewrite (AC-6). A human
    ``Co-Authored-By`` passes — the patterns name AI authors and AI vendor
    addresses, not the trailer itself.

    Args:
        repo: Repository holding ``base_ref`` and ``head_ref``.
        base_ref: The base the slice branched from.
        head_ref: The slice's branch ref.

    Returns:
        One ``attribution_trailer`` finding per offending commit, naming
        the commit, the matched line and the pattern class.
    """
    merge_base = _git(repo, "merge-base", base_ref, head_ref).strip()
    log = _git(repo, "log", f"{merge_base}..{head_ref}",
               "--pretty=format:%H%x1f%B%x1e")
    findings: list[Finding] = []
    for record in log.split("\x1e"):
        record = record.removeprefix("\n")
        if not record.strip():
            continue
        sha, _, message = record.partition("\x1f")
        match = attribution_match(message)
        if match is not None:
            name, line = match
            findings.append(
                Finding(
                    "attribution_trailer",
                    f"commit {sha[:12]}: AI attribution in the commit message "
                    f"({name}): {line[:200]!r} — rewrite this branch's history "
                    "without it; a human Co-Authored-By passes, machine "
                    "attribution does not",
                )
            )
    return findings


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
    runner: CommandRunner | None = None,
) -> L0Report:
    """Run the full L0 gate on a checkout of the slice's branch.

    The caller (F-10) passes a clean clone checked out at ``head_ref``:
    the diff comes from refs but the commands run on the working tree, so
    a checkout at any other commit, or with uncommitted or untracked
    files, raises ``GateError`` instead of gating code nobody reviewed.
    Commands are shell strings from
    ``project.yaml`` and run through ``runner``: unconfined by default
    (fixture tests); F-10 passes the F-05 sandboxed runner, because the
    commands execute the agent's code.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        repo: Repository holding ``base_ref`` and ``head_ref``.
        base_ref: Base ref (compared from the merge-base).
        head_ref: The slice's branch ref.
        task: The task record (risk flags, contract test refs).
        baseline: Baseline mapping; when ``None``, loaded from the
            profile's ``baselines`` path as committed at ``base_ref``.
        timeout_sec: Per-command wall-clock cap.
        runner: Executes each profile command. Default: unconfined
            :func:`~factory.gates.run_command`; F-10 passes the sandboxed
            runner (the commands execute the agent's code).

    Returns:
        The ``L0Report``; verdict is ``"fail"`` iff any finding exists.

    Raises:
        GateError: If git can't produce the diff (bad refs, git missing),
            or the checkout isn't a clean tree at ``head_ref``.
    """
    repo = Path(repo)
    _assert_checkout(repo, head_ref)
    changes = diff_file_changes(repo, base_ref, head_ref)
    # Matched risk flags: computed ONCE here and passed through the diff
    # policy, the built-in sentinels and the project sentinels — one
    # implementation (factory.sentinels.matched_risk_flags), one
    # computation per run (F-25 review finding 7).
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    matched = matched_risk_flags(risk_globs, changes)
    findings, escalations, risk_flags_added, _matched = check_diff_policy(
        profile, changes, task, matched_flags=matched,
        repo=repo, base_ref=base_ref, head_ref=head_ref,
    )
    # F-42 AC-1: the branch's own commit messages carry no AI attribution
    # — before anything is pushed, on every round (each round re-runs L0,
    # and a fix round hands the finding back so the implementer rewrites
    # its own branch history).
    findings.extend(attribution_findings(repo, base_ref, head_ref))
    # F-25: the built-in sentinel pass — pure diff scans; a credential hit
    # escalates immediately (nothing is pushed before a passing round, but
    # a passing retry would push the branch whose local history still
    # holds the credential commit). Each sentinel leaves a result
    # (pass/fail) on the report so the attempt record and the PR body can
    # show what was checked. The project sentinel COMMANDS run later,
    # after setup (F-25 review finding 1).
    builtin_findings, sentinel_results, sentinel_escalations = builtin_sentinel_pass(
        profile, changes, matched
    )
    findings.extend(builtin_findings)
    escalations = sorted(set(escalations) | set(sentinel_escalations))

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

    results: list[CommandResult] = []
    setup_failed = False
    setup_command = commands.get("setup")
    if setup_command:
        result = (runner or _run_command)("setup", setup_command, repo, timeout_sec)
        results.append(result)
        if result.timed_out or result.exit_code != 0:
            findings.append(
                Finding(
                    "setup_failed",
                    f"setup failed (exit {result.exit_code}); remaining gates skipped",
                )
            )
            setup_failed = True
    if not setup_failed:
        # Project sentinel commands run only after setup has succeeded
        # (F-25 review finding 1): a sentinel may need the environment
        # setup installs, and a fresh-clone failure before setup would
        # escalate a correct change. When setup fails they are recorded
        # skipped, never run.
        project_findings, project_results, project_escalations = run_project_sentinels(
            profile, matched, repo, runner=runner, timeout_sec=timeout_sec
        )
        findings.extend(project_findings)
        sentinel_results.extend(project_results)
        escalations = sorted(set(escalations) | set(project_escalations))
    else:
        sentinel_results.extend(
            skipped_project_results(profile, "setup failed; sentinel not run")
        )

    ordered = [(g, commands.get(g)) for g in _GATE_ORDER]
    ordered += sorted((f"extra:{k}", v) for k, v in (commands.get("extra") or {}).items())
    if not setup_failed:
        for name, command in ordered:
            if not command:
                continue
            result = (runner or _run_command)(name, command, repo, timeout_sec)
            results.append(result)
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
            errors = sorted(
                {
                    (repo_relative(f, cwd, repo), n, msg)
                    for f, n, msg in errors
                }
            )
            owned = [e for e in errors if repo_owned(e[0])]
            for file, line, message in owned:
                if line in known_errors.get(file, set()):
                    preexisting.append(f"{file}:{line} pre-existing (baselined), not a failure")
                else:
                    where = f"{file}:{line}" + (f": {message}" if message else "")
                    findings.append(
                        Finding(
                            "new_error",
                            f"{name}: new error at {where} (not in baseline)",
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
            elif not owned:
                # Every parsed location was a framework/foreign frame
                # (dropped from new_error, INC-005) and no marker fired:
                # nothing in the repo or its baseline explains the
                # non-zero exit, so it fails like an unparsable one.
                findings.append(
                    Finding(
                        "command_failed",
                        f"{name} exited {result.exit_code} with no repo-owned error "
                        f"location: {result.output.strip()[:500]}",
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
        sentinel_results=sentinel_results,
    )
