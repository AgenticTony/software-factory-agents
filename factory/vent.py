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

from factory.progress import ProgressLog, safe_name
from factory.sentinels import redact_credentials
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
VENT_TRUNCATED = " …[vent truncated]"

# Vents older than this drop out of the weekly grouping (F-11's "the
# week's vents"; a week is seven days, not a calendar one).
VENT_WINDOW_DAYS = 7


class VentCollector:
    """In-memory vent sink living inside one session's runner process.

    The runner increments :attr:`turn` as assistant messages stream past,
    so a vent made mid-stream is stamped with the turn it happened in
    (best effort, single asyncio loop: the SDK invokes the in-process
    handler between stream messages). :func:`record` is the handler's one
    effect — it appends to :attr:`vents` and nothing else; the sandboxed
    runner cannot reach the real log, and the orchestrator flushes
    :attr:`vents` into it once the session returns.
    """

    def __init__(self, run_id: str, task_id: str, seat: str) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.seat = seat
        self.turn = 0
        self.vents: list[dict[str, Any]] = []

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
        text = str(text)
        if len(text) > VENT_TEXT_CAP:
            text = text[: VENT_TEXT_CAP - len(VENT_TRUNCATED)] + VENT_TRUNCATED
        # F-25's invariant, held for the same reason: untrusted session
        # text is redacted before it reaches orchestrator-owned storage
        # (a model echoing a credential into a complaint must not persist
        # it into the logs `factory status` later prints).
        text = redact_credentials(text)
        vent = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "seat": self.seat,
            "turn": self.turn,
            "category": category,
            "text": text,
        }
        self.vents.append(vent)
        return vent


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
    log: ProgressLog, vents: "Sequence[dict[str, Any]]", *, at: str | None = None
) -> list[dict[str, Any]]:
    """Append one progress-log entry per vent record, stamped ``at``.

    The orchestrator-side half of AC-2: called the moment a session
    returns (its record carried the vents out of the sandbox), so each
    tool call becomes exactly one log entry — ``kind: "vent"`` plus every
    field the record stamped — in the task's canonical log outside every
    clone. An empty ``vents`` is a no-op.
    """
    stamp = at or now_iso()
    stored = []
    for vent in vents:
        stored.append(
            log.append({"kind": "vent", **vent, "at": stamp})
        )
    return stored


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
        for entry in ProgressLog(path).entries():
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
