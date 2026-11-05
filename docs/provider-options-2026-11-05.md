# Model providers for client work: options and costs (2026-11-05)

**Status:** research note, not a decision. Prices were collected from the web on 2026-11-05 and many come from aggregators. Verify each one on the provider's own pricing page before quoting it.

## The question

Client repos can't go to the Z.ai endpoint. This note looks at which models and hosts could run client work through this factory, and what each would cost.

Today's own-repo work stays on the Z.ai GLM plan, which has a flat price and carries only our own data.

## Constraints that come from the factory

- **The interface.** Every seat runs through `claude-agent-sdk`, which speaks the Anthropic Messages API. `session_env` sets `ANTHROPIC_BASE_URL` and a token. A provider must therefore do one of these:
  - Expose an Anthropic-compatible endpoint (Anthropic, Bedrock, Vertex, Foundry, and a few open-model hosts).
  - Sit behind a translation proxy such as LiteLLM. That proxy becomes a new component inside the trust boundary, so it needs pinning, a sandbox egress rule and its own review.
- **Bedrock, Vertex and Foundry.** These use cloud credentials rather than a base URL plus token. The Agent SDK supports them natively, but `session_env` and the credential checks currently assume the Z.ai shape, so supporting them is a work order.
- **Auth.** Anthropic's docs say products built on the Agent SDK should use API keys, not subscription OAuth, and that Claude usage can't be resold. Use an API key for client work. A Team plan's monthly API credits (up to $500) apply only through a linked Console API key.
- **Kinds of client work expected:** web apps (Next.js, FastAPI), AI integrations, voice assistants and automation. That calls for a strong generalist coder, with frontier quality where a verdict matters.

## Why the cached-read price matters most

Over the last 30 days, all repos used 10.62B tokens with a 93.6% cache hit rate. At that ratio, the price of cached input dominates the bill, so the right comparison is the cached rate, not the headline input rate.

## Scenario: client work is ~3B tokens a month

That's about 30% of current volume, with 93.6% cached (~2.81B), ~0.17B fresh input and ~0.75% output (~22M).

| Model / host | EU processing | DPA | Anthropic-compatible | $/M tokens (input / cached / output) | Monthly cost, scenario |
|---|---|---|---|---|---|
| **Claude Sonnet 5.5, Bedrock EU** (Stockholm/Ireland; Frankfurt via EU profile) | Yes (Bedrock in-region / EU geo) | AWS DPA | Native (SDK supports Bedrock) | $2 / $0.20 / $10, if parity with first-party holds (unverified) | **~$1.1k** |
| Claude Opus 5.5, Bedrock EU | Yes | AWS DPA | Native | $4 / $0.20 / $20 | ~$1.9k |
| Claude via Anthropic API | **No**: `inference_geo` is `global` or `us` only | Anthropic DPA | Native | Same as Bedrock | Same; US/global processing (SCCs) |
| Claude via Microsoft Foundry | **Not yet**: runs on Anthropic infrastructure; EU listed as "coming 2026" | MS + Anthropic | Native | First-party rates | Same; not EU today |
| GLM-5.3, EU host (Lyceum: Paris/Finland) | Yes | Yes (terms Annex 1) | Yes | $1.40 / $0.26 / $4.40 | **~$1.1k**, no cheaper than Sonnet because its cached rate is higher |
| GLM-5.3, Scaleway | EU | Not checked | No (needs a proxy) | €1.80 / – / €5.50 | ≥ ~$1.1k |
| DeepSeek V4 Flash, Lyceum EU | Yes | Yes | Yes | $0.25 / $0.06 / $0.30 | **~$0.2k** |
| Qwen3.8 27B, Lyceum EU | Yes | Yes | Yes | $0.40 / $0.10 / $2.40 | ~$0.4k |
| Devstral 2, Mistral EU endpoint (+10%) | Yes (`api.eu.mistral.ai`) | Mistral (EU company) | Not stated; assume a proxy is needed | $0.40 / ? / $2.00 | ~$0.25k with a cache discount, ~$1.3k without one |
| Claude Haiku 5.5 (cheap seats only) | Via Bedrock EU, if offered there | AWS DPA | Native | $0.10 / – / $0.50 | <$0.1k |

The cost column is plain arithmetic on the listed rates. Prompt-cache writes (about 1.25× the input rate) are folded into fresh input.

## Origin of each model

Some clients care where a model comes from, not only where it is hosted.

