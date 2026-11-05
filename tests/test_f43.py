"""F-43 acceptance tests — the egress redaction seam (BUILD-ORDER §F-43).

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f43.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed: AC-1 runs the real ``run_once`` pipeline —
clone manager, queue, L0/L1 through the pinned srt sandbox, the real PR-body
builder and the real pre-push fence — with only the model seats scripted (the
test_f42 harness shape). AC-2/AC-3 exercise the real ``check_pr_text``, the
real vent flush into a real progress log on disk, and the real unparsable
stdout dump. AC-4 exercises the real ``read_trace`` and the real
``render_status``. AC-5 runs the real diff scanners over the real corpus
files (the F-33 gate). AC-6 parses the real orchestrator/session source. No
model spend in this file.
"""

import ast
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from factory.contract import acceptance_hash, load_profile
from factory.gates import FileChange
from factory.importer import ImportRefused, run_import
from factory.orchestrator import PullRequest, run_once
from factory.progress import ProgressLog
from factory.publish import PushRefused, check_pr_text
from factory.replay import patch_findings
from factory.session import SessionConfigError, SessionRecord, _save_session_stdout
from factory.state import TaskQueue
from factory.status import render_status
from factory.vent import VentCollector, flush_vents

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)
REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = (
    "from impl import answer\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    "    assert answer() == 42\n"
)

# The planted seat token (C-1): a long literal no pattern shape matches, so
# only the literal scrub can catch it — the exact exfiltration the review
# proved. The echo (M-8) is a bare ANTHROPIC_AUTH_TOKEN=<value> assignment;
# review round 1 (finding 8) added the Z.ai-shaped value — the production
# key is <id>.<secret>, NOT a JWT — so the tests cover both shapes.
SEAT_TOKEN = "seat-tok-0f1e2d3c4b5a6978879601525354555657585960"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
ENV_ECHO = f"ANTHROPIC_AUTH_TOKEN={JWT}"
# A Z.ai credential's real shape: <id>.<secret>, no eyJ, no known prefix.
ZAI_KEY = "d1f4a9c2e8b37d65f0a2.Sk7VmQ2xP9wL4TtN6bZ"
ENV_ECHO_ZAI = f"ANTHROPIC_AUTH_TOKEN={ZAI_KEY}"

# m-16's three injection shapes: a screen clear, an OSC-8 hyperlink and a
# cursor move (each alone is enough to spoof the owner's terminal).
SCREEN_CLEAR = "\x1b[2J\x1b[H"
OSC8_LINK = "\x1b]8;;https://evil.example\x1b\\click me\x1b]8;;\x1b\\"
CURSOR_MOVE = "\x1b[5A\x1b[?25l"


# ---------------------------------------------------------------- helpers --


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class _Env:
    """The test_f42 harness shape, trimmed to one passing run: a fixture
    repo with a bare remote, a queue, and the real run_once pipeline."""

    def __init__(self, tmp_path: Path, *, title: str = "Make answer() return 42"):
        self.root = tmp_path / "factory"
        self.root.mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text(
            "def test_base():\n    assert True\n"
        )
        profile = {
            "name": "fixture",
            "repo": {"path": str(self.repo), "default_branch": "main",
                     "remote": str(self.remote), "merge_method": "squash"},
            "commands": {"setup": "true",
                         "test": f"{shlex.quote(sys.executable)} -m pytest -q "
                                 f"-p no:cacheprovider"},
            "paths": {"risk": {}, "protected": [".factory/**"]},
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        (factory_dir / "project.yaml").write_text(json.dumps(profile))
        self.queue_path = factory_dir / "tasks.json"
        acceptance = [{"id": "AC-1", "text": "answer() returns 42",
                       "check": {"type": "test", "ref": REF}}]
        task = {
            "id": "T-1", "title": title, "source": "docs/T-1.md",
            "kind": "build", "priority": "P1", "status": "ready", "depends_on": [],
            "risk": [], "gates": {"spec_approval": True, "merge": "human",
                                  "deploy": "human"},
            "acceptance": acceptance,
            "acceptance_hash": acceptance_hash(acceptance),
            "approved_at": "2026-10-18T10:00:00Z", "approved_by": "owner",
            "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                       "wall_clock_min": 10},
            "attempts": [], "links": {},
        }
        self.queue_path.write_text(
            json.dumps({"version": 1, "project": "fixture", "tasks": [task]},
                       indent=2) + "\n"
        )
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)],
                       check=True)
        self.prs: list[PullRequest] = []

    def open_pr(self, pr: PullRequest) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def task(self) -> dict:
        return TaskQueue(self.queue_path).get("T-1")

    def run(self, seats):
        from factory.mergeorder import Protection

        return run_once(
            self.queue_path, factory_root=self.root, session_runner=seats,
            open_pr=self.open_pr, now=lambda: OFF_PEAK,
            protection=lambda remote, branch: Protection(checked=True, strict=True),
            pr_state=lambda url: "open",
        )


class _Seats:
    """Stub seats: a completed implementer whose result text carries the
    planted secrets, and a passing review whose one minor finding carries
    them too — the review still passes (a minor never fails a round), so
    the publish path runs with secrets in every session-text input."""

    def __init__(self, review: str):
        self.review = review
        self.results: dict[str, str] = {}

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests").mkdir(exist_ok=True)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            result = (f"Implemented answer(). The seat env holds {SEAT_TOKEN} "
                      f"and the runner echoes {ENV_ECHO} and {ENV_ECHO_ZAI}.")
            self.results["implementer"] = result
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                result=result,
            )
        self.results[seat] = self.review
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10}, result=self.review,
        )


def _review_with_finding() -> str:
    """A passing review carrying one minor finding whose text holds both
    planted secrets — the L2 finding text C-1 names."""
    return json.dumps({
        "verdict": "pass",
        "findings": [{
            "id": "R-1", "axis": "spec", "severity": "minor", "file": "impl.py",
            "line": 1,
            "text": f"the env leaks {SEAT_TOKEN}; runner said {ENV_ECHO} "
                    f"and {ENV_ECHO_ZAI}",
        }],
    })


# --- AC-1 (C-1): the PR body's session-text inputs pass the seam ------------


def test_ac_1(tmp_path, monkeypatch):
    env = _Env(tmp_path)
    seats = _Seats(_review_with_finding())
    # The run's egress secrets are exactly the seat token the orchestrator
    # hands the trace writer today; plant ours there.
    import factory.orchestrator as orch

    monkeypatch.setattr(orch, "seat_token", lambda: SEAT_TOKEN)
    result = env.run(seats)
    assert result.outcome == "pr_opened", result
    (pr,) = env.prs
    body = pr.body

    # The secrets really were in the session text (the path was exercised,
    # not skipped): both inputs carried them before the seam.
    assert SEAT_TOKEN in seats.results["implementer"]
    assert SEAT_TOKEN in seats.review

    # Neither reaches the rendered body — not the literal token, not the
    # echoed env values (the JWT AND the Z.ai-shaped <id>.<secret>).
    assert SEAT_TOKEN not in body, "the literal seat token reached the PR body"
    assert JWT not in body, "the echoed ANTHROPIC_AUTH_TOKEN value reached the body"
    assert ZAI_KEY not in body, (
        "the Z.ai-shaped env echo (<id>.<secret>, not a JWT) reached the body"
    )
    # The finding itself still renders (redacted, not dropped): a minor
    # finding belongs in the body, and its redaction proves the seam ran.
    assert "[redacted]" in body, "the finding text was dropped, not scrubbed"
    assert "ANTHROPIC_AUTH_TOKEN=[redacted]" in body, (
        "the bare env echo was not pattern-redacted"
    )


