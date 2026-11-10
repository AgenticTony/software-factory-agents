"""F-45 acceptance tests — BUILD-ORDER.md, work order F-45 (pre-lease,
publish and crash-window robustness).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f45.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the fixture project's git repo and bare "GitHub" remote, the
  queue and the run lease (real ``flock``), the stale-PR sweep (real
  clones/fetches/merges and real sandboxed L0/L1 re-validation), the real
  ``git`` binary pushing against a real TCP server that accepts and never
  answers (AC-3), the hash-chained progress log (AC-2/AC-4), and the real
  atomic queue write with its rename (AC-6).
* **Stubbed:** the model (seat stubs, the f10/f26 pattern — no API spend),
  ``gh`` (a fake binary: one that prints a URL fast, one that hangs), the
  ``gh pr view``/``gh api`` subprocess seam (canned stdout, AC-5), and the
  branch-protection admission call.

Spec note (F-45 §AC-7): ``docs/remediation-plan-2026-11-05.md`` adds "the
repo's stale run.lock content is cleared" to AC-7; ``.factory/tasks.json``
(the authoritative contract) does not. The repo's real stale lock content
— the literal string ``reclaim`` — is used here as a garbage-content input
and is NOT cleared by this suite or by the fix.
"""

import contextlib
import fcntl
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import textwrap
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml
from _support.f63 import ledger_approve

from factory import orchestrator as orch
from factory import publish
from factory.contract import acceptance_hash
from factory.mergeorder import Protection, classify_protection, gh_pr_state
from factory.progress import ProgressLog, ProgressLogError
from factory.publish import PublishError, PullRequest, gh_pr_create, push_branch
from factory.runs import RunError
from factory.session import SessionRecord
from factory.state import RunInProgressError, TaskQueue

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)
REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = textwrap.dedent(
    """
    from impl import answer

    def test_ac_1():
        assert answer() == 42
    """
)
REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", *, ref: str = REF, **overrides) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42", "check": {"type": "test", "ref": ref}}
    ]
    task = {
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
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 30},
        "scope": {"paths": ["impl.py", "tests/test_feature.py"]},
        "attempts": [],
    }
    task.update(overrides)
    return task


class Env:
    """One fixture project: local repo + bare remote + factory root."""

    def __init__(self, tmp_path: Path, tasks: list | None = None):
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
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
        self.profile = {
            "name": "fixture",
            "repo": {
                "path": str(self.repo),
                "default_branch": "main",
                "remote": str(self.remote),
                "merge_method": "squash",
            },
            "commands": {
                "setup": "true",
                "test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
            },
            "paths": {"risk": {"payments": ["src/pay/**"]}, "protected": [".factory/**"]},
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
        doc = {"version": 1, "project": "fixture", "tasks": tasks or [_task()]}
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)], check=True
        )
        self.prs: list[PullRequest] = []

    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self, task_id: str = "T-1") -> dict:
        return TaskQueue(self.queue_path).get(task_id)

    def log(self, task_id: str = "T-1") -> ProgressLog:
        return ProgressLog.for_task(self.root, "fixture", task_id)

    def run(self, session_runner, **kwargs):
        # F-63: record the fixture's owner approvals (profile + frozen
        # tasks) before the run — the ledger every run consults.
        ledger_approve(self.root, self.queue_path)
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", lambda r, b: Protection(checked=True, strict=True))
        kwargs.setdefault("pr_state", lambda url: "open")
        return orch.run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner,
            open_pr=self.open_pr,
            **kwargs,
        )

    def advance_remote_main(self, impl_body: str | None, *, extra_file: str | None = None,
                            message: str = "remote moves on") -> str:
        """Commit to the remote's main and return its new head sha.

        ``impl_body`` rewrites impl.py (pass a conflicting body to force a
        merge conflict); ``extra_file`` adds an unrelated file instead.
        """
        scratch = self.tmp / f"advance-{message.replace(' ', '-')}"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(scratch)], check=True)
        if impl_body is not None:
            (scratch / "impl.py").write_text(impl_body)
        if extra_file is not None:
            (scratch / extra_file).write_text("unrelated remote change\n")
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "add", "-A")
        _git(scratch, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", message)
        _git(scratch, "push", "-q", "origin", "HEAD:main")
        return _git(self.remote, "rev-parse", "refs/heads/main")


class Seats:
    """Stub seats with ``run_session_sandboxed``'s signature (f10 pattern)."""

    def __init__(self, review=REVIEW_PASS):
        self.review = review
        self.calls: list[tuple[str, str]] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        self.calls.append((seat, prompt))
        run_dir = Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests").mkdir(exist_ok=True)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(seat=seat, status="completed", num_turns=3,
                                 model="stub-model", result="Implemented answer().")
        return SessionRecord(seat=seat, status="completed", num_turns=1,
                             model="stub-model", result=self.review)


