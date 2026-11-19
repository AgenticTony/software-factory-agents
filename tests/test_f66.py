"""F-66 — contract epochs and `factory doctor` (BUILD-ORDER §F-66).

Every rule change the factory loads a project under is declared in a
register (``factory/data/schema/epochs.json``), CI fingerprints the rule
surfaces against it, the current epoch is stamped on every queue write,
``factory doctor`` reports every violation of every onboarded repo
through all-errors loaders with the epoch/WO/fix named, and ``factory
import`` re-proposes a draft-only stale queue instead of refusing.

Fixtures reproduce the F-13 pilot's refusals under tmp_path (a pre-F-64
over-cap queue, a scope-less queue, a profile with a banned allowlist
host). The real JobFinderOS repo is never touched: the owner runs
``factory doctor`` against it after merge (BUILD-ORDER §F-66 tail).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from factory import doctor
from factory.approvals import ApprovalLedger
from factory.cli import build_parser, main
from factory.contract import (
    ContractError,
    apply_profile_defaults,
    load_profile,
    validate_queue,
)
from factory.epochs import (
    IMPLEMENTED_AUTOMATIC,
    current_epoch,
    fingerprint_surfaces,
    load_register,
)
from factory.importer import ImportRefused, run_import
from factory.orchestrator import run_once
from factory.state import TaskQueue

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT = "proj"

# The F-13 refusals, as fixtures: the pilot's real repo is the owner's
# to doctor after merge.
DOCS_ZAI = "docs.z.ai"


def _task(tid: str = "t-1", *, kind: str = "build", status: str = "draft",
          scope: bool = True, wall: int = 30, turns: int = 60,
          approved: bool = False) -> dict:
    """A queue task shaped to the tasks schema; every knob is one of the
    F-13 walls (over-cap wall_clock_min, missing scope, a status past
    draft)."""
    task = {
        "id": tid,
        "title": f"task {tid}",
        "source": "docs/plan.md",
        "kind": kind,
        "priority": "P2",
        "status": status,
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [
            {"id": "AC-1", "text": "it works",
             "check": {"type": "manual"}},
        ],
        "budget": {"size": "S", "max_attempts": 2, "max_turns": turns,
                   "wall_clock_min": wall},
    }
    if scope:
        task["scope"] = {"paths": ["src/**"]}
    if approved:
        task["acceptance_hash"] = "0" * 64
        task["approved_at"] = "2026-11-01T00:00:00Z"
        task["approved_by"] = "owner"
    return task


def _profile(repo: Path, *, docs_allowlist: list | None = None) -> dict:
    """A minimal self-project profile that loads under current rules."""
    return {
        "name": PROJECT,
        "repo": {
            "path": str(repo),
            "default_branch": "main",
            "remote": "https://example.invalid/proj.git",
        },
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": [".factory/**"]},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": docs_allowlist if docs_allowlist is not None else [],
    }


def _write_repo(base: Path, *, tasks: list | None = None,
                profile_extra: dict | None = None,
                queue_text: str | None = None,
                queue_doc: dict | None = None) -> tuple[Path, Path, Path]:
    """One fixture repo under tmp_path: (repo, queue_path, profile_path)."""
    repo = base
    factory_dir = repo / ".factory"
    factory_dir.mkdir(parents=True)
    profile = _profile(repo)
    for key, value in (profile_extra or {}).items():
        if isinstance(value, dict) and isinstance(profile.get(key), dict):
            profile[key] = {**profile[key], **value}
        else:
            profile[key] = value
    profile_path = factory_dir / "project.yaml"
    profile_path.write_text(yaml.safe_dump(profile))
    queue_path = factory_dir / "tasks.json"
    if queue_text is not None:
        queue_path.write_text(queue_text)
    else:
        doc = queue_doc if queue_doc is not None else {
            "version": current_epoch(), "project": PROJECT,
            "tasks": tasks if tasks is not None else [_task()],
        }
        queue_path.write_text(json.dumps(doc, indent=2) + "\n")
    return repo, queue_path, profile_path


def _approve_profile(root: Path, profile_path: Path) -> None:
    """Record the fixture's profile approval the owner way (with the
    F-66 repo_path the ledger now records)."""
    profile = load_profile(profile_path)
    ledger = ApprovalLedger.for_project(root, profile["name"])
    ledger.record_profile_for(profile, approved_by="owner")


def _proposal_text(*tasks: dict) -> str:
    """A scripted importer-session answer: a full replacement proposal
    under the current rules (no status — the importer writes it)."""
    return json.dumps({
        "project": PROJECT,
        "tasks": [
            {k: v for k, v in t.items() if k != "status"} for t in tasks
        ],
    })


def _snapshot(root: Path) -> dict:
    """path -> (sha256, mtime_ns) for every file under root."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            out[str(path)] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_mtime_ns,
            )
    return out


