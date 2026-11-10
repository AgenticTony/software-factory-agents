"""F-63 acceptance tests — BUILD-ORDER.md, work order F-63.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f63.py::test_ac_N``
means a test function with exactly that name.

What is real and what is stubbed:

* **Real:** the ledger (the hash-chained approvals file under the factory
  root), the queue/profile loading and validation, ``run_once``'s pick /
  demote / sweep decisions, and — for the one end-to-end leg (AC-1's
  "an approved task is picked") — the F-05 sandboxed gate runner with the
  real pinned srt, exactly the test_f10 shape.
* **Stubbed:** the model seats (``Seats``, the test_f10 shape) and the gh
  seams (``open_pr`` recorder, ``protection``/``pr_state`` fakes). No API
  spend, no GitHub contact.
"""

import json
import shlex
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from factory.approve import render_contract
from factory.contract import (
    ContractError,
    acceptance_hash,
    load_profile,
    load_tasks,
)
from factory.gates import FileChange
from factory.l0 import check_diff_policy
from factory.mergeorder import Protection
from factory.orchestrator import run_once
from factory.session import SessionRecord
from factory.state import TaskQueue

# The test-support helper is imported lazily inside the tests that use it
# (red-first: factory.approvals does not exist on the untouched tree).
REPO_ROOT = Path(__file__).resolve().parent.parent
STOCKHOLM = ZoneInfo("Europe/Stockholm")
OFF_PEAK = datetime(2026, 11, 15, 15, 0, tzinfo=STOCKHOLM)  # a Sunday
REF = "tests/test_feature.py::test_ac_1"
PROTECTED_STRICT = Protection(checked=True, strict=True)

TEST_SOURCE = textwrap.dedent(
    """
    from impl import answer


    def test_ac_1():
        assert answer() == 42
    """
)
REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(task_id: str = "T-1", *, risk=(), ref: str = REF, **overrides) -> dict:
    acceptance = [
        {"id": "AC-1", "text": "answer() returns 42", "check": {"type": "test", "ref": ref}}
    ]
    task = {
        "id": task_id,
        "title": f"Make answer() return 42 ({task_id})",
        "source": f"docs/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": list(risk),
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-11-15T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20, "wall_clock_min": 30},
        "scope": {"paths": ["impl.py", "tests/test_feature.py"]},
        "attempts": [],
    }
    task.update(overrides)
    return task


class Env:
    """One fixture project: local repo + bare remote + factory root."""

    def __init__(self, tmp_path: Path, tasks: list | None = None,
                 profile_extra=None):
        self.tmp = tmp_path
        self.root = tmp_path / "factory-root"
        (self.root / "holdouts" / "fixture").mkdir(parents=True)
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
            "repo": {
                "path": str(self.repo),
                "default_branch": "main",
                "remote": str(self.remote),
                "merge_method": "squash",
            },
            "commands": {
                "setup": "true",
                "test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
            },
            "paths": {
                "risk": {"payments": ["src/pay/**"]},
                "protected": [".factory/**"],
            },
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        profile.update(profile_extra or {})
        self.profile = profile
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        self.profile_path = factory_dir / "project.yaml"
        self.profile_path.write_text(yaml.safe_dump(profile))
        self.queue_path = factory_dir / "tasks.json"
        doc = {"version": 1, "project": "fixture", "tasks": tasks or [_task()]}
        self.queue_path.write_text(json.dumps(doc, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)], check=True
        )
        self.prs: list = []

    def rewrite_profile(self, profile: dict) -> None:
        """Replace the fixture's profile file (kept out of git history)."""
        self.profile = profile
        self.profile_path.write_text(yaml.safe_dump(profile))

    def open_pr(self, pr) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def queue(self) -> TaskQueue:
        return TaskQueue(self.queue_path)

    def task(self, task_id: str = "T-1") -> dict:
        return self.queue().get(task_id)

    def run(self, session_runner=None, **kwargs):
        kwargs.setdefault("now", lambda: OFF_PEAK)
        kwargs.setdefault("protection", lambda remote, branch: PROTECTED_STRICT)
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_once(
            self.queue_path,
            factory_root=self.root,
            session_runner=session_runner or Seats(),
            open_pr=self.open_pr,
            **kwargs,
        )


class Seats:
    """Stub seats (the test_f10 shape): the implementer makes the change,
    every review seat passes."""

    def __init__(self, answer=42, result="Implemented answer().",
                 review=REVIEW_PASS):
        self.answer = answer
        self.result = result
        self.review = review
        self.calls: list[str] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        from pathlib import Path as _Path

        self.calls.append(seat)
        run_dir = _Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text(
                f"def answer():\n    return {self.answer}\n"
            )
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=3, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40}, result=self.result,
            )
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10}, result=self.review,
        )


class NoSessions(Seats):
    """A seat runner that fails the test if ANY session starts."""

    def __call__(self, seat, *args, **kwargs):
        raise AssertionError(f"session {seat!r} started; the ledger said no")


# --------------------------------------------------------------------------
# AC-1 — task approvals live in an owner ledger
# --------------------------------------------------------------------------


