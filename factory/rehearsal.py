"""The realistic rehearsal fixture (BUILD-ORDER F-24, AC-1/AC-2).

Every fixture the factory's own tests used before F-24 was a toy repo:
nothing installed, a short PATH, a remote that never moves. That is exactly
how the three JobFinderOS readiness incidents of 2026-10-18 (PR #24)
passed CI — each one only exists on a repo that looks like a real project.
This module builds such a repo, offline, and drives one rehearsal run
through the real ``factory run`` machinery with only the model seats
stubbed.

The fixture is realistic on the three axes that broke that day:

* **Installed setup outputs.** ``commands.setup`` creates a Python venv
  and a ``node_modules`` tree (a vendored package installed by npm, with
  a lifecycle script, from the local tree — no registry, no network) and
  the profile declares both as ``paths.setup_outputs`` so L1's between-
  phase clean keeps them. The ``test``/``test_ref`` commands run through
  ``.venv/bin/python`` and ``lint`` through ``node_modules/.bin``: wipe
  either tree and every gate command dies with exit 127 — the shape of
  incident (1).
* **An ambient PATH with home-dir entries.** :func:`ambient_path_with_home_entries`
  recreates the incident's environment: real home dirs (~/.pyenv/shims,
  ~/.local/bin, …) first, plus a literal ``~`` entry. The sandbox drops
  inherited home entries (either spelling) and literal ``~`` entries
  (PR #24, incident 2); the fixture's ``tools/env-guard.sh`` — a plain
  toolchain precondition with the host's home paths baked in — refuses a
  PATH entry under the home (either spelling) or an unexpanded ``~``
  entry, so a reverted *or simplified* filter fails setup inside the
  sandbox, deterministically on every platform.
* **A remote ahead of the local clone.** The bare remote's ``main``
  carries a commit the local repo has never pulled, so a run that clones
  the local checkout without fetching the remote builds on stale code —
  incident (3).

The rehearsal itself (:func:`run_rehearsal`) is one ``run_once`` over a
one-criterion contract: real clone manager, real srt-sandboxed L0 and L1,
real push and PR (via a recording ``open_pr``), stub seats. F-28's
readiness-run mechanics are this same path.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from factory.contract import acceptance_hash
from factory.gates import git as hardened_git
from factory.orchestrator import run_once
from factory.publish import PullRequest
from factory.session import SessionRecord

REHEARSAL_TEST_REF = "tests/test_feature.py::test_ac_1"

TEST_SOURCE = "from impl import answer\n\n\ndef test_ac_1():\n    assert answer() == 42\n"

REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})

# The fixture's tracked file set. Committed to the base commit on main, so
# every clone carries them; the contract test file is NOT here — the
# implementer seat adds it on the branch, the red-first way.
_RUN_REF = '''"""Run one named test ref (``path::name``) or a whole test file.

