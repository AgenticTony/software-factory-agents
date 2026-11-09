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
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

from claude_agent_sdk import create_sdk_mcp_server, tool

from factory.approvals import require_profile_approval
from factory.contract import (
    SCHEMA_DIR,
    ContractError,
    canonical_json,
    iter_schema_violations,
    load_profile,
    load_tasks,
    validate_queue,
)
from factory.egress import check_secrets, scrub_session_text, scrub_session_value
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
    seat_token,
)
from factory.state import (
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
    "acceptance",
    "budget",
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

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ImportRefused(ContractError):
    """The import was refused. The message is written for humans.


    Subclasses :class:`~factory.contract.ContractError` so every caller
    that already refuses on invalid contract state refuses on a broken
    import too."""


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
    }
    task = proposal["$defs"]["task"]
    keep = {"id", *SPEC_FIELDS, "out_of_scope_match"}
    task["properties"] = {
        name: prop for name, prop in task["properties"].items() if name in keep
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
        '- gates.merge is always "human" — the owner merges; the importer',
        "  never proposes an auto merge.",
        "- budget.size tops out at M: a task too big for size M is split",
        "  into slices or proposed as separate tasks, never sized above",
        "  the enum.",
        "- priority is P1–P3 only — the values listed above; P0 is not a",
        "  priority level this queue knows.",
        '- a check whose type is anything but "manual" must carry ref.',
        "- owner_steps you propose are objects with text and done: false —",
        "  done_at, done_by and evidence are written when the owner ticks",
        '  them, never by you. Slices you propose always have status "todo".',
        "- Never set status, acceptance_hash, approved_at, approved_by,",
        "  attempts, links or out_of_scope. Every task is imported as draft;",
        "  approval is the owner's act (factory approve).",
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


def _task_from_proposal(raw: Any, index: int, record_paths: set[str]) -> tuple[dict, str | None]:
    """Build one proposed task from a raw proposal entry.

    Returns the task (``id``, ``status: draft`` and exactly the spec
    fields the entry carries — no defaults: a default here would read as
    a proposal to an existing task's merge; see :func:`_new_task`) and
    the out-of-scope record it claims to resemble, if any.

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
    return task, match


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
# the task's approval (F-57 review round 1, F4).
CONTRACT_FIELDS = ("acceptance", "risk", "gates")


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
) -> ImportPlan:
    """Build the import plan: merged document, per-task changes,
    out-of-scope matches and the full diff. Validates the merged
    document against the schema — an invalid proposal is refused before
    anything is shown or written (AC-1).

    Args:
        existing_doc: The queue as it is on disk, or None for a fresh
            project (the proposal then creates the queue).
        proposal: The parsed proposal (see :func:`parse_proposal`).
        records: The out-of-scope register (see :func:`read_out_of_scope`).
        project: The queue's project name — from the existing queue, or
            the profile for a fresh one.

    Raises:
        ImportRefused: The proposal names another project, omits or
            misspells the ``project`` key (F-49 AC-6 — the proposal
            schema requires it), duplicates an id, carries forbidden
            fields or statuses, names an unknown out-of-scope record —
            or the merged document would not validate (AC-1).
    """
    proposed_project = proposal.get("project")
    if proposed_project is not None and proposed_project != project:
        raise ImportRefused(
            f"the proposal names project {proposed_project!r}; this queue "
            f"is {project!r}"
        )
    record_paths = {record["path"] for record in records}
    proposed_tasks: list[dict] = []
    matches: list[OutOfScopeMatch] = []
    seen: set[str] = set()
    for index, raw in enumerate(proposal.get("tasks", [])):
        task, match = _task_from_proposal(raw, index, record_paths)
        if task["id"] in seen:
            raise ImportRefused(
                f"task {task['id']} appears twice in the proposal; task ids "
                "must be unique or dependency and attempt records become "
                "ambiguous"
            )
        seen.add(task["id"])
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
    )


def _doc_text(doc: dict) -> str:
    """The on-disk rendering of a queue document (the diff's currency)."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ----------------------------------------------------------------- summary --


def render_summary(plan: ImportPlan) -> str:
    """The import summary: per-task changes, the out-of-scope matches
    (each with its record's path, for you to confirm — AC-4), and the
    full diff."""
    def ids(kind: str) -> list[str]:
        return [c.task_id for c in plan.changes if c.kind == kind]

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


# -------------------------------------------------------------------- write --


def apply_import(
    plan: ImportPlan,
    queue_path: Path | str,
    *,
    confirm: Callable[[], bool],
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

    Args:
        plan: The plan to apply.
        queue_path: The queue file.
        confirm: Called before anything is written; False writes nothing.

    Returns:
        True if the queue was written, False if the confirm declined.

    Raises:
        ImportRefused: The queue changed since the plan was built.
        OSError: The write itself failed (the previous contents, if any,
            are intact).
    """
    if not confirm():
        return False
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
        return ImportRefused(
            f"{exc}; saved to {saved}; fix it and review with {command}"
        )
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
) -> str:
    """The refusal message for a session that did not complete.

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
        return f"{head}; no proposal was written before the abort"
    except (OSError, ValueError) as exc:
        return f"{head}; (its last proposal could not be read: {exc})"
    try:
        saved = _save_refused_output(text, project, factory_root, secrets=secrets)
    except OSError as exc:
        return f"{head}; (its last proposal could not be saved: {exc})"
    command = _import_fix_command(saved, repo, queue_path, docs_dir)
    return f"{head}; its last proposal was saved to {saved}; " \
           f"review it with {command}"


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
        "- gates.merge is always \"human\" — the owner merges; the importer",
        "  never proposes an auto merge.",
        "- budget.size tops out at M: a task too big for size M is split",
        "  into slices or proposed as separate tasks, never sized above",
        "  the enum.",
        "- priority is P1–P3 only — the values listed above; P0 is not a",
        "  priority level this queue knows.",
        '- a check whose type is anything but "manual" must carry ref.',
        "- Never set status, acceptance_hash, approved_at, approved_by,",
        "  attempts, links or out_of_scope. Every task is imported as",
        "  draft; approval is the owner's act (factory approve).",
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
    facts: dict = {}
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
                protected=list(
                    (profile.get("paths") or {}).get("protected") or []
                ),
                token=next((s for s in secrets if s), None),
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
                    # the same parse, the same scrub, the same plan.
                    proposal = scrub_session_value(
                        parse_proposal(text), secrets
                    )
                    plan = plan_import(
                        existing_doc, proposal, records, project=project
                    )
                except ImportRefused as second:
                    writer.append({
                        "type": "import_repair", "outcome": "still_refused",
                        "turns": turns,
                    })
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
                    facts = {
                        "ran": True, "outcome": "repaired",
                        "turns": turns,
                        # the judged document, for the caller's keep path
                        "output": text,
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
    proposal = _accepted_set_proposal(accepted, project)
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
    egress_secrets = (seat_token() or "",)
    if queue_path is None:
        queue_path = repo / ".factory" / "tasks.json"
    queue_path = Path(queue_path)
    records = read_out_of_scope(repo)
    existing_doc = load_tasks(queue_path) if queue_path.is_file() else None
    profile: dict | None = None
    if existing_doc is not None:
        project = existing_doc["project"]
    else:
        profile = load_profile(repo / ".factory" / "project.yaml")
        project = profile["name"]
    # The ONE listing for the whole run (F-49 review round 1, finding 3):
    # the prompt, the scaled budget and the summary's truncated-doc names
    # all describe the same docs — computed before the session runs, so
    # a docs change DURING the session cannot make the summary name a
    # different capped set than the prompt offered.
    listing = _list_planning_docs(repo, docs_dir)
    prompt = build_import_prompt(repo, docs_dir, records, project=project,
                                 files=listing)
    keep = session is None or keep_refused

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
    if session is not None:
        text = session(prompt)
    else:
        if profile is None:
            # The sandbox settings (docs/network allowlists, protected
            # paths) come from the profile; no profile, no session.
            profile = load_profile(repo / ".factory" / "project.yaml")
            # F-62 AC-3: with an existing queue, the session runs under
            # the QUEUE's project (the namespace every path builder
            # uses); the profile must agree — a queue naming another
            # project's namespace under this repo's contract is
            # refused before anything is spent.
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
        sink_records: list[SessionRecord] = []

        def _sink(record: SessionRecord) -> None:
            sink_records.append(record)

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
        )
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
                               project=project)
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
        applied = False
        if confirm is not None:
            applied = apply_import(
                plan, queue_path, confirm=lambda: bool(confirm(summary))
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
    token = seat_token()
    if not token:
        raise ImportRefused(
            "no Z.ai token available (keychain item factory-zai), so the "
            "importer session cannot run; prepare a proposal elsewhere and "
            "review it with `factory import --proposal <file>`"
        )
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    repo = Path(repo)
    if queue_path is None:
        queue_path = repo / ".factory" / "tasks.json"
    queue = (
        TaskQueue(queue_path)
        if queue_path is not None and Path(queue_path).is_file() else None
    )
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
                protected=list((profile.get("paths") or {}).get("protected") or []),
                token=token, run_id=run.run_id,
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
            raise ImportRefused(_abort_refusal(
                scrub_session_text(
                    f"the importer session's record was unusable: {exc}",
                    (token,),
                ),
                run=run, project=profile["name"], factory_root=root,
                repo=repo, queue_path=Path(queue_path), docs_dir=docs_dir,
                secrets=(token,),
            )) from exc
        except (SessionConfigError, SandboxError, RunError) as exc:
            # Runner setup errors: no bundled CLI / token (SessionConfigError),
            # srt missing or not at the pin (SandboxError, from ensure_srt
            # before anything spawns), a run-dir fault (RunError). Each is
            # a refusal (exit 1 at the CLI), never a traceback. The session
            # never started, so there is no scratch proposal to salvage.
            raise ImportRefused(f"the importer session could not run: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            # The outer wall clock (the runner hung past its teardown
            # grace): salvage before cleanup, exactly like an aborted
            # record — the scratch is still on disk here.
            raise ImportRefused(_abort_refusal(
                f"the importer session timed out after {exc.timeout:.0f}s",
                run=run, project=profile["name"], factory_root=root,
                repo=repo, queue_path=Path(queue_path), docs_dir=docs_dir,
                secrets=(token,),
            )) from exc
        else:
            # R3-1 (review round 3): the runner-returned record is
            # validated at the file boundary (_read_record) — this is the
            # defence-in-depth half for records that arrive as objects
            # (a harness seam), and it fails exactly like the F-56 record
            # error: salvaged, scrubbed, named.
            from factory.session import record_field_error

            field_error = record_field_error(record)
            if field_error is not None:
                raise ImportRefused(_abort_refusal(
                    scrub_session_text(
                        "the importer session's record is unusable "
                        f"(record field {field_error})", (token,),
                    ),
                    run=run, project=profile["name"], factory_root=root,
                    repo=repo, queue_path=Path(queue_path), docs_dir=docs_dir,
                    secrets=(token,),
                ))
            if record.status != "completed":
                # The salvage (inside the raise's argument) runs BEFORE
                # the finally's cleanup_run sweeps the scratch: any other
                # placement reads a deleted file. This is the seam the
                # third lost jobfinderos session exposed. record.reason is
                # session text in a message the terminal prints — seam it
                # with the token this session itself held.
                raise ImportRefused(_abort_refusal(
                    scrub_session_text(
                        f"the importer session ended {record.status}: "
                        f"{record.reason}", (token,)
                    ),
                    run=run, project=profile["name"], factory_root=root,
                    repo=repo, queue_path=Path(queue_path), docs_dir=docs_dir,
                    secrets=(token,),
                ))
        finally:
            cleanup_run(run)
    if record_sink is not None:
        record_sink(record)
    return record.result or ""
