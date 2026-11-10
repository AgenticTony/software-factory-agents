"""F-41 acceptance tests — the final full review after fix rounds.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f41.py::test_ac_N``
means a test function with exactly that name. BUILD-ORDER §F-41 AC-1..AC-6
is the contract, byte-exact.

What is real and what is stubbed: the same split as the F-23/F-38 suites —
the git fixture (local repo + bare remote + factory root), the clone
manager, the queue, L0 and L1 through the real pinned srt sandbox are
real; only the model seats are scripted. No real model anywhere in this
file. The scripted reviewer decides WHICH review is asking from the
prompt's own shape (a delta review carries a Delta section; the final
review never does), never from call order — the fan-out is concurrent.

F-41 also fixes a merge defect the F-16 run exposed
(``merge_reviews`` keyed duplicates on finding text alone): the two
``test_merge_reviews_*`` tests cover both halves of the new rule.
"""

import json
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from _support.f63 import ledger_approve

from factory.config import ConfigError, FactoryConfig, load_config
from factory.contract import acceptance_hash
from factory.l2 import (
    apply_convergence,
    apply_vote,
    final_review_verdict,
    merge_reviews,
    parse_verdict,
    parse_verifier_vote,
)
from factory.orchestrator import PullRequest, run_once
from factory.prompts import assemble_reviewer_prompt
from factory.session import SessionRecord
from factory.state import TaskQueue

STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)
REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = (
    "from impl import answer\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    "    assert answer() == 42\n"
)

IMPL_GOOD = "def answer():\n    return 42\n"
IMPL_FIX = "def answer():\n    return 42  # fix round\n"
# Round 1 ships this untested helper; the round-2 fix to impl.py breaks
# its behaviour while never touching the file (the F-41 gap: the delta
# review cannot see it, L0/L1 have no test covering it).
OTHER_GOOD = "from impl import answer\n\n\ndef helper():\n    return answer() * 2\n"
OTHER_BROKEN = (
    "from impl import answer\n"
    "\n"
    "\n"
    "def helper():\n"
    "    # the fix round renamed answer()'s contract: helper now returns\n"
    "    # the wrong half — untested, so no gate catches it\n"
    "    return answer() // 2\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", **overrides) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42", "check": {"type": "test", "ref": REF}}
    ]
    task = {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-18T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 30},
        "attempts": [],
    }
    task.update(overrides)
    return task


def _finding(text="scope creep", file="impl.py", axis="spec",
             severity="blocking", **extra) -> dict:
    item = {"id": "R-1", "axis": axis, "severity": severity, "file": file, "text": text}
    item.update(extra)
    return item


def _verdict(*findings: dict, declared: str | None = None) -> str:
    payload = {"verdict": declared or ("fail" if findings else "pass"),
               "findings": list(findings)}
    return json.dumps(payload)


class Env:
    """One fixture project: local repo + bare remote + factory root."""

    def __init__(self, tmp_path: Path, tasks: list | None = None, config: str | None = None):
        self.tmp = tmp_path
        self.root = tmp_path / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
        if config is not None:
            (self.root / "config.yaml").write_text(config)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text(
            "def test_base():\n    assert True\n"
        )
        import yaml

        self.profile = {
            "name": "fixture",
            "repo": {
                "path": str(self.repo),
                "default_branch": "main",
                "remote": str(self.remote),
                "merge_method": "squash",
            },
            "commands": {
                "setup": "true",
                "test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
            },
            "paths": {"risk": {}, "protected": [".factory/**"]},
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        (factory_dir / "project.yaml").write_text(yaml.safe_dump(self.profile))
        self.queue_path = factory_dir / "tasks.json"
        doc = {"version": 1, "project": "fixture", "tasks": tasks or [_task()]}
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)], check=True
        )
        self.prs: list[PullRequest] = []

    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        return TaskQueue(self.queue_path).get(task_id)

    def report(self, attempt_no: int) -> dict:
        """The stored validator report of one attempt (F-10's _store layout)."""
        task = self.task()
        return json.loads(
            Path(task["attempts"][attempt_no - 1]["validator_report"]).read_text()
        )

    def trace_lines(self) -> list[dict]:
        """This run's whole F-30 trace, as event dicts."""
        run_id = self.task()["attempts"][0]["run_id"]
        path = self.root / "trace" / "fixture" / f"{run_id}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def run(self, session_runner, **kwargs):
        # F-63: record the fixture's owner approvals (profile + frozen
        # tasks) before the run — the ledger every run consults.
        ledger_approve(self.root, self.queue_path)
        from factory.mergeorder import Protection

        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault(
            "protection", lambda r, b: Protection(checked=True, strict=True)
        )
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_once(
            self.queue_path, factory_root=self.root, session_runner=session_runner,
            open_pr=self.open_pr, **kwargs,
        )


