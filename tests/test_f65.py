"""F-65 — Declared file scope (contract tests, red-first).

Four acceptance criteria (BUILD-ORDER §F-65, as amended in #120):

- AC-1 contracts declare their file scope (schema, semantics, hash
  coverage, approval + run refusals, render, importer);
- AC-2 L0 fails changes outside the declared scope (every change
  class, real git);
- AC-3 the implementer asks instead of drifting (prompt, request
  transport + escalation, repeat escalation, owner widen path);
- AC-4 generated files only by owner-approved profile list.

Every git behaviour here is real git in a temp repo; the orchestrator
scenarios drive ``run_once`` with scripted seats (tests/_support/f23).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from _support.f23 import REF, TEST_SOURCE, Env, Scripted, _git

from factory.approvals import ApprovalLedger, require_profile_approval
from factory.approve import (
    ApprovalError,
    approve_task,
    recover_task,
    render_contract,
)
from factory.contract import (
    CONTRACT_HASH_EXCLUDED_FIELDS,
    ContractError,
    contract_hash,
    load_profile,
    load_tasks,
)
from factory.gates import diff_file_changes, scope_matches
from factory.l0 import check_diff_policy
from factory.progress import ProgressLog
from factory.prompts import assemble_implementer_prompt, prompt_hash
from factory.session import SessionRecord, parse_scope_requests
from factory.state import TaskQueue, attempt_budget_used

# The implementer prompt hash on the pre-F-65 tree (e9cccb7): AC-3 bumps
# the prompt, so the shipped hash must differ from this.
PRE_F65_IMPLEMENTER_HASH = (
    "4855e4ba3ed3a9af252690e6913ffceefec5ba2582dc1496c7c08bf1185c6538"
)

# What the scripted implementers in these scenarios write (f23.Scripted
# writes impl.py + tests/test_feature.py; the drift scenarios add
# evil.py deliberately out of scope).
IN_SCOPE = ["impl.py", "tests/test_feature.py"]
SCOPE = {"paths": IN_SCOPE}
SCOPE_WITH_EVIL = {"paths": [*IN_SCOPE, "evil.py"]}


def _task(task_id: str = "T-1", *, status: str = "ready", scope=None,
          **overrides) -> dict:
    """A fixture task: build kind, ready (or overridden), with a scope
    unless the scenario explicitly passes ``scope`` (None means the
    scope-less refusals are what is under test)."""
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42",
         "check": {"type": "test", "ref": REF}}
    ]
    task = {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": status,
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": _acceptance_hash(acceptance),
        "approved_at": "2026-11-10T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
        "attempts": [],
    }
    if scope is not None:
        task["scope"] = scope
    task.update(overrides)
    return task


def _acceptance_hash(acceptance) -> str:
    import hashlib

    canonical = json.dumps(acceptance, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _queue(tmp_path: Path, tasks: list, project: str = "fixture") -> Path:
    """Write a tasks.json queue and return its path."""
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(
        {"version": 1, "project": project, "tasks": tasks}, indent=2) + "\n")
    return path


# ------------------------------------------------------------- real git ---

def _git_repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """A fixture git repo with one base commit on main."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _commit_change(repo: Path, message: str = "change") -> None:
    """Commit the working tree's current state on a feature branch."""
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _branch_commit(repo: Path, message: str) -> None:
    """Create the feature branch, then commit the working tree."""
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


BASE_FILES = {
    "src/keep.py": "KEEP = 1\n",
    "docs/readme.md": "# readme\n",
    "other/file.txt": "other\n",
    "tests/test_base.py": "def test_base():\n    assert True\n",
}
TASK_SCOPE = {"paths": ["src/**", "tests/test_feature.py"]}
MINIMAL_PROFILE = {
    "name": "fixture",
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
}


def _out_of_scope_findings(repo: Path, task: dict, profile=None):
    """Real-git diff of main...feature through the L0 diff policy."""
    changes = diff_file_changes(repo, "main", "feature")
    findings, *_ = check_diff_policy(
        profile or dict(MINIMAL_PROFILE), changes, task
    )
    return [f for f in findings if f.id == "out_of_scope"], findings


# ------------------------------------------------------------------ AC-1 ---

