"""Importer budget limits and abort salvage (F-12/F-13 step 3).

Three lost sessions on one import (``~/Desktop/jfos-factory``: 55 planning
docs, ~30 tasks) bought this file:

* the budgets were hard-coded (30 turns / 20 min) with no CLI override —
  the 2026-11-05 post-#94 run hit the wall clock with the self-check loop
  riding along and finished nothing;
* an aborted session's scratch — the one place the #94 self-check prompt
  tells the session to write ``$TMPDIR/proposal.json`` — is swept by
  ``cleanup_run`` on every exit path, so the abort deleted the session's
  only surviving work product.

Evidence the scaled defaults are derived from (not guessed):

* run ``20261105T081854Z-1c569f`` (55 docs, 30 tasks, no self-check):
  ``num_turns`` 56, 548 s, **completed** under ``max_turns`` 30. The
  bundled CLI's result counter increments per user-role message row
  (prompt + every tool-result batch), while the cap binds on the query
  loop's own turn count — 56 rows fit inside ≤30 counted turns, ~1 row
  per listed doc;
* the post-#94 run (same repo, plus the write/parse/shape self-check):
  >20 min, ``ended aborted: wall_clock``, nothing kept.
"""

import json
import subprocess
from pathlib import Path

import pytest
from _support.f63 import importer_fixture_approve

import factory.importer as importer
from factory.cli import main
from factory.importer import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_MIN,
    ImportRefused,
)
from factory.importer import (
    run_import as _run_import,
)
from factory.session import SessionRecord

# ------------------------------------------------------------- fixtures --


def run_import(repo, *args, **kwargs):
    """F-63: the real session path checks the profile against the owner
    ledger — approve this fixture's profile under the factory root this
    call uses (the importer's own default when none was passed, so a
    monkeypatched default is honoured), then delegate to the real one."""
    root = kwargs.get("factory_root") or importer.FACTORY_ROOT_DEFAULT
    if kwargs.get("session") is None:  # only the real path checks the ledger
        importer_fixture_approve(root, repo)
    return _run_import(repo, *args, **kwargs)

def _profile_yaml() -> str:
    """The smallest profile the project schema accepts (name ``proj``)."""
    return (
        "name: proj\n"
        "repo:\n"
        "  path: .\n"
        "  default_branch: main\n"
        "  remote: https://example.invalid/proj.git\n"
        "commands:\n"
        '  setup: "true"\n'
        '  test: "true"\n'
        "paths:\n"
        "  risk: {}\n"
        "  protected: []\n"
        "deploy:\n"
        "  mechanism: none\n"
        "  auto_on_merge: false\n"
        "live_checks: {}\n"
        "docs_allowlist: []\n"
    )


def _repo(tmp_path: Path, *, docs: int = 2) -> Path:
    """A fixture project (git repo, profile, ``docs`` planning docs)."""
    repo = tmp_path / "proj"
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True)
    for index in range(docs):
        (docs_dir / f"d{index:03d}.md").write_text(
            f"# Doc {index}\n\n## WO-{index}: thing {index}\n\n"
            f"Acceptance: thing {index} works.\n"
        )
    factory_dir = repo / ".factory"
    factory_dir.mkdir(exist_ok=True)
    (factory_dir / "project.yaml").write_text(_profile_yaml())
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "test@factory.local"],
        ["config", "user.name", "factory test"],
        ["add", "-A"],
        ["commit", "-m", "base"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)
    return repo


