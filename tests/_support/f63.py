"""F-63 test support: record a fixture's owner approvals (one call).

The ledger is the source of approval truth from F-63 on, so every test
fixture that drives ``run_once``/``run_loop`` (or the importer session)
must first record what the owner would have approved: the profile and
every task in a frozen-contract status. This is the ONE helper for that
(F-63 AC-1's fixture rule) — no test builds ledger files by hand.
"""

from __future__ import annotations

from pathlib import Path

from factory.approvals import (
    ApprovalLedger,
    changed_groups,
)
from factory.contract import load_profile, load_tasks
from factory.state import CONTRACT_FROZEN_STATUSES

# Statuses whose tasks need a ledger entry: everything that implies a
# frozen contract except done (merged work needs no approval to rest).
ENTRY_STATUSES = CONTRACT_FROZEN_STATUSES - {"done"}


def _approve_profile_as_owner(root: Path | str, profile: dict) -> None:
    """Record the profile approval the way ``factory approve --profile``
    does — approved_by an OWNER, never the fixture approver: the fixture
    path (R2-3) is eval/rehearsal production code, and its mixing guard
    refuses to touch a ledger holding owner entries like these."""
    ledger = ApprovalLedger.for_project(root, profile["name"])
    approval = ledger.latest_profile_approval()
    if approval is None or changed_groups(profile, approval):
        ledger.record_profile_for(profile, approved_by="owner")


def importer_fixture_approve(root: Path | str, repo: Path | str) -> None:
    """Record the profile approval for a repo whose REAL import session
    a test drives (``run_import``'s session path checks the profile
    against the ledger before the session starts)."""
    _approve_profile_as_owner(
        root, load_profile(Path(repo) / ".factory" / "project.yaml")
    )


def ledger_approve(
    root: Path | str,
    queue_path: Path | str,
    *,
    tasks: bool = True,
    profile: bool = True,
) -> None:
    """Record the fixture's owner approvals into ``<root>/approvals/``.

    Mirrors what ``factory approve`` records, for a fixture queue built
    the way tests build them (hand-minted frozen statuses with correct
    hashes): one ``task`` entry per task whose status implies a frozen
    contract, hashed over the task's CURRENT content, plus the profile
    next to the queue (loaded the way ``run_once`` loads it, so the AC-2
    group hashes match exactly).

    ``tasks=False`` records only the profile — the shape AC-1's
    hand-minted-ready test needs (the profile is approved, the task is
    not). A queue whose ``project`` disagrees with the profile's name
    (the F-62 pair-mismatch fixtures) gets the profile approval only:
    its point is the pair refusal, and its task entries would live in
    the wrong project's ledger anyway.

    Idempotent per (task, contract_hash): re-recording an unchanged task
    appends a duplicate entry, which the any-match rule treats
    identically.
    """
    queue_path = Path(queue_path)
    doc = load_tasks(queue_path)
    loaded = load_profile(queue_path.parent / "project.yaml")
    if profile:
        _approve_profile_as_owner(root, loaded)
    if tasks and loaded["name"] == doc["project"]:
        ledger = ApprovalLedger.for_project(root, doc["project"])
        for task in doc.get("tasks", []):
            if task.get("status") in ENTRY_STATUSES:
                ledger.record_task_for(task)


# R1 fix 2: EVERY factory module that binds FACTORY_ROOT_DEFAULT (its own
# `from factory.runs import …` copy — each is an independent name) must be
# listed here. The suite's autouse pin (tests/conftest.py) redirects them
# all to a per-test tmp dir so no test can write the owner's real
# ~/factory; test_factory_root_default_pin_is_complete fails when a new
# module binds the default without joining this list. runs.py defines it
# (patched too — function-local imports in eval.py bind from it at call
# time); dashboard.py resolved its default-arg at import, so
# make_server now takes None and resolves at call time to make the pin
# effective there as well.
FACTORY_ROOT_PINNED_MODULES = (
    "factory.runs",
    "factory.approvals",
    "factory.approve",
    "factory.cli",
    "factory.dashboard",
    "factory.eval",
    "factory.importer",
    "factory.orchestrator",
    "factory.session",
)


def modules_binding_factory_root_default() -> list[str]:
    """Every factory submodule holding a ``FACTORY_ROOT_DEFAULT`` name."""
    import importlib
    import pkgutil

    import factory

    names = []
    for info in pkgutil.iter_modules(factory.__path__):
        module = importlib.import_module(f"factory.{info.name}")
        if hasattr(module, "FACTORY_ROOT_DEFAULT"):
            names.append(module.__name__)
    return sorted(names)


def pin_factory_root_default(monkeypatch, target) -> None:
    """Redirect every pinned module's ``FACTORY_ROOT_DEFAULT`` to
    ``target`` (the conftest autouse fixture's one call)."""
    import importlib

    for name in FACTORY_ROOT_PINNED_MODULES:
        # raising=False: factory.eval binds the name inside functions, so
        # the module attribute may not exist yet — setting it (a) is
        # harmless and (b) makes any later module-level import patched too.
        monkeypatch.setattr(
            importlib.import_module(name), "FACTORY_ROOT_DEFAULT", target,
            raising=False,
        )
