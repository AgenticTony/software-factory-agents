"""F-47 — Sandbox read boundary and toolkit staging.

The contract tests, exact names ``test_ac_1``..``test_ac_4`` (the queue's
acceptance refs), plus edge tests. Red-first: written and run against
untouched ``470a5ff`` before any implementation; each AC's red line is
quoted in ``.factory/BOOTSTRAP.md``.

The goal behind the four ACs: a sandboxed session sees only its own run,
whatever filesystem layout the owner chooses; an escaped process leaves
an observable trace; admission compares against an independent record;
staging is explicit.

Real-srt policy (F-05): the sandbox tests attempt the forbidden action
inside a real ``srt`` and assert on the actual failure, never on the
settings JSON alone. They are never skipped.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_f05 import _child, _fixture_repo
from test_f22 import (
    _content_hash,
    _hash_tree,
    _make_mcp,
    _make_skill,
    _write_index,
)

import factory.sandbox as sandbox_module
from factory.runs import create_run
from factory.sandbox import (
    SRT_PIN,
    SandboxError,
    build_settings,
    run_sandboxed,
    write_settings,
)
from factory.status import render_status
from factory.toolkit import ToolkitError, load_index, stage_toolkit
from factory.trace import TraceWriter, trace_path

MODEL_ENDPOINT = "api.z.ai"
REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT = "fixture"

# A child that tries each path (read, or write when the label says so)
# and prints ``label:ok`` / ``label:denied <Exc>`` — the actual outcome
# inside the sandbox, never inferred from settings.
_ATTEMPT = textwrap.dedent(
    """
    import os, sys
    def attempt(label, fn):
        try:
            fn()
            print(f"{label}:ok", flush=True)
        except Exception as exc:
            print(f"{label}:denied {type(exc).__name__}", flush=True)
    def read(path):
        if os.path.isdir(path):
            os.listdir(path)
        else:
            open(path, "rb").read()
    def write(path):
        with open(path, "w") as fh:
            fh.write("x")
    pairs = sys.argv[1:]
    for label, path in zip(pairs[::2], pairs[1::2]):
        if label.startswith("write_"):
            attempt(label, lambda p=path: write(p))
        else:
            attempt(label, lambda p=path: read(p))
    """
)


def _outcomes(proc: subprocess.CompletedProcess) -> dict[str, str]:
    return dict(
        line.split(":", 1) for line in proc.stdout.splitlines() if ":" in line
    )


def _outside_home(path: Path) -> bool:
    homes = {Path.home(), Path.home().resolve()}
    real = path.resolve()
    return not any(real == h or h in real.parents for h in homes)


def _populated_root(base: Path) -> tuple[Path, dict[str, Path]]:
    """A factory root OUTSIDE $HOME with every sibling store a session
    must not read: the trace, progress logs, saved session stdout, the
    holdouts, a second run of the same project, another project's run,
    the settings dir, and a loose file at the root itself."""
    root = base / "factory-root"
    markers = {
        "trace": root / "trace" / PROJECT / "20260101T000000Z-aaaaaa.jsonl",
        "progress": root / "progress" / PROJECT / "F-01.jsonl",
        "sessions": root / "sessions" / PROJECT / "stdout.txt",
        "holdouts": root / "holdouts" / "scenario.md",
        "other_run": root / "runs" / PROJECT / "20260101T000000Z-bbbbbb" / "m.txt",
        "other_project": root / "runs" / "other" / "20260101T000000Z-cccccc" / "m.txt",
        "settings_dir": root / "sandbox" / "other-run.json",
        "root_file": root / "owner-notes.txt",
    }
    for label, path in markers.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"secret {label}\n")
    return root, markers


def _shapes(run_dir: Path, root: Path) -> dict[str, dict]:
    """The three settings shapes every caller builds: the gate runner
    (local binding requested, the profile opts in), the single session
    (a staged runner dir under the factory root re-allowed), and the
    review fan-out (read-only, its own scratch under the factory root)."""
    profile = {"docs_allowlist": [], "network_allowlist": [],
               "sandbox": {"allow_local_binding": True}}
    runner = root / "sandbox" / f"{PROJECT}-{run_dir.name}.json-runner"
    runner.mkdir(parents=True, exist_ok=True)
    (runner / "spec.json").write_text("{}")
    scratch_base = root / "sandbox" / f"{PROJECT}-{run_dir.name}.json-r1.json-scratch"
    home, tmp = scratch_base / "home", scratch_base / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "gate": {
            "settings": build_settings(profile, run_dir, model_endpoint=MODEL_ENDPOINT,
                                       factory_root=root, local_binding=True),
            "scratch": None,
            "write": run_dir / ".tmp" / "probe.txt",
            "own_read": runner,  # not re-allowed for the gate: see below
        },
        "session": {
            "settings": build_settings(profile, run_dir, model_endpoint=MODEL_ENDPOINT,
                                       factory_root=root, allow_read=[runner]),
            "scratch": None,
            "write": run_dir / ".tmp" / "probe.txt",
            "own_read": runner / "spec.json",
        },
        "fanout": {
            "settings": build_settings(profile, run_dir, model_endpoint=MODEL_ENDPOINT,
                                       factory_root=root, allow_read=[runner],
                                       read_only=True, scratch_write=(home, tmp)),
            "scratch": (home, tmp),
            "write": tmp / "probe.txt",
            "own_read": runner / "spec.json",
        },
    }


# --- AC-1 (M-9): the factory root itself is denied ---------------------------


def test_ac_1(tmp_path: Path):
    """With ``--factory-root`` OUTSIDE $HOME, a session reads its own run
    (and writes its scratch) but nothing else under the root: not
    ``trace/``, ``progress/``, ``sessions/``, ``holdouts/``, another run,
    another project, the settings dir or a loose root file — for the
    gate-runner, single-session and fan-out settings shapes alike."""
    root, markers = _populated_root(tmp_path)
    assert _outside_home(root), f"fixture root {root} must sit outside $HOME"
    run = create_run(PROJECT, _fixture_repo(tmp_path), factory_root=root)
    resolved_root = str(root.resolve())

    for shape, spec in _shapes(run.root, root).items():
        settings_path = tmp_path / f"settings-{shape}.json"
        write_settings(spec["settings"], settings_path)

        args: list[str] = []
        for label, path in markers.items():
            args += [label, str(path)]
        args += ["own_run", str(run.root / "README.md")]
        if shape != "gate":
            args += ["own_runner", str(spec["own_read"])]
        args += ["write_scratch", str(spec["write"])]
        proc = run_sandboxed(
            _child(_ATTEMPT, args), run.root, settings_path,
            timeout=60, scratch=spec["scratch"],
        )
        outcomes = _outcomes(proc)
        for label in markers:
            assert outcomes.get(label, "").startswith("denied"), (
                f"[{shape}] {label} ({markers[label]}) was readable inside the "
                f"sandbox with the factory root outside $HOME\n"
                f"{proc.stdout}{proc.stderr}"
            )
        assert outcomes.get("own_run") == "ok", (shape, proc.stdout + proc.stderr)
        if shape != "gate":
            assert outcomes.get("own_runner") == "ok", (shape, proc.stdout + proc.stderr)
        assert outcomes.get("write_scratch") == "ok", (shape, proc.stdout + proc.stderr)
        # Settings-level invariant (every shape): the root itself is denied,
        # so a new caller of build_settings cannot bypass it.
        deny = [str(Path(p)) for p in spec["settings"]["filesystem"]["denyRead"]]
        assert resolved_root in deny, (shape, deny)


# --- AC-2 (I-9): a force-closed escapee is observable ------------------------

# The leader forks a child that escapes the process group (setsid), keeps
# the inherited stdout/stderr open and sleeps; the leader writes the
# child's pid where the test can find it (to kill it afterwards) and
# exits (or sleeps past the timeout, for the timeout path).
_ESCAPEE = textwrap.dedent(
    """
    import os, sys, time
    pidfile, leader_sleep = sys.argv[1], float(sys.argv[2])
    pid = os.fork()
    if pid == 0:
        os.setsid()
        time.sleep(40)
        os._exit(0)
    with open(pidfile, "w") as fh:
        fh.write(str(pid))
    print("leader-out", flush=True)
    time.sleep(leader_sleep)
    """
)


def _kill_pidfile(pidfile: Path) -> None:
    with suppress(OSError, ValueError):
        os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)


def _passthrough_srt(tmp_path: Path) -> Path:
    """A pin-reporting srt stand-in that runs ``-c <command>`` unsandboxed.

    srt on Linux puts the command in a pid namespace bwrap tears down when
    srt dies, so a setsid child cannot outlive the kill there; this shim
    exercises the drain's force-close on EVERY platform, through the real
    :func:`run_sandboxed` (pin check included)."""
    shim = tmp_path / "srt-shim"
    shim.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "--version" ]; then echo "{SRT_PIN}"; exit 0; fi
        exec /bin/sh -c "$4"
        """))
    shim.chmod(0o755)
    return shim


