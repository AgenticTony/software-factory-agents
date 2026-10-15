# Bootstrap ledger

Until `factory approve` exists (F-04), nothing can legally write `.factory/tasks.json`, so its statuses stay `draft`. Progress during the bootstrap is recorded **here** instead, one entry per work order, with evidence.

F-04 AC-4 reconciles this ledger into `tasks.json`: each entry is approved and set to its final status through the normal state machine. After that, this file is frozen.

Entries record evidence, not claims. Each criterion is marked proven, pending, or not met.

---

## F-01 — Repo scaffold

- **Commits:**
  - `c997c7b` on `main`. This went straight to `main` before the branch rule existed, and was pushed.
  - Review fixes on `fix/f01-review`.
- **Built:** 2026-10-16. Interactive session, then a code review (10 findings) the same day.
- **AC-1** proven: `tests/test_f01.py::test_ac_1` passes locally.
- **AC-2** proven: the CI `test` job passed on [PR #1](https://github.com/AgenticTony/software-factory-agents/pull/1) (ruff + pytest, Python 3.12). The review changed CI to a 3.11 + 3.13 matrix, so the fix PR's CI run must also pass before reconcile.
- **AC-3** proven: `tests/test_f01.py::test_ac_3` passes locally.
- **Status to set at reconcile:** `done` once the fix PR's CI is green and you merge it. There's no deploy target, so L4 doesn't apply.
