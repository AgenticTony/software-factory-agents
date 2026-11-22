"""F-105 acceptance tests — the lint-suppression sentinel counts NET.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f105.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** fixture git repos, the real diff machinery
  (:func:`factory.gates.diff_file_changes`) and the real builtin sentinel
  pass (:func:`factory.sentinels.builtin_sentinel_findings`) — the
  test_f25/test_f46 harness pattern. AC-2 additionally runs the real
  ``run_l0`` over the J-2 fixture (unconfined runner, trivial commands).
* **Nothing is stubbed:** no seats, no network, no model.

The J-2 shape (run 20261120T153807Z-84e974, attempt 1,
``backend/app/services/draft_service.py:1387``): the implementer inserted
a new typed ``except`` above an unchanged ``except Exception as e:
# noqa: BLE001`` and reworked the handler bodies; git re-aligned the
unchanged noqa line as removed plus added, and the sentinel flagged the
added copy. The fixtures below reproduce that alignment with real git —
AC-2 pins that the diff REALLY carries the line on both sides, so the
test cannot pass vacuously if a fixture stops re-aligning.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _support.tokens import fake_github_token

from factory.gates import diff_file_changes
from factory.l0 import run_l0
from factory.sentinels import builtin_sentinel_findings

# Runtime-built (F-58 AC-G): the source carries no high-entropy literal.
GHP = fake_github_token("f105-ghp")

# Both halves of every fixture live in the one risk flag's globs; the
# cross-file and rename rows stay inside them too, so only the net rule
# (never the risk-path rule) decides a row's outcome.
RISK = {"external_send": ["src/pay/**", "backend/app/services/**"]}

# The exact J-2 re-alignment, minimised from the real diff
# (5441439...7caa6f8): a new typed except above the unchanged noqa
# handler, with the body edits that made git re-align the line.
J2_PATH = "backend/app/services/send_service.py"
J2_BASE = '''\
def _send_with_pdfs(application, attachments, smtp):
    try:
        dispatch(application, attachments, smtp)
    except PdfVerifyError as e:
        application.error = str(e)
        logger.error("PDF text-layer verification failed - send blocked: %s", e)
        return
    except Exception as e:  # noqa: BLE001
        application.status = "failed"
        application.error = f"Email send failed: {e}"
        logger.error("Tailored application send failed: %s", e)
'''
J2_HEAD = '''\
def _send_with_pdfs(application, attachments, smtp):
    try:
        dispatch(application, attachments, smtp)
    except PdfVerifyError as e:
        application.error = str(e)
        logger.error("PDF text-layer verification failed - send blocked: %s", e)
        return
    except OriginalCvUnreadableError as e:
        # the send never left - the message says what to do first
        # (re-upload), because a plain retry would fail the same way.
        application.status = "failed"
        application.error = CV_UNREADABLE_MESSAGE
        logger.error(
            "Tailored application send aborted - original CV unreadable "
            "(%s): %s", type(e).__name__, e, exc_info=True,
        )
        return
    except Exception as e:  # noqa: BLE001
        # a dispatch failure the user can only wait out and retry.
        application.status = "failed"
        application.error = SEND_FAILED_MESSAGE
        logger.error(
            "Tailored application send failed (%s): %s",
            type(e).__name__, e, exc_info=True,
        )
'''

BASE_PROFILE = {
    "name": "fixture",
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
}

TASK = {"id": "T-1", "risk": []}


# ---------------------------------------------------------------- helpers --


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path, name: str, files: dict[str, str | bytes]) -> Path:
    """A fixture git repo with one base commit on ``main``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _write(repo: Path, rel: str, text: str | bytes) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        target.write_bytes(text)
    else:
        target.write_text(text)


def _commit(repo: Path, files: dict[str, str | bytes],
            rename: tuple[str, str] | None = None) -> None:
    """One commit on ``feature`` rewriting the given files (optionally a
    rename, so the diff machinery sees an R row)."""
    _git(repo, "checkout", "-b", "feature")
    if rename:
        (repo / rename[1]).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", rename[0], rename[1])
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")


def _suppression_findings(tmp_path: Path, name: str,
                          base: dict[str, str | bytes],
                          head: dict[str, str | bytes],
                          rename: tuple[str, str] | None = None) -> list:
    """``lint_suppression_added`` findings of the real sentinel pass over
    a real git diff of ``base`` → ``head``."""
    repo = _repo(tmp_path, name, base)
    _commit(repo, head, rename=rename)
    changes = diff_file_changes(repo, "main", "feature")
    return [
        f for f in builtin_sentinel_findings(changes, RISK)
        if f.id == "lint_suppression_added"
    ]


def _noqa_files(findings: list) -> set:
    return {f.file for f in findings}


# ------------------- AC-1: net count per file, per suppression ---------------


