"""The importer (F-12): planning docs -> a proposed ``tasks.json``.

``factory import`` runs an importer session over a project's planning
documents and proposes a queue. Four rules shape everything here:

* **The output validates against the schema, or the import is refused**
  (AC-1). The merged document goes through the F-02 validator before it
  is shown, and through :meth:`TaskQueue.save` — which validates again —
  when it is written. Invalid state can never reach disk.
* **Every imported task is ``draft``** (AC-2). A proposal that carries a
  status other than ``draft``, an approval field, ``attempts``,
  ``links`` or ``out_of_scope`` is refused, naming the task: those are
  written by you and the orchestrator, never by the importer.
* **Nothing is written without confirmation.** :func:`run_import`
  proposes; it writes only through :func:`apply_import`, on an explicit
  confirm, and then via :class:`~factory.state.TaskQueue` — so the
  writer lock and the append-only attempts rule hold on this path too.
  The plan remembers the queue it was built from: if the queue changed
  while the confirm prompt waited (an approval, a status move), the
  write is refused instead of reverting that change.
* **Re-running with unchanged documents proposes no diff for existing
  tasks** (AC-3): the merge compares the proposed spec fields with the
  stored ones and touches nothing else — status, attempts, approval
  fields and links are preserved as they are, and so is the queue's own
  key order. A spec field the proposal leaves out keeps its stored value
  (defaults apply to new tasks only; an explicit value, ``[]`` included,
  is a proposal). A task the proposal does not mention is carried over
  and *listed* (kind ``missing``), so a silently dropped work order is
  visible instead of quiet.

The out-of-scope register (TASK-CONTRACT §5, ``.factory/out-of-scope/``)
is read and shown to the importer session. A proposed task that resembles
a record carries the record's path in its proposal (``out_of_scope_match``)
and is listed in the summary with that path (AC-4). Matching is by
concept — the session's job, never a keyword matcher here — and a match
is only ever *listed* for you to confirm: the task is still proposed,
still ``draft``, never dropped or rejected on its own.

The queue's project name is known before the session runs (the existing
queue's, or the profile's for a fresh project): the prompt states it
literally in the output contract — the session proposes for *this*
queue, never a spelling guessed from the repository directory. And a
real session's output is never thrown away by a refusal: any
``ImportRefused`` after the session returns (parse, plan, or an apply
the queue's change or a failed write refuses) keeps the raw output
(credential-redacted, mode 0600) under ``<factory-root>/import/`` and
names the file plus the full re-review command, so the fix is an edit
and one paste — not another session. Keeping keys on the session's
cost, not on the factory root: the real session always keeps, an
injected stub never does (it re-runs free) unless its caller passes
``keep_refused`` (an eval/replay harness replaying a real run's
output).

The session sits behind one injected callable (the F-06 pattern):
``session(prompt) -> text``. Tests pass a stub returning a fixed
proposal; the default, :func:`sdk_import_session`, runs the reviewer
seat through :func:`factory.session.run_session_sandboxed` — inside srt,
in a disposable clone of the repo's committed HEAD, with the read-only
reviewer guard (read-only sandbox, no Write/Edit, workspace integrity
check) — and returns its final message. It never runs in-process on the
live checkout (DESIGN D3).

The session's budget scales with the work — the number of planning
docs the prompt lists (:func:`scaled_import_budget`; ``--max-turns`` /
``--timeout-min`` override) — and a session that does not complete has
its scratch's last ``$TMPDIR/proposal.json`` copied out before the run
is cleaned up (:func:`_abort_refusal`): three full jobfinderos sessions
were lost to fixed budgets and cleanup-deleted proposals before either
existed.

F-49 hardening, from the 2026-11-05 review (m-4, m-6, m-7, I-18):

* the planning-doc listing ANNOUNCES its cap — ``+N more`` in the
  prompt, the dropped docs named in the import summary the owner
  confirms (:class:`PlanningDocs`), and the budget scales with the docs
  actually listed;
* the fresh-queue write takes :func:`factory.state.queue_writer_lock`
  across its no-queue check and its write, so two concurrent fresh
  imports serialise (one writes, one refuses) instead of racing;
* EVERY parse/validate failure the session output can provoke —
  RecursionError from deep nesting, ValueError from a huge integer
  literal, a document past :data:`MAX_PROPOSAL_DEPTH` — is a refusal
  through the keep-refused path, never a crash past it;
* the proposal is judged by :func:`plan_side_proposal_schema` — the
  same shapes the session self-checks — so a proposal without a
  non-empty ``project`` key, an extra key, or a handed ``status`` is
  refused before planning.

F-67 — the four gaps the F-13 pilot's fresh import exposed (each
defeated parent-side; the session only ever proposes):

* **v0 gates are forced, never trusted** (:func:`_normalise_gates`):
  every task the importer writes — new and merged — lands
  ``merge``/``deploy`` ``human`` and ``spec_approval`` true on
  :data:`factory.state.ELIGIBLE_KINDS` kinds, whatever the proposal
  said; every override is listed in the summary before the diff, and
  :func:`factory.approve.check_v0_gates` refuses a non-human gate
  wherever the queue came from;
* **every import session is on record**
  (:func:`_write_session_record`): the importer session AND each repair
  session write a provider-stamped (the PARENT's resolution), scrubbed,
  0600 record under ``<factory-root>/import/`` on every exit path —
  accepted, refused, aborted — and ``factory status`` renders them;
* **possibly shipped work is flagged, never decided**
  (:func:`_validate_possibly_shipped`): the proposal's
  ``possibly_shipped`` list is validated (every entry names a proposed
  task; evidence is repo-relative), rendered FIRST in the summary, and
  changes nothing — the owner decides;
* **criteria the docs still carry are never silently dropped**
  (:func:`_account_reference`): the repo's own ``.factory/tasks.json``,
  read as PLAIN JSON (it may predate the current schema), is accounted
  against — every reference task and criterion mapped
  (``predecessors`` + a criterion's ``from``), re-proposed, or dropped
  against a real line of a kept planning doc; a drop whose criterion
  text the docs still carry blocks :func:`apply_import` until the owner
  accepts it, per drop. The proposal-only fields never reach the queue.

"""

from __future__ import annotations

import contextlib
import copy
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

from claude_agent_sdk import create_sdk_mcp_server, tool

from factory.approvals import require_profile_approval
from factory.contract import (
    SCHEMA_DIR,
    ContractError,
    budget_floor,
    canonical_json,
    iter_schema_violations,
    load_profile,
    load_tasks,
    scope_pattern_error,
    validate_queue,
)
from factory.egress import (
    check_secrets,
    escape_approval_text,
    scrub_session_text,
    scrub_session_value,
)
from factory.gates import (
    GateError,
    contract_test_files,
    default_branch_files,
    scope_risk_flags,
)
from factory.provider import (
    ProviderError,
    keychain_service,
    provider_token,
    resolve_provider,
    run_secrets,
)
from factory.runs import (
    FACTORY_ROOT_DEFAULT,
    GIT_HARDENING,
    RunDir,
    RunError,
    cleanup_run,
    create_run,
    new_run_id,
    project_component,
    sandbox_settings_path,
)
from factory.sandbox import SandboxError
from factory.session import (
    SessionConfigError,
    SessionRecord,
    SessionRecordError,
    run_session_sandboxed,
)
from factory.state import (
    ELIGIBLE_KINDS,
    RunInProgressError,
    TaskQueue,
    _atomic_write_json,
    contract_change_action,
    now_iso,
    queue_writer_lock,
)
from factory.trace import TraceWriter

OUT_OF_SCOPE_DIR = ".factory/out-of-scope"
DEFAULT_DOCS_DIR = "docs"
DRAFT = "draft"

# Fields a proposal may set on a task — the spec. Everything else in the
# task record belongs to you (approval, out_of_scope) or the orchestrator
# (status moves, attempts, links).
SPEC_FIELDS = (
    "title",
    "source",
    "kind",
    "priority",
    "depends_on",
    "risk",
    "gates",
    "scope",
    "acceptance",
    "budget",
    "dependencies",
    "owner_steps",
    "slices",
)
# A proposal carrying any of these is refused, naming the task (AC-2).
FORBIDDEN_FIELDS = (
    "acceptance_hash",
    "approved_at",
    "approved_by",
    "attempts",
    "links",
    "out_of_scope",
)

DEFAULT_MAX_TURNS = 30
DEFAULT_TIMEOUT_MIN = 20.0
# The ceilings for an explicit --max-turns / --timeout-min and for the
# scaled default alike (one bound, so a flag can never ask for more than
# the formula could): 200 loop turns is ~35 min of model loop at the
# observed pace, and 120 min is the outer wall clock of a whole import
# — past either, an import is wedged, not working, and the salvage
# copy (below) is the recovery, not more budget.
TURN_CEILING = 200
TIMEOUT_MIN_CEILING = 120.0
# Long planning-doc trees are listed, not inlined; the session reads the
# files it needs from the repo (it runs with read tools).
MAX_DOC_FILES = 200
# A refusal lists every violation, capped: a 30-task proposal can carry
# hundreds of the same three shape errors, and one-per-round (the
# 2026-11-05 jobfinderos import) makes the fix cost a review round per
# defect class.
MAX_SHOWN_VIOLATIONS = 50
TASKS_SCHEMA = SCHEMA_DIR / "tasks.schema.json"
# The staged proposal schema's file name: the prompt names it under the
# session's writable scratch ($TMPDIR — the target repo has no schema/
# dir of its own), run_session_sandboxed stages it there before the
# session starts, and sdk_import_session derives its content. One name,
# three seams.
PROPOSAL_SCHEMA_NAME = "proposal.schema.json"
PROPOSAL_OUTPUT_NAME = "proposal.json"
# The refused output as staged into the repair session's scratch (F-57
# AC-2): the repair reads exactly what was judged, never the docs.
REFUSED_OUTPUT_NAME = "refused.json"

# ------------------------------------------------------ F-57: propose_task --

# The importer session's per-task tool (AC-1). One task per call,
# validated AS IT IS WRITTEN against the same staged proposal schema the
# session self-checks with — the 2026-11-05 import broke on every shape
# at once because the only check came after a 47 KB answer.
PROPOSE_TOOL_NAME = "propose_task"
# What the CLI calls the tool (and so what ``allowed_tools`` names): SDK
# MCP tools are exposed as ``mcp__<server>__<tool>``, the server named
# for the tool like vent's.
PROPOSE_QUALIFIED_NAME = f"mcp__{PROPOSE_TOOL_NAME}__{PROPOSE_TOOL_NAME}"
# Bounds on model-controlled input (the session is untrusted): a call
# past the cap is a refusal message plus a recorded fact, never a crash
# and never a lost accepted set.
PROPOSE_MAX_CALLS = 1000
PROPOSE_MAX_TASKS = 200
PROPOSE_TASK_MAX_CHARS = 200_000

# ------------------------------------------------------- F-57: the repair --

# ONE bounded repair session per refused import (AC-2): a quarter of the
# import's own budget, floored so a small import still gets a real
# repair, and always under validate_budget's ceilings by construction
# (it is a fraction of a budget those ceilings already bound).
REPAIR_BUDGET_FRACTION = 4
REPAIR_TURN_FLOOR = 8
REPAIR_TIMEOUT_FLOOR = 5.0

# ----------------------------------------------------- F-67: gates/records --

# The possibly-shipped and dropped-criteria bounds (both lists are
# session-written: bounded at the schema AND re-checked parent-side,
# where the count is judged against the proposal's own task count).
MAX_SHIPPED_ENTRIES = 200
MAX_DROPPED_ENTRIES = 1000
MAX_PREDECESSORS = 50
# The Rewritten-criteria section's render cap (R1-1) — the count is
# stated, never hidden.
MAX_SHOWN_REWRITTEN = 20
# The import session-record files' suffix under <factory-root>/import/
# (beside the kept refused outputs, same permissions and naming rule).
RECORD_SUFFIX = ".record.json"
# The reference queue (the repo's own .factory/tasks.json) is read as
# PLAIN JSON — it may predate the current schema (the F-13 pilot's
# landed queue fails F-64's budget bounds 22 times over) — but it is
# still bounded: a huge or deeply nested file refuses the import
# cleanly, and the read never writes a byte back.
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_DOC_TEXT_BYTES = 10 * 1024 * 1024
# A drop is "backed" when its cited doc line carries one of these
# markers — the repo's own records mark finished work this way (work-
# order status lines, executed stamps, checkbox ticks). Narrow and
# documented: the flag's false-negative risks are stated in
# _account_reference's docstring.
_DONE_MARKER = re.compile(
    r"\[[xX ]\]|✓|✔|✅|\[[Dd]one\]|\b[Dd]one\b|\b[Ss]hipped\b"
    r"|\b[Ss]uperseded\b|\b[Ee]xecuted\b|\b[Cc]ompleted\b|\b[Ll]anded\b"
)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ImportRefused(ContractError):
    """The import was refused. The message is written for humans.

    ``saved_path`` (F-67): the kept output's file name when this
    refusal rode out through :func:`keep_refused_import_output` — the
    session-record write names it without re-parsing the message.

    Subclasses :class:`~factory.contract.ContractError` so every caller
    that already refuses on invalid contract state refuses on a broken
    import too."""

    saved_path: str | None = None


# --------------------------------------------------------------- proposal --

# A proposal's own nesting ceiling. The deepest legitimate proposal is
# ~6 levels (queue -> tasks -> task -> acceptance -> check); anything
# past this is hostile or broken, and every downstream consumer of a
# parsed proposal RECURSES over its structure (the egress scrub, the
# schema check, canonical_json, the queue write), so the parse boundary
# bounds what they can be fed — never a RecursionError downstream.
MAX_PROPOSAL_DEPTH = 64


def _max_nesting(value: Any) -> int:
    """Deepest list/dict nesting in a parsed value, computed ITERATIVELY.

    A recursive depth walk could itself be the RecursionError this check
    exists to prevent, so it carries its own stack.
    """
    depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, level = stack.pop()
        if level > depth:
            depth = level
        if isinstance(node, dict):
            stack.extend((item, level + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, level + 1) for item in node)
    return depth


def parse_proposal(text: str) -> dict:
    """Parse the importer session's output into a proposal document.

    A fenced code block is stripped first (sessions habitually wrap
    their JSON in one); what remains must be a JSON object with a
    ``tasks`` list, nested no deeper than :data:`MAX_PROPOSAL_DEPTH`.

    F-49 AC-5 (m-7): EVERY failure the text can provoke in the parse is
    a refusal, never an escape past the keep-refused path —

    * ``RecursionError`` (json's recursive descent on deeply nested
      arrays/objects),
    * ``ValueError`` from an integer literal past the 3.11+ int/str
      conversion limit (a plain ValueError, not a JSONDecodeError),
    * a document that parses but nests beyond the ceiling — bounded
      here so no downstream consumer (scrub, schema, the write) can be
      crashed by structure the parse accepted.

    Raises:
        ImportRefused: The output is not JSON, not an object, has no
            ``tasks`` list, or nests too deeply — the proposal never
            reaches the queue, and the caller keeps the raw output.
    """
    text = text.strip()
    match = _FENCE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportRefused(
            f"the importer session's output is not valid JSON: {exc}"
        ) from exc
    except RecursionError as exc:
        raise ImportRefused(
            "the importer session's output is nested too deeply to parse "
            "(RecursionError) — no proposal can be read from it"
        ) from exc
    except ValueError as exc:
        # Not a JSONDecodeError: Python 3.11+ refuses integer literals
        # over the int/str conversion limit (4300 digits) like this.
        raise ImportRefused(
            f"the importer session's output is not valid JSON "
            f"(hostile or broken value: {exc})"
        ) from exc
    depth = _max_nesting(data)
    if depth > MAX_PROPOSAL_DEPTH:
        raise ImportRefused(
            f"the importer session's output nests {depth} levels deep; a "
            f"proposal is at most {MAX_PROPOSAL_DEPTH} — deeply nested "
            "output is refused at the parse boundary so no later consumer "
            "of the proposal can be crashed by it"
        )
    if not isinstance(data, dict):
        raise ImportRefused(
            "the importer session's output must be a JSON object with a "
            f"'tasks' list, got {type(data).__name__}"
        )
    if not isinstance(data.get("tasks"), list):
        raise ImportRefused(
            "the importer session's output has no 'tasks' list; the "
            "proposal shape is {\"project\": …, \"tasks\": […]}"
        )
    return data


# --------------------------------------------------- out-of-scope register --


