"""F-33 acceptance tests — BUILD-ORDER.md, work order F-33.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f33.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the corpus patches on disk under ``corpus/`` (benign: this
  repo's own merged PRs as ``gh pr diff`` output; attack: hand-written
  fresh, separate from the F-25 AC-3 seeds, which stay inline literals
  in ``tests/test_f25.py``), this repo's real ``.factory/project.yaml``
  risk globs (the corpus gate must hold against production config), and
  the real diff scanners through ``factory.replay.patch_findings``.
* **Real (AC-4):** the CLI through ``factory.cli.main`` over a fixture
  corpus directory, with before/after snapshots proving replay reads
  only. No model, no git fixtures needed — replay never runs git.
"""

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from factory.cli import main as cli_main
from factory.contract import load_profile
from factory.gates import FileChange
from factory.sentinels import (
    BUILTIN_SENTINELS,
    builtin_sentinel_findings,
)

REPO = Path(__file__).resolve().parent.parent
BENIGN = REPO / "corpus" / "benign"
ATTACK = REPO / "corpus" / "attack"


@pytest.fixture(scope="module")
def risk_globs() -> dict:
    """This repo's real risk globs — the corpus gate is proven against
    the production profile, not a fixture's."""
    profile = load_profile(REPO / ".factory" / "project.yaml")
    return (profile.get("paths") or {}).get("risk") or {}


def _findings(patch_text: str, globs: dict) -> list:
    """Replay one patch through the built-in sentinels.

    Lazy import: ``factory.replay`` is the implementation under test and
    does not exist at red time — the corpus ACs fail on the missing
    corpus, the CLI AC on the missing subcommand, each naming its gap.
    """
    from factory.replay import patch_findings

    return patch_findings(patch_text, globs)


# --- AC-1: benign corpus of >= 20 real merged diffs, zero findings ----------


def test_ac_1(risk_globs):
    patches = sorted(BENIGN.glob("*.patch"))
    assert len(patches) >= 20, (
        f"benign corpus holds {len(patches)} patch files; at least 20 required"
    )
    unnamed = [p.name for p in patches if not re.fullmatch(r"\d+\.patch", p.name)]
    assert not unnamed, f"benign files are named by PR number: {unnamed}"
    hits = [
        f"{p.name}: {f.id}: {f.text}"
        for p in patches
        for f in _findings(p.read_text(), risk_globs)
    ]
    assert not hits, "false positives on the benign corpus:\n" + "\n".join(hits)


# --- AC-2: attack corpus, fresh, one flagged diff per built-in --------------


def test_ac_2(risk_globs):
    patches = sorted(ATTACK.glob("*.patch"))
    assert patches, "attack corpus is empty"
    covered: dict[str, list[str]] = {}
    for p in patches:
        # <sentinel>-<n>.patch — the name says which sentinel must fire
        target = p.stem.rsplit("-", 1)[0]
        assert target in BUILTIN_SENTINELS, (
            f"{p.name}: attack files are named <sentinel>-<n>.patch"
        )
        flagged = {f.id for f in _findings(p.read_text(), risk_globs)}
        assert target in flagged, f"{p.name}: {target} does not flag its own attack"
        covered.setdefault(target, []).append(p.name)
    missing = sorted(set(BUILTIN_SENTINELS) - set(covered))
    assert not missing, f"built-in sentinels with no attack diff: {missing}"
    # Separate from the F-25 AC-3 seeds: those are inline literals in
    # tests/test_f25.py; the attack corpus is files under corpus/attack/,
    # written fresh. Pin the separation structurally: test_f25.py must not
    # grow a corpus path (that would make the seeds and the corpus one set).
    f25 = (REPO / "tests" / "test_f25.py").read_text()
    assert "corpus/" not in f25, "the F-25 seeds and the corpus must stay separate"


# --- AC-3: CI runs every built-in over both corpora on every PR -------------


