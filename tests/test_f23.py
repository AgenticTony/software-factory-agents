"""F-23 acceptance tests — BUILD-ORDER.md, work order F-23.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f23.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed: same split as the F-10 tests — the git
fixture (local repo + bare remote + factory root), the clone manager, the
queue, L0 and L1 through the real pinned srt sandbox are real; only the
model seats are stubbed. The stubs here are *scripted per round*: the
implementer writes a different (or deliberately identical) impl.py each
round, and the reviewer returns one verdict per call, because F-23 is
entirely about what happens between rounds — which findings repeat, what
the next review is scoped to, and when the loop refuses another round.
"""

import json
import re
import shlex
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from factory.contract import acceptance_hash, load_tasks
from factory.l2 import apply_convergence, finding_key, parse_verdict
from factory.orchestrator import PullRequest, run_once
from factory.progress import ProgressLog
from factory.session import SessionRecord
from factory.state import TaskQueue

STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)
REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = textwrap.dedent(
    """
    from impl import answer


    def test_ac_1():
        assert answer() == 42
    """
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
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 10},
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

    def __init__(self, tmp_path: Path, tasks: list | None = None):
        self.tmp = tmp_path
        self.root = tmp_path / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
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
        import yaml

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

    def log(self) -> ProgressLog:
        return ProgressLog.for_task(self.root, "fixture", "T-1")

    def report(self, attempt_no: int) -> dict:
        """The stored validator report of one attempt (F-10's _store layout)."""
        task = self.task()
        return json.loads(Path(task["attempts"][attempt_no - 1]["validator_report"]).read_text())

    def run(self, session_runner):
        return run_once(
            self.queue_path, factory_root=self.root, session_runner=session_runner,
            open_pr=self.open_pr, now=lambda: OFF_PEAK,
        )


class Scripted:
    """Stub seats with per-round scripted behaviour.

    ``impls`` holds one impl.py body per implementer round (the last
    repeats). The contract test must pass for L2 to run at all, so every
    body returns 42; the *content* differs when a scenario needs a moving
    head (a non-empty review delta) and is identical when it needs an
    unmoved one (an empty delta). The reviewer returns ``reviews[call-1]``
    (the last verdict repeats), so a scenario scripts exactly what each
    round's review says.
    """

    SAME = ("def answer():\n    return 42\n",)  # identical rounds: no new commit
    MOVING = (  # green every round, different content: the head moves each round
        "def answer():\n    return 42\n",
        "def answer():\n    return 42  # fix round\n",
        "def answer():\n    return 42  # fix round 2\n",
    )

    def __init__(self, impls=SAME, reviews=(_verdict(),), result="Implemented."):
        self.impls = list(impls)
        self.reviews = list(reviews)
        self.result = result
        self.calls: list[tuple[str, str, Path, dict]] = []

    def prompts(self, seat: str) -> list[str]:
        return [prompt for s, prompt, _, _ in self.calls if s == seat]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        self.calls.append((seat, prompt, run_dir, kwargs))
        if seat == "implementer":
            round_no = sum(1 for s, *_ in self.calls if s == "implementer")
            body = self.impls[min(round_no - 1, len(self.impls) - 1)]
            (run_dir / "impl.py").write_text(body)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40}, result=self.result,
            )
        call_no = sum(1 for s, *_ in self.calls if s == "reviewer")
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            result=self.reviews[min(call_no - 1, len(self.reviews) - 1)],
        )


# --- AC-1: stable finding keys; repeats are carried, not new ---------------


def test_ac_1(tmp_path: Path):
    # Unit half: the key is stable under text noise, and a repeat whose file
    # the delta didn't touch is demoted to carried, not new.
    one = parse_verdict(_verdict(_finding("Scope creep in impl.py")))
    two = parse_verdict(_verdict(_finding("  scope   CREEP\tin impl.py ")))
    assert one.findings[0].key == two.findings[0].key  # normalised summary
    assert finding_key(one.findings[0]) == one.findings[0].key
    other_file = parse_verdict(_verdict(_finding("Scope creep in impl.py", file="other.py")))
    other_axis = parse_verdict(
        _verdict(_finding("Scope creep in impl.py", axis="standards")))
    assert other_file.findings[0].key != one.findings[0].key
    assert other_axis.findings[0].key != one.findings[0].key

    prior = {one.findings[0].key: "sha-of-round-1"}
    carried = apply_convergence(two, prior, delta_files=set(), delta_mode=True)
    assert carried.findings[0].state == "carried"
    data = carried.to_dict()
    assert data["findings"][0]["state"] == "carried"  # listed as carried, not new
    assert data["verdict"] == "fail"  # an unresolved blocking finding still fails
    # The same key on a file the delta DID touch is a repeat after a failed
    # fix attempt: not carried, still a finding of this round.
    moved = apply_convergence(two, prior, delta_files={"impl.py"}, delta_mode=True)
    assert moved.findings[0].state == "new"

    # Integration half: a two-round run stores keys per attempt and marks
    # the unchanged repeat carried in round 2's stored report.
    repeat = _finding("Scope creep in impl.py")
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.SAME,  # identical content: the head (and delta) never moves
        reviews=(_verdict(repeat), _verdict(repeat)),
    )
    result = env.run(seats)
    assert result.outcome == "needs_human"  # round 2 repeats round 1 (see AC-4)
    first, second = env.report(1), env.report(2)
    assert first["L2"]["findings"][0]["key"] == one.findings[0].key
    assert second["L2"]["findings"][0]["key"] == first["L2"]["findings"][0]["key"]
    assert first["L2"]["findings"][0]["state"] == "new"
    assert second["L2"]["findings"][0]["state"] == "carried"
    # Stored per attempt where the next run can seed from: the progress log.
    entries = env.log().entries()
    assert entries[0]["finding_keys"] == [one.findings[0].key]
    assert entries[1]["finding_keys"] == [one.findings[0].key]


