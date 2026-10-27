"""F-12 acceptance tests — BUILD-ORDER.md, work order F-12 (importer).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f12.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the F-02 loader and the real tasks schema (every applied
  queue is re-loaded through ``load_tasks``, so AC-1 is judged by the
  same validator production uses), the merge/summary/diff logic, the
  write path through :class:`~factory.state.TaskQueue` (lock, schema
  validation, append-only attempts), and the out-of-scope register
  files on disk.
* **Stubbed:** the importer session — the model boundary (F-06's
  injected-``query`` pattern). Each test hands ``run_import`` a
  ``session`` callable returning a fixed proposal, which is exactly the
  seam the real session result enters at.
"""

import json
from pathlib import Path

import pytest

from factory.cli import main
from factory.contract import load_tasks
from factory.importer import (
    ImportRefused,
    apply_import,
    parse_proposal,
    plan_import,
    read_out_of_scope,
    run_import,
)
from factory.state import TaskQueue

OOS_NAME = "no-ai-replies.md"
OOS_PATH = f".factory/out-of-scope/{OOS_NAME}"
OOS_TEXT = (
    "# Rejected: AI-generated replies\n\n"
    "Durable reason: replies must stay human-written; assistive drafts only.\n"
    "Asked for: 2026-09-30, 2026-10-11.\n"
)

# ------------------------------------------------------------- fixtures --


def _profile_yaml() -> str:
    """The smallest profile the project schema accepts."""
    return (
        "name: proj\n"
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


def _repo(tmp_path: Path, *, oos: dict[str, str] | None = None) -> Path:
    """A fixture project: planning docs, a profile, optionally the
    out-of-scope register.

    Idempotent: a test may call it twice against the same ``tmp_path``
    (to add the register to a project it already built) — every
    directory is created ``exist_ok`` so the second call extends the
    first instead of raising ``FileExistsError``."""
    repo = tmp_path / "proj"
    docs = repo / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "PLAN.md").write_text(
        "# Plan\n\n## WO-1: add search\n\nAcceptance: search finds a word.\n\n"
        "## WO-2: billing page\n\nAcceptance: the page renders an invoice.\n"
    )
    factory_dir = repo / ".factory"
    factory_dir.mkdir(exist_ok=True)
    (factory_dir / "project.yaml").write_text(_profile_yaml())
    if oos:
        register = factory_dir / "out-of-scope"
        register.mkdir(exist_ok=True)
        for name, text in oos.items():
            (register / name).write_text(text)
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


def _proposal(*tasks: dict, project: str = "proj") -> str:
    """The importer session's output: one JSON object."""
    return json.dumps({"project": project, "tasks": list(tasks)})


def _attempt() -> dict:
    """One attempt record in the schema's shape (the orchestrator's)."""
    return {
        "run_id": "20260101T000000Z-ab12cd",
        "slice_id": "",
        "started": "2026-10-20T09:00:00Z",
        "ended": "2026-10-20T10:00:00Z",
        "outcome": "pass",
        "prompt_versions": {"implementer": "deadbeef"},
        "skills": [],
    }


# ----------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """AC-1: the output validates against the schema.

    The applied queue is re-loaded through the real F-02 loader — the
    same schema validation production runs on every load."""
    repo = _repo(tmp_path)
    result = run_import(
        repo,
        session=lambda prompt: _proposal(_wo("WO-1"), _wo("WO-2")),
        confirm=lambda summary: True,
    )
    queue = load_tasks(result.queue_path)  # raises if the schema is violated
    assert queue["project"] == "proj"
    assert [task["id"] for task in queue["tasks"]] == ["WO-1", "WO-2"]


def test_ac_1_schema_violation_is_refused(tmp_path: Path):
    """AC-1's refusal arm: an impossible priority never reaches disk."""
    repo = _repo(tmp_path)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(
            repo,
            session=lambda prompt: _proposal(_wo("WO-1", priority="P9")),
            confirm=lambda summary: True,
        )
    assert "P9" in str(excinfo.value)
    assert not (repo / ".factory" / "tasks.json").exists()


def test_nothing_is_written_without_confirmation(tmp_path: Path):
    """The orchestrator shows the diff; nothing is written without
    confirmation (BUILD-ORDER F-12 prose)."""
    repo = _repo(tmp_path)
    result = run_import(
        repo,
        session=lambda prompt: _proposal(_wo("WO-1")),
        confirm=lambda summary: False,
    )
    assert result.applied is False
    assert "WO-1" in result.summary  # the proposal was still shown
    assert not (repo / ".factory" / "tasks.json").exists()


def test_apply_import_writes_only_on_confirm(tmp_path: Path):
    """apply_import is the only writer, and it honours its confirm."""
    repo = _repo(tmp_path)
    proposal = parse_proposal(_proposal(_wo("WO-1")))
    plan = plan_import(None, proposal, read_out_of_scope(repo), project="proj")
    queue_path = repo / ".factory" / "tasks.json"
    assert apply_import(plan, queue_path, confirm=lambda: False) is False
    assert not queue_path.exists()
    assert apply_import(plan, queue_path, confirm=lambda: True) is True
    assert [task["id"] for task in load_tasks(queue_path)["tasks"]] == ["WO-1"]


