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
  without a replacement. Replacement is per-file arithmetic (F-33):
  verbatim re-adds pair up as moves, and a file that ends the diff with
  as many assertion lines as it started is quiet; only a net loss flags,
  naming every removed line no re-add matches. A replacement must be a
  real assertion statement — a tautology or a string mentioning "assert"
  levels nothing (PR #47 review round), and a comment line is neither an
  assertion nor a replacement for one.
- ``credential_added`` — a credential-shaped string (private-key block,
  well-known token prefixes, a generic secret assignment) on an added line
  in ANY file. The finding text names the matched shape and NEVER echoes
  the line: findings ride into reports, prompts and PR bodies, so the
  sentinel must not become the leak it exists to catch. An environment
  READ (``os.environ.get("TOKEN", ...)``) is the safe pattern, not a
  leak: the generic assignment shape is tested against the line with
  the env call's head and first argument blanked, so the lookup itself
  is exempt while a 16+ literal anywhere else — a fallback argument, an
  ``or`` after the call, a comment's env mention — still flags; the
  prefix shapes (AKIA, ghp_, sk_live...) scan env-read lines anyway. A
  credential also ESCALATES ``credential_added`` — removing the line
  next round cannot un-leak it from the branch history ("retrying
  can't make it safe").
- ``lint_suppression_added`` — ``# noqa``, ``type: ignore`` or a
  lint-disable comment on an added line in a file under a ``paths.risk``
  glob, and only where that syntax is a comment in the file's language:
  ``# noqa`` inside JSON or markdown prose quotes the pattern, it cannot
  silence a linter there (F-33 corpus: PRs #25/#37 documenting these
  sentinels). Outside risk paths the same comment is ordinary code
  review territory. The echoed line goes through :func:`redact_credentials`,
  as do the skip-marker and assertion echoes, so a credential on the same
  line prints as ``[redacted]``.
- ``skip_marker_added`` — a skip/xfail/only marker added on an existing
  test. Substring-level against the line with quoted spans blanked, so
  every statement shape fires (a decorator, module-level ``pytestmark``,
  a one-line conditional, ``pytest.param`` marks, the JS forms) while
  marker text inside a string fixture stays inert — fixture data, not
  a marker (F-33 corpus, PR #8).

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
    # F-43 (I-10): the three shapes a seat env actually leaks. A JWT is
    # two eyJ-prefixed base64url segments (the JSON ``{"`` header) plus a
    # signature; ``ANTHROPIC_AUTH_TOKEN=<jwt>`` echoes carry no quotes, so
    # the quoted "secret assignment" shape never saw them.
    (
        "JSON web token",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{7,}\.eyJ[A-Za-z0-9_-]{7,}(?:\.[A-Za-z0-9_-]+)?"
        ),
    ),
    # An Authorization header printed into text: the scheme plus a long
    # unquoted value (case-insensitive scheme; the value alphabet is what
    # every bearer format in use shares).
    (
        "bearer token",
        re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._+/=-]{16,}"),
    ),
    # An unquoted YAML ``password:`` value: a bare 16+ run on the value
    # side of the mapping, where YAML needs no quotes at all. Line-anchored
    # so prose ("the password: see docs") and quoted values (the secret
    # assignment shape's territory) stay out.
    (
        "unquoted YAML password",
        re.compile(
            r"(?im)^[ \t-]*password:[ \t]*[A-Za-z0-9+/=_-]{16,}[ \t]*(?:#.*)?$"
        ),
    ),
    # F-43 review round 1 (finding 8): the bare env echo M-8 named —
    # ``ANTHROPIC_AUTH_TOKEN=<id>.<secret>`` — is not a JWT (the Z.ai key
    # shape), so no shape caught it. An UPPERCASE env-var name ending in
    # _TOKEN/_KEY/_SECRET/_PASSWORD, ``=``, and a 16+ unquoted value (the
    # value alphabet excludes quotes: quoted assignments are the secret
    # assignment shape's territory).
    (
        "environment variable echo",
        re.compile(
            r"\b[A-Z][A-Z0-9_]*(?:_TOKEN|_KEY|_SECRET|_PASSWORD)="
            r"[A-Za-z0-9+/=_.\-]{16,}"
        ),
    ),
)

