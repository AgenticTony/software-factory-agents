"""F-56 — Session record transport.

Contract tests, exact names ``test_ac_1``..``test_ac_5`` (the queue's
acceptance refs), plus review/edge tests. Red-first: written and run
against untouched ``af7cf2c`` before any implementation; each AC's red
line is quoted in ``.factory/BOOTSTRAP.md``.

The goal behind the ACs (BUILD-ORDER §F-56): a session that completed is
never lost to how its result travels back. The record crosses srt's
stdout pipe as one JSON line today — three of four real jobfinderos
importer runs lost it to a truncated line (the fourth's line was exactly
81,920 bytes, cut inside ``permission_denials``). The record travels by
file; stdout carries only ``factory-trace`` lines; a missing record says
exactly why it is missing.

Real-srt policy (F-05): the sandbox legs attempt the real action inside a
real ``srt`` and assert on the actual outcome, never on the settings JSON
alone. They are never skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from _support.f05 import _child, _fixture_repo
from _support.f12 import _git_repo
from _support.f63 import importer_fixture_approve
from _support.tokens import fake_token
from claude_agent_sdk import ResultMessage

from factory.sandbox import SRT_PIN
from factory.session import (
    SessionConfigError,
    SessionRecord,
)

REPO = Path(__file__).resolve().parent.parent
PROJECT = "fixture"

# The record transport's naming convention (production: record_path_for /
# the -record sibling of the srt settings; mirrored here so the stubs can
# play the entry's part without the implementation existing).
RECORD_FILE_NAME = "record.json"


def _record_file(settings_path: Path) -> Path:
    return (settings_path.parent
            / (settings_path.name + "-record")
            / RECORD_FILE_NAME)


# ---------------------------------------------------------------- helpers --


def _entry_stub(monkeypatch, captured: dict, *, stdout="", stderr="",
                returncode=0, on_run=None, write_record=True):
    """Stub ``run_sandboxed`` inside factory.session as the in-sandbox
    entry: write the record file (the transport this WO builds) from the
    spec the orchestrator stages, return the given streams/exit.

    ``write_record=False`` is the failed entry (nothing written); or pass
    ``on_run(spec, record_path)`` to perturb the flow (plant something
    else at the path)."""

    from factory import session as session_mod

    def fake_run_sandboxed(cmd, run_dir, settings_path, **kwargs):
        captured["cmd"] = list(cmd)
        captured["run_dir"] = Path(run_dir)
        captured["settings_path"] = Path(settings_path)
        captured["kwargs"] = kwargs
        spec = json.loads(Path(cmd[-1]).read_text())
        captured["spec"] = spec
        record_path = _record_file(Path(settings_path))
        if on_run is not None:
            on_run(spec, record_path)
        elif write_record:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(
                SessionRecord(seat=spec["seat"], status="completed").to_dict()
            ))
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(session_mod, "run_sandboxed", fake_run_sandboxed)


def _result_message(**overrides) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={},
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


def _run_stream(message: ResultMessage):
    """run_session over one injected ResultMessage (the f06 pattern)."""
    import asyncio

    from factory.session import run_session

    async def query(*, prompt, options, transport=None):
        yield message

    return asyncio.run(run_session(
        "reviewer", "do it", REPO, 5, 10, query_fn=query, token="tok"))


# --------------------------------------------------- AC-1: file transport --

def test_ac_1(tmp_path: Path, monkeypatch):
    """The record travels by file: a 2 MB record whose stdout is cut
    mid-line still yields the full record; fan-out seats get distinct
    record paths; a symlink at the record path is refused; the spec names
    the path and it lives outside the clone."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    captured: dict = {}
    # A 2 MB record (a huge result — permission_denials gets its own bound
    # in AC-3) and a stdout torn mid factory-trace line: the pipe is cut,
    # the file is whole.
    big = SessionRecord(
        seat="implementer", status="completed", result="x" * (2 << 20),
    )

    def on_run(spec, record_path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(big.to_dict()))

    _entry_stub(monkeypatch, captured,
                stdout='factory-trace {"type": "tool_call", "args": "cut mid-j',
                on_run=on_run)
    record = session_mod.run_session_sandboxed(
        "implementer", "p", run.root, {},
        factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
    )
    assert record.status == "completed"
    assert record.result == "x" * (2 << 20), "the full 2 MB record did not arrive"

    # The spec named the record path before the session started; it is
    # outside the clone, in the session's own -record sibling.
    spec = captured["spec"]
    assert spec["record_path"] == str(_record_file(run.settings_path))
    assert run.root not in Path(spec["record_path"]).parents

    # Fan-out: two seats, two settings, two record paths — never shared.
    seen: list[Path] = []

    def fanout_run(spec, record_path):
        seen.append(record_path)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(
            SessionRecord(seat=spec["seat"], status="completed").to_dict()))

    _entry_stub(monkeypatch, {}, on_run=fanout_run)
    records = session_mod.run_sessions_parallel(
        [("reviewer", "r1"), ("verifier", "v1")], run.root, {},
        factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
    )
    assert [r.status for r in records] == ["completed", "completed"]
    assert len(seen) == 2 and seen[0] != seen[1], seen
    # The per-session settings are derived in spec order; which thread
    # writes first is scheduling — compare as a set.
    assert sorted(p.parent.name for p in seen) == sorted([
        f"{run.settings_path.name}-r1.json-record",
        f"{run.settings_path.name}-v1.json-record",
    ])

    # A symlink planted at the record path is refused at read time — the
    # orchestrator never follows it (O_NOFOLLOW), and the failure says
    # the OPEN refused (ELOOP), not merely that a record is missing: a
    # dangling symlink would read as "missing" either way, so the
    # assertion pins the mechanism (mutation-checked: removing O_NOFOLLOW
    # goes red here).
    _entry_stub(monkeypatch, {}, returncode=1,
                on_run=lambda spec, record_path: record_path.symlink_to(
                    tmp_path / "elsewhere.json"))
    with pytest.raises(SessionConfigError, match="cannot be opened"):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )


