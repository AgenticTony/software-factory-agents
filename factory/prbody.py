"""The PR body (BUILD-ORDER F-10 AC-7, AC-8, AC-9, AC-14).

Shape adapted from mattpocock/skills ``pr``; the difference is that every
section that carries a judgement is built by the orchestrator from
validator facts, never from the agent's own account:

* **Summary** — task, branch, base/head commits, changed files.
* **Evidence** — per contract test ref: red on base (with L1's
  ``red_reason``) and green on head, read from the L1 report's command
  results.
* **Sentinels** — every sentinel L0 ran on the diff, pass or fail (F-25),
  as a table read from the L0 report's ``sentinel_results``.
* **Merge Danger** — **Door** (``one-way`` when the task carries any of
  :data:`ONE_WAY_FLAGS`, else ``two-way``) and **Blast Radius** (risk
  flags plus the ``paths.risk`` globs the diff matched, or ``contained``).
  The implementer's prose goes *below* these values, as inert fenced text.
* **Contract** — the approved acceptance items and their hash.
* **Validator report** — the L0/L1/L2 reports as JSON.
* **How to revert** — per Door: revert the squash-merge commit, or, for a
  one-way Door, a statement naming each flag a code revert can't undo.

Model text is only ever rendered inside a code fence longer than any
backtick run it contains, so no line of it can open a heading, close the
fence, or forge a Door. :func:`check_pr_body` is the gate the orchestrator
runs before pushing: a body missing a section (or carrying one twice) is
never opened.
"""

from __future__ import annotations

import json
import re

from factory.gates import contract_test_refs, matches

REQUIRED_SECTIONS = (
    "Summary",
    "Evidence",
    "Sentinels",
    "Merge Danger",
    "Contract",
    "Validator report",
    "How to revert",
)

# Data that leaked, money that moved, a schema that migrated, a deploy that
# shipped, a call an external service acted on: reverting the code recalls
# none of it.
ONE_WAY_FLAGS = ("migration", "deploy", "external_service", "payments", "privacy")


class PRBodyError(Exception):
    """The PR body is missing a required section; the PR must not open."""


def fence(text: str, info: str = "text") -> str:
    """Wrap untrusted text in a code fence it cannot break out of.

    The fence is longer than any backtick run in the text, so no line can
    close it (CommonMark: a closing fence must be at least as long), and
    every line is indented two spaces, so none starts with ``## `` for a
    line-oriented reader (:func:`check_pr_body`, GitHub's own outline).
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    inner = "\n".join("  " + line for line in text.rstrip().splitlines())
    return f"{ticks}{info}\n{inner}\n{ticks}"


def _flags(task: dict, extra_flags=()) -> list[str]:
    return sorted(set(task.get("risk") or []) | set(extra_flags or []))


def door(task: dict, extra_flags=()) -> str:
    """``one-way`` when any effective risk flag can't be undone by a revert."""
    return "one-way" if set(_flags(task, extra_flags)) & set(ONE_WAY_FLAGS) else "two-way"


def blast_radius(task: dict, profile: dict, changed_paths, extra_flags=()) -> str:
    """The risk flags plus every ``paths.risk`` glob the diff matched.

    Returns:
        ``contained`` when there is neither a flag nor a matched glob.
    """
    flags = _flags(task, extra_flags)
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    hits = []
    for flag in sorted(risk_globs):
        for glob in risk_globs[flag]:
            files = sorted(p for p in changed_paths if matches(p, [glob]))
            if files:
                hits.append(f"`{flag}`: `{glob}` matched " + ", ".join(files))
    if not flags and not hits:
        return "contained"
    parts = []
    if flags:
        parts.append("flags " + ", ".join(f"`{f}`" for f in flags))
    if hits:
        parts.append("paths " + "; ".join(hits))
    return " — ".join(parts)


def how_to_revert(task: dict, profile: dict, extra_flags=()) -> str:
    """The revert instructions for this PR's Door."""
    method = (profile.get("repo") or {}).get("merge_method") or "squash"
    if method == "squash":
        code = (
            "Revert the squash-merge commit: `git revert <squash-merge-sha>` on the "
            "default branch (or the PR's **Revert** button), then let CI run."
        )
    elif method == "merge":
        code = (
            "Revert the merge commit: `git revert -m 1 <merge-sha>` on the default "
            "branch (or the PR's **Revert** button)."
        )
    else:
        code = (
            "Revert each rebased commit of this PR, newest first, on the default branch "
            "(or the PR's **Revert** button)."
        )
    if door(task, extra_flags) == "two-way":
        return f"Door is `two-way`. {code}"
    one_way = [f for f in _flags(task, extra_flags) if f in ONE_WAY_FLAGS]
    lines = [
        f"Door is `one-way`. {code}",
        "",
        "Reverting the code **does not undo** what these risk flags cover — each needs "
        "its own recovery:",
    ]
    lines += [f"- `{flag}`" for flag in one_way]
    return "\n".join(lines)


