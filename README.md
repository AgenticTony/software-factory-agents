# Software Factory

A local agent software factory: approved task contracts in, reviewed PRs out.
Humans set intent and merge; sandboxed agent sessions do the volume.

- [CLAUDE.md](CLAUDE.md) — repo guide and reading order
- [BUILD-ORDER.md](BUILD-ORDER.md) — the factory's own build plan (F-00…F-21)
- [TASK-CONTRACT.md](TASK-CONTRACT.md) · [VALIDATOR-SPEC.md](VALIDATOR-SPEC.md) — what the factory consumes, and how "done" is proven
- [DESIGN-LOCAL-FACTORY.md](DESIGN-LOCAL-FACTORY.md) — architecture and the research behind it

## Setup

```bash
pip install -e ".[dev]"   # the factory CLI + ruff/pytest (Python >= 3.11)
npm ci                    # the pinned sandbox runtime, srt (Node >= 22.12)
```

Agent sessions run inside Anthropic's [`srt`](https://github.com/anthropic-experimental/sandbox-runtime)
(Seatbelt on macOS, bubblewrap on Linux). It is pinned exactly in `package.json`
and installed from the committed lockfile; the factory verifies `srt --version`
(once per process) before sandboxing anything and refuses to run on a
mismatch. System dependencies (per srt's own README):

The verified hosts (this table lists exactly them — `tests/test_f58.py::test_ac_6`
proves it against CI's `runs-on` in both directions):

| Host | Status | Install |
|---|---|---|
| macOS | supported — the factory's own host, verified by the owner's local gate (CI has no macOS job) | none (uses the built-in `sandbox-exec`) |
| Ubuntu 24.04 | supported — verified by CI (`test (ubuntu-24.04)` and the other 24.04 jobs) | `sudo apt-get install -y bubblewrap socat ripgrep` and `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (AppArmor otherwise blocks bubblewrap's user namespaces) |
| Ubuntu 26.04 | NOT supported — srt's nested user namespace fails (`apply-seccomp: write /proc/self/setgroups … Permission denied`, PR #97) and the sysctl does not lift it; deferred to the proposed F-61 work order (`.factory/BOOTSTRAP.md` §F-58) | — |

Then `ruff check . && pytest -q -n auto` (parallel via pytest-xdist; plain
`pytest -q` works too). The sandbox tests attempt the forbidden
actions for real and are never skipped: a missing `srt` or system dependency
fails them, by design. Deliberate exceptions are visible in the `-rs` skip
summary, never silent: the live-session halves of the f06 contract tests run
only on the explicit `FACTORY_LIVE_TESTS=1` opt-in (F-51 — never inferred
from a credential being present), and tests marked `live_network` (they need
the public internet) are deselected by the main CI job and run in the named
`live-network` job instead. CI (`.github/workflows/ci.yml`) runs the same
steps plus the `typecheck` (pyright) and `wheel` install jobs.

## Operating: doctor every onboarded project (F-66)

`factory doctor [--repo PATH] [--factory-root ROOT]` is a read-only health
check of every repo the approval ledger records (each profile approval
carries its repo path) plus any `--repo` given. It loads each repo's
profile and queue through all-errors modes of the real loaders and reports
every finding — schema and semantic violations, tasks approval would refuse
(a missing `scope.paths`, an over-cap budget), profile approvals that no
longer match, a queue older than the current contract epoch — each with
the epoch, the work order that introduced the rule, and the exact fix
command. Any finding exits non-zero; a clean repo prints `clean` and exits
0. It starts no session and writes nothing.

`factory run`/`loop`/`import` run the same checks for their own repo
first and refuse with the full report instead of one bare schema error
(the F-13 pilot met three rule changes that way). A queue that fails
validation but still parses, with every task `draft`, is re-proposed by
`factory import` under the current rules — you review the whole diff as
a first import; a task past `draft`, or a file that doesn't parse,
refuses with the tasks named.

## Operating: review a contract before you approve it (F-80)

`factory review-contract <task-id>` runs one read-only model session that
reads a task's contract and the document its `source` names (as committed
on the default branch) and reports findings under six lenses — purpose,
testability, scope, ambiguity, budget, and (F-94) the defect class: a
criterion that fixes one site of a defect that occurs elsewhere is a
finding naming the other sites. A valid review is stored under
`<factory-root>/contract-reviews/`, keyed by the contract hash, and
`factory approve <task-id>` shows it above the y/N, blockers first. A
reply that mis-formats the findings object gets one repair turn on the
same session (F-108) — the parse never loosens, and a repaired review is
flagged "(format repaired)" at approval. It is
advice: nothing blocks on it and nothing is edited for you; any change to
the contract makes the stored review stale. It spends one session, so run
it outside the peak window (TASK-CONTRACT §3).

## Operating: the monthly validator eval (F-16)

The validator is measured, not assumed. `factory eval` seeds the known
bug classes on branches of the SoloAdventurer fixture, runs every layer
over each seeded diff plus known-clean diffs, and writes a per-diff
report: which layers caught each seed, every layer's verdict on each
known-clean diff (a clean diff a layer wrongly failed shows as that
layer's false positive), and the catch rate per layer as the **mean and
spread** over repeats — one run is a ratchet, not a measurement. Per
seed it shows `caught k/N repeats`, the pass^k line (seeds caught on
every repeat), the seeds never caught ("check the seed or grader
first"), and "found, then dropped" — a finding on the seeded defect
that the verifier voted out, each entry saying whether that run was
still caught and the report counting the drops that cost a catch. Each
diff's contract states the developer's task as intent (F-99): the
eval's own description of the seeded bug — its title AND its id —
never reaches a seat (contracts, branches and commit messages run on
opaque EVAL-NN slots), so the numbers measure the reviewers, not the
eval's own text. Besides the
bug classes, the seeds include shapes of real misses (F-95): a defect's
second site that L2 must report as a non-blocking `sibling`, a diff
that breaks an unchanged caller, and a clean diff next to old code of
the same class.

- **On demand**: run `factory eval` whenever the validator changes
  (a gate, a prompt, the review fan-out) or after a run escaped it.
- **Monthly**: run it once a month regardless — a validator nobody has
  re-tested is just another claim. It drives real model sessions, so it
  is never in CI or `factory run`/`loop`; this command is the only thing
  that starts it.
- **Repeats**: `--repeats N` (default 3, pending owner confirmation
  along with the cadence) runs each diff N times; every rate in the
  report carries its mean, stdev and min–max over the repeats. Each
  repeat is a full L2 pass over 11 diffs (8 seeds, 3 clean) — 22
  reviewer sessions per repeat at 2 reviewers plus the F-96 per-finding
  verifier fan-out (one session per union finding, two per
  blocking/major — the seed diffs carry one or two findings each, so
  roughly 11–22 verifier sessions), so N multiplies the model cost:
  expect around 100 sessions at the default N=3.

Owner steps per run: read the rendered report it prints and saves under
`<factory-root>/eval/`, record it as tick evidence, and act on a layer
whose catch rate is falling — fix it or drop it. The reviewer fan-out
width is the `reviewers` knob in `<factory-root>/config.yaml` (override
per run with `--reviewers`), so the eval always measures the review
pipeline the queue actually runs.

## Operating: the model registry and stacks (F-100)

Every model the factory uses is set in one place — the `models` block of
`<factory-root>/config.yaml` (owner-maintained; the factory never writes
it) — and each project names the stack it runs in its `project.yaml`
(`model_stack`, an owner-approved security field). The block ships three
documented stacks as the default (`factory/models.py`'s
`DEFAULT_REGISTRY`, mirrored in the config docstring):

- `standard` — **zai-only** (owner decision 2026-11-19): every role on
  the GLM workhorse `glm-latest`, so an existing zai profile loads and
  runs with no new credential. Own projects and every client whose
  onboarding answer is "no restriction".
- `standard-plus` — the opt-in: standard's zai seats plus the
  cross-family defect-hunt seat (anthropic `claude-sonnet-5-5`) and the
  final-review tiers (anthropic `claude-opus-5-5`/`claude-fable-5-2`,
  F-97 reads them). A project reaches it only through `model_stack`
  (re-approval) with anthropic in its `providers_allowed`.
- `no-china` — a client whose answer forbids Chinese-provider models:
  anthropic seats only.
- `contained` — the code may not leave a named environment: bedrock in
  the client's region (the `region` of the project's own provider
  block).

Bumping a model is one line: `aliases: {glm-latest: glm-5.4}` moves
every role that uses the alias, with no re-approval — a project's
`providers_allowed` list and its recorded `model_restrictions` answer
stay the authority, and a stack edit that would move a project outside
them refuses at load, naming the project, the role and the kind (never a
fallback). A client's restriction answer (`none` | `no_china` |
`contained`, with who answered and when) is asked at onboarding and
recorded in the profile — clients are open by default, and a client with
no recorded answer refuses to load. A profile's legacy
`provider.models` block (F-59) still overrides the stack for one
release, but a registry edit cannot reach a seat pinned there — move to
`model_stack`. Every run records what actually ran: each session's
record and trace carry the role, the stack, the requested model (alias
expanded) and the model the API reported (`factory status` shows the
stack, the model per role, and any requested-vs-reported mismatch), and
the PR body names the implementer's model.

Try a model before switching: `factory eval --role
reviewer.hunt=claude-sonnet-5-5` (repeatable) runs the seeded eval with
those roles overridden for that eval only — never written to any profile
or to the registry, named in the report, still inside the fixture's
allowed providers. A before/after pair of reports is the evidence for a
registry change.

The implementer is measured the same way, on real past work (F-104): an
implementer model change — an alias bump or a stack edit — is preceded
by a `factory bench run <project> --role implementer=<new model>
--against <latest report>` whose diff the owner reads before approving
the registry edit. `factory doctor` notes a registry whose implementer
model differs from the one the latest bench report ran.

## Operating: the replay benchmark of merged tasks (F-104)

The eval measures the reviewers; the bench measures the implementer, on
real past work. `factory bench add <project> <task-id | pr-number>`
records a MERGED task as one item: its approved contract plus its
hidden tests (the files its test-checked acceptance refs name), proven
at add — the hidden tests must fail on the base commit and pass on the
merged head three times, and a contract that quotes the merged diff
refuses. Items live under `<factory-root>/bench/<project>/`, outside
every sandbox, schema-validated on load.

`factory bench run <project> [--repeats N] [--role implementer=<alias>]
[--against <report>]` replays each item through the real pipeline
(implementer, L0, L1, L2 with the verifiers) in a clone of the repo as
it was BEFORE the work: one branch at the base, no remote, no tags,
packs rebuilt, and verified to hold no commit from base..head before
any session starts. Publishing is replaced by scoring — after the
attempt settles the parent copies the hidden tests onto the replay's
head, outside the session, and runs them. A replay is a **clean merge**
only when the hidden tests pass AND the factory would have published:
an escalation is never a clean merge. The report gives per item k/N
clean merges, the yield figure (clean merges per unit cost), cost per
clean merge, the models that ran per role, and the `--against` diff
with regressions (pass → fail) first.

Paid sessions: every replay with the default seat runner spends real
money (~$25–35 per 1-repeat run of 8 items, an estimate). It is the
owner's command, like `factory eval` — never in CI, never from
`factory run`/`loop`. Default repeats is 1 (a swap check); use 3 for a
baseline.


Private by design. Nothing here deploys anywhere; it's a CLI.
