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
* Every finding carries ``axis`` — ``spec`` or ``standards`` — and
  ``severity`` — one of ``blocking``, ``major``, ``minor`` or ``note``
  (the four values the seat prompt documents). Both are matched the way
  severity always was — trimmed, case-insensitive — and the canonical
  value stored, so "Spec" is the review's structure, not a protocol
  fault. Anything else after normalising still makes the whole output
  invalid (the AC-1 rule): the two axes are the review's structure, and
  an unlisted severity is the model inventing a scale the factory
  doesn't act on. A ``line`` is an int or a quoted int ("42" is the same
  place as 42); a bool, a negative, a float or a range string is not a
  place.
* A ``blocking`` finding on **either** axis means FAIL, whatever the other
  axis says and whatever the declared verdict says — neither axis is
  allowed to mask the other. F-41 AC-6 extends the same weight to
  ``major``: a surviving major fails a round exactly as a blocking one
  does, and a declared pass overrides neither — F-31's run (2026-10-30)
  published with a confirmed major defect because only ``blocking``
  failed a round.
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

F-38 adds the parallel reviewer fan-out and the verifier vote. N reviewer
verdicts merge into ONE union (:func:`merge_reviews`): findings two
reviewers both report become one entry carrying which reviewers reported
it, and one invalid verdict fails the whole review exactly as a single
invalid one did — after :func:`parse_reviews_with_retry` has given that
seat ONE re-run with the same prompt (PR #90 round 2: a formatting slip
by one reviewer no longer voids every reviewer's findings; a
still-invalid retry keeps the fail-closed union FAIL). Duplicates merge
on the F-23 stable key — the same
defect in the same wording — and, since F-41, on **location** too: the
same file with lines within :data:`_MERGE_LINE_WINDOW` of each other,
when both findings carry a file and a line and their wording overlaps
(:func:`_shares_wording`). Text alone keyed apart two
reviewers describing one bug in different words (F-16's run: every heavy
finding stayed ``reported_by=[1]``, so the unanimous rule almost never
fired); location is the defect's identity when the wording isn't. A
verifier session then votes each union finding in or out
(:func:`parse_verifier_vote`): a drop needs an explicit vote with a
reason; everything the verifier failed to vote on, or voted on in a shape
it wasn't asked for, is KEPT — an unverifiable vote may not remove a
finding. :func:`apply_vote` tags and ranks the survivors, forces a
``blocking`` severity to the ``important`` tag, and mechanically enforces
REVIEW.md's nit budget (at most three ``minor``/``note`` findings; the
rest drop with reason ``nit_budget``). Verifier and nit-budget drops land
in ``verifier_dropped`` — the attempt record and the stored report, never
the PR body — while ``dropped`` stays the F-23 out-of-delta list the PR
body renders. The declared verdict is fail-closed across the fan-out:
any reviewer's declared ``fail`` stands.

F-41 adds the final full review after fix rounds: when a fix round's
delta review passes, one more full-diff review runs before publish.
:func:`final_review_verdict` is its failure rule — only a surviving
``blocking`` or ``major`` finding fails it; a declared fail with nothing
heavier does not (the minors reach the PR body within the nit budget),
while an unparsable verdict still does.

Running the reviewer sessions themselves is F-10's wiring; everything here is
pure parsing and judgement over the text sessions produced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, ClassVar, NamedTuple, Sequence

from factory.gates import Finding, GateReport

VALID_AXES = ("spec", "standards")
VALID_SEVERITIES = ("blocking", "major", "minor", "note")

# F-38: the verifier's survivor tags (IMPORTANT reads worst-first) and the
# REVIEW.md nit budget — at most three minor/note findings reach the human.
VALID_TAGS = ("important", "nit", "pre-existing")
NIT_BUDGET = 3

_SEVERITY_RANK = {name: rank for rank, name in enumerate(VALID_SEVERITIES)}
_TAG_RANK = {name: rank for rank, name in enumerate(VALID_TAGS)}

