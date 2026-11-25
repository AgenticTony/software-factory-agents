"""F-104 — replay real past tasks to measure the implementer.

The benchmark a model swap is judged on (the spec's ACs, one test each):

* **AC-1** — an item proves itself at add: hidden tests fail on base,
  pass on the merged head (three times), the contract never quotes the
  merged diff, and the store is schema-validated on load.
* **AC-2** — the future is unreachable: the sanitized origin and the
  session's own clone hold no commit from base..head, no remote beyond
  the sanitized local origin, no tag; the hidden test text appears in
  no built prompt and no run artefact before scoring; ids, branches and
  commit messages run on opaque BENCH-NN slots.
* **AC-3** — the replay is the real pipeline (``_run_task`` with
  publishing replaced by scoring): a correct scripted fix scores a
  clean merge, a fix that satisfies its own L1 proof but not the hidden
  tests scores a fail, an escalation is recorded as such and is never a
  clean merge, and nothing touches GitHub or the project repo.
* **AC-4** — comparison is the point: k/N per item, yield, cost per
  clean merge, the models that ran, ``--against`` lists regressions
  first, and a ``--role`` override is recorded and never persisted.
* **AC-5** — the swap rule is written down: the README and the registry
  docs name the bench-before-swap rule, and ``factory doctor`` notes a
  registry implementer the latest bench report did not run.

Scripted seats, real git, a real base→head history — never a paid
session, never JobFinderOS.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from _support.f104 import (
    HIDDEN_MARKER,
    HIDDEN_TEST,
    PR_NUMBER,
    QUOTE_CRITERION,
    QUOTE_FIX_IMPL,
    SPEC_DOC,
    TASK_ID,
    Env104,
    Seats,
)

from factory.bench import (
    BenchError,
    bench_add,
    bench_run,
    compare_reports,
    load_items,
    render_report,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True,
    ).stdout.strip()


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    ).returncode == 0


class CountingRunner:
    """A command runner that records every command it executes."""

    def __init__(self):
        from factory.gates import run_command

        self._run = run_command
        self.calls: list[tuple[str, str]] = []

    def __call__(self, name, command, repo, timeout_sec):
        self.calls.append((name, command))
        return self._run(name, command, repo, timeout_sec)


# ------------------------------------------------------------------ AC-1 --


def test_ac_1(tmp_path: Path) -> None:
    env = Env104(tmp_path)
    runner = CountingRunner()

    # A valid item adds: slot, store layout, hidden file verbatim.
    out = bench_add(env.factory_root, "fixture", TASK_ID,
                    command_runner=runner)
    slot = out["slot"]
    assert slot == "BENCH-01", out
    item_dir = env.factory_root / "bench" / "fixture" / slot
    assert (item_dir / "item.json").is_file()
    hidden = item_dir / "hidden" / "tests" / "test_hidden.py"
    assert hidden.read_text() == HIDDEN_TEST
    item = out["item"]
    assert item.source_task == TASK_ID
    assert item.source_pr == PR_NUMBER
    assert item.base == env.base_sha
    assert item.head == env.head_sha

    # The item proves itself: red once on base, green THREE times on
    # head (the flake guard) — the add itself ran those commands.
    greens = [name for name, _ in runner.calls
              if name.startswith("green:")]
    reds = [name for name, _ in runner.calls if name.startswith("red:")]
    assert len(reds) == 1 and len(greens) == 3, runner.calls

    # Schema-validated on load; a second add of the same head refuses.
    assert [i.slot for i in load_items(env.factory_root, "fixture")] \
        == [slot]
    with pytest.raises(BenchError, match="already an item"):
        bench_add(env.factory_root, "fixture", TASK_ID)

    # A PR number resolves the same task.
    with pytest.raises(BenchError, match="already an item"):
        bench_add(env.factory_root, "fixture", str(PR_NUMBER))

    # An unmerged task refuses, named.
    env2 = Env104(tmp_path / "unmerged")
    env2.task["links"].pop("merged")
    env2.queue_path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [env2.task]},
        indent=2) + "\n")
    with pytest.raises(BenchError, match="merged"):
        bench_add(env2.factory_root, "fixture", TASK_ID)

    # Hidden tests that PASS on base refuse, named (they prove nothing).
    env3 = Env104(
        tmp_path / "passesbase",
        hidden_test="from impl import answer\n"
                    "\n"
                    "\n"
                    "def test_ac_1():\n"
                    "    assert isinstance(answer(), int)\n")
    with pytest.raises(BenchError, match="base"):
        bench_add(env3.factory_root, "fixture", TASK_ID)

    # Hidden tests that FAIL on the merged head refuse, named.
    env4 = Env104(
        tmp_path / "failshead",
        hidden_test="from impl import answer\n"
                    "\n"
                    "\n"
                    "def test_ac_1():\n"
                    "    assert answer() == -1\n")
    with pytest.raises(BenchError, match="merged head"):
        bench_add(env4.factory_root, "fixture", TASK_ID)

    # A contract that QUOTES the merged diff refuses (F-99's rule). The
    # quote check runs before the proofs, so the refusal names it even
    # though this item's hidden test would also fail on the head.
    from factory.contract import acceptance_hash

    env5 = Env104(tmp_path / "quotes", answer_impl=QUOTE_FIX_IMPL)
    env5.task["acceptance"][0]["text"] = QUOTE_CRITERION
    env5.task["acceptance_hash"] = acceptance_hash(
        env5.task["acceptance"])
    env5.queue_path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [env5.task]},
        indent=2) + "\n")
    with pytest.raises(BenchError, match="quotes the merged diff"):
        bench_add(env5.factory_root, "fixture", TASK_ID)

    # A hidden file the merged head does not hold refuses, named.
    env6 = Env104(tmp_path / "gonefile")
    env6.task["acceptance"][0]["check"]["ref"] = \
        "tests/test_gone.py::test_ac_1"
    env6.task["acceptance_hash"] = acceptance_hash(
        env6.task["acceptance"])
    env6.queue_path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [env6.task]},
        indent=2) + "\n")
    with pytest.raises(BenchError, match="test_gone"):
        bench_add(env6.factory_root, "fixture", TASK_ID)

    # A corrupted store refuses at load, named.
    (item_dir / "item.json").write_text('{"slot": "BENCH-01"}\n')
    with pytest.raises(BenchError, match="schema"):
        load_items(env.factory_root, "fixture")

    # A merge-commit merge adds (A3): the pushed commits ARE on main, so
    # plain merge-base would return the head itself; the base is the
    # fork point with main's pre-merge tip.
    env7 = Env104(tmp_path / "mergecommit", merge="merge")
    out7 = bench_add(env7.factory_root, "fixture", TASK_ID)
    pre_merge_tip = _git(env7.repo, "rev-parse", f"{env7.merge_sha}^1")
    assert out7["item"].base == pre_merge_tip
    assert out7["item"].base == env7.base_sha
    assert out7["item"].head == env7.head_sha

    # A deleted task branch adds (A2): the branch GitHub cleans up is
    # gone from the local repo; the pushed-head commit is the
    # precondition, and the proofs fetch it explicitly.
    env8 = Env104(tmp_path / "nodebranch", merge="merge",
                  keep_branch=False)
    assert not _git_ok(env8.repo, "rev-parse", "--verify", "--quiet",
                       f"refs/heads/{env8.branch}")
    out8 = bench_add(env8.factory_root, "fixture", TASK_ID)
    assert out8["item"].head == env8.head_sha

    # The same, squash-merged: the head is an unreachable object once
    # the branch is gone — still provable, still added.
    env9 = Env104(tmp_path / "nodebranchsquash", keep_branch=False)
    out9 = bench_add(env9.factory_root, "fixture", TASK_ID)
    assert out9["item"].head == env9.head_sha


# ------------------------------------------------------------------ AC-2 --


def test_ac_2(tmp_path: Path, monkeypatch) -> None:
    env = Env104(tmp_path)
    bench_add(env.factory_root, "fixture", TASK_ID)
    seats = Seats(mode="good")
    out = bench_run(env.factory_root, "fixture", session_runner=seats,
                    keep=True)
    report = out["report"]
    assert report["items"][0]["clean"] == 1

    # The sanitized origin: main at the base plus ONLY the replay's own
    # pushed bench branch, no remote, no tags, and no commit from
    # base..head anywhere in its object store.
    origin = Path(out["run_root"]) / "origin"
    refnames = [
        line.strip() for line in
        _git(origin, "for-each-ref", "--format=%(refname)").splitlines()
        if line.strip()]
    assert all(
        ref == "refs/heads/main"
        or ref.startswith("refs/heads/factory/bench-")
        for ref in refnames), refnames
    assert _git(origin, "rev-parse", "main") == env.base_sha
    assert _git(origin, "remote") == ""
    future = _git(env.repo, "rev-list", f"{env.base_sha}..{env.head_sha}"
                  ).split()
    assert future, "fixture must hold a real base..head future"
    origin_objects = _git(origin, "cat-file", "--batch-all-objects",
                          "--batch-check")
    for sha in future:
        assert sha not in origin_objects

    # The session's own clone (the hostile probe ran inside it, before
    # the session wrote anything): no future object, no remote beyond
    # the sanitized local origin, no tag, and NOTHING of the hidden
    # tests anywhere in the working tree or the object store.
    assert seats.probe, "the hostile session probe never ran"
    for probe in seats.probe:
        for sha in future:
            assert sha not in probe["objects"]
        assert str(env.repo) not in probe["remotes"], probe["remotes"]
        assert str(env.remote) not in probe["remotes"], probe["remotes"]
        assert HIDDEN_MARKER not in probe["grep_stdout"]
        assert HIDDEN_MARKER not in probe["objects"]
        assert "refs/tags/" not in probe["refs"], probe["refs"]

    # The implementer's prompt is built before any work exists: it
    # carries nothing of the hidden tests, and the real task id never
    # reaches a seat — ids and branches run on BENCH-NN slots. (The
    # REVIEWER prompts legitimately echo the implementer's own test
    # output through the gate reports; the decisive prompt-leak probe
    # is the wrong-mode run below, where the implementer wrote a
    # DIFFERENT test, so any hidden-text hit would be the parent's.)
    for seat, prompt, _, _ in seats.calls:
        assert TASK_ID not in prompt
    implementer_prompt = seats.prompts("implementer")[0]
    assert HIDDEN_MARKER not in implementer_prompt
    assert "BENCH-01" in implementer_prompt
    assert "factory/bench-01-" in implementer_prompt

    # The run's own pre-session leak check passed (bench_run returned),
    # and the scoring clone is gone: the hidden tests' only presence on
    # disk is the item store.
    run_root = Path(out["run_root"])
    assert not (run_root / "score").exists()

    # The decisive prompt probe: a replay whose implementer wrote its
    # OWN (different) test — the hidden text in ANY seat's prompt there
    # could only have come from the parent.
    wrong_seats = Seats(mode="wrong")
    wrong = bench_run(env.factory_root, "fixture",
                      session_runner=wrong_seats)
    assert wrong["report"]["items"][0]["repeats"][0]["hidden_pass"] \
        is False
    for seat, prompt, _, _ in wrong_seats.calls:
        assert HIDDEN_MARKER not in prompt, seat

    # A8's base-relative rule: a hidden assert line that already lives
    # in the BASE tree (here the spec doc, in the real world a sibling
    # test file) is NOT a leak — the real run saw the base too — so the
    # replay proceeds.
    basemarker = Env104(
        tmp_path / "basemarker",
        spec_doc=SPEC_DOC + f"\nExample: `assert answer() == 42  # "
        f"{HIDDEN_MARKER}`\n")
    bench_add(basemarker.factory_root, "fixture", TASK_ID)
    base_seats = Seats(mode="good")
    base_out = bench_run(basemarker.factory_root, "fixture",
                         session_runner=base_seats)
    assert base_out["report"]["items"][0]["clean"] == 1
    assert base_seats.calls

    # The leak check itself is load-bearing (A5/A8): when a PARENT-BUILT
    # artefact — here the scratch queue's task, via the _bench_task
    # seam — carries a hidden assert line the base does NOT hold, the
    # replay refuses with "leak:" before any session starts. Since A9,
    # a refused replay is RECORDED (an errored item in the report, a
    # non-zero exit), never silently skipped — and no seat ever runs.
    from factory import bench as bench_module

    real_task = bench_module._bench_task
    marker_line = f"assert answer() == 42  # {HIDDEN_MARKER}"

    def task_with_marker(item):
        task = real_task(item)
        task["title"] = task["title"] + f" — {marker_line}"
        return task

    monkeypatch.setattr(bench_module, "_bench_task", task_with_marker)
    leak_seats = Seats(mode="good")
    leak_out = bench_run(env.factory_root, "fixture",
                         session_runner=leak_seats)
    record = leak_out["report"]["items"][0]["repeats"][0]
    assert "leak:" in record["factory_outcome"], record
    assert record["clean"] is False
    assert leak_seats.calls == []
    assert leak_out["report"]["errors"] == ["BENCH-01"]
    monkeypatch.undo()


# ------------------------------------------------------------------ AC-3 --


def test_ac_3(tmp_path: Path) -> None:
    env = Env104(tmp_path)
    bench_add(env.factory_root, "fixture", TASK_ID)
    repo_head_before = _git(env.repo, "rev-parse", "HEAD")
    remote_branches_before = env.remote_branches()

    # A correct scripted fix: a clean merge, with the factory's own
    # numbers on the record.
    good = bench_run(env.factory_root, "fixture",
                     session_runner=Seats(mode="good"))
    item = good["report"]["items"][0]
    repeat = item["repeats"][0]
    assert repeat["hidden_pass"] is True
    assert repeat["factory_outcome"] == "would_publish"
    assert repeat["clean"] is True
    assert repeat["rounds"] >= 1
    assert repeat["turns"] > 0
    assert repeat["input_tokens"] > 0
    assert repeat["cost_usd"] is not None
    assert any(role.startswith("implementer")
               for role in good["report"]["roles"])

    # A fix that satisfies its own L1 proof but not the hidden tests
    # scores a fail — exactly the model that gamed the factory's tests.
    wrong = bench_run(env.factory_root, "fixture",
                      session_runner=Seats(mode="wrong"))
    item = wrong["report"]["items"][0]
    assert item["repeats"][0]["hidden_pass"] is False
    assert item["repeats"][0]["clean"] is False

    # An escalated attempt is recorded as such and is NEVER a clean
    # merge, whatever the hidden tests say on its kept head.
    blocked = bench_run(env.factory_root, "fixture",
                        session_runner=Seats(mode="good", reviews="block"))
    item = blocked["report"]["items"][0]
    repeat = item["repeats"][0]
    assert repeat["factory_outcome"] != "would_publish"
    assert repeat["factory_outcome"].startswith("escalated:")
    assert repeat["clean"] is False

    # Nothing touched GitHub or the project repo: the bare remote holds
    # exactly the branches it held, the repo's HEAD never moved, and no
    # factory PR was opened about a bench replay.
    assert env.remote_branches() == remote_branches_before
    assert _git(env.repo, "rev-parse", "HEAD") == repo_head_before
    assert _git(env.repo, "status", "--porcelain") == ""


# ------------------------------------------------------------------ AC-4 --


def test_ac_4(tmp_path: Path) -> None:
    env = Env104(tmp_path)
    bench_add(env.factory_root, "fixture", TASK_ID)

    # Repeats default to 1 and scale: N records per item.
    one = bench_run(env.factory_root, "fixture",
                    session_runner=Seats(mode="good"))
    assert one["report"]["repeats"] == 1
    two = bench_run(env.factory_root, "fixture", repeats=2,
                    session_runner=Seats(mode="good"))
    item = two["report"]["items"][0]
    assert len(item["repeats"]) == 2 and item["clean"] == 2

    # The report carries yield (clean merges per unit cost), cost per
    # clean merge, k/N per item and the models that ran per role.
    text = render_report(two["report"])
    assert "2/2" in text
    assert "yield" in text
    assert "cost per clean merge" in text

    # --against lists regressions (pass -> fail) first.
    against = bench_run(env.factory_root, "fixture",
                        session_runner=Seats(mode="wrong"),
                        against=two["report_path"])
    report = against["report"]
    assert report["against"]["regressions"] == ["BENCH-01"]
    text = render_report(report)
    regressions_at = text.index("Regressions")
    assert "BENCH-01" in text[regressions_at:]

    # compare_reports sorts every section by slot, regressions first.
    before = {
        "items": [
            {"slot": "BENCH-02", "clean": 1, "judged": 1},
            {"slot": "BENCH-01", "clean": 1, "judged": 1},
            {"slot": "BENCH-03", "clean": 0, "judged": 1},
        ]
    }
    after = {
        "items": [
            {"slot": "BENCH-03", "clean": 1, "judged": 1},
            {"slot": "BENCH-02", "clean": 0, "judged": 1},
            {"slot": "BENCH-01", "clean": 0, "judged": 1},
        ]
    }
    diff = compare_reports(before, after)
    assert diff["regressions"] == ["BENCH-01", "BENCH-02"]
    assert diff["improvements"] == ["BENCH-03"]
    assert diff["unchanged"] == []

    # A --role override reaches the sessions, is named in the report,
    # and is never written anywhere.
    config_before = (env.factory_root / "config.yaml").exists() and \
        (env.factory_root / "config.yaml").read_bytes()
    profile_before = (env.repo / ".factory" / "project.yaml").read_bytes()

    def item_store() -> list:
        return sorted(
            (p, p.read_bytes()) for p in
            (env.factory_root / "bench" / "fixture").glob("BENCH-*/**/*")
            if p.is_file())

    store_before = item_store()
    override = bench_run(env.factory_root, "fixture",
                         session_runner=Seats(mode="good"),
                         role_overrides={"implementer": "glm-5.4"},
                         keep=True)
    assert override["report"]["role_overrides"] == \
        {"implementer": "glm-5.4"}
    queue = json.loads(
        (Path(override["run_root"]) / "queue" / "tasks.json").read_text())
    provider = queue["tasks"][0]["attempts"][-1]["provider"]
    assert provider["model"] == "glm-5.4", provider
    if config_before:
        assert (env.factory_root / "config.yaml").read_bytes() == \
            config_before
    assert (env.repo / ".factory" / "project.yaml").read_bytes() == \
        profile_before
    assert item_store() == store_before


# ------------------------------------------------------------------ AC-5 --


def test_ac_5(tmp_path: Path) -> None:
    import yaml

    from factory import doctor

    env = Env104(tmp_path)
    bench_add(env.factory_root, "fixture", TASK_ID)

    # A bumped registry (the alias now feeds glm-5.4) as the owner's
    # config: the full standard stack, alias moved.
    glm = {"kind": "zai", "model": "glm-latest"}
    registry = {
        "aliases": {"glm-latest": "glm-5.4"},
        "providers": {"zai": {"origin": "cn"},
                      "anthropic": {"origin": "us"}},
        "stacks": {"standard": {"contained": False, "roles": {
            "implementer": glm, "reviewer.conformance": glm,
            "reviewer.hunt": glm, "verifier": glm, "importer": glm,
            "final_review.fix": glm, "final_review.strong": glm}}},
    }
    (env.factory_root / "config.yaml").write_text(yaml.safe_dump(
        {"models": registry}, sort_keys=False))

    def findings(root: Path) -> list[str]:
        reports = doctor.factory_report(root)
        return [f.message for r in reports for f in r.findings
                if f.check == "bench-swap"]

    def bench_report(stamp: str, implementer: str) -> None:
        env.write_report(f"bench-{stamp}.json", {
            "kind": "bench", "project": "fixture", "stamp": stamp,
            "repeats": 1, "role_overrides": {},
            "roles": {"implementer": [implementer]}, "items": [],
            "totals": {"clean": 0, "judged": 0},
        })

    # A report on the OLD implementer: the note appears after the alias
    # bump, naming both models.
    bench_report("20261124T100000Z", "glm-5.3")
    notes = findings(env.factory_root)
    assert len(notes) == 1, notes
    assert "glm-5.3" in notes[0] and "glm-5.4" in notes[0]

    # A NEWER bench report on the new model clears it.
    bench_report("20261124T120000Z", "glm-5.4")
    assert findings(env.factory_root) == []

    # The swap rule is written down: README's registry section and the
    # config docstring (the F-100 registry docs) both name it.
    readme = (Path(__file__).resolve().parent.parent / "README.md"
              ).read_text()
    assert "factory bench run" in readme
    assert "implementer model change" in readme
    config_doc = (Path(__file__).resolve().parent.parent / "factory"
                  / "config.py").resolve().read_text()
    assert "factory bench run" in config_doc


# ---------------------------------------------- round 1: A1/A4 + the shapes --


def test_cli_run_runs_sessions(tmp_path: Path, monkeypatch) -> None:
    """A1: `factory bench run` defaults to the REAL session runner —
    the owner's paid baseline must run models, not NoneType. The CLI
    path reaches the session seam (run_session_sandboxed, called by its
    module-global name) and the interpreter probe fires with it."""
    from factory import bench
    from factory.cli import main

    env = Env104(tmp_path)
    bench_add(env.factory_root, "fixture", TASK_ID)
    seats = Seats(mode="good")
    monkeypatch.setattr(bench, "run_session_sandboxed", seats,
                        raising=False)
    probed: list[dict] = []
    monkeypatch.setattr(bench, "probe_session_interpreter",
                        lambda profile, srt_bin_path=None:
                        probed.append({"profile": profile}),
                        raising=False)
    code = main(["bench", "run", "fixture",
                 "--factory-root", str(env.factory_root)])
    assert code == 0
    assert seats.calls, "the CLI run never reached a session seat"
    assert probed, "the interpreter probe never ran with the real runner"
    reports = sorted((env.factory_root / "bench" / "fixture"
                      / "reports").glob("bench-*.json"))
    report = json.loads(reports[-1].read_text())
    assert report["items"][0]["repeats"][0]["factory_outcome"] == \
        "would_publish"
    assert report["items"][0]["clean"] == 1


def test_scoring_uses_sandboxed_runner(tmp_path: Path,
                                       monkeypatch) -> None:
    """A4: the hidden-test scoring and the add-time proofs run the
    project's commands through the SANDBOXED gate runner by default —
    the replay head is agent code. The seam is bench.sandboxed_runner,
    called by its module-global name; command_runner stays the test
    override."""
    from factory import bench
    from factory.gates import run_command

    calls: list[Path] = []

    def recording_runner(run_dir, settings_path, **kwargs):
        calls.append(Path(run_dir))

        def _run(name, command, repo, timeout_sec):
            return run_command(name, command, repo, timeout_sec)

        return _run

    monkeypatch.setattr(bench, "sandboxed_runner", recording_runner,
                        raising=False)
    env = Env104(tmp_path)
    bench_add(env.factory_root, "fixture", TASK_ID)
    assert calls, "the add-time proofs never built a sandboxed runner"
    add_count = len(calls)
    out = bench_run(env.factory_root, "fixture", session_runner=Seats(
        mode="good"))
    assert out["report"]["items"][0]["repeats"][0]["hidden_pass"] is True
    assert len(calls) > add_count, \
        "the scoring path never built a sandboxed runner"
    # The scoring runner confined a clone under the replay's scratch;
    # the add-time ones ran under the bench's disposable .add-* scratch
    # (both cleaned by design once each phase finished).
    for run_dir in calls[add_count:]:
        assert str(out["run_root"]) in str(run_dir), run_dir


def test_merge_shapes_replay(tmp_path: Path) -> None:
    """The goal-level trio, end to end: a squash merge, a merge commit
    and a deleted-branch squash item all add, replay through the real
    pipeline with NO future object reachable, and score a clean merge."""
    shapes = {
        "squash": dict(merge="squash"),
        "merge-commit": dict(merge="merge"),
        "deleted-branch-squash": dict(merge="squash", keep_branch=False),
    }
    for name, kwargs in shapes.items():
        env = Env104(tmp_path / name, **kwargs)
        bench_add(env.factory_root, "fixture", TASK_ID)
        seats = Seats(mode="good")
        out = bench_run(env.factory_root, "fixture",
                        session_runner=seats, keep=True)
        item = out["report"]["items"][0]
        assert item["repeats"][0]["factory_outcome"] == "would_publish"
        assert item["repeats"][0]["hidden_pass"] is True, name
        assert item["clean"] == 1, name
        # No future object anywhere the session could reach.
        origin = Path(out["run_root"]) / "origin"
        future = _git(env.repo, "rev-list",
                      f"{env.base_sha}..{env.head_sha}").split()
        objects = _git(origin, "cat-file", "--batch-all-objects",
                       "--batch-check")
        for sha in future:
            assert sha not in objects, (name, sha)
        for probe in seats.probe:
            for sha in future:
                assert sha not in probe["objects"], (name, sha)


# ------------------------------------------------------ round 2: A6 and A7 --

# A real setup command: writes the module the hidden test imports, and
# fails when the implementer broke the setup path (the break_setup arm).
SETUP_COMMAND = (
    f'{shlex.quote(sys.executable)} -c "import os, pathlib, sys; '
    "pathlib.Path('generated.py').write_text('EXPECTED = 42'); "
    "sys.exit(3) if os.path.exists('break_setup') else None\""
)
NEEDS_SETUP_TEST = (
    "from generated import EXPECTED\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    "    from impl import answer\n"
    "    assert answer() == EXPECTED\n"
)


def test_setup_runs_in_proofs_and_scoring(tmp_path: Path) -> None:
    """A6: the add-time proofs and the hidden-test scoring run the
    profile's `commands.setup` — a project whose tests need the
    environment setup builds (a venv, a generated module) would refuse
    every item at add and score every replay FAIL without it. The
    fixture is merge-commit shaped with a real setup, so the proof, the
    replay's own F-73 setup and the scoring all go through setup."""
    env = Env104(
        tmp_path, merge="merge", setup_command=SETUP_COMMAND,
        setup_outputs=["generated.py"], hidden_test=NEEDS_SETUP_TEST)
    out = bench_add(env.factory_root, "fixture", TASK_ID)
    assert out["slot"] == "BENCH-01"

    # Scoring with setup: the hidden test imports what setup generates.
    good = bench_run(env.factory_root, "fixture",
                     session_runner=Seats(mode="good"))
    repeat = good["report"]["items"][0]["repeats"][0]
    assert repeat["hidden_pass"] is True, repeat
    assert repeat["clean"] is True

    # An implementer that broke the setup path breaks the REPLAY's own
    # setup (the factory's F-73/L0 rule): the attempt escalates
    # setup_failed and is never a clean merge.
    broken = bench_run(env.factory_root, "fixture",
                       session_runner=Seats(mode="good", break_setup=True))
    repeat = broken["report"]["items"][0]["repeats"][0]
    assert repeat["factory_outcome"] == "escalated:setup_failed"
    assert repeat["clean"] is False

    # The scoring-side failure arm: a setup that fails AT THE SCORE
    # CLONE records hidden_pass False with the note, never a crash.
    from factory.gates import CommandResult, run_command

    def failing_score_setup(name, command, repo, timeout_sec):
        if name.startswith("setup:score"):
            return CommandResult(name=name, command=command, exit_code=9,
                                 output="scripted setup failure")
        return run_command(name, command, repo, timeout_sec)

    scored = bench_run(env.factory_root, "fixture",
                       session_runner=Seats(mode="good"),
                       command_runner=failing_score_setup)
    repeat = scored["report"]["items"][0]["repeats"][0]
    assert repeat["hidden_pass"] is False
    assert repeat["hidden_note"].startswith("setup failed"), repeat
    assert repeat["factory_outcome"] == "would_publish"

    # A setup that fails outright refuses the item at add, named.
    failing = Env104(
        tmp_path / "failing", merge="merge",
        setup_command=f"{shlex.quote(sys.executable)} -c "
                      '"import sys; sys.exit(4)"')
    with pytest.raises(BenchError, match="setup failed"):
        bench_add(failing.factory_root, "fixture", TASK_ID)


