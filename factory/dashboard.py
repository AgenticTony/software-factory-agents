"""The read-only dashboard (``factory dashboard``, F-31).

One local page that shows the queue, the metrics and any run, live,
instead of reading ``tasks.json``, progress logs and traces by hand.
Read-only by construction: every request path ends in a read through the
existing loaders (F-02 contract loading, F-11 status rendering, F-30
trace reading) — nothing here writes, so the dashboard can never become
a second writer of the queue.

The HTTP layer is the standard library's ``http.server``
(:class:`~http.server.ThreadingHTTPServer`, AC-1); the pages are
server-rendered HTML plus the static assets shipped in the package
(``factory/dashboard_static/``). No web framework, no new dependency.

Safety posture (AC-2, AC-6):

* Loopback only — the socket binds ``127.0.0.1``, never a public
  interface.
* ``GET`` only — every other method gets ``405`` with ``Allow: GET``.
* Every route checks the ``Host`` header before anything else and
  refuses anything but ``127.0.0.1:<port>`` / ``localhost:<port>`` with
  ``403`` (DNS rebinding: a page on an attacker's domain pointing a
  form or fetch at ``127.0.0.1`` arrives with *their* host in the
  ``Host`` header; the browser cannot forge this one).
* URL path segments are ids, not paths: run ids must match
  :data:`factory.runs.RUN_ID_RE` (the schema-side pattern ``trace`` uses
  to stay inside ``<factory_root>/trace/<project>/``), task ids are
  conservative id segments (the queue schema's ``id`` is a plain
  string, so the match is a strict charset plus membership in the
  *validated* queue) and static names come from a fixed whitelist. No
  route ever builds a filesystem path from URL input, so ``..``,
  ``%2e%2e`` and absolute-form targets cannot read outside the factory
  root — they 404.
* ``/healthz`` answers ``ok`` before any queue, profile or trace is
  read, so a smoke test (and F-35's confirm flow) can tell a dead
  server from a broken queue.

One event transport for everything live: ``GET /runs/<run-id>/events``
is a server-sent event stream fed by the same follower as ``factory
trace --follow`` (:func:`factory.trace.read_trace` with the same stop
conditions — ``run_end``, a run that is no longer live, a pruned
trace). The page's JavaScript falls back to polling if the stream
drops.
"""

from __future__ import annotations

import dataclasses
import html
import http.server
import importlib.resources
import json
import re
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory.contract import ContractError, load_profile, load_tasks
from factory.runs import FACTORY_ROOT_DEFAULT, RUN_ID_RE, project_component
from factory.state import run_lease_held
from factory.status import needs_human_tasks, render_status
from factory.trace import TraceError, read_trace, trace_path

__all__ = ["DEFAULT_PORT", "GET_ROUTES", "Dashboard", "make_server"]

DEFAULT_PORT = 7788

# The route table (AC-2's sweep surface; F-34 extends the *POST* set in
# its own code, this GET set stays the dashboard's). Placeholders are
# what a test fills in to sweep every route.
GET_ROUTES = (
    "/",
    "/healthz",
    "/queue",
    "/metrics",
    "/tasks/<task-id>",
    "/runs/<run-id>",
    "/runs/<run-id>/events",
    "/static/<name>",
)

# A task id (or static name) as a URL segment: the queue schema's ``id``
# is a plain string, so the URL match is a strict charset — alphanumeric
# first character, no separators a path could abuse — and, for tasks,
# membership in the validated queue. ``..``, ``%2e%2e`` and anything
# containing a slash fail the match and 404.
_ID_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

# Static assets shipped in the package, name -> content type. A fixed
# whitelist: nothing under ``/static/`` is ever resolved on disk from
# the URL, so traversal there is impossible by construction.
_STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

