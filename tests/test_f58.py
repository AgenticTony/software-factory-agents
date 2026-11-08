"""F-58 contract tests — host preflight, red-CI guard, host-named CI,
the host table, and runtime-built fake credentials (GitGuardian green).

Owner scope decision (2026-11-08, quoted in .factory/BOOTSTRAP.md §F-58):
the production host is the owner's Mac; Ubuntu 26.04 support is DEFERRED,
so AC-1, AC-2 and AC-7 have NO test function here (the ledger records
why, and the deferred work is carried as a proposed WO). AC-3 and AC-6
are amended as built here. AC-G is owner-added, hence ``test_ac_g``.

Red-first on the untouched tree (fddca65): every failure below is its
AC's own defect — a missing seam fails through ``_ac_import`` with the
seam's name, never an import error at collection time.
"""

import ast
import importlib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml
from _support import wait_until

from factory.contract import acceptance_hash
from factory.mergeorder import Protection
from factory.orchestrator import run_once
from factory.runs import RunError
from factory.state import TaskQueue

REPO = Path(__file__).resolve().parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
README = REPO / "README.md"
CLAUDE_MD = REPO / "CLAUDE.md"
# The proposed work order carrying the deferred Ubuntu 26.04 host work
# (id proposed here; the ledger §F-58 holds its full text).
DEFERRED_WO = "F-61"

# A Sunday afternoon in the config's timezone: outside every default
# peak window (weekdays 08:00–12:00 Europe/Stockholm), so runs may start.
OFF_PEAK = datetime(2026, 1, 18, 15, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
PROTECTED_STRICT = Protection(checked=True, strict=True)
PLAN_LIMIT = Protection(plan_limit=True, detail="403 upgrade to GitHub Pro")


def _ac_import(module: str, name: str):
    """Import a seam this WO adds, failing with the AC's defect (never a
    collection-time ImportError) when it is missing."""
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        pytest.fail(f"AC defect — missing seam {module}.{name}: {exc}")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1",
          *, ref: str = "tests/test_feature.py::test_ac_1") -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42",
         "check": {"type": "test", "ref": ref}}
    ]
    return {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-18T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 10},
        "attempts": [],
    }


class Env:
    """One fixture project: local repo + bare remote + factory root (the
    test_f26 pattern, local to this file — no cross-test imports)."""

    def __init__(self, tmp_path: Path, tasks: list | None = None,
                 *, unavailable: bool = False):
        self.tmp = tmp_path
        self.root = tmp_path / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        repo_cfg = {
            "path": str(self.repo),
            "default_branch": "main",
            "remote": str(self.remote),
            "merge_method": "squash",
        }
        if unavailable:
            repo_cfg["branch_protection"] = "unavailable"
        self.profile = {
            "name": "fixture",
            "repo": repo_cfg,
            "commands": {
                "setup": "true",
                "test": f"{sys.executable} -m pytest -q -p no:cacheprovider",
            },
            "paths": {"risk": {"payments": ["src/pay/**"]},
                      "protected": [".factory/**"]},
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        self.profile_path = factory_dir / "project.yaml"
        self.profile_path.write_text(yaml.safe_dump(self.profile))
        self.queue_path = factory_dir / "tasks.json"
        doc = {"version": 1, "project": "fixture",
               "tasks": tasks if tasks is not None else [_task()]}
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)],
            check=True,
        )
        self.prs: list = []

    def queue_bytes(self) -> bytes:
        return self.queue_path.read_bytes()

    def runs_created(self) -> list[Path]:
        runs = self.root / "runs"
        return sorted(p for p in runs.glob("*/*") if p.is_dir()) \
            if runs.is_dir() else []

    def open_pr(self, pr) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        return TaskQueue(self.queue_path).get(task_id)

    def run(self, session_runner=None, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", _protection(
            PLAN_LIMIT if self.profile["repo"].get("branch_protection")
            == "unavailable" else PROTECTED_STRICT))
        kwargs.setdefault("pr_state", lambda url: "open")
        kwargs.setdefault("srt_bin_path", _ok_srt())
        return run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )

    def advance_remote_main(self, body: str = "def answer():\n    return 99\n",
                            message: str = "remote moves on") -> str:
        scratch = self.tmp / f"advance-{message.replace(' ', '-')}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(scratch)],
                       check=True)
        (scratch / "impl.py").write_text(body)
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x",
             "commit", "-qm", message)
        _git(scratch, "push", "-q", "origin", "HEAD:main")
        return _git(self.remote, "rev-parse", "refs/heads/main")

    def push_branch(self, branch: str) -> str:
        """A PR branch on the remote: main's head plus one commit."""
        scratch = self.tmp / f"branch-{branch.replace('/', '-')}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(scratch)],
                       check=True)
        (scratch / "impl.py").write_text("def answer():\n    return 42\n")
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x",
             "commit", "-qm", "pr branch")
        _git(scratch, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return _git(self.remote, "rev-parse", f"refs/heads/{branch}")

    def force_in_review(self, task_id: str = "T-1", *, branch: str,
                        pr_url: str = "https://github.com/fixture/fixture/pull/9"):
        q = TaskQueue(self.queue_path)
        q.transition(task_id, "in_progress")
        q.transition(task_id, "in_review")
        q.get(task_id)["links"] = {"pr": pr_url, "branch": branch}
        q.save()


def _protection(result: Protection):
    return lambda remote, branch: result


def _ci(state: str, checks=(("test-ubuntu-24.04 (3.11)", "success"),),
        detail: str = ""):
    """A CIState-shaped fake (SimpleNamespace: the orchestrator reads
    .state/.checks/.detail only)."""
    return SimpleNamespace(state=state, checks=tuple(checks), detail=detail)


class CIRecorder:
    """A ci_state seam fake answering per-SHA canned states, recording
    every (remote, sha) call."""

    def __init__(self, by_sha: dict | None = None, default=None):
        self.by_sha = by_sha or {}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, remote, sha):
        self.calls.append(sha)
        return self.by_sha.get(sha, self.default)


def _fake_srt(directory: Path, name: str, body: str) -> Path:
    """An executable fake srt: answers ``--version`` with the pin, then
    runs ``body`` (the pin check is part of the honest preflight)."""
    script = directory / name
    script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in --version) echo 'srt 0.0.79'; exit 0;; esac\n"
        + body
    )
    script.chmod(0o755)
    return script


_OK_SRT: Path | None = None