# ----------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path: Path):
    """AC-2: every imported task is draft — executable and
    non-executable kinds alike."""
    repo = _repo(tmp_path)
    result = run_import(
        repo,
        session=lambda prompt: _proposal(
            _wo("WO-1"), _wo("WO-2", kind="decision")
        ),
        confirm=lambda summary: True,
    )
    queue = load_tasks(result.queue_path)
    assert queue["tasks"]
    assert all(task["status"] == "draft" for task in queue["tasks"])


def test_ac_2_proposed_status_other_than_draft_is_refused(tmp_path: Path):
    """A proposal that tries to hand itself a status is refused, naming
    the task: approval is the owner's act, never the importer's."""
    repo = _repo(tmp_path)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(
            repo,
            session=lambda prompt: _proposal(_wo("WO-1", status="ready")),
            confirm=lambda summary: True,
        )
    message = str(excinfo.value)
    assert "WO-1" in message and "ready" in message
    assert not (repo / ".factory" / "tasks.json").exists()


def test_proposal_carrying_approval_fields_is_refused(tmp_path: Path):
    """Approval fields, attempts and links are written by you and the
    orchestrator — a proposal carrying any of them is refused."""
    repo = _repo(tmp_path)
    smuggled = _wo("WO-1", acceptance_hash="a" * 64,
                   approved_at="2026-10-20T09:00:00Z")
    with pytest.raises(ImportRefused) as excinfo:
        run_import(
            repo,
            session=lambda prompt: _proposal(smuggled),
            confirm=lambda summary: True,
        )
    assert "acceptance_hash" in str(excinfo.value)


# ----------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path):
    """AC-3: running it twice with no doc changes proposes no diff for
    existing tasks, and status and attempts are preserved."""
    repo = _repo(tmp_path)

    def session(prompt: str) -> str:
        return _proposal(_wo("WO-1"), _wo("WO-2"))

    first = run_import(repo, session=session, confirm=lambda summary: True)
    assert first.applied is True

    # Between the two imports you approved WO-1 and a run was attempted:
    # status, hash, approval fields and the append-only attempts log.
    queue = TaskQueue(first.queue_path)
    task = queue.get("WO-1")
    task["status"] = "ready"
    task["acceptance_hash"] = "a" * 64
    task["approved_at"] = "2026-10-20T09:00:00Z"
    task["approved_by"] = "owner"
    task["attempts"] = [_attempt()]
    queue.save()

    second = run_import(repo, session=session, confirm=lambda summary: True)
    kinds = {change.task_id: change.kind for change in second.plan.changes}
    assert kinds == {"WO-1": "unchanged", "WO-2": "unchanged"}
    assert second.plan.diff == ""
    assert "unchanged tasks (2): WO-1, WO-2" in second.summary

    # The applied queue keeps the owner's and orchestrator's state — the
    # only change on disk is the queue's updated_at stamp.
    after = load_tasks(first.queue_path)
    wo1 = next(t for t in after["tasks"] if t["id"] == "WO-1")
    assert wo1["status"] == "ready"
    assert wo1["acceptance_hash"] == "a" * 64
    assert wo1["approved_by"] == "owner"
    assert wo1["attempts"] == [_attempt()]
    wo2 = next(t for t in after["tasks"] if t["id"] == "WO-2")
    assert wo2["status"] == "draft"
    assert wo2["attempts"] == []


def test_ac_3_changed_doc_updates_spec_keeps_state(tmp_path: Path):
    """When a doc does change, the changed spec fields are proposed —
    and the task's status and attempts still survive the merge."""
    repo = _repo(tmp_path)

    def session(prompt: str) -> str:
        return _proposal(_wo("WO-1"), _wo("WO-2"))

    run_import(repo, session=session, confirm=lambda summary: True)

    queue = TaskQueue(repo / ".factory" / "tasks.json")
    task = queue.get("WO-1")
    task["status"] = "ready"
    task["acceptance_hash"] = "a" * 64
    task["approved_at"] = "2026-10-20T09:00:00Z"
    task["attempts"] = [_attempt()]
    queue.save()

    (repo / "docs" / "PLAN.md").write_text(
        "# Plan\n\n## WO-1: add search, now with filters\n\nChanged.\n\n"
        "## WO-2: billing page\n\nUnchanged.\n"
    )

    def changed_session(prompt: str) -> str:
        return _proposal(
            _wo("WO-1", title="Work order WO-1 (with filters)"), _wo("WO-2")
        )
    result = run_import(
        repo, session=changed_session, confirm=lambda summary: True
    )
    by_id = {change.task_id: change for change in result.plan.changes}
    assert by_id["WO-1"].kind == "changed"
    assert by_id["WO-1"].fields == ("title",)
    assert by_id["WO-2"].kind == "unchanged"

    after = load_tasks(repo / ".factory" / "tasks.json")
    wo1 = next(t for t in after["tasks"] if t["id"] == "WO-1")
    assert wo1["title"] == "Work order WO-1 (with filters)"
    assert wo1["status"] == "ready"
    assert wo1["attempts"] == [_attempt()]


