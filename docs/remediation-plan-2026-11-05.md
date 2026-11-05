# Remediation plan — CODE-REVIEW-2026-11-05

**Status:** F-43 to F-55 are drafted in `BUILD-ORDER.md` (Phase 1r) and `.factory/tasks.json`, all
`draft`. **`BUILD-ORDER.md` is the canonical spec**; where wording differs, it wins. This doc keeps
the rationale and the finding-to-WO map.

**Goal:** close every finding in `CODE-REVIEW-2026-11-05.md`: 1 critical, 14 major, 23 minor,
19 info, 8 architecture. Every code finding closes with a red-first test that fails on today's
code. Every doc finding closes with a mechanical check where one is possible. Four items are
decisions, not fixes. They're marked **DECIDE** and have a recommendation.

**Ids:** new WOs start at F-43. F-36 stays unused. The untracked
`docs/proposals-2026-10-30-borrowings.md` reuses F-40/F-41, and F-52 resolves that. F-56 and F-57 come from the F-13 import run, not the review (§1b).

---

## 1. Work orders

Build mode follows BUILD-ORDER's rule. Anything that changes what judges agents
(`factory_control`) is built **interactively**, one WO per session. **Factory-built** marks a WO
the factory may build for itself once Tier 1 has landed. Before queuing one, check that it
touches no `paths.protected` entry.

### Tier 1: before any further real run on an outside repo (gates F-13 steps 4–5)

**F-43 — Egress redaction seam** · factory_control, privacy · M · interactive
Everything that leaves a session passes one function, `scrub_session_text(text)`. That
function runs `scrub_literal(seat_token)`, then `redact_credentials`, then strips terminal
control sequences.
- AC-1 (C-1) The implementer notes, every L2 finding text and the L2 verdict JSON pass the seam before `build_pr_body`. A test plants the literal seat token, plus a bare `ANTHROPIC_AUTH_TOKEN=<value>` echo, in each input and asserts that neither reaches the rendered body.
- AC-2 (C-1) `check_pr_text` refuses a title or body that contains the literal seat token. This is a second, independent fence before the push.
- AC-3 (M-8) Vent text and `sessions/unparsable-record-*.stdout.txt` dumps pass the same seam. The test plants the token in each.
- AC-4 (m-16) Vent text, `factory status` output and trace lines printed to a terminal have ANSI/OSC/C0 control sequences stripped. The test covers screen-clear, OSC-8 link and cursor-move sequences.
- AC-5 (I-10) `redact_credentials` and the credential sentinel catch JWTs, `Bearer <token>` literals and unquoted YAML `password:` values. Each pattern gets an attack diff and an F-33 benign-corpus pass.
- AC-6 A single test enumerates every orchestrator path that writes session-derived text off the machine or to the owner's terminal, and asserts each one calls the seam. New channels can't silently skip it.

**F-44 — Verdict integrity** · factory_control · M · interactive
- AC-1 (M-1) Severity is canonicalized once at parse (`strip().lower()`, next to the axis canonicalization). A test merges `"Blocking"` with `"minor"` in both orders and asserts blocking wins and the verdict is fail. `eval._count_blocking` is case-insensitive too.
- AC-2 (m-10) `_extract_json` refuses output that carries more than one valid verdict object with different verdicts, as an `_InvalidVerdict` (retry path). The test uses FAIL-then-example-PASS and the reverse.
- AC-3 (m-8) `parse_test_count` takes the count from the runner's final summary line, not the maximum anywhere in the output. The test has a test that prints `999999 passed`.
- AC-4 (I-11) Removing `pytest.raises` counts as an assertion removal in `ASSERTION_PATTERN`. Attack and benign corpus entries are added.

