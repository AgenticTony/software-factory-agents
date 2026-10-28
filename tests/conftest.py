"""Suite-wide guards for the environment the tests run in."""

import functools
from pathlib import Path

import pytest

import factory

_REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run the suite against another checkout's code.

    This repo is usually installed editable into a shared environment. A
    worktree's bare ``pytest`` then imports ``factory`` from whatever
    checkout the editable install points at, not the one under test, so
    every result is about another branch — and one test fails as an
    apparent sandbox regression. Fail loudly before the first test
    collects, with the remedy in the message.
    """
    factory_path = Path(factory.__file__).resolve()
    if _REPO_ROOT not in factory_path.parents:
        raise pytest.UsageError(
            f"factory imported from {factory_path}, not the repo under test "
            f"({_REPO_ROOT}): a shared editable install is shadowing this "
            "checkout. Run the suite from this repo as `python -m pytest` "
            "or with PYTHONPATH=., or give this checkout its own "
            "environment."
        )
    _guard_nested_sandbox()


def _guard_nested_sandbox() -> None:
    """INC-007: skip sandbox-spawning tests only when the suite runs nested.

    macOS cannot apply a seatbelt profile from inside one, so a sandboxed
    run of this suite (the L0 gate runs it inside the F-05 sandbox, F-10
    AC-11) can never spawn a second srt. Tests that reach for
    run_sandboxed would fail on the kernel's refusal, not on the code
    under test; they skip instead. The probe is the environment's truth:
    an agent inside its sandbox cannot forge it — nothing it does makes
    applying a second profile succeed — and "nested" needs both the
    kernel's refusal and the kernel reporting this process sandboxed.

    A probe that fails any other way on a host raises SandboxError, and
    the run stops here with a usage error: a broken seatbelt stack must
    fail the suite, never quietly skip the sandbox tests (README /
    CLAUDE.md: F-05 tests fail, never skip; review of #56/#57).

    functools.wraps carries the real function's identity onto the
    wrapper, so introspection tests (docstring contracts) still see the
    truth.
    """
    from factory import sandbox as sandbox_module

    try:
        can_apply = sandbox_module.can_apply_seatbelt()
    except sandbox_module.SandboxError as exc:
        raise pytest.UsageError(
            f"INC-007 seatbelt probe failed on this host: {exc}. The F-05 "
            "sandbox tests fail, never skip — fix the host's sandbox stack "
            "(README, Setup)."
        ) from exc
    if can_apply:
        return

    def _nested_unavailable(*args, **kwargs):
        pytest.skip("nested seatbelt cannot apply a profile (INC-007)")

    sandbox_module.run_sandboxed = functools.wraps(
        sandbox_module.run_sandboxed
    )(_nested_unavailable)
