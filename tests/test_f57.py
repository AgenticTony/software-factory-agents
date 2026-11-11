"""F-57 acceptance tests — BUILD-ORDER.md, work order F-57.

Importer robustness: a per-task ``propose_task`` tool on the importer
seat (validate each task as the model writes it), ONE bounded repair
session for a refused real-session proposal (never a full re-run), a
prose-docs fixture proving the flow offline, and ``factory eval
--importer`` for the owner's one paid measurement.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f57.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the F-02 loader and the real tasks schema, the full
  ``plan_import`` validation (the tool's accepted set and the repair's
  output both go through it), the real proposal-schema derivation, the
  real import prompt, the real keep-refused writers (mode 0600,
  ``<factory-root>/import/``), the real trace writer, and the prose
  fixture repo (copied to tmp and committed — no test writes into the
  checkout).
* **Stubbed:** the model. The real-session seam
  (``factory.importer.run_session_sandboxed`` + ``seat_token``) is
  patched with ``_support.f57.ScriptedRunner``, whose Nth call returns
  the Nth scripted record — so import → refusal → the ONE repair runs
  exactly the production path, offline. The ``propose_task`` tool's
  handler runs under test exactly as the SDK would run it in-session
  (the F-27 pattern); its collector is driven directly.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _support import no_bare_python3
from _support.f57 import (
    OVERSIZED_DOC,
    ScriptedRunner,
    _broken_wo,
    _completed,
    _corrected_tasks,
    _proposal,
    _prose_repo,
    _wo,
)
from _support.f63 import importer_fixture_approve
from _support.tokens import fake_token

import factory.importer as importer
from factory.contract import load_tasks
from factory.importer import (
    FORBIDDEN_FIELDS,
    ImportRefused,
    _list_planning_docs,
    build_import_prompt,
    build_proposal_schema,
    read_out_of_scope,
    scaled_import_budget,
    validate_budget,
)
from factory.importer import (
    run_import as _run_import,
)
from factory.session import SessionRecord, build_options

REPO = Path(__file__).resolve().parent.parent
PROJECT = "prose-demo"
SEAT_TOKEN = fake_token("f57-seat")


# ------------------------------------------------------------- helpers --


def run_import(repo, *args, **kwargs):
    """F-63: the real session path checks the profile against the owner
    ledger — approve this fixture's profile under the factory root this
    call uses (the importer's own default when none was passed, so a
    monkeypatched default is honoured), then delegate to the real one."""
    root = kwargs.get("factory_root") or importer.FACTORY_ROOT_DEFAULT
    if kwargs.get("session") is None:  # only the real path checks the ledger
        importer_fixture_approve(root, repo)
    return _run_import(repo, *args, **kwargs)

def _patch_real_session(monkeypatch, records) -> ScriptedRunner:
    """The offline real-session seam: run_session_sandboxed + a token,
    scripted so the Nth call returns the Nth record."""
    runner = ScriptedRunner(records)
    monkeypatch.setattr("factory.importer.run_session_sandboxed", runner)
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "tok"
                        if service == "factory-zai" else None)
    return runner


def _quota_record() -> SessionRecord:
    """A session record that died on quota (the orchestrator's _QUOTA
    shape)."""
    return SessionRecord(seat="reviewer", status="failed",
                         reason="api_error_429 rate limit exceeded")


def _saved(root: Path) -> list[Path]:
    # F-67: the import dir also holds *.record.json session records —
    # these assertions count the kept PROPOSAL outputs only.
    directory = root / "import"
    return sorted(
        path for path in (directory.iterdir() if directory.is_dir() else [])
        if not path.name.endswith(".record.json")
    )


def _collector(**kwargs) -> "object":
    """A ProposeCollector over the real staged schema (the test seam for
    the in-session handler; the real one is built by the sandboxed entry
    from the spec)."""
    from factory.importer import ProposeCollector

    schema = build_proposal_schema(json.loads(
        (REPO / "factory" / "data" / "schema" / "tasks.schema.json").read_text()
    ))
    return ProposeCollector(
        project=PROJECT, schema=schema, record_paths=(),
        secrets=(SEAT_TOKEN,), **kwargs,
    )


def _call(tool, **args):
    """Invoke an SDK tool's handler the way the SDK would (F-27's helper)."""
    return asyncio.run(tool.handler(args))


def _mcp_guard(options):
    """The mcp__.* PreToolUse hook out of built options, if any."""
    for matcher in options.hooks["PreToolUse"]:
        if matcher.matcher == "mcp__.*":
            return matcher.hooks[0]
    return None


def _fake_sdk(monkeypatch, record: SessionRecord) -> None:
    """Replace sdk_import_session with one returning ``record`` (calling
    the record_sink a real run would), for precedence legs."""
    import factory.importer as importer_mod

    def fake_sdk(prompt, repo, **kwargs):
        sink = kwargs.get("record_sink")
        if sink is not None:
            sink(record)
        return record.result or ""

    monkeypatch.setattr(importer_mod, "sdk_import_session", fake_sdk)
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "tok"
                        if service == "factory-zai" else None)


def _seed_ready_task(repo: Path) -> None:
    """A queue with one approved, ready task (the set-level rule's
    trigger: a ready task needs a non-empty acceptance)."""
    from factory.state import TaskQueue

    run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)
    queue = TaskQueue(repo / ".factory" / "tasks.json")
    task = queue.get("PROSE-1")
    task["status"] = "ready"
    task["acceptance_hash"] = "a" * 64
    task["approved_at"] = "2026-11-07T09:00:00Z"
    task["approved_by"] = "owner"
    queue.save()


# ----------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """AC-1: a ``propose_task(task)`` tool on the importer seat (the vent
    pattern) validates ONE task per call against the staged proposal
    schema and the import rules, returns ``accepted <id>`` or EVERY
    violation; re-proposing an id replaces it; zero calls falls back to
    the text answer."""
    import factory.importer as importer_mod

    assert hasattr(importer_mod, "make_propose_tool"), (
        "the propose_task tool does not exist (F-57 AC-1)"
    )
    from factory.importer import make_propose_tool

    # --- wiring (the red leg: today's options carry no propose server).
    options = build_options(
        "reviewer", tmp_path, 5,
        base_url="https://api.z.ai/api/anthropic", token="t",
        propose_collector=_collector(scratch_dir=None),
    )
    assert "propose_task" in options.mcp_servers, (
        "the importer session's options carry no propose_task MCP server"
    )
    assert options.mcp_servers["propose_task"]["type"] == "sdk"
    assert "mcp__propose_task__propose_task" in options.allowed_tools
    # The grant under the toolkit's mcp guard (F-22 finding 2: under
    # bypassPermissions the grant list pre-approves and the deny hook
    # enforces — propose_task must be granted exactly like vent).
    tooled = build_options(
        "reviewer", tmp_path, 5,
        base_url="https://api.z.ai/api/anthropic", token="t",
        propose_collector=_collector(scratch_dir=None),
        toolkit={"staged": str(tmp_path / "tk"), "items": []},
    )
    guard = _mcp_guard(tooled)
    assert guard is not None, "toolkit options carry the mcp guard hook"
    assert asyncio.run(guard({
        "tool_name": "mcp__propose_task__propose_task", "tool_input": {},
    }, "tu1", {})) == {}
    denied = asyncio.run(guard({
        "tool_name": "mcp__stranger__tool", "tool_input": {},
    }, "tu2", {}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    # --- the tool itself: accept, every-violation, replace, forbidden.
    collector = _collector(scratch_dir=None)
    tool = make_propose_tool(collector)
    assert tool.name == "propose_task"
    assert tool.input_schema["required"] == ["task"]

    # accept
    assert _call(tool, task=_wo("WO-1")) == {"content": [{
        "type": "text", "text": "accepted WO-1",
    }]}

    # every violation, not first-error-only: three independent shape
    # errors in one task come back together, each named.
    broken = _wo("WO-9", risk="payments", priority="P0")
    broken["budget"] = {"size": "L", "max_attempts": 3, "max_turns": 20,
                        "wall_clock_min": 30}
    violations = _call(tool, task=broken)["content"][0]["text"]
    assert "WO-9" in violations
    assert "is not of type 'array'" in violations   # risk as a string
    assert "'P0' is not one of" in violations
    assert "'L' is not one of" in violations

    # per-call import rules beyond shape: a handed status, and an
    # out-of-scope match naming no real record.
    refused = _call(tool, task=_wo("WO-4", status="ready"))
    assert "ready" in refused["content"][0]["text"]
    for banned in FORBIDDEN_FIELDS:
        refused = _call(tool, task=_wo("WO-3", **{banned: "x"}))
        assert banned in refused["content"][0]["text"], banned
        assert not refused.get("is_error")
    ghost = _wo("WO-5", out_of_scope_match=".factory/out-of-scope/ghost.md")
    assert "ghost.md" in _call(tool, task=ghost)["content"][0]["text"]

    # replace: re-proposing an id replaces it, in first-proposal order
    assert _call(tool, task=_wo("WO-1", title="second"))["content"][0][
        "text"] == "accepted WO-1"
    assert _call(tool, task=_wo("WO-2"))["content"][0]["text"] == "accepted WO-2"
    assert [t["id"] for t in collector.tasks()] == ["WO-1", "WO-2"]
    assert collector.tasks()[0]["title"] == "second"

    # zero calls → the text answer (the fallback), today's path unchanged.
    repo = _prose_repo(tmp_path)
    result = run_import(
        repo, session=lambda prompt: _proposal(_wo("PROSE-1")),
        confirm=lambda summary: True,
    )
    assert [t["id"] for t in load_tasks(result.queue_path)["tasks"]] == [
        "PROSE-1"
    ]


def test_ac_1_accepted_set_rides_the_record_out(tmp_path):
    """Mid-session wiring (the F-27 vents precedent): a tool call made
    during a live stream is collected and rides the SessionRecord out —
    `proposed_tasks` on every exit path, an aborted session's partial
    set included — which is the channel run_import reads it by."""
    from claude_agent_sdk import AssistantMessage

    from factory.importer import make_propose_tool
    from factory.session import run_session

    def _result_message(**overrides):
        from claude_agent_sdk import ResultMessage

        base = dict(
            subtype="success", duration_ms=10, duration_api_ms=8,
            is_error=False, num_turns=2, session_id="s1",
            usage={"input_tokens": 10, "output_tokens": 2},
            model_usage={}, terminal_reason="completed",
        )
        base.update(overrides)
        return ResultMessage(**base)

    collector = _collector(scratch_dir=None)
    live_tool = make_propose_tool(collector)

    async def _proposing_query(*, prompt, options, transport=None):
        yield AssistantMessage(content=[], model="glm-5.3")  # turn 1
        await live_tool.handler({"task": _wo("WO-1")})
        await live_tool.handler({"task": _wo("WO-2", risk="oops")})
        yield _result_message()

    record = asyncio.run(run_session(
        "reviewer", "import the docs", tmp_path, 5, 10,
        query_fn=_proposing_query,
        base_url="https://api.z.ai/api/anthropic", token="t",
        propose_collector=collector,
    ))
    assert record.status == "completed"
    assert [t["id"] for t in record.proposed_tasks] == ["WO-1"]
    assert record.propose_facts == []

    # a session that dies before its result message keeps its partial set
    # on the record too (the _carry rule runs on every exit path).
    partial = _collector(scratch_dir=None)
    partial_tool = make_propose_tool(partial)

    async def _dying_query(*, prompt, options, transport=None):
        yield AssistantMessage(content=[], model="glm-5.3")
        await partial_tool.handler({"task": _wo("WO-9")})
        return  # the stream ends with no ResultMessage

    dead = asyncio.run(run_session(
        "reviewer", "import the docs", tmp_path, 5, 10,
        query_fn=_dying_query,
        base_url="https://api.z.ai/api/anthropic", token="t",
        propose_collector=partial,
    ))
    assert dead.status == "failed" and dead.reason == "no_result_message"
    assert [t["id"] for t in dead.proposed_tasks] == ["WO-9"]


def test_ac_1_out_of_scope_match_checked_per_call(tmp_path):
    """A real register record is accepted per call; a ghost is refused —
    the register the collector is built with is the same one the prompt
    lists."""
    import factory.importer as importer_mod

    assert hasattr(importer_mod, "ProposeCollector"), (
        "the propose tool's collector does not exist (F-57 AC-1)"
    )
    from factory.importer import ProposeCollector, make_propose_tool

    schema = build_proposal_schema(json.loads(
        (REPO / "factory" / "data" / "schema" / "tasks.schema.json").read_text()
    ))
    collector = ProposeCollector(
        project=PROJECT, schema=schema,
        record_paths=(".factory/out-of-scope/no-ai-replies.md",),
        secrets=(), scratch_dir=None,
    )
    tool = make_propose_tool(collector)
    matched = _wo("WO-1", out_of_scope_match=".factory/out-of-scope/no-ai-replies.md")
    assert _call(tool, task=matched)["content"][0]["text"] == "accepted WO-1"


def test_ac_1_tool_registered_only_where_a_collector_is_given(tmp_path):
    """The three real seats never carry the propose_task server or grant:
    registration follows the importer's collector, never the seat name."""
    for seat in ("implementer", "reviewer", "verifier"):
        options = build_options(
            seat, tmp_path, 5,
            base_url="https://api.z.ai/api/anthropic", token="t",
        )
        assert "propose_task" not in (options.mcp_servers or {}), seat
        assert not any("propose_task" in name for name in options.allowed_tools), seat


