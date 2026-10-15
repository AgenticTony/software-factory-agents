# Software Factory — Research Digest

**Purpose:** This project will build a software factory. This document distills 15 talks from the AI Engineer channel (`youtube.com/@aiDotEngineer`, search: "software factory") into the collective playbook — what a software factory is, the reference architecture, what works in production, why they fail, and a rollout order.

**Watched (Oct 15, 2026):**

| # | Talk | Speaker / Org |
|---|------|---------------|
| 1 | Harness Engineering: How to Build a Software Factory | Dru Knox, Tessl |
| 2 | What It Actually Takes to Build a Software Factory | Tereza Tížková, Factory |
| 3 | No, That's Not a Software Factory | Ryan Cooke, WorkOS |
| 4 | Why Software Factories Fail (a.k.a. "Harness Engineering Is Not Enough") | Dex (HumanLayer) |
| 5 | The Software Factory: From Bug Report to Production Code | Davis Palmie, Factory |
| 6 | Building Your Own Software Factory | Eric Zakariasson, Cursor |
| 7 | Software Engineering Is Becoming Factory Engineering | Zach Lloyd, Warp |
| 8 | Building Self-Improving Agent Software Factories | Suraj Gupta, Warp |
| 9 | Building Blocks for Uber's Software Factory | Uday Kiran Medisetty & Adam Huda, Uber |
| 10 | Dark Factory: OpenClaw Ships Faster Than You Can Read the Diff | Vincent Koc, OpenClaw |
| 11 | The Factory That Dreams: 39 AI Agents, No Framework | Rushabh Doshi, Machinecraft |
| 12 | Orchestras, Not Factories: How the Fastest Builders Work *(counterpoint)* | Charlie Holtz, Conductor |
| 13 | The Multi-Agent Architecture That Actually Ships | Luke Alvoeiro, Factory |
| 14 | Ship Production Software in Minutes, Not Months | Eno Reyes, Factory |
| 15 | Making Codebases Agent Ready | Eno Reyes, Factory |

*Not watched: "WF2026: Software Factories & Keynotes" — an 8.6-hour livestream; specific segments can be pulled on request.*

---

## 1. Consensus definition

Every speaker converges on roughly the same definition, each with a different emphasis:

- **Tessl:** A software factory is an agentic system where **the end product shipped to users is created by agents**; your engineering team's job becomes building the factory — making it more autonomous, more automated, and higher quality. Everyone becomes an internal tool builder.
- **Factory (Tížková):** It is the **whole lifecycle with autonomy** — collecting signals, prioritizing, orchestrating, executing, validating, testing in production, iterating, and continuously learning. It is **not** a coding agent, not even a swarm of coding agents, and not a consultancy deck. Coding was always the easy part.
- **Warp (Lloyd):** The discipline of software engineering is becoming **factory engineering**. The factory is a graph of steps every product shares: inputs → triage → spec → implement → review → verify → ship → monitor → loop. "You're not just building the product, you're building the thing that builds the product."
- **WorkOS:** The dream is that **each engineer gets a small engineering team for themselves** — measurable as accelerated feature delivery, not code output.
- **Conductor (counterpoint):** Prefers "orchestra" to "factory" — the human conducts, agents and humans interleave, and the feel should be flow and craft, not a production line. Same architecture, different stance on where the human sits.