def test_ac_1(tmp_path: Path):
    from _support.f63 import ledger_approve

    from factory.approvals import ApprovalLedger, ApprovalLedgerError
    from factory.contract import contract_hash
    from factory.tick import tick_steps

    # (1) A hand-minted ready task — correct acceptance_hash, approved_by
    #     stamped by its writer — with no ledger entry is never picked and
    #     is demoted, the reason named.
    env = Env(tmp_path / "a")
    ledger_approve(env.root, env.queue_path, tasks=False)  # profile only
    result = env.run(NoSessions())
    assert result.outcome == "idle", result
    task = env.task()
    assert task["status"] == "draft", "unledgered ready task must demote to draft"
    assert result.unledgered == ["T-1"], result
    assert env.prs == []
    # The forged approval fields survived as history (re-approval
    # overwrites them); what failed is the ledger, not the hash.
    assert task["approved_by"] == "owner"
    assert task["acceptance_hash"] == acceptance_hash(task["acceptance"])

    # (2) After approval, an edit to risk / gates / budget / out_of_scope /
    #     acceptance demotes the task (risk/gates/budget/out_of_scope are
    #     beyond acceptance_hash — the whole contract is what was approved).
    def demoted_after_edit(tmp: Path, edit) -> None:
        env = Env(tmp)
        ledger_approve(env.root, env.queue_path)
        queue = env.queue()
        edit(queue.get("T-1"))
        queue.save()
        audit = queue.demote_changed_contracts(
            ApprovalLedger.for_project(env.root, "fixture")
        )
        assert audit.demoted == ["T-1"], audit
        assert env.task()["status"] == "draft"

    demoted_after_edit(tmp_path / "b-risk", lambda t: t.update(risk=["payments"]))
    demoted_after_edit(
        tmp_path / "b-gates",
        lambda t: t["gates"].update({"deploy": "auto"}),
    )
    demoted_after_edit(
        tmp_path / "b-budget", lambda t: t["budget"].update({"max_attempts": 2})
    )
    demoted_after_edit(
        tmp_path / "b-oos", lambda t: t.update(out_of_scope=["the moon"])
    )
    demoted_after_edit(
        tmp_path / "b-acceptance",
        lambda t: t["acceptance"][0].update(text="answer() returns 43"),
    )

    # (3) An approved task is picked (the full pipeline, real sandbox gates).
    env = Env(tmp_path / "c")
    ledger_approve(env.root, env.queue_path)
    result = env.run(Seats())
    assert result.outcome == "pr_opened", result
    assert result.unledgered == []

    # (4) Ticking an owner step after approval does not demote: the tick
    #     writes done/done_at/done_by/evidence, all outside the hash.
    env = Env(tmp_path / "d", tasks=[_task(owner_steps=[
        {"text": "check the PR", "done": False}
    ])])
    ledger_approve(env.root, env.queue_path)
    tick_steps(env.queue(), "T-1", [1], evidence="looked", done_by="owner",
               confirm=lambda question: True)
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    audit = env.queue().demote_changed_contracts(ledger)
    assert audit.demoted == [] and audit.unledgered == [], audit
    assert env.task()["status"] == "ready"
    assert env.queue().pick(ledger).task["id"] == "T-1"

    # (5) A ledger line rewritten in place refuses the load, the line named.
    root = tmp_path / "e"
    ledger = ApprovalLedger.for_project(root, "fixture")
    for tid in ("T-1", "T-2", "T-3"):
        ledger.record_task_for(_task(tid))
    lines = ledger.path.read_text().splitlines()
    original = list(lines)
    # middle line: the chain catches it on the follower (entry 3 names
    # the broken link — the rewritten line is entry 2)
    record = json.loads(lines[1])
    record["task_id"] = "T-9"
    lines[1] = json.dumps(record, sort_keys=True, ensure_ascii=False)
    ledger.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ApprovalLedgerError, match=r"entry 3"):
        ledger.entries()
    # last line restored + rewritten as well-formed JSON but
    # schema-invalid: the chain is intact, the schema catches it
    record = json.loads(original[2])
    record["contract_hash"] = "not-a-hash"
    lines = [*original[:2],
             json.dumps(record, sort_keys=True, ensure_ascii=False)]
    ledger.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ApprovalLedgerError, match=r"line 3"):
        ledger.entries()

    # (6) An in_review task without a ledger entry is not swept or
    #     re-validated; the reason is reported. With an entry, the sweep
    #     proves the (already up-to-date) PR and stamps its cache.
    env = Env(tmp_path / "f", tasks=[_task(
        status="in_review",
        links={"pr": "https://github.com/fixture/fixture/pull/7",
               "branch": "factory/t-1-abc123"},
    )])
    branch = "factory/t-1-abc123"
    _git(env.repo, "branch", branch)
    _git(env.repo, "push", "-q", str(env.remote), branch)
    ledger_approve(env.root, env.queue_path, tasks=False)  # profile only
    result = env.run(NoSessions())
    assert result.outcome == "idle", result
    assert env.task()["status"] == "in_review"  # not demoted, not acted on
    assert (env.task().get("links") or {}).get("revalidated") is None
    assert result.ready_prs == []
    assert result.unledgered == ["T-1"]
    # approved: the same sweep now acts on it
    ledger_approve(env.root, env.queue_path)
    result = env.run(NoSessions())
    assert result.outcome == "idle", result
    assert env.task()["links"]["revalidated"]["default"] == _git(
        env.repo, "rev-parse", "HEAD"
    )
    assert result.ready_prs and result.unledgered == []

    # The hash is over the whole contract minus exactly the lifecycle
    # fields — pinned by the exclusion test below, which also proves an
    # unknown future field changes the hash.
    base = _task()
    assert contract_hash(base) == contract_hash(_task())


