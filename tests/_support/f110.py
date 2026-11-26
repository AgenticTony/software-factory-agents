"""F-110 test support: a pinned-API fixture project, the bench's seats.

Builds on the F-104 harness shapes (real git, a merged task, an approval
ledger, scripted seats) with the layout the real baseline's pins live in:
a package module under ``backend/app/services/`` whose hidden tests reach
it through an ancestor-root import (``from app.services import
pdf_service``), the same shape BENCH-03's ``applicant_name`` keyword pin
has. Only the model seats are scripted.

The fixture's shapes:

* BASE's ``verify_text_layer`` takes ``min_chars``/``label``/``context``;
  the merged HEAD adds ``applicant_name`` — exactly an API name the
  contract never states (the F-110 story);
* ``PINNED_TEST`` calls the project callable with the new keyword (a
  pin); ``PLAIN_TEST`` uses only base-stated names (no pins);
* ``STAR_TEST`` star-imports the project module and uses a head-only
  name (the unresolvable arm);
* the seats cover: the good replay, the wrong fix (passes its own L1
  proof, fails the hidden tests), a round that exits non-completed with
  uncommitted edits (the dirty-retry defect), per-call costs (the summed
  cost) and an interrupting seat (the resume flow).

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
from pathlib import Path

from _support.f63 import ledger_approve
from factory.session import SessionRecord

HIDDEN_PATH = "backend/tests/test_hidden.py"
HIDDEN_PATH_2 = "backend/tests/test_more.py"

# BASE's public surface: no applicant_name anywhere.
BASE_SERVICE = (
    '"""The PDF text-layer service (base surface)."""\n'
    "\n"
    "MIN_CHARS = 200\n"
    "\n"
    "\n"
    "def verify_text_layer(pdf_bytes, *, min_chars=MIN_CHARS,\n"
    '                     label="document", context="send"):\n'
    '    """Verify the PDF is machine-readable."""\n'
    '    return "ok"\n'
)

# HEAD adds the keyword the contract never names (J-5's real shape).
HEAD_SERVICE = (
    '"""The PDF text-layer service (head surface)."""\n'
    "\n"
    "MIN_CHARS = 200\n"
    "\n"
    "\n"
    "def verify_text_layer(pdf_bytes, *, min_chars=MIN_CHARS,\n"
    '                     label="document", context="send",\n'
    "                     applicant_name=None):\n"
    '    """Verify the PDF is machine-readable."""\n'
    "    if applicant_name and any(ord(c) > 255 for c in applicant_name):\n"
    '        return "ok-v2 name-aware"\n'
    '    return "ok-v2"\n'
)

# A SECOND base module whose callable already takes the keyword: a pin
# that exists at base UNDER ANOTHER MODULE PATH must stay a pin when the
# test reaches it through a head-only module instead.
OLD_SERVICE = (
    "def render(text, *, new_kw=None):\n"
    '    return "rendered"\n'
)

# The head-only module re-exporting render: the attack arm where the
# name exists at base only under old_service.
NEW_SERVICE_HEAD = (
    "def render(text, *, new_kw=None):\n"
    '    return "rendered-new"\n'
)

# The keyword pin (the BENCH-03 shape, verbatim from the real item): a
# locally-defined wrapper collects **kwargs and unpacks them into the
# project call — the keyword never appears as a named keyword at the
# project call site itself.
PINNED_TEST = (
    "from app.services import pdf_service\n"
    "\n"
    "\n"
    "def _blocked(pdf, context, **kwargs):\n"
    "    # a wrapper defined in this file: its OWN name is never a pin,\n"
    "    # but the keywords its callers pass reach the project call\n"
    "    return pdf_service.verify_text_layer(\n"
    "        pdf, context=context, **kwargs\n"
    "    )\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    msg = _blocked(b"pdf", "send", applicant_name="\\u0141ukasz")\n'
    '    assert msg == "ok-v2 name-aware"\n'
)

# No pins: every reached name exists at base under the same path.
PLAIN_TEST = (
    "from app.services import pdf_service\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    assert pdf_service.verify_text_layer(b"pdf") == "ok-v2"\n'
)

PLAIN_TEST_2 = (
    "from app.services import pdf_service\n"
    "\n"
    "\n"
    "def test_ac_2():\n"
    '    assert pdf_service.verify_text_layer(b"pdf", label="cv") == "ok-v2"\n'
)

# The aliased-import attribute call with a head-added keyword.
ALIASED_TEST = (
    "import app.services.pdf_service as ps\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    assert ps.verify_text_layer(b"pdf", applicant_name="A") == "ok-v2"\n'
)

# The star-import arm: a head-only name through ``from x import *``.
STAR_TEST = (
    "from app.services.pdf_service import *\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    assert name_group_label() == "from your profile name: "\n'
)

# The other-module-path arm: render exists at base under old_service,
# but the test reaches it through the head-only new_service.
WRONG_PATH_TEST = (
    "from app.services import new_service\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    assert new_service.render("x", new_kw=1) == "rendered-new"\n'
)

# HEAD-only names the star arm reaches.
HEAD_STAR_NAMES = (
    'name_group_label = lambda: "from your profile name: "\n'
)

# The dataclass / pydantic / SQLModel shape (no explicit __init__): a
# keyword is accepted only when it is a DECLARED field. The head adds
# `company` (and changes a default, so the clean arm's test is red on
# base without naming anything new).
POSTING_BASE = (
    "import dataclasses\n"
    "\n"
    "\n"
    "@dataclasses.dataclass\n"
    "class JobPosting:\n"
    "    title: str\n"
    '    url: str = "u"\n'
)
POSTING_HEAD = (
    "import dataclasses\n"
    "\n"
    "\n"
    "@dataclasses.dataclass\n"
    "class JobPosting:\n"
    "    title: str\n"
    '    url: str = "https://u"\n'
    '    company: str = ""\n'
)
DATACLASS_PINNED_TEST = (
    "from app.services.posting_service import JobPosting\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    posting = JobPosting(title="t", url="u", company="c")\n'
    '    assert posting.company == "c"\n'
)
DATACLASS_CLEAN_TEST = (
    "from app.services.posting_service import JobPosting\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    "    posting = JobPosting(title=\"t\")\n"
    '    assert posting.url == "https://u" == JobPosting.url\n'
)

# The B2 arms: a keyword whose WORD appears in contract prose only
# (must refuse), vs a contract line pairing the callable with the
# keyword (states it). The head accepts the keyword; the base does not.
COMPANY_HEAD = (
    '"""The PDF text-layer service (company-aware head)."""\n'
    "\n"
    "MIN_CHARS = 200\n"
    "\n"
    "\n"
    "def verify_text_layer(pdf_bytes, *, min_chars=MIN_CHARS,\n"
    '                     label="document", context="send",\n'
    "                     applicant_name=None, company=None):\n"
    '    if company:\n'
    '        return "ok-v2 company"\n'
    '    return "ok-v2"\n'
)
PROSE_KEYWORD_TEST = (
    "from app.services import pdf_service\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    assert pdf_service.verify_text_layer(b"pdf") == "ok-v2"\n'
    "\n"
    "\n"
    "def helper(text):\n"
    '    return pdf_service.verify_text_layer(text, company="Acme")\n'
)

# Owner-written interface text naming the pinned keyword.
INTERFACE_TEXT = (
    "verify_text_layer accepts applicant_name (str | None): the profile "
    "name the render draws as the document subtitle.\n"
)

# Interface text that QUOTES the merged diff (F-99's rule on the
# interface): names the pinned keyword (so the pin itself is allowed)
# and then quotes a long added code line verbatim.
QUOTE_INTERFACE_LINE = (
    '        return "ok-v2 name-aware"\n'
)
INTERFACE_QUOTING_DIFF = (
    "verify_text_layer accepts applicant_name (str | None). "
    "The service returns:\n\n"
    f"    {QUOTE_INTERFACE_LINE.strip()}\n\n"
    "for a name-carried lost character.\n"
)

SPEC_PATH = "docs/plan.md"
SPEC_DOC = (
    "# The plan\n"
    "\n"
    "## T-real-2: the text layer knows the applicant name\n"
    "\n"
    "The verified render covers the profile name drawn as subtitle.\n"
)

TASK_ID = "T-real-2"
PR_NUMBER = 11
PR_URL = f"https://github.com/fixture/proj/pull/{PR_NUMBER}"

# The replay's implementer-side fix (the good seat writes this).
GOOD_SERVICE = HEAD_SERVICE
# The wrong fix: its own proof passes, the hidden tests fail.
WRONG_SERVICE = (
    '"""A wrong fix: no name awareness."""\n'
    "\n"
    "MIN_CHARS = 200\n"
    "\n"
    "\n"
    "def verify_text_layer(pdf_bytes, *, min_chars=MIN_CHARS,\n"
    '                     label="document", context="send",\n'
    "                     who=None):\n"
    '    return "ok-wrong"\n'
)
WRONG_OWN_TEST = (
    "from app.services import pdf_service\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    '    assert pdf_service.verify_text_layer(b"pdf", who="A") == "ok-wrong"\n'
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _pkg_init(doc: str) -> str:
    return f'"""{doc}"""\n'


class Env110:
    """One fixture project: repo + merged task + ledger + bare remote.

    ``hidden_test`` is the stored hidden test's text (PINNED_TEST by
    default — the AC-1 arms), ``hidden_test_2`` an optional second file
    (AC-2's every-ref arm), ``interface`` owner-written interface text
    stored at add, ``star_head_names`` extra head-module content the
    star arm needs, and ``max_attempts`` the task budget the replay
    runs under (2 exercises the retry path).
    """

    def __init__(self, tmp_path: Path, *, hidden_test: str = PINNED_TEST,
                 hidden_test_2: "str | None" = None,
                 interface: "str | None" = None,
                 star_head_names: "str | None" = None,
                 base_service: str = BASE_SERVICE,
                 head_service: str = HEAD_SERVICE,
                 with_old_service: bool = False,
                 with_new_service: bool = False,
                 setup_command: str = "true",
                 setup_outputs: "list[str] | None" = None,
                 max_attempts: int = 2,
                 criterion_text: str = "the verified render covers the name",
                 extra_base_files: "dict[str, str] | None" = None,
                 extra_head_files: "dict[str, str] | None" = None):
        import yaml

        self.tmp = tmp_path
        self.factory_root = tmp_path / "factory-root"
        self.factory_root.mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")

        acceptance = [
            {"id": "AC-1", "text": criterion_text,
             "check": {"type": "test",
                       "ref": f"{HIDDEN_PATH}::test_ac_1"}},
        ]
        if hidden_test_2 is not None:
            acceptance.append(
                {"id": "AC-2", "text": "the label override works too",
                 "check": {"type": "test",
                           "ref": f"{HIDDEN_PATH_2}::test_ac_2"}})
        from factory.contract import acceptance_hash

        self.task = {
            "id": TASK_ID,
            "title": "The text layer knows the applicant name",
            "source": f"{SPEC_PATH}#t-real-2",
            "kind": "build",
            "priority": "P1",
            "status": "in_review",
            "depends_on": [],
            "risk": [],
            "gates": {"spec_approval": True, "merge": "human",
                      "deploy": "human"},
            "acceptance": acceptance,
            "acceptance_hash": acceptance_hash(acceptance),
            "approved_at": "2026-11-26T10:00:00Z",
            "approved_by": "owner",
            "budget": {"size": "S", "max_attempts": max_attempts,
                       "max_turns": 20, "wall_clock_min": 30},
            "scope": {"paths": ["backend/app/services/**",
                                "backend/tests/test_hidden.py",
                                "backend/tests/test_more.py"]},
            "attempts": [],
        }
        self.profile = {
            "name": "fixture",
            "providers_allowed": ["anthropic"],
            "repo": {
                "path": str(self.repo),
                "default_branch": "main",
                "remote": str(self.remote),
                "merge_method": "squash",
            },
            "commands": {
                "setup": setup_command,
                "test": f"{shlex.quote(sys.executable)} -m pytest -q "
                        "-p no:cacheprovider",
            },
            "paths": {
                "risk": {},
                "protected": [".factory/**"],
                **({"setup_outputs": setup_outputs}
                   if setup_outputs else {}),
            },
            "dependency_manifests": ["pyproject.toml"],
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        (factory_dir / "project.yaml").write_text(
            yaml.safe_dump(self.profile, sort_keys=False))
        self.queue_path = factory_dir / "tasks.json"

        services = self.repo / "backend" / "app" / "services"
        services.mkdir(parents=True)
        (services / "__init__.py").write_text(
            _pkg_init("app.services"))
        (self.repo / "backend" / "app" / "__init__.py").write_text(
            _pkg_init("app"))
        (services / "pdf_service.py").write_text(base_service)
        if with_old_service:
            # render(new_kw=) exists at BASE under old_service only.
            (services / "old_service.py").write_text(OLD_SERVICE)
        for rel, text in (extra_base_files or {}).items():
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        tests = self.repo / "backend" / "tests"
        tests.mkdir(parents=True)
        (tests / "__init__.py").write_text("")
        (tests / "test_base.py").write_text(
            "def test_base():\n    assert True\n")
        docs = self.repo / "docs"
        docs.mkdir()
        (docs / "plan.md").write_text(SPEC_DOC)
        (self.repo / ".gitignore").write_text(
            ".factory/*.lock\n"
            + "".join(f"{line}\n" for line in (setup_outputs or [])))
        (self.repo / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\nversion = '0'\n")
        self.base_sha = _commit(self.repo, "base")

        # The merged task branch: the fix and the hidden tests.
        branch = "factory/t-real-2-20261126T000000Z-abc123"
        _git(self.repo, "checkout", "-q", "-b", branch)
        (services / "pdf_service.py").write_text(head_service)
        if star_head_names is not None:
            (services / "pdf_service.py").write_text(
                head_service + "\n\n" + star_head_names)
        if with_new_service:
            (services / "new_service.py").write_text(NEW_SERVICE_HEAD)
        for rel, text in (extra_head_files or {}).items():
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        (tests / "test_hidden.py").write_text(hidden_test)
        if hidden_test_2 is not None:
            (tests / "test_more.py").write_text(hidden_test_2)
        self.head_sha = _commit(self.repo, f"{TASK_ID}: the fix")
        self.branch = branch
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "--squash", branch)
        self.merge_sha = _commit(
            self.repo, f"{TASK_ID}: the fix (#{PR_NUMBER})")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo),
             str(self.remote)], check=True)
        _git(self.repo, "remote", "add", "origin", str(self.remote))
        _git(self.repo, "fetch", "--quiet", "origin")
        self.task["links"] = {
            "pr": PR_URL, "branch": branch, "pushed_head": self.head_sha,
            "merged": PR_URL,
        }
        self.queue_path.write_text(json.dumps(
            {"version": 1, "project": "fixture", "tasks": [self.task]},
            indent=2) + "\n")
        _git(self.repo, "add", "-A")
        _commit(self.repo, "queue")
        ledger_approve(self.factory_root, self.queue_path)
        self.interface = interface


def _verdict(*findings: dict) -> str:
    return json.dumps({"verdict": "fail" if findings else "pass",
                       "findings": list(findings)})


def _finding(text: str) -> dict:
    return {"id": "R-1", "axis": "standards", "severity": "major",
            "file": "backend/app/services/pdf_service.py", "text": text}


class Seats110:
    """Scripted seats for the F-110 replay scenarios.

    ``mode`` picks the implementer's behaviour:

    * ``good`` — the merged answer (hidden tests pass, factory
      publishes);
    * ``wrong`` — the wrong fix with its own passing proof (L1 green,
      hidden tests fail);
    * ``dirty_exit`` — the FIRST implementer call edits the module and
      ends the session NON-completed (the round the orchestrator
      cannot commit: the dirty-retry defect's cause); later calls
      complete.

    ``costs`` prices each implementer call in order (the summed-cost
    arms); ``interrupt_at`` raises KeyboardInterrupt on that
    1-based implementer call count (the resume arms); ``reviews``
    "block" never converges.
    """

    def __init__(self, mode: str = "good", reviews: str = "pass",
                 costs: "list[float] | None" = None,
                 interrupt_at: int = 0, implementer_turns: int = 3,
                 reviewer_cost: float = 0.001,
                 verifier_cost: float = 0.0005):
        self.mode = mode
        self.reviews = reviews
        self.costs = list(costs or [])
        self.interrupt_at = interrupt_at
        self.implementer_turns = implementer_turns
        self.reviewer_cost = reviewer_cost
        self.verifier_cost = verifier_cost
        self.lock = threading.Lock()
        self.calls: list[tuple[str, Path, str]] = []
        self.impl_calls = 0

    def seats(self) -> list[str]:
        return [seat for seat, _, _ in self.calls]

    def prompts(self, seat: str) -> "list[str]":
        return [prompt for s, _, prompt in self.calls if s == seat]

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        with self.lock:
            self.calls.append((seat, run_dir, prompt))
            if seat == "implementer":
                self.impl_calls += 1
                call_no = self.impl_calls
        if seat == "implementer":
            if self.interrupt_at and call_no >= self.interrupt_at:
                raise KeyboardInterrupt("bench interrupt arm")
            service = run_dir / "backend" / "app" / "services" \
                / "pdf_service.py"
            own_ref = "backend/tests/test_hidden.py"
            if self.mode == "good":
                text, own = GOOD_SERVICE, PINNED_TEST
            elif self.mode == "wrong":
                text, own = WRONG_SERVICE, WRONG_OWN_TEST
            else:  # dirty_exit
                text, own = GOOD_SERVICE, PINNED_TEST
            service.parent.mkdir(parents=True, exist_ok=True)
            service.write_text(text)
            target = run_dir / own_ref
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(own)
            # The dirty-retry defect's cause: only the FIRST call ends
            # non-completed (the orchestrator cannot commit its tree);
            # the retry completes like any good round.
            dirty = self.mode == "dirty_exit" and call_no == 1
            cost = (self.costs[call_no - 1]
                    if call_no <= len(self.costs) else 0.004)
            return SessionRecord(
                seat=seat,
                status="aborted" if dirty else "completed",
                reason="turn budget" if dirty else None,
                num_turns=self.implementer_turns, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                usage={"input": 120, "cache_read": 0, "cache_write": 0,
                       "output": 40},
                total_cost_usd=cost,
                result="Implemented.",
            )
        if seat == "verifier":
            import re

            match = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
            fid = match.group(1) if match else "R-1"
            return SessionRecord(
                seat=seat, status="completed", num_turns=1,
                model="stub-model",
                tokens={"input_tokens": 30, "output_tokens": 5},
                usage={"input": 30, "cache_read": 0, "cache_write": 0,
                       "output": 5},
                total_cost_usd=self.verifier_cost,
                result=json.dumps(
                    {"votes": [{"id": fid, "keep": True,
                                "reason": "scripted keep"}]}),
            )
        verdict = (_verdict(_finding("the name path has no test"))
                   if self.reviews == "block" else _verdict())
        return SessionRecord(
            seat=seat, status="completed", num_turns=1,
            model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            usage={"input": 60, "cache_read": 0, "cache_write": 0,
                   "output": 10},
            total_cost_usd=self.reviewer_cost,
            result=verdict,
        )
