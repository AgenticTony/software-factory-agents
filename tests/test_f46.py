"""F-46 acceptance tests — gate evasion hardening (L0/L1).

Every AC is one evasion of the WO's goal — an implementer session must not
get past L0/L1 by shaping its own commits or test configuration:

- AC-1 (M-2): a commit body containing ``\\x1e`` before an AI trailer must
  still be flagged — the scan reads each commit's message by sha, never by
  splitting one combined log on a separator byte the commit itself can
  carry.
- AC-2 (m-17): attribution matching runs on a canonical form (NFKC,
  zero-width/format characters stripped, dashes folded), so Unicode
  spellings of a trailer no longer bypass it; the finding echoes the
  ORIGINAL line.
- AC-3 (m-9): a lint suppression added while renaming a file OUT of a risk
  path still flags — the old path counts too.
- AC-4 (M-4): test-runner configuration is protected by a schema-level
  default a profile can only widen; the pytest table inside pyproject.toml
  (and setup.cfg's / tox.ini's pytest sections) is compared between base
  and head, so a dependency bump still flows but a runner-config change
  never does.
- AC-5 (M-4): L1 counts a red or green run only when the named contract
  test actually ran — "not collected" (exit 5, no-tests-ran, absent from
  the runner's outcome list) is a FAILURE at both ends, never a red proof.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import yaml

from factory.attribution import attribution_match, title_attribution_match
from factory.contract import load_profile, runner_config_policy
from factory.eval import eval_profile
from factory.gates import FileChange, diff_file_changes
from factory.l0 import attribution_findings, check_diff_policy, run_l0
from factory.l1 import run_l1
from factory.sentinels import builtin_sentinel_findings, matched_risk_flags

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
HUMAN_TRAILER = "Co-authored-by: Jane Reviewer <jane@example.com>"

BASE_PROFILE = {
    "name": "fixture",
    "repo": {"path": ".", "default_branch": "main", "remote": "nowhere"},
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
}

TASK = {"id": "T-1", "risk": []}


# ---------------------------------------------------------------- helpers --


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        input=stdin,
    )


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, name: str, files: dict[str, str] | None = None) -> Path:
    """A fixture git repo with one base commit on ``main``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    for rel, text in (files or {"src/a.py": "x = 0\n"}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _branch(repo: Path) -> None:
    _git(repo, "checkout", "-b", "feature")


def _commit(repo: Path, message: str | bytes, files: dict[str, str] | None = None,
            rename: tuple[str, str] | None = None) -> str:
    """One commit on the current branch; the message may be raw bytes
    (non-UTF-8 commit bodies must be committable for AC-1)."""
    if rename:
        (repo / rename[1]).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", rename[0], rename[1])
    for rel, text in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "--allow-empty-message",
         "-F", "-", stdin=(
             message if isinstance(message, bytes) else message.encode("utf-8")
         ))
    return _git_out(repo, "rev-parse", "HEAD")


def _run_l0(repo: Path, profile=None, task=None):
    return run_l0(
        dict(profile or BASE_PROFILE), repo, "main", "feature",
        task=task or TASK,
    )


def _finding_texts(report, finding_id: str) -> list[str]:
    return [f.text for f in report.findings if f.id == finding_id]


def _profile_yaml(tmp_path: Path, name: str, protected: list[str] | None) -> Path:
    """A minimal schema-valid profile with a controllable protected list."""
    profile = {
        "name": name,
        "repo": {"path": ".", "default_branch": "main", "remote": "nowhere"},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": protected or []},
        "dependency_manifests": [],
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }
    path = tmp_path / f"{name}.project.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False))
    return path


# ------------------------------------------ AC-1: per-commit attribution ---


def test_ac_1(tmp_path):
    # The M-2 shape: a \x1e byte in the body before the trailer. The old
    # scan split one combined log on \x1e, so the trailer text landed in a
    # fragment with no \x1f and was never scanned — the branch pushed with
    # machine attribution in its history.
    repo = _repo(tmp_path, "x1e")
    _branch(repo)
    clean_sha = _commit(repo, "feat: clean first\n", files={"src/a.py": "x = 1\n"})
    bad_sha = _commit(
        repo,
        "feat: hide\n\n\x1e\n" + AI_TRAILER + "\n",
        files={"src/a.py": "x = 2\n"},
    )
    findings = attribution_findings(repo, "main", "feature")
    assert len(findings) == 1, [f.text for f in findings]
    assert findings[0].id == "attribution_trailer"
    assert bad_sha[:12] in findings[0].text, findings[0].text
    assert clean_sha[:12] not in findings[0].text
    # through the whole gate, not just the unit
    report = _run_l0(repo)
    assert report.verdict == "fail"
    assert _finding_texts(report, "attribution_trailer")

    # \x1f in the body, and both bytes together — every byte of every
    # commit's full message is scanned, whatever it contains.
    for label, body in (
        ("x1f", "feat: hide\n\n\x1f junk\n" + AI_TRAILER + "\n"),
        ("both", "feat: hide\n\n\x1e\x1f\n" + AI_TRAILER + "\n"),
    ):
        repo = _repo(tmp_path, label)
        _branch(repo)
        sha = _commit(repo, body, files={"src/a.py": "x = 1\n"})
        findings = attribution_findings(repo, "main", "feature")
        assert len(findings) == 1 and sha[:12] in findings[0].text, label

    # a clean multi-commit branch passes, and a human Co-Authored-By does
    # too — the scan reads each message, it does not refuse every body.
    repo = _repo(tmp_path, "clean")
    _branch(repo)
    _commit(repo, "feat: one\n", files={"src/a.py": "x = 1\n"})
    _commit(repo, f"feat: two\n\n{HUMAN_TRAILER}\n", files={"src/a.py": "x = 2\n"})
    assert attribution_findings(repo, "main", "feature") == []
    assert _run_l0(repo).verdict == "pass"

    # a non-UTF-8 commit body (git stores raw bytes): the scan must decode
    # with replacement and still scan — never crash, never silently skip.
    repo = _repo(tmp_path, "latin1")
    _branch(repo)
    sha = _commit(
        repo,
        "r\xe9sum\xe9\n\n".encode("latin-1") + AI_TRAILER.encode("ascii") + b"\n",
        files={"src/a.py": "x = 1\n"},
    )
    findings = attribution_findings(repo, "main", "feature")
    assert len(findings) == 1 and sha[:12] in findings[0].text, findings
    repo = _repo(tmp_path, "latin1-clean")
    _branch(repo)
    _commit(repo, "r\xe9sum\xe9\n\nplain body\n".encode("latin-1"),
            files={"src/a.py": "x = 1\n"})
    assert attribution_findings(repo, "main", "feature") == []
    assert _run_l0(repo).verdict == "pass"


