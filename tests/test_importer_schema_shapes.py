"""F-12/F-13 step 3: the importer's output contract states the schema's
shapes, the session self-checks, and a schema refusal lists every
violation.

The refused jobfinderos import (2026-11-05) failed three ways the prompt
never warned about: hand-written JSON syntax breaks, per-task shape
errors (risk as a string, gates as booleans, ``AC1`` ids, invented
check types, string owner_steps/slices, size ``L``, priority ``P0``),
and a refusal that named only the first of ~240 violations. These tests
pin the three fixes: the contract is *generated* from
``schema/tasks.schema.json`` (so it cannot drift), the prompt tells the
session to parse- and schema-check its own output before answering, and
a refusal enumerates every violation class in one pass.
"""

from __future__ import annotations

import builtins
import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from factory.cli import main
from factory.importer import (
    SPEC_FIELDS,
    ImportRefused,
    build_import_prompt,
    plan_import,
)
from factory.session import (
    SessionConfigError,
    SessionRecord,
    run_session_sandboxed,
)

# --------------------------------------------------------------- fixtures --


def _wo(tid: str = "WO-1", **overrides) -> dict:
    """One schema-valid proposed task (the shape every test breaks one
    field of)."""
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
                "check": {"type": "test", "ref": f"tests/test_{tid.lower()}.py"},
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }
    task.update(overrides)
    return task


_PROFILE_YAML = textwrap.dedent(
    """\
    name: proj
    repo:
      path: /tmp/proj
      default_branch: main
      remote: https://example.invalid/proj.git
    commands:
      setup: "true"
      test: "true"
    paths:
      risk: {}
      protected: []
    deploy:
      mechanism: none
      auto_on_merge: false
    live_checks: {}
    docs_allowlist: []
    """
)


def _repo(tmp_path: Path) -> Path:
    """A fixture project: planning docs and a profile (a fresh import)."""
    repo = tmp_path / "proj"
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "PLAN.md").write_text("# Plan\n\n## WO-1: add search\n")
    (repo / ".factory").mkdir(exist_ok=True)
    (repo / ".factory" / "project.yaml").write_text(_PROFILE_YAML)
    return repo


def _schema() -> dict:
    from factory.importer import _load_tasks_schema

    return _load_tasks_schema()


def _resolve(prop: dict, schema: dict) -> dict:
    if "$ref" in prop:
        target: Any = schema
        ref = prop["$ref"]
        parts = ref.split("/") if not ref.startswith("#") else ref.lstrip("#/").split("/")
        for part in parts:
            target = target[part]
        return target
    return prop


def _enums_under(prop: dict, schema: dict) -> list[list[str]]:
    """Every enum reachable from one property (through $refs, items and
    nested properties), as lists of value strings."""
    prop = _resolve(prop, schema)
    found: list[list[str]] = []
    if "enum" in prop:
        found.append([str(value) for value in prop["enum"]])
    if "items" in prop:
        found.extend(_enums_under(prop["items"], schema))
    for sub in (prop.get("properties") or {}).values():
        found.extend(_enums_under(sub, schema))
    return found


def _spec_property(schema: dict, field: str) -> dict:
    items = _resolve(schema["properties"]["tasks"], schema)["items"]
    return _resolve(items, schema)["properties"][field]


# --------------------------------------------- 1: the contract is generated --


def test_prompt_states_every_spec_enum_from_the_schema(tmp_path: Path):
    """Every enum value of every spec field appears in the prompt —
    derived from the schema file, not from a hand-copied list."""
    prompt = build_import_prompt(tmp_path, "docs", [], project="proj")
    schema = _schema()
    checked = 0
    for field in SPEC_FIELDS:
        for values in _enums_under(_spec_property(schema, field), schema):
            for value in values:
                assert value in prompt, (
                    f"{field}: enum value {value!r} (from the schema) is "
                    f"missing from the import prompt"
                )
                checked += 1
    assert checked >= 25  # kind, priority, risk, gates, check, budget, slices


