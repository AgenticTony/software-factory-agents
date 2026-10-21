"""F-25 acceptance tests — BUILD-ORDER.md, work order F-25.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f25.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real (L0-level tests):** fixture git repos, the real diff machinery and
  ``run_l0`` with the unconfined runner (the f07/f08 pattern).
* **Real (end-to-end AC-4):** the fixture project repo + bare remote, the
  clone manager, the queue, L0/L1, and the PR-body builder — the ``Seats``
  stub replaces only the model (the f10 pattern).
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

import pytest
import yaml

from factory.contract import ContractError, acceptance_hash, load_profile
from factory.gates import FileChange
from factory.l0 import run_l0
from factory.l1 import L1Report
from factory.orchestrator import PullRequest, run_once
from factory.prbody import PRBodyError, build_pr_body, check_pr_body
from factory.sentinels import (
    BUILTIN_SENTINELS,
    builtin_sentinel_findings,
    matched_risk_flags,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)

BASE_PROFILE = {
    "name": "fixture",
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
}

TASK = {"id": "T-1", "risk": []}


# ------------------------------------------------------------- L0 fixtures --


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """Build a fixture git repo with one base commit on ``main``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _change(repo: Path, files: dict[str, str]) -> None:
    """Add a ``feature`` branch commit rewriting the given files."""
    _git(repo, "checkout", "-b", "feature")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")


def _run(repo: Path, profile=None, task=None, **kwargs):
    return run_l0(
        profile or dict(BASE_PROFILE),
        repo,
        "main",
        "feature",
        task=task or dict(TASK),
        **kwargs,
    )


def _finding_ids(report) -> set:
    return {f.id for f in report.findings}


def _result_for(report, name: str) -> dict:
    return next(r for r in report.sentinel_results if r["name"] == name)


# --- AC-1: built-in sentinels fail with the sentinel's name -----------------


