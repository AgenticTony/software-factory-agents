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
    PromptError,
    assemble_implementer_prompt,
    assemble_reviewer_prompt,
    prompt_hash,
    prompt_versions,
)
from factory.session import build_options, run_session

REPO = Path(__file__).resolve().parent.parent


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------ pins --
# The JobFinderOS pattern (WO F-09): a prompt file may only change together
# with its pin here — test_ac_2 fails until the pin is updated, so a silent
# prompt edit can never ride a green run.
PROMPT_PINS = {
    IMPLEMENTER_FILE: "15f5a0206c3016334173471786af4a4efbd28b1cd0b3b3981cb29b39dca316d6",
    REVIEWER_FILE: "4bbd3cf891684e8b6dccbf6cc63c864451284bea01ca07c382eee3acb79d5a0c",
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
            {"verdict": "fail", "findings": [{"id": "F", "axis": "spec", "text": "x"}]}
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
        json.dumps({"verdict": "fail", "findings": [{"axis": "spec", "text": "wrong"}]})
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
    assert set(versions) == {"implementer", "reviewer"}
    for seat, digest in versions.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert versions["implementer"] == prompt_hash(assemble_implementer_prompt(profile))
    assert versions["reviewer"] == prompt_hash(assemble_reviewer_prompt(profile))


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