def read_out_of_scope(repo: Path | str) -> list[dict[str, str]]:
    """The out-of-scope register (TASK-CONTRACT §5): one record per
    rejected concept, from ``<repo>/.factory/out-of-scope/*.md``.

    Args:
        repo: The project repo root.

    Returns:
        ``[{"path": "<repo-relative>", "text": <file text>}]``, sorted by
        path. A missing register reads as empty — a project without one
        imports fine; an unreadable record is refused, never skipped
        (a silently dropped record would hide a rejected concept from
        the session that needs it).
    """
    register = Path(repo) / OUT_OF_SCOPE_DIR
    if not register.is_dir():
        return []
    records: list[dict[str, str]] = []
    for path in sorted(register.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ImportRefused(
                f"{OUT_OF_SCOPE_DIR}/{path.name}: cannot read the "
                f"out-of-scope record: {exc}"
            ) from exc
        records.append({"path": f"{OUT_OF_SCOPE_DIR}/{path.name}", "text": text})
    return records


# ------------------------------------------------------------------ prompt --


def _load_tasks_schema() -> dict:
    """The parsed ``schema/tasks.schema.json``.

    The prompt's output contract is *generated* from this (see
    :func:`_schema_contract_lines`), so the contract and the validator
    can never drift apart: a schema edit changes the prompt on the next
    build, and a hand-copied field list that went stale (the 2026-11-05
    jobfinderos import named field names but never their shapes) cannot
    exist.
    """
    return json.loads(TASKS_SCHEMA.read_text(encoding="utf-8"))


def build_proposal_schema(schema: dict) -> dict:
    """Derive the staged self-check schema from the queue schema.

    The importer session runs in a clone of the *target* repo, which has
    no ``schema/`` dir — and the queue schema itself would refuse a
    VALID proposal (it requires top-level ``version`` and per-task
    ``status``, both written by the importer, never proposed). This
    narrows the loaded queue schema to exactly what a proposal may
    carry, so the session's self-check judges the same shapes
    :func:`plan_import` will judge:

    * top level: ``{"project", "tasks"}`` only — no ``version``,
      nothing else;
    * per task: ``id`` and the spec fields plus ``out_of_scope_match``;
      ``status`` and every forbidden field (:data:`FORBIDDEN_FIELDS`)
      are absent properties, so carrying one is a shape error the check
      names — not a ~31-violation wall about fields the prompt forbids
      setting;
    * ``id`` gains ``minLength: 1`` (an unreadable id is refused by
      :func:`plan_import` too);
    * every enum, pattern, bound and object shape is the queue schema's
      own — derived, never re-typed;
    * of the task's conditional rules, the risk⇒human-merge rule rides
      along (:func:`plan_import` enforces it on the merged document
      too); the rules whose ``if`` clause *reads* status do not. They
      cannot fire honestly here (status is not a proposal field), and
      copying them verbatim backfires: their ``if`` clauses lack
      ``required: [status]``, so on a status-less task they pass
      *vacuously* and their ``then`` fires — demanding
      ``acceptance_hash``/``approved_at`` and rebuilding, inside the
      self-check, the very ~31-violation wall of required fields the
      prompt forbids setting. "Reads status" is matched on the ``if``
      clause's structure — ``status`` as an object key (a property
      read) or an exact ``required`` entry — never on rule text: a rule
      that only mentions status in a ``$comment`` or constrains it in
      its ``then`` (harmless here: ``then`` filters values of present
      keys, and status can never be present) survives.

    Agreement (:func:`plan_import` versus this schema): a proposal that
    passes the staged check is never refused for *shape*. What the
    staged check cannot see, and ``plan_import`` still refuses — the
    semantic-only checks: the project name spelled exactly as the prompt
    states, unique task ids, non-blank task ids, and
    ``out_of_scope_match`` naming a real register record.
    """
    proposal = copy.deepcopy(schema)
    proposal["title"] = "Importer proposal (factory import self-check)"
    proposal["required"] = ["project", "tasks"]
    proposal["additionalProperties"] = False
    proposal["properties"] = {
        # minLength 1 (F-49 AC-6): the queue schema only says "string" —
        # an empty project name is not a name, and this is the one schema
        # that ever sees the proposal's own project value.
        "project": {**proposal["properties"]["project"], "minLength": 1},
        "tasks": proposal["properties"]["tasks"],
        # F-67 AC-3: work that may already be shipped, flagged for the
        # owner — never decided. Bounded like every session-written list.
        "possibly_shipped": {
            "type": "array",
            "maxItems": MAX_SHIPPED_ENTRIES,
            "items": {
                "type": "object",
                "required": ["task_id", "evidence", "reason"],
                "additionalProperties": False,
                "properties": {
                    "task_id": {"type": "string", "minLength": 1,
                                "maxLength": 200},
                    "evidence": {"type": "string", "minLength": 1,
                                 "maxLength": 400},
                    "reason": {"type": "string", "minLength": 1,
                               "maxLength": 500},
                },
            },
        },
        # F-67 AC-4: the criteria accounting's explicit-drops list, every
        # entry backed by a line of a kept planning doc.
        "dropped_criteria": {
            "type": "array",
            "maxItems": MAX_DROPPED_ENTRIES,
            "items": {
                "type": "object",
                "required": ["ref_task", "doc", "line", "reason"],
                "additionalProperties": False,
                "properties": {
                    "ref_task": {"type": "string", "minLength": 1,
                                 "maxLength": 200},
                    "ref_ac": {"type": "string",
                               "pattern": "^AC-[0-9]+$"},
                    "doc": {"type": "string", "minLength": 1,
                            "maxLength": 400},
                    "line": {"type": "integer", "minimum": 1},
                    "reason": {"type": "string", "minLength": 1,
                               "maxLength": 500},
                },
            },
        },
    }
    task = proposal["$defs"]["task"]
    keep = {"id", *SPEC_FIELDS, "out_of_scope_match", "predecessors"}
    task["properties"] = {
        name: prop for name, prop in task["properties"].items() if name in keep
    }
    # F-67 AC-4: the reference ids a proposed task replaces (a merge or
    # a split names several; several tasks may share one). Proposal-only:
    # consumed by the accounting, stripped before the queue write.
    task["properties"]["predecessors"] = {
        "type": "array",
        "maxItems": MAX_PREDECESSORS,
        "items": {"type": "string", "minLength": 1, "maxLength": 200},
    }
    # F-67 AC-4: per-criterion provenance — "<ref task id>/<ref AC id>"
    # naming the reference criterion this one carries (a split maps each
    # reference criterion into exactly one part). Proposal-only, like
    # predecessors: stripped before the queue write.
    acceptance = proposal["$defs"].get("acceptance_item")
    if isinstance(acceptance, dict):
        acceptance.setdefault("properties", {})["from"] = {
            "type": "string",
            "pattern": "^[^/\\s]+/AC-[0-9]+$",
            "maxLength": 300,
        }
    task["properties"]["out_of_scope_match"] = {
        "type": "string",
        "description": (
            "The out-of-scope record this task resembles — the register "
            "path; the owner confirms the match."
        ),
    }
    task["properties"]["id"] = {
        **task["properties"]["id"], "minLength": 1,
    }
    task["required"] = [
        name for name in task["required"]
        if name != "status" and name not in FORBIDDEN_FIELDS
    ]
    task["additionalProperties"] = False
    task["allOf"] = [
        rule for rule in task.get("allOf", [])
        if not _if_reads_status(rule)
    ]
    return proposal


def _if_reads_status(rule: dict) -> bool:
    """Does the rule's ``if`` clause read the task's ``status``?

    Matched structurally, never by text: ``status`` as an object key
    anywhere in the ``if`` (a property read, nested in ``properties``,
    ``not``, combinators) or as an exact ``required`` list entry. A
    ``$comment``/``description`` string that merely says "status", or a
    rule that only constrains status in its ``then``, does not read
    status in its ``if`` and survives — dropping on substring would
    silently lose such a rule (round-3 review nit).
    """

    def reads(node: Any) -> bool:
        if isinstance(node, dict):
            return "status" in node or any(reads(child) for child in node.values())
        if isinstance(node, list):
            return any(reads(item) for item in node)
        return node == "status"

    return reads(rule.get("if"))


# The task fields a proposal may omit because _new_task defaults them
# for a fresh task (F-12): the plan-side schema check drops exactly
# these from the task's required list, so the defaults behaviour is
# unchanged while every OTHER shape the session self-checks, the factory
# side checks too (F-49 AC-6).
_NEW_TASK_DEFAULT_FIELDS = ("depends_on", "risk", "gates")


def plan_side_proposal_schema(schema: dict) -> dict:
    """The proposal schema :func:`plan_import` judges the proposal by
    (F-49 AC-6, I-18): :func:`build_proposal_schema`, minus the three
    task fields a proposal may leave to the F-12 defaults.

    One schema, both sides: the session self-checks its output against
    ``build_proposal_schema`` (staged in its scratch) and the factory
    re-judges the same shapes here — a missing or empty ``project``, an
    extra top-level key, a task key outside the spec fields, a handed
    ``status`` — so a proposal that skips naming its project is refused
    BEFORE planning, by the schema, not by a second hand-written check.
    The staged check stays the stricter of the two (it demands the
    defaults-able trio too); a proposal that passes it always passes
    here, which keeps the agreement one-directional exactly as before.
    """
    derived = build_proposal_schema(schema)
    task = derived["$defs"]["task"]
    task["required"] = [
        name for name in task["required"]
        if name not in _NEW_TASK_DEFAULT_FIELDS
    ]
    return derived


def _resolve_ref(prop: dict, schema: dict) -> dict:
    """Resolve a local ``$ref`` (``#/$defs/<name>``) to its definition."""
    if "$ref" not in prop:
        return prop
    target: Any = schema
    for part in prop["$ref"].lstrip("#").lstrip("/").split("/"):
        target = target[part]
    return target


def _describe_prop(prop: dict, schema: dict) -> str:
    """One schema property as prompt prose: enums, patterns, integer
    bounds, arrays and object shapes (required keys unmarked, optional
    keys ``?``-suffixed, ``additionalProperties: false`` stated)."""
    prop = _resolve_ref(prop, schema)
    if "enum" in prop:
        return "one of " + " | ".join(str(value) for value in prop["enum"])
    kind = prop.get("type")
    if kind == "boolean":
        return "true | false"
    if kind == "integer":
        low, high = prop.get("minimum"), prop.get("maximum")
        if low is not None and high is not None:
            return f"integer {low}–{high}"
        if low is not None:
            return f"integer >= {low}"
        return "integer"
    if kind == "string":
        if "pattern" in prop:
            return f"string matching {prop['pattern']}"
        return "string"
    if kind == "array":
        items = _describe_prop(prop["items"], schema)
        unique = " (unique items)" if prop.get("uniqueItems") else ""
        return f"array of {items}{unique}"
    if kind == "object":
        required = set(prop.get("required") or [])
        shape = ", ".join(
            f"{name}{'?' if name not in required else ''}: "
            f"{_describe_prop(sub, schema)}"
            for name, sub in (prop.get("properties") or {}).items()
        )
        closed = " — no other keys" if prop.get("additionalProperties") is False else ""
        return "{" + shape + "}" + closed
    return str(kind or "any")


def _schema_contract_lines(schema: dict) -> list[str]:
    """The output contract's per-field shape lines, generated from the
    loaded tasks schema — every spec field the proposal may set, with
    its enums, patterns, bounds, object shapes and required keys, plus
    the schema's own required-field list for a task."""
    tasks = _resolve_ref(schema["properties"]["tasks"], schema)
    task = _resolve_ref(tasks["items"], schema)
    props = task["properties"]
    lines = [
        "Each task carries: id, title, source (repo-relative path of the doc",
        "that owns the spec, with #heading when useful) — plus exactly these",
        "field shapes, generated from the factory's tasks schema",
        "(a proposal outside them is refused, however plausible it reads):",
    ]
    for name in SPEC_FIELDS:
        lines.append(f"- {name}: {_describe_prop(props[name], schema)}")
    lines.append(
        "The schema requires every task to carry: "
        + ", ".join(task["required"])
        + ". You never"
    )
    lines.extend(["set status — every imported task is draft.", "Rules:"])
    return lines


class PlanningDocs(NamedTuple):
    """The planning-doc listing split at the cap (F-49 AC-3, m-4).

    ``kept`` is what the prompt lists (and what the budget scales with);
    ``dropped`` is what the cap left out — announced, never silent: the
    prompt says ``+N more``, and the import summary names the dropped
    docs, so tasks defined only in them cannot quietly vanish from the
    proposal.
    """

    kept: list[str]
    dropped: list[str]


def _list_planning_docs(repo: Path, docs_dir: str) -> PlanningDocs:
    """The repo-relative planning docs the prompt lists: markdown files
    under ``docs_dir``, sorted; the first :data:`MAX_DOC_FILES` are kept
    and the rest are dropped — the dropped names ride along so the
    truncation is announced (:class:`PlanningDocs`, F-49 AC-3).

    The traversal is explicit and interpreter-independent (F-57, the
    F-49/F-51 re-routed item): ``os.walk(followlinks=False)`` — never a
    directory symlink followed, never a symlink loop — so the listing
    cannot depend on pathlib glob internals that differ across Python
    versions. A FILE symlink is listed only when its target resolves
    INSIDE the repo: one whose target escapes is excluded, so the listing
    never contradicts the read boundary the clone's committed tree and
    F-47's sandbox deny actually enforce (containment is theirs; this
    merely refuses to advertise a path they would deny).

    One listing for two consumers — the prompt (:func:`build_import_prompt`)
    and the budget (:func:`scaled_import_budget`) — so the number the
    budget scales with is by construction the number of docs the session
    was actually told to read."""
    repo = Path(repo)
    docs = repo / docs_dir
    if not docs.is_dir():
        return PlanningDocs([], [])
    resolved_repo = repo.resolve()
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(docs, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix not in {".md", ".markdown"} or not path.is_file():
                continue
            try:
                inside = path.resolve().is_relative_to(resolved_repo)
            except OSError:  # a dangling or unreadable link: not ours to list
                continue
            if not inside:
                continue
            found.append(str(path.relative_to(repo)))
    all_docs = sorted(found)
    return PlanningDocs(all_docs[:MAX_DOC_FILES], all_docs[MAX_DOC_FILES:])


def scaled_import_budget(doc_count: int) -> tuple[int, float]:
    """The default ``(max_turns, timeout_min)`` for an import listing
    ``doc_count`` planning docs::

        max_turns    = min(30 + doc_count, 200)
        timeout_min  = min(20 + doc_count / 5, 120)

    Derived from the two runs that bought it, not guessed:

    * run ``20261105T081854Z-1c569f`` (55 docs, 30 tasks, no self-check)
      **completed** inside the hard ``max_turns`` 30 while reporting
      ``num_turns`` 56 in 548 s. Those are different counters: the
      bundled CLI's result ``num_turns`` increments per user-role
      message row (the prompt, every tool-result batch), while the cap
      binds on the query loop's own turn count — 56 rows ≈ 55 docs +
      the prompt, inside ≤30 counted turns. So ~1 row per listed doc,
      and a 30-turn base covered reading plus proposing for 55 docs.
    * the post-#94 run (same repo, plus the write/parse/shape
      self-check) burned past the hard 20 min and aborted with nothing
      kept: the self-check rounds are work of the same order as the
      reading, and the fixed budget had no room for them.

    ``30 + docs`` sizes the cap to the observed row count plus the base
    the 55-doc run proved sufficient without the self-check (85 turns
    for 55 docs, ~40% over a doubled-base estimate); ``20 + docs/5``
    gives that repo 31 min — ~3.4× its observed no-self-check wall time
    and 11 min past the budget the self-check run already exhausted.
    Small imports stay at today's defaults in practice (3 docs → 33
    turns / 20.6 min). Explicit flags always win (:func:`run_import`)."""
    return (
        min(DEFAULT_MAX_TURNS + doc_count, TURN_CEILING),
        min(DEFAULT_TIMEOUT_MIN + doc_count / 5, TIMEOUT_MIN_CEILING),
    )


def validate_budget(
    max_turns: int | None, timeout_min: float | None
) -> None:
    """Refuse an impossible explicit budget before any session starts.

    ``max_turns`` must be an integer in ``[1, TURN_CEILING]``;
    ``timeout_min`` a float in ``(0, TIMEOUT_MIN_CEILING]`` (NaN and inf
    fail the comparison and are refused with the same message). None
    means "not given" and passes — the scaled default applies.

    Raises:
        ImportRefused: A given budget is outside its bounds — naming the
            flag, the bound and the value, so the fix is a retyping.
    """
    if max_turns is not None and not 1 <= max_turns <= TURN_CEILING:
        raise ImportRefused(
            f"--max-turns must be an integer from 1 to {TURN_CEILING}, "
            f"got {max_turns!r}"
        )
    if timeout_min is not None and not 0 < timeout_min <= TIMEOUT_MIN_CEILING:
        raise ImportRefused(
            f"--timeout-min must be a number above 0 and at most "
            f"{TIMEOUT_MIN_CEILING:g}, got {timeout_min!r}"
        )


def build_import_prompt(
    repo: Path, docs_dir: str, records: list[dict], *,
    project: str | None = None,
    files: PlanningDocs | None = None,
) -> str:
    """The importer session's prompt: which docs to read, the register
    to match against, and the exact output contract.

    Args:
        repo: The project repo root.
        docs_dir: Repo-relative planning-docs directory.
        records: The out-of-scope register (see :func:`read_out_of_scope`).
        project: The queue's project name — the one the proposal must
            carry exactly. With it the output contract states the name
            literally; without it (the pre-name contract arm, kept for
            direct calls) the placeholder stays and the session is back
            to guessing — :func:`run_import` always passes it.
        files: The planning-doc listing, when the caller already holds
            the ONE computation (:func:`run_import` does — the prompt,
            the budget and the summary must all describe the same docs,
            so the listing is computed once and threaded through; F-49
            review round 1, finding 3). None computes it here, for
            direct callers.

    The name is stated, not described: a placeholder like
    ``"<project name>"`` left the session to guess from the repository
    directory's spelling, and a one-letter-case guess was refused by
    :func:`plan_import` after a full session had already been spent.

    The field shapes are stated, not named: the contract lines come from
    :func:`_schema_contract_lines` over the loaded schema, so the prompt
    always says what the validator will enforce. And the session is told
    to check its own output before answering — parse, then shape, with
    the absolute pinned interpreter (``jsonschema`` is importable there;
    a bare ``python3`` inside the session resolves to whatever the
    sandbox PATH finds, not the factory's venv) and against the proposal
    schema *staged into its writable scratch* by
    :func:`sdk_import_session` (``$TMPDIR/proposal.schema.json`` — the
    session's clone is the *target* repo and has no schema/ of its own,
    and the queue schema would refuse a valid proposal for the version
    and status the importer itself writes; see
    :func:`build_proposal_schema`). The repo cwd is read-only, so both
    the proposal and the check outputs live under ``$TMPDIR``.
    """
    repo = Path(repo)
    if files is None:
        files = _list_planning_docs(repo, docs_dir)
    lines = [
        "You are the factory's importer seat. Read the project's planning",
        "documents and propose a tasks.json queue for them.",
        "",
        "Planning documents (read them from the repo):",
    ]
    if files.kept:
        lines.extend(f"- {name}" for name in files.kept)
    else:
        lines.append("- (none found under the docs directory)")
    if files.dropped:
        # F-49 AC-3 (m-4): the cap is announced, never silent — the
        # session knows docs exist it was not told to read (and can rglob
        # for them if it chooses; the budget was sized for the listing),
        # and the summary the owner confirms names the dropped docs.
        lines.append(
            f"- (+{len(files.dropped)} more planning docs under {docs_dir}/ "
            "exist but are not listed; the listing above is what this "
            "import was sized for)"
        )
    lines += ["", "Out-of-scope register (TASK-CONTRACT §5) — concepts the",
              "owner already rejected. Match by concept, not keyword:"]
    if records:
        for record in records:
            lines += [f"### {record['path']}", record["text"].strip(), ""]
    else:
        lines.append("(the register is empty or missing)")
    if project is not None:
        contract = f'  {{"project": "{project}", "tasks": [...]}}'
        naming_rule = [
            f"- project must be exactly {project} — the queue's name as",
            "  given here, not the repository directory's spelling or a",
            "  variant casing; a proposal naming anything else is refused.",
        ]
    else:
        contract = '  {"project": "<project name>", "tasks": [...]}'
        naming_rule = []
    interpreter = shlex.quote(sys.executable)
    proposal_ref = f"$TMPDIR/{PROPOSAL_OUTPUT_NAME}"
    schema_ref = f"$TMPDIR/{PROPOSAL_SCHEMA_NAME}"
    parse_cmd = (
        f"{interpreter} -c \"import json; json.load(open('{proposal_ref}'))\""
    )
    shape_cmd = (
        f"{interpreter} -c \"import json, jsonschema; "
        f"d=json.load(open('{proposal_ref}')); "
        f"s=json.load(open('{schema_ref}')); "
        f"[print(e.json_path + ': ' + e.message) for e in "
        f"jsonschema.Draft202012Validator(s).iter_errors(d)]\""
    )
    lines += [
        "",
        "Output — and output nothing else — one JSON object:",
        contract,
        *_schema_contract_lines(_load_tasks_schema()),
        *naming_rule,
        "- gates in v0 are the safest value, whatever you propose (the",
        "  parent forces them and lists every override to the owner):",
        '- merge is always "human" and deploy is always "human" — the',
        "  owner merges and deploys; never propose an auto gate.",
        "- spec_approval is always true for build and docs tasks; other",
        "  kinds (owner_only, decision, research) keep what you propose.",
        "- budget.size tops out at M: a task too big for size M is split",
        "  into slices or proposed as separate tasks, never sized above",
        "  the enum.",
        "- priority is P1–P3 only — the values listed above; P0 is not a",
        "  priority level this queue knows.",
        '- a check whose type is anything but "manual" must carry ref.',
        "- a TEST check's ref is EXACT (F-71): <repo-relative path>::",
        "  <test name>, one or more :: segments, never an absolute or ..",
        "  path, never an empty or whitespace-padded name. Approval and",
        "  this importer both refuse anything else. For a NEW test file",
        "  the convention is tests/test_<task>.py::test_ac_N.",
        "- a COMMAND check's ref is the full command, non-empty (F-71).",
        "- a task that adds a dependency declares it (F-71): dependencies",
        '  is a list of {"ecosystem": "pypi"|"npm", "name"} entries —',
        "  every package a direct manifest gains must be declared, or",
        "  the run escalates. Names are normalised (PEP 503 / lowercase).",
        "- owner_steps you propose are objects with text and done: false —",
        "  done_at, done_by and evidence are written when the owner ticks",
        '  them, never by you. Slices you propose always have status "todo".',
        "- Never set status, acceptance_hash, approved_at, approved_by,",
        "  attempts, links or out_of_scope. Every task is imported as draft;",
        "  approval is the owner's act (factory approve).",
        "- Possibly shipped work (F-67): for each build or docs task you",
        "  propose, check the committed HEAD you are reading for the",
        "  task's named deliverables — named files, functions, migrations,",
        "  or a status line in the repo's own records (a work-order",
        "  doc's status, CLAUDE.md). Report every match in the top-level",
        '  "possibly_shipped" list, one {task_id, evidence (a path:line',
        "  or a commit sha, repo-relative), reason (one line)} object per",
        "  match. The owner decides — nothing changes a task because you",
        "  flagged it; a task you then omit from the proposal makes its",
        "  entry refuse.",
        "- Criteria accounting (F-67), against the repo's existing",
        "  .factory/tasks.json when one exists: every reference task and",
        "  every one of its acceptance criteria must be accounted for, or",
        "  the proposal is REFUSED. Account for each by (a) naming the",
        '  reference ids a new task replaces in its "predecessors" list',
        "  (a split or a merge names them; several tasks may share one),",
        '  (b) giving each carried criterion a "from": "<ref task id>/',
        '  <ref AC id>" beside its text, or (c) listing the drop in the',
        '  top-level "dropped_criteria" as {ref_task, ref_ac (omit for a',
        "  whole task), doc, line, reason} — where doc:line is a line of",
        "  a planning doc you were shown that removes, completes or",
        "  supersedes it. A drop whose criterion text the docs still",
        "  carry blocks the apply until the owner accepts it by name.",
        "- scope.paths names the files each task may change (F-65):",
        "  repo-relative globs, `*` never crossing `/`, `**` spanning",
        "  directories. Propose one for EVERY task. Refused: an absolute",
        "  or `..` path, an empty pattern, `[` character classes, and any",
        "  pattern that matches everything (`**`, `*`, `**/*`, `*/**`).",
        "  Name the real surface (the module, its tests, the docs the",
        "  task owns) — the owner edits or approves it with the contract.",
        '- When a proposed task resembles an out-of-scope record, add',
        '  "out_of_scope_match": "<the record\'s path>" to that task. Never',
        "  drop, reword or omit a task because it matches — the owner",
        "  confirms the match; you only surface it.",
        "- Propose every task the documents define, including tasks already",
        "  in the queue; unchanged tasks produce no diff.",
        "",
        "Propose each task as you finalise it, with the propose_task tool:",
        "- propose_task(task): ONE task per call, validated immediately",
        "  against the same schema and rules as above. The answer is",
        "  'accepted <id>' or every violation that task broke — fix what it",
        "  names and re-propose. Re-proposing an id REPLACES it",
        "  (first-proposal order is kept).",
        "- The accepted set IS the proposal: once you have called the tool",
        "  at all, it wins over any text answer — finish with a one-line",
        "  confirmation, not the JSON. If you never call it, your final",
        "  text answer is the proposal (the contract above, as one JSON",
        "  object and nothing else).",
        "",
        "Before you answer, check your own output (the interpreter named",
        "below is the pinned one your sandbox already runs, and jsonschema",
        "is importable there):",
        f"- propose_task keeps {proposal_ref} written for you as the",
        "  accepted set grows. If you never called it, write the finished",
        f"  JSON to {proposal_ref} yourself, in your writable scratch (the",
        "  repo cwd is read-only).",
        "- Parse check:",
        f"  {parse_cmd}",
        f"- Shape check against {schema_ref} — the factory's tasks schema,",
        "  staged for you before this session and already narrowed to a",
        "  proposal's own fields (no version, no status, none of the owner",
        "  or orchestrator keys):",
        f"  {shape_cmd}",
        "- Fix everything those print, re-check, and only then answer.",
        "  If you used propose_task, your final answer is one line",
        "  confirming you are done (the tool's accepted set is checked",
        "  again by the factory); without it, your final answer is the",
        "  JSON object itself, and nothing else.",
        "- What the shape check cannot see, and is still refused after you",
        "  answer: the project name spelled exactly as stated above, task",
        "  ids unique and non-blank, and out_of_scope_match naming a real",
        "  record from the register.",
    ]
    return "\n".join(lines)


# --------------------------------------------------- F-57: the propose tool --


def _task_violations(
    task: dict, schema: dict, record_paths: "Sequence[str]",
) -> list[str]:
    """EVERY violation one proposed task breaks (F-57 AC-1): the
    per-task import rules first (readable id, forbidden fields, status,
    a real out-of-scope record), then every shape violation the staged
    proposal schema reports for it — the same shapes the session
    self-checks with and :func:`plan_import` judges the whole proposal
    by, so a task the tool accepts is never refused for a per-task shape
    later, and a task it refuses names everything at once.
    """
    lines: list[str] = []
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        try:
            shown = json.dumps(task, default=str)[:200]
        except (RecursionError, ValueError):
            shown = f"<unserialisable {type(task).__name__}>"
        lines.append(f"the task has no readable id: {shown}")
        name = "the task"
    else:
        name = f"task {task_id}"
    for banned in FORBIDDEN_FIELDS:
        if banned in task:
            lines.append(
                f"{name}: a proposal must not carry {banned!r} — approval "
                "fields, attempts, links and out_of_scope are written by "
                "you and the orchestrator, never by the importer"
            )
    status = task.get("status")
    if status is not None and status != DRAFT:
        lines.append(
            f"{name}: proposed status {status!r}; every imported task is "
            f"{DRAFT} — approval is yours (factory approve)"
        )
    match = task.get("out_of_scope_match")
    if match is not None and (
        not isinstance(match, str) or match not in set(record_paths)
    ):
        known = ", ".join(sorted(record_paths)) or "none"
        lines.append(
            f"{name}: out_of_scope_match {match!r} names no record under "
            f"{OUT_OF_SCOPE_DIR}/ (known records: {known})"
        )
    # F-65: every declared scope pattern obeys the load-time refusals
    # HERE too (the shared validator), so a proposal never lands a
    # catch-all or absolute scope the queue would refuse to load.
    scope_paths = (
        (task.get("scope") or {}).get("paths")
        if isinstance(task.get("scope"), dict) else None
    )
    if isinstance(scope_paths, list):
        for pattern in scope_paths:
            reason = scope_pattern_error(pattern)
            if reason is not None:
                lines.append(
                    f"{name}: scope pattern {pattern!r} {reason} — scope "
                    "paths are repo-relative globs naming the files the "
                    "task may change (F-65)"
                )
    # The staged proposal schema over a one-task document: every shape
    # violation is about this task (the project key is filled in).
    lines.extend(iter_schema_violations(
        {"project": "-", "tasks": [task]}, schema
    ))
    return lines


class ProposeCollector:
    """The in-memory propose_task sink living inside one importer
    session's runner process (the vent pattern, F-27).

    One call validates ONE task against the staged proposal schema and
    the per-task import rules; an accepted task is stored (re-proposing
    an id replaces it, first-proposal order kept) and the accepted set
    is persisted — atomically, on every accept — to the session's
    writable scratch as ``$TMPDIR/proposal.json``: the very file the
    self-check prompt names and the F-56 abort salvage copies out, so a
    crash, timeout or quota mid-session keeps what was proposed.

    Everything about a call is bounded (the session is untrusted): past
    the calls cap, the accepted-task cap, the per-task size cap or the
    nesting ceiling, a call is a refusal message to the model plus a
    fact on :attr:`facts` — never a crash, never a lost set. The
    collector rides the :class:`~factory.session.SessionRecord` out of
    the sandbox (``proposed_tasks`` / ``propose_facts``); the accepted
    set is an EARLY WARNING, never the authority — the final set goes
    through the same full :func:`plan_import` a text answer faces.
    """

    def __init__(
        self,
        *,
        project: str,
        schema: dict,
        record_paths: "Sequence[str]" = (),
        secrets: "Sequence[str]" = (),
        scratch_dir: Path | str | None = None,
    ) -> None:
        self.project = project
        self.schema = schema
        self.record_paths = tuple(record_paths)
        # F-43: the literal secrets this session's runner holds (the seat
        # token), so a task is seam-scrubbed the moment it is accepted.
        self.secrets = tuple(
            secret for secret in check_secrets(secrets) if secret
        )
        self.scratch_dir = (
            Path(scratch_dir) if scratch_dir is not None else None
        )
        self.calls = 0
        self._facts: list[dict[str, Any]] = []
        self._tasks: dict[str, dict] = {}
        self._order: list[str] = []

    def facts(self) -> list[dict[str, Any]]:
        """The recorded bound-hits (one per capped call class), as a
        copy — what rides the record out of the sandbox."""
        return [dict(fact) for fact in self._facts]

    def tasks(self) -> list[dict]:
        """The accepted set, in first-proposal order (a replaced task
        keeps its first-proposed position with its newest value)."""
        return [copy.deepcopy(self._tasks[task_id]) for task_id in self._order]

    def _fact(self, kind: str, detail: str) -> None:
        self._facts.append({"kind": kind, "detail": detail, "at": now_iso()})

    def _persist(self) -> None:
        """Keep the accepted set crash-safe: the atomic writer, the file
        the self-check prompt names and the abort salvage copies."""
        if self.scratch_dir is None:
            return
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            self.scratch_dir / PROPOSAL_OUTPUT_NAME,
            {"project": self.project, "tasks": self.tasks()},
        )

    def propose(self, task: Any) -> str:
        """Validate, cap and store one proposed task; the tool's text
        answer — ``accepted <id>`` or every violation it broke.

        Raises:
            ValueError: ``task`` is not an object — a malformed call the
                SDK's tool wrapper answers as an isError result the
                model can read and retry, never a protocol error.
        """
        if not isinstance(task, dict):
            raise ValueError(
                "propose_task takes one task object: id plus exactly the "
                "spec fields the output contract states"
            )
        self.calls += 1
        if self.calls > PROPOSE_MAX_CALLS:
            self._fact(
                "calls_capped", f"call {self.calls} past {PROPOSE_MAX_CALLS}"
            )
            return (
                f"refused: the propose_task call cap ({PROPOSE_MAX_CALLS}) "
                f"is reached; the accepted set stands at {len(self._tasks)} "
                "tasks — finish with your answer"
            )
        # Depth first, iteratively: nothing downstream may recurse over
        # hostile structure (the parse boundary's own rule).
        depth = _max_nesting(task)
        if depth > MAX_PROPOSAL_DEPTH:
            self._fact("task_too_deep", f"nesting depth {depth}")
            return (
                f"refused: the task nests {depth} levels deep; a task is at "
                f"most {MAX_PROPOSAL_DEPTH} — propose it flatter"
            )
        try:
            size = len(json.dumps(task, ensure_ascii=False, default=str))
        except (RecursionError, ValueError) as exc:
            self._fact("task_too_large", f"unserialisable ({exc})")
            return (
                "refused: the task cannot be measured for size "
                f"({type(exc).__name__}) — propose it smaller"
            )
        if size > PROPOSE_TASK_MAX_CHARS:
            self._fact("task_too_large", f"{size} characters")
            return (
                f"refused: the task serialises to {size} characters; one "
                f"task is at most {PROPOSE_TASK_MAX_CHARS} — propose it "
                "smaller"
            )
        # The egress seam (vent's clean_text rule: redact, then anything
        # else — never store a raw credential or control sequence), then
        # validation judges the scrubbed values exactly as the text
        # answer's will be judged after its own scrub.
        task = scrub_session_value(task, self.secrets)
        violations = _task_violations(task, self.schema, self.record_paths)
        if violations:
            task_id = task.get("id")
            head = (
                f"task {task_id} refused"
                if isinstance(task_id, str) and task_id.strip()
                else "task refused"
            )
            return _format_violations(head, violations)
        task_id = task["id"]
        if (
            task_id not in self._tasks
            and len(self._tasks) >= PROPOSE_MAX_TASKS
        ):
            self._fact(
                "tasks_capped", f"id {task_id} past {PROPOSE_MAX_TASKS}"
            )
            return (
                f"refused: the accepted-task cap ({PROPOSE_MAX_TASKS}) is "
                "reached; re-propose an existing id or finish"
            )
        if task_id not in self._tasks:
            self._order.append(task_id)
        self._tasks[task_id] = task
        self._persist()
        return f"accepted {task_id}"


def make_propose_tool(collector: ProposeCollector) -> Any:
    """The in-process ``propose_task(task)`` SDK tool (F-57 AC-1).

    One per importer session, closed over that session's collector: the
    handler can do exactly one thing — validate and store one task —
    and has no path to any file (except the collector's own atomic
    scratch write), environment or network.
    """

    @tool(
        PROPOSE_TOOL_NAME,
        "Propose ONE task of the importer's queue. Each call validates "
        "that task against the proposal schema and the import rules, and "
        "answers 'accepted <id>' or every violation it broke — fix and "
        "re-propose. Re-proposing an id replaces it (first-proposal order "
        "is kept); the accepted set is the proposal: once you have called "
        "this tool at all it wins over any text answer, so finish with a "
        "one-line confirmation instead of the JSON. If you never call it, "
        "your final text answer is the proposal.",
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "object",
                    "description": "One task: id plus exactly the spec "
                    "fields the output contract states.",
                }
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    )
    async def _propose(args: dict[str, Any]) -> dict[str, Any]:
        answer = collector.propose(args["task"])
        return {"content": [{"type": "text", "text": answer}]}

    return _propose


