"""L2 adversarial review — verdict parsing and enforcement (F-09).

The only model-judged layer (VALIDATOR-SPEC L2): a fresh reviewer session
reads the approved contract, the diff and the L0/L1 output — never the
implementer's narrative — and returns one JSON verdict object. This module
owns the orchestrator side of that contract: extracting the JSON from the
reviewer's output, validating its shape, and computing the effective
verdict.

The rules, enforced here rather than trusted from the model:

* Output that isn't valid verdict JSON is **treated as FAIL** — a reviewer
  that cannot speak the verdict protocol has not reviewed anything. That
  includes a session that produced no text at all (``result=None`` from a
  failed or timed-out reviewer): the orchestrator must stop the change,
  not crash on it.
* Every finding carries ``axis`` — exactly ``spec`` or ``standards`` — and
  ``severity`` — exactly ``blocking``, ``major``, ``minor`` or ``note``
  (the four values the seat prompt documents). A finding without either
  makes the whole output invalid (the AC-1 rule): the two axes are the
  review's structure, and an unlisted severity is the model inventing a
  scale the factory doesn't act on.
* A ``blocking`` finding on **either** axis means FAIL, whatever the other
  axis says and whatever the declared verdict says — neither axis is
  allowed to mask the other.
* A declared ``fail`` stands even without blocking findings; a declared
  ``pass`` with only non-blocking findings passes.
* Duplicate JSON keys are invalid: ``json.loads`` silently keeps the last
  duplicate, so ``{"verdict": "fail", ..., "verdict": "pass"}`` would flip
  the verdict. A dup anywhere in the object makes that candidate
  malformed.

Running the reviewer session itself is F-10's wiring; everything here is
pure parsing and judgement over the text a session produced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from factory.gates import Finding, GateReport

VALID_AXES = ("spec", "standards")
VALID_SEVERITIES = ("blocking", "major", "minor", "note")

# A fenced block (with or without the json tag) around the verdict object —
# models habitually wrap their JSON; the fence is extracted before parsing.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class L2Finding:
    """One reviewer finding. Same shape as the deterministic layers'
    :class:`~factory.gates.Finding` plus the two L2-only fields: ``axis``
    (``spec`` | ``standards``) and ``severity`` (one of the four values
    the seat prompt documents; only the exact value ``blocking`` carries
    verdict weight)."""

    id: str
    text: str
    axis: str | None = None
    severity: str | None = None
    file: str | None = None
    line: int | None = None


@dataclass
class L2Report(GateReport):
    """The L2 verdict plus everything F-10 consumes downstream.

    ``parse_error`` is set iff the reviewer's output was not valid verdict
    JSON; the verdict is then ``fail`` with a single
    ``invalid_verdict_json`` finding, per the AC-1 rule.
    """

    layer: ClassVar[str] = "L2"
    parse_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "verdict": self.verdict,
            "findings": self.findings_as_dicts(),
            "commands": self.commands_as_dicts(),
            "parse_error": self.parse_error,
        }


class _InvalidVerdict(Exception):
    """Internal: the output is not valid verdict JSON (reason in the str)."""


class _DuplicateKey(ValueError):
    """Internal: a JSON object repeated a key (json.loads would have kept
    the last one silently)."""


def _no_duplicate_keys(pairs: list) -> dict:
    """``object_pairs_hook`` rejecting repeated keys at any depth."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate key {key!r}")
        result[key] = value
    return result


def _balanced_objects(text: str) -> list:
    """The top-level ``{...}`` spans in ``text``, outermost-first per
    position, braces inside JSON strings respected. A verdict object with
    nested finding dicts yields ONE span (the outer object)."""
    spans = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                spans.append(text[start : index + 1])
    return spans


