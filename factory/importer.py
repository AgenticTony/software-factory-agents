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
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import json
import os
import re
import shlex
import subprocess
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
from factory.runs import (
    FACTORY_ROOT_DEFAULT,
    RunError,
    cleanup_run,
    create_run,
    new_run_id,
)
from factory.sandbox import SandboxError
from factory.sentinels import redact_credentials
from factory.session import SessionConfigError, run_session_sandboxed, seat_token
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
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ImportRefused(
                f"{OUT_OF_SCOPE_DIR}/{path.name}: cannot read the "
                f"out-of-scope record: {exc}"
            ) from exc
        records.append({"path": f"{OUT_OF_SCOPE_DIR}/{path.name}", "text": text})
    return records


# ------------------------------------------------------------------ prompt --


def build_import_prompt(
    repo: Path, docs_dir: str, records: list[dict], *, project: str | None = None
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

    The name is stated, not described: a placeholder like
    ``"<project name>"`` left the session to guess from the repository
    directory's spelling, and a one-letter-case guess was refused by
    :func:`plan_import` after a full session had already been spent.
    """
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
    lines += [
        "",
        "Output — and output nothing else — one JSON object:",
        contract,
        "Each task carries: id, title, source (repo-relative path of the doc",
        "that owns the spec, with #heading when useful), kind (build | docs |",
        "decision | research | owner_only), priority (P1 | P2 | P3),",
        "depends_on, risk, gates {spec_approval, merge, deploy}, acceptance",
        "[{id, text, check {type, ref}}], budget {size, max_attempts,",
        "max_turns, wall_clock_min}, and owner_steps / slices when the doc",
        "defines them.",
        "Rules:",
        *naming_rule,
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
    # The queue the plan was built from (None: no queue existed). The
    # write refuses if the disk queue no longer matches it, so a change
    # made while the confirm prompt waits is never silently reverted.
    base_doc: dict | None = None


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


def _merge_task(existing: dict, proposed: dict) -> tuple[dict, list[str]]:
    """Merge a proposal into an existing task: only the spec fields the
    proposal carries *and* changes are updated, so status, attempts,
    approval fields, links and every spec field the proposal leaves out
    survive untouched (AC-3)."""
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
    # Same normalisation as a fresh task (_new_task): a queue
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
        merged.append(_new_task(task))
        changes.append(TaskChange(task_id=task["id"], kind="new"))

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
    fresh queue is validated once more and written atomically. Either
    way the queue on disk must still be the one the plan was built from
    (``plan.base_doc``): a change made in between — an approval, a
    status move, a queue created by another writer — refuses the write
    rather than being overwritten with the stale plan.

    Args:
        plan: The plan to apply.
        queue_path: The queue file.
        confirm: Called before anything is written; False writes nothing.

    Returns:
        True if the queue was written, False if the confirm declined.

    Raises:
        ImportRefused: The queue changed since the plan was built.
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
        # The package's own atomic writer (factory.state); a fresh queue
        # has no TaskQueue instance yet, so the temp+rename write is used
        # directly — same-package use of the one writer primitive.
        _atomic_write_json(path, doc)
    return True


# ------------------------------------------------- refused output kept --


def _refusal_suffix(text: str) -> str:
    """``.json`` when the session's output is JSON (fence stripped, as
    :func:`parse_proposal` reads it), ``.txt`` when it is not — the
    extension says what there is to fix."""
    stripped = text.strip()
    match = _FENCE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        return ".txt"
    return ".json"


def _save_refused_output(
    text: str, project: str, factory_root: Path | str | None
) -> Path:
    """Keep the importer session's raw output for a refused proposal.

    The sandboxed session's run dir is deleted on every exit path, so
    without this a refused proposal cost the whole session and left
    nothing to fix and re-review with ``factory import --proposal``.
    The copy is credential-redacted
    (:func:`factory.sentinels.redact_credentials` — the same scrubber
    sentinel output goes through; no secrets are *expected* in a
    proposal, but the session writes the text, not us) and written with
    mode 0600 under ``<factory-root>/import/`` as
    ``<project>-<run id>.proposal.json`` (``.txt`` when the output is
    not JSON). The id is :func:`factory.runs.new_run_id` — a UTC stamp
    plus 6 uuid hex, unique per mint (ids minted outside a run are its
    documented use) — so one create-only open suffices.

    Raises:
        OSError: The factory root is unwritable — the caller keeps the
            original refusal; the save never masks it.
    """
    root = Path(factory_root) if factory_root is not None else FACTORY_ROOT_DEFAULT
    directory = root / "import"
    directory.mkdir(parents=True, exist_ok=True)
    # Schema names are slugs (^[a-z0-9-]+$); the sanitiser is defence in
    # depth for a hand-edited queue, whose project still reaches here.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", project)
    path = directory / f"{safe}-{new_run_id()}.proposal{_refusal_suffix(text)}"
    # O_EXCL: create-only, never overwriting an earlier refusal; the
    # mode is set at creation, not by a later chmod.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(redact_credentials(text))
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
) -> Exception:
    """Keep a real session's refused output; return the refusal to raise.

    One seam for every refusal after a session returns: run_import's
    parse/plan/apply guard and the CLI's own apply guard both go through
    here, so no path throws an expensive proposal away — a stale queue
    found at apply time (the session already ran) loses the output
    exactly like a parse refusal does.

    An :class:`ImportRefused` comes back with the saved file's path and
    the full re-review command in its message. Any other exception (a
    write ``OSError``) keeps its type — its callers already refuse on
    it — and the saved path rides along as a note on the exception; a
    failed save never masks the original refusal.
    """
    try:
        saved = _save_refused_output(text, project, factory_root)
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


# ------------------------------------------------------------------- entry --


@dataclass
class ImportResult:
    """What a run proposed: the plan, the rendered summary, the resolved
    queue path, whether it was written, and the session's raw output.

    ``raw_output`` travels to the caller (the CLI holds the interactive
    apply outside run_import) so a refusal at *its* apply can still keep
    the output through :func:`keep_refused_import_output`.
    """

    plan: ImportPlan
    summary: str
    queue_path: Path
    applied: bool
    raw_output: str = ""


def run_import(
    repo: Path | str,
    queue_path: Path | str | None = None,
    *,
    docs_dir: str = DEFAULT_DOCS_DIR,
    session: Callable[[str], str] | None = None,
    confirm: Callable[[str], bool] | None = None,
    factory_root: Path | str | None = None,
    keep_refused: bool = False,
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

    Returns:
        The :class:`ImportResult` — plan, summary, resolved queue path,
        whether the queue was written, and the session's raw output.

    Raises:
        ImportRefused: No session output could be parsed or merged into
            a schema-valid, all-draft proposal (AC-1/AC-2), or the
            apply was refused (the queue changed while the confirm
            waited). When the output was expensive (the real session, or
            ``keep_refused``), the raw output is saved (redacted, 0600)
            under ``<factory-root>/import/`` and the message names the
            file and the full re-review command. A write ``OSError``
            after the session keeps its type — the copy is still saved,
            noted on the exception.
    """
    repo = Path(repo)
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
    prompt = build_import_prompt(repo, docs_dir, records, project=project)
    if session is not None:
        text = session(prompt)
    else:
        if profile is None:
            # The sandbox settings (docs/network allowlists, protected
            # paths) come from the profile; no profile, no session.
            profile = load_profile(repo / ".factory" / "project.yaml")
        text = sdk_import_session(
            prompt, repo, profile=profile, queue_path=queue_path,
            factory_root=factory_root,
        )
    keep = session is None or keep_refused
    try:
        # Everything after the session returns: parse, plan, and the
        # apply a changed queue or failed write can refuse. The
        # session's output is the expensive part (a real run's clone is
        # already deleted by now) — on any of these refusals it is
        # kept, and the refusal says where, so the fix is a file edit
        # plus one command, not another session. A stub re-runs free
        # and keeps nothing (unless keep_refused).
        proposal = parse_proposal(text)
        plan = plan_import(existing_doc, proposal, records, project=project)
        summary = render_summary(plan)
        applied = False
        if confirm is not None:
            applied = apply_import(
                plan, queue_path, confirm=lambda: bool(confirm(summary))
            )
    except (ImportRefused, OSError) as exc:
        if keep:
            kept = keep_refused_import_output(
                exc, text=text, project=project, repo=repo,
                queue_path=queue_path, docs_dir=docs_dir,
                factory_root=factory_root,
            )
            if kept is not exc:
                raise kept from exc
        raise
    return ImportResult(
        plan=plan, summary=summary, queue_path=queue_path, applied=applied,
        raw_output=text,
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

    Args:
        prompt: The importer prompt (:func:`build_import_prompt`).
        repo: The project repo root (cloned, never used as the cwd).
        profile: The project profile — the sandbox's docs/network
            allowlists and ``paths.protected``.
        queue_path: The queue whose run lease to hold; None or a missing
            file holds none (a fresh project has no orchestrator).
        factory_root: Factory root (default ``~/factory``).
        max_turns/timeout_min: The session's budget.

    Raises:
        ImportRefused: No model credential (review a prepared proposal
            with ``factory import --proposal`` instead), the clone or the
            sandbox could not be set up, the session timed out, or it
            ended other than ``completed`` (a reviewer that mutated its
            clone ends ``failed``).
    """
    token = seat_token()
    if not token:
        raise ImportRefused(
            "no Z.ai token available (keychain item factory-zai), so the "
            "importer session cannot run; prepare a proposal elsewhere and "
            "review it with `factory import --proposal <file>`"
        )
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    queue = (
        TaskQueue(queue_path)
        if queue_path is not None and Path(queue_path).is_file() else None
    )
    lease = queue.idle_lease() if queue is not None else contextlib.nullcontext()
    with lease:
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
            )
        except (SessionConfigError, SandboxError, RunError) as exc:
            # Runner setup errors: no bundled CLI / token (SessionConfigError),
            # srt missing or not at the pin (SandboxError, from ensure_srt
            # before anything spawns), a run-dir fault (RunError). Each is
            # a refusal (exit 1 at the CLI), never a traceback.
            raise ImportRefused(f"the importer session could not run: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ImportRefused(
                f"the importer session timed out after {exc.timeout:.0f}s"
            ) from exc
        finally:
            cleanup_run(run)
    if record.status != "completed":
        raise ImportRefused(
            f"the importer session ended {record.status}: {record.reason}"
        )
    return record.result or ""
