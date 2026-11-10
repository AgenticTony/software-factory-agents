"""F-42 acceptance tests — session hardening: attribution and sandbox bypass.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f42.py::test_ac_N``
means a test function with exactly that name. BUILD-ORDER §F-42 AC-1..AC-6
is the contract, byte-exact. AC-6 (main's history is not rewritten) has no
test here by design: it is proven by the F-42 ledger entry listing the five
affected commits and by main being left alone — the proof is the history,
not a scripted assertion.

What is real and what is stubbed: the L0 halves run the real gate over a
real fixture git repo; the publish halves run the real seam with a fake
``gh`` binary; the seat halves build the real per-seat options and run the
real hook callbacks; the orchestrator half (AC-5) runs the real
``run_once`` pipeline — clone manager, queue, L0/L1 through the pinned srt
sandbox — with only the model seats scripted. No model spend in this file.
"""

import asyncio
import hashlib
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
from _support.f63 import ledger_approve
from claude_agent_sdk import ResultMessage

from factory.attribution import attribution_match
from factory.contract import acceptance_hash
from factory.l0 import run_l0
from factory.orchestrator import PullRequest, run_once
from factory.prompts import assemble_implementer_prompt
from factory.publish import PushRefused, gh_pr_create
from factory.session import (
    SEATS,
    SessionRecord,
    build_options,
    run_session,
    sandbox_bypass_hook,
)
from factory.state import TaskQueue
from factory.status import render_status

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
REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})

BASE_PROFILE = {
    "name": "fixture",
    "repo": {"path": ".", "default_branch": "main", "remote": "nowhere"},
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
}

# The five real trailer shapes this WO exists because of (BUILD-ORDER
# preamble: #22, #58, #61, #82, #86) plus the harness footer — every class
# the one pattern list must catch, and the human shapes it must not.
AI_CO_AUTHOR_CLAUDE = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
AI_CO_AUTHOR_CODE = "Co-authored-by: Claude Code <noreply@anthropic.com>"
AI_CO_AUTHOR_COPILOT = "Co-authored-by: Copilot <copilot@github.com>"
AI_CO_AUTHOR_FACTORY = "Co-authored-by: Software Factory <factory@localhost>"
GENERATED_WITH = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
HUMAN_CO_AUTHOR = "Co-authored-by: Jane Reviewer <jane@example.com>"
HUMAN_CO_AUTHOR_GITHUB = (
    "Co-authored-by: AgenticTony <agentictony@users.noreply.github.com>"
)


# ---------------------------------------------------------------- helpers --


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, name: str) -> Path:
    """A fixture git repo with one base commit on ``main``."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _commit(repo: Path, message: str) -> str:
    """One commit on a ``feature`` branch changing a source file."""
    if "feature" not in _git(repo, "branch", "--list", "feature"):
        _git(repo, "checkout", "-b", "feature")
    (repo / "src" / "a.py").write_text(f"x = {_git(repo, 'rev-list', '--count', 'HEAD')}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_l0(repo: Path, task=None):
    return run_l0(
        dict(BASE_PROFILE), repo, "main", "feature", task=task or {"id": "T-1", "risk": []}
    )


def _fake_gh(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A ``gh`` binary that records its argv and prints a PR url:
    ``(binary, argv log)`` — the log proves gh never ran for a refusal."""
    log = tmp_path / f"{name}-args"
    fake = tmp_path / name
    fake.write_text(
        "#!/bin/sh\n"
        f"for a in \"$@\"; do printf '%s\\n' \"$a\" >> {shlex.quote(str(log))}; done\n"
        "echo https://github.com/AgenticTony/x/pull/7\n"
    )
    fake.chmod(0o755)
    return fake, log


def _result(**overrides) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={},
        total_cost_usd=0.001,
        result="done",
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


def _pre_input(tool: str, tool_input: dict, tool_use_id: str = "u1") -> dict:
    return {
        "session_id": "s",
        "transcript_path": "t",
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    }


