"""F-24 acceptance tests — BUILD-ORDER.md, work order F-24.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f24.py::test_ac_N``
means a test function with exactly that name.

The rehearsal is the point of this work order. Every earlier fixture was a
toy repo — nothing installed, a short PATH, a remote that never moves —
which is exactly how the three 2026-10-18 readiness incidents (PR #24)
passed CI. ``factory.rehearsal`` builds a fixture that is realistic on the
three axes that broke that day (an installed venv + ``node_modules`` the
gates must keep, an ambient PATH carrying home-dir entries, a remote whose
default branch is ahead of the local clone), and ``run_rehearsal`` drives
it through the real ``factory run`` machinery — clone manager, sandboxed
L0/L1, push, PR — with only the model seats stubbed.

The git fixture is built once per session and copied per scenario
(``fixture_template`` + ``rehearsal``): each rehearsal mutates its own
queue and remote, so scenarios cannot share state, but the repo
construction and the remote-advance push are paid once.
"""

import ast
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from factory import l1 as l1_module
from factory import orchestrator as orchestrator_module
from factory import sandbox as sandbox_module
from factory.contract import load_profile
from factory.gates import git as hardened_git
from factory.rehearsal import (
    RehearsalError,
    RehearsalFixture,
    ambient_path_with_home_entries,
    build_rehearsal_fixture,
    run_rehearsal,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """merge-base --is-ancestor with strict exit semantics.

    0 means yes, 1 means no; any other exit (missing object, bad ref,
    wrong repo) is an error and fails the test, never a quiet ``False`` —
    a check that cannot fail for the right reason proves nothing. This is
    the only ancestor helper in the file.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1), (
        f"merge-base --is-ancestor errored in {repo} (rc={proc.returncode}): "
        f"{proc.stderr.strip()[:300]}"
    )
    return proc.returncode == 0


def _test_names_in(file: Path) -> set:
    """The top-level test function names in ``file``, parsed, never run.

    Resolving the incident register's refs by importing each cited module
    would re-execute module-level code (and break on any cited module
    whose imports need the sandbox); the register cites plain module-level
    test functions, and the AST says whether the name exists.
    """
    tree = ast.parse(file.read_text())
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


@pytest.fixture(scope="session")
def fixture_template(tmp_path_factory):
    """The fixture pair, built once per session; spawns copy it."""
    return build_rehearsal_fixture(tmp_path_factory.mktemp("f24-template") / "proj")


@pytest.fixture
def rehearsal(tmp_path, monkeypatch, fixture_template):
    """A fresh-fixture spawner running under the poisoned ambient PATH.

    Each scenario needs its own queue and remote: a rehearsal that fails
    leaves the task ``needs_human`` with attempts spent, and a run pushes
    into its own remote. Each spawn is a copy of the session template with
    the profile's repo paths rewritten to the copy.
    """
    monkeypatch.setenv("PATH", ambient_path_with_home_entries())
    state = {"n": 0}

    def spawn():
        state["n"] += 1
        n = state["n"]
        repo = tmp_path / f"proj-{n}"
        remote = tmp_path / f"proj-{n}-remote.git"
        shutil.copytree(fixture_template.repo, repo)
        shutil.copytree(fixture_template.remote, remote)
        profile_path = repo / ".factory" / "project.yaml"
        profile = yaml.safe_load(profile_path.read_text())
        profile["repo"]["path"] = str(repo)
        profile["repo"]["remote"] = str(remote)
        profile_path.write_text(yaml.safe_dump(profile))
        root = tmp_path / f"factory-root-{n}"
        root.mkdir()
        fixture = RehearsalFixture(
            repo=repo,
            remote=remote,
            profile_path=profile_path,
            queue_path=repo / ".factory" / "tasks.json",
            local_main=fixture_template.local_main,
            remote_main=fixture_template.remote_main,
        )
        return fixture, root

    yield spawn


# --- AC-1: the fixture is realistic on the three incident axes --------------


def test_ac_1(rehearsal, monkeypatch):
    """A fixture project (built by the test, no network) has a setup that
    installs a Python venv and a node_modules tree, declared as
    setup_outputs; a remote whose default branch is ahead of the local
    clone; and an ambient PATH that includes home-dir entries."""
    fixture, root = rehearsal()

    # The remote is ahead: its main has commits the local clone lacks.
    remote_main = hardened_git(fixture.remote, "rev-parse", "refs/heads/main").strip()
    local_main = hardened_git(fixture.repo, "rev-parse", "refs/heads/main").strip()
    assert remote_main != local_main
    assert fixture.remote_main == remote_main
    # Judged in the remote, which holds both objects (strict semantics:
    # an errored merge-base fails the test, never reads as "not ahead").
    assert _is_ancestor(fixture.remote, local_main, remote_main)
    # And the local clone really lacks the advanced commit — cat-file,
    # not merge-base, so a missing object is checked as a missing object.
    missing = subprocess.run(
        ["git", "-C", str(fixture.repo), "cat-file", "-e", f"{remote_main}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0, (
        "the fixture's local clone must NOT contain the remote's advanced main"
    )

    # The profile declares what setup installs, and setup touches no network.
    profile = load_profile(fixture.profile_path)
    outputs = profile["paths"]["setup_outputs"]
    assert outputs == [".venv", "node_modules"]
    setup = profile["commands"]["setup"]
    assert "venv" in setup and "npm install" in setup
    assert "--no-audit" in setup and "--no-fund" in setup, (
        "npm must not reach the registry: the fixture is offline by design"
    )
    assert not re.search(r"https?://", setup), "setup must not fetch any URL"

    # The ambient PATH carries home-dir entries (absolute and literal ~).
    ambient = ambient_path_with_home_entries()
    home = str(Path.home())
    assert any(entry.startswith(home + "/") for entry in ambient.split(":"))
    assert any(entry.startswith("~") for entry in ambient.split(":"))

    # Setup really installs both trees, end to end through the real sandbox,
    # under that ambient PATH (the sanitised child PATH passes the guard).
    from factory.runs import cleanup_run, create_run
    from factory.sandbox import build_settings, run_sandboxed, write_settings
    from factory.session import DEFAULT_BASE_URL

    monkeypatch.setenv("PATH", ambient)
    run = create_run("rehearsal-fixture", fixture.repo, factory_root=root)
    try:
        settings = build_settings(
            profile, run.root, model_endpoint=DEFAULT_BASE_URL, factory_root=root
        )
        write_settings(settings, run.settings_path)
        proc = run_sandboxed(
            ["/bin/sh", "-c", setup], run.root, run.settings_path, timeout=240
        )
        assert proc.returncode == 0, (proc.stdout + proc.stderr)[-800:]
        assert (run.root / ".venv" / "bin" / "python").exists()
        assert (run.root / "node_modules" / ".bin" / "rehearse-lint").exists()
    finally:
        cleanup_run(run)


# --- AC-2: the rehearsal passes on main --------------------------------------


def test_ac_2(rehearsal):
    """A rehearsal test runs setup, L0 and L1 over a one-criterion contract
    against that fixture, end to end through the real sandbox, and passes
    on main. run_rehearsal itself raises on a stale-base PR (INC-003), so
    returning without an error already proves the remote-default property."""
    fixture, root = rehearsal()
    result, prs = run_rehearsal(fixture, root, now=lambda: OFF_PEAK)

    assert result.outcome == "pr_opened", result
    assert len(prs) == 1
    task = fixture.task()
    assert task["status"] == "in_review"
    attempts = task["attempts"]
    assert attempts and attempts[-1]["outcome"] == "pass"

    # Setup, L0 and L1 all ran inside the sandbox on the clean clone.
    reports = result.reports
    assert reports["L0"]["verdict"] == "pass"
    commands = {c["name"] for c in reports["L0"]["commands"]}
    assert "setup" in commands and "test" in commands and "lint" in commands
    assert reports["L1"]["verdict"] == "pass", reports["L1"]["findings"]
    red = next(c for c in reports["L1"]["commands"] if c["name"].startswith("red:"))
    green = next(c for c in reports["L1"]["commands"] if c["name"].startswith("green:"))
    assert red["exit_code"] != 0 and green["exit_code"] == 0


# --- AC-3: reverting each PR #24 fix fails the rehearsal ---------------------


def test_ac_3(rehearsal, monkeypatch):
    """Reverting each PR #24 fix in turn (L1 `_clean` keeping setup outputs,
    `_path_outside_home`, `_build_on_remote_default`) makes the rehearsal
    fail. Proven by three tests that monkeypatch the old behaviour back
    in."""

    def old_clean(repo, keep=()):
        hardened_git(repo, "clean", "-fdx")  # PR #24's pre-fix behaviour

    # (1) _clean without the setup-output keeps: the venv is wiped between
    # L1's phases, every ref exits 127, the round never passes.
    with monkeypatch.context() as m:
        m.setattr(l1_module, "_clean", old_clean)
        fixture, root = rehearsal()
        result, prs = run_rehearsal(fixture, root, now=lambda: OFF_PEAK)
    assert result.outcome == "needs_human", result
    assert not prs
    findings = [f["id"] for f in (result.reports.get("L1") or {}).get("findings") or []]
    assert "test_not_green" in findings, result.reports["L1"]

    # (2) _path_outside_home as identity: the sandboxed child inherits the
    # home/literal-~ PATH entries, the fixture's env guard fails setup.
    with monkeypatch.context() as m:
        m.setattr(sandbox_module, "_path_outside_home", lambda path: path)
        fixture, root = rehearsal()
        result, prs = run_rehearsal(fixture, root, now=lambda: OFF_PEAK)
    assert result.outcome == "needs_human", result
    assert not prs
    findings = [f["id"] for f in (result.reports.get("L0") or {}).get("findings") or []]
    assert "setup_failed" in findings, result.reports["L0"]

    # (3) _build_on_remote_default as a no-op: the run builds on the stale
    # local main, and run_rehearsal itself raises on the stale-base PR —
    # the harness, not only this test, detects INC-003 (review finding 2).
    with monkeypatch.context() as m:
        m.setattr(orchestrator_module, "_build_on_remote_default", lambda *a, **k: None)
        fixture, root = rehearsal()
        with pytest.raises(RehearsalError, match="remote's default-branch"):
            run_rehearsal(fixture, root, now=lambda: OFF_PEAK)


# --- AC-4: CI runs the rehearsal on every PR ---------------------------------


def test_ac_4():
    """CI runs the rehearsal on every PR (it is not marked slow or
    optional)."""
    # The rehearsal this criterion is about must be part of the tree CI
    # checks out: on a base without F-24 there is no rehearsal to run at
    # all, whatever ci.yml says. (Checked as a file, not an import: an
    # editable install of a newer checkout would satisfy the import.)
    assert (REPO_ROOT / "factory" / "rehearsal.py").is_file(), (
        "the rehearsal harness must live in the repo CI clones"
    )

    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    triggers = workflow.get(True) or workflow.get("on")
    assert "pull_request" in triggers, "CI must run on every PR"
    # A paths filter on the PR trigger would skip the rehearsal entirely.
    pr_trigger = triggers.get("pull_request") if isinstance(triggers, dict) else None
    if isinstance(pr_trigger, dict):
        assert not ({"paths", "paths-ignore"} & set(pr_trigger)), (
            "CI must not filter which PR files trigger the suite"
        )

    steps = workflow["jobs"]["test"]["steps"]
    pytest_steps = [s for s in steps if "pytest" in (s.get("run") or "")]
    assert pytest_steps, "CI must run pytest"
    for step in pytest_steps:
        run = step["run"]
        assert run.strip() == "pytest -q", (
            f"the suite must run whole (no marker/path filters): {run!r}"
        )
        assert "if" not in step and "continue-on-error" not in step, (
            "the pytest step must not be conditional or non-blocking"
        )

    # Nothing may deselect the rehearsal from inside the suite either:
    # per-test or module-level marks, a conftest collection hook, or
    # pytest addopts would all remove it while ci.yml still says pytest -q.
    module = sys.modules[__name__]
    assert not vars(module).get("pytestmark"), (
        "no module-level pytest marks may deselect this file"
    )
    for name in ("test_ac_1", "test_ac_2", "test_ac_3"):
        test = getattr(module, name)
        assert not test.__dict__.get("pytestmark"), f"{name} must not carry pytest markers"
        assert not hasattr(test, "__unittest_skip__"), f"{name} must not be skipped"
    for conftest in (REPO_ROOT / "conftest.py", REPO_ROOT / "tests" / "conftest.py"):
        if not conftest.exists():
            continue
        for node in ast.walk(ast.parse(conftest.read_text())):
            assert not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "pytest_collection_modifyitems"
            ), f"{conftest} must not deselect tests at collection time"
    ini = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    ini_options = ini.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert "addopts" not in ini_options, "pytest must run with no addopts filters"


# --- AC-5: the incident register ---------------------------------------------


def test_ac_5():
    """`docs/incidents.md` records the three incidents, and for each: what
    broke, which layer should have caught it, and the test that now does.
    Every future factory incident adds an entry and a test in the same PR
    (checked by a test that every entry names an existing test)."""
    doc = (REPO_ROOT / "docs" / "incidents.md").read_text()

    sections = re.split(r"(?m)^## ", doc)[1:]
    assert len(sections) >= 3, "the three PR #24 incidents must each have an entry"
    for section in sections:
        assert "**What broke:**" in section, section.splitlines()[0]
        assert "**Which layer should have caught it:**" in section, (
            section.splitlines()[0]
        )
        refs = re.findall(r"`(tests/[\w/.]+\.py::[\w_]+)`", section)
        assert refs, f"entry {section.splitlines()[0]!r} names no test"

    # The standing rule: every test ref in the whole file names an existing
    # top-level test function, resolved by parsing (never executing) the
    # cited module. Future incidents citing a renamed or deleted test fail.
    every_ref = set(re.findall(r"`(tests/[\w/.]+\.py::[\w_]+)`", doc))
    assert every_ref, "the register cites no tests at all"
    for ref in sorted(every_ref):
        path, _, name = ref.partition("::")
        file = REPO_ROOT / path
        assert file.exists(), f"{ref}: file does not exist"
        assert name in _test_names_in(file), f"{ref}: test does not exist"


# --- owner review round (9 findings) ------------------------------------------


def test_review1_path_leak_covers_absolute_home_entries(rehearsal, monkeypatch):
    """Review finding 1: the PATH revert must catch the absolute-$HOME
    leak class too, not only literal `~` entries — a `_path_outside_home`
    simplified to startswith('~') would leak `/<home>/.pyenv/shims` back
    into the sandboxed PATH. The fixture's env guard bakes the host's home
    paths in and refuses entries under them, so the rehearsal still fails."""
    def tilde_only_filter(path):
        return ":".join(e for e in path.split(":") if e and not e.startswith("~"))

    with monkeypatch.context() as m:
        m.setattr(sandbox_module, "_path_outside_home", tilde_only_filter)
        fixture, root = rehearsal()
        result, prs = run_rehearsal(fixture, root, now=lambda: OFF_PEAK)
    assert result.outcome != "pr_opened" or not prs, (
        "a tilde-only PATH filter must still fail the rehearsal"
    )
    findings = [f["id"] for f in (result.reports.get("L0") or {}).get("findings") or []]
    assert "setup_failed" in findings, result.reports["L0"]


def test_review3_fixture_ignores_install_trees(rehearsal):
    """Review finding 3: a real project gitignores what setup installs; the
    orchestrator's `git add -A` would otherwise commit .venv and
    node_modules into the PR of any session that ran setup."""
    fixture, _ = rehearsal()
    for ignored in (".venv/bin/python", "node_modules/tiny-kit/package.json"):
        proc = subprocess.run(
            ["git", "-C", str(fixture.repo), "check-ignore", "-q", ignored],
            capture_output=True,
        )
        assert proc.returncode == 0, f"{ignored} must be gitignored by the fixture"


def test_review4_ambient_path_has_no_empty_entries(monkeypatch):
    """Review finding 4: with PATH unset the poisoned ambient PATH must not
    grow an empty entry — an empty PATH entry means the current directory,
    an unintended extra leak class AC-3 cannot attribute."""
    monkeypatch.delenv("PATH", raising=False)
    ambient = ambient_path_with_home_entries()
    entries = ambient.split(":")
    assert ambient, "the poisoned PATH must still carry the home entries"
    assert "" not in entries, "an empty PATH entry resolves commands from cwd"
    assert not ambient.endswith(":"), f"trailing separator: {ambient!r}"


def test_review7_incident_refs_resolve_without_executing(tmp_path):
    """Review finding 7: ref resolution must parse, not import — a cited
    module with module-level side effects (or imports that need the
    sandbox) must resolve without running any of it."""
    poison = tmp_path / "test_poison.py"
    poison.write_text(
        'raise RuntimeError("import side effect must never run")\n\n\n'
        "def test_ok():\n    pass\n"
    )
    assert "test_ok" in _test_names_in(poison)
