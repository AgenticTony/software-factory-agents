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
| `acceptance` | The **validation contract**: list of `{id, text, check}`. `check.type` is `test` (named test that must fail before / pass after), `command`, `holdout` (scenario id), `prod_probe` (post-deploy check), or `manual` (becomes an owner step). Frozen when the spec is approved: `acceptance_hash` = SHA-256 of the canonical JSON. A changed hash after approval sends the task back to `draft` — except a `done` task, which is **flagged for your review** instead: reopening verified work is a human decision, never automatic. |
| `out_of_scope` | Optional list of things this task must **not** change or add, e.g. adjacent features that look related. It gives the L2 Spec axis something concrete to check scope creep against (VALIDATOR-SPEC L2). You write it at approval, alongside `acceptance`. It isn't part of `acceptance_hash` (F-04 hashes `acceptance` only), so editing it after approval doesn't send the task back to `draft`. |
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

Recovery and continuation edges (same allowed-transition table, kept out of the diagram for readability):

| Edge | Set when |
|---|---|
| `in_progress → ready` | the run aborted with no PR and no outcome (orchestrator crash, killed session). The task re-enters the queue under the same attempt budget. Liveness comes from a **run lease**, an OS lock the session holds for its whole life and the OS releases when the process dies. `reclaim_abandoned` applies this edge only when the lease is free, so a live run is never reclaimed, and a run that died before its first attempt record still is. |
| `in_review → in_progress` | you rejected the PR — the task returns for a fix round. |
| `in_review → needs_human` | stale base (F-26): the default branch moved past the open PR's base and re-validation of the PR merged onto the new base failed — the PR was only ever validated on the base it was built on. |
| `deploying → ready` | the previous slice merged and live-verified and more slices remain. Owner-step and `done` evaluation waits until the last slice merges; a task can only be `done`/`awaiting_owner` with every slice merged. |
| `ready → blocked` | a dependency regressed (its task was reopened or demoted). `sync_readiness` reconciles both directions. |

Escalation reasons (enum): `attempts_exhausted`, `budget_exhausted`, `risk_path_touched`, `new_dependency`, `protected_path_touched`, `contract_ambiguous`, `quota_exhausted`, `live_check_failed`, `cross_repo`, `pr_create_failed`, `orchestrator_error`, `publish_refused`, `review_not_converging`, `sentinel_failed`, `credential_added`, `stale_base`.

The three orchestrator-side reasons (F-10):

- `pr_create_failed`: the validated branch was pushed but `gh pr create` failed. The task is `needs_human` and `links.branch` names the pushed branch. Open the PR by hand from that branch, or delete it and re-queue.
- `orchestrator_error`: the orchestrator itself failed mid-round (a gate crash, a reviewer timeout). The attempt counts against the budget, the task re-queues, and `loop` stops. Look at the attempt's `note` before running again.
- `publish_refused`: the push fence or the PR-body gate refused, which retrying can't change. The task is `needs_human`; the attempt's `note` says which rule fired.

`review_not_converging` (F-23): a review round's FAIL contained only findings already raised in an earlier round — the loop is bouncing the same findings back and forth, and another round cannot fix that. Re-review now scopes to the delta since the last review and carries repeats instead of re-raising them (BUILD-ORDER F-23); reaching this escalation means those mechanisms see no progress. A human decides whether the findings are real, the reviewer is wrong, or the contract is the problem.

`sentinel_failed` (F-25): a project sentinel — a `sentinels.<risk flag>` command in `project.yaml` — ran because the diff touched that flag's `paths.risk` globs, and failed. Retrying cannot make an invariant hold, so the task is `needs_human` with the sentinel's output in the L0 report and the attempt record. Read what the sentinel said: either the change really breaks the project's invariant (reject the PR or fix the project), or the sentinel command itself is wrong (fix it in `project.yaml` by PR).