# --------------------------------------------------------------------- AC-1 --

def test_ac_1():
    """Epochs are declared, CI fingerprints the rule surfaces, the
    current epoch is stamped on every factory write."""
    reg = load_register()
    # Epochs 1-4: baseline plus the three retroactive WOs the pilot hit.
    by_wo = {e.wo: e for e in reg.entries}
    assert {e.epoch for e in reg.entries} >= {1, 2, 3, 4}
    assert current_epoch() == 4
    assert by_wo["F-64"].epoch == 2 and "queue" in by_wo["F-64"].surfaces
    assert by_wo["F-65"].epoch == 3
    assert by_wo["F-59"].epoch == 4 and "profile" in by_wo["F-59"].surfaces
    # Every tightening raises the epoch; a non-tightening entry does not.
    seen = 0
    for entry in reg.entries:
        if entry.tightens:
            assert entry.epoch == seen + 1
            seen = entry.epoch
        else:
            assert entry.epoch <= seen
    # F-96's verification block is registered as tightening nothing.
    f96 = by_wo.get("F-96")
    assert f96 is not None and f96.tightens is False
    assert f96.epoch == 4  # the epoch did not move
    # The fingerprint: live rule surfaces equal the LATEST entry's.
    latest = reg.entries[-1]
    assert latest.fingerprint is not None, (
        "the latest epochs.json entry must carry the fingerprint the CI "
        "test compares the live rule surfaces against"
    )
    live = fingerprint_surfaces()
    assert live["schemas"] == latest.fingerprint.schemas, (
        "a schema file changed with no epochs.json entry — add one "
        "(a change that tightens nothing gets an entry that says so; "
        "VALIDATOR-SPEC 'contract epochs')"
    )
    assert live["functions"] == latest.fingerprint.functions, (
        "a semantic-check function changed (or a new rule function "
        "exists) with no epochs.json entry — add one"
    )
    # Every registered automatic rewrite is implemented.
    for entry in reg.entries:
        if entry.migration.kind == "automatic":
            assert entry.migration.name in IMPLEMENTED_AUTOMATIC
    # The factory's own queue carries the current epoch.
    own = json.loads((REPO_ROOT / ".factory" / "tasks.json").read_text())
    assert own["version"] == current_epoch()
    # A queue from a future epoch refuses to load (a downgrade never
    # reinterprets a newer queue).
    with pytest.raises(ContractError, match="epoch"):
        validate_queue(
            {"version": current_epoch() + 1, "project": "p", "tasks": []},
            "tasks",
        )


def test_ac_1_version_stamped_on_every_write(tmp_path):
    """TaskQueue.save (the orchestrator's one writer) re-stamps the
    queue's version to the current epoch; nothing else in the document
    changes that the writer did not intend."""
    repo, queue_path, _ = _write_repo(
        tmp_path / "r",
        queue_doc={"version": 1, "project": PROJECT, "tasks": [_task()]},
    )
    queue = TaskQueue(queue_path)
    assert queue.doc["version"] == 1  # loaded stale, written current
    queue.transition("t-1", "blocked")
    written = json.loads(queue_path.read_text())
    assert written["version"] == current_epoch()


# --------------------------------------------------------------------- AC-2 --

