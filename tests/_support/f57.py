"""Shared F-57 harness: the prose fixture, scripted sessions, repair fakes.

The prose-import fixture repo (``tests/fixtures/prose-import/``) is test
data shipped in the tree; ``_prose_repo`` copies it into ``tmp_path``,
commits it, and returns the repo path — no test writes into the checkout
(F-51's rule). The scripted-session fakes follow the test_importer_limits
``_patch_session`` pattern: the real-session seam
(``factory.importer.run_session_sandboxed``) is patched with a stateful
callable whose Nth call returns the Nth scripted record, so the offline
tests drive exactly the path a real import takes (session → refusal → the
ONE repair session) without a model, a keychain or srt.
"""

import json
import shutil
import subprocess
from pathlib import Path

from factory.session import SessionRecord

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "prose-import"
# The oversized work order's doc (AC-3/AC-4 measure the split against it).
OVERSIZED_DOC = "docs/offline-mode.md"
OWNER_DOC = "docs/owner-round.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _prose_repo(tmp_path: Path) -> Path:
    """The prose fixture, copied into tmp and committed (a real git repo,
    the way the sandboxed importer session needs it)."""
    repo = tmp_path / "prose-demo"
    shutil.copytree(FIXTURE_DIR, repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@factory.local")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "prose fixture")
    return repo


def _wo(tid: str = "PROSE-1", **overrides) -> dict:
    """One schema-valid proposed task (the shape every broken variant
    starts from)."""
    task = {
        "id": tid,
        "title": f"Work order {tid}",
        "source": "docs/snippet-search.md",
        "kind": "build",
        "priority": "P1",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [
            {
                "id": "AC-1",
                "text": "it works",
                "check": {
                    "type": "test",
                    "ref": f"tests/test_{tid.lower().replace('-', '_')}.py::test_ac_1",
                },
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }
    task.update(overrides)
    return task


def _proposal(*tasks: dict, project: str = "prose-demo") -> str:
    """A session's text answer: one JSON object."""
    return json.dumps({"project": project, "tasks": list(tasks)})


def _broken_wo(tid: str = "PROSE-1") -> dict:
    """The 2026-11-05 broken shapes, all six at once (AC-3): risk as a
    string, boolean gates, ``AC1`` ids, check type ``grep``, size ``L``,
    priority ``P0``."""
    task = _wo(tid)
    task.update({
        "risk": "payments",
        "gates": True,
        "priority": "P0",
        "acceptance": [{
            "id": "AC1",
            "text": "it works",
            "check": {"type": "grep", "ref": "rg -n works docs/"},
        }],
        "budget": {"size": "L", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    })
    return task


def _corrected_tasks() -> list[dict]:
    """The repaired proposal over the prose fixture (AC-3): the plain WO
    fixed, the owner doc as ONE owner_only task, and the oversized
    offline-mode work order SPLIT into two mid-size tasks (the prompt's
    rule: a task too big for size M is split, never sized above the
    enum)."""
    plain = _wo("PROSE-1")
    owner = _wo("PROSE-2", source=OWNER_DOC, kind="owner_only",
                title="Owner round: certificate and provider decision")
    medium = {"size": "M", "max_attempts": 3, "max_turns": 100,
              "wall_clock_min": 90}
    cache = _wo("PROSE-3a", source=OVERSIZED_DOC,
                title="Offline mode: cache layer, sync engine, conflicts",
                budget=medium, depends_on=[])
    surface = _wo(
        "PROSE-3b", source=OVERSIZED_DOC,
        title="Offline mode: interface, migration, telemetry, docs, rehearsal",
        budget=medium, depends_on=["PROSE-3a"])
    return [plain, owner, cache, surface]


def _completed(result: str, *, proposed_tasks: list | None = None,
               num_turns: int = 12) -> SessionRecord:
    """A completed session record carrying ``result`` text (and the
    propose tool's accepted set, the way a real session's record does
    once F-57 wires it)."""
    record = SessionRecord(seat="reviewer", status="completed", result=result,
                           num_turns=num_turns)
    record.proposed_tasks = list(proposed_tasks or [])
    return record


class ScriptedRunner:
    """A stateful fake for ``run_session_sandboxed``: the Nth call returns
    the Nth scripted record. Records every call's prompt, run dir and
    kwargs so tests can assert what the repair actually received."""

    def __init__(self, records: list[SessionRecord]):
        self.records = list(records)
        self.calls: list[dict] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        index = len(self.calls)
        run_dir = Path(run_dir)
        self.calls.append({
            "seat": seat, "prompt": prompt, "run_dir": run_dir,
            # captured AT CALL TIME: the run dir is deleted by cleanup
            # before the test asserts, so a later rglob would be
            # vacuously empty (the F-51 lesson — never a check that is
            # true on its first call).
            "md_files": sorted(
                str(path.relative_to(run_dir))
                for path in run_dir.rglob("*.md")
            ),
            "profile": profile, **kwargs,
        })
        if index >= len(self.records):
            raise AssertionError(
                f"scripted runner called a {index + 1}. time "
                f"(only {len(self.records)} record(s) scripted)"
            )
        return self.records[index]
