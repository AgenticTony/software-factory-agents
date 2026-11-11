"""F-12 follow-up: the importer prompt names the project, and refused
session output is kept.

Found on the JobFinderOS import (F-13 step 3 dogfood), round 1:

* ``factory import --repo ~/Desktop/jfos-factory`` was refused with
  "the proposal names project 'JobFinderOS'; this queue is 'jobfinderos'"
  — ``build_import_prompt`` showed a ``{"project": "<project name>"}``
  placeholder and never said the name, so the importer session guessed
  it from the repo directory. The name is known at the call site (the
  existing queue's, or the profile's for a fresh project).
* That refusal cost a full real session: ``cleanup_run`` deletes the
  run dir and the raw output was never kept, so there was nothing to
  re-review with ``factory import --proposal``.

Round 2 (review of PR #92):

* A refusal *after* the session (a stale queue at apply time, a failed
  write) must keep the output too — the expensive part already ran.
* The fix command in the refusal must carry the original run's
  ``--repo`` (and non-default ``--queue``/``--docs``); the CLI defaults
  ``--repo`` to the cwd, so a pasted bare command reviews the wrong
  repo.
* Saving keys on "real session" (``session=None``, or an explicit
  ``keep_refused`` for a replay harness), never on ``factory_root`` —
  a refused ``--proposal`` re-review with ``--factory-root`` must not
  re-save the owner's own file.
* ``plan_import``'s strict-equality project guard stays untouched and
  case-sensitive: it guards against a proposal for the wrong repo.

All red-first in this file. ``tests/conftest.py`` is in
``paths.protected``, so the suite-wide ``FACTORY_ROOT_DEFAULT``
monkeypatch lives here instead: every test that can reach a save points
the default root at ``tmp_path`` first.
"""

import json
from pathlib import Path

import pytest
from _support.f63 import importer_fixture_approve

import factory.importer as importer
from factory.cli import main
from factory.contract import load_tasks
from factory.importer import ImportRefused, plan_import
from factory.importer import run_import as _run_import
from factory.state import TaskQueue

QUEUE_NAME = "jobfinderos"       # the profile/queue name (schema slug)
REPO_DIR = "JobFinderOS"         # the repo directory's spelling


# ------------------------------------------------------------- fixtures --


def run_import(repo, *args, **kwargs):
    """F-63: the real session path checks the profile against the owner
    ledger — approve this fixture's profile under the factory root this
    call uses (the importer's own default when none was passed, so a
    monkeypatched default is honoured), then delegate to the real one."""
    root = kwargs.get("factory_root") or importer.FACTORY_ROOT_DEFAULT
    if kwargs.get("session") is None:  # only the real path checks the ledger
        importer_fixture_approve(root, repo)
    return _run_import(repo, *args, **kwargs)


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


def _real_session(monkeypatch, text: str) -> None:
    """Make the real-session path (session=None) return ``text`` without
    touching a sandbox: sdk_import_session is the only thing run_import
    calls for it."""
    monkeypatch.setattr(
        "factory.importer.sdk_import_session",
        lambda prompt, repo, **kwargs: text,
    )


def _pin_default_root(monkeypatch, tmp_path: Path) -> Path:
    """Point FACTORY_ROOT_DEFAULT at tmp_path: nothing in this file may
    write into the real ~/factory (conftest.py is protected, so the
    suite-wide pin lives here)."""
    root = tmp_path / "home-factory"
    monkeypatch.setattr("factory.importer.FACTORY_ROOT_DEFAULT", root)
    return root


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


# --------------------------------------- refusals keep the output ------