def test_task_absent_from_proposal_is_kept_and_listed(tmp_path: Path):
    """A task the proposal does not mention is carried untouched — and
    listed, so a silently dropped WO is visible, never quiet."""
    repo = _repo(tmp_path)

    def session(prompt: str) -> str:
        return _proposal(_wo("WO-1"), _wo("WO-2"))

    run_import(repo, session=session, confirm=lambda summary: True)
    result = run_import(
        repo,
        session=lambda prompt: _proposal(_wo("WO-1")),
        confirm=lambda summary: True,
    )
    by_id = {change.task_id: change.kind for change in result.plan.changes}
    assert by_id == {"WO-1": "unchanged", "WO-2": "missing"}
    assert "WO-2" in result.summary
    queue = load_tasks(repo / ".factory" / "tasks.json")
    assert [task["id"] for task in queue["tasks"]] == ["WO-1", "WO-2"]


# ----------------------------------------------------------------- AC-4 --


def test_ac_4(tmp_path: Path):
    """AC-4: the importer reads .factory/out-of-scope/; a proposed task
    that resembles a record is listed in the summary with the record's
    path — still proposed as draft, never dropped or rejected on its
    own: you confirm the match."""
    repo = _repo(tmp_path, oos={OOS_NAME: OOS_TEXT})
    proposal = _proposal(
        _wo("WO-1"),
        _wo("WO-2", out_of_scope_match=OOS_PATH),
    )
    result = run_import(
        repo, session=lambda prompt: proposal, confirm=lambda summary: True
    )
    # The match is surfaced, with the record's path.
    assert [(m.task_id, m.record) for m in result.plan.matches] == [
        ("WO-2", OOS_PATH)
    ]
    assert OOS_PATH in result.summary
    assert "WO-2" in result.summary
    assert "confirm" in result.summary
    # The matched task is still proposed, still draft — matching is for
    # your confirmation, never an automatic rejection.
    queue = load_tasks(result.queue_path)
    wo2 = next(task for task in queue["tasks"] if task["id"] == "WO-2")
    assert wo2["status"] == "draft"


def test_ac_4_match_on_unchanged_task_is_still_listed(tmp_path: Path):
    """A resemblance is listed whatever the merge decided about the
    spec: the second import of an unchanged matched task still shows
    the record, because the confirmation is always yours."""
    repo = _repo(tmp_path, oos={OOS_NAME: OOS_TEXT})
    proposal = _proposal(_wo("WO-1", out_of_scope_match=OOS_PATH))
    run_import(repo, session=lambda prompt: proposal,
               confirm=lambda summary: True)
    result = run_import(repo, session=lambda prompt: proposal,
                        confirm=lambda summary: True)
    change_kinds = {c.task_id: c.kind for c in result.plan.changes}
    assert change_kinds == {"WO-1": "unchanged"}
    assert [(m.task_id, m.record) for m in result.plan.matches] == [
        ("WO-1", OOS_PATH)
    ]
    assert OOS_PATH in result.summary


def test_ac_4_unknown_record_path_is_refused(tmp_path: Path):
    """A match naming a record that does not exist is a broken proposal,
    not a silent miss: refused, naming the path."""
    repo = _repo(tmp_path)
    ghost = ".factory/out-of-scope/ghost.md"
    with pytest.raises(ImportRefused) as excinfo:
        run_import(
            repo,
            session=lambda prompt: _proposal(_wo("WO-1", out_of_scope_match=ghost)),
            confirm=lambda summary: True,
        )
    assert "ghost.md" in str(excinfo.value)


def test_ac_4_task_without_a_match_is_not_listed(tmp_path: Path):
    """No resemblance, no listing: the summary says none."""
    repo = _repo(tmp_path, oos={OOS_NAME: OOS_TEXT})
    result = run_import(
        repo,
        session=lambda prompt: _proposal(_wo("WO-1")),
        confirm=lambda summary: True,
    )
    assert result.plan.matches == []
    assert "Out-of-scope matches" in result.summary
    assert "- none" in result.summary
    assert OOS_NAME not in result.summary


# ------------------------------------------- supporting behaviour ------


def test_read_out_of_scope_lists_the_register(tmp_path: Path):
    """The register is read from .factory/out-of-scope/*.md, repo-relative
    paths; a missing register reads as empty, never a crash."""
    assert read_out_of_scope(_repo(tmp_path)) == []
    repo = _repo(tmp_path, oos={OOS_NAME: OOS_TEXT})
    records = read_out_of_scope(repo)
    assert [record["path"] for record in records] == [OOS_PATH]
    assert "human-written" in records[0]["text"]


def test_parse_proposal_accepts_a_fenced_block(tmp_path: Path):
    """The session may wrap its JSON in a code fence; the parser strips
    it and refuses prose-only output with a readable error."""
    parsed = parse_proposal("Here is the queue:\n```json\n" + _proposal(_wo("WO-1")) + "\n```\n")
    assert [task["id"] for task in parsed["tasks"]] == ["WO-1"]
    with pytest.raises(ImportRefused):
        parse_proposal("I could not read the docs, sorry.")
    with pytest.raises(ImportRefused):
        parse_proposal(json.dumps({"tasks": "everything"}))


