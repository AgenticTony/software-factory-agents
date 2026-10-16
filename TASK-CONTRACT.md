# Task Contract — what the factory consumes

The factory never reads a project's planning prose to decide what to do. It reads **`.factory/tasks.json`** (the queue) and **`.factory/project.yaml`** (how to build, check, and ship the project). Both live in the target repo. Schemas: [`schema/tasks.schema.json`](schema/tasks.schema.json), [`schema/project.schema.json`](schema/project.schema.json). Draft profiles: [`projects/`](projects/).

Companion docs: [DESIGN-LOCAL-FACTORY.md](DESIGN-LOCAL-FACTORY.md) (architecture + decisions), [VALIDATOR-SPEC.md](VALIDATOR-SPEC.md) (how "done" is proven).

---

## 1. Why a JSON contract, not the markdown

- Prose status ("open — gated on copy research", "blocker cleared, 3/7") is written for humans and gets misread by models. One misread → the factory works on a blocked task, or marks an unfinished one done.
- Anthropic's long-running harness keeps its feature list in JSON because models are less inclined to rewrite JSON than markdown.
- **Agents never write `tasks.json`.** Only the orchestrator does, and only through the state transitions in §3. The markdown queue (e.g. JobFinderOS `docs/work-orders/README.md`) keeps its prose rationale; the status table between `<!-- factory:queue:start -->` / `<!-- factory:queue:end -->` markers is regenerated from `tasks.json`.

## 2. The task record

One record per work item (WO, story, or step). Big items are split into **slices** — the unit one agent session actually builds.

| Field | Meaning |
|---|---|
| `id` | Project's own id, verbatim: `WO-25`, `A-web.1`, `W.1`. |
| `title`, `source` | Human title; path (+ heading anchor) of the doc that owns the detail. The agent is given `source`, so the WO stays the spec. |
| `kind` | `build` · `decision` · `research` · `owner_only` · `docs`. **Only `build` and `docs` are executed.** Decision/research/owner-only tasks are surfaced to you, never attempted (JobFinderOS WO-27 is a decision WO; WO-29/30 are research-gated). |
| `priority` | `P1`–`P3`. Ties broken by queue order. |
| `depends_on` | Task ids. Cross-repo deps are written `repo:id` (e.g. `soloadventurer-mobile:9b`) and are always treated as unmet until a human marks them met. |
| `status` | See §3. |
| `risk` | Static flags declared from the doc: `auth`, `payments`, `rls`, `migration`, `safety`, `privacy`, `new_dependency`, `deploy`, `cross_repo`, `external_service`, `factory_control` (changes to how work is chosen or judged: schemas, contracts, validator config). The validator adds dynamic flags from the diff (§ VALIDATOR-SPEC L0). |
| `gates` | `spec_approval` (bool), `merge` (`human`/`auto`), `deploy` (`human`/`auto`). Any risk flag forces `human` on merge. In v0 everything is `human`. |
| `acceptance` | The **validation contract**: list of `{id, text, check}`. `check.type` is `test` (named test that must fail before / pass after), `command`, `holdout` (scenario id), `prod_probe` (post-deploy check), or `manual` (becomes an owner step). Frozen when the spec is approved: `acceptance_hash` = SHA-256 of the canonical JSON. A changed hash after approval sends the task back to `draft`. |
| `owner_steps` | `[{text, done}]` — things only you can do (JobFinderOS "Owner steps (not buildable by a session)"; SoloAdventurer 👤 items). |
| `slices` | `[{id, text, status, acceptance_ids}]` — ordered build units. One session builds one slice. |
| `budget` | `{size: S|M, max_attempts, max_turns, wall_clock_min}`. L-sized work must be sliced; the factory refuses an unsliced L. |
| `attempts` | Append-only log: `{run_id, slice_id, started, ended, outcome, pr, rounds, input_tokens, output_tokens, escalation}`. |
| `links` | `pr`, `branch`, `execution_record` (where the factory appends its record in the WO doc). |

## 3. Status set and lifecycle

```
draft ──(you approve spec + contract)──▶ ready ◀──(deps met)── blocked
  ▲                                         │
  │ contract changed                        ▼ orchestrator picks it
  │                                    in_progress ──(attempts exhausted / ambiguity / risk hit)──▶ needs_human
  │                                         │
  │                                         ▼ validator L0–L3 PASS, PR open
  │                                     in_review ──(you merge — v0 always)──▶ deploying
  │                                                                              │
  │                                                     L4 live check FAIL ◀─────┤
  │                                                     → revert + needs_human   │ L4 PASS
  │                                                                              ▼
  │                                                           owner steps open? ── yes ──▶ awaiting_owner
  │                                                                              │ no            │ you tick them
  └─────────────────────────────────────────────────────────────────────────────▼──────────────▼
                                                                                done
```