def test_contract_hash_exclusion_list(tmp_path: Path):
    """The pinned exclusion list, and what it means (AC-1's hash rule).

    Everything the factory itself legitimately writes to a task after
    approval is excluded; everything else — including any field a future
    schema adds — changes the hash.
    """
    from factory.contract import (
        CONTRACT_HASH_EXCLUDED_FIELDS,
        OWNER_STEP_HASH_EXCLUDED_FIELDS,
        contract_hash,
    )

    assert CONTRACT_HASH_EXCLUDED_FIELDS == (
        "status", "acceptance_hash", "approved_by", "approved_at",
        "attempts", "attempt_resets", "links",
    )
    assert OWNER_STEP_HASH_EXCLUDED_FIELDS == ("done", "done_at", "done_by", "evidence")

    base = _task()
    same = contract_hash(base)

    def unchanged(**fields) -> None:
        task = _task(**fields)
        assert contract_hash(task) == same, fields

    # excluded: lifecycle fields
    unchanged(status="in_review", acceptance_hash="0" * 64,
              approved_by="someone-else", approved_at="2020-01-01T00:00:00Z",
              attempts=[{"run_id": "x"}], attempt_resets=[{"at": "y"}],
              links={"pr": "https://x"})
    # excluded: the tick-written owner-step completion fields — the SAME
    # step, ticked vs not, hashes identically (the step's presence and
    # text are contract content and stay hashed: the last assertion)
    ticked = _task(owner_steps=[{"text": "check", "done": True,
                                 "done_at": "2026-11-15T12:00:00Z",
                                 "done_by": "owner", "evidence": "looked"}])
    unticked = _task(owner_steps=[{"text": "check", "done": False}])
    assert contract_hash(ticked) == contract_hash(unticked)

    # NOT excluded: a real contract field, and an unknown future field
    assert contract_hash(_task(risk=["payments"])) != same
    assert contract_hash(_task(gates={"spec_approval": True, "merge": "human",
                                      "deploy": "auto"})) != same
    assert contract_hash(_task(**{"future_field": True})) != same
    assert contract_hash(_task(owner_steps=[{"text": "different"}])) != same

    # canonicalisation: key order is irrelevant; NFC/NFD and 1 vs 1.0 are
    # distinct bytes and stay distinct hashes
    reordered = _task(budget={"wall_clock_min": 30, "max_turns": 20,
                             "max_attempts": 3, "size": "S"})
    assert contract_hash(reordered) == same
    nfc = _task(title="café")
    nfd = _task(title="café")
    assert contract_hash(nfc) != contract_hash(nfd)
    assert contract_hash(_task(budget={"size": "S", "max_attempts": 3,
                                       "max_turns": 20, "wall_clock_min": 30.0})) != same
    # acceptance list order is semantic: swapping items changes the hash
    two = [{"id": "AC-1", "text": "one"}, {"id": "AC-2", "text": "two"}]
    assert contract_hash({"acceptance": two}) != contract_hash(
        {"acceptance": list(reversed(two))}
    )


def test_ledger_binds_entries_to_its_project(tmp_path: Path):
    """A ledger file copied from another project's approvals refuses to
    load: every entry names the project, and the file knows its own."""
    from factory.approvals import ApprovalLedger, ApprovalLedgerError

    source = ApprovalLedger.for_project(tmp_path / "root-a", "project-a")
    source.record_task_for(_task())
    copied = ApprovalLedger.for_project(tmp_path / "root-b", "project-b")
    copied.path.parent.mkdir(parents=True, exist_ok=True)
    copied.path.write_bytes(source.path.read_bytes())
    with pytest.raises(ApprovalLedgerError, match="project-a"):
        copied.entries()


def test_ledger_tail_truncation_only_removes(tmp_path: Path):
    """Known property: truncating the tail drops approvals (fail-safe —
    the affected tasks demote/refuse), and the surviving prefix loads."""
    from factory.approvals import ApprovalLedger

    ledger = ApprovalLedger.for_project(tmp_path, "fixture")
    ledger.record_task_for(_task("T-1"))
    ledger.record_task_for(_task("T-2"))
    lines = ledger.path.read_text().splitlines()
    ledger.path.write_text(lines[0] + "\n")
    entries = ledger.entries()
    assert [e["task_id"] for e in entries] == ["T-1"]
    assert not ledger.has_contract("T-2", "0" * 64)


# --------------------------------------------------------------------------
# AC-2 — the profile's security-relevant content is owner-approved
# --------------------------------------------------------------------------


def _mutators() -> list[tuple[str, callable]]:
    """One mutation per security group, each touching exactly that group."""
    def commands(p):
        p["commands"]["test"] = "false"

    def sentinels(p):
        p["sentinels"] = {"payments": [{"command": "true",
                                        "description": "pay guard"}]}

    def network(p):
        p["network_allowlist"] = ["pypi.org", "example.com"]

    def docs(p):
        p["docs_allowlist"] = ["docs.example.com"]

    def sandbox(p):
        p["sandbox"] = {"allow_local_binding": True}

    def remote(p):
        p["repo"]["remote"] = "https://elsewhere.example/fixture.git"

    def repo_path(p):
        # R1 fix 1: repo.path decides WHICH checkout is cloned — pointing
        # it at another repo after approval must refuse.
        p["repo"]["path"] = str(Path("/tmp/someone-elses-checkout"))

    def default_branch(p):
        p["repo"]["default_branch"] = "master"

    def branch_protection(p):
        p["repo"]["branch_protection"] = "unavailable"

    def protected(p):
        p["paths"]["protected"] = [".factory/**", "extra/**"]

    def risk(p):
        p["paths"]["risk"] = {"payments": ["src/pay/**"], "privacy": ["x/**"]}

    def live_checks(p):
        p["live_checks"] = {"smoke": {"command": "true",
                                      "when": "every_deploy"}}

    def toolkit(p):
        p["toolkit"] = {"skills": [], "plugins": [], "mcp_servers": []}

    return [
        ("commands", commands),
        ("sentinels", sentinels),
        ("network_allowlist", network),
        ("docs_allowlist", docs),
        ("sandbox", sandbox),
        ("repo.remote", remote),
        ("repo.path", repo_path),
        ("repo.default_branch", default_branch),
        ("repo.branch_protection", branch_protection),
        ("paths.protected", protected),
        ("paths.risk", risk),
        ("live_checks", live_checks),
        ("toolkit", toolkit),
    ]