# The severities that fail a round (F-41 AC-6): a surviving major weighs
# exactly as much as a blocking finding — a declared pass overrides
# neither. Kept as a helper because three functions ask the same
# question (apply_convergence, apply_vote, final_review_verdict) and a
# fourth (the orchestrator) renders its notes from the same set.
_ROUND_FAILING_SEVERITIES = ("blocking", "major")


def _fails_round(finding) -> bool:
    """Whether one finding's severity fails a round (blocking or major)."""
    return (
        isinstance(finding, L2Finding)
        and (finding.severity or "").strip().lower() in _ROUND_FAILING_SEVERITIES
    )

# A fenced block (with or without the json tag) around the verdict object —
# models habitually wrap their JSON; the fence is extracted before parsing.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class L2Finding:
    """One reviewer finding. Same shape as the deterministic layers'
    :class:`~factory.gates.Finding` plus the L2-only fields: ``axis``
    (``spec`` | ``standards``), ``severity`` (one of the four values
    the seat prompt documents; ``blocking`` and ``major`` both carry
    verdict weight — F-41 AC-6), the F-23 convergence fields — ``key``
    (the stable
    finding identity, set at parse time), ``missed_earlier`` (the
    reviewer's justification for a new finding outside the delta) and
    ``state`` (``new`` or ``carried``, set by :func:`apply_convergence`)
    — the F-38 fan-out fields — ``reported_by`` (1-based reviewer numbers
    that reported it, set by :func:`merge_reviews`), ``verifier_reason``
    and ``tag`` (set by :func:`parse_verifier_vote` /
    :func:`apply_vote`) — and the shared location fields."""

    id: str
    text: str
    axis: str | None = None
    severity: str | None = None
    file: str | None = None
    line: int | None = None
    key: str | None = None
    missed_earlier: str | None = None
    state: str = "new"
    reported_by: list = field(default_factory=list)
    verifier_reason: str | None = None
    tag: str | None = None


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

    F-38 adds the verifier's ledger, kept deliberately separate from
    ``dropped``: ``verifier_dropped`` holds the findings the verifier voted
    out and the ones the nit budget capped, each with its reason — they
    land in the attempt record and the stored report, but NEVER in the PR
    body (AC-4), which is why they don't ride ``dropped``.
    ``verifier_error`` is set when the verifier's output was not valid
    vote JSON: the vote then dropped nothing (every finding survived), and
    the error names why — a broken verifier degrades to today's
    no-filter review, never to a review that silently lost findings.
    """

    layer: ClassVar[str] = "L2"
    parse_error: str | None = None
    declared: str | None = None
    dropped: list = field(default_factory=list)
    no_progress: bool = False
    no_progress_reason: str | None = None
    verifier_dropped: list = field(default_factory=list)
    verifier_error: str | None = None

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
            "verifier_dropped": self.verifier_dropped,
            "verifier_error": self.verifier_error,
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
        if (
            not isinstance(axis, str)
            or axis.strip().lower() not in VALID_AXES
        ):
            raise _InvalidVerdict(
                f"finding {index} carries no valid axis ('spec' or "
                f"'standards'): {axis!r}"
            )
        # The same tolerance severity has always had, and the canonical
        # value is stored: "Spec" is the review's structure, not a
        # protocol fault (PR #90 round 2 — an exact match voided whole
        # reviews over casing).
        axis = axis.strip().lower()
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
        if isinstance(line, bool):
            # bool IS int in Python; a true/false line is a slip, and
            # True would reach the ±5-line location merge as line 1.
            raise _InvalidVerdict(f"finding {index} has a non-integer line")
        if isinstance(line, int):
            if line < 0:
                raise _InvalidVerdict(f"finding {index} has a non-integer line")
        elif isinstance(line, str) and line.strip().isdigit():
            # A quoted line number names the same place as the int
            # (PR #90 round 2 — "42" used to void the whole verdict).
            line = int(line.strip())
        elif line is not None:
            raise _InvalidVerdict(f"finding {index} has a non-integer line")
        missed = item.get("missed_earlier")
        if missed is not None and not isinstance(missed, str):
            raise _InvalidVerdict(
                f"finding {index} missed_earlier must be a string "
                "explaining why the earlier round missed it"
            )
        if isinstance(missed, str) and not missed.strip():
            # The reviewer prompt shows the key in its example JSON, so a
            # first-round review echoes it back blank. A blank is an absent
            # reason, not a malformed verdict — voiding the whole review
            # over it failed clean diffs in the 2026-11-04 seeded eval.
            # Delta mode is unaffected: apply_convergence treats None and
            # blank alike (out_of_delta_without_missed_earlier).
            missed = None
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


def parse_reviews_with_retry(
    sessions: Sequence[Any],
    rerun: Callable[[], Any] | None = None,
) -> tuple[list[L2Report], int]:
    """Parse the fan-out's reviewer seats, retrying a seat that could not
    speak the verdict protocol exactly once (PR #90 round 2).

    One reviewer's formatting slip — garbage instead of JSON, or a
    session that died before producing text — used to void every
    reviewer's findings. That seat now gets ONE re-run via ``rerun`` (the
    caller's closure re-running the same seat with the same prompt
    through the same fan-out machinery); if the retry parses, it IS the
    seat's verdict. Fail-closed throughout: a still-invalid retry leaves
    the seat's report invalid and :func:`merge_reviews` then fails the
    union exactly as before, no seat is ever dropped, a fan-out never
    passes with fewer reviewers than configured — and a retry is never a
    loop, because it spends tokens.

    Args:
        sessions: The fan-out's session records, in seat order.
        rerun: Callable re-running ONE seat, called as ``rerun(seat_no,
            session, parse_error)`` — the seat's 1-based fan-out
            position, its original session record and the reason its
            verdict failed to parse, so the caller can target the same
            seat and trace the original failure. It performs the
            caller's own bookkeeping (records, spend, trace) and returns
            the retry's session record, or ``None`` to REFUSE the
            re-run: the orchestrator refuses when the attempt's turn or
            wall-clock budget cannot fit one more seat (the re-run is
            capped at what remains and floored like the fan-out's own
            start check). A refused re-run is not counted. A quota hit
            on the re-run is NOT a refusal — the record comes back (and
            counts: tokens were spent) and the caller aborts the
            attempt exactly as its fan-out's own quota branch does.

    Returns:
        ``(reports, retries)`` — one :class:`L2Report` per seat, the
        retry's when it parses, and how many re-runs were spent.
    """
    reports: list[L2Report] = []
    retries = 0
    for seat_no, session in enumerate(sessions, start=1):
        report = parse_verdict(
            session.result if session.status == "completed" else None
        )
        if report.parse_error is not None and rerun is not None:
            retry_session = rerun(seat_no, session, report.parse_error)
            if retry_session is not None:
                retries += 1
                retried = parse_verdict(
                    retry_session.result
                    if retry_session.status == "completed"
                    else None
                )
                if retried.parse_error is None:
                    report = retried
        reports.append(report)
    return reports, retries


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
      in the findings — a carried blocking or major finding still fails
      the round (F-41 AC-6), because it is an unresolved problem — but is
      listed ``state: "carried"``, never as new.
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
    finding was dropped — out of delta by the F-23 rules, or by the F-38
    verifier's vote — and the declared fail stands with nothing left to
    fix.

    Args:
        report: The freshly parsed verdict of this round (after the F-38
            vote, in a fan-out pipeline: :func:`apply_vote`'s output).
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
    # F-41 AC-6: a surviving major fails the round exactly as a blocking
    # one does — a declared pass overrides neither.
    heavy = any(_fails_round(f) for f in active)
    verdict = (
        "fail"
        if report.parse_error is not None or report.declared == "fail" or heavy
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
    # Review finding 4: on the FIRST review (no delta, nothing out of
    # scope), a declared fail whose every finding the verifier voted out
    # is an unsupported fail — the vote removed its whole substance, so
    # the round passes on L2 (the drops stay recorded; the PR body says
    # the reviewers' fail was voted out). On a delta round (2+) the
    # F-23 all_dropped stop keeps its original meaning: findings dropped
    # as out of scope, or voted out, leave a fail with nothing to fix.
    vote_rejected_fail = (
        not active
        and not delta_mode
        and not dropped
        and bool(report.verifier_dropped)
        and report.parse_error is None
    )
    if vote_rejected_fail:
        verdict = "pass"
    everything_dropped = (
        not active and bool(dropped or report.verifier_dropped)
        and not vote_rejected_fail
    )
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
        verifier_dropped=report.verifier_dropped,
        verifier_error=report.verifier_error,
    )


# ------------------------------------------------------- F-38: the vote --

# F-41: how far apart two findings' lines may sit on the same file and
# still be one defect. Two reviewers pinpointing the same bug land within
# a handful of lines of each other; findings further apart on one file
# are different defects, however similar the wording.
_MERGE_LINE_WINDOW = 5
_MERGE_WORD_OVERLAP = 0.3


def _wording(text: str | None) -> set[str]:
    """The meaningful words of a finding's text, for :func:`_shares_wording`."""
    return set(re.findall(r"[a-z0-9_]{4,}", (text or "").lower()))


