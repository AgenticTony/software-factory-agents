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
    ensure_srt,
    run_sandboxed,
    sandbox_env,
    srt_bin,
    write_settings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_ENDPOINT = "api.z.ai"


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
    assert outcomes["outside_run"].startswith("denied"), (
        "write outside the run dir but inside the factory root succeeded\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert outcomes["outside_tmp"].startswith("denied"), (
        f"write to /tmp succeeded\n{proc.stdout}{proc.stderr}"
    )
    assert not outside_run.exists() and not outside_tmp.exists()


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


# --- F-05 AC-4: no GitHub/keychain credentials inside the sandbox ----------


def test_ac_4(tmp_path: Path):
    run, _, settings = _make_run(tmp_path)
    keychain_derived = "JFO_PROBE_PASS"
    names = ["GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", keychain_derived]
    script = textwrap.dedent(
        """
        import os, sys
        for name in sys.argv[1:]:
            value = os.environ.get(name)
            print(f"{name}={'<set:' + value + '>' if value is not None else 'absent'}", flush=True)
        print(f"API_TIMEOUT_MS={os.environ.get('API_TIMEOUT_MS')}", flush=True)
        """
    )
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
        _child(script, names), run.root, run.settings_path, env=env, timeout=60
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
    probe = Path("~/.factory-sandbox-probe").expanduser()
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
    script = textwrap.dedent(
        """
        import os, sys
        for name in sys.argv[1:]:
            value = os.environ.get(name)
            print(f"{name}={value if value is not None else 'absent'}", flush=True)
        """
    )
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


def test_toolchain_under_home_is_executable(tmp_path: Path):
    """F-06 runs the Claude Code CLI, which may live under $HOME
    (~/.local/bin/claude, pyenv, uv, nvm). The home-wide denyRead must not
    make the session's own toolchain unexecutable: build_settings re-allows
    the resolved toolchain paths (binary dir, its sibling lib/ when the
    dir is a bin/, and the literal symlinks on the way — never ancestors)."""
    home = Path.home()
    tool_root = home / ".factory-sandbox-toolchain-test"
    tool_bin = tool_root / "bin"
    tool_bin.mkdir(parents=True, exist_ok=True)
    try:
        claude = tool_bin / "claude"
        claude.write_text("#!/bin/sh\necho 'claude 2.0.0 (toolchain ok)'\n")
        claude.chmod(0o755)
        run, _, _ = _make_run(tmp_path)
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
    finally:
        shutil.rmtree(tool_root, ignore_errors=True)


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
    mechanism is gone; tests inject via the srt_bin_path parameter."""
    from factory.sandbox import srt_bin as srt_bin_fn

    monkeypatch.setenv("FACTORY_SRT_BIN", "/tmp/definitely-not-srt")
    assert srt_bin_fn() == REPO_ROOT / "node_modules" / ".bin" / "srt"


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
    cursor-agent, Copilot shims and friends live there."""
    home = Path.home()
    base = home / ".factory-sandbox-tc-r4"
    claude = base / "share" / "claude" / "versions" / "X" / "claude"
    assert _toolchain_allowread(home, [claude]) == {
        base / "share" / "claude" / "versions" / "X"
    }
    python = base / "pyenv" / "versions" / "3.12" / "bin" / "python"
    assert _toolchain_allowread(home, [python]) == {
        base / "pyenv" / "versions" / "3.12" / "bin",
        base / "pyenv" / "versions" / "3.12" / "lib",  # stdlib + site-packages
    }


def test_toolchain_bin_dir_under_home_still_executes(tmp_path: Path):
    """The narrowed toolchain reads must still let a binary under $HOME run:
    the round-3 exec fix has to survive its own scope reduction."""
    home = Path.home()
    tool_root = home / ".factory-sandbox-toolchain-r4"
    tool_bin = tool_root / "bin"
    tool_bin.mkdir(parents=True, exist_ok=True)
    try:
        probe = tool_bin / "probe"
        probe.write_text("#!/bin/sh\necho 'r4 toolchain ok'\n")
        probe.chmod(0o755)
        run, _, _ = _make_run(tmp_path)
        settings = build_settings({}, run.root, model_endpoint=MODEL_ENDPOINT,
                                  toolchain=[probe])
        write_settings(settings, run.settings_path)
        proc = run_sandboxed([str(probe)], run.root, run.settings_path, timeout=60)
        assert "r4 toolchain ok" in proc.stdout, (
            f"rc={proc.returncode}\n{proc.stdout}{proc.stderr}"
        )
    finally:
        shutil.rmtree(tool_root, ignore_errors=True)


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


def test_toolchain_symlink_under_home_executes(tmp_path: Path):
    """The native Claude Code install is a symlink under $HOME
    (~/.local/bin/claude -> ~/.local/share/claude/versions/X). Exec through
    the link — directly and via a PATH lookup — must work, not just through
    the resolved path: F-06 finds the CLI by name."""
    home = Path.home()
    base = home / ".factory-sandbox-tc-r5"
    link_dir = base / "bin"
    real_dir = base / "share" / "tool" / "versions" / "1.0"
    real_dir.mkdir(parents=True, exist_ok=True)
    link_dir.mkdir(parents=True, exist_ok=True)
    try:
        real = real_dir / "r5tool"
        real.write_text("#!/bin/sh\necho 'r5 symlink ok'\n")
        real.chmod(0o755)
        link = link_dir / "r5tool"
        link.symlink_to(Path("..") / "share" / "tool" / "versions" / "1.0" / "r5tool")
        run, _, _ = _make_run(tmp_path)
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
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_bin_dir_parent_never_reopens_a_shared_prefix():
    """A binary living in ~/.local/bin must not re-allow all of ~/.local
    (uv credentials, cursor-agent, ... under ~/.local/share). For a bin/ dir
    only its sibling lib/ (where an interpreter's stdlib and site-packages
    live) joins the reads, never the whole parent."""
    home = Path.home()
    reads = _toolchain_allowread(home, [home / ".local" / "bin" / "sometool"])
    assert home / ".local" not in reads, reads
    assert reads == {home / ".local" / "bin", home / ".local" / "lib"}


def test_run_sandboxed_docstring_matches_the_select_pump():
    """The teardown is a single-threaded select loop; the docstring must not
    describe the abandoned reader-thread design."""
    doc = run_sandboxed.__doc__ or ""
    assert "reader thread" not in doc.lower(), "docstring still describes reader threads"
    assert "select" in doc


# --- review follow-ups (fresh-context review of d898f10) ------------------


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
