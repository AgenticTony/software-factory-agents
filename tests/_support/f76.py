"""F-76 test support: scripted re-validation gates, remote moves, stale parks.

The F-38/F-72/F-75 split holds: git, the clone manager, the queue, the
L0/L1 sandboxes and the push to the local bare remote are real; only the
model seats, the CI/gh seams and — for the scripted re-validation arms —
the ``run_l0``/``run_l1`` module seams are scripted. What this harness
adds over ``_support.f75``:

* ``GateScript`` — the scripted ``orch.run_l0``/``orch.run_l1`` seams:
  one verdict script per call (the last repeats), every call's repo
  recorded (the fresh-clone proof), and a LEAKAGE PROBE — the first L0
  call plants a file in ITS clone; every later call records whether it
  can see it (a re-run that reused the first clone would);
* ``Env76`` — Env75 plus ``advance_remote_main`` (push a commit onto the
  bare remote's ``main``) and ``park_stale`` (drive a task to a real
  ``stale_base`` park with the PR still open — the state ``factory
  recover --revalidate`` takes over from).

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _support.f72 import Seats
from _support.f75 import Env75
from factory.gates import CommandResult, Finding
from factory.l0 import L0Report
from factory.l1 import L1Report

__all__ = ["Env76", "GateScript", "l0_pass", "l0_fail", "l1_fail"]


def l0_pass(escalations: "list[str] | None" = None) -> dict:
    """A passing L0 script (optionally with immediate-stop escalations)."""
    return {"verdict": "pass", "escalations": list(escalations or [])}


def l0_fail(*findings: str, commands: "list[dict] | None" = None) -> dict:
    """A failing L0 script — one finding per text (verdict fail iff any
    finding exists, exactly the real L0 rule)."""
    return {
        "verdict": "fail",
        "findings": [{"id": "command_failed", "text": text}
                     for text in findings],
        "commands": commands or [
            {"name": "test", "command": "pytest -q", "exit_code": 1,
             "output": findings[0] if findings else "", "timed_out": False},
        ],
    }


def l1_fail(*findings: str) -> dict:
    """A failing L1 script — the id is a REAL L1 command-derived finding
    (test_proves_nothing: the base run stayed green), so the F-76 re-run
    trigger (command ids only) fires for it."""
    return {
        "verdict": "fail",
        "findings": [{"id": "test_proves_nothing", "text": text}
                     for text in findings],
    }


class GateScript:
    """The scripted ``orch.run_l0``/``orch.run_l1`` seams.

    ``l0``/``l1``: one script dict per call, the last repeating. A script
    is ``{"verdict": ..., "escalations": [...], "findings": [...],
    "commands": [...]}`` rendered into REAL gate reports (the sweep reads
    ``.verdict``/``.escalations``/``.findings`` and stores ``.to_dict()``
    — real report objects keep that honest).

    Every call records ``(layer, repo)``. The leakage probe: the FIRST L0
    call writes ``<leak_probe>`` into its own clone; every later call
    records whether the file is visible in ITS clone — ``leak_seen`` must
    stay False (a re-run reusing the first clone would see it).
    """

    def __init__(self, l0: "list[dict] | None" = None,
                 l1: "list[dict] | None" = None,
                 leak_probe: str = "leak-probe.txt"):
        self.l0 = list(l0 or [])
        self.l1 = list(l1 or [])
        self.calls: list[tuple[str, Path]] = []
        self.repos = {"L0": [], "L1": []}
        self.leak_seen: bool | None = None
        self.planted: Path | None = None
        self._probe = leak_probe

    def _pop(self, scripts: list) -> dict:
        if not scripts:
            return {"verdict": "pass"}
        return scripts.pop(0) if len(scripts) > 1 else scripts[0]

    def _report(self, layer: str, script: dict):
        findings = [Finding(**f) for f in script.get("findings") or []]
        commands = [
            CommandResult(name=c.get("name", "test"),
                          command=c.get("command", "pytest -q"),
                          exit_code=c.get("exit_code"),
                          output=c.get("output", ""),
                          timed_out=bool(c.get("timed_out")))
            for c in script.get("commands") or []
        ]
        if layer == "L0":
            return L0Report(verdict=script["verdict"],
                            findings=findings,
                            command_results=commands,
                            escalations=list(script.get("escalations") or []))
        return L1Report(verdict=script["verdict"], findings=findings,
                        command_results=commands)

    def run_l0(self, profile, repo, base, head, **kwargs):
        repo = Path(repo)
        self.calls.append(("L0", repo))
        self.repos["L0"].append(repo)
        probe = repo / self._probe
        if not self.repos["L0"][:-1]:
            probe.write_text("state only the first clone ever sees\n",
                             encoding="utf-8")
            self.planted = probe
        elif self.leak_seen is None:
            self.leak_seen = probe.exists()
        return self._report("L0", self._pop(self.l0))

    def run_l1(self, profile, repo, base, head, **kwargs):
        repo = Path(repo)
        self.calls.append(("L1", repo))
        self.repos["L1"].append(repo)
        return self._report("L1", self._pop(self.l1))

    def patch(self, monkeypatch):
        """Bind the seams onto factory.orchestrator's module globals."""
        import factory.orchestrator as orch

        monkeypatch.setattr(orch, "run_l0", self.run_l0)
        monkeypatch.setattr(orch, "run_l1", self.run_l1)
        return self