def _wo(tid: str = "WO-0") -> dict:
    """One proposed task: every field the queue schema requires."""
    return {
        "id": tid,
        "title": f"Work order {tid}",
        "source": "docs/d000.md",
        "kind": "build",
        "priority": "P1",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [
            {
                "id": "AC-1",
                "text": "it works",
                "check": {
                    "type": "test",
                    "ref": f"tests/test_{tid.lower()}.py::test_ac_1",
                },
            }
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }


def _proposal(*tasks: dict, project: str = "proj") -> str:
    """The importer session's output: one JSON object."""
    return json.dumps({"project": project, "tasks": list(tasks)})


def _patch_session(monkeypatch, record_factory):
    """Patch the real-session seam (run_session_sandboxed + seat token)
    with ``record_factory(kwargs) -> SessionRecord | raises``; returns the
    captured kwargs dict."""
    captured: dict = {}

    def fake(seat, prompt, run_dir, profile, **kwargs):
        captured.update(kwargs, seat=seat, run_dir=Path(run_dir))
        return record_factory(kwargs)

    monkeypatch.setattr("factory.importer.run_session_sandboxed", fake)
    monkeypatch.setattr("factory.importer.seat_token", lambda: "tok")
    return captured


def _completed(captured: dict) -> SessionRecord:
    return SessionRecord(
        seat="reviewer", status="completed", result=_proposal(_wo()),
    )


def _write_scratch_proposal(settings_path: Path, text: str) -> None:
    """Write ``$TMPDIR/proposal.json`` the way the sandboxed session would:
    through the scratch sibling of the srt settings file."""
    scratch = settings_path.with_name(settings_path.name + "-scratch") / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "proposal.json").write_text(text, encoding="utf-8")


def _saved_files(factory_root: Path) -> list[Path]:
    directory = factory_root / "import"
    return sorted(directory.iterdir()) if directory.is_dir() else []


# --------------------------------------- item 1: --max-turns / --timeout-min --


def test_flags_reach_the_session_call(tmp_path: Path, monkeypatch):
    """``run_import(max_turns=…, timeout_min=…)`` hands the sdk session the
    caller's numbers unchanged — the explicit override seam."""
    repo = _repo(tmp_path)
    captured = _patch_session(monkeypatch, _completed)
    run_import(repo, factory_root=tmp_path / "froot",
               max_turns=7, timeout_min=3.5)
    assert captured["max_turns"] == 7
    assert captured["timeout_min"] == 3.5


def test_cli_flags_reach_the_session_call(tmp_path: Path, monkeypatch):
    """``factory import --max-turns N --timeout-min M`` parses, threads
    through run_import into the session call (driven through main, the
    repo's CLI-test convention)."""
    repo = _repo(tmp_path)
    captured = _patch_session(monkeypatch, _completed)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    importer_fixture_approve(tmp_path / "froot", repo)  # F-63: real path
    rc = main(["import", "--repo", str(repo),
               "--factory-root", str(tmp_path / "froot"),
               "--max-turns", "9", "--timeout-min", "4.25"])
    assert rc == 0
    assert captured["max_turns"] == 9
    assert captured["timeout_min"] == 4.25


@pytest.mark.parametrize("flag,value", [
    ("--max-turns", "0"),
    ("--max-turns", "-3"),
    ("--max-turns", str(importer.TURN_CEILING + 1)),
    ("--timeout-min", "0"),
    ("--timeout-min", "-1.5"),
    ("--timeout-min", str(importer.TIMEOUT_MIN_CEILING + 1)),
])
def test_bad_flags_refuse_before_any_session(tmp_path, capsys, monkeypatch,
                                             flag, value):
    """A budget outside its bounds is a refusal (exit 1, stderr naming the
    flag) — and no session ever starts for it."""
    repo = _repo(tmp_path)
    _patch_session(
        monkeypatch,
        lambda kwargs: pytest.fail("the session must never start"),
    )

    def no_sdk(*args, **kwargs):
        raise AssertionError("sdk_import_session was called")

    monkeypatch.setattr("factory.importer.sdk_import_session", no_sdk)
    rc = main(["import", "--repo", str(repo), flag, value])
    assert rc == 1
    err = capsys.readouterr().err
    assert flag in err
    assert value in err