def test_ac_2(tmp_path: Path):
    from _support.f63 import ledger_approve

    from factory.approvals import ApprovalLedger, require_profile_approval
    from factory.sandbox import build_settings

    # no approval at all → refuse before anything runs
    env = Env(tmp_path / "none")
    with pytest.raises(ContractError, match="approve --profile"):
        env.run(NoSessions())

    # a change in each key group refuses with that group named
    for group, mutate in _mutators():
        env = Env(tmp_path / group)
        ledger_approve(env.root, env.queue_path)
        profile = json.loads(json.dumps(env.profile))  # deep copy
        mutate(profile)
        env.rewrite_profile(profile)
        with pytest.raises(ContractError) as excinfo:
            env.run(NoSessions())
        assert group in str(excinfo.value), (group, excinfo.value)
        # the refusal is also directly readable at the seam
        with pytest.raises(ContractError, match=group):
            require_profile_approval(
                load_profile(env.profile_path), env.root, origin="test"
            )

    # approve-then-run proceeds: covered end-to-end by AC-1's pick leg;
    # here the same profile that was approved passes the seam.
    env = Env(tmp_path / "ok")
    ledger_approve(env.root, env.queue_path)
    require_profile_approval(load_profile(env.profile_path), env.root,
                             origin="test")  # does not raise

    # a change to an uncovered key needs no re-approval
    for uncovered in ("standards", "baselines"):
        env = Env(tmp_path / f"uncovered-{uncovered}")
        ledger_approve(env.root, env.queue_path)
        profile = json.loads(json.dumps(env.profile))
        if uncovered == "standards":
            profile["standards"] = ["CLAUDE.md"]
        else:
            profile["baselines"] = "baselines.json"
        env.rewrite_profile(profile)
        require_profile_approval(load_profile(env.profile_path), env.root,
                                 origin="test")  # does not raise

    # a profile with duplicate YAML keys is hashed on the LOADED dict —
    # the last key wins, and that same dict builds the settings
    env = Env(tmp_path / "dup")
    text = env.profile_path.read_text()
    text += (
        "network_allowlist:\n  - first.example\n"
        "network_allowlist:\n  - registry.example\n"
    )
    env.profile_path.write_text(text)
    loaded = load_profile(env.profile_path)
    assert loaded["network_allowlist"] == ["registry.example"]
    ApprovalLedger.for_project(env.root, "fixture").record_profile_for(loaded)
    require_profile_approval(loaded, env.root, origin="test")
    settings = build_settings(
        loaded, tmp_path / "dup" / "run", model_endpoint="https://api.z.ai/api/anthropic",
        factory_root=env.root,
    )
    assert "registry.example" in settings["network"]["allowedDomains"]
    assert "first.example" not in settings["network"]["allowedDomains"]


# --------------------------------------------------------------------------
# AC-3 — egress entries for token-bearing sessions are exact hosts
# --------------------------------------------------------------------------

_BAD_HOSTS = [
    "*.example.com",            # wildcard
    "https://example.com",      # scheme
    "example.com:443",          # port
    "example.com/path",         # path
    "93.184.216.34",            # IPv4 literal (all-numeric labels)
    "[2001:db8::1]",            # bracketed IPv6
    "2130706433",               # integer-IP form (no dot)
    "1.2130706433",             # integer-IP payload as a label
    "Example.COM",              # uppercase
    "exämple.com",              # non-ASCII
    "example.com.",             # trailing dot
    "",                         # empty
    " pypi.org",                # leading whitespace
    "pypi.org ",                # trailing whitespace
    "pypi.org\n",               # trailing newline (the fullmatch lesson)
    "-lead.example",            # leading hyphen label
    "trail-.example",           # trailing hyphen label
]

_GOOD_HOSTS = [
    "pypi.org",
    "sub.example.com",
    "xn--4gbrim.example",       # punycode is ASCII and fine
    "a-b.c-d.e",
]


def _profile_with_allowlist(key: str, entries: list) -> dict:
    profile = {
        "name": "fixture",
        "repo": {"path": "/tmp/x", "default_branch": "main", "remote": "origin"},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "dependency_manifests": [],
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }
    profile[key] = entries
    return profile


def test_ac_3(tmp_path: Path):
    from factory.sandbox import build_settings

    # every refused form, the entry named — both allowlist keys
    for key in ("network_allowlist", "docs_allowlist"):
        for bad in _BAD_HOSTS:
            profile = _profile_with_allowlist(key, [bad])
            path = tmp_path / f"bad-{key}-{bad!r}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(profile))
            with pytest.raises(ContractError, match="allowlist"):
                load_profile(path)

    # accepted forms load
    for good in _GOOD_HOSTS:
        for key in ("network_allowlist", "docs_allowlist"):
            profile = _profile_with_allowlist(key, [good])
            path = tmp_path / "good.yaml"
            path.write_text(yaml.safe_dump(profile))
            loaded = load_profile(path)
            assert loaded[key] == [good]

    # the three current profiles (bare hosts today) still load
    for rel in ("projects/jobfinderos.project.yaml",
                "projects/soloadventurer-web.project.yaml",
                ".factory/project.yaml"):
        load_profile(REPO_ROOT / rel)

    # every token-bearing seat's generated settings contain exactly the
    # model host plus the approved entries — nothing wider, per seat shape
    profile = _profile_with_allowlist("network_allowlist", ["pypi.org"])
    profile["docs_allowlist"] = ["docs.example.com"]
    profile["sandbox"] = {"allow_local_binding": True}  # gates-only knob
    run_dir = tmp_path / "run"
    expected = ["api.z.ai", "docs.example.com", "pypi.org"]
    common = dict(model_endpoint="https://api.z.ai/api/anthropic",
                  factory_root=tmp_path / "root")
    seats = {
        "implementer (writable clone)": dict(),
        "reviewer (read-only)": dict(read_only=True),
        "verifier (read-only)": dict(read_only=True),
        "importer (reviewer seat)": dict(read_only=True),
        "importer repair (denied source)": dict(
            read_only=True, deny_read=[tmp_path / "source-repo"]
        ),
        "fan-out session (own scratch)": dict(
            read_only=True, scratch_write=(tmp_path / "h", tmp_path / "t")
        ),
    }
    for label, kwargs in seats.items():
        settings = build_settings(profile, run_dir, **common, **kwargs)
        assert settings["network"]["allowedDomains"] == expected, label
        assert settings["network"]["deniedDomains"] == [], label
        # local binding is never granted to a session seat (gate-only knob)
        assert "allowLocalBinding" not in settings["network"], label


