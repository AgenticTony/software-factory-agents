"""The run trace: one live JSONL event log per run (BUILD-ORDER F-30).

A run is visible only after it ends unless something records it while it
happens. This module is that recorder: ``<factory_root>/trace/<project>/
<run-id>.jsonl``, one event per line, appended as the events occur —
phases, tool calls, gate checks, session spend and the attempt's terminal
marker. The idea is Super Simple Software Factory's "every event written
while the agent works" (disler/super-simple-software-factory); we take the
trace and leave the rest.

Who writes what (the trust boundary is the sandbox):

* The **orchestrator** — outside the sandbox, free to write anywhere —
  owns the file. It writes the phase, gate, session and terminal events
  through :class:`TraceWriter`.
* The **session runner** — inside the F-05 sandbox, where the trace path
  is deliberately NOT writable (AC-1: outside ``allowWrite``) — can only
  observe its SDK stream. Its ``tool_call`` events cross the boundary as
  prefixed stdout lines (see :data:`STREAM_PREFIX`), streamed live through
  ``run_sandboxed``'s line callback, and the orchestrator appends them.
  The same stdout protocol the :class:`~factory.session.SessionRecord`
  itself rides (F-27's vents chose the record; tool calls cannot wait for
  it — AC-2 wants them on disk before the stream ends).

Redaction (AC-4): every string that reaches a line goes through the F-25
credential shapes (:func:`factory.sentinels.redact_credentials` — this
module is deliberately not a second pattern list) plus the caller's
literal secrets (the seat token), and is length-capped. The session-side
tap redacts before anything crosses the pipe; the writer redacts again —
untrusted text is scrubbed at every boundary it crosses, the same
invariant F-25/F-27 hold.

Failure (AC-5): tracing never changes a run. A write that fails (disk
full, permission) is reported once on stderr and the writer disables
itself; no exception ever leaves this module through a write path.

Privacy: a trace holds capped snippets of everything its run read and
wrote — on a PII-bearing project that is personal data. So the file is
owner-only (0600 under a 0700 dir) and does not live forever:
:func:`prune_traces` age-out, run by ``reclaim_runs`` under the lease
(no run of the project is alive), deletes files past
:data:`RETENTION_DAYS`.

Reading (AC-6): :func:`read_trace` prints the whole file in order, one
line per event — events after a ``run_end`` included (cleanup-time lines
exist). With ``follow=True`` it keeps printing appended lines until the
attempt ends — a ``run_end`` event, the terminal marker the orchestrator
stamps on every outcome (published, failed or aborted) — or until the
run can no longer write one (see ``live`` there). It reads only.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from factory.egress import check_secrets, strip_control_sequences
from factory.runs import RUN_ID_RE, project_component
from factory.sentinels import redact_credentials, scrub_literal
from factory.state import now_iso


class TraceError(Exception):
    """A trace file that cannot be read as text (corrupted, or a run
    killed mid-character)."""


# The event vocabulary (AC-1) plus the terminal marker AC-6's --follow
# needs: without it, "until the attempt ends" has nothing to stop on.
# F-47 (I-9): ``sandbox_escapee`` — a sandboxed command whose drain
# force-closed pipes held by a process that escaped its group
# (:func:`factory.sandbox.escapee_event`); written orchestrator-side only
# (the stdout forwarder passes tool_call alone, so a session cannot forge
# or suppress one), counted by ``factory status``.
EVENT_TYPES = (
    "phase_start", "phase_end", "tool_call", "gate_check", "session_end",
    "run_end", "sandbox_escapee",
    # F-56 review round 1: a record read and used after the orchestrator's
    # wall-clock kill (the file could only predate the kill). Emitted by
    # the session runner's timeout path, never accepted from stdout (the
    # forwarder still passes tool_call only).
    "record_used_after_timeout",
    # F-57 AC-2: the importer's ONE repair session — start and outcome
    # (repaired / still_refused / failed / quota_stopped /
    # workspace_failed) with its turns. Written orchestrator-side only
    # (the importer module's TraceWriter), never accepted from stdout.
    "import_repair",
)
PHASES = ("setup", "implementer", "L0", "L1", "L2", "publish")
RUN_END_OUTCOMES = ("published", "failed", "aborted")

# The stdout line prefix carrying events out of the sandboxed runner.
STREAM_PREFIX = "factory-trace "

# Tool args and result snippets: capped red-first-style (redact, then cap —
# capping first could cut a credential in half and leave a prefix no
# pattern matches, the F-27 review's finding 2). One tool call is evidence,
# not a transcript.
SNIPPET_CAP = 2000
# Any other string field, after redaction, keeps lines bounded.
STRING_CAP = 4000
TRUNCATED = " …[trace truncated]"
# F-43: scrub_literal moved to factory.sentinels (beside
# redact_credentials, so the egress seam composes one home); this module
# re-exports it unchanged — one implementation, same name, and
# factory.session's import keeps working.
# Command-output tails inside gate_check notes.
_NOTE_TAIL = 200
# --follow poll interval (seconds).
POLL_SEC = 0.2
# --follow's escape hatch: a run that can no longer write its run_end (a
# killed orchestrator, a writer disabled by a failed write) would else be
# followed forever. After this many no-growth polls past the run's death,
# the reader stops and says why.
GONE_POLLS = 25  # 5 s at POLL_SEC
# Trace retention: files older than this are pruned (they hold repo
# content, PII on some projects — evidence, but not forever).
RETENTION_DAYS = 14

# L0 finding ids produced by the diff scan (as opposed to the command and
# sentinel rows): the diff_policy row speaks for these.
_L0_DIFF_FINDING_IDS = frozenset(
    {"protected_path_touched", "test_deleted", "baseline_missing",
     "baseline_invalid"}
)


def trace_path(factory_root: Path | str, project: str, run_id: str) -> Path:
    """The trace file of one run — outside every clone (``runs/``) and
    outside the sandbox write allowlist by construction. The project is
    a validated slug (F-62 AC-3): an unvalidated component here puts
    the trace (and the reader that follows it) outside the project's
    namespace, outside the factory root, or inside another project's."""
    return (
        Path(factory_root) / "trace" / project_component(project)
        / f"{run_id}.jsonl"
    )


