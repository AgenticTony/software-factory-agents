"""Shared F-71 harness: a pre-spend fixture repo, queue and registry stubs.

The fixture models the F-13 pilot's failure shapes (never that repo's
content): a repo whose profile routes one service file and a billing
tree through the ``external_service`` risk flag, one protected
calibration test, and pip manifests. The approval side gets a queue
beside a loadable profile; the L0 side gets a two-commit repo whose
branch changes dependency manifests.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = "prespend-demo"

# --- the fixture repo -------------------------------------------------------

RISK_FILE = "svc/pdf_service.py"
RISK_GLOB_FILE = "billing/invoice.py"
PROTECTED_TEST = "backend/tests/test_calibration.py"

BASE_REQUIREMENTS_TXT = "fastapi>=0.100\n"
BASE_REQUIREMENTS_LOCK = "fastapi==0.100.0\nstarlette==0.27.0\n"
BASE_PYPROJECT = """\
[project]
name = "fixture"
version = "0.1.0"
dependencies = ["httpx>=0.27"]

[tool.ruff]
line-length = 88
"""

BASE_FILES = {
    RISK_FILE: "def stamp(pdf): ...\n",
    "svc/util.py": "def tidy(text): ...\n",
    RISK_GLOB_FILE: "def total(rows): ...\n",
    "billing/deep/invoice.py": "def tax(rows): ...\n",
    PROTECTED_TEST: "def test_cal(): assert True\n",
    "requirements.txt": BASE_REQUIREMENTS_TXT,
    "requirements.lock": BASE_REQUIREMENTS_LOCK,
    "pyproject.toml": BASE_PYPROJECT,
    "README.md": "# prespend fixture\n",
}

_PROFILE = """\
# The F-71 fixture project: the pilot's risk/protected shapes.
name: prespend-demo

repo:
  path: {repo}
  default_branch: main
  remote: https://example.invalid/prespend-demo.git
  merge_method: squash

commands:
  setup: "true"
  test: "true"

paths:
  risk:
    external_service:
      - "svc/pdf_service.py"
      - "billing/**"
      # A file that exists ONLY untracked in the fixture's working tree
      # (never committed): the default branch is the truth, so a scope
      # reaching it alone approves.
      - "svc/untracked_service.py"
    # fnmatch semantics: `*` CROSSES `/` (the risk matcher is the
    # broader, fail-safe reading — a risk glob should over-match).
    payments:
      - "billing/*"
    # A flag whose glob names ONLY a file that exists untracked in the
    # working tree: the default branch is the truth, so a scope reaching
    # it alone approves (arm g's discriminator).
    privacy:
      - "svc/untracked_service.py"
  protected:
    - "backend/tests/test_calibration.py"

dependency_manifests:
  - requirements.txt
  - requirements.lock
  - pyproject.toml
  - package.json
  - package-lock.json

deploy:
  mechanism: none
  auto_on_merge: false

live_checks: {{}}

docs_allowlist: []
"""


# The R1-7 arm's profile: the standard risk/protected layout plus a
# paths.generated allowance over a risk glob — approval must treat the
# generated paths as candidates exactly as L0's allowance does.
PROFILE_WITH_GENERATED = """\
# The F-71 fixture project with a generated-path allowance (R1-7).
name: prespend-demo

repo:
  path: {repo}
  default_branch: main
  remote: https://example.invalid/prespend-demo.git
  merge_method: squash

commands:
  setup: "true"
  test: "true"

paths:
  risk:
    external_service:
      - "svc/pdf_service.py"
      - "billing/**"
    payments:
      - "billing/*"
  protected:
    - "backend/tests/test_calibration.py"
  generated:
    - "billing/generated/**"

dependency_manifests:
  - requirements.txt
  - requirements.lock
  - pyproject.toml
  - package.json
  - package-lock.json

deploy:
  mechanism: none
  auto_on_merge: false

live_checks: {{}}