def test_ac_1(tmp_path):
    # (a) credential-shaped string added in a NON-test file
    repo = _repo(tmp_path, "cred", {"src/config.py": "x = 1\n"})
    _change(repo, {"src/config.py": 'x = 2\nKEY = "AKIAIOSFODNN7EXAMPLE"\n'})
    report = _run(repo)
    assert report.verdict == "fail"
    assert "credential_added" in _finding_ids(report)
    finding = next(f for f in report.findings if f.id == "credential_added")
    assert finding.file == "src/config.py"
    assert _result_for(report, "credential_added")["verdict"] == "fail"

    # (b) skip marker added on an existing test
    repo = _repo(
        tmp_path, "skip", {"tests/test_a.py": "def test_ac_1():\n    assert True\n"}
    )
    _change(
        repo,
        {"tests/test_a.py": textwrap.dedent(
            """
            import pytest


            @pytest.mark.skip(reason="flaky")
            def test_ac_1():
                assert True
            """
        ).lstrip()},
    )
    report = _run(repo)
    assert report.verdict == "fail"
    assert "skip_marker_added" in _finding_ids(report)
    assert _result_for(report, "skip_marker_added")["verdict"] == "fail"

    # (c) assertion removed from an existing test without a replacement
    repo = _repo(
        tmp_path, "assert", {"tests/test_a.py": "def test_ac_1():\n    assert total() == 3\n"}
    )
    _change(repo, {"tests/test_a.py": "def test_ac_1():\n    total()\n"})
    report = _run(repo)
    assert report.verdict == "fail"
    assert "assertion_removed" in _finding_ids(report)
    assert _result_for(report, "assertion_removed")["verdict"] == "fail"

    # (d) lint suppression added on a diff-changed line in a risk path
    repo = _repo(tmp_path, "noqa", {"src/pay/charge.py": "x = 1\n"})
    _change(repo, {"src/pay/charge.py": "x = 2  # noqa\n"})
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": {"payments": ["src/pay/**"]}, "protected": []}
    report = _run(repo, profile, task={"id": "T-1", "risk": ["payments"]})
    assert report.verdict == "fail"
    assert "lint_suppression_added" in _finding_ids(report)
    assert _result_for(report, "lint_suppression_added")["verdict"] == "fail"

    # ... and the same suppression OUTSIDE every risk path stays quiet
    repo = _repo(tmp_path, "noqa-clean", {"src/misc.py": "x = 1\n"})
    _change(repo, {"src/misc.py": "x = 2  # noqa\n"})
    report = _run(repo)
    assert report.verdict == "pass"
    assert "lint_suppression_added" not in _finding_ids(report)


# --- AC-2: project sentinels under sentinels.<risk flag> ---------------------


_MIN_PROFILE = {
    "name": "fixture",
    "repo": {
        "path": "/tmp/proj",
        "default_branch": "main",
        "remote": "https://example.com/proj.git",
    },
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
    "deploy": {"mechanism": "none", "auto_on_merge": False},
    "live_checks": {},
    "docs_allowlist": [],
}


def _profile_file(tmp_path: Path, sentinels) -> Path:
    profile = dict(_MIN_PROFILE)
    profile["sentinels"] = sentinels
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(profile))
    return path


def test_ac_2(tmp_path):
    # (a) the schema validates the sentinels key: shape and risk-flag names
    load_profile(_profile_file(tmp_path, {
        "payments": [{"command": "grep -r TODO src/pay", "description": "no TODOs in payments"}]
    }))
    with pytest.raises(ContractError):
        load_profile(_profile_file(tmp_path, {"made_up_flag": [
            {"command": "true", "description": "x"}
        ]}))
    with pytest.raises(ContractError):
        load_profile(_profile_file(tmp_path, {"payments": [{"command": "true"}]}))
    with pytest.raises(ContractError):
        load_profile(_profile_file(tmp_path, {"payments": [
            {"command": 3, "description": "x"}
        ]}))

    # (b) a project sentinel runs ONLY when the diff touches the flag's globs
    repo = _repo(tmp_path, "outside", {"README.md": "base\n"})
    _change(repo, {"README.md": "change\n"})
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": {"payments": ["src/pay/**"]}, "protected": []}
    profile["sentinels"] = {"payments": [{
        "command": "touch sentinel-ran && exit 3",
        "description": "payments invariant",
    }]}
    report = _run(repo, profile, task={"id": "T-1", "risk": ["payments"]})
    assert report.verdict == "pass"
    assert "sentinel_failed" not in report.escalations
    assert not (repo / "sentinel-ran").exists(), "sentinel ran though no risk path was touched"
    entry = _result_for(report, "payments invariant")
    assert entry["kind"] == "project"
    assert entry["verdict"] == "skipped"

    # (c) a failing project sentinel FAILs and escalates sentinel_failed
    repo = _repo(tmp_path, "inside", {"src/pay/charge.py": "x = 1\n"})
    _change(repo, {"src/pay/charge.py": "x = 2\n"})
    report = _run(repo, profile, task={"id": "T-1", "risk": ["payments"]})
    assert report.verdict == "fail"
    assert "sentinel_failed" in report.escalations
    assert "sentinel_failed" in _finding_ids(report)
    entry = _result_for(report, "payments invariant")
    assert entry["verdict"] == "fail"
    assert entry["risk_flag"] == "payments"


# --- AC-3: every built-in sentinel has a flagged and a clean fixture ---------


def _clean_test_file() -> str:
    return "def test_ac_1():\n    assert total() == 3\n"


_SENTINEL_CASES = {
    "credential_added": {
        "risk": {},
        "task_risk": [],
        "base": {"src/config.py": "x = 1\n"},
        "flagged": {"src/config.py": 'x = 2\nKEY = "AKIAIOSFODNN7EXAMPLE"\n'},
        "clean": {"src/config.py": "x = 2\n"},
    },
    "skip_marker_added": {
        "risk": {},
        "task_risk": [],
        "base": {"tests/test_a.py": _clean_test_file()},
        "flagged": {
            "tests/test_a.py":
                "import pytest\n\n\n@pytest.mark.skip(reason='x')\n" + _clean_test_file()
        },
        "clean": {
            "tests/test_a.py":
                _clean_test_file() + "\n\ndef test_ac_2():\n    assert total() >= 0\n"
        },
    },
    "assertion_removed": {
        "risk": {},
        "task_risk": [],
        "base": {"tests/test_a.py": _clean_test_file()},
        "flagged": {"tests/test_a.py": "def test_ac_1():\n    total()\n"},
        "clean": {"tests/test_a.py": _clean_test_file() + "    assert total() >= 0\n"},
    },
    "lint_suppression_added": {
        "risk": {"payments": ["src/pay/**"]},
        "task_risk": ["payments"],
        "base": {"src/pay/charge.py": "x = 1\n"},
        "flagged": {"src/pay/charge.py": "x = 2  # type: ignore\n"},
        "clean": {"src/pay/charge.py": "x = 2\n"},
    },
}


def test_ac_3(tmp_path):
    assert set(_SENTINEL_CASES) == set(BUILTIN_SENTINELS), (
        "every built-in sentinel needs a flagged and a clean fixture "
        f"(cases: {sorted(_SENTINEL_CASES)}, builtins: {sorted(BUILTIN_SENTINELS)})"
    )
    for name, case in _SENTINEL_CASES.items():
        profile = dict(BASE_PROFILE)
        profile["paths"] = {"risk": dict(case["risk"]), "protected": []}
        task = {"id": "T-1", "risk": list(case["task_risk"])}

        flagged = _repo(tmp_path, f"{name}-flagged", case["base"])
        _change(flagged, case["flagged"])
        report = _run(flagged, profile, task)
        assert name in _finding_ids(report), f"{name}: flagged fixture was not flagged"
        assert report.verdict == "fail"
        assert _result_for(report, name)["verdict"] == "fail"

        clean = _repo(tmp_path, f"{name}-clean", case["base"])
        _change(clean, case["clean"])
        report = _run(clean, profile, task)
        assert name not in _finding_ids(report), f"{name}: clean fixture was flagged"
        assert report.verdict == "pass"
        assert _result_for(report, name)["verdict"] == "pass"


# --- AC-4: sentinel results in the PR body and the attempt record ------------


REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = textwrap.dedent(
    """
    from impl import answer


    def test_ac_1():
        assert answer() == 42
    """
)
REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})


def _body_task() -> dict:
    return {
        "id": "T-1",
        "title": "Make answer() return 42",
        "source": "docs/T-1.md",
        "risk": [],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 10},
        "acceptance": [
            {"id": "AC-1", "text": "answer() returns 42",
             "check": {"type": "test", "ref": REF}}
        ],
    }


_SENTINEL_RESULTS = [
    {"name": "credential_added", "kind": "builtin", "verdict": "pass"},
    {"name": "skip_marker_added", "kind": "builtin", "verdict": "pass"},
    {"name": "assertion_removed", "kind": "builtin", "verdict": "pass"},
    {"name": "lint_suppression_added", "kind": "builtin", "verdict": "fail",
     "detail": "src/pay/charge.py:3: x = 2  # noqa"},
    {"name": "payments invariant", "kind": "project", "verdict": "fail",
     "risk_flag": "payments", "detail": "exit 1: invariant violated"},
    {"name": "auth invariant", "kind": "project", "verdict": "skipped",
     "risk_flag": "auth", "detail": "risk path not touched"},
]


def _l0_dict(sentinel_results) -> dict:
    return {
        "layer": "L0",
        "verdict": "fail" if any(r["verdict"] == "fail" for r in sentinel_results) else "pass",
        "findings": [],
        "escalations": [],
        "risk_flags_added": [],
        "commands": [],
        "preexisting_notes": [],
        "sentinel_results": sentinel_results,
    }


def test_ac_4(tmp_path):
    # (a) the PR body renders every sentinel result, pass or fail
    reports = {"L0": _l0_dict(_SENTINEL_RESULTS), "L1": {"layer": "L1", "verdict": "pass"},
               "L2": {"layer": "L2", "verdict": "pass"}}
    body = build_pr_body(
        _body_task(), {"repo": {"default_branch": "main"}},
        branch="feat/x", base_sha="b" * 40, head_sha="a" * 40,
        changed_paths=["src/pay/charge.py"], reports=reports,
        l1_report=L1Report(verdict="pass"), attempt=1,
    )
    match = re.search(r"^## Sentinels\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    assert match, f"no Sentinels section in body:\n{body}"
    section = match.group(1)
    for result in _SENTINEL_RESULTS:
        assert result["name"] in section, f"{result['name']} missing from the section"
        assert result["verdict"] in section
    assert "src/pay/charge.py" in section  # the failing detail is visible
    check_pr_body(body)
    with pytest.raises(PRBodyError):
        check_pr_body(re.sub(r"^## Sentinels\n.*?\n\n", "", body, count=1, flags=re.M | re.S))

    # (b) end to end, a passing run records sentinels in the attempt and PR body
    env = Env(tmp_path)
    seats = Seats()
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    attempt = env.task()["attempts"][-1]
    assert attempt["outcome"] == "pass"
    recorded = attempt["sentinels"]
    assert {r["name"] for r in recorded} == set(BUILTIN_SENTINELS)
    assert all(r["verdict"] == "pass" for r in recorded)
    assert "## Sentinels" in env.prs[0].body
    assert "credential_added" in env.prs[0].body

    # (c) end to end, a failing project sentinel escalates with the results
    env = Env(
        tmp_path,
        name="escalated",
        tasks=[_e2e_task(risk=["payments"])],
        profile_extra={"sentinels": {"payments": [{
            "command": "false", "description": "payments invariant",
        }]}},
    )
    seats = Seats(extra_files={"src/pay/charge.py": "x = 2\n"})
    result = env.run(seats)
    assert result.outcome == "needs_human", result
    assert result.escalation == "sentinel_failed"
    attempt = env.task()["attempts"][-1]
    assert attempt["escalation"] == "sentinel_failed"
    entry = next(r for r in attempt["sentinels"] if r["kind"] == "project")
    assert entry["verdict"] == "fail"
    assert entry["risk_flag"] == "payments"


# ------------------------------------------------- end-to-end harness (f10) --


def _e2e_task(task_id: str = "T-1", *, risk=()) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42", "check": {"type": "test", "ref": REF}}
    ]
    return {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": list(risk),
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-20T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 10},
        "attempts": [],
    }


class Env:
    """One fixture project: local repo + bare remote + factory root.

    Each instance gets its own subdirectory of ``tmp_path`` — a test may
    build several (a passing run and an escalated one) side by side.
    """

    def __init__(self, tmp_path: Path, tasks=None, profile_extra=None, name="e2e"):
        self.tmp = tmp_path / name
        self.tmp.mkdir()
        self.root = self.tmp / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
        (self.root / "holdouts" / "fixture" / "scenario.md").write_text("HOLDOUT\n")
        self.repo = self.tmp / "proj"
        self.remote = self.tmp / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
        profile = {
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
            "paths": {
                "risk": {"payments": ["src/pay/**"]},
                "protected": [".factory/**"],
            },
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        profile.update(profile_extra or {})
        self.profile = profile
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        self.profile_path = factory_dir / "project.yaml"
        self.profile_path.write_text(yaml.safe_dump(profile))
        self.queue_path = factory_dir / "tasks.json"
        doc = {"version": 1, "project": "fixture", "tasks": tasks or [_e2e_task()]}
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
        from factory.state import TaskQueue

        return TaskQueue(self.queue_path).get(task_id)

    def run(self, session_runner, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
        return run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )


class Seats:
    """Stub seats with ``run_session_sandboxed``'s signature (f10 pattern)."""

    def __init__(self, answer=42, result="Implemented answer().", review=REVIEW_PASS,
                 extra_files=None):
        self.answer = answer
        self.result = result
        self.review = review
        self.extra_files = extra_files or {}

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        from factory.session import SessionRecord

        run_dir = Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text(f"def answer():\n    return {self.answer}\n")
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            for rel, text in self.extra_files.items():
                target = run_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40}, result=self.result,
            )
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10}, result=self.review,
        )


# --------------------------------------------------------- extra coverage --


def test_credential_patterns_flag_known_shapes_and_ignores_prose():
    """The credential shapes F-30 will reuse for redaction, through the
    real scanning API: known token/key/private-key forms flag; prose,
    short values and env lookups do not."""
    flagged = [
        'key = "-----BEGIN RSA PRIVATE KEY-----"',
        "aws_key = AKIAIOSFODNN7EXAMPLE",
        'token = "ghp_' + "a" * 36 + '"',
        'stripe = "sk_live_' + "b" * 24 + '"',
        'api_key: "super-secret-value-123456"',
    ]
    clean = [
        "the token was rotated on Tuesday",
        'password = "short"',
        'api_key = os.getenv("API_KEY")',
        'secret = "correct horse battery staple"',
    ]
    changes = [
        FileChange("src/config.py", "M", added_lines=[(i + 1, text)])
        for i, text in enumerate(flagged + clean)
    ]
    findings = builtin_sentinel_findings(changes, {})
    lines = {f.line for f in findings if f.id == "credential_added"}
    assert lines == set(range(1, len(flagged) + 1))
    # the finding must never echo the credential itself: it rides into
    # reports, prompts and PR bodies, so the text names the shape only
    secrets = [
        "-----BEGIN RSA PRIVATE KEY-----",
        "AKIAIOSFODNN7EXAMPLE",
        "a" * 36,  # the ghp_ token body
        "b" * 24,  # the sk_live_ key body
        "super-secret-value-123456",
    ]
    for finding in findings:
        for secret in secrets:
            assert secret not in finding.text


def test_lint_suppression_covers_noqa_type_ignore_and_lint_disables():
    risk = {"payments": ["src/pay/**"]}
    lines = [
        "x = 2  # noqa",
        "y = 3  # type: ignore",
        "z = 4  // eslint-disable-next-line no-console",
        "w = 5  # ruff: noqa",
    ]
    changes = [FileChange("src/pay/charge.py", "M", added_lines=list(enumerate(lines, 1)))]
    findings = builtin_sentinel_findings(changes, risk)
    flagged = {f.line for f in findings if f.id == "lint_suppression_added"}
    assert flagged == {1, 2, 3, 4}
    # the same additions outside every risk path flag nothing
    assert builtin_sentinel_findings(changes, {}) == []


def test_matched_risk_flags_count_renamed_old_paths():
    risk = {"payments": ["src/pay/**"]}
    changes = [FileChange("src/general.py", "R", old_path="src/pay/charge.py")]
    assert matched_risk_flags(risk, changes) == {"payments"}


def test_escalation_enum_has_sentinel_failed():
    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "schema" / "tasks.schema.json").read_text()
    )
    assert "sentinel_failed" in schema["$defs"]["escalation"]["enum"]


def test_l0_report_serialises_sentinel_results(tmp_path):
    repo = _repo(tmp_path, "serialise", {"README.md": "x\n"})
    _change(repo, {"README.md": "y\n"})
    report = _run(repo)
    as_dict = report.to_dict()
    assert {r["name"] for r in as_dict["sentinel_results"]} == set(BUILTIN_SENTINELS)
    assert all(r["verdict"] == "pass" for r in as_dict["sentinel_results"])
