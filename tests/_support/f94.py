"""F-94 test support: lens-aware reviewer seats and a one-round run.

Composed from the same ``_support`` primitives as F-82 (F-51 AC-7: no
test module imports another — support modules compose instead): git,
the clone manager, the queue, the L0/L1 sandboxes and the push to the
local bare remote are real; the model seats and the CI/gh seams are
scripted. What this adds:

* ``HUNT_PIN`` — the defect-hunt lens block, pinned by literal (the
  test is the sanctioned way the text changes);
* ``LensSeats`` — scripted seats whose reviewer verdict depends on the
  LENS the seat was given (its system prompt), not on call order: the
  conformance seat and the defect-hunt seat can say different things,
  and a seat's first verdict can be scripted malformed (the retry arm);
* ``run_round`` — one task, one attempt, CI green: the run's result.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from _support.f72 import IMPL_GOOD, TEST_SOURCE, _finding, _task, _verdict
from _support.f75 import CI
from _support.f78 import GREEN, ONE_ATTEMPT, seams
from factory.session import SessionRecord

__all__ = [
    "HUNT_MARK", "HUNT_PIN", "LensSeats", "run_round", "_finding",
    "_task", "_verdict",
]

# The defect-hunt lens block, exactly as the seat receives it (appended
# to the base reviewer prompt). Pinned by literal: editing the block in
# factory/prompts.py without this pin fails test_ac_2.
HUNT_PIN = (
    "## Your lens: hunt the defect beyond the hunk\n"
    "\n"
    "On top of the review above, hunt for defects the diff causes or "
    "leaves behind:\n"
    "\n"
    "- Read every hunk and its enclosing function, and ask what input or "
    "state makes each line wrong.\n"
    "- For every removed or replaced line, name the behaviour it "
    "enforced and where the new code re-establishes it.\n"
    "- Grep the callers of every changed function, and grep the codebase "
    "for other sites of each defect class the diff fixes or touches. "
    "Report each other site as its own finding with `\"sibling\": true`, "
    "its `file` and `line`, and the defect class named in its `text`.\n"
    "\n"
    "Draw the line exactly. A defect this diff CAUSES — including a break "
    "at an unchanged caller, test or config — is reported WITHOUT "
    "`sibling` and judged by its severity like any finding. "
    "`\"sibling\": true` is only for an older site of the same class that "
    "this diff neither causes nor touches: the orchestrator records it for "
    "a follow-up task and it does not fail this round, whatever its "
    "severity — rate it by what the defect costs, and judge your verdict "
    "on the diff alone. `sibling: true` on a file the diff changes lowers "
    "nothing: a defect inside the diff is judged by the diff's rules.\n"
)
# What the scripted runner looks for to tell the two lenses apart.
HUNT_MARK = "## Your lens: hunt the defect beyond the hunk"


class LensSeats:
    """Scripted seats keyed by lens.

    ``conformance`` / ``hunt``: the verdict JSON a reviewer seat returns
    when its system prompt lacks / carries the defect-hunt block.
    ``hunt_first``: when set, the FIRST hunt-lens reviewer call returns
    this text instead (a malformed verdict: the retry arm).
    ``verifier``: the verifier's output (default: not a vote — nothing
    is dropped, every finding stands).
    """

    def __init__(self, conformance: str | None = None,
                 hunt: str | None = None, hunt_first: str | None = None,
                 verifier: str = "no vote"):
        self.conformance = conformance or _verdict(declared="pass")
        self.hunt = hunt or _verdict(declared="pass")
        self.hunt_first = hunt_first
        self.verifier = verifier
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, dict]] = []

    def reviewer_systems(self) -> list[str]:
        return [kw.get("system_prompt") for seat, _, kw in self.calls
                if seat == "reviewer"]

    def implementer_prompts(self) -> list[str]:
        return [p for seat, p, _ in self.calls if seat == "implementer"]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        with self.lock:
            self.calls.append((seat, prompt, kwargs))
            hunt_calls = sum(
                1 for s, _, kw in self.calls
                if s == "reviewer"
                and HUNT_MARK in (kw.get("system_prompt") or ""))
        if seat == "implementer":
            for rel, content in (("impl.py", IMPL_GOOD),
                                 ("tests/test_feature.py", TEST_SOURCE)):
                target = run_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            result = "Implemented."
        elif seat == "verifier":
            result = self.verifier
        else:
            system = kwargs.get("system_prompt") or ""
            if HUNT_MARK in system:
                result = (self.hunt_first
                          if self.hunt_first is not None and hunt_calls == 1
                          else self.hunt)
            else:
                result = self.conformance
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            usage={"input": 60, "cache_read": 0, "cache_write": 0,
                   "output": 10},
            result=result,
        )


def run_round(tmp: Path, monkeypatch, seats: LensSeats, *, reviewers: int = 2,
              budget: dict | None = None, profile_extra: dict | None = None):
    """One task through run_once with CI green; ``(env, result)``."""
    from _support.f76 import Env76
    from factory.config import FactoryConfig

    env = Env76(tmp, tasks=[_task(budget=budget or ONE_ATTEMPT)],
                profile_extra=profile_extra)
    seams(monkeypatch, env)
    result = env.run_once(seats, ci_state=CI(default=GREEN),
                          config=FactoryConfig(reviewers=reviewers))
    return env, result


def followup(env) -> "dict | None":
    """The queue's T-1-followup record, or None."""
    from factory.state import TaskQueue

    return next((t for t in TaskQueue(env.queue_path).doc.get("tasks", [])
                 if t.get("id") == "T-1-followup"), None)


def dumps(payload) -> str:
    return json.dumps(payload)