def _shares_wording(existing: L2Finding, finding: L2Finding) -> bool:
    """Whether two nearby findings describe the same thing: at least
    :data:`_MERGE_WORD_OVERLAP` of the shorter finding's meaningful words
    appear in the other. Location alone is not identity — two different
    defects a few lines apart must stay two findings, or one disappears
    from the verdict and the survivor claims a reviewer who never
    reported it. Too little wording to compare means no merge: separate
    findings only cost the verifier a vote."""
    one, two = _wording(existing.text), _wording(finding.text)
    if not one or not two:
        return False
    return len(one & two) / min(len(one), len(two)) >= _MERGE_WORD_OVERLAP


def _same_location(existing: L2Finding, finding: L2Finding) -> bool:
    """Whether two findings name the same spot: the same file, with
    integer lines within :data:`_MERGE_LINE_WINDOW` of each other. Both
    must carry a file AND a line — a finding without a line has no
    location identity to merge on (its wording is all it has)."""
    if not existing.file or not finding.file or existing.file != finding.file:
        return False
    if not isinstance(existing.line, int) or not isinstance(finding.line, int):
        return False
    return abs(existing.line - finding.line) <= _MERGE_LINE_WINDOW


def merge_reviews(reports: Sequence[L2Report]) -> L2Report:
    """Merge N reviewer verdicts into the ONE union the verifier votes on.

    Findings merge on :func:`finding_key` (the same stable identity F-23
    uses across rounds), so a defect two reviewers both report — however
    they spaced or cased its text — is one union entry carrying both
    reviewer numbers in ``reported_by``. Since F-41 they ALSO merge on
    location: the same file with lines within :data:`_MERGE_LINE_WINDOW`
    of each other, when both findings carry a file and a line and their
    wording overlaps (a location merge keeps every reporter's wording) — two
    reviewers describing one bug in different words keyed apart under
    text alone, and F-16's run showed the cost (every heavy finding
    ``reported_by=[1]``, the unanimous rule silent). The exact-text key
    is tried first; a location match merges into the entry that
    registered first (proximity is judged against ITS line, so a chain
    of ever-further findings cannot drift into one entry). A duplicate
    that one reviewer rated more severely keeps the harsher severity (a
    fan-out that downgraded a duplicate blocking call to "minor" would
    hide it). Union ids are regenerated (``U-1`` …) because two
    reviewers' ``R-1`` are different findings, and the verifier votes by
    id.

    The union's declared verdict is fail-closed: ANY reviewer's declared
    ``fail`` stands — the fan-out adds eyes, it never dilutes a refusal.
    And one invalid verdict invalidates the whole review (the single-
    reviewer rule unchanged): the union is a FAIL whose ``parse_error``
    names the reviewer that could not speak the protocol.

    Args:
        reports: The parsed verdicts, one per reviewer session, in fan-out
            order (reviewer 1 first — the union's order follows it).

    Returns:
        The union :class:`L2Report` — findings with fresh ids, reported_by
        and (merged) severity; or the invalid FAIL when any input was
        unparsable.
    """
    for index, report in enumerate(reports, start=1):
        if report.parse_error is not None:
            reason = f"reviewer {index}: {report.parse_error}"
            return L2Report(
                verdict="fail",
                findings=[Finding("invalid_verdict_json", reason)],
                parse_error=reason,
            )

    merged: list[L2Finding] = []
    index_of_key: dict[str, int] = {}
    for number, report in enumerate(reports, start=1):
        for finding in report.findings:
            if not isinstance(finding, L2Finding):
                continue  # an invalid-verdict Finding never reaches here
            index = index_of_key.get(finding.key)
            if index is None:
                # F-41: no exact-text duplicate — try the location key
                # (same file, nearby line) before registering a new entry.
                index = next(
                    (i for i, existing in enumerate(merged)
                     if number not in existing.reported_by
                     and _same_location(existing, finding)
                     and _shares_wording(existing, finding)),
                    None,
                )
            if index is None:
                union_finding = replace(
                    finding, id=f"U-{len(merged) + 1}", reported_by=[number]
                )
                merged.append(union_finding)
                index_of_key[union_finding.key] = len(merged) - 1
                continue
            existing = merged[index]
            if number not in existing.reported_by:
                existing.reported_by.append(number)
            harsher = _SEVERITY_RANK.get(finding.severity, 99) < _SEVERITY_RANK.get(
                existing.severity, 99
            )
            # The harsher call leads (replace() preserves the entry's
            # identity). Rank 0 is blocking, so harsher is the SMALLER rank
            # — the pre-F-41 comparison ran the other way and quietly kept
            # the softer call on every mixed-severity duplicate.
            lead, other = (finding, existing) if harsher else (existing, finding)
            text = lead.text
            if existing.key != finding.key and other.text not in text:
                # A location merge is a judgement, not an identity: keep
                # every reporter's wording, so a wrong merge can never
                # erase a defect from what the verifier and implementer see.
                text = f"{text} (also reported: {other.text})"
            merged[index] = replace(existing, severity=lead.severity, text=text)

    declared = (
        "fail"
        if any(report.declared == "fail" for report in reports)
        else "pass"
    )
    blocking = any(
        (f.severity or "").strip().lower() == "blocking" for f in merged
    )
    return L2Report(
        verdict="fail" if declared == "fail" or blocking else "pass",
        findings=merged,
        declared=declared,
    )