**Tessl's three pillars, in build order:**
1. **Autonomy** — how rarely humans must correct the agent mid-task.
2. **Automation** — how much runs without human review (you can have high autonomy and low automation: it one-shots, but you still don't trust it).
3. **Quality** — hold constant during transition, then raise. The payoff of a factory is *quality* (backlog disappears; capacity goes to tests and refactors), not just speed.

**Dan Shapiro's autonomy ladder** (cited by Cursor): autocomplete → pair programmer → AI writes majority/human reviews → human delegates and manages → software factory ("dark factory": agents build, test, and ship; you provide intent only).

---

## 2. Reference architecture

Synthesizing Uber's six production building blocks, WorkOS's two-system split, Warp's factory floor, and Tessl's three layers, one shape repeats everywhere:

```
INPUTS                 CONTROL PLANE               EXECUTION PLANE            DATA / IMPROVEMENT PLANE
-----------            ------------------------     ----------------------     ------------------------
issue tracker    ──▶   triage agents         ──▶   sandboxed cloud      ──▶   transcripts & traces
Slack / support        spec-writing agents         environments (K8s/VM)      skills marketplace
user feedback          orchestrator agents         coding agents              memory / fact stores
monitoring/Sentry      PM agents (plans→tickets)   review agents              context graph / knowledge base
tweets / Discord       code-owner/risk agents      validators (incl.         evals & model routing
                       webhook router              computer-use QA)           meta-loop (self-improvement)
```

**Uber's six building blocks (the most complete production example — 70%+ of PRs now agent-authored, 2× LOC/engineer YoY, 250 automated migrations covering 9M LOC):**

1. **Model gateway** — every LLM call (internal + external tools) through one endpoint. PII anonymizer (20+ types), AI guard (5 safety models, <100ms), SPIRE identity, per-user/project/team cost attribution, caching/token optimization, audit logs + session traces feeding evals. 800+ projects, 100M requests/day.
2. **MCP gateway** — automated crawler wraps internal APIs into MCPs with one config change; token exchange for SaaS MCPs (Google, Slack, Jira); evolution: direct MCP → **Omni MCP** (one installable MCP that discovers/invokes all others) → **CLI pattern projection** (responses don't eat context) → code-mode skill (generates Python scripts to hyper-optimize top token consumers). 1,000+ tools, **40% fleetwide token savings**.
3. **Agentified devpods** — pre-provisioned Kubernetes pods with repos snapshotted and search index prebuilt, so agents start working in seconds; "mega devpod" spanning all repos for cross-repo agents.
4. **Skills marketplace** — 2,500 skills with lint checks + automated review for baseline quality; one command to install; persona-based auto-install; 20,000 executions/day; traces/comments/evals feed back to skill authors.
5. **Context graph** — 150 node/edge types, 40M entries connecting mobile apps, backend, data lake, design docs, Jira, incidents. The single biggest lever: massive reductions in tokens, turns, and latency on every eval they ran against it.
6. **Company-wide AI assistant** (Slack/CLI/web, personalizable team personas — 300 personas, 20k sessions/day) so non-engineers tap the same substrate.

**WorkOS's two systems:**
- **TARS** (interaction): embedded in Slack/Linear/GitHub; subscribes to webhooks so it tracks project progress, not just code. Linear ticket dependencies → auto-picks the next ticket when one completes; asks the agent to re-plan the project when gaps appear. A "PM" agent drafts their hill-top PRD (purpose, customer voice, competitive analysis, early designs, milestones) from a brief — humans edit rather than blank-page — and breaks approved PRDs into tickets.
- **Horizon** (orchestration): sandbox infra in front of an **MCP gateway / context engine** (semantic tables over Snowflake describing product usage and customer conversations, with tool descriptions telling agents *how the org uses each system*). The MCP gateway unexpectedly became leverage for the whole company (people query it from Slack directly). Next: an evergreen **memory layer** (who works on what, org semantics) portable across all AI tools.

**Key insight (WorkOS):** Their first factory version — sandbox + prompt → PR — was *indistinguishable* from engineers just using Claude Code on laptops. The value appeared only when they **encoded their product-engineering process** (PRDs, ticket dependencies, review rituals) into the factory.

---

## 3. The three loops (Tessl's harness engineering, endorsed by nearly everyone)

- **Inner loop** — fast, cheap checks the agent runs constantly while working on a PR (lint, typecheck, unit tests, quick builds). Drives **autonomy**.
- **Outer loop** — expensive, exhaustive checks when the PR goes up: agentic QA, mutation testing, computer-use validation, code review agents. Takes work humans would otherwise verify manually; builds the **trust** needed for automation.
- **Meta loop** — sits outside development; observes agent logs, PRs, issue tracker, user feedback; finds mistakes that leaked through or needed human correction; feeds fixes back into inner/outer loops (usually as new/updated skills). Drives **quality**. "How AI-native am I?" is a function of meta-loop investment.

**Why harness engineering is hard (Tessl):** (1) the discipline changes weekly — you become an AI researcher just keeping up; (2) it's fundamentally **unplanned work** that competes with shipping — if you never do it you're stuck in a local maximum, if you always do it you miss deadlines; (3) the signals you need are illegible — hidden in local agent logs and people's heads. Fix: move all agent touchpoints onto surfaces where everything is saved and inspectable.

**Self-improvement mechanics (Warp, in production on their open-source repo):**
- **Skills loop:** inner-loop agent applies a skill (e.g., GitHub issue triage); an outer-loop agent reviews its decisions + human feedback (thumbs, comments), synthesizes improvements, and **opens a PR against the skill** — version-controlled, human-reviewed, fully observable history.
- **Persistent memory:** per-agent fact store; outer-loop agent extracts facts/learnings/outcomes from runs; memories are versioned, human-editable, and **source-traceable** (you can open the run that produced each memory). Future runs report which memories they used.
- **Model routing with evals:** best-at-k evaluation across models per task class ("UI tasks are fine on GLM, don't run them on Opus"); rule-based routing config (DB migrations → GLM, runbooks/API docs → Qwen); eval sidecar keeps routing honest against *your* workflows, not public benchmarks.

---

## 4. Multi-agent orchestration (what actually ships)

**Warp's five primitive patterns:** delegation (parent spawns child), **creator-verifier** (fresh agent checks work — separation of concerns kills cost bias), direct communication (agent DMs — hard, state fragments), negotiation (over shared resources; best when positive-sum), broadcast (status to everyone — unglamorous but keeps long runs coherent).

**Factory's "Missions" — the most detailed shipping architecture (16-day runs, believe 30 possible):**
- **Three roles:** orchestrator (planning + the *validation contract*), workers (implementation), validators (verification).
- **Validation contract** — written **during planning, before any code**: hundreds of assertions defining what "done" means independently of implementation. Every feature maps to assertions; the sum of features must cover every assertion. This is the anti-drift mechanism for multi-day runs.
- **Workers run serially** — one worker at a time, each with clean context, committing via git so the next worker inherits a clean, working codebase. Parallel agents conflict, duplicate, and make inconsistent architectural decisions; error-rate compounding beats throughput gains. Read-only work (research, search, code review) parallelizes freely.
- **Structured handoffs** — a worker never just says "done": what was completed, what was left, commands run + exit codes, issues found, whether it followed procedures. Errors surface at milestone boundaries; corrective work gets scoped; the mission self-heals on paper trails, not memory.
- **Two validators, both adversarial by design** (never saw the code): *scrutiny validator* (tests, types, lints, per-feature code-review agents) and *user-testing validator* (spawns the app, clicks through it via computer use — "does this work end to end?" not "does the code look right?"). Most wall-clock time is spent here, not generating tokens.
- **Production numbers:** 60% of time and tokens on implementation; validation essentially never passes first try; final codebase ≈ 50% test code at 90% coverage; heavy prompt caching.
- **Bitter-lesson-proofing:** nearly all orchestration logic lives in ~700 lines of prompts/skills, not a hardcoded state machine — four sentences can change execution strategy; the deterministic layer is thin bookkeeping only. The architecture also *compensates for weaker models* (contracts + checkpoints let open-weight models succeed).
- **Right model per seat:** planning = slow careful reasoner; implementation = fast, code-fluent, creative; validation = precise instruction-follower — possibly a *different provider* than the implementer so it isn't biased by the same training data.

**Machinecraft (39 agents, zero framework, running a real factory's go-to-market):** one agent = one job (Athena orchestrates, Prometheus owns the sale, Plutus prices, Hephaestus knows machine specs, Vera fact-checks, Memnon guards corrections so "the second a human fixes something, it stays fixed"). Agents **hold meetings and argue**; a single answer comes out. Layered memory (working / pinned facts / episodes / relationships / salience gate) plus a **nightly dream cycle** that replays the day, locks in useful knowledge, forgets stale junk, and distills the day's work into reusable skills — with a morning dream report. Golden rule: **Eira drafts, a human sends.** No fine-tuning anywhere — the "brain" is well-organized memory (vectors + relationship graph) over their own private history. Cost: ~$30k to build (vs. a $230k agency quote), ~$2k/month to run. Architecture forked as "Brain OS."

---

## 5. Prerequisites: agent readiness (the step everyone skips)

This got more airtime than anything else. **A messy codebase plus agents equals a worse codebase, compounding** (Factory cites Stanford data and a growing productivity gap between thoughtful and careless adopters — a power law: you succeed big or fail big).

**Factory's 8 readiness pillars:** (1) validation — linters/formatters so opinionated that agent output *always* matches senior-engineer quality; (2) documented build system & CI; (3) feedback loops — unit/integration tests, including **tests that fail on AI slop** ("a slop test is better than no test" — people and agents upgrade them); (4) docs — readmes, `agents.md`; (5) cleanly reproducible dev environments; (6) modularity with clear boundaries and rules against sprawl; (7) observability — how long to find out *why* something is wrong; (8) proactive security scanning.

**The core argument (Factory, Eno Reyes):** software is the most verifiable domain — that's why coding agents lead. The frontier of what agents can do is an **input function of your validation criteria**. The limiter on "bug reported → agent fixes → human approves → deployed in an hour" is *not model capability, it's your org's validation*. This is where 5–7× comes from, and it's an investment, not a purchase. New DevX flywheel: better agents → better environment → better agents.

**Google/Meta vs. everyone (Factory):** the difference isn't talent — it's that a new grad can ship a trivial change to YouTube without taking it down, because of validation density. Agents now both *expose* these gaps and *remediate them* ("ask an agent where our linters aren't opinionated enough").

**Cursor's practical checklist — primitives, guardrails, enablers:**
- **Primitives:** modular/colocated code (an agent `ls`-ing one folder beats grepping a scattered repo — same as human onboarding), established usage patterns (auth service, startup scripts, test boilerplate) so you can point agents at existing references.
- **Guardrails:** hooks protecting sensitive areas (encryption, auth); **rules that emerge dynamically** when agents go off the rails — *not* pre-installed rule packs. Best internal example: they don't use foreign keys for perf reasons; models always add them anyway; a review-bot rule flags every FK in a migration PR. That rule is "the bridge between model behavior and human intent."
- **Enablers:** skills/MCPs, and a **feature-flag skill** so autonomous agents can merge flagged and let humans decide later (revert or roll forward); environments agents can boot themselves.
- Checklist: is it **runnable**? **accessible** (can agents reach Linear/Notion/Datadog/Slack)? **verifiable** (unit/integration/UI tests the agent can run itself)?

**Conductor's "slop-free zones":** parts of the codebase that always require human review (their CI blocks AI on the migrations file); Slack is assumed human-written; and unusual investment in CLAUDE.md/skills — "if you could whisper something in a new intern's ear every time they sat down to work, you'd choose those words carefully. That's what agents.md is."

---

## 6. Why factories fail

The strongest warnings in the set, chiefly from HumanLayer's talk:

- **Harness engineering cannot fix a model-training problem.** RL rewards "tests pass," not maintainability; the cost function of bad architecture is measured in months and years, so the reward signal can't propagate back through the episode. Result: models can't *maintain and improve codebase quality over time* — shotgun surgery accumulates. (Emerging benchmarks — Sweep Marathon, Deep Sweep, Cognition Frontier Code with judge models and test-quality penalties — are directionally right but early.)
- **The industry data is ugly:** since mass agent adoption, code-review quality is down (more/longer comments, PRs merged unreviewed), incidents are up, bugs per developer are up. Companies that shouldn't have outages are having agent-caused outages.
- **"Lights-off" (nobody reads code) fails in brownfield.** HumanLayer ran it fully in mid-2025: eventually an agent-unsolvable issue appears, the site is down, and you're miserable reading slop you stopped understanding months ago. Agents start degrading after ~3–6 months of unread codebase.
- **Their fix — turn the lights back on:** product review → architecture (component contracts, data models) → **program design** (types, method signatures, layout, call stacks — badly under-emphasized) → vertical slices (implementation order + check points). "30 minutes of pre-planning saves hours of review." And: **"you don't have too many PRs; you have too many bad PRs"** — a good PR is a joy to review even at volume, because it matches what was aligned up front.
- **Cheating agents (Factory):** write the done-criteria wrong and the agent optimizes for *passing your tests*, not accomplishing the intent. Hence validation contracts written before code, plus user-testing validators that actually click the product.
- **Output metrics lie (WorkOS, Factory):** % of AI PRs and LOC counts disguise whether the factory works. Measure outcomes.
- **Scope discipline (Factory):** precise execution does not prevent poor design. Humans stay for architecture, strategy, direction — as validation gates. Roll out pillar by pillar; loop the pipeline *last*.
- **Cursor's guardrails:** never outsource important decisions (security, auth, payments, databases); system-critical code should be human-written or always human-reviewed; humans remain accountable for what ships (brownouts/blackouts as agents enter time-critical paths). Their security team runs ~10 "security sentinel" invariants over sensitive PRs; red-team with compute *before* a human ever looks.
- **Conductor's "don't beat the market":** for any workflow trend (Ralph loops, etc.) ask *"why isn't this the default?"* If it works for everyone, the harness vendor will absorb it — wait. Only invest workflow effort where you have **real alpha**: knowledge about your users or codebase the models don't have. Otherwise you're "midwit meming" — the amazing Emacs config of agentic coding. (Conductor rewrote their app twice before instituting slop-free zones.)
- **OpenClaw's scars:** git worktrees at scale nuked a machine (70–80 active worktrees; prefer cloning the repo N times); the real bottlenecks became **raw compute and human brain space**, not tokens; and detecting a derailing agent is a soft skill — "you can feel the reasoning tokens… it sounds off because of *how it's explaining itself*, not what it's doing." Exactly like managing people who start bullshitting you: kill the session.

---

## 7. Cost & model strategy

- **Coinbase pattern (via Factory):** token spend kept growing while AI *costs* fell — default models (not frontier), aggressive caching, no spend caps but results required, smart routing.
- **Factory auto-routing:** classify task difficulty from prompt/codebase/tool usage → pick the **cheapest model above the threshold**, switch on failure. Conservative benchmark: ~25% savings, plus reliability (open models are often faster; provider failover). Same GPT-5.2 code review as Opus at half price; open source 10–30× cheaper.
- **Uber:** 40% fleetwide token savings from gateway-level caching + the MCP-to-CLI projection + code-mode scripts.
- **Deferred context (Factory):** progressively disclose tools — short descriptions up front, full load on demand, nothing deleted, just hidden. At scale (hundreds of tools), 50%+ token savings.
- **Factory Missions:** heavy prompt caching on long runs.
- **OpenClaw:** "2025 was token maxing. 2026 is token efficiency."
- **Uber governance:** spend guardrails per team via gateway attribution — visibility first, limits second.

---

## 8. Context, memory, and organizational knowledge

- **Uber's context graph is the biggest single lever** in the whole set — connecting code, owners, design docs, incidents, Jira, data lake (40M entries) collapses the "where is this / who owns it / what pattern do I follow" tax before every task.
- **Factory:** agents fail mostly from *missing context*, not insufficient intelligence — and the missing context lives in the cracks: standups, whiteboard photos, meeting transcripts. Factory transcribes every meeting; docs/process become "a knowledge base and a map for agents to imitate how your team thinks." PRD → auto roadmap/tickets → parallel droids.
- **Conductor "feed the beast":** a CIA agent watches Slack/Discord/meetings and writes everything to Postgres; agents get a SQL tool over it. "Put everything in a database and give your agent SQL."
- **WorkOS:** evergreen memory layer — who works on what, team semantics, org-level semantics — shared across factory and other AI tools.
- **Warp:** versioned, source-traceable per-agent memory stores; runs report which memories they used.
- **Cursor:** mine your own transcripts — automations that (a) extract repetitive tasks from chat history and propose skills, (b) read *merged-PR human review comments* (highest-signal intent data) and store learnings, (c) "continual learning" plugin that turns corrections into rules automatically.
- **Machinecraft:** memory with a salience gate (don't remember junk), contradiction resolution (corrections win), and nightly consolidation. Continuity without confabulation.

---

## 9. Metrics that matter

| Good (outcomes) | Bad (gameable output) |
|---|---|
| Manual takeovers ↓, human PR comments ↓ (Tessl) | % of AI-written PRs (WorkOS) |
| % PRs initiated without human input ↑ (Tessl) | Lines of code / token counts (Factory: "Goodhart's law in real time") |
| Signal → production time (Factory) | Token leaderboards |
| Human intervention count per unit of work (Factory) | |
| Median time to repair, defect rate, time to recovery (Factory, WorkOS) | |
| Code shelf life (Factory) | |
| Cost per PR (Factory) | |
| Voluntary adoption — engineers choosing the cloud sandbox over local (WorkOS) | |
| Shipped value per unit human-time + token cost (Warp) | |
| Tests as % of LOC and coverage as *byproducts* of runs (Factory Missions: 50%/90%) | |

---

## 10. Human role & operating style

- **Decide what, not how** (Factory). History as abstraction ladder: human computers → programming languages → coding agents → managed agent teams.
- **Worker → manager; sync planning → async execution** (Cursor). Real operating rhythm at Cursor: ~4 repos and 5–10 async cloud agents live at all times, one synchronous planning/conversation task in the foreground; plan synchronously, execute asynchronously, review as things land. "There's no shortcut — you have to spawn a lot of agents with good guardrails and watch what happens."
- **Front-load context** — long specs/plans before launching long-running agents (Cursor); argue with the orchestrator about scope, approve the plan, then walk away (Factory).
- **Feel the models** (Cursor "alignment with the models"; Factory's "droid whispering"; OpenClaw's Matrix intuition) — learn each model's failure modes and how they compound across roles.
- **Orchestras, not factories** (Conductor): human at the center conducting intermingled humans and agents; collaboration (shared agent workspaces, humans chatting inside agent tasks in real time) as the coming interface shift; free-range agents with APIs to spawn their own workspaces (OpenClaw texting a Conductor API to kick off work).
- **Non-technical contribution rises** (Tessl): once work flows through tickets and legible surfaces, anyone can propose; engineers curate the factory.
- Zach Lloyd hasn't written a line of code in six months and ships constantly — "if your joy is writing code, that's a bummer; if it's shipping product, it's never been a better time."

---

## 11. Security & governance

- Uber: PII redaction, identity (SPIRE), AI guard middleware, audit logs + session traces feeding evals, per-team spend attribution, RBAC.
- Factory: every action auditable, least privilege, role-based access; enterprise questions to answer *before* scaling: audit logs, indemnification, ownership when an agent does something destructive. "YOLO mode is not an enterprise deployment."
- WorkOS: agent **authorization** is an unsolved open problem (they asked the room; nobody has it figured out).
- Cursor: security sentinel invariants on sensitive paths; protect auth/encryption/payment code with hooks; humans accountable for ships.
- Conductor: slop-free zones enforced in CI.
- Factory Missions: block progress automatically on unaddressed handoff issues.

---

## 12. Rollout playbook (synthesized consensus order)

1. **Get agent-ready first** (Factory's 8 pillars; Uber's monorepo/Bazel foundation). Agents can do this step for you — "ask an agent where your linters aren't opinionated enough."
2. **Build the control plane** (Tessl): every work item → issue tracker → headless agent in sandbox → PR → comments. All agent touchpoints legible and stored. Skills registry with versioning + review.
3. **Start with one narrow, verifiable pillar** — incident triage is the canonical first win (Sentry alert → pull traces → post diagnosis to Slack → human decides). Trust compounds on correct diagnoses.
4. **Add verification depth**: inner-loop checks agents run themselves → outer-loop agentic QA + code-review agents → computer-use user testing. Write validation contracts before code. Shift checks left (Uber validates in the inner loop before CI; drafts don't hit CI until validated).
5. **Wire the lifecycle**: webhooks so tickets with dependencies chain automatically; agent re-plans when gaps appear (WorkOS); routing incidents and user feedback straight into the queue; monitoring output feeds back as input.
6. **Add the meta loop** (only now): skills-improvement agents PR-ing skill updates; memory extraction; transcript mining; model routing with evals; scheduled maintenance sweeps (feature-flag cleanup, flaky-test hunts) on managed, bounded loops scheduled off-peak (Uber: Sundays, capped Monday diff volume).
7. **Scale serially, not as a swarm**: one worker at a time with clean context; parallelize read-only work only (Factory). Scale environments (isolated VMs per agent, pre-provisioned), then fleet (Cursor runs thousands/day).
8. **Keep humans at the gates**: architecture, program design, security, auth/payments/DB decisions, and slop-free zones. Measure outcomes. Improve quality *after* automation, not before.

---

## 13. Open problems (flagged by speakers, unsolved)

- Agent **authorization** (WorkOS explicitly punted to the room).
- **Maintainability as a trainable reward** — no benchmark yet captures it (HumanLayer).
- Managing the **markdown sprawl** of rules/skills/specs — "somewhat unsolved" even at Cursor; their answer is a factory-spec folder in-repo + a review council for what enters the factory.
- **Team alignment on shared factory rules** — engineers privatize rules to their machines; needs the same ceremony code review once solved (Cursor Q&A).
- Scaling **CI capacity and experiment slots** as commit volume explodes (Uber).
- Lovable's pattern (via Cursor): give agents a **vent tool** that complains to Slack — the complaints turned out to be a high-value harness bug tracker. Cheap, weird, worth stealing.

---

## 14. Per-talk one-liners

1. **Tessl (Knox):** Factory = agents build the product, humans build the factory; autonomy → automation → quality via inner/outer/meta loops; harness engineering is unplanned, fast-moving work that must be made legible.
2. **Factory (Tížková):** Whole-lifecycle autonomy; agnostic + autonomous + always-improving; sequential workers, adversarial validators (~40% of effort), validation contracts; deferred tool context; readiness determines whether AI helps or compounds mess.
3. **WorkOS (Cooke):** Sandbox→PR alone adds nothing over laptops; encode your real process (PRDs, dependencies, webhooks); MCP context engine is company-wide leverage; measure outcomes not output; authorization unsolved.
4. **HumanLayer:** The harness is not enough — models can't hold codebase quality; RL can't reward maintainability; lights-off fails in brownfield; plan (incl. program design) so review stays feasible; "too many bad PRs, not too many PRs."
5. **Factory (Palmie):** Signal → production system across all SDLC roles; model-agnostic + sovereign + integrated; O(N)→O(1) standardization; start with triage, loop last; measure signal-to-prod, interventions, MTTR, shelf life, cost/PR.
6. **Cursor (Zakariasson):** Primitives/guardrails/enablers; rules emerge; feature flags de-risk autonomy; manager mindset, async, 5–10 agents live; mine transcripts for skills/rules; agentic code owners; Linear → auto cloud agent per ticket; don't outsource the important decisions.
7. **Warp (Lloyd):** Every sizable project gets a factory like it got CI/CD; triage-first ("easy+unambiguous → implement"); product spec + tech spec; agent review before human review as risk management; skill loops with observer agents; build-in-the-open as strategy.
8. **Warp (Gupta):** Self-improvement = skill-improvement PRs + versioned traceable memory + eval-driven model routing (GLM for UI etc.).
9. **Uber (Medisetty & Huda):** Six building blocks at real scale; gateway + MCP gateway + agentified devpods + skills marketplace + context graph + assistant; shift-left validation; self-healing CI; managed maintenance loops; bottlenecks move to CI capacity, experiments, and *should-we-build* decisions.
10. **OpenClaw (Koc):** 3,000 commits/day; swim lanes as factory lines; the great refactor (2,700 commits, ~1M LOC, 82% of core) survived on overfit tests; worktrees will hurt you; bottlenecks become compute + your brain; token efficiency is 2026's theme.
11. **Machinecraft (Doshi):** 36 specialist agents, no framework, biological memory architecture + nightly consolidation; corrections are permanent; drafts vs. sends separation; company brain for ~$30k; forkable.
12. **Conductor (Holtz):** Orchestras, not factories — flow over automation theater; stay *near* the frontier but don't beat the market without alpha; slop-free zones; feed the beast (everything → DB → SQL); free-range agents + collaboration + agent-spawned workspaces.
13. **Factory (Alvoeiro):** Five multi-agent primitives; Missions = orchestrator/workers/validators with validation contracts, serial workers, structured handoffs, dual adversarial validation; 16-day runs; prompts-not-state-machines keeps it bitter-lesson-proof.
14. **Factory (Reyes, ship):** Bottleneck is human attention; agents fail on missing context in the cracks; transcribe everything; docs = map of how your team thinks; incident response in minutes; reactive → predictive ops.
15. **Factory (Reyes, readiness):** Automation-via-verification; the 8 pillars; your validation ceiling is your autonomy ceiling; slop tests beat no tests; agents can remediate their own environment.

---

*Synthesized from full transcripts, Oct 15 2026. Original transcripts (cleaned) retained at `/tmp/watch_sf/*/clean.txt` until cleanup.*