def test_ac_2(tmp_path: Path, monkeypatch):
    """A drain that force-closes a setsid escapee: the CompletedProcess
    (and the TimeoutExpired) carry ``force_closed_pipes``; the gate
    runner writes a ``sandbox_escapee`` event to the run's trace; and
    ``factory status`` counts it. A normal command sets nothing and
    writes nothing."""
    from factory.orchestrator import sandboxed_runner

    grace = 2.0
    monkeypatch.setattr(sandbox_module, "_DRAIN_GRACE_SEC", grace)
    root = tmp_path / "factory-root"
    run = create_run(PROJECT, _fixture_repo(tmp_path), factory_root=root)
    settings = build_settings({}, run.root, model_endpoint=MODEL_ENDPOINT)
    write_settings(settings, run.settings_path)
    shim = _passthrough_srt(tmp_path)

    # 1. Real srt, normal exit with an escapee. macOS: the escapee holds
    # the pipes past the grace and is force-closed. Linux: bwrap's pid
    # namespace dies with srt, so there is nothing to force-close — the
    # flag must then be unset (no false alarm); the shim legs below
    # prove the alarm on Linux.
    pidfile = run.root / "escapee.pid"
    started = time.monotonic()
    try:
        proc = run_sandboxed(
            _child(_ESCAPEE, [str(pidfile), "0"]), run.root, run.settings_path,
            timeout=60,
        )
    finally:
        _kill_pidfile(pidfile)
    elapsed = time.monotonic() - started
    assert "leader-out" in proc.stdout, proc.stdout + proc.stderr
    flag = getattr(proc, "force_closed_pipes", None)
    if sys.platform == "darwin":
        assert flag, f"escapee held the pipes but no flag: {flag!r}"
        assert elapsed < grace + 15, elapsed
    else:
        # Either outcome is legitimate here; what must hold is that the
        # flag tells the truth about the drain: set iff it ran into the
        # grace deadline with a pipe still open.
        assert flag is not None, "run_sandboxed must always set the attribute"
        if flag:
            assert elapsed >= grace, (flag, elapsed)
        else:
            assert elapsed < grace + 5, elapsed

    # 2. The same through the shim (every platform): the flag names the
    # streams, and the drain returned within the grace window.
    pidfile = run.root / "escapee2.pid"
    started = time.monotonic()
    try:
        proc = run_sandboxed(
            _child(_ESCAPEE, [str(pidfile), "0"]), run.root, run.settings_path,
            timeout=60, srt_bin_path=shim,
        )
    finally:
        _kill_pidfile(pidfile)
    elapsed = time.monotonic() - started
    assert set(proc.force_closed_pipes) == {"stdout", "stderr"}, proc.force_closed_pipes
    assert grace <= elapsed < grace + 10, elapsed

    # 3. Timeout path: the TimeoutExpired carries the flag too.
    pidfile = run.root / "escapee3.pid"
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            run_sandboxed(
                _child(_ESCAPEE, [str(pidfile), "30"]), run.root, run.settings_path,
                timeout=3, srt_bin_path=shim,
            )
    finally:
        _kill_pidfile(pidfile)
    assert set(caught.value.force_closed_pipes) == {"stdout", "stderr"}

    # 4. A normal command (no escapee) sets nothing.
    proc = run_sandboxed(_child("print('plain')", []), run.root,
                         run.settings_path, timeout=60, srt_bin_path=shim)
    assert proc.force_closed_pipes == ()

    # 5. Through a real caller (the gate runner) into the real trace, and
    # into `factory status`.
    trace = TraceWriter(root, PROJECT, run.run_id, "F-01")
    runner = sandboxed_runner(run.root, run.settings_path, srt_bin_path=shim,
                              on_escapee=trace.append)
    clean = runner("lint", "echo fine", run.root, 60)
    assert clean.exit_code == 0
    assert not trace_path(root, PROJECT, run.run_id).exists() or not any(
        json.loads(line).get("type") == "sandbox_escapee"
        for line in trace_path(root, PROJECT, run.run_id).read_text().splitlines()
    ), "a normal gate command wrote an escapee event"
    pidfile = run.root / "escapee4.pid"
    script = tmp_path / "escapee.py"
    script.write_text(_ESCAPEE)
    try:
        result = runner("test", f"{sys.executable} {script} {pidfile} 0",
                        run.root, 60)
    finally:
        _kill_pidfile(pidfile)
    assert result.exit_code == 0
    events = [json.loads(line) for line in
              trace_path(root, PROJECT, run.run_id).read_text().splitlines()]
    escapees = [e for e in events if e.get("type") == "sandbox_escapee"]
    assert len(escapees) == 1, events
    assert escapees[0]["run_id"] == run.run_id
    assert escapees[0]["label"] == "gate:test"
    assert set(escapees[0]["streams"]) == {"stdout", "stderr"}

    page = render_status({"tasks": []}, project=PROJECT, factory_root=root,
                         now=datetime.now(timezone.utc))
    assert "## Sandbox escapees this week" in page, page
    assert "1 force-closed" in page, page
    assert run.run_id in page, page