# --- AC-2 (C-1): check_pr_text is a second, independent fence ---------------


def test_ac_2():
    # Refuses the body — with the raw token, never scrubbed first: the
    # fence does not depend on the seam having run.
    body = f"## Summary\n\nthe token is {SEAT_TOKEN} and the echo {ENV_ECHO}\n"
    with pytest.raises(PushRefused) as exc:
        check_pr_text("T-1: fine", body, secrets=(SEAT_TOKEN,))
    message = str(exc.value)
    assert SEAT_TOKEN not in message, "the refusal echoes the token"
    assert JWT not in message, "the refusal echoes the token's value"
    assert "credential" in message.lower(), "the refusal must name itself"
    # Refuses the title too.
    with pytest.raises(PushRefused):
        check_pr_text(f"T-1: {SEAT_TOKEN}", "clean body", secrets=(SEAT_TOKEN,))
    # A clean text passes, and a literal below the secret-length floor is
    # noise, not a secret (scrub_literal's MIN_SECRET_LEN rule).
    check_pr_text("T-1: fine", "a clean body", secrets=(SEAT_TOKEN,))
    check_pr_text("T-1: short", "contains tok", secrets=("tok",))


# --- AC-3 (M-8): vents and unparsable-record dumps pass the same seam -------


def test_ac_3(tmp_path):
    # Vents: the orchestrator-side flush writes the task's progress log on
    # disk — the file factory status later prints from.
    task_dir = tmp_path / "progress" / "fixture"
    task_dir.mkdir(parents=True)
    log = ProgressLog(task_dir / "T-1.jsonl")
    planted = (f"the runner env is {SEAT_TOKEN} / {ENV_ECHO} / {ENV_ECHO_ZAI} "
               f"and it draws {SCREEN_CLEAR}{OSC8_LINK}")
    flush_vents(log, [{"category": "environment", "text": planted, "at": "",
                       "run_id": "r", "task_id": "T-1", "seat": "implementer",
                       "turn": 1}],
                run_id="r", task_id="T-1", seat="implementer",
                secrets=(SEAT_TOKEN,))
    stored = (task_dir / "T-1.jsonl").read_text()
    assert SEAT_TOKEN not in stored, "the literal seat token reached the vent log"
    assert JWT not in stored, "the echoed token value reached the vent log"
    assert ZAI_KEY not in stored, "the Z.ai-shaped echo reached the vent log"
    assert "\x1b" not in stored, "control sequences reached the vent log"

    # The in-sandbox half records the same scrub before anything crosses
    # the boundary (the collector the session runner builds with the token).
    collector = VentCollector(run_id="r", task_id="T-1", seat="implementer",
                              secrets=(SEAT_TOKEN,))
    vent = collector.record("environment", planted)
    assert SEAT_TOKEN not in vent["text"] and JWT not in vent["text"]
    assert "\x1b" not in vent["text"]

    # The unparsable-record dump: the whole raw stdout of a session whose
    # record would not parse, written under <factory-root>/sessions/.
    run_dir = tmp_path / "runs" / "fixture" / "r1"
    run_dir.mkdir(parents=True)
    dump = _save_session_stdout(
        f"line one\n{SEAT_TOKEN}\n{ENV_ECHO}\n{ENV_ECHO_ZAI}\n{CURSOR_MOVE}rewritten\n",
        run_dir, tmp_path, secrets=(SEAT_TOKEN,),
    )
    dumped = Path(dump).read_text()
    assert SEAT_TOKEN not in dumped, "the literal token reached the dump"
    assert JWT not in dumped, "the echoed token value reached the dump"
    assert ZAI_KEY not in dumped, "the Z.ai-shaped echo reached the dump"
    assert "\x1b" not in dumped, "control sequences reached the dump"
    assert "rewritten" in dumped, "the seam shredded ordinary text"


# --- AC-4 (m-16): terminal-bound text is control-sequence stripped ----------


def test_ac_4(tmp_path):
    from factory.egress import strip_control_sequences

    # The three shapes m-16 names, each stripped whole; ordinary text, tabs
    # and newlines survive.
    for sequence in (SCREEN_CLEAR, OSC8_LINK, CURSOR_MOVE):
        cleaned = strip_control_sequences(f"before{sequence}after")
        assert "\x1b" not in cleaned
        assert "before" in cleaned and "after" in cleaned
    kept = strip_control_sequences("a\tb\nc")
    assert kept == "a\tb\nc"

    # Trace lines printed to a terminal: read_trace is the one reading
    # seam, and what it prints carries no escape byte.
    from factory.trace import read_trace

    trace_file = tmp_path / "run.jsonl"
    trace_file.write_text(
        json.dumps({"type": "tool_call", "note": f"ran {OSC8_LINK}"}) + "\n"
        + json.dumps({"type": "phase_start", "phase": "L0"}) + "\n"
    )
    printed: list[str] = []
    read_trace(trace_file, out=printed.append)
    assert printed and all("\x1b" not in line for line in printed), printed

    # factory status output: the rendered page is what the terminal (and
    # the dashboard) sees. The vent arrives through the seam (stored
    # clean), and anything else on the page is stripped at render.
    env = _Env(tmp_path / "status")
    task_dir = env.root / "progress" / "fixture"
    task_dir.mkdir(parents=True)
    log = ProgressLog(task_dir / "T-1.jsonl")
    flush_vents(log, [{"category": "environment", "text": f"evil{OSC8_LINK}",
                       "at": "", "run_id": "r", "task_id": "T-1",
                       "seat": "implementer", "turn": 1}],
                run_id="r", task_id="T-1", seat="implementer",
                secrets=(SEAT_TOKEN,))
    page = render_status(TaskQueue(env.queue_path).doc, project="fixture",
                         factory_root=env.root, now=OFF_PEAK)
    assert "\x1b" not in page, "factory status printed a control sequence"
    assert "click me" in page, "the vent text itself was shredded"


# --- AC-5 (I-10): JWT / Bearer / unquoted YAML password shapes --------------


def _shape_findings(line: str, path: str = "src/conf.py"):
    from factory.sentinels import builtin_sentinel_findings

    change = FileChange(path, "M", added_lines=[(1, line)])
    return builtin_sentinel_findings([change], {})


