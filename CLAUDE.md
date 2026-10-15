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
- **Schema-validate on load.** Code touching `tasks.json` or `project.yaml`
  validates against `schema/` first (F-02). Invalid state refuses to run.
- **Acceptance criteria become failing tests before code** (red-first,
  VALIDATOR-SPEC L1). A work-order session writes the tests first.
- **Mechanical rules are linters/tests, not prose.** If a rule can be checked,
  check it in CI instead of writing it here.
- `ruff check .` and `pytest -q` must pass before any commit.

## Working here

- Python >= 3.11. Setup: `pip install -e ".[dev]"`. CLI: `factory --help`.
- Bootstrap phase: one work order per session (F-01 to F-11, BUILD-ORDER.md).
- CI (`.github/workflows/ci.yml`) runs ruff + pytest on every PR.
