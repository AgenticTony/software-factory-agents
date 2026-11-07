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
import tomllib
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jsonschema.exceptions import best_match

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


def apply_profile_defaults(profile: dict, schema_path: Path | None = None) -> dict:
    """Union the schema-level runner-config defaults into a profile.

    The ONE route every profile-shaped mapping takes: ``load_profile``
    after validation, and the in-code synthetic profiles eval and the
    rehearsal fixture build. ``paths.protected`` gains the default
    protected globs (conftest, pytest.ini) — a profile can only ADD to
    the default set, never remove from it (F-46 AC-4), so every consumer
    reading ``paths.protected`` downstream (L0's diff policy, the seats'
    protected-path hook, the reviewer prompt) sees the same widened set.

    Args:
        profile: The profile mapping (validated, when it came from disk).
        schema_path: Optional override for the profile schema path.

    Returns:
        The same mapping, with ``paths.protected`` widened.
    """
    globs = runner_config_policy(schema_path)["protected_globs"]
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
