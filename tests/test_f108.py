"""F-108 — a contract review's malformed output gets one repair turn.

Evidence: JobFinderOS J-3 and J-8 (2026-11-20/21) — twice a
``factory review-contract`` session ended "the session's output is not a
valid review (… not exactly one JSON object …)" with the reading done,
nothing stored, and the owner re-running the whole session by hand. The
review seats have had exactly this recovery since F-85 (one repair turn
on the SAME session, the parse never loosened); the contract reviewer
never got it.

Contract rule (CLAUDE.md): an acceptance ref
``tests/test_f108.py::test_ac_N`` means a test function with exactly
that name.

Two acceptance criteria (BUILD-ORDER §F-108):

- AC-1 on a parse failure, the SAME session is resumed once at
  ``REPAIR_TURN_CAP`` with the parse error, the findings schema and
  "re-emit the findings object you just produced, correctly formatted;
  change no finding". A repaired object passes the UNCHANGED strict
  parse and is stored with ``repaired: true``, the repair's usage and
  turns added to the stored total, and ``factory approve`` showing it
  with one "(format repaired)" note. A second failure refuses with
  today's message and stores nothing.
- AC-2 the repair cannot change what was reviewed: the same read-only
  seat sandbox, the same ``validate_contract_findings`` against the
  task's criterion ids, and the store-time contract-hash recheck — a
  contract edited mid-review refuses and stores nothing, repaired or
  not.

Real vs stubbed: the queue, the clone manager, the store and the owner
ledger are real (``_support.f80``); only the model seat is scripted.
"""

import json
from pathlib import Path

import pytest
from _support.f80 import (
    OFF_PEAK,
    Echo,
    Env80,
    finding,
    findings_payload,
)

from factory.config import FactoryConfig
from factory.contract import ContractError, contract_hash
from factory.session import SessionRecord
from factory.state import TaskQueue

NO_PEAK = FactoryConfig(peak_window=None)

# The one instruction the repair turn carries (AC-1's exact demand).
REPAIR_INSTRUCTION = (
    "Re-emit the findings object you just produced, correctly formatted; "
    "change no finding."
)

GOOD = findings_payload(finding("CR-1", "purpose", "major",
                                "the check cannot fail on the happy path",
                                "AC-1"))
# The J-3/J-8 shape: the findings object, complete, with prose after it.
PROSE = GOOD + "\nHope this helps; the contract holds up overall."
TWO_OBJECTS = GOOD + "\n" + findings_payload()
FENCE_THEN_PROSE = "```json\n" + GOOD + "\n```\nDone."
MISSING_BRACE = GOOD[:-1]
# A repair naming a criterion this contract does not have.
UNKNOWN_CRITERION = findings_payload(finding("CR-1", "purpose", "major",
                                             "about a criterion that is "
                                             "not there", "AC-9"))
# Valid JSON, the wrong shape (an unknown top-level key).
WRONG_SHAPE = json.dumps({"findings": [finding()], "verdict": "pass"})


def reply(result, *, status: str = "completed", reason: str | None = None,
          turns: int = 4, session_id: str | None = "cr-sid-1",
          usage: dict | None = None) -> dict:
    """One scripted session reply."""
    return {"result": result, "status": status, "reason": reason,
            "turns": turns, "session_id": session_id,
            "usage": usage if usage is not None else {
                "input": 900, "cache_read": 0, "cache_write": 0,
                "output": 120}}


class RepairReviewSeat:
    """The scripted ``contract_reviewer`` over a list of replies, one per
    runner call (the first session, then its repair turn). Records every
    call's seat, prompt and kwargs; ``hook`` runs inside the FIRST call
    with the run dir (the mid-review owner-edit arms)."""

    def __init__(self, replies: list[dict], *, hook=None):
        self.replies = list(replies)
        self.hook = hook
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        index = min(len(self.calls), len(self.replies) - 1)
        scripted = self.replies[index]
        self.calls.append((seat, prompt, kwargs))
        if self.hook is not None and len(self.calls) == 1:
            self.hook(Path(run_dir))
        return SessionRecord(
            seat=seat, status=scripted["status"],
            reason=scripted["reason"], num_turns=scripted["turns"],
            session_id=scripted["session_id"], model="stub-model",
            usage=scripted["usage"],
            result=(scripted["result"]
                    if scripted["status"] == "completed" else None),
        )