def propose_server(collector: ProposeCollector) -> Any:
    """The in-process MCP server carrying the propose tool, for
    ``ClaudeAgentOptions.mcp_servers`` (the tool is additionally listed
    in ``allowed_tools`` under its qualified name, and granted under the
    toolkit's mcp guard, exactly like vent's)."""
    return create_sdk_mcp_server(
        name=PROPOSE_TOOL_NAME, tools=[make_propose_tool(collector)]
    )


# -------------------------------------------------------------------- plan --


@dataclass(frozen=True)
class TaskChange:
    """What the import proposes for one task.

    ``kind`` is one of:

    * ``new`` — not in the queue; proposed as ``draft``.
    * ``unchanged`` — in the queue, proposed spec identical: no diff.
    * ``changed`` — proposed spec differs in ``fields``; only those
      fields are updated, state is preserved — unless a CONTRACT field
      (``acceptance``, ``risk``, ``gates``) changed on a non-draft task,
      which sends the task back to ``draft`` with its approval cleared
      (``demoted``; F-03's one X→draft reason — the approved contract
      changed — and F-57 review round 1, F4: a risk/gates relaxation can
      never ride an existing approval).
    * ``missing`` — in the queue but not proposed; carried untouched.
    """

    task_id: str
    kind: str
    fields: tuple[str, ...] = ()
    demoted: bool = False
    flagged: bool = False


@dataclass(frozen=True)
class OutOfScopeMatch:
    """A proposed task that resembles a register record (AC-4)."""

    task_id: str
    record: str


@dataclass(frozen=True)
class GateOverride:
    """One gate the parent forced (F-67 AC-1): what the proposal said,
    what the queue held, and the v0 value that landed (always
    ``human``/``True``). Rendered in the summary before the diff — a
    relaxation is never silently dropped."""

    task_id: str
    field: str
    proposed: Any
    stored: Any
    new_task: bool


@dataclass(frozen=True)
class RiskAddition:
    """One risk flag the parent ADDED to a proposal's ``risk`` (F-71
    AC-1): the flag, the reaching paths (capped for render), and whether
    the task was new. Adding is the conservative direction — the parent
    never removes a flag; rendered with the overrides, before the diff."""

    task_id: str
    flag: str
    reaching: tuple
    new_task: bool


@dataclass(frozen=True)
class BudgetClamp:
    """One budget field the parent clamped UP to the profile's floor
    (F-71 AC-3): never down. Rendered with the overrides, before the
    diff."""

    task_id: str
    field: str
    proposed: int
    clamped_to: int


@dataclass(frozen=True)
class ShippedFlag:
    """Possibly-shipped work (F-67 AC-3): flagged for the owner, never
    decided. ``evidence_found`` is None when the evidence is not a
    repo-relative path (a commit sha) and was not checked; False when a
    path-shaped evidence names no file in the repo."""

    task_id: str
    evidence: str
    reason: str
    evidence_found: bool | None


@dataclass(frozen=True)
class DropEntry:
    """One explicitly dropped reference task or criterion (F-67 AC-4),
    backed by a doc line.

    R2-1: EVERY drop needs the owner's per-drop acceptance — there is
    deliberately no exemption (a done-marked line about one criterion
    cited to drop another walked the R1 exemption; the session chooses
    which line to cite, so no cited line can decide). The evidence the
    owner decides with is carried as ADVISORY labels: ``cited_done``
    (the cited line carries a done-marker), ``own_source`` (the cited
    doc is the reference task's own ``source`` doc) and
    ``still_in_docs`` (the exact-text label) — none of them bypass
    anything."""

    ref_task: str
    ref_ac: str | None
    doc: str
    line: int
    reason: str
    still_in_docs: bool
    cited_done: bool = False
    own_source: bool = False

    @property
    def label(self) -> str:
        return self.ref_task if self.ref_ac is None \
            else f"{self.ref_task}/{self.ref_ac}"


@dataclass(frozen=True)
class RewrittenCriterion:
    """One reference criterion whose mapped proposed criterion changed
    its TEXT or weakened its CHECK (R1-1/R2-2: the accounting proves
    claims; the review must also SHOW what each claim did to the
    criterion's meaning and enforcement). Includes the same-id carry
    (same task id + same AC id). Not blocking — visible, escaped, in
    the summary's Rewritten section with what changed named."""

    ref_key: str
    ref_text: str
    new_text: str
    ref_check: "dict | None" = None
    new_check: "dict | None" = None
    changed: tuple = ()


@dataclass
class ImportPlan:
    """What the import proposes: the merged document, the per-task
    changes, the out-of-scope matches and the full unified diff."""

    project: str
    proposed_doc: dict
    changes: list[TaskChange] = field(default_factory=list)
    matches: list[OutOfScopeMatch] = field(default_factory=list)
    diff: str = ""
    # The queue the plan was built from (None: no queue existed). The
    # write refuses if the disk queue no longer matches it, so a change
    # made while the confirm prompt waits is never silently reverted.
    base_doc: dict | None = None
    # Planning docs the listing cap dropped (F-49 AC-3, m-4): the
    # summary names them, so the owner knows the proposal was built from
    # a capped view. Set by run_import (which sees the repo); a directly
    # built plan has none.
    truncated_docs: list[str] = field(default_factory=list)
    # F-67 AC-1: every gate the parent forced (rendered before the
    # diff — a relaxation is never silently dropped).
    gate_overrides: list[GateOverride] = field(default_factory=list)
    # F-71 AC-1/AC-3: the risk flags the parent added and the budget
    # fields it clamped up to the floor — rendered in the SAME section
    # as the gate overrides, before the diff.
    risk_additions: list[RiskAddition] = field(default_factory=list)
    budget_clamps: list[BudgetClamp] = field(default_factory=list)
    # F-67 AC-3: possibly-shipped work, flagged for the owner (rendered
    # FIRST — before the overrides and the diff).
    shipped: list[ShippedFlag] = field(default_factory=list)
    # F-67 AC-4 + R2-1: the explicit drops. Every drop needs the
    # owner's per-drop acceptance at apply; the advisory labels
    # (done-marker, own source doc, exact text still carried) ride
    # along for the decision, never as a bypass.
    dropped: list[DropEntry] = field(default_factory=list)
    # R1-1: mappings that changed a criterion's text — shown in the
    # summary's Rewritten section, never blocking.
    rewritten: list[RewrittenCriterion] = field(default_factory=list)
    # F-67 AC-4: the accounting's note when there was no reference
    # queue to account against (rendered beside the drops).
    reference_note: str = ""


def _task_from_proposal(
    raw: Any, index: int, record_paths: set[str],
) -> "tuple[dict, str | None, list[str], list[tuple[str, str, dict | None]]]":
    """Build one proposed task from a raw proposal entry.

    Returns the task (``id``, ``status: draft`` and exactly the spec
    fields the entry carries — no defaults: a default here would read as
    a proposal to an existing task's merge; see :func:`_new_task`), the
    out-of-scope record it claims to resemble (if any), its
    ``predecessors`` (F-67 AC-4: the reference ids it replaces) and its
    acceptance criteria's ``from`` mappings as ``("<ref task>/<ref AC>",
    <the criterion's proposed text>, <its check>)`` triples (R1-1/R2-2:
    the text and the check ride along so a mapping that rewrites a
    criterion or weakens its check can be shown). The two
    accounting fields are proposal-only — extracted here and never
    copied into the task, so they cannot reach tasks.json.

    Raises:
        ImportRefused: The entry is not an object, carries a forbidden
            field or a non-draft status, has no readable id, or names a
            record that does not exist.
    """
    if not isinstance(raw, dict):
        raise ImportRefused(f"proposal task #{index} is not an object")
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        try:
            shown = json.dumps(raw, default=str)[:200]
        except (RecursionError, ValueError):
            # Hostile structure in the entry itself (F-49 AC-5's class:
            # json.dumps recurses too): name the type, never crash the
            # refusal path on the thing it is refusing.
            shown = f"<unserialisable {type(raw).__name__}>"
        raise ImportRefused(
            f"proposal task #{index} has no readable id: {shown}"
        )
    for banned in FORBIDDEN_FIELDS:
        if banned in raw:
            raise ImportRefused(
                f"task {task_id}: a proposal must not carry {banned!r} — "
                "approval fields, attempts, links and out_of_scope are "
                "written by you and the orchestrator, never by the importer"
            )
    status = raw.get("status")
    if status is not None and status != DRAFT:
        raise ImportRefused(
            f"task {task_id}: proposed status {status!r}; every imported "
            f"task is {DRAFT} — approval is yours (factory approve)"
        )
    match = raw.get("out_of_scope_match")
    if match is not None:
        if not isinstance(match, str) or match not in record_paths:
            known = ", ".join(sorted(record_paths)) or "none"
            raise ImportRefused(
                f"task {task_id}: out_of_scope_match {match!r} names no "
                f"record under {OUT_OF_SCOPE_DIR}/ (known records: {known})"
            )
    task: dict[str, Any] = {"id": task_id, "status": DRAFT}
    for spec_field in SPEC_FIELDS:
        if spec_field in raw:
            task[spec_field] = raw[spec_field]
    # F-67 AC-4: the accounting metadata rides beside the task, never
    # in it — a "from" left inside an acceptance item would land in the
    # owner's queue (the queue schema's items carry exactly id/text/
    # check).
    predecessors_raw = raw.get("predecessors")
    predecessors = (
        [str(dep) for dep in predecessors_raw]
        if isinstance(predecessors_raw, list) else []
    )
    froms: "list[tuple[str, str, dict | None]]" = []
    acceptance = task.get("acceptance")
    if isinstance(acceptance, list):
        cleaned: list[Any] = []
        for item in acceptance:
            if isinstance(item, dict) and "from" in item:
                from_ref = item["from"]
                ac_text = item.get("text")
                ac_check = item.get("check")
                if isinstance(from_ref, str):
                    froms.append((
                        from_ref,
                        ac_text if isinstance(ac_text, str) else "",
                        ac_check if isinstance(ac_check, dict) else None,
                    ))
                cleaned.append(
                    {key: value for key, value in item.items()
                     if key != "from"}
                )
            else:
                cleaned.append(item)
        task["acceptance"] = cleaned
    return task, match, predecessors, froms


def _new_task(proposed: dict) -> dict:
    """A task the queue does not hold yet: the proposal plus the spec
    defaults (no dependencies, no risk flags, every gate human). Only a
    new task gets defaults — for an existing one an omitted field keeps
    its stored value (:func:`_merge_task`)."""
    task = copy.deepcopy(proposed)
    task.setdefault("depends_on", [])
    task.setdefault("risk", [])
    task.setdefault(
        "gates", {"spec_approval": True, "merge": "human", "deploy": "human"}
    )
    # attempts is the orchestrator's append-only log (TASK-CONTRACT §2) —
    # never proposed (FORBIDDEN_FIELDS), but the record is born with the
    # key so every reader of an imported task sees a list, never a
    # KeyError on a field the schema leaves optional.
    task.setdefault("attempts", [])
    return task


# The spec fields that ARE the approved contract: changing one on a
# non-draft task changes what the owner approved (F-03's X→draft reason).
# acceptance rides the recomputed hash; risk and gates ride the schema's
# risk⇒human-merge rule — a proposal that relaxes either must never keep
# the task's approval (F-57 review round 1, F4). scope rides the F-63
# whole-contract hash (F-65): a widened file scope is a new contract.
CONTRACT_FIELDS = ("acceptance", "risk", "gates", "scope")


def _merge_task(
    existing: dict, proposed: dict
) -> tuple[dict, list[str], bool, bool]:
    """Merge a proposal into an existing task: only the spec fields the
    proposal carries *and* changes are updated, so status, attempts,
    approval fields, links and every spec field the proposal leaves out
    survive untouched (AC-3).

    One exception (F4, review round 2): a proposal that CHANGES a
    contract field (:data:`CONTRACT_FIELDS`) on a task follows the ONE
    shared rule (:func:`factory.state.contract_change_action` — the same
    rule the queue audit applies to a stale hash): a contract-frozen
    status goes back to ``draft`` THROUGH THE LEGAL EDGE with its hash
    and approval fields KEPT as history (re-approval overwrites them);
    ``done`` is FLAGGED for the owner's review, never reopened
    automatically; ``draft`` and ``not_buildable`` are left as they are.
    The owner re-approves (or reopens) after reading the diff — the
    import proposes, the owner disposes."""
    changed = [
        spec_field
        for spec_field in SPEC_FIELDS
        if spec_field in proposed
        and canonical_json(existing.get(spec_field))
        != canonical_json(proposed[spec_field])
    ]
    merged = copy.deepcopy(existing)
    for spec_field in changed:
        merged[spec_field] = copy.deepcopy(proposed[spec_field])
    demoted = flagged = False
    if any(field in changed for field in CONTRACT_FIELDS):
        action = contract_change_action(existing.get("status", DRAFT))
        if action == "demote":
            demoted = True
            merged["status"] = DRAFT  # hash/approval fields stay (history)
        elif action == "flag":
            flagged = True  # done: kept, surfaced for the owner's review
    # Same normalisation as a fresh task (_new_task): a queue
    # written before the key existed (or by hand) still merges out with a
    # list, not a missing key.
    merged.setdefault("attempts", [])
    return merged, changed, demoted, flagged


