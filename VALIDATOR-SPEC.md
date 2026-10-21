# Validator Spec — how the factory proves "done"

The validator decides whether a slice may become a PR, and whether a task may become `done`. In both your projects the worst defects shipped **with green tests**: SoloAdventurer's SOS, block and report features wrote to tables that didn't exist, and the suites passed because they mock Supabase. JobFinderOS had a migration from an unmerged branch strand production. So the rule is:

> **A claim of "done" needs evidence from the thing users touch — never only from the code that describes it.**

Companion docs: [TASK-CONTRACT.md](TASK-CONTRACT.md) (what a contract is), [DESIGN-LOCAL-FACTORY.md](DESIGN-LOCAL-FACTORY.md) (where the validator sits).

---

## The five layers

Each layer catches what the others can't. L0–L3 run before a PR opens; L4 runs after deploy. Any FAIL stops the line for that slice.

| Layer | Runs where | Has secrets? | Catches |
|---|---|---|---|
| **L0 Deterministic gates** | Sandbox (clean clone) | No | Broken build/types/tests; regressions vs baseline; forbidden diffs |
| **L1 Contract proof** | Sandbox | No | Tests that don't actually test the change ("would it fail without it?") |
| **L2 Adversarial review** | Fresh agent session, read-only tools | No | Scope creep, wrong approach, edge cases, mock-only evidence |
| **L3 Holdout scenarios** | Orchestrator, local/preview build | Test creds only | Agent optimising for the visible tests instead of the intent |
| **L4 Live verification** | Orchestrator, after deploy | Yes (keychain) | "Merged ≠ live"; drift between repo and production |

### L0 — Deterministic gates (no model involved)

Run from `project.yaml` on a **fresh clone of the slice's branch**, not the worker's own checkout:

1. `commands.setup`, then `lint`, `typecheck`, `test`, `build`, and every `extra` command.
2. **Baseline comparison.** Distinguish what the diff broke from what was already red. Reuse SoloAdventurer's `verifier.md` rules verbatim: known errors are exact `file:line` entries from the baseline file, never whole files; test count must not drop without a stated, verified reason.
3. **Diff policy** (computed from `git diff <base>...<branch>`):
   - A file matching `paths.protected` is modified or deleted → FAIL, escalation `protected_path_touched`. This includes the test files named in the approved contract (locked at approval).
   - A file matching `paths.risk.<flag>` → add the flag to the task; it now needs a human merge, escalation `risk_path_touched` if the flag wasn't declared in the contract.
   - Any `dependency_manifests` file changes → escalation `new_dependency`. Then check the package exists, is older than 30 days and isn't a near-miss of a popular name (hallucinated packages run ~5% for commercial models and ~22% for open ones).
   - Test files deleted, `skip`/`xfail`/`.only` added, or assertions removed → FAIL.
4. **Sentinels (F-25)** — standing invariants over every diff, each FAILing with the sentinel's name and leaving a result (pass/fail/skipped) on the L0 report, the attempt record and the PR body:
   - Built-ins: a credential-shaped string added (`credential_added` — key, token and private-key patterns; the finding names the shape, never echoes the line, and it ESCALATES: nothing is pushed before a passing round, but a passing retry would push the branch whose local history still holds the credential commit — rotate the credential (the provider saw it) and re-queue; the next run builds a fresh branch); a skip, `xfail` or `only` marker added on an existing test (`skip_marker_added`); an assertion removed without a replacement (`assertion_removed`); a lint suppression (`# noqa`, `type: ignore`, lint-disable) added on a diff-changed line in a risk path (`lint_suppression_added`). The skip-marker, assertion and lint-suppression findings echo their line through `redact_credentials`, so a credential on that line prints as `[redacted]`.
   - Project sentinels: `sentinels.<risk flag>` commands in `project.yaml` run (through the sandboxed gate runner) only when the diff touches that flag's `paths.risk` globs — and only after `commands.setup` succeeds, because a sentinel may need the environment setup installs (setup failure records them skipped); a failure FAILs and escalates `sentinel_failed`, with the command output redacted through the credential patterns before it reaches any finding, record or PR body. Untouched flags record their sentinels as skipped; a flag with no `paths.risk` globs is refused at profile load.
