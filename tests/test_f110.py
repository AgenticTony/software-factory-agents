"""F-110 — the bench measures the implementer, not the bench.

The first baseline (4/30 clean for $114) could not judge a model swap
because it measured the wrong things in four places. The ACs, one test
each:

* **AC-1** — hidden tests may only use the stated interface: an AST
  pin analysis at ``bench add`` refuses (named) every project name the
  hidden tests reach that the base does not already expose and the
  contract does not state; ``--interface FILE`` stores owner-written
  interface text (quote-checked, F-99) the replay's contract shows;
  ``factory bench check`` re-validates stored items and changes
  nothing.
* **AC-2** — a failure keeps its evidence: every hidden ref runs (not
  only to the first failure), each with its status and an escaped,
  redacted output tail; a non-clean replay keeps its escalation note
  and setup failure text in the report plus a small evidence folder
  (diff + test output); clean replays keep nothing.
* **AC-3** — cost is the sum over every attempt and every role; null
  only when no session reported a cost; totals, yield and cost per
  clean merge use it.
* **AC-4** — infrastructure is an error, not a failure: a closed list
  decides ``error:`` outcomes; errored replays leave the k/N
  denominator; the report shows ``k/N judged, E errored``; every other
  escalation — including any reason not on the list — is judged and
  counts against the model; ``--against`` treats errored↔judged flips
  as changes, never regressions.
* **AC-5** — a retry starts clean: the round whose session ended
  non-completed leaves its tree uncommitted (the orchestrator commits
  only completed sessions), and the retry's ready-clone refuses that
  dirt as ``setup_failed`` — fixed in the PRODUCTION attempt path
  (reset before readying), and an item whose BASE cannot build its
  environment is refused at add.
* **AC-6** — a run survives interruption: the report is written
  incrementally and atomically after each replay, SIGINT/SIGTERM
  leaves it marked ``incomplete``, ``--items`` selects items,
  ``--resume`` appends the missing replays under the same report
  without re-running any recorded one, and the cleanup never deletes
  finished replays' evidence.

Scripted seats, real git, a real base→head history — never a paid
session, never JobFinderOS.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _support.f110 import (
    ALIASED_TEST,
    COMPANY_HEAD,
    DATACLASS_CLEAN_TEST,
    DATACLASS_PINNED_TEST,
    HIDDEN_PATH,
    HIDDEN_PATH_2,
    INTERFACE_QUOTING_DIFF,
    INTERFACE_TEXT,
    PINNED_TEST,
    PLAIN_TEST,
    PLAIN_TEST_2,
    POSTING_BASE,
    POSTING_HEAD,
    PROSE_KEYWORD_TEST,
    STAR_TEST,
    TASK_ID,
    WRONG_PATH_TEST,
    Env110,
    Seats110,
)

from factory.bench import BenchError, bench_add, load_items


@pytest.fixture(autouse=True)
def _no_peak_window(monkeypatch):
    """Every bench test runs with the peak window disabled (the owner's
    default window would make replays escalate inside it)."""
    import dataclasses

    import factory.bench as bench_module
    import factory.cli as cli_module

    for module in (bench_module, cli_module):
        real = module.load_config
        monkeypatch.setattr(
            module, "load_config",
            lambda path, _real=real: dataclasses.replace(
                _real(path), peak_window=None))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True,
    ).stdout.strip()


class StubRunner:
    """A command runner answering from a script: a key matched against
    the command's name OR text → (exit_code, output). Unscripted
    commands run for real (the F-104 CountingRunner shape)."""

    def __init__(self, script: "dict[str, tuple[int, str]] | None" = None):
        from factory.gates import CommandResult, run_command

        self._result = CommandResult
        self._run = run_command
        self.script = dict(script or {})
        self.calls: list[str] = []

    def __call__(self, name, command, repo, timeout_sec):
        self.calls.append(name)
        for key, (code, out) in self.script.items():
            if key in name or key in command:
                return self._result(name=name, command=command,
                                    exit_code=code, output=out,
                                    timed_out=False)
        return self._run(name, command, repo, timeout_sec)


# ------------------------------------------------------------------ AC-1 --


def test_ac_1(tmp_path: Path) -> None:
    """Hidden tests may only use the stated interface: the keyword pin
    refuses named, the interface text allows it and reaches the
    implementer, base-present and in-file names are not pins, the
    unresolvable and other-path arms are caught, the interface text is
    quote-checked, and bench check re-validates without changing
    anything."""
    from factory.bench import _bench_task, bench_check, bench_run

    # The BENCH-03 shape: a keyword the merged head added and the
    # contract never names.
    env = Env110(tmp_path)
    with pytest.raises(BenchError) as caught:
        bench_add(env.factory_root, "fixture", TASK_ID,
                  command_runner=StubRunner())
    assert "verify_text_layer(applicant_name=)" in str(caught.value), \
        str(caught.value)
    # The refusal names the module path too (where the callable lives).
    assert "app.services.pdf_service" in str(caught.value)

    # The same item with that keyword in owner-written interface text
    # is accepted; the store carries the text; the replay's contract
    # shows it to the implementer — in the task AND in the prompt.
    env2 = Env110(tmp_path / "with-interface", interface=INTERFACE_TEXT)
    bench_add(env2.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT,
              command_runner=StubRunner())
    item = load_items(env2.factory_root, "fixture")[0]
    assert item.interface == INTERFACE_TEXT
    task = _bench_task(item)
    assert any("applicant_name" in a["text"] for a in task["acceptance"]), \
        "the replay's contract must show the interface"
    seats = Seats110()
    bench_run(env2.factory_root, "fixture", session_runner=seats,
              command_runner=StubRunner())
    assert any("applicant_name" in prompt
               for prompt in seats.prompts("implementer")), \
        "the interface text must reach the implementer's prompt"

    # A name already present at base is not a pin: the plain test uses
    # only base-stated names and adds with no interface text.
    env3 = Env110(tmp_path / "plain", hidden_test=PLAIN_TEST,
                  hidden_test_2=PLAIN_TEST_2)
    bench_add(env3.factory_root, "fixture", TASK_ID,
              command_runner=StubRunner())

    # A keyword present at base under ANOTHER module path is still a
    # pin when the test reaches it through a head-only module.
    env4 = Env110(tmp_path / "wrong-path", hidden_test=WRONG_PATH_TEST,
                  with_old_service=True, with_new_service=True)
    with pytest.raises(BenchError) as caught:
        bench_add(env4.factory_root, "fixture", TASK_ID,
                  command_runner=StubRunner())
    assert "render(new_kw=)" in str(caught.value), str(caught.value)

    # The aliased import's attribute call is caught.
    env5 = Env110(tmp_path / "aliased", hidden_test=ALIASED_TEST)
    with pytest.raises(BenchError) as caught:
        bench_add(env5.factory_root, "fixture", TASK_ID,
                  command_runner=StubRunner())
    assert "verify_text_layer(applicant_name=)" in str(caught.value)

    # A star-import plus a new name is refused as unresolvable.
    env6 = Env110(tmp_path / "star", hidden_test=STAR_TEST,
                  star_head_names="def name_group_label():\n"
                                  '    return "from your profile name: "\n')
    with pytest.raises(BenchError) as caught:
        bench_add(env6.factory_root, "fixture", TASK_ID,
                  command_runner=StubRunner())
    text = str(caught.value)
    assert "name_group_label" in text and "unresolvable" in text, text

    # A3: a class without __init__ (the dataclass / pydantic / SQLModel
    # shape) accepts a keyword only if it is a declared field: head
    # adds `company`, the constructor keyword pins.
    env6b = Env110(tmp_path / "dataclass-pin",
                   hidden_test=DATACLASS_PINNED_TEST,
                   extra_base_files={
                       "backend/app/services/posting_service.py":
                       POSTING_BASE},
                   extra_head_files={
                       "backend/app/services/posting_service.py":
                       POSTING_HEAD})
    with pytest.raises(BenchError) as caught:
        bench_add(env6b.factory_root, "fixture", TASK_ID,
                  command_runner=StubRunner())
    assert "JobPosting(company=)" in str(caught.value), str(caught.value)
    # P1b: base-declared FIELDS are not pins — constructing with them
    # and reading one as an attribute adds clean.
    env6c = Env110(tmp_path / "dataclass-clean",
                   hidden_test=DATACLASS_CLEAN_TEST,
                   extra_base_files={
                       "backend/app/services/posting_service.py":
                       POSTING_BASE},
                   extra_head_files={
                       "backend/app/services/posting_service.py":
                       POSTING_HEAD})
    bench_add(env6c.factory_root, "fixture", TASK_ID,
              command_runner=StubRunner())

    # B2: keyword pins are stated by the interface text, or by the
    # contract as `kw=` / beside the callable — prose naming the WORD
    # does not state the keyword.
    env6d = Env110(tmp_path / "prose-keyword",
                   hidden_test=PROSE_KEYWORD_TEST,
                   criterion_text="the company name is shown")
    with pytest.raises(BenchError) as caught:
        bench_add(env6d.factory_root, "fixture", TASK_ID,
                  command_runner=StubRunner())
    assert "verify_text_layer(company=)" in str(caught.value), \
        str(caught.value)
    # …and a contract line that pairs the callable with the keyword
    # states it.
    env6e = Env110(tmp_path / "stated-keyword",
                   hidden_test=PROSE_KEYWORD_TEST,
                   head_service=COMPANY_HEAD,
                   criterion_text="verify_text_layer(company=) carries "
                                  "the company")
    bench_add(env6e.factory_root, "fixture", TASK_ID,
              command_runner=StubRunner())

    # The interface text itself is quote-checked (F-99): naming the
    # keyword is fine, quoting the merged diff's added code line is not.
    env7 = Env110(tmp_path / "quoting", interface=INTERFACE_QUOTING_DIFF)
    with pytest.raises(BenchError) as caught:
        bench_add(env7.factory_root, "fixture", TASK_ID,
                  interface_text=INTERFACE_QUOTING_DIFF,
                  command_runner=StubRunner())
    assert "quotes the merged diff" in str(caught.value)

    # bench check re-validates stored items, names each one that would
    # now refuse, and changes nothing: strip the interface from the
    # stored item (a pre-interface store's exact shape) and re-check.
    env8 = Env110(tmp_path / "check", interface=INTERFACE_TEXT)
    bench_add(env8.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT,
              command_runner=StubRunner())
    doc_path = next((env8.factory_root / "bench" / "fixture")
                    .glob("BENCH-*")) / "item.json"
    doc = json.loads(doc_path.read_text())
    del doc["interface"]
    doc_path.write_text(json.dumps(doc, indent=2) + "\n")
    report = bench_check(env8.factory_root, "fixture")
    refusals = [r for i in report["items"] for r in i["refusals"]]
    assert any("verify_text_layer(applicant_name=)" in r for r in refusals)
    assert [i["slot"] for i in report["items"] if i["refusals"]], \
        "the refusing item must be named"
    assert json.loads(doc_path.read_text()) == doc  # nothing changed


# ------------------------------------------------------------------ AC-2 --


def test_ac_2(tmp_path: Path) -> None:
    """A failure keeps its evidence: every ref runs, each with status
    and an escaped/redacted tail; the escalation note and setup failure
    text stay on the record; the evidence folder holds the diff and the
    test output; clean replays keep nothing."""
    from factory.bench import bench_run

    env = Env110(tmp_path, hidden_test=PLAIN_TEST,
                 hidden_test_2=PLAIN_TEST_2)
    bench_add(env.factory_root, "fixture", TASK_ID,
              command_runner=StubRunner())
    # The wrong fix: would publish, hidden FAIL — with hostile output
    # (ANSI, NUL, a token shape) on the failing ref's tail. The token
    # is built at runtime (the F-58 rule: no token-like literal in the
    # tree) in CREDENTIAL_PATTERNS' own ghp_ shape.
    from _support.tokens import fake_github_token

    hostile = ("line1\n\x1b[2J\x1b[1mFAIL\x1b[0m\n\x00binary\x00\n"
               f"token={fake_github_token('f110-tail')}\nlast line\n")
    stub = StubRunner({f"{HIDDEN_PATH}::test_ac_1": (1, hostile)})
    result = bench_run(env.factory_root, "fixture",
                       session_runner=Seats110(mode="wrong"),
                       command_runner=stub)
    record = result["report"]["items"][0]["repeats"][0]
    assert record["hidden_pass"] is False
    # EVERY ref ran, not only to the first failure.
    statuses = {r["ref"]: r["status"] for r in record["hidden_results"]}
    assert statuses[f"{HIDDEN_PATH}::test_ac_1"] == "fail"
    assert f"{HIDDEN_PATH_2}::test_ac_2" in statuses
    # The tail is escaped (F-109) and the token redacted.
    tail = next(r["tail"] for r in record["hidden_results"]
                if r["ref"] == f"{HIDDEN_PATH}::test_ac_1")
    assert "\\x1b[2J" in tail and "\\x00" in tail
    assert fake_github_token("f110-tail") not in tail
    assert "[redacted]" in tail
    # The evidence folder exists under runs/<stamp>/<slot>-rN/ with the
    # diff and the test output.
    stamp = result["report"]["stamp"]
    slot = result["report"]["items"][0]["slot"]
    evidence = (env.factory_root / "bench" / "fixture" / "runs" / stamp
                / f"{slot}-r1")
    diff = (evidence / "diff.patch").read_text()
    assert "verify_text_layer" in diff
    out_text = (evidence / "hidden-output.txt").read_text()
    assert "test_ac_1" in out_text and "\\x1b[2J" in out_text

    # The setup failure text stays on the record (the A6 arm): the
    # scoring-side setup fails on the replay head.
    env2 = Env110(tmp_path / "setup-note", hidden_test=PLAIN_TEST)
    bench_add(env2.factory_root, "fixture", TASK_ID,
              command_runner=StubRunner())
    result2 = bench_run(
        env2.factory_root, "fixture",
        session_runner=Seats110(mode="good"),
        command_runner=StubRunner({"setup:score": (2, "boom: no venv")}))
    record2 = result2["report"]["items"][0]["repeats"][0]
    assert record2["hidden_pass"] is False
    assert "setup failed on the replay head" in (record2["hidden_note"]
                                                 or "")

    # A clean replay keeps no evidence folder.
    env3 = Env110(tmp_path / "clean", hidden_test=PLAIN_TEST)
    bench_add(env3.factory_root, "fixture", TASK_ID,
              command_runner=StubRunner())
    result3 = bench_run(env3.factory_root, "fixture",
                        session_runner=Seats110(mode="good"),
                        command_runner=StubRunner())
    stamp3 = result3["report"]["stamp"]
    runs3 = env3.factory_root / "bench" / "fixture" / "runs" / stamp3
    assert not runs3.exists() or not any(runs3.iterdir()), \
        "a clean replay keeps nothing"

    # B5: an evidence folder that cannot be written is EXPLAINED on the
    # record (evidence_error), never a silent gap — the tail and the
    # outcome still land.
    import factory.bench as bench_module

    monkeypatch_stamp = pytest.MonkeyPatch()
    monkeypatch_stamp.setattr(bench_module, "new_run_id",
                              lambda: "20261126T120000Z-fixed01")
    try:
        env4 = Env110(tmp_path / "evidence-error",
                      hidden_test=PLAIN_TEST)
        bench_add(env4.factory_root, "fixture", TASK_ID,
                  command_runner=StubRunner())
        blocker = (env4.factory_root / "bench" / "fixture" / "runs"
                   / "20261126T120000Z-fixed01" / "BENCH-01-r1")
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the evidence folder goes")
        result4 = bench_run(env4.factory_root, "fixture",
                            session_runner=Seats110(mode="wrong"),
                            command_runner=StubRunner())
    finally:
        monkeypatch_stamp.undo()
    record4 = result4["report"]["items"][0]["repeats"][0]
    assert record4["hidden_pass"] is False
    assert record4.get("evidence_error"), \
        "an unwritable evidence folder must be explained on the record"


# ------------------------------------------------------------------ AC-3 --


def test_ac_3(tmp_path: Path) -> None:
    """Cost is the sum over every attempt and every role: attempts
    costing $3 and $5 report $8; a setup_failed replay after a paid $3
    attempt reports $3, never n/a; the totals use the summed figure;
    and a trace that reported no cost falls back to the attempt sums
    rather than hiding a paid attempt (B4)."""
    from factory.bench import bench_run

    env = Env110(tmp_path, hidden_test=PINNED_TEST,
                 interface=INTERFACE_TEXT)
    bench_add(env.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    # Two paid rounds (review blocks both): the implementer costs $3
    # then $5 → the replay cost is $8, not the last attempt's $5.
    result = bench_run(env.factory_root, "fixture",
                       session_runner=Seats110(reviews="block",
                                               costs=[3.0, 5.0],
                                               reviewer_cost=0.0,
                                               verifier_cost=0.0),
                       command_runner=StubRunner())
    record = result["report"]["items"][0]["repeats"][0]
    assert record["attempts"] >= 2
    assert record["cost_usd"] == pytest.approx(8.0), record["cost_usd"]
    assert result["report"]["totals"]["cost_usd"] == pytest.approx(8.0)

    # setup_failed after a paid attempt: round 1 costs $3 and commits;
    # the profile's setup fails from round 2 on (the round-1 commit
    # message is the marker) — the replay reports $3, never n/a.
    env2 = Env110(
        tmp_path / "setup-fail",
        hidden_test=PINNED_TEST, interface=INTERFACE_TEXT,
        setup_command='! git log -1 --format=%s | grep -q "attempt 1"')
    bench_add(env2.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    result2 = bench_run(env2.factory_root, "fixture",
                        session_runner=Seats110(costs=[3.0],
                                                reviewer_cost=0.0,
                                                verifier_cost=0.0),
                        command_runner=StubRunner())
    record2 = result2["report"]["items"][0]["repeats"][0]
    assert record2["factory_outcome"] == "escalated:setup_failed"
    assert record2["cost_usd"] == pytest.approx(3.0), record2["cost_usd"]
    # The escalation note (the setup failure text) is kept on the record.
    assert record2["escalation_note"], "the escalation note is kept"
    assert "setup" in record2["escalation_note"]

    # B4: sessions traced but none reported a cost, while the attempt
    # records carry one — the replay falls back to the attempt sums,
    # never n/a over a paid attempt.
    import factory.bench as bench_module

    real_trace_roles = bench_module._trace_roles

    def _no_cost_trace(scratch, project, run_id):
        roles = real_trace_roles(scratch, project, run_id)
        return {role: {**usage, "cost_usd": 0.0, "cost_sessions": 0}
                for role, usage in roles.items()}

    monkeypatch_trace = pytest.MonkeyPatch()
    monkeypatch_trace.setattr(bench_module, "_trace_roles",
                              _no_cost_trace)
    try:
        result3 = bench_run(env2.factory_root, "fixture",
                            session_runner=Seats110(costs=[3.0],
                                                    reviewer_cost=0.0,
                                                    verifier_cost=0.0),
                            command_runner=StubRunner())
    finally:
        monkeypatch_trace.undo()
    record3 = result3["report"]["items"][0]["repeats"][0]
    assert record3["cost_usd"] == pytest.approx(3.0), record3["cost_usd"]

    # B7: the fallback is decided PER RUN dir. Run A's trace reported
    # $5; run B's trace reported none while its attempt record carries
    # $3 (a session killed by quota before its result event) — the
    # replay reports $8, never $5 with the paid run hidden.
    from factory.bench import _replay_cost

    def _trace_by_run(scratch, project, run_id):
        if run_id == "run-a":
            return {"implementer": {
                "sessions": 1, "turns": 10, "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 5.0, "cost_sessions": 1,
                "models": ["m"]}}
        return {"implementer": {
            "sessions": 1, "turns": 10, "input_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0, "cost_sessions": 0,
            "models": ["m"]}}

    monkeypatch_b7 = pytest.MonkeyPatch()
    monkeypatch_b7.setattr(bench_module, "_trace_roles", _trace_by_run)
    try:
        cost, _by_role = _replay_cost(
            [{"run_id": "run-a", "cost_usd": 5.0},
             {"run_id": "run-b", "cost_usd": 3.0}],
            Path("/nonexistent-scratch"), "fixture")
    finally:
        monkeypatch_b7.undo()
    assert cost == pytest.approx(8.0), cost

    # An attempt with no run_id counts from its own record.
    cost2, _ = _replay_cost([{"run_id": None, "cost_usd": 2.5}],
                            Path("/nonexistent-scratch"), "fixture")
    assert cost2 == pytest.approx(2.5), cost2


# ------------------------------------------------------------------ AC-4 --


def test_ac_4(tmp_path: Path) -> None:
    """Infrastructure is an error, not a failure: errored replays leave
    the k/N denominator, the report shows k/N judged E errored, unknown
    escalation reasons are judged (count against the model), and
    --against treats errored↔judged flips as changes, never
    regressions."""
    from factory.bench import (
        _INFRA_ESCALATIONS,
        _record_is_error,
        bench_run,
        compare_reports,
        render_report,
    )

    # The closed list, exactly the brief's members (a missing
    # credential and a crash arrive as ``error:`` outcomes, a dirty
    # clone as setup_failed).
    assert _INFRA_ESCALATIONS == frozenset({"setup_failed",
                                            "quota_exhausted",
                                            "peak_window"})
    # Unknown reasons are judged — the costlier-error rule.
    assert _record_is_error({"factory_outcome":
                             "escalated:attempts_exhausted"}) is False
    assert _record_is_error({"factory_outcome":
                             "escalated:brand_new_reason"}) is False
    assert _record_is_error({"factory_outcome":
                             "escalated:scope_change_needed"}) is False
    assert _record_is_error({"factory_outcome":
                             "escalated:quota_exhausted"}) is True
    assert _record_is_error({"factory_outcome":
                             "error:RunError: no credential"}) is True
    assert _record_is_error({"factory_outcome": "would_publish"}) is False

    # A real errored replay (setup_failed at ROUND 1, nothing committed
    # — the lowercased bench branch name is the marker) leaves the
    # denominator.
    env = Env110(
        tmp_path, hidden_test=PINNED_TEST, interface=INTERFACE_TEXT,
        setup_command='if git branch --show-current | grep -qi BENCH; '
                      "then exit 4; fi")
    bench_add(env.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    result = bench_run(env.factory_root, "fixture",
                       session_runner=Seats110(costs=[3.0]),
                       command_runner=StubRunner())
    item = result["report"]["items"][0]
    assert item["errored"] == 1 and item["judged"] == 0
    assert item["clean"] == 0
    assert result["report"]["totals"]["judged"] == 0
    assert result["report"]["totals"]["errored"] == 1
    rendered = render_report(result["report"])
    assert "0/0 judged" in rendered and "1 errored" in rendered

    # A2: a setup_failed AFTER COMMITTED model work is judged — the
    # model's round-1 commit broke the setup the retry runs (with the
    # AC-5 reset, committed work is the only dirt that can remain).
    env2 = Env110(
        tmp_path / "committed-setup", hidden_test=PINNED_TEST,
        interface=INTERFACE_TEXT,
        setup_command='! git log -1 --format=%s | grep -q "attempt 1"')
    bench_add(env2.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    result2 = bench_run(env2.factory_root, "fixture",
                        session_runner=Seats110(costs=[3.0]),
                        command_runner=StubRunner())
    item2 = result2["report"]["items"][0]
    assert item2["errored"] == 0 and item2["judged"] == 1, item2
    record2 = item2["repeats"][0]
    assert record2["factory_outcome"] == "escalated:setup_failed"
    assert "committed" in (record2["escalation_note"] or ""), \
        record2["escalation_note"]

    # A judged failure (the wrong fix) counts in the denominator.
    env3 = Env110(tmp_path / "judged", hidden_test=PLAIN_TEST)
    bench_add(env3.factory_root, "fixture", TASK_ID,
              command_runner=StubRunner())
    result3 = bench_run(env3.factory_root, "fixture",
                        session_runner=Seats110(mode="wrong"),
                        command_runner=StubRunner())
    item3 = result3["report"]["items"][0]
    assert item3["judged"] == 1 and item3["errored"] == 0

    # --against: errored→judged and judged→errored are changes, never
    # regressions. Synthetic reports (the flip is the unit).
    before = {"items": [{"slot": "BENCH-01", "judged": 0, "clean": 0,
                         "errored": 3},
                        {"slot": "BENCH-02", "judged": 3, "clean": 1,
                         "errored": 0}]}
    after = {"items": [{"slot": "BENCH-01", "judged": 3, "clean": 0,
                        "errored": 0},
                       {"slot": "BENCH-02", "judged": 0, "clean": 0,
                        "errored": 3}]}
    diff = compare_reports(before, after)
    assert diff["regressions"] == [], diff
    assert sorted(diff["error_changes"]) == ["BENCH-01", "BENCH-02"], diff
    # A genuine regression (pass → fail among judged) still leads.
    diff2 = compare_reports(
        {"items": [{"slot": "BENCH-03", "judged": 2, "clean": 1}]},
        {"items": [{"slot": "BENCH-03", "judged": 2, "clean": 0}]})
    assert diff2["regressions"] == ["BENCH-03"]


# ------------------------------------------------------------------ AC-5 --


def test_ac_5(tmp_path: Path) -> None:
    """A retry starts clean: a round whose session ends non-completed
    leaves uncommitted edits (the orchestrator commits only completed
    sessions); the retry's setup passes on a clean clone instead of
    refusing setup_failed; and an item whose BASE cannot build its
    environment is refused at add, named."""
    from factory.bench import bench_run

    # The production attempt path, through the bench replay: round 1
    # dirties the clone and ends aborted (no commit); round 2 must run.
    env = Env110(tmp_path, hidden_test=PINNED_TEST,
                 interface=INTERFACE_TEXT)
    bench_add(env.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    seats = Seats110(mode="dirty_exit")
    result = bench_run(env.factory_root, "fixture",
                       session_runner=seats, command_runner=StubRunner())
    record = result["report"]["items"][0]["repeats"][0]
    assert record["factory_outcome"] != "escalated:setup_failed", \
        "the retry must not judge round 1's dirt as setup's"
    assert seats.impl_calls >= 2, "the retry round must run a session"
    assert record["attempts"] >= 2

    # An item whose base cannot build its environment is refused at
    # add, named (the replay readies its clone at BASE — a base that
    # cannot install scores every replay an infrastructure error).
    env2 = Env110(
        tmp_path / "base-setup", hidden_test=PINNED_TEST,
        interface=INTERFACE_TEXT,
        setup_command="sh -c 'exit 3'")
    with pytest.raises(BenchError) as caught:
        bench_add(env2.factory_root, "fixture", TASK_ID,
                  interface_text=INTERFACE_TEXT,
                  command_runner=StubRunner())
    assert "base" in str(caught.value).lower(), str(caught.value)


# ------------------------------------------------------------------ AC-6 --


def test_ac_6(tmp_path: Path) -> None:
    """A run survives interruption: the report is written
    incrementally and atomically; an interrupt marks it incomplete;
    --items selects; --resume appends the missing replays under the
    same report without re-running any recorded one; the cleanup never
    deletes finished replays' evidence."""
    from factory.bench import bench_run

    env = Env110(tmp_path, hidden_test=PINNED_TEST,
                 interface=INTERFACE_TEXT)
    bench_add(env.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    env2 = Env110(tmp_path / "second", hidden_test=PINNED_TEST,
                  interface=INTERFACE_TEXT)
    bench_add(env2.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    # one store, two items (distinct heads, so the store loads)
    store = env.factory_root / "bench" / "fixture"
    src_slot = next((env2.factory_root / "bench" / "fixture").glob(
        "BENCH-*"))
    dst_slot = store / "BENCH-02"
    subprocess.run(["cp", "-R", str(src_slot), str(dst_slot)], check=True)
    doc = json.loads((dst_slot / "item.json").read_text())
    doc["slot"] = "BENCH-02"
    (dst_slot / "item.json").write_text(json.dumps(doc, indent=2) + "\n")

    # --items selects only the named items.
    picked = bench_run(env.factory_root, "fixture", items=["BENCH-02"],
                       session_runner=Seats110(mode="wrong"),
                       command_runner=StubRunner())
    assert [i["slot"] for i in picked["report"]["items"]] == ["BENCH-02"]

    # SIGINT mid-run (the 3rd implementer call = replay 3 of 4): an
    # incomplete report with the finished replays.
    before = set((store / "reports").glob("bench-*.json"))
    with pytest.raises(KeyboardInterrupt):
        bench_run(env.factory_root, "fixture", repeats=2,
                  session_runner=Seats110(mode="wrong", interrupt_at=3),
                  command_runner=StubRunner())
    new_reports = set((store / "reports").glob("bench-*.json")) - before
    assert len(new_reports) == 1, "exactly one report per stamp"
    report_path = next(iter(new_reports))
    partial = json.loads(report_path.read_text())
    assert partial.get("incomplete"), partial.get("incomplete")
    done = [(i["slot"], r["repeat"])
            for i in partial["items"] for r in i["repeats"]]
    assert len(done) == 2 and len(set(done)) == 2, done
    # The evidence of the finished replays survived the cleanup.
    runs_dir = store / "runs" / partial["stamp"]
    assert any(runs_dir.glob("*/hidden-output.txt")), \
        "finished replays' evidence must survive the run cleanup"

    # Resume: the same report gains the missing replays, none re-run.
    resume_calls: list[str] = []
    bench_run(env.factory_root, "fixture", repeats=2, resume=report_path,
              session_runner=_CountingSeats(resume_calls),
              command_runner=StubRunner())
    final = json.loads(report_path.read_text())
    assert not final.get("incomplete"), final.get("incomplete")
    all_replays = [(i["slot"], r["repeat"])
                   for i in final["items"] for r in i["repeats"]]
    assert sorted(all_replays) == [("BENCH-01", 1), ("BENCH-01", 2),
                                   ("BENCH-02", 1), ("BENCH-02", 2)]
    assert len(resume_calls) == 2, \
        "only the two missing replays ran, never a recorded one"
    # Resume cannot re-run a recorded FAILED replay to replace it: a
    # resume over the complete report runs nothing.
    again_seats = _CountingSeats([])
    again = bench_run(env.factory_root, "fixture", repeats=2,
                      resume=report_path,
                      session_runner=again_seats,
                      command_runner=StubRunner())
    assert not again_seats.sink, "a complete report resumes to nothing"
    assert again["report"]["items"] == final["items"]

    # A1: --resume + --items never drops a recorded replay from the
    # report. Interrupt after replay 1 of 4, resume --items BENCH-02
    # only: the file keeps BENCH-01's record, gains BENCH-02's two, and
    # STAYS incomplete (its own set is not finished); the full resume
    # then completes it with no replay run twice.
    before2 = set((store / "reports").glob("bench-*.json"))
    with pytest.raises(KeyboardInterrupt):
        bench_run(env.factory_root, "fixture", repeats=2,
                  session_runner=Seats110(mode="wrong", interrupt_at=2),
                  command_runner=StubRunner())
    partial2_path = next(iter(
        set((store / "reports").glob("bench-*.json")) - before2))
    partial2 = json.loads(partial2_path.read_text())
    assert [(i["slot"], r["repeat"]) for i in partial2["items"]
            for r in i["repeats"]] == [("BENCH-01", 1)]
    narrowed_calls: list[str] = []
    bench_run(env.factory_root, "fixture", repeats=2,
              resume=partial2_path, items=["BENCH-02"],
              session_runner=_CountingSeats(narrowed_calls),
              command_runner=StubRunner())
    narrowed = json.loads(partial2_path.read_text())
    kept = sorted((i["slot"], r["repeat"]) for i in narrowed["items"]
                  for r in i["repeats"])
    assert kept == [("BENCH-01", 1), ("BENCH-02", 1), ("BENCH-02", 2)], kept
    assert narrowed.get("incomplete"), \
        "a resumed report stays incomplete while its own set misses replays"
    assert len(narrowed_calls) == 2
    final2 = bench_run(env.factory_root, "fixture", repeats=2,
                       resume=partial2_path,
                       session_runner=_CountingSeats([]),
                       command_runner=StubRunner())
    done2 = sorted((i["slot"], r["repeat"]) for i in final2["report"]["items"]
                   for r in i["repeats"])
    assert done2 == [("BENCH-01", 1), ("BENCH-01", 2),
                     ("BENCH-02", 1), ("BENCH-02", 2)]
    assert not final2["report"].get("incomplete")

    # R2-A1: a plain resume (no --items) runs ONLY the report's own
    # items — a store item the report never had neither runs nor has
    # its replays silently dropped. Narrow first, interrupt after one.
    before3 = set((store / "reports").glob("bench-*.json"))
    with pytest.raises(KeyboardInterrupt):
        bench_run(env.factory_root, "fixture", repeats=2,
                  items=["BENCH-01"],
                  session_runner=Seats110(mode="wrong", interrupt_at=2),
                  command_runner=StubRunner())
    partial3_path = next(iter(
        set((store / "reports").glob("bench-*.json")) - before3))
    partial3 = json.loads(partial3_path.read_text())
    assert [i["slot"] for i in partial3["items"]] == ["BENCH-01"]
    plain_calls: list[str] = []
    bench_run(env.factory_root, "fixture", repeats=2,
              resume=partial3_path,
              session_runner=_CountingSeats(plain_calls),
              command_runner=StubRunner())
    resumed3 = json.loads(partial3_path.read_text())
    assert [i["slot"] for i in resumed3["items"]] == ["BENCH-01"], \
        "a resume never widens the report to store items it never had"
    assert sorted((i["slot"], r["repeat"]) for i in resumed3["items"]
                  for r in i["repeats"]) == [("BENCH-01", 1), ("BENCH-01", 2)]
    assert len(plain_calls) == 1  # only the missing BENCH-01 r2 ran
    stamp3 = resumed3["stamp"]
    ran = sorted(p.name for p in
                 (store / "runs" / stamp3).iterdir()) \
        if (store / "runs" / stamp3).exists() else []
    assert not any(name.startswith("BENCH-02") for name in ran), \
        "a store item outside the report must not run on resume"

    # R2-A2: a resume without --role ADOPTS the report's overrides (one
    # report is one measurement); a different set refuses.
    before4 = set((store / "reports").glob("bench-*.json"))
    with pytest.raises(KeyboardInterrupt):
        bench_run(env.factory_root, "fixture", repeats=2,
                  items=["BENCH-01"], role_overrides={"implementer": "glm-5.4"},
                  session_runner=Seats110(mode="wrong", interrupt_at=1),
                  command_runner=StubRunner())
    partial4_path = next(iter(
        set((store / "reports").glob("bench-*.json")) - before4))
    assert json.loads(partial4_path.read_text())["role_overrides"] == \
        {"implementer": "glm-5.4"}
    bench_run(env.factory_root, "fixture", repeats=2,
              resume=partial4_path,
              session_runner=_CountingSeats([]),
              command_runner=StubRunner())
    resumed4 = json.loads(partial4_path.read_text())
    assert resumed4["role_overrides"] == {"implementer": "glm-5.4"}, \
        "a resume keeps the report's role overrides"
    with pytest.raises(BenchError) as caught:
        bench_run(env.factory_root, "fixture", repeats=2,
                  resume=partial4_path, role_overrides={"implementer": "glm-5.3"},
                  session_runner=_CountingSeats([]),
                  command_runner=StubRunner())
    assert "role overrides" in str(caught.value)


class _CountingSeats(Seats110):
    """Seats that count implementer calls (the no-re-run proof)."""

    def __init__(self, sink: list):
        super().__init__(mode="wrong")
        self.sink = sink

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat == "implementer":
            self.sink.append(prompt[:40])
        return super().__call__(seat, prompt, run_dir, profile, **kwargs)


def test_resume_integrity(tmp_path: Path) -> None:
    """The goal-level resume attack: every way a resume could change
    WHAT is measured refuses or keeps one honest measurement — a
    pre-F-110 report (no fingerprints), a store that changed under the
    report, a foreign --items slot, changed repeats, and a second
    concurrent resume."""
    import fcntl

    from factory.bench import bench_run

    env = Env110(tmp_path, hidden_test=PINNED_TEST,
                 interface=INTERFACE_TEXT)
    bench_add(env.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    env2 = Env110(tmp_path / "second", hidden_test=PINNED_TEST,
                  interface=INTERFACE_TEXT)
    bench_add(env2.factory_root, "fixture", TASK_ID,
              interface_text=INTERFACE_TEXT, command_runner=StubRunner())
    store = env.factory_root / "bench" / "fixture"
    src = next((env2.factory_root / "bench" / "fixture").glob("BENCH-*"))
    dst = store / "BENCH-02"
    subprocess.run(["cp", "-R", str(src), str(dst)], check=True)
    doc = json.loads((dst / "item.json").read_text())
    doc["slot"] = "BENCH-02"
    (dst / "item.json").write_text(json.dumps(doc, indent=2) + "\n")

    with pytest.raises(KeyboardInterrupt):
        bench_run(env.factory_root, "fixture", repeats=2,
                  items=["BENCH-01"],
                  session_runner=Seats110(mode="wrong", interrupt_at=2),
                  command_runner=StubRunner())
    report_path = sorted((store / "reports").glob("bench-*.json"))[-1]

    # A pre-F-110 report (no item fingerprints) cannot resume: its
    # records predate the summed cost and the error classification.
    partial = json.loads(report_path.read_text())
    saved = partial.pop("items_fingerprint")
    report_path.write_text(json.dumps(partial, indent=2) + "\n")
    with pytest.raises(BenchError) as caught:
        bench_run(env.factory_root, "fixture", repeats=2,
                  resume=report_path,
                  session_runner=Seats110(mode="wrong"),
                  command_runner=StubRunner())
    assert "pre-F-110" in str(caught.value), str(caught.value)
    partial["items_fingerprint"] = saved
    report_path.write_text(json.dumps(partial, indent=2) + "\n")

    # A store that changed under the report (its interface text edited)
    # refuses, naming the item — the recorded replays and the new ones
    # would measure different contracts.
    item_doc = json.loads(
        (store / "BENCH-01" / "item.json").read_text())
    item_doc["interface"] = item_doc["interface"] + "\nAn edit.\n"
    (store / "BENCH-01" / "item.json").write_text(
        json.dumps(item_doc, indent=2) + "\n")
    with pytest.raises(BenchError) as caught:
        bench_run(env.factory_root, "fixture", repeats=2,
                  resume=report_path,
                  session_runner=Seats110(mode="wrong"),
                  command_runner=StubRunner())
    assert "BENCH-01" in str(caught.value) and "changed" in str(caught.value)
    item_doc["interface"] = item_doc["interface"].replace("\nAn edit.\n", "")
    (store / "BENCH-01" / "item.json").write_text(
        json.dumps(item_doc, indent=2) + "\n")

    # A --items slot the report never had refuses (it cannot widen the
    # measurement), and changed repeats refuse.
    with pytest.raises(BenchError) as caught:
        bench_run(env.factory_root, "fixture", repeats=2,
                  resume=report_path, items=["BENCH-02"],
                  session_runner=Seats110(mode="wrong"),
                  command_runner=StubRunner())
    assert "BENCH-02" in str(caught.value)
    with pytest.raises(BenchError) as caught:
        bench_run(env.factory_root, "fixture", repeats=3,
                  resume=report_path,
                  session_runner=Seats110(mode="wrong"),
                  command_runner=StubRunner())
    assert "repeats" in str(caught.value)

    # A second concurrent resume of one report refuses (the first
    # holds the report's lock — on the RESOLVED path).
    lock = open(str(Path(report_path).resolve()) + ".lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BenchError) as caught:
            bench_run(env.factory_root, "fixture", repeats=2,
                      resume=report_path,
                      session_runner=Seats110(mode="wrong"),
                      command_runner=StubRunner())
        assert "another bench run" in str(caught.value)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    # A1: every INCREMENTAL write of a fresh run carries the in-progress
    # mark — a hard kill (SIGKILL/OOM/SIGHUP) leaves a file that says
    # it is unfinished, never a short sample reading as a full
    # measurement, and the killed run's own file is marked too.
    import factory.bench as bench_module
    from factory.bench import render_report as rr

    before_a1 = set((store / "reports").glob("bench-*.json"))
    with pytest.raises(KeyboardInterrupt):
        bench_run(env.factory_root, "fixture", repeats=2,
                  items=["BENCH-01"],
                  session_runner=Seats110(mode="wrong", interrupt_at=2),
                  command_runner=StubRunner())
    a1_path = next(iter(set((store / "reports").glob("bench-*.json"))
                        - before_a1))
    partial_a1 = json.loads(a1_path.read_text())
    assert partial_a1.get("incomplete"), "the killed run's file is marked"
    assert "in progress" in partial_a1["incomplete"] \
        or "interrupted" in partial_a1["incomplete"]
    assert "INCOMPLETE" in rr(partial_a1)

    spy_writes: list[dict] = []
    real_write = bench_module._write_report_files

    def _spy_write(report_path_, rendered_path_, report_):
        spy_writes.append(json.loads(json.dumps(report_)))
        return real_write(report_path_, rendered_path_, report_)

    monkeypatch_a1 = pytest.MonkeyPatch()
    monkeypatch_a1.setattr(bench_module, "_write_report_files", _spy_write)
    try:
        result_a1 = bench_run(env.factory_root, "fixture", repeats=2,
                              items=["BENCH-02"],
                              session_runner=Seats110(mode="wrong"),
                              command_runner=StubRunner())
    finally:
        monkeypatch_a1.undo()
    assert len(spy_writes) >= 2, "one incremental write per replay"
    assert spy_writes[0].get("incomplete"), \
        "the mid-run write must say the report is not finished"
    assert "in progress" in spy_writes[0]["incomplete"], \
        spy_writes[0]["incomplete"]
    assert not spy_writes[-1].get("incomplete"), \
        "the final write of a finished run drops the mark"
    assert not result_a1["report"].get("incomplete")

    # A2: a resume started while a FRESH run holds the report refuses.
    concurrent: list[str] = []

    def _spy_probe(report_path_, rendered_path_, report_):
        # Write first, then probe: the concurrent resume sees the
        # on-disk (in-progress-marked) report and must refuse on the
        # lock the fresh run holds.
        result_ = real_write(report_path_, rendered_path_, report_)
        try:
            bench_run(env.factory_root, "fixture",
                      resume=report_path_,
                      session_runner=Seats110(mode="wrong"),
                      command_runner=StubRunner())
            concurrent.append("RESUME RAN concurrently")
        except BenchError as exc:
            concurrent.append(str(exc))
        return result_

    monkeypatch_a2 = pytest.MonkeyPatch()
    monkeypatch_a2.setattr(bench_module, "_write_report_files", _spy_probe)
    try:
        bench_run(env.factory_root, "fixture", repeats=1,
                  items=["BENCH-02"],
                  session_runner=Seats110(mode="wrong"),
                  command_runner=StubRunner())
    finally:
        monkeypatch_a2.undo()
    assert concurrent and all(
        "another bench run holds" in msg for msg in concurrent), concurrent

    # B8: the fingerprint covers what the replay starts from — a moved
    # base is a different task.
    with pytest.raises(KeyboardInterrupt):
        bench_run(env.factory_root, "fixture", repeats=2,
                  items=["BENCH-01"],
                  session_runner=Seats110(mode="wrong", interrupt_at=2),
                  command_runner=StubRunner())
    b8_path = sorted((store / "reports").glob("bench-*.json"))[-1]
    b8_doc = json.loads((store / "BENCH-01" / "item.json").read_text())
    saved_base = b8_doc["base"]
    b8_doc["base"] = env.merge_sha if hasattr(env, "merge_sha") else saved_base
    if b8_doc["base"] != saved_base:
        (store / "BENCH-01" / "item.json").write_text(
            json.dumps(b8_doc, indent=2) + "\n")
        with pytest.raises(BenchError) as caught:
            bench_run(env.factory_root, "fixture", repeats=2,
                      resume=b8_path,
                      session_runner=Seats110(mode="wrong"),
                      command_runner=StubRunner())
        assert "BENCH-01" in str(caught.value)
        b8_doc["base"] = saved_base
        (store / "BENCH-01" / "item.json").write_text(
            json.dumps(b8_doc, indent=2) + "\n")

    # With the lock released the resume completes the one measurement.
    final = bench_run(env.factory_root, "fixture", repeats=2,
                      resume=report_path,
                      session_runner=_CountingSeats([]),
                      command_runner=StubRunner())
    assert sorted((i["slot"], r["repeat"])
                  for i in final["report"]["items"]
                  for r in i["repeats"]) == [("BENCH-01", 1), ("BENCH-01", 2)]
    assert not final["report"].get("incomplete")
