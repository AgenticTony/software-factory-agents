"""F-09 acceptance tests — BUILD-ORDER.md, work order F-09.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f09.py::test_ac_N``
means a test function with exactly that name. The extra tests below cover
paths the acceptance refs don't name: fenced/brace-span JSON extraction,
verdict-key and axis-value enforcement, L2 report serialisation, baseline-only
reviewer assembly for projects without ``standards``, a missing standards file
refusing assembly, ``prompt_versions`` hashes, the red_reason classification
matrix on real pytest output shapes, ``red_reason`` staying None for a
proves-nothing ref, and the session plumbing that carries an assembled
system prompt into a seat's options.
"""

import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from claude_agent_sdk import ResultMessage

from factory.contract import ContractError, load_profile, load_tasks
from factory.l1 import run_l1
from factory.l2 import L2Report, parse_verdict
from factory.prompts import (
    IMPLEMENTER_FILE,
    PROMPTS_DIR,
    REVIEWER_FILE,
    VERIFIER_FILE,
    PromptError,
    assemble_implementer_prompt,
    assemble_reviewer_prompt,
    assemble_verifier_prompt,
    prompt_hash,
    prompt_versions,
)
from factory.sandbox import can_apply_seatbelt
from factory.session import build_options, run_session

REPO = Path(__file__).resolve().parent.parent


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------ pins --
# The JobFinderOS pattern (WO F-09): a prompt file may only change together
# with its pin here — test_ac_2 fails until the pin is updated, so a silent
# prompt edit can never ride a green run.
PROMPT_PINS = {
    # F-42; re-pinned for AC-3 (the attribution and sandbox-bypass rules).
    IMPLEMENTER_FILE: "4855e4ba3ed3a9af252690e6913ffceefec5ba2582dc1496c7c08bf1185c6538",
    # F-41; re-pinned for AC-6 (a surviving major fails the round).
    REVIEWER_FILE: "1306ee568d0f09b02aef2c34694d61e343163a1abde54d19001fba9f55acba22",
    # F-38; re-pinned in its review round (the unanimous-rule paragraph).
    VERIFIER_FILE: "d7e5db822401e928ef64d8c032a34b66c3505f0f0fc115ca67fd0d8ea92c694b",
}


# ------------------------------------------------------------- fixtures --

def _fixture_profile(tmp_path: Path, standards: tuple[str, ...] | None = None) -> dict:
    """A profile dict shaped like a loaded project.yaml, with a real repo
    directory holding one standards doc when ``standards`` names any."""
    repo = tmp_path / "proj"
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "STANDARDS.md").write_text(
        "# Fixture standards\n\n- Use kebab-case filenames everywhere.\n"
    )
    profile = {
        "name": "fixture",
        "repo": {
            "path": str(repo),
            "default_branch": "main",
            "remote": "git@example:fixture/proj.git",
        },
        "commands": {"setup": "pip install -e .", "test": "pytest -q"},
        "paths": {"risk": {}, "protected": ["schema/*"]},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }
    if standards is not None:
        profile["standards"] = list(standards)
    return profile


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
    """Add a ``feature`` branch commit with the given changes."""
    _git(repo, "checkout", "-b", "feature")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    _git(repo, "checkout", "main")


def _task_with_ref(ref: str) -> dict:
    return {
        "id": "T-1",
        "acceptance": [
            {"id": "AC-1", "text": "it works", "check": {"type": "test", "ref": ref}}
        ],
    }


L1_PROFILE = {"name": "fixture", "commands": {}}

COLLECTION_TEST = "from impl import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"
ASSERTION_TEST = "from impl import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"


# ------------------------------------------------------------ acceptance --

def test_ac_1():
    # AC-1: reviewer output that isn't valid verdict JSON is treated as FAIL.
    report = parse_verdict("Looks good to me — no blocking issues, ship it.")
    assert isinstance(report, L2Report)
    assert report.verdict == "fail"
    assert report.parse_error, "the failure must say why the verdict was invalid"
    assert any(f.id == "invalid_verdict_json" for f in report.findings)


def test_ac_2():
    # AC-2: the prompt-hash test fails if a prompt file changes without the
    # pin being updated. The pins below are the only sanctioned way to
    # change a prompt — edit the file, then copy the new hash here.
    for name, pin in PROMPT_PINS.items():
        path = PROMPTS_DIR / name
        assert path.is_file(), f"seat prompt {name} is missing"
        assert re.fullmatch(r"[0-9a-f]{64}", pin), f"pin for {name} is not a sha256"
        assert _sha(path.read_text()) == pin, (
            f"{name} changed without updating its pin in PROMPT_PINS — a prompt "
            "edit must carry its new hash (the JobFinderOS pattern)"
        )


def test_ac_3(tmp_path):
    # AC-3: the reviewer prompt carries the VALIDATOR-SPEC L2 mandatory
    # checklist: real schema vs mock, scope, states, lifecycle.
    prompt = assemble_reviewer_prompt(_fixture_profile(tmp_path))
    assert "## Mandatory checklist" in prompt
    checklist = prompt.split("## Mandatory checklist", 1)[1].split("\n## ", 1)[0]
    lowered = checklist.lower()
    # real backend vs mock-only evidence
    assert "real schema" in lowered and "mock" in lowered
    # scope of the slice
    assert "scope" in lowered
    # loading / error / empty / offline states; offline reports unknown
    for word in ("loading", "error", "empty", "offline", "unknown"):
        assert word in lowered, f"checklist must cover the {word} state"
    # provider/subscription/cache lifecycle
    assert "invalidat" in lowered and "cancel" in lowered and "logout" in lowered