# --- AC-3 (I-2): index-load domains vs the VETTED allowlist ------------------


def _report(entry: dict, allowlists: list[str] | None, *, extra: str = "") -> str:
    """A vet report in :func:`factory.toolkit.render_report`'s shape."""
    lines = [
        f"# Vet report — {entry['name']}",
        "",
        f"- candidate: `{entry['dir']}`",
        f"- content hash: `{entry['content_hash']}`",
        "- licence: MIT",
    ]
    if allowlists is not None:
        lines.append(f"- allowlists: {', '.join(allowlists) or '(none)'}")
    lines += ["- findings: 0", "", "No findings.", extra]
    return "\n".join(lines) + "\n"


def _mcp_toolkit(tmp: Path, domains: list[str], vetted: list[str] | None,
                 *, extra: str = "") -> Path:
    toolkit = tmp / "toolkit"
    entry = _make_mcp(toolkit, name="docs", domains=domains)
    report = toolkit / entry["report"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(_report(entry, vetted, extra=extra))
    _write_index(toolkit, [entry])
    return toolkit


def test_ac_3(tmp_path: Path):
    """An MCP descriptor naming a domain its vet report's allowlists never
    covered refuses at load; the same entry with the domain vetted loads;
    the real repo index still loads."""
    unvetted = _mcp_toolkit(tmp_path / "a", ["collector.evil.example"],
                            ["docs.example.com"])
    with pytest.raises(ToolkitError, match="collector.evil.example"):
        load_index(unvetted)

    vetted = _mcp_toolkit(tmp_path / "b", ["docs.example.com"],
                          ["docs.python.org", "docs.example.com"])
    assert [e["name"] for e in load_index(vetted)["items"]] == ["docs"]

    assert load_index(REPO_ROOT / "toolkit")["items"]


# --- AC-4 (I-19): staging copies explicitly ---------------------------------


def test_ac_4(tmp_path: Path):
    """An MCP item whose vendored dir is NOT under ``mcp/`` stages with its
    descriptor at ``mcp/<name>.json``; the staged tree is EXACTLY the
    plugin manifest, the pinned files and one descriptor per MCP item;
    an extra unpinned source file still refuses."""
    toolkit = tmp_path / "toolkit"
    skill = _make_skill(toolkit)
    # The schema admits any of skills|plugins|mcp as an item's dir.
    item_dir = toolkit / "plugins" / "lookup"
    item_dir.mkdir(parents=True)
    (item_dir / "LICENSE").write_text((toolkit / "skills" / "demo-skill" / "LICENSE").read_text())
    (item_dir / "server.js").write_text("// vendored entrypoint\n")
    files = _hash_tree(item_dir)
    mcp = {
        "kind": "mcp_server", "name": "lookup",
        "upstream": "https://github.com/example/lookup",
        "pin": {"version": "1.0.0", "integrity": "c" * 64},
        "licence": "MIT", "content_hash": _content_hash(files), "files": files,
        "report": "reports/lookup.md", "dir": "plugins/lookup",
        "mcp": {"command": "node", "args": ["{staged}/plugins/lookup/server.js"],
                "tools": [{"name": "find", "mode": "read"}],
                "domains": ["docs.example.com"]},
    }
    resolved = {"root": str(toolkit), "items": [skill, mcp],
                "skills": [skill["name"]], "plugins": [], "mcp_servers": ["lookup"]}

    staged = stage_toolkit(resolved, tmp_path / "staged")
    got = {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}
    expected = {".claude-plugin/plugin.json", "mcp/lookup.json"}
    expected |= {f"{skill['dir']}/{rel}" for rel in skill["files"]}
    expected |= {f"{mcp['dir']}/{rel}" for rel in mcp["files"]}
    assert got == expected
    assert json.loads((staged / "mcp" / "lookup.json").read_text()) == mcp["mcp"]

    # An unpinned extra file in a source dir refuses — and leaves nothing.
    (item_dir / "extra.js").write_text("// not in the index\n")
    with pytest.raises(ToolkitError, match="extra.js"):
        stage_toolkit(resolved, tmp_path / "staged2")
    assert not (tmp_path / "staged2").exists()


# --- edges and wiring --------------------------------------------------------


def test_build_settings_refuses_a_root_that_would_deny_the_system(tmp_path: Path):
    """A factory root that is ``/`` or at/above a system read root would
    deny libc, the loader and /etc: refused, naming why — never a
    silently weakened deny."""
    run_dir = tmp_path / "runs" / PROJECT / "20260101T000000Z-dddddd"
    run_dir.mkdir(parents=True)
    bad = [Path("/"), Path("/usr"), Path("/etc").resolve()]
    if Path("/etc").resolve() != Path("/etc"):
        bad.append(Path("/etc").resolve().parent)  # macOS: /private
    for root in bad:
        with pytest.raises(SandboxError, match="factory root"):
            build_settings({}, run_dir, model_endpoint=MODEL_ENDPOINT,
                           factory_root=root)


@pytest.mark.parametrize("which", ["repo_root", "home_parent"])
def test_overlapping_roots_keep_the_toolchain_and_deny_the_rest(
    tmp_path: Path, monkeypatch, which: str
):
    """The overlap cases, proven with real srt: a factory root that
    CONTAINS this checkout (srt's package, the factory source, a repo-local
    venv) and one that is an ancestor of $HOME. The sandboxed interpreter
    still runs (toolchain and srt carved out), the run is readable, a
    system file is readable — and the rest of the root is not.

    The ancestor-of-$HOME case fakes $HOME (``tmp/home/me``) so the probe
    — a SIBLING home, ``tmp/home/other/secret.txt`` — is inside the root
    and outside the (denied) home wherever the checkout lives: only the
    root deny can block it (review round 1, finding 2)."""
    if which == "repo_root":
        root = REPO_ROOT
        probe = REPO_ROOT / "CLAUDE.md"
    else:
        root = tmp_path / "home"
        fake_home = root / "me"
        fake_home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        assert Path("~").expanduser() == fake_home
        probe = root / "other" / "secret.txt"
        probe.parent.mkdir(parents=True)
        probe.write_text("another home's secret\n")
    # A canonical run layout AWAY from the root: the derived runs/ deny
    # (run_dir.parent.parent) must not cover the probe, or the leg would
    # pass without the root deny (caught by mutation in review round 1).
    run_dir = tmp_path / "elsewhere" / "runs" / PROJECT / "20260101T000000Z-ffffff"
    run_dir.mkdir(parents=True)
    (run_dir / "own.txt").write_text("mine\n")
    settings = build_settings({}, run_dir, model_endpoint=MODEL_ENDPOINT,
                              factory_root=root)
    assert str(root.resolve()) in settings["filesystem"]["denyRead"]
    assert probe.resolve().is_relative_to(root.resolve())
    for path in settings["filesystem"]["denyRead"] if which == "home_parent" else ():
        if Path(path) != root.resolve():
            assert not probe.resolve().is_relative_to(Path(path)), (
                f"{probe} is also covered by the deny {path}")
    settings_path = tmp_path / "settings.json"
    write_settings(settings, settings_path)
    proc = run_sandboxed(
        _child(_ATTEMPT, ["own", str(run_dir / "own.txt"),
                          "system", "/etc/hosts",
                          "inside_root", str(probe)]),
        run_dir, settings_path, timeout=60,
    )
    outcomes = _outcomes(proc)
    assert outcomes.get("own") == "ok", proc.stdout + proc.stderr
    assert outcomes.get("system") == "ok", proc.stdout + proc.stderr
    assert outcomes.get("inside_root", "").startswith("denied"), (
        proc.stdout + proc.stderr)


def test_symlinked_factory_root_is_denied_by_its_real_path(tmp_path: Path):
    """A root reached through a symlink is denied at its resolved path,
    like the run dir (macOS /tmp is /private/tmp)."""
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real)
    run_dir = real / "runs" / PROJECT / "20260101T000000Z-eeeeee"
    run_dir.mkdir(parents=True)
    settings = build_settings({}, run_dir, model_endpoint=MODEL_ENDPOINT,
                              factory_root=link)
    assert str(real.resolve()) in settings["filesystem"]["denyRead"]
    derived = build_settings({}, link / "runs" / PROJECT / run_dir.name,
                             model_endpoint=MODEL_ENDPOINT)
    assert str(real.resolve()) in derived["filesystem"]["denyRead"]


