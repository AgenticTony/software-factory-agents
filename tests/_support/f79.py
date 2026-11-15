"""F-79 test support: the kept-work fix-round harness.

Composed from the same ``_support`` primitives as F-77/F-78 (F-51 AC-7:
no test module imports another): git, the clone manager, the queue, the
L0/L1 sandboxes and the push to the local bare remote are real; the
model seats and the CI/gh seams are scripted. What this adds:

* ``ObservingSeats`` — ``_support.f72.Seats`` that also records, at the
  moment each implementer session STARTS, what its clone holds
  (``impl.py``'s text and ``HEAD``) — the proof a resumed round was
  seeded from the kept commit, read before the seat writes anything;
* ``recover_open`` / ``fix_round`` — the owner's keep-the-PR reset and
  one sweep-started CI fix round, as F-77/F-78 drive them;
* ``kept_park`` — the wo21 shape: a CI fix round on an open PR whose
  change passes L0 and L1, then fails review with the one-attempt
  budget spent — the task parks holding its PR, nothing pushed.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _support.f72 import Seats, _finding, _verdict
from _support.f75 import CI
from _support.f78 import GREEN, PENDING, RED, Parked, seams
from factory.approve import recover_task
from factory.config import FactoryConfig

__all__ = [
    "EVIDENCE", "FAIL", "FINDING_TEXT", "IMPL_DONE", "IMPL_WIP", "PASS",
    "ObservingSeats",
    "fix_round", "git_out", "kept_park", "recover_open", "wip_dir",
]

EVIDENCE = "checked the park; the PR's work is worth keeping"
# The fix round's validated change (passes L0 and L1) and the resumed
# round's follow-up on top of it.
IMPL_WIP = "def answer():\n    return 42  # kept: the CI fix\n"
IMPL_DONE = "def answer():\n    return 42  # kept: the CI fix + review fix\n"
FINDING_TEXT = "the guard is unwired on the download path"
PASS = _verdict()
FAIL = _verdict(_finding(text=FINDING_TEXT, severity="major", axis="spec"))


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True).stdout.strip()


class ObservingSeats(Seats):
    """Seats that record what each implementer session's clone held
    when the session started: ``seen[i] = {"impl": text, "head": sha}``
    for the i-th implementer call (0-based)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen: list[dict] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            run_dir = Path(run_dir)
            self.seen.append({
                "impl": (run_dir / "impl.py").read_text(),
                "head": git_out(run_dir, "rev-parse", "HEAD"),
            })
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


def wip_dir(p: Parked) -> Path:
    return p.env.root / "wip" / "fixture" / "T-1"


def recover_open(p: Parked, **extra) -> str:
    """The owner's reset on a park whose PR GitHub reports open."""
    return recover_task(
        p.queue(), "T-1", reset_attempts=True, evidence=EVIDENCE,
        done_by="owner", factory_root=p.env.root,
        pr_state=lambda url: "open", pr_ready=lambda url: None,
        confirm=lambda question: True, echo=lambda text: None, **extra)


def fix_round(p: Parked, monkeypatch, *, head: str | None = None,
              rerun: bool = False, **kwargs):
    """One run whose sweep starts the CI fix round on ``head`` (default:
    the park's pushed head, already on ``ci_rerun_heads``; ``rerun``
    scripts the flake re-run a never-seen head gets first). CI is green
    for every other SHA, so a published fix settles green."""
    seams(monkeypatch, p.env)
    script = [RED, RED, PENDING, RED] if rerun else [RED, RED]
    return p.env.run_once(
        p.seats, ci_state=CI(by_sha={head or p.head: script}, default=GREEN),
        config=FactoryConfig(ci_wait_min=5), **kwargs)


def kept_park(tmp: Path, monkeypatch) -> Parked:
    """The wo21 shape. A ``ci_failed`` park is reset keeping its PR; the
    sweep's fix round writes ``IMPL_WIP`` (L0 and L1 pass), review FAILs
    it with a major finding, and the one-attempt budget is spent: the
    task parks ``attempts_exhausted`` holding its open (drafted) PR,
    nothing pushed. The next implementer round scripts ``IMPL_DONE``
    and its review passes."""
    seats = ObservingSeats(
        impl_files=[{}, {"impl.py": IMPL_WIP}, {"impl.py": IMPL_DONE}],
        reviews=(PASS, FAIL, PASS))
    p = Parked(tmp, monkeypatch, seats=seats)
    assert recover_open(p) == "in_review"
    result = fix_round(p, monkeypatch)
    assert result.outcome == "needs_human", result
    assert result.escalation == "attempts_exhausted", result
    assert result.pr == p.url
    assert p.env.task()["status"] == "needs_human"
    return p