5. **Project-specific static checks:**
   - JobFinderOS: single Alembic head; `check_dev_lock.py`; the prompt-version pins in `TestTailorPromptCraft`/calibration tests unchanged unless the contract says so.
   - SoloAdventurer: generated-types `tsc` (catches phantom tables *and* columns); `check-schema-refs.py` for `.from()`/`.rpc()`/`.invoke()` targets.

### L1 — Contract proof ("red-first", mechanised)

Both projects already require "a test that would fail without the change". The validator checks it instead of trusting it:

1. Check out the **base commit**, apply **only** the test files from the diff, run the tests named in the contract's `check.type: test` items → **each must FAIL**.
2. Apply the full diff → each must PASS.
3. Every `acceptance` item must map to evidence: a passing test, a command's exit code, or (for `holdout`/`prod_probe`) a scheduled later check. An item with no evidence → FAIL with `contract_ambiguous` if the item can't be checked as written.

A test that passes on the base commit proves nothing, and this is where false greens get through.

**What L1 can't prove.** Red-then-green shows that the test depends on the change. It doesn't show that the test checks behaviour. Two cases pass L1 and still prove nothing:

- **Red for the wrong reason.** The test is red on base only because the symbol it imports doesn't exist yet (collection or import error), so the run never reached an assertion. This is how red-first legitimately looks for a brand-new function, so L1 can't reject it. It does mean the red phase proved only that the name was missing.
- **Tautological or implementation-coupled assertions.** The expected value recomputes what the code computes (`assert add(a, b) == a + b`), or the test asserts on mocked internal collaborators or call counts instead of the result at a public interface. It goes red on base (the code is missing), green on head, and would stay green under a wrong implementation that mirrors the same logic.

L1 therefore records **how** each ref went red (`assertion` vs `collection`) in its report. Judging assertion quality is L2's job (the test-quality checks below), and holdouts (L3) back it up with checks the agent never sees.

### L2 — Adversarial review (the only model-judged layer)