def test_ac_4():
    # AC-4: every finding carries axis (spec | standards); a finding without
    # one is invalid verdict JSON (AC-1 applies); a blocking finding on
    # either axis means FAIL, whatever the other axis says.
    no_axis = parse_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [{"id": "F1", "severity": "blocking", "text": "no axis"}],
            }
        )
    )
    assert no_axis.verdict == "fail"
    assert no_axis.parse_error

    bad_axis = parse_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [
                    {"id": "F1", "axis": "performance", "severity": "note", "text": "x"}
                ],
            }
        )
    )
    assert bad_axis.verdict == "fail"
    assert bad_axis.parse_error

    for axis in ("spec", "standards"):
        report = parse_verdict(
            json.dumps(
                {
                    "verdict": "pass",
                    "findings": [
                        {
                            "id": "F1",
                            "axis": axis,
                            "severity": "blocking",
                            "text": f"blocking on the {axis} axis",
                        }
                    ],
                }
            )
        )
        assert report.verdict == "fail", (
            f"a blocking {axis} finding must fail whatever the other axis says"
        )

    non_blocking = parse_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [
                    {"id": "F1", "axis": "standards", "severity": "note", "text": "minor"}
                ],
            }
        )
    )
    assert non_blocking.verdict == "pass"


def test_ac_5(tmp_path):
    # AC-5: the assembled reviewer prompt contains the smell baseline, the
    # test-quality checks and the project's standards files; the assembled
    # implementer prompt contains none of the standards files.
    profile = _fixture_profile(tmp_path, standards=("docs/STANDARDS.md",))
    standards_text = (
        Path(profile["repo"]["path"]) / profile["standards"][0]
    ).read_text()

    reviewer = assemble_reviewer_prompt(profile)
    assert standards_text in reviewer, "the project's standards files must ride in"
    assert "Fowler" in reviewer, "the smell baseline must ride in"
    assert "Duplicated Code" in reviewer and "Refused Bequest" in reviewer
    assert "independent source" in reviewer, "the test-quality checks must ride in"

    implementer = assemble_implementer_prompt(profile)
    assert standards_text not in implementer, (
        "standards live with the reviewer, never the implementer"
    )
    assert "Fowler" not in implementer and "Duplicated Code" not in implementer


def test_ac_6(tmp_path):
    # AC-6: project.schema.json accepts optional standards: [string];
    # tasks.schema.json accepts optional per-task out_of_scope: [string];
    # unknown keys are still rejected. (Written red before the schema
    # change: both accept-halves fail against the pre-F-09 schemas.)
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()

    def write_profile(name: str, extra: dict | None = None) -> Path:
        profile = _fixture_profile(tmp_path / "raw" / name)
        profile["repo"]["path"] = "/repos/fixture"
        if extra:
            profile.update(extra)
        path = profile_dir / f"{name}.yaml"
        path.write_text(yaml.safe_dump(profile))
        return path

    with_standards = load_profile(
        write_profile("with-standards", extra={"standards": ["CODING_STANDARDS.md"]})
    )
    assert with_standards["standards"] == ["CODING_STANDARDS.md"]
    load_profile(write_profile("without-standards"))  # still optional
    with pytest.raises(ContractError):
        load_profile(write_profile("unknown-key", extra={"bogus": ["x"]}))

    def write_tasks(name: str, task_extra: dict | None = None) -> Path:
        task = {
            "id": "T-1",
            "title": "fixture task",
            "source": "docs/fixture.md#t-1",
            "kind": "build",
            "priority": "P1",
            "status": "draft",
            "depends_on": [],
            "risk": [],
            "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
            "acceptance": [
                {
                    "id": "AC-1",
                    "text": "it works",
                    "check": {"type": "test", "ref": "tests/x.py::test_works"},
                }
            ],
            "budget": {
                "size": "S",
                "max_attempts": 3,
                "max_turns": 80,
                "wall_clock_min": 60,
            },
        }
        if task_extra:
            task.update(task_extra)
        path = profile_dir / f"{name}.json"
        path.write_text(
            json.dumps({"version": 1, "project": "fixture", "tasks": [task]})
        )
        return path

    with_out_of_scope = load_tasks(
        write_tasks("with-oos", task_extra={"out_of_scope": ["the settings page"]})
    )
    assert with_out_of_scope["tasks"][0]["out_of_scope"] == ["the settings page"]
    load_tasks(write_tasks("without-oos"))  # still optional
    with pytest.raises(ContractError):
        load_tasks(write_tasks("unknown-task-key", task_extra={"bogus": True}))


def test_ac_7(tmp_path):
    # AC-7: the L1 report records a red_reason per test ref — collection for
    # a test importing a not-yet-existing module, assertion for a failing
    # assert — and the verdict doesn't depend on it.
    task = _task_with_ref("tests/test_answer.py::test_answer")

    collection_repo = _repo(
        tmp_path, "collection", {"tests/test_answer.py": COLLECTION_TEST}
    )
    _change(collection_repo, files={"impl.py": "def answer():\n    return 42\n"})
    collection = run_l1(L1_PROFILE, collection_repo, "main", "feature", task)
    assert collection.verdict == "pass", "red by collection is still valid red-first"
    assert (
        collection.red_reasons["tests/test_answer.py::test_answer"] == "collection"
    ), "a module-level import of a missing module is a collection red"

    assertion_repo = _repo(
        tmp_path,
        "assertion",
        {
            "impl.py": "def answer():\n    return 41\n",
            "tests/test_answer.py": ASSERTION_TEST,
        },
    )
    _change(assertion_repo, files={"impl.py": "def answer():\n    return 42\n"})
    assertion = run_l1(L1_PROFILE, assertion_repo, "main", "feature", task)
    assert assertion.verdict == "pass"
    assert (
        assertion.red_reasons["tests/test_answer.py::test_answer"] == "assertion"
    ), "a failing assert is an assertion red"

    assert "red_reasons" in collection.to_dict()