# Page group order for the queue view: what needs the operator first
# sorts first. (The members mirror factory/data/schema/tasks.schema.json's status
# enum; the order is the dashboard's presentation choice.)
_STATUS_ORDER = (
    "needs_human", "awaiting_owner", "in_progress", "in_review",
    "deploying", "ready", "blocked", "draft", "done", "not_buildable",
)


@dataclasses.dataclass(frozen=True)
class Response:
    """One non-streaming HTTP answer, transport-free."""

    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()


@dataclasses.dataclass(frozen=True)
class EventsRoute:
    """The SSE route, matched but not yet rendered: the handler streams
    it (it has no single body to send)."""

    run_id: str
    project: str
    trace: Path
    after: int = 0


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _page(title: str, body: str) -> Response:
    """One full HTML page, head to foot."""
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — factory dashboard</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header><h1>factory dashboard</h1>
<nav><a href="/">overview</a> <a href="/queue">queue</a>
<a href="/metrics">metrics</a></nav></header>
<main>
{body}
</main>
</body>
</html>"""
    return Response(200, doc.encode())


def _error(status: int, title: str, detail: str = "") -> Response:
    """An error page — the whole page, never a partial render."""
    detail_html = f"\n<p>{_esc(detail)}</p>" if detail else ""
    return Response(status, (
        f"<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{_esc(title)} — factory dashboard</title>"
        f"<link rel=\"stylesheet\" href=\"/static/style.css\"></head>"
        f"<body><h1>{_esc(title)}</h1>{detail_html}</body></html>\n"
    ).encode())


class Dashboard:
    """Routes and rendering, transport-free: :meth:`authorize` and
    :meth:`get` are pure enough to drive without a socket, and
    :class:`DashboardServer` is the thin HTTP shell around them."""

    def __init__(self, factory_root: Path | str, queue: Path | str) -> None:
        self.factory_root = Path(factory_root)
        self.queue_path = Path(queue)
        # The bound port; DashboardServer sets it right after the bind,
        # before any request is served.
        self.port = 0

    # ------------------------------------------------------- request gate

    def authorize(self, method: str, host: str | None) -> Response | None:
        """The check every route runs before anything else: the ``Host``
        header must name this loopback server (DNS rebinding gets 403),
        and the method must be GET (everything else gets 405). None
        means: proceed."""
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if (host or "").strip().lower() not in allowed:
            return _error(403, "Forbidden — wrong Host for this server")
        if method != "GET":
            return Response(405, b"GET only\n", "text/plain; charset=utf-8",
                            (("Allow", "GET"),))
        return None

    # ------------------------------------------------------------ routing

    def get(self, path: str) -> Response | EventsRoute:
        """Route one GET path. Returns the response, or an
        :class:`EventsRoute` for the SSE route (the handler streams
        it)."""
        path, _, query = path.partition("?")
        path = urllib.parse.unquote(path)
        if "\x00" in path or "\\" in path:
            return _error(404, "Not found")
        if path == "/":
            return self.overview()
        if path == "/healthz":
            # AC-7: no queue read, no profile read, no trace read — a
            # dead server must be tellable from a broken queue.
            return Response(200, b"ok", "text/plain; charset=utf-8")
        if path == "/queue":
            return self.queue_page()
        if path == "/metrics":
            return self.metrics_page()

        match = re.fullmatch(rf"/static/({_ID_SEGMENT.pattern})", path)
        if match:
            return self.static_file(match.group(1))
        match = re.fullmatch(rf"/tasks/({_ID_SEGMENT.pattern})", path)
        if match:
            return self.task_page(match.group(1))
        match = re.fullmatch(rf"/runs/({RUN_ID_RE.pattern})/events", path)
        if match:
            return self.events_route(match.group(1), _after_seq(query))
        match = re.fullmatch(rf"/runs/({RUN_ID_RE.pattern})", path)
        if match:
            return self.run_page(match.group(1))
        return _error(404, "Not found")

    # ------------------------------------------------------------ loaders

    def project(self) -> str:
        """The project name, from the validated profile (the same file
        ``factory trace`` reads it from)."""
        return load_profile(self.queue_path.parent / "project.yaml")["name"]

    def _queue(self) -> dict:
        """The queue, after schema validation (F-02) and the profile
        pair check (F-62 AC-3): a queue whose ``project`` disagrees with
        the profile this dashboard serves refuses — invalid or
        mismatched state raises :class:`ContractError`, which every
        page renders as an error page, never partial data."""
        queue = load_tasks(self.queue_path)
        profile_name = self.project()
        if queue.get("project") != profile_name:
            raise ContractError(
                f"the queue at {self.queue_path} names project "
                f"{queue.get('project')!r} but the profile names "
                f"{profile_name!r}: a queue and profile that disagree "
                "cannot name one project's namespace"
            )
        return queue

    def _contract_error(self, exc: ContractError) -> Response:
        return _error(503, "The factory state is not readable", str(exc))

    # ------------------------------------------------------------- pages

    def overview(self) -> Response:
        try:
            project = self.project()
            queue = self._queue()
        except ContractError as exc:
            return self._contract_error(exc)
        counts: dict[str, int] = {}
        for task in queue.get("tasks") or []:
            status = task.get("status", "?")
            counts[status] = counts.get(status, 0) + 1
        rows = "".join(
            f"<li><a href=\"/queue#status-{_esc(status)}\">{_esc(status)}</a>"
            f" — {counts[status]}</li>"
            for status in _STATUS_ORDER if status in counts
        )
        try:
            # F-62 AC-3: a validated slug — the same glob the status
            # reader uses, never an arbitrary directory list.
            trace_dir = (
                self.factory_root / "trace" / project_component(project)
            )
            runs = sorted(
                (entry for entry in trace_dir.glob("*.jsonl")
                 if RUN_ID_RE.fullmatch(entry.stem)),
                key=lambda entry: entry.stem,
            )
            run_list = "".join(
                f"<li><a href=\"/runs/{_esc(entry.stem)}\">{_esc(entry.stem)}"
                f"</a></li>"
                for entry in runs
            ) or "<li>no runs yet</li>"
        except OSError:
            run_list = "<li>no runs yet</li>"
        body = f"""
