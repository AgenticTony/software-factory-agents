# How the AI Labs Actually Run Internal Software Factories

Companion to [RESEARCH-SOFTWARE-FACTORY.md](RESEARCH-SOFTWARE-FACTORY.md) (talk synthesis). This doc covers what Anthropic, OpenAI, Google DeepMind, Nvidia, and Meta have **published about their own internal operations** — formal papers where they exist, engineering blogs and exec statements where they don't. Researched 2026-10-15.

---

## TL;DR

1. **Formal papers exist but are rare.** The big three publish internal telemetry mainly as engineering blog posts, not papers. The genuine paper exceptions: OpenAI's ["The Shift to Agentic AI: Evidence from Codex"](https://arxiv.org/abs/2606.26959), DeepMind's [AlphaEvolve white paper](https://arxiv.org/abs/2506.13131), and Meta's ["An Open-sourced AI Software Engineer at Industrial Scale"](https://arxiv.org/abs/2512.10398).
2. **The "90% of code written by AI" claim is roughly right but team-specific.** Anthropic company-wide is ~80% AI-authored as of mid-2026; the Claude Code team is at 90–95% (tests ~100%); and by Jan 2026 both Anthropic's and OpenAI's leads said flagship projects are ~100% AI-written.
3. **The single best "how it operates" source** is Anthropic's Oct 2026 post on [redesigning source control](https://www.anthropic.com/engineering/redesigning-source-control): agents merged 30,000+ changesets/day, which broke Git, so Anthropic built a custom server-side version-control system (Scion) and moved testing to **post-merge, pre-deploy** — the literal inversion of the classic PR gate.
4. **The labs' factories are not dark factories.** Humans still set intent, review semantics, and own deploy decisions. The machines own volume: authoring, conflict resolution, first-pass review, and continuous optimization.

---

## 1. The 90% claim — what the numbers actually are

| Claim | Who / when | Source |
|---|---|---|
| ~50% of merged code AI-written (early estimate) | LessWrong analysis of Anthropic diffs, Oct 2025 | [LessWrong](https://www.lesswrong.com/posts/prSnGGAgfWtZexYLp/is-90-of-code-at-anthropic-being-written-by-ais) |
| **80%+ of merged code authored by Claude** (company-wide, telemetry) | Anthropic, May 2026 | [When AI builds itself](https://www.anthropic.com/institute/recursive-self-improvement) |
| ~80% company-wide (Dario-adjacent reporting) | VentureBeat, Jun 2026 | [VentureBeat](https://venturebeat.com/ai/anthropic-says-nearly-80-percent-of-code-written-by-ai/) |
| **90–95% on the Claude Code team; tests ~100%** | Boris Cherny (Claude Code lead) | [Fortune, Jan 2026](https://fortune.com/2026/01/29/100-percent-of-code-at-anthropic-and-openai-is-now-ai-written-boris-cherny-roon) |
| **"100% of code" on flagship projects at both Anthropic and OpenAI** | Cherny + roon (OpenAI), Jan 2026 | [Fortune](https://fortune.com/2026/01/29/100-percent-of-code-at-anthropic-and-openai-is-now-ai-written-boris-cherny-roon) |
| 100% of Nvidia engineers code with AI; 3× code volume | Jensen Huang / reporting, Feb 2026 | [Tom's Hardware](https://www.tomshardware.com/tech-industry/artificial-intelligence/nvidia-now-produces-three-times-as-much-code-as-before-ai-specialized-version-of-cursor-is-being-used-by-over-30-000-nvidia-engineers-internally) |

**Reading:** "90%" was true for flagship teams in late 2025–2026 and is the trajectory for everyone else. The honest company-wide number mid-2026 is ~80%, doubling roughly with each capability generation (Anthropic reports task time-horizons doubling every ~4 months).

---

## 2. Anthropic — the deepest public account of a running factory

### 2a. Scale broke their tooling, so they rebuilt the tooling
[Redesigning source control for agentic coding](https://www.anthropic.com/engineering/redesigning-source-control) (Oct 2026):

- **Merges grew from ~2,500/day (May 2026) to 30,000+/day (Sept 2026)** — almost all agent-driven. Their ~100 GB monorepo's Git history exceeded the size of any individual checkout; Git's local-first design (expensive clones, local history) collapsed under always-on agents.
- **Built "Scion"**, a custom version-control system: thin always-online clients, server-side commits/changesets, diffs/blame/ancestry exposed via API (so tools like code review or CI consume version control as a service), tiered caches at >99% hit rate, Spanner backend. Claims: ~200× smaller infra, 100× faster checkouts, 25% CI CPU saved.
- **Built "Antique"**, a review tool designed for machine-writable, human-intent-focused review.
- **Post-merge, pre-deploy testing.** The defining operational inversion: instead of gating merges on test suites, changes merge and are tested continuously before deploy. Quote: *"Claude can typically fix these conflicts faster than humans can even identify them."* Bottleneck moved from merge queue to deploy gate.
- **Review is intent-based**: semantic approval constraints, directory-level merge permissions, and **"Claude Tag superfans"** — always-on agents that monitor specific components, dig into anomalies, and file fixes.
- Human role: set intent and constraints, spot-check semantics. Agents: author, resolve conflicts, run first-pass review, monitor.

### 2b. Recursive self-improvement telemetry
[When AI builds itself](https://www.anthropic.com/institute/recursive-self-improvement) (May 2026):

- 80%+ of merged code Claude-authored; **8× code/day vs 2024**.
- A pre-merge Claude reviewer would have caught **~⅓ of past incident bugs** — automated review is now a standing control.
- Claude-driven optimization: speedups went 3× (May 2025) → **52× (Apr 2026)**; an 800-fix campaign cut one API error class **1000×**. Quality held at parity with the pre-agent baseline.
- Explicitly frames risks (compounding errors, silent capability drift, homogenization) — the meta-loop concerns from the talk synthesis.

### 2c. How the team that builds Claude Code works
[How Claude Code is built (Pragmatic Engineer)](https://newsletter.pragmaticengineer.com/p/how-claude-code-is-built): the Claude Code team runs itself as the reference factory — agents write ~90–95%, humans own product intent and review.

---

## 3. OpenAI — the one with a real telemetry paper

- **Blog:** [How agents are transforming work](https://openai.com/index/how-agents-are-transforming-work) (Jun 2026).
- **Paper:** ["The Shift to Agentic AI: Evidence from Codex"](https://arxiv.org/abs/2606.26959) — internal telemetry from rolling out Codex company-wide:
  - ~**97.9% employee adoption** (technical and non-technical).
  - Researchers generated **~50× more output within 6 months** of adoption.
  - **137× growth in non-developer use** (legal, support, ops) — agents leaking out of engineering into the whole company.
  - Through Aug 2025 (early rollout): average worker spent <10% of tokens on Codex; 25.6% of employees made ≥1 request — i.e., the factory started as a minority habit and compounded.
  - Longer task horizons and higher complexity per task over time.
- Context: roon (Jan 2026, [Fortune](https://fortune.com/2026/01/29/100-percent-of-code-at-anthropic-and-openai-is-now-ai-written-boris-cherny-roon)): flagship projects ~100% AI-written.
- Sept 2026 follow-up: ["Research Acceleration: The View Inside OpenAI"](https://openai.com/index/research-acceleration-view-inside-openai) — same pattern applied to research workloads.

**Takeaway:** OpenAI's published evidence is adoption/telemetry-centric (who uses it, how much, how complex), not architecture-centric. The architecture story (sandboxes, review, CI) is not public the way Anthropic's is.

---

## 4. Google DeepMind — AlphaEvolve: the factory applied to algorithms

[AlphaEvolve white paper](https://arxiv.org/abs/2506.13131) (Novikov et al., 2025; [blog](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms), [problem repo](https://github.com/google-deepmind/alphaevolve_repository_of_problems)):

- Evolutionary coding agent: population of programs, LLM proposes mutations, automated evaluator (real tests/benchmarks) selects — **the outer loop is fully machine-graded**.
- **Internal deployment results:** recovered ~0.7% of Google's worldwide compute footprint; improved datacenter scheduling (Borg); sped up 4×4 complex matrix multiplication (first improvement on Strassen's 56-year-old result in that setting); accelerated kernel and TPU circuit design.
- Made public on Google Cloud in Jul 2026.
- This is the clearest published case of an **autonomous improve-the-factory loop**: not just writing features, but evolving the code that runs the company.

---

## 5. Nvidia — internal Cursor fork at 30,000-engineer scale

[Tom's Hardware, Feb 2026](https://www.tomshardware.com/tech-industry/artificial-intelligence/nvidia-now-produces-three-times-as-much-code-as-before-ai-specialized-version-of-cursor-is-being-used-by-over-30-000-nvidia-engineers-internally):

- **30,000+ engineers** on a specialized internal fork of Cursor.
- **3× code volume** vs pre-AI baseline; 100% of engineers use AI.
- At that volume, human review is impossible — hence heavy investment in **AI code review** as the control plane.
- Jensen's framing: "100% of engineers code with AI" — note this is *usage*, not *authorship share*; authorship share is the more aggressive Anthropic/OpenAI metric.

---

## 6. Meta — the open-sourced industrial-scale agent

["An Open-sourced AI Software Engineer at Industrial Scale"](https://arxiv.org/abs/2512.10398) (Dec 2025) — Meta's **Confucius Code Agent (CCA)**: a build–test–improve loop agent that operates reliably in massive monorepos. Notable because it's the closest thing to a lab publishing its factory-worker design as an actual paper + open source.

---

## 7. The wider academic paper trail (2025–2026)

Not lab-internal, but peer-review-grade material on the factory model:

| Paper | What it contributes |
|---|---|
| [Agentic Software Engineering: SASE / SE 3.0](https://arxiv.org/abs/2509.06216) (Hassan et al.) | Foundational pillars for agent-run SE |
| [The Rise of AI Teammates in SE 3.0](https://arxiv.org/abs/2507.15003) (Li et al.) | Survey; agents initiating/reviewing/evolving code at scale |
| [Agentic AI in Industry: Adoption Level and Deployment](https://arxiv.org/html/2605.14675v1) | Empirical adoption study across industry |
| [The End of Software Engineering](https://arxiv.org/html/2606.05608v1) | "Agentic Engineering" as a new discipline |
| [Agentic AI Software Engineers: Programming with Trust](https://arxiv.org/html/2502.13767v3) | Trust requirements ≥ human processes |
| [Are We All Using Agents the Same Way?](https://arxiv.org/abs/2601.20106) | Empirical developer-agent collaboration study |
| [Measuring Agents in Production](https://arxiv.org/abs/2512.04123) | Evaluating deployed agents in real production |
| [Empirical Study of Agent Developer Practices](https://arxiv.org/html/2512.01939v1) | How agent builders actually work |

Counterpoint worth keeping: METR's RCT found experienced devs on mature repos were initially ~19% *slower* with agents — factory gains are real but repo-dependent (matches the "brownfield fails without planning" finding in the talk synthesis).

---

## 8. What this changes for OUR factory (vs. the talk synthesis)

The talk synthesis (RESEARCH-SOFTWARE-FACTORY.md) described factories being built in 2025–2026. The lab sources show where this goes at scale, and they revise several assumptions:

1. **Version control is a factory component, not a given.** At 30k merges/day Anthropic abandoned Git's local-first model for a server-side VCS with API-exposed history. We won't need Scion, but design the control plane assuming **VCS as a service consumed by agents** (thin clients, server-side operations), not heavyweight local clones.
2. **Move the gate from merge to deploy.** Post-merge/pre-deploy testing with auto-fix loops is the labs' answer to PR-queue collapse. Our outer loop should budget for "merged ≠ shipped; deploy gate is the quality boundary."
3. **Intent-based review beats line-based review at volume.** Semantic approval constraints, directory-level permissions, always-on component-watcher agents ("superfans") as the monitoring tier.
4. **The improve-the-factory loop is real and measured.** AlphaEvolve (0.7% of Google's compute) and Anthropic's 800-fix/1000× error-reduction campaigns show the meta loop compounding. Fund it from day one.
5. **Adoption compounds non-linearly.** OpenAI: <10% of tokens early → 50× output in 6 months → 137× non-dev growth. Instrument everything from day one; the telemetry IS the paper.
6. **Agents leak beyond engineering.** Plan the factory's interfaces (and auth) for non-developer orchestration from the start.
7. **Quality gates hold the line.** Anthropic reports quality parity *because of* standing automated review (would've caught ⅓ of incident bugs) — not despite the volume. Validation investment is the enabler of autonomy, exactly as the Factory talks argued.

---

## Source index (all primary)

- Anthropic: [Redesigning source control](https://www.anthropic.com/engineering/redesigning-source-control) · [When AI builds itself](https://www.anthropic.com/institute/recursive-self-improvement)
- OpenAI: [How agents are transforming work](https://openai.com/index/how-agents-are-transforming-work) · [arXiv:2606.26959](https://arxiv.org/abs/2606.26959) · [Research Acceleration](https://openai.com/index/research-acceleration-view-inside-openai) · [press summary](https://the-agent-report.com/2026/06/openai-shift-to-agentic-ai-codex)
- DeepMind: [AlphaEvolve paper](https://arxiv.org/abs/2506.13131) · [blog](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms) · [repo](https://github.com/google-deepmind/alphaevolve_repository_of_problems)
- Meta: [CCA paper](https://arxiv.org/abs/2512.10398)
- Nvidia: [Tom's Hardware report](https://www.tomshardware.com/tech-industry/artificial-intelligence/nvidia-now-produces-three-times-as-much-code-as-before-ai-specialized-version-of-cursor-is-being-used-by-over-30-000-nvidia-engineers-internally)
- Claim tracking: [Fortune (Cherny & roon)](https://fortune.com/2026/01/29/100-percent-of-code-at-anthropic-and-openai-is-now-ai-written-boris-cherny-roon) · [LessWrong estimate](https://www.lesswrong.com/posts/prSnGGAgfWtZexYLp/is-90-of-code-at-anthropic-being-written-by-ais) · [VentureBeat](https://venturebeat.com/ai/anthropic-says-nearly-80-percent-of-code-written-by-ai/) · [Pragmatic Engineer](https://newsletter.pragmaticengineer.com/p/how-claude-code-is-built) · [JetBrains research](https://blog.jetbrains.com/research/2026/08/how-much-code-do-developers-really-let-agents-write)
