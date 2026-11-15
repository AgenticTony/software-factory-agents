"""F-73 test support: ready clones — the git identity and setup seams.

The F-38/F-72 split holds: git, the clone manager, the queue, the L0/L1
sandboxes (srt, real) and the push to the local bare remote are real;
only the model seats and the CI/gh seams are scripted. What this adds:

* ``Env73`` — Env72 whose fixture repo ignores the directory the
  scripted setup creates (``.venv2/``), so a setup-built venv in the run
  clone reads clean to git exactly as the AC's "gitignored venv" arm
  needs;
* ``GitSeats`` — Seats whose implementer writes REAL git commits in the
  run clone (arbitrary commands, an environment for the
  ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` arms), recording at each session
  start what the clone holds (the repo-local identity the parent set,
  the setup-created venv marker) and after its commands the head's
  author/committer identity and tree — the values the pushed history is
  judged against;
* the scripted ``commands.setup`` values: one that builds a gitignored
  venv (the green arm), one that exits non-zero, one that leaves an
  un-ignored file behind.

No JobFinderOS content here — shapes only.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from _support.f72 import Env72, Seats
from factory.session import SessionRecord

__all__ = [
    "DIRTY_SETUP", "FAIL_SETUP", "GitSeats", "PLAIN_SETUP", "VENV_MARKER",
    "VENV_SETUP", "git_out", "git_read", "identities_of", "IDENT_SUFFIX",
]

# The factory identity the orchestrator commits with (orchestrator.
# _IDENTITY): name and email exactly.
FACTORY_NAME = "Software Factory"
FACTORY_EMAIL = "factory@localhost"
IDENT_SUFFIX = f"{FACTORY_NAME} <{FACTORY_EMAIL}>"

VENV_MARKER = "marker-from-setup"
VENV_SETUP = (f"{shlex.quote(sys.executable)} -m venv .venv2 "
              f"&& touch .venv2/{VENV_MARKER}")
FAIL_SETUP = "echo boom-setup >&2; exit 3"
DIRTY_SETUP = "touch stray-untracked.txt"
PLAIN_SETUP = "true"


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True).stdout.strip()


def git_read(repo: Path, *args: str) -> str:
    """git stdout, empty on a non-zero exit (an unset config key)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def identities_of(repo: Path, base: str, head: str) -> list[tuple[str, str, str, str]]:
    """Every (author name, author email, committer name, committer email)
    in base..head, one tuple per commit, HEAD order — the pushed history
    the identity tests judge."""
    out = git_out(repo, "log", "--format=%an%x1f%ae%x1f%cn%x1f%ce",
                  f"{base}..{head}")
    return [tuple(line.split("\x1f")) for line in out.splitlines() if line]


class Env73(Env72):
    """Env72 whose fixture repo also ignores ``.venv2/`` — committed onto
    the fixture base and pushed to the bare remote, so the run clone
    inherits the ignore and a setup-created venv never reads dirty."""

    def __init__(self, tmp_path: Path, **kwargs):
        super().__init__(tmp_path, **kwargs)
        gi = self.repo / ".gitignore"
        gi.write_text(gi.read_text() + ".venv2/\n")
        git_out(self.repo, "add", "-A")
        git_out(self.repo, "commit", "-m", "ignore the setup venv")
        subprocess.run(
            ["git", "-C", str(self.repo), "push", "-q", str(self.remote),
             "HEAD:refs/heads/main"], check=True, capture_output=True)
        self.base_sha = git_out(self.repo, "rev-parse", "HEAD")


class GitSeats(Seats):
    """Seats whose implementer writes real commits in the run clone.

    ``git_commands``: shell snippets run in the clone AFTER the round's
    files are written (the session's own commits). ``git_env``: extra
    environment for them (the ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` arms).

    Recorded per implementer call, at session start: ``clone_seen[i]``
    holds the repo-local identity the parent configured and whether the
    setup-created venv marker is present — read before the seat writes
    anything. After its commands: ``commit_ids[i]`` holds the head's
    ``(an, ae, cn, ce)`` and tree. ``impl_prompts``: the implementer
    prompts, in order.
    """

    def __init__(self, *args, git_commands: "list[str] | None" = None,
                 git_env: "dict | None" = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.git_commands = list(git_commands or [])
        self.git_env = dict(git_env or {})
        self.clone_seen: list[dict] = []
        self.commit_ids: list[dict] = []
        self.impl_prompts: list[str] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        if seat != "implementer":
            return super().__call__(seat, prompt, run_dir, profile, **kwargs)
        run_dir = Path(run_dir)
        with self.lock:
            self.calls.append((seat, prompt, run_dir, kwargs))
            round_no = sum(1 for s, *_ in self.calls if s == "implementer")
            self.round_reviews = 0
        self.impl_prompts.append(prompt)
        # What the clone holds the moment the session starts — the F-79
        # ObservingSeats precedent: read before the seat writes anything.
        self.clone_seen.append({
            "user.name": git_read(run_dir, "config", "--local", "user.name"),
            "user.email": git_read(run_dir, "config", "--local", "user.email"),
            "venv_marker": (run_dir / ".venv2" / VENV_MARKER).exists(),
        })
        self._script(run_dir, round_no)
        env = {**os.environ, **self.git_env}
        for cmd in self.git_commands:
            proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=run_dir, env=env,
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                raise AssertionError(
                    f"scripted git command failed: {cmd}\n{proc.stderr}")
        fields = git_out(
            run_dir, "log", "-1",
            "--format=%an%x1f%ae%x1f%cn%x1f%ce").split("\x1f")
        if len(fields) != 4:  # a name carrying the separator byte
            fields = [fields[0], fields[1], fields[-2], fields[-1]] \
                if len(fields) > 4 else fields + [""] * (4 - len(fields))
        an, ae, cn, ce = fields[:4]
        self.commit_ids.append({
            "an": an, "ae": ae, "cn": cn, "ce": ce,
            "tree": git_out(run_dir, "rev-parse", "HEAD^{tree}"),
        })
        return SessionRecord(
            seat=seat, status="completed", num_turns=self.implementer_turns,
            model="stub-model",
            tokens={"input_tokens": 120, "output_tokens": 40},
            usage={"input": 120, "cache_read": 0, "cache_write": 0,
                   "output": 40},
            result=self.impl_result,
        )
