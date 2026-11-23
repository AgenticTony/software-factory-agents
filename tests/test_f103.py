"""F-103 contract tests: a finding blocks only when it can cost production.

F-96's five review rounds ended with two tail rounds — the review is
recall-tuned, so a finding COUNT says nothing about readiness, and yet
nothing in the prompts defines what a severity means: ``reviewer.md``
never says what ``major`` is, and the verifier was forbidden to challenge
one at all. F-103 pins the rubric (A blocks on production cost, B routes
to the PR body and the follow-up, C is capped polish) as ONE shared block
in every seat prompt, and lets the verifier re-rate a ``blocking``/
``major`` finding DOWN — but only with more evidence than keeping needs:
two agreeing verifier votes, each reason quoting the finding's own
scenario, never on a unanimous finding, never on a sibling, and a failed
session always keeps the severity. Red-first against the tree before the
implementation (test_ac_3 pins today's routing and was green from the
start, by design — the AC exists so F-97 cannot erode it).
"""

from __future__ import annotations

from pathlib import Path

from _support.f94 import HUNT_MARK, followup, run_round
from _support.f96 import VoteSeats, _finding, _verdict

# tests/test_base.py is tracked on the fixture's base and never touched
# by the scripted implementer: the canonical "other site".
UNTOUCHED = "tests/test_base.py"
CHANGED = "impl.py"


# ---------------------------------------------------------------- pins ---

# The severity rubric — ONE shared block, the spec's exact table plus the
# scenario rule, defined once in factory/prompts.py as SEVERITY_RUBRIC
# and included byte-for-byte in every reviewer seat prompt (the final
# review's prompt is the same assembled reviewer prompt) and in the
# verifier prompt. Pinned by literal: the test is the sanctioned way the
# text changes.
RUBRIC_PIN = (
    "## The severity rubric\n"
    "\n"
    "Whether a finding blocks depends on what it can cost production, "
    "not on how the review felt. Rate every finding with this table:\n"
    "\n"
    "| Severity | Bucket | Test | Route |\n"
    "|---|---|---|---|\n"
    "| `blocking` | A | Certain on the main path: wrong output, data "
    "loss, an acceptance criterion not met, or a broken boundary "
    "(sandbox, push, budget, approval, secrets, auth, payments, verdict "
    "integrity) | fails the round, fix |\n"
    "| `major` | A | The same costs, reachable under an input or state "
    "the finding names | fails the round, fix |\n"
    "| `minor` | B | Real and wrong, but visible and caught by a human "
    "before harm: a misleading message, a noisy follow-up, a slow path, "
    "a weak test | PR body + F-82 follow-up, never blocks |\n"
    "| `note` | C | Polish: naming, comments, structure, style | PR body "
    "within the nit budget, else cut |\n"
    "\n"
    "A `blocking` or `major` finding states its failure scenario in "
    "`text`: the input or state, and the wrong result. \"Could be "
    "cleaner\" is never A.\n"
)

# The same block WITHOUT the heading, as VALIDATOR-SPEC's L2 section
# carries it (the doc has its own structure around the block).
DOC_RUBRIC_PIN = RUBRIC_PIN[len("## The severity rubric\n\n"):]

# The re-rate rule, as VALIDATOR-SPEC's L2 section states it.
DOC_RERATE_PIN = (
    "**The re-rate rule (F-103).** The verifier may re-rate a "
    "`blocking` or `major` finding DOWN — never up — to `minor` or "
    "`note`, and only when BOTH its verifier sessions vote the same "
    "re-rate, each reason quoting the finding's own scenario (from its "
    "`text`) and showing why it reaches no A cost. A failed session, a "
    "single vote, a vote whose reason quotes no scenario, or a finding "
    "every reviewer reported (the unanimous rule) keeps the original "
    "severity, the refusal recorded; a sibling's severity is never "
    "touched. Every applied re-rate lands in the stored report and the "
    "attempt record's `review_rerates` with both reasons."
)

# The finding every re-rate scenario is played against. Its text states
# the failure scenario the rubric demands of an A finding — the re-rate
# reasons quote it, and the no-scenario attack quotes nothing.
MAJOR = "AC2-MAJOR on an empty cart the checkout total renders 0 instead of the sum"
SCENARIO_QUOTE = '"on an empty cart"'

# The one serious re-rate that the rule allows: two agreeing votes, each
# reason quoting the scenario it is judging.
AGREE = [
    {"tag": "important",
     "reason": f"I checked the client guard; {SCENARIO_QUOTE} cannot "
               "reach this code, so no A cost is reachable",
     "rerate": "minor"},
    {"tag": "important",
     "reason": f"the diff adds the guard; {SCENARIO_QUOTE} never reaches "
               "the server, no A cost",
     "rerate": "minor"},
]


