"""F-79: the fix-round WIP store — validated work kept LOCALLY.

A fix round on an open PR that passes L0 and L1 and then does not
publish (review fails, or the round escalates) used to lose its work
with the run's clone. The parent now keeps it:

* **What is kept.** ONE commit the PARENT builds (``git commit-tree``
  under the factory identity): the validated head's tree, its single
  parent the PR's pushed head. Round commits a session made are never
  kept as history — so no session-authored identity, no intermediate
  commit (a reverted guarded change, an add-then-delete) and no
  rewritten ancestry can ride a WIP.
* **Where.** A ``git bundle`` of exactly ``parent..sha`` under
  ``<factory_root>/wip/<project>/<task>/<run-id>.bundle`` — the factory
  root, beside reports, traces and progress logs, where every other
  parent-written record lives. Never the PR branch (a head that has not
  passed review must never reach a PR the sweep could report ready),
  never the owner's source checkout (the factory only ever reads it),
  and outside every session's write boundary (a session writes its own
  run dir only).
* **The record.** ``links.wip = {sha, parent, run_id}`` on the task,
  written only by :meth:`factory.state.TaskQueue.record_wip`. The sha is
  the integrity check: a bundle that does not yield exactly that commit,
  with exactly that one parent, is not restored.
* **Order.** Preserve = bundle on disk (temp + rename), THEN the link.
  Clear = the link, THEN the files. A crash between either pair leaves
  an orphan file no link names — ignored, and removed by the next
  preserve or discard for the task. A link that names a missing or
  mismatching bundle restores nothing and is cleared.

The previous round's fix list (what the next in-run round would have
been handed) rides beside the bundle as ``<run-id>.findings.json`` —
parent-written, already scrubbed and escaped by the round that built it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from factory.gates import git as hardened_git
from factory.runs import GIT_HARDENING, RUN_ID_RE, project_component
from factory.state import atomic_install

__all__ = [
    "WIP_REF",
    "WipError",
    "discard",
    "load_findings",
    "preserve",
    "restore",
    "store_findings",
    "task_dir",
]

# The one ref name a WIP bundle carries (and the fetch asks for).
WIP_REF = "refs/factory/wip"

_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_FINDINGS_CAP = 200  # lines; a bound, never a semantics change


class WipError(Exception):
    """The WIP could not be preserved or restored; the message says why."""


def task_dir(factory_root: Path, project: str, task_id: str) -> Path:
    """``<factory_root>/wip/<project>/<task>`` — both components pass the
    rules every factory-root path builder uses (F-62 AC-3)."""
    return (Path(factory_root) / "wip" / project_component(project)
            / re.sub(r"[^\w.-]", "_", task_id))


def _bundle(factory_root: Path, project: str, task_id: str,
            run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id or ""):
        raise WipError(f"not a run id: {run_id!r}")
    return task_dir(factory_root, project, task_id) / f"{run_id}.bundle"


def _git(repo: Path, *args: str) -> str:
    return hardened_git(repo, *args).strip()


def preserve(repo: Path, *, factory_root: Path, project: str, task_id: str,
             run_id: str, head: str, parent: str,
             identity: tuple[str, ...]) -> str:
    """Keep ``head``'s tree as one parent-built commit on ``parent``.

    Args:
        repo: The run's work clone (holds both commits).
        head: The round's validated head (passed L0 and L1).
        parent: The PR branch's pushed head — ``head`` must descend
            from it and differ from it in content.
        identity: The ``-c user.name=… -c user.email=…`` git arguments
            of the factory identity the commit is built under.

    Returns:
        The WIP commit's sha (bundled; the caller records it).

    Raises:
        WipError: ``head`` does not descend from ``parent``, carries no
            change beyond it, or git could not build the bundle.
    """
    for name, value in (("head", head), ("parent", parent)):
        if not _SHA_RE.fullmatch(value or ""):
            raise WipError(f"{name} is not a commit sha: {value!r}")
    try:
        ancestor = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(repo), "merge-base",
             "--is-ancestor", parent, head], capture_output=True)
        if ancestor.returncode != 0:
            raise WipError(
                f"the validated head {head[:12]} does not descend from the "
                f"pushed head {parent[:12]}")
        tree = _git(repo, "rev-parse", "--verify", f"{head}^{{tree}}")
        if tree == _git(repo, "rev-parse", "--verify", f"{parent}^{{tree}}"):
            raise WipError(
                f"the validated head {head[:12]} holds no change beyond the "
                f"pushed head {parent[:12]}")
        sha = _git(
            repo, *identity, "commit-tree", tree, "-p", parent, "-m",
            f"{task_id}: fix-round work kept by the factory "
            f"(run {run_id}; passed L0 and L1, not yet reviewed)")
        target = _bundle(factory_root, project, task_id, run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        hardened_git(repo, "update-ref", WIP_REF, sha)
        tmp = target.with_name(target.name + f".tmp{os.getpid()}")
        try:
            hardened_git(repo, "bundle", "create", "--quiet", str(tmp),
                         WIP_REF, f"^{parent}")
            atomic_install(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
            subprocess.run(
                ["git", *GIT_HARDENING, "-C", str(repo), "update-ref", "-d",
                 WIP_REF], capture_output=True)
    except WipError:
        raise
    except Exception as exc:  # noqa: BLE001 — git/OS failure: one named error
        raise WipError(f"{type(exc).__name__}: {exc}"[:400]) from exc
    # Everything else kept for this task is superseded — older bundles,
    # and any fix list (it described an earlier commit; this round
    # stores its own when it ends). One WIP per task. Best-effort: a
    # file no link names is ignored anyway.
    for other in target.parent.iterdir():
        if other.name != target.name:
            with contextlib.suppress(OSError):
                other.unlink()
    return sha


def restore(repo: Path, *, factory_root: Path, project: str, task_id: str,
            wip: dict) -> str:
    """Fetch the recorded WIP commit into ``repo`` (which must already
    hold ``wip['parent']``) and return its sha.

    The commit is accepted only when the bundle yields EXACTLY the
    recorded sha and that commit has EXACTLY one parent, the recorded
    one — the shape :func:`preserve` builds and nothing else.

    Raises:
        WipError: The record is malformed, the bundle is missing or
            unreadable, or what it holds is not the recorded commit.
    """
    sha, parent = wip.get("sha"), wip.get("parent")
    if not (isinstance(sha, str) and _SHA_RE.fullmatch(sha)
            and isinstance(parent, str) and _SHA_RE.fullmatch(parent)):
        raise WipError("the recorded WIP is malformed")
    bundle = _bundle(factory_root, project, task_id,
                     str(wip.get("run_id") or ""))
    if not bundle.is_file():
        raise WipError(f"the kept bundle {bundle.name} is missing")
    try:
        hardened_git(repo, "fetch", "--quiet", "--no-tags", str(bundle),
                     WIP_REF)
        got = _git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        line = _git(repo, "rev-list", "--parents", "-n", "1", got).split()
    except Exception as exc:  # noqa: BLE001 — unreadable bundle
        raise WipError(
            f"the kept bundle could not be read ({type(exc).__name__}: "
            f"{exc})"[:400]) from exc
    if got != sha:
        raise WipError(
            f"the kept bundle holds {got[:12]}, not the recorded {sha[:12]}")
    if line[1:] != [parent]:
        raise WipError(
            f"the kept commit {sha[:12]} is not one commit on the recorded "
            f"pushed head {parent[:12]}")
    return sha


def discard(factory_root: Path, project: str, task_id: str) -> None:
    """Remove every kept file for the task. Idempotent, never raises:
    the link is the record — a leftover file no link names is ignored."""
    try:
        shutil.rmtree(task_dir(factory_root, project, task_id),
                      ignore_errors=True)
    except Exception:  # noqa: BLE001 — a bad component: nothing to remove
        pass


def store_findings(factory_root: Path, project: str, task_id: str,
                   run_id: str, findings: list[str]) -> None:
    """Write the round's fix list beside its bundle (parent-built lines,
    already scrubbed and escaped by the round)."""
    bundle = _bundle(factory_root, project, task_id, run_id)
    if not bundle.is_file():
        return
    lines = [str(line) for line in findings][:_FINDINGS_CAP]
    bundle.with_name(f"{run_id}.findings.json").write_text(
        json.dumps(lines, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def load_findings(factory_root: Path, project: str, task_id: str,
                  run_id: str) -> list[str]:
    """The fix list stored for the WIP's round; empty when none was
    written (the round died before it had one) or it does not parse."""
    try:
        path = _bundle(factory_root, project, task_id, run_id).with_name(
            f"{run_id}.findings.json")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, WipError):
        return []
    if not isinstance(data, list):
        return []
    return [line for line in data if isinstance(line, str)][:_FINDINGS_CAP]