def _ok_srt() -> Path:
    """A module-wide fake srt that faithfully runs the -c command: the
    default host probe for Env-based tests (fast, no real srt boot)."""
    global _OK_SRT
    if _OK_SRT is None:
        import tempfile

        directory = Path(tempfile.mkdtemp(prefix="f58-ok-srt-"))
        _OK_SRT = _fake_srt(
            directory, "srt",
            'while [ "$1" != "-c" ] && [ $# -gt 0 ]; do shift; done\n'
            'shift\nexec sh -c "$1"\n',
        )
    return _OK_SRT


_SETGROUPS = (
    "apply-seccomp: write /proc/self/setgroups (nested userns is "
    "capability-restricted; caller must provide CAP_SYS_ADMIN): "
    "Permission denied"
)


# ============================================================= AC-3 =========
# CI names its host (amended: per-host job naming on 24.04 only — the
# 26.04 job is deferred with AC-1/2/7).


def _ci_jobs() -> dict:
    return yaml.safe_load(CI_YML.read_text())["jobs"]


def test_ac_3():
    """Every CI job's id names its ``runs-on`` host; the host stays the
    pinned ubuntu-24.04 (never the floating alias, no 26.04 job); the
    sandbox job installs the sandbox stack, runs npm ci and cannot
    deselect test_f05; the F-51 invariants (exact live_network deselect,
    fail-fast off, matrix unchanged) survive the rename."""
    raw = CI_YML.read_text()
    jobs = _ci_jobs()

    # Every job names its host: a runner-image change then fails a NAMED
    # job (the amended AC-3).
    for job_id, job in jobs.items():
        runs_on = job.get("runs-on")
        assert runs_on, f"job {job_id!r} has no runs-on"
        assert runs_on in job_id, (
            f"job {job_id!r} does not name its runs-on host {runs_on!r}"
        )
    assert all(j["runs-on"] == "ubuntu-24.04" for j in jobs.values()), (
        "the verified CI host is ubuntu-24.04 only (26.04 is deferred; "
        "no job may float with an alias label)"
    )
    assert "ubuntu-latest" not in raw, "the floating alias may never appear"

    # The pin's stated reason: 26.04 is a known-broken host, deferred to
    # the proposed WO; changing the image must be a deliberate edit.
    first_pin = raw.find("runs-on: ubuntu-24.04")
    assert first_pin > 0
    comment_window = raw[max(0, first_pin - 700):first_pin]
    assert "26.04" in comment_window, (
        "the runs-on pin must state the 26.04 diagnosis beside it"
    )
    assert DEFERRED_WO in comment_window, (
        f"the pin must point at the deferred work order ({DEFERRED_WO})"
    )
    assert "deliberate" in comment_window, (
        "the pin must say changing the image is a deliberate edit"
    )

    # The sandbox job: installs bubblewrap, runs npm ci, and its pytest
    # step cannot deselect test_f05.
    steps_text = {
        job_id: "\n".join(s.get("run", "") for s in job["steps"])
        for job_id, job in jobs.items()
    }
    sandbox_ids = [jid for jid, text in steps_text.items()
                   if "bubblewrap" in text and "npm ci" in text]
    assert sandbox_ids, "no job installs the sandbox stack (bubblewrap + npm ci)"
    sandbox = jobs[sandbox_ids[0]]
    pytest_steps = [s.get("run", "").strip() for s in sandbox["steps"]
                    if "pytest" in s.get("run", "")]
    assert pytest_steps == ['pytest -q -n auto -m "not live_network"'], (
        f"the sandbox job's pytest step must deselect exactly live_network, "
        f"got {pytest_steps!r}"
    )
    for jid, text in steps_text.items():
        assert "--deselect" not in text, f"{jid}: --deselect is forbidden"
        assert " -k " not in f" {text} ", f"{jid}: a -k filter could silently deselect tests"
        assert "--ignore" not in text, f"{jid}: --ignore is forbidden"
        assert "test_f05" not in text, f"{jid}: names test_f05 explicitly"

    # The one sanctioned deselect can only drop live_network-marked
    # tests: test_f05's own contract tests (the sandbox attack suite)
    # carry no such mark, so the main job still runs them.
    f05 = ast.parse((REPO / "tests" / "test_f05.py").read_text())
    for func in (n for n in f05.body
                 if isinstance(n, ast.FunctionDef)
                 and re.fullmatch(r"test_ac_\d+", n.name)):
        for dec in func.decorator_list:
            rendered = ast.unparse(dec)
            assert "live_network" not in rendered, (
                f"test_f05.py::{func.name} must not carry the live_network "
                "marker — the main job's deselect must never drop the "
                "sandbox contract tests"
            )

    # F-51 invariants survive the rename: fail-fast off, matrix unchanged,
    # a separately named job still selects exactly live_network.
    assert sandbox["strategy"]["fail-fast"] is False
    assert sandbox["strategy"]["matrix"]["python-version"] == ["3.11", "3.13"]
    live_jobs = [jid for jid, text in steps_text.items()
                 if "pytest" in text and "live_network" in text
                 and "not live_network" not in text]
    assert live_jobs, "a separately named job must still select live_network"
    live_steps = [s.get("run", "").strip() for s in jobs[live_jobs[0]]["steps"]
                  if "pytest" in s.get("run", "")]
    assert live_steps == ["pytest -q -rs -m live_network"], (
        f"the live-network job must select exactly live_network: {live_steps!r}"
    )


# ============================================================= AC-4 =========
# factory run preflights the host: one named refusal before the queue is
# read, reclaimed, picked or anything is recorded.