class Seats:
    """Scripted seats for the final-review pipeline.

    Review scripts are keyed by WHICH review is asking, detected from the
    prompt's own shape and the fan-out's position in the round (the
    fan-out is concurrent, so call order is scheduling, never meaning):

    * ``round1`` — the attempt's first review: a full-diff prompt, no
      Delta section, and no earlier fan-out in the round.
    * ``delta`` — a fix round's regular review: the prompt carries
      "## Delta since the last review".
    * ``final`` — the F-41 final review: a full-diff prompt in a round
      that already reviewed.
    * ``repeat`` — the final review's repeat (a full-diff fan-out in a
      round whose regular review was skipped). Defaults to the ``final``
      script: an honest stub says the same thing twice unless scripted
      otherwise.

    ``impl_files`` maps implementer round -> {path: content} (the last
    repeats); every round writes the contract test. The verifier votes
    by finding text (the F-38 pattern): ``vote_by_text`` maps a text
    substring to {"drop": True, "reason": …} or vote kwargs; findings
    with no entry are kept tagged important. ``on_reviewer(prompt)``
    runs inside any reviewer call (the budget test burns its fake clock
    there, on the delta prompt only).
    """

    def __init__(self, scripts: dict | None = None, impl_files: list | None = None,
                 vote_by_text: dict | None = None, on_reviewer=None,
                 implementer_turns: int = 3, reviewers: int = 2):
        self.scripts = {"round1": _verdict(), "delta": _verdict(), "final": _verdict()}
        self.scripts.update(scripts or {})
        self.scripts.setdefault("repeat", self.scripts["final"])
        self.impl_files = impl_files or [
            {"impl.py": IMPL_GOOD},
            {"impl.py": IMPL_FIX},
            {"impl.py": IMPL_FIX + "# final fix\n"},
        ]
        self.vote_by_text = vote_by_text or {}
        self.on_reviewer = on_reviewer
        self.implementer_turns = implementer_turns
        self.reviewers = reviewers
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Path, dict]] = []
        self.round_reviews = 0  # reviewer calls since this round's implementer
        self.finals_run = 0  # final reviews that already ran (1st vs repeat)

    def seats(self) -> list[str]:
        return [seat for seat, *_ in self.calls]

    def prompts(self, seat: str) -> list[str]:
        return [prompt for s, prompt, *_ in self.calls if s == seat]

    def kwargs(self, seat: str) -> list[dict]:
        return [kw for s, _, _, kw in self.calls if s == seat]

    def _review_script(self, prompt: str, impl_rounds: int) -> str:
        fanout_no, _ = divmod(self.round_reviews, self.reviewers)
        is_delta = "## Delta since the last review" in prompt
        if fanout_no == 0 and impl_rounds == 1:
            return self.scripts["round1"]
        if fanout_no > 0 or not is_delta:
            # The final full review — or its repeat: a full-diff fan-out
            # in a round whose regular review was skipped (fan-out 0,
            # no Delta section). Which one depends on how many finals
            # already ran, not on the prompt (they are the same shape).
            script = (self.scripts["final"] if self.finals_run == 0
                      else self.scripts["repeat"])
            self.finals_run += 1
            return script
        return self.scripts["delta"]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        with self.lock:
            self.calls.append((seat, prompt, run_dir, kwargs))
            impl_rounds = sum(1 for s, *_ in self.calls if s == "implementer")
            if seat == "implementer":
                self.round_reviews = 0
        if seat == "implementer":
            files = self.impl_files[min(impl_rounds - 1, len(self.impl_files) - 1)]
            for rel, text in files.items():
                (run_dir / rel).write_text(text)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=self.implementer_turns,
                model="stub-model", tokens={"input_tokens": 120, "output_tokens": 40},
                result="Implemented answer().",
            )
        if seat == "reviewer":
            with self.lock:
                script = self._review_script(prompt, impl_rounds)
                self.round_reviews += 1
            if self.on_reviewer is not None:
                self.on_reviewer(prompt)
            return SessionRecord(
                seat=seat, status="completed", num_turns=2, model="stub-model",
                tokens={"input_tokens": 60, "output_tokens": 10}, result=script,
            )
        if seat == "verifier":
            votes = []
            match = None
            for match in re.finditer(r"```json\n(\[\s*\{.*?\}\s*\])\n```", prompt, re.S):
                pass
            assert match, f"no findings array in the verifier prompt:\n{prompt[:2000]}"
            for finding in json.loads(match.group(1)):
                spec = next(
                    (v for text, v in self.vote_by_text.items()
                     if text in finding.get("text", "")),
                    None,
                )
                if spec is None:
                    votes.append({"id": finding["id"], "keep": True,
                                  "reason": "confirmed against the diff",
                                  "tag": "important"})
                elif spec.get("drop"):
                    votes.append({"id": finding["id"], "keep": False,
                                  "reason": spec["reason"]})
                else:
                    vote = {"id": finding["id"], "keep": True, **spec}
                    votes.append(vote)
            return SessionRecord(
                seat=seat, status="completed", num_turns=1, model="stub-model",
                tokens={"input_tokens": 55, "output_tokens": 5},
                result=json.dumps({"votes": votes}),
            )
        raise AssertionError(f"unexpected seat {seat!r}")