def test_ac_1_set_level_refusal_matches_the_text_answer(tmp_path, monkeypatch):
    """A set whose every call returned ``accepted`` but which is invalid
    AS A WHOLE is refused by the SAME plan_import a text answer faces:
    a phantom LOCAL depends_on (a legal per-task shape — the tool cannot
    judge cross-task references; F-57 review round 1, F5)."""
    repo = _prose_repo(tmp_path)
    phantom_set = [_wo("PROSE-1"), _wo("PROSE-9", depends_on=["NOPE-1"])]

    # (a) the accepted-set path: every task fine per call, the set
    # refused by plan_import; the scripted repair dies on quota so the
    # final refusal carries the plan's own violation.
    _patch_real_session(monkeypatch, [
        _completed("done", proposed_tasks=phantom_set),
        _quota_record(),
    ])
    with pytest.raises(ImportRefused) as via_set:
        run_import(repo, factory_root=tmp_path / "froot-a")
    assert "depends_on names 'NOPE-1'" in str(via_set.value)

    # (b) the same proposal as a text answer: the same refusal class,
    # the same violation named, the same phantom.
    with pytest.raises(ImportRefused) as via_text:
        run_import(repo, session=lambda p: _proposal(*phantom_set))
    assert type(via_text.value) is type(via_set.value)
    assert "depends_on names 'NOPE-1'" in str(via_text.value)
    # a dep on a task the queue already carries, and a cross-repo dep,
    # are both fine (the merged ids, not just the proposal's, are the
    # reference; repo:id is never judged here).
    repo2 = _prose_repo(tmp_path / "second")
    run_import(repo2, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)
    ok = run_import(
        repo2, session=lambda p: _proposal(
            _wo("PROSE-1"),
            _wo("PROSE-2", depends_on=["PROSE-1", "other-repo:WO-77"]),
        ),
    )
    assert [c.kind for c in ok.plan.changes] == ["unchanged", "new"]


def test_ac_1_precedence_accepted_set_wins_both_ways(tmp_path, monkeypatch):
    """Calls were made → the accepted set wins over the text answer, and
    the divergence is visible to the owner: a valid set with a broken
    text answer succeeds with a note; a set that is invalid as a whole
    with a valid text answer is refused anyway, note in the message."""
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"

    # (a) set valid, text not even JSON: the set is the proposal, the
    # note says so, and no session ran twice.
    _fake_sdk(monkeypatch, _completed("I could not finish the JSON, sorry",
                                      proposed_tasks=[_wo("PROSE-1")]))
    _patch_real_session(monkeypatch, [])  # no repair: nothing is refused
    result = run_import(repo, factory_root=root, confirm=lambda s: True)
    assert result.applied
    assert [t["id"] for t in load_tasks(result.queue_path)["tasks"]] == [
        "PROSE-1"
    ]
    assert "accepted set" in result.summary

    # (b) set invalid as a whole (a phantom local dep), text answer
    # valid: the set still wins — refused, with the note.
    _fake_sdk(monkeypatch, _completed(_proposal(_wo("PROSE-1")),
                                      proposed_tasks=[
                                          _wo("PROSE-1"),
                                          _wo("PROSE-9",
                                              depends_on=["NOPE-1"]),
                                      ]))
    # the repair dies on quota, so the final refusal carries the plan's
    # own violation plus the precedence note.
    _patch_real_session(monkeypatch, [_quota_record()])
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    assert "depends_on names 'NOPE-1'" in message  # plan_import, not a fallback
    assert "accepted set" in message  # the precedence is stated


def test_ac_1_bounds_hostile_input(tmp_path):
    """Model-controlled tool input is untrusted: per-call size cap, nesting
    depth, total-calls cap, total-accepted cap — each a refusal message to
    the model plus a recorded fact, never a crash, never lost output; NUL
    and ANSI in a title are cleaned like vent's clean_text."""
    import factory.importer as importer_mod

    assert hasattr(importer_mod, "PROPOSE_MAX_CALLS"), (
        "the propose tool's bounds do not exist (F-57 AC-1)"
    )
    from factory.importer import (
        PROPOSE_MAX_CALLS,
        PROPOSE_MAX_TASKS,
        PROPOSE_TASK_MAX_CHARS,
        make_propose_tool,
    )

    collector = _collector(scratch_dir=None)
    tool = make_propose_tool(collector)

    # 1,005 calls against the calls cap, with ids cycling inside the
    # accepted-task cap (replacement, not growth): every call past the
    # cap is refused with the cap named, the set keeps what it had, the
    # fact is recorded.
    for index in range(PROPOSE_MAX_CALLS + 5):
        _call(tool, task=_wo(f"W-{index % PROPOSE_MAX_TASKS:05d}"))
    over = _call(tool, task=_wo("W-OVER"))
    assert "call cap" in over["content"][0]["text"]
    assert len(collector.tasks()) == PROPOSE_MAX_TASKS
    assert any(f["kind"] == "calls_capped" for f in collector.facts())

    # a 10 MB-class task: refused by the per-call size cap, recorded
    # (its own collector — the calls cap above consumes later calls).
    sized = _collector(scratch_dir=None)
    size_tool = make_propose_tool(sized)
    huge = _wo("W-HUGE", title="x" * (PROPOSE_TASK_MAX_CHARS + 1))
    size_answer = _call(size_tool, task=huge)["content"][0]["text"]
    assert str(PROPOSE_TASK_MAX_CHARS) in size_answer and "smaller" in (
        size_answer
    )
    assert any(f["kind"] == "task_too_large" for f in sized.facts())

    # nesting depth 1,000: refused by the depth bound (the same ceiling
    # the parse boundary enforces), iteratively — no RecursionError.
    deep_collector = _collector(scratch_dir=None)
    deep_tool = make_propose_tool(deep_collector)
    deep: dict = {"id": "W-DEEP"}
    node = deep
    for _ in range(1000):
        node["title"] = {}
        node = node["title"]
    refused = _call(deep_tool, task=deep)
    assert "nest" in refused["content"][0]["text"]
    assert any(f["kind"] == "task_too_deep" for f in deep_collector.facts())

    # total-accepted cap: the cap+1-st DISTINCT id is refused, recorded.
    capped = _collector(scratch_dir=None)
    cap_tool = make_propose_tool(capped)
    for index in range(PROPOSE_MAX_TASKS):
        _call(cap_tool, task=_wo(f"T-{index:05d}"))
    refusal = _call(cap_tool, task=_wo("T-ONE-MORE"))
    assert "accepted-task cap" in refusal["content"][0]["text"]
    assert len(capped.tasks()) == PROPOSE_MAX_TASKS
    assert any(f["kind"] == "tasks_capped" for f in capped.facts())

    # control characters: NUL and an ANSI escape in a title are cleaned
    # (vent's clean_text seam), the task accepted with a clean title.
    clean = _collector(scratch_dir=tmp_path)
    clean_tool = make_propose_tool(clean)
    noisy = _wo("W-CLEAN", title="bad\x00ti\x1b[2Jtle")
    assert _call(clean_tool, task=noisy)["content"][0]["text"] == (
        "accepted W-CLEAN"
    )
    stored = clean.tasks()[0]
    assert "\x00" not in stored["title"] and "\x1b" not in stored["title"]
    assert "bad" in stored["title"] and "tle" in stored["title"]
    # the literal seat token never rides a task out of the sandbox.
    leaky = _wo("W-LEAK", title=f"env {SEAT_TOKEN} echoed")
    assert _call(clean_tool, task=leaky)["content"][0]["text"] == (
        "accepted W-LEAK"
    )
    assert SEAT_TOKEN not in json.dumps(clean.tasks())