def test_refused_output_is_saved_and_reimportable(tmp_path: Path,
                                                  monkeypatch):
    """A real session's wrong-project proposal refuses, names the saved
    file, and that file re-imports cleanly through ``--proposal`` once
    the name is fixed — a refused real session no longer costs the
    output."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    wrong = _proposal(_wo(), project=REPO_DIR)
    _real_session(monkeypatch, wrong)

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo)  # the real-session path, default root

    message = str(excinfo.value)
    assert f"names project '{REPO_DIR}'" in message  # the guard still speaks
    saved = _one_saved_file(home)
    assert saved.name.startswith(f"{QUEUE_NAME}-")
    assert saved.suffix == ".json"
    assert f"saved to {saved}" in message
    assert saved.read_text(encoding="utf-8") == wrong  # the raw output
    assert saved.stat().st_mode & 0o777 == 0o600
    assert not (repo / ".factory" / "tasks.json").exists()

    # Fix the name in the saved file and review it with --proposal.
    fixed = json.loads(saved.read_text(encoding="utf-8"))
    fixed["project"] = QUEUE_NAME
    saved.write_text(json.dumps(fixed), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    rc = main(["import", "--repo", str(repo), "--proposal", str(saved)])
    assert rc == 0
    queue = load_tasks(repo / ".factory" / "tasks.json")
    assert queue["project"] == QUEUE_NAME
    assert [task["id"] for task in queue["tasks"]] == ["WO-1"]
    assert queue["tasks"][0]["status"] == "draft"


def test_parse_refusal_saved_as_txt(tmp_path: Path, monkeypatch):
    """Output that is not JSON at all is kept too — as ``.txt``, so the
    session's actual words survive the refusal."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    prose = "I could not read the docs, sorry."
    _real_session(monkeypatch, prose)

    with pytest.raises(ImportRefused, match="not valid JSON"):
        run_import(repo)

    saved = _one_saved_file(home)
    assert saved.suffix == ".txt"
    assert saved.read_text(encoding="utf-8") == prose
    assert saved.stat().st_mode & 0o777 == 0o600


def test_saved_output_is_redacted(tmp_path: Path, monkeypatch):
    """No secrets are expected in a proposal, but the credential scan
    (factory.sentinels.redact_credentials — the same scrubber sentinel
    output goes through) runs on the saved copy anyway."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    token = "ghp_" + "A" * 36
    _real_session(monkeypatch, _proposal(_wo(title=f"leak {token}"),
                                         project=REPO_DIR))

    with pytest.raises(ImportRefused):
        run_import(repo)

    saved = _one_saved_file(home)
    kept = saved.read_text(encoding="utf-8")
    assert token not in kept
    assert "leak [redacted]" in kept


def test_stale_apply_refusal_keeps_the_output(tmp_path: Path, monkeypatch):
    """Round 2: the queue changes while the confirm waits — apply
    refuses as stale, and that refusal (after the session already ran)
    keeps the output and names it, exactly like a parse/plan refusal."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    run_import(repo, session=lambda p: _proposal(_wo()),
               confirm=lambda summary: True)  # the queue now exists
    proposal = _proposal(_wo(), _wo("WO-2"))
    _real_session(monkeypatch, proposal)

    def approve_meanwhile(summary: str) -> bool:
        queue = TaskQueue(repo / ".factory" / "tasks.json")
        queue.get("WO-1")["title"] = "changed while the confirm waited"
        queue.save()
        return True

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, confirm=approve_meanwhile)

    message = str(excinfo.value)
    assert "changed since" in message
    assert "saved to" in message
    saved = _one_saved_file(home)
    assert saved.read_text(encoding="utf-8") == proposal


def test_cli_stale_apply_on_a_real_session_keeps_the_output(
        tmp_path: Path, monkeypatch, capsys):
    """Round 2, CLI arm: ``factory import`` (real session) prompts, the
    queue changes before the y — apply refuses stale, and the CLI saves
    the session's output and names it instead of dropping it."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    run_import(repo, session=lambda p: _proposal(_wo()),
               confirm=lambda summary: True)
    proposal = _proposal(_wo(), _wo("WO-2"))
    _real_session(monkeypatch, proposal)

    def approve_meanwhile(prompt: str = "") -> str:
        queue = TaskQueue(repo / ".factory" / "tasks.json")
        queue.get("WO-1")["title"] = "changed while the prompt waited"
        queue.save()
        return "y"

    monkeypatch.setattr("builtins.input", approve_meanwhile)
    importer_fixture_approve(home, repo)  # F-63: the CLI's real session
    rc = main(["import", "--repo", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "changed since" in err and "saved to" in err
    assert _one_saved_file(home).read_text(encoding="utf-8") == proposal
    # The approval survived: the stale write refused.
    assert load_tasks(repo / ".factory" / "tasks.json")["tasks"][0][
        "title"] == "changed while the prompt waited"


def test_write_failure_at_apply_keeps_the_output(tmp_path: Path,
                                                 monkeypatch):
    """Round 2: a failed write (OSError) after the session also keeps
    the output — the exception keeps its type, the copy still lands."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    _real_session(monkeypatch, _proposal(_wo()))

    def broken_apply(plan, queue_path, *, confirm, **_kwargs):
        # F-67: apply_import grew confirm_drop — the fake keeps accepting
        # whatever the caller passes.
        raise OSError("disk on fire")

    monkeypatch.setattr("factory.importer.apply_import", broken_apply)

    with pytest.raises(OSError, match="disk on fire"):
        run_import(repo, confirm=lambda summary: True)

    saved = _one_saved_file(home)
    assert json.loads(saved.read_text(encoding="utf-8"))["tasks"][0][
        "id"] == "WO-1"