def test_run_import_refuses_bad_budget_before_the_session(tmp_path):
    """The library seam refuses the same bad values before the session —
    a caller that is not the CLI gets the same guard."""
    repo = _repo(tmp_path)

    def never(prompt: str) -> str:
        raise AssertionError("the injected session was called")

    for bad in ({"max_turns": 0}, {"max_turns": -1},
                {"max_turns": importer.TURN_CEILING + 1},
                {"timeout_min": 0.0}, {"timeout_min": -0.5},
                {"timeout_min": importer.TIMEOUT_MIN_CEILING + 1},
                {"timeout_min": float("inf")},
                {"timeout_min": float("nan")}):
        with pytest.raises(ImportRefused, match="turns|timeout"):
            run_import(repo, session=never, **bad)


# ------------------------------------------- item 2: scaled default budgets --


def test_scaled_budget_grows_with_the_doc_count():
    """A 55-doc repo gets a bigger default than a 3-doc repo, in both
    dimensions — the budget scales with the work."""
    big = importer.scaled_import_budget(55)
    small = importer.scaled_import_budget(3)
    assert big[0] > small[0]
    assert big[1] > small[1]


def test_scaled_budget_is_capped():
    """The scaling caps out: a monster repo cannot ask for unbounded
    turns or wall clock."""
    assert importer.scaled_import_budget(100_000) == (
        importer.TURN_CEILING, importer.TIMEOUT_MIN_CEILING,
    )


def test_scaled_budget_matches_the_evidence():
    """The formula pinned to the runs that bought it:

    * ``max_turns = 30 + docs`` (cap 200) — the 55-doc/30-task
      20261105T081854Z run completed inside a 30-turn loop cap while
      emitting 56 user-role rows (~1 per listed doc); one cap-turn per
      doc plus the 30-turn base covers the rows AND the #94 self-check
      rounds the base already proved sufficient without;
    * ``timeout_min = 20 + docs/5`` (cap 120) — that run took 548 s
      without the self-check and the post-#94 run burned past 20 min
      with it; 31 min for 55 docs is ~3.4× the observed no-self-check
      wall time.
    """
    assert importer.scaled_import_budget(55) == (85, 31.0)
    # small imports stay at today's defaults in practice
    assert importer.scaled_import_budget(0) == (DEFAULT_MAX_TURNS,
                                                DEFAULT_TIMEOUT_MIN)
    assert importer.scaled_import_budget(3) == (33, 20.6)


def test_default_budget_scales_into_the_session_call(tmp_path, monkeypatch):
    """No flags: the DEFAULT is scaled with the listed docs and reaches
    the session call — 55 docs -> 85 turns / 31 min."""
    repo = _repo(tmp_path, docs=55)
    captured = _patch_session(monkeypatch, _completed)
    run_import(repo, factory_root=tmp_path / "froot")
    assert captured["max_turns"] == 85
    assert captured["timeout_min"] == 31.0


def test_explicit_flags_override_the_scaled_default(tmp_path, monkeypatch):
    """Flags always win over the scaled default — each dimension on its
    own, the other keeps its scaled value."""
    repo = _repo(tmp_path, docs=55)
    captured = _patch_session(monkeypatch, _completed)
    run_import(repo, factory_root=tmp_path / "froot", max_turns=40)
    assert captured["max_turns"] == 40
    assert captured["timeout_min"] == 31.0

    captured2 = _patch_session(monkeypatch, _completed)
    run_import(repo, factory_root=tmp_path / "froot2", timeout_min=25.0)
    assert captured2["max_turns"] == 85
    assert captured2["timeout_min"] == 25.0


# ------------------------------------------------- item 3: salvage on abort --


