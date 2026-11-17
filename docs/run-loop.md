# Run loop: run, retro, fix what broke, repeat

Owner decision, 2026-11-12: run real tasks through the factory and find problems that way. Write a short retro after each run, and fix only what broke. The safety floor stays (sandbox, push fence, credential guard, the owner merges). Everything above it has to be earned by a real run.

## Before the first run (after F-72 merges)

The `factory` command runs from the main checkout of this repo (the miniconda editable install). That checkout is at 7694097, which is behind F-67, F-71 and F-72, so it has to be updated first.

1. Update the factory:

   ```bash
   cd ~/Desktop/Software_Factory_Agents && git checkout main && git pull --ff-only && factory --help >/dev/null && echo factory-ok
   ```

2. Merge the JobFinderOS contract PR for wo21.
   - The branch is `chore/wo21-declare-deps`, commit e3df12b, prepared in `~/Desktop/jfo-f13`. It is not pushed yet.
   - It declares `pypdf` in the contract's `dependencies` (F-71) and adds `external_service` to its risk. Its scope covers `draft_service.py` and `pdf_service.py`, so without it L0 escalates risk_path_touched, as it did in run 2.
   - With F-71, a declared dependency is checked against the registry instead of escalating.

3. Bring the JobFinderOS checkout up to date.
   - `.factory/tasks.json` there holds run 2's local state: wo21 is `needs_human` with 1 attempt. `git pull` refuses while that file is modified, so discard it first.
   - wo21 then returns to `draft` with the new contract. The run history stays in `~/factory/progress` and `~/factory/reports`.

   ```bash
   cd ~/Desktop/JobFinderOS && git checkout -- .factory/tasks.json && git pull --ff-only
   ```

4. Re-approve the project profile.
   - F-71 added a `budget_floor` approval group, so a pre-F-71 profile approval is likely to be refused.
   - If nothing changed, the command says so.

   ```bash
   cd ~/Desktop/JobFinderOS && factory approve --profile
   ```

5. Approve wo21. This is interactive, and `--yes` is refused because the contract declares a dependency.
   - Optional, before the first run of any new or changed contract (F-80): `factory review-contract <task-id>` outside the peak window — one read-only session reads the contract and its source document; `factory approve` then shows its findings (blockers first) above the y/N. Advice only.

   ```bash
   cd ~/Desktop/JobFinderOS && factory approve wo21-pdf-text-layer-verification
   ```

6. Run it.
   - Run outside 08:00–12:00 on weekdays, which is the peak window.
   - Send the full output to the reviewer session.

   ```bash
   cd ~/Desktop/JobFinderOS && factory loop
   ```

## Retro after each run

This takes about 15 minutes and is not a full review. Its sources:
- the `factory loop` output;
- `factory status`;
- `~/factory/reports/<project>/<task>/`;
- `~/factory/trace`;
- the PR, if one opened.

```
### Run <date> — <task id> — <outcome>
- Turns: implementer <n>/<max> per attempt; where they went (setup, exploration, the change, fixes)
- Escalated: <class or none> — why — was it the task's fault, the contract's, or the factory's?
- Owner touches: <count> — each one named (approve, recover, merge, a manual fix)
- Cost: tokens/usage per seat (implementer, reviewers, verifier, arbiter); wall clock — `factory status <task-id>` prints each attempt's turns against the cap and the per-attempt/per-task usage and cost totals (F-83)
- What broke in the factory: <one line each, or none>
- Fix as a WO? <yes/no per item; one review round unless it's security/push/sandbox>
```

## Metrics (one row per run)

