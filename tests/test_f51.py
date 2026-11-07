"""F-51 — Test suite and CI hygiene (BUILD-ORDER F-51, contract in
.factory/tasks.json; source findings CODE-REVIEW-2026-11-05 M-11, m-20,
m-21, m-22, m-23, I-1, I-16, I-17, A-7).

The goal behind the nine ACs: the gate that judges every factory change
must be TRUSTWORTHY — green on any interpreter and any machine because
the code is right; never red because of where Python lives, a live
endpoint or a timing sleep; never quietly weaker (a silent skip, a
deselected mandatory assertion, a blanket warning filter, a blanket
type-ignore); never spending money by accident; and the package works
as a wheel, not only from this checkout.

Red-first: written and run against untouched 47705d1 before any
implementation; each AC's red line is quoted in .factory/BOOTSTRAP.md.
Where a leg needs a symbol that only the implementation creates
(``_support.wait_until``, the ``_support`` package itself), it sits
AFTER the assertion that is red on the untouched tree, so the first
failure is always the AC's defect, never an import error.
"""

from __future__ import annotations

import ast
import glob as globmod
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
PROJECT_YAML = REPO / ".factory" / "project.yaml"
TESTS_DIR = REPO / "tests"
FACTORY_DIR = REPO / "factory"
VENV_PY = Path(sys.executable)

# The importer-prompt test whose bare-``python3`` assertion is M-11.
IMPORTER_PROMPT_TEST = (
    "tests/test_importer_schema_shapes.py::test_prompt_carries_the_self_check_instruction"
)

# Interpreters whose *paths* contain "python3" — M-11's victims.
HOSTILE_EXECUTABLES = [
    "/opt/conda/envs/factory/bin/python3",   # conda
    "~/.pyenv/versions/3.11.9/bin/python3",  # pyenv (a path shape; never expanded)
    "/usr/bin/python3",                      # system
    "/opt/my tools/factory/bin/python3",     # a space (shlex-quoted in the prompt)
]


# --------------------------------------------------------------- helpers --


def _source(path: Path) -> str:
    return path.read_text()


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def _ci() -> dict:
    return yaml.safe_load(CI_YML.read_text())


def _ci_run_steps(job: dict) -> list[str]:
    """Every ``run:`` command of a CI job, in order."""
    return [s["run"] for s in job.get("steps", []) if "run" in s]


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _has_marker(func: ast.FunctionDef | ast.AsyncFunctionDef, marker: str) -> bool:
    """True when the function carries ``@pytest.mark.<marker>``."""
    for dec in func.decorator_list:
        target = None
        if isinstance(dec, ast.Call):
            target = dec.func
        elif isinstance(dec, ast.Attribute):
            target = dec
        if (
            isinstance(target, ast.Attribute)
            and target.attr == marker
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark"
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "pytest"
        ):
            return True
    return False


def _module_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [node.module] if node.module else [""]


def _public_endpoint_in(text: str) -> str | None:
    """The first public (non-loopback, non-RFC1918) host or IP in text.

    A live_network test must reference the public internet somewhere —
    otherwise it was marked for no reason and the split is a silent
    deselect of a mandatory assertion.
    """
    local = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "example.invalid")
    for token in re.findall(r"[A-Za-z0-9_.:-]+", text):
        low = token.lower().strip(".:")
        if low in local or low.endswith(".local"):
            continue
        ip = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})", low)
        if ip:
            first, second = (int(x) for x in low.split(".")[:2])
            if first in (10, 127, 0) or (first == 192 and second == 168) or (
                first == 172 and 16 <= second <= 31
            ):
                continue
            return low
        if re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", low):
            return low
    return None


