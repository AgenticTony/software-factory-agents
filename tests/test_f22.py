"""F-22 — Vetted agent toolkit (skills, plugins, MCP servers).

The contract tests, exact names ``test_ac_1``..``test_ac_8`` (the queue's
acceptance refs). Red-first: this file landed before any implementation,
importing ``factory.toolkit`` that did not exist (the F-26/F-27
collection-red pattern).

Two invariants shape every fixture here:

* **No credential ever lives in a fixture.** Credential-shaped strings
  (the MCP env gate's probe, the encoded blob) are built at runtime in
  the test; the committed corpus under ``toolkit/fixtures/vet/`` carries
  only class markers (paths, phrases, patterns) that match the vet's
  classes without being secrets.
* **The vetter only reads.** Every vet test may tree-hash its candidate
  before and after; AC-3 asserts it for the corpus and the CLI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from factory.contract import ContractError, acceptance_hash, load_profile
from factory.mergeorder import Protection
from factory.orchestrator import run_once
from factory.session import SessionRecord, build_options, run_session
from factory.toolkit import (
    ALLOWED_LICENCES,
    TOOLKIT_PLUGIN_NAME,
    VET_CLASSES,
    load_index,
    resolve_toolkit,
    stage_toolkit,
    vet_candidate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLKIT_DIR = REPO_ROOT / "toolkit"
FIXTURES = TOOLKIT_DIR / "fixtures" / "vet"
STOCKHOLM = ZoneInfo("Europe/Stockholm")
# A Sunday afternoon: outside every default peak window, so runs may start.
OFF_PEAK = datetime(2026, 10, 18, 15, 0, tzinfo=STOCKHOLM)
# The vet allowlist used throughout: the profile-side domains a candidate
# may talk to. api.z.ai stands in for the model endpoint.
ALLOW = ["docs.python.org", "docs.example.com", "api.z.ai"]

MIT = textwrap.dedent(
    """
    MIT License

    Copyright (c) 2026 Fixture Upstream

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
    """
).strip() + "\n"

PROPRIETARY = "All rights reserved. No use, copying or redistribution.\n"

SKILL_MD = (
    "---\n"
    "name: demo-skill\n"
    "description: Demonstration vetted skill for the F-22 contract tests.\n"
    "---\n"
    "\n"
    "Look things up on https://docs.python.org before guessing.\n"
)

DEMO_HASH_PLACEHOLDER = "0" * 64  # replaced by _make_skill at runtime


# ---------------------------------------------------------------- helpers --


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    """``{posix relpath: sha256}`` over every file under ``root``.

    The test's own independent implementation of the content-addressing
    ``factory.toolkit`` must agree with — a drift in either changes the
    index hashes and refuses at load (AC-1).
    """
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _sha(path.read_bytes())
    return files


def _content_hash(files: dict[str, str]) -> str:
    blob = "\n".join(f"{name}\n{digest}" for name, digest in sorted(files.items()))
    return _sha(blob.encode())


def _make_skill(toolkit: Path, name: str = "demo-skill",
                licence_text: str = MIT) -> dict:
    """One vendored skill item on disk; its index entry (hashes real)."""
    item_dir = toolkit / "skills" / name
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "SKILL.md").write_text(SKILL_MD)
    (item_dir / "LICENSE").write_text(licence_text)
    files = _hash_tree(item_dir)
    return {
        "kind": "skill",
        "name": name,
        "upstream": "https://github.com/example/example-skills",
        "pin": {"commit": "a" * 40},
        "licence": "MIT",
        "content_hash": _content_hash(files),
        "files": files,
        "report": f"reports/{name}.md",
        "dir": f"skills/{name}",
    }


def _make_mcp(toolkit: Path, name: str = "docs",
              domains: list | None = None) -> dict:
    """One vendored read-only MCP server item; its index entry.

    The spawn references ``{staged}`` (review finding 3): the server
    runs from the vendored, hash-verified copy inside the staged
    toolkit — never a runtime fetch (npx/uvx/…) or an ambient install.
    """
    item_dir = toolkit / "mcp" / name
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "LICENSE").write_text(MIT)
    (item_dir / "README.md").write_text(f"# {name}\n\nRead-only docs server.\n")
    (item_dir / "server.js").write_text(
        "// stub vendored server entrypoint (review finding 3 fixture)\n"
    )
    files = _hash_tree(item_dir)
    return {
        "kind": "mcp_server",
        "name": name,
        "upstream": "https://github.com/example/example-docs-server",
        "pin": {"version": "1.2.3", "integrity": "b" * 64},
        "licence": "MIT",
        "content_hash": _content_hash(files),
        "files": files,
        "report": f"reports/{name}.md",
        "dir": f"mcp/{name}",
        "mcp": {
            "command": "node",
            "args": [f"{{staged}}/mcp/{name}/server.js"],
            # Admitted inert keys only (review round 3: descriptor env
            # is an allowlist — CACHE_MODE-style knobs were never inert
            # by construction, so the fixture uses the real admitted set).
            "env": {"NO_COLOR": "1", "TZ": "Europe/Stockholm"},
            "tools": [
                {"name": "search", "mode": "read"},
                {"name": "fetch_doc", "mode": "read"},
            ],
            "domains": domains if domains is not None else ["docs.example.com"],
        },
    }


def _write_reports(toolkit: Path, entries: list[dict]) -> None:
    """Vetting reports that name the content hash each one vetted."""
    for entry in entries:
        report = toolkit / entry["report"]
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            f"# Vet report — {entry['name']}\n\n"
            f"content hash: {entry['content_hash']}\n"
        )


def _write_index(toolkit: Path, entries: list[dict]) -> None:
    (toolkit / "index.yaml").write_text(
        yaml.safe_dump({"items": entries}, sort_keys=False)
    )


def _toolkit_with(  # one skill + one mcp server, reports and index written
    tmp: Path,
) -> tuple[Path, dict, dict]:
    toolkit = tmp / "toolkit"
    skill = _make_skill(toolkit)
    mcp = _make_mcp(toolkit)
    entries = [skill, mcp]
    _write_reports(toolkit, entries)
    _write_index(toolkit, entries)
    return toolkit, skill, mcp


PROFILE_EXTRA_BASE = {
    "name": "demo",
    "repo": {
        "path": "/tmp/demo-repo",
        "default_branch": "main",
        "remote": "https://example.com/demo.git",
    },
    "commands": {"setup": "true", "test": "true"},
    "paths": {"risk": {}, "protected": []},
    "deploy": {"mechanism": "none", "auto_on_merge": False},
    "live_checks": {},
    "docs_allowlist": ["docs.python.org", "docs.example.com"],
    "network_allowlist": [],
}


def _profile(tmp: Path, toolkit_section: dict | None = None) -> Path:
    profile = dict(PROFILE_EXTRA_BASE)
    if toolkit_section is not None:
        profile["toolkit"] = toolkit_section
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "project.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False))
    return path


def _tree_state(root: Path) -> dict[str, str]:
    return _hash_tree(root)


def _result_message(**overrides):
    """A ResultMessage with sane defaults (the f06/f27 helper shape)."""
    from claude_agent_sdk import ResultMessage

    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={
            "glm-5.3": {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.001,
                "contextWindow": 200000,
                "maxOutputTokens": 8192,
            }
        },
        terminal_reason="completed",
    )
    base.update(overrides)
    return ResultMessage(**base)


# ------------------------------------------------- AC-1: index and hashes --


def test_ac_1(tmp_path: Path):
    """The index exists, is schema-validated on every load, carries every
    admitted-item field, and refuses a vendored file that no longer
    matches its index hash."""
    toolkit, skill, mcp = _toolkit_with(tmp_path)

    index = load_index(toolkit)
    assert [i["name"] for i in index["items"]] == ["demo-skill", "docs"]
    entry = index["items"][0]
    assert entry["kind"] == "skill"
    assert entry["upstream"].startswith("https://")
    assert len(entry["pin"]["commit"]) == 40
    assert entry["licence"] == "MIT"
    assert len(entry["content_hash"]) == 64
    assert entry["report"].endswith(".md")
    assert entry["dir"] == "skills/demo-skill"

    # Schema-validated on every load: malformed shapes refuse.
    from factory.toolkit import ToolkitError

    def broken(mutate) -> None:
        entries = [_make_skill(toolkit, name="x-skill"), _make_mcp(toolkit, name="x-docs")]
        _write_reports(toolkit, entries)  # valid reports first; the
        # mutation then breaks the entry shape, not the report plumbing.
        for e in entries:
            mutate(e)
        _write_index(toolkit, entries)
        with pytest.raises(ToolkitError):
            load_index(toolkit)

    broken(lambda e: e.pop("name"))                          # required field
    broken(lambda e: e.update(kind="wizard"))                # kind enum
    broken(lambda e: e.update(licence="Proprietary"))        # licence enum
    broken(lambda e: e.update(content_hash="not-a-hash"))    # hash shape
    broken(lambda e: e.update(dir="../outside"))             # dir escape
    broken(lambda e: e["pin"].pop("commit", None))           # pin: neither form
    broken(lambda e: e.update(report="reports/missing.md"))  # report absent

    # Rebuild the good index for the tamper checks.
    _toolkit_with(tmp_path)
    # A vendored file whose content no longer matches its index hash
    # refuses at load, naming the file.
    (toolkit / "skills" / "demo-skill" / "SKILL.md").write_text("tampered\n")
    with pytest.raises(ToolkitError, match="SKILL.md"):
        load_index(toolkit)

    _toolkit_with(tmp_path)
    (toolkit / "skills" / "demo-skill" / "LICENSE").unlink()  # deleted file
    with pytest.raises(ToolkitError, match="LICENSE"):
        load_index(toolkit)

    _toolkit_with(tmp_path)
    extra = toolkit / "skills" / "demo-skill" / "unlisted.md"  # unlisted file
    extra.write_text("never went through admission\n")
    with pytest.raises(ToolkitError, match="unlisted.md"):
        load_index(toolkit)

    # The repo's own toolkit index loads (starts empty in this PR; PR 2
    # admits the first real items through the human-approved process).
    repo_index = load_index(TOOLKIT_DIR)
    assert isinstance(repo_index["items"], list)


# --------------------------------------------------- AC-2: the licence gate --


def test_ac_2(tmp_path: Path):
    """Only allowlisted SPDX licences pass; missing or proprietary
    licences are refused with the reason named. The code allowlist and
    the schema enum are the same five licences."""
    from factory.toolkit import ToolkitError, identify_licence

    assert ALLOWED_LICENCES == (
        "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
    )
    schema = json.loads((REPO_ROOT / "schema" / "toolkit.schema.json").read_text())
    entry = schema["properties"]["items"]["items"]
    enum = entry["properties"]["licence"]["enum"]
    assert sorted(enum) == sorted(ALLOWED_LICENCES)

    # The vet identifies the licence from the candidate's LICENSE file.
    assert identify_licence(MIT) == "MIT"
    assert identify_licence(PROPRIETARY) is None  # not any allowlisted grant

    clean = tmp_path / "cand-mit"
    clean.mkdir()
    (clean / "SKILL.md").write_text(SKILL_MD)
    (clean / "LICENSE").write_text(MIT)
    assert vet_candidate(clean, allowlists=ALLOW).licence == "MIT"
    assert vet_candidate(clean, allowlists=ALLOW).clean

    reserved = tmp_path / "cand-reserved"
    reserved.mkdir()
    (reserved / "SKILL.md").write_text(SKILL_MD)
    (reserved / "LICENSE").write_text(PROPRIETARY)
    result = vet_candidate(reserved, allowlists=ALLOW)
    assert not result.clean
    assert result.licence is None
    assert "All rights reserved" in result.licence_reason, result.licence_reason

    unlicensed = tmp_path / "cand-none"
    unlicensed.mkdir()
    (unlicensed / "SKILL.md").write_text(SKILL_MD)
    result = vet_candidate(unlicensed, allowlists=ALLOW)
    assert not result.clean
    assert "licence" in (result.licence_reason or "").lower()

    # The index side of the gate: an entry whose vendored LICENSE
    # contradicts its declared licence refuses at load.
    toolkit = tmp_path / "toolkit"
    lying = _make_skill(toolkit, licence_text=PROPRIETARY)  # claims MIT
    _write_reports(toolkit, [lying])
    _write_index(toolkit, [lying])
    with pytest.raises(ToolkitError, match="licen"):
        load_index(toolkit)


# ------------------------------------------------------- AC-3: the vet scan --


def test_ac_3(tmp_path: Path, capsys):
    """``factory toolkit vet`` flags one seeded fixture per class and
    passes a clean one; the encoded-blob fixture is built at runtime (no
    blob ever lives in the repo); the scan reads only."""
    assert VET_CLASSES == (
        "network", "shell", "encoded_blob", "credential_read", "env_harvest",
        "hidden_chars", "prompt_injection", "self_modify",
        # Review finding 1: symlinks/non-regular files are a class of
        # their own, beyond the contract's eight — a vendored link is an
        # exfiltration channel, not a style problem.
        "symlink",
    )

    # The clean fixture: no findings, licence admitted.
    clean = vet_candidate(FIXTURES / "clean", allowlists=ALLOW)
    assert clean.findings == [], clean.findings
    assert clean.licence == "MIT"

    # One committed fixture per scanner-safe class, each flagged under
    # its own class. (encoded_blob is runtime-built below.)
    committed = {
        "network": "network",
        "shell": "shell",
        "credential_read": "credential-read",
        "env_harvest": "env-harvest",
        "hidden_chars": "hidden-chars",
        "prompt_injection": "prompt-injection",
        "self_modify": "self-modify",
    }
    for cls, dirname in committed.items():
        result = vet_candidate(FIXTURES / dirname, allowlists=ALLOW)
        classes = {f.cls for f in result.findings}
        assert cls in classes, (dirname, result.findings)
        assert not result.clean

    # The encoded-blob class: the blob is built at runtime, never
    # committed (the corpus guard below pins that).
    blob_dir = tmp_path / "encoded-blob"
    blob_dir.mkdir()
    (blob_dir / "SKILL.md").write_text(SKILL_MD)
    (blob_dir / "LICENSE").write_text(MIT)
    blob = "QUFB" + "A" * 300  # base64-charset run far past the threshold
    (blob_dir / "payload.txt").write_text(f"payload = {blob}\n")
    result = vet_candidate(blob_dir, allowlists=ALLOW)
    blobs = [f for f in result.findings if f.cls == "encoded_blob"]
    assert blobs, result.findings
    # The finding points at the file and line without echoing the blob.
    assert blobs[0].file == "payload.txt"
    assert blob[:60] not in blobs[0].detail

    # Guard: no committed fixture carries an encoded blob or a literal
    # credential shape — those live only in runtime-built candidates.
    assert not (FIXTURES / "encoded-blob").exists()
    for path in sorted(FIXTURES.rglob("*")):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not re.search(r"[A-Za-z0-9+/=]{256,}", text), path
            for prefix in ("ghp_", "sk_live_", "AKIA"):
                assert prefix not in text, path

    # The vetter reads only: the scanned trees are byte-identical after
    # both the direct scans and the CLI runs below.
    before = {
        dirname: _tree_state(FIXTURES / dirname) for dirname in committed.values()
    }
    # The CLI end to end: clean exits 0; a seeded fixture exits 1 naming
    # its class on stdout.
    from factory.cli import main as cli_main

    profile_path = _profile(tmp_path)
    assert cli_main(["toolkit", "vet", str(FIXTURES / "clean"),
                     "--profile", str(profile_path)]) == 0
    code = cli_main(["toolkit", "vet", str(FIXTURES / "prompt-injection"),
                     "--profile", str(profile_path)])
    assert code == 1
    out = capsys.readouterr().out
    assert "prompt_injection" in out
    for name, state in before.items():
        assert _tree_state(FIXTURES / name) == state, name


# --------------------------------------------------- AC-4: admission is human --


def test_ac_4(tmp_path: Path, capsys):
    """A clean scan never admits: vetting writes nothing to the index,
    no admitting subcommand exists, every entry's report exists and
    names the content hash it vetted (a version bump cannot ride an old
    report), and aggregator repos are discovery-only — the index pins
    the real upstream."""
    from factory.cli import main as cli_main
    from factory.toolkit import ToolkitError

    toolkit, skill, mcp = _toolkit_with(tmp_path)
    index_bytes = (toolkit / "index.yaml").read_bytes()

    # A clean scan through the real CLI leaves the index untouched.
    clean = tmp_path / "candidate"
    clean.mkdir()
    (clean / "SKILL.md").write_text(SKILL_MD)
    (clean / "LICENSE").write_text(MIT)
    assert cli_main(["toolkit", "vet", str(clean), "--profile",
                     str(_profile(tmp_path))]) == 0
    assert (toolkit / "index.yaml").read_bytes() == index_bytes

    # No admitting command: the toolkit group offers exactly `vet`.
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["toolkit", "bogus"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "vet" in err and "admit" not in err and "add" not in err

    # Every entry's report exists (AC-1's load check) and names the
    # content hash it vetted: bump the content, keep the old report —
    # the load refuses.
    (toolkit / "skills" / "demo-skill" / "SKILL.md").write_text(
        SKILL_MD + "v2: new content, same old report\n"
    )
    files = _hash_tree(toolkit / "skills" / "demo-skill")
    skill2 = dict(skill, content_hash=_content_hash(files), files=files)
    _write_index(toolkit, [skill2, mcp])
    with pytest.raises(ToolkitError, match="report"):
        load_index(toolkit)

    # Aggregators are discovery-only: an upstream pointing at one
    # refuses at load, naming the aggregator.
    _toolkit_with(tmp_path)
    aggregator = _make_skill(toolkit, name="agg-skill")
    aggregator["upstream"] = "https://mcpmarket.com/server/demo"
    _write_reports(toolkit, [aggregator])
    _write_index(toolkit, [aggregator])
    with pytest.raises(ToolkitError, match="mcpmarket"):
        load_index(toolkit)


# ------------------------------------------------- AC-5: MCP admission rules --


def test_ac_5(tmp_path: Path):
    """MCP servers are admitted only read-only, credential-free,
    allowlisted-domain, version+integrity pinned; a session enables
    their tools one by one through allowed_tools and runs them from the
    staged, sandbox-confined descriptor."""
    from factory.toolkit import ToolkitError, vet_descriptor

    # A write tool refuses (the descriptor gate the vet runs). The base
    # descriptor spawns from {staged} — the vendored, hash-verified copy
    # (review finding 3).
    def descriptor(**overrides):
        base = {
            "command": "node",
            "args": ["{staged}/mcp/docs/server.js"],
            "env": {"NO_COLOR": "1"},
            "tools": [{"name": "search", "mode": "read"}],
            "domains": ["docs.example.com"],
        }
        base.update(overrides)
        return base

    write_tool = descriptor(tools=[{"name": "deploy", "mode": "write"}])
    reasons = vet_descriptor(write_tool, allowlists=ALLOW)
    assert any("write" in r and "deploy" in r for r in reasons), reasons

    # A send tool refuses too (writing is sending, to an L2 reviewer's
    # eye as much as a filesystem's).
    send_tool = descriptor(tools=[{"name": "send_email", "mode": "send"}])
    assert vet_descriptor(send_tool, allowlists=ALLOW) != []

    # Credential-shaped env refuses: the probe string is built at
    # runtime and never written to any file.
    token_env = descriptor(env={"SERVICE_TOKEN": "ghp_" + "0" * 36})
    reasons = vet_descriptor(token_env, allowlists=ALLOW)
    assert any("SERVICE_TOKEN" in r for r in reasons), reasons

    # A domain outside the project's allowlists refuses.
    offlist = descriptor(domains=["collector.evil.example"])
    reasons = vet_descriptor(offlist, allowlists=ALLOW)
    assert any("collector.evil.example" in r for r in reasons), reasons

    # The clean descriptor passes the gate.
    assert vet_descriptor(descriptor(), allowlists=ALLOW) == []

    # The index holds the same line: a write-tool or credential-env
    # entry refuses at load (schema + load checks).
    toolkit = tmp_path / "toolkit"
    writer = _make_mcp(toolkit, name="writer")
    writer["mcp"]["tools"] = [{"name": "deploy", "mode": "write"}]
    _write_reports(toolkit, [writer])
    _write_index(toolkit, [writer])
    with pytest.raises(ToolkitError, match="deploy"):
        load_index(toolkit)

    # Resolve re-checks the domains against the *running project's*
    # allowlists: an index admitted for one project does not smuggle a
    # domain into another.
    toolkit, skill, mcp = _toolkit_with(tmp_path)
    evil_toolkit = tmp_path / "toolkit2"
    evil = _make_mcp(evil_toolkit, name="docs", domains=["collector.evil.example"])
    _write_reports(evil_toolkit, [evil])
    _write_index(evil_toolkit, [evil])
    profile = yaml.safe_load(_profile(tmp_path, {
        "skills": [], "plugins": [], "mcp_servers": ["docs"],
    }).read_text())
    with pytest.raises(ToolkitError, match="collector.evil.example"):
        resolve_toolkit(profile, "implementer", toolkit_root=evil_toolkit)

    # The session side: tools enabled one by one via allowed_tools, the
    # server spawned from the staged descriptor's exact command/args/env.
    resolved = resolve_toolkit(profile, "implementer", toolkit_root=toolkit)
    staged = stage_toolkit(resolved, tmp_path / "staged-toolkit")
    spec = {
        "staged": str(staged),
        "items": resolved["items"],
    }
    options = build_options(
        "implementer", tmp_path, 5,
        base_url="https://api.z.ai/api/anthropic", token="t", toolkit=spec,
    )
    assert options.strict_mcp_config is True
    server = options.mcp_servers["docs"]
    assert server["command"] == "node"
    # The {staged} placeholder is substituted with the staged root at
    # build time (review finding 3): the spawn runs the vendored,
    # hash-verified copy, not an ambient path.
    assert server["args"] == [f"{staged}/mcp/docs/server.js"]
    # Exactly the descriptor's admitted keys, on top of the spawn's
    # inert neutralizers (review round 3) — no other name crosses.
    assert server["env"]["NO_COLOR"] == "1"
    assert server["env"]["NODE_OPTIONS"] == "" and "CACHE_MODE" not in server["env"]
    tools = [t for t in options.allowed_tools if t.startswith("mcp__docs__")]
    assert sorted(tools) == ["mcp__docs__fetch_doc", "mcp__docs__search"]
    # The staged descriptor rides the read-only staged copy (AC-7's
    # pattern) — the JSON is there for inspection inside the sandbox.
    assert json.loads((staged / "mcp" / "docs.json").read_text())["command"] == "node"


# ------------------------------------------------ AC-6: per-project selection --


def test_ac_6(tmp_path: Path):
    """Each project chooses its toolkit in project.yaml; every name must
    be an index entry (kind included) and an unknown or mismatched name
    refuses the profile at load, as F-02 refuses invalid state. Seats
    may narrow the choice, never widen it."""
    toolkit, skill, mcp = _toolkit_with(tmp_path)

    good = _profile(tmp_path, {
        "skills": ["demo-skill"],
        "plugins": [],
        "mcp_servers": ["docs"],
        "seats": {"reviewer": {"skills": [], "mcp_servers": ["docs"]}},
    })
    profile = load_profile(good, toolkit_root=toolkit)
    assert profile["toolkit"]["skills"] == ["demo-skill"]

    # Unknown names refuse, named.
    unknown = _profile(tmp_path / "a", {"skills": ["ghost-skill"],
                                        "plugins": [], "mcp_servers": []})
    with pytest.raises(ContractError, match="ghost-skill"):
        load_profile(unknown, toolkit_root=toolkit)

    # A name in the wrong list (kind mismatch) refuses.
    mismatch = _profile(tmp_path / "b", {"skills": [],
                                         "plugins": [],
                                         "mcp_servers": ["demo-skill"]})
    with pytest.raises(ContractError, match="demo-skill"):
        load_profile(mismatch, toolkit_root=toolkit)

    # A seat may narrow, never widen: reviewer listing an item the
    # project does not refuses.
    widened = _profile(tmp_path / "c", {
        "skills": ["demo-skill"], "plugins": [], "mcp_servers": [],
        "seats": {"reviewer": {"skills": [], "mcp_servers": ["docs"]}},
    })
    with pytest.raises(ContractError, match="docs"):
        load_profile(widened, toolkit_root=toolkit)

    # Narrowing resolves: the reviewer seat gets its read-only subset.
    profile = load_profile(good, toolkit_root=toolkit)
    full = resolve_toolkit(profile, "implementer", toolkit_root=toolkit)
    narrowed = resolve_toolkit(profile, "reviewer", toolkit_root=toolkit)
    assert [i["name"] for i in full["items"]] == ["demo-skill", "docs"]
    assert [i["name"] for i in narrowed["items"]] == ["docs"]

    # The repo's real schema carries the toolkit shape (an unknown key
    # inside toolkit refuses, a seat beyond the two known refuses).
    import jsonschema

    schema = json.loads((REPO_ROOT / "schema" / "project.schema.json").read_text())
    valid = {
        "name": "demo", "repo": {"path": "/x", "default_branch": "main",
                                 "remote": "https://example.com/d.git"},
        "commands": {"setup": "true", "test": "true"},
        "paths": {"risk": {}, "protected": []},
        "deploy": {"mechanism": "none", "auto_on_merge": False},
        "live_checks": {}, "docs_allowlist": [],
        "toolkit": {"skills": ["a"], "plugins": [], "mcp_servers": [],
                    "seats": {"reviewer": {"skills": [], "plugins": [],
                                           "mcp_servers": []}}},
    }
    jsonschema.Draft202012Validator(schema).validate(valid)
    bad = dict(valid, toolkit={"skills": ["a"], "honk": []})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(bad)
    bad = dict(valid, toolkit={"skills": ["a"], "seats": {"pilot": {}}})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(bad)


# ------------------------------------------- AC-7: exactly the project's toolkit --


def test_ac_7(tmp_path: Path, monkeypatch):
    """A session gets exactly its project's toolkit and nothing from the
    owner's personal Claude setup (a planted skill and MCP server stay
    invisible); the toolkit loads from a read-only staged copy outside
    the clone, and the sandboxed spec carries it."""
    # Plant a personal setup in a fake home: a skill and an MCP server.
    home = tmp_path / "fake-home"
    skill_dir = home / ".claude" / "skills" / "personal-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: personal-skill\ndescription: owner's own.\n---\n"
    )
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "mcpServers": {"personal-mcp": {"command": "npx", "args": ["-y", "personal"]}},
    }))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)

    toolkit, skill, mcp = _toolkit_with(tmp_path)
    profile = load_profile(_profile(tmp_path, {
        "skills": ["demo-skill"], "plugins": [], "mcp_servers": ["docs"],
    }), toolkit_root=toolkit)
    resolved = resolve_toolkit(profile, "implementer", toolkit_root=toolkit)

    # Staged copy OUTSIDE the clone (the F-06 runner-staging pattern).
    run_dir = tmp_path / "runs" / "demo" / "20260101T000000Z-ab12cd"
    run_dir.mkdir(parents=True)
    settings_path = tmp_path / "sandbox" / "demo-20260101T000000Z-ab12cd.json"
    staged = stage_toolkit(resolved, settings_path.with_name(
        settings_path.name + "-toolkit"))
    assert run_dir not in staged.parents and staged not in run_dir.parents
    assert not (run_dir / "toolkit").exists()
    wrapper = json.loads((staged / ".claude-plugin" / "plugin.json").read_text())
    assert wrapper["name"] == TOOLKIT_PLUGIN_NAME
    assert (staged / "skills" / "demo-skill" / "SKILL.md").read_bytes() == (
        toolkit / "skills" / "demo-skill" / "SKILL.md"
    ).read_bytes()

    # The sandbox settings: the staged copy is readable, never writable.
    from factory.sandbox import build_settings

    settings = build_settings(
        profile, run_dir, model_endpoint="https://api.z.ai/api/anthropic",
        allow_read=[staged],
    )
    fs = settings["filesystem"]
    assert str(staged) in fs["allowRead"]
    writable = [Path(w) for w in fs["allowWrite"]]
    assert not any(w == staged or w in staged.parents for w in writable)

    # The options: exactly the project's items; the planted personal
    # skill and MCP server are nowhere; setting_sources stays [].
    spec = {"staged": str(staged), "items": resolved["items"]}
    captured: dict = {}

    async def query(*, prompt, options, transport=None):
        captured["options"] = options
        yield _result_message()

    record = asyncio.run(run_session(
        "implementer", "do it", run_dir, 4, 5,
        query_fn=query, base_url="https://api.z.ai/api/anthropic", token="t",
        toolkit=spec,
    ))
    assert record.status == "completed"
    options = captured["options"]
    assert options.setting_sources == []
    assert options.skills == [f"{TOOLKIT_PLUGIN_NAME}:demo-skill"]
    assert {p["path"] for p in options.plugins} == {str(staged)}
    assert "docs" in options.mcp_servers and "vent" in options.mcp_servers
    assert "personal-mcp" not in options.mcp_servers
    assert "personal-skill" not in (options.skills or [])
    dumped = json.dumps(options.model_dump()) if hasattr(options, "model_dump") \
        else json.dumps(vars(options), default=str)
    assert "personal-skill" not in dumped and "personal-mcp" not in dumped
    assert sorted(
        t for t in options.allowed_tools if t.startswith("mcp__docs__")
    ) == ["mcp__docs__fetch_doc", "mcp__docs__search"]

    # The sandboxed entry: the spec file carries the toolkit, staged
    # outside the clone, and the settings allow exactly that read.
    from factory import session as session_mod

    spec_seen: dict = {}

    def fake_run_sandboxed(cmd, run_dir_arg, settings_arg, **kwargs):
        spec_seen["path"] = cmd[-1]
        spec_seen["settings"] = settings_arg
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps(SessionRecord(
                seat="implementer", status="completed").to_dict()) + "\n", "")

    monkeypatch.setattr(session_mod, "run_sandboxed", fake_run_sandboxed)
    session_mod.run_session_sandboxed(
        seat="implementer", prompt="p", run_dir=run_dir, profile=profile,
        max_turns=1, timeout_min=1, token="stub-token",
        toolkit=resolved, factory_root=tmp_path,
    )
    spec = json.loads(Path(spec_seen["path"]).read_text())
    assert spec["toolkit"]["staged"] == str(staged)
    assert Path(spec["toolkit"]["staged"]) not in run_dir.parents
    assert [i["name"] for i in spec["toolkit"]["items"]] == ["demo-skill", "docs"]
    written = json.loads(Path(spec_seen["settings"]).read_text())
    assert str(staged) in written["filesystem"]["allowRead"]


# ------------------------------------------------- AC-8: attempts record skills --


def test_ac_8(tmp_path: Path, monkeypatch):
    """Every attempt's ``skills`` records what actually loaded as
    ``<name>@<content-hash>``, from the session record into tasks.json —
    replacing the hardcoded empty list (a session-less sweep attempt
    still records the honest empty list)."""
    toolkit, skill, mcp = _toolkit_with(tmp_path)
    expected = sorted([
        f"{skill['name']}@{skill['content_hash']}",
        f"{mcp['name']}@{mcp['content_hash']}",
    ])

    # The runner stamps its record from the toolkit it actually loaded.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec = {"staged": str(tmp_path / "staged"), "items": [skill, mcp]}

    async def query(*, prompt, options, transport=None):
        yield _result_message()

    record = asyncio.run(run_session(
        "implementer", "p", run_dir, 2, 1,
        query_fn=query, base_url="https://api.z.ai/api/anthropic", token="t",
        toolkit=spec,
    ))
    assert sorted(record.skills) == expected

    # The orchestrator end to end: profile toolkit -> per-seat resolve ->
    # session -> attempt["skills"] in tasks.json (stub seats mimic the
    # runner's stamping from the toolkit kwarg they were handed).
    import factory.toolkit as toolkit_mod

    monkeypatch.setattr(toolkit_mod, "default_toolkit_dir", lambda: toolkit)

    repo = tmp_path / "proj"
    remote = tmp_path / "remote.git"

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    repo.mkdir()
    git("init", "-b", "main")
    git("config", "user.email", "fixture@example.com")
    git("config", "user.name", "Fixture")
    (repo / "impl.py").write_text("def answer():\n    return 0\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text(
        "def test_base():\n    assert True\n"
    )
    ref = "tests/test_feature.py::test_ac_1"
    acceptance = [{"id": "AC-1", "text": "answer() returns 42",
                   "check": {"type": "test", "ref": ref}}]
    task = {
        "id": "T-1", "title": "Make answer() return 42", "source": "docs/T-1.md",
        "kind": "build", "priority": "P1", "status": "ready", "depends_on": [],
        "risk": [], "gates": {"spec_approval": True, "merge": "human",
                              "deploy": "human"},
        "acceptance": acceptance, "acceptance_hash": acceptance_hash(acceptance),
        "approved_at": "2026-10-18T10:00:00Z", "approved_by": "owner",
        "budget": {"size": "S", "max_attempts": 3, "max_turns": 20,
                   "wall_clock_min": 10},
        "attempts": [],
    }
    profile = dict(PROFILE_EXTRA_BASE)
    profile["repo"] = {"path": str(repo), "default_branch": "main",
                       "remote": str(remote)}
    profile["commands"] = {
        "setup": "true",
        "test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
    }
    profile["toolkit"] = {"skills": ["demo-skill"], "plugins": [],
                          "mcp_servers": ["docs"],
                          "seats": {"reviewer": {"skills": [],
                                                 "mcp_servers": ["docs"]}}}
    factory_dir = repo / ".factory"
    factory_dir.mkdir()
    profile_path = factory_dir / "project.yaml"
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
    (factory_dir / "tasks.json").write_text(json.dumps(
        {"version": 1, "project": "demo", "tasks": [task]}, indent=2) + "\n")
    (repo / ".gitignore").write_text(".factory/*.lock\n")
    git("add", "-A")
    git("commit", "-m", "base")
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(remote)],
                   check=True)

    test_source = textwrap.dedent(
        """
        from impl import answer

        def test_ac_1():
            assert answer() == 42
        """
    )
    review_pass = json.dumps({"verdict": "pass", "findings": []})
    seat_toolkits: dict[str, list] = {}

    def seat(name, prompt, run_dir_arg, prof, **kwargs):
        tk = kwargs.get("toolkit")
        if tk is not None:
            seat_toolkits[name] = [i["name"] for i in tk["items"]]
            loaded = sorted(f"{i['name']}@{i['content_hash']}"
                            for i in tk["items"])
        else:
            loaded = []
        if name == "implementer":
            run_dir_arg = Path(run_dir_arg)
            (run_dir_arg / "impl.py").write_text(
                "def answer():\n    return 42\n"
            )
            (run_dir_arg / "tests").mkdir(exist_ok=True)
            (run_dir_arg / "tests" / "test_feature.py").write_text(test_source)
            return SessionRecord(seat=name, status="completed", skills=loaded)
        return SessionRecord(seat=name, status="completed", skills=loaded,
                             result=review_pass)

    result = run_once(
        factory_dir / "tasks.json",
        factory_root=tmp_path / "factory-root",
        session_runner=seat,
        open_pr=lambda pr: "https://github.com/demo/demo/pull/1",
        now=lambda: OFF_PEAK,
        protection=lambda r, b: Protection(checked=True, strict=True),
        pr_state=lambda url: "open",
    )
    assert result.outcome == "pr_opened", result.outcome
    # The implementer got the full toolkit, the reviewer its narrowed subset.
    assert seat_toolkits["implementer"] == ["demo-skill", "docs"]
    assert seat_toolkits["reviewer"] == ["docs"]
    attempts = json.loads(
        (factory_dir / "tasks.json").read_text())["tasks"][0]["attempts"]
    assert attempts, "the run recorded its attempt"
    assert sorted(attempts[-1]["skills"]) == expected, attempts[-1]


# ------------------------------------------------------ focused extra checks --


def test_content_hash_is_tree_order_independent(tmp_path: Path):
    """The content hash addresses the file tree, not its inode order:
    the same files staged in a different creation order hash the same."""
    from factory.toolkit import content_hash_of

    a, b = tmp_path / "a", tmp_path / "b"
    for root, order in ((a, ["SKILL.md", "LICENSE"]), (b, ["LICENSE", "SKILL.md"])):
        root.mkdir()
        for name in order:
            (root / name).write_text(f"content of {name}\n")
    assert content_hash_of(a) == content_hash_of(b) == _content_hash(
        _hash_tree(a)
    )


def test_vet_refuses_a_missing_candidate(tmp_path: Path):
    """A candidate path that is not a readable directory is a clean
    refusal, never a traceback or a silent pass."""
    from factory.toolkit import ToolkitError

    with pytest.raises(ToolkitError, match="candidate"):
        vet_candidate(tmp_path / "nope", allowlists=ALLOW)
    file_candidate = tmp_path / "file.txt"
    file_candidate.write_text("not a directory\n")
    with pytest.raises(ToolkitError, match="candidate"):
        vet_candidate(file_candidate, allowlists=ALLOW)


def test_benign_env_access_is_not_harvest(tmp_path: Path):
    """A single env read is the safe pattern everywhere in this repo
    (`os.environ.get(...)`); harvesting is bulk collection — the vet
    flags the latter, not the former."""
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "LICENSE").write_text(MIT)
    (cand / "helper.py").write_text(
        "import os\n"
        "token = os.environ.get('MY_TOKEN', '')\n"
    )
    result = vet_candidate(cand, allowlists=ALLOW)
    assert [f for f in result.findings if f.cls == "env_harvest"] == []


def test_staged_toolkit_is_cleaned_up(tmp_path: Path, monkeypatch):
    """The F-06 cleanup contract covers the staged toolkit: a session
    with a toolkit leaves no ``-toolkit`` copy under ``sandbox/`` after
    ``cleanup_run`` (the staged copy is per-session, never a cache)."""
    from factory import session as session_mod
    from factory.runs import RunDir, cleanup_run

    run_root = tmp_path / "runs" / "demo" / "20260101T000000Z-ab12cd"
    run_root.mkdir(parents=True)
    settings_path = tmp_path / "sandbox" / "demo-20260101T000000Z-ab12cd.json"
    toolkit, _skill, _mcp = _toolkit_with(tmp_path / "tk")
    resolved = {"root": str(toolkit), "items": [_skill]}

    def fake_run_sandboxed(cmd, run_dir_arg, settings_arg, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps(SessionRecord(
                seat="implementer", status="completed").to_dict()) + "\n", "")

    monkeypatch.setattr(session_mod, "run_sandboxed", fake_run_sandboxed)
    session_mod.run_session_sandboxed(
        seat="implementer", prompt="p", run_dir=run_root, profile={},
        max_turns=1, timeout_min=1, token="stub-token",
        toolkit=resolved, factory_root=tmp_path,
    )
    staged = settings_path.with_name(settings_path.name + "-toolkit")
    assert staged.is_dir() and (staged / "skills" / "demo-skill").is_dir()
    cleanup_run(RunDir("demo", "20260101T000000Z-ab12cd", run_root,
                       settings_path))
    assert not staged.exists()
    assert not settings_path.exists()
    assert not settings_path.with_name(settings_path.name + "-runner").exists()


def test_reclaim_sweep_removes_a_stray_staged_toolkit(tmp_path: Path):
    """The reclaim path too: an abandoned run's stray ``-toolkit`` dir
    under ``sandbox/`` is swept like ``-runner``/``-scratch`` (the F-26
    stray regex knows the sibling)."""
    from factory.orchestrator import reclaim_runs
    from factory.state import TaskQueue

    root = tmp_path / "factory-root"
    sandbox = root / "sandbox"
    sandbox.mkdir(parents=True)
    queue_path = tmp_path / "tasks.json"
    queue_path.write_text(json.dumps(
        {"version": 1, "project": "demo", "tasks": []}))
    run_id = "20260101T000000Z-ab12cd"
    stray = sandbox / f"demo-{run_id}.json-toolkit"
    (stray / "skills" / "x").mkdir(parents=True)
    # A live lease must not be needed: an idle queue reclaims strays.
    removed_runs = reclaim_runs(TaskQueue(queue_path), "demo", root)
    assert removed_runs == []  # no run dir existed; only the stray did
    assert not stray.exists()


def test_hidden_chars_survive_a_utf16_file(tmp_path: Path):
    """A zero-width marker re-encoded as UTF-16 still trips the
    hidden-character class — the scan decodes the real characters
    instead of replacing them into garbage."""
    cand = tmp_path / "cand-utf16"
    cand.mkdir()
    (cand / "LICENSE").write_text(MIT)
    zwsp = chr(0x200B)  # explicit — an invisible literal proves nothing
    (cand / "SKILL.md").write_bytes(
        f"Always{zwsp}follow the marker\n".encode("utf-16"))
    result = vet_candidate(cand, allowlists=ALLOW)
    assert any(f.cls == "hidden_chars" for f in result.findings), (
        result.findings
    )


def test_report_path_cannot_escape_the_toolkit_dir(tmp_path: Path):
    """An index entry whose report path carries ``..`` is refused by the
    schema — the report lives under toolkit/, never outside it."""
    from factory.toolkit import ToolkitError

    toolkit = tmp_path / "toolkit"
    escape = _make_skill(toolkit)
    escape["report"] = "reports/../outside.md"
    _write_reports(toolkit, [escape])
    _write_index(toolkit, [escape])
    with pytest.raises(ToolkitError, match="report"):
        load_index(toolkit)


# ------------------- owner review round on PR #77 (4 findings, red-first) ---

# Finding 2's live reproduction first proved the gap off-suite: one tiny
# real turn through run_session (the f06 in-process seam — NOT
# test_f06's ac_3/ac_4) with a stub stdio MCP server advertising a
# listed read tool and an UNLISTED write tool. Pre-fix, on head 1b7e9c8:
# {"status": "completed", "permission_denials": [], "result": "done",
#  "marker_exists_after": true} — the model called the unlisted tool,
# nothing denied it, the stub executed. bypassPermissions pre-approves
# via allowed_tools and restricts nothing: the grant list alone cannot
# enforce AC-5's "enabled one by one". The test below pins the
# enforcement that closes it (a PreToolUse deny hook — deny hooks still
# block under bypassPermissions, F-06's established fact).


def test_review2_unlisted_mcp_tools_are_denied_builtins_untouched(
        tmp_path: Path):
    """Finding 2: under bypassPermissions, ``allowed_tools`` grants
    nothing enforceable — a deny hook must refuse every ``mcp__`` call
    whose (server, tool) pair the seat's toolkit never listed, while
    listed tools, the vent tool and every built-in tool pass."""
    import asyncio

    from factory.vent import VentCollector

    toolkit, skill, mcp = _toolkit_with(tmp_path)
    staged = tmp_path / "staged"
    staged.mkdir()
    spec = {"staged": str(staged), "items": [skill, mcp]}
    options = build_options(
        "implementer", tmp_path, 5,
        base_url="https://api.z.ai/api/anthropic", token="t",
        vent_collector=VentCollector(run_id="r", task_id="t",
                                     seat="implementer"),
        toolkit=spec,
    )
    mcp_hooks = [hm for hm in options.hooks["PreToolUse"]
                 if hm.matcher and hm.matcher.startswith("mcp__")]
    assert mcp_hooks, "no MCP guard hook is wired into the session options"

    async def decision(tool_name: str) -> str:
        verdicts = []
        for matcher in mcp_hooks:
            for callback in matcher.hooks:
                verdict = await callback(
                    {"tool_name": tool_name, "tool_input": {}}, None, {})
                if verdict:
                    verdicts.append(verdict)
        assert len(verdicts) <= 1, "one deny is enough; no stacking"
        out = verdicts[0] if verdicts else {}
        decision_value = (out.get("hookSpecificOutput") or {}).get(
            "permissionDecision")
        return decision_value or "allow"

    # Unlisted tools of a listed server are denied — the exact live
    # reproduction's shape (docs advertises write_doc at runtime; the
    # descriptor never listed it).
    assert asyncio.run(decision("mcp__docs__write_doc")) == "deny"
    # Tools of a server that is not in the seat's toolkit at all.
    assert asyncio.run(decision("mcp__ghost__read")) == "deny"
    # Malformed mcp names never match a grant.
    assert asyncio.run(decision("mcp__docs")) == "deny"
    assert asyncio.run(decision("mcp__vent__evil")) == "deny"
    # Listed tools of toolkit servers pass…
    assert asyncio.run(decision("mcp__docs__search")) == "allow"
    assert asyncio.run(decision("mcp__docs__fetch_doc")) == "allow"
    # …the vent tool (an in-process server, not a toolkit one) passes…
    assert asyncio.run(decision("mcp__vent__vent")) == "allow"
    # …and built-in tools are untouched by this hook.
    assert asyncio.run(decision("Bash")) == "allow"
    assert asyncio.run(decision("Write")) == "allow"


def test_review1_symlinks_and_non_regular_files_refused_everywhere(
        tmp_path: Path):
    """Finding 1: a vendored symlink (file or dir) or any non-regular
    file is refused at vet (its own class), at load, and at staging —
    and the link target's content never reaches the staged copy or a
    content hash. Pre-fix, ``rglob``+``is_file`` and ``copytree``
    (symlinks=False) both FOLLOWED the link: the staged copy held the
    outside file's content as a regular file, readable by the session."""
    import os

    from factory.toolkit import ToolkitError

    outside = tmp_path / "outside" / "secret-notes.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("OUTSIDE-CONTENT-MARKER\n")

    # --- vet: a symlinked file is a finding of its own class, and the
    # link target's content never enters the candidate's content hash.
    cand = tmp_path / "cand-link"
    cand.mkdir()
    (cand / "LICENSE").write_text(MIT)
    (cand / "SKILL.md").write_text(SKILL_MD)
    (cand / "link.txt").symlink_to(outside)
    result = vet_candidate(cand, allowlists=ALLOW)
    links = [f for f in result.findings if f.cls == "symlink"]
    assert any(f.file == "link.txt" for f in links), result.findings
    # Hashed like the link never existed: exactly the regular files —
    # the link target's content never enters the hash.
    assert result.content_hash == _content_hash({
        "LICENSE": _sha((cand / "LICENSE").read_bytes()),
        "SKILL.md": _sha((cand / "SKILL.md").read_bytes()),
    })

    # --- vet: a symlinked directory and a fifo are findings too.
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    (real_dir / "x.md").write_text("x\n")
    cand2 = tmp_path / "cand-dirlink"
    cand2.mkdir()
    (cand2 / "LICENSE").write_text(MIT)
    (cand2 / "SKILL.md").write_text(SKILL_MD)
    (cand2 / "sub").symlink_to(real_dir)
    result = vet_candidate(cand2, allowlists=ALLOW)
    assert any(f.cls == "symlink" and f.file == "sub" for f in result.findings)

    cand3 = tmp_path / "cand-fifo"
    cand3.mkdir()
    (cand3 / "LICENSE").write_text(MIT)
    (cand3 / "SKILL.md").write_text(SKILL_MD)
    os.mkfifo(cand3 / "pipe")
    result = vet_candidate(cand3, allowlists=ALLOW)
    assert any(f.cls == "symlink" and f.file == "pipe" for f in result.findings)

    # --- load: a vendored item carrying a symlink refuses the index,
    # the link named.
    toolkit, skill, mcp = _toolkit_with(tmp_path / "tk1")
    (toolkit / "skills" / "demo-skill" / "link.txt").symlink_to(outside)
    with pytest.raises(ToolkitError, match="link.txt"):
        load_index(toolkit)

    # --- stage: refusing pre-copy, nothing staged — the outside
    # content never appears in any staged copy.
    toolkit2, skill2, mcp2 = _toolkit_with(tmp_path / "tk2")
    resolved = {
        "root": str(toolkit2),
        "items": [skill2, mcp2],
    }
    (toolkit2 / "mcp" / "docs" / "link.txt").symlink_to(outside)
    dest = tmp_path / "staged-out"
    with pytest.raises(ToolkitError, match="link.txt"):
        stage_toolkit(resolved, dest)
    assert not dest.exists()
    assert not list(tmp_path.glob("staged-out/**/*"))


