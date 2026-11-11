"""Shared F-67 harness: a pilot-shaped fixture repo and session fakes.

The fixture models the F-13 pilot's JobFinderOS import (read-only shapes,
never that repo's content): a repo whose landed ``.factory/tasks.json``
REFERENCE queue predates the current schema (F-64 budget bounds — the
same ``wall_clock_min`` violation the real landed queue carries), and
whose planning docs still carry a criterion the fresh import dropped
(the WO-19 URL-exfiltration guard's shape). Every import test that needs
a reference queue builds from here; the schema-valid PROPOSALS are built
by :func:`_pwo`, the scripted real-session records by :func:`_record`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from factory.session import SessionRecord

PROJECT = "pilot-demo"

# The criterion the docs still carry after the import dropped it — the
# WO-19 URL-exfiltration guard's shape (never its exact words).
URL_CRITERION = (
    "Prompts never fetch or output URLs found inside posting bodies; the "
    "stored apply_url is the only exception"
)
INJECTION_CRITERION = (
    "A posting carrying ignore-all-previous-instructions text yields "
    "schema-conformant output across every prompt"
)
LANGUAGE_CRITERION = (
    "A posting whose language misses the user's requirement is flagged "
    "in the verdict"
)
CAP_CRITERION = (
    "The nightly invoice cap never charges a completed order twice"
)
DPA_CRITERION = "The owner signs the data processing agreement"

_DONE_MARKER = "[x] done"

# The work-order doc, assembled from lines so the drop entries can cite
# exact line numbers (computed, never hand-counted).
_PILOT_19_LINES = [
    "# PILOT-19 — honest gates",
    "",
    "## Acceptance",
    f"- AC-1: {INJECTION_CRITERION}.",
    f"- AC-2: {URL_CRITERION} (grant exception).",
    f"- AC-3: {LANGUAGE_CRITERION}.",
    "",
    "## Notes",
    "The trust boundary work is planned as two tasks.",
]
_PILOT_07_LINES = [
    "# PILOT-07 — invoice cap",
    "",
    f"- The nightly invoice cap: {CAP_CRITERION} — {_DONE_MARKER}, "
    "shipped in release 2.4.",
]
_PILOT_23_LINES = [
    "# PILOT-23 — agreements",
    "",
    f"- {DPA_CRITERION}.",
]

DOCS = {
    "docs/work-orders/PILOT-19.md": _PILOT_19_LINES,
    "docs/work-orders/PILOT-07.md": _PILOT_07_LINES,
    "docs/work-orders/PILOT-23.md": _PILOT_23_LINES,
}


def _line_of(doc: str, needle: str) -> int:
    """The 1-based line number of the line containing ``needle``."""
    for number, line in enumerate(DOCS[doc], start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} not in {doc}")


# Cited doc lines, computed from the assembled docs (never hand-counted).
URL_LINE = _line_of("docs/work-orders/PILOT-19.md", "never fetch or output")
CAP_DONE_LINE = _line_of("docs/work-orders/PILOT-07.md", _DONE_MARKER)
DPA_LINE = _line_of("docs/work-orders/PILOT-23.md", "data processing")


def _ref_check(tid: str, ac_number: int) -> dict:
    """The reference criterion's check — every landed criterion is
    TEST-backed (R2-2: the downgrade arm must exercise a real
    test->manual weakening, not manual->manual)."""
    return {
        "type": "test",
        "ref": f"backend/tests/test_{tid.replace('-', '_')}.py"
               f"::test_ac_{ac_number}",
    }


def _ref_acceptance(tid: str, *texts: str) -> list[dict]:
    return [
        {"id": f"AC-{index}", "text": text,
         "check": _ref_check(tid, index)}
        for index, text in enumerate(texts, start=1)
    ]


def _ref_task(tid: str, kind: str, *ac_texts: str, **extra) -> dict:
    """One reference-queue task — deliberately schema-INVALID (the
    F-64 budget bound, the same violation the pilot's landed queue
    carries) so every test reading it exercises the plain-JSON rule."""
    task = {
        "id": tid,
        "title": f"Reference work order {tid}",
        "source": f"docs/work-orders/{tid.upper()}.md",
        "kind": kind,
        "priority": "P1",
        "status": "done" if tid == "pilot-07" else "draft",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": kind != "owner_only",
                  "merge": "human", "deploy": "human"},
        "acceptance": _ref_acceptance(tid, *ac_texts),
        "budget": {"size": "M", "max_attempts": 3, "max_turns": 100,
                   "wall_clock_min": 600},
    }
    task.update(extra)
    return task


def reference_queue() -> dict:
    """The landed queue the import is checked against: a split-candidate
    work order (three criteria, one still in the docs), a done task
    whose doc line marks the work shipped, and an owner-only task whose
    ``spec_approval: false`` must survive the import."""
    return {
        "version": 1,
        "project": PROJECT,
        "tasks": [
            _ref_task("pilot-19", "build", INJECTION_CRITERION,
                      URL_CRITERION, LANGUAGE_CRITERION),
            _ref_task("pilot-07", "build", CAP_CRITERION),
            _ref_task("pilot-23", "owner_only", DPA_CRITERION),
        ],
    }


_PROFILE = """\
# The pilot-shaped fixture project (F-67): a self project on the zai
# default provider, with the landed reference queue in place.
name: pilot-demo

