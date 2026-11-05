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
   - A file matching `paths.protected` is modified or deleted → FAIL, escalation `protected_path_touched`. This includes the test files named in the approved contract (locked at approval), and — independently of the profile — any tracked path under `.venv/`: the gate runner's venv is factory infrastructure inside the clone, and a committed `.venv` is planted environment (`git add -f`), never project source; the venv creation refuses such a clone before any gate command runs.
   - A file matching `paths.risk.<flag>` → add the flag to the task; it now needs a human merge, escalation `risk_path_touched` if the flag wasn't declared in the contract.
   - A commit between the merge-base and head whose message carries an AI attribution trailer or footer → FAIL `attribution_trailer` (F-42), on every round before anything is pushed. One pattern list (`factory/attribution.py`), shared with the publish step, covers: a `Co-Authored-By`/`Assisted-by`/`Generated-by` trailer naming an unambiguous AI author (an assistant name or the factory's own seat identity), any trailer line naming a name that is also a human name (Kimi, Gemini, Cursor, …) only when an AI shape sits beside it (a bot/noreply or AI vendor address, or the product's compound name — "Cursor Agent"), any trailer naming an AI vendor address, the bare `AI-assisted:` declaration, and a `Generated with` footer (the phrase followed by an AI tool or vendor link, anywhere in a line — "generated with openapi-generator/faker/ruff" is prose and passes). The publish step refuses the same patterns in a PR's title (unanchored — a title is one line) and its body's FREE TEXT, before the branch is pushed; fenced blocks are skipped, since the implementer's notes and the validator report may legitimately quote a trailer. A squash commit builds its message from that title and the branch's commits, so it cannot inherit a trailer. A human `Co-Authored-By` passes; the branch's own history is rewritten by the implementer in its fix round, never `main`'s.
   - Any `dependency_manifests` file changes → escalation `new_dependency`, except a plain modification of a root `pyproject.toml` whose dependency surface is unchanged between merge-base and head: the WHOLE parsed document compares (`tomllib`), minus an explicit inert allowlist (project metadata — name/description/readme/version/urls/scripts — and dev-tool config tables: tool.ruff/pytest/coverage/mypy, tool.setuptools.package-data), so dependency-bearing tables outside the obvious three (PEP 735 `[dependency-groups]`, `dynamic` dependencies, `[tool.uv.sources]`, `[tool.poetry.*]`, an unknown `tool.*` table) escalate too. A parse failure on either side, any other manifest, an added/deleted/renamed pyproject, or a policy call without refs escalates — fail closed. Then check the package exists, is older than 30 days and isn't a near-miss of a popular name (hallucinated packages run ~5% for commercial models and ~22% for open ones).
   - Test files deleted, `skip`/`xfail`/`.only` added, or assertions removed → FAIL.
4. **Sentinels (F-25)** — standing invariants over every diff, each FAILing with the sentinel's name and leaving a result (pass/fail/skipped) on the L0 report, the attempt record and the PR body:
   - Built-ins: a credential-shaped string added (`credential_added` — key, token and private-key patterns; the finding names the shape, never echoes the line, and it ESCALATES: nothing is pushed before a passing round, but a passing retry would push the branch whose local history still holds the credential commit — rotate the credential (the provider saw it) and re-queue; the next run builds a fresh branch. An environment READ — `token = os.environ.get("TOKEN", "")` — is the safe pattern, not a leak: the generic assignment shape is tested with the env call's head and first argument blanked, so the lookup itself is exempt while a 16+ literal anywhere else on the line — a fallback argument, an `or` after the call, a comment's env mention — still flags; the prefix shapes scan env-read lines anyway); a skip, `xfail` or `only` marker added on an existing test (`skip_marker_added` — matched on the line with quoted spans blanked: every statement shape fires — decorators, module-level `pytestmark`, one-line conditionals, `pytest.param` marks — while marker text inside a string fixture stays inert); an assertion removed without a replacement (`assertion_removed` — replacement is per-file arithmetic: verbatim re-adds pair up, a file that ends with as many assertion lines as it started is quiet, only a net loss flags; a replacement must be a real assertion statement — a tautology or a string mentioning "assert" levels nothing); a lint suppression (`# noqa`, `type: ignore`, lint-disable) added on a diff-changed line in a risk path (`lint_suppression_added` — and only where that syntax is a comment in the file's language: `# noqa` inside JSON or markdown prose cannot suppress anything); a runner-forgery enabler added in a test file or conftest (`runner_exit_forgery`, F-44 round 2 — a hard exit/kill, an output-capture escape, or a runner session hook defined in a TEST file (in a conftest the hook definition alone is the registered plugin surface and stays quiet; there the exit/capture patterns carry the load): test code runs inside the runner's process and can print a forged result line then exit before the real one, which no stdout parser can distinguish; the line scan flags the enablers, self-kill `os.kill(os.getpid(), …)` excepted as the honest crash-simulation shape, and a human decides). The skip-marker, assertion, runner-forgery and lint-suppression findings echo their line through `redact_credentials`, so a credential on that line prints as `[redacted]`.
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
- Output: JSON `{verdict, findings: [{id, axis, severity, file, line, text}]}`, with `axis` being `spec` or `standards`, stored as the `validator_report`. A finding with severity `blocking` or `major` on either axis means FAIL (F-41: a surviving major weighs exactly as much as a blocking finding, and a declared pass overrides neither). Findings are not re-ranked across axes.

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

