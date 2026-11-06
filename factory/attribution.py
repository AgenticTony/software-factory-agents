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
* **The ``Generated with`` footer** is the phrase followed (anywhere
  later on the same line, however many connecting tokens sit between) by
  an AI tool or an AI vendor link — in every surface (commit message, PR
  body, title). Ordinary prose ("generated with
  openapi-generator/faker/ruff") names no AI tool and passes; the
  AI-tool requirement is the guard, not the token count (m-17: a
  ``{0,2}`` cap was an evasion — "Generated with my favourite shiny tool
  Claude").
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

**Canonical form (F-46, m-17, review round 1).** Every pattern matches
against TWO canonical forms of the text (:func:`_canonical_forms`):
confusable lookalikes folded (Cyrillic ``с`` inside "claude"), NFKD
(fullwidth and mathematical-bold spellings become ASCII, accents
decompose), the invisible set stripped — format characters, the
Default_Ignorable set (variation selectors, Hangul fillers), nonspacing
and enclosing marks (``Cláude`` is "Claude"), and control characters
other than tab and newline (``Co-Authored\\x1e-By`` is the trailer) —
and Unicode dashes folded to ``-``. The two forms differ on line
separators: the line-split form turns every ``str.splitlines()``
boundary into a newline (a trailer behind ``\\r``/LS/PS/NEL is
line-initial), the control-stripped form joins a keyword a record
separator split — both run, and the finding echoes the ORIGINAL line of
whichever hit.
"""

from __future__ import annotations

import re
import unicodedata

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
        # The phrase, then (anywhere later on the line, however many
        # connecting tokens sit between — m-17: a {0,2} cap was an
        # evasion) an AI tool — bracketed or bare — or an AI vendor link.
        # The AI-tool requirement is what keeps ordinary prose out:
        # "generated with faker/ruff/openapi-generator" names none.
        r"generated with\s+[^\n]*?("
        r"\[[^\]\n]*\b(?:claude|copilot|cursor|codex|gemini)\b[^\]\n]*\]"
        r"|\b(?:claude|copilot|cursor|codex|gemini|chatgpt|gpt|openai|anthropic)\b"
        r"|[^\s\n]*(?:" + _AI_VENDOR_DOMAINS + r")"
        r")",
    ),
)

# Single letters visually confusable with ASCII, folded to their Latin
# lookalike BEFORE matching (F-46's decision on the m-17 homoglyph class:
# NFKC does not cross scripts, and "сlaude" with a Cyrillic с is
# indistinguishable from "claude" in every terminal a human reads). A
# hand-picked set — the unambiguous Cyrillic/Greek shapes — not a
# confusables database; over-matching is the module's standing direction.
_CONFUSABLES = str.maketrans({
    # Cyrillic lookalikes (lowercase and uppercase forms)
    "а": "a", "А": "a", "в": "b", "В": "b", "с": "c", "С": "c",
    "е": "e", "Е": "e", "һ": "h", "Һ": "h", "і": "i", "І": "i",
    "ј": "j", "Ј": "j", "к": "k", "К": "k", "м": "m", "М": "m",
    "н": "h", "Н": "h", "о": "o", "О": "o", "р": "p", "Р": "p",
    "ѕ": "s", "Ѕ": "s", "т": "t", "Т": "t", "ԁ": "d", "у": "y", "У": "y",
    "ԛ": "q", "ԝ": "w", "х": "x", "Х": "x",
    # Greek lookalikes
    "α": "a", "Α": "a", "ϵ": "e", "ε": "e", "ι": "i", "Ι": "i",
    "κ": "k", "Κ": "k", "ν": "v", "ο": "o", "Ο": "o", "ρ": "p",
    "Ρ": "p", "ϲ": "c", "ς": "s", "τ": "t", "Τ": "t", "υ": "u",
})

# The Unicode Default_Ignorable_Code_Point set (PropList.txt), as ranges.
# Covers what Cf alone misses (review round 1, finding 5): variation
# selectors (FE00..FE0F — also Mn), the Hangul fillers 115F/1160/3164 and
# FFA0 (category Lo, invisible in rendering), CGJ 034F, the ARABIC LETTER
# MARK, the tag and supplementary-variation-selector planes. Most of it
# is also Cf/Mn; the Lo fillers are why the set exists.
_DI_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C),
    (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180E),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F),
    (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF), (0xFFA0, 0xFFA0),
)
_DEFAULT_IGNORABLE = frozenset(
    chr(cp)
    for start, end in _DI_RANGES
    for cp in range(start, end + 1)
)

# Every str.splitlines() boundary except \n and the two-char \r\n: each
# renders as a line break in the surfaces this module scans, so a trailer
# behind one is line-initial to a human terminal (review round 1,
# finding 4).
_LINE_BOUNDARY = re.compile("\r\n|[\r\v\f\x1c\x1d\x1e\x85\n  ]")


def _fold(text: str) -> str:
    """Fold one text to its match form: NFKD (fullwidth and
    mathematical-bold spellings decompose to ASCII, accents to base +
    mark), the invisible set — format characters (Cf), the
    Default_Ignorable set (variation selectors, Hangul fillers) and
    nonspacing/enclosing marks (Mn/Me — the combining accent of
    ``Cláude``) — stripped, other control characters except tab and
    newline stripped (an ``\\x1e`` inside the trailer keyword splits it
    as surely as a zero-width space, and a commit body can carry any
    byte), Unicode dashes (Pd) folded to ``-``, and finally the
    confusable lookalikes translated — AFTER the decomposition, so a
    precomposed confusable (Cyrillic ``ѐ`` → Cyrillic ``е`` + grave)
    has its base letter folded once the mark is gone (review round 3).

    Args:
        text: The text to fold (line structure must be preserved by the
            caller — newlines survive this fold).

    Returns:
        The folded text; same number of newlines as the input modulo the
        caller's own line-boundary handling.
    """
    text = unicodedata.normalize("NFKD", text)
    out = []
    for ch in text:
        category = unicodedata.category(ch)
        if (
            category == "Cf"
            or ch in _DEFAULT_IGNORABLE
            or category in ("Mn", "Me")
            or (category == "Cc" and ch not in "\t\n")
        ):
            continue
        out.append("-" if category == "Pd" else ch)
    return "".join(out).translate(_CONFUSABLES)


def _canonical_forms(text: str):
    """The TWO canonical forms every attribution pattern is tried
    against, each with its aligned ORIGINAL lines: the line-split form
    (every ``str.splitlines()`` boundary — CR, VT, FF, the record
    separators, NEL, LS, PS — becomes a newline first, so a trailer
    behind one is line-anchored) and the control-stripped form (those
    bytes vanish, which JOINS a keyword they split — ``Co-Authored\\x1e-
    By``). The two pull opposite ways for the record separators, so both
    run and the echo names the original line of whichever hit (review
    round 1, finding 4).

    Args:
        text: The text to canonicalise (non-empty; the caller skips
            empty texts).

    Yields:
        ``(canonical, original_lines)`` pairs — the folded form and the
        original lines its line numbering maps back onto.
    """
    yield _fold(text), text.split("\n")
    yield _fold(_LINE_BOUNDARY.sub("\n", text)), text.splitlines()


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
    emoji with the footer, the address with the author).

    Matching runs on each text's TWO canonical forms
    (:func:`_canonical_forms`), computed once per text before the
    pattern loop, so a trailer spelled with Unicode tricks, or parked
    behind a Unicode line separator, is the trailer all the same; the
    returned line is the text's ORIGINAL one, aligned by the form that
    hit, so a human reads what was actually written.
    """
    prepared = [(text, list(_canonical_forms(text))) for text in texts if text]
    for name, pattern in patterns:
        for text, forms in prepared:
            for canonical, original_lines in forms:
                match = pattern.search(canonical)
                if match:
                    line_no = canonical.count("\n", 0, match.start())
                    return name, original_lines[line_no].strip()
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
