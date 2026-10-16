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
- **AC-2** proven: the CI `test` job passed on [PR #1](https://github.com/AgenticTony/software-factory-agents/pull/1) (ruff + pytest, Python 3.12), and again on the review-fix [PR #2](https://github.com/AgenticTony/software-factory-agents/pull/2) under the 3.11 + 3.13 matrix (all checks SUCCESS; merged).
- **AC-3** proven: `tests/test_f01.py::test_ac_3` passes locally.
- **Status to set at reconcile:** `done`. AC-2's matrix-CI contingency cleared with PR #2's green run and merge; no deploy target, so L4 doesn't apply.

---

## F-02 — Contract loading and validation

- **Commits:** `f-02/contract-loading` → [PR #4](https://github.com/AgenticTony/software-factory-agents/pull/4). First push drew 10 review findings (test names not matching contract refs; two inputs crashed instead of raising ContractError — unhashable `kind`, missing file; ledger not updated); all fixed on the same PR.
- **Built:** 2026-10-16. Interactive session, red-first (suite failed on the missing `factory.contract` module before implementation), then a code review the same day.
- **AC-1** proven: `tests/test_f02.py::test_ac_1` passes — the four named rejections, each with a task-named readable error. Crash paths from the review are covered by `test_missing_and_malformed_files_are_refused_not_raised` and `test_unhashable_field_values_are_refused_not_crashed`.
- **AC-2** proven: `tests/test_f02.py::test_ac_2` passes — order-insensitive, change-sensitive, exact SHA-256/canonical-JSON formula.
- **AC-3** proven: `tests/test_f02.py::test_ac_3` passes — the real `tasks.json` loads and validates; uniqueness/prefix asserted, contents deliberately not pinned.
- **Status to set at reconcile:** `done` once PR #4's CI is green and you merge it. No deploy target, so L4 doesn't apply.