def _decision(callback, tool: str, tool_input: dict,
              tool_use_id: str = "u1") -> tuple[str | None, str]:
    """Run one hook callback; (permissionDecision, reason) — (None, "") = pass."""
    out = asyncio.run(
        callback(_pre_input(tool, tool_input, tool_use_id), tool_use_id, {"signal": None})
    )
    specific = out.get("hookSpecificOutput") or {}
    return specific.get("permissionDecision"), specific.get(
        "permissionDecisionReason", ""
    )


def _bypass_input(command: str = "ls -la") -> dict:
    return {"command": command, "dangerouslyDisableSandbox": True}


# ------------------------------------- AC-1: L0 fails AI attribution --------
#
# A commit on the branch whose message carries an AI attribution trailer or
# footer fails L0 with a named finding before anything is pushed; a human
# Co-Authored-By passes; the scan covers exactly the commits between the
# merge-base and head.


def test_ac_1(tmp_path):
    # The two shapes already on this repo's main (#82/#86): an AI author
    # name and the AI vendor address — one finding naming both.
    repo = _repo(tmp_path, "ac1a")
    sha = _commit(repo, f"feat: a thing\n\n{AI_CO_AUTHOR_CLAUDE}\n")
    report = _run_l0(repo)
    assert report.verdict == "fail", report.findings
    finding = next(f for f in report.findings if f.id == "attribution_trailer")
    assert sha[:12] in finding.text, finding.text
    assert "Claude Opus 5.5" in finding.text and "noreply@anthropic.com" in finding.text

    # The other AI author shapes: vendor address without the name, a
    # different vendor, and this factory's own seat identity (#58/#61).
    for trailer in (AI_CO_AUTHOR_CODE, AI_CO_AUTHOR_COPILOT, AI_CO_AUTHOR_FACTORY):
        repo = _repo(tmp_path, f"ac1-{hashlib.sha1(trailer.encode()).hexdigest()[:6]}")
        _commit(repo, f"feat: x\n\n{trailer}\n")
        findings = [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]
        assert findings, trailer
        assert trailer.split("<")[0].strip() in findings[0].text

    # The "Generated with" footer class.
    repo = _repo(tmp_path, "ac1b")
    _commit(repo, f"feat: y\n\nbody line\n\n{GENERATED_WITH}\n")
    findings = [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]
    assert findings and "Generated with" in findings[0].text

    # A human Co-Authored-By passes — including main's own #22 shape.
    repo = _repo(tmp_path, "ac1c")
    _commit(repo, f"feat: z\n\n{HUMAN_CO_AUTHOR}\n")
    assert not [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]

    repo = _repo(tmp_path, "ac1d")
    _commit(repo, f"feat: w\n\n{HUMAN_CO_AUTHOR_GITHUB}\n")
    assert not [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]

    # Scoping: a trailer BELOW the merge-base (on main itself — where five
    # already sit) never fails the branch; only the branch's own commits
    # between merge-base and head are matched.
    repo = _repo(tmp_path, "ac1e")
    (repo / "src" / "a.py").write_text("x = 99\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"base with trailer\n\n{AI_CO_AUTHOR_CLAUDE}\n")
    _commit(repo, "feat: clean\n")
    assert _run_l0(repo).verdict == "pass"

    # The one pattern list is the same one publish uses (AC-2 shares it).
    assert attribution_match(f"xx\n\n{AI_CO_AUTHOR_CLAUDE}\n") is not None
    assert attribution_match(f"title\n\n{HUMAN_CO_AUTHOR}\n") is None


# ------------------------------------- AC-2: publish refuses the PR text ----


def test_ac_2(tmp_path):
    fake, log = _fake_gh(tmp_path, "gh-ac2")
    pr = dict(remote="https://github.com/AgenticTony/x.git", base="main",
              head="factory/t-1-x")

    # Clean title and body open the PR (gh really ran).
    url = gh_pr_create(PullRequest(title="T-1: thing", body="## Summary\nbody\n", **pr),
                       gh_bin=fake)
    assert url.endswith("/pull/7")
    assert log.exists() and "--body-file" in log.read_text()

    # A title carrying the footer is refused before gh starts, naming it.
    log.unlink()
    with pytest.raises(PushRefused) as exc:
        gh_pr_create(PullRequest(title=f"T-1: thing {GENERATED_WITH}", body="b", **pr),
                     gh_bin=fake)
    assert "Generated with" in str(exc.value)
    assert not log.exists(), "gh must not run for a refused PR"

    # A body carrying an AI co-author trailer is refused, naming the match.
    with pytest.raises(PushRefused) as exc:
        gh_pr_create(
            PullRequest(title="T-1: thing",
                        body=f"## Summary\n\n{AI_CO_AUTHOR_CLAUDE}\n", **pr),
            gh_bin=fake)
    assert "Co-Authored-By" in str(exc.value) or "co_author" in str(exc.value)
    assert not log.exists()

    # A human co-author in the body is not a refusal.
    gh_pr_create(PullRequest(title="T-1: thing",
                             body=f"## Summary\n\n{HUMAN_CO_AUTHOR}\n", **pr),
                 gh_bin=fake)
    assert log.exists()


# --------------------- AC-3: the CLI's own attribution switched off --------


@pytest.mark.parametrize("seat", sorted(SEATS))
def test_ac_3(seat, tmp_path):
    options = build_options(seat, tmp_path, 10,
                            base_url="https://api.z.ai/api/anthropic", token="t")
    # The pinned CLI's own attribution switch, off: the settings schema the
    # bundled binary reads (attribution.commitTrailers / commit / pr — the
    # modern form of the deprecated includeCoAuthoredBy), delivered through
    # the SDK's --settings. commitTrailers false + empty texts = no commit
    # trailer and no PR footer from the CLI, on any seat.
    assert options.settings, "every seat starts with explicit attribution settings"
    settings = json.loads(options.settings)
    assert settings["attribution"] == {"commitTrailers": False, "commit": "", "pr": ""}
    # Isolation unchanged: no filesystem settings are loaded on top.
    assert options.setting_sources == []

    # prompts/implementer.md states the rule, and the pin is updated with
    # it (tests/test_f09.py::PROMPT_PINS freezes the file; this asserts the
    # assembled prompt the sessions actually run with).
    prompt = assemble_implementer_prompt()
    assert "attribution" in prompt.lower()
    assert "co-authored-by" in prompt.lower() or "co-authored" in prompt.lower()


# ------------------- AC-4: the sandbox-bypass deny, every seat -------------


@pytest.mark.parametrize("seat", sorted(SEATS))
def test_ac_4(seat, tmp_path):
    options = build_options(seat, tmp_path, 10,
                            base_url="https://api.z.ai/api/anthropic", token="t")
    # The deny must hold under bypassPermissions — that is the mode every
    # seat actually runs in (a deny hook still blocks there).
    assert options.permission_mode == "bypassPermissions"

    # Every Bash-matching PreToolUse hook sees the call; one of them — the
    # sandbox-bypass hook, present on EVERY seat (the fan-out seats are
    # reviewer/verifier; both are in SEATS and both build these options) —
    # denies it with a reason that names the rule.
    decisions = []
    for matcher in options.hooks["PreToolUse"]:
        if re.fullmatch(matcher.matcher, "Bash"):
            for callback in matcher.hooks:
                decisions.append(_decision(callback, "Bash", _bypass_input()))
    denials = [reason for decision, reason in decisions if decision == "deny"]
    assert any("not negotiable" in reason for reason in denials), decisions

    # Without the flag — and with it explicitly false — nothing denies.
    for tool_input in ({"command": "ls -la"}, {"command": "ls -la",
                                               "dangerouslyDisableSandbox": False}):
        for matcher in options.hooks["PreToolUse"]:
            if re.fullmatch(matcher.matcher, "Bash"):
                for callback in matcher.hooks:
                    assert _decision(callback, "Bash", tool_input)[0] is None

    # The hook feeds the shared denial state AC-5 counts on: each deny
    # records the tool_use_id and bumps the count.
    class _State:
        denied: set = set()
        count = 0

    state = _State()
    hook = sandbox_bypass_hook(state)
    decision, reason = _decision(hook.hooks[0], "Bash", _bypass_input("git status"))
    assert decision == "deny" and "not negotiable" in reason
    assert state.count == 1 and "u1" in state.denied


# ------- AC-5: denials traced, counted on the attempt record, in status ----


def test_ac_5(tmp_path):
    # (1) The denial lands in the F-30 stream: the denied call's tool_call
    # event is marked as a denied bypass, closed or still open.
    from factory.session import _tap_tool_calls

    class State:
        def __init__(self):
            self.denied: set[str] = set()
            self.count = 0

    state = State()
    hook = sandbox_bypass_hook(state)
    asyncio.run(hook.hooks[0](_pre_input("Bash", _bypass_input(), "u9"), "u9", {}))
    assert state.count == 1 and "u9" in state.denied

    events: list[dict] = []
    pending: dict = {}
    use_block = SimpleNamespace(name="Bash", id="u9", input=_bypass_input())
    result_block = SimpleNamespace(tool_use_id="u9", is_error=True,
                                   content="denied: the sandbox is not negotiable")
    _tap_tool_calls(SimpleNamespace(content=[use_block]), pending, "implementer",
                    None, events.append, denied=state.denied)
    _tap_tool_calls(SimpleNamespace(content=[result_block]), pending, "implementer",
                    None, events.append, denied=state.denied)
    assert len(events) == 1 and events[0]["tool"] == "Bash"
    assert events[0].get("denied") == "sandbox_bypass"
    assert events[0]["ok"] is False
    assert not state.denied, "a traced denial leaves the pending set"

    # (2) The count rides the session record out of run_session: the hook
    # (the real one build_options wired) fires in-process, as the SDK
    # would, and the completed record carries the denial count.
    async def query(*, prompt, options, transport=None):
        for matcher in options.hooks["PreToolUse"]:
            if re.fullmatch(matcher.matcher, "Bash"):
                for callback in matcher.hooks:
                    await callback(_pre_input("Bash", _bypass_input(), "q1"), "q1", {})
        yield _result()

    record = asyncio.run(run_session(
        "implementer", "do it", tmp_path, 5, 10, query_fn=query,
        base_url="https://api.z.ai/api/anthropic", token="t"))
    assert record.status == "completed"
    assert record.sandbox_bypass_denials == 1

    # (3) The attempt record counts it, the trace's session row carries it,
    # and factory status shows the week's total.
    env = _Env(tmp_path)
    result = env.run(_Seats())
    assert result.outcome == "pr_opened", result
    task = env.task()
    attempt = task["attempts"][0]
    assert attempt["sandbox_bypass_denials"] == 2

    trace = env.root / "trace" / "fixture" / f"{attempt['run_id']}.jsonl"
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    session_rows = [row for row in rows if row.get("type") == "session_end"
                    and row.get("seat") == "implementer"]
    assert session_rows and session_rows[0]["sandbox_bypass_denials"] == 2

    page = render_status(TaskQueue(env.queue_path).doc, project="fixture",
                         factory_root=env.root, now=datetime.now(STOCKHOLM))
    assert re.search(r"2 Bash call\(s\) setting dangerouslyDisableSandbox", page), page
    assert "Sandbox bypass attempts this week" in page


class _Env:
    """The F-10 harness shape, trimmed to one passing run: a fixture repo
    with a bare remote, a queue, and the real run_once pipeline."""

    def __init__(self, tmp_path: Path, *, title: str = "Make answer() return 42",
                 seam_check=None):
        self.seam_check = seam_check
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
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
        profile = {
            "name": "fixture",
            "repo": {"path": str(self.repo), "default_branch": "main",
                     "remote": str(self.remote), "merge_method": "squash"},
            "commands": {"setup": "true",
                         "test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"},
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
                       "wall_clock_min": 30},
            "attempts": [], "links": {},
        }
        self.queue_path.write_text(
            json.dumps({"version": 1, "project": "fixture", "tasks": [task]}, indent=2)
            + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)],
                       check=True)
        self.prs: list[PullRequest] = []

    def open_pr(self, pr: PullRequest) -> str:
        # The real seam's refusal rule, where the run under test wants it
        # exercised (review finding 2): raise before recording on a hit.
        if self.seam_check is not None:
            self.seam_check(pr.title, pr.body)
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def remote_branches(self) -> set[str]:
        out = _git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads")
        return set(out.split())

    def task(self) -> dict:
        return TaskQueue(self.queue_path).get("T-1")

    def run(self, seats):
        from factory.mergeorder import Protection

        # F-63: record the fixture's owner approvals before the run.
        ledger_approve(self.root, self.queue_path)
        return run_once(
            self.queue_path, factory_root=self.root, session_runner=seats,
            open_pr=self.open_pr, now=lambda: OFF_PEAK,
            protection=lambda remote, branch: Protection(checked=True, strict=True),
            pr_state=lambda url: "open",
        )


