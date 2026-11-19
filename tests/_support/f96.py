"""F-96 test support: per-finding verifier seats over the F-94 harness.

Composed from ``_support.f94`` (F-51 AC-7: no test module imports
another). Everything real the F-94 harness makes real stays real — git,
the clone manager, the queue, L0/L1, the push fence; only the model
seats are scripted. What this adds:

* ``RULE_PIN`` — the shared sibling rule, pinned by literal exactly as
  ``reviewer.md`` carries it (the test is the sanctioned way the text
  changes);
* ``IMPL_SUMMARY_CLAIM`` — the implementer-prompt sentence F-96 removes
  (the reviewer never sees the summary), pinned so its removal is the
  test's to sanction;
* ``VoteSeats`` — ``LensSeats`` whose verifier votes are scripted PER
  SESSION (a serious finding's two sessions can disagree), observing
  every verifier prompt, the ids each was given and how many sessions
  ran at once.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable

from _support.f94 import LensSeats, _finding, _verdict
from factory.session import SessionRecord

__all__ = [
    "IMPL_SUMMARY_CLAIM", "RULE_PIN", "VoteSeats", "_finding", "_verdict",
]

# The shared sibling rule — the "Draw the line" paragraph, moved from the
# defect-hunt lens into the BASE reviewer prompt (F-96 AC-5) so every
# seat carries it word for word. Pinned by literal: editing the section
# in factory/data/prompts/reviewer.md without this pin fails test_ac_5.
RULE_PIN = (
    "A defect this diff CAUSES — including a break at an unchanged "
    "caller,\ntest or config — is reported WITHOUT `\"sibling\"` and "
    "judged by its\nseverity like any finding. `\"sibling\": true` is only "
    "for an older site\nof the same class that this diff neither causes "
    "nor touches: the\norchestrator records it for a follow-up task and it "
    "does not fail this\nround, whatever its severity — rate it by what "
    "the defect costs, and\njudge your verdict on the diff alone. "
    "`\"sibling\": true` on a file the\ndiff changes lowers nothing: a "
    "defect inside the diff is judged by the\ndiff's rules."
)

# The implementer-prompt sentence F-96 AC-5 removes (prompt-audit #2):
# the reviewer never sees the implementer's summary, and the prompt
# stops claiming it does. Pinned so re-adding the claim fails the test.
IMPL_SUMMARY_CLAIM = "an honest summary helps the reviewer navigate"

# The findings array in a verifier prompt (per-finding: exactly one
# entry — F-96 AC-1), the same fenced shape the F-38 prompt used.
_FINDING_ARRAY = re.compile(r"```json\n(\[\s*\{.*?\}\s*\])\n```", re.S)

# A quota-shaped failure reason (matches factory.orchestrator._QUOTA).
QUOTA_REASON = "api_error_429: usage quota exceeded"


class VoteSeats(LensSeats):
    """``LensSeats`` whose verifier votes are scripted PER SESSION.

    ``script`` maps a finding-text substring to the list of per-session
    scripts its verifier sessions play in order (the last repeats when a
    finding gets more sessions than scripts). One script is one of:

    * ``{"drop": True, "reason": str}`` — a vote to drop;
    * ``{"tag": str, "reason": str}`` — a vote to keep, tagged;
    * ``"invalid"`` — a completed session whose vote does not parse;
    * ``"invalid_resumable"`` — the same, WITH a ``session_id`` (the
      shape F-85's repair turn resumes — the test observes the repair
      call's ``settings_path``);
    * ``"crash"`` — a failed session (no result);
    * ``"timeout"`` — an aborted session (no result);
    * ``"quota"`` — a failed session whose reason matches the quota
      classifier.

    Findings with no script entry are kept ``important``/``confirmed``
    by every session. The seat records every verifier prompt, the ids
    each prompt was given, and the most verifier sessions that ran at
    once (the parallel cap is observable through it). ``on_verifier_call``
    runs on the verifier session's entry (1-based call number) — the
    budget tests burn wall clock between waves with it. A prompt is
    never assumed to carry one finding: today's single-prompt verifier
    also parses, so red-first runs fail on the assertions, not on a
    crashed stub.
    """

    def __init__(self, script: dict | None = None, *,
                 conformance: str | None = None, hunt: str | None = None,
                 on_verifier_call: "Callable[[int], None] | None" = None,
                 pause: float = 0.0):
        super().__init__(conformance=conformance, hunt=hunt)
        self.script = script or {}
        self.on_verifier_call = on_verifier_call
        # A verifier session's dwell time: the cap tests need the
        # sessions of one wave genuinely in flight together (a
        # microsecond stub can finish before its siblings start, and
        # max-in-flight would read 1 whatever the pool size).
        self.pause = pause
        self.verifier_prompts: list[str] = []
        self.verifier_id_lists: list[list] = []
        self.max_inflight = 0
        self._inflight = 0
        self._verifier_calls = 0
        self._calls_by_id: dict[str, int] = {}

    def _vote(self, prompt: str, ordinals: dict, call_no: int) -> SessionRecord:
        """One verifier session's record. ``ordinals`` (computed under
        the call lock, so two sessions of one finding deterministically
        play scripts 1 and 2) maps finding id -> this session's 1-based
        ordinal for it; ``call_no`` is the 1-based GLOBAL verifier call
        number (the fan-out ordinal — what the repair's settings derive
        from)."""
        match = _FINDING_ARRAY.search(prompt)
        # A repair-turn prompt carries no findings array: return a
        # well-formed empty vote (the parent's repair-usability check
        # then decides) rather than crashing the stub.
        found = json.loads(match.group(1)) if match else []
        votes = []
        status = "completed"
        reason = None
        invalid = False
        sid = None
        for finding in found:
            fid = finding["id"]
            scripts = next(
                (s for key, s in self.script.items()
                 if key in finding.get("text", "")),
                None,
            )
            if scripts is not None:
                index = min(ordinals.get(fid, 1) - 1, len(scripts) - 1)
                script = scripts[index]
            else:
                script = {"tag": "important",
                          "reason": "confirmed against the diff"}
            if script == "invalid":
                invalid = True
                continue
            if script == "invalid_resumable":
                invalid = True
                sid = f"resume-sid-v{call_no}"
                continue
            if script == "crash":
                status, reason = "failed", "the sandbox runner crashed"
                continue
            if script == "timeout":
                status, reason = "aborted", "wall-clock timeout"
                continue
            if script == "quota":
                status, reason = "failed", QUOTA_REASON
                continue
            if script.get("drop"):
                votes.append({"id": fid, "keep": False,
                              "reason": script["reason"]})
            else:
                votes.append({"id": fid, "keep": True,
                              "reason": script["reason"],
                              "tag": script.get("tag", "important")})
        result = None
        if status == "completed":
            # "invalid" is a COMPLETED session whose text is not a vote
            # at all (not an empty-votes payload — that one parses and
            # keeps every finding unvoted, a different arm).
            result = "not a vote at all" if invalid else json.dumps(
                {"votes": votes})
        return SessionRecord(
            seat="verifier", status=status, reason=reason, num_turns=1,
            model="stub-model", session_id=sid,
            tokens={"input_tokens": 55, "output_tokens": 5},
            result=result,
        )

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat != "verifier":
            return super().__call__(seat, prompt, run_dir, profile, **kwargs)
        if kwargs.get("resume"):
            # An F-85 repair turn, not a new session: play a
            # well-formed vote and stay out of the session accounting.
            return self._vote(prompt, {}, 0)
        with self.lock:
            self._verifier_calls += 1
            number = self._verifier_calls
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
            match = _FINDING_ARRAY.search(prompt)
            found = json.loads(match.group(1)) if match else []
            self.verifier_prompts.append(prompt)
            self.verifier_id_lists.append([f["id"] for f in found])
            ordinals = {}
            for f in found:
                fid = f["id"]
                self._calls_by_id[fid] = self._calls_by_id.get(fid, 0) + 1
                ordinals[fid] = self._calls_by_id[fid]
        if self.on_verifier_call is not None:
            self.on_verifier_call(number)
        try:
            if self.pause:
                time.sleep(self.pause)
            record = self._vote(prompt, ordinals, number)
        finally:
            with self.lock:
                self._inflight -= 1
        return record

    def verifier_call_count(self) -> int:
        """How many verifier sessions ran."""
        with self.lock:
            return self._verifier_calls