class ExplodingRepairSeat:
    """A first session that completes malformed and resumable, then a
    repair turn whose runner call RAISES (the transcript is gone)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SessionRecord(
                seat=seat, status="completed", num_turns=4,
                session_id="cr-sid-x", model="stub-model",
                usage={"input": 900, "cache_read": 0, "cache_write": 0,
                       "output": 120}, result=PROSE)
        raise RuntimeError("the session transcript is gone")


def _review(env: Env80, seat, task_id: str = "T-1", **kwargs):
    from factory.contractreview import review_contract

    kwargs.setdefault("now", lambda: OFF_PEAK)
    kwargs.setdefault("config", NO_PEAK)
    return review_contract(env.queue_path, task_id, factory_root=env.root,
                           session_runner=seat, **kwargs)


def _refused(env: Env80, seat, match: str, *, queue_edited: bool = False,
             **kwargs) -> str:
    """A refusal: raises, stores nothing. The queue is byte-identical
    unless the scripted scenario itself edited it mid-review. Returns
    the refusal message."""
    before = env.queue_bytes()
    stored = [(p, p.read_bytes()) for p in env.stored()]
    with pytest.raises(ContractError, match=match) as excinfo:
        _review(env, seat, **kwargs)
    assert [(p, p.read_bytes()) for p in env.stored()] == stored
    if not queue_edited:
        assert env.queue_bytes() == before
    return str(excinfo.value)


def _header_note(env: Env80) -> str:
    """What ``factory approve`` echoed about the contract review before
    the y/N (every line carrying 'contract review')."""
    asked: list[str] = []
    echo = Echo()

    def confirm(question):
        asked.append(echo.text)
        return True

    from factory.approve import approve_task

    status = approve_task(
        TaskQueue(env.queue_path), "T-1", approved_by="owner", echo=echo,
        factory_root=env.root, confirm=confirm)
    assert status == "ready"
    return "\n".join(line for line in asked[0].splitlines()
                     if "contract review" in line)


# --------------------------------------------------------------------------
# AC-1 — one repair, same session, then today's refusal
# --------------------------------------------------------------------------


def test_ac_1(tmp_path: Path, monkeypatch):
    from factory import contractreview
    from factory.contractreview import load_stored_review
    from factory.l2 import parse_contract_review
    from factory.orchestrator import (
        CONTRACT_REVIEWER_TURN_CAP,
        REPAIR_TURN_CAP,
    )

    assert CONTRACT_REVIEWER_TURN_CAP > REPAIR_TURN_CAP  # a tight cap

    # (a) the J-3/J-8 shape, repaired: trailing prose, then the clean
    # object — stored once, under the pre-session hash, flagged.
    env = Env80(tmp_path / "a", task=None)
    chash = contract_hash(env.task())
    before = env.queue_bytes()
    seat = RepairReviewSeat([
        reply(PROSE),
        reply(GOOD, turns=2, session_id=None, usage={
            "input": 300, "cache_read": 0, "cache_write": 0,
            "output": 40}),
    ])
    result = _review(env, seat)
    assert env.queue_bytes() == before, "the review never writes the queue"
    assert len(seat.calls) == 2
    # The repair resumed the SAME session — same seat, same sandbox
    # contract, one instruction, the FIXED repair cap.
    seat_name, prompt, kwargs = seat.calls[1]
    assert seat_name == "contract_reviewer"
    assert kwargs["resume"] == "cr-sid-1"
    assert kwargs["max_turns"] == REPAIR_TURN_CAP
    assert kwargs["system_prompt"] == contractreview.SYSTEM_PROMPT
    assert kwargs["role"] == "contract_reviewer"
    assert kwargs["protected"] == seat.calls[0][2]["protected"]
    assert kwargs["run_id"] == seat.calls[0][2]["run_id"]
    # The prompt: the parse error verbatim, the findings schema, the one
    # instruction.
    parsed = parse_contract_review(PROSE, ["AC-1"])
    assert parsed.error is not None
    assert parsed.error in prompt
    for key in ("id", "lens", "severity", "text", "criterion",
                "suggested_change"):
        assert f'"{key}"' in prompt, key
    assert REPAIR_INSTRUCTION in prompt
    # The stored review: repaired, the repair's usage and turns ADDED.
    stored = json.loads((env.review_dir() / f"{chash}.json").read_text())
    assert result.path == env.review_dir() / f"{chash}.json"
    assert stored["contract_hash"] == chash
    assert stored["repaired"] is True
    assert [f["id"] for f in stored["findings"]] == ["CR-1"]
    assert stored["usage"] == {"input": 1200, "cache_read": 0,
                               "cache_write": 0, "output": 160}
    assert stored["turns"] == 6  # the session's 4 + the repair's 2
    # The loader accepts the new field, and approve shows one note.
    assert load_stored_review(
        env.root, "fixture", env.task()).state == "current"
    note = _header_note(env)
    assert "(format repaired)" in note

    # (b) a second malformed reply refuses with today's message and
    # stores nothing — and NEVER a second repair (a third reply is
    # scripted and must stay unused).
    env = Env80(tmp_path / "b", task=None)
    seat = RepairReviewSeat([
        reply(PROSE), reply("no review at all"), reply(GOOD)])
    message = _refused(env, seat, "not a valid review")
    assert len(seat.calls) == 2
    # Today's message, exactly: the shape the owner saw twice, naming
    # the SECOND reply's parse error (the session's newest state).
    assert message.startswith(
        "contract review failed: the session's output is not a valid "
        "review (")
    assert "not exactly one JSON object" in message
    assert message.endswith("— nothing was stored")

    # (c) a malformed reply from a session with no resumable id gets NO
    # repair: today's single-call refusal.
    env = Env80(tmp_path / "c", task=None)
    seat = RepairReviewSeat([reply(PROSE, session_id=None)])
    _refused(env, seat, "not a valid review")
    assert len(seat.calls) == 1

    # (d) a repair session that dies (the turn cap, an SDK error) is a
    # clean refusal naming it — no traceback, nothing stored.
    env = Env80(tmp_path / "d", task=None)
    seat = RepairReviewSeat([
        reply(PROSE),
        reply(None, status="failed", reason="error_max_turns",
              session_id=None)])
    _refused(env, seat, "repair turn ended failed")
    assert len(seat.calls) == 2

    # (e) a repair whose runner call RAISES (the transcript gone): clean
    # refusal, nothing stored, no traceback.
    env = Env80(tmp_path / "e", task=None)
    seat = ExplodingRepairSeat()
    _refused(env, seat, "could not run")

    # (f) a clean first reply: no repair, no flag, no note.
    env = Env80(tmp_path / "f", task=None)
    seat = RepairReviewSeat([reply(GOOD)])
    result = _review(env, seat)
    assert len(seat.calls) == 1
    assert "repaired" not in result.review
    assert "(format repaired)" not in _header_note(env)

    # (h) every lost-review shape from the runs, each repaired once and
    # stored.
    for name, first in (("trailing prose", PROSE),
                        ("fence then prose", FENCE_THEN_PROSE),
                        ("two objects", TWO_OBJECTS),
                        ("missing closing brace", MISSING_BRACE)):
        env = Env80(tmp_path / f"lost-{name.replace(' ', '-')}", task=None)
        seat = RepairReviewSeat([reply(first), reply(GOOD)])
        result = _review(env, seat)
        assert len(seat.calls) == 2, name
        assert result.review.get("repaired") is True, name
        assert [f["id"] for f in result.review["findings"]] == ["CR-1"], name
        assert "(format repaired)" in _header_note(env), name

    # (i) the parse itself never loosened: every malformed shape above
    # still fails it (no lenient mode was added).
    for bad in (PROSE, FENCE_THEN_PROSE, TWO_OBJECTS, MISSING_BRACE):
        assert parse_contract_review(bad, ["AC-1"]).error is not None

    # (g) LAST (it patches the wall clock for the rest of the test): no
    # wall clock left for a repair — the refusal names it and the repair
    # never starts (the scripted clean second reply stays unused).
    env = Env80(tmp_path / "g", task=None)
    monkeypatch.setattr(contractreview, "REVIEW_WALL_CLOCK_MIN", 0.0)
    seat = RepairReviewSeat([reply(PROSE), reply(GOOD)])
    _refused(env, seat, "repair turn")
    assert len(seat.calls) == 1


# --------------------------------------------------------------------------
# AC-2 — the repair cannot change what was reviewed
# --------------------------------------------------------------------------


def test_ac_2(tmp_path: Path):
    from factory.contractreview import load_stored_review

    # (a) a repair naming a criterion the contract does not have fails
    # the SAME strict validation (validate_contract_findings against the
    # task's criterion ids) and refuses — nothing stored.
    env = Env80(tmp_path / "a", task=None)
    seat = RepairReviewSeat([reply(PROSE), reply(UNKNOWN_CRITERION)])
    _refused(env, seat, "not a valid review")
    assert len(seat.calls) == 2

    # (b) a repair that is valid JSON but the wrong shape: same refusal.
    env = Env80(tmp_path / "b", task=None)
    seat = RepairReviewSeat([reply(PROSE), reply(WRONG_SHAPE)])
    _refused(env, seat, "not a valid review")

    # (c) the contract edited between the session and the store: the
    # hash recomputed at store time differs, so the review refuses and
    # stores nothing — repaired or not.
    def edit_contract(run_dir: Path) -> None:
        env.edit_task(lambda task: task["acceptance"][0].update(
            text="answer() returns 43 on every path"))

    env = Env80(tmp_path / "c-plain", task=None)
    chash = contract_hash(env.task())
    seat = RepairReviewSeat([reply(GOOD)], hook=edit_contract)
    _refused(env, seat, "changed while the review ran", queue_edited=True)
    assert env.stored() == []
    # The refusal fires for a repaired review too: the repair succeeded,
    # the store still refuses.
    env = Env80(tmp_path / "c-repaired", task=None)
    seat = RepairReviewSeat([reply(PROSE), reply(GOOD)],
                            hook=edit_contract)
    _refused(env, seat, "changed while the review ran", queue_edited=True)
    assert env.stored() == []
    # The arm's own control: the edit really did change the hash (both
    # arms' tasks start identical, so chash is the pre-edit hash here).
    assert contract_hash(env.task()) != chash

    # (d) a mid-review queue rewrite that changes NOTHING (a write of
    # the same bytes) still stores: the check is the hash, not mtime.
    env = Env80(tmp_path / "d", task=None)

    def rewrite_same(run_dir: Path) -> None:
        env.queue_path.write_bytes(env.queue_path.read_bytes())

    seat = RepairReviewSeat([reply(GOOD)], hook=rewrite_same)
    result = _review(env, seat)
    assert result.review["contract_hash"] == contract_hash(env.task())

    # (e) a repair that adds a finding the malformed reply never carried
    # is ALLOWED — the parse is the gate — and the flag carries the
    # honesty: approve names it a repaired review.
    env = Env80(tmp_path / "e", task=None)
    grown = findings_payload(
        finding("CR-1", "purpose", "major",
                "the check cannot fail on the happy path", "AC-1"),
        finding("CR-2", "scope", "minor",
                "a finding the prose never mentioned", None),
    )
    seat = RepairReviewSeat([reply(PROSE), reply(grown)])
    result = _review(env, seat)
    assert result.review["repaired"] is True
    assert [f["id"] for f in result.review["findings"]] == ["CR-1", "CR-2"]
    assert "(format repaired)" in _header_note(env)

    # (f) the stored repaired review reloads through the same validator
    # the session's output was held to (the store's re-read at approval).
    stored = load_stored_review(env.root, "fixture", env.task())
    assert stored.state == "current"
    assert stored.review["repaired"] is True
    assert [f["id"] for f in stored.review["findings"]] == ["CR-1", "CR-2"]
