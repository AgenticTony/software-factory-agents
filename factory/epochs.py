"""Contract epochs — the declared history of every rule change (F-66).

Three work orders (F-64, F-65, F-59) tightened the rules a project
loads under, and only the factory's own queue and profile were swept:
onboarded repos met the change mid-pilot as bare refusals. From F-66
on, every change to the rule surfaces — both schema files and the
semantic-check functions in :mod:`factory.contract` — is DECLARED as an
epoch entry in ``factory/data/schema/epochs.json`` (schema-validated
here on load, protected in the factory's own profile), and a CI test
(:func:`fingerprint_surfaces` against the latest entry's fingerprint)
fails when a rule surface changed with no entry.

This module is deliberately dependency-free within the package (no
imports from :mod:`factory.contract`): ``contract`` imports this for
the version ceiling, so a cycle would bite at import time.

Design rules:

* The CURRENT epoch is the register's highest epoch number. It is
  stamped as the queue's top-level ``version`` on every factory write
  (:func:`factory.state.TaskQueue.save`, the importer's write path).
* An entry that TIGHTENS (refuses something that loaded before) must
  raise the epoch. An entry that tightens nothing — a refactor, an
  addition, a loosening — records that and leaves the epoch where it
  is; both kinds update the fingerprint, because both change a rule
  surface's bytes.
* Only migrations NAMED here as ``automatic`` may edit project state
  without a session, and only rewrites that cannot change a task's
  meaning. :data:`IMPLEMENTED_AUTOMATIC` is the closed set the code
  actually implements; a registered name outside it refuses the load
  (a register entry is a declaration, never an execution grant).
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_DIR = Path(__file__).resolve().parent / "data" / "schema"
EPOCHS_FILE = _SCHEMA_DIR / "epochs.json"
EPOCHS_SCHEMA = _SCHEMA_DIR / "epochs.schema.json"

# The one automatic rewrite F-66 ships: re-stamping the queue's
# top-level ``version`` to the current epoch on a write. It cannot
# change a task's meaning — ``version`` is a document-level field
# outside every task, so no contract hash covers it — and it only ever
# runs on a queue that already validates under the current rules (the
# loaders refuse anything else before a write can happen).
RESTAMP_VERSION = "restamp-version"
IMPLEMENTED_AUTOMATIC = frozenset({RESTAMP_VERSION})

# Which top-level bindings of factory/contract.py are RULE SURFACES for
# the fingerprint: every semantic check, validator, loader and
# rule-bearing constant the queue/profile rules depend on. A NEW
# top-level binding matching this pattern that the register's latest
# entry does not list fails the CI test — moving a check into a fresh
# ``_check*``/``_provider*`` helper cannot slip past the register. A
# helper named outside the pattern (e.g. ``_fmt``) is not covered:
# that reach is stated in VALIDATOR-SPEC as the fingerprint's bound.
_RULE_NAME_RE = re.compile(
    r"^(?:_check|_validate|validate_|load_|scope_|budget_|iter_schema|"
    r"_provider|_allowlist|apply_profile|NON_EXECUTABLE|POST_APPROVAL|"
    r"CONTRACT_HASH|OWNER_STEP|_SCOPE|_WILDONLY|_DISALLOWED|_ALLOWLIST|"
    r"_PROVIDER|_REGION|_BASE_URL|_TASK_FREE_TEXT)"
)


class EpochError(Exception):
    """The epoch register is missing, malformed or internally
    inconsistent. The message is written for humans."""


@dataclass(frozen=True)
class Migration:
    """One epoch's migration: what moves a stale project forward.

    kind ``none`` — the entry changed nothing a project must react to.
    kind ``owner_action`` — ``command`` is the exact command the owner
    runs (a factory command an entry spells must name a real
    subcommand, F-92 AC-4's rule).
    kind ``automatic`` — ``name`` is a rewrite the factory performs
    without a session, from :data:`IMPLEMENTED_AUTOMATIC` only.
    """

    kind: str
    command: str | None = None
    name: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Fingerprint:
    schemas: dict[str, str]
    functions: dict[str, str]


@dataclass(frozen=True)
class EpochEntry:
    epoch: int
    date: str
    wo: str
    surfaces: tuple[str, ...]
    change: str
    tightens: bool
    migration: Migration
    checks: tuple[str, ...] = ()
    fingerprint: Fingerprint | None = None


@dataclass(frozen=True)
class EpochRegister:
    entries: tuple[EpochEntry, ...] = field(default_factory=tuple)

    @property
    def current(self) -> int:
        """The register's highest epoch number."""
        return max(entry.epoch for entry in self.entries)

    def epoch_for_check(self, check: str) -> int:
        """The epoch that introduced ``check`` (1 — the baseline — when
        no entry claims it)."""
        for entry in self.entries:
            if check in entry.checks:
                return entry.epoch
        return 1

    def entry_for_epoch(self, epoch: int) -> EpochEntry:
        """The entry that RAISED ``epoch`` (its migration is the fix
        path for violations of that epoch's rules)."""
        for entry in self.entries:
            if entry.epoch == epoch and entry.tightens:
                return entry
        return self.entries[0]


def _load_json(path: Path, origin: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EpochError(f"{origin}: cannot read file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EpochError(f"{origin}: invalid JSON: {exc}") from exc


@functools.lru_cache(maxsize=1)
def load_register() -> EpochRegister:
    """Load and validate the epoch register (once per process).

    Raises:
        EpochError: The register or its schema is missing/malformed, an
            entry is structurally impossible (a tightening that does
            not raise the epoch, a non-tightening that does, an
            unimplemented automatic rewrite, a fingerprint somewhere
            other than the last entry).
    """
    schema = _load_json(EPOCHS_SCHEMA, str(EPOCHS_SCHEMA))
    doc = _load_json(EPOCHS_FILE, str(EPOCHS_FILE))
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    if (error := next(validator.iter_errors(doc), None)) is not None:
        loc = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise EpochError(
            f"{EPOCHS_FILE}: schema violation at {loc}: {error.message}"
        )
    entries: list[EpochEntry] = []
    raised = 0
    for raw in doc["entries"]:
        migration = Migration(**raw["migration"])
        if migration.kind == "automatic" and (
                migration.name not in IMPLEMENTED_AUTOMATIC):
            raise EpochError(
                f"{EPOCHS_FILE}: entry {raw['wo']} names automatic "
                f"migration {migration.name!r}, which the factory does "
                "not implement — a register entry declares what the "
                "code does, never an execution grant"
            )
        entry = EpochEntry(
            epoch=raw["epoch"], date=raw["date"], wo=raw["wo"],
            surfaces=tuple(raw["surfaces"]), change=raw["change"],
            tightens=raw["tightens"], migration=migration,
            checks=tuple(raw.get("checks") or []),
            fingerprint=(Fingerprint(raw["fingerprint"]["schemas"],
                                     raw["fingerprint"]["functions"])
                         if "fingerprint" in raw else None),
        )
        if entry.tightens and entry.epoch != raised + 1:
            raise EpochError(
                f"{EPOCHS_FILE}: entry {entry.wo} tightens the rules but "
                f"declares epoch {entry.epoch} (expected {raised + 1}) — "
                "a tightening must raise the epoch by exactly one"
            )
        if not entry.tightens and entry.epoch > raised:
            raise EpochError(
                f"{EPOCHS_FILE}: entry {entry.wo} tightens nothing but "
                f"raises the epoch to {entry.epoch} — record the change "
                "against the current epoch instead"
            )
        if entry.fingerprint is not None and raw is not doc["entries"][-1]:
            raise EpochError(
                f"{EPOCHS_FILE}: entry {entry.wo} carries a fingerprint, "
                "but only the LATEST entry may (it is the live state CI "
                "compares against)"
            )
        entries.append(entry)
        raised = max(raised, entry.epoch)
    return EpochRegister(tuple(entries))


def current_epoch() -> int:
    """The current contract epoch (the register's highest number)."""
    return load_register().current


def stamp_version(doc: dict) -> dict:
    """The ONE automatic rewrite: set the queue's top-level ``version``
    to the current epoch (F-66 AC-1 — every factory write re-stamps).

    Deliberately touches nothing else: ``version`` is outside every
    task, so no approved contract's hash covers it, and the stamp only
    ever runs on a queue that validates under the current rules.
    """
    doc["version"] = current_epoch()
    return doc


def version_ceiling_error(version: Any) -> str | None:
    """Why ``version`` cannot be a queue epoch from THIS factory, or
    None. A queue recording a NEWER epoch than the register's current
    one refuses: this factory cannot know which of its rules the queue
    was written under, and guessing would silently reinterpret tasks.
    Older epochs load (stale — reported by ``factory doctor``, healed by
    the next write's re-stamp)."""
    if isinstance(version, bool) or not isinstance(version, int):
        return None  # the schema types the field
    current = current_epoch()
    if version > current:
        return (
            f"the queue records contract epoch {version}, newer than "
            f"this factory's current epoch {current} — a factory "
            "downgrade never reinterprets a newer queue; upgrade the "
            "factory (git pull) or restore the queue this factory wrote"
        )
    return None


# ------------------------------------------------------------- fingerprint --

def _contract_module_path() -> Path:
    return Path(__file__).resolve().parent / "contract.py"


def _top_level_sources(path: Path) -> dict[str, str]:
    """name -> exact source segment for every top-level binding of a
    module, via ``ast`` (constants and functions alike)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    out: dict[str, str] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets
                         if isinstance(t, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.append(node.target.id)
        for name in names:
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                out[name] = segment
    return out


def fingerprint_surfaces() -> dict[str, dict[str, str]]:
    """The live rule surfaces: sha256 per schema file (raw bytes — the
    schemas ship verbatim, so even a whitespace change is declared) and
    sha256 per rule-bearing top-level binding of
    ``factory/contract.py`` (matched by :data:`_RULE_NAME_RE`; the
    bound is stated in VALIDATOR-SPEC)."""
    schemas: dict[str, str] = {}
    for name in ("tasks.schema.json", "project.schema.json"):
        schemas[name] = hashlib.sha256(
            (_SCHEMA_DIR / name).read_bytes()
        ).hexdigest()
    functions = {
        name: hashlib.sha256(source.encode("utf-8")).hexdigest()
        for name, source in _top_level_sources(
            _contract_module_path()
        ).items()
        if _RULE_NAME_RE.match(name)
    }
    return {"schemas": schemas, "functions": functions}