def test_carried_keys_survive_text_normalisation_across_rounds(tmp_path: Path):
    """The stored key is the identity: re-raised with different casing and
    spacing, the round-2 report still demotes it to carried."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.SAME,
        reviews=(
            _verdict(_finding("Scope creep in impl.py")),
            _verdict(_finding("scope   creep IN impl.py")),
        ),
    )
    env.run(seats)
    assert env.report(2)["L2"]["findings"][0]["state"] == "carried"


def test_prior_keys_seed_from_an_earlier_runs_progress_log(tmp_path: Path):
    """A finding raised in an earlier run (recorded in the progress log) is
    carried when the next run's reviewer raises the same key again."""
    first = parse_verdict(_verdict(_finding("Scope creep in impl.py")))
    env = Env(tmp_path)
    env.log().append({
        "run_id": "earlier", "outcome": "fail", "findings": [],
        "finding_keys": [first.findings[0].key],
    })
    seats = Scripted(
        impls=Scripted.SAME,  # the head never moves: nothing either round can fix
        reviews=(
            _verdict(_finding("Scope creep in impl.py")),
            _verdict(_finding("Scope creep in impl.py")),
        ),
    )
    result = env.run(seats)
    # Round 1 is carried (cross-run key) but NOT stopped — this run's
    # implementer just made an attempt the reviewer has seen once
    # (review finding 8). Round 2 re-raises the same key now with an
    # in-run raise sha and an untouched file: that is the bounce the
    # stop exists for.
    assert result.outcome == "needs_human"
    assert result.escalation == "review_not_converging"
    assert len(seats.prompts("reviewer")) == 2
    assert env.task()["attempts"][0]["outcome"] == "fail"  # round 1: a plain fail
    assert env.task()["attempts"][0].get("escalation") is None
    assert env.report(1)["L2"]["findings"][0]["state"] == "carried"


# --- AC-2: delta as primary scope; out-of-delta findings need a why --------


def test_ac_2(tmp_path: Path):
    # Unit half: in a delta round, a NEW finding outside the delta without a
    # missed-earlier justification is dropped; with one, and inside the
    # delta, it stands. A declared pass whose only blocker was dropped
    # becomes a pass.
    outside = parse_verdict(
        _verdict(_finding("old bug", file="tests/test_base.py"), declared="pass"))
    dropped = apply_convergence(
        outside, prior={}, delta_files={"impl.py"}, delta_mode=True)
    assert not dropped.findings and dropped.verdict == "pass"
    assert dropped.dropped[0]["reason"] == "out_of_delta_without_missed_earlier"

    justified = parse_verdict(_verdict(
        _finding("old bug", file="tests/test_base.py", missed_earlier="round 1 reviewed "
                    "only the delta's files and missed this pre-existing break")))
    kept = apply_convergence(
        justified, prior={}, delta_files={"impl.py"}, delta_mode=True)
    assert kept.findings and kept.verdict == "fail"

    inside = parse_verdict(_verdict(_finding("new bug", file="impl.py")))
    assert apply_convergence(
        inside, prior={}, delta_files={"impl.py"}, delta_mode=True).findings

    # Round one has no delta: nothing is out of scope yet.
    first_round = apply_convergence(
        outside, prior={}, delta_files=set(), delta_mode=False)
    assert first_round.findings and first_round.verdict == "fail"

    # Integration half: round 2's reviewer prompt scopes to the delta since
    # the last reviewed sha, keeps the full diff for reference — and an
    # out-of-delta finding really is dropped from the effective verdict.
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,  # a moving head: round 2's delta is non-empty
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # declared pass; the one blocking finding is out of the delta
                _finding("pre-existing oddity", file="tests/test_base.py"),
                declared="pass"),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"  # the drop flipped the effective verdict
    round1, round2 = seats.prompts("reviewer")
    assert "## Diff (base...head)" in round1  # round 1: the full diff is the scope
    assert "## Delta since the last review (primary scope)" in round2
    assert "## Full diff (base...head, reference only)" in round2
    delta = round2.split("primary scope)", 1)[1].split("## Full diff", 1)[0]
    assert "# fix round" in delta  # the delta carries round 2's change...
    assert "return 0" not in delta  # ...and only that: base content is outside it
    second = env.report(2)["L2"]
    assert second["dropped"][0]["finding"]["file"] == "tests/test_base.py"


