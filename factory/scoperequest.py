"""The request_scope tool (BUILD-ORDER F-65, AC-3).

The implementer's honest channel for "this task needs a file outside
its declared scope": one tool, ``request_scope(path, reason)``, an
in-process SDK MCP tool exactly like the vent tool (F-27) and the
importer's propose_task (F-57). The handler runs inside the SANDBOXED
runner and only appends to an in-memory
:class:`ScopeCollector`; the requests ride the
:class:`~factory.session.SessionRecord` out of the sandbox on the F-56
file transport, and the orchestrator — outside, unconfined — validates
them strictly (:func:`factory.session.parse_scope_requests`) and
escalates ``scope_change_needed`` the moment the session returns.

A request never widens anything by itself: nothing inside a run can
edit the queue (``.factory/**`` is protected structurally since F-63),
and the request's only effect is the escalation — the owner widens the
contract or declines. Trusting the surviving entries is therefore safe;
the TEXT is still attacker-controlled (a session writes its own
record), which is why the collector bounds and seam-scrubs here, the
parent re-validates there, and every display of a path or reason
passes :func:`factory.egress.escape_approval_text`.
"""

from __future__ import annotations

from typing import Any, Sequence

from claude_agent_sdk import create_sdk_mcp_server, tool

from factory.contract import scope_pattern_error
from factory.egress import check_secrets
from factory.session import (
    SCOPE_REQUEST_MAX,
    SCOPE_REQUEST_PATH_MAX,
    clean_request_text,
)

SCOPE_TOOL_NAME = "request_scope"
# What the CLI calls the tool (and so what ``allowed_tools`` must name):
# SDK MCP tools are exposed as ``mcp__<server>__<tool>``; the server is
# registered under SCOPE_TOOL_NAME too.
SCOPE_QUALIFIED_NAME = f"mcp__{SCOPE_TOOL_NAME}__{SCOPE_TOOL_NAME}"
REASON_CAP = 2000  # mirrors the vent text cap; the parent re-caps


class ScopeCollector:
    """In-memory scope-request sink living inside one implementer
    session's runner process.

    The handler's one effect is :meth:`record` — append one bounded,
    seam-scrubbed ``{path, reason}``. The sandboxed runner cannot reach
    the queue, the ledger or anything else; the orchestrator reads
    :attr:`requests` once the session returns and re-validates
    everything (the bounds here are the first, not the last, line).
    """

    def __init__(self, *, secrets: "Sequence[str]" = ()) -> None:
        # F-43: the literal secrets this session's runner holds (the
        # seat token), so a reason is seam-scrubbed BEFORE it crosses
        # the sandbox boundary. The guard is loud on a bare str.
        self.secrets = tuple(
            secret for secret in check_secrets(secrets) if secret
        )
        self.requests: list[dict[str, Any]] = []

    def record(self, path: str, reason: str) -> dict[str, Any]:
        """Validate, cap and redact one request; return the stored form.

        Raises:
            ValueError: The path is not a well-formed scope pattern
                (:func:`factory.contract.scope_pattern_error`) — the
                SDK's tool wrapper turns the raise into an isError
                result the model can read and retry, so an invalid call
                costs a retry, never a session.
        """
        problem = scope_pattern_error(path)
        if problem is not None:
            raise ValueError(f"scope request path {path!r} {problem}")
        if len(self.requests) >= SCOPE_REQUEST_MAX:
            raise ValueError(
                f"too many scope requests (bound {SCOPE_REQUEST_MAX}) — "
                "the owner reads at most that many; consolidate"
            )
        request = {
            "path": path[:SCOPE_REQUEST_PATH_MAX],
            "reason": clean_request_text(str(reason), self.secrets),
        }
        self.requests.append(request)
        return request


def make_scope_tool(collector: ScopeCollector) -> Any:
    """The in-process ``request_scope(path, reason)`` SDK tool.

    One per session, closed over that session's collector: the handler
    can do exactly one thing — append one bounded record — and has no
    path to any file, environment or network (the tool is the handler;
    the collector is a list).
    """

    @tool(
        SCOPE_TOOL_NAME,
        "Ask for a file outside this task's declared scope instead of "
        "editing it. The owner reads the path and reason and either "
        "widens the contract or declines — the request NEVER widens "
        "your scope itself, and editing the file anyway fails L0 and "
        "costs you the round. One call per path; reason is one line.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path or glob you need "
                    "(e.g. tests/util.py or src/gen/**).",
                },
                "reason": {
                    "type": "string",
                    "description": "One line: why the task cannot be "
                    "done without touching it.",
                },
            },
            "required": ["path", "reason"],
            "additionalProperties": False,
        },
    )
    async def _request_scope(args: dict[str, Any]) -> dict[str, Any]:
        request = collector.record(args["path"], args["reason"])
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"requested {request['path']} — the owner decides; "
                        "do not edit it this run"
                    ),
                }
            ]
        }

    return _request_scope


def scope_server(collector: ScopeCollector) -> Any:
    """The in-process MCP server carrying the request tool, for
    ``ClaudeAgentOptions.mcp_servers`` (the tool is additionally listed
    in the implementer seat's ``allowed_tools``)."""
    return create_sdk_mcp_server(
        name=SCOPE_TOOL_NAME, tools=[make_scope_tool(collector)]
    )