**F-45 — Pre-lease, publish and crash-window robustness** · factory_control · M · interactive
- AC-1 (M-5) The stale-PR sweep runs under `queue.idle_lease()`. The test races a sweep against `reclaim_runs` and asserts no live run dir is deleted.
- AC-2 (M-6) The `_sweep_escalation` log append is best-effort (logged, not raised) or ordered after the transition. The test injects a failing append and asserts the task reaches `needs_human` and the next run proceeds.
- AC-3 (M-7) The publish-side `git push`, `ls-remote` and `gh pr create` calls run with `GIT_TERMINAL_PROMPT=0` and a timeout, through the same wrapper as `_fetch_sha`. The test uses a hanging fake remote and asserts a timed-out `PublishError`.
- AC-4 (m-13) A `finish()` partial failure appends exactly one attempt. The test injects a failure between `append_attempt` and `transition`.
- AC-5 (m-14) `gh pr view` returning `null` or other non-object JSON maps to the documented unresolvable state.
- AC-6 (I-3) Atomic writes fsync the parent directory after rename.
- AC-7 (I-4) The run-lease error names the holder only when that holder is alive; otherwise it says the lock is stale. The repo's stale `run.lock` content is cleared.

**F-46 — Gate evasion hardening (L0/L1)** · factory_control · M · interactive
- AC-1 (M-2) The attribution scan reads each commit separately (`git log --format=%B -z` per sha, or `rev-list` then `cat-file`), not by splitting on `\x1e`. A commit body with an embedded `\x1e` plus a trailer is flagged.
- AC-2 (m-17) Attribution matching runs on NFKC-normalized text with zero-width characters removed, and allows any number of connecting tokens. Tests cover U+2010, fullwidth colon and a zero-width space inside "claude".
- AC-3 (m-9) `lint_suppression_added` checks both the old and new path of a rename. The test renames `payments/x.py` to `utils/x.py` and adds `# noqa`.
- AC-4 (M-4) Conftest, `pytest.ini`, `pyproject [tool.pytest]` and `setup.cfg [tool:pytest]` are protected by a schema-level default, not by each profile's own list. A profile can only widen the default. All three profiles validate.
- AC-5 (M-4) L1 treats a contract test that wasn't collected (exit 5, or missing from the collection list) as a failure at **both** red and green, not as red. The test uses a conftest that sets `collect_ignore` and asserts L1 fails.

**F-47 — Sandbox read boundary and toolkit staging** · factory_control, privacy · S · interactive
- AC-1 (M-9) `denyRead` covers the factory root itself, with explicit carve-outs for the run's own dir and srt. A test with `--factory-root` outside `$HOME` asserts a session can't read `trace/`, `progress/` or `sessions/`.
- AC-2 (I-9) When a drain force-closes a `setsid` escapee, the event goes to the trace and to `factory status`, not only into a docstring.
- AC-3 (I-2) The index-load MCP domain check validates against the vetted allowlist, not against itself. The test uses an index entry with an unvetted domain.
- AC-4 (I-19) MCP descriptor staging copies its files explicitly. The test asserts the staged set without relying on copytree side effects.

**Owner step (new, added to F-13 before step 4) — client data processing** · owner
- Decide in writing whether client source may go to the Z.ai endpoint. For EU clients that means a DPA or written client consent, plus a record of which provider processes which repo. Until that's settled, F-13/F-17 run only on repos you own.

### Tier 2: right after Tier 1, in parallel with F-13 steps 4–5

**F-48 — Eval parity and variance** · factory_control · M · interactive · depends F-44
- AC-1 (M-3) The eval's L2 path runs `apply_convergence` exactly as production does. A shared function is called from both, and a test asserts the eval and the orchestrator call the same symbol.
- AC-2 (m-12) The eval checks quota before parsing, as production does. The test asserts zero re-runs for a quota-hit seat.
- AC-3 (m-11) The clean-diff report line shows L0 and L1 as well as L2. A clean diff that failed L0 renders as an L0 false positive.
- AC-4 (I-12) `factory eval --repeats N` runs each seed N times and reports the mean and spread per metric. **DECIDE:** the default N and how often it runs (cost). Recommendation: N=3 monthly, N=1 on demand.
- AC-5 Re-run F-16's seeded eval on the fixed pipeline and record the new baseline in the ledger. The old numbers were measured without convergence.