def test_ac_8():
    # AC-8: every vendored prompt file sits under prompts/vendor/ next to its
    # upstream LICENSE and the source commit SHA.
    vendor = PROMPTS_DIR / "vendor"
    assert vendor.is_dir(), "prompts/vendor/ is missing"
    sources = [d for d in vendor.iterdir() if d.is_dir()]
    assert sources, "prompts/vendor/ holds no vendored sources"
    for source in sources:
        license_file = source / "LICENSE"
        assert license_file.is_file(), f"{source.name} carries no upstream LICENSE"
        assert license_file.read_text().strip(), f"{source.name}'s LICENSE is empty"
        sha_file = source / "SOURCE_SHA"
        assert sha_file.is_file(), f"{source.name} records no source commit SHA"
        assert re.fullmatch(
            r"[0-9a-f]{40}", sha_file.read_text().strip()
        ), f"{source.name}'s SOURCE_SHA is not a commit SHA"
        vendored = [
            p
            for p in source.iterdir()
            if p.is_file() and p.suffix == ".md" and p.name != "LICENSE"
        ]
        assert vendored, f"{source.name} vendors no prompt content"


# ------------------------------------------------------------------ L2 --

def test_fenced_json_is_extracted():
    report = parse_verdict(
        "Verdict follows.\n```json\n"
        + json.dumps(
            {
                "verdict": "fail",
                "findings": [
                    {"id": "F", "axis": "spec", "severity": "note", "text": "x"}
                ],
            }
        )
        + "\n```"
    )
    assert report.verdict == "fail"
    assert report.parse_error is None
    assert report.findings[0].axis == "spec"


def test_verdict_key_is_required():
    report = parse_verdict(json.dumps({"findings": []}))
    assert report.verdict == "fail"
    assert report.parse_error


def test_uppercase_verdict_is_normalised():
    report = parse_verdict('{"verdict": "PASS", "findings": []}')
    assert report.verdict == "pass"
    assert report.parse_error is None


def test_declared_fail_stays_fail():
    report = parse_verdict(
        json.dumps(
            {
                "verdict": "fail",
                "findings": [{"axis": "spec", "severity": "major", "text": "wrong"}],
            }
        )
    )
    assert report.verdict == "fail"
    assert report.parse_error is None


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict": "pass", "findings": "none"}',  # findings not a list
        '{"verdict": "pass", "findings": [42]}',  # finding not an object
        '{"verdict": "pass", "findings": [{"axis": "spec"}]}',  # no text
        '{"verdict": "later", "findings": []}',  # unknown verdict value
        '["pass"]',  # not an object
        '{"verdict": "pass", "findings": [{"axis": null, "text": "x"}]}',
    ],
)
def test_malformed_verdicts_are_failures(payload):
    report = parse_verdict(payload)
    assert report.verdict == "fail"
    assert report.parse_error
    assert any(f.id == "invalid_verdict_json" for f in report.findings)


def test_l2_report_serialises_axis_and_severity():
    report = parse_verdict(
        json.dumps(
            {
                "verdict": "fail",
                "findings": [
                    {
                        "id": "F-1",
                        "axis": "standards",
                        "severity": "blocking",
                        "file": "src/app.py",
                        "line": 7,
                        "text": "Speculative Generality",
                    }
                ],
            }
        )
    )
    data = report.to_dict()
    assert data["layer"] == "L2"
    assert data["verdict"] == "fail"
    assert data["findings"][0]["axis"] == "standards"
    assert data["findings"][0]["severity"] == "blocking"
    assert data["findings"][0]["file"] == "src/app.py"
    assert data["findings"][0]["line"] == 7


# ------------------------------------------------------------- prompts --

def test_reviewer_prompt_without_standards_runs_baseline_only(tmp_path):
    prompt = assemble_reviewer_prompt(_fixture_profile(tmp_path, standards=()))
    assert "no standards files" in prompt
    assert "Fowler" in prompt  # the baseline applies alone


def test_missing_standards_file_refuses_assembly(tmp_path):
    profile = _fixture_profile(tmp_path, standards=("docs/ABSENT.md",))
    with pytest.raises(PromptError):
        assemble_reviewer_prompt(profile)


def test_implementer_prompt_carries_project_specifics_but_no_standards(tmp_path):
    profile = _fixture_profile(tmp_path, standards=("docs/STANDARDS.md",))
    prompt = assemble_implementer_prompt(profile)
    assert "pytest -q" in prompt, "the project's test command rides in"
    assert "schema/*" in prompt, "the project's protected paths ride in"
    standards_text = (
        Path(profile["repo"]["path"]) / profile["standards"][0]
    ).read_text()
    assert standards_text not in prompt


def test_prompt_versions_hash_the_assembled_prompts(tmp_path):
    profile = _fixture_profile(tmp_path, standards=("docs/STANDARDS.md",))
    versions = prompt_versions(profile)
    # F-38: one entry per fan-out session — reviewer-1/reviewer-2 share the
    # assembled reviewer prompt — plus the verifier's own.
    assert set(versions) == {"implementer", "reviewer-1", "reviewer-2", "verifier"}
    for seat, digest in versions.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert versions["implementer"] == prompt_hash(assemble_implementer_prompt(profile))
    assert versions["reviewer-1"] == prompt_hash(assemble_reviewer_prompt(profile))
    assert versions["reviewer-2"] == versions["reviewer-1"]
    assert versions["verifier"] == prompt_hash(assemble_verifier_prompt(profile))


# ----------------------------------------------------------- red_reason --

def test_red_reason_classification_matrix():
    from factory.gates import CommandResult
    from factory.l1 import red_reason

    collection = CommandResult(
        "red:r",
        "pytest",
        2,
        "tests/test_answer.py:1: in <module>\nE   ModuleNotFoundError: No module "
        "named 'impl'\n=========================== short test summary info "
        "===========================\nERROR tests/test_answer.py\n1 error in 0.02s\n",
    )
    assert red_reason(collection) == "collection"

    assertion = CommandResult(
        "red:r",
        "pytest",
        1,
        "E       assert 41 == 42\n\ntests/test_answer.py:3: AssertionError\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_answer.py::test_answer - assert 41 == 42\n1 failed in 0.02s\n",
    )
    assert red_reason(assertion) == "assertion"

    runtime_error = CommandResult(
        "red:r",
        "pytest",
        1,
        "E   TypeError: boom\n\ntests/test_x.py:2: TypeError\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_x.py::test_answer - TypeError: boom\n1 failed in 0.02s\n",
    )
    assert runtime_error.exit_code == 1
    assert red_reason(runtime_error) == "unknown"

    green = CommandResult("red:r", "pytest", 0, "1 passed in 0.01s\n")
    assert red_reason(green) is None

    timed_out = CommandResult("red:r", "pytest", None, "", timed_out=True)
    assert red_reason(timed_out) is None


