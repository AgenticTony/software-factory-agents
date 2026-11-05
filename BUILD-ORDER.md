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
| **F-13** | Onboard JobFinderOS (pilot) | 1 | F-00, F-11, F-12, F-16, F-22, F-27, F-28, F-41, F-42, F-43, F-44, F-45, F-46, F-47, F-56 | cross_repo | owner |
| **F-14** | L4 `verify-live` + merged/deployed tracking | 2 | F-10, F-54 | deploy | M |
| **F-15** | L3 holdout runner | 2 | F-10, F-54 | — | M |
| **F-16** | Seeded-bug validator eval | 2 | F-07, F-08, F-09, F-38 | — | M |
| **F-17** | Onboard SoloAdventurer Web | 3 | F-13, F-14, F-15 | cross_repo | owner |
| **F-18** | Triage quarantine + first watcher | 3 | F-14 | external_service | M |
| **F-19** | Meta loop (delta edits to prompts) | 4 | F-11, F-16, F-60 | — | M |
| **F-20** | Cross-provider reviewer | 4 | F-09, F-59 | external_service | S |
| **F-21** | Per-category auto-merge | 4 | F-11, F-14, F-16, F-20, F-38 | deploy | M |
| **F-22** | Vetted agent toolkit (skills, plugins, MCP servers) | 1 | F-06, F-09, F-10 | factory_control | M |
| **F-23** | Review convergence | 1b | F-09, F-10 | factory_control | M |
| **F-24** | Realistic rehearsal fixture in CI | 1b | F-05, F-07, F-08, F-10 | factory_control | M |
| **F-25** | Sensitive-path sentinels | 1b | F-07 | factory_control | M |
| **F-26** | Merges in order | 1b | F-10 | factory_control | S |
| **F-27** | Vent tool | 1b | F-06, F-10 | factory_control | S |
| **F-28** | Dogfood pilot: the factory builds its own safe WOs | 1b | F-23, F-24, F-25, F-26, F-29, F-30, F-33 | — | owner |
| **F-29** | Owner-step tick (`factory tick`) | 1b | F-03, F-04 | factory_control | S |
| **F-30** | Run trace: live event log per run | 1b | F-06, F-07, F-08, F-10, F-25 | factory_control | M |
| **F-31** | Read-only dashboard (`factory dashboard`) | 1b | F-11, F-30 | privacy | M |
| **F-32** | Same-session correction | 1b | F-23, F-30 | factory_control | M |
| **F-33** | Sentinel false-positive corpus | 1b | F-25 | factory_control | M |
| **F-34** | Operator chat (read-only) | 1b | F-31, F-30, F-11 | factory_control, privacy | M |
| **F-35** | Operator actions (proposals you confirm) | 1b | F-34, F-29, F-04 | factory_control | M |
| **F-37** | Planning partner (planner seat) | 1b | F-34, F-35, F-12 | factory_control, privacy | M |
| **F-38** | Parallel review with verifier voting | 1b | F-09, F-23 | factory_control | M |
| **F-39** | Docs MCP server admission | 1b | F-22 | factory_control | S |
| **F-40** | Profile-declared toolchain | 1b | F-05 | factory_control | S |
| **F-41** | Final full review after fix rounds | 1b | F-23, F-38 | factory_control | M |
| **F-42** | Session hardening: attribution and sandbox-bypass attempts | 1b | F-06, F-07, F-30 | factory_control | S |
| **F-43** | Egress redaction seam | 1r | F-30, F-42 | factory_control, privacy | M |
| **F-44** | Verdict integrity | 1r | F-38, F-41 | factory_control | M |
| **F-45** | Pre-lease, publish and crash-window robustness | 1r | F-10, F-26 | factory_control | M |
| **F-46** | Gate evasion hardening (L0/L1) | 1r | F-08, F-25, F-42 | factory_control | M |
| **F-47** | Sandbox read boundary and toolkit staging | 1r | F-05, F-22 | factory_control, privacy | S |
| **F-48** | Eval parity and variance | 1r | F-16, F-44 | factory_control | M |
| **F-49** | Contract and importer hardening | 1r | F-02, F-12 | factory_control | M |
| **F-50** | CLI and dashboard robustness | 1r | F-11, F-31 | — | M |
| **F-51** | Test suite and CI hygiene | 1r | F-01, F-05 | factory_control | M |
| **F-52** | Docs truth pass | 1r | F-11 | — | S |
| **F-53** | Split `_run_task` into phase functions | 1r | F-43, F-44, F-45, F-46, F-47 | factory_control | M |
| **F-54** | Validation-pipeline seam | 1r | F-53, F-48 | factory_control | M |
| **F-55** | Module hygiene | 1r | F-54 | factory_control | M |
| **F-56** | Session record transport | 1r | F-06, F-30, F-43 | factory_control | M |
| **F-57** | Importer robustness: per-task tool, repair turn, prose eval | 1r | F-12, F-49, F-56 | factory_control | M |
| **F-58** | Linux sandbox host on Ubuntu 26.04 and red-CI guard | 1r | F-05, F-51 | factory_control | M |
| **F-59** | Per-project model provider and provider record | 1r | F-05, F-06, F-43 | factory_control, external_service, privacy | M |
| **F-60** | Project memory store | 1c | F-13, F-30, F-43, F-59 | factory_control, privacy | M |

Order to build in: **F-00 (in parallel) → F-01 → F-02 → F-03 → F-04 → F-07 → F-08 → F-05 → F-06 → F-09 → F-10 → F-29** (done), **then F-24 → F-23 → F-25 → F-26 → F-27 → F-30 → F-33 → F-28 → F-11 → F-12 → F-31 → F-34 → F-35 → F-37 → F-38 → F-16 → F-32 → F-22 → F-41 → F-42 → F-13**; F-39 follows F-22 whenever its admission path is chosen at spec approval — it gates nothing else. The deterministic core (F-02–F-04, F-07, F-08) went first: it's pure Python and testable without any model. F-29 went right after them because nothing else could close without it: F-00 and F-06 were stuck on owner steps no command could tick, and F-09/F-10 can't reconcile until F-06 is done. Hardening comes next, rehearsal fixture first so every later change is checked against a realistic repo. F-30 lands before F-28 so the first unattended loop is traceable while it runs, not only afterwards. F-33 lands before F-28 too: a credential hit escalates with no retry, so the sentinels' false-positive rate is measured before an unattended loop depends on them. F-28 then runs the factory on its own F-11 and F-12. F-31 (dashboard) only reads, but it serves trace content to the browser and owns the Host check that F-34, F-35 and F-37 lean on, so it is risk-flagged `privacy` — its Door is one-way under F-10 AC-9 — while remaining the next WO the factory can build for itself: it touches no protected path, and the flag routes its PR through the fuller Merge Danger review. F-34, F-35 and F-37: all three are built interactively (factory_control: they change what reaches the queue and how — F-34 and F-35 carry approvals to it, F-37 adds draft work orders to it from a seat with a security-sensitive read-plus-fetch surface), never by the factory itself. F-34 and F-37 also carry `privacy` — operator transcripts and client briefs are personal data at rest — so their Doors are one-way under F-10 AC-9 too. F-32 changes how retries spend tokens, so it waits until F-30 and F-28 give a baseline to compare against. Neither F-31 nor F-32 blocks F-13. F-38 lands before F-16 (F-16 depends on it) so the seeded-bug eval measures the parallel reviewer the queue will actually run, not the single reviewer it replaces — the F-28 baseline (independent review 8 findings, L2 2) is the case for not waiting. F-16 measures how much each layer catches before any outside project depends on it. **Review remediation (Phase 1r, from CODE-REVIEW-2026-11-05):** F-43 → F-44 → F-45 → F-46 → F-47 → F-56 before F-13's first `factory run` on JobFinderOS; F-48 → F-49 → F-51 → F-57 → F-58 alongside F-13, with F-50 and F-52 as factory-built candidates; then F-53 → F-54 → F-55, and F-14/F-15 build on F-54's seam. F-59 (per-project model provider) lands before the first client repo (any paid job); F-20 builds on it. F-60 (project memory) follows F-13's first runs, since it learns from them; F-19 drafts its entries.

F-14 (live checks) and F-15 (holdouts) need a deployed project with real users, which the factory itself is not, so they arrive with the first outside project (Phase 2). F-21 stays last: it unlocks only on numbers F-11, F-14, F-16 and F-38 produce from real merges, so all four are in its `depends_on`. Trigger evidence, for when the numbers arrive: Anthropic's code-review record (Oct 2026; linked under F-38 below, summarized in RESEARCH-SOFTWARE-FACTORY.md §15) saw revert share fall >40% while merged volume doubled with classifier-set risk tiers and no human on the low tier — dial the low tier on first when F-16 and F-38 give us our own numbers.

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
- the week's vents by category (F-27)
- metrics from DESIGN §9