**F-49 — Contract and importer hardening** · factory_control · M · interactive
- AC-1 (M-10) A CI test resolves every acceptance ref on every non-draft task to a real test function, using F-01's AST resolution. A typo'd ref on a `ready` task fails CI.
- AC-2 (m-3) Schema validation enforces `format: date-time` (`FormatChecker`).
- AC-3 (m-4) The planning-doc listing over 200 files ends with `+N more`, and the proposal names the truncated docs.
- AC-4 (m-6) The fresh-queue import write holds the queue lock across check and write.
- AC-5 (m-7) A proposal that raises `RecursionError` takes the keep-refused-output path; the session output is saved.
- AC-6 (I-18) A proposal without a `project` key is refused.

**F-50 — CLI and dashboard robustness** · — · M · factory-built candidate
- AC-1 (m-1) The dashboard `/metrics` expands `~` in the repo path. The test uses a `~`-relative profile.
- AC-2 (m-2) `factory status` survives a non-UTF-8 commit subject (`errors="replace"`).
- AC-3 (m-5) An explicit `--config <path>` that doesn't exist is an error; only the default path may be absent.
- AC-4 (m-15) `factory sentinels replay` exits non-zero on any benign hit or missed attack.
- AC-5 (I-5) A trace line that parses to a non-dict renders as an error row, not a 500.
- AC-6 (I-6) A `rendered_path` OSError in `_run_eval` exits 2 with a message.
- AC-7 (I-7, I-8) The stale `cli.py` module docstring is fixed and the dead HEAD suppression at `dashboard.py:573` removed.

**F-51 — Test suite and CI hygiene** · factory_control (touches tests/CI) · M · interactive
- AC-1 (M-11) The importer prompt test checks for no *bare* `python3` outside the quoted interpreter path. It passes under conda, pyenv and system Python. The full local suite is green.
- AC-2 (m-20) Live-endpoint assertions (`api.z.ai` reachability, the 1.1.1.1 raw-socket deny) move to a separately named CI job, so an outage reddens that job, not every PR. The sandbox-deny assertion stays mandatory.
- AC-3 (m-21) Live session tests run only with an explicit opt-in (`FACTORY_LIVE_TESTS=1` or `-m live`), never inferred from a credential being present.
- AC-4 (m-22) `pytest` and `ruff` get exact pins; CI's Node version meets srt's `>=22.12.0` floor.
- AC-5 (m-23) `re.split(..., maxsplit=1)`; the suite runs with `-W error::DeprecationWarning`.
- AC-6 (I-16) The test_f05 fixed sleeps become condition waits with a timeout.
- AC-7 (I-17) The cross-test-module import moves into a shared `tests/` helper.
- AC-8 (I-1) A wheel install works (schema/toolkit shipped as package data and read with `importlib.resources`), proven by a CI step that installs the built wheel and runs `factory --help` plus contract loading.
- AC-9 (A-7) `pyright` in basic mode runs in CI and is clean.

**F-52 — Docs truth pass** · — · S · factory-built candidate (check protected docs)
- AC-1 (M-12) **DECIDE:** recommendation is to delete the queue-marker regeneration claim from `TASK-CONTRACT.md` (lines 13 and 144). `tasks.json` plus `factory status` is the one source; a second rendered table is the duplicated truth §7 warns about.
- AC-2 (M-13) VALIDATOR-SPEC's done rule names the no-deploy carve-out, and marks L3/L4 as not yet built until F-14/F-15 land.
- AC-3 (M-14) The borrowings proposals doc is deleted, or re-baselined with ids from F-58 up.
- AC-4 (m-18) README: "never skipped" names the INC-007 exception, "every layer" becomes L0–L2, the stale WO range is fixed, and BOOTSTRAP.md's "frozen" header is corrected.
- AC-5 (m-19) VALIDATOR-SPEC names `prompts/reviewer.md` as the L2 base, not `verifier.md`.
- AC-6 (I-13, I-14, I-15) BUILD-ORDER's build-order narrative is brought current; F-13 step 2 names the right file; DESIGN's vendored-skills list matches what's vendored.
- AC-7 (A-8) **DECIDE:** a retention rule for "round-N finding M" comments. Recommendation: keep them, but each one must cite a PR number so it stays resolvable; a lint enforces this.
- AC-8 A cross-doc claims test asserts, at minimum, that every CLI command and file path named in README/VALIDATOR-SPEC/TASK-CONTRACT exists. That puts the repo's "mechanical rules are tests" rule onto the docs.

