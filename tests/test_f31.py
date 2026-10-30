"""F-31 acceptance tests — BUILD-ORDER.md, work order F-31.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f31.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the HTTP server (``ThreadingHTTPServer`` on loopback, driven
  through ``http.client`` like a browser would), the F-02 loaders behind
  the queue and metrics views, the F-11 render, the F-30 trace writer and
  the F-30 follower behind the event stream.
* **Stubbed:** nothing. The dashboard is read-only, so the fixture
  factory root needs no sandbox, no model session and no git history.

Every test drives the server over real HTTP on ``127.0.0.1`` — the seam
the contract names. Request paths stay raw (``..``, ``%2e%2e`` and
absolute-form targets are sent as-is), because the point of AC-6 is
exactly what the server does with hostile paths.
"""

import contextlib
import hashlib
import http.client
import http.server
import importlib.resources
import io
import json
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import yaml

from factory.cli import build_parser
from factory.cli import main as cli_main
from factory.contract import acceptance_hash
from factory.dashboard import GET_ROUTES, make_server
from factory.trace import TraceWriter

PROJECT = "fixture"
RUN_ID = "20260101T000000Z-ab12cd"
RUN_ID_2 = "20260101T000001Z-cd34ef"
REF = "tests/test_feature.py::test_ac_1"

# The one dependency list AC-1's "no new dependency" pins: the dashboard
# adds nothing to what the factory already shipped.
KNOWN_DEPENDENCIES = {
    "jsonschema>=4",
    "PyYAML>=6",
    "claude-agent-sdk==0.2.168",
}


# ------------------------------------------------------------- fixtures --


def _attempt(
    run_id: str,
    outcome: str = "pass",
    *,
    rounds: int = 0,
    in_tok: int = 0,
    out_tok: int = 0,
    escalation: str | None = None,
    note: str | None = None,
) -> dict:
    """One attempt record, the shape the schema's ``attempt`` requires."""
    record = {
        "run_id": run_id,
        "slice_id": "",
        "started": "2026-10-16T09:00:00Z",
        "ended": "2026-10-16T10:00:00Z",
        "outcome": outcome,
        "rounds": rounds,
        "turns": 10,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "prompt_versions": {"implementer": "deadbeef"},
        "skills": [],
    }
    if escalation:
        record["escalation"] = escalation
    if note:
        record["note"] = note
    return record


