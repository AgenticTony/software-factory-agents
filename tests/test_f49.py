"""F-49 acceptance tests — BUILD-ORDER.md, work order F-49.

Contract and importer hardening: the queue's refs must resolve (M-10),
``format: date-time`` must be enforced (m-3), truncation must be announced
(m-4), the fresh-queue write must hold the queue lock (m-6), a
RecursionError-class proposal must keep its session output (m-7), and a
proposal without a ``project`` key must be refused (I-18).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f49.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the F-02 loader and both real schemas, the real tasks.json,
  the real AST resolver (factory.contract), real processes for the
  AC-4 race, the real lock files, the real save-refused path
  (<factory-root>/import/, mode 0600).
* **Stubbed:** the importer session (the F-06 injected-callable pattern)
  and, for the AC-3 budget leg, the sandboxed runner seam
  (``run_session_sandboxed``) over a real git fixture — the
  test_importer_limits pattern.
"""

import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import jsonschema
import pytest

from factory import contract, importer, state
from factory.cli import main
from factory.contract import ContractError, load_profile, load_tasks
from factory.importer import (
    ImportRefused,
    build_import_prompt,
    parse_proposal,
    plan_import,
    read_out_of_scope,
    run_import,
)

REPO = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------- fixtures --


def _profile_yaml() -> str:
    """The smallest profile the project schema accepts (name ``proj``)."""
    return (
        "name: proj\n"
        "repo:\n"
        "  path: .\n"
        "  default_branch: main\n"
        "  remote: https://example.invalid/proj.git\n"
        "commands:\n"
        '  setup: "true"\n'
        '  test: "true"\n'
        "paths:\n"
        "  risk: {}\n"
        "  protected: []\n"
        "deploy:\n"
        "  mechanism: none\n"
        "  auto_on_merge: false\n"
        "live_checks: {}\n"
        "docs_allowlist: []\n"
    )


def _repo(tmp_path: Path, *, docs: int = 2, oos: dict[str, str] | None = None,
          git: bool = False) -> Path:
    """A fixture project: planning docs, a profile, optionally the
    out-of-scope register, optionally committed to git (the budget leg's
    sandboxed-session path clones the repo)."""
    repo = tmp_path / "proj"
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for index in range(docs):
        (docs_dir / f"d{index:03d}.md").write_text(
            f"# Doc {index}\n\n## WO-{index}: thing {index}\n\n"
            f"Acceptance: thing {index} works.\n"
        )
    factory_dir = repo / ".factory"
    factory_dir.mkdir(exist_ok=True)
    (factory_dir / "project.yaml").write_text(_profile_yaml())
    if oos:
        register = factory_dir / "out-of-scope"
        register.mkdir(exist_ok=True)
        for name, text in oos.items():
            (register / name).write_text(text)
    if git:
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "test@factory.local"],
            ["config", "user.name", "factory test"],
            ["add", "-A"],
            ["commit", "-q", "-m", "base"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True,
                           capture_output=True, text=True)
    return repo


def _wo(tid: str = "WO-1", **overrides) -> dict:
    """One proposed task: every field the queue schema requires."""
    task = {
        "id": tid,
        "title": f"Work order {tid}",
        "source": "docs/d000.md",
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
                    "ref": f"tests/test_{tid.lower()}.py::test_ac_1",
                },
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }
    task.update(overrides)
    return task


def _proposal(*tasks: dict, project: str = "proj") -> str:
    """The importer session's output: one JSON object."""
    return json.dumps({"project": project, "tasks": list(tasks)})


def _saved_files(factory_root: Path) -> list[Path]:
    directory = factory_root / "import"
    return sorted(directory.iterdir()) if directory.is_dir() else []


# ----------------------------------------------------------------- AC-1 --
# (M-10) every non-draft acceptance ref resolves to a real test function.


def _queue_ref_failures(repo: Path, doc: dict) -> list[str]:
    """The CI guard's walk (the shape test_ac_1 runs on the real queue):
    every ``check.type: test`` ref on every non-draft task must resolve
    statically; each failure names the task, the AC and the ref."""
    failures: list[str] = []
    for task in doc.get("tasks", []):
        if task.get("status") == "draft":
            continue  # red-first: a draft task's tests don't exist yet
        for ac in task.get("acceptance") or []:
            check = ac.get("check") if isinstance(ac, dict) else None
            if not (isinstance(check, dict) and check.get("type") == "test"):
                continue
            ref = check.get("ref")
            reason = contract.resolve_test_ref(repo, ref)
            if reason is not None:
                failures.append(
                    f"task {task.get('id')} {ac.get('id')} ref {ref!r}: {reason}"
                )
    return failures