Acceptance:
- AC-1 For a fixture `tasks.json`, the summary lists every `needs_human` task with its escalation reason.
- AC-2 Metrics computed from `attempts` match hand-computed values for the fixture.
- AC-3 The summary reports outcome measures only (RESEARCH-SOFTWARE-FACTORY §9): interventions per task, review rounds per attempt, tokens per merged PR, time from approval to merge, and the share of merged factory PRs later reverted (a revert commit naming the PR's merge commit). It never reports lines of code or PR counts as a measure of success.
- AC-4 `factory status` lists the week's vents grouped by category, from the F-27 `weeks_vents` seam (no second implementation).

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
- AC-1 `factory run` and `factory loop` refuse to start on a repo whose default-branch protection does not require status checks with `strict` (up to date) enabled, naming the missing setting (checked via `gh api`, faked in tests). The one exception is a repo whose plan can't offer protection (GitHub answers the protection call with its plan-limit 403, e.g. a private repo on a free plan) and whose `project.yaml` declares `branch_protection: unavailable` (schema-validated enum, red-first). Then the factory enforces the equivalent itself: before it reports a factory PR ready or merges it, it fetches the remote default-branch head and checks that the PR branch contains it; if not, it re-validates per AC-3. The declaration never excuses a repo that could have protection but doesn't (protection call returns not-protected, or the declaration is missing): that still refuses, naming the setting.
- AC-2 The loop keeps at most one open factory PR per repo by default (`concurrency: 1` in `project.yaml`, schema-validated). A ready task waits while one is open.
- AC-3 When the default branch has moved past an open factory PR's base, `factory run` re-runs L0 and L1 on the PR merged onto the new base before reporting it ready; a failure moves the task to `needs_human` with escalation `stale_base` (schema enum, red-first).

### F-27 — Vent tool
Lovable's pattern, via Cursor: give agents a tool to complain. The complaints became their best harness bug tracker. Agents push through friction that never shows up as a failure (F-19 AC-4); this records it at the moment it happens.
- AC-1 Implementer and reviewer sessions have one extra tool, `vent(category, text)`, an in-process SDK tool listed in `allowed_tools`. Categories: missing_tool, unclear_contract, flaky_check, environment, other.
- AC-2 Each call appends one record (run id, task id, seat, turn, category, text) to the run's progress log outside the clone. The tool can write nothing else, and text is length-capped.
- AC-3 Venting never changes a run's outcome or budget (a test vents mid-run and asserts the attempt record is otherwise identical).
- AC-4 The week's vents are readable grouped by category through the read-only `weeks_vents` seam, whose docstring names its consumers: `factory status` (F-11) lists them, F-19's review reads them.

### F-30 — Run trace: live event log per run
Today a run is visible only after it ends: the session loop (`factory/session.py`) keeps the `ResultMessage` and drops every other SDK message, so tool calls, gate evidence and per-phase spend are never recorded, and nobody can see what a session is doing while it runs. Seen in IndyDevDan's Super Simple Software Factory (github.com/disler/super-simple-software-factory): every event is written while the agent works, and every gate records what it checked, not only the verdict. We take the trace and leave the rest (it runs on `main` with no sandbox; its gates check the agent's claims against themselves).
- AC-1 The orchestrator appends one event per line to `<factory_root>/trace/<project>/<run-id>.jsonl`, outside every clone and outside the sandbox's write allowlist (a test asserts the path is not writable from a session). Event types: `phase_start`/`phase_end` for setup, implementer, L0, L1, L2 and publish; `tool_call` (seat, tool name, args, result snippet, ok, started/ended); `gate_check` (layer, item, ok, note); `session_end` (seat, tokens by component, cost, turns, model). Every event carries run id, task id, attempt number and a sequence number.
- AC-2 Events are written as they happen. A test with a fake SDK stream asserts a `tool_call` line is on disk before the stream yields its `ResultMessage`.
- AC-3 Gate events carry evidence on pass and on fail: each L0 check, each L1 red/green proof and each L2 finding is one `gate_check` with a note saying what was looked at (one test per layer, both outcomes).
- AC-4 Secrets never reach the trace. Args and result snippets are length-capped, and the seat token value plus credential-shaped strings (the F-25 patterns) are replaced with `[redacted]` before the line is written. A test runs a fake tool call whose output contains the seat token and asserts the token appears nowhere in the file.
- AC-5 Tracing never changes a run. A trace write that fails (disk full, permission) is reported once and the run continues; a test makes every write raise and asserts the attempt record is identical to an untraced run.
- AC-6 `factory trace <run-id> [--follow]` prints the trace in order, one line per event; `--follow` keeps printing new events until the attempt ends (published, failed or aborted). It reads only.
- AC-7 Per-phase token totals in the trace sum to the attempt's token totals in `tasks.json` for a fixture run (no second source of truth for spend).

### F-28 — Dogfood pilot: the factory builds its own safe work orders
**Kind:** owner_only. The first real loop runs on the factory itself, where a mistake only hurts the factory. Principle 5 still holds: agents never edit what judges them.

Owner steps:
1. Extend `paths.protected` in this repo's `.factory/project.yaml` (by PR) to cover what judges agents: `factory/l0.py`, `factory/l1.py`, `factory/l2.py`, `factory/gates.py`, `factory/sandbox.py`, `factory/session.py`, `factory/publish.py`, `factory/orchestrator.py`, `factory/prompts.py`, `factory/trace.py`, `prompts/**` and every existing `tests/test_f*.py`, on top of today's `.factory/**`, `.github/workflows/**`, `schema/**`, the two spec files and `CLAUDE.md`. This repo is private on a free plan, so it can't have branch protection (see F-10 AC-13): declare `branch_protection: unavailable` in the same PR, so F-26 runs with its own up-to-date check instead of refusing. Nothing server-side then stops a stale merge, so merge factory PRs only after `factory run` has reported them ready against the current `main`.
2. Readiness run on a clean clone: every profile command passes unattended (the F-24 rehearsal covers the mechanics).
3. Approve F-11 and F-12 only. Their code (`factory/cli.py` status and import commands, new modules) is outside every protected path; the orchestrator adds their own contract tests to the protected set at approval.
4. `factory run` once, watching; review and merge or reject the PR yourself. Then `factory loop` for the second.
5. Record per attempt: rounds, your review time, findings you raised that L2 missed, and whether the merged change needed a fix later. That is the baseline F-11 then reports automatically.

Step 5 record note (2026-10-27): the step was ticked 2026-10-26T13:29:01Z with evidence that carries rounds, L2-missed findings and later fixes, but no owner review time; tick evidence is immutable, so the gap is recorded here instead. The PR #62 body, the only other place a review time was written, labels CET local times as `Z` and has the weekdays off by one. GitHub's UTC record: PR #58 (F-11) created 2026-10-25T20:25:35Z (Sunday), merged 2026-10-26T07:27:39Z (Monday); PR #61 (F-12) created 2026-10-26T11:04:13Z, merged 2026-10-26T13:21:41Z. Owner attention time for either PR was never measured, so the F-28 baseline has no review-time figure. F-11 does not fill the gap: it reports time from approval to merge (F-11 AC-3), which includes overnight waits, not your attention time.

Protected-path note (2026-10-28): step 1's list is the set protected when F-28 ran. The PR #50–#62 review extends it in PR #70 (judging and gating modules such as `factory/sentinels.py`, `factory/replay.py`, `factory/mergeorder.py`, `factory/contract.py`, `factory/state.py`, `factory/approve.py`, `factory/tick.py`, `factory/prbody.py`, plus `conftest.py` and the pytest/ruff config files). The current `paths.protected` in `.factory/project.yaml` is the source of truth; this list is not kept in step with it. Step text and tick evidence are unchanged (immutable).

