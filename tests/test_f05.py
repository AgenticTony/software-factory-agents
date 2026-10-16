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

import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from factory.runs import RunError, cleanup_run, create_run
from factory.sandbox import (
    SRT_PIN,
    SandboxError,
    build_settings,
    ensure_srt,
    run_sandboxed,
    sandbox_env,
    srt_bin,
    write_settings,
)

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


def test_missing_srt_is_a_failure_not_a_skip(tmp_path: Path, monkeypatch):
    """A broken install fails loudly; there is no skip path (owner rule)."""
    monkeypatch.setenv("FACTORY_SRT_BIN", str(tmp_path / "missing-srt"))
    run, _, settings = _make_run(tmp_path)
    with pytest.raises(SandboxError, match="FACTORY_SRT_BIN"):
        run_sandboxed(["true"], run.root, run.settings_path)
    assert not (run.root / "inside.txt").exists()


# --- settings generation from the profile -----------------------------------


def test_settings_allowlist_is_model_endpoint_plus_profile_lists(tmp_path: Path):
    run, _, settings = _make_run(tmp_path)
    net = settings["network"]
    assert net["allowedDomains"] == [MODEL_ENDPOINT, "docs.python.org", "pypi.org"]
    assert net["deniedDomains"] == []
    fs = settings["filesystem"]
    assert fs["allowWrite"] == [str(run.root)]
    assert fs["denyWrite"] == []
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