`credential_added` (F-25): the diff adds a credential-shaped string (a key, token or private-key pattern). Nothing is pushed before a passing round, so the credential sits in the run branch's LOCAL history only — but a retry that later passes would push that earlier commit, so the task is `needs_human` on the round that found it, with no implementer retry. Rotate the credential regardless (the model provider saw it; it is compromised even though nothing was pushed). No history rewrite is needed: re-queueing discards the run branch — each run clones the default branch fresh. The shapes are heuristics; a false positive costs exactly this escalation (`needs_human`, no retry), where a human confirms and re-queues — erring protective is the intent.

`stale_base` (F-26): the default branch moved past an open factory PR's base, and re-running L0 and L1 on the PR merged onto the new base failed (or the merge itself conflicts). The PR's validation only ever held on the base it was built on, so the task leaves `in_review` for `needs_human` instead of waiting on a merge that would break `main`. The attempt record's `note` says which step failed; the stored report (`<run-id>-revalidate.json`) carries the L0/L1 output. The owner updates the branch (a fix round via PR rejection) or rejects the PR; `factory run` refuses to report a factory PR ready against a default head it hasn't been validated on.

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
| `repo` | Local path, default branch, remote, merge method. `branch_protection: unavailable` declares the plan-limit exception (F-26): GitHub answers the protection call with a 403 because the plan can't have protection, and the factory enforces up-to-date itself (re-validating open factory PRs against the current remote default head). `concurrency` caps open factory PRs per repo (default 1; a ready task waits while the limit is taken). |
| `commands` | `setup`, `lint`, `typecheck`, `test`, `build` — exact commands, run from repo root. |
| `baselines` | Path to a baseline file (test counts, known errors) if the project has one. |
| `paths.risk` | Globs whose change adds a risk flag (auth, migrations, RLS, payments, safety). |
| `paths.protected` | Globs workers may not modify: validator-owned tests, contract fixtures, CI config, the factory's own files. Workers may *add* tests elsewhere. |
| `dependency_manifests` | Files whose change means a new/changed dependency (`package.json`, `requirements*.lock`). |
| `sentinels` | Per risk flag, a list of `{command, description}` (F-25). Each command runs — after `commands.setup`, only — when the diff touches that flag's `paths.risk` globs; a failure FAILs L0 and escalates `sentinel_failed` (a flag with no `paths.risk` globs is refused at load). The built-in sentinels (credential added, skip marker, assertion removed, lint suppression in risk paths) always run; a credential find also escalates `credential_added` (rotate it: the provider saw it; nothing is pushed yet, and re-queueing starts a fresh branch). |
| `deploy` | Mechanism, whether merge auto-deploys, the deploy command if manual. |
| `live_checks` | Post-deploy commands the orchestrator runs **outside** the sandbox with secrets from the macOS keychain. |
| `holdouts` | Directory outside the repo (`~/factory/holdouts/<project>/`) and how to run it. |
| `docs_allowlist` | Domains the agent may fetch for docs grounding (both projects mandate official-docs lookups). |
| `standards` | Optional repo-relative paths of the project's coding-standards docs (e.g. `CODING_STANDARDS.md`, `CONTRIBUTING.md`). **Read by the L2 reviewer only**, never pushed to the implementer. Mechanical rules belong in L0 tooling, not here. |
| `cross_repo` | Sibling repos whose changes can break this one. v0: any task touching them → `needs_human`. |

**Out-of-scope register (`.factory/out-of-scope/`).** One markdown file per rejected *concept* (not per request): what was rejected, the durable reason, and the requests that asked for it. Only you write it. It's a protected path, and it records rejected enhancements, never features that turned out to exist already. The importer (F-12) and triage (F-18) read it, and a draft that resembles a record is shown to you with the record cited. Matching is by concept, not keyword, and **you confirm the match**: nothing is rejected automatically. Adapted from mattpocock/skills `triage`.

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
