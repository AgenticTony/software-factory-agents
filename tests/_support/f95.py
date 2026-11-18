"""F-95 test support: scripted seats for the sibling and caller-break seeds.

Composed from ``_support.f16`` (F-51 AC-7: no test module imports
another — support modules compose instead). The fixture repo, the seed
branches and the L0/L1 gates are real; every L2 seat is scripted, so no
model session runs anywhere.

* ``Seats`` — ``ScriptedSeats`` with per-contract verifier drops: the
  verifier votes out the listed finding ids (with a reason) and keeps
  the rest, the shape of a real verifier's vote.
* ``SplitSeats`` — reviewer seat 1 and seat 2 say different things for
  one contract (the defect-hunt seat reports the sibling, the other
  seat reports an in-diff blocker): the "round failed with the sibling
  present" case.
* ``normalised`` — a record with the run-to-run noise removed (pytest's
  timing line), for the isolation pins.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading

from _support.f16 import _FINDINGS_ARRAY, ScriptedSeats
from factory.session import SessionRecord

__all__ = ["Seats", "SplitSeats", "normalised", "finding"]


def finding(fid: str, file: str, text: str, *, severity: str = "major",
            sibling: bool | None = None) -> dict:
    """One scripted reviewer finding (``sibling`` only when given)."""
    out = {"id": fid, "axis": "spec", "severity": severity, "file": file,
           "text": text}
    if sibling is not None:
        out["sibling"] = sibling
    return out


class Seats(ScriptedSeats):
    """``ScriptedSeats`` whose verifier drops ``drops[contract]`` ids."""

    def __init__(self, findings_by_contract: dict | None = None,
                 drops: dict[str, set[str]] | None = None):
        super().__init__(findings_by_contract)
        self.drops = drops or {}

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat != "verifier":
            return super().__call__(seat, prompt, run_dir, profile, **kwargs)
        contract = re.search(r'"id": "(EVAL-[^"]+)"', prompt).group(1)
        self.calls.append((seat, contract))
        found = json.loads(_FINDINGS_ARRAY.search(prompt).group(1))
        dropped = self.drops.get(contract, set())
        votes = [
            {"id": f["id"], "keep": False, "reason": "not a real defect"}
            if f["id"] in dropped else
            {"id": f["id"], "keep": True, "reason": "confirmed",
             "tag": "important"}
            for f in found
        ]
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 8, "output_tokens": 4},
            result=json.dumps({"votes": votes}),
        )


class SplitSeats(Seats):
    """Reviewer seats disagree on ``contract``: the first reviewer call
    returns ``first``, every later one ``rest`` (each a findings list; a
    non-empty list declares fail). ``drops``: the verifier votes these
    ids out — a finding only one seat reported is not unanimous, so the
    drop stands."""

    def __init__(self, contract: str, first: list, rest: list,
                 drops: set[str] | None = None):
        super().__init__(drops={contract: drops or set()})
        self.contract = contract
        self.first = first
        self.rest = rest
        self._lock = threading.Lock()
        self._n = 0

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "reviewer" and f'"id": "{self.contract}"' in prompt:
            with self._lock:
                n = self._n
                self._n += 1
            found = self.first if n == 0 else self.rest
            self.calls.append((seat, self.contract))
            return SessionRecord(
                seat=seat, status="completed", num_turns=2,
                model="stub-model",
                tokens={"input_tokens": 10, "output_tokens": 5},
                result=json.dumps({"verdict": "fail" if found else "pass",
                                   "findings": found}),
            )
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


_TIMING = re.compile(r"\bin \d+(?:\.\d+)?s\b")


def normalised(record: dict, root=None) -> str:
    """The record as JSON with pytest's timing line blanked — the only
    run-to-run noise in a fixture record (durations are not stored) —
    and, given the fixture's ``root``, its path and its base commit's
    sha replaced: two fixtures built a second apart differ in both, and
    nothing else."""
    text = json.dumps(record, sort_keys=True)
    if root is not None:
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "main"],
                             capture_output=True, text=True,
                             check=True).stdout.strip()
        text = text.replace(str(root), "<repo>").replace(sha[:12], "<base>")
    return _TIMING.sub("in Ns", text)