def test_ac_1():
    """Contracts declare their file scope."""
    # --- the matcher: *, ? never cross /; ** spans directories;
    # case-sensitive on the exact string.
    assert scope_matches("a/b/c.py", ["a/*.py"]) is False  # * stops at /
    assert scope_matches("a/c.py", ["a/*.py"]) is True
    assert scope_matches("a/b/c.py", ["a/**/*.py"]) is True
    assert scope_matches("a/x.py", ["a/**/*.py"]) is True  # ** is zero dirs
    assert scope_matches("a/b/c/d.py", ["a/**"]) is True
    assert scope_matches("a/bc.py", ["a/b?.py"]) is True
    assert scope_matches("a/b/c.py", ["a/b?.py"]) is False  # ? never crosses /
    assert scope_matches("A/x.py", ["a/**"]) is False  # case-sensitive
    assert scope_matches("src/x.py", ["src/x.py"]) is True  # literal

    # --- refused shapes refuse at load, naming the task and the pattern.
    bad_patterns = [
        "",            # empty
        "/abs/x.py",   # absolute
        "../escape.py",  # .. segment
        "a/../../x.py",
        "a\\b.py",      # backslash
        "**",           # catch-all and equivalents…
        "*",
        "**/*",
        "*/**",
        "**/**",
        "*/*",
        # R1-1: `?` is a wildcard too — a pattern with NO literal
        # character matches every path however spelled. Each of these
        # matches every path (proven against real paths in review).
        "?*/**",
        "**/?*",
        "**/*?*",
        "?*",
        "*?",
        "**/",
        "src//x.py",    # empty segment (double slash)
        "src/",         # trailing slash
        "./src/**",     # . segment
        "src/[ab].py",  # character classes unsupported
    ]
    for pattern in bad_patterns:
        path = _queue_for_pattern(pattern)
        with pytest.raises(ContractError) as excinfo:
            load_tasks(path)
        message = str(excinfo.value)
        assert "T-1" in message, f"{pattern!r}: task not named: {message}"
        if pattern:
            assert pattern in message or repr(pattern) in message, (
                f"{pattern!r}: pattern not named: {message}"
            )

    # A control character in a pattern refuses at load: the F-63
    # control walk covers it (proven) — the refusal names the field.
    ctrl = _queue_for_pattern("src/\x1bx.py")
    with pytest.raises(ContractError) as excinfo:
        load_tasks(ctrl)
    assert "scope" in str(excinfo.value)

    # A pattern with a single literal still LOADS (the structural rule
    # refuses only wildcard-only patterns — R1-1's positive half).
    load_tasks(_queue(_tmp(), [_task(scope={"paths": ["src/**", "a?c/**"]})]))

    # Schema shape: scope.paths must be a non-empty list of strings.
    for scope_shape in ({"paths": []}, {"paths": "src/**"}, {"other": 1}):
        path = _queue_for_shape(scope_shape)
        with pytest.raises(ContractError):
            load_tasks(path)

    # --- scope is inside the whole-contract hash.
    assert "scope" not in CONTRACT_HASH_EXCLUDED_FIELDS
    h1 = contract_hash(_task(scope=SCOPE))
    h2 = contract_hash(_task(scope={"paths": ["other.py"]}))
    assert h1 != h2

    # --- approval refuses a scope-less build task, and a docs task.
    for kind in ("build", "docs"):
        queue_path = _queue(tmp := _tmp(), [_task(status="draft", scope=None,
                                                  kind=kind)])
        queue = TaskQueue(queue_path)
        with pytest.raises(ApprovalError) as excinfo:
            approve_task(queue, "T-1", approved_by="owner",
                         confirm=lambda _: True, echo=lambda _t: None,
                         factory_root=tmp / "root")
        assert "scope" in str(excinfo.value)

    # --- the run itself refuses a scope-less task before any session:
    # build and docs, even when the ledger holds the task's hash.
    for kind in ("build", "docs"):
        seats = Scripted()
        env = Env(tmp := _tmp(), tasks=[_task(scope=None, kind=kind)])
        result = env.run(seats)
        assert result.outcome == "needs_human", (
            f"{kind}: {result.outcome} {result.escalation}"
        )
        assert result.escalation == "scope_missing"
        assert seats.calls == [], "a session started for a scope-less task"
        task = env.task()
        assert task["status"] == "needs_human"
        assert task["attempts"][-1]["escalation"] == "scope_missing"
        assert "T-1" in task["attempts"][-1]["note"]

    # --- Y6: the scope_missing refusal spends no budget — three refusals
    # (one run, then the queue idles; planted here as the attempts a
    # fail-closed period would leave) leave the whole budget unspent.
    spent = _task(scope=None)
    spent["attempts"] = [{
        "run_id": f"r{i}", "slice_id": "T-1",
        "started": "2026-11-10T10:00:00Z", "ended": "2026-11-10T10:00:01Z",
        "outcome": "escalated", "rounds": 0, "turns": 0,
        "input_tokens": 0, "output_tokens": 0, "prompt_versions": {},
        "skills": [], "escalation": "scope_missing", "note": "no scope",
    } for i in range(3)]
    assert attempt_budget_used(spent, "T-1") == 0

    # --- a scope-less owner_only task raises no SCOPE refusal: the
    # existing kind refusal (approving a never-executed kind has no
    # meaning) is the only one.
    queue_path = _queue(_tmp(), [_task(status="draft", scope=None,
                                       kind="owner_only")])
    with pytest.raises(ApprovalError) as excinfo:
        approve_task(TaskQueue(queue_path), "T-1", approved_by="owner",
                     confirm=lambda _: True, echo=lambda _t: None,
                     factory_root=_tmp() / "root")
    assert "scope" not in str(excinfo.value)

    # --- render_contract shows the paths.
    rendered = render_contract(_task(scope=SCOPE))
    assert "impl.py" in rendered and "tests/test_feature.py" in rendered

    # --- a scope_missing queue stops cleanly: the parked task idles the
    # next run — no spin, no session (the loop probe).
    seats = Scripted()
    env = Env(_tmp(), tasks=[_task(scope=None)])
    assert env.run(seats).escalation == "scope_missing"
    second = env.run(seats)
    assert second.outcome == "idle", second.outcome
    assert seats.calls == []

    # --- the importer: a proposal that widens a non-draft task's scope
    # is a CONTRACT change (Y17) — demoted exactly like an acceptance
    # change; and the propose-tool path refuses malformed scope patterns
    # with the task named (Y18 — the tool path, not only the later load).
    from factory.importer import (
        _load_tasks_schema,
        _merge_task,
        _task_violations,
        build_proposal_schema,
    )

    ready = _task(scope=SCOPE)
    proposal = dict(ready)
    proposal["scope"] = SCOPE_WITH_EVIL
    merged, changed, demoted, flagged = _merge_task(ready, proposal)
    assert demoted and merged["status"] == "draft"
    assert "scope" in changed
    # …and an unchanged scope on a ready task demotes nothing.
    merged, changed, demoted, flagged = _merge_task(ready, dict(ready))
    assert not demoted and merged["status"] == "ready"

    schema = build_proposal_schema(_load_tasks_schema())
    base_task = {
        "id": "WO-9", "title": "t", "source": "docs/x.md", "kind": "build",
        "priority": "P2", "depends_on": [], "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [], "budget": {"size": "S", "max_attempts": 3,
                                     "max_turns": 20, "wall_clock_min": 30},
    }
    for bad in (["**"], ["/etc/passwd"]):
        task_v = dict(base_task)
        task_v["scope"] = {"paths": bad}
        scope_violations = [
            v for v in _task_violations(task_v, schema, ()) if "scope" in v
        ]
        assert scope_violations and "WO-9" in scope_violations[0], bad

    # --- the real queues still load (no scopes there — optional field).
    repo_root = Path(__file__).resolve().parent.parent
    load_tasks(repo_root / ".factory" / "tasks.json")
    load_tasks(repo_root / "projects" / "jobfinderos.tasks.json")