_SYNTHETIC_TEST_FILE = '''\
def test_ac_1():
    pass


def make_fixture():
    """A helper, not a test — pytest never collects it."""


if False:
    def test_hidden():
        pass


def outer():
    def test_nested():
        pass


test_none = None


def test_shadowed():
    pass


test_shadowed = None


class TestGroup:
    def test_grouped(self):
        pass


class Helper:  # not Test*: pytest would not collect it
    def test_inside(self):
        pass


class TestWithInit:  # Test* but has a constructor: not collected
    def __init__(self):
        pass

    def test_inside(self):
        pass
'''


def test_ac_1(tmp_path: Path):
    """AC-1 (M-10): every non-draft acceptance ref resolves to a real,
    statically-visible test function — a typo'd ref on a ``ready`` task
    fails the guard, naming the task, the AC and the ref; the same typo on
    a ``draft`` task passes (red-first)."""
    resolve = getattr(contract, "resolve_test_ref", None)
    assert resolve is not None, (
        "M-10: no static ref resolver exists — nothing enforces that the "
        "queue's acceptance refs name real tests before the orchestrator "
        "discovers a broken contract mid-spend"
    )

    # The real queue passes today (CODE-REVIEW M-10 counted 0 dangling
    # non-draft refs; re-counted on this tree below in the ledger).
    real = load_tasks(REPO / ".factory" / "tasks.json")
    assert _queue_ref_failures(REPO, real) == []

    # A synthetic repo with one real test file: every broken shape fails.
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text(_SYNTHETIC_TEST_FILE)
    (tmp_path / "outside.py").write_text("def test_ac_1():\n    pass\n")

    def doc(*entries: tuple[str, str]) -> dict:
        return {"tasks": [
            {"id": tid, "status": status, "acceptance": [
                {"id": "AC-1", "text": "t",
                 "check": {"type": "test", "ref": ref}},
            ]}
            for tid, ref, status in entries
        ]}

    # A typo'd ref on a ready task fails, naming task, AC and ref.
    failures = _queue_ref_failures(repo, doc(
        ("T-READY", "tests/test_ok.py::test_missing", "ready"),
    ))
    assert len(failures) == 1
    assert "T-READY" in failures[0]
    assert "AC-1" in failures[0]
    assert "tests/test_ok.py::test_missing" in failures[0]

    # The same typo on a draft task passes — its tests don't exist yet.
    assert _queue_ref_failures(repo, doc(
        ("T-DRAFT", "tests/test_ok.py::test_missing", "draft"),
    )) == []

    # A function a conditional hides, a nested def, a non-function
    # binding, and a def shadowed by a later assignment all fail —
    # importing the module would see some of them; the AST does not lie.
    for ref in (
        "tests/test_ok.py::test_hidden",
        "tests/test_ok.py::test_nested",
        "tests/test_ok.py::test_none",
        "tests/test_ok.py::test_shadowed",
    ):
        assert contract.resolve_test_ref(repo, ref) is not None, ref

    # The forms that do resolve.
    assert contract.resolve_test_ref(repo, "tests/test_ok.py::test_ac_1") is None
    assert contract.resolve_test_ref(
        repo, "tests/test_ok.py::TestGroup::test_grouped") is None

    # A file outside the repo never resolves.
    outside = str(tmp_path / "outside.py") + "::test_ac_1"
    assert "repo" in contract.resolve_test_ref(repo, outside)


# ----------------------------------------------------------------- AC-2 --
# (m-3) format: date-time is enforced on every schema validation.


