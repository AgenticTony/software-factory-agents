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
corpus PRs that tripped it; see the F-33 ledger entry):

- `assertion_removed` now flags removal, not movement: per test file,
  verbatim re-adds pair up and a file that ends with as many assertion
  lines as it started (moved, edited, replaced) is quiet; only a net
  loss flags. Commented-out assertions are not replacements. (PRs #2,
  #11, #27, #28, #35, #44.)
- `skip_marker_added` anchors to statement level: the marker must be a
  decorator or a standalone call. Marker text inside a string literal
  is fixture data — the scanner tests embed markers in quoted patch
  fixtures. (PR #8.)
- `lint_suppression_added` requires a comment syntax the file's
  language has: `# noqa` inside JSON or markdown prose quotes the
  pattern; it cannot silence a linter there. (PRs #25, #37.)
- `credential_added`'s generic "secret assignment" shape yields on
  environment reads (`token = os.environ.get("TOKEN", "")` is the safe
  pattern); the prefix shapes (AKIA, ghp_, sk_live…) still scan those
  lines, so a hardcoded fallback still fires. (PR #18.)

## Excluded PRs — correct hits, the sentinel was right

| PR | Why it is not in the corpus |
|----|------------------------------|
| #19 (F-09) | `tests/test_f09.py` writes a fixture secret file (`"SEKRIT-KEY-12345"`) to prove redaction — a seeded credential fixture, exactly what `credential_added` exists to surface. |
| #37 (F-25) | the sentinel tests themselves: seeded credential fixtures (AKIA/ghp_/sk_live literals), skip-marker fixtures, and lint-suppression prose — 30 correct hits. |
| #42 (F-27) | vent tests carry credential-shaped vent text to prove vents redact. |

45 PRs were merged at build time; 42 are in the corpus.

## Attack — hand-written, fresh

`attack/<sentinel>-<n>.patch`; the name says which sentinel must flag
it. Written fresh for F-33, separate files from the F-25 AC-3 seed
fixtures (those stay inline literals in `tests/test_f25.py`), so tuning
a pattern to its own seed cannot hide a regression.

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
