# Local Software Factory — Design (v2)

**v2, 2026-10-15.** Supersedes v1. It applies every fix from [REVIEW-IMPROVEMENTS.md](REVIEW-IMPROVEMENTS.md) and records the build decisions (§7).

**Goal:** point one factory at an existing repo and its work queue (JobFinderOS work orders, SoloAdventurer execution order) and let it work through the queue item by item. You own intent, risk decisions and releases.

**Read with:**
- [TASK-CONTRACT.md](TASK-CONTRACT.md) — what the factory consumes
- [VALIDATOR-SPEC.md](VALIDATOR-SPEC.md) — how "done" is proven
- [`schema/`](schema/) and [`projects/`](projects/) — machine-readable contract and draft per-project adapters
- Background: [RESEARCH-SOFTWARE-FACTORY.md](RESEARCH-SOFTWARE-FACTORY.md), [RESEARCH-AI-LAB-INTERNAL-FACTORIES.md](RESEARCH-AI-LAB-INTERNAL-FACTORIES.md)

---

## 1. What we take from the labs, and what we don't

Anthropic's published factory ([Redesigning source control](https://www.anthropic.com/engineering/redesigning-source-control), Oct 2026) made four moves:

1. **VCS as a service:** thin clients; commits, diffs and history on the server.
2. **Merge freely, gate the deploy:** testing after merge, with agents fixing conflicts.
3. **Review the intent, not the lines:** machine-written review records, semantic constraints, directory permissions.
4. **Standing watchers ("superfans"):** always-on agents per component that file fixes.

All four are responses to **30,000 merges/day on a ~100 GB monorepo**. A solo factory runs 2–4 orders of magnitude lower. So:

| Move | v2 decision | Why |
|---|---|---|
| VCS as a service | **No.** Plain Git + GitHub. Agents get a fresh partial clone per task. | Git only broke at their scale. A custom VCS rebuilds the problem it solved. |
| Merge freely, test after | **No (for now). Full suite before merge.** Post-merge testing is a scale trigger (§3). | At tens of merges/day the merge queue isn't a bottleneck, and testing first keeps `main` clean. |
| Intent review | **Yes.** Every PR carries the approved contract plus the validator report (L2). | Cheap, and it's what makes reviewing at volume possible. |
| Directory permissions | **Yes.** `paths.risk` + `paths.protected` in each project profile, plus CODEOWNERS/branch protection. | Built-in and free. |
| Watchers | **Later (Phase 3)**, and only through the triage quarantine. | Watchers ingest untrusted signals (errors, logs), so the Rule of Two applies. |

What the labs have that we can't copy, and what replaces it:

| Labs have | We use instead |
|---|---|
| In-house models and eval harnesses | Deterministic validation layers (L0, L1, L3, L4) carry most of the weight |
| Mostly trusted, internal input | Untrusted-input quarantine |
| Dedicated infra teams | Commodity tools only, and a deliberately thin orchestrator |

