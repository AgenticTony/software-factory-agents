"""F-62 — Host-boundary escapes (security audit run-2, 2026-11-09).

Three confirmed highs share one shape — a lower-trust file or string
reaches a trusted host action:

* AC-1: the srt launcher resolves its interpreter through the confined
  child's PATH, whose first entry (``<run>/.venv/bin``) the confined
  child can write — a gate command plants a ``node`` there and the next
  launch runs it unsandboxed as the owner.
* AC-2: the pushed tree and the branch's commit messages are never
  scanned for the literal seat token — the PR-text fence covers title
  and body only, and no shape matches the token's bare form.
* AC-3: the queue's ``project`` string reaches every factory-root path
  builder unvalidated — a client repo's tasks.json places repair-path
  artifacts outside the per-project namespace, outside the factory
  root, and turns ``factory status`` into an arbitrary-directory read.

Red-first: every ``test_ac_N`` below failed on the untouched tree
(at af67f18) in the way its finding describes — the AC-1 marker
appeared, AC-2 had no fence to call, and AC-3's builders accepted the
escapes the audit observed.
"""

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _support.f05 import _fixture_repo

from factory.publish import PushRefused, push_branch
from factory.runs import create_run
from factory.sandbox import (
    SRT_PIN,
    SandboxError,
    build_settings,
    write_settings,
)
from factory.session import SessionRecord
from factory.state import TaskQueue
from factory.trace import trace_path

# A Z.ai-shaped DUMMY seat token: <16 hex>.<24 base64url-ish>. The audit
# proved this exact shape matches no credential shape in any bare
# rendering (that is the finding), so nothing upstream refuses it before
# the fence under test. Obviously fake; never a real credential.
FAKE_TOKEN = "0123456789abcdef.ZhVvEySkTiRaQwQzu1"
# A second dummy with characters URL-encoding actually transforms
# (+ / =), for the encoded-form tests.
FAKE_TOKEN_URLISH = "f62.tok+en/9=8"

MARKER_NAME = "escaped-marker.txt"


# ---------------------------------------------------------------- helpers --


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _make_run(tmp_path: Path) -> tuple:
    """A canonical run (clone + settings) under a temp factory root."""
    root = tmp_path / "factory-root"
    run = create_run("fixture", _fixture_repo(tmp_path), factory_root=root)
    settings = build_settings({}, run.root, model_endpoint="api.z.ai",
                              factory_root=root)
    write_settings(settings, run.settings_path)
    return run, root, settings


def _plant_cmd(run_dir: Path, name: str, marker: Path) -> str:
    """A shell snippet writing an executable ``<run>/.venv/bin/<name>``
    that would write ``marker`` OUTSIDE the run dir if it ever ran as
    the launcher (the audit's exact payload shape)."""
    payload = (
        "#!/bin/sh\n"
        f"echo 'PLANTED-{name}-RAN argv=\"$*\"' > {shlex.quote(str(marker))}\n"
    )
    return (
        f"mkdir -p .venv/bin && printf '%s\\n' {shlex.quote(payload)} "
        f"> .venv/bin/{name} && chmod +x .venv/bin/{name} && "
        f"echo planted-{name}\n"
    )


_STANDIN_TEMPLATE = textwrap.dedent("""\
    #!/usr/bin/env node
    // F-62 stand-in srt: the real cli.js shebang on line 1 (so a
    // PATH-resolved mutation exec's it the way the real one is exec'd),
    // answers --version with the pin, and runs the -c command
    // unsandboxed. Every invocation logs its interpreter (process
    // .argv[0]), its script path, its arguments and the environment it
    // was launched with, beside itself.
    const fs = require("fs");
    const path = require("path");
    const cp = require("child_process");
    const args = process.argv.slice(2);
    fs.appendFileSync(
      path.join(__dirname, {log_name!r}),
      JSON.stringify({{
        node: process.argv[0],
        cli: process.argv[1],
        args: args,
        path: process.env.PATH || "",
        hasVenvEnv: "VIRTUAL_ENV" in process.env,
        hasTokenEnv: "ANTHROPIC_AUTH_TOKEN" in process.env,
      }}) + "\\n");
    if (args[0] === "--version") {{
      console.log("srt " + {pin!r});
      process.exit(0);
    }}
    const i = args.indexOf("-c");
    if (i >= 0) {{
      const r = cp.spawnSync("/bin/sh", ["-c", args[i + 1]],
                             {{ stdio: "inherit" }});
      process.exit(r.status === null ? 127 : r.status);
    }}
    """)


def _stand_in_srt(directory: Path, name: str = "srt-standin",
                  log_name: str = "argv0.log") -> Path:
    """Write the JS stand-in srt and return its path (executable)."""
    directory.mkdir(parents=True, exist_ok=True)
    standin = directory / name
    standin.write_text(
        _STANDIN_TEMPLATE.format(log_name=log_name, pin=SRT_PIN))
    standin.chmod(0o755)
    return standin


def _log_entries(standin: Path, log_name: str = "argv0.log") -> list[dict]:
    log = standin.parent / log_name
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()
            if line.strip()]


def _trusted_node() -> str:
    found = shutil.which("node", path=os.environ.get("PATH", ""))
    return str(Path(found).resolve()) if found else ""


@pytest.fixture
def fresh_binary_cache():
    """Clear the per-process PATH-resolution cache around a test that
    monkeypatches PATH (the launcher resolves node/bwrap/socat/rg once
    per process; a polluted cache would leak between tests)."""
    from factory import sandbox as sandbox_module

    sandbox_module._resolved_on_path.cache_clear()
    yield
    sandbox_module._resolved_on_path.cache_clear()


# ------------------------------------------------------------------ AC-1 --


