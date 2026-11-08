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

## Operating: the monthly validator eval (F-16)

The validator is measured, not assumed. `factory eval` seeds the known
bug classes on branches of the SoloAdventurer fixture, runs every layer
over each seeded diff plus known-clean diffs, and writes a per-diff
report: which layers caught each seed, every layer's verdict on each
known-clean diff (a clean diff a layer wrongly failed shows as that
layer's false positive), and the catch rate per layer as the **mean and
spread** over repeats — one run is a ratchet, not a measurement.

- **On demand**: run `factory eval` whenever the validator changes
  (a gate, a prompt, the review fan-out) or after a run escaped it.
- **Monthly**: run it once a month regardless — a validator nobody has
  re-tested is just another claim. It drives real model sessions, so it
  is never in CI or `factory run`/`loop`; this command is the only thing
  that starts it.
- **Repeats**: `--repeats N` (default 3, pending owner confirmation
  along with the cadence) runs each diff N times; every rate in the
  report carries its mean, stdev and min–max over the repeats. Each
  repeat is a full L2 pass — up to 24 real sessions per repeat (16
  reviewers + 8 verifiers in the last baseline), so N multiplies the
  model cost: 72 sessions at the default N=3 versus 24 at N=1.

Owner steps per run: read the rendered report it prints and saves under
`<factory-root>/eval/`, record it as tick evidence, and act on a layer
whose catch rate is falling — fix it or drop it. The reviewer fan-out
width is the `reviewers` knob in `<factory-root>/config.yaml` (override
per run with `--reviewers`), so the eval always measures the review
pipeline the queue actually runs.

Private by design. Nothing here deploys anywhere; it's a CLI.