def test_duplicate_proposed_id_is_refused(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(
            repo,
            session=lambda prompt: _proposal(_wo("WO-1"), _wo("WO-1")),
            confirm=lambda summary: True,
        )
    assert "twice" in str(excinfo.value)


def test_proposal_project_mismatch_is_refused(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(
            repo,
            session=lambda prompt: _proposal(_wo("WO-1"), project="other"),
            confirm=lambda summary: True,
        )
    assert "other" in str(excinfo.value)


def test_defaults_risk_depends_on_and_gates(tmp_path: Path):
    """A task may omit depends_on, risk and gates; the human-gate
    defaults are applied for it."""
    repo = _repo(tmp_path)
    bare = {
        "id": "WO-9",
        "title": "Bare",
        "source": "docs/PLAN.md",
        "kind": "build",
        "priority": "P2",
        "acceptance": [],
        "budget": {"size": "S", "max_attempts": 1, "max_turns": 5,
                   "wall_clock_min": 5},
    }
    result = run_import(
        repo,
        session=lambda prompt: _proposal(bare),
        confirm=lambda summary: True,
    )
    task = load_tasks(result.queue_path)["tasks"][0]
    assert task["status"] == "draft"
    assert task["depends_on"] == []
    assert task["risk"] == []
    assert task["gates"] == {
        "spec_approval": True,
        "merge": "human",
        "deploy": "human",
    }


# ------------------------------------------- L2 review round: R-2/R-3 --


def test_r2_missing_proposal_file_exits_1_never_traceback(tmp_path: Path, capsys):
    """R-2: a typo'd --proposal path is a refusal (exit 1, stderr
    message), not a FileNotFoundError traceback from outside the try
    block. Driven through the real parser (main), the repo's CLI-test
    convention."""
    repo = _repo(tmp_path)
    rc = main(["import", "--repo", str(repo),
               "--proposal", str(tmp_path / "nope.md")])
    assert rc == 1
    assert "nope.md" in capsys.readouterr().err
    assert not (repo / ".factory" / "tasks.json").exists()


def test_r2_binary_proposal_file_exits_1_never_traceback(tmp_path: Path, capsys):
    """R-2: a binary --proposal file raises UnicodeDecodeError (a
    ValueError); contract.read_file turns it into a ContractError refusal."""
    repo = _repo(tmp_path)
    binary = tmp_path / "bin.md"
    binary.write_bytes(b"\xff\xfe\x00")
    rc = main(["import", "--repo", str(repo), "--proposal", str(binary)])
    assert rc == 1
    # The specific refusal, not just the prefix any refusal prints
    # (PR #63 round 3, finding 7).
    err = capsys.readouterr().err
    assert "bin.md" in err and "not valid UTF-8" in err
    assert not (repo / ".factory" / "tasks.json").exists()


def test_r3_eof_at_confirm_writes_nothing(tmp_path: Path, capsys, monkeypatch):
    """R-3: EOF at the [y/N] prompt (closed or piped stdin) prints to
    stderr, writes nothing, and exits 1 — the same "user could not
    answer" class KeyboardInterrupt already had."""
    repo = _repo(tmp_path)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(_proposal(_wo("WO-1")))

    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    rc = main(["import", "--repo", str(repo), "--proposal", str(proposal)])
    assert rc == 1
    assert "nothing written" in capsys.readouterr().err
    assert not (repo / ".factory" / "tasks.json").exists()


def test_r3_explicit_n_declines_with_exit_0(tmp_path: Path, capsys, monkeypatch):
    """The documented exit contract (main()'s docstring): an explicit
    answer other than "y" is the one exit-0 decline — distinct from
    the EOF refusal's exit 1 (PR #63 round 3, finding 8)."""
    repo = _repo(tmp_path)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(_proposal(_wo("WO-1")))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    rc = main(["import", "--repo", str(repo), "--proposal", str(proposal)])
    assert rc == 0
    assert "nothing written" in capsys.readouterr().out
    assert not (repo / ".factory" / "tasks.json").exists()


# ----------------------------------- PR #63 review round 3: read class --


def test_eof_outside_the_prompt_is_not_reported_as_stdin(tmp_path: Path, capsys,
                                                        monkeypatch):
    """Only the [y/N] prompt's EOFError means "no answer on stdin" — an
    EOFError from anywhere else is not misdiagnosed as one (finding 4)."""
    repo = _repo(tmp_path)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(_proposal(_wo("WO-1")))

    def broken(*args, **kwargs):
        raise EOFError("from the importer, not stdin")

    monkeypatch.setattr("factory.cli.run_import", broken)
    with pytest.raises(EOFError, match="from the importer"):
        main(["import", "--repo", str(repo), "--proposal", str(proposal)])
    assert "no answer on stdin" not in capsys.readouterr().err


def test_empty_proposal_path_refuses_never_starts_a_session(tmp_path: Path,
                                                            capsys, monkeypatch):
    """``--proposal ""`` (an unset shell variable) is a missing file,
    not "no --proposal given": it refuses, exit 1, and never launches
    the model session (finding 9)."""
    repo = _repo(tmp_path)

    def no_session(*args, **kwargs):
        raise AssertionError("the importer session was launched")

    monkeypatch.setattr("factory.importer.sdk_import_session", no_session)
    rc = main(["import", "--repo", str(repo), "--proposal", ""])
    assert rc == 1
    assert "--proposal is empty" in capsys.readouterr().err
    assert not (repo / ".factory" / "tasks.json").exists()


def test_read_file_is_public_contract_api():
    """The refusing reader the CLI shares is public, not a private
    import across modules (finding 6)."""
    from factory import cli, contract

    assert cli.read_file is contract.read_file
    assert not hasattr(contract, "_read")


@pytest.fixture
def latin1_locale(monkeypatch):
    """Simulate a non-UTF-8 locale (Windows cp1252, ISO-8859-1): every
    read that leaves the encoding to the locale decodes as latin-1,
    under which any byte string is "valid" text."""
    import io

    real = io.text_encoding
    monkeypatch.setattr(
        io, "text_encoding",
        lambda encoding, stacklevel=2: "latin-1" if encoding is None
        else real(encoding, stacklevel),
    )


def test_binary_reads_refuse_whatever_the_locale(tmp_path: Path, latin1_locale):
    """The "not UTF-8 refuses" guarantee holds on any locale: each
    refusing reader pins encoding="utf-8" instead of trusting the
    platform default (finding 3)."""
    from factory.config import ConfigError, load_config
    from factory.contract import ContractError, read_file
    from factory.progress import ProgressLog, ProgressLogError
    from factory.replay import ReplayError, replay_lines

    binary = tmp_path / "bin"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ContractError, match="not valid UTF-8"):
        read_file(binary, "bin")
    with pytest.raises(ConfigError):
        load_config(binary)
    with pytest.raises(ReplayError):
        replay_lines({"paths": {"risk": {}}}, [(binary, "bin")])
    log = tmp_path / "log.jsonl"
    log.write_bytes(b"\xff\n")
    with pytest.raises(ProgressLogError):
        ProgressLog(log).entries()
    # ...and the writer pins it too: non-ASCII text (ensure_ascii=False)
    # round-trips instead of being written in the locale's encoding.
    fresh = ProgressLog(tmp_path / "fresh.jsonl")
    fresh.append({"run_id": "r1", "note": "Malmö — klart"})
    assert fresh.entries()[0]["note"] == "Malmö — klart"
    repo = _repo(tmp_path)
    register = repo / ".factory" / "out-of-scope"
    register.mkdir(parents=True, exist_ok=True)
    (register / "bin.md").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ImportRefused):
        read_out_of_scope(repo)


