"""Seat prompts — BUILD-ORDER F-09.

The factory's agent seats run project-agnostic prompt *files* assembled
with per-run project specifics. The files adapt mattpocock/skills @
``49dd158`` (MIT — vendored under ``prompts/vendor/mattpocock-skills/`` with
the license and the source SHA); they are pinned by content hash in
``tests/test_f09.py`` (the JobFinderOS pattern), never loaded as live skills.

Assembly rules (VALIDATOR-SPEC L2):

* The **implementer** gets the project's mechanics (test command, protected
  paths) and *never* the project's ``standards`` files — standards live with
  the reviewer, who judges the diff with the least context pressure, not with
  the implementer, who has the most.
* The **reviewer** gets the base prompt — axes, smell baseline, mandatory
  checklist, test-quality checks — plus every ``standards`` file's content,
  or an explicit "baseline only" statement when the project documents none.
* The **verifier** (F-38) gets the same standards assembly as the reviewer:
  it votes reviewer findings in or out, and judging whether a finding is
  "style-only" or cites a real documented standard needs the same texts.

Prompts are read orchestrator-side only: inside the sandbox the repo
(including ``prompts/``) is unreadable by design (F-06), so the orchestrator
assembles, hashes and hands the text to the session via its options — never
a path.
"""

from __future__ import annotations

import hashlib
import importlib.resources
from pathlib import Path

# F-51 (I-1): the seat prompts ship inside the package
# (factory/data/prompts), resolved through importlib.resources — the
# same one resolver as the schemas and the toolkit (no __file__ escapes,
# no CWD-relative reads).
PROMPTS_DIR = Path(
    str(importlib.resources.files("factory").joinpath("data", "prompts"))
)
IMPLEMENTER_FILE = "implementer.md"
REVIEWER_FILE = "reviewer.md"
VERIFIER_FILE = "verifier.md"


class PromptError(Exception):
    """A seat prompt cannot be assembled: missing file, or a ``standards``
    entry naming a file that doesn't exist in the project repo."""


def prompt_hash(text: str) -> str:
    """The prompt content hash (sha256 hex) recorded per attempt in
    ``prompt_versions`` and pinned per file in the tests."""
    return hashlib.sha256(text.encode()).hexdigest()


def load_prompt(name: str) -> str:
    """Read a prompt file from ``prompts/``.

    Args:
        name: File name, e.g. ``IMPLEMENTER_FILE``.

    Returns:
        The file's text.

    Raises:
        PromptError: If the file is missing or unreadable.
    """
    path = PROMPTS_DIR / name
    try:
        return path.read_text()
    except OSError as exc:
        raise PromptError(f"seat prompt {path} cannot be read: {exc}") from exc


def _repo_root(profile: dict) -> Path:
    """The project's repo root: ``repo.path`` expanded (real profiles write
    ``~/Desktop/...``) and resolved, so containment checks compare real
    filesystem locations, not spellings."""
    repo_path = (profile.get("repo") or {}).get("path") or "."
    return Path(repo_path).expanduser().resolve()


def _standards_path(profile: dict, rel: str) -> Path:
    """Resolve one ``standards`` entry to a path INSIDE the project repo.

    Standards are repo-relative (TASK-CONTRACT §5). An absolute entry is a
    contract violation, and an entry that escapes the repo — directly via
    ``..`` or through a symlink — would let a profile read any file on the
    machine into a prompt sent to the model provider. Both refuse.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        rel: The repo-relative standards path.

    Returns:
        The resolved path, guaranteed inside the repo root.

    Raises:
        PromptError: If the entry is absolute or resolves outside the repo.
    """
    if Path(rel).is_absolute():
        raise PromptError(
            f"standards entry {rel!r} must be repo-relative, not absolute — "
            "fix project.yaml's 'standards' list"
        )
    root = _repo_root(profile)
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root):
        raise PromptError(
            f"standards entry {rel!r} resolves outside the project repo "
            f"({root}) — fix project.yaml's 'standards' list"
        )
    return resolved


def assemble_implementer_prompt(profile: dict | None = None) -> str:
    """The implementer's system prompt: the base file plus this project's
    mechanics. Deliberately excludes the project's ``standards`` files
    (VALIDATOR-SPEC L2: standards live with the reviewer) — the task
    specifics reach the implementer through the contract (the user prompt),
    not the system prompt.

    Args:
        profile: The loaded ``project.yaml`` mapping (optional in tests).

    Returns:
        The assembled system prompt text.
    """
    prompt = load_prompt(IMPLEMENTER_FILE)
    profile = profile or {}
    commands = profile.get("commands") or {}
    protected = (profile.get("paths") or {}).get("protected") or []
    lines: list[str] = []
    if commands.get("test"):
        lines.append(f"- The project's tests run with: `{commands['test']}`")
    if commands.get("test_ref"):
        lines.append(f"- One named test runs with: `{commands['test_ref']}`")
    if protected:
        joined = ", ".join(f"`{pattern}`" for pattern in protected)
        lines.append(f"- Protected paths, never to be modified: {joined}")
    if lines:
        prompt += "\n\n## This project\n\n" + "\n".join(lines) + "\n"
    return prompt