def test_ac_4(tmp_path: Path):
    """A host whose sandbox cannot start refuses ``factory run`` with ONE
    named message before anything is read or recorded: the queue is
    byte-identical, no run dir exists, no task is picked. A healthy fake
    host proceeds; stub seats skip the probe; admission still runs first;
    a PATH shim or an env var cannot forge the answer."""
    preflight_host = _ac_import("factory.sandbox", "preflight_host")
    HostPreflightError = _ac_import("factory.sandbox", "HostPreflightError")
    import factory.orchestrator as orch

    env = Env(tmp_path, tasks=[_task()])
    before = env.queue_bytes()

    # The exact PR #97 Linux failure → one refusal naming the setting.
    setgroups_srt = _fake_srt(tmp_path, "srt-setgroups",
                              f"echo '{_SETGROUPS}' >&2\nexit 1\n")
    with pytest.raises(HostPreflightError, match="setgroups") as excinfo:
        env.run(srt_bin_path=setgroups_srt)
    message = str(excinfo.value)
    assert "README" in message, "the refusal must point at the README section"
    # The refusal must NAME the missing host setting itself, not merely
    # echo the failure text back (the echo alone would pass with a
    # generic "preflight failed" message).
    assert "sysctl" in message or "apparmor" in message, (
        "the refusal must name the missing host setting (the sysctl)"
    )
    assert "user namespace" in message or "userns" in message, (
        "the refusal must name the missing host capability"
    )
    assert env.queue_bytes() == before, "the queue must be untouched"
    assert not env.runs_created(), "no run dir may be created"
    assert env.task()["status"] == "ready", "no task may be picked"

    # A macOS seatbelt refusal → the same class, naming the seatbelt.
    seatbelt_srt = _fake_srt(
        tmp_path, "srt-seatbelt",
        "echo 'sandbox-exec: sandbox_apply: Operation not permitted' >&2\n"
        "exit 71\n")
    with pytest.raises(HostPreflightError, match="sandbox_apply") as excinfo:
        env.run(srt_bin_path=seatbelt_srt)
    assert "seatbelt" in str(excinfo.value).lower(), (
        "the refusal must name the seatbelt, not just echo the failure"
    )
    assert env.queue_bytes() == before, "the queue must still be untouched"

    # srt missing → a refusal naming the install, still before the pick.
    with pytest.raises(Exception, match="srt") as missing:
        env.run(srt_bin_path=tmp_path / "no-such-srt")
    assert "npm ci" in str(missing.value) or "not found" in str(missing.value)

    # A healthy host proceeds (idle queue: nothing to pick, no session).
    idle = Env(tmp_path / "idle", tasks=[])
    assert idle.run().outcome == "idle"

    # ORDER, not just side effects: the probe runs before the queue is
    # read — instrumented TaskQueue and probe record the sequence.
    order: list[str] = []
    real_task_queue = orch.TaskQueue

    def recording_queue(path, *args, **kwargs):
        order.append("queue-read")
        return real_task_queue(path, *args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(orch, "preflight_host", lambda **kw: order.append("probe"))
    monkey.setattr(orch, "TaskQueue", recording_queue)
    try:
        Env(tmp_path / "order", tasks=[]).run()
    finally:
        monkey.undo()
    assert order == ["probe", "queue-read"], (
        "the host probe must run before the queue is read"
    )

    # Stub seats: the probe is not called at all.
    stub_env = Env(tmp_path / "stub", tasks=[])
    calls: list = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(orch, "preflight_host",
                   lambda **kw: calls.append(kw) or None)
    try:
        stub_env.run(session_runner=lambda *a, **k: pytest.fail("no session"))
    finally:
        monkey.undo()
    assert calls == [], "with stub seats the host probe must not run"

    # Admission still runs first: a refused repo never pays the probe.
    probe_calls: list = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(orch, "preflight_host",
                   lambda **kw: probe_calls.append(kw) or None)
    try:
        with pytest.raises(Exception, match="strict"):
            env.run(protection=_protection(
                Protection(checked=True, strict=False, detail="no checks")))
    finally:
        monkey.undo()
    assert probe_calls == [], "admission refuses before the host probe"

    # A fake srt earlier on PATH cannot forge the answer, and no env var
    # claims a healthy host: the probe uses the pinned absolute binary.
    import factory.sandbox as sandbox_module

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _fake_srt(shim_dir, "srt",
              'while [ "$1" != "-c" ] && [ $# -gt 0 ]; do shift; done\n'
              'shift\necho probe-ok\nexit 0\n')
    broken = _fake_srt(tmp_path, "srt-broken",
                       "echo 'apply-seccomp: write /proc/self/setgroups: "
                       "Permission denied' >&2\nexit 1\n")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sandbox_module, "srt_bin", lambda: broken)
    monkey.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    monkey.setenv("FACTORY_HOST_OK", "1")
    monkey.setenv("FACTORY_PREFLIGHT_SKIP", "1")
    try:
        with pytest.raises(HostPreflightError, match="setgroups"):
            preflight_host()
    finally:
        monkey.undo()


def test_ac_4_failures(tmp_path: Path):
    """Every other preflight failure refuses — unknown is never ok: exit 0
    without the expected output, any other failure text, and a hang
    (timeout refusal with no child left running — checked, not assumed)."""
    preflight_host = _ac_import("factory.sandbox", "preflight_host")
    HostPreflightError = _ac_import("factory.sandbox", "HostPreflightError")

    # rc 0 but not the probe's expected output → refused.
    silent = _fake_srt(tmp_path, "srt-silent", "exit 0\n")
    with pytest.raises(HostPreflightError, match="output"):
        preflight_host(srt_bin_path=silent, timeout_sec=10)

    # Some other failure → refused, naming what failed.
    weird = _fake_srt(tmp_path, "srt-weird", "echo 'kaboom' >&2\nexit 3\n")
    with pytest.raises(HostPreflightError, match="kaboom"):
        preflight_host(srt_bin_path=weird, timeout_sec=10)

    # A hang → timeout refusal, and no child left running afterwards.
    pidfile = tmp_path / "hang.pid"
    hang = _fake_srt(tmp_path, "srt-hang",
                     f"echo $$ > {pidfile}\nsleep 300\n")
    with pytest.raises(HostPreflightError, match="timed out"):
        preflight_host(srt_bin_path=hang, timeout_sec=3)
    pid = int(pidfile.read_text().strip())
    wait_until(lambda: _pid_gone(pid), timeout=10,
               what=f"the hung fake srt (pid {pid}) to be gone")


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # alive, just not ours to signal
    return False


def test_ac_4_real_host_probe():
    """The REAL probe passes on this host (macOS locally, ubuntu-24.04 in
    CI) with the pinned real srt. It fails, never skips, on a broken host
    (a nested-suite run skips through the INC-007 conftest guard, like
    the F-05 suite itself)."""
    preflight_host = _ac_import("factory.sandbox", "preflight_host")
    assert preflight_host() is None


# ============================================================= AC-5 =========
# Self-enforced green CI when repo.branch_protection: unavailable.


