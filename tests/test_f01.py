"""F-01 acceptance tests — BUILD-ORDER.md, work order F-01.

Test names must equal the contract refs in .factory/tasks.json
(`tests/test_f01.py::test_ac_<n>`); L1 (F-08) resolves them by node id.
"""

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import yaml

REPO = Path(__file__).resolve().parent.parent
SUBCOMMANDS = ["approve", "run", "loop", "status", "import", "verify-live"]


def test_ac_1():
    """AC-1: `factory --help` lists every subcommand."""
    result = subprocess.run(
        [sys.executable, "-m", "factory", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    for cmd in SUBCOMMANDS:
        assert cmd in result.stdout, f"`{cmd}` missing from factory --help"


def test_ac_2():
    """AC-2 (wiring only): CI runs the exact gates on every PR.

    This proves the workflow is wired, not that it passes. The pass itself is
    evidenced by the CI run on the F-01 PR (see .factory/BOOTSTRAP.md).
    """
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    triggers = ci.get("on", ci.get(True))  # PyYAML parses bare `on:` as True
    assert "pull_request" in triggers, "CI must run on pull requests"
    runs = [
        step["run"].strip()
        for job in ci["jobs"].values()
        for step in job["steps"]
        if "run" in step
    ]
    assert "ruff check ." in runs, "CI must run `ruff check .` as its own step"
    assert "pytest -q" in runs, "CI must run `pytest -q` as its own step"


def test_ac_3():
    """AC-3: this repo's own project profile validates against the schema."""
    profile = yaml.safe_load((REPO / ".factory" / "project.yaml").read_text())
    schema = json.loads((REPO / "schema" / "project.schema.json").read_text())
    jsonschema.validate(profile, schema)


def test_contract_refs_resolve():
    """Every F-01 acceptance ref in tasks.json names a test in this module."""
    tasks = json.loads((REPO / ".factory" / "tasks.json").read_text())["tasks"]
    f01 = next(t for t in tasks if t["id"] == "F-01")
    for ac in f01["acceptance"]:
        path, _, name = ac["check"]["ref"].partition("::")
        assert path == "tests/test_f01.py", ac["check"]["ref"]
        assert callable(globals().get(name)), f"{ac['check']['ref']} does not resolve"
