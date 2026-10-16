"""F-02 acceptance tests — BUILD-ORDER.md, work order F-02.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f02.py::test_ac_N``
means a test function with exactly that name. The extra descriptive tests
below cover paths the acceptance refs don't name.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from factory.contract import ContractError, acceptance_hash, load_profile, load_tasks

REPO = Path(__file__).resolve().parent.parent


def _task(**overrides):
    base = {
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
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 80, "wall_clock_min": 60},
    }
    base.update(overrides)
    return base


def _tasks_file(tmp_path, tasks):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"version": 1, "project": "fixture", "tasks": tasks}))
    return path


def test_ac_1(tmp_path):
    # a risk-flagged task with merge: auto
    path = _tasks_file(
        tmp_path,
        [
            _task(
                id="T-RISK",
                risk=["deploy"],
                gates={"spec_approval": True, "merge": "auto", "deploy": "human"},
            )
        ],
    )
    with pytest.raises(ContractError, match=r"T-RISK.*risk.*human"):
        load_tasks(path)

    # a decision task in ready
    path = _tasks_file(
        tmp_path,
        [
            _task(
                id="T-DEC",
                kind="decision",
                status="ready",
                acceptance_hash="x" * 64,
                approved_at="2026-10-16T00:00:00Z",
            )
        ],
    )
    with pytest.raises(ContractError, match=r"T-DEC.*decision.*ready"):
        load_tasks(path)

    # a ready task without acceptance_hash
    path = _tasks_file(tmp_path, [_task(id="T-NOHASH", status="ready")])
    with pytest.raises(ContractError, match=r"T-NOHASH.*acceptance_hash"):
        load_tasks(path)

    # an invalid YAML profile
    profile = tmp_path / "project.yaml"
    profile.write_text("name: [unclosed\n  bad")
    with pytest.raises(ContractError, match="invalid YAML"):
        load_profile(profile)


def test_ac_2():
    item = {
        "id": "AC-1",
        "text": "it works",
        "check": {"type": "test", "ref": "tests/x.py::test_works"},
    }
    reordered = {
        "check": {"ref": "tests/x.py::test_works", "type": "test"},
        "text": "it works",
        "id": "AC-1",
    }
    assert acceptance_hash([item]) == acceptance_hash([reordered])

    changed = copy.deepcopy(item)
    changed["text"] = "it works differently"
    assert acceptance_hash([item]) != acceptance_hash([changed])

    expected = hashlib.sha256(
        json.dumps([item], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert acceptance_hash([item]) == expected


def test_ac_3(tmp_path):
    data = load_tasks(REPO / ".factory" / "tasks.json")
    ids = [t["id"] for t in data["tasks"]]
    # The queue validates and parses as ours; contents are not pinned, so
    # adding or reordering work orders doesn't redden a validation test.
    assert ids, "queue must not be empty"
    assert len(set(ids)) == len(ids), "task ids must be unique"
    assert all(i.startswith("F-") for i in ids), f"unexpected ids: {ids}"


def test_missing_and_malformed_files_are_refused_not_raised(tmp_path):
    # missing tasks.json / profile
    with pytest.raises(ContractError, match="cannot read"):
        load_tasks(tmp_path / "absent-tasks.json")
    with pytest.raises(ContractError, match="cannot read"):
        load_profile(tmp_path / "absent-profile.yaml")

    # non-UTF-8 bytes
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ContractError, match="not valid UTF-8"):
        load_tasks(binary)

    # top-level JSON list instead of the queue object
    listing = tmp_path / "list.json"
    listing.write_text(json.dumps(["not", "a", "queue"]))
    with pytest.raises(ContractError, match="schema violation"):
        load_tasks(listing)

    # empty YAML file (parses to None, not a mapping)
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with pytest.raises(ContractError, match="must be a YAML mapping"):
        load_profile(empty)


def test_unhashable_field_values_are_refused_not_crashed(tmp_path):
    # a list where a string enum belongs — must be a readable refusal,
    # never a TypeError from the semantic pass
    path = _tasks_file(tmp_path, [_task(id="T-KIND", kind=["build"])])
    with pytest.raises(ContractError, match="kind"):
        load_tasks(path)

    path = _tasks_file(tmp_path, [_task(id="T-STATUS", status=["ready"])])
    with pytest.raises(ContractError, match="status"):
        load_tasks(path)

    # task entry that is not an object at all
    path = _tasks_file(tmp_path, [["not", "a", "task"]])
    with pytest.raises(ContractError, match="schema violation"):
        load_tasks(path)


def test_post_approval_statuses_all_require_hash(tmp_path):
    # The semantic layer must mirror the schema's full rule: ready is only
    # the first of the statuses that require a frozen contract.
    for status in ["in_progress", "in_review", "deploying", "awaiting_owner", "done"]:
        path = _tasks_file(tmp_path, [_task(id=f"T-{status}", status=status)])
        with pytest.raises(ContractError, match="acceptance_hash"):
            load_tasks(path)
