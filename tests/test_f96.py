"""F-96 contract tests: every finding gets its own verifier; a serious
one needs two to drop it; every seat knows the sibling rule.

F-38 ran ONE verifier session over the whole merged list, so one bad
verifier pass could drop every finding it disliked — and since F-94 gave
the seats different lenses, the findings only seat 2 hunts (caller
breaks, siblings) were exactly the ones a single verifier could drop
unprotected. F-96 gives each finding its own fresh verifier session (a
serious one two), keeps every finding whose verifier session failed, and
moves the sibling rule into the base reviewer prompt so every seat draws
the same line — including in fix rounds, where an unflagged old site
today blocks a correct PR and a FLAGGED one is dropped as out of delta
and lost. Red-first against the tree before the implementation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from _support.f72 import IMPL_GOOD
from _support.f94 import HUNT_MARK, followup, run_round
from _support.f96 import (
    IMPL_SUMMARY_CLAIM,
    RULE_PIN,
    VoteSeats,
    _finding,
    _verdict,
)

# tests/test_base.py is tracked on the fixture's base and never touched
# by the scripted implementer: the canonical "other site".
UNTOUCHED = "tests/test_base.py"
CHANGED = "impl.py"


def _sibling(text: str, file: str = UNTOUCHED, severity: str = "major",
             **extra) -> dict:
    """A finding the reviewer FLAGS as a sibling site — F-94's strict
    claim, so the parent's outside-the-diff proof can rule on it."""
    extra.setdefault("sibling", True)
    return _finding(text=text, file=file, severity=severity, axis="spec",
                    **extra)


def _by_text(l2: dict, marker: str) -> dict:
    return next(f for f in l2["findings"] if marker in f["text"])


# --------------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path, monkeypatch):
    """After merge_reviews every finding gets its own verifier session:
    a serious one two, everything else one — each prompt naming exactly
    one finding id and carrying no other finding's text."""
    major = _finding("AC1-MAJOR empty-state row renders unknown",
                     severity="major")
    minor_b = _finding("AC1-minor-two rename helper _do to _render",
                       severity="minor")
    minor_c = _finding("AC1-minor-three dead import os predates this",
                       severity="minor", file="other.py")
    seats = VoteSeats(
        conformance=_verdict(major, minor_b, declared="fail"),
        hunt=_verdict(minor_c, declared="fail"),
    )
    env, result = run_round(tmp_path, monkeypatch, seats)

    # The session plan: the major twice (two independent verifiers),
    # each minor once — 4 sessions over 3 findings.
    assert seats.verifier_call_count() == 4, seats.verifier_id_lists
    assert len(seats.verifier_prompts) == 4
    texts = ("AC1-MAJOR", "AC1-minor-two", "AC1-minor-three")
    for prompt, ids in zip(seats.verifier_prompts, seats.verifier_id_lists):
        assert len(ids) == 1, f"a verifier prompt carries {len(ids)} findings"
        present = [t for t in texts if t in prompt]
        assert len(present) == 1, (
            f"a verifier prompt must carry one finding's text, got {present}")
        # The prompt names the finding it was given, with its reporters.
        assert '"reported_by"' in prompt
    # Each finding was verified in its own fresh session: every id
    # appears across the prompts, the major twice.
    all_ids = [i for ids in seats.verifier_id_lists for i in ids]
    assert len(all_ids) == len(set(all_ids)) or True  # ids repeat for majors
    counted: dict[str, int] = {}
    for ids in seats.verifier_id_lists:
        for i in ids:
            counted[i] = counted.get(i, 0) + 1
    assert sorted(counted.values()) == [1, 1, 2]  # the major twice
    # Every prompt carries the contract and the diff the reviewers saw.
    for prompt in seats.verifier_prompts:
        assert '"id": "T-1"' in prompt
        assert "## Full diff (base...head)" in prompt
    # The session plan covers every finding: each id appears, the
    # major's twice.
    counted: dict[str, int] = {}
    for ids in seats.verifier_id_lists:
        for i in ids:
            counted[i] = counted.get(i, 0) + 1
    assert len(counted) == 3 and sorted(counted.values()) == [1, 1, 2]
    # The round itself is unchanged by the fan-out shape: the declared
    # fail stands (the findings were kept by the default keep votes).
    assert result.outcome != "pr_opened", result


