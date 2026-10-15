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
4. **Project-specific static checks:**
   - JobFinderOS: single Alembic head; `check_dev_lock.py`; the prompt-version pins in `TestTailorPromptCraft`/calibration tests unchanged unless the contract says so.
   - SoloAdventurer: generated-types `tsc` (catches phantom tables *and* columns); `check-schema-refs.py` for `.from()`/`.rpc()`/`.invoke()` targets.

### L1 — Contract proof ("red-first", mechanised)

Both projects already require "a test that would fail without the change". The validator checks it instead of trusting it:

1. Check out the **base commit**, apply **only** the test files from the diff, run the tests named in the contract's `check.type: test` items → **each must FAIL**.
2. Apply the full diff → each must PASS.
3. Every `acceptance` item must map to evidence: a passing test, a command's exit code, or (for `holdout`/`prod_probe`) a scheduled later check. An item with no evidence → FAIL with `contract_ambiguous` if the item can't be checked as written.

A test that passes on the base commit proves nothing, and this is where false greens get through.

### L2 — Adversarial review (the only model-judged layer)

- **Fresh session**, read-only tools (Read, Grep, Glob, Bash for running checks only). Reuse SoloAdventurer's `verifier.md` as the base prompt: *default verdict FAIL; evidence flips it, claims do not.*
- **Input:** the approved contract, the diff, L0/L1 output. **Not** the implementer's narrative. The reviewer judges what was built against what was asked.
- **Mandatory checklist** (from your own incident history):
  - Does any new backend call (table, RPC, edge function, column) exist in the **real schema**, or only in a mock? A test that mocks the client is not evidence the backend exists.
  - Does the change touch only the slice's scope?
  - Loading, error, empty and offline states — and does offline UI report *unknown*, not a false "disabled"?
  - New provider/subscription/cache: what invalidates it, who cancels it, what happens on logout?
- **Model:** a different model from the implementer where possible. With GLM-only, the self-preference bias is real, so L0, L1 and L3 — none judged by the model — carry most of the weight. Add a cross-provider reviewer when budget allows (see design doc §7).
- Output: JSON `{verdict, findings: [{id, severity, file, line, text}]}`, stored as the `validator_report`.

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