class _Seats:
    """Stub seats: a correct implementer whose session denied two bypass
    attempts, and a passing review fan-out."""

    def __init__(self, review: str = REVIEW_PASS):
        self.review = review
        self.lock = __import__("threading").Lock()
        self.calls: list[str] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        from pathlib import Path as _Path

        with self.lock:
            self.calls.append(seat)
        run_dir = _Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests").mkdir(exist_ok=True)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                result="Implemented answer().", sandbox_bypass_denials=2,
            )
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10}, result=self.review,
        )


# ---------------------- review round (PR #89, four findings) -----------------
#
# Red-first on the pre-fix tree (head bb87c84): each test failed on the
# behaviour its finding names — the probes under review1/3/4 hit (or
# passed) the wrong way through attribution_match, and review2 failed on
# the missing check_pr_text surface with the branch already pushed.


def test_review1_generated_with_footer_is_anchored_to_the_ai_shape(tmp_path):
    """Finding 1: 'generated with <ordinary tool>' prose is not the AI
    footer. Only a line-shaped footer naming an AI tool (or an AI vendor
    link) fails; the review's probed lines pass."""
    ordinary = [
        "docs: client SDK generated with openapi-generator",
        "Fixture data generated with faker",
        "report generated with ruff",
        "generated with openapi-typescript",
        "The snapshot fixtures were generated with faker v2.3",
    ]
    for line in ordinary:
        assert attribution_match(line) is None, line
    footers = [
        GENERATED_WITH,  # 🤖 Generated with [Claude Code](https://claude.com/claude-code)
        "Generated with Claude Code",
        "Generated with GitHub Copilot",
        "- Generated with [Claude Code](https://claude.com/claude-code)",
        "Generated with https://claude.com/claude-code",
    ]
    for line in footers:
        assert attribution_match(line) == ("generated_with_footer", line.strip()), line
    # The gate agrees: the ordinary prose as a real commit subject passes
    # L0; the real footer in a commit body fails it.
    repo = _repo(tmp_path, "rev1a")
    _commit(repo, "docs: client SDK generated with openapi-generator\n")
    assert not [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]
    repo = _repo(tmp_path, "rev1b")
    _commit(repo, f"feat: x\n\n{GENERATED_WITH}\n")
    assert [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]


