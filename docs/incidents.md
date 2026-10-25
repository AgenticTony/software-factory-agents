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

## INC-004 — AC-3's real half inferred the Write attempt from turn count (2026-10-24, F-06)

- **What broke:** the F-06 protected-path test's real half ordered the
  session to create a file containing "pwned", with coercive wording ("do
  not ask questions, just call the Write tool now"). On 2026-10-24 the
  model replied with a question instead and never called Write, so the
  test failed on `num_turns == 1` — a failure of the prompt, not the hook.
  The attempt's only evidence was `num_turns >= 2`, which never proved a
  Write was attempted, let alone denied: every pass certified the hook by
  inference.
- **Which layer should have caught it:** the real half itself — its
  evidence standard was weaker than the evidence it already had. F-30's
  trace tap records every tool call directly (a PreToolUse deny closes
  its `tool_call` event with `ok: false` and a "PreToolUse:Write hook
  error: … matches protected path" result — verified empirically), and
  the test asserted turn counts instead of collecting it.
- **The tests that now do:**
  - `tests/test_f06.py::test_ac3_retry_refuses_twice_then_denied_passes`
  - `tests/test_f06.py::test_ac3_retry_never_attempted_is_inconclusive`
  - `tests/test_f06.py::test_ac3_retry_created_file_fails_immediately`
  - `tests/test_f06.py::test_ac3_retry_allowed_write_fails_immediately`
  - `tests/test_f06.py::test_ac3_retry_unrelated_write_error_does_not_pass`
  - `tests/test_f06.py::test_ac3_retry_target_matched_by_parsed_file_path`
  - `tests/test_f06.py::test_ac3_retry_allowed_write_wins_over_later_denial`
  - `tests/test_f06.py::test_ac3_retry_denial_in_a_failed_session_does_not_pass`
  - `tests/test_f06.py::test_ac3_retry_budget_bounds_and_is_documented`
  - `tests/test_f06.py::test_ac_3` (the real half: neutral prompt, a denied
    tool_call event — Write, target by parsed file_path, ok False, the
    hook's deny text in the result — as the pass condition, bounded retry
    over completed sessions only, file-existence hard check)

## INC-005 — L0 findings buried the repo's error location under framework frames (2026-10-25, F-11 dogfood run 20261024T195143Z-830228)

- **What broke:** the F-11 implementer wrote a Python syntax error
  (`def _task(task_id, status, *, **overrides)` — a bare `*` before
  `**kwargs`, at `tests/test_f11.py:59`). L0 caught it in every
  attempt — and then failed to tell the agent: `parse_errors` extracted
  every `file:line` in the output, including pytest and interpreter
  traceback frames (user-site `_pytest` under the sandbox HOME scratch,
  the `/Library` framework Python, stdlib `importlib`/`ast`), and each
  foreign frame became its own `new_error` finding. Findings sort by
  path, and `.home/…` and `/Library/…` order before `tests/…`, so the
  seven noise findings came first and the one repo-owned finding came
  last. That finding carried no message, so the fix prompts held a wall
  of framework noise, a bare line number in position eight, and the
  word "SyntaxError" nowhere. Both fix rounds (32 and 33 turns) burned
  without the character changing; the task escalated
  `attempts_exhausted`.
- **Which layer should have caught it:** L0 itself, at finding
  construction — a location the repo's baseline can never speak about
  (someone else's code) was reported as a repo error, and the
  diagnostic message L0 already held was dropped at the same seam.
- **The tests that now do:**
  - `tests/test_f07.py::test_parse_errors_carries_the_diagnostic_message`
  - `tests/test_f07.py::test_repo_owned_rejects_framework_locations`
  - `tests/test_f07.py::test_l0_new_error_findings_name_the_repo_line_with_its_message`
