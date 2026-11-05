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
from zoneinfo import ZoneInfo

import pytest

from factory.contract import acceptance_hash, load_profile
from factory.gates import FileChange
from factory.orchestrator import PullRequest, run_once
from factory.progress import ProgressLog
from factory.publish import PushRefused, check_pr_text
from factory.replay import patch_findings
from factory.session import SessionRecord, _save_session_stdout
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
# proved. The echo (M-8) is a bare ANTHROPIC_AUTH_TOKEN=<jwt> assignment,
# JWT-shaped because that is what the seat env actually holds.
SEAT_TOKEN = "seat-tok-0f1e2d3c4b5a6978879601525354555657585960"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
ENV_ECHO = f"ANTHROPIC_AUTH_TOKEN={JWT}"

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
                      f"and the runner echoes {ENV_ECHO}.")
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
            "text": f"the env leaks {SEAT_TOKEN}; runner said {ENV_ECHO}",
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
    # echoed env value (the JWT the echo carries).
    assert SEAT_TOKEN not in body, "the literal seat token reached the PR body"
    assert JWT not in body, "the echoed ANTHROPIC_AUTH_TOKEN value reached the body"
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
    planted = (f"the runner env is {SEAT_TOKEN} / {ENV_ECHO} "
               f"and it draws {SCREEN_CLEAR}{OSC8_LINK}")
    flush_vents(log, [{"category": "environment", "text": planted, "at": "",
                       "run_id": "r", "task_id": "T-1", "seat": "implementer",
                       "turn": 1}],
                run_id="r", task_id="T-1", seat="implementer",
                secrets=(SEAT_TOKEN,))
    stored = (task_dir / "T-1.jsonl").read_text()
    assert SEAT_TOKEN not in stored, "the literal seat token reached the vent log"
    assert JWT not in stored, "the echoed token value reached the vent log"
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
        f"line one\n{SEAT_TOKEN}\n{ENV_ECHO}\n{CURSOR_MOVE}rewritten\n",
        run_dir, tmp_path, secrets=(SEAT_TOKEN,),
    )
    dumped = Path(dump).read_text()
    assert SEAT_TOKEN not in dumped, "the literal token reached the dump"
    assert JWT not in dumped, "the echoed token value reached the dump"
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


def test_ac_6():
    tree = ast.parse((REPO_ROOT / "factory" / "orchestrator.py").read_text())

    # The channels, derived by grepping the writers (review C-1/M-8/m-16):
    # the PR body (build_pr_body -> gh pr create, off the machine), the
    # vents (flush_vents -> the progress log factory status prints), the
    # trace (TraceWriter -> the file factory trace prints) and the
    # unparsable-record dump (_save_session_stdout, session.py).

    # (1) every build_pr_body call scrubs BOTH session-text inputs.
    builds = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and _callee_name(n) == "build_pr_body"]
    assert builds, "the PR-body writer is gone — update this enumeration"
    for call in builds:
        notes = _kw(call, "implementer_notes")
        assert notes is not None and _mentions(notes, "scrub_session_text"), (
            f"orchestrator.py:{call.lineno}: implementer notes bypass the seam"
        )
        reports = _kw(call, "reports")
        assert reports is not None and _mentions(reports, "scrub_session_value"), (
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

    # (6) The tripwire: session-derived text (a session record's result,
    # reason, vents, or the parsed findings) may only reach the seam, the
    # guarded writers above, a local-disk sink, a prompt builder (ingress
    # INTO the next session — the review's own function, not egress), or
    # a pure expression op. Any OTHER call that receives it is a new
    # egress channel, and this fails naming it — that is how a new
    # channel cannot silently skip the seam.
    seam_routed = {
        "scrub_session_text", "scrub_session_value",  # the seam itself
        "build_pr_body",   # (1) proves its inputs are seam-scrubbed
        "flush_vents",     # (3) proves the secrets are handed over
    }
    local_or_ingress = {
        # Local-disk records under the factory root, never off-machine and
        # never printed to the terminal: the attempt record / queue, the
        # numbered findings in the progress log (finish), the stored
        # reports, the trace writer (its _clean scrubs literals+shapes at
        # write) and the in-memory result objects the CLI summarizes.
        "attempt_record", "append_attempt", "append", "_numbered_findings",
        "_store", "gate_checks", "session_end", "run_end", "phase",
        "_log_vents", "_is_quota", "_tokens", "_turns_spent", "finish",
        "RunResult", "LoopResult", "transition", "render",
        # Verdict plumbing (pure merges/parses over finding objects).
        "apply_vote", "apply_convergence", "final_review_verdict",
        "parse_reviews_with_retry", "parse_verifier_vote", "_review_drops",
        # Prompt builders: session text riding INTO a session's prompt is
        # the review flow itself (ingress), not text leaving the machine.
        "_implementer_prompt", "_reviewer_prompt", "_verifier_prompt",
        "dumps",
    }
    pure_ops = {
        # Expression plumbing that cannot write anywhere by itself.
        "bool", "str", "len", "sorted", "list", "dict", "set", "tuple",
        "next", "any", "all", "sum", "min", "max", "enumerate", "isinstance",
        "getattr", "join", "search", "match", "fullmatch", "get", "items",
        "keys", "values", "strip", "lower", "upper", "split",
    }
    allowed = seam_routed | local_or_ingress | pure_ops
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _mentions(node, "result", "reason", "vents", "findings"):
            continue
        name = _callee_name(node)
        if name not in allowed:
            offenders.append(f"orchestrator.py:{node.lineno}: {name or '<expr>'}")
    assert not offenders, (
        "new egress channel(s) receiving session text without the seam:\n"
        + "\n".join(offenders)
    )