# ------------------------------------- what the fix command tells you --


def test_fix_command_names_the_repo_and_skips_default_flags(
        tmp_path: Path, monkeypatch):
    """Round 2: the pasted command must review the same repo the run
    used (the CLI defaults --repo to the cwd) — ``--repo <absolute>``
    always; ``--queue``/``--docs`` only when they differ from the
    defaults, so the default case stays paste-clean."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    _real_session(monkeypatch, _proposal(_wo(), project=REPO_DIR))

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo)

    message = str(excinfo.value)
    saved = _one_saved_file(home)
    assert f"factory import --proposal {saved} --repo {repo.absolute()}" \
        in message
    assert "--queue" not in message
    assert "--docs" not in message


def test_fix_command_carries_nondefault_queue_and_docs(
        tmp_path: Path, monkeypatch):
    """The non-default arm: --queue and --docs ride along when the
    original run used them."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    _real_session(monkeypatch, _proposal(_wo(), project=REPO_DIR))
    queue_path = repo / "custom" / "tasks.json"
    (repo / "custom").mkdir()
    queue_path.write_text(json.dumps(
        {"version": 1, "project": QUEUE_NAME,
         "updated_at": "2026-11-05T00:00:00Z",
         "tasks": [{**_wo(), "status": "draft", "attempts": []}]}
    ), encoding="utf-8")

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, queue_path, docs_dir="plans")

    message = str(excinfo.value)
    saved = _one_saved_file(home)
    assert f"--proposal {saved} --repo {repo.absolute()}" in message
    assert f"--queue {queue_path}" in message
    assert "--docs plans" in message


def test_fix_command_quotes_paths_with_spaces(tmp_path: Path,
                                              monkeypatch):
    """A repo directory with a space must arrive as one shell word:
    the command shlex-quotes its path arguments."""
    repo = _repo(tmp_path, dir_name="My Project")
    _pin_default_root(monkeypatch, tmp_path)
    _real_session(monkeypatch, _proposal(_wo(), project=REPO_DIR))

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo)

    expected_repo = repr(str(repo.absolute()))  # single-quoted, POSIX
    assert f"--repo {expected_repo}" in str(excinfo.value)


# ------------------------------------------- who keeps, who doesn't ----


def test_stub_refusal_writes_nowhere_even_with_a_factory_root(
        tmp_path: Path, monkeypatch):
    """Round 2: saving keys on the real session, never on factory_root.
    A stub session's refusal keeps the original message and writes
    nothing — with or without an explicit factory root (a refused
    --proposal re-review passing --factory-root must not re-save the
    owner's own file), and never under the default root."""
    home = _pin_default_root(monkeypatch, tmp_path)
    repo = _repo(tmp_path)
    wrong = _proposal(_wo(), project=REPO_DIR)

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, session=lambda prompt: wrong)  # no factory_root
    assert f"names project '{REPO_DIR}'" in str(excinfo.value)
    assert "saved to" not in str(excinfo.value)

    explicit = tmp_path / "explicit-root"
    with pytest.raises(ImportRefused):
        run_import(repo, session=lambda prompt: wrong,
                   factory_root=explicit)  # root given, still a stub
    assert "saved to" not in str(excinfo.value)
    assert not explicit.exists()
    assert not home.exists()


def test_keep_refused_keeps_a_stub_sessions_output(tmp_path: Path,
                                                   monkeypatch):
    """The explicit opt-in: a caller replaying a real session's output
    through an injected session (an eval/replay harness) passes
    keep_refused=True and gets the same keep-and-name behaviour."""
    repo = _repo(tmp_path)
    home = _pin_default_root(monkeypatch, tmp_path)
    wrong = _proposal(_wo(), project=REPO_DIR)

    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, session=lambda prompt: wrong, keep_refused=True)

    message = str(excinfo.value)
    saved = _one_saved_file(home)
    assert f"saved to {saved}" in message
    assert saved.read_text(encoding="utf-8") == wrong