### Tier 3: structure, before L3/L4 (F-14, F-15)

**F-53 — Split `_run_task` into phase functions** · factory_control · M · interactive · depends Tier 1
Pure extraction: no behavior change. The schema forbids size L, so the old F-53 is sliced into F-53/F-54.
- AC-1 (A-1) `_run_task` becomes phase functions over `_Ctx` (prepare, implement, gates, review, publish). No function in `orchestrator.py` is over 150 lines; a test enforces this.
- AC-2 (A-1) The existing suite passes with no test file changed in the WO's diff.

**F-54 — Validation-pipeline seam** · factory_control · M · interactive · depends F-53, F-48
- AC-1 (A-2) The per-round gate sequence is a data list of `(name, callable)`. A test registers a dummy layer and sees it run, record and render in the PR body without editing the orchestrator.
- AC-2 (A-6) `eval.py` imports only public orchestrator functions. A test asserts no `orchestrator._*` imports in `eval.py`.

**F-55 — Module hygiene** · factory_control · M · interactive · depends F-54
- AC-1 (A-3) Venv creation, the prompt builders and `reclaim_runs` move out of `orchestrator.py`; `now_iso()` moves to `runs.py`; `importer.py` stops importing `_atomic_write_json` privately.
- AC-2 (A-4) The `contract ⇄ toolkit` cycle is broken, and an import-graph test asserts the layering is acyclic.
- AC-3 (A-5) One git wrapper. A test asserts no direct `subprocess` git call outside it.

---

### 1b. From the F-13 import run (not review findings)

Both are drafted in `BUILD-ORDER.md` (Phase 1r) and `.factory/tasks.json`; BUILD-ORDER is canonical.

**F-56 — Session record transport** · factory_control · M · interactive · **Tier 1** (gates F-13 step 5)
Three of four real importer runs on 2026-11-05 lost the session record to a truncated stdout line; the fourth line was exactly 81,920 bytes, cut inside `permission_denials`. Any seat on a real run can hit it. The record moves to a file, failures name the srt exit code and a scrubbed stderr tail, `permission_denials` is bounded, the truncation is reproduced or bounded, and the importer salvages on this path too.

**F-57 — Importer robustness: per-task tool, repair turn, prose eval** · factory_control · M · interactive · Tier 2 · depends F-49, F-56
A `propose_task` tool validates each task as it is written, one repair session replaces a full re-run, and a prose-docs fixture plus `factory eval --importer` measure the importer on a repo whose docs aren't contract-shaped.

## 2. Placement in the build order

```
now ──► F-43 ─► F-44 ─► F-45 ─► F-46 ─► F-47 ─► F-56 (Tier 1, sequential: each touches the judge)
                                   │
        owner: client data step ───┤
                                   ▼
        F-13 steps 4–5 (first real outside run)   ║  F-48 → F-49 → F-51 → F-57 (Tier 2, interactive)
                                                   ║  F-50, F-52           (factory-built candidates)
                                   ▼
        F-53 ─► F-54 ─► F-55                       (Tier 3)
                                   ▼
        F-14 (L4) · F-15 (L3)  ─►  F-17 (second outside project)
```