def test_ac_1(tmp_path):
    """One table over real-git diff fixtures: a file is flagged only when
    the diff adds more of one suppression (rule code included) to it than
    it removes; re-alignments, moves and string data never flag."""
    table = [
        # The J-2 class: a new typed handler inserted above, the old
        # handler re-emitted with a reworked body — git re-aligns the
        # UNCHANGED suppression line as removed plus added. Net zero,
        # quiet (this is the minimal shape of AC-2's fixture; the diff
        # really carries the line on both sides, or this row would flag
        # under the old code and fail red-first).
        (
            "re-aligned unchanged noqa line",
            {"src/pay/swap.py": (
                "def run():\n"
                "    try:\n"
                "        step()\n"
                "    except Exception as e:  # noqa: BLE001\n"
                "        handle_a(e)\n"
                "        handle_b(e)\n"
            )},
            {"src/pay/swap.py": (
                "def run():\n"
                "    try:\n"
                "        step()\n"
                "    except RetryableError as e:\n"
                "        handle_a(e)\n"
                "        handle_new(e)\n"
                "    except Exception as e:  # noqa: BLE001\n"
                "        # the handler now explains itself\n"
                "        handle_a(e)\n"
                "        handle_changed(e)\n"
            )},
            None,
            set(),
        ),
        # A suppression moved within ONE file: -1 here, +1 there, net 0.
        (
            "noqa moved within one file",
            {"src/pay/move.py": (
                "def alpha():\n"
                "    x = 1  # noqa: E501\n\n\n"
                "def beta():\n"
                "    y = 2\n"
            )},
            {"src/pay/move.py": (
                "def alpha():\n"
                "    x = 1\n\n\n"
                "def beta():\n"
                "    y = 2  # noqa: E501\n"
            )},
            None,
            set(),
        ),
        # The plain new suppression: today's case, still a finding.
        (
            "new noqa",
            {"src/pay/new.py": "x = 1\n"},
            {"src/pay/new.py": "x = 2  # noqa: BLE001\n"},
            None,
            {"src/pay/new.py"},
        ),
        # Rule code changed: different keys, the added one has no removal.
        (
            "rule code swap",
            {"src/pay/code.py": "x = 1  # noqa: BLE001\n"},
            {"src/pay/code.py": "x = 1  # noqa: E501\n"},
            None,
            {"src/pay/code.py"},
        ),
        # Bare noqa replacing a coded one: it widens — its own key, flags.
        (
            "bare noqa replacing a coded one",
            {"src/pay/bare.py": "x = 1  # noqa: BLE001\n"},
            {"src/pay/bare.py": "x = 1  # noqa\n"},
            None,
            {"src/pay/bare.py"},
        ),
        # Per FILE, not across files: the source goes quiet, the
        # receiving file flags.
        (
            "suppression moved to another file",
            {
                "src/pay/a.py": "x = 1  # noqa: BLE001\n",
                "src/pay/b.py": "y = 2\n",
            },
            {
                "src/pay/a.py": "x = 1\n",
                "src/pay/b.py": "y = 2  # noqa: BLE001\n",
            },
            None,
            {"src/pay/b.py"},
        ),
        # Two identical suppressions on different lines, both re-aligned.
        (
            "two identical suppressions both re-aligned",
            {"src/pay/twin.py": (
                "def one():\n"
                "    a = 1  # noqa: E501\n\n\n"
                "def two():\n"
                "    b = 2  # noqa: E501\n"
            )},
            {"src/pay/twin.py": (
                "def two():\n"
                "    b = 2  # noqa: E501\n\n\n"
                "def one():\n"
                "    a = 1  # noqa: E501\n"
            )},
            None,
            set(),
        ),
        # Quoted suppressions are data: nothing is counted on either side.
        (
            "suppression inside a string literal",
            {"src/pay/str.py": 'DOC = "old"\n'},
            {"src/pay/str.py": 'DOC = "old"\nNOTE = "# noqa: BLE001"\n'},
            None,
            set(),
        ),
        # CRLF: whitespace normalises out of the key, so a re-aligned
        # CRLF line nets to zero...
        (
            "CRLF re-aligned suppression",
            {"src/pay/crlf.py": b"x = 1  # noqa: BLE001\r\ny = 2\r\n"},
            {"src/pay/crlf.py": b"y = 2\r\nx = 1  # noqa: BLE001\r\n"},
            None,
            set(),
        ),
        # ...while a NEW suppression on a CRLF file still flags.
        (
            "CRLF new suppression",
            {"src/pay/crlf2.py": b"x = 1\r\n"},
            {"src/pay/crlf2.py": b"x = 1  # noqa: BLE001\r\n"},
            None,
            {"src/pay/crlf2.py"},
        ),
        # type: ignore with and without a bracket code: both directions
        # change the key, both flag (the narrowing one too — a human
        # reviews; a silently re-scoped ignore is the costlier miss).
        (
            "bracket code added to type: ignore",
            {"src/pay/br1.py": "x = f()  # type: ignore\n"},
            {"src/pay/br1.py": "x = f()  # type: ignore[attr-defined]\n"},
            None,
            {"src/pay/br1.py"},
        ),
        (
            "bracket code dropped from type: ignore",
            {"src/pay/br2.py": "x = f()  # type: ignore[attr-defined]\n"},
            {"src/pay/br2.py": "x = f()  # type: ignore\n"},
            None,
            {"src/pay/br2.py"},
        ),
        # Two suppressions on one line where one is new: the surplus
        # flags (the first match's key runs to end of line, so a
        # co-located second suppression can never hide inside it).
        (
            "second suppression added on the same line",
            {"src/pay/same.py": "x = 1  # noqa: E501\n"},
            {"src/pay/same.py": "x = 2  # noqa: E501  # type: ignore\n"},
            None,
            {"src/pay/same.py"},
        ),
        # The J-1 lesson, applied to the removal side: a removed line
        # that held nothing but string content (docstring prose quoting
        # a suppression) never cancels — prose is not a suppression, and
        # letting it neutralise a real added one would be an evasion.
        (
            "docstring prose removal does not cancel a real addition",
            {"src/pay/doc.py": (
                'def f():\n'
                '    """Docs.\n\n'
                '    The phrase `# noqa: BLE001` appears in prose here.\n'
                '    """\n'
                '    return 1\n'
            )},
            {"src/pay/doc.py": (
                'def f():\n'
                '    """Docs."""\n'
                '    return 2  # noqa: BLE001\n'
            )},
            None,
            {"src/pay/doc.py"},
        ),
        # Round 1: a suppression comment carrying a credential-shaped
        # token still flags — and the finding's key text is redacted
        # (asserted in full by test_review1_finding_key_is_redacted).
        (
            "suppression comment carrying a token",
            {"src/pay/tok.py": "x = 1\n"},
            {"src/pay/tok.py": f"x = 2  # noqa: S105 token={GHP}\n"},
            None,
            {"src/pay/tok.py"},
        ),
        # A rename with one suppression added: net +1 in the (renamed)
        # file — the per-file counter sees one change, both halves of it.
        (
            "rename with one suppression added",
            {"src/pay/old.py": "".join(
                f"value_{n} = {n}\n" for n in range(1, 11)
            )},
            {"src/pay/new.py": "".join(
                [f"value_{n} = {n}\n" for n in range(1, 11)]
                + ["value_11 = 11  # noqa: BLE001\n"]
            )},
            ("src/pay/old.py", "src/pay/new.py"),
            {"src/pay/new.py"},
        ),
    ]
    for name, base, head, rename, expected in table:
        findings = _suppression_findings(tmp_path, name.replace(" ", "-"),
                                         base, head, rename=rename)
        assert _noqa_files(findings) == expected, (
            f"{name}: expected {sorted(expected)}, got "
            f"{sorted(_noqa_files(findings))}: "
            f"{[f.text for f in findings]}"
        )