def test_binary_progress_log_is_a_progress_log_error(tmp_path: Path):
    """The tamper-evident log refuses an undecodable byte as
    ProgressLogError — the error every caller handles — never a raw
    UnicodeDecodeError past its chain check (finding 2)."""
    from factory.progress import ProgressLog, ProgressLogError

    log = tmp_path / "log.jsonl"
    log.write_bytes(b'{"n": 1}\n\xff\n')
    with pytest.raises(ProgressLogError, match="not valid UTF-8"):
        ProgressLog(log).entries()


def test_binary_trace_refuses_never_traceback(tmp_path: Path, capsys):
    """``factory trace`` on a corrupted (non-UTF-8) trace — e.g. a run
    killed mid-character — exits 1 with a message, never a raw
    UnicodeDecodeError out of main() (finding 1)."""
    from factory.trace import trace_path

    repo = _repo(tmp_path)
    root = tmp_path / "froot"
    run_id = "20260101T000000Z-ab12cd"
    path = trace_path(root, "proj", run_id)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"type": "phase_start"}\n\xe2\x82\n')
    rc = main(["trace", run_id, "--queue", str(repo / ".factory" / "tasks.json"),
               "--factory-root", str(root)])
    assert rc == 1
    assert "not valid UTF-8" in capsys.readouterr().err


# ------------------------------------ PR #61 review (prs-50-62.md) --


def _git_repo(tmp_path: Path) -> Path:
    """The fixture project, committed: the sandboxed importer session
    reads a disposable clone of the repo's HEAD, never the checkout."""
    import subprocess

    repo = _repo(tmp_path)
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "init"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    return repo


class _SandboxedRunnerSpy:
    """Stands in for ``run_session_sandboxed``: records each call and
    returns a completed record carrying ``proposal``."""

    def __init__(self, proposal: str):
        self.proposal = proposal
        self.calls: list[dict] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        import inspect

        from factory.session import SessionRecord, run_session_sandboxed
        from factory.state import run_lease_held

        # The call must bind against the real runner's signature, so the
        # spy can never accept a call production would reject.
        inspect.signature(run_session_sandboxed).bind(
            seat, prompt, run_dir, profile, **kwargs)
        run_dir = Path(run_dir)
        self.lease_held = (
            run_lease_held(self.queue_path) if getattr(self, "queue_path", None)
            else None
        )
        self.calls.append({
            "seat": seat, "prompt": prompt, "run_dir": run_dir,
            "run_dir_existed": run_dir.is_dir(),
            "has_plan": (run_dir / "docs" / "PLAN.md").is_file(),
            "profile": profile, **kwargs,
        })
        return SessionRecord(seat=seat, status="completed", result=self.proposal)