### F-31 — Read-only dashboard (`factory dashboard`)
One local page that shows the queue and any run, live, instead of reading `tasks.json`, progress logs and traces by hand. Read-only by construction, so it can never become a second writer of `tasks.json`, and it needs no web framework. One event transport for everything live: server-sent events over the F-30 trace; no WebSocket (token-gated POSTs from F-34/F-35 plus server events cover every need, and a bidirectional transport would add a dependency for nothing).
- AC-1 `factory dashboard [--port N]` serves on `127.0.0.1` only (a test asserts the bound address), using the standard library's `http.server.ThreadingHTTPServer` and static files shipped in the package. No new dependency. A test holds one event stream open and asserts a second request is still answered.
- AC-2 The server answers `GET` only; every other method gets 405. Every route checks the `Host` header: anything other than `127.0.0.1:<port>` or `localhost:<port>` gets 403 (DNS rebinding), with one test per route. A test runs every route against a fixture factory root and asserts no file under it changed (mtime and hash).
- AC-3 Queue view: every task grouped by status, with escalation reasons for `needs_human` and the open steps for `awaiting_owner`, read from `tasks.json` after schema validation (invalid state shows an error page, never partial data).
- AC-4 Run view: phases as swim lanes on a time axis, tool calls inside each lane, gate checks with their evidence, tokens and cost per phase, from the F-30 trace. A running task updates live over `GET /runs/<run-id>/events`, a server-sent event stream fed by the same follower as `factory trace --follow` (same stop conditions: `run_end`, a run that is no longer live, a pruned trace); the page falls back to polling if the stream drops. A test appends events to a fixture trace and asserts the stream delivers them in order and closes on `run_end`.
- AC-5 Metrics come from the F-11 functions, not a second implementation: for the F-11 fixture, the dashboard's numbers equal `factory status` output (one test).
- AC-6 Paths in the URL are run ids and task ids matched against the schema's id patterns; anything else is a 404. A test asserts `..` and absolute paths cannot read outside the factory root.
- AC-7 `GET /healthz` returns 200 with body `ok` without reading the queue or any trace, so a smoke test (and F-35's confirm flow) can tell a dead server from a broken queue.

### F-34 — Operator chat (read-only)
Adds a chat panel to the dashboard, with an operator agent that can explain the factory's state. It can read everything and change nothing.
- AC-1 `factory dashboard --operator` adds a chat panel and one POST route, `POST /operator/chat`. Every operator POST route is listed in one declared set in the dashboard module, which later work orders extend in their own code (F-35's `/operator/confirm`, F-37's inbox upload), never by editing `tests/test_f34.py`; every route in the set enforces AC-2. Without `--operator`, F-31 AC-2 holds unchanged (GET only, everything else 405); a test runs F-31's route sweep both ways and asserts, with `--operator`, that the routes accepting POST are exactly the declared set.
- AC-2 Every POST requires a per-launch random token embedded in the served page, a `Host` of exactly `127.0.0.1:<port>` or `localhost:<port>` (F-31 AC-2's set) and a matching `Origin`. A cross-site form post, a DNS-rebinding `Host` or a missing or wrong token gets 403 and starts no session (one test per case); a test drives one chat turn from a page served as `localhost`, end to end. The confirm half of that e2e lands with F-35's confirm route (F-35 AC-3), which enforces the same checks.
- AC-3 A new `operator` seat: read-only sandbox (`build_settings(read_only=True)`) with its network narrowed to the model provider's host — the profile's `docs_allowlist` and `network_allowlist`, which `build_settings` adds for the build seats today, are left out (a new `build_settings` option; a test asserts the seat's `allowedDomains` is exactly the model host). No GitHub token. Its tools are denied explicitly in `disallowed_tools`, because `allowed_tools` grants without restricting: Write, Edit, NotebookEdit, Bash, WebFetch, WebSearch and Task (sub-agents) — WebSearch runs on the provider's side, outside srt's network confinement, so the sandbox alone cannot stop it. A test asserts the seat denies each of those tools. Reads are limited to the queue, traces, progress logs, reports and the repo. Its prompt `prompts/operator.md` is pinned like the other seats (F-09 AC-2).
- AC-4 `POST /operator/chat` starts the turn and returns its run id; the panel renders the turn from F-31's `GET /runs/<run-id>/events` stream, so chat has no second streaming path. Tool calls reach the trace through the same `_tap_tool_calls` path as F-30, with the same redaction (seat token plus F-25 shapes); every chat turn is a trace run under its own run id.
- AC-5 Transcripts are saved to `<factory_root>/operator/<session-id>.jsonl` (0600 file in a 0700 directory) and pruned by the F-30 retention window. A turn has a `max_turns` cap and a wall-clock limit from config; hitting either ends the turn with a visible message, never a hang.
- AC-6 The chat changes nothing: a test runs a scripted operator session against a fixture factory root and asserts every file outside `operator/` and `trace/` is byte-identical afterwards.

### F-35 — Operator actions (proposals you confirm)
The operator can suggest actions. Only your click carries them out, through the same code as the CLI.
- AC-1 The operator seat gets one more in-process tool, `propose_action(kind, task_id, step?)`. Kinds are limited to `approve`, `tick` and `recover`. `run`, `loop`, merging and anything else is refused with a reason.
- AC-2 The proposal card is built by the server from `tasks.json`, never from the agent's text. `approve` shows `render_contract(task)`, the same text `factory approve` prints, with its `acceptance_hash`; `tick` shows the step text and an empty evidence box only you fill in. A test proves the agent's own description of a proposal never reaches the card.
- AC-3 Confirming is `POST /operator/confirm` with a one-time nonce bound to the proposal's kind, task id, step and `acceptance_hash`. A reused nonce, or one whose task changed since the card was shown, gets 409 and writes nothing. The confirm POST enforces F-34 AC-2's token, `Host` and `Origin` checks (403, one test per case), and the end-to-end test completes here: one confirm from a page served as `localhost`.
- AC-4 A confirmed action runs `approve_task`, `tick_steps` or `recover_task` with your click as the `confirm` callback, never `assume_yes`. Every existing refusal still applies (no acceptance items, already ticked, blank evidence, and so on) and shows on the card. Risk-flagged tasks need a second explicit checkbox, the click-level equivalent of F-04 AC-3's "always interactive".
- AC-5 No agent path can confirm: the operator seat has no route, tool or token that reaches `/operator/confirm`. A test drives a session that tries every tool and asserts the queue is unchanged.
- AC-6 Every proposal and every confirm or decline goes into the operator transcript and the F-30 trace, with who clicked and when.

### F-37 — Planning partner (planner seat)
A second chat seat for discovery, architecture and risky decisions: it reads everything, writes only planning drafts, and turns a plan into draft work orders you confirm. It shapes the plan; it never makes it binding.
- AC-1 The F-34 panel offers a `planner` seat (`POST /operator/chat` with `seat: planner`) with the operator's read-only sandbox and tool denials (F-34 AC-3). In-process tools run inside the sandboxed runner, so the planner's sandbox allows exactly the model host plus the project's `docs_allowlist` hosts (never `network_allowlist`), and with Bash, WebFetch, WebSearch and Task denied, `fetch_doc` is the planner's only path to those hosts. Documentation lookups go through that in-process `fetch_doc(url)` tool, which accepts only `https` URLs whose host is an exact member of the project's `docs_allowlist`, on the default port only (no explicit port other than 443), with a path of at most 256 bytes drawn from `A-Z a-z 0-9 - . _ ~ /`; a URL carrying a query string or fragment is refused, not stripped, a redirect response is refused rather than followed, a session's fetch count is capped from config, every fetched URL is recorded in the trace, and the fetched body honors the same size cap and text types as an inbox upload with a timeout from config (one test per refusal: non-https scheme, query string, fragment, non-allowlisted host, explicit non-443 port, over-long path, disallowed path character, fetch cap reached, redirect; plus a test that the planner's sandbox `allowedDomains` is exactly the model host plus `docs_allowlist`). The path rules bound, not close, an outbound channel to allowlisted hosts, so `docs_allowlist` lists only documentation hosts whose access logs no third party reads — never a user-content or shared-hosting domain. Files you attach (a client brief, say) upload through a token-gated POST (F-34 AC-2) into `<factory_root>/plans/<project>/inbox/` (0600 files in 0700 directories), with a size cap from config and text types only — anything else is refused at upload. Inbox files and saved plans are pruned by the same retention window as operator transcripts (F-34 AC-5), and `factory plans purge <project>` deletes them on demand.
- AC-2 The planner's only write is an in-process `save_plan(name, markdown)` tool into `<factory_root>/plans/<project>/`, with `name` matched against `[a-z0-9-]+\.md` minus the reserved `decisions` and `index` names: `decisions.md` is yours alone, and `index.md` is regenerated by the factory after each save, so neither is planner-writable. `index.md` lists only each plan's file name, size and save time, never text from inside a plan, so planner-authored content cannot ride the index into every resumed prompt (AC-4); a test saves a plan whose first line is an instruction and asserts `index.md` does not contain it. A test asserts `save_plan` cannot write outside that directory (`..`, absolute paths, symlinks) or under either reserved name, and that after a scripted planning session the repo, `tasks.json`, every trace other than the session's own and every plans file other than the session's own saves and the regenerated `index.md` are byte-identical, and `index.md` equals the generator's output for the directory.
- AC-3 `propose_work_orders(tasks)` validates the proposed tasks against `schema/tasks.schema.json` and shows them in the panel as the same diff `factory import` (F-12) shows; your confirm writes them through F-12's write path as `draft` only. A proposal carrying any other status, approval fields or attempts is refused, and so is one naming a task id already in `tasks.json` (F-12 merges spec fields into existing ids; a planner proposal only adds). Confirming is F-35's `POST /operator/confirm` with one added kind, `work_orders` (F-35 AC-1's `propose_action` kinds are unchanged): its one-time nonce is bound to the hash of the proposed tasks and the hash of `tasks.json` when the card was shown, and a reused nonce, or a queue that changed since, gets 409 and writes nothing (one test per case), with F-35 AC-3's token, `Host` and `Origin` checks. Nothing reaches `tasks.json` without the click (test).
- AC-4 Planning resumes across days: a planner session for a project starts with that project's plan index and `decisions.md` from `<factory_root>/plans/<project>/`, and no other memory source. A test asserts a resumed session's prompt carries both. `decisions.md` changes only by your hand: no tool or route writes it — the planner drafts proposed decision entries as an ordinary plan and you add them to the file yourself — and a test asserts a scripted session leaves the file byte-identical.
- AC-5 The planner's model comes from config (`seats.planner.model`, defaulting to the factory default), so it can run on a stronger model than the builders. The trace records the model of every planner turn (test).
- AC-6 No path from the planner to approval: the planner seat has no tool, route or token that reaches F-35's confirm or `factory approve`, and a test drives a session that tries every tool and asserts no task changes status.

### F-38 — Parallel review with verifier voting
L2 is one reviewer. Anthropic's code-review record ([solving code review at Anthropic](https://claude.dev/blog/solving-code-review-at-anthropic), Oct 2026) attributes their review-quality jump to multiple review agents hunting the same diff in parallel, with a separate verifier group voting findings in or out before a human sees them. Our own measured baseline (F-28 step 5) says the single reviewer is the weak layer: independent review found 8 findings, L2 caught 2, including two real bugs and a convention violation the reviewer had the docs to catch.
- AC-1 The orchestrator runs N reviewer sessions (config, default 2) over the same diff in parallel, each a fresh context, then a verifier pass over the union of findings: each finding is kept with the voter's reasoning or dropped; survivors are ranked and tagged important / nit / pre-existing.
- AC-2 Style-only and diagnostic findings are dropped before the verdict, and at most three nits (severity `minor` or `note`) reach the PR body — REVIEW.md's nit budget, mechanically enforced; `blocking` and `major` findings are never capped.
- AC-3 Dropped findings and the drop reason land in the attempt record, so F-19 can measure the reviewer noise the verifier removed.
- AC-4 The fan-out and the vote are tested with scripted reviewer and verifier sessions, no real model: findings two reviewers both report merge into one entry of the union, and a finding the verifier drops never reaches the verdict or the PR body (one test per case). Measuring catch quality on seeded-bug and known-clean diffs is F-16's job (F-16 AC-3), run against this reviewer after it lands; that eval needs real model sessions, so it never runs in routine CI.
- AC-5 `prompt_versions` records every reviewer and verifier prompt hash; the fan-out is inside the attempt's turn and wall-clock budget.

### F-39 — Docs MCP server admission
F-22 AC-9's MCP half, deferred owner-directed on 2026-10-30: no upstream docs MCP server passes the spawn/env allowlists (Phase-1 evaluation: context7-mcp, pypi-docs-mcp, docs-mcp-server, python-docs-mcp, pg-aiguide). The structural gap — real Python servers need `httpx` (absent from the sandbox interpreter, which ships `mcp` + stdlib), real Node servers assume npx/node_modules, and the vet's lexical scan cannot pass a real dependency tree — closes one of two ways, chosen at spec approval: **vendored-bundle provenance** (a reproducible bundle built from a pinned upstream, the pin covering the artifact, not just the source) or a **minimal factory-owned stdio server** on `mcp` + stdlib, restricted to the profile's docs_allowlist.

Acceptance:
- AC-1 One admission path is chosen at spec approval and implemented: either (a) vendored-bundle provenance — a reproducible bundle built from a pinned upstream, its build command and pinned build tool recorded in the index entry, every bundled dependency's licence allowlisted and recorded — or (b) a minimal factory-owned stdio server using only `mcp` and the standard library, every outbound request's host inside the running profile's `docs_allowlist` (scripted test: a host outside the allowlist is never fetched). The chosen server's spawn tokens and env keys pass the F-22 allowlists mechanically, proven red-first on the refusal cases.
- AC-2 The vetter's network class no longer counts a URL inside a LICENSE/NOTICE file as a finding when it is the canonical licence URL of the declared SPDX licence — red-first: a candidate whose only finding is its own licence text's URL passes. (This is what blocked `frontend-design` from anthropics/skills: the Apache-2.0 text quotes `www.apache.org/licenses/`.)
- AC-3 The first docs MCP server is admitted through the F-22 process — vet report committed, owner-approved PR — and a JobFinderOS session test shows its read-only tools granted one by one through `allowed_tools` and enforced by the MCP guard hook (scripted, no model).

### F-40 — Profile-declared toolchain
The sandbox re-allows a fixed list of binaries (`TOOLCHAIN_BINS`: node, npm, npx, claude, git, curl), so any other stack's tools under the denied home are unreachable: a Flutter SDK, a JVM, `uv`. Every other stack-specific setting already lives in `project.yaml`; this is the one hard-coded exception, and the one change that lets a new stack onboard through a profile alone.

Acceptance:
- AC-1 `project.yaml` gains an optional `toolchain` list (schema-validated, red-first): each entry is a binary name resolved on PATH, or an absolute or `~/` path to a binary or SDK `bin` directory. Absent, the sandbox's defaults are exactly today's `TOOLCHAIN_BINS`, proven by an unchanged-settings test.
- AC-2 Profile load refuses an entry that is `$HOME` itself, an ancestor of it, or inside a credential location (`~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.gnupg`), naming the entry. A relative path or an entry that resolves to nothing is refused the same way.
- AC-3 A profile-listed tool under the denied home executes in the gate runner AND every session seat, proven on real srt with a fixture tool under a denied HOME, while a sibling file beside the tool's install stays unreadable: only what `_toolchain_allowread` re-allows becomes readable, never the parent. Nothing is added to `allowWrite`.
- AC-4 The gate runner's run-local venv stays Python-only in effect: a profile with no Python commands still gates end to end through the real runner (fixture project whose setup, lint and test use only the listed tool).

### F-41 — Final full review after fix rounds
Fix-round reviewers work from the delta (F-23): that keeps reviews converging, but a new finding outside the delta needs a `missed_earlier` reason or it's dropped. L0 and L1 re-run in full, yet a fix that breaks untested code in a file it didn't touch can pass both, and the delta review may not see it. F-16's run (2026-10-30) passed this way: two major findings in round one, then a delta review before publish. This closes the gap before outside projects depend on it, without giving up convergence: one full review at the end, where only blocking and major findings count. F-31's run showed a second gap: a surviving major finding doesn't fail a round at all, so AC-6 closes that too.

Acceptance:
- AC-1 When an attempt's fix rounds have ended in an L2 pass and at least one earlier L2 round ran in that attempt, the orchestrator runs one final full review before publish: the configured N reviewers in fresh contexts over the complete `base...head` diff, with no delta section and no carried-findings suppression, then the verifier vote. A task that passes L2 on its first round skips it, because that review was already full.
- AC-2 The F-23 out-of-delta drop does not apply to the final review: a finding in a file the fix rounds never touched stands without a `missed_earlier` field. Only surviving `blocking` or `major` findings fail it. `minor` and `note` findings never fail it and reach the PR body within the nit budget.
- AC-3 A failed final review sends its findings to one more implementer round, followed by L0, L1 and a repeat of the final review. The repeat count is bounded by a config value (default 1). When it is exhausted the task escalates `review_not_converging`: nothing is published with an unresolved blocking or major finding.
- AC-4 The final review sits inside the attempt's turn and wall-clock budgets, sharing F-38's per-session turn rule. When the remaining budget can't fit it, the task escalates `budget_exhausted`: a run never skips the final review to publish in time.
- AC-5 The attempt record and the F-30 trace show that the final review ran, its findings and its verdict. An end-to-end scripted test proves the gap this closes: a fix round breaks untested code in a file outside its delta, the delta review misses it, the final review catches it, and no PR opens until it is fixed.
- AC-6 A surviving `major` finding fails an L2 round exactly as a `blocking` one does, on every round, in `apply_vote` and in `apply_convergence`: a PR never opens with a surviving major finding. F-31's run (2026-10-30) published with a confirmed major defect because only `blocking` failed a round. A declared pass does not override it. The F-23 convergence stops still apply, so a repeated major finding escalates `review_not_converging` and never loops. `REVIEW.md` and `prompts/reviewer.md` state the rule, and the prompt pin is updated.

### F-42 — Session hardening: attribution and sandbox-bypass attempts
Reading F-31's trace on the dashboard (2026-10-30) found two gaps no gate had flagged. The implementer writes `Co-Authored-By` trailers into its commits, and five commits on `main` now carry them (#22, #58, #61, #82, #86), against the repo's rule. On a client repo that's a contract and trust problem, not a style one. Sessions also pass `dangerouslyDisableSandbox: true` to Bash, about 30 times across the traces. The outer `srt` sandbox still confined the F-31 attempt, since the bind failed with the flag set, but the attempts are invisible unless someone reads the trace.

Acceptance:
- AC-1 A commit on a task's branch whose message carries an AI attribution trailer or footer fails L0 with a named finding before anything is pushed: `Co-Authored-By` naming an AI author or an AI vendor address, and `Generated with` footers. The patterns live in one list, matched on the commit messages between the merge-base and head, on every round. A human `Co-Authored-By` passes. The implementer gets the finding in its fix round and rewrites its own branch history.
- AC-2 The publish step refuses a PR title or body carrying the same patterns, naming the match, so nothing reaches GitHub through either path. A squash commit therefore can't inherit a trailer from the branch.
- AC-3 Sessions start with the CLI's own attribution default switched off, proven by a test on the built options and settings for every seat. `prompts/implementer.md` states the rule, and the prompt pin is updated.
- AC-4 Every seat's PreToolUse hook denies a Bash call whose input sets `dangerouslyDisableSandbox` to true, for all seats, under `bypassPermissions`, with a reason telling the session the sandbox is not negotiable. A real-session proof shows the denial firing, and a test covers each seat.
- AC-5 Each denial lands in the F-30 trace and counts on the attempt record, and `factory status` shows the week's total, so repeated bypass attempts are visible to the owner rather than lost in a tool result.
- AC-6 The existing commits on `main` are not rewritten. The F-42 ledger entry records that decision and lists the five affected commits, so the history stays honest.

### F-32 — Same-session correction
When L0 or L1 fails today, the next round is a fresh session that rebuilds its context from the progress log. The session that just did the work already holds that context. SSSF sends gate violations back into the same live session, so a correction costs a reply, not a restart. The reviewer stays a separate seat: L2 keeps its independence.
- AC-1 After an L0 or L1 failure, the orchestrator resumes the implementer's session by id and sends the numbered findings (same format as the progress log), up to `budget.max_corrections` per attempt (schema field, default 2, red-first). The resumed session runs in the same clone under the same sandbox settings as the first turn; a test asserts the settings file and run dir are unchanged between turns. Corrections count against the attempt's `max_turns` and wall clock.
- AC-2 It falls back to today's fresh session plus progress log when the session cannot be resumed, its budget is spent, or the failure is in L2. A test covers each fallback reason and asserts the progress log gets the same entries either way.
- AC-3 L2 never runs in a resumed implementer session: a test asserts the reviewer seat always starts fresh with its own options.
- AC-4 The attempt record and the F-30 trace carry each correction (number, findings sent, tokens spent), so F-11 can compare tokens per merged PR before and after this WO.
- AC-5 Resumed context and correction evidence never come from a file the session can write. The SDK session transcript lives under the session's writable HOME scratch (`<run_dir>/.home/.claude/projects/…/<session-id>.jsonl`; checked 2026-10-21 by running the CLI with a throwaway `HOME`), so the orchestrator copies it outside the sandbox write allowlist when each turn ends, and resumes only from that copy (or rebuilds from the F-30 trace). A test edits the in-sandbox transcript between turns and asserts the resumed session doesn't see the edit. F-19 AC-4 reads transcripts from the same copy.

### F-33 — Sentinel false-positive corpus
F-25 AC-3 proves each built-in sentinel on one seeded diff and one clean diff. That says nothing about how often the sentinels fire on real work, and since the F-25 review a credential hit escalates with no retry, so every false positive costs a `needs_human`. Idea from the ruflo-protector corpus gate: a detector change ships only if it stays silent on a corpus of known-good changes and still fires on a corpus of known attacks.
- AC-1 A benign corpus of at least 20 real merged diffs (this repo's merged PRs, stored as patch files named by PR number) on which every built-in sentinel passes: zero findings across the corpus.
- AC-2 An attack corpus with at least one diff per built-in sentinel that the sentinel flags, kept separate from the F-25 AC-3 seed fixtures (separate files, written fresh), so tuning a pattern to its own seed can't hide a regression.
- AC-3 CI runs every built-in sentinel over both corpora on every PR. A sentinel in `BUILTIN_SENTINELS` with no attack diff, a hit on a benign diff, or a missed attack fails the run, naming the sentinel and the corpus file.
- AC-4 `factory sentinels replay [--corpus benign|attack] [<patch> ...]` runs the built-in sentinels over corpus diffs and prints one line per sentinel per diff. It reads only: a test asserts no file under the factory root changes.
- AC-5 The corpus directory is added to `paths.protected` in this repo's `.factory/project.yaml` (in the same PR), so a session can't edit the evidence that judges its sentinels.

## Phase 1r — Review remediation

Every finding in [`CODE-REVIEW-2026-11-05.md`](CODE-REVIEW-2026-11-05.md) — 1 critical, 14 major, 23 minor, 19 info, 8 architecture — maps to exactly one WO below; the map is in [`docs/remediation-plan-2026-11-05.md`](docs/remediation-plan-2026-11-05.md). Tier 1 (F-43 to F-47) gates F-13's first real run on an outside repo. Tier 2 (F-48 to F-52) runs alongside F-13. Tier 3 (F-53 to F-55) lands before L3/L4 (F-14, F-15) so they arrive on a seam, not inside one function. All except F-50 and F-52 change what judges agents, so they are built interactively, one WO per session; F-50 and F-52 may be built by the factory once Tier 1 has landed.

### F-43 — Egress redaction seam
CODE-REVIEW-2026-11-05.md C-1: the implementer's final text goes into the PR body with no redaction, the seat env holds the Z.ai token, and the orchestrator does the push — so the sandbox's network deny never applies. Traces scrub the literal token; vents and unparsable-record dumps get pattern redaction only (M-8). This WO makes one seam that everything leaving a session passes: literal-token scrub, then `redact_credentials`, then terminal control-sequence stripping.

Acceptance:
- AC-1 (C-1) The implementer notes, every L2 finding text and the L2 verdict JSON pass the seam before `build_pr_body`. A test plants the literal seat token and a bare `ANTHROPIC_AUTH_TOKEN=<value>` echo in each input and asserts neither reaches the rendered body.
- AC-2 (C-1) `check_pr_text` refuses a PR title or body that carries the literal seat token, naming the refusal without echoing the token — a second, independent fence before the push.
- AC-3 (M-8) Vent text and `sessions/unparsable-record-*.stdout.txt` dumps pass the same seam; a test plants the token in each and asserts it is absent on disk.
- AC-4 (m-16) Vent text, `factory status` output and trace lines printed to a terminal have ANSI/OSC/C0 control sequences stripped; a test covers screen-clear, OSC-8 link and cursor-move sequences.
- AC-5 (I-10) `redact_credentials` and the credential sentinel catch JWTs, `Bearer <token>` literals and unquoted YAML `password:` values: one F-33 attack diff per shape, and the F-33 benign corpus stays clean.
- AC-6 A test enumerates every orchestrator path that writes session-derived text off the machine or to the owner's terminal and asserts each calls the seam, so a new channel cannot skip it.

### F-44 — Verdict integrity
CODE-REVIEW-2026-11-05.md M-1, m-10, m-8, I-11: four ways a defect passes the validator without a signal — a capitalized severity softens a blocking finding in `merge_reviews`, a FAIL followed by a quoted example pass-JSON parses as PASS, a printed fake test count beats the min-tests floor, and removing `pytest.raises` isn't counted as an assertion removal.

Acceptance:
- AC-1 (M-1) Severity is canonicalized once at parse (`strip().lower()`, beside the axis canonicalization). A test merges `"Blocking"` with `"minor"` in both orders and asserts the merged finding is blocking and the verdict fail; `eval._count_blocking` is case-insensitive.
- AC-2 (m-10) `_extract_json` refuses output carrying more than one valid verdict object with differing verdicts, as an invalid verdict that takes the retry path. Tests cover FAIL-then-example-PASS and the reverse.
- AC-3 (m-8) `parse_test_count` reads the count from the runner's final summary line, not the maximum anywhere in the output; a test whose test prints `999999 passed` does not clear the floor.
- AC-4 (I-11) Removing a `pytest.raises` block counts as an assertion removal, with an F-33 attack diff and a clean benign corpus.

### F-45 — Pre-lease, publish and crash-window robustness
CODE-REVIEW-2026-11-05.md M-5, M-6, M-7, m-13, m-14, I-3, I-4: the stale-PR sweep creates run dirs before the run lease is held, a failing progress-log append in `_sweep_escalation` wedges the queue in an unbudgeted crash loop, and the publish phase calls the remote with no prompt-disable or timeout while holding the lease.

Acceptance:
- AC-1 (M-5) The stale-PR sweep runs under `queue.idle_lease()`. A test races a sweep against `reclaim_runs` and asserts no live run dir is deleted.
- AC-2 (M-6) A failing progress-log append in `_sweep_escalation` no longer leaves the task in `in_review`: a test injects the failure and asserts the task reaches `needs_human` and the next run proceeds.
- AC-3 (M-7) Publish-side `git push`, `ls-remote` and `gh pr create` run with `GIT_TERMINAL_PROMPT=0` and a timeout through the same wrapper as `_fetch_sha`; a test with a hanging fake remote asserts a timed-out `PublishError`.
- AC-4 (m-13) A `finish()` failure between `append_attempt` and the transition records exactly one attempt; a test injects the failure.
- AC-5 (m-14) `gh pr view` exiting 0 with non-object JSON (`null`) maps to the documented unresolvable PR state, not an `AttributeError`.
- AC-6 (I-3) Atomic queue writes fsync the parent directory after the rename; a test asserts the directory fsync call.
- AC-7 (I-4) The run-lease error names the holder only when that holder is alive and says the lock is stale otherwise; a test covers both.

### F-46 — Gate evasion hardening (L0/L1)
CODE-REVIEW-2026-11-05.md M-2, M-4, m-9, m-17: one `\x1e` byte in a commit body hides an attribution trailer from L0; Unicode look-alikes evade the attribution patterns; a rename evades `lint_suppression_added`; and L1's red-first proof accepts a conftest that conditionally hides the contract tests, while only this repo's profile protects conftest.

Acceptance:
- AC-1 (M-2) The attribution scan reads each commit's message separately (per sha), not by splitting one log on `\x1e`; a commit whose body embeds `\x1e` before a trailer is flagged.
- AC-2 (m-17) Attribution matching runs on NFKC-normalized text with zero-width characters removed and any number of connecting tokens; tests cover U+2010, a fullwidth colon and a zero-width space inside "claude".
- AC-3 (m-9) `lint_suppression_added` checks both the old and new path of a rename; a test renames `payments/x.py` to `utils/x.py` with an added `# noqa` and asserts the finding.
- AC-4 (M-4) Test-runner configuration (`**/conftest.py`, `pytest.ini`, `pyproject.toml`'s pytest table, `setup.cfg`'s pytest section) is protected by a schema-level default that a profile can only widen; every profile in the repo still validates.
- AC-5 (M-4) L1 treats a contract test that was not collected as a failure at both the red and the green end; a test using a conftest that sets `collect_ignore` asserts L1 fails.

### F-47 — Sandbox read boundary and toolkit staging
CODE-REVIEW-2026-11-05.md M-9, I-2, I-9, I-19: `denyRead` protects the factory root's `trace/`, `progress/` and `sessions/` only because the default root lives under `$HOME`; `--factory-root` elsewhere exposes them to every session. Plus three toolkit/sandbox loose ends.

Acceptance:
- AC-1 (M-9) `denyRead` covers the factory root itself with explicit carve-outs for the run's own dir and srt; a test with `--factory-root` outside `$HOME` asserts a session cannot read `trace/`, `progress/` or `sessions/`.
- AC-2 (I-9) A drain that force-closes a `setsid` escapee writes an event to the trace and counts in `factory status`.
- AC-3 (I-2) The index-load MCP domain check validates against the vetted allowlist, not against itself; a test uses an index entry with an unvetted domain.
- AC-4 (I-19) MCP descriptor staging copies its files explicitly; a test asserts the staged set without relying on a copytree side effect.

### F-48 — Eval parity and variance
CODE-REVIEW-2026-11-05.md M-3, m-11, m-12, I-12: the eval's L2 path stops at `apply_vote` while production always runs `apply_convergence`, so the eval measures a pipeline the queue doesn't run. It also parses seats before the quota check, hides L0 false positives on clean diffs, and runs each seed once.

Acceptance:
- AC-1 (M-3) The eval's L2 path runs `apply_convergence` exactly as production does, through one shared function; a test asserts the eval and the orchestrator call the same symbol.
- AC-2 (m-12) The eval checks quota before parsing seats, as production does; a test asserts zero re-runs for a quota-hit seat.
- AC-3 (m-11) The clean-diff report line shows L0 and L1 as well as L2, so a clean diff that failed L0 renders as an L0 false positive.
- AC-4 (I-12) `factory eval --repeats N` runs each seed N times and reports mean and spread per metric (default N=3; the owner confirms the default and cadence at approval).
- AC-5 (manual) The F-16 seeded eval is re-run on the fixed pipeline and the new baseline is recorded as tick evidence; the old numbers were measured without convergence.

### F-49 — Contract and importer hardening
CODE-REVIEW-2026-11-05.md M-10, m-3, m-4, m-6, m-7, I-18: nothing enforces that acceptance refs on non-draft tasks resolve to real tests, `format: date-time` is annotation-only, and the importer truncates, races, loses output on deep JSON and accepts a proposal with no `project`.

Acceptance:
- AC-1 (M-10) A CI test resolves every acceptance ref on every non-draft task to a real test function, using F-01's AST resolution; a typo'd ref on a `ready` task fails.
- AC-2 (m-3) Schema validation enforces `format: date-time`; a malformed timestamp is rejected.
- AC-3 (m-4) A planning-doc listing over 200 files ends with `+N more` and the proposal names the truncated docs.
- AC-4 (m-6) The fresh-queue import write holds the queue lock across the check and the write.
- AC-5 (m-7) A proposal that raises `RecursionError` takes the keep-refused-output path; the session output is saved.
- AC-6 (I-18) A proposal without a `project` key is refused.

### F-50 — CLI and dashboard robustness
CODE-REVIEW-2026-11-05.md m-1, m-2, m-5, m-15, I-5, I-6, I-7, I-8: owner-facing commands that degrade silently, traceback, or exit 0 on failure.

Acceptance:
- AC-1 (m-1) The dashboard `/metrics` page expands `~` in the profile's repo path; a test uses a `~`-relative profile.
- AC-2 (m-2) `factory status` survives a non-UTF-8 commit subject.
- AC-3 (m-5) An explicit `--config <path>` that does not exist is an error; only the default config path may be absent.
- AC-4 (m-15) `factory sentinels replay` exits non-zero on any benign hit or missed attack.
- AC-5 (I-5) A trace line that parses to a non-dict renders as an error row on the run page, not a 500.
- AC-6 (I-6) An `OSError` reading `rendered_path` in `_run_eval` exits 2 with a message.
- AC-7 (I-7, I-8) The `cli.py` module docstring lists the current commands and the dead HEAD suppression in `dashboard.py` is gone; a test asserts the docstring names every registered subcommand.

### F-51 — Test suite and CI hygiene
CODE-REVIEW-2026-11-05.md M-11, m-20 to m-23, I-1, I-16, I-17, A-7: the local suite is red on any interpreter whose path contains `python3`, CI depends on live endpoints, live tests switch on by inference, pins are inconsistent, and wheel installs don't work.

Acceptance:
- AC-1 (M-11) The importer prompt test checks for no bare `python3` outside the quoted interpreter path and passes under conda, pyenv and system Python.
- AC-2 (m-20) Live-endpoint assertions (Z.ai reachability, the raw-socket deny to a public IP) run in a separately named CI job; the sandbox-deny assertions stay mandatory in the main job.
- AC-3 (m-21) Live session tests run only on an explicit opt-in (`FACTORY_LIVE_TESTS=1`), never inferred from a credential being present.
- AC-4 (m-22) `pytest` and `ruff` carry exact pins and CI's Node version meets srt's `>=22.12.0` floor.
- AC-5 (m-23) `re.split` passes `maxsplit` by keyword and the suite runs with `-W error::DeprecationWarning`.
- AC-6 (I-16) The `test_f05` fixed sleeps become condition waits with a timeout.
- AC-7 (I-17) The cross-test-module import moves into a shared `tests/` helper.
- AC-8 (I-1) A wheel install works — schema and toolkit ship as package data read via `importlib.resources` — proven by a CI step that installs the built wheel and loads the contract.
- AC-9 (A-7) `pyright` in basic mode runs in CI and is clean.

### F-52 — Docs truth pass
CODE-REVIEW-2026-11-05.md M-12, M-13, M-14, m-18, m-19, I-13 to I-15, A-8: destination-state prose that no WO shipped, plus stale counts and names. Applies the repo's own rule — mechanical claims are tests — to the docs.

Acceptance:
- AC-1 (M-12) TASK-CONTRACT's queue-marker regeneration claim is removed (`tasks.json` plus `factory status` is the one source; the owner may instead choose to implement it at approval).
- AC-2 (M-13) VALIDATOR-SPEC's done rule names the no-deploy close-out and marks L3/L4 as not yet built until F-14/F-15 land.
- AC-3 (M-14) `docs/proposals-2026-10-30-borrowings.md` is deleted or re-baselined with ids from F-60 up; no doc assigns a live WO id a second meaning.
- AC-4 (m-18) README names the INC-007 skip exception, says the eval runs L0–L2, carries a current WO range, and BOOTSTRAP.md's header matches its use.
- AC-5 (m-19) VALIDATOR-SPEC names `prompts/reviewer.md` as the L2 base prompt.
- AC-6 (I-13, I-14, I-15) BUILD-ORDER's build-order narrative is current, F-13 step 2 names the right file, and DESIGN's vendored-skills list matches what is vendored.
- AC-7 (A-8) Review-provenance comments ("round-N finding M") each cite a PR number; a lint enforces it.
- AC-8 A cross-doc test asserts every `factory` subcommand and repo path named in README, VALIDATOR-SPEC and TASK-CONTRACT exists.

### F-53 — Split `_run_task` into phase functions
CODE-REVIEW-2026-11-05.md §5.2, A-1: `_run_task` is one 1,136-line function with five nested closures, and L3/L4 must thread through it. Pure extraction: no behavior change, the existing suite stays green unchanged. Lands after Tier 1 so the fixes' tests guard the refactor.

Acceptance:
- AC-1 (A-1) `_run_task` becomes phase functions over `_Ctx` (prepare, implement, gates, review, publish); no function in `orchestrator.py` exceeds 150 lines, enforced by a test.
- AC-2 (A-1) The existing test suite passes with no test file changed in this WO's diff.

### F-54 — Validation-pipeline seam
CODE-REVIEW-2026-11-05.md §5.2 OCP, A-2, A-6: adding a validator layer means editing an inline sequence plus prbody, state and cli; the eval reaches into orchestrator privates, which is where M-3 crept in.

Acceptance:
- AC-1 (A-2) The per-round gate sequence is a data list of `(name, callable)`; a test registers a dummy layer and sees it run, record and render in the PR body without editing the orchestrator.
- AC-2 (A-6) `eval.py` imports only public orchestrator functions; a test asserts no `orchestrator._*` import in `eval.py`.

### F-55 — Module hygiene
CODE-REVIEW-2026-11-05.md §5.1, A-3 to A-5: misplaced primitives, one import cycle, and six git wrappers on a security-relevant surface.

Acceptance:
- AC-1 (A-3) Venv creation, the prompt builders and `reclaim_runs` move out of `orchestrator.py`; `now_iso()` moves to `runs.py`; `importer.py` no longer imports `_atomic_write_json` privately.
- AC-2 (A-4) The `contract`/`toolkit` import cycle is broken and an import-graph test asserts the package layering is acyclic.
- AC-3 (A-5) One git wrapper; a test asserts no direct `subprocess` git call outside it.

### F-56 — Session record transport
Found in the 2026-11-05 jobfinderos import (F-13 step 3), not in CODE-REVIEW-2026-11-05.md: the sandboxed entry's record crosses the srt stdout pipe as one JSON line, and three of four real importer runs lost it to a truncated line. The fourth run's record line was exactly 81,920 bytes (5 × 16 KiB), cut inside `permission_denials` (the reviewer's denied Bash commands echoed back in full). The cause is unproven: probes ruled out a non-blocking stdout, the pump and record size alone (a 400 KB record survives). A truncated record loses a completed session, which any seat on a real run can hit, so this gates F-13 step 5 alongside Tier 1.

Acceptance:
- AC-1 The session entry writes its record to a file the orchestrator names (outside the clone, writable by the sandbox, atomic temp-and-rename) and the orchestrator reads it from there; stdout carries only `factory-trace` lines. A test with a 2 MB record whose stdout is cut mid-line still yields the full record.
- AC-2 A missing or unreadable record raises an error naming the srt exit code, the signal if any, and a stderr tail passed through the F-43 seam; full stdout and stderr are saved together under `<factory-root>/sessions/`. A fake sandbox that exits 1 with stderr asserts all three appear.
- AC-3 The record's `permission_denials` is bounded: each `tool_input` capped (a documented size), the list capped, and a dropped count recorded. A test with 200 denials of 5 KB each yields a record under the documented bound with the count.
- AC-4 The 81,920-byte truncation is reproduced or bounded under real srt (candidate: srt's dead-control-channel kill, `node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js`), and when srt reports killing the command the error says so. The finding is recorded in the WO's ledger entry.
- AC-5 When a session completed but its record is unusable, `factory import` salvages `$TMPDIR/proposal.json` exactly as the F-12 abort path does (PR #95), and the refusal names the saved file and the `--proposal` command.

### F-57 — Importer robustness: per-task tool, repair turn, prose eval
Found in the 2026-11-05 jobfinderos import: one 47 KB hand-written JSON answer broke on syntax and on every schema shape. PRs #94/#95 put the shapes in the prompt, staged a self-check and kept refused or aborted output, but each failure still costs a full re-run, and no test proves a real model can import a repo whose docs are prose. This WO validates per task as it is written, repairs instead of re-running, and measures the importer on a prose fixture.

Acceptance:
- AC-1 A `propose_task(task)` tool on the importer seat (the vent pattern, `factory/vent.py`) validates one task per call against the staged proposal schema and the import rules, and returns `accepted <id>` or every violation. Re-proposing an id replaces it; the accepted set is the proposal; zero calls falls back to the text answer. Tests cover accept, every-violation, replace, forbidden fields and the fallback.
- AC-2 A refused proposal from a real session gets ONE repair session (the saved output, the full violation list and the contract; no doc re-reading; a turn budget well under the import's) instead of a full re-run. Quota stops it, it is a trace event, and a failed repair names both saved files. Scripted-session tests cover success, failure, quota and a stub session (no repair).
- AC-3 A prose-docs fixture repo (three prose work orders with no contract-shaped content, one owner-only, one too big for size M) drives an offline test: a scripted session returning the 2026-11-05 broken shapes (risk as a string, boolean gates, `AC1` ids, check type `grep`, size `L`, `P0`) is refused with every violation and fixed by the repair path.
- AC-4 `factory eval --importer` runs one real import against the fixture on demand (never in routine CI) and reports schema validity, task count and whether the oversized work order was split; the owner runs it once and records the report.

### F-58 — Linux sandbox host on Ubuntu 26.04 and red-CI guard
Found while fixing CI on 2026-11-05 (PR #97), not in the review. From 2026-10-24 `ubuntu-latest` resolved to ubuntu-26.04, where srt's nested user namespace fails (`apply-seccomp: write /proc/self/setgroups (nested userns is capability-restricted…): Permission denied`) and `kernel.apparmor_restrict_unprivileged_userns=0` does not lift it; all 176 sandbox tests failed on every branch for 11 days. PR #97 pins CI to ubuntu-24.04 as a stopgap. Nothing stopped PRs #90–#95 merging over the red CI: this repo declares `repo.branch_protection: unavailable` (private repo, free plan), and the factory's self-enforcement covers "branch up to date" but not "CI green".

Acceptance:
- AC-1 The 26.04 failure is reproduced and its cause named (AppArmor userns profile, kernel capability rule, or bubblewrap/srt version), with a minimal repro recorded in the WO's ledger entry.
- AC-2 The sandbox runs on ubuntu-26.04 without weakening it: an srt upgrade or a documented host setting (sysctl or an AppArmor profile for bubblewrap). The `tests/test_f05.py` attack tests pass unchanged on 26.04.
- AC-3 CI runs the sandbox suite on both `ubuntu-24.04` and `ubuntu-26.04`, as separately named jobs, so a runner image change fails one named job instead of every test; the 24.04-only pin from PR #97 is removed or kept with a stated reason in the workflow.
- AC-4 `factory run` preflights the host: when the sandbox cannot create its nested user namespace, the run refuses before picking a task, with one error naming the missing setting, not a cascade of session failures. A test with a fake srt that exits with the setgroups error asserts the refusal.
- AC-5 With `repo.branch_protection: unavailable`, the factory self-enforces green CI as it already self-enforces up-to-date: `factory run` refuses to start while the default branch's latest CI run for HEAD failed, and the stale-PR sweep never reports a PR ready whose checks fail. Tests cover a red default branch and a red PR, through the existing `pr_state`/protection seams.
- AC-6 README's and CLAUDE.md's host table lists exactly the hosts CI verifies; a test asserts every Ubuntu version in that table appears as a CI job.
- AC-7 Nesting is detected on Linux too: a suite run inside the srt sandbox on Linux skips its sandbox-spawning tests through the INC-007 guard instead of failing. The macOS-only skip PR #97 put on `test_nested_suite_skips_instead_of_failing` is removed, and that test passes on ubuntu-24.04, ubuntu-26.04 and macOS.

### F-59 — Per-project model provider and provider record
Every seat today reaches one provider: `session_env` hard-codes the Z.ai base URL and token, so client source would go to Z.ai, with no DPA in place, processing outside the EU, under a consumer plan. This WO makes the provider a per-project choice, recorded per run, and refuses a client repo that has not made that choice. It does not pick a provider: `docs/provider-options-2026-11-05.md` compares the options (recommended default for client repos: Claude Sonnet 5.5 on Bedrock EU, which the Agent SDK supports natively), and the choice is the owner's at each project's onboarding. A gateway or proxy (LiteLLM and similar) is out of scope; it would be a new component inside the trust boundary and gets its own WO when a client needs a model without an Anthropic-compatible endpoint. F-20 (cross-provider reviewer) builds on this. Gates the first client repo (any paid job); not F-13 or F-17, which are owner repos.

Acceptance:
- AC-1 `project.yaml` gains `data_owner` (`self` | `client`) and a `provider` block: `kind` (`zai` | `anthropic` | `bedrock` | `vertex` | `anthropic_compatible`), a `model` per seat (implementer, reviewer, verifier, importer), `region` (required for bedrock/vertex), `base_url` (required for and only allowed with `anthropic_compatible`, https only). Schema-validated on load (F-02). A `client` project with no explicit `provider`, or with `kind: zai`, refuses to load, with the reason named; a `self` project with no block keeps today's Z.ai default. Tests cover every kind, each missing-field case and both refusals.
- AC-2 Credentials are per provider: one keychain item per kind (`factory-<kind>`), with the env lookup rule `seat_token` applies today generalised. A token is used only with its own provider's endpoint, never sent to another, and a token exported for one provider is refused when the project names another. `session_env` stays literal: for each kind it returns exactly the variable names that provider needs per the Agent SDK docs (for Bedrock a scoped bearer token or short-lived credentials plus the region; never the owner's ambient `~/.aws` profile or files). Tests assert the exact env key set per kind, the cross-provider refusal, and that no ambient `ANTHROPIC_*`/`AWS_*` value leaks in.
- AC-3 Session egress follows the provider: the sandbox network allowlist for a session contains the configured provider's inference host(s) (derived from kind, region or base_url) and no other model endpoint. A `client` project's session cannot reach the Z.ai host. Tests read the generated srt settings for each kind.
- AC-4 Every session record and attempt carries a provider record: kind, model id, region or endpoint host (never the token), and token usage (input, cache-read, cache-write, output). The task schema gains the fields, and `factory status` shows per-project, per-provider token totals and an estimated cost from an owner-maintained price table in the factory config (a missing price shows `n/a`, never 0). Tests cover the record, the schema, the total, and the missing-price case.
- AC-5 Seats may name a different model of the project's provider; a different provider only from the project's own `providers_allowed` list (for F-20's second reviewer). A `client` project never falls back to Z.ai or to any provider outside its list on error, quota or a missing credential: the session fails closed with the reason named. Tests cover the allowed override, the refused override and the no-fallback path.
- AC-6 The importer and `factory eval` take the same provider resolution as `factory run` (one resolver, no second code path). A test runs the importer's session construction for a `client` project and asserts the resolved provider, env and egress match AC-1 to AC-3.
- AC-7 One real session per newly supported kind the owner has provisioned (at minimum `bedrock`, EU region, Sonnet 5.5) runs against the rehearsal fixture (F-24); its record shows the provider, region and token usage, and egress logs show no other model host. The owner runs it (paid) and records the evidence.

## Phase 1c — First outside project

### F-13 — Onboard JobFinderOS (pilot)
**Kind:** owner_only. This is the onboarding steps in TASK-CONTRACT §6, run on the real repo. It starts only after F-28 has a measured record on the factory's own queue. A readiness rehearsal already ran on 2026-10-18 (L0 passed; it found the three bugs fixed in PR #24); draft profile, baseline and contracts are in `projects/`, unapproved.

Owner steps:
1. Readiness run: every `project.yaml` command passes unattended on a clean clone.
2. Resolve every `TODO(verify)` in `projects/jobfinderos.project.yaml`; commit it as `JobFinderOS/.factory/project.yaml`.
3. Run `factory import`; read the full diff line by line; commit.
4. Decide in writing whether client source may be sent to the Z.ai endpoint: for an EU client that means a data processing agreement or the client's written consent, recorded per repo. Until then, onboard only repos you own.
5. Approve 2–3 self-contained build WOs, not WO-13/16/17 (payments), WO-27 (decision) or WO-29/30 (research-gated).
6. Run `factory run` once, watching. Merge or reject the PR yourself.

### F-60 — Project memory store
Every attempt starts cold: the implementer rebuilds the project from the contract and the progress log, and nothing one work order learns about a repo reaches the next. Two Anthropic talks (Nov 2026: "Context Engineering & Autonomous Memory", AI DevCon; "Build Agents That Run for Hours", AI Engineer) report that a curated memory store is where repeat runs get better and cheaper, and both name the risks this WO guards against. In-band memory writes are a prompt-injection path: a session reading planted text in a client repo could write it into memory, and every later session would trust it. Models also overwrite markdown state more readily than JSON. A critic that sees the builder's own account of its work drifts towards agreeing with it. So the store is structured, owner-written, sliced by seat, and fails closed when an entry goes stale. Setup and commands stay in `project.yaml`, where they are checked; memory holds only knowledge a check can't hold. F-19 drafts entries from transcripts; it never writes them.

Acceptance:
- AC-1 Each project has one store at `<factory_root>/memory/<project>/entries.jsonl`, validated against `schema/memory.schema.json` on load (F-02); an invalid store refuses the run with the line named. An entry has `id`, `kind` (`repo_fact` | `finding_pattern` | `reviewer_calibration` | `attempt_note`), `text`, `sources` (run ids, with turn where one applies), `model` (the seat model it was learned under, from the F-59 provider record), `created`, `last_confirmed`, and optional `supersedes`. The file is append-only: a change is a new entry that supersedes the old one, and retiring an entry appends a retirement. A test rewrites a past line and asserts the load refuses.
- AC-2 Only the owner's `factory memory add | retire | confirm | redact` commands write the store, each under the store lock and only if the store's content hash still matches the one the command read, so two writes never clobber each other (the optimistic-write pattern Anthropic's memory stores use). No seat, tool, route or session writes it, it is outside every sandbox write allowlist, and a scripted session of each seat leaves the file byte-identical (the F-37 AC-4 pattern). F-19 and AC-6 produce drafts under `drafts/`, which are never served.
- AC-3 Each seat gets its own slice, built into its prompt by the orchestrator, never read from a path the session can reach. Implementer: `repo_fact`, `finding_pattern`, `attempt_note`. Reviewer and verifier: `repo_fact`, `finding_pattern`, `reviewer_calibration`, and never an `attempt_note` or any text an implementer session wrote. A session for one project never gets another project's entries. Tests assert each seat's built prompt and the cross-project refusal.
- AC-4 An entry is served only while it is fresh: its `model` matches the seat's resolved model and `last_confirmed` is within `memory.max_age_days` (`project.yaml`, default 30). A stale entry is withheld, never served with a warning, and `factory status` lists it as needing `factory memory confirm`. Tests cover a model change, an aged entry and a confirmed one.
- AC-5 The served slice has a size cap per seat (`memory.max_chars`, schema field, default 8000). A store whose fresh slice exceeds it refuses the run with the overflow named, so curation happens before the run, never by silent truncation. A test covers the refusal.
- AC-6 `factory memory seed <project>` drafts a repo map (layout, entry points, where tests live) from a read-only session with no repo write access and no GitHub token, and writes it to `drafts/` for the owner to read and add. A test asserts the seat's sandbox settings and that nothing is served until `add`.
- AC-7 `factory memory calibrate <run-id> --finding <n> --agree | --disagree --note <text>` records a `reviewer_calibration` entry that cites the run and the finding, so the reviewer's reference examples are the owner's own past judgements. The reviewer slice carries the most recent entries up to the AC-5 cap. A test asserts the entry and its place in the reviewer prompt.
- AC-8 Every session record lists the entry ids it was served and the store's content hash, and the F-30 trace carries both, so F-19 can attribute failures to entries the way it attributes them to `prompt_versions`. A test covers the record.
- AC-9 Memory text passes the F-43 egress redaction seam before it reaches a provider, like any other prompt content. A test seeds an entry with a credential-shaped string and asserts it never reaches the session's built input.
- AC-10 `factory memory redact <id>` replaces an entry's `text` with `[redacted]` and records when and why, keeping its id, sources and place in the file. It is the one permitted in-place change: a client's erasure request, or a secret that slipped in, must be removable, and the record that something was there must stay. A test asserts the redacted text is gone from the file and from every built prompt, and that the AC-1 append-only check still passes.

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
- AC-3 The eval runs L2 as F-38's parallel reviewer and verifier, over the seeded-bug diffs plus known-clean diffs, and reports per diff whether the surviving findings caught the seeded bug, and how many blocking findings each known-clean diff drew. It runs on demand and monthly, never in routine CI: it drives real model sessions.

AC-3 is checked `manual`, not `test` or `command`: an L1 red/green run would drive real model sessions, and so would a `command` check, whose exit code is evidence the validator collects on every attempt (VALIDATOR-SPEC L1 step 3). `manual` becomes an owner step (TASK-CONTRACT §2), and the one below is its evidence.

Owner steps: Run the AC-3 eval once on demand after F-16 merges and record its per-diff report (seeded bug caught or not; blocking findings per known-clean diff) as the tick evidence.

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
A weekly review of escalations, L2 findings and the week's vents → PRs with **itemised delta edits** to prompt files. Edits target what actually caused failures. Every attempt records which prompt versions and skills it used (F-10 AC-6), and the review traces failures back to them. This is a non-trained version of the credit-assignment idea in market-based post-training ([arXiv 2610.22322](https://arxiv.org/abs/2610.22322)).
- AC-1 A PR that touches validators, holdouts, contract tests or schemas is rejected by L0.
- AC-2 Edits are additions, removals or changes to individual items, never whole-file rewrites (a diff-size check).
- AC-3 The weekly review groups failed and escalated attempts by `prompt_versions` and `skills`, and reports each version's failure share. Each proposed edit names the version it targets and the evidence (attempt run ids) behind it.
- AC-4 The review also reads the session transcripts of the attempts it samples, because agents push through friction that never shows up as a failure. Every proposal cites the run id and turn it comes from.
- AC-5 A proposal that targets anything other than a prompt file (a new check, CI, `standards`, steering files) comes out as a `draft` task for you, never as a meta-loop PR. Agents can't edit what judges them (Principle 5).
- AC-6 The weekly review reads the week's vents (the F-27 `weeks_vents` seam) alongside failures and transcripts, so friction that never became a failure still reaches you.

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
- AC-9 The first JobFinderOS toolkit goes through the whole process end to end: one vetted skill (strict-api, vendored from `alirezarezvani/claude-skills` @ `0496583`, MIT), admitted by owner-approved PR, with its report committed. A JobFinderOS session test shows the skill available. *(Re-scoped 2026-10-30, owner-directed — the F-27 AC-4 precedent. Original text promised a read-only documentation MCP server in the same toolkit; the Phase-1 evaluation found no upstream docs MCP server that passes the spawn/env allowlists — context7-mcp (published dist needs external node packages), pypi-docs-mcp (httpx/bs4 absent from the session interpreter, arbitrary-URL tool, GitHub API hosts), docs-mcp-server (native deps, arbitrary-URL fetching), python-docs-mcp (pins mcp<2.0 vs the interpreter's 2.3.0, docs.python.org unallowlisted), pg-aiguide (Bun/TS RAG service on a credentialed LLM API). The MCP half is deferred to F-39 rather than bending the rules.)*