def test_red_reason_is_none_when_the_test_proves_nothing(tmp_path):
    task = _task_with_ref("tests/test_answer.py::test_answer")
    repo = _repo(
        tmp_path,
        "proves-nothing",
        {
            "impl.py": "def answer():\n    return 42\n",
            "tests/test_answer.py": ASSERTION_TEST,
        },
    )
    _change(repo, files={"impl.py": "def answer():\n    return 43\n"})
    report = run_l1(L1_PROFILE, repo, "main", "feature", task)
    assert report.verdict == "fail"  # the test passed on base — proves nothing
    assert report.red_reasons["tests/test_answer.py::test_answer"] is None


# ------------------------------------------------------------- session --

def test_build_options_carries_an_assembled_system_prompt(tmp_path):
    options = build_options(
        "implementer",
        tmp_path,
        10,
        base_url="https://api.z.ai/api/anthropic",
        token="t",
        system_prompt="ASSEMBLED SEAT PROMPT",
    )
    assert options.system_prompt == "ASSEMBLED SEAT PROMPT"


def test_build_options_without_override_keeps_the_seat_default(tmp_path):
    options = build_options(
        "implementer", tmp_path, 10, base_url="https://x", token="t"
    )
    assert options.system_prompt is None  # F-09 supplies prompts per run


def test_run_session_forwards_the_system_prompt(tmp_path):
    captured = {}

    async def _query(*, prompt, options, transport=None):
        captured["system_prompt"] = options.system_prompt
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            usage={},
            model_usage={},
            terminal_reason="completed",
        )

    record = asyncio.run(
        run_session(
            "implementer",
            "do the task",
            tmp_path,
            5,
            1,
            query_fn=_query,
            base_url="https://x",
            token="t",
            system_prompt="ASSEMBLED",
        )
    )
    assert record.status == "completed"
    assert captured["system_prompt"] == "ASSEMBLED"


# ------------------------------------------------- owner review round (PR #19) --
# Eight findings, 7 reproduced locally against bededb4 + 1 confirmed by
# code-reading; each fixed on this PR with its red test below.

def test_review1_standards_load_when_repo_path_uses_a_tilde(tmp_path, monkeypatch):
    # Finding 1: every real project.yaml writes repo.path as ~/Desktop/...,
    # and assembly never expanded the ~ — the first real project with
    # standards failed every L2 review. The tests passed only because they
    # used absolute temp paths.
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "proj"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "STANDARDS.md").write_text("# tilde-loaded standards\n")
    profile = _fixture_profile(tmp_path / "raw" / "f1", standards=("docs/STANDARDS.md",))
    profile["repo"]["path"] = "~/proj"
    prompt = assemble_reviewer_prompt(profile)
    assert "# tilde-loaded standards" in prompt


def test_review2_standards_cannot_read_outside_the_repo(tmp_path):
    # Finding 2: an absolute or ../ standards entry read ANY file and sent
    # it to the model — a secrets file landed in the reviewer prompt both
    # ways. Standards are repo-relative paths (TASK-CONTRACT §5); anything
    # resolving outside the repo (including via a symlink) refuses.
    repo = tmp_path / "proj"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "STANDARDS.md").write_text("# in-repo standards\n")
    secret = tmp_path / "SECRET.md"
    secret.write_text("SEKRIT-KEY-12345")
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "payload.md").write_text("OUTSIDE-PAYLOAD")
    (repo / "docs" / "link.md").symlink_to(outside_dir / "payload.md")

    def profile_with(entry: str) -> dict:
        profile = _fixture_profile(
            tmp_path / "raw" / "f2", standards=("docs/STANDARDS.md",)
        )
        profile["repo"]["path"] = str(repo)
        profile["standards"] = [entry]
        return profile

    prompt = assemble_reviewer_prompt(profile_with("docs/STANDARDS.md"))
    assert "# in-repo standards" in prompt  # the legit entry still loads
    for entry in (str(secret), "../SECRET.md", "docs/link.md"):
        with pytest.raises(PromptError):
            assemble_reviewer_prompt(profile_with(entry))


def test_review3_parse_verdict_none_is_a_fail_not_a_crash():
    # Finding 3: a failed or timed-out reviewer session records
    # result=None; parse_verdict(None) crashed with AttributeError, taking
    # the orchestrator down instead of stopping the change.
    report = parse_verdict(None)  # type: ignore[arg-type]
    assert report.verdict == "fail"
    assert report.parse_error
    assert any(f.id == "invalid_verdict_json" for f in report.findings)


@pytest.mark.parametrize("severity", ["blocker", "critical", "high", "urgent"])
def test_review4_unknown_severities_are_invalid_verdicts(severity):
    # Finding 4: a declared pass with a "blocker"/"critical"/"high" finding
    # passed — only the exact word "blocking" blocked. The prompt lists
    # four allowed values; the parser now enforces them (and requires the
    # field: the documented output shape always carries it).
    report = parse_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [
                    {"id": "F1", "axis": "spec", "severity": severity, "text": "x"}
                ],
            }
        )
    )
    assert report.verdict == "fail"
    assert report.parse_error


def test_review4_missing_severity_is_invalid_too():
    report = parse_verdict(
        json.dumps(
            {"verdict": "pass", "findings": [{"id": "F1", "axis": "spec", "text": "x"}]}
        )
    )
    assert report.verdict == "fail"
    assert report.parse_error