def test_ac_5(tmp_path: Path):
    """With branch_protection unavailable, a RED default branch refuses
    the run before the pick (naming the SHA and every failing check), a
    pending one proceeds, an unresolvable one refuses, and a repo with no
    checks at all refuses as an owner policy question. A red, pending,
    unknown or checkless PR is never reported ready; re-run-then-green
    is. With real protection, zero CI calls."""
    env = Env(tmp_path, tasks=[_task()], unavailable=True)
    before = env.queue_bytes()
    remote_head = _git(env.remote, "rev-parse", "refs/heads/main")

    # RED default branch → one refusal, nothing read or recorded.
    ci = CIRecorder(default=_ci("failed", checks=(
        ("live-network", "failed"),
        ("test-ubuntu-24.04 (3.11)", "failure"),
    )))
    with pytest.raises(RunError) as excinfo:
        env.run(ci_state=ci)
    message = str(excinfo.value)
    assert remote_head[:12] in message, "the refusal names the SHA"
    assert "live-network" in message and "test-ubuntu-24.04 (3.11)" in message, (
        "the refusal names every failing check"
    )
    assert env.queue_bytes() == before
    assert not env.runs_created()
    assert ci.calls == [remote_head], "the CI answer is for the remote head"

    # The REMOTE head, never the local checkout's HEAD.
    local_head = _git(env.repo, "rev-parse", "HEAD")
    moved = env.advance_remote_main(message="guard reads the remote")
    assert moved != local_head
    ci2 = CIRecorder(default=_ci("failed", checks=(("any", "failure"),)))
    with pytest.raises(RunError, match=re.escape(moved[:12])):
        env.run(ci_state=ci2)
    assert ci2.calls == [moved], "the guard asked about the remote head"

    # Pending → proceed (a run merges nothing).
    pending = Env(tmp_path / "pending", tasks=[], unavailable=True)
    assert pending.run(ci_state=CIRecorder(default=_ci(
        "pending", checks=(("test-ubuntu-24.04 (3.11)", "in_progress"),)),
    )).outcome == "idle"

    # Unresolvable → refuse (fail closed, F-45: never guessed).
    with pytest.raises(RunError, match="unresolvable"):
        pending.run(ci_state=CIRecorder(default=_ci(
            "unresolvable", detail="gh printed no JSON")))

    # No checks at all → refuse as the owner's policy question, never a
    # silent pass.
    with pytest.raises(RunError, match="no CI checks"):
        pending.run(ci_state=CIRecorder(default=_ci("none")))

    # --- a red or pending PR is never reported ready ---
    pr_env = Env(tmp_path / "pr", tasks=[_task()], unavailable=True)
    branch_sha = pr_env.push_branch("factory/t-1")
    pr_env.force_in_review(branch="factory/t-1")
    default_sha = _git(pr_env.remote, "rev-parse", "refs/heads/main")

    red = CIRecorder({
        default_sha: _ci("success"),
        branch_sha: _ci("failed", checks=(("live-network", "failed"),)),
    })
    result = pr_env.run(ci_state=red)
    assert result.ready_prs == [], "a red PR is never reported ready"
    assert result.not_ready_prs, "the refusal to call it ready is visible"
    assert any("live-network" in line and "failed" in line
               for line in result.not_ready_prs), result.not_ready_prs
    assert pr_env.task()["status"] == "in_review"

    # Re-run-then-green: the same PR, checks now green → ready.
    green = CIRecorder({
        default_sha: _ci("success"),
        branch_sha: _ci("success", checks=(("live-network", "success"),)),
    })
    result = pr_env.run(ci_state=green)
    assert result.not_ready_prs == []
    assert any("factory/t-1" in line or "pull/9" in line
               for line in result.ready_prs), result.ready_prs

    # A pending PR is not ready either, and an unknown CI state is not
    # ready (fail closed) — one more PR on its own env.
    pr2 = Env(tmp_path / "pr2", tasks=[_task()], unavailable=True)
    b2 = pr2.push_branch("factory/t-2")
    pr2.force_in_review(branch="factory/t-2",
                        pr_url="https://github.com/fixture/fixture/pull/10")
    d2 = _git(pr2.remote, "rev-parse", "refs/heads/main")
    result = pr2.run(ci_state=CIRecorder({
        d2: _ci("success"),
        b2: _ci("pending", checks=(("GitGuardian", "in_progress"),)),
    }))
    assert result.ready_prs == []
    assert any("GitGuardian" in line and "pending" in line
               for line in result.not_ready_prs), result.not_ready_prs

    result = pr2.run(ci_state=CIRecorder({
        d2: _ci("success"),
        b2: _ci("unresolvable", detail="gh errored"),
    }))
    assert result.ready_prs == []
    assert result.not_ready_prs

    # A PR with no checks yet is not ready (freshly opened, race).
    result = pr2.run(ci_state=CIRecorder({
        d2: _ci("success"),
        b2: _ci("none"),
    }))
    assert result.ready_prs == []
    assert result.not_ready_prs

    # --- real protection: zero CI calls, readiness as before ---
    strict_env = Env(tmp_path / "strict", tasks=[_task()])
    strict_env.push_branch("factory/t-3")
    strict_env.force_in_review(branch="factory/t-3")
    zero = CIRecorder()
    result = strict_env.run(ci_state=zero)
    assert zero.calls == [], "with real protection no CI call is made"
    assert result.ready_prs, "the ready line stands on up-to-date alone"
    assert result.not_ready_prs == []


# GitHub's combined-status answer for a commit with NO legacy statuses:
# rollup "pending", zero entries — the shape every commit in this repo
# has. R1-1: reading the rollup made every green commit "pending", so no
# PR was ever reported ready. The ENTRIES are the truth; the rollup is
# not classified at all.
_R1_EMPTY_STATUS = {"state": "pending", "statuses": [], "total_count": 0}


# --- gh_ci_state: the thin wrapper, unit-tested against canned gh --------


def _run_page(checks, *, total=None):
    """One canned check-runs page document."""
    return {"total_count": total if total is not None else len(checks),
            "check_runs": checks}