def test_prompt_tracks_a_schema_enum_edit(tmp_path: Path, monkeypatch):
    """The contract is generated from the loaded schema: editing an enum
    changes the prompt. A hand-copied list would sit still."""
    from factory import importer

    baseline = build_import_prompt(tmp_path, "docs", [], project="proj")
    schema = _schema()
    schema["$defs"]["task"]["properties"]["priority"]["enum"].append("P4")
    schema["$defs"]["check"]["properties"]["type"]["enum"].append("grep")
    monkeypatch.setattr(importer, "_load_tasks_schema", lambda: schema)
    edited = build_import_prompt(tmp_path, "docs", [], project="proj")
    assert "P4" in edited and "grep" in edited
    assert "P4" not in baseline and "grep" not in baseline


def test_prompt_states_the_acceptance_id_pattern(tmp_path: Path):
    """The AC-<number> pattern comes from the schema, not from memory."""
    prompt = build_import_prompt(tmp_path, "docs", [], project="proj")
    pattern = _schema()["$defs"]["acceptance_item"]["properties"]["id"]["pattern"]
    assert pattern in prompt


def test_prompt_states_required_keys_of_every_object_shape(tmp_path: Path):
    """The object shapes (gates, budget, acceptance items, slices,
    owner_steps) name their required keys, derived from the schema."""
    prompt = build_import_prompt(tmp_path, "docs", [], project="proj")
    schema = _schema()
    shapes = {
        "gates": _spec_property(schema, "gates"),
        "budget": _spec_property(schema, "budget"),
        "acceptance item": schema["$defs"]["acceptance_item"],
        "slice": schema["$defs"]["slice"],
        "owner step": _spec_property(schema, "owner_steps")["items"],
    }
    for name, shape in shapes.items():
        for key in shape.get("required") or []:
            assert key in prompt, (
                f"{name}: required key {key!r} missing from the prompt"
            )


def test_prompt_carries_the_hard_rules(tmp_path: Path):
    """The three rules the refused import tripped over: merge is human,
    size tops out at M (split, never L), priority is P1–P3 only."""
    prompt = build_import_prompt(tmp_path, "docs", [], project="proj")
    assert 'gates.merge is always "human"' in prompt
    assert "too big for size M" in prompt
    assert "P1–P3" in prompt


# --------------------------------------------------- 2: the self-check rule --


def test_prompt_carries_the_self_check_instruction(tmp_path: Path):
    """The session must parse- and schema-check its own output before
    answering; the final answer stays the JSON object."""
    prompt = build_import_prompt(tmp_path, "docs", [], project="proj")
    assert "python3 -c" in prompt
    assert "json.load" in prompt
    assert "schema/tasks.schema.json" in prompt
    assert "jsonschema" in prompt
    assert "final answer" in prompt


# ------------------------------------------- 3: every violation, one refusal --


def _three_violation_proposal() -> dict:
    return {
        "project": "proj",
        "tasks": [
            _wo("WO-A", priority="P0"),
            _wo("WO-B", risk="spills across modules"),
            _wo("WO-C", gates={"spec_approval": True, "merge": True,
                               "deploy": "auto"}),
        ],
    }


def test_refusal_lists_every_violation(tmp_path: Path):
    """A proposal with 3 distinct violation classes is refused naming
    all 3, with their paths — not just the first."""
    with pytest.raises(ImportRefused) as excinfo:
        plan_import(None, _three_violation_proposal(), [], project="proj")
    message = str(excinfo.value)
    assert "tasks/0/priority" in message and "P0" in message
    assert "tasks/1/risk" in message
    assert "'spills across modules' is not of type 'array'" in message
    assert "tasks/2/gates/merge" in message
    assert "True is not one of" in message


def test_refusal_is_capped_with_a_more_count(tmp_path: Path):
    """60 violations list 50, then '+10 more' — bounded, never silent."""
    proposal = {"project": "proj",
                "tasks": [_wo(f"WO-{i:02d}", priority="P0") for i in range(60)]}
    with pytest.raises(ImportRefused) as excinfo:
        plan_import(None, proposal, [], project="proj")
    message = str(excinfo.value)
    assert message.count("is not one of") == 50
    assert "+10 more" in message


