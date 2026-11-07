"""F-27 acceptance tests — BUILD-ORDER.md, work order F-27 (vent tool).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f27.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the fixture project's git repo and bare "GitHub" remote, the
  clone manager, the queue, L0 and L1 through the real pinned ``srt``
  sandbox, and the in-process SDK tool (the same ``SdkMcpTool`` machinery a
  live session calls; the handler runs under test exactly as the SDK would
  run it inside a session).
* **Stubbed:** the model. Seat stubs (the f10/f26 pattern) return
  ``SessionRecord``s that may carry ``vents``, the way a real session's
  record does after the runner collected tool calls; ``gh pr create`` is an
  ``open_pr`` recorder and branch protection a ``Protection`` fake.

Architecture note the tests pin: the vent tool's handler executes inside
the SANDBOXED runner process (F-06 execs ``python -m factory.session`` under
srt), where writing the real progress log — outside the run dir — is
denied. So the handler only appends to an in-memory collector; the vents
ride the ``SessionRecord`` out of the sandbox (the same stdout channel as
the record itself), and the orchestrator — outside, unconfined — flushes
them into the task's progress log (AC-2's "appends one record" happens per
tool call, one log entry each, the moment the session that produced them
returns).
"""

import asyncio
import hashlib
import json
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from claude_agent_sdk import AssistantMessage

from factory.contract import acceptance_hash
from factory.mergeorder import Protection
from factory.orchestrator import run_once
from factory.progress import ProgressLog
from factory.publish import PullRequest
from factory.session import SessionRecord, build_options, run_session
from factory.vent import (
    VENT_CATEGORIES,
    VENT_TEXT_CAP,
    VentCollector,
    flush_vents,
    make_vent_tool,
    weeks_vents,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)
REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = textwrap.dedent(
    """
    from impl import answer


    def test_ac_1():
        assert answer() == 42
    """
)
REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})

# The f26 pattern: the fixture remote is a local path the real gh seam
# cannot answer for, so admission gets a strictly-protected fake.
PROTECTED_STRICT = Protection(checked=True, strict=True)

RUN_ID = "20260101T000000Z-ab12cd"


def _result_message(**overrides):
    """A ResultMessage with sane defaults (the f06 helper shape)."""
    from claude_agent_sdk import ResultMessage

    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={
            "glm-5.3": {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.001,
                "contextWindow": 200000,
                "maxOutputTokens": 8192,
            }
        },
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


def _assistant() -> AssistantMessage:
    return AssistantMessage(content=[], model="glm-5.3")


def _vent_dict(**overrides) -> dict:
    """One vent record, the shape the collector stamps per call (AC-2)."""
    record = {
        "run_id": RUN_ID,
        "task_id": "T-1",
        "seat": "implementer",
        "turn": 1,
        "category": "missing_tool",
        "text": "no ripgrep in the sandbox PATH",
    }
    record.update(overrides)
    return record


# ----------------------------------------------------------------- fixture --


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", *, ref: str = REF, **overrides) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42", "check": {"type": "test", "ref": ref}}
    ]
    task = {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-18T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 10},
        "attempts": [],
    }
    task.update(overrides)
    return task


class Env:
    """One fixture project: local repo + bare remote + factory root."""

    def __init__(self, tmp_path: Path, tasks: list | None = None):
        self.tmp = tmp_path
        self.root = tmp_path / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
        profile = {
            "name": "fixture",
            "repo": {
                "path": str(self.repo),
                "default_branch": "main",
                "remote": str(self.remote),
                "merge_method": "squash",
            },
            "commands": {
                "setup": "true",
                "test": f"{sys.executable} -m pytest -q -p no:cacheprovider",
            },
            "paths": {"risk": {}, "protected": [".factory/**"]},
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        self.profile = profile
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        (factory_dir / "project.yaml").write_text(yaml.safe_dump(profile))
        self.queue_path = factory_dir / "tasks.json"
        doc = {"version": 1, "project": "fixture", "tasks": tasks or [_task()]}
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)], check=True
        )
        self.prs: list[PullRequest] = []

    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        doc = json.loads(self.queue_path.read_text())
        return {t["id"]: t for t in doc["tasks"]}[task_id]

    def log(self, task_id: str = "T-1") -> ProgressLog:
        return ProgressLog.for_task(self.root, "fixture", task_id)

    def run(self, session_runner):
        return run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            now=lambda: OFF_PEAK,
            protection=lambda remote, branch: PROTECTED_STRICT,
            pr_state=lambda url: "open",
        )