def test_mcp_report_allowlists_fail_closed(tmp_path: Path):
    """No allowlists line, a duplicated one (a spoof in the body) or a
    descriptor domain differing only by case: refused at load."""
    missing = _mcp_toolkit(tmp_path / "m", ["docs.example.com"], None)
    with pytest.raises(ToolkitError, match="allowlists"):
        load_index(missing)
    spoof = _mcp_toolkit(tmp_path / "s", ["collector.evil.example"],
                         ["docs.example.com"],
                         extra="- allowlists: collector.evil.example")
    with pytest.raises(ToolkitError, match="allowlists"):
        load_index(spoof)
    case = _mcp_toolkit(tmp_path / "c", ["Docs.Example.com"], ["docs.example.com"])
    with pytest.raises(ToolkitError, match="Docs.Example.com"):
        load_index(case)
    # A subdomain of a vetted domain is inside it (the resolve-time rule).
    sub = _mcp_toolkit(tmp_path / "d", ["api.docs.example.com"], ["docs.example.com"])
    assert load_index(sub)["items"]


def test_session_escapee_reaches_the_trace_sink(tmp_path: Path, monkeypatch):
    """The session path (``_run_prepared``, shared by the single session
    and the fan-out) emits one ``sandbox_escapee`` event through its
    trace sink when run_sandboxed reports force-closed pipes — on the
    normal return AND on the timeout raise. F-56 re-seat: the record the
    fake entry leaves travels by the file (record_path_for), so the
    timeout leg keeps its record and still raises only the timeout."""
    import factory.session as session_module

    record = {"seat": "implementer", "status": "completed", "reason": None,
              "permission_denials_dropped": 0}

    def fake(cmd, run_dir, settings_path, **kwargs):
        record_path = session_module.record_path_for(Path(settings_path))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record))
        proc = subprocess.CompletedProcess(cmd, 0, "", "")
        proc.force_closed_pipes = ("stdout",)
        return proc

    monkeypatch.setattr(session_module, "run_sandboxed", fake)
    root = tmp_path / "factory-root"
    run = create_run(PROJECT, _fixture_repo(tmp_path), factory_root=root)
    events: list[dict] = []
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    session_module._run_prepared(
        "implementer", "p", run.root, {}, run.settings_path,
        factory_root=root, max_turns=1, timeout_min=1, token="tok",
        cli_path=cli, trace_sink=events.append, run_id=run.run_id,
    )
    assert [e["type"] for e in events] == ["sandbox_escapee"], events
    assert events[0]["label"] == "session:implementer"
    assert events[0]["streams"] == ["stdout"]

    def fake_timeout(cmd, run_dir, settings_path, **kwargs):
        # No record written: the runner hung before delivering one, so
        # the timeout itself stays the failure (a record WOULD be used —
        # F-56 pins that in tests/test_f56.py's timeout test).
        exc = subprocess.TimeoutExpired(cmd, 1, output="", stderr="")
        exc.force_closed_pipes = ("stderr",)
        raise exc

    monkeypatch.setattr(session_module, "run_sandboxed", fake_timeout)
    events.clear()
    with pytest.raises(subprocess.TimeoutExpired):
        session_module._run_prepared(
            "implementer", "p", run.root, {}, run.settings_path,
            factory_root=root, max_turns=1, timeout_min=1, token="tok",
            cli_path=cli, trace_sink=events.append, run_id=run.run_id,
        )
    assert [(e["type"], e["timed_out"]) for e in events] == [
        ("sandbox_escapee", True)], events


