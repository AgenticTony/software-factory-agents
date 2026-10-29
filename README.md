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

Private by design. Nothing here deploys anywhere; it's a CLI.
