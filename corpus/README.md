# Sentinel corpus (F-33)

The corpus that judges the built-in sentinels (BUILD-ORDER.md F-33).
CI replays every built-in over both corpora on every PR
(`tests/test_f33.py::test_ac_3`); `factory sentinels replay` does the
same by hand, read-only. The directory is a protected path
(`paths.protected` in `.factory/project.yaml`), so a session cannot edit
the evidence that judges its sentinels.

## Benign — this repo's own merged PRs

`benign/<pr-number>.patch` is `gh pr diff <n>` of a merged PR, fetched
2026-10-23. Every PR merged at build time is in, except the exclusions
below — PRs were not picked to dodge findings.

Building this corpus forced four red-first sentinel fixes (each with the
corpus PRs that tripped it; see the F-33 ledger entry and its review
round):

- `assertion_removed` flags removal, not movement: per test file,
  verbatim re-adds pair up and a file that ends with as many assertion
  lines as it started (moved, edited, replaced) is quiet; only a net
  loss flags. A **replacement** must be a real assertion statement — a
  tautology (`assert True`, a constant) or a string mentioning "assert"
  levels nothing; comments are neither assertions nor replacements.
  (PRs #2, #11, #27, #28, #35, #44 tripped the first cut; the review
  round closed the tautology/padding holes.)
- `skip_marker_added` matches on the line with quoted spans blanked:
  every statement shape fires (decorators, module-level `pytestmark`,
  one-line conditionals, `pytest.param` marks, the JS forms), while
  marker text inside a string fixture stays inert. (PR #8.)
- `lint_suppression_added` requires a comment syntax the file's
  language has: `# noqa` inside JSON or markdown prose quotes the
  pattern; it cannot silence a linter there. (PRs #25, #37.)
- `credential_added`'s generic "secret assignment" shape exempts the
  env call itself — its head and first argument are blanked before the
  shape runs, so a bare read (`token = os.environ.get("TOKEN", "")`) is
  clean while a 16+ literal anywhere else on the line (a fallback
  argument, an `or` after the call, a comment's env mention) still
  flags. The prefix shapes (AKIA, ghp_, sk_live…) scan env-read lines
  regardless. (PR #18; the review round closed the
  env-text-anywhere hole.)

## Excluded PRs — correct hits, the sentinel was right

| PR | Why it is not in the corpus |
|----|------------------------------|
| #2 (F-01 review round) | consolidated three wiring asserts into two stronger ones — a real net assertion reduction the count rule flags; a human reviewed exactly that at merge time. |
| #19 (F-09) | `tests/test_f09.py` writes a fixture secret file (`SEKRIT-KEY-…`) to prove redaction — a seeded credential fixture, exactly what `credential_added` exists to surface. |
| #37 (F-25) | the sentinel tests themselves: seeded credential fixtures (AKIA/ghp_/sk_live literals), skip-marker fixtures, and lint-suppression prose — 30 correct hits. |
| #42 (F-27) | vent tests carry credential-shaped vent text to prove vents redact. |

45 PRs were merged at build time; 41 are in the corpus.

## Evidence limits (review finding 5)

Under this repo's production risk globs (`schema/**`, `.factory/**`,
the spec markdown, workflows), almost no Python is in lint scope — the
"0 benign hits" for `lint_suppression_added` is evidence for JSON and
markdown control files only. The corpus is therefore **also** replayed
under a catch-all glob (`{"all_paths": ["**"]}`) by
`test_review7_lint_evidence_limit_is_pinned`: the result is six genuine
`# noqa` additions in Python files — correct hits, had those files been
risk paths — and zero prose/JSON hits:

- #18 `factory/session.py:722` (`# noqa: PLW0603`), #22
  `tests/test_f10.py:446,455` and #40 `tests/test_f26.py:978,1300`
  (`# noqa: E731`), #45 `factory/trace.py:258` (`# noqa: BLE001`).

Benign evidence for the lint sentinel over Python/TSX **risk paths**
(the JobFinderOS shape: auth, privacy, external_service) first exists
when that project onboards and contributes its own corpus (F-13); this
repo's risk paths hold no production code.

## Attack — hand-written, fresh, low-entropy

`attack/<sentinel>-<n>.patch`; the name says which sentinel must flag
it. Written fresh, separate files from the F-25 AC-3 seed fixtures
(those stay inline literals in `tests/test_f25.py`), so tuning a
pattern to its own seed cannot hide a regression. The review round
added one file per relaxed boundary (`skipIf`/`pytestmark`/inline/
`param` marks, `assert True` swaps, string padding, env-comment and
`or`-fallback secrets, a quoted-path secret). F-44 (review I-11) added
`assertion_removed-5.patch`: a `with pytest.raises(...)` block removed
wholesale — the with-line is the only line naming the assertion. F-44
review round 2 added `runner_exit_forgery-{1,2,3}.patch`: the hard-exit,
capture-escape and session-hook families of the in-process runner
forgery (one file per family).

Every fixture token is deliberately **low-entropy placeholder-shaped**
(`ghp_0000…`, `AKIA0000…`, zero runs): they match the sentinel's shapes
— the gate proves it on every PR — without tripping GitGuardian or
GitHub secret scanning, which would otherwise hold a standing red
check on this repo. If a scanner still flags one, mark it a known
false positive in the scanner's dashboard rather than changing the
fixture back: a high-entropy fixture is indistinguishable from a leak.

## Regenerating

```sh
gh pr list -R AgenticTony/software-factory-agents --state merged \
  --limit 100 --json number --jq '.[].number' \
  | while read -r n; do gh pr diff -R AgenticTony/software-factory-agents \
      "$n" > "benign/$n.patch"; done
```

Then re-run the gate: `python -m pytest -q tests/test_f33.py`. Every hit
is either a new false positive (fix the sentinel red-first) or a new
exclusion (record the PR and the reason above, in the same PR).