def _extract_json(output: Any) -> Any:
    """Find the verdict JSON in the reviewer's output.

    Candidates, tried in order: the whole text (a pure-JSON reply), then
    every fenced block and balanced object **from last to first** — a
    model that quotes an example verdict earlier and states the real one
    at the end must have the real one win. A candidate with repeated keys
    is malformed and skipped like any parse failure.

    Args:
        output: The reviewer session's result text.

    Returns:
        The parsed JSON value.

    Raises:
        _InvalidVerdict: If no candidate parses as JSON (the last error is
            carried as the reason).
    """
    text = output.strip()
    candidates: list[str] = [text]
    positioned: list[tuple[int, str]] = []
    for match in _FENCE.finditer(text):
        positioned.append((match.start(1), match.group(1)))
    for span in _balanced_objects(text):
        positioned.append((text.find(span), span))
    candidates.extend(text for _, text in sorted(positioned, reverse=True))

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate, object_pairs_hook=_no_duplicate_keys)
        except (json.JSONDecodeError, _DuplicateKey, ValueError) as exc:
            last_error = exc
    reason = (
        f"no valid verdict JSON in the reviewer output ({last_error})"
        if last_error is not None
        else "empty reviewer output"
    )
    raise _InvalidVerdict(reason)


def _invalid(reason: str) -> L2Report:
    """The AC-1 outcome: unparsable/invalid verdict JSON is a FAIL."""
    return L2Report(
        verdict="fail",
        findings=[Finding("invalid_verdict_json", reason)],
        parse_error=reason,
    )


def _validated_report(data: Any) -> L2Report:
    """Validate the parsed verdict object and compute the effective verdict.

    Raises:
        _InvalidVerdict: On any shape violation — the caller turns it into
            the AC-1 FAIL.
    """
    if not isinstance(data, dict):
        raise _InvalidVerdict(f"verdict JSON is not an object: {type(data).__name__}")
    declared = data.get("verdict")
    if not isinstance(declared, str) or declared.strip().lower() not in ("pass", "fail"):
        raise _InvalidVerdict(f'verdict must be "pass" or "fail", got {declared!r}')
    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        raise _InvalidVerdict("findings must be a list")

    findings: list[L2Finding] = []
    blocking = False
    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            raise _InvalidVerdict(f"finding {index} is not an object")
        axis = item.get("axis")
        if axis not in VALID_AXES:
            raise _InvalidVerdict(
                f"finding {index} carries no valid axis ('spec' or "
                f"'standards'): {axis!r}"
            )
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _InvalidVerdict(f"finding {index} carries no text")
        severity = item.get("severity")
        if (
            not isinstance(severity, str)
            or severity.strip().lower() not in VALID_SEVERITIES
        ):
            raise _InvalidVerdict(
                f"finding {index} severity must be one of {VALID_SEVERITIES}, "
                f"got {severity!r}"
            )
        line = item.get("line")
        if line is not None and not isinstance(line, int):
            raise _InvalidVerdict(f"finding {index} has a non-integer line")
        findings.append(
            L2Finding(
                id=str(item.get("id") or f"L2-{index + 1}"),
                text=text,
                axis=axis,
                severity=severity,
                file=item.get("file"),
                line=line,
            )
        )
        if severity.strip().lower() == "blocking":
            blocking = True

    verdict = "fail" if declared.strip().lower() == "fail" or blocking else "pass"
    return L2Report(verdict=verdict, findings=findings)


def parse_verdict(output: Any) -> L2Report:
    """Parse and enforce the reviewer's verdict output.

    Args:
        output: The reviewer session's result — a string, or ``None`` /
            non-text when the session failed, aborted or timed out before
            producing one.

    Returns:
        The :class:`L2Report`. Verdict ``fail`` whenever the output is not
        text, is not valid verdict JSON, the declared verdict is ``fail``,
        or any finding is ``blocking`` on either axis.

    Raises:
        Nothing — invalid output is a returned FAIL, never an exception:
        a bad reviewer must stop the line, not the orchestrator.
    """
    if not isinstance(output, str):
        return _invalid(
            f"the reviewer produced no verdict text "
            f"({type(output).__name__}): a failed or aborted session is a "
            "FAIL, not a crash"
        )
    try:
        return _validated_report(_extract_json(output))
    except _InvalidVerdict as exc:
        return _invalid(str(exc))
