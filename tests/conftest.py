"""Suite-wide guards for the environment the tests run in."""

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