- F-13 gets F-43 to F-47 and F-56 added to its dependencies. That's a contract change, so it gets re-approved through `factory approve`. F-13 is still `draft`, so nothing in progress breaks.
- F-14 and F-15 get F-54 (the pipeline seam) as a dependency, so L3/L4 land on the seam instead of in a 1,136-line function.
- F-16's baseline is re-measured in F-48 AC-5.
- Tier 1 order: redaction first (the only finding that leaks off the machine), then verdicts (the product's promise), then the queue wedge, then evasion, then the read boundary.

## 3. Finding-to-WO map (every finding, exactly once)

| Finding | WO | Finding | WO | Finding | WO |
|---|---|---|---|---|---|
| C-1 | F-43 | m-1 | F-50 | I-1 | F-51 |
| M-1 | F-44 | m-2 | F-50 | I-2 | F-47 |
| M-2 | F-46 | m-3 | F-49 | I-3 | F-45 |
| M-3 | F-48 | m-4 | F-49 | I-4 | F-45 |
| M-4 | F-46 | m-5 | F-50 | I-5 | F-50 |
| M-5 | F-45 | m-6 | F-49 | I-6 | F-50 |
| M-6 | F-45 | m-7 | F-49 | I-7 | F-50 |
| M-7 | F-45 | m-8 | F-44 | I-8 | F-50 |
| M-8 | F-43 | m-9 | F-46 | I-9 | F-47 |
| M-9 | F-47 | m-10 | F-44 | I-10 | F-43 |
| M-10 | F-49 | m-11 | F-48 | I-11 | F-44 |
| M-11 | F-51 | m-12 | F-48 | I-12 | F-48 |
| M-12 | F-52 | m-13 | F-45 | I-13 | F-52 |
| M-13 | F-52 | m-14 | F-45 | I-14 | F-52 |
| M-14 | F-52 | m-15 | F-50 | I-15 | F-52 |
| A-1 | F-53 | m-16 | F-43 | I-16 | F-51 |
| A-2 | F-54 | m-17 | F-46 | I-17 | F-51 |
| A-3 | F-55 | m-18 | F-52 | I-18 | F-49 |
| A-4 | F-55 | m-19 | F-52 | I-19 | F-47 |
| A-5 | F-55 | m-20 | F-51 | A-7 | F-51 |
| A-6 | F-54 | m-21 | F-51 | A-8 | F-52 |
| | | m-22 | F-51 | | |
| | | m-23 | F-51 | | |

**Info ids** (the report doesn't number them; split here so none hides inside a compound bullet):
I-1 wheel installs · I-2 MCP domain tautology · I-3 directory fsync · I-4 dead lease holder ·
I-5 dashboard non-dict trace · I-6 `_run_eval` OSError · I-7 stale cli docstring · I-8 dead HEAD
suppression · I-9 setsid escapee alarm · I-10 JWT/Bearer/YAML-password shapes · I-11
`pytest.raises` removal · I-12 eval n=1 · I-13 BUILD-ORDER narrative · I-14 F-13 step 2 file ·
I-15 DESIGN vendored list · I-16 fixed sleeps · I-17 cross-test import · I-18 importer `project`
key · I-19 MCP copytree side effect.

**Architecture ids:** A-1 `_run_task` split · A-2 pipeline seam · A-3 orchestrator extractions +
`now_iso` + private import · A-4 contract⇄toolkit cycle · A-5 git wrappers · A-6 eval private
imports · A-7 pyright · A-8 comment retention rule.

## 4. Done means

- Each WO merged with its ACs as red-first tests, and the BOOTSTRAP/ledger entry carries evidence per AC.
- `ruff`, `pyright` and `pytest` are green locally on conda, pyenv and system Python, and in CI.
- A closing review, run the same way as CODE-REVIEW-2026-11-05, finds none of these ids reopened.
- Remaining gaps to "production grade" that aren't review findings: F-14 (L4), F-15 (L3), and one outside project shipped end to end (F-13 then F-17).