def test_ac_1_accepted_set_persists_crash_safe(tmp_path, monkeypatch):
    """The accepted set is saved as it grows (atomic write, the scratch's
    ``proposal.json`` — the very file the F-56 abort salvage copies out),
    so a crash, timeout or quota mid-session keeps what was proposed."""
    import factory.importer as importer_mod

    assert hasattr(importer_mod, "make_propose_tool"), (
        "the propose tool does not exist (F-57 AC-1)"
    )
    from factory.importer import PROPOSAL_OUTPUT_NAME, make_propose_tool

    scratch = tmp_path / "scratch" / "tmp"
    scratch.mkdir(parents=True)
    collector = _collector(scratch_dir=scratch)
    tool = make_propose_tool(collector)
    _call(tool, task=_wo("WO-1"))
    persisted = scratch / PROPOSAL_OUTPUT_NAME
    assert json.loads(persisted.read_text())["tasks"][0]["id"] == "WO-1"
    _call(tool, task=_wo("WO-2"))
    assert [t["id"] for t in json.loads(persisted.read_text())["tasks"]] == [
        "WO-1", "WO-2",
    ]
    _call(tool, task=_wo("WO-1", title="replaced"))
    doc = json.loads(persisted.read_text())
    assert [t["id"] for t in doc["tasks"]] == ["WO-1", "WO-2"]
    assert doc["tasks"][0]["title"] == "replaced"
    assert doc["project"] == PROJECT

    # the salvage keeps the collector-written file: an aborted session
    # whose scratch holds it names the saved copy in its refusal.
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"

    def aborts(seat, prompt, run_dir, profile, **kwargs):
        settings = Path(kwargs["settings_path"])
        target = settings.with_name(settings.name + "-scratch") / "tmp"
        target.mkdir(parents=True, exist_ok=True)
        (target / PROPOSAL_OUTPUT_NAME).write_text(persisted.read_text())
        return SessionRecord(seat="reviewer", status="aborted",
                             reason="wall_clock")

    monkeypatch.setattr("factory.importer.run_session_sandboxed", aborts)
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "tok"
                        if service == "factory-zai" else None)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    assert "its last proposal was saved to" in str(excinfo.value)
    saved = _saved(root)
    assert len(saved) == 1
    assert [t["id"] for t in json.loads(saved[0].read_text())["tasks"]] == [
        "WO-1", "WO-2",
    ]


def test_ac_1_prompt_teaches_the_tool(tmp_path):
    """The import prompt states the tool, the replace rule, the accepted
    set's precedence and the zero-calls fallback — and stays free of any
    bare interpreter mention (F-51 AC-1 keeps holding on this prompt)."""
    repo = _prose_repo(tmp_path)
    prompt = build_import_prompt(repo, "docs", read_out_of_scope(repo),
                                 project=PROJECT)
    assert "propose_task" in prompt
    assert "re-proposing" in prompt.lower()
    assert "accepted set" in prompt
    assert no_bare_python3(prompt, sys.executable) is None


# ----------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path, monkeypatch):
    """AC-2: a refused proposal from a real session gets ONE repair
    session (the saved output, the full violation list and the contract;
    no doc re-reading; a budget well under the import's) instead of a
    full re-run. Scripted success: the repaired proposal is the plan."""
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    runner = _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        _completed(_proposal(*_corrected_tasks()), num_turns=9),
    ])
    result = None
    try:
        result = run_import(repo, factory_root=root, confirm=lambda s: True)
    except ImportRefused:
        pass
    assert result is not None, (
        "the refused real-session import must be repaired in ONE session, "
        "not refused (F-57 AC-2)"
    )
    assert len(runner.calls) == 2, "exactly one import + one repair session"
    repair = runner.calls[1]
    # no doc re-reading, structurally: the repair's workspace is an empty
    # repo — no planning docs anywhere in it (captured at call time; the
    # dir is cleaned before this assert runs) — and not the import's clone.
    assert repair["run_dir"] != runner.calls[0]["run_dir"]
    assert repair["md_files"] == [], (
        "the repair workspace must hold no planning docs to read"
    )
    # the inputs: the saved refused output + the schema, staged in scratch
    assert "refused.json" in (repair["scratch_files"] or {})
    assert "proposal.schema.json" in (repair["scratch_files"] or {})
    # the budget: a fraction of the import's, above the floor
    turns, timeout = scaled_import_budget(3)  # the fixture lists 3 docs
    from factory.importer import repair_budget

    assert (repair["max_turns"], repair["timeout_min"]) == repair_budget(
        turns, timeout)
    assert repair["max_turns"] < runner.calls[0]["max_turns"]
    assert repair["timeout_min"] < runner.calls[0]["timeout_min"]
    # the prompt: violations in full, the contract, and no docs
    assert "does not match" in repair["prompt"]  # a violation line, verbatim
    assert "refused.json" in repair["prompt"]
    assert "no access to the planning documents" in repair["prompt"]
    # the generated contract lines (the schema's own enums — not the
    # hard-rules prose, which is what a contract-less prompt would carry)
    assert "one of P1 | P2 | P3" in repair["prompt"]
    assert "The schema requires every task to carry:" in repair["prompt"]
    # the repaired queue: written, valid under the real schema
    queue = load_tasks(result.queue_path)
    assert [t["id"] for t in queue["tasks"]] == [
        "PROSE-1", "PROSE-2", "PROSE-3a", "PROSE-3b",
    ]
    assert "repair" in result.summary.lower()
    # the trace event: start + outcome with turns
    traces = sorted((root / "trace" / PROJECT).glob("*.jsonl"))
    assert traces, "the repair wrote no trace"
    events = [json.loads(line) for line in traces[-1].read_text().splitlines()]
    repairs = [e for e in events if e["type"] == "import_repair"]
    assert [e.get("outcome") for e in repairs] == ["started", "repaired"]
    assert repairs[-1]["turns"] == 9
    # a refused-then-repaired import keeps the ORIGINAL refused output
    # (the repair's premise is that the expensive answer survives); the
    # repair's own output needs no keeping — it became the plan.
    saved = _saved(root)
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["tasks"][0]["id"] == "PROSE-1"


def test_ac_2_failed_repair_names_both_files(tmp_path, monkeypatch):
    """A repair that returns a still-broken proposal (or the same one) is
    refused; the error names BOTH saved files — the original refused
    output and the repair's — and both exist on disk, mode 0600."""
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    runner = _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        _completed(_proposal(_broken_wo())),  # the repair repeats the shapes
    ])
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    files = _saved(root)
    assert len(files) == 2, [p.name for p in files]
    for path in files:
        assert str(path) in message, path
        assert path.stat().st_mode & 0o777 == 0o600
    assert "original" in message and "repair" in message
    assert len(runner.calls) == 2  # never a repair of the repair


def test_ac_2_quota_stops_the_repair(tmp_path, monkeypatch):
    """A repair that hits quota stops, saves what it has and reports —
    reusing _is_quota; no second repair, no verdict read from a dead
    session."""
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    runner = _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        _quota_record(),
    ])
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    assert "quota" in message.lower() or "429" in message
    files = _saved(root)
    assert len(files) == 1 and str(files[0]) in message  # the original kept
    assert len(runner.calls) == 2
    # the trace says quota_stopped — not a generic failure and never a
    # verdict read out of a dead session.
    traces = sorted((root / "trace" / PROJECT).glob("*.jsonl"))
    events = [json.loads(line) for line in traces[-1].read_text().splitlines()]
    repairs = [e for e in events if e["type"] == "import_repair"]
    assert repairs[-1]["outcome"] == "quota_stopped"


def test_ac_2_quota_stopped_import_gets_no_repair(tmp_path, monkeypatch):
    """An import whose session died on quota is refused as today — no
    repair session is spent against a dead quota."""
    repo = _prose_repo(tmp_path)
    runner = _patch_real_session(monkeypatch, [_quota_record()])
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=tmp_path / "froot")
    assert "ended failed" in str(excinfo.value)
    assert len(runner.calls) == 1
    assert _saved(tmp_path / "froot") == []


def test_ac_2_stub_session_never_repairs(tmp_path, monkeypatch):
    """A stub/scripted session (an injected callable — no real model) is
    today's path exactly: refused, kept nothing, and the repair never
    runs (the real-session seam is never even called)."""
    repo = _prose_repo(tmp_path)

    def bomb(*args, **kwargs):
        raise AssertionError("the repair ran for a stub session")

    monkeypatch.setattr("factory.importer.run_session_sandboxed", bomb)
    with pytest.raises(ImportRefused):
        run_import(repo, session=lambda p: _proposal(_broken_wo()),
                   factory_root=tmp_path / "froot")
    assert _saved(tmp_path / "froot") == []