def _run_pytest(
    nodeids: list[str],
    *,
    env: dict[str, str] | None = None,
    plugin: Path | None = None,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run this repo's pytest, from this repo, with this venv."""
    run_env = dict(os.environ)
    run_env.pop("FACTORY_LIVE_TESTS", None)
    run_env["PYTHONPATH"] = str(REPO)
    plugin_args: list[str] = []
    if plugin is not None:
        run_env["PYTHONPATH"] = f"{plugin.parent}{os.pathsep}{run_env['PYTHONPATH']}"
        plugin_args = ["-p", plugin.stem]
    if env:
        run_env.update(env)
    return subprocess.run(
        [str(VENV_PY), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "-rs", *plugin_args, *(extra or []), *nodeids],
        cwd=REPO, env=run_env, capture_output=True, text=True, timeout=600,
    )


def _write_exec_plugin(tmp: Path, executable: str) -> Path:
    """A pytest plugin that pins sys.executable before any test runs."""
    plugin = tmp / "f51_exec_plugin.py"
    plugin.write_text(textwrap.dedent(f"""
        import sys
        def pytest_configure(config):
            sys.executable = {executable!r}
    """))
    return plugin


# ------------------------------------------------- AC-1 (M-11): bare python3 --


def test_ac_1(tmp_path: Path):
    """The importer-prompt no-bare-``python3`` check is
    interpreter-independent: the real contract test passes under conda,
    pyenv, system and space-containing interpreter paths, because the
    check removes the exact quoted interpreter path first and only then
    searches for a BARE ``python3``.

    M-11: today's ``assert "python3" not in prompt`` fails on every
    interpreter whose path contains ``python3`` — green CI is a naming
    accident (GitHub's setup-python binary is ``python``).
    """
    for executable in HOSTILE_EXECUTABLES:
        plugin = _write_exec_plugin(tmp_path, executable)
        proc = _run_pytest([IMPORTER_PROMPT_TEST], plugin=plugin)
        assert proc.returncode == 0, (
            f"the importer prompt test is red under interpreter {executable!r} "
            f"(M-11: the suite is green in CI only because GitHub's binary is "
            f"named 'python'):\n{proc.stdout[-2000:]}\n{proc.stderr[-500:]}"
        )
    # Teeth, on the exact check the fixed test uses: after the exact
    # interpreter forms are removed, ANY residual "python3" trips — a
    # bare one AND a hard-coded other interpreter (round 1, major 3:
    # /usr/bin/python3, ./python3, python3.11 are the same bug — not the
    # factory's venv).
    from _support import no_bare_python3

    exe = "/opt/conda/envs/x/bin/python3"
    assert no_bare_python3(f"run '{exe}' -c check", exe) is None
    for dirty in [
        f"run '{exe}' or just python3 -c check",
        f"run '{exe}' or use /usr/bin/python3 -c check",
        f"run '{exe}' or use ./python3 -c check",
        f"run '{exe}' or use python3.11 -c check",
    ]:
        hit = no_bare_python3(dirty, exe)
        assert hit is not None and "python3" in hit, (
            f"the check has no teeth on {dirty!r}"
        )


# ---------------------------------------- AC-2 (m-20): live endpoints split --


def test_ac_2():
    """Assertions that need the public internet run in their own named CI
    job via ONE registered ``live_network`` marker; every sandbox-deny
    assertion that does not need the internet stays mandatory in the main
    job, pinned by name.

    m-20: today a Z.ai outage reddens CI on unrelated diffs.
    """
    pyproject = _pyproject()
    ini = pyproject["tool"]["pytest"]["ini_options"]

    # One registered marker, and --strict-markers so a typo cannot
    # silently select nothing.
    markers = ini.get("markers") or []
    assert any(m.split(":")[0].strip() == "live_network" for m in markers), (
        "the live_network marker is not registered in [tool.pytest.ini_options]"
    )
    opts = ini.get("addopts", "")
    assert "--strict-markers" in opts, (
        "--strict-markers is missing: a typo'd marker name would deselect "
        "nothing and fail nothing"
    )

    ci = _ci()
    jobs = ci["jobs"]

    # The main job deselects exactly live_network — nothing else.
    test_steps = _ci_run_steps(jobs["test"])
    pytest_cmd = next((s for s in test_steps if s.strip().startswith("pytest")), "")
    assert '-m "not live_network"' in pytest_cmd, (
        "the main test job does not deselect live_network; an endpoint "
        "outage reddens every PR"
    )
    other_deselects = re.findall(r"-m\s+\"?(?!not live_network\b)[^\s\"]+", pytest_cmd)
    assert not other_deselects, (
        f"the main job's -m deselects more than live_network: {other_deselects}"
    )

    # A separately named job selects it.
    live_jobs = [
        (name, steps)
        for name, job in jobs.items()
        if name != "test"
        for steps in _ci_run_steps(job)
        if "-m live_network" in steps or "-m 'live_network'" in steps
    ]
    assert live_jobs, "no separately named CI job runs -m live_network"

    # Every marked test touches a public endpoint (grep its source).
    marked: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(_source(path))
        for func in _functions(tree):
            if _has_marker(func, "live_network"):
                segment = ast.get_source_segment(_source(path), func) or ""
                assert _public_endpoint_in(segment), (
                    f"{path.name}::{func.name} carries live_network but its "
                    "source references no public endpoint — a silent deselect "
                    "of a mandatory assertion"
                )
                marked.append(f"{path.name}::{func.name}")
    assert marked, "no test carries the live_network marker"

    # The mandatory deny tests stay in the main job: each still exists,
    # unmarked, in tests/test_f05.py.
    f05 = _source(TESTS_DIR / "test_f05.py")
    f05_tree = ast.parse(f05)
    mandatory = [
        "test_ac_3",  # the blocked-domain control (deny half of F-05 AC-3)
        "test_git_hook_and_config_planting_is_denied_in_sandbox",
        "test_planted_hook_never_fires_for_orchestrator_git",
        "test_home_reads_are_denied",
        "test_sibling_run_reads_are_denied",
        "test_settings_deny_reads_for_holdouts_ssh_gh_and_other_runs",
        "test_settings_credential_deny_covers_github_and_keychain_names",
        "test_settings_allowlist_is_model_endpoint_plus_profile_lists",
        "test_local_binding_session_denied_gate_allowed_real_srt",
    ]
    defined = {f.name for f in _functions(f05_tree)}
    for name in mandatory:
        assert name in defined, f"mandatory deny test {name} no longer exists"
        func = next(f for f in _functions(f05_tree) if f.name == name)
        assert not _has_marker(func, "live_network"), (
            f"mandatory deny test {name} was marked live_network — it must "
            "stay in the main job"
        )


# --------------------------------- AC-3 (m-21): live tests are explicit opt-in --


def _write_f06_plugin(tmp: Path, *, seat_token: str | None) -> Path:
    """A pytest plugin that (a) fakes the Z.ai credential lookup (bound
    into test_f06's namespace at collection, after configure) and (b)
    turns any attempt to start a REAL session into a loud, named error —
    so this test can prove opt-in behaviour without ever spending."""
    plugin = tmp / "f51_f06_plugin.py"
    plugin.write_text(textwrap.dedent(f"""
        import factory.runs as runs
        import factory.session as session


        def pytest_configure(config):
            session.seat_token = lambda: {seat_token!r}

            def _boom(*args, **kwargs):
                raise AssertionError("LIVE SESSION ATTEMPTED WITHOUT OPT-IN")

            runs.create_run = _boom
    """))
    return plugin


def test_ac_3(tmp_path: Path):
    """test_f06's real-session tests run ONLY with FACTORY_LIVE_TESTS=1;
    a credential never turns them on. Without the opt-in they skip with
    a reason naming the variable; with the opt-in and no credential they
    fail loudly; and no non-"1" value is an opt-in.

    m-21: today the tests infer liveness from the keychain — an org
    runner standardizing on the Z.ai gateway silently flips them to real
    spend. The create_run tripwire makes that defect observable without
    a single real session.
    """
    nodeids = ["tests/test_f06.py::test_ac_3", "tests/test_f06.py::test_ac_4"]

    # 1. Credential present, no opt-in: SKIPPED with the variable named,
    #    and no live session attempted.
    plugin = _write_f06_plugin(tmp_path, seat_token="fake-credential")
    proc = _run_pytest(nodeids, plugin=plugin)
    assert proc.returncode == 0, (
        "with a credential present and no FACTORY_LIVE_TESTS the live tests "
        f"must skip, not run:\n{proc.stdout[-2500:]}"
    )
    assert "2 skipped" in proc.stdout, proc.stdout[-800:]
    assert "FACTORY_LIVE_TESTS" in proc.stdout, (
        "the skip reason must name the opt-in variable:\n" + proc.stdout[-800:]
    )
    assert "LIVE SESSION ATTEMPTED" not in proc.stdout, (
        "a live session was attempted without the opt-in (m-21)"
    )

    # 2. Opt-in set, no credential: a LOUD failure, never a silent skip.
    plugin = _write_f06_plugin(tmp_path, seat_token=None)
    proc = _run_pytest(nodeids, plugin=plugin, env={"FACTORY_LIVE_TESTS": "1"})
    assert proc.returncode != 0, "opt-in without a credential must fail loudly"
    assert "factory-zai" in proc.stdout, (
        "the loud failure must name the missing credential:\n" + proc.stdout[-1500:]
    )

    # 3. No other value is an opt-in: 0, true, yes, " 1", empty.
    for value in ["0", "true", "yes", " 1", ""]:
        plugin = _write_f06_plugin(tmp_path, seat_token="fake-credential")
        proc = _run_pytest(nodeids, plugin=plugin, env={"FACTORY_LIVE_TESTS": value})
        assert proc.returncode == 0 and "2 skipped" in proc.stdout, (
            f"FACTORY_LIVE_TESTS={value!r} must not be an opt-in:\n"
            f"{proc.stdout[-1500:]}"
        )
        assert "LIVE SESSION ATTEMPTED" not in proc.stdout


# ------------------------------------- AC-4 (m-22): exact pins and Node floor --


def _dev_extras() -> dict[str, str]:
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    pins: dict[str, str] = {}
    for entry in dev:
        name, _, version = entry.partition("==")
        pins[name.strip()] = entry.strip()
        assert _ == "==", f"dev dependency {entry!r} is not an exact pin"
    return pins


def _node_floor() -> tuple[int, int, int]:
    """srt's engines floor, read from the committed lockfile."""
    lock = json.loads((REPO / "package-lock.json").read_text())
    engines = lock["packages"]["node_modules/@anthropic-ai/sandbox-runtime"]["engines"]["node"]
    floor = engines.lstrip(">=<^~ ")
    return tuple(int(x) for x in floor.split("."))  # type: ignore[return-value]


def _node_version_satisfies(version: str, floor: tuple[int, ...]) -> bool:
    """A concrete X.Y.Z compares directly; a bare major 'N' (or 'N.x')
    alias resolves to the LATEST release of major N, so it can never
    resolve below the floor while N >= floor's major — every release of
    that major line is >= the floor, which names a published 22.x."""
    v = version.strip().strip("'\"")
    if re.fullmatch(r"\d+", v):
        return int(v) >= floor[0]
    if re.fullmatch(r"\d+\.x", v):
        return int(v[:-2]) >= floor[0]
    parts = tuple(int(x) for x in v.split("."))
    return parts >= floor


def test_ac_4():
    """pytest and ruff carry exact pins (plus every tool this WO adds:
    pyright, build), and CI's node-version satisfies srt's >=22.12.0
    floor read from the committed lockfile — not a hard-coded number.

    m-22: loose pins let the gate drift; the '22' alias is only safe
    while it provably cannot resolve below the floor.
    """
    pins = _dev_extras()
    for tool in ["pytest", "ruff", "pytest-xdist", "pyright", "build"]:
        assert tool in pins, f"{tool} missing from the dev extra"
    for tool in ["pytest", "ruff"]:  # the gate's own tools, the letter of m-22
        assert re.fullmatch(re.escape(tool) + r"==\d+(\.\d+)*", pins[tool]), (
            f"{tool} is not exactly pinned: {pins[tool]!r}"
        )
    for tool in ["pyright", "build", "pytest-xdist"]:  # gate tools, same rule
        assert re.fullmatch(re.escape(tool) + r"==\d+(\.\d+)*", pins[tool]), (
            f"{tool} is not exactly pinned: {pins[tool]!r}"
        )
    # The runtime exact pins stay exact (the F-49 precedent).
    runtime = {
        e.partition("==")[0].strip(): e
        for e in _pyproject()["project"]["dependencies"]
        if "==" in e
    }
    for tool in ["rfc3339-validator", "claude-agent-sdk"]:
        assert re.fullmatch(re.escape(tool) + r"==\d+(\.\d+)*(\.\d+)*", runtime[tool]), (
            f"runtime pin {tool} drifted: {runtime[tool]!r}"
        )

    # node-version comes from the setup-node `with:` block, not run steps.
    test_job = _ci()["jobs"]["test"]
    node_version = None
    for step in test_job["steps"]:
        with_step = step.get("with") or {}
        if "node-version" in with_step:
            node_version = str(with_step["node-version"])
    assert node_version is not None, "the test job pins no node-version"
    floor = _node_floor()
    assert _node_version_satisfies(node_version, floor), (
        f"CI node-version {node_version!r} does not provably satisfy srt's "
        f"engines floor >= {'.'.join(map(str, floor))} from package-lock.json"
    )


# ------------------------- AC-5 (m-23): maxsplit by keyword, warnings as errors --


_RE_POSITIONAL_LIMITS = {"split": 2, "sub": 3, "subn": 3}


def _positional_count_violations() -> list[str]:
    """re.split/sub/subn calls passing count/maxsplit positionally
    (Python 3.13 deprecates them; a future Python removes them)."""
    found = []
    for path in sorted(FACTORY_DIR.glob("*.py")):
        tree = ast.parse(_source(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "re"
                and func.attr in _RE_POSITIONAL_LIMITS
                and len(node.args) > _RE_POSITIONAL_LIMITS[func.attr]
            ):
                found.append(f"{path.name}:{node.lineno} re.{func.attr}")
    return found


def test_ac_5(tmp_path: Path):
    """Every re.split/sub/subn in the package passes its count by
    keyword, and the suite turns DeprecationWarning into an error in the
    CONFIG (plain ``pytest -q``, ``-n auto`` and CI all inherit it — a
    CLI flag only CI passes does not count).

    m-23: ``re.split(pattern, rest, 1)`` — 12 warnings on 3.13, a hard
    error on a future Python; the suite must meet it first.
    """
    violations = _positional_count_violations()
    assert not violations, (
        "positional maxsplit/count in re calls (deprecated on 3.13, removed "
        f"later): {violations}"
    )

    ini = _pyproject()["tool"]["pytest"]["ini_options"]
    filters = ini.get("filterwarnings") or []
    assert any(
        f == "error::DeprecationWarning" for f in filters
    ), "filterwarnings does not turn DeprecationWarning into an error"

    # No ignore may subsume the error filter (pytest: later filters win).
    for f in filters:
        if f.startswith("ignore"):
            category = f.split("::")[-1].strip()
            assert category not in ("DeprecationWarning", "Warning"), (
                f"blanket-ish warning ignore {f!r} subsumes error::DeprecationWarning"
            )

    # Empirical: a DeprecationWarning raised inside a test is an ERROR
    # under this repo's own pytest config.
    probe = tmp_path / "test_deprecation_probe.py"
    probe.write_text(
        "import warnings\n"
        "def test_warns():\n"
        "    warnings.warn('boom', DeprecationWarning)\n"
    )
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "-c", str(PYPROJECT), str(probe)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, (
        "a DeprecationWarning in a test passed under the suite's config:\n"
        f"{proc.stdout[-1000:]}"
    )


# ------------------------------------ AC-6 (I-16): condition waits, not sleeps --


def test_ac_6():
    """No bare ``time.sleep`` call remains in test_f05.py: every parent
   -side wait polls its condition through the ONE shared wait_until
    helper (timeout failing with the condition named; immediate return
    when the predicate is already true; a raising predicate surfaces,
    never swallowed). The sleeps that remain are the SUBJECT of their
    tests — child scripts whose survival the test kills — and live
    inside the scripts' source strings, classified in the ledger.

    I-16: fixed sleeps are both slow (always pay the full sleep) and
    flaky (pay it and still lose the race).
    """
    f05 = _source(TESTS_DIR / "test_f05.py")
    tree = ast.parse(f05)
    sleep_calls = [
        f"line {n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "sleep"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "time"
    ]
    assert not sleep_calls, (
        f"bare time.sleep waits remain in test_f05.py: {sleep_calls} — every "
        "wait must poll its condition through wait_until"
    )

    # The shared helper's contract (import lands here only after the red
    # assertion above, so the red is always the defect, never a typo).
    from _support import wait_until

    with pytest.raises(AssertionError, match="the child writes its heartbeat"):
        wait_until(lambda: False, timeout=0.5, what="the child writes its heartbeat")

    started = time.monotonic()
    assert wait_until(lambda: True, timeout=10, what="already true") is True
    assert time.monotonic() - started < 5, "wait_until did not return immediately"

    def _explodes():
        raise RuntimeError("predicate failure must surface")
    with pytest.raises(RuntimeError, match="predicate failure"):
        wait_until(_explodes, timeout=5, what="a raising predicate")


# ------------------------------ AC-7 (I-17): no cross-test-module imports --


def _test_module_imports() -> list[str]:
    """Every import of another test_* module, at any depth (top-level or
    function-local)."""
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        tree = ast.parse(_source(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module in _module_names(node):
                    base = module.split(".")[0]
                    if base.startswith("test_"):
                        offenders.append(f"{path.name}:{node.lineno} ({module})")
    return offenders


def test_ac_7():
    """No test module imports another — the shared harness lives in one
    ``tests/_support`` package, byte-for-byte, and the helper itself
    imports no test module. I-17: today's cross-imports make every
    imported module's collection side effects part of every importer.
    """
    offenders = _test_module_imports()
    assert not offenders, (
        f"cross-test-module imports remain: {offenders}"
    )

    support = TESTS_DIR / "_support"
    support_files = sorted(support.glob("*.py")) if support.is_dir() else []
    assert support_files, "the shared tests/_support package does not exist"
    for path in support_files:
        tree = ast.parse(_source(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module in _module_names(node):
                    assert not module.split(".")[0].startswith("test_"), (
                        f"{path.name}:{node.lineno} imports test module {module}"
                    )


# ------------------------------------------- AC-8 (I-1): a wheel install works --


def _uses_dunder_file(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(node)
    )


# Round 2, finding 4: the srt runtime lookup is the ONE allowed
# __file__-escape, pinned to its exact assignment — anything else in
# sandbox.py (or anywhere) is flagged.
_PACKAGE_ESCAPE_ALLOWLIST = {("sandbox.py", "_REPO_ROOT")}
_RESOURCE_DIR_PREFIXES = ("schema/", "toolkit/", "prompts/", "data/",
                          "factory/", "factory/data/")


def _package_escape_violations() -> list[str]:
    """``__file__``-rooted chains that ESCAPE the package (parent.parent),
    and CWD-relative resource reads — the two ways the package only works
    from this checkout. Round 2, finding 4: the CWD scan flags ANY
    relative path-with-directories constant handed to Path()/open()
    (old prefixes AND new — `Path("factory/data/schema")` is as
    checkout-bound as `Path("schema")` was), and the sandbox escape
    allowlist is the single `_REPO_ROOT` assignment (the srt runtime
    lookup: node_modules comes from the operated repo's npm ci, never
    the wheel) — a second escape anywhere, sandbox.py included, is
    caught. Layout navigation on run dirs (run_dir.parent.parent) is
    not a resource path and is not flagged."""
    found = []
    for path in sorted(FACTORY_DIR.glob("*.py")):
        tree = ast.parse(_source(path))
        # The allowlisted top-level assignments for THIS module: the nodes
        # an escape chain inside is excused (sandbox's _REPO_ROOT, the srt
        # runtime lookup).
        allowed_nodes = [
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and (path.name, t.id) in _PACKAGE_ESCAPE_ALLOWLIST
                for t in node.targets
            )
        ]

        def _excused(chain: ast.AST) -> bool:
            return any(
                chain in ast.walk(allowed) for allowed in allowed_nodes
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "parent":
                inner = node.value
                if (
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "parent"
                    and _uses_dunder_file(inner)
                    and not _excused(inner)
                ):
                    found.append(
                        f"{path.name}:{node.lineno} __file__ parent.parent escape"
                    )
            if isinstance(node, ast.Call):
                callee = node.func
                callee_name = (
                    callee.id if isinstance(callee, ast.Name)
                    else callee.attr if isinstance(callee, ast.Attribute)
                    else ""
                )
                if callee_name not in ("Path", "open"):
                    continue
                for arg in node.args[:1]:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and "/" in arg.value
                        and not arg.value.startswith(("/", "~"))
                        and arg.value.split("/", 1)[0] in
                        ("schema", "toolkit", "prompts", "data", "factory")
                    ):
                        found.append(
                            f"{path.name}:{node.lineno} CWD-relative {arg.value!r}"
                        )
    return found


def _resource_files() -> list[str]:
    """Every file under the package's shipped data trees, package-relative."""
    files: set[str] = set()
    for sub in ("data", "dashboard_static"):
        root = FACTORY_DIR / sub
        if root.is_dir():
            files.update(
                str(p.relative_to(FACTORY_DIR))
                for p in root.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            )
    return sorted(files)


def test_ac_8(tmp_path: Path):
    """The schema files, the vetted toolkit and the seat prompts ship
    INSIDE the package (``factory/data/``), read through
    importlib.resources — never a ``parent.parent`` escape or a
    CWD-relative path; the wheel carries every resource file; and the
    protected/risk globs followed the files, so a move never unprotects
    them. I-1: wheel installs break today — only editable installs work.
    """
    # The resources exist inside the package.
    for rel in [
        "factory/data/schema/tasks.schema.json",
        "factory/data/schema/project.schema.json",
        "factory/data/schema/toolkit.schema.json",
        "factory/data/toolkit/index.yaml",
        "factory/data/prompts/reviewer.md",
        "factory/data/prompts/implementer.md",
        "factory/data/prompts/verifier.md",
    ]:
        assert (REPO / rel).is_file(), f"resource {rel} does not exist in the package"

    # No package-escape or CWD-relative resource reads remain.
    violations = _package_escape_violations()
    assert not violations, f"resource paths that break a wheel install: {violations}"

    # package-data covers EVERY resource file (glob compare).
    patterns = _pyproject()["tool"]["setuptools"]["package-data"]["factory"]
    covered: set[str] = set()
    for pattern in patterns:
        covered.update(
            str(Path(p).relative_to(FACTORY_DIR))
            for p in globmod.glob(str(FACTORY_DIR / pattern), recursive=True)
            if Path(p).is_file()
        )
    resources = set(_resource_files())
    assert resources, "no resource files found under factory/data"
    assert resources <= covered, (
        f"resource files not covered by package-data patterns: "
        f"{sorted(resources - covered)}"
    )

    # The protected and risk globs followed the files.
    profile = yaml.safe_load(PROJECT_YAML.read_text())
    protected = profile["paths"]["protected"]
    risk_factory_control = profile["paths"]["risk"]["factory_control"]
    from factory.gates import matches

    samples = [
        "factory/data/schema/tasks.schema.json",
        "factory/data/schema/project.schema.json",
        "factory/data/toolkit/index.yaml",
        "factory/data/toolkit/skills/demo-skill/run.sh",
        "factory/data/prompts/reviewer.md",
        "factory/data/prompts/vendor/some-skill/LICENSE",
    ]
    for sample in samples:
        assert matches(sample, protected), (
            f"{sample} matches no paths.protected glob — the move unprotected it"
        )
    for sample in samples[:2]:
        assert matches(sample, risk_factory_control), (
            f"{sample} matches no factory_control risk glob — the move "
            "de-risked the queue's own schema"
        )

    # The wheel carries every resource file (built with the pinned build,
    # offline-safe via --no-isolation) — in an EXPORT of the tracked tree,
    # so the build never writes into the real checkout (round 2, 2c) and
    # a stale build/ can never leak deleted modules into the wheel.
    export = _export_working_tree(tmp_path / "export")
    outdir = tmp_path / "wheel"
    outdir.mkdir()
    proc = subprocess.run(
        [str(VENV_PY), "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(outdir), "."],
        cwd=export, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, f"wheel build failed:\n{proc.stdout[-1500:]}"
    (wheel,) = outdir.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for rel in _resource_files():
        assert f"factory/{rel}" in names, f"{rel} missing from the built wheel"
    assert not (REPO / "build").exists() and not (REPO / "dist").exists(), (
        "test_ac_8's build wrote into the real checkout"
    )

    # THE mandatory CWD/escape proof (round 2, finding 4): a real pip
    # install of the built wheel, HERMETICALLY (--no-deps --no-index — no
    # PyPI, so this stays in the main job) into a --target dir, then load
    # the real contract through it from OUTSIDE the tree with the wheel
    # FIRST on the path (PYTHONPATH precedes site-packages, so the
    # installed wheel's factory must win over the suite's editable
    # install; the runtime deps come from the suite environment). A
    # nested fresh venv would need PyPI for the deps — that fuller
    # install is the live-network wheel job's own test. PYTHONPATH is
    # stripped from the INSTALL step: with the checkout visible, pip
    # sees the editable *.egg-info and answers "already installed",
    # silently installing nothing (caught red on this leg's first run).
    target = tmp_path / "sitepkgs"
    subprocess.run(
        [str(VENV_PY), "-m", "pip", "install", "--no-deps", "--no-index",
         "--target", str(target), str(wheel)],
        check=True, capture_output=True, timeout=300,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = subprocess.run(
        [str(VENV_PY), "-c", "\n".join([
            "import pathlib",
            "import factory.contract as contract",
            "import factory.prompts as prompts",
            "import factory.toolkit as toolkit",
            f"repo = pathlib.Path({str(export)!r})",
            "tasks = contract.load_tasks(repo / '.factory' / 'tasks.json')",
            "profile = contract.load_profile(repo / '.factory' / 'project.yaml')",
            "assert tasks and profile",
            "index = toolkit.load_index(toolkit.default_toolkit_dir())",
            "assert index.get('items') is not None",
            "assert prompts.load_prompt(prompts.REVIEWER_FILE)",
            "print(contract.__file__)",
        ])],
        cwd=outside, capture_output=True, text=True, timeout=300,
        env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
             "PYTHONPATH": str(target)},
    )
    assert proc.returncode == 0, (
        f"hermetic wheel install could not load the contract from outside "
        f"the tree:\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    )
    location = proc.stdout.strip().splitlines()[-1]
    assert location.startswith(str(target)), (
        f"factory resolved to {location} — not the freshly installed wheel "
        f"under {target} (the tree or the editable install is shadowing it)"
    )


def _export_working_tree(dest: Path) -> Path:
    """A copy of the WORKING tree in tmp_path. Round 3, minor 3 + round 4,
    finding 2: the listing is `git ls-files -z --cached --others
    --exclude-standard` (NUL-delimited: core.quotePath would C-quote
    non-ASCII names and the old not-exists skip would silently drop them
    from the export), and the ONLY silent skip is exactly the set git
    itself reports deleted (`git ls-files --deleted -z`) — any other
    missing path is LOUD (a quoted name or an unexpected miss must never
    quietly shrink the wheel). Symlinks copy AS symlinks; the build runs
    here, so nothing writes build/ or dist/ into the real checkout
    (round 2, 2c)."""
    def _ls_files(*args: str) -> list[str]:
        proc = subprocess.run(
            ["git", "ls-files", "-z", *args],
            cwd=REPO, capture_output=True, text=True, check=True,
        )
        return [name for name in proc.stdout.split("\0") if name]

    listing = _ls_files("--cached", "--others", "--exclude-standard")
    assert listing, "git ls-files returned nothing"
    deleted = set(_ls_files("--deleted"))
    copied = 0
    for rel in listing:
        source = REPO / rel
        if rel in deleted:
            continue  # exactly the paths git reports deleted
        assert source.exists() or source.is_symlink(), (
            f"{rel} is neither on disk nor reported deleted — refusing to "
            "silently shrink the export (round 4, finding 2)"
        )
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        copied += 1
    assert copied, "the export copied nothing"
    return dest


@pytest.mark.live_network
def test_review_the_ci_wheel_job_steps_actually_run(tmp_path: Path):
    """Round 1 finding 2 + round 2 findings 1–2 + round 3 critical 1: the
    shipped CI job is the artifact under test. This EXECUTES the wheel
    job's run steps extracted from ci.yml — in order, with the job's own
    shell text — through Actions' own step shell (``bash --noprofile
    --norc -eo pipefail -c``), against an exported copy of the working
    tree (cwd + GITHUB_WORKSPACE — the real checkout is never written),
    with the job's ``python`` a THROWAWAY venv whose OWN bin dir is first
    on PATH — no symlink indirection (round 3, critical 1: through a
    symlink, python finds no pyvenv.cfg and runs as the BASE interpreter
    — PEP 668 red on Linux, and on macOS it installed build/setuptools
    into the owner's global pyenv). A hard TRIPWIRE asserts, through the
    same shell and PATH BEFORE any pip runs, that ``python`` is that
    exact venv; and the base interpreter's dist-info listing is asserted
    unchanged after the whole job. --system-site-packages dropped: a
    fresh venv bootstraps its own pip, and the wheel's deps come from
    PyPI in this live-network leg. Marked live_network, honestly: the
    job's fresh-venv ``pip install dist/*.whl`` resolves the runtime
    deps from the index — the real wheel job runs every PR regardless;
    the mandatory no-network guarantees live in test_ac_8."""
    wheel_job = _ci()["jobs"]["wheel"]
    steps = [s["run"] for s in wheel_job["steps"] if "run" in s]
    assert len(steps) == 4, f"expected the 4 known run steps, got {len(steps)}"

    export = _export_working_tree(tmp_path / "export")
    # The job's `python`: a throwaway venv, addressed by its OWN bin dir
    # first on PATH (never a symlink — venv detection follows sys.executable).
    job_python_env = tmp_path / "jobpython"
    subprocess.run(
        [str(VENV_PY), "-m", "venv", str(job_python_env)],
        check=True, capture_output=True,
    )
    scratch_home = tmp_path / "home"
    scratch_home.mkdir()
    wheelenv = tmp_path / "wheelenv"

    env = dict(os.environ)
    env.update({
        "PATH": f"{job_python_env / 'bin'}{os.pathsep}{env['PATH']}",
        "HOME": str(scratch_home),
        "GITHUB_WORKSPACE": str(export),
        "PYTHONPATH": "",  # the job's python must not see this checkout
    })
    env.pop("VIRTUAL_ENV", None)

    def _run_step(text: str):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", text],
            cwd=export, env=env, capture_output=True, text=True, timeout=900,
        )

    # TRIPWIRE (round 3, critical 1): through the exact shell/PATH the
    # job steps use, `python` must BE the throwaway venv — before any
    # pip runs, so the test can never install into anything else.
    tripwire = _run_step(
        "python -c \"import sys; "
        f"assert sys.prefix != sys.base_prefix, sys.prefix; "
        f"assert sys.prefix == {str(job_python_env.resolve())!r}, sys.prefix\""
    )
    assert tripwire.returncode == 0, (
        f"the job's `python` is not the throwaway venv — refusing to run "
        f"any pip step:\n{tripwire.stdout[-400:]}\n{tripwire.stderr[-400:]}"
    )

    # Snapshot the BASE interpreter's installed distributions: the whole
    # job must leave them byte-identical (the round-3 escape replaced
    # setuptools in the owner's global pyenv). Round 4, finding 1 +
    # round 5: the site dirs are the BASE interpreter's REAL ones, asked
    # of the base executable itself (sys._base_executable, printed by
    # the job venv's python, then `site.getsitepackages()`). Round 4's
    # sysconfig shape was VACUOUS on macOS/pyenv (a nonexistent dir);
    # round 5 found sysconfig is ALSO wrong on Debian/Ubuntu — its
    # posix_prefix purelib (/usr/lib/python3.12/site-packages) does not
    # exist, because Debian installs into dist-packages — so the
    # existence guard reddened the live job on any Debian-based host.
    # getsitepackages() reports reality on both (pyenv:
    # …/3.11.9/lib/python3.11/site-packages, 239 dists; the reviewer's
    # container: the existing /usr/lib/python3/dist-packages + the
    # /usr/local dist-packages, missing entries filtered). Guards stay
    # loud: ≥1 existing dir, and a NON-EMPTY snapshot (the base always
    # carries pip's dist-info), so a wrong path is red, never silently
    # empty.
    base_exec_probe = subprocess.run(
        [str(job_python_env / "bin" / "python"), "-c",
         "import sys; print(sys._base_executable)"],
        capture_output=True, text=True, check=True,
    )
    base_executable = base_exec_probe.stdout.strip()
    assert base_executable, "could not identify the base interpreter"
    base_dirs_probe = subprocess.run(
        [base_executable, "-c", "import site; print(site.getsitepackages())"],
        capture_output=True, text=True, check=True,
    )
    base_sites = []
    for raw in ast.literal_eval(base_dirs_probe.stdout.strip()):
        site_dir = Path(raw)
        if site_dir.is_dir() and site_dir not in base_sites:
            base_sites.append(site_dir)  # nonexistent entries are FILTERED
    assert base_sites, (
        f"no base site dir exists among {base_dirs_probe.stdout.strip()} — "
        "the snapshot cannot prove anything (rounds 4–5)"
    )

    def _base_snapshot() -> list:
        entries = []
        for site_dir in base_sites:
            entries.extend(
                f"{site_dir}:{p.name}" for p in site_dir.glob("*.dist-info")
            )
        return sorted(entries)

    base_before = _base_snapshot()
    assert base_before, (
        "the base interpreter snapshot is EMPTY — pip's own dist-info is "
        "always present; a wrong path must be red, not silently green"
    )

    for index, step in enumerate(steps):
        text = step.replace("/tmp/wheelenv", str(wheelenv))
        proc = _run_step(text)
        assert proc.returncode == 0, (
            f"wheel-job run step {index + 1} failed as written in ci.yml"
            f" (path-substituted scratch only):\n--- step ---\n{text}\n"
            f"--- stdout ---\n{proc.stdout[-800:]}\n--- stderr ---\n{proc.stderr[-800:]}"
        )

    # The real checkout and the base interpreter were never written.
    assert not (REPO / "build").exists() and not (REPO / "dist").exists(), (
        "the wheel-job execution wrote into the real checkout"
    )
    assert _base_snapshot() == base_before, (
        "the wheel job changed the BASE interpreter's site-packages "
        f"(before {len(base_before)} / after {len(_base_snapshot())} entries)"
    )


# ------------------------------------------- AC-9 (A-7): pyright basic is clean --

# Exact (F-51/A-7): every suppression is narrow, rule-named and carries a
# reason at its site. A new one must change this number deliberately.
# 1. l0.py configparser.optionxform — the documented stdlib idiom; the
#    stubs type the slot as a method.
# 2. session.py options.plugins — the SDK accepts plain local-plugin
#    dicts at runtime; SdkPluginConfig doesn't model the shape.
# 3. session.py options.mcp_servers — dict|str|Path in the SDK; this
#    branch only ever assigns a mapping.
# 4. dashboard.py server.dashboard — http.server stubs don't know the
#    attached attribute (converted from a mypy-style ignore by F-51).
PYRIGHT_IGNORE_ALLOWLIST = 4


# Round 1, finding 4a: an exact allowlist of [tool.pyright] keys — any
# other key (a report* override softened to "warning"/"none", ignore,
# exclude, strict, define...) is a config neuter pyright would exit 0 on.
PYRIGHT_CONFIG_KEYS = {"typeCheckingMode", "include"}


def test_ac_9():
    """pyright runs in basic mode over ``factory`` in CI, is clean, and
    no rule is switched off globally and no file-level disable exists;
    inline suppressions are narrow, rule-named, and counted against an
    exact allowlist. A-7: nothing type-checks the factory today. Round 1,
    finding 4a: the config keys are an exact allowlist (a
    ``reportX = "warning"`` neuter passed the old check), the suppression
    scan covers EVERY .py under factory/ (the data/ fixtures are inside
    pyright's include), and CI runs ``pyright --warnings`` (bare pyright
    exits 0 on warnings).
    """
    pyproject = _pyproject()
    config = pyproject.get("tool", {}).get("pyright")
    assert config, "[tool.pyright] is not configured"
    assert config.get("typeCheckingMode") == "basic", (
        "pyright must run in basic mode (not standard/strict, not off)"
    )
    assert "factory" in (config.get("include") or []), (
        "pyright's include must cover the factory package"
    )

    # Exact key allowlist: no report* override of ANY value, no ignore/
    # exclude/strict/… — nothing that softens, narrows or re-modes the
    # check can ride into the config.
    extra = set(config) - PYRIGHT_CONFIG_KEYS
    assert not extra, (
        f"[tool.pyright] carries keys outside the exact allowlist "
        f"{sorted(PYRIGHT_CONFIG_KEYS)}: {sorted(extra)} — a config neuter"
    )

    # No file-level pyright directive, no bare `# type: ignore`, and the
    # inline suppression count matches the allowlist exactly — over EVERY
    # .py under factory/ (rglob: the data/ fixtures are in include too).
    suppressions = 0
    for path in sorted(FACTORY_DIR.rglob("*.py")):
        for lineno, line in enumerate(_source(path).splitlines(), 1):
            stripped = line.strip()
            if re.match(r"^#\s*pyright:", stripped):
                raise AssertionError(
                    f"{path.relative_to(REPO)}:{lineno} file-level pyright directive"
                )
            if "# type: ignore" in line:
                raise AssertionError(
                    f"{path.relative_to(REPO)}:{lineno} bare `# type: ignore` — use a narrow, "
                    "rule-named `# pyright: ignore[rule]` with a reason"
                )
            if "# pyright: ignore" in line:
                suppressions += 1
                assert re.search(r"# pyright: ignore\[[\w]+\]", line), (
                    f"{path.relative_to(REPO)}:{lineno} suppression must be rule-named"
                )
    assert suppressions == PYRIGHT_IGNORE_ALLOWLIST, (
        f"{suppressions} pyright suppressions vs allowlist "
        f"{PYRIGHT_IGNORE_ALLOWLIST} — update the allowlist deliberately"
    )

    # CI runs it, warnings fatal (round 1, 4a: `echo pyright` or
    # `pyright || true` passed the old any-substring check).
    all_steps = [s for job in _ci()["jobs"].values() for s in _ci_run_steps(job)]
    assert "pyright --warnings" in all_steps, (
        "the typecheck job must run exactly `pyright --warnings` — bare "
        "pyright exits 0 on warnings, and anything else may not run it at all"
    )


# ------------------------------------------------------------------- edges --


def test_review_every_non_draft_acceptance_ref_is_collected():
    """F-49's ledger routing 3a, built here per its text ("a queue-wide
    collection check belongs in F-51's test/CI hygiene"): every
    acceptance ref on every non-draft task must be a test pytest actually
    COLLECTS — static resolution (F-49) proves the function; this proves
    the collection — and must not be skip/xfail-marked at the function
    or module level (a permanently-skipped contract test is a green gate
    that checks nothing)."""
    from factory.contract import load_tasks

    doc = load_tasks(REPO / ".factory" / "tasks.json")
    refs: list[str] = []
    for task in doc["tasks"]:
        if task.get("status") == "draft":
            continue
        for item in task.get("acceptance") or []:
            check = item.get("check") or {}
            if check.get("type") == "test" and "::" in (check.get("ref") or ""):
                refs.append(check["ref"])
    assert refs, "no non-draft test refs found — the loader or queue changed shape"

    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode in (0, 5), proc.stderr[-500:]
    collected = {
        line.split("::", 1)[0] + "::" + line.split("::", 1)[1].split("[", 1)[0]
        for line in proc.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }
    uncollected = [ref for ref in refs if ref not in collected]
    assert not uncollected, (
        f"acceptance refs pytest never collects (a green gate checking "
        f"nothing): {uncollected}"
    )

    # Not skip/xfail-marked (function decorators or module pytestmark).
    import ast as _ast

    files = {ref.split("::")[0] for ref in refs}
    for rel in sorted(files):
        tree = _ast.parse(_source(REPO / rel))
        module_skipped = False
        for node in tree.body:
            if isinstance(node, _ast.Assign) and any(
                isinstance(t, _ast.Name) and t.id == "pytestmark" for t in node.targets
            ):
                if any(
                    isinstance(n, _ast.Attribute)
                    and n.attr in ("skip", "skipif", "xfail")
                    for n in _ast.walk(node.value)
                ):
                    module_skipped = True
        marked = []
        for func in _functions(tree):
            ref_name = f"{rel}::{func.name}"
            if ref_name not in refs:
                continue
            if module_skipped or any(
                isinstance(n, _ast.Attribute)
                and n.attr in ("skip", "skipif", "xfail")
                and isinstance(n.value, _ast.Attribute)
                and n.value.attr == "mark"
                for d in func.decorator_list
                for n in _ast.walk(d)
            ):
                marked.append(ref_name)
        assert not marked, f"acceptance tests skipped/xfail-marked: {marked}"


def test_review_the_new_ci_jobs_are_pinned_exactly():
    """Round 1, finding 4b: the three new jobs cannot be silently
    neutered. Each must exist, run on the pinned host, carry NO
    job-level ``if``/``continue-on-error``, no step-level
    ``if``/``continue-on-error``, and run its exact command — an
    ``echo pyright``, a ``pyright || true`` or an ``if: false`` job all
    fail here. (f24's guard covers the main test job's steps only.)"""
    jobs = _ci()["jobs"]
    pins = {
        "live-network": ["pytest -q -rs -m live_network"],
        "typecheck": ["pyright --warnings"],
        # the wheel job's exact steps are EXECUTED by
        # test_review_the_ci_wheel_job_steps_actually_run; here: shape.
        "wheel": ["python -m build --wheel --no-isolation"],
    }
    for name, required_commands in pins.items():
        assert name in jobs, f"the {name} job is gone"
        job = jobs[name]
        assert job.get("runs-on") == "ubuntu-24.04", (
            f"{name}: runs-on must stay the pinned ubuntu-24.04"
        )
        for banned in ("if", "continue-on-error"):
            assert banned not in job, (
                f"{name}: job-level {banned} would let it fail silently"
            )
        run_steps = []
        for step in job["steps"]:
            for banned in ("if", "continue-on-error"):
                assert banned not in step, (
                    f"{name}: step-level {banned} would let it fail silently"
                )
            if "run" in step:
                run_steps.append(step["run"])
        for command in required_commands:
            assert command in run_steps, (
                f"{name}: the exact command {command!r} is not a run step"
            )


def test_review_the_conftest_spend_guard_is_pinned(tmp_path: Path):
    """Round 2 finding 3 + round 3 major 2: the suite-wide no-spend guard
    must be proven, not assumed — WITHOUT writing anything into the real
    tests/ dir (the round-2 shape raced test_ac_7's scanner and a killed
    run could leave a stray test file for git add -A). The probe lives in
    tmp_path; the REAL tests/conftest.py is loaded as a plugin
    (``-p conftest`` with tests/ on PYTHONPATH — its autouse guard then
    governs the probe exactly as it governs the suite). With a
    plugin-faked keychain AND ambient ANTHROPIC_AUTH_TOKEN/BASE_URL: no
    opt-in → seat_token() is None (probe passes); FACTORY_LIVE_TESTS=1 →
    the credential flows (probe fails with the fake visible). The tests/
    dir is asserted byte-identical before and after. Mutation-proven:
    the guard disabled → the no-opt-in leg goes red."""
    def _tests_dir_snapshot() -> list:
        return sorted(str(p.relative_to(TESTS_DIR)) for p in TESTS_DIR.iterdir())

    before = _tests_dir_snapshot()
    scratch = tmp_path / "probe"
    scratch.mkdir()
    plugin = scratch / "f51_guard_plugin.py"
    plugin.write_text(textwrap.dedent("""
        import factory.session as session

        def pytest_configure(config):
            session._keychain_token = lambda: "FAKE-KEYCHAIN-TOKEN"
    """))
    probe = scratch / "test_probe.py"
    probe.write_text(
        "def test_probe():\n"
        "    import factory.session as session\n"
        "    token = session.seat_token()\n"
        "    print(f'PROBE_TOKEN={token!r}')\n"
        "    assert token is None, f'guard failed: {token!r}'\n"
    )

    def _run(extra_env: dict[str, str]):
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_LIVE_TESTS"}
        env.update({
            # tests/ on the path so `-p conftest` loads the REAL guard;
            # REPO so factory and _support import from the checkout.
            "PYTHONPATH": f"{scratch}{os.pathsep}{TESTS_DIR}{os.pathsep}{REPO}",
            "ANTHROPIC_AUTH_TOKEN": "AMBIENT-TOKEN",
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            **extra_env,
        })
        return subprocess.run(
            [str(VENV_PY), "-m", "pytest", "-q", "--no-header",
             "--rootdir", str(scratch),
             "-p", "no:cacheprovider", "-p", "conftest",
             "-p", "f51_guard_plugin", "test_probe.py"],
            cwd=scratch, env=env, capture_output=True, text=True, timeout=300,
        )

    proc = _run({})  # no opt-in: guard must stub keychain AND strip env
    assert proc.returncode == 0 and "guard failed" not in proc.stdout, (
        f"the conftest spend guard did not neutralise a faked keychain "
        f"+ ambient token:\n{proc.stdout[-600:]}"
    )

    # With the opt-in the guard stands down and the credential flows —
    # the probe's None-assert fails with the fake visible.
    proc = _run({"FACTORY_LIVE_TESTS": "1"})
    assert proc.returncode != 0 and "FAKE-KEYCHAIN-TOKEN" in proc.stdout, (
        "with FACTORY_LIVE_TESTS=1 the guard should stand down and let "
        f"the credential flow:\n{proc.stdout[-600:]}"
    )

    # The real tests/ dir is byte-identical (round 3, major 2).
    assert _tests_dir_snapshot() == before, (
        "the guard-pin test wrote into (or removed from) the real tests/ dir"
    )


def test_review_live_opt_in_value_is_exactly_one(monkeypatch):
    """Only the exact string "1" opts in — 0/true/yes/empty/whitespace
    are not opt-ins (AC-3's value semantics, unit-level)."""
    from _support import live_tests_enabled

    monkeypatch.delenv("FACTORY_LIVE_TESTS", raising=False)
    assert live_tests_enabled() is False
    for value in ["0", "true", "yes", "", " 1", "1 ", "01"]:
        monkeypatch.setenv("FACTORY_LIVE_TESTS", value)
        assert live_tests_enabled() is False, value
    monkeypatch.setenv("FACTORY_LIVE_TESTS", "1")
    assert live_tests_enabled() is True


def test_review_no_bare_python3_excludes_only_the_exact_quoted_path():
    """Only the exact interpreter path (quoted or bare) is removed; every
    OTHER ``python3`` — another absolute path, a relative one, a suffixed
    binary — is the bug (not the factory's venv) and must trip (round 1,
    major 3: the word-boundary shape let all three through)."""
    from _support import no_bare_python3

    exe = "/opt/conda/envs/x/bin/python3"
    quoted = f"run '{exe}' -c check"  # the shlex-quoted form the prompt embeds
    assert no_bare_python3(quoted, exe) is None
    for must_trip in [
        "run /usr/bin/python3 -c check",
        "run ./python3 -c check",
        "run python3.11 -c check",
        "run python3 -c check",
    ]:
        assert no_bare_python3(must_trip, exe) is not None, must_trip