The fixture project's own runner: pytest is not installed into the venv
(the rehearsal is offline), so tests are plain functions the runner calls
directly. Exit 0 = every test passed, 1 = a failure, 2 = nothing ran.
"""
import importlib.util
import os
import sys
import traceback


def _load(path):
    sys.path.insert(0, os.getcwd())  # the project root, where impl.py lives
    spec = importlib.util.spec_from_file_location("rehearsal_" + path.replace("/", "_"),
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv):
    if len(argv) != 2:
        print("usage: run_ref.py <path>[::<name>]", file=sys.stderr)
        return 2
    path, _, name = argv[1].partition("::")
    module = _load(path)
    if name:
        functions = [(name, getattr(module, name))]
    else:
        functions = sorted((n, f) for n, f in vars(module).items()
                           if n.startswith("test") and callable(f))
    if not functions:
        print("no tests found in %s" % path, file=sys.stderr)
        return 2
    failed = 0
    for test_name, func in functions:
        try:
            func()
            print("%s::%s passed" % (path, test_name), flush=True)
        except Exception:
            traceback.print_exc()
            print("%s::%s FAILED" % (path, test_name), flush=True)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''

# A plain toolchain precondition, the kind real setups carry: command
# resolution through an unexpanded PATH entry is broken for every tool
# downstream (the shell never expanded it), and so is resolution through
# the owner's home, which the sandbox denies reading. Inside the sandbox
# this is the deterministic canary for incident (2) on BOTH leak classes:
# the sanitised child PATH never carries a literal `~` entry or anything
# under the real home, and a leaked one of either kind always does — on
# every platform, with no dependence on how a given execvp treats a
# denied candidate. The home paths are baked in at fixture-build time
# (both spellings of the home, the same set `_path_outside_home` drops).
_ENV_GUARD_TEMPLATE = '''#!/bin/sh
# Refuse a PATH that leaks the owner's home (either spelling) or carries
# an unexpanded `~` entry. Home paths are baked in by factory.rehearsal.
homes='{homes}'
rest=$PATH:
while [ -n "$rest" ]; do
  entry=${{rest%%:*}}
  rest=${{rest#*:}}
  case $entry in
    "~"|"~/"*)
      echo "env-guard: PATH carries an unexpanded ~ entry: $entry" >&2
      exit 1
      ;;
  esac
  scan=$homes:
  while [ -n "$scan" ]; do
    home=${{scan%%:*}}
    scan=${{scan#*:}}
    case $entry in
      "$home"|"$home"/*)
        echo "env-guard: PATH leaks a home-dir entry ($home): $entry" >&2
        exit 1
        ;;
    esac
  done
done
'''

_LINT_JS = '''#!/usr/bin/env node
// The fixture's lint tool: reads the tracked manifest it checks for.
const fs = require("fs");
fs.readFileSync("lint-manifest.txt", "utf8");
console.log("lint-ok");
'''

_TINY_KIT = '''{
  "name": "tiny-kit",
  "version": "1.0.0",
  "bin": { "rehearse-lint": "bin/lint.js" },
  "scripts": { "install": "node -e \\"console.log('tiny-kit lifecycle ran')\\"" }
}
'''


@dataclass
class RehearsalFixture:
    """The built fixture project: local repo (stale main), advanced remote,
    and the orchestrator-side profile/queue paths."""

    repo: Path
    remote: Path
    profile_path: Path
    queue_path: Path
    local_main: str
    remote_main: str

    def task(self) -> dict:
        from factory.state import TaskQueue

        return TaskQueue(self.queue_path).get("T-1")


def ambient_path_with_home_entries() -> str:
    """The ambient PATH of the owner's interactive shell, the incident's
    environment: home dirs first (both spellings the fix drops), then the
    real PATH. ``~/.local/bin`` and friends need not exist on the host —
    the leaked literal ``~`` entry is the deterministic canary. Empty
    entries are dropped everywhere: an empty PATH entry means the current
    directory, a leak class of its own that AC-3 could not attribute."""
    home = Path.home()
    inherited = [entry for entry in os.environ.get("PATH", "").split(":") if entry]
    return ":".join(
        [
            f"{home}/.pyenv/shims",
            f"{home}/.local/bin",
            f"{home}/.npm-global/bin",
            "~/.local/bin",
            *inherited,
        ]
    )


def _env_guard_source() -> str:
    """The guard script with this host's home paths baked in (both
    spellings, the same set ``_path_outside_home`` drops)."""
    homes = {str(Path.home())}
    homes.add(str(Path.home().resolve()))
    return _ENV_GUARD_TEMPLATE.format(homes=":".join(sorted(homes)))


def _write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(0o755)


def _profile(repo: Path, remote: Path) -> dict:
    return {
        "name": "rehearsal-fixture",
        "repo": {
            "path": str(repo),
            "default_branch": "main",
            "remote": str(remote),
            "merge_method": "squash",
        },
        "commands": {
            "setup": (
                "tools/env-guard.sh && python3 -m venv .venv"
                " && npm install --no-audit --no-fund --no-save ./vendor/tiny-kit"
            ),
            "lint": "node_modules/.bin/rehearse-lint",
            "test": ".venv/bin/python tools/run_ref.py tests/test_base.py",
            "test_ref": ".venv/bin/python tools/run_ref.py {ref}",
        },
        "paths": {
            "risk": {},
            "protected": [".factory/**"],
            "setup_outputs": [".venv", "node_modules"],
        },
        "dependency_manifests": ["package.json"],
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {},
        "docs_allowlist": [],
    }


def _task() -> dict:
    acceptance = [
        {
            "id": "AC-1",
            "text": "answer() returns 42",
            "check": {"type": "test", "ref": REHEARSAL_TEST_REF},
        }
    ]
    return {
        "id": "T-1",
        "title": "Make answer() return 42 (rehearsal)",
        "source": "docs/T-1.md",
        "kind": "build",
        "priority": "P1",
        "status": "ready",
        "depends_on": [],
        "risk": [],
        "gates": {"spec_approval": True, "merge": "human", "deploy": "human"},
        "acceptance": acceptance,
        "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-19T10:00:00Z",
        "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 2, "max_turns": 20, "wall_clock_min": 30},
        "attempts": [],
    }


def build_rehearsal_fixture(dest: Path) -> RehearsalFixture:
    """Build the fixture project at ``dest`` (offline, no network).

    The local repo sits at the base commit; the bare remote's ``main`` is
    one commit ahead (a file that landed "on GitHub" and was never pulled)
    — the stale-checkout shape of incident (3).
    """
    dest = Path(dest)
    dest.mkdir(parents=True)
    hardened_git(dest, "init", "-q", "-b", "main")
    hardened_git(dest, "config", "user.email", "rehearsal@example.com")
    hardened_git(dest, "config", "user.name", "Rehearsal")

    _write(dest / "impl.py", "def answer():\n    return 0\n")
    _write(dest / "tools" / "run_ref.py", _RUN_REF)
    _write(dest / "tools" / "env-guard.sh", _env_guard_source(), executable=True)
    _write(dest / "tests" / "test_base.py", "def test_base():\n    assert True\n")
    _write(dest / "package.json", '{"name": "rehearsal-fixture", "private": true}\n')
    _write(dest / "lint-manifest.txt", "rehearsal fixture lint manifest\n")
    _write(dest / "vendor" / "tiny-kit" / "package.json", _TINY_KIT)
    _write(dest / "vendor" / "tiny-kit" / "bin" / "lint.js", _LINT_JS)

    remote = dest.parent / f"{dest.name}-remote.git"
    _write(dest / ".factory" / "project.yaml", yaml.safe_dump(_profile(dest, remote)))
    _write(
        dest / ".factory" / "tasks.json",
        json.dumps(
            {"version": 1, "project": "rehearsal-fixture", "tasks": [_task()]}, indent=2
        )
        + "\n",
    )
    # A real project ignores what setup installs: the orchestrator commits
    # with `git add -A`, so a session that ran setup would otherwise put
    # the whole .venv and node_modules tree into its PR.
    _write(dest / ".gitignore", ".factory/*.lock\n/.venv/\n/node_modules/\n")

    hardened_git(dest, "add", "-A")
    hardened_git(dest, "commit", "-qm", "base")
    local_main = hardened_git(dest, "rev-parse", "refs/heads/main").strip()

    subprocess.run(
        ["git", "clone", "--bare", "-q", str(dest), str(remote)], check=True
    )
    scratch = dest.parent / f"{dest.name}-scratch"
    subprocess.run(["git", "clone", "-q", str(remote), str(scratch)], check=True)
    identity = (
        "-c", "user.email=rehearsal@example.com", "-c", "user.name=Rehearsal",
    )
    try:
        _write(
            scratch / "REMOTE-ONLY.md",
            "Landed on the remote's default branch; the local clone never pulled it.\n",
        )
        hardened_git(scratch, *identity, "add", "-A")
        hardened_git(
            scratch, *identity, "commit", "-qm", "advance the default branch on the remote"
        )
        hardened_git(scratch, "push", "-q", "origin", "HEAD:main")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    remote_main = hardened_git(remote, "rev-parse", "refs/heads/main").strip()

    return RehearsalFixture(
        repo=dest,
        remote=remote,
        profile_path=dest / ".factory" / "project.yaml",
        queue_path=dest / ".factory" / "tasks.json",
        local_main=local_main,
        remote_main=remote_main,
    )


class _RehearsalSeats:
    """Stub seats with :func:`factory.session.run_session_sandboxed`'s
    signature: the implementer writes the fix plus the contract test into
    the run dir the way a session would; the reviewer returns a pass
    verdict. No model, no API spend — everything downstream (commit, clean
    clone, sandboxed L0/L1, push, PR) is the real machinery."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, seat: str, prompt: str, run_dir, profile, **kwargs):
        run_dir = Path(run_dir)
        self.calls.append((seat, run_dir))
        if seat == "implementer":
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            return SessionRecord(
                seat=seat, status="completed", num_turns=2, model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                result="Implemented answer().",
            )
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            result=REVIEW_PASS,
        )