| Date | Task | Outcome | Owner touches | Implementer turns | Total tokens | Wall clock | Factory defects found |
|------|------|---------|---------------|-------------------|--------------|------------|-----------------------|
| 2026-11-11 | wo21 run 1 | needs_human attempts_exhausted | 2 | 30+30 (cap) | — (recorded null) | — | turn cap too low; clone without venv or git identity; failed sessions record no usage |
| 2026-11-11 | wo21 run 2 | needs_human new_dependency + risk_path_touched | 3 | — | ~117k | — | every manifest change escalated; contract risk did not cover scope; work lost |
| 2026-11-12 | wo21 run 3 | **pr_opened** (JFO #163), 2 attempts | 3 (profile re-approve, approve, merge) | 100 (cap) + 31 | 233k in / 53k out / 6.0M cache-read | 31.5 min | implementer committed as the owner; setup burn; sandbox missing fonts led to a skip marker |
| 2026-11-13 | wo21 run 4 (#163 live test) | needs_human stale_base (F-75 never reached) | 1 (profile re-approve after repo move) | 0 (no session) | 0 model tokens | ~3 min | one flaky test fails stale-base re-validation and parks a passing PR; an open stale_base PR has no retry exit |
| 2026-11-14 | wo21 run 5 (F-76 live) | needs_human ci_failed, then a stalled recovery | 4 (revalidate, reset-attempts, merge #138, the repair) | 0 (session-less park) | 0 model tokens | ~8 min (sweep) | F-77 existed but was unmerged when needed; recover ran pre-F-77 code |
| 2026-11-14 | wo21 run 6 (F-77 live) | needs_human scope_change_needed (budget spent 2/2, PR kept) | 2 (reset, merge #139) | 60 + 43 | pending usage | ~29 min | a scope park on an open PR with spent budget has no exit that keeps the work (F-78); `factory adopt` named by the contract but absent from the CLI |
| 2026-11-14 | F-78 build (session) | branch held for review | 2 (approve F-78, merge pending) | — (session build, not a factory run) | — | — | **finding: the SFA profile declares `repo.branch_protection: unavailable` on a PUBLIC repo whose owner has ADMIN — a false declaration (the schema allows it only for plan-limit-403 private repos); `factory loop` on this repo correctly refused to build.** Owner deciding protection separately; F-78 built session-style on a branch instead |
| 2026-11-14 | wo21 run 7 (F-78 live) | **pr_opened**: fix round passed L0, L1 and review and pushed to #163; the PR's own tests went green | 3 (adopt, reset, loop) plus 2 unrelated (merge JobFinderOS #165, merge #163) | 92 | 227k in / 61k out / 4.0M cache-read | 26 min | the loop prints a stale pre-round `not ready` line; a `main`-side advisory reddens every PR and a re-run reuses the stale merge commit |
| 2026-11-15 | wo22 run 1 (first run after F-79, F-80, F-82) | **pr_opened** (JobFinderOS #166), 2 attempts, CI green on the first wait | 3 (contract review, approve, loop) plus the merge | 53 (failed lint) + 23 | 230k in / 41k out / 3.6M cache-read; $2.65 nominal | 28 min | a lint error cost a whole attempt; a reviewer's malformed JSON cost a whole session again (F-85); contract review does not converge between runs |

## Retros

### Run 2026-11-12: wo21-pdf-text-layer-verification, pr_opened (JobFinderOS #163)
- **Turns:**
  - Attempt 1 used all 100. About 10 went on setup: it built the venv, installed the dev lock, and retried pip with `--trusted-host` twice, which needs checking. 3 more went to borrowing a git identity. The rest went on the change and tests.
  - Attempt 2 took 31 turns to remove the skip marker and re-verify.
- **What failed, and why:**
  - Nothing escalated. F-71 worked: `pypdf` was declared, and the registry check passed (exists, first release 2014, no near-miss). `external_service` was declared, so no `risk_path_touched`.
  - Attempt 1 failed L0 on `skip_marker_added`. The implementer added `pytest.skip("no unicode TTF candidate on this machine")` to `test_pdf_fonts.py`, because the sandbox has no system fonts. That was the task's fault, and L0 caught it correctly. Attempt 2 removed the skip and passed L0, L1 and review. The verifier dropped 3 review notes as non-defects.
- **Owner touches:** 3.
  - The profile re-approve was a one-off caused by F-71's new group.
  - Approve and merge are the 2 the target allows.
  - The prep (the contract PR, pulls and the local-state discard) was my operator work this time. Normally it's the importer's.
- **Cost:** 233k input, 53k output and 6.0M cache-read tokens on the provider zai; 31.5 min wall clock. `provider_usage.model` recorded `?`, a small telemetry gap.
- **What broke in the factory:**
  1. **The implementer committed as the owner.** With no git identity in the clone, it read `git log` and committed attempt 1 as the owner's name and email (commit c8a8ead on #163). Agent work is now attributed to a human in a public repo. This is F-73 AC-2's git-identity half, which now has a real run behind it. → **Fix as a small WO:** set the factory identity in the clone before the session, and run setup there too.
  2. **Setup burn:** about 10 turns building a venv the profile's `setup` already knows how to build. That's the same F-73 AC-2 fix, in the same WO.
  3. **The sandbox has no fonts,** so a PDF font test can't pass inside it. The implementer reached for a skip. → Watch for it. If another PDF or font task hits it, add a font package to the sandbox or the setup. Not now.
  4. **pip needed `--trusted-host` retries:** unexplained. Read the trace before the next run; it could be the sandbox's TLS or proxy. Not a WO yet.
- **Fix as a WO?** Yes, one: F-73 AC-2 only (the clone gets the factory git identity and runs `setup` before the session), with one review round. F-73's AC-1 and AC-3 stay shelved.

### Run 2026-11-13: wo21-pdf-text-layer-verification, needs_human stale_base (JobFinderOS #163 live test)
- **Goal:** F-75's first real run. #163 is red on CI (`test_wo21_pdf_text_layer.py::test_ac_2`, both DBs), so the factory should wait, re-run the failed job once, then run a fix round.
- **What happened:** the run never reached CI. `main` had moved (b79058f → 4a8fcc3, the repo-move PR JFO #164), so the F-26 sweep re-ran L0/L1 on #163 merged onto the new head. L0's test step failed on `test_idle_transactions.py::TestRoutesReleaseBeforeSlowWork::test_match_run_background_task_runs_with_request_tx_closed` (1 failed, 759 passed), which is not in the baseline. The task parked as `stale_base`. Run 2 was idle.
- **Flaky, not a real failure:** the same merge (#163 head b727899 + 4a8fcc3) rebuilt locally passed the full backend suite twice (760 passed, 16 skipped). The test alone passed 6 out of 6. `main` passed too (756 passed). It also passed in #163's and #164's CI. So the failure looks timing-dependent and showed up once in three full runs.
- **Owner touches:** 1, the profile re-approve the repo move required.
- **Cost:** 0 model tokens (the sweep runs no session). About 3 min wall clock.
- **What broke in the factory:**
  1. **Stale-base re-validation has no flake guard.** One failing L0 run parks a PR that passed review. F-75 re-runs a failed CI job before acting; F-26's re-validation doesn't. Every merge to `main` re-validates every open factory PR, so the flaky test can catch any of them.
  2. **An open stale_base PR has no retry exit.** The sweep only acts on a merged PR (back to in_review) or a closed one (fix round from the current default head, which re-implements and does not reuse the PR's work). `factory recover` requires the PR closed. Re-opening only helps `pr_closed`. So a parked PR that is actually fine has no way back to in_review short of being rebuilt from scratch.
  3. **F-75 is still untested live.** The re-validation runs before the CI step, so the CI path never ran. `gh run list` on #163 still shows attempt 1 only.
- **JobFinderOS, not the factory:** `test_match_run_background_task_runs_with_request_tx_closed` is flaky. That's a JFO test issue. Separately, `test_ac_2` passes on macOS and fails on CI Ubuntu (`('cover_letter_pdf', 'Test Candidate')`), which is probably a font difference on the runner. That's the real red F-75 should face.
- **Fix as a WO?** Yes, one (needs owner approval): stale-base re-validation re-runs L0/L1 once on a failure before escalating (the F-75 flake-guard rule), and a parked stale_base PR with an open link gets a re-validate exit (e.g. `factory recover <task> --revalidate`) that keeps the PR's work. One review round. After it lands, re-run this live test.

### Run 2026-11-14 (morning): wo21 — F-76's first live pass parks `ci_failed`; the recovery ran before its fix existed
- **Goal:** F-76 live — re-validate #163 against the moved `main` (4a8fcc3), then face the red CI (`test_ac_2`, both DB legs).
- **What happened:** `--revalidate` passed (b727899 onto 4a8fcc3). The 09:06 sweep found CI red on the pushed head, re-ran the failed job once (`ci_rerun_heads` stamped), still red — the em-dash KNOWN_PHRASE never survives pypdf extraction on Ubuntu's fonts. Budget spent (`max_attempts: 2`, both burned by pre-F-75 runs) → parked `ci_failed` session-less, PR drafted. Correct behaviour, all of it.
- **The stall:** the 11:08 `recover --reset-attempts` ran with the checkout at F-76 — F-77 (the fix for exactly this) sat uncommitted in a worktree because its session died at the usage limit. The reset landed `ready`; `factory loop` then counted the picked task's own open PR #163 against `concurrency=1` and stopped `concurrency_wait` after 1 run. No writer moves `ready → in_review`: a permanent stall, not a wait.
- **Owner touches:** 4 (revalidate, reset-attempts, merge #138, the repair session).
- **What broke in the factory:**
  1. **F-77 existed but was unmerged when needed.** A session that dies at the usage limit leaves reviewed work uncommitted with nothing pointing at the worktree. The handoff plan said "check the F-77 PR exists and merge it" — but no PR had been opened; the step was unanswerable.
  2. **`factory` runs whatever the SFA main checkout holds** (editable install). A recover run before its fix merges is a silent no-op of the fix. The run-loop prep steps say "update the factory first" — that discipline slipped exactly when it mattered.
  3. **The reset evidence names a design gap:** attempts spent before F-75 existed counted against the ci_failed park (a budget semantic F-77's reset marker now bridges).
- **Fix as a WO?** F-77 was already it. Landed same day (PR #139): committed the dead session's worktree work after re-verifying (diff read, F-75/76/77 tests re-run, recover probed on a copy of the real queue), rebased over #138, 6/6 checks green on 879f623.

### Session repair (2026-11-14 ~12:00, recorded for the audit trail)
After #139 merged and main was pulled, wo21 was re-parked by hand (`ready → needs_human`, one field, the factory's own JSON format) — reconstructing the state the 11:08 reset should have found — and the REAL F-77 recover ran against it: `needs_human → in_review`, links untouched, a second reset marker on file, budget 0/2, `gh pr ready` un-drafted #163, progress entry `recovered | ci_failed → in_review`. The hand-edit is visible in `.factory/tasks.json`'s uncommitted diff and named in the reset evidence. The next `factory loop` started the CI fix round on #163 (run 20261114T134530Z-f49bb7) — the retro below covers it.

### Run 2026-11-14 (afternoon): wo21 — F-77's first live keep-the-PR recovery, and the fix round that found the real holes
- **Goal:** run the CI fix round on #163 under the F-77 reset (the red: `test_ac_2`, the em-dash KNOWN_PHRASE missing from pypdf extraction on Ubuntu fonts).
- **The recovery worked end to end.** After #139 merged and main was pulled, the task was re-parked by hand (`ready → needs_human`, one field — reconstructing what the 11:08 pre-F-77 reset should have found) and the real F-77 recover ran: `needs_human → in_review`, links untouched, budget 0/2, `gh pr ready` un-drafted #163, honest progress entry. The next loop's sweep started the fix round on the SAME PR — no re-implementation, no second PR.
- **The round itself:** attempt 1 (60 turns) fixed the font-dependent test; L0 and L1 passed; L2 found what the round was actually for — U-4 `[spec/major]`: the production send path calls `verify_text_layer` without `must_contain`, so a glyph-substituted, ATS-unreadable cover letter PASSES the runtime guard (the reviewer proved it empirically: forced `_resolve_unicode_font` to None → 136 chars of `?`-substituted extraction → the guard passes → dispatch); and U-2: `download_cover_letter`/`download_tailored_cv` build outbound PDFs with NO verification at all. Attempt 2 (43 turns) fixed what the contract covered and requested `backend/app/api/v1/applications.py` — parked `scope_change_needed`, nothing pushed, PR kept (drafted), budget spent 2/2.
- **What broke in the factory:**
  1. **A `scope_change_needed` park on an open PR with a spent budget has no exit that keeps the work.** `recover_scope` → `ready` (deadlocks on its own open PR, and the budget is spent anyway); the reset is gated to `attempts_exhausted`/`ci_failed`; closing the PR rejects the round's work. → **Fix as a WO: F-78** (this PR) — the keep-the-PR reset for scope parks.
  2. **`factory adopt` does not exist.** TASK-CONTRACT's scope exit names it as the ledger step; the CLI never got it — the designed exit was unusable end to end even before the PR/budget problem. F-78 AC-2 builds it.
  3. The reviewer finding real spec holes in a CI fix round is the loop working: the fix round's diff (a font-tolerant test) gave L2 the lens to check the guarantee, not just the test. No fix needed.
- **Owner decision (2026-11-14 ~15:15):** park wo21, fix the gap first — wo21 resumes after F-78 lands (widen scope, adopt, reset, loop).
- **Cost:** turns 60 + 43; token usage pending the run's usage record; ~29 min wall clock.

### Session 2026-11-14 (evening): F-78 drafted (#141), then built session-style — and a finding the loop surfaced
- **Finding (owner decision pending): the SFA profile declares `repo.branch_protection: unavailable` on a public repo whose owner has ADMIN.** The schema allows that declaration only for a plan-limit-403 private repo; this repo could have protection and doesn't, so `factory loop` on software-factory-agents itself refuses to build — correctly (its 2026-10-24 rule: never build into a repo where main can take a direct push). The owner decides whether to enable protection (strict required checks, admins included) or leave the factory's own repo session-built; nothing was changed. Recorded here per the owner's word: "don't touch branch protection today."
- **How F-78 was built:** the loop path blocked by the finding, the owner directed a build session on its own branch, as F-77 was: red-first tests (3 ACs red on the untouched tree for the behavioural reasons), the implementation (state + approve + cli), the full gate in the worktree venv, a 4-mutation battery (all red), the docs (TASK-CONTRACT rows, BUILD-ORDER §F-78, BOOTSTRAP §F-78). Committed on `feat/f78-scope-park-keep-pr`; push and PR HELD for owner review per the owner's instruction.
- **A misdiagnosis, corrected by the build:** the drafted F-78 AC-2 ("`factory adopt` exists — a command TASK-CONTRACT names but the CLI never got") was wrong. The command exists as `factory approve --adopt` (F-63's `adopt_queue`: renders the contract, interactive, records the whole-contract hash, works on non-draft tasks). The draft's author (this same operator) searched for the literal subcommand, found nothing, and wrote the WO around the gap. AC-2 became "the reset's ledger gate accepts exactly what `factory approve --adopt` records," TASK-CONTRACT's command name is corrected, and the lesson is logged: verify the CAPABILITY, not the name, before declaring a command missing.
- **What F-78 changes:** `factory recover <scope-parked task> --reset-attempts --evidence` — the F-65 ledger gate first (the widened contract must be re-approved via `factory approve --adopt`; unapproved refuses naming the remedy, GitHub never read), then the F-77 keep-the-PR rule by the PR's state: open → `in_review` with links untouched and the drafted PR un-drafted, so the next sweep runs the CI fix round on the same PR under the widened scope; closed → fresh-run re-queue with the marker; merged/unreadable → refused. One predicate, two parks (`keep_pr_eligible`: ci_failed + scope_change_needed); the marker writer itself DEMANDS the owner ledger for a scope park (owner review round pre-commit: the first cut gated only the command — a direct `TaskQueue.recover_attempts` call bypassed F-65; the gate moved into the writer, `recover_scope`'s shape, red-proven by revert-check), while the command dispatch set stays — a scope park only ever routes through the scope half.
- **Post-merge (the owner's sequence for wo21):** widen `scope.paths` with `backend/app/api/v1/applications.py` → `factory approve --adopt` → `factory recover wo21-pdf-text-layer-verification --reset-attempts --evidence "…"` → `factory loop` (the U-2/U-4 findings are already in the task's progress log for the fresh session).

The target is 2 owner touches per task: approve and merge.

### Run 2026-11-14 (evening): wo21 — F-78's first live recovery, and the first fix round that published
- **Goal:** resume the scope-parked wo21 on #163 with the widened contract (`backend/app/api/v1/applications.py` in scope, `max_attempts` 3).
- **What happened:** `factory approve --adopt`, then `factory recover --reset-attempts` kept the PR and returned the task to `in_review`. The next `factory loop` ran the CI fix round as run `20261114T184258Z-140cfb`: one attempt, 92 turns, 26 minutes. L0, L1 and review passed (3 review findings, none dropped) and the round pushed `1c7e85f` to #163 under the factory identity. Both backend test jobs went green on that head.
- **Escalated:** none.
- **The one red check was not the task's.** `Security audit (pip-audit)` failed on `dnspython 2.9.0`, an advisory published against a pin already on the project's `main`. It was fixed on `main` by a two-line lock bump (JobFinderOS #165). Re-running the failed job did not help: a re-run reuses the run's original merge commit, built against the old `main`. A close-and-reopen of the PR produced a fresh merge commit, and all 8 checks passed.
- **Owner touches:** 3 for the task (adopt, reset, loop). Two more for the unrelated advisory and the merge.
- **Cost:** 227k input, 61k output and 4.0M cache-read tokens.
- **What broke in the factory:**
  1. **The loop printed a stale line.** Under the run that published, it printed `not ready: CI red (Backend…)`, which was the sweep's read of the old head from before the round. The line is true when read and false when printed. Small reporting fix.
  2. **A red caused by `main` looks the same as a red caused by the task.** With budget left, the next sweep would have started a paid fix round for an advisory the task did not introduce. The factory has no check for "this check also fails on the default branch". Watch for a second case before building anything.
  3. **A re-run cannot see a fix that landed on the default branch.** F-75's flake guard re-runs the failed job, which reuses the old merge commit. That is right for flakes and useless for a `main`-side fix. Same watch item as 2.
- **Fix as a WO?** No new one from this run. F-79 (kept fix-round work, one resume rule) was already in build and covers what the afternoon run lost. Items 2 and 3 wait for a second occurrence.


### Run 2026-11-15: wo22-apply-link-recovery, pr_opened in one run (JobFinderOS #166)
- **Goal:** the second task through the factory, and the first run with the contract review (F-80), kept fix-round work (F-79) and follow-up drafting (F-82) in place.
- **Before the run: two contract reviews.**
  - The first (`factory review-contract`, 11 turns, 2 minutes, 34k input and 5.8k output tokens) returned 5 findings, 4 major: an undeclared `privacy` risk path, a scope gap (the API schema file was missing, so every criterion could pass while the user saw nothing), two behaviours merged into one criterion, and an ambiguous re-probe rule. All five were applied to the contract.
  - The second, on the edited contract (11 turns, 2.7 minutes), returned 6 more, 3 major. One of them is against an edit made in response to the first review: AC-2 now excludes a twin that has only an email address, and the source design counts such a twin as usable. The owner approved with these open.
- **The run** (`20261115T151556Z-94d58c`, 28 minutes):
  - Attempt 1: 53 implementer turns, failed L0 on lint. One unused import in the task's own test file (`F401`).
  - Attempt 2: 23 implementer turns, then 8 reviewer and 6 verifier turns. L0, L1 and review passed. It published #166: 488 added lines across 5 files, including an Alembic migration and the schema file the first contract review added to scope.
  - CI settled green on the first wait (8 polls). No fix round, no park, no recovery command.
- **Escalated:** none.
- **Owner touches:** 3 for the task (contract review, approve, loop), plus the merge. The second contract review was a fourth, optional one.
- **Cost:** 230k input, 41k output and 3.6M cache-read tokens; $2.65 at the trace's nominal rates. The attempt records carry no per-seat turns or cost because the run used the pre-F-83 code: the factory checkout was deliberately not updated while a loop was running.
- **What worked:**
  1. **The contract review changed the outcome.** The schema file it added to scope is in the diff. wo21 parked on exactly that kind of gap.
  2. **F-82 drafted the follow-up.** Three minor findings left open by review became `wo22-apply-link-recovery-followup`. One deserves more than "minor": the twin lookup loads the whole job-postings table on every call.
  3. **F-79 was not needed:** the round published, so no work was kept.
- **What broke in the factory:**
  1. **A lint error cost a whole attempt.** 53 turns were spent, the gate failed on one unused import, and the next attempt started over. Candidate fix: the session runs the profile's lint command before it finishes, or a lint-only failure gets a short fix round in the same clone instead of a new attempt. *(F-86, 2026-11-16: the second candidate is the rule — a lint/typecheck-only L0 failure gets ONE repair round in the same clone at a 10-turn cap, then the full gate again; nothing else about the failed-round path changed.)*
  2. **A reviewer's malformed verdict cost a whole session again** (trailing comma, same as the wo21 runs). F-85 is the fix and is in build.
  3. **Contract review does not converge.** A fresh review of an improved contract found six new things. The useful rule is one review, apply what matters, then the owner decides. A second review is worth it only after a large rewrite.
  4. **An edit made to satisfy a review can contradict the source document.** The reviewer offered two options for email-only twins; the more cautious one was written into the contract without checking it against the design. Edits made from review findings need the same check against the source that the original contract got.
  5. **Updating the factory under a running loop is unsafe,** and nothing prevents it. The installed command runs from the main checkout. Watch item: a guard, or a separate install.
- **Fix as a WO?** One new candidate, small: lint before finish (item 1). Items 3 and 4 are practice, recorded here. Item 5 waits for a second occurrence.


## Known gaps carried forward (fix only if a run hits them)
- ~~The clone has no virtualenv and no git identity before the session (F-73 AC-2, shelved). wo21 run 1 burned turns on it.~~ (F-73, 2026-11-15: the run clone gets the factory identity as repo-local config and the profile's `commands.setup` before each attempt's first session; nothing publishes under another identity — a foreign author or committer in base..head rebuilds the branch as one factory commit on the base, tree asserted equal.)
- ~~A lint-only failure cost a whole attempt~~ (F-86, from the wo22 run-1 trace): when L0 fails on the lint or typecheck command alone, the attempt gets ONE repair round in the same clone — implementer seat, 10-turn cap funded by the attempt's own budgets, the command's output as the only finding under the untrusted-data heading — and the full gate runs again, judging the repair's diff exactly like any change. The repair is visible on the attempt record (`lint_repair`), the trace and `factory status`.
- ~~A malformed verdict re-ran the whole reviewer session~~ (F-85, from the 140cfb/f49bb7 `reviewer_retry` traces): a completed seat whose verdict or vote fails to parse gets ONE repair turn on the SAME session (the SDK's resume) before today's full re-run — the parse error, the schema, "re-emit the verdict only; do not change any finding". The parsers stay exactly as strict; a repair that shrinks what the malformed text carried falls back to the re-run.
- ~~A failed session records no token usage~~ (F-74 AC-1's usage half, taken over by F-83): a session the SDK ends with a delivered result message keeps its turns/usage/cost whatever the outcome; a session that died before any result (wall clock, a transport error) still records none — its usage renders unknown, never 0.
- A retry gets only the failure line, not the previous attempt's work (F-73 AC-3, shelved) — fixed for fix rounds on an open PR by F-79 (the kept commit plus that round's review findings); a fresh run's later attempts still get only the progress log.
- ~~A park with no PR had no exit, and a failed escalation PR hid the real question~~ (F-92, from JobFinderOS run 3, 2026-11-17: `scope_change_needed` behind a missing `factory-escalation` label became `pr_create_failed`, and no `factory recover` form moved it): the label is created before the PR (never overwritten), a failed escalation PR keeps `original_escalation` and `links.branch` and every recovery reads that class, `factory recover <id> --redraft --evidence` returns any no-PR park to draft for re-approval, `--reject` answers an escalation draft PR of any class, and the notes name `factory approve --adopt` (a test checks every backticked command against the CLI).
- Local queue state lives in the repo's `.factory/tasks.json`, so a contract PR forces the owner to discard local state before pulling (step 3). This is new from this prep and becomes a WO only if it bites again.