def _forbid_in_process_session(monkeypatch):
    def in_process(*args, **kwargs):
        raise AssertionError(
            "the importer ran run_session in-process, outside the srt sandbox"
        )

    monkeypatch.setattr("factory.session.run_session", in_process)
    monkeypatch.setattr("factory.importer.run_session", in_process, raising=False)
    monkeypatch.setattr("factory.importer.seat_token", lambda: "tok")


def test_default_import_session_runs_sandboxed_never_in_process(
        tmp_path: Path, monkeypatch):
    """#61 finding 1 (high): the default importer session goes through
    ``run_session_sandboxed`` as the read-only reviewer seat, in a
    disposable clone under the factory root — never ``run_session``
    in-process on the live checkout (DESIGN D3)."""
    repo = _git_repo(tmp_path)
    factory_root = tmp_path / "factory-root"
    spy = _SandboxedRunnerSpy(_proposal(_wo("WO-1")))
    _forbid_in_process_session(monkeypatch)
    monkeypatch.setattr("factory.importer.run_session_sandboxed", spy,
                        raising=False)

    result = run_import(repo, factory_root=factory_root)

    assert [c.task_id for c in result.plan.changes] == ["WO-1"]
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["seat"] == "reviewer"  # the read-only guard keys on it
    run_dir = call["run_dir"]
    assert run_dir.resolve() != repo.resolve()
    assert run_dir.parent == factory_root / "runs" / "proj"
    assert call["run_dir_existed"] and call["has_plan"]  # a clone of HEAD
    assert call["factory_root"] == factory_root
    assert call["settings_path"].parent == factory_root / "sandbox"
    assert call["profile"]["name"] == "proj"
    assert call["protected"] == []
    # The clone and its sandbox files are disposable: gone afterwards.
    assert not run_dir.exists()
    assert not list((factory_root / "runs" / "proj").iterdir())


