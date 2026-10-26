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
* **Re-running with unchanged documents proposes no diff for existing
  tasks** (AC-3): the merge compares the proposed spec fields with the
  stored ones and touches nothing else — status, attempts, approval
  fields and links are preserved as they are. A task the proposal does
  not mention is carried over and *listed* (kind ``missing``), so a
  silently dropped work order is visible instead of quiet.

The out-of-scope register (TASK-CONTRACT §5, ``.factory/out-of-scope/``)
is read and shown to the importer session. A proposed task that resembles
a record carries the record's path in its proposal (``out_of_scope_match``)
and is listed in the summary with that path (AC-4). Matching is by
concept — the session's job, never a keyword matcher here — and a match
is only ever *listed* for you to confirm: the task is still proposed,
still ``draft``, never dropped or rejected on its own.

The session sits behind one injected callable (the F-06 pattern):
``session(prompt) -> text``. Tests pass a stub returning a fixed
proposal; the default, :func:`sdk_import_session`, runs the reviewer
seat — read-only: no Write/Edit — through
:func:`factory.session.run_session` and returns its final message.
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from factory.contract import (
    ContractError,
    canonical_json,
    load_profile,
    load_tasks,
    validate_queue,
)
from factory.session import run_session, seat_token
from factory.state import TaskQueue, _atomic_write_json, now_iso

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
# Long planning-doc trees are listed, not inlined; the session reads the
# files it needs from the repo (it runs with read tools).
MAX_DOC_FILES = 200

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ImportRefused(ContractError):
    """The import was refused. The message is written for humans.


    Subclasses :class:`~factory.contract.ContractError` so every caller
    that already refuses on invalid contract state refuses on a broken
    import too."""


# --------------------------------------------------------------- proposal --