def test_ac_5():
    from factory.sentinels import redact_credentials

    jwt2 = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0."
            "dozjgNryP4J3jVmNHl0w5N65Yv-mYv_z7XvvVXyL0eI")
    shapes = {
        "JSON web token": f"auth = getconf_token('{jwt2}')",
        "bearer token": 'curl -H "Authorization: Bearer '
                        "0f1e2d3c4b5a6978879601525354555657585960\" url",
        "unquoted YAML password": "  password: hunter2hunter2hunter2x",
    }
    for shape, line in shapes.items():
        # redact_credentials catches the value …
        redacted = redact_credentials(line)
        assert "hunter2" not in redacted or shape != "unquoted YAML password"
        assert not re.search(r"eyJ[A-Za-z0-9_-]{7,}\.", redacted), shape
        assert "0f1e2d3c4b5a6978" not in redacted, shape
        # … and the credential sentinel flags the added line, naming the
        # shape without echoing the value.
        findings = [f for f in _shape_findings(line) if f.id == "credential_added"]
        assert findings, f"{shape}: the sentinel missed the shape"
        assert shape in findings[0].text, findings[0].text
        for secret in (jwt2, "0f1e2d3c4b5a69788796", "hunter2hunter2"):
            assert secret not in findings[0].text, "the finding echoes the value"

    # One F-33 attack diff per shape: each file flags credential_added
    # through the real replay path, under this repo's real risk globs.
    profile = load_profile(REPO_ROOT / ".factory" / "project.yaml")
    globs = (profile.get("paths") or {}).get("risk") or {}
    for name, shape in (("credential_added-7.patch", "JSON web token"),
                        ("credential_added-8.patch", "bearer token"),
                        ("credential_added-9.patch", "unquoted YAML password")):
        patch = REPO_ROOT / "corpus" / "attack" / name
        assert patch.is_file(), f"missing attack diff for the {shape} shape"
        findings = patch_findings(patch.read_text(), globs)
        fired = [f for f in findings if f.id == "credential_added"]
        assert fired and shape in fired[0].text, f"{name}: {shape} did not flag"

    # The benign corpus stays clean: zero findings over every patch, the
    # same gate test_f33 runs (measured 0 before these shapes landed).
    hits = [
        f"{p.name}: {f.id}"
        for p in sorted((REPO_ROOT / "corpus" / "benign").glob("*.patch"))
        for f in patch_findings(p.read_text(), globs)
    ]
    assert not hits, "false positives on the benign corpus:\n" + "\n".join(hits)


# --- hostile-pass finding: a control sequence splitting the secret ----------


def test_hostile_control_split_secret_does_not_reassemble():
    """The pre-commit hostile pass found this: a token split by an escape
    sequence (``<half>\\x1b[0m<half>``) survives the literal scrub — it is
    not the literal while split — and the control-sequence strip then
    MERGES the halves into the whole token, after every scrub has run.

    The fix keeps the contract's composition order (literals, then
    patterns, then strip) and adds one rule: the strip is itself a
    text-rewriting boundary, so when it changes the text the scrubbers
    re-judge the rewritten form — a secret must not reassemble behind
    them. Split across a NEWLINE stays a known limit (the seam keeps line
    structure by design; the trace writer has the same limit since F-30),
    and an encoded secret (base64) is out of the seam's scope — no shape
    list decodes."""
    from factory.egress import scrub_session_text

    split = SEAT_TOKEN[:20] + "\x1b[0m" + SEAT_TOKEN[20:]
    assert SEAT_TOKEN not in scrub_session_text(split, (SEAT_TOKEN,))
    split_echo = f"ANTHROPIC_AUTH_TOKEN={JWT[:30]}\x1b[0m{JWT[30:]}"
    assert JWT not in scrub_session_text(split_echo, ())
    # The strip itself is unchanged: sequences still go, text stays.
    assert "\x1b" not in scrub_session_text(split, (SEAT_TOKEN,))


# --- AC-6: every egress channel is enumerated, and new ones trip a test -----


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _kw(call: ast.Call, name: str):
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _mentions(node, *names: str) -> bool:
    return any(
        (isinstance(n, ast.Name) and n.id in names)
        or (isinstance(n, ast.Attribute) and n.attr in names)
        for n in ast.walk(node)
    )


