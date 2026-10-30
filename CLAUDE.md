# Software Factory — repo guide

This repo IS the factory: a Python CLI that turns approved task contracts into
reviewed PRs via sandboxed agent sessions. Humans approve specs and merge;
agents do the volume. Architecture and reasoning: `DESIGN-LOCAL-FACTORY.md`.

## Read in this order

1. `TASK-CONTRACT.md` — the task record, statuses, picker (what the factory consumes)
2. `VALIDATOR-SPEC.md` — validation layers L0–L4, how "done" is proven
3. `BUILD-ORDER.md` + `.factory/tasks.json` — the queue; this repo's own build plan
4. `schema/` — `tasks.schema.json`, `project.schema.json`
5. `projects/` — draft adapters for the target projects

## Rules that outlive this file

- **Never hand-edit `.factory/tasks.json`.** The orchestrator (F-03+) is its only
  writer. Changes go through a PR a human reviews.
- **Bootstrap (until F-04 lands):** record each finished work order in
  `.factory/BOOTSTRAP.md`, with evidence per acceptance criterion. F-04 reconciles
  it into `tasks.json`.
- **Contract test names are exact:** an acceptance ref `tests/test_fNN.py::test_ac_N`
  means a test function with exactly that name.
- **Schema-validate on load.** Code touching `tasks.json` or `project.yaml`
  validates against `schema/` first (F-02). Invalid state refuses to run.
- **Acceptance criteria become failing tests before code** (red-first,
  VALIDATOR-SPEC L1). A work-order session writes the tests first.
- **Mechanical rules are linters/tests, not prose.** If a rule can be checked,
  check it in CI instead of writing it here.
- `ruff check .` and `pytest -q` must pass before any commit.
- **Git:** never commit to `main`. One branch + PR per work order
  (`feat/fNN-<slug>`). Conventional commits (`feat:` / `fix:` / `chore:` / `docs:`),
  with the WO id in the subject, e.g. `feat: contract loading (F-02)`. **Never add
  `Co-Authored-By` or any AI attribution trailer.**

## Working here

- Python >= 3.11; CI tests 3.11 and 3.13. CLI: `factory --help`.
- Tests run in parallel: `pytest -q -n auto` (pytest-xdist, pinned in the
  `[dev]` extra). Plain `pytest -q` still works. The two real-session tests in
  `tests/test_f06.py` (`test_ac_3`/`test_ac_4`) go live against the `factory-zai`
  credential when one exists and are conventionally deselected (`--deselect`)
  from local parallel runs.
- Setup: `pip install -e ".[dev]"` **and** `npm ci` (installs the pinned sandbox
  runtime `srt` from `package-lock.json`; needs Node >= 22.12). System deps:
  none on macOS; Linux `bubblewrap socat ripgrep`, and on Ubuntu
  24.04+ `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. Without
  them the F-05 sandbox tests fail (never skip) — see README "Setup".
- Bootstrap phase: one work order per session (F-01 to F-11, BUILD-ORDER.md).
- CI (`.github/workflows/ci.yml`) runs ruff + pytest on every PR.
