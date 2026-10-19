# Factory Build Order

The factory's own build plan, written in the format the factory will consume: one work order (F-xx) per task, with checkable acceptance criteria, risk flags and owner steps. The machine-readable copy is [`.factory/tasks.json`](.factory/tasks.json). It validates against [`schema/tasks.schema.json`](schema/tasks.schema.json), and every task starts in `draft` until you approve it, per [TASK-CONTRACT.md](TASK-CONTRACT.md) §6.

**Bootstrapping:** the factory can't build itself before it exists. F-01 to F-10 were built **interactively**, by you plus a Claude Code session, one WO per session. The hardening WOs (F-23 to F-27) are built the same way, because they change what judges agents. Then F-28 hands the factory its own safe WOs (F-11, F-12) as the first real loop, on a repo where a mistake only hurts the factory itself. Outside projects (F-13 onward) come only after that loop has a measured record.

**Stack:** Python ≥ 3.11 (CI: 3.11 + 3.13), `pytest`, `ruff`, JSON Schema validation, Claude Agent SDK (`claude-agent-sdk`, pinned) for agent sessions, `gh` for PRs, `srt` / Claude Code sandbox for isolation. No web framework; it's a CLI.

---

## Queue

| # | Work order | Phase | Depends on | Risk | Size |
|---|---|---|---|---|---|
| **F-00** | Phase 0 owner prerequisites | 0 | — | — | owner |
| **F-01** | Repo scaffold | 1 | — | — | S |
| **F-02** | Contract loading and validation | 1 | F-01 | — | S |
| **F-03** | State machine and picker | 1 | F-02 | — | M |
| **F-04** | `factory approve` | 1 | F-03 | — | S |
| **F-05** | Clone manager and sandbox | 1 | F-01 | external_service | M |
| **F-06** | Session runner (Claude Agent SDK on Z.ai) | 1 | F-05 | external_service | M |
| **F-07** | L0 deterministic gates | 1 | F-02 | — | M |
| **F-08** | L1 red-first proof | 1 | F-07 | — | S |
| **F-09** | Agent prompts + L2 review | 1 | F-06 | — | M |
| **F-10** | `factory run` / `factory loop` + PR creation | 1 | F-03, F-04, F-06, F-07, F-08, F-09 | deploy | M |
| **F-11** | `factory status`: daily summary + metrics | 1 | F-03, F-23, F-24, F-25, F-26 | — | S |
| **F-12** | Importer (`factory import`) | 1 | F-06, F-02, F-23, F-24, F-25, F-26 | — | M |
| **F-13** | Onboard JobFinderOS (pilot) | 1 | F-00, F-11, F-12, F-16, F-22, F-27, F-28 | cross_repo | owner |
| **F-14** | L4 `verify-live` + merged/deployed tracking | 2 | F-10 | deploy | M |
| **F-15** | L3 holdout runner | 2 | F-10 | — | M |
| **F-16** | Seeded-bug validator eval | 2 | F-07, F-08, F-09 | — | M |
| **F-17** | Onboard SoloAdventurer Web | 3 | F-13, F-14, F-15 | cross_repo | owner |
| **F-18** | Triage quarantine + first watcher | 3 | F-14 | external_service | M |
| **F-19** | Meta loop (delta edits to prompts) | 4 | F-11, F-16 | — | M |
| **F-20** | Cross-provider reviewer | 4 | F-09 | external_service | S |
| **F-21** | Per-category auto-merge | 4 | F-11, F-14, F-16, F-20 | deploy | M |
| **F-22** | Vetted agent toolkit (skills, plugins, MCP servers) | 1 | F-06, F-09, F-10 | factory_control | M |
| **F-23** | Review convergence | 1b | F-09, F-10 | factory_control | M |
| **F-24** | Realistic rehearsal fixture in CI | 1b | F-05, F-07, F-08, F-10 | factory_control | M |
| **F-25** | Sensitive-path sentinels | 1b | F-07 | factory_control | M |
| **F-26** | Merges in order | 1b | F-10 | factory_control | S |
| **F-27** | Vent tool | 1b | F-06, F-10 | factory_control | S |
| **F-28** | Dogfood pilot: the factory builds its own safe WOs | 1b | F-23, F-24, F-25, F-26, F-29 | — | owner |
| **F-29** | Owner-step tick (`factory tick`) | 1b | F-03, F-04 | factory_control | S |

Order to build in: **F-00 (in parallel) → F-01 → F-02 → F-03 → F-04 → F-07 → F-08 → F-05 → F-06 → F-09 → F-10** (done), **then F-29 → F-24 → F-23 → F-25 → F-26 → F-27 → F-28 → F-11 → F-12 → F-16 → F-22 → F-13.** The deterministic core (F-02–F-04, F-07, F-08) went first: it's pure Python and testable without any model. F-29 comes first because nothing else can close without it: F-00 and F-06 are stuck on owner steps no command can tick, and F-09/F-10 can't reconcile until F-06 is done. Hardening comes next, rehearsal fixture first so every later change is checked against a realistic repo. F-28 then runs the factory on its own F-11 and F-12. F-16 measures how much each layer catches before any outside project depends on it.