class Seats:
    """Stub seats (the f10 pattern) that may vent mid-run.

    The implementer writes a correct change; the review seats pass. Vent
    templates are dicts WITHOUT ``run_id``: the stub stamps each with the
    ``run_id``/``task_id`` the orchestrator handed the session — exactly
    what a real session's collector does with the same kwargs. The F-38
    fan-out runs two reviewer sessions; only the first vents (one vent
    per template is what the assertions below count), and the calls log
    is appended under a lock because the fan-out is concurrent.
    """

    def __init__(self, impl_vents=(), review_vents=()):
        self.impl_vents = list(impl_vents)
        self.review_vents = list(review_vents)
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []  # (seat, kwargs)

    @staticmethod
    def _stamped(templates, kwargs):
        return [{**v, "run_id": kwargs["run_id"]} for v in templates]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        with self.lock:
            self.calls.append((seat, kwargs))
            first_review = (
                seat == "reviewer"
                and sum(1 for s, _ in self.calls if s == "reviewer") == 1
            )
        run_dir = Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                result="Implemented answer().",
                vents=self._stamped(self.impl_vents, kwargs),
            )
        vents = self.review_vents if first_review else []
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            result=REVIEW_PASS, vents=self._stamped(vents, kwargs),
        )


def _tree_hashes(root: Path) -> dict[str, str]:
    """Every file under root: relpath -> content hash (change detector)."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _call(tool, **args) -> dict:
    """Invoke an SdkMcpTool's handler the way the SDK does."""
    return asyncio.run(tool.handler(args))


# --------------------------------------------------- F-27 AC-1: the tool --


def test_ac_1():
    """Implementer and reviewer sessions have one extra tool, ``vent(category,
    text)`` — an in-process SDK tool, listed in ``allowed_tools``, with the
    five categories as the only valid ones."""
    # Both seats: the tool rides the built options (allowed_tools + the
    # in-process MCP server), captured from a fake session per seat.
    for seat in ("implementer", "reviewer"):
        captured: dict = {}

        async def _query(*, prompt, options, transport=None):
            captured["options"] = options
            yield _result_message()

        record = asyncio.run(run_session(
            seat, "do the task", REPO_ROOT, 5, 10,
            query_fn=_query, base_url="https://api.z.ai/api/anthropic", token="t",
        ))
        assert record.status == "completed"
        options = captured["options"]
        # The grant names the tool as the CLI sees it (review finding 4).
        assert "mcp__vent__vent" in options.allowed_tools, (seat, options.allowed_tools)
        assert "vent" in options.mcp_servers, seat
        # McpSdkServerConfig is a TypedDict: an in-process server, not a
        # spawned one.
        assert options.mcp_servers["vent"]["type"] == "sdk", seat

    # The tool itself: name, schema-validated categories, handler contract.
    collector = VentCollector(run_id=RUN_ID, task_id="T-1", seat="implementer")
    tool = make_vent_tool(collector)
    assert tool.name == "vent"
    # The CLI-side validation surface: the schema carries exactly the five
    # categories, so a malformed call never reaches the handler in a real
    # session (the SDK answers it as an isError result the model can read).
    schema = tool.input_schema
    assert isinstance(schema, dict), "the vent schema must be full JSON Schema"
    assert sorted(schema["properties"]["category"]["enum"]) == sorted(VENT_CATEGORIES)
    assert set(VENT_CATEGORIES) == {
        "missing_tool", "unclear_contract", "flaky_check", "environment", "other",
    }

    # Every category records; the handler answers success (not isError).
    for category in VENT_CATEGORIES:
        result = _call(tool, category=category, text=f"friction: {category}")
        assert result.get("is_error") is not True, (category, result)
    assert [v["category"] for v in collector.vents] == list(VENT_CATEGORIES)

    # A category outside the enum is refused by the handler (defence in
    # depth for direct calls; the SDK layer raises ValueError into an
    # isError result the model can read and retry).
    with pytest.raises(ValueError):
        _call(tool, category="grievance", text="not a category")

    # Mid-session wiring: a call made during the stream is collected with
    # the turn it happened in and rides the record out of the session.
    collector2 = VentCollector(run_id=RUN_ID, task_id="T-1", seat="reviewer")
    live_tool = make_vent_tool(collector2)

    async def _venting_query(*, prompt, options, transport=None):
        yield _assistant()  # turn 1
        await live_tool.handler({"category": "environment", "text": "npm EPERM"})
        yield _result_message()

    record = asyncio.run(run_session(
        "reviewer", "review the diff", REPO_ROOT, 5, 10,
        query_fn=_venting_query, base_url="https://api.z.ai/api/anthropic",
        token="t", vent_collector=collector2,
    ))
    assert [v["at"] for v in record.vents] == [collector2.vents[0]["at"]]
    assert [{k: v for k, v in vent.items() if k != "at"} for vent in record.vents] == [{
        "run_id": RUN_ID,
        "task_id": "T-1",
        "seat": "reviewer",
        "turn": 1,
        "category": "environment",
        "text": "npm EPERM",
    }], record.vents