def test_status_counts_escapees_per_project_in_the_week(tmp_path: Path):
    """The count is per project, over the week window, read from the run
    traces; old events, other projects' events and a forged tool_call
    carrying the type in a nested field do not count."""
    root = tmp_path / "factory-root"
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    tdir = root / "trace" / PROJECT
    tdir.mkdir(parents=True)
    (tdir / "20261105T000000Z-aaaaaa.jsonl").write_text("\n".join([
        json.dumps({"type": "sandbox_escapee", "ts": "2026-11-05T10:00:00Z",
                    "run_id": "20261105T000000Z-aaaaaa", "label": "gate:test"}),
        json.dumps({"type": "tool_call", "ts": "2026-11-05T10:00:01Z",
                    "args": {"type": "sandbox_escapee"}}),
        json.dumps({"type": "sandbox_escapee", "ts": "2026-10-01T10:00:00Z",
                    "run_id": "20261105T000000Z-aaaaaa", "label": "old"}),
        "{not json",
    ]) + "\n")
    other = root / "trace" / "other"
    other.mkdir(parents=True)
    (other / "20261105T000000Z-bbbbbb.jsonl").write_text(
        json.dumps({"type": "sandbox_escapee", "ts": "2026-11-05T10:00:00Z",
                    "run_id": "20261105T000000Z-bbbbbb"}) + "\n")
    page = render_status({"tasks": []}, project=PROJECT, factory_root=root, now=now)
    assert "1 force-closed" in page, page
    assert "20261105T000000Z-aaaaaa" in page
    assert "20261105T000000Z-bbbbbb" not in page
    empty = render_status({"tasks": []}, project="nobody", factory_root=root, now=now)
    assert "0 force-closed" in empty, empty