<section id="queue-counts">
<h2>queue</h2>
<ul>{rows or '<li>no tasks</li>'}</ul>
<p><a href="/queue">open the queue view</a></p>
</section>
<section id="runs">
<h2>runs</h2>
<ul>{run_list}</ul>
</section>"""
        return _page("overview", body)

    def queue_page(self) -> Response:
        try:
            queue = self._queue()
        except ContractError as exc:
            return self._contract_error(exc)
        tasks = queue.get("tasks") or []
        groups: dict[str, list[dict]] = {}
        for task in tasks:
            groups.setdefault(task.get("status", "?"), []).append(task)
        escalations = {
            entry["id"]: entry for entry in needs_human_tasks(queue)}

        sections: list[str] = []
        for status in _STATUS_ORDER:
            group = groups.get(status)
            if not group:
                continue
            cards = []
            for task in group:
                tid = task.get("id", "?")
                extra = ""
                if status == "needs_human":
                    reason = escalations.get(tid) or {}
                    why = reason.get("escalation") or "no escalation recorded"
                    note = reason.get("note")
                    extra = (f"<p class=\"escalation\">{_esc(why)}"
                             + (f": {_esc(note)}" if note else "")
                             + "</p>")
                elif status == "awaiting_owner":
                    steps = task.get("owner_steps") or []
                    if steps:
                        items = "".join(
                            f"<li class=\"step\" "
                            f"data-step-{'done' if step.get('done') else 'open'}>"
                            f"{_esc(step.get('text', '?'))}"
                            f"{' — done' if step.get('done') else ' — open'}"
                            f"</li>"
                            for step in steps)
                        extra = (f"<p>owner steps:</p><ul class=\"owner-steps\">"
                                 f"{items}</ul>")
                    else:
                        extra = "<p>no owner steps recorded</p>"
                cards.append(
                    f"<li class=\"task\" data-task=\"{_esc(tid)}\">"
                    f"<span class=\"task-id\">{_esc(tid)}</span> "
                    f"{_esc(task.get('title', ''))}"
                    f"<a class=\"task-link\" href=\"/tasks/{_esc(tid)}\">"
                    f"detail</a>{extra}</li>")
            sections.append(
                f"<section id=\"status-{_esc(status)}\"><h2>{_esc(status)} "
                f"({len(group)})</h2><ul>{''.join(cards)}</ul></section>")
        # A status the dashboard does not order (a schema change the
        # dashboard has not caught up with) still renders — grouped, at
        # the end, never dropped.
        for status in sorted(set(groups) - set(_STATUS_ORDER)):
            ids = ", ".join(t.get("id", "?") for t in groups[status])
            sections.append(
                f"<section id=\"status-{_esc(status)}\"><h2>{_esc(status)} "
                f"({len(groups[status])})</h2><p>{_esc(ids)}</p></section>")
        return _page("queue", "\n".join(sections)
                     or "<p>the queue is empty</p>")

    def task_page(self, task_id: str) -> Response:
        try:
            queue = self._queue()
        except ContractError as exc:
            return self._contract_error(exc)
        task = next((t for t in (queue.get("tasks") or [])
                     if t.get("id") == task_id), None)
        if task is None:
            return _error(404, "Not found")
        rows = "".join(
            f"<tr><th>{_esc(key)}</th><td>{_esc(value)}</td></tr>"
            for key, value in (
                ("status", task.get("status")),
                ("priority", task.get("priority")),
                ("title", task.get("title")),
                ("source", task.get("source")),
                ("risk", ", ".join(task.get("risk") or []) or "none"),
                ("attempts", len(task.get("attempts") or [])),
                ("budget", task.get("budget", {}).get("size", "?")),
            ))
        escalation, note = None, None
        for attempt in reversed(task.get("attempts") or []):
            if attempt.get("escalation"):
                escalation, note = attempt.get("escalation"), attempt.get("note")
                break
        if escalation:
            rows += (f"<tr><th>escalation</th><td>{_esc(escalation)}"
                     + (f": {_esc(note)}" if note else "")
                     + "</td></tr>")
        return _page(f"task {task_id}",
                     f"<section id=\"task\"><h2>{_esc(task_id)}</h2>"
                     f"<table>{rows}</table></section>")

    def metrics_page(self) -> Response:
        try:
            queue = self._queue()
            project = self.project()
            profile = load_profile(self.queue_path.parent / "project.yaml")
        except ContractError as exc:
            return self._contract_error(exc)
        # The F-11 render, verbatim — the numbers ARE `factory status`
        # output, one implementation, no second one here (AC-5).
        text = render_status(queue, project=project,
                             factory_root=self.factory_root,
                             repo=Path(profile["repo"]["path"]))
        return Response(200, text.encode(), "text/plain; charset=utf-8")

    def static_file(self, name: str) -> Response:
        content_type = _STATIC_FILES.get(name)
        if content_type is None:
            return _error(404, "Not found")
        # this module's package is literally "factory" (typed Package;
        # __package__ is str | None to the checker)
        resource = importlib.resources.files("factory") / "dashboard_static" / name
        return Response(200, resource.read_bytes(), content_type)

    # -------------------------------------------------------------- runs

    def _trace_of(self, run_id: str) -> tuple[str, Path] | Response:
        try:
            project = self.project()
        except ContractError as exc:
            return self._contract_error(exc)
        path = trace_path(self.factory_root, project, run_id)
        if not path.is_file():
            return _error(404, "Not found")
        return project, path

    def events_route(self, run_id: str, after: int = 0) -> Response | EventsRoute:
        found = self._trace_of(run_id)
        if isinstance(found, Response):
            return found
        project, path = found
        return EventsRoute(run_id, project, path, after)

    def run_page(self, run_id: str) -> Response:
        found = self._trace_of(run_id)
        if isinstance(found, Response):
            return found
        _, path = found
        lines: list[str] = []
        read_trace(path, follow=False, out=lines.append)
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"type": "trace_corrupt", "raw": line})
        # The live renderer consumes exactly these events; the page's
        # JavaScript builds the swim lanes from them.
        payload = json.dumps(events, ensure_ascii=False).replace("</", "<\\/")
        return _page(f"run {run_id}", f"""