def test_proposal_review_path_lists_every_violation(tmp_path, capsys):
    """The --proposal path (factory import --proposal <file>) refuses
    through the same every-violation listing."""
    repo = _repo(tmp_path)
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(_three_violation_proposal()))
    rc = main(["import", "--repo", str(repo), "--proposal", str(proposal_file)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "tasks/0/priority" in err and "P0" in err
    assert "tasks/1/risk" in err
    assert "tasks/2/gates/merge" in err


def test_semantics_only_refusal_keeps_its_message(tmp_path: Path):
    """A refusal with no schema violations (semantic or structural
    import rules) keeps its single, readable message."""
    with pytest.raises(ImportRefused) as excinfo:
        plan_import(
            None,
            {"project": "proj", "tasks": [_wo("WO-A"), _wo("WO-A")]},
            [],
            project="proj",
        )
    assert "appears twice" in str(excinfo.value)


# --------------------------------------------- 4: the session-record guard --


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# run\n")
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "test@factory.local"],
        ["config", "user.name", "factory test"],
        ["add", "-A"],
        ["commit", "-m", "base"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True,
                       capture_output=True, text=True)
    return path


def test_unparsable_record_saves_full_stdout(tmp_path: Path, monkeypatch):
    """A truncated record line raises SessionConfigError naming a file
    that holds the full raw stdout (redacted, 0600) — the 56 turns are
    never lost to a diagnosis again."""
    run_dir = _git_repo(tmp_path / "run")
    factory_root = tmp_path / "factory-root"
    settings_path = tmp_path / "sandbox" / "probe.json"
    settings_path.parent.mkdir(parents=True)
    stdout = (
        'factory-trace {"event": "tool_call", "tool": "Bash"}\n'
        '{"seat": "reviewer", "status": "compl'
    )

    def fake_run_sandboxed(cmd, cwd, settings, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("factory.session.run_sandboxed", fake_run_sandboxed)
    with pytest.raises(SessionConfigError) as excinfo:
        run_session_sandboxed(
            "reviewer", "the prompt", run_dir,
            {"docs_allowlist": [], "network_allowlist": []},
            factory_root=factory_root, settings_path=settings_path,
            max_turns=1, timeout_min=0.05, token="tok",
            cli_path=tmp_path / "claude", protected=(),
        )
    message = str(excinfo.value)
    sessions_dir = factory_root / "sessions"
    saved = list(sessions_dir.iterdir())
    assert len(saved) == 1, f"expected one saved stdout, got {saved}"
    path = saved[0]
    assert str(path) in message, f"the refusal must name the saved file: {message}"
    assert str(len('{"seat": "reviewer", "status": "compl')) in message
    assert path.read_text(encoding="utf-8") == stdout  # the FULL stdout
    assert "tool_call" in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600


def test_entry_final_print_flushes(tmp_path: Path, monkeypatch):
    """The in-sandbox entry's final print (the record line the
    orchestrator parses) flushes explicitly — a torn last line is the
    unparsable-record failure mode this guards."""
    from factory import session as session_mod

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "seat": "reviewer", "prompt": "p", "cwd": str(tmp_path),
        "max_turns": 1, "timeout_min": 0.05,
        "cli_path": str(tmp_path / "claude"),
    }))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")

    async def fake_run_session(*args, **kwargs):
        return SessionRecord(seat="reviewer", status="completed")

    monkeypatch.setattr(session_mod, "run_session", fake_run_session)
    calls: list[tuple[tuple, dict]] = []
    real_print = builtins.print

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", spy)
    assert session_mod._main([str(spec)]) == 0
    record_prints = [
        (args, kwargs) for args, kwargs in calls
        if args and isinstance(args[0], str) and '"seat"' in args[0]
    ]
    assert record_prints, "the entry never printed its record"
    assert all(kwargs.get("flush") is True for _, kwargs in record_prints)
