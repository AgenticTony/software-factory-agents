"""F-101 support: a hand-minted escalation park whose PR branch exists
ONLY on the bare remote — the normal accept shape once a run's clones
are gone (the F-72 crash-window state: exactly the links, status and
attempt record a real escalated run leaves) — plus a ``create_run`` spy
that observes the accept round's two clones before cleanup deletes them.

The branch head carries the implementation, the contract test and the
new dependency, so the accept round's L0/L1 PASS at the head with the
escalation class waived — a successful accept needs nothing else.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from _support.f63 import ledger_approve
from _support.f72 import IMPL_GOOD, TEST_SOURCE, Env72, _git, _task
from factory.state import TaskQueue

PR_URL = "https://github.com/fixture/fixture/pull/1"

# The slashed name is deliberate (F-101 hostile pass): the accept's ref
# and refspec must carry path characters through unchanged.
BRANCH = "factory/feat/x/y"

DEP_PYPROJECT = (
    "[project]\nname = 'fixture'\nversion = '0.1'\ndependencies = ['requests']\n"
)

HEAD_FILES = {
    "impl.py": IMPL_GOOD,
    "tests/test_feature.py": TEST_SOURCE,
    "pyproject.toml": DEP_PYPROJECT,
}


def dependency_task(task_id: str = "T-1") -> dict:
    """The f72 dependency task: scope names pyproject.toml, so the added
    dependency is in-scope work that escalates (and is waivable)."""
    return _task(task_id, scope={"paths": ["impl.py", "tests/test_feature.py",
                                           "pyproject.toml"]})


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@dataclass
class Parked:
    """A minted park plus the shas the scenarios need."""

    env: Env72
    branch: str
    head: str  # the reviewed PR head, on the remote branch
    base: str  # the repo's base commit
    rewritten: str | None = None  # force-push replacement tip, when asked
    advanced: str | None = None  # a tip pushed past the head, when asked
    stray: str | None = None  # a source-repo sha no remote branch reaches
    pr_url: str = PR_URL
    ready: list[str] = field(default_factory=list)  # pr_ready() calls

    def __post_init__(self):
        # The PR head the seam answers; tests move it with .pop/.append.
        self.head_answers = [self.head]

    def accept(self, **overrides):
        """Drive `factory recover --accept` on this park (scripted seats,
        the PR seams answered from this object's shas)."""
        from factory.approve import recover_task

        def pr_head(url: str) -> str:
            return self.head_answers[-1]

        kwargs = {
            "accept": True,
            "evidence": "read the diff; requests is the documented client",
            "factory_root": self.env.root,
            "pr_head": pr_head,
            "pr_ready": self.ready.append,
            "session_runner": CleanSeats(),
            "confirm": lambda question: True,
            "echo": lambda text: None,
        }
        kwargs.update(overrides)
        return recover_task(TaskQueue(self.env.queue_path), "T-1", **kwargs)


class CleanSeats:
    """Clean-pass L2 seats for the accept round (no findings → no
    verifier fan-out); keeps the seat prompts for assertions."""

    def __init__(self):
        from _support.f72 import Seats

        self._seats = Seats()
        self.calls = self._seats.calls

    def __call__(self, *args, **kwargs):
        return self._seats(*args, **kwargs)


def park(tmp_path: Path, *, branch: str = BRANCH,
         rewrite_files: dict[str, str] | None = None,
         stray_files: dict[str, str] | None = None,
         advance_files: dict[str, str] | None = None) -> Parked:
    """Mint a needs_human new_dependency park, branch remote-only.

    Args:
        tmp_path: The test's tmp dir (the fixture project lives under it).
        branch: The escalation branch name (remote-only after this).
        rewrite_files: When given, force-push a REPLACEMENT tip built
            from base with these files — a commit descending from
            neither the head nor main, so the branch no longer reaches
            ``head`` (the force-past-the-reviewed-sha world).
        stray_files: When given, also commit these on an ``other``
            branch of the SOURCE repo. Every refs/heads/* is cloned, so
            the sha lands in the accept work clone's object store while
            the escalation branch never reaches it (the object-store
            sha on no branch the spec refuses).
        advance_files: When given, push a SECOND commit on top of the
            head with these files — the branch still REACHES the head
            but its tip has moved past it (the remote-advanced world:
            the round must judge the reviewed head, never the tip).

    Returns:
        The :class:`Parked` fixture (env, shas).
    """
    env = Env72(tmp_path, tasks=[dependency_task()])
    stray = None
    if stray_files:
        _git(env.repo, "checkout", "-q", "-b", "other")
        _write(env.repo, stray_files)
        stray = _commit_all(env.repo, "T-other: stray work")
        _git(env.repo, "checkout", "-q", "main")
    scratch = tmp_path / "scratch"
    subprocess.run(["git", "clone", "-q", str(env.remote), str(scratch)],
                   check=True)
    _git(scratch, "config", "user.email", "fixture@example.com")
    _git(scratch, "config", "user.name", "Fixture")
    _git(scratch, "checkout", "-q", "-b", branch)
    _write(scratch, HEAD_FILES)
    head = _commit_all(scratch, "T-1: the dependency work")
    _git(scratch, "push", "-q", "origin", branch)
    advanced = None
    if advance_files:
        _write(scratch, advance_files)
        advanced = _commit_all(scratch, "T-1: pushed past the reviewed head")
        _git(scratch, "push", "-q", "origin", branch)
    rewritten = None
    if rewrite_files:
        _git(scratch, "checkout", "-q", "-B", "rewrite", env.base_sha)
        _write(scratch, rewrite_files)
        rewritten = _commit_all(scratch, "T-1: rewritten history")
        subprocess.run(
            ["git", "-C", str(scratch), "push", "-q", "--force",
             "origin", f"rewrite:{branch}"],
            check=True,
        )
    queue = TaskQueue(env.queue_path)
    task = queue.get("T-1")
    task["status"] = "needs_human"
    task["attempts"] = [{
        "run_id": "20261119T080000Z-f10101",
        "slice_id": "T-1",
        "started": "2026-11-19T07:00:00Z",
        "outcome": "escalated",
        "escalation": "new_dependency",
        "pr": PR_URL,
        "prompt_versions": {},
        "skills": [],
    }]
    task["links"] = {"pr": PR_URL, "branch": branch, "escalation_pr": True}
    queue.save()
    ledger_approve(env.root, env.queue_path)
    return Parked(env=env, branch=branch, head=head, base=env.base_sha,
                  rewritten=rewritten, advanced=advanced, stray=stray)


class AcceptSpy:
    """Wraps ``factory.orchestrator.create_run`` for one accept round.

    The first call (source = the project repo) is the WORK clone; the
    call whose source IS that work clone is the CLEAN clone. The
    escalation branch has been fetched by then, so the spy reads the
    work clone's ``refs/factory/accept/*`` at exactly that moment —
    cleanup deletes the clone before the test could look. Attach with
    ``monkeypatch.setattr(orchestrator, "create_run", spy)``.
    """

    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self.calls: list[dict] = []
        self.work_root: Path | None = None
        self.work_accept_refs: dict[str, str] | None = None
        self.clean: dict | None = None

    def __call__(self, project: str, source, **kwargs):
        from factory.runs import create_run  # the real fn, never the patch

        call = {"source": Path(source), "ref": kwargs.get("ref"),
                "full": kwargs.get("full"),
                "local_fetch": kwargs.get("local_fetch")}
        self.calls.append(call)
        if self.work_root is not None and Path(source) == self.work_root:
            self.clean = call
            out = subprocess.run(
                ["git", "-C", str(self.work_root), "for-each-ref",
                 "--format=%(refname) %(objectname)", "refs/factory/accept/"],
                capture_output=True, text=True,
            ).stdout
            self.work_accept_refs = {
                parts[0]: parts[1]
                for parts in (line.split() for line in out.splitlines())
                if len(parts) == 2
            }
        run = create_run(project, source, **kwargs)
        if self.work_root is None and call["source"] == self.repo:
            self.work_root = run.root
        return run
