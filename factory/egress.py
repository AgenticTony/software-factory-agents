"""The egress seam (BUILD-ORDER F-43) — one function everything leaving a
session passes.

CODE-REVIEW-2026-11-05 C-1: the implementer's final text goes into the PR
body, the seat env holds the Z.ai token, and the orchestrator does the push
— so the sandbox's network deny never applies to the PR channel. M-8: vents
and unparsable-record dumps got pattern redaction only, so a bare
``ANTHROPIC_AUTH_TOKEN=<jwt>`` echo (no quotes, no known prefix) sailed
through. m-16: whatever did cross was printed to the owner's terminal with
ANSI/OSC/C0 control sequences intact — a screen clear, a spoofed status
line, an OSC-8 link.

The seam is one composition, in one fixed order:

1. **Literal scrub** — every caller-known secret (``scrub_literal``), the
   seat token today. This runs FIRST so no later rewrite can split or
   reshape the literal: a partial pattern match cannot leave a token
   fragment behind, because the whole literal is already gone.
2. **Pattern redaction** — :func:`factory.sentinels.redact_credentials`,
   the F-25 credential shapes (plus F-43's JWT / Bearer / unquoted YAML
   password shapes), for secrets the caller does not hold literally.
3. **Control-sequence strip** — LAST, so redaction judged the text as it
   was written; the strip then removes what could command the terminal.
   Because the strip REWRITES text (it can merge two halves a secret was
   split into), the scrubbers re-judge the rewritten form whenever the
   strip changed anything — the hostile pass found a control-split token
   reassembling behind the scrub otherwise.

The two scrubbers are not re-implemented here — :mod:`factory.sentinels`
owns them (F-43 moved ``scrub_literal`` beside ``redact_credentials``);
this module only composes them, so the pattern list can never drift
between the trace's redaction and the egress seam's.

The secrets arrive as a SEQUENCE, never a hard-coded "the seat token":
F-59 adds per-provider credentials, and each caller passes what it holds —
the orchestrator the seat token, the in-sandbox runner its env token, the
flush the orchestrator's again (untrusted text is scrubbed at every
boundary it crosses, the F-27 invariant; the scrub is idempotent).

What passes the seam (the channels of AC-6, asserted by
``tests/test_f43.py::test_ac_6``):

* the PR body's session-text inputs — the implementer notes, the L2
  finding texts and the verdict JSON (:func:`scrub_session_value` over the
  report dict) — before ``build_pr_body``;
* vent text, twice: in-sandbox at :meth:`VentCollector.record` and again
  at :func:`factory.vent.flush_vents`;
* the unparsable-record stdout dump
  (:func:`factory.session._save_session_stdout`);
* terminal-bound text at its reading seams: trace lines
  (:func:`factory.trace.read_trace`) and the status page
  (:func:`factory.status.render_status`) strip control sequences at
  PRINT, not at write — the stored trace keeps its content; only what a
  terminal would execute is removed.

:func:`factory.publish.check_pr_text` is deliberately NOT this seam: it is
a second, independent fence that refuses the PR outright when the literal
token still appears — defense that assumes the seam may have failed.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from factory.sentinels import redact_credentials, scrub_literal

# What must never reach a terminal, applied last. Order matters: the OSC
# and DCS patterns consume their ST terminator before the ESC catch-all
# can halve it, and the catch-all then takes any other two-byte escape
# the specific families missed. C0 controls go except tab and newline (a
# redaction seam that shredded line structure would shred its own
# evidence); carriage return goes — it is how a line rewrites the one
# above it — DEL with them, and the 8-bit C1 controls (U+0080–U+009F:
# \u009b CSI, \u009d OSC, \u0090 DCS — review round 1, finding 6), which
# terminals in several VTE/xterm and Linux-console configurations execute
# without any ESC byte.
_CONTROL_SEQUENCES = (
    # OSC, BEL- or ST-terminated — hyperlinks (OSC 8), window titles.
    re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"),
    # CSI — SGR colors, cursor moves, screen clears, mode switches.
    re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]"),
    # DCS/SOS/PM/APC strings, ST-terminated — the terminator REQUIRED
    # (review round 1, finding 7): an optional one made an unterminated
    # introducer swallow everything to the next ESC or end of text, a way
    # for a hostile seat to hide the findings after it. Without an ST the
    # introducer alone falls through to the catch-all below, and the text
    # after it stays.
    re.compile(r"\x1b[PX^_][^\x1b]*\x1b\\"),
    # Any other escape sequence (ESC + one byte).
    re.compile(r"\x1b."),
    # C0 minus tab/newline, DEL, and the C1 range.
    re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]"),
)


def check_secrets(secrets: Sequence[str]) -> tuple[str, ...]:
    """The one guard every ``secrets``/``extra_secrets`` entry calls
    (review round 2): refuse a bare ``str`` — and, round 3, any element
    that is not a ``str`` or ``None``.

    A parenthesised string — ``secrets=(token or "")`` — is iterable
    character by character, every character is shorter than the scrub
    floor, and the literal scrub silently scrubs NOTHING. A non-str
    element (keychain bytes, a nested tuple) dies even deeper, inside
    ``str.replace``, with an error that never names the real mistake.
    Both must be loud: :class:`TypeError` at the boundary, naming the
    shape — the element's TYPE only, never its value (it may be the
    secret). ``None`` elements pass (the ``(token,) if token else ()``
    and empty-token fallback shapes); real sequences are returned as a
    TUPLE, normalised exactly once (a one-shot iterable stays usable by
    everything downstream).
    """
    if isinstance(secrets, str):
        raise TypeError(
            "secrets must be a sequence of secrets, not a str — a bare "
            "string is iterated character by character and every character "
            "is shorter than the scrub floor, so the literal scrub never "
            "runs; pass a tuple/list, e.g. secrets=(token,)"
        )
    checked = tuple(secrets)
    for item in checked:
        if not isinstance(item, (str, type(None))):
            raise TypeError(
                f"secrets must be a sequence of str secrets; got a "
                f"{type(item).__name__} element — the element is not "
                "printed (it may be the secret); pass str secrets or None"
            )
    return checked


def strip_control_sequences(text: str) -> str:
    """Remove ANSI/OSC/C0 control sequences from ``text``.

    m-16: session text printed raw can clear the owner's screen, move the
    cursor or plant an OSC-8 link — none of it survives this. Tab and
    newline stay: the strip removes commands, not structure.
    """
    for pattern in _CONTROL_SEQUENCES:
        text = pattern.sub("", text)
    return text


def scrub_session_text(text: str, secrets: Sequence[str] = ()) -> str:
    """The egress seam: literal secrets, then credential shapes, then
    control sequences.

    The strip is itself a text-rewriting boundary: a secret SPLIT by an
    escape sequence (``<half>\\x1b[0m<half>`` — found by the pre-commit
    hostile pass) is not the literal while split, so both scrubbers miss
    it, and the strip then merges the halves into the whole secret. When
    the strip changes the text, the scrubbers therefore re-judge the
    rewritten form; when it changes nothing (the common case) the seam is
    exactly the three-stage composition, run once.

    Args:
        text: Untrusted session text about to leave a session's boundary
            — into a PR body, a vent, a dump or a terminal.
        secrets: Caller-known literal secrets (the seat token; F-59 adds
            per-provider credentials). Short literals are noise and are
            ignored (:data:`factory.sentinels.MIN_SECRET_LEN`). A bare
            ``str`` is refused (:func:`check_secrets`).
    """
    secrets = check_secrets(secrets)

    def _scrub(value: str) -> str:
        for secret in secrets:
            value = scrub_literal(value, secret)
        return redact_credentials(value)

    text = _scrub(text)
    stripped = strip_control_sequences(text)
    if stripped != text:
        return _scrub(stripped)
    return stripped


def scrub_session_value(value: Any, secrets: Sequence[str] = ()) -> Any:
    """The seam over a report-shaped value: every string — keys included —
    passes :func:`scrub_session_text`, structure and non-strings kept.

    The L2 report is a dict of session-derived strings (finding texts, the
    verdict JSON's payloads, parse errors); a hostile finding dict could
    also carry a secret in a KEY, and the PR body renders both. The copy
    is new; the input is never mutated (stored reports keep what the
    validators produced). The secrets are NORMALISED ONCE here and the
    recursion carries the tuple (round 3, finding 1: the guard's return
    used to be discarded, so a one-shot iterable was consumed by the
    check and every nested scrub saw nothing).
    """
    return _scrub_value(value, check_secrets(secrets))


def _scrub_value(value: Any, secrets: tuple) -> Any:
    """scrub_session_value's recursion over an already-checked tuple."""
    if isinstance(value, str):
        return scrub_session_text(value, secrets)
    if isinstance(value, dict):
        return {
            _scrub_value(key, secrets): _scrub_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, secrets) for item in value)
    return value


# F-63 R2-1: the control classes approval OUTPUT must never emit raw —
# the same families strip_control_sequences removes, PLUS the bidi and
# isolate controls that seam keeps by design (a redaction seam that
# reordered its own evidence would shred it; an approval prompt that
# lets them through lets a spoof reorder what the owner reads), and
# newline/carriage-return/tab: a VALUE carrying them can rewrite the
# prompt from inside (the R2-1 spoof).
_BIDI_CONTROLS = "\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c"


def escape_approval_text(text: str, *, escape_non_ascii: bool = False) -> str:
    """Escape every character an approval prompt must show literally.

    The strip seam (:func:`strip_control_sequences`) REMOVES commands and
    keeps structure — right for redactions, wrong for approval output,
    where silently dropping bytes would hide exactly the oddity the owner
    must see. This escaper makes every control character VISIBLE as a
    ``\\xNN``/``\\uNNNN`` escape: C0 (newline, carriage return and tab
    included), DEL, the C1 range, and the bidi/isolate controls. With
    ``escape_non_ascii`` (repo.remote and repo.path), every non-ASCII
    character escapes too, so a homoglyph remote reads as its codepoints.

    Composed, not forked: the class definitions live beside the strip
    patterns; the two seams share the module's one notion of "control".
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069 \
                or code in (0x200E, 0x200F, 0x061C):
            out.append(f"\\u{code:04x}")
        elif escape_non_ascii and code > 0x7E:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)