def test_ac_2():
    """AC-2 (m-3): every schema validation the factory does rejects a
    malformed ``date-time`` (annotation-only in draft 2020-12 unless a
    format checker is passed), valid RFC 3339 still loads, and the real
    repo files keep loading."""
    # The checker must be REAL in this environment: jsonschema's
    # date-time check silently accepts anything without the optional
    # rfc3339-validator dependency — this leg is the no-op guard.
    assert "date-time" in jsonschema.Draft202012Validator.FORMAT_CHECKER.checkers

    real = load_tasks(REPO / ".factory" / "tasks.json")

    def with_stamp(doc: dict, field: str, value: str, path: tuple) -> dict:
        mutated = json.loads(json.dumps(doc))
        node = mutated
        for key in path[:-1]:
            node = node[key]
        node[path[-1]][field] = value
        return mutated

    # Malformed stamps are refused, naming the field.
    for bad in ("yesterday", "2026-13-01T00:00:00Z", "2026-11-07"):
        doc = json.loads(json.dumps(real))
        doc["updated_at"] = bad
        with pytest.raises(ContractError, match="updated_at"):
            contract.validate_queue(doc, "tasks")
    # attempts' started and approved_at ride the same rule.
    done = next(t for t in real["tasks"] if t["status"] == "done"
                and t.get("attempts"))
    with pytest.raises(ContractError, match="started"):
        doc = with_stamp(real, "started", "not a time",
                         ("tasks", t_index(real, done["id"]), "attempts", 0))
        contract.validate_queue(doc, "tasks")
    approved = next(t for t in real["tasks"] if t.get("approved_at"))
    with pytest.raises(ContractError, match="approved_at"):
        doc = json.loads(json.dumps(real))
        doc["tasks"][t_index(real, approved["id"])]["approved_at"] = "2026-1-1"
        contract.validate_queue(doc, "tasks")

    # Valid RFC 3339 — Z and a numeric offset — is accepted, and the
    # orchestrator's own stamp round-trips.
    for good in ("2026-11-07T00:00:00Z", "2026-11-07T01:00:00+01:00",
                 "2026-11-07T00:00:00.123456Z", state.now_iso()):
        doc = json.loads(json.dumps(real))
        doc["updated_at"] = good
        contract.validate_queue(doc, "tasks")

    # The real files still load through the checker-enforcing validator.
    load_tasks(REPO / ".factory" / "tasks.json")
    load_profile(REPO / ".factory" / "project.yaml")
    for profile in sorted((REPO / "projects").glob("*.project.yaml")):
        load_profile(profile)


def t_index(doc: dict, task_id: str) -> int:
    """The queue position of a task id (test helper for path mutation)."""
    return [t["id"] for t in doc["tasks"]].index(task_id)


# ----------------------------------------------------------------- AC-3 --
# (m-4) the planning-doc listing announces its truncation.


def test_ac_3(tmp_path: Path, monkeypatch):
    """AC-3 (m-4): a planning-doc listing over 200 files ends with
    ``+N more`` (N the exact count dropped), the proposal shown to the
    owner names the truncated docs, exactly 200 shows no ``+0 more``, and
    the budget scales with the 200 the session was told to read."""
    repo = _repo(tmp_path, docs=205)

    # The prompt: 200 listed entries, then the exact +N more.
    prompts: list[str] = []

    def session(prompt: str) -> str:
        prompts.append(prompt)
        return _proposal(_wo("WO-0"))

    result = run_import(repo, session=session)
    prompt = prompts[0]
    listed = [line for line in prompt.splitlines() if line.startswith("- docs/")]
    assert len(listed) == 200, f"expected the cap, got {len(listed)}"
    assert "+5 more" in prompt

    # The owner's surface names the docs that were left out.
    for name in ("docs/d200.md", "docs/d201.md", "docs/d202.md",
                 "docs/d203.md", "docs/d204.md"):
        assert name in result.summary, name

    # Exactly at the cap: no +0 more, no truncation section.
    exact = _repo(tmp_path / "exact", docs=200)
    prompt = build_import_prompt(exact, "docs", read_out_of_scope(exact))
    assert "+0 more" not in prompt
    assert "+0 more" not in run_import(
        exact, session=lambda p: _proposal(_wo("WO-0"))
    ).summary

    # The budget scales with the 200 listed, not the 205 on disk
    # (the scaled_import_budget docstring invariant, pinned at the seam).
    git_repo = _repo(tmp_path / "budget", docs=205, git=True)
    captured: dict = {}

    def fake_session(seat, prompt_text, run_dir, profile, **kwargs):
        captured.update(kwargs)
        from factory.session import SessionRecord
        return SessionRecord(seat="reviewer", status="completed",
                             result=_proposal(_wo("WO-0")))

    monkeypatch.setattr("factory.importer.run_session_sandboxed", fake_session)
    monkeypatch.setattr("factory.importer.seat_token", lambda: "tok")
    run_import(git_repo, factory_root=tmp_path / "froot")
    # At 200+ listed docs the turn count is ceiling-capped either way
    # (TURN_CEILING=200); the wall clock discriminates: 20 + 200/5 = 60
    # for the 200 actually listed, not 61 for the 205 on disk.
    assert captured["max_turns"] == importer.TURN_CEILING
    assert captured["timeout_min"] == pytest.approx(
        importer.scaled_import_budget(200)[1])
    assert captured["timeout_min"] != pytest.approx(
        importer.scaled_import_budget(205)[1])


# ----------------------------------------------------------------- AC-4 --
# (m-6) the fresh-queue write holds the queue lock across check and write.

# B (a real subprocess) applies its own fresh import while A sits in the
# window between the no-queue check and the write (A's validate_queue
# call in the fresh branch, monkeypatched here to run B first).
_RACE_RUNNER = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from factory.importer import apply_import, parse_proposal, plan_import
queue = Path(sys.argv[1])
plan = plan_import(None, parse_proposal(Path(sys.argv[2]).read_text()), [],
                   project="proj")