class Env76(Env75):
    """Env75 plus the F-76 shapes: remote-main moves and a stale park."""

    def advance_remote_main(self, impl_body: "str | None",
                            *, message: str = "main moves on",
                            extra_file: "str | None" = None) -> str:
        """Push one commit onto the bare remote's main; its new head sha."""
        scratch = self.tmp / f"main-{abs(hash(message)) & 0xffff}"
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(scratch)],
            check=True, capture_output=True)
        if impl_body is not None:
            (scratch / "impl.py").write_text(impl_body)
        if extra_file is not None:
            (scratch / extra_file).write_text("remote-only\n")
        for args in (("add", "-A"), ("commit", "-qm", message)):
            subprocess.run(
                ["git", "-C", str(scratch),
                 "-c", "user.email=fixture@factory.local",
                 "-c", "user.name=Fixture", *args],
                check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(scratch), "push", "-q", "origin",
             "HEAD:refs/heads/main"], check=True, capture_output=True)
        return self.remote_branch_sha("main")

    def push_to_branch(self, branch: str, impl_body: str, *,
                       message: str = "foreign push") -> str:
        """Push one commit onto an existing remote branch (a foreign hand
        moving the PR head under the factory)."""
        scratch = self.tmp / f"push-{abs(hash(message)) & 0xffff}"
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(scratch)],
            check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(scratch), "checkout", "-q",
             f"origin/{branch}"], check=True, capture_output=True)
        (scratch / "impl.py").write_text(impl_body)
        for args in (("add", "-A"), ("commit", "-qm", message)):
            subprocess.run(
                ["git", "-C", str(scratch),
                 "-c", "user.email=fixture@factory.local",
                 "-c", "user.name=Fixture", *args],
                check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(scratch), "push", "-q", "origin",
             f"HEAD:refs/heads/{branch}"], check=True, capture_output=True)
        return self.remote_branch_sha(branch)

    def run_once(self, session_runner=None, **kwargs):
        """run_once with the fixture defaults (off-peak clock, strict
        protection, an open-PR seam) and the ledger approval refreshed —
        Env72.run's shape, minus the ci_state default."""
        from _support.f63 import ledger_approve
        from factory.mergeorder import Protection
        from factory.orchestrator import run_once

        ledger_approve(self.root, self.queue_path)
        kwargs.setdefault("now", lambda: OFF_PEAK_NOW())
        kwargs.setdefault(
            "protection", lambda r, b: Protection(checked=True, strict=True))
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_once(self.queue_path, factory_root=self.root,
                        session_runner=session_runner or Seats(),
                        open_pr=self.open_pr, **kwargs)

    def park_stale(self, *, task_id: str = "T-1", conflict: bool = True,
                   extra: dict | None = None) -> str:
        """Publish the task's PR, move main under it, and run the sweep
        once so it parks ``stale_base`` with the PR still open (the
        pr_state seam answers "open"). Returns the PR url.

        ``conflict``: park via a real merge conflict (impl.py changes on
        both sides — no gates run). Otherwise park via a conflict-free
        remote move; the CALLER patches the gate seams first (they must
        fail TWICE — the F-76 re-run means one failure no longer parks).
        The publish run always uses the REAL gates (saved/restored
        around it), whatever the caller patched.
        """
        import factory.l0
        import factory.l1
        import factory.orchestrator as orch

        saved = (orch.run_l0, orch.run_l1)
        orch.run_l0, orch.run_l1 = factory.l0.run_l0, factory.l1.run_l1
        try:
            assert self.run_once(**(extra or {})).outcome == "pr_opened"
        finally:
            orch.run_l0, orch.run_l1 = saved
        url = self.task(task_id)["links"]["pr"]
        if conflict:
            self.advance_remote_main(
                "def answer():\n    return 99\n",
                message="conflicting remote change")
        else:
            self.advance_remote_main(None, extra_file="remote_only.txt")
        result = self.run_once(**(extra or {}))
        assert result.outcome == "needs_human", result
        task = self.task(task_id)
        assert task["status"] == "needs_human"
        assert task["attempts"][-1]["escalation"] == "stale_base"
        return url


def OFF_PEAK_NOW():
    """The shared off-peak clock answer (Sunday afternoon, Stockholm)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, 18, 15, 0,
                    tzinfo=ZoneInfo("Europe/Stockholm"))


def IN_PEAK_NOW():
    """Monday 09:00 Stockholm — inside the default peak window."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, 19, 9, 0,
                    tzinfo=ZoneInfo("Europe/Stockholm"))