def _tmp() -> Path:
    """A fresh directory under pytest's tmp (unique per call)."""
    import tempfile

    counter = getattr(_tmp, "_n", 0)
    _tmp._n = counter + 1  # type: ignore[attr-defined]
    path = Path(tempfile.mkdtemp(prefix=f"f65-{counter}-"))
    return path


def _queue_for_pattern(pattern: str) -> Path:
    paths = [pattern] if pattern else ["src/**", ""]
    return _queue(_tmp(), [_task(scope={"paths": paths})])


def _queue_for_shape(scope_shape) -> Path:
    task = _task()
    task["scope"] = scope_shape
    return _queue(_tmp(), [task])


# ------------------------------------------------------------------ AC-2 ---

def test_ac_2():
    """L0 fails changes outside the declared scope — every change class,
    real git."""
    tmp = _tmp()

    # --- added, out of scope.
    repo = _git_repo(tmp, "added", dict(BASE_FILES))
    (repo / "docs" / "new.py").write_text("x = 1\n")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["docs/new.py"]
    assert "added" in oos[0].text and "docs/new.py" in oos[0].text

    # --- modified, out of scope.
    repo = _git_repo(tmp, "modified", dict(BASE_FILES))
    (repo / "docs" / "readme.md").write_text("# changed\n")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["docs/readme.md"]

    # --- deleted, out of scope.
    repo = _git_repo(tmp, "deleted", dict(BASE_FILES))
    (repo / "other" / "file.txt").unlink()
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["other/file.txt"]
    assert "deleted" in oos[0].text

    # --- type-change (file replaced by symlink), out of scope.
    repo = _git_repo(tmp, "typechange", dict(BASE_FILES))
    (repo / "other" / "file.txt").unlink()
    (repo / "other" / "file.txt").symlink_to("../src/keep.py")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["other/file.txt"]

    # --- mode-only change, out of scope.
    repo = _git_repo(tmp, "modeonly", dict(BASE_FILES))
    os.chmod(repo / "docs" / "readme.md", 0o755)
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["docs/readme.md"]

    # --- submodule pointer added at an out-of-scope path (plus its
    # .gitmodules): both judged.
    sub_src = tmp / "subsrc"
    sub_src.mkdir()
    _git(sub_src, "init", "-b", "main")
    _git(sub_src, "config", "user.email", "fixture@example.com")
    _git(sub_src, "config", "user.name", "Fixture")
    (sub_src / "lib.py").write_text("L = 1\n")
    _git(sub_src, "add", "-A")
    _git(sub_src, "commit", "-m", "base")
    repo = _git_repo(tmp, "submodule", dict(BASE_FILES))
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add",
         str(sub_src), "vendor/lib")
    _branch_commit(repo, "add submodule")
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert "vendor/lib" in [f.file for f in oos]
    assert ".gitmodules" in [f.file for f in oos]

    # --- rename BOTH sides judged: in-scope file renamed OUT of scope…
    repo = _git_repo(tmp, "rename-out", dict(BASE_FILES))
    _git(repo, "mv", "src/keep.py", "docs/keep.py")
    _branch_commit(repo, "rename out")
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["docs/keep.py"]

    # …and an out-of-scope file renamed INTO scope (the old side fails).
    repo = _git_repo(tmp, "rename-in", dict(BASE_FILES))
    _git(repo, "mv", "docs/readme.md", "src/readme.md")
    _branch_commit(repo, "rename in")
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["docs/readme.md"]

    # --- copy destination judged (-C is off: the copy lands as an add).
    repo = _git_repo(tmp, "copy", dict(BASE_FILES))
    (repo / "docs" / "copy.py").write_text("KEEP = 1\n")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["docs/copy.py"]

    # --- a case variant of an in-scope path is out of scope. Real git
    # on this case-insensitive macOS host: when no src/ exists yet, the
    # variant spelling SURVIVES as its own path (probed)…
    repo = _git_repo(tmp, "case", {"keep.txt": "K\n"})
    (repo / "SRC").mkdir()
    (repo / "SRC" / "x.py").write_text("x = 1\n")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["SRC/x.py"]
    # …and a case-only `git mv` reports both sides, the old side being
    # the variant the scope never named (R100 docs/Readme.md →
    # docs/readme.md, probed).
    repo = _git_repo(tmp, "case-rename", {"docs/Readme.md": "# r\n"})
    _git(repo, "mv", "docs/Readme.md", "docs/readme.md")
    _branch_commit(repo, "case rename")
    oos, _ = _out_of_scope_findings(
        repo, _task(scope={"paths": ["docs/readme.md"]})
    )
    assert [f.file for f in oos] == ["docs/Readme.md"]

    # --- an NFD look-alike of an in-scope NFC path is out of scope.
    # Probed on this macOS host: an NFD write over an NFC file collapses
    # at the FS (git records the NFC path, in scope — the attack is
    # absorbed), and core.precomposeunicode=true re-composes even
    # plumbing argv. But a branch built on a normalization-preserving
    # host (Linux; or precompose off) genuinely carries NFD tree paths —
    # planted here the real way, via update-index, so the tree holds the
    # decomposed bytes git itself reports quoted.
    nfc = "src/caf\u00e9.py"  # composed
    nfd = "src/cafe\u0301.py"  # cafe + COMBINING ACUTE (NFD)
    assert nfc != nfd
    repo = _git_repo(tmp, "nfd", {"keep.txt": "K\n"})
    _git(repo, "checkout", "-b", "feature")
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="X = 2\n", capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(repo, "-c", "core.precomposeunicode=false", "update-index",
         "--add", "--cacheinfo", f"100644,{blob},{nfd}")
    _git(repo, "commit", "-m", "nfd tree path")
    oos, _ = _out_of_scope_findings(repo, _task(scope={"paths": [nfc]}))
    assert [f.file for f in oos] == [nfd]

    # --- a symlink is judged by its own path, never its target: out of
    # scope pointing in fails…
    repo = _git_repo(tmp, "symlink", dict(BASE_FILES))
    (repo / "docs" / "link.py").symlink_to("../src/keep.py")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ["docs/link.py"]
    # …and one in scope pointing out does not.
    repo = _git_repo(tmp, "symlink2", dict(BASE_FILES))
    (repo / "src" / "link-out.py").symlink_to("../docs/readme.md")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert oos == []

    # --- a path with a quote or newline in its name is judged on the
    # exact bytes git reports.
    repo = _git_repo(tmp, "weird", dict(BASE_FILES))
    (repo / "docs" / 'we"ird\nname.py').write_text("x = 1\n")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert [f.file for f in oos] == ['docs/we"ird\nname.py']

    # --- an all-in-scope diff passes: no findings at all.
    repo = _git_repo(tmp, "inscope", dict(BASE_FILES))
    (repo / "src" / "keep.py").write_text("KEEP = 2\n")
    (repo / "tests" / "test_feature.py").write_text(TEST_SOURCE)
    _commit_change(repo)
    oos, all_findings = _out_of_scope_findings(repo, _task(scope=TASK_SCOPE))
    assert oos == []
    assert all_findings == []

    # --- the task's own contract test files are allowed even outside
    # scope.paths (they are written red-first by the implementer). The
    # scope here names ONLY src/** so the allowance is what carries the
    # test file (Y1: with the exemption mutated away this must FAIL).
    repo = _git_repo(tmp, "contractfile", dict(BASE_FILES))
    (repo / "tests" / "test_feature.py").write_text(TEST_SOURCE)
    (repo / "src" / "keep.py").write_text("KEEP = 2\n")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope={"paths": ["src/**"]}))
    assert oos == []

    # --- scope never overrides protection: a protected path inside the
    # scope still fails exactly as protected (no out_of_scope)…
    repo = _git_repo(tmp, "protected-in", dict(BASE_FILES))
    (repo / "src" / "keep.py").write_text("KEEP = 2\n")
    _commit_change(repo)
    profile = dict(MINIMAL_PROFILE)
    profile["paths"] = {"risk": {}, "protected": ["src/keep.py"]}
    oos, all_findings = _out_of_scope_findings(
        repo, _task(scope=TASK_SCOPE), profile
    )
    assert oos == []
    assert [f.id for f in all_findings] == ["protected_path_touched"]
    # …and a path that is BOTH protected and out of scope gets BOTH
    # findings.
    repo = _git_repo(tmp, "protected-out", dict(BASE_FILES))
    (repo / "docs" / "readme.md").write_text("# changed\n")
    _commit_change(repo)
    profile = dict(MINIMAL_PROFILE)
    profile["paths"] = {"risk": {}, "protected": ["docs/readme.md"]}
    oos, all_findings = _out_of_scope_findings(
        repo, _task(scope=TASK_SCOPE), profile
    )
    assert [f.id for f in all_findings].count("protected_path_touched") == 1
    assert [f.file for f in oos] == ["docs/readme.md"]

    # --- nothing implicit about tests/: a test file that is not one of
    # the task's own contract test files is out of scope like any other.
    repo = _git_repo(tmp, "othertests", dict(BASE_FILES))
    (repo / "tests" / "other_test.py").write_text(
        "def test_o():\n    assert True\n"
    )
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(
        repo, _task(scope={"paths": ["src/**", "tests/test_feature.py"]})
    )
    assert [f.file for f in oos] == ["tests/other_test.py"]

    # --- no scope declared: the diff policy makes no scope judgement
    # (the run-level scope_missing refusal is that task's gate).
    repo = _git_repo(tmp, "noscope", dict(BASE_FILES))
    (repo / "docs" / "new.py").write_text("x = 1\n")
    _commit_change(repo)
    oos, _ = _out_of_scope_findings(repo, _task(scope=None))
    assert oos == []