wrote = apply_import(plan, queue, confirm=lambda: True)
print("WROTE" if wrote else "DECLINED")
"""


def test_ac_4(tmp_path: Path, monkeypatch):
    """AC-4 (m-6): the check "no queue exists" and the write of the fresh
    queue happen under the queue's writer lock. A real subprocess applies
    a fresh import while this process sits in the check→write window:
    exactly one write happens, the loser refuses, and the on-disk queue
    is the winner's byte for byte. A second probe holds the lock file
    itself: the fresh write must block on it, not write past it."""
    repo = _repo(tmp_path)
    queue_path = repo / ".factory" / "tasks.json"

    plan_a = plan_import(None, parse_proposal(_proposal(_wo("WO-A"))), [],
                         project="proj")
    proposal_b = tmp_path / "proposal-b.json"
    proposal_b.write_text(_proposal(_wo("WO-B")))

    bytes_after_b: list[bytes] = []
    real_validate = importer.validate_queue
    fired = threading.Event()

    def racing_validate(doc, origin, *args, **kwargs):
        if origin == str(queue_path) and not fired.is_set():
            fired.set()
            race = subprocess.run(
                [sys.executable, "-c", _RACE_RUNNER, str(queue_path),
                 str(proposal_b)],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONPATH": str(REPO)},
            )
            assert race.returncode == 0, race.stderr
            assert "WROTE" in race.stdout, race.stdout + race.stderr
            bytes_after_b.append(queue_path.read_bytes())
        return real_validate(doc, origin, *args, **kwargs)

    monkeypatch.setattr("factory.importer.validate_queue", racing_validate)

    # The loser (this process, whose plan went stale inside the window)
    # refuses with the existing changed-since message; nothing is
    # overwritten — the queue on disk is B's, byte for byte.
    with pytest.raises(ImportRefused, match="changed since"):
        importer.apply_import(plan_a, queue_path, confirm=lambda: True)
    assert bytes_after_b, "the racing writer never ran"
    assert queue_path.read_bytes() == bytes_after_b[0]
    assert [t["id"] for t in load_tasks(queue_path)["tasks"]] == ["WO-B"]

    # The lock probe: while another process holds <queue>.lock (the lock
    # every TaskQueue write takes), the fresh write must WAIT, not write
    # past it. Today's code writes straight through the held lock.
    queue2 = tmp_path / "repo2" / ".factory" / "tasks.json"
    queue2.parent.mkdir(parents=True)
    plan_c = plan_import(None, parse_proposal(_proposal(_wo("WO-C"))), [],
                         project="proj")
    started = threading.Event()
    entered = threading.Event()

    def started_validate(doc, origin, *args, **kwargs):
        if origin == str(queue2):
            started.set()
        return real_validate(doc, origin, *args, **kwargs)

    monkeypatch.setattr("factory.importer.validate_queue", started_validate)

    lock_fd = os.open(queue2.with_name(queue2.name + ".lock"),
                      os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        real_write = importer._atomic_write_json

        def watched_write(path, doc):
            entered.set()
            return real_write(path, doc)

        monkeypatch.setattr("factory.importer._atomic_write_json", watched_write)
        outcome: list = []

        def apply_c():
            outcome.append(
                importer.apply_import(plan_c, queue2, confirm=lambda: True))

        thread = threading.Thread(target=apply_c)
        thread.start()
        assert started.wait(timeout=5.0), "the fresh write never started"
        # Past its validation and inside the window, the write MUST be
        # stuck on the lock: with the lock held there is no path to
        # _atomic_write_json (today there is — the m-6 defect).
        assert not entered.wait(timeout=0.5), (
            "the fresh-queue write reached the disk write while another "
            "writer held the queue lock (m-6: the write does not take it)"
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "the fresh write never finished"
    assert outcome == [True]
    assert [t["id"] for t in load_tasks(queue2)["tasks"]] == ["WO-C"]


# ----------------------------------------------------------------- AC-5 --
# (m-7) RecursionError keeps the output: no crash past the save path.


def test_ac_5(tmp_path: Path, capsys):
    """AC-5 (m-7): any failure to parse or validate the session output —
    RecursionError from deeply nested JSON, ValueError from a huge
    integer literal — takes the keep-refused path: the expensive output
    is saved (byte-identical, 0600), the CLI exits with a message, and
    normal refusals are unchanged."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"

    deep_list = ('{"project": "proj", "tasks": [{"id": "WO-1", "title": '
                 + "[" * 100_000 + "]" * 100_000 + "}]}")
    deep_object = ('{"project": "proj", "tasks": [{"id": "WO-1", "title": '
                   + '{"a": ' * 100_000 + "1" + "}" * 100_000 + "}]}")
    huge_int = '{"project": "proj", "tasks": [], "note": ' + "9" * 5_000 + "}"

    for name, text in (("deep list", deep_list),
                       ("deep object", deep_object),
                       ("huge integer", huge_int)):
        with pytest.raises(ImportRefused, match=name.split()[0] if "deep" in name
                           else "integer"):
            run_import(repo, session=lambda prompt, t=text: t,
                       keep_refused=True, factory_root=root)
        saved = _saved_files(root)
        assert len(saved) == 1, name
        assert saved[0].read_text(encoding="utf-8") == text, name
        assert saved[0].stat().st_mode & 0o777 == 0o600, name
        for path in saved:
            path.unlink()

    # Nesting that PARSES (below the interpreter's recursion limit) is
    # still bounded at the parse boundary — a named refusal, never a
    # RecursionError from a downstream consumer (scrub, schema, dumps).
    parses_deep = ('{"project": "proj", "tasks": [{"id": "WO-1", "title": '
                   + "[" * 700 + "]" * 700 + "}]}")
    with pytest.raises(ImportRefused, match="nest"):
        run_import(repo, session=lambda prompt: parses_deep,
                   keep_refused=True, factory_root=root)
    saved = _saved_files(root)
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == parses_deep
    saved[0].unlink()

    # A normal refusal is unchanged: refused, saved, .json, every
    # violation still listed.
    with pytest.raises(ImportRefused, match="P0"):
        run_import(repo, session=lambda p: _proposal(_wo("WO-1",
                                                         priority="P0")),
                   keep_refused=True, factory_root=root)
    saved = _saved_files(root)
    assert len(saved) == 1 and saved[0].suffix == ".json"

    # The CLI exits with a message, not a traceback.
    deep_file = tmp_path / "deep.txt"
    deep_file.write_text(deep_list)
    rc = main(["import", "--repo", str(repo), "--proposal", str(deep_file)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "importer" in err.lower()


# ----------------------------------------------------------------- AC-6 --
# (I-18) a proposal without a project key is refused.


def test_ac_6(tmp_path: Path):
    """AC-6 (I-18): a proposal without a ``project`` key — or with one
    that is not a non-empty string — is refused before planning; the
    right project is accepted and another project keeps the existing
    mismatch refusal."""
    repo = _repo(tmp_path)
    # No project key at all: today plan_import skips the check and the
    # queue is planned (and would be written) under the profile's name.
    with pytest.raises(ImportRefused, match="project"):
        plan_import(None, {"tasks": [_wo("WO-1")]}, [], project="proj")
    assert not (repo / ".factory" / "tasks.json").exists()

    # Not a non-empty string: refused by the same one schema check.
    for bad in ("", None, 123):
        proposal = {"tasks": [_wo("WO-1")]}
        if bad is not None:
            proposal["project"] = bad
        with pytest.raises(ImportRefused, match="project"):
            plan_import(None, proposal, [], project="proj")

    # The right project is accepted; another name keeps the existing
    # mismatch refusal.
    plan_import(None, parse_proposal(_proposal(_wo("WO-1"))), [],
                project="proj")
    with pytest.raises(ImportRefused, match="other"):
        plan_import(None, parse_proposal(_proposal(_wo("WO-1"),
                                                   project="other")),
                    [], project="proj")

    # Through run_import (the CLI path): a session that forgot the key
    # is refused and nothing is written.
    with pytest.raises(ImportRefused, match="project"):
        run_import(repo, session=lambda p: json.dumps({"tasks": [_wo("WO-1")]}),
                   confirm=lambda s: True)
    assert not (repo / ".factory" / "tasks.json").exists()


# ------------------------------------------------------ hostile pass --
# Refs: shapes a hostile or clumsy queue can carry.


def _synthetic_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text(_SYNTHETIC_TEST_FILE)
    return repo


def test_review_resolver_requires_collectability(tmp_path: Path):
    """Review round 1, finding 2: the resolver proves a function PYTEST
    WOULD COLLECT AS A TEST, not merely "a top-level function" — a
    production function in a production module, a helper in a test file,
    a non-Test class and a Test* class with __init__ are all real
    functions pytest never runs, and a ready-task ref to any of them is
    exactly M-10's mid-spend failure."""
    repo = _synthetic_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "thing.py").write_text(
        "def test_helper():\n    pass\n\ndef resolve_stuff():\n    pass\n")
    resolve = contract.resolve_test_ref

    # The reviewer's probe shape: a real production function in a real
    # production file — refused (file naming first, and it is outside
    # any test tree).
    assert resolve(repo, "src/thing.py::resolve_stuff") is not None
    assert resolve(repo, "src/thing.py::test_helper") is not None
    # A helper in a proper test file: real top-level function, never
    # collected.
    assert "python_functions" in resolve(repo, "tests/test_ok.py::make_fixture")
    # Classes: wrong prefix, and a constructor.
    assert "python_classes" in resolve(
        repo, "tests/test_ok.py::Helper::test_inside")
    assert "__init__" in resolve(
        repo, "tests/test_ok.py::TestWithInit::test_inside")
    # A method of a collected Test* class still resolves.
    assert resolve(repo, "tests/test_ok.py::TestGroup::test_grouped") is None

    # The repo's pytest config is honoured: a custom python_functions
    # pattern collects check_* and refuses test_*; a custom testpaths
    # restricts placement.
    configured = tmp_path / "configured"
    (configured / "tests").mkdir(parents=True)
    (configured / "tests" / "test_ok.py").write_text(_SYNTHETIC_TEST_FILE)
    (configured / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npython_functions = ['check_*']\n")
    assert resolve(configured, "tests/test_ok.py::check_ac_1") is not None
    assert resolve(configured, "tests/test_ok.py::test_ac_1") is not None
    assert resolve(configured, "tests/test_ok.py::make_fixture") is not None

    treed = tmp_path / "treed"
    (treed / "tests").mkdir(parents=True)
    (treed / "suite").mkdir(parents=True)
    (treed / "tests" / "test_ok.py").write_text(_SYNTHETIC_TEST_FILE)
    (treed / "suite" / "test_ok.py").write_text(_SYNTHETIC_TEST_FILE)
    (treed / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["suite"]\n')
    assert resolve(treed, "suite/test_ok.py::test_ac_1") is None
    assert "testpaths" in resolve(treed, "tests/test_ok.py::test_ac_1")

    # A pytest config the reader cannot honour fails closed.
    broken = tmp_path / "broken"
    (broken / "tests").mkdir(parents=True)
    (broken / "tests" / "test_ok.py").write_text(_SYNTHETIC_TEST_FILE)
    (broken / "pytest.ini").write_text("just some prose\nwith no delimiter\n")
    reason = resolve(broken, "tests/test_ok.py::test_ac_1")
    assert reason is not None and "cannot be read" in reason

    # The real repo's own walk still passes (testpaths=["tests"],
    # default patterns — every queue ref matches).
    assert _queue_ref_failures(REPO, load_tasks(REPO / ".factory" / "tasks.json")) == []


def test_review_one_listing_across_prompt_budget_and_summary(tmp_path: Path):
    """Review round 1, finding 3: the prompt, the budget and the summary
    describe the SAME listing — computed once, before the session. A doc
    added while the session runs must not change the capped set the
    summary names."""
    repo = _repo(tmp_path, docs=205)

    def session_adding_a_doc(prompt: str) -> str:
        # The docs tree changes DURING the session.
        (repo / "docs" / "zz-late.md").write_text("# late\n")
        return _proposal(_wo("WO-0"))

    result = run_import(repo, session=session_adding_a_doc)
    assert "zz-late.md" not in result.summary
    assert "docs/d204.md" in result.summary  # the original dropped five
    assert "5 more" in result.summary
    for name in ("docs/d200.md", "docs/d204.md"):
        assert name in result.summary


def test_review_ref_hostile_shapes(tmp_path: Path):
    """Refs: padded names, unicode look-alikes, empty parts, backslashes,
    four parts, non-UTF-8 and unparsable files, a symlink out — each
    refused with a reason, never a crash."""
    repo = _synthetic_repo(tmp_path)
    resolve = contract.resolve_test_ref

    # Padded name and path: the ref names a DIFFERENT node id than the
    # function — L1 would pass the padded name to the runner.
    assert resolve(repo, "tests/test_ok.py::test_ac_1 ") is not None
    assert resolve(repo, "tests/test_ok.py ::test_ac_1") is None  # stripped path
    # Unicode look-alike (Cyrillic е): a different name, not the function.
    assert resolve(repo, "tests/test_ok.py::tеst_ac_1") is not None
    # Empty parts and no separator.
    assert resolve(repo, "tests/test_ok.py::") is not None
    assert resolve(repo, "::test_ac_1") is not None
    assert resolve(repo, "tests/test_ok.py") is not None
    # Four parts (path::Class::name::extra): not a node id this queue knows.
    assert resolve(repo, "tests/test_ok.py::TestGroup::test_grouped::x") is not None
    # A Windows-style path names no file on this tree.
    assert resolve(repo, "tests\\test_ok.py::test_ac_1") is not None
    # A param-id ref fail-closes (F-46 AC-5 amendment): the param cannot
    # be proven to exist statically.
    assert resolve(repo, "tests/test_ok.py::test_ac_1[param]") is not None

    # A non-UTF-8 and an unparsable test file both refuse, never crash.
    (repo / "tests" / "test_binary.py").write_bytes(b"def \xff\xfe():\n    pass\n")
    assert resolve(repo, "tests/test_binary.py::test_ac_1") is not None
    (repo / "tests" / "test_broken.py").write_text("def (oops\n")
    assert resolve(repo, "tests/test_broken.py::test_ac_1") is not None

    # A symlinked file that escapes the repo does not resolve, even
    # though the link itself lives inside it.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret_test.py").write_text("def test_ac_1():\n    pass\n")
    link = repo / "tests" / "test_escaped.py"
    link.symlink_to(outside / "secret_test.py")
    reason = resolve(repo, "tests/test_escaped.py::test_ac_1")
    assert reason is not None and "outside" in reason

    # A relative escape through ".." and an absolute path both refuse.
    assert resolve(repo, "../outside/secret_test.py::test_ac_1") is not None
    assert resolve(repo, str(outside / "secret_test.py") + "::test_ac_1") is not None


def test_review_timestamp_hostile_shapes():
    """Timestamps: the checker's exact edges, pinned — fractional seconds
    and leap-second/Lowercase forms per RFC 3339, absurd offsets and a
    space separator refused."""
    real = load_tasks(REPO / ".factory" / "tasks.json")

    def check(stamp: str) -> Exception | None:
        doc = json.loads(json.dumps(real))
        doc["updated_at"] = stamp
        try:
            contract.validate_queue(doc, "tasks")
            return None
        except ContractError as exc:
            return exc

    # Refused: an offset over its range, a space for the T separator,
    # hour 24, an unpadded date — and a leap second: the checker (via
    # datetime) is stricter than the RFC here, which is the fail-closed
    # side and the side now_iso() never emits (spec note).
    for bad in ("2026-11-07T00:00:00+24:00", "2026-11-07 00:00:00Z",
                "2026-11-07T24:00:00Z", "2026-1-07T00:00:00Z",
                "2016-12-31T23:59:60Z"):
        assert check(bad) is not None, bad
    # Accepted (RFC 3339 allows these): fractional seconds, lowercase
    # t/z, a negative offset.
    for good in ("2026-11-07T00:00:00.5Z", "2026-11-07t00:00:00z",
                 "2026-11-07T00:00:00-05:00"):
        assert check(good) is None, str(check(good))


def test_review_doc_listing_edges(tmp_path: Path):
    """The doc listing's own edges, pinned: a markdown symlink that
    escapes the repo is still listed (containment is the clone's and the
    sandbox read boundary's job — F-47 — where its target does not
    exist; classified in the ledger), and a non-UTF-8 file name never
    crashes the prompt build."""
    repo = tmp_path / "proj"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "plan.md").write_text("# P\n")
    (tmp_path / "lure.md").write_text("# lure\n")
    (docs / "lure.md").symlink_to(tmp_path / "lure.md")
    prompt = build_import_prompt(repo, "docs", [])
    assert "docs/plan.md" in prompt
    assert "docs/lure.md" in prompt  # pinned: listed; see the ledger
    # A file whose name is not decodable UTF-8 must not crash the build.
    # macOS refuses to CREATE such a name (Errno 92 — the probe runs its
    # full course on Linux CI); an astral-plane name stands in here.
    weird = docs / os.fsdecode(b"bad\xff.md")
    try:
        weird.write_text("# x\n")
    except OSError:
        weird = docs / "plan-\U0001f9ea.md"
        weird.write_text("# x\n")
    prompt = build_import_prompt(repo, "docs", [])
    assert "docs/plan.md" in prompt
    assert weird.name in prompt


def test_review_race_stale_lock_and_directory_queue(tmp_path: Path):
    """The race edges: a lock file left behind by a crashed process does
    not block the next writer (the OS released the flock when the holder
    died), and a queue path that is a directory refuses with OSError,
    never a traceback."""
    repo = _repo(tmp_path)
    queue_path = repo / ".factory" / "tasks.json"

    # A subprocess takes the writer lock and dies without releasing it.
    crasher = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         f"fd = os.open({str(queue_path) + '.lock'!r}, os.O_CREAT | os.O_WRONLY)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(30)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert crasher.stdout.readline().strip() == "held"
    finally:
        crasher.kill()
        crasher.wait(timeout=5)
    # The stale lock FILE never blocks: the flock died with the process.
    plan = plan_import(None, parse_proposal(_proposal(_wo("WO-1"))), [],
                       project="proj")
    assert importer.apply_import(plan, queue_path, confirm=lambda: True) is True

    # A queue path occupied by a directory: the fresh write refuses with
    # OSError (the CLI's exit-1 path), never a traceback.
    dir_queue = tmp_path / "dir" / ".factory" / "tasks.json"
    dir_queue.parent.mkdir(parents=True)
    dir_queue.mkdir()
    plan = plan_import(None, parse_proposal(_proposal(_wo("WO-2"))), [],
                       project="proj")
    with pytest.raises(OSError):
        importer.apply_import(plan, dir_queue, confirm=lambda: True)


def test_review_parse_hostile_shapes(tmp_path: Path):
    """Parsing: a deep fence, a BOM, two objects back-to-back and
    NaN/Infinity literals — each a named refusal or a schema refusal,
    never a crash past the save path."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"

    cases = {
        "fenced deep": "```json\n" + "[" * 100_000 + "]" * 100_000 + "\n```",
        "BOM": "﻿" + _proposal(_wo("WO-1")),
        "two objects": _proposal(_wo("WO-1")) + _proposal(_wo("WO-2")),
        "NaN project": '{"project": NaN, "tasks": []}',
        "Infinity": '{"project": "proj", "tasks": [], "note": Infinity}',
    }
    for name, text in cases.items():
        with pytest.raises((ImportRefused, ContractError)):
            run_import(repo, session=lambda p, t=text: t,
                       keep_refused=True, factory_root=root)
        for path in _saved_files(root):
            path.unlink()

    # parse_proposal's own boundary stays honest for the plain shapes;
    # NaN is not one (json.loads accepts it by default — the SCHEMA
    # refuses it downstream, ledger note).
    with pytest.raises(ImportRefused):
        parse_proposal(cases["two objects"])


def test_review_project_exactness(tmp_path: Path):
    """Project names: whitespace and case variants keep the exact-match
    refusal — ' proj' is not 'proj', and neither is 'Proj'."""
    for near in (" proj", "Proj"):
        with pytest.raises(ImportRefused, match="names project"):
            plan_import(None, parse_proposal(_proposal(_wo("WO-1"),
                                                       project=near)),
                        [], project="proj")


def test_review_proposal_extra_keys_refused(tmp_path: Path):
    """Extra keys — top level or per task — are shape errors the one
    schema check names; a typo'd spec field can no longer be silently
    dropped by the merge."""
    with pytest.raises(ImportRefused, match="notes"):
        plan_import(None, {"project": "proj", "notes": "hi",
                           "tasks": [_wo("WO-1")]}, [], project="proj")
    with pytest.raises(ImportRefused, match="dependson"):
        plan_import(None, {"project": "proj", "tasks": [
            _wo("WO-1", dependson=["WO-2"])]}, [], project="proj")


def test_review_fresh_write_never_overwrites_an_appeared_queue(
        tmp_path: Path):
    """The locked write composes with the existing staleness guard: a
    plan built fresh whose queue was created while the confirm waited
    still refuses (the #61-finding-3 arm, now under the lock)."""
    repo = _repo(tmp_path)
    queue_path = repo / ".factory" / "tasks.json"
    planned = run_import(repo, session=lambda p: _proposal(_wo("WO-A")))
    other = run_import(repo, session=lambda p: _proposal(_wo("WO-B")),
                       confirm=lambda s: True)
    with pytest.raises(ImportRefused, match="changed since"):
        importer.apply_import(planned.plan, queue_path, confirm=lambda: True)
    assert [t["id"] for t in load_tasks(other.queue_path)["tasks"]] == ["WO-B"]


def test_review_real_queue_counts():
    """The counts the ledger quotes (M-10 re-count on this tree): every
    dangling ref is on a draft task; none on a non-draft task."""
    doc = load_tasks(REPO / ".factory" / "tasks.json")
    total = draft_dangling = non_draft_dangling = resolved = 0
    for task in doc["tasks"]:
        for ac in task.get("acceptance") or []:
            check = ac.get("check") if isinstance(ac, dict) else None
            if not (isinstance(check, dict) and check.get("type") == "test"):
                continue
            total += 1
            reason = contract.resolve_test_ref(REPO, check.get("ref"))
            if reason is None:
                resolved += 1
            elif task.get("status") == "draft":
                draft_dangling += 1
            else:
                non_draft_dangling += 1
    assert non_draft_dangling == 0
    assert total == resolved + draft_dangling
    assert draft_dangling > 0  # the red-first drafts, as designed
