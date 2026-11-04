"""F-12 follow-up: the importer prompt names the project, and refused
session output is kept.

Two bugs from the JobFinderOS import (F-13 step 3 dogfood):

* ``factory import --repo ~/Desktop/jfos-factory`` was refused with
  "the proposal names project 'JobFinderOS'; this queue is 'jobfinderos'"
  — ``build_import_prompt`` showed a ``{"project": "<project name>"}``
  placeholder and never said the name, so the importer session guessed
  it from the repo directory. The name is known at the call site (the
  existing queue's, or the profile's for a fresh project).
* That refusal cost a full real session: ``cleanup_run`` deletes the
  run dir and the raw output was never kept, so there was nothing to
  re-review with ``factory import --proposal``.

The fix, red-first in this file: the prompt states the exact name in
the output contract and as a rule; and a proposal refused at parse or
plan time is saved (credential-redacted, mode 0600) under
``<factory-root>/import/`` with the path in the refusal message.
``plan_import``'s strict-equality project guard is untouched — it is a
real guard against a proposal for the wrong repo, and it stays
case-sensitive.
"""

import json
from pathlib import Path

import pytest

from factory.cli import main
from factory.contract import load_tasks
from factory.importer import ImportRefused, plan_import, run_import

QUEUE_NAME = "jobfinderos"       # the profile/queue name (schema slug)
REPO_DIR = "JobFinderOS"         # the repo directory's spelling


# ------------------------------------------------------------- fixtures --


def _profile_yaml(name: str) -> str:
    """The smallest profile the project schema accepts, named ``name``."""
    return (
        f"name: {name}\n"
        "repo:\n"
        "  path: .\n"
        "  default_branch: main\n"
        "  remote: https://example.invalid/proj.git\n"
        "commands:\n"
        '  setup: "true"\n'
        '  test: "true"\n'
        "paths:\n"
        "  risk: {}\n"
        "  protected: []\n"
        "deploy:\n"
        "  mechanism: none\n"
        "  auto_on_merge: false\n"
        "live_checks: {}\n"
        "docs_allowlist: []\n"
    )


def _repo(tmp_path: Path, *, name: str = QUEUE_NAME,
          dir_name: str = REPO_DIR) -> Path:
    """A fixture project whose repo directory spells the name differently
    than the profile does — the shape the real bug was found on."""
    repo = tmp_path / dir_name
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "PLAN.md").write_text(
        "# Plan\n\n## WO-1: add search\n\nAcceptance: search finds a word.\n"
    )
    factory_dir = repo / ".factory"
    factory_dir.mkdir()
    (factory_dir / "project.yaml").write_text(_profile_yaml(name))
    return repo