def test_missed_earlier_is_parsed_onto_the_finding():
    report = parse_verdict(_verdict(
        _finding("old bug", file="old.py", missed_earlier="misread the first diff")))
    assert report.findings[0].missed_earlier == "misread the first diff"


# --- AC-3: fix the class; the next review checks the siblings --------------


def test_ac_3(tmp_path: Path):
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value returned", file="impl.py", severity="major")),
            _verdict(),  # round 2 passes: PR opens
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"
    fix_prompt = seats.prompts("implementer")[1]
    assert "## Findings to fix" in fix_prompt
    assert "wrong value returned" in fix_prompt
    assert "sibling" in fix_prompt  # fix the class, not the instance
    assert "defect class" in fix_prompt
    # Round 1 gets no fix section: there was nothing to fix yet.
    assert "## Findings to fix" not in seats.prompts("implementer")[0]
    # The next round's reviewer checks the siblings explicitly.
    assert "sibling" in seats.prompts("reviewer")[1]
    assert "sibling" not in seats.prompts("reviewer")[0].lower().split("## diff")[0]


# --- AC-4: no-progress stop → review_not_converging (schema, red-first) ----


def test_ac_4(tmp_path: Path):
    # Schema half (red-first against the pre-WO enum): the queue accepts an
    # attempt escalated with review_not_converging.
    task = _task(status="needs_human")
    task["attempts"] = [{
        "run_id": "r1", "slice_id": "T-1", "started": "2026-10-18T10:00:00Z",
        "outcome": "escalated", "escalation": "review_not_converging",
        "prompt_versions": {"implementer": "x", "reviewer": "y"}, "skills": [],
    }]
    queue = tmp_path / "tasks.json"
    queue.write_text(json.dumps({"version": 1, "project": "fixture", "tasks": [task]}))
    loaded = load_tasks(queue)  # ContractError before the enum lands (red)
    assert loaded["tasks"][0]["attempts"][0]["escalation"] == "review_not_converging"

    # Orchestrator half: a FAIL whose findings were all raised before and
    # whose files nobody touched since stops the loop at that round
    # instead of spending another one (review finding 3: a repeat on a
    # file the fix DID touch is an attempt in progress, not a bounce).
    repeat = _finding("wrong value returned", file="impl.py")
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.SAME,  # identical content: the finding's file is untouched
        reviews=(_verdict(repeat), _verdict(repeat)),  # budget for a 3rd round exists
    )
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "review_not_converging"
    assert [c[0] for c in seats.calls] == ["implementer", "reviewer"] * 2  # 2, not 3
    task = env.task()
    assert task["status"] == "needs_human"
    assert len(task["attempts"]) == 2
    assert task["attempts"][-1]["escalation"] == "review_not_converging"
    assert env.prs == []

    # Contrast: a genuinely new finding each round is progress — the budget,
    # not convergence, ends it.
    env2 = Env(tmp_path / "contrast")
    seats2 = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value returned", file="impl.py")),
            _verdict(_finding("also the wrong sign", file="impl.py")),
            _verdict(_finding("and no docstring", file="impl.py")),
        ),
    )
    result2 = env2.run(seats2)
    assert result2.outcome == "needs_human"
    assert result2.escalation == "attempts_exhausted"
    assert len(seats2.prompts("reviewer")) == 3

    # Mixed rounds continue: one carried finding plus one new one is progress.
    env3 = Env(tmp_path / "mixed")
    seats3 = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value returned", file="impl.py")),
            _verdict(
                _finding("wrong value returned", file="impl.py"),
                _finding("also the wrong sign", file="impl.py"),
            ),
            _verdict(),  # round 3 passes: PR opens
        ),
    )
    assert env3.run(seats3).outcome == "pr_opened"


# --- owner review round (PR #35; 9 findings — all fixed red-first) ----------


