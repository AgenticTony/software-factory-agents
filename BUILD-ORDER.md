# Factory Build Order

The factory's own build plan, written in the format the factory will consume: one work order (F-xx) per task, with checkable acceptance criteria, risk flags and owner steps. The machine-readable copy is [`.factory/tasks.json`](.factory/tasks.json). It validates against [`schema/tasks.schema.json`](schema/tasks.schema.json), and every task starts in `draft` until you approve it, per [TASK-CONTRACT.md](TASK-CONTRACT.md) §6.

**Bootstrapping:** the factory can't build itself before it exists. F-01 to F-11 are built **interactively**, by you plus a Claude Code session (GLM or Claude), one WO per session. Once F-10 works end to end, the factory takes over its own remaining WOs (F-14 onward). That's the first real test, on a repo where a mistake only hurts the factory itself.

**Stack:** Python 3.12, `pytest`, `ruff`, JSON Schema validation, headless Claude Code for agent sessions, `gh` for PRs, `srt` / Claude Code sandbox for isolation. No web framework; it's a CLI.

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
| **F-06** | Session runner (headless Claude Code on Z.ai) | 1 | F-05 | external_service | M |
| **F-07** | L0 deterministic gates | 1 | F-02 | — | M |
| **F-08** | L1 red-first proof | 1 | F-07 | — | S |
| **F-09** | Agent prompts + L2 review | 1 | F-06 | — | M |
| **F-10** | `factory run` / `factory loop` + PR creation | 1 | F-03, F-04, F-06, F-07, F-08, F-09 | deploy | M |
| **F-11** | `factory status`: daily summary + metrics | 1 | F-03 | — | S |
| **F-12** | Importer (`factory import`) | 1 | F-06, F-02 | — | M |
| **F-13** | Onboard JobFinderOS (pilot) | 1 | F-00, F-10, F-11, F-12 | cross_repo | owner |
| **F-14** | L4 `verify-live` + merged/deployed tracking | 2 | F-10 | deploy | M |
| **F-15** | L3 holdout runner | 2 | F-10 | — | M |
| **F-16** | Seeded-bug validator eval | 2 | F-07, F-08, F-09 | — | M |
| **F-17** | Onboard SoloAdventurer Web | 3 | F-13, F-14, F-15 | cross_repo | owner |
| **F-18** | Triage quarantine + first watcher | 3 | F-14 | external_service | M |
| **F-19** | Meta loop (delta edits to prompts) | 4 | F-11, F-16 | — | M |
| **F-20** | Cross-provider reviewer | 4 | F-09 | external_service | S |
| **F-21** | Per-category auto-merge | 4 | F-16, F-20 | deploy | M |

Order to build in: **F-00 (in parallel) → F-01 → F-02 → F-03 → F-04 → F-07 → F-08 → F-05 → F-06 → F-09 → F-10 → F-11 → F-12 → F-13.** The deterministic core (F-02–F-04, F-07, F-08) goes first: it's pure Python and testable without any model. The parts that touch Z.ai come after, so F-00's answers are in before anything runs against the plan.

---

## Phase 0

### F-00 — Owner prerequisites
**Kind:** owner_only. Nothing here is buildable by a session.

Owner steps:
1. Email Z.ai support with two questions:
   - Are scheduled, unattended headless Claude Code runs allowed on the Coding Plan?
   - Does renewing a legacy Max plan keep the prompt-based quota?
2. Check the legacy term's end date on Z.ai's "My plan" page.
3. Check JobFinderOS's **production** env on Render for `GLM_BASE_URL`. If it's the Coding Plan endpoint (`api/coding/paas/v4`), move production to a metered key, or the Mistral-hosted path (MIG-WO5), **before F-13**.
4. Turn on branch protection on JobFinderOS `main`: PRs required, CI required, no force-push.
5. Create macOS keychain items for the live-check secrets: `JFO_PROBE_PASS`, `JFO_SUPABASE_ANON_KEY`.
6. Create the GitHub repo for the factory, private.

---

## Phase 1 — v0 on JobFinderOS

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
`run_session(seat, prompt, cwd, max_turns, timeout_min)` wraps `claude -p --output-format json --max-turns N` inside the F-05 sandbox.
- **Env:** per-seat env: Z.ai base URL, auth token from the keychain, `API_TIMEOUT_MS`.
- **Clock:** wall-clock is enforced, and paused outside the session.
- **Hooks:** a per-run settings file with a PreToolUse hook that denies Edit/Write on `paths.protected`.
- **Record:** turns, tokens and the model reported back.

Acceptance:
- AC-1 With a stub `claude` binary, the runner kills the session at the wall-clock limit and returns `aborted`.
- AC-2 Malformed JSON output is reported as a failed attempt, not a crash.
- AC-3 The hook blocks an Edit to a protected path inside a real session.
- AC-4 A real smoke session against Z.ai completes a trivial prompt, and the recorded model name is read from the result.

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

### F-11 — `factory status`: daily summary + metrics
Write a markdown summary per project:
- done, in review, needs you (with reasons), awaiting owner
- quota used this week
- metrics from DESIGN §9

Acceptance:
- AC-1 For a fixture `tasks.json`, the summary lists every `needs_human` task with its escalation reason.
- AC-2 Metrics computed from `attempts` match hand-computed values for the fixture.

### F-12 — Importer (`factory import`)
An importer session reads a project's planning docs and proposes `tasks.json`. Every task is `draft`. The orchestrator shows the full diff; nothing is written without confirmation.

Acceptance:
- AC-1 The output validates against the schema, or the import is refused.
- AC-2 Every imported task is `draft`.
- AC-3 Running it twice with no doc changes proposes no diff for existing tasks. Status and attempts are preserved.

### F-13 — Onboard JobFinderOS (pilot)
**Kind:** owner_only. This is the onboarding steps in TASK-CONTRACT §6, run on the real repo.

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

## Phase 4 — Self-improvement and gate dial-down

### F-19 — Meta loop
A weekly review of escalations and L2 findings → PRs with **itemised delta edits** to prompt files.
- AC-1 A PR that touches validators, holdouts, contract tests or schemas is rejected by L0.
- AC-2 Edits are additions, removals or changes to individual items, never whole-file rewrites (a diff-size check).

### F-20 — Cross-provider reviewer
A per-seat provider config. Risk-flagged tasks get a second L2 review from a different provider.
- AC-1 A risk-flagged task's PR shows two independent verdicts.

### F-21 — Per-category auto-merge
Enable `merge: auto` for a task category only when DESIGN §3's trigger is met: catch rate ≥ 90%, revert rate < 5%, over 30 merges.
- AC-1 The schema still rejects auto-merge on any risk-flagged task.
- AC-2 Auto-merge switches off automatically if the category's revert rate crosses the threshold.
