"""L1 contract proof — red-first, mechanised (VALIDATOR-SPEC.md, layer L1).

Checks the claim behind every ``check.type: test`` item in a task's
approved contract instead of trusting it: the named test must FAIL on the
merge-base commit with only the diff's test files applied (the red proof),
and PASS with the full diff applied (the green proof). A test that already
passes on the merge-base proves nothing about the change — that is exactly
where false greens get through.

Mechanics (the caller — F-10 — passes a clean clone of the slice's branch):

1. The repo is forced onto ``head_ref`` and cleaned, and each named ref is
   checked for existence. A ref that doesn't name a test (``path::name``),
   whose file is absent from the branch, or whose named test the runner
   can't find, is a FAIL with ``contract_ambiguous``: the item can't be
   checked as written (VALIDATOR-SPEC L1 step 3).
2. Red: ``git checkout --detach`` the **merge-base** of base and head —
   the diff is ``base...head``, so it is the merge-base tree the red
   proof must be built on, never base's tip, which may have moved on.
   Apply ONLY the diff's test-file changes (added/modified files checked
   out from head; deleted and renamed-away files removed — each *path*
   judged independently, so a rename across the test boundary moves only
   its test half), run each ref. Exit 0 is a FAIL
   (``test_proves_nothing``); collection errors and assertion failures
   both count as red — the question is "would it fail without the
   change".
3. Green: ``git checkout --force <head>``, run each ref. Any non-zero exit
   is a FAIL: ``contract_ambiguous`` when the runner's own output says
   the test wasn't found (anchored on the runner's message lines — a
   test *failing with* "404 Not Found" is a worker problem, not a broken
   contract), ``test_not_green`` when it ran and failed.

Between phases the tree is ``git clean``-ed: a stale ``__pycache__``
written by the red run survives a same-size checkout (Python's mtime+size
check has one-second granularity) and would make green run base's
bytecode. Each red run is also classified (``red_reason``: assertion,
collection or unknown) for the L2 reviewer's test-quality pass — the
verdict never depends on the classification.
The per-ref command comes from ``commands.test_ref`` — a shell
template whose ``{ref}``/``{path}``/``{name}``/``{python}`` placeholders
are substituted by literal replacement (shell braces like ``${VAR}`` pass
through untouched), defaulting to pytest via ``python -m`` so the repo
root lands on ``sys.path`` the way a project's own CI runs it. The repo
is left on ``head_ref`` afterwards, whatever the verdict.

Known limit: a helper *outside* ``gates.TEST_GLOBS`` that the diff ships
and the test imports (not ``conftest.py``, which counts as test
infrastructure) can make red fail for an unrelated reason and mask a
proves-nothing test. The red output rides in the report for the L2
reviewer; a project-level test-support list is future work.

Deferred beyond F-08: mapping the non-test acceptance items to evidence
(command exit codes, scheduled holdout/probe checks) — that assembly
belongs to the full validator report F-10 owns.
"""

import contextlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from factory.gates import (
    CommandResult,
    CommandRunner,
    Finding,
    GateError,
    GateReport,
    contract_test_files,
    contract_test_refs,
    diff_name_status,
    git,
    is_test_file,
    ref_path,
    run_command,
)
from factory.runs import GIT_HARDENING

# Runs one named test. {ref} = the whole node id, {path}/{name} = its two
# halves (jest-style runners take them as separate arguments), {python} =
# the orchestrator's interpreter. ``python -m`` (not bare pytest) so the
# repo root lands on sys.path the way project CI does.
DEFAULT_TEST_REF = "{python} -m pytest -q {ref}"

# Runner output meaning "the named test doesn't exist" rather than "it ran
# and failed": pytest's own not-found error line and empty-run summary.
# Anchored on the line start so a test whose *output* contains "404 Not
# Found" or "command not found" classifies as test_not_green, not as a
# broken contract.
_NOT_FOUND = (
    re.compile(r"^ERROR: not found:", re.MULTILINE),
    re.compile(r"^no tests ran in \d", re.MULTILINE),
)

# How a red run went red (F-09, VALIDATOR-SPEC L1 "red for the wrong
# reason"). The epistemic class that matters downstream is "red only
# because a name was missing" — the case the L2 reviewer is told to
# scrutinise, because the red phase proved name-absence, not behaviour.
# Two pytest shapes land in it: a module-level import of a missing module
# (file-level ``ERROR`` short-summary lines, no ``::test_id``) and a
# missing name hit at RUNTIME inside the test body (NameError,
# module-has-no-attribute, ModuleNotFoundError, cannot-import-name — the
# most common red-first shape for a brand-new function). Assertion
# failures are ``FAILED`` lines naming assert/AssertionError; anything
# else red is ``unknown``. The reason rides in the report for the L2
# reviewer's test-quality pass; the verdict never depends on it.
_RED_COLLECTION = (
    re.compile(r"(?m)^errors during collection"),
    re.compile(r"(?m)^ERROR collecting "),
    re.compile(r"ImportError while importing test module"),
    re.compile(r"(?m)^ERROR \S+$"),
)
_RED_NAME_MISSING = (
    re.compile(r"NameError: name '"),
    re.compile(r"AttributeError: module '"),
    re.compile(r"ModuleNotFoundError: No module named"),
    re.compile(r"ImportError: cannot import name"),
)
_RED_ASSERTION = (
    re.compile(r"(?m)^FAILED .*assert"),
    re.compile(r"AssertionError"),
)