def test_ac_2(tmp_path, capsys):
    """Doctor: read-only, every repo in the ledger plus --repo, ALL
    violations through all-errors loaders, epoch/migration named,
    non-zero on any finding, clean repo exits 0."""
    root = tmp_path / "froot"
    root.mkdir()

    # The pre-F-64 queue: TWO over-cap tasks — both must be reported
    # (all-errors, not the first refusal).
    repo1, qp1, pp1 = _write_repo(
        tmp_path / "r1",
        tasks=[_task(wall=600), _task("t-2", wall=450)],
    )
    findings = doctor.project_report(pp1, qp1, factory_root=root).findings
    assert findings, "the over-cap queue must produce findings"
    over = [f for f in findings if f.tag == "budget"]
    assert len(over) >= 4, (
        "every over-cap violation is collected (schema + per-task, both "
        f"tasks), not just the first refusal — got {len(over)}"
    )
    named = " ".join(f.message for f in findings)
    assert "t-1" in named and "t-2" in named
    f = over[0]
    assert f.epoch == 2 and "F-64" in f.wo
    assert f.fix and "factory import" in f.fix

    # The scope-less queue: valid schema, approval would refuse.
    repo2, qp2, pp2 = _write_repo(
        tmp_path / "r2", tasks=[_task(scope=False)],
    )
    findings = doctor.project_report(pp2, qp2, factory_root=root).findings
    scope = [f for f in findings if f.check == "task-approvability"]
    assert scope and "t-1" in scope[0].message
    assert scope[0].epoch == 3 and "F-65" in scope[0].wo
    assert scope[0].fix

    # Semantic violations collect all-errors too: TWO malformed scope
    # patterns both load-refuse — both must be reported.
    repo2b, qp2b, pp2b = _write_repo(
        tmp_path / "r2b",
        queue_doc={"version": 1, "project": PROJECT, "tasks": [
            {**_task("t-1"), "scope": {"paths": ["../climb/**"]}},
            {**_task("t-2"), "scope": {"paths": ["**"]}},
        ]},
    )
    findings = doctor.project_report(pp2b, qp2b,
                                     factory_root=root).findings
    semantics = [f for f in findings if f.check == "queue-semantics"]
    assert len(semantics) == 2, (
        f"both malformed scope patterns reported, not the first — got "
        f"{len(semantics)}"
    )
    assert all(f.epoch == 3 and "F-65" in f.wo for f in semantics)

    # The banned-host profile: the docs.z.ai refusal, with the epoch.
    repo3, qp3, pp3 = _write_repo(
        tmp_path / "r3", profile_extra={"docs_allowlist": [DOCS_ZAI]},
    )
    findings = doctor.project_report(pp3, qp3, factory_root=root).findings
    host = [f for f in findings if DOCS_ZAI in f.message]
    assert host, "the banned allowlist host is reported"
    assert host[0].epoch == 4 and "F-59" in host[0].wo
    assert "factory approve --profile" in host[0].fix

    # A clean repo: no findings, and the CLI exits 0 with a summary.
    repo4, qp4, pp4 = _write_repo(tmp_path / "r4")
    _approve_profile(root, pp4)
    report = doctor.project_report(pp4, qp4, factory_root=root)
    assert report.findings == []
    exit_code = main(["doctor", "--repo", str(repo4),
                      "--factory-root", str(root)])
    assert exit_code == 0
    assert "clean" in capsys.readouterr().out

    # Every repo in the ledger: doctor finds r4's repo WITHOUT --repo.
    _approve_profile(root, pp1)  # r1's profile is clean; its queue is not
    ledgers = doctor.factory_report(root, extra_repos=[])
    rendered = doctor.render(ledgers)
    assert "r1" in rendered and "wall_clock_min" in rendered

    # Read-only: a full doctor run changes no file under the factory
    # root or any repo (hash and mtime).
    before = {p: _snapshot(p) for p in (root, tmp_path / "r1",
                                        tmp_path / "r4")}
    doctor.factory_report(root, extra_repos=[])
    for path, snap in before.items():
        assert _snapshot(path) == snap, f"doctor wrote under {path}"

    # A ledger repo path that does not exist, or is a symlink: named,
    # never a crash.
    gone = tmp_path / "gone"
    gone.mkdir()
    pp5 = tmp_path / "r5" / ".factory" / "project.yaml"
    pp5.parent.mkdir(parents=True)
    prof5 = _profile(tmp_path / "r5")
    prof5["repo"]["path"] = str(gone)
    pp5.write_text(yaml.safe_dump(prof5))
    ledger = ApprovalLedger.for_project(root, PROJECT)
    ledger.record_profile_for(load_profile(pp5), approved_by="owner")
    shutil.rmtree(gone)
    link = tmp_path / "linked-repo"
    link.symlink_to(tmp_path / "r1")
    findings = [f for report in doctor.factory_report(
        root, extra_repos=[link]) for f in report.findings]
    assert any("does not exist" in f.message for f in findings)
    assert any("symlink" in f.message for f in findings)

    # A queue that is not JSON: reported, never a crash.
    repo6, qp6, pp6 = _write_repo(tmp_path / "r6", queue_text="{not json")
    findings = doctor.project_report(pp6, qp6, factory_root=root).findings
    assert any(f.check == "queue-parse" for f in findings)

    # run refuses with doctor's full report, not one bare schema error.
    with pytest.raises(ContractError) as excinfo:
        run_once(qp1, profile_path=pp1, factory_root=root)
    message = str(excinfo.value)
    assert "t-1" in message and "t-2" in message
    assert "epoch 2" in message and "F-64" in message
    assert "factory import" in message


