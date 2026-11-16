"""F-86 test support: a mechanical lint gate and a scripted repair seat.

The F-72/F-83 split holds: git, the clone manager, the queue and the
L0/L1 sandboxes are real; only the model seats are scripted. What this
harness adds over ``_support.f72``:

* a profile whose ``lint`` command is REAL ruff pinned to F401 — the
  mechanical failure F-86 exists for (an unused import in the task's
  own change) is produced by the real tool, not a script;
* :class:`RepairSeats` — the f72 :class:`~_support.f72.Seats` runner
  whose implementer seat recognises the repair round by its prompt
  heading and applies a repair file script (path -> content, None =
  delete), so a test chooses what the repair "fixes" — honestly, or by
  deletion / suppression / config edit / revert.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from _support.f72 import IMPL_GOOD, Env72, Seats
from factory.session import SessionRecord

__all__ = ["REPAIR_MARKER", "IMPL_LINTY", "RepairSeats", "env86"]

# The repair prompt's untrusted-data heading — the seam the scripted
# seat dispatches on (an implementer prompt never carries it).
REPAIR_MARKER = "Gate failure to repair"

# The round-1 change: correct behaviour, one unused import — the exact
# wo22 shape (F401 in the task's own change), so only lint fails.
IMPL_LINTY = "import os\n\n\ndef answer():\n    return 42\n"

# The honest repair: the same change without the unused import.
REPAIR_GOOD = IMPL_GOOD


def lint_command() -> str:
    """The profile's ``lint``: real ruff, F401 only, config discovered
    (so a repair that edits lint config changes what it reports)."""
    return f"{shlex.quote(sys.executable)} -m ruff check --select F401 ."


def env86(tmp_path: Path, tasks: list | None = None, risk: dict | None = None,
          typecheck: str | None = None) -> Env72:
    """One fixture project whose gates include a real mechanical lint.

    ``risk`` merges into the profile's ``paths.risk`` (the lint-
    suppression sentinel fires on risk-path files only — F-25's rule).
    """
    commands = {"lint": lint_command()}
    if typecheck is not None:
        commands["typecheck"] = typecheck
    profile_extra: dict = {"commands": commands}
    if risk is not None:
        profile_extra["paths"] = {"risk": risk}
    return Env72(tmp_path, tasks=tasks, profile_extra=profile_extra)


class RepairSeats(Seats):
    """Seats whose implementer seat plays the F-86 repair round.

    The repair session is dispatched on the repair prompt's heading
    (the parent runs it with seat ``implementer``); it applies ONE
    entry of ``repair_files`` per repair (path -> content, None =
    delete — the last entry repeats) and returns
    ``repair_turns``-many turns. ``repair_error`` raises instead (the
    runner crashing mid-repair); ``repair_status`` builds the record
    with that status (the cap/window arms).
    """

    def __init__(self, repair_files: list | None = None, repair_turns: int = 4,
                 repair_status: str = "completed", repair_reason: str = "",
                 repair_error: Exception | None = None, **kwargs):
        super().__init__(**kwargs)
        self.repair_files = list(repair_files or [{}])
        self.repair_turns = repair_turns
        self.repair_status = repair_status
        self.repair_reason = repair_reason
        self.repair_error = repair_error
        self.repairs = 0
        self.repair_prompts: list[str] = []
        self.repair_kwargs: list[dict] = []

    def _apply(self, run_dir: Path) -> None:
        script = self.repair_files[min(self.repairs - 1,
                                       len(self.repair_files) - 1)]
        for rel, content in script.items():
            target = run_dir / rel
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer" and REPAIR_MARKER in prompt:
            self.repairs += 1
            self.repair_prompts.append(prompt)
            self.repair_kwargs.append(dict(kwargs))
            if self.repair_error is not None:
                raise self.repair_error
            self._apply(Path(run_dir))
            return SessionRecord(
                seat=seat, status=self.repair_status,
                num_turns=self.repair_turns, model="stub-model",
                reason=self.repair_reason or None,
                tokens={"input_tokens": 90, "output_tokens": 20},
                usage={"input": 90, "cache_read": 0, "cache_write": 0,
                       "output": 20},
                result="repaired the lint failure",
            )
        return super().__call__(seat, prompt, Path(run_dir), profile,
                                **kwargs)