def _normalise_gates(
    task: dict, stored: dict | None,
) -> list[GateOverride]:
    """The gate half of the ONE normalisation step (F-67 AC-1), in
    place. Whatever the proposal says (``"auto"``, ``"Human"``, a
    boolean, a missing key): ``merge``/``deploy`` become ``"human"``,
    and ``spec_approval`` becomes ``True`` for kinds the picker runs.
    Every change is a :class:`GateOverride`.

    A proposal whose ``gates`` is not an object is left alone: the
    proposal-schema check refuses it right after, the shape named. A
    merged task whose proposal OMITS ``gates`` is left alone too: the
    stored gates survive (F-12 AC-3), and a stored auto gate is caught
    by :func:`factory.approve.check_v0_gates` at approval. See
    :func:`_normalise_task` — the step's entry point."""
    gates = task.get("gates")
    if not isinstance(gates, dict):
        return []
    task_id = task.get("id", "?")
    stored_gates = (
        stored.get("gates") if isinstance(stored, dict) else None
    )
    stored_gates = stored_gates if isinstance(stored_gates, dict) else {}
    overrides: list[GateOverride] = []

    def _force(field: str, v0_value: Any) -> None:
        if gates.get(field) != v0_value:
            overrides.append(GateOverride(
                task_id=task_id, field=f"gates.{field}",
                proposed=gates.get(field),
                stored=stored_gates.get(field),
                new_task=stored is None,
            ))
            gates[field] = v0_value

    _force("merge", "human")
    _force("deploy", "human")
    if task.get("kind") in ELIGIBLE_KINDS:
        _force("spec_approval", True)
    return overrides


def _refusal_error(ref: str) -> str | None:
    """Why a proposed test ref is not runnable (F-71 AC-2), or None.

    The parent refuses an inexact ref in a proposal the same way
    approval does — the shape is :func:`factory.approve._ref_runnable_error`'s,
    re-stated here so the refusal names the criterion the same way."""
    from factory.approve import _ref_runnable_error

    return _ref_runnable_error(ref)


def _normalise_task(
    task: dict, stored: dict | None, *,
    profile: dict | None = None,
    tracked_files: list | None = None,
) -> tuple[list[GateOverride], list[RiskAddition], list[BudgetClamp]]:
    """The ONE normalisation step the parent applies to EVERY task the
    importer writes — new and merged — inside :func:`plan_import`,
    before the schema check judges the normalised task and the merge
    builds the diff. F-67's gate forcing; F-71's pre-spend half:

    * AC-2 — a proposed acceptance's test refs must be EXACT
      (``<repo-relative path>::<name>``; an empty command ref refuses
      too). The parent refuses the proposal the same way approval
      does, the criterion named. Judged on what the PROPOSAL carries:
      a merged task whose proposal omits ``acceptance`` keeps its
      stored criteria, and approval judges those.
    * AC-1 — the risk flags the task's effective scope (proposed if
      carried, else stored) can reach are ADDED to ``risk`` when
      missing. Adding is the conservative direction; a flag is never
      removed. Each addition is listed with the overrides.
    * AC-3 — the effective budget (proposed if carried, else stored) is
      clamped UP to the profile's budget floor, never down; each clamp
      is listed with the overrides.

    The risk/floor halves need the profile (risk globs, floor) and the
    repo's default-branch tracked files; without a profile (direct
    callers, and a queue beside no profile file) they are skipped —
    approval's fail-closed checks stand behind that. Refusable classes
    (non-executable kinds) are left alone: they never spend.

    Raises:
        ImportRefused: A proposed test ref is not runnable, or a
            proposed command check's ref is empty.
    """
    overrides = _normalise_gates(task, stored)
    task_id = task.get("id", "?")
    if task.get("kind") not in ELIGIBLE_KINDS:
        return overrides, [], []
    additions: list[RiskAddition] = []
    clamps: list[BudgetClamp] = []

    # AC-2: the refs the PROPOSAL itself carries.
    if "acceptance" in task:
        for item in task.get("acceptance") or []:
            check = item.get("check") if isinstance(item, dict) else None
            if not isinstance(check, dict):
                continue
            criterion = item.get("id", "?")
            if check.get("type") == "test":
                ref = check.get("ref")
                if isinstance(ref, str):
                    reason = _refusal_error(ref)
                    if reason is not None:
                        raise ImportRefused(
                            f"task {task_id}: {criterion}'s check is not "
                            f"runnable — {reason}; write the exact ref "
                            "(tests/test_<task>.py::test_ac_N for a new "
                            "test file; F-71)"
                        )
            elif check.get("type") == "command":
                command = check.get("ref")
                if isinstance(command, str) and not command.strip():
                    raise ImportRefused(
                        f"task {task_id}: {criterion}'s command check is "
                        "empty — a run cannot prove it (F-71)"
                    )

    # AC-1/AC-3 halves need the profile's rules.
    if not profile:
        return overrides, additions, clamps

    # AC-1: compute the flags the effective scope reaches, add misses.
    effective_scope = task.get("scope")
    if not isinstance(effective_scope, dict) or not effective_scope.get(
            "paths"):
        effective_scope = (
            stored.get("scope") if isinstance(stored, dict) else None
        )
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    if risk_globs and isinstance(effective_scope, dict) \
            and effective_scope.get("paths") and tracked_files is not None:
        computed = scope_risk_flags(
            effective_scope.get("paths") or [], risk_globs, tracked_files,
            generated_globs=(profile.get("paths") or {}).get("generated")
            or (),
            extra_files=contract_test_files(task),
        )
        effective_risk = task.get("risk")
        if not isinstance(effective_risk, list):
            stored_risk = (
                stored.get("risk") if isinstance(stored, dict) else None
            )
            effective_risk = stored_risk if isinstance(
                stored_risk, list) else []
        missing = {
            flag: paths for flag, paths in computed.items()
            if flag not in effective_risk
        }
        if missing:
            task["risk"] = sorted(set(effective_risk) | set(missing))
            additions = [
                RiskAddition(
                    task_id=task_id, flag=flag,
                    reaching=tuple(paths[:3]), new_task=stored is None,
                )
                for flag, paths in sorted(missing.items())
            ]

    # AC-3: clamp the effective budget UP to the floor, never down.
    floor = budget_floor(profile)
    effective_budget = task.get("budget")
    if not isinstance(effective_budget, dict):
        stored_budget = (
            stored.get("budget") if isinstance(stored, dict) else None
        )
        effective_budget = stored_budget if isinstance(
            stored_budget, dict) else None
    if isinstance(effective_budget, dict):
        clamped = dict(effective_budget)
        touched = False
        for field, minimum in floor.items():
            value = clamped.get(field)
            if isinstance(value, int) and not isinstance(value, bool) \
                    and value < minimum:
                clamps.append(BudgetClamp(
                    task_id=task_id, field=f"budget.{field}",
                    proposed=value, clamped_to=minimum,
                ))
                clamped[field] = minimum
                touched = True
        if touched:
            task["budget"] = clamped
    return overrides, additions, clamps


def _format_violations(head: str, lines: list[str]) -> str:
    """One refusal from a violation list: the count, every line capped
    at :data:`MAX_SHOWN_VIOLATIONS` with a ``+N more`` tail."""
    plural = "" if len(lines) == 1 else "s"
    parts = [f"{head}: {len(lines)} schema violation{plural}:"]
    parts.extend(f"- {line}" for line in lines[:MAX_SHOWN_VIOLATIONS])
    if len(lines) > MAX_SHOWN_VIOLATIONS:
        parts.append(f"- (+{len(lines) - MAX_SHOWN_VIOLATIONS} more)")
    return "\n".join(parts)


def _refusal_listing_every_violation(exc: ContractError, doc: dict) -> str:
    """The refusal message for a merged document that fails validation:
    every schema violation, capped at :data:`MAX_SHOWN_VIOLATIONS` with a
    ``+N more`` tail — not just the first.

    One-per-round naming (what ``validate_queue`` alone gives) made the
    2026-11-05 jobfinderos import cost a review round per defect class:
    every one of 30 tasks carried the same seven shape errors, and the
    refusal reported one. A semantic or structural refusal the schema
    lines do not cover (duplicates, forbidden fields) keeps its own
    message as the lead line.
    """
    lines = iter_schema_violations(doc, TASKS_SCHEMA)
    head = str(exc)
    if not lines:
        return head
    parts: list[str] = []
    if not any(f"at {line}" in head for line in lines):
        parts.append(head)
    parts.append(_format_violations("the proposed tasks.json is refused", lines))
    return "\n".join(parts)


def plan_import(
    existing_doc: dict | None,
    proposal: dict,
    records: list[dict],
    *,
    project: str,
    reference: dict | None = None,
    doc_texts: dict[str, str] | None = None,
    repo: Path | None = None,
    profile: dict | None = None,
) -> ImportPlan:
    """Build the import plan: merged document, per-task changes,
    out-of-scope matches and the full diff. Validates the merged
    document against the schema — an invalid proposal is refused before
    anything is shown or written (AC-1).

    F-67 adds three parent-side stages, in order after the per-task
    rules: the v0 gate normalisation (every task the importer writes,
    new and merged — :func:`_normalise_task`, which F-71 extends with
    the pre-spend half), the possibly-shipped validation
    (:func:`_validate_possibly_shipped`) and the criteria accounting
    (:func:`_account_reference`).

    Args:
        existing_doc: The queue as it is on disk, or None for a fresh
            project (the proposal then creates the queue).
        proposal: The parsed proposal (see :func:`parse_proposal`).
        records: The out-of-scope register (see :func:`read_out_of_scope`).
        project: The queue's project name — from the existing queue, or
            the profile for a fresh one.
        reference: The reference queue for the criteria accounting
            (:func:`read_reference_queue`'s plain-JSON read). None (the
            default, for direct callers) skips the accounting —
            :func:`run_import` always passes the real one.
        doc_texts: The kept planning docs' texts (see
            :func:`read_kept_doc_texts`), for the doc-line checks. None
            with a reference means no doc can back a drop.
        repo: The project repo, for possibly-shipped evidence existence
            checks. None skips the check (the flag renders
            ``evidence not found`` for path-shaped evidence).
        profile: The loaded project profile (F-71): its ``paths.risk``
            drive the risk-flag additions, its ``budget_floor`` the
            clamps, and its presence gates both. None or empty skips
            those halves (approval's fail-closed checks stand behind
            that); :func:`run_import` passes whatever it loaded.

    Raises:
        ImportRefused: The proposal names another project, omits or
            misspells the ``project`` key (F-49 AC-6 — the proposal
            schema requires it), duplicates an id, carries forbidden
            fields or statuses, names an unknown out-of-scope record —
            or the merged document would not validate (AC-1); or (F-67)
            a malformed/unknown ``possibly_shipped`` entry, or a
            reference task/criterion the proposal leaves unaccounted;
            or (F-71) a proposed test ref that is not runnable, an
            empty command ref, or a repo whose default branch cannot be
            listed for the risk computation.
    """
    proposed_project = proposal.get("project")
    if proposed_project is not None and proposed_project != project:
        raise ImportRefused(
            f"the proposal names project {proposed_project!r}; this queue "
            f"is {project!r}"
        )
    record_paths = {record["path"] for record in records}
    existing_tasks = existing_doc.get("tasks", []) if existing_doc else []
    by_id = {task.get("id"): task for task in existing_tasks}
    proposed_tasks: list[dict] = []
    matches: list[OutOfScopeMatch] = []
    predecessors: dict[str, list[str]] = {}
    froms: set[str] = set()
    from_triples: list[tuple[str, str, "dict | None"]] = []
    gate_overrides: list[GateOverride] = []
    risk_additions: list[RiskAddition] = []
    budget_clamps: list[BudgetClamp] = []
    # F-71 AC-1: the default branch's tracked files, computed ONCE for
    # the whole plan (the risk computation runs per task) — and only
    # when the profile declares risk globs. Fail closed: a repo whose
    # default branch cannot be listed cannot have its scopes checked,
    # so the import refuses rather than writing tasks whose risk flags
    # nobody computed.
    tracked_files: list | None = None
    if profile and repo is not None \
            and (profile.get("paths") or {}).get("risk"):
        default_branch = (profile.get("repo") or {}).get(
            "default_branch") or "main"
        try:
            # R1-3: the branch the run builds on — the remote default
            # after a fetch when the repo has an origin (the pilot's
            # checkout sat behind origin), else the local default.
            tracked_files = default_branch_files(repo, default_branch)
        except GateError as exc:
            raise ImportRefused(
                f"cannot compute the proposals' risk flags: {exc} (F-71)"
            ) from exc
    seen: set[str] = set()
    for index, raw in enumerate(proposal.get("tasks", [])):
        task, match, task_predecessors, task_froms = _task_from_proposal(
            raw, index, record_paths)
        if task["id"] in seen:
            raise ImportRefused(
                f"task {task['id']} appears twice in the proposal; task ids "
                "must be unique or dependency and attempt records become "
                "ambiguous"
            )
        seen.add(task["id"])
        # F-67 AC-1 / F-71: the ONE normalisation step, applied to every
        # task the importer writes — new and merged — before the schema
        # check judges the normalised task and the merge builds the diff.
        task_overrides, task_additions, task_clamps = _normalise_task(
            task, by_id.get(task["id"]),
            profile=profile, tracked_files=tracked_files,
        )
        gate_overrides.extend(task_overrides)
        risk_additions.extend(task_additions)
        budget_clamps.extend(task_clamps)
        # F-67 AC-4: the accounting metadata, collected beside the tasks.
        predecessors[task["id"]] = task_predecessors
        froms.update(ref for ref, _text, _check in task_froms)
        from_triples.extend(task_froms)
        proposed_tasks.append(task)
        if match:
            matches.append(OutOfScopeMatch(task_id=task["id"], record=match))

    # F-49 AC-6 (I-18): the ONE schema check the session's self-check and
    # the factory share. The semantic rules above keep their readable,
    # task-naming messages (status, forbidden fields, duplicate ids,
    # unknown records — checked first, on purpose); everything the schema
    # expresses better — a missing/empty/mistyped ``project``, an extra
    # top-level key, a task key outside the spec fields — is refused
    # here, BEFORE the merge, so a proposal cannot skip naming its
    # project and a typo'd task field cannot be silently dropped by the
    # merge's spec-field copy.
    try:
        violations = iter_schema_violations(
            proposal, plan_side_proposal_schema(_load_tasks_schema())
        )
    except RecursionError as exc:
        # A directly-driven plan (parse_proposal's depth bound not taken
        # on this path): refuse, never crash past the keep-refused path.
        raise ImportRefused(
            "the proposal is nested too deeply to check against the "
            "proposal schema (RecursionError)"
        ) from exc
    if violations:
        raise ImportRefused(_format_violations(
            "the proposal does not match the importer's proposal schema "
            "(the same shape check the session runs on its own output)",
            violations,
        ))

    # F-67 AC-3: possibly-shipped flags — validated against the proposal
    # itself (every entry names a proposed task) and the repo (path-
    # shaped evidence), before anything merges.
    shipped = _validate_possibly_shipped(proposal, seen, repo)
    # F-67 AC-4: the criteria accounting over the reference queue.
    dropped, reference_note = _account_reference(
        proposed_ids=seen,
        proposed_ac_ids={
            task["id"]: {
                item.get("id") for item in task.get("acceptance") or []
                if isinstance(item, dict)
            }
            for task in proposed_tasks
        },
        predecessors=predecessors,
        froms=froms,
        reference=reference,
        dropped_raw=proposal.get("dropped_criteria"),
        doc_texts=doc_texts,
    )
    # R1-1: show what every mapping did to the criterion's meaning.
    rewritten = _rewritten_criteria(from_triples, proposed_tasks, reference)

    existing_tasks = existing_doc.get("tasks", []) if existing_doc else []
    by_id = {task.get("id"): task for task in existing_tasks}
    merged: list[dict] = []
    changes: list[TaskChange] = []
    for existing in existing_tasks:
        task_id = existing.get("id", "?")
        proposed = next(
            (t for t in proposed_tasks if t["id"] == task_id), None
        )
        if proposed is None:
            changes.append(TaskChange(task_id=task_id, kind="missing"))
            merged.append(copy.deepcopy(existing))
            continue
        new_task, changed_fields, demoted, flagged = _merge_task(
            existing, proposed
        )
        merged.append(new_task)
        changes.append(
            TaskChange(
                task_id=task_id,
                kind="changed" if changed_fields else "unchanged",
                fields=tuple(changed_fields),
                demoted=demoted,
                flagged=flagged,
            )
        )
    for task in proposed_tasks:
        if task["id"] in by_id:
            continue
        merged.append(_new_task(task))
        changes.append(TaskChange(task_id=task["id"], kind="new"))

    # F5 (review round 1): a LOCAL dependency (no ``repo:`` prefix)
    # naming no task in the merged document is a broken proposal —
    # dependency and attempt records would be ambiguous. Cross-repo
    # ``repo:id`` deps are never judged here (the queue cannot see
    # another repo's ids). Checked on the MERGED ids, so a dep on a task
    # the queue already carries is fine; only the PROPOSED tasks' own
    # deps are judged (a pre-existing dangling dep is the queue's
    # problem, not this import's claim).
    merged_ids = {task.get("id") for task in merged}
    for task in proposed_tasks:
        for raw_dep in task.get("depends_on") or []:
            dep = raw_dep
            if not isinstance(dep, str) or not dep.strip():
                raise ImportRefused(
                    f"task {task['id']}: depends_on {dep!r} is malformed "
                    "— a dependency is a task id or repo:id"
                )
            repo_part, sep, id_part = dep.partition(":")
            if sep:
                if not repo_part or not id_part:
                    # "x:", ":y", ":" — neither a local id nor a real
                    # cross-repo reference
                    raise ImportRefused(
                        f"task {task['id']}: depends_on {dep!r} is "
                        "malformed — a cross-repo dependency is repo:id "
                        "with both parts non-empty"
                    )
                if repo_part == project:
                    # this queue's OWN project prefix: a local dependency
                    # by another spelling — judged against the merged ids
                    dep = id_part
                else:
                    continue  # a real cross-repo reference: never judged here
            if dep == task["id"]:
                raise ImportRefused(
                    f"task {task['id']}: depends_on names itself — a "
                    "task can never satisfy its own dependency"
                )
            if dep not in merged_ids:
                raise ImportRefused(
                    f"task {task['id']}: depends_on names {dep!r}, which "
                    "is neither in the proposal nor in the queue — a "
                    "local dependency must name a real task id "
                    "(cross-repo dependencies use repo:id)"
                )

    if existing_doc is not None:
        # The existing document, tasks swapped in place: its key order
        # (this repo's queues keep updated_at before tasks) and its
        # updated_at stamp are inherited, so an unchanged re-import
        # renders byte-identical and the diff is empty (AC-3). The write
        # path stamps its own updated_at.
        proposed_doc: dict[str, Any] = copy.deepcopy(existing_doc)
        proposed_doc["tasks"] = merged
    else:
        proposed_doc = {"version": 1, "project": project, "tasks": merged}
    try:
        validate_queue(proposed_doc, f"proposed tasks.json for {project}")
    except ContractError as exc:
        raise ImportRefused(
            _refusal_listing_every_violation(exc, proposed_doc)
        ) from exc
    except RecursionError as exc:
        # Defence in depth behind the proposal-schema check (which bounds
        # what can reach the merge): a refusal, never a crash past the
        # keep-refused path.
        raise ImportRefused(
            "the proposed tasks.json is nested too deeply to validate "
            "(RecursionError)"
        ) from exc

    before = _doc_text(existing_doc) if existing_doc else ""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            _doc_text(proposed_doc).splitlines(keepends=True),
            fromfile="tasks.json (current)",
            tofile="tasks.json (proposed)",
        )
    )
    return ImportPlan(
        project=project,
        proposed_doc=proposed_doc,
        changes=changes,
        matches=matches,
        diff=diff,
        base_doc=copy.deepcopy(existing_doc),
        gate_overrides=gate_overrides,
        risk_additions=risk_additions,
        budget_clamps=budget_clamps,
        shipped=shipped,
        dropped=dropped,
        rewritten=rewritten,
        reference_note=reference_note,
    )


def _check_repr(check: "dict | None") -> str:
    """One check as a compact renderable string (type + ref/expect)."""
    if not isinstance(check, dict):
        return "(none)"
    kind = str(check.get("type", "?"))
    detail = check.get("ref") or check.get("expect") or ""
    return f"{kind} {detail}".strip()