def parse_proposal(text: str) -> dict:
    """Parse the importer session's output into a proposal document.

    A fenced code block is stripped first (sessions habitually wrap
    their JSON in one); what remains must be a JSON object with a
    ``tasks`` list.

    Raises:
        ImportRefused: The output is not JSON, not an object, or has no
            ``tasks`` list — the proposal never reaches the queue.
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
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            raise ImportRefused(
                f"{OUT_OF_SCOPE_DIR}/{path.name}: cannot read the "
                f"out-of-scope record: {exc}"
            ) from exc
        records.append({"path": f"{OUT_OF_SCOPE_DIR}/{path.name}", "text": text})
    return records


# ------------------------------------------------------------------ prompt --


def build_import_prompt(repo: Path, docs_dir: str, records: list[dict]) -> str:
    """The importer session's prompt: which docs to read, the register
    to match against, and the exact output contract."""
    repo = Path(repo)
    docs = repo / docs_dir
    files: list[str] = []
    if docs.is_dir():
        files = sorted(
            str(path.relative_to(repo))
            for path in docs.rglob("*")
            if path.is_file() and path.suffix in {".md", ".markdown"}
        )[:MAX_DOC_FILES]
    lines = [
        "You are the factory's importer seat. Read the project's planning",
        "documents and propose a tasks.json queue for them.",
        "",
        "Planning documents (read them from the repo):",
    ]
    if files:
        lines.extend(f"- {name}" for name in files)
    else:
        lines.append("- (none found under the docs directory)")
    lines += ["", "Out-of-scope register (TASK-CONTRACT §5) — concepts the",
              "owner already rejected. Match by concept, not keyword:"]
    if records:
        for record in records:
            lines += [f"### {record['path']}", record["text"].strip(), ""]
    else:
        lines.append("(the register is empty or missing)")
    lines += [
        "",
        "Output — and output nothing else — one JSON object:",
        '  {"project": "<project name>", "tasks": [...]}',
        "Each task carries: id, title, source (repo-relative path of the doc",
        "that owns the spec, with #heading when useful), kind (build | docs |",
        "decision | research | owner_only), priority (P1 | P2 | P3),",
        "depends_on, risk, gates {spec_approval, merge, deploy}, acceptance",
        "[{id, text, check {type, ref}}], budget {size, max_attempts,",
        "max_turns, wall_clock_min}, and owner_steps / slices when the doc",
        "defines them.",
        "Rules:",
        "- Never set status, acceptance_hash, approved_at, approved_by,",
        "  attempts, links or out_of_scope. Every task is imported as draft;",
        "  approval is the owner's act (factory approve).",
        '- When a proposed task resembles an out-of-scope record, add',
        '  "out_of_scope_match": "<the record\'s path>" to that task. Never',
        "  drop, reword or omit a task because it matches — the owner",
        "  confirms the match; you only surface it.",
        "- Propose every task the documents define, including tasks already",
        "  in the queue; unchanged tasks produce no diff.",
    ]
    return "\n".join(lines)


# -------------------------------------------------------------------- plan --


@dataclass(frozen=True)
class TaskChange:
    """What the import proposes for one task.

    ``kind`` is one of:

    * ``new`` — not in the queue; proposed as ``draft``.
    * ``unchanged`` — in the queue, proposed spec identical: no diff.
    * ``changed`` — proposed spec differs in ``fields``; only those
      fields are updated, state is preserved.
    * ``missing`` — in the queue but not proposed; carried untouched.
    """

    task_id: str
    kind: str
    fields: tuple[str, ...] = ()


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


def _task_from_proposal(raw: Any, index: int, record_paths: set[str]) -> tuple[dict, str | None]:
    """Build one proposed task from a raw proposal entry.

    Returns the task (with ``status: draft`` and the spec defaults
    applied) and the out-of-scope record it claims to resemble, if any.

    Raises:
        ImportRefused: The entry is not an object, carries a forbidden
            field or a non-draft status, has no readable id, or names a
            record that does not exist.
    """
    if not isinstance(raw, dict):
        raise ImportRefused(f"proposal task #{index} is not an object")
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ImportRefused(
            f"proposal task #{index} has no readable id: "
            f"{json.dumps(raw, default=str)[:200]}"
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
    return task, match


def _merge_task(existing: dict, proposed: dict) -> tuple[dict, list[str]]:
    """Merge a proposal into an existing task: only the spec fields the
    proposal actually changes are updated, so status, attempts, approval
    fields and links survive untouched (AC-3)."""
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
    # Same normalisation as a fresh task (_task_from_proposal): a queue
    # written before the key existed (or by hand) still merges out with a
    # list, not a missing key.
    merged.setdefault("attempts", [])
    return merged, changed


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
        ImportRefused: The proposal names another project, duplicates an
            id, carries forbidden fields or statuses, names an unknown
            out-of-scope record — or the merged document would not
            validate (AC-1).
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
        new_task, changed_fields = _merge_task(existing, proposed)
        merged.append(new_task)
        changes.append(
            TaskChange(
                task_id=task_id,
                kind="changed" if changed_fields else "unchanged",
                fields=tuple(changed_fields),
            )
        )
    for task in proposed_tasks:
        if task["id"] in by_id:
            continue
        merged.append(copy.deepcopy(task))
        changes.append(TaskChange(task_id=task["id"], kind="new"))

    proposed_doc: dict[str, Any] = {
        "version": 1,
        "project": project,
        "tasks": merged,
    }
    if existing_doc is not None and existing_doc.get("updated_at"):
        # Inherited so the diff shows the tasks, not a fresh stamp (the
        # write path stamps its own).
        proposed_doc["updated_at"] = existing_doc["updated_at"]
    try:
        validate_queue(proposed_doc, f"proposed tasks.json for {project}")
    except ContractError as exc:
        raise ImportRefused(str(exc)) from exc

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
            lines.append(f"  - {change.task_id}: {', '.join(change.fields)}")
    if missing:
        lines.append(
            f"- not in the proposal, kept as is ({len(missing)}): "
            f"{', '.join(missing)}"
        )
    lines.append(
        "- every proposed task is draft; approval stays yours (factory approve)"
    )
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
    fresh queue is validated once more and written atomically.

    Args:
        plan: The plan to apply.
        queue_path: The queue file.
        confirm: Called before anything is written; False writes nothing.

    Returns:
        True if the queue was written, False if the confirm declined.
    """
    if not confirm():
        return False
    path = Path(queue_path)
    if path.is_file():
        queue = TaskQueue(path)
        queue.doc["tasks"] = copy.deepcopy(plan.proposed_doc["tasks"])
        # save() validates the whole document, takes the writer lock,
        # re-reads the disk queue and enforces the append-only attempts
        # rule — the same guarantees every other writer goes through.
        queue.save()
    else:
        doc = copy.deepcopy(plan.proposed_doc)
        doc["updated_at"] = now_iso()
        validate_queue(doc, str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        # The package's own atomic writer (factory.state); a fresh queue
        # has no TaskQueue instance yet, so the temp+rename write is used
        # directly — same-package use of the one writer primitive.
        _atomic_write_json(path, doc)
    return True


# ------------------------------------------------------------------- entry --


@dataclass
class ImportResult:
    """What a run proposed: the plan, the rendered summary, the resolved
    queue path and whether it was written."""

    plan: ImportPlan
    summary: str
    queue_path: Path
    applied: bool


def run_import(
    repo: Path | str,
    queue_path: Path | str | None = None,
    *,
    docs_dir: str = DEFAULT_DOCS_DIR,
    session: Callable[[str], str] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> ImportResult:
    """Propose a queue from the project's planning documents.

    Reads the out-of-scope register, runs the importer session (or the
    injected ``session``), parses and merges the proposal, renders the
    summary and — only when ``confirm`` is given and returns True —
    writes the queue. With ``confirm=None`` this is a pure proposal:
    nothing is written, whatever the summary says.

    Args:
        repo: The project repo root (the planning docs and the register
            live there).
        queue_path: The queue file; default ``<repo>/.factory/tasks.json``.
        docs_dir: Repo-relative planning-docs directory for the prompt.
        session: Injected importer session (``prompt -> text``); default
            :func:`sdk_import_session`.
        confirm: ``summary -> bool``; asked before the one and only write.

    Returns:
        The :class:`ImportResult` — plan, summary, resolved queue path,
        and whether the queue was written.

    Raises:
        ImportRefused: No session output could be parsed or merged into a
            schema-valid, all-draft proposal (AC-1/AC-2).
    """
    repo = Path(repo)
    if queue_path is None:
        queue_path = repo / ".factory" / "tasks.json"
    queue_path = Path(queue_path)
    records = read_out_of_scope(repo)
    existing_doc = load_tasks(queue_path) if queue_path.is_file() else None
    if existing_doc is not None:
        project = existing_doc["project"]
    else:
        project = load_profile(repo / ".factory" / "project.yaml")["name"]
    prompt = build_import_prompt(repo, docs_dir, records)
    if session is not None:
        text = session(prompt)
    else:
        text = sdk_import_session(prompt, repo)
    plan = plan_import(existing_doc, parse_proposal(text), records, project=project)
    summary = render_summary(plan)
    applied = False
    if confirm is not None:
        applied = apply_import(
            plan, queue_path, confirm=lambda: bool(confirm(summary))
        )
    return ImportResult(
        plan=plan, summary=summary, queue_path=queue_path, applied=applied
    )


def sdk_import_session(
    prompt: str,
    repo: Path | str,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    timeout_min: float = DEFAULT_TIMEOUT_MIN,
) -> str:
    """The default importer session: the reviewer seat (read-only — no
    Write/Edit) over the project repo, returning its final message.

    The in-process call is the library seam; an unattended run wraps the
    same seat through the sandboxed runner
    (``factory.session.run_session_sandboxed``). A machine with no model
    credential refuses here — review a prepared proposal with
    ``factory import --proposal`` instead.
    """
    token = seat_token()
    if not token:
        raise ImportRefused(
            "no Z.ai token available (keychain item factory-zai), so the "
            "importer session cannot run; prepare a proposal elsewhere and "
            "review it with `factory import --proposal <file>`"
        )
    record = asyncio.run(
        run_session(
            "reviewer", prompt, Path(repo), max_turns, timeout_min, token=token
        )
    )
    if record.status != "completed":
        raise ImportRefused(
            f"the importer session ended {record.status}: {record.reason}"
        )
    return record.result or ""
