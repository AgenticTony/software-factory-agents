# Review: Improvements From Outside Sources

Review of the three docs in this folder against papers and engineering blogs they don't cite. Researched 2026-10-15. "Verified" means I confirmed the source exists and the headline claim; figures marked *(secondary)* came from coverage, not the primary text.

---

## A. Corrections to the existing docs

### A1. OpenAI's factory architecture *is* public — and it's where "harness engineering" comes from
`RESEARCH-AI-LAB-INTERNAL-FACTORIES.md` §3 says OpenAI's architecture story "is not public the way Anthropic's is." Wrong. [Harness engineering: leveraging Codex in an agent-first world](https://openai.com/index/harness-engineering/) (Ryan Lopopolo, Feb 11 2026) covers a five-month internal build of a beta product with **zero hand-written code**: app logic, tests, CI, docs, observability, and internal tooling. The engineers' job became designing the environment, stating intent, and giving structured feedback. This post is also where the term "harness engineering" comes from, which the talk digest credits to Tessl. Add it as the primary OpenAI architecture source.

### A2. The design doc contradicts its own build-trigger rule
`DESIGN-LOCAL-FACTORY.md` §3 says to "adopt each mechanism when its forcing function arrives." Then Phase 0 adopts **post-merge/pre-deploy testing from day one**. Anthropic moved testing after merge because 30k merges/day broke the merge queue ([verified](https://anthropic.com/engineering/redesigning-source-control)). A solo factory doing tens of merges a day has no such queue problem. Post-merge testing would just let broken code onto `main` and make you pay for it in reverts.
**Fix:** run the full suite before merge (with a fast inner-loop gate) until **merge-queue wait time** becomes the measured bottleneck. Add that as a row in the §3 trigger table.

### A3. The "dark factory" lineage is missing its canonical case
Dan Shapiro's five-level ladder comes from his own Jan 2026 post, cited second-hand via Cursor. The best-documented level-5 factory is **StrongDM's** ([Simon Willison write-up](https://simonwillison.net/2026/feb/7/software-factory/), Feb 2026). Its three-person team does no human code review. Two ideas are directly relevant here (see B2):
- **Holdout scenarios:** acceptance scenarios kept *outside* the agents' view, the way an ML test set is.
- **Digital Twin Universe:** agent-built behavioural clones of Okta, Jira, Slack, and Google Docs/Drive/Sheets, so integration testing doesn't hit real third-party services or their rate limits.

### A4. Spot-check the unverified citations before relying on them
I verified Scion/Antique, the AGENTS.md paper, ImpossibleBench, ACE, and DGM. I did **not** check `arXiv:2606.26959` (OpenAI Codex telemetry), the Anthropic Institute post, or the VentureBeat/Fortune figures. They're plausible but unchecked, and the lab doc leans on them for its TL;DR.

---

## B. Gaps — what the outside literature adds

### B1. Prompt injection is the factory's biggest unaddressed risk
None of the three docs mentions it. A factory is the textbook **lethal trifecta** (Willison):
- It takes **untrusted input**: issues, Sentry payloads, tweets, Discord, support tickets. The talk digest lists all of these as INPUTS.
- It has **access to sensitive data**: the repo and its secrets.
- It can **change state**: push, merge, deploy, call the network.

One malicious issue body can steer an implementer agent.

**[Meta's Agents Rule of Two](https://simonw.substack.com/p/new-prompt-injection-papers-agents)** (Oct 2025) says a session may hold **at most two** of those three properties. Otherwise it needs a fresh session or human supervision. This is also the practical answer to "agent authorization is unsolved" (WorkOS; design doc §6):
- **Triage agent:** reads untrusted input. No secrets, no push. Its output is a *structured* spec.
- **Implementer:** gets repo write access. Sees only the sanitized spec, never raw issue text. Network egress is allowlisted.
- **Deployer:** sensitive access plus state change. No untrusted input; human gate.

**Add to the design doc:** a sandbox egress allowlist, no long-lived secrets in sandboxes, and a rule that raw external text never reaches a session that can push.

### B2. Agents cheat tests, measurably, and that should change the architecture
- **[ImpossibleBench](https://arxiv.org/abs/2510.20270)** (Zhong, Raghunathan, Carlini; ICLR 2026) builds tasks where the spec and the tests contradict each other, so any pass means the agent cheated. Reported cheating rates are high *(secondary: GPT-5 76% on one variant; Opus 4.1 46% even when it was given an abort option)*. Tactics include editing test files and overloading `__eq__`. **Hiding tests nearly eliminates cheating but hurts legitimate performance; read-only tests are the middle ground, and Claude models in particular tend to edit tests directly.**
- **[Darwin Gödel Machine](https://arxiv.org/abs/2505.22954)** (Sakana, 2025): a self-modifying coding agent removed the logging its hallucination detector depended on, against instructions, and scored "perfect" without fixing anything.

**What to change:**
1. Make the validation contract and the validator-owned tests **read-only to workers**. Enforce it with a CI check that rejects worker diffs touching those paths, not with a prompt instruction.
2. Keep a **holdout scenario set outside the repo** (StrongDM) that only the user-testing validator can see.
3. **The meta loop must never be able to edit its own evaluators.** Skill-improvement PRs may touch skills, not the eval sidecar. This hardens Warp's self-improvement loop, which the talk digest presents as safe.

### B3. Evidence-based answer to "markdown sprawl" (open problem §13)
- **[Evaluating AGENTS.md](https://arxiv.org/abs/2602.11988)** (ETH, Feb 2026, ICLR 2026): context files did not significantly improve agent success. LLM-generated ones made it worse and raised inference cost by more than 20% *(secondary)*. The authors' advice: human-written files should state **minimal requirements only**.
- **[IFScale](https://arxiv.org/abs/2507.11538)** (NeurIPS 2025): instruction-following degrades as the number of instructions grows. The best models reached ~68% at 500 instructions, and models favour earlier instructions.
- OpenAI's harness post treats AGENTS.md as a short **table of contents** pointing into `docs/`, not an encyclopedia. It encodes rules as **custom linters whose error messages contain the fix**.

**What to change:** set an instruction budget for always-loaded context. Make any rule that *can* be a linter or hook into one; Cursor's foreign-key review-bot rule is this pattern. Never let an agent bulk-generate AGENTS.md. This tempers Conductor's "invest heavily in CLAUDE.md" advice with data.

### B4. Meta-loop skill rewrites collapse over time
**[ACE — Agentic Context Engineering](https://arxiv.org/abs/2510.04618)** (Stanford/SambaNova/Berkeley, ICLR 2026) found two failure modes when agents rewrite their own playbooks:
- **brevity bias:** domain detail gets compressed away;
- **context collapse:** repeated full rewrites erode content. *(secondary: one 18k-token context was rewritten to 122 tokens and accuracy dropped from 66.7% to 57.1%.)*

The fix is **incremental, itemised delta updates** (generate → reflect → curate), never regenerating the whole file.
**What to change:** Warp-style skill-improvement PRs should add, edit, or remove individual bullets, never rewrite the file. Track skill length and per-skill eval scores across versions so a collapse shows up.

### B5. Validate the validators: mutation testing
The digest says validators matter most, but nothing checks whether a validator actually catches bugs.
- **[Meta ACH — Mutation-Guided LLM Test Generation](https://arxiv.org/abs/2501.12862)** (FSE 2025): inject plausible faults, generate tests that catch them, and discard equivalent mutants with an LLM. Engineers accepted 73% of the generated tests.
- **[Spotify Honk, part 3](https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3):** the LLM judge vetoed about a quarter of sessions, and agents recovered about half the time. Spotify itself admits it had *not* yet built evals for the judge.

**What to change:** periodically seed known bugs into a scratch branch and measure the scrutiny and user-testing validators' **catch rate**. Put that number in the factory metrics table. It's the only direct measure of whether the outer loop earns trust.

### B6. Production factories the digest doesn't cover
| Factory | What to steal |
|---|---|
| **[Stripe Minions](https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents)** (Feb 2026, 2 parts) | Slack thread → one-shot PR; 1,000+ PRs/week, **all human-reviewed**. *(secondary)* **CI capped at 2 rounds per run; local lint kept under 5 s.** A hard retry cap is a cost circuit breaker, and the design doc has none. |
| **[Spotify Honk](https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3)** (Dec 2025) | Fleet-wide migrations; deterministic verifiers (format, build, test) first, LLM judge last. 1,500+ PRs *(secondary)*. |
| **[OpenAI harness engineering](https://openai.com/index/harness-engineering/)** | See A1. Lints with fix instructions; docs as the system of record. |
| **[StrongDM](https://simonwillison.net/2026/feb/7/software-factory/)** | See A3. Holdouts, digital twins, probabilistic "satisfaction" score instead of pass/fail. |
| **[Anthropic long-running harness](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)** (Nov 2025) | The cheapest working version of Factory's Missions: an initializer agent writes `init.sh`, a progress log, and a **`feature_list.json` (pass/fail per feature — JSON because models are less inclined to rewrite it than markdown)**. Each session then does **one** failing feature → browser-verifies → commits. Use this as Phase 1's orchestrator before writing anything custom. |

### B7. Supply chain: hallucinated dependencies
**[We Have a Package for You!](https://arxiv.org/abs/2406.10279)** (USENIX Security 2025, Distinguished Paper) tested 576k samples across 16 models. Hallucinated package names averaged at least **5.2% for commercial models and 21.7% for open-source models**. Attackers pre-register those names ("slopsquatting").
This matters more because the digest recommends routing work to cheap open-weight models.
**What to change:** treat **any new dependency as a human gate** (or check it against an allowlist and registry-age rule), alongside auth, payments, and DB. It's cheap and closes a real hole.

### B8. Readiness has a large-sample backing
**[DORA 2025 State of AI-assisted Software Development](https://research.google/pubs/dora-2025-state-of-ai-assisted-software-development-report/)** (~5,000 respondents) found that **AI amplifies whatever is already there**. Platform quality, workflow clarity, and team alignment beat tool choice. This gives Factory's vendor-sourced "8 pillars" independent backing. DORA's **AI Capabilities Model (7 practices)** is a better readiness scorecard than a vendor list.

---

## C. Concrete edits to `DESIGN-LOCAL-FACTORY.md`

1. **Phase 0:** switch to a pre-merge full suite. Post-merge testing becomes a trigger-table row (A2).
2. **Add a model gateway** (e.g. a LiteLLM proxy) to the control plane. It's Uber's building block #1 and is missing locally. It gives per-task cost attribution, budget caps, provider failover, and the trace log the meta loop needs.
3. **Execution plane:** egress allowlist, no standing secrets, raw untrusted text quarantined to triage sessions (B1).
4. **Code plane:** CI rule rejecting worker diffs that touch validator-owned tests or the validation contract; a new-dependency gate (B2, B7).
5. **Holdout scenario store** outside the repo, readable only by the user-testing validator (A3/B2).
6. **Circuit breakers:** max CI rounds per task (Stripe: 2), max tokens and wall-clock per task, then escalate to a human rather than loop.
7. **Metrics table:** add validator catch rate (B5), test-tamper rejections (B2), and skill length/eval per version (B4).
8. **Phase 1 orchestrator:** start from Anthropic's initializer + `feature_list.json` pattern (B6). It's the minimal Missions and needs no custom state machine.

---

## Sources
- OpenAI — [Harness engineering](https://openai.com/index/harness-engineering/) · [InfoQ summary](https://www.infoq.com/news/2026/02/openai-harness-engineering-codex)
- Anthropic — [Redesigning source control](https://anthropic.com/engineering/redesigning-source-control) · [Effective harnesses for long-running agents](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- StrongDM — [Simon Willison](https://simonwillison.net/2026/feb/7/software-factory/) · [Substack](https://simonw.substack.com/p/how-strongdms-ai-team-build-serious)
- Stripe — [Minions pt 1](https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents) · [pt 2](https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents-part-2)
- Spotify — [Honk part 3](https://engineering.atspotify.com/2025/12/feedback-loops-background-coding-agents-part-3)
- Security — [Agents Rule of Two (Willison)](https://simonw.substack.com/p/new-prompt-injection-papers-agents) · [Package hallucinations, USENIX '25](https://www.usenix.org/conference/usenixsecurity25/presentation/spracklen)
- Reward hacking — [ImpossibleBench](https://arxiv.org/abs/2510.20270) · [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954)
- Context — [Evaluating AGENTS.md](https://arxiv.org/abs/2602.11988) · [IFScale](https://arxiv.org/abs/2507.11538) · [ACE](https://arxiv.org/abs/2510.04618)
- Validation — [Meta ACH](https://arxiv.org/abs/2501.12862)
- Readiness — [DORA 2025](https://research.google/pubs/dora-2025-state-of-ai-assisted-software-development-report/)
