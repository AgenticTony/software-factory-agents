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
* The reviewer **seats** (F-94) differ by lens, never by base: seat 1 (and
  every odd seat) gets exactly the assembled reviewer prompt — contract
  conformance plus the standards axis — and seat 2 (every even seat) the
  same text plus :data:`DEFECT_HUNT_LENS`. A one-seat fan-out gets both.
  :func:`reviewer_seat_prompts` is the one place the assignment is made;
  every review site (round, final, revalidate, eval) and every per-seat
  retry asks it, and :func:`prompt_versions` hashes each seat's own text.
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


# F-103: the severity rubric — ONE shared block, defined here and
# included byte-for-byte in every reviewer seat prompt (the final full
# review runs on the same assembled reviewer prompts) and in the verifier
# prompt. Pinned by literal in tests/test_f103.py — editing it here
# without the pin fails the test. The block lives in the SYSTEM prompt,
# above the untrusted contract, diff and finding text a seat is handed in
# its user prompt, so injected "rate everything minor" text in a diff
# competes with the rubric, not replaces it; whether a vote can actually
# move a severity is enforced by l2's two-vote rule, never by the prompt.
# The implementer never sees it: it does not rate findings.
SEVERITY_RUBRIC = (
    "## The severity rubric\n"
    "\n"
    "Whether a finding blocks depends on what it can cost production, "
    "not on how the review felt. Rate every finding with this table:\n"
    "\n"
    "| Severity | Bucket | Test | Route |\n"
    "|---|---|---|---|\n"
    "| `blocking` | A | Certain on the main path: wrong output, data "
    "loss, an acceptance criterion not met, or a broken boundary "
    "(sandbox, push, budget, approval, secrets, auth, payments, verdict "
    "integrity) | fails the round, fix |\n"
    "| `major` | A | The same costs, reachable under an input or state "
    "the finding names | fails the round, fix |\n"
    "| `minor` | B | Real and wrong, but visible and caught by a human "
    "before harm: a misleading message, a noisy follow-up, a slow path, "
    "a weak test | PR body + F-82 follow-up, never blocks |\n"
    "| `note` | C | Polish: naming, comments, structure, style | PR body "
    "within the nit budget, else cut |\n"
    "\n"
    "A `blocking` or `major` finding states its failure scenario in "
    "`text`: the input or state, and the wrong result. \"Could be "
    "cleaner\" is never A.\n"
)


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
    ``~/Developer/...``) and resolved, so containment checks compare real
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
    baseline, mandatory checklist, test-quality checks), the F-103
    severity rubric (:data:`SEVERITY_RUBRIC`, one shared block — the
    final review runs on this same assembled prompt), plus the content of
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
            + "\n\n" + SEVERITY_RUBRIC
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
    return (
        prompt
        + "\n\n" + SEVERITY_RUBRIC
        + "\n\n## Project standards\n\n" + "\n".join(sections)
    )


# F-94: the defect-hunt lens — what the second reviewer seat adds to the
# base prompt (JobFinderOS run 4, #176: both seats got the identical
# prompt, so the second mostly duplicated the first, and neither looked
# for the fixed defect's other sites). Pinned by literal in
# tests/test_f94.py — editing it here without the pin fails the test.
# F-96 moved the "Draw the line" sibling paragraph out of the lens and
# into the base reviewer prompt (reviewer.md), so EVERY seat carries it
# word for word — the lens keeps only the hunt itself, plus the pointer
# to the rule it must not restate (a second copy in the hunt seat is
# exactly the drift the move removes).
DEFECT_HUNT_LENS = (
    "## Your lens: hunt the defect beyond the hunk\n"
    "\n"
    "On top of the review above, hunt for defects the diff causes or "
    "leaves behind:\n"
    "\n"
    "- Read every hunk and its enclosing function, and ask what input or "
    "state makes each line wrong.\n"
    "- For every removed or replaced line, name the behaviour it "
    "enforced and where the new code re-establishes it.\n"
    "- Grep the callers of every changed function, and grep the codebase "
    "for other sites of each defect class the diff fixes or touches. "
    "Report each other site as its own finding with `\"sibling\": true`, "
    "its `file` and `line`, and the defect class named in its `text`.\n"
    "\n"
    "Draw each site's line by the sibling rule in the base prompt above — "
    "it applies to every site you report, this lens or not.\n"
)


