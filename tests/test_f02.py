"""F-02 acceptance tests — BUILD-ORDER.md, work order F-02."""

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


def test_ac_1_invalid_states_are_rejected_with_readable_errors(tmp_path):
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


def test_ac_2_acceptance_hash_is_order_insensitive_and_change_sensitive():
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


def test_ac_3_this_repos_tasks_json_validates():
    data = load_tasks(REPO / ".factory" / "tasks.json")
    ids = [t["id"] for t in data["tasks"]]
    assert ids == [f"F-{i:02d}" for i in range(22)]