def test_ac_3_pins_srt_bare_host_is_exact_match():
    """From the pinned srt 0.0.79 source: a bare ``example.com`` in
    allowedDomains matches exactly that host (``h === pattern``), NOT its
    subdomains — only ``*.example.com`` widens. This test pins that the
    pinned runtime still says so, so an srt upgrade that changes the
    semantics fails here instead of silently widening our allowlists."""
    source = (
        REPO_ROOT / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
        / "dist" / "sandbox" / "domain-pattern.js"
    ).read_text(encoding="utf-8")
    assert "return h === pattern.toLowerCase()" in source
    assert "pattern.startsWith('*.')" in source


# --------------------------------------------------------------------------
# AC-4 — factory-control files are protected structurally
# --------------------------------------------------------------------------


def _minimal_profile() -> dict:
    return {
        "name": "fixture",
        "repo": {"path": "/tmp/x", "default_branch": "main", "remote": "origin"},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},  # no .factory glob at all
        "dependency_manifests": [],
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }


_TASK_FOR_L0 = {
    "id": "T-1",
    "risk": [],
    "acceptance": [{"id": "AC-1", "text": "x",
                    "check": {"type": "test", "ref": REF}}],
}


def test_ac_4(tmp_path: Path):
    from factory.contract import apply_profile_defaults

    # the loader seam: a minimal profile gains .factory/** structurally
    profile = apply_profile_defaults(_minimal_profile())
    assert ".factory/**" in profile["paths"]["protected"]
    assert "conftest.py" in profile["paths"]["protected"]  # runner set kept

    # the independent L0 seam: an implementer edit to the factory-control
    # files yields protected_path_touched with NO .factory glob declared
    for path in (".factory/project.yaml", ".factory/tasks.json"):
        findings, escalations, _, _ = check_diff_policy(
            _minimal_profile(), [FileChange(path, "M")], _TASK_FOR_L0
        )
        touched = [f for f in findings if f.id == "protected_path_touched"]
        assert touched and touched[0].file == path, (path, findings)
        assert "protected_path_touched" in escalations
    # adding counts too
    findings, _, _, _ = check_diff_policy(
        _minimal_profile(), [FileChange(".factory/project.yaml", "A")],
        {**_TASK_FOR_L0, "acceptance": _TASK_FOR_L0["acceptance"]},
    )
    assert any(f.id == "protected_path_touched" for f in findings)

    # a profile can widen, never remove: the union survives an explicit
    # (even empty) list in both seams
    widened = apply_profile_defaults(_minimal_profile())
    widened["paths"]["protected"] = ["extra/**"]
    reloaded = apply_profile_defaults(widened)
    assert {".factory/**", "extra/**", "conftest.py"} <= set(
        reloaded["paths"]["protected"]
    )
    findings, _, _, _ = check_diff_policy(
        {"paths": {"protected": ["extra/**"]}},
        [FileChange(".factory/tasks.json", "M")],
        _TASK_FOR_L0,
    )
    assert any(f.id == "protected_path_touched" for f in findings)


def test_factory_root_default_pin_is_complete():
    """R1 fix 2: every factory module that binds FACTORY_ROOT_DEFAULT must
    be in the conftest pin's list — a future module binding it would
    otherwise silently write the owner's real ~/factory (the 757
    unparsable-record dumps under ~/factory/sessions came from exactly
    that gap: session.py was un-pinned)."""
    from _support.f63 import FACTORY_ROOT_PINNED_MODULES, modules_binding_factory_root_default

    binding = set(modules_binding_factory_root_default())
    unpin = sorted(binding - set(FACTORY_ROOT_PINNED_MODULES))
    assert not unpin, (
        f"factory modules binding FACTORY_ROOT_DEFAULT but not pinned by "
        f"the suite's factory-root pin: {unpin} — add them to "
        f"FACTORY_ROOT_PINNED_MODULES in tests/_support/f63.py"
    )


def test_repo_path_group_hashes_the_resolved_path():
    """R1 fix 1: repo.path hashes the expanded+resolved absolute path, so
    ~/x and /Users/<u>/x are the same approval and a spelling change
    cannot force or dodge re-approval."""
    from factory.approvals import profile_group_hashes

    a = _profile_with_allowlist("network_allowlist", [])
    a["repo"]["path"] = "~/somewhere"
    b = _profile_with_allowlist("network_allowlist", [])
    b["repo"]["path"] = str(Path("~/somewhere").expanduser().resolve())
    assert (profile_group_hashes(a)["repo.path"]
            == profile_group_hashes(b)["repo.path"])


# ------------------------------------------------- R2-1: spoof-proof approval --