def _check(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


class _CannedGh:
    """A _run seam answering canned gh api documents, recording argv.

    Serves each check-runs page BY THE REQUESTED ``page=`` parameter (an
    empty page beyond the canned ones), so a caller that ignores
    pagination — always requesting page 1 — cannot accidentally be
    served the later pages anyway (the mutation that proved this).
    """

    def __init__(self, pages, status_doc, *, rc=0, raw=None):
        self.pages = list(pages)
        self.status_doc = status_doc
        self.rc = rc
        self.raw = raw  # if set, stdout is this string verbatim
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        url = next((a for a in argv
                    if "/check-runs" in a or a.endswith("/status")), "")
        if self.rc != 0:
            return SimpleNamespace(returncode=self.rc, stdout="",
                                   stderr="gh: error")
        if "/check-runs" in url:
            page_arg = next((a for a in argv if a.startswith("page=")), "page=1")
            number = int(page_arg.removeprefix("page="))
            doc = (self.pages[number - 1] if number <= len(self.pages)
                   else _run_page([]))
            stdout = self.raw if self.raw is not None else json.dumps(doc)
        else:
            stdout = (self.raw if self.raw is not None
                      else json.dumps(self.status_doc))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


_REMOTE = "https://github.com/fixture/fixture.git"
_SHA = "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e"


def _gh_ci(canned, sha=_SHA):
    gh_ci_state = _ac_import("factory.mergeorder", "gh_ci_state")
    return gh_ci_state(_REMOTE, sha, gh_bin="/usr/bin/gh", _run=canned), canned


@pytest.mark.parametrize("conclusion", [
    "failure", "cancelled", "timed_out", "action_required", "stale",
])
def test_ac_5_failed_conclusions(conclusion):
    """Any non-green completed conclusion is FAILED, and the failing
    check is named — never reported as success or pending."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test-ubuntu-24.04 (3.11)", conclusion)])],
        _R1_EMPTY_STATUS,
    ))
    assert state.state == "failed"
    assert ("test-ubuntu-24.04 (3.11)", conclusion) in state.checks


def test_ac_5_all_green_is_success():
    """success, neutral and skipped all count as green (all checks count,
    live-network and GitGuardian included)."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([
            _check("test-ubuntu-24.04 (3.11)", "success"),
            _check("test-ubuntu-24.04 (3.13)", "success"),
            _check("live-network-ubuntu-24.04", "success"),
            _check("GitGuardian", "skipped"),
            _check("typecheck-ubuntu-24.04", "neutral"),
        ])],
        _R1_EMPTY_STATUS,
    ))
    assert state.state == "success"


def test_ac_5_in_progress_is_pending():
    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("GitGuardian", status="in_progress",
                           conclusion=None),
                    _check("wheel-ubuntu-24.04", "queued", "queued")])],
        {"state": "pending", "statuses": []},
    ))
    assert state.state == "pending"


def test_ac_5_no_checks_at_all_is_none():
    state, _ = _gh_ci(_CannedGh(
        [_run_page([])], {"state": "", "statuses": []},
    ))
    assert state.state == "none"


def test_ac_5_pagination_walks_every_page():
    """101 checks across two pages are ALL read (dropping pagination
    would silently judge a subset)."""
    page1 = _run_page([_check(f"check-{i:03d}") for i in range(100)],
                      total=101)
    page2 = _run_page([_check(f"check-{i:03d}") for i in range(100, 101)],
                      total=101)
    state, canned = _gh_ci(_CannedGh(
        [page1, page2], _R1_EMPTY_STATUS,
    ))
    assert state.state == "success"
    assert len(state.checks) == 101, "every check must be read"
    check_runs_calls = [c for c in canned.calls if "/check-runs" in " ".join(c)]
    assert len(check_runs_calls) == 2, "both pages were fetched"
    # A failure hidden on page two still fails the whole answer.
    page2_red = _run_page([_check("check-100", "failure")], total=101)
    state, _ = _gh_ci(_CannedGh(
        [page1, page2_red], _R1_EMPTY_STATUS,
    ))
    assert state.state == "failed"


def test_ac_5_latest_attempt_and_exact_sha():
    """The query pins the exact SHA and asks for the latest attempt per
    check name (filter=latest): an older failed attempt behind a green
    latest run reads green, and a run for a different SHA is never
    consulted (the URL cannot name another SHA)."""
    state, canned = _gh_ci(_CannedGh(
        [_run_page([_check("test-ubuntu-24.04 (3.11)", "success")])],
        _R1_EMPTY_STATUS,
    ))
    assert state.state == "success"
    check_calls = [c for c in canned.calls if "/check-runs" in " ".join(c)]
    assert check_calls
    for call in check_calls:
        joined = " ".join(call)
        assert _SHA in joined, "the check-runs URL pins the exact SHA"
        assert "filter=latest" in joined, "latest attempt per check name"
        assert "per_page=100" in joined


def test_ac_5_combined_status_is_read_too():
    """A repo can report through the statuses API: a red combined status
    fails the answer even with green check-runs, and the red context is
    named."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test-ubuntu-24.04 (3.11)", "success")])],
        {"state": "failure",
         "statuses": [{"context": "legacy/ci", "state": "failure"}]},
    ))
    assert state.state == "failed"
    assert ("legacy/ci", "failure") in state.checks


@pytest.mark.parametrize("junk", ["null", "[1, 2]", "<html>login page</html>", ""])
def test_ac_5_rc0_junk_is_unresolvable(junk):
    """rc-0 with null, a list, an HTML page or nothing on stdout is
    unresolvable (the F-45 rule) — never guessed, never success."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([])], {"state": "success"}, raw=junk,
    ))
    assert state.state == "unresolvable"


def test_ac_5_gh_error_is_unresolvable():
    state, _ = _gh_ci(_CannedGh(
        [_run_page([])], {}, rc=1,
    ))
    assert state.state == "unresolvable"


def test_ac_5_gh_missing_refuses():
    gh_ci_state = _ac_import("factory.mergeorder", "gh_ci_state")
    from factory.publish import PublishError

    with pytest.raises(PublishError, match="gh"):
        gh_ci_state(_REMOTE, _SHA, gh_bin=str(Path("/nonexistent/gh")))