def test_ac_1_real_srt_record_dir_persists_on_the_host(tmp_path: Path):
    """The record path must be a carve-out that persists on the host: on
    Linux, srt mounts a tmpfs over the read-denied factory root, so a
    write under it that is NOT carved out never reaches the host (F-47
    round 1, finding 1). Real srt writes into the session's -record dir
    through settings built exactly as _run_prepared builds them — the
    file must exist on the host afterwards (macOS and Linux CI alike)."""
    from _support.f05 import _child, _fixture_repo

    from factory.runs import create_run
    from factory.sandbox import build_settings, run_sandboxed, write_settings

    root = tmp_path / "factory-root"
    run = create_run(PROJECT, _fixture_repo(tmp_path), factory_root=root)
    record_dir = _record_file(run.settings_path).parent
    record_dir.mkdir(parents=True)
    settings = build_settings(
        {}, run.root, model_endpoint="api.z.ai",
        factory_root=root, record_write=record_dir,
    )
    assert str(record_dir) in settings["filesystem"]["allowWrite"]
    assert str(record_dir.resolve()) in settings["filesystem"]["allowRead"]
    write_settings(settings, run.settings_path)
    target = record_dir / "probe.txt"
    proc = run_sandboxed(
        _child(
            "import sys, os\n"
            "with open(sys.argv[1], 'w') as fh:\n"
            "    fh.write('record bytes')\n"
            "print('wrote', flush=True)",
            [str(target)],
        ),
        run.root, run.settings_path, timeout=60,
    )
    assert "wrote" in proc.stdout, proc.stdout + proc.stderr
    assert target.exists(), (
        "the record dir write did not reach the host — the record file "
        "transport would silently lose every record on Linux")


# ------------------------------------------- AC-2: failures explain themselves --