# An environment READ is the safe way for a secret to travel: the env
# var's NAME next to the keyword (``token = os.environ.get(
# "ANTHROPIC_AUTH_TOKEN", "")``) is not a committed secret — the F-33
# corpus caught the "secret assignment" shape flagging exactly that
# (PR #18's runner). The exemption blanks ONLY the call head and its
# first argument (the name): a 16+ literal anywhere else on the line —
# a fallback argument, an ``or`` after the call, a trailing comment's
# env mention — is still tested and still flags (PR #47 review finding
# 3; matching the call's text anywhere on the line exempted committed
# secrets). Prefix shapes (AKIA, ghp_, sk_live…) scan env-read lines
# regardless.
_ENV_CALL_SPAN = re.compile(
    r"(?i)\b(?:os\.)?(?:environ\.get|getenv)\s*\(\s*(?:\"[^\"]*\"|'[^']*')?"
    r"|\benviron\s*\[[^\]]*\]"
    r"|process\.env\.\w+"
)

# The credential shapes as bare patterns, in the same order — the list F-30
# (trace redaction) and later consumers reuse.
CREDENTIAL_PATTERNS = tuple(pattern for _, pattern in _CREDENTIAL_SHAPES)


# A literal shorter than this is not a secret worth scrubbing — replacing
# a one-character "token" shreds ordinary text (a real seat token is long).
MIN_SECRET_LEN = 8
REDACTED = "[redacted]"


def redact_credentials(text: str) -> str:
    """Replace every credential-shaped match in ``text`` with ``[redacted]``.

    Sentinel command output (a payments scan that greps for keys prints
    exactly what it found) and any other untrusted text that must travel
    into findings, reports or PR bodies goes through here first.
    """
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def is_scrubbable_secret(secret: str | None) -> bool:
    """Whether ``secret`` is a real secret worth scrubbing.

    THE one rule (review round 1, finding 10): both :func:`scrub_literal`
    and the PR-text fence (:func:`factory.publish.check_pr_text`) call it,
    so F-59's per-provider formats can change what counts as a secret in
    one place — a fence that silently stops refusing what the seam still
    scrubs is exactly the drift this prevents.
    """
    return bool(secret) and len(secret) >= MIN_SECRET_LEN


def scrub_literal(text: str, secret: str | None) -> str:
    """Replace the literal secret (the seat token) with ``[redacted]``.

    Long secrets only (:func:`is_scrubbable_secret`): a short literal
    redacts noise, not secrets. F-30 defined this beside the trace writer;
    F-43 moved it here — beside :func:`redact_credentials`, whose sibling
    it is — so the egress seam (:mod:`factory.egress`) composes the two
    scrubbers from one home. :mod:`factory.trace` re-exports it unchanged
    (one implementation).
    """
    if is_scrubbable_secret(secret):
        return text.replace(secret, REDACTED)
    return text


# A single- or double-quoted span, escapes included — the tool that
# separates code from data on a diff line. Matching a marker or an
# assertion inside one would flag fixture strings (the F-33 corpus
# class: PR #8's scanner tests embed markers inside quoted patches), so
# every substring-level pattern below runs against the line with these
# spans blanked (PR #47 review finding 1: blanking spans, not anchoring
# to line start, keeps every statement shape catchable).
_QUOTED_SPAN = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


def _outside_strings(text: str) -> str:
    """``text`` with quoted spans blanked: matches here are code, not data."""
    return _QUOTED_SPAN.sub(" ", text)


# Added lines in test files matching any of these -> skip_marker_added.
# Substring-level against the string-blanked line: a marker is code
# wherever it sits on the line (a decorator, module-level ``pytestmark``,
# a one-line conditional, ``pytest.param`` marks, mocha forms), while the
# same text inside a quoted fixture is data and stays inert.
SKIP_MARKER_PATTERNS = (
    re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b"),
    re.compile(r"\bpytest\.skip\("),
    re.compile(r"\bskipTest\("),
    re.compile(r"@unittest\.skip"),  # covers skipIf/skipUnless: no \b
    re.compile(r"\bthis\.skip\("),
    re.compile(r"\b(?:it|describe|test|context)\.skip\("),
    re.compile(r"\b(?:xit|xdescribe)\("),
    re.compile(r"\b(?:it|describe|test)\.only\("),
)

# Removed lines in test files matching any of these -> assertion_removed.
# Substring-level on purpose: the removed side stays broad (a human
# reviews); what counts as a REPLACEMENT is the stricter classifier
# below (PR #47 review finding 2).
#
# F-44 (review I-11): a `pytest.raises` block is an assertion — removing
# the with-line (the body often carries no assert of its own) silently
# greens a test that used to demand failure. Both spellings count: the
# `pytest.raises` attribute form and the `from pytest import raises`
# bare form. unittest's `assertRaises`/`assertRaisesRegex` were already
# covered — the broad `\bassert` matches the "assert" inside them (the
# '.' before it is a word boundary).
ASSERTION_PATTERNS = (
    re.compile(r"\bassert"),
    re.compile(r"\bexpect\("),
    re.compile(r"\bpytest\.raises\b|\braises\s*\("),
)