def _final_prompts(seats: Seats) -> list[str]:
    """The final-review reviewer prompts: full-diff shape, in rounds that
    already reviewed (the first review of an attempt is full too — this
    helper excludes it by counting fan-outs, two reviewer calls each)."""
    prompts = seats.prompts("reviewer")
    assert prompts, "no reviewer ran"
    fanouts = [prompts[i:i + seats.reviewers]
               for i in range(0, len(prompts), seats.reviewers)]
    full = [f for f in fanouts if "## Delta since the last review" not in f[0]]
    return [p for group in full[1:] for p in group]  # [1:]: the round-1 full review


def _fix_round(scripts: dict, impl_files: list | None = None, **kwargs) -> Seats:
    """The canonical F-41 setup: round 1 fails on impl.py, round 2's fix
    moves the head (a real delta) and its delta review passes."""
    return Seats(
        scripts={
            "round1": _verdict(_finding("wrong value returned", file="impl.py")),
            "delta": _verdict(),
            "final": _verdict(),
            **scripts,
        },
        impl_files=impl_files or [
            {"impl.py": IMPL_GOOD},
            {"impl.py": IMPL_FIX},
            {"impl.py": IMPL_FIX + "# final fix\n"},
        ],
        **kwargs,
    )


# --- AC-1: one final full review before publish ------------------------------