def test_ac_2(tmp_path: Path, monkeypatch):
    """A missing or unreadable record raises an error naming the srt exit
    code, the signal if any, a scrubbed stderr tail, and the saved stdout
    AND stderr (0600, seam-scrubbed) under <factory-root>/sessions/."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)

    # 1. Exit 1 with stderr: all three facts in the message, both streams
    # saved together, the planted token in neither.
    token = fake_token("f56-record", prefix="tok-secret-")
    _entry_stub(monkeypatch, {}, returncode=1, write_record=False,
                stderr=f"boom: the command failed with {token}\n")
    with pytest.raises(SessionConfigError) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token=token,
        )
    message = str(excinfo.value)
    assert "exit 1" in message, message
    assert "boom: the command failed" in message, message
    assert token not in message, "the raw token reached the error message"
    sessions_dir = tmp_path / "sessions"
    saved = sorted(sessions_dir.iterdir())
    assert sorted(p.name.split(".", 1)[1] for p in saved) == [
        "stderr.txt", "stdout.txt"], saved
    for path in saved:
        assert token not in path.read_text(), path
        assert path.stat().st_mode & 0o777 == 0o600, path
    assert "the command failed" in saved[0].read_text()
    assert str(saved[1]) in message, "the saved stdout path must be named"
    assert str(saved[0]) in message, "the saved stderr path must be named"

    # 2. A signal death: returncode -9 names SIGKILL.
    _entry_stub(monkeypatch, {}, returncode=-9, stderr="",
                write_record=False)
    with pytest.raises(SessionConfigError) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    assert "SIGKILL" in str(excinfo.value), str(excinfo.value)

    # 3. The record-failure type: every branch raises it (the importer's
    # salvage keys on it).
    from factory.session import SessionRecordError
    assert issubclass(SessionRecordError, SessionConfigError)
    _entry_stub(monkeypatch, {}, returncode=1, stderr="x",
                write_record=False)
    with pytest.raises(SessionRecordError):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )


# --------------------------------------------- AC-3: permission_denials bound --

def test_ac_3():
    """permission_denials is bounded where the record is built: each
    tool_input capped at the documented size, the list capped at the
    documented count, the dropped count recorded, truncation marked; a
    small list passes through unchanged."""
    def denial(n: int) -> dict:
        return {
            "tool_name": "Bash",
            "tool_input": {"command": "x" * 5000, "n": n},
        }

    record = _run_stream(_result_message(
        permission_denials=[denial(n) for n in range(200)]))
    # The list is bounded (200 in, fewer out — the substantive defect).
    assert len(record.permission_denials) < 200, (
        len(record.permission_denials))
    from factory.session import (
        DENIAL_LIST_MAX,
        DENIAL_TOOL_INPUT_MAX_CHARS,
    )
    assert len(record.permission_denials) == DENIAL_LIST_MAX
    dropped = getattr(record, "permission_denials_dropped", None)
    assert dropped == 200 - DENIAL_LIST_MAX, dropped
    total = len(json.dumps(record.to_dict()))
    bound = DENIAL_LIST_MAX * (DENIAL_TOOL_INPUT_MAX_CHARS + 512) + (2 << 20)
    assert total < bound, total
    for entry in record.permission_denials:
        value = entry.get("tool_input")
        text = value if isinstance(value, str) else json.dumps(value)
        assert len(text) <= DENIAL_TOOL_INPUT_MAX_CHARS, len(text)
        assert entry.get("tool_input_truncated") is True
    # Small lists with small inputs ride through unchanged — no dropped
    # count, no truncation markers.
    small = [{"tool_name": "Bash", "tool_input": {"command": "ls"}},
             {"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}]
    small_record = _run_stream(_result_message(permission_denials=small))
    assert small_record.permission_denials == small
    assert not any("tool_input_truncated" in e for e in small_record.permission_denials)
    assert small_record.permission_denials_dropped == 0


# ------------------------------------------------ AC-4: the 81,920-byte cut --

# srt's own words, quoted from node_modules/@anthropic-ai/sandbox-runtime/
# dist/cli.js (pinned 0.0.79):
#   line 315 — the dead-control-channel kill:
SRT_CONTROL_KILL_LINE = (
    "Error: control fd 3 failed before delivering an update: "
    "read ECONNRESET. Refusing to run the command without the control "
    "channel it asks for."
)
#   line 458 — the command died by a signal srt observed:
SRT_SIGNAL_LINE = "Process killed by signal: SIGKILL"


def test_ac_4(tmp_path: Path, monkeypatch):
    """The 81,920-byte truncation, reproduced or bounded: when srt
    reports killing the command (its own stderr signature, quoted from
    cli.js), the record-failure error says so explicitly; the factory's
    own spawn never passes --control-fd, so the dead-channel kill cannot
    fire in a factory run; and under real srt a large single line
    survives a live reader at the observed cut size."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    captured: dict = {}

    # 1. srt's dead-control-channel kill signature → the error names it.
    _entry_stub(monkeypatch, captured, returncode=1, write_record=False,
                stderr=SRT_CONTROL_KILL_LINE + "\n")
    with pytest.raises(SessionConfigError) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    message = str(excinfo.value)
    assert "srt reported killing the command" in message, message

    # 2. srt's signal-death signature → reported likewise.
    _entry_stub(monkeypatch, {}, returncode=1, write_record=False,
                stderr=SRT_SIGNAL_LINE + "\n")
    with pytest.raises(SessionConfigError) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    assert "killed by a signal" in str(excinfo.value), str(excinfo.value)

    # 3. The factory's spawn passes no --control-fd (pinned on the real
    # argv run_sandboxed builds): the dead-channel path is configured out.
    assert "--control-fd" not in captured["cmd"], captured["cmd"]