# --------------------------------- AC-2: normalisation before matching -----


def test_ac_2():
    # U+2010 hyphen in the trailer prefix (m-17's exact shape).
    hit = attribution_match("Co‐Authored‐By: Claude <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit
    # the finding echoes the ORIGINAL line, not the canonical form.
    assert "‐" in hit[1], hit[1]

    # a fullwidth colon.
    hit = attribution_match("Co-Authored-By： Claude <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit

    # a zero-width space inside "claude", and a soft hyphen in the prefix.
    hit = attribution_match("Co-Authored-By: cl​aude <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit
    hit = attribution_match("Co\xad-Authored-By: Claude <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit
    hit = attribution_match("Co-Authored-By: Anthro­pic <noreply@x>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit

    # Unicode spaces between tokens (NBSP, ideographic space).
    hit = attribution_match("Co-Authored-By: Claude　<noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit

    # a homoglyph author (Cyrillic с U+0441 inside "claude") and the
    # mathematical-bold spelling (NFKC folds it) — decided in this WO: a
    # small confusables fold covers the homoglyph class.
    hit = attribution_match("Co-Authored-By: сlaude <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit
    hit = attribution_match("Co-Authored-By: 𝐂𝐥𝐚𝐮𝐝𝐞 <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit

    # several tricks in one line still match.
    hit = attribution_match("Co‐Authored‐By： cl​aude <noreply@anthropic.com>\n")
    assert hit is not None, hit

    # the generated-with footer reaches an AI tool past any number of
    # connecting tokens, and the PR-title form normalises too.
    hit = attribution_match("Fix billing\n\nGenerated with my favourite shiny tool Claude\n")
    assert hit is not None and hit[0] == "generated_with_footer", hit
    hit = attribution_match(
        "Generated with our good old friend 𝐂𝐨𝐩𝐢𝐥𝐨𝐭 and friends\n"
    )
    assert hit is not None and hit[0] == "generated_with_footer", hit
    hit = title_attribution_match("T-1: add Co‐Authored‐By: Claude support")
    assert hit is not None, hit

    # the false-positive guards stay green: ordinary prose and human credits.
    assert attribution_match("generated with openapi-generator/faker/ruff\n") is None
    assert attribution_match("generated with faker\n") is None
    assert attribution_match(f"feat: x\n\n{HUMAN_TRAILER}\n") is None
    assert attribution_match(
        "Co-authored-by: Kimi Larsson <kimi@acme.se>\n"
    ) is None

    # a trailer buried mid-body is still line-anchored: prose discussing a
    # trailer does not trip, the canonical trailer on its own line does.
    body = "We discussed whether Co-Authored-By: Claude trailers are okay.\n"
    assert attribution_match(body) is None
    assert attribution_match(body + AI_TRAILER + "\n") is not None


# ---------------------------- AC-3: lint suppression across a rename ------


def test_ac_3(tmp_path):
    risk = {"payments": ["payments/**"]}

    # The m-9 shape: rename payments/x.py -> utils/x.py and add a noqa
    # suppression comment.
    repo = _repo(tmp_path, "out", {"payments/x.py": "def charge():\n    return 1\n"})
    _branch(repo)
    _commit(repo, "refactor: move\n",
            files={"utils/x.py": "def charge():\n    return 1\n\n\ny = 2  # noqa\n"},
            rename=("payments/x.py", "utils/x.py"))
    changes = diff_file_changes(repo, "main", "feature")
    assert [(c.status, c.old_path, c.path) for c in changes] == [
        ("R", "payments/x.py", "utils/x.py")
    ], changes
    findings = builtin_sentinel_findings(changes, risk)
    assert [f.id for f in findings if f.id == "lint_suppression_added"], findings
    # the risk flag was counted via the old path all along; the sentinel
    # now agrees with it.
    assert matched_risk_flags(risk, changes) == {"payments"}

    # a rename INTO a risk path with an added suppression (new path side).
    repo = _repo(tmp_path, "in", {"utils/y.py": "def f():\n    return 1\n"})
    _branch(repo)
    _commit(repo, "refactor: move in\n",
            files={"payments/y.py": "def f():\n    return 1\n\n\ny = 2  # type: ignore\n"},
            rename=("utils/y.py", "payments/y.py"))
    changes = diff_file_changes(repo, "main", "feature")
    assert [f.id for f in builtin_sentinel_findings(changes, risk)
            if f.id == "lint_suppression_added"], changes

    # a rename out of a risk path with NO suppression stays clean.
    repo = _repo(tmp_path, "clean", {"payments/x.py": "def charge():\n    return 1\n"})
    _branch(repo)
    _commit(repo, "refactor: clean move\n",
            files={"utils/x.py": "def charge():\n    return 1\n\n\ny = 2\n"},
            rename=("payments/x.py", "utils/x.py"))
    changes = diff_file_changes(repo, "main", "feature")
    assert builtin_sentinel_findings(changes, risk) == []

    # a copy (C status) is judged by its NEW path only — the copy-source
    # exclusion is matched_risk_flags' reviewed semantics (F-25 round-2
    # finding 3), and the sentinel must not disagree with it.
    copy = [FileChange("utils/x.py", "C", old_path="payments/x.py",
                       added_lines=[(1, "y = 2  # noqa")])]
    assert builtin_sentinel_findings(copy, risk) == []
    assert matched_risk_flags(risk, copy) == set()

    # a rename out of a risk path into a format where '#' is not a comment
    # (JSON) does not flag: the suppression is inert where it landed. The
    # content type is read from the file's NEW path — that is where the
    # added lines live and where a linter would (not) read them.
    repo = _repo(tmp_path, "json", {"payments/x.py": "x = 1\n"})
    _branch(repo)
    _commit(repo, "refactor: to json\n",
            files={"assets/x.json": '{\n  "a": 1  # noqa\n}\n'},
            rename=("payments/x.py", "assets/x.json"))
    changes = diff_file_changes(repo, "main", "feature")
    assert builtin_sentinel_findings(changes, risk) == []

    # and a .py renamed to .txt (no comment map entry — every syntax
    # allowed) still flags through the old path. The content stays mostly
    # identical so git detects the rename (a tiny rewrite can fall below
    # -M's similarity threshold and split into add+delete, which no
    # rename rule ever sees — recorded as a residual in the F-46 ledger).
    lines = [f"value_{n} = {n}\n" for n in range(1, 21)]
    repo = _repo(tmp_path, "txt", {"payments/x.py": "".join(lines)})
    _branch(repo)
    _commit(repo, "refactor: to txt\n",
            files={"docs/x.txt": "".join(
                lines[:2] + ["value_3 = 3  # noqa\n"] + lines[3:]
            )},
            rename=("payments/x.py", "docs/x.txt"))
    changes = diff_file_changes(repo, "main", "feature")
    assert [f.id for f in builtin_sentinel_findings(changes, risk)
            if f.id == "lint_suppression_added"], changes


# --------------------- AC-4: runner config protected by default -----------


def test_ac_4(tmp_path):
    # Structural, whatever the profile says: a conftest edit with a
    # profile that does not list it.
    findings, escalations, _added, _matched = check_diff_policy(
        {"paths": {"protected": []}},
        [FileChange("tests/conftest.py", "M")],
        TASK,
    )
    assert any(
        f.id == "protected_path_touched" and f.file == "tests/conftest.py"
        for f in findings
    ), findings
    assert "protected_path_touched" in escalations

    # load_profile unions the schema-level default in — a profile can only
    # widen the set, never narrow it.
    loaded = load_profile(_profile_yaml(tmp_path, "narrow", ["src/**"]))
    assert "**/conftest.py" in loaded["paths"]["protected"]
    assert "**/pytest.ini" in loaded["paths"]["protected"]
    assert "src/**" in loaded["paths"]["protected"]

    # real-repo table checks. A dependency-only pyproject edit is NOT this
    # rule's business (it flows to the new-dependency rule).
    base_pyproject = '[project]\nname = "x"\ndependencies = ["a"]\n'
    repo = _repo(tmp_path, "dep-only", {"pyproject.toml": base_pyproject})
    _branch(repo)
    _commit(repo, "chore: bump\n", files={
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["a", "b"]\n',
    })
    changes = diff_file_changes(repo, "main", "feature")
    findings, _esc, _added, _matched = check_diff_policy(
        BASE_PROFILE, changes, TASK,
        repo=repo, base_ref="main", head_ref="feature",
    )
    assert not [f for f in findings if f.id == "protected_path_touched"], findings

    # the pytest table changed -> protected, including being added.
    repo = _repo(tmp_path, "table", {"pyproject.toml": base_pyproject})
    _branch(repo)
    _commit(repo, "chore: configure pytest\n", files={
        "pyproject.toml": base_pyproject
        + '\n[tool.pytest.ini_options]\naddopts = "-x"\n',
    })
    changes = diff_file_changes(repo, "main", "feature")
    findings, escalations, _a, _m = check_diff_policy(
        BASE_PROFILE, changes, TASK,
        repo=repo, base_ref="main", head_ref="feature",
    )
    hits = [f for f in findings if f.id == "protected_path_touched"]
    assert hits and hits[0].file == "pyproject.toml", findings
    assert "protected_path_touched" in escalations

    # an unchanged pytest table beside a changed dependency surface is not
    # this rule's finding (both sides parse and compare equal).
    repo = _repo(tmp_path, "table-same", {"pyproject.toml": base_pyproject
                                          + '\n[tool.pytest.ini_options]\naddopts = "-x"\n'})
    _branch(repo)
    _commit(repo, "chore: bump only\n", files={
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["a", "b"]\n'
                          '\n[tool.pytest.ini_options]\naddopts = "-x"\n',
    })
    changes = diff_file_changes(repo, "main", "feature")
    findings, _esc, _a, _m = check_diff_policy(
        BASE_PROFILE, changes, TASK,
        repo=repo, base_ref="main", head_ref="feature",
    )
    assert not [f for f in findings if f.id == "protected_path_touched"], findings

    # setup.cfg's [tool:pytest] and tox.ini's [pytest] (the amended AC).
    for label, base_file, head_file in (
        ("setupcfg", "[metadata]\nname = x\n",
         "[metadata]\nname = x\n\n[tool:pytest]\naddopts = -x\n"),
        ("toxini", "[tox]\nenv_list = py\n",
         "[tox]\nenv_list = py\n\n[pytest]\naddopts = -x\n"),
    ):
        repo = _repo(tmp_path, label, {"setup.cfg" if label == "setupcfg" else "tox.ini":
                                       base_file})
        name = "setup.cfg" if label == "setupcfg" else "tox.ini"
        _branch(repo)
        _commit(repo, "chore: runner config\n", files={name: head_file})
        changes = diff_file_changes(repo, "main", "feature")
        findings, _esc, _a, _m = check_diff_policy(
            BASE_PROFILE, changes, TASK,
            repo=repo, base_ref="main", head_ref="feature",
        )
        hits = [f for f in findings if f.id == "protected_path_touched"]
        assert hits and hits[0].file == name, (label, findings)

    # an unparseable pyproject at head fails closed as touched.
    repo = _repo(tmp_path, "broken", {"pyproject.toml": base_pyproject})
    _branch(repo)
    _commit(repo, "chore: break toml\n", files={"pyproject.toml": "not [ toml\n"})
    changes = diff_file_changes(repo, "main", "feature")
    findings, _esc, _a, _m = check_diff_policy(
        BASE_PROFILE, changes, TASK,
        repo=repo, base_ref="main", head_ref="feature",
    )
    assert [f for f in findings if f.id == "protected_path_touched"], findings

    # every profile in the repo validates, loads, and carries the default
    # (the fourth — eval's synthetic fixture profile — included).
    for rel in (
        ".factory/project.yaml",
        "projects/jobfinderos.project.yaml",
        "projects/soloadventurer-web.project.yaml",
    ):
        profile = load_profile(REPO_ROOT / rel)
        assert "**/conftest.py" in profile["paths"]["protected"], rel
        assert "**/pytest.ini" in profile["paths"]["protected"], rel
    assert "**/conftest.py" in eval_profile(tmp_path / "evalrepo")["paths"]["protected"]

    # through the whole gate: a conftest edit fails L0 on a profile that
    # never named it.
    repo = _repo(tmp_path, "gate", {"tests/conftest.py": "x = 1\n"})
    _branch(repo)
    _commit(repo, "chore: touch conftest\n", files={"tests/conftest.py": "x = 2\n"})
    report = _run_l0(repo)
    assert report.verdict == "fail"
    assert [f for f in report.findings if f.id == "protected_path_touched"]


# ------------------- AC-5: "not collected" fails at both ends -------------


def test_ac_5(tmp_path):
    task = {"id": "T-1", "risk": [], "acceptance": [
        {"id": "AC-1", "text": "it works",
         "check": {"type": "test", "ref": "tests/test_contract.py::test_ac_1"}},
    ]}

    def _run(repo, profile=None):
        return run_l1(profile or {"name": "fixture", "commands": {}},
                      repo, "main", "feature", task=task)

    def _result(report, name):
        return next(r for r in report.command_results if r.name == name)

    # The M-4 conftest, executable on pytest 9 (collect_ignore alone no
    # longer hides an explicitly-passed node id, so the hiding is done by
    # dropping the collected items — same finding, same observable): the
    # test module imports cleanly at red (impl exists on base, wrong
    # value), the conftest keys its hiding on a module only the diff
    # adds — at red the contract test is hidden, the run reports
    # "no tests ran"/exit 5, and that must FAIL L1, not count as the red
    # proof. At green the module exists, the test runs and passes; the
    # fake red is still a FAIL.
    repo = _repo(tmp_path, "m4", {"impl.py": "def answer():\n    return 0\n"})
    _branch(repo)
    _commit(repo, "feat: hide at red\n", files={
        "impl.py": "def answer():\n    return 42\n",
        "impl_v2.py": "SENTINEL = 1\n",
        "conftest.py": (
            "try:\n"
            "    import impl_v2\n"
            "    _hidden = False\n"
            "except ImportError:\n"
            "    _hidden = True\n"
            "\n"
            "\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    if _hidden:\n"
            "        items[:] = []\n"
        ),
        "tests/test_contract.py": (
            "from impl import answer\n"
            "\n"
            "\n"
            "def test_ac_1():\n"
            "    assert answer() == 42\n"
        ),
    })
    report = _run(repo)
    assert report.verdict == "fail", report.findings
    hits = [f for f in report.findings if f.id == "contract_not_collected"]
    assert hits and "red" in hits[0].text, report.findings
    assert _result(report, "red:tests/test_contract.py::test_ac_1").exit_code == 5

    # runner config injected by the diff: pytest.ini addopts deselects the
    # ref at green (AC-4 also catches the file; L1 must fail on its own —
    # at the green end here, since the red phase applies only test files).
    repo = _repo(tmp_path, "deselect", {"impl.py": "def answer():\n    return 0\n"})
    _branch(repo)
    ref = "tests/test_contract.py::test_ac_1"
    _commit(repo, "feat: work\n", files={
        "impl.py": "def answer():\n    return 42\n",
        "tests/test_contract.py": (
            "from impl import answer\n"
            "\n"
            "\n"
            "def test_ac_1():\n"
            "    assert answer() == 42\n"
        ),
        "pytest.ini": f"[pytest]\naddopts = --deselect {ref}\n",
    })
    report = _run(repo)
    assert report.verdict == "fail", report.findings
    hits = [f for f in report.findings if f.id == "contract_not_collected"]
    assert hits and "green" in hits[0].text, report.findings

    # the LEGITIMATE red-first shape must not regress: the contract test's
    # module imports a name that only exists at head — collected, errored
    # at collection, exit != 5, red_reason "collection", L1 passes.
    repo = _repo(tmp_path, "legit")
    _branch(repo)
    _commit(repo, "feat: new module\n", files={
        "impl.py": "def answer():\n    return 42\n",
        "tests/test_contract.py": (
            "from impl import answer\n"
            "\n"
            "\n"
            "def test_ac_1():\n"
            "    assert answer() == 42\n"
        ),
    })
    report = _run(repo)
    assert report.verdict == "pass", report.findings
    assert report.red_reasons[ref] == "collection", report.red_reasons
    red = _result(report, f"red:{ref}")
    assert red.exit_code not in (0, 5) and "found no collectors" in red.output

    # a non-pytest runner template: jest's "No tests found" and an outcome
    # list that never names the ref are both "not collected" — L1 fails
    # closed for jest too, never treating the absence of a pytest
    # signature as proof.
    runner = tmp_path / "fake-jest.sh"
    runner.write_text(
        "#!/bin/sh\n"
        "echo 'No tests found, exiting with code 1'\n"
        "exit 1\n"
    )
    runner.chmod(0o755)
    repo = _repo(tmp_path, "jest")
    _branch(repo)
    _commit(repo, "feat: work\n", files={
        "impl.py": "def answer():\n    return 42\n",
        "tests/test_contract.py": (
            "from impl import answer\n"
            "\n"
            "\n"
            "def test_ac_1():\n"
            "    assert answer() == 42\n"
        ),
    })
    profile = {"name": "fixture", "commands": {
        "test_ref": f"sh {shlex.quote(str(runner))} {{ref}}",
    }}
    report = _run(repo, profile)
    assert report.verdict == "fail", report.findings
    assert [f for f in report.findings if f.id == "contract_not_collected"]

    runner.write_text(
        "#!/bin/sh\n"
        "echo 'PASS src/unrelated.test.ts'\n"
        "echo 'Tests: 1 failed, 1 total'\n"
        "exit 1\n"
    )
    report = _run(repo, profile)
    assert report.verdict == "fail", report.findings
    assert [f for f in report.findings if f.id == "contract_not_collected"]


# ------------------------- hostile pass pins (real tools) ------------------


def test_review1_merge_commit_in_range_carries_the_trailer(tmp_path):
    """A merge commit between the merge-base and head is scanned like any
    other — its message is attacker bytes too."""
    repo = _repo(tmp_path, "merge", {"src/a.py": "x = 0\n"})
    _branch(repo)
    _commit(repo, "feat: side work\n", files={"src/a.py": "x = 1\n"})
    _git(repo, "checkout", "main")
    _commit(repo, "main: other\n", files={"src/b.py": "y = 1\n"})
    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-ff", "-m",
         f"merge: bring main in\n\n{AI_TRAILER}\n", "main")
    findings = attribution_findings(repo, "main", "feature")
    assert len(findings) == 1, [f.text for f in findings]
    merge_sha = _git_out(repo, "rev-list", "--merges", "main..feature")
    assert merge_sha[:12] in findings[0].text, findings[0].text


def test_review2_crlf_and_empty_commit_messages(tmp_path):
    """CRLF bodies still match (the trailer's line is found regardless of
    the preceding line ending); an empty message is clean, never a crash."""
    repo = _repo(tmp_path, "crlf")
    _branch(repo)
    _commit(repo, "feat: crlf\r\n\r\n" + AI_TRAILER + "\r\n",
            files={"src/a.py": "x = 1\n"})
    findings = attribution_findings(repo, "main", "feature")
    assert len(findings) == 1, findings

    repo = _repo(tmp_path, "empty-msg")
    _branch(repo)
    _commit(repo, "", files={"src/a.py": "x = 1\n"})
    assert attribution_findings(repo, "main", "feature") == []
    assert _run_l0(repo).verdict == "pass"


def test_review3_control_byte_inside_the_trailer_keyword(tmp_path):
    """A \x1e byte inside the trailer keyword itself (not just before the
    trailer) splits the pattern's target word; the canonical form strips
    control characters (tab and newline excepted), so the trailer is the
    trailer whatever invisible bytes it carries."""
    hit = attribution_match("Co-Authored\x1e-By: Claude <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit
    hit = attribution_match("Co-Authored-By: cl\x00aude <noreply@anthropic.com>\n")
    assert hit is not None, hit
    # ... and the whole-phrase fullwidth spelling folds through NFKC.
    hit = attribution_match("Ｇｅｎｅｒａｔｅｄ ｗｉｔｈ Ｃｌａｕｄｅ\n")
    assert hit is not None and hit[0] == "generated_with_footer", hit


def test_review4_not_collected_evidence_classification():
    """The one helper both L1 ends use, pinned per class: real verdicts
    (assertion red, collection error, timeout) return None; every
    collection-hiding shape returns its class and a legible reason."""
    from factory.gates import CommandResult
    from factory.l1 import not_collected_evidence

    ref = "tests/test_contract.py::test_ac_1"

    def result(exit_code, output="", timed_out=False):
        return CommandResult("run", "pytest", exit_code, output, timed_out)

    # real verdicts — never "not collected"
    assert not_collected_evidence(
        result(1, "FAILED tests/test_contract.py::test_ac_1 - assert"), ref) is None
    assert not_collected_evidence(
        result(4, "ERROR collecting tests/test_contract.py\n"
                  "ImportError while importing test module\n"), ref) is None
    assert not_collected_evidence(result(None, "", timed_out=True), ref) is None
    # exit 5 / no-tests-ran / jest shapes
    kind = not_collected_evidence(result(5, "no tests ran in 0.01s"), ref)
    assert kind and kind[0] == "not_collected"
    kind = not_collected_evidence(result(1, "no tests ran in 0.01s"), ref)
    assert kind and kind[0] == "not_collected"
    kind = not_collected_evidence(result(1, "No tests found, exiting with code 1"), ref)
    assert kind and kind[0] == "not_collected"
    # the ref absent from a jest-style outcome list
    evidence = not_collected_evidence(
        result(1, "PASS src/unrelated.test.ts\nFAIL src/other.test.ts\n"), ref)
    assert evidence and evidence[0] == "not_collected", evidence
    # the ref PRESENT in the outcome list is a real verdict
    assert not_collected_evidence(
        result(1, "FAIL tests/test_contract.py - bad"), ref) is None
    # the runner's own not-found line is the not_found class (a contract
    # problem, judged contract_ambiguous at the green end)
    assert not_collected_evidence(
        result(4, "ERROR: not found: tests/test_contract.py::test_ac_1"), ref)[0] == "not_found"


def test_review5_undetected_rename_still_raises_the_risk_flag(tmp_path):
    """A rename rewritten past -M's similarity threshold presents as
    add+delete. The recorded residual: the ADDed copy outside the risk
    paths carries no lint-suppression finding, but the DELETION of the
    risk-path file still raises the flag (matched_risk_flags judges
    every change's own path) — the human-merge gate holds."""
    risk = {"payments": ["payments/**"]}
    lines = [f"value_{n} = {n}\n" for n in range(1, 21)]
    repo = _repo(tmp_path, "split", {"payments/x.py": "".join(lines)})
    _branch(repo)
    # a completely rewritten "rename": git reports add + delete
    _commit(repo, "refactor: rewrite elsewhere\n", files={
        "utils/x.py": "def fresh():\n    return 'new implementation'  # noqa\n",
    }, rename=("payments/x.py", "utils/x.py"))
    changes = diff_file_changes(repo, "main", "feature")
    assert {(c.status, c.path) for c in changes} == {
        ("A", "utils/x.py"), ("D", "payments/x.py"),
    }, changes
    assert matched_risk_flags(risk, changes) == {"payments"}
    assert builtin_sentinel_findings(changes, risk) == []


def test_review6_runner_config_table_edges(tmp_path):
    """Nested pyprojects, added-with-table, added-without-table, and
    non-pytest setup.cfg sections: the structural check follows the
    basename, flags only the pytest surface, and leaves dependency-only
    additions to the dependency rule."""
    base = '[project]\nname = "x"\ndependencies = ["a"]\n'

    # a NESTED pyproject's pytest table is runner config too.
    repo = _repo(tmp_path, "nested", {"services/api/pyproject.toml": base})
    _branch(repo)
    _commit(repo, "chore: nested config\n", files={
        "services/api/pyproject.toml": base
        + '\n[tool.pytest.ini_options]\naddopts = "-x"\n',
    })
    changes = diff_file_changes(repo, "main", "feature")
    findings, _esc, _a, _m = check_diff_policy(
        BASE_PROFILE, changes, TASK, repo=repo, base_ref="main", head_ref="feature")
    hits = [f for f in findings if f.id == "protected_path_touched"]
    assert hits and hits[0].file == "services/api/pyproject.toml", findings

    # a pyproject ADDED that carries a pytest table flags; one without
    # does not (it is a dependency question, not a runner-config one).
    repo = _repo(tmp_path, "added")
    _branch(repo)
    _commit(repo, "feat: add pkg\n", files={
        "pyproject.toml": base + '\n[tool.pytest.ini_options]\naddopts = "-x"\n',
    })
    changes = diff_file_changes(repo, "main", "feature")
    findings, _esc, _a, _m = check_diff_policy(
        BASE_PROFILE, changes, TASK, repo=repo, base_ref="main", head_ref="feature")
    assert [f for f in findings if f.id == "protected_path_touched"], findings

    repo = _repo(tmp_path, "added-plain")
    _branch(repo)
    _commit(repo, "feat: add pkg\n", files={"pyproject.toml": base})
    changes = diff_file_changes(repo, "main", "feature")
    findings, _esc, _a, _m = check_diff_policy(
        BASE_PROFILE, changes, TASK, repo=repo, base_ref="main", head_ref="feature")
    assert not [f for f in findings if f.id == "protected_path_touched"], findings

    # setup.cfg sections that are not pytest's stay free to change.
    repo = _repo(tmp_path, "cfg-other", {"setup.cfg": "[metadata]\nname = x\n"})
    _branch(repo)
    _commit(repo, "chore: metadata\n", files={
        "setup.cfg": "[metadata]\nname = y\nversion = 2\n",
    })
    changes = diff_file_changes(repo, "main", "feature")
    findings, _esc, _a, _m = check_diff_policy(
        BASE_PROFILE, changes, TASK, repo=repo, base_ref="main", head_ref="feature")
    assert not [f for f in findings if f.id == "protected_path_touched"], findings


def test_review7_rehearsal_profile_carries_the_defaults(tmp_path):
    """The fifth profile-shaped mapping (the rehearsal fixture) routes
    through the same defaults, so no synthetic profile can narrow the
    runner-config protection either."""
    from factory.rehearsal import _profile

    profile = _profile(tmp_path / "repo", tmp_path / "remote")
    assert "**/conftest.py" in profile["paths"]["protected"]
    assert "**/pytest.ini" in profile["paths"]["protected"]


# ------------------- owner review round 1 (real probes, red-first) ---------

_CONDEF_TEST = (
    "import importlib.util\n"
    "\n"
    "\n"
    "if importlib.util.find_spec('impl_v2'):\n"
    "    def test_ac_1():\n"
    "        from impl import answer\n"
    "\n"
    "        assert answer() == 42\n"
)
_CONDEF_TRY = (
    "try:\n"
    "    import impl_v2\n"
    "    _present = True\n"
    "except ImportError:\n"
    "    _present = False\n"
    "\n"
    "\n"
    "if _present:\n"
    "    def test_ac_1():\n"
    "        from impl import answer\n"
    "\n"
    "        assert answer() == 42\n"
)


def _l1_task():
    return {"id": "T-1", "risk": [], "acceptance": [
        {"id": "AC-1", "text": "it works",
         "check": {"type": "test", "ref": "tests/test_contract.py::test_ac_1"}},
    ]}


def test_review8_root_runner_config_is_protected():
    """The ROOT forms: fnmatch's ``**/x`` needs a slash before x, so the
    root — the most common place for both files — needs its own entry
    (review finding 1; probed: root conftest.py and pytest.ini additions
    passed with an empty protected list)."""
    for status, path in (("A", "conftest.py"), ("M", "conftest.py"),
                         ("A", "pytest.ini"), ("M", "pytest.ini")):
        findings, escalations, _a, _m = check_diff_policy(
            {"paths": {"protected": []}}, [FileChange(path, status)], TASK,
        )
        assert any(
            f.id == "protected_path_touched" and f.file == path
            for f in findings
        ), (status, path, findings)
        assert "protected_path_touched" in escalations, (status, path)
    # and through the union: a loaded profile carries the root forms too.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        loaded = load_profile(_profile_yaml(Path(td), "root", []))
        assert "conftest.py" in loaded["paths"]["protected"]
        assert "pytest.ini" in loaded["paths"]["protected"]


def test_review9_every_pytest_config_name_is_protected():
    """pytest 9 reads four dedicated config files — pytest.ini,
    .pytest.ini, pytest.toml, .pytest.toml (review finding 2; probed:
    each hides a named test with addopts=-k). All four are protected at
    the root and at any depth, whatever the profile says."""
    for path in ("pytest.ini", "tests/pytest.ini", ".pytest.ini",
                 "pkg/.pytest.ini", "pytest.toml", "deep/pkg/pytest.toml",
                 ".pytest.toml", "a/.pytest.toml"):
        findings, _esc, _a, _m = check_diff_policy(
            {"paths": {"protected": []}}, [FileChange(path, "A")], TASK,
        )
        assert any(
            f.id == "protected_path_touched" and f.file == path
            for f in findings
        ), (path, findings)


def test_review10_pytest_config_names_covered_by_policy():
    """The structural guard: the file names pytest itself discovers are
    read from the INSTALLED pytest's own loader table — a future pytest
    that adds a name fails this suite instead of opening a hole. Names
    with a dedicated pytest meaning are whole-file protected; the three
    shared files are covered by the pytest-table comparison."""
    from _pytest.config.findpaths import CONFIG_LOADERS_BY_NAME

    policy = runner_config_policy()
    globs = set(policy["protected_globs"])
    tables = set(policy["pytest_tables"])
    for name in CONFIG_LOADERS_BY_NAME:
        assert (name in globs and f"**/{name}" in globs) or name in tables, (
            f"pytest discovers {name!r} as a config file but the F-46 "
            "default protects neither the whole file nor its pytest table"
        )


def test_review11_self_hiding_contract_test_fails_l1(tmp_path):
    """The contract test hiding ITSELF at red (review finding 3): the
    definition is conditional on a module only the diff adds, so red
    reports the runner's not-found line and pytest 9 exits 4 — previously
    accepted as a red proof. At the red end ANY not-collected evidence is
    a FAIL: the test exists at head (the diff applied its own file there),
    so not-found at red means it was hidden."""
    for label, source in (("find_spec", _CONDEF_TEST), ("try", _CONDEF_TRY)):
        repo = _repo(tmp_path, f"selfhide-{label}",
                     {"impl.py": "def answer():\n    return 0\n"})
        _branch(repo)
        _commit(repo, "feat: conditional test\n", files={
            "impl.py": "def answer():\n    return 42\n",
            "impl_v2.py": "SENTINEL = 1\n",
            "tests/test_contract.py": source,
        })
        report = run_l1({"name": "fixture", "commands": {}},
                        repo, "main", "feature", task=_l1_task())
        assert report.verdict == "fail", (label, report.findings)
        hits = [f for f in report.findings if f.id == "contract_not_collected"]
        assert hits and "red" in hits[0].text, (label, report.findings)
        assert report.red_reasons["tests/test_contract.py::test_ac_1"] == (
            "not_collected"), (label, report.red_reasons)

    # the legitimate import-error red is untouched: the module is FOUND,
    # errors at collection, and the runner says "found no collectors" —
    # no not-found line, no not-collected failure.
    repo = _repo(tmp_path, "legit-r1")
    _branch(repo)
    _commit(repo, "feat: new module\n", files={
        "impl.py": "def answer():\n    return 42\n",
        "tests/test_contract.py": (
            "from impl import answer\n"
            "\n"
            "\n"
            "def test_ac_1():\n"
            "    assert answer() == 42\n"
        ),
    })
    report = run_l1({"name": "fixture", "commands": {}},
                    repo, "main", "feature", task=_l1_task())
    assert report.verdict == "pass", report.findings
    assert report.red_reasons["tests/test_contract.py::test_ac_1"] == "collection"


def test_review12_unicode_line_separators_before_a_trailer():
    """Every str.splitlines() boundary renders as a line break, so a
    trailer after one is line-initial to a human terminal (review
    finding 4). The canonical match runs on BOTH forms: the line-split
    form (boundaries become newline) and the control-stripped form (which
    still joins a keyword split by a record-separator byte)."""
    seps = ("\r", "\r\n", "\u2028", "\u2029", "\u0085", "\x0b", "\x0c")
    for sep in seps:
        hit = attribution_match(f"fix something{sep}{AI_TRAILER}\n")
        assert hit is not None and hit[0] == "co_author_ai_author", (sep, hit)
        assert "Co-Authored-By" in hit[1], (sep, hit[1])
    # and the keyword-split control byte still matches via the joined form
    hit = attribution_match("Co-Authored\x1e-By: Claude <noreply@anthropic.com>\n")
    assert hit is not None and hit[0] == "co_author_ai_author", hit


def test_review13_invisible_non_cf_splitters():
    """Default-ignorable characters (variation selectors, Hangul
    fillers) and combining marks (Mn/Me) split "claude" without being
    category Cf (review finding 5): the fold strips the
    Default_Ignorable set and nonspacing/enclosing marks after NFKD."""
    words = (
        "Cl\ufe0faude",       # variation selector-16 (Mn, DI)
        "Cl\u3164aude",       # HANGUL FILLER (Lo, DI)
        "Cla\u0301ude",       # combining acute accent (Mn)
        "Cl\uffa0aude",       # HALFWIDTH HANGUL FILLER (Lo, DI)
        "Cl\u034faude",       # combining grapheme joiner (Mn, DI)
    )
    # a neutral address, so only the AI-AUTHOR-NAME class can match —
    # the vendor-address class would fire whatever the name spells.
    for word in words:
        hit = attribution_match(f"Co-Authored-By: {word} <dev@example.org>\n")
        assert hit is not None and hit[0] == "co_author_ai_author", (word, hit)


def test_review14_pytest_config_at_distinguishes_absent_from_unreadable(monkeypatch, tmp_path):
    """An unreadable-but-present config fails the comparison closed; only
    a PROVEN absence (cat-file -e) reads as absent (review finding 6)."""
    from factory import l0 as l0_module

    repo = _repo(tmp_path, "cfg")
    _branch(repo)
    _commit(repo, "chore: config\n", files={
        "setup.cfg": "[metadata]\nname = x\n\n[tool:pytest]\naddopts = -x\n",
    })
    # a real absent path: all-None surface, no error.
    surface, error = l0_module._pytest_config_at(
        repo, "feature", "nope/setup.cfg", ["tool:pytest"])
    assert error is None and surface == {"tool:pytest": None}, (surface, error)

    # present but the read fails: an error, never "absent". The stub
    # breaks only `show` — the merge-base call in the same path is real.
    real_git = l0_module._git

    def broken_read(repo, *args):
        if args and args[0] == "show":
            raise l0_module.GateError("git show failed: boom")
        return real_git(repo, *args)

    monkeypatch.setattr(l0_module, "_git", broken_read)
    surface, error = l0_module._pytest_config_at(
        repo, "feature", "setup.cfg", ["tool:pytest"])
    assert error is not None and "boom" in error, (surface, error)
    findings = [l0_module._runner_config_change(
        "setup.cfg", "modified", repo, "main", "feature")]
    assert findings and "unparseable" in findings[0], findings