def test_ac_5_unknown_completed_conclusion_is_unresolvable():
    """A completed check with no usable conclusion is unknown — never
    counted green (the INC-007 rule: unknown is never ok)."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("mystery", conclusion=None, status="completed")])],
        _R1_EMPTY_STATUS,
    ))
    assert state.state == "unresolvable"


# ============================================================= AC-6 =========
# The host table matches CI exactly — both directions.

_UBUNTU_VERSION = re.compile(r"[Uu]buntu[- ](\d{2}\.\d+)")


def _supported_ubuntu(text: str) -> set[str]:
    """Ubuntu versions a document lists as SUPPORTED: a line naming an
    Ubuntu version that is not a not-supported line. 'Not supported'
    rows never count as listed."""
    supported: set[str] = set()
    for line in text.splitlines():
        found = _UBUNTU_VERSION.search(line)
        if not found:
            continue
        lowered = line.lower()
        if "not supported" in lowered or "not yet" in lowered:
            continue
        supported.add(found.group(1))
    return supported


def _ci_hosts() -> set[str]:
    return {j["runs-on"].removeprefix("ubuntu-")
            for j in _ci_jobs().values()}


def test_ac_6():
    """README's table and CLAUDE.md's host list name exactly the verified
    hosts — macOS (the owner's local gate, stated honestly: CI has no
    macOS job) and Ubuntu 24.04 (CI). 26.04 is listed as NOT supported
    with a pointer to the deferred WO. Both directions hold for Ubuntu:
    every supported version has a CI job on that host, and every CI job
    host is documented as supported."""
    readme = README.read_text()
    claude = CLAUDE_MD.read_text()
    ci_hosts = _ci_hosts()

    for name, text in (("README.md", readme), ("CLAUDE.md", claude)):
        supported = _supported_ubuntu(text)
        missing = supported - ci_hosts
        assert not missing, (
            f"{name} documents Ubuntu {sorted(missing)} as supported but "
            f"no CI job runs on that host (CI runs on {sorted(ci_hosts)})"
        )
        undocumented = ci_hosts - supported
        assert not undocumented, (
            f"{name} does not document CI host(s) Ubuntu "
            f"{sorted(undocumented)} as supported"
        )

    # 26.04 is listed as NOT supported, in both documents.
    for name, text in (("README.md", readme), ("CLAUDE.md", claude)):
        rows = [ln for ln in text.splitlines() if "26.04" in ln]
        assert rows, f"{name} must list Ubuntu 26.04"
        assert any("not supported" in ln.lower() for ln in rows), (
            f"{name}: a 26.04 line must say NOT supported"
        )
    assert DEFERRED_WO in readme and DEFERRED_WO in claude, (
        f"both must point at the deferred work order ({DEFERRED_WO})"
    )

    # macOS is stated honestly: the owner's local gate, not a CI claim.
    mac_rows = [ln for ln in readme.splitlines()
                if ln.strip().startswith("|") and "macOS" in ln]
    assert mac_rows and any("local gate" in ln for ln in mac_rows), (
        "the README macOS row must say the proof is the owner's local "
        "gate (CI has no macOS job)"
    )


# ============================================================= AC-G ========
# GitGuardian green on factory PRs, with no real secret hidden: the fake
# credentials are BUILT at runtime, and a lint keeps new literals out.


def _shannon(s: str) -> float:
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


# A literal starting with one of these prefixes (plus a 16+ body) is a
# planted credential, whatever its entropy.
_TOKEN_PREFIXES = (
    "SEAT-TOKEN-", "seat-tok-", "tok-secret-", "sk_live_", "ghp_", "gho_",
    "ghs_", "github_pat_", "AKIA", "sk-ant-", "xoxb-", "xoxp-",
    "glpat-", "AIza", "eyJ",
)
_HIGH_ENTROPY_RUN = re.compile(r"[A-Za-z0-9_-]{24,}")
# The factory's own run-id timestamp signature: a run carrying it
# (importer-eval-20261108T104244Z-e0b6fc) is a generated identifier,
# not a secret — its entropy is indistinguishable from a token's
# (measured: both ≈4.24), so the shape, not the entropy, is the tell.
_RUN_ID_SIGNATURE = re.compile(r"\d{8}T\d{6}Z")
# The Z.ai/GitHub-app shape: <hex id>.<secret> — both halves matter and
# neither reaches the run threshold alone.
_ID_SECRET = re.compile(r"\b[0-9a-f]{20,}\.[A-Za-z0-9]{16,}\b")


def _token_like(value: str) -> str | None:
    """Why a string (a literal, or any raw line) is token-like, or None.
    Calibrated: catches the old literals (proven by mutation) and a
    realistic random token; passes prose, paths, run ids, hex shas and
    base64 of ordinary text. The prefix rule SEARCHES, not startswith: a
    token quoted mid-line (round 1: the ledger) must flag too."""
    for prefix in _TOKEN_PREFIXES:
        match = re.search(re.escape(prefix) + r"[A-Za-z0-9_-]{16,}", value)
        if match:
            return f"credential prefix {prefix!r}"
    for match in _HIGH_ENTROPY_RUN.finditer(value):
        run = match.group(0)
        if _RUN_ID_SIGNATURE.search(run):
            continue  # a factory run id, not a secret
        if "_" in run or "-" in run:
            # Separators make it a NAME (test_review2_…, importer-eval-…):
            # secret bodies are unbroken blobs. This is what separates a
            # random token from every named identifier in the repo.
            continue
        if _shannon(run) < 4.1:
            continue
        classes = sum((bool(re.search(r"[a-z]", run)),
                       bool(re.search(r"[A-Z]", run)),
                       bool(re.search(r"\d", run))))
        if classes >= 2:
            return f"high-entropy run {run[:12]!r}…"
    if _ID_SECRET.search(value):
        return "id.secret credential shape"
    return None


def test_ac_g():
    """(1) No test source holds a token-like literal: every fake
    credential is built at runtime by tests/_support/tokens.py (no file
    excluded — the helper itself holds no literal either), so no PR adds
    a high-entropy string for GitGuardian to flag. (2) The helper keeps
    the shapes the scrub/detector tests rely on: prefix, length,
    distinctness per label, the JWT's two eyJ segments, the Z.ai
    id.secret key. (3) The old literal bodies are gone from the tree."""
    offenders: list[str] = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as exc:
            offenders.append(f"{path.relative_to(REPO)}: unparsable: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                why = _token_like(node.value)
                if why:
                    offenders.append(
                        f"{path.relative_to(REPO)}:{node.lineno}: {why}")
    assert not offenders, (
        "token-like literals in tests/ (build them at runtime with "
        "tests/_support/tokens.py — a literal here reddens GitGuardian "
        "on every PR that adds it):\n  " + "\n  ".join(offenders)
    )

    # The helper exists, is deterministic per label, and distinct across
    # labels — the properties the scrub tests and mutation proofs use.
    fake_token = _ac_import("_support.tokens", "fake_token")
    fake_jwt = _ac_import("_support.tokens", "fake_jwt")
    fake_zai_key = _ac_import("_support.tokens", "fake_zai_key")
    assert fake_token("label-a") == fake_token("label-a"), \
        "deterministic per label"
    assert fake_token("label-a") != fake_token("label-b"), \
        "distinct per label"
    token = fake_token("shape")
    assert token.startswith("SEAT-TOKEN-") and len(token) == 35, (
        "the seat-token shape (prefix + 24-char body) is what the scrub "
        "tests were written against"
    )
    assert re.fullmatch(r"SEAT-TOKEN-[a-z0-9]{24}", token)

    # The JWT keeps the detector shape: two eyJ-prefixed base64url
    # segments plus a signature — matched by the sentinel's own pattern.
    from factory.sentinels import _CREDENTIAL_SHAPES

    jwt_re = next(p for name, p in _CREDENTIAL_SHAPES
                  if name == "JSON web token")
    jwt = fake_jwt("shape")
    assert jwt_re.search(jwt), f"fake JWT must keep the eyJ shape: {jwt!r}"
    assert jwt.count(".") == 2

    # The Z.ai key keeps <hex id>.<alnum secret>, no eyJ, no known prefix.
    zai = fake_zai_key("shape")
    assert re.fullmatch(r"[0-9a-f]{24}\.[A-Za-z0-9]{20}", zai), zai

    # The old literal bodies are gone from the whole tests tree. They are
    # assembled from fragments here so this file itself holds none of
    # them whole. (f33's 22-letter alphabet fixture line stays by design:
    # below the lint's length threshold, it is the env-exemption corpus,
    # not a credential body.)
    for head, tail in (
        ("zq91wjk", "27fp48dnv"),
        ("f57r2xq", "91wjk27fp48dnv"),
        ("0f1e2d3c4b5a6978", "879601525354555657585960"),
        ("Sk7VmQ2xP9wL4", "TtN6bZ"),
    ):
        body = head + tail
        hits = [p for p in (REPO / "tests").rglob("*.py")
                if body in p.read_text()]
        assert not hits, f"old literal body still in {hits}"


def test_ac_g_no_gitguardian_ignore_file():
    """The fix hides nothing: no .gitguardian.yaml exists, so a REAL
    secret added anywhere — factory/ included — is still flagged by
    GitGuardian. (Verified by absence; the proof of green is this WO's
    own PR, recorded in the ledger as owner evidence.)"""
    assert not (REPO / ".gitguardian.yaml").exists(), (
        "a .gitguardian.yaml was added without a ledgered reason — the "
        "preferred fix needs none"
    )


# ============================================================ round 1 ======
# Reviewer round 1 (2026-11-08): real-GitHub shapes, per-rule lint teeth,
# the peak-window ordering, and repo-wide literal hygiene.

# Trimmed from the reviewer's recorded REAL gh responses (/tmp/f58gh/,
# 2026-11-08; only the fields classify_ci reads — no ids, no urls). The
# green commit is fddca65 (every check green); the red one fd041c2
# (GitGuardian failure). Canned shapes that GitHub never returns are how
# R1-1 slipped through: the fixtures below are the recorded truth.
_R1_GREEN_CHECKS = [
    {"name": "test (3.13)", "status": "completed", "conclusion": "success"},
    {"name": "wheel", "status": "completed", "conclusion": "success"},
    {"name": "live-network", "status": "completed", "conclusion": "success"},
    {"name": "test (3.11)", "status": "completed", "conclusion": "success"},
    {"name": "typecheck", "status": "completed", "conclusion": "success"},
]
_R1_GG_RED_CHECKS = [
    {"name": "wheel", "status": "completed", "conclusion": "success"},
    {"name": "test (3.11)", "status": "completed", "conclusion": "success"},
    {"name": "typecheck", "status": "completed", "conclusion": "success"},
    {"name": "test (3.13)", "status": "completed", "conclusion": "success"},
    {"name": "live-network", "status": "completed", "conclusion": "success"},
    {"name": "GitGuardian Security Checks", "status": "completed",
     "conclusion": "failure"},
]


def test_r1_real_green_reads_success():
    """R1-1: the recorded real green pair — five green check-runs plus a
    zero-entry 'pending' status rollup — reads SUCCESS. A commit with no
    legacy statuses contributes nothing through the status API."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page(_R1_GREEN_CHECKS, total=5)], _R1_EMPTY_STATUS,
    ))
    assert state.state == "success", (
        "a green commit with no legacy statuses must read success — "
        "reading the rollup made every PR unreadable as ready"
    )