def assemble_reviewer_prompt(profile: dict | None = None) -> str:
    """The reviewer's system prompt: the base file (two axes, smell
    baseline, mandatory checklist, test-quality checks) plus the content of
    every project ``standards`` file — or an explicit statement that the
    project documents none, so the baseline runs alone (VALIDATOR-SPEC L2).

    Args:
        profile: The loaded ``project.yaml`` mapping (optional in tests).

    Returns:
        The assembled system prompt text.

    Raises:
        PromptError: If a ``standards`` entry is absolute, resolves outside
            the project repo, or names a file that doesn't exist or can't
            be read. A reviewer silently judging without its standards is
            worse than a loud refusal — and a standards list that can reach
            arbitrary files would put them in a prompt sent to the model.
    """
    prompt = load_prompt(REVIEWER_FILE)
    standards = (profile or {}).get("standards") or []
    if not standards:
        return (
            prompt
            + "\n\n## Project standards\n\n"
            "This project documents no standards files: the smell baseline "
            "above applies alone, and the report must say so.\n"
        )
    sections: list[str] = []
    for rel in standards:
        # standards is empty when profile is None, so the loop body only
        # ever runs with a loaded profile (stated for the checker).
        assert profile is not None
        path = _standards_path(profile, rel)
        if not path.is_file():
            raise PromptError(
                f"standards file {rel!r} does not exist under the project repo "
                f"({_repo_root(profile)}): fix project.yaml's 'standards' list"
            )
        try:
            text = path.read_text()
        except OSError as exc:
            raise PromptError(
                f"standards file {rel!r} cannot be read: {exc}"
            ) from exc
        sections.append(f"### {rel}\n\n{text.rstrip()}\n")
    return prompt + "\n\n## Project standards\n\n" + "\n".join(sections)


def assemble_verifier_prompt(profile: dict | None = None) -> str:
    """The verifier's system prompt (F-38): the base file (vote rules, drop
    cases, tags) plus the same standards assembly the reviewer gets — the
    verifier judges whether a finding is style-only or cites a real
    documented standard, which needs the standards texts in hand.

    Args:
        profile: The loaded ``project.yaml`` mapping (optional in tests).

    Returns:
        The assembled system prompt text.

    Raises:
        PromptError: If a ``standards`` entry is absolute, resolves outside
            the project repo, or names a file that doesn't exist or can't
            be read (same rule as the reviewer: a verifier silently voting
            without its standards would be worse than a loud refusal).
    """
    prompt = load_prompt(VERIFIER_FILE)
    standards = (profile or {}).get("standards") or []
    if not standards:
        return (
            prompt
            + "\n\n## Project standards\n\n"
            "This project documents no standards files: judge style-only "
            "against the smell baseline the reviewers were given.\n"
        )
    sections: list[str] = []
    for rel in standards:
        # standards is empty when profile is None, so the loop body only
        # ever runs with a loaded profile (stated for the checker).
        assert profile is not None
        path = _standards_path(profile, rel)
        if not path.is_file():
            raise PromptError(
                f"standards file {rel!r} does not exist under the project repo "
                f"({_repo_root(profile)}): fix project.yaml's 'standards' list"
            )
        try:
            text = path.read_text()
        except OSError as exc:
            raise PromptError(
                f"standards file {rel!r} cannot be read: {exc}"
            ) from exc
        sections.append(f"### {rel}\n\n{text.rstrip()}\n")
    return prompt + "\n\n## Project standards\n\n" + "\n".join(sections)


def prompt_versions(profile: dict | None = None, *, reviewers: int = 2) -> dict[str, str]:
    """Seat -> content hash of the assembled prompt, for the attempt record
    (``prompt_versions``, BUILD-ORDER F-10 AC-6, F-38 AC-5) and credit
    assignment in F-19. Hashing the *assembled* prompt means a change to the
    base file or to what assembly injects both show up as a new version.

    The reviewer fan-out (F-38) gets one entry per session —
    ``reviewer-1`` .. ``reviewer-N`` — so every session's prompt hash is
    recorded even though the fan-out shares one assembled prompt; the
    verifier gets its own entry. A reviewer count below 1 is treated as 1
    (the config layer refuses it; this stays total for direct callers).

    Args:
        profile: The loaded ``project.yaml`` mapping (optional in tests).
        reviewers: How many reviewer sessions the fan-out runs.

    Returns:
        ``{"implementer": <sha256>, "reviewer-1": <sha256>, …,
        "verifier": <sha256>}``.
    """
    versions = {"implementer": prompt_hash(assemble_implementer_prompt(profile))}
    reviewer_hash = prompt_hash(assemble_reviewer_prompt(profile))
    for number in range(1, max(1, int(reviewers)) + 1):
        versions[f"reviewer-{number}"] = reviewer_hash
    versions["verifier"] = prompt_hash(assemble_verifier_prompt(profile))
    return versions
