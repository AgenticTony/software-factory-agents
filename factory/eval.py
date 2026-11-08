"""Seeded-bug validator eval (BUILD-ORDER F-16).

A validator nobody has tested is just another claim (VALIDATOR-SPEC
"Validating the validator"). This module is the monthly, per-project
job: seed the known bug classes on branches of a fixture repo, run the
validator's layers over each seeded diff (plus known-clean diffs), and
record the **catch rate per layer**. A layer that catches nothing gets
fixed or dropped.

The fixture set is the SoloAdventurer fixture (:func:`build_soloadventurer_fixture`)
— a small offline repo shaped like the real SoloAdventurer app: a
Supabase-style store whose tables are named in a committed schema, a
phantom-table ratchet script (the real repo's ``check-schema-refs.py``),
and pytest coverage. Every seeded bug is one of the five classes
VALIDATOR-SPEC names: a phantom table name, a removed ownership (RLS)
filter, an off-by-one, a deleted ``await``, and a test that mocks the
thing it claims to test — plus a contract test that can't fail (the
class L1 exists for).

Layers:

* **L0** (:func:`factory.l0.run_l0`) — commands, diff policy, sentinels.
* **L1** (:func:`factory.l1.run_l1`) — the red-first proof of the seed's
  contract tests.
* **L2** — F-38's parallel reviewer and verifier over the seeded diff
  (:func:`run_l2_review`), rendering its verdict through the SAME shared
  post-review chain the orchestrator runs
  (:func:`factory.l2.settle_review_verdict`, F-48): union, verifier
  vote, convergence — quota checked before any parse, exactly
  production's order.

It runs on demand and monthly — never in routine CI: with real L2 it
drives real model sessions (:func:`run_on_demand_eval`, the
``factory eval`` command). Tests drive it with scripted seats; nothing
in this module is on any routine path. Each diff runs ``repeats`` times
(default :data:`DEFAULT_REPEATS`, F-48): one run is a sample, and every
rate in the report carries its mean and spread over the repeats.
"""

import contextlib
import json
import shlex
import shutil
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from factory.contract import apply_profile_defaults
from factory.gates import git as hardened_git
from factory.l0 import run_l0
from factory.l1 import run_l1
from factory.l2 import (
    merge_reviews,
    parse_reviews_with_retry,
    parse_verifier_vote,
    settle_review_verdict,
)
from factory.session import run_session_sandboxed, run_sessions_parallel

# Per-session budgets for the eval's L2 seats. An eval diff is tiny
# compared to a real attempt; the caps keep a confused session bounded.
SEAT_MAX_TURNS = 20
SEAT_TIMEOUT_MIN = 10.0
DEFAULT_REVIEWERS = 2
# F-48 (I-12): how many times each seed/clean diff runs per eval. One
# run is a ratchet, not a measurement — the report carries mean and
# spread. The default (3) is the contract's; the owner confirms default
# and cadence at approval. N=1 renders spread n/a, never 0.
DEFAULT_REPEATS = 3


class EvalError(RuntimeError):
    """The eval cannot run as asked (bad fixture, bad seed, git failure)."""