class VerifierOutcome(NamedTuple):
    """What one verifier session's vote did to the union.

    ``kept`` are the union findings that survived, carrying the voter's
    ``verifier_reason`` and ``tag`` (unvoted keeps carry neither).
    ``dropped`` is the drop ledger — ``{"finding": <finding dict>,
    "reason": <the voter's reason>}``. ``error`` is set iff the output was
    not valid vote JSON, in which case ``kept`` is the whole union and
    ``dropped`` empty: an unverifiable vote may not remove a finding.
    """

    kept: list
    dropped: list
    error: str | None


def parse_verifier_vote(output: Any, findings: Sequence[L2Finding]) -> VerifierOutcome:
    """Parse the verifier's vote over the union findings (F-38).

    The protocol is one object ``{"votes": [{"id", "keep", "reason",
    "tag?"}]}`` — extracted with the same candidate machinery as the
    reviewer's verdict (fences, balanced objects, last-wins, duplicate
    keys rejected). Every violation makes the WHOLE vote invalid: the
    verifier that cannot speak the vote protocol voted on nothing, and
    every finding survives it — the fail-open direction here is keeping
    findings, never dropping them.

    Args:
        output: The verifier session's result text (or None / non-text
            when the session failed — same treatment).
        findings: The union findings the verifier was asked about.

    Returns:
        The :class:`VerifierOutcome`.
    """
    def invalid(reason: str) -> VerifierOutcome:
        return VerifierOutcome(kept=list(findings), dropped=[], error=reason)

    if not isinstance(output, str):
        return invalid(
            f"the verifier produced no vote text ({type(output).__name__}): "
            "a failed or aborted session drops nothing"
        )
    try:
        data = _extract_json(output)
    except _InvalidVerdict as exc:
        return invalid(f"no valid vote JSON in the verifier output ({exc})")
    if not isinstance(data, dict) or not isinstance(data.get("votes"), list):
        return invalid("vote JSON must be an object with a 'votes' list")

    by_id = {f.id: f for f in findings if isinstance(f, L2Finding)}
    votes: dict[str, dict] = {}
    for index, vote in enumerate(data["votes"]):
        if not isinstance(vote, dict):
            return invalid(f"vote {index} is not an object")
        vid = vote.get("id")
        if vid not in by_id:
            return invalid(f"vote {index} names an unknown finding id: {vid!r}")
        if vid in votes:
            return invalid(f"two votes for finding {vid!r}: an ambiguous vote "
                           "is no vote")
        if not isinstance(vote.get("keep"), bool):
            return invalid(f"vote for {vid!r} carries no boolean 'keep'")
        reason = vote.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return invalid(f"vote for {vid!r} carries no reason")
        if vote["keep"]:
            tag = vote.get("tag")
            if tag not in VALID_TAGS:
                return invalid(
                    f"vote for {vid!r} keeps without a valid tag "
                    f"({VALID_TAGS}): {tag!r}"
                )
        votes[vid] = vote

    kept: list[L2Finding] = []
    dropped: list[dict] = []
    for finding in findings:
        if not isinstance(finding, L2Finding):
            continue
        vote = votes.get(finding.id)
        if vote is None:
            # Nobody voted on it: kept, and untagged by the vote (the
            # default tag comes from severity at vote-application time).
            kept.append(replace(finding))
        elif vote["keep"]:
            kept.append(
                replace(
                    finding,
                    verifier_reason=vote["reason"],
                    tag=vote["tag"],
                )
            )
        else:
            dropped.append(
                {"finding": dict(finding.__dict__), "reason": vote["reason"]}
            )
    return VerifierOutcome(kept=kept, dropped=dropped, error=None)


