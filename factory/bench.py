"""Replay benchmark of merged tasks with hidden tests (F-104).

F-100 made swapping any model a one-line registry edit, and the eval
(F-16/F-95/F-99) measures only the reviewers, on seeded defects.
Nothing measured the implementer, so "GLM 5.3 → 5.4" was a leap of
faith whose first real evidence would be a client PR. This module is
that measurement: replay a merged task's approved contract against a
clone of the repo as it was BEFORE the work, run the real production
attempt path over it, then — outside the session — copy the merged
task's own tests in and run them on whatever the replay produced.

The costlier failure is a benchmark the model can cheat by seeing the
merged answer, so leak prevention comes first and every item proves
itself before it counts:

* **AC-1** (:func:`bench_add`) — the hidden tests must FAIL on the base
  commit and PASS on the merged head three times (the flake guard),
  through the REAL L1 red-first proof; a contract that quotes the
  merged diff refuses (F-99's rule); the store under
  ``<factory_root>/bench/<project>/`` is schema-validated on load and
  lives outside every sandbox.
* **AC-2** (:func:`sanitize_origin`) — each replay runs in a clone made
  from the base commit ALONE: one branch, no remote, no tags, packs
  rebuilt, and VERIFIED (``git cat-file --batch-all-objects`` holds no
  commit from base..head) before any session starts. The hidden tests
  never enter the run dir or a prompt: they live in the item store, are
  checked against everything the parent has built BEFORE any session
  starts (:func:`_leak_check` — after the session a correct implementer
  converges toward them by design, so only the pre-session state can be
  judged), and are copied into a scoring clone only after the attempt
  settles. Task ids, branches and commit messages run on opaque
  ``BENCH-NN`` slots, as the eval's ``EVAL-NN`` do.
* **AC-3** (:func:`_replay_one`) — the replay is the REAL pipeline:
  ``factory.orchestrator._run_task`` with publishing replaced by
  scoring (the PR seam records "would publish"; the push lands on the
  sanitized local origin, never on GitHub), with the project's own
  profile and the owner's own registry config. A replay's clean merge
  needs BOTH the hidden tests passing AND the factory would have
  published — an escalation is never a clean merge, whatever the
  hidden tests say on its kept head.
* **AC-4** (:func:`bench_run`, :func:`compare_reports`) — the report
  gives per item k/N judged clean merges, the yield figure (clean
  merges per unit cost), cost per clean merge, the models that ran per
  role, and ``--against`` lists regressions (pass → fail) first. A
  ``--role`` override rides the config in memory only (F-100 AC-5's
  rules).
* **AC-5** (:func:`bench_swap_note`) — ``factory doctor`` notes a
  registry whose implementer model differs from the one the latest
  bench report ran; the README and the registry docs state the rule.

F-110 made the bench measure the implementer, not the bench, on the
first baseline's four holes: hidden tests may only use the STATED
interface (an AST pin analysis at add and :func:`bench_check`, with
``--interface FILE`` as the owner's way to state it), a failure keeps
its evidence (every ref runs, tails escaped and redacted, an evidence
folder outside the cleanup's reach), cost is the SUM over every
attempt and role, infrastructure is an ``error:`` that leaves the k/N
denominator (everything else — any unknown escalation included — is
judged and counts against the model), a retry starts clean (the
production ready-clone resets a non-completed round's uncommitted
tree), and a run survives interruption (incremental atomic reports,
``incomplete`` on SIGINT/SIGTERM, ``--items`` and ``--resume``).

Paid runs are the owner's: ``factory bench run`` drives real model
sessions (the default seat runner) and is never called by anything but
this command, exactly like ``factory eval``. Everything in the test
suite is scripted.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from factory.approvals import ApprovalLedger
from factory.config import (
    CONFIG_FILE,
    FactoryConfig,
    in_peak_window,
    load_config,
)
from factory.contract import (
    SCHEMA_DIR as _CONTRACT_SCHEMA_DIR,
)
from factory.contract import (
    ContractError,
    acceptance_hash,
    escape_recorded_text,
    iter_schema_violations,
    load_profile,
    load_tasks,
)
from factory.epochs import current_epoch
from factory.gates import (
    GateError,
    contract_test_files,
    contract_test_refs,
)
from factory.gates import (
    git as hardened_git,
)

# The l1 privates are deliberate reach-ins, the eval's precedent
# (eval.py's orchestrator imports): the add-time proof and the scoring
# run the SAME per-ref command L1 runs, never a copy that could drift.
from factory.l1 import DEFAULT_TEST_REF, _ref_command, not_collected_evidence

# The orchestrator/session names below are bound HERE so the tests (and
# only the tests) can swap them at this module's globals — the
# call-time seam lesson (F-51): a from-import at the consumer keeps the
# patch surface alive. A1: bench_run's default seat runner is the REAL
# sandboxed session runner, exactly run_once's default. A4: the
# add-time proofs and the hidden-test scoring run project commands
# through the sandboxed GATE runner — the replay head is agent code
# (gates.run_command is the fixtures-only shape its own docstring
# disclaims for production).
from factory.orchestrator import _gate_settings, sandboxed_runner
from factory.runs import (
    FACTORY_ROOT_DEFAULT,
    GIT_HARDENING,
    RunDir,
    cleanup_run,
    create_run,
    new_run_id,
    sandbox_settings_path,
)
from factory.session import probe_session_interpreter, run_session_sandboxed

__all__ = [
    "BENCH_ITEM_SCHEMA",
    "BenchError",
    "BenchItem",
    "ADD_GREEN_RUNS",
    "bench_add",
    "bench_check",
    "bench_run",
    "bench_swap_note",
    "compare_reports",
    "latest_bench_report",
    "load_items",
    "render_report",
    "sanitize_origin",
]

# The item schema ships in the package (the F-51 rule) beside the
# queue/profile schemas; the epochs fingerprint covers exactly those
# two, so adding this file needs no epochs entry.
BENCH_ITEM_SCHEMA = _CONTRACT_SCHEMA_DIR / "bench-item.schema.json"

# The bench task's budget: the schema's own ceilings minus headroom. A
# replay must not truncate an implementer the real task let finish (the
# score would measure the budget, not the model), and must stay inside
# the caps every queue validates.
BENCH_MAX_TURNS = 100
BENCH_MAX_ATTEMPTS = 2
BENCH_WALL_CLOCK_MIN = 150

# AC-1's flake guard: the hidden tests run green on the merged head
# this many times at add (L1's own green pass is the first).
ADD_GREEN_RUNS = 3

# AC-1's quote check: an added diff line at least this long (stripped)
# appearing verbatim in the contract text is a quoted answer.
QUOTE_MIN_LINE = 12

# Per-command cap for the add-time proofs and the post-replay scoring.
_PROOF_TIMEOUT_SEC = 900
# The setup window (A6): a cold venv + npm install outlasts a gate
# command; this is a floor on the owner's patience, not a ceiling on
# correctness.
_SETUP_TIMEOUT_SEC = 1800


class BenchError(RuntimeError):
    """The bench cannot run as asked (bad item, git failure, a leak)."""


# ------------------------------------------------------------- the store --


@dataclass(frozen=True)
class BenchItem:
    """One loaded, schema-validated bench item."""

    slot: str
    project: str
    source_task: str
    source_pr: int
    repo_path: str
    default_branch: str
    base: str
    head: str
    added: str
    contract: dict
    hidden_refs: tuple
    hidden_files: tuple
    interface: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "BenchItem":
        return cls(
            slot=str(raw["slot"]), project=str(raw["project"]),
            source_task=str(raw["source_task"]),
            source_pr=int(raw["source_pr"]),
            repo_path=str(raw["repo_path"]),
            default_branch=str(raw["default_branch"]),
            base=str(raw["base"]), head=str(raw["head"]),
            added=str(raw["added"]), contract=dict(raw["contract"]),
            hidden_refs=tuple(raw["hidden_refs"]),
            hidden_files=tuple(raw["hidden_files"]),
            interface=str(raw.get("interface") or ""),
        )

    def item_dir(self, factory_root: Path) -> Path:
        return bench_dir(factory_root) / self.project / self.slot

    def hidden_dir(self, factory_root: Path) -> Path:
        return self.item_dir(factory_root) / "hidden"

    def hidden_files_text(self, factory_root: Path) -> dict:
        """The hidden tests' bytes from the store: path → text."""
        out: dict[str, str] = {}
        root = self.hidden_dir(factory_root)
        for rel in self.hidden_files:
            path = root / rel
            if not path.is_file():
                raise BenchError(
                    f"{self.slot}: the store is missing the hidden file "
                    f"{rel} ({path}) — re-add the item"
                )
            out[rel] = path.read_text(encoding="utf-8")
        return out


def bench_dir(factory_root: Path | str | None) -> Path:
    """The bench root: ``<factory_root>/bench/``."""
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    return root / "bench"


def _validate_item_doc(doc, origin: str) -> None:
    violations = list(iter_schema_violations(doc, BENCH_ITEM_SCHEMA))
    if violations:
        raise BenchError(
            f"{origin}: bench item schema violation at {violations[0]} "
            f"({len(violations)} total) — the store refuses to load; "
            "re-add the item or restore it from a backup"
        )


def load_items(factory_root: Path | str | None, project: str) -> list:
    """Every item of one project, schema-validated on load (the F-02
    rule), sorted by slot. Refuses the whole store on any violation —
    a benchmark over an unvalidated item measures nothing."""
    root = bench_dir(factory_root) / project
    if not root.is_dir():
        raise BenchError(
            f"no bench items for project {project!r} under {root} — add "
            "one first (`factory bench add <project> <task-id | pr-number>`)"
        )
    items: list[BenchItem] = []
    seen_heads: set[str] = set()
    for item_dir in sorted(root.iterdir()):
        doc_path = item_dir / "item.json"
        if not doc_path.is_file():
            continue
        try:
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BenchError(f"{doc_path}: cannot read the item: {exc}") \
                from exc
        _validate_item_doc(doc, str(doc_path))
        if doc["project"] != project:
            raise BenchError(
                f"{doc_path}: names project {doc['project']!r} but lives "
                f"under {project!r}"
            )
        if doc["head"] in seen_heads:
            raise BenchError(
                f"{doc_path}: two items share the merged head "
                f"{doc['head'][:12]} — the store refuses to load"
            )
        seen_heads.add(doc["head"])
        _check_hidden_paths(str(doc_path), doc["hidden_files"])
        items.append(BenchItem.from_dict(doc))
    if not items:
        raise BenchError(
            f"no bench items for project {project!r} under {root} — add "
            "one first (`factory bench add <project> <task-id | pr-number>`)"
        )
    return items


def _resolve_project_repo(factory_root: Path | str | None,
                          project: str) -> Path:
    """The project's repo, from the approval ledger the way doctor reads
    it (F-66): the latest profile approval's recorded repo path."""
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    ledger = ApprovalLedger.for_project(root, project)
    approval = ledger.latest_profile_approval()
    repo_path = (approval or {}).get("repo_path")
    if not isinstance(repo_path, str) or not repo_path:
        raise BenchError(
            f"project {project!r} has no profile approval recording its "
            f"repo path under {root}/approvals/ — run `factory approve "
            "--profile` from the project's repo first"
        )
    repo = Path(repo_path).expanduser()
    if not (repo / ".git").exists():
        raise BenchError(
            f"project {project!r} records the repo path {repo_path}, which "
            "is not a git repository (moved? cloned elsewhere?) — "
            "re-approve the profile from its new location"
        )
    return repo