def test_cli_default_import_goes_through_the_sandboxed_runner(
        tmp_path: Path, monkeypatch, capsys):
    """#61 finding 1, at the CLI: ``factory import`` with no --proposal
    reaches the sandboxed runner (never run_session)."""
    repo = _git_repo(tmp_path)
    spy = _SandboxedRunnerSpy(_proposal(_wo("WO-1")))
    _forbid_in_process_session(monkeypatch)
    monkeypatch.setattr("factory.importer.run_session_sandboxed", spy,
                        raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    rc = main(["import", "--repo", str(repo),
               "--factory-root", str(tmp_path / "factory-root")])

    assert rc == 0
    assert len(spy.calls) == 1 and spy.calls[0]["seat"] == "reviewer"
    assert "nothing written" in capsys.readouterr().out


def test_sandboxed_import_session_failure_refuses(tmp_path: Path, monkeypatch,
                                                  capsys):
    """A sandboxed session that did not complete (e.g. the reviewer
    guard's mutation verdict) refuses the import, exit 1, and the clone
    is still removed."""
    from factory.session import SessionRecord

    repo = _git_repo(tmp_path)
    factory_root = tmp_path / "factory-root"
    _forbid_in_process_session(monkeypatch)
    monkeypatch.setattr(
        "factory.importer.run_session_sandboxed",
        lambda seat, prompt, run_dir, profile, **kw: SessionRecord(
            seat=seat, status="failed", reason="reviewer_mutated_workspace"),
        raising=False,
    )
    rc = main(["import", "--repo", str(repo), "--factory-root", str(factory_root)])
    assert rc == 1
    assert "reviewer_mutated_workspace" in capsys.readouterr().err
    assert not list((factory_root / "runs" / "proj").iterdir())
    assert not (repo / ".factory" / "tasks.json").exists()


def _echo_proposal(doc: dict) -> dict:
    """A proposal that echoes every task's stored spec (unchanged docs)."""
    from factory.importer import SPEC_FIELDS

    return {
        "project": doc["project"],
        "tasks": [
            {"id": t["id"], **{f: t[f] for f in SPEC_FIELDS if f in t}}
            for t in doc["tasks"]
        ],
    }


def test_reimport_of_a_real_queue_proposes_no_diff():
    """#61 finding 2: AC-3 on a queue not born from the importer's fresh
    path — this repo's own ``.factory/tasks.json`` keeps ``updated_at``
    before ``tasks``. Echoing every task's spec must give an empty diff,
    not a moved ``updated_at`` line."""
    queue_path = Path(__file__).resolve().parent.parent / ".factory" / "tasks.json"
    doc = load_tasks(queue_path)
    assert list(doc).index("updated_at") < list(doc).index("tasks")
    plan = plan_import(doc, _echo_proposal(doc), [], project=doc["project"])
    assert {c.kind for c in plan.changes} == {"unchanged"}
    assert plan.diff == ""


def test_reimport_preserves_the_queues_key_order(tmp_path: Path):
    """#61 finding 2, fixture form: a queue written by TaskQueue (keys
    version, project, updated_at, tasks) re-imports with no diff."""
    queue_path = tmp_path / "tasks.json"
    doc = {"version": 1, "project": "proj", "updated_at": "2026-10-01T00:00:00Z",
           "tasks": [{**_wo("WO-1"), "status": "draft", "attempts": []}]}
    queue_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    loaded = load_tasks(queue_path)
    plan = plan_import(loaded, _echo_proposal(loaded), [], project="proj")
    assert plan.diff == ""
    assert list(plan.proposed_doc) == ["version", "project", "updated_at", "tasks"]


def test_apply_refuses_when_the_queue_changed_after_the_plan(tmp_path: Path):
    """#61 finding 3: an approval made while the confirm prompt waits is
    never silently reverted — apply_import refuses a stale plan."""
    repo = _repo(tmp_path)
    first = run_import(repo, session=lambda p: _proposal(_wo("WO-1")),
                       confirm=lambda s: True)
    planned = run_import(repo, session=lambda p: _proposal(_wo("WO-1"),
                                                           _wo("WO-2")))
    # Meanwhile, in another terminal: factory approve WO-1.
    queue = TaskQueue(first.queue_path)
    task = queue.get("WO-1")
    task["status"] = "ready"
    task["acceptance_hash"] = "a" * 64
    task["approved_at"] = "2026-10-20T09:00:00Z"
    task["approved_by"] = "owner"
    queue.save()

    with pytest.raises(ImportRefused, match="changed since"):
        apply_import(planned.plan, planned.queue_path, confirm=lambda: True)
    wo1 = next(t for t in load_tasks(first.queue_path)["tasks"]
               if t["id"] == "WO-1")
    assert wo1["status"] == "ready"
    assert wo1["approved_by"] == "owner"


def test_apply_refuses_when_a_queue_appeared_after_a_fresh_plan(tmp_path: Path):
    """#61 finding 3, fresh arm: a plan built with no queue on disk must
    not overwrite a queue another writer created meanwhile."""
    repo = _repo(tmp_path)
    planned = run_import(repo, session=lambda p: _proposal(_wo("WO-2")))
    other = run_import(repo, session=lambda p: _proposal(_wo("WO-1")),
                       confirm=lambda s: True)
    with pytest.raises(ImportRefused, match="changed since"):
        apply_import(planned.plan, planned.queue_path, confirm=lambda: True)
    assert [t["id"] for t in load_tasks(other.queue_path)["tasks"]] == ["WO-1"]


def test_cli_stale_plan_refuses_exit_1(tmp_path: Path, monkeypatch, capsys):
    """#61 finding 3 at the CLI: the queue changes while [y/N] waits; y
    refuses with exit 1 and the approval survives."""
    repo = _repo(tmp_path)
    run_import(repo, session=lambda p: _proposal(_wo("WO-1")),
               confirm=lambda s: True)
    proposal = tmp_path / "p.json"
    proposal.write_text(_proposal(_wo("WO-1"), _wo("WO-2")))
    queue_path = repo / ".factory" / "tasks.json"

    def approve_meanwhile(prompt=""):
        queue = TaskQueue(queue_path)
        queue.get("WO-1")["approved_by"] = "owner"
        queue.get("WO-1")["approved_at"] = "2026-10-20T09:00:00Z"
        queue.get("WO-1")["acceptance_hash"] = "a" * 64
        queue.get("WO-1")["status"] = "ready"
        queue.save()
        return "y"

    monkeypatch.setattr("builtins.input", approve_meanwhile)
    rc = main(["import", "--repo", str(repo), "--proposal", str(proposal)])
    assert rc == 1
    assert "changed since" in capsys.readouterr().err
    assert load_tasks(queue_path)["tasks"][0]["approved_by"] == "owner"


@pytest.mark.parametrize("omitted", ["depends_on", "risk", "gates"])
def test_omitted_spec_field_keeps_the_existing_value(tmp_path: Path, omitted):
    """#61 finding 4: a proposal that leaves out depends_on, risk or
    gates keeps the stored value — a default is not a proposal."""
    repo = _repo(tmp_path)
    stored = {
        "depends_on": ["WO-1"],
        "risk": ["auth"],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "auto"},
    }
    first = run_import(
        repo,
        session=lambda p: _proposal(_wo("WO-1"), _wo("WO-2", **stored)),
        confirm=lambda s: True,
    )
    bare = _wo("WO-2", **stored)
    del bare[omitted]
    second = run_import(repo, session=lambda p: _proposal(_wo("WO-1"), bare),
                        confirm=lambda s: True)
    kinds = {c.task_id: (c.kind, c.fields) for c in second.plan.changes}
    assert kinds["WO-2"] == ("unchanged", ())
    assert second.plan.diff == ""
    wo2 = next(t for t in load_tasks(first.queue_path)["tasks"]
               if t["id"] == "WO-2")
    assert wo2[omitted] == stored[omitted]