def test_ac_2_ledger_repo_path_recorded(tmp_path):
    """The ledger records the repo path with each profile approval;
    doctor reports the re-approval owed when it predates the field."""
    root = tmp_path / "froot"
    root.mkdir()
    repo, queue_path, profile_path = _write_repo(tmp_path / "r")
    _approve_profile(root, profile_path)
    ledger = ApprovalLedger.for_project(root, PROJECT)
    approval = ledger.latest_profile_approval()
    assert approval.get("repo_path") == str(
        Path(approval["repo_path"]).resolve()
    ), "the repo path is recorded expanded+resolved"
    assert approval["repo_path"].startswith(str(tmp_path))

    # A ledger whose profile approval predates the field: doctor names
    # the re-approval owed (a fresh hand-built genesis line, chained).
    from factory.approvals import profile_group_hashes
    fresh = tmp_path / "froot2"
    (fresh / "approvals").mkdir(parents=True)
    profile = apply_profile_defaults(_profile(tmp_path / "r"))
    manual = fresh / "approvals" / f"{PROJECT}.jsonl"
    manual.write_text(json.dumps({
        "kind": "profile", "project": PROJECT,
        "groups": profile_group_hashes(profile),
        "approved_by": "owner", "approved_at": "2026-11-01T00:00:00Z",
        "n": 1, "prev": "0" * 64,
    }) + "\n")
    findings = [f for report in doctor.factory_report(fresh, extra_repos=[])
                for f in report.findings]
    assert any("repo_path" in f.message or "re-approve" in f.message
               for f in findings), (
        "a ledger whose profile approvals predate repo_path is reported"
    )


# --------------------------------------------------------------------- AC-3 --

def test_ac_3(tmp_path):
    """The F-13 pilot's two refusals each name the epoch, the WO, what
    changed and the exact fix command — never a bare schema line."""
    root = tmp_path / "froot"
    root.mkdir()

    # Refusal 1: the over-cap queue.
    repo, qp, pp = _write_repo(tmp_path / "r1",
                               tasks=[_task(wall=600)])
    with pytest.raises(ContractError) as excinfo:
        run_once(qp, profile_path=pp, factory_root=root)
    message = str(excinfo.value)
    assert "epoch 2" in message
    assert "F-64" in message
    assert "factory import" in message
    assert "wall_clock_min" in message  # what changed is named
    bare = re.search(r"^.*schema violation at tasks/\d+.*$", message,
                     re.M)
    assert not (bare and bare.group() == message.strip()), (
        "the refusal never ends as only a bare schema-violation line"
    )

    # Refusal 2: the docs.z.ai profile.
    repo, qp, pp = _write_repo(tmp_path / "r2",
                               profile_extra={"docs_allowlist": [DOCS_ZAI]})
    with pytest.raises(ContractError) as excinfo:
        run_once(qp, profile_path=pp, factory_root=root)
    message = str(excinfo.value)
    assert DOCS_ZAI in message
    assert "epoch 4" in message
    assert "F-59" in message
    assert "factory approve --profile" in message

    # The same two refusals through `factory import`.
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, queue_path=qp, session=lambda p: "unused",
                   factory_root=root)
    assert "epoch 4" in str(excinfo.value)
    assert "F-59" in str(excinfo.value)


