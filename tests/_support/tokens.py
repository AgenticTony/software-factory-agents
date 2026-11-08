"""Runtime-built fake credentials (F-58 AC-G).

GitGuardian's generic high-entropy detector flags every PR that ADDS a
high-entropy literal — the old test literals (``SEAT-TOKEN-…`` bodies,
the jwt.io example JWT, hex secrets) made each factory PR red the moment
it touched one of those lines. The values are now BUILT at runtime,
deterministically per label (hashlib), so the source carries no
high-entropy literal at all: the only strings here are short prefixes
and labels, which ``tests/test_f58.py::test_ac_g``'s lint proves for
the whole tests tree on every PR.

Shape is preserved exactly — the scrub and detector tests were written
against these shapes and mutation-prove them:

* ``fake_token`` — the seat token: ``SEAT-TOKEN-`` + a 24-char
  lowercase-alnum body (35 total), deliberately NOT shaped like any
  credential pattern, so only the literal scrub can catch it (the F-30 /
  F-43 / F-57 contract);
* ``fake_aws_key`` — ``AKIA`` + exactly 16 uppercase/digit chars (the
  sentinel pattern is ``AKIA[0-9A-Z]{16}\\b``: a longer body would not
  match, and the detection is the point of the test);
* ``fake_stripe_key`` — ``sk_live_`` + a 24-char body (pattern: 16+);
* ``fake_jwt`` — two eyJ-prefixed base64url segments (unpadded, as JWTs
  are) plus a signature, so the F-43 "JSON web token" pattern fires;
* ``fake_zai_key`` — ``<24 hex>.<20 alnum>``, the Z.ai
  ``<id>.<secret>`` shape: no eyJ, no known prefix.

Determinism matters for review: the same label yields the same value on
every interpreter and every run, so a failure message or a ledger can
name the label and mean one value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import string

_LOWER_DIGITS = string.ascii_lowercase + string.digits
_UPPER_DIGITS = string.ascii_uppercase + string.digits
_LETTERS_DIGITS = string.ascii_letters + string.digits
_B64URL = string.ascii_letters + string.digits + "-_"


def _body(label: str, length: int, alphabet: str) -> str:
    """A deterministic per-label body of ``length`` chars from
    ``alphabet`` — hashlib-driven, never random, never a literal."""
    out: list[str] = []
    block = 0
    while len(out) < length:
        digest = hashlib.sha256(f"{label}:{block}".encode()).digest()
        for byte in digest:
            out.append(alphabet[byte % len(alphabet)])
            if len(out) == length:
                break
        block += 1
    return "".join(out)


def fake_token(label: str, *, prefix: str = "SEAT-TOKEN-",
               body_len: int = 24) -> str:
    """The seat token: ``prefix`` + a deterministic lowercase-alnum body
    (default 24 chars, 35 total with the standard prefix)."""
    return f"{prefix}{_body('seat:' + label, body_len, _LOWER_DIGITS)}"


def fake_aws_key(label: str) -> str:
    """``AKIA`` + exactly 16 uppercase/digit chars — the AWS access-key
    shape the sentinel pattern ``AKIA[0-9A-Z]{16}`` requires (a longer
    body would break the word boundary and the detection)."""
    return "AKIA" + _body("aws:" + label, 16, _UPPER_DIGITS)


def fake_stripe_key(label: str) -> str:
    """``sk_live_`` + a 24-char body — the Stripe live-key shape
    (the sentinel pattern takes 16+)."""
    return "sk_live_" + _body("stripe:" + label, 24, _LOWER_DIGITS)


def fake_jwt(label: str) -> str:
    """A JWT-shaped value whose segments derive from ``label``: both
    visible segments are base64url of real JSON (so both start ``eyJ``,
    which is what the F-43 pattern matches), unpadded as JWTs are."""
    header = {"alg": "HS256", "typ": "JWT", "kid": _body("kid:" + label, 8, _B64URL)}
    payload = {"sub": _body("sub:" + label, 12, _B64URL), "iat": 1731024000}

    def segment(doc: dict) -> str:
        raw = json.dumps(doc, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signature = _body("sig:" + label, 32, _B64URL)
    return f"{segment(header)}.{segment(payload)}.{signature}"


def fake_zai_key(label: str) -> str:
    """The Z.ai credential shape ``<24 hex>.<20 alnum>``: no eyJ, no
    known prefix — only the env-echo and literal scrubs catch it."""
    return (f"{_body('zai-id:' + label, 24, string.digits + 'abcdef')}"
            f".{_body('zai-secret:' + label, 20, _LETTERS_DIGITS)}")