def test_review1_an_invalid_review_does_not_advance_the_frontier(tmp_path: Path):
    """Finding 1: a review whose output is not valid verdict JSON reviewed
    nothing — `last_reviewed` must not move, or code no reviewer ever saw
    becomes 'outside the delta' in the next round."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=("this is not verdict json at all", _verdict()),
    )
    result = env.run(seats)
    # Round 2's review covers everything since the last REAL review: the
    # full diff is the scope, not a delta against an unreviewed head.
    prompt2 = seats.prompts("reviewer")[1]
    assert "## Delta since the last review" not in prompt2, (
        "the round-1 head was marked reviewed although its review "
        "produced no verdict"
    )
    assert "## Diff (base...head)" in prompt2
    assert result.outcome == "pr_opened"


def test_review1b_failed_session_variant(tmp_path: Path):
    """Finding 1, the timed-out-review half: a reviewer session that never
    completed also advances nothing."""
    env = Env(tmp_path)
    seats = Scripted(impls=Scripted.MOVING, reviews=(_verdict(),))
    base_call = seats.__call__

    def runner(seat, prompt, run_dir, profile, **kwargs):
        if seat != "reviewer":
            return base_call(seat, prompt, run_dir, profile, **kwargs)
        if not any(c[0] == "reviewer" for c in seats.calls):
            seats.calls.append((seat, prompt, Path(run_dir), kwargs))
            return SessionRecord(seat=seat, status="failed",
                                 reason="error_during_execution")
        return base_call(seat, prompt, run_dir, profile, **kwargs)

    result = env.run(runner)
    prompt2 = seats.prompts("reviewer")[1]
    assert "## Delta since the last review" not in prompt2
    assert "## Diff (base...head)" in prompt2
    assert result.outcome == "pr_opened"


def test_review2_delta_survives_a_rewritten_prior_round(tmp_path: Path):
    """Finding 2: an implementer that amends the reviewed round's commit
    leaves `last_reviewed` unreachable in the fresh clone; the round must
    fall back to the full diff, not die as orchestrator_error."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(),  # round 2 passes: PR opens
        ),
    )
    base_call = seats.__call__

    def amender(seat, prompt, run_dir, profile, **kwargs):
        if seat != "implementer":
            return base_call(seat, prompt, run_dir, profile, **kwargs)
        seats.calls.append((seat, prompt, Path(run_dir), kwargs))
        round_no = sum(1 for s, *_ in seats.calls if s == "implementer")
        (Path(run_dir) / "impl.py").write_text(
            f"def answer():\n    return 42  # round {round_no}\n")
        (Path(run_dir) / "tests" / "test_feature.py").write_text(TEST_SOURCE)
        if round_no > 1:  # amend round 1's commit away: head loses its parent
            subprocess.run(["git", "-C", str(run_dir), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-c", "user.email=s@localhost", "-c", "user.name=S",
                 "-C", str(run_dir), "commit", "--amend", "--no-edit", "-qm", "x"],
                check=True)
        return SessionRecord(seat=seat, status="completed", num_turns=3,
                             model="stub-model", result="done")

    result = env.run(amender)
    assert result.outcome == "pr_opened"  # red before: orchestrator_error
    prompt2 = seats.prompts("reviewer")[1]
    assert "## Diff (base...head)" in prompt2  # fell back to the full diff
    assert "## Delta since the last review" not in prompt2


def test_review3_a_repeat_on_a_fixed_file_is_not_the_stop(tmp_path: Path):
    """Finding 3: a prior-key finding on a file the fix DID touch is an
    attempt in progress, not a bounce — the budget, not convergence, ends
    the run."""
    repeat = _finding("wrong value returned", file="impl.py")
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,  # impl.py changes every round
        reviews=(_verdict(repeat), _verdict(repeat), _verdict(repeat)),
    )
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "attempts_exhausted"  # red before: converging
    assert len(seats.prompts("reviewer")) == 3
    # The repeat on a touched file stands as the round's own finding.
    assert env.report(2)["L2"]["findings"][0]["state"] == "new"