- **US:** Claude.
- **EU:** Mistral/Devstral.
- **Chinese origin, open weights:** GLM (Zhipu, which is on the US Entity List), DeepSeek, Qwen and Kimi.
  - Running open weights on an EU host sends nothing to the vendor.
  - Some clients, especially US and regulated ones, will still refuse Chinese-origin models as a matter of policy. Ask the client rather than assume.

## Quality (directional; benchmarks disagree and many are vendor claims)

- **Frontier:** Claude Opus 5.5 and Sonnet 5.5.
- **Strongest open models for long agentic coding:** GLM 5.2/5.3 and Kimi K3. Qwen3.8 and DeepSeek V4 are close behind.
- **Devstral 2:** about 62–72% on SWE-bench Verified depending on who ran the evaluation. Cheap, and from an EU company.
- **Measure it ourselves.** `factory eval` (seeded diffs) plus one JobFinderOS task per candidate is the real test. Each candidate needs one eval run.

## Recommendation

1. **Default for client repos:** Claude Sonnet 5.5 on Bedrock EU.
   - It costs about the same as EU-hosted GLM at our cache ratio.
   - It's a frontier model from a US vendor, which clients won't object to.
   - It processes data in the EU and the SDK supports it natively, so no proxy is needed.
   - Escalate to Opus 5.5 for the verifier/final seat or for hard tasks.
2. **Cheap seats:** reviewer fan-out and the importer can run on Haiku 5.5 or DeepSeek V4 Flash via Lyceum, but only after `factory eval` shows no loss in catch rate. A cheap reviewer that misses bugs costs more than it saves.
3. **EU-sensitive clients:** offer Mistral/Devstral (EU vendor) as a choice. It needs the LiteLLM proxy work order, so build that only when a client asks.
4. **Own repos:** stay on the Z.ai GLM plan.
5. **Factory work order (to draft): per-project provider.**
   - Add `provider` to `project.yaml`, covering z.ai, anthropic, bedrock, vertex and anthropic-compatible URLs.
   - Support Bedrock/Vertex credentials in `session_env`.
   - Write a provider record per run (which model processed which repo); this is the DPA evidence.
   - Refuse a client project that has no explicit provider.
   - Add a sandbox egress allowlist per provider.
6. **Price it in.** Model cost is about 5–15% of revenue at Sonnet rates. Add it as a line in each quote and track it per job from the run records.

## Still to verify before committing

- Bedrock EU prices for Sonnet 5.5 and Opus 5.5 (is parity real?) and which EU regions offer them in-region.
- Whether Haiku 5.5 is on Bedrock EU.
- Whether Mistral offers an Anthropic-compatible endpoint, and its cached-input price.
- Lyceum's DPA terms and sub-processors.
- The ratio of output to input tokens in our real runs. The traces may double-count, so measure it with one tagged run.

## Sources

- [Claude Agent SDK legal and compliance (auth for products)](https://code.claude.com/docs/en/legal-and-compliance)
- [Claude data residency, `inference_geo`](https://platform.claude.com/docs/en/manage-claude/data-residency)
- [Foundry EU timeline thread (Microsoft Q&A)](https://learn.microsoft.com/en-sg/answers/questions/5867930/timeline-for-claude-in-microsoft-foundry-to-run-on)
- [Bedrock model card, Claude (AWS EU docs)](https://docs.aws.eu/bedrock/latest/userguide/model-card-anthropic-claude-fable-5.html)
- [Bedrock EU regions (innFactory)](https://innfactory.ai/de/ki-modelle/anthropic-claude/)
- [Requesty EU Claude pricing (aggregator)](https://www.requesty.ai/eu/claude)
- [EU-hosted LLM API providers compared (Lyceum, read 2026-10-20; vendor-written)](https://lyceum.technology/magazine/eu-hosted-llm-api-providers-compared/)
- [OpenRouter GLM 5.2 (EU routing prices)](https://openrouter.ai/z-ai/glm-5.2)
- [Mistral Devstral 2 announcement](https://mistral.ai/news/devstral-2-vibe-cli/)
- [Open-weight coding models (Kilo)](https://kilo.ai/open-source-models)
- [Claude Code compatible models (community guide)](https://github.com/Alorse/cc-compatible-models)
- [LiteLLM: Claude Code with non-Anthropic models](https://docs.litellm.ai/docs/tutorials/claude_non_anthropic_models)
