"""F-67 acceptance tests — BUILD-ORDER.md, work order F-67.

Importer v0 gates (AC-1: the parent forces human gates on everything it
writes and approve refuses anything else), the import session record
(AC-2: every real import and repair session leaves a provider-stamped,
credential-free record under ``<factory-root>/import/``, and status
shows it), the possibly-shipped flag (AC-3: flagged for the owner, never
decided) and dropped-criteria accounting (AC-4: every reference task and
criterion is mapped or explicitly dropped against a doc line; a drop
still in the docs blocks the apply until the owner accepts it, per
drop).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f67.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the F-02 loader and the real tasks schema, the full
  ``plan_import`` validation (normalisation, possibly-shipped
  validation, criteria accounting), the real prompts, the real record
  writer (mode 0600, ``<factory-root>/import/``), the real apply path
  with its per-drop gate, ``factory approve``'s gate check, and
  ``render_status`` over written records. The fixture repo is committed
  (the sandboxed session path clones it).
* **Stubbed:** the model — the real-session seam
  (``factory.importer.run_session_sandboxed`` + the keychain) is patched
  with the F-57 ``ScriptedRunner`` pattern for the AC-2 record tests;
  everything else drives the injected-stub session path, which exercises
  the same parent-side checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _support.f57 import ScriptedRunner
from _support.f63 import importer_fixture_approve
from _support.f67 import (
    CAP_DONE_LINE,
    DPA_CRITERION,
    LANGUAGE_CRITERION,
    PROJECT,
    URL_CRITERION,
    URL_LINE,
    _ac,
    _proposal,
    _pwo,
    _record,
    _ref_check,
    build_repo,
    fresh_repo,
    import_records,
)
from _support.tokens import fake_token

from factory.approve import ApprovalError, approve_task, check_v0_gates
from factory.config import FactoryConfig
from factory.contract import load_tasks
from factory.importer import (
    ImportRefused,
    plan_import,
    render_summary,
    run_import,
)
from factory.state import TaskQueue
from factory.status import render_status

REPO = Path(__file__).resolve().parent.parent
SEAT_TOKEN = fake_token("f67-seat")
PILOT_19 = "docs/work-orders/PILOT-19.md"
PILOT_07 = "docs/work-orders/PILOT-07.md"


def _stub_session(text: str):
    return lambda prompt: text


def _split_tasks() -> list[dict]:
    """The pilot's split shape: PILOT-19's three criteria mapped across
    two new tasks (predecessors + per-criterion from), and the
    owner-only task re-proposed under its own id (spec_approval kept
    false). The carried criteria keep the reference's TEST checks — an
    honest split keeps the enforcement (R2-2 arms exercise the loss of
    it)."""
    return [
        _pwo("p19a", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "trust boundary block present",
                             "pilot-19/AC-1",
                             check=_ref_check("pilot-19", 1)),
                         _ac("AC-2", URL_CRITERION, "pilot-19/AC-2",
                             check=_ref_check("pilot-19", 2))]),
        _pwo("p19b", predecessors=["pilot-19"], depends_on=["p19a"],
             acceptance=[_ac("AC-1", LANGUAGE_CRITERION, "pilot-19/AC-3",
                             check=_ref_check("pilot-19", 3))]),
        _pwo("pilot-23", kind="owner_only",
             gates={"spec_approval": False, "merge": "human",
                    "deploy": "human"},
             scope={"paths": ["docs/**"]},
             acceptance=[_ac("AC-1", DPA_CRITERION,
                             check=_ref_check("pilot-23", 1))]),
    ]


def _owner_carry() -> dict:
    return _split_tasks()[2]


def _run_real(repo, monkeypatch, records, *, token: str = "tok", **kwargs):
    """The offline real-session path (the F-57 pattern): approve the
    fixture profile, patch the session seam and the keychain, delegate
    to the real run_import."""
    root = kwargs.setdefault("factory_root", repo.parent / "froot")
    importer_fixture_approve(root, repo)
    monkeypatch.setattr("factory.importer.run_session_sandboxed",
                        ScriptedRunner(records))
    monkeypatch.setattr(
        "factory.provider._keychain_token",
        lambda service: token if service == "factory-zai" else None)
    kwargs.setdefault("queue_path", repo.parent / "fresh-tasks.json")
    return run_import(repo, **kwargs)


# ----------------------------------------------------------------- AC-1 --


def test_ac_1(tmp_path: Path):
    """AC-1: imported gates are human in v0 — whatever the proposal says,
    the parent forces ``merge``/``deploy`` human and ``spec_approval``
    true on picker-eligible kinds, lists every override before the diff,
    keeps owner_only's ``spec_approval: false``, and ``factory approve``
    refuses a non-human gate wherever it came from."""
    repo = fresh_repo(tmp_path)

    # (a) through the real plan_import + render_summary: a build task
    # proposing every relaxation lands all-human, each override listed
    # with its proposed value, before the diff.
    relaxed = _pwo(
        "WO-9",
        gates={"spec_approval": False, "merge": "auto", "deploy": "auto"},
    )
    plan = plan_import(None, json.loads(_proposal(relaxed)), [],
                       project=PROJECT)
    landed = plan.proposed_doc["tasks"][0]["gates"]
    assert landed == {"spec_approval": True, "merge": "human",
                      "deploy": "human"}
    summary = render_summary(plan)
    overrides_at = summary.index("## Gate overrides")
    diff_at = summary.index("## Full diff")
    assert overrides_at < diff_at
    section = summary[overrides_at:diff_at]
    for field, proposed in (("merge", "'auto'"), ("deploy", "'auto'"),
                            ("spec_approval", "False")):
        assert f"WO-9: gates.{field}" in section, field
        assert proposed in section, field

    # (b) through run_import with a stub session: the write is what the
    # plan showed.
    result = run_import(repo, session=_stub_session(_proposal(relaxed)),
                        confirm=lambda s: True)
    assert load_tasks(result.queue_path)["tasks"][0]["gates"] == landed

    # (c) owner_only keeps its spec_approval: false (no forcing outside
    # ELIGIBLE_KINDS); merge/deploy are still forced human.
    owner = _pwo("WO-10", kind="owner_only",
                 gates={"spec_approval": False, "merge": "auto",
                        "deploy": "auto"},
                 scope={"paths": ["docs/**"]})
    result = run_import(repo, session=_stub_session(_proposal(
        _pwo("WO-9"), owner)), confirm=lambda s: True)
    tasks = {t["id"]: t for t in load_tasks(result.queue_path)["tasks"]}
    assert tasks["WO-10"]["gates"]["spec_approval"] is False
    assert tasks["WO-10"]["gates"]["merge"] == "human"
    assert tasks["WO-10"]["gates"]["deploy"] == "human"
    assert "spec_approval" not in result.summary.split(
        "## Gate overrides")[-1].split("##")[0]

    # (d) a MERGED task: the proposal re-proposes an existing id with
    # deploy: auto — the stored human value survives and the override
    # names both values.
    again = _pwo("WO-9", gates={"spec_approval": True, "merge": "human",
                                "deploy": "auto"})
    result = run_import(repo, session=_stub_session(_proposal(
        _pwo("WO-10"), again)), confirm=lambda s: True)
    tasks = {t["id"]: t for t in load_tasks(result.queue_path)["tasks"]}
    assert tasks["WO-9"]["gates"]["deploy"] == "human"
    section = result.summary.split("## Gate overrides")[-1].split("##")[0]
    assert "WO-9: gates.deploy" in section and "'auto'" in section

    # (e) approve refuses a planted non-human gate, naming task and field
    # — before any scope or ledger concern.
    queue_path = tmp_path / "planted" / "tasks.json"
    queue_path.parent.mkdir(parents=True)
    planted = _pwo("WO-PL")
    planted["status"] = "draft"
    planted["gates"]["deploy"] = "auto"
    queue_path.write_text(json.dumps(
        {"version": 1, "project": PROJECT, "tasks": [planted]}) + "\n")
    with pytest.raises(ApprovalError) as caught:
        approve_task(TaskQueue(queue_path), "WO-PL", approved_by="owner",
                     confirm=lambda _q: True, echo=lambda _t: None,
                     factory_root=tmp_path / "lroot")
    assert "WO-PL" in str(caught.value) and "deploy" in str(caught.value)

    # (f) the factory's own queue loads and passes the gate check for
    # every task; an owner_only task's spec_approval: false is never
    # judged by it.
    own = load_tasks(REPO / ".factory" / "tasks.json")
    assert own["tasks"]
    for task in own["tasks"]:
        check_v0_gates(task)
    check_v0_gates({"id": "X", "kind": "owner_only", "gates": {
        "spec_approval": False, "merge": "human", "deploy": "human"}})


# ----------------------------------------------------------------- AC-2 --


def test_ac_2(tmp_path: Path, monkeypatch):
    """AC-2: every import session is on record — the main session and
    each repair session write a provider-stamped, credential-free record
    under ``<factory-root>/import/`` on every exit path (accepted,
    refused, repaired), the provider is the PARENT's resolution (a
    forged session claim is overwritten), and ``factory status`` renders
    the import rows with unknown counts shown as unknown."""
    def _accept_drop(_drop) -> bool:
        return True

    repo = build_repo(tmp_path)
    good = _proposal(*_split_tasks(), dropped_criteria=[
        {"ref_task": "pilot-07", "doc": PILOT_07,
         "line": CAP_DONE_LINE, "reason": "shipped in release 2.4"},
    ])
    broken = _proposal(_pwo("p19a", priority="P0"))

    # (a) scripted import with one repair round: TWO records — the main
    # session (refused) and the repair (repaired) — each with provider,
    # model and usage; both name the kept proposal file.
    root = tmp_path / "froot-a"
    result = _run_real(repo, monkeypatch,
                       [_record(broken), _record(good, model="glm-4.7")],
                       factory_root=root, confirm=lambda s: True,
                       confirm_drop=_accept_drop)
    assert result.applied
    records = import_records(root)
    by_kind = {r["kind"]: r for r in records}
    assert set(by_kind) == {"import", "import_repair"}
    assert by_kind["import"]["outcome"] == "refused"
    assert by_kind["import_repair"]["outcome"] == "repaired"
    for record in records:
        assert record["provider"]["kind"] == "zai"
        assert record["model"] == "glm-4.7"
        assert record["usage"]["input"] == 1000
        assert record["turns"] == 5
        assert record["run_id"] and record["project"] == PROJECT
    assert by_kind["import"]["proposal_file"] \
        == by_kind["import_repair"]["proposal_file"]
    assert by_kind["import"]["proposal_file"]

    # (b) a session-written record claiming provider zai on a BEDROCK
    # profile is overwritten by the parent's resolution; the model stays
    # what the session reported (the profile's model lives in the
    # provider block).
    bedrock_repo = build_repo(tmp_path / "bedrock", bedrock=True)
    root_b = tmp_path / "froot-b"
    importer_fixture_approve(root_b, bedrock_repo)
    monkeypatch.setattr("factory.importer.run_session_sandboxed",
                        ScriptedRunner([
                            _record(good, provider={"kind": "zai",
                                                    "model": "liar"}),
                        ]))
    monkeypatch.setattr(
        "factory.provider._keychain_token",
        lambda service: "tok" if service == "factory-bedrock" else None)
    result = run_import(bedrock_repo, factory_root=root_b,
                        queue_path=tmp_path / "fresh-b.json",
                        confirm=lambda s: True, confirm_drop=_accept_drop)
    assert result.applied
    (record,) = import_records(root_b)
    assert record["provider"]["kind"] == "bedrock"
    assert record["provider"]["model"] == "model-imp"
    assert record["model"] == "glm-4.7"

    # (c) a refused import still writes its record — both sessions
    # refused, two records on the refusal path, beside the kept outputs.
    root_c = tmp_path / "froot-c"
    with pytest.raises(ImportRefused):
        _run_real(repo, monkeypatch, [_record(broken), _record(broken)],
                  factory_root=root_c)
    records = import_records(root_c)
    assert [r["outcome"] for r in records] == ["refused", "refused"]

    # (d) a record with no model and no usage: unknown / null, never a
    # bare null model; a record with PARTIAL usage renders its known
    # count, unknowns as unknown and the cost n/a (never a partial sum
    # priced as money); status renders the import rows under Providers.
    root_d = tmp_path / "froot-d"
    _run_real(repo, monkeypatch, [_record(good, model=None, usage=None)],
              factory_root=root_d, confirm=lambda s: True,
              confirm_drop=_accept_drop)
    root_d2 = tmp_path / "froot-d2"
    _run_real(repo, monkeypatch,
              [_record(good, model="glm-4.7",
                       usage={"input": 1000, "cache_read": None,
                              "cache_write": 50, "output": None})],
              factory_root=root_d2, confirm=lambda s: True,
              confirm_drop=_accept_drop)
    (record,) = import_records(root_d)
    assert record["model"] == "unknown"
    assert record["usage"] is None
    queue = load_tasks(tmp_path / "fresh-tasks.json")
    page = render_status(queue, project=PROJECT, factory_root=root_d,
                         repo=repo)
    providers_at = page.index("## Providers")
    import_rows = page[providers_at:]
    assert "Import sessions" in import_rows
    assert "unknown" in import_rows
    assert "glm" not in import_rows
    page2 = render_status(
        queue, project=PROJECT, factory_root=root_d2, repo=repo,
        config=FactoryConfig(prices={"zai": {"glm-4.7": {
            "input": 1.0, "cache_read": 0.1, "cache_write": 0.2,
            "output": 2.0}}}))
    partial_rows = page2[page2.index("## Providers"):]
    assert "1,000" in partial_rows and "unknown" in partial_rows
    # the partial-usage row's cost is n/a, never a partial sum in dollars
    for line in partial_rows.splitlines():
        if "1,000" in line:
            assert line.rstrip().endswith("n/a |"), line

    # (e) never a credential: the session's own reported model carrying
    # the seat token must not reach any record.
    root_e = tmp_path / "froot-e"
    _run_real(repo, monkeypatch,
              [_record(good, model=f"glm-4.7 {SEAT_TOKEN}")],
              factory_root=root_e, token=SEAT_TOKEN, confirm=lambda s: True,
              confirm_drop=_accept_drop)
    for path in (root_e / "import").glob("*.record.json"):
        assert SEAT_TOKEN not in path.read_text(encoding="utf-8")


# ----------------------------------------------------------------- AC-3 --


def test_ac_3(tmp_path: Path):
    """AC-3: possibly shipped work is flagged for the owner, never
    decided — valid entries render FIRST (before the overrides and the
    diff) and both tasks stay draft; a malformed entry or an unknown id
    refuses; attacker-controlled text renders escaped; a missing
    evidence path is flagged, not refused."""
    entry = {"task_id": "WO-1", "evidence": "src/app.py:42",
             "reason": "the named service already exists on main"}

    # (a) two valid entries render first; both tasks stay draft after
    # the apply — nothing changes a task's status because of the list.
    proposal = _proposal(
        _pwo("WO-1"), _pwo("WO-2"),
        possibly_shipped=[entry, {"task_id": "WO-2",
                                  "evidence": "commit a1b2c3d",
                                  "reason": "the migration landed"}])
    result = run_import(fresh_repo(tmp_path), session=_stub_session(proposal),
                        confirm=lambda s: True)
    assert result.applied
    summary = result.summary
    shipped_at = summary.index("## Possibly shipped")
    overrides_at = summary.index("## Gate overrides")
    diff_at = summary.index("## Full diff")
    assert shipped_at < overrides_at < diff_at
    assert "WO-1" in summary[shipped_at:overrides_at]
    assert "src/app.py:42" in summary[shipped_at:overrides_at]
    tasks = {t["id"]: t for t in load_tasks(result.queue_path)["tasks"]}
    assert tasks["WO-1"]["status"] == "draft"
    assert tasks["WO-2"]["status"] == "draft"

    # (b) an evidence path that does not exist is flagged, not refused.
    result = run_import(
        fresh_repo(tmp_path / "b"), session=_stub_session(_proposal(
            _pwo("WO-1"),
            possibly_shipped=[{"task_id": "WO-1",
                               "evidence": "src/missing.py:7",
                               "reason": "cannot verify"}])),
        confirm=lambda s: True)
    assert result.applied
    assert "evidence not found" in result.summary

    # (c) a malformed entry refuses; so does evidence escaping the repo.
    repo_c = fresh_repo(tmp_path / "c")
    for bad in ({"task_id": "WO-1", "evidence": "src/app.py"},  # no reason
                {"task_id": "WO-1", "evidence": "../outside.py:1",
                 "reason": "escapes the repo"},
                {"task_id": "WO-1", "evidence": "/etc/passwd",
                 "reason": "absolute path"}):
        with pytest.raises(ImportRefused) as caught:
            run_import(repo_c, session=_stub_session(
                _proposal(_pwo("WO-1"), possibly_shipped=[bad])),
                confirm=lambda s: True)
        assert "possibly_shipped" in str(caught.value)

    # (d) an unknown id refuses, naming it.
    with pytest.raises(ImportRefused) as caught:
        run_import(fresh_repo(tmp_path / "d"), session=_stub_session(
            _proposal(_pwo("WO-1"), possibly_shipped=[
                {"task_id": "WO-GHOST", "evidence": "src/app.py:1",
                 "reason": "not in the proposal"}])),
            confirm=lambda s: True)
    assert "WO-GHOST" in str(caught.value)

    # (e) a reason with ESC/CSI/bidi bytes renders escaped, never raw.
    # The egress seam strips control sequences from what run_import
    # carries; the render's own escaping is proven directly (a plan
    # built without the scrub), and the run's summary never carries the
    # raw bytes.
    hostile = {"task_id": "WO-1", "evidence": "src/app.py:1",
               "reason": "run \x1b[31mred\x1b[0m ‮prompt.exe"}
    plan = plan_import(
        None, json.loads(_proposal(_pwo("WO-1"), possibly_shipped=[hostile])),
        [], project=PROJECT)
    body = render_summary(plan)
    assert "\\x1b[31m" in body and "\\u202e" in body
    assert "\x1b" not in body and "‮" not in body
    result = run_import(fresh_repo(tmp_path / "e"), session=_stub_session(
        _proposal(_pwo("WO-1"), possibly_shipped=[hostile])),
        confirm=lambda s: True)
    assert result.applied
    assert "\x1b" not in result.summary and "‮" not in result.summary


# ----------------------------------------------------------------- AC-4 --


def test_ac_4(tmp_path: Path):
    """AC-4: criteria the docs still carry are never silently dropped —
    the reference is the repo's existing queue read as plain JSON (even
    schema-invalid, even under a fresh --queue), every reference task
    and criterion must be mapped (predecessors/from) or dropped against
    a real doc line, a drop still in the docs blocks the apply until the
    owner accepts it PER DROP, a done-backed drop applies, and a split
    is not a drop."""
    repo = build_repo(tmp_path)
    queue_path = tmp_path / "fresh-tasks.json"
    done_backed = [{"ref_task": "pilot-07", "doc": PILOT_07,
                    "line": CAP_DONE_LINE,
                    "reason": "the doc marks the work shipped"}]
    url_drop = {"ref_task": "pilot-19", "ref_ac": "AC-2", "doc": PILOT_19,
                "line": URL_LINE,
                "reason": "superseded by the trust boundary"}

    # (a) a fully-accounted import over the SCHEMA-INVALID reference and
    # a fresh --queue runs and applies: the split is NOT reported as a
    # drop, and EVERY drop needs the owner's per-drop acceptance (R2-1:
    # no exemption — the done line and the own-source doc are advisory
    # labels the owner decides with, never a bypass). Declined refuses
    # and writes nothing; accepted applies. The labels render.
    asked_a: list[str] = []

    def _ask_a(drop) -> bool:
        asked_a.append(drop.label)
        return True

    with pytest.raises(ImportRefused):
        run_import(repo, tmp_path / "fresh-aa.json",
                   session=_stub_session(_proposal(
                       *_split_tasks(), dropped_criteria=done_backed)),
                   confirm=lambda s: True,
                   confirm_drop=lambda _d: False)
    assert not (tmp_path / "fresh-aa.json").exists()

    result = run_import(repo, queue_path,
                        session=_stub_session(_proposal(
                            *_split_tasks(), dropped_criteria=done_backed)),
                        confirm=lambda s: True, confirm_drop=_ask_a)
    assert result.applied and queue_path.is_file()
    assert asked_a == ["pilot-07"]  # asked even though done-backed
    written = {t["id"]: t for t in load_tasks(queue_path)["tasks"]}
    assert set(written) == {"p19a", "p19b", "pilot-23"}
    assert written["pilot-23"]["gates"]["spec_approval"] is False
    dropped_at = result.summary.index("## Dropped criteria")
    dropped_section = result.summary[dropped_at:
                                     result.summary.index("## Rewritten")]
    assert "pilot-07" in dropped_section
    assert "cited line marks done" in dropped_section
    assert "cited doc is the task's own source" in dropped_section
    assert "pilot-19" not in dropped_section  # the split is not a drop
    for task in written.values():
        assert "predecessors" not in task
        for item in task["acceptance"]:
            assert "from" not in item

    # (b) an unaccounted criterion refuses, naming the reference task
    # and the AC — over the same schema-invalid reference and a fresh
    # --queue, proving the check ran (and never wrote).
    losing = [
        _pwo("p19a", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "trust boundary block present",
                             "pilot-19/AC-1")]),
        _pwo("p19b", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", LANGUAGE_CRITERION, "pilot-19/AC-3")]),
        _owner_carry(),
    ]
    with pytest.raises(ImportRefused) as caught:
        run_import(repo, tmp_path / "fresh-b.json",
                   session=_stub_session(_proposal(
                       *losing, dropped_criteria=done_backed)),
                   confirm=lambda s: True)
    message = str(caught.value)
    assert "pilot-19" in message and "AC-2" in message
    assert not (tmp_path / "fresh-b.json").exists()

    # (c) a drop still in the docs blocks apply until accepted, PER
    # DROP: two flagged drops — accepting only the first refuses and
    # writes nothing; accepting both applies.
    both_flagged = [url_drop, {"ref_task": "pilot-19", "ref_ac": "AC-3",
                               "doc": PILOT_19, "line": URL_LINE + 1,
                               "reason": "language flag superseded"}]
    partial = [
        _pwo("p19a", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "trust boundary block present",
                             "pilot-19/AC-1"),
                         _ac("AC-2", "a fresh criterion of the split")]),
        _pwo("p19b", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "another fresh criterion")]),
        _owner_carry(),
    ]
    blocked_queue = tmp_path / "blocked-tasks.json"
    asked: list[str] = []

    def _accept_only_first(drop) -> bool:
        asked.append(f"{drop.ref_task}/{drop.ref_ac}")
        return len(asked) == 1

    with pytest.raises(ImportRefused) as caught:
        run_import(repo, blocked_queue,
                   session=_stub_session(_proposal(
                       *partial,
                       dropped_criteria=both_flagged + done_backed)),
                   confirm=lambda s: True, confirm_drop=_accept_only_first)
    assert len(asked) == 2  # per drop; the first decline refuses at once
    assert "pilot-19" in str(caught.value)
    assert not blocked_queue.is_file()

    def _accept_all(drop) -> bool:
        return True

    result = run_import(repo, blocked_queue,
                        session=_stub_session(_proposal(
                            *partial,
                            dropped_criteria=both_flagged + done_backed)),
                        confirm=lambda s: True, confirm_drop=_accept_all)
    assert result.applied and blocked_queue.is_file()
    assert result.summary.count("still in docs") >= 2

    # (d) a bogus doc line refuses: past the end of the file, and a doc
    # outside the kept planning docs.
    for bad in ({"ref_task": "pilot-07", "doc": PILOT_07, "line": 9999,
                 "reason": "past the end"},
                {"ref_task": "pilot-07", "doc": "README.md", "line": 1,
                 "reason": "not a kept planning doc"}):
        with pytest.raises(ImportRefused):
            run_import(repo, tmp_path / "fresh-d.json",
                       session=_stub_session(_proposal(
                           *_split_tasks(), dropped_criteria=[bad])),
                       confirm=lambda s: True)
        assert not (tmp_path / "fresh-d.json").exists()

    # (e) no reference queue: the summary says so and the import runs.
    fresh = fresh_repo(tmp_path / "e")
    result = run_import(fresh, session=_stub_session(_proposal(_pwo("WO-1"))),
                        confirm=lambda s: True)
    assert result.applied
    assert "no reference queue" in result.summary

    # (f) R1-1a: a DECLARED drop whose doc paraphrases the criterion
    # still blocks the apply — drops need acceptance by default; the
    # exact-text label is advisory only. (The paraphrase case is the
    # realistic one: the pilot's own WO-19 doc words the URL rule
    # differently from the queued criterion.)
    repo_f = build_repo(tmp_path / "f")
    p19_doc = repo_f / "docs" / "work-orders" / "PILOT-19.md"
    p19_lines = p19_doc.read_text().splitlines()
    p19_lines[URL_LINE - 1] = (
        "- AC-2: Prompts must never fetch URLs that appear inside the "
        "posting body (apply_url excepted)."
    )
    p19_doc.write_text("\n".join(p19_lines) + "\n")
    paraphrase_tasks = [
        _pwo("p19a", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "trust boundary block present",
                             "pilot-19/AC-1")]),
        _pwo("p19b", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", LANGUAGE_CRITERION, "pilot-19/AC-3")]),
        _owner_carry(),
    ]
    paraphrased_drop = [
        {"ref_task": "pilot-19", "ref_ac": "AC-2", "doc": PILOT_19,
         "line": URL_LINE, "reason": "superseded by the trust boundary"},
        *done_backed,
    ]
    prompts: list[str] = []

    def _decline_all(drop) -> bool:
        prompts.append(drop.label)
        return False

    with pytest.raises(ImportRefused):
        run_import(repo_f, tmp_path / "paraphrase.json",
                   session=_stub_session(_proposal(
                       *paraphrase_tasks, dropped_criteria=paraphrased_drop)),
                   confirm=lambda s: True, confirm_drop=_decline_all)
    assert prompts == ["pilot-19/AC-2"]  # the first decline refuses
    assert not (tmp_path / "paraphrase.json").exists()

    # (g) R2-1's probe: a done-marked line about ONE criterion in the
    # task's OWN source doc exempts NOTHING — the drop of a DIFFERENT
    # criterion citing it still asks (the session chooses which line to
    # cite; only the owner decides).
    repo_g = build_repo(tmp_path / "g")
    p19_g = repo_g / "docs" / "work-orders" / PILOT_19.split("/")[-1]
    lines_g = p19_g.read_text().splitlines()
    lines_g.append("- AC-1 trust boundary block: [x] done, shipped")
    p19_g.write_text("\n".join(lines_g) + "\n")
    done_line = len(lines_g)
    other_done_drop = [{
        "ref_task": "pilot-19", "ref_ac": "AC-2", "doc": PILOT_19,
        "line": done_line,
        "reason": "cites a done line about a different criterion",
    }] + done_backed
    asked_g: list[str] = []

    def _ask_g(drop) -> bool:
        asked_g.append(drop.label)
        return False

    with pytest.raises(ImportRefused):
        run_import(repo_g, tmp_path / "otherdone.json",
                   session=_stub_session(_proposal(
                       *paraphrase_tasks, dropped_criteria=other_done_drop)),
                   confirm=lambda s: True, confirm_drop=_ask_g)
    assert asked_g == ["pilot-19/AC-2"]
    assert not (tmp_path / "otherdone.json").exists()
    # the summary shows the done label as ADVISORY evidence, in view
    result_g = run_import(repo_g, tmp_path / "otherdone-yes.json",
                          session=_stub_session(_proposal(
                              *paraphrase_tasks,
                              dropped_criteria=other_done_drop)),
                          confirm=lambda s: True,
                          confirm_drop=lambda _d: True)
    assert result_g.applied
    assert "cited line marks done" in result_g.summary

    # (h) R1-1b: a mapping that rewrites a criterion is SHOWN — the
    # "Rewritten criteria" section, after the drops and before the diff,
    # carries the reference text and the new text side by side (also for
    # the same-id carry). R2-2: a mapping that keeps the text but
    # WEAKENS THE CHECK (a test-backed criterion turned manual) is shown
    # with what changed. Visible, not blocking.
    bogus = [
        _pwo("p19a", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "trust boundary block present",
                             "pilot-19/AC-1"),
                         _ac("AC-2", "Fix the footer typo",
                             "pilot-19/AC-2")]),
        _pwo("p19b", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", LANGUAGE_CRITERION, "pilot-19/AC-3")]),
        _pwo("pilot-23", kind="owner_only",
             gates={"spec_approval": False, "merge": "human",
                    "deploy": "human"},
             scope={"paths": ["docs/**"]},
             acceptance=[_ac("AC-1", "The countersigned DPA is archived")]),
    ]
    result = run_import(repo, tmp_path / "rewritten.json",
                        session=_stub_session(_proposal(
                            *bogus, dropped_criteria=done_backed)),
                        confirm=lambda s: True, confirm_drop=_accept_all)
    assert result.applied
    summary = result.summary
    rewritten_at = summary.index("## Rewritten criteria")
    diff_at = summary.index("## Full diff")
    dropped_at2 = summary.index("## Dropped criteria")
    assert dropped_at2 < rewritten_at < diff_at
    section = summary[rewritten_at:diff_at]
    assert "pilot-19/AC-2" in section
    assert "posting bodies" in section and "footer typo" in section
    assert "pilot-23/AC-1" in section  # the same-id carry, changed text
    assert "data processing agreement" in section
    assert "countersigned DPA" in section

    # (i) R2-2: same text, weaker check — a test-backed criterion
    # mapped with check {"type": "manual", ...} is listed with the check
    # change named, and applies (visible, never blocking).
    weakened = [
        _pwo("p19a", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "trust boundary block present",
                             "pilot-19/AC-1"),
                         _ac("AC-2", URL_CRITERION, "pilot-19/AC-2",
                             check={"type": "manual",
                                    "expect": "looks fine"})]),
        _pwo("p19b", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", LANGUAGE_CRITERION, "pilot-19/AC-3")]),
        _owner_carry(),
    ]
    result = run_import(repo, tmp_path / "weakened.json",
                        session=_stub_session(_proposal(
                            *weakened, dropped_criteria=done_backed)),
                        confirm=lambda s: True, confirm_drop=_accept_all)
    assert result.applied
    section = result.summary[
        result.summary.index("## Rewritten criteria"):
        result.summary.index("## Full diff")]
    assert "pilot-19/AC-2" in section
    assert "check" in section
    assert "manual" in section and "looks fine" in section

    # (j) F-1: the quiet swap — a test check re-pointed at a DIFFERENT
    # test keeps the criterion looking test-backed while removing what
    # it actually verified. Same text, same type, new ref: listed as
    # [check] with BOTH refs shown.
    repointed = [
        _pwo("p19a", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", "trust boundary block present",
                             "pilot-19/AC-1"),
                         _ac("AC-2", URL_CRITERION, "pilot-19/AC-2",
                             check={"type": "test",
                                    "ref": "backend/tests/test_units.py"
                                           "::test_smoke"})]),
        _pwo("p19b", predecessors=["pilot-19"],
             acceptance=[_ac("AC-1", LANGUAGE_CRITERION, "pilot-19/AC-3")]),
        _owner_carry(),
    ]
    result = run_import(repo, tmp_path / "repointed.json",
                        session=_stub_session(_proposal(
                            *repointed, dropped_criteria=done_backed)),
                        confirm=lambda s: True, confirm_drop=_accept_all)
    assert result.applied
    section = result.summary[
        result.summary.index("## Rewritten criteria"):
        result.summary.index("## Full diff")]
    assert "pilot-19/AC-2" in section
    assert "[check]" in section
    assert _ref_check("pilot-19", 2)["ref"] in section  # the lost ref
    assert "backend/tests/test_units.py::test_smoke" in section