def test_ac_2_repair_budget_is_a_fraction_with_a_floor():
    """The repair budget: a named fraction of the import's with floors,
    always under the import's own scaled budget and always inside
    validate_budget's ceilings."""
    import factory.importer as importer_mod

    assert hasattr(importer_mod, "repair_budget"), (
        "the repair budget does not exist (F-57 AC-2)"
    )
    from factory.importer import (
        REPAIR_TIMEOUT_FLOOR,
        REPAIR_TURN_FLOOR,
        repair_budget,
    )

    for docs in (0, 3, 55, 200):
        turns, timeout = scaled_import_budget(docs)
        r_turns, r_timeout = repair_budget(turns, timeout)
        assert r_turns < turns and r_timeout < timeout, docs
        validate_budget(r_turns, r_timeout)  # never raises
    assert repair_budget(4, 3.0) == (REPAIR_TURN_FLOOR, REPAIR_TIMEOUT_FLOOR)


def test_ac_2_trace_event_type_registered():
    """``import_repair`` is a trace event type (the repair is visible in
    the F-30 vocabulary, written orchestrator-side only)."""
    from factory.trace import EVENT_TYPES

    assert "import_repair" in EVENT_TYPES


def test_ac_2_repaired_proposal_hits_the_same_validation(tmp_path, monkeypatch):
    """A repaired proposal is checked by EXACTLY the same function as the
    original: a repair that answers a DIFFERENT broken shape (valid where
    the first was broken, invalid elsewhere) is refused for the new
    violation — no weaker second door."""
    repo = _prose_repo(tmp_path)
    runner = _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        _completed(_proposal(_wo("PROSE-1", priority="P9"))),  # a new violation
    ])
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=tmp_path / "froot")
    assert "'P9' is not one of" in str(excinfo.value)
    assert len(runner.calls) == 2


# ----------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path, monkeypatch):
    """AC-3: the prose fixture (three prose work orders, no
    contract-shaped content; one owner-only, one too big for size M)
    drives the offline proof: the 2026-11-05 broken shapes are refused
    with EVERY violation listed, then fixed by the repair path; the
    final proposal is valid under the real schema."""
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"

    # (1) every violation listed, by name — the stub arm (no repair, so
    # the refusal itself is what the test reads).
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, session=lambda p: _proposal(_broken_wo()))
    message = str(excinfo.value)
    for marker in (
        "is not of type 'array'",       # risk as a string
        "is not of type 'object'",      # boolean gates
        "'AC1' does not match",         # AC1 ids
        "'grep' is not one of",         # check type grep
        "'L' is not one of",            # size L
        "'P0' is not one of",           # priority P0
    ):
        assert marker in message, marker

    # (2) the real-session arm: refused, then repaired in ONE session.
    runner = _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        _completed(_proposal(*_corrected_tasks())),
    ])
    result = None
    try:
        result = run_import(repo, factory_root=root, confirm=lambda s: True)
    except ImportRefused:
        pass
    assert result is not None, "the repair path must fix the broken shapes"
    assert len(runner.calls) == 2

    # (3) the final proposal is valid under the REAL schema (the F-02
    # loader re-validates on load), and the prose docs came out right:
    # the owner doc is owner-gated, the oversized doc is split.
    queue = load_tasks(result.queue_path)
    tasks = {t["id"]: t for t in queue["tasks"]}
    assert tasks["PROSE-2"]["kind"] == "owner_only"
    offline = [t for t in tasks.values() if t["source"] == OVERSIZED_DOC]
    assert len(offline) == 2, "the oversized work order must come out split"
    assert all(t["budget"]["size"] in ("S", "M") for t in offline)


def test_ac_3_fixture_is_prose_with_no_contract_shapes():
    """The fixture's planning docs carry no JSON and none of the contract
    field names — the importer must derive every queue field itself."""
    docs = sorted(
        (REPO / "tests" / "fixtures" / "prose-import" / "docs").glob("*.md")
    )
    assert len(docs) == 3
    field_names = (
        "acceptance", "depends_on", "priority", "gates", '"risk"',
        "budget", "spec_approval", "owner_steps", "out_of_scope",
        "acceptance_hash", "approved_by", "attempts", "P0", "P1", "P2",
        "size M", '"id"', "status",
    )
    for doc in docs:
        text = doc.read_text()
        assert "{" not in text and "}" not in text, doc.name
        for name in field_names:
            assert name not in text, (doc.name, name)


# ------------------------------------------- AC-4 (manual; scripted edges --


def test_ac_4_flag_parses():
    """``factory eval --importer`` exists on the parser (the AC's CLI
    surface; the real run is the owner's manual tick)."""
    from factory.cli import build_parser

    try:
        args = build_parser().parse_args(["eval", "--importer"])
    except SystemExit:
        args = None
    assert args is not None and args.importer is True


def test_ac_4_refuses_without_the_live_opt_in(tmp_path, monkeypatch, capsys):
    """The importer eval refuses to run without the explicit live opt-in
    (FACTORY_LIVE_TESTS=1) — a credential alone never turns it on, and
    nothing spends: with a fake credential and bombs under the real
    seams, the refusal still comes first (the live path is unreachable
    from the suite)."""
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "FAKE-TOKEN"
                        if service == "factory-zai" else None)

    def bomb(*args, **kwargs):
        raise AssertionError("the importer eval spent a real session")

    monkeypatch.setattr("factory.importer.sdk_import_session", bomb)
    monkeypatch.setattr("factory.importer.run_session_sandboxed", bomb)
    monkeypatch.setattr("factory.session.run_session_sandboxed", bomb)
    from factory.cli import main

    root = tmp_path / "froot"
    # every non-"1" value is a refusal too — the value semantics are the
    # contract (m-21): an org runner standardising on the variable can
    # never flip a paid session on.
    for value in (None, "0", "true", "yes", " 1", ""):
        monkeypatch.delenv("FACTORY_LIVE_TESTS", raising=False)
        if value is not None:
            monkeypatch.setenv("FACTORY_LIVE_TESTS", value)
        try:
            rc = main(["eval", "--importer", "--factory-root", str(root)])
        except SystemExit:
            rc = None  # the flag itself missing is the same defect
        assert rc == 2, value
        assert "FACTORY_LIVE_TESTS=1" in capsys.readouterr().err, value
    assert not (root / "eval").exists()


def test_ac_4_report_shape_on_a_scripted_session(tmp_path, monkeypatch):
    """The report: schema validity, task count, and whether the oversized
    work order was split — proven on a scripted session (no spend)."""
    import factory.eval as eval_mod

    assert hasattr(eval_mod, "run_importer_eval"), (
        "factory.eval.run_importer_eval does not exist (F-57 AC-4)"
    )
    _patch_real_session(monkeypatch, [
        _completed(_proposal(*_corrected_tasks())),
    ])
    out = eval_mod.run_importer_eval(factory_root=tmp_path, scripted=True)
    report = out["report"]
    assert report["schema_valid"] is True
    assert report["task_count"] == 4
    assert report["oversized_split"] is True
    assert report["tasks_from_oversized"] == 2
    # S2: how the plan was produced is part of the report
    assert report["tool_used"] is False      # the scripted session used no tool
    assert report["propose_calls"] == 0
    assert report["accepted_tasks"] == 0
    assert report["repair"] is None
    assert report["precedence_note"] is None
    rendered = out["rendered_path"].read_text()
    assert "4" in rendered and "split" in rendered.lower()
    assert "propose_task: no" in rendered
    assert out["report_path"].exists()


def test_ac_4_refused_then_repaired_reports_valid(tmp_path, monkeypatch):
    """A refused scripted import that its repair fixes reports valid —
    the repair is part of the measured path, never hidden."""
    import factory.eval as eval_mod

    assert hasattr(eval_mod, "run_importer_eval"), (
        "factory.eval.run_importer_eval does not exist (F-57 AC-4)"
    )
    _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo()), num_turns=9),
        _completed(_proposal(*_corrected_tasks()), num_turns=11),
    ])
    out = eval_mod.run_importer_eval(factory_root=tmp_path, scripted=True)
    report = out["report"]
    assert report["schema_valid"] is True
    # S2: first-pass success and a repaired import never read the same
    # (F-67: the repair facts also name the kept original's path)
    assert report["repair"] == {"ran": True, "outcome": "repaired",
                                "turns": 11,
                                "output": report["repair"]["output"],
                                "original_saved":
                                    report["repair"]["original_saved"]}
    assert "repaired (outcome repaired, 11 turns)" in (
        out["rendered_path"].read_text())


def test_ac_4_never_refused_silently_reports_invalid(tmp_path, monkeypatch):
    """A scripted import whose repair ALSO fails reports schema_valid
    False with the refusal — never a faked clean bill."""
    import factory.eval as eval_mod

    assert hasattr(eval_mod, "run_importer_eval"), (
        "factory.eval.run_importer_eval does not exist (F-57 AC-4)"
    )
    _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        _completed(_proposal(_broken_wo())),
    ])
    out = eval_mod.run_importer_eval(factory_root=tmp_path, scripted=True)
    report = out["report"]
    assert report["schema_valid"] is False
    assert report["task_count"] == 0
    assert report["oversized_split"] is None
    assert "'P0' is not one of" in report["refusal"]


# ------------------------------------- inherited I-a: the doc listing --


