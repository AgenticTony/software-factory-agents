"""Built-in diff sentinels and project sentinel commands (BUILD-ORDER F-25).

Risk paths today force a human merge but nothing checks what the diff does
there. This module is L0's standing set of "security sentinel" invariants
(the Cursor pattern): named checks that run on every diff and FAIL with the
sentinel's name, so a human merge sees exactly what was checked, pass or
fail.

All four built-in scanners live here — each sentinel's scanner and its
result are produced together, so renaming a finding id can no longer
silently orphan a sentinel result behind an always-``pass`` row:

- ``assertion_removed`` — an assertion line removed from an existing test
  without a replacement (a moved assertion trips it; a human reviews).
- ``credential_added`` — a credential-shaped string (private-key block,
  well-known token prefixes, a generic secret assignment) on an added line
  in ANY file. The finding text names the matched shape and NEVER echoes
  the line: findings ride into reports, prompts and PR bodies, so the
  sentinel must not become the leak it exists to catch. A credential also
  ESCALATES ``credential_added`` — removing the line next round cannot
  un-leak it from the branch history ("retrying can't make it safe").
- ``lint_suppression_added`` — ``# noqa``, ``type: ignore`` or a
  lint-disable comment on an added line in a file under a ``paths.risk``
  glob. Outside risk paths the same comment is ordinary code review
  territory. When the same line also matched a credential shape, the
  finding does not echo the line (same no-echo rule).
- ``skip_marker_added`` — a skip/xfail/only marker added on an existing
  test.

Projects extend the set per risk flag in ``project.yaml``::

    sentinels:
      payments:
        - command: grep -r "TODO" src/pay || test $? -eq 1
          description: no TODOs left in payments code

Each command runs (through the same sandboxed runner as the other gate
commands) only when the diff touches that flag's ``paths.risk`` globs, and
only AFTER ``commands.setup`` has succeeded — a sentinel may need the
environment setup installs, and a fresh-clone failure before setup would
escalate a correct change. When setup fails, project sentinels are recorded
``skipped``. A failing sentinel FAILs the verdict and escalates
``sentinel_failed``; untouched flags also record ``skipped`` — the human
sees what was checked, and what deliberately wasn't. Sentinel COMMAND
OUTPUT is redacted through :data:`CREDENTIAL_PATTERNS` before it reaches
any finding or result: a payments scan that greps for keys prints exactly
the strings that must not travel further.

:data:`CREDENTIAL_PATTERNS` and :func:`redact_credentials` are the reusable
shape list and scrubber (F-30's trace redaction and any later consumer
import them from here rather than re-deriving them).
"""

import re
from pathlib import Path

from factory.gates import CommandRunner, Finding, is_test_file, matches, run_command

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


def redact_credentials(text: str) -> str:
    """Replace every credential-shaped match in ``text`` with ``[redacted]``.

    Sentinel command output (a payments scan that greps for keys prints
    exactly what it found) and any other untrusted text that must travel
    into findings, reports or PR bodies goes through here first.
    """
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


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
BUILTIN_SENTINELS = (
    "assertion_removed",
    "credential_added",
    "lint_suppression_added",
    "skip_marker_added",
)


