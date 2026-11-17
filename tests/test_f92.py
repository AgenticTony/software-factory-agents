"""F-92 — Every park has an owner exit, and a failed escalation PR keeps
the real question.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f92.py::test_ac_N``
means a test function with exactly that name.

Four acceptance criteria (BUILD-ORDER §F-92):

- AC-1 the escalation label exists before ``gh pr create --label`` runs:
  ``gh label create`` (never ``--force``) through ``remote_call``, "already
  exists" is success, a failed create lands ``pr_create_failed`` naming
  the label step, and the PR always carries the label.
- AC-2 a failed escalation PR records ``original_escalation`` and
  ``links.branch``; every recovery predicate reads the park class through
  ONE helper (original first); status and the loop's parked line show
  both classes; ``factory status <task>`` prints the parked attempt's
  note, escaped and capped.
- AC-3 ``factory recover <id> --redraft --evidence`` moves ANY
  ``needs_human`` park holding no PR link to ``draft`` (markers cleared,
  attempts kept, progress-log entry), interactive only; a PR-holding park
  is refused with its real exit named. Table-driven over the schema enum.
- AC-4 every backticked ``factory …`` command in a string literal under
  ``factory/`` names a real subcommand and flag of the CLI parser.

Real vs stubbed: git, the clone manager, the queue, the L0/L1 sandboxes,
the push to the local bare remote, ``remote_call`` and ``gh_pr_create``
are real; the model seats are scripted and ``gh`` is a stub executable
on disk (no network).
"""

from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
import json
import re
import stat
import sys
from pathlib import Path

import pytest
from _support.f63 import ledger_approve
from _support.f72 import IMPL_GOOD, TEST_SOURCE, Env72, _task

import factory.publish as publish
import factory.state as state_mod
from factory.approve import ApprovalError, approve_task, recover_task
from factory.cli import build_parser, main
from factory.contract import ContractError
from factory.orchestrator import ESCALATION_PR_LABEL
from factory.progress import ProgressLog
from factory.publish import PublishError, PullRequest, gh_pr_create
from factory.session import SessionRecord
from factory.state import TaskQueue, pick_next
from factory.status import render_status, render_task_status

FACTORY_PKG = Path(__file__).resolve().parents[1] / "factory"
SCHEMA = FACTORY_PKG / "data" / "schema" / "tasks.schema.json"
SLUG_REMOTE = "https://github.com/fixture/fixture.git"
EVIDENCE = "read the attempt note; the contract needs rework"

# gh 2.104's own text for a label the repo already carries
# (pkg/cmd/label/create.go: "label with name %q already exists; use
# `--force` to update its color and description").
GH_ALREADY_EXISTS = (
    'label with name "{name}" already exists; use `--force` to update '
    "its color and description"
)


# --------------------------------------------------------------------------
# A gh stub on disk: logs argv, answers per mode (label create / pr create)
# --------------------------------------------------------------------------


def _gh_stub(tmp: Path, label_mode: str) -> tuple[Path, Path]:
    """A ``gh`` executable that appends its argv (JSON) to a log.

    ``label_mode``: ``missing`` (create succeeds), ``present`` (gh's
    already-exists refusal), ``present-case`` (the same refusal for a
    label that differs only in case — GitHub treats names
    case-insensitively), ``refused`` (HTTP 403), ``hang`` (sleeps past
    any timeout).
    """
    log = tmp / "gh-calls.jsonl"
    stub = tmp / "gh"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"log = {str(log)!r}\n"
        f"mode = {label_mode!r}\n"
        "argv = sys.argv[1:]\n"
        "with open(log, 'a') as fh:\n"
        "    fh.write(json.dumps(argv) + '\\n')\n"
        "if argv[:2] == ['label', 'create']:\n"
        "    name = argv[2]\n"
        "    if mode == 'missing':\n"
        "        print('created'); sys.exit(0)\n"
        "    if mode == 'present':\n"
        f"        sys.stderr.write({GH_ALREADY_EXISTS!r}.format(name=name) + '\\n')\n"
        "        sys.exit(1)\n"
        "    if mode == 'present-case':\n"
        f"        sys.stderr.write({GH_ALREADY_EXISTS!r}.format(name=name.upper()) + '\\n')\n"
        "        sys.exit(1)\n"
        "    if mode == 'hang':\n"
        "        time.sleep(30); sys.exit(0)\n"
        "    sys.stderr.write('HTTP 403: Resource not accessible by integration\\n')\n"
        "    sys.exit(1)\n"
        "if argv[:2] == ['pr', 'create']:\n"
        "    print('https://github.com/fixture/fixture/pull/7'); sys.exit(0)\n"
        "sys.stderr.write('unexpected gh call\\n'); sys.exit(2)\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, log


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def _escalation_pr(label: str | None = ESCALATION_PR_LABEL) -> PullRequest:
    return PullRequest(remote=SLUG_REMOTE, base="main", head="factory/t-1-x",
                       title="[escalation] T-1: t", body="body", draft=True,
                       label=label)