def test_review2_pr_text_refusal_precedes_the_push_and_skips_fences(tmp_path):
    """Finding 2: the PR-text refusal fires BEFORE push_branch (no orphan
    remote branch, publish_refused — not pr_create_failed with one), and
    fenced blocks, which legitimately quote a trailer, are not scanned."""
    from factory.publish import check_pr_text

    fenced_body = (
        "## Summary\n\nsome words\n\n```json\n"
        '{"findings": [{"text": "quotes '
        "Co-Authored-By: Claude <noreply@anthropic.com> here"
        '"}]}\n```\n'
    )
    # The seam rule: a fenced quote is evidence, not attribution; the
    # title and the body's free text are the PR's own words.
    check_pr_text("T-1: fine", fenced_body)
    with pytest.raises(PushRefused):
        check_pr_text("T-1: fine", f"## Summary\n\n{AI_CO_AUTHOR_CLAUDE}\n")
    with pytest.raises(PushRefused):
        check_pr_text(f"T-1: add {AI_CO_AUTHOR_CLAUDE} support", fenced_body)

    # The gh wrapper keeps the same rule as its backstop (AC-2's seam).
    fake, log = _fake_gh(tmp_path, "rev2-gh")
    pr = dict(remote="https://github.com/AgenticTony/x.git", base="main",
              head="factory/t-1-x")
    from factory.publish import gh_pr_create as _create
    assert _create(PullRequest(title="T-1: thing", body=fenced_body, **pr),
                   gh_bin=fake).endswith("/pull/7")
    log.unlink()
    with pytest.raises(PushRefused):
        _create(PullRequest(title="T-1: thing",
                            body=f"## Summary\n\n{AI_CO_AUTHOR_CLAUDE}\n", **pr),
                gh_bin=fake)
    assert not log.exists()

    # Orchestrator: a reviewer finding quoting the trailer inside the
    # fenced validator report still publishes.
    quoting_review = json.dumps({"verdict": "pass", "findings": [
        {"id": "R-1", "axis": "spec", "severity": "minor", "file": "impl.py",
         "line": 1,
         "text": f"the branch commit carries '{AI_CO_AUTHOR_CLAUDE}' — strip it"},
    ]})
    env = _Env(tmp_path / "a", seam_check=lambda t, b: check_pr_text(t, b))
    result = env.run(_Seats(review=quoting_review))
    assert result.outcome == "pr_opened", result
    assert AI_CO_AUTHOR_CLAUDE in env.prs[0].body  # the quote rode the fence
    assert f"refs/heads/{env.prs[0].head}" in env.remote_branches()

    # Orchestrator: a trailer in the PR title is refused BEFORE the push —
    # nothing reaches the remote, no PR opens, publish_refused (not
    # pr_create_failed with a stranded branch).
    env2 = _Env(tmp_path / "b", title=f"Add {AI_CO_AUTHOR_CLAUDE} support",
                seam_check=lambda t, b: check_pr_text(t, b))
    result2 = env2.run(_Seats())
    assert result2.outcome == "needs_human", result2
    assert result2.escalation == "publish_refused"
    assert env2.prs == []
    assert env2.remote_branches() == {"refs/heads/main"}