def _tainted(node: ast.AST, attrs: tuple, names: tuple) -> bool:
    """Whether ``node``'s subtree carries session-derived text: an
    attribute access in ``attrs`` (``record.result``) or a bare name in
    ``names`` (importer's ``proposal``/``text``). Attributes and names are
    checked separately — a variable merely CALLED ``result`` (a RunResult,
    a concurrent Future) is not session text until one of its session
    fields is touched."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in attrs:
            return True
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
    return False


# The modules the tripwire watches, derived by grepping the factory package
# for the expressions that carry session-derived text
# (``grep -lE '\.result\b|\.reason\b|\.vents\b|\.findings\b|\.raw_output\b|
# proc\.stderr|proc\.stdout' factory/*.py``) and keeping every module where
# those hits are MODEL-SESSION values (session-record fields, parsed L2
# findings, importer proposals) rather than plumbing over its own results
# (dropped for that reason: gates.py/sandbox.py/mergeorder.py/toolkit.py/
# publish.py/rehearsal.py — CommandResult and exception plumbing; l2.py —
# parse/merge machinery whose outputs reach egress only through the guarded
# reports channel).
_SESSION_TEXT_ATTRS = ("result", "reason", "vents", "findings", "raw_output")

_EGRESS_MODULES = {
    "factory/orchestrator.py": {
        "names": (), "extra_attrs": (),
        "allow": {
            # The seam; the PR-body writer (rule 1 proves scrubbed inputs);
            # the vent flush (rule 3 proves the secrets).
            "scrub_session_text", "scrub_session_value", "build_pr_body",
            "flush_vents",
            # Local-disk records under the factory root (attempt record /
            # queue, numbered findings, stored reports, the trace writer —
            # _clean scrubs at write) and the in-memory results the CLI
            # summarizes.
            "attempt_record", "append_attempt", "append", "_numbered_findings",
            "_store", "gate_checks", "session_end", "run_end", "phase",
            "_log_vents", "_is_quota", "_tokens", "_turns_spent", "finish",
            "RunResult", "LoopResult", "transition", "render",
            # Verdict plumbing (pure merges/parses over finding objects).
            "apply_vote", "apply_convergence", "final_review_verdict",
            "parse_reviews_with_retry", "parse_verifier_vote", "_review_drops",
            # Prompt builders: session text riding INTO a session's prompt
            # is the review flow itself (ingress), not egress.
            "_implementer_prompt", "_reviewer_prompt", "_verifier_prompt",
            "dumps",
        },
    },
    "factory/importer.py": {
        # ``proposal`` is the parsed session output; ``text`` the session's
        # raw output — the two forms importer session text takes.
        "names": ("proposal", "text"),
        "extra_attrs": (),
        "allow": {
            # The seam; the parse (pre-scrub, in-memory); the guarded
            # save/keep/abort writers (test_review3 asserts their secrets)
            # and the file write whose payload that seam produced; the
            # scratch read (input, not egress) and the suffix probe (pure);
            # plan/apply/render_summary — guarded by the scrub-before-plan
            # rule; the constructors (messages and the in-memory result).
            "scrub_session_text", "scrub_session_value", "parse_proposal",
            "_save_refused_output", "keep_refused_import_output",
            "_abort_refusal", "plan_import", "apply_import", "render_summary",
            "ImportRefused", "ImportResult", "write", "read_text",
            "_refusal_suffix",
        },
    },
    "factory/session.py": {
        "names": (),
        # proc.stderr / proc.stdout: the sandboxed runner's raw streams.
        "extra_attrs": ("stderr", "stdout"),
        "allow": {
            # The seam; the dump write (rule 5 proves the secrets; the
            # payload is the seam's own output); the record transport
            # (stdout protocol, asdict/dumps) and its error constructors
            # (the one stderr embed scrubs — test_review5; _GitState and
            # the interpreter probe's RunError are orchestrator-owned git /
            # probe output, not session text); Future.result() in the
            # fan-out (the record arrives through it, already seam-tapped).
            "scrub_session_text", "_save_session_stdout", "write",
            "SessionConfigError", "SessionRecord", "from_dict", "asdict",
            "RunError", "_GitState", "result", "dumps", "loads", "print",
            "splitlines", "startswith", "append",
        },
    },
    "factory/cli.py": {
        "names": (),
        # The CLI's session-text carrier is the import result's
        # raw_output; run-loop prints are orchestrator-built summaries and
        # the toolkit-vet findings are scanner output, so "findings" is
        # not tainted here. print is deliberately NOT allowed, so
        # print(result.raw_output) trips.
        "attrs": ("raw_output",),
        "extra_attrs": (),
        "allow": {
            # The CLI's keep path (test_review3 asserts its secrets);
            # apply_import (the scrubbed plan — the scrub-before-plan
            # rule); nothing else in the CLI may receive session text.
            "keep_refused_import_output", "seat_token", "apply_import",
        },
    },
    "factory/eval.py": {
        "names": (),
        "extra_attrs": (),
        "allow": {
            # Eval replays sessions through the orchestrator's own guarded
            # machinery; report building is local-disk.
            "run_sessions_parallel", "parse_verifier_vote",
            "orchestrator", "_verifier_prompt", "append", "get", "dumps",
            "str", "len", "list", "sorted",
        },
    },
    "factory/status.py": {
        "names": (),
        "extra_attrs": (),
        "allow": {"append", "join", "get", "str", "len", "sorted", "format"},
    },
    "factory/vent.py": {
        "names": (),
        "extra_attrs": (),
        "allow": {
            # clean_text IS the seam call here; the collector/log are the
            # guarded channel (rule 3).
            "scrub_session_text", "clean_text", "record", "append_many",
            "append", "get", "str", "isinstance",
        },
    },
}

_PURE_OPS = {
    # Expression plumbing that cannot write anywhere by itself.
    "bool", "str", "len", "sorted", "list", "dict", "set", "tuple",
    "next", "any", "all", "sum", "min", "max", "enumerate", "isinstance",
    "getattr", "join", "search", "match", "fullmatch", "get", "items",
    "keys", "values", "strip", "lower", "upper", "split", "splitlines",
    "startswith", "format", "append", "loads", "dumps",
}


def _offenders(relative: str, source: str | None = None) -> list[str]:
    """Calls in ``relative`` (or in ``source``, doctored) that receive
    session-derived text without the seam, named file:line."""
    text = source if source is not None else (REPO_ROOT / relative).read_text()
    config = _EGRESS_MODULES[relative]
    attrs = config.get("attrs", _SESSION_TEXT_ATTRS) + config["extra_attrs"]
    allowed = config["allow"] | _PURE_OPS
    found = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        if not _tainted(node, attrs, config["names"]):
            continue
        if _callee_name(node) not in allowed:
            found.append(f"{relative}:{node.lineno}: {_callee_name(node) or '<expr>'}")
    return found


def _scrub_assigned_names(tree: ast.AST, seam_name: str) -> set[str]:
    """Names assigned a value that calls ``seam_name(...)`` — the
    hoisted-scrub variables (review round 1, finding 9): ``scrubbed_notes
    = scrub_session_text(...)[:cap]`` (the cap slice included, so the
    assign's value may wrap the call)."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(sub, ast.Call) and _callee_name(sub) == seam_name
            for sub in ast.walk(node.value)
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_ac_6():
    # The channels, derived by grepping the writers (review C-1/M-8/m-16):
    # the PR body (build_pr_body -> gh pr create, off the machine), the
    # vents (flush_vents -> the progress log factory status prints), the
    # trace (TraceWriter -> the file factory trace prints), the
    # unparsable-record dump (_save_session_stdout, session.py), and —
    # review round 1 — the importer's tasks.json write and refused-output
    # save, the CLI's keep path and every other module that handles
    # session-derived values (the _EGRESS_MODULES derivation above).

    # (1) every build_pr_body call scrubs BOTH session-text inputs.
    tree = ast.parse((REPO_ROOT / "factory" / "orchestrator.py").read_text())
    text_scrubbed = _scrub_assigned_names(tree, "scrub_session_text")
    value_scrubbed = _scrub_assigned_names(tree, "scrub_session_value")
    builds = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and _callee_name(n) == "build_pr_body"]
    assert builds, "the PR-body writer is gone — update this enumeration"
    for call in builds:
        notes = _kw(call, "implementer_notes")
        assert notes is not None and (
            _mentions(notes, "scrub_session_text")
            or (isinstance(notes, ast.Name) and notes.id in text_scrubbed)
        ), f"orchestrator.py:{call.lineno}: implementer notes bypass the seam"
        reports = _kw(call, "reports")
        assert reports is not None and (
            _mentions(reports, "scrub_session_value")
            or any(
                isinstance(n, ast.Name) and n.id in value_scrubbed
                for n in ast.walk(reports)
            )
        ), (
            f"orchestrator.py:{call.lineno}: the validator reports (L2 finding "
            "text, verdict JSON) bypass the seam"
        )

    # (2) the pre-push fence sees the secrets (AC-2's independent check).
    fences = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and _callee_name(n) == "check_pr_text"]
    assert fences and any(_kw(c, "secrets") is not None for c in fences), (
        "check_pr_text is called without the seat secrets"
    )

    # (3) every vent flush hands over the secrets.
    flushes = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and _callee_name(n) == "flush_vents"]
    assert flushes, "the vent flush is gone — update this enumeration"
    for call in flushes:
        assert _kw(call, "secrets") is not None, (
            f"orchestrator.py:{call.lineno}: vents flushed without secrets"
        )

    # (4) the trace writer scrubs the same literals at write.
    writers = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and _callee_name(n) == "TraceWriter"]
    assert writers, "the trace writer is gone — update this enumeration"
    for call in writers:
        assert _kw(call, "extra_secrets") is not None, (
            f"orchestrator.py:{call.lineno}: TraceWriter without extra_secrets"
        )

    # (5) session.py's dump path gets the token.
    stree = ast.parse((REPO_ROOT / "factory" / "session.py").read_text())
    dumps = [n for n in ast.walk(stree)
             if isinstance(n, ast.Call) and _callee_name(n) == "_save_session_stdout"]
    assert dumps, "the unparsable-record dump is gone — update this enumeration"
    for call in dumps:
        assert _kw(call, "secrets") is not None, (
            f"session.py:{call.lineno}: the dump is written without secrets"
        )

    # (6) The tripwire, over every module that handles session-derived
    # text (the _EGRESS_MODULES derivation above): such text may only
    # reach the seam, a guarded writer, a local-disk sink or a pure op —
    # anything else is a new egress channel, named file:line. That is how
    # a new channel cannot silently skip the seam.
    offenders = [
        offender
        for relative in _EGRESS_MODULES
        for offender in _offenders(relative)
    ]
    assert not offenders, (
        "new egress channel(s) receiving session text without the seam:\n"
        + "\n".join(offenders)
    )


# ======================= owner review round 1 (PR #98) =======================
#
# Ten findings, most severe first. Each test landed red on the round-1
# branch head (277746e) before its fix, exactly as the AC tests did.