def test_r1_real_gitguardian_red_reads_failed():
    """R1-1: the recorded real red pair — GitGuardian's check failed —
    reads FAILED, naming the check. R2-2: the fixture is the recorded
    document WHOLE — six checks including typecheck, total_count 6 —
    exactly as GitHub answered for fd041c2."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page(_R1_GG_RED_CHECKS, total=6)], _R1_EMPTY_STATUS,
    ))
    assert state.state == "failed"
    assert ("GitGuardian Security Checks", "failure") in state.checks
    assert len(state.checks) == 6, (
        "the recorded red answer carries all six checks"
    )


def test_r1_status_entries_are_the_truth_not_the_rollup():
    """R1-1/R1-5: the status API is judged by its ENTRIES. A failure
    entry fails, a pending entry pends, a success entry is green, an
    unknown entry state refuses; the rollup never classifies on its own."""

    def entry(st):
        return {"context": "legacy/ci", "state": st}

    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test (3.11)")])],
        {"state": "success", "statuses": [entry("failure")], "total_count": 1},
    ))
    assert state.state == "failed"
    assert ("legacy/ci", "failure") in state.checks

    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test (3.11)")])],
        {"state": "failure", "statuses": [entry("pending")], "total_count": 1},
    ))
    assert state.state == "pending"

    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test (3.11)")])],
        {"state": "pending", "statuses": [entry("success")], "total_count": 1},
    ))
    assert state.state == "success"

    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test (3.11)")])],
        {"state": "success", "statuses": [entry("weird")], "total_count": 1},
    ))
    assert state.state == "unresolvable"


@pytest.mark.parametrize("bad_statuses", [
    ["not-a-dict"],
    [{"state": "success"}],          # no context
    [{"context": "legacy/ci"}],      # no state
    "not-a-list",
])
def test_r1_malformed_status_entries_refuse(bad_statuses):
    """R1-5: a non-dict status entry, an entry without context or state,
    or a non-list statuses field is UNRESOLVABLE — never silently
    skipped (unknown is never ok)."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test (3.11)")])],
        {"state": "pending", "statuses": bad_statuses,
         "total_count": 1},
    ))
    assert state.state == "unresolvable"


