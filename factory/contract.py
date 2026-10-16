"""Contract loading and validation (F-02).

The factory refuses to operate on state it cannot trust. Every load of
``tasks.json`` or ``project.yaml`` is checked twice: first against the
semantic rules the queue depends on (readable, task-named messages), then
against the JSON Schemas as the general backstop. Any failure raises
ContractError with a message a human can act on — a stack of raw
jsonschema output is not acceptable at 3am.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

# Kinds the factory never executes (TASK-CONTRACT §2). They surface to the
# owner; only build/docs are ever picked.
NON_EXECUTABLE_KINDS = {"decision", "research", "owner_only"}


class ContractError(Exception):
    """Invalid contract state. The message is written for humans."""


def canonical_json(value: Any) -> str:
    """Canonical form: sorted keys, no whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def acceptance_hash(acceptance: list) -> str:
    """SHA-256 of the canonical JSON of a task's acceptance list.

    Frozen at spec approval (F-04); any later edit to the acceptance text
    changes the hash and sends the task back to draft (F-03).
    """
    return hashlib.sha256(canonical_json(acceptance).encode("utf-8")).hexdigest()


def _check_semantics(tasks: Any, origin: str) -> None:
    """The rules the queue depends on, checked before schema validation so
    the named cases get task-specific messages instead of jsonschema's."""
    if not isinstance(tasks, list):
        return  # malformed shape: let the schema report it with structure
    for task in tasks:
        if not isinstance(task, dict):
            continue
        tid = task.get("id", "<missing id>")
        risk = task.get("risk") or []
        gates = task.get("gates") if isinstance(task.get("gates"), dict) else {}

        if risk and gates.get("merge") == "auto":
            raise ContractError(
                f"{origin}: task {tid} has risk flags {risk} but gates.merge=auto; "
                f"any risk flag forces human merge (TASK-CONTRACT §2)"
            )
        kind = task.get("kind")
        status = task.get("status")
        if kind in NON_EXECUTABLE_KINDS and status == "ready":
            raise ContractError(
                f"{origin}: task {tid} is kind={kind} but status=ready; "
                f"{kind} tasks are never executable — they live in draft or not_buildable"
            )
        if status == "ready" and not task.get("acceptance_hash"):
            raise ContractError(
                f"{origin}: task {tid} is ready but has no acceptance_hash; "
                "approve the spec first so the contract is frozen (factory approve)"
            )


def _validate(instance: Any, schema_path: Path, origin: str) -> None:
    try:
        schema = json.loads(schema_path.read_text())
    except FileNotFoundError as exc:
        raise ContractError(f"{origin}: schema not found at {schema_path}") from exc
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise ContractError(f"{origin}: schema violation at {path}: {error.message}")


def load_tasks(path: Path | str, schema_path: Path | None = None) -> dict:
    """Load and fully validate a tasks.json queue. Refuses invalid state."""
    path = Path(path)
    origin = str(path)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ContractError(f"{origin}: invalid JSON: {exc}") from exc
    _check_semantics(data.get("tasks") if isinstance(data, dict) else None, origin)
    _validate(data, schema_path or SCHEMA_DIR / "tasks.schema.json", origin)
    return data


def load_profile(path: Path | str, schema_path: Path | None = None) -> dict:
    """Load and fully validate a project.yaml profile. Refuses invalid state."""
    path = Path(path)
    origin = str(path)
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ContractError(f"{origin}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(
            f"{origin}: profile must be a YAML mapping, got {type(data).__name__}"
        )
    _validate(data, schema_path or SCHEMA_DIR / "project.schema.json", origin)
    return data
