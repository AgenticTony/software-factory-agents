"""Built-in diff sentinels and project sentinel commands (BUILD-ORDER F-25).

Risk paths today force a human merge but nothing checks what the diff does
there. This module is L0's standing set of "security sentinel" invariants
(the Cursor pattern): named checks that run on every diff and FAIL with the
sentinel's name, so a human merge sees exactly what was checked, pass or
fail.

Two of the four built-in sentinels predate this module — ``skip_marker_added``
and ``assertion_removed`` are line heuristics ``factory.l0.check_diff_policy``
has enforced since F-07. F-25 names the standing set, adds the two new
scanners below, and records a result per sentinel in the L0 report.

- ``credential_added`` — a credential-shaped string (private-key block,
  well-known token prefixes, a generic secret assignment) on an added line
  in ANY file. The finding text names the matched shape and NEVER echoes
  the line: findings ride into reports, prompts and PR bodies, so the
  sentinel itself must not become the leak it exists to catch. Heuristic
  by necessity — a human reviews, like ``assertion_removed``.
- ``lint_suppression_added`` — ``# noqa``, ``type: ignore`` or a lint-disable
  comment on an added line in a file under a ``paths.risk`` glob. Outside
  risk paths the same comment is ordinary code review territory.

Projects extend the set per risk flag in ``project.yaml``::

    sentinels:
      payments:
        - command: grep -r "TODO" src/pay || test $? -eq 1
          description: no TODOs left in payments code

Each command runs (through the same sandboxed runner as the other gate
commands) only when the diff touches that flag's ``paths.risk`` globs; a
failure FAILs the verdict and escalates ``sentinel_failed``. Untouched
flags record their sentinels as ``skipped`` — the human sees what was
checked, and what deliberately wasn't.

:data:`CREDENTIAL_PATTERNS` is the reusable shape list (F-30's trace
redaction and any later consumer import it from here rather than
re-deriving it).
"""

import re
from pathlib import Path

from factory.gates import CommandRunner, Finding, matches, run_command

# (shape name, pattern) — the name is what findings and results carry.
_CREDENTIAL_SHAPES = (
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key"
            r"|client[_-]?secret)\b[^\n]{0,20}?[\"'][A-Za-z0-9+/=_\-]{16,}[\"']"
        ),
    ),
)

# The credential shapes as bare patterns, in the same order — the list F-30
# (trace redaction) and later consumers reuse.
CREDENTIAL_PATTERNS = tuple(pattern for _, pattern in _CREDENTIAL_SHAPES)

# Added lines matching any of these in a file under a risk-path glob ->
# lint_suppression_added.
LINT_SUPPRESSION_PATTERNS = (
    re.compile(r"#\s*noqa"),
    re.compile(r"#\s*type:\s*ignore\b"),
    re.compile(r"@\s*ts-(?:ignore|expect-error)\b"),
    re.compile(r"//\s*eslint-disable"),
    re.compile(r"/\*\s*eslint-disable"),
    re.compile(r"<!--\s*(?:eslint|ruff|biome)-disable"),
    re.compile(r"#\s*(?:ruff|flake8)\s*:\s*(?:noqa|ignore)"),
    re.compile(r"#\s*pylint:\s*(?:disable|skip-file)"),
)

# The standing built-in sentinel set, sorted for deterministic result order.
# skip_marker_added and assertion_removed are emitted by
# factory.l0.check_diff_policy (F-07); the other two by this module.
BUILTIN_SENTINELS = (
    "assertion_removed",
    "credential_added",
    "lint_suppression_added",
    "skip_marker_added",
)


def matched_risk_flags(risk_globs: dict, changes) -> set:
    """The risk flags whose ``paths.risk`` globs the diff touches.

    A rename touches both its old and new path (a rename away from a risk
    path still touched it), matching check_diff_policy's rename semantics.
    """
    matched: set = set()
    for change in changes:
        paths = {change.path}
        if change.old_path:
            paths.add(change.old_path)
        for flag, globs in risk_globs.items():
            if any(matches(path, globs) for path in paths):
                matched.add(flag)
    return matched


