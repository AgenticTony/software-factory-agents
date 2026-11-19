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
and pytest coverage. Six seeded bugs are the classes VALIDATOR-SPEC
names: a phantom table name, a removed ownership (RLS) filter, an
off-by-one, a deleted ``await``, and a test that mocks the thing it
claims to test — plus a contract test that can't fail (the class L1
exists for). F-95 adds shapes of misses the factory actually had, each
on its own base (a setup commit off the shared fixture): a sibling
seed (one site of a defect class fixed, the second untouched — caught
only by a PASSING L2 that reports the second site as a sibling), a
caller-break seed (caught only on a failing round with a finding on
the unchanged caller's file), and a clean diff beside an old site of
the class. F-99: every seed and clean diff also carries a
``contract_title`` — the developer's task, stated as intent, never
naming the defect — and that, not the eval's own description of the
seeded bug, is the only title any seat is prompted with; the specs'
ids name their bug classes, so the contract ids, branches and commit
messages run on opaque ``slot`` handles instead. Every number measures
the reviewers, never the eval's own text.

Layers:

* **L0** (:func:`factory.l0.run_l0`) — commands, diff policy, sentinels.
* **L1** (:func:`factory.l1.run_l1`) — the red-first proof of the seed's
  contract tests.
* **L2** — F-38's parallel reviewer fan-out and F-96's per-finding
  verifier fan-out over the seeded diff
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

from factory.approvals import ensure_fixture_profile_approval
from factory.contract import apply_profile_defaults, load_profile
from factory.gates import git as hardened_git
from factory.l0 import run_l0
from factory.l1 import run_l1
from factory.l2 import (
    L2Finding,
    _fails_round,
    merge_reviews,
    parse_reviews_with_retry,
    settle_review_verdict,
)
from factory.session import complete_records, run_session_sandboxed, run_sessions_parallel
from factory.siblings import normalise_path

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

    ``contract_title`` (F-99) is the task a developer would plausibly
    have been given that produced this diff, stated as intent — never
    naming the defect. ``title`` is the eval's own description of the
    seeded bug: a REPORT label only (the per-diff lines), never a prompt
    — a contract that told the reviewers the answer measured their
    reading, not their reviewing (F-99).

    ``slot`` (F-99 round 2) is the spec's opaque handle — a position
    number (``EVAL-01`` …), carrying no part of the id. The id names
    the bug class ("mocks-what-it-tests"), so it must never reach a
    seat: the task's contract id, the ``eval/`` branch, the
    ``eval-base/`` setup ref and the setup/seed commit messages all use
    the slot (L1's red_reason echoes the branch name into the review
    prompt); ``id`` stays on the record and in the report only.
    """

    id: str
    slot: str
    layer: str
    title: str
    contract_title: str
    markers: tuple[str, ...] = ()
    edits: tuple[tuple[str, str], ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    contract: dict = field(default_factory=dict)
    # F-95: files committed on the seed's OWN base (``eval-base/<id>``,
    # one setup commit off the shared fixture) before the seed's diff —
    # the shared fixture and every other seed's base never see them.
    setup_files: dict[str, str] = field(default_factory=dict)
    # F-95: when set, an L2 finding is catch evidence only if its file is
    # one of these (the caller-break seed: the catch is the CALLER's
    # file, never the changed one). Empty keeps the F-16 rule.
    finding_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiblingSeed(SeedSpec):
    """F-95: a seed whose catch is a REPORTED SIBLING, not a failed round.

    The setup commit plants one defect class at two sites; the seed's
    diff fixes one. L2 catches it only when ALL hold: its verdict is
    ``pass`` (a sibling never blocks — a failed round is no sibling
    catch, whatever it carried), a surviving finding is marked
    ``sibling`` (the parent's two-condition ruling, F-94), that
    finding's file is ``sibling_file`` (the untouched site), and its
    text names one of ``markers``. L0 and L1 never catch this kind."""

    sibling_file: str = ""


@dataclass(frozen=True)
class CleanSpec:
    """A known-clean diff: the validator must pass it and L2 must draw
    no blocking finding on it (AC-3's false-positive dial).

    ``contract_title`` (F-99): as :class:`SeedSpec` — the developer's
    task as intent, the only title any seat sees. ``title`` is the
    eval's report label. ``slot`` (F-99 round 2): as :class:`SeedSpec`
    — the opaque handle for the contract id, branch and commits."""

    id: str
    slot: str
    title: str
    contract_title: str
    edits: tuple[tuple[str, str], ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    # F-95: as SeedSpec.setup_files — old code the clean diff sits next to.
    setup_files: dict[str, str] = field(default_factory=dict)


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
        # F-100: the fixture runs the standard stack and admits both of
        # its provider kinds (zai workhorse, anthropic hunt/final) — an
        # eval --role override naming either kind stays inside AC-3's
        # guard, exactly as a real project's would.
        "model_stack": "standard",
        "providers_allowed": ["zai", "anthropic"],
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
                    "description": "schema-refs ratchet",
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

# F-95 setup code (committed on a seed's own base, never on the shared
# fixture). OLD_SITES: one defect class at two sites in two files — an
# owner check written ``not owner_id``, which a whitespace-only id
# passes (the JobFinderOS run-4 shape: a blank-but-not-empty value
# accepted by two reads).
OLD_SITES = {
    "app/owners.py": (
        '"""Owner-id guards for the trip reads."""\n'
        "\n"
        "\n"
        "def require_owner(owner_id):\n"
        '    """Refuse a read with no owner: an empty id would match every\n'
        '    unowned row."""\n'
        "    if not owner_id:\n"
        '        raise ValueError("owner_id is required")\n'
        "    return owner_id\n"
    ),
    "app/gear.py": (
        '"""Gear reads, scoped to the owner of the trip."""\n'
        "\n"
        "from app import store\n"
        "\n"
        "\n"
        "def gear_for_owner(owner_id):\n"
        '    """Every gear item on the owner\'s own trips."""\n'
        "    if not owner_id:\n"
        '        raise ValueError("owner_id is required")\n'
        '    trips = {t["id"] for t in store.query("trips")\n'
        '             if t["owner_id"] == owner_id}\n'
        '    return [g for g in store.query("gear") if g["trip_id"] in trips]\n'
    ),
}

# F-95: an untested caller of pack_list in another file — the
# caller-break seed changes pack_list's return shape and leaves this
# file untouched (and broken).
REPORT_SOURCE = (
    '"""The trip report: a one-line pack summary."""\n'
    "\n"
    "from app.api import pack_list\n"
    "\n"
    "\n"
    "def pack_summary(trip_id):\n"
    '    """The first gear items on a trip, comma-separated."""\n'
    '    return ", ".join(i["item"] for i in pack_list(trip_id))\n'
)

SOLOADVENTURER_SEEDS = [
    SeedSpec(
        id="phantom-table",
        slot="EVAL-01",
        layer="L0",
        title="a write path retargeted to a table the schema doesn't know",
        # The developer's task (F-99): migrate the write to the new
        # table — never the defect (the schema doesn't know it). The
        # table name rides the diff itself.
        contract_title="archive trips into the new archived_trips table",
        markers=("archived_trips",),
        edits=(
            ('store.insert("trip_archive", {"trip_id": trip_id})',
             'store.insert("archived_trips", {"trip_id": trip_id})'),
        ),
    ),
    SeedSpec(
        id="removed-rls-filter",
        slot="EVAL-02",
        layer="L0",
        title="the ownership filter dropped from an owner-scoped read",
        # Round 2: "returns all trips" REQUESTED the security hole —
        # a reviewer following that contract would pass it. Neutral
        # intent the diff plausibly came from; the unscoped read is
        # the defect they must find.
        contract_title="simplify trips_for",
        markers=("owner", "trips_for", "rows"),
        edits=(
            ('    return [row for row in rows if row["owner_id"] == owner_id]',
             "    return list(rows)"),
        ),
    ),
    SeedSpec(
        id="off-by-one",
        slot="EVAL-03",
        layer="L0",
        title="the pack-list cap reads one item past the limit",
        # Round 2: "include one item past the limit" REQUESTED the
        # off-by-one. Neutral intent; the wrong bound is the defect
        # they must find.
        contract_title="simplify pack_list's slicing",
        markers=("pack_list", "limit"),
        edits=(
            ("    return items[:limit]", "    return items[: limit + 1]"),
        ),
    ),
    SeedSpec(
        id="deleted-await",
        slot="EVAL-04",
        layer="L0",
        title="the await dropped from the checkin-count fetch",
        # F-102 (AC-3): round 1's "make the checkin-count fetch
        # synchronous" REQUESTED the change that carries the defect.
        # Neutral intent — the tidy-up a developer would plausibly have
        # been booked for; the dangling coroutine is the defect they
        # must find ("coroutine" stays a marker no seat may be handed).
        contract_title="tidy the checkin-count fetch",
        markers=("checkin_count", "fetchone", "await", "coroutine"),
        edits=(
            ("    row = await store.fetchone(", "    row = store.fetchone("),
        ),
    ),
    SeedSpec(
        id="mocks-what-it-tests",
        slot="EVAL-05",
        layer="L2",
        title="a test that mocks the function it claims to test",
        # The task: test isolation — not "the test mocks the function
        # it claims to test", which IS the defect.
        contract_title="isolate the checkin-count test from the store",
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
        slot="EVAL-06",
        layer="L1",
        title="a contract test that already passes on base — it proves nothing",
        # The task: add the contract test — not that it can never fail.
        # The acceptance text below follows the same rule (intent).
        contract_title="add a contract test for trip archiving",
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
    # F-95: shaped like misses the factory actually had. Each carries its
    # own setup commit, so the F-16 records above never move.
    SiblingSeed(
        id="sibling-second-site",
        slot="EVAL-07",
        layer="L2",
        title="one site of a defect class fixed; the same defect at a "
              "second site, untouched (JobFinderOS run 4)",
        # The task names the ONE function the diff touches and the
        # hardening wanted — never the whitespace-only gap, never the
        # untouched second site (the leaked label read as an unmet
        # contract and the reviewers blocked on the site they had
        # correctly found).
        contract_title="harden require_owner against blank owner ids",
        markers=("gear_for_owner", "whitespace", "strip"),
        setup_files=OLD_SITES,
        edits=(
            ("    if not owner_id:\n"
             '        raise ValueError("owner_id is required")\n'
             "    return owner_id\n",
             "    if not owner_id or not owner_id.strip():\n"
             '        raise ValueError("owner_id is required")\n'
             "    return owner_id\n"),
        ),
        sibling_file="app/gear.py",
    ),
    SeedSpec(
        id="caller-break",
        slot="EVAL-08",
        layer="L2",
        title="pack_list returns plain names now; an unchanged caller "
              "in another file still reads dicts (#163 round 1)",
        # The task names the API change only — the broken caller is the
        # defect the reviewers must find (never "pack_summary").
        contract_title="pack_list returns item names instead of item dicts",
        markers=("pack_summary",),
        setup_files={"app/report.py": REPORT_SOURCE},
        edits=(
            ("    return items[:limit]",
             '    return [i["item"] for i in items][:limit]'),
            ('    items = [g["item"] for g in pack_list("t1", limit=2)]',
             '    items = pack_list("t1", limit=2)'),
        ),
        finding_files=("app/report.py",),
    ),
]

CLEAN_DIFFS = [
    CleanSpec(
        id="clean-docstring",
        slot="EVAL-09",
        title="a clearer docstring on an owner-scoped read",
        contract_title="clarify the docstring on the owner-scoped trips read",
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
        slot="EVAL-10",
        title="a new pure helper with its own coverage",
        contract_title="add a logbook line helper with its own test",
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
    # F-95: a clean diff next to old code of the sibling seed's class (the
    # unfixed whitespace-only owner check). A sibling finding on the old
    # site is allowed; it is never a blocking finding or a false fail.
    # F-99: the docstring states what archive_trip does — the call its
    # body makes (store.insert into trip_archive) — never a state change
    # the fixture disproves (the old "its row moves to trip_archive"
    # claimed a move store.insert never performs: it returns a dict and
    # mutates nothing, and both of L2's clean-diff false fails came from
    # reviewers rightly flagging that claim).
    CleanSpec(
        id="clean-beside-old-site",
        slot="EVAL-11",
        title="a clearer docstring, next to an old whitespace-only "
              "owner check",
        # The task names the one function whose docstring is being
        # clarified — not the old site beside it.
        contract_title="clarify archive_trip's docstring",
        setup_files=OLD_SITES,
        edits=(
            ('    """Archive a completed trip."""\n',
             '    """Archive a completed trip by inserting into '
             'trip_archive."""\n'),
        ),
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
    config=None,
) -> dict:
    """Run the validator's layers over one seeded (or clean) diff.

    Creates branch ``eval/<slot>`` from ``base_ref`` in the fixture repo
    (F-95: from the seed's own ``eval-base/<slot>`` — ``base_ref`` plus
    one setup commit — when the seed carries ``setup_files``; every
    layer then judges the diff against that base), applies the seed,
    and runs L0 and L1 — plus L2 as F-38's parallel
    reviewer and verifier when ``session_runner`` is given (the real
    runner drives real model sessions; tests script it). The repo is
    left on the seed's branch. F-99 round 2: every ref and commit
    message is slot-named — the id names the bug class and L1's
    red_reason echoes the branch into the review prompt, so the git
    surface must be as opaque as the contract.

    Returns the per-diff record: each layer's report dict and the layers
    that caught the seed (a failing layer whose own recorded evidence
    names one of the seed's ``markers``; for a :class:`SiblingSeed`, a
    PASSING L2 by :func:`_sibling_check`, whose verdict and reason ride
    on ``sibling_check``); a clean diff carries the number of blocking
    findings its review drew — ``None`` when L2 never ran. A seed with
    setup code also carries its ``base``.
    """
    repo = Path(repo)
    profile = eval_profile(repo)
    task = _seed_task(seed)
    _git(repo, "checkout", "--force", base_ref)
    # F-95: a seed with setup code gets its OWN base — one setup commit
    # on ``eval-base/<slot>``, rebuilt from ``base_ref`` on every run
    # (``-B`` resets it, so repeats never stack) — and every layer judges
    # the seed's diff against that base. ``base_ref`` itself never moves,
    # so no other seed or clean diff can see the setup.
    seed_base = base_ref
    if seed.setup_files:
        seed_base = f"eval-base/{seed.slot}"
        _git(repo, "checkout", "-B", seed_base, base_ref)
        _write_files(repo, seed.setup_files)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"setup: {seed.slot}")
    _git(repo, "checkout", "-B", f"eval/{seed.slot}", seed_base)
    _apply_diff(repo, seed)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"seed: {seed.slot}")

    l0 = run_l0(profile, repo, seed_base, f"eval/{seed.slot}", task=task,
                runner=command_runner)
    l1 = run_l1(profile, repo, seed_base, f"eval/{seed.slot}", task=task,
                runner=command_runner)
    layers = {"L0": l0.to_dict(), "L1": l1.to_dict()}
    if session_runner is not None:
        void_reason: list[str] = []
        report, retries = run_l2_review(
            repo, profile, seed_base, f"eval/{seed.slot}", task,
            session_runner=session_runner, reviewers=reviewers,
            l0=l0, l1=l1, factory_root=factory_root,
            config=config, void_reason=void_reason,
        )
        if report is None:
            # Voided (PR #90 round 4): never a verdict — excluded from
            # the catch and false-fail denominators, counted on the
            # report's own voided line instead. F-96 round 6: the
            # reason is NAMED (verifier quota, or a verifier window
            # that starved before every finding was voted).
            layers["L2"] = {"quota_voided": True,
                            "void_reason": void_reason[0]
                            if void_reason else "verifier_quota",
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
    if seed.setup_files:
        # Only setup seeds carry it: the F-16 records stay byte-for-byte.
        record["base"] = seed_base
    if isinstance(seed, SiblingSeed):
        check = _sibling_check(layers, seed, root=repo)
        record["caught_by"] = ["L2"] if check["caught"] else []
        record["sibling_check"] = check
    elif isinstance(seed, SeedSpec):
        record["caught_by"] = [
            name for name, report in layers.items()
            if _catches(report, seed.markers,
                        files=seed.finding_files if name == "L2" else (),
                        root=repo)
        ]
        if seed.finding_files:
            # F-95 R2-1: a caller-break miss says why (only seeds with a
            # catch file carry it: the F-16 records stay byte-for-byte).
            record["caller_check"] = _caller_check(
                layers, seed, "L2" in record["caught_by"], root=repo)
    else:
        l2 = layers.get("L2") or {}
        record["blocking_findings"] = (
            None if l2.get("quota_voided")
            else (_count_blocking(l2) if "L2" in layers else None)
        )
    return record


def _catches(report: dict, markers: tuple[str, ...],
             files: tuple[str, ...] = (), root: Path | None = None) -> bool:
    """True when a layer's own recorded evidence names the seeded defect.

    The verdict alone is not the catch (a layer that failed over
    findings unrelated to the seed caught nothing — exactly the false
    positive the known-clean diffs exist to dial). The evidence is what
    the layer itself recorded: the output of the commands and sentinels
    that failed, L1's red-reason table, and — for L2 — the surviving
    findings (a finding the verifier dropped never survived, so it
    never counts). ``files`` (F-95), when given, admits only findings
    filed on one of those paths that would THEMSELVES fail the round
    (:func:`_blocks`, l2's own rule: not ruled sibling, a round-failing
    severity): the caller-break catch is a blocking finding on the
    caller — never one on the changed file that merely names it, and
    never a non-blocking one (a sibling, a minor) in a round something
    else failed (review round 3).
    ``root`` is the fixture clone the reviewers read (an absolute path
    inside it counts, as it does for the parent's sibling ruling).
    """
    if report.get("verdict") in (None, "pass") or not markers:
        return False
    evidence = "\n".join(
        part for part in _catch_evidence(report, files, root) if part
    ).lower()
    return any(marker.lower() in evidence for marker in markers)


def _blocks(finding: dict) -> bool:
    """Whether a STORED L2 finding would itself fail a round — l2's own
    :func:`factory.l2._fails_round` (a round-failing severity, not ruled
    sibling) applied to the stored dict, so the eval never re-types the
    severity list."""
    return _fails_round(L2Finding(
        id=str(finding.get("id")), text=str(finding.get("text", "")),
        severity=finding.get("severity"),
        sibling=finding.get("sibling") is True,
    ))


def _on_files(finding: dict, files: tuple[str, ...],
              root: Path | None = None) -> bool:
    """Whether a stored finding is filed on one of ``files`` (its path
    normalised the F-94 way, :func:`factory.siblings.normalise_path`:
    ``./x`` and ``a/../x`` count, an absolute path only inside ``root``,
    a climbing path never)."""
    return normalise_path(finding.get("file"), root) in files


def _names_marker(finding: dict, markers: tuple[str, ...]) -> bool:
    text = f"{finding.get('file') or ''} {finding.get('text', '')}".lower()
    return any(marker.lower() in text for marker in markers)


def _sibling_check(layers: dict, seed: "SiblingSeed",
                   root: Path | None = None) -> dict:
    """F-95: the sibling kind's catch rule, with the reason on the record.

    Caught only when the L2 verdict is ``pass`` AND a SURVIVING finding
    is marked ``sibling`` (the parent's ruling, never the reviewer's
    claim alone), is filed on ``seed.sibling_file`` and names a marker.
    Every other shape scores zero, and ``reason`` says which rule it
    failed — the first that applies, in this order: L2 never ran or was
    quota-voided; the round FAILED (a sibling never blocks, so a blocked
    round is no sibling catch, whatever it carried); then what the
    surviving findings lacked; and when nothing survived but the
    verifier dropped a matching finding, that it was found, then
    dropped."""
    l2 = layers.get("L2")
    if not l2:
        return {"caught": False, "reason": "L2 did not run"}
    if l2.get("quota_voided"):
        return {"caught": False, "reason": "L2 quota-voided (no verdict)"}
    if l2.get("verdict") != "pass":
        return {"caught": False,
                "reason": f"the round failed (verdict {l2.get('verdict')}): "
                          "a sibling must never block, so a blocked round "
                          "is no sibling catch"}
    findings = [f for f in l2.get("findings") or [] if isinstance(f, dict)]
    site = (seed.sibling_file,)
    on_site = [f for f in findings if _on_files(f, site, root)]
    if any(f.get("sibling") is True and _names_marker(f, seed.markers)
           for f in on_site):
        return {"caught": True,
                "reason": f"reported {seed.sibling_file} as a sibling; "
                          "the round passed"}
    if any(f.get("sibling") is True for f in on_site):
        return {"caught": False,
                "reason": f"the sibling on {seed.sibling_file} names no "
                          f"marker ({', '.join(seed.markers)})"}
    if any(_names_marker(f, seed.markers) for f in on_site):
        return {"caught": False,
                "reason": f"{seed.sibling_file} was reported but not "
                          "marked sibling"}
    if any(f.get("sibling") is True for f in findings):
        return {"caught": False,
                "reason": f"a sibling was reported on another file, not "
                          f"{seed.sibling_file}"}
    if _dropped_matches(l2, seed, root):
        return {"caught": False,
                "reason": "found, then dropped by the verifier: no "
                          f"surviving finding on {seed.sibling_file}"}
    return {"caught": False,
            "reason": f"no surviving finding on {seed.sibling_file}"}


def _caller_check(layers: dict, seed: SeedSpec, caught: bool,
                  root: Path | None = None) -> dict:
    """F-95 R2-1: why a caller-break run was (not) caught — the live eval
    must tell "the reviewers never saw the break" from "a seat found it
    but marked it sibling" (F-94's named residual: flag + a file outside
    the diff makes it non-blocking) and from "found, then dropped".

    Reasons, the first that applies: L2 not run or quota-voided; caught;
    a surviving finding on the caller's file naming a marker that the
    parent ruled sibling (``residual: True`` — rendered in the per-seed
    block); one naming a marker that did not block for another reason
    (its severity); a caller finding that names no marker; a matching
    finding the verifier voted out; no finding on the caller."""
    l2 = layers.get("L2")
    if not l2:
        return {"caught": False, "reason": "L2 did not run"}
    if l2.get("quota_voided"):
        return {"caught": False, "reason": "L2 quota-voided (no verdict)"}
    if caught:
        return {"caught": True,
                "reason": "the round failed on a finding on the caller"}
    files = tuple(seed.finding_files)
    on_caller = [f for f in l2.get("findings") or []
                 if isinstance(f, dict) and _on_files(f, files, root)]
    named = [f for f in on_caller if _names_marker(f, seed.markers)]
    if any(f.get("sibling") is True for f in named):
        return {"caught": False, "residual": True,
                "reason": "reported on the caller but ruled sibling "
                          "(non-blocking): the F-94 residual"}
    if named:
        return {"caught": False,
                "reason": "reported on the caller but the round did not "
                          f"fail (verdict {l2.get('verdict')}, severity "
                          f"{named[0].get('severity')})"}
    if on_caller:
        return {"caught": False,
                "reason": "a finding on the caller names no marker "
                          f"({', '.join(seed.markers)})"}
    if _dropped_matches(l2, seed, root):
        return {"caught": False,
                "reason": "found, then dropped by the verifier"}
    return {"caught": False,
            "reason": f"no surviving finding on {', '.join(files)}"}


def _dropped_matches(l2: dict, seed: SeedSpec,
                     root: Path | None = None) -> list[dict]:
    """The verifier's VOTE drops of a finding that names the seeded
    defect (a marker, and — when the seed has catch files — one of
    those files). A refused drop (``override_refused``: the finding
    survived) and a nit budget cut (not the verifier's judgment) are
    not counted. F-102: each drop carries ``would_have_caught`` — the
    seed's own catch rule re-applied to the run with that ONE finding
    restored (:func:`_would_have_caught`) — so a drop that could never
    have produced the catch never reads as a lost one. A drop whose
    finding names no file on a seed with catch files rides the entry
    counted-undecidable (it cannot be judged; excluding it would
    flatter the verifier)."""
    files = ((seed.sibling_file,) if isinstance(seed, SiblingSeed)
             else tuple(seed.finding_files))
    out = []
    for entry in l2.get("verifier_dropped") or []:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason") or "")
        if reason == "nit_budget" or reason.startswith("override_refused"):
            continue
        finding = entry.get("finding")
        if not isinstance(finding, dict) or not _names_marker(finding,
                                                              seed.markers):
            continue
        if files and finding.get("file") is not None \
                and not _on_files(finding, files, root):
            continue
        caught, undecidable = _would_have_caught(seed, finding, l2, root)
        row = {"id": finding.get("id"), "file": finding.get("file"),
               "text": finding.get("text"), "reason": reason,
               "would_have_caught": caught}
        if undecidable:
            row["undecidable"] = undecidable
        out.append(row)
    return out


def _restored_l2(l2: dict, finding: dict) -> dict:
    """The stored L2 report with ONE dropped finding put back among the
    survivors (F-102): the counterfactual the drop-cost rule judges.
    The round's verdict is re-settled the stored findings settle it —
    the restored finding itself fails the round exactly when
    :func:`_blocks` says (l2's own rule), so a restored minor flips
    nothing and a restored blocking finding fails the round."""
    findings = [f for f in l2.get("findings") or [] if isinstance(f, dict)]
    restored: dict = dict(l2, findings=[*findings, finding])
    if _blocks(finding):
        restored["verdict"] = "fail"
    return restored


def _would_have_caught(seed: SeedSpec, finding: dict, l2: dict,
                       root: Path | None = None) -> "tuple[bool, str | None]":
    """F-102: whether keeping this one dropped finding would have
    caught the seed — the seed's OWN catch rule re-applied to the run
    with the finding restored (:func:`_restored_l2`): :func:`_blocks`
    for caller-break, the sibling rule (:func:`_sibling_check`) for the
    sibling seed, F-16's marker rule (:func:`_catches`, which needs a
    failing verdict) for the rest. Returns ``(caught, reason)``, the
    reason set only when the restore could not be judged — and an
    undecidable restore COUNTS as caught: the costlier error is a
    drop-cost number that flatters the review pipeline (a minor drop
    read as harmless because its severity was missing)."""
    files = ((seed.sibling_file,) if isinstance(seed, SiblingSeed)
             else tuple(seed.finding_files))
    if files and finding.get("file") is None:
        return True, "the dropped finding names no file"
    verdict = l2.get("verdict")
    if verdict not in ("pass", "fail"):
        return True, f"no verdict on the record ({verdict!r})"
    if (files and finding.get("sibling") is not True
            and finding.get("sibling_claim") is True
            and _on_files(finding, files, root)):
        # The drop is stored PRE-ruling: settle marks the union, and a
        # dropped finding never survives to be marked (its ``sibling``
        # reads false beside its ``sibling_claim``). The restore
        # re-applies the parent's two-condition rule where the seed's
        # own rule can decide it — the claim, on a catch file the
        # seed's diff never touches (outside it by construction). A
        # plain seed's drop is unfiltered by file, so its diff
        # membership is unknowable from the record: the claim is left
        # undecided there and a blocking restore COUNTS (the costly
        # side, never the flattering one).
        finding = dict(finding, sibling=True)
    if isinstance(seed, SiblingSeed):
        check = _sibling_check({"L2": _restored_l2(l2, finding)}, seed,
                               root=root)
        return check["caught"], None
    # The marker rule needs a FAILING round; a passing one flips only
    # when the restored finding itself fails it — which a missing or
    # blank severity cannot decide (and with catch files, the rule
    # admits only findings that block: undecidable the same way).
    severity = finding.get("severity")
    if (files or verdict == "pass") and (
            not isinstance(severity, str) or not severity.strip()):
        return True, "the restored finding carries no severity"
    return _catches(_restored_l2(l2, finding), seed.markers,
                    files=files, root=root), None


def found_then_dropped(record: dict, seed: SeedSpec,
                       root: Path | None = None) -> list[dict]:
    """F-95 (F-96's baseline): on one seed run, the findings a seat
    reported that name the seeded defect and that the verifier then
    voted out — today these read as a plain miss. F-99: the report's
    entries carry ``seed_caught`` beside these (whether that run was
    still caught by any layer) so a harmless drop never reads as a lost
    catch. F-102: each drop carries ``would_have_caught`` (with
    ``undecidable`` named when the restore could not be judged).

    "Names the seeded defect" is the seed's own catch evidence rule: a
    marker in the finding's file or text, on the seed's catch file when
    it has one (the sibling seed's untouched site, the caller-break
    seed's caller). Read only from the verifier's vote drops on the
    stored L2 report — never from the surviving findings. Empty when L2
    did not run or was quota-voided. ``root``: the fixture clone, so an
    absolute path inside it counts."""
    l2 = (record.get("layers") or {}).get("L2") or {}
    if not l2 or l2.get("quota_voided"):
        return []
    return _dropped_matches(l2, seed, root)


def _catch_evidence(report: dict, files: tuple[str, ...] = (),
                    root: Path | None = None) -> list[str]:
    """The human-readable output a layer's failure is attributed to
    (findings restricted to ``files`` when given)."""
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
        if isinstance(finding, dict) and (
                not files
                or (_on_files(finding, files, root) and _blocks(finding))):
            parts.append(f"{finding.get('file') or ''} "
                         f"{finding.get('text', '')}")
    return parts


def _seed_task(seed: SeedSpec | CleanSpec) -> dict:
    """The task dict the eval's seats are prompted with (F-99: the
    contract's title is the developer's task, ``contract_title`` — never
    ``seed.title``, the eval's own description of the seeded bug, which
    stays a report label on the record; and its id is the opaque
    ``slot`` — never ``EVAL-{seed.id}``, which names the bug class; a
    contract that told the reviewers the answer measured their reading,
    not their reviewing)."""
    contract = seed.contract if isinstance(seed, SeedSpec) else None
    acceptance = [contract] if contract else []
    return {
        "id": seed.slot,
        "title": seed.contract_title,
        "acceptance": acceptance,
        "risk": [],
    }


def _write_files(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _apply_diff(repo: Path, seed: SeedSpec | CleanSpec) -> None:
    _write_files(repo, seed.files)
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
    config=None,
    void_reason: "list[str] | None" = None,
):
    """L2 as F-38's parallel reviewer and verifier over one diff.

    The orchestrator's pipeline: N reviewer sessions over the same diff
    (a fresh context each, run concurrently through
    :func:`factory.session.run_sessions_parallel`), the verdicts merged
    into a union, and — when the union carries findings — the F-96
    per-finding verifier fan-out: one fresh verifier session per finding
    (a ``blocking`` or ``major`` two), run through the SAME
    :func:`factory.orchestrator.run_verifier_fanout` the queue's own
    reviews use, so the prompts and the vote fold cannot drift (F-48
    parity, one layer down). The verdict is settled through the
    SAME shared post-review chain production renders (F-48, M-3:
    :func:`factory.l2.settle_review_verdict`) in the FIRST-ROUND mode a
    single-round review is: empty prior, no delta, ``delta_mode=False``,
    no final review — for a one-round task the per-round review is what
    gates publish (the F-41 final review runs only after a delta round
    passed, which a one-round task never has).

    Quota is checked exactly where production checks it (F-48, m-12):
    BEFORE any parse or re-run on the fan-out's own records, and a
    quota-hit verifier session stops the fan-out before its vote is
    parsed. A seat whose first run
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

    Args:
        void_reason: F-96 round 6 — an out-list; when the review is
            voided (quota, or a verifier pool whose window ran out
            before every finding was voted), the reason string is
            appended so the caller's record can NAME it instead of
            scoring or silently passing an unverified verdict.

    Returns:
        ``(report, retries)`` — the L2 report (``None`` when the review
        was voided: quota, or a starved verifier window), and how many
        reviewer re-runs the retry policy spent on seats whose first
        verdict failed to parse.
    """
    from factory import orchestrator
    from factory.models import reviewer_seat_role
    from factory.prompts import assemble_verifier_prompt, reviewer_seat_prompts

    diff = _git(repo, "diff", "--no-color", "--no-ext-diff",
                f"{base_ref}...{head_ref}")
    l0_dict = l0.to_dict() if l0 is not None else {}
    l1_dict = l1.to_dict() if l1 is not None else {}
    review_prompt = orchestrator._review_prompt(task, diff, l0_dict, l1_dict)
    retry_records: list = []
    # F-94 (F-48 parity): the run's seat lenses, seat for seat — and a
    # re-run of seat N keeps seat N's lens, exactly as production's.
    seat_systems = reviewer_seat_prompts(profile, reviewers)

    def _rerun_review_seat(seat_no=None, session=None, parse_error=None):
        """One retry of one reviewer seat: the same prompt through the
        same fan-out machinery — parse_reviews_with_retry drives it (it
        passes the seat's 1-based position — which picks the seat's own
        lens, F-94 — its original session and the parse failure, which
        the eval does not need: it has no attempt budget to fit and no
        trace to name the seat) and decides whether the retry's verdict
        is used."""
        record = complete_records(run_sessions_parallel(
            [("reviewer", review_prompt)],
            repo, profile,
            runner=session_runner,
            factory_root=factory_root,
            config=config,
            max_turns=SEAT_MAX_TURNS,
            timeout_min=SEAT_TIMEOUT_MIN,
            protected=list((profile.get("paths") or {}).get("protected") or []),
            system_prompt=seat_systems[(seat_no or 1) - 1],
            seat_roles=[reviewer_seat_role(seat_no or 1, reviewers)],
        ))[0]
        retry_records.append(record)
        return record

    reviews = complete_records(run_sessions_parallel(
        [("reviewer", review_prompt)] * reviewers,
        repo, profile,
        runner=session_runner,
        factory_root=factory_root,
        config=config,
        max_turns=SEAT_MAX_TURNS,
        timeout_min=SEAT_TIMEOUT_MIN,
        protected=list((profile.get("paths") or {}).get("protected") or []),
        system_prompt=seat_systems[0],
        seat_system_prompts=seat_systems,
        seat_roles=[reviewer_seat_role(n, reviewers)
                    for n in range(1, reviewers + 1)],
    ))
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
        # F-96 (F-48 parity): the SAME per-finding verifier fan-out the
        # run uses — one session per finding (a serious one two), the
        # same prompts, the same vote fold — through the ONE shared
        # implementation. A quota hit anywhere voids the review exactly
        # as before (m-12): never parsed into a verdict.
        # F-96 round 6: the pool's window is the per-seat budget TIMES
        # the session count — every session is owed its own full
        # SEAT_TIMEOUT_MIN whatever order the sliding pool admits it
        # in, so a normal pool neither starves nor hands a late session
        # a shrunken window (round 5's flat window did both).
        # F-102 (AC-5): the WINDOW stays the admission check, but each
        # session's own timeout is CAPPED at SEAT_TIMEOUT_MIN — one
        # hung eval session can no longer run the whole window (the
        # turn cap still bounds spend; this bounds wall clock).
        session_count = len(
            orchestrator.verifier_sessions_for(union.findings))
        fanout = orchestrator.run_verifier_fanout(
            task, diff, union.findings,
            runner=session_runner, run_dir=repo, profile=profile,
            factory_root=factory_root, config=config,
            protected=list((profile.get("paths") or {}).get("protected") or []),
            system_prompt=assemble_verifier_prompt(profile),
            srt_bin_path=None, run_id=None, task_id=None, toolkit=None,
            parallel=orchestrator._verifier_parallel(profile),
            remaining_min=lambda: SEAT_TIMEOUT_MIN * max(1, session_count),
            timeout_cap_min=SEAT_TIMEOUT_MIN,
            max_turns=SEAT_MAX_TURNS,
        )
        if fanout.quota is not None:
            if void_reason is not None:
                void_reason.append("verifier_quota")
            return None, retries
        if fanout.starved:
            # Round 6 A: a pool whose window ran out before every
            # finding was voted voids the review EXACTLY like quota —
            # never a verdict scored over unverified findings.
            if void_reason is not None:
                void_reason.append("verifier_window_starved")
            return None, retries
        vote = fanout.vote
    # M-3: the ONE shared chain, first-round mode (empty prior, no
    # delta) — the verdict the queue would render for this review.
    return settle_review_verdict(
        union, {}, frozenset(), delta_mode=False,
        vote=vote, reviewers=reviewers,
        outside_diff=orchestrator._outside_diff(repo, base_ref, head_ref),
    ), retries


def _count_blocking(l2: dict | None) -> int:
    """Count ``blocking`` findings in a STORED L2 report dict.

    Case-insensitive on purpose (F-44, review M-1): reports stored before
    F-44's parse-time canonicalisation may hold ``"Blocking"`` — this
    reads records on disk, not freshly parsed findings, so it keeps
    tolerating the casing the parser no longer emits. An F-94 sibling
    (outside the diff, never blocking) is not counted: it is not a
    clean diff's false positive."""
    if not l2:
        return 0
    return sum(
        1 for f in l2.get("findings") or []
        if isinstance(f, dict)
        and f.get("sibling") is not True
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
    voided review says so (F-102: with its reason — quota, or a starved
    verifier window): it produced no verdict at all."""
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
        # F-102 (AC-5): the line names the VOID, never its most common
        # cause — a starved verifier window is as voided as a quota
        # hit, and the reason says which.
        reason = f" ({l2['void_reason']})" if l2.get("void_reason") else ""
        parts.append(f"L2 voided (no verdict){reason}")
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


def dropped_entry(record: dict, seed: SeedSpec,
                  root: Path | None = None) -> dict:
    """One found-then-dropped entry exactly as the report carries it:
    the seed's id, its verifier-vote drops (each judged F-102's
    would-have-caught rule) and whether the run was still caught by any
    layer (F-99). ``run_seeded_eval`` stamps the repeat beside it; a
    single-seed caller (tests, a future per-seed rerun) reuses this
    instead of re-deriving the shape."""
    return {"id": seed.id,
            "dropped": found_then_dropped(record, seed, root=root),
            "seed_caught": bool(record.get("caught_by"))}


def run_seeded_eval(
    repo: Path,
    *,
    base_ref: str = "main",
    command_runner=None,
    session_runner=None,
    reviewers: int = DEFAULT_REVIEWERS,
    factory_root: Path | None = None,
    repeats: int = 1,
    config=None,
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
        "config": config,
    }
    seeds: list = []
    clean: list = []
    dropped: list = []
    for repeat in range(1, repeats + 1):
        for seed in SOLOADVENTURER_SEEDS:
            record = run_seed(repo, seed, **common)
            record["repeat"] = repeat
            seeds.append(record)
            # F-95: per seed and repeat (empty when nothing matched) — kept
            # beside the records, which stay byte-for-byte F-16's.
            # F-99 (AC-3): seed_caught says whether that run was still
            # caught by any layer. F-102 (AC-1): each drop inside is
            # judged — restoring it would / would not have caught the
            # seed — so the count line counts real losses only.
            entry = dropped_entry(record, seed, root=repo)
            entry["repeat"] = repeat
            dropped.append(entry)
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
    overrides = dict(getattr(config, "role_overrides", None) or {})
    return {
        "fixture": "soloadventurer",
        "base_ref": base_ref,
        "l2": "sessions" if session_runner is not None else "skipped",
        "repeats": repeats,
        # F-100 AC-5: every --role override this eval ran with, named —
        # a before/after pair of reports is the evidence for a registry
        # change. Never written to any profile or to the registry.
        "role_overrides": overrides,
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
        # F-95: per seed, caught k of N judged repeats (the pass^k and
        # never-caught lines read it), and per seed and repeat the
        # findings a seat reported and the verifier voted out.
        "per_seed": per_seed_counts(seeds),
        "found_then_dropped": dropped,
        # F-102 (AC-2): the slot → seed id table, OUTSIDE the pinned
        # F-16 record shape (one top-level key; the per-diff records
        # never move) — a slot in a session trace resolves without
        # reading this module. Reports written before F-102 carry no
        # key and render without the section.
        "slots": {spec.slot: spec.id
                  for spec in [*SOLOADVENTURER_SEEDS, *CLEAN_DIFFS]},
    }


def per_seed_counts(seed_records: list) -> dict[str, dict]:
    """F-95: per seed id, ``{"caught", "judged", "voided"}`` over its runs.

    A run is caught when any layer caught it (``caught_by``). A run no
    layer caught whose L2 was quota-voided produced no verdict that
    could have caught it: it is ``voided`` and leaves ``judged`` — never
    a miss (PR #90 round 4's rule; the per-diff line already renders it
    "not judged"). A caught run counts as judged even when its L2 was
    voided: another layer's catch stands. Insertion order is the
    records' first-seen order."""
    out: dict[str, dict] = {}
    for record in seed_records:
        counts = out.setdefault(str(record.get("id")),
                                {"caught": 0, "judged": 0, "voided": 0})
        voided = ((record.get("layers") or {}).get("L2") or {}).get(
            "quota_voided")
        if record.get("caught_by"):
            counts["caught"] += 1
            counts["judged"] += 1
        elif voided:
            counts["voided"] += 1
        else:
            counts["judged"] += 1
    return out


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
    renders as that layer's false positive (F-48, m-11); then the
    per-seed block (F-95: caught k/N repeats, pass^k, never caught,
    found-then-dropped — :func:`_per_seed_lines`); then the catch
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
    ]
    # F-100 AC-5: the eval's --role overrides, named — the report is the
    # evidence for a registry change, so it says which roles it pinned.
    overrides = report.get("role_overrides") or {}
    if overrides:
        lines.append("Role overrides (this eval only, never written): "
                     + ", ".join(f"{role}=`{model}`" for role, model
                                 in sorted(overrides.items())))
        lines.append("")
    lines += [
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
    lines += _per_seed_lines(report)
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
    # F-102 (AC-2): the slot → seed id table, only on reports that
    # carry it (a top-level key outside the pinned record shape) —
    # reports written before F-102 render exactly as they did.
    slots = report.get("slots")
    if slots:
        lines += ["", "## Slot → seed", ""]
        lines += [f"- {slot} → {seed_id}" for slot, seed_id in slots.items()]
    lines.append("")
    return "\n".join(lines)


def _per_seed_lines(report: dict) -> list[str]:
    """F-95: the per-seed reliability block — caught k/N repeats per
    seed, the pass^k line (the seeds caught on EVERY judged repeat: the
    reliability number, not "caught once in N"), the never-caught line
    (a 0% seed is usually a broken seed or grader before it is a weak
    model), and found-then-dropped per seed and repeat. Recomputed from
    the records when the report predates F-95; found-then-dropped is
    then "not recorded", never a claimed none."""
    seeds = report.get("seeds") or []
    counts = per_seed_counts(seeds)
    expected = {str(r.get("id")): r.get("expected_layer") or "-"
                for r in seeds}
    lines = ["", "## Per seed over repeats", ""]
    for seed_id, c in counts.items():
        voided = (f" ({c['voided']} quota-voided)" if c["voided"] else "")
        if c["judged"]:
            body = f"caught {c['caught']}/{c['judged']} repeats{voided}"
        else:
            body = f"not judged ({c['voided']} quota-voided)"
        lines.append(f"- seed {seed_id}: {body} "
                     f"(expected {expected[seed_id]})")
    judged = {k: c for k, c in counts.items() if c["judged"]}
    every = [k for k, c in judged.items() if c["caught"] == c["judged"]]
    never = [k for k, c in judged.items() if c["caught"] == 0]
    lines.append(
        f"pass^k (caught on every judged repeat): {len(every)} of "
        f"{len(judged)} judged seeds"
        + (f" — {', '.join(every)}" if every else "")
    )
    lines.append(
        "Never caught: "
        + (f"{', '.join(never)} — check the seed or grader first"
           if never else "none")
    )
    multi = int(report.get("repeats") or 1) > 1
    for record in seeds:
        if ((record.get("caller_check") or {}).get("residual")):
            tag = f" [r{_repeat_of(record)}]" if multi else ""
            lines.append(f"- seed {record.get('id')}{tag}: reported as "
                         "sibling, did not block (the F-94 residual)")
    entries = report.get("found_then_dropped")
    lines += ["", "## Found, then dropped by the verifier", ""]
    if entries is None:
        lines.append("- not recorded (the report predates F-95)")
        return lines
    shown = [e for e in entries if e.get("dropped")]
    if not shown:
        lines.append("- found, then dropped by the verifier: none")
    for entry in shown:
        tag = (f" [r{entry.get('repeat')}]"
               if int(report.get("repeats") or 1) > 1 else "")
        what = "; ".join(
            f"{d.get('id')} ({d.get('file')}) — {d.get('reason')}"
            + (f" [counted undecidable: {d['undecidable']}]"
               if d.get("undecidable") else "")
            for d in entry["dropped"]
        )
        # F-99 (AC-3): whether the drop cost anything — the seed was
        # still caught by any layer on that run, or this drop was the
        # catch thrown away. Pre-F-99 entries carry no verdict and keep
        # the old line shape.
        cost = ""
        if entry.get("seed_caught") is True:
            cost = " (seed still caught)"
        elif entry.get("seed_caught") is False:
            cost = " (seed missed)"
        lines.append(f"- seed {entry.get('id')}{tag}: found, then dropped "
                     f"by the verifier: {what}{cost}")
    # F-102 (AC-1): the count line means what it says. A drop costs a
    # catch only when the seed's own rule, re-applied with that ONE
    # finding restored, would have caught it — so N counts only
    # ``would_have_caught`` and M is the MISSED runs' drops (a drop on
    # a caught run cost nothing whatever it was). Reports written
    # before F-102 (no field on the drops) keep the F-99 line over all
    # shown drops, and pre-F-99 reports keep no line at all.
    if any("would_have_caught" in d
           for e in shown for d in e["dropped"]):
        missed_drops = [d for e in shown if e.get("seed_caught") is False
                        for d in e["dropped"]]
        # A missed-run drop with no verdict (a hand-edited mixed report)
        # counts: undecided is the costly side, same as the rule below.
        costly = sum(1 for d in missed_drops
                     if "would_have_caught" not in d or d["would_have_caught"])
        lines.append(f"Drops that cost a catch: {costly} of "
                     f"{len(missed_drops)}")
    elif any("seed_caught" in e for e in shown):
        costly = sum(len(e["dropped"]) for e in shown
                     if e.get("seed_caught") is False)
        total = sum(len(e["dropped"]) for e in shown)
        lines.append(f"Drops that cost a catch: {costly} of {total}")
    return lines


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
    config=None,
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
    from factory.runs import FACTORY_ROOT_DEFAULT, RunDir, project_component
    # The reach-ins above (with run_l2_review's _review_prompt /
    # run_verifier_fanout) are deliberate: AC-3 measures the pipeline the
    # queue actually runs, so the eval calls the orchestrator's own
    # prompt builders and gate settings rather than a copy that could
    # drift. Promoting them to public entry points means editing
    # factory/orchestrator.py — a protected path, so an owner change.

    factory_root = Path(factory_root) if factory_root else FACTORY_ROOT_DEFAULT
    eval_root = factory_root / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # F-62 AC-3: the fixture project is a literal today, but it is a
    # project name reaching a factory-root builder — routed through the
    # same validated-slug rule as every other one.
    fixture_project = project_component("soloadventurer")
    fixture_dir = eval_root / fixture_project
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture = build_soloadventurer_fixture(fixture_dir)
    settings_path = eval_root / f"srt-settings-{stamp}.json"
    profile = eval_profile(fixture)
    # F-63 AC-2: the eval's profile is factory-owned source (defined in
    # this module, admitted by the same reviewed-PR chain as the schema
    # consts); its approval is recorded under the root the eval was
    # given — which DEFAULTS to ~/factory for an owner-run eval. The
    # append itself refuses to mix a fixture entry into a ledger that
    # holds owner entries (approvals.py R2-3), so the default root is
    # safe; an explicit, reported code path, never a silent exemption.
    ensure_fixture_profile_approval(factory_root, profile)
    if command_runner is None:
        _gate_settings(profile, RunDir(fixture_project, stamp, fixture,
                                       settings_path), factory_root)
        command_runner = sandboxed_runner(fixture, settings_path)
    if config is None:
        from factory.config import CONFIG_FILE, load_config

        config = load_config(factory_root / CONFIG_FILE)
    try:
        report = run_seeded_eval(
            fixture,
            command_runner=command_runner,
            session_runner=session_runner if session_runner is not None
            else run_session_sandboxed,
            reviewers=reviewers,
            factory_root=factory_root,
            repeats=repeats,
            config=config,
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
    # F-63 AC-2 (same trust chain as the seeded eval's fixture profile
    # above): the prose fixture's committed profile is factory-owned
    # test data (protected tests/fixtures/**); record its approval
    # before the import session starts. Root defaults to ~/factory —
    # the fixture/owner mixing guard (R2-3) keeps owner ledgers clean.
    ensure_fixture_profile_approval(
        root, load_profile(fixture / ".factory" / "project.yaml")
    )
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