def test_build_options_without_a_collector_has_no_vent_server(tmp_path: Path):
    """``build_options`` used directly (f06 option-shape tests) still builds:
    no collector, no MCP server — the grant-list entry alone grants nothing."""
    options = build_options(
        "implementer", tmp_path, 5,
        base_url="https://api.z.ai/api/anthropic", token="t",
    )
    assert "mcp__vent__vent" in options.allowed_tools
    assert options.mcp_servers == {}


# ------------------------------------------- F-27 AC-2: the record's path --


def test_ac_2(tmp_path: Path):
    """Each call appends one record (run id, task id, seat, turn, category,
    text) to the run's progress log outside the clone; the tool writes
    nothing else and text is length-capped."""
    # --- the handler: cap, redaction, and nothing-but-the-record ----------
    collector = VentCollector(run_id=RUN_ID, task_id="T-1", seat="implementer")
    tool = make_vent_tool(collector)
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path)
        before = _tree_hashes(tmp_path)
        long_text = "x" * (VENT_TEXT_CAP + 5_000)
        _call(tool, category="flaky_check", text=long_text)
        _call(tool, category="environment",
              text="the sandbox leaked ghp_0123456789012345678901234567890123456789")
        after = _tree_hashes(tmp_path)
    assert before == after, "the vent tool wrote something other than its record"
    assert len(collector.vents) == 2
    capped = collector.vents[0]
    assert capped["run_id"] == RUN_ID
    assert capped["task_id"] == "T-1"
    assert capped["seat"] == "implementer"
    assert capped["category"] == "flaky_check"
    assert len(capped["text"]) <= VENT_TEXT_CAP
    assert len(capped["text"]) < len(long_text)
    # F-25's invariant, held here too: untrusted session text that reaches
    # orchestrator-owned storage is redacted before it is stored.
    assert "ghp_" not in collector.vents[1]["text"]
    assert "[redacted]" in collector.vents[1]["text"]

    # --- flush: one progress-log entry per vent, timestamped ---------------
    log = ProgressLog.for_task(tmp_path, "fixture", "T-1")
    flushed = flush_vents(log, collector.vents)
    assert len(flushed) == 2
    entries = log.entries()
    assert [e["kind"] for e in entries] == ["vent", "vent"]
    stored = entries[0]
    for key in ("run_id", "task_id", "seat", "turn", "category", "text"):
        assert stored[key] == capped[key], key
    assert stored["at"] == capped["at"]  # stamped at call time (finding 7)
    assert stored["kind"] == "vent"

    # --- end to end: the orchestrator flushes what the sessions vented -----
    env = Env(tmp_path)
    impl_vent = {
        "task_id": "T-1", "seat": "implementer", "turn": 1,
        "category": "missing_tool", "text": "no ripgrep in the sandbox PATH",
    }
    review_vent = {
        "task_id": "T-1", "seat": "reviewer", "turn": 1,
        "category": "unclear_contract",
        "text": "AC-1 wording does not name the fixture",
    }
    seats = Seats(impl_vents=[impl_vent], review_vents=[review_vent])
    result = env.run(seats)
    assert result.outcome == "pr_opened", result

    # The sessions were told which run and task they vent for.
    attempt = env.task()["attempts"][0]
    for seat, kwargs in seats.calls:
        assert kwargs["run_id"] == attempt["run_id"], seat
        assert kwargs["task_id"] == "T-1", seat

    # Both vents landed in the run's progress log — one entry each, in call
    # order, carrying every AC-2 field, against the run's own id.
    vents = [e for e in env.log().entries() if e.get("kind") == "vent"]
    assert [v["seat"] for v in vents] == ["implementer", "reviewer"]
    assert [v["category"] for v in vents] == ["missing_tool", "unclear_contract"]
    for vent, expected in zip(vents, (impl_vent, review_vent)):
        for key in ("task_id", "seat", "turn", "category", "text"):
            assert vent[key] == expected[key], key
        assert vent["run_id"] == attempt["run_id"]
    # The log is outside every clone: canonical location, not under runs/.
    log_path = env.log().path
    assert log_path == env.root / "progress" / "fixture" / "T-1.jsonl"
    assert (env.root / "runs") not in log_path.parents

    # Vents are telemetry for the owner (F-11/F-19), not fresh-session
    # context: the log's prompt rendering carries the round entry, never a
    # vent's text.
    rendered = env.log().render()
    assert "no ripgrep in the sandbox PATH" not in rendered
    assert "AC-1 wording" not in rendered
    assert "outcome" in rendered