def _git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise BenchError(
            f"git {' '.join(args[:2])} failed in {repo}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout.strip()


def _git_raw(repo: Path, *args: str) -> str:
    """Like :func:`_git_out` but WITHOUT the strip: a blob's exact bytes
    (the hidden tests are stored verbatim)."""
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise BenchError(
            f"git {' '.join(args[:2])} failed in {repo}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True, text=True,
    ).returncode == 0


# --------------------------------------------------------------- AC-1 add --


def _added_lines(repo: Path, base: str, head: str) -> list:
    """The diff's added CODE lines (``base...head``), stripped.

    F-99's rule guards against a contract that hands over the answer,
    and the answer is code. Prose is not: an implementer routinely
    copies the contract's own wording into comments and docstrings, and
    a contract may legitimately name the exact user-facing text it
    requires (string literals). So only code lines count: docs files
    are skipped, Python lines are classified by the tokenizer (a line
    made only of strings, comments and joining punctuation is prose),
    other files by stripping string literals and comments. A Python
    file the tokenizer cannot read counts every line as code — the
    check fails toward refusing.
    """
    diff = _git_out(repo, "diff", "--no-color", "--no-ext-diff", "-U0",
                    f"{base}...{head}")
    per_file: dict[str, list] = {}
    path: str | None = None
    new_line = 0
    # File headers come only between `diff --git` and the first hunk: an
    # ADDED line whose text starts "++ " reads "+++ …" in the diff and
    # must never be taken for a header (that would skip the file's rest).
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header, path = True, None
            continue
        if in_header:
            if line.startswith("+++ "):
                target = line[4:].strip().strip('"')
                path = target[2:] if target.startswith("b/") else None
            elif line.startswith("@@"):
                in_header = False
            if in_header:
                continue
        if line.startswith("@@"):
            match = re.match(r"@@ -\S+ \+(\d+)", line)
            new_line = int(match.group(1)) if match else 0
            continue
        if line.startswith("+") and path is not None:
            per_file.setdefault(path, []).append((new_line, line[1:]))
            new_line += 1
    lines: list[str] = []
    for file_path, added in per_file.items():
        suffix = Path(file_path).suffix.lower()
        if suffix in _PROSE_SUFFIXES:
            continue
        prose: set = set()
        if suffix == ".py":
            source = _git_out(repo, "show", f"{head}:{file_path}")
            prose = _python_prose_lines(source)
        for number, text in added:
            stripped = text.strip()
            if len(stripped) < QUOTE_MIN_LINE:
                continue
            if suffix == ".py":
                if number in prose:
                    continue
            elif _generic_prose(stripped):
                continue
            lines.append(stripped)
    return lines


# Files whose added lines are prose by kind (the quote check skips them).
_PROSE_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})
# String literals (double, single, backtick), escapes honoured.
_STRING_LITERAL_RE = re.compile(
    r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`')
# Punctuation that joins string pieces without making a line code.
_PROSE_JOINERS = frozenset({",", "+", "(", ")"})


def _python_prose_lines(source: str) -> set:
    """Line numbers of a Python source that hold only prose: strings,
    comments and joining punctuation (``"text",`` in a call is prose;
    ``x = "text"`` is code). An unreadable source yields no prose."""
    import io
    import tokenize

    prose_types = {tokenize.STRING, tokenize.COMMENT, tokenize.NL,
                   tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                   tokenize.ENDMARKER}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        if hasattr(tokenize, name):
            prose_types.add(getattr(tokenize, name))
    code_lines: set = set()
    seen: set = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            rows = range(tok.start[0], tok.end[0] + 1)
            seen.update(rows)
            if tok.type in prose_types:
                continue
            if tok.type == tokenize.OP and tok.string in _PROSE_JOINERS:
                continue
            code_lines.update(rows)
    except (tokenize.TokenError, SyntaxError):
        return set()
    return seen - code_lines


def _generic_prose(line: str) -> bool:
    """A non-Python added line is prose when, once its string literals
    and comments are removed, nothing word-like is left."""
    rest = _STRING_LITERAL_RE.sub("", line)
    rest = re.sub(r"//.*$", "", rest).strip()
    if rest.startswith(("/*", "*", "#", "<!--", "-->")):
        return True
    return re.search(r"[A-Za-z0-9_]", rest) is None


def _contract_quote(contract_text: str, added: list) -> str | None:
    """F-99's rule at add: the contract states the task, never the
    answer. A stripped added line of the merged diff appearing verbatim
    in the contract text is a quoted answer — name it."""
    for line in added:
        if line in contract_text:
            return line
    return None


# ---------------------------------------- F-110 AC-1: the stated interface --

# The hidden tests are the original session's own red-first tests, so
# they reach the API the ORIGINAL implementation happened to build:
# BENCH-03's test calls verify_text_layer(..., applicant_name=...) — a
# keyword J-5's implementation added and the contract never names. A
# replay that builds the right behaviour under another name fails on
# the naming, not the work, and the bench scores the model for it. The
# pin analysis below refuses such items at add (and at check) until
# the owner states the interface: every project name the hidden tests
# reach must exist at the BASE, under the same path with the same
# keyword on the same callable, or be named in the contract or the
# item's interface text.


@dataclass(frozen=True)
class _Pin:
    """One project name a hidden test reaches that the base does not
    already expose and the allowance text does not name."""

    display: str    # what the refusal prints
    path: str       # the full dotted path (module.name)
    keyword: "str | None"  # set for callable(keyword=) pins
    names: tuple    # every identifier the allowance text may name


class _ModuleResolver:
    """Resolve dotted import names against the repo's BASE and HEAD
    trees.

    Roots are every ancestor of the importing file's directory up to
    the repo root — the layout real projects use (a ``backend/app``
    package tree imported by tests sitting beside it under
    ``backend/tests``). A dotted name is a PROJECT module when it
    resolves to a file in either tree; stdlib and third-party imports
    resolve in neither and can never be pins. A module that resolves
    only at HEAD is a head-only module: everything reached through it
    is a pin until the interface states it.
    """

    def __init__(self, repo: Path, base: str, head: str, test_path: str):
        self.base_files = _tree_files(repo, base)
        self.head_files = _tree_files(repo, head)
        parts = Path(test_path).parent.parts
        self.roots = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]

    def _candidates(self, dotted: str):
        flat = dotted.replace(".", "/")
        for root in self.roots:
            base = f"{root}/{flat}" if root else flat
            yield base + ".py"
            yield base + "/__init__.py"

    def resolve(self, dotted: str) -> "tuple[str, str] | None":
        """``(repo_relative_path, where)`` with where in
        ``{"base", "head"}``, or None when the name is no project
        module (base first: a module that exists at both is read at
        base — that is the surface the replay starts from)."""
        for cand in self._candidates(dotted):
            if cand in self.base_files:
                return cand, "base"
            if cand in self.head_files:
                return cand, "head"
        return None

    def at_base(self, dotted: str) -> bool:
        return any(cand in self.base_files
                   for cand in self._candidates(dotted))


def _tree_files(repo: Path, commit: str) -> frozenset:
    """Every file path in one commit's tree."""
    out = _git_out(repo, "ls-tree", "-r", "--name-only", commit)
    return frozenset(line for line in out.splitlines() if line)


def _module_source(repo: Path, commit: str, path: str) -> "str | None":
    """A tree file's bytes, or None when it cannot be read."""
    try:
        return _git_raw(repo, "show", f"{commit}:{path}")
    except BenchError:
        return None


def _module_surface(source: "str | None", module_path: str = "") \
        -> "dict | None":
    """A module's importable surface, for pin checks: top-level names
    (functions, classes, assignments, imports — conditional and try
    blocks included, coarsely) and, per callable, the keywords its
    signature accepts. Class FIELD declarations (``AnnAssign`` and
    plain assigns in the class body — the dataclass / pydantic /
    SQLModel shape) count as both members and accepted keywords, on the
    class itself and on same-module bases. Import aliases are recorded
    as ``alias`` entries for the caller to follow one level (the
    package re-export shape). None when the source does not parse: the
    pin check then fails closed (everything is a pin)."""
    if source is None:
        return None
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    surface: dict = {}

    def _signature(args) -> "tuple[set, bool]":
        names = {a.arg for a in getattr(args, "args", []) or []}
        names |= {a.arg for a in getattr(args, "kwonlyargs", []) or []}
        var_kw = getattr(args, "kwarg", None) is not None
        return names, var_kw

    def _walk(node, top: bool) -> None:
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kws, var_kw = _signature(child.args)
                surface.setdefault(child.name, {"kind": "func",
                                                "keywords": kws,
                                                "var_kw": var_kw})
            elif isinstance(child, ast.ClassDef):
                entry = surface.setdefault(child.name,
                                           {"kind": "class",
                                            "keywords": set(),
                                            "var_kw": False,
                                            "members": set(),
                                            "fields": set(),
                                            "bases": [
                                                b.id for b in child.bases
                                                if isinstance(b, ast.Name)],
                                            "methods": {}})
                for sub in child.body:
                    if isinstance(sub, (ast.FunctionDef,
                                        ast.AsyncFunctionDef)):
                        entry["methods"][sub.name] = _signature(sub.args)
                        entry["members"].add(sub.name)
                    elif isinstance(sub, ast.AnnAssign):
                        if isinstance(sub.target, ast.Name):
                            entry["members"].add(sub.target.id)
                            entry["fields"].add(sub.target.id)
                    elif isinstance(sub, ast.Assign):
                        for target in sub.targets:
                            if isinstance(target, ast.Name):
                                entry["members"].add(target.id)
                                entry["fields"].add(target.id)
            elif isinstance(child, ast.Assign) and top:
                for target in child.targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            surface.setdefault(name.id,
                                               {"kind": "value"})
            elif isinstance(child, ast.AnnAssign) and top:
                if isinstance(child.target, ast.Name):
                    surface.setdefault(child.target.id, {"kind": "value"})
            elif isinstance(child, ast.ImportFrom) and top:
                for alias in child.names:
                    surface.setdefault(
                        alias.asname or alias.name,
                        {"kind": "alias", "from": child.module or "",
                         "level": child.level or 0,
                         "name": alias.name})
            elif isinstance(child, ast.Import) and top:
                for alias in child.names:
                    surface.setdefault(
                        alias.asname or alias.name.split(".")[0],
                        {"kind": "alias", "from": alias.name,
                         "level": 0, "name": alias.name,
                         "absolute": True})
            elif isinstance(child, (ast.If, ast.Try, ast.With)) and top:
                _walk(child, top=False)

    _walk(tree, top=True)
    return surface


def _class_keywords(surface: "dict | None", entry: dict,
                    seen: "set | None" = None) -> set:
    """The keywords a class accepts by DECLARED surface: its own field
    declarations and __init__ signature, plus the same-module bases'
    (followed to the base of the module — an unresolvable base
    contributes nothing, which is the fail-closed half: a pydantic or
    SQLModel base's dynamic acceptance is not statically known)."""
    keywords = set(entry.get("fields") or set())
    init = (entry.get("methods") or {}).get("__init__")
    if init is not None:
        keywords |= init[0]
    seen = seen or set()
    if entry.get("var_kw") or (init and init[1]):
        return keywords  # **kwargs in the class's own signature
    for base_name in entry.get("bases") or []:
        if base_name in seen or surface is None:
            continue
        base = surface.get(base_name)
        if base and base.get("kind") == "class":
            seen.add(base_name)
            base_init = (base.get("methods") or {}).get("__init__")
            if base_init and base_init[1]:
                return keywords  # a **kwargs base accepts anything
            keywords |= _class_keywords(surface, base, seen)
    return keywords


def _surface_accepts(surface: "dict | None", chain: list,
                     keyword: str) -> bool:
    """Whether a base module's surface exposes ``chain`` (a callable
    path like ``["verify_text_layer"]`` or ``["Client", "render"]``)
    accepting ``keyword``. Unparseable surface (None) or a missing
    name fails closed: the keyword is a pin. A class without an
    explicit ``__init__`` (the dataclass / pydantic / SQLModel shape)
    accepts a keyword only when it is a DECLARED field of the class or
    of a base resolvable in the same module — a decorator-built
    constructor's dynamic acceptance is not statically known, so an
    undeclared keyword is a pin, never a free pass."""
    if surface is None:
        return False
    entry = surface.get(chain[0])
    if entry is None:
        return False
    if len(chain) == 1:
        if entry["kind"] == "class":
            init = entry["methods"].get("__init__")
            if init is not None:
                return keyword in init[0] or init[1]
            if "class_keywords" in entry:  # a followed re-export
                return keyword in entry["class_keywords"]
            return keyword in _class_keywords(surface, entry)
        if entry["kind"] == "func":
            return keyword in entry["keywords"] or entry["var_kw"]
        return False  # a plain value is not callable at base
    if entry["kind"] != "class":
        return False
    method = entry["methods"].get(chain[1])
    if method is None:
        return False
    return keyword in method[0] or method[1]


def _follow_alias(repo: Path, base: str, resolver: _ModuleResolver,
                  current_path: str, entry: dict) -> "dict | None":
    """B1: follow ONE import-alias entry to the class or function it
    re-exports (the package ``__init__`` shape: ``from .draft import
    ApplicationDraft``), so a base that genuinely accepts a keyword is
    not pinned. The target module's own surface is parsed once and its
    entry substituted with its class keywords precomputed (the target's
    same-module bases count); an alias-of-alias or a deeper dynamic
    re-export stays unresolvable — fail-closed, a pin until the
    interface states it."""
    if entry.get("absolute"):
        target_mod = entry["from"]
    else:
        level = entry.get("level") or 0
        parts = Path(current_path).parent.parts
        root = ("/".join(parts[:len(parts) - (level - 1)])
                if level > 1 else "/".join(parts))
        prefix = (root + "/") if level else ""
        target_mod = (f"{prefix}{entry.get('from') or ''}").rstrip("/")
    if not target_mod:
        return None
    found = resolver.resolve(target_mod)
    if not found or found[1] != "base":
        return None
    source = _module_source(repo, base, found[0])
    target_surface = _module_surface(source)
    if target_surface is None:
        return None
    target_entry = target_surface.get(entry["name"])
    if target_entry is None or target_entry["kind"] not in ("class",
                                                            "func"):
        return None
    resolved = dict(target_entry)
    if resolved["kind"] == "class":
        resolved["class_keywords"] = _class_keywords(
            target_surface, target_entry)
    return resolved


def _surface_has(surface: "dict | None", chain: list) -> bool:
    """Whether a base module's surface defines the name chain at all
    (the import/attribute pin check; unparseable fails closed)."""
    if surface is None:
        return False
    entry = surface.get(chain[0])
    if entry is None or len(chain) == 1:
        return entry is not None
    return entry["kind"] == "class" and chain[1] in entry["members"]


class _TestBindings:
    """One hidden test file's import bindings, defined names and
    star-imports, off its AST. ``defined`` is deliberately coarse
    (every stored name, def, class and argument anywhere in the file):
    a name the file defines is its own, never a pin — the cost of a
    rare false allowance is an unfair replay, never a flattering one."""

    def __init__(self, tree, resolver: _ModuleResolver, test_path: str):
        import ast

        self.modules: dict = {}   # local name → dotted project module
        self.members: dict = {}   # local name → (module, imported name)
        self.stars: list = []     # star-imported project modules
        self.defined: set = set()
        parts = Path(test_path).parent.parts
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    self.defined.add(local)
                    dotted = (alias.name if alias.asname
                              else alias.name.split(".")[0])
                    # Only a PROJECT module binds as one: stdlib and
                    # third-party imports can never be pin sources.
                    if resolver.resolve(dotted):
                        self.modules[local] = dotted
            elif isinstance(node, ast.ImportFrom):
                level = node.level or 0
                if level:
                    # A relative import's base is a PATH (the test's own
                    # package), joined with "/" — the resolver takes
                    # both forms (it only swaps "." for "/").
                    root = ("/".join(parts[:len(parts) - (level - 1)])
                            if level > 1 else "/".join(parts))
                    base_mod = (f"{root}/{node.module}" if node.module
                                else root).rstrip("/")
                else:
                    base_mod = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        if base_mod and resolver.resolve(base_mod):
                            self.stars.append(base_mod)
                        continue
                    local = alias.asname or alias.name
                    self.defined.add(local)
                    sub = (f"{base_mod}/{alias.name}" if level
                           else (f"{base_mod}.{alias.name}" if base_mod
                                 else alias.name))
                    if resolver.resolve(sub):
                        self.modules[local] = sub
                    elif base_mod and resolver.resolve(base_mod):
                        self.members[local] = (base_mod, alias.name)
                    # neither: a third-party or stdlib binding — never
                    # a pin source
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                self.defined.add(node.name)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    self.defined.add(node.id)
            elif isinstance(node, ast.arg):
                self.defined.add(node.arg)


def _chain_parts(node) -> "tuple[str | None, list[str]]":
    """``(root_name, attributes)`` for a Name-rooted attribute chain
    (``ps.f.g`` → ``("ps", ["f", "g"])``); the root is None for
    anything else (a call, a subscript, a lambda…)."""
    import ast

    attrs: list = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return node.id, list(reversed(attrs))
    return None, attrs


def _hidden_pins(repo: Path, base: str, head: str, hidden: dict,
                 contract_text: str, interface_text: str = "") -> list:
    """Every pin the hidden tests carry (F-110 AC-1): project names
    they reach that the base does not already expose under the same
    path (with the same keyword on the same callable) and neither the
    contract nor the interface text states.

    The analysis is an AST walk, never a regex: imported modules and
    names, attributes read from project modules, and keyword arguments
    at calls to project callables. Names the test file defines itself
    are not pins; stdlib and third-party imports are not project
    names; a star-import from a project module makes every otherwise
    unbound used name unresolvable — caught as a pin, never passed.

    Stating a pin (word-bounded, never a bare substring — ``app``
    inside ``applicant`` is not the ``app`` package): a module or
    attribute pin is stated by either text; a KEYWORD pin is stated
    only by the interface text, or by the contract as ``keyword=``
    or on a line that also names the callable — prose using the word
    does not state an API keyword (B2: "the company name is shown"
    does not state ``verify(company=)``).
    """
    import ast
    import builtins

    pins: dict = {}
    allowance = contract_text + "\n" + interface_text

    def _allowed(pin: _Pin) -> bool:
        if pin.keyword:
            if interface_text and (
                    re.search(rf"\b{re.escape(pin.keyword)}\b",
                              interface_text)
                    or pin.display in interface_text
                    or pin.path in interface_text):
                return True
            callable_name = pin.display.partition("(")[0]
            for line in contract_text.splitlines():
                if not re.search(rf"\b{re.escape(pin.keyword)}\b", line):
                    continue
                if f"{pin.keyword}=" in line or (
                        callable_name and
                        re.search(rf"\b{re.escape(callable_name)}\b",
                                  line)):
                    return True
            return False
        for name in (pin.display, pin.path, *pin.names):
            if name and re.search(rf"\b{re.escape(name)}\b", allowance):
                return True
        return False

    def _add(display: str, path: str, keyword: "str | None",
             *names: str) -> None:
        pin = _Pin(display=display, path=path, keyword=keyword,
                   names=tuple(n for n in names if n))
        if not _allowed(pin):
            pins.setdefault(display, pin)

    for test_path, text in hidden.items():
        resolver = _ModuleResolver(repo, base, head, test_path)
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise BenchError(
                f"the hidden test {test_path} does not parse ({exc}) — "
                "its pins cannot be computed and the bench refuses to "
                "guess") from exc
        binds = _TestBindings(tree, resolver, test_path)
        base_surface: dict = {}

        def _surface_for(dotted: str) -> "dict | None":
            """The BASE's surface of a project module (None for a
            head-only or unparseable module — fail closed), with
            one-level re-export aliases resolved to the class or
            function they name."""
            if dotted not in base_surface:
                found = resolver.resolve(dotted)
                source = (_module_source(repo, base, found[0])
                          if found and found[1] == "base" else None)
                surface = _module_surface(source)
                if surface is not None and found is not None:
                    for name, entry in list(surface.items()):
                        if entry.get("kind") == "alias":
                            resolved = _follow_alias(
                                repo, base, resolver, found[0], entry)
                            if resolved is not None:
                                surface[name] = resolved
                base_surface[dotted] = surface
            return base_surface[dotted]

        def _project_chain(node) -> "tuple[str, list[str]] | None":
            """(dotted module path, attribute chain) when the node's
            root binding is a project module or member; else None.
            Leading attributes that are SUBMODULES advance the module
            (``import app.services as s; s.pdf_service.f``), a name the
            module's own surface holds stops the walk (a re-export or
            a member)."""
            root, attrs = _chain_parts(node)
            if root is None:
                return None
            if root in binds.members:
                mod, name = binds.members[root]
                return mod, [name, *attrs]
            if root not in binds.modules:
                return None
            mod = binds.modules[root]
            while attrs:
                surface = _surface_for(mod)
                if surface is not None and attrs[0] in surface:
                    break
                if resolver.resolve(f"{mod}.{attrs[0]}"):
                    mod = f"{mod}.{attrs[0]}"
                    attrs = attrs[1:]
                    continue
                break
            return mod, attrs

        # The imports themselves: a head-only project module is a pin
        # however little the test uses it (the replay head must carry
        # the module for the test to even import), and a from-import
        # member the base's module lacks is one too.
        for dotted in sorted(set(binds.modules.values())):
            if not resolver.at_base(dotted):
                _add(dotted, dotted, None, *dotted.split("."))
        for local in sorted(binds.members):
            mod, name = binds.members[local]
            if not _surface_has(_surface_for(mod), [name]):
                _add(f"{mod}.{name}", f"{mod}.{name}", None, name)

        star_surfaces = [(_mod, _surface_for(_mod)) for _mod in binds.stars]

        # The BENCH-03 shape: a locally-defined wrapper collects
        # ``**kwargs`` and unpacks them into a project call, so the
        # keyword never appears as a named keyword at the project call
        # site. One level of flow: every keyword a wrapper's CALLERS
        # pass (beyond its explicit params) that the base target does
        # not accept is a pin on the target, named like any other.
        local_defs = {
            node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fname, fdef in local_defs.items():
            var_kw = fdef.args.kwarg
            if var_kw is None:
                continue
            targets: list = []
            for node in ast.walk(fdef):
                if not isinstance(node, ast.Call):
                    continue
                if not any(kw.arg is None
                           and isinstance(kw.value, ast.Name)
                           and kw.value.id == var_kw.arg
                           for kw in node.keywords):
                    continue
                chain = _project_chain(node.func)
                if chain is not None and chain[1]:
                    targets.append(chain)
            if not targets:
                continue
            explicit = {a.arg for a in fdef.args.args} \
                | {a.arg for a in fdef.args.kwonlyargs} \
                | {a.arg for a in fdef.args.posonlyargs}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                root, attrs = _chain_parts(node.func)
                if root != fname or attrs:
                    continue
                for kw in node.keywords:
                    if kw.arg is None or kw.arg in explicit:
                        continue
                    if any(_surface_accepts(_surface_for(mod), tattrs,
                                            kw.arg)
                           for mod, tattrs in targets):
                        continue
                    mod, tattrs = targets[0]
                    _add(f"{tattrs[-1]}({kw.arg}=)",
                         f"{mod}.{'.'.join(tattrs)}", kw.arg)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                chain = _project_chain(node.func)
                if chain is None:
                    continue
                mod, attrs = chain
                if not attrs:
                    continue  # calling a module itself is not an API use
                for kw in node.keywords:
                    if kw.arg is None:
                        continue  # ** unpacking, not a named keyword
                    if not _surface_accepts(_surface_for(mod), attrs,
                                            kw.arg):
                        # names: the KEYWORD alone — a contract naming
                        # the callable does not state a keyword the
                        # base never accepted.
                        _add(f"{attrs[-1]}({kw.arg}=)",
                             f"{mod}.{'.'.join(attrs)}", kw.arg)
            elif isinstance(node, ast.Attribute):
                chain = _project_chain(node)
                if chain is None:
                    continue
                mod, attrs = chain
                if not attrs:
                    continue
                # Module dunders (``__file__``, ``__name__``…) are
                # Python plumbing, not project API — reading one pins
                # nothing.
                if attrs[-1].startswith("__") and attrs[-1].endswith("__"):
                    continue
                if _surface_has(_surface_for(mod), attrs):
                    continue
                _add(f"{mod}.{'.'.join(attrs)}",
                     f"{mod}.{'.'.join(attrs)}", None, *attrs)
            elif isinstance(node, ast.Name) \
                    and isinstance(node.ctx, ast.Load) \
                    and binds.stars \
                    and node.id not in binds.defined \
                    and node.id not in binds.modules \
                    and node.id not in binds.members \
                    and node.id not in dir(builtins):
                if any(_surface_has(surface, [node.id])
                       for _, surface in star_surfaces
                       if surface is not None):
                    continue  # the base module already exports it
                _add(f"from {binds.stars[0]} import * → {node.id} "
                     "(unresolvable)",
                     f"{binds.stars[0]}.{node.id}", None, node.id)
    return [pins[key] for key in sorted(pins)]


def _pins_refusal(tid: str, pins: list) -> str:
    """The named refusal for a pin-carrying item: each pin as
    ``module.name`` or ``callable(keyword=)``."""
    listed = "; ".join(
        f"{p.display}" + (f" [{p.path.rpartition('.')[0]}]"
                          if p.keyword else "") for p in pins[:8])
    return (
        f"{tid}: the hidden tests pin API names the contract does not "
        f"state — {listed}{'; …' if len(pins) > 8 else ''}. A replay "
        "would score the ORIGINAL implementation's naming, not the "
        "model's work: re-add with `bench add --interface FILE` naming "
        "the interface (its text becomes part of the replayed contract) "
        "or restate the names in the contract itself"
    )


def _interface_quote_refusal(tid: str, interface: str, added: list,
                             pins: list) -> "str | None":
    """F-99's rule on the interface text: naming the pinned API is its
    purpose, so the pin names themselves are masked out before the
    quote check — everything left is ordinary contract text and may
    not quote the merged diff's added code lines."""
    masked = interface
    for pin in pins:
        for name in (pin.display, pin.path, *pin.names):
            if name:
                masked = re.sub(rf"\b{re.escape(name)}\b", "", masked)
    quoted = _contract_quote(masked, added)
    if quoted is None:
        return None
    return (
        f"{tid}: the interface text quotes the merged diff (F-99's "
        f"rule: it may name the pinned API, never the answer) — the "
        f"added line {quoted[:80]!r} appears verbatim in it beyond the "
        "pinned names; rewrite it as the interface's intent"
    )


def bench_add(factory_root: Path | str | None, project: str, source: str,
              *, command_runner=None, interface_text: "str | None" = None,
              ) -> dict:
    """Add one benchmark item from a merged task (AC-1).

    ``source`` is the task id or the merged PR number. The item is the
    task's approved contract plus its hidden tests (the files its
    ``test`` acceptance refs name), and it proves itself before it
    counts: through the REAL L1 red-first proof in a disposable clone —
    the hidden tests must fail on the base commit and pass on the
    merged head, ``ADD_GREEN_RUNS`` times (L1's green pass plus the
    extra runs below). A contract that quotes the merged diff refuses
    (checked first — the cheapest and the most decisive). F-110: the
    hidden tests may only use the STATED interface — every project
    name they reach that the base does not already expose (same path,
    same keyword on the same callable) and neither the contract nor
    ``interface_text`` names is a pin, refused named; the base's own
    setup must build (the replay readies its clone at the base — a
    base that cannot install is an infrastructure error on every
    replay, never the model's). The project repo is only read (the
    proofs run in a clone under the bench root).

    Returns:
        ``{"slot", "item", "item_dir", "hidden_dir"}``.

    Raises:
        BenchError: every refusal, named (unmerged task, missing links,
            a base that cannot be derived, hidden tests that pass on
            base or fail on head, a quoted contract, a duplicated head,
            a pin the stated interface does not cover, a base whose
            setup fails).
    """
    runner = command_runner
    repo = _resolve_project_repo(factory_root, project)
    queue_path = repo / ".factory" / "tasks.json"
    try:
        queue = load_tasks(queue_path)
    except ContractError as exc:
        raise BenchError(f"the project queue refused to load: {exc}") \
            from exc
    task = _find_task(queue, source)
    tid = task.get("id") or source

    links = task.get("links") or {}
    pr_url = links.get("pr")
    if not pr_url or links.get("merged") != pr_url:
        raise BenchError(
            f"{tid}: not a merged task — the bench replays work that "
            "reached the default branch (links.merged must equal links.pr; "
            "an open or unmerged PR has no proven hidden-test pass)"
        )
    head = links.get("pushed_head")
    # The branch NAME in the links is informational only: GitHub
    # deletes merged branches, so no local ref may reach the head (A2).
    # The pushed-head COMMIT existing is the real precondition; the
    # proof clone below fetches it explicitly, reachable or not.
    if not head or not _git_ok(repo, "cat-file", "-e", f"{head}^{{commit}}"):
        raise BenchError(
            f"{tid}: the recorded pushed head {head!r} is not a commit in "
            "the repo — the item cannot be proven"
        )
    # One item per merged head: a duplicated item double-weights the
    # same work in every aggregate. The existing store still validates.
    store = bench_dir(factory_root) / project
    if store.is_dir() and any(
            (entry / "item.json").is_file() for entry in store.iterdir()):
        for other in load_items(factory_root, project):
            if other.head == head:
                raise BenchError(
                    f"{tid}: there is already an item for this merged head "
                    f"({other.slot}, from {other.source_task}) — one item "
                    "per merged task"
                )
    profile = load_profile(repo / ".factory" / "project.yaml")
    default_branch = (profile.get("repo") or {}).get(
        "default_branch") or "main"
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{default_branch}"):
        raise BenchError(
            f"{tid}: the repo has no branch {default_branch!r} to derive "
            "the base from"
        )
    _refuse_stale_default(repo, default_branch, tid)
    base = _derive_base(repo, head, default_branch)
    if base == head:
        raise BenchError(
            f"{tid}: the default branch descends from the pushed head in a "
            "way no rule below derives a base from — refusing rather than "
            "guessing the fork point"
        )
    if base == _git_out(repo, "rev-parse", f"refs/heads/{default_branch}"):
        raise BenchError(
            f"{tid}: no work between the base and the merged head — the "
            "item would replay an empty task"
        )

    refs = contract_test_refs(task)
    if not refs:
        raise BenchError(
            f"{tid}: the contract names no test-checked acceptance — "
            "there are no hidden tests to score a replay with"
        )
    files = sorted(contract_test_files(task))
    _check_hidden_paths(tid, files)
    for path in files:
        if not _git_ok(repo, "cat-file", "-e", f"{head}:{path}"):
            raise BenchError(
                f"{tid}: the hidden test file {path} is not on the merged "
                f"head {head[:12]} — the acceptance ref names a file the "
                "merged work does not carry"
            )
    source_path = str(task.get("source") or "").partition("#")[0]
    if not source_path or not _git_ok(repo, "cat-file", "-e",
                                      f"{base}:{source_path}"):
        raise BenchError(
            f"{tid}: the task's spec doc ({task.get('source')!r}) is not "
            "on the base commit — the merged diff carried its own spec, so "
            "a replay could not hand the implementer what the real run "
            "handed it"
        )

    scope = task.get("scope")
    scope_paths = (scope.get("paths")
                   if isinstance(scope, dict) else None) or []
    contract = {
        "title": str(task.get("title") or ""),
        "source": str(task.get("source") or ""),
        "acceptance": copy.deepcopy(task.get("acceptance") or []),
        "scope": list(scope_paths),
        "risk": list(task.get("risk") or []),
        "out_of_scope": list(task.get("out_of_scope") or []),
        "dependencies": copy.deepcopy(task.get("dependencies") or []),
    }
    contract_text = "\n".join(
        [contract["title"]]
        + [str(item.get("text") or "")
           for item in contract["acceptance"]
           if isinstance(item, dict)]
        + contract["out_of_scope"]
    )
    quoted = _contract_quote(
        contract_text, _added_lines(repo, base, head))
    if quoted is not None:
        raise BenchError(
            f"{tid}: the contract quotes the merged diff (F-99's rule: it "
            f"must state the task, never the answer) — the added line "
            f"{quoted[:80]!r} appears verbatim in the contract text; "
            "rewrite the criterion as intent and re-run bench add"
        )

    # F-110 AC-1: the hidden tests may only use the stated interface.
    # The hidden texts come off the head blobs (the store's own bytes);
    # the allowance is the contract plus the owner-written interface
    # text. Cheap and decisive — it refuses before any setup runs.
    hidden = {
        path: _git_raw(repo, "show", f"{head}:{path}")
        for path in files
    }
    pins = _hidden_pins(repo, base, head, hidden, contract_text,
                        interface_text or "")
    if pins:
        raise BenchError(_pins_refusal(tid, pins))
    if interface_text is not None:
        refusal = _interface_quote_refusal(
            tid, interface_text, _added_lines(repo, base, head), pins)
        if refusal is not None:
            raise BenchError(refusal)

    # The proofs run in a disposable clone: the parent never checks the
    # project's own checkout out. The head is fetched into the clone BY
    # SHA (A2): GitHub deletes merged branches, so no ref may reach it —
    # the upload-pack override serves the exact commit the task record
    # names, whatever reachability says. The commands run through the
    # sandboxed gate runner (A4): the hidden tests are project code.
    stamp = new_run_id()
    add_root = bench_dir(factory_root) / project / f".add-{stamp}"
    add_root.mkdir(parents=True)
    settings_path = sandbox_settings_path(
        bench_dir(factory_root).parent, project, stamp)
    try:
        val = _proof_clone(repo, add_root / "val", head)
        gate = RunDir(project=project, run_id=stamp, root=val,
                      settings_path=settings_path)
        _gate_settings(profile, gate, bench_dir(factory_root).parent)
        proof_runner = runner or sandboxed_runner(
            val, settings_path, srt_bin_path=None)
        # F-110 AC-5: the BASE's own setup must build. The replay
        # readies its clone at the base commit before any session; a
        # base whose environment cannot install (a dependency only the
        # merged diff added, a file the setup script lost) escalates
        # setup_failed on EVERY replay — an infrastructure error each
        # time, never the model's. Refuse the item, named, at add.
        hardened_git(val, "checkout", "-q", "--force", base)
        setup_failure = _run_setup(profile, proof_runner, val, "base")
        if setup_failure is not None:
            raise BenchError(
                f"{tid}: {setup_failure} on the BASE commit {base[:12]} — "
                "every replay readies its clone there and would error "
                "before any session; fix the repo's base (or the setup "
                "command) and re-run bench add"
            )
        # A6: the proofs need the environment the profile's setup builds
        # (a venv, generated modules) — run it on the merged head's tree
        # before L1, through the same gate runner. L1's between-phase
        # cleans keep declared `paths.setup_outputs` and the gate venv
        # exactly as in production, so one run carries red, green and
        # the extra flake-guard greens.
        hardened_git(val, "checkout", "-q", "--force", head)
        setup_failure = _run_setup(profile, proof_runner, val, "add")
        if setup_failure is not None:
            raise BenchError(
                f"{tid}: {setup_failure} on the merged head — the item "
                "cannot be proven without the project's environment; fix "
                "the setup command or the repo and re-run bench add"
            )
        # L1 IS the add-time proof: red on the merge-base with only the
        # diff's test files, green on the head, through the profile's own
        # per-ref command.
        try:
            report = _run_l1_for_add(profile, val, base, head, task,
                                     proof_runner)
        except GateError as exc:
            raise BenchError(
                f"{tid}: the add-time proof failed: {exc}") from exc
        findings = [f for f in report.get("findings") or []]
        if findings:
            proving = [f for f in findings
                       if f.get("id") == "test_proves_nothing"]
            if proving:
                raise BenchError(
                    f"{tid}: the hidden tests PASS on the base commit — "
                    f"they prove nothing ({_finding_texts(proving)}); an "
                    "item must fail on base and pass on the merged head"
                )
            raise BenchError(
                f"{tid}: the hidden tests fail on the merged head "
                f"({_finding_texts(findings)}); an item must fail on base "
                "and pass on the merged head"
            )
        # The flake guard: L1's green pass is run 1; ADD_GREEN_RUNS - 1
        # more green passes on the exact merged head.
        hardened_git(val, "checkout", "--force", head)
        template = ((profile.get("commands") or {}).get("test_ref")
                    or DEFAULT_TEST_REF)
        for ref in refs:
            for _ in range(ADD_GREEN_RUNS - 1):
                result = proof_runner(f"green:{ref}",
                                      _ref_command(template, ref),
                                      val, _PROOF_TIMEOUT_SEC)
                evidence = not_collected_evidence(result, ref)
                if result.timed_out or result.exit_code != 0 \
                        or evidence is not None:
                    raise BenchError(
                        f"{tid}: the hidden test {ref} did not pass every "
                        "green run on the merged head (a flaky hidden test "
                        "cannot score a replay)"
                    )
    finally:
        shutil.rmtree(add_root, ignore_errors=True)
        settings_path.unlink(missing_ok=True)

    # N3 (my call: refuse): hidden tests with no assert lines leave the
    # leak check nothing to hunt — the item would replay unchecked.
    if not _leak_markers(hidden):
        raise BenchError(
            f"{tid}: the hidden tests carry no assert lines — no leak "
            "marker can be computed, and the bench refuses to replay an "
            "item it cannot check (assert-style hidden tests, e.g. "
            "pytest, are the supported shape; jest/vitest-style items "
            "are future work)"
        )

    # The slot: the next free BENCH-NN. Allocated by mkdir itself (N2):
    # two parallel adds cannot take the same slot — the directory is the
    # reservation, so a collision retries on the next number.
    project_root = bench_dir(factory_root) / project
    project_root.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        slot = f"BENCH-{number:02d}"
        try:
            item_dir = project_root / slot
            item_dir.mkdir(exist_ok=False)
            break
        except FileExistsError:
            number += 1
            if number > 9999:
                raise BenchError(
                    f"no free bench slot under {project_root} (tried "
                    "BENCH-0001..BENCH-9999)"
                ) from None

    doc = {
        "slot": slot,
        "project": project,
        "source_task": str(tid),
        "source_pr": _pr_number(pr_url),
        "repo_path": str(repo),
        "default_branch": default_branch,
        "base": base,
        "head": head,
        "added": datetime.now(timezone.utc).isoformat(),
        "contract": contract,
        "hidden_refs": list(refs),
        "hidden_files": list(files),
    }
    if interface_text is not None:
        if not interface_text.strip():
            raise BenchError(
                f"{tid}: the interface text is empty — name the pinned "
                "API or drop --interface"
            )
        doc["interface"] = interface_text
    _validate_item_doc(doc, "new item")
    try:
        (item_dir / "item.json").write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        for path, text in hidden.items():
            target = item_dir / "hidden" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    except BaseException:
        # release the reservation a failed write holds
        shutil.rmtree(item_dir, ignore_errors=True)
        raise
    return {
        "slot": slot,
        "item": BenchItem.from_dict(doc),
        "item_dir": item_dir,
        "hidden_dir": item_dir / "hidden",
    }


def bench_check(factory_root: Path | str | None, project: str) -> dict:
    """Re-validate every stored item (F-110 AC-1); change nothing.

    The store loads through the schema (as ever), each item's repo,
    base and head must still exist, its hidden files must still be in
    the store, and the pin analysis re-runs against the CURRENT repo —
    an item added before the interface rule (or whose repo moved under
    its pins) is named with each refusal it would now get at add. The
    natural companion to a rule change: the owner runs it, re-adds the
    refused items with ``--interface``, and the store is honest again.

    Returns:
        ``{"project", "items": [{"slot", "source_task", "ok",
        "refusals"}], "refused": [slots]}``.
    """
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    items = load_items(root, project)
    out: list[dict] = []
    for item in items:
        refusals: list[str] = []
        repo = Path(item.repo_path).expanduser()
        if not (repo / ".git").exists():
            refusals.append(
                f"the recorded repo {item.repo_path} is not a git "
                "repository (moved? cloned elsewhere?) — re-approve the "
                "profile and re-add the item")
        elif not _git_ok(repo, "cat-file", "-e", f"{item.base}^{{commit}}") \
                or not _git_ok(repo, "cat-file", "-e",
                               f"{item.head}^{{commit}}"):
            refusals.append(
                "the repo no longer holds the item's base or head commit "
                "(history rewritten?) — re-add the item")
        else:
            try:
                hidden = item.hidden_files_text(root)
            except BenchError as exc:
                refusals.append(str(exc))
            else:
                contract = item.contract
                contract_text = "\n".join(
                    [str(contract.get("title") or "")]
                    + [str(a.get("text") or "")
                       for a in contract.get("acceptance") or []
                       if isinstance(a, dict)]
                    + list(contract.get("out_of_scope") or []))
                pins = _hidden_pins(repo, item.base, item.head, hidden,
                                    contract_text, item.interface or "")
                if pins:
                    refusals.append(_pins_refusal(item.slot, pins))
        out.append({"slot": item.slot, "source_task": item.source_task,
                    "ok": not refusals, "refusals": refusals})
    return {"project": project, "items": out,
            "refused": [i["slot"] for i in out if not i["ok"]]}


def _check_hidden_paths(where: str, paths) -> None:
    """N1: every hidden-file path is a plain repo-relative path — not
    absolute, no ``..`` segment. The paths are written under the
    store's ``hidden/`` and copied into the score clone; a climbing or
    absolute path would write outside them.

    Raises:
        BenchError: the first offending path, named with its shape.
    """
    for path in paths:
        text = str(path)
        if not text or text.strip() != text:
            raise BenchError(
                f"{where}: the hidden file path {text!r} is empty or "
                "padded")
        if Path(text).is_absolute():
            raise BenchError(
                f"{where}: the hidden file path {text!r} is absolute — "
                "hidden files are repo-relative")
        if ".." in Path(text).parts:
            raise BenchError(
                f"{where}: the hidden file path {text!r} climbs (..) — "
                "hidden files are repo-relative and cannot escape the "
                "store's hidden/ or the score clone")


def _refuse_stale_default(repo: Path, default_branch: str, tid: str) \
        -> None:
    """A7: refuse to derive a base from a stale local default branch.

    The local checkout can lag the remote (JobFinderOS: 16 commits).
    Deriving the base from it silently picks the wrong fork point — a
    merge commit that exists only on the remote falls back to a plain
    merge-base far in the past, and the replay would start before other
    tasks' merged work, scoring something meaningless with no error. So
    when the repo tracks a remote default branch and the local one does
    not contain it (behind), refuse, naming the count — the owner pulls
    the project repo; the bench never fetches.
    """
    names = _git_out(repo, "remote").split()
    if not names:
        return
    remote = "origin" if "origin" in names else (
        names[0] if len(names) == 1 else None)
    if remote is None:
        return
    tracking = f"refs/remotes/{remote}/{default_branch}"
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", tracking):
        return
    if _git_ok(repo, "merge-base", "--is-ancestor", tracking,
               f"refs/heads/{default_branch}"):
        return  # the local branch is current or ahead
    behind = _git_out(repo, "rev-list", "--count",
                      f"refs/heads/{default_branch}..{tracking}").strip()
    raise BenchError(
        f"{tid}: the local {default_branch} is {behind} commit(s) behind "
        f"{remote}/{default_branch} — the base would be derived from "
        "stale history (a merge that exists only on the remote sends the "
        "fork point far into the past, and the replay would start in the "
        "wrong place); pull the project repo and re-run bench add (the "
        "bench never fetches)"
    )


def _derive_base(repo: Path, head: str, default_branch: str) -> str:
    """The commit the replay starts from: the fork point of the pushed
    head and the default branch.

    A merge-commit merge (``--no-ff``) puts the pushed commits ON the
    default branch, so the plain merge-base IS the head — the base is
    the fork point with main's PRE-MERGE tip instead: the newest
    first-parent merge commit that contains the head but whose first
    parent does not (a later merge that merely descends from the one
    that brought the head in is skipped exactly by that second half).
    Squash merges (and anything else that leaves no ancestry) keep the
    plain merge-base: the fork point, directly.
    """
    default_ref = f"refs/heads/{default_branch}"
    for merge_sha in _git_out(repo, "rev-list", "--first-parent",
                              "--merges", default_ref).split():
        if not _git_ok(repo, "merge-base", "--is-ancestor", head,
                       merge_sha):
            continue
        if _git_ok(repo, "merge-base", "--is-ancestor", head,
                   f"{merge_sha}^1"):
            continue  # a later merge descending from the real one
        parent = _git_out(repo, "rev-parse", f"{merge_sha}^1")
        return _git_out(repo, "merge-base", head, parent)
    return _git_out(repo, "merge-base", head, default_ref)


def _proof_clone(repo: Path, dest: Path, head: str) -> Path:
    """A disposable clone of the project repo holding the pushed head,
    fetched BY SHA (A2: merged branches get deleted, so no ref may
    reach the head; the upload-pack override serves the exact commit
    the task record names)."""
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "clone", "--quiet", str(repo), str(dest)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot clone {repo} for the add-time proofs: "
            f"{proc.stderr.strip()[:400]}"
        )
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(dest), "fetch", "--quiet",
         "--no-tags",
         "--upload-pack",
         "git -c uploadpack.allowAnySHA1InWant=true upload-pack",
         str(repo), head], capture_output=True, text=True)
    if proc.returncode != 0 or not _git_ok(dest, "cat-file", "-e",
                                           f"{head}^{{commit}}"):
        raise BenchError(
            f"cannot fetch the pushed head {head[:12]} into the proof "
            f"clone: {proc.stderr.strip()[:300]}"
        )
    return dest


def _run_setup(profile: dict, runner, clone: Path, where: str) \
        -> "str | None":
    """Run the profile's ``commands.setup`` through the gate runner
    (A6) — the proofs and the scoring need the environment the project
    builds (a venv, node_modules, generated modules), which a fresh
    clone does not have. Mirrors L0's own invocation (its ``setup``
    command, its failure classes); a heavy project setup gets a longer
    window than a gate command (a cold npm install can outlast one).

    Returns None on success (or when the profile sets no setup), a
    failure description otherwise — the caller refuses (add) or records
    (scoring), never crashes."""
    command = ((profile.get("commands") or {}).get("setup") or "").strip()
    if not command:
        return None
    result = runner(f"setup:{where}", command, clone, _SETUP_TIMEOUT_SEC)
    if result.timed_out:
        return (f"setup failed: timed out after {_SETUP_TIMEOUT_SEC}s")
    if result.exit_code != 0:
        tail = (result.output or "").strip()[-300:]
        return (f"setup failed (exit {result.exit_code})"
                + (f": {tail}" if tail else ""))
    return None


def _run_l1_for_add(profile, val, base, head, task, runner) -> dict:
    from factory.l1 import run_l1

    report = run_l1(profile, val, base, head, task=task, runner=runner,
                    timeout_sec=_PROOF_TIMEOUT_SEC)
    return report.to_dict()


def _finding_texts(findings) -> str:
    texts = [
        f"{f.get('id')}: {(f.get('text') or '')[:120]}"
        for f in findings if isinstance(f, dict)
    ][:3]
    return "; ".join(texts) or "no recorded finding"


def _find_task(queue: dict, source: str) -> dict:
    """The merged task by id, or by PR number (``…/pull/<n>``)."""
    tasks = queue.get("tasks") or []
    for task in tasks:
        if isinstance(task, dict) and task.get("id") == source:
            return task
    if re.fullmatch(r"[0-9]+", source):
        number = int(source)
        suffix = f"/pull/{number}"
        matches = [task for task in tasks if isinstance(task, dict)
                   and str((task.get("links") or {}).get("pr") or "")
                   .endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise BenchError(
                f"PR #{number} matches {len(matches)} tasks — name the "
                "task id instead"
            )
    raise BenchError(
        f"no task {source!r} in the project queue — bench add takes a "
        "task id or a merged PR number"
    )


def _pr_number(pr_url: str) -> int:
    match = re.search(r"/pull/([0-9]+)$", str(pr_url))
    if not match:
        raise BenchError(f"the task's PR link is not a pull-request URL: "
                         f"{pr_url!r}")
    return int(match.group(1))


# ------------------------------------------------------- AC-2 the future --


def sanitize_origin(source: Path, dest: Path, base: str,
                    default_branch: str) -> None:
    """Build the sanitized origin: a repo holding the base commit ALONE.

    Clone, reduce to one branch at the base, drop the remote, every
    other ref and every tag, expire the reflogs and rebuild the packs
    (``gc --prune=now``) — then VERIFY, fail closed, before any session
    could see it: exactly one ref (the default branch, at the base), no
    remote, and EVERY object in the store reachable from the base
    (``cat-file --batch-all-objects`` ⊆ ``rev-list --objects <base>``
    — the general check, not a base..head list: a squash commit, a
    merge commit or any straggler fails it, whatever it is). A leak
    here is the benchmark's costlier failure: the model could read the
    merged answer out of the object database.

    Raises:
        BenchError: the construction or the verification failed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "clone", "--quiet", str(source),
         str(dest)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot clone {source} for the replay: "
            f"{proc.stderr.strip()[:400]}"
        )
    hardened_git(dest, "checkout", "-q", "--detach", base)
    hardened_git(dest, "branch", "-f", default_branch, base)
    refnames = [
        line.strip()
        for line in _git_out(dest, "for-each-ref", "--format=%(refname)")
        .splitlines() if line.strip()
    ]
    keep = f"refs/heads/{default_branch}"
    for ref in refnames:
        if ref != keep:
            hardened_git(dest, "update-ref", "-d", ref)
    if (dest / ".git" / "refs" / "remotes").exists():
        shutil.rmtree(dest / ".git" / "refs" / "remotes",
                      ignore_errors=True)
    for name in ("ORIG_HEAD", "FETCH_HEAD"):
        (dest / ".git" / name).unlink(missing_ok=True)
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(dest), "remote", "remove",
         "origin"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot remove the clone's remote: {proc.stderr.strip()[:300]}"
        )
    hardened_git(dest, "reflog", "expire", "--expire=now", "--all")
    proc = subprocess.run(
        ["git", *GIT_HARDENING, "-C", str(dest), "gc", "--prune=now",
         "--quiet"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BenchError(
            f"cannot rebuild the origin's packs: {proc.stderr.strip()[:300]}"
        )
    hardened_git(dest, "checkout", "-q", default_branch)

    # The verification (fail closed BEFORE any session starts).
    refnames = [
        line.strip()
        for line in _git_out(dest, "for-each-ref", "--format=%(refname)")
        .splitlines() if line.strip()
    ]
    if refnames != [keep]:
        raise BenchError(
            f"the sanitized origin holds refs {refnames} — only "
            f"{keep} at the base may exist; refusing the replay"
        )
    if _git_out(dest, "rev-parse", f"refs/heads/{default_branch}") != base:
        raise BenchError(
            "the sanitized origin's branch does not point at the base "
            "commit; refusing the replay"
        )
    if _git_out(dest, "remote") != "":
        raise BenchError(
            "the sanitized origin kept a remote — the future must be "
            "unreachable; refusing the replay"
        )
    objects = _git_out(dest, "cat-file", "--batch-all-objects",
                       "--batch-check")
    have = {line.split(" ", 1)[0] for line in objects.splitlines()
            if line.strip()}
    allowed = {
        line.split(" ", 1)[0]
        for line in _git_out(dest, "rev-list", "--objects", base)
        .splitlines() if line.strip()
    }
    extra = sorted(have - allowed)
    if extra:
        raise BenchError(
            f"the sanitized origin holds {len(extra)} object(s) not "
            f"reachable from the base ({extra[0][:12]} …) — the merged "
            "answer must be unreachable; refusing the replay"
        )


def _leak_markers(hidden: dict) -> list:
    """The hidden tests' assert lines — the strings whose appearance in
    parent-built state before the session means the answer leaked.
    Trivial lines (imports, defs) are excluded: they collide with what
    any test file legitimately carries. The check itself runs BEFORE
    the session (see :func:`_replay_one`): after it, a correct
    implementer's own tree converges toward the hidden tests and every
    gate echoes what it ran, so a post-hoc walk could not tell a leak
    from the work."""
    markers: list[str] = []
    for text in hidden.values():
        for line in text.splitlines():
            stripped = line.strip()
            if "assert" in stripped and len(stripped) >= 8 \
                    and stripped not in markers:
                markers.append(stripped)
    return markers[:20]


def _base_absent_markers(origin: Path, markers: list) -> list:
    """A8: only the markers the BASE tree does not already hold.

    Common assert lines live in sibling test files on the base commit
    (JobFinderOS: every PR'd task shares one with some pre-existing
    file) — whatever the base already holds, the real run saw too, so
    it is not a leak. Each marker is grep'd fixed-string against the
    sanitized origin (= the base tree); a grep that itself errors keeps
    the marker (fail closed)."""
    kept: list[str] = []
    for marker in markers:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(origin), "grep", "-q",
             "-I", "-F", "--", marker], capture_output=True, text=True)
        if proc.returncode == 0:
            continue  # the base tree already holds it
        kept.append(marker)
    return kept


def _leak_check(paths, markers: list) -> None:
    """Refuse (named, with the paths) when parent-built state carries
    any hidden-test marker. Runs before any session starts and before
    the hidden tests are copied anywhere near the replay's outputs."""
    if not markers:
        return
    offending: list[str] = []
    for root in paths:
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8",
                                      errors="replace")
            except OSError:  # pragma: no cover - unreadable artefact
                continue
            hits = [m for m in markers if m in text]
            if hits:
                offending.append(f"{path} ({hits[0][:60]}…)")
                if len(offending) >= 5:
                    break
        if len(offending) >= 5:
            break
    if offending:
        raise BenchError(
            "leak: the hidden tests' text reached a run artefact before "
            "scoring — " + "; ".join(offending)
        )


# ---------------------------------------------------------- AC-3 the run --


class _ScorePR:
    """The replay's PR seam: publishing replaced by scoring. Records
    that the factory would have published, and returns an opaque
    bench:// URL no GitHub path will ever accept."""

    def __init__(self, project: str, slot: str):
        self.project = project
        self.slot = slot
        self.calls: list[dict] = []

    def __call__(self, pr) -> str:
        self.calls.append({
            "title": pr.title, "draft": bool(pr.draft),
            "label": getattr(pr, "label", None),
        })
        return f"bench://{self.project}/{self.slot}"


def _bench_task(item: BenchItem) -> dict:
    """The replay's task dict: the item's contract on an opaque slot,
    under the bench's own budget.

    F-110: the owner-written interface text is part of the contract the
    implementer is prompted with — an extra acceptance criterion (check
    type ``manual``: no gate runs it, the L2 reviewer reads it like any
    criterion, and the prompt renders it in the contract block). The
    item's own acceptance ids stay untouched; the interface criterion
    takes the next free number.
    """
    acceptance = copy.deepcopy(item.contract["acceptance"])
    if item.interface:
        numbers = [int(m.group(1)) for entry in acceptance
                   if (m := re.match(r"AC-(\d+)$",
                                     str(entry.get("id") or "")))]
        acceptance.append({
            "id": f"AC-{max(numbers, default=0) + 1}",
            "text": "Public interface this task must expose (part of "
                    "the approved contract): " + item.interface.strip(),
            "check": {"type": "manual"},
        })
    task = {
        "id": item.slot,
        "title": item.contract["title"],
        "source": item.contract.get("source") or "bench",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": list(item.contract.get("risk") or []),
        "gates": {"spec_approval": True, "merge": "human",
                  "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": item.added,
        "approved_by": "factory bench",
        "budget": {"size": "M", "max_attempts": BENCH_MAX_ATTEMPTS,
                   "max_turns": BENCH_MAX_TURNS,
                   "wall_clock_min": BENCH_WALL_CLOCK_MIN},
        "scope": {"paths": list(item.contract["scope"])},
        "attempts": [],
    }
    if item.contract.get("out_of_scope"):
        task["out_of_scope"] = list(item.contract["out_of_scope"])
    if item.contract.get("dependencies"):
        task["dependencies"] = copy.deepcopy(
            item.contract["dependencies"])
    return task


def _replay_one(item: BenchItem, factory_root: Path, run_root: Path,
                config: FactoryConfig, *, session_runner,
                command_runner, repeat: int, stamp: str,
                evidence_root: Path) -> dict:
    """One replay of one item: the real attempt path over a sanitized
    clone of the base, then hidden-test scoring on the produced head.

    F-110: a replay that is not clean keeps its evidence — every hidden
    ref's status and output tail on the record, the escalation note and
    setup failure text, and a small evidence folder (the base..head
    diff and the test output) under ``bench/<project>/runs/<stamp>/``
    where the run's own cleanup cannot reach it."""
    from factory import orchestrator
    from factory.state import TaskQueue

    started = time.monotonic()
    repo = Path(item.repo_path).expanduser()
    if not _git_ok(repo, "cat-file", "-e", f"{item.base}^{{commit}}") \
            or not _git_ok(repo, "cat-file", "-e",
                           f"{item.head}^{{commit}}"):
        raise BenchError(
            f"{item.slot}: the recorded repo {item.repo_path} no longer "
            "holds the item's base or head commit (history rewritten?) — "
            "re-add the item"
        )

    origin = run_root / "origin"
    sanitize_origin(repo, origin, item.base, item.default_branch)

    scratch = run_root / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    queue_dir = run_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "tasks.json"
    queue_path.write_text(json.dumps(
        {"version": current_epoch(), "project": item.project,
         "tasks": [_bench_task(item)]}, indent=2) + "\n",
        encoding="utf-8")

    profile = copy.deepcopy(load_profile(
        repo / ".factory" / "project.yaml", config=config))
    # The replay repoints the profile's repo at the sanitized origin:
    # the project's own commands, paths, sentinels and stack run; the
    # remote is the local origin, so a push can never leave the bench.
    profile["repo"] = {**profile["repo"], "path": str(origin),
                       "remote": str(origin),
                       "default_branch": item.default_branch}

    score_pr = _ScorePR(item.project, item.slot)
    # A1: the default seat runner is the REAL sandboxed session runner,
    # exactly run_once's default — the owner's paid baseline runs
    # models. The interpreter probe runs with it, and only with it
    # (tests inject stub seats and no probe), mirroring run_once.
    seat_runner = session_runner or run_session_sandboxed
    if seat_runner is run_session_sandboxed:
        probe_session_interpreter(profile, srt_bin_path=None)
    ctx = orchestrator._Ctx(
        queue=TaskQueue(queue_path), task_id=item.slot,
        profile=profile, factory_root=scratch,
        session_runner=seat_runner, open_pr=score_pr, config=config,
        now=lambda: datetime.now(timezone.utc), srt_bin_path=None,
        gate_timeout_sec=600,
    )
    # AC-2's artefact check, run BEFORE any session starts: everything
    # the parent has built so far (the queue, the scratch, the origin)
    # must carry nothing of the hidden tests. This is the moment the
    # check is meaningful — a post-hoc walk over the replay's outputs
    # could not tell a leak from a CORRECT implementer's own work (its
    # tree converges toward the hidden tests, and every gate echoes
    # what it ran); before the session, any hit is the parent's.
    hidden = item.hidden_files_text(factory_root)
    # A8: only markers the base tree does not already hold count — the
    # origin IS the base tree, so the filter runs against it before any
    # scan.
    _leak_check([scratch, queue_dir, origin],
                _base_absent_markers(origin, _leak_markers(hidden)))

    escalation = None
    outcome_error = None
    try:
        with ctx.queue.begin_run(item.slot):
            result = orchestrator._run_task(ctx, None)
        outcome = result.outcome
        escalation = getattr(result, "escalation", None)
    except Exception as exc:  # noqa: BLE001 — recorded, never silent
        outcome_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        outcome = "error"
    if (escalation or outcome) == "peak_window":
        # The window opened mid-run: the pipeline never attempted this
        # replay. Recorded as an errored replay (named, the run exits
        # non-zero), never as the implementer's failure.
        raise BenchError(
            "the peak window opened mid-run: this replay was never "
            "attempted — re-run the bench outside the window")

    task = ctx.queue.get(item.slot)

    # The replay head: the validated pushed head, else the kept branch
    # an escalation push left on the origin.
    links = task.get("links") or {}
    head = links.get("pushed_head")
    if not head and links.get("branch"):
        head = _origin_branch_tip(origin, links["branch"])
    hidden_pass, hidden_note, hidden_results = _score_hidden(
        origin, head, hidden, item, profile, command_runner, scratch)

    if outcome_error:
        factory_outcome = f"error:{outcome_error}"
    elif outcome == "pr_opened":
        factory_outcome = "would_publish"
    else:
        factory_outcome = f"escalated:{escalation or outcome}"
    attempts = task.get("attempts") or []
    last = attempts[-1] if attempts else {}
    cost_usd, by_role = _replay_cost(attempts, scratch, item.project)
    tokens_in = sum(bucket["input_tokens"] for bucket in by_role.values())
    tokens_out = sum(bucket["output_tokens"]
                     for bucket in by_role.values())
    models = sorted({model for bucket in by_role.values()
                     for model in bucket["models"]})
    if not models:
        models = (last.get("model") or "").split()

    # A2: whether COMMITTED model work precedes this outcome. The
    # signal is the orchestrator's own marker on the setup-failure
    # note ("after committed work on the branch") — the run clone that
    # held the commit is deleted before the bench can read it, and the
    # marker states the same truth the owner reads in production.
    note_text = str(last.get("note") or "")
    model_committed = "after committed work on the branch" in note_text
    record = {
        "repeat": repeat,
        "clean": bool(hidden_pass
                      and factory_outcome == "would_publish"),
        "hidden_pass": hidden_pass,
        "hidden_note": hidden_note,
        "hidden_results": hidden_results,
        "factory_outcome": factory_outcome,
        "escalation": escalation,
        # AC-2: a non-clean replay keeps its own record — the
        # escalation note (the setup failure text included) it ended
        # with, escaped and redacted the way session text is.
        "escalation_note": _recorded_value(note_text or None),
        # A2: a setup_failed after a commit is the model's failure
        # (judged), not infrastructure.
        "model_committed": model_committed,
        "head": head,
        "pr": links.get("pr"),
        "rounds": last.get("rounds", 0),
        "turns": sum(int(a.get("turns") or 0) for a in attempts),
        "turns_by_seat": dict(last.get("turns_by_seat") or {}),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": cost_usd,
        "attempts": len(attempts),
        "wall_sec": round(time.monotonic() - started, 1),
        "models": models,
        "by_role": by_role,
    }
    if score_pr.calls:
        record["pr_calls"] = score_pr.calls
    if factory_outcome == "escalated:setup_failed" and record["model_committed"]:
        record["escalation_note"] = _recorded_value(
            (last.get("note") or "")
            + " — judged, not errored: committed model work precedes "
              "this setup failure (F-110 AC-4)")
    if not record["clean"]:
        evidence_error = _write_evidence(
            evidence_root / f"{item.slot}-r{repeat}",
            origin, item, head, record)
        if evidence_error is not None:
            # B5: an unwritable evidence folder is a fact on the record
            # (a full disk must be visible), never a silent gap.
            record["evidence_error"] = _recorded_value(evidence_error)
    return record


def _origin_branch_tip(origin: Path, branch: str) -> "str | None":
    if _git_ok(origin, "rev-parse", "--verify", "--quiet",
               f"refs/heads/{branch}"):
        return _git_out(origin, "rev-parse", f"refs/heads/{branch}")
    return None


def _score_hidden(origin: Path, head: "str | None", hidden: dict,
                  item: BenchItem, profile: dict, command_runner,
                  scratch: Path) -> "tuple[bool, str | None, list]":
    """Copy the hidden tests into a fresh clone of the replay head and
    run their refs — outside every session, after the attempt settled.

    F-110 AC-2: EVERY ref runs, never only up to the first failure —
    each records its status (pass / fail / timeout / not-collected) and
    the escaped, redacted tail of its output, so a failure can be split
    into real defects and naming mismatches without re-running a paid
    replay.

    The commands run through the sandboxed gate runner by default (A4):
    the replay head is agent-written code, and ``gates.run_command`` is
    the fixtures-only shape its own docstring disclaims for production.
    The clone is built the merge-time re-validation's way (create_run +
    _gate_settings + sandboxed_runner), inside the replay's scratch so
    it dies with it."""
    if not head:
        return False, "no replay head (nothing was pushed)", []
    score = create_run(item.project, origin, factory_root=scratch,
                       ref=head)
    try:
        runner = command_runner or _sandbox_gate_runner(
            profile, score, scratch, item.project)
        for path, text in hidden.items():
            target = score.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        # A6: the hidden tests may need the environment the profile's
        # setup builds. A failure is RECORDED (hidden_pass False, the
        # note naming it) — the score of a replay whose head broke its
        # own setup, never a crash.
        setup_failure = _run_setup(profile, runner, score.root, "score")
        if setup_failure is not None:
            return False, f"setup failed on the replay head: " \
                          f"{_recorded_value(setup_failure)}", []
        template = ((profile.get("commands") or {}).get("test_ref")
                    or DEFAULT_TEST_REF)
        results: list[dict] = []
        for ref in item.hidden_refs:
            result = runner(f"score:{ref}", _ref_command(template, ref),
                            score.root, _PROOF_TIMEOUT_SEC)
            evidence = not_collected_evidence(result, ref)
            if result.timed_out:
                status, note = "timeout", f"{ref} timed out on the " \
                                          "replay head"
            elif evidence is not None:
                status, note = "not_collected", \
                    f"{ref} was not collected ({evidence[1]})"
            elif result.exit_code != 0:
                status, note = "fail", f"{ref} fails on the replay head"
            else:
                status, note = "pass", None
            results.append({"ref": ref, "status": status,
                            "tail": _recorded_tail(result.output)})
        hidden_pass = all(r["status"] == "pass" for r in results)
        note = next((f"{r['ref']} "
                     + {"fail": "fails on the replay head",
                        "timeout": "timed out on the replay head",
                        "not_collected": "was not collected"}[
                         r["status"]]
                     for r in results if r["status"] != "pass"), None)
        return hidden_pass, note, results
    finally:
        cleanup_run(score)


# F-110 AC-2: the output tail a failing ref keeps — capped at this many
# lines (owner decision to confirm: enough to read pytest's failure
# block, not enough to smuggle a transcript), redacted like session
# text (redact BEFORE the cap so a cut never leaves a token fragment),
# then escaped per F-109 so the report always loads.
EVIDENCE_TAIL_LINES = 40


def _recorded_tail(output: "str | None",
                   lines: int = EVIDENCE_TAIL_LINES) -> str:
    """The recordable tail of a command's output: credentials redacted
    (the session records' own scrubber), capped to the last ``lines``
    lines, control characters escaped (:func:`escape_recorded_text`)."""
    from factory.sentinels import redact_credentials

    redacted = redact_credentials(output or "")
    kept = redacted.splitlines()[-lines:] if lines else []
    return escape_recorded_text("\n".join(kept))


def _recorded_value(value: "str | None", cap: int = 400) -> "str | None":
    """A short recorded note (an escalation note, a setup failure
    text): redacted then escaped, capped to ``cap`` characters."""
    from factory.sentinels import redact_credentials

    if not value:
        return None
    return escape_recorded_text(
        redact_credentials(value)[:cap])


def _write_evidence(folder: Path, origin: Path, item: BenchItem,
                    head: "str | None", record: dict) -> "str | None":
    """AC-2: the small evidence folder a non-clean replay keeps under
    ``bench/<project>/runs/<stamp>/<slot>-rN/`` — the base..head diff
    and the hidden tests' output. Outside the run root on purpose: the
    run's own ``finally`` cleanup can never delete it. Best-effort on
    OS errors — the evidence is a courtesy on top of the record, never
    a reason to fail a scored replay — but NEVER a silent gap (B5):
    returns the failure reason for the record when the folder could
    not be written, None on success."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if head:
            diff = _git_out(origin, "diff", "--no-color", "--no-ext-diff",
                            f"{item.base}...{head}")
        else:
            diff = "(no replay head — nothing was pushed)"
        # B3: the diff is agent-written text like the tails — redacted
        # (no escape: a patch file's own bytes are its content, and
        # nothing downstream loads it through the queue).
        from factory.sentinels import redact_credentials

        (folder / "diff.patch").write_text(
            redact_credentials(diff) + "\n", encoding="utf-8")
        lines = [f"{item.slot} r{record.get('repeat')} — "
                 f"{record.get('factory_outcome')}"]
        if record.get("hidden_note"):
            lines.append(f"hidden: {record['hidden_note']}")
        if record.get("escalation_note"):
            lines.append(f"escalation: {record['escalation_note']}")
        for result in record.get("hidden_results") or []:
            lines.append(f"[{result.get('status')}] {result.get('ref')}")
            tail = result.get("tail") or ""
            if tail:
                lines.append(tail)
                lines.append("")
        (folder / "hidden-output.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        return None
    except OSError as exc:
        return (f"evidence not kept ({folder}): "
                f"{type(exc).__name__}: {exc}")[:200]


def _sandbox_gate_runner(profile: dict, run: RunDir, factory_root: Path,
                         project: str):
    """The merge-time re-validation's gate shape (orchestrator's
    ``create_run`` → ``_gate_settings`` → ``sandboxed_runner``), for
    parent-run project commands over the replay's code. ``project`` is
    the validated slug the settings path already carries."""
    _gate_settings(profile, run, factory_root)
    return sandboxed_runner(run.root, run.settings_path,
                            srt_bin_path=None)


def _replay_cost(attempts: list, scratch: Path, project: str) \
        -> "tuple[float | None, dict]":
    """F-110 AC-3/B7: the replay's summed cost and per-role usage.

    One trace per run dir (the rounds of a single ``_run_task`` share
    ``work.run_id`` — dedupe before summing, or a two-round replay
    counts twice). The cost is decided PER RUN: that run's trace-summed
    figure when its trace reported one, else that run's attempt-record
    costs — a trace without a cost must not hide a paid attempt (B4's
    rule per run: the realistic case is a session killed by quota
    before its result event). Attempts with no run_id count from their
    own records. Null only when nothing anywhere reported a cost, never
    a fake 0, never a last-attempt n/a over a paid round."""
    by_role: dict = {}
    seen_runs: set = set()
    total = 0.0
    reported = False

    def _attempt_cost(value) -> "float | None":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    for attempt in attempts:
        run_id = attempt.get("run_id")
        if run_id is None:
            cost = _attempt_cost(attempt.get("cost_usd"))
            if cost is not None:
                total += cost
                reported = True
            continue
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        roles = _trace_roles(scratch, project, run_id)
        for role, usage in roles.items():
            bucket = by_role.setdefault(role, {
                "sessions": 0, "turns": 0, "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0, "cost_sessions": 0,
                "models": []})
            for key in ("sessions", "turns", "input_tokens",
                        "output_tokens", "cost_sessions"):
                bucket[key] += usage.get(key, 0)
            bucket["cost_usd"] += usage.get("cost_usd", 0.0)
            for model in usage.get("models") or []:
                if model not in bucket["models"]:
                    bucket["models"].append(model)
        if any(bucket.get("cost_sessions") for bucket in roles.values()):
            total += sum(bucket["cost_usd"] for bucket in roles.values())
            reported = True
        else:
            for other in attempts:
                if other.get("run_id") != run_id:
                    continue
                cost = _attempt_cost(other.get("cost_usd"))
                if cost is not None:
                    total += cost
                    reported = True
    return (round(total, 6) if reported else None), by_role


def _trace_roles(scratch: Path, project: str,
                 run_id: "str | None") -> dict:
    """Per-role usage off the run's own trace (F-30's session_end
    events: role, model, tokens, cost) — the attempt record aggregates
    the attempt; the trace keeps the seats apart."""
    if not run_id:
        return {}
    path = scratch / "trace" / project / f"{run_id}.jsonl"
    if not path.is_file():
        return {}
    roles: dict = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover - unreadable trace
        return {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "session_end":
            continue
        role = event.get("role") or event.get("seat") or "?"
        bucket = roles.setdefault(role, {
            "sessions": 0, "turns": 0, "input_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0, "cost_sessions": 0,
            "models": []})
        bucket["sessions"] += 1
        turns = event.get("turns")
        if isinstance(turns, int) and not isinstance(turns, bool):
            bucket["turns"] += turns
        tokens = event.get("tokens") or {}
        for name in ("input_tokens", "output_tokens"):
            value = tokens.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                bucket[name] += value
        cost = event.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            bucket["cost_usd"] += float(cost)
            # F-110 AC-3: a session that REPORTED a cost — the replay's
            # summed cost is null only when none did, never a fake 0.
            bucket["cost_sessions"] += 1
        model = event.get("model") or event.get("requested_model")
        if model and model not in bucket["models"]:
            bucket["models"].append(model)
    return roles


# --------------------------------------------------------- AC-4 the report --

# F-110 AC-4: the closed list of INFRASTRUCTURE escalations — outcomes
# that are errors of the bench's own machinery or the owner's
# environment, never the implementer's behaviour. Errored replays
# leave the k/N denominator (they judge nothing) and the run exits
# non-zero naming them. Every other escalation — attempts_exhausted,
# review_not_converging, scope_change_needed, ci_failed, and ANY
# reason not on this list — is judged and counts against the model:
# where the classification could go either way, the flattering reading
# (dropping a bad replay) is the costlier one. Members, justified:
_INFRA_ESCALATIONS = frozenset({
    # The profile's own setup command failed or left the clone dirty
    # (F-73's ready-clone rule) — the project's environment, not the
    # model. (A round's dirt after a non-completed session is reset
    # before readying since F-110 AC-5, so this arm is genuinely
    # environmental.)
    "setup_failed",
    # The provider's quota — the owner's account, not the model.
    "quota_exhausted",
    # The owner's own spend window; a mid-run window already refuses
    # the whole replay as an error.
    "peak_window",
})
# A MISSING CREDENTIAL and a CRASH never appear as escalations at all:
# the pipeline raises (provider resolution without a token, an
# orchestrator error) and the replay records them as an ``error:``
# factory_outcome — the second error arm in :func:`_record_is_error`.
# A DIRTY CLONE is setup_failed's second arm, above. A2 (review round
# 1): a setup_failed AFTER COMMITTED model work is NOT an error — with
# AC-5's reset only a commit can change what the retry's setup sees,
# so the model broke the setup itself and is judged. Owner decision to
# confirm: a quota_exhausted after paid rounds is likewise dropped
# from k/N and named in the report — the owner's account, not the
# model's, whatever the timing.


def _record_is_error(record: dict) -> bool:
    """Whether one replay record is an infrastructure error (AC-4):
    an ``error:`` outcome, or an escalation on the closed list — EXCEPT
    a ``setup_failed`` after COMMITTED model work (A2): with F-110
    AC-5's reset, uncommitted dirt cannot reach the retry's setup, so
    a setup that breaks after a commit is the model's own doing (it
    edited a requirements or setup file) and is judged, counting
    against it."""
    outcome = str(record.get("factory_outcome") or "")
    if outcome.startswith("error:"):
        return True
    if outcome.startswith("escalated:"):
        reason = outcome.partition(":")[2]
        if reason in _INFRA_ESCALATIONS:
            if reason == "setup_failed" and record.get("model_committed"):
                return False
            return True
        return False
    return False


def _item_fingerprint(item: BenchItem, factory_root: Path) -> str:
    """What one report measured of an item: its contract, interface,
    hidden refs, hidden-test bytes, and what the replay STARTS FROM
    (base, head, repo path, default branch — B8: a re-add that moved
    the base is a different task), hashed. A resume refuses when the
    store's fingerprint no longer matches the report's — the new
    replays would be a different measurement pasted into the old
    report. Reports written before a fingerprint rule change refuse
    the same way (as drifted), which is the honest answer: re-run."""
    import hashlib

    payload = json.dumps({
        "base": item.base,
        "head": item.head,
        "repo_path": item.repo_path,
        "default_branch": item.default_branch,
        "contract": item.contract,
        "interface": item.interface,
        "hidden_refs": list(item.hidden_refs),
        "hidden_files": dict(sorted(item.hidden_files_text(
            factory_root).items())),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _BenchInterrupted(BaseException):
    """SIGTERM during a bench run (SIGINT arrives as KeyboardInterrupt).
    Deliberately NOT an Exception: the per-replay error recorder must
    not swallow it — the run stops, the report is marked incomplete."""


def _report_from_records(project: str, stamp: str, repeats: int,
                         role_overrides: dict, records: dict,
                         run_items: list, against: "str | Path | None",
                         mark: "str | None" = None,
                         fingerprints: "dict | None" = None) -> dict:
    """The report dict off the finished records (bench_run's assembly,
    extracted so the aggregation is one pure function): per item k/N
    judged E errored, totals over the summed figures, the roles that
    ran, the --against diff, and the incomplete mark an interrupt
    leaves."""
    report = {
        "kind": "bench",
        "project": project,
        "stamp": stamp,
        "repeats": repeats,
        "role_overrides": dict(role_overrides or {}),
        "roles": _report_roles(records),
        # What this report measured of each item (the resume contract:
        # a store that drifted refuses rather than mixing measurements).
        "items_fingerprint": {
            item.slot: (fingerprints or {}).get(item.slot)
            for item in run_items},
        "items": [
            {"slot": item.slot,
             "source_task": item.source_task,
             "source_pr": item.source_pr,
             "repeats": records.get(item.slot, []),
             "clean": sum(1 for r in records.get(item.slot, [])
                          if r.get("clean")),
             "judged": sum(1 for r in records.get(item.slot, [])
                           if not _record_is_error(r)),
             "errored": sum(1 for r in records.get(item.slot, [])
                            if _record_is_error(r))}
            for item in run_items
        ],
    }
    report["errors"] = [
        item["slot"] for item in report["items"]
        if item["errored"]]
    report["totals"] = _totals(report["items"])
    clean = report["totals"]["clean"]
    cost = report["totals"]["cost_usd"]
    report["yield_clean_per_usd"] = (
        round(clean / cost, 4)
        if cost is not None and cost > 0 else None)
    report["cost_per_clean_usd"] = (
        round(cost / clean, 4)
        if cost is not None and clean else None)
    report["against"] = None
    if against is not None:
        before = _load_against(against)
        report["against"] = {
            "path": str(against),
            **compare_reports(before, report),
        }
    if mark:
        report["incomplete"] = mark
    return report


def _write_report_files(report_path: Path, rendered_path: Path,
                        report: dict) -> None:
    """Write the report and its rendering ATOMICALLY, after every
    replay — a report that survives interruption halfway through
    writing is worse than none (F-110 AC-6). The rename goes through
    :func:`factory.state.atomic_install` (F-45 AC-6's one durable
    rename: file fsync, rename, parent-dir fsync)."""
    from factory.state import atomic_install

    tmp = report_path.with_name(report_path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    atomic_install(tmp, report_path)
    tmp_md = rendered_path.with_name(rendered_path.name + ".tmp")
    tmp_md.write_text(render_report(report), encoding="utf-8")
    atomic_install(tmp_md, rendered_path)


def bench_run(factory_root: Path | str | None, project: str,
              *, repeats: "int | None" = 1,
              role_overrides: dict | None = None,
              against: "str | Path | None" = None, session_runner=None,
              command_runner=None, config: FactoryConfig | None = None,
              keep: bool = False, now=None,
              items: "list[str] | None" = None,
              resume: "str | Path | None" = None) -> dict:
    """Run the replay benchmark (AC-4): every item, ``repeats`` times
    each, through the real pipeline with publishing replaced by
    scoring. Writes the report under ``<factory_root>/bench/<project>/
    reports/`` and returns ``{"report", "report_path",
    "rendered_path", "run_root"}``.

    F-110 AC-6: the report is written INCREMENTALLY and atomically
    after every replay, so an interrupted run (SIGINT/SIGTERM) leaves a
    report marked ``incomplete`` with every finished replay on it —
    ``resume`` re-enters that report, skips the recorded replays and
    appends the rest under the same stamp (a recorded replay is never
    re-run, whatever its result). ``items`` narrows the run to the
    named slots.

    ``role_overrides`` rides the config in memory ONLY (F-100 AC-5):
    never written to any profile, the registry or the item store, named
    in the report. ``against`` diffs this report against a named earlier
    one, regressions first.

    Raises:
        BenchError: no items, a bad ``repeats``, an unverifiable store,
            a start inside the peak window, a leak the run itself
            refused to score over, or a resume against a report that is
            not this project's (or whose repeats or role overrides
            differ — one report is one measurement).
    """
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    items_loaded = load_items(root, project)
    by_slot = {item.slot: item for item in items_loaded}

    # The resume contract: same project, same repeats, same overrides —
    # one report is one measurement. Recorded replays carry over
    # verbatim; only the missing (slot, repeat) pairs run.
    resumed: dict = {}
    adopted_overrides: "dict | None" = None
    report_items = None
    if resume is not None:
        try:
            resumed = json.loads(Path(resume).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BenchError(
                f"cannot read the --resume report {resume}: {exc}") from exc
        if not isinstance(resumed, dict) or resumed.get("kind") != "bench":
            raise BenchError(
                f"{resume} is not a bench report — --resume takes a "
                "report written by an earlier `factory bench run`")
        if resumed.get("project") != project:
            raise BenchError(
                f"{resume} is a report for project "
                f"{resumed.get('project')!r}, not {project!r}")
        if repeats is None:
            repeats = int(resumed.get("repeats") or 1)
        elif repeats != int(resumed.get("repeats") or 1):
            raise BenchError(
                f"--resume cannot change a report's repeats: {resume} ran "
                f"{resumed.get('repeats')}, this run asked {repeats}")
        # R2-A2: one report is one measurement. A resume with no
        # --role ADOPTS the report's own overrides (dropping them would
        # measure the default stack and label it the report's model);
        # any explicit difference — empty against non-empty included —
        # refuses.
        report_overrides = resumed.get("role_overrides") or {}
        if role_overrides is None:
            adopted_overrides = dict(report_overrides) or None
        elif dict(role_overrides) != report_overrides:
            raise BenchError(
                "--resume cannot change a report's role overrides: "
                f"{resume} ran {report_overrides}, "
                f"this run asked {dict(role_overrides)} — one report is "
                "one measurement")
        missing = [i.get("slot") for i in resumed.get("items") or []
                   if isinstance(i, dict)
                   and i.get("slot") not in by_slot]
        if missing:
            raise BenchError(
                f"the --resume report names items missing from the "
                f"store: {', '.join(sorted(map(str, missing)))} — a "
                "resume replays the report's own items only")
        report_slots = [str(i.get("slot")) for i in resumed.get("items")
                        or [] if isinstance(i, dict)]
        report_items = [by_slot[slot] for slot in report_slots
                        if slot in by_slot]
        # The store must still hold WHAT the report measured: an item
        # whose contract, interface or hidden tests changed between the
        # run and the resume would mix two measurements in one report.
        # A report with no fingerprints predates F-110's summed cost
        # and error classification — its records cannot mix with new
        # ones either.
        stored_fp = resumed.get("items_fingerprint")
        if not isinstance(stored_fp, dict) or not stored_fp:
            raise BenchError(
                f"{resume} is a pre-F-110 report (no item fingerprints) "
                "— its records predate the summed cost and the judged/"
                "errored split; re-run it instead of resuming")
        drifted = sorted(
            slot for slot, fp in
            ((item.slot, _item_fingerprint(item, root))
             for item in report_items)
            if stored_fp.get(slot) != fp)
        if drifted:
            raise BenchError(
                f"the store changed under the report: "
                f"{', '.join(drifted)} no longer match what {resume} "
                "measured (contract, interface, hidden tests, or the "
                "base/head/repo it replays from — a report written "
                "before a fingerprint rule change refuses the same way) "
                "— re-run the report instead of resuming it")
        # R2-A1: the resume runs the REPORT's items — never store items
        # the report never had (their replays would run and vanish from
        # the file), and --items may only narrow.
        if items is not None:
            foreign = [slot for slot in items if slot not in
                       set(report_slots)]
            if foreign:
                raise BenchError(
                    f"--resume cannot widen a report with items it never "
                    f"measured: {', '.join(foreign)} (the report holds "
                    f"{', '.join(report_slots)}) — start a new run for "
                    "them")
    if repeats is None:
        repeats = 1
    if isinstance(repeats, bool) or not isinstance(repeats, int) \
            or repeats < 1:
        raise BenchError(f"repeats must be an integer >= 1, got {repeats!r}")

    if items is not None:
        items = [str(slot) for slot in items]
        unknown = [slot for slot in items if slot not in by_slot]
        if unknown:
            raise BenchError(
                f"no bench items {', '.join(unknown)} for project "
                f"{project!r} — the store holds "
                f"{', '.join(sorted(by_slot))}")
        run_items = [by_slot[slot] for slot in items]
    else:
        run_items = items_loaded

    loaded = config if config is not None else load_config(root / CONFIG_FILE)
    if adopted_overrides:
        loaded = dataclasses.replace(loaded,
                                     role_overrides=dict(adopted_overrides))
    if role_overrides:
        loaded = dataclasses.replace(loaded,
                                     role_overrides=dict(role_overrides))
    # The replay's config: the owner's registry and knobs, except the
    # publish-path CI wait — a bench PR has no checks to wait for, and
    # ci_wait_min=0 is the documented pre-F-75 shape (no CI read in the
    # publish path at all). Nothing else about the pipeline changes.
    run_config = dataclasses.replace(loaded, ci_wait_min=0)
    # The pipeline refuses to start a session inside the peak window,
    # and a replay it refuses would score as a factory failure — a
    # corrupted baseline. Refuse the whole run up front, before any
    # spend, the way `factory review-contract` does.
    clock = now or (lambda: datetime.now(timezone.utc))
    if in_peak_window(clock(), run_config.peak_window):
        raise BenchError(
            "bench run refused: inside the peak window no replay starts "
            "(every replay would escalate peak_window and score as a "
            "failure); run it outside the window set in config.yaml")

    # A resume keeps the report's own stamp and file: the appended
    # replays land under the same report (AC-6). A1/R2-A1 (review):
    # the REPORT's item set is always the report's own — every recorded
    # replay stays in the file; the RUN set (what may run now) is the
    # report's items, narrowed by --items, never widened, and never the
    # bare store's (a store item the report never had would run and
    # its paid replays would vanish from the file).
    stamp = str(resumed.get("stamp")) if resumed else new_run_id()
    records: dict = {}
    if resumed:
        for entry in resumed.get("items") or []:
            if isinstance(entry, dict) and entry.get("slot"):
                records[str(entry["slot"])] = list(
                entry.get("repeats") or [])
    if report_items is None:
        report_items = run_items
    elif items is not None:
        run_items = [item for item in report_items
                     if item.slot in set(items)]
    else:
        run_items = report_items

    run_root = bench_dir(root) / project / ".runs" / stamp
    # The evidence tree sits OUTSIDE the run root on purpose (AC-6):
    # the run's finally cleanup deletes the scratch and the sanitized
    # origin, never a finished replay's evidence.
    evidence_root = bench_dir(root) / project / "runs" / stamp
    reports_dir = bench_dir(root) / project / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = (Path(resume) if resume is not None
                   else reports_dir / f"bench-{stamp}.json")
    rendered_path = report_path.with_suffix(".md")

    # What this report measures of each item — stamped into every
    # written report so a resume can refuse a store that drifted.
    fingerprints = {item.slot: _item_fingerprint(item, root)
                    for item in report_items}

    # One report is written by ONE run at a time (A2, round 3): EVERY
    # run — fresh included — takes the flock on its RESOLVED report
    # path before its first write and holds it to the end. A fresh run
    # holding its own report is what stops the owner's resume of a
    # report that is still being written (the in-progress mark says so;
    # the lock enforces it); resolving the path first means a symlink
    # or a second spelling of the same report cannot dodge the lock.
    # flock (kernel-managed): a crashed holder releases it with its
    # process, so the lockfile left beside the report is inert.
    import fcntl

    lock_path = str(Path(report_path).resolve()) + ".lock"
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        raise BenchError(
            f"another bench run holds {Path(report_path).resolve()} — "
            "one report is written at a time; let it finish or stop it "
            "first")

    def _assemble(mark: "str | None" = None) -> dict:
        final_mark = mark
        if mark is None:
            # A1 (round 3): EVERY write before the report's own set is
            # fully recorded carries a mark — fresh runs included. A
            # hard kill (SIGKILL, OOM, a closed terminal) leaves the
            # last incremental write, and that file must say the
            # measurement is unfinished, never read as a short but
            # complete sample. Only a write over a fully-recorded set
            # (the loop's last write, or a final write of a narrowed
            # resume that still owes replays) may drop it — and the
            # narrowed case keeps it.
            missing = sum(
                1 for item in report_items
                for rep in range(1, repeats + 1)
                if not any(r.get("repeat") == rep
                           for r in records.get(item.slot, [])))
            if missing:
                final_mark = (
                    f"in progress: {missing} replay(s) of this report's "
                    "set still to run — if no bench run holds this "
                    f"report, resume it with `factory bench run {project}"
                    f" --resume {report_path}`")
        return _report_from_records(
            project, stamp, repeats,
            dict(getattr(run_config, "role_overrides", None) or {}),
            records, report_items, against=against, mark=final_mark,
            fingerprints=fingerprints)

    run_root.mkdir(parents=True, exist_ok=True)
    try:
        # SIGINT arrives as KeyboardInterrupt; SIGTERM and SIGHUP (a
        # closed terminal tab) get handlers that raise the same shape
        # (a BaseException, so the per-replay error recorder cannot
        # swallow it — the run stops, the report is marked). Main
        # thread only.
        def _term(_sig, _frame):
            raise _BenchInterrupted(signal.Signals(_sig).name)

        old_handlers: dict = {}
        for sig_name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, sig_name, None)
            if sig is None:  # pragma: no cover - platform without it
                continue
            try:
                old_handlers[sig] = signal.signal(sig, _term)
            except ValueError:  # pragma: no cover - not the main thread
                pass
        try:
            for item in run_items:
                for repeat in range(1, repeats + 1):
                    if any(r.get("repeat") == repeat
                           for r in records.get(item.slot, [])):
                        continue  # already recorded — never re-run
                    one_root = (run_root if (len(run_items) == 1
                                             and repeats == 1
                                             and not records)
                                else run_root / f"{item.slot}-r{repeat}")
                    one_root.mkdir(parents=True, exist_ok=True)
                    # A9: one failing replay never aborts the run — the
                    # paid replays already done must survive. The error
                    # is recorded (never clean, hidden_pass False, the
                    # reason on the outcome) and the run goes on; the
                    # report is always written.
                    try:
                        record = _replay_one(
                            item, root, one_root, run_config,
                            session_runner=session_runner,
                            command_runner=command_runner, repeat=repeat,
                            stamp=stamp, evidence_root=evidence_root)
                    except Exception as exc:  # noqa: BLE001 — recorded
                        record = {
                            "repeat": repeat, "clean": False,
                            "hidden_pass": False,
                            "hidden_results": [],
                            "hidden_note":
                                f"replay error: {str(exc)[:300]}",
                            "factory_outcome":
                                f"error:{type(exc).__name__}: "
                                f"{str(exc)[:300]}",
                            "escalation_note": None,
                            "head": None, "rounds": 0, "turns": 0,
                            "turns_by_seat": {}, "input_tokens": 0,
                            "output_tokens": 0, "cost_usd": None,
                            "attempts": 0, "wall_sec": 0.0, "models": [],
                            "by_role": {},
                        }
                    records.setdefault(item.slot, []).append(record)
                    records[item.slot].sort(key=lambda r: r.get("repeat")
                                            or 0)
                    # AC-6: incremental, atomic — every finished replay
                    # is on disk before the next one starts.
                    _write_report_files(report_path, rendered_path,
                                        _assemble())
        except (KeyboardInterrupt, _BenchInterrupted) as exc:
            reason = (f"{type(exc).__name__}"
                      + (f": {exc}" if str(exc) else ""))
            done = sum(len(v) for v in records.values())
            _write_report_files(
                report_path, rendered_path,
                _assemble(mark=f"interrupted ({reason}) after {done} "
                               f"replay(s) — resume with `factory bench "
                               f"run {project} --resume {report_path}`"))
            raise
        finally:
            for sig, old in old_handlers.items():
                signal.signal(sig, old)
        report = _assemble()
        _write_report_files(report_path, rendered_path, report)
        return {"report": report, "report_path": report_path,
                "rendered_path": rendered_path, "run_root": run_root}
    finally:
        if lock_handle is not None:
            lock_handle.close()
        if not keep:
            shutil.rmtree(run_root, ignore_errors=True)


