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
  (:func:`run_l2_review`), exactly the pipeline the orchestrator runs.

It runs on demand and monthly — never in routine CI: with real L2 it
drives real model sessions (:func:`run_on_demand_eval`, the
``factory eval`` command). Tests drive it with scripted seats; nothing
in this module is on any routine path.
"""

import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from factory.gates import git as hardened_git
from factory.l0 import run_l0
from factory.l1 import run_l1
from factory.l2 import apply_vote, merge_reviews, parse_verdict, parse_verifier_vote
from factory.session import run_sessions_parallel

# Per-session budgets for the eval's L2 seats. An eval diff is tiny
# compared to a real attempt; the caps keep a confused session bounded.
SEAT_MAX_TURNS = 20
SEAT_TIMEOUT_MIN = 10.0
DEFAULT_REVIEWERS = 2


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
    """

    id: str
    layer: str
    title: str
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

    ``python`` overrides the interpreter in the command strings — the
    on-demand driver passes the run-local gate venv's (``.venv/bin/python``),
    because the sandbox refuses to execute the orchestrator's own
    interpreter; tests keep ``sys.executable`` (unconfined, like the
    F-07/F-08 fixtures).
    """
    python = python or sys.executable
    quoted = shlex.quote(python)
    return {
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
    }


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
        edits=(
            ('store.insert("trip_archive", {"trip_id": trip_id})',
             'store.insert("archived_trips", {"trip_id": trip_id})'),
        ),
    ),
    SeedSpec(
        id="removed-rls-filter",
        layer="L0",
        title="the ownership filter dropped from an owner-scoped read",
        edits=(
            ('    return [row for row in rows if row["owner_id"] == owner_id]',
             "    return list(rows)"),
        ),
    ),
    SeedSpec(
        id="off-by-one",
        layer="L0",
        title="the pack-list cap reads one item past the limit",
        edits=(
            ("    return items[:limit]", "    return items[: limit + 1]"),
        ),
    ),
    SeedSpec(
        id="deleted-await",
        layer="L0",
        title="the await dropped from the checkin-count fetch",
        edits=(
            ("    row = await store.fetchone(", "    row = store.fetchone("),
        ),
    ),
    SeedSpec(
        id="mocks-what-it-tests",
        layer="L2",
        title="a test that mocks the function it claims to test",
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
) -> dict:
    """Run the validator's layers over one seeded (or clean) diff.

    Creates branch ``eval/<id>`` from ``base_ref`` in the fixture repo,
    applies the seed, and runs L0 and L1 — plus L2 as F-38's parallel
    reviewer and verifier when ``session_runner`` is given (the real
    runner drives real model sessions; tests script it). The repo is
    left on the seed's branch.

    Returns the per-diff record: each layer's report dict and the layers
    that caught the seed (verdict not ``pass``); a clean diff carries the
    number of blocking findings its review drew instead.
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
        layers["L2"] = run_l2_review(
            repo, profile, base_ref, f"eval/{seed.id}", task,
            session_runner=session_runner, reviewers=reviewers,
            l0=l0, l1=l1,
        ).to_dict()
    record = {
        "id": seed.id,
        "title": seed.title,
        "expected_layer": getattr(seed, "layer", None),
        "layers": layers,
    }
    if isinstance(seed, SeedSpec):
        record["caught_by"] = [
            name for name, report in layers.items()
            if report["verdict"] not in (None, "pass")
        ]
    else:
        record["blocking_findings"] = _count_blocking(layers.get("L2"))
    return record


