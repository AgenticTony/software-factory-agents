"""L2 adversarial review — verdict parsing and enforcement (F-09, F-23).

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

F-23 adds review convergence across rounds. Every finding gets a **stable
key** — sha256 of its file, axis and normalised summary — at parse time, so
a finding raised in one round is recognisable in the next.
:func:`apply_convergence` then classifies a round's verdict against the
keys raised before it: a repeat whose subject is provably untouched since
it was raised — a real delta shows the named file unchanged, and a repeat
from an earlier run has no continuity at all — is **carried**: listed as
carried, never as new, because re-raising it adds no information. Any
other in-run repeat (the file was touched, or the finding names no file)
is an **attempt in progress** and stands as the round's own finding. In a
delta round a *new* finding on a file the delta didn't change is
**dropped** unless it says why the earlier round missed it; a finding that
names no file is never dropped — it cannot be shown to sit in unchanged
code, and dropping it could flip a declared pass carrying a real
contract-level blocker into an opened PR. The function also computes the
round's **no-progress** verdict (``no_progress`` +
``no_progress_reason``): a FAIL the next round cannot act on — every
finding a carried repeat from this run on untouched code, or everything
dropped — so the orchestrator escalates instead of spending the round.
The effective verdict is recomputed over what survives, which is how a
declared pass whose only out-of-scope blocker was dropped becomes the
pass the reviewer actually declared.

Running the reviewer session itself is F-10's wiring; everything here is
pure parsing and judgement over the text a session produced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
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
    :class:`~factory.gates.Finding` plus the L2-only fields: ``axis``
    (``spec`` | ``standards``), ``severity`` (one of the four values
    the seat prompt documents; only the exact value ``blocking`` carries
    verdict weight), the F-23 convergence fields — ``key`` (the stable
    finding identity, set at parse time), ``missed_earlier`` (the
    reviewer's justification for a new finding outside the delta) and
    ``state`` (``new`` or ``carried``, set by :func:`apply_convergence`)
    — and the shared location fields."""

    id: str
    text: str
    axis: str | None = None
    severity: str | None = None
    file: str | None = None
    line: int | None = None
    key: str | None = None
    missed_earlier: str | None = None
    state: str = "new"


def _normalise_summary(text: str) -> str:
    """Collapse whitespace runs and case: the same finding re-raised with
    different spacing or casing must key identically across rounds."""
    return " ".join(text.split()).casefold()


def finding_key(finding: L2Finding) -> str:
    """The stable identity of a finding: sha256 of file, axis (the rule
    category) and normalised summary (F-23 AC-1). Deliberately excludes
    ``line`` (line numbers drift as the diff moves) and ``severity`` (the
    same defect re-rated a round later is the same finding).

    A fileless finding keys the same way — by its wording. An earlier
    revision keyed fileless findings by any ``AC-n`` ids their text
    mentions; the owner review reverted it: different defects on one
    acceptance item merged into one thread and stopped the run after a
    single fix attempt, while an unanchored contract blocker got dropped
    and flipped a declared pass into a PR. If contract anchoring is ever
    wanted, it belongs in the verdict schema as an explicit ``ac`` field
    validated at parse time — not inferred from prose."""
    material = "\x00".join(
        (finding.file or "", finding.axis or "", _normalise_summary(finding.text))
    )
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class L2Report(GateReport):
    """The L2 verdict plus everything F-10 consumes downstream.

    ``parse_error`` is set iff the reviewer's output was not valid verdict
    JSON; the verdict is then ``fail`` with a single
    ``invalid_verdict_json`` finding, per the AC-1 rule. ``declared`` is
    what the reviewer actually said (kept so convergence can recompute the
    effective verdict after dropping out-of-scope findings). ``dropped``
    lists the findings :func:`apply_convergence` removed as out of delta,
    with the reason — a dropped finding never reaches the verdict, but the
    human reading the report still sees it. ``no_progress`` is the F-23
    stop verdict, computed by :func:`apply_convergence` where the prior
    set is in hand: True means this round's FAIL gives the next round
    nothing it can fix. ``no_progress_reason`` says which case fired
    (``carried_repeats`` or ``all_dropped``) — the orchestrator renders
    the escalation note from it instead of re-deriving the branch.
    """

    layer: ClassVar[str] = "L2"
    parse_error: str | None = None
    declared: str | None = None
    dropped: list = field(default_factory=list)
    no_progress: bool = False
    no_progress_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "verdict": self.verdict,
            "findings": self.findings_as_dicts(),
            "commands": self.commands_as_dicts(),
            "parse_error": self.parse_error,
            "declared": self.declared,
            "dropped": self.dropped,
            "no_progress": self.no_progress,
            "no_progress_reason": self.no_progress_reason,
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
        missed = item.get("missed_earlier")
        if missed is not None and (not isinstance(missed, str) or not missed.strip()):
            raise _InvalidVerdict(
                f"finding {index} missed_earlier must be a non-empty string "
                "explaining why the earlier round missed it"
            )
        finding = L2Finding(
            id=str(item.get("id") or f"L2-{index + 1}"),
            text=text,
            axis=axis,
            severity=severity,
            file=item.get("file"),
            line=line,
            missed_earlier=missed,
        )
        finding.key = finding_key(finding)
        findings.append(finding)
        if severity.strip().lower() == "blocking":
            blocking = True

    declared = declared.strip().lower()
    verdict = "fail" if declared == "fail" or blocking else "pass"
    return L2Report(verdict=verdict, findings=findings, declared=declared)


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