def test_explicit_empty_depends_on_still_clears(tmp_path: Path):
    """#61 finding 4's other arm: an explicit ``[]`` is a proposal."""
    repo = _repo(tmp_path)
    run_import(repo, session=lambda p: _proposal(
        _wo("WO-1"), _wo("WO-2", depends_on=["WO-1"])), confirm=lambda s: True)
    second = run_import(repo, session=lambda p: _proposal(
        _wo("WO-1"), _wo("WO-2", depends_on=[])))
    kinds = {c.task_id: (c.kind, c.fields) for c in second.plan.changes}
    assert kinds["WO-2"] == ("changed", ("depends_on",))


def test_out_of_scope_register_reaches_the_session_prompt(tmp_path: Path):
    """#61 finding 7: the register's records (path and text) are in the
    prompt the importer session receives — AC-4's concept matching
    depends on the session seeing them."""
    repo = _repo(tmp_path, oos={OOS_NAME: OOS_TEXT})
    seen: list[str] = []

    def session(prompt: str) -> str:
        seen.append(prompt)
        return _proposal(_wo("WO-1"))

    run_import(repo, session=session)
    assert len(seen) == 1
    assert f"### {OOS_PATH}" in seen[0]
    assert OOS_TEXT.strip() in seen[0]
    assert "docs/PLAN.md" in seen[0]


def test_build_import_prompt_says_when_the_register_is_empty(tmp_path: Path):
    """#61 finding 7: with no register the prompt says so explicitly."""
    from factory.importer import build_import_prompt

    repo = _repo(tmp_path)
    prompt = build_import_prompt(repo, "docs", read_out_of_scope(repo))
    assert "(the register is empty or missing)" in prompt
    assert OOS_PATH not in prompt


def test_sandboxed_import_holds_the_run_lease(tmp_path: Path, monkeypatch):
    """#61 finding 1, sibling: the importer's clone lives under
    ``runs/<project>/``, which reclaim_runs empties whenever the queue's
    run lease is free — so the lease is held while the session runs, and
    an import refuses (never races) while a factory run holds it."""
    repo = _git_repo(tmp_path)
    factory_root = tmp_path / "factory-root"
    run_import(repo, session=lambda p: _proposal(_wo("WO-1")),
               confirm=lambda s: True)
    queue_path = repo / ".factory" / "tasks.json"
    spy = _SandboxedRunnerSpy(_proposal(_wo("WO-1")))
    spy.queue_path = queue_path
    _forbid_in_process_session(monkeypatch)
    monkeypatch.setattr("factory.importer.run_session_sandboxed", spy,
                        raising=False)

    run_import(repo, factory_root=factory_root)
    assert spy.lease_held is True

    from factory.state import RunInProgressError

    # A live factory run holds the same lease (one exclusive lock).
    with TaskQueue(queue_path).idle_lease():
        with pytest.raises(RunInProgressError, match="run is in progress"):
            run_import(repo, factory_root=factory_root)
    assert len(spy.calls) == 1  # the refused import never started a session


def test_cli_import_srt_unusable_refuses_exit_1_never_traceback(
        tmp_path: Path, monkeypatch, capsys):
    """Round 2: srt missing or at the wrong version surfaces from the real
    sandboxed runner as ``SandboxError`` (``ensure_srt``, before anything
    spawns). ``factory import`` refuses — exit 1 with the message, never a
    traceback — and the clone is still removed."""
    from factory.sandbox import SandboxError

    def srt_missing(srt_bin_path=None):
        raise SandboxError("srt not found at node_modules/.bin/srt")

    repo = _git_repo(tmp_path)
    factory_root = tmp_path / "factory-root"
    _forbid_in_process_session(monkeypatch)
    monkeypatch.setattr("factory.sandbox.ensure_srt", srt_missing)

    rc = main(["import", "--repo", str(repo), "--factory-root", str(factory_root)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "srt not found" in err and "Traceback" not in err
    assert not list((factory_root / "runs" / "proj").iterdir())
    assert not (repo / ".factory" / "tasks.json").exists()


@pytest.mark.parametrize("error", ["SandboxError", "RunError"])
def test_cli_import_runner_setup_error_refuses_exit_1(
        tmp_path: Path, monkeypatch, capsys, error):
    """Round 2: any runner setup error raised from the sandboxed runner
    (``SandboxError``, ``RunError``) is an ImportRefused at the CLI."""
    from factory.runs import RunError
    from factory.sandbox import SandboxError

    exc_type = {"SandboxError": SandboxError, "RunError": RunError}[error]

    def broken(seat, prompt, run_dir, profile, **kw):
        raise exc_type(f"{error}: runner could not be set up")

    repo = _git_repo(tmp_path)
    factory_root = tmp_path / "factory-root"
    _forbid_in_process_session(monkeypatch)
    monkeypatch.setattr("factory.importer.run_session_sandboxed", broken,
                        raising=False)

    rc = main(["import", "--repo", str(repo), "--factory-root", str(factory_root)])

    assert rc == 1
    assert "runner could not be set up" in capsys.readouterr().err
    assert not list((factory_root / "runs" / "proj").iterdir())
