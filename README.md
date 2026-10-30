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

| Platform | Install |
|---|---|
| macOS | none (uses the built-in `sandbox-exec`) |
| Linux | `sudo apt-get install -y bubblewrap socat ripgrep` |
| Ubuntu 24.04+ | also `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (AppArmor otherwise blocks bubblewrap's user namespaces) |

Then `ruff check . && pytest -q -n auto` (parallel via pytest-xdist; plain
`pytest -q` works too and is what CI runs). The sandbox tests attempt the forbidden
actions for real and are never skipped: a missing `srt` or system dependency
fails them, by design. CI (`.github/workflows/ci.yml`) runs the same steps.

## Operating: the monthly validator eval (F-16)

The validator is measured, not assumed. `factory eval` seeds the known
bug classes on branches of the SoloAdventurer fixture, runs every layer
over each seeded diff plus known-clean diffs, and writes a per-diff
report: which layers caught each seed, how many blocking findings each
known-clean diff drew, and the catch rate per layer.

- **On demand**: run `factory eval` whenever the validator changes
  (a gate, a prompt, the review fan-out) or after a run escaped it.
- **Monthly**: run it once a month regardless — a validator nobody has
  re-tested is just another claim. It drives real model sessions, so it
  is never in CI or `factory run`/`loop`; this command is the only thing
  that starts it.

Owner steps per run: read the rendered report it prints and saves under
`<factory-root>/eval/`, record it as tick evidence, and act on a layer
whose catch rate is falling — fix it or drop it. The reviewer fan-out
width is the `reviewers` knob in `<factory-root>/config.yaml` (override
per run with `--reviewers`), so the eval always measures the review
pipeline the queue actually runs.

Private by design. Nothing here deploys anywhere; it's a CLI.