class RehearsalError(RuntimeError):
    """The rehearsal failed a property the run's outcome alone doesn't show.

    F-28's readiness flow treats a raised rehearsal exactly like a failed
    one: the run is not ready.
    """


def run_rehearsal(
    fixture: RehearsalFixture, factory_root: Path, *, now=None
) -> tuple:
    """One full ``factory run`` over the fixture's one-criterion contract.

    Beyond the run's own outcome, the rehearsal proves the property the
    outcome cannot show on its own (incident INC-003): a ``pr_opened``
    result whose PR head does not contain the remote's default-branch
    commit was built on a stale local checkout, and raises instead of
    returning. A readiness run (F-28 step 2) is green only when this
    returns without raising.

    Returns:
        ``(RunResult, prs)`` — the run's outcome and the PRs the recorder
        captured (empty unless one was opened).

    Raises:
        RehearsalError: the run reported ``pr_opened`` with no recorded
            PR, or the PR head lacks the remote's default-branch commit.
    """
    prs: list[PullRequest] = []

    def open_pr(pr: PullRequest) -> str:
        prs.append(pr)
        return f"https://github.com/rehearsal/fixture/pull/{len(prs)}"

    result = run_once(
        fixture.queue_path,
        fixture.profile_path,
        factory_root=Path(factory_root),
        session_runner=_RehearsalSeats(),
        open_pr=open_pr,
        now=now or (lambda: datetime.now(timezone.utc)),
    )
    if result.outcome == "pr_opened":
        if not prs:
            raise RehearsalError("run_once reported pr_opened but no PR was recorded")
        # rc 0 = the remote's advanced main is in the PR head; anything
        # else (rc 1 = not an ancestor, or a git error, with stderr
        # surfaced in the message) means the run built on a stale or
        # unknown base: not ready, by construction.
        proc = subprocess.run(
            ["git", "-C", str(fixture.remote), "merge-base", "--is-ancestor",
             fixture.remote_main, f"refs/heads/{prs[-1].head}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RehearsalError(
                f"the PR head {prs[-1].head} does not contain the remote's "
                f"default-branch commit {fixture.remote_main[:12]}: the run built "
                f"on a stale local checkout (git rc={proc.returncode}, "
                f"stderr: {proc.stderr.strip()[:200]})"
            )
    return result, prs