def _wo(tid: str = "WO-1", **overrides) -> dict:
    """One proposed task: every field the queue schema requires."""
    task = {
        "id": tid,
        "title": f"Work order {tid}",
        "source": "docs/PLAN.md",
        "kind": "build",
        "priority": "P1",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [
            {
                "id": "AC-1",
                "text": "it works",
                "check": {
                    "type": "test",
                    "ref": f"tests/test_{tid.lower()}.py::test_ac_1",
                },
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }
    task.update(overrides)
    return task


def _proposal(*tasks: dict, project: str = QUEUE_NAME) -> str:
    """The importer session's output: one JSON object."""
    return json.dumps({"project": project, "tasks": list(tasks)})


def _one_saved_file(factory_root: Path) -> Path:
    """The single refused-output file under ``<factory-root>/import/``."""
    files = sorted((factory_root / "import").glob("*.proposal.*"))
    assert len(files) == 1, f"expected one saved proposal, got {files}"
    return files[0]


# ------------------------------------------------- the prompt names it --


def test_prompt_states_the_exact_project_name(tmp_path: Path):
    """The output contract says the name literally — placeholder gone.

    A profile named ``jobfinderos`` in a repo directory named
    ``JobFinderOS``: the prompt must carry the queue's spelling in the
    JSON contract and as a rule, and must not carry the directory's
    spelling anywhere (there was nothing else to copy but the guess)."""
    repo = _repo(tmp_path)
    prompts: list[str] = []

    def session(prompt: str) -> str:
        prompts.append(prompt)
        return _proposal(_wo())

    run_import(repo, session=session)

    assert len(prompts) == 1
    prompt = prompts[0]
    assert f'{{"project": "{QUEUE_NAME}", "tasks":' in prompt
    assert f"project must be exactly {QUEUE_NAME}" in prompt
    assert REPO_DIR not in prompt


def test_project_check_stays_case_sensitive():
    """plan_import's guard is untouched: strict equality, no case
    folding — a proposal spelling the name as the directory does is
    still refused (it may be a proposal for the wrong repo)."""
    proposal = json.loads(_proposal(_wo(), project=REPO_DIR))
    with pytest.raises(ImportRefused) as excinfo:
        plan_import(None, proposal, [], project=QUEUE_NAME)
    assert REPO_DIR in str(excinfo.value)


# ------------------------------------------- refused output is kept ----


def test_refused_output_is_saved_and_reimportable(tmp_path: Path, capsys,
                                                  monkeypatch):
    """A wrong-project proposal refuses, names the saved file, and that
    file re-imports cleanly through ``--proposal`` once the name is
    fixed — a refused real session no longer costs the output."""
    repo = _repo(tmp_path)
    factory_root = tmp_path / "froot"
    wrong = _proposal(_wo(), project=REPO_DIR)

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, session=lambda prompt: wrong,
                   factory_root=factory_root)

    message = str(excinfo.value)
    assert f"names project '{REPO_DIR}'" in message  # the guard still speaks
    saved = _one_saved_file(factory_root)
    assert saved.name.startswith(f"{QUEUE_NAME}-")
    assert saved.suffix == ".json"
    assert f"saved to {saved}" in message
    assert f"factory import --proposal {saved}" in message
    assert saved.read_text(encoding="utf-8") == wrong  # the raw output
    assert saved.stat().st_mode & 0o777 == 0o600
    assert not (repo / ".factory" / "tasks.json").exists()

    # Fix the name in the saved file and review it with --proposal.
    fixed = json.loads(saved.read_text(encoding="utf-8"))
    fixed["project"] = QUEUE_NAME
    saved.write_text(json.dumps(fixed), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    rc = main(["import", "--repo", str(repo), "--proposal", str(saved),
               "--factory-root", str(factory_root)])
    assert rc == 0
    queue = load_tasks(repo / ".factory" / "tasks.json")
    assert queue["project"] == QUEUE_NAME
    assert [task["id"] for task in queue["tasks"]] == ["WO-1"]
    assert queue["tasks"][0]["status"] == "draft"


def test_parse_refusal_saved_as_txt(tmp_path: Path):
    """Output that is not JSON at all is kept too — as ``.txt``, so the
    session's actual words survive the refusal."""
    repo = _repo(tmp_path)
    factory_root = tmp_path / "froot"
    prose = "I could not read the docs, sorry."

    with pytest.raises(ImportRefused, match="not valid JSON"):
        run_import(repo, session=lambda prompt: prose,
                   factory_root=factory_root)

    saved = _one_saved_file(factory_root)
    assert saved.suffix == ".txt"
    assert saved.read_text(encoding="utf-8") == prose
    assert saved.stat().st_mode & 0o777 == 0o600


def test_saved_output_is_redacted(tmp_path: Path):
    """No secrets are expected in a proposal, but the credential scan
    (factory.sentinels.redact_credentials — the same scrubber sentinel
    output goes through) runs on the saved copy anyway."""
    repo = _repo(tmp_path)
    factory_root = tmp_path / "froot"
    token = "ghp_" + "A" * 36
    leak = _proposal(_wo(title=f"leak {token}"), project=REPO_DIR)

    with pytest.raises(ImportRefused):
        run_import(repo, session=lambda prompt: leak,
                   factory_root=factory_root)

    saved = _one_saved_file(factory_root)
    kept = saved.read_text(encoding="utf-8")
    assert token not in kept
    assert "leak [redacted]" in kept


def test_stub_refusal_without_factory_root_writes_nowhere(
        tmp_path: Path, monkeypatch):
    """A stub session with no explicit factory root is free to re-run:
    its refusal keeps the original message and writes nothing under the
    default factory root — the suite's injected-session tests must not
    scatter files into the real ``~/factory/import/``."""
    monkeypatch.setattr("factory.importer.FACTORY_ROOT_DEFAULT",
                        tmp_path / "home-factory")
    repo = _repo(tmp_path)
    wrong = _proposal(_wo(), project=REPO_DIR)

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, session=lambda prompt: wrong)  # no factory_root

    message = str(excinfo.value)
    assert f"names project '{REPO_DIR}'" in message
    assert "saved to" not in message
    assert not (tmp_path / "home-factory").exists()


def test_real_session_refusal_saved_under_default_root(
        tmp_path: Path, monkeypatch):
    """The other arm: the real sandboxed session (session=None) is the
    expensive one — its refused output is kept even with the default
    factory root, and the message names the file."""
    monkeypatch.setattr("factory.importer.FACTORY_ROOT_DEFAULT",
                        tmp_path / "home-factory")
    repo = _repo(tmp_path)
    wrong = _proposal(_wo(), project=REPO_DIR)

    def fake_sdk_session(prompt, repo, **kwargs):
        return wrong

    monkeypatch.setattr("factory.importer.sdk_import_session",
                        fake_sdk_session)

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo)  # the real-session path, default root

    saved = _one_saved_file(tmp_path / "home-factory")
    assert f"factory import --proposal {saved}" in str(excinfo.value)
    assert saved.read_text(encoding="utf-8") == wrong