def snippet(text: str, secret: str | None = None, *,
            cap: int = SNIPPET_CAP) -> str:
    """Redact (the literal secret, then the F-25 shapes), then cap — the
    ONE implementation, shared by the writer's tool_call events and the
    session-side tap (two copies of this order can drift; the F-27
    review's finding 2 is why the order is redact-then-cap at all)."""
    text = scrub_literal(text, secret)
    return _cap(redact_credentials(text), cap)


def _cap(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[: limit - len(TRUNCATED)] + TRUNCATED
    return text


def _clean(value: Any, secrets: Sequence[str]) -> Any:
    """Redact and cap recursively: every string loses the literal secrets,
    the F-25 credential shapes, and everything past :data:`STRING_CAP`."""
    if isinstance(value, str):
        for secret in secrets:
            value = scrub_literal(value, secret)
        return _cap(redact_credentials(value), STRING_CAP)
    if isinstance(value, dict):
        return {key: _clean(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item, secrets) for item in value]
    return value


class _PhaseSpend:
    """The session records whose spend sums to one phase's token totals.

    AC-7: per-phase totals come from the same records the attempt record
    sums — no second source of truth for spend."""

    def __init__(self) -> None:
        self.records: list = []

    def add(self, record: Any) -> None:
        self.records.append(record)

    def totals(self) -> dict | None:
        totals: dict[str, int] = {}
        for record in self.records:
            usage = getattr(record, "tokens", None) or {}
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key) or 0
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    value = 0
                totals[key] = totals.get(key, 0) + int(value)
        return totals or None