# --------------------------------------------------------------------------
# AC-1 — the escalation label exists before the PR is created
# --------------------------------------------------------------------------


def test_ac_1(tmp_path: Path, monkeypatch):
    # (a) label missing → created (no --force), THEN the PR opens with it.
    (tmp_path / "missing").mkdir()
    stub, log = _gh_stub(tmp_path / "missing", "missing")
    url = gh_pr_create(_escalation_pr(), gh_bin=stub)
    assert url == "https://github.com/fixture/fixture/pull/7"
    calls = _calls(log)
    assert [c[:2] for c in calls] == [["label", "create"], ["pr", "create"]]
    label_call, pr_call = calls
    assert label_call[2] == ESCALATION_PR_LABEL
    assert "--force" not in label_call, "an owner's label is never overwritten"
    assert label_call[label_call.index("--repo") + 1] == "fixture/fixture"
    assert "--description" in label_call and "--color" in label_call
    assert pr_call[pr_call.index("--label") + 1] == ESCALATION_PR_LABEL
    assert "--draft" in pr_call

    # (b) label present → gh's already-exists refusal is success; no
    # overwrite, the PR still carries the label. Also when the existing
    # label differs only in case (GitHub names are case-insensitive).
    for mode in ("present", "present-case"):
        (tmp_path / mode).mkdir()
        stub, log = _gh_stub(tmp_path / mode, mode)
        assert gh_pr_create(_escalation_pr(), gh_bin=stub).endswith("/pull/7")
        calls = _calls(log)
        assert [c[:2] for c in calls] == [["label", "create"], ["pr", "create"]]
        assert "--force" not in calls[0]
        assert calls[1][calls[1].index("--label") + 1] == ESCALATION_PR_LABEL

    # (c) create refused → PublishError naming the label step; no PR.
    (tmp_path / "refused").mkdir()
    stub, log = _gh_stub(tmp_path / "refused", "refused")
    with pytest.raises(PublishError, match=r"label") as info:
        gh_pr_create(_escalation_pr(), gh_bin=stub)
    assert ESCALATION_PR_LABEL in str(info.value)
    assert "403" in str(info.value)
    assert [c[:2] for c in _calls(log)] == [["label", "create"]]

    # (d) a hung create is bounded by remote_call's timeout — a
    # PublishError (RemoteCallTimeout) naming the label step; no PR.
    (tmp_path / "hang").mkdir()
    stub, log = _gh_stub(tmp_path / "hang", "hang")
    monkeypatch.setattr(publish, "NET_TIMEOUT_SEC", 1)
    with pytest.raises(PublishError, match=r"label create.*timed out"):
        gh_pr_create(_escalation_pr(), gh_bin=stub)
    assert [c[:2] for c in _calls(log)] == [["label", "create"]]
    monkeypatch.undo()

    # (e) the validated path (no label) makes no label call at all.
    (tmp_path / "plain").mkdir()
    stub, log = _gh_stub(tmp_path / "plain", "refused")
    gh_pr_create(dataclasses.replace(_escalation_pr(label=None), draft=False),
                 gh_bin=stub)
    assert [c[:2] for c in _calls(log)] == [["pr", "create"]]

    # (f) end to end: a real escalation whose label create is refused
    # lands pr_create_failed, the reason names the label step, and the
    # PR seam never ran `gh pr create`.
    (tmp_path / "e2e").mkdir()
    stub, log = _gh_stub(tmp_path / "e2e", "refused")
    env = Env72(tmp_path / "e2e" / "env", tasks=[_task(scope=_SCOPE)])

    def open_pr(pr: PullRequest) -> str:
        return gh_pr_create(dataclasses.replace(pr, remote=SLUG_REMOTE),
                            gh_bin=stub)

    from _support.f72 import OFF_PEAK

    from factory.mergeorder import Protection
    from factory.orchestrator import run_once

    ledger_approve(env.root, env.queue_path)
    result = run_once(env.queue_path, factory_root=env.root,
                      session_runner=_Requesting(), open_pr=open_pr,
                      now=lambda: OFF_PEAK,
                      protection=lambda r, b: Protection(checked=True,
                                                         strict=True),
                      pr_state=lambda url: "open")
    assert result.outcome == "needs_human", result
    assert result.escalation == "pr_create_failed"
    note = env.task()["attempts"][-1]["note"]
    assert "label" in note and ESCALATION_PR_LABEL in note
    assert [c[:2] for c in _calls(log)] == [["label", "create"]]