def reviewer_seat_lens(seat_no: int, reviewers: int) -> str:
    """Which lens reviewer seat ``seat_no`` (1-based) of an N-seat
    fan-out gets (F-94): ``"both"`` for a one-seat fan-out,
    ``"conformance"`` for odd seats, ``"defect_hunt"`` for even ones."""
    if max(1, int(reviewers)) == 1:
        return "both"
    return "conformance" if int(seat_no) % 2 == 1 else "defect_hunt"


def reviewer_seat_prompts(profile: dict | None = None,
                          reviewers: int = 2) -> list[str]:
    """Every reviewer seat's system prompt, seat 1 first (F-94).

    The conformance seat gets :func:`assemble_reviewer_prompt` unchanged
    (today's job: the contract and the standards axis — the base prompt
    IS the conformance review); a defect-hunt seat, and the single seat
    of a one-seat fan-out (which carries both lenses), gets the same
    base plus :data:`DEFECT_HUNT_LENS`. So two seats' prompts differ
    only by the lens block.

    Every reviewer fan-out and every per-seat retry or repair takes seat
    N's prompt from here — a retried seat 2 keeps the defect hunt — and
    the eval's fan-out uses the same list (F-48 parity). A reviewer
    count below 1 is treated as 1 (the config layer refuses it).

    Raises:
        PromptError: As :func:`assemble_reviewer_prompt`.
    """
    base = assemble_reviewer_prompt(profile)
    hunt = base + "\n\n" + DEFECT_HUNT_LENS
    count = max(1, int(reviewers))
    return [base if reviewer_seat_lens(n, count) == "conformance" else hunt
            for n in range(1, count + 1)]


def assemble_verifier_prompt(profile: dict | None = None) -> str:
    """The verifier's system prompt (F-38): the base file (vote rules, drop
    cases, tags, the F-103 re-rate-down vote), the same shared severity
    rubric the reviewer gets (:data:`SEVERITY_RUBRIC` — the re-rate reason
    argues against the rubric's own A bucket), plus the same standards
    assembly the reviewer gets — the verifier judges whether a finding is
    style-only or cites a real documented standard, which needs the
    standards texts in hand.

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
            + "\n\n" + SEVERITY_RUBRIC
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
    return (
        prompt
        + "\n\n" + SEVERITY_RUBRIC
        + "\n\n## Project standards\n\n" + "\n".join(sections)
    )


def prompt_versions(profile: dict | None = None, *, reviewers: int = 2) -> dict[str, str]:
    """Seat -> content hash of the assembled prompt, for the attempt record
    (``prompt_versions``, BUILD-ORDER F-10 AC-6, F-38 AC-5) and credit
    assignment in F-19. Hashing the *assembled* prompt means a change to the
    base file or to what assembly injects both show up as a new version.

    The reviewer fan-out (F-38) gets one entry per session —
    ``reviewer-1`` .. ``reviewer-N`` — each the hash of THAT seat's own
    prompt (F-94: the seats differ by lens, see
    :func:`reviewer_seat_prompts`); the verifier gets its own entry. A
    reviewer count below 1 is treated as 1 (the config layer refuses it;
    this stays total for direct callers).

    Args:
        profile: The loaded ``project.yaml`` mapping (optional in tests).
        reviewers: How many reviewer sessions the fan-out runs.

    Returns:
        ``{"implementer": <sha256>, "reviewer-1": <sha256>, …,
        "verifier": <sha256>}``.
    """
    versions = {"implementer": prompt_hash(assemble_implementer_prompt(profile))}
    for number, text in enumerate(
            reviewer_seat_prompts(profile, reviewers), start=1):
        versions[f"reviewer-{number}"] = prompt_hash(text)
    versions["verifier"] = prompt_hash(assemble_verifier_prompt(profile))
    return versions