def test_review4_severity_case_is_normalised():
    report = parse_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "findings": [
                    {"id": "F1", "axis": "spec", "severity": "BLOCKING", "text": "x"}
                ],
            }
        )
    )
    assert report.verdict == "fail"  # BLOCKING still blocks
    assert report.parse_error is None


def test_review5_runtime_missing_name_reds_are_collection(tmp_path):
    # Finding 5: the most common red-first shape — a test that imports or
    # calls a function that doesn't exist yet — failed at RUNTIME (not at
    # collection), so red_reason said "unknown". Epistemically it is the
    # same class as a collection error: red only because a name was
    # missing, exactly the case the reviewer is told to scrutinise.
    task = _task_with_ref("tests/test_answer.py::test_answer")
    repo = _repo(
        tmp_path,
        "missing-name",
        {
            "tests/test_answer.py": (
                "def test_answer():\n"
                "    from impl import answer\n"
                "    assert answer() == 42\n"
            )
        },
    )
    _change(repo, files={"impl.py": "def answer():\n    return 42\n"})
    report = run_l1(L1_PROFILE, repo, "main", "feature", task)
    assert report.verdict == "pass"
    assert report.red_reasons["tests/test_answer.py::test_answer"] == "collection"


def test_review5_missing_name_classification_matrix():
    from factory.gates import CommandResult
    from factory.l1 import red_reason

    shapes = {
        "NameError": "E   NameError: name 'answer' is not defined\n"
        "FAILED tests/test_a.py::test_answer - NameError: name 'answer' is "
        "not defined\n1 failed in 0.02s\n",
        "AttributeError": "E   AttributeError: module 'impl' has no attribute "
        "'answer'\nFAILED tests/test_a.py::test_answer - AttributeError: "
        "module 'impl' has no attribute 'answer'\n1 failed in 0.02s\n",
        "ModuleNotFoundError": "E   ModuleNotFoundError: No module named "
        "'helpers'\nFAILED tests/test_a.py::test_answer - "
        "ModuleNotFoundError: No module named 'helpers'\n1 failed in 0.02s\n",
        "ImportError": "E   ImportError: cannot import name 'answer' from "
        "'impl'\nFAILED tests/test_a.py::test_answer - ImportError: cannot "
        "import name 'answer' from 'impl'\n1 failed in 0.02s\n",
    }
    for label, output in shapes.items():
        result = CommandResult("red:r", "pytest", 1, output)
        assert red_reason(result) == "collection", label

    # A genuine runtime failure is still unknown, and an assertion failure
    # quoting a NameError in its message stays an assertion red.
    genuine = CommandResult(
        "red:r",
        "pytest",
        1,
        "E   TypeError: boom\nFAILED tests/test_a.py::t - TypeError: boom\n",
    )
    assert red_reason(genuine) == "unknown"
    quoted = CommandResult(
        "red:r",
        "pytest",
        1,
        "E   AssertionError: assert 'NameError: name ' in out\n"
        "FAILED tests/test_a.py::t - AssertionError: assert ...\n",
    )
    assert red_reason(quoted) == "assertion"


def test_review6_the_real_verdict_wins_over_an_earlier_example():
    # Finding 6: a JSON snippet quoted earlier in the output (an example,
    # or restating the format) parsed before the real verdict at the end —
    # a false FAIL. Extraction now prefers the LAST candidate.
    example = json.dumps(
        {
            "verdict": "fail",
            "findings": [
                {"id": "EX", "axis": "spec", "severity": "note", "text": "example"}
            ],
        }
    )
    real = json.dumps({"verdict": "pass", "findings": []})

    report = parse_verdict(
        f"An example verdict looks like:\n```json\n{example}\n```\n\n"
        f"The actual review:\n```json\n{real}\n```"
    )
    assert report.verdict == "pass"
    assert report.parse_error is None

    # And symmetrically: an example pass quoted early must not mask a real
    # fail (nor a raw trailing object an earlier real one).
    report = parse_verdict(
        f"```json\n{real}\n```\nthe diff is wrong, so:\n{example}"
    )
    assert report.verdict == "fail"


def test_review7_duplicate_keys_are_an_invalid_verdict():
    # Finding 7: json.loads keeps the LAST duplicate key, so
    # {"verdict":"fail", ..., "verdict":"pass"} silently parsed as pass.
    report = parse_verdict(
        '{"verdict": "fail", "findings": [], "verdict": "pass"}'
    )
    assert report.verdict == "fail"
    assert report.parse_error
    assert any(f.id == "invalid_verdict_json" for f in report.findings)

    dup_in_finding = parse_verdict(
        '{"verdict": "pass", "findings": [{"axis": "spec", "axis": "spec", '
        '"severity": "note", "text": "x"}]}'
    )
    assert dup_in_finding.verdict == "fail"
    assert dup_in_finding.parse_error


def test_review8_reviewer_seat_runs_headless():
    # Finding 8 (code-reading): the reviewer kept the default permission
    # mode, but a headless session has nobody to answer Bash permission
    # prompts — the seat could never run the checks its spec promises.
    # Same D3 reasoning as the implementer: the srt sandbox is the
    # permission system; read-only-ness comes from the disallowed edit
    # tools + the PreToolUse hooks, not from prompts nobody can answer.
    from factory.session import SEATS, build_options

    assert SEATS["reviewer"].permission_mode == "bypassPermissions"
    assert SEATS["reviewer"].disallowed_tools == ("Write", "Edit", "NotebookEdit")
    options = build_options(
        "reviewer", REPO, 10, base_url="https://x", token="t"
    )
    assert options.permission_mode == "bypassPermissions"
    assert set(options.disallowed_tools) == {"Write", "Edit", "NotebookEdit"}


# ---------------------------------------- owner review round 2 (PR #19) --
# Fixing finding 8 (reviewer bypassPermissions for headless Bash) exposed
# that the reviewer's Bash could WRITE the repo under review: overwrite
# source, edit tests, commit. The fix is two-layered — a read-only sandbox
# for the reviewer seat (writes confined to .home/.tmp) plus an
# orchestrator-side workspace-integrity check after every review.