def test_stage_toolkit_refuses_a_shared_item_dir(tmp_path: Path):
    """Two items naming one vendored dir would merge into one staged dir
    under a file-by-file copy: refused before anything is written."""
    toolkit = tmp_path / "toolkit"
    skill = _make_skill(toolkit)
    twin = dict(skill, name="twin")
    resolved = {"root": str(toolkit), "items": [skill, twin],
                "skills": ["demo-skill", "twin"], "plugins": [], "mcp_servers": []}
    with pytest.raises(ToolkitError, match="twin"):
        stage_toolkit(resolved, tmp_path / "staged")
    assert not (tmp_path / "staged").exists()


def test_staging_preserves_executable_bits(tmp_path: Path):
    """A vendored executable (the spawn allowlist admits a
    ``{staged}/...`` binary) stays executable in the staged copy."""
    toolkit = tmp_path / "toolkit"
    skill = _make_skill(toolkit)
    tool = toolkit / "skills" / "demo-skill" / "run.sh"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    skill["files"] = _hash_tree(toolkit / "skills" / "demo-skill")
    skill["content_hash"] = _content_hash(skill["files"])
    resolved = {"root": str(toolkit), "items": [skill],
                "skills": ["demo-skill"], "plugins": [], "mcp_servers": []}
    staged = stage_toolkit(resolved, tmp_path / "staged")
    assert os.access(staged / "skills" / "demo-skill" / "run.sh", os.X_OK)