def apply_convergence(
    report: L2Report,
    prior: dict[str, str | None],
    delta_files: set[str] | frozenset[str],
    *,
    delta_mode: bool,
) -> L2Report:
    """Classify one round's verdict against the keys raised before it (F-23).

    The orchestrator calls this right after :func:`parse_verdict`, with the
    keys of every earlier round's findings (``prior``: key -> the reviewed
    sha that raised it, ``None`` when it was raised in an earlier run and
    no sha survives), the files the delta changed since the last review,
    and whether this round runs in delta mode (a review happened before it
    in this run).

    The classification, per finding:

    * **Prior key from an earlier run → carried.** No diff continuity ever
      existed for it, so a re-raise is always the same finding. It stays
      in the findings — a carried *blocking* finding still fails the
      round, because it is an unresolved problem — but is listed
      ``state: "carried"``, never as new.
    * **In-run prior key, provably untouched → carried.** Only a real
      delta can prove "untouched since", and only a named file can be
      shown untouched: the finding's file is one the delta didn't change.
      Without a delta (the round fell back to the full diff after the
      implementer rewrote the reviewed history), an in-run repeat is an
      attempt in progress — the rewrite touched the history those
      findings live in — and stands as this round's own finding
      (``state: "new"``); it must not trigger the stop.
    * **In-run prior key on a file the delta touched, or with no file at
      all → a repeat in progress.** The implementer changed the code the
      finding concerns — or, with no file, no delta can ever show the
      finding's subject untouched, so an honest re-raise after a fix
      attempt must not be mistaken for a bounce. Same rule:
      ``state: "new"``, no stop. A genuinely stuck fileless thread runs
      out at ``attempts_exhausted``; that is the accepted cost of never
      dropping contract-level findings.
    * **New finding on a file outside the delta → needs a why.** In delta
      mode, a finding whose key was never raised and whose file the delta
      didn't change must carry ``missed_earlier`` explaining what the
      earlier round missed; without it, it is dropped as
      ``out_of_delta_without_missed_earlier`` and recorded in ``dropped``.
      A finding that names **no file** is never dropped: it cannot be
      shown to sit in unchanged code, and the contract-level findings
      (a missing test, an unmet acceptance item) rarely name one — a
      declared pass whose only blocker is a fileless finding must not be
      flipped into an opened PR. Rounds without a delta (the first review
      of a run, or the rewrite fallback) drop nothing: the full diff is
      the scope.
    * The effective verdict is recomputed over what survives: a declared
      ``fail`` stands, and so does a parse error (an invalid verdict is a
      FAIL before and after convergence); a declared ``pass`` whose only
      blocking finding was dropped becomes the pass the reviewer declared.

    **No progress** (``no_progress`` on the returned report) means a FAIL
    the next round cannot act on, either because every finding is a
    carried repeat from **this run** on untouched code (the bounce the
    stop exists for — a carry seeded from an earlier run doesn't count:
    this run's implementer has only just attempted it), or because every
    finding was dropped and the declared fail stands with nothing left to
    fix.

    Args:
        report: The freshly parsed verdict of this round.
        prior: Keys raised in earlier rounds, -> their raise sha or None.
        delta_files: Paths changed since the last reviewed sha.
        delta_mode: True when an earlier review in this run exists.

    Returns:
        The classified :class:`L2Report` — a new report with new finding
        objects; the input is left untouched, so one parsed verdict can be
        classified against different deltas (tests do exactly that).
    """
    active: list = []
    dropped: list = []
    for finding in report.findings:
        if not isinstance(finding, L2Finding):
            # The invalid-verdict finding: nothing to key or classify.
            active.append(finding)
            continue
        if finding.key in prior:
            if prior[finding.key] is None:
                # Raised in an earlier run: no diff continuity ever existed,
                # so it is always the same finding being re-raised.
                finding = replace(finding, state="carried")
            elif (
                delta_mode
                and finding.file is not None
                and finding.file not in delta_files
            ):
                # Raised in this run and provably untouched since: only a
                # real delta over a named file can prove that. Without a
                # delta (the history-rewrite fallback), or without a file
                # (a contract-level finding), the repeat is an attempt in
                # progress, not a carry.
                finding = replace(finding, state="carried")
            active.append(finding)
        elif (
            delta_mode
            and finding.file is not None
            and finding.file not in delta_files
            and not (finding.missed_earlier or "").strip()
        ):
            # A named file the delta didn't touch, without a why. A
            # fileless finding is never dropped: it cannot be shown to
            # sit in unchanged code (round-1 finding 7).
            dropped.append(
                {
                    "finding": dict(finding.__dict__),
                    "reason": "out_of_delta_without_missed_earlier",
                }
            )
        else:
            active.append(finding)
    blocking = any(
        isinstance(f, L2Finding) and (f.severity or "").strip().lower() == "blocking"
        for f in active
    )
    verdict = (
        "fail"
        if report.parse_error is not None or report.declared == "fail" or blocking
        else "pass"
    )
    l2_findings = [f for f in active if isinstance(f, L2Finding)]
    only_this_runs_carries = (
        l2_findings
        and len(l2_findings) == len(active)
        and all(
            f.state == "carried" and prior.get(f.key) is not None
            for f in l2_findings
        )
    )
    everything_dropped = not active and bool(dropped)
    no_progress = verdict == "fail" and (
        only_this_runs_carries or everything_dropped
    )
    reason = None
    if no_progress:
        reason = "all_dropped" if everything_dropped else "carried_repeats"
    return L2Report(
        verdict=verdict,
        findings=active,
        declared=report.declared,
        dropped=dropped,
        parse_error=report.parse_error,
        no_progress=no_progress,
        no_progress_reason=reason,
    )
