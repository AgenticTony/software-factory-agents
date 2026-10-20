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
        reviews=(_verdict(_finding("Scope creep in impl.py")),),
    )
    result = env.run(seats)
    # Only prior keys in the FAIL → no progress: the run refuses a second round.
    assert result.outcome == "needs_human"
    assert result.escalation == "review_not_converging"
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

    # Orchestrator half: a FAIL whose findings were all raised before stops
    # the loop at that round instead of spending another one.
    repeat = _finding("wrong value returned", file="impl.py")
    env = Env(tmp_path)
    seats = Scripted(
        impls=Scripted.MOVING,  # budget for a third round exists...
        reviews=(_verdict(repeat), _verdict(repeat)),  # ...but never gets spent
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