def test_doc_listing_never_follows_or_lists_escaped_symlinks(tmp_path):
    """I-a (from F-49 3b, re-routed by F-51): the planning-doc listing is
    interpreter-independent — directory symlinks are never followed (the
    3.11 rglob did), and a file symlink whose target escapes the repo is
    excluded (F-47's read boundary owns containment; the listing must
    not contradict it). A file symlink to a file INSIDE the repo stays
    listed; the F-49 pin on the escaping lure is superseded here."""
    repo = tmp_path / "proj"
    docs = repo / "docs"
    other = repo / "other"
    docs.mkdir(parents=True)
    other.mkdir()
    (docs / "plan.md").write_text("# P\n")
    (other / "hidden.md").write_text("# H\n")
    (docs / "target.md").write_text("# T\n")
    (tmp_path / "lure.md").write_text("# lure\n")
    (docs / "dirlink").symlink_to(other)                    # a directory symlink
    (docs / "escape.md").symlink_to(tmp_path / "lure.md")   # escapes the repo
    (docs / "inside.md").symlink_to(repo / "docs" / "target.md")  # inside

    listing = _list_planning_docs(repo, "docs")
    assert listing.kept == [
        "docs/inside.md", "docs/plan.md", "docs/target.md",
    ], listing.kept
    assert listing.dropped == []

    # The prompt sees the same listing (one seam, one policy).
    prompt = build_import_prompt(repo, "docs", [], project="proj")
    assert "docs/plan.md" in prompt and "docs/inside.md" in prompt
    assert "hidden.md" not in prompt and "escape.md" not in prompt
    assert "dirlink" not in prompt


def test_doc_listing_survives_a_symlink_loop(tmp_path):
    """A symlink loop under docs/ cannot hang the listing: bounded by
    construction (never following directory symlinks), proven in a
    subprocess so a regression shows as a timeout failure, not a hung
    suite."""
    repo = tmp_path / "proj"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "plan.md").write_text("# P\n")
    (docs / "loop").symlink_to(docs)      # a symlink pointing at its parent
    (docs / "sub").mkdir()
    (docs / "sub" / "again").symlink_to(docs)

    script = (
        "import json, sys; sys.path.insert(0, {repo_root!r}); "
        "from pathlib import Path; "
        "from factory.importer import _list_planning_docs; "
        "print(json.dumps(_list_planning_docs(Path({repo!r}), 'docs').kept))"
    ).format(repo_root=str(REPO), repo=str(repo))
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the planning-doc listing hung on a symlink loop")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert json.loads(proc.stdout) == ["docs/plan.md"], proc.stdout


# ----------------------------- review round 1: F1 — forged accepted sets --


@pytest.mark.parametrize("shape", ["deep", "str", "int", "dict", "items",
                                   "count"])
def test_f1_forged_accepted_set_refused_not_crashed(tmp_path, monkeypatch,
                                                    shape):
    """F1: the record file is session-writable, so proposed_tasks is
    untrusted structure — every hostile shape crosses the SAME parse
    boundary as a text answer (an ImportRefused through keep + repair,
    the output kept), never a traceback, never a silent accept."""
    def _deep(n):
        value = "x"
        for _ in range(n):
            value = [value]
        return value

    if shape == "deep":
        tasks = [_wo("PROSE-1", title=_deep(900))]
    elif shape == "str":
        tasks = "PROSE"
    elif shape == "int":
        tasks = 5
    elif shape == "dict":
        tasks = {"a": 1}
    elif shape == "items":
        tasks = [1, None, "x"]
    else:  # count
        tasks = [_wo(f"PROSE-{index}") for index in range(3000)]

    record = _completed("done")
    record.proposed_tasks = tasks
    _fake_sdk(monkeypatch, record)
    _patch_real_session(monkeypatch, [_quota_record()])  # repair dies fast
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    # No pytest.raises-only guard: a TypeError/RecursionError escaping
    # IS the failure this test exists to catch.
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    if shape == "count":
        assert "3000" in message and "cap" in message
    else:
        # whichever wall the shape hit, it is a parse-boundary refusal
        assert any(marker in message for marker in (
            "no proposal can be read", "no 'tasks' list",
            "not valid JSON", "nests", "cannot be measured for size",
        )), message
    # the expensive output was kept and named (keep + repair ran)
    saved = _saved(root)
    assert saved and str(saved[0]) in message


def test_f1_host_side_caps_equal_the_sandbox_caps(tmp_path, monkeypatch):
    """The sandbox's task-count cap is re-applied where the record is
    read — advisory in the sandbox, enforced at the host: exactly at the
    cap the set plans; one past it is refused naming the cap."""
    from factory.importer import PROPOSE_MAX_TASKS

    def _drive(tasks):
        record = _completed("done")
        record.proposed_tasks = tasks
        _fake_sdk(monkeypatch, record)
        _patch_real_session(monkeypatch, [_quota_record()])
        return run_import(_prose_repo(tmp_path / f"repo{len(tasks)}"),
                          factory_root=tmp_path / f"froot{len(tasks)}")

    at_cap = _drive([_wo(f"P-{i}") for i in range(PROPOSE_MAX_TASKS)])
    assert len(at_cap.plan.proposed_doc["tasks"]) == PROPOSE_MAX_TASKS
    with pytest.raises(ImportRefused, match="cap"):
        _drive([_wo(f"P-{i}") for i in range(PROPOSE_MAX_TASKS + 1)])


# ------------------------------- review round 1: F2 — the repair's lease --


def test_f2_busy_queue_stops_the_repair_as_a_refusal(tmp_path, monkeypatch):
    """F2: a queue whose run lease is held by a live run (real flock, in
    a child process) stops the repair BEFORE it starts — a terminal
    ``lease_busy`` trace outcome, the kept original named in the refusal,
    never a RunInProgressError traceback the CLI cannot catch. The same
    test pins that the repair takes the lease at all (m28: a repair that
    skips it would run the scripted session and answer differently)."""
    import subprocess

    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    queue_path = repo / ".factory" / "tasks.json"
    run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)

    _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        _quota_record(),  # never reached while the lease is busy
    ])
    import factory.importer as importer_mod

    orig_session_proposal = importer_mod._session_proposal
    holder = []

    def grab_lock_then(*args, **kwargs):
        # between the import session (lease released) and the repair
        # (lease about to be taken): a live run starts.
        lock = str(queue_path) + ".run.lock"
        holder.append(subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, time, sys; "
             "f = open(sys.argv[1], 'a+'); "
             "fcntl.flock(f, fcntl.LOCK_EX); "
             "print('held', flush=True); time.sleep(60)", lock],
            stdout=subprocess.PIPE, text=True,
        ))
        holder[-1].stdout.readline()
        return orig_session_proposal(*args, **kwargs)

    monkeypatch.setattr(importer_mod, "_session_proposal", grab_lock_then)
    try:
        with pytest.raises(ImportRefused) as excinfo:
            run_import(repo, factory_root=root)
    finally:
        for proc in holder:
            proc.kill()
            proc.wait()
    message = str(excinfo.value)
    assert "saved to" in message  # the kept original is named
    saved = _saved(root)
    assert len(saved) == 1
    traces = sorted((root / "trace" / PROJECT).glob("*.jsonl"))
    events = [json.loads(line) for line in traces[-1].read_text().splitlines()]
    repairs = [e for e in events if e["type"] == "import_repair"]
    assert repairs[-1]["outcome"] == "lease_busy"  # terminal, not "started"


# ------------------------- review round 1: F3 — settings-level doc deny --


def test_f3_repair_settings_deny_the_source_repo(tmp_path, monkeypatch):
    """F3: the repair's srt settings deny the SOURCE repo and its docs
    dir for reading — built from the real settings builder, asserted on
    the file that was written. The fixture repo lives under the pytest
    tmp dir, which on macOS is OUTSIDE $HOME: the home-wide deny says
    nothing about it, and this deny is what holds there."""
    import factory.session as session_mod
    from factory.session import record_path_for

    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    captured: list[dict] = []

    def fake_run_sandboxed(command, run_dir, settings_path, **kwargs):
        settings = json.loads(Path(settings_path).read_text())
        captured.append(settings)
        record = _completed(
            _proposal(*_corrected_tasks())
            if len(captured) > 1 else _proposal(_broken_wo())
        )
        path = record_path_for(Path(settings_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "seat": record.seat, "status": record.status,
            "result": record.result, "num_turns": 6,
        }))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(session_mod, "run_sandboxed", fake_run_sandboxed)
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "tok"
                        if service == "factory-zai" else None)
    # run_session_sandboxed stays REAL: the settings under test are the
    # ones the real session machinery writes.
    result = None
    try:
        result = run_import(repo, factory_root=root, confirm=lambda s: True)
    except ImportRefused:
        pass
    assert result is not None and len(captured) == 2
    repair_settings = captured[1]
    denied = repair_settings["filesystem"]["denyRead"]
    assert str(repo.resolve()) in denied, denied
    assert str((repo / "docs").resolve()) in denied, denied
    # and nothing re-allows it
    allowed = repair_settings["filesystem"]["allowRead"]
    assert not any(str(repo.resolve()) == entry for entry in allowed)


# -------------------- review round 1: F4 — contract change demotes --