def _import_repo(tmp_path: Path) -> Path:
    """The importer-test fixture shape: a planning doc and a minimal
    schema-valid profile (tests/test_importer_project_name.py's _repo)."""
    repo = tmp_path / "JobFinderOS"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "PLAN.md").write_text(
        "# Plan\n\n## WO-1: add search\n\nAcceptance: search finds a word.\n"
    )
    factory_dir = repo / ".factory"
    factory_dir.mkdir()
    (factory_dir / "project.yaml").write_text(
        "name: jobfinderos\n"
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
    return repo


def _wo_task(title: str, acceptance: str) -> dict:
    return {
        "id": "WO-1", "title": title, "source": "docs/PLAN.md",
        "kind": "build", "priority": "P1", "depends_on": [], "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": [{"id": "AC-1", "text": acceptance,
                        "check": {"type": "test",
                                  "ref": "tests/test_wo1.py::test_ac_1"}}],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 30},
    }


# --- review1: importer writes session text to tasks.json unscrubbed ---------


def test_review1(tmp_path, monkeypatch):
    repo = _import_repo(tmp_path)
    monkeypatch.setattr("factory.importer.seat_token", lambda: SEAT_TOKEN)
    title = (f"Add search (env {SEAT_TOKEN}; echo {ENV_ECHO_ZAI}; "
             f"clear {SCREEN_CLEAR})")
    acceptance = f"search works; env said {ENV_ECHO} and {ENV_ECHO_ZAI}"
    session_out = json.dumps(
        {"project": "jobfinderos", "tasks": [_wo_task(title, acceptance)]}
    )
    result = run_import(repo, session=lambda prompt: session_out,
                        confirm=lambda summary: True)
    assert result.applied
    queue_text = (repo / ".factory" / "tasks.json").read_text()
    for blob, label in ((queue_text, "tasks.json"), (result.summary, "the plan")):
        assert SEAT_TOKEN not in blob, f"the literal token reached {label}"
        assert ZAI_KEY not in blob, f"the Z.ai-shaped echo reached {label}"
        assert JWT not in blob, f"the JWT echo reached {label}"
        assert "\x1b" not in blob, f"control sequences reached {label}"
    # The task itself survived, redacted — not dropped or refused.
    assert "[redacted]" in queue_text
    task = json.loads(queue_text)["tasks"][0]
    assert task["id"] == "WO-1" and "Add search" in task["title"]


# --- review2: refused-import save is pattern-only, no literal scrub ---------


def test_review2(tmp_path, monkeypatch):
    repo = _import_repo(tmp_path)
    root = tmp_path / "home-factory"
    monkeypatch.setattr("factory.importer.FACTORY_ROOT_DEFAULT", root)
    monkeypatch.setattr("factory.importer.seat_token", lambda: SEAT_TOKEN)
    # A proposal naming the wrong project refuses AFTER the session ran;
    # its title carries both planted secrets (the real-session keep path).
    poisoned = _wo_task(
        f"leak {SEAT_TOKEN} echo {ENV_ECHO_ZAI}", f"env: {ENV_ECHO_ZAI}"
    )
    monkeypatch.setattr(
        "factory.importer.sdk_import_session",
        lambda prompt, repo, **kwargs: json.dumps(
            {"project": "JobFinderOS", "tasks": [poisoned]}
        ),
    )
    with pytest.raises(ImportRefused):
        run_import(repo)
    files = sorted((root / "import").glob("*.proposal.*"))
    assert files, "the refused output was not kept"
    kept = files[0].read_text()
    assert SEAT_TOKEN not in kept, "the literal token reached the saved output"
    assert ZAI_KEY not in kept, "the Z.ai-shaped echo reached the saved output"
    assert "\x1b" not in kept, "control bytes reached the saved output"


# --- review3: the tripwire sees every module; mutations still trip ----------


def _module_assignments(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """name -> every value expression the module assigns it (module-wide;
    a coarse view, but enough to refuse a name bound to a non-sequence)."""
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
    return assigned


def _is_secrets_expr_ok(value: ast.AST, assigned: dict) -> bool:
    """A ``secrets=``/``extra_secrets=`` value that is a real sequence
    shape (round 3, finding 5 — the round-2 rule accepted any expression
    with a tuple anywhere in its subtree, and any bare Name):

    * a tuple/list literal;
    * the guard's own normalization (``check_secrets(...)`` — its return
      IS a tuple by construction);
    * an IfExp whose BOTH arms are acceptable;
    * a Name the same module assigns only from acceptable expressions
      (``egress_secrets = (seat_token() or "",)``; ``secrets =
      check_secrets(secrets)`` at a guard), or a parameter pass-through
      (``secrets=secrets`` — the callee's own guarded parameter; only the
      names ``secrets``/``extra_secrets``, never a name that could be a
      bare string like ``token``).
    """
    if isinstance(value, (ast.Tuple, ast.List)):
        return True
    if isinstance(value, ast.Call) and _callee_name(value) == "check_secrets":
        return True
    if isinstance(value, ast.IfExp):
        return (_is_secrets_expr_ok(value.body, assigned)
                and _is_secrets_expr_ok(value.orelse, assigned))
    if isinstance(value, ast.Name):
        if value.id in ("secrets", "extra_secrets") and value.id not in assigned:
            return True  # parameter pass-through, guarded at its own entry
        sources = assigned.get(value.id)
        return bool(sources) and all(
            _is_secrets_expr_ok(source, assigned) for source in sources
        )
    return False


def _secrets_shape_offenders(module: str, source: str | None = None):
    """Every ``secrets=``/``extra_secrets=`` keyword and every
    ``egress_secrets =`` assignment in one factory module whose shape is
    not a sequence, as ``module:line: what``."""
    text = source if source is not None else (REPO_ROOT / module).read_text()
    tree = ast.parse(text)
    assigned = _module_assignments(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in ("secrets", "extra_secrets") and not (
                    _is_secrets_expr_ok(keyword.value, assigned)
                ):
                    found.append(
                        f"{module}:{node.lineno}: {keyword.arg}= is not a "
                        "sequence shape (a bare string scrubs nothing)"
                    )
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "egress_secrets"
                        for t in node.targets)
                and not _is_secrets_expr_ok(node.value, assigned)):
            found.append(
                f"{module}:{node.lineno}: egress_secrets = is not a "
                "sequence shape (a bare string scrubs nothing)"
            )
    return found


_FACTORY_MODULES = sorted(
    path.name for path in (REPO_ROOT / "factory").glob("*.py")
)