def _sandbox_repo(tmp_path: Path) -> tuple[Path, dict]:
    """A fixture run dir (git repo with tracked source + test) + profile."""
    repo = tmp_path / "run-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text('def app():\n    return "ok"\n')
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    profile = {
        "name": "fixture",
        "repo": {"path": str(repo)},
        "commands": {},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
        "network_allowlist": [],
    }
    return repo, profile


def test_review9_reviewer_sandbox_is_read_only(tmp_path):
    # The owner's repro list, at the sandbox layer: with read_only settings
    # the reviewer's Bash cannot overwrite source, edit tests, write via
    # python, or commit — and scratch still works in .home/.tmp. The
    # writable scratch lives OUTSIDE the run dir (run/.home and run/.tmp
    # are symlinks to it): on Linux the runs_root read-deny makes srt
    # re-bind the run dir read-only AFTER the writable sub-directory binds,
    # burying them — only the outside spellings stay live there.
    from factory.sandbox import build_settings, run_sandboxed, write_settings

    repo, profile = _sandbox_repo(tmp_path)
    from factory.session import _reviewer_scratch

    scratch_home, scratch_tmp = _reviewer_scratch(
        tmp_path / "reviewer-settings.json", repo
    )
    settings = build_settings(
        profile,
        repo,
        model_endpoint="api.z.ai",
        toolchain=[],
        factory_root=tmp_path,
        read_only=True,
        writable_roots=(scratch_home, scratch_tmp),
    )
    settings_path = tmp_path / "reviewer-settings.json"
    write_settings(settings, settings_path)
    assert (repo / ".home").is_symlink() and (repo / ".tmp").is_symlink()

    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    probes = [
        (["sh", "-c", "echo broken > src/app.py"], repo / "src" / "app.py"),
        (["sed", "-i", "s/True/False/", "tests/test_a.py"], repo / "tests" / "test_a.py"),
        (["python3", "-c", "open('src/new.py', 'w').write('x')"], repo / "src" / "new.py"),
        (["sh", "-c", "git add -A; git commit -qm reviewer-edit"], None),
    ]
    for cmd, guarded in probes:
        proc = run_sandboxed(cmd, repo, settings_path, timeout=60)
        assert proc.returncode != 0, f"write probe was allowed: {cmd}"
        if guarded is not None and guarded.name != "new.py":
            assert "ok" in guarded.read_text() or "True" in guarded.read_text(), (
                f"{guarded} was modified"
            )
    assert not (repo / "src" / "new.py").exists()
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert head_after == head_before, "the reviewer committed inside the run dir"

    scratch = run_sandboxed(
        ["sh", "-c", "echo note > .tmp/scratch && echo saved > .home/saved"],
        repo, settings_path, timeout=60,
    )
    assert scratch.returncode == 0, "scratch writes in .home/.tmp must still work"


def test_review9_implementer_sandbox_still_writes(tmp_path):
    # The other direction: the implementer's settings keep the run dir
    # writable — the read-only mode is the reviewer's, not a global change.
    from factory.sandbox import build_settings, run_sandboxed, write_settings

    repo, profile = _sandbox_repo(tmp_path)
    settings = build_settings(
        profile, repo, model_endpoint="api.z.ai", toolchain=[], factory_root=tmp_path
    )
    assert str(repo) in settings["filesystem"]["allowWrite"]
    settings_path = tmp_path / "implementer-settings.json"
    write_settings(settings, settings_path)
    proc = run_sandboxed(
        ["sh", "-c", "echo written > src/app.py"], repo, settings_path, timeout=60
    )
    assert proc.returncode == 0
    assert (repo / "src" / "app.py").read_text().strip() == "written"
def test_review9_session_builds_read_only_settings_for_the_reviewer(
    tmp_path, monkeypatch
):
    # Wiring: run_session_sandboxed passes read_only=True to build_settings
    # exactly for the reviewer seat (and not for the implementer).
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    calls: list[bool | None] = []

    def fake_build(settings_profile, run_dir, **kwargs):
        calls.append(kwargs.get("read_only"))
        return {}

    def fake_run_sandboxed(cmd, run_dir, settings_path, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"seat": "x", "status": "completed"}), stderr=""
        )

    monkeypatch.setattr(session_module, "build_settings", fake_build)
    monkeypatch.setattr(session_module, "run_sandboxed", fake_run_sandboxed)
    settings_path = tmp_path / "s.json"

    session_module.run_session_sandboxed(
        "reviewer", "review it", repo, profile,
        settings_path=settings_path, max_turns=5, timeout_min=1,
        token="t", cli_path=Path("/bin/echo"),
    )
    session_module.run_session_sandboxed(
        "implementer", "build it", repo, profile,
        settings_path=settings_path, max_turns=5, timeout_min=1,
        token="t", cli_path=Path("/bin/echo"),
    )
    assert calls == [True, False]


def test_review9_workspace_integrity_checked_after_review(tmp_path, monkeypatch):
    # The orchestrator-side backstop: even if some future path lets the
    # reviewer write (a settings bug, an srt regression), HEAD and the
    # working tree are verified unchanged after every reviewer session — a
    # mutation fails the review (reason reviewer_mutated_workspace), the
    # verdict text is not trusted, and the workspace is restored.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout

    def mutating_run(cmd, run_dir, settings_path, **kwargs):
        # "The reviewer session": edits a tracked file, commits it, and
        # drops an untracked file — the owner-reproduced capability.
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qam", "reviewer fix"],
            capture_output=True,
        )
        (repo / "src" / "smuggled.py").write_text("injected = True\n")
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps(
                {"seat": "reviewer", "status": "completed",
                 "result": '{"verdict": "pass", "findings": []}'}
            ),
            stderr="",
        )

    monkeypatch.setattr(session_module, "run_sandboxed", mutating_run)
    record = session_module.run_session_sandboxed(
        "reviewer", "review it", repo, profile,
        settings_path=tmp_path / "s.json", factory_root=tmp_path,
        max_turns=5, timeout_min=1,
        token="t", cli_path=Path("/bin/echo"),
    )
    assert record.status == "failed"
    assert record.reason == "reviewer_mutated_workspace"

    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert head_after == head_before, "the workspace must be restored"
    porcelain = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == "", f"tree not clean after restore: {porcelain!r}"
    assert not (repo / "src" / "smuggled.py").exists()