def test_ac_1(tmp_path: Path):
    """AC-1: the sandbox launcher resolves only from trusted paths.

    A gate command that plants ``<run>/.venv/bin/node`` (and ``bwrap``,
    ``socat``) does not change what the next ``run_sandboxed`` launches;
    the pin check runs exactly the argv the launch uses; the child's
    venv PATH still reaches the confined command (through the ``-c``
    string, not the launcher env)."""
    from factory.orchestrator import sandboxed_runner

    run, root, _ = _make_run(tmp_path)
    marker = tmp_path / MARKER_NAME
    standin = _stand_in_srt(tmp_path)

    runner = sandboxed_runner(run.root, run.settings_path,
                              srt_bin_path=standin)

    # Gate command 1: plant node/bwrap/socat inside the run's own venv,
    # each of which would write a marker outside the run dir.
    setup = runner(
        "setup",
        _plant_cmd(run.root, "node", marker)
        + _plant_cmd(run.root, "bwrap", marker)
        + _plant_cmd(run.root, "socat", marker),
        run.root, 60,
    )
    assert setup.exit_code == 0, setup.output

    # Gate command 2 runs next in the same clean clone, through the same
    # runner and venv. The confined command resolves python into the
    # venv — proving the venv PATH reached it — and the planted binaries
    # must not have executed.
    lint = runner("lint", "command -v python > python-where.txt", run.root, 60)
    assert lint.exit_code == 0, lint.output
    where = (run.root / "python-where.txt").read_text().strip()
    assert where.startswith(str(run.root / ".venv" / "bin")), (
        f"the confined command lost the venv PATH: resolved {where!r}"
    )

    assert not marker.exists(), (
        "a binary the confined child planted inside the run dir ran as "
        "the sandbox launcher — the host boundary is broken"
    )

    # Every stand-in invocation (the pin check AND both launches) was
    # interpreted by the SAME trusted node, running the same cli script.
    entries = _log_entries(standin)
    pins = [e for e in entries if e["args"][:1] == ["--version"]]
    launches = [e for e in entries if e["args"][:1] == ["--settings"]]
    assert len(pins) >= 1 and len(launches) == 2, (
        f"expected the pin check plus both launches in the log, got {entries}"
    )
    node = _trusted_node()
    assert node, "node must be resolvable for the test to mean anything"
    for entry in entries:
        assert entry["node"] == node, (
            f"the launcher ran a different interpreter: {entry}"
        )
        assert entry["cli"] == str(standin.resolve()), entry
    # THE pin-check argv equals the launch argv: same interpreter, same
    # cli script — one code path, proven by the stand-in's own log.
    for pin in pins:
        for launch in launches:
            assert (pin["node"], pin["cli"]) == (launch["node"], launch["cli"])


def test_ac_1_real_srt_planted_node_does_not_run(tmp_path: Path):
    """The real-srt proof of AC-1: a planted ``.venv/bin/node`` does not
    run (no marker outside the run dir) and a normal command still works
    through the real sandbox stack. Skipped unless the pinned srt and a
    usable seatbelt/bwrap stack are available (the test_f05 skip
    pattern; the suite's nested-sandbox guard covers nesting)."""
    pytest.importorskip("factory.sandbox")
    from factory import sandbox as sandbox_module

    if not sandbox_module.can_apply_seatbelt():
        pytest.skip("seatbelt/bwrap stack unavailable (INC-007 pattern)")
    try:
        sandbox_module.ensure_srt()
    except SandboxError as exc:
        pytest.skip(f"real srt unusable here: {exc}")

    run, root, _ = _make_run(tmp_path)
    marker = tmp_path / MARKER_NAME
    venv_bin = run.root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    # Plant every binary the macOS launch path resolves through PATH:
    # node (the launcher's interpreter — pre-fix), env and bash (srt's
    # own wrapped command resolves both via which()).
    for name in ("node", "env", "bash"):
        (venv_bin / name).write_text(
            "#!/bin/sh\necho planted > " + shlex.quote(str(marker)) + "\n")
        (venv_bin / name).chmod(0o755)

    proc = sandbox_module.run_sandboxed(
        ["/bin/sh", "-c", "echo real-srt-ok"],
        run.root, run.settings_path, venv=run.root / ".venv", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "real-srt-ok" in proc.stdout
    assert not marker.exists(), "the planted venv node executed"


def test_trusted_node_refuses_run_dir_factory_root_and_missing(
    tmp_path: Path, monkeypatch, fresh_binary_cache,
):
    """A node resolved from the orchestrator's PATH that lives inside
    the run dir or the factory root is refused with the reason named;
    a host with no node at all is refused, naming the fix."""
    from factory import sandbox as sandbox_module

    run, root, _ = _make_run(tmp_path)

    # A node "installed" inside the run dir, found first on PATH.
    fake_bin = run.root / "toolbin"
    fake_bin.mkdir()
    (fake_bin / "node").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "node").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    with pytest.raises(SandboxError, match="run dir"):
        sandbox_module.run_sandboxed(["/bin/sh", "-c", "true"], run.root,
                                     run.settings_path,
                                     srt_bin_path=_stand_in_srt(tmp_path))

    # A node inside the factory root (a repo-local toolchain dir). The
    # resolver caches per process — re-resolve against the new PATH.
    root_bin = root / "toolbin"
    root_bin.mkdir()
    (root_bin / "node").write_text("#!/bin/sh\nexit 0\n")
    (root_bin / "node").chmod(0o755)
    monkeypatch.setenv("PATH", f"{root_bin}:{os.environ['PATH']}")
    sandbox_module._resolved_on_path.cache_clear()
    with pytest.raises(SandboxError, match="factory root"):
        sandbox_module.run_sandboxed(["/bin/sh", "-c", "true"], run.root,
                                     run.settings_path,
                                     srt_bin_path=_stand_in_srt(tmp_path))

    # No node anywhere on PATH.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    sandbox_module._resolved_on_path.cache_clear()
    with pytest.raises(SandboxError, match="node"):
        sandbox_module.run_sandboxed(["/bin/sh", "-c", "true"], run.root,
                                     run.settings_path,
                                     srt_bin_path=_stand_in_srt(tmp_path))