def test_review3():
    # (a) Channel rules: run_import scrubs the proposal BEFORE
    # plan_import, and every secrets=/extra_secrets= keyword and every
    # egress_secrets assignment in EVERY factory module is a sequence
    # shape (round 2: the CLI's bare string; round 3: the rule now covers
    # the whole package and refuses subtree-tuples and str-bound names).
    importer_src = (REPO_ROOT / "factory" / "importer.py").read_text()
    scrub_at = importer_src.index("scrub_session_value")
    plan_at = importer_src.index("plan_import(existing_doc")
    assert scrub_at < plan_at, (
        "run_import must scrub the proposal before plan_import/validation, "
        "so a redaction cannot produce a schema-valid-looking but different "
        "task without the plan diff showing it"
    )
    offenders = [
        offender
        for name in _FACTORY_MODULES
        for offender in _secrets_shape_offenders(f"factory/{name}")
    ]
    assert not offenders, "\n".join(offenders)

    # (b) Mutations: every bad shape trips the rule, naming file:line.
    cli = (REPO_ROOT / "factory" / "cli.py").read_text()
    # round 2's shape: a parenthesised string.
    round2 = _secrets_shape_offenders(
        "factory/cli.py",
        cli.replace('secrets=(seat_token() or "",),',
                    'secrets=(seat_token() or ""),'),
    )
    assert round2 and "factory/cli.py" in round2[0], round2
    # an IfExp whose true arm is a str (a tuple in the else arm used to
    # pass the round-2 subtree rule).
    ifexp = _secrets_shape_offenders(
        "factory/cli.py",
        cli.replace('secrets=(seat_token() or "",),',
                    'secrets=((seat_token() or "") if True else ()),'),
    )
    assert ifexp, "a str-valued IfExp with a tuple else-arm passed the rule"
    # a str-bound Name.
    importer = importer_src.replace(
        "secrets=egress_secrets,", "secrets=project,",
    )
    name_case = _secrets_shape_offenders("factory/importer.py", importer)
    assert name_case and "secrets= is not a sequence" in name_case[0], (
        "a str-bound name passed the rule"
    )
    # finding 6's mutation: orchestrator's assignment loses its comma.
    orch = (REPO_ROOT / "factory" / "orchestrator.py").read_text()
    comma_drop = _secrets_shape_offenders(
        "factory/orchestrator.py",
        orch.replace('egress_secrets = (seat_token() or "",)',
                     'egress_secrets = (seat_token() or "")'),
    )
    assert comma_drop and "orchestrator.py" in comma_drop[0], (
        "the orchestrator comma-drop mutation did not trip, named"
    )

    # (c) Tripwire mutations (finding 3 of round 1, unchanged): a new
    # writer in importer.py or cli.py receiving session text trips it.
    importer_doctored = importer_src + (
        "\n\ndef _evil(proposal):\n    print(proposal['title'])\n"
    )
    assert any("print" in o for o in _offenders(
        "factory/importer.py", importer_doctored
    )), "a proposal print in importer.py did not trip the tripwire"
    cli_doctored = cli + (
        "\n\ndef _evil(result):\n    print(result.raw_output)\n"
    )
    assert any("print" in o for o in _offenders(
        "factory/cli.py", cli_doctored
    )), "a raw_output print in cli.py did not trip the tripwire"


# --- review4: egress.py and vent.py are protected paths ----------------------


def test_review4():
    from factory.gates import matches

    profile = load_profile(REPO_ROOT / ".factory" / "project.yaml")
    protected = profile["paths"]["protected"]
    for path in ("factory/egress.py", "factory/vent.py"):
        assert matches(path, protected), (
            f"{path} is not covered by paths.protected: a factory-built "
            "session could weaken the egress seam unreviewed (the F-42 "
            "factory/attribution.py precedent)"
        )


# --- review5: SessionConfigError embeds raw stderr ---------------------------


def test_review5(tmp_path, monkeypatch):
    import factory.session as session_mod

    # build_settings asserts the canonical runs layout (F-28).
    run_dir = tmp_path / "factory" / "runs" / "fixture" / "r1"
    run_dir.mkdir(parents=True)
    settings = tmp_path / "settings.json"
    cli = tmp_path / "cli"
    cli.write_text("#!/bin/sh\n")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = (f"runner failed; env was {SEAT_TOKEN} {ENV_ECHO_ZAI}"
                  f"{SCREEN_CLEAR}boom\n")

    monkeypatch.setattr(session_mod, "run_sandboxed", lambda *a, **k: _Proc())
    with pytest.raises(SessionConfigError) as exc:
        session_mod._run_prepared(
            "reviewer", "do it", run_dir, {"name": "fixture"}, settings,
            max_turns=2, timeout_min=1, token=SEAT_TOKEN, cli_path=cli,
        )
    message = str(exc.value)
    assert SEAT_TOKEN not in message, "the literal token reached the error"
    assert ZAI_KEY not in message, "the Z.ai-shaped echo reached the error"
    assert "\x1b" not in message, "control sequences reached the error"
    assert "boom" in message, "the seam shredded the diagnostic text"


# --- review6: 8-bit C1 controls pass the strip -------------------------------


def test_review6():
    from factory.egress import strip_control_sequences

    for planted in ("\u009b2J done", "\u009d8;;https://evil\u009c link",
                    "\u0090p1q payload"):
        cleaned = strip_control_sequences(planted)
        for control in ("\u009b", "\u009d", "\u009c", "\u0090"):
            assert control not in cleaned, repr(planted)
    # Ordinary non-ASCII text survives: the strip removes control bytes,
    # not characters.
    kept = strip_control_sequences("å ä ö — café 🐛\u009b2J")
    assert "å ä ö — café 🐛" in kept


# --- review7: unterminated DCS swallows the rest of the text -----------------


def test_review7():
    from factory.egress import scrub_session_text, strip_control_sequences

    # An introducer with no ST: only the two-byte introducer goes; the
    # text after it stays (a hostile seat must not be able to hide later
    # findings by opening a string and never closing it).
    for introducer in ("P", "_", "^", "X"):
        cleaned = strip_control_sequences(f"a\x1b{introducer}hidden-finding-text")
        assert "hidden-finding-text" in cleaned, introducer
        assert "\x1b" not in cleaned
    # A TERMINATED DCS string is still removed whole.
    assert strip_control_sequences("a\x1bPdata\x1b\\b") == "ab"
    # And through the full seam (the strip's rewrite re-judged, nothing
    # reassembles).
    assert "hidden" in scrub_session_text("a\x1bPhidden\x1b[0m text", (SEAT_TOKEN,))


# --- review8: no shape for unquoted env echoes -------------------------------


def test_review8():
    from factory.sentinels import redact_credentials

    # The M-8 shape, Z.ai-valued: <id>.<secret>, no JWT, no quotes.
    echoes = {
        ENV_ECHO_ZAI,
        f"SERVICE_API_KEY={ZAI_KEY}",
        f"CLIENT_SECRET={'ab12cd34ef56gh78'}",
        f"DB_PASSWORD={'hunter2hunter2hunter2'}",
    }
    for echo in echoes:
        assert redact_credentials(echo) != echo, f"unredacted: {echo}"
        assert ZAI_KEY not in redact_credentials(echo) or echo != ENV_ECHO_ZAI
    # Env names outside the shape, and short values, stay untouched.
    for clean in ("API_TIMEOUT_MS=60000", "MAX_TOKENS=8",
                  'TOKEN = "read from env"', "ANTHROPIC_BASE_URL=https://api.z.ai"):
        assert redact_credentials(clean) == clean, clean

    # The sentinel flags the shape on an added line, naming it without
    # echoing the value.
    findings = [f for f in _shape_findings(f"export {ENV_ECHO_ZAI}")
                if f.id == "credential_added"]
    assert findings and "environment variable echo" in findings[0].text
    assert ZAI_KEY not in findings[0].text and SEAT_TOKEN not in findings[0].text

    # One F-33 attack diff for the shape, through the real replay.
    profile = load_profile(REPO_ROOT / ".factory" / "project.yaml")
    globs = (profile.get("paths") or {}).get("risk") or {}
    patch = REPO_ROOT / "corpus" / "attack" / "credential_added-10.patch"
    assert patch.is_file(), "missing attack diff for the env-echo shape"
    fired = [f for f in patch_findings(patch.read_text(), globs)
             if f.id == "credential_added"]
    assert fired and "environment variable echo" in fired[0].text

    # The benign corpus stays clean (0 before this shape, measured
    # 2026-11-05; the gate must hold with it).
    hits = [
        f"{p.name}: {f.id}"
        for p in sorted((REPO_ROOT / "corpus" / "benign").glob("*.patch"))
        for f in patch_findings(p.read_text(), globs)
    ]
    assert not hits, "false positives on the benign corpus:\n" + "\n".join(hits)


