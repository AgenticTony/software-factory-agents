"""Contract loading and validation (F-02).

The factory refuses to operate on state it cannot trust. Every load of
``tasks.json`` or ``project.yaml`` is checked twice: first against the
semantic rules the queue depends on (readable, task-named messages), then
against the JSON Schemas as the general backstop. The schemas are the
authoritative rule set; the semantic layer is a readable front-end that
mirrors the subset needing human-readable messages and must stay
scope-matched to them (see the mirror notes below).

Any failure — unreadable file, bad parse, semantic violation, schema
violation — raises ContractError with a message a human can act on.
"""

import functools
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jsonschema.exceptions import best_match

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

# Kinds the factory never executes (TASK-CONTRACT §2).
# MIRRORS the schema's task allOf rule 3 (non-executable kinds limited to
# these statuses). Keep both in sync.
NON_EXECUTABLE_KINDS = {"decision", "research", "owner_only"}
NON_EXECUTABLE_STATUSES = {"draft", "not_buildable", "done"}

# Statuses that require a frozen contract. MIRRORS the schema's task allOf
# rule 2 (post-approval statuses require acceptance_hash + approved_at).
# Keep both in sync.
POST_APPROVAL_STATUSES = {
    "ready",
    "in_progress",
    "in_review",
    "deploying",
    "awaiting_owner",
    "done",
}


class ContractError(Exception):
    """Invalid contract state. The message is written for humans."""


