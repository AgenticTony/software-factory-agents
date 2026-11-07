"""Shared test support (F-51 AC-6/AC-7, I-16/I-17).

One home for what test modules share: the ``wait_until`` condition-wait
helper (no test sleeps for a condition), the live-session opt-in gate
(AC-3: a model-spend test runs only on an explicit FACTORY_LIVE_TESTS=1,
never inferred from a credential), the interpreter-independent bare-
``python3`` prompt check (AC-1/M-11), and the per-origin harness modules
(``_support.f05`` … ``_support.f38``) moved byte-for-byte out of the test
modules that other suites used to import. No test module imports another
test module, and nothing here imports a test module.
"""

from __future__ import annotations

import os
import time
from shlex import quote
from typing import Callable

import pytest

LIVE_TESTS_ENV = "FACTORY_LIVE_TESTS"

# F-51 review round 1 (major 3): after the exact interpreter path is
# removed, NO residual "python3" is acceptable — a hard-coded OTHER
# interpreter (/usr/bin/python3, ./python3, python3.11) is the same bug
# (not the factory's venv), so this is a substring check on the residue,
# not a word-boundary one. Verified: under conda, pyenv, system and
# space-path interpreters the real prompt's residue is empty.


def live_tests_enabled() -> bool:
    """True only on the exact opt-in ``FACTORY_LIVE_TESTS=1``.

    Every other value — ``0``, ``true``, ``yes``, `` 1``, empty — is NOT
    an opt-in: the value semantics are the contract (AC-3/m-21), so an
    org runner that merely standardizes on the Z.ai gateway can never
    silently flip a test to real spend.
    """
    return os.environ.get(LIVE_TESTS_ENV) == "1"


def live_session_gate() -> None:
    """The first call of every live half: skip unless explicitly opted in.

    The skip reason names the variable (visible in ``-rs``). The caller
    consults the credential only AFTER this gate — the credential never
    decides whether a live session runs.
    """
    if not live_tests_enabled():
        pytest.skip(
            "live session half: opt in with FACTORY_LIVE_TESTS=1 "
            "(a credential alone never turns this on)"
        )


def no_bare_python3(text: str, executable: str) -> str | None:
    """The first residual ``python3`` in ``text``, or None if there is none.

    The exact interpreter path is removed first — in the ``shlex.quote``
    form the prompt embeds and the bare form — and never by stripping the
    ``python3`` substring itself, so a conda/pyenv/system interpreter
    whose own path ends in ``python3`` cannot false-positive (M-11).
    After that removal, ANY remaining ``python3`` is reported (round 1,
    major 3): a hard-coded other interpreter — ``/usr/bin/python3``,
    ``./python3``, ``python3.11`` — is exactly the bug the check exists
    for (a bare ``python3`` resolves to whatever the sandbox PATH finds,
    which is not the factory's venv), so the residue must be empty.
    """
    for form in {quote(executable), executable}:
        text = text.replace(form, "")
    return "python3" if "python3" in text else None


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    what: str,
    interval: float = 0.1,
) -> bool:
    """Poll ``predicate`` until it is true, failing with ``what`` on timeout.

    Never a fixed sleep: returns as soon as the condition holds, and on
    timeout raises ``AssertionError`` naming the condition that never
    came true. An exception raised by the predicate propagates — a
    crashing condition is a failure, not something to out-wait.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout:g}s waiting for {what}"
            )
        time.sleep(interval)