def _load_against(against: "str | Path") -> dict:
    path = Path(against)
    try:
        before = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError(f"cannot read the --against report {path}: {exc}") \
            from exc
    if not isinstance(before, dict) or before.get("kind") != "bench":
        raise BenchError(
            f"{path} is not a bench report — --against takes a report "
            "written by an earlier `factory bench run`"
        )
    return before


def compare_reports(before: dict, after: dict) -> dict:
    """The item-level diff two reports are compared by: an item PASSED a
    report when it has at least one clean merge over judged repeats.
    Regressions (passed → failed) come FIRST and every section is
    sorted by slot; an item on only one side never counts as either.

    F-110 AC-4: an item whose replays went from all-errored to judged
    (or back) lands in ``error_changes`` — a change to investigate,
    never a regression or an improvement (an error judges nothing)."""
    def passed(items, slot: str) -> "bool | None":
        for item in items or []:
            if isinstance(item, dict) and item.get("slot") == slot:
                if not item.get("judged"):
                    return None
                return item.get("clean", 0) > 0
        return None

    def errored(items, slot: str) -> "bool | None":
        """True when every recorded repeat errored (nothing judged)."""
        for item in items or []:
            if isinstance(item, dict) and item.get("slot") == slot:
                total = (item.get("judged") or 0) \
                    + (item.get("errored") or 0)
                if not total:
                    return None
                return not item.get("judged")
        return None

    before_items = before.get("items") or []
    after_items = after.get("items") or []

    def _slots(items) -> set:
        return {str(item.get("slot")) for item in items
                if isinstance(item, dict)}

    slots = sorted(_slots(after_items) | _slots(before_items))
    regressions: list[str] = []
    improvements: list[str] = []
    unchanged: list[str] = []
    error_changes: list[str] = []
    for slot in slots:
        was, now = passed(before_items, slot), passed(after_items, slot)
        was_err = errored(before_items, slot)
        now_err = errored(after_items, slot)
        if was_err is not None and now_err is not None and was_err != now_err:
            error_changes.append(str(slot))
        if was is None or now is None:
            continue
        if was and not now:
            regressions.append(str(slot))
        elif now and not was:
            improvements.append(str(slot))
        else:
            unchanged.append(str(slot))
    return {"regressions": regressions, "improvements": improvements,
            "unchanged": unchanged, "error_changes": error_changes}