F-14 (live checks) and F-15 (holdouts) need a deployed project with real users, which the factory itself is not, so they arrive with the first outside project (Phase 2). F-21 stays last: it unlocks only on numbers F-11, F-14 and F-16 produce from real merges.

---

## Phase 0

### F-00 — Owner prerequisites
**Kind:** owner_only. Nothing here is buildable by a session.

Owner steps:
1. Email Z.ai support with two questions:
   - Are scheduled, unattended Claude Code runs (via the Claude Agent SDK) allowed on the Coding Plan? (The owner has decided to proceed; this is for the record.)
   - Does renewing a legacy Max plan keep the prompt-based quota?
2. Check the legacy term's end date on Z.ai's "My plan" page.
3. Check JobFinderOS's **production** env on Render for `GLM_BASE_URL`. If it's the Coding Plan endpoint (`api/coding/paas/v4`), move production to a metered key, or the Mistral-hosted path (MIG-WO5), **before F-13**.
4. Turn on branch protection on JobFinderOS `main`: PRs required, CI required, no force-push.
5. Create macOS keychain items for the live-check secrets: `JFO_PROBE_PASS`, `JFO_SUPABASE_ANON_KEY`.
6. Create the GitHub repo for the factory, private.

---

## Phase 1 — v0 core

### F-01 — Repo scaffold
Turn this folder into the factory repo: `git init`; Python package `factory/` with a `factory` CLI entry point; `pyproject.toml`; `pytest`, `ruff`; GitHub Actions CI; a `CLAUDE.md` for the repo; schemas under `schema/`; this repo's own `.factory/project.yaml`.

Acceptance:
- AC-1 `factory --help` lists `approve`, `run`, `loop`, `status`, `import`, `verify-live` (stubs allowed).
- AC-2 `pytest` and `ruff check` pass in CI on a PR.
- AC-3 `.factory/project.yaml` for this repo validates against `schema/project.schema.json`.

### F-02 — Contract loading and validation
Load `tasks.json` and `project.yaml`, validate both against the schemas on every load, and refuse to run on invalid state. Compute `acceptance_hash` as SHA-256 of canonical JSON: sorted keys, no whitespace.

Acceptance:
- AC-1 These are rejected with a readable error:
  - a risk-flagged task with `merge: auto`
  - a `decision` task in `ready`
  - a `ready` task without `acceptance_hash`
  - an invalid YAML profile
- AC-2 `acceptance_hash` is identical when the acceptance JSON's keys are reordered, and different when any text changes.
- AC-3 The `tasks.json` in this repo validates.

### F-03 — State machine and picker
Implement the transitions in TASK-CONTRACT §3 as an explicit allowed-transition table, and the picker in §4.
- Writes are atomic: temp file + rename. `attempts` is append-only.
- The orchestrator is the only writer.

Acceptance:
- AC-1 Every transition not in the table raises; one test per illegal edge class.
- AC-2 The picker returns nothing when a writer task is `in_progress` for the repo.
- AC-3 The picker respects priority, then queue order. It skips non-`ready` tasks and non-build kinds.
- AC-4 A `repo:id` cross-repo dependency is never treated as met automatically.
- AC-5 A task with an approved contract whose `acceptance` hash no longer matches goes back to `draft`.
- AC-6 A simulated crash mid-write leaves the previous `tasks.json` intact.

### F-04 — `factory approve <id>`
A human-run command. It prints the contract, asks for confirmation, sets `acceptance_hash`, `approved_at` and `approved_by`, then moves `draft` → `ready` (or `blocked` if a dependency is open).

Acceptance:
- AC-1 Approving a task with no acceptance items is refused.
- AC-2 After approval, the status is `ready` or `blocked` according to its dependencies.
- AC-3 `--yes` is not accepted when the task has risk flags. Risk-flagged approval is always interactive.
- AC-4 `factory approve --reconcile-bootstrap` reads `.factory/BOOTSTRAP.md`. For each entry, after interactive confirmation, it approves the task and moves it through legal transitions to its recorded status. A criterion marked "pending" can never produce `done`.

### F-05 — Clone manager and sandbox
- Create `~/factory/runs/<project>/<run-id>/` as a `git clone --filter=blob:none` from the local repo.
- Copy in a warm dependency cache.
- Generate the sandbox config: writes only in the run dir; reads denied for `~/factory/holdouts`, `~/.ssh`, `~/.config/gh` and other projects; network allowlist = model endpoint + `docs_allowlist` + `network_allowlist`.
- Clean up on completion.
- Pin the `srt` version.