def test_review4_fix_prompt_carries_the_findings_once_and_no_key_hashes(
        tmp_path: Path):
    """Finding 4: the fix block duplicated the history's newest entry, and
    the history rendered the 64-char finding_keys hashes."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("Xyzzy unique marker finding", file="impl.py")),
            _verdict(),
        ),
    )
    assert env.run(seats).outcome == "pr_opened"
    prompt2 = seats.prompts("implementer")[1]
    assert prompt2.count("Xyzzy unique marker finding") == 1
    assert not re.search(r"[0-9a-f]{64}", prompt2), (
        "finding-key hashes leaked into the implementer prompt"
    )


def test_review5_the_stop_is_decided_where_the_prior_set_lives():
    """Finding 5: apply_convergence computes the no-progress verdict itself
    (L2Report.no_progress) — no orchestrator-side snapshot to mis-order."""
    verdict = parse_verdict(_verdict(_finding("wrong value", file="impl.py")))
    key = verdict.findings[0].key

    untouched = apply_convergence(
        verdict, prior={key: "sha-1"}, delta_files=set(), delta_mode=True)
    assert untouched.no_progress is True  # in-run carry, file untouched

    touched = apply_convergence(
        verdict, prior={key: "sha-1"}, delta_files={"impl.py"}, delta_mode=True)
    assert touched.no_progress is False  # a fix was attempted

    cross_run = apply_convergence(
        verdict, prior={key: None}, delta_files=set(), delta_mode=True)
    assert cross_run.no_progress is False  # earlier run, not this run

    fresh = apply_convergence(verdict, prior={}, delta_files={"impl.py"},
                              delta_mode=True)
    assert fresh.no_progress is False


def test_review6_declared_fail_with_everything_dropped_stops(tmp_path: Path):
    """Finding 6: a declared fail whose every finding was dropped leaves a
    FAIL with nothing to fix — the run must stop, not burn the budget."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # declared fail; the one finding is out of the delta
                _finding("old oddity", file="tests/test_base.py")),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "review_not_converging"  # red before: exhausted
    assert len(seats.prompts("reviewer")) == 2  # round 3 never spent
    assert env.report(2)["L2"]["verdict"] == "fail"
    assert not env.report(2)["L2"]["findings"]


def test_review7_fileless_findings_are_never_dropped(tmp_path: Path):
    """Finding 7: contract-level findings rarely name a file; dropping them
    in delta rounds could flip a declared pass into an opened PR. Round 3
    tightened the fixture: the blocker carries no AC id either — an
    unanchored fileless finding must stand exactly like an anchored one."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # declared pass; the blocker names no file, no AC id
                _finding("the new endpoint has no auth check", file=None),
                declared="pass"),
            _verdict(),  # round 3 passes: PR opens
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"
    assert len(seats.prompts("reviewer")) == 3  # red before: 2 (PR at round 2)
    second = env.report(2)["L2"]
    assert second["findings"] and second["verdict"] == "fail"
    assert not second["dropped"]


def test_review8_a_pass_round_does_not_log_finding_keys(tmp_path: Path):
    """Finding 8 (pass-path half): a passed round's findings were accepted
    as non-blocking; recording their keys would poison a later fix round
    after a rejected PR. (The immediate-stop half is the rewritten
    cross-run seeding test above.)"""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # declared pass; the non-blocking note rides the pass
                _finding("minor tidy-up possible", file="impl.py",
                         severity="minor"),
                declared="pass"),
        ),
    )
    assert env.run(seats).outcome == "pr_opened"
    last = env.log().entries()[-1]
    assert last["outcome"] == "pass"
    assert "finding_keys" not in last  # red before: the pass logged keys
    # Fail rounds still log them — that is what seeds the next run.
    assert "finding_keys" in env.log().entries()[0]


def test_review9_dropped_findings_reach_the_human(tmp_path: Path):
    """Finding 9: a dropped blocking finding must be visible to the merger
    (PR body, outside the JSON blob) and in the round's numbered findings."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # declared pass; the one blocking finding is out of delta
                _finding("pre-existing oddity", file="tests/test_base.py"),
                declared="pass"),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"
    body = env.prs[0].body
    assert "dropped as out of scope" in body  # red before: JSON blob only
    assert "pre-existing oddity" in body
    round2_findings = env.log().entries()[1]["findings"]
    assert any("[L2 dropped" in line for line in round2_findings)


# --- owner review round 2 (PR #35; 9 findings on 116e16b — all fixed) ------


def _fix_section(prompt: str) -> str:
    """The text of a prompt's '## Findings to fix' section. The progress
    log (history) legitimately repeats old findings — including dropped
    ones, for the record — so fix-block assertions must scope here."""
    section = prompt.split("## Findings to fix", 1)
    assert len(section) == 2, "no fix section in prompt"
    return section[1].split("\n## ", 1)[0]