class HangingServer:
    """A TCP server that accepts connections and never answers (AC-3).

    The git/gh client blocks on the response forever — the exact hang the
    timeout wrapper must cut off. Deterministic: no sleeps drive the
    assertion; the client simply never hears from us.
    """

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self._conns.append(conn)  # held open, never answered

    def url(self, path: str = "hang.git") -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def close(self):
        self._stop.set()
        self.thread.join(timeout=5)
        for conn in self._conns:
            with contextlib.suppress(OSError):
                conn.close()
        self.sock.close()


def _tiny_repo(path: Path) -> str:
    """A one-commit repo with branch ``factory/x``; returns its head sha."""
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "fixture@example.com")
    _git(path, "config", "user.name", "Fixture")
    (path / "impl.py").write_text("x = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    _git(path, "checkout", "-q", "-b", "factory/x")
    (path / "impl.py").write_text("x = 2\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "change")
    return _git(path, "rev-parse", "HEAD")


def _run_in_thread(fn, timeout: float) -> tuple[threading.Thread, dict]:
    """Run ``fn`` in a daemon thread; return (thread, box) with exc/ok inside."""
    box: dict = {}

    def go():
        try:
            box["ok"] = fn()
        except BaseException as exc:  # noqa: BLE001 — recorded, re-asserted
            box["exc"] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    return thread, box


def _patched_for_task(real_for_task, error):
    """A ProgressLog.for_task classmethod whose append raises ``error``.

    Models the two anticipated failure classes at the same seam: OSError
    (a full disk) and ProgressLogError (a rewritten/corrupt hash chain —
    exactly what ProgressLog._read raises when the chain is broken).
    """

    def for_task(cls, factory_root, project, task_id):
        log = real_for_task(cls, factory_root, project, task_id)

        def append(_entry):
            raise error

        log.append = append
        return log

    return classmethod(for_task)


# ------------------------------------------------------------------ AC-1 ---


def test_ac_1(tmp_path: Path, monkeypatch):
    """M-5: the stale-PR sweep runs under ``queue.idle_lease()`` — a
    concurrent ``reclaim_runs`` cannot delete the sweep's live run dirs.

    Deterministic sync (no sleep-and-hope): ``create_run`` is frozen by
    events while the sweep's scratch clone exists, and the main thread's
    ``reclaim_runs`` runs against that frozen window. The lease is an
    ``flock`` on a separate fd, so two threads in one process contend
    exactly as two processes do.
    """
    env = Env(tmp_path)  # one task: the sweep runs on the idle tick
    assert env.run(Seats()).outcome == "pr_opened"
    assert env.task()["status"] == "in_review"
    # An unrelated remote advance: the PR is stale, so the sweep MUST clone
    # (create_run) before it can conclude anything.
    env.advance_remote_main(None, extra_file="remote_only.txt")

    created = threading.Event()
    release = threading.Event()
    real_create_run = orch.create_run

    def frozen_create_run(*args, **kwargs):
        run = real_create_run(*args, **kwargs)
        created.set()
        release.wait(timeout=60)
        return run

    monkeypatch.setattr(orch, "create_run", frozen_create_run)

    box: dict = {}

    def target():
        try:
            box["result"] = env.run(Seats())
        except BaseException as exc:  # noqa: BLE001 — asserted below
            box["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        # The sweep's run dir exists while the sweep is mid-flight: this is
        # exactly the window M-5 names.
        assert created.wait(timeout=60), "the sweep never created its run dir"
        runs_dir = env.root / "runs" / "fixture"
        live = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
        assert live, "the sweep's live run dir vanished before reclaim ran"

        removed = orch.reclaim_runs(TaskQueue(env.queue_path), "fixture", env.root)
        # THE assertion: reclaim saw a live (leased) run and deleted nothing.
        assert removed == [], f"reclaim_runs deleted live run dirs: {removed}"
        assert sorted(p.name for p in runs_dir.iterdir() if p.is_dir()) == live
    finally:
        release.set()
        thread.join(timeout=180)
    assert not thread.is_alive()
    assert "exc" not in box, repr(box.get("exc"))
    assert "result" in box
    # The sweep completed on its own clone: the PR was re-validated and the
    # idle tick resolved without touching a task (nothing to pick).
    assert box["result"].outcome == "idle"
    assert box["result"].ready_prs, "the re-validated PR should be reported ready"
    assert env.task()["status"] == "in_review"

    # Mechanical pins on the fix itself, so a refactor cannot silently
    # drop them:
    src = (REPO_ROOT / "factory" / "orchestrator.py").read_text()
    call = src.index("stale = _sweep_stale_prs(")
    assert "idle_lease" in src[call - 400:call], (
        "the _sweep_stale_prs call in run_once must run under queue.idle_lease()"
    )
    # AC-1's writer sweep as a pin: create_run's only call sites in the
    # orchestrator are the sweep (leased) and _run_task (inside begin_run);
    # the importer's call sits inside its own idle_lease.
    assert src.count("create_run(") == 4, (
        "create_run gained or lost a call site: every writer under runs/ "
        "must hold the run lease (sweep under idle_lease, _run_task under "
        "begin_run)"
    )
    assert "idle_lease" in (REPO_ROOT / "factory" / "importer.py").read_text()


# ------------------------------------------------------------------ AC-2 ---


def test_ac_2(tmp_path: Path, monkeypatch):
    """M-6: a failing progress-log append in ``_sweep_escalation`` no longer
    wedges the task in ``in_review`` — it reaches ``needs_human`` and the
    next run proceeds. Failures injected at each step of the escalation
    (append_attempt, the log append — OSError AND a real corrupt hash chain
    — and the transition); in every case the queue stays consistent and the
    next run proceeds.
    """
    real_for_task = ProgressLog.for_task.__func__

    # --- leg A: the log append dies on OSError (disk full) ---
    env = Env(tmp_path / "a")
    assert env.run(Seats()).outcome == "pr_opened"
    env.advance_remote_main("def answer():\n    return 99\n", message="conflicting change")

    monkeypatch.setattr(
        ProgressLog, "for_task", _patched_for_task(real_for_task, OSError("disk full")))
    raised = None
    try:
        result = env.run(Seats())
    except Exception as exc:  # noqa: BLE001 — the wedge, re-asserted below
        raised = exc
    monkeypatch.undo()
    if raised is not None:
        # The wedge M-6 names: the append crashed the run BEFORE the
        # transition, so the task is stuck in in_review and every later run
        # re-validates, fails at the same append, and crashes again.
        assert env.task()["status"] == "needs_human", (
            f"a failing progress-log append wedged the sweep: task is "
            f"{env.task()['status']} ({raised!r})"
        )
    assert raised is None
    assert result.outcome == "needs_human"
    assert result.escalation == "stale_base"
    task = env.task()
    assert task["status"] == "needs_human"
    assert task["attempts"][-1]["escalation"] == "stale_base"
    # The next run proceeds: nothing is wedged (recovery scan + idle tick).
    assert env.run(Seats()).outcome == "idle"

    # --- leg B: a REAL corrupt hash chain (a rewritten log on disk) ---
    env_b = Env(tmp_path / "b")
    assert env_b.run(Seats()).outcome == "pr_opened"
    env_b.advance_remote_main("def answer():\n    return 99\n", message="conflicting change")
    log_path = env_b.root / "progress" / "fixture" / "T-1.jsonl"
    lines = log_path.read_text().splitlines()
    record = json.loads(lines[0])
    record["prev"] = "f" * 64  # no longer chains to genesis
    lines[0] = json.dumps(record, sort_keys=True, ensure_ascii=False)
    log_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ProgressLogError):
        env_b.log().entries()  # the corruption is real

    raised_b = None
    try:
        result_b = env_b.run(Seats())
    except ProgressLogError as exc:
        raised_b = exc
    if raised_b is not None:
        assert env_b.task()["status"] == "needs_human", (
            f"a corrupt hash chain wedged the sweep: {env_b.task()['status']}"
        )
    assert raised_b is None
    assert result_b.outcome == "needs_human"
    assert env_b.task()["status"] == "needs_human"

    # --- leg C: append_attempt itself dies (nothing recorded, queue intact) ---
    env_c = Env(tmp_path / "c")
    assert env_c.run(Seats()).outcome == "pr_opened"
    env_c.advance_remote_main("def answer():\n    return 99\n", message="conflicting change")

    def append_attempt_dead(self, task_id, attempt):
        raise OSError("queue write failed")

    monkeypatch.setattr(TaskQueue, "append_attempt", append_attempt_dead)
    with pytest.raises(OSError):
        env_c.run(Seats())
    monkeypatch.undo()
    task_c = env_c.task()  # reloads cleanly: the queue is consistent
    assert task_c["status"] == "in_review"
    assert len(task_c["attempts"]) == 1  # only the original pass attempt
    # The next run proceeds and the escalation lands properly.
    result_c = env_c.run(Seats())
    assert result_c.outcome == "needs_human"
    assert env_c.task()["attempts"][-1]["escalation"] == "stale_base"

    # --- leg D: the transition dies (attempt recorded, queue intact) ---
    env_d = Env(tmp_path / "d")
    assert env_d.run(Seats()).outcome == "pr_opened"
    env_d.advance_remote_main("def answer():\n    return 99\n", message="conflicting change")
    real_transition = TaskQueue.transition

    def transition_dead(self, task_id, new_status):
        if new_status == "needs_human":
            raise OSError("queue write failed")
        return real_transition(self, task_id, new_status)

    monkeypatch.setattr(TaskQueue, "transition", transition_dead)
    with pytest.raises(OSError):
        env_d.run(Seats())
    monkeypatch.undo()
    task_d = env_d.task()
    assert task_d["status"] == "in_review"  # the transition never landed
    assert task_d["attempts"][-1]["escalation"] == "stale_base"  # but the attempt did
    # The next run proceeds: re-validates, fails again, escalates cleanly.
    result_d = env_d.run(Seats())
    assert result_d.outcome == "needs_human"
    assert env_d.task()["status"] == "needs_human"


# ------------------------------------------------------------------ AC-3 ---


def test_ac_3(tmp_path: Path, monkeypatch):
    """M-7: publish-side ``git push``, the ls-remote probe and ``gh pr
    create`` run prompt-disabled and timed through the SAME wrapper as
    ``_fetch_sha``. A real hanging remote (a server that accepts and never
    answers) yields a timed-out ``PublishError``.

    gh does not honour GIT_TERMINAL_PROMPT: gh calls go out with
    GH_PROMPT_DISABLED=1 as well (gh's own switch — ``gh help environment``
    documents it as "set to any value to disable interactive prompting in
    the terminal"), and the wrapper's timeout is the provable backstop:
    whatever gh does — auth prompt, device flow, a hung API — the call
    cannot outlive it. The hanging fake gh below proves exactly that.
    """
    repo = tmp_path / "repo"
    sha = _tiny_repo(repo)

    # --- leg 1: git push against a real never-answering server ---
    server = HangingServer()
    try:
        # The wrapper resolves its default at call time, so the test can
        # shrink it (raising=False keeps the RED run meaningful: the current
        # code ignores the attribute entirely and just hangs).
        monkeypatch.setattr(publish, "NET_TIMEOUT_SEC", 4, raising=False)
        thread, box = _run_in_thread(
            lambda: push_branch(
                repo, server.url(), "factory/x", sha=sha,
                created={"factory/x"}, default_branch="main",
            ),
            timeout=20,
        )
        assert not thread.is_alive(), (
            "git push hung on a server that never answers: the publish path "
            "has no timeout and holds the run lease forever (M-7)"
        )
        assert isinstance(box.get("exc"), PublishError), box
        assert "timed out" in str(box["exc"])
    finally:
        server.close()

    # --- leg 2: gh pr create against a hanging fake gh binary ---
    fake_gh = tmp_path / "gh-hang"
    fake_gh.write_text("#!/bin/sh\nsleep 60\n")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(publish, "NET_TIMEOUT_SEC", 3, raising=False)
    thread, box = _run_in_thread(
        lambda: gh_pr_create(
            PullRequest(remote="https://github.com/fixture/fixture", base="main",
                        head="factory/x", title="t", body="b"),
            gh_bin=fake_gh,
        ),
        timeout=15,
    )
    assert not thread.is_alive(), "gh pr create hung: no timeout on the gh call (M-7)"
    assert isinstance(box.get("exc"), PublishError), box
    assert "timed out" in str(box["exc"])

    # --- leg 3: every publish-side remote call routes through the ONE
    # wrapper, prompt-disabled and timed. Real pushes against a real local
    # remote, watched at the subprocess boundary (spies active only around
    # the call under test). ---
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(remote)], check=True)
    seen: list[tuple[list, dict]] = []
    real_run = publish.subprocess.run

    def run_spy(cmd, **kwargs):
        seen.append((list(cmd), kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", run_spy)
    push_branch(repo, str(remote), "factory/x", sha=sha,
                created={"factory/x"}, default_branch="main")
    monkeypatch.undo()

    push_cmds = [cmd for cmd, _ in seen if "push" in cmd]
    probe_cmds = [cmd for cmd, _ in seen if "ls-remote" in cmd]
    assert push_cmds and probe_cmds, "push and the ls-remote probe must both run"
    for cmd, kw in seen:
        assert kw.get("timeout") is not None, f"untimed remote call: {cmd}"
        assert kw["env"].get("GIT_TERMINAL_PROMPT") == "0", cmd

    # gh pr create through a fast fake gh: same wrapper, gh's prompt switch.
    fast_gh = tmp_path / "gh-fast"
    fast_gh.write_text("#!/bin/sh\necho 'https://github.com/fixture/fixture/pull/7'\n")
    fast_gh.chmod(fast_gh.stat().st_mode | stat.S_IXUSR)
    seen.clear()
    monkeypatch.setattr(publish.subprocess, "run", run_spy)
    monkeypatch.setattr(publish, "NET_TIMEOUT_SEC", 30, raising=False)
    url = gh_pr_create(
        PullRequest(remote="https://github.com/fixture/fixture", base="main",
                    head="factory/x", title="t", body="b"),
        gh_bin=fast_gh,
    )
    monkeypatch.undo()
    assert url.endswith("/pull/7")
    gh_runs = [(cmd, kw) for cmd, kw in seen if "pr" in cmd and "create" in cmd]
    assert gh_runs, "gh pr create ran no subprocess through the wrapper"
    for cmd, kw in gh_runs:
        assert kw.get("timeout") is not None, f"untimed gh call: {cmd}"
        assert kw["env"].get("GIT_TERMINAL_PROMPT") == "0", cmd
        assert kw["env"].get("GH_PROMPT_DISABLED") == "1", cmd

    # --- leg 4: _fetch_sha uses the same wrapper (not a second mechanism).
    # Spied at the orchestrator's imported name — the boundary _fetch_sha
    # actually calls through. ---
    wrapped: list[list] = []
    real_remote_call = orch.remote_call

    def remote_call_spy(cmd, **kwargs):
        wrapped.append(list(cmd))
        return real_remote_call(cmd, **kwargs)

    monkeypatch.setattr(orch, "remote_call", remote_call_spy)
    fetched = orch._fetch_sha(repo, str(remote), "main")
    assert _git(remote, "rev-parse", "refs/heads/main") == fetched
    monkeypatch.undo()
    assert any("fetch" in cmd for cmd in wrapped), (
        "_fetch_sha must route through the shared remote_call wrapper — "
        "one wrapper, not a second mechanism"
    )
    # ... and the wrapper it routes through is publish's one (not a local
    # twin): the orchestrator imports the very object publish's calls use.
    assert orch.remote_call is publish.remote_call
    # A timed-out fetch maps to RunError (the orchestrator's error currency).
    server2 = HangingServer()
    try:
        monkeypatch.setattr(publish, "NET_TIMEOUT_SEC", 3, raising=False)
        thread, box = _run_in_thread(
            lambda: orch._fetch_sha(repo, server2.url(), "main"), timeout=15)
        assert not thread.is_alive(), "an orchestrator fetch hung untimed"
        assert isinstance(box.get("exc"), RunError), box
        assert "timed out" in str(box["exc"])
    finally:
        server2.close()


# ------------------------------------------------------------------ AC-4 ---


def test_ac_4(tmp_path: Path, monkeypatch):
    """m-13: ONE finish() failure records EXACTLY ONE attempt — injected at
    every point inside finish() (append_attempt, the log append — OSError
    and a corrupt-chain ProgressLogError — trace.run_end, and the
    transition), not only between append_attempt and the transition.

    The failing-review fixture is f10's: the implementer rewrites impl.py
    every round (a real delta), reviewers fail with a per-round finding,
    and the budget (max_attempts=2) ends the task on round 2.
    """
    from factory.trace import TraceWriter

    def fail_seats():
        lock = threading.Lock()
        calls: list[tuple[str, str]] = []

        def varying(seat, prompt, run_dir, profile, **kwargs):
            if seat == "implementer":
                with lock:
                    calls.append((seat, prompt))
                    round_no = sum(1 for s, _ in calls if s == "implementer")
                (Path(run_dir) / "impl.py").write_text(
                    f"def answer():\n    return 42  # fix round {round_no}\n")
                (Path(run_dir) / "tests").mkdir(exist_ok=True)
                (Path(run_dir) / "tests" / "test_feature.py").write_text(TEST_SOURCE)
                return SessionRecord(seat=seat, status="completed", num_turns=3,
                                     model="stub-model", result="Implemented.")
            with lock:
                calls.append((seat, prompt))
                round_no = sum(1 for s, _ in calls if s == "implementer")
            return SessionRecord(
                seat=seat, status="completed", num_turns=1, model="stub-model",
                result=json.dumps({"verdict": "fail", "findings": [
                    {"id": "R1", "axis": "spec", "severity": "blocking",
                     "file": "impl.py", "text": f"scope creep {round_no}"}]}),
            )

        return varying

    def budget_task():
        return _task(budget={"size": "S", "max_attempts": 2, "max_turns": 20,
                             "wall_clock_min": 30})

    def outcomes(env):
        return [a["outcome"] for a in env.task()["attempts"]]

    real_for_task = ProgressLog.for_task.__func__
    real_transition = TaskQueue.transition
    real_append = TaskQueue.append_attempt
    real_run_end = TraceWriter.run_end

    # --- leg A: the log append inside finish() dies on OSError ---
    env = Env(tmp_path / "a", tasks=[budget_task()])
    monkeypatch.setattr(
        ProgressLog, "for_task",
        _patched_for_task(real_for_task, OSError("disk full")))
    raised = None
    try:
        result = env.run(fail_seats())
    except Exception as exc:  # noqa: BLE001
        raised = exc
    monkeypatch.undo()
    if raised is not None or outcomes(env) != ["fail", "escalated"]:
        pytest.fail(
            f"a finish() log-append failure double-spent the budget: "
            f"attempts {outcomes(env)} (raised {raised!r}) — one failure "
            f"must record exactly one attempt per finish() call"
        )
    assert result.outcome == "needs_human"
    assert result.escalation == "attempts_exhausted"
    assert env.task()["status"] == "needs_human"
    # The log append is telemetry (best-effort): both entries were lost to
    # the injected OSError, and nothing else was.
    assert env.log().entries() == []

    # --- leg B: the log append dies with a corrupt-chain ProgressLogError ---
    env_b = Env(tmp_path / "b", tasks=[budget_task()])
    monkeypatch.setattr(
        ProgressLog, "for_task",
        _patched_for_task(real_for_task,
                          ProgressLogError("entry 1 does not chain to entry 0")))
    raised_b = None
    try:
        result_b = env_b.run(fail_seats())
    except Exception as exc:  # noqa: BLE001
        raised_b = exc
    monkeypatch.undo()
    assert raised_b is None, repr(raised_b)
    assert outcomes(env_b) == ["fail", "escalated"]
    assert result_b.outcome == "needs_human"

    # --- leg C: trace.run_end dies inside finish() (between the append and
    # the transition) ---
    env_c = Env(tmp_path / "c", tasks=[budget_task()])
    fired: list[int] = []

    def run_end_dead_once(self, outcome, escalation=None):
        if not fired:
            fired.append(1)
            raise RuntimeError("trace fsync died")
        return real_run_end(self, outcome, escalation=escalation)

    monkeypatch.setattr(TraceWriter, "run_end", run_end_dead_once)
    raised_c = None
    try:
        result_c = env_c.run(fail_seats())
    except Exception as exc:  # noqa: BLE001
        raised_c = exc
    monkeypatch.undo()
    if raised_c is not None or outcomes(env_c) != ["fail", "escalated"]:
        pytest.fail(
            f"a finish() trace failure double-spent the budget: attempts "
            f"{outcomes(env_c)} (raised {raised_c!r})"
        )
    assert result_c.outcome in ("needs_human", "error")
    assert env_c.task()["status"] in ("needs_human", "ready")

    # --- leg D: the transition inside finish() dies ---
    env_d = Env(tmp_path / "d", tasks=[budget_task()])

    def transition_dead_for_needs_human(self, task_id, new_status):
        if new_status == "needs_human":
            raise OSError("queue write failed")
        return real_transition(self, task_id, new_status)

    monkeypatch.setattr(TaskQueue, "transition", transition_dead_for_needs_human)
    raised_d = None
    try:
        result_d = env_d.run(fail_seats())
    except Exception as exc:  # noqa: BLE001
        raised_d = exc
    monkeypatch.undo()
    if raised_d is not None or outcomes(env_d) != ["fail", "escalated"]:
        pytest.fail(
            f"a finish() transition failure double-spent the budget: "
            f"attempts {outcomes(env_d)} (raised {raised_d!r})"
        )
    assert result_d.outcome in ("needs_human", "error")
    assert env_d.task()["status"] in ("needs_human", "ready")
    # The next run proceeds from wherever the queue was left.
    assert env_d.run(Seats()).outcome in ("idle", "needs_human")

    # --- leg E: append_attempt itself dies (nothing from finish(); the
    # error path records exactly one attempt for the round) ---
    env_e = Env(tmp_path / "e", tasks=[budget_task()])
    fired_e: list[int] = []

    def append_dead_once(self, task_id, attempt):
        if not fired_e:
            fired_e.append(1)
            raise OSError("queue write failed")
        return real_append(self, task_id, attempt)

    monkeypatch.setattr(TaskQueue, "append_attempt", append_dead_once)
    raised_e = None
    try:
        result_e = env_e.run(fail_seats())
    except Exception as exc:  # noqa: BLE001
        raised_e = exc
    monkeypatch.undo()
    assert raised_e is None, repr(raised_e)
    # Round 1's fail attempt failed to record; the error path recorded one
    # escalated attempt (escalation orchestrator_error) for that failure
    # and the run stopped (outcome error). One attempt per failure, never
    # two — and never zero.
    assert outcomes(env_e) == ["escalated"]
    assert env_e.task()["attempts"][0]["escalation"] == "orchestrator_error"
    assert result_e.outcome == "error"
    assert env_e.task()["status"] == "ready"


# ------------------------------------------------------------------ AC-5 ---


def test_ac_5():
    """m-14: ``gh pr view`` exiting 0 with non-object JSON maps to the
    documented unresolvable PR state (PublishError → the sweep skips, the
    concurrency limit fails closed), never an AttributeError. Also the
    same guard for ``gh api``'s protection parser (amended AC-5: the
    hostile pass found the identical crash shape in classify_protection).
    """
    def fake_run(stdout, rc=0):
        return lambda cmd, **kw: SimpleNamespace(returncode=rc, stdout=stdout,
                                                 stderr="")

    url = "https://github.com/fixture/fixture/pull/1"
    # Every non-object shape and the empty/malformed ones: no crash.
    for stdout in ("null\n", "[]\n", '"OPEN"\n', "42\n", "{}\n", "", "   \n"):
        with pytest.raises(PublishError) as excinfo:
            gh_pr_state(url, _run=fake_run(stdout))
        assert "state" in str(excinfo.value), stdout
        assert "unresolvable" in str(excinfo.value), stdout
    # The documented happy paths keep working.
    assert gh_pr_state(url, _run=fake_run('{"state": "OPEN"}\n')) == "open"
    assert gh_pr_state(url, _run=fake_run('{"state": "MERGED"}\n')) == "merged"
    # rc != 0 stays the existing PublishError.
    with pytest.raises(PublishError):
        gh_pr_state(url, _run=fake_run("whatever\n", rc=1))

    # classify_protection: the same non-object shapes classify as a
    # refusal with a detail, never an AttributeError.
    for stdout in ("null\n", "[]\n", '"protected"\n', "7\n", ""):
        protection = classify_protection(0, stdout, "")
        assert isinstance(protection, Protection)
        assert not protection.checked and not protection.strict \
            and not protection.plan_limit
        assert protection.detail
    # The happy shapes are unchanged.
    strict = classify_protection(
        0, '{"required_status_checks": {"strict": true, "contexts": []}}', "")
    assert strict.checked and strict.strict


# ------------------------------------------------------------------ AC-6 ---


def test_ac_6(tmp_path: Path, monkeypatch):
    """I-3: atomic queue writes fsync the parent DIRECTORY after the rename
    (a power loss between rename and dir-fsync can lose the rename), and
    every atomic rename in factory/ goes through the one helper.
    """
    from factory import state

    target = tmp_path / "tasks.json"
    target.write_text('{"old": true}\n')
    events: list = []
    real_replace, real_open, real_fsync = os.replace, os.open, os.fsync

    def replace_spy(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    def open_spy(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        # Only direct os.open calls are visible here (builtin open bypasses
        # the module attribute), so this is the helper's directory fd.
        if Path(str(path)) == tmp_path:
            events.append(("open-dir", fd))
        return fd

    def fsync_spy(fd):
        events.append(("fsync", fd))
        return real_fsync(fd)

    monkeypatch.setattr(state.os, "replace", replace_spy)
    monkeypatch.setattr(state.os, "open", open_spy)
    monkeypatch.setattr(state.os, "fsync", fsync_spy)

    state._atomic_write_json(target, {"tasks": []})

    assert "replace" in events, "the atomic write never renamed"
    replace_at = events.index("replace")
    # The dir fd can REUSE the just-closed temp file's number (fds
    # recycle), so the directory fsync is identified positionally: the fd
    # opened for the parent AFTER the rename, then fsynced.
    open_dir_at = [i for i, event in enumerate(events)
                   if isinstance(event, tuple) and event[0] == "open-dir"]
    assert open_dir_at, "the parent directory was never opened for fsync"
    open_at = open_dir_at[0]
    assert open_at > replace_at, (
        f"the directory must be opened for fsync AFTER the rename: {events}"
    )
    dir_fd = events[open_at][1]
    dir_fsyncs = [i for i, event in enumerate(events)
                  if isinstance(event, tuple) and event[0] == "fsync"
                  and event[1] == dir_fd and i > open_at]
    assert dir_fsyncs, f"the directory fd was never fsynced: {events}"
    assert all(i > replace_at for i in dir_fsyncs), (
        f"the directory fsync must happen AFTER the rename: {events}"
    )
    # The temp file itself was fsynced before the rename (existing behavior).
    file_fsyncs = [i for i, event in enumerate(events)
                   if isinstance(event, tuple) and event[0] == "fsync"
                   and i < replace_at]
    assert file_fsyncs
    assert json.loads(target.read_text()) == {"tasks": []}

    # Every atomic rename in factory/ is this helper's — no module does its
    # own os.replace (I-3's grep, as a mechanical pin).
    for module in (REPO_ROOT / "factory").glob("*.py"):
        if module.name == "state.py":
            continue
        assert "os.replace(" not in module.read_text(), (
            f"{module.name} renames on its own: atomic writes must go "
            f"through state._atomic_write_json"
        )

    # And a real queue save goes through the same path end-to-end.
    events.clear()
    qpath = tmp_path / "q" / "tasks.json"
    qpath.parent.mkdir()
    qpath.write_text(json.dumps({"version": 1, "project": "fixture",
                                 "tasks": [_task()]}))
    TaskQueue(qpath).save()
    assert "replace" in events and any(
        isinstance(event, tuple) and event[0] == "fsync" for event in events)


# ------------------------------------------------------------------ AC-7 ---


def test_ac_7(tmp_path: Path, monkeypatch):
    """I-4: the run-lease error names the holder only when that holder is
    alive (a live pid, and kill(pid,0) → EPERM — alive, another user) and
    says the lock content is stale otherwise (a dead pid, garbage content —
    including the repo's real stale ``reclaim`` — and an empty file).
    """
    from factory import state

    env = Env(tmp_path)
    queue = TaskQueue(env.queue_path)
    lock_path = env.queue_path.with_name(env.queue_path.name + ".run.lock")

    def contend(content: str) -> str:
        lock_path.write_text(content)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with pytest.raises(RunInProgressError) as excinfo:
                    with queue.begin_run("T-1"):
                        pass
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return str(excinfo.value)

    # A live pid (this process): the holder is named.
    live = contend(f"F-13 pid={os.getpid()}")
    assert "held by" in live and str(os.getpid()) in live, live
    assert "stale" not in live

    # A dead pid: never named as the holder — the line is stale.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = dead.pid
    dead.wait()
    stale = contend(f"F-13 pid={dead_pid}")
    assert "stale" in stale, stale
    assert "held by" not in stale, f"a dead pid was named as the holder: {stale}"

    # Garbage content — including the repo's REAL stale lock content.
    for content in ("reclaim", "garbage with no pid", "pid=notanumber"):
        msg = contend(content)
        assert "stale" in msg and "held by" not in msg, (content, msg)

    # An empty file: stale, never "held by unknown".
    empty = contend("")
    assert "stale" in empty and "held by" not in empty, empty

    # kill(pid, 0) → EPERM: the process is alive, owned by another user —
    # it IS named (alive is alive, whoever owns it).
    real_kill = state.os.kill

    def kill_permission(pid, sig):
        if pid == os.getpid() and sig == 0:
            raise PermissionError(1, "Operation not permitted")
        return real_kill(pid, sig)

    monkeypatch.setattr(state.os, "kill", kill_permission)
    eperm = contend(f"F-13 pid={os.getpid()}")
    assert "held by" in eperm and str(os.getpid()) in eperm, eperm
    monkeypatch.undo()

    # The holder line the lease itself writes now carries the pid, so the
    # next contender can make exactly this liveness call.
    with queue.idle_lease():
        written = lock_path.read_text()
    assert f"pid={os.getpid()}" in written, written