Acceptance (tests must *attempt* the forbidden action, not just inspect config):
- AC-1 A sandboxed process can't write outside the run dir.
- AC-2 A sandboxed process can't read a file in `~/factory/holdouts`.
- AC-3 A sandboxed process can't reach a non-allowlisted domain, and can reach `api.z.ai`.
- AC-4 No `GH_TOKEN`/`GITHUB_TOKEN` or keychain-derived variable is visible inside the sandbox.

### F-06 — Session runner
`run_session(seat, prompt, cwd, max_turns, timeout_min)` calls `claude_agent_sdk.query()` with a `ClaudeAgentOptions` built per seat, inside the F-05 sandbox. The whole runner process runs under `srt`, so the Claude Code subprocess the SDK spawns inherits the sandbox.
- **Options:**
  - `cwd` = run dir
  - `env` = Z.ai base URL, auth token from the keychain, `API_TIMEOUT_MS`
  - `max_turns`; `system_prompt` = the seat prompt (F-09)
  - `setting_sources=[]`: no user/project settings, so a session can't pick up your personal Claude config
  - per-seat `allowed_tools` / `disallowed_tools`: the reviewer gets no Write/Edit
- **Hooks:** in-process `PreToolUse` callbacks via `HookMatcher`:
  - `Write|Edit|NotebookEdit` → deny if `tool_input.file_path` matches `paths.protected`
  - `Bash` → deny `git push`, `gh `, and anything touching `~/.config/gh`
- **Clock:** wall-clock is enforced with `asyncio.wait_for` around the message stream, and paused outside the session.
- **Record:** from `ResultMessage`: `num_turns`, `subtype`, `is_error`, `terminal_reason`, tokens, and the model name from `model_usage`.
- **Testability:** `query` is injected, so tests use a fake async generator instead of a real model.

Acceptance:
- AC-1 With an injected fake `query` that never finishes, the runner stops at the wall-clock limit and returns `aborted`.
- AC-2 A `ResultMessage` with an error subtype (`error_max_turns`, `error_during_execution`, or `is_error` with `api_error_status`), and an SDK exception, are each recorded as a failed attempt with the reason. Neither crashes the runner.
- AC-3 The protected-path hook returns `permissionDecision: "deny"` for an Edit/Write to a protected path (unit test on the callback), and blocks one inside a real session.
- AC-4 A real smoke session against Z.ai completes a trivial prompt, and the recorded model name is read from `model_usage`.
- AC-5 The built options have `setting_sources == []`. The session env contains no GitHub token and no keychain secret other than the Z.ai token.

Owner steps: F-00 step 1 answered before AC-4 is run repeatedly or on a schedule.

### F-07 — L0 deterministic gates
Implement VALIDATOR-SPEC L0:
- run `project.yaml` commands on a clean clone of the branch
- baseline comparison, porting the exact `file:line` rules from SoloAdventurer's `verifier.md`
- diff policy: protected paths, risk paths, dependency manifests, test deletion / `skip` / `xfail` / `.only`, removed assertions

Acceptance (each against a fixture repo):
- AC-1 A modified protected file → FAIL with `protected_path_touched`.
- AC-2 A changed dependency manifest → escalation `new_dependency`.
- AC-3 A touched risk path that wasn't declared → the flag is added and escalation is `risk_path_touched`.
- AC-4 A deleted test, or an added skip marker → FAIL.
- AC-5 A pre-existing error at a baseline `file:line` doesn't fail; a new error in the same file at another line does.

### F-08 — L1 red-first proof
Check out the base commit, apply only the diff's test files, run the contract's named tests (they must fail), apply the full diff (they must pass).

Acceptance:
- AC-1 A fixture where the named test already passes on base → FAIL ("test proves nothing").
- AC-2 A proper red-first fixture → PASS.
- AC-3 A contract `test` check naming a test that doesn't exist → FAIL with `contract_ambiguous`.

### F-09 — Agent prompts + L2 review
Generalise SoloAdventurer's `implementer.md` and `verifier.md` into project-agnostic seat prompts. The project specifics come from `project.yaml` and the task.
- The reviewer gets the contract + diff + L0/L1 output, not the implementer's narrative.
- The reviewer returns JSON `{verdict, findings[]}`.
- Prompts are versioned by content hash, pinned in a test (the JobFinderOS pattern).