<section id="run" data-run-id="{_esc(run_id)}">
<h2>run {_esc(run_id)}</h2>
<p id="stream-state">connecting…</p>
<div id="lanes"></div>
<noscript>
{_noscript_lanes(events)}
</noscript>
<script type="application/json" id="trace-events">{payload}</script>
<script src="/static/app.js"></script>
</section>""")

    def stream_events(
        self, route: EventsRoute, write: Callable[[bytes], None],
    ) -> None:
        """The SSE body (AC-4): the same follower as ``factory trace
        --follow`` — :func:`factory.trace.read_trace` with the same stop
        conditions (``run_end``, a run that is no longer live, a pruned
        trace) — with every trace line framed as one server-sent event.
        A vanished client ends the write and the stream; nothing is
        written to the factory root."""
        def send(line: str) -> None:
            if not _already_embedded(line, route.after):
                write(f"data: {line}\n\n".encode())

        run_dir = (
            self.factory_root / "runs"
            / project_component(route.project) / route.run_id
        )

        def live() -> bool:
            return run_dir.exists() and run_lease_held(self.queue_path)

        try:
            read_trace(route.trace, follow=True, out=send, live=live)
        except (OSError, TraceError):
            pass  # the client is gone (or the trace is corrupt): stop


def _after_seq(query: str) -> int:
    """The ``after`` cursor of an events URL: the last ``seq`` the page
    embedded. Anything but a non-negative integer means no cursor, so a
    bad value can only replay events, never hide them."""
    values = urllib.parse.parse_qs(query).get("after") or [""]
    return int(values[0]) if values[0].isdecimal() else 0


def _already_embedded(line: str, after: int) -> bool:
    """Whether the page already holds this trace line. ``run_end`` is
    never skipped: it is what closes the page's EventSource, which would
    otherwise reconnect to a finished run forever. A line with no usable
    ``seq`` is sent, since it can't be shown to be a duplicate."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    seq = event.get("seq") if isinstance(event, dict) else None
    return (isinstance(seq, int) and seq <= after
            and event.get("type") != "run_end")