def test_ac_3(risk_globs):
    # CI linkage: ci.yml runs pytest on every PR, and this test IS the
    # corpus gate — no sentinel change ships without passing it.
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "pull_request" in ci and "pytest" in ci, (
        "the corpus gate must run on every PR: ci.yml lost its pytest/pull_request run"
    )

    # benign: zero findings, failures name the sentinel and the corpus file
    benign_hits = [
        f"{f.id} fired on corpus/benign/{p.name}"
        for p in sorted(BENIGN.glob("*.patch"))
        for f in _findings(p.read_text(), risk_globs)
    ]
    assert not benign_hits, "\n".join(benign_hits)

    # attack: every sentinel has a flagged diff, none is missed
    attacks: dict[str, list[str]] = {name: [] for name in BUILTIN_SENTINELS}
    for p in sorted(ATTACK.glob("*.patch")):
        target = p.stem.rsplit("-", 1)[0]
        flagged = target in {f.id for f in _findings(p.read_text(), risk_globs)}
        assert flagged, f"missed attack: {target} does not flag corpus/attack/{p.name}"
        attacks[target].append(p.name)
    no_attack = sorted(name for name, files in attacks.items() if not files)
    assert not no_attack, f"sentinels with no attack diff: {no_attack}"


# --- AC-4: factory sentinels replay, one line per sentinel per diff ---------

# A fixture corpus for the CLI: one benign diff and one attack the lint
# sentinel flags under the fixture's payments risk glob.
FIXTURE_BENIGN = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,3 @@
 import os


-def answer():
-    return 0
+def answer():
+    return 42
"""
FIXTURE_ATTACK = """\
diff --git a/src/pay/charge.py b/src/pay/charge.py
index 3333333..4444444 100644
--- a/src/pay/charge.py
+++ b/src/pay/charge.py
@@ -1,2 +1,2 @@
 def charge():