def test_review3_ambiguous_ai_names_need_an_ai_shape(tmp_path):
    """Finding 3: names that are also human names (Kimi, Gemini, Cursor,
    Mistral, Llama, Qwen, GLM) flag only with an AI shape beside them —
    a bot/noreply address, an AI vendor address, or the product's
    compound name. A human colleague passes; unambiguous product names
    still match alone."""
    humans = [
        "Co-authored-by: Kimi Larsson <kimi@acme.se>",
        "Co-authored-by: Gemini Larsson <gemini@acme.se>",
        "Co-authored-by: Mistral Ek <mistral@acme.se>",
        "Co-authored-by: Cursor Redlund <cursor@acme.se>",
        "Co-authored-by: Llama Nurk <llama@acme.se>",
        "Co-authored-by: Qwen Zhang <qwen@acme.se>",
    ]
    for line in humans:
        assert attribution_match(line) is None, line
    bots = [
        "Co-authored-by: Kimi <bot@moonshot.ai>",
        "Co-authored-by: Kimi <kimi@moonshot.ai>",
        "Co-authored-by: Cursor Agent <dev@acme.se>",
        "Co-authored-by: Gemini Code Assist <dev@acme.se>",
        "Co-authored-by: GLM <noreply@zhipu.ai>",
        "Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>",
    ]
    for line in bots:
        assert attribution_match(line) is not None, line
    # The gate agrees on a real commit either way.
    repo = _repo(tmp_path, "rev3a")
    _commit(repo, "feat: x\n\nCo-authored-by: Kimi Larsson <kimi@acme.se>\n")
    assert _run_l0(repo).verdict == "pass"
    repo = _repo(tmp_path, "rev3b")
    _commit(repo, "feat: x\n\nCo-authored-by: Kimi <bot@moonshot.ai>\n")
    assert [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]