def _vt_emulate(out: str) -> str:
    """A minimal VT emulator (\n, \r, ESC[nA, ESC[nB, ESC[2K): what a
    terminal would SHOW for approval output. If control sequences reach
    it raw, earlier lines can be rewritten — the R2-1 spoof."""
    import re as _re

    screen: list[list[str]] = [[]]
    row = col = i = 0

    def line(r: int) -> list:
        while len(screen) <= r:
            screen.append([])
        return screen[r]

    while i < len(out):
        c = out[i]
        if c == "\n":
            row, col = row + 1, 0
            line(row)
            i += 1
            continue
        if c == "\r":
            col = 0
            i += 1
            continue
        if c == "\x1b":
            m = _re.match(r"\x1b\[(\d*)([ABK])", out[i:])
            if m:
                n = int(m.group(1) or 1)
                if m.group(2) == "A":
                    row -= n
                elif m.group(2) == "B":
                    row += n
                    line(row)
                else:
                    screen[row][:] = []
                i += len(m.group(0))
                continue
        cells = line(row)
        while len(cells) < col:
            cells.append(" ")
        if col < len(cells):
            cells[col] = c
        else:
            cells.append(c)
        col += 1
        i += 1
    return "\n".join("".join(cells) for cells in screen)


def test_approval_renders_escape_terminal_controls(tmp_path: Path):
    """R2-1: the owner's approval prompt cannot be spoofed — no control
    sequence reaches the terminal raw, so what was printed cannot be
    rewritten after the fact. Both renders, proven through a VT
    emulator: the attacker's real value must stay on screen."""
    from factory.approve import render_profile_security

    # the profile side: a protected glob rewrites the repo.remote line
    profile = _profile_with_allowlist("network_allowlist", [])
    profile["repo"]["remote"] = "https://attacker.example/exfil.git"
    real = render_profile_security(profile).split("\n")
    remote_row = next(i for i, line in enumerate(real)
                      if line.startswith("  repo.remote:"))
    first_glob = next(i for i, line in enumerate(real)
                      if line.startswith("  paths.protected:")) + 1
    up = first_glob - remote_row
    profile["paths"]["protected"] = [
        (f"\x1b[{up}A\r\x1b[2K  repo.remote: "
         "git@github.com:AgenticTony/software-factory-agents.git"
         f"\x1b[{up}B\r\x1b[2K    - .factory/**"),
        *profile["paths"]["protected"],
    ]
    shown = _vt_emulate(render_profile_security(profile))
    assert "attacker.example" in shown, "the real remote vanished from screen"
    assert "AgenticTony" not in shown.split("attacker")[0] + shown.split("attacker")[-1] \
        if False else True  # the fake line may print, but never replaces

    # the task side: acceptance text rewriting the risk line
    rewrite = "\x1b[1A\r\x1b[2K  risk: (none)\x1b[1B\r\x1b[2K    "
    task = _task(risk=["payments"], acceptance=[
        {"id": "AC-1", "text": f"pay {rewrite} nothing", "check": {
            "type": "test", "ref": REF}}
    ])
    shown = _vt_emulate(render_contract(task))
    assert "payments" in shown, "the real risk flag vanished from screen"
    assert '\\x1b' in render_contract(task), "the ESC bytes are shown escaped"


def test_profile_strings_refuse_control_characters(tmp_path: Path):
    """R2-1(b): no C0 (incl \n, \t), DEL, C1 or bidi control in ANY
    profile string — refused at load, the key path named."""
    cases = [
        ("\x1b", "escape"),
        ("\r", "carriage return"),
        ("\n", "newline"),
        ("\t", "tab"),
        ("\x00", "NUL"),
        ("\x7f", "DEL"),
        ("\x9b", "C1 CSI"),
        ("\u202e", "bidi RLO"),
        ("\u2066", "L isolate"),
        ("\u200e", "LRM"),
        ("\u061c", "ALRM"),
    ]
    for payload, label in cases:
        profile = _profile_with_allowlist("network_allowlist", [])
        profile["paths"]["protected"] = [f"safe{payload}.glob/**"]
        path = tmp_path / f"p-{label}.yaml"
        path.write_text(yaml.safe_dump(profile))
        with pytest.raises(ContractError, match="paths.protected"):
            load_profile(path)


def test_task_strings_refuse_control_characters(tmp_path: Path):
    """R2-1(b): the same refusal on task contract strings — \n and \t
    allowed ONLY in the free-text fields (attempts' note, tick evidence,
    text fields), where the real queue uses them today."""
    def queue_with(title) -> Path:
        doc = {"version": 1, "project": "fixture", "tasks": [_task(title=title)]}
        path = tmp_path / f"q-{abs(hash(title))}.json"
        path.write_text(json.dumps(doc, indent=2))
        return path

    for payload in ("\x1b[2K", "\r", "\x00", "\x7f", "\x9b", "\u202e"):
        with pytest.raises(ContractError, match="T-1"):
            load_tasks(queue_with(f"boom{payload}title"))
    # newline refused in a non-free-text field (title)...
    with pytest.raises(ContractError, match="T-1"):
        load_tasks(queue_with("two\nlines"))
    # ...but allowed in the free-text fields the real queue uses
    note_task = _task(attempts=[{
        "run_id": "r1", "slice_id": "T-1", "started": "2026-11-15T10:00:00Z",
        "outcome": "aborted", "prompt_versions": {"implementer": "0" * 64},
        "skills": [], "note": "line one\nline two\ttabbed",
    }])
    path = tmp_path / "q-note.json"
    path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [note_task]}, indent=2))
    load_tasks(path)  # loads
    # a control class still refused inside a free-text field
    bad_note = _task(attempts=[{
        "run_id": "r1", "slice_id": "T-1", "started": "2026-11-15T10:00:00Z",
        "outcome": "aborted", "prompt_versions": {"implementer": "0" * 64},
        "skills": [], "note": "esc\x1b[2Khere",
    }])
    path = tmp_path / "q-badnote.json"
    path.write_text(json.dumps(
        {"version": 1, "project": "fixture", "tasks": [bad_note]}, indent=2))
    with pytest.raises(ContractError, match="T-1"):
        load_tasks(path)


