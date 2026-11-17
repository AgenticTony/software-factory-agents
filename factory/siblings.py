"""Sibling sites: review findings outside the diff (F-94).

JobFinderOS run 4 (#176) fixed one site of a defect class; three more
sites of the same class shipped. F-94 makes the reviewers look for them
— and lets a finding they mark as an older site of the class never
block the PR it was found on: such a finding is a *sibling*, kept in
the report and carried to F-82's follow-up draft instead.

A sibling needs BOTH conditions (:func:`factory.l2.mark_siblings`):
the reviewer's claim (``"sibling": true`` — the defect predates this
diff and is another site of the class) AND the parent's proof, made
here, that the finding's file is outside the diff. Either alone keeps
today's rules. The claim is needed because the file alone cannot tell
an old site from a regression the diff CAUSES at an unchanged caller —
reported where it shows up, outside the diff by nature, and the costlier
miss (a shipped break). The proof is needed because a claim alone must
never let an in-diff defect dodge its fail. Residual, named: a reviewer
that wrongly flags a diff-caused break on an untouched file makes it
non-blocking — that takes a mislabel; without the flag nothing changes.

The parent's half — :meth:`DiffFiles.is_sibling`, a fact it proves:

* The ``file`` must resolve to a path **tracked at the reviewed head**
  that the diff does **not** change.
* "Changed" is every path ``base...head`` names, a rename's OLD path
  and NEW path both — a finding on either is inside the diff.
* Resolution is fail-closed. ``./x`` and ``a/../x`` normalise; an
  absolute path counts only when it lies inside the reviewed clone; a
  path that climbs out (``../x``), names nothing tracked at the head
  (a typo, a case variant, a deleted file), or is a tracked symlink
  whose target is a changed path — or leaves the repo, or cannot be
  read — is NOT a sibling. Comparisons against the changed set are
  case-insensitive (macOS checkouts are). Whatever cannot be proven
  outside the diff keeps today's rules, and so does a finding that
  names no file.
* The reviewer's ``sibling: true`` on a file the diff changes lowers no
  bar: this half says no, so the finding is judged as before F-94.

The follow-up half (AC-4) is :func:`followup_scope_refusal`: a sibling's
file joins the draft's ``scope.paths`` only when it is a clean
repo-relative path tracked on the base branch that matches no protected
and no risk glob — every other file is left out, and the criterion says
why.

Pure helpers plus git reads; nothing here writes.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from factory.contract import scope_pattern_error
from factory.gates import diff_name_status, git, matches

__all__ = [
    "DiffFiles", "diff_files", "followup_scope_refusal", "normalise_path",
    "tracked_paths",
]

# A symlink chain longer than this is treated as unresolvable (git's
# own limit is 40; a cycle must never hang the review).
_MAX_LINK_HOPS = 40


def normalise_path(file: object, root: "Path | None" = None) -> "str | None":
    """A finding's ``file`` as a clean repo-relative posix path, or None.

    Strips whitespace and leading ``./``; collapses ``a/../b``. An
    absolute path is kept only when it lies inside ``root`` (the clone
    the reviewers read — its spelled and its resolved form both
    count). Anything that climbs out of the repo, is empty, or carries
    a NUL is None.
    """
    if not isinstance(file, str):
        return None
    text = file.strip()
    if not text or "\x00" in text:
        return None
    if text.startswith("/"):
        if root is None:
            return None
        for base in {str(Path(root)), str(Path(root).resolve())}:
            prefix = base.rstrip("/") + "/"
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        else:
            return None
    clean = posixpath.normpath(text)
    if clean in (".", "") or clean == ".." or clean.startswith("../") \
            or clean.startswith("/"):
        return None
    return clean


def tracked_paths(repo: Path, ref: str) -> "dict[str, str]":
    """Every path tracked in ``ref``'s tree -> its git mode."""
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", ref)
    tracked: dict[str, str] = {}
    for entry in out.split("\x00"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        tracked[path] = meta.split(" ", 1)[0]
    return tracked


@dataclass(frozen=True)
class DiffFiles:
    """What the parent knows about one reviewed diff (F-94 AC-3).

    ``changed``: every path ``base...head`` names (renames: old AND
    new). ``tracked``: every path in the head tree. ``links``: each
    tracked symlink -> its repo-relative target, or None when the
    target leaves the repo or cannot be read. ``root``: the clone the
    reviewers read (absolute ``file`` values are judged against it).
    """

    changed: frozenset
    tracked: frozenset
    links: dict = field(default_factory=dict)
    root: "Path | None" = None

    def is_sibling(self, file: object) -> bool:
        """True only when ``file`` provably names a tracked head path
        outside the diff — the module docstring's fail-closed rules."""
        rel = normalise_path(file, self.root)
        if rel is None or rel not in self.tracked:
            return False
        changed = {path.casefold() for path in self.changed}
        seen: set[str] = set()
        current: "str | None" = rel
        for _ in range(_MAX_LINK_HOPS):
            if current is None:
                return False  # a link out of the repo: not provable
            if current.casefold() in changed:
                return False
            if current in seen or current not in self.links:
                return current not in seen
            seen.add(current)
            current = self.links[current]
        return False


def _link_target(repo: Path, ref: str, path: str) -> "str | None":
    """A tracked symlink's target as a repo-relative path, or None."""
    try:
        target = git(repo, "cat-file", "blob", f"{ref}:{path}")
    except Exception:  # noqa: BLE001 — unreadable: not provable, fail closed
        return None
    if target.startswith("/"):
        return None
    return normalise_path(posixpath.join(posixpath.dirname(path), target))


def diff_files(repo: Path, base_ref: str, head_ref: str) -> DiffFiles:
    """Build the :class:`DiffFiles` of ``base...head`` in ``repo``.

    Raises:
        GateError: git failed or a ref does not resolve.
    """
    changed = frozenset(
        path
        for change in diff_name_status(repo, base_ref, head_ref)
        for path in (change.path, change.old_path)
        if path
    )
    tracked = tracked_paths(repo, head_ref)
    links = {path: _link_target(repo, head_ref, path)
             for path, mode in tracked.items() if mode == "120000"}
    return DiffFiles(changed=changed, tracked=frozenset(tracked),
                     links=links, root=Path(repo))


def followup_scope_refusal(file: object, base_tracked, profile: dict
                           ) -> "str | None":
    """Why a sibling's ``file`` may NOT join the follow-up's
    ``scope.paths`` (F-94 AC-4), or None when it may.

    The file must be a clean repo-relative path (:func:`normalise_path`
    with no clone root — an absolute path never qualifies), a valid
    scope path, tracked on the base branch (``base_tracked``), and
    match no protected and no risk glob of the profile. The reason is
    the words the criterion carries.
    """
    rel = normalise_path(file)
    if rel is None:
        return "not a clean path inside the repo"
    if scope_pattern_error(rel) is not None or any(
            char in rel for char in "*?"):
        # A file name carrying glob characters would widen the scope to
        # every path it matches: never a scope entry.
        return "not a valid scope path"
    if rel not in base_tracked:
        return "not tracked on the base branch"
    paths = profile.get("paths") or {}
    if matches(rel, paths.get("protected") or []):
        return "a protected path"
    for flag, globs in sorted((paths.get("risk") or {}).items()):
        if matches(rel, globs or []):
            return f"matches the {flag!r} risk glob"
    return None