- **Fresh session**, read-only tools (Read, Grep, Glob, Bash for running checks only). Reuse SoloAdventurer's `verifier.md` as the base prompt: *default verdict FAIL; evidence flips it, claims do not.*
- **Input:** the approved contract, the diff, L0/L1 output (including each ref's red reason). **Not** the implementer's narrative. The reviewer judges what was built against what was asked.
- **Two axes, reviewed separately** (adapted from mattpocock/skills `code-review`). A change can pass one and fail the other, so neither is allowed to mask the other:
  - **Spec:** does the diff implement the contract? It reports acceptance items that are missing or partial, behaviour nobody asked for (scope creep, checked against the contract's `out_of_scope`), and items that look implemented but wrongly. Each finding quotes the acceptance item it concerns.
  - **Standards:** does the diff follow the project's documented standards (`project.yaml` `standards`)? A fixed baseline of named code smells (Fowler, *Refactoring* ch. 3: Mysterious Name, Duplicated Code, Feature Envy, Data Clumps, Primitive Obsession, Repeated Switches, Shotgun Surgery, Divergent Change, Speculative Generality, Message Chains, Middle Man, Refused Bequest) always applies. A documented project standard overrides a smell. A smell is always a judgement call, never a hard violation, so it never blocks on its own. Anything L0 tooling already enforces is skipped. A project with no `standards` runs the baseline only, and the report says so.
  - Standards live with the reviewer, not the implementer: the implementer has the most context pressure, and the reviewer receives a diff and has the least.
- **Mandatory checklist** (from your own incident history):
  - Does any new backend call (table, RPC, edge function, column) exist in the **real schema**, or only in a mock? A test that mocks the client is not evidence the backend exists.
  - Does the change touch only the slice's scope?
  - Loading, error, empty and offline states — and does offline UI report *unknown*, not a false "disabled"?
  - New provider/subscription/cache: what invalidates it, who cancels it, what happens on logout?
  - **Test quality** (the part L1 can't prove). Every contract test, and especially any ref L1 reports as red only by `collection`:
    - Does its expected value come from an independent source (a literal, a worked example, the acceptance text), or does it recompute what the code does?
    - Does it assert through the public interface, or on mocked internal collaborators, private methods or call counts? Mocks belong only at system boundaries (external APIs, time, randomness).
- **Model:** a different model from the implementer where possible. With GLM-only, the self-preference bias is real, so L0, L1 and L3 — none judged by the model — carry most of the weight. Add a cross-provider reviewer when budget allows (see design doc §7).
- Output: JSON `{verdict, findings: [{id, axis, severity, file, line, text}]}`, with `axis` being `spec` or `standards`, stored as the `validator_report`. A finding with severity `blocking` on either axis means FAIL. Findings are not re-ranked across axes.

### L3 — Holdout scenarios (agents never see them)

- Stored at `holdouts.dir`, **outside the repo**. The agent sandbox denies reads there. The orchestrator runs them against a local build (JobFinderOS) or a Vercel preview (SoloAdventurer).
- Start with 3–5 end-to-end scenarios per project covering what matters most:
  - JobFinderOS: signup → first hunt completes; a generated draft has zero fabrication-guard flags; landing renders in `/sv`.
  - SoloAdventurer: sign in; block a user and see the row in `blocks`; report a message and see the row in `reports`; join the waitlist.
- Scenarios check **outcomes in the real store**, not UI text. They're the direct guard against the phantom-backend class.
- When a holdout catches something, write a regular test for it *as well*. Holdouts stay secret; the regression test doesn't.

### L4 — Live verification (after deploy)

- Runs `live_checks` from `project.yaml`, chosen by `when`: `every_deploy` always; `risk:<flag>` when the task carries that flag.
- **Merged and deployed are separate facts.** JobFinderOS doesn't deploy on merge, so the orchestrator records `merged` and waits for a deploy before L4. SoloAdventurer deploys on merge, so L4 runs right after the Vercel deployment is ready.
- L4 FAIL → the orchestrator opens a revert PR, sets `needs_human` with `live_check_failed`, and stops the queue for that repo until you look.
- **A task reaches `done` only after L4 passes.** This encodes your standing rule: "merged" ≠ "live" — verify every backend claim against prod.

---

## Rounds and escalation

```
attempt 1: implement → L0 → L1 → L2
  FAIL → findings back to the implementer (same session context, numbered findings only)
attempt 2: fix → L0 → L1 → L2
  FAIL → one more round
attempt 3: fix → L0 → L1 → L2
  FAIL → needs_human (attempts_exhausted). Never retried automatically.
PASS → L3 → PR opened → you merge → deploy → L4
```

Not every round shares a session: an aborted run re-enters the queue, and a PR you reject comes back as a fix round, maybe days later. A **fresh** session carries no context, so the orchestrator keeps a per-task **progress log**, an append-only record of each attempt's report and findings (from Anthropic's long-running-harness `progress.txt` pattern). The log lives outside the clone, and every new session gets a read-only copy. The agent never edits earlier entries.

`max_attempts` defaults to 3, matching your existing `loop.md`. Risk, protected-path, new-dependency and cross-repo hits escalate **immediately**: no retries, since retrying won't make them safe.

## Validating the validator

A validator nobody has tested is just another claim. Monthly, per project:

1. Branch from `main` and seed 5–10 known bugs: a phantom table name, a removed RLS check, an off-by-one, a deleted `await`, a test that mocks the thing it claims to test.
2. Run the validator on each.
3. Record the **catch rate per layer** in the factory metrics. A layer that catches nothing gets fixed or dropped.

This is Meta's mutation-testing approach (ACH) applied to the factory's own gates.

## What's deliberately not here (v0)

- Mutation testing of every PR: too slow and costly on a coding-plan quota. Monthly seeded bugs instead.
- Computer-use UI testing: L3 Cypress/pytest flows cover the critical paths first.
- Auto-merge: no task auto-merges in v0. It's earned per task category once catch rate and revert rate are measured.