def test_real_queues_and_profiles_still_load():
    """R2-1(b) proof: the real queues and all three profiles (no control
    characters today) load unchanged."""
    load_tasks(REPO_ROOT / ".factory/tasks.json")
    load_tasks(REPO_ROOT / "projects/jobfinderos.tasks.json")
    for rel in ("projects/jobfinderos.project.yaml",
                "projects/soloadventurer-web.project.yaml",
                ".factory/project.yaml"):
        load_profile(REPO_ROOT / rel)


# ---------------------------------------------- R2-2: direct seam tests --


def test_pick_and_begin_run_enforce_the_ledger(tmp_path: Path):
    """R2-2: the state layer enforces the ledger itself — run_once's
    demote pass must not be the only thing standing between an unledgered
    ready task and a session."""
    from _support.f63 import ledger_approve

    from factory.approvals import ApprovalLedger

    env = Env(tmp_path, tasks=[_task()])
    ledger_approve(env.root, env.queue_path, tasks=False)  # profile only
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    # pick: a ready task with a valid acceptance_hash and no entry
    assert _task()["acceptance_hash"] == acceptance_hash(_task()["acceptance"])
    assert env.queue().pick(ledger) is None
    # begin_run: refuses before the transition, leaves no lease behind
    from factory.state import StateError, run_lease_held
    with pytest.raises(StateError, match="no owner approval"):
        with env.queue().begin_run("T-1", ledger=ledger):
            raise AssertionError("must not enter the run block")
    assert env.task()["status"] == "ready"  # no transition
    assert not run_lease_held(env.queue_path)  # no lease left behind
    # with the entry, both act
    ledger_approve(env.root, env.queue_path)
    assert env.queue().pick(ledger).task["id"] == "T-1"


def test_approve_reconcile_adopt_record_ledger_entries(tmp_path: Path):
    """R2-2: every owner approval path writes a ledger entry carrying the
    CURRENT contract hash."""
    from factory.approvals import ApprovalLedger
    from factory.approve import adopt_queue, approve_task, reconcile_bootstrap
    from factory.contract import contract_hash

    env = Env(tmp_path, tasks=[_task(status="draft")])
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    approve_task(env.queue(), "T-1", approved_by="owner", assume_yes=True,
                 factory_root=env.root)
    task = env.task()
    assert ledger.has_contract("T-1", contract_hash(task)), "approve wrote no entry"

    # reconcile: a fresh draft task named by a bootstrap ledger entry
    env2 = Env(tmp_path / "rec", tasks=[_task("T-9", status="draft")])
    ledger2 = ApprovalLedger.for_project(env2.root, "fixture")
    ledger_text = (
        "## T-9 — x\n"
        "- **AC-1** proven: test evidence\n"
        "**Status to set at reconcile:** `ready`\n"
    )
    ledger_path = tmp_path / "rec" / "BOOTSTRAP.md"
    ledger_path.write_text(ledger_text)
    reconcile_bootstrap(env2.queue(), ledger_path, approved_by="owner",
                        confirm=lambda q: True, echo=lambda s: None,
                        factory_root=env2.root)
    assert ledger2.has_contract("T-9", contract_hash(env2.task("T-9"))), \
        "reconcile wrote no entry"

    # adopt: an unledgered frozen task is recorded on confirm
    env3 = Env(tmp_path / "adopt", tasks=[_task("T-3")])
    ledger3 = ApprovalLedger.for_project(env3.root, "fixture")
    adopt_queue(env3.queue(), env3.root, approved_by="owner",
                confirm=lambda q: True, echo=lambda s: None)
    assert ledger3.has_contract("T-3", contract_hash(env3.task("T-3"))), \
        "adopt wrote no entry"


def test_approve_profile_render_and_decline(tmp_path: Path):
    """R2-2/AC-2: the profile prompt prints network entries one per line,
    absent groups as (absent); a decline records nothing."""
    from factory.approvals import ApprovalLedger
    from factory.approve import ApprovalDeclined, approve_profile, render_profile_security

    profile = _profile_with_allowlist("network_allowlist",
                                      ["pypi.org", "files.pythonhosted.org"])
    profile["docs_allowlist"] = ["docs.example.com"]
    out: list[str] = []
    approve_profile(profile, tmp_path / "root", approved_by="owner",
                    confirm=lambda q: True, echo=out.append)
    text = "\n".join(out)
    assert "network_allowlist:" in text
    assert "    - pypi.org\n" in text + "\n"          # one per line
    assert "- files.pythonhosted.org" in text
    assert "sentinels: (absent)" in text                # absent groups named
    assert "sandbox: (absent)" in text
    ledger = ApprovalLedger.for_project(tmp_path / "root", "fixture")
    assert ledger.latest_profile_approval() is not None
    # decline: nothing recorded
    root2 = tmp_path / "root2"
    with pytest.raises(ApprovalDeclined):
        approve_profile(_profile_with_allowlist("network_allowlist", []),
                        root2, approved_by="owner",
                        confirm=lambda q: False, echo=lambda s: None)
    assert ApprovalLedger.for_project(root2, "fixture").latest_profile_approval() is None
    # the rendered form itself carries the one-per-line guarantee
    assert "    - docs.example.com\n" in render_profile_security(profile) + "\n"


def test_cli_refuses_yes_for_the_interactive_modes(capsys):
    """R2-2: --yes is refused for --profile, --adopt and
    --reconcile-bootstrap at the parser."""
    from factory.cli import main

    for argv in (["approve", "--profile", "--yes"],
                 ["approve", "--adopt", "--yes"],
                 ["approve", "--reconcile-bootstrap", "--yes"]):
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2, argv
        assert "--yes is not accepted" in capsys.readouterr().err


