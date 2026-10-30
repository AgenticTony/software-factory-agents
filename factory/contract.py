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


def read_file(path: Path, origin: str) -> str:
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
        return path.read_text(encoding="utf-8")
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
        schema = json.loads(read_file(schema_path, str(schema_path)))
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


def validate_against_schema(instance: Any, schema_path: Path | str,
                            origin: str) -> None:
    """Public schema validation for the repo's other documents (F-22's
    toolkit index is the first consumer: it wraps this in ToolkitError).

    See :func:`_validate` — the shape is identical; this is the seam for
    callers outside this module.
    """
    _validate(instance, Path(schema_path), origin)


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
        data = json.loads(read_file(path, origin))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{origin}: invalid JSON: {exc}") from exc
    validate_queue(data, origin, schema_path)
    return data


def _check_profile_semantics(data: dict, origin: str, *,
                             toolkit_root: Path | str | None = None) -> None:
    """Readable front-end checks on a parsed profile (schema backs them).

    Args:
        data: The parsed profile mapping.
        origin: Profile path, for the error message.
        toolkit_root: Toolkit index root for the ``toolkit`` names check
            (F-22 AC-6); default the factory checkout's ``toolkit/``.

    Raises:
        ContractError: On the first semantic violation found.
    """
    sentinels = data.get("sentinels") or {}
    risk_globs = (data.get("paths") or {}).get("risk") or {}
    for flag, entries in sentinels.items():
        if not risk_globs.get(flag):
            raise ContractError(
                f"{origin}: sentinels.{flag} has no paths.risk globs, so its "
                f"{len(entries)} sentinel(s) could never run — add "
                f"paths.risk.{flag} globs or drop the sentinels key "
                "(a skipped-forever sentinel reads as checked when it can't be)"
            )
    _check_profile_toolkit(data.get("toolkit"), origin,
                           toolkit_root=toolkit_root)


def _check_profile_toolkit(section: Any, origin: str, *,
                           toolkit_root: Path | str | None) -> None:
    """F-22 AC-6: every toolkit name in a profile must be an index entry
    of the right kind, and a seat section may only narrow (a subset of
    the project's own lists) — never widen. Unknown or unvetted names
    refuse the profile at load, exactly as F-02 refuses invalid state.

    The import is deferred: factory.toolkit imports this module, so a
    module-level cycle would bite at import time.
    """
    if section is None:
        return
    from factory.toolkit import ToolkitError, default_toolkit_dir, load_index

    root = Path(toolkit_root) if toolkit_root is not None else default_toolkit_dir()
    try:
        index = load_index(root)
    except ToolkitError as exc:
        raise ContractError(f"{origin}: {exc}") from exc
    kinds: dict[str, str] = {
        entry["name"]: entry["kind"] for entry in index["items"]
    }
    lists = (("skills", "skill"), ("plugins", "plugin"),
             ("mcp_servers", "mcp_server"))
    for key, kind in lists:
        for name in section.get(key) or []:
            if name not in kinds:
                raise ContractError(
                    f"{origin}: toolkit.{key} names {name!r}, which is not "
                    f"in the toolkit index ({root / 'index.yaml'}) — every "
                    "toolkit name must be an admitted index entry (F-22 AC-6)"
                )
            if kinds[name] != kind:
                raise ContractError(
                    f"{origin}: toolkit.{key} names {name!r}, which is a "
                    f"{kinds[name]} in the index, not a {kind}"
                )
    for seat, seat_section in (section.get("seats") or {}).items():
        for key, _kind in lists:
            widened = sorted(
                set(seat_section.get(key) or [])
                - set(section.get(key) or [])
            )
            if widened:
                raise ContractError(
                    f"{origin}: toolkit.seats.{seat}.{key} widens beyond the "
                    f"project's toolkit.{key}: {', '.join(map(repr, widened))} "
                    "— a seat narrows the toolkit, never extends it (AC-6)"
                )


def load_profile(path: Path | str, schema_path: Path | None = None, *,
                 toolkit_root: Path | str | None = None) -> dict:
    """Load and fully validate a project.yaml profile.

    Args:
        path: Path to the profile file (e.g. ``.factory/project.yaml``).
        schema_path: Optional override for the profile schema path.
        toolkit_root: Optional override for the toolkit index root
            (F-22's ``toolkit:`` names check; default this checkout's
            ``toolkit/`` dir).

    Returns:
        The parsed profile mapping.

    Raises:
        ContractError: If the file is unreadable or not YAML, not a mapping,
            schema-invalid, or semantically inconsistent (F-25: a
            ``sentinels.<flag>`` whose flag has no ``paths.risk`` globs;
            F-22: a toolkit name that is not an admitted index entry, or
            a seat section that widens the project's toolkit).
    """
    path = Path(path)
    origin = str(path)
    try:
        data = yaml.safe_load(read_file(path, origin))
    except yaml.YAMLError as exc:
        raise ContractError(f"{origin}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(
            f"{origin}: profile must be a YAML mapping, got {type(data).__name__}"
        )
    _validate(data, schema_path or SCHEMA_DIR / "project.schema.json", origin)
    _check_profile_semantics(data, origin, toolkit_root=toolkit_root)
    return data