# ------------------------------------------------------------------ AC-3 ---

def test_ac_3(monkeypatch):
    """The implementer works inside its scope and asks instead of
    drifting."""
    # --- the prompt: the system prompt carries the request boundary and
    # the hash moved off the pre-F-65 pin.
    system = assemble_implementer_prompt(None)
    assert "request_scope" in system
    assert prompt_hash(system) != PRE_F65_IMPLEMENTER_HASH

    tmp = _tmp()

    # --- a scripted implementer that files a request escalates in
    # round 1, paths and reasons in the note, stored scope unchanged.
    class Requesting:
        def __init__(self, requests):
            self.requests = requests
            self.calls: list = []

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            self.calls.append(seat)
            if seat != "implementer":
                return SessionRecord(
                    seat=seat, status="completed", num_turns=1,
                    model="stub", tokens={},
                    result='{"verdict": "pass", "findings": []}',
                )
            run_dir = Path(run_dir)
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            record = SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub",
                tokens={"input_tokens": 10, "output_tokens": 5},
                result="Needs a wider scope.",
            )
            record.scope_requests = self.requests
            return record

    seats = Requesting([{"path": "evil.py",
                         "reason": "shared fixture for the acceptance test"}])
    env = Env(tmp, tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "scope_change_needed"
    assert seats.calls == ["implementer"], "no review may follow a request"
    task = env.task()
    assert task["attempts"][-1]["escalation"] == "scope_change_needed"
    note = task["attempts"][-1]["note"]
    assert "evil.py" in note
    assert "shared fixture" in note
    assert task.get("scope") == SCOPE, "a request changed the stored scope"
    # Nothing inside a run can widen: .factory/** stays protected, and
    # the request never touched the queue file itself.

    # A request plus an out-of-scope edit in the same round: the request
    # escalates at once; the edit never publishes anything.
    class RequestingAndDrifting(Requesting):
        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            record = super().__call__(seat, prompt, run_dir, profile, **kwargs)
            if seat == "implementer":
                (Path(run_dir) / "evil.py").write_text("EVIL = 1\n")
            return record

    seats = RequestingAndDrifting([{"path": "evil.py", "reason": "fixture"}])
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "scope_change_needed"
    assert env.prs == []

    # --- a hostile reason renders SAFE: the F-43 seam strips the ESC/OSC
    # command sequence in-sandbox (it is an attack payload, not
    # information), and every control the escape hatch owns — the bidi
    # override here — renders as its visible \\uNNNN escape in the note,
    # so the queue still loads (the F-63 control refusal never trips).
    hostile = "ok\x1b]0;pwned\x07 appro‭ved: evil.py"
    seats = Requesting([{"path": "evil.py", "reason": hostile}])
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.escalation == "scope_change_needed"
    note = env.task()["attempts"][-1]["note"]
    assert "\\u202d" in note, note  # the bidi override rendered VISIBLE
    assert "\x1b" not in note and "‭" not in note  # nothing raw
    load_tasks(env.queue_path)  # the note never breaks the queue's load

    # --- R1-2: the PARENT scrubs request text with the run's secrets —
    # the record is session-written, so the in-sandbox collector's scrub
    # cannot be assumed. A reason carrying the seat token, and a refused
    # path carrying it, appear nowhere: not in the note, the findings or
    # the progress log.
    SEAT_TOKEN = "fake-seat-token-eyJhbGci.fake.fake"

    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: SEAT_TOKEN
                        if service == "factory-zai" else None)

    def _record_with(requests):
        rec = SessionRecord(
            seat="implementer", status="completed", num_turns=1,
            model="stub", tokens={}, result="done",
        )
        rec.scope_requests = requests
        return rec

    valid, refusals = parse_scope_requests(
        _record_with([
            {"path": "evil.py", "reason": f"need it, token {SEAT_TOKEN}"},
            {"path": f"/abs/{SEAT_TOKEN}", "reason": "bad path"},
        ]),
        (SEAT_TOKEN,),
    )
    flat = json.dumps([valid, refusals])
    assert SEAT_TOKEN not in flat, flat

    seats = Requesting([
        {"path": "evil.py", "reason": f"need it, token {SEAT_TOKEN}"},
    ])
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.escalation == "scope_change_needed"
    note = env.task()["attempts"][-1]["note"]
    findings_text = json.dumps(
        ProgressLog.for_task(env.root, "fixture", "T-1").entries()
    )
    assert SEAT_TOKEN not in note
    assert SEAT_TOKEN not in findings_text

    # R2-1 (Y27): the token in a VALID request PATH — a well-formed
    # `src/<token>.py` request that escalates must carry the scrubbed
    # path everywhere it lands: note, progress log, findings.
    valid, refusals = parse_scope_requests(
        _record_with([{"path": f"src/{SEAT_TOKEN}.py",
                       "reason": "generated there"}]),
        (SEAT_TOKEN,),
    )
    assert len(valid) == 1 and SEAT_TOKEN not in valid[0]["path"]
    assert "[redacted]" in valid[0]["path"]

    seats = Requesting([{"path": f"src/{SEAT_TOKEN}.py",
                         "reason": "generated there"}])
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.escalation == "scope_change_needed"
    note = env.task()["attempts"][-1]["note"]
    assert SEAT_TOKEN not in note, note
    assert "[redacted]" in note  # scrubbed visible, not silently dropped
    assert SEAT_TOKEN not in json.dumps(
        ProgressLog.for_task(env.root, "fixture", "T-1").entries()
    )
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: None)

    # --- Y15: the parent bound holds — 25 valid requests keep 20, the
    # rest dropped by name.
    many = [{"path": f"d{i}/x.py", "reason": "r"} for i in range(25)]
    valid, refusals = parse_scope_requests(_record_with(many))
    assert len(valid) == 20 and len(refusals) == 5
    assert "past the 20-request bound" in refusals[0]

    # --- Y21: request paths that break the shape rules are DROPPED with
    # a named refusal and never escalate — the round proceeds (an
    # in-scope diff passes and opens the PR, the refusals reported in
    # the task's progress log, where a passing round's findings live).
    invalid_only = [
        {"path": "../x", "reason": "climb"},
        {"path": "/etc/passwd", "reason": "absolute"},
        {"path": "**", "reason": "catch-all"},
    ]

    seats = Requesting(invalid_only)
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.outcome == "pr_opened", result.escalation
    log_text = json.dumps(
        ProgressLog.for_task(env.root, "fixture", "T-1").entries()
    )
    assert log_text.count("scope request ignored") == 3, log_text[:400]

    # --- Y20: the tool grant has ONE source — the implementer seat
    # config. The implementer's options carry it; reviewer and verifier
    # options never do.
    from factory.session import build_options

    grant = "mcp__request_scope__request_scope"
    impl_options = build_options(
        "implementer", Path(_tmp()), 5,
        base_url="https://api.z.ai/api/anthropic", token="t",
    )
    assert grant in list(impl_options.allowed_tools)
    for seat in ("reviewer", "verifier"):
        other = build_options(
            seat, Path(_tmp()), 5,
            base_url="https://api.z.ai/api/anthropic", token="t",
        )
        assert grant not in list(other.allowed_tools)

    # --- unrequested out-of-scope edits: round 1 is a fix round…
    class Drifting:
        """Writes evil.py (out of scope) for the first `drift` rounds,
        then stays in scope — and removes the drift file."""

        def __init__(self, drift_rounds):
            self.drift_rounds = drift_rounds
            self.rounds = 0
            self.calls: list = []

        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            self.calls.append(seat)
            if seat != "implementer":
                return SessionRecord(
                    seat=seat, status="completed", num_turns=1,
                    model="stub", tokens={},
                    result='{"verdict": "pass", "findings": []}',
                )
            run_dir = Path(run_dir)
            self.rounds += 1
            (run_dir / "impl.py").write_text(
                f"def answer():\n    return 42  # r{self.rounds}\n"
            )
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            evil = run_dir / "evil.py"
            if self.rounds <= self.drift_rounds:
                evil.write_text("EVIL = 1\n")
            elif evil.exists():
                evil.unlink()  # the revert
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub",
                tokens={"input_tokens": 10, "output_tokens": 5},
                result="Implemented.",
            )

    seats = Drifting(drift_rounds=10)  # never reverts
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.outcome == "needs_human"
    assert result.escalation == "scope_change_needed"
    attempts = env.task()["attempts"]
    assert len(attempts) <= 2, "escalation must come within 2 rounds"
    assert "evil.py" in attempts[-1]["note"]

    # --- R1-2's sibling (self-found in review): the 2-round repeat note
    # embeds the out-of-scope git PATH — a drift file NAMED after the
    # seat token must not land raw in the durable attempt note.
    class TokenNamedDrift(Drifting):
        def __call__(self, seat, prompt, run_dir, profile, **kwargs):
            record = super().__call__(seat, prompt, run_dir, profile,
                                      **kwargs)
            if seat == "implementer":
                (Path(run_dir) / f"evil-{SEAT_TOKEN}.py").write_text("E\n")
            return record

    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: SEAT_TOKEN
                        if service == "factory-zai" else None)
    seats = TokenNamedDrift(drift_rounds=10)
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    # F-72: the scope escalation keeps the work as a draft PR — and the
    # push fence REFUSES it, because the drift file's NAME carries the
    # seat token (a tree entry in the published range). The refusal is
    # the recorded escalation, the original in the note — scrubbed.
    assert result.escalation == "publish_refused"
    note = env.task()["attempts"][-1]["note"]
    assert "scope_change_needed" in note
    assert SEAT_TOKEN not in note, note
    assert "[redacted]" in note  # scrubbed, not merely omitted
    monkeypatch.setattr("factory.provider._keychain_token",
                        lambda service: None)

    # --- one that reverts in round 2 opens a PR.
    seats = Drifting(drift_rounds=1)
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    result = env.run(seats)
    assert result.outcome == "pr_opened", result.escalation
    assert env.task()["status"] == "in_review"

    # --- the owner widens the scope, records the re-approval the way an
    # owner does (adopt — approve refuses non-draft tasks), recovers:
    # the task is ready and the next run passes L0 with that file.
    from factory.approve import adopt_queue

    seats = Drifting(drift_rounds=10)  # parks on scope_change_needed
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    assert env.run(seats).escalation == "scope_change_needed"
    queue_path = env.queue_path
    doc = json.loads(queue_path.read_text())
    doc["tasks"][0]["scope"] = SCOPE_WITH_EVIL
    queue_path.write_text(json.dumps(doc, indent=2) + "\n")
    queue = TaskQueue(queue_path)
    adopted = adopt_queue(
        queue, env.root, approved_by="owner",
        confirm=lambda _q: True, echo=lambda _t: None,
    )
    assert [r.action for r in adopted] == ["adopted"], adopted
    new_status = recover_task(
        queue, "T-1", evidence="evil.py is the shared fixture; widened",
        confirm=lambda _q: True, echo=lambda _t: None, factory_root=env.root,
    )
    assert new_status == "ready"
    assert TaskQueue(queue_path).get("T-1")["status"] == "ready"

    # A widened scope that is NOT re-approved does not move — on a FRESH
    # parked task, so the refusal is the ledger gate itself (R1-4: the
    # previous spelling ran on an already-recovered task and only ever
    # hit the eligibility refusal). The refusal names the missing
    # approval, the task is unchanged and nothing is logged.
    seats = Drifting(drift_rounds=10)
    env = Env(_tmp(), tasks=[_task(scope=SCOPE)])
    assert env.run(seats).escalation == "scope_change_needed"
    queue_path = env.queue_path
    doc = json.loads(queue_path.read_text())
    doc["tasks"][0]["scope"] = SCOPE_WITH_EVIL
    queue_path.write_text(json.dumps(doc, indent=2) + "\n")
    log_before = len(ProgressLog.for_task(env.root, "fixture", "T-1").entries())
    with pytest.raises(ContractError) as excinfo:
        recover_task(TaskQueue(queue_path), "T-1", evidence="not re-approved",
                     confirm=lambda _q: True, echo=lambda _t: None,
                     factory_root=env.root)
    assert "adopt" in str(excinfo.value), str(excinfo.value)
    task = TaskQueue(queue_path).get("T-1")
    assert task["status"] == "needs_human"  # unchanged
    assert len(ProgressLog.for_task(env.root, "fixture", "T-1").entries()) \
        == log_before  # nothing logged for a refused move

    # A task parked on ANOTHER escalation never takes the scope exit
    # (Y8): the scope recover path is scope-escalations only.
    parked = _task(scope=SCOPE_WITH_EVIL)
    parked["attempts"] = [{
        "run_id": "r", "slice_id": "T-1", "started": "2026-11-10T10:00:00Z",
        "ended": "2026-11-10T10:01:00Z", "outcome": "escalated",
        "rounds": 1, "turns": 0, "input_tokens": 0, "output_tokens": 0,
        "prompt_versions": {}, "skills": [],
        "escalation": "review_not_converging", "note": "bounce",
    }]
    parked["status"] = "needs_human"
    queue_path = _queue(tmp2 := _tmp(), [parked])
    ledger = ApprovalLedger.for_project(tmp2 / "root", "fixture")
    ledger.record_task_for(TaskQueue(queue_path).get("T-1"), approved_by="o")
    with pytest.raises(ApprovalError) as excinfo:
        recover_task(TaskQueue(queue_path), "T-1", evidence="no",
                     confirm=lambda _q: True, echo=lambda _t: None,
                     factory_root=tmp2 / "root")
    assert "not recoverable" in str(excinfo.value)

    # And a fresh run with the widened scope passes L0 with that file.
    env2 = Env(_tmp(), tasks=[_task(scope=SCOPE_WITH_EVIL)])
    seats = Drifting(drift_rounds=10)  # writes evil.py every round
    result = env2.run(seats)
    assert result.outcome == "pr_opened", result.escalation

    # --- the per-run user prompt lists the task's scope paths.
    from factory.orchestrator import _implementer_prompt

    prompt = _implementer_prompt(_task(scope=SCOPE), None, "b", "", 1, 3)
    assert "impl.py" in prompt and "request_scope" in prompt


