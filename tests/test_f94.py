"""F-94 contract tests: reviews look for the same defect elsewhere.

JobFinderOS run 4 (#176) fixed a whitespace-only email at the one site
its contract named; three more sites of the same defect shipped, and
neither the contract review nor L2 flagged them. F-94 adds a sixth
contract-review lens (the defect class), gives the second reviewer seat
a defect-hunt lens, and makes the PARENT decide — from the diff's own
file list, never from the model's severity or flag — that a finding
outside the diff cannot block: it reaches F-82's follow-up draft
instead. Red-first against the tree before the implementation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from _support.f94 import (
    HUNT_MARK,
    HUNT_PIN,
    LensSeats,
    _finding,
    _verdict,
    followup,
    run_round,
)

# The sixth lens, by literal (the spec's exact words).
DEFECT_CLASS_PIN = (
    "For each criterion that fixes a defect, name the defect class in one "
    "line and search the clone for other sites of the same class. A "
    "criterion that fixes one site of a class that occurs elsewhere is a "
    "finding: name each other site as file:line."
)

# tests/test_base.py is tracked on the fixture's base and never touched
# by the scripted implementer (it writes impl.py and
# tests/test_feature.py): the canonical "other site".
UNTOUCHED = "tests/test_base.py"
CHANGED = "impl.py"


def _sibling(text: str, file: str = UNTOUCHED, severity: str = "major",
             **extra) -> dict:
    """A finding the reviewer FLAGS as a sibling site (``sibling: true``
    unless the caller overrides it) — a sibling needs the flag AND a file
    the parent proves outside the diff."""
    extra.setdefault("sibling", True)
    return _finding(text=text, file=file, severity=severity, axis="spec",
                    **extra)


# --------------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """The contract review names the defect class: a sixth lens,
    ``defect_class``, pinned by literal, in the built prompt under a
    six-lens heading, and a scripted reviewer output using it parses."""
    from factory import contractreview
    from factory.contractreview import LENSES, build_prompt
    from factory.l2 import CONTRACT_REVIEW_LENSES, parse_contract_review

    assert LENSES["defect_class"] == DEFECT_CLASS_PIN
    assert tuple(LENSES)[-1] == "defect_class"
    assert CONTRACT_REVIEW_LENSES == tuple(LENSES)
    assert len(LENSES) == 6

    task = {"id": "T-1", "title": "fix the email check",
            "acceptance": [{"id": "AC-1", "text": "strip the email",
                            "check": {"type": "manual"}}]}
    source = contractreview._Source(path="docs/T-1.md", anchor=None,
                                    text="fix it", truncated=False,
                                    section_found=True)
    prompt = build_prompt(task, source=source, profile={}, tracked=["a.py"],
                          tracked_total=1)
    lines = prompt.splitlines()
    assert "## The six lenses" in lines
    assert "## The five lenses" not in lines
    assert f"- defect_class: {DEFECT_CLASS_PIN}" in lines
    assert '"defect_class"' in prompt  # the output protocol names it
    assert "five lenses" not in prompt

    output = json.dumps({"findings": [{
        "id": "CR-1", "lens": "defect_class", "severity": "major",
        "text": "AC-1 fixes one site; dedupe.py:236 has the same check",
        "criterion": "AC-1", "suggested_change": None}]})
    outcome = parse_contract_review(output, ["AC-1"])
    assert outcome.error is None, outcome.error
    assert [f["lens"] for f in outcome.findings] == ["defect_class"]


# --------------------------------------------------------------------- AC-2 --


def _profile(tmp_path: Path) -> dict:
    return {"name": "p", "repo": {"path": str(tmp_path)},
            "paths": {"protected": [".factory/**"]}}


def test_ac_2(tmp_path: Path, monkeypatch):
    """Seat lenses: seat 1 conformance (today's prompt), seat 2 the
    defect hunt (the same base plus the lens block); odd/even beyond 2;
    one seat carries both; prompt_versions hashes each seat's own
    prompt; the run, the eval and a retried seat 2 all use the same
    assignment."""
    from factory import eval as eval_module
    from factory import orchestrator
    from factory.prompts import (
        assemble_reviewer_prompt,
        prompt_hash,
        prompt_versions,
        reviewer_seat_prompts,
    )

    profile = _profile(tmp_path)
    base = assemble_reviewer_prompt(profile)
    hunt = base + "\n\n" + HUNT_PIN
    assert HUNT_MARK not in base
    # Seat 1 is today's prompt; seat 2 differs ONLY by the lens block.
    assert reviewer_seat_prompts(profile, 2) == [base, hunt]
    assert reviewer_seat_prompts(profile, 3) == [base, hunt, base]
    assert reviewer_seat_prompts(profile, 4) == [base, hunt, base, hunt]
    # One seat carries both: the base (conformance) plus the hunt.
    assert reviewer_seat_prompts(profile, 1) == [hunt]
    versions = prompt_versions(profile, reviewers=3)
    assert versions["reviewer-1"] == prompt_hash(base)
    assert versions["reviewer-2"] == prompt_hash(hunt)
    assert versions["reviewer-3"] == prompt_hash(base)
    assert prompt_versions(profile, reviewers=1)["reviewer-1"] == \
        prompt_hash(hunt)

    # Seat for seat: record every reviewer fan-out the run and the eval
    # make (the spec order IS the seat number).
    fanouts: list[tuple[str, list]] = []

    def recording(where, real):
        def wrapper(specs, *args, **kwargs):
            if specs and specs[0][0] == "reviewer":
                fanouts.append((where, list(
                    kwargs.get("seat_system_prompts")
                    or [kwargs.get("system_prompt")] * len(specs))))
            return real(specs, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(orchestrator, "run_sessions_parallel",
                        recording("run", orchestrator.run_sessions_parallel))
    monkeypatch.setattr(eval_module, "run_sessions_parallel",
                        recording("eval", eval_module.run_sessions_parallel))

    # The run, reviewers: 2 — and seat 2's first verdict is malformed,
    # so its fresh retry must still carry the defect-hunt lens.
    seats = LensSeats(hunt_first="not a verdict")
    env, result = run_round(tmp_path / "run2", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    loaded = _loaded_profile(env)
    want = reviewer_seat_prompts(loaded, 2)
    assert want[0] != want[1]
    run_fanouts = [systems for where, systems in fanouts if where == "run"]
    assert run_fanouts[0] == want, "the round's fan-out, seat for seat"
    # The retry fan-out of seat 2 (one spec): the hunt lens again.
    assert run_fanouts[1] == [want[1]]
    systems = seats.reviewer_systems()
    assert systems.count(want[1]) == 2 and systems.count(want[0]) == 1
    attempt = env.task()["attempts"][-1]
    assert attempt["prompt_versions"]["reviewer-1"] == prompt_hash(want[0])
    assert attempt["prompt_versions"]["reviewer-2"] == prompt_hash(want[1])

    # reviewers: 1 — the one seat carries both lenses.
    fanouts.clear()
    seats1 = LensSeats()
    env1, result1 = run_round(tmp_path / "run1", monkeypatch, seats1,
                              reviewers=1)
    assert result1.outcome == "pr_opened", result1
    one = reviewer_seat_prompts(_loaded_profile(env1), 1)
    assert [s for w, s in fanouts if w == "run"][0] == one
    assert seats1.reviewer_systems() == one

    # The eval's fan-out matches the run's, seat for seat.
    fanouts.clear()
    head = _branch_head(env.repo)
    eval_seats = LensSeats()
    report, _ = eval_module.run_l2_review(
        env.repo, loaded, env.base_sha, head, env.task(),
        session_runner=eval_seats, reviewers=2)
    assert report is not None
    assert [s for w, s in fanouts if w == "eval"][0] == want


def _loaded_profile(env) -> dict:
    from factory.contract import load_profile

    return load_profile(env.queue_path.parent / "project.yaml")


def _branch_head(repo: Path) -> str:
    """A head on the fixture repo: base plus the green implementation."""
    from _support.f72 import IMPL_GOOD, TEST_SOURCE

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("checkout", "-q", "-b", "f94-eval")
    (repo / "impl.py").write_text(IMPL_GOOD)
    (repo / "tests" / "test_feature.py").write_text(TEST_SOURCE)
    git("add", "-A")
    git("-c", "user.email=f@x", "-c", "user.name=F", "commit", "-qm", "head")
    return git("rev-parse", "HEAD")


# --------------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path, monkeypatch):
    """A finding outside the diff can never block — decided by the
    parent from the diff's files; inside the diff, today's rules hold
    exactly, ``sibling: true`` or not."""
    # (a) A major sibling on an untouched file, reported by BOTH seats
    # (unanimous) — and the hunt seat declared fail on the strength of
    # it alone: the round passes, the finding is kept, marked sibling,
    # with its original severity.
    major = _sibling("whitespace-only email accepted here too",
                     sibling=True)
    seats = LensSeats(conformance=_verdict(major, declared="pass"),
                      hunt=_verdict(major, declared="fail"))
    env, result = run_round(tmp_path / "a", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    l2 = result.reports["L2"]
    assert l2["verdict"] == "pass"
    [kept] = l2["findings"]
    assert kept["sibling"] is True and kept["severity"] == "major"
    assert kept["reported_by"] == [1, 2]

    # (b) The same major on a CHANGED file: the round fails.
    inside = _sibling("whitespace-only email accepted", file=CHANGED)
    seats = LensSeats(hunt=_verdict(inside, declared="pass"))
    _, result = run_round(tmp_path / "b", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    assert result.reports["L2"]["verdict"] == "fail"
    assert result.reports["L2"]["findings"][0]["sibling"] is False

    # (c) A forged sibling: true on a changed-file blocking finding:
    # the flag never lowers the bar inside the diff — still fails.
    forged = _sibling("the check is inverted", file=CHANGED,
                      severity="blocking", sibling=True)
    seats = LensSeats(hunt=_verdict(forged, declared="pass"))
    _, result = run_round(tmp_path / "c", monkeypatch, seats)
    assert result.outcome != "pr_opened", result
    assert result.reports["L2"]["verdict"] == "fail"

    # (d) A sibling is never sent to the implementer: round 1 fails on
    # an in-diff major and carries a sibling major; round 2's fix list
    # names the first and not the second.
    seats = _TwoRounds(
        first=_verdict(inside, _sibling("SIBLING-ONLY-MARKER at line 1"),
                       declared="fail"))
    _, result = run_round(tmp_path / "d", monkeypatch, seats,
                          budget={"size": "S", "max_attempts": 2,
                                  "max_turns": 40, "wall_clock_min": 60})
    prompts = seats.implementer_prompts()
    assert len(prompts) >= 2, result
    assert "whitespace-only email accepted" in prompts[1]
    assert "SIBLING-ONLY-MARKER" not in prompts[1]

    # (g) An UNFLAGGED blocking on an untouched file — a break this diff
    # causes at an unchanged caller, reported where it shows up — fails
    # the round exactly as before F-94, and reaches the implementer's fix
    # list: the file alone never makes a sibling.
    caused = _sibling("CALLER-BREAK-MARKER still calls the renamed helper",
                      severity="blocking", sibling=False)
    seats = _TwoRounds(first=_verdict(caused, declared="fail"))
    _, result = run_round(tmp_path / "g", monkeypatch, seats,
                          budget={"size": "S", "max_attempts": 2,
                                  "max_turns": 40, "wall_clock_min": 60})
    prompts = seats.implementer_prompts()
    assert len(prompts) >= 2, result
    assert "CALLER-BREAK-MARKER" in prompts[1]

    # (h) The same blocking, FLAGGED by the reviewer as an older site of
    # the class, on the same untouched file: the round passes and the
    # finding goes to the follow-up draft.
    flagged = _sibling("OLD-SITE-MARKER same check missing here",
                       severity="blocking")
    seats = LensSeats(hunt=_verdict(flagged, declared="pass"))
    env, result = run_round(tmp_path / "h", monkeypatch, seats)
    assert result.outcome == "pr_opened", result
    draft = followup(env)
    assert draft is not None
    assert any("OLD-SITE-MARKER" in c["text"] for c in draft["acceptance"])

    # (i) The flag is parsed strictly: only the JSON literal true.
    from factory.l2 import parse_verdict as _parse

    for loose in ("true", 1, "yes", None, ["true"]):
        parsed = _parse(_verdict(_sibling("x", sibling=loose)))
        assert parsed.parse_error is None
        assert parsed.findings[0].sibling_claim is False, loose
    assert _parse(_verdict(_sibling("x"))).findings[0].sibling_claim is True

    # (e) Unit: the verifier votes to drop a unanimous major sibling;
    # the unanimous rule refuses the drop, and it still cannot block.
    from factory.l2 import VerifierOutcome, merge_reviews, parse_verdict, settle_review_verdict

    reports = [parse_verdict(_verdict(major, declared="fail")),
               parse_verdict(_verdict(major, declared="pass"))]
    union = merge_reviews(reports)
    vote = VerifierOutcome(kept=[], dropped=[{
        "finding": dict(union.findings[0].__dict__),
        "reason": "pre-existing"}], error=None)
    settled = settle_review_verdict(
        union, {}, frozenset(), delta_mode=False, vote=vote, reviewers=2,
        outside_diff=lambda f: f == UNTOUCHED)
    assert settled.verdict == "pass"
    assert [f.sibling for f in settled.findings] == [True]

    # (f) The parent's rule on a real diff: a rename's old AND new path
    # are in the diff; ./, a/../ and an in-clone absolute path normalise;
    # ../, untracked, a case variant and a symlink into the diff are not
    # provable — none of them is a sibling. Only a tracked, untouched
    # path (directly or through a symlink to one) is.
    import os

    from factory.siblings import diff_files

    repo = tmp_path / "diff"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "a.py").write_text("a\n")
    (repo / "old.py").write_text("old\n" * 20)
    (repo / "other.py").write_text("o\n")
    os.symlink("a.py", repo / "to_a.py")
    os.symlink("other.py", repo / "to_other.py")
    git("add", "-A")
    git("-c", "user.email=f@x", "-c", "user.name=F", "commit", "-qm", "b")
    base = git("rev-parse", "HEAD")
    (repo / "a.py").write_text("changed\n")
    git("mv", "old.py", "new.py")
    git("add", "-A")
    git("-c", "user.email=f@x", "-c", "user.name=F", "commit", "-qm", "h")
    files = diff_files(repo, base, git("rev-parse", "HEAD"))
    for inside in ("a.py", "./a.py", "x/../a.py", str(repo / "a.py"),
                   "old.py", "new.py", "../a.py", "A.py", "to_a.py",
                   "nothere.py", "", None):
        assert files.is_sibling(inside) is False, inside
    for outside in ("other.py", "./other.py", str(repo / "other.py"),
                    "to_other.py"):
        assert files.is_sibling(outside) is True, outside


class _TwoRounds(LensSeats):
    """Round 1's reviewers both return ``first``; later rounds pass."""

    def __init__(self, first: str):
        super().__init__()
        self.first = first

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        record = super().__call__(seat, prompt, run_dir, profile, **kwargs)
        if seat == "reviewer" and len(self.implementer_prompts()) == 1:
            record.result = self.first
        return record


# --------------------------------------------------------------------- AC-4 --


def test_ac_4(tmp_path: Path, monkeypatch):
    """Surviving siblings reach F-82's follow-up draft: a tracked,
    unprotected file enters its scope; a protected path, a risk-glob
    path, an untracked path and a ``../`` path never do, and the
    criterion says the file was left out. The PR body lists siblings
    under their own line. The draft stays ``draft``."""
    tracked = _sibling("SITE-TRACKED", severity="minor")
    protected = _sibling("SITE-PROTECTED", file=".factory/project.yaml",
                         severity="minor")
    risky = _sibling("SITE-RISK", file=".gitignore", severity="minor")
    # Both seats report them: the draft's scope rule is what is under
    # test here, not which lens found the sites.
    sites = _verdict(tracked, protected, risky, declared="pass")
    seats = LensSeats(conformance=sites, hunt=sites)
    env, result = run_round(
        tmp_path / "pub", monkeypatch, seats,
        profile_extra={"paths": {"risk": {"deploy": [".gitignore"]},
                                 "protected": [".factory/**"]}})
    assert result.outcome == "pr_opened", result
    draft = followup(env)
    assert draft is not None and draft["status"] == "draft"
    assert draft["scope"]["paths"] == [UNTOUCHED]
    criteria = {c["text"] for c in draft["acceptance"]}
    by_marker = {m: next(t for t in criteria if m in t)
                 for m in ("SITE-TRACKED", "SITE-PROTECTED", "SITE-RISK")}
    assert "left out" not in by_marker["SITE-TRACKED"]
    assert "left out of the follow-up's scope" in by_marker["SITE-PROTECTED"]
    assert "protected" in by_marker["SITE-PROTECTED"]
    assert "left out of the follow-up's scope" in by_marker["SITE-RISK"]
    assert "risk" in by_marker["SITE-RISK"]
    body = env.prs[0].body
    assert "outside the diff" in body
    section = body.split("## Review findings left open", 1)[1]
    assert section.index("outside the diff") < section.index("SITE-TRACKED")

    # The filter itself, every refusal arm (the parent's rule — not the
    # model's): untracked, ../, absolute, protected, risk; a clean
    # tracked path is admitted, normalised.
    from factory.siblings import followup_scope_refusal

    base = frozenset({"impl.py", "tests/test_base.py", ".gitignore",
                      ".factory/project.yaml"})
    profile = {"paths": {"protected": [".factory/**"],
                         "risk": {"deploy": [".gitignore"]}}}

    def refusal(file):
        return followup_scope_refusal(file, base, profile)

    assert refusal("tests/test_base.py") is None
    assert refusal("./tests/test_base.py") is None
    assert "not tracked" in refusal("nothere.py")
    assert refusal("../tests/test_base.py") is not None
    assert refusal("/etc/passwd") is not None
    assert "protected" in refusal(".factory/project.yaml")
    assert "risk" in refusal(".gitignore")