def _sibling(text: str, file: str = UNTOUCHED, severity: str = "major",
             **extra) -> dict:
    """A finding the reviewer FLAGS as a sibling site — F-94's strict
    claim, so the parent's outside-the-diff proof can rule on it."""
    extra.setdefault("sibling", True)
    return _finding(text=text, file=file, severity=severity, axis="spec",
                    **extra)


def _by_text(l2: dict, marker: str) -> dict:
    return next(f for f in l2["findings"] if marker in f["text"])


# ---------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """The rubric block is ONE shared text, byte-for-byte in every
    reviewer seat prompt (both lenses, and the one-seat fan-out that
    carries both — the final review runs on the same assembled reviewer
    prompts), the verifier prompt, and exactly once in each. The
    prompts.py constant IS the pin."""
    import factory.prompts

    assert factory.prompts.SEVERITY_RUBRIC == RUBRIC_PIN, (
        "factory.prompts.SEVERITY_RUBRIC drifted from the pin — the pin "
        "is the sanctioned way the shared block changes")

    profile = {
        "repo": {"path": str(tmp_path)},
        "standards": ["standards.md"],
    }
    (tmp_path / "standards.md").write_text(
        "# Style\n\nNo prints in library code.\n")

    reviewer = factory.prompts.assemble_reviewer_prompt(profile)
    assert RUBRIC_PIN in reviewer, "rubric block absent from reviewer prompt"
    assert reviewer.count(RUBRIC_PIN) == 1, "the block must appear once"
    # The block sits with the review policy, above the project's
    # standards section (assembly appends the block, then standards).
    assert reviewer.index(RUBRIC_PIN) < reviewer.index("## Project standards")

    seats = factory.prompts.reviewer_seat_prompts(profile, 2)
    assert len(seats) == 2 and seats[0] != seats[1]
    for number, seat in enumerate(seats, start=1):
        assert RUBRIC_PIN in seat, (
            f"rubric block absent from reviewer seat {number}'s prompt")
        assert seat.count(RUBRIC_PIN) == 1
    # The lens still rides the base: the hunt seat differs by the lens
    # block, never by the rubric.
    assert HUNT_MARK in seats[1] and HUNT_MARK not in seats[0]

    one_seat = factory.prompts.reviewer_seat_prompts(profile, 1)
    assert RUBRIC_PIN in one_seat[0], (
        "rubric block absent from the one-seat fan-out's prompt")

    verifier = factory.prompts.assemble_verifier_prompt(profile)
    assert RUBRIC_PIN in verifier, "rubric block absent from verifier prompt"
    assert verifier.count(RUBRIC_PIN) == 1

    # The same holds without a profile (no standards — the baseline-only
    # assembly branch): every seat still carries the block.
    assert RUBRIC_PIN in factory.prompts.assemble_reviewer_prompt()
    assert RUBRIC_PIN in factory.prompts.assemble_verifier_prompt()
    for seat in factory.prompts.reviewer_seat_prompts(None, 2):
        assert RUBRIC_PIN in seat

    # The hashes that reach the attempt record hash the text the block
    # lives in — a prompt whose hash is recorded carries the block.
    versions = factory.prompts.prompt_versions(profile, reviewers=2)
    assert versions["reviewer-1"] == factory.prompts.prompt_hash(seats[0])
    assert versions["verifier"] == factory.prompts.prompt_hash(verifier)


# ---------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path: Path, monkeypatch):
    """A verifier may re-rate a blocking/major finding DOWN, and only
    down, with more evidence than keeping needs: two agreeing votes, each
    reason quoting the finding's own scenario; one vote, a failed
    session, a reason quoting no scenario, an up-re-rate or an
    empty-reason vote never moves the severity; a unanimous finding and
    a sibling cannot be re-rated at all. Every outcome lands in the
    report's and the attempt record's rerate ledger with both reasons."""
    major = _finding(MAJOR, severity="major")

    # (a) ONE re-rate vote: the other session keeps — the severity is
    # unchanged, the round fails, the attempt is recorded as dissent.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [
            AGREE[0],
            {"tag": "important", "reason": "keep: no acceptance item is broken"},
        ]},
    )
    env, result = run_round(tmp_path / "a", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["severity"] == "major", kept
    assert "rerate not applied" in kept["verifier_reason"], kept
    assert not l2.get("rerated"), l2.get("rerated")

    # (b) TWO agreeing re-rate votes on a major one seat reported: the
    # severity becomes minor, the round passes and publishes, the finding
    # reaches the PR body's open-findings section and the F-82 draft,
    # and the ledger records the re-rate with BOTH reasons.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": AGREE},
    )
    env, result = run_round(tmp_path / "b", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    assert l2["verdict"] == "pass", l2["verdict"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["severity"] == "minor", kept
    [entry] = l2["rerated"]
    assert entry["id"] == kept["id"]
    assert entry["from"] == "major" and entry["to"] == "minor"
    assert len(entry["reasons"]) == 2, entry
    assert "client guard" in entry["reasons"][0]
    assert "adds the guard" in entry["reasons"][1]
    assert "refused" not in entry
    # The attempt record carries the same ledger (the schema knows it —
    # a shape violation would refuse the whole queue at load).
    attempt = env.task("T-1")["attempts"][-1]
    assert attempt["review_rerates"] == [entry], attempt.get("review_rerates")
    # The rerated finding rides the PR body's open-findings section and
    # the follow-up draft as the minor it now is.
    body = env.prs[0].body
    assert "Review findings left open" in body
    assert "AC2-MAJOR" in body
    draft = followup(env)
    assert draft is not None
    assert any("AC2-MAJOR" in c["text"] for c in draft["acceptance"])
    # The block sits in the verifier's SYSTEM prompt — above the
    # untrusted diff and finding text, which ride the user prompt alone.
    assert seats.verifier_system_prompts and RUBRIC_PIN in \
        seats.verifier_system_prompts[0]
    assert RUBRIC_PIN not in seats.verifier_prompts[0]
    assert "AC2-MAJOR" in seats.verifier_prompts[0]

    # (c) A UNANIMOUS major (every seat reported it): even two agreeing
    # re-rate votes are refused — recorded, severity stands, round fails.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(major, declared="pass"),
        script={"AC2-MAJOR": AGREE},
    )
    env, result = run_round(tmp_path / "c", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["severity"] == "major", kept
    [entry] = l2["rerated"]
    assert entry["refused"] == "unanimous", entry
    assert entry["from"] == "major" and entry["to"] == "minor"
    assert len(entry["reasons"]) == 2
    assert "rerate to minor refused" in kept["verifier_reason"], kept

    # (d) A failed second session: its absent vote is not a re-rate —
    # the severity stands, the round fails.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [AGREE[0], "crash"]},
    )
    env, result = run_round(tmp_path / "d", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["severity"] == "major", kept
    assert "verifier session failed" in kept["verifier_reason"], kept
    assert not l2.get("rerated"), l2.get("rerated")

    # (e) A re-rate reason that quotes NO scenario counts as a keep:
    # both sessions vote re-rate but engage with nothing.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [
            {"tag": "important", "reason": "I disagree, this feels minor "
                                           "to me", "rerate": "minor"},
            {"tag": "important", "reason": "overrated, fine by me",
             "rerate": "minor"},
        ]},
    )
    env, result = run_round(tmp_path / "e", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["severity"] == "major", kept
    assert "quotes no scenario" in kept["verifier_reason"], kept
    assert not l2.get("rerated"), l2.get("rerated")

    # (f) A sibling's severity is untouched: a flagged old site, both
    # sessions voting the same re-rate — refused, recorded, and the
    # sibling still never blocks (the round publishes).
    flagged = _sibling("AC2-OLD-SITE on an empty cart the total renders 0 here too")
    seats = VoteSeats(
        conformance=_verdict(declared="pass"),
        hunt=_verdict(flagged, declared="pass"),
        script={"AC2-OLD-SITE": [
            {"tag": "nit",
             "reason": f"the old site is guarded upstream; {SCENARIO_QUOTE} "
                       "cannot reach it", "rerate": "note"},
            {"tag": "nit",
             "reason": f"upstream validates it; {SCENARIO_QUOTE} is caught "
                       "there", "rerate": "note"},
        ]},
    )
    env, result = run_round(tmp_path / "f", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-OLD-SITE")
    assert kept["sibling"] is True
    assert kept["severity"] == "major", (
        "a sibling's severity is never touched")
    [entry] = l2["rerated"]
    assert entry["refused"] == "sibling", entry
    assert entry["to"] == "note"
    draft = followup(env)
    assert draft is not None
    assert any("AC2-OLD-SITE" in c["text"] for c in draft["acceptance"])

    # (g) Re-rating UP is never the verifier's: a rerate to "major" is a
    # protocol violation — the vote is invalid, the finding keeps its
    # severity, the round fails.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [
            {"tag": "important",
             "reason": f"it is worse than reported; {SCENARIO_QUOTE} also "
                       "corrupts totals", "rerate": "major"},
            {"tag": "important",
             "reason": f"agreed it is worse; {SCENARIO_QUOTE} repeats",
             "rerate": "major"},
        ]},
    )
    env, result = run_round(tmp_path / "g", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["severity"] == "major", kept
    assert "verifier session failed" in kept["verifier_reason"], kept
    assert "re-rate down to minor or note" in l2["verifier_error"]

    # (h) A re-rate with an EMPTY reason is not a vote at all: the
    # severity stands, the round fails.
    seats = VoteSeats(
        conformance=_verdict(major, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-MAJOR": [
            {"tag": "important", "reason": "", "rerate": "minor"},
            {"tag": "important", "reason": "keep: no acceptance item is "
                                           "broken"},
        ]},
    )
    env, result = run_round(tmp_path / "h", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-MAJOR")
    assert kept["severity"] == "major", kept
    assert "verifier session failed" in kept["verifier_reason"], kept

    # (i) A re-rate of a NON-serious finding is a protocol violation:
    # a minor has no lower severity to be voted to.
    minor = _finding("AC2-minor rename helper _do to _render",
                     severity="minor")
    seats = VoteSeats(
        conformance=_verdict(minor, declared="pass"),
        hunt=_verdict(declared="pass"),
        script={"AC2-minor": [
            {"tag": "nit", "reason": "small, but re-rate it anyway",
             "rerate": "note"},
        ]},
    )
    env, result = run_round(tmp_path / "i", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    kept = _by_text(l2, "AC2-minor")
    assert kept["severity"] == "minor", kept
    assert "verifier session failed" in kept["verifier_reason"], kept
    assert "only a blocking or major finding" in l2["verifier_error"]


# ---------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path, monkeypatch):
    """The stop rule is pinned: a round whose surviving findings are all
    `minor`/`note` passes and publishes; the minors reach the PR body's
    open-findings section and the F-82 draft; a `note` beyond the nit
    budget is cut and recorded. This AC pins today's behaviour so F-97
    cannot erode it."""
    f1 = _finding("AC3-minor-one the empty state says disabled instead "
                  "of unknown", severity="minor")
    f2 = _finding("AC3-minor-two the export path re-reads the file twice",
                  severity="minor", file="other1.py")
    f3 = _finding("AC3-note-one rename helper _do to _render",
                  severity="note", file="other2.py")
    f4 = _finding("AC3-note-two the docstring misnames the helper",
                  severity="note", file="other3.py")
    seats = VoteSeats(
        conformance=_verdict(f1, f2, f3, f4, declared="pass"),
        hunt=_verdict(declared="pass"),
    )
    env, result = run_round(tmp_path, monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    assert l2["verdict"] == "pass", l2["verdict"]
    survivors = [f["text"] for f in l2["findings"]]
    assert len(survivors) == 3, survivors
    [cut] = l2["verifier_dropped"]
    assert cut["reason"] == "nit_budget"
    assert "AC3-note-two" in cut["finding"]["text"]
    # The minors land in the PR body and the draft; the cut note in neither.
    body = env.prs[0].body
    assert "Review findings left open" in body
    assert "AC3-minor-one" in body and "AC3-minor-two" in body
    assert "AC3-note-one" in body
    assert "AC3-note-two" not in body
    draft = followup(env)
    assert draft is not None
    texts = [c["text"] for c in draft["acceptance"]]
    assert len(texts) == 3, texts
    assert any("AC3-minor-one" in t for t in texts)
    assert not any("AC3-note-two" in t for t in texts)
    # The cut is recorded on the attempt record, not only in memory.
    drops = env.task("T-1")["attempts"][-1]["review_drops"]
    assert any(d["reason"] == "nit_budget" for d in drops), drops


# ---------------------------------------------------------------- AC-4 --


def test_ac_4():
    """The docs say it: VALIDATOR-SPEC's L2 section carries the same
    rubric block and the re-rate rule; README and BUILD-ORDER §F-41 carry
    no "rate it major" statement the rubric replaces."""
    spec = (Path(__file__).resolve().parent.parent / "VALIDATOR-SPEC.md"
            ).read_text()
    assert DOC_RUBRIC_PIN in spec, (
        "VALIDATOR-SPEC's L2 section does not carry the rubric block")
    assert DOC_RERATE_PIN in spec, (
        "VALIDATOR-SPEC's L2 section does not carry the re-rate rule")
    root = Path(__file__).resolve().parent.parent
    for doc in ("README.md", "BUILD-ORDER.md"):
        text = (root / doc).read_text()
        assert "rate it major" not in text, (
            f"{doc} still carries a 'rate it major' style statement the "
            "rubric replaces")