@dataclass(frozen=True)
class SeedSpec:
    """One seeded bug: edits applied on a branch of the fixture repo.

    ``layer`` names the layer the class exists to be caught by — the
    spec the eval is measured against (an independent expectation: the
    report never reads this field to decide what was caught).

    ``contract`` is the per-seed task acceptance used by L1 (only the
    ``tautological-test`` seed carries one: the other classes ship no
    contract test, and L1 passes trivially without refs).

    ``markers`` are the strings a catch must name: the phantom table's
    name, the read whose filter was dropped, and so on. They are the
    independent expectation the report is measured against — attribution
    never runs off a layer verdict alone (a layer that failed over
    findings unrelated to the seed caught nothing).
    """

    id: str
    layer: str
    title: str
    markers: tuple[str, ...] = ()
    edits: tuple[tuple[str, str], ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    contract: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CleanSpec:
    """A known-clean diff: the validator must pass it and L2 must draw
    no blocking finding on it (AC-3's false-positive dial)."""

    id: str
    title: str
    edits: tuple[tuple[str, str], ...] = ()
    files: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The SoloAdventurer fixture
# ---------------------------------------------------------------------------

SCHEMA_TABLES = ["checkins", "gear", "trip_archive", "trips"]

STORE_SOURCE = '''"""The in-memory stand-in for the Supabase tables SoloAdventurer reads."""

import asyncio

TRIPS = [
    {"id": "t1", "owner_id": "u1", "name": "Jotunheimen", "days": 5},
    {"id": "t2", "owner_id": "u1", "name": "Lofoten", "days": 3},
    {"id": "t3", "owner_id": "u2", "name": "Dolomites", "days": 7},
]
GEAR = [
    {"id": "g1", "trip_id": "t1", "item": "tent"},
    {"id": "g2", "trip_id": "t1", "item": "stove"},
    {"id": "g3", "trip_id": "t1", "item": "map"},
    {"id": "g4", "trip_id": "t1", "item": "rope"},
    {"id": "g5", "trip_id": "t2", "item": "kayak"},
]
CHECKINS = {"t1": {"trip_id": "t1", "count": 12}}


def query(table):
    rows = {"trips": TRIPS, "gear": GEAR}
    return list(rows.get(table, []))


def insert(table, row):
    return {"table": table, **row}


async def fetchone(table, where):
    await asyncio.sleep(0)
    stored = CHECKINS if table == "checkins" else {}
    row = stored.get(where["trip_id"], {"trip_id": where["trip_id"], "count": 0})
    return dict(row)
'''

API_SOURCE = '''"""Trip queries — every read and write names its table."""

from app import store


def trips_for(owner_id):
    """The owner's own trips. The RLS mirror of this read: an owner sees
    only the rows whose owner_id is theirs."""
    rows = store.query("trips")
    return [row for row in rows if row["owner_id"] == owner_id]


def pack_list(trip_id, limit=3):
    """The first ``limit`` gear items on a trip's pack-list page."""
    items = [i for i in store.query("gear") if i["trip_id"] == trip_id]
    return items[:limit]


def archive_trip(trip_id):
    """Archive a completed trip."""
    return store.insert("trip_archive", {"trip_id": trip_id})


async def checkin_count(trip_id):
    row = await store.fetchone("checkins", {"trip_id": trip_id})
    return row["count"]
'''

TEST_SOURCE = '''import asyncio

from app.api import archive_trip, checkin_count, pack_list, trips_for


def test_owner_sees_only_their_trips():
    names = [t["name"] for t in trips_for("u1")]
    assert names == ["Jotunheimen", "Lofoten"]


def test_pack_list_is_capped_at_limit():
    items = [g["item"] for g in pack_list("t1", limit=2)]
    assert items == ["tent", "stove"]


def test_archive_returns_the_trip():
    assert archive_trip("t1")["trip_id"] == "t1"


def test_checkin_count_comes_from_the_table():
    assert asyncio.run(checkin_count("t1")) == 12
'''

RATCHET_SOURCE = '''#!/usr/bin/env python3
"""The phantom-table ratchet (SoloAdventurer's check-schema-refs.py).

Scans app/ for store table targets — query/insert/fetchone/rpc — and
fails naming file:line for any target the committed schema doesn't
know. This is the standing guard the monthly eval's phantom-table seed
must be caught by: a renamed table in a write path no test covers is
invisible to the test gate and loud to this ratchet.
"""

import json
import re
import sys
from pathlib import Path

CALL = re.compile(
    r"""\\bstore\\.(query|insert|fetchone|rpc)\\(\\s*["']([A-Za-z][\\w-]*)["']"""
)


def main() -> int:
    tables = set(json.loads(Path("schema/tables.json").read_text()))
    problems = []
    for path in sorted(Path("app").rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for _, table in CALL.findall(line):
                if table not in tables:
                    problems.append(f"{path}:{lineno}: unknown table '{table}'")
    if problems:
        print("\\n".join(problems))
        return 1
    print("schema refs ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

FIXTURE_FILES = {
    "schema/tables.json": json.dumps(SCHEMA_TABLES, indent=2) + "\n",
    "app/__init__.py": "",
    "app/store.py": STORE_SOURCE,
    "app/api.py": API_SOURCE,
    "tests/__init__.py": "",
    "tests/test_api.py": TEST_SOURCE,
    "scripts/check_schema_refs.py": RATCHET_SOURCE,
}


def eval_profile(repo: Path, python: str | None = None) -> dict:
    """The fixture project's profile: what the gates run and what the
    L2 seats are allowed to read.

    ``python`` overrides the interpreter in the command strings; the
    default is this process's (the F-38 fixture convention — under the
    gate sandbox the commands run exactly as a real profile's do, with
    the run-local gate venv first on PATH). The profile routes through
    :func:`factory.contract.apply_profile_defaults` like every loaded
    profile, so the schema-level runner-config default (F-46 AC-4) is
    unioned in here too.
    """
    python = python or sys.executable
    quoted = shlex.quote(python)
    return apply_profile_defaults({
        "name": "soloadventurer",
        "repo": {
            "path": str(repo),
            "default_branch": "main",
            "remote": "",
            "merge_method": "squash",
        },
        "commands": {
            "setup": "true",
            "test": f"{quoted} -m pytest -q -p no:cacheprovider",
        },
        "paths": {
            "risk": {"schema": ["app/**"]},
            "protected": [".factory/**", "schema/**"],
        },
        "dependency_manifests": [],
        "sentinels": {
            "schema": [
                {
                    "command": f"{quoted} scripts/check_schema_refs.py",
                    "description": "phantom-table ratchet (schema refs)",
                }
            ]
        },
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
        "network_allowlist": [],
    })


def build_soloadventurer_fixture(dest: Path) -> Path:
    """Build the SoloAdventurer fixture repo offline and return its path.

    One base commit on ``main``: the store, the trip queries, pytest
    coverage and the phantom-table ratchet, with the profile the gates
    and sessions use committed under ``.factory/project.yaml``.
    """
    dest = Path(dest)
    if dest.exists():
        raise EvalError(f"fixture destination already exists: {dest}")
    dest.mkdir(parents=True)
    for rel, text in FIXTURE_FILES.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    target = dest / ".factory"
    target.mkdir()
    (target / "project.yaml").write_text(_dump_yaml(eval_profile(dest)))
    _git(dest, "init", "-b", "main")
    _git(dest, "config", "user.email", "fixture@example.com")
    _git(dest, "config", "user.name", "Fixture")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", "base")
    return dest


def _dump_yaml(profile: dict) -> str:
    import yaml

    return yaml.safe_dump(profile, sort_keys=False)


def _git(repo: Path, *args: str) -> str:
    return hardened_git(repo, *args).strip()


# ---------------------------------------------------------------------------
# The seeds (VALIDATOR-SPEC's bug classes) and the known-clean diffs
# ---------------------------------------------------------------------------

SOLOADVENTURER_SEEDS = [
    SeedSpec(
        id="phantom-table",
        layer="L0",
        title="a write path retargeted to a table the schema doesn't know",
        markers=("archived_trips",),
        edits=(
            ('store.insert("trip_archive", {"trip_id": trip_id})',
             'store.insert("archived_trips", {"trip_id": trip_id})'),
        ),
    ),
    SeedSpec(
        id="removed-rls-filter",
        layer="L0",
        title="the ownership filter dropped from an owner-scoped read",
        markers=("owner", "trips_for", "rows"),
        edits=(
            ('    return [row for row in rows if row["owner_id"] == owner_id]',
             "    return list(rows)"),
        ),
    ),
    SeedSpec(
        id="off-by-one",
        layer="L0",
        title="the pack-list cap reads one item past the limit",
        markers=("pack_list", "limit"),
        edits=(
            ("    return items[:limit]", "    return items[: limit + 1]"),
        ),
    ),
    SeedSpec(
        id="deleted-await",
        layer="L0",
        title="the await dropped from the checkin-count fetch",
        markers=("checkin_count", "fetchone", "await", "coroutine"),
        edits=(
            ("    row = await store.fetchone(", "    row = store.fetchone("),
        ),
    ),
    SeedSpec(
        id="mocks-what-it-tests",
        layer="L2",
        title="a test that mocks the function it claims to test",
        markers=("mock", "checkin_count"),
        edits=(
            (
                "import asyncio\n\nfrom app.api import archive_trip, checkin_count, "
                "pack_list, trips_for\n",
                "from unittest import mock\n\nfrom app import api\n"
                "from app.api import archive_trip, pack_list, trips_for\n",
            ),
            (
                "def test_checkin_count_comes_from_the_table():\n"
                '    assert asyncio.run(checkin_count("t1")) == 12\n',
                "def test_checkin_count_comes_from_the_table():\n"
                '    with mock.patch.object(api, "checkin_count",\n'
                "                           mock.Mock(return_value=12)):\n"
                '        assert api.checkin_count("t1") == 12\n',
            ),
        ),
    ),
    SeedSpec(
        id="tautological-test",
        layer="L1",
        title="a contract test that already passes on base — it proves nothing",
        markers=("test_archive", "tautolog", "proves nothing", "already pass"),
        files={
            "tests/test_archive.py": (
                "from app.api import archive_trip\n"
                "\n"
                "\n"
                "def test_archive_trip_archives():\n"
                '    assert archive_trip("t1")["trip_id"] == "t1"\n'
            )
        },
        contract={
            "id": "AC-1",
            "text": "archiving a trip returns the archived trip",
            "check": {
                "type": "test",
                "ref": "tests/test_archive.py::test_archive_trip_archives",
            },
        },
    ),
]

CLEAN_DIFFS = [
    CleanSpec(
        id="clean-docstring",
        title="a clearer docstring on an owner-scoped read",
        edits=(
            (
                '    """The owner\'s own trips. The RLS mirror of this read: an owner sees\n'
                '    only the rows whose owner_id is theirs."""\n',
                '    """Only the given owner\'s trips — what RLS enforces on the\n'
                '    table, this query repeats in application code."""\n',
            ),
        ),
    ),
    CleanSpec(
        id="clean-new-helper",
        title="a new pure helper with its own coverage",
        files={
            "app/logbook.py": (
                '"""Trip logbook helpers."""\n'
                "\n"
                "\n"
                "def logbook_line(trip) -> str:\n"
                '    return f"{trip[\'name\']} — {trip[\'days\']} days"\n'
            ),
            "tests/test_logbook.py": (
                "from app.logbook import logbook_line\n"
                "\n"
                "\n"
                "def test_logbook_line_names_the_trip():\n"
                '    assert logbook_line({"name": "Lofoten", "days": 3}) == \\\n'
                '        "Lofoten — 3 days"\n'
            ),
        },
    ),
]


def seed_by_id(seed_id: str) -> SeedSpec:
    for seed in SOLOADVENTURER_SEEDS:
        if seed.id == seed_id:
            return seed
    raise EvalError(f"no seeded bug named {seed_id!r}")


# ---------------------------------------------------------------------------
# Running the eval
# ---------------------------------------------------------------------------


def run_seed(
    repo: Path,
    seed: SeedSpec | CleanSpec,
    *,
    base_ref: str = "main",
    command_runner=None,
    session_runner=None,
    reviewers: int = DEFAULT_REVIEWERS,
    factory_root: Path | None = None,
) -> dict:
    """Run the validator's layers over one seeded (or clean) diff.

    Creates branch ``eval/<id>`` from ``base_ref`` in the fixture repo,
    applies the seed, and runs L0 and L1 — plus L2 as F-38's parallel
    reviewer and verifier when ``session_runner`` is given (the real
    runner drives real model sessions; tests script it). The repo is
    left on the seed's branch.

    Returns the per-diff record: each layer's report dict and the layers
    that caught the seed (a failing layer whose own recorded evidence
    names one of the seed's ``markers``); a clean diff carries the number
    of blocking findings its review drew — ``None`` when L2 never ran.
    """
    repo = Path(repo)
    profile = eval_profile(repo)
    task = _seed_task(seed)
    _git(repo, "checkout", "--force", base_ref)
    _git(repo, "checkout", "-B", f"eval/{seed.id}", base_ref)
    _apply_diff(repo, seed)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"seed: {seed.id}")

    l0 = run_l0(profile, repo, base_ref, f"eval/{seed.id}", task=task,
                runner=command_runner)
    l1 = run_l1(profile, repo, base_ref, f"eval/{seed.id}", task=task,
                runner=command_runner)
    layers = {"L0": l0.to_dict(), "L1": l1.to_dict()}
    if session_runner is not None:
        report, retries = run_l2_review(
            repo, profile, base_ref, f"eval/{seed.id}", task,
            session_runner=session_runner, reviewers=reviewers,
            l0=l0, l1=l1, factory_root=factory_root,
        )
        if report is None:
            # Quota-voided (PR #90 round 4): never a verdict — excluded
            # from the catch and false-fail denominators, counted on
            # the report's own quota-voided line instead.
            layers["L2"] = {"quota_voided": True,
                            "review_retries": retries}
        else:
            layers["L2"] = report.to_dict()
            # The fan-out's retry ledger for this diff (0 when every
            # seat parsed first time).
            layers["L2"]["review_retries"] = retries
    record = {
        "id": seed.id,
        "title": seed.title,
        "expected_layer": getattr(seed, "layer", None),
        "layers": layers,
    }
    if isinstance(seed, SeedSpec):
        record["caught_by"] = [
            name for name, report in layers.items()
            if _catches(report, seed.markers)
        ]
    else:
        l2 = layers.get("L2") or {}
        record["blocking_findings"] = (
            None if l2.get("quota_voided")
            else (_count_blocking(l2) if "L2" in layers else None)
        )
    return record


def _catches(report: dict, markers: tuple[str, ...]) -> bool:
    """True when a layer's own recorded evidence names the seeded defect.

    The verdict alone is not the catch (a layer that failed over
    findings unrelated to the seed caught nothing — exactly the false
    positive the known-clean diffs exist to dial). The evidence is what
    the layer itself recorded: the output of the commands and sentinels
    that failed, L1's red-reason table, and — for L2 — the surviving
    findings (a finding the verifier dropped never survived, so it
    never counts).
    """
    if report.get("verdict") in (None, "pass") or not markers:
        return False
    evidence = "\n".join(
        part for part in _catch_evidence(report) if part
    ).lower()
    return any(marker.lower() in evidence for marker in markers)


def _catch_evidence(report: dict) -> list[str]:
    """The human-readable output a layer's failure is attributed to."""
    parts = []
    for command in report.get("commands") or []:
        if (isinstance(command, dict)
                and (command.get("exit_code") not in (0, None)
                     or command.get("timed_out"))):
            parts.append(str(command.get("output") or ""))
    for sentinel in report.get("sentinel_results") or []:
        if isinstance(sentinel, dict) and sentinel.get("verdict") == "fail":
            parts.append(str(sentinel.get("detail") or ""))
    red_reasons = report.get("red_reasons")
    if isinstance(red_reasons, dict):
        parts += [f"{ref} {reason or ''}" for ref, reason in red_reasons.items()]
    for finding in report.get("findings") or []:
        if isinstance(finding, dict):
            parts.append(f"{finding.get('file') or ''} "
                         f"{finding.get('text', '')}")
    return parts


def _seed_task(seed: SeedSpec | CleanSpec) -> dict:
    contract = seed.contract if isinstance(seed, SeedSpec) else None
    acceptance = [contract] if contract else []
    return {
        "id": f"EVAL-{seed.id}",
        "title": seed.title,
        "acceptance": acceptance,
        "risk": [],
    }


def _apply_diff(repo: Path, seed: SeedSpec | CleanSpec) -> None:
    for rel, text in seed.files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for old, new in seed.edits:
        for rel in _files_with(repo, old):
            path = repo / rel
            path.write_text(path.read_text().replace(old, new, 1))
            break
        else:
            raise EvalError(f"seed {seed.id}: edit target not found: {old!r}")


def _files_with(repo: Path, needle: str) -> list[str]:
    return [
        str(path.relative_to(repo))
        for path in sorted(repo.rglob("*.py"))
        if needle in path.read_text()
    ]


def run_l2_review(
    repo: Path,
    profile: dict,
    base_ref: str,
    head_ref: str,
    task: dict,
    *,
    session_runner,
    reviewers: int = DEFAULT_REVIEWERS,
    l0=None,
    l1=None,
    factory_root: Path | None = None,
):
    """L2 as F-38's parallel reviewer and verifier over one diff.

    The orchestrator's pipeline: N reviewer sessions over the same diff
    (a fresh context each, run concurrently through
    :func:`factory.session.run_sessions_parallel`), the verdicts merged
    into a union, and — when the union carries findings — one verifier
    session voting the union down. The verdict is settled through the
    SAME shared post-review chain production renders (F-48, M-3:
    :func:`factory.l2.settle_review_verdict`) in the FIRST-ROUND mode a
    single-round review is: empty prior, no delta, ``delta_mode=False``,
    no final review — for a one-round task the per-round review is what
    gates publish (the F-41 final review runs only after a delta round
    passed, which a one-round task never has).

    Quota is checked exactly where production checks it (F-48, m-12):
    BEFORE any parse or re-run on the fan-out's own records, and before
    the verifier's output is parsed as a vote. A seat whose first run
    died on quota is never parsed, never re-run and never counted as a
    retry; one quota hit anywhere voids the diff's review (PR #90 round
    4): the report is ``None`` — quota noise never becomes a verdict, a
    retry, a catch rate or a false fail. A seat whose verdict merely
    fails to parse still gets exactly ONE re-run with the same prompt
    (:func:`factory.l2.parse_reviews_with_retry`); a still-invalid retry
    fails the union as always.

    Args:
        l0 / l1: The layer reports the reviewers see alongside the diff
            (the orchestrator hands its reviewers the same). Defaults to
            the report dicts already on the record when re-reviewing.

    Returns:
        ``(report, retries)`` — the L2 report (``None`` when the review
        was quota-voided), and how many reviewer re-runs the retry
        policy spent on seats whose first verdict failed to parse.
    """
    from factory import orchestrator
    from factory.prompts import assemble_reviewer_prompt, assemble_verifier_prompt

    diff = _git(repo, "diff", "--no-color", "--no-ext-diff",
                f"{base_ref}...{head_ref}")
    l0_dict = l0.to_dict() if l0 is not None else {}
    l1_dict = l1.to_dict() if l1 is not None else {}
    review_prompt = orchestrator._review_prompt(task, diff, l0_dict, l1_dict)
    retry_records: list = []

    def _rerun_review_seat(seat_no=None, session=None, parse_error=None):
        """One retry of one reviewer seat: the same prompt through the
        same fan-out machinery — parse_reviews_with_retry drives it (it
        passes the seat's 1-based position, its original session and the
        parse failure, none of which the eval needs: it has no attempt
        budget to fit and no trace to name the seat) and decides whether
        the retry's verdict is used."""
        record = run_sessions_parallel(
            [("reviewer", review_prompt)],
            repo, profile,
            runner=session_runner,
            factory_root=factory_root,
            max_turns=SEAT_MAX_TURNS,
            timeout_min=SEAT_TIMEOUT_MIN,
            protected=list((profile.get("paths") or {}).get("protected") or []),
            system_prompt=assemble_reviewer_prompt(profile),
        )[0]
        retry_records.append(record)
        return record

    reviews = run_sessions_parallel(
        [("reviewer", review_prompt)] * reviewers,
        repo, profile,
        runner=session_runner,
        factory_root=factory_root,
        max_turns=SEAT_MAX_TURNS,
        timeout_min=SEAT_TIMEOUT_MIN,
        protected=list((profile.get("paths") or {}).get("protected") or []),
        system_prompt=assemble_reviewer_prompt(profile),
    )
    # m-12: quota BEFORE parsing, production's order — a quota-hit
    # first-run seat is never parsed, never re-run (no tokens burned
    # against a dead quota), never counted as a retry.
    if any(orchestrator._is_quota(r) for r in reviews):
        return None, 0
    reports, retries = parse_reviews_with_retry(
        reviews, _rerun_review_seat, stop=orchestrator._is_quota)
    if any(orchestrator._is_quota(r) for r in retry_records):
        return None, retries
    union = merge_reviews(reports)
    vote = None
    if union.parse_error is None and union.findings:
        verifier_prompt = orchestrator._verifier_prompt(task, diff, union.findings)
        verifier = session_runner(
            "verifier", verifier_prompt, repo, profile,
            factory_root=factory_root,
            max_turns=SEAT_MAX_TURNS,
            timeout_min=SEAT_TIMEOUT_MIN,
            protected=list((profile.get("paths") or {}).get("protected") or []),
            system_prompt=assemble_verifier_prompt(profile),
        )
        # The same class on the verifier: production aborts on a
        # quota-hit verifier before parsing its output — the dead
        # session is never read as a no-vote that keeps every finding.
        if orchestrator._is_quota(verifier):
            return None, retries
        vote = parse_verifier_vote(
            verifier.result if verifier.status == "completed" else None,
            union.findings,
        )
    # M-3: the ONE shared chain, first-round mode (empty prior, no
    # delta) — the verdict the queue would render for this review.
    return settle_review_verdict(
        union, {}, frozenset(), delta_mode=False,
        vote=vote, reviewers=reviewers,
    ), retries


def _count_blocking(l2: dict | None) -> int:
    """Count ``blocking`` findings in a STORED L2 report dict.

    Case-insensitive on purpose (F-44, review M-1): reports stored before
    F-44's parse-time canonicalisation may hold ``"Blocking"`` — this
    reads records on disk, not freshly parsed findings, so it keeps
    tolerating the casing the parser no longer emits."""
    if not l2:
        return 0
    return sum(
        1 for f in l2.get("findings") or []
        if isinstance(f, dict)
        and (f.get("severity") or "").strip().lower() == "blocking"
    )


def _false_fail_by_layer(clean_records: list) -> dict[str, dict | None]:
    """Per layer: how many known-clean diffs that layer FAILED over how
    many it judged (F-48, m-11 — the false-positive aggregate is not
    L2-only: a clean diff a layer wrongly failed is THAT layer's false
    positive, whatever the other layers said). A quota-voided L2 is no
    verdict (PR #90 round 4): it leaves the denominator. ``None`` per
    layer that judged nothing: an unmeasured rate is n/a in the report,
    never a clean bill of 0."""
    out: dict[str, dict | None] = {}
    for layer in LAYERS:
        judged = [r for r in clean_records if _layer_ran(r, layer)]
        if not judged:
            out[layer] = None
            continue
        failed = sum(
            1 for r in judged
            if (r.get("layers") or {}).get(layer, {}).get("verdict") == "fail"
        )
        out[layer] = {"failed": failed, "judged": len(judged)}
    return out


def _clean_review_summary(record: dict) -> str:
    """The verdict half of a clean diff's per-diff line: every layer's
    verdict, L0 first (F-48, m-11 — a clean diff that FAILED a layer
    renders as that layer's false positive, not as a clean diff whose L2
    happened to pass), then L2's blocking count — a fail, above all an
    invalid_verdict_json fail (whose lone finding carries no severity,
    so it counts 0 blocking), must never read as a clean review. A
    quota-voided review says so: it produced no verdict at all."""
    layers = record.get("layers") or {}
    parts: list[str] = []
    for name in ("L0", "L1"):
        verdict = (layers.get(name) or {}).get("verdict") or "unknown"
        if verdict == "fail":
            parts.append(f"{name} fail (false positive)")
        else:
            parts.append(f"{name} {verdict}")
    l2 = layers.get("L2") or {}
    if l2.get("quota_voided"):
        parts.append("L2 quota-voided (no verdict)")
        return ", ".join(parts)
    blocking = record.get("blocking_findings")
    if blocking is None or "L2" not in layers:
        parts.append("L2 n/a (skipped)")
        return ", ".join(parts)
    verdict = l2.get("verdict") or "unknown"
    drew = f"{blocking} blocking finding(s)"
    if verdict == "fail":
        why = "false positive"
        if l2.get("parse_error") is not None:
            why += ", invalid_verdict_json"
        parts.append(f"L2 fail ({why}), {drew}")
    elif l2.get("parse_error") is not None:
        parts.append(f"L2 {verdict} (invalid_verdict_json), {drew}")
    else:
        parts.append(f"L2 {verdict}, {drew}")
    return ", ".join(parts)


def _reviewer_retries(report: dict) -> int:
    """How many reviewer seat re-runs the whole eval spent (per-diff
    ``review_retries`` summed) — the rendered header says it when > 0."""
    records = (report.get("seeds") or []) + (report.get("clean") or [])
    return sum(
        int(((r.get("layers") or {}).get("L2") or {}).get("review_retries") or 0)
        for r in records
    )


LAYERS = ("L0", "L1", "L2")


def run_seeded_eval(
    repo: Path,
    *,
    base_ref: str = "main",
    command_runner=None,
    session_runner=None,
    reviewers: int = DEFAULT_REVIEWERS,
    factory_root: Path | None = None,
    repeats: int = 1,
) -> dict:
    """Run the whole eval over the fixture set and return its report.

    Every seeded bug and every known-clean diff goes through
    :func:`run_seed` — ``repeats`` times each (F-48: one run per seed is
    a ratchet, not a measurement; each record carries its 1-based
    ``repeat``). The report carries the per-run records (so every
    aggregate is recomputable), the pooled catch rate per layer, the
    per-repeat rates with each layer's mean and spread
    (``catch_rate_by_repeat`` / ``catch_rate_stats``), and the per-layer
    clean-diff false-fail counts (``false_fail_on_clean``) with the same
    per-repeat treatment. Quota-voided runs leave every denominator and
    are counted separately (``l2_quota_voided``), as before. Without
    ``session_runner`` the L2 layer is skipped and its rate is n/a — the
    report then says so rather than implying L2 was measured.

    Raises:
        EvalError: If ``repeats`` is not an integer >= 1.
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise EvalError(f"repeats must be an integer >= 1, got {repeats!r}")
    repo = Path(repo)
    common = {
        "base_ref": base_ref,
        "command_runner": command_runner,
        "session_runner": session_runner,
        "reviewers": reviewers,
        "factory_root": factory_root,
    }
    seeds: list = []
    clean: list = []
    for repeat in range(1, repeats + 1):
        for seed in SOLOADVENTURER_SEEDS:
            record = run_seed(repo, seed, **common)
            record["repeat"] = repeat
            seeds.append(record)
        for diff in CLEAN_DIFFS:
            record = run_seed(repo, diff, **common)
            record["repeat"] = repeat
            clean.append(record)
    voided = sum(
        1 for r in seeds + clean
        if ((r.get("layers") or {}).get("L2") or {}).get("quota_voided")
    )
    catch_by_repeat = _catch_rate_by_repeat(seeds)
    ff_by_repeat = _false_fail_by_repeat(clean)
    return {
        "fixture": "soloadventurer",
        "base_ref": base_ref,
        "l2": "sessions" if session_runner is not None else "skipped",
        "repeats": repeats,
        "seeds": seeds,
        "clean": clean,
        # Per layer: how many clean diffs that layer failed over how
        # many it judged (None when it judged nothing) — rendered as the
        # per-layer false-fail lines (F-48, m-11).
        "false_fail_on_clean": _false_fail_by_layer(clean),
        # Per repeat, per layer: that repeat's false-fail COUNT (None
        # when the layer judged nothing that repeat) and the mean and
        # spread over the repeats that judged — n/a, never 0, at one.
        "false_fail_by_repeat": ff_by_repeat,
        "false_fail_stats": {
            layer: _stats(values) for layer, values in ff_by_repeat.items()
        },
        # How many reviews died on quota (round 4): never a verdict, so
        # they leave the rates above and are counted here instead.
        "l2_quota_voided": voided,
        # Pooled across every run (the F-16 shape, now over N runs).
        "catch_rate": catch_rate_by_layer(seeds),
        "caught_counts": caught_counts_by_layer(seeds),
        # Per repeat, per layer: the rate within that repeat (None when
        # the layer judged nothing that repeat), and the mean/spread
        # over the repeats that judged — spread is None (rendered n/a)
        # when only one repeat judged, never a fake 0.
        "catch_rate_by_repeat": catch_by_repeat,
        "catch_rate_stats": {
            layer: _stats(values) for layer, values in catch_by_repeat.items()
        },
    }


def _layer_ran(record: dict, layer: str) -> bool:
    """Whether a layer actually produced a verdict for a diff — a
    quota-voided L2 (PR #90 round 4) produced none, so it leaves the
    catch-rate denominator instead of counting as a miss."""
    layer_dict = (record.get("layers") or {}).get(layer)
    return bool(layer_dict) and not layer_dict.get("quota_voided")


def _layer_totals(seed_records: list) -> dict:
    return {layer: sum(1 for r in seed_records if _layer_ran(r, layer))
            for layer in LAYERS}


def catch_rate_by_layer(seed_records: list) -> dict:
    """The share of seeds each layer caught (a failing layer whose own
    evidence names the seed — see :func:`_catches`), over the seeds the
    layer actually judged: a quota-voided review is no verdict and no
    miss — and a layer that judged NOTHING has no rate at all
    (``None``, rendered n/a), never a false 0%."""
    counts = caught_counts_by_layer(seed_records)
    totals = _layer_totals(seed_records)
    return {
        layer: (counts[layer] / totals[layer]) if totals[layer] else None
        for layer in LAYERS
    }


def caught_counts_by_layer(seed_records: list) -> dict:
    counts = {layer: 0 for layer in LAYERS}
    for record in seed_records:
        for layer in record.get("caught_by") or []:
            if layer in counts:
                counts[layer] += 1
    return counts


def _repeat_of(record: dict) -> int:
    """A record's 1-based repeat number — records written before F-48
    carry none, which reads as repeat 1 (the report they came from ran
    each diff once)."""
    return int(record.get("repeat") or 1)


def _per_repeat(records: list, layer: str, value_of, *, rate: bool = True) -> list:
    """Per repeat, the layer's value over the runs it judged that
    repeat: a catch rate for seeds (``rate=True``, the judged-run mean
    of the 0/1 catches), a fail count for clean diffs (``rate=False``,
    the raw count — the owner reads "1 of 2", and a repeat whose quota
    voids shrank the denominator must not read as a lower count). A
    repeat the layer judged nothing (no runs, or all quota-voided)
    contributes ``None`` — no denominator is not a 0 measurement."""
    repeats = max((_repeat_of(r) for r in records), default=0)
    values: list = []
    for repeat in range(1, repeats + 1):
        judged = [
            r for r in records
            if _repeat_of(r) == repeat and _layer_ran(r, layer)
        ]
        if judged:
            total = sum(value_of(r) for r in judged)
            values.append(total / len(judged) if rate else total)
        else:
            values.append(None)
    return values


def _catch_rate_by_repeat(seed_records: list) -> dict:
    """Each layer's catch RATE within each repeat (None when the layer
    judged nothing that repeat) — the input to the mean/spread stats."""
    return {
        layer: _per_repeat(
            seed_records, layer,
            lambda r: 1 if layer in (r.get("caught_by") or []) else 0,
        )
        for layer in LAYERS
    }


def _false_fail_by_repeat(clean_records: list) -> dict:
    """Each layer's false-fail COUNT within each repeat (None when the
    layer judged nothing that repeat)."""
    return {
        layer: _per_repeat(
            clean_records, layer,
            lambda r: 1 if (r.get("layers") or {}).get(layer, {}).get(
                "verdict") == "fail" else 0,
            rate=False,
        )
        for layer in LAYERS
    }


def _stats(values: list) -> dict | None:
    """Mean and spread (sample stdev, min, max) over the non-None
    values. ``None`` when no value exists (the layer judged nothing at
    all); ``stdev`` is None when only one repeat judged — one sample has
    no spread, rendered n/a, never a fake 0."""
    real_values = [v for v in values if v is not None]
    if not real_values:
        return None
    return {
        "mean": sum(real_values) / len(real_values),
        "stdev": (statistics.stdev(real_values)
                  if len(real_values) > 1 else None),
        "min": min(real_values),
        "max": max(real_values),
        "repeats_judged": len(real_values),
    }


def render_report(report: dict) -> str:
    """The owner-facing per-diff report (the AC-3 tick evidence): per
    seeded bug (per repeat, when the report ran more than one), which
    layers caught it; per known-clean diff, EVERY layer's verdict with
    L2's blocking-finding count — a clean diff a layer wrongly failed
    renders as that layer's false positive (F-48, m-11); then the catch
    rate per layer and the per-layer false-fail lines, each with its
    mean and spread over the repeats (F-48, I-12). The header names the
    repeat count and reviewer re-runs when any fired. Reports written
    before F-48 (no ``repeats``, no per-record ``repeat``) render as
    1-repeat reports — spread n/a — never a crash."""
    retries = _reviewer_retries(report)
    repeats = int(report.get("repeats") or 1)
    header = (
        f"Seeded-bug validator eval — fixture: {report['fixture']} "
        f"(base {report['base_ref']}, L2: {report['l2']}, "
        f"{repeats} repeat" + ("s" if repeats != 1 else "")
    )
    if retries:
        header += f", {retries} reviewer retry" + ("s" if retries != 1 else "")
    lines = [
        header + ")",
        "",
        "## Per diff",
        "",
    ]
    for record in report["seeds"]:
        caught_layers = list(record.get("caught_by") or [])
        voided = ((record.get("layers") or {}).get("L2") or {}).get(
            "quota_voided")
        if caught_layers:
            # Another layer's catch still counts; the void is a
            # parenthetical, never a miss (round 5).
            middle = "caught by " + ", ".join(caught_layers)
            if voided:
                middle += " (L2 quota-voided)"
        elif voided:
            middle = "L2 quota-voided, not judged"
        else:
            middle = "caught by NOT CAUGHT"
        expected = record.get("expected_layer") or "-"
        repeat = _repeat_tag(report, record)
        lines.append(
            f"- seed {record['id']}{repeat}: {middle} "
            f"(expected {expected}) — {record['title']}"
        )
    for record in report["clean"]:
        repeat = _repeat_tag(report, record)
        lines.append(
            f"- clean {record['id']}{repeat}: {_clean_review_summary(record)} "
            f"— {record['title']}"
        )
    lines += ["", "## Catch rate per layer", ""]
    counts = report["caught_counts"]
    totals = _layer_totals(report["seeds"])
    by_repeat = report.get("catch_rate_by_repeat") or _catch_rate_by_repeat(
        report["seeds"])
    for layer in LAYERS:
        rate = report["catch_rate"][layer]
        if rate is None:
            # No denominator anywhere (every seed quota-voided): n/a,
            # the same rule the false-fail lines follow — never a false
            # 0%.
            lines.append(f"- {layer}: n/a (0 judged)")
            continue
        stats = _stats(by_repeat[layer])
        if stats is None:
            # No per-repeat values derivable (a report whose records
            # carry a pooled rate but no judgeable runs — a hand-built
            # or trimmed report): the pooled line, no spread claim.
            lines.append(
                f"- {layer}: {rate:.0%} "
                f"({counts[layer]} of {totals[layer]} seeds)"
            )
            continue
        judged = stats["repeats_judged"]
        if stats["stdev"] is None:
            # One repeat judged: no spread to state — n/a, never a
            # rendered 0 that fakes a measurement.
            tail = (f"pooled {counts[layer]} of {totals[layer]} seed runs; "
                    f"spread n/a, {judged} judged repeat"
                    + ("" if judged == 1 else "s"))
        else:
            tail = (f"pooled {counts[layer]} of {totals[layer]} seed runs; "
                    f"mean of {judged} repeats: stdev {stats['stdev']:.0%}, "
                    f"min–max {stats['min']:.0%}–{stats['max']:.0%}")
        lines.append(f"- {layer}: {stats['mean']:.0%} ({tail})")
    # PR #90 round 2 → F-48 (m-11): the catch-rate table's false-positive
    # counterpart over the known-clean diffs, PER LAYER — a clean diff a
    # layer wrongly failed is that layer's false positive. n/a (not 0)
    # per layer that judged nothing. The spread is over each repeat's
    # failed-clean-diff COUNT.
    lines += ["", "## False-fail on clean diffs", ""]
    false_fail = _false_fail_by_layer(report.get("clean") or [])
    ff_by_repeat = report.get("false_fail_by_repeat") or _false_fail_by_repeat(
        report.get("clean") or [])
    for layer in LAYERS:
        pooled = false_fail[layer]
        if pooled is None:
            lines.append(f"- {layer}: n/a (0 judged)")
            continue
        stats = _stats(ff_by_repeat[layer])
        if stats is None:
            # Judged runs exist but no per-repeat values are derivable
            # (a trimmed report): the pooled counts, no spread claim.
            lines.append(f"- {layer}: {pooled['failed']} of {pooled['judged']} "
                         "clean runs")
            continue
        judged = stats["repeats_judged"]
        if stats["stdev"] is None:
            tail = f"spread n/a, {judged} judged repeat" + (
                "" if judged == 1 else "s")
        else:
            tail = (f"mean {stats['mean']:.1f}/repeat, "
                    f"stdev {stats['stdev']:.1f}, "
                    f"min–max {stats['min']:.0f}–{stats['max']:.0f}")
        lines.append(
            f"- {layer}: {pooled['failed']} of {pooled['judged']} clean runs "
            f"({tail})"
        )
    # PR #90 round 4: quota noise is quarantined here, out of the tick
    # evidence above — a quota-voided review is no verdict.
    total_runs = len(report.get("seeds") or []) + len(report.get("clean") or [])
    if report.get("l2") == "sessions" and total_runs:
        lines.append(
            f"L2 quota-voided: {report.get('l2_quota_voided', 0)} "
            f"of {total_runs} L2 runs"
        )
    else:
        lines.append(f"L2 quota-voided: {report.get('l2_quota_voided', 0)}")
    lines.append("")
    return "\n".join(lines)


def _repeat_tag(report: dict, record: dict) -> str:
    """The per-diff line's repeat marker — shown only when the report ran
    more than one repeat (pre-F-48 reports and single runs stay clean)."""
    repeats = int(report.get("repeats") or 1)
    if repeats <= 1:
        return ""
    return f" [r{_repeat_of(record)}]"


def run_on_demand_eval(
    factory_root: Path | None = None,
    out_path: Path | None = None,
    *,
    reviewers: int = DEFAULT_REVIEWERS,
    repeats: int = DEFAULT_REPEATS,
    session_runner=None,
    command_runner=None,
    keep_fixture: bool = False,
) -> dict:
    """One on-demand (or monthly) eval run — the AC-3 owner step.

    Builds a fresh SoloAdventurer fixture, runs every seeded bug and
    known-clean diff through the full validator — L0 and L1 through the
    pinned srt sandbox (:func:`factory.orchestrator.sandboxed_runner`,
    the same gate path a real run uses), L2 as F-38's parallel reviewer
    and verifier over **real model sessions**
    (:func:`factory.session.run_session_sandboxed`, the default) — and
    writes the per-diff report next to the factory's own records. This
    is what ``factory eval`` runs and what the owner records as tick
    evidence; nothing routine ever calls it.

    Args:
        factory_root: Factory root (default ``~/factory``); the fixture,
            the sandbox settings and the report live under
            ``<factory_root>/eval/``.
        out_path: Where the report JSON goes (default
            ``<factory_root>/eval/seeded-eval-<stamp>.json``; the
            rendered markdown sits beside it).
        reviewers: Reviewer sessions per diff (F-38's fan-out width);
            ``factory eval`` passes the factory config's ``reviewers``.
        repeats: How many times each diff runs (F-48, I-12 — default
            :data:`DEFAULT_REPEATS`, the contract's number; the owner
            confirms default and cadence at approval). Each repeat is a
            full pass of real L2 sessions, so cost scales with N.
        session_runner / command_runner: Overrides for tests; the
            defaults drive real sessions and the real sandbox.
        keep_fixture: Keep the fixture repo for inspection (default:
            removed with the run's scratch; the reports stay).

    Returns:
        ``{"report", "report_path", "rendered_path"}``.
    """
    from factory.orchestrator import _gate_settings, sandboxed_runner
    from factory.runs import FACTORY_ROOT_DEFAULT, RunDir
    # The reach-ins above (with run_l2_review's _review_prompt /
    # _verifier_prompt) are deliberate: AC-3 measures the pipeline the
    # queue actually runs, so the eval calls the orchestrator's own
    # prompt builders and gate settings rather than a copy that could
    # drift. Promoting them to public entry points means editing
    # factory/orchestrator.py — a protected path, so an owner change.

    factory_root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    eval_root = factory_root / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fixture_dir = eval_root / "soloadventurer"
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture = build_soloadventurer_fixture(fixture_dir)
    settings_path = eval_root / f"srt-settings-{stamp}.json"
    profile = eval_profile(fixture)
    if command_runner is None:
        _gate_settings(profile, RunDir("soloadventurer", stamp, fixture,
                                       settings_path), factory_root)
        command_runner = sandboxed_runner(fixture, settings_path)
    try:
        report = run_seeded_eval(
            fixture,
            command_runner=command_runner,
            session_runner=session_runner if session_runner is not None
            else run_session_sandboxed,
            reviewers=reviewers,
            factory_root=factory_root,
            repeats=repeats,
        )
        out_path = Path(out_path) if out_path else (
            eval_root / f"seeded-eval-{stamp}.json"
        )
        out_path.write_text(json.dumps(report, indent=2) + "\n")
        rendered_path = out_path.with_suffix(".md")
        rendered_path.write_text(render_report(report))
        return {
            "report": report,
            "report_path": out_path,
            "rendered_path": rendered_path,
        }
    finally:
        with contextlib.suppress(FileNotFoundError):
            settings_path.unlink()
        if not keep_fixture:
            shutil.rmtree(fixture_dir, ignore_errors=True)


# ------------------------------------------------- F-57: the importer eval --

# The oversized prose work order (AC-3/AC-4): the doc the split rule is
# measured against.
IMPORTER_OVERSIZED_DOC = "docs/offline-mode.md"

# The prose-import fixture's checkout location. The second allowed
# __file__-escape beside the sandbox's srt runtime lookup (both pinned
# in tests/test_f51.py's allowlist): this eval is an owner-run command
# from the checkout, and the fixture — deliberately test data, protected
# under tests/fixtures/** — is not package data (a wheel ships no
# tests/, and the eval's refusal says so).
_IMPORTER_FIXTURE_ROOT = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    / "prose-import"
)


def _importer_fixture_source() -> Path:
    """The prose-import fixture's home in the checkout (see
    :data:`_IMPORTER_FIXTURE_ROOT` — why it is not package data)."""
    return _IMPORTER_FIXTURE_ROOT


def _build_importer_fixture(dest: Path) -> Path:
    """Copy the prose fixture into ``dest`` and commit it: a real git
    repo (the sandboxed importer session clones committed HEAD), with
    NO queue — the import proposes one."""
    source = _importer_fixture_source()
    if not source.is_dir():
        raise EvalError(
            f"the prose-import fixture is missing at {source} — the "
            "importer eval runs from the factory checkout"
        )
    shutil.copytree(source, dest)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "eval@factory.local"),
        ("config", "user.name", "factory eval"),
        ("add", "-A"),
        ("commit", "-q", "-m", "prose fixture"),
    ):
        _git(dest, *args)
    return dest


def _importer_eval_refusal(refusal: str) -> dict:
    """The refused arm of the AC-4 report: schema_valid False with the
    refusal text — a faked clean bill is never rendered."""
    return {
        "fixture": "prose-import",
        "schema_valid": False,
        "refusal": refusal,
        "task_count": 0,
        "oversized_doc": IMPORTER_OVERSIZED_DOC,
        "tasks_from_oversized": 0,
        "oversized_split": None,
        "tool_used": False,
        "propose_calls": 0,
        "accepted_tasks": 0,
        "repair": None,
        "precedence_note": None,
    }


def _importer_eval_report(result) -> dict:
    """The succeeded arm of the AC-4 report: schema validity (judged by
    the real pipeline — the import, its refusal and the ONE repair all
    included), task count, whether the oversized work order came out
    split, and HOW the plan was produced (S2, review round 1: the
    propose tool's use, the repair's outcome and turns, the precedence
    note) — first-pass success and a repaired one never read the same."""
    tasks = result.plan.proposed_doc.get("tasks", [])
    from_oversized = [
        task for task in tasks
        if str(task.get("source", "")).startswith(IMPORTER_OVERSIZED_DOC)
    ]
    repair = dict(result.repair) if result.repair else None
    return {
        "fixture": "prose-import",
        "schema_valid": True,
        "refusal": None,
        "task_count": len(tasks),
        "oversized_doc": IMPORTER_OVERSIZED_DOC,
        "tasks_from_oversized": len(from_oversized),
        "oversized_split": (
            len(from_oversized) > 1 if tasks else None
        ),
        # S2: what produced the plan
        "tool_used": result.propose_calls > 0,
        "propose_calls": result.propose_calls,
        "accepted_tasks": result.accepted_count,
        "repair": repair,
        "precedence_note": result.precedence_note or None,
    }


def _render_importer_report(report: dict) -> str:
    """The owner-facing tick evidence: one block, the three facts."""
    lines = [
        f"Importer eval — fixture: {report['fixture']} "
        f"(oversized doc: {report['oversized_doc']})",
        "",
        f"- schema_valid: {report['schema_valid']}",
        f"- task_count: {report['task_count']}",
    ]
    if report["schema_valid"]:
        split = ("split into "
                 f"{report['tasks_from_oversized']} tasks"
                 if report["oversized_split"] else "NOT split")
        lines.append(f"- oversized work order: {split}")
    else:
        lines.append("- oversized work order: n/a (the import was refused)")
        lines.append(f"- refusal: {report['refusal']}")
    produced = (
        f"propose_task: {'yes' if report['tool_used'] else 'no'} "
        f"({report['propose_calls']} call(s), "
        f"{report['accepted_tasks']} accepted)"
    )
    if report["repair"]:
        turns = report["repair"].get("turns")
        turns_text = (
            f"{turns} turns" if isinstance(turns, int)
            else "an unknown number of turns"
        )
        produced += (
            f"; repaired (outcome {report['repair'].get('outcome')}, "
            f"{turns_text})"
        )
    lines.append(f"- produced by: {produced}")
    if report["precedence_note"]:
        lines.append(f"- precedence: {report['precedence_note']}")
    lines.append("")
    return "\n".join(lines)


def run_importer_eval(
    factory_root: Path | None = None,
    out_path: Path | None = None,
    *,
    scripted: bool = False,
) -> dict:
    """ONE real import against the prose fixture (F-57 AC-4) — the
    owner's on-demand measurement, never routine CI.

    ``factory eval --importer`` is the only caller on the real path, and
    the real path refuses to run without the explicit live opt-in —
    exactly ``FACTORY_LIVE_TESTS=1``, the f06 convention: the value
    semantics are the contract, so a credential alone (or an org runner
    exporting it) can never turn a paid session on. ``scripted=True`` is
    the offline test seam: the caller has patched the session runner, so
    nothing can spend — the gate stands down for that shape only.

    The report names the three facts AC-4 asks for: whether the proposal
    was schema-valid (judged by the real pipeline — the import, its
    refusal, and the ONE repair all included), how many tasks it
    proposed, and whether the oversized work order came out split (the
    prompt's rule: a task too big for size M is split, never sized above
    the enum). Nothing is written to any queue — the import proposes
    only.

    Returns:
        ``{"report", "report_path", "rendered_path"}``.
    """
    import os

    from factory.importer import ImportRefused, run_import
    from factory.runs import FACTORY_ROOT_DEFAULT

    if not scripted and os.environ.get("FACTORY_LIVE_TESTS") != "1":
        raise EvalError(
            "the importer eval drives ONE real model session: opt in "
            "with FACTORY_LIVE_TESTS=1 (a credential alone never turns "
            "it on)"
        )
    root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    eval_root = root / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    # S3+S6 (review rounds): ONE run id for the whole invocation — the
    # fixture dir AND the report file — so two evals, however close in
    # time, never touch each other's anything (run ids are unique per
    # mint).
    from factory.runs import new_run_id

    run_stamp = new_run_id()
    fixture_dir = eval_root / f"importer-fixture-{run_stamp}"
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture = _build_importer_fixture(fixture_dir)
    try:
        try:
            result = run_import(fixture, factory_root=root)
            report = _importer_eval_report(result=result)
        except ImportRefused as exc:
            report = _importer_eval_refusal(str(exc))
        out = Path(out_path) if out_path else (
            eval_root / f"importer-eval-{run_stamp}.json"
        )
        out.write_text(json.dumps(report, indent=2) + "\n")
        rendered_path = out.with_suffix(".md")
        rendered_path.write_text(_render_importer_report(report))
        return {"report": report, "report_path": out,
                "rendered_path": rendered_path}
    finally:
        shutil.rmtree(fixture_dir, ignore_errors=True)