def test_run_once_wires_the_gate_runner_to_the_run_trace(tmp_path: Path,
                                                          monkeypatch):
    """The orchestrator hands its gate runner the run's trace writer: an
    escapee reported during the L0/L1 gates lands in THAT run's trace
    (stamped with its run id and task) and in `factory status`."""
    from test_f30 import Env, Seats

    import factory.orchestrator as orch
    from factory.sandbox import escapee_event

    real = orch.sandboxed_runner
    sinks: list = []

    def wrapped(run_dir, settings_path, **kwargs):
        sinks.append(kwargs.get("on_escapee"))
        runner = real(run_dir, settings_path, **kwargs)

        def run(name, command, repo, timeout_sec):
            if kwargs.get("on_escapee") is not None and len(sinks) == 1:
                kwargs["on_escapee"](escapee_event(f"gate:{name}", ("stdout",)))
                sinks.append("fired")
            return runner(name, command, repo, timeout_sec)

        return run

    monkeypatch.setattr(orch, "sandboxed_runner", wrapped)
    env = Env(tmp_path)
    result = env.run(Seats())
    assert result.outcome == "pr_opened", result
    assert sinks and callable(sinks[0]), sinks
    run_id = env.task()["attempts"][0]["run_id"]
    escapees = [e for e in env.trace_events(run_id)
                if e["type"] == "sandbox_escapee"]
    assert len(escapees) == 1, escapees
    assert escapees[0]["task_id"] == "T-1" and escapees[0]["run_id"] == run_id
    page = render_status(json.loads(env.queue_path.read_text()),
                         project="fixture", factory_root=env.root,
                         now=datetime.now(timezone.utc))
    assert "1 force-closed" in page and run_id in page, page


def test_staged_tree_equality_catches_a_stray_file(tmp_path: Path, monkeypatch):
    """The whole-tree check is an equality: a file that appears in the
    staged dir during the copy (another writer, a copy routine that drags
    a sibling along) refuses, even though every pinned file re-hashes."""
    import factory.toolkit as toolkit_module

    toolkit = tmp_path / "toolkit"
    skill = _make_skill(toolkit)
    resolved = {"root": str(toolkit), "items": [skill],
                "skills": ["demo-skill"], "plugins": [], "mcp_servers": []}
    real_copy2 = toolkit_module.shutil.copy2

    def copy2(src, dst, **kwargs):
        Path(dst).parent.joinpath("..", "stray.txt").resolve().write_text("x")
        return real_copy2(src, dst, **kwargs)

    monkeypatch.setattr(toolkit_module.shutil, "copy2", copy2)
    with pytest.raises(ToolkitError, match="stray.txt"):
        stage_toolkit(resolved, tmp_path / "staged")
    assert not (tmp_path / "staged").exists()