# ----------------------------- AC-2: the J-2 case, replayed ------------------


def test_ac_2(tmp_path):
    """A fixture diff in the exact J-2 shape — a new typed ``except``
    inserted above an unchanged ``except Exception as e:  # noqa: BLE001``
    that git re-aligns as removed plus added — passes L0's sentinel."""
    repo = _repo(tmp_path, "j2", {J2_PATH: J2_BASE})
    _commit(repo, {J2_PATH: J2_HEAD})
    changes = diff_file_changes(repo, "main", "feature")
    change = next(c for c in changes if c.path == J2_PATH)

    # The fixture REALLY re-aligns the unchanged noqa line: it appears on
    # both sides of the diff with identical text. Without this pin the
    # row below could pass vacuously if a fixture edit stopped aligning.
    removed = [text for _no, text in change.removed_lines if "noqa" in text]
    added = [text for _no, text in change.added_lines if "noqa" in text]
    assert removed == ["    except Exception as e:  # noqa: BLE001"], removed
    assert added == ["    except Exception as e:  # noqa: BLE001"], added

    # The sentinel pass itself: quiet (the file gains nothing it lost).
    assert [
        f for f in builtin_sentinel_findings(changes, RISK)
        if f.id == "lint_suppression_added"
    ] == []

    # End to end through L0: the attempt that cost $3.04 passes its gate.
    profile = dict(BASE_PROFILE)
    profile["paths"] = {"risk": RISK, "protected": []}
    report = run_l0(
        profile, repo, "main", "feature",
        task={"id": "T-2", "risk": ["external_send"]},
    )
    assert report.verdict == "pass", [f.text for f in report.findings]
    sentinel = next(
        r for r in report.sentinel_results
        if r["name"] == "lint_suppression_added"
    )
    assert sentinel["verdict"] == "pass", sentinel


# ------------------- review round 1: the finding's key is redacted ----------


def test_review1_finding_key_is_redacted(tmp_path):
    """Round 1: the finding interpolates the key, and the key is built
    from the raw comment — a credential-shaped token riding a
    suppression comment must not reach reports, prompts or PR bodies in
    the clear (the no-echo rule the line echo already follows)."""
    findings = _suppression_findings(
        tmp_path, "review1-token",
        {"src/pay/tok.py": "x = 1\n"},
        {"src/pay/tok.py": f"x = 2  # noqa: S105 token={GHP}\n"},
    )
    assert len(findings) == 1 and findings[0].file == "src/pay/tok.py", findings
    text = findings[0].text
    assert "[redacted]" in text, text
    assert GHP not in text, text
