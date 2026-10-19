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
"""

import importlib.util
import re
import subprocess
import sys
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
    ambient_path_with_home_entries,
    build_rehearsal_fixture,
    run_rehearsal,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
    ).returncode == 0


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    """A fresh-fixture spawner running under the poisoned ambient PATH.

    Each scenario needs its own queue: a rehearsal that fails leaves the
    task ``needs_human`` with attempts spent, and the next ``run_once``
    would find nothing eligible.
    """
    monkeypatch.setenv("PATH", ambient_path_with_home_entries())
    state = {"n": 0}

    def spawn():
        state["n"] += 1
        fixture = build_rehearsal_fixture(tmp_path / f"proj-{state['n']}")
        root = tmp_path / f"factory-root-{state['n']}"
        root.mkdir()
        return fixture, root

    yield spawn


# --- AC-1: the fixture is realistic on the three incident axes --------------


def test_ac_1(tmp_path, monkeypatch):
    """A fixture project (built by the test, no network) has a setup that
    installs a Python venv and a node_modules tree, declared as
    setup_outputs; a remote whose default branch is ahead of the local
    clone; and an ambient PATH that includes home-dir entries."""
    fixture = build_rehearsal_fixture(tmp_path / "proj")

    # The remote is ahead: its main has commits the local clone lacks.
    remote_main = _git_out(fixture.remote, "rev-parse", "refs/heads/main")
    local_main = _git_out(fixture.repo, "rev-parse", "refs/heads/main")
    assert remote_main != local_main
    # Judged in the remote: it holds both objects, the local clone does not.
    assert _is_ancestor(fixture.remote, local_main, remote_main)
    assert not _is_ancestor(fixture.repo, remote_main, "refs/heads/main"), (
        "the fixture's local clone must NOT contain the remote's advanced main"
    )
    assert fixture.remote_main == remote_main

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
    run = create_run("rehearsal-fixture", fixture.repo, factory_root=tmp_path / "root")
    try:
        settings = build_settings(
            profile, run.root, model_endpoint=DEFAULT_BASE_URL,
            factory_root=tmp_path / "root",
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
    on main."""
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

    # The run built on the remote's advanced main, not the stale local one.
    assert _is_ancestor(
        fixture.remote, fixture.remote_main, f"refs/heads/{prs[0].head}"
    ), "the PR head must contain the remote-only commit"


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
    # local main, so the PR head misses the remote-only commit.
    with monkeypatch.context() as m:
        m.setattr(orchestrator_module, "_build_on_remote_default", lambda *a, **k: None)
        fixture, root = rehearsal()
        result, prs = run_rehearsal(fixture, root, now=lambda: OFF_PEAK)
    assert result.outcome == "pr_opened", result
    assert not _is_ancestor(
        fixture.remote, fixture.remote_main, f"refs/heads/{prs[0].head}"
    ), "a reverted remote-default fetch must leave the PR head without the remote commit"


# --- AC-4: CI runs the rehearsal on every PR ---------------------------------


def test_ac_4():
    """CI runs the rehearsal on every PR (it is not marked slow or
    optional)."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    triggers = workflow.get(True) or workflow.get("on")
    assert "pull_request" in triggers, "CI must run on every PR"

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

    # The rehearsal itself carries no marker that could deselect it.
    module = sys.modules[__name__]
    for name in ("test_ac_1", "test_ac_2", "test_ac_3"):
        test = getattr(module, name)
        assert not test.__dict__.get("pytestmark"), f"{name} must not carry pytest markers"
        assert not hasattr(test, "__unittest_skip__"), f"{name} must not be skipped"


# --- AC-5: the incident register ---------------------------------------------


def _load_test_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"incident-ref-{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    # test. Future incidents that cite a renamed or deleted test fail here.
    every_ref = set(re.findall(r"`(tests/[\w/.]+\.py::[\w_]+)`", doc))
    assert every_ref, "the register cites no tests at all"
    for ref in sorted(every_ref):
        path, _, name = ref.partition("::")
        file = REPO_ROOT / path
        assert file.exists(), f"{ref}: file does not exist"
        module = _load_test_module(file)
        assert callable(getattr(module, name, None)), f"{ref}: test does not exist"
