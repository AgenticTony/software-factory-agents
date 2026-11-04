"""AI-attribution patterns — the ONE list L0 and publish share (F-42).

Five commits on this repo's ``main`` carry machine-written attribution
(#22, #58, #61, #82, #86) because the implementer's CLI appends
``Co-Authored-By: Claude <noreply@anthropic.com>`` trailers (and
``Generated with …`` PR footers) by default. On a client repo that is a
contract and trust problem, not a style one, so F-42 refuses the class at
both places it could leave the factory:

* :func:`factory.l0.run_l0` scans the commit messages between the
  merge-base and head and FAILs with an ``attribution_trailer`` finding
  (AC-1) — before anything is pushed.
* :func:`factory.publish.check_pr_text` (called by the orchestrator
  BEFORE the push, and again by :func:`factory.publish.gh_pr_create` as
  the seam's backstop) refuses a PR title or body whose free text
  carries the same patterns (AC-2) — so a squash commit, whose message
  GitHub builds from that title and body, cannot inherit one either.

Matching rules (PR #89 review round):

* **Trailer prefixes** — ``Co-Authored-By:``, ``Assisted-by:``,
  ``Generated-by:`` and ``AI-assisted:`` all carry attribution.
* **Unambiguous AI authors** (Claude, Anthropic, Copilot, ChatGPT,
  GPT-n, Codex, OpenAI, this factory's own seat names) match on their
  own: none of them is a human's name.
* **Names that are also human names** (Kimi, Gemini, Mistral, Llama,
  Cursor, Qwen, GLM, Moonshot) flag only with an AI shape beside them —
  a bot/noreply address, an AI vendor address, or the product's compound
  name ("Cursor Agent", "Gemini Code Assist"). ``Co-authored-by: Kimi
  Larsson <kimi@acme.se>`` is a human credit and passes.
* **AI vendor addresses** flag whatever the name; Copilot's
  ``copilot@github.com`` is named exactly so GitHub's human
  ``<login>@users.noreply.github.com`` addresses pass.
* **The ``Generated with`` footer** is the phrase followed (within two
  connecting tokens) by an AI tool or an AI vendor link — anywhere in a
  line, in every surface (commit message, PR body, title). Ordinary prose
  ("generated with openapi-generator/faker/ruff") names no AI tool and
  passes.
* A bare ``AI-assisted: yes`` declaration fails on its own: the trailer
  exists only to declare AI assistance.

Two compiled forms of the same list (:data:`_PATTERN_CORES`, never a
second hand-maintained list): **anchored** — trailers and declarations
match only at a line start, for commit messages and PR bodies, where
prose may legitimately *discuss* a trailer mid-sentence; and
**unanchored**, for a PR TITLE — one line by construction, so a trailer
anywhere in it is deliberate. The footer is the exception
(:data:`_UNANCHORED`): it matches mid-line in every surface, since its
AI-tool requirement already keeps ordinary prose out.

A human ``Co-Authored-By`` passes: a human crediting a human is what the
trailer exists for. Where the classes must choose, they over-match (a
human genuinely named Claude fails closed — the branch FAILs and a human
reads it); under-matching silently publishes an attribution the contract
forbids.
"""

from __future__ import annotations

import re

# Trailer prefixes a squash-merged body can carry an AI attribution
# under.
_TRAILER_NAMES = r"(?:co-authored-by|assisted-by|generated-by|ai[- ]assisted)"

# AI authors that are not human names — they match alone.
_AI_AUTHORS = (
    r"claude|anthropic|copilot|chatgpt|gpt-?\d\w*|codex|openai"
    r"|software factory|factory implementer"
)

# Names that are ALSO human names — they flag only with an AI shape
# beside them (bot/noreply address, vendor address, product compound).
_AMBIGUOUS = r"kimi|gemini|mistral|llama|cursor|qwen|glm|moonshot"

# AI vendor mail domains. Copilot's GitHub address is matched exactly:
# GitHub's human noreply addresses are ``<login>@users.noreply.github.com``
# and must pass.
_AI_VENDOR_DOMAINS = (
    r"anthropic\.com|openai\.com|cursor\.(?:sh|com)|character\.ai|moonshot\.ai"
    r"|deepseek\.com|mistral\.ai|claude\.(?:com|ai)"
)