# What counts as a replacement assertion: a real statement, not a
# tautology and not a string mentioning "assert". Runs against the
# string-blanked line so padding with prose can never level the count.
# `pytest.raises` sits on BOTH sides (F-44): a block MOVED within a file
# removes one with-line and adds one — counting it as a replacement keeps
# moves quiet exactly as moved asserts are.
_TAUTOLOGY_ASSERT = re.compile(
    r"""^assert\s+(?:True|False|None|[-\d.]+|(["']).*\1)\s*$"""
)
_ASSERTION_STATEMENT = (
    re.compile(r"^assert\b"),           # a bare assert statement
    re.compile(r"^\w+\.assert\w*\("),   # self.assertEqual / mock.assert_…
    re.compile(r"\bexpect\("),          # jest/vitest
    re.compile(r"\bpytest\.raises\b|\braises\s*\("),
)


def _is_replacement_assertion(text: str) -> bool:
    """True when ``text`` is an assertion that can replace a removed one.

    ``assert True`` (or any constant) is a tautology, a string or
    comment mentioning "assert" is prose: neither restores what a
    removed assertion checked, so neither levels the per-file count.
    """
    line = text.strip()
    if not _is_code_line(text) or _TAUTOLOGY_ASSERT.fullmatch(line):
        return False
    return any(p.search(_outside_strings(line)) for p in _ASSERTION_STATEMENT)

# Comment openers across the languages the marker and assertion patterns
# cover: a line starting with one is prose about code, not code — it is
# neither an assertion nor a replacement for one.
_COMMENT_OPENERS = ("#", "//", "*", "<!--")


def _is_code_line(text: str) -> bool:
    """True when ``text`` is a code line (not blank, not a comment)."""
    stripped = text.lstrip()
    return bool(stripped) and not stripped.startswith(_COMMENT_OPENERS)

# (required comment syntax, pattern) — a suppression is real only where
# that syntax introduces a comment in the changed file: "# noqa" inside
# JSON or markdown prose QUOTES the pattern, it cannot silence a linter
# there (F-33 corpus: PRs #25 and #37 documenting these sentinels
# tripped the unfiltered scan).
LINT_SUPPRESSION_PATTERNS = (
    ("hash", re.compile(r"#\s*noqa")),
    ("hash", re.compile(r"#\s*type:\s*ignore\b")),
    ("slash", re.compile(r"@\s*ts-(?:ignore|expect-error)\b")),
    ("slash", re.compile(r"//\s*eslint-disable")),
    ("slash", re.compile(r"/\*\s*eslint-disable")),
    ("html", re.compile(r"<!--\s*(?:eslint|ruff|biome)-disable")),
    ("hash", re.compile(r"#\s*(?:ruff|flake8)\s*:\s*(?:noqa|ignore)")),
    ("hash", re.compile(r"#\s*pylint:\s*(?:disable|skip-file)")),
)

# Comment syntaxes by filename suffix. Unlisted suffixes allow every
# syntax — a format the map doesn't know fails safe (flag; a human
# reviews), the same direction as the whole sentinel set.
_COMMENT_SYNTAX_BY_SUFFIX = {
    ".json": frozenset(),            # JSON has no comments at all
    ".md": frozenset({"html"}),      # '#' is a heading, not a comment
    ".markdown": frozenset({"html"}),
    ".html": frozenset({"html", "slash"}),
    ".htm": frozenset({"html", "slash"}),
    ".xml": frozenset({"html"}),
    ".svg": frozenset({"html"}),
}
_EVERY_SYNTAX = frozenset({"hash", "slash", "html"})