def test_aborted_session_with_proposal_saves_it(tmp_path, monkeypatch):
    """The third lost session: wall_clock abort, scratch holds the
    proposal the self-check told it to write. The refusal names the saved
    file (redacted, 0600, under <factory-root>/import/) and the re-review
    command — and the copy happens BEFORE cleanup sweeps the scratch."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"
    proposal = _proposal(_wo("WO-1"), _wo("WO-2"))

    def aborts(kwargs):
        _write_scratch_proposal(kwargs["settings_path"], proposal)
        return SessionRecord(seat="reviewer", status="aborted",
                             reason="wall_clock")

    _patch_session(monkeypatch, aborts)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    assert "the importer session ended aborted: wall_clock" in message
    assert "its last proposal was saved to" in message
    assert "factory import --proposal" in message
    saved = _saved_files(root)
    assert len(saved) == 1
    assert saved[0].suffix == ".json"
    assert json.loads(saved[0].read_text(encoding="utf-8")) == json.loads(
        proposal)
    assert saved[0].stat().st_mode & 0o777 == 0o600
    # cleanup ran (the run dir is gone) yet the copy survived it
    runs = root / "runs" / "proj"
    assert list(runs.iterdir()) == [] if runs.is_dir() else True
    # the scratch itself is swept
    assert not (root / "sandbox").is_dir() or not any(
        (root / "sandbox").iterdir())


def test_aborted_session_without_proposal_says_so(tmp_path, monkeypatch):
    """No file in the scratch: the refusal says so explicitly — never a
    bare 'aborted' that leaves the owner guessing what survived."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"

    def aborts(kwargs):
        return SessionRecord(seat="reviewer", status="aborted",
                             reason="wall_clock")

    _patch_session(monkeypatch, aborts)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    assert "ended aborted: wall_clock" in message
    assert "no proposal was written before the abort" in message
    assert _saved_files(root) == []


def test_failed_session_saves_its_last_proposal_too(tmp_path, monkeypatch):
    """Any non-completed end salvages — a failed session (error_max_turns,
    reviewer mutation) holds its work exactly like an aborted one."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"

    def fails(kwargs):
        _write_scratch_proposal(kwargs["settings_path"], _proposal(_wo()))
        return SessionRecord(seat="reviewer", status="failed",
                             reason="error_max_turns")

    _patch_session(monkeypatch, fails)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    assert "ended failed: error_max_turns" in str(excinfo.value)
    assert "its last proposal was saved to" in str(excinfo.value)
    assert len(_saved_files(root)) == 1


def test_outer_timeout_salvages_the_proposal_too(tmp_path, monkeypatch):
    """The outer wall clock (runner hung past its grace) is an abort path
    like any other: the scratch proposal is copied out before cleanup."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"

    def hangs(kwargs):
        _write_scratch_proposal(kwargs["settings_path"], _proposal(_wo()))
        raise subprocess.TimeoutExpired(cmd=["srt"], timeout=1260.0)

    _patch_session(monkeypatch, hangs)
    with pytest.raises(ImportRefused) as excinfo:
        run_import(repo, factory_root=root)
    message = str(excinfo.value)
    assert "timed out after 1260s" in message
    assert "its last proposal was saved to" in message
    assert len(_saved_files(root)) == 1


def test_salvaged_proposal_imports_via_dash_proposal(tmp_path, monkeypatch,
                                                     capsys):
    """The salvaged file is the input `factory import --proposal` already
    serves — no new format, one paste from the refusal."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"

    def aborts(kwargs):
        _write_scratch_proposal(kwargs["settings_path"], _proposal(_wo()))
        return SessionRecord(seat="reviewer", status="aborted",
                             reason="wall_clock")

    _patch_session(monkeypatch, aborts)
    with pytest.raises(ImportRefused):
        run_import(repo, factory_root=root)
    saved = _saved_files(root)[0]

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    rc = main(["import", "--repo", str(repo), "--proposal", str(saved)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "WO-0" in out  # the summary lists the salvaged task
    assert "nothing written" in out


def test_completed_session_saves_nothing(tmp_path, monkeypatch):
    """A completed session's path is unchanged: no salvage copy, the
    result flows through as before (a post-session refusal keeps its
    output through keep_refused_import_output, not this path)."""
    repo = _repo(tmp_path)
    root = tmp_path / "froot"

    def completes(kwargs):
        _write_scratch_proposal(kwargs["settings_path"], _proposal(_wo()))
        return SessionRecord(seat="reviewer", status="completed",
                             result=_proposal(_wo()))

    _patch_session(monkeypatch, completes)
    result = run_import(repo, factory_root=root)
    assert result.plan.project == "proj"
    assert _saved_files(root) == []