def test_sandboxed_spec_carries_run_and_task_ids(tmp_path: Path, monkeypatch):
    """The sandboxed runner is a separate process: the run/task ids travel
    in the spec, so the in-sandbox collector can stamp every record."""
    from factory import session as session_mod
    from factory.runs import cleanup_run, create_run

    run = create_run("proj", REPO_ROOT, factory_root=tmp_path)
    captured: dict = {}

    def fake_run_sandboxed(cmd, run_dir, settings_path, **kwargs):
        captured["spec"] = json.loads(Path(cmd[-1]).read_text())
        record = SessionRecord(seat="implementer", status="completed")
        # F-56 re-seat: the record rides the file the spec names.
        from factory.session import record_path_for

        record_path = record_path_for(Path(settings_path))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record.to_dict()))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(session_mod, "run_sandboxed", fake_run_sandboxed)
    session_mod.run_session_sandboxed(
        seat="implementer", prompt="p", run_dir=run.root, profile={},
        max_turns=1, timeout_min=1, token="stub-token",
        run_id=run.run_id, task_id="T-9",
    )
    cleanup_run(run)
    assert captured["spec"]["run_id"] == run.run_id
    assert captured["spec"]["task_id"] == "T-9"


# --------------------------------------- F-27 AC-3: venting changes nothing --