# --------------------------------------------------------------------- AC-4 --

def test_ac_4(tmp_path):
    """A draft-only stale queue is re-proposed; anything past draft, or
    an unparseable file, refuses naming the tasks; no migration ever
    touches an approved task's contract."""
    root = tmp_path / "froot"
    root.mkdir()

    # A draft-only pre-F-64 queue: re-import proposes a full replacement
    # under the current rules, reviewed as a first import (full diff).
    repo, qp, pp = _write_repo(tmp_path / "r1", tasks=[_task(wall=600)])
    _approve_profile(root, pp)
    result = run_import(
        repo, qp, session=lambda _p: _proposal_text(_task(wall=30)),
        factory_root=root,
    )
    assert result.plan.stale_base is True
    assert "600" in result.plan.diff and "30" in result.plan.diff, (
        "the review shows the whole old-file-to-proposal diff"
    )
    assert "epoch" in result.summary  # the staleness is stated
    apply = run_import(
        repo, qp, session=lambda _p: _proposal_text(_task(wall=30)),
        factory_root=root, confirm=lambda _s: True,
        confirm_drop=lambda _d: True,
    )
    assert apply.applied
    written = json.loads(qp.read_text())
    validate_queue(written, str(qp))  # the written queue is current-rule valid
    assert written["version"] == current_epoch()
    assert written["tasks"][0]["budget"]["wall_clock_min"] == 30

    # The same queue with ONE approved task: the re-proposal refuses,
    # naming it, and the file is untouched byte for byte.
    repo2, qp2, pp2 = _write_repo(
        tmp_path / "r2",
        tasks=[_task("t-1", wall=600, status="ready", approved=True),
               _task("t-2", wall=600)],
    )
    _approve_profile(root, pp2)
    before = qp2.read_text()
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo2, qp2, session=lambda _p: _proposal_text(_task()),
                   factory_root=root)
    message = str(excinfo.value)
    assert "t-1" in message and "epoch" in message
    assert qp2.read_text() == before, (
        "an approved task's queue is never rewritten by a refusal"
    )

    # An approved task's contract_hash survives every factory write:
    # the only automatic rewrite re-stamps `version` (top-level).
    repo3, qp3, pp3 = _write_repo(
        tmp_path / "r3",
        queue_doc={"version": 1, "project": PROJECT,
                   "tasks": [_task("t-1", status="ready", approved=True)]},
    )
    task_before = json.loads(qp3.read_text())["tasks"][0]
    queue = TaskQueue(qp3)
    queue.get("t-1")["title"] = "renamed by the orchestrator"
    queue.save()
    task_after = json.loads(qp3.read_text())["tasks"][0]
    assert task_after == {**task_before,
                          "title": "renamed by the orchestrator"}, (
        "the write applied exactly the orchestrator's own change and the "
        "version stamp — the automatic rewrite touched nothing inside "
        "the task"
    )
    assert task_after["acceptance_hash"] == task_before["acceptance_hash"]

    # A file that does not parse: refused, naming the epoch it predates.
    repo4, qp4, pp4 = _write_repo(tmp_path / "r4", queue_text="{not json")
    _approve_profile(root, pp4)
    with pytest.raises(ImportRefused):
        run_import(repo4, qp4, session=lambda _p: _proposal_text(_task()),
                   factory_root=root)

    # HOSTILE is not stale (F-62's shape, re-pinned here): a queue whose
    # project slug climbs out (../../outside) is draft-only and
    # parses, but the re-proposal gate refuses it and the session never
    # starts — staleness excuses rule-shaped failures, never hostility.
    repo5, qp5, pp5 = _write_repo(
        tmp_path / "r5",
        queue_doc={"version": 1, "project": "../../outside",
                   "tasks": [_task(wall=600)]},
    )
    _approve_profile(root, pp5)
    with pytest.raises(ContractError):
        run_import(repo5, qp5, session=_never, factory_root=root)

    # R1 (review round 1): a queue naming a NEWER epoch is never a
    # re-proposal base — a factory older than the queue's epoch never
    # rewrites it under rules it doesn't know (the AC-1 ceiling,
    # import-side). The refusal uses the ceiling's own words and the
    # file is byte-identical.
    repo6, qp6, pp6 = _write_repo(
        tmp_path / "r6",
        queue_doc={"version": current_epoch() + 95, "project": PROJECT,
                   "tasks": [_task(wall=600)]},
    )
    _approve_profile(root, pp6)
    before6 = qp6.read_text()
    with pytest.raises(ImportRefused, match="newer than"):
        run_import(repo6, qp6, session=_never, factory_root=root)
    assert qp6.read_text() == before6