def test_ac_4_real_srt_large_line_survives_a_live_reader(tmp_path: Path):
    """The size/boundary probe under real srt: one large single line —
    at exactly the observed 81,920 bytes and at 2 MB — crosses the pipe
    whole while the orchestrator's pump keeps reading. Ruled out: the
    pipe, the pump and size alone (a 400 KB record was already known to
    survive); what cuts a line is the READER stopping (the wall-clock
    kill / the F-47 force-close), which the file transport makes
    irrelevant to the record."""
    from _support.f05 import _child, _fixture_repo

    from factory.runs import create_run
    from factory.sandbox import build_settings, run_sandboxed, write_settings

    run = create_run(PROJECT, _fixture_repo(tmp_path), factory_root=tmp_path)
    settings = build_settings({}, run.root, model_endpoint="api.z.ai",
                              factory_root=tmp_path)
    write_settings(settings, run.settings_path)
    for size in (81_920, 2 << 20):
        child = _child(
            "import sys\n"
            "sys.stdout.write('x' * int(sys.argv[1]) + '\\n')\n"
            "sys.stdout.flush()\n",
            [str(size)],
        )
        proc = run_sandboxed(child, run.root, run.settings_path, timeout=120)
        assert len(proc.stdout.strip()) == size, (
            size, len(proc.stdout), proc.stderr)


# ---------------------------------------------- AC-5: the importer salvages --

def test_ac_5(tmp_path: Path, monkeypatch):
    """A completed importer session whose record is unusable: the
    $TMPDIR/proposal.json in its scratch is salvaged exactly as the F-12
    abort path does — before cleanup — and the refusal names the saved
    file and the exact `factory import --proposal` command; with no
    proposal in the scratch, the refusal says so plainly."""
    from factory.importer import ImportRefused, run_import
    from factory.session import SessionRecordError

    repo = _git_repo(tmp_path)
    factory_root = tmp_path / "factory-root"
    importer_fixture_approve(factory_root, repo)  # F-63

    def broken_record(seat, prompt, run_dir, profile, **kwargs):
        # The session ran; the record never became usable (what
        # _run_prepared raises for a missing/unusable record file). The
        # scratch still holds the proposal the session wrote.
        scratch = (Path(kwargs["settings_path"]).parent
                   / (Path(kwargs["settings_path"]).name + "-scratch"))
        proposal = scratch / "tmp" / "proposal.json"
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_text('{"project": "proj", "tasks": []}')
        raise SessionRecordError(
            "the sandboxed session entry's record file is missing; "
            "srt exit 0", exit_code=0)

    monkeypatch.setattr("factory.importer.run_session_sandboxed",
                        broken_record)
    monkeypatch.setattr("factory.importer.seat_token", lambda: "tok")
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=factory_root)
    message = str(excinfo.value)
    assert "saved to" in message, message
    assert "factory import --proposal" in message, message
    saved = [p for p in (factory_root / "import").iterdir()
             if p.name.endswith(".proposal.json")]
    assert len(saved) == 1, saved
    assert saved[0].read_text() == '{"project": "proj", "tasks": []}'

    # No proposal in the scratch: the refusal says so plainly.
    def no_record_no_proposal(seat, prompt, run_dir, profile, **kwargs):
        raise SessionRecordError(
            "the sandboxed session entry's record file is missing; "
            "srt exit 0", exit_code=0)

    monkeypatch.setattr("factory.importer.run_session_sandboxed",
                        no_record_no_proposal)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=factory_root)
    assert "no proposal was written" in str(excinfo.value), str(excinfo.value)


# =======================================================================
# Review and edge tests
# =======================================================================