Not every round shares a session: an aborted run re-enters the queue, and a PR you reject comes back as a fix round, maybe days later. A **fresh** session carries no context, so the orchestrator keeps a per-task **progress log**, an append-only record of each attempt's report and findings (from Anthropic's long-running-harness `progress.txt` pattern). The log lives outside the clone, and every new session gets a read-only copy. The agent never edits earlier entries. The log also carries **vent** entries (F-27): every `vent(category, text)` call a session's in-process tool collected is flushed in — one entry per call, the moment the session returns — as owner telemetry for `factory status` (F-11) and the F-19 review; vents never render into fresh-session prompts and never touch the attempt record or the budget. Every run is also **traced live** (F-30): one JSONL event log per run at `<factory_root>/trace/<project>/<run-id>.jsonl`, outside every clone and outside the sandbox's write allowlist — phase starts/ends, each tool call as it happens (the session's stdout carries the events out; the trace path itself is not writable from inside), one gate_check per L0 check, L1 red/green proof and L2 finding with the evidence it looked at, per-session spend, and the attempt's terminal marker. Args and result snippets are capped, the seat token and credential-shaped strings (the L0 sentinel patterns) are `[redacted]` before a line is written — at every boundary, so the writer itself also scrubs the seat token from orchestrator-written error text — and a failing trace write is reported once and changes nothing: a run's outcome never depends on its own observability. The pipe out of the sandbox is untrusted: only `tool_call` events cross it, and the writer's stamps (seq, ts, run, task, attempt) merge after the event, so nothing inside a sandbox can forge a terminal marker or re-stamp history. Trace files are owner-only (0600 under a 0700 dir) — they hold capped snippets of everything a run read and wrote, which on PII-bearing projects is personal data — and `reclaim_runs` prunes them after a retention window (14 days). `factory trace <run-id> [--follow]` prints the whole file in order, read-only; `--follow` keeps printing until the run ends (published, failed or aborted) or can no longer write a marker — a killed run, a trace pruned under the reader — and says why it stopped. A session's toolkit (F-22) is part of the same trust story: a project may give its seats vetted skills, plugins and read-only MCP servers (`toolkit:` in project.yaml, admitted items only, per-seat narrowing for the reviewer); they load from a staged read-only copy outside the clone while `setting_sources` stays empty — nothing reaches a session from the owner's personal Claude setup — and every item is licence-gated, content-hashed on load (a tampered vendored file refuses the index), and admitted only by an owner-approved PR carrying the vetting report (`factory toolkit vet` reads a candidate and never admits). Each attempt records what it loaded as `name@content-hash`, so a later failure can be attributed to a toolkit version.

`max_attempts` defaults to 3, matching your existing `loop.md`. Risk, protected-path, new-dependency and cross-repo hits escalate **immediately**: no retries, since retrying won't make them safe.

Merge order (F-26) guards the same invariant one level up: a PR's validation only holds on the base it was built on, so before `factory run` reports an open factory PR ready, it checks ordering. Admission: a repo whose default branch doesn't require status checks with `strict` (branches up to date) refuses to run at all — except a plan-limited repo (GitHub's 403) that declares `repo.branch_protection: unavailable`, where the factory enforces the equivalent itself. Ordering inside the run: a drained queue inside the peak window returns idle whatever the clock says; the window returns before any gate work or gh lookup; off-peak, every run — with or without pickable work — resolves the GitHub state of each open PR **once** (idle ticks touch only `in_review` links plus the recovery-eligible `needs_human` ones, a set computed once and handed to the sweep; a failed lookup is presumed open, so the open-PR limit fails closed while the run itself never crashes on one rotten link) and first applies the recovery edges from that GitHub truth — a re-opened **or merged** escalated PR returns its task to `in_review` (a merged one waits in in_review for the F-14 deploy gate like every merged factory PR, and `links.merged` stamps that PR terminal by URL — never gh-polled, counted, or re-validated again, while a later PR on the same task is not covered; nothing in F-26 advances a merged task on a deploy-target profile — a no-deploy profile (`deploy.mechanism` none-form, empty `live_checks`) closes it `in_review → deploying → done`/`awaiting_owner` in the same run, the merge the evidence), and a closed `stale_base` PR was the owner's rejection: the task goes straight to `ready` for its fix round — `blocked` when a dependency is open, exactly as approval routes — with no lease-less `in_progress` hop. These edges sit outside the general transition table and are gated to a `stale_base`/`pr_closed` escalation holding a PR link — no other `needs_human` task can exit through them (the exceptions are the `credential_added` re-queue, owner-gated to `factory recover --evidence` with an interactive confirm, written only by the dedicated `TaskQueue.recover_credential` method — the generic transition refuses a credential task — and never by the sweep; and the `attempts_exhausted` budget reset, owner-gated to `factory recover --evidence … --reset-attempts` through `TaskQueue.recover_attempts`, which appends an `attempt_resets` marker — history kept, the budget counts only attempts after the latest marker — and is likewise never written by the sweep) — and their escalations never eat an attempt (session-less records; the fix round starts with its slot unspent). A pr_closed task's PR was already closed at escalation, so its fix-round word is `factory recover <task-id>`, which checks GitHub and writes the same gated edge only when the PR is closed (an open PR would hold the concurrency slot under the queued fix round forever; the check fails closed when gh can't answer). The sweep then re-validates the open PRs: `git ls-remote` (prompt-disabled, timed out) skips any PR whose cached `(branch, base)` pair in `links.revalidated` is unchanged — stamped both when a re-validation passes and when the PR is found up to date — and otherwise L0 and L1 re-run on the PR merged onto the new base (locally proving the merge a strict setting would force). A merged PR is stamped in `links.merged` (its URL) and never re-validated or re-polled; its task waits in `in_review` for the F-14 deploy gate, unless the profile has no deploy target (`deploy.mechanism` none-form, empty `live_checks`), in which case the run closes it through `deploying` to `done`/`awaiting_owner` — nothing will ever deploy or go live to verify. A PR closed without merging escalates `pr_closed` so its task is never silently stranded. A failure — merge conflict (read from the index, locale-independent; the local merge runs under `LC_ALL=C` with its own timeout, whose failure wording names the local pathology, not the network) or gates — escalates `stale_base` and the task leaves `in_review` for `needs_human`; every remote git call is prompt-disabled and timed out, and a timeout is a clean RunError, never a traceback. The loop keeps at most one open factory PR per repo (`repo.concurrency`, default 1; counted by GitHub's word — an escalated task's still-open PR holds its slot, an unknown state holds it too, and the picked task's own PR counts like any other, so a fix round waits until you actually close the rejected PR instead of opening a second one and orphaning it), and a `concurrency_wait` names the open PRs and any link whose state couldn't be resolved, so a stall never looks like a healthy tick. Every open PR the sweep proved current — up to date with, or re-validated on, the remote default head, or cached as either for exactly that head — is reported on the run's result and printed as `ready: <pr-url> against <default-branch>@<sha12>`; an unknown-state or failing PR never is.

## Validating the validator

A validator nobody has tested is just another claim. Monthly, per project:

1. Branch from `main` and seed 5–10 known bugs: a phantom table name, a removed RLS check, an off-by-one, a deleted `await`, a test that mocks the thing it claims to test.
2. Run the validator on each.
3. Record the **catch rate per layer** in the factory metrics. A layer that catches nothing gets fixed or dropped.

This is Meta's mutation-testing approach (ACH) applied to the factory's own gates.

The L0 sentinels carry their own standing eval (F-33), run by CI on every PR: a **benign corpus** of this repo's merged PRs — every built-in sentinel must pass every diff; PRs whose credential-shaped test fixtures are correct hits are excluded with the PR and reason recorded — and a fresh **attack corpus** with at least one flagged diff per built-in. `tests/test_f33.py` is the gate; `factory sentinels replay [--corpus benign|attack] [<patch> ...]` replays by hand, read-only. A sentinel change ships only if it stays silent on the benign corpus and still fires on its attacks.

## What's deliberately not here (v0)

- Mutation testing of every PR: too slow and costly on a coding-plan quota. Monthly seeded bugs instead.
- Computer-use UI testing: L3 Cypress/pytest flows cover the critical paths first.
- Auto-merge: no task auto-merges in v0. It's earned per task category once catch rate and revert rate are measured.
