"""Shared harness moved from tests/test_f22.py (F-51 AC-7, I-17).

No test module imports another test module: what other suites used to
import from tests/test_f22.py lives here now, byte-for-byte (cut by AST node
extents from the origin at 47705d1, before any F-51 implementation). The
origin re-imports these names; importers import them from here. This
module is not collected by pytest and imports no test module.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import yaml

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


SKILL_MD = (
    "---\n"
    "name: demo-skill\n"
    "description: Demonstration vetted skill for the F-22 contract tests.\n"
    "---\n"
    "\n"
    "Look things up on https://docs.python.org before guessing.\n"
)


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
    """Vetting reports that name the content hash each one vetted.

    F-47 (I-2): an MCP item's report also records the allowlists it was
    vetted against (render_report's ``- allowlists:`` line) — the index
    load checks the descriptor's domains against THAT record. Here the
    vet ran against a project that allowed exactly the descriptor's
    domains, so the per-project re-check at resolve time stays the one
    that refuses a domain the RUNNING project doesn't allow."""
    for entry in entries:
        report = toolkit / entry["report"]
        report.parent.mkdir(parents=True, exist_ok=True)
        text = (
            f"# Vet report — {entry['name']}\n\n"
            f"content hash: {entry['content_hash']}\n"
        )
        if entry.get("kind") == "mcp_server":
            text += f"- allowlists: {', '.join(entry['mcp']['domains'])}\n"
        report.write_text(text)


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