def _result(report, name: str):
    return next((r for r in report.command_results if r.name == name), None)


def build_evidence(task: dict, l1_report) -> str:
    """The Evidence section: one row per contract test ref, from L1 alone."""
    refs = contract_test_refs(task)
    if not refs:
        return "The contract names no `test` checks: L1 had nothing to prove red-first."
    rows = ["| Contract test | Red on base | Green on head |", "|---|---|---|"]
    for ref in refs:
        red, green = _result(l1_report, f"red:{ref}"), _result(l1_report, f"green:{ref}")
        reason = (l1_report.red_reasons or {}).get(ref)
        if red is None:
            red_cell = "not run"
        elif red.timed_out:
            red_cell = "timed out"
        elif red.exit_code == 0:
            red_cell = "**NOT red on base** (exit 0)"
        else:
            red_cell = f"red on base (exit {red.exit_code}, red_reason: {reason or 'unknown'})"
        if green is None:
            green_cell = "not run"
        elif green.timed_out:
            green_cell = "timed out"
        elif green.exit_code == 0:
            green_cell = "green on head (exit 0)"
        else:
            green_cell = f"**NOT green on head** (exit {green.exit_code})"
        safe_ref = ref.replace("|", "\\|").replace("`", "'")
        rows.append(f"| `{safe_ref}` | {red_cell} | {green_cell} |")
    rows.append("")
    rows.append(f"L1 verdict: `{l1_report.verdict}` (built from the L1 report, not model text).")
    return "\n".join(rows)


def build_sentinel_section(reports: dict) -> str:
    """The Sentinels section: every sentinel L0 ran, pass or fail (F-25).

    Read from the L0 report's ``sentinel_results`` — orchestrator facts, not
    model text. A project sentinel's ``detail`` carries its command output,
    so it is flattened and pipe-escaped like the dropped-findings lines: one
    inert table cell, never a line of its own.
    """
    results = ((reports.get("L0") or {}).get("sentinel_results")) or []
    if not results:
        return "No sentinel results were recorded for this run."
    rows = [
        "| Sentinel | Kind | Risk flag | Verdict | Detail |",
        "|---|---|---|---|---|",
    ]
    for result in results:
        rows.append(
            f"| `{_cell(result.get('name'))}` | {result.get('kind', '—')} "
            f"| {_cell(result.get('risk_flag'))} | **{result.get('verdict', '—')}** "
            f"| {_cell(result.get('detail'))} |"
        )
    rows.append("")
    rows.append(
        "Every sentinel L0 ran on this diff, pass or fail (F-25); built from "
        "the L0 report, not model text."
    )
    return "\n".join(rows)


def _cell(value) -> str:
    """One inert table cell: flattened, pipes escaped, empty shown as ``—``."""
    return _flat(value).replace("|", "\\|") or "—"


def build_pr_body(
    task: dict,
    profile: dict,
    *,
    branch: str,
    base_sha: str,
    head_sha: str,
    changed_paths: list[str],
    reports: dict,
    l1_report,
    extra_flags=(),
    implementer_notes: str | None = None,
    attempt: int,
) -> str:
    """Assemble the full PR body from validator facts.

    Args:
        task: The task record (contract, risk flags).
        profile: The project profile (risk globs, merge method).
        branch/base_sha/head_sha: What the PR proposes.
        changed_paths: Files the diff touches.
        reports: ``{"L0": dict, "L1": dict, "L2": dict}`` report dicts.
        l1_report: The L1 report object (Evidence is built from it).
        extra_flags: Risk flags L0 found in the diff.
        implementer_notes: The implementer's own account — rendered fenced,
            below the computed values, never as evidence.
        attempt: The attempt number that passed.
    """
    budget = task.get("budget") or {}
    summary = [
        f"Task **{task['id']}** — {task.get('title', '')}".rstrip(),
        "",
        f"- Branch `{branch}` → `{(profile.get('repo') or {}).get('default_branch', 'main')}`",
        f"- Base `{base_sha[:12]}`, head `{head_sha[:12]}`",
        f"- Attempt {attempt} of {budget.get('max_attempts', '?')}; L0, L1 and L2 passed",
        f"- Source: `{task.get('source', '')}`",
        "",
        "Changed files:",
        "",
        fence("\n".join(changed_paths) or "(none)"),
    ]
    merge_danger = [
        f"**Door:** `{door(task, extra_flags)}`",
        "",
        f"**Blast Radius:** {blast_radius(task, profile, changed_paths, extra_flags)}",
        "",
        "Door and Blast Radius are computed by the orchestrator from the risk flags "
        "and the diff; nothing below can change them.",
        "",
        "### Implementer notes (unverified)",
        "",
        fence(implementer_notes or "(none)"),
    ]
    contract = {
        "acceptance": task.get("acceptance", []),
        "acceptance_hash": task.get("acceptance_hash"),
        "out_of_scope": task.get("out_of_scope", []),
    }
    sections = {
        "Summary": "\n".join(summary),
        "Evidence": build_evidence(task, l1_report),
        "Sentinels": build_sentinel_section(reports),
        "Merge Danger": "\n".join(merge_danger),
        "Contract": fence(json.dumps(contract, indent=2, ensure_ascii=False), "json"),
        "Validator report": _validator_section(reports),
        "How to revert": how_to_revert(task, profile, extra_flags),
    }
    body = "\n\n".join(f"## {name}\n\n{sections[name]}" for name in REQUIRED_SECTIONS)
    return body + "\n\n---\nOpened by the software factory. A human reviews and merges.\n"