def canonical_json(value: Any) -> str:
    """Canonicalise to the hashing form.

    Args:
        value: Any JSON-serialisable value.

    Returns:
        Canonical JSON string: sorted keys, no whitespace.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def acceptance_hash(acceptance: list) -> str:
    """Hash a task's acceptance list.

    Frozen at spec approval (F-04); any later edit to the acceptance text
    changes the hash and sends the task back to draft (F-03).

    Args:
        acceptance: The task's ``acceptance`` list.

    Returns:
        Hex SHA-256 digest of the canonical JSON of ``acceptance``.
    """
    return hashlib.sha256(canonical_json(acceptance).encode("utf-8")).hexdigest()


def _field(task: dict, key: str) -> Any:
    """Read a task field only when it is a string.

    Non-string values are returned as None so set membership and equality
    checks below never crash on unhashable input; the schema then describes
    the actual type error.
    """
    value = task.get(key)
    return value if isinstance(value, str) else None


def _check_semantics(tasks: Any, origin: str) -> None:
    """Readable front-end for the schema rules the queue depends on.

    Runs before schema validation so the named failure cases get task-specific
    messages instead of jsonschema's. Non-list/non-dict shapes are left to
    the schema, which reports them with structure.

    Args:
        tasks: The parsed ``tasks`` array.
        origin: File name for error messages.

    Raises:
        ContractError: On the first semantic violation found.
    """
    if not isinstance(tasks, list):
        return
    seen_ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        tid = task.get("id")
        if isinstance(tid, str):
            if tid in seen_ids:
                raise ContractError(
                    f"{origin}: duplicate task id {tid}; task ids must be "
                    "unique or dependency and attempt records become ambiguous"
                )
            seen_ids.add(tid)
        tid = tid if isinstance(tid, str) else "<missing id>"
        risk = task.get("risk")
        gates = task.get("gates") if isinstance(task.get("gates"), dict) else {}

        if risk and gates.get("merge") == "auto":
            raise ContractError(
                f"{origin}: task {tid} has risk flags {risk} but gates.merge=auto; "
                "any risk flag forces human merge (TASK-CONTRACT §2)"
            )
        kind = _field(task, "kind")
        status = _field(task, "status")
        if kind in NON_EXECUTABLE_KINDS and status not in NON_EXECUTABLE_STATUSES:
            shown = status if status is not None else "<missing>"
            allowed = ", ".join(sorted(NON_EXECUTABLE_STATUSES))
            raise ContractError(
                f"{origin}: task {tid} is kind={kind} but status={shown}; "
                f"{kind} tasks are never executable — allowed statuses: {allowed}"
            )
        if kind == "owner_only" and status == "done":
            # F-29: no contract to freeze; done means every owner step ticked.
            steps = task.get("owner_steps") or []
            if not steps or not all(step.get("done") for step in steps):
                raise ContractError(
                    f"{origin}: task {tid} is an owner_only task marked done, but "
                    "it needs at least one owner step and every one ticked (factory tick)"
                )
        elif status in POST_APPROVAL_STATUSES:
            missing = [k for k in ("acceptance_hash", "approved_at") if not task.get(k)]
            if missing:
                raise ContractError(
                    f"{origin}: task {tid} is {status} but has no {' or '.join(missing)}; "
                    "approve the spec first so the contract is frozen (factory approve)"
                )


def _read(path: Path, origin: str) -> str:
    """Read a file, refusing unreadable/non-UTF-8 input.

    Args:
        path: File to read.
        origin: File name for error messages.

    Returns:
        The file's text.

    Raises:
        ContractError: If the file is missing, unreadable, or not UTF-8.
    """
    try:
        return path.read_text()
    except OSError as exc:
        raise ContractError(f"{origin}: cannot read file: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ContractError(f"{origin}: file is not valid UTF-8: {exc}") from exc


@functools.lru_cache(maxsize=None)
def _validator(schema_path: Path) -> jsonschema.Draft202012Validator:
    """Build (once) the validator for a schema file.

    Args:
        schema_path: Path to the JSON Schema file.

    Returns:
        A cached validator for that schema.

    Raises:
        ContractError: If the schema file is missing, unreadable, or not JSON.
    """
    try:
        schema = json.loads(_read(schema_path, str(schema_path)))
    except json.JSONDecodeError as exc:
        raise ContractError(f"schema {schema_path} is not valid JSON: {exc}") from exc
    return jsonschema.Draft202012Validator(schema)


def _validate(instance: Any, schema_path: Path, origin: str) -> None:
    """Validate against a schema, raising the most relevant error.

    Args:
        instance: Parsed document to check.
        schema_path: Path to the JSON Schema file.
        origin: File name for error messages.

    Raises:
        ContractError: On the first (most relevant) schema violation.
    """
    error = best_match(_validator(schema_path).iter_errors(instance))
    if error is not None:
        loc = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise ContractError(f"{origin}: schema violation at {loc}: {error.message}")


def validate_queue(
    data: Any, origin: str = "tasks", schema_path: Path | None = None
) -> None:
    """Validate a parsed queue document: semantics, then schema.

    The write path (F-03) uses this before persisting so invalid queue
    state can never be written; ``load_tasks`` uses it after parsing.

    Args:
        data: The parsed queue document.
        origin: File name (or label) for error messages.
        schema_path: Optional override for the tasks schema path.

    Raises:
        ContractError: On the first semantic or schema violation found.
    """
    _check_semantics(data.get("tasks") if isinstance(data, dict) else None, origin)
    _validate(data, schema_path or SCHEMA_DIR / "tasks.schema.json", origin)


def load_tasks(path: Path | str, schema_path: Path | None = None) -> dict:
    """Load and fully validate a tasks.json queue.

    Args:
        path: Path to the queue file (e.g. ``.factory/tasks.json``).
        schema_path: Optional override for the tasks schema path.

    Returns:
        The parsed queue document.

    Raises:
        ContractError: If the file is unreadable or not JSON, semantically
            invalid, or schema-invalid.
    """
    path = Path(path)
    origin = str(path)
    try:
        data = json.loads(_read(path, origin))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{origin}: invalid JSON: {exc}") from exc
    validate_queue(data, origin, schema_path)
    return data


def load_profile(path: Path | str, schema_path: Path | None = None) -> dict:
    """Load and fully validate a project.yaml profile.

    Args:
        path: Path to the profile file (e.g. ``.factory/project.yaml``).
        schema_path: Optional override for the profile schema path.

    Returns:
        The parsed profile mapping.

    Raises:
        ContractError: If the file is unreadable or not YAML, not a mapping,
            or schema-invalid.
    """
    path = Path(path)
    origin = str(path)
    try:
        data = yaml.safe_load(_read(path, origin))
    except yaml.YAMLError as exc:
        raise ContractError(f"{origin}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(
            f"{origin}: profile must be a YAML mapping, got {type(data).__name__}"
        )
    _validate(data, schema_path or SCHEMA_DIR / "project.schema.json", origin)
    return data