def test_review10_fallback_round_does_not_carry_repeats_as_untouched(tmp_path: Path):
    """Finding 1: when the history was rewritten the round reviews the full
    diff with no delta — an in-run repeat there is an attempt in progress
    (the implementer touched those files by rewriting), not a provably
    untouched carry. The stop must not fire through the fallback path."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(_finding("wrong value", file="impl.py")),
        ),
    )
    base_call = seats.__call__

    def amender(seat, prompt, run_dir, profile, **kwargs):
        if seat != "implementer":
            return base_call(seat, prompt, run_dir, profile, **kwargs)
        seats.calls.append((seat, prompt, Path(run_dir), kwargs))
        round_no = sum(1 for s, *_ in seats.calls if s == "implementer")
        (Path(run_dir) / "impl.py").write_text(
            f"def answer():\n    return 42  # round {round_no}\n")
        (Path(run_dir) / "tests" / "test_feature.py").write_text(TEST_SOURCE)
        if round_no > 1:  # rewrite history every later round: fallback path
            subprocess.run(["git", "-C", str(run_dir), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-c", "user.email=s@localhost", "-c", "user.name=S",
                 "-C", str(run_dir), "commit", "--amend", "--no-edit", "-qm", "x"],
                check=True)
        return SessionRecord(seat=seat, status="completed", num_turns=3,
                             model="stub-model", result="done")

    result = env.run(amender)
    assert result.outcome == "needs_human"
    # Red on 116e16b: review_not_converging after 2 rounds (the fallback's
    # empty delta_files marked the impl.py repeat carried → untouched).
    assert result.escalation == "attempts_exhausted"
    assert len(seats.prompts("reviewer")) == 3
    assert env.report(2)["L2"]["findings"][0]["state"] == "new"


def test_review11_dropped_findings_stay_out_of_the_fix_block(tmp_path: Path):
    """Finding 2: dropped findings are for the log and the human, never for
    the implementer's 'Findings to fix' — fixing their defect class would
    expand scope into code the orchestrator ruled out."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # round 2: one actionable + one out-of-delta dropped
                _finding("wrong value still", file="impl.py"),
                _finding("legacy oddity", file="tests/test_base.py"),
            ),
            _verdict(),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"
    fix3 = _fix_section(seats.prompts("implementer")[2])
    assert "wrong value still" in fix3
    assert "legacy oddity" not in fix3, (
        "a dropped (out-of-scope) finding reached the implementer's fix block"
    )
    # ...while the round's log entry keeps it for the record.
    assert any("[L2 dropped" in line for line in env.log().entries()[1]["findings"])