def test_ac_1(tmp_path: Path):
    # A task that passes L2 on its FIRST round skips the final review:
    # that review was already full.
    env = Env(tmp_path)
    seats = Seats(scripts={"round1": _verdict()})  # first review passes
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert seats.seats().count("reviewer") == 2  # one fan-out, nothing after it
    assert env.task()["attempts"][-1].get("final_review") is None

    # A fix round that passes the delta review gets ONE more full review:
    # the configured N reviewers over the complete base...head diff — no
    # delta section, no carried-findings suppression — then the verifier
    # vote. The PR opens only after the final review passes.
    env2 = Env(tmp_path / "two-round")
    seats2 = _fix_round({})
    result2 = env2.run(seats2)
    assert result2.outcome == "pr_opened", result2
    # Round 1's fan-out + round 2's delta fan-out + the final fan-out.
    assert seats2.seats().count("reviewer") == 6
    final = _final_prompts(seats2)
    assert len(final) == 2  # the configured default fan-out, fresh contexts
    assert final[0] == final[1]  # same complete diff, byte-identical prompts
    assert "## Diff (base...head)" in final[0]
    assert "## Delta since the last review" not in final[0]
    assert "## Findings from earlier rounds" not in final[0]
    # The delta review kept its own scope (the round's regular review).
    delta_prompt = [p for p in seats2.prompts("reviewer")
                    if "## Delta since the last review" in p][0]
    assert delta_prompt != final[0]
    # No verifier anywhere: every scripted verdict was findings-free.
    assert seats2.seats().count("verifier") == 1  # round 1's blocking finding only
    assert len(env2.prs) == 1


# --- AC-2: no out-of-delta drop; only blocking/major fail ---------------------


def test_ac_2(tmp_path: Path):
    # A finding in a file the fix rounds never touched stands in the
    # final review WITHOUT a missed_earlier field (the F-23 drop does
    # not apply), and a surviving major fails it: nothing publishes.
    env = Env(tmp_path / "stands", config="final_review_repeats: 0\n")
    seats = _fix_round(
        {"final": _verdict(
            _finding("helper() returns the wrong half — broken by the fix",
                     file="other.py", severity="major", line=5),
            declared="pass")},
        impl_files=[
            {"impl.py": IMPL_GOOD, "other.py": OTHER_GOOD},
            {"impl.py": IMPL_FIX, "other.py": OTHER_BROKEN},
        ],
    )
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "review_not_converging"
    assert env.prs == []
    final_report = env.report(2)["L2"]
    assert final_report["verdict"] == "fail"  # AC-6: the major fails it
    assert final_report["dropped"] == []  # AC-2: nothing was out of scope
    matches = [f for f in final_report["findings"]
               if f.get("file") == "other.py"]
    assert len(matches) == 1 and matches[0]["severity"] == "major"
    assert matches[0].get("missed_earlier") is None  # it never needed one

    # minor and note findings never fail the final review — not even a
    # declared fail with nothing heavier — and reach the PR body within
    # the nit budget.
    env2 = Env(tmp_path / "minors")
    nits = [
        _finding(f"nit {i}: tidy the helper", file="other.py",
                 severity="minor", id=f"R-{i}")
        for i in range(1, 6)
    ]
    seats2 = _fix_round({"final": _verdict(*nits, declared="fail")})
    result2 = env2.run(seats2)
    assert result2.outcome == "pr_opened", result2
    body = env2.prs[0].body
    assert "nit 1" in body and "nit 2" in body and "nit 3" in body
    assert "nit 4" not in body and "nit 5" not in body  # the nit budget
    attempt = env2.task()["attempts"][-1]
    assert attempt["final_review"]["verdict"] == "pass"
    assert len(attempt["final_review"]["findings"]) == 3
    budget_drops = [d for d in attempt["review_drops"] if d["reason"] == "nit_budget"]
    assert len(budget_drops) == 2


# --- AC-3: one more implementer round, then a bounded repeat ------------------