def test_stale_default_refuses(tmp_path: Path) -> None:
    """A7: a local default branch behind its tracking ref refuses the
    add, naming the count — the base would be derived from stale
    history and the replay would start in the wrong place. After a
    fast-forward the add goes through."""
    env = Env104(tmp_path)
    # two back: the tracking ref was fetched at the post-merge commit
    # (before the queue commit), so local main lands one BEHIND it
    _git(env.repo, "reset", "--hard", "HEAD~2")
    # the queue file is tracked: reset removed it from the tree — put
    # the (identical) record back so the add can read the task
    env.queue_path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [env.task]},
        indent=2) + "\n")
    with pytest.raises(BenchError, match="1 commit\\(s\\) behind"):
        bench_add(env.factory_root, "fixture", TASK_ID)
    _git(env.repo, "merge", "--ff-only", "refs/remotes/origin/main")
    out = bench_add(env.factory_root, "fixture", TASK_ID)
    assert out["item"].base == env.base_sha


# ------------------------------------------------------ round 3: A9 and N1 --

def test_replay_errors_are_recorded(tmp_path: Path, monkeypatch) -> None:
    """A9: one failing replay never aborts the run — the error is
    recorded (never clean, hidden_pass False), the paid replays already
    done survive in the report, and the CLI exits non-zero naming the
    errored slots."""
    from factory import bench as bench_module
    from factory.cli import main

    env1 = Env104(tmp_path / "one")
    env2 = Env104(tmp_path / "two")
    bench_add(env1.factory_root, "fixture", TASK_ID)
    # A second item in the SAME store, from the second fixture's repo
    # (a hand-joined store: slot bumped, repo pointed at env2).
    item2 = bench_add(env2.factory_root, "fixture", TASK_ID)
    import shutil as _shutil

    src = env2.factory_root / "bench" / "fixture" / item2["slot"]
    dst = env1.factory_root / "bench" / "fixture" / "BENCH-02"
    _shutil.copytree(src, dst)
    doc = json.loads((dst / "item.json").read_text())
    doc["slot"] = "BENCH-02"
    (dst / "item.json").write_text(json.dumps(doc, indent=2))

    seats = Seats(mode="good")
    monkeypatch.setattr(bench_module, "run_session_sandboxed", seats,
                        raising=False)
    real_sanitize = bench_module.sanitize_origin
    calls = {"n": 0}

    def sanitize_fails_first(source, dest, base, default_branch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BenchError("scripted sanitize failure")
        return real_sanitize(source, dest, base, default_branch)

    monkeypatch.setattr(bench_module, "sanitize_origin",
                        sanitize_fails_first)
    code = main(["bench", "run", "fixture",
                 "--factory-root", str(env1.factory_root)])
    assert code == 1, "an errored replay must exit non-zero"
    reports = sorted((env1.factory_root / "bench" / "fixture"
                      / "reports").glob("bench-*.json"))
    report = json.loads(reports[-1].read_text())
    by_slot = {item["slot"]: item for item in report["items"]}
    assert set(by_slot) == {"BENCH-01", "BENCH-02"}
    errored = by_slot["BENCH-01"]["repeats"][0]
    assert errored["factory_outcome"].startswith("error:")
    assert errored["clean"] is False
    assert errored["hidden_pass"] is False
    assert "scripted sanitize failure" in errored["factory_outcome"]
    assert by_slot["BENCH-02"]["clean"] == 1


def test_hidden_path_validation(tmp_path: Path) -> None:
    """N1: a hidden file path that is absolute or climbs (..) refuses
    at add and on load — the path is written under hidden/ and into the
    score clone."""
    from factory.contract import acceptance_hash

    env = Env104(tmp_path / "climb")
    env.task["acceptance"][0]["check"]["ref"] = \
        "../evil.py::test_ac_1"
    env.task["acceptance_hash"] = acceptance_hash(env.task["acceptance"])
    env.queue_path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [env.task]},
        indent=2) + "\n")
    # the refusal names the PATH-SHAPE reason, not just the path — a
    # "not on the merged head" refusal here would pass vacuously
    with pytest.raises(BenchError, match=r"climbs \(\.\.\)"):
        bench_add(env.factory_root, "fixture", TASK_ID)

    # The same shapes refuse at load: a hand-corrupted store.
    env2 = Env104(tmp_path / "store")
    out = bench_add(env2.factory_root, "fixture", TASK_ID)
    doc_path = out["item_dir"] / "item.json"
    doc = json.loads(doc_path.read_text())
    doc["hidden_files"] = ["/abs/evil.py"]
    doc_path.write_text(json.dumps(doc, indent=2) + "\n")
    with pytest.raises(BenchError, match="absolute"):
        load_items(env2.factory_root, "fixture")


def test_assertless_hidden_tests_refuse(tmp_path: Path) -> None:
    """N3 (my call: refuse): hidden tests with no assert lines give the
    leak check nothing to hunt — jest/vitest-shaped items refuse at
    add, named, rather than replaying unchecked."""
    env = Env104(
        tmp_path / "assertless",
        # red on base, green on head — but with no assert LINE, so no
        # leak marker can be computed from it
        hidden_test="from impl import answer\n"
                    "\n"
                    "\n"
                    "def test_ac_1():\n"
                    "    if answer() + 1 != 43:\n"
                    "        raise SystemExit('wrong answer')\n")
    with pytest.raises(BenchError, match="no assert lines"):
        bench_add(env.factory_root, "fixture", TASK_ID)