def test_review3_mcp_spawn_must_run_hash_verified_vendored_content(
        tmp_path: Path):
    """Finding 3: the pin was declarative. A descriptor whose command
    fetches at spawn (npx/uvx/bunx/pipx/pip install/curl/wget/ssh) is
    refused, and so is one that never references ``{staged}`` — the
    server must run from the vendored, hash-verified copy inside the
    staged toolkit, so the index pin describes what actually executes."""
    from factory.toolkit import ToolkitError, vet_descriptor

    def descriptor(**overrides):
        base = {
            "command": "node",
            "args": ["{staged}/mcp/docs/server.js"],
            "env": {"NO_COLOR": "1"},
            "tools": [{"name": "search", "mode": "read"}],
            "domains": ["docs.example.com"],
        }
        base.update(overrides)
        return base

    # Fetch-runners refuse, the runner named.
    for command, args in (
        ("npx", ["-y", "docs-server@1.2.3"]),
        ("uvx", ["docs-server"]),
        ("bunx", ["docs-server"]),
        ("pipx", ["run", "docs-server"]),
        ("pip", ["install", "docs-server"]),
        ("python", ["-m", "pip", "install", "docs-server"]),
        ("curl", ["-sSL", "https://get.docs.example/server.sh"]),
        ("wget", ["https://get.docs.example/server.sh"]),
        ("ssh", ["host", "docs-server"]),
    ):
        reasons = vet_descriptor(descriptor(command=command, args=args),
                                 allowlists=ALLOW)
        assert any(command in r for r in reasons), (command, reasons)

    # A spawn that never touches the staged copy refuses: an ambient
    # install is unpinned by construction.
    reasons = vet_descriptor(descriptor(args=["/usr/local/server.js"]),
                             allowlists=ALLOW)
    assert any("{staged}" in r for r in reasons), reasons

    # The vendored form passes.
    assert vet_descriptor(descriptor(), allowlists=ALLOW) == []

    # The index holds the same line: a fetch-runner entry refuses at
    # load.
    toolkit = tmp_path / "toolkit"
    fetcher = _make_mcp(toolkit, name="fetcher")
    fetcher["mcp"]["command"] = "npx"
    fetcher["mcp"]["args"] = ["-y", "docs-server@1.2.3"]
    _write_reports(toolkit, [fetcher])
    _write_index(toolkit, [fetcher])
    with pytest.raises(ToolkitError, match="npx"):
        load_index(toolkit)