# The one list: (name, core regex without any line anchor). Compiled
# anchored below for commit messages and bodies; compiled unanchored for
# titles. The name rides into findings and refusals so the implementer
# and the owner see WHICH class fired, not just that one did.
_PATTERN_CORES: tuple[tuple[str, str], ...] = (
    (
        "co_author_ai_author",
        _TRAILER_NAMES + r":[^\n]*\b(" + _AI_AUTHORS + r")\b",
    ),
    (
        "co_author_ambiguous_ai_name",
        # An ambiguous name + an AI shape anywhere later on the same
        # trailer line, or the product's compound name right behind it.
        _TRAILER_NAMES + r":[^\n]*("
        r"\b(?:" + _AMBIGUOUS + r")\b[^\n]*<[^>\n]*\b(?:noreply|bot)\b[^>\n]*@[^>\n]*>"
        r"|\b(?:" + _AMBIGUOUS + r")\b[^\n]*@(?:" + _AI_VENDOR_DOMAINS + r")\b"
        r"|\b(?:" + _AMBIGUOUS + r")[ \t]+(?:agent|code[ \t]+assist|cli)"
        r")",
    ),
    (
        "co_author_ai_vendor_address",
        _TRAILER_NAMES + r":[^\n]*(?:"
        r"<[^\n>]*@(?:" + _AI_VENDOR_DOMAINS + r")>|<copilot@github\.com>"
        r")[ \t]*$",
    ),
    (
        "ai_assisted_declaration",
        # The bare declaration: the trailer exists only to say AI helped,
        # so any non-empty value is the attribution itself.
        r"ai[- ]assisted:[ \t]*\S",
    ),
    (
        "generated_with_footer",
        # The phrase, then (within two connecting tokens, so "Generated
        # with GitHub Copilot" reaches its tool) an AI tool — bracketed
        # or bare — or an AI vendor link. The AI-tool requirement is what
        # keeps ordinary prose out: "generated with
        # faker/ruff/openapi-generator" names none.
        r"generated with\s+(?:[\w.-]+[ \t]+){0,2}("
        r"\[[^\]\n]*\b(?:claude|copilot|cursor|codex|gemini)\b[^\]\n]*\]"
        r"|\b(?:claude|copilot|cursor|codex|gemini|chatgpt|gpt|openai|anthropic)\b"
        r"|[^\s\n]*(?:" + _AI_VENDOR_DOMAINS + r")"
        r")",
    ),
)

# Cores that match anywhere in a line, in every surface. The footer's
# AI-tool requirement is what keeps ordinary prose out, not its position:
# "This change was generated with Claude Code." mid-body is the
# attribution all the same (PR #89 second review).
_UNANCHORED = frozenset({"generated_with_footer"})

# Anchored: trailers and declarations are line-initial. Up to eight
# non-word, non-newline characters may sit before them — a quote, list
# bullet, heading mark, or the emoji on the real footer line. (Newline is
# excluded so the prefix can never start mid-line and swallow a line
# break; word characters are excluded so ordinary prose still cannot
# reach the phrase.)
ATTRIBUTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        name,
        re.compile(
            core if name in _UNANCHORED else r"^[^\w\n]{0,8}" + core,
            re.IGNORECASE | re.MULTILINE,
        ),
    )
    for name, core in _PATTERN_CORES
)

# Unanchored, for a PR TITLE — one line by construction, so a trailer
# anywhere in it ("T-1: add Co-Authored-By: Claude support") carries the
# pattern. Derived from the same cores, never a second list.
TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(core, re.IGNORECASE)) for name, core in _PATTERN_CORES
)


def _match(patterns: tuple[tuple[str, re.Pattern[str]], ...], *texts: str):
    """The first hit of ``patterns`` across ``texts``, as the full source
    line — a finding that names the trailer names the whole line (the
    emoji with the footer, the address with the author), taken from the
    line's own start whatever the pattern anchored on."""
    for name, pattern in patterns:
        for text in texts:
            match = pattern.search(text or "")
            if match:
                start = text.rfind("\n", 0, match.start()) + 1
                end = text.find("\n", match.start())
                line = text[start:end if end != -1 else len(text)]
                return name, line.strip()
    return None


def attribution_match(*texts: str) -> tuple[str, str] | None:
    """The first AI-attribution hit across ``texts`` (anchored form).

    Args:
        *texts: The surfaces to scan — a commit message, a PR body — each
            in full; the trailer patterns are line-anchored, so prose
            that discusses a trailer mid-sentence does not trip.

    Returns:
        ``(pattern name, matched line stripped)`` for the first match, in
        :data:`ATTRIBUTION_PATTERNS` order, or ``None`` when every text is
        free of AI attribution.
    """
    return _match(ATTRIBUTION_PATTERNS, *texts)


def title_attribution_match(title: str) -> tuple[str, str] | None:
    """The AI-attribution hit in a PR TITLE (unanchored form).

    A title is one line by construction, so a trailer anywhere in it is
    deliberate; the same cores, compiled without the line anchor.
    """
    return _match(TITLE_PATTERNS, title)