def red_reason(result: CommandResult) -> str | None:
    """Classify how a red run went red: ``assertion``, ``collection`` or
    ``unknown`` — or None when the run wasn't red (green, or timed out
    before producing a verdict).

    ``collection`` covers both name-missing shapes: collection-time import
    errors and runtime NameError/AttributeError/ModuleNotFoundError/
    cannot-import-name failures, which prove the same thing — a name was
    absent — and so deserve the same reviewer scrutiny. Assertion is
    checked before name-missing so an assert whose *message* quotes a
    ``NameError:`` still counts as an assertion red.

    Args:
        result: The red-phase :class:`~factory.gates.CommandResult` for one
            contract test ref.

    Returns:
        The red reason, or None when no red verdict exists to classify.
    """
    if result.exit_code is None or result.exit_code == 0:
        return None
    if any(pattern.search(result.output) for pattern in _RED_COLLECTION):
        return "collection"
    if any(pattern.search(result.output) for pattern in _RED_ASSERTION):
        return "assertion"
    if any(pattern.search(result.output) for pattern in _RED_NAME_MISSING):
        return "collection"
    return "unknown"


@dataclass
class L1Report(GateReport):
    """The L1 verdict plus everything F-09/F-10 consume downstream.

    ``red_reasons`` maps each provable contract test ref to how its red run
    went red — ``assertion``, ``collection`` or ``unknown`` — or None when
    the ref never produced a red verdict (green on base, or a timeout).
    The verdict itself never depends on it; the L2 reviewer's test-quality
    pass reads it (a ref red only by ``collection`` proved just that a name
    was missing, and deserves the closest scrutiny).
    """

    layer: ClassVar[str] = "L1"
    red_reasons: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "verdict": self.verdict,
            "findings": self.findings_as_dicts(),
            "commands": self.commands_as_dicts(),
            "red_reasons": dict(self.red_reasons),
        }


def _ref_command(template: str, ref: str) -> str:
    """Render the per-ref shell command from the template.

    Literal ``str.replace`` per placeholder — no ``str.format``: shell
    braces such as ``${VAR}`` or ``{{`` must pass through unharmed, and
    an unknown ``{placeholder}`` stays literal for the shell to judge.
    """
    path = ref_path(ref) or ref
    name = ref.partition("::")[2] if "::" in ref else ref
    command = template
    for placeholder, value in (
        ("python", sys.executable),
        ("ref", ref),
        ("path", path),
        ("name", name),
    ):
        command = command.replace("{" + placeholder + "}", shlex.quote(value))
    return command


def _merge_base(repo: Path, base_ref: str, head_ref: str) -> str:
    """The commit the ``base...head`` diff is computed from."""
    out = git(repo, "merge-base", base_ref, head_ref).strip()
    if not out:
        raise GateError(f"no common ancestor for {base_ref} and {head_ref}")
    return out


def _clean(repo: Path, keep: tuple[str, ...] = ()) -> None:
    """Drop untracked files (bytecode, caches) the phases leave behind.

    A red-phase ``__pycache__`` can survive into green when source size
    and mtime second are unchanged, and Python would then import base's
    bytecode. Only safe on the documented clean-clone checkout.

    ``keep`` names the profile's ``paths.setup_outputs`` (an installed
    venv, ``node_modules``): without them every ref fails with "command
    not found" in both phases, and a healthy test reads as red-and-not-
    green. Each is excluded as an anchored path, so only that directory
    survives; its contents came from setup, not from either phase.
    """
    excludes = [arg for path in keep for arg in ("-e", "/" + path.strip("/"))]
    git(repo, "clean", "-fdx", *excludes)


def _setup_outputs(profile: dict) -> tuple[str, ...]:
    """The profile's declared setup outputs, validated as plain repo paths."""
    outputs = (profile.get("paths") or {}).get("setup_outputs") or []
    for path in outputs:
        parts = Path(path).parts
        if not path or Path(path).is_absolute() or ".." in parts or path.startswith("~"):
            raise GateError(f"paths.setup_outputs entry {path!r} must be a repo-relative path")
    return tuple(outputs)


def _file_on_ref(repo: Path, ref: str, path: str) -> bool:
    """True when ``path`` exists in ``ref``'s tree."""
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
        capture_output=True,
    )
    return proc.returncode == 0