def test_review4_staged_copy_is_reverified_after_staging(tmp_path: Path):
    """Finding 4: resolve verified the source once; the copy was never
    checked. stage_toolkit now verifies the source before copying AND
    the staged copy against the index hashes after — a mutation between
    resolve and stage (or during the copy) refuses, and no partial
    staged tree survives."""
    from factory.toolkit import ToolkitError

    toolkit, skill, mcp = _toolkit_with(tmp_path)
    profile = yaml.safe_load(_profile(tmp_path, {
        "skills": ["demo-skill"], "plugins": [], "mcp_servers": ["docs"],
    }).read_text())
    resolved = resolve_toolkit(profile, "implementer", toolkit_root=toolkit)

    # Mutate the source after resolve, before stage.
    (toolkit / "skills" / "demo-skill" / "SKILL.md").write_text(
        SKILL_MD + "swapped after resolve\n"
    )
    dest = tmp_path / "staged-review4"
    with pytest.raises(ToolkitError, match="SKILL.md"):
        stage_toolkit(resolved, dest)
    assert not dest.exists(), "a refused stage leaves no partial copy"

    # Happy path: the staged copy's per-file hashes equal the index's
    # (recomputed on the copy itself, not trusted from the source).
    toolkit2, skill2, mcp2 = _toolkit_with(tmp_path / "tk-ok")
    profile2 = yaml.safe_load(_profile(tmp_path / "tk-ok", {
        "skills": ["demo-skill"], "plugins": [], "mcp_servers": ["docs"],
    }).read_text())
    resolved2 = resolve_toolkit(profile2, "implementer",
                                toolkit_root=toolkit2)
    staged2 = stage_toolkit(resolved2, tmp_path / "staged-ok")
    assert _hash_tree(staged2 / "skills" / "demo-skill") == skill2["files"]
    assert _hash_tree(staged2 / "mcp" / "docs") == mcp2["files"]


