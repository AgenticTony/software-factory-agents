# REVIEW.md — what matters when reviewing this repo

Written for the reviewers (human and L2) of this factory's own code.
CLAUDE.md carries the rules of working here; this file carries the rules
of *judging* work here. Loaded by the reviewer only (the implementer
never sees it — standards live with the reviewer, F-09 AC-5).

## Every finding carries evidence

- File and line, always. A finding without a location is a hunch.
- A claim of "done" or "works" needs output from the thing users touch —
  a test, a command's exit, a rendered page. Prose is not evidence.
- Interrogate the data source of every number and display: where does it
  read from, and what happens when that source is a clone on another
  branch, an unparsable stamp, or a state that skipped a transition?
  (Three of eight findings in the F-11 review round were this class.)

## Display values never assert falsehoods

Fail-closed is for access checks. For anything a human reads, unknown
and offline are honest; "disabled", "empty" or "none" printed about a
state you could not read is a lie with a straight face. If every step is
done, say that — never "no steps recorded".

## Conventions (violations are findings, not nits)

- Conventional commits with the WO id in the subject: `feat: … (F-11)`.
- One branch + one PR per work order; never commit to main.
- Protected paths (`.factory/**`, judge modules, contract tests,
  `factory/data/**`, the spec docs) change only through owner-reviewed
  interactive PRs — a factory session touching them is a blocker.

## Nit budget

A nit is a finding of severity `minor` or `note`. Report every
`blocking` and `major` finding you have evidence for — the budget never
caps them, and never drop a real defect to stay within it. At most three
nits per review: keep the three that matter most. Style-only findings
are dropped (the linter owns style).

The budget limits noise, not the verdict. The verdict follows the
reviewer prompt's rules: default FAIL, a `blocking` finding fails, and
FAIL stands without one when the evidence for an acceptance item isn't
there. A surviving `major` finding fails the round exactly as a
`blocking` one does (F-41): no PR opens with one, and a declared pass
does not override it. Nits alone never turn a pass into a fail — do not
manufacture a blocking finding to be thorough, and do not inflate a nit
to `major`.

## The bug class beats the bug instance

When you find a defect, name its class and check the siblings (the fix
prompt will ask for exactly that). A fix that addresses one instance of
a systematic mistake is half a fix.