def test_ac_3(tmp_path: Path):
    """Venting never changes a run's outcome or budget: a mid-run vent leaves
    the attempt record identical (session level and in tasks.json)."""
    # --- session level: the record differs only by its vents ---------------
    async def _plain(*, prompt, options, transport=None):
        yield _result_message()

    collector = VentCollector(run_id=RUN_ID, task_id="T-1", seat="implementer")
    tool = make_vent_tool(collector)

    async def _venting(*, prompt, options, transport=None):
        yield _assistant()
        await tool.handler({"category": "other", "text": "just friction"})
        yield _result_message()

    def _run(query):
        return asyncio.run(run_session(
            "implementer", "do the task", REPO_ROOT, 5, 10,
            query_fn=query, base_url="https://api.z.ai/api/anthropic", token="t",
            vent_collector=collector if query is _venting else None,
        )).to_dict()

    plain = _run(_plain)
    venting = _run(_venting)
    strip = {"vents", "duration_ms"}  # the vent list; measured wall time
    assert {k: v for k, v in plain.items() if k not in strip} == {
        k: v for k, v in venting.items() if k not in strip
    }
    assert plain["vents"] == []
    assert len(venting["vents"]) == 1

    # --- orchestrator level: attempts in tasks.json are identical ----------
    env_a, env_b = tmp_path / "a", tmp_path / "b"
    env_a.mkdir()
    env_b.mkdir()
    quiet = Env(env_a)
    venting_env = Env(env_b)
    venting_seats = Seats(
        impl_vents=[_vent_dict()],
        review_vents=[_vent_dict(seat="reviewer", category="other")],
    )
    result_quiet = quiet.run(Seats())
    result_venting = venting_env.run(venting_seats)
    assert result_quiet.outcome == result_venting.outcome == "pr_opened"
    # Two separate runs name different run ids, timestamps and report
    # paths; everything else about the attempt must be identical — strip
    # the run's own identity and compare the substance (the venting run's
    # budget numbers, outcome, model, prompt versions, everything).
    identity = {"run_id", "started", "ended", "validator_report"}
    a_attempt, b_attempt = quiet.task()["attempts"][0], venting_env.task()["attempts"][0]
    assert {k: v for k, v in a_attempt.items() if k not in identity} == {
        k: v for k, v in b_attempt.items() if k not in identity
    }
    # The venting run's budget accounting is the same numbers...
    a, b = a_attempt, b_attempt
    assert (a["turns"], a["input_tokens"], a["output_tokens"]) == (
        b["turns"], b["input_tokens"], b["output_tokens"])
    # ...and its vents went to the progress log, never the attempt record.
    assert [e for e in venting_env.log().entries() if e.get("kind") == "vent"]
    assert not [e for e in quiet.log().entries() if e.get("kind") == "vent"]


# ------------------------------------ F-27 AC-4: the status/meta-loop seam --


def test_ac_4(tmp_path: Path):
    """The week's vents, grouped by category — the one reading seam
    `factory status` (F-11) lists and the F-19 review reads. Read-only."""
    now = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
    fresh_at = "2026-10-16T15:00:00Z"  # 2 days ago — inside the week
    today_at = "2026-10-18T09:00:00Z"
    stale_at = "2026-10-05T15:00:00Z"  # 13 days ago — outside

    log1 = ProgressLog.for_task(tmp_path, "fixture", "T-1")
    flush_vents(log1, [_vent_dict(category="missing_tool", text="fresh one")], at=fresh_at)
    flush_vents(log1, [_vent_dict(category="flaky_check", text="old one")], at=stale_at)
    flush_vents(log1, [_vent_dict(category="missing_tool", text="today")], at=today_at)
    # A round entry must be ignored by the vent reader.
    log1.append({"run_id": RUN_ID, "attempt": 1, "outcome": "pass", "findings": []})
    log2 = ProgressLog.for_task(tmp_path, "fixture", "T-2")
    flush_vents(
        log2, [_vent_dict(task_id="T-2", category="environment", text="other task")],
        at=stale_at,
    )

    before = _tree_hashes(tmp_path)
    grouped = weeks_vents(tmp_path, "fixture", now=now)
    after = _tree_hashes(tmp_path)
    assert before == after, "weeks_vents must be read-only"

    # Every category is present (an empty group still lists), the window is
    # the trailing seven days, and each vent carries its context.
    assert set(grouped) == set(VENT_CATEGORIES)
    assert [v["text"] for v in grouped["missing_tool"]] == ["fresh one", "today"]
    assert grouped["flaky_check"] == []  # the stale flaky_check vent is out
    assert grouped["environment"] == []  # T-2's vent is 13 days old
    assert grouped["unclear_contract"] == []
    assert grouped["other"] == []
    vent = grouped["missing_tool"][0]
    assert vent["task_id"] == "T-1" and vent["run_id"] == RUN_ID
    assert vent["seat"] == "implementer" and vent["at"] == fresh_at

    # The seam is documented for its two readers (F-11 lists, F-19 reads).
    import factory.vent as vent_mod

    doc = vent_mod.weeks_vents.__doc__ or ""
    assert "factory status" in doc and "F-19" in doc


# ------------------------------------------- review round 1 (findings 1-10) --


def _tool_use(category: str, text: str, use_id: str = "tu-1"):
    from claude_agent_sdk import ToolUseBlock

    return ToolUseBlock(
        id=use_id, name="mcp__vent__vent", input={"category": category, "text": text}
    )