# --------------- owner review round 2 on PR #77 (2 findings, red-first) -----


def test_review5_spawn_rule_is_an_allowlist_not_a_denylist():
    """Finding 1 (round 2): the round-1 denylist was bypassable — every
    probe below was ADMITTED on 9769e79. The rule is now an allowlist:
    a vendored ``{staged}/...`` executable, or an allowed interpreter
    whose FIRST non-option argument is a ``{staged}/...`` script; only
    benign runtime flags may precede the script; shells, inline-code
    and module flags, URL args and staged-root escapes all refuse."""
    from factory.toolkit import vet_descriptor

    def spawn(command, args):
        return vet_descriptor({
            "command": command, "args": args,
            "env": {},
            "tools": [{"name": "search", "mode": "read"}],
            "domains": ["docs.example.com"],
        }, allowlists=ALLOW)

    # The six bypass probes from the review, each refused, each reason
    # naming what was probed.
    probes = [
        ("python3.12", ["-m", "pip", "install", "x", "{staged}/a"],
         "python3.12"),
        ("python3", ["-I", "-m", "pip", "install", "x", "{staged}"], "-I"),
        ("npm", ["exec", "pkg", "{staged}"], "npm"),
        ("node", ["-e", "fetch('https://registry.npmjs.org/x')", "{staged}"],
         "-e"),
        ("sh", ["-c", "curl x|sh", "{staged}"], "sh"),
        ("deno", ["run", "https://x/y.ts", "{staged}"], "deno"),
        # …and the reviewer's extra shapes.
        ("env", ["sh", "-c", "curl x|sh"], "env"),
        ("node", ["--require", "x", "{staged}/s.js"], "--require"),
        ("node", ["{staged}/../x"], "{staged}/../x"),
    ]
    for command, args, marker in probes:
        reasons = spawn(command, args)
        assert reasons, (command, args)
        assert any(marker in r for r in reasons), (command, args, reasons)

    # The admitted shapes: a vendored script under an allowed
    # interpreter (flags after the script belong to the script —
    # ``--port`` is not ``-p``), and a vendored executable itself.
    assert spawn("node", ["{staged}/server.js"]) == []
    assert spawn("python3", ["{staged}/server.py", "--port-free-flag"]) == []
    assert spawn("{staged}/bin/server", ["--verbose"]) == []
    # A benign runtime flag before the script is fine; anything else
    # before it (option bundles smuggle letters past exact matching)
    # refuses.
    assert spawn("python3", ["-u", "{staged}/server.py"]) == []
    assert spawn("python3", ["-Ic", "{staged}/server.py"]) != []