def test_review9_clean_review_passes_the_integrity_check(tmp_path, monkeypatch):
    # The quiet path: a review that only reads returns its record
    # untouched, and the implementer is never subjected to the check.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout

    def quiet_run(cmd, run_dir, settings_path, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"seat": "x", "status": "completed", "num_turns": 2}),
            stderr="",
        )

    monkeypatch.setattr(session_module, "run_sandboxed", quiet_run)
    for seat in ("reviewer", "implementer"):
        record = session_module.run_session_sandboxed(
            seat, "do it", repo, profile,
            settings_path=tmp_path / "s.json", factory_root=tmp_path,
            max_turns=5, timeout_min=1,
            token="t", cli_path=Path("/bin/echo"),
        )
        assert record.status == "completed"
        assert record.reason is None
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert head_after == head_before


# ---------------------------------------- owner review round 3 (PR #19) --
# The round-2 symlink scratch left two residues: the F-05 exclude patterns
# (trailing slash) don't match symlinks, so `git add -A` would commit the
# two dangling links; and the symlinks outlive the review, so a later
# implementer run in the same run dir writes $HOME/$TMPDIR through links
# pointing OUTSIDE its sandbox — denied.

def test_review10_excludes_cover_symlinked_scratch(tmp_path):
    # Finding 1: `/.home/` (trailing slash) matches directories only, and a
    # symlink is a file to git — the reviewer's scratch links showed as
    # `?? .home`/`?? .tmp` and would ride `git add -A` into the PR. The
    # seeded patterns must match either form.
    from factory.runs import _seed_run_excludes

    repo, _ = _sandbox_repo(tmp_path)
    scratch = tmp_path / "scratch"
    (scratch / "home").mkdir(parents=True)
    (scratch / "tmp").mkdir(parents=True)
    (repo / ".home").symlink_to(scratch / "home")
    (repo / ".tmp").symlink_to(scratch / "tmp")
    _seed_run_excludes(repo, None)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == "", f"symlinked scratch leaks into status: {status!r}"


def test_review10_excludes_still_cover_real_scratch_dirs(tmp_path):
    # The other form must keep working: plain .home/.tmp directories (the
    # F-05 shape, and the post-review shape after round 3's teardown).
    from factory.runs import _seed_run_excludes

    repo, _ = _sandbox_repo(tmp_path)
    (repo / ".home").mkdir()
    (repo / ".tmp").mkdir()
    (repo / ".home" / "pip-cache").write_text("cache")
    (repo / ".tmp" / "scratch").write_text("tmp")
    _seed_run_excludes(repo, None)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == "", f"real scratch dirs leak into status: {status!r}"


def test_review10_review_leaves_real_scratch_dirs(tmp_path, monkeypatch):
    # Finding 2: the symlinks must not outlive the review — a later
    # implementer run in the same run dir would write $HOME/$TMPDIR through
    # links pointing outside its sandbox. The review leaves the plain
    # directories the F-05 flow expects.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout

    def quiet_run(cmd, run_dir, settings_path, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"seat": "reviewer", "status": "completed"}),
            stderr="",
        )

    monkeypatch.setattr(session_module, "run_sandboxed", quiet_run)
    record = session_module.run_session_sandboxed(
        "reviewer", "review it", repo, profile,
        settings_path=tmp_path / "s.json", factory_root=tmp_path,
        max_turns=5, timeout_min=1,
        token="t", cli_path=Path("/bin/echo"),
    )
    assert record.status == "completed"
    assert (repo / ".home").is_dir() and not (repo / ".home").is_symlink()
    assert (repo / ".tmp").is_dir() and not (repo / ".tmp").is_symlink()
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert head_after == head_before


def test_review10_implementer_scratch_writable_after_a_review(tmp_path, monkeypatch):
    # The owner's exact repro shape: after a reviewer session leaves real
    # dirs, an implementer-style sandboxed write to $HOME and $TMPDIR inside
    # the same run dir must succeed (source writes stay the implementer's to
    # make — that seat keeps allowWrite=[run_dir]).
    import factory.session as session_module
    from factory.sandbox import build_settings, run_sandboxed, write_settings

    repo, profile = _sandbox_repo(tmp_path)

    def quiet_run(cmd, run_dir, settings_path, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"seat": "reviewer", "status": "completed"}),
            stderr="",
        )

    monkeypatch.setattr(session_module, "run_sandboxed", quiet_run)
    session_module.run_session_sandboxed(
        "reviewer", "review it", repo, profile,
        settings_path=tmp_path / "s.json", factory_root=tmp_path,
        max_turns=5, timeout_min=1,
        token="t", cli_path=Path("/bin/echo"),
    )
    # The next run in the same dir is an implementer: writable run dir.
    settings = build_settings(
        profile, repo, model_endpoint="api.z.ai",
        toolchain=[], factory_root=tmp_path,
    )
    settings_path = tmp_path / "implementer.json"
    write_settings(settings, settings_path)
    proc = run_sandboxed(
        ["sh", "-c", "echo state > .home/cc.json && echo t > .tmp/build"],
        repo, settings_path, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert (repo / ".home" / "cc.json").read_text().strip() == "state"
    assert (repo / ".tmp" / "build").read_text().strip() == "t"


# --- review round 4 (backstop on every exit path; refs, branch, ignored) ---


def _rev(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_and_break(repo: Path) -> None:
    """What a mutating reviewer does: edit a tracked file and commit it."""
    (repo / "src" / "app.py").write_text("BROKEN\n")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qam", "reviewer tweak"],
        capture_output=True, check=True,
    )


def _review(session_module, repo, profile, tmp_path):
    return session_module.run_session_sandboxed(
        "reviewer", "review it", repo, profile,
        settings_path=tmp_path / "s.json", factory_root=tmp_path,
        max_turns=5, timeout_min=1, token="t", cli_path=Path("/bin/echo"),
    )


def test_review11_backstop_restores_when_the_review_times_out(tmp_path, monkeypatch):
    # The backstop must hold on the exits a misbehaving reviewer is most
    # likely to take: an outer timeout still propagates, but only after
    # HEAD and the tree are restored.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    head = _rev(repo, "rev-parse", "HEAD")

    def mutate_then_hang(cmd, run_dir, settings_path, **kwargs):
        _commit_and_break(repo)
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(session_module, "run_sandboxed", mutate_then_hang)
    with pytest.raises(subprocess.TimeoutExpired):
        _review(session_module, repo, profile, tmp_path)
    assert _rev(repo, "rev-parse", "HEAD") == head
    assert (repo / "src" / "app.py").read_text() == 'def app():\n    return "ok"\n'


def test_review11_backstop_restores_when_the_entry_returns_no_record(
    tmp_path, monkeypatch
):
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    head = _rev(repo, "rev-parse", "HEAD")

    def mutate_then_crash(cmd, run_dir, settings_path, **kwargs):
        _commit_and_break(repo)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="crash")

    monkeypatch.setattr(session_module, "run_sandboxed", mutate_then_crash)
    with pytest.raises(session_module.SessionConfigError):
        _review(session_module, repo, profile, tmp_path)
    assert _rev(repo, "rev-parse", "HEAD") == head
    assert (repo / "src" / "app.py").read_text() == 'def app():\n    return "ok"\n'


