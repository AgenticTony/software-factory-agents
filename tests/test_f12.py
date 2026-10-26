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
