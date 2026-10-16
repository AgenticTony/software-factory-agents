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

- **Commits:** `feat/f03-state-machine` → [PR #6](https://github.com/AgenticTony/software-factory-agents/pull/6). Red-first: the suite failed on the missing `factory.state` module before implementation. First push drew 10 review findings (2 reproduced: concurrent writers silently losing updates; `ensure_ascii` escaping every non-ASCII character); all 10 fixed on the same PR, see the review-round notes below.
- **Built:** 2026-10-16. Interactive session, then a review round the same day.
- **Design notes:** TASK-CONTRACT §3 as an explicit 23-edge `ALLOWED_TRANSITIONS` table; the eight `X -> draft` edges exist solely for "contract changed" (§2), with `demote_changed_contracts()` as their caller — a demoted task needs human re-approval (F-04), so it can never auto-retry. `not_buildable` has no outgoing edges (importer sets it at creation). The §4 picker is a pure function plus a `TaskQueue` store that is the only write path: validate (F-02 `validate_queue`, new public fn in contract.py) → append-only attempts check → temp-file+`os.replace` atomic write → roll back memory on any failure.
- **Review-round changes (all 10 findings):**
  - **Concurrent writers** (reproduced): `save()` now takes an exclusive `flock` on `tasks.json.lock` (never renamed, so it's a stable lock point), re-reads the queue under it, and refuses with `ConcurrentWriteError` if the file changed since load — `factory loop` and a human `factory approve` can no longer lose each other's writes. The append-only check now runs against the queue re-read from disk, not the stale snapshot.
  - **Slice continuation:** `deploying → ready` added; guards enforce that `done`/`awaiting_owner` require every slice merged and continuation requires an unmerged one. TASK-CONTRACT §3 gained a recovery/continuation edge table.
  - **Stale `in_progress`:** `in_progress → ready` (aborted run) edge + `reclaim_abandoned(now)` using last-attempt `started` + `budget.wall_clock_min`.
  - **Reclaim follow-up (2nd review):** the timestamp rule could reclaim a *live* run (setup and validation sit outside the session clock) and missed a crash before the first attempt record. Replaced with a **run lease**: an OS `flock` taken by `begin_run()` before the `ready → in_progress` transition and held for the session's life. `reclaim_abandoned()` acts only when the lease is free. Proven by `test_reclaim_never_takes_a_live_run`, `test_reclaim_frees_a_crash_before_the_first_attempt`, `test_begin_run_refuses_a_second_concurrent_run`, and `test_lease_is_freed_when_the_holding_process_dies` (a real subprocess exits via `os._exit` while holding the lease).
  - **`done` on criteria change:** decision recorded — `done` tasks are never auto-demoted; `demote_changed_contracts` returns them in `flagged_done` for owner review. Reopening (`done → draft`) stays a human action. TASK-CONTRACT §2 updated.
  - **`ensure_ascii=False`** (reproduced): the queue keeps literal em-dashes/arrows; no escape-noise diffs.
  - **Duplicate task ids** now rejected in `validate_queue` with a readable error.
  - **`StateError`/`TransitionError`/`ConcurrentWriteError` subclass `ContractError`** so `except ContractError` handlers catch state failures too.
  - **PR rejection:** `in_review → in_progress` (fix round) edge.
  - **Dependency drift:** `ready → blocked` edge + `sync_readiness()` reconciling both directions; cross-repo deps keep a task blocked.
  - **Picker O(n²):** the id index is built once per `pick_next` call.
- **AC-1** proven: `tests/test_f03.py::test_ac_1` walks **all** 100 status pairs — the 23 table edges return their reason, the other 77 raise `TransitionError`; the store-level refusal leaves file and memory untouched. One test per illegal edge class: `test_illegal_edge_class_raises` (self / forward skip / backward / lateral / exit-from-needs_human / exit-from-not_buildable).
- **AC-2** proven: `test_ac_2` — any `in_progress` task makes `pick_next` and `TaskQueue.pick` return None.
- **AC-3** proven: `test_ac_3` — P1 later in the queue beats P2 earlier; equal priority resolved by queue order; draft/blocked/in_review and decision/research/owner_only skipped; `docs` picked.
- **AC-4** proven: `test_ac_4` — a `repo:id` dep is never met, even when a task with that literal id sits `done` in the same queue; same-repo dep on a `done` task is met (`test_dependency_must_be_done_in_this_queue` covers open and missing deps).
- **AC-5** proven: `test_ac_5` — external acceptance edit without a hash update → `demote_changed_contracts()` demotes to `draft` and persists; a matching hash leaves the task untouched; `test_done_task_with_changed_contract_is_flagged_not_demoted` covers the `done` carve-out.
- **AC-6** proven: `test_ac_6` — `os.replace` crashing mid-write leaves the previous `tasks.json` byte-identical, loadable, and no temp junk behind; `test_crash_during_serialisation_leaves_file_intact` covers the earlier crash window.
- **Extra invariants tested:** append-only `attempts` (rewrite, clear, and task-removal each refused with file intact), `save` refuses schema-invalid state, attempts are schema-validated on append, `updated_at` stamped per write, slice selection (first incomplete / fall-through fully merged / unsliced = one unit), concurrency (both a lost-append and a lost-transition scenario), non-ASCII round-trip, duplicate-id rejection, error hierarchy, new edges, `reclaim_abandoned`, `sync_readiness`.
- **Status to set at reconcile:** `done` once this PR's CI is green (ruff + pytest on 3.11 + 3.13) and you merge it. No deploy target, so L4 doesn't apply.

---

## F-04 — `factory approve`

- **Commits:** `feat/f04-approve` → PR (this one). Red-first: the suite failed on the missing `factory.approve` module before implementation.
- **Built:** 2026-10-16. Interactive session, then this ledger entry.
- **Design notes:** new `factory/approve.py` — `approve_task` (render contract → checks → confirm → freeze `acceptance_hash`/`approved_at`/`approved_by` → `draft -> ready|blocked` per the picker's dependency rule) and `reconcile_bootstrap` (parse BOOTSTRAP.md, per interactive entry approve + walk forward one legal F-03 transition at a time to the recorded status). Wired into `factory/cli.py` as a real subcommand (`--yes`, `--reconcile-bootstrap`, `--queue`, `--ledger`, `--approved-by`; refusals exit 1, never a traceback). `factory/state.py` gained public `unmet_dependencies`/`dependencies_met` (the §4 rule, named for reuse) and `now_iso` (renamed from `_now_iso` so approval stamps timestamps in the queue's style). `gates.spec_approval` is **not** touched by approval: every task in this repo's queue already carries `true` at draft, so it reads as "this gate applies", not "approved" — the frozen-contract markers are the three fields above.
  - Reconcile rules beyond the literal WO text, recorded for review: a criterion the ledger never mentions also blocks `done` (evidence per criterion is what the ledger exists to record); entries whose dependency isn't reconciled yet fail with the dep named (reconcile runs in file = dependency order); already-reconciled entries are skipped, so a re-run after a partial reconcile is safe; a declined entry is skipped, and its dependents then fail on the open dependency.
- **AC-1** proven: `tests/test_f04.py::test_ac_1` — empty `acceptance` refused with a readable error, file byte-identical.
- **AC-2** proven: `test_ac_2` — dep-free → `ready`; open dep → `blocked`; done dep → `ready`; hash = the F-02 formula, `approved_at`/`approved_by` stamped. Cross-repo dep stays `blocked` (extra test).
- **AC-3** proven: `test_ac_3` — `--yes` on a risk-flagged task refused before anything is printed or written; interactive confirmation approves it.
- **AC-4** proven: `test_ac_4` — fixture ledger: F-1/F-2 reconciled to `done` through legal transitions in dependency order, one interactive confirmation per entry that gets that far; the pending entry is refused *before* asking (no point confirming a refusal), stays `draft`, no hash. Extra tests: unmentioned criterion also blocks `done`; declined entry skipped with dependents failing on the dep; second run skips; intermediate target (`in_review`) walks the full path; risk-flagged entry refuses `--yes`; the parser and a full reconcile run against **this repo's real** BOOTSTRAP.md + tasks.json (F-01..F-03 → done, F-04 untouched).
- **Extra coverage:** non-draft and non-executable-kind refusals, declined confirmation writes nothing, contract rendering includes ids/texts/checks, CLI end-to-end (`--yes` exit 0, risk `--yes` exit 1, `--reconcile-bootstrap`, task-id/reconcile exclusivity).
- **Smoke-tested by hand** on a copy of the real queue: `approve F-04 --yes` → `blocked` (F-03 still draft); interactive `y` on F-11 → `blocked`; `approve F-05 --yes` (risk) → refused, exit 1; `--reconcile-bootstrap` with three `y`s → F-01..F-03 `done`.
- **Status to set at reconcile:** `done` once this PR's CI is green (ruff + pytest on 3.11 + 3.13) and you merge it. No deploy target, so L4 doesn't apply.
