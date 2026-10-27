"""Read-only replay of the built-in sentinels over corpus diffs (F-33).

The corpus gate (the ruflo-protector idea, BUILD-ORDER F-33): a detector
change ships only if it stays silent on a corpus of known-good changes
and still fires on a corpus of known attacks. The corpora live in the
repo, under protected paths, so a session cannot edit the evidence that
judges its sentinels:

- ``corpus/benign/<pr-number>.patch`` — this repo's own merged PRs as
  ``gh pr diff`` output. Every built-in sentinel must pass every diff.
  PRs whose hits were CORRECT (seeded credential fixtures in tests) are
  left out, with each PR and its reason recorded in ``corpus/README.md``
  and the bootstrap ledger — PRs are never dropped to dodge a finding.
- ``corpus/attack/<sentinel>-<n>.patch`` — hand-written attacks, fresh
  files separate from the F-25 AC-3 seed fixtures (those stay inline
  literals in ``tests/test_f25.py``), so tuning a pattern to its own
  seed cannot hide a regression. Each file's name says the sentinel
  that must flag it.

``tests/test_f33.py`` is the gate CI runs on every PR; this module is
its engine, and the ``factory sentinels replay`` command (F-33 AC-4)
drives it by hand — one line per sentinel per diff. Both read only: no
file under the factory root or the corpus changes.
"""

from pathlib import Path

from factory.gates import changes_from_patch
from factory.sentinels import BUILTIN_SENTINELS, builtin_sentinel_findings

# The corpus directory, relative to the profile's repo — a protected
# path in this repo's own profile since F-33 (AC-5).
CORPUS_DIR = "corpus"


class ReplayError(Exception):
    """A replay could not run: missing corpus, missing patch, bad use."""


def patch_findings(patch_text: str, risk_globs: dict) -> list:
    """Scan one patch's text with the built-in sentinels.

    Args:
        patch_text: Unified-diff text (``gh pr diff`` / ``git diff``).
        risk_globs: The profile's ``paths.risk`` mapping — the lint
            sentinel is only real under a risk glob, so replay runs
            against the corpus's own production profile.

    Returns:
        Findings from :func:`builtin_sentinel_findings`.
    """
    return builtin_sentinel_findings(changes_from_patch(patch_text), risk_globs)


def resolve_replay_files(profile: dict, corpus: str | None, patches) -> list:
    """The patches to replay, each with the label its lines print.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        corpus: ``benign`` or ``attack`` to replay that whole corpus,
            or None for both — the CI-gate shape.
        patches: Explicit patch paths (argv strings), mutually exclusive
            with ``corpus``.

    Returns:
        ``(path, label)`` pairs, corpus files sorted by name.

    Raises:
        ReplayError: A named patch is missing, the profile has no
            ``repo.path``, a corpus directory is absent, or both corpora
            hold no patches.
    """
    if patches:
        files = [Path(p) for p in patches]
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            raise ReplayError(f"patch file(s) not found: {', '.join(missing)}")
        return [(p, str(p)) for p in files]
    repo = (profile.get("repo") or {}).get("path")
    if not repo:
        raise ReplayError(
            "the profile has no repo.path to locate the corpus under"
        )
    names = ("benign", "attack") if corpus is None else (corpus,)
    entries = []
    for name in names:
        corpus_dir = Path(repo).expanduser() / CORPUS_DIR / name
        if not corpus_dir.is_dir():
            raise ReplayError(f"no corpus directory at {corpus_dir}")
        entries += [
            (p, f"{CORPUS_DIR}/{name}/{p.name}")
            for p in sorted(corpus_dir.glob("*.patch"))
        ]
    if not entries:
        raise ReplayError("the corpus directories hold no patches")
    return entries


def replay_lines(profile: dict, entries) -> list:
    """One line per sentinel per diff: ``<label>: <sentinel>: <verdict>``.

    A hit appends ``(N finding[s])`` — the count, never the line: the
    no-echo rule of :mod:`factory.sentinels` travels with the sentinel,
    whatever prints its output.

    Raises:
        ReplayError: A patch cannot be read (missing, unreadable, or
            not UTF-8).
    """
    risk_globs = (profile.get("paths") or {}).get("risk") or {}
    lines = []
    for path, label in entries:
        # An unreadable or binary patch refuses the replay (a ValueError
        # is not an OSError — name it, or it tracebacks past the CLI).
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            raise ReplayError(f"{label}: cannot read patch: {exc}") from exc
        findings = patch_findings(text, risk_globs)
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.id] = counts.get(finding.id, 0) + 1
        for name in BUILTIN_SENTINELS:
            n = counts.get(name, 0)
            detail = f" ({n} finding{'s' if n != 1 else ''})" if n else ""
            lines.append(f"{label}: {name}: {'fail' if n else 'pass'}{detail}")
    return lines