docs_allowlist: []
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def build_repo(tmp_path: Path, *, files: dict | None = None) -> Path:
    """The fixture git repo, one base commit on ``main``."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for rel, text in (files if files is not None else BASE_FILES).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@factory.local")
    _git(repo, "config", "user.name", "fixture")
    _commit_all(repo, "base")
    return repo


def branch(repo: Path, name: str = "feature") -> None:
    _git(repo, "checkout", "-q", "-b", name)


def commit_files(repo: Path, files: dict, message: str = "change") -> str:
    """One commit on the current branch; returns its sha."""
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _commit_all(repo, message)
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def tracked_files(repo: Path, ref: str = "main") -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.splitlines()


# --- the fixture queue + profile --------------------------------------------


def _task(task_id: str = "WO-9", **overrides) -> dict:
    """One schema-valid draft build task; F-71's defaults are the floor
    (60 turns / 30 min), an exact test ref, and a scope that reaches no
    risk path (the risk-reach arms override the scope), so every arm
    states only what it probes."""
    task = {
        "id": task_id,
        "title": f"Work order {task_id}",
        "source": f"docs/work-orders/{task_id}.md",
        "kind": "build",
        "priority": "P1",
        "status": "draft",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "scope": {"paths": ["svc/util.py"]},
        "acceptance": [
            {"id": "AC-1", "text": "it works",
             "check": {"type": "test",
                       "ref": "tests/test_wo9.py::test_ac_1"}},
        ],
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 60,
                   "wall_clock_min": 30},
    }
    task.update(overrides)
    return task


def write_queue(tmp_path: Path, tasks: list[dict], *, repo: Path,
                profile_extra: str = "", profile_name: str | None = None,
                profile_text: str | None = None,
                with_profile: bool = True) -> Path:
    """A queue with its profile beside it (the approve --profile default),
    ``repo.path`` pointing at the fixture repo. ``profile_text`` replaces
    the profile wholesale (an arm that needs its own risk/generated
    layout); ``{repo}`` is substituted."""
    factory = tmp_path / "control" / ".factory"
    factory.mkdir(parents=True, exist_ok=True)
    queue_path = factory / "tasks.json"
    queue_path.write_text(json.dumps(
        {"version": 1, "project": PROJECT, "tasks": tasks}, indent=2
    ) + "\n", encoding="utf-8")
    if with_profile:
        if profile_text is not None:
            profile = profile_text.format(repo=repo)
        else:
            profile = _PROFILE.format(repo=repo)
            if profile_name:
                profile = profile.replace(
                    f"name: {PROJECT}", f"name: {profile_name}")
            profile += profile_extra
        (factory / "project.yaml").write_text(profile, encoding="utf-8")
    return queue_path


def say_yes(_question: str) -> bool:
    return True


def say_no(_question: str) -> bool:
    return False


class Echo:
    """Captured approval output; ``text`` joins every line."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.extend(text.splitlines())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# --- the registry stub --------------------------------------------------------


def _iso(days_ago: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def pypi_doc(name: str, *, first_release_days_ago: float = 400.0) -> dict:
    """A PyPI JSON document whose FIRST (earliest) release is that many
    days old — every version stamp sits at or after it, so the minimum
    the client takes is the intended age."""
    return {
        "info": {"name": name},
        "releases": {
            "0.1.0": [{"upload_time_iso_8601": _iso(first_release_days_ago)}],
            "1.0.0": [{"upload_time_iso_8601": _iso(first_release_days_ago)}],
        },
    }


def npm_doc(name: str, *, first_release_days_ago: float = 400.0) -> dict:
    """An npm registry document whose first version is that many days
    old (every stamp at it, so the minimum is the intended age)."""
    return {
        "name": name,
        "dist-tags": {"latest": "1.0.0"},
        "time": {
            "created": _iso(first_release_days_ago),
            "modified": _iso(1.0),
            "0.1.0": _iso(first_release_days_ago),
            "1.0.0": _iso(first_release_days_ago),
        },
        "versions": {
            "0.1.0": {"name": name, "version": "0.1.0"},
            "1.0.0": {"name": name, "version": "1.0.0"},
        },
    }


class StubGetJson:
    """The registry client's ONE network seam, stubbed.

    ``answers`` maps (ecosystem, normalised-name) to a JSON document.
    ``fail`` names (ecosystem, name) pairs the "registry" fails for —
    None return (down), a raised timeout, or a malformed body. Only the
    HTTP fetch is stubbed; every check above it (parse, name match,
    first release, near-miss) runs real.
    """

    def __init__(self, answers: dict, *, fail: dict | None = None) -> None:
        self.answers = answers
        self.fail = fail or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, ecosystem: str, name: str):
        self.calls.append((ecosystem, name))
        mode = self.fail.get((ecosystem, name), "ok")
        if mode == "down":
            return None
        if mode == "timeout":
            raise TimeoutError(f"{ecosystem} registry timed out")
        if mode == "malformed":
            return "<html>not json</html>"
        if mode == "wrong-name":
            return (pypi_doc if ecosystem == "pypi" else npm_doc)(
                f"other-{name}")
        if mode == "oversize":
            return "x" * 10
        return self.answers[(ecosystem, name)]


def declared_dependency(name: str, ecosystem: str = "pypi") -> dict:
    return {"ecosystem": ecosystem, "name": name}