# --- review9: reports re-scrubbed on every tail-shrink retry -----------------


def test_review9(tmp_path, monkeypatch):
    import factory.orchestrator as orch

    # Force the shrink loop to exhaust: a >65k L2 finding text is not
    # compacted (only command outputs are), so every tail overflows and
    # the pre-fix code re-scrubbed the whole reports dict — and the notes
    # — once per iteration. The publish then refuses on size (PRBodyError,
    # the deterministic class); the scrub CALL COUNTS are what this test
    # pins, plus the normal-run body behaviour below.
    env = _Env(tmp_path)
    monkeypatch.setattr(orch, "seat_token", lambda: SEAT_TOKEN)
    counts = {"value": 0, "text": 0}
    real_value, real_text = orch.scrub_session_value, orch.scrub_session_text
    monkeypatch.setattr(
        orch, "scrub_session_value",
        lambda v, s: (counts.__setitem__("value", counts["value"] + 1)
                      or real_value(v, s)),
    )
    monkeypatch.setattr(
        orch, "scrub_session_text",
        lambda t, s: (counts.__setitem__("text", counts["text"] + 1)
                      or real_text(t, s)),
    )
    huge_review = json.dumps({
        "verdict": "pass",
        "findings": [{
            "id": "R-1", "axis": "spec", "severity": "minor", "file": "impl.py",
            "line": 1, "text": f"leak {SEAT_TOKEN}; " + "x" * 70000,
        }],
    })
    result = env.run(_Seats(huge_review))
    assert result.outcome == "needs_human", result  # body over size: refused
    # Scrubbed ONCE each, whatever the shrink loop did: one notes call,
    # one call per report key — never once per iteration.
    assert counts["text"] == 1, counts
    assert counts["value"] == 3, counts  # L0, L1, L2 — the report keys

    # And the publishable path is unchanged: the scrubbed body still
    # scrubs (test_ac_1 pins its content); this pins that the hoist did
    # not skip the seam for the fitting case either. A fresh env — the
    # first run left its task needs_human, so a re-run there would idle.
    counts["text"] = counts["value"] = 0
    env2 = _Env(tmp_path / "second")
    result = env2.run(_Seats(_review_with_finding()))
    assert result.outcome == "pr_opened", result
    assert counts["text"] == 1 and counts["value"] == 3, counts
    (pr,) = env2.prs
    assert SEAT_TOKEN not in pr.body and "[redacted]" in pr.body


# --- review10: check_pr_text re-implements scrub_literal's guard -------------


def test_review10():
    import inspect

    from factory.publish import check_pr_text
    from factory.sentinels import is_scrubbable_secret, scrub_literal

    # One rule, one home: the truth table.
    assert is_scrubbable_secret(None) is False
    assert is_scrubbable_secret("") is False
    assert is_scrubbable_secret("short") is False
    assert is_scrubbable_secret("x" * 8) is True
    # Both consumers call the helper — F-59 cannot change one rule
    # without the other following.
    assert "is_scrubbable_secret" in inspect.getsource(scrub_literal)
    assert "is_scrubbable_secret" in inspect.getsource(check_pr_text)
    # And the behaviours the shared rule produces.
    assert scrub_literal("has short", "short") == "has short"
    assert check_pr_text("t", "body with tok", secrets=("tok",)) is None


# ======================= owner review round 2 (PR #98) =======================
#
# One defect, introduced by round 1: the CLI's keep path passed
# ``secrets=(seat_token() or "")`` — a parenthesised STRING, not a tuple.
# The seam iterated it character by character; every character is shorter
# than MIN_SECRET_LEN, so the literal scrub never ran and the saved
# refused output kept the seat token in clear. The fix is two levels:
# the bug (a tuple at the call site) and the class (a bare str is refused
# at every entry that takes secrets, one shared guard).


def test_review11_cli_keep_scrubs_literal(tmp_path, monkeypatch, capsys):
    """The real CLI keep path scrubs the literal token: a real session
    (monkeypatched sdk_import_session — the keep keys on it) proposes a
    task whose title carries the seat token; the queue goes stale while
    the apply prompt waits; apply refuses and the CLI saves the session's
    raw output through keep_refused_import_output with the CLI's secrets.
    On e9d3078 the CLI passed a bare string and the token survived."""
    from factory.cli import main
    from factory.importer import run_import

    repo = _import_repo(tmp_path)
    root = tmp_path / "home-factory"
    monkeypatch.setattr("factory.importer.FACTORY_ROOT_DEFAULT", root)
    monkeypatch.setattr("factory.importer.seat_token", lambda: SEAT_TOKEN)
    # The CLI's keep path uses ITS OWN seat_token import — plant there
    # too, or the test would scrub with the real keychain item.
    monkeypatch.setattr("factory.cli.seat_token", lambda: SEAT_TOKEN)
    # A queue the plan will differ from: a second task, distinct id, with
    # the token in its title — session text via the real-session keep path
    # (the CLI keep keys on the session being real).
    first = _wo_task("Work order WO-1", "it works")
    run_import(repo, session=lambda p: json.dumps(
        {"project": "jobfinderos", "tasks": [first]}), confirm=lambda s: True)
    poisoned = dict(
        _wo_task(f"leak {SEAT_TOKEN} echo {ENV_ECHO_ZAI}", f"env {ENV_ECHO_ZAI}"),
        id="WO-2",
    )
    monkeypatch.setattr(
        "factory.importer.sdk_import_session",
        lambda prompt, repo, **kwargs: json.dumps(
            {"project": "jobfinderos", "tasks": [first, poisoned]}
        ),
    )

    def approve_meanwhile(prompt: str = "") -> str:
        from factory.state import TaskQueue

        queue = TaskQueue(repo / ".factory" / "tasks.json")
        queue.get("WO-1")["title"] = "changed while the prompt waited"
        queue.save()
        return "y"

    monkeypatch.setattr("builtins.input", approve_meanwhile)
    rc = main(["import", "--repo", str(repo)])
    assert rc == 1, capsys.readouterr().err
    files = sorted((root / "import").glob("*.proposal.*"))
    assert files, "the refused output was not kept on the CLI path"
    kept = files[0].read_text()
    assert SEAT_TOKEN not in kept, (
        "the literal seat token survived the CLI keep path — secrets was a "
        "bare string, iterated character by character"
    )
    assert ZAI_KEY not in kept and "\x1b" not in kept