def builtin_sentinel_findings(changes, risk_globs: dict) -> list:
    """Scan added lines for the two F-25 built-in sentinels.

    ``skip_marker_added`` and ``assertion_removed`` (the other two built-ins)
    are line heuristics inside :func:`factory.l0.check_diff_policy` and are
    not re-scanned here.

    Args:
        changes: ``FileChange`` list from ``diff_file_changes``.
        risk_globs: The profile's ``paths.risk`` mapping.

    Returns:
        Findings for ``credential_added`` (any file) and
        ``lint_suppression_added`` (risk-path files only). Credential
        findings name the matched shape; the line itself is never echoed.
    """
    findings: list[Finding] = []
    all_risk_globs = [glob for globs in risk_globs.values() for glob in globs]
    for change in changes:
        in_risk_path = matches(change.path, all_risk_globs)
        for no, text in change.added_lines:
            for name, pattern in _CREDENTIAL_SHAPES:
                if pattern.search(text):
                    findings.append(
                        Finding(
                            "credential_added",
                            f"credential-shaped string added in {change.path}:{no} "
                            f"({name}); the line is not echoed here",
                            file=change.path,
                            line=no,
                        )
                    )
                    break
            if in_risk_path and any(p.search(text) for p in LINT_SUPPRESSION_PATTERNS):
                findings.append(
                    Finding(
                        "lint_suppression_added",
                        f"lint suppression added in {change.path}:{no}: {text.strip()}",
                        file=change.path,
                        line=no,
                    )
                )
    findings.sort(key=lambda f: (f.id, f.file or "", f.line or 0))
    return findings


def run_project_sentinels(
    profile: dict,
    changes,
    repo: Path | str,
    runner: CommandRunner | None = None,
    timeout_sec: int = 600,
):
    """Run the profile's ``sentinels.<risk flag>`` commands for touched flags.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        changes: ``FileChange`` list from ``diff_file_changes``.
        repo: Repository the commands run in.
        runner: Executes each command; default unconfined
            :func:`~factory.gates.run_command` (L0 passes the sandboxed
            runner, the same one the gate commands go through).
        timeout_sec: Per-command wall-clock cap.

    Returns:
        ``(findings, results, escalations)`` — a FAIL finding plus the
        ``sentinel_failed`` escalation per failing command, and one result
        dict per configured sentinel (``skipped`` when the flag's globs
        were not touched).
    """
    config = profile.get("sentinels") or {}
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    matched = matched_risk_flags(risk_globs, changes)
    repo = Path(repo)
    findings: list[Finding] = []
    results: list[dict] = []
    escalations: list[str] = []
    for flag in sorted(config):
        for entry in config[flag]:
            command = entry["command"]
            if flag not in matched:
                results.append({
                    "name": entry["description"],
                    "kind": "project",
                    "verdict": "skipped",
                    "risk_flag": flag,
                    "detail": "risk path not touched",
                })
                continue
            result = (runner or run_command)(
                f"sentinel:{flag}", command, repo, timeout_sec
            )
            if result.exit_code == 0 and not result.timed_out:
                results.append({
                    "name": entry["description"],
                    "kind": "project",
                    "verdict": "pass",
                    "risk_flag": flag,
                })
                continue
            reason = "timed out" if result.timed_out else f"exit {result.exit_code}"
            findings.append(
                Finding(
                    "sentinel_failed",
                    f"project sentinel failed ({flag}: {entry['description']}; "
                    f"{reason}): {command}",
                )
            )
            results.append({
                "name": entry["description"],
                "kind": "project",
                "verdict": "fail",
                "risk_flag": flag,
                "detail": result.output.strip()[-200:],
            })
            escalations.append("sentinel_failed")
    return findings, results, escalations


def run_sentinels(
    profile: dict,
    changes,
    findings,
    repo: Path | str,
    runner: CommandRunner | None = None,
    timeout_sec: int = 600,
):
    """The full F-25 sentinel pass L0 runs right after the diff policy.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        changes: ``FileChange`` list from ``diff_file_changes``.
        findings: ``check_diff_policy``'s findings — the skip-marker and
            assertion sentinels are read from them, not re-scanned.
        repo: Repository the project sentinel commands run in.
        runner: Command runner (see :func:`run_project_sentinels`).
        timeout_sec: Per-command wall-clock cap.

    Returns:
        ``(findings_to_add, sentinel_results, escalations_to_add)`` — the
        new FAIL findings (new built-ins plus failed project sentinels),
        one result dict per sentinel (built-in and project, pass/fail/
        skipped), and the ``sentinel_failed`` escalation when a project
        sentinel failed.
    """
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    builtin_new = builtin_sentinel_findings(changes, risk_globs)
    finding_ids = {f.id for f in findings} | {f.id for f in builtin_new}
    matched = matched_risk_flags(risk_globs, changes)
    results = []
    for name in BUILTIN_SENTINELS:
        entry = {
            "name": name,
            "kind": "builtin",
            "verdict": "fail" if name in finding_ids else "pass",
        }
        if name == "lint_suppression_added":
            entry["detail"] = (
                "risk paths touched: " + ", ".join(sorted(matched))
                if matched
                else "no risk path touched"
            )
        results.append(entry)
    project_findings, project_results, escalations = run_project_sentinels(
        profile, changes, repo, runner=runner, timeout_sec=timeout_sec
    )
    results.extend(project_results)
    return builtin_new + project_findings, results, escalations