def _seed_task(seed: SeedSpec | CleanSpec) -> dict:
    acceptance = [seed.contract] if getattr(seed, "contract", None) else []
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
):
    """L2 as F-38's parallel reviewer and verifier over one diff.

    The orchestrator's pipeline, unchanged: N reviewer sessions over the
    same diff (a fresh context each, run concurrently through
    :func:`factory.session.run_sessions_parallel`), the verdicts merged
    into a union, and — when the union carries findings — one verifier
    session voting the union down. No convergence pass: an eval review
    is a single round.

    Args:
        l0 / l1: The layer reports the reviewers see alongside the diff
            (the orchestrator hands its reviewers the same). Defaults to
            the report dicts already on the record when re-reviewing.
    """
    from factory import orchestrator
    from factory.prompts import assemble_reviewer_prompt

    diff = _git(repo, "diff", "--no-color", "--no-ext-diff",
                f"{base_ref}...{head_ref}")
    l0_dict = l0.to_dict() if l0 is not None else {}
    l1_dict = l1.to_dict() if l1 is not None else {}
    review_prompt = orchestrator._review_prompt(task, diff, l0_dict, l1_dict)
    reviews = run_sessions_parallel(
        [("reviewer", review_prompt)] * reviewers,
        repo, profile,
        runner=session_runner,
        max_turns=SEAT_MAX_TURNS,
        timeout_min=SEAT_TIMEOUT_MIN,
        protected=list((profile.get("paths") or {}).get("protected") or []),
        system_prompt=assemble_reviewer_prompt(profile),
    )
    union = merge_reviews([
        parse_verdict(review.result if review.status == "completed" else None)
        for review in reviews
    ])
    if union.parse_error is not None or not union.findings:
        return union
    verifier_prompt = orchestrator._verifier_prompt(task, diff, union.findings)
    from factory.prompts import assemble_verifier_prompt

    verifier = session_runner(
        "verifier", verifier_prompt, repo, profile,
        max_turns=SEAT_MAX_TURNS,
        timeout_min=SEAT_TIMEOUT_MIN,
        protected=list((profile.get("paths") or {}).get("protected") or []),
        system_prompt=assemble_verifier_prompt(profile),
    )
    vote = parse_verifier_vote(
        verifier.result if verifier.status == "completed" else None,
        union.findings,
    )
    return apply_vote(union, vote, reviewers=reviewers)


def _count_blocking(l2: dict | None) -> int:
    if not l2:
        return 0
    return sum(
        1 for f in l2.get("findings") or []
        if isinstance(f, dict) and f.get("severity") == "blocking"
    )


LAYERS = ("L0", "L1", "L2")


def run_seeded_eval(
    repo: Path,
    *,
    base_ref: str = "main",
    command_runner=None,
    session_runner=None,
    reviewers: int = DEFAULT_REVIEWERS,
) -> dict:
    """Run the whole eval over the fixture set and return its report.

    Every seeded bug and every known-clean diff goes through
    :func:`run_seed`; the report carries the per-diff records and the
    catch rate per layer (the share of seeds each layer caught). Without
    ``session_runner`` the L2 layer is skipped and its rate is 0 — the
    report then says so rather than implying L2 was measured.
    """
    repo = Path(repo)
    common = {
        "base_ref": base_ref,
        "command_runner": command_runner,
        "session_runner": session_runner,
        "reviewers": reviewers,
    }
    seeds = [run_seed(repo, seed, **common) for seed in SOLOADVENTURER_SEEDS]
    clean = [run_seed(repo, diff, **common) for diff in CLEAN_DIFFS]
    return {
        "fixture": "soloadventurer",
        "base_ref": base_ref,
        "l2": "sessions" if session_runner is not None else "skipped",
        "seeds": seeds,
        "clean": clean,
        "catch_rate": catch_rate_by_layer(seeds),
        "caught_counts": caught_counts_by_layer(seeds),
    }


def catch_rate_by_layer(seed_records: list) -> dict:
    """The share of seeds each layer caught (verdict not ``pass``)."""
    counts = caught_counts_by_layer(seed_records)
    total = len(seed_records) or 1
    return {layer: counts[layer] / total for layer in LAYERS}


def caught_counts_by_layer(seed_records: list) -> dict:
    counts = {layer: 0 for layer in LAYERS}
    for record in seed_records:
        for layer in record.get("caught_by") or []:
            if layer in counts:
                counts[layer] += 1
    return counts


def render_report(report: dict) -> str:
    """The owner-facing per-diff report (the AC-3 tick evidence): per
    seeded bug, which layers caught it; per known-clean diff, how many
    blocking findings its review drew; then the catch rate per layer."""
    lines = [
        f"Seeded-bug validator eval — fixture: {report['fixture']} "
        f"(base {report['base_ref']}, L2: {report['l2']})",
        "",
        "## Per diff",
        "",
    ]
    for record in report["seeds"]:
        caught = ", ".join(record["caught_by"]) or "NOT CAUGHT"
        expected = record.get("expected_layer") or "-"
        lines.append(
            f"- seed {record['id']}: caught by {caught} "
            f"(expected {expected}) — {record['title']}"
        )
    for record in report["clean"]:
        lines.append(
            f"- clean {record['id']}: {record['blocking_findings']} blocking "
            f"finding(s) — {record['title']}"
        )
    lines += ["", "## Catch rate per layer", ""]
    counts = report["caught_counts"]
    total = len(report["seeds"])
    for layer in LAYERS:
        lines.append(
            f"- {layer}: {report['catch_rate'][layer]:.0%} "
            f"({counts[layer]} of {total} seeds)"
        )
    lines.append("")
    return "\n".join(lines)