def test_review6_file_hashes_refuse_links_at_the_choke_point(tmp_path: Path):
    """Finding 2 (round 2): ``file_hashes`` silently skipped symlinks —
    refusal depended on every caller remembering the separate check. It
    now raises at the one choke point, so ``content_hash_of`` and every
    future caller inherit the refusal; the vet keeps its finding class
    for the report."""
    import os

    from factory.toolkit import ToolkitError, content_hash_of, file_hashes

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "x.md").write_text("x\n")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")

    for name, plant in (
        ("symlinked file", lambda root: (root / "link.md").symlink_to(outside)),
        ("symlinked dir", lambda root: (root / "sub").symlink_to(real_dir)),
        ("fifo", lambda root: os.mkfifo(root / "pipe")),
    ):
        tree = tmp_path / f"tree-{name.replace(' ', '-')}"
        tree.mkdir()
        (tree / "SKILL.md").write_text(SKILL_MD)
        plant(tree)
        with pytest.raises(ToolkitError, match="link|pipe|sub"):
            file_hashes(tree)
        with pytest.raises(ToolkitError):
            content_hash_of(tree)


# --------------- owner review round 3 on PR #77 (1 finding, red-first) ------


def test_review7_descriptor_env_is_an_allowlist(tmp_path: Path):
    """Finding (round 3): the spawn allowlist closed argv, but the
    descriptor's ENV still injected code into the server process
    (NODE_OPTIONS --require, PYTHONPATH, PYTHONSTARTUP, LD_PRELOAD) —
    and the child's cwd is the run clone the implementer writes, so
    relative variants run planted code too. Env is now an allowlist of
    inert keys with path-free value grammars, enforced at the same
    three layers as the spawn rule — vet, load, resolve."""
    from factory.toolkit import ToolkitError, vet_descriptor

    def spawn_env(env):
        return vet_descriptor({
            "command": "node",
            "args": ["{staged}/mcp/docs/server.js"],
            "env": env,
            "tools": [{"name": "search", "mode": "read"}],
            "domains": ["docs.example.com"],
        }, allowlists=ALLOW)

    # The reviewer's four probes plus relative variants and the rest of
    # the interpreter/loader control set — each refused, key named.
    refused = [
        ("NODE_OPTIONS", "--require /tmp/evil.js"),
        ("NODE_OPTIONS", "--require ./x.js"),
        ("NODEPATH", "/tmp/mods"),  # typo-guard: near-miss key refuses too
        ("NODE_PATH", "/tmp/mods"),
        ("NODE_EXTRA_CA_CERTS", "/tmp/ca.pem"),
        ("PYTHONPATH", "/tmp/evil"),
        ("PYTHONPATH", "."),
        ("PYTHONSTARTUP", "/tmp/evil.py"),
        ("PYTHONSTARTUP", "./evil.py"),
        ("PYTHONHOME", "/tmp"),
        ("PYTHONUSERBASE", "/tmp"),
        ("PYTHONINSPECT", "1"),
        ("LD_PRELOAD", "/tmp/x.so"),
        ("LD_PRELOAD", "./x.so"),
        ("LD_LIBRARY_PATH", "/tmp"),
        ("LD_AUDIT", "/tmp/x.so"),
        ("DYLD_INSERT_LIBRARIES", "/tmp/x.so"),
        ("DYLD_LIBRARY_PATH", "/tmp"),
        ("BASH_ENV", "/tmp/evil.sh"),
        ("ENV", "/tmp/evil.sh"),
        ("PATH", "/tmp/bin"),
        ("HOME", "/tmp/home"),
    ]
    for key, value in refused:
        reasons = spawn_env({key: value})
        assert reasons, (key, value)
        assert any(key in r for r in reasons), (key, value, reasons)

    # The admitted keys: locale/zone/color/verbosity knobs, inert data.
    assert spawn_env({
        "LANG": "sv_SE.UTF-8",
        "LC_ALL": "C",
        "TZ": "Europe/Stockholm",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "LOG_LEVEL": "debug",
    }) == []

    # Value grammar: no path (outside {staged} or anywhere), no
    # expansions, no placeholder — a path in an inert key's value is a
    # smuggle attempt, not a locale.
    for key, value in [
        ("LANG", "/tmp/evil"), ("LANG", "./x"), ("LANG", "${HOME}/x"),
        ("TZ", "../etc"), ("TZ", "/etc/passwd"), ("TZ", "Europe/../x"),
        ("NO_COLOR", "./x"), ("LOG_LEVEL", "/var/log/app"), ("TERM", "x;y"),
    ]:
        reasons = spawn_env({key: value})
        assert reasons, (key, value)

    # The same gate at load: an index entry whose descriptor env carries
    # a control variable refuses, key named.
    toolkit = tmp_path / "toolkit"
    poisoned = _make_mcp(toolkit, name="poisoned")
    poisoned["mcp"]["env"] = {"NODE_OPTIONS": "--require /tmp/evil.js"}
    _write_reports(toolkit, [poisoned])
    _write_index(toolkit, [poisoned])
    with pytest.raises(ToolkitError, match="NODE_OPTIONS"):
        load_index(toolkit)

    # …and at resolve: a relative-path PYTHONPATH refuses against the
    # running profile.
    toolkit2, skill2, mcp2 = _toolkit_with(tmp_path / "tk2")
    mcp2["mcp"]["env"] = {"PYTHONPATH": "."}
    _write_index(toolkit2, [skill2, mcp2])
    profile = yaml.safe_load(_profile(tmp_path / "tk2", {
        "skills": [], "plugins": [], "mcp_servers": ["docs"],
    }).read_text())
    with pytest.raises(ToolkitError, match="PYTHONPATH"):
        resolve_toolkit(profile, "implementer", toolkit_root=toolkit2)