def _comment_syntaxes(path: str) -> frozenset:
    """The comment syntaxes ``path``'s language can carry."""
    name = path.rsplit("/", 1)[-1].lower()
    for suffix, syntaxes in _COMMENT_SYNTAX_BY_SUFFIX.items():
        if name.endswith(suffix):
            return syntaxes
    return _EVERY_SYNTAX

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
    consume the same set. A RENAME touches both its old and new path (a
    rename away from a risk path still touched it — the pre-F-25 event
    split's semantics). A copy's source path does NOT count: the diff
    changes the new file, and copying out of a risk path never raised the
    flag before F-25 (round-2 finding 3).
    """
    matched: set = set()
    for change in changes:
        paths = {change.path}
        if change.status == "R" and change.old_path:
            paths.add(change.old_path)
        for flag, globs in risk_globs.items():
            if any(matches(path, globs) for path in paths):
                matched.add(flag)
    return matched


def _credential_shape(text: str) -> str | None:
    """The first credential shape that matches ``text``, else None.

    The generic "secret assignment" shape is tested against the line
    with env-call spans blanked when one is present: the lookup itself
    is exempt, everything else on the line is not (see
    :data:`_ENV_CALL_SPAN`).
    """
    env_read = _ENV_CALL_SPAN.search(text)
    for name, pattern in _CREDENTIAL_SHAPES:
        if name == "secret assignment" and env_read:
            if pattern.search(_ENV_CALL_SPAN.sub(" ", text)):
                return name
            continue
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
        syntaxes = _comment_syntaxes(change.path)
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
            # Every echoed line goes through redact_credentials — the
            # no-echo rule is not lint-only: a skip reason can carry a
            # token, and the credential scanner cannot be the only guard
            # on what findings print (round-2 finding 4).
            echo = redact_credentials(text.strip())
            if test_file and any(
                p.search(_outside_strings(text)) for p in SKIP_MARKER_PATTERNS
            ):
                findings.append(
                    Finding(
                        "skip_marker_added",
                        f"skip/only marker added in {scan_path}:{no}: {echo}",
                        file=scan_path,
                        line=no,
                    )
                )
            if in_risk_path and any(
                s in syntaxes and p.search(text)
                for s, p in LINT_SUPPRESSION_PATTERNS
            ):
                findings.append(
                    Finding(
                        "lint_suppression_added",
                        f"lint suppression added in {change.path}:{no}: {echo}",
                        file=change.path,
                        line=no,
                    )
                )
        if test_file:
            _flag_weakened_assertions(change, scan_path, findings)
    findings.sort(key=lambda f: (f.id, f.file or "", f.line or 0))
    return findings


def _flag_weakened_assertions(change, scan_path: str, findings: list) -> None:
    """Append ``assertion_removed`` findings for a net-weakened test file.

    F-25's first cut fired on every removed line that mentioned
    "assert", so any real PR that moved or reformatted a test tripped it
    — 11 false positives across six merged PRs of this repo's own
    history (the F-33 corpus). "Without a replacement" is now per-file
    arithmetic: verbatim re-adds pair up (moves), and a file that ends
    the diff with as many assertion lines as it started is quiet —
    moves, edits, replacements alike (whether a replacement is STRONG
    is L2's judgement, not a line scan's). Only a net loss flags, and
    it names every removed line no re-add matches: over-reporting the
    excess is the safe side, under-reporting a weakened test is not.
    A REPLACEMENT must be a real assertion statement (:func:
    `_is_replacement_assertion`): a tautology (``assert True``) or a
    string mentioning "assert" levels nothing (PR #47 review finding
    2). The removed side stays deliberately broad — substring matches
    on code lines — so an honest consolidation that reduces the count
    still escalates to a human, exactly as F-25 intended.
    """
    removed = [
        (no, text) for no, text in change.removed_lines
        if _is_code_line(text) and any(p.search(text) for p in ASSERTION_PATTERNS)
    ]
    added = [
        text.strip() for _no, text in change.added_lines
        if _is_replacement_assertion(text)
    ]
    if not removed or len(added) >= len(removed):
        return
    n_removed, n_added = len(removed), len(added)
    for no, text in removed:
        stripped = text.strip()
        if stripped in added:
            added.remove(stripped)
            continue
        findings.append(
            Finding(
                "assertion_removed",
                f"assertion removed in {scan_path}:{no} "
                f"({n_removed} removed vs {n_added} added): "
                f"{redact_credentials(stripped)}",
                file=scan_path,
                line=no,
            )
        )


def builtin_sentinel_pass(profile: dict, changes, matched: set):
    """The pure half of the sentinel pass: scan, derive results, escalate.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        changes: ``FileChange`` list from ``diff_file_changes``.
        matched: The diff's matched risk flags —
            :func:`matched_risk_flags` computed ONCE by the caller.

    Returns:
        ``(findings, results, escalations)`` — the four built-in scans'
        findings, one result dict per built-in sentinel (pass/fail), and
        the ``credential_added`` escalation when a credential was found:
        nothing is pushed before a passing round, but a passing retry
        would push the branch whose local history still holds the
        credential commit — retrying can't make it safe.
    """
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    scans = builtin_sentinel_findings(changes, risk_globs)
    finding_ids = {f.id for f in scans}
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
            # Redact the WHOLE output, then cut: cutting first can land
            # inside a key, and the surviving fragment no longer matches
            # any credential shape (round-2 finding 1).
            output = redact_credentials(result.output).strip()[-200:]
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