What we add that they haven't published:
- prompt-injection session separation (Meta's Rule of Two)
- locked tests and hidden holdout scenarios (StrongDM, ImpossibleBench)
- dependency gating (slopsquatting)
- live verification before `done`

## 2. Principles

1. **Validation is the ceiling on autonomy.** Every human gate removed must be replaced by a check that earns it.
2. **Merged ≠ deployed ≠ live.** These are three separate recorded facts. A task is `done` only after live verification.
3. **One writer per repo at a time.** Parallelise only read-only work (research, review). Errors compound faster than throughput grows.
4. **No session holds all three of:** untrusted input, secrets, and the ability to push or deploy.
5. **Agents can't edit what judges them:** contract tests, holdouts, validator config, CI, the factory itself.
6. **Thin deterministic core, prompts for behaviour.** The orchestrator is bookkeeping. Agent behaviour lives in versioned prompt files.
7. **Measure outcomes.** Interventions per task, escalation reasons, validator catch rate, revert rate, time from signal to live. Never lines of code.

## 3. Scale triggers — build each mechanism when its forcing function arrives

| Forcing function (measured on our system) | Then build |
|---|---|
| Any agent touching a repo | Day one: task contract, pre-merge validation, branch protection, metrics |
| First project live-verified by the factory | Holdout scenarios + automatic L4 |
| A second project onboarded | Cross-repo dependency handling |
| >1 component in production with error telemetry | First watcher, via the triage quarantine |
| You can't skim the daily summary in 5 minutes | Intent dashboard: contract + verdict + exceptions, diffs on demand |
| Validator catch rate ≥ 90% and revert rate < 5% over 30 merges in a task category | Auto-merge for that category behind feature flags. Risk-flagged tasks never auto-merge. |
| One writer can't keep up and the queue has independent tasks on disjoint files | Parallel writers, one per file set |
| **Merge-queue wait** becomes the measured bottleneck | Post-merge, pre-deploy testing with auto-revert |
| Thousands of merges/day or a repo >10 GB | Revisit server-side VCS |

## 4. Architecture (v2)

```
                    YOU: write/approve work orders + contracts (intent)
                                     │
  untrusted signals ──▶ TRIAGE QUARANTINE (Phase 3)        project repo: .factory/tasks.json
  (Sentry, issues)      no secrets · no push · outputs                    .factory/project.yaml
                        structured draft tasks only ───────▶ TASK QUEUE ◀──┘ (agents never write it)
                                                                │
┌───────────────────────────── ORCHESTRATOR (Python, thin) ─────┴──────────────────────────────┐
│ picks next ready slice · enforces budgets/attempt caps · holds GitHub token + prod secrets   │
│ (keychain) · pushes branches · opens PRs · runs L3/L4 · writes tasks.json + daily summary    │
└───────┬──────────────────────────────────────────────────────┬───────────────────────────────┘
        │ one writer at a time                                  │ read-only, parallel OK
        ▼                                                       ▼
┌── SANDBOXED SESSION (Seatbelt / srt) ──────────┐      ┌── REVIEW SESSION ─────────────────┐
│ fresh partial clone · writes only in run dir   │      │ fresh context · read-only tools   │
│ network: model endpoint + docs + registries    │      │ sees contract + diff, not the     │
│ no GitHub token · no prod secrets              │      │ implementer's narrative (L2)      │
│ hooks deny edits to protected paths            │      └───────────────────────────────────┘
│ implement → L0 → L1 (inner loop, own checks)   │
└───────┬────────────────────────────────────────┘
        ▼
  L0 + L1 re-run on a clean clone → L2 review → L3 holdouts ──FAIL (≤3 attempts)──▶ needs_human
        │ PASS
        ▼
  PR (contract + validator report) ──▶ YOU: merge (v0: every PR; risk-flagged: always)
        ▼
  deploy (JobFinderOS: manual, Render + wrangler · SoloAdventurer: Vercel on merge)
        ▼
  L4 live checks ──FAIL──▶ revert PR + needs_human + repo queue paused
        │ PASS
        ▼
  owner steps open? ──yes──▶ awaiting_owner ──(you tick them)──▶ done
        │ no
        ▼
       done ──▶ metrics ──▶ (Phase 4) meta loop: delta edits to skills only, never to evaluators
```

The inline blueprint diagram from this session shows the same flow.

## 5. Where you step in (v0)

| Point | What you do | Can it go away later? |
|---|---|---|
| Before a task starts | Approve its contract (acceptance criteria + risk flags). Batch this in a weekly planning pass. | No. It's the intent. It gets faster, not removed. |
| Escalation | Resolve `needs_human`: give a ruling, re-scope, or take over | Becomes rarer as validation improves |
| Merge | Merge the PR after reading the contract, verdict and diff | Yes for low-risk categories, via the §3 auto-merge trigger. Never for risk flags. |
| Deploy (JobFinderOS) | Trigger the deploy, or approve the orchestrator to run it | Yes, once L4 has a clean track record |
| Owner steps | Live credentials, device tests, store, legal, picking the pilot city | No. These aren't software. |

## 6. Components to build

| Component | v0 scope | Reuse |
|---|---|---|
| `factory` CLI (orchestrator) | `import`, `approve <id>`, `run` (one slice), `loop` (until queue empty, quota hit or escalation), `status`, `verify-live <id>` | — |
| Contract validation | JSON Schema check of `tasks.json` + `project.yaml` on every load; refuse to run on invalid state | `schema/*.json` |
| Session runner | Starts one agent session in a sandboxed clone with seat-specific model, env, tools, hooks and turn cap | — |
| Implementer prompt | One slice, red-first test, conventional commit, structured report | SoloAdventurer `.claude/agents/implementer.md`, generalised |
| Reviewer prompt | Default FAIL, contract-vs-diff, project checklist | SoloAdventurer `.claude/agents/verifier.md`, generalised |
| Importer prompt | Proposes `tasks.json` from planning docs; you review the diff | — |
| L0/L1 engine | Deterministic: commands, baseline, diff policy, red-first proof | Rules from `verifier.md` |
| L3/L4 runner | Holdout runner, `live_checks` from the profile | `ops/verify_deployment.sh`, `preflight.py`, `check-schema-refs.py`, migration-drift workflow |
| Daily summary | Markdown: done, in review, needs you (with reasons), quota used | — |

## 7. Decisions

### D1 — Substrate: Claude Code engine, driven by a thin Python orchestrator
- Each agent seat (implementer, reviewer, importer) is one **Claude Code session** started by the orchestrator. That reuses Claude Code's tools, hooks, sandbox and turn caps, plus your existing agent prompts, instead of writing an agent loop.
- **v0 runs headless Claude Code** (`claude -p … --output-format json --max-turns N`) behind a `run_session()` function. **The Claude Agent SDK (Python) is the drop-in upgrade** once D2's terms question is answered. It gives in-process hooks and typed results, and drives the same engine.
- No agent framework, no state machine beyond the status set. Same stance as Machinecraft and Factory Missions: orchestration in prompts, bookkeeping in code.

### D2 — Models: GLM via Z.ai's Anthropic-compatible endpoint
- **Mechanics (verified against Z.ai docs, 2026-10-15).** Each session gets these in its env:
  - `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`
  - `ANTHROPIC_AUTH_TOKEN=<z.ai key from keychain>`
  - `API_TIMEOUT_MS=3000000`

  Because env is set **per session**, each seat can use a different provider. The single-provider limit applies within one session, not to the factory.
- **Model names.** Z.ai's own pages disagree: the Claude Code guide maps Opus/Sonnet to GLM-4.7, while the plan FAQ says plans cover GLM-5.3 and GLM-5.3-Flash. Don't hard-code a mapping. Run one session, check `/status`, and record the actual model in `attempts[].model`.
- **Terms caveat (open, must resolve before unattended runs).** The Coding Plan is "strictly limited to use within officially supported tools". Claude Code is listed; the Agent SDK isn't named; unattended or automated use isn't addressed either way. Actions:
  1. Ask Z.ai support in writing whether scheduled headless Claude Code runs are allowed under your plan.
  2. Until they answer, v0 uses headless Claude Code (the listed tool), not the SDK.
  3. If they say no, the fallback is Z.ai's pay-as-you-go API key on the same endpoint. Nothing else changes.
- **Quota contention, checked in the repo.** JobFinderOS's *product* calls GLM through Z.ai's **Coding Plan endpoint**: `backend/.env.example` sets `GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4`, and CLAUDE.md calls it a "flat-rate key". Matching and tailoring in production hunts run on it. Confirm the production env on Render matches. If it does:
  - **the factory and production hunts would compete for one quota**, and a busy factory night could starve real users' hunts
  - a production backend is almost certainly not an "officially supported tool" under the plan's terms

  **Separate them before Phase 1**: production on a metered API key (or the Mistral-hosted GLM path already sketched in `.env.example`, MIG-WO5); the factory on the Coding Plan.
- **Reviewer diversity.** v0 runs both seats on GLM, with separate sessions and different prompts. Self-preference bias is real, so L0/L1/L3/L4, none of which a model judges, are the main guard. Phase 4 adds a cross-provider reviewer (e.g. Claude Sonnet on an Anthropic API key) for risk-flagged tasks. The reviewer reads a lot and writes little, so it's the cheap seat to put on a second provider.

### D3 — Sandbox: native macOS Seatbelt plus a clone per task
- **Isolation:** each run gets `~/factory/runs/<project>/<run-id>/`, a `git clone --filter=blob:none` of the local repo at the target branch. No worktrees: OpenClaw's scars, and JobFinderOS standard 7 (concurrent sessions collide).
- **Sandbox:** Claude Code's built-in sandbox / Anthropic's `srt` (Seatbelt on macOS, no container):
  - writes are allowed only in the run dir
  - reads are denied for `~/factory/holdouts`, `~/.ssh`, `~/.config/gh` and other projects
  - network is limited to the model endpoint + `docs_allowlist` + `network_allowlist`

  `srt` is a beta research preview, so pin its version.
- **Secrets split (Rule of Two):** the sandbox holds one secret, the model key. It has **no GitHub token**: the agent commits locally, and the orchestrator pushes and opens the PR. **No prod credentials**: L4 runs outside, with secrets from the macOS keychain. That also enforces JobFinderOS standard 8 (tests never touch the live DB) structurally.
- **Not Docker in v0.** It's heavier on a Mac and not needed for one serial writer. JobFinderOS's CI already covers Linux and Docker parity. Revisit if sandbox-vs-CI differences start causing false passes.

### D4 — Budget: caps exist to catch derailing, not to save quota
**Your plan: legacy Max, paid yearly in January 2026.** Under Z.ai's [usage revision notice](https://docs.z.ai/devpack/notice/usage-revision) (Jul 30 2026), existing subscriptions keep their terms until the current billing cycle ends. Yours is a one-year term paid January 2026, so the legacy quota runs to **≈ January 2027**: about three months, enough for Phases 0–2. The exact date is on the [My plan](https://z.ai/manage-apikey/coding-plan/personal/my-plan) page.

**Open question for Z.ai (send with the D2 terms question):** the notice also says "users on discontinued plans can still renew". Does renewing keep the legacy prompt-based quota, or move you to credits? If renewing keeps it, renew before the term lapses.

| | Until the cycle ends (legacy, prompt-based) | After renewal (credits plan) |
|---|---|---|
| Max quota | **~1,600 prompts / 5 h, ~8,000 / week** | 28,000 credits / 5 h, 140,000 / week |
| Unit | 1 prompt ≈ one query ≈ 15–20 model calls (Z.ai's estimate) | Tokens (input + cached + output) × model multiplier |
| Peak pricing | GLM-5.3 costs **3× at peak, 1× off-peak**. GLM-5.3-Flash 1.2× / 0.4×. | Off-peak at 50% (overview page); confirm the multipliers |

**Peak hours:** Mon–Fri 14:00–18:00 Singapore time = **08:00–12:00 Swedish summer time, 07:00–11:00 winter time.** Weekends count as off-peak all day.

**What this means:** at roughly 15–20 model calls per prompt, a 200-turn slice is on the order of 10–15 prompts. 8,000 prompts a week is hundreds of slices. **Quota is not the constraint on the legacy plan.** The real limits are one writer at a time and your review capacity. So the caps below exist to **stop a derailing agent early**, not to ration quota. A cap that's too tight just turns good runs into escalations, which costs your attention.

- **Per session (calibration period):** S slice = 80 turns / 60 min; M slice = 200 turns / 150 min. An L must be sliced.
  - The clock **stops** during setup and during L0–L3 validation, so a slow test suite can't time out a correct implementation.
  - Each clone gets a **warm dependency cache** (prebuilt venv + `node_modules` copied in), not a fresh install.
- **Per slice:** max 3 attempts (1 build + 2 fix rounds), same as your `loop.md`. Then `needs_human`.
- **Per factory:**
  - **Schedule runs outside 08:00–12:00 weekdays** (07:00–11:00 in winter). Weekends and nights are cheapest.
  - Factory warns at **50% of the weekly quota** (a soft limit) and stops at 75%, so interactive work keeps headroom.
  - On the first quota/429 error, stop the window, mark `quota_exhausted`, don't retry-loop.
- **After two weeks:** set real caps from data. Take the ~90th percentile of turns and minutes across successful slices × 1.5. Track how often caps trigger escalations, as a sign they're still too tight.
- **Before the renewal (≈ January 2027):** re-measure on credits. Record `input_tokens` / `output_tokens` per attempt from day one so the credits cost can be projected before the switch, not discovered after it.
- **Production must not share this quota** (see D2). On the legacy plan, JobFinderOS hunts and the factory would draw from the same 8,000 prompts.

### D5 — Human gates in v0
Every contract is approved by you; every PR is merged by you; risk flags are permanent human gates. Gates come down per category only through the §3 auto-merge trigger.

## 8. Phased build plan

**Phase 0 — Prerequisites (1–2 days)**
- Get Z.ai's written answer on automated headless use (D2).
- Move JobFinderOS's production AI off the Coding Plan endpoint (the example config uses it; confirm on Render) (D2).
- JobFinderOS readiness run: every `project.yaml` command passes unattended on a clean clone. Fix the `TODO(verify)` lines in [`projects/jobfinderos.project.yaml`](projects/jobfinderos.project.yaml).
- Branch protection on `main`; keychain items for the live-check secrets.

**Phase 1 — v0 on JobFinderOS (pilot)**
- Orchestrator CLI: schema validation, picker, session runner, sandbox, L0 + L1 + L2, PR creation, daily summary.
- Import the WO queue; you review the import diff; approve 2–3 WOs. Good first candidates are self-contained build WOs with code-checkable criteria. Skip WO-13 (payments), WO-27 (decision) and WO-29/30 (research-gated).
- Deploy stays manual. `factory verify-live <id>` runs L4 when you deploy.

**Phase 2 — Trust**
- L3 holdouts (3–5 scenarios), automatic L4 after deploy detection, the factory metrics, and the first monthly seeded-bug run.

**Phase 3 — Second project and watchers**
- Onboard SoloAdventurer Web. Cross-repo tasks escalate to you until cross-repo handling exists.
- First watcher (Sentry → triage quarantine → draft tasks for your approval).

**Phase 4 — Self-improvement and gate dial-down**
- Meta loop: weekly review of escalations and findings → **delta edits** to prompts and skills as PRs. It never touches validators, holdouts or contract tests (ACE, DGM).
- Cross-provider reviewer for risk-flagged tasks. Auto-merge per category once §3's trigger is met.

## 9. Metrics (instrumented from the first run)

- Interventions per task (approvals excluded), escalations by reason, attempts per slice
- **Validator catch rate per layer** (monthly seeded bugs)
- Revert rate, L4 failure rate, time from approval to live
- Credits/tokens per slice and per done task, quota-exhaustion events
- Never: lines of code, PR count, token leaderboards

## 10. Risks and honest gaps

- **Same-model review (v0).** Mitigated by the non-model layers; fixed in Phase 4.
- **Coding Plan terms.** Unresolved until Z.ai answers. The fallback is metered API, with no architecture change.
- **`srt` is beta.** Pin the version; keep the secrets split as the real boundary, not the sandbox alone.
- **Single machine.** Repos and `tasks.json` are in Git; run dirs are disposable; holdouts need a backup (they live outside the repos).
- **Agent authorization stays unsolved industry-wide.** The secrets split, per-session capability limits and human merge are today's state of the art.
- **Cross-repo blindness (SoloAdventurer).** Web can't see mobile's schema changes. v0 refuses cross-repo tasks rather than pretending to check them.

## Sources
- [Anthropic — Redesigning source control](https://www.anthropic.com/engineering/redesigning-source-control) · [Effective harnesses for long-running agents](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Z.ai — Claude Code setup](https://docs.z.ai/scenario-example/develop-tools/claude.md) · [Coding Plan FAQ](https://docs.z.ai/devpack/faq) · [Coding Plan overview / quotas](https://docs.z.ai/devpack/overview)
- [Anthropic sandbox-runtime (srt)](https://github.com/anthropic-experimental/sandbox-runtime) · [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing)
- Requesty, Agent SDK with a custom base URL: the SDK reads `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` — [docs](https://docs.requesty.ai/integrations/anthropic-agent-sdks.md)
- Rest: see [REVIEW-IMPROVEMENTS.md](REVIEW-IMPROVEMENTS.md) sources