def test_review12_str_secrets_refused(tmp_path):
    """The class fix: a bare str is refused at EVERY entry that takes
    secrets — iterating it character by character scrubs nothing, so the
    mistake must be loud, not silent. Tuples and lists still work."""
    from factory import egress
    from factory.importer import _abort_refusal, _save_refused_output, keep_refused_import_output
    from factory.progress import ProgressLog
    from factory.publish import check_pr_text
    from factory.session import _save_session_stdout
    from factory.trace import TraceWriter
    from factory.vent import VentCollector, clean_text, flush_vents

    log = ProgressLog(tmp_path / "log.jsonl")
    bad = SEAT_TOKEN  # a str where a sequence belongs

    entries = {
        "scrub_session_text": lambda: egress.scrub_session_text("x", bad),
        "scrub_session_value": lambda: egress.scrub_session_value("x", bad),
        "check_pr_text": lambda: check_pr_text("t", "b", secrets=bad),
        "flush_vents": lambda: flush_vents(log, [], secrets=bad),
        "clean_text": lambda: clean_text("x", bad),
        "VentCollector": lambda: VentCollector("r", "t", "s", secrets=bad),
        "_save_session_stdout": lambda: _save_session_stdout(
            "x", tmp_path, tmp_path, secrets=bad),
        "_save_refused_output": lambda: _save_refused_output(
            "x", "proj", tmp_path, secrets=bad),
        "keep_refused_import_output": lambda: keep_refused_import_output(
            ImportError("x"), text="x", project="p", repo=tmp_path,
            queue_path=tmp_path, docs_dir="docs", factory_root=tmp_path,
            secrets=bad),
        "_abort_refusal": lambda: _abort_refusal(
            "head", run=SimpleNamespace(settings_path=tmp_path),
            project="p", factory_root=tmp_path, repo=tmp_path,
            queue_path=tmp_path, docs_dir="docs", secrets=bad),
        "TraceWriter.extra_secrets": lambda: TraceWriter(
            tmp_path, "proj", "run1", "T-1", extra_secrets=bad),
    }
    for name, call in entries.items():
        with pytest.raises(TypeError, match="sequence of secrets"):
            call()
    # Sequences still work: tuple AND list scrub the literal.
    redacted = egress.scrub_session_text(f"leak {SEAT_TOKEN}", (SEAT_TOKEN,))
    assert redacted == "leak [redacted]"
    assert egress.scrub_session_text(
        f"leak {SEAT_TOKEN}", [SEAT_TOKEN]) == "leak [redacted]"
    with pytest.raises(PushRefused):
        check_pr_text("t", f"b {SEAT_TOKEN}", secrets=[SEAT_TOKEN])


# ======================= owner review round 3 (PR #98) =======================
#
# Six findings on 6b7dea8. The important one: scrub_session_value called
# check_secrets but DISCARDED its normalised tuple, so a one-shot iterable
# (a generator, an F-59 lazily-built credential view) was consumed by the
# guard and every nested scrub saw nothing — silent no-scrub again.


def test_review13(tmp_path):
    """A one-shot secrets iterable scrubs (or refuses) at every entry:
    the guard's normalised tuple is what the scrub consumes, not the
    caller's exhausted iterator."""
    from factory.egress import scrub_session_text, scrub_session_value
    from factory.importer import _save_refused_output
    from factory.progress import ProgressLog
    from factory.publish import check_pr_text
    from factory.session import _save_session_stdout
    from factory.trace import TraceWriter
    from factory.vent import VentCollector, flush_vents

    def gen():
        return (secret for secret in (SEAT_TOKEN,))

    # The recursive seam over a report-shaped value — round 3's finding:
    # the guard consumed the generator and the recursion scrubbed nothing.
    value = scrub_session_value(
        {"k": f"leak {SEAT_TOKEN}", "l": [f"leak {SEAT_TOKEN}"]}, gen()
    )
    dumped = json.dumps(value)
    assert SEAT_TOKEN not in dumped, "the generator was consumed by the guard"
    assert "[redacted]" in dumped

    assert SEAT_TOKEN not in scrub_session_text(f"leak {SEAT_TOKEN}", gen())
    with pytest.raises(PushRefused):
        check_pr_text("t", f"body {SEAT_TOKEN}", secrets=gen())

    log = ProgressLog(tmp_path / "vents.jsonl")
    flush_vents(log, [{"category": "other", "text": f"leak {SEAT_TOKEN}",
                       "at": ""}], secrets=gen())
    assert SEAT_TOKEN not in (tmp_path / "vents.jsonl").read_text()

    collector = VentCollector("r", "t", "s", secrets=gen())
    assert SEAT_TOKEN not in collector.record("other", f"leak {SEAT_TOKEN}")["text"]

    writer = TraceWriter(tmp_path, "p", "genrun", "T-1", extra_secrets=gen())
    writer.append({"type": "tool_call", "note": f"leak {SEAT_TOKEN}"})
    trace = (tmp_path / "trace" / "p" / "genrun.jsonl").read_text()
    assert SEAT_TOKEN not in trace and "[redacted]" in trace

    dump = _save_refused_output(f"leak {SEAT_TOKEN}", "p", tmp_path,
                                secrets=gen())
    assert SEAT_TOKEN not in Path(dump).read_text()
    saved = _save_session_stdout(f"leak {SEAT_TOKEN}", tmp_path, tmp_path,
                                 secrets=gen())
    assert SEAT_TOKEN not in Path(saved).read_text()


@pytest.mark.parametrize("bad", [b"x" * 16, (SEAT_TOKEN,), 16])
def test_review14(bad):
    """check_secrets refuses non-str elements (bytes, nested tuples,
    ints) naming the TYPE — never the value, which may be the secret —
    and accepts short strings and None (the `if token else ()` shapes)."""
    from factory.egress import check_secrets

    with pytest.raises(TypeError) as excinfo:
        check_secrets((bad,))
    message = str(excinfo.value)
    assert type(bad).__name__ in message, message
    assert "xxxx" not in message, "bytes value leaked into the error"
    assert SEAT_TOKEN not in message, "the nested tuple's value leaked"
    assert check_secrets(("", None, SEAT_TOKEN)) == ("", None, SEAT_TOKEN)


def test_review15(tmp_path):
    """The never-mask contract: a TypeError from the secrets guard never
    hides the original diagnostic — keep_refused_import_output chains it
    explicitly (`__cause__`, it holds the original as a parameter), and
    the save helpers raised while the original is being handled keep it
    reachable via `__context__` (documented in their docstrings)."""
    from factory.importer import _abort_refusal, keep_refused_import_output
    from factory.session import _save_session_stdout

    original = ValueError("the original diagnostic")

    try:
        raise original
    except ValueError:
        with pytest.raises(TypeError) as guard_error:
            _save_session_stdout("x", tmp_path, tmp_path, secrets="bad")
    assert guard_error.value.__context__ is original

    try:
        raise original
    except ValueError:
        with pytest.raises(TypeError) as chained:
            keep_refused_import_output(
                original, text="x", project="p", repo=tmp_path,
                queue_path=tmp_path, docs_dir="docs", factory_root=tmp_path,
                secrets="bad",
            )
    assert chained.value.__cause__ is original, (
        "the refusal being kept must stay visible under the guard's error"
    )

    try:
        raise original
    except ValueError:
        with pytest.raises(TypeError) as abort_guard:
            _abort_refusal(
                "head", run=SimpleNamespace(settings_path=tmp_path),
                project="p", factory_root=tmp_path, repo=tmp_path,
                queue_path=tmp_path, docs_dir="docs", secrets="bad",
            )
    assert abort_guard.value.__context__ is original