def _report_roles(records: dict) -> dict:
    roles: dict = {}
    for repeats in records.values():
        for record in repeats:
            for role, usage in (record.get("by_role") or {}).items():
                for model in usage.get("models") or []:
                    roles.setdefault(role, [])
                    if model not in roles[role]:
                        roles[role].append(model)
    return roles


def _totals(items: list) -> dict:
    clean = sum(item.get("clean", 0) for item in items)
    judged = sum(item.get("judged", 0) for item in items)
    errored = sum(item.get("errored", 0) for item in items)
    cost = None
    tokens_in = tokens_out = 0
    wall = 0.0
    for item in items:
        for record in item.get("repeats") or []:
            value = record.get("cost_usd")
            if value is not None:
                cost = (cost or 0.0) + float(value)
            tokens_in += record.get("input_tokens", 0) or 0
            tokens_out += record.get("output_tokens", 0) or 0
            wall += record.get("wall_sec", 0.0) or 0.0
    return {"clean": clean, "judged": judged, "errored": errored,
            "cost_usd": cost,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "wall_sec": round(wall, 1)}


def _fmt_cost(value) -> str:
    if value is None:
        return "n/a (no session reported a cost)"
    return f"${value:.2f}"


def render_report(report: dict) -> str:
    """The owner-facing bench report: per item k/N clean merges with
    each repeat's outcome (k/N judged, E errored — F-110 AC-4), the
    yield figure, cost per clean merge, the models that ran per role,
    the overrides this run carried, and the ``--against`` diff with
    regressions first. An interrupted run is marked INCOMPLETE with
    its resume command (AC-6)."""
    repeats = int(report.get("repeats") or 1)
    lines = [
        f"Replay benchmark — project: {report['project']} "
        f"({len(report.get('items') or [])} item(s), {repeats} repeat"
        + ("s" if repeats != 1 else "") + ")",
        "",
    ]
    if report.get("incomplete"):
        lines += [f"⚠ INCOMPLETE — {report['incomplete']}", ""]
    overrides = report.get("role_overrides") or {}
    if overrides:
        lines.append(
            "Role overrides (this run only, never written): "
            + ", ".join(f"{role}=`{model}`"
                        for role, model in sorted(overrides.items())))
        lines.append("")
    for item in report.get("items") or []:
        lines.append(
            f"- {item['slot']}: {item.get('clean', 0)}/"
            f"{item.get('judged', 0)} judged"
            + (f", {item.get('errored', 0)} errored"
               if item.get("errored") else "")
            + " clean merges (from "
            f"{item.get('source_task')}, PR #{item.get('source_pr')})")
        for record in item.get("repeats") or []:
            note = record.get("hidden_note")
            tail = f" — {note}" if note else ""
            lines.append(
                f"  [r{record.get('repeat')}] hidden tests "
                f"{'pass' if record.get('hidden_pass') else 'FAIL'}; "
                f"{record.get('factory_outcome')}; {record.get('rounds', 0)} "
                f"round(s), {record.get('turns', 0)} implementer turn(s), "
                f"{_fmt_cost(record.get('cost_usd'))}{tail}")
            if record.get("escalation_note"):
                lines.append(f"      note: {record['escalation_note']}")
    totals = report.get("totals") or {}
    lines += ["", "## Totals", ""]
    lines.append(
        f"- clean merges: {totals.get('clean', 0)} of "
        f"{totals.get('judged', 0)} judged replays"
        + (f" ({totals.get('errored', 0)} errored)"
           if totals.get("errored") else ""))
    lines.append(
        f"- total cost: {_fmt_cost(totals.get('cost_usd'))} "
        f"({totals.get('input_tokens', 0)} in / "
        f"{totals.get('output_tokens', 0)} out tokens, "
        f"{totals.get('wall_sec', 0)}s wall)")
    yield_value = report.get("yield_clean_per_usd")
    if yield_value is None:
        yield_text = "n/a (no cost reported)"
    else:
        yield_text = f"{yield_value:.4f} clean merges per USD"
    lines.append(f"- yield: {yield_text}")
    per_clean = report.get("cost_per_clean_usd")
    if per_clean is None:
        per_clean_text = "n/a (0 clean merges)"
    else:
        per_clean_text = f"${per_clean:.2f}"
    lines.append(f"- cost per clean merge: {per_clean_text}")
    roles = report.get("roles") or {}
    if roles:
        lines += ["", "## Models that ran, per role", ""]
        for role in sorted(roles):
            lines.append(f"- {role}: {', '.join(sorted(roles[role]))}")
    against = report.get("against")
    if against:
        lines += ["", f"## Against {against.get('path')}", ""]
        regressions = against.get("regressions") or []
        lines.append(
            "Regressions (pass → fail): "
            + (", ".join(regressions) if regressions else "none"))
        improvements = against.get("improvements") or []
        if improvements:
            lines.append(f"Improvements (fail → pass): "
                         f"{', '.join(improvements)}")
        error_changes = against.get("error_changes") or []
        if error_changes:
            lines.append(f"Errored↔judged changes (not regressions): "
                         f"{', '.join(error_changes)}")
        unchanged = against.get("unchanged") or []
        if unchanged:
            lines.append(f"Unchanged: {', '.join(unchanged)}")
        if not regressions:
            lines.append(
                "No item regressed: the swap's evidence is the yield and "
                "cost lines above.")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------- AC-5 doctor --