def _never(prompt):
    raise AssertionError("the session must never start")


QUEUES_TOTAL = [
    # (doc tasks, first-violation substring) — one clean FIRST violation
    # plus a malformed LATER field the first-error path never reached.
    (
        [{"id": "t\x1b1", "kind": "owner_only", "status": "done",
          "owner_steps": ["x"]}],
        "control character",
    ),
    (
        [{"id": "t-1"}, {"id": "t-1", "kind": "owner_only",
                         "status": "done", "owner_steps": [3]}],
        "duplicate task id",
    ),
    (
        [{"id": "t\x1b1", "kind": "owner_only", "status": "done",
          "owner_steps": [{"done": True}]}],
        "control character",
    ),
]

PROFILES_TOTAL = [
    # (profile overrides, first-violation substring)
    (
        {"docs_allowlist": ["docs.z.ai"], "seat_providers": ["x"]},
        "docs.z.ai",
    ),
    (
        {"docs_allowlist": ["docs.z.ai"], "sentinels": {"flag": 3}},
        "docs.z.ai",
    ),
    (
        {"docs_allowlist": ["docs.z.ai"], "paths": ["x"]},
        "docs.z.ai",
    ),
    (
        {"docs_allowlist": ["docs.z.ai"], "toolkit": ["x"]},
        "docs.z.ai",
    ),
    (
        {"docs_allowlist": ["docs.z.ai"], "name": 7},
        "docs.z.ai",
    ),
]


def test_collectors_total_over_malformed_shapes(tmp_path):
    """R2 (review round 1): the all-errors collectors are TOTAL where
    the first-error path refused — validate_queue/load_profile still
    refuse naming the FIRST violation (never a traceback), and doctor
    reports the same finding without crashing."""
    from factory.contract import (
        _profile_semantic_violations,
        _semantic_violations,
    )
    for tasks, expected in QUEUES_TOTAL:
        doc = {"version": current_epoch(), "project": PROJECT,
               "tasks": tasks}
        with pytest.raises(ContractError, match=expected):
            validate_queue(doc, "tasks")
        violations = _semantic_violations(tasks, "tasks")
        assert violations and expected in violations[0][1]
    for index, (overrides, expected) in enumerate(PROFILES_TOTAL):
        repo, qp, pp = _write_repo(tmp_path / f"p{index}",
                                    profile_extra=overrides)
        data = yaml.safe_load(pp.read_text())
        violations = _profile_semantic_violations(data, str(pp))
        assert violations and expected in violations[0][1], overrides
        findings = doctor.project_report(
            pp, qp, factory_root=tmp_path / "root").findings
        assert any(f.check == "profile-semantics" and expected in f.message
                   for f in findings), overrides


# ------------------------------------------------------------------ CLI pin --

def test_doctor_cli_registered():
    """`factory doctor` is a real subcommand with --repo/--factory-root."""
    parser = build_parser()
    args = parser.parse_args(["doctor", "--repo", "/tmp/x"])
    assert args.command == "doctor" and args.repo == ["/tmp/x"]