def _task(tid: str, status: str, *, attempts: list[dict] | None = None, **extra) -> dict:
    """One task with every field the queue schema requires."""
    task = {
        "id": tid,
        "title": f"Task {tid}",
        "source": f"BUILD-ORDER.md#{tid.lower()}",
        "kind": "build",
        "priority": "P1",
        "status": status,
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [
            {
                "id": "AC-1",
                "text": "it works",
                "check": {"type": "test", "ref": REF},
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
        "acceptance_hash": acceptance_hash([
            {
                "id": "AC-1",
                "text": "it works",
                "check": {"type": "test", "ref": REF},
            }
        ]),
        "approved_by": "owner",
        "approved_at": "2026-10-01T09:00:00Z",
        "attempts": attempts or [],
    }
    task.update(extra)
    return task


def _queue_doc() -> dict:
    """Tasks in every state AC-3 renders, with the escalation reasons and
    owner steps the page must show."""
    return {
        "version": 1,
        "project": PROJECT,
        "updated_at": "2026-10-18T15:00:00Z",
        "tasks": [
            _task("T-1", "ready"),
            _task(
                "T-B", "needs_human",
                attempts=[
                    _attempt("r3", "fail", rounds=3),
                    _attempt("r4", "escalated", rounds=2,
                             escalation="attempts_exhausted",
                             note="3 attempts failed L0"),
                ],
            ),
            _task(
                "T-D", "needs_human",
                attempts=[
                    _attempt("r6", "escalated", rounds=1,
                             escalation="credential_added",
                             note="fixture string looked like a credential"),
                ],
            ),
            _task(
                "T-E", "awaiting_owner",
                attempts=[_attempt("r7", rounds=2)],
                owner_steps=[
                    {"text": "step one", "done": True,
                     "done_at": "2026-10-04T09:00:00Z", "done_by": "owner",
                     "evidence": "checked"},
                    {"text": "step two", "done": False},
                ],
            ),
            _task("T-A", "done", attempts=[_attempt("r1", rounds=1)]),
            _task("T-G", "in_progress", attempts=[_attempt("r8", rounds=1)]),
        ],
    }


def _metrics_doc() -> dict:
    """The F-11 fixture queue (the same shape tests/test_f11.py drives):
    attempts with token counts, two merged PRs, escalations, owner steps
    — the numbers ``factory status`` computes from it."""
    pr_a = "https://github.com/fixture/repo/pull/7"
    pr_c = "https://github.com/fixture/repo/pull/9"
    return {
        "version": 1,
        "project": PROJECT,
        "updated_at": "2026-10-18T15:00:00Z",
        "tasks": [
            _task(
                "T-A", "done",
                attempts=[
                    _attempt("r1", rounds=1, in_tok=100, out_tok=200),
                    _attempt("r2", rounds=2, in_tok=300, out_tok=400),
                ],
                links={"pr": pr_a, "merged": pr_a},
            ),
            _task(
                "T-B", "needs_human",
                attempts=[
                    _attempt("r3", "fail", rounds=3, in_tok=1000, out_tok=1000),
                    _attempt("r4", "escalated", rounds=2, in_tok=2000, out_tok=500,
                             escalation="attempts_exhausted",
                             note="3 attempts failed L0"),
                ],
            ),
            _task(
                "T-C", "in_review",
                attempts=[_attempt("r5", rounds=1, in_tok=500, out_tok=500)],
                links={"pr": pr_c, "merged": pr_c},
            ),
            _task(
                "T-D", "needs_human",
                attempts=[
                    _attempt("r6", "escalated", rounds=1, in_tok=10, out_tok=10,
                             escalation="credential_added",
                             note="fixture string looked like a credential"),
                ],
            ),
            _task(
                "T-E", "awaiting_owner",
                attempts=[_attempt("r7", rounds=2, in_tok=40, out_tok=60)],
                owner_steps=[
                    {"text": "step one", "done": True,
                     "done_at": "2026-10-04T09:00:00Z", "done_by": "owner",
                     "evidence": "checked"},
                    {"text": "step two", "done": False},
                ],
            ),
            _task("T-F", "ready"),
        ],
    }


def _root(tmp_path: Path, doc: dict | None = None) -> tuple[Path, Path]:
    """One fixture factory root: profile + validated queue on disk."""
    factory_dir = tmp_path / ".factory"
    factory_dir.mkdir()
    profile = {
        "name": PROJECT,
        "repo": {
            "path": str(tmp_path),
            "default_branch": "main",
            "remote": str(tmp_path),
            "merge_method": "squash",
        },
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "dependency_manifests": [],
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }
    (factory_dir / "project.yaml").write_text(yaml.safe_dump(profile))
    queue = factory_dir / "tasks.json"
    queue.write_text(json.dumps(doc or _queue_doc(), indent=2) + "\n")
    return tmp_path, queue


def _trace(root: Path, run_id: str, *, end: bool = True) -> Path:
    """One fixture trace through the real F-30 writer: two phase lanes, a
    tool call, gate-check evidence, per-phase tokens and a session cost,
    and (unless ``end=False``) the ``run_end`` marker."""
    writer = TraceWriter(root, PROJECT, run_id, "T-1")
    with writer.phase("setup"):
        pass
    with writer.phase("implementer") as spend:
        spend.add(SimpleNamespace(
            tokens={"input_tokens": 1200, "output_tokens": 300}))
        writer.append({
            "type": "tool_call",
            "seat": "implementer",
            "tool": "Bash",
            "args": "pytest -q",
            "result": "1 passed in 0.02s",
            "ok": True,
            "started": "2026-10-18T15:01:00Z",
            "ended": "2026-10-18T15:01:04Z",
        })
        writer.session_end(SimpleNamespace(
            status="ok", reason=None,
            tokens={"input_tokens": 1200, "output_tokens": 300},
            total_cost_usd=0.0123, num_turns=3, model="fixture-model",
            duration_ms=41000,
        ), "implementer")
        writer.gate_checks({
            "layer": "L0",
            "commands": [{
                "name": "test", "command": "pytest -q", "exit_code": 0,
                "output": "1 passed in 0.02s",
            }],
        })
    if end:
        writer.run_end("published")
    return writer.path


def _open_trace(root: Path, run_id: str) -> TraceWriter:
    """A trace that has started but is not ending: one phase pair, no
    ``run_end`` — what a stream reads while it waits for more events."""
    writer = TraceWriter(root, PROJECT, run_id, "T-1")
    with writer.phase("setup"):
        pass
    return writer


class Server:
    """A real dashboard server on loopback; every request goes over
    HTTP exactly as a browser would send it."""

    def __init__(self, root: Path, queue: Path, port: int = 0):
        self.server = make_server(factory_root=root, queue=queue, port=port)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def request(
        self, path: str, *, method: str = "GET", host: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """One raw HTTP request; ``host`` overrides the Host header (a
        rebinding probe connects to 127.0.0.1 but claims another name)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, headers={} if host is None
                         else {"Host": host})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def get(self, path: str, **kw) -> tuple[int, dict[str, str], bytes]:
        return self.request(path, **kw)

    def open_stream(self, run_id: str) -> tuple[http.client.HTTPConnection,
                                                http.client.HTTPResponse]:
        """Open the SSE endpoint and hand back the live response, so a
        test can interleave reads with trace appends."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("GET", f"/runs/{run_id}/events")
        return conn, conn.getresponse()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under the factory root: mtime and content hash."""
    out: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            out[str(path.relative_to(root))] = (stat.st_mtime_ns, digest)
    return out


def _read_data(resp: http.client.HTTPResponse) -> str | None:
    """The next SSE ``data:`` payload, or None at end of stream."""
    while True:
        line = resp.readline()
        if not line:
            return None
        if line.startswith(b"data: "):
            return line[len(b"data: "):].rstrip(b"\n").decode()


def _metrics_section(page: str) -> str:
    """Everything from the metrics heading down — the numbers, without
    the page's clock-stamped header line."""
    return page[page.index("## Metrics"):]


# --------------------------------------------------------------- tests --


def test_ac_1(tmp_path: Path):
    """`factory dashboard` serves on 127.0.0.1 only, on the stdlib
    threading server, with static files shipped in the package and no new
    dependency; one held-open event stream does not starve a second
    request."""
    root, queue = _root(tmp_path)
    srv = Server(root, queue)
    try:
        # The bound address: loopback, and an explicit --port is honored.
        assert isinstance(srv.server, http.server.ThreadingHTTPServer)
        assert srv.server.server_address[0] == "127.0.0.1"
        probe = make_server(factory_root=root, queue=queue, port=0)
        free_port = probe.server_address[1]
        probe.server_close()
        pinned = make_server(factory_root=root, queue=queue, port=free_port)
        try:
            assert pinned.server_address == ("127.0.0.1", free_port)
        finally:
            pinned.server_close()

        # Static files ship in the package and are served.
        static_dir = importlib.resources.files("factory") / "dashboard_static"
        assert (static_dir / "app.js").is_file()
        assert (static_dir / "style.css").is_file()
        status, headers, body = srv.get("/static/app.js")
        assert status == 200
        assert len(body) > 0

        # The CLI exposes `factory dashboard [--port N]`.
        args = build_parser().parse_args(["dashboard", "--port", "1234"])
        assert args.port == 1234

        # No new dependency: the shipped list is unchanged.
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml")
        deps = set(tomllib.loads(pyproject.read_text())["project"]["dependencies"])
        assert deps == KNOWN_DEPENDENCIES

        # One event stream held open; a second request is still answered
        # (the threading server serves each connection on its own thread).
        _trace(root, RUN_ID, end=False)
        conn, resp = srv.open_stream(RUN_ID)
        try:
            first = _read_data(resp)
            assert first is not None
            assert json.loads(first)["type"] == "phase_start"
            status, _, body = srv.get("/healthz")
            assert status == 200
            assert body == b"ok"
        finally:
            conn.close()
    finally:
        srv.close()


def test_ac_2(tmp_path: Path):
    """GET only (everything else 405); every route refuses a foreign Host
    with 403; and a full route sweep against a fixture factory root
    changes no file under it (mtime and hash)."""
    root, queue = _root(tmp_path)
    _trace(root, RUN_ID)
    before = _snapshot(root)
    srv = Server(root, queue)
    try:
        routes = [
            route.replace("<run-id>", RUN_ID)
                 .replace("<task-id>", "T-1")
                 .replace("<name>", "app.js")
            for route in GET_ROUTES
        ]
        # Every declared route answers GET with 200 on the fixture root.
        for route in routes:
            status, _, body = srv.get(route)
            assert status == 200, f"{route}: got {status}"
        assert _snapshot(root) == before

        # Every other method gets 405, with the one allowed method named.
        for route in routes:
            for method in ("POST", "PUT", "DELETE", "HEAD", "PATCH"):
                status, headers, _ = srv.request(route, method=method)
                assert status == 405, f"{route} {method}: got {status}"
                assert headers.get("Allow") == "GET"

        # Every route checks the Host header: DNS-rebinding names, a bare
        # host without the port, and a wrong port all get 403. One pass
        # per route.
        for route in routes:
            for hostile in ("evil.example", "evil.example:80",
                            "127.0.0.1", f"localhost:{srv.port + 1}"):
                status, _, _ = srv.get(route, host=hostile)
                assert status == 403, f"{route} Host {hostile}: got {status}"
            # The two allowed hosts pass on every route.
            assert srv.get(route, host=f"127.0.0.1:{srv.port}")[0] == 200
            assert srv.get(route, host=f"localhost:{srv.port}")[0] == 200

        # The sweep — reads only: no file under the factory root changed.
        assert _snapshot(root) == _snapshot(root)
    finally:
        srv.close()


def test_ac_3(tmp_path: Path):
    """The queue view groups every task by status, shows escalation
    reasons for needs_human and open owner steps for awaiting_owner, all
    read after schema validation — invalid state shows an error page and
    never partial data."""
    root, queue = _root(tmp_path)
    srv = Server(root, queue)
    try:
        status, headers, body = srv.get("/queue")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        page = body.decode()

        # Every task appears, under its status group.
        for status_name in ("ready", "in_progress", "awaiting_owner",
                            "needs_human", "done"):
            assert f'id="status-{status_name}"' in page, status_name
        section = page[page.index('id="status-needs_human"'):
                       page.index('id="status-awaiting_owner"')]
        assert "T-B" in section and "T-D" in section
        assert "T-1" not in section  # a ready task is in its own group
        assert 'id="status-done"' in page and "T-A" in page

        # needs_human: the escalation reason (and note) of the last
        # escalated attempt.
        assert "attempts_exhausted" in page
        assert "3 attempts failed L0" in page
        assert "credential_added" in page

        # awaiting_owner: the open steps (and the done one marked done).
        assert "step two" in page
        assert "step one" in page
        assert "data-step-done" in page and "data-step-open" in page
    finally:
        srv.close()

    # Invalid state (schema-invalid queue): an error page, never the
    # tasks that would still validate.
    broken = {
        "version": 1,
        "project": PROJECT,
        "updated_at": "2026-10-18T15:00:00Z",
        "tasks": [
            {"id": "T-9", "status": "not-a-status",
             "title": "Half-written queue"},
        ],
    }
    queue.write_text(json.dumps(broken))
    status, headers, body = srv.get("/queue")
    assert status >= 400
    assert headers["Content-Type"].startswith("text/html")
    page = body.decode()
    assert "T-9" not in page
    assert "Half-written queue" not in page
    # No partial data either: the earlier, valid fixture's content is gone.
    assert "T-B" not in page
    assert "3 attempts failed L0" not in page


def test_ac_4(tmp_path: Path):
    """The run view: phase lanes, tool calls, gate checks with evidence,
    tokens and cost per phase, from the F-30 trace; the event stream
    delivers appended events in order and closes on run_end."""
    root, queue = _root(tmp_path)
    _trace(root, RUN_ID)
    srv = Server(root, queue)
    try:
        status, headers, body = srv.get(f"/runs/{RUN_ID}")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        page = body.decode()
        # Both phase lanes are on the page.
        assert "setup" in page and "implementer" in page
        # The tool call sits in its lane.
        assert "Bash" in page and "pytest -q" in page
        # Gate checks carry their evidence.
        assert "gate_check" in page or "gate check" in page.lower()
        assert "exit 0" in page
        # Tokens and cost per phase (1200 in / 300 out; $0.0123).
        assert "1200" in page and "300" in page
        assert "0.0123" in page
        # The events the live renderer consumes ship with the page.
        assert 'id="trace-events"' in page
        events = json.loads(page[
            page.index('id="trace-events">')
            + len('id="trace-events">'):page.index("</script>")])
        # The full fixture trace, in write order: the L0 report renders
        # one gate_check row per command plus its diff_policy row.
        assert [e["type"] for e in events] == [
            "phase_start", "phase_end", "phase_start", "tool_call",
            "session_end", "gate_check", "gate_check", "phase_end", "run_end"]

        # Live: a second run's trace, streamed while it is written.
        writer = _open_trace(root, RUN_ID_2)
        conn, resp = srv.open_stream(RUN_ID_2)
        try:
            first = _read_data(resp)
            assert first is not None
            assert json.loads(first)["type"] == "phase_start"

            writer.append({
                "type": "tool_call", "seat": "implementer", "tool": "Read",
                "args": "BUILD-ORDER.md", "result": "ok", "ok": True,
                "started": "2026-10-18T15:02:00Z",
                "ended": "2026-10-18T15:02:01Z",
            })
            writer.run_end("published")

            delivered = [json.loads(first)]
            while True:
                payload = _read_data(resp)
                if payload is None:
                    break
                delivered.append(json.loads(payload))
            # In order, nothing lost, nothing invented.
            assert [e["type"] for e in delivered] == [
                "phase_start", "phase_end", "tool_call", "run_end"]
            assert delivered[-1]["outcome"] == "published"
            # The stream closed on run_end: the next read hit EOF.
        finally:
            conn.close()
    finally:
        srv.close()


def test_ac_5(tmp_path: Path):
    """Metrics come from the F-11 functions: for the F-11 fixture, the
    dashboard's numbers equal `factory status` output."""
    root, queue = _root(tmp_path, doc=_metrics_doc())
    srv = Server(root, queue)
    try:
        status, headers, body = srv.get("/metrics")
        assert status == 200
        assert headers["Content-Type"].startswith("text/plain")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli_main([
                "status", "--queue", str(queue),
                "--factory-root", str(root), "--repo", str(root),
            ])
        assert rc == 0
        assert _metrics_section(body.decode()).rstrip("\n") == \
            _metrics_section(out.getvalue()).rstrip("\n")

        # The numbers really are the F-11 outcome measures, not blanks.
        metrics = _metrics_section(body.decode())
        assert "tokens per merged PR: 1,000 (2 merged PRs)" in metrics
        assert "interventions per task: 0.40" in metrics
    finally:
        srv.close()


def test_ac_6(tmp_path: Path):
    """URL paths are ids matched against the schema's id patterns; `..`
    and absolute paths cannot read outside the factory root."""
    root, queue = _root(tmp_path)
    _trace(root, RUN_ID)
    queue_marker = "3 attempts failed L0"  # queue content that must never leak
    srv = Server(root, queue)
    try:
        escapes = [
            "/runs/../../tasks.json",
            "/runs/../tasks.json",
            f"/runs/{RUN_ID}/../../tasks.json",
            "/tasks/../tasks.json",
            "/tasks/%2e%2e/tasks.json",
            "/tasks/..%2Ftasks.json",
            "/tasks/....//tasks.json",
            "/static/../tasks.json",
            "/static/..%2f/tasks.json",
            "/static/%2e%2e/%2e%2e/tasks.json",
            "/static//etc/passwd",
            "//etc/passwd",
            "/../tasks.json",
            "/runs/not-a-run-id",
            "/runs/20260101T000000Z-AB12CD",  # wrong case for the hex part
            "/runs/20260101T000000Z-ffffff",  # well-formed run id, no trace
            "/tasks/T-ZZZ",                   # well-formed id, no such task
            "http://127.0.0.1/tasks.json",    # absolute-form request target
        ]
        for path in escapes:
            status, _, body = srv.get(path)
            assert status == 404, f"{path}: got {status}"
            assert queue_marker.encode() not in body, path
            assert b"T-B" not in body, path
            assert b"tasks.json" not in body.replace(b"..%2f", b""), path

        # Real ids still serve.
        assert srv.get(f"/runs/{RUN_ID}")[0] == 200
        assert srv.get("/tasks/T-1")[0] == 200
        assert srv.get("/queue")[0] == 200
    finally:
        srv.close()


def test_ac_7(tmp_path: Path):
    """`GET /healthz` returns 200 with body `ok` without reading the queue
    or any trace — a dead server is distinguishable from a broken queue."""
    root, queue = _root(tmp_path)
    _trace(root, RUN_ID)
    srv = Server(root, queue)
    try:
        status, headers, body = srv.get("/healthz")
        assert status == 200
        assert body == b"ok"
        assert headers["Content-Type"].startswith("text/plain")

        # Still `ok` with the queue broken to unreadable JSON...
        queue.write_text("{ this is not json")
        status, _, body = srv.get("/healthz")
        assert status == 200 and body == b"ok"
        # ...and the queue view on the same broken state is an error, so
        # a smoke test can tell the two failures apart.
        assert srv.get("/queue")[0] >= 400

        # Still `ok` with no trace at all to read.
        for trace in (root / "trace" / PROJECT).glob("*.jsonl"):
            trace.unlink()
        status, _, body = srv.get("/healthz")
        assert status == 200 and body == b"ok"
        assert srv.get(f"/runs/{RUN_ID}")[0] == 404
    finally:
        srv.close()