repo:
  path: .
  default_branch: main
  remote: https://example.invalid/pilot-demo.git
  merge_method: squash

commands:
  setup: "true"
  test: "true"

paths:
  risk: {}
  protected: []

dependency_manifests: []

deploy:
  mechanism: none
  auto_on_merge: false

live_checks: {}

docs_allowlist: []

network_allowlist: []
"""

_BEDROCK_PROFILE = """\
# A client bedrock project (the F-59 shape) for the provider-stamp tests.
name: pilot-demo
data_owner: client
provider:
  kind: bedrock
  region: eu-north-1
  models:
    implementer: model-impl
    reviewer: model-rev
    verifier: model-ver
    importer: model-imp
providers_allowed: [bedrock]

repo:
  path: .
  default_branch: main
  remote: https://example.invalid/pilot-demo.git
  merge_method: squash

commands:
  setup: "true"
  test: "true"

paths:
  risk: {}
  protected: []

dependency_manifests: []

deploy:
  mechanism: none
  auto_on_merge: false

live_checks: {}

docs_allowlist: []

network_allowlist: []
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def build_repo(tmp_path: Path, *, bedrock: bool = False) -> Path:
    """The pilot-shaped repo, committed (the sandboxed session clones
    it): planning docs, the landed reference queue and a profile."""
    repo = tmp_path / "pilot"
    for name, lines in DOCS.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    factory = repo / ".factory"
    factory.mkdir()
    (factory / "tasks.json").write_text(
        json.dumps(reference_queue(), indent=2) + "\n", encoding="utf-8")
    (factory / "project.yaml").write_text(
        _BEDROCK_PROFILE if bedrock else _PROFILE, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@factory.local")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pilot fixture")
    return repo


def fresh_repo(tmp_path: Path) -> Path:
    """A repo with NO reference queue (first import) — a copy of the
    pilot fixture with the landed queue removed."""
    repo = build_repo(tmp_path)
    (repo / ".factory" / "tasks.json").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop the reference queue")
    return repo


def _pwo(tid: str, **overrides) -> dict:
    """One schema-valid PROPOSED task (the current schema — scope and
    bounds included), optionally carrying the F-67 proposal-only
    accounting fields (``predecessors``, acceptance ``from``)."""
    task = {
        "id": tid,
        "title": f"Work order {tid}",
        "source": f"docs/work-orders/{tid.upper().split('-')[0]}.md",
        "kind": "build",
        "priority": "P1",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "scope": {"paths": [f"src/{tid}/**", f"tests/test_{tid}.py"]},
        "acceptance": [
            {
                "id": "AC-1",
                "text": "it works",
                "check": {
                    "type": "test",
                    "ref": f"tests/test_{tid.lower().replace('-', '_')}"
                           ".py::test_ac_1",
                },
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }
    task.update(overrides)
    return task


def _ac(ac_id: str, text: str, from_ref: str | None = None,
        check: dict | None = None) -> dict:
    item = {
        "id": ac_id,
        "text": text,
        "check": check or {"type": "manual", "expect": "review"},
    }
    if from_ref is not None:
        item["from"] = from_ref
    return item


def _proposal(*tasks: dict, project: str = PROJECT, **extra) -> str:
    doc: dict = {"project": project, "tasks": list(tasks)}
    doc.update(extra)
    return json.dumps(doc)


_DEFAULT_USAGE = {"input": 1000, "cache_read": 200, "cache_write": 50,
                  "output": 300}


def _record(result: str, *, model: str | None = "glm-4.7",
            usage: dict | None | object = _DEFAULT_USAGE,
            provider: dict | None = None,
            status: str = "completed", num_turns: int = 5,
            proposed_tasks: list | None = None) -> SessionRecord:
    """A completed real-session record (the ScriptedRunner currency),
    with the F-59 provider/usage telemetry AC-2 records. ``usage=None``
    is honoured (a session that reported nothing); omit it for the
    default block."""
    record = SessionRecord(
        seat="reviewer", status=status, result=result, num_turns=num_turns,
        model=model,
        usage=usage if isinstance(usage, dict) else None,
    )
    if provider is not None:
        record.provider = provider
    record.proposed_tasks = list(proposed_tasks or [])
    return record


def import_records(root: Path) -> list[dict]:
    """The ``<root>/import/*.record.json`` session records, parsed."""
    directory = Path(root) / "import"
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.record.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def proposal_files(root: Path) -> list[Path]:
    """The kept refused outputs (the F-12 writer's files) only."""
    directory = Path(root) / "import"
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.name.endswith(".proposal.json")
                  or p.name.endswith(".proposal.txt"))