def test_f4_contract_change_on_live_task_demotes_both_paths(tmp_path):
    """F4 (round 2): a proposal that changes a CONTRACT field (risk,
    gates, acceptance) on a non-draft task follows the ONE shared rule
    (factory.state.contract_change_action — the queue audit's own): a
    contract-frozen status goes back to draft THROUGH THE LEGAL EDGE with
    hash and approval fields KEPT as history; done is flagged, never
    reopened; not_buildable and draft are left. Identically via the
    accepted set and via the text answer."""
    from factory.state import TaskQueue

    def _downgrade_proposal():
        return _wo("PROSE-1", risk=[],
                   gates={"spec_approval": True, "merge": "auto",
                          "deploy": "human"})

    def _seed(repo):
        run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
                   confirm=lambda s: True)
        queue = TaskQueue(repo / ".factory" / "tasks.json")
        task = queue.get("PROSE-1")
        task.update(status="ready", acceptance_hash="a" * 64,
                    approved_at="2026-11-07T09:00:00Z", approved_by="owner",
                    risk=["payments"])
        queue.save()

    def _outcome(repo, via_set):
        if via_set:
            _fake_sdk_outcome(repo, [_downgrade_proposal()])
        return run_import(repo, session=lambda p: _proposal(
            _downgrade_proposal()), confirm=lambda s: True)

    def _fake_sdk_outcome(repo, tasks):
        pass  # the set path is driven by the shared fake below

    # (a) via the text answer
    repo_a = _prose_repo(tmp_path / "a")
    _seed(repo_a)
    result_a = _outcome(repo_a, via_set=False)
    task = load_tasks(result_a.queue_path)["tasks"][0]
    assert task["status"] == "draft"
    # history kept (re-approval overwrites), the risk relaxation carried
    assert task["acceptance_hash"] == "a" * 64
    assert task["approved_by"] == "owner" and task["approved_at"]
    # F-67: the proposed merge relaxation never lands — v0 gates are
    # human, the override is listed, and the demote rides the risk
    # change (the contract field that actually changed).
    assert task["gates"]["merge"] == "human" and task["risk"] == []
    assert "PROSE-1: gates.merge" in result_a.summary
    assert "-> draft (the approved contract changed" in result_a.summary
    assert "history" in result_a.summary

    # (b) via the accepted set — identical outcome
    repo_b = _prose_repo(tmp_path / "b")
    _seed(repo_b)
    record = _completed("done", proposed_tasks=[_downgrade_proposal()])
    import factory.importer as importer_mod

    def fake_sdk(prompt, repo_arg, **kwargs):
        sink = kwargs.get("record_sink")
        if sink is not None:
            sink(record)
        return record.result or ""

    monkeypatch_holder = _MonkeyPatch(importer_mod, "sdk_import_session",
                                      fake_sdk)
    monkeypatch_holder.set()
    try:
        result_b = run_import(repo_b, factory_root=tmp_path / "fbroot",
                              confirm=lambda s: True)
    finally:
        monkeypatch_holder.unset()
    task_b = load_tasks(result_b.queue_path)["tasks"][0]
    assert task_b == task


class _MonkeyPatch:
    """A one-slot monkeypatch for module attributes (test-local; the
    pytest monkeypatch fixture is function-scoped and these legs run
    inside helper functions)."""

    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value
        self.original = None

    def set(self):
        self.original = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)

    def unset(self):
        setattr(self.module, self.name, self.original)


def test_f4_acceptance_change_demotes_too(tmp_path):
    """The same rule for acceptance (the field the F-03 hash freezes):
    a changed acceptance on a ready task cannot ride the old hash."""
    from factory.state import TaskQueue

    repo = _prose_repo(tmp_path)
    run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)
    queue = TaskQueue(repo / ".factory" / "tasks.json")
    task = queue.get("PROSE-1")
    task.update(status="ready", acceptance_hash="a" * 64,
                approved_at="2026-11-07T09:00:00Z", approved_by="owner")
    queue.save()
    result = run_import(
        repo, session=lambda p: _proposal(
            _wo("PROSE-1", acceptance=[{
                "id": "AC-1", "text": "changed", 
                "check": {"type": "manual", "expect": "owner review"},
            }])
        ), confirm=lambda s: True,
    )
    task = load_tasks(result.queue_path)["tasks"][0]
    assert task["status"] == "draft"
    assert task["acceptance_hash"] == "a" * 64  # history kept
    assert "-> draft (the approved contract changed" in result.summary


# ------------------- review round 1: G1 — the repair answer is scrubbed --


def test_g1_repair_answer_passes_the_egress_seam(tmp_path, monkeypatch):
    """G1: the repair's answer is session text — the literal seat token
    and control sequences it carries never reach the plan, the queue or
    the summary (the scrub runs BEFORE plan_import, like the original
    proposal's)."""
    token = fake_token("f57-g1")
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: token
                        if service == "factory-zai" else None)
    monkeypatch.setattr("factory.importer.run_session_sandboxed",
                        ScriptedRunner([
                            _completed(_proposal(_broken_wo())),
                            _completed(json.dumps({
                                "project": "prose-demo",
                                "tasks": [_wo(
                                    "PROSE-1",
                                    title=f"leak {token} mark \x1b[2Jhere",
                                    acceptance=[{
                                        "id": "AC-1",
                                        "text": f"env {token} echo",
                                        "check": {"type": "manual",
                                                  "expect": "review"},
                                    }],
                                )],
                            })),
                        ]))
    repo = _prose_repo(tmp_path)
    result = run_import(repo, factory_root=tmp_path / "froot",
                        confirm=lambda s: True)
    assert result.applied
    queue_text = (repo / ".factory" / "tasks.json").read_text()
    for blob, label in ((queue_text, "tasks.json"),
                        (result.summary, "the summary")):
        assert token not in blob, label
        assert "\x1b" not in blob, label
    assert "mark" in queue_text and "[redacted]" in queue_text


# --------------------------- review round 1: S1 — calls, all refused --


def test_s1_all_refused_calls_are_stated(tmp_path, monkeypatch):
    """S1: precedence follows CALLS, not accepts — a session whose every
    propose_task call was refused and whose text answer does not parse
    is refused with the fact stated, before the repair (which then also
    gets its chance; the scripted one dies on quota)."""
    record = _completed("done — see the tool results")
    record.proposed_tasks = []
    record.propose_calls = 7
    _fake_sdk(monkeypatch, record)
    _patch_real_session(monkeypatch, [_quota_record()])
    repo = _prose_repo(tmp_path)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=tmp_path / "froot")
    message = str(excinfo.value)
    assert "7 propose_task call(s), all refused" in message

    # and the softer arm: calls made, none accepted, text answer VALID —
    # the text is the proposal, with the fact stated in the summary.
    record2 = _completed(_proposal(_wo("PROSE-1")))
    record2.proposed_tasks = []
    record2.propose_calls = 3
    _fake_sdk(monkeypatch, record2)
    _patch_real_session(monkeypatch, [])
    result = run_import(repo, factory_root=tmp_path / "froot2",
                        confirm=lambda s: True)
    assert result.applied
    assert "none accepted" in result.summary


def test_s3_concurrent_evals_get_their_own_fixture_dirs(tmp_path, monkeypatch):
    """S3: the fixture dir is per-run (run-id unique) — two evals never
    clobber each other's fixture, proven by two scripted runs building
    and sweeping two different dirs."""
    import factory.eval as eval_mod

    swept: list[str] = []
    real_rmtree = eval_mod.shutil.rmtree

    def spy_rmtree(path, *args, **kwargs):
        swept.append(str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(eval_mod.shutil, "rmtree", spy_rmtree)
    for _ in range(2):
        _patch_real_session(monkeypatch, [
            _completed(_proposal(*_corrected_tasks())),
        ])
        eval_mod.run_importer_eval(factory_root=tmp_path, scripted=True)
    fixture_dirs = [p for p in swept if "importer-fixture-" in p]
    assert len(fixture_dirs) == 2 and fixture_dirs[0] != fixture_dirs[1]


# ------------------- review round 2: R2-1 — the shared rule per status --


@pytest.mark.parametrize("field,override", [
    ("risk", {"risk": []}),
    # the gates leg relaxes DEPLOY, not merge: merge auto with
    # risk ["payments"] present is schema-invalid (the risk⇒human rule),
    # and the point here is a LEGAL gates-only change on a frozen task.
    ("gates", {"risk": ["payments"],
               "gates": {"spec_approval": True, "merge": "human",
                         "deploy": "auto"}}),
    ("acceptance", {"risk": ["payments"],
                    "acceptance": [{
                        "id": "AC-1", "text": "changed",
                        "check": {"type": "manual", "expect": "review"},
                    }]}),
])
def test_r2_1_each_contract_field_alone_demotes(tmp_path, field, override):
    """The sibling contract fields are PRESERVED by the non-target legs
    (the seeded task stores risk=["payments"]), so each leg changes
    exactly ONE contract field — dropping any field from
    CONTRACT_FIELDS goes red on its own leg."""
    """n09/n10: each contract field ALONE — risk-only, gates-only,
    acceptance-only — triggers the shared rule on a frozen task; dropping
    any one field from CONTRACT_FIELDS goes red here."""
    from factory.state import TaskQueue

    repo = _prose_repo(tmp_path / field)
    run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)
    queue = TaskQueue(repo / ".factory" / "tasks.json")
    task = queue.get("PROSE-1")
    task.update(status="ready", acceptance_hash="a" * 64,
                approved_at="2026-11-07T09:00:00Z", approved_by="owner",
                risk=["payments"])
    if field == "gates":
        # F-67: the importer can no longer WRITE a non-human gate, so
        # the gates leg's stored task carries one the pre-F-67 way —
        # planted in the queue by hand (schema-legal; approve refuses
        # it). The proposal's own (normalised, human) gates then differ
        # from the stored value: a LEGAL gates-only contract change.
        task["gates"]["deploy"] = "auto"
    queue.save()
    result = run_import(
        repo, session=lambda p: _proposal(_wo("PROSE-1", **override)),
        confirm=lambda s: True,
    )
    after = load_tasks(result.queue_path)["tasks"][0]
    assert after["status"] == "draft", field
    assert after["acceptance_hash"] == "a" * 64  # history kept
    assert "-> draft (the approved contract changed" in result.summary