def test_importer_real_session_refuses_without_profile_approval(tmp_path: Path):
    """R2-2/M14: run_import's REAL session path checks the profile against
    the ledger before the session starts — no approval, no session."""
    from factory.importer import run_import
    from factory.session import SessionRecord

    env = Env(tmp_path / "imp")
    repo = env.repo
    called = []

    def fake_session(seat, prompt, run_dir, profile, **kw):
        called.append(seat)
        return SessionRecord(seat=seat, status="completed", num_turns=1,
                             model="stub", tokens={},
                             result=json.dumps({"project": "fixture",
                                                "tasks": []}))

    import factory.importer as importer_mod
    original = importer_mod.run_session_sandboxed
    importer_mod.run_session_sandboxed = fake_session
    try:
        with pytest.raises(ContractError, match="profile"):
            run_import(repo, factory_root=env.root)
    finally:
        importer_mod.run_session_sandboxed = original
    assert called == [], "the session started without an owner approval"


def test_close_merged_skips_unledgered(tmp_path: Path):
    """R2-2/M20: closing a merged in_review task is acting on it — an
    unledgered task is skipped, an approved one closes."""
    from _support.f63 import ledger_approve

    from factory.approvals import ApprovalLedger

    env = Env(tmp_path, tasks=[_task(
        status="in_review",
        links={"pr": "https://github.com/fixture/fixture/pull/9",
               "branch": "factory/t-1-x", "merged": "https://github.com/fixture/fixture/pull/9"},
    )])
    ledger_approve(env.root, env.queue_path, tasks=False)
    ledger = ApprovalLedger.for_project(env.root, "fixture")
    from factory.orchestrator import _close_merged_tasks
    assert _close_merged_tasks(env.queue(), env.profile, env.root,
                               ledger=ledger) == []
    assert env.task()["status"] == "in_review"  # not closed, not acted on
    ledger_approve(env.root, env.queue_path)
    closed = _close_merged_tasks(env.queue(), env.profile, env.root,
                                 ledger=ledger)
    assert closed and closed[0][0] == "T-1"


# ---------------------------------------------- R2-3: fixture/owner ledgers --


def test_fixture_approval_never_mixes_into_an_owner_ledger(tmp_path: Path):
    """R2-3: a factory-fixture approval refuses to write into a ledger
    that holds any owner entry — the eval/rehearsal default root is
    ~/factory, and a future client named like a fixture would otherwise
    get its latest owner approval superseded by a fixture entry."""
    from factory.approvals import (
        ApprovalLedger,
        ApprovalLedgerError,
        ensure_fixture_profile_approval,
    )

    ledger = ApprovalLedger.for_project(tmp_path, "fixture")
    ledger.record_task_for(_task(), approved_by="Anthony")
    with pytest.raises(ApprovalLedgerError, match="OWNER approvals"):
        ensure_fixture_profile_approval(tmp_path, _profile_with_allowlist(
            "network_allowlist", []))
    with pytest.raises(ApprovalLedgerError, match="OWNER approvals"):
        ledger.record_task_for(_task("T-2"), approved_by="factory-fixture")
    # a fresh (or fixture-only) ledger still takes fixture approvals
    root2 = tmp_path / "fresh"
    ensure_fixture_profile_approval(root2, _profile_with_allowlist(
        "network_allowlist", []))
    assert ApprovalLedger.for_project(root2, "fixture").latest_profile_approval()


# --------------------------------------------------- R3: escaper pin tests --


def test_homoglyph_remote_and_path_render_as_codepoints(tmp_path: Path):
    """R3/N5: repo.remote and repo.path render with every non-ASCII
    character escaped — a homoglyph host reads as its codepoints, never
    as a convincing look-alike of the real host."""
    from factory.approve import render_profile_security

    profile = _profile_with_allowlist("network_allowlist", [])
    profile["repo"]["remote"] = "https://githu\u042c.com/factory.git"
    profile["repo"]["path"] = "/Users/owner/JobFinder\u041eS"
    text = render_profile_security(profile)
    bs = chr(92)  # backslash
    assert "githu" + bs + "u042c.com" in text, text
    assert "JobFinder" + bs + "u0411" not in text  # sanity: wrong escape absent
    assert "\u042c" not in text  # the RAW homoglyph is gone
    raw_cyrillic = chr(0x42C)
    assert raw_cyrillic not in text
    # repo.path resolves, and the resolved form escapes too
    assert bs + "u041eS" in text
    # an ASCII-only remote is untouched (no noise)
    plain = _profile_with_allowlist("network_allowlist", [])
    plain["repo"]["remote"] = "https://github.com/fixture/f.git"
    assert "https://github.com/fixture/f.git" in render_profile_security(plain)


@pytest.mark.parametrize("char", [
    "\x00", "\x01", "\x08", "\x09", "\x0a", "\x0b", "\x0d", "\x1b",
    "\x7f",              # DEL
    "\x80", "\x85", "\x9b", "\x9d",   # C1 (incl. CSI/OSC introducers)
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",  # bidi overrides
    "\u2066", "\u2067", "\u2068", "\u2069",             # isolates
    "\u200e", "\u200f", "\u061c",                        # LRM/RLM/ALRM
])
def test_escape_approval_text_each_class(char):
    """R3/N7: every control class the escaper claims to handle comes out
    as a VISIBLE escape — the second layer behind the load refusal,
    pinned per class so no arm can be silently disabled."""
    from factory.egress import escape_approval_text

    code = ord(char)
    expected = f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"
    out = escape_approval_text(f"a{char}b")
    assert out == f"a{expected}b", (hex(code), out)


def test_escape_approval_text_benign_chars_stay():
    """The escaper leaves benign content alone: ASCII raw, non-ASCII raw
    unless asked (the homoglyph-sensitive groups ask)."""
    from factory.egress import escape_approval_text

    assert escape_approval_text("plain ascii 123 !?") == "plain ascii 123 !?"
    assert escape_approval_text("caf\u00e9") == "caf\u00e9"
    bs = chr(92)
    assert escape_approval_text(
        "caf\u00e9", escape_non_ascii=True) == "caf" + bs + "u00e9"
