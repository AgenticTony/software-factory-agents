"""F-101 — `factory recover --accept` validates an escalated PR whose
branch exists only on GitHub.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f101.py::test_ac_N``
means a test function with exactly that name.

Three acceptance criteria (next-work-orders §F-101):
- AC-1 the fetched head is a REAL ref in the work clone
  (``refs/factory/accept/<branch>``) and the sha is proven REACHABLE from
  it (``merge-base --is-ancestor``), never merely present: a force-push
  past the reviewed sha, and a sha that only rode along in the object
  store, refuse with today's message BEFORE any gate session runs.
- AC-2 the clean clone checks out the exact sha with its file contents:
  the work clone is made WITHOUT the blob filter (a blobless clone
  cannot serve the clean clone's blob backfills — the partial-of-partial
  defect), and the accept ref is fetched into the clean clone explicitly
  (a clone copies only refs/heads, so the sha must not resolve by
  lazy-fetch luck).
- AC-3 the full leased accept round, end to end through the real entry
  point, reaches its verdict and the task advances on pass.

The two defects this fixes are recorded verbatim in BOOTSTRAP §F-96's
residual: ``ref '<sha>' not found in clone of <run dir>`` and the clean
clone's checkout dying on ``fatal: git upload-pack: …`` when it cannot
backfill blobs from a blobless file:// promisor. On git 2.50 a bare path
fetch degrades to unfiltered and both errors hide behind git's
lazy-fetch fallback — so these tests pin the CONSTRUCTION (the ref, the
reachability proof, the unfiltered work clone) instead of waiting for
git to fail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _support import f101
from _support.f72 import _git

from factory.approve import ApprovalError

NOT_REACHABLE = "is not reachable on"  # today's refusal message, kept
ONLY_BRANCH_FILE = "print('only on the reviewed head')\n"
LATER_FILE = "# pushed after the owner read the head\n"

# The mid-round rewrite's files: the dependency work PLUS a failing
# test. If the accept round ever judges the rewritten tip, L0 fails
# with findings; judged at the reviewed head it passes.
REWRITE_FILES = {
    "impl.py": "def answer():\n    return 42\n",
    "tests/test_feature.py": (
        "from impl import answer\n\n\ndef test_ac_1():\n"
        "    assert answer() == 42\n\n\ndef test_late():\n    assert False\n"
    ),
    "pyproject.toml": f101.DEP_PYPROJECT,
}


def _remote_sha(env, ref: str) -> str:
    return _git(env.remote, "rev-parse", "--verify", ref)


def _base_fixture(tmp_path: Path):
    """A source repo, a bare remote, and a slashed branch pushed to the
    remote only. Returns (repo, remote, branch, head, tip) — head is the
    reviewed commit, tip a later commit past it."""
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "impl.py").write_text("def answer():\n    return 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)],
                   check=True)
    scratch = tmp_path / "scratch"
    subprocess.run(["git", "clone", "-q", str(remote), str(scratch)],
                   check=True)
    _git(scratch, "config", "user.email", "fixture@example.com")
    _git(scratch, "config", "user.name", "Fixture")
    branch = "factory/feat/x/y"
    _git(scratch, "checkout", "-q", "-b", branch)
    (scratch / "onlybranch.py").write_text(ONLY_BRANCH_FILE)
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-m", "the reviewed head")
    head = _git(scratch, "rev-parse", "HEAD")
    _git(scratch, "push", "-q", "origin", branch)
    # a later commit: the branch tip moves past the reviewed sha
    (scratch / "later.py").write_text(LATER_FILE)
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-m", "pushed later")
    _git(scratch, "push", "-q", "origin", branch)
    tip = _git(scratch, "rev-parse", "HEAD")
    return repo, remote, branch, head, tip


# --------------------------------------------------------------------------
# AC-1 — the fetched head is a real ref; presence is not reachability
# --------------------------------------------------------------------------

def test_ac_1(tmp_path: Path, monkeypatch):
    import factory.orchestrator as orchestrator

    # (1) a branch pushed only to the remote is accepted at its exact
    # head, and the head lands as a REAL ref in the work clone —
    # refs/factory/accept/<branch>, a namespace nothing else writes.
    parked = f101.park(tmp_path)
    spy = f101.AcceptSpy(parked.env.repo)
    monkeypatch.setattr(orchestrator, "create_run", spy)
    assert parked.accept() == "in_review"
    accept_ref = f"refs/factory/accept/{parked.branch}"
    assert spy.work_accept_refs == {accept_ref: parked.head}

    # (2) a force-push that moved the branch past the reviewed sha
    # refuses with today's message, before any gate session runs.
    parked = f101.park(
        tmp_path / "force-past",
        rewrite_files={"impl.py": "def answer():\n    return 99\n"},
    )
    with pytest.raises(ApprovalError, match=NOT_REACHABLE):
        parked.accept()
    assert parked.env.task()["status"] == "needs_human"
    assert parked.ready == []

    # (3) a sha that exists in the work clone's object store but which
    # the branch does not reach refuses with the SAME message — today
    # the presence check lets it through and the round BURNS its L0/L1
    # gates on the unreviewed commit before anything stops it.
    parked = f101.park(
        tmp_path / "stray",
        stray_files={**f101.HEAD_FILES, "stray.py": "# on no branch\n"},
    )
    parked.head_answers = [parked.stray]  # the seam answers the stray sha
    with pytest.raises(ApprovalError, match=NOT_REACHABLE):
        parked.accept()
    assert parked.env.task()["status"] == "needs_human"
    assert parked.ready == []
    # refused at the fetch: no accept round report was ever stored
    assert list((parked.env.root / "reports" / "fixture").glob("*")) == []


# --------------------------------------------------------------------------
# AC-2 — the clean clone reaches the sha and its blobs, by construction
# --------------------------------------------------------------------------

def test_ac_2(tmp_path: Path):
    from factory.runs import create_run

    repo, remote, branch, head, tip = _base_fixture(tmp_path)

    froot = tmp_path / "factory-root"
    # the accept path's work clone: FULL, never blobless — it is the
    # local store the clean clone's blob backfills must resolve against.
    work = create_run("fixture", repo, factory_root=froot, ref="main",
                      full=True)
    no_filter = subprocess.run(
        ["git", "-C", str(work.root), "config", "--get",
         "remote.origin.partialclonefilter"],
        capture_output=True,
    )
    assert no_filter.returncode != 0, "the accept work clone is not filtered"
    # AC-1's fetch, exactly as the accept round runs it
    accept_ref = f"refs/factory/accept/{branch}"
    _git(work.root, "fetch", "--quiet", "--no-tags", str(remote),
         f"+refs/heads/{branch}:{accept_ref}")
    subprocess.run(
        ["git", "-C", str(work.root), "merge-base", "--is-ancestor", head,
         accept_ref],
        check=True,
    )
    # the remote is GONE from here on: building and checking out the
    # clean clone must need no network beyond the fetch above.
    remote.rename(tmp_path / "remote-gone.git")

    clean = create_run("fixture", work.root, factory_root=froot, ref=head,
                       local_fetch=[(work.root, f"+{accept_ref}:{accept_ref}")])
    assert _git(clean.root, "rev-parse", "HEAD") == head  # exact sha…
    assert not (clean.root / "later.py").exists()  # …never the branch tip
    # the sha's file contents arrived: a file only that commit has
    assert (clean.root / "onlybranch.py").read_text() == ONLY_BRANCH_FILE
    # the accept ref came along (it names the branch tip; the checkout
    # above proved the round still judges the exact sha, never the tip)
    assert _git(clean.root, "rev-parse", "--verify", accept_ref) == tip


# --------------------------------------------------------------------------
# AC-3 — the full leased accept round, end to end
# --------------------------------------------------------------------------

def test_ac_3(tmp_path: Path, monkeypatch):
    import factory.orchestrator as orchestrator

    parked = f101.park(tmp_path)
    spy = f101.AcceptSpy(parked.env.repo)
    monkeypatch.setattr(orchestrator, "create_run", spy)
    seats = f101.CleanSeats()
    assert parked.accept(session_runner=seats) == "in_review"
    task = parked.env.task()
    assert task["status"] == "in_review"
    assert parked.ready == [f101.PR_URL]  # the PR left draft
    # the round reached its L2 verdict through the scripted seats
    assert any(seat == "reviewer" for seat, *_ in seats.calls)
    # the release guard held: same tree as the accepted head, one commit
    released = _remote_sha(parked.env, f"refs/heads/{parked.branch}")
    assert released != parked.head
    assert (_remote_sha(parked.env, f"{released}^{{tree}}")
            == _remote_sha(parked.env, f"{parked.head}^{{tree}}"))
    # construction pins (the AC's mechanism, observable at the only
    # moment the clones exist):
    accept_ref = f"refs/factory/accept/{parked.branch}"
    assert spy.calls[0]["full"] is True  # no partial-of-partial
    assert spy.clean["ref"] == parked.head  # built AT the reviewed sha
    assert spy.clean["local_fetch"] == [
        (spy.work_root, f"+{accept_ref}:{accept_ref}")]
    assert spy.work_accept_refs == {accept_ref: parked.head}
    # accept run twice in a row: the second refuses (no longer parked)
    with pytest.raises(ApprovalError):
        parked.accept()


# --------------------------------------------------------------------------
# Hostile pass — the stale accept ref; the remote rewritten mid-round
# --------------------------------------------------------------------------

def test_stale_accept_ref_is_overwritten_never_trusted(tmp_path: Path):
    """An earlier accept's refs/factory/accept/<branch> is overwritten by
    the next fetch, never trusted: the ref always equals what the remote
    branch holds, and reachability is judged against THAT."""
    from factory.runs import create_run

    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "impl.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)],
                   check=True)
    scratch = tmp_path / "scratch"
    subprocess.run(["git", "clone", "-q", str(remote), str(scratch)],
                   check=True)
    _git(scratch, "config", "user.email", "fixture@example.com")
    _git(scratch, "config", "user.name", "Fixture")
    branch = "factory/feat/x/y"
    _git(scratch, "checkout", "-q", "-b", branch)
    (scratch / "a.py").write_text("a\n")
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-m", "one")
    first = _git(scratch, "rev-parse", "HEAD")
    _git(scratch, "push", "-q", "origin", branch)

    work = create_run("fixture", repo, factory_root=tmp_path / "f",
                      ref="main", full=True)
    accept_ref = f"refs/factory/accept/{branch}"
    # first accept fetch: the ref lands at the first head
    _git(work.root, "fetch", "--quiet", "--no-tags", str(remote),
         f"+refs/heads/{branch}:{accept_ref}")
    assert _git(work.root, "rev-parse", "--verify", accept_ref) == first
    # the branch advances; the second accept fetch with the SAME forced
    # refspec overwrites the stale value, never keeps it
    (scratch / "b.py").write_text("b\n")
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-m", "two")
    _git(scratch, "push", "-q", "origin", branch)
    second = _git(scratch, "rev-parse", "HEAD")
    _git(work.root, "fetch", "--quiet", "--no-tags", str(remote),
         f"+refs/heads/{branch}:{accept_ref}")
    assert _git(work.root, "rev-parse", "--verify", accept_ref) == second
    # reachability against the OVERWRITTEN ref: the first head is still
    # an ancestor (accepted); a rewrite that drops it refuses.
    subprocess.run(["git", "-C", str(work.root), "merge-base", "--is-ancestor",
                    first, accept_ref], check=True)
    _git(scratch, "checkout", "-q", "-B", "rewrite", "main")
    (scratch / "c.py").write_text("c\n")
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-m", "rewritten")
    subprocess.run(["git", "-C", str(scratch), "push", "-q", "--force",
                    "origin", f"rewrite:{branch}"], check=True)
    _git(work.root, "fetch", "--quiet", "--no-tags", str(remote),
         f"+refs/heads/{branch}:{accept_ref}")
    refused = subprocess.run(
        ["git", "-C", str(work.root), "merge-base", "--is-ancestor",
         first, accept_ref],
        capture_output=True,
    )
    assert refused.returncode != 0, "the stale head must not verify"


def test_remote_rewritten_between_fetch_and_clone(tmp_path: Path,
                                                  monkeypatch):
    """The remote is force-pushed while the accept round builds its clean
    clone. The round validates the REVIEWED sha (from the work clone,
    never the new tip) and the release's lease then refuses — the new
    tip is never validated, never overwritten."""
    import factory.orchestrator as orchestrator
    from factory.runs import create_run

    parked = f101.park(tmp_path)
    state = {"work_root": None, "pushed": False}

    def spy(project, source, **kwargs):
        if (state["work_root"] is not None
                and Path(source) == state["work_root"] and not state["pushed"]):
            state["pushed"] = True  # the rewrite lands mid-round, here
            scratch = parked.env.tmp / "rewrite-clone"
            subprocess.run(
                ["git", "clone", "-q", str(parked.env.remote), str(scratch)],
                check=True)
            _git(scratch, "config", "user.email", "fixture@example.com")
            _git(scratch, "config", "user.name", "Fixture")
            _git(scratch, "checkout", "-q", "-B", "rewrite", parked.base)
            f101._write(scratch, REWRITE_FILES)
            _git(scratch, "add", "-A")
            _git(scratch, "commit", "-m", "the rewrite")
            rewritten = _git(scratch, "rev-parse", "HEAD")
            subprocess.run(
                ["git", "-C", str(scratch), "push", "-q", "--force",
                 "origin", f"rewrite:{parked.branch}"], check=True)
            state["rewritten"] = rewritten
        run = create_run(project, source, **kwargs)
        if state["work_root"] is None and Path(source) == Path(parked.env.repo):
            state["work_root"] = run.root
        return run

    monkeypatch.setattr(orchestrator, "create_run", spy)
    with pytest.raises(ApprovalError, match="lease|moved|stale") as exc:
        parked.accept()
    assert "FAILED" not in str(exc.value), (
        "the round judged the rewritten tip, not the reviewed sha")
    # the rewritten tip still holds the remote: nothing was overwritten
    assert _remote_sha(parked.env,
                       f"refs/heads/{parked.branch}") == state["rewritten"]
    assert parked.env.task()["status"] == "needs_human"
    assert parked.ready == []


def test_remote_advanced_past_the_reviewed_head(tmp_path: Path):
    """The branch ADVANCED past the sha the owner reviewed (a plain new
    commit — the head is still an ancestor, so reachability holds and
    the accept round RUNS). The round judges the reviewed head exactly:
    the later commit's failing test never reaches L0, and the release's
    lease refuses because the remote no longer holds the accepted sha.
    This is the arm that dies if the clean clone is ever made at the
    branch tip instead of the sha."""
    # the advanced tip adds a FAILING test: judged at the tip, L0 fails
    # with findings; judged at the reviewed head, the round passes.
    parked = f101.park(
        tmp_path, advance_files={
            **f101.HEAD_FILES,
            "tests/test_late.py": "def test_late():\n    assert False\n",
        })
    with pytest.raises(ApprovalError, match="lease|moved|stale") as exc:
        parked.accept()
    assert "FAILED" not in str(exc.value), (
        "the round judged the advanced tip, not the reviewed sha")
    assert _remote_sha(parked.env,
                       f"refs/heads/{parked.branch}") == parked.advanced
    assert parked.env.task()["status"] == "needs_human"
    assert parked.ready == []
