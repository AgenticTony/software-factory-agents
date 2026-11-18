"""Shared harness moved from tests/test_f16.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f16.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

import json
import re

from factory.eval import seed_by_id
from factory.session import SessionRecord

_FINDINGS_ARRAY = re.compile(r"```json\n(\[\s*\{.*?\}\s*\])\n```", re.S)


def _finding():
    return {
        "id": "R-1",
        "axis": "standards",
        "severity": "blocking",
        "file": "tests/test_api.py",
        "text": "the test mocks checkin_count — the function it claims to test",
    }


class ScriptedSeats:
    """Scripted reviewer/verifier seats, keyed by the eval contract id
    in the prompt — the spec's opaque SLOT (F-99 round 2: the seed id
    never reaches a seat, so seats route by slot too). Reviewers return
    the scripted findings for their diff (an empty list is a declared
    pass); the verifier keeps every finding. Same seat signature the
    F-38 fan-out calls the real runner with.
    """

    def __init__(self, findings_by_contract: dict[str, list] | None = None):
        self.findings_by_contract = findings_by_contract or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        match = re.search(r'"id": "(EVAL-[^"]+)"', prompt)
        assert match, f"no eval contract id in the {seat} prompt"
        contract_id = match.group(1)
        self.calls.append((seat, contract_id))
        if seat == "reviewer":
            findings = self.findings_by_contract.get(contract_id, [])
            verdict = "fail" if findings else "pass"
            return SessionRecord(
                seat=seat, status="completed", num_turns=2, model="stub-model",
                tokens={"input_tokens": 10, "output_tokens": 5},
                result=json.dumps({"verdict": verdict, "findings": findings}),
            )
        found = json.loads(_FINDINGS_ARRAY.search(prompt).group(1))
        votes = [
            {"id": f["id"], "keep": True, "reason": "confirmed against the diff",
             "tag": "important"}
            for f in found
        ]
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 8, "output_tokens": 4},
            result=json.dumps({"votes": votes}),
        )


MOCK_SEED_FINDINGS = {
    seed_by_id("mocks-what-it-tests").slot: [_finding()],
    # F-95: the reviewer that catches every L2 seed also reports the
    # sibling seed's untouched second site (flagged, so the round
    # passes) and the caller-break seed's broken caller (unflagged, so
    # it blocks).
    seed_by_id("sibling-second-site").slot: [{
        "id": "R-1", "axis": "spec", "severity": "major",
        "file": "app/gear.py", "sibling": True,
        "text": "gear_for_owner accepts a whitespace-only owner_id — the "
                "same class the diff fixes in require_owner",
    }],
    seed_by_id("caller-break").slot: [{
        "id": "R-1", "axis": "spec", "severity": "blocking",
        "file": "app/report.py",
        "text": "pack_summary still reads i['item'] but pack_list now "
                "returns plain names",
    }],
}