def test_review_timeout_after_record_written_uses_it(tmp_path, monkeypatch):
    """A timeout AFTER the record was written (the runner hung in
    teardown past the outer wall clock): the completed record is used,
    not discarded — the goal is never to lose a session that finished."""
    import subprocess as sp

    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    written = SessionRecord(seat="implementer", status="completed",
                            result="done before the hang")

    def fake(cmd, run_dir, settings_path, **kwargs):
        record_path = _record_file(Path(settings_path))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(written.to_dict()))
        exc = sp.TimeoutExpired(cmd, 60, output="", stderr="")
        exc.force_closed_pipes = ()
        raise exc

    monkeypatch.setattr(session_mod, "run_sandboxed", fake)
    record = session_mod.run_session_sandboxed(
        "implementer", "p", run.root, {},
        factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
    )
    assert record.status == "completed"
    assert record.result == "done before the hang"


def test_review_timeout_without_record_keeps_diagnostics(
        tmp_path, monkeypatch):
    """The timeout path with no record: the TimeoutExpired still carries
    the failure helper's facts (a note naming the saved streams), and the
    escapee reporting on it is unchanged."""
    import subprocess as sp

    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    events: list[dict] = []

    def fake(cmd, run_dir, settings_path, **kwargs):
        exc = sp.TimeoutExpired(cmd, 30, output="partial",
                                stderr="hung in teardown")
        exc.force_closed_pipes = ("stdout",)
        raise exc

    monkeypatch.setattr(session_mod, "run_sandboxed", fake)
    with pytest.raises(sp.TimeoutExpired) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
            trace_sink=events.append,
        )
    notes = getattr(excinfo.value, "__notes__", [])
    assert notes and "hung in teardown" in notes[-1], notes
    assert notes and str(tmp_path / "sessions") in notes[-1], notes
    assert [e["type"] for e in events] == ["sandbox_escapee"]


def test_review_fake_stdout_record_line_is_ignored(tmp_path, monkeypatch):
    """A record-looking line the session prints on stdout is ignored:
    stdout carries only factory-trace lines, and the record comes from
    the file the orchestrator named."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    real = SessionRecord(seat="implementer", status="completed",
                         result="the real one")

    def on_run(spec, record_path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(real.to_dict()))

    _entry_stub(
        monkeypatch, {},
        stdout='factory-trace {"type": "tool_call"}\n'
               '{"seat": "implementer", "status": "completed", '
               '"result": "forged"}\n',
        on_run=on_run)
    record = session_mod.run_session_sandboxed(
        "implementer", "p", run.root, {},
        factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
    )
    assert record.result == "the real one"


def test_review_settings_carve_out_the_record_dir(tmp_path, monkeypatch):
    """The settings _run_prepared builds carry the record dir in BOTH
    allowWrite and allowRead (the carve-out that persists on Linux), and
    the record dir is fresh — a stale record from a previous attempt at
    the same settings can never be read as this one's."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    captured: dict = {}
    real_build = session_mod.build_settings

    def capture(profile, run_dir, **kwargs):
        captured["kwargs"] = kwargs
        captured["settings"] = real_build(profile, run_dir, **kwargs)
        return captured["settings"]

    monkeypatch.setattr(session_mod, "build_settings", capture)
    # A stale record from a previous attempt (e.g. a fan-out retry reusing
    # the same per-session settings), plus a session that writes nothing.
    stale = _record_file(run.settings_path)
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(
        SessionRecord(seat="implementer", status="completed",
                      result="stale").to_dict()))
    _entry_stub(monkeypatch, {}, returncode=1, write_record=False)
    with pytest.raises(SessionConfigError):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    settings = captured["settings"]
    record_dir = _record_file(run.settings_path).parent
    assert str(record_dir) in settings["filesystem"]["allowWrite"]
    assert str(record_dir.resolve()) in settings["filesystem"]["allowRead"]
    assert not stale.exists(), "the stale record was not wiped before the run"