def test_ac_3(tmp_path: Path):
    # The config knob: default 1, whole numbers >= 0, bools refused.
    assert FactoryConfig().final_review_repeats == 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text("final_review_repeats: 2\n")
        assert load_config(path).final_review_repeats == 2
        path.write_text("final_review_repeats: 0\n")
        assert load_config(path).final_review_repeats == 0
        path.write_text("final_review_repeats: -1\n")
        try:
            load_config(path)
        except ConfigError:
            pass
        else:
            raise AssertionError("negative final_review_repeats must refuse")
        path.write_text("final_review_repeats: true\n")
        try:
            load_config(path)
        except ConfigError:
            pass
        else:
            raise AssertionError("a boolean final_review_repeats must refuse")

    # A failed final review sends its findings to one more implementer
    # round, followed by L0, L1 and a repeat of the final review. When
    # the repeat fails again the budget is spent: review_not_converging,
    # and nothing is published with an unresolved major.
    env = Env(tmp_path / "exhausted")
    major = _finding("helper() still returns the wrong half", file="other.py",
                     severity="major")
    seats = _fix_round(
        {"final": _verdict(major, declared="pass")},
        impl_files=[
            {"impl.py": IMPL_GOOD, "other.py": OTHER_GOOD},
            {"impl.py": IMPL_FIX, "other.py": OTHER_BROKEN},
            {"impl.py": IMPL_FIX, "other.py": OTHER_BROKEN},  # the fix round fails
        ],
    )
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "review_not_converging"
    assert env.prs == []
    task = env.task()
    assert [a["outcome"] for a in task["attempts"]] == ["fail", "fail", "escalated"]
    assert task["attempts"][-1]["escalation"] == "review_not_converging"
    # The failed reviews' findings were each next round's work: round 1's
    # blocking finding reached round 2's implementer, and the FAILED
    # FINAL review's major reached round 3's.
    fix_prompt_2 = seats.prompts("implementer")[1]
    assert "## Findings to fix" in fix_prompt_2
    assert "wrong value returned" in fix_prompt_2
    fix_prompt_3 = seats.prompts("implementer")[2]
    assert "## Findings to fix" in fix_prompt_3
    assert "helper() still returns the wrong half" in fix_prompt_3
    # The repeat was the final review again: full diff, no delta section,
    # and the round's L0/L1 really ran (reports stored per attempt).
    repeat = _final_prompts(seats)[-2:]
    assert len(repeat) == 2 and "## Delta since the last review" not in repeat[0]
    for attempt_no in (2, 3):
        stored = env.report(attempt_no)
        assert stored["L0"]["verdict"] == "pass"
        assert stored["L1"]["verdict"] == "pass"
    assert task["attempts"][1]["final_review"]["verdict"] == "fail"
    assert task["attempts"][1]["final_review"]["repeat"] is False
    assert task["attempts"][2]["final_review"]["repeat"] is True

    # final_review_repeats: 0 — the first failed final review escalates
    # at once; no repeat round is spent.
    env0 = Env(tmp_path / "zero", config="final_review_repeats: 0\n")
    seats0 = _fix_round(
        {"final": _verdict(major, declared="pass")},
        impl_files=[
            {"impl.py": IMPL_GOOD, "other.py": OTHER_GOOD},
            {"impl.py": IMPL_FIX, "other.py": OTHER_BROKEN},
        ],
    )
    result0 = env0.run(seats0)
    assert result0.outcome == "needs_human", result0
    assert result0.escalation == "review_not_converging"
    assert [a["outcome"] for a in env0.task()["attempts"]] == ["fail", "escalated"]


# --- AC-4: inside the budgets; never skipped to publish -----------------------


class _FakeTime:
    """A monotonic clock the scripted reviewer flips: after the delta
    fan-out returns, every monotonic() call steps an hour, so the
    wall-clock budget reads as spent before the final review starts."""

    def __init__(self):
        self.step = 0.0
        self._now = 1000.0

    def monotonic(self) -> float:
        self._now += self.step
        return self._now