Acceptance:
- AC-1 Reviewer output that isn't valid verdict JSON → treated as FAIL.
- AC-2 The prompt-hash test fails if a prompt file changes without the pin being updated.
- AC-3 The reviewer prompt includes the mandatory checklist from VALIDATOR-SPEC L2: real schema vs mock, scope, states, lifecycle.
- AC-4 Every reviewer finding carries `axis` (`spec` or `standards`). A finding without one is invalid verdict JSON (AC-1 applies). A `blocking` finding on either axis means FAIL, whatever the other axis says.
- AC-5 The assembled reviewer prompt contains the smell baseline, the test-quality checks and the project's `standards` files. The assembled implementer prompt contains none of the `standards` files.
- AC-6 `project.schema.json` accepts an optional `standards: [string]` and `tasks.schema.json` accepts an optional per-task `out_of_scope: [string]`. Unknown keys are still rejected. Both tests are red before the schema change.
- AC-7 The L1 report (F-08's `L1Report`) records a `red_reason` per test ref: `assertion`, `collection` or `unknown`. A fixture whose test imports a not-yet-existing module gives `collection`, and a failing assert gives `assertion`. The verdict doesn't depend on it.
- AC-8 Every vendored prompt file sits under `prompts/vendor/` next to its upstream `LICENSE` and the source commit SHA. A test fails if either is missing.

Prompt sources. The seat prompts adapt [mattpocock/skills](https://github.com/mattpocock/skills) @ `49dd158` (MIT), copied in and pinned by AC-2 rather than loaded as live skills:
- **Implementer:** `tdd`: red before green, one test and its minimal code per cycle, vertical slices (not every test first and then all the code), test anti-patterns, mocks only at system boundaries. Refactoring belongs to review, not the loop. His "confirm the seams with the user" step becomes "the contract's `test` refs are the agreed seams": the session is AFK.
- **Bug tasks** (from F-18): `diagnosing-bugs` Phase 1: a tight, red-capable reproduction command that has already been run once, before any hypothesis. If no correct seam exists for a regression test, that is reported as a finding and isn't papered over.
- **Reviewer:** `code-review`: the two axes and the smell baseline (VALIDATOR-SPEC L2). The issue-tracker lookup is dropped, because the contract is passed in directly.

### F-10 — `factory run` / `factory loop` + PR creation
- **`run`:** pick → clone → implementer session → L0 + L1 on a clean clone → L2 → (fail: findings back, up to `max_attempts`) → push the branch with the orchestrator's token → `gh pr create`, with the contract and validator report in the body → `in_review`.
- **`loop`:** repeats until the queue is empty, the quota stop is hit, the peak window starts, or there's an escalation.
- **Never merges.**

Acceptance:
- AC-1 End to end on a fixture repo, with a stub model that makes a correct change: a PR is opened and the task is `in_review` with one attempt logged.
- AC-2 A stub model that fails three times → `needs_human` with `attempts_exhausted`, and no PR.
- AC-3 A quota/429 error stops `loop` and marks `quota_exhausted`, with no retry.
- AC-4 `loop` won't start a session during 08:00–12:00 Mon–Fri Swedish time. The window comes from config.
- AC-5 The code contains no `gh pr merge` call (a grep test).
- AC-6 Every attempt records `prompt_versions` (seat → prompt content hash, from F-09) and `skills` (the skills loaded). An attempt without them is a schema error.
- AC-7 The PR body has these sections: **Summary**, **Evidence**, **Merge Danger** (Door + Blast Radius), **Contract**, **Validator report**. The PR isn't opened if any is missing.
- AC-8 **Evidence** is built by the orchestrator from the L1 report, not from model text. For each contract test ref it shows red on base (with `red_reason`) and green on head. Nothing the implementer writes can alter this section.
- AC-9 **Door** is `one-way` when the task carries any of `migration`, `deploy`, `external_service`, `payments` or `privacy` (data that has leaked can't be recalled), and `two-way` otherwise. **Blast Radius** lists the risk flags and matched `paths.risk` globs, or `contained`. The implementer may add prose below them but can't change either value.
- AC-10 An attempt that starts in a fresh session (re-queued run, rejected-PR fix round) receives the task's progress log, entries 1..N. The log is orchestrator-owned, lives outside the clone and is append-only: rewriting an earlier entry raises.
- AC-11 L0 and L1 run every project command (setup, lint, typecheck, test, and each `test_ref`) inside the F-05 sandbox via `run_sandboxed` on the clean clone, never in the orchestrator's own environment: the agent's `conftest.py` and test code are untrusted. A fixture whose `conftest.py` tries to write outside the run dir and read `~/factory/holdouts` is denied both during the L0 test gate and the L1 red/green runs.
- AC-12 Every run dir `run` creates is removed with `cleanup_run` on every outcome (PR opened, `needs_human`, quota stop, exception), together with its settings, staged runner and scratch. `loop` reclaims, at start, run dirs no live run lease holds. A run killed mid-session is cleaned up by the next `loop` start.
- AC-13 The orchestrator never pushes to the default branch: the push refspec names only the task's own branch, and a push targeting `repo.default_branch`, or any ref the run didn't create, raises before any git process starts. Every orchestrator-side git call uses `GIT_HARDENING`. (The factory's own repo can't have branch protection on a free private plan, so this check is the only guard on its `main`.)
- AC-14 The PR body also carries a **How to revert** section, built by the orchestrator: for a `two-way` Door, revert the squash-merge commit; for a `one-way` Door, a statement that reverting the code doesn't undo the listed risk flags, naming each one. The PR isn't opened without it (same rule as AC-7).

PR body shape adapted from mattpocock/skills `pr` (its Summary visuals credit Dex Horthy's `show-me`). The difference: Evidence and Merge Danger come from validator facts, not the agent's own account.

### F-11 — `factory status`: daily summary + metrics
Write a markdown summary per project:
- done, in review, needs you (with reasons), awaiting owner
- quota used this week
- metrics from DESIGN §9

Acceptance:
- AC-1 For a fixture `tasks.json`, the summary lists every `needs_human` task with its escalation reason.
- AC-2 Metrics computed from `attempts` match hand-computed values for the fixture.
- AC-3 The summary reports outcome measures only (RESEARCH-SOFTWARE-FACTORY §9): interventions per task, review rounds per attempt, tokens per merged PR, time from approval to merge, and the share of merged factory PRs later reverted (a revert commit naming the PR's merge commit). It never reports lines of code or PR counts as a measure of success.

### F-12 — Importer (`factory import`)
An importer session reads a project's planning docs and proposes `tasks.json`. Every task is `draft`. The orchestrator shows the full diff; nothing is written without confirmation.

Acceptance:
- AC-1 The output validates against the schema, or the import is refused.
- AC-2 Every imported task is `draft`.
- AC-3 Running it twice with no doc changes proposes no diff for existing tasks. Status and attempts are preserved.
- AC-4 The importer reads `.factory/out-of-scope/` (TASK-CONTRACT §5). A proposed task that resembles a record is listed in the import summary with the record's path. It's still proposed as `draft` and is never dropped or rejected on its own: you confirm the match.

## Phase 1b — Harden, then dogfood

The research's rollout order (RESEARCH-SOFTWARE-FACTORY §12; RESEARCH-AI-LAB-INTERNAL-FACTORIES §8) is: get ready, build the control plane, add verification depth, then let the loop run — on something narrow where a mistake is cheap. The 2026-10-18 JobFinderOS readiness rehearsal found three factory bugs no toy-repo test could catch (PR #24), before any agent ran. So before any outside project goes through the factory: harden what exists (F-23 to F-27, built interactively, because they touch what judges agents), then prove the loop on the factory's own safe work orders (F-28). Outside projects come after (F-13).

### F-29 — Owner-step tick (`factory tick`)
Owner steps can be recorded but never ticked: no command writes `done: true`, so F-06 sits at `awaiting_owner` after reconcile (its step is confirmed in the ledger), F-09/F-10 can't reconcile behind it, and F-00 (`owner_only`) has no path to `done` at all. F-13 and F-28 depend on both. The tick is a human action, so it is interactive and leaves evidence, like approval.
- AC-1 `factory tick <id> <step> [<step> ...] --evidence <text>` shows each named owner step (1-based) and asks for confirmation per step. A confirmed step gets `done: true`, `done_at`, `done_by` and `evidence` (additive schema fields; `done: true` without the other three is refused at load). `--evidence` is required and non-blank; there is no `--yes`.
- AC-2 An unknown task, a step number out of range, an already-ticked step, a blank evidence string, or a declined confirmation each leaves `tasks.json` byte-identical and exits non-zero with a message naming the problem. One tick call is one atomic save: a refusal on any named step writes none of them.
- AC-3 Ticking the last open step of an `awaiting_owner` task moves it to `done` in the same save. Ticking a step of a task in any other status records the tick and never changes the status.
- AC-4 An `owner_only` task whose last step is ticked moves `draft → not_buildable → done` through two new table edges. Both are guarded: only a non-executable kind may enter `not_buildable` from `draft`, and only an `owner_only` task with at least one step and every step ticked may go `not_buildable → done`. A `build` or `docs` task taking either edge raises (one test per edge).
- AC-5 Reconcile leaves `owner_only` tasks untouched: the real-files reconcile tests assert F-00 is unchanged by the run, not that it is `draft`, so they keep passing after F-00 is ticked to `done`.

### F-23 — Review convergence
Rounds are already capped by `max_attempts`, but every L2 round re-reviews the whole diff from scratch. Findings drift to pre-existing code, repeat, or chase the last fix's regression. Our own review record: F-05 took 5 rounds, F-09 4, F-10 3, several finding bugs the previous round's fix introduced. The Claude Code team reports one PR that went through 50+ overnight rounds before they changed the loop: re-review only the delta, never repeat a finding, fix the class not the instance.
- AC-1 Every L2 finding is stored per attempt with a stable key (file, rule or category, normalised summary). A finding whose key was raised in an earlier round and is unchanged in the diff since is not re-raised; the reviewer's verdict lists it as carried, not new.
- AC-2 Rounds after the first give the reviewer the delta since the last reviewed sha as primary scope, with the full diff for reference only. A new finding outside the delta must say why it was missed earlier, or it is dropped as out of scope.
- AC-3 The implementer's fix prompt carries each finding with an instruction to find and fix sibling sites of the same defect class, and the next round's reviewer checks for siblings explicitly.
- AC-4 No-progress stop: if a round's FAIL contains only findings whose keys were already raised and claimed fixed, the task escalates `review_not_converging` (added to the schema enum, red-first) instead of spending another round.

### F-24 — Realistic rehearsal fixture in CI
Every factory test today uses a toy repo: nothing installed, a short `PATH`, a remote that never moves. That is why L1 wiping setup outputs, the home-dir `PATH` leaking into the sandbox (npm `spawn EPERM`) and building on stale local `main` all passed CI. The labs' practice: every incident becomes a standing eval, and each review of an incident asks which layer should have caught it.
- AC-1 A fixture project (built by the test, no network) has a setup that installs a Python venv and a `node_modules` tree, declared as `setup_outputs`; a remote whose default branch is ahead of the local clone; and an ambient `PATH` that includes home-dir entries.
- AC-2 A rehearsal test runs setup, L0 and L1 over a one-criterion contract against that fixture, end to end through the real sandbox, and passes on `main`.
- AC-3 Reverting each PR #24 fix in turn (L1 `_clean` keeping setup outputs, `_path_outside_home`, `_build_on_remote_default`) makes the rehearsal fail. Proven by three tests that monkeypatch the old behaviour back in.
- AC-4 CI runs the rehearsal on every PR (it is not marked slow or optional).
- AC-5 `docs/incidents.md` records the three incidents, and for each: what broke, which layer should have caught it, and the test that now does. Every future factory incident adds an entry and a test in the same PR (checked by a test that every entry names an existing test).

### F-25 — Sensitive-path sentinels
Risk paths today force a human merge but nothing checks what the diff does there. Cursor runs about ten standing "security sentinel" invariants over sensitive PRs before a human looks; agents also cheat by weakening the tests that judge them.
- AC-1 L0 runs built-in sentinels on every diff and fails with the sentinel's name: a credential-shaped string added (key, token, private-key patterns); a new skip, xfail or `only` marker on an existing test; an assertion removed from an existing test without a replacement; a `# noqa`, `type: ignore` or lint-disable added on a line the diff also changed in a risk path.
- AC-2 `project.yaml` may add project sentinels under `sentinels.<risk flag>` (a list of command plus description), validated by the schema; `paths.risk` keeps its current shape. They run only when the diff touches that risk flag's globs, and a failure escalates `sentinel_failed` (schema enum, red-first).
- AC-3 Each built-in sentinel has a seeded fixture diff that it flags and a clean diff that it passes.
- AC-4 Sentinel results appear in the PR body and in the attempt record, pass or fail, so the human merge sees what was checked.

### F-26 — Merges in order
Two factory PRs that each pass on their own base can break `main` together. The labs solve it with merge queues; the small-scale version is "branch must be up to date before merging" plus re-validation on the new base. The research also says scale serially before scaling out.
- AC-1 `factory run` and `factory loop` refuse to start on a repo whose default-branch protection does not require status checks with `strict` (up to date) enabled, naming the missing setting (checked via `gh api`, faked in tests).
- AC-2 The loop keeps at most one open factory PR per repo by default (`concurrency: 1` in `project.yaml`, schema-validated). A ready task waits while one is open.
- AC-3 When the default branch has moved past an open factory PR's base, `factory run` re-runs L0 and L1 on the PR merged onto the new base before reporting it ready; a failure moves the task to `needs_human` with escalation `stale_base` (schema enum, red-first).

### F-27 — Vent tool
Lovable's pattern, via Cursor: give agents a tool to complain. The complaints became their best harness bug tracker. Agents push through friction that never shows up as a failure (F-19 AC-4); this records it at the moment it happens.
- AC-1 Implementer and reviewer sessions have one extra tool, `vent(category, text)`, an in-process SDK tool listed in `allowed_tools`. Categories: missing_tool, unclear_contract, flaky_check, environment, other.
- AC-2 Each call appends one record (run id, task id, seat, turn, category, text) to the run's progress log outside the clone. The tool can write nothing else, and text is length-capped.
- AC-3 Venting never changes a run's outcome or budget (a test vents mid-run and asserts the attempt record is otherwise identical).
- AC-4 `factory status` (F-11) lists the week's vents grouped by category, and F-19's review reads them.

### F-28 — Dogfood pilot: the factory builds its own safe work orders
**Kind:** owner_only. The first real loop runs on the factory itself, where a mistake only hurts the factory. Principle 5 still holds: agents never edit what judges them.

Owner steps:
1. Extend `paths.protected` in this repo's `.factory/project.yaml` (by PR) to cover what judges agents: `factory/l0.py`, `factory/l1.py`, `factory/l2.py`, `factory/gates.py`, `factory/sandbox.py`, `factory/session.py`, `factory/publish.py`, `factory/orchestrator.py`, `factory/prompts.py`, `prompts/**` and every existing `tests/test_f*.py`, on top of today's `.factory/**`, `.github/workflows/**`, `schema/**`, the two spec files and `CLAUDE.md`. Turn on branch protection for this repo's `main` with up-to-date status checks (F-26 refuses to run without it).
2. Readiness run on a clean clone: every profile command passes unattended (the F-24 rehearsal covers the mechanics).
3. Approve F-11 and F-12 only. Their code (`factory/cli.py` status and import commands, new modules) is outside every protected path; the orchestrator adds their own contract tests to the protected set at approval.
4. `factory run` once, watching; review and merge or reject the PR yourself. Then `factory loop` for the second.
5. Record per attempt: rounds, your review time, findings you raised that L2 missed, and whether the merged change needed a fix later. That is the baseline F-11 then reports automatically.

## Phase 1c — First outside project

### F-13 — Onboard JobFinderOS (pilot)
**Kind:** owner_only. This is the onboarding steps in TASK-CONTRACT §6, run on the real repo. It starts only after F-28 has a measured record on the factory's own queue. A readiness rehearsal already ran on 2026-10-18 (L0 passed; it found the three bugs fixed in PR #24); draft profile, baseline and contracts are in `projects/`, unapproved.

Owner steps:
1. Readiness run: every `project.yaml` command passes unattended on a clean clone.
2. Resolve every `TODO(verify)` in `projects/jobfinderos.project.yaml`; commit it as `JobFinderOS/.factory/project.yaml`.
3. Run `factory import`; read the full diff line by line; commit.
4. Approve 2–3 self-contained build WOs, not WO-13/16/17 (payments), WO-27 (decision) or WO-29/30 (research-gated).
5. Run `factory run` once, watching. Merge or reject the PR yourself.

---

## Phase 2 — Trust

### F-14 — L4 `verify-live` + merged/deployed tracking
`factory verify-live <id>` runs `live_checks` by `when`, with secrets from the keychain, outside the sandbox. It records merged and deployed as separate facts. On FAIL: open a revert PR, mark `needs_human` with `live_check_failed`, and pause the repo's queue. Only an L4 PASS (with owner steps done) reaches `done`.
- AC-1 A failing live check → revert PR opened, task `needs_human`, queue paused.
- AC-2 A task can't reach `done` without a recorded L4 PASS.
- AC-3 Secrets never appear in logs or the summary (a test with a canary value).

### F-15 — L3 holdout runner
Run the scenarios in `holdouts.dir` against a local build or preview, and check outcomes in the real store.
- AC-1 A holdout FAIL blocks the PR.
- AC-2 Holdout files are absent from every run dir and every prompt (a test).

### F-16 — Seeded-bug validator eval
A monthly job: seed 5–10 known bugs on a branch, run the validator, record the catch rate per layer.
- AC-1 The report shows the catch rate per layer for a fixture set of seeded bugs.
- AC-2 The phantom-table seed is caught by L0 or L2 on the SoloAdventurer fixture.

## Phase 3 — Second project and watchers

### F-17 — Onboard SoloAdventurer Web
**Kind:** owner_only. Same steps as F-13 with `projects/soloadventurer-web.project.yaml`. Every task depending on `soloadventurer-mobile:*` stays `blocked` until you mark the dependency met.

### F-18 — Triage quarantine + first watcher
A Sentry-alert → triage-session pipeline. That session has no secrets, can't push, and outputs only `draft` tasks for your approval.
- AC-1 The triage session's sandbox has no repo write access and no GitHub token.
- AC-2 Its output is schema-valid draft tasks only. Free text never reaches an implementer.
- AC-3 Triage checks `.factory/out-of-scope/` the same way the importer does (F-12 AC-4).
- AC-4 A bug draft carries at least one `test` acceptance item that reproduces the reported symptom. A signal that can't be turned into one is listed in the summary as needing info and doesn't become a draft.

## Phase 4 — Self-improvement and gate dial-down

### F-19 — Meta loop
A weekly review of escalations and L2 findings → PRs with **itemised delta edits** to prompt files. Edits target what actually caused failures. Every attempt records which prompt versions and skills it used (F-10 AC-6), and the review traces failures back to them. This is a non-trained version of the credit-assignment idea in market-based post-training ([arXiv 2610.22322](https://arxiv.org/abs/2610.22322)).
- AC-1 A PR that touches validators, holdouts, contract tests or schemas is rejected by L0.
- AC-2 Edits are additions, removals or changes to individual items, never whole-file rewrites (a diff-size check).
- AC-3 The weekly review groups failed and escalated attempts by `prompt_versions` and `skills`, and reports each version's failure share. Each proposed edit names the version it targets and the evidence (attempt run ids) behind it.
- AC-4 The review also reads the session transcripts of the attempts it samples, because agents push through friction that never shows up as a failure. Every proposal cites the run id and turn it comes from.
- AC-5 A proposal that targets anything other than a prompt file (a new check, CI, `standards`, steering files) comes out as a `draft` task for you, never as a meta-loop PR. Agents can't edit what judges them (Principle 5).

Review categories adapted from mattpocock/skills `retro`: navigation, automated checks, coding standards, steering-file bloat and no-ops, tool economy, information access. His warning applies too: a fully automated retro loop chases false positives, which is why every output is a proposal you review.

### F-20 — Cross-provider reviewer
A per-seat provider config. Risk-flagged tasks get a second L2 review from a different provider.
- AC-1 A risk-flagged task's PR shows two independent verdicts.

### F-21 — Per-category auto-merge
Enable `merge: auto` for a task category only when DESIGN §3's trigger is met: catch rate ≥ 90%, revert rate < 5%, over 30 merges.
- AC-1 The schema still rejects auto-merge on any risk-flagged task.
- AC-2 Auto-merge switches off automatically if the category's revert rate crosses the threshold.

### F-22 — Vetted agent toolkit (skills, plugins, MCP servers)
Agents today get only Claude Code's built-in tools: `setting_sources=[]` (F-06) rightly keeps the owner's personal Claude setup out of the sandbox, because it holds Gmail, production databases, deploy tools and app connectors. But it left agents with no skills at all, while F-10 AC-6 records `skills` and F-19 improves them. This work order adds the missing middle: a factory-owned, version-pinned toolkit, vetted before admission and chosen per project at onboarding.

Candidate sources (discovery only; admission pins the upstream repo): `anthropics/skills` (licence is per skill: some Apache-2.0, the document skills proprietary), `alirezarezvani/claude-skills` (MIT), `Jeffallan/claude-skills` (MIT), `punkpeye/awesome-mcp-servers`, the GitHub MCP Registry, mcpmarket.com.

Acceptance:
- AC-1 A toolkit index (`toolkit/index.yaml`, schema-validated on every load) lists each admitted item: kind (`skill`, `plugin` or `mcp_server`), name, upstream repo, pinned commit SHA (or package version plus integrity hash), SPDX licence, content hash, and the path of its vetting report. Vendored files live under `toolkit/` next to their upstream LICENSE. A vendored file whose content no longer matches its index hash is refused at load.
- AC-2 Licence gate: only allowlisted licences (MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC) are admitted. A skill whose licence is missing or proprietary (for example "All rights reserved", as on Anthropic's document skills) is refused, with the reason named.
- AC-3 `factory toolkit vet <path>` scans every file of a candidate, prompts and scripts alike, and fails on each of these classes: outbound network calls or URLs outside the project's allowlists; shell execution, eval or exec; encoded blobs above a size threshold; reads of credential locations (`~/.ssh`, `~/.aws`, `.env`, the macOS keychain via `security`); environment-variable harvesting; hidden characters (zero-width, bidi overrides, Unicode tag characters); prompt-injection phrasing (instructions to ignore earlier instructions, to hide actions from the user, or to exfiltrate); self-modification or auto-update. A seeded fixture for each class is flagged, and a clean fixture passes.
- AC-4 Admission is human: an item enters the index only through a PR carrying its vetting report, approved by the owner. A clean scan never admits on its own, and any version bump repeats the full vet. Aggregators (awesome lists, mcpmarket, the GitHub MCP Registry) are discovery sources only: the index always pins the real upstream repo.
- AC-5 MCP servers are admitted only when read-only (no tool that writes, sends or deploys), with no credentials in their environment, every network domain they need inside the project's allowlists, and a pinned version with an integrity hash. They run inside the session's sandbox, and their tools are enabled one by one through `allowed_tools`. A descriptor that needs a credential or exposes a write tool is refused.
- AC-6 Each project chooses its toolkit in `project.yaml` (`toolkit: {skills, plugins, mcp_servers}`, optionally narrowed per seat, so the reviewer can get a read-only subset). Every name must be an index entry, and an unknown or unvetted name refuses the profile at load, as F-02 does for invalid state.
- AC-7 A session gets exactly its project's toolkit and nothing from the owner's personal Claude setup. The test plants a skill and an MCP server in a fake personal config and asserts neither is visible, while every listed item is. The toolkit is loaded from a read-only staged copy outside the clone (the F-06 runner-staging pattern).
- AC-8 Every attempt's `skills` records what actually loaded, as `<name>@<content-hash>` (replacing the hardcoded empty list), so F-19 can attribute failures to toolkit versions.
- AC-9 The first JobFinderOS toolkit goes through the whole process end to end: at least one skill and one read-only documentation MCP server, vetted, admitted by owner-approved PR, with reports committed. A JobFinderOS session test shows both available.