def test_review12_a_failed_review_keeps_the_fix_list_and_carried_list_together(
        tmp_path: Path):
    """Finding 3: a round whose review produced no verdict adds nothing
    actionable — the next fix block must still carry the last valid
    review's findings, or the reviewer is told 'do not re-raise A' while
    the implementer is no longer told to fix A."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            "not verdict json at all",  # round 2's review fails to parse
            _verdict(),  # round 3 passes: PR opens
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"
    fix3 = _fix_section(seats.prompts("implementer")[2])
    assert "wrong value" in fix3, (
        "the open finding from the last valid review vanished from the fix "
        "block after a failed review"
    )


def test_review13_the_stop_requires_l0_and_l1_to_have_passed():
    """Finding 4: the no-progress stop is gated on the round's own L0 and L1
    passing — not on the accident that L2 happens to run last."""
    from factory.l0 import L0Report
    from factory.l1 import L1Report
    from factory.orchestrator import _round_stuck

    stuck = parse_verdict(_verdict(_finding("wrong value", file="impl.py")))
    stuck = apply_convergence(
        stuck, prior={stuck.findings[0].key: "sha"}, delta_files=set(),
        delta_mode=True)
    assert stuck.no_progress
    ok_l0 = L0Report(verdict="pass")
    ok_l1 = L1Report(verdict="pass")
    assert _round_stuck(ok_l0, ok_l1, stuck) is True
    assert _round_stuck(L0Report(verdict="fail"), ok_l1, stuck) is False
    assert _round_stuck(ok_l0, None, stuck) is False  # L1 never ran
    assert _round_stuck(None, None, None) is False


def test_review14_the_stop_reason_comes_from_the_classifier():
    """Finding 5: apply_convergence says WHY the round is stuck; the
    orchestrator must not re-derive the branch from report fields."""
    carried = parse_verdict(_verdict(_finding("wrong value", file="impl.py")))
    key = carried.findings[0].key
    report = apply_convergence(
        carried, prior={key: "sha"}, delta_files=set(), delta_mode=True)
    assert report.no_progress_reason == "carried_repeats"

    dropped_case = parse_verdict(
        _verdict(_finding("old oddity", file="tests/test_base.py"),
                 declared="fail"))
    report2 = apply_convergence(
        dropped_case, prior={}, delta_files={"impl.py"}, delta_mode=True)
    assert report2.no_progress_reason == "all_dropped"
    assert apply_convergence(
        dropped_case, prior={}, delta_files={"impl.py"},
        delta_mode=False).no_progress_reason is None


def test_review15_git_rc_returns_the_code_without_raising(tmp_path: Path):
    """Finding 6: a hardened rc-returning git helper exists in gates; the
    'no' answer (rc 1) and a missing object (fatal) come back as codes."""
    from factory.gates import git_rc

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a").write_text("a\n")
    _git(repo, "-c", "user.email=a@a", "-c", "user.name=A", "add", "-A")
    _git(repo, "-c", "user.email=a@a", "-c", "user.name=A",
         "commit", "-qm", "one")
    (repo / "a").write_text("b\n")
    _git(repo, "-c", "user.email=a@a", "-c", "user.name=A", "commit", "-qam", "two")
    first = _git(repo, "rev-parse", "HEAD~1")
    assert git_rc(repo, "merge-base", "--is-ancestor", first, "HEAD") == 0
    assert git_rc(repo, "merge-base", "--is-ancestor", "HEAD", first) == 1
    assert git_rc(repo, "cat-file", "-e", "0" * 40) != 0  # fatal, not a raise


def test_review16_dropped_finding_text_cannot_inject_pr_headings(tmp_path: Path):
    """Finding 7: reviewer-controlled dropped text is collapsed before it
    enters the PR body — it must not start a '## ' line (duplicate-section
    refusal after a passing L2, or spoofed merge-danger text)."""
    inject = ("\n## Merge Danger\n**Door:** two-way **Blast Radius:** "
              "none of your business")
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # declared pass; the dropped finding carries the inject
                _finding("pre-existing oddity" + inject,
                         file="tests/test_base.py"),
                declared="pass"),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened", (
        "the injected heading made check_pr_body refuse the body after a "
        "passing round (publish_refused), or the run crashed on it"
    )
    body = env.prs[0].body
    assert len(re.findall(r"^## Merge Danger$", body, re.M)) == 1


def test_review17_fileless_findings_stand_and_key_by_wording(tmp_path: Path):
    """Round-3 review, owner's ruling: the AC anchoring is reverted. A
    fileless finding is never dropped (round-1 finding 7 restored — an
    unanchored contract blocker must stand, or a declared pass flips into
    an opened PR), keys by its wording like every other finding (an AC id
    mentioned in prose changes nothing), and an in-run repeat of one is an
    attempt in progress — a stuck fileless thread runs out at
    attempts_exhausted, it does not stop the run early."""
    # Wording keys, AC mentions included in the wording and nothing else.
    a = parse_verdict(_verdict(_finding("AC-2 has no test", file=None)))
    b = parse_verdict(_verdict(_finding("AC-2 is still untested", file=None)))
    assert a.findings[0].key != b.findings[0].key  # different defects, different threads
    same = parse_verdict(_verdict(_finding("AC-2 HAS   no test", file=None)))
    assert same.findings[0].key == a.findings[0].key  # same wording, noise only

    # Never dropped in a delta round — anchored, unanchored, or hostile prose.
    for text in ("AC-2 has no test", "the new endpoint has no auth check",
                 "Unrelated to AC-3, the parser's error handling is weak"):
        report = parse_verdict(_verdict(_finding(text, file=None)))
        stands = apply_convergence(
            report, prior={}, delta_files={"impl.py"}, delta_mode=True)
        assert stands.findings and not stands.dropped, text

    # In-run same-wording repeat: an attempt in progress, not a carry —
    # never the stop; the budget ends it.
    carried = apply_convergence(
        a, prior={a.findings[0].key: "sha-1"}, delta_files=set(),
        delta_mode=True)
    assert carried.findings[0].state == "new"  # red on 95f19d3: carried
    assert carried.no_progress is False

    # Cross-run repeats still carry (listed, not new) — they always did.
    cross = apply_convergence(
        a, prior={a.findings[0].key: None}, delta_files=set(), delta_mode=True)
    assert cross.findings[0].state == "carried"

    # Integration: a stuck fileless thread runs out, it does not stop.
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.SAME,
        reviews=(
            _verdict(_finding("AC-1 has no test", file=None)),
            _verdict(_finding("AC-1 has no test", file=None)),
            _verdict(_finding("AC-1 has no test", file=None)),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "attempts_exhausted"  # red on 95f19d3: converging


# --- owner review round 3 (PR #35; 9 findings on 95f19d3 — anchoring reverted) --


def test_review18_a_different_defect_on_the_same_ac_is_not_a_carry(tmp_path: Path):
    """Finding 1: AC-keyed fileless repeats carried after a real fix
    attempt — the finding-3 bug for contract-level findings. With wording
    keys restored, 'AC-2's test asserts the wrong value' after 'AC-2 has
    no test' is a new thread, not a carry of the old one."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,  # the implementer works on it every round
        reviews=(
            _verdict(_finding("AC-2 has no test", file=None)),
            _verdict(_finding("AC-2's test asserts the wrong value", file=None)),
            _verdict(_finding("AC-2's test asserts the wrong value", file=None)),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "attempts_exhausted"  # red on 95f19d3: converging
    assert len(seats.prompts("reviewer")) == 3
    assert env.report(2)["L2"]["findings"][0]["state"] == "new"


def test_review19_an_unanchored_fileless_blocker_does_not_flip_a_pass(tmp_path: Path):
    """Finding 2: the anchoring undid round-1 finding 7 — a declared pass
    whose only blocker is a fileless finding with no AC id was dropped
    and the PR opened. Never dropping fileless findings is restored."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(
                _finding("the new endpoint has no auth check", file=None),
                declared="pass"),
        ),
    )
    result = env.run(seats)
    # Red on 95f19d3: the drop flipped the verdict and the PR opened.
    assert result.outcome == "needs_human"
    assert result.escalation == "attempts_exhausted"
    second = env.report(2)["L2"]
    assert second["findings"] and second["verdict"] == "fail"
    assert not second["dropped"]
    assert env.prs == []


def test_review20_mentioning_an_ac_changes_nothing(tmp_path: Path):
    """Finding 3: `_AC_REF` over free text let any mention of an AC id
    bypass the drop rule and merge threads. With the anchoring gone, an
    AC id in prose is just wording: no bypass (nothing fileless is
    dropped anyway) and no thread merging (wording keys)."""
    # 'Unrelated to AC-3, the parser is weak' is NOT the thread 'AC-3 is
    # untested' — different wording, different key.
    a = parse_verdict(_verdict(_finding("AC-3 is untested", file=None)))
    b = parse_verdict(_verdict(
        _finding("Unrelated to AC-3, the parser's error handling is weak",
                 file=None)))
    assert a.findings[0].key != b.findings[0].key
    # And both stand in a delta round, mention or no mention.
    for report in (a, b):
        stands = apply_convergence(
            report, prior={}, delta_files={"impl.py"}, delta_mode=True)
        assert stands.findings and not stands.dropped


def test_review21_an_l0l1_failed_round_keeps_the_open_l2_findings(tmp_path: Path):
    """Finding 6 (orchestrator): when L0/L1 fail a round, L2 never runs
    and the fix list lost the last valid review's findings — while the
    next reviewer was still told 'do not re-raise' them. The same
    substitution a failed review gets."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(),  # (review 2 never runs: round 2 breaks the contract test)
            _verdict(),  # round 3 passes: PR opens
        ),
    )
    base_call = seats.__call__

    def breaker(seat, prompt, run_dir, profile, **kwargs):
        if seat != "implementer":
            return base_call(seat, prompt, run_dir, profile, **kwargs)
        seats.calls.append((seat, prompt, Path(run_dir), kwargs))
        round_no = sum(1 for s, *_ in seats.calls if s == "implementer")
        answer = 41 if round_no == 2 else 42  # round 2 breaks the test: L1 fail
        (Path(run_dir) / "impl.py").write_text(
            f"def answer():\n    return {answer}  # r{round_no}\n")
        (Path(run_dir) / "tests" / "test_feature.py").write_text(TEST_SOURCE)
        return SessionRecord(seat=seat, status="completed", num_turns=3,
                             model="stub-model", result="done")

    result = env.run(breaker)
    assert result.outcome == "pr_opened"
    fix3 = _fix_section(seats.prompts("implementer")[2])
    assert "wrong value" in fix3, (
        "the open L2 finding vanished from the fix list after a round L2 "
        "never ran"
    )