# --------------------------------------------------------------------------
# AC-2 — a failed escalation PR keeps the question and the branch
# --------------------------------------------------------------------------

_SCOPE = {"paths": ["impl.py", "tests/test_feature.py"]}
_WIDENED = {"paths": ["impl.py", "tests/test_feature.py", "shared.py"]}


_FAIL = json.dumps({"verdict": "fail", "findings": [{
    "id": "R-1", "axis": "correctness", "severity": "major",
    "file": "impl.py", "text": "the shared fixture is duplicated here"}]})


class _Requesting:
    """The run-3 shape: round 1 writes green work (committed on the run
    branch) and its review fails; round 2's implementer files a scope
    request — ``scope_change_needed`` with work to keep."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        self.calls.append(seat)
        if seat != "implementer":
            return SessionRecord(seat=seat, status="completed", num_turns=1,
                                 model="stub", tokens={}, result=_FAIL)
        run_dir = Path(run_dir)
        (run_dir / "impl.py").write_text(IMPL_GOOD)
        (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
        record = SessionRecord(
            seat=seat, status="completed", num_turns=3, model="stub",
            tokens={"input_tokens": 10, "output_tokens": 5},
            result="Needs a wider scope.")
        if self.calls.count("implementer") >= 2:
            record.scope_requests = [{
                "path": "shared.py",
                "reason": "the shared fixture lives there"}]
        return record


def _parked_scope_behind_failed_pr(tmp: Path) -> Env72:
    env = Env72(tmp, tasks=[_task(scope=dict(_SCOPE))])
    env.open_pr_error = PublishError(
        f"gh label create {ESCALATION_PR_LABEL} failed: HTTP 403")
    result = env.run(_Requesting())
    assert result.outcome == "needs_human", result
    assert result.escalation == "pr_create_failed"
    return env


def _bare_park(escalation: str, **attempt_extra) -> dict:
    task = _task(status="needs_human")
    task["attempts"] = [{
        "run_id": "20261117T000000Z-f92f92", "slice_id": "T-1",
        "started": "2026-11-17T00:00:00Z", "ended": "2026-11-17T00:00:00Z",
        "outcome": "escalated", "rounds": 1, "turns": 0,
        "input_tokens": 0, "output_tokens": 0,
        "prompt_versions": {}, "skills": [],
        "escalation": escalation, "note": "parked for the test",
        **attempt_extra,
    }]
    return task


_PREDICATES = (
    "recovery_task_eligible", "credential_recovery_eligible",
    "merged_recovery_eligible", "escalation_accept_eligible",
    "scope_recovery_eligible", "attempts_reset_eligible",
    "keep_pr_eligible",
)


def test_ac_2(tmp_path: Path, capsys):
    env = _parked_scope_behind_failed_pr(tmp_path / "run")
    task = env.task()
    attempt = task["attempts"][-1]
    # Both classes recorded: the honest one (F-72 AC-3, unchanged) and
    # the question the run escalated on.
    assert attempt["escalation"] == "pr_create_failed"
    assert attempt["original_escalation"] == "scope_change_needed"
    assert "label create" in attempt["note"]
    assert "shared.py" in attempt["note"]
    # The pushed branch is on the task; no PR link was invented.
    links = task.get("links") or {}
    assert "pr" not in links
    assert links.get("branch") in env.remote_branches()

    # The one helper: original first, else escalation.
    assert state_mod.park_class(task) == "scope_change_needed"
    assert state_mod.park_class(_bare_park("budget_exhausted")) \
        == "budget_exhausted"

    # Every recovery predicate reads the class through it: a class behind
    # a failed PR answers exactly as the plain class does.
    enum = json.loads(SCHEMA.read_text())["$defs"]["escalation"]["enum"]
    for original in enum:
        for links_shape in ({}, {"pr": "https://github.com/f/f/pull/1",
                                 "branch": "factory/T-1-x"},
                            {"pr": "https://github.com/f/f/pull/1",
                             "escalation_pr": True}):
            plain = _bare_park(original)
            behind = _bare_park("pr_create_failed",
                                original_escalation=original)
            plain["links"] = dict(links_shape)
            behind["links"] = dict(links_shape)
            for name in _PREDICATES:
                predicate = getattr(state_mod, name)
                assert predicate(behind) == predicate(plain), (
                    name, original, links_shape)

    # Status shows both classes and the parked attempt's note.
    doc = json.loads(env.queue_path.read_text())
    page = render_task_status(doc, "T-1", project="fixture")
    assert "pr_create_failed (from scope_change_needed)" in page
    assert "shared.py" in page  # the note: the owner's next step
    summary = render_status(doc, project="fixture", factory_root=env.root)
    assert "pr_create_failed (from scope_change_needed)" in summary

    # The note is escaped and capped like other session text.
    hostile = copy.deepcopy(doc)
    hostile_attempt = hostile["tasks"][0]["attempts"][-1]
    hostile_attempt["note"] = "\x1b[2Jcleared\u202e" + "x" * 5000
    page = render_task_status(hostile, "T-1", project="fixture")
    assert "\x1b" not in page and "\u202e" not in page
    assert "\\x1b" in page and "\\u202e" in page
    assert "x" * 2100 not in page

    # The loop's parked line shows both classes.
    env2 = Env72(tmp_path / "loop", tasks=[_task(scope=dict(_SCOPE))])
    ledger_approve(env2.root, env2.queue_path)
    env2.open_pr_error = PublishError("gh label create failed")
    from _support.f72 import OFF_PEAK

    import factory.cli as cli_mod
    import factory.orchestrator as orch
    from factory.mergeorder import Protection

    def loop(queue_path, profile_path=None, **kwargs):
        return orch.run_loop(
            queue_path, profile_path, session_runner=_Requesting(),
            open_pr=env2.open_pr, now=lambda: OFF_PEAK,
            protection=lambda r, b: Protection(checked=True, strict=True),
            pr_state=lambda url: "open", factory_root=env2.root)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(cli_mod, "run_loop", loop)
        code = main(["loop", "--queue", str(env2.queue_path),
                     "--factory-root", str(env2.root)])
    finally:
        mp.undo()
    out = capsys.readouterr().out
    assert code == 1
    assert "parked: T-1 (pr_create_failed (from scope_change_needed))" in out

    # The recovery: widen the scope, re-approve the widened contract
    # (the ledger), then `recover --evidence` re-queues it — exactly as a
    # plain scope_change_needed park. Before the re-approval: refused.
    raw = json.loads(env.queue_path.read_text())
    raw["tasks"][0]["scope"] = dict(_WIDENED)
    env.queue_path.write_text(json.dumps(raw, indent=2) + "\n")
    # (the writer's own ledger refusal — the CLI exits 1 on it)
    with pytest.raises((ApprovalError, ContractError),
                       match="has no owner approval in the ledger"):
        recover_task(TaskQueue(env.queue_path), "T-1", evidence=EVIDENCE,
                     factory_root=env.root, confirm=lambda q: True,
                     echo=lambda text: None)
    assert env.task()["status"] == "needs_human"
    ledger_approve(env.root, env.queue_path)
    status = recover_task(TaskQueue(env.queue_path), "T-1",
                          evidence=EVIDENCE, factory_root=env.root,
                          confirm=lambda q: True, echo=lambda text: None)
    assert status == "ready"
    assert env.task()["scope"] == _WIDENED


# --------------------------------------------------------------------------
# AC-3 — one exit for every other park: back to draft
# --------------------------------------------------------------------------


def _queue_with(tmp: Path, task: dict) -> TaskQueue:
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "tasks.json"
    path.write_text(json.dumps({"version": 1, "project": "fixture",
                                "tasks": [task]}, indent=2) + "\n")
    return TaskQueue(path)


def _log(root: Path) -> list:
    return ProgressLog.for_task(root, "fixture", "T-1").entries()


# Every `factory recover` form the table tries, in order.
_FORMS = (
    {"evidence": EVIDENCE, "reset_attempts": True},
    {"evidence": EVIDENCE},
    {"evidence": EVIDENCE, "redraft": True},
)


def test_ac_3(tmp_path: Path):
    enum = json.loads(SCHEMA.read_text())["$defs"]["escalation"]["enum"]
    assert len(enum) >= 21
    moved_by: dict[str, list] = {}
    # Table: for EVERY class in the schema enum, a needs_human park with
    # no PR link has at least one `factory recover` form that moves it —
    # and --redraft moves every one of them to draft. A class added to
    # the enum without an exit fails here.
    for cls in enum:
        for shape in ("plain", "behind_failed_pr"):
            extra = ({"original_escalation": cls} if shape != "plain" else {})
            esc = cls if shape == "plain" else "pr_create_failed"
            moved_by[f"{cls}/{shape}"] = []
            for n, form in enumerate(_FORMS):
                root = tmp_path / f"{cls}-{shape}-{n}"
                queue = _queue_with(root, _bare_park(esc, **extra))
                try:
                    status = recover_task(
                        queue, "T-1", factory_root=root,
                        confirm=lambda q: True, echo=lambda text: None,
                        **form)
                except (ApprovalError, ContractError):
                    continue  # refused (the CLI exits 1 on both)
                moved_by[f"{cls}/{shape}"].append((n, status))
    dead = sorted(key for key, moves in moved_by.items() if not moves)
    assert dead == [], f"parks with no exit: {dead}"
    redraft = len(_FORMS) - 1
    not_redrafted = sorted(
        key for key, moves in moved_by.items()
        if (redraft, "draft") not in moves)
    assert not_redrafted == [], not_redrafted

    # The redraft writer: markers cleared exactly as reject_escalation
    # does, attempts kept byte-for-byte, a progress-log entry written,
    # and the evidence recorded.
    root = tmp_path / "writer"
    park = _bare_park("budget_exhausted")
    park["attempt_resets"] = []
    queue = _queue_with(root, park)
    before = copy.deepcopy(queue.get("T-1"))
    prompts: list[str] = []
    status = recover_task(queue, "T-1", redraft=True, evidence=EVIDENCE,
                          factory_root=root, done_by="owner",
                          confirm=lambda q: prompts.append(q) or True,
                          echo=lambda text: None)
    assert status == "draft"
    assert prompts, "the redraft is interactive"
    after = TaskQueue(queue.path).get("T-1")
    assert after["status"] == "draft"
    for marker in ("acceptance_hash", "approved_at", "approved_by"):
        assert marker not in after, marker
    assert after["attempts"] == before["attempts"]
    entry = _log(root)[-1]
    assert entry["outcome"] == "recovered"
    assert entry["escalation"] == "budget_exhausted"
    assert entry["evidence"] == EVIDENCE
    assert entry["done_by"] == "owner"
    assert "draft" in entry["note"]

    # Interactive only, evidence required, a decline writes nothing.
    for kwargs, pattern in (
            ({"assume_yes": True, "evidence": EVIDENCE}, "--yes"),
            ({}, "--evidence"),
            ({"evidence": "   "}, "--evidence")):
        root = tmp_path / f"refuse-{pattern.strip('-')}-{len(kwargs)}"
        queue = _queue_with(root, _bare_park("orchestrator_error"))
        snapshot = queue.path.read_text()
        with pytest.raises(ApprovalError, match=pattern):
            recover_task(queue, "T-1", redraft=True, factory_root=root,
                         confirm=lambda q: True, echo=lambda text: None,
                         **kwargs)
        assert queue.path.read_text() == snapshot
    from factory.approve import ApprovalDeclined

    root = tmp_path / "declined"
    queue = _queue_with(root, _bare_park("orchestrator_error"))
    snapshot = queue.path.read_text()
    with pytest.raises(ApprovalDeclined):
        recover_task(queue, "T-1", redraft=True, evidence=EVIDENCE,
                     factory_root=root, confirm=lambda q: False,
                     echo=lambda text: None)
    assert queue.path.read_text() == snapshot
    assert _log(root) == []

    # A PR-holding park is refused, its real exit named; nothing written.
    pr = "https://github.com/fixture/fixture/pull/3"
    for links, pattern in (
            ({"pr": pr, "branch": "factory/T-1-x"}, "--reset-attempts"),
            ({"pr": pr, "branch": "factory/T-1-x", "escalation_pr": True},
             "--reject"),
            ({"pr": pr, "branch": "factory/T-1-x", "merged": pr},
             "merged")):
        park = _bare_park("review_not_converging")
        park["links"] = links
        root = tmp_path / f"pr-{pattern.strip('-')}"
        queue = _queue_with(root, park)
        snapshot = queue.path.read_text()
        with pytest.raises(ApprovalError, match=pattern):
            recover_task(queue, "T-1", redraft=True, evidence=EVIDENCE,
                         factory_root=root, confirm=lambda q: True,
                         echo=lambda text: None)
        assert queue.path.read_text() == snapshot

    # R1-1: a drafted task runs FRESH — the old run's links go with the
    # draft (a later crash window must never pair a new PR with the old
    # branch or the stale escalation marker), and the progress entry
    # names what was dropped so the kept work stays findable. Contract
    # links (execution_record, step) and merged stay.
    run_scoped = ("branch", "pushed_head", "wip", "revalidated",
                  "ci_rerun_heads")
    kept_links = {"execution_record": "docs/T-1.md#record", "step": "4"}
    old_pr = "https://github.com/fixture/fixture/pull/9"
    for how in ("redraft", "reject"):
        park = _bare_park("budget_exhausted")
        park["links"] = {**kept_links, "branch": "factory/t-1-old",
                         "pushed_head": "a" * 40}
        if how == "reject":
            park["links"].update({
                "pr": old_pr, "escalation_pr": True,
                "revalidated": {"pr": "a" * 40, "default": "b" * 40},
                "ci_rerun_heads": ["a" * 40]})
        root = tmp_path / f"links-{how}"
        queue = _queue_with(root, park)
        echoed: list[str] = []
        recover_task(queue, "T-1", factory_root=root, pr_close=lambda u: None,
                     confirm=lambda q: True, echo=echoed.append,
                     **({"redraft": True, "evidence": EVIDENCE}
                        if how == "redraft" else {"reject": True}))
        links = TaskQueue(queue.path).get("T-1").get("links") or {}
        assert not [k for k in run_scoped if k in links], (how, links)
        # A reject keeps the closed escalation PR's pair (F-72 AC-4 pins
        # links.pr on the rejected task — tests/test_f72.py, protected).
        expected = dict(kept_links)
        if how == "reject":
            expected.update({"pr": old_pr, "escalation_pr": True})
        assert links == expected, (how, links)
        entry = _log(root)[-1]
        assert entry["branch"] == "factory/t-1-old", how
        assert "factory/t-1-old" in "\n".join(echoed), how
        if how == "reject":
            assert entry["pr"] == old_pr

    # …and the crash window that marker could poison: a fresh run's PASS
    # attempt saved, its links not — the completion links the new PR and
    # clears the stale escalation marker, so the sweep owns the new PR.
    from factory.orchestrator import _finish_unrecorded_publish

    new_pr = "https://github.com/fixture/fixture/pull/10"
    task = _task(status="in_progress")  # begin_run moved it (run_once)
    task["links"] = {"pr": old_pr, "escalation_pr": True}
    task["attempts"] = [{**_bare_park("x")["attempts"][0],
                         "outcome": "pass", "pr": new_pr}]
    del task["attempts"][0]["escalation"]
    queue = _queue_with(tmp_path / "crash-window", task)
    result = _finish_unrecorded_publish(queue, "T-1", None)
    assert result is not None and result.outcome == "pr_opened"
    links = TaskQueue(queue.path).get("T-1")["links"]
    assert links == {"pr": new_pr}, links

    # The writer's own gate (not only the command's courtesy): a direct
    # TaskQueue.redraft caller cannot draft a PR-holding park, and blank
    # evidence refuses.
    from factory.state import StateError, TransitionError

    for links in ({"pr": pr}, {"pr": pr, "escalation_pr": True},
                  {"pr": pr, "merged": pr}):
        park = _bare_park("budget_exhausted")
        park["links"] = links
        queue = _queue_with(tmp_path / f"writer-{len(links)}", park)
        snapshot = queue.path.read_text()
        with pytest.raises(TransitionError, match="no PR link"):
            queue.redraft("T-1", evidence=EVIDENCE)
        assert queue.path.read_text() == snapshot
    queue = _queue_with(tmp_path / "writer-blank", _bare_park("setup_failed"))
    with pytest.raises(StateError, match="evidence"):
        queue.redraft("T-1", evidence="  ")

    # An escalation draft PR of ANY class has an exit: --reject (closes
    # the PR, task → draft), not only new_dependency/risk_path_touched.
    for cls in ("review_not_converging", "budget_exhausted",
                "new_dependency"):
        park = _bare_park(cls)
        park["links"] = {"pr": pr, "branch": "factory/T-1-x",
                         "escalation_pr": True}
        root = tmp_path / f"reject-{cls}"
        queue = _queue_with(root, park)
        closed: list[str] = []
        status = recover_task(queue, "T-1", reject=True, factory_root=root,
                              pr_close=closed.append,
                              confirm=lambda q: True, echo=lambda text: None)
        assert status == "draft" and closed == [pr], cls

    # The not-recoverable refusal names --redraft for no-PR parks.
    park = _bare_park("review_not_converging")
    park["links"] = {"pr": pr, "escalation_pr": True}
    queue = _queue_with(tmp_path / "refusal-text", park)
    with pytest.raises(ApprovalError, match="--redraft"):
        recover_task(queue, "T-1", factory_root=tmp_path / "refusal-text",
                     confirm=lambda q: True, echo=lambda text: None)

    # The CLI: --redraft exists, and --yes with it is refused (exit 1).
    root = tmp_path / "cli"
    queue = _queue_with(root, _bare_park("setup_failed"))
    snapshot = queue.path.read_text()
    code = main(["recover", "T-1", "--redraft", "--evidence", EVIDENCE,
                 "--yes", "--queue", str(queue.path),
                 "--factory-root", str(root)])
    assert code == 1
    assert queue.path.read_text() == snapshot

    # Gate: a redrafted task runs again only through `factory approve`.
    env = Env72(tmp_path / "gate", tasks=[_task(
        scope=dict(_SCOPE),
        budget={"size": "S", "max_attempts": 3, "max_turns": 80,
                "wall_clock_min": 60})])
    env.open_pr_error = PublishError("gh label create failed")
    env.run(_Requesting())
    assert env.task()["status"] == "needs_human"
    status = recover_task(TaskQueue(env.queue_path), "T-1", redraft=True,
                          evidence=EVIDENCE, factory_root=env.root,
                          confirm=lambda q: True, echo=lambda text: None)
    assert status == "draft"
    assert pick_next(TaskQueue(env.queue_path).doc["tasks"]) is None
    seats = _Requesting()
    result = env.run(seats)
    assert result.outcome == "idle", result
    assert seats.calls == [], "a redrafted task ran without re-approval"
    assert env.task()["status"] == "draft"
    status = approve_task(TaskQueue(env.queue_path), "T-1",
                          approved_by="owner", factory_root=env.root,
                          confirm=lambda q: True, echo=lambda text: None)
    assert status == "ready"


# --------------------------------------------------------------------------
# AC-4 — refusals and notes name commands that exist
# --------------------------------------------------------------------------

_HOLE = "\u2063"  # an f-string placeholder (never in source text)


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(_HOLE)
        return "".join(parts)
    return None


def _subparsers(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:  # noqa: SLF001 — argparse has no API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return dict(action.choices)
    return {}


def _options(parser: argparse.ArgumentParser) -> set:
    return {opt for action in parser._actions  # noqa: SLF001
            for opt in action.option_strings}


def command_errors(text: str, parser: argparse.ArgumentParser) -> list:
    """Every backticked ``factory …`` command in ``text`` checked against
    the parser: the subcommand (and a nested one, e.g. ``toolkit list``)
    exists, and every ``--flag`` belongs to THAT subcommand."""
    errors = []
    subs = _subparsers(parser)
    for match in re.finditer(r"`factory ([^`]*)`", text):
        words = match.group(1).replace(_HOLE, " X ").split()
        if not words:
            errors.append(f"empty command: {match.group(0)!r}")
            continue
        name, rest = words[0], words[1:]
        if name.startswith("-"):
            if name not in _options(parser):
                errors.append(f"unknown top-level flag {name}")
            continue
        sub = subs.get(name)
        if sub is None:
            errors.append(f"unknown command `factory {name}`")
            continue
        nested = _subparsers(sub)
        if nested and rest and rest[0] in nested:
            sub = nested[rest[0]]
            rest = rest[1:]
        known = _options(sub)
        for word in rest:
            if word.startswith("--"):
                flag = word.split("=", 1)[0].rstrip(".,;:)")
                if flag not in known:
                    errors.append(f"`factory {name}` has no flag {flag}")
    return errors


def test_ac_4():
    parser = build_parser()
    # The checker itself: a wrong subcommand, a flag of another
    # subcommand, and a command split across an f-string are all judged.
    assert command_errors("run `factory adopt` now", parser)
    assert command_errors("`factory approve --reset-attempts`", parser)
    split = ast.parse('f"`factory recover {tid} --redraft --evidence`"')
    joined = _literal_text(split.body[0].value)  # type: ignore[attr-defined]
    assert joined is not None and not command_errors(joined, parser)
    bad = ast.parse('f"`factory recover {tid} --adopt`"')
    joined = _literal_text(bad.body[0].value)  # type: ignore[attr-defined]
    assert joined is not None and command_errors(joined, parser)
    assert not command_errors("`factory approve --adopt`", parser)

    found = 0
    errors: list[str] = []
    for path in sorted(FACTORY_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        # An f-string's literal parts are judged once, as the whole
        # f-string (its Constant children are not separate literals).
        inner = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                 for v in n.values}
        for node in ast.walk(tree):
            if id(node) in inner:
                continue
            text = _literal_text(node)
            if text is None or "`factory " not in text:
                continue
            found += len(re.findall(r"`factory [^`]*`", text))
            rel = path.relative_to(FACTORY_PKG.parent)
            errors += [f"{rel}:{node.lineno}: {e}"
                       for e in command_errors(text, parser)]
    assert found > 50, found  # the scan sees the source's commands
    assert errors == [], "\n".join(errors)
