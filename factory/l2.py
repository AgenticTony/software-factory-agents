"""L2 adversarial review — verdict parsing and enforcement (F-09).

The only model-judged layer (VALIDATOR-SPEC L2): a fresh reviewer session
reads the approved contract, the diff and the L0/L1 output — never the
implementer's narrative — and returns one JSON verdict object. This module
owns the orchestrator side of that contract: extracting the JSON from the
reviewer's output, validating its shape, and computing the effective
verdict.

The rules, enforced here rather than trusted from the model:

* Output that isn't valid verdict JSON is **treated as FAIL** — a reviewer
  that cannot speak the verdict protocol has not reviewed anything.
* Every finding carries ``axis`` — exactly ``spec`` or ``standards``. A
  finding without a valid axis makes the whole output invalid (the AC-1
  rule): the two axes are the review's structure, so a finding that doesn't
  know its axis can't be acted on.
* A ``blocking`` finding on **either** axis means FAIL, whatever the other
  axis says and whatever the declared verdict says — neither axis is
  allowed to mask the other.
* A declared ``fail`` stands even without blocking findings; a declared
  ``pass`` with only non-blocking findings passes.

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

# A fenced block (with or without the json tag) around the verdict object —
# models habitually wrap their JSON; the fence is extracted before parsing.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class L2Finding:
    """One reviewer finding. Same shape as the deterministic layers'
    :class:`~factory.gates.Finding` plus the two L2-only fields: ``axis``
    (``spec`` | ``standards``) and free-form ``severity`` (only the exact
    value ``blocking`` carries verdict weight)."""

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


def _extract_json(output: str) -> Any:
    """Find the verdict JSON in the reviewer's output.

    Tries the whole text, then each fenced block, then the outermost brace
    span — in that order, first parse wins.

    Args:
        output: The reviewer session's result text.

    Returns:
        The parsed JSON value.

    Raises:
        _InvalidVerdict: If no candidate parses as JSON.
    """
    text = output.strip()
    candidates = [text, *_FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    raise _InvalidVerdict("no JSON object found in the reviewer output")


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
        if severity is not None and not isinstance(severity, str):
            raise _InvalidVerdict(f"finding {index} has a non-string severity")
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
        if (severity or "").strip().lower() == "blocking":
            blocking = True

    verdict = "fail" if declared.strip().lower() == "fail" or blocking else "pass"
    return L2Report(verdict=verdict, findings=findings)


def parse_verdict(output: str) -> L2Report:
    """Parse and enforce the reviewer's verdict output.

    Args:
        output: The reviewer session's result text (raw, possibly with
            prose or fences around the JSON object).

    Returns:
        The :class:`L2Report`. Verdict ``fail`` whenever the output is not
        valid verdict JSON, the declared verdict is ``fail``, or any
        finding is ``blocking`` on either axis.

    Raises:
        Nothing — invalid output is a returned FAIL, never an exception:
        a bad reviewer must stop the line, not the orchestrator.
    """
    try:
        return _validated_report(_extract_json(output))
    except _InvalidVerdict as exc:
        return _invalid(str(exc))
