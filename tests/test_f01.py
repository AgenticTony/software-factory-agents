"""F-01 acceptance tests — BUILD-ORDER.md, work order F-01."""

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import yaml

REPO = Path(__file__).resolve().parent.parent
SUBCOMMANDS = ["approve", "run", "loop", "status", "import", "verify-live"]


def test_ac_1_help_lists_all_subcommands():
    result = subprocess.run(
        [sys.executable, "-m", "factory", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    for cmd in SUBCOMMANDS:
        assert cmd in result.stdout, f"`{cmd}` missing from factory --help"


def test_ac_2_ci_runs_ruff_and_pytest_and_both_pass():
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    runs = [
        step["run"] for job in ci["jobs"].values() for step in job["steps"] if "run" in step
    ]
    assert any("pytest" in r for r in runs), "CI must run pytest"
    assert any("ruff check" in r for r in runs), "CI must run ruff check"

    ruff = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert ruff.returncode == 0, ruff.stdout


def test_ac_3_project_profile_validates():
    profile = yaml.safe_load((REPO / ".factory" / "project.yaml").read_text())
    schema = json.loads((REPO / "schema" / "project.schema.json").read_text())
    jsonschema.validate(profile, schema)