def matched_risk_flags(risk_globs: dict, changes) -> set:
    """The risk flags whose ``paths.risk`` globs the diff touches.

    THE single implementation of "which risk flags did this diff touch"
    (owner review finding 7): L0 computes it once and passes it through —
    the builtin pass, the project sentinels and ``check_diff_policy``
    consume the same set. A rename touches both its old and new path (a
    rename away from a risk path still touched it).
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


def _credential_shape(text: str) -> str | None:
    """The first credential shape that matches ``text``, else None."""
    for name, pattern in _CREDENTIAL_SHAPES:
        if pattern.search(text):
            return name
    return None


def builtin_sentinel_findings(changes, risk_globs: dict) -> list:
    """Scan the diff with all four built-in sentinels.

    Args:
        changes: ``FileChange`` list from ``diff_file_changes``.
        risk_globs: The profile's ``paths.risk`` mapping.

    Returns:
        Findings for ``credential_added`` (any file) and
        ``lint_suppression_added`` (risk-path files only) — those two
        scanners plus the skip-marker and assertion scanners below, one
        home for the whole standing set. Credential-bearing lines are
        never echoed: findings name the shape only.
    """
    findings: list[Finding] = []
    all_risk_globs = [glob for globs in risk_globs.values() for glob in globs]
    for change in changes:
        in_risk_path = matches(change.path, all_risk_globs)
        # Skip/assertion scanning follows the rename semantics of
        # check_diff_policy's event split: a rename's old half is judged by
        # its own side, so a test renamed INTO the test paths scans by its
        # new path, one renamed OUT by its old.
        scan_path = change.old_path if is_test_file(change.old_path or "") else change.path
        test_file = is_test_file(scan_path)
        for no, text in change.added_lines:
            shape = _credential_shape(text)
            if shape is not None:
                findings.append(
                    Finding(
                        "credential_added",
                        f"credential-shaped string added in {change.path}:{no} "
                        f"({shape}); the line is not echoed here",
                        file=change.path,
                        line=no,
                    )
                )
            if test_file and any(p.search(text) for p in SKIP_MARKER_PATTERNS):
                findings.append(
                    Finding(
                        "skip_marker_added",
                        f"skip/only marker added in {scan_path}:{no}: {text.strip()}",
                        file=scan_path,
                        line=no,
                    )
                )
            if in_risk_path and any(p.search(text) for p in LINT_SUPPRESSION_PATTERNS):
                echo = (
                    text.strip() if shape is None
                    else f"line not echoed (matched credential shape: {shape})"
                )
                findings.append(
                    Finding(
                        "lint_suppression_added",
                        f"lint suppression added in {change.path}:{no}: {echo}",
                        file=change.path,
                        line=no,
                    )
                )
        if test_file:
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
    findings.sort(key=lambda f: (f.id, f.file or "", f.line or 0))
    return findings


def builtin_sentinel_pass(profile: dict, changes, findings, matched: set):
    """The pure half of the sentinel pass: scan, derive results, escalate.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        changes: ``FileChange`` list from ``diff_file_changes``.
        findings: ``check_diff_policy``'s findings (kept for signature
            symmetry; the built-in results are derived from the combined
            finding ids).
        matched: The diff's matched risk flags —
            :func:`matched_risk_flags` computed ONCE by the caller.

    Returns:
        ``(findings, results, escalations)`` — the four built-in scans'
        findings, one result dict per built-in sentinel (pass/fail), and
        the ``credential_added`` escalation when a credential was found
        (removing the line next round cannot un-leak it from the branch
        history, so retrying can't make it safe).
    """
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    scans = builtin_sentinel_findings(changes, risk_globs)
    finding_ids = {f.id for f in findings} | {f.id for f in scans}
    escalations: list[str] = []
    if "credential_added" in finding_ids:
        escalations.append("credential_added")
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
    return scans, results, escalations


def skipped_project_results(profile: dict, reason: str) -> list:
    """One ``skipped`` result per configured project sentinel, with why.

    Used when the sentinels cannot run at all — setup failed, so the
    environment they may need was never built.
    """
    config = profile.get("sentinels") or {}
    return [
        {
            "name": entry["description"],
            "kind": "project",
            "verdict": "skipped",
            "risk_flag": flag,
            "detail": reason,
        }
        for flag in sorted(config)
        for entry in config[flag]
    ]


def run_project_sentinels(
    profile: dict,
    matched: set,
    repo: Path | str,
    runner: CommandRunner | None = None,
    timeout_sec: int = 600,
):
    """Run the profile's ``sentinels.<risk flag>`` commands for touched flags.

    Called by L0 only after ``commands.setup`` has succeeded — a sentinel
    may need the environment setup installs (owner review finding 1).

    Args:
        profile: The loaded ``project.yaml`` mapping.
        matched: The diff's matched risk flags —
            :func:`matched_risk_flags` computed ONCE by the caller.
        repo: Repository the commands run in.
        runner: Executes each command; default unconfined
            :func:`~factory.gates.run_command` (L0 passes the sandboxed
            runner, the same one the gate commands go through).
        timeout_sec: Per-command wall-clock cap.

    Returns:
        ``(findings, results, escalations)`` — a FAIL finding plus the
        ``sentinel_failed`` escalation per failing command, and one result
        dict per configured sentinel (``skipped`` when the flag's globs
        were not touched). Command output is redacted through
        :func:`redact_credentials` before it reaches any text.
    """
    config = profile.get("sentinels") or {}
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
            output = redact_credentials(result.output.strip()[-200:])
            findings.append(
                Finding(
                    "sentinel_failed",
                    f"project sentinel failed ({flag}: {entry['description']}; "
                    f"{reason}): {redact_credentials(command)}\noutput: {output}",
                )
            )
            results.append({
                "name": entry["description"],
                "kind": "project",
                "verdict": "fail",
                "risk_flag": flag,
                "detail": output,
            })
            escalations.append("sentinel_failed")
    return findings, results, escalations