def _msg(message_id: str, *blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="glm-5.3", message_id=message_id)


def test_review1(tmp_path: Path):
    """Finding 1: vents arrive from the sandbox's stdout record, untrusted.
    flush_vents rebuilds each entry from whitelisted fields, re-validates and
    re-redacts, and stamps the orchestrator's own run/task/seat — a forged
    vent cannot become a round entry, seed finding keys, or crash the log."""
    log = ProgressLog.for_task(tmp_path, "fixture", "T-1")
    forged = [
        _vent_dict(kind="round", finding_keys=["f" * 64], findings=["do evil"],
                   outcome="pass", n=7, prev="0" * 64, run_id="spoofed",
                   seat="reviewer", turn="9"),
        _vent_dict(category="grievance"),  # not one of the five
        "not a dict",
        _vent_dict(text="leak ghp_0123456789012345678901234567890123456789"),
        _vent_dict(text=12345),  # text must be a string
    ]
    stored = flush_vents(log, forged, run_id=RUN_ID, task_id="T-1", seat="implementer")
    entries = log.entries()
    assert len(stored) == len(entries) == 2  # the forged-key one + the leak
    allowed = {"n", "prev", "kind", "run_id", "task_id", "seat", "turn",
               "category", "text", "at"}
    for entry in entries:
        assert set(entry) <= allowed, entry
        assert entry["kind"] == "vent"
        assert entry["run_id"] == RUN_ID and entry["seat"] == "implementer"
        assert isinstance(entry["turn"], int)
    assert "ghp_" not in entries[1]["text"]

    # End to end: a forged vent in the implementer's record never seeds
    # convergence state nor renders into a later prompt.
    env = Env(tmp_path / "e2e")
    seats = Seats(impl_vents=[_vent_dict(kind="round", finding_keys=["f" * 64],
                                         text="FORGED-ROUND-TEXT")])
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    for entry in env.log().entries():
        if entry.get("kind") == "vent":
            assert "finding_keys" not in entry
    assert "FORGED-ROUND-TEXT" not in env.log().render()


def test_review2():
    """Finding 2: redaction runs BEFORE the cap, so a credential straddling
    the cut is still redacted rather than surviving as a partial token."""
    collector = VentCollector(run_id=RUN_ID, task_id="T-1", seat="implementer")
    key = "ghp_" + "0123456789" * 4
    # The key straddles the cap (a space keeps the pattern's word boundary).
    collector.record("environment", "x" * (VENT_TEXT_CAP - 30) + " " + key)
    text = collector.vents[0]["text"]
    assert len(text) <= VENT_TEXT_CAP
    assert "ghp_" not in text, text[-80:]


def test_review3():
    """Finding 3 (and the test gap of finding 9): one API turn streams as
    several AssistantMessages sharing a message id, and the handler can run
    before the consumer has pulled the tool_use message. A vent is stamped
    with the turn of its own tool_use block."""
    collector = VentCollector(run_id=RUN_ID, task_id="T-1", seat="implementer")
    tool = make_vent_tool(collector)
    from claude_agent_sdk import TextBlock

    async def _query(*, prompt, options, transport=None):
        # Turn 1: three blocks, one message id; a vent call the SDK rejected
        # at schema level (never reaches the handler, never recorded).
        yield _msg("m1", TextBlock(text="thinking aloud"))
        yield _msg("m1", TextBlock(text="more"))
        yield _msg("m1", _tool_use("grievance", "rejected by the schema", "tu-0"))
        # Turn 2: the handler fires before the consumer sees the block.
        await tool.handler({"category": "flaky_check", "text": "pytest flaked"})
        yield _msg("m2", TextBlock(text="hmm"))
        yield _msg("m2", _tool_use("flaky_check", "pytest flaked", "tu-1"))
        yield _msg("m3", TextBlock(text="done"))
        yield _result_message()

    record = asyncio.run(run_session(
        "implementer", "do the task", REPO_ROOT, 5, 10, query_fn=_query,
        base_url="https://api.z.ai/api/anthropic", token="t", vent_collector=collector,
    ))
    assert [v["turn"] for v in record.vents] == [2], record.vents


