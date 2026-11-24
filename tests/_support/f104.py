"""F-104 test support: a merged fixture task, the bench replay's seats.

Real git throughout (the F-38/F-23 split): a fixture project whose task
branch was merged the way the factory merges (squash — the pushed head's
tree lands on main as a NEW commit, so main never descends from the
pushed head), an approval ledger that records the repo (the way
``factory approve --profile`` does, so bench resolves the project the
way doctor does), and a bare remote the replay must never touch. Only
the model seats are scripted.

The fixture's shapes:

* the merged answer is ``answer() -> 42`` with a hidden contract test
  asserting it (the marker line is unique to the hidden file);
* the WRONG-fix arm implements ``answer() -> "42"`` with its own test
  at the same node id — it passes L1 red-first (fails on base, passes
  on its head) and fails the hidden test: exactly a fix that satisfies
  its own proof but not the hidden one.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
from pathlib import Path

from _support.f63 import ledger_approve
from factory.session import SessionRecord

REF = "tests/test_hidden.py::test_ac_1"

# Unique to the hidden file — the leak probe greps for these.
HIDDEN_MARKER = "F104-HIDDEN-MARKER"
HIDDEN_TEST = (
    "from impl import answer\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    f"    assert answer() == 42  # {HIDDEN_MARKER}\n"
)

BASE_IMPL = "def answer():\n    return 0\n"
FIX_IMPL = "def answer():\n    return 42\n"
# The wrong fix: passes its own L1 proof, fails the hidden test.
WRONG_IMPL = 'def answer():\n    return "42"\n'
WRONG_OWN_TEST = (
    "from impl import answer\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    assert answer() == "42"\n'
)

# The spec doc, committed at BASE (the replay points the implementer at
# it — an item whose doc is not on base cannot replay faithfully).
SPEC_DOC = (
    "# The plan\n"
    "\n"
    "## T-real-1: answer() returns 42\n"
    "\n"
    "The stored configuration read returns the numeric id 42.\n"
)

SPEC_PATH = "docs/plan.md"

# A long added line the quoting contract names verbatim (the quote
# refusal arm): the fix line itself, quoted inside a criterion.
QUOTE_FIX_IMPL = 'def answer():\n    return {"answer": 42, "unit": "int"}\n'
QUOTE_FIX_LINE = '    return {"answer": 42, "unit": "int"}'
QUOTE_CRITERION = (
    'answer() returns the dict literal `'
    'return {"answer": 42, "unit": "int"}` exactly'
)

TASK_ID = "T-real-1"
PR_NUMBER = 7
PR_URL = f"https://github.com/fixture/proj/pull/{PR_NUMBER}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class Env104:
    """One fixture project: repo + merged task + ledger + bare remote.

    ``answer_impl`` picks the merged fix's shape (the default plain fix,
    or the dict-returning shape the quote-refusal arm needs — its
    criterion quotes a long added line verbatim). ``merge`` picks how
    the task's PR landed on main: ``squash`` (a NEW commit with the
    branch's tree) or ``merge`` (``--no-ff``: a merge commit whose
    second parent IS the pushed head). ``keep_branch`` deletes the task
    branch after the merge — the shape GitHub leaves once a PR's branch
    is cleaned up. ``spec_doc`` overrides the plan doc committed at
    BASE (the leak arm plants a hidden-test assert line in it).
    """

    def __init__(self, tmp_path: Path, *, answer_impl: str = FIX_IMPL,
                 criterion_text: str = "answer() returns 42",
                 hidden_test: str = HIDDEN_TEST,
                 merge: str = "squash", keep_branch: bool = True,
                 spec_doc: str = SPEC_DOC,
                 setup_command: str = "true",
                 setup_outputs: "list[str] | None" = None):
        import yaml

        self.tmp = tmp_path
        self.factory_root = tmp_path / "factory-root"
        self.factory_root.mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")

        self.criterion_text = criterion_text
        acceptance = [
            {"id": "AC-1", "text": criterion_text,
             "check": {"type": "test", "ref": REF}}
        ]
        from factory.contract import acceptance_hash

        self.task = {
            "id": TASK_ID,
            "title": "Return the numeric configuration id",
            "source": f"{SPEC_PATH}#t-real-1",
            "kind": "build",
            "priority": "P1",
            "status": "in_review",
            "depends_on": [],
            "risk": [],
            "gates": {"spec_approval": True, "merge": "human",
                      "deploy": "human"},
            "acceptance": acceptance,
            "acceptance_hash": acceptance_hash(acceptance),
            "approved_at": "2026-11-24T10:00:00Z",
            "approved_by": "owner",
            "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                       "wall_clock_min": 30},
            "scope": {"paths": ["impl.py", "tests/test_hidden.py"]},
            "attempts": [],
        }
        self.profile = {
            "name": "fixture",
            "providers_allowed": ["anthropic"],
            "repo": {
                "path": str(self.repo),
                "default_branch": "main",
                "remote": str(self.remote),
                "merge_method": "squash",
            },
            "commands": {
                "setup": setup_command,
                "test": f"{shlex.quote(sys.executable)} -m pytest -q "
                        "-p no:cacheprovider",
            },
            "paths": {
                "risk": {},
                "protected": [".factory/**"],
                # A6: declared setup outputs survive L1's between-phase
                # cleans exactly as in production.
                **({"setup_outputs": setup_outputs}
                   if setup_outputs else {}),
            },
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        (factory_dir / "project.yaml").write_text(
            yaml.safe_dump(self.profile, sort_keys=False))
        self.queue_path = factory_dir / "tasks.json"
        (self.repo / "impl.py").write_text(BASE_IMPL)
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_base.py").write_text(
            "def test_base():\n    assert True\n")
        docs = self.repo / "docs"
        docs.mkdir()
        (docs / "plan.md").write_text(spec_doc)
        # A real project gitignores what its setup generates: F-73's
        # ready-clone rule refuses a setup that leaves the clone dirty
        # (untracked files git does not ignore).
        (self.repo / ".gitignore").write_text(
            ".factory/*.lock\n"
            + "".join(f"{line}\n" for line in (setup_outputs or [])))
        self.base_sha = _commit(self.repo, "base")
        # The merged task branch: the fix and the hidden tests.
        branch = "factory/t-real-1-20261124T000000Z-abc123"
        _git(self.repo, "checkout", "-q", "-b", branch)
        (self.repo / "impl.py").write_text(answer_impl)
        (self.repo / "tests" / "test_hidden.py").write_text(hidden_test)
        self.head_sha = _commit(self.repo, f"{TASK_ID}: the fix")
        self.branch = branch
        # The merge: squash lands the branch's tree as a NEW commit (no
        # ancestry); --no-ff lands a merge commit whose second parent IS
        # the pushed head.
        _git(self.repo, "checkout", "-q", "main")
        if merge == "squash":
            _git(self.repo, "merge", "--squash", branch)
            self.merge_sha = _commit(
                self.repo, f"{TASK_ID}: the fix (#{PR_NUMBER})")
        else:
            _git(self.repo, "merge", "--no-ff", branch, "-m",
                 f"Merge pull request #{PR_NUMBER} from fixture/t-real-1")
            self.merge_sha = _git(self.repo, "rev-parse", "HEAD")
        if not keep_branch:
            _git(self.repo, "branch", "-D", branch)
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo),
             str(self.remote)], check=True)
        # A7: the local repo tracks its remote the way a cloned project
        # repo does, so a stale local default branch is detectable (the
        # bench must refuse to derive a base from stale history).
        _git(self.repo, "remote", "add", "origin", str(self.remote))
        _git(self.repo, "fetch", "--quiet", "origin")
        self.remote_branches_before = self.remote_branches()
        # The queue records the merged task the sweep would have stamped.
        self.task["links"] = {
            "pr": PR_URL, "branch": branch, "pushed_head": self.head_sha,
            "merged": PR_URL,
        }
        self.queue_path.write_text(json.dumps(
            {"version": 1, "project": "fixture", "tasks": [self.task]},
            indent=2) + "\n")
        _git(self.repo, "add", "-A")
        _commit(self.repo, "queue")
        # The ledger records the repo the way an owner profile approval
        # does — bench resolves the project from it, like doctor.
        ledger_approve(self.factory_root, self.queue_path)

    # ------------------------------------------------------------ helpers --

    def remote_branches(self) -> list[str]:
        out = _git(self.remote, "for-each-ref",
                   "--format=%(refname:short)", "refs/heads/")
        return [line for line in out.splitlines() if line.strip()]

    def write_report(self, name: str, report: dict) -> Path:
        """Drop a bench report under <root>/bench/<project>/reports/."""
        reports = (self.factory_root / "bench" / "fixture" / "reports")
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / name
        path.write_text(json.dumps(report, indent=2) + "\n")
        return path


def _verdict(*findings: dict, declared: str | None = None) -> str:
    payload = {"verdict": declared or ("fail" if findings else "pass"),
               "findings": list(findings)}
    return json.dumps(payload)


def _finding(text: str, file: str = "impl.py", severity: str = "major",
             **extra) -> dict:
    item = {"id": "R-1", "axis": "standards", "severity": severity,
            "file": file, "text": text}
    item.update(extra)
    return item


class Seats:
    """Scripted seats for the replay scenarios.

    ``mode`` picks the implementer's behaviour:

    * ``good`` — the merged answer (hidden tests pass, factory
      publishes);
    * ``wrong`` — the wrong fix with its own passing proof (L1 green,
      hidden tests fail);
    * ``good`` plus ``reviews="block"`` — the factory never converges
      (an escalation recorded while the hidden tests would pass).

    ``probe`` collects the hostile session's own leak evidence: every
    implementer call greps its working tree and git object store for the
    hidden marker and records what it found (the AC-2 attack, run for
    real inside the session's own clone).
    """

    def __init__(self, mode: str = "good", reviews: str = "pass",
                 implementer_turns: int = 3, break_setup: bool = False):
        self.mode = mode
        self.reviews = reviews
        self.implementer_turns = implementer_turns
        self.break_setup = break_setup
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Path, dict]] = []
        self.probe: list[dict] = []

    def seats(self) -> list[str]:
        return [seat for seat, *_ in self.calls]

    def prompts(self, seat: str) -> list[str]:
        return [prompt for s, prompt, _, _ in self.calls if s == seat]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        with self.lock:
            self.calls.append((seat, prompt, run_dir, dict(kwargs)))
            first_impl = sum(1 for s, *_ in self.calls
                             if s == "implementer") == 1
        if seat == "implementer":
            if first_impl:
                # The probe runs BEFORE this session writes anything:
                # a clean clone at session start holds nothing of the
                # hidden tests (later rounds legitimately hold the
                # implementer's own work).
                self._leak_probe(run_dir)
            impl = {"good": FIX_IMPL, "wrong": WRONG_IMPL}[self.mode]
            own_test = (WRONG_OWN_TEST if self.mode == "wrong"
                        else HIDDEN_TEST)
            (run_dir / "impl.py").write_text(impl)
            target = run_dir / "tests" / "test_hidden.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(own_test)
            if self.break_setup:
                # The implementer broke the setup path: scoring's setup
                # run fails on the replay head (A6's failure arm).
                (run_dir / "break_setup").write_text("")
            return SessionRecord(
                seat=seat, status="completed",
                num_turns=self.implementer_turns, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                usage={"input": 120, "cache_read": 0, "cache_write": 0,
                       "output": 40},
                total_cost_usd=0.004,
                result="Implemented.",
            )
        if seat == "verifier":
            # Keep every finding: one keep vote for the id the prompt
            # carries (the F-96 vote shape).
            fid = "R-1"
            import re

            match = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
            if match:
                fid = match.group(1)
            return SessionRecord(
                seat=seat, status="completed", num_turns=1,
                model="stub-model",
                tokens={"input_tokens": 30, "output_tokens": 5},
                usage={"input": 30, "cache_read": 0, "cache_write": 0,
                       "output": 5},
                total_cost_usd=0.0005,
                result=json.dumps(
                    {"votes": [{"id": fid, "keep": True,
                                "reason": "scripted keep"}]}),
            )
        # reviewer-family seats: the round's verdict.
        if self.reviews == "block":
            verdict = _verdict(_finding("the returned value has no unit "
                                        "test"))
        else:
            verdict = _verdict()
        return SessionRecord(
            seat=seat, status="completed", num_turns=1,
            model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            usage={"input": 60, "cache_read": 0, "cache_write": 0,
                   "output": 10},
            total_cost_usd=0.001,
            result=verdict,
        )

    def _leak_probe(self, run_dir: Path) -> None:
        """The AC-2 attack, run from inside the session's own clone:
        grep the working tree and list every object the repo holds."""
        try:
            grep = subprocess.run(
                ["grep", "-rIn", "--exclude-dir=.git", HIDDEN_MARKER,
                 str(run_dir)], capture_output=True, text=True)
            git_grep = subprocess.run(
                ["git", "-C", str(run_dir), "grep", "-I", HIDDEN_MARKER],
                capture_output=True, text=True)
            objects = subprocess.run(
                ["git", "-C", str(run_dir), "cat-file",
                 "--batch-all-objects", "--batch-check"],
                capture_output=True, text=True)
            refs = subprocess.run(
                ["git", "-C", str(run_dir), "for-each-ref"],
                capture_output=True, text=True)
            remotes = subprocess.run(
                ["git", "-C", str(run_dir), "remote", "-v"],
                capture_output=True, text=True)
            self.probe.append({
                "grep_stdout": grep.stdout,
                "git_grep_stdout": git_grep.stdout,
                "objects": objects.stdout,
                "refs": refs.stdout,
                "remotes": remotes.stdout,
                "remote_stderr": remotes.stderr,
            })
        except OSError as exc:  # pragma: no cover - diagnostic probe
            self.probe.append({"error": str(exc)})
