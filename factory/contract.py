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

F-49 adds the queue's integrity checks: every validator built here
enforces ``format`` keywords (a malformed ``date-time`` used to be
annotation-only), and :func:`resolve_test_ref` statically proves an
acceptance ref names a function pytest would collect as a test — the CI
guard (and any future caller, e.g. an approve-time check) resolves refs
BEFORE the orchestrator can spend a session discovering a broken
contract mid-run.
"""

import ast
import configparser
import copy
import fnmatch
import functools
import hashlib
import importlib.resources
import json
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jsonschema
import yaml
from jsonschema.exceptions import best_match

from factory.runs import ProjectSlugError, project_component

# F-51 (I-1): the schemas ship INSIDE the package (factory/data/schema)
# and are resolved through importlib.resources — the ONE resolver — so a
# wheel install reads exactly what this checkout reads. No
# ``Path(__file__).parent.parent`` escapes, no CWD-relative reads: those
# are the two ways the package only worked from the checkout.
SCHEMA_DIR = Path(
    str(importlib.resources.files("factory").joinpath("data", "schema"))
)

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


# F-63 AC-1: the whole-contract hash covers every field EXCEPT what the
# factory itself legitimately writes to a task after approval. This list
# is pinned by test (tests/test_f63.py::test_contract_hash_exclusion_list):
# anything else a task carries — including fields a future schema adds —
# changes the hash.
CONTRACT_HASH_EXCLUDED_FIELDS = (
    "status",           # the lifecycle the ledger gates, not the contract
    "acceptance_hash",  # written at approval, mirrors `acceptance`
    "approved_by",      # written at approval
    "approved_at",      # written at approval
    "attempts",         # append-only run history
    "attempt_resets",   # append-only budget history
    "links",            # orchestrator-written PR/branch bookkeeping
)

# The tick-written completion fields of one owner step (F-63 AC-1):
# `tick_owner_steps` writes all four together, so all four are outside
# the hash — ticking an owner step after approval is not a contract
# change. The step's `text` (and anything else) stays IN the hash.
OWNER_STEP_HASH_EXCLUDED_FIELDS = ("done", "done_at", "done_by", "evidence")


def contract_hash(task: dict) -> str:
    """Hash a task's whole contract (F-63 AC-1).

    SHA-256 over canonical JSON (sorted keys, fixed separators,
    ``ensure_ascii=False``) of the task minus exactly
    :data:`CONTRACT_HASH_EXCLUDED_FIELDS` and each owner step's
    :data:`OWNER_STEP_HASH_EXCLUDED_FIELDS`. Unlike
    :func:`acceptance_hash` (acceptance text only), this covers risk,
    gates, budget, out_of_scope and every other contract field, so an
    edit to ANY of them invalidates the owner's ledger approval and the
    task goes back to draft. Acceptance list ORDER is semantic and kept
    (it is the order criteria are proven in).

    Args:
        task: The task record.

    Returns:
        Hex SHA-256 digest.
    """
    trimmed = {
        key: value for key, value in task.items()
        if key not in CONTRACT_HASH_EXCLUDED_FIELDS
    }
    steps = []
    for step in trimmed.get("owner_steps") or []:
        if isinstance(step, dict):
            steps.append({
                key: value for key, value in step.items()
                if key not in OWNER_STEP_HASH_EXCLUDED_FIELDS
            })
        else:
            steps.append(step)
    if steps or "owner_steps" in trimmed:
        trimmed["owner_steps"] = steps
    canonical = json.dumps(trimmed, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _field(task: dict, key: str) -> Any:
    """Read a task field only when it is a string.

    Non-string values are returned as None so set membership and equality
    checks below never crash on unhashable input; the schema then describes
    the actual type error.
    """
    value = task.get(key)
    return value if isinstance(value, str) else None


# F-65: the segments a scope pattern may not contain, and the classes
# refused outright. Decisions, documented (BUILD-ORDER §F-65 glob
# tricks): ``[...]`` character classes are NOT supported by the scope
# matcher (gates.scope_matches) — a bracket in a pattern would silently
# mean its fnmatch sense to some readers and a literal to others, so it
# refuses; a project with bracket-named paths covers them with ``*``.
# An empty segment (``src//x``, a trailing ``/``) and a ``.`` segment
# (``./src/**``) never match a real git path — they narrow a scope to
# nothing while reading like a real scope, so they refuse too.
_SCOPE_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\\\[\]]")
# F-65 R1-1: a segment made only of wildcards (`*` and `?`, any mix).
# `?*` matches every non-empty name exactly as `*` does, so a pattern
# whose every segment is wildcard-only (`?*/**`, `**/?*`, `*?`) matches
# every path — the structural catch-all, however spelled.
_WILDONLY_RE = re.compile(r"[*?]+")


def scope_pattern_error(pattern: Any) -> str | None:
    """Why ``pattern`` cannot be a declared-scope glob (F-65), or None.

    ONE validator for every scope-shaped pattern a load can see: a
    task's ``scope.paths`` (task semantics), the profile's
    ``paths.generated`` (profile semantics), the importer's proposals
    (per-task violations) and the parent-side validation of a session's
    scope request. The refusals:

    * not a string, or empty;
    * a NUL/control character, a backslash, or a ``[``/`]`` (character
      classes are unsupported — see :func:`factory.gates.scope_matches`);
    * absolute, or any ``..`` segment (a scope cannot climb) — and no
      ``.``/empty segment (``src//x``, ``src/``, ``./src/**``): those
      match no real git path while reading like a scope;
    * structurally catch-all: no segment carries a literal character —
      every segment is made only of ``*`` and ``?`` (``**``, ``*``,
      ``**/*``, ``*/**``, ``?*/**``, ``**/?*``, ``*?`` and equivalents
      — ``?*`` matches every non-empty name exactly as ``*`` does). A
      scope that matches every path is no scope; the rule is
      structural (no literal anywhere), not a spelling deny-list.

    Args:
        pattern: The candidate glob (any type — non-strings refuse).

    Returns:
        The refusal reason naming what is wrong, or None when the
        pattern is a well-formed scope glob.
    """
    if not isinstance(pattern, str) or not pattern:
        return "is empty (or not a string)"
    bad = _SCOPE_FORBIDDEN_CHARS.search(pattern)
    if bad is not None:
        return (
            f"carries {bad.group()!r} — control characters, backslashes and "
            "[...] character classes are refused (write the glob without them)"
        )
    if pattern.startswith("/"):
        return "is absolute — scope paths are repo-relative"
    segments = pattern.split("/")
    for seg in segments:
        if seg == "..":
            return "has a .. segment — a scope cannot climb out of the repo"
        if seg in ("", "."):
            return (
                f"has an empty or . segment ({pattern!r}) — it matches no "
                "real git path; write the clean relative glob"
            )
    if all(_WILDONLY_RE.fullmatch(seg) for seg in segments):
        return "matches every path — a declared scope must name places"
    return None


def _check_task_scope(task: dict, origin: str, tid: str) -> None:
    """Refuse a malformed declared scope, naming the task and pattern."""
    scope = task.get("scope")
    if scope is None:
        return  # absent is legal (the schema keeps it optional); the
        # approval and run refusals demand it for ELIGIBLE_KINDS
    paths = scope.get("paths") if isinstance(scope, dict) else None
    if not isinstance(paths, list):  # shape is the schema's to describe
        return
    for pattern in paths:
        reason = scope_pattern_error(pattern)
        if reason is not None:
            raise ContractError(
                f"{origin}: task {tid} declares scope pattern {pattern!r} "
                f"that {reason} (F-65)"
            )


# F-63 R2-1(b): the control classes no PROFILE string may carry, and no
# task string may carry outside its newline/tab exemptions. Same families
# as factory.egress's seams (C0, DEL, C1, bidi/isolate); refused at load
# with the key path named — a value that can rewrite the terminal (or
# reorder what the owner reads) never reaches an approval prompt.
_DISALLOWED_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]"
)
# Task free-text fields where \n and \t are legitimate today (the real
# queue's attempts[].note is multi-line; tick evidence and text fields
# may be). Every other control class is refused there too.
_TASK_FREE_TEXT_KEYS = frozenset({"note", "evidence", "text"})


def _iter_string_paths(value: Any, path: str):
    """Yield ``(path, string)`` for every string in a JSON-shaped value."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_string_paths(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_string_paths(item, f"{path}[{index}]")


def _check_control_strings(
    value: Any, origin: str, where: str, *, free_text_newline: bool
) -> None:
    """Refuse control characters in every string under ``value``.

    Args:
        value: The JSON-shaped value to walk (a profile, a task).
        origin: File name for the error message.
        where: What the walk names in the message (the profile itself,
            or the task id).
        free_text_newline: Exempt ``\n``/``\t`` — but no other control
            class — inside the task free-text fields
            (:data:`_TASK_FREE_TEXT_KEYS`). Profiles pass False: no
            profile string may carry any control, newline and tab
            included.
    """
    for path, text in _iter_string_paths(value, where):
        exempt = free_text_newline and path.rsplit(".", 1)[-1] in _TASK_FREE_TEXT_KEYS
        for position, char in enumerate(text):
            if not _DISALLOWED_CONTROL_RE.match(char):
                continue
            if exempt and char in ("\n", "\t"):
                continue
            raise ContractError(
                f"{origin}: {where}: {path} carries the control character "
                f"U+{ord(char):04X} at offset {position} — control "
                "sequences and bidi controls are refused at load: they "
                "can rewrite the terminal an approval prompt prints to"
            )


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
        # F-63 R2-1(b): control characters refused in every task string
        # (newline/tab exempt in the free-text fields only).
        _check_control_strings(task, origin, f"task {tid}",
                               free_text_newline=True)
        # F-65 AC-1: every declared scope pattern is a well-formed
        # repo-relative glob (a malformed scope refuses the load).
        _check_task_scope(task, origin, tid)
        risk = task.get("risk")
        # bound once so the isinstance guard narrows the .get below
        gates_value = task.get("gates")
        gates = gates_value if isinstance(gates_value, dict) else {}

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
def _schema_document(schema_path: Path) -> dict:
    """Load (once) a JSON Schema document.

    Args:
        schema_path: Path to the JSON Schema file.

    Returns:
        The parsed schema mapping.

    Raises:
        ContractError: If the schema file is missing, unreadable, or not JSON.
    """
    try:
        return json.loads(read_file(schema_path, str(schema_path)))
    except json.JSONDecodeError as exc:
        raise ContractError(f"schema {schema_path} is not valid JSON: {exc}") from exc


@functools.lru_cache(maxsize=None)
def _validator(schema_path: Path) -> jsonschema.Draft202012Validator:
    """Build (once) the validator for a schema file.

    The ONE place every factory-side validator is built. Every validator
    carries the draft's format checker (F-49 AC-2, m-3): ``format`` is
    annotation-only in draft 2020-12 unless a checker is passed, so a
    malformed ``date-time`` (``updated_at``, attempts' ``started``/
    ``ended``, ``approved_at``, ``done_at``) used to load silently. The
    date-time check needs the optional ``rfc3339-validator`` dependency
    — without it the checker silently accepts anything, which is why the
    dependency is pinned in ``pyproject.toml`` and F-49's tests assert
    the checker is live, not just passed.

    Args:
        schema_path: Path to the JSON Schema file.

    Returns:
        A cached validator for that schema.

    Raises:
        ContractError: If the schema file is missing, unreadable, or not JSON.
    """
    return _build_validator(_schema_document(schema_path))


def _build_validator(schema: dict) -> jsonschema.Draft202012Validator:
    """Build a format-checking validator for an already-parsed schema.

    Split from :func:`_validator` so callers holding a schema DOCUMENT
    (the importer's derived proposal schema) build through the same one
    seam — same checker, same draft — as the file-backed schemas.
    """
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )


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


def iter_schema_violations(
    instance: Any, schema: Path | str | dict
) -> list[str]:
    """Every schema violation as ``"<path>: <message>"``, sorted by path.

    :func:`_validate` raises the single most relevant error — the right
    front door for a load or a write, where one refusal names one thing
    to fix. The importer's refusal needs the other shape: a proposed
    queue can violate the same shapes in every task at once, and naming
    only the first made a 30-task proposal cost one review round per
    defect class. This lists them all, so one edit round fixes the
    proposal.

    Args:
        instance: The parsed document to check.
        schema: The JSON Schema to check against — a path to a schema
            file (built once, cached) or an already-parsed schema
            document (the importer's derived proposal schema, F-49).

    Returns:
        Every violation as ``"<path>: <message>"`` (``<root>`` for the
        document level), sorted by path; empty when the document is
        schema-valid.
    """
    if isinstance(schema, dict):
        validator = _build_validator(schema)
    else:
        validator = _validator(Path(schema))
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: "/".join(str(part) for part in error.absolute_path),
    )
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"{error.message}"
        for error in errors
    ]


def validate_queue(
    data: Any, origin: str = "tasks", schema_path: Path | None = None
) -> None:
    """Validate a parsed queue document: semantics, then schema, then
    the project slug.

    The write path (F-03) uses this before persisting so invalid queue
    state can never be written; ``load_tasks`` uses it after parsing.

    Args:
        data: The parsed queue document.
        origin: File name (or label) for error messages.
        schema_path: Optional override for the tasks schema path.

    Raises:
        ContractError: On the first semantic or schema violation found,
            or when ``project`` is not a ``[a-z0-9-]+`` slug (F-62
            AC-3). The schema's ``^…$`` pattern is not enough on its
            own: Python's ``$`` matches before a trailing newline, so
            ``"abc\\n"`` is schema-valid — the fullmatch here is the
            load-bearing check, and the builders' own
            :func:`factory.runs.project_component` is the same rule
            again one layer down (defence in depth, never the only
            layer).
    """
    _check_semantics(data.get("tasks") if isinstance(data, dict) else None, origin)
    _validate(data, schema_path or SCHEMA_DIR / "tasks.schema.json", origin)
    if isinstance(data, dict) and "project" in data:
        try:
            project_component(data["project"])
        except ProjectSlugError as exc:
            raise ContractError(f"{origin}: {exc}") from exc


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


# --------------------------------------------------------- test-ref resolution --

# pytest's own defaults for the collection knobs (pytest docs, "Changing
# naming conventions"); a repo's config can only narrow or re-spell them.
_PYTEST_DEFAULT_FILES = ("test_*.py", "*_test.py")
_PYTEST_DEFAULT_FUNCTIONS = ("test*",)
_PYTEST_DEFAULT_CLASSES = ("Test*",)


def _pytest_collection_config(repo: Path) -> tuple | str:
    """Read the repo's pytest collection config STATICALLY (F-49 review
    round 1, finding 2).

    Resolution proves a function pytest would COLLECT as a test, so the
    collection rules come from the repo itself, read in pytest's own
    config-discovery order (pytest.ini, then pyproject.toml's
    ``[tool.pytest.ini_options]``, then tox.ini's ``[pytest]``, then
    setup.cfg's ``[tool:pytest]`` — first file found wins).

    Returns:
        ``(testpaths, python_files, python_functions, python_classes)`` —
        testpaths ``None`` when unset (no placement restriction), the
        pattern tuples defaulting to pytest's — or a human-readable
        error string when a config file exists but cannot be honoured
        (unparseable): fail closed, never guess the rules.
    """
    def from_mapping(mapping: dict):
        def split(raw) -> tuple[str, ...] | None:
            if raw is None:
                return None
            if isinstance(raw, str):
                parts = raw.split()
            else:
                parts = [str(item) for item in raw]
            return tuple(parts) or None

        return (
            split(mapping.get("testpaths")),
            split(mapping.get("python_files")) or _PYTEST_DEFAULT_FILES,
            split(mapping.get("python_functions")) or _PYTEST_DEFAULT_FUNCTIONS,
            split(mapping.get("python_classes")) or _PYTEST_DEFAULT_CLASSES,
        )

    def read_ini(path: Path, section: str | None) -> dict | str | None:
        """One ini file → its pytest keys, ``None`` when it holds none.

        A synthetic root section carries the file's root-level keys —
        pytest.ini's convention is bare top-level keys, which a plain
        configparser read rejects (they sit before any section header).
        For tox.ini/setup.cfg only the named section counts (their root
        keys belong to tox/setuptools, not pytest); for pytest.ini the
        root keys ARE the config, with an explicit ``[pytest]`` section
        also honoured. A parse failure comes back as a string — fail
        closed, never guess.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"cannot read {path.name}: {exc}"
        parser = configparser.ConfigParser()
        try:
            parser.read_string("[__root__]\n" + text, source=path.name)
        except configparser.Error as exc:
            return f"{path.name} does not parse ({exc})"
        data: dict[str, str] = {}
        if section is None:
            if parser.has_section("__root__"):
                data.update(parser["__root__"])
            if parser.has_section("pytest"):
                data.update(parser["pytest"])
        elif parser.has_section(section):
            data.update(parser[section])
        return data or None

    ini_candidates = (
        ("pytest.ini", None),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    )
    for name, section in ini_candidates:
        path = repo / name
        if not path.is_file():
            continue
        parsed = read_ini(path, section)
        if isinstance(parsed, str):
            return (f"{parsed}; the pytest collection rules cannot be "
                    "read statically")
        if parsed is None:
            continue  # not a pytest config — keep looking
        return from_mapping(parsed)
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            return (
                f"pyproject.toml exists but does not parse ({exc}); the "
                "pytest collection rules cannot be read statically"
            )
        options = doc.get("tool", {}).get("pytest", {}).get("ini_options", {})
        if isinstance(options, dict) and options:
            return from_mapping(options)
    return (None, _PYTEST_DEFAULT_FILES, _PYTEST_DEFAULT_FUNCTIONS,
            _PYTEST_DEFAULT_CLASSES)


def _top_level_bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """What each TOP-LEVEL name of a module is finally bound to.

    Walks the module body in order; the last binding wins:

    * a (``async``) ``def`` binds ``"function"``;
    * a ``class`` binds ``"class"`` and records its direct methods;
    * a top-level assignment (``Assign``/``AnnAssign``/``AugAssign``
      with a bare ``Name`` target) rebinds to ``"rebound"``.

    Only DIRECT top-level statements count, on purpose: a ``def`` inside
    ``if`` (a conditional hide), inside another function, or inside a
    loop is not a statically-guaranteed top-level function — importing
    the module might see it or might not, and a later top-level
    assignment (``test_ac_1 = None``) shadows an earlier ``def`` exactly
    as it would at run time.

    Returns:
        ``(bindings, class_methods)`` — the final binding kind per
        top-level name, and the direct-method name set per top-level
        class.
    """
    bindings: dict[str, str] = {}
    methods: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings[node.name] = "function"
        elif isinstance(node, ast.ClassDef):
            bindings[node.name] = "class"
            methods[node.name] = {
                sub.name
                for sub in node.body
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = "rebound"
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                bindings[node.target.id] = "rebound"
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                bindings[node.target.id] = "rebound"
    return bindings, methods


def resolve_test_ref(repo: Path | str, ref: Any) -> str | None:
    """Resolve a contract test ref STATICALLY to a function pytest would
    collect as a test (F-49 AC-1, M-10; review round 1, finding 2).

    The module is never imported (importing runs module code and cannot
    see a function a conditional hides); the file is parsed with ``ast``
    instead:

    * ``path::name`` — a top-level ``def``/``async def`` named exactly
      ``name`` in that file, not shadowed by a later top-level
      assignment;
    * ``path::Class::name`` — a direct method of a top-level class.

    AND the ref must be something pytest would collect — a top-level
    function named ``resolve_test_ref`` in a production module is a real
    function but not a test, and a ref the runner will not collect fails
    at L1 mid-spend, which is exactly what this guard exists to prevent.
    The collection rules are read statically from the repo's pytest
    config (:func:`_pytest_collection_config`): the file's name matches
    ``python_files`` (default ``test_*.py``/``*_test.py``) and sits
    under ``testpaths`` when set; the function name matches
    ``python_functions`` (default ``test*``); a class matches
    ``python_classes`` (default ``Test*``) and defines no ``__init__``.

    The file must exist under ``repo`` and stay inside it: absolute
    paths, ``..`` climbs and symlinks resolving out are all refused — a
    ref that names no file in this repo cannot be proven here.

    Parametrised ids (``name[param]``) fail closed, consistent with F-46
    AC-5's amendment (L1 treats a not-collected contract test as a
    failure at both ends): the param cannot be proven to exist
    statically, and a ref the runner can't collect is a mid-spend
    failure exactly when the queue stops being cheap to fix. When the
    queue carries one, name the bare function.

    Args:
        repo: The repo root the ref's path is relative to.
        ref: The raw ``check.ref`` value.

    Returns:
        None when the ref resolves; a human-readable reason naming what
        is wrong when it does not. Never raises for a bad ref — a reason
        IS the answer.
    """
    if not isinstance(ref, str) or "::" not in ref:
        return f"does not name a test (expected path::name): {ref!r}"
    path_text, _, tail = ref.partition("::")
    parts = tail.split("::")
    path_text = path_text.strip()
    if not path_text:
        return f"ref has no test-file path: {ref!r}"
    if any(not part.strip() for part in parts):
        return f"ref names an empty test name: {ref!r}"
    if len(parts) > 2:
        return (
            "ref is not a test node id (expected path::name or "
            f"path::Class::name): {ref!r}"
        )
    name = parts[-1]
    if name != name.strip():
        return (
            f"test name {name!r} is padded with whitespace; the ref must "
            "name the function exactly as defined"
        )
    if "[" in name or "]" in name:
        return (
            f"parametrised test ids are not statically provable — name the "
            f"bare function (fail closed, F-46 AC-5): {ref!r}"
        )
    raw = Path(path_text)
    if raw.is_absolute():
        return f"test file path must be repo-relative, not absolute: {path_text!r}"
    if ".." in raw.parts:
        return f"test file path must not climb out of the repo: {path_text!r}"
    repo = Path(repo).resolve()
    try:
        resolved = (repo / raw).resolve(strict=True)
    except OSError:
        return f"test file {path_text!r} does not exist under the repo"
    try:
        resolved.relative_to(repo)
    except ValueError:
        return (
            f"test file {path_text!r} resolves outside the repo "
            "(symlink or climb) and cannot be proven"
        )
    if not resolved.is_file():
        return f"test file {path_text!r} does not exist under the repo"
    config = _pytest_collection_config(repo)
    if isinstance(config, str):
        return f"the repo's pytest config cannot be read: {config}"
    testpaths, file_patterns, function_patterns, class_patterns = config

    if not any(fnmatch.fnmatch(raw.name, pattern) for pattern in file_patterns):
        return (
            f"test file {path_text!r} does not match the repo's pytest "
            f"python_files patterns ({', '.join(file_patterns)}) — pytest "
            "would not collect it as a test module"
        )
    if testpaths is not None and not any(
        raw == Path(tp) or tp in raw.parents or raw.is_relative_to(tp)
        for tp in map(Path, testpaths)
    ):
        return (
            f"test file {path_text!r} is outside the repo's pytest "
            f"testpaths ({', '.join(testpaths)}) — pytest would not "
            "collect it by default"
        )
    try:
        tree = ast.parse(resolved.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return (
            f"test file {path_text!r} does not parse: {exc.msg} "
            f"(line {exc.lineno}) — a file the runner cannot collect is "
            "not a contract test"
        )
    except (OSError, UnicodeDecodeError) as exc:
        return f"test file {path_text!r} cannot be read: {exc}"
    bindings, methods = _top_level_bindings(tree)
    if len(parts) == 2:
        cls = parts[0]
        if not any(fnmatch.fnmatch(cls, pattern) for pattern in class_patterns):
            return (
                f"class {cls!r} does not match the repo's pytest "
                f"python_classes patterns ({', '.join(class_patterns)}) — "
                "pytest would not collect it"
            )
        if bindings.get(cls) != "class":
            return f"{path_text} has no top-level class named {cls!r}"
        if "__init__" in methods[cls]:
            return (
                f"{path_text}::{cls} defines __init__ — pytest does not "
                "collect classes with a constructor"
            )
        if name not in methods[cls]:
            return f"{path_text}::{cls} has no method named {name!r}"
        if not any(fnmatch.fnmatch(name, pattern) for pattern in function_patterns):
            return (
                f"test name {name!r} does not match the repo's pytest "
                f"python_functions patterns ({', '.join(function_patterns)})"
            )
        return None
    binding = bindings.get(name)
    if binding is None:
        return (
            f"{path_text} defines no top-level test function named {name!r} "
            "(a def inside a conditional, a class or another function does "
            "not count)"
        )
    if binding != "function":
        return (
            f"{path_text}::{name} is not a top-level function "
            f"(its final binding is a {binding})"
        )
    if not any(fnmatch.fnmatch(name, pattern) for pattern in function_patterns):
        return (
            f"test name {name!r} does not match the repo's pytest "
            f"python_functions patterns ({', '.join(function_patterns)}) — "
            "pytest would not collect it as a test"
        )
    return None


# F-63 AC-3: an egress allowlist entry is one exact, lowercase ASCII DNS
# name. Each label is [a-z0-9]([a-z0-9-]{0,61}[a-z0-9])? (RFC 1123 label
# shape, 63-char ceiling), at least one dot, and the final label is not
# all-numeric (kills IPv4 literals and integer-IP payloads alike).
# fullmatch — a ``^…$`` pattern admits a trailing newline (the F-62
# lesson). Everything else refuses at load with the entry named: a
# wildcard, scheme, port, path, IP literal, uppercase, non-ASCII, a
# trailing dot, padding, an empty string.
_ALLOWLIST_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_ALLOWLIST_HOST_RE = re.compile(rf"{_ALLOWLIST_LABEL}(\.{_ALLOWLIST_LABEL})+")


# F-59: region shapes, pattern-checked so a region is never free text
# interpolated into a host. Bedrock: the AWS partition shape
# (eu-north-1, us-gov-west-1, il-central-1). Vertex: the cloud-region
# shape (us-east5, europe-west1, asia-south2) plus the three specials
# the pinned CLI's URL builder names (global, us, eu).
_PROVIDER_REGION_RES: dict[str, re.Pattern[str]] = {
    "bedrock": re.compile(r"^[a-z]{2}(-gov)?(-[a-z]+)+-\d$"),
    "vertex": re.compile(r"^[a-z]+(-[a-z]+)*\d$|^(global|us|eu)$"),
}
_REGION_KINDS = ("bedrock", "vertex")
_BASE_URL_KIND = "anthropic_compatible"


def _provider_field_error(kind: str, block: dict, *, seat: str | None,
                          origin: str) -> str | None:
    """Why one provider block (the project's own, or a seat override)
    cannot stand — the field-named refusal F-59 AC-1 requires."""
    who = "provider" if seat is None else f"seat_providers.{seat}"
    region = block.get("region")
    base_url = block.get("base_url")
    if kind in _REGION_KINDS and not region:
        return (f"{who}.region is required for kind {kind!r} — the "
                "inference host is derived from it and the factory "
                "refuses to guess a region")
    if kind not in _REGION_KINDS and region is not None:
        return (f"{who}.region is refused for kind {kind!r}: only "
                f"{list(_REGION_KINDS)} derive their endpoint from a "
                "region")
    if kind == _BASE_URL_KIND and not base_url:
        return (f"{who}.base_url is required for kind {kind!r} — the "
                "endpoint is the project's own choice, not a default")
    if kind != _BASE_URL_KIND and base_url is not None:
        return (f"{who}.base_url is allowed only with kind "
                f"{_BASE_URL_KIND!r}, not {kind!r}")
    if region is not None:
        pattern = _PROVIDER_REGION_RES[kind]
        if not pattern.fullmatch(str(region)):
            return (f"{who}.region {region!r} is not a valid {kind} "
                    "region (pattern-checked: a region is never free "
                    "text that could reach a host)")
    if base_url is not None:
        problem = _provider_base_url_error(str(base_url))
        if problem is not None:
            return f"{who}.base_url {base_url!r} {problem}"
    return None


def _provider_base_url_error(base_url: str) -> str | None:
    """Why ``base_url`` cannot be an anthropic_compatible endpoint."""
    from factory.provider import model_host_owner

    parts = urlsplit(base_url)
    if parts.scheme != "https":
        return "must be https:// (the endpoint carries the provider token)"
    if parts.username or parts.password:
        return "must carry no userinfo (user:password@ is not an endpoint)"
    if parts.query or parts.fragment:
        return "must carry no query or fragment"
    host = parts.hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return "must not name localhost"
    if (not _ALLOWLIST_HOST_RE.fullmatch(host)
            or host.rsplit(".", 1)[-1].isdigit()):
        return ("must be one exact DNS host (the F-63 exact-host rule: "
                "no wildcard, IP literal, port, path or padding)")
    # R1-1: the HOST is the control, not the kind label — an
    # anthropic_compatible alias of a known model provider's host is the
    # "client source reaches the unapproved provider" path. Name the
    # owner and the right kind for it; refuse for every data_owner (a
    # self project has no reason to alias its own or another provider
    # either).
    owner = model_host_owner(host)
    if owner is not None:
        return (f"names {host!r}, a {owner}-owned model host — use kind "
                f"{owner!r} for that provider (the anthropic_compatible "
                "kind is for endpoints NO known provider family owns)")
    return None


def _check_provider_semantics(data: dict, origin: str) -> None:
    """F-59: the provider choice is load-refused prose-and-steel.

    Every rule here is the front half of the WO's goal — the owner
    approves the provider — made mechanical at load: a client project
    with no provider, or with zai, refuses; region/base_url presence and
    shapes are per kind; a seat override only from providers_allowed;
    and no allowlist entry names another provider's model host (or a
    client repo's network_allowlist would reopen Z.ai). The approval
    half is the F-63 group hash (data_owner / provider /
    providers_allowed / seat_providers); this check only decides
    whether the content can stand at all."""
    from factory.provider import (
        bedrock_runtime_host,
        model_host_owner,
        vertex_hosts,
    )

    owner = data.get("data_owner", "self")
    block = data.get("provider")
    allowed = data.get("providers_allowed") or []
    seats = data.get("seat_providers") or {}

    if owner == "client" and not isinstance(block, dict):
        raise ContractError(
            f"{origin}: a client project (data_owner: client) must "
            "declare an explicit provider block — client source may not "
            "reach the default Z.ai endpoint (F-59); name the owner-"
            "approved provider in project.yaml"
        )
    if isinstance(block, dict):
        kind = block.get("kind")
        if owner == "client" and kind == "zai":
            raise ContractError(
                f"{origin}: provider.kind is zai on a client project — "
                "client source may not go to the Z.ai endpoint (no DPA, "
                "processing outside the EU, a consumer plan); F-59 "
                "refuses the load"
            )
        error = _provider_field_error(str(kind), block, seat=None,
                                      origin=origin)
        if error is not None:
            raise ContractError(f"{origin}: {error} (F-59)")
        models = block.get("models")
        if not isinstance(models, dict) or not models:
            raise ContractError(
                f"{origin}: provider.models is required — one model id "
                "per seat (implementer, reviewer, verifier, importer); "
                "the schema types the map"
            )
    if owner == "client" and "zai" in allowed:
        raise ContractError(
            f"{origin}: providers_allowed names zai on a client project "
            "— no seat of a client project may reach the Z.ai endpoint "
            "(F-59)"
        )
    for seat, entry in seats.items():
        if not isinstance(entry, dict):
            continue  # the schema types the block
        kind = str(entry.get("kind"))
        if kind not in allowed:
            raise ContractError(
                f"{origin}: seat_providers.{seat} names provider "
                f"{kind!r} which is not in the project's "
                f"providers_allowed {allowed!r} — a seat runs the "
                "project's provider or one the owner allowed (F-59 AC-5)"
            )
        error = _provider_field_error(kind, entry, seat=str(seat),
                                      origin=origin)
        if error is not None:
            raise ContractError(f"{origin}: {error} (F-59)")
    # "No other model endpoint" (AC-3): an egress allowlist entry naming
    # a model host that is not EXACTLY one of the project's own (or a
    # seat override's) inference hosts refuses — otherwise a client
    # repo's allowlist reopens Z.ai, and a same-kind entry would reopen
    # another REGION (a data-residency hole, not a convenience).
    own_hosts: set[str] = set()
    for block in [block if isinstance(block, dict) else {},
                  *(e for e in seats.values() if isinstance(e, dict))]:
        kind = str(block.get("kind") or "")
        region = block.get("region")
        base_url = block.get("base_url")
        if kind == "zai":
            own_hosts.add("api.z.ai")
        elif kind == "anthropic":
            own_hosts.add("api.anthropic.com")
        elif kind == "bedrock" and region:
            own_hosts.add(bedrock_runtime_host(str(region)))
        elif kind == "vertex" and region:
            own_hosts.update(vertex_hosts(str(region)))
        elif kind == "anthropic_compatible" and base_url:
            own_hosts.add(urlsplit(str(base_url)).hostname or "")
    for key in ("network_allowlist", "docs_allowlist"):
        for entry in data.get(key) or []:
            if not isinstance(entry, str):
                continue
            provider_kind = model_host_owner(entry)
            if provider_kind is None:
                continue
            if entry in own_hosts:
                continue
            raise ContractError(
                f"{origin}: {key} entry {entry!r} is a model-provider "
                f"host of kind {provider_kind!r}, not this project's "
                "own inference host — the sandbox allowlist is the "
                "egress boundary of a session holding the provider "
                "token, and a client repo may not reopen another model "
                "endpoint (or another region of its own kind) through "
                "it (F-59 AC-3)"
            )


def _check_allowlist_hosts(data: dict, origin: str) -> None:
    """Refuse any network/docs allowlist entry that is not one exact
    lowercase DNS host (F-63 AC-3).

    The srt allowlist is the egress boundary of sessions that hold the
    shared provider token, and pinned srt 0.0.79 matches a bare entry
    EXACTLY (``h === pattern``; only ``*.base`` widens — verified in the
    pinned source, pinned by test). So the admitted form is precisely
    the exact-host form; anything broader or stranger is a misconfigured
    or hostile entry and refuses the profile at load, the entry named.
    """
    for key in ("network_allowlist", "docs_allowlist"):
        entries = data.get(key)
        if not isinstance(entries, list):
            continue  # the schema types the field
        for entry in entries:
            if not isinstance(entry, str) or (
                not _ALLOWLIST_HOST_RE.fullmatch(entry)
                or entry.rsplit(".", 1)[-1].isdigit()
            ):
                raise ContractError(
                    f"{origin}: {key} entry {entry!r} is not one exact "
                    "lowercase DNS host (labels [a-z0-9-], at least one dot, "
                    "final label not all-numeric) — no wildcard, scheme, "
                    "port, path, IP literal or padding is admitted, because "
                    "the allowlist is the egress boundary of a session that "
                    "holds the provider token"
                )


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
    _check_provider_semantics(data, origin)
    # F-71 AC-3: a budget floor above the task schema's F-64 caps would
    # refuse every contract (nothing could satisfy it), so it refuses at
    # load, the field named.
    floor = data.get("budget_floor")
    if isinstance(floor, dict):
        for field, cap in budget_caps().items():
            value = floor.get(field)
            if isinstance(value, int) and not isinstance(value, bool) \
                    and value > cap:
                raise ContractError(
                    f"{origin}: budget_floor.{field} is {value}, above the "
                    f"task budget cap of {cap} (F-64) — a floor no contract "
                    "can satisfy; lower the floor or raise the cap by owner "
                    "decision"
                )
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
    # F-65 AC-4: generated-path globs obey the declared-scope rules —
    # a catch-all or climbing pattern would widen every task's scope
    # implicitly, so it refuses at load, the pattern named.
    generated = (data.get("paths") or {}).get("generated")
    if isinstance(generated, list):
        for pattern in generated:
            reason = scope_pattern_error(pattern)
            if reason is not None:
                raise ContractError(
                    f"{origin}: paths.generated pattern {pattern!r} {reason} "
                    "(F-65)"
                )
    # F-62 AC-3: the profile's name feeds the same factory-root path
    # builders the queue's project does; the schema's ``^…$`` pattern
    # admits a trailing newline (Python re ``$``), so the fullmatch
    # here is the load-bearing spelling (queue-side: validate_queue).
    # Runs BEFORE the control-string walk (F-63 R2-1) so a bad name gets
    # the slug refusal that NAMES the value, not the control-character
    # one that names the key path — the slug rule subsumes controls.
    if "name" in data:
        try:
            project_component(data["name"])
        except ProjectSlugError as exc:
            raise ContractError(f"{origin}: {exc}") from exc
    # F-63 AC-3: exact-host egress entries, refused at load (see
    # _check_allowlist_hosts).
    _check_allowlist_hosts(data, origin)
    # F-63 R2-1(b): no control character in ANY profile string — the
    # profile feeds the approval prompt and the run's own paths.
    _check_control_strings(data, origin, "profile", free_text_newline=False)
    _check_profile_toolkit(data.get("toolkit"), origin,
                           toolkit_root=toolkit_root)


def runner_config_policy(schema_path: Path | None = None) -> dict:
    """The default test-runner-config protection policy (F-46 AC-4).

    THE one copy lives in the project schema (``$defs.defaultRunnerConfig
    .const`` — schema state, changeable only by a reviewed PR); this
    reads it. ``protected_globs`` are unioned into every loaded profile's
    ``paths.protected`` by :func:`apply_profile_defaults`; ``pytest_tables``
    names, per config file, the TOML tables / configparser sections L0's
    diff policy compares between the merge-base and head, structurally,
    whatever the profile says — a profile can only widen the default set,
    never remove from it.

    Args:
        schema_path: Optional override for the profile schema path.

    Returns:
        The policy mapping (a deep copy; callers may not mutate the
        schema's copy).

    Raises:
        ContractError: If the schema is missing the ``$defs`` entry —
            the runner-config default is judge state; the factory refuses
            to guess it.
    """
    path = Path(schema_path) if schema_path is not None else (
        SCHEMA_DIR / "project.schema.json"
    )
    try:
        policy = _schema_document(path)["$defs"]["defaultRunnerConfig"]["const"]
    except (KeyError, TypeError) as exc:
        raise ContractError(
            f"schema {path} is missing $defs.defaultRunnerConfig.const — "
            "the runner-config default is judge state; refusing to guess it"
        ) from exc
    return copy.deepcopy(policy)


def default_protected_control(schema_path: Path | None = None) -> list:
    """The schema-level factory-control protection set (F-63 AC-4).

    THE one copy lives in the project schema (``$defs.
    defaultProtectedControl.const``) beside the runner-config default;
    this reads it. ``.factory/**`` is the security config itself — the
    profile, the queue, the approval-adjacent state a client repo or an
    implementer rewrite could otherwise reshape — and its protection is
    structural, never each profile's own opt-in (the audit's finding:
    the config deciding whether the config is protected is
    self-referential). Unioned into ``paths.protected`` by
    :func:`apply_profile_defaults` and independently re-unioned by
    :func:`factory.l0.check_diff_policy`; a profile can only widen.

    Raises:
        ContractError: If the schema is missing the ``$defs`` entry —
            the control-protection default is judge state; the factory
            refuses to guess it.
    """
    path = Path(schema_path) if schema_path is not None else (
        SCHEMA_DIR / "project.schema.json"
    )
    try:
        globs = _schema_document(path)["$defs"]["defaultProtectedControl"]["const"]
    except (KeyError, TypeError) as exc:
        raise ContractError(
            f"schema {path} is missing $defs.defaultProtectedControl.const — "
            "the factory-control protection default is judge state; "
            "refusing to guess it"
        ) from exc
    return copy.deepcopy(globs)


# F-71 AC-3: the floor's defaults, as named constants — the owner's to
# confirm at review (the spec's 60/30 are the pilot-informed proposal:
# run 1 died twice inside 30 turns with nothing validated).
BUDGET_FLOOR_DEFAULT_TURNS = 60
BUDGET_FLOOR_DEFAULT_WALL_MIN = 30


@functools.lru_cache(maxsize=1)
def budget_caps(schema_path: Path | None = None) -> dict[str, int]:
    """The task budget maxima (F-64's caps) read from the tasks schema.

    ``{"max_turns": …, "wall_clock_min": …}`` — the ceiling a profile's
    ``budget_floor`` must stay inside. Read from the schema so the floor
    rule can never drift from the cap it bounds (a cap change moves both).
    """
    schema = _schema_document(
        schema_path if schema_path is not None else SCHEMA_DIR / "tasks.schema.json"
    )
    budget = schema.get("$defs", {}).get("task", {}).get(
        "properties", {}).get("budget", {}).get("properties", {})
    caps: dict[str, int] = {}
    for field in ("max_turns", "wall_clock_min"):
        cap = budget.get(field, {}).get("maximum")
        if not isinstance(cap, int):
            raise ContractError(
                "tasks.schema.json carries no integer maximum for "
                f"budget.{field} — the F-71 floor cannot be bounded"
            )
        caps[field] = cap
    return caps


def budget_floor(profile: dict) -> dict[str, int]:
    """The effective budget floor of a (loaded) profile: the declared
    ``budget_floor`` over the :data:`BUDGET_FLOOR_DEFAULT_*` defaults —
    a partial declaration keeps the other field's default."""
    declared = profile.get("budget_floor")
    declared = declared if isinstance(declared, dict) else {}
    return {
        "max_turns": declared.get("max_turns", BUDGET_FLOOR_DEFAULT_TURNS),
        "wall_clock_min": declared.get(
            "wall_clock_min", BUDGET_FLOOR_DEFAULT_WALL_MIN),
    }


def apply_profile_defaults(profile: dict, schema_path: Path | None = None) -> dict:
    """Union the schema-level defaults into a profile.

    The ONE route every profile-shaped mapping takes: ``load_profile``
    after validation, and the in-code synthetic profiles eval and the
    rehearsal fixture build. ``paths.protected`` gains the default
    protected globs — the runner-config set (conftest, pytest.ini,
    F-46 AC-4) and the factory-control set (``.factory/**``, F-63
    AC-4) — so a profile can only ADD to either set, never remove from
    it, and every consumer reading ``paths.protected`` downstream (L0's
    diff policy, the seats' protected-path hook, the reviewer prompt)
    sees the same widened set.

    Args:
        profile: The profile mapping (validated, when it came from disk).
        schema_path: Optional override for the profile schema path.

    Returns:
        The same mapping, with ``paths.protected`` widened.
    """
    globs = [*runner_config_policy(schema_path)["protected_globs"],
             *default_protected_control(schema_path)]
    paths = profile.setdefault("paths", {})
    protected = list(paths.get("protected") or [])
    protected += [glob for glob in globs if glob not in protected]
    paths["protected"] = protected
    return profile


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
        The parsed profile mapping, with the schema-level runner-config
        defaults unioned into ``paths.protected`` (F-46 AC-4 — a profile
        can only widen the default set, never remove from it).

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
    return apply_profile_defaults(data, schema_path)
