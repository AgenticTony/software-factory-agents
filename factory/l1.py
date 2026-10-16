"""L1 contract proof — red-first, mechanised (VALIDATOR-SPEC.md, layer L1).

Checks the claim behind every ``check.type: test`` item in a task's
approved contract instead of trusting it: the named test must FAIL on the
base commit with only the diff's test files applied (the red proof), and
PASS with the full diff applied (the green proof). A test that already
passes on base proves nothing about the change — that is exactly where
false greens get through.

Mechanics (the caller — F-10 — passes a clean clone of the slice's branch):

1. The repo is forced onto ``head_ref`` and each named ref is checked for
   existence. A ref that doesn't name a test (``path::name``), whose file
   is absent from the branch, or whose named test the runner can't find,
   is a FAIL with ``contract_ambiguous``: the item can't be checked as
   written (VALIDATOR-SPEC L1 step 3).
2. Red: ``git checkout --detach <base>``, apply ONLY the diff's test-file
   changes (added/modified files checked out from head; deleted and
   renamed-away files removed), run each ref. Exit 0 is a FAIL
   (``test_proves_nothing``); collection errors and assertion failures
   both count as red — the question is "would it fail without the change".
3. Green: ``git checkout --force <head>``, run each ref. Any non-zero exit
   is a FAIL: ``contract_ambiguous`` when the runner reports the test
   wasn't found at all, ``test_not_green`` when it ran and failed.

The per-ref command comes from ``commands.test_ref`` — a shell template
with ``{ref}`` (the quoted node id) and ``{python}`` (the orchestrator's
interpreter) placeholders — defaulting to pytest via ``python -m`` so the
repo root lands on ``sys.path`` the way a project's own CI runs it. The
repo is left on ``head_ref`` afterwards, whatever the verdict.

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

# Shared L0 machinery: git access, command runner, findings, diff parsing.
from factory.l0 import (
    CommandResult,
    Finding,
    GateError,
    _git,
    _is_test_file,
    _run_command,
    contract_test_files,
    diff_file_changes,
)

# Runner output meaning "the named test doesn't exist" rather than "it
# failed": pytest's not-found error and its empty-collection summary.
_NOT_FOUND = re.compile(r"not found|no tests ran|no tests collected", re.IGNORECASE)


@dataclass
class L1Report:
    """The L1 verdict plus everything F-09/F-10 consume downstream."""

    verdict: str
    findings: list = field(default_factory=list)
    command_results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": "L1",
            "verdict": self.verdict,
            "findings": [f.__dict__ | {} for f in self.findings],
            "commands": [c.to_dict() for c in self.command_results],
        }


def contract_test_refs(task: dict) -> list:
    """Collect the named-test refs from a task's acceptance checks.

    Every ``check.type: test`` ref, in acceptance order, de-duplicated.
    Unlike :func:`factory.l0.contract_test_files` this keeps malformed
    refs too: they become ``contract_ambiguous`` findings instead of
    being silently dropped.

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


def _ref_command(template: str, ref: str) -> str:
    """Render the per-ref shell command from the template."""
    try:
        return template.format(ref=shlex.quote(ref), python=shlex.quote(sys.executable))
    except (KeyError, IndexError, ValueError) as exc:
        raise GateError(f"commands.test_ref template is invalid: {template!r}: {exc}") from exc