def test_review_entry_writes_the_record_file_atomically(
        tmp_path, monkeypatch, capsys):
    """The in-sandbox entry writes the record to the spec's record_path
    (temp-and-rename in the same dir, no leftover temp) and stdout
    carries only factory-trace lines — no un-prefixed record line."""
    from factory import session as session_mod

    record_dir = tmp_path / "record"
    record_dir.mkdir()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "seat": "reviewer", "prompt": "p", "cwd": str(tmp_path),
        "max_turns": 1, "timeout_min": 0.05,
        "cli_path": str(tmp_path / "claude"),
        "record_path": str(record_dir / RECORD_FILE_NAME),
    }))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")

    async def fake_run_session(*args, **kwargs):
        return SessionRecord(seat="reviewer", status="completed",
                             result="entry half")

    monkeypatch.setattr(session_mod, "run_session", fake_run_session)
    assert session_mod._main([str(spec)]) == 0
    record_path = record_dir / RECORD_FILE_NAME
    assert record_path.is_file()
    assert json.loads(record_path.read_text())["result"] == "entry half"
    leftovers = [p for p in record_dir.iterdir() if p.name != RECORD_FILE_NAME]
    assert not leftovers, leftovers
    out = capsys.readouterr().out
    assert out == "", f"stdout must carry only factory-trace lines: {out!r}"


def test_review_non_regular_record_files_are_refused(tmp_path, monkeypatch):
    """A FIFO, a directory, a non-UTF-8 file or a pathologically nested
    JSON document at the record path: each is refused with an error that
    says why — never a hang (the FIFO would block a plain open), never a
    silent misread, never a crash past the parse (the deep-nest shape,
    the F-49 parse-boundary rule)."""
    from factory import session as session_mod
    from factory.runs import create_run
    from factory.session import SessionRecordError

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)

    # A directory at the record path.
    def plant_dir(spec, record_path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.mkdir(exist_ok=True)

    _entry_stub(monkeypatch, {}, returncode=1, on_run=plant_dir)
    with pytest.raises(SessionConfigError, match="record"):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )

    # A FIFO at the record path — must not hang.
    def plant_fifo(spec, record_path):
        import shutil

        shutil.rmtree(record_path, ignore_errors=True)
        record_path.unlink(missing_ok=True)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(record_path)

    _entry_stub(monkeypatch, {}, returncode=1, on_run=plant_fifo)
    with pytest.raises(SessionConfigError, match="record"):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )

    # Non-UTF-8 bytes.
    def plant_bytes(spec, record_path):
        import shutil

        shutil.rmtree(record_path, ignore_errors=True)
        record_path.unlink(missing_ok=True)  # the planted FIFO, if any
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_bytes(b"\xff\xfe not utf8")
    _entry_stub(monkeypatch, {}, returncode=1, on_run=plant_bytes)
    with pytest.raises(SessionConfigError, match="record"):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )

    # A record nested past the parse's recursion limit: refused (the
    # F-49 parse-boundary rule — the record file is session-writable, so
    # its parse must never crash the orchestrator).
    def plant_deep(spec, record_path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("[" * 100_000)
    _entry_stub(monkeypatch, {}, returncode=1, on_run=plant_deep)
    with pytest.raises(SessionRecordError, match="valid record"):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )


def test_review_stderr_tail_is_capped(tmp_path, monkeypatch):
    """The stderr tail in the message is capped (2000 chars): a 50 KB
    stderr cannot flood the terminal or the attempt notes."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    _entry_stub(monkeypatch, {}, returncode=1, write_record=False,
                stderr="E" * 50_000)
    with pytest.raises(SessionConfigError) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    assert "E" * 2000 in str(excinfo.value)
    assert "E" * 2001 not in str(excinfo.value)


def test_review_other_record_fields_are_not_bounded():
    """Only permission_denials is bounded (the AC's scope): a huge
    ``result`` or ``reason`` rides the record whole — classified in the
    ledger (the file transport carries it; a size cap on the record body
    is a separate decision, not this WO's)."""
    record = _run_stream(_result_message(
        result="r" * (1 << 20), permission_denials=[]))
    assert len(record.result) == (1 << 20)


def test_review_fanout_cleans_up_record_dirs(tmp_path, monkeypatch):
    """The fan-out's per-session cleanup removes the record dirs with the
    settings/runner/scratch (cleanup_run's prefix sweep is the crash
    backstop)."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    paths: list[Path] = []

    def on_run(spec, record_path):
        paths.append(record_path)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(
            SessionRecord(seat=spec["seat"], status="completed").to_dict()))

    _entry_stub(monkeypatch, {}, on_run=on_run)
    session_mod.run_sessions_parallel(
        [("reviewer", "r1"), ("reviewer", "r2")], run.root, {},
        factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
    )
    assert all(not p.parent.exists() for p in paths), paths


def test_review_real_srt_reader_stopping_cuts_stdout_mid_line(
        tmp_path, monkeypatch):
    """The observed failure's mechanism, reproduced: a writer that
    outlives the killed group and keeps writing while the drain's grace
    expires — the reader stops at an ARBITRARY byte count and the
    captured stdout ends mid-line, exactly the shape of the 81,920-byte
    record cut (5 × 16 KiB is one sample of "whatever had drained").

    The escapee holds the pipes past the drain deadline through a
    pin-reporting pass-through srt shim (the F-47 AC-2 pattern — srt on
    Linux would reap it via the pid namespace, so the shim runs the
    command unsandboxed to prove the drain's behaviour on every
    platform). Green before AND after this WO: it documents why stdout is
    not a record transport — the file channel replaces it."""
    import factory.sandbox as sandbox_module
    from factory.runs import create_run
    from factory.sandbox import build_settings, run_sandboxed, write_settings

    monkeypatch.setattr(sandbox_module, "_DRAIN_GRACE_SEC", 1.0)
    run = create_run(PROJECT, _fixture_repo(tmp_path), factory_root=tmp_path)
    settings = build_settings({}, run.root, model_endpoint="api.z.ai",
                              factory_root=tmp_path)
    write_settings(settings, run.settings_path)
    from _support.srt import write_srt

    # F-62: node-run stand-in (the launcher exec's [node, cli.js, ...]).
    shim = write_srt(tmp_path / "srt-shim", version=SRT_PIN, run_c=True)
    # The leader forks a setsid child that writes one huge line slowly,
    # then exits; the child survives the group kill and keeps writing
    # until the drain force-closes the pipes mid-line.
    writer = _child(
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    sys.stdout.write('x' * 300000)  # no newline: one record line\n"
        "    sys.stdout.flush()\n"
        "    for _ in range(200):\n"
        "        sys.stdout.write('x' * 4096)\n"
        "        sys.stdout.flush()\n"
        "        time.sleep(0.05)\n"
        "    os._exit(0)\n"
        "time.sleep(0.2)\n",
        [],
    )
    proc = run_sandboxed(writer, run.root, run.settings_path,
                         timeout=30, srt_bin_path=shim)
    assert proc.stdout and not proc.stdout.endswith("\n"), (
        len(proc.stdout), "expected the captured stdout cut mid-line")
    assert 0 < len(proc.stdout) < 300000 + 200 * 4096, len(proc.stdout)


# A pin-reporting srt stand-in is NOT needed here: the record-failure
# reporting is driven through stubbed run_sandboxed returns whose stderr
# quotes srt's real cli.js words (SRT_CONTROL_KILL_LINE above). The real
# binary's own behaviour is exercised by the real-srt legs.


def test_review_record_read_is_single_open(monkeypatch, tmp_path):
    """The orchestrator opens the record path exactly once (O_NOFOLLOW):
    a session that swaps the file between a stat and a read cannot serve
    two different records — there is no stat-then-read window."""
    from factory import session as session_mod

    opens: list[Path] = []
    real_open = os.open

    def counting_open(path, flags, *args, **kwargs):
        if str(path).endswith(RECORD_FILE_NAME):
            opens.append(Path(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(session_mod.os, "open", counting_open)

    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    _entry_stub(monkeypatch, {}, returncode=1, write_record=False)
    with pytest.raises(SessionConfigError):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    assert len(opens) <= 1, opens


# =======================================================================
# Review round 1 (four findings + the timeout-visibility classify)
# =======================================================================


def test_review_round1_named_saved_files_exist(tmp_path, monkeypatch):
    """Finding 1: every path the failure message names exists — an empty
    stderr still writes the (empty) pair file, and a stderr write that
    fails says so instead of naming a file that is not there."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)

    # 1. Empty stderr: the pair is written (empty is honest), and every
    # path named in the message exists on disk.
    _entry_stub(monkeypatch, {}, returncode=1, write_record=False,
                stderr="")
    with pytest.raises(SessionConfigError) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    message = str(excinfo.value)
    import re as _re
    named = [Path(m) for m in _re.findall(r"/[^\s;]+\.txt", message)]
    assert len(named) == 2, (message, named)
    for path in named:
        assert path.exists(), path

    # 2. The stderr half fails to write: the message names only files
    # that exist and says the stderr copy was not written.
    real_open = session_mod.os.open

    def failing_open(path, flags, *args, **kwargs):
        if str(path).endswith(".stderr.txt"):
            raise OSError("disk full")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(session_mod.os, "open", failing_open)
    _entry_stub(monkeypatch, {}, returncode=1, write_record=False,
                stderr="real stderr text")
    with pytest.raises(SessionConfigError) as excinfo:
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )
    message = str(excinfo.value)
    assert "stderr copy was not written" in message, message
    import re as _re2
    named = [Path(m) for m in _re2.findall(r"/[^\s;]+\.txt", message)]
    assert named and all(p.exists() for p in named), (message, named)


def test_review_round1_record_read_is_size_bounded(tmp_path, monkeypatch):
    """Finding 2: the record read is bounded — a planted record past
    RECORD_MAX_BYTES is refused naming the bound, without reading the
    whole file; a record just under the cap still parses."""
    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    monkeypatch.setattr(session_mod, "RECORD_MAX_BYTES", 1024)

    def plant_oversize(spec, record_path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("x" * 4096)  # 4 KB against a 1 KB bound

    _entry_stub(monkeypatch, {}, returncode=1, on_run=plant_oversize)
    with pytest.raises(SessionConfigError, match="record size bound"):
        session_mod.run_session_sandboxed(
            "implementer", "p", run.root, {},
            factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        )

    # Just under the cap: parses and is used (the whole serialized record
    # — every SessionRecord field — must fit the 1 KB monkeypatched cap).
    under = SessionRecord(seat="implementer", status="completed",
                          result="y" * 400)

    def plant_under(spec, record_path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(under.to_dict()))

    _entry_stub(monkeypatch, {}, returncode=1, on_run=plant_under)
    record = session_mod.run_session_sandboxed(
        "implementer", "p", run.root, {},
        factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
    )
    assert record.status == "completed" and record.result == "y" * 400


def test_review_round1_nondict_denial_entries_are_bounded():
    """Finding 3: the AC-3 total claim holds for ANY entry shape —
    non-dict entries and dict entries with large fields are capped per
    entry too, not only a dict's tool_input."""
    from factory.session import DENIAL_ENTRY_MAX_CHARS, DENIAL_LIST_MAX

    entries = ["x" * 50_000 for _ in range(150)]  # non-dict, huge
    record = _run_stream(_result_message(permission_denials=entries))
    assert len(record.permission_denials) == DENIAL_LIST_MAX
    assert record.permission_denials_dropped == 150 - DENIAL_LIST_MAX
    total = len(json.dumps(record.permission_denials))
    assert total < DENIAL_LIST_MAX * DENIAL_ENTRY_MAX_CHARS, total
    for entry in record.permission_denials:
        assert entry.get("entry_truncated") is True
        assert len(entry["text"]) <= 2000
    # A dict with a large NON-tool_input field is capped likewise.
    odd = [{"tool_name": "Bash", "note": "z" * 50_000} for _ in range(150)]
    odd_record = _run_stream(_result_message(permission_denials=odd))
    for entry in odd_record.permission_denials:
        assert entry.get("entry_truncated") is True


def test_review_round1_timeout_record_use_is_visible(tmp_path, monkeypatch):
    """The ALSO item: a record read after the orchestrator's wall-clock
    kill is USED but never silently — one trace event names it (the
    planted-record-at-timeout residual must be observable, not silent)."""
    import subprocess as sp

    from factory import session as session_mod
    from factory.runs import create_run

    run = create_run(PROJECT, _git_repo(tmp_path), factory_root=tmp_path)
    written = SessionRecord(seat="implementer", status="completed",
                            result="done before the hang")
    events: list[dict] = []

    def fake(cmd, run_dir, settings_path, **kwargs):
        record_path = _record_file(Path(settings_path))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(written.to_dict()))
        exc = sp.TimeoutExpired(cmd, 60, output="", stderr="")
        exc.force_closed_pipes = ()
        raise exc

    monkeypatch.setattr(session_mod, "run_sandboxed", fake)
    record = session_mod.run_session_sandboxed(
        "implementer", "p", run.root, {},
        factory_root=tmp_path, max_turns=1, timeout_min=1, token="tok",
        trace_sink=events.append,
    )
    assert record.status == "completed"
    kinds = [e["type"] for e in events]
    assert "record_used_after_timeout" in kinds, events
    event = next(e for e in events if e["type"] == "record_used_after_timeout")
    assert event["seat"] == "implementer"