class TraceWriter:
    """Appends one run's events to its trace file, live.

    One writer per run (one file per run id). ``attempt`` is whatever
    attempt is currently running — the orchestrator bumps it each round;
    every event is stamped with it (AC-1). All writes funnel through
    :meth:`append`, so stamping, redaction and the failure policy hold
    for every event type alike.
    """

    def __init__(self, factory_root: Path | str, project: str, run_id: str,
                 task_id: str, *, extra_secrets: Sequence[str] = ()) -> None:
        self.path = trace_path(factory_root, project, run_id)
        self.run_id = run_id
        self.task_id = task_id
        # Round 2 (class fix): a bare str extra_secrets iterated
        # character by character scrubbed nothing — loud at the entry.
        self.secrets = tuple(
            secret for secret in check_secrets(extra_secrets) if secret
        )
        self.attempt = 1
        self.seq = 0
        self.disabled = False
        self._ended = False
        self._private = False
        # F-38: the reviewer fan-out forwards tool_call events from
        # concurrent sandboxed sessions; the seq stamp and the write must
        # be atomic under them, or two threads interleave half-stamped
        # lines and fork the sequence.
        self._lock = threading.Lock()
        try:
            # Traces hold repo content: a directory this writer creates is
            # owner-only. An existing directory keeps its mode — never
            # widen one, and a deliberately read-only trace dir stays
            # read-only (AC-5's sabotage must not be silently repaired);
            # the file itself is chmod 0600 on first write either way.
            if not self.path.parent.exists():
                self.path.parent.mkdir(parents=True)
                self.path.parent.chmod(0o700)
        except OSError as exc:
            self._fail(exc)

    def _fail(self, exc: OSError) -> None:
        """AC-5: report once, disable, never touch the run again."""
        if not self.disabled:
            print(
                f"factory: trace write failed for run {self.run_id} "
                f"({type(exc).__name__}: {exc}); the run continues untraced",
                file=sys.stderr,
            )
        self.disabled = True

    def append(self, event: dict) -> None:
        """Stamp, redact and append one event; a failing write is reported
        once and the writer goes silent (the run never notices). The
        stamps merge AFTER the event, so no event dict — and nothing an
        untrusted source put inside one — can override them. Under the
        lock: from F-38 the reviewer fan-out appends from concurrent
        session threads."""
        with self._lock:
            if self.disabled:
                return
            self.seq += 1
            record = {
                **event,
                "seq": self.seq,
                "ts": now_iso(),
                "run_id": self.run_id,
                "task_id": self.task_id,
                "attempt": self.attempt,
            }
            line = json.dumps(_clean(record, self.secrets), ensure_ascii=False)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                if not self._private:
                    self.path.chmod(0o600)
                    self._private = True
            except OSError as exc:
                self._fail(exc)

    @contextmanager
    def phase(self, name: str) -> Iterator[_PhaseSpend]:
        """One phase's start/end pair, however the phase ends: a raise
        still writes ``phase_end`` (marked ``error``), so the trace never
        dangles an open phase. Sessions the phase runs append their records
        to the yielded spend box; their tokens land on ``phase_end``."""
        spend = _PhaseSpend()
        self.append({"type": "phase_start", "phase": name})
        error: BaseException | None = None
        try:
            yield spend
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            error = exc
            raise
        finally:
            event: dict = {"type": "phase_end", "phase": name}
            if error is not None:
                event["error"] = True
            tokens = spend.totals()
            if tokens:
                event["tokens"] = tokens
            self.append(event)

    def session_end(self, record: Any, seat: str) -> None:
        """One session's spend and outcome (AC-1's session_end row), plus
        the session's sandbox-bypass denial count (F-42 AC-5: the summary
        next to the marked ``tool_call`` events, never instead of them).

        ``turns`` is TELEMETRY, session-reported (F-64 AC-1): only a
        non-bool int >= 0 renders; anything else a forged record carries
        renders as None ("turns unknown"), never raw — the same rule the
        importer's ``_record_turns`` set. Turn budgets never read it.
        """
        turns = getattr(record, "num_turns", None)
        if isinstance(turns, bool) or not isinstance(turns, int) or turns < 0:
            turns = None
        self.append({
            "type": "session_end",
            "seat": seat,
            "status": getattr(record, "status", None),
            "reason": getattr(record, "reason", None),
            "tokens": getattr(record, "tokens", None),
            "cost_usd": getattr(record, "total_cost_usd", None),
            "turns": turns,
            "model": getattr(record, "model", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "sandbox_bypass_denials": getattr(record, "sandbox_bypass_denials", 0) or 0,
        })

    def gate_checks(self, report: dict) -> None:
        """Every check of one gate report as its own event (AC-3)."""
        for gate in gate_checks(report):
            self.append(gate)

    def run_end(self, outcome: str, escalation: str | None = None) -> None:
        """The attempt's terminal marker — what --follow stops on. The
        first terminal event wins: an error path after a recorded outcome
        (a PR open, then a crash) must not rewrite history."""
        if self._ended:
            return
        self._ended = True
        event: dict = {"type": "run_end", "outcome": outcome}
        if escalation:
            event["escalation"] = escalation
        self.append(event)


# ------------------------------------------------------ gate check evidence --


def _tail(text: str, limit: int = _NOTE_TAIL) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def gate_checks(report: dict) -> list[dict]:
    """The gate_check events of one L0/L1/L2 report dict (AC-3): one row
    per check on pass and on fail, each note saying what was looked at.

    * L0: one row per profile command (its command line and output tail),
      one per sentinel result (name, kind, risk flag, verdict), and one
      ``diff_policy`` row for the diff scan (protected paths, test
      deletions, baseline).
    * L1: one row per red/green proof — the ref, which tree it ran on,
      its exit code, and for red how it went red.
    * L2: one row per finding (axis, severity, location, carried state)
      and a ``verdict`` row for the review itself.
    """
    layer = report.get("layer")
    checks: list[dict] = []
    if layer == "L0":
        for command in report.get("commands") or []:
            exit_code = command.get("exit_code")
            ok = exit_code == 0 and not command.get("timed_out")
            note = (f"{command.get('name')}: `{command.get('command')}` "
                    f"-> exit {exit_code}")
            if command.get("timed_out"):
                note += " (timed out)"
            output = _tail(command.get("output") or "")
            if output:
                note += f"; output tail: {output}"
            checks.append({"type": "gate_check", "layer": "L0",
                           "item": command.get("name"), "ok": ok, "note": note})
        for sentinel in report.get("sentinel_results") or []:
            verdict = sentinel.get("verdict")
            note = f"sentinel {sentinel.get('name')} ({sentinel.get('kind')}"
            if sentinel.get("risk_flag"):
                note += f", risk flag {sentinel['risk_flag']}"
            note += f"): {verdict}"
            if sentinel.get("detail"):
                note += f" — {sentinel['detail']}"
            checks.append({"type": "gate_check", "layer": "L0",
                           "item": f"sentinel:{sentinel.get('name')}",
                           "ok": verdict == "pass", "note": note})
        diff_findings = [
            finding for finding in report.get("findings") or []
            if finding.get("id") in _L0_DIFF_FINDING_IDS
        ]
        note = ("diff base...head: protected paths, test deletions, baseline"
                + ("; " + "; ".join(
                    f"{finding.get('id')}: {finding.get('text')}"
                    for finding in diff_findings) if diff_findings else ""))
        checks.append({"type": "gate_check", "layer": "L0",
                       "item": "diff_policy", "ok": not diff_findings,
                       "note": note})
        # F-42 AC-1: the commit-message scan is its own check, said even
        # when clean — an owner reading the trace sees it ran.
        attribution = [
            finding for finding in report.get("findings") or []
            if finding.get("id") == "attribution_trailer"
        ]
        note = ("commit messages merge-base..head: AI attribution"
                + ("; " + "; ".join(
                    finding.get("text") or "" for finding in attribution)
                   if attribution else ""))
        checks.append({"type": "gate_check", "layer": "L0",
                       "item": "commit_messages", "ok": not attribution,
                       "note": note})
    elif layer == "L1":
        red_reasons = report.get("red_reasons") or {}
        for command in report.get("commands") or []:
            name = str(command.get("name") or "")
            ref = name.partition(":")[2]
            red = name.startswith("red:")
            exit_code = command.get("exit_code")
            if command.get("timed_out"):
                ok = False
            elif red:
                ok = exit_code is not None and exit_code != 0
            else:
                ok = exit_code == 0
            if red:
                note = (f"{ref} on the merge-base with only the diff's test "
                        f"files: exit {exit_code} (red reason: "
                        f"{red_reasons.get(ref)})")
            else:
                note = f"{ref} with the full diff applied: exit {exit_code}"
            if not ok:
                note += f"; output tail: {_tail(command.get('output') or '')}"
            checks.append({"type": "gate_check", "layer": "L1", "item": name,
                           "ok": ok, "note": note})
    elif layer == "L2":
        for finding in report.get("findings") or []:
            where = ""
            if finding.get("file"):
                where = f" ({finding.get('file')}:{finding.get('line') or ''})"
            note = (f"[{finding.get('axis')}/{finding.get('severity')}] "
                    f"{finding.get('text')}{where}")
            if finding.get("state") == "carried":
                note += " (carried from an earlier round)"
            if finding.get("tag"):
                # F-38: the verifier's survivor tag (and which reviewers
                # reported the finding, when the fan-out merged it).
                note += f" [{finding['tag']}]"
            if finding.get("reported_by"):
                note += f" (reviewers {','.join(map(str, finding['reported_by']))})"
            checks.append({"type": "gate_check", "layer": "L2",
                           "item": f"finding:{finding.get('id')}",
                           "ok": False, "note": note})
        parse_error = report.get("parse_error")
        ok = report.get("verdict") == "pass" and not parse_error
        note = f"reviewer verdict {report.get('declared') or report.get('verdict')}"
        if parse_error:
            note += f"; parse error: {_tail(parse_error)}"
        checks.append({"type": "gate_check", "layer": "L2", "item": "verdict",
                       "ok": ok, "note": note})
    return checks


# ------------------------------------------------------------------ reading --


def _is_run_end(line: str) -> bool:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(event, dict) and event.get("type") == "run_end"


def read_trace(path: Path, *, follow: bool = False,
               out: Callable[[str], None] = print,
               live: Callable[[], bool] | None = None) -> None:
    """Print the trace in order, one line per event (AC-6) — the whole
    file; a ``run_end`` line never truncates the read (cleanup-time events
    can follow one). With ``follow``, keep printing appended lines until
    the attempt ends — a ``run_end`` event — or until no marker can come:
    ``live`` says whether the run might still write (the CLI passes "the
    run dir exists"), and once it stops being true the reader waits
    :data:`GONE_POLLS` polls for stragglers, then stops and says why on
    stderr; a trace file deleted under the reader (retention) stops it
    too. Without ``live``, follow waits for the ``run_end`` however long
    it takes. Reads only: nothing is written, created or locked.

    In follow mode a line caught mid-write (readable but not yet
    newline-terminated) is pushed back and waited on, so one event never
    prints as two fragments.

    F-43 (m-16): what is PRINTED is control-sequence stripped — the stored
    line keeps its content (the file is evidence), but a screen clear, a
    cursor move or an OSC-8 link planted in session text never reaches the
    owner's terminal through this, the one reading seam.

    Raises:
        TraceError: A line is not valid UTF-8 (a corrupted trace).
    """
    path = Path(path)
    gone_polls = 0
    with path.open(encoding="utf-8") as handle:
        while True:
            position = handle.tell()
            try:
                line = handle.readline()
            except UnicodeDecodeError as exc:
                raise TraceError(f"{path.name}: trace is not valid UTF-8: {exc}") from exc
            if line and line.endswith("\n"):
                out(strip_control_sequences(line.rstrip("\n")))
                if follow and _is_run_end(line):
                    return
                continue
            if line and not follow:
                out(strip_control_sequences(line.rstrip("\n")))  # truncated tail
                continue
            if line:  # mid-write in follow mode: push back, wait for the rest
                handle.seek(position)
            if not follow:
                return
            if not path.exists():  # pruned (retention) under the reader
                print(f"factory trace: {path.name} was removed; stopping",
                      file=sys.stderr)
                return
            if live is not None and not live():
                gone_polls += 1
                if gone_polls >= GONE_POLLS:
                    print("factory trace: the run is no longer live and "
                          "wrote no run_end; stopping", file=sys.stderr)
                    return
            else:
                gone_polls = 0
            time.sleep(POLL_SEC)


def prune_traces(factory_root: Path | str, project: str, *,
                 days: int = RETENTION_DAYS,
                 now: float | None = None) -> list[Path]:
    """Delete this project's trace files older than the retention window.

    Traces outlive their runs (that is their point) but not the window:
    they hold capped snippets of everything a run read and wrote, which on
    a PII-bearing project is personal data. ``reclaim_runs`` calls this
    with the lease held — no run of the project is alive, so no trace is
    being appended to. Only run-id-shaped ``.jsonl`` files are touched.

    Returns:
        The files removed.
    """
    directory = Path(factory_root) / "trace" / project_component(project)
    if not directory.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - days * 86400.0
    removed: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".jsonl" or not RUN_ID_RE.fullmatch(entry.stem):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except OSError:
            continue
        removed.append(entry)
    return removed