def _default_tag(severity: str | None) -> str:
    """The tag an unvoted survivor gets: severity's own bucket."""
    heavy = (severity or "").strip().lower() in ("blocking", "major")
    return "important" if heavy else "nit"


def _unanimous(finding: L2Finding, reviewers: int) -> bool:
    """Whether every reviewer of the fan-out reported this blocking or
    major finding (review finding 2's unanimous rule)."""
    if reviewers < 1:
        return False
    heavy = (finding.severity or "").strip().lower() in ("blocking", "major")
    reported = set(finding.reported_by or [])
    return heavy and reported == set(range(1, reviewers + 1))


def apply_vote(
    report: L2Report,
    outcome: VerifierOutcome,
    reviewers: int = 1,
) -> L2Report:
    """Apply the verifier's vote: rank, tag and budget the survivors (F-38).

    The mechanical rules, enforced here rather than trusted from the
    model:

    * A ``blocking`` finding is always tagged ``important``. A blocking
      finding is the one thing that fails the round; tagging it
      ``pre-existing`` ("not the author's fault") while it blocks the
      author's PR is a contradiction — if it truly predates the diff and
      should not block, the verifier's tool is the DROP, with a reason,
      not a re-tag. Every other severity keeps the verifier's tag.
    * **The unanimous rule** (review finding 2): a ``blocking`` or
      ``major`` finding that EVERY reviewer of the fan-out reported cannot
      be dropped by the one verifier — independent agreement is stronger
      evidence than one voter's objection. The drop is refused: the
      finding survives tagged ``important`` and the ledger records the
      attempt as ``override_refused: <the verifier's reason>`` (the reason
      is kept — a refused drop with no why is a hole in the audit). A
      finding fewer than all reviewers reported keeps the vote's full
      semantics, drop and all.
    * Survivors are ranked by severity first (the Anthropic record's
      "sorted by severity"), then by tag (important, nit, pre-existing),
      then by union order.
    * REVIEW.md's nit budget: at most :data:`NIT_BUDGET` findings of
      severity ``minor`` or ``note`` survive; the rest move to
      ``verifier_dropped`` with reason ``nit_budget``. ``blocking`` and
      ``major`` are never capped.
    * The verdict is recomputed over what survived the vote: a declared
      fail stands, and so does any surviving blocking or major finding
      (F-41 AC-6) — the reverse (a declared pass whose only blocking
      finding the verifier dropped) is the pass the reviewers actually
      declared.

    Args:
        report: The union report (:func:`merge_reviews` output).
        outcome: The parsed vote (:func:`parse_verifier_vote` output).
        reviewers: How many reviewer sessions the fan-out ran (the
            unanimous rule needs N; default 1 — with one reviewer,
            every finding is by definition unanimous).

    Returns:
        The post-vote :class:`L2Report` — ranked survivors in ``findings``,
        the verifier's and the budget's drops in ``verifier_dropped``.
    """
    kept = list(outcome.kept)
    drops: list[dict] = []
    unanimous_pool = {
        finding.id: finding for finding in report.findings
        if isinstance(finding, L2Finding) and _unanimous(finding, reviewers)
    }
    for entry in outcome.dropped:
        finding = unanimous_pool.get((entry.get("finding") or {}).get("id"))
        if finding is None:
            drops.append(entry)
            continue
        kept.append(replace(
            finding,
            tag="important",
            verifier_reason=(
                "vote to drop refused (every reviewer reported it): "
                f"{entry.get('reason')}"
            ),
        ))
        drops.append({
            **entry,
            "reason": f"override_refused: {entry.get('reason')}",
        })

    decorated: list[tuple] = []
    for index, finding in enumerate(kept):
        if not isinstance(finding, L2Finding):
            decorated.append((0, 0, index, finding))
            continue
        tag = finding.tag or _default_tag(finding.severity)
        if (finding.severity or "").strip().lower() == "blocking":
            tag = "important"
        decorated.append((
            _SEVERITY_RANK.get(finding.severity, 99),
            _TAG_RANK.get(tag, 99),
            index,
            replace(finding, tag=tag),
        ))
    decorated.sort(key=lambda entry: entry[:3])
    ranked = [entry[3] for entry in decorated]

    survivors: list = []
    nits = 0
    for finding in ranked:
        if (
            isinstance(finding, L2Finding)
            and (finding.severity or "").strip().lower() in ("minor", "note")
        ):
            nits += 1
            if nits > NIT_BUDGET:
                drops.append(
                    {"finding": dict(finding.__dict__), "reason": "nit_budget"}
                )
                continue
        survivors.append(finding)

    # F-41 AC-6: a surviving major fails the round exactly as a blocking
    # one does — a declared pass overrides neither.
    heavy = any(_fails_round(f) for f in survivors)
    verdict = (
        "fail"
        if report.parse_error is not None or report.declared == "fail" or heavy
        else "pass"
    )
    return L2Report(
        verdict=verdict,
        findings=survivors,
        declared=report.declared,
        parse_error=report.parse_error,
        verifier_dropped=drops,
        verifier_error=outcome.error,
    )