def _file_on_ref(repo: Path, ref: str, path: str) -> bool:
    """True when ``path`` exists in ``ref``'s tree."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
        capture_output=True,
    )
    return proc.returncode == 0


def _apply_test_changes(repo: Path, head_ref: str, changes: list) -> None:
    """Stage the diff's test-file side onto the current (base) checkout.

    Added/modified files take head's content; deleted and renamed-away
    files are removed. A rename stages both halves: remove the old path,
    check out the new one.
    """
    for change in changes:
        if change.status == "D":
            _git(repo, "rm", "-q", "--", change.path)
        elif change.status == "R":
            _git(repo, "rm", "-q", "--", change.old_path)
            _git(repo, "checkout", head_ref, "--", change.path)
        else:  # A, C, M, T
            _git(repo, "checkout", head_ref, "--", change.path)


def run_l1(
    profile: dict,
    repo: Path | str,
    base_ref: str,
    head_ref: str,
    task: dict | None = None,
    timeout_sec: int = 600,
) -> L1Report:
    """Run the L1 red-first proof on a checkout of the slice's branch.

    The caller (F-10) passes a clean clone; any repo holding both refs
    works, so tests run fixtures directly. The gate moves the checkout
    around (base for red, head for green) and leaves it on ``head_ref``.

    Args:
        profile: The loaded ``project.yaml`` mapping; only
            ``commands.test_ref`` is read (optional template override).
        repo: Repository holding ``base_ref`` and ``head_ref``.
        base_ref: Base ref the slice is compared against.
        head_ref: The slice's branch ref.
        task: The task record whose ``check.type: test`` acceptance
            items name the tests to prove red-first.
        timeout_sec: Per-run wall-clock cap.

    Returns:
        The ``L1Report``; verdict is ``"fail"`` iff any finding exists.

    Raises:
        GateError: If git fails or the ``test_ref`` template is invalid
            (before the checkout is touched).
    """
    repo = Path(repo)
    task = task or {}
    template = (profile.get("commands") or {}).get("test_ref") or "{python} -m pytest -q {ref}"

    refs = contract_test_refs(task)
    findings: list[Finding] = []
    results: list[CommandResult] = []

    # A broken template must fail before the gate moves the checkout.
    _ref_command(template, "probe::probe")

    _git(repo, "checkout", "--force", head_ref)
    ref_files = contract_test_files(task)
    test_changes = [
        c
        for c in diff_file_changes(repo, base_ref, head_ref)
        if _is_test_file(c.path) or _is_test_file(c.old_path or "") or c.path in ref_files
    ]

    provable: list[str] = []
    for ref in refs:
        path = ref.split("::", 1)[0] if "::" in ref else None
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
        _git(repo, "checkout", "--detach", base_ref)
        _apply_test_changes(repo, head_ref, test_changes)
        for ref in provable:
            result = _run_command(f"red:{ref}", _ref_command(template, ref), repo, timeout_sec)
            results.append(result)
            if result.timed_out:
                findings.append(
                    Finding("command_timeout", f"red run of {ref} exceeded {timeout_sec}s")
                )
            elif result.exit_code == 0:
                findings.append(
                    Finding(
                        "test_proves_nothing",
                        f"{ref} passes on {base_ref} with only the diff's test files; "
                        "a test that already passes on base cannot prove the change",
                        file=ref.split("::", 1)[0],
                    )
                )

        _git(repo, "checkout", "--force", head_ref)
        for ref in provable:
            result = _run_command(f"green:{ref}", _ref_command(template, ref), repo, timeout_sec)
            results.append(result)
            if result.timed_out:
                findings.append(
                    Finding("command_timeout", f"green run of {ref} exceeded {timeout_sec}s")
                )
            elif result.exit_code != 0:
                if _NOT_FOUND.search(result.output):
                    findings.append(
                        Finding(
                            "contract_ambiguous",
                            f"{ref} does not exist on {head_ref}: "
                            f"{result.output.strip()[:200]}",
                            file=ref.split("::", 1)[0],
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            "test_not_green",
                            f"{ref} fails with the full diff applied",
                            file=ref.split("::", 1)[0],
                        )
                    )
    finally:
        # Leave the caller's repo on the slice branch, gate or no gate. A
        # restore failure never masks the gate's own error.
        with contextlib.suppress(GateError):
            _git(repo, "checkout", "--force", head_ref)

    findings.sort(key=lambda f: (f.id, f.file or "", f.text))
    return L1Report(
        verdict="fail" if findings else "pass",
        findings=findings,
        command_results=results,
    )
