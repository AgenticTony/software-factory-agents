# Factory incident register

Every factory incident adds an entry here and a test in the same PR
(BUILD-ORDER.md F-24 AC-5). Each entry names three things: what broke,
which layer should have caught it, and the test that now does. A test in
`tests/test_f24.py::test_ac_5` fails if any entry is missing a field or
names a test that does not exist, so the register and the suite cannot
drift apart.

The first three entries are the bugs the JobFinderOS readiness rehearsal
found on 2026-10-18 (PR #24) — the run that motivated F-24: none of them
could fail CI, because every fixture the factory's own tests used was a
toy repo.

## INC-001 — L1 deleted what setup installed (2026-10-18, F-08)

- **What broke:** L1's between-phase `git clean -fdx` removed
  `backend/.venv` and `frontend/node_modules`, so every contract ref
  failed with exit 127 in both phases and a healthy test read as
  `test_not_green`. The gates had never run against a repo where setup
  installs anything.
- **Which layer should have caught it:** the factory's own test suite —
  an L1 test with a fixture that declares `paths.setup_outputs` and
  actually installs something would have gone red the moment `_clean`
  wiped it. Toy fixtures with nothing installed hid the whole class.
- **The tests that now do:**
  - `tests/test_f08.py::test_rehearsal_clean_keeps_declared_setup_outputs`
  - `tests/test_f24.py::test_ac_3` (reverts the fix; the rehearsal fails)

## INC-002 — Sandboxed commands inherited home dirs on PATH (2026-10-18, F-05)

- **What broke:** the ambient PATH starts with `~/.local/bin`,
  `~/.pyenv/shims` and similar, which the sandbox denies reading. npm's
  lifecycle spawn probed a denied dir first and died with `spawn EPERM`,
  so `setup` failed inside a sandbox that had passed every F-05 test —
  those tests ran with the CI runner's short, home-free PATH.
- **Which layer should have caught it:** the sandbox tests: they asserted
  the built environment's allow/deny lists but never ran a command under
  the owner's real interactive PATH. Realism of the ambient environment,
  not just of the settings file, is the sandbox's acceptance surface.
- **The tests that now do:**
  - `tests/test_f05.py::test_rehearsal_ambient_path_drops_home_entries`
  - `tests/test_f05.py::test_rehearsal_sandboxed_child_sees_no_home_path_entries`
  - `tests/test_f24.py::test_ac_3` (reverts the fix; the rehearsal fails)

## INC-003 — The run built on the local checkout's stale main (2026-10-18, F-10)

- **What broke:** the work clone came from the local repo at
  `default_branch`, which was a week behind GitHub (`b0e2417` against
  `9daedd1`). The agent built on stale code and the PR carried a wrong
  base. Every orchestrator test used a remote that never moved, so the
  gap between local clone and remote was invisible.
- **Which layer should have caught it:** the orchestrator's tests — a
  fixture whose remote advances after the clone is made would have shown
  the run starting from the wrong commit.
- **The tests that now do:**
  - `tests/test_f10.py::test_rehearsal_run_builds_on_the_remote_default_branch`
  - `tests/test_f10.py::test_rehearsal_run_refuses_when_the_remote_is_unreachable`
  - `tests/test_f24.py::test_ac_3` (reverts the fix; the rehearsal fails)