def test_symlinked_binaries_into_the_run_dir_are_refused(
    tmp_path: Path, fresh_binary_cache,
):
    """A node (via PATH) or an srt (via srt_bin_path) whose REALPATH
    lands inside the run dir is refused — symlink spellings cannot
    smuggle a run-dir binary past the containment check. A node symlink
    whose target is the real system node is fine (it executes the real
    node)."""
    from factory import sandbox as sandbox_module

    run, root, _ = _make_run(tmp_path)
    real_node = shutil.which("node")
    if not real_node:
        pytest.skip("no node on PATH")

    # PATH entry inside the run dir whose node is a symlink to a
    # run-dir payload: realpath lands inside the run dir → refused.
    payload = run.root / "payload-node"
    payload.write_text("#!/bin/sh\nexit 0\n")
    payload.chmod(0o755)
    fake_bin = run.root / "linkbin"
    fake_bin.mkdir()
    (fake_bin / "node").symlink_to(payload)
    import pytest as _pytest

    original_path = os.environ["PATH"]
    try:
        os.environ["PATH"] = f"{fake_bin}:{original_path}"
        sandbox_module._resolved_on_path.cache_clear()
        with _pytest.raises(SandboxError, match="run dir"):
            sandbox_module.run_sandboxed(
                ["/bin/sh", "-c", "true"], run.root, run.settings_path,
                srt_bin_path=_stand_in_srt(tmp_path))
    finally:
        os.environ["PATH"] = original_path
        sandbox_module._resolved_on_path.cache_clear()

    # srt_bin_path as a symlink into the run dir — its TARGET inside
    # the run dir is what the realpath check refuses.
    standin = _stand_in_srt(tmp_path)
    target = run.root / "planted-srt.js"
    target.write_text(standin.read_text())
    target.chmod(0o755)
    link = run.root / "srt-link"
    link.symlink_to(target)
    with _pytest.raises(SandboxError, match="run dir"):
        sandbox_module.run_sandboxed(["/bin/sh", "-c", "true"], run.root,
                                     run.settings_path, srt_bin_path=link)

    # A node symlink pointing at the real system node executes the real
    # node — allowed by design (the toolchain allowlist keeps such dirs).
    good_bin = tmp_path / "goodbin"
    good_bin.mkdir()
    (good_bin / "node").symlink_to(real_node)
    try:
        os.environ["PATH"] = f"{good_bin}:{original_path}"
        sandbox_module._resolved_on_path.cache_clear()
        proc = sandbox_module.run_sandboxed(
            ["/bin/sh", "-c", "echo ok"], run.root, run.settings_path,
            srt_bin_path=standin, timeout=60)
        assert proc.returncode == 0 and "ok" in proc.stdout
    finally:
        os.environ["PATH"] = original_path
        sandbox_module._resolved_on_path.cache_clear()