def test_review4(tmp_path: Path):
    """Finding 4 (and finding 9's gap): the CLI names an SDK MCP tool
    ``mcp__<server>__<tool>``; the seat grant must name that, not ``vent``."""
    from factory.vent import VENT_QUALIFIED_NAME

    assert VENT_QUALIFIED_NAME == "mcp__vent__vent"
    for seat in ("implementer", "reviewer"):
        options = build_options(
            seat, tmp_path, 5, base_url="https://api.z.ai/api/anthropic", token="t",
            vent_collector=VentCollector(run_id=RUN_ID, task_id="T-1", seat=seat),
        )
        assert VENT_QUALIFIED_NAME in options.allowed_tools, (seat, options.allowed_tools)
        (server_name,) = options.mcp_servers
        tool_name = make_vent_tool(VentCollector(RUN_ID, "T-1", seat)).name
        assert f"mcp__{server_name}__{tool_name}" == VENT_QUALIFIED_NAME


def test_review5(tmp_path: Path, monkeypatch, capsys):
    """Finding 5: a failed vent flush is telemetry lost, never a crashed
    run — the attempt is still recorded and the outcome unchanged."""
    from factory import orchestrator as orch
    from factory.progress import ProgressLogError

    def broken_flush(*args, **kwargs):
        raise ProgressLogError("disk on fire")

    monkeypatch.setattr(orch, "flush_vents", broken_flush)
    env = Env(tmp_path)
    result = env.run(Seats(impl_vents=[_vent_dict()]))
    assert result.outcome == "pr_opened", result
    assert len(env.task()["attempts"]) == 1
    assert "disk on fire" in capsys.readouterr().err


def test_review6(tmp_path: Path):
    """Finding 6: one corrupted task log does not hide every other task's
    vents; the unreadable file is reported, not raised."""
    now = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
    good = ProgressLog.for_task(tmp_path, "fixture", "T-1")
    flush_vents(good, [_vent_dict(text="survives")], at="2026-10-17T10:00:00Z")
    bad = ProgressLog.for_task(tmp_path, "fixture", "T-0")
    bad.path.write_text("{not json\n")
    unreadable: list[str] = []
    grouped = weeks_vents(tmp_path, "fixture", now=now, unreadable=unreadable)
    assert [v["text"] for v in grouped["missing_tool"]] == ["survives"]
    assert unreadable == [str(bad.path)]


def test_review7(tmp_path: Path):
    """Finding 7: each vent is stamped when it was made (in the collector),
    not when the session returned; the flush stamp is only the fallback,
    and a stamp from the future (untrusted) is clamped to the flush time."""
    collector = VentCollector(run_id=RUN_ID, task_id="T-1", seat="implementer")
    collector.record("other", "early")
    made_at = collector.vents[0]["at"]
    assert made_at.endswith("Z")
    log = ProgressLog.for_task(tmp_path, "fixture", "T-1")
    flush_vents(log, [
        collector.vents[0],
        _vent_dict(at="2099-01-01T00:00:00Z"),
        _vent_dict(at="yesterday-ish"),
    ], at="2030-01-01T00:00:00Z")  # the flush: after the real call above
    stamps = [e["at"] for e in log.entries()]
    assert stamps[0] == made_at
    assert stamps[1] == "2030-01-01T00:00:00Z"  # 2099 clamped to the flush
    assert stamps[2] == "2030-01-01T00:00:00Z"  # unparsable -> the flush


def test_review8(tmp_path: Path, monkeypatch):
    """Finding 10: flushing m vents reads and verifies the log once, not m
    times, and the batch still chains like single appends."""
    log = ProgressLog.for_task(tmp_path, "fixture", "T-1")
    log.append({"run_id": RUN_ID, "outcome": "pass"})
    reads = {"n": 0}
    original = ProgressLog._read

    def counting(self):
        reads["n"] += 1
        return original(self)

    monkeypatch.setattr(ProgressLog, "_read", counting)
    flush_vents(log, [_vent_dict(text=f"v{i}") for i in range(5)], at="2026-10-18T14:00:00Z")
    assert reads["n"] == 1
    monkeypatch.setattr(ProgressLog, "_read", original)
    assert [e["n"] for e in log.entries()] == [1, 2, 3, 4, 5, 6]