@pytest.mark.parametrize("status", ["waiting", "requested", "pending"])
def test_r1_awaiting_check_statuses_are_pending(status):
    """R1-4: `waiting`, `requested` and `pending` are valid GitHub
    check-run statuses (e.g. a required environment approval): they are
    PENDING, not unresolvable."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("deploy-gate", status=status, conclusion=None)])],
        _R1_EMPTY_STATUS,
    ))
    assert state.state == "pending"


def test_r1_ci_guard_respects_the_peak_window(tmp_path: Path):
    """R1-6: inside the peak window the run makes NO gh lookups — the
    F-26 round-1 finding-5 invariant (no gate work, no gh, no
    escalations in-window). A red default branch is enforced at the
    first off-peak tick, not by an in-window refusal; off-peak it still
    refuses before anything is acted on or recorded."""
    env = Env(tmp_path, tasks=[_task()], unavailable=True)
    red = CIRecorder(default=_ci("failed",
                                 checks=(("live-network", "failed"),)))
    tuesday = datetime(2026, 1, 20, 10, 0,
                       tzinfo=ZoneInfo("Europe/Stockholm"))  # in 08–12
    result = env.run(ci_state=red, now=lambda: tuesday)
    assert result.outcome == "peak_window"
    assert red.calls == [], "no gh lookup may happen inside the window"

    with pytest.raises(RunError, match="failing CI"):
        env.run(ci_state=CIRecorder(default=_ci(
            "failed", checks=(("live-network", "failed"),))))


# --- R1-2/R1-3: the lint covers the whole repo, and each rule has teeth --

from fnmatch import fnmatch  # noqa: E402

# NAMED exclusions, each with its stated reason (never factory/**: a real
# secret in factory code or config must always be visible to the lint).
_LINT_EXCLUDES = {
    # The F-25/F-33 attack corpus deliberately embeds credential-shaped
    # strings: it IS the sentinel detector's test data, reviewed as such.
    "corpus/**": "attack corpus: credential-shaped by design",
    # npm's own machine-generated integrity hashes (sha512-… base64):
    # lockfile metadata, not secrets. The only file in the repo whose
    # every long unbroken run is a checksum.
    "package-lock.json": "npm integrity hashes, machine-generated",
}


def _lint_exempt(rel_path: str) -> bool:
    return any(fnmatch(rel_path, pat) for pat in _LINT_EXCLUDES)


def _tracked_text_files() -> list:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                         capture_output=True, check=True)
    files = []
    for raw in out.stdout.decode().split("\0"):
        if not raw:
            continue
        try:
            (REPO / raw).read_text()
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — not text GitGuardian parses
        files.append(raw)
    return files


def _repo_token_offenders() -> list:
    """Token-like lines in every tracked text file: .py through the ast
    (string literals, precise) plus a raw-line pass over EVERY file
    (markdown, yaml, json — GitGuardian scans raw lines, and a literal
    quoted in prose is exactly what R1-2 caught in the ledger)."""
    offenders: list[str] = []
    for rel in _tracked_text_files():
        if _lint_exempt(rel):
            continue
        path = REPO / rel
        if rel.endswith(".py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)):
                    why = _token_like(node.value)
                    if why:
                        offenders.append(f"{rel}:{node.lineno}: {why}")
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            why = _token_like(line)
            if why:
                offenders.append(f"{rel}:{lineno}: {why}")
    return sorted(set(offenders))


def test_r1_no_token_literal_anywhere_in_the_repo():
    """R1-2: GitGuardian scans every added line of every file, so the
    lint must too — not just tests/*.py. (Round 1 caught the §F-58
    ledger quoting the very token the helper removed: fixed with it.)"""
    offenders = _repo_token_offenders()
    assert not offenders, (
        "token-like literals in tracked files (build them at runtime; "
        "describe removed ones, never quote them):\n  "
        + "\n  ".join(offenders)
    )


def test_r1_lint_rules_each_have_teeth():
    """R1-3: each _token_like rule catches its own shape (positives
    assembled from fragments so this file stays clean) and each
    documented negative passes — per-rule teeth, not just the
    combination on a clean tree."""
    # prefix rule
    prefixed = "SEAT-" + "TOKEN-" + "qz91wjk" + "27fp48dnv0xy"
    assert _token_like(prefixed), "prefix rule must catch a planted prefix"
    assert "prefix" in _token_like(prefixed)
    # generic high-entropy run rule
    randomish = "Zq8" + "mJp2vN4x" + "Kw7RtY3u" + "B6sH1dL0fG5cX"
    assert _token_like(randomish), "entropy rule must catch a random token"
    assert "entropy" in _token_like(randomish)
    # id.secret shape rule
    id_secret = ("d1f4a9c2e8b3" + "7d65f0a2" + "."
                 + "Sk7VmQ2xP9wL4" + "TtN6bZ")
    assert _token_like(id_secret), "id.secret rule must catch the shape"
    assert "id.secret" in _token_like(id_secret)

    # negatives: generated identifiers, not secrets
    import hashlib

    sha40 = hashlib.sha256(b"round1").hexdigest()[:40]
    assert _token_like(sha40) is None, "a 40-hex sha is not a token"
    assert _token_like("importer-eval-20261108T104244Z-e0b6fc") is None, (
        "a factory run id (timestamp signature) is not a token"
    )
    assert _token_like("/opt/conda/envs/factory/bin/python3") is None
    assert _token_like(str(sha40)) is None
    assert _token_like("user@example.com") is None
    uuid_like = "3f2a" + "1c9e-4b8d" + "-a7f2-9c1d" + "-e5b6a8f7c3d2"
    assert _token_like(uuid_like) is None, "a uuid is not a token"
    # npm integrity hashes are machine-generated checksums, and their
    # base64 pieces are unbroken 24+ runs — indistinguishable from a
    # token BY SHAPE, so they are handled by the NAMED file exclusion,
    # not by a value exemption: outside package-lock.json such a run
    # still flags (honest — GitGuardian would flag it too).
    assert _lint_exempt("package-lock.json")
    assert not _lint_exempt("factory/session.py"), (
        "factory/** may never be lint-exempt"
    )
    assert not _lint_exempt("tests/test_f43.py")


# ============================================================ round 2 ======
# Reviewer round 2 (2026-11-08): the failed-beats-pending priority is
# pinned, and the red fixture is the recording whole.

def test_r2_failed_beats_pending():
    """R2-1: a FAILED check plus an in_progress check on the same SHA
    reads FAILED, not pending — GitHub's own combined rollup calls such a
    commit "failure". If a refactor flips this order, a red main with one
    concurrently re-running check reads "pending", the run-start guard
    PROCEEDS, and `factory run` starts over red CI — the exact incident
    class this WO closes (the sweep side is fail-safe either way; the
    guard is the hole)."""
    state, _ = _gh_ci(_CannedGh(
        [_run_page([
            _check("test-ubuntu-24.04 (3.11)", "failure"),
            _check("live-network-ubuntu-24.04", status="in_progress",
                   conclusion=None),
        ])],
        _R1_EMPTY_STATUS,
    ))
    assert state.state == "failed", (
        "a failure concurrent with a re-run must read failed — pending "
        "would let the run-start guard proceed over red CI"
    )
    # And the same pair through a legacy status entry: failure entry +
    # pending entry → failed.
    state, _ = _gh_ci(_CannedGh(
        [_run_page([_check("test-ubuntu-24.04 (3.11)", "success")])],
        {"state": "pending",
         "statuses": [{"context": "legacy/ci", "state": "failure"},
                      {"context": "legacy/other", "state": "pending"}],
         "total_count": 2},
    ))
    assert state.state == "failed"