def final_review_verdict(report: L2Report) -> L2Report:
    """The final full review's failure rule (F-41 AC-2): only a
    surviving ``blocking`` or ``major`` finding fails it.

    The final review runs once the fix rounds have converged, over the
    complete diff with nothing out of scope (AC-2), so its verdict is
    about the findings alone: ``minor`` and ``note`` findings never fail
    it — they reach the PR body within the nit budget — and neither does
    a declared ``fail`` with nothing heavier standing. A parse error
    still fails it (a review that cannot speak the verdict protocol has
    not reviewed anything), and a surviving blocking or major does
    whatever the declared verdict says (AC-6, applied upstream by
    :func:`apply_convergence`; this function never softens it).

    Args:
        report: The converged final-review report (apply_convergence
            output in full-diff mode, after the verifier vote).

    Returns:
        The report with the final review's own verdict — the input
        unchanged when the verdict already agrees.
    """
    heavy = any(_fails_round(f) for f in report.findings)
    fail = report.parse_error is not None or heavy
    if fail == (report.verdict == "fail"):
        return report
    if fail:
        return replace(report, verdict="fail")
    # A declared fail with nothing heavier than nits: the round passes,
    # and no stop may fire on the strength of the verdict it overturned.
    return replace(report, verdict="pass", no_progress=False, no_progress_reason=None)
