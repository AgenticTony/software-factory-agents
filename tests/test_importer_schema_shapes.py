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
import copy
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from factory.cli import main
from factory.importer import (
    FORBIDDEN_FIELDS,
    SPEC_FIELDS,
    ImportRefused,
    _load_tasks_schema,
    build_import_prompt,
    build_proposal_schema,
    plan_import,
    run_import,
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
    answering; the final answer stays the JSON object.

    Round 2: the check names the absolute pinned interpreter (the one
    proven to import jsonschema inside the sandbox — a bare ``python3``
    resolves to whatever the sandbox PATH finds, which is not the
    factory's venv) and the STAGED proposal schema under $TMPDIR (the
    target repo has no schema/ dir of its own, and the queue schema
    would flag a valid proposal's missing status/version)."""
    prompt = build_import_prompt(tmp_path, "docs", [], project="proj")
    assert sys.executable in prompt  # named, never bare
    assert "python3" not in prompt
    assert "$TMPDIR/proposal.json" in prompt
    assert "$TMPDIR/proposal.schema.json" in prompt  # staged before the session
    assert "schema/tasks.schema.json" not in prompt  # not the target repo's
    assert "jsonschema" in prompt
    assert "final answer" in prompt
    assert "json.load" in prompt


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


# ================= round 2: the self-check works on its real target =========
#
# The round-1 instruction told the session to validate against the
# clone's schema/tasks.schema.json — wrong twice: the importer runs in a
# clone of the TARGET repo (JobFinderOS has no schema/ dir), and the
# queue schema requires version + per-task status, so a VALID proposal
# would print ~31 violations the prompt forbids satisfying. The staged
# schema is the queue schema narrowed to a proposal's own fields.


def test_proposal_schema_derives_from_the_queue_schema():
    """build_proposal_schema: same task shapes, no version, no status
    (forbidden like every other never-set field), out_of_scope_match
    allowed, the risk⇒human-merge rule kept."""
    queue = _load_tasks_schema()
    staged = build_proposal_schema(queue)
    assert staged["required"] == ["project", "tasks"]  # version gone
    assert set(staged["properties"]) == {"project", "tasks"}
    task = staged["$defs"]["task"]
    assert "status" not in task["required"]
    for banned in ("status", *FORBIDDEN_FIELDS):
        assert banned not in task["properties"], banned
    for field in ("id", *SPEC_FIELDS, "out_of_scope_match"):
        assert field in task["properties"], field
    # the shapes are the queue's own, not a hand-copy: enums identical
    for field in ("kind", "priority"):
        assert (task["properties"][field]["enum"]
                == queue["$defs"]["task"]["properties"][field]["enum"])
    assert task["properties"]["id"].get("minLength") == 1  # blank id refused
    # the one allOf rule that can fire without status rides along…
    assert any('"human"' in json.dumps(rule)
               for rule in task.get("allOf", []))
    # …and the status-gated ones are dropped: their `if` lacks
    # `required: [status]`, so on a status-less task they pass vacuously
    # and demand approval fields — the ~31-violation wall, rebuilt.
    assert all("status" not in json.dumps(rule)
               for rule in task.get("allOf", []))


def test_staged_check_agrees_with_plan_import(tmp_path: Path):
    """A proposal that passes the staged check is not refused for shape
    — and every incident-class shape error is caught by BOTH checks."""
    staged = build_proposal_schema(_load_tasks_schema())
    validator = Draft202012Validator(staged)

    valid = {"project": "proj", "tasks": [_wo()]}
    assert not list(validator.iter_errors(valid))
    plan_import(None, copy.deepcopy(valid), [], project="proj")

    breaks = {
        "priority P0": {"priority": "P0"},
        "acceptance id AC1": {"acceptance": [{
            "id": "AC1", "text": "t",
            "check": {"type": "test", "ref": "tests/x.py"}}]},
        "invented check type": {"acceptance": [{
            "id": "AC-1", "text": "t",
            "check": {"type": "grep", "ref": "app"}}]},
        "boolean merge gate": {"gates": {"spec_approval": True,
                                         "merge": True, "deploy": "human"}},
        "risk as a string": {"risk": "spills across modules"},
        "owner_steps as strings": {"owner_steps": ["confirm with Paddle"]},
        "slices as strings": {"slices": ["phase one"]},
        "size L": {"budget": {"size": "L", "max_attempts": 2,
                              "max_turns": 40, "wall_clock_min": 600}},
        "status handed over": {"status": "ready"},
        "approval field": {"approved_by": "owner"},
    }
    for name, override in breaks.items():
        proposal = {"project": "proj", "tasks": [_wo("WO-9", **override)]}
        errors = list(validator.iter_errors(proposal))
        assert errors, f"{name}: the staged check missed it"
        with pytest.raises(ImportRefused):
            plan_import(None, copy.deepcopy(proposal), [], project="proj")


def test_scratch_files_land_in_the_session_tmp(tmp_path: Path, monkeypatch):
    """run_session_sandboxed(scratch_files=...) stages files into the
    session's TMPDIR — the reviewer scratch, reachable from its cwd."""
    run_dir = _git_repo(tmp_path / "run")
    settings_path = tmp_path / "sandbox" / "probe.json"
    settings_path.parent.mkdir(parents=True)
    seen: dict[str, str] = {}

    def fake_run_sandboxed(cmd, cwd, settings, **kwargs):
        staged = Path(cwd) / ".tmp" / "proposal.schema.json"
        seen["content"] = staged.read_text(encoding="utf-8")
        record = SessionRecord(seat="reviewer", status="completed")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(record.to_dict()), stderr="")

    monkeypatch.setattr("factory.session.run_sandboxed", fake_run_sandboxed)
    run_session_sandboxed(
        "reviewer", "the prompt", run_dir,
        {"docs_allowlist": [], "network_allowlist": []},
        factory_root=tmp_path / "root", settings_path=settings_path,
        max_turns=1, timeout_min=0.05, token="tok",
        cli_path=tmp_path / "claude", protected=(),
        scratch_files={"proposal.schema.json": '{"type": "object"}'},
    )
    assert seen["content"] == '{"type": "object"}'


def test_scratch_file_names_must_be_bare(tmp_path: Path, monkeypatch):
    """A scratch file name with a path in it refuses before anything
    runs — staging must never write outside the session's tmp."""
    run_dir = _git_repo(tmp_path / "run")
    monkeypatch.setattr(
        "factory.session.run_sandboxed",
        lambda *a, **kw: pytest.fail("the session must never start"),
    )
    with pytest.raises(SessionConfigError):
        run_session_sandboxed(
            "reviewer", "p", run_dir,
            {"docs_allowlist": [], "network_allowlist": []},
            factory_root=tmp_path / "root",
            settings_path=tmp_path / "sandbox" / "s.json",
            max_turns=1, timeout_min=0.05, token="tok",
            cli_path=tmp_path / "claude", protected=(),
            scratch_files={"../escape.txt": "x"},
        )


def test_real_import_session_stages_the_proposal_schema(
    tmp_path: Path, monkeypatch
):
    """The real session path (session=None) hands run_session_sandboxed
    the derived proposal schema to stage — the prompt's named file and
    the staged file are the same shapes, by construction."""
    repo = _git_repo(_repo(tmp_path))
    captured: dict = {}

    def fake(seat, prompt, run_dir, profile, **kwargs):
        captured.update(kwargs)
        return SessionRecord(
            seat=seat, status="completed",
            result=json.dumps({"project": "proj", "tasks": [_wo()]}),
        )

    monkeypatch.setattr("factory.importer.run_session_sandboxed", fake)
    monkeypatch.setattr("factory.importer.seat_token", lambda: "tok")
    result = run_import(repo, factory_root=tmp_path / "froot")
    assert result.plan.project == "proj"
    staged = json.loads(captured["scratch_files"]["proposal.schema.json"])
    assert staged == build_proposal_schema(_load_tasks_schema())


def test_self_check_commands_run_in_the_real_sandbox(tmp_path: Path):
    """Behavior, not text: the prompt's check commands, extracted and
    run under srt with the importer seat's real settings, stay silent on
    a valid proposal and name all three violations on a broken one. The
    fixture repo has NO schema/ dir — the target-repo reality."""
    from factory.runs import cleanup_run, create_run
    from factory.sandbox import (
        build_settings,
        default_toolchain,
        run_sandboxed,
        write_settings,
    )

    repo = _repo(tmp_path)
    assert not (repo / "schema").exists()
    prompt = build_import_prompt(repo, "docs", [], project="proj")
    # each check command starts at the named interpreter — cut the line
    # there so bash receives exactly the command, not its bullet
    commands = [
        line.strip()[line.strip().index(sys.executable):]
        for line in prompt.splitlines()
        if ' -c "' in line
    ]
    assert len(commands) == 2  # the parse check and the shape check
    shape_check = next(c for c in commands if "proposal.schema.json" in c)

    run = create_run("fixture", _git_repo(tmp_path / "src"),
                     factory_root=tmp_path / "froot")
    try:
        settings = build_settings(
            {"docs_allowlist": [], "network_allowlist": []}, run.root,
            model_endpoint="api.z.ai", toolchain=default_toolchain(),
            allow_read=[tmp_path / "runner"], read_only=True,
        )
        write_settings(settings, run.settings_path)
        # Stage into the session's TMPDIR the way
        # run_session_sandboxed(scratch_files=...) does (production
        # writes through the .tmp symlink into the scratch outside the
        # clone; same path from inside the sandbox); run_sandboxed
        # prefixes TMPDIR=<run>/.tmp onto the command itself.
        stage = run.root / ".tmp"
        stage.mkdir(exist_ok=True)
        (stage / "proposal.schema.json").write_text(
            json.dumps(build_proposal_schema(_load_tasks_schema())))

        def run_line(line: str):
            # bash -c: the session's own shell shape, so $TMPDIR expands
            # exactly as it will inside the session (probed under srt).
            return run_sandboxed(["/bin/bash", "-c", line], run.root,
                                 run.settings_path, timeout=120)

        valid = json.dumps({"project": "proj", "tasks": [_wo()]}, indent=2)
        (stage / "proposal.json").write_text(valid)
        for line in commands:
            proc = run_line(line)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            assert not proc.stdout.strip(), (
                f"a valid proposal printed: {proc.stdout}"
            )

        broken = json.dumps({
            "project": "proj",
            "tasks": [_wo("WO-9", status="ready", priority="P0",
                          acceptance=[{"id": "AC1", "text": "t",
                                       "check": {"type": "test",
                                                 "ref": "tests/x.py"}}])],
        })
        (stage / "proposal.json").write_text(broken)
        proc = run_line(shape_check)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        assert "priority" in out and "is not one of" in out
        assert "AC1" in out and "does not match" in out
        assert "status" in out and "Additional properties" in out
    finally:
        cleanup_run(run)