def _doc_text(doc: dict) -> str:
    """The on-disk rendering of a queue document (the diff's currency)."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ----------------------------------------------------------------- summary --


def render_summary(plan: ImportPlan) -> str:
    """The import summary: per-task changes, the out-of-scope matches
    (each with its record's path, for you to confirm — AC-4), and the
    full diff.

    F-67 adds four review sections, always rendered, in the order the
    owner must read them: possibly shipped (FIRST), the dropped
    criteria (R1-1: the acceptance mark decides the apply gate; the
    still-in-docs label is advisory), the rewritten criteria (R1-1:
    every mapping that changed a criterion's text, the same-id carry
    included), and the forced gate overrides — all before the diff.
    Every session-derived string in them is escaped
    (:func:`factory.egress.escape_approval_text`): the evidence and
    reasons are attacker-controlled text.
    """
    def ids(kind: str) -> list[str]:
        return [c.task_id for c in plan.changes if c.kind == kind]

    def show(value: Any) -> str:
        return escape_approval_text(str(value))

    new, unchanged, changed, missing = (
        ids("new"),
        ids("unchanged"),
        ids("changed"),
        ids("missing"),
    )
    lines = [
        f"# factory import — {plan.project}",
        "",
        "## Summary",
        "",
        f"- new tasks ({len(new)}): {', '.join(new) or 'none'}",
        f"- unchanged tasks ({len(unchanged)}): {', '.join(unchanged) or 'none'}",
        f"- changed tasks ({len(changed)}): {', '.join(changed) or 'none'}",
    ]
    for change in plan.changes:
        if change.kind == "changed":
            if change.demoted:
                suffix = (" -> draft (the approved contract changed; the "
                          "old hash and approval stay as history; "
                          "re-approve after review)")
            elif change.flagged:
                suffix = (" — contract changed on a DONE task: flagged "
                          "for your review, not reopened")
            else:
                suffix = ""
            lines.append(
                f"  - {change.task_id}: {', '.join(change.fields)}{suffix}"
            )
    if missing:
        lines.append(
            f"- not in the proposal, kept as is ({len(missing)}): "
            f"{', '.join(missing)}"
        )
    lines.append(
        "- every proposed task is draft; approval stays yours (factory approve)"
    )
    # F-67 AC-3: possibly shipped — FIRST, before the overrides and the
    # diff. Flagged, never decided: the owner drops, re-scopes or
    # approves each one; nothing here changes a task.
    lines += ["", "## Possibly shipped", ""]
    if not plan.shipped:
        lines.append("- none flagged")
    for flag in plan.shipped:
        suffix = ""
        if flag.evidence_found is False:
            suffix = " [evidence not found]"
        lines.append(
            f"- {show(flag.task_id)}: {show(flag.evidence)} — "
            f"{show(flag.reason)}{suffix} (flagged for you, never decided; "
            "still draft)"
        )
    # F-67 AC-4: the dropped criteria, after possibly_shipped, with the
    # still-in-docs ones marked — those block the apply until you accept
    # each drop, by name.
    lines += ["", "## Dropped criteria", ""]
    if plan.reference_note:
        lines.append(f"- ({plan.reference_note})")
    if not plan.dropped:
        lines.append("- none dropped" if not plan.reference_note else "- none")
    else:
        lines.append(
            "- every drop below applies only on your explicit per-drop "
            "acceptance"
        )
    for drop in plan.dropped:
        marks = []
        if drop.cited_done:
            marks.append("cited line marks done")
        if drop.own_source:
            marks.append("cited doc is the task's own source")
        if drop.still_in_docs:
            marks.append("still in docs")
        mark = f" [{'; '.join(marks)}]" if marks else ""
        lines.append(
            f"- {show(drop.label)}: dropped — {show(drop.reason)} "
            f"(doc {show(drop.doc)}:{drop.line}){mark}"
        )
    # R1-1: every mapping that changed a criterion's text — visible,
    # escaped, capped; the count is stated so nothing hides behind the
    # cap (the rest is the proposal's from fields against the
    # reference queue).
    lines += ["", "## Rewritten criteria", ""]
    if not plan.rewritten:
        lines.append(
            "- none (every mapped criterion carries its reference text)"
        )
    for item in plan.rewritten[:MAX_SHOWN_REWRITTEN]:
        what = "+".join(item.changed) if item.changed else "text"
        parts = []
        if "text" in item.changed:
            parts.append(
                f"text: {show(item.ref_text)} -> {show(item.new_text)}")
        if "check" in item.changed:
            parts.append(
                f"check: {show(_check_repr(item.ref_check))} -> "
                f"{show(_check_repr(item.new_check))}")
        lines.append(f"- {show(item.ref_key)} [{what}]: " + "; ".join(parts))
    if len(plan.rewritten) > MAX_SHOWN_REWRITTEN:
        lines.append(
            f"- (+{len(plan.rewritten) - MAX_SHOWN_REWRITTEN} more "
            "rewritten — compare the reference queue's acceptance texts "
            "with the proposal's from-mapped criteria to see every one)"
        )
    # F-67 AC-1: the forced gate overrides, before the diff — a
    # relaxation is never silently dropped. F-71 adds the risk flags
    # the parent added and the budget fields it clamped up to the
    # floor, in the SAME section, same order.
    lines += ["", "## Gate overrides", ""]
    if not (plan.gate_overrides or plan.risk_additions
            or plan.budget_clamps):
        lines.append("- none (v0 forces human gates; nothing was changed)")
    for override in plan.gate_overrides:
        origin = "new task" if override.new_task else "existing task"
        forced = (True if override.field == "gates.spec_approval"
                  else "human")
        stored = (f", stored {override.stored!r}"
                  if override.stored is not None else "")
        lines.append(
            f"- {show(override.task_id)}: {override.field} proposed "
            f"{override.proposed!r} -> forced {forced!r} ({origin}{stored})"
        )
    # F-71 AC-1: every risk flag the parent added — the conservative
    # direction; never a removal (that would be the session's).
    for addition in plan.risk_additions:
        origin = "new task" if addition.new_task else "existing task"
        reach = ", ".join(addition.reaching)
        lines.append(
            f"- {show(addition.task_id)}: risk +{show(addition.flag)} "
            f"(the scope reaches {show(reach)}; {origin})"
        )
    # F-71 AC-3: every budget field clamped UP to the floor — never down.
    for clamp in plan.budget_clamps:
        lines.append(
            f"- {show(clamp.task_id)}: {clamp.field} {clamp.proposed} -> "
            f"{clamp.clamped_to} (clamped up to the profile's budget floor)"
        )
    if plan.truncated_docs:
        # F-49 AC-3 (m-4): the proposal was built from a capped view —
        # the owner confirming it must know which docs the session never
        # saw, because no task defined only in them could have been
        # proposed.
        lines += [
            "",
            "## Planning docs not listed",
            "",
            f"The session was shown the first {MAX_DOC_FILES} planning "
            f"docs; {len(plan.truncated_docs)} more exist and were not "
            "listed, so no task defined only in these could have been "
            "proposed:",
        ]
        lines.extend(f"- {name}" for name in plan.truncated_docs)
    lines += ["", "## Out-of-scope matches", ""]
    if not plan.matches:
        lines.append("- none")
    for match in plan.matches:
        lines.append(
            f"- {match.task_id} resembles {match.record} — still proposed as "
            "draft; you confirm the match (nothing is dropped or rejected "
            "automatically)"
        )
    lines += ["", "## Full diff", ""]
    if plan.diff:
        lines += ["```diff", plan.diff.rstrip("\n"), "```"]
    else:
        lines.append("No changes — the proposal matches the queue.")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------- F-67: reference reading --


def read_reference_queue(repo: Path | str) -> dict | None:
    """The repo's existing ``.factory/tasks.json``, read as PLAIN JSON.

    The reference the criteria accounting checks against — even when the
    file fails the current schema (the F-13 pilot's landed queue
    predates F-64's bounds), and even when ``--queue`` points somewhere
    else: an import into a fresh queue file is checked against the
    queue the repo already carries. A missing file reads as None (the
    check has nothing to account for); a file that is not JSON, nests
    too deeply, or is larger than :data:`MAX_REFERENCE_BYTES` REFUSES
    the import — fail-closed, never a silent skip, never a crash, and
    the read never writes a byte back.

    Raises:
        ImportRefused: The reference file exists but cannot be read as
            a bounded plain-JSON queue (not JSON, wrong shape, too
            large, nested too deeply).
    """
    path = Path(repo) / ".factory" / "tasks.json"
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImportRefused(
            f"{path}: the reference queue cannot be read ({exc}) — the "
            "criteria accounting (F-67) refuses to run blind"
        ) from exc
    if size > MAX_REFERENCE_BYTES:
        raise ImportRefused(
            f"{path}: the reference queue is {size} bytes; the criteria "
            f"accounting reads at most {MAX_REFERENCE_BYTES} — a queue "
            "that size is not a queue this import can account against"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImportRefused(
            f"{path}: the reference queue cannot be read ({exc})"
        ) from exc
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ImportRefused(
            f"{path}: the reference queue is not valid JSON ({exc}) — the "
            "criteria accounting (F-67) refuses to run blind"
        ) from exc
    depth = _max_nesting(doc)
    if depth > MAX_PROPOSAL_DEPTH:
        raise ImportRefused(
            f"{path}: the reference queue nests {depth} levels deep; the "
            f"accounting reads at most {MAX_PROPOSAL_DEPTH}"
        )
    if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
        raise ImportRefused(
            f"{path}: the reference queue has no 'tasks' list — the "
            "criteria accounting (F-67) refuses to run blind"
        )
    return doc


def read_kept_doc_texts(
    repo: Path | str, kept: "Sequence[str]",
) -> dict[str, str]:
    """The kept planning docs' texts, bounded per file — the accounting's
    doc-line existence checks and still-in-docs search run over these.

    Docs the listing cap dropped are absent by design: a drop citing
    one refuses (the session was never shown it). A doc that cannot be
    read refuses the import, never skips — a missing text would make
    the doc-line check vacuous.
    """
    repo = Path(repo)
    texts: dict[str, str] = {}
    for name in kept:
        path = repo / name
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_DOC_TEXT_BYTES + 1)
        except OSError as exc:
            raise ImportRefused(
                f"{name}: a kept planning doc cannot be read ({exc}) — "
                "the criteria accounting (F-67) refuses to run blind"
            ) from exc
        try:
            texts[name] = raw[:MAX_DOC_TEXT_BYTES].decode(
                "utf-8", errors="replace"
            )
        except (UnicodeDecodeError, ValueError) as exc:  # pragma: no cover
            raise ImportRefused(
                f"{name}: a kept planning doc cannot be decoded ({exc})"
            ) from exc
    return texts


def _norm_text(value: str) -> str:
    """The still-in-docs matcher's normalisation: whitespace collapsed,
    case folded — doc wrapping and casing must not hide a carried
    criterion. (The input is the OWNER's reference/doc text, never
    session text — named to say so.)"""
    return " ".join(value.split()).casefold()


def _line_text(doc_text: str, line: int) -> str:
    lines = doc_text.splitlines()
    return lines[line - 1] if 1 <= line <= len(lines) else ""


# ------------------------------------------------- F-67: the accounting --


def _validate_possibly_shipped(
    proposal: dict, task_ids: "set[str]", repo: Path | None,
) -> list[ShippedFlag]:
    """Validate the proposal's ``possibly_shipped`` list (F-67 AC-3).

    Every entry must name a task the proposal carries, with non-empty
    repo-relative evidence and a one-line reason; the count may not
    exceed the proposal's own task count. Evidence that escapes the
    repo (``..``, an absolute path) refuses; a path-shaped evidence that
    names no file in the repo is FLAGGED ``evidence not found`` — the
    owner judges it, the parent does not refuse it.

    Raises:
        ImportRefused: A malformed entry, an unknown task id, an
            escaping evidence path, or a count past the proposal's own
            task count.
    """
    raw = proposal.get("possibly_shipped")
    if raw is None:
        return []
    tasks = proposal.get("tasks") or []
    if len(raw) > len(tasks):
        raise ImportRefused(
            f"possibly_shipped lists {len(raw)} entries for a proposal of "
            f"{len(tasks)} task(s) — every entry must name a task the "
            "proposal carries"
        )
    flags: list[ShippedFlag] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict) or set(entry) - {
            "task_id", "evidence", "reason",
        } or not {"task_id", "evidence", "reason"} <= set(entry):
            raise ImportRefused(
                f"possibly_shipped entry #{index} is malformed — each "
                "entry is an object with exactly task_id, evidence and "
                "reason"
            )
        task_id, evidence, reason = (
            entry["task_id"], entry["evidence"], entry["reason"])
        if task_id not in task_ids:
            known = ", ".join(sorted(task_ids)[:10]) or "none"
            raise ImportRefused(
                f"possibly_shipped entry #{index} names task {task_id!r}, "
                f"which the proposal does not carry (proposed: {known})"
            )
        if (not isinstance(evidence, str) or not evidence.strip()
                or not isinstance(reason, str) or not reason.strip()):
            raise ImportRefused(
                f"possibly_shipped entry #{index} (task {task_id}) carries "
                "an empty evidence or reason — name where the work lives "
                "and why it looks shipped"
            )
        if evidence.startswith("/") or ".." in evidence or "\x00" in evidence:
            raise ImportRefused(
                f"possibly_shipped entry #{index} (task {task_id}): "
                f"evidence {evidence[:80]!r} escapes the repository — "
                "evidence is repo-relative (a path, a path:line, or a "
                "commit sha)"
            )
        found: bool | None = None
        if repo is not None and not any(ch.isspace() for ch in evidence):
            candidate = repo / evidence.split(":", 1)[0]
            found = candidate.exists()
        flags.append(ShippedFlag(
            task_id=task_id, evidence=evidence, reason=reason,
            evidence_found=found,
        ))
    return flags


def _account_reference(
    proposed_ids: "set[str]",
    proposed_ac_ids: "dict[str, set]",
    predecessors: "dict[str, list[str]]",
    froms: "set[str]",
    reference: dict | None,
    dropped_raw: Any,
    doc_texts: dict[str, str] | None,
) -> "tuple[list[DropEntry], str]":
    """The deterministic criteria accounting (F-67 AC-4).

    Every reference task must be accounted — proposed under its own id,
    named in some proposed task's ``predecessors``, or dropped whole
    with a doc line — and every one of its acceptance criteria must be
    mapped (a proposed criterion's ``from``, or the same-id carry: a
    reference task re-proposed under its own id keeps a criterion when
    the proposed task carries the same AC id) or dropped with a doc
    line. Anything unaccounted refuses the proposal, naming the
    reference task and AC. The accounting never trusts the session's
    word for completeness: it is computed over the reference itself.

    Each drop's cited ``doc:line`` must exist in a KEPT planning doc
    (the ``doc_texts`` map — docs the listing cap dropped are not in
    it, so a drop citing one refuses). ``still_in_docs`` is then decided
    mechanically: the reference criterion's text, normalised
    (:func:`_norm_text`), found in any kept doc — EXCEPT when the
    drop's own cited line carries a done-marker (:data:`_DONE_MARKER`)
    and quotes the criterion (the doc itself says the work shipped).
    Flagged drops block :func:`apply_import` until the owner accepts
    each one.

    Failure modes, stated (the rule is exact-text, whole-doc):
    a paraphrased criterion still carried by the docs is NOT flagged
    (false negative — the drop still renders, so the owner sees it);
    a done-marked quote of the criterion exempts the drop even if
    another unmarked copy exists elsewhere (rare; the exemption needs
    both the marker and the criterion text on the cited line); a
    criterion whose text survives only past the per-doc read cap is
    missed (docs that large do not exist in practice).

    Raises:
        ImportRefused: A drop names an unknown reference task or AC, a
            doc outside the kept set, a line past the doc's end, or a
            reference task/criterion is unaccounted.
    """
    if reference is None:
        return [], (
            "no reference queue exists under .factory/ — there was nothing "
            "to account for"
        )
    doc_texts = doc_texts or {}
    ref_tasks: dict[str, dict] = {}
    for entry in reference.get("tasks") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ref_tasks[entry["id"]] = entry

    # A from naming something the reference does not carry is a broken
    # mapping — refused by name, never silently unmatched (a bogus from
    # would otherwise leave the real criterion unaccounted behind it).
    for from_ref in sorted(froms):
        ref_id, _, ac_id = from_ref.partition("/")
        if ref_id not in ref_tasks \
                or ac_id not in _ref_ac_ids(ref_tasks[ref_id]):
            raise ImportRefused(
                f"a proposed criterion's from names {from_ref!r}, which "
                "the reference queue does not carry — a from maps a real "
                "reference task and criterion"
            )

    # Validate the drops against the reference and the kept docs first.
    drops: list[DropEntry] = []
    for index, entry in enumerate(dropped_raw or [], start=1):
        if not isinstance(entry, dict):
            raise ImportRefused(
                f"dropped_criteria entry #{index} is not an object"
            )
        ref_task = entry.get("ref_task")
        if ref_task not in ref_tasks:
            raise ImportRefused(
                f"dropped_criteria entry #{index} names reference task "
                f"{ref_task!r}, which the reference queue does not carry"
            )
        ref_ac = entry.get("ref_ac")
        ref_acs = _ref_ac_ids(ref_tasks[ref_task])
        if ref_ac is not None and ref_ac not in ref_acs:
            raise ImportRefused(
                f"dropped_criteria entry #{index} names {ref_task}/"
                f"{ref_ac}, which the reference task does not carry"
            )
        doc, line, reason = (
            entry.get("doc"), entry.get("line"), entry.get("reason"))
        if doc not in doc_texts:
            raise ImportRefused(
                f"dropped_criteria entry #{index} ({ref_task}) cites doc "
                f"{doc!r}, which is not a kept planning doc of this "
                "import (docs the listing cap dropped cannot back a drop)"
            )
        line_count = len(doc_texts[doc].splitlines())
        if not isinstance(line, int) or isinstance(line, bool) \
                or not 1 <= line <= line_count:
            raise ImportRefused(
                f"dropped_criteria entry #{index} ({ref_task}) cites "
                f"{doc}:{line!r}, but the doc has {line_count} line(s) — "
                "a drop must cite a line that exists"
            )
        drops.append(DropEntry(
            ref_task=ref_task, ref_ac=ref_ac, doc=doc, line=line,
            reason=reason if isinstance(reason, str) else "",
            still_in_docs=False,
        ))

    # The mapping sets: criterion keys are "<ref task>/<ref AC id>".
    mapped = set(froms)
    dropped_keys = {f"{d.ref_task}/{d.ref_ac}" for d in drops if d.ref_ac}
    whole_dropped = {d.ref_task for d in drops if d.ref_ac is None}
    unaccounted: list[str] = []
    for ref_id, ref_task in ref_tasks.items():
        if not (
            ref_id in proposed_ids
            or ref_id in whole_dropped
            or any(ref_id in deps for deps in predecessors.values())
        ):
            unaccounted.append(f"{ref_id} (the whole task)")
            continue
        if ref_id in whole_dropped:
            continue  # a whole-task drop carries every criterion
        for ac_id in _ref_ac_ids(ref_task):
            key = f"{ref_id}/{ac_id}"
            if key in mapped or key in dropped_keys:
                continue
            if ref_id in proposed_ids and ac_id in proposed_ac_ids.get(
                    ref_id, set()):
                continue  # the same-id carry: the criterion survives
            unaccounted.append(key)
    if unaccounted:
        raise ImportRefused(
            "the proposal leaves reference criteria unaccounted — every "
            "reference task and criterion must be mapped (predecessors "
            "plus a criterion's from), re-proposed under its own id, or "
            "listed in dropped_criteria with a doc line: "
            + "; ".join(unaccounted[:MAX_SHOWN_VIOLATIONS])
        )

    # R2-1: no exemption — the advisory labels the owner decides with
    # are computed here, and the apply gate asks for every drop.
    docs_norm = {
        name: _norm_text(doc_text)
        for name, doc_text in doc_texts.items()
    }
    flagged: list[DropEntry] = []
    for drop in drops:
        ref_task = ref_tasks[drop.ref_task]
        cited_line = _line_text(doc_texts[drop.doc], drop.line)
        own_source_doc = str(ref_task.get("source", "")).partition("#")[0]
        criteria = _ref_ac_texts(ref_task, drop.ref_ac)
        carried = any(
            _norm_text(criterion) in doc_norm
            for criterion in criteria
            for doc_norm in docs_norm.values()
        )
        flagged.append(dataclasses_replace(
            drop,
            still_in_docs=carried,
            cited_done=bool(_DONE_MARKER.search(cited_line)),
            own_source=drop.doc == own_source_doc,
        ))
    return flagged, ""


def _check_changed(ref_check: "dict | None",
                   new_check: "dict | None") -> bool:
    """Did the mapping change the criterion's enforcement (R2-2)? The
    check TYPE, or — for test checks, the only kind that enforces
    anything mechanical — its REF. Manual-to-manual rewording of the
    expectation is a review note, not a lost enforcement."""
    ref_type = (ref_check or {}).get("type")
    new_type = (new_check or {}).get("type")
    if ref_type != new_type:
        return True
    if ref_type == "test":
        return (ref_check or {}).get("ref") != (new_check or {}).get("ref")
    return False


def _rewritten_criteria(
    from_triples: "list[tuple[str, str, dict | None]]",
    proposed_tasks: list[dict],
    reference: dict | None,
) -> list[RewrittenCriterion]:
    """Every mapping that changes a criterion's TEXT or CHECK
    (R1-1/R2-2).

    The accounting proves each reference criterion is claimed; this
    shows the owner what each claim did to its MEANING and its
    ENFORCEMENT: every mapped pair — an explicit ``from``, or the
    same-id carry (same task id + same AC id) — whose proposed text,
    normalised (:func:`_norm_text`), differs from the reference text,
    or whose check changed (:func:`_check_changed`: the type, or a
    test check's ref — a test-backed criterion turned manual keeps its
    words and loses its enforcement). Not blocking; rendered escaped
    in the summary's Rewritten section with what changed named, capped,
    with the count stated.
    """
    if reference is None:
        return []
    ref_texts: dict[str, str] = {}
    ref_checks: dict[str, "dict | None"] = {}
    ref_ids: set = set()
    for entry in reference.get("tasks") or []:
        if not isinstance(entry, dict):
            continue
        ref_id = entry.get("id")
        ref_ids.add(ref_id)
        for item in entry.get("acceptance") or []:
            if not isinstance(item, dict):
                continue
            key = f"{ref_id}/{item.get('id')}"
            if isinstance(item.get("text"), str):
                ref_texts[key] = item["text"]
            check = item.get("check")
            ref_checks[key] = check if isinstance(check, dict) else None
    triples = list(from_triples)
    for task in proposed_tasks:
        if task.get("id") not in ref_ids:
            continue
        for item in task.get("acceptance") or []:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            text = item.get("text")
            check = item.get("check")
            triples.append((
                f"{task['id']}/{item.get('id')}",
                text if isinstance(text, str) else "",
                check if isinstance(check, dict) else None,
            ))
    seen: set[tuple[str, str, str]] = set()
    rewritten: list[RewrittenCriterion] = []
    for ref_key, new_text, new_check in triples:
        ref_text = ref_texts.get(ref_key)
        if ref_text is None:
            continue
        text_changed = _norm_text(ref_text) != _norm_text(new_text)
        check_changed = _check_changed(
            ref_checks.get(ref_key), new_check)
        if not text_changed and not check_changed:
            continue
        marker = (ref_key, _norm_text(new_text), str(new_check))
        if marker in seen:
            continue
        seen.add(marker)
        rewritten.append(RewrittenCriterion(
            ref_key=ref_key, ref_text=ref_text, new_text=new_text,
            ref_check=ref_checks.get(ref_key), new_check=new_check,
            changed=("text", "check") if text_changed and check_changed
            else ("text",) if text_changed else ("check",),
        ))
    return rewritten


def _ref_ac_ids(ref_task: dict) -> "set":
    """The reference task's acceptance ids (garbage entries skipped)."""
    return {
        item.get("id") for item in ref_task.get("acceptance") or []
        if isinstance(item, dict)
    }


def _ref_ac_texts(ref_task: dict, ref_ac: str | None) -> "list[str]":
    """The reference criterion texts a drop covers: one AC's, or every
    AC's for a whole-task drop (empty texts skipped)."""
    return [
        item["text"] for item in ref_task.get("acceptance") or []
        if isinstance(item, dict)
        and isinstance(item.get("text"), str) and item["text"].strip()
        and (ref_ac is None or item.get("id") == ref_ac)
    ]


# -------------------------------------------------------------------- write --


def apply_import(
    plan: ImportPlan,
    queue_path: Path | str,
    *,
    confirm: Callable[[], bool],
    confirm_drop: "Callable[[DropEntry], bool] | None" = None,
) -> bool:
    """Write the plan's merged document — only on an explicit confirm.

    An existing queue is written through :class:`TaskQueue.save`
    (validation, the writer lock, the append-only attempts check); a
    fresh queue is validated once more and written atomically UNDER THE
    QUEUE WRITER LOCK (:func:`factory.state.queue_writer_lock` — F-49
    AC-4, m-6): the "no queue exists" decision and the write happen
    inside the same lock every other queue writer takes, so two
    concurrent fresh imports serialise — one writes, the other finds the
    winner's queue under the lock and refuses instead of overwriting it.
    Either way the queue on disk must still be the one the plan was
    built from (``plan.base_doc``): a change made in between — an
    approval, a status move, a queue created by another writer — refuses
    the write rather than being overwritten with the stale plan.

    F-67 AC-4 + R1-1 + R2-1: EVERY dropped criterion blocks the write
    until the owner explicitly accepts THAT drop — per drop, by name,
    with no exemption (the cited line's done-marker and the task's own
    source doc are advisory evidence in the summary, never a bypass).
    A declined or unanswered drop (no ``confirm_drop`` callable) refuses
    the write; there is deliberately no blanket acceptance.

    Args:
        plan: The plan to apply.
        queue_path: The queue file.
        confirm: Called before anything is written; False writes nothing.
        confirm_drop: Called once per dropped criterion with the entry
            itself; any False refuses the write.

    Returns:
        True if the queue was written, False if the confirm declined.

    Raises:
        ImportRefused: The queue changed since the plan was built, or a
            drop was not accepted.
        OSError: The write itself failed (the previous contents, if any,
            are intact).
    """
    if not confirm():
        return False
    for drop in plan.dropped:
        if confirm_drop is None or not confirm_drop(drop):
            raise ImportRefused(
                f"the proposal drops {drop.label} (cited {drop.doc}:"
                f"{drop.line}) — every drop applies only on your "
                "explicit per-drop acceptance (re-propose the criterion "
                "instead, if it should survive)"
            )
    path = Path(queue_path)
    stale = ImportRefused(
        f"{path}: the queue changed since this import was planned (an "
        "approval, a status move or another writer); nothing written — "
        "re-run factory import to plan against the current queue"
    )
    if path.is_file():
        if plan.base_doc is None:
            raise stale
        queue = TaskQueue(path)
        # The baseline check: the queue this instance loaded must be the
        # one the plan was built from. From here on save()'s own
        # concurrent-write check (against this load) covers the rest of
        # the window up to the locked write.
        if canonical_json(queue.doc) != canonical_json(plan.base_doc):
            raise stale
        queue.doc["tasks"] = copy.deepcopy(plan.proposed_doc["tasks"])
        # save() validates the whole document, takes the writer lock,
        # re-reads the disk queue and enforces the append-only attempts
        # rule — the same guarantees every other writer goes through.
        queue.save()
    else:
        if plan.base_doc is not None:
            raise stale  # the queue the plan merged into is gone
        doc = copy.deepcopy(plan.proposed_doc)
        doc["updated_at"] = now_iso()
        validate_queue(doc, str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        # F-49 AC-4 (m-6): the lock TaskQueue.save serialises on, taken
        # across the check and the write. The re-check inside the lock is
        # the race's loser detector — a queue that appeared meanwhile was
        # created by another writer, and overwriting it would silently
        # drop that writer's fresh queue.
        with queue_writer_lock(path):
            if path.is_file():
                raise stale  # created by another writer while we planned
            # The package's own atomic writer (factory.state); a fresh
            # queue has no TaskQueue instance yet, so the temp+rename
            # write is used directly — same-package use of the one writer
            # primitive, now under the one writer lock.
            _atomic_write_json(path, doc)
    return True


# ------------------------------------------------- refused output kept --


def _refusal_suffix(text: str) -> str:
    """``.json`` when the session's output is JSON (fence stripped, as
    :func:`parse_proposal` reads it), ``.txt`` when it is not — the
    extension says what there is to fix.

    The same parse failures :func:`parse_proposal` converts (deep
    nesting's RecursionError, the huge-int ValueError) are caught here
    too: the keep-refused path must never crash on the very input that
    made it necessary (F-49 AC-5)."""
    stripped = text.strip()
    match = _FENCE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    try:
        json.loads(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return ".txt"
    return ".json"


def _save_refused_output(
    text: str, project: str, factory_root: Path | str | None,
    *, secrets: "Sequence[str]" = (),
) -> Path:
    """Keep the importer session's raw output for a refused proposal.

    The sandboxed session's run dir is deleted on every exit path, so
    without this a refused proposal cost the whole session and left
    nothing to fix and re-review with ``factory import --proposal``.
    The copy passes the F-43 egress seam
    (:func:`factory.egress.scrub_session_text` with the caller's
    ``secrets`` — review round 1, finding 2: pattern redaction alone was
    the exact M-8 gap; the real Z.ai key is ``<id>.<secret>``, no shape
    catches it, only the literal scrub does) and is written with mode
    0600 under ``<factory-root>/import/`` as
    ``<project>-<run id>.proposal.json`` (``.txt`` when the output is
    not JSON). The id is :func:`factory.runs.new_run_id` — a UTC stamp
    plus 6 uuid hex, unique per mint (ids minted outside a run are its
    documented use) — so one create-only open suffices.

    Raises:
        OSError: The factory root is unwritable — the caller keeps the
            original refusal; the save never masks it.
        TypeError: ``secrets`` is a bare ``str`` or a non-str element
            (round 3): a programming error that deliberately wins over
            the diagnostic being saved — raised while the original is
            being handled, it stays reachable as the ``__context__``.
    """
    secrets = check_secrets(secrets)
    root = Path(factory_root) if factory_root is not None else FACTORY_ROOT_DEFAULT
    directory = root / "import"
    directory.mkdir(parents=True, exist_ok=True)
    # F-62 AC-3: the project component is validated, never sanitised —
    # a sanitised ``../x`` would silently move the saved refusal; the
    # queue that named it is state the owner must see refused.
    safe = project_component(project)
    path = directory / f"{safe}-{new_run_id()}.proposal{_refusal_suffix(text)}"
    # O_EXCL: create-only, never overwriting an earlier refusal; the
    # mode is set at creation, not by a later chmod.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(scrub_session_text(text, secrets))
    return path


def _validated_usage(usage: Any) -> "dict[str, Any] | None":
    """The F-64 telemetry rule over a record's usage block: each of the
    four counts is a non-bool int >= 0 or None (unknown) — anything else
    a forged record carries renders unknown downstream, never raw and
    never 0. None when there is no usable block at all."""
    if not isinstance(usage, dict):
        return None

    def count(name: str) -> Any:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) \
                or value < 0:
            return None
        return value

    block = {name: count(name)
             for name in ("input", "cache_read", "cache_write", "output")}
    return block if any(v is not None for v in block.values()) else None


def _write_session_record(
    *,
    factory_root: Path | str | None,
    project: str,
    profile: dict | None,
    run_id: str | None,
    record: "SessionRecord | None",
    kind: str,
    outcome: str,
    proposal_file: str | None,
    secrets: "Sequence[str]",
) -> Path | None:
    """Write one import session's record (F-67 AC-2) under
    ``<factory-root>/import/`` as ``<project>-<run id>.record.json`` —
    the refused-output writer's naming, mode 0600 and create-only open.

    The provider is the PARENT's resolution
    (:func:`factory.provider.resolve_provider` over the profile,
    role ``importer``) — never the session-written ``record.provider``
    (the F-59 R2-1 rule: the record file is session-writable, so its
    provider claim is evidence of nothing). The model is what the
    session reported, or the string ``unknown`` — never a bare null.
    Usage passes the F-64 rule (:func:`_validated_usage`); turns pass
    :func:`_record_turns`. The whole payload passes the egress seam
    (:func:`scrub_session_value` with the run's secrets) before the
    write: a credential echoed anywhere in the session's reported fields
    must not reach the record. ``run_id`` None means no session existed
    (nothing was spent) — nothing is written.

    Returns:
        The written path, or None when nothing was written (no run id,
        or the write failed — a failed record never masks the outcome
        it records).
    """
    if not run_id:
        return None
    provider: dict[str, Any]
    try:
        provider = resolve_provider(
            profile or {}, "importer").provider_record()
    except ProviderError:
        provider = {"kind": "unknown"}
    model = getattr(record, "model", None) if record is not None else None
    payload = {
        "kind": kind,
        "project": project,
        "run_id": run_id,
        "provider": provider,
        # the session-reported model (never null), or "unknown"
        "model": model if isinstance(model, str) and model else "unknown",
        "usage": _validated_usage(
            getattr(record, "usage", None) if record is not None else None),
        "turns": _record_turns(record) if record is not None else None,
        "outcome": outcome,
        "proposal_file": proposal_file,
        "written_at": now_iso(),
    }
    payload = scrub_session_value(payload, secrets)
    root = Path(factory_root) if factory_root is not None \
        else FACTORY_ROOT_DEFAULT
    directory = root / "import"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        safe = project_component(project)
        path = directory / f"{safe}-{run_id}{RECORD_SUFFIX}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False)
                         + "\n")
        return path
    except OSError:
        # The record is telemetry, never a gate: a failed write must not
        # mask the outcome it records (the refusal or summary still
        # reaches the owner).
        return None


def _import_fix_command(
    saved: Path, repo: Path, queue_path: Path, docs_dir: str
) -> str:
    """The ``factory import`` command that re-reviews the saved file
    against the same repo the refused run used.

    ``--repo`` is always spelled out (absolute, shell-quoted): the CLI
    defaults it to the current directory, so a bare command pasted from
    the factory checkout reviews against the wrong repo. ``--queue``
    and ``--docs`` ride along only when the refused run used non-default
    values, so the default case stays paste-clean.
    """
    parts = [
        "factory", "import",
        "--proposal", shlex.quote(str(saved)),
        "--repo", shlex.quote(str(repo.absolute())),
    ]
    if queue_path != repo / ".factory" / "tasks.json":
        parts += ["--queue", shlex.quote(str(queue_path))]
    if docs_dir != DEFAULT_DOCS_DIR:
        parts += ["--docs", shlex.quote(docs_dir)]
    return " ".join(parts)


def keep_refused_import_output(
    exc: Exception, *, text: str, project: str, repo: Path,
    queue_path: Path, docs_dir: str, factory_root: Path | str | None,
    secrets: "Sequence[str]" = (),
) -> Exception:
    """Keep a real session's refused output; return the refusal to raise.

    One seam for every refusal after a session returns: run_import's
    parse/plan/apply guard and the CLI's own apply guard both go through
    here, so no path throws an expensive proposal away — a stale queue
    found at apply time (the session already ran) loses the output
    exactly like a parse refusal does. ``secrets`` (the seat token, in
    the caller's hand on both paths) rides into the egress seam the
    saved copy passes through — review round 1, finding 2.

    An :class:`ImportRefused` comes back with the saved file's path and
    the full re-review command in its message. Any other exception (a
    write ``OSError``) keeps its type — its callers already refuse on
    it — and the saved path rides along as a note on the exception; a
    failed save never masks the original refusal. A :class:`TypeError`
    from the secrets guard (round 3: a bare str or a bad element) is a
    programming error and deliberately wins — but CHAINED to ``exc``
    (``raise … from exc``), so the refusal being kept stays visible as
    the ``__cause__``.
    """
    try:
        secrets = check_secrets(secrets)
    except TypeError as guard_error:
        raise guard_error from exc
    try:
        saved = _save_refused_output(text, project, factory_root, secrets=secrets)
    except OSError as save_exc:
        if isinstance(exc, ImportRefused):
            return ImportRefused(
                f"{exc} (the raw output could not be saved: {save_exc})"
            )
        return exc
    command = _import_fix_command(saved, repo, queue_path, docs_dir)
    if isinstance(exc, ImportRefused):
        refusal = ImportRefused(
            f"{exc}; saved to {saved}; fix it and review with {command}"
        )
        # F-67 AC-2: the kept file's name rides the exception out — the
        # caller's session-record write names it without re-parsing the
        # message.
        refusal.saved_path = saved.name
        return refusal
    exc.add_note(f"raw importer output saved to {saved}; "
                 f"re-review with {command}")
    return exc


# ------------------------------------------------------- abort salvage --


def _scratch_proposal_path(settings_path: Path) -> Path:
    """The session's ``$TMPDIR/proposal.json``: the writable scratch the
    #94 self-check prompt tells the session to write its finished JSON
    to — the ``tmp`` dir under the scratch sibling of the run's srt
    settings (``<settings>-scratch/tmp``; session._reviewer_scratch /
    _scratch_dirs layout, cleaned with the run by ``cleanup_run``)."""
    scratch = settings_path.with_name(settings_path.name + "-scratch")
    return scratch / "tmp" / PROPOSAL_OUTPUT_NAME


def _abort_refusal(
    head: str, *, run: RunDir, project: str, factory_root: Path,
    repo: Path, queue_path: Path, docs_dir: str,
    secrets: "Sequence[str]" = (),
) -> "tuple[str, str | None]":
    """The refusal message for a session that did not complete, with the
    saved proposal file's name (F-67 AC-2 rides it onto the session
    record; None when nothing was kept).

    Before ``cleanup_run`` sweeps the scratch, the proposal the session
    had written so far — if any — is copied out through the one writer
    (:func:`_save_refused_output`: seam-scrubbed with the session's
    ``secrets``, mode 0600, O_EXCL, under ``<factory-root>/import/``),
    and the message names the file and the full re-review command.
    Without a file, the message says so explicitly — never a bare
    ``aborted`` that leaves the owner guessing what survived. A failed
    copy never masks the abort; a :class:`TypeError` from the secrets
    guard (round 3) is a programming error that deliberately wins, and —
    raised while the caller handles the original failure — the original
    stays reachable as the ``__context__``."""
    secrets = check_secrets(secrets)
    proposal = _scratch_proposal_path(run.settings_path)
    try:
        text = proposal.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"{head}; no proposal was written before the abort", None
    except (OSError, ValueError) as exc:
        return f"{head}; (its last proposal could not be read: {exc})", None
    try:
        saved = _save_refused_output(text, project, factory_root, secrets=secrets)
    except OSError as exc:
        return f"{head}; (its last proposal could not be saved: {exc})", None
    command = _import_fix_command(saved, repo, queue_path, docs_dir)
    return (f"{head}; its last proposal was saved to {saved}; "
            f"review it with {command}", saved.name)


# --------------------------------------------------- F-57: the ONE repair --


def repair_budget(max_turns: int, timeout_min: float) -> tuple[int, float]:
    """The ONE repair session's ``(max_turns, timeout_min)`` (F-57 AC-2):
    a quarter of the import's own budget, floored at
    :data:`REPAIR_TURN_FLOOR` turns / :data:`REPAIR_TIMEOUT_FLOOR`
    minutes so a small import still gets a real repair, and always
    inside :func:`validate_budget`'s ceilings by construction (it is a
    fraction of a budget those ceilings already bound).
    """
    return (
        max(REPAIR_TURN_FLOOR, int(max_turns) // REPAIR_BUDGET_FRACTION),
        max(REPAIR_TIMEOUT_FLOOR,
            round(float(timeout_min) / REPAIR_BUDGET_FRACTION, 1)),
    )


def build_repair_prompt(*, project: str, violations: str) -> str:
    """The ONE repair session's prompt (F-57 AC-2): the saved refused
    output, the FULL violation list and the output contract — never the
    planning docs. The repair's workspace is an empty repository
    (structurally: there is nothing to read — see
    :func:`_repair_refused_proposal`), so fixing needs none: everything
    the violations name is in the refused proposal itself.
    """
    interpreter = shlex.quote(sys.executable)
    proposal_ref = f"$TMPDIR/{PROPOSAL_OUTPUT_NAME}"
    schema_ref = f"$TMPDIR/{PROPOSAL_SCHEMA_NAME}"
    refused_ref = f"$TMPDIR/{REFUSED_OUTPUT_NAME}"
    parse_cmd = (
        f"{interpreter} -c \"import json; json.load(open('{proposal_ref}'))\""
    )
    shape_cmd = (
        f"{interpreter} -c \"import json, jsonschema; "
        f"d=json.load(open('{proposal_ref}')); "
        f"s=json.load(open('{schema_ref}')); "
        f"[print(e.json_path + ': ' + e.message) for e in "
        f"jsonschema.Draft202012Validator(s).iter_errors(d)]\""
    )
    lines = [
        "You are the factory's importer-repair seat. A refused importer",
        "proposal is repaired in ONE pass — this one.",
        "",
        "You have no access to the planning documents and must not try to",
        "read them: your workspace is an empty repository that holds none.",
        "Everything you need is in the refused proposal and the violations.",
        "",
        f"- the refused proposal: {refused_ref}",
        "- the proposal schema (narrowed to a proposal's own fields):",
        f"  {schema_ref}",
        "",
        "Fix ONLY what the violation list names. Do not invent, drop or",
        "rename tasks: a task the violations do not name passes through",
        "unchanged. The full violation list, verbatim:",
        "",
        *(f"  {line}" for line in violations.splitlines()),
        "",
        f'The proposal must name project exactly "{project}". Each task',
        "carries exactly these field shapes, generated from the factory's",
        "tasks schema (a proposal outside them is refused, however",
        "plausible it reads):",
    ]
    lines.extend(_schema_contract_lines(_load_tasks_schema()))
    lines += [
        "- gates in v0 are the safest value, whatever you propose (the",
        "  parent forces them and lists every override to the owner):",
        "- merge is always \"human\" and deploy is always \"human\" — the",
        "  owner merges and deploys; never propose an auto gate.",
        "- spec_approval is always true for build and docs tasks; other",
        "  kinds (owner_only, decision, research) keep what the proposal",
        "  says.",
        "- budget.size tops out at M: a task too big for size M is split",
        "  into slices or proposed as separate tasks, never sized above",
        "  the enum.",
        "- priority is P1–P3 only — the values listed above; P0 is not a",
        "  priority level this queue knows.",
        '- a check whose type is anything but "manual" must carry ref.',
        "- a TEST check's ref is EXACT (F-71): <repo-relative path>::",
        "  <test name>, one or more :: segments, never an absolute or ..",
        "  path, never an empty or whitespace-padded name. Approval and",
        "  this importer both refuse anything else. For a NEW test file",
        "  the convention is tests/test_<task>.py::test_ac_N.",
        "- a COMMAND check's ref is the full command, non-empty (F-71).",
        "- a task that adds a dependency declares it (F-71): dependencies",
        '  is a list of {"ecosystem": "pypi"|"npm", "name"} entries —',
        "  every package a direct manifest gains must be declared, or",
        "  the run escalates. Names are normalised (PEP 503 / lowercase).",
        "- Never set status, acceptance_hash, approved_at, approved_by,",
        "  attempts, links or out_of_scope. Every task is imported as",
        "  draft; approval is the owner's act (factory approve).",
        "- Carry the proposal's possibly_shipped and dropped_criteria",
        "  lists, and each task's predecessors and acceptance \"from\"",
        "  fields, through unchanged: they are the owner's accounting,",
        "  not violations — dropping them re-refuses the proposal.",
        "",
        "Before you answer, check your own output (the interpreter named",
        "below is the pinned one your sandbox already runs, and jsonschema",
        "is importable there):",
        f"- Write the corrected JSON to {proposal_ref}, in your writable",
        "  scratch (the repo cwd is read-only).",
        "- Parse check:",
        f"  {parse_cmd}",
        f"- Shape check against {schema_ref}:",
        f"  {shape_cmd}",
        "- Fix everything those print, re-check, and only then answer.",
        "  Your final answer is the JSON object itself, and nothing else.",
    ]
    return "\n".join(lines)


def _repair_workspace(root: Path, project: str, run_id: str) -> Path | None:
    """An EMPTY git repository for the repair session, under the
    project's own runs dir (created and swept under the queue's run
    lease, exactly like the import's clone). No planning docs,
    structurally: the session can read its workspace all it likes and
    find nothing of the repo the import read. One commit so the reviewer
    guard's git-state snapshot has a HEAD. None if git cannot make it
    (the repair is skipped, never wedged)."""
    # F-62 AC-3: a validated slug — this mkdir and the sandboxed
    # session it hosts must stay inside <root>/runs/<project>/.
    workspace = root / "runs" / project_component(project) / run_id
    workspace.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("-c", "user.name=factory-repair",
         "-c", "user.email=repair@factory.invalid",
         "commit", "-q", "--allow-empty", "-m", "repair workspace (empty)"),
    ):
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(workspace), *args],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            shutil.rmtree(workspace, ignore_errors=True)
            return None
    return workspace


def _record_turns(record: SessionRecord) -> int | None:
    """R2-2 (review round 2): a turn count the factory renders (summary,
    trace, the eval report) — only a non-bool int >= 0 counts; anything
    else a forged record carries renders as ``None`` ("turns unknown"),
    never raw. The repair already ran by the time its record is read, so
    a garbage count costs the number, not the session."""
    turns = record.num_turns
    if (
        isinstance(turns, bool)
        or not isinstance(turns, int)
        or turns < 0
    ):
        return None
    return turns


def _repair_not_started(original: Path) -> dict:
    """The facts of a repair that could not start: the original is
    kept, nothing was spent, nothing is claimed."""
    return {"ran": False, "outcome": "could_not_start",
            "original_saved": str(original)}


def _repair_refused_proposal(
    exc: ImportRefused,
    *,
    payload: str,
    project: str,
    repo: Path,
    queue_path: Path,
    docs_dir: str,
    factory_root: Path | str | None,
    profile: dict,
    records: list[dict],
    existing_doc: dict | None,
    import_turns: int,
    import_timeout: float,
    secrets: "Sequence[str]",
    reference: dict | None = None,
    doc_texts: dict[str, str] | None = None,
) -> "tuple[ImportPlan | None, dict, ImportRefused | None]":
    """Run the ONE repair session for a refused real-session proposal
    (F-57 AC-2). Returns ``(plan, facts, final_refusal)`` — exactly one
    of plan/final_refusal is meaningful.

    The order of operations is the contract: the original output is
    saved FIRST (the keep-refused writer); the repair then runs — in an
    empty repository (no doc re-reading, structurally), under a budget
    that is a fraction of the import's (:func:`repair_budget`), holding
    the queue's run lease like the import's own session does; and its
    answer goes through the SAME :func:`parse_proposal` → egress scrub →
    :func:`plan_import` chain the original faced — a repair is a second
    chance to be valid, never a second door. A still-refused repair (or
    one that dies, or hits quota — :func:`factory.orchestrator._is_quota`,
    the one quota seam, reused here as F-48 reused it) names BOTH saved
    files. Never a repair of the repair: this function is called once
    per import, from one place.

    A repair that cannot even START — no token, no sandbox, no run dir,
    an unsaveable original — returns ``(None, facts, None)`` (facts
    naming the kept original, or empty when even that failed) and the
    caller falls back to today's keep-refused path exactly: nothing was
    spent, so nothing is claimed.
    """
    # Deferred like eval.py's (F-48): the orchestrator is the seam's
    # owner; F-54 owns promoting it. Recorded in the F-57 ledger.
    from factory.orchestrator import _is_quota

    secrets = check_secrets(secrets)
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    # The original output is kept FIRST — the repair's whole premise is
    # that the expensive answer survives.
    try:
        original = _save_refused_output(
            payload, project, factory_root, secrets=secrets
        )
    except OSError:
        # The original could not be kept: the repair's premise failed
        # before anything was spent — today's refusal path instead.
        return None, {}, None
    run_id = new_run_id()
    writer = TraceWriter(root, project, run_id, "-")

    def _write_repair_record(
        outcome: str, record: "SessionRecord | None" = None,
    ) -> None:
        """The repair session's own record (F-67 AC-2) — written on
        every exit where a repair session attempt existed."""
        _write_session_record(
            factory_root=factory_root, project=project, profile=profile,
            run_id=run_id, record=record, kind="import_repair",
            outcome=outcome, proposal_file=original.name, secrets=secrets,
        )

    writer.append({
        "type": "import_repair", "outcome": "started",
        "original_saved": str(original),
    })
    r_turns, r_timeout = repair_budget(import_turns, import_timeout)
    queue = TaskQueue(queue_path) if queue_path.is_file() else None
    # F2 (review round 1): the lease is taken through an ExitStack so a
    # busy queue (RunInProgressError raises on ENTER) is a terminal trace
    # outcome plus the could-not-start fallback — never an error class
    # the CLI does not catch, and never a trace left at "started".
    lease_stack = contextlib.ExitStack()
    try:
        lease_stack.enter_context(
            queue.idle_lease() if queue is not None
            else contextlib.nullcontext()
        )
    except RunInProgressError:
        writer.append({"type": "import_repair", "outcome": "lease_busy"})
        return None, _repair_not_started(original), None
    final: ImportRefused | None = None
    plan: ImportPlan | None = None
    # original_saved rides every path (F-67 AC-2's record names it).
    facts: dict = {"original_saved": str(original)}
    run: RunDir | None = None
    with lease_stack:
        # The workspace is created AND swept inside the lease: reclaim
        # empties runs/<project>/ the moment the lease is free, exactly
        # as it would the import's own clone.
        workspace = _repair_workspace(root, project, run_id)
        if workspace is None:
            writer.append({"type": "import_repair",
                           "outcome": "workspace_failed"})
            # The workspace could not be made; nothing was spent. The
            # original is already on disk — the caller wraps the
            # refusal naming it, never saving a second copy.
            return None, _repair_not_started(original), None
        run = RunDir(project, run_id, workspace,
                     sandbox_settings_path(root, project, run_id))
        try:
            record = run_session_sandboxed(
                "reviewer",
                build_repair_prompt(project=project, violations=str(exc)),
                workspace, profile,
                factory_root=root, settings_path=run.settings_path,
                max_turns=r_turns, timeout_min=r_timeout,
                # F-59/R1-2: the reviewer SEAT config with the importer
                # ROLE's provider + model; the credential is looked up
                # by the resolver for THAT spec inside the constructor —
                # never passed raw, never crossed between kinds.
                role="importer",
                protected=list(
                    (profile.get("paths") or {}).get("protected") or []
                ),
                run_id=run_id,
                # F3 (review round 1): the settings half of "no doc
                # re-reading" — the source repo and its docs dir are
                # denied for reading wherever they live (the empty
                # workspace is the structural half; the home-wide deny
                # says nothing about a repo outside $HOME).
                deny_read=(
                    repo.resolve(), (repo / docs_dir).resolve(),
                ),
                scratch_files={
                    PROPOSAL_SCHEMA_NAME: json.dumps(
                        build_proposal_schema(_load_tasks_schema()), indent=2
                    ),
                    REFUSED_OUTPUT_NAME: payload,
                },
            )
        except SessionRecordError as session_exc:
            # The repair session RAN but its record never arrived
            # (F-56's failure class): the turns were spent, there is no
            # answer to validate, and the trace says so. Today's
            # refusal path carries the refusal.
            writer.append({
                "type": "import_repair", "outcome": "record_lost",
                "detail": str(session_exc)[:200],
            })
            _write_repair_record("record_lost")
            return None, _repair_not_started(original), None
        except (SessionConfigError, SandboxError, RunError) as session_exc:
            # The repair session could not START (no token, no bundled
            # CLI, srt missing, a run-dir fault): nothing was spent —
            # fall back to today's refusal path.
            writer.append({
                "type": "import_repair", "outcome": "could_not_start",
                "detail": str(session_exc)[:200],
            })
            return None, _repair_not_started(original), None
        except subprocess.TimeoutExpired as session_exc:
            # The repair session hung past its teardown grace: something
            # ran and died — say so, naming the kept original.
            writer.append({"type": "import_repair", "outcome": "failed"})
            _write_repair_record("failed")
            final = ImportRefused(
                f"{exc}; the repair session timed out after "
                f"{session_exc.timeout:.0f}s; the original output is "
                f"saved at {original}; review it with "
                f"{_import_fix_command(original, repo, queue_path, docs_dir)}"
            )
        else:
            # R3-1: the repair's record, validated the same way (the file
            # boundary is the primary; this catches object-carried
            # records) — the repair ran, so the original stays named.
            from factory.session import record_field_error

            field_error = record_field_error(record)
            if field_error is not None:
                writer.append({
                    "type": "import_repair", "outcome": "record_unusable",
                })
                _write_repair_record("record_unusable", record)
                final = ImportRefused(
                    f"{exc}; the repair session ran but its record is "
                    f"unusable (record field {field_error}); the original "
                    f"output is saved at {original}; review it with "
                    f"{_import_fix_command(original, repo, queue_path, docs_dir)}"
                )
            elif _is_quota(record):
                turns = _record_turns(record)
                writer.append({
                    "type": "import_repair", "outcome": "quota_stopped",
                    "turns": turns,
                })
                _write_repair_record("quota_stopped", record)
                kept = ""
                partial_text = record.result or ""
                if partial_text.strip():
                    with contextlib.suppress(OSError):
                        partial = _save_refused_output(
                            partial_text, project, factory_root,
                            secrets=secrets,
                        )
                        kept = (f"; the repair's partial output was saved "
                                f"to {partial}")
                final = ImportRefused(
                    f"{exc}; one repair session was attempted and stopped "
                    "on quota ("
                    + scrub_session_text(
                        f"{record.status}: {record.reason or ''}", secrets)
                    + f"); the original output is saved at {original}; "
                    f"review it with {_import_fix_command(original, repo, queue_path, docs_dir)}"
                    + kept
                )
            elif record.status != "completed":
                turns = _record_turns(record)
                writer.append({
                    "type": "import_repair", "outcome": "failed",
                    "turns": turns,
                })
                _write_repair_record("failed", record)
                final = ImportRefused(
                    f"{exc}; one repair session was attempted and ended "
                    + scrub_session_text(
                        f"{record.status}: {record.reason or ''}", secrets)
                    + f"; the original output is saved at {original}; "
                    f"review it with {_import_fix_command(original, repo, queue_path, docs_dir)}"
                )
            else:
                turns = _record_turns(record)
                text = record.result or ""
                try:
                    # EXACTLY the chain the original proposal faced —
                    # the same parse, the same scrub, the same plan
                    # (F-67: the same criteria accounting; F-71: the
                    # same pre-spend normalisation).
                    proposal = scrub_session_value(
                        parse_proposal(text), secrets
                    )
                    plan = plan_import(
                        existing_doc, proposal, records, project=project,
                        reference=reference, doc_texts=doc_texts, repo=repo,
                        profile=profile,
                    )
                except ImportRefused as second:
                    writer.append({
                        "type": "import_repair", "outcome": "still_refused",
                        "turns": turns,
                    })
                    _write_repair_record("refused", record)
                    try:
                        repair_saved = _save_refused_output(
                            text, project, factory_root, secrets=secrets
                        )
                        original_cmd = _import_fix_command(
                            original, repo, queue_path, docs_dir)
                        repair_cmd = _import_fix_command(
                            repair_saved, repo, queue_path, docs_dir)
                        final = ImportRefused(
                            f"{exc}; one repair session ran but its "
                            f"proposal was still refused: {second}; the "
                            f"original output is saved at {original} "
                            f"(review: {original_cmd}); the repair's "
                            f"output is saved at {repair_saved} "
                            f"(review: {repair_cmd})"
                        )
                    except OSError as save_exc:
                        final = ImportRefused(
                            f"{exc}; one repair session ran but its "
                            f"proposal was still refused: {second}; the "
                            f"original output is saved at {original}; the "
                            f"repair's output could not be saved "
                            f"({save_exc})"
                        )
                else:
                    writer.append({
                        "type": "import_repair", "outcome": "repaired",
                        "turns": turns,
                    })
                    _write_repair_record("repaired", record)
                    facts = {
                        "ran": True, "outcome": "repaired",
                        "turns": turns,
                        # the judged document, for the caller's keep path
                        "output": text,
                        "original_saved": str(original),
                    }
        finally:
            if run is not None:
                cleanup_run(run)
    return plan, facts, final


def _accepted_set_proposal(accepted: Any, project: str) -> dict:
    """The accepted set as a proposal document — through the SAME parse
    boundary the text answer faces (review round 1, F1).

    The record file is session-writable, so ``proposed_tasks`` is
    untrusted structure: a forged record carries the same hostility as a
    forged text answer and must hit the same wall. The set is rendered
    to the same JSON form and pushed through :func:`parse_proposal`
    (JSON shape, ``tasks`` a list, nesting ≤ MAX_PROPOSAL_DEPTH — all
    before any host-side deepcopy, ``canonical_json`` or scrub could
    recurse over it), then the sandbox's own caps are re-applied
    host-side (task count, per-task size): advisory in the sandbox,
    enforced where the record is read.

    Raises:
        ImportRefused: The set is not a bounded, proposal-shaped list of
            tasks — the refusal goes through keep and repair exactly like
            a refused text answer, never a traceback.
    """
    try:
        rendered = json.dumps(
            {"project": project, "tasks": accepted},
            ensure_ascii=False, default=str,
        )
    except (RecursionError, ValueError, TypeError) as exc:
        raise ImportRefused(
            "the session's proposed_tasks cannot be serialised into a "
            f"proposal ({type(exc).__name__}) — no proposal can be read "
            "from it"
        ) from exc
    proposal = parse_proposal(rendered)  # the SAME boundary, literally
    tasks = proposal["tasks"]
    if len(tasks) > PROPOSE_MAX_TASKS:
        raise ImportRefused(
            f"the accepted set holds {len(tasks)} tasks; the cap is "
            f"{PROPOSE_MAX_TASKS}"
        )
    for index, task in enumerate(tasks):
        try:
            size = len(json.dumps(task, ensure_ascii=False, default=str))
        except (RecursionError, ValueError, TypeError) as exc:
            raise ImportRefused(
                f"accepted task #{index} cannot be measured for size "
                f"({type(exc).__name__})"
            ) from exc
        if size > PROPOSE_TASK_MAX_CHARS:
            raise ImportRefused(
                f"accepted task #{index} serialises to {size} characters; "
                f"one task is at most {PROPOSE_TASK_MAX_CHARS}"
            )
    return proposal


def _carry_proposal_toplevel(proposal: dict, text: str) -> dict:
    """Carry the F-67 top-level proposal fields off a parsing text
    answer onto the accepted-set proposal (the propose_task path's only
    channel for them: the collector is per-task, so ``possibly_shipped``
    and ``dropped_criteria`` travel in the final text answer).

    Only the two whitelisted fields ride; ``tasks``/``project`` never
    (the accepted set owns those). A text answer that is not a JSON
    object — the expected one-line confirmation — carries nothing. The
    values are the session's: the plan-side schema and the parent's
    validation judge them exactly as a text-answer proposal's would be.
    """
    if not text.strip():
        return proposal
    stripped = text.strip()
    match = _FENCE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    if len(stripped) > PROPOSE_TASK_MAX_CHARS:
        return proposal
    try:
        answer = json.loads(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return proposal
    if not isinstance(answer, dict):
        return proposal
    for name in ("possibly_shipped", "dropped_criteria"):
        value = answer.get(name)
        if isinstance(value, list):
            proposal[name] = value
    return proposal


def _session_proposal(
    accepted: Any, text: str, project: str, *, calls: int = 0,
) -> "tuple[dict, str]":
    """The ONE precedence decision (F-57 AC-1): zero propose_task calls
    → the session's text answer is the proposal, exactly today; calls
    made → the accepted set is the proposal and wins over any text
    answer, with the divergence stated for the owner to see (the note
    rides the summary on success and the refusal message on failure).

    ``calls`` (review round 1, S1) is how many propose_task calls the
    session made, accepted or refused. Calls made with NOTHING accepted
    is its own visible fact: the text answer still is the proposal, but
    the note (or, when the text does not parse either, the refusal
    itself) says that every proposed task was refused — the owner is not
    left to wonder why the tool's set is empty.
    """
    if not accepted:
        if not calls:
            return parse_proposal(text), ""
        try:
            proposal = parse_proposal(text)
        except ImportRefused as exc:
            raise ImportRefused(
                f"{exc}; the session made {calls} propose_task call(s), "
                "all refused — every task it proposed broke a rule, and "
                "its text answer does not parse either"
            ) from exc
        return proposal, (
            f"the session made {calls} propose_task call(s), none "
            "accepted; the text answer is the proposal"
        )
    proposal = _carry_proposal_toplevel(
        _accepted_set_proposal(accepted, project), text)
    tasks = proposal["tasks"]
    if not text.strip():
        return proposal, (
            f"the session made {calls} propose_task call(s) and answered "
            f"no text; the accepted set ({len(tasks)} tasks) is the "
            "proposal"
        )
    try:
        answer = parse_proposal(text)
    except ImportRefused:
        return proposal, (
            f"the session made propose_task calls and its text answer was "
            f"not valid JSON; the accepted set ({len(tasks)} tasks) is "
            f"the proposal"
        )
    if canonical_json(answer.get("tasks", [])) != canonical_json(tasks):
        return proposal, (
            f"the session made propose_task calls and its text answer "
            f"differs from the accepted set ({len(tasks)} tasks); the "
            "accepted set is the proposal"
        )
    return proposal, ""


# ------------------------------------------------------------------- entry --


@dataclass
class ImportResult:
    """What a run proposed: the plan, the rendered summary, the resolved
    queue path, whether it was written, and the session's raw output.

    ``raw_output`` travels to the caller (the CLI holds the interactive
    apply outside run_import) so a refusal at *its* apply can still keep
    the output through :func:`keep_refused_import_output`. It is the
    proposal that was judged: the session's text answer, or — when the
    propose tool's accepted set or a repair's answer won (F-57) — that
    winning document, rendered as JSON.

    ``repair`` (F-57 AC-2) carries the repair facts when the ONE repair
    session ran (``{"ran": True, "outcome": …, "turns": N}``); empty
    otherwise. ``precedence_note`` (AC-1) states the accepted-set/text
    divergence when the tool's set won over a differing text answer.
    """

    plan: ImportPlan
    summary: str
    queue_path: Path
    applied: bool
    raw_output: str = ""
    repair: dict = field(default_factory=dict)
    precedence_note: str = ""
    # F-57 review round 1 (S2): what produced the plan, for the eval's
    # report and any caller that measures the tool.
    propose_calls: int = 0
    accepted_count: int = 0


def run_import(
    repo: Path | str,
    queue_path: Path | str | None = None,
    *,
    docs_dir: str = DEFAULT_DOCS_DIR,
    session: Callable[[str], str] | None = None,
    confirm: Callable[[str], bool] | None = None,
    confirm_drop: "Callable[[DropEntry], bool] | None" = None,
    factory_root: Path | str | None = None,
    keep_refused: bool = False,
    max_turns: int | None = None,
    timeout_min: float | None = None,
) -> ImportResult:
    """Propose a queue from the project's planning documents.

    Reads the out-of-scope register, runs the importer session (or the
    injected ``session``), parses and merges the proposal, renders the
    summary and — only when ``confirm`` is given and returns True —
    writes the queue. With ``confirm=None`` this is a pure proposal:
    nothing is written, whatever the summary says.

    The real session gets the ``propose_task`` tool (F-57 AC-1): each
    task is validated as the model writes it, the accepted set is the
    proposal once any call was made (the text answer then only a
    confirmation; a divergence between the two is stated in the summary
    and the refusal), and zero calls keep today's text-answer path
    exactly. A refused proposal from the real session then gets ONE
    bounded repair session (F-57 AC-2) — the saved output, the full
    violation list and the contract, no doc re-reading — whose answer
    goes through the same parse/scrub/plan validation; a still-refused
    repair refuses the import naming both saved files. An injected
    ``session`` never triggers a repair (a stub re-runs free).

    Args:
        repo: The project repo root (the planning docs and the register
            live there).
        queue_path: The queue file; default ``<repo>/.factory/tasks.json``.
        docs_dir: Repo-relative planning-docs directory for the prompt.
        session: Injected importer session (``prompt -> text``); default
            :func:`sdk_import_session` (sandboxed, in a disposable clone).
        confirm: ``summary -> bool``; asked before the one and only write.
        confirm_drop: Called once per drop that needs acceptance (R1-1:
            every drop unless backed by a done-marked line in the
            reference task's own source doc)
            (F-67 AC-4) at apply time; any False refuses the write.
            Passed straight to :func:`apply_import`.
        factory_root: Factory root for the default session's run dir,
            sandbox settings, and refused-output copies (default
            ``~/factory``).
        keep_refused: Keep the refused output for an *injected* session
            too — for a caller replaying a real session's output (an
            eval/replay harness), where re-running is not free. The real
            session (``session=None``) always keeps; a plain stub never
            does.
        max_turns/timeout_min: The real session's budget. None (the
            default) scales with the docs the prompt lists
            (:func:`scaled_import_budget`); an explicit value always wins
            and is bounds-checked before any session starts
            (:func:`validate_budget`).

    Returns:
        The :class:`ImportResult` — plan, summary, resolved queue path,
        whether the queue was written, the session's raw output, and the
        repair/precedence facts.

    Raises:
        ImportRefused: An explicit budget is outside its bounds (before
            any session starts); no session output could be parsed or
            merged into a schema-valid, all-draft proposal (AC-1/AC-2)
            — including, for the real session, a repair that did not fix
            it, naming both saved files; or the apply was refused (the
            queue changed while the confirm waited; a repair is never
            spent on this — the output was valid). When the output was
            expensive (the real session, or ``keep_refused``), the raw
            output is saved (redacted, 0600) under
            ``<factory-root>/import/`` and the message names the file
            and the full re-review command. A write ``OSError`` after
            the session keeps its type — the copy is still saved,
            noted on the exception.
    """
    validate_budget(max_turns, timeout_min)
    repo = Path(repo)
    # F-43 review round 1 (finding 1): the egress secrets for everything
    # this run writes from session text — the proposal that becomes
    # tasks.json (committed and PUSHED by the owner; on a public repo
    # that is the open web) and the refused-output save. The session may
    # hold the token in its env; nothing it echoes may come back out.
    if queue_path is None:
        queue_path = repo / ".factory" / "tasks.json"
    queue_path = Path(queue_path)
    records = read_out_of_scope(repo)
    existing_doc = load_tasks(queue_path) if queue_path.is_file() else None
    profile: dict | None = None
    if existing_doc is not None:
        project = existing_doc["project"]
        # The profile is the provider truth even when the queue exists
        # (F-59): load it for the resolver; a missing profile keeps the
        # pre-F-59 zai default via run_secrets({}).
        profile = (load_profile(repo / ".factory" / "project.yaml")
                   if (repo / ".factory" / "project.yaml").is_file() else {})
    else:
        profile = load_profile(repo / ".factory" / "project.yaml")
        project = profile["name"]
    # F-59: every provider token the profile's seats resolve to (the
    # project's kind plus seat overrides) — a Bedrock project's importer
    # scrubs its own token, not only the Z.ai one.
    egress_secrets = run_secrets(profile)
    # The ONE listing for the whole run (F-49 review round 1, finding 3):
    # the prompt, the scaled budget and the summary's truncated-doc names
    # all describe the same docs — computed before the session runs, so
    # a docs change DURING the session cannot make the summary name a
    # different capped set than the prompt offered.
    listing = _list_planning_docs(repo, docs_dir)
    prompt = build_import_prompt(repo, docs_dir, records, project=project,
                                 files=listing)
    # F-67 AC-4: the reference queue (the repo's own, as PLAIN JSON — it
    # may predate the current schema) and the kept docs' texts, computed
    # once and threaded into BOTH plan_import calls (the proposal's and
    # the repair's): an import into a fresh --queue is checked against
    # the queue the repo already carries, and the doc-line checks judge
    # the same docs the prompt listed.
    reference = read_reference_queue(repo)
    doc_texts = read_kept_doc_texts(repo, listing.kept)
    keep = session is None or keep_refused
    # F-67 AC-2: the main session's once-per-run record channel. The
    # write happens on every exit path (accepted, refused, aborted), for
    # the real session only — an injected stub re-runs free and keeps
    # nothing, records included.
    main_session: dict[str, Any] = {}

    def _run_sink(run_id: str, record: "SessionRecord | None",
                  proposal_saved: str | None) -> None:
        main_session["run_id"] = run_id
        main_session["record"] = record
        if proposal_saved is not None:
            main_session.setdefault("proposal_saved", proposal_saved)

    record_written: list[bool] = []

    def _write_main_record(outcome: str, proposal_file: str | None = None) -> None:
        """Write the main import session's record, once per run."""
        if not keep or record_written or "run_id" not in main_session:
            return
        record_written.append(True)
        record = main_session.get("record")
        if record is None and sink_records:
            record = sink_records[-1]
        _write_session_record(
            factory_root=factory_root, project=project, profile=profile,
            run_id=main_session["run_id"], record=record, kind="import",
            outcome=outcome,
            proposal_file=proposal_file
            or main_session.get("proposal_saved"),
            secrets=egress_secrets,
        )

    def _keep(exc: BaseException, payload: str) -> BaseException:
        """Today's keep-refused wrapper: an ImportRefused comes back with
        the saved file's path and the full re-review command; anything
        else keeps its type with a note. ``exc`` itself when ``keep`` is
        off (a stub keeps nothing)."""
        if not keep or not isinstance(exc, (ImportRefused, OSError)):
            return exc
        return keep_refused_import_output(
            exc, text=payload, project=project, repo=repo,
            queue_path=queue_path, docs_dir=docs_dir,
            factory_root=factory_root, secrets=egress_secrets,
        )

    accepted: Any = None
    propose_calls = 0
    raw_calls: Any = 0
    effective_turns = max_turns or DEFAULT_MAX_TURNS
    effective_timeout = timeout_min or DEFAULT_TIMEOUT_MIN
    sink_records: list[SessionRecord] = []

    def _sink(record: SessionRecord) -> None:
        sink_records.append(record)

    if session is not None:
        text = session(prompt)
    else:
        if profile is None or "name" not in profile:
            # The sandbox settings (docs/network allowlists, protected
            # paths) come from the profile; no profile, no session. With
            # an existing queue the profile may have been loaded earlier
            # only for the resolver's secrets (F-59) — the strict load
            # here still refuses when it is missing.
            profile = load_profile(repo / ".factory" / "project.yaml")
        # F-62 AC-3: with an existing queue, the session runs under the
        # QUEUE's project (the namespace every path builder uses); the
        # profile must agree — a queue naming another project's
        # namespace under this repo's contract is refused before
        # anything is spent (and before the F-63 approval check, so the
        # refusal names the actual disagreement).
        if existing_doc is not None and project != profile["name"]:
            raise ImportRefused(
                f"the queue at {queue_path} names project "
                f"{project!r} but the profile at "
                f"{repo / '.factory' / 'project.yaml'} names "
                f"{profile['name']!r}: a queue and profile that "
                "disagree cannot name one project's namespace"
            )
        # F-63 AC-2: the importer session and its repair seat run under
        # the profile's security-relevant content — the owner must have
        # approved it before any session starts. The check hashes THIS
        # dict; the session below and the repair path both receive it.
        # (This module's FACTORY_ROOT_DEFAULT on purpose: tests patch it
        # to redirect every factory-root path this module takes.)
        require_profile_approval(
            profile,
            Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT,
            origin=str(repo / ".factory" / "project.yaml"),
        )
        if max_turns is None or timeout_min is None:
            scaled_turns, scaled_timeout = scaled_import_budget(
                len(listing.kept)
            )
        effective_turns = max_turns if max_turns is not None else scaled_turns
        effective_timeout = (
            timeout_min if timeout_min is not None else scaled_timeout
        )
        try:
            text = sdk_import_session(
                prompt, repo, profile=profile, queue_path=queue_path,
                factory_root=factory_root,
                max_turns=effective_turns, timeout_min=effective_timeout,
                docs_dir=docs_dir,
                # F-57 AC-1: the real session gets the per-task tool, built
                # from the same staged schema, the same register and the
                # queue's own project name; the accepted set rides the
                # record back out of the sandbox.
                propose={
                    "project": project,
                    "schema": build_proposal_schema(_load_tasks_schema()),
                    "record_paths": [record["path"] for record in records],
                },
                record_sink=_sink,
                run_sink=_run_sink,
            )
        except ImportRefused:
            # The session aborted (wall clock, turn cap, an unusable
            # record): it still ran, so it is still on record (F-67
            # AC-2) — write what the run sink captured, then re-raise.
            _write_main_record("aborted")
            raise
        if sink_records:
            # F1 (review round 1): the record file is session-writable,
            # so its fields are UNTRUSTED — taken raw here; the parse
            # boundary in _accepted_set_proposal makes the list safe and
            # the scalar checks inside the try (below) make the counts
            # safe. Nothing structural happens before those.
            accepted = sink_records[-1].proposed_tasks
            raw_calls = sink_records[-1].propose_calls

    raw_output = text
    repair_final: ImportRefused | None = None
    precedence_note = ""
    repair: dict = {}
    accepted_count = 0
    try:
        # The ONE precedence decision (F-57 AC-1) — zero propose_task
        # calls: the text answer, today's path exactly. Calls made: the
        # accepted set is the proposal and wins over any text answer,
        # the divergence stated. Then the proposal passes the egress
        # seam BEFORE plan_import and validation (review round 1,
        # finding 1): every session-derived string — titles, sources,
        # AC text, owner steps, keys included — loses the seat token,
        # the credential shapes and control sequences first, so (a)
        # nothing reaches tasks.json (which the owner commits and
        # pushes) or the printed plan unscrubbed, and (b) a redaction
        # cannot produce a schema-valid-looking but different task
        # without the plan diff showing it — validation judges the
        # scrubbed values, not the raw ones.
        refused: ImportRefused | None = None
        try:
            # R2-2 (review round 2): the record's propose_calls is
            # rendered into messages — only a non-bool int >= 0 is a
            # count; anything else (a string carrying the seat token, a
            # dict, a bool, a negative) is a forged record, refused
            # THROUGH keep and repair like every other broken session
            # product. Never interpolated raw.
            if (
                isinstance(raw_calls, bool)
                or not isinstance(raw_calls, int)
                or raw_calls < 0
            ):
                raise ImportRefused(
                    "the session's record carries a malformed "
                    "propose_calls field "
                    f"({type(raw_calls).__name__}) — the record is "
                    "unusable; no proposal can be trusted from it"
                )
            propose_calls = raw_calls
            proposal, precedence_note = _session_proposal(
                accepted, text, project, calls=propose_calls
            )
            accepted_count = len(proposal["tasks"]) if accepted else 0
            proposal = scrub_session_value(proposal, egress_secrets)
            if accepted:
                raw_output = (
                    json.dumps(proposal, indent=2, ensure_ascii=False) + "\n"
                )
            plan = plan_import(existing_doc, proposal, records,
                               project=project, reference=reference,
                               doc_texts=doc_texts, repo=repo,
                               profile=profile)
        except ImportRefused as exc:
            refused = exc
        if refused is not None:
            if session is not None:
                # A stub re-runs free: today's path exactly — the outer
                # keep wraps the refusal as before.
                raise refused
            # The real session: keep the output, then the ONE repair
            # (F-57 AC-2). Its answer faces the SAME validation; a
            # still-refused repair's final refusal names BOTH saved
            # files — it already kept them, so the outer keep must not
            # save a third copy (the identity check below).
            plan, repair, final = _repair_refused_proposal(
                refused, payload=raw_output, project=project, repo=repo,
                queue_path=queue_path, docs_dir=docs_dir,
                factory_root=factory_root, profile=profile or {},
                records=records, existing_doc=existing_doc,
                import_turns=effective_turns,
                import_timeout=effective_timeout,
                secrets=egress_secrets,
                reference=reference, doc_texts=doc_texts,
            )
            # The main session's proposal was refused: its record says
            # so (F-67 AC-2), naming the kept original the repair works
            # from — written whatever the repair did with it.
            _write_main_record(
                "refused",
                proposal_file=(
                    Path(str(repair.get("original_saved"))).name
                    if repair.get("original_saved") else None
                ),
            )
            if plan is None:
                if final is None:
                    # The repair could not even start. When the helper
                    # had already kept the original, wrap the refusal
                    # exactly as the keep path would — naming THAT file,
                    # never saving a second copy (the identity check in
                    # the outer except recognises this exception).
                    saved_path = repair.get("original_saved")
                    if saved_path:
                        command = _import_fix_command(
                            Path(saved_path), repo, queue_path, docs_dir
                        )
                        repair_final = ImportRefused(
                            f"{refused}; saved to {saved_path}; fix it and "
                            f"review with {command}"
                        )
                        raise repair_final from refused
                    # Nothing was kept (the save itself failed): today's
                    # exact refusal — the outer keep wraps it.
                    raise refused
                repair_final = (
                    final if not precedence_note
                    else ImportRefused(f"{final}; ({precedence_note})")
                )
                raise repair_final from refused
            raw_output = repair.get("output", raw_output)
        # F-49 AC-3: the plan carries the docs the ONE listing (computed
        # before the session) dropped, so the summary the owner confirms
        # names them — the same capped set the prompt described.
        plan.truncated_docs = listing.dropped
        summary = render_summary(plan)
        if repair:
            turns_text = (
                f"{repair['turns']} turns"
                if isinstance(repair.get("turns"), int)
                else "an unknown number of turns"
            )
            summary += (
                f"\n(one repair session ran after the first proposal was "
                f"refused: outcome {repair['outcome']}, {turns_text} — "
                "the repaired proposal is what you are confirming)\n"
            )
        if precedence_note:
            summary += f"\n({precedence_note})\n"
        # The main session's record (F-67 AC-2): written once the plan
        # exists — before the confirm, so a declined apply still leaves
        # the session's cost on record. proposal_file stays None here:
        # an accepted proposal became the plan, not a kept file.
        _write_main_record("accepted")
        applied = False
        if confirm is not None:
            applied = apply_import(
                plan, queue_path, confirm=lambda: bool(confirm(summary)),
                confirm_drop=confirm_drop,
            )
    except (ImportRefused, OSError) as exc:
        if exc is repair_final:
            # The repair's own final refusal: both files already saved
            # and named by it — never a second keep.
            raise
        kept = _keep(exc, raw_output)
        if kept is not exc:
            if precedence_note and isinstance(kept, ImportRefused):
                kept = ImportRefused(f"{kept}; ({precedence_note})")
            raise kept from exc
        raise
    return ImportResult(
        plan=plan, summary=summary, queue_path=queue_path, applied=applied,
        raw_output=raw_output, repair=repair,
        precedence_note=precedence_note,
        propose_calls=propose_calls,
        accepted_count=accepted_count,
    )


def sdk_import_session(
    prompt: str,
    repo: Path | str,
    *,
    profile: dict,
    queue_path: Path | str | None = None,
    factory_root: Path | str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    timeout_min: float = DEFAULT_TIMEOUT_MIN,
    docs_dir: str = DEFAULT_DOCS_DIR,
    propose: dict | None = None,
    record_sink: "Callable[[SessionRecord], None] | None" = None,
    run_sink: 'Callable[[str, "SessionRecord | None", "str | None"], None] | None' = None,
) -> str:
    """The default importer session: the reviewer seat, sandboxed.

    Runs through :func:`factory.session.run_session_sandboxed` exactly as
    the orchestrator runs its reviewer — the whole runner inside srt with
    the read-only reviewer guard (read-only sandbox settings, no
    Write/Edit/NotebookEdit, the workspace-integrity check that fails a
    session which changed anything) — in a disposable clone of the repo's
    committed HEAD under ``<factory_root>/runs/<project>/``. The live
    checkout is never the session's cwd and nothing runs in-process
    (DESIGN D3): the session reads committed planning docs, so commit
    them before importing. The clone and its sandbox files are removed on
    every exit path.

    While the clone exists the queue's run lease is held (when the queue
    exists): every run dir of a project is created under that lease, so
    the orchestrator's reclaim never deletes the clone mid-session, and
    an import refuses instead of racing a live run.

    The prompt's self-check schema is staged into the session's writable
    scratch before the session starts (``scratch_files`` —
    :func:`build_proposal_schema` rendered as
    ``$TMPDIR/proposal.schema.json``): the session's clone is the target
    repo, which has no schema/ of its own, and the narrowed schema is
    what its own output is judged against.

    ``propose`` (F-57 AC-1) wires the per-task ``propose_task`` tool into
    the session (``{"project", "schema", "record_paths"}`` — what the
    sandboxed entry builds its collector from); None runs the session
    without it. ``record_sink``, when given, is called with the
    session's :class:`~factory.session.SessionRecord` on the completed
    path — the channel the accepted set rides out on
    (``record.proposed_tasks``), the same role the return value plays for
    the text answer.

    A session that does not complete — ``aborted`` (wall clock, turn
    cap), ``failed``, or the outer wall clock (``TimeoutExpired``) — has
    its scratch's ``$TMPDIR/proposal.json`` (the file the self-check
    prompt tells it to write, and — F-57 — the one the propose tool
    keeps written as the accepted set grows) copied out through the one
    refused-output writer BEFORE ``cleanup_run`` sweeps the scratch, and
    the refusal names the saved file and the re-review command, or says
    no proposal was written (:func:`_abort_refusal`). The third full
    jobfinderos session was lost to exactly this gap.

    Args:
        prompt: The importer prompt (:func:`build_import_prompt`).
        repo: The project repo root (cloned, never used as the cwd).
        profile: The project profile — the sandbox's docs/network
            allowlists and ``paths.protected``.
        queue_path: The queue whose run lease to hold; None or a missing
            file holds none (a fresh project has no orchestrator).
        factory_root: Factory root (default ``~/factory``).
        max_turns/timeout_min: The session's budget. :func:`run_import`
            passes the scaled default or the caller's explicit values.
        docs_dir: The planning-docs directory, for the re-review command
            in an abort refusal (rides along only when non-default).
        propose: The propose tool's configuration (F-57 AC-1); None for
            no tool.
        record_sink: Called with the completed session's record, so the
            caller can read ``proposed_tasks`` (F-57 AC-1) without
            changing this function's text return.

    Raises:
        ImportRefused: No model credential (review a prepared proposal
            with ``factory import --proposal`` instead), the clone or the
            sandbox could not be set up, the session timed out, it ended
            other than ``completed`` (a reviewer that mutated its clone
            ends ``failed``), or — F-56 — its RECORD was unusable though
            the session ran (missing, unreadable, invalid; the error
            names srt's exit path). The last three salvage the scratch's
            last proposal, when one exists, before cleanup.
    """
    # F-59: the importer seat's provider — the ONE resolver (the
    # profile's own block; the importer ROLE's model), the per-kind
    # credential, the named refusal. Never a fallback: a client project
    # whose provider has no credential refuses here.
    try:
        spec = resolve_provider(profile, "importer")
    except ProviderError as exc:
        raise ImportRefused(f"the importer session cannot run: {exc}") \
            from exc
    token = provider_token(spec.kind, base_url=spec.base_url)
    if not token:
        raise ImportRefused(
            f"no {spec.kind} credential available (keychain item "
            f"{keychain_service(spec.kind)}), so the importer session "
            "cannot run; prepare a proposal elsewhere and review it with "
            "`factory import --proposal <file>` — the importer never "
            "falls back to another provider (F-59)"
        )
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    repo = Path(repo)
    if queue_path is None:
        queue_path = repo / ".factory" / "tasks.json"
    queue = (
        TaskQueue(queue_path)
        if queue_path is not None and Path(queue_path).is_file() else None
    )
    # F-67 AC-2: the once-per-run channel the session record write rides
    # on — fired on EVERY exit where a session attempt existed (the
    # run_session_sandboxed call was made: completed, aborted, failed,
    # record lost, timeout), with the record in hand when there is one
    # and the salvage's proposal file name when there is not. The
    # could-not-start refusals (before the session call) fire nothing:
    # nothing was spent, no session existed.
    run_fired: list[bool] = []

    def _fire(record: "SessionRecord | None",
              proposal_saved: str | None = None) -> None:
        if run_sink is not None and not run_fired:
            run_fired.append(True)
            run_sink(run.run_id, record, proposal_saved)

    def _abort_exc(head: str) -> "tuple[ImportRefused, str | None]":
        message, salvaged = _abort_refusal(
            head, run=run, project=profile["name"], factory_root=root,
            repo=repo, queue_path=Path(queue_path), docs_dir=docs_dir,
            secrets=(token,),
        )
        return ImportRefused(message), salvaged

    def _saved_record_or_none() -> "SessionRecord | None":
        """The record file's contents, best effort (the file may hold a
        record even when the runner died before returning one)."""
        from factory.session import _read_record_quietly, record_path_for

        return _read_record_quietly(record_path_for(run.settings_path))
    # F2 (review round 1): a busy queue here is an ImportRefused (the
    # CLI's caught class), naming the holder — the same conversion the
    # repair applies to its own lease. Pre-existing behaviour for the
    # import session, same file, same round.
    lease_stack = contextlib.ExitStack()
    try:
        lease_stack.enter_context(
            queue.idle_lease() if queue is not None
            else contextlib.nullcontext()
        )
    except RunInProgressError as exc:
        raise ImportRefused(
            f"the import refuses to race a live run ({exc}); retry when "
            "the run ends"
        ) from exc
    with lease_stack:
        try:
            run = create_run(profile["name"], Path(repo), factory_root=root)
        except RunError as exc:
            raise ImportRefused(f"cannot clone the repo for the importer session: {exc}") from exc
        try:
            record = run_session_sandboxed(
                "reviewer", prompt, run.root, profile,
                factory_root=root, settings_path=run.settings_path,
                max_turns=max_turns, timeout_min=timeout_min,
                # F-59: the reviewer SEAT config with the importer ROLE's
                # provider + model (the resolver's seat is the role).
                # R1-2: no raw token — the constructor resolves the
                # credential for the spec (the refusal above already
                # named a missing one).
                role="importer",
                protected=list((profile.get("paths") or {}).get("protected") or []),
                run_id=run.run_id,
                scratch_files={
                    PROPOSAL_SCHEMA_NAME: json.dumps(
                        build_proposal_schema(_load_tasks_schema()), indent=2
                    )
                },
                propose=propose,
            )
        except SessionRecordError as exc:
            # F-56 AC-5: the session RAN — its record is what broke
            # (missing, unreadable, invalid; srt's exit path named in the
            # error). Whatever the session wrote to its scratch rides out
            # exactly as an abort's does: the salvage (inside the raise's
            # argument) runs BEFORE the finally's cleanup_run sweeps the
            # scratch. The error text is already seam-scrubbed by the
            # record-failure helper; it is scrubbed again here because it
            # is session text in a message the terminal prints.
            refusal, salvaged = _abort_exc(scrub_session_text(
                f"the importer session's record was unusable: {exc}",
                (token,),
            ))
            _fire(_saved_record_or_none(), salvaged)
            raise refusal from exc
        except (SessionConfigError, SandboxError, RunError) as exc:
            # Runner setup errors: no bundled CLI / token (SessionConfigError),
            # srt missing or not at the pin (SandboxError, from ensure_srt
            # before anything spawns), a run-dir fault (RunError). Each is
            # a refusal (exit 1 at the CLI), never a traceback. The session
            # never started, so there is no scratch proposal to salvage and
            # no session record (F-67: nothing was spent).
            raise ImportRefused(f"the importer session could not run: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            # The outer wall clock (the runner hung past its teardown
            # grace): salvage before cleanup, exactly like an aborted
            # record — the scratch is still on disk here.
            refusal, salvaged = _abort_exc(
                f"the importer session timed out after {exc.timeout:.0f}s"
            )
            _fire(_saved_record_or_none(), salvaged)
            raise refusal from exc
        else:
            # R3-1 (review round 3): the runner-returned record is
            # validated at the file boundary (_read_record) — this is the
            # defence-in-depth half for records that arrive as objects
            # (a harness seam), and it fails exactly like the F-56 record
            # error: salvaged, scrubbed, named.
            from factory.session import record_field_error

            field_error = record_field_error(record)
            if field_error is not None:
                refusal, salvaged = _abort_exc(scrub_session_text(
                    "the importer session's record is unusable "
                    f"(record field {field_error})", (token,),
                ))
                _fire(record, salvaged)
                raise refusal
            if record.status != "completed":
                # The salvage (inside the raise's argument) runs BEFORE
                # the finally's cleanup_run sweeps the scratch: any other
                # placement reads a deleted file. This is the seam the
                # third lost jobfinderos session exposed. record.reason is
                # session text in a message the terminal prints — seam it
                # with the token this session itself held.
                refusal, salvaged = _abort_exc(scrub_session_text(
                    f"the importer session ended {record.status}: "
                    f"{record.reason}", (token,)
                ))
                _fire(record, salvaged)
                raise refusal
            _fire(record)
        finally:
            cleanup_run(run)
    if record_sink is not None:
        record_sink(record)
    return record.result or ""