def test_ac_4(tmp_path: Path, monkeypatch):
    # Turn budget (F-64 AMENDED, owner decision 2026-11-09): the review
    # seats' caps are the parent's fixed constants, so a turn budget the
    # implementer's record claims to have spent (7 of 10) still runs the
    # final review at FULL caps — it is never skipped to publish in time
    # and never squeezed out by the implementer; the PR opens only after
    # it passed. (The pre-F-64 remainder rule escalated budget_exhausted
    # here — the exact starvation the amendment removes.)
    from factory.orchestrator import REVIEWER_TURN_CAP, VERIFIER_TURN_CAP

    env = Env(tmp_path / "turns", tasks=[_task(
        # max_turns 13 is deliberately NOT the verifier cap (10) or the
        # reviewer cap (20), so a site regressing to the task budget is
        # distinguishable from the fixed caps.
        budget={"size": "S", "max_attempts": 3, "max_turns": 13, "wall_clock_min": 30},
    )])
    # The FINAL review carries one minor finding (declared pass), so its
    # VERIFIER runs too — both verifier passes (round 1's and the final
    # review's) are pinned at the fixed cap.
    seats = _fix_round(
        {"final": _verdict(
            _finding("late nit: trailing whitespace", severity="minor"),
            declared="pass")},
        implementer_turns=7,
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    # Round 1 + the delta fan-out + THE FINAL FAN-OUT: all three ran.
    assert seats.seats().count("reviewer") == 6
    assert {kw["max_turns"] for kw in seats.kwargs("reviewer")} == {
        REVIEWER_TURN_CAP
    }
    verifier_caps = [kw["max_turns"] for kw in seats.kwargs("verifier")]
    assert len(verifier_caps) == 2  # round 1's vote + the FINAL vote
    assert set(verifier_caps) == {VERIFIER_TURN_CAP}
    attempt = env.task()["attempts"][-1]
    assert attempt["final_review"]["verdict"] == "pass"  # it really ran

    # Wall clock: the delta fan-out burns the budget; the final review
    # refuses to start rather than being skipped.
    env2 = Env(tmp_path / "wall")
    fake = _FakeTime()
    monkeypatch.setattr("factory.orchestrator.time", fake)

    def burn_on_delta(prompt: str) -> None:
        if "## Delta since the last review" in prompt:
            fake.step = 3600.0  # the delta reviewers' session "takes" an hour

    seats2 = _fix_round({}, on_reviewer=burn_on_delta)
    result2 = env2.run(seats2)
    assert result2.outcome == "needs_human", result2
    assert result2.escalation == "budget_exhausted"
    assert env2.prs == []
    assert seats2.seats().count("reviewer") == 4  # the final fan-out never started
    attempt2 = env2.task()["attempts"][-1]
    assert "final review" in attempt2["note"]


# --- AC-5: the attempt record, the trace, and the closed gap ------------------


def test_ac_5(tmp_path: Path):
    # The gap F-41 closes, end to end: a fix round breaks untested code
    # in a file outside its delta, the delta review misses it, the final
    # review catches it, and no PR opens until it is fixed.
    env = Env(tmp_path)
    seats = _fix_round(
        {"final": _verdict(
            _finding("helper() returns the wrong half — broken by the fix",
                     file="other.py", severity="major", line=6),
            declared="pass"),
         "repeat": _verdict()},  # the repeat passes once other.py is fixed
        impl_files=[
            {"impl.py": IMPL_GOOD, "other.py": OTHER_GOOD},
            {"impl.py": IMPL_FIX, "other.py": OTHER_BROKEN},
            # The repeat round fixes the collateral damage too.
            {"impl.py": IMPL_FIX, "other.py": OTHER_GOOD + "# helper restored\n"},
        ],
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    assert len(env.prs) == 1  # and not one PR sooner
    task = env.task()
    assert [a["outcome"] for a in task["attempts"]] == ["fail", "fail", "pass"]
    # The attempt records show the final review ran, its findings and
    # its verdict (fail on the round that shipped the break, pass — a
    # repeat — on the round that fixed it).
    assert "final_review" not in task["attempts"][0]
    failed = task["attempts"][1]["final_review"]
    assert failed["verdict"] == "fail" and failed["repeat"] is False
    assert [f["file"] for f in failed["findings"]] == ["other.py"]
    assert failed["findings"][0]["severity"] == "major"
    passed = task["attempts"][2]["final_review"]
    assert passed["verdict"] == "pass" and passed["repeat"] is True
    # The stored report keeps BOTH reviews of the failing round: the
    # delta review that passed and the final review that caught it.
    reports = env.report(2)
    assert reports["L2"]["verdict"] == "fail"
    assert reports["L2-delta"]["verdict"] == "pass"
    # The F-30 trace shows the final review ran, its findings and verdict.
    events = env.trace_lines()
    phases = [e["phase"] for e in events if e["type"] == "phase_start"]
    assert phases.count("L2-final") == 2  # the failed one and its repeat
    final_rows = [
        e for e in events
        if e["type"] == "gate_check" and e.get("layer") == "L2"
        and "wrong half" in e.get("note", "")
    ]
    assert final_rows and all(row["ok"] is False for row in final_rows)
    verdict_rows = [
        e for e in events
        if e["type"] == "phase_start" and e["phase"] == "L2-final"
    ]
    assert len(verdict_rows) == 2
    # The delta review missed it: its stored findings are empty.
    assert reports["L2-delta"]["findings"] == []


# --- AC-6: a surviving major fails the round, everywhere ----------------------


def test_ac_6(tmp_path: Path):
    # apply_vote: a declared pass with a surviving major fails.
    union = merge_reviews([
        parse_verdict(_verdict(_finding("empty state lies", severity="major"),
                               declared="pass")),
    ])
    voted = apply_vote(
        union,
        parse_verifier_vote(
            json.dumps({"votes": [
                {"id": "U-1", "keep": True, "reason": "confirmed", "tag": "important"},
            ]}),
            union.findings,
        ),
        reviewers=1,
    )
    assert voted.verdict == "fail"  # the declared pass does not override it

    # apply_convergence: the same rule on every round — first round
    # (full diff) and delta round (the finding inside the delta).
    first = apply_convergence(
        parse_verdict(_verdict(_finding("empty state lies", severity="major"),
                               declared="pass")),
        prior={}, delta_files=frozenset(), delta_mode=False)
    assert first.verdict == "fail"
    delta = apply_convergence(
        parse_verdict(_verdict(_finding("empty state lies", file="impl.py",
                                        severity="major", line=3),
                               declared="pass")),
        prior={}, delta_files=frozenset({"impl.py"}), delta_mode=True)
    assert delta.verdict == "fail"

    # The F-23 convergence stops still apply: a repeated major on
    # untouched code is a carried bounce — no_progress, so the
    # orchestrator escalates review_not_converging instead of looping.
    report = parse_verdict(_verdict(
        _finding("empty state lies", file="impl.py", severity="major")))
    key = report.findings[0].key
    bounced = apply_convergence(
        report, prior={key: "sha-1"}, delta_files=frozenset({"other.py"}),
        delta_mode=True)
    assert bounced.verdict == "fail" and bounced.no_progress
    assert bounced.no_progress_reason == "carried_repeats"

    # The final review's own rule (AC-2's other half): a declared fail
    # with nothing heavier than minors does not fail it.
    minors = parse_verdict(_verdict(
        _finding("tidy the helper", severity="minor"), declared="fail"))
    flipped = final_review_verdict(
        apply_convergence(minors, prior={}, delta_files=frozenset(), delta_mode=False))
    assert flipped.verdict == "pass"

    # Through the orchestrator: a round that declares pass with a
    # surviving major never opens a PR.
    env = Env(tmp_path / "orchestrator", tasks=[_task(
        budget={"size": "S", "max_attempts": 1, "max_turns": 20, "wall_clock_min": 30},
    )])
    seats = Seats(scripts={
        "round1": _verdict(_finding("empty state lies", severity="major"),
                           declared="pass"),
    })
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert env.prs == []
    assert env.report(1)["L2"]["verdict"] == "fail"

    # REVIEW.md and prompts/reviewer.md state the rule (and the prompt
    # pin is the sanctioned way the prompt changed — test_f09 enforces
    # it). Whitespace is normalised: REVIEW.md wraps at 76 columns.
    review_md = " ".join(
        (Path(__file__).resolve().parent.parent / "REVIEW.md").read_text().split())
    assert ("A surviving `major` finding fails the round exactly as a "
            "`blocking` one does") in review_md
    prompt = assemble_reviewer_prompt(env.profile)
    assert "A `blocking` or `major` finding" in prompt


# --- the merge defect: file + nearby line --------------------------------------


def test_merge_reviews_merges_on_file_and_nearby_line():
    # Two reviewers describe one defect in different words at nearby
    # lines of the same file: one union entry, both reviewer numbers —
    # so the unanimous rule can actually fire (F-16's run showed it
    # almost never did: every heavy finding stayed reported_by=[1]).
    one = parse_verdict(_verdict(_finding(
        "helper returns the wrong half", file="other.py", severity="major",
        line=10)))
    two = parse_verdict(_verdict(_finding(
        "the halving in helper is inverted", file="other.py", severity="major",
        line=13)))
    union = merge_reviews([one, two])
    assert len(union.findings) == 1
    merged = union.findings[0]
    assert merged.reported_by == [1, 2]
    # A duplicate rated more severely keeps the harsher severity.
    harsh = parse_verdict(_verdict(_finding(
        "helper returns the wrong half (wrong result)", file="other.py",
        severity="blocking", line=11)))
    union2 = merge_reviews([one, harsh])
    assert len(union2.findings) == 1
    assert union2.findings[0].severity == "blocking"
    # And the unanimous rule fires where it never could before: both
    # reviewers reported this major, so the one verifier cannot drop it.
    voted = apply_vote(
        union,
        parse_verifier_vote(
            json.dumps({"votes": [
                {"id": "U-1", "keep": False, "reason": "style-only: rename"},
            ]}),
            union.findings,
        ),
        reviewers=2,
    )
    assert voted.verdict == "fail"  # it survived the drop and fails the round
    assert voted.findings[0].tag == "important"
    assert voted.verifier_dropped[0]["reason"].startswith("override_refused")


def test_merge_reviews_keeps_different_bugs_apart_at_nearby_lines():
    # Location alone is not identity: two different defects a few lines
    # apart stay two findings, or one vanishes from the verdict, and the
    # survivor claims a second reviewer who never reported it.
    union = merge_reviews([
        parse_verdict(_verdict(_finding(
            "SQL injection in the user lookup query", file="app.py",
            severity="major", line=10))),
        parse_verdict(_verdict(_finding(
            "Off-by-one in the pagination limit", file="app.py",
            severity="major", line=13))),
    ])
    assert [f.text for f in union.findings] == [
        "SQL injection in the user lookup query",
        "Off-by-one in the pagination limit",
    ]
    assert [f.reported_by for f in union.findings] == [[1], [2]]


def test_a_location_merge_keeps_every_reporters_wording():
    # Merged findings stay readable: the implementer and the verifier see
    # both descriptions, whichever severity won.
    union = merge_reviews([
        parse_verdict(_verdict(_finding(
            "helper returns the wrong half", file="other.py",
            severity="minor", line=10))),
        parse_verdict(_verdict(_finding(
            "the halving in helper is inverted", file="other.py",
            severity="major", line=13))),
    ])
    assert len(union.findings) == 1
    merged = union.findings[0]
    assert merged.severity == "major"
    assert "helper returns the wrong half" in merged.text
    assert "the halving in helper is inverted" in merged.text
    assert merged.reported_by == [1, 2]


def test_merge_reviews_keeps_the_text_key_otherwise():
    # Same defect, same wording, no location: still one entry.
    one = parse_verdict(_verdict(_finding("scope creep: answer() logs")))
    two = parse_verdict(_verdict(_finding("SCOPE   creep: answer() logs")))
    assert len(merge_reviews([one, two]).findings) == 1
    # A file with no line on one side: the text key decides, and
    # different words stay separate findings.
    no_line = parse_verdict(_verdict(_finding(
        "off-by-one in the loop", file="impl.py", line=4)))
    different_words = parse_verdict(_verdict(_finding(
        "the loop bounds exclude the last row", file="impl.py")))
    separate = merge_reviews([no_line, different_words])
    assert len(separate.findings) == 2
    assert sorted(f.reported_by[0] for f in separate.findings) == [1, 2]
    # Lines far apart on the same file are different findings.
    far = parse_verdict(_verdict(_finding(
        "unrelated: dead import", file="impl.py", line=400, severity="note")))
    assert len(merge_reviews([no_line, far]).findings) == 2
