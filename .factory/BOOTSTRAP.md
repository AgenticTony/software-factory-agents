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

---

## F-03 — State machine and picker

- **Commits:** `feat/f03-state-machine` → PR (see repo). Red-first: the suite failed on the missing `factory.state` module before implementation.
- **Built:** 2026-10-16. Interactive session.
- **Design notes:** TASK-CONTRACT §3's diagram as an explicit 19-edge `ALLOWED_TRANSITIONS` table; the eight `X -> draft` edges exist solely for "contract changed" (§2), with `demote_changed_contracts()` as their caller — a demoted task needs human re-approval (F-04), so it can never auto-retry. `not_buildable` has no outgoing edges (importer sets it at creation). The §4 picker is a pure function plus a `TaskQueue` store that is the only write path: validate (F-02) → check `attempts` grew only → temp file + `os.replace` → roll back memory on any failure.
- **AC-1** proven: `tests/test_f03.py::test_ac_1` walks **all** 100 status pairs — the 19 table edges return their reason, the other 81 raise `TransitionError`; the store-level refusal leaves file and memory untouched. One test per illegal edge class: `test_illegal_edge_class_raises` (self / forward skip / backward / lateral / exit-from-needs_human / exit-from-not_buildable).
- **AC-2** proven: `test_ac_2` — any `in_progress` task makes `pick_next` and `TaskQueue.pick` return None.
- **AC-3** proven: `test_ac_3` — P1 later in the queue beats P2 earlier; equal priority resolved by queue order; draft/blocked/in_review and decision/research/owner_only skipped; `docs` picked.
- **AC-4** proven: `test_ac_4` — a `repo:id` dep is never met, even when a task with that literal id sits `done` in the same queue; same-repo dep on a `done` task is met (`test_dependency_must_be_done_in_this_queue` covers open and missing deps).
- **AC-5** proven: `test_ac_5` — external acceptance edit without a hash update → `demote_changed_contracts()` returns the id, persists `draft`; a matching hash leaves the task untouched.
- **AC-6** proven: `test_ac_6` — `os.replace` crashing mid-write leaves the previous `tasks.json` byte-identical, loadable, and no temp junk behind; `test_crash_during_serialisation_leaves_file_intact` covers the earlier crash window.
- **Extra invariants tested:** append-only `attempts` (rewrite, clear, and task-removal each refused with file intact), `save` refuses schema-invalid state, attempts are schema-validated on append, `updated_at` stamped per write, slice selection (first incomplete / fall-through fully merged / unsliced = one unit).
- **Status to set at reconcile:** `done` once this PR's CI is green (ruff + pytest on 3.11 + 3.13) and you merge it. No deploy target, so L4 doesn't apply.
