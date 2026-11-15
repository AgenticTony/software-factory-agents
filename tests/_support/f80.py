"""F-80 test support: the contract-review harness.

Composed from ``_support.f72`` (F-51 AC-7: no test module imports
another): the fixture repo, its bare remote, the queue, the clone
manager and the owner ledger are real; only the model seat is scripted.
What this adds:

* ``Env80`` — an ``Env72`` whose one task is a DRAFT (the status the
  review exists for) with a committed source document, pushed to the
  bare remote the review's clone is built from;
* ``ReviewSeat`` — the scripted ``contract_reviewer`` seat: records
  every call (seat, prompt, run dir, kwargs) and what the clone held,
  returns a scripted result text, and can run a hook inside the clone
  (the hostile arms);
* ``findings_payload`` / ``finding`` — the output protocol, test-side.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from _support.f63 import ledger_approve
from _support.f72 import OFF_PEAK, Env72, _git, _task
from factory.config import FactoryConfig, PeakWindow
from factory.session import SessionRecord

__all__ = [
    "IN_PEAK", "OFF_PEAK", "PEAK_CONFIG", "SOURCE_DOC", "SOURCE_PATH",
    "Echo", "Env80", "ReviewSeat", "draft_task", "finding",
    "findings_payload",
]

SOURCE_PATH = "docs/T-1.md"
SOURCE_DOC = (
    "# T-1 — answer returns 42\n"
    "\n"
    "Purpose: callers of `answer()` get 42 on every path.\n"
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A weekday inside the window below; OFF_PEAK (a Sunday) is outside it.
PEAK_CONFIG = FactoryConfig(peak_window=PeakWindow(
    "Europe/Stockholm", ("mon", "tue", "wed", "thu", "fri"),
    "08:00", "18:00"))
IN_PEAK = OFF_PEAK.replace(day=19, hour=10)  # Monday 2026-10-19 10:00


def draft_task(task_id: str = "T-1", **overrides) -> dict:
    """One approvable DRAFT build task (no approval stamps; a budget at
    the profile's floor so ``factory approve`` accepts it)."""
    task = _task(task_id, status="draft")
    for key in ("acceptance_hash", "approved_at", "approved_by"):
        task.pop(key, None)
    task["budget"] = {"size": "S", "max_attempts": 3, "max_turns": 60,
                      "wall_clock_min": 30}
    task["source"] = SOURCE_PATH
    task.update(overrides)
    return task


def finding(id_: str = "CR-1", lens: str = "purpose",
            severity: str = "major",
            text: str = "the check cannot detect the failure it exists for",
            criterion: str | None = "AC-1",
            suggested_change: str | None = None, **extra) -> dict:
    item = {"id": id_, "lens": lens, "severity": severity, "text": text,
            "criterion": criterion, "suggested_change": suggested_change}
    item.update(extra)
    return item


def findings_payload(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


class Echo:
    """Collects approve's echo lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class Env80(Env72):
    """A fixture project whose queue holds one draft task with a
    committed source document. ``files`` adds or overrides committed
    files (``None`` content skips the default source doc); ``symlinks``
    maps a repo-relative link path to its target."""

    def __init__(self, tmp_path: Path, *, task: dict | None = None,
                 files: dict | None = None, symlinks: dict | None = None,
                 profile_extra: dict | None = None):
        super().__init__(tmp_path, tasks=[task or draft_task()],
                         profile_extra=profile_extra)
        tree = {SOURCE_PATH: SOURCE_DOC}
        tree.update(files or {})
        for rel, content in tree.items():
            if content is None:
                continue
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        for rel, link_target in (symlinks or {}).items():
            link = self.repo / rel
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(link_target)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "source docs")
        subprocess.run(
            ["git", "-C", str(self.repo), "push", "-q", str(self.remote),
             "main"], check=True, capture_output=True)
        ledger_approve(self.root, self.queue_path)  # the profile (F-63)

    def queue_bytes(self) -> bytes:
        return self.queue_path.read_bytes()

    def review_dir(self, task_id: str = "T-1") -> Path:
        return self.root / "contract-reviews" / "fixture" / task_id

    def stored(self, task_id: str = "T-1") -> list[Path]:
        directory = self.review_dir(task_id)
        return sorted(directory.glob("*.json")) if directory.is_dir() else []

    def edit_task(self, mutate) -> None:
        """Hand-edit the queue the way an owner's contract PR does (the
        test fixture's own write — never the factory's)."""
        doc = json.loads(self.queue_path.read_text())
        mutate(doc["tasks"][0])
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")


class ReviewSeat:
    """The scripted ``contract_reviewer`` seat.

    ``result``: the session's result text (default: one valid finding).
    ``status``/``reason``: a failed or aborted session. ``hook``: called
    with the run dir before the record returns (the hostile arms).
    """

    def __init__(self, result: str | None = None, *,
                 status: str = "completed", reason: str | None = None,
                 hook=None):
        self.result = result if result is not None else findings_payload(
            finding())
        self.status = status
        self.reason = reason
        self.hook = hook
        self.calls: list[tuple[str, str, Path, dict]] = []
        self.clone_files: list[list[str]] = []

    @property
    def prompt(self) -> str:
        assert self.calls, "the seat was never started"
        return self.calls[-1][1]

    @property
    def kwargs(self) -> dict:
        return self.calls[-1][3]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        self.calls.append((seat, prompt, run_dir, kwargs))
        self.clone_files.append(sorted(
            _git(run_dir, "ls-files").splitlines()))
        if self.hook is not None:
            self.hook(run_dir)
        return SessionRecord(
            seat=seat, status=self.status, reason=self.reason,
            num_turns=4, model="stub-model",
            tokens={"input_tokens": 900, "output_tokens": 120},
            usage={"input": 900, "cache_read": 0, "cache_write": 0,
                   "output": 120},
            result=self.result if self.status == "completed" else None,
        )
