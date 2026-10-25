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


# INC-007: macOS cannot apply a seatbelt profile from inside one, so a
# sandboxed run of this suite (the L0 gate runs it inside the F-05
# sandbox, F-10 AC-11) can never spawn a second srt. Tests that reach
# for run_sandboxed would fail on the kernel's refusal, not on the code
# under test; they skip instead — the probe is the environment's truth
# (an agent inside its sandbox cannot forge it, nothing it does makes
# applying a second profile succeed), and the skipped tests run for
# real in CI's unsandboxed run of the same PR.
from factory import sandbox as _sandbox_module  # noqa: E402

if not _sandbox_module.can_apply_seatbelt():
    def _nested_unavailable(*args, **kwargs):
        pytest.skip("nested seatbelt cannot apply a profile (INC-007)")
    _sandbox_module.run_sandboxed = _nested_unavailable