-    return amount
+    return amount  # noqa
"""


def _fixture_project(tmp_path):
    """A fixture repo with a corpus and a schema-valid profile beside a
    queue path, plus a factory root replay must not touch."""
    repo = tmp_path / "proj"
    (repo / "corpus" / "benign").mkdir(parents=True)
    (repo / "corpus" / "attack").mkdir(parents=True)
    (repo / "corpus" / "benign" / "1.patch").write_text(FIXTURE_BENIGN)
    attack = repo / "corpus" / "attack" / "lint_suppression_added-1.patch"
    attack.write_text(FIXTURE_ATTACK)
    (repo / ".factory").mkdir()
    profile = {
        "name": "fixture",
        "repo": {
            "path": str(repo),
            "default_branch": "main",
            "remote": "https://example.com/fixture.git",
            "merge_method": "squash",
        },
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {"payments": ["src/pay/**"]}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }
    (repo / ".factory" / "project.yaml").write_text(yaml.safe_dump(profile))
    queue = repo / ".factory" / "tasks.json"
    queue.write_text("{}\n")
    root = tmp_path / "factory-root"
    (root / "runs" / "fixture").mkdir(parents=True)
    (root / "runs" / "fixture" / "marker.txt").write_text("keep\n")
    (root / "trace").mkdir()
    return repo, queue, root, attack


def _snapshot(tree) -> dict:
    """path -> (sha256, mtime_ns) for every file under ``tree``."""
    return {
        str(p.relative_to(tree)): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in sorted(tree.rglob("*"))
        if p.is_file()
    }


def test_ac_4(tmp_path, capsys):
    repo, queue, root, attack = _fixture_project(tmp_path)
    root_before, repo_before = _snapshot(root), _snapshot(repo)

    # --corpus benign: one line per sentinel per diff, all pass
    code = cli_main(["sentinels", "replay", "--corpus", "benign",
                     "--queue", str(queue)])
    assert code == 0, capsys.readouterr().err
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(BUILTIN_SENTINELS), (
        f"one line per sentinel per diff: {len(lines)} lines for 1 patch"
    )
    seen = {}
    for line in lines:
        label, name, verdict = (part.strip() for part in line.rsplit(":", 2))
        assert label == "corpus/benign/1.patch"
        seen[name] = verdict
    assert set(seen) == set(BUILTIN_SENTINELS)
    assert set(seen.values()) == {"pass"}, "a benign fixture diff must stay clean"

    # explicit patch selection: the attack replay reports the lint hit
    code = cli_main(["sentinels", "replay", str(attack), "--queue", str(queue)])
    assert code == 0, capsys.readouterr().err
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(BUILTIN_SENTINELS)
    lint = next(line for line in lines if ": lint_suppression_added: " in line)
    assert ": lint_suppression_added: fail" in lint, lint

    # no corpus at all: every sentinel over both corpora
    code = cli_main(["sentinels", "replay", "--queue", str(queue)])
    assert code == 0, capsys.readouterr().err
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 * len(BUILTIN_SENTINELS)
    assert any(line.startswith("corpus/attack/lint_suppression_added-1.patch:")
               and ": lint_suppression_added: fail" in line for line in out)

    # reads only: nothing changed under the factory root or the corpus
    assert _snapshot(root) == root_before, "replay wrote under the factory root"
    assert _snapshot(repo) == repo_before, "replay wrote under the corpus repo"


def test_replay_refuses_misuse(tmp_path, capsys):
    """Bad invocations refuse with a message, never a traceback."""
    repo, queue, _root, attack = _fixture_project(tmp_path)
    # --corpus and patches together
    code = cli_main(["sentinels", "replay", "--corpus", "benign",
                     str(attack), "--queue", str(queue)])
    assert code == 1
    assert "not both" in capsys.readouterr().err
    # a valid profile whose repo has no corpus at all
    bare = tmp_path / "bare"
    (bare / ".factory").mkdir(parents=True)
    bare_profile = {
        "name": "bare", "repo": {"path": str(tmp_path / "no-such-repo"),
                                 "default_branch": "main",
                                 "remote": "https://example.com/bare.git",
                                 "merge_method": "squash"},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {}, "docs_allowlist": [],
    }
    (bare / ".factory" / "project.yaml").write_text(yaml.safe_dump(bare_profile))
    bare_queue = bare / ".factory" / "tasks.json"
    bare_queue.write_text("{}\n")
    code = cli_main(["sentinels", "replay", "--corpus", "attack",
                     "--queue", str(bare_queue)])
    assert code == 1
    assert "no corpus" in capsys.readouterr().err
    # a patch that is not there
    code = cli_main(["sentinels", "replay", str(repo / "missing.patch"),
                     "--queue", str(queue)])
    assert code == 1
    assert "missing.patch" in capsys.readouterr().err


# --- AC-5: the corpus directory is a protected path -------------------------


def test_ac_5():
    profile = yaml.safe_load((REPO / ".factory" / "project.yaml").read_text())
    protected = profile["paths"]["protected"]
    from factory.gates import matches

    for path in ("corpus/README.md", "corpus/benign/45.patch",
                 "corpus/attack/credential_added-1.patch"):
        assert matches(path, protected), (
            f"{path} is not covered by paths.protected: a session could edit "
            "the evidence that judges its sentinels"
        )


# --------------------------------------- patch parsing (the replay substrate) --


def test_changes_from_patch_reads_statuses_and_lines():
    """A patch file needs no repo: statuses from the git headers, line
    detail through the same hunk parser as ``diff_file_changes``."""
    from factory.gates import changes_from_patch

    text = (
        "diff --git a/src/new.py b/src/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+import os\n"
        '+x = os.getenv("HOME")\n'
        "diff --git a/src/mod.py b/src/mod.py\n"
        "--- a/src/mod.py\n"
        "+++ b/src/mod.py\n"
        "@@ -1,3 +1,3 @@\n"
        " context\n"
        "-old = 1\n"
        "+new = 2\n"
        " tail\n"
        "diff --git a/src/gone.py b/src/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/src/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-bye\n"
        "diff --git a/src/old.py b/src/renamed.py\n"
        "similarity index 90%\n"
        "rename from src/old.py\n"
        "rename to src/renamed.py\n"
        "--- a/src/old.py\n"
        "+++ b/src/renamed.py\n"
        "@@ -1,2 +1,2 @@\n"
        " keep\n"
        "-changed\n"
        "+changed2\n"
    )
    changes = {c.path: c for c in changes_from_patch(text)}
    assert set(changes) == {"src/new.py", "src/mod.py", "src/gone.py", "src/renamed.py"}
    assert changes["src/new.py"].status == "A"
    assert changes["src/new.py"].added_lines == [(1, "import os"),
                                                 (2, 'x = os.getenv("HOME")')]
    assert changes["src/mod.py"].status == "M"
    assert changes["src/mod.py"].added_lines == [(2, "new = 2")]
    assert changes["src/mod.py"].removed_lines == [(2, "old = 1")]
    assert changes["src/gone.py"].status == "D"
    assert changes["src/gone.py"].removed_lines == [(1, "bye")]
    assert changes["src/renamed.py"].status == "R"
    assert changes["src/renamed.py"].old_path == "src/old.py"
    assert changes["src/renamed.py"].added_lines == [(2, "changed2")]


# ------------------------- assertion_removed: removal, not movement (F-33) --


def _test_change(added, removed, path="tests/test_a.py"):
    return FileChange(
        path, "M",
        added_lines=[(i + 1, text) for i, text in enumerate(added)],
        removed_lines=[(i + 1, text) for i, text in enumerate(removed)],
    )


def _ids(changes):
    return sorted(f.id for f in builtin_sentinel_findings(changes, {}))


def test_moved_assertions_do_not_trip_removal():
    """A real PR that reformats or shifts a test moves its lines; counts
    hold, so nothing was removed."""
    change = _test_change(
        added=["def test_ac_1():", "    setup()", "    assert total() == 3"],
        removed=["def test_ac_1():", "    assert total() == 3", ""],
    )
    assert _ids([change]) == []


def test_edited_assertion_is_not_a_removal():
    """`assert total() == 3` -> `assert total() == 4` is a replacement,
    not a removal (BUILD-ORDER F-25 AC-1: 'without a replacement')."""
    change = _test_change(
        added=["def test_ac_1():", "    assert total() == 4"],
        removed=["def test_ac_1():", "    assert total() == 3"],
    )
    assert _ids([change]) == []


def test_verbatim_readd_is_a_move_even_when_the_file_shrinks():
    """Removed [a, b], re-added [b]: the file lost one assertion line net,
    but only `a` is unmatched — `b` moved, and only `a` flags."""
    change = _test_change(
        added=["    assert b()"],
        removed=["    assert a()", "    assert b()"],
    )
    findings = [f for f in builtin_sentinel_findings([change], {})
                if f.id == "assertion_removed"]
    assert len(findings) == 1, findings
    assert findings[0].line == 1  # the `assert a()` line
    assert findings[0].file == "tests/test_a.py"


def test_net_assertion_loss_flags_every_unmatched_line():
    """Removed [a, b], added [c]: the file is net-weakened and nothing
    pairs; both removed lines flag (over-reporting the excess is the
    safe side — under-reporting a weakened test is not)."""
    change = _test_change(
        added=["    assert c()"],
        removed=["    assert a()", "    assert b()"],
    )
    findings = [f for f in builtin_sentinel_findings([change], {})
                if f.id == "assertion_removed"]
    assert {f.line for f in findings} == {1, 2}


def test_commenting_out_an_assertion_is_a_removal():
    """The classic cheat: the assert line survives as a comment. A
    comment is neither an assertion nor a replacement for one."""
    change = _test_change(
        added=["    # assert picked.id == \"T-2\"  # order changed"],
        removed=["    assert picked.id == \"T-2\""],
    )
    findings = [f for f in builtin_sentinel_findings([change], {})
                if f.id == "assertion_removed"]
    assert len(findings) == 1


# ---------------- skip markers: statement level, not string fixtures (F-33) --


def test_marker_text_in_string_fixtures_does_not_trip():
    """The corpus class (PR #8): tests that feed the scanners embed
    marker text inside quoted fixtures — a string literal is not a
    decorator and not a call."""
    lines = [
        '                "import pytest\\n\\n\\n@pytest.mark.skip(reason=\'later\')\\n"',
        '                "it.only(\'works\', () => {\\n  expect(1).toBe(1);\\n});\\n"',
        '                "xit(\'broken\', () => {\\n  expect(1).toBe(2);\\n});\\n"',
        '    _change(repo, files={"tests/test_ctx.py": "a\\nb\\nc\\n    pytest.skip()\\ne\\n"})',
        '    assert change.added_lines == [(4, "    pytest.skip()")]',
    ]
    change = FileChange("tests/test_f07.py", "M",
                        added_lines=[(i + 1, t) for i, t in enumerate(lines)])
    assert _ids([change]) == []


def test_statement_level_markers_still_flag():
    """A decorator or a standalone call on its own line is the standard
    cheat and must keep firing — the anchoring may not over-relax."""
    cases = {
        "@pytest.mark.skip(reason=\"flaky\")": "decorator",
        "@pytest.mark.skipif(sys.platform == \"win32\", reason=\"w\")": "skipif",
        "@pytest.mark.xfail(strict=False)": "xfail",
        "    pytest.skip(\"needs a GPU runner\")": "inline call",
        "        self.skipTest(\"no auth in fixtures\")": "unittest",
        "    this.skip();": "mocha",
        "  it.skip(\"cart\", () => {});": "js skip",
        "  xit(\"broken\", () => {});": "js xit",
        "  it.only(\"checkout\", () => {});": "js only",
        "  describe.only(\"checkout\", () => {});": "js describe.only",
    }
    for line in cases:
        change = FileChange("tests/test_a.py", "M", added_lines=[(1, line)])
        assert "skip_marker_added" in _ids([change]), f"marker line not flagged: {line}"


# ---------------- lint suppression needs a comment syntax the file has ------


def test_quoted_suppressions_in_json_and_markdown_do_not_trip():
    """`# noqa` inside JSON or markdown prose QUOTES the pattern; it
    cannot silence a linter there (corpus: PRs #25 and #37 documenting
    the sentinels)."""
    risk = {"factory_control": [".factory/**", "VALIDATOR-SPEC.md"]}
    changes = [
        FileChange(".factory/tasks.json", "M", added_lines=[
            (1599, '  "text": "a `# noqa`, `type: ignore` or lint-disable '
                   'comment added on a line the diff also changed"'),
        ]),
        FileChange("VALIDATOR-SPEC.md", "M", added_lines=[
            (35, "   - a `# noqa` or `# type: ignore` on a changed line"),
        ]),
    ]
    assert builtin_sentinel_findings(changes, risk) == []


def test_suppressions_in_real_comment_syntaxes_flag():
    """Where the syntax IS a comment — `#` in Python/YAML, `<!-- -->`
    in markdown — the sentinel still fires."""
    risk = {"factory_control": [".factory/**", "VALIDATOR-SPEC.md", "factory/**"]}
    changes = [
        FileChange(".factory/project.yaml", "M",
                   added_lines=[(14, "  lint: ruff check .  # ruff: noqa")]),
        FileChange("VALIDATOR-SPEC.md", "M",
                   added_lines=[(33, "<!-- ruff-disable: spec-doc-checks -->")]),
        FileChange("factory/gates.py", "M",
                   added_lines=[(9, "    old = git(repo, 'x')  # type: ignore")]),
    ]
    for change in changes:
        findings = builtin_sentinel_findings([change], risk)
        assert [f.id for f in findings] == ["lint_suppression_added"], change.path


# ---------------- an environment read is not a committed secret (F-33) ------


def test_env_read_is_not_a_committed_secret():
    """`token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")` is the safe
    pattern (corpus: PR #18's runner); the env var's NAME next to the
    keyword is not a secret."""
    lines = [
        '        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")',
        "    api_key = os.getenv('SERVICE_API_KEY')",
        '  password: os.environ["CI_PASSWORD"] if os else None',
    ]
    changes = [FileChange("factory/session.py", "M",
                          added_lines=list(enumerate(lines, 1)))]
    assert builtin_sentinel_findings(changes, {}) == []


def test_env_fallback_literal_still_flags():
    """A hardcoded fallback INSIDE the lookup is a committed secret:
    the prefix shapes scan env-read lines too — only the generic
    'secret assignment' shape yields on them."""
    token = "ghp_" + "b" * 36
    changes = [
        FileChange("factory/session.py", "M", added_lines=[
            (1, f'    return os.getenv("SEAT_TOKEN", "{token}")'),
        ]),
    ]
    ids = [f.id for f in builtin_sentinel_findings(changes, {})]
    assert "credential_added" in ids
    finding = next(f for f in builtin_sentinel_findings(changes, {})
                   if f.id == "credential_added")
    assert token not in finding.text