def _noscript_lanes(events: list[dict]) -> str:
    """The run view without JavaScript: the same lanes, as a plain list
    — phases in order, tool calls, gate evidence, tokens and cost."""
    phases: list[dict] = []
    current: dict | None = None
    for event in events:
        kind = event.get("type")
        if kind == "phase_start":
            current = {"name": event.get("phase", "?"), "tools": [],
                       "gates": [], "tokens": None, "cost": None}
            phases.append(current)
            continue
        if current is None:
            continue
        if kind == "phase_end":
            current["tokens"] = event.get("tokens")
        elif kind == "session_end":
            cost = event.get("cost_usd")
            if cost is not None:
                current["cost"] = cost
        elif kind == "tool_call":
            current["tools"].append(event)
        elif kind == "gate_check":
            current["gates"].append(event)

    sections = []
    for phase in phases:
        rows = []
        for tool in phase["tools"]:
            rows.append(
                f"<li class=\"tool {'ok' if tool.get('ok') else 'failed'}\">"
                f"<code>{_esc(tool.get('tool'))}</code> "
                f"{_esc(tool.get('args'))} → {_esc(tool.get('result'))}</li>")
        for gate in phase["gates"]:
            rows.append(
                f"<li class=\"gate {'ok' if gate.get('ok') else 'failed'}\">"
                f"gate check [{_esc(gate.get('layer'))}] "
                f"{_esc(gate.get('item'))}: {_esc(gate.get('note'))}</li>")
        spend = []
        tokens = phase["tokens"]
        if tokens:
            spend.append(
                f"tokens {tokens.get('input_tokens', 0)} in / "
                f"{tokens.get('output_tokens', 0)} out")
        if phase["cost"] is not None:
            spend.append(f"cost ${phase['cost']:.4f}")
        sections.append(
            f"<div class=\"lane\"><h3>{_esc(phase['name'])}</h3>"
            + (f"<p class=\"spend\">{' · '.join(spend)}</p>" if spend else "")
            + (f"<ul>{''.join(rows)}</ul>" if rows else "<p>no activity</p>")
            + "</div>")
    return "\n".join(sections) or "<p>no phases recorded</p>"