def test_review4_other_ai_trailer_styles(tmp_path):
    """Finding 4: Assisted-by, Generated-by and AI-assisted trailers follow
    the same AI-name/vendor-address rules as Co-Authored-By; the bare
    AI-assisted declaration fails on its own."""
    fails = [
        "Assisted-by: Claude <noreply@anthropic.com>",
        "Generated-by: GitHub Copilot <copilot@github.com>",
        "Assisted-by: Kimi <bot@moonshot.ai>",
        "AI-assisted: yes",
    ]
    for line in fails:
        assert attribution_match(line) is not None, line
    passes = [
        "Assisted-by: Jane Doe <jane@acme.se>",
        "Assisted-by: Kimi Larsson <kimi@acme.se>",
        "Generated-by: openapi-generator",
    ]
    for line in passes:
        assert attribution_match(line) is None, line
    repo = _repo(tmp_path, "rev4")
    _commit(repo, "feat: x\n\nAssisted-by: Claude <noreply@anthropic.com>\n")
    findings = [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]
    assert findings and "Assisted-by" in findings[0].text


def test_review5_mid_line_footer_and_suffixed_model_names(tmp_path):
    """Second review of PR #89: a ``Generated with <AI tool>`` phrase
    mid-line in a commit body is still the attribution (the AI-tool
    requirement, not a line anchor, is what keeps ordinary prose out);
    model names with a suffix (``gpt-4o``) and Cursor's current domain
    (``cursor.com``) are AI authors."""
    hits = [
        ("fix: x\n\nThis change was generated with Claude Code.", "generated_with_footer"),
        ("fix: x\n\nCode generated with Claude's help", "generated_with_footer"),
        ("Co-authored-by: aider (gpt-4o) <noreply@aider.chat>", "co_author_ai_author"),
        ("Co-authored-by: GPT-4o-mini <bot@example.com>", "co_author_ai_author"),
        ("Co-authored-by: cursoragent <cursoragent@cursor.com>", "co_author_ai_vendor_address"),
    ]
    for text, name in hits:
        match = attribution_match(text)
        assert match is not None and match[0] == name, text
    # Ordinary prose stays clean whatever its position in the line.
    for text in [
        "fix: x\n\nThe client SDK was generated with openapi-generator.",
        "fix: x\n\nValues generated with gpt2-tokenizer",
        "Co-authored-by: Kimi Larsson <12345+kimi@users.noreply.github.com>",
    ]:
        assert attribution_match(text) is None, text
    # The gate agrees on the mid-line footer.
    repo = _repo(tmp_path, "rev5")
    _commit(repo, "feat: x\n\nThis change was generated with Claude Code.\n")
    assert [f for f in _run_l0(repo).findings if f.id == "attribution_trailer"]