def test_r2_1_done_is_flagged_never_reopened(tmp_path):
    """R2-1: a contract change on a DONE task keeps the status and flags
    it for the owner's review — reopening verified work is a human
    decision (TASK-CONTRACT's acceptance row), identical both paths."""
    from factory.state import TaskQueue

    def seed_done(repo):
        run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
                   confirm=lambda s: True)
        queue = TaskQueue(repo / ".factory" / "tasks.json")
        task = queue.get("PROSE-1")
        task.update(status="done", acceptance_hash="a" * 64,
                    approved_at="2026-11-07T09:00:00Z", approved_by="owner",
                    risk=["payments"])
        queue.save()

    downgrade = _wo("PROSE-1", risk=[])
    repo_a = _prose_repo(tmp_path / "a")
    seed_done(repo_a)
    result_a = run_import(repo_a, session=lambda p: _proposal(downgrade),
                          confirm=lambda s: True)
    task = load_tasks(result_a.queue_path)["tasks"][0]
    assert task["status"] == "done"           # never reopened
    assert task["risk"] == []                 # the change still carried
    assert "flagged for your review, not reopened" in result_a.summary

    # the accepted-set path: identical
    repo_b = _prose_repo(tmp_path / "b")
    seed_done(repo_b)
    record = _completed("done", proposed_tasks=[downgrade])
    import factory.importer as importer_mod

    def fake_sdk(prompt, repo_arg, **kwargs):
        sink = kwargs.get("record_sink")
        if sink is not None:
            sink(record)
        return record.result or ""

    patch = _MonkeyPatch(importer_mod, "sdk_import_session", fake_sdk)
    patch.set()
    try:
        result_b = run_import(repo_b, factory_root=tmp_path / "fb",
                              confirm=lambda s: True)
    finally:
        patch.unset()
    assert load_tasks(result_b.queue_path)["tasks"][0] == task


def test_r2_1_not_buildable_and_draft_are_left(tmp_path):
    """R2-1: a contract change on a not_buildable task (never executable
    — the change rides the visible diff, the task is already in human
    hands) and on a draft task (an ordinary change) keeps the status."""
    from factory.state import TaskQueue

    repo = _prose_repo(tmp_path)
    run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)
    queue = TaskQueue(repo / ".factory" / "tasks.json")
    queue.get("PROSE-1")["status"] = "not_buildable"
    queue.save()
    result = run_import(
        repo, session=lambda p: _proposal(
            _wo("PROSE-1", risk=[])  # a contract change on not_buildable
        ), confirm=lambda s: True,
    )
    task = load_tasks(result.queue_path)["tasks"][0]
    assert task["status"] == "not_buildable"
    assert "-> draft" not in result.summary
    # a contract change on a DRAFT task is an ordinary change
    result2 = run_import(
        repo, session=lambda p: _proposal(
            _wo("PROSE-1", risk=["payments"])
        ), confirm=lambda s: True,
    )
    task2 = load_tasks(result2.queue_path)["tasks"][0]
    assert task2["status"] == "not_buildable"  # still left, still not draft


# ----------------------- review round 2: R2-2 — forged record scalars --


@pytest.mark.parametrize("calls", [
    fake_token("f57-r2-2"),
    {"x": fake_token("f57-r2-2")},
    True,
    -1,
])
def test_r2_2_forged_propose_calls_never_render(tmp_path, monkeypatch, calls):
    """R2-2: propose_calls comes from the session-writable record and is
    rendered into messages — anything but a non-bool int >= 0 is a forged
    record, refused through keep and repair; the token never appears in
    the message, the saved file, or anything else the factory writes."""
    token = fake_token("f57-r2-2")
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: token
                        if service == "factory-zai" else None)
    record = _completed("not json")
    record.propose_calls = calls
    _fake_sdk(monkeypatch, record)
    _patch_real_session(monkeypatch, [_quota_record()])
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    assert "malformed propose_calls" in message
    assert token not in message
    saved = _saved(root)
    assert saved and token not in saved[0].read_text()


def test_r2_2_forged_repair_turns_never_render(tmp_path, monkeypatch):
    """The repair record's num_turns is rendered too (summary, trace, the
    eval report) — a valid-but-null count renders as unknown, never raw;
    a DICT count (round 3) is a forged record, refused unusable; the
    token never reaches the eval report or its markdown either way."""
    token = fake_token("f57-r2-2")
    import factory.eval as eval_mod

    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: token
                        if service == "factory-zai" else None)

    def _drive(num_turns, root):
        runner = ScriptedRunner([
            _completed(_proposal(_broken_wo())),
            _completed(_proposal(*_corrected_tasks()), num_turns=num_turns),
        ])
        monkeypatch.setattr("factory.importer.run_session_sandboxed", runner)
        return eval_mod.run_importer_eval(factory_root=root, scripted=True)

    # valid but unknown: the repair succeeds, rendered safely
    out = _drive(None, tmp_path)
    assert out["report"]["schema_valid"] is True
    assert out["report"]["repair"]["turns"] is None
    blob = json.dumps(out["report"]) + out["rendered_path"].read_text()
    assert token not in blob
    assert "unknown number of turns" in out["rendered_path"].read_text()

    # round 3: a dict num_turns is a forged record — refused unusable
    out2 = _drive({"leak": token}, tmp_path / "b")
    report2 = out2["report"]
    assert report2["schema_valid"] is False
    assert "record is unusable" in report2["refusal"]
    assert token not in json.dumps(report2)


def test_r2_2_huge_valid_call_count_is_fine(tmp_path, monkeypatch):
    """A huge-but-valid int is a count, not an attack: it renders as
    digits and the flow proceeds (an absurd one still gets the note)."""
    record = _completed("not json")
    record.propose_calls = 10 ** 50
    _fake_sdk(monkeypatch, record)
    _patch_real_session(monkeypatch, [_quota_record()])
    repo = _prose_repo(tmp_path)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=tmp_path / "froot")
    assert "100000000000000000000000000000000000000000000000000" in (
        str(excinfo.value)
    )


# ------------------- review round 2: surviving mutations n03/n12/n14 --


def test_n03_host_side_per_task_size_cap(tmp_path, monkeypatch):
    """n03: one forged task over the per-task size cap is refused
    host-side (the sandbox cap alone does not bound the record)."""
    from factory.importer import PROPOSE_TASK_MAX_CHARS

    record = _completed("done")
    record.proposed_tasks = [
        _wo("BIG", title="x" * (PROPOSE_TASK_MAX_CHARS + 1))
    ]
    _fake_sdk(monkeypatch, record)
    _patch_real_session(monkeypatch, [_quota_record()])
    repo = _prose_repo(tmp_path)
    with pytest.raises(ImportRefused, match="serialises to"):
        run_import(repo, factory_root=tmp_path / "froot")


def test_n12_dep_on_a_queue_only_id_is_accepted(tmp_path):
    """n12: the reference set is the MERGED document — a dependency
    naming an id that exists ONLY in the existing queue (not in the
    proposal) is accepted; building merged_ids from the proposal alone
    goes red here."""
    run_import(tmp_path and _prose_repo(tmp_path),
               session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True) if False else None
    repo = _prose_repo(tmp_path)
    run_import(repo, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)
    # PROSE-1 stays in the queue; the new proposal does not re-propose
    # it but ACCOUNTS for it (F-67 predecessors, its criterion carried
    # by from) and depends on it — the dep still resolves against the
    # merged, queue-only id.
    replacement = _wo("PROSE-2", depends_on=["PROSE-1"],
                      predecessors=["PROSE-1"])
    replacement["acceptance"][0]["from"] = "PROSE-1/AC-1"
    result = run_import(
        repo, session=lambda p: _proposal(replacement),
        confirm=lambda s: True,
    )
    tasks = {t["id"]: t for t in load_tasks(result.queue_path)["tasks"]}
    assert tasks["PROSE-2"]["depends_on"] == ["PROSE-1"]


def test_n14_record_carries_the_call_count(tmp_path):
    """n14: the collector's call count rides the record out (_carry
    stamps propose_calls) — the channel run_import reads it by."""
    from claude_agent_sdk import AssistantMessage

    from factory.importer import make_propose_tool
    from factory.session import run_session

    def _result_message(**overrides):
        from claude_agent_sdk import ResultMessage

        base = dict(
            subtype="success", duration_ms=10, duration_api_ms=8,
            is_error=False, num_turns=2, session_id="s1",
            usage={"input_tokens": 10, "output_tokens": 2},
            model_usage={}, terminal_reason="completed",
        )
        base.update(overrides)
        return ResultMessage(**base)

    collector = _collector(scratch_dir=None)
    tool = make_propose_tool(collector)

    async def _query(*, prompt, options, transport=None):
        yield AssistantMessage(content=[], model="glm-5.3")
        await tool.handler({"task": _wo("WO-1")})
        await tool.handler({"task": _wo("WO-2")})
        await tool.handler({"task": _wo("WO-1", risk="bad")})  # refused
        yield _result_message()

    record = asyncio.run(run_session(
        "reviewer", "import the docs", tmp_path, 5, 10,
        query_fn=_query, base_url="https://api.z.ai/api/anthropic",
        token="t", propose_collector=collector,
    ))
    assert record.propose_calls == 3
    assert len(record.proposed_tasks) == 2  # the refused call stored nothing


# --------------------------- review round 2: S5 — dependency residuals --


@pytest.mark.parametrize("dep", ["PROSE-1", "prose-demo:PROSE-1"])
def test_s5_self_dependency_refused(tmp_path, dep):
    """S5: a task depending on ITSELF (by either spelling) can never run;
    refused on the text path (plan_import owns the rule — the set path
    inherits it)."""
    repo = _prose_repo(tmp_path / dep.replace(":", "-"))
    with pytest.raises(ImportRefused, match="names itself"):
        run_import(repo, session=lambda p: _proposal(
            _wo("PROSE-1", depends_on=[dep])
        ))


@pytest.mark.parametrize("dep", [":", "x:", ":y"])
def test_s5_malformed_cross_repo_refused(tmp_path, dep):
    """S5: ':', 'x:', ':y' are neither local ids nor real cross-repo
    references — refused as malformed."""
    repo = _prose_repo(tmp_path / dep.replace(":", "c"))
    with pytest.raises(ImportRefused, match="malformed"):
        run_import(repo, session=lambda p: _proposal(
            _wo("PROSE-1", depends_on=[dep])
        ))