def _apply_test_changes(repo: Path, head_ref: str, changes: list, ref_files: set) -> None:
    """Stage the diff's test-file side onto the current (merge-base) checkout.

    Each path is applied independently, by whether THAT path is a test
    path (globs, or named by the contract): added/modified test files
    take head's content, deleted and renamed-away test files are removed.
    A rename that crosses the test boundary therefore moves only its
    test half — removing a base implementation file, or checking out a
    head implementation file, would fake the red either way.
    """

    def test_side(path: str | None) -> bool:
        return bool(path) and (is_test_file(path) or path in ref_files)

    for change in changes:
        if change.status == "D":
            if test_side(change.path):
                git(repo, "rm", "-q", "--", change.path)
            continue
        if test_side(change.path):
            git(repo, "checkout", head_ref, "--", change.path)
        if change.old_path and test_side(change.old_path):
            git(repo, "rm", "-q", "--", change.old_path)


def run_l1(
    profile: dict,
    repo: Path | str,
    base_ref: str,
    head_ref: str,
    task: dict | None = None,
    timeout_sec: int = 600,
    runner: CommandRunner | None = None,
) -> L1Report:
    """Run the L1 red-first proof on a checkout of the slice's branch.

    The caller (F-10) passes a clean clone; any repo holding both refs
    works, so tests run fixtures directly. The gate moves the checkout
    around — merge-base for red, head for green, ``git clean`` between
    phases — and leaves it on ``head_ref``.

    Args:
        profile: The loaded ``project.yaml`` mapping; only
            ``commands.test_ref`` is read (optional template override).
        repo: Repository holding ``base_ref`` and ``head_ref``.
        base_ref: Base ref the slice is compared against (diffed from
            the merge-base, as is the red proof).
        head_ref: The slice's branch ref.
        task: The task record whose ``check.type: test`` acceptance
            items name the tests to prove red-first.
        timeout_sec: Per-run wall-clock cap.
        runner: Executes each per-ref command. Default: unconfined
            :func:`~factory.gates.run_command`; F-10 passes the sandboxed
            runner (the refs execute the agent's test code).

    Returns:
        The ``L1Report``; verdict is ``"fail"`` iff any finding exists.

    Raises:
        GateError: If git fails (bad refs, no common ancestor).
    """
    repo = Path(repo)
    task = task or {}
    runner = runner or run_command
    keep = _setup_outputs(profile)
    template = (profile.get("commands") or {}).get("test_ref") or DEFAULT_TEST_REF

    refs = contract_test_refs(task)
    findings: list[Finding] = []
    results: list[CommandResult] = []

    git(repo, "checkout", "--force", head_ref)
    _clean(repo, keep)
    base = _merge_base(repo, base_ref, head_ref)
    ref_files = contract_test_files(task)
    test_changes = [
        c
        for c in diff_name_status(repo, base_ref, head_ref)
        if is_test_file(c.path) or is_test_file(c.old_path or "") or c.path in ref_files
    ]

    provable: list[str] = []
    reasons: dict[str, str | None] = {}
    for ref in refs:
        path = ref_path(ref)
        if path is None:
            findings.append(
                Finding(
                    "contract_ambiguous",
                    f"test ref does not name a test (expected path::name): {ref}",
                )
            )
        elif not _file_on_ref(repo, head_ref, path):
            findings.append(
                Finding(
                    "contract_ambiguous",
                    f"test file {path} named by the contract is not on {head_ref}",
                    file=path,
                )
            )
        else:
            provable.append(ref)

    try:
        git(repo, "checkout", "--detach", base)
        _apply_test_changes(repo, head_ref, test_changes, ref_files)
        for ref in provable:
            result = runner(f"red:{ref}", _ref_command(template, ref), repo, timeout_sec)
            results.append(result)
            reasons[ref] = red_reason(result)
            if result.timed_out:
                findings.append(
                    Finding("command_timeout", f"red run of {ref} exceeded {timeout_sec}s")
                )
            elif result.exit_code == 0:
                findings.append(
                    Finding(
                        "test_proves_nothing",
                        f"{ref} passes on the base commit ({base[:12]}, merge-base of "
                        f"{base_ref} and {head_ref}) with only the diff's test files; "
                        "a test that already passes on base cannot prove the change",
                        file=ref_path(ref),
                    )
                )

        git(repo, "checkout", "--force", head_ref)
        _clean(repo, keep)
        for ref in provable:
            result = runner(f"green:{ref}", _ref_command(template, ref), repo, timeout_sec)
            results.append(result)
            if result.timed_out:
                findings.append(
                    Finding("command_timeout", f"green run of {ref} exceeded {timeout_sec}s")
                )
            elif result.exit_code != 0:
                if any(p.search(result.output) for p in _NOT_FOUND):
                    findings.append(
                        Finding(
                            "contract_ambiguous",
                            f"{ref} does not exist on {head_ref}: "
                            f"{result.output.strip()[:200]}",
                            file=ref_path(ref),
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            "test_not_green",
                            f"{ref} fails with the full diff applied",
                            file=ref_path(ref),
                        )
                    )
    finally:
        # Leave the caller's repo on the slice branch, gate or no gate. A
        # restore failure never masks the gate's own error.
        with contextlib.suppress(GateError):
            git(repo, "checkout", "--force", head_ref)
            _clean(repo, keep)

    findings.sort(key=lambda f: (f.id, f.file or "", f.text))
    return L1Report(
        verdict="fail" if findings else "pass",
        findings=findings,
        command_results=results,
        red_reasons=reasons,
    )