# ------------------------------------------------------------ HTTP shell


class _Handler(http.server.BaseHTTPRequestHandler):
    """The thin shell: gate (host, method), route, send — nothing else.
    Logging is silenced: the dashboard runs next to a prompt, not a
    log file."""

    server_version = "factory-dashboard/0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    @property
    def dashboard(self) -> Dashboard:
        # http.server stubs don't know the attached dashboard attribute
        # (narrow, rule-named; F-51/A-7)
        return self.server.dashboard  # pyright: ignore[reportAttributeAccessIssue]

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def _stream(self, route: EventsRoute) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(payload: bytes) -> None:
            self.wfile.write(payload)
            self.wfile.flush()

        try:
            self.dashboard.stream_events(route, write)
        except OSError:
            pass  # the reader hung up mid-stream: done

    def do_GET(self) -> None:
        deny = self.dashboard.authorize("GET", self.headers.get("Host"))
        if deny is None:
            try:
                routed = self.dashboard.get(self.path)
            except Exception:  # noqa: BLE001 — a broken render is a 500,
                routed = _error(500, "Internal error")  # never a dropped
            if isinstance(routed, EventsRoute):  # connection
                self._stream(routed)
                return
            deny = routed
        self._send(deny)

    def _refuse(self) -> None:
        deny = self.dashboard.authorize(self.command, self.headers.get("Host"))
        if deny is not None:
            self._send(deny)
            return
        # A GET-shaped method that is not GET cannot happen; answer the
        # policy directly rather than route it.
        self._send(Response(405, b"GET only\n",
                            "text/plain; charset=utf-8", (("Allow", "GET"),)))

    def __getattr__(self, name: str) -> Any:
        # http.server answers 501 for a method with no do_<METHOD>, so every
        # method but GET resolves here: AC-2 says all of them get 405.
        if name.startswith("do_"):
            return self._refuse
        raise AttributeError(name)


class DashboardServer(http.server.ThreadingHTTPServer):
    """The dashboard's HTTP server: bound to 127.0.0.1 (AC-1), one
    thread per connection so a held-open event stream never starves a
    second request."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, factory_root: Path | str, queue: Path | str,
                 port: int) -> None:
        self.dashboard = Dashboard(factory_root, queue)
        super().__init__(("127.0.0.1", port), _Handler)
        self.dashboard.port = self.server_address[1]


def make_server(factory_root: Path | str = FACTORY_ROOT_DEFAULT,
                queue: Path | str | None = None,
                port: int = DEFAULT_PORT) -> DashboardServer:
    """Bind the dashboard server on ``127.0.0.1:<port>`` (port 0 lets the
    OS pick). Serving is the caller's job: ``serve_forever()`` for the
    CLI, a thread for a test."""
    queue_path = (Path(queue) if queue is not None
                  else Path(factory_root) / ".factory" / "tasks.json")
    return DashboardServer(factory_root, queue_path, port)