# --------------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path: Path, monkeypatch):
    """A blocking/major finding needs TWO agreeing drops to go; one keep
    or one invalid vote keeps it; the unanimous rule stays on top; a
    minor follows its single vote; a surviving sibling still never
    blocks and still reaches the follow-up."""
    major = _finding("AC2-MAJOR scope creep: answer() also logs",
                     severity="major")

    # (a) major, drop + drop -> dropped, both reasons recorded.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [
            {"drop": True, "reason": "not real: the logger is asked for"},
            {"drop": True, "reason": "not real: the contract names logging"},
        ]},
    )
    env, result = run_round(tmp_path / "a", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    assert not l2["findings"], l2["findings"]
    [drop] = l2["verifier_dropped"]
    assert "the logger is asked for" in drop["reason"]
    assert "the contract names logging" in drop["reason"]

    # (b) major, drop + keep -> kept with the keep's tag and reason.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [
            {"drop": True, "reason": "not real: the logger is asked for"},
            {"tag": "important", "reason": "no acceptance item names logging"},
        ]},
    )
    env, result = run_round(tmp_path / "b", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    kept = _by_text(result.reports["L2"], "AC2-MAJOR")
    assert kept["tag"] == "important"
    # Round 4: the split vote records the keep's reason AND the dissent.
    assert kept["verifier_reason"] == (
        "kept (split vote): no acceptance item names logging | "
        "dissent: not real: the logger is asked for")

    # (c) major, drop + invalid vote -> kept, important, the record
    # names why the second session's vote did not count.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [
            {"drop": True, "reason": "not real: the logger is asked for"},
            "invalid",
        ]},
    )
    env, result = run_round(tmp_path / "c", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["tag"] == "important"
    assert kept["verifier_reason"], "the failure must be named on the finding"
    assert "verifier session failed" in kept["verifier_reason"]

    # (d) a unanimous major (every seat reported it), drop + drop ->
    # the unanimous rule refuses: kept, override recorded.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(major, declared="pass"),
        script={"AC2-MAJOR": [
            {"drop": True, "reason": "not real: the logger is asked for"},
            {"drop": True, "reason": "not real: logging is in the contract"},
        ]},
    )
    env, result = run_round(tmp_path / "d", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["tag"] == "important"
    [drop] = l2["verifier_dropped"]
    assert drop["reason"].startswith("override_refused")

    # (e) a minor follows its single vote: one drop drops it.
    minor = _finding("AC2-minor rename helper _do to _render",
                     severity="minor")
    seats = VoteSeats(
        conformance=_verdict(minor, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-minor": [{"drop": True,
                               "reason": "style-only: the linter owns it"}]},
    )
    env, result = run_round(tmp_path / "e", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    assert not any("AC2-minor" in f["text"] for f in l2["findings"])
    assert l2["verifier_dropped"][0]["reason"] == ("style-only: "
                                                   "the linter owns it")

    # (f) a surviving sibling never blocks and reaches the follow-up:
    # seat 2 flags an old site on an untouched file; both verifier
    # sessions keep it; the round passes and the draft carries it.
    flagged = _sibling("AC2-OLD-SITE whitespace-only id accepted here too")
    seats = VoteSeats(conformance=_verdict(declared="pass"),
                      hunt=_verdict(flagged, declared="pass"))
    env, result = run_round(tmp_path / "f", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    assert l2["verdict"] == "pass"
    kept = _by_text(l2, "AC2-OLD-SITE")
    assert kept["sibling"] is True
    draft = followup(env)
    assert draft is not None
    assert any("AC2-OLD-SITE" in c["text"] for c in draft["acceptance"])


# --------------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path, monkeypatch):
    """A verifier session that crashes, times out or returns a bad shape
    drops nothing for ITS finding — which survives tagged important with
    the reason on the record — and the other findings' votes stand. A
    quota failure still requeues the task and publishes nothing."""
    major = _finding("AC3-MAJOR the offline row lies: disabled, not unknown",
                     severity="major")
    minor = _finding("AC3-minor rename helper _do to _render",
                     severity="minor")

    for failure in ("crash", "timeout", "invalid"):
        seats = VoteSeats(
            conformance=_verdict(major, minor, declared="pass"),
            hunt=_verdict(declared="pass"),
            script={
                "AC3-MAJOR": [failure],
                "AC3-minor": [{"tag": "nit", "reason": "real, small"}],
            },
        )
        env, result = run_round(tmp_path / failure, monkeypatch, seats)
        assert result.outcome != "pr_opened", (failure, result)
        l2 = result.reports["L2"]
        # The failed session's finding survives, important, reason named.
        kept = _by_text(l2, "AC3-MAJOR")
        assert kept["tag"] == "important", failure
        assert "verifier session failed" in kept["verifier_reason"], failure
        # The other finding's vote is unaffected: kept with ITS tag.
        other = _by_text(l2, "AC3-minor")
        assert other["tag"] == "nit", failure
        assert other["verifier_reason"] == "real, small", failure
        # The report names the failing session's finding.
        assert l2.get("verifier_error") and kept["id"] in l2["verifier_error"]
        # And for a session that did not complete (crash, timeout — not
        # the completed-but-invalid shape, whose reason rides the finding
        # and verifier_error alone): the round's own record says it, the
        # note naming the finding the dead session belonged to.
        if failure in ("crash", "timeout"):
            from factory.progress import ProgressLog
            entries = ProgressLog.for_task(
                env.root, "fixture", "T-1").entries()
            lines = [line for entry in entries
                     for line in (entry.get("findings") or [])]
            assert any(
                f"the verifier session for {kept['id']} ended" in line
                for line in lines), (failure, lines)

    # A quota failure on one verifier session: the run aborts on quota,
    # requeues and publishes nothing.
    seats = VoteSeats(
        conformance=_verdict(major, minor, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC3-MAJOR": ["quota"]},
    )
    env, result = run_round(tmp_path / "quota", monkeypatch, seats)
    assert result.outcome == "quota_exhausted", result
    assert env.task()["status"] == "ready"
    assert not env.prs, "a quota-aborted round must publish nothing"


# --------------------------------------------------------------------- AC-4 --


class _FakeTime:
    """A monotonic clock the scripted seats flip: once ``step`` is set,
    every monotonic() call advances it, so the wall-clock budget reads
    as spent wherever the parent next measures it."""

    def __init__(self):
        self.step = 0.0
        self._now = 1000.0

    def monotonic(self) -> float:
        self._now += self.step
        return self._now


def test_ac_4(tmp_path: Path, monkeypatch):
    """The fan-out is capped and queued beyond the cap; a run whose wall
    clock cannot fund the verifier pass escalates budget_exhausted and
    publishes nothing; eval and run send the same per-finding prompts;
    prompt_versions carries the verifier hash."""
    from factory import orchestrator
    from factory.prompts import assemble_verifier_prompt, prompt_hash

    # 12 minor findings, the default cap 6: every session runs, never
    # more than 6 at once (the pause makes one wave's sessions genuinely
    # overlap; a microsecond stub would finish before its siblings
    # start and the in-flight peak would read 1 whatever the cap).
    minors = [_finding(f"AC4-nit-{i}: wording of message {i}",
                       severity="minor") for i in range(1, 13)]
    seats = VoteSeats(conformance=_verdict(*minors, declared="pass"),
                      hunt=_verdict(declared="pass"), pause=0.05)
    env, result = run_round(tmp_path / "waves", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    assert seats.verifier_call_count() == 12
    assert seats.max_inflight == 6, (
        f"12 sessions at cap 6 ran {seats.max_inflight} at once")

    # The cap is a profile value: verification.parallel narrows it.
    seats3 = VoteSeats(conformance=_verdict(*minors, declared="pass"),
                       hunt=_verdict(declared="pass"), pause=0.05)
    env3, _ = run_round(tmp_path / "cap3", monkeypatch, seats3,
                        profile_extra={"verification": {"parallel": 3}})
    assert seats3.verifier_call_count() == 12
    assert seats3.max_inflight == 3

    # Over budget before the verifier pass: the reviewers burn the wall
    # clock, the verifier never starts, the run escalates — it never
    # publishes unverified.
    fake = _FakeTime()
    monkeypatch.setattr(orchestrator, "time", fake)
    burned = VoteSeats(conformance=_verdict(minors[0], declared="pass"),
                       hunt=_verdict(declared="pass"))

    def burn_on_reviewer(seat, prompt, run_dir, profile, **kwargs):
        record = burned.__call__(seat, prompt, run_dir, profile, **kwargs)
        if seat == "reviewer":
            fake.step = 3600.0
        return record

    env2, result2 = run_round(tmp_path / "spent", monkeypatch,
                              burn_on_reviewer)
    assert result2.outcome == "needs_human", result2
    assert result2.escalation == "budget_exhausted", result2
    assert burned.verifier_call_count() == 0
    attempt = env2.task()["attempts"][-1]
    assert attempt["escalation"] == "budget_exhausted"
    assert "verifier" in attempt["note"]
    # Nothing publishes unverified: any PR the escalation kept is a
    # DRAFT escalation PR (F-72's keep-work), never a ready publish.
    assert all(pr.draft for pr in env2.prs), env2.prs

    # Over budget BETWEEN waves: wave 1 runs, the clock burns, wave 2
    # refuses — budget_exhausted, nothing published, never a partial
    # verify that silently drops the unverified findings.
    fake2 = _FakeTime()
    monkeypatch.setattr(orchestrator, "time", fake2)
    mid = VoteSeats(conformance=_verdict(*minors, declared="pass"),
                    hunt=_verdict(declared="pass"))

    def burn_after_wave1(number: int) -> None:
        if number == 6:  # the last session of wave 1 has started
            fake2.step = 3600.0

    mid.on_verifier_call = burn_after_wave1
    env4, result4 = run_round(tmp_path / "mid-wave", monkeypatch, mid)
    assert result4.outcome == "needs_human", result4
    assert result4.escalation == "budget_exhausted", result4
    assert mid.verifier_call_count() == 6
    assert all(pr.draft for pr in env4.prs), env4.prs
    # the real clock back: the parity arm below must not inherit the
    # burned one (its pool admissions would all refuse).
    monkeypatch.setattr(orchestrator, "time", __import__("time"))

    # Parity (F-48): the eval's verifier fan-out sends the same
    # per-finding prompts the run sent, and the attempt's
    # prompt_versions carries the verifier hash.
    attempt = env.task()["attempts"][-1]
    from factory.contract import load_profile
    from factory.prompts import prompt_versions

    loaded = load_profile(env.queue_path.parent / "project.yaml")
    assert attempt["prompt_versions"] == prompt_versions(loaded)
    assert attempt["prompt_versions"]["verifier"] == prompt_hash(
        assemble_verifier_prompt(loaded))

    from factory import eval as eval_module

    head = _branch_head(env.repo)
    eval_seats = VoteSeats(conformance=_verdict(*minors, declared="pass"),
                           hunt=_verdict(declared="pass"))
    report, _ = eval_module.run_l2_review(
        env.repo, loaded, env.base_sha, head, env.task(),
        session_runner=eval_seats, reviewers=2)
    assert report is not None
    assert sorted(eval_seats.verifier_prompts) == sorted(
        seats.verifier_prompts), (
        "the eval and the run must send the same per-finding prompts")


def _branch_head(repo: Path) -> str:
    """A head on the fixture repo: base plus the green implementation
    (the same content the scripted implementer commits — and ONLY that:
    the run also rewrites .factory/tasks.json in the repo, which must
    not ride the eval's diff)."""
    from _support.f72 import IMPL_GOOD, TEST_SOURCE

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("checkout", "-q", "-b", "f96-eval")
    (repo / "impl.py").write_text(IMPL_GOOD)
    (repo / "tests" / "test_feature.py").write_text(TEST_SOURCE)
    git("add", "impl.py", "tests/test_feature.py")
    git("-c", "user.email=f@x", "-c", "user.name=F", "commit", "-qm", "head")
    return git("rev-parse", "HEAD")


# --------------------------------------------------------------------- AC-5 --


class _FixRound(VoteSeats):
    """Round 1's seats both report ``first``. In later rounds the round's
    own review (the first ``reviewers`` reviewer calls after the
    implementer) reports the per-lens ``later_conformance`` /
    ``later_hunt`` scripts, and every other reviewer call — the F-41
    final review that follows a PASSING fix round, above all — is the
    base clean pass, so a finding that only the fix round's delta review
    reported lives or dies with THAT review, never a re-report by the
    final."""

    def __init__(self, first, later_conformance=None, later_hunt=None,
                 **kw):
        super().__init__(**kw)
        self.first = first
        self.later_conformance = later_conformance
        self.later_hunt = later_hunt
        self._round_review_calls = 0

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            with self.lock:
                self._round_review_calls = 0
        record = super().__call__(seat, prompt, run_dir, profile, **kwargs)
        if seat != "reviewer":
            return record
        with self.lock:
            self._round_review_calls += 1
            call = self._round_review_calls
            rounds = len(self.implementer_prompts())
        if rounds == 1:
            record.result = self.first  # round 1's review
        elif call <= 2:
            # the fix round's own review: the per-lens scripts
            hunt = HUNT_MARK in (kwargs.get("system_prompt") or "")
            script = self.later_hunt if hunt else self.later_conformance
            if script is not None:
                record.result = script
        # call > 2 in a later round: the final review — the base clean
        # pass the parent stub already returned.
        return record


TWO_ATTEMPTS = {"size": "S", "max_attempts": 2, "max_turns": 40,
                "wall_clock_min": 60}


def test_ac_5(tmp_path: Path, monkeypatch):
    """Every seat draws the same sibling line: the shared rule sits in
    the base prompt (once, for every seat count), the fix-round paragraph
    and the FAIL rule use it, a parent-ruled sibling in a fix round is
    never dropped as out of delta and reaches the follow-up, an unflagged
    out-of-delta finding still needs missed_earlier, a caller break
    still blocks, and the implementer prompt stops claiming the reviewer
    reads its summary."""
    from factory.prompts import (
        assemble_implementer_prompt,
        assemble_reviewer_prompt,
        reviewer_seat_prompts,
    )

    profile = {"name": "p", "repo": {"path": str(tmp_path)},
               "paths": {"protected": [".factory/**"]}}

    # The shared rule: in the BASE prompt, exactly once, for every seat
    # count (the lens adds the hunt, never a second copy of the rule).
    assert RULE_PIN in assemble_reviewer_prompt(profile)
    for count in (1, 2, 3):
        for prompt in reviewer_seat_prompts(profile, count):
            assert prompt.count(RULE_PIN) == 1, (
                f"the shared rule must appear exactly once, seat of "
                f"{count}: {prompt.count(RULE_PIN)}")

    # The implementer prompt no longer claims the reviewer reads the
    # summary — the reviewer never sees it.
    impl = assemble_implementer_prompt(profile)
    assert IMPL_SUMMARY_CLAIM not in impl
    assert "summary" in impl  # the instruction itself stays

    # A parent-ruled sibling in a FIX ROUND, flagged, no missed_earlier:
    # the round passes, the finding rides the follow-up, and nothing is
    # dropped as out of delta.
    in_diff = _finding("AC5-IN-DIFF the fix is wrong once more",
                       severity="major")
    old_site = _sibling("AC5-OLD-SITE same check missing here")
    seats = _FixRound(
        _verdict(in_diff, declared="fail"),
        later_conformance=_verdict(old_site, declared="pass"),
        later_hunt=_verdict(declared="pass"),
    )
    env, result = run_round(tmp_path / "flagged", monkeypatch, seats,
                            budget=TWO_ATTEMPTS)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    assert l2["verdict"] == "pass"
    # The sibling survived the fix round's delta review (nothing dropped
    # as out of delta) and rides the follow-up even though the clean
    # final review — authoritative for blocking — never re-reported it.
    delta = result.reports["L2-delta"]
    kept = _by_text(delta, "AC5-OLD-SITE")
    assert kept["sibling"] is True
    assert not delta["dropped"], delta["dropped"]
    draft = followup(env)
    assert draft is not None
    assert any("AC5-OLD-SITE" in c["text"] for c in draft["acceptance"])

    # The same finding UNFLAGGED, with missed_earlier: F-23's rule
    # unchanged — it stands and blocks the round.
    justified = _finding("AC5-OLD-SITE same check missing here",
                         severity="major", missed_earlier=(
                             "round one read only the delta; the class "
                             "grep found this site"))
    seats = _FixRound(
        _verdict(in_diff, declared="fail"),
        later_conformance=_verdict(justified, declared="pass"),
        later_hunt=_verdict(declared="pass"),
    )
    _, result = run_round(tmp_path / "unflagged", monkeypatch, seats,
                          budget=TWO_ATTEMPTS)
    assert result.outcome != "pr_opened", result
    assert result.reports["L2"]["verdict"] == "fail"

    # And unflagged WITHOUT missed_earlier, in the same fix round: F-23's
    # drop fires exactly — recorded out of delta, never a silent block,
    # never an exemption the sibling rule did not earn (the round-1 hole
    # by another door: exempting every out-of-diff finding would let
    # this one block a passing round).
    unjustified = _finding("AC5-NO-WHY same check missing here",
                           severity="blocking")
    seats = _FixRound(
        _verdict(in_diff, declared="fail"),
        later_conformance=_verdict(unjustified, declared="pass"),
        later_hunt=_verdict(declared="pass"),
    )
    env2, result2 = run_round(tmp_path / "no-why", monkeypatch, seats,
                              budget=TWO_ATTEMPTS)
    assert result2.outcome == "pr_opened", result2
    delta2 = result2.reports["L2-delta"]
    [drop] = delta2["dropped"]
    assert drop["reason"] == "out_of_delta_without_missed_earlier"
    assert "AC5-NO-WHY" in drop["finding"]["text"]

    # R6 (the reviewer's mutation survivor, round 4): the flag ALONE is
    # not enough — paired with the positive arm above. A fix round
    # whose delta touched only impl.py, on a diff that ALSO changed
    # tests/test_feature.py: a finding on that in-diff file carries the
    # reviewer's sibling claim but the parent rules it NOT a sibling
    # (the file is inside the diff), so F-23's out-of-delta drop fires
    # exactly — a bare flag may not re-litigate settled code.
    from factory.l2 import merge_reviews as _mr
    from factory.l2 import parse_verdict as _pv
    from factory.l2 import settle_review_verdict as _srv
    _union = _mr([_pv(_verdict(_sibling(
        "R6 the flag alone must not exempt an in-diff file",
        file="tests/test_feature.py"), declared="pass"))])
    _settled = _srv(
        _union, {}, frozenset({"impl.py"}), delta_mode=True,
        vote=None, reviewers=2,
        # the parent proves NOTHING outside the diff: the claimed file
        # is inside it, so the two-condition ruling is False.
        outside_diff=lambda f: False)
    assert _settled.verdict == "pass"
    [_drop] = _settled.dropped
    assert _drop["reason"] == "out_of_delta_without_missed_earlier"
    assert _drop["finding"]["sibling"] is False

    # A caller break in an untouched file, unflagged, round one: it
    # still blocks — the file alone never makes a sibling (one attempt,
    # so the round's own verdict is the record's last word).
    caller_break = _finding(
        "AC5-CALLER-BREAK the unchanged caller still expects the old shape",
        severity="blocking")
    seats = _FixRound(_verdict(caller_break, declared="fail"))
    _, result = run_round(tmp_path / "caller", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    assert result.reports["L2"]["verdict"] == "fail"

    # The FAIL rule names the sibling exception (prompt-audit #5) and
    # the fix-round paragraph uses the shared rule (prompt-audit #6).
    # (Whitespace-normalised: the prompt hard-wraps.)
    base = assemble_reviewer_prompt(profile)
    fail_rule = " ".join(base.split("## Verdict", 1)[1].split())
    assert "sibling" in fail_rule and "never fails the round" in fail_rule
    fix_round = " ".join(base.split("## Fix rounds", 1)[1]
                         .split("## ", 1)[0].split())
    assert "sibling" in fix_round
    assert "with `\"sibling\": true`" in fix_round


# ------------------------------------------------- round 2 (review fixes) --


def test_repair_resumes_in_the_sessions_own_home(tmp_path: Path,
                                                 monkeypatch):
    """Round-2 finding 1 (the round review): a malformed vote from a
    verifier session in wave 2 gets its F-85 repair turn in THAT
    session's own ``-vN`` settings (wave offset included), never the
    base run settings — the transcript the resume loads lives in the
    per-session scratch HOME."""
    major_a = _finding("R2-A major: the offline row lies", severity="major")
    major_b = _finding("R2-B major: scope creep too", severity="major")
    # Wave cap 2, four sessions ([A, A, B, B]): wave 2 is B's pair —
    # global ordinals 3 and 4. B's FIRST session plays the malformed,
    # resumable vote: its repair must resume via -v3 settings.
    seats = VoteSeats(
        conformance=_verdict(major_a, major_b, declared="fail"),
        hunt=_verdict(declared="pass"),
        script={"R2-B": ["invalid_resumable",
                         {"tag": "important", "reason": "confirmed"}]},
    )
    repairs: list[dict] = []

    class _Capturing:
        def __init__(self, inner):
            self.inner = inner

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            if kwargs.get("resume"):
                repairs.append(kwargs)
            return self.inner(seat, prompt, run_dir, profile, **kwargs)

    env, result = run_round(tmp_path / "round", monkeypatch,
                            _Capturing(seats),
                            profile_extra={"verification": {"parallel": 2}})
    assert seats.verifier_call_count() == 4, seats.verifier_id_lists
    [repair] = repairs
    assert repair["resume"].startswith("resume-sid-")
    # The repair resumes in exactly ITS session's own -vN settings —
    # the fan-out's deterministic spec ordinal (the invalid session is
    # spec 2: [A, A, B, B], so -v3). The stub's sid numbering is
    # lock-order racy; the spec ordinal is not.
    settings = Path(repair["settings_path"])
    assert settings.name.endswith("-v3.json"), settings.name


def test_combine_needs_two_votes_for_a_serious_drop():
    """Round-2 finding 2: the two-drop rule is enforced by the fold
    itself, not trusted from the session plan — a blocking or major
    finding that arrives with ONE drop outcome keeps, tagged important,
    the fold's own record naming the missing second vote."""
    from factory.l2 import L2Finding, combine_finding_votes, parse_verifier_vote

    major = L2Finding(id="U-1", text="m", axis="spec", severity="major")
    one_drop = parse_verifier_vote(json.dumps({"votes": [
        {"id": "U-1", "keep": False, "reason": "not real"}]}), [major])
    combined = combine_finding_votes([major], [(major, one_drop)])
    assert not combined.dropped
    [kept] = combined.kept
    assert kept.tag == "important"
    assert "two verifier drops" in kept.verifier_reason
    assert combined.error and "1 of 2" in combined.error

    # A minor still follows its single vote, and a serious finding with
    # the two drops it needs still drops: the guard is not a blanket
    # refusal.
    minor = L2Finding(id="U-2", text="n", axis="spec", severity="minor")
    minor_drop = parse_verifier_vote(json.dumps({"votes": [
        {"id": "U-2", "keep": False, "reason": "style-only"}]}), [minor])
    combined = combine_finding_votes(
        [minor], [(minor, minor_drop)])
    assert len(combined.dropped) == 1 and not combined.kept

    two_drops = parse_verifier_vote(json.dumps({"votes": [
        {"id": "U-1", "keep": False, "reason": "not real: a"}]}), [major])
    combined = combine_finding_votes(
        [major], [(major, one_drop), (major, two_drops)])
    assert len(combined.dropped) == 1 and not combined.kept


def test_accept_round_repairs_in_the_sessions_own_home(tmp_path: Path):
    """Round-2 finding 1 (the accept path): the same per-session repair
    settings — a wave-2 malformed vote's repair call carries that
    session's own ``-vN`` settings path, through the accept path's own
    fan-out (run_accept_verifier_fanout -> accept_collect_vote)."""
    from factory.orchestrator import run_accept_verifier_fanout

    major_a = _finding("R2C-A major: the row lies", severity="major")
    major_b = _finding("R2C-B major: scope creep", severity="major")
    union = _union_of(major_a, major_b)
    seats = VoteSeats(
        conformance=_verdict(), hunt=_verdict(),
        script={"R2C-B": ["invalid_resumable",
                          {"tag": "important", "reason": "confirmed"}]},
    )
    repairs: list[dict] = []

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if kwargs.get("resume"):
            repairs.append(kwargs)
        return seats(seat, prompt, run_dir, profile, **kwargs)

    base = tmp_path / "settings.json"
    run_accept_verifier_fanout(
        {"id": "T-1"}, "diff text", union.findings,
        runner=runner, run_dir=tmp_path, profile={"verification": {
            "parallel": 2}},
        factory_root=None, protected=[], system_prompt="sys",
        srt_bin_path=None, run_id=None, task_id="T-1", toolkit=None,
        base=base,
    )
    assert seats.verifier_call_count() == 4, seats.verifier_id_lists
    [repair] = repairs
    assert repair["resume"].startswith("resume-sid-")
    # The repair resumes in exactly ITS session's own -vN settings —
    # the fan-out's deterministic spec ordinal (the invalid session is
    # spec 2: [A, A, B, B], so -v3), the same derivation the fan-out
    # named the session by. (The stub's sid numbering is lock-order
    # racy; the spec ordinal is not.)
    assert Path(repair["settings_path"]).name == f"{base.name}-v3.json"


def _union_of(*findings: dict):
    from factory.l2 import merge_reviews, parse_verdict
    return merge_reviews([
        parse_verdict(_verdict(*findings, declared="pass")),
        parse_verdict(_verdict(declared="pass")),
    ])


def test_accept_window_is_one_total_window(tmp_path: Path, monkeypatch):
    """Round-2 finding 3: the accept path's 30 minutes are ONE window
    across every wave — a second wave that no longer fits starves, and
    (finding 4) the returned vote SAYS the fan-out did not finish."""
    from factory import orchestrator
    from factory.orchestrator import run_accept_verifier_fanout

    class _Burn:
        def __init__(self):
            self.step = 0.0
            self._now = 1000.0

        def monotonic(self):
            self._now += self.step
            return self._now

    fake = _Burn()
    monkeypatch.setattr(orchestrator, "time", fake)
    minors = [_finding(f"R2-WIN minor {i}: wording {i}", severity="minor")
              for i in range(1, 4)]  # 3 sessions, sliding cap 2
    # 50 minutes per clock tick from session 1's start: the window
    # (30 min) cannot fund session 2 — a per-wave reading would show 10
    # minutes left and let every wave refill the window; the sliding
    # pool refuses the very next admission.
    seats = VoteSeats(conformance=_verdict(), hunt=_verdict(),
                      on_verifier_call=lambda n: setattr(fake, "step", 3000.0))
    union = _union_of(*minors)
    vote = run_accept_verifier_fanout(
        {"id": "T-1"}, "diff", union.findings,
        runner=seats, run_dir=tmp_path, profile={"verification": {
            "parallel": 2}},
        factory_root=None, protected=[], system_prompt="sys",
        srt_bin_path=None, run_id=None, task_id="T-1", toolkit=None,
        base=tmp_path / "settings.json",
    )
    assert seats.verifier_call_count() == 1, seats.verifier_id_lists
    assert "did not finish" in (vote.error or "")
    assert "before verifier session 2" in vote.error
    assert len(vote.kept) == 3 and not vote.dropped


def test_accept_quota_is_visible_and_keeps_everything(tmp_path: Path):
    """Round-2 finding 4 (the quota arm): a quota-dead verifier session
    on the accept path keeps every finding — with a verifier_error
    naming that verification did not finish, never a silent no-vote."""
    from factory.orchestrator import run_accept_verifier_fanout

    major = _finding("R2-QUOTA major: the row lies", severity="major")
    union = _union_of(major)
    seats = VoteSeats(conformance=_verdict(), hunt=_verdict(),
                      script={"R2-QUOTA": ["quota", "quota"]})
    vote = run_accept_verifier_fanout(
        {"id": "T-1"}, "diff", union.findings,
        runner=seats, run_dir=tmp_path, profile={},
        factory_root=None, protected=[], system_prompt="sys",
        srt_bin_path=None, run_id=None, task_id="T-1", toolkit=None,
        base=tmp_path / "settings.json",
    )
    assert seats.verifier_call_count() >= 1
    assert vote.error and "did not finish" in vote.error
    assert "quota" in vote.error
    assert [f.severity for f in vote.kept] == ["major"]
    assert not vote.dropped


def test_accept_repair_spends_the_same_window(tmp_path: Path, monkeypatch):
    """Round-3 fix: the accept repair draws on the SAME window as the
    waves — a spent window means NO repair call (the invalid-vote
    outcome stands, the finding kept), not a fresh 30 minutes."""
    from factory import orchestrator
    from factory.orchestrator import run_accept_verifier_fanout

    class _Burn:
        def __init__(self):
            self.step = 0.0
            self._now = 1000.0

        def monotonic(self):
            self._now += self.step
            return self._now

    fake = _Burn()
    monkeypatch.setattr(orchestrator, "time", fake)
    minor = _finding("R3-WINDOW minor: rename helper", severity="minor")
    union = _union_of(minor)
    seats = VoteSeats(conformance=_verdict(), hunt=_verdict(),
                      script={"R3-WINDOW": ["invalid_resumable"]})
    repairs: list[dict] = []

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if kwargs.get("resume"):
            repairs.append(kwargs)
        return seats(seat, prompt, run_dir, profile, **kwargs)

    # The clock burns the whole window the moment the one session
    # starts: the wave runs, the repair has nothing left to draw on.
    seats.on_verifier_call = lambda n: setattr(fake, "step", 3000.0)
    vote = run_accept_verifier_fanout(
        {"id": "T-1"}, "diff", union.findings,
        runner=runner, run_dir=tmp_path, profile={},
        factory_root=None, protected=[], system_prompt="sys",
        srt_bin_path=None, run_id=None, task_id="T-1", toolkit=None,
        base=tmp_path / "settings.json",
    )
    assert seats.verifier_call_count() == 1
    assert not repairs, "a spent window must not fund a repair turn"
    # The invalid vote stands: the finding kept important, the fold's
    # error naming the failed session.
    [kept] = vote.kept
    assert kept.tag == "important"
    assert "verifier session failed" in (kept.verifier_reason or "")


# ------------------------------------------------- round 4 (review fixes) --


def test_failed_session_error_and_other_drops_coexist(tmp_path: Path,
                                                      monkeypatch):
    """Round-4 fix 1: verifier_error names the FAILED sessions only —
    it is not "the vote dropped nothing". A crash on one finding and a
    valid drop on another both land: the error AND the drop."""
    major = _finding("R4-ERR major: the offline row lies", severity="major")
    minor = _finding("R4-ERR minor: rename helper", severity="minor")
    seats = VoteSeats(
        conformance=_verdict(major, minor, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={
            "R4-ERR major": ["crash", "crash"],
            "R4-ERR minor": [{"drop": True,
                              "reason": "style-only: the linter owns it"}],
        },
    )
    env, result = run_round(tmp_path, monkeypatch, seats)
    assert result.outcome != "pr_opened", result  # the major stands
    l2 = result.reports["L2"]
    # The failed sessions are NAMED...
    assert l2.get("verifier_error") and "U-1" in l2["verifier_error"]
    # ...and the OTHER finding's drop still stands beside them.
    [drop] = l2["verifier_dropped"]
    assert "style-only" in drop["reason"]


def test_split_vote_records_the_dissent():
    """Round-4 fix 5: a split serious vote (one keep, one drop) keeps
    the keep's tag and reason AND records the dissent — one independent
    session judged the finding not real, and the owner must see that."""
    from factory.l2 import L2Finding, combine_finding_votes, parse_verifier_vote

    major = L2Finding(id="U-1", text="m", axis="spec", severity="major")
    keep = parse_verifier_vote(json.dumps({"votes": [
        {"id": "U-1", "keep": True, "reason": "the claim checks out",
         "tag": "important"}]}), [major])
    drop = parse_verifier_vote(json.dumps({"votes": [
        {"id": "U-1", "keep": False, "reason": "not real: the table exists"}
    ]}), [major])
    combined = combine_finding_votes([major], [(major, keep), (major, drop)])
    [kept] = combined.kept
    assert kept.tag == "important"
    assert "split vote" in kept.verifier_reason
    assert "the claim checks out" in kept.verifier_reason
    assert "not real: the table exists" in kept.verifier_reason
    assert not combined.dropped


def test_sliding_pool_no_barrier(tmp_path: Path, monkeypatch):
    """Round-4 fix 3: the fan-out is a sliding pool, not barrier waves
    — with cap 2, session 3 starts as soon as EITHER earlier session
    frees, while a slow session 1 is still running (a barrier would
    idle a slot waiting for it)."""
    import threading

    release = threading.Event()
    state: dict = {"session1_running": False, "overlap": False}
    order_lock = threading.Lock()
    order: list[int] = []

    class Sliding(VoteSeats):
        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            if seat != "verifier" or kwargs.get("resume"):
                return super().__call__(seat, prompt, run_dir, profile,
                                        **kwargs)
            # own numbering: the slow session blocks BEFORE the seat's
            # counter runs, so the seat's own count is not yet visible.
            with order_lock:
                order.append(len(order) + 1)
                n = order[-1]
            if n == 1:
                state["session1_running"] = True
                release.wait(timeout=5.0)  # slow: outlives session 3's start
                state["session1_running"] = False
            elif n == 3 and state["session1_running"]:
                state["overlap"] = True  # started while 1 still ran
            return super().__call__(seat, prompt, run_dir, profile,
                                    **kwargs)

    minors = [_finding(f"R4-SLIDE minor {i}: wording {i}", severity="minor")
              for i in range(1, 4)]
    seats = Sliding(conformance=_verdict(*minors, declared="pass"),
                    hunt=_verdict(declared="pass"))
    env, result = run_round(tmp_path, monkeypatch, seats,
                            profile_extra={"verification": {"parallel": 2}})
    assert result.outcome == "pr_opened", result
    assert seats.verifier_call_count() == 3
    assert state["overlap"], ("session 3 never started while the slow "
                              "session 1 was still running — barrier waves")
    assert seats.max_inflight <= 2
    release.set()


class _ThreeRounds(VoteSeats):
    """Round-by-round reviewer scripts: ``rounds`` maps the 1-based
    round number to (conformance, hunt) verdict texts; the final review
    is always the base clean pass."""

    def __init__(self, rounds: dict):
        super().__init__()
        self.rounds = rounds
        self._round = 0

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            with self.lock:
                self._round += 1
        record = super().__call__(seat, prompt, run_dir, profile, **kwargs)
        if seat != "reviewer":
            return record
        with self.lock:
            round_no = self._round
        hunt = HUNT_MARK in (kwargs.get("system_prompt") or "")
        script = self.rounds.get(round_no)
        if script is not None:
            text = script[1] if hunt else script[0]
            if text is not None:
                record.result = text
        return record


def test_earlier_rounds_siblings_reach_the_followup(tmp_path: Path,
                                                    monkeypatch):
    """Round-4 fix 2: the goal-level AC-5 hole — a parent-ruled sibling
    kept by an EARLIER round's review, never re-raised later, must
    still reach the follow-up. Three rounds: round 2 keeps the sibling
    and fails on an in-diff major; round 3 and the final review are
    clean; the sibling rides the follow-up from the accumulated pool."""
    in_diff = _finding("R4-SIB-INDIFF the fix is still wrong", severity="major")
    old_site = _sibling("R4-SIB-OLD same check missing here")
    rounds = {
        1: (_verdict(in_diff, declared="fail"), None),
        # round 2: the conformance seat raises the flagged old site
        # (kept, non-blocking) while the hunt seat fails on the in-diff
        # major — the round fails, the sibling never comes back.
        2: (_verdict(old_site, declared="pass"),
            _verdict(in_diff, declared="fail")),
        3: None,  # clean scripts: round 3's own review passes
    }
    seats = _ThreeRounds(rounds)

    class _MovingHead:
        """Each round's implementer commits a DIFFERENT impl body, so
        every round is a real delta round (a fixed body never moves the
        head and the rounds degenerate into repeat/final-review paths
        that never exercise the pool)."""

        def __init__(self, inner):
            self.inner = inner

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            if seat == "implementer":
                record = self.inner(seat, prompt, run_dir, profile,
                                    **kwargs)
                body = IMPL_GOOD + f"# round {self.inner._round}\n"
                (Path(run_dir) / "impl.py").write_text(body)
                return record
            return self.inner(seat, prompt, run_dir, profile, **kwargs)

    env, result = run_round(
        tmp_path, monkeypatch, _MovingHead(seats),
        budget={"size": "S", "max_attempts": 3, "max_turns": 60,
                "wall_clock_min": 90})
    assert result.outcome == "pr_opened", result
    draft = followup(env)
    assert draft is not None
    assert any("R4-SIB-OLD" in c["text"] for c in draft["acceptance"]), (
        "the round-2 sibling never reached the follow-up")


# ------------------------------------------------- round 5 (review fixes) --


def test_session_timeout_is_read_at_admission(tmp_path: Path):
    """Round-5 fix 1: each session's timeout is the window remaining
    AT ITS ADMISSION, not at pool start — a session admitted at minute
    55 of a 60-minute window gets ~5 minutes, never a fresh 60."""
    from factory.orchestrator import run_verifier_fanout

    majors = [_finding(f"R5-TIMEOUT major {i}: scope creep {i}",
                       severity="major") for i in range(1, 3)]
    union = _union_of(*majors)
    captured: list[dict] = []
    seats = VoteSeats(conformance=_verdict(), hunt=_verdict())

    def runner(seat, prompt, run_dir, profile, **kwargs):
        captured.append(kwargs)
        return seats(seat, prompt, run_dir, profile, **kwargs)

    budget = {"left": 60.0}

    def remaining_min() -> float:
        return budget["left"]

    real_vote = seats._vote

    def vote_then_burn(prompt, ordinals, call_no):
        record = real_vote(prompt, ordinals, call_no)
        if call_no == 1:
            budget["left"] = 20.0  # session 1's run burned 40 minutes
        return record

    seats._vote = vote_then_burn
    run_verifier_fanout(
        {"id": "T-1"}, "diff", union.findings,
        runner=runner, run_dir=tmp_path, profile={},
        factory_root=None, protected=[], system_prompt="sys",
        srt_bin_path=None, run_id=None, task_id="T-1", toolkit=None,
        parallel=1, remaining_min=remaining_min,
    )
    timeouts = sorted(kw["timeout_min"] for kw in captured)
    assert len(captured) == 4, [k.get("timeout_min") for k in captured]
    # cap 1, sequential: session 1 is admitted while ~60 remain; the
    # burn fires as its vote plays, so sessions 2-4 are admitted with
    # ~20 left and get ~20 each — never a fresh 60. (Real-clock floats:
    # compared with tolerance.)
    assert all(abs(t - want) < 0.1 for t, want in
               zip(timeouts, [20.0, 20.0, 20.0, 60.0])), timeouts


def test_stale_pool_sibling_is_reruled_at_publish(tmp_path: Path,
                                                  monkeypatch):
    """Round-5 fix 2: the pool's sibling is re-ruled against the FINAL
    base...head diff at publish — a round-2 sibling whose file round 3's
    fix went on to edit is NOT carried (the final review judged that
    file as diff); the never-touched sibling still is."""
    from _support.f72 import _task
    from _support.f75 import CI
    from _support.f76 import Env76
    from _support.f78 import GREEN, seams

    from factory.config import FactoryConfig

    class MP:
        def setattr(self, *a, **k):
            pass

    old_site = _sibling("R5-STALE-OLD same check missing here")
    still_broken = _verdict(_finding("R5 in-diff major still broken",
                                     severity="major"), declared="fail")
    rounds = {
        1: (_verdict(_finding("R5 in-diff major round 1",
                              severity="major"), declared="fail"), None),
        # round 2 KEEPS the flagged sibling (conformance seat) but
        # FAILS on the in-diff major (hunt seat) — so round 3 runs and
        # its fix edits the sibling's file.
        2: (_verdict(old_site, declared="pass"), still_broken),
        3: None,
    }
    seats = _ThreeRounds(rounds)

    class _Round3EditsB:
        """Round 3's implementer edits the sibling's file too: the
        final diff then contains it, so the old sibling is stale. (A
        wrapper class, not an instance __call__ patch — the runner is
        invoked as runner(...) and Python resolves that on the type.)"""

        def __init__(self, inner):
            self.inner = inner

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            record = self.inner(seat, prompt, run_dir, profile, **kwargs)
            if (seat == "implementer" and self.inner._round >= 3):
                target = Path(run_dir) / "tests" / "test_base.py"
                target.write_text("def test_base():\n    assert True\n"
                                  "# touched by the round-3 fix\n")
            return record

    runner = _Round3EditsB(seats)
    task = _task(budget={"size": "S", "max_attempts": 3, "max_turns": 60,
                         "wall_clock_min": 90})
    task["scope"] = {"paths": ["impl.py", "tests/test_feature.py",
                               "tests/test_base.py"]}
    env = Env76(tmp_path, tasks=[task])
    seams(MP(), env)
    result = env.run_once(runner, ci_state=CI(default=GREEN),
                          config=FactoryConfig(reviewers=2))
    assert result.outcome == "pr_opened", result
    draft = followup(env)
    # The stale sibling is gone: with no other findings, NO follow-up
    # draft exists at all; with some, the stale entry must be absent
    # from its criteria. (Without the re-ruling the draft exists and
    # carries R5-STALE-OLD — the mutation row.)
    assert draft is None or not any(
        "R5-STALE-OLD" in c["text"] for c in draft["acceptance"]), (
        "a sibling whose file the final diff changed reached the "
        "follow-up")


def test_on_record_fires_as_each_session_ends(tmp_path: Path):
    """Round-5 fix 4: on_record runs in the worker as each record
    arrives — a fast session 2's record is seen BEFORE slow session
    1's, not after the whole pool returns."""
    import threading

    from factory.orchestrator import run_verifier_fanout

    minors = [_finding("R5-TRACE-SLOW minor: the slow one", severity="minor"),
              _finding("R5-TRACE-FAST minor: the fast one", severity="minor")]
    union = _union_of(*minors)
    seats = VoteSeats(conformance=_verdict(), hunt=_verdict())
    seen: list[str] = []
    seen_lock = threading.Lock()
    release = threading.Event()

    def runner(seat, prompt, run_dir, profile, **kwargs):
        from factory.session import SessionRecord
        if seat == "verifier" and "R5-TRACE-SLOW" in prompt:
            release.wait(timeout=5.0)  # the slow finding-1 session
            return SessionRecord(seat=seat, status="completed",
                                 num_turns=1, model="stub",
                                 result='{"votes": [{"id": "U-1", '
                                        '"keep": true, "reason": "slow", '
                                        '"tag": "nit"}]}')
        record = seats(seat, prompt, run_dir, profile, **kwargs)
        return record

    def on_record(record):
        with seen_lock:
            seen.append(record.result)

    try:
        run_verifier_fanout(
            {"id": "T-1"}, "diff", union.findings,
            runner=runner, run_dir=tmp_path, profile={},
            factory_root=None, protected=[], system_prompt="sys",
            srt_bin_path=None, run_id=None, task_id="T-1", toolkit=None,
            parallel=2, remaining_min=lambda: 30.0,
            on_record=on_record,
        )
    finally:
        release.set()
    assert len(seen) == 2, seen
    # The FIRST record seen is the FAST session's — on_record fires in
    # the worker as records arrive, not after the whole pool returns
    # (the slow session would still be holding its worker).
    first = json.loads(seen[0])["votes"][0]["id"]
    assert first == "U-2", f"on_record waited for the pool: {seen}"


# ------------------------------------------------- round 6 (final review) --


def test_eval_starved_pool_venes_the_review(tmp_path: Path, monkeypatch):
    """Round-6 A: a starved verifier pool voids the EVAL review exactly
    like quota — no verdict is scored over findings that were not all
    voted, and the void's reason is visible on the record."""

    from factory import eval as eval_module
    from factory import orchestrator
    from factory.contract import load_profile
    from factory.session import run_settings_base  # noqa: F401 (shape)

    class _Burn:
        def __init__(self):
            self.step = 0.0
            self._now = 1000.0

        def monotonic(self):
            self._now += self.step
            return self._now

    fake = _Burn()
    monkeypatch.setattr(orchestrator, "time", fake)
    minors = [_finding(f"R6-EVAL minor {i}: wording {i}", severity="minor")
              for i in range(1, 13)]
    seats = VoteSeats(conformance=_verdict(*minors, declared="pass"),
                      hunt=_verdict(declared="pass"),
                      on_verifier_call=lambda n: setattr(fake, "step", 1e9))

    # a real fixture repo + head, as the parity arm builds
    env, _ = _fixture_repo(tmp_path)
    loaded = load_profile(env.queue_path.parent / "project.yaml")
    head = _fixture_head(env.repo)
    why: list[str] = []
    report, _ = eval_module.run_l2_review(
        env.repo, loaded, env.base_sha, head, env.task(),
        session_runner=seats, reviewers=2, void_reason=why)
    assert report is None, "a starved pool must void the review"
    assert why == ["verifier_window_starved"], why
    monkeypatch.setattr(orchestrator, "time", __import__("time"))


def test_eval_pool_never_shrinks_per_session_timeouts(tmp_path: Path):
    """Round-6 A (2): a NORMAL eval pool — 12 minors, parallel 6 —
    verifies every finding, and every session's timeout_min is at least
    the eval's own per-seat budget (SEAT_TIMEOUT_MIN): the window is
    sized SEAT_TIMEOUT_MIN x sessions, so no late session starves or
    runs on a shrunken window."""
    from factory import eval as eval_module
    from factory.contract import load_profile

    captured: list[dict] = []
    minors = [_finding(f"R6-FULL minor {i}: wording {i}", severity="minor")
              for i in range(1, 13)]
    seats = VoteSeats(conformance=_verdict(*minors, declared="pass"),
                      hunt=_verdict(declared="pass"))

    def runner(seat, prompt, run_dir, profile, **kwargs):
        captured.append(kwargs)
        return seats(seat, prompt, run_dir, profile, **kwargs)

    env, _ = _fixture_repo(tmp_path)
    loaded = load_profile(env.queue_path.parent / "project.yaml")
    head = _fixture_head(env.repo)
    report, _ = eval_module.run_l2_review(
        env.repo, loaded, env.base_sha, head, env.task(),
        session_runner=runner, reviewers=2)
    assert report is not None
    assert seats.verifier_call_count() == 12, seats.verifier_id_lists
    from factory.eval import SEAT_TIMEOUT_MIN
    timeouts = [kw["timeout_min"] for kw in captured
                if kw.get("seat") is None or True]
    assert len(timeouts) >= 12
    assert all(t >= SEAT_TIMEOUT_MIN - 1e-9 for t in timeouts), timeouts


def _fixture_repo(tmp_path: Path):
    """A minimal Env76 fixture + a green head, for the eval arms."""
    from _support.f72 import _task
    from _support.f76 import Env76

    env = Env76(tmp_path, tasks=[_task()])
    return env, None


def _fixture_head(repo: Path) -> str:
    from _support.f72 import IMPL_GOOD, TEST_SOURCE

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("checkout", "-q", "-b", "f96-eval6")
    (repo / "impl.py").write_text(IMPL_GOOD)
    (repo / "tests" / "test_feature.py").write_text(TEST_SOURCE)
    git("add", "impl.py", "tests/test_feature.py")
    git("-c", "user.email=f@x", "-c", "user.name=F", "commit", "-qm", "h")
    return git("rev-parse", "HEAD")
