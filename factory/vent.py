"""The vent tool (BUILD-ORDER F-27).

Lovable's pattern, via Cursor: give agents a tool to complain, and the
complaints become the best harness bug tracker you have. Agents push
through friction that never shows up as a failure — a missing binary, a
flaky check, an ambiguous contract line (F-19 AC-4 reads exactly this
signal later); the vent records it at the moment it happens instead of
losing it inside a session that still "completed".

One tool, ``vent(category, text)``, five categories. The handler runs
in-process inside the SANDBOXED runner (F-06), where the real progress
log — outside the run dir — is not writable, so it only appends to an
in-memory :class:`VentCollector`. The vents ride the
:class:`~factory.session.SessionRecord` out of the sandbox on the same
stdout channel as the record itself, and the orchestrator — outside,
unconfined — flushes them into the task's progress log with
:func:`flush_vents` the moment the session that produced them returns
(:func:`~factory.progress.ProgressLog`, outside every clone, append-only
and hash-chained like every other entry).

Venting is telemetry, never control flow: nothing here touches a run's
outcome, attempt record or budget (F-27 AC-3), and vent entries are
deliberately excluded from the log's prompt rendering — they are for the
owner, `factory status` (F-11) and the F-19 review, not fresh-session
context.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from claude_agent_sdk import create_sdk_mcp_server, tool

from factory.egress import check_secrets, scrub_session_text
from factory.progress import ProgressLog, ProgressLogError, safe_name
from factory.state import now_iso

if TYPE_CHECKING:  # pragma: no cover — import cycle avoidance, typing only
    from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool

# The WO's five categories, the only valid ones (schema- AND
# handler-enforced; a malformed call comes back as an isError result the
# model can read and retry, never a protocol error that kills a session).
VENT_CATEGORIES = (
    "missing_tool",
    "unclear_contract",
    "flaky_check",
    "environment",
    "other",
)
# Text is length-capped: one vent is a note, not a transcript.
VENT_TEXT_CAP = 2000
VENT_TOOL_NAME = "vent"
# What the CLI calls the tool (and so what ``allowed_tools`` must name):
# SDK MCP tools are exposed as ``mcp__<server>__<tool>``, and the server is
# registered under VENT_TOOL_NAME too.
VENT_QUALIFIED_NAME = f"mcp__{VENT_TOOL_NAME}__{VENT_TOOL_NAME}"
VENT_TRUNCATED = " …[vent truncated]"

# Vents older than this drop out of the weekly grouping (F-11's "the
# week's vents"; a week is seven days, not a calendar one).
VENT_WINDOW_DAYS = 7


class VentCollector:
    """In-memory vent sink living inside one session's runner process.

    Turns: the runner feeds every assistant message to
    :meth:`observe`. One API turn streams as several messages (one per
    content block) sharing a ``message_id``, so a turn is a distinct id.
    The SDK may run the handler before the consumer has pulled the
    message carrying its ``tool_use`` block, so :meth:`record` stamps the
    turn seen so far and :meth:`observe` later re-stamps the vent with the
    turn of its own ``tool_use`` block (matched by its input). A session
    cut off before that block was consumed keeps the call-time turn.

    :meth:`record` is the handler's one effect — it appends to
    :attr:`vents` and nothing else; the sandboxed runner cannot reach the
    real log, and the orchestrator flushes :attr:`vents` into it once the
    session returns.
    """

    def __init__(self, run_id: str, task_id: str, seat: str,
                 *, secrets: "Sequence[str]" = ()) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.seat = seat
        # F-43: the literal secrets this session's runner holds (the seat
        # token), so a vent is seam-scrubbed BEFORE it crosses the sandbox
        # boundary — the orchestrator scrubs again at the flush.
        # Round 2: the one guard — a bare str must be loud here too.
        self.secrets = tuple(
            secret for secret in check_secrets(secrets) if secret
        )
        self.turn = 0
        self.vents: list[dict[str, Any]] = []
        self._seen_ids: set[str] = set()
        # (category, raw text) per vent, parallel to ``vents`` — the key a
        # tool_use block's input is matched on; never leaves the runner.
        self._keys: list[tuple[str, str]] = []
        self._matched: list[bool] = []
        # tool_use inputs seen in the stream, not yet matched to a vent.
        self._pending: list[tuple[int, tuple[str, str]]] = []

    def observe(self, message: Any) -> None:
        """Count turns and match vent ``tool_use`` blocks to their vents.

        Subagent messages (``parent_tool_use_id`` set) do not advance the
        session's turn; a vent a subagent makes takes the current turn.
        """
        if getattr(message, "parent_tool_use_id", None) is None:
            message_id = getattr(message, "message_id", None)
            if message_id is None or message_id not in self._seen_ids:
                self.turn += 1
                if message_id is not None:
                    self._seen_ids.add(message_id)
        for block in getattr(message, "content", None) or []:
            if getattr(block, "name", None) != VENT_QUALIFIED_NAME:
                continue
            raw = getattr(block, "input", None) or {}
            key = (str(raw.get("category")), str(raw.get("text")))
            self._pending.append((self.turn, key))
        self._match()

    def _match(self) -> None:
        for index, key in enumerate(self._keys):
            if self._matched[index]:
                continue
            for slot, (turn, pending_key) in enumerate(self._pending):
                if pending_key == key:
                    self.vents[index]["turn"] = turn
                    self._matched[index] = True
                    del self._pending[slot]
                    break

    def record(self, category: str, text: str) -> dict[str, Any]:
        """Validate, cap and redact one vent; return the stored record.

        Raises:
            ValueError: ``category`` is not one of :data:`VENT_CATEGORIES`.
                The SDK's tool wrapper turns the raise into an isError
                result the model can read, so an invalid call costs a
                retry, never a session.
        """
        if category not in VENT_CATEGORIES:
            raise ValueError(
                f"unknown vent category {category!r}: expected one of "
                f"{list(VENT_CATEGORIES)}"
            )
        raw = str(text)
        vent = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "seat": self.seat,
            "turn": self.turn,
            "category": category,
            "text": clean_text(raw, self.secrets),
            # When the call was made, not when the session returned.
            "at": now_iso(),
        }
        self.vents.append(vent)
        self._keys.append((category, raw))
        self._matched.append(False)
        self._match()  # the tool_use block may already have streamed past
        return vent


def clean_text(text: str, secrets: "Sequence[str]" = ()) -> str:
    """The F-43 egress seam, then cap (review finding 2: capping first
    could cut a credential in half and leave a prefix no pattern matches).

    F-25's invariant, held for the same reason: untrusted session text is
    redacted before it reaches orchestrator-owned storage — a model echoing
    a credential into a complaint must not persist it into the logs
    `factory status` later prints. F-43 (M-8) widened the redaction to the
    full seam: the caller's literal secrets (the seat token — pattern
    redaction alone never caught a bare ``ANTHROPIC_AUTH_TOKEN=<jwt>``
    echo) and terminal control sequences (m-16: `factory status` prints
    this text to the owner's terminal). A pure forwarder: the guard lives
    in :func:`factory.egress.scrub_session_text` (round 3, finding 4) —
    this always forwards, so it inherits.
    """
    text = scrub_session_text(text, secrets)
    if len(text) > VENT_TEXT_CAP:
        text = text[: VENT_TEXT_CAP - len(VENT_TRUNCATED)] + VENT_TRUNCATED
    return text


def make_vent_tool(collector: VentCollector) -> "SdkMcpTool[Any]":
    """The in-process ``vent(category, text)`` SDK tool (F-27 AC-1).

    One per session, closed over that session's collector: the handler can
    do exactly one thing — append one record — and has no path to any
    file, environment or network (the tool is the handler; the collector
    is a list).
    """

    @tool(
        VENT_TOOL_NAME,
        "Report harness friction as it happens: a missing tool or binary, an "
        "unclear contract, a flaky check, a broken environment. Vents never "
        "change your run's outcome or budget — they tell the factory's owner "
        "what to fix. vent(category, text); category is one of "
        "missing_tool | unclear_contract | flaky_check | environment | other.",
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(VENT_CATEGORIES),
                    "description": "What kind of friction this is.",
                },
                "text": {
                    "type": "string",
                    "description": "What happened, where, and what you did "
                    "instead. One note, not a transcript.",
                },
            },
            "required": ["category", "text"],
            "additionalProperties": False,
        },
    )
    async def _vent(args: dict[str, Any]) -> dict[str, Any]:
        vent = collector.record(args["category"], args["text"])
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"recorded ({vent['category']}, turn {vent['turn']})",
                }
            ]
        }

    return _vent


def vent_server(collector: VentCollector) -> "McpSdkServerConfig":
    """The in-process MCP server carrying the vent tool, for
    ``ClaudeAgentOptions.mcp_servers`` (the tool is additionally listed in
    ``allowed_tools`` per the work order)."""
    return create_sdk_mcp_server(
        name=VENT_TOOL_NAME, tools=[make_vent_tool(collector)]
    )


def flush_vents(
    log: ProgressLog,
    vents: "Sequence[Any]",
    *,
    at: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    seat: str | None = None,
    secrets: "Sequence[str]" = (),
) -> list[dict[str, Any]]:
    """Append one progress-log entry per valid vent, as one batch.

    The orchestrator-side half of AC-2: called the moment a session
    returns, so each tool call becomes exactly one log entry in the task's
    canonical log outside every clone. An empty ``vents`` is a no-op.

    ``vents`` crossed the sandbox boundary on the runner's stdout, so they
    are untrusted (review finding 1): every entry is rebuilt from the
    whitelisted fields only — ``kind`` is always ``"vent"``, and no
    ``n``/``prev``/``finding_keys``/``findings`` can ride in. A vent that
    is not a mapping, has a category outside :data:`VENT_CATEGORIES` or a
    non-string text is dropped; text passes the F-43 egress seam (the
    caller's ``secrets`` — the orchestrator hands the seat token — then
    the credential shapes, then the control-sequence strip) and is capped
    again here; ``run_id``/``task_id``/``seat``, when given, override what
    the record claims (the orchestrator knows which session it ran).

    ``at`` is the flush time: the fallback stamp for a vent whose own
    ``at`` (stamped at call time) is missing, unparsable or later than the
    flush — a vent cannot be dated into the future to linger in the
    weekly window.
    """
    secrets = check_secrets(secrets)
    stamp = at or now_iso()
    ceiling = _parse_at(stamp)
    entries = []
    for vent in vents:
        if not isinstance(vent, dict):
            continue
        category, text = vent.get("category"), vent.get("text")
        if category not in VENT_CATEGORIES or not isinstance(text, str):
            continue
        turn = vent.get("turn")
        made = vent.get("at")
        made_at = _parse_at(made) if isinstance(made, str) else None
        keep_made = made_at is not None and (ceiling is None or made_at <= ceiling)
        entries.append({
            "kind": "vent",
            "run_id": run_id if run_id is not None else str(vent.get("run_id", "")),
            "task_id": task_id if task_id is not None else str(vent.get("task_id", "")),
            "seat": seat if seat is not None else str(vent.get("seat", "")),
            "turn": turn if isinstance(turn, int) and not isinstance(turn, bool)
            and turn >= 0 else 0,
            "category": category,
            "text": clean_text(text, secrets),
            "at": made if keep_made else stamp,
        })
    return log.append_many(entries)


def _parse_at(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def weeks_vents(
    factory_root: Path | str,
    project: str,
    *,
    now: datetime | None = None,
    unreadable: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """The project's vents from the trailing seven days, grouped by
    category — the one reading seam `factory status` (F-11) lists and the
    F-19 review reads (both call this; no second implementation).

    Every category key is always present (empty groups included), so a
    status page renders a stable shape. Each item is the stored log entry
    minus the chain fields (``n``/``prev``): run id, task id, seat, turn,
    category, redacted text and the ``at`` stamp. Vents without a parsable
    ``at`` stay (a malformed stamp must not hide a vent from the owner);
    entries that are not vents are ignored. Read-only: nothing under
    ``factory_root`` is written.

    A task log that fails to parse or verify is skipped, not raised (one
    corrupt file must not hide every other task's vents); its path is
    appended to ``unreadable`` when the caller passes a list, so the status
    page can say so.
    """
    root = Path(factory_root) / "progress" / safe_name(project)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    horizon = moment - timedelta(days=VENT_WINDOW_DAYS)
    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in VENT_CATEGORIES}
    if not root.is_dir():
        return grouped
    for path in sorted(root.glob("*.jsonl")):
        try:
            entries = ProgressLog(path).entries()
        except (ProgressLogError, OSError, UnicodeDecodeError):
            if unreadable is not None:
                unreadable.append(str(path))
            continue
        for entry in entries:
            if entry.get("kind") != "vent":
                continue
            if entry.get("category") not in grouped:
                continue  # not one of ours; status never renders it
            stamped = _parse_at(str(entry.get("at", "")))
            if stamped is not None and stamped < horizon:
                continue
            grouped[entry["category"]].append(
                {k: v for k, v in entry.items() if k not in ("n", "prev", "kind")}
            )
    return grouped