def test_s5_own_project_prefix_is_local(tmp_path):
    """S5: a dep prefixed with THIS queue's own project name is a local
    dependency by another spelling — judged against the merged ids both
    ways: a real id accepted, a phantom refused."""
    # the prose-demo:PROSE-1 SELF-spelling is refused by the parametrized
    # self-dep test above; here: another task's dep through the prefix.
    repo2 = _prose_repo(tmp_path / "b")
    run_import(repo2, session=lambda p: _proposal(_wo("PROSE-1")),
               confirm=lambda s: True)
    replacement = _wo("PROSE-2", depends_on=["prose-demo:PROSE-1"],
                      predecessors=["PROSE-1"])
    replacement["acceptance"][0]["from"] = "PROSE-1/AC-1"
    result2 = run_import(repo2, session=lambda p: _proposal(replacement),
                         confirm=lambda s: True)
    assert result2.applied
    ghost = _wo("PROSE-3", depends_on=["prose-demo:GHOST"],
                predecessors=["PROSE-1", "PROSE-2"])
    ghost["acceptance"][0]["from"] = "PROSE-1/AC-1"
    ghost["acceptance"].append({
        "id": "AC-2", "text": "carries PROSE-2's criterion",
        "check": {"type": "manual", "expect": "review"},
        "from": "PROSE-2/AC-1",
    })
    with pytest.raises(ImportRefused, match="depends_on names"):
        run_import(repo2, session=lambda p: _proposal(ghost))


def test_s6_two_evals_two_report_files(tmp_path, monkeypatch):
    """S6: the report file is named by the run id — two evals, however
    close in time, write two reports (the same uniqueness as the
    fixture dir)."""
    import factory.eval as eval_mod

    paths = []
    for _ in range(2):
        _patch_real_session(monkeypatch, [
            _completed(_proposal(*_corrected_tasks())),
        ])
        out = eval_mod.run_importer_eval(factory_root=tmp_path,
                                         scripted=True)
        paths.append(out["report_path"])
    assert paths[0] != paths[1] and paths[0].exists() and paths[1].exists()


# ------------------ review round 3: R3-1 — record fields at one wall --


@pytest.mark.parametrize("forged", [
    {"result": 5}, {"result": ["x"]}, {"result": {"a": "TOK"}},
])
def test_r3_1_forged_import_record_field(tmp_path, monkeypatch, forged):
    """R3-1: a runner-returned record with a forged STRING field (result
    as int/list/dict) is refused through the record-unusable path — the
    salvage runs, no AttributeError, and a token-shaped value never
    reaches the message."""
    token = fake_token("f57-r3")
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: token
                        if service == "factory-zai" else None)
    values = {"TOK": token}
    forged = {
        key: (values.get(value, value) if isinstance(value, str) else value)
        for key, value in forged.items()
    }
    record = SessionRecord(seat="reviewer", status="completed",
                           num_turns=3, **forged)
    _patch_real_session(monkeypatch, [record, _completed("x")])
    repo = _prose_repo(tmp_path)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=tmp_path / "froot")
    message = str(excinfo.value)
    assert "record is unusable" in message and "record field" in message
    assert token not in message


@pytest.mark.parametrize("forged", [
    {"result": 5}, {"result": ["x"]}, {"result": {"a": "TOK"}},
    {"status": {"x": "TOK"}, "reason": {"r": "TOK"}},
    {"status": "TOK"},
    {"status": None, "reason": {"r": "TOK"}},
])
def test_r3_1_forged_repair_record_field(tmp_path, monkeypatch, forged):
    """The repair's record, same wall: any forged field refuses the
    import naming the KEPT ORIGINAL (the repair ran; its output is not
    trusted), never a traceback, never the token."""
    token = fake_token("f57-r3")
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: token
                        if service == "factory-zai" else None)
    values = {"TOK": token}
    forged = {
        key: (values.get(value, value) if isinstance(value, str) else value)
        for key, value in forged.items()
    }
    base = {"seat": "reviewer", "status": "completed", "num_turns": 2}
    base.update(forged)
    record = SessionRecord(**base)
    _patch_real_session(monkeypatch, [
        _completed(_proposal(_broken_wo())),
        record,
    ])
    repo = _prose_repo(tmp_path)
    root = tmp_path / "froot"
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    assert "record is unusable" in message
    assert token not in message
    saved = _saved(root)
    assert saved and str(saved[0]) in message  # the original kept + named
    traces = sorted((root / "trace" / PROJECT).glob("*.jsonl"))
    events = [json.loads(line)
              for line in traces[-1].read_text().splitlines()]
    assert [e for e in events if e["type"] == "import_repair"][-1][
        "outcome"] == "record_unusable"


def test_r3_1_the_file_boundary_refuses_forged_fields_every_seat(tmp_path):
    """The ONE wall: _read_record type-checks every field of every
    record that crosses the file boundary — an IMPLEMENTER record with a
    forged field is the same SessionRecordError the orchestrator already
    handles (unknown keys were already refused by from_dict's TypeError)."""
    import subprocess as sp

    from factory.session import SessionRecordError, _read_record

    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps({
        "seat": "implementer", "status": "completed", "result": 5,
        "num_turns": 3,
    }))
    proc = sp.CompletedProcess(["srt"], 0, stdout="", stderr="")
    with pytest.raises(SessionRecordError) as excinfo:
        _read_record(record_path, proc, tmp_path, None, "tok")
    assert "record field result" in str(excinfo.value)
    assert "int" in str(excinfo.value)

    # a clean record still reads, and unknown keys still refuse
    record_path.write_text(json.dumps({
        "seat": "implementer", "status": "completed", "result": "done",
        "num_turns": 3,
    }))
    record = _read_record(record_path, proc, tmp_path, None, "tok")
    assert record.result == "done"
    record_path.write_text(json.dumps({
        "seat": "implementer", "status": "completed", "result": "done",
        "num_turns": 3, "mystery": {"x": 1},
    }))
    with pytest.raises(SessionRecordError, match="valid record"):
        _read_record(record_path, proc, tmp_path, None, "tok")


# ------------------ final sweep: T1/T2 — the wall's last two seams --


def _planted_timeout_run(monkeypatch, tmp_path, record_doc):
    """Drive the real timeout branch of run_session_sandboxed with a
    PLANTED record file: run_sandboxed plants it, then hangs past the
    outer wall clock. Returns the raised TimeoutExpired (or the record
    the factory wrongly used, as ('USED', record))."""
    import factory.session as session_mod
    from factory.session import record_path_for, run_session_sandboxed

    repo = tmp_path / "ws"
    repo.mkdir()
    import subprocess as sp

    for args in (("init", "-q", "-b", "main"),
                 ("-c", "user.name=t", "-c", "user.email=t@x",
                  "commit", "-q", "--allow-empty", "-m", "x")):
        sp.run(["git", "-C", str(repo), *args], check=True,
               capture_output=True)

    def plant_and_hang(command, run_dir, settings_path, **kwargs):
        path = record_path_for(Path(settings_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record_doc))
        raise subprocess.TimeoutExpired(cmd=command, timeout=90.0)

    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: "tok"
                        if service == "factory-zai" else None)
    monkeypatch.setattr(session_mod, "run_sandboxed", plant_and_hang)
    try:
        run_session_sandboxed(
            "reviewer", "import", repo,
            {"docs_allowlist": [], "network_allowlist": []},
            factory_root=tmp_path, settings_path=tmp_path / "s.json",
            max_turns=2, timeout_min=0.05,
        )
    except subprocess.TimeoutExpired as exc:
        return exc
    return ("USED",)
@pytest.mark.parametrize("forged", [
    {"result": 5},
    {"status": {"x": fake_token("f57-q03")}, "reason": None},
])
def test_t1_forged_record_at_timeout_is_absent(tmp_path, monkeypatch, forged):
    """T1/q03: the TIMEOUT path's read (_read_record_quietly) honours the
    record wall — a planted record with a forged field is treated as
    ABSENT: the timeout stays the error (TimeoutExpired raised, never the
    forged record returned), and the token never reaches the exception's
    message or notes."""
    token = fake_token("f57-q03")
    doc = {"seat": "reviewer", "status": "completed", "result": "x",
           "num_turns": 2}
    doc.update(forged)
    outcome = _planted_timeout_run(monkeypatch, tmp_path, doc)
    assert not isinstance(outcome, tuple), "a forged record was USED"
    rendered = str(outcome) + "".join(str(note) for note in outcome.__notes__)
    assert token not in rendered
    assert "record" in rendered  # the timeout's own record-failure note

    # and the quietly reader itself: the forged record reads as absent
    from factory.session import _read_record_quietly

    path = tmp_path / "planted.json"
    path.write_text(json.dumps(doc))
    assert _read_record_quietly(path) is None


@pytest.mark.parametrize("field,items", [
    ("vents", ["a string, not a dict"]),
    ("skills", [5]),
    ("models", [7]),
    ("propose_facts", [["a list, not a dict"]]),
    ("proposed_tasks", ["a string, not a dict"]),
])
def test_t2_list_item_types_pinned_at_the_wall(tmp_path, field, items):
    """T2/q08: the wall checks list ITEM types — vents/skills/models/
    propose_facts/proposed_tasks holding a wrong-typed item is the same
    SessionRecordError, naming the field, at the file boundary."""
    import subprocess as sp

    from factory.session import SessionRecordError, _read_record

    doc = {"seat": "reviewer", "status": "completed", "result": "x",
           "num_turns": 2}
    doc[field] = items
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(doc))
    proc = sp.CompletedProcess(["srt"], 0, stdout="", stderr="")
    with pytest.raises(SessionRecordError) as excinfo:
        _read_record(record_path, proc, tmp_path, None, "tok")
    message = str(excinfo.value)
    assert f"record field {field}" in message
    assert "not a" in message