| Status | Set by | Meaning |
|---|---|---|
| `draft` | importer / you | No approved contract yet. Not eligible. |
| `blocked` | orchestrator | Contract approved, a dependency isn't `done`. |
| `ready` | orchestrator | Eligible to run. |
| `in_progress` | orchestrator | A session owns a slice. Only **one** writer task at a time per repo (v0). |
| `in_review` | orchestrator | Validator passed, PR open, waiting on your merge. |
| `deploying` | orchestrator | Merged; waiting for deploy + live verification. |
| `awaiting_owner` | orchestrator | **Built and live-verified, owner steps open.** Not done. This is the "blocker cleared ≠ story done" state both projects already use. |
| `needs_human` | orchestrator | Escalated. `attempts[-1].escalation` says why. Never retried automatically. |
| `done` | orchestrator | All slices merged, L4 live check passed, owner steps ticked. **Requires live evidence — CI green is not enough.** |
| `not_buildable` | importer | `decision`/`research`/`owner_only` kinds. Listed in your daily summary. |

Escalation reasons (enum): `attempts_exhausted`, `budget_exhausted`, `risk_path_touched`, `new_dependency`, `protected_path_touched`, `contract_ambiguous`, `quota_exhausted`, `live_check_failed`, `cross_repo`.

## 4. Picking the next task

```
eligible = status == ready
           and kind in {build, docs}
           and every depends_on is done
           and no other writer task in_progress for this repo
order by priority, then position in queue
take the first incomplete slice of the first eligible task
```

## 5. The project profile (`.factory/project.yaml`)

The adapter. Everything the factory must know about a repo that isn't in a task.

| Section | Contents |
|---|---|
| `repo` | Local path, default branch, remote, merge method. |
| `commands` | `setup`, `lint`, `typecheck`, `test`, `build` — exact commands, run from repo root. |
| `baselines` | Path to a baseline file (test counts, known errors) if the project has one. |
| `paths.risk` | Globs whose change adds a risk flag (auth, migrations, RLS, payments, safety). |
| `paths.protected` | Globs workers may not modify: validator-owned tests, contract fixtures, CI config, the factory's own files. Workers may *add* tests elsewhere. |
| `dependency_manifests` | Files whose change means a new/changed dependency (`package.json`, `requirements*.lock`). |
| `deploy` | Mechanism, whether merge auto-deploys, the deploy command if manual. |
| `live_checks` | Post-deploy commands the orchestrator runs **outside** the sandbox with secrets from the macOS keychain. |
| `holdouts` | Directory outside the repo (`~/factory/holdouts/<project>/`) and how to run it. |
| `docs_allowlist` | Domains the agent may fetch for docs grounding (both projects mandate official-docs lookups). |
| `cross_repo` | Sibling repos whose changes can break this one. v0: any task touching them → `needs_human`. |

## 6. Onboarding an existing project

1. **Readiness check.** Factory runs `commands.*` on a clean clone. Every command must run unattended and pass (or match the baseline). Fix the repo first if not — a factory on a red repo can't tell its own breakage from existing breakage.
2. **Write `project.yaml`** from the repo's CLAUDE.md / CI config. Drafts for both projects are in [`projects/`](projects/).
3. **Import the queue.** An importer session reads the planning docs and proposes `tasks.json`. **You review the full import diff line by line before it's committed** — bulk-generated state you didn't read is how the wrong-operation-label bug shipped. Every imported task starts in `draft`.
4. **Approve contracts** for the first 2–3 tasks only. The factory starts on those; approve more as it earns trust.
5. **Insert the queue markers** in the markdown so the status table regenerates from `tasks.json`.

## 7. Mapping the two projects

| Concept | JobFinderOS | SoloAdventurer Web |
|---|---|---|
| Task source | `docs/work-orders/WO-*.md`, queue in `docs/work-orders/README.md` | `docs/EXECUTION_ORDER.md` steps + stories in `docs/sprints/PHASE_*.md` |
| `id` | `WO-25` | Story id (`A-web.1`, `W.1`); step number kept as `links.step` |
| `acceptance` | "Acceptance criteria" section (already red-first) | Story DoD in sprint doc |
| `owner_steps` | "Owner steps (not buildable by a session)" | 👤 items |
| `risk` | "Gate" section; WO-13 = `payments` | `[safety: true]`, `[needs_human: true]`, ⚠ |
| `depends_on` | Queue table column | Execution-order column (mostly `soloadventurer-mobile:*`) |
| `execution_record` | "Execution record — <date>" section in the WO | Sprint doc story section |
| Fit | High — WOs are already self-contained contracts. **Pilot project.** | Low for v0 — most open items are cross-repo or human-gated. Second project. |