def latest_bench_report(factory_root: Path | str | None) -> "dict | None":
    """The newest bench report under ``<factory_root>/bench/*/reports/``
    (by the stamp in its file name), or None when no report exists."""
    root = bench_dir(factory_root)
    if not root.is_dir():
        return None
    best: "tuple[str, Path] | None" = None
    for path in sorted(root.glob("*/reports/bench-*.json")):
        match = re.search(r"bench-(\d{8}T\d{6}Z)", path.name)
        if not match:
            continue
        if best is None or match.group(1) > best[0]:
            best = (match.group(1), path)
    if best is None:
        return None
    try:
        report = json.loads(best[1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def bench_swap_note(factory_root: Path | str | None) -> "str | None":
    """AC-5's doctor note: the registry's implementer model differs from
    the one the latest bench report ran. Informational (the swap rule
    is the owner's to follow); names both models and the command."""
    from factory.models import (
        DEFAULT_REGISTRY,
        DEFAULT_STACK_NAME,
        expand_alias,
    )

    report = latest_bench_report(factory_root)
    if report is None:
        return None
    ran = (report.get("roles") or {}).get("implementer") or []
    if not ran:
        return None
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    try:
        config = load_config(root / CONFIG_FILE)
        registry = getattr(config, "models", None) or DEFAULT_REGISTRY
    except Exception:  # noqa: BLE001 — doctor reports, never crashes
        registry = DEFAULT_REGISTRY
    stack = registry.stacks.get(DEFAULT_STACK_NAME)
    if stack is None or "implementer" not in stack.roles:
        return None
    try:
        current = expand_alias(registry,
                               stack.roles["implementer"].model)
    except Exception:  # noqa: BLE001 — a broken registry has its own note
        return None
    if current in ran:
        return None
    return (
        f"the registry's implementer model is {current}, but the latest "
        f"bench report ({report.get('stamp', '?')}, project "
        f"{report.get('project', '?')}) ran {', '.join(ran)} — an "
        "implementer model change is preceded by a bench run with the new "
        "model against the current baseline (F-104): run `factory bench "
        "run <project> --role implementer=<new model> --against <latest "
        "report>` and read the diff before approving the registry edit"
    )