def test_review11_scratch_torn_down_when_setup_fails(tmp_path, monkeypatch):
    # The scratch symlinks are created before settings are built; a setup
    # failure after them must still leave plain .home/.tmp directories.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)

    def broken_settings(*args, **kwargs):
        raise session_module.SessionConfigError("settings exploded")

    monkeypatch.setattr(session_module, "build_settings", broken_settings)
    with pytest.raises(session_module.SessionConfigError):
        _review(session_module, repo, profile, tmp_path)
    for name in (".home", ".tmp"):
        assert (repo / name).is_dir() and not (repo / name).is_symlink(), name


def _quiet_record(cmd):
    return subprocess.CompletedProcess(
        cmd, 0, stdout=json.dumps({"seat": "reviewer", "status": "completed"}),
        stderr="",
    )


def test_review11_new_branch_counts_as_mutation(tmp_path, monkeypatch):
    # HEAD and porcelain alone miss a new ref: a branch or tag the reviewer
    # creates is a mutation, and the restore removes it.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)

    def make_branch(cmd, run_dir, settings_path, **kwargs):
        subprocess.run(["git", "-C", str(repo), "branch", "smuggled"], check=True)
        subprocess.run(["git", "-C", str(repo), "tag", "smuggled-tag"], check=True)
        return _quiet_record(cmd)

    monkeypatch.setattr(session_module, "run_sandboxed", make_branch)
    record = _review(session_module, repo, profile, tmp_path)
    assert (record.status, record.reason) == ("failed", "reviewer_mutated_workspace")
    assert "smuggled" not in _rev(repo, "branch", "--list", "smuggled")
    assert _rev(repo, "tag", "--list", "smuggled-tag") == ""


def test_review11_branch_switch_counts_as_mutation(tmp_path, monkeypatch):
    # Same commit, different branch: HEAD's sha is unchanged, but the run
    # is no longer on the branch the orchestrator pushes.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)

    def switch(cmd, run_dir, settings_path, **kwargs):
        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "other"], check=True)
        return _quiet_record(cmd)

    monkeypatch.setattr(session_module, "run_sandboxed", switch)
    record = _review(session_module, repo, profile, tmp_path)
    assert (record.status, record.reason) == ("failed", "reviewer_mutated_workspace")
    assert _rev(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert _rev(repo, "branch", "--list", "other") == ""


# INC-007 host-capability tests: these two write into a fixture repo's
# .git/info deliberately (the exclude file the guard must respect); a
# sandbox denies .git-internal writes by design, so nested they skip.
@pytest.mark.skipif(
    not can_apply_seatbelt(),
    reason="nested seatbelt cannot apply a profile (INC-007)",
)
def test_review11_ignored_artifact_counts_as_mutation(tmp_path, monkeypatch):
    # A file written into a gitignored dir is invisible to plain porcelain.
    # It counts as a mutation and is removed, while ignored files that
    # existed before the review (a warm cache) survive the restore.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text("/build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "keep.txt").write_text("warm cache")

    def drop_artifact(cmd, run_dir, settings_path, **kwargs):
        (repo / "build" / "out.bin").write_text("smuggled")
        return _quiet_record(cmd)

    monkeypatch.setattr(session_module, "run_sandboxed", drop_artifact)
    record = _review(session_module, repo, profile, tmp_path)
    assert (record.status, record.reason) == ("failed", "reviewer_mutated_workspace")
    assert not (repo / "build" / "out.bin").exists()
    assert (repo / "build" / "keep.txt").read_text() == "warm cache"


@pytest.mark.skipif(
    not can_apply_seatbelt(),
    reason="nested seatbelt cannot apply a profile (INC-007)",
)
def test_review11_quiet_review_with_preexisting_ignored_files_passes(
    tmp_path, monkeypatch
):
    # The guard must not flag the warm cache or scratch as a mutation.
    import factory.session as session_module

    repo, profile = _sandbox_repo(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text("/build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "keep.txt").write_text("warm cache")
    monkeypatch.setattr(
        session_module, "run_sandboxed",
        lambda cmd, run_dir, settings_path, **kw: _quiet_record(cmd),
    )
    record = _review(session_module, repo, profile, tmp_path)
    assert record.status == "completed"