def test_launcher_env_carries_no_run_dir_path_and_no_virtual_env(
    tmp_path: Path, fresh_binary_cache,
):
    """The launcher environment's PATH has no entry inside the run dir
    and carries no VIRTUAL_ENV — the child's venv reaches the confined
    command only through the ``-c`` assignment prefix (the TMPDIR
    pattern). The stand-in, running as srt does, reports its own
    environment."""
    from factory import sandbox as sandbox_module

    run, root, _ = _make_run(tmp_path)
    venv = run.root / ".venv"
    (venv / "bin").mkdir(parents=True)
    standin = _stand_in_srt(tmp_path)

    proc = sandbox_module.run_sandboxed(
        ["/bin/sh", "-c", "printenv VIRTUAL_ENV > venv-inside.txt; true"],
        run.root, run.settings_path, venv=venv, srt_bin_path=standin,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    entries = _log_entries(standin)
    assert entries, "the stand-in never ran"
    # Only the LAUNCH entries describe the launcher env — the pin check
    # runs with the orchestrator's ambient env (absolute-argv exec; its
    # PATH is never consulted for resolution).
    launches = [e for e in entries if e["args"][:1] == ["--settings"]]
    assert launches, "the stand-in never launched a command"
    for entry in launches:
        for path_entry in entry["path"].split(":"):
            if not path_entry:
                continue
            assert not path_entry.startswith(str(run.root)), (
                f"launcher PATH leaks the run dir: {entry['path']}"
            )
        assert entry["hasVenvEnv"] is False, (
            "VIRTUAL_ENV reached the launcher environment"
        )
    # ... while the CONFINED command did see the venv, via the prefix.
    inside = (run.root / "venv-inside.txt").read_text().strip()
    assert inside == str(venv)


def test_the_token_rides_the_environment_never_the_argv(tmp_path: Path):
    """The seat token is in the launcher environment (the sandbox's one
    secret) and absent from the Popen argv — argv shows up in ``ps``;
    env does not. The ``-c`` string carries only TMPDIR/PATH/VIRTUAL_ENV
    assignments."""
    from factory import sandbox as sandbox_module

    run, root, _ = _make_run(tmp_path)
    standin = _stand_in_srt(tmp_path)
    proc = sandbox_module.run_sandboxed(
        ["/bin/sh", "-c", "true"],
        run.root, run.settings_path, srt_bin_path=standin,
        env={"ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN}, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    entries = _log_entries(standin)
    assert any(e["hasTokenEnv"] for e in entries), (
        "the token never reached the session environment"
    )
    for entry in entries:
        blob = json.dumps(entry["args"])
        assert FAKE_TOKEN not in blob, "the token appeared in the argv"


def test_relative_path_entries_never_reach_the_launcher(tmp_path: Path):
    """Round 1, hole 2: a relative PATH entry (``.``, ``bin``,
    ``node_modules/.bin`` — common on a developer's PATH) resolves
    against the READER's cwd. srt runs with cwd=run_dir, so a relative
    entry searches the run dir at srt's own lookup time: a planted
    ``<run>/bash`` would stand in for the shell srt wraps the command
    with. No relative or empty entry may survive into the launcher
    PATH."""
    from factory import sandbox as sandbox_module

    run, root, _ = _make_run(tmp_path)
    standin = _stand_in_srt(tmp_path)
    proc = sandbox_module.run_sandboxed(
        ["/bin/sh", "-c", "true"],
        run.root, run.settings_path, srt_bin_path=standin,
        env={"PATH": ".:bin:node_modules/.bin::/usr/bin:/bin"},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    launches = [e for e in _log_entries(standin)
                if e["args"][:1] == ["--settings"]]
    assert launches
    for entry in launches:
        entries = entry["path"].split(":")
        assert "" not in entries, f"empty entry survived: {entry['path']}"
        for path_entry in entries:
            assert Path(path_entry).is_absolute(), (
                f"relative entry {path_entry!r} reached the launcher PATH: "
                f"{entry['path']}"
            )


def test_node_from_a_relative_path_entry_is_refused(
    tmp_path: Path, monkeypatch, fresh_binary_cache,
):
    """Round 1, hole 2: ``shutil.which`` on a relative entry resolves
    against the ORCHESTRATOR's cwd — the client checkout the owner runs
    ``factory`` from. A client ``node_modules/.bin/node`` would become
    the trusted launcher node (client-chosen code as the interpreter).
    A node findable only through a relative entry is NOT FOUND."""
    from factory import sandbox as sandbox_module

    run, root, _ = _make_run(tmp_path)
    standin = _stand_in_srt(tmp_path)
    marker = tmp_path / "relative-node-ran.txt"
    client_bin = tmp_path / "client" / "node_modules" / ".bin"
    client_bin.mkdir(parents=True)
    fake = client_bin / "node"
    fake.write_text("#!/bin/sh\necho ran > " + shlex.quote(str(marker)) + "\n")
    fake.chmod(0o755)
    monkeypatch.chdir(tmp_path / "client")
    monkeypatch.setenv("PATH", "node_modules/.bin")
    with pytest.raises(SandboxError, match="not on the orchestrator's PATH"):
        sandbox_module.run_sandboxed(
            ["/bin/sh", "-c", "true"], run.root, run.settings_path,
            srt_bin_path=standin, timeout=60,
        )
    assert not marker.exists(), (
        "a node found through a relative PATH entry executed"
    )


def test_linux_settings_pin_bwrap_socat_and_ripgrep(
    tmp_path: Path, monkeypatch, fresh_binary_cache,
):
    """On Linux, build_settings pins bwrapPath/socatPath (absolute,
    resolved like node) and ripgrep.command — every helper srt 0.0.79
    otherwise resolves through PATH (whichSync('bwrap'/'socat'), spawn
    ('rg')). A pinned binary that resolves inside the run dir is
    refused; a missing one is simply not pinned (srt answers for
    itself, exactly as today)."""
    from factory import sandbox as sandbox_module

    run, root, _ = _make_run(tmp_path)
    toolbin = tmp_path / "toolbin"
    toolbin.mkdir()
    for name in ("bwrap", "socat", "rg"):
        (toolbin / name).write_text("#!/bin/sh\nexit 0\n")
        (toolbin / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{toolbin}:{os.environ['PATH']}")
    monkeypatch.setattr(sys, "platform", "linux")
    try:
        # Re-clear AFTER the setup above: on a real Linux host,
        # _make_run's own build_settings already resolved the SYSTEM
        # bwrap/socat/rg into the per-process cache (the designed
        # behaviour — one resolution per process from a stable PATH);
        # the pins under test must resolve against the toolbin PATH.
        sandbox_module._resolved_on_path.cache_clear()
        settings = build_settings({}, run.root, model_endpoint="api.z.ai",
                                  factory_root=root)
        assert settings["bwrapPath"] == str(toolbin / "bwrap")
        assert settings["socatPath"] == str(toolbin / "socat")
        assert settings["ripgrep"] == {"command": str(toolbin / "rg")}

        # A bwrap that resolves inside the run dir: refused, named.
        planted = run.root / "bin"
        planted.mkdir()
        (planted / "bwrap").write_text("#!/bin/sh\nexit 0\n")
        (planted / "bwrap").chmod(0o755)
        monkeypatch.setenv("PATH", f"{planted}:{os.environ['PATH']}")
        sandbox_module._resolved_on_path.cache_clear()
        with pytest.raises(SandboxError, match="bwrap.*run dir"):
            build_settings({}, run.root, model_endpoint="api.z.ai",
                           factory_root=root)

        # Nothing resolvable → no keys pinned, no refusal (srt's own
        # error at start time is today's behaviour and stays srt's).
        monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
        sandbox_module._resolved_on_path.cache_clear()
        settings = build_settings({}, run.root, model_endpoint="api.z.ai",
                                  factory_root=root)
        assert "bwrapPath" not in settings
        assert "socatPath" not in settings
        assert "ripgrep" not in settings
    finally:
        monkeypatch.undo()
        sandbox_module._resolved_on_path.cache_clear()


# ------------------------------------------------------------------ AC-2 --


def _content_repo(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A repo with a base commit; returns (repo, base_sha)."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text("# base\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@factory.invalid")
    _git(repo, "config", "user.name", "factory test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _head_commit(repo: Path, message: str = "head") -> str:
    # --allow-empty: the commit-message cases have nothing staged, and
    # the message itself is the payload under test.
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _pushed(remote: Path) -> set[str]:
    return set(_git(remote, "for-each-ref", "--format=%(refname)",
                    "refs/heads").split())


def _b64_core(raw: bytes, alignment: int,
              encoder=base64.b64encode) -> str:
    """The chars of ``raw``'s base64 contribution at ``alignment`` that
    depend on no neighbouring byte (the fence's mid-stream needles)."""
    encoded = encoder(b"\x00" * alignment + raw).decode("ascii")
    start = -(-8 * alignment // 6)  # ceil
    end = (8 * (alignment + len(raw))) // 6
    return encoded[start:end]


def _every_form(secret: str) -> list[str]:
    """Every encoding the fence matches — used to prove a refusal
    message echoes none of them."""
    raw = secret.encode()
    forms = [secret, raw.hex(), raw.hex().upper(),
             urllib.parse.quote(secret, safe="")]
    forms += [base64.b64encode(raw).decode(),
              base64.urlsafe_b64encode(raw).decode()]
    forms += [_b64_core(raw, a) for a in range(3)]
    forms += [_b64_core(raw, a, base64.urlsafe_b64encode) for a in range(3)]
    return [form for form in forms if form]


def test_ac_2(tmp_path: Path):
    """AC-2: pushed content is scanned for the secrets the orchestrator
    holds. The token in a new file, a modified file, a file path, a
    commit message, base64 mid-stream at each alignment, hex and
    URL-encoded: each refuses like ``check_pr_text`` — nothing is
    pushed, the reason names the file or commit SHA and the encoding,
    and never echoes any form of the secret."""
    from factory.publish import check_push_content

    case_no = [0]

    def refusal_case(mutate, *, token: str = FAKE_TOKEN,
                     expect_file: bool = True):
        case_no[0] += 1
        repo, base = _content_repo(tmp_path, f"case-{case_no[0]}")
        mutate(repo)
        head = _head_commit(repo)
        with pytest.raises(PushRefused) as excinfo:
            check_push_content(repo, base, head, secrets=(token,))
        message = str(excinfo.value)
        for form in _every_form(token):
            assert form not in message, (
                f"the refusal echoes the secret ({form[:6]}…): {message}"
            )
        assert "secret" in message or "credential" in message
        return message, head

    # The literal in a new file, and in a modified file.
    refusal_case(lambda r: (r / "notes").mkdir(exist_ok=True)
                 or (r / "notes" / "env-audit.txt").write_text(FAKE_TOKEN))
    refusal_case(lambda r: (r / "README.md").write_text(
        "# base\nseen: " + FAKE_TOKEN + "\n"))

    # The token in a FILE PATH (diff headers carry it — the whole output
    # is scanned, not just + lines).
    refusal_case(lambda r: (r / f"notes-{FAKE_TOKEN}.txt")
                 .write_text("harmless\n"))

    # The token in a commit message: the refusal names the SHA.
    def commit_message_case():
        repo, base = _content_repo(tmp_path, "case-msg")
        head = _head_commit(repo, f"notes: rotate {FAKE_TOKEN}")
        from factory.publish import check_push_content
        with pytest.raises(PushRefused) as excinfo:
            check_push_content(repo, base, head, secrets=(FAKE_TOKEN,))
        message = str(excinfo.value)
        assert head[:12] in message, "the refusal must name the commit"
        for form in _every_form(FAKE_TOKEN):
            assert form not in message

    commit_message_case()

    # base64 mid-stream at all three byte alignments: the secret's bytes
    # embedded after a 3/1/2-byte prefix, so its encoded core starts at
    # every possible alignment of the surrounding stream — and none of
    # the three is the plain encoding of the token alone.
    plain = base64.b64encode(FAKE_TOKEN.encode()).decode()
    for prefix in (b"ab+", b"a", b"ab"):
        content = base64.b64encode(prefix + FAKE_TOKEN.encode()).decode()
        assert content != plain, "the alignment cases must be distinct"
        refusal_case(lambda r, c=content: (r / "docs").mkdir(exist_ok=True)
                     or (r / "docs" / "debug.txt").write_text(c))

    # Hex, lower and upper; URL-percent-encoded (the +/= characters are
    # what URL-encoding transforms).
    refusal_case(lambda r: (r / "hex.txt").write_text(
        FAKE_TOKEN_URLISH.encode().hex()), token=FAKE_TOKEN_URLISH)
    refusal_case(lambda r: (r / "hex.txt").write_text(
        FAKE_TOKEN_URLISH.encode().hex().upper()), token=FAKE_TOKEN_URLISH)
    refusal_case(lambda r: (r / "url.txt").write_text(
        urllib.parse.quote(FAKE_TOKEN_URLISH, safe="")),
        token=FAKE_TOKEN_URLISH)

    # A repo .gitattributes textconv (and an external diff driver) in
    # the head must not hide the token: --no-textconv/--no-ext-diff are
    # mandatory.
    def hide_via_textconv(repo: Path):
        (repo / ".gitattributes").write_text("secret.bin diff=hide\n")
        (repo / "secret.bin").write_text(FAKE_TOKEN)
        _git(repo, "config", "diff.hide.textconv", "cat /dev/null")

    refusal_case(hide_via_textconv)

    def hide_via_extdiff(repo: Path):
        hider = repo.parent / "false-diff"
        hider.write_text("#!/bin/sh\nexit 0\n")
        hider.chmod(0o755)
        (repo / "plain.txt").write_text(FAKE_TOKEN)
        _git(repo, "config", "diff.external", str(hider))

    refusal_case(hide_via_extdiff)

    # A repo .gitattributes textconv (and an external diff driver): the
    # object scan reads raw object bytes through cat-file, which never
    # consults either — these cases pin that no repo diff configuration
    # can change what the scan sees.
    def hide_via_textconv(repo: Path):
        (repo / ".gitattributes").write_text("secret.bin diff=hide\n")
        (repo / "secret.bin").write_text(FAKE_TOKEN)
        _git(repo, "config", "diff.hide.textconv", "cat /dev/null")

    refusal_case(hide_via_textconv)

    def hide_via_extdiff(repo: Path):
        hider = repo.parent / "false-diff"
        hider.write_text("#!/bin/sh\nexit 0\n")
        hider.chmod(0o755)
        (repo / "plain.txt").write_text(FAKE_TOKEN)
        _git(repo, "config", "diff.external", str(hider))

    refusal_case(hide_via_extdiff)

    # A token inside a BINARY file (raw object bytes, no "Binary files
    # differ" rendering to hide behind).
    def binary_payload(repo: Path):
        (repo / "blob.bin").write_bytes(
            b"\x00\x01" + FAKE_TOKEN.encode() + b"\x00\x02\n")

    refusal_case(binary_payload)

    # Round 1, hole 1: the scan must cover every OBJECT that leaves,
    # not the net diff — the push transfers intermediate commits' trees
    # and blobs and every parent of a merge, none of which a diff of
    # base..head shows.
    def add_then_delete(repo: Path):
        (repo / "leak.txt").write_text(FAKE_TOKEN + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add the leak")
        _git(repo, "rm", "-q", "leak.txt")
        _git(repo, "commit", "-qm", "remove it again")

    refusal_case(add_then_delete)

    def orphan_merge(repo: Path):
        # An orphan branch commits the token; the merge takes only the
        # orphan's HISTORY (-s ours keeps our tree), so the net diff is
        # empty while the leak blob rides the pushed objects. (The
        # orphan commit may also carry base's files — its TREE is
        # discarded by -s ours; only its history is merged in.)
        _git(repo, "checkout", "-q", "--orphan", "leak-branch")
        (repo / "leak.txt").write_text(FAKE_TOKEN + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "orphan leak")
        _git(repo, "checkout", "-q", "-f", "main")
        _git(repo, "merge", "-q", "--allow-unrelated-histories",
             "-s", "ours", "leak-branch", "-m", "merge the history")

    refusal_case(orphan_merge)

    def intermediate_tree_entry_name(repo: Path):
        # The token ONLY as a tree entry name inside an intermediate
        # commit: added as a file name, gone by the head.
        (repo / f"{FAKE_TOKEN}.txt").write_text("harmless\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "oddly named file")
        _git(repo, "rm", "-q", f"{FAKE_TOKEN}.txt")
        _git(repo, "commit", "-qm", "gone")

    refusal_case(intermediate_tree_entry_name)

    def intermediate_author_identity(repo: Path):
        # The token in the AUTHOR identity of an intermediate commit —
        # not the message, the author/committer fields.
        (repo / "impl.py").write_text("x = 2\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "innocent message"],
            check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": FAKE_TOKEN,
                 "GIT_AUTHOR_EMAIL": "dev@client.example"},
        )

    refusal_case(intermediate_author_identity)

    # Chunk-boundary integrity: a blob larger than the scan chunk with
    # the token STRADDLING the boundary — the overlap must carry it.
    # (Green against the whole-string scan by construction; its teeth
    # are the no-overlap mutation, which goes red on exactly this case.)
    try:
        from factory.publish import PUSH_SCAN_CHUNK
    except ImportError:  # the pre-round-1 tree: no streaming scan yet
        PUSH_SCAN_CHUNK = 0

    if PUSH_SCAN_CHUNK:

        def straddling_the_chunk_boundary(repo: Path):
            token = FAKE_TOKEN.encode()
            start = PUSH_SCAN_CHUNK - (len(token) // 2)
            (repo / "big.txt").write_bytes(
                b"a" * start + token + b"b" * PUSH_SCAN_CHUNK)

        refusal_case(straddling_the_chunk_boundary)

    # A clean head does not refuse; an empty secret tuple does not
    # refuse; neither does an empty-string secret (seat_token() when no
    # credential is held) or one shorter than the scrub floor.
    repo, base = _content_repo(tmp_path, "clean")
    (repo / "impl.py").write_text("x = 1\n")
    head = _head_commit(repo)
    check_push_content(repo, base, head, secrets=(FAKE_TOKEN,))
    check_push_content(repo, base, head, secrets=())
    check_push_content(repo, base, head, secrets=("",))
    check_push_content(repo, base, head, secrets=("short",))


def test_clean_head_pushes_after_the_scan(tmp_path: Path):
    """The scan sits before push_branch: a clean head passes it and the
    push lands on a real (bare, local) remote; a dirty head refuses and
    the remote shows no new ref."""
    from factory.publish import check_push_content

    repo, base = _content_repo(tmp_path, "pushrepo")
    (repo / "impl.py").write_text("x = 1\n")
    head = _head_commit(repo)
    check_push_content(repo, base, head, secrets=(FAKE_TOKEN,))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(remote)],
                   check=True)
    push_branch(repo, str(remote), "factory/f62-clean", sha=head,
                created={"factory/f62-clean"}, default_branch="main")
    assert "refs/heads/factory/f62-clean" in _pushed(remote)

    # Dirty: the fence refuses BEFORE the push — the remote gains
    # nothing (proven end-to-end by the orchestrator test below; here
    # the ordering is the raise itself).
    (repo / "notes.txt").write_text(FAKE_TOKEN)
    dirty = _head_commit(repo)
    with pytest.raises(PushRefused):
        check_push_content(repo, base, dirty, secrets=(FAKE_TOKEN,))
    assert _pushed(remote) == {"refs/heads/main", "refs/heads/factory/f62-clean"}


REVIEW_PASS = json.dumps({"verdict": "pass", "findings": []})
TEST_SOURCE = (
    "from impl import answer\n"
    "\n"
    "\n"
    "def test_ac_1():\n"
    "    assert answer() == 42\n"
)


class _Seats:
    """Stub seats: a passing implementer that also persists the seat
    token into the repo (the audit's scenario), and a passing review."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, seat, prompt, run_dir, profile, **kwargs):
        from pathlib import Path as _Path

        self.calls.append(seat)
        run_dir = _Path(run_dir)
        if seat == "implementer":
            (run_dir / "impl.py").write_text("def answer():\n    return 42\n")
            (run_dir / "tests").mkdir(exist_ok=True)
            (run_dir / "tests" / "test_feature.py").write_text(TEST_SOURCE)
            # The leak: the raw token in one file, base64 mid-stream in
            # another — nothing shape-based catches either.
            (run_dir / "notes").mkdir(exist_ok=True)
            (run_dir / "notes" / "env-audit.txt").write_text(FAKE_TOKEN)
            (run_dir / "docs").mkdir(exist_ok=True)
            (run_dir / "docs" / "debug.txt").write_text(
                base64.b64encode(b"ab+" + FAKE_TOKEN.encode()).decode())
            return SessionRecord(
                seat=seat, status="completed", num_turns=3,
                model="stub-model",
                tokens={"input_tokens": 120, "output_tokens": 40},
                result="Implemented answer().",
            )
        return SessionRecord(
            seat=seat, status="completed", num_turns=1, model="stub-model",
            tokens={"input_tokens": 60, "output_tokens": 10},
            result=REVIEW_PASS,
        )


class _Env:
    """The F-10/F-42 harness shape, trimmed to one passing run whose
    implementer leaks the seat token into the tree it publishes."""

    def __init__(self, tmp_path: Path, *, queue_project: str = "fixture"):
        self.root = tmp_path / "factory"
        self.root.mkdir(parents=True)
        self.repo = tmp_path / "proj"
        self.remote = tmp_path / "remote.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "fixture@example.com")
        _git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "impl.py").write_text("def answer():\n    return 0\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text(
            "def test_base():\n    assert True\n")
        profile = {
            "name": "fixture",
            "repo": {"path": str(self.repo), "default_branch": "main",
                     "remote": str(self.remote), "merge_method": "squash"},
            "commands": {
                "setup": "true",
                "test": f"{shlex.quote(sys.executable)} -m pytest -q "
                        "-p no:cacheprovider",
            },
            "paths": {"risk": {}, "protected": [".factory/**"]},
            "deploy": {"mechanism": "none", "auto_on_merge": False},
            "live_checks": {},
            "docs_allowlist": [],
        }
        factory_dir = self.repo / ".factory"
        factory_dir.mkdir()
        (factory_dir / "project.yaml").write_text(json.dumps(profile))
        self.queue_path = factory_dir / "tasks.json"
        from factory.contract import acceptance_hash
        acceptance = [{"id": "AC-1", "text": "answer() returns 42",
                       "check": {"type": "test",
                                 "ref": "tests/test_feature.py::test_ac_1"}}]
        task = {
            "id": "T-1", "title": "Make answer() return 42",
            "source": "docs/T-1.md", "kind": "build", "priority": "P1",
            "status": "ready", "depends_on": [], "risk": [],
            "gates": {"spec_approval": True, "merge": "human",
                      "deploy": "human"},
            "acceptance": acceptance,
            "acceptance_hash": acceptance_hash(acceptance),
            "approved_at": "2026-11-09T10:00:00Z", "approved_by": "owner",
            "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                       "wall_clock_min": 10},
            "attempts": [], "links": {},
        }
        self.queue_path.write_text(
            json.dumps({"version": 1, "project": queue_project,
                        "tasks": [task]}, indent=2) + "\n")
        (self.repo / ".gitignore").write_text(".factory/*.lock\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(self.repo), str(self.remote)],
            check=True)
        self.prs: list = []

    def open_pr(self, pr) -> str:
        self.prs.append(pr)
        return f"https://github.com/fixture/fixture/pull/{len(self.prs)}"

    def remote_branches(self) -> set[str]:
        return set(_git(self.remote, "for-each-ref",
                        "--format=%(refname)", "refs/heads").split())

    def run(self, session_runner, **kwargs):
        from zoneinfo import ZoneInfo

        from factory.mergeorder import Protection
        from factory.orchestrator import run_once

        # A Sunday afternoon in Stockholm: outside every default peak
        # window (the F-42 harness instant).
        kwargs.setdefault(
            "now", lambda: datetime(
                2026, 10, 18, 15, 0, tzinfo=ZoneInfo("Europe/Stockholm")))
        kwargs.setdefault(
            "protection",
            lambda remote, branch: Protection(checked=True, strict=True))
        kwargs.setdefault("pr_state", lambda url: "open")
        return run_once(
            self.queue_path, factory_root=self.root,
            session_runner=session_runner, open_pr=self.open_pr, **kwargs)


def test_push_content_refusal_precedes_the_push(tmp_path, monkeypatch):
    """End to end: a session that writes the seat token into the tree
    still passes every existing fence (PR text, sentinels — the token
    matches no shape), and the push-content fence refuses the publish:
    nothing reaches the remote, no PR opens, publish_refused."""
    import factory.orchestrator as orch

    env = _Env(tmp_path)
    monkeypatch.setattr(orch, "seat_token", lambda: FAKE_TOKEN)
    result = env.run(_Seats())
    assert result.outcome == "needs_human", result
    assert result.escalation == "publish_refused", result
    assert env.prs == [], "a PR opened about a refused push"
    assert env.remote_branches() == {"refs/heads/main"}, (
        "the token-carrying branch reached the remote"
    )


# ------------------------------------------------------------------ AC-3 --


BAD_NAMES = ["../x", "/abs", "a/b", "", "abc\n", "аbc", "ABC", "a b", "."]
VALID_NAMES = ["fixture", "a-b-2", "jobfinderos", "-"]  # "-" matches the
# profile's own ^[a-z0-9-]+$ — kept allowed; every builder embeds it in
# an absolute path, never as a bare argument (hostile-pass note).


def _queue_doc(project: str) -> dict:
    return {"version": 1, "project": project, "tasks": []}


def _write_queue(tmp_path: Path, project: str) -> Path:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(_queue_doc(project), indent=2) + "\n")
    return path


def test_ac_3(tmp_path: Path):
    """AC-3: every factory-root path component built from a project name
    is a validated slug. ``../x``, an absolute path, ``a/b``, the empty
    string and a Unicode look-alike are refused at queue load and at
    each builder; a queue whose project differs from the profile's name
    refuses to load with both names shown; the importer repair path and
    ``factory status`` with such a queue create and read nothing outside
    ``<root>/<area>/<slug>``."""
    from factory import importer, orchestrator, progress, runs, status
    from factory import trace as trace_module
    from factory import vent as vent_module
    from factory.runs import ProjectSlugError, project_component

    # The helper: never sanitises, always refuses.
    for bad in BAD_NAMES:
        with pytest.raises(ProjectSlugError):
            project_component(bad)
    for good in VALID_NAMES:
        assert project_component(good) == good

    # The schema pattern alone still admits a trailing newline (Python
    # re ``$`` matches before it) — the loader check is load-bearing,
    # not decorative.
    import jsonschema

    schema = json.loads(
        (Path(runs.__file__).parent / "data" / "schema"
         / "tasks.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(_queue_doc("abc\n")), (
        "if the schema now refuses the newline, drop this proof and pin "
        "the schema refusal instead — never keep both silently"
    )

    from factory.contract import ContractError, load_tasks

    for bad in BAD_NAMES:
        with pytest.raises(ContractError):
            load_tasks(_write_queue(tmp_path, bad))

    # A profile name with the trailing-newline shape is refused at load
    # too (the same builders consume it).
    profile = tmp_path / "project.yaml"
    profile.write_text(json.dumps({
        "name": "abc\n",
        "repo": {"path": ".", "default_branch": "main", "remote": "nowhere"},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {}, "docs_allowlist": [],
    }))
    with pytest.raises(ContractError, match="abc"):
        from factory.contract import load_profile
        load_profile(profile)

    # Every routed builder refuses instead of building an escaped path.
    rid = "20261109T120000Z-a1b2c3"
    (tmp_path / "b1").mkdir()
    builders = [
        ("runs.create_run", lambda: create_run(
            "../x", _fixture_repo(tmp_path / "b1"), factory_root=tmp_path)),
        ("runs.sandbox_settings_path", lambda: runs.sandbox_settings_path(
            tmp_path, "../x", rid)),
        ("trace.trace_path", lambda: trace_path(tmp_path, "../x", rid)),
        ("trace.prune_traces", lambda: trace_module.prune_traces(
            tmp_path, "../x")),
        ("progress.ProgressLog.for_task",
         lambda: progress.ProgressLog.for_task(tmp_path, "../x", "T-1")),
        ("vent.weeks_vents", lambda: vent_module.weeks_vents(
            tmp_path, "../x", now=datetime.now(timezone.utc),
            unreadable=[])),
        ("importer._save_refused_output",
         lambda: importer._save_refused_output("text", "../x", tmp_path)),
        ("importer._repair_workspace",
         lambda: importer._repair_workspace(tmp_path, "../x", rid)),
        ("status._week_escapees", lambda: status._week_escapees(
            tmp_path, "../x", datetime.now(timezone.utc), [])),
    ]
    for name, build in builders:
        with pytest.raises(ProjectSlugError):
            build()

    # reclaim_runs: the orchestrator's own runs/sandbox reader refuses
    # before touching the filesystem.
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    good_queue = _write_queue(ok_dir, "fixture")
    queue = TaskQueue(good_queue)
    with pytest.raises(ProjectSlugError):
        orchestrator.reclaim_runs(queue, "../x", tmp_path)


def test_mismatched_queue_and_profile_refuses(tmp_path: Path):
    """A queue and a profile that disagree never load together: run_once,
    the importer and the dashboard all refuse, naming both projects."""
    from factory.importer import ImportRefused, run_import
    from factory.orchestrator import RunError

    # run_once: queue says "otherproj", profile says "fixture".
    env = _Env(tmp_path, queue_project="otherproj")
    with pytest.raises(RunError, match="fixture") as excinfo:
        env.run(lambda *a, **k: pytest.fail("no session may run"))
    assert "otherproj" in str(excinfo.value)

    # The importer: same disagreement on an existing queue (env.repo's
    # queue names otherproj; its profile names fixture).
    called = []

    def fake_session(*args, **kwargs):
        called.append(args)
        raise AssertionError("the session must never start")

    monkeypatch_session = pytest.MonkeyPatch()
    monkeypatch_session.setattr("factory.importer.sdk_import_session",
                                fake_session)
    try:
        with pytest.raises(ImportRefused, match="fixture") as excinfo:
            run_import(repo=env.repo, session=None,
                       factory_root=tmp_path / "froot")
        assert "otherproj" in str(excinfo.value)
    finally:
        monkeypatch_session.undo()
    assert called == [], "the importer ran a session on a mismatched pair"

    # The dashboard renders the mismatch as an error page, not data.
    from factory.dashboard import Dashboard

    dash = Dashboard(env.root, env.queue_path)
    page = dash.overview()
    assert page.status == 503, "a mismatched pair rendered a page"
    body = page.body.decode()
    assert "fixture" in body and "otherproj" in body


def _tree(root: Path) -> set[str]:
    found: set[str] = set()
    for path in sorted(root.rglob("*")) if root.exists() else []:
        found.add(str(path))
    return found


def test_status_and_importer_with_hostile_queue_touch_nothing_outside(
    tmp_path: Path, monkeypatch,
):
    """The audit's exact scenario, refused at load: a client repo's
    tasks.json names ``../../outside``; ``factory status`` and the
    importer repair path create and read nothing outside the tmp root —
    and nothing at all outside what existed before, in the root or its
    parent."""
    from factory import cli
    from factory.contract import ContractError

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hostile = _write_queue(scratch, "../../outside")
    (tmp_path / "froot").mkdir()
    before_root = _tree(tmp_path)

    import argparse

    args = argparse.Namespace(
        queue=str(hostile), factory_root=str(tmp_path / "froot"),
        repo=str(tmp_path),
    )
    code = cli._run_status(args)
    assert code == 1, "factory status accepted a hostile queue project"
    after_root = _tree(tmp_path)
    assert after_root == before_root, (
        "something was created or read-surfaced outside the expected tree"
    )
    assert not (tmp_path.parent / "outside").exists()

    # The importer repair path (queue with hostile project + a session
    # that refuses): the queue load itself refuses; no workspace,
    # settings or trace file is written anywhere.
    repo = scratch / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@factory.invalid")
    _git(repo, "config", "user.name", "factory test")
    (repo / "README.md").write_text("# x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    (repo / ".factory").mkdir()
    hostile_in_repo = repo / ".factory" / "tasks.json"
    hostile_in_repo.write_text(json.dumps(_queue_doc("../../outside")) + "\n")

    # Snapshot AFTER the fixture exists: the refusal must be the only
    # thing that happens from here.
    before_refusal = _tree(tmp_path)

    def refusing_session(prompt):
        raise AssertionError("the session must never start")

    with pytest.raises(ContractError):
        from factory.importer import run_import
        run_import(repo=repo, session=refusing_session,
                   factory_root=tmp_path / "froot")
    assert _tree(tmp_path) == before_refusal, (
        "the refused import created something outside the pre-existing tree"
    )
    assert not (tmp_path.parent / "outside").exists()