def _validator_section(reports: dict) -> str:
    """The gate reports as JSON, with the L2 round's dropped findings —
    if any — pulled out above the blob (F-23 finding 9: a reviewer
    finding the orchestrator dropped as out of scope still belongs in
    front of the human merging the PR, not buried in the JSON).

    F-38 draws the opposite line for the verifier's drops: a finding the
    verifier voted out (or the nit budget capped) NEVER reaches the PR
    body — removing that noise is the verifier's job — so the
    ``verifier_dropped`` ledger is stripped from the rendered JSON here
    while the stored validator report and the attempt record keep every
    entry with its reason. The input dict is not mutated: the body renders
    a projection.

    The dropped list is reviewer-controlled text, so it rides inside a
    ``text`` fence — the same treatment the implementer's notes get. The
    fence is what makes the payload inert: it is one backtick longer than
    any run in the text and indents every line, so nothing inside can
    close it or start a ``## `` heading (a ``<!--`` could otherwise hide
    everything after it in GitHub's rendered view while
    :func:`check_pr_body`'s raw regex still passed, and links would plant
    in front of the merger). Flattening keeps each finding on one line of
    the list — readability, not containment; the fence alone would hold a
    multiline payload. Inside the JSON fence the same text was always
    safe (newlines escaped there)."""
    body_reports = dict(reports)
    l2 = reports.get("L2")
    if isinstance(l2, dict) and "verifier_dropped" in l2:
        body_reports["L2"] = {
            key: value for key, value in l2.items() if key != "verifier_dropped"
        }
    section = fence(json.dumps(body_reports, indent=2, ensure_ascii=False), "json")
    dropped = (reports.get("L2") or {}).get("dropped") or []
    if dropped:
        lines = [
            f"- [{_flat(d.get('finding', {}).get('severity'))}] "
            f"{_flat(d.get('finding', {}).get('file')) or '(no file)'}: "
            f"{_flat(d.get('finding', {}).get('text'))}"
            for d in dropped
        ]
        section = (
            "**Reviewer findings dropped as out of scope** (no "
            "`missed_earlier` justification; not counted in the verdict — "
            "read them before merging):\n\n"
            + fence("\n".join(lines), "text")
            + "\n\n" + section
        )
    return (
        section
        + "\n\nL3 holdouts: not run (F-15). L4 live verification runs after "
        "deploy (F-14)."
    )


def _flat(value) -> str:
    """Reviewer-controlled text as one line: all whitespace runs (newlines
    included) collapse to single spaces, so each dropped finding renders
    as a single readable list line inside the fence that holds it."""
    return " ".join(str(value or "").split())


def check_pr_body(body: str) -> None:
    """Refuse a body unless every required section appears exactly once.

    Raises:
        PRBodyError: A section is missing or duplicated, Merge Danger
            lacks its Door / Blast Radius values, or the body is over
            GitHub's size limit (gh would refuse it after the push).
    """
    if len(body) > 65_536:
        raise PRBodyError(f"PR body is {len(body)} chars; GitHub's limit is 65,536")
    headings = re.findall(r"^## (.+?)\s*$", body, re.M)
    for name in REQUIRED_SECTIONS:
        count = headings.count(name)
        if count != 1:
            raise PRBodyError(
                f"PR body must carry section '## {name}' exactly once (found {count})"
            )
    match = re.search(r"^## Merge Danger\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    danger = match.group(1)
    for value in ("**Door:**", "**Blast Radius:**"):
        if value not in danger:
            raise PRBodyError(f"Merge Danger is missing {value}")