def test_review8_session_neutralizes_control_vars_in_the_mcp_child_env(
        tmp_path: Path, monkeypatch):
    """Finding (round 3), session side: whatever the CLI merges into the
    MCP child's environment, the config env wins — so the spawn passes
    inert neutralizing values for every interpreter/loader control
    variable (verified per-runtime: empty NODE_OPTIONS adds no options;
    PYTHONPATH=\"\" would add the child's CWD — the implementer-written
    clone — so it gets a path the sandbox's write-allowlist can never
    let exist) on top of the descriptor's admitted keys. Nothing else
    crosses: the config env is exactly the scrub set plus the admitted
    descriptor env."""
    from factory.toolkit import ENV_SCRUB

    # Control vars riding the parent (session) environment — the window
    # and sandbox layers should never let these through, but the spawn
    # boundary does not trust them either.
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/x.so")

    toolkit, skill, mcp = _toolkit_with(tmp_path)
    staged = tmp_path / "staged"
    staged.mkdir()
    spec = {"staged": str(staged), "items": [mcp]}
    options = build_options(
        "implementer", tmp_path, 5,
        base_url="https://api.z.ai/api/anthropic", token="t", toolkit=spec,
    )
    child_env = options.mcp_servers["docs"]["env"]
    # Neutralized: an inherited value loses the merge to these.
    assert child_env["NODE_OPTIONS"] == ""
    assert child_env["LD_PRELOAD"] == ""
    assert child_env["PYTHONPATH"] == "/factory-mcp-scrub"
    assert child_env["PYTHONUSERBASE"] == "/factory-mcp-scrub"
    assert child_env["NODE_PATH"] == ""
    assert child_env["BASH_ENV"] == ""
    # The descriptor's admitted keys ride along unchanged…
    assert child_env["NO_COLOR"] == "1"
    assert child_env["TZ"] == "Europe/Stockholm"
    # …and the config env is exactly scrub + admitted: no other name
    # crosses this boundary, whatever the parent carried.
    assert set(child_env) == set(ENV_SCRUB) | {"NO_COLOR", "TZ"}