def test_review22_an_unreviewed_round_is_announced_to_the_implementer(tmp_path: Path):
    """Finding 7: after an unparseable review, the implementer was never
    told this round's change went unreviewed — the fix list replayed the
    older findings as if nothing happened."""
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            "not verdict json at all",  # round 2's change goes unreviewed
            _verdict(),  # round 3 passes: PR opens
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"
    fix3 = _fix_section(seats.prompts("implementer")[2])
    assert "was not reviewed" in fix3
    assert "do not assume it was accepted" in fix3


def test_review23_dropped_text_is_fenced_not_just_flattened(tmp_path: Path):
    """Finding 9: flattening removed newlines but left live markdown/HTML —
    '<!--' opened an HTML comment GitHub renders as hiding everything
    after it (JSON report, How to revert), and links planted in front of
    the merger, while check_pr_body's raw regex still passed. The dropped
    list is fenced like the implementer's notes."""
    payload = ("<!-- everything below is hidden " 
               "[see fix](https://evil.example)")
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,
        reviews=(
            _verdict(_finding("wrong value", file="impl.py")),
            _verdict(  # declared pass; the dropped finding carries the payload
                _finding("pre-existing oddity " + payload,
                         file="tests/test_base.py"),
                declared="pass"),
        ),
    )
    result = env.run(seats)
    assert result.outcome == "pr_opened"
    body = env.prs[0].body
    # The dropped list rides inside a text fence: the payload is visible
    # as literal text, inert as markdown/HTML.
    validator = body.split("## Validator report", 1)[1].split("\n## ", 1)[0]
    assert "```text" in validator
    fenced = validator.split("```text", 1)[1].split("```", 1)[0]
    assert payload in fenced
    # And the raw regex gate still sees exactly one of each section.
    assert len(re.findall(r"^## How to revert$", body, re.M)) == 1