# ------------------------------------------------------------------ AC-4 ---

def test_ac_4():
    """Generated files only by an owner-approved profile list."""
    tmp = _tmp()

    # --- a lockfile change outside task scope passes ONLY when
    # paths.generated declares it.
    repo = _git_repo(tmp, "lockfile", {**BASE_FILES, "poetry.lock": "lock v1\n"})
    (repo / "poetry.lock").write_text("lock v2\n")
    _commit_change(repo)
    task = _task(scope={"paths": ["src/**"]})

    oos, _ = _out_of_scope_findings(repo, task)
    assert [f.file for f in oos] == ["poetry.lock"]

    profile = dict(MINIMAL_PROFILE)
    profile["paths"] = {"risk": {}, "protected": [], "generated": ["poetry.lock"]}
    oos, _ = _out_of_scope_findings(repo, task, profile)
    assert oos == []

    # --- a catch-all paths.generated refuses to load, pattern named.
    profile_file = tmp / "project.yaml"

    def write_profile(generated):
        doc = {
            "name": "fixture",
            "repo": {"path": str(tmp / "repo"), "default_branch": "main",
                     "remote": str(tmp / "remote.git")},
            "commands": {"setup": "true", "test": "true"},
            "paths": {"risk": {}, "protected": [], "generated": generated},
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        profile_file.write_text(yaml.safe_dump(doc))
        return load_profile(profile_file)

    for bad in (["**"], ["*/**"], ["/abs/lock"], ["../lock"], [""]):
        with pytest.raises(ContractError) as excinfo:
            write_profile(bad)
        assert "generated" in str(excinfo.value), bad

    # --- a widened paths.generated without re-approval refuses the run
    # through the existing profile-approval mechanism.
    loaded = write_profile(["poetry.lock"])
    root = tmp / "factory-root"
    ledger = ApprovalLedger.for_project(root, "fixture")
    ledger.record_profile_for(loaded, approved_by="owner")
    require_profile_approval(loaded, root)  # the approved shape runs
    # …then widen it: the group hash changed, the run refuses.
    write_profile(["poetry.lock", "package-lock.json"])
    widened = load_profile(profile_file)
    with pytest.raises(Exception) as excinfo:
        require_profile_approval(widened, root)
    assert "paths.generated" in str(excinfo.value)
