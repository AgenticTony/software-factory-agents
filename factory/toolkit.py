"""The vetted agent toolkit: index, vet, admission invariants (F-22).

``setting_sources=[]`` (F-06) rightly keeps the owner's personal Claude
setup out of every sandbox — but it left agents with no skills at all.
This module is the missing middle: a factory-owned, version-pinned
toolkit, **vetted before admission** and chosen per project at
onboarding. Three rules hold the line:

* **Admission is human** (AC-4). An item enters ``toolkit/index.yaml``
  only through a PR carrying its vetting report, approved by the owner.
  Nothing in this module writes the index — ``factory toolkit vet``
  reads a candidate, prints findings, and that is all. The load-side
  checks below make a lazy admission impossible anyway: every entry
  needs a report that exists and names the content hash *it* vetted, so
  a version bump cannot ride an old report.
* **The vetter only reads** (AC-3). :func:`vet_candidate` opens files.
  No subprocess, no network, no execution of candidate code — ever, not
  even "just to check what it does". A candidate is data until a human
  admits it.
* **Fail closed at every load** (AC-1/2/5). The index is
  schema-validated, licence-gated, and content-addressed on every load:
  a tampered, deleted or extra vendored file refuses the whole index,
  exactly as F-02 refuses invalid queue state.

MCP servers (AC-5) carry a descriptor in the index (command/args/env,
tools classified read|write|send|deploy, network domains). They are
admitted read-only with no credentials in their environment and their
domains inside the *running project's* allowlists (re-checked at
:func:`resolve_toolkit` time, because the index is factory-global while
allowlists are per project). At session time the runner spawns them from
the staged descriptor — inside the F-05 sandbox, everything the runner
spawns is confined — with each tool enabled one by one through
``allowed_tools``.

Content addressing: an item's ``content_hash`` is sha256 over the sorted
``(relpath, per-file sha256)`` pairs of its vendored dir; attempts
record ``<name>@<content_hash>`` (AC-8) so F-19 can attribute failures
to toolkit versions.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import yaml

from factory.contract import SCHEMA_DIR, ContractError, validate_against_schema
from factory.sentinels import CREDENTIAL_PATTERNS

TOOLKIT_SCHEMA = SCHEMA_DIR / "toolkit.schema.json"
INDEX_FILE = "index.yaml"
# The staged wrapper plugin's name — skills ride one local plugin so the
# session can load them with ``setting_sources=[]`` (nothing personal,
# nothing from the clone; the CLI discovers skills only through it).
TOOLKIT_PLUGIN_NAME = "factory-toolkit"

# AC-2: the only SPDX ids an item may carry. The schema's licence enum
# mirrors this list exactly (test_ac_2 pins the pair).
ALLOWED_LICENCES = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC")

# AC-3: the vet's failure classes, in the contract's order.
VET_CLASSES = (
    "network",
    "shell",
    "encoded_blob",
    "credential_read",
    "env_harvest",
    "hidden_chars",
    "prompt_injection",
    "self_modify",
)

# Encoded-blob threshold: a run of base64/hex charset longer than this
# is a payload, not prose.
BLOB_THRESHOLD = 256

# Discovery-only sources (AC-4): the index pins the real upstream repo,
# never an aggregator that lists it.
AGGREGATOR_MARKERS = (
    "mcpmarket.com",
    "punkpeye/awesome-mcp-servers",
    "awesome-mcp",
    "github.com/modelcontextprotocol/servers",  # the GitHub MCP Registry
)

# Licence filenames a candidate may carry (checked case-insensitively).
LICENCE_FILES = (
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    "LICENCE", "LICENCE.md", "LICENCE.txt",
    "COPYING", "COPYING.md",
)


class ToolkitError(Exception):
    """The toolkit index is unusable, or a candidate/profile refuses the
    toolkit rules. Raised instead of admitting or ignoring."""


# ------------------------------------------------------ content addressing --


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(root: Path) -> dict[str, str]:
    """``{posix relpath: sha256}`` over every file under ``root``.

    The walk is sorted and path-based, so the result is independent of
    directory iteration order and inode numbers.
    """
    root = Path(root)
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _sha256(path.read_bytes())
    return files


def _aggregate(files: dict[str, str]) -> str:
    """The item content hash: sha256 over the sorted (relpath, hash) pairs."""
    blob = "\n".join(f"{name}\n{digest}" for name, digest in sorted(files.items()))
    return _sha256(blob.encode())


def content_hash_of(root: Path) -> str:
    """The content hash of a directory tree (see :func:`file_hashes`)."""
    return _aggregate(file_hashes(root))


def default_toolkit_dir() -> Path:
    """This factory checkout's ``toolkit/`` dir (the default index root)."""
    return Path(__file__).resolve().parent.parent / "toolkit"


# ---------------------------------------------------------- the licence gate --


def identify_licence(text: str) -> str | None:
    """The allowlisted SPDX id a licence text grants, or None.

    Marker-phrase detection over the grant language, not the filename:
    a renamed or extensionless licence still identifies, and "All
    rights reserved" with no grant identifies as nothing (which the
    callers refuse with the reason named, AC-2).
    """
    low = text.lower()
    if "apache license" in low and "version 2.0" in low:
        return "Apache-2.0"
    if "permission is hereby granted, free of charge" in low:
        return "MIT"
    if "permission to use, copy, modify, and/or distribute this software" in low:
        return "ISC"
    if "redistribution and use in source and binary forms" in low:
        return "BSD-3-Clause" if "neither the name of" in low else "BSD-2-Clause"
    return None


def _licence_file(root: Path) -> Path | None:
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.name.upper() in {
            name.upper() for name in LICENCE_FILES
        }:
            return entry
    return None


# ------------------------------------------------------------ the vet scan --

_URL_RE = re.compile(r"(?:https?|wss?|ftp)://[^\s'\"<>\\)\]]+")
_NET_CALLS = re.compile(
    r"\b(?:requests\.(?:get|post|put|patch|delete|head)\b|urllib\.request|"
    r"httpx\.|fetch\s*\(|socket\.connect|net\.http\.|curl\b|wget\b|nc\s+-)"
)
_SHELL_RE = re.compile(
    r"\b(?:os\.system|os\.popen|subprocess|Popen\b|\beval\s*\(|\bexec\s*\(|"
    r"child_process|execSync|spawnSync|bash\s+-c|sh\s+-c|/bin/(?:ba)?sh\b)"
)
_B64_RUN = re.compile(r"[A-Za-z0-9+/=]{%d,}" % BLOB_THRESHOLD)
_HEX_RUN = re.compile(r"[0-9a-fA-F]{%d,}" % BLOB_THRESHOLD)
_CRED_READ = re.compile(
    r"~/?\.ssh|\.ssh/id_|id_rsa|id_ed25519|~/?\.aws|\.aws/credentials|"
    r"(?<![\w.])\.env\b|security\s+find-(?:generic|internet)-password|"
    r"find-generic-password|keychain"
)
_ENV_HARVEST = re.compile(
    r"dict\s*\(\s*os\.environ|os\.environ\.(?:items|values|copy)\s*\(|"
    r"for\s+\w+(?:\s*,\s*\w+)?\s+in\s+os\.environ|"
    r"console\.log\s*\(\s*process\.env\s*\)|"
    r"JSON\.stringify\s*\(\s*process\.env|"
    r"Object\.(?:entries|keys|values)\s*\(\s*process\.env|"
    r"\bprintenv\b|/proc/self/environ|/proc/[0-9]+/environ"
)
_INJECTION_PHRASES = (
    "ignore previous instructions",
    "ignore earlier instructions",
    "ignore all previous",
    "ignore your instructions",
    "disregard previous instructions",
    "disregard your instructions",
    "do not tell the user",
    "don't tell the user",
    "dont tell the user",
    "without telling the user",
    "hide this from the user",
    "hide your actions",
    "without the user's knowledge",
    "exfiltrate",
    "secretly send",
)
_SELF_MODIFY = re.compile(
    r"self[-_]?update|auto[-_]?update|autoupdate|update[sd]?\s+itself|"
    r"modif(?:y|ies)\s+itself|open\s*\(\s*__file__|"
    r"__file__[^\n]*['\"][wa]['\"]|inspect\.getsource|"
    r"(?:curl|wget)[^\n]*\|\s*(?:ba)?sh\b"
)
# Hidden characters (AC-3): zero-width (U+200B–200D, U+2060, U+FEFF),
# bidi overrides (U+202A–202E, U+2066–U+2069), Unicode tag characters
# (U+E0000–U+E007F).
_HIDDEN_CHARS = frozenset(
    [*range(0x200B, 0x200F), 0x2060, 0xFEFF,
     *range(0x202A, 0x202F), *range(0x2066, 0x206A),
     *range(0xE0000, 0xE0080)]
)


@dataclass(frozen=True)
class Finding:
    """One vet finding: the class, the file (candidate-relative), the
    1-based line, and a detail that never echoes a credential or a
    blob — the class and the construct are the evidence."""

    cls: str
    file: str
    line: int | None
    detail: str


@dataclass(frozen=True)
class VetResult:
    """What :func:`vet_candidate` found in a candidate."""

    findings: list[Finding] = field(default_factory=list)
    licence: str | None = None
    licence_reason: str | None = None
    content_hash: str = ""

    @property
    def clean(self) -> bool:
        """No findings and an allowlisted licence: admissible evidence."""
        return not self.findings and self.licence in ALLOWED_LICENCES


def _host_allowed(host: str, allowlists: Sequence[str]) -> bool:
    return any(host == a or host.endswith("." + a) for a in allowlists)


def _url_hosts(line: str) -> list[str]:
    hosts = []
    for match in _URL_RE.finditer(line):
        rest = match.group(0).split("://", 1)[1]
        host = re.split(r"[/?:#]", rest, 1)[0]
        if host:
            hosts.append(host)
    return hosts


def _scan_line(text: str, allowlists: Sequence[str]) -> list[tuple[str, str]]:
    """The (class, detail) hits for one line of a candidate file."""
    hits: list[tuple[str, str]] = []
    hosts = _url_hosts(text)
    offlist = [h for h in hosts if not _host_allowed(h, allowlists)]
    if offlist:
        hits.append(("network", f"URL host {offlist[0]} is outside the allowlists"))
    elif (match := _NET_CALLS.search(text)) and not any(
        _host_allowed(h, allowlists) for h in hosts
    ) and not any(a in text for a in allowlists):
        hits.append(("network", f"network call ({match.group(0)}) names no "
                                "allowlisted host"))
    if match := _SHELL_RE.search(text):
        hits.append(("shell", f"shell/eval construct {match.group(0)!r}"))
    if match := _B64_RUN.search(text) or _HEX_RUN.search(text):
        hits.append(("encoded_blob", f"encoded run of {len(match.group(0))} chars "
                                     f"exceeds the {BLOB_THRESHOLD} threshold"))
    if match := _CRED_READ.search(text):
        hits.append(("credential_read", f"credential location {match.group(0)!r}"))
    if match := _ENV_HARVEST.search(text):
        hits.append(("env_harvest", f"environment harvesting {match.group(0)!r}"))
    if match := _SELF_MODIFY.search(text):
        hits.append(("self_modify", f"self-modification {match.group(0)!r}"))
    low = text.lower()
    for phrase in _INJECTION_PHRASES:
        if phrase in low:
            hits.append(("prompt_injection", f"injection phrase {phrase!r}"))
            break
    return hits


def _read_candidate_text(path: Path) -> str:
    """Decode a candidate file for scanning.

    Strict UTF-8 first; a file that fails it (UTF-16 with BOM, or any
    other encoding) gets the UTF-16 attempt and then latin-1 — the scan
    must see the real characters, because a zero-width marker re-encoded
    in UTF-16 would decode as garbage under naive errors="replace" and
    the hidden-character class would silently miss it. Latin-1 never
    fails, so a genuinely binary file still scans (as noise — a binary
    payload in a skill is a finding, not a crash).
    """
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("utf-16")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _scan_file(path: Path, relative: str, findings: list[Finding],
               allowlists: Sequence[str]) -> None:
    """Scan one file: line classes plus whole-text hidden characters."""
    try:
        text = _read_candidate_text(path)
    except OSError as exc:
        raise ToolkitError(
            f"candidate file {relative} cannot be read: {exc} — the vet "
            "refuses rather than scan around a file it cannot see"
        ) from exc
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for cls, detail in _scan_line(line, allowlists):
            findings.append(Finding(cls, relative, lineno, detail))
    # Hidden characters: report the first per line (one finding per line
    # is enough to refuse; the codepoint is named, never reproduced).
    for lineno, line in enumerate(lines, start=1):
        for char in line:
            if ord(char) in _HIDDEN_CHARS:
                findings.append(Finding(
                    "hidden_chars", relative, lineno,
                    f"hidden character U+{ord(char):04X}"))
                break


def vet_candidate(path: Path | str, *, allowlists: Sequence[str]) -> VetResult:
    """Scan every file of a candidate item; read-only, always.

    Args:
        path: The candidate's directory (a skill, plugin or MCP server
            checkout). Must exist and be a directory.
        allowlists: The project's allowed domains (docs + network
            allowlists) — an outbound URL to a host outside them is a
            finding, an allowlisted one is not.

    Returns:
        The :class:`VetResult` — findings per class, the identified
        licence (or the reason it refused), and the candidate's content
        hash for the admission PR's report.

    Raises:
        ToolkitError: The path is not a readable directory — a refusal,
        never a scan of nothing (which would read as clean).
    """
    root = Path(path)
    if not root.is_dir():
        raise ToolkitError(f"candidate {root} is not a directory")
    findings: list[Finding] = []
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path_ in files:
        _scan_file(path_, path_.relative_to(root).as_posix(), findings,
                   allowlists)
    licence_path = _licence_file(root)
    licence: str | None = None
    reason: str | None = None
    if licence_path is None:
        reason = "licence missing: no LICENSE/LICENCE/COPYING file in the candidate"
    else:
        text = licence_path.read_text(encoding="utf-8", errors="replace")
        licence = identify_licence(text)
        if licence is None:
            if "all rights reserved" in text.lower():
                reason = ('licence not allowlisted: proprietary '
                          '("All rights reserved", no grant text)')
            else:
                reason = "licence not identified as any allowlisted SPDX " \
                         f"({', '.join(ALLOWED_LICENCES)})"
            licence = None
    return VetResult(
        findings=findings,
        licence=licence,
        licence_reason=reason,
        content_hash=_aggregate(file_hashes(root)),
    )


def render_report(result: VetResult, candidate: Path | str,
                  allowlists: Sequence[str]) -> str:
    """The vetting report markdown an admission PR carries (AC-4).

    Includes the candidate's content hash — the index's ``report``
    check requires the report to name the hash it vetted, so this is
    the artifact that makes a version bump mechanically repeat the vet.
    """
    lines = [
        f"# Vet report — {Path(candidate).name}",
        "",
        f"- candidate: `{Path(candidate)}`",
        f"- content hash: `{result.content_hash}`",
        f"- licence: {result.licence or 'REFUSED'}"
        + (f" ({result.licence_reason})" if result.licence_reason else ""),
        f"- allowlists: {', '.join(allowlists) or '(none)'}",
        f"- findings: {len(result.findings)}",
        "",
    ]
    if result.findings:
        lines.append("| class | file | line | detail |")
        lines.append("|---|---|---|---|")
        for f in result.findings:
            lines.append(f"| {f.cls} | {f.file} | {f.line or ''} | {f.detail} |")
    else:
        lines.append("No findings. A clean scan is admission *evidence*, "
                     "never admission: the item enters the index only "
                     "through an owner-approved PR (F-22 AC-4).")
    return "\n".join(lines) + "\n"


# ------------------------------------------------- MCP descriptor admission --


_CRED_ENV_KEY = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL", re.I
)


def _credential_shaped(value: str) -> bool:
    """Whether an env value looks like a credential: any F-25 shape, or
    an opaque run long enough to be a key. Over-matching is the safe
    direction — a refused env name is a human's glance, a leaked token
    is a rotation."""
    if any(pattern.search(value) for pattern in CREDENTIAL_PATTERNS):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{32,}", value))


def _descriptor_reasons(mcp: dict, allowlists: Sequence[str]) -> list[str]:
    """Why an MCP descriptor refuses admission (AC-5); empty = admitted.

    Read-only tools, no credentials in the environment, every domain
    inside the allowlists. The pinned version + integrity hash are
    schema-checked on the index entry, not here.
    """
    reasons: list[str] = []
    for tool in mcp.get("tools") or []:
        if tool.get("mode") != "read":
            reasons.append(
                f"tool {tool.get('name')!r} has mode {tool.get('mode')!r}: "
                "MCP servers are admitted read-only (no tool that writes, "
                "sends or deploys)"
            )
    for key, value in (mcp.get("env") or {}).items():
        if _CRED_ENV_KEY.search(key):
            reasons.append(
                f"env {key!r} is a credential name: MCP servers run with "
                "no credentials in their environment"
            )
        if _credential_shaped(str(value)):
            reasons.append(
                f"env {key!r} has a credential-shaped value"
            )
    for domain in mcp.get("domains") or []:
        if not _host_allowed(domain, allowlists):
            reasons.append(
                f"domain {domain!r} is outside the project's allowlists"
            )
    if not reasons and not (mcp.get("tools") or []):
        reasons.append("descriptor declares no tools")
    return reasons


def vet_descriptor(mcp: dict, *, allowlists: Sequence[str]) -> list[str]:
    """The descriptor half of :func:`vet_candidate` for MCP servers.

    Kept public and separate: the vet report for an MCP candidate cites
    these reasons alongside the file scan, and the index load re-runs
    the same gate (defence in depth after admission).
    """
    return _descriptor_reasons(mcp, allowlists)


# ------------------------------------------------------------- index loading --

_KIND_LISTS = (("skills", "skill"), ("plugins", "plugin"),
               ("mcp_servers", "mcp_server"))


def _check_entry(root: Path, entry: dict) -> None:
    """The load-time invariants one index entry must hold (fail closed)."""
    name = entry["name"]
    if entry["licence"] not in ALLOWED_LICENCES:
        raise ToolkitError(
            f"toolkit item {name!r}: licence {entry['licence']!r} is not "
            f"allowlisted ({', '.join(ALLOWED_LICENCES)})"
        )
    upstream = entry["upstream"].lower()
    for marker in AGGREGATOR_MARKERS:
        if marker.lower() in upstream:
            raise ToolkitError(
                f"toolkit item {name!r}: upstream {entry['upstream']!r} is an "
                f"aggregator ({marker}); the index pins the real upstream "
                "repo — aggregators are discovery sources only (AC-4)"
            )
    item_dir = root / entry["dir"]
    if not item_dir.is_dir():
        raise ToolkitError(
            f"toolkit item {name!r}: vendored dir {entry['dir']} is missing "
            f"under {root}"
        )
    listed: dict[str, str] = entry["files"]
    actual = file_hashes(item_dir)
    for rel in sorted(set(listed) - set(actual)):
        raise ToolkitError(
            f"toolkit item {name!r}: vendored file {entry['dir']}/{rel} is "
            "missing (index hash no longer matches the tree)"
        )
    for rel in sorted(set(actual) - set(listed)):
        raise ToolkitError(
            f"toolkit item {name!r}: {entry['dir']}/{rel} exists but is not "
            "in the index — unlisted content never went through admission"
        )
    for rel in sorted(listed):
        if actual[rel] != listed[rel]:
            raise ToolkitError(
                f"toolkit item {name!r}: vendored file {entry['dir']}/{rel} "
                "no longer matches its index hash"
            )
    if _aggregate(actual) != entry["content_hash"]:
        raise ToolkitError(
            f"toolkit item {name!r}: content_hash does not match its files"
        )
    report = root / entry["report"]
    if not report.is_file():
        raise ToolkitError(
            f"toolkit item {name!r}: vetting report {entry['report']} is "
            "missing — an item is admitted only with its report (AC-4)"
        )
    if entry["content_hash"] not in report.read_text(encoding="utf-8",
                                                     errors="replace"):
        raise ToolkitError(
            f"toolkit item {name!r}: report {entry['report']} does not name "
            "the content hash it vetted — a version bump repeats the full "
            "vet (AC-4)"
        )
    licence_file = _licence_file(item_dir)
    if licence_file is None:
        raise ToolkitError(
            f"toolkit item {name!r}: vendored dir carries no LICENSE file "
            "(vendored files live next to their upstream licence, AC-1)"
        )
    identified = identify_licence(
        licence_file.read_text(encoding="utf-8", errors="replace"))
    if identified != entry["licence"]:
        # Strict on purpose: the vet that admitted this item identified
        # the same file with the same marker phrases, so a mismatch here
        # means the vendored licence was swapped or never vetted.
        raise ToolkitError(
            f"toolkit item {name!r}: index says {entry['licence']!r} but the "
            f"vendored LICENSE identifies as "
            f"{identified!r} — the licence gate refuses (AC-2)"
        )
    if entry["kind"] == "mcp_server":
        reasons = _descriptor_reasons(entry["mcp"], entry["mcp"]["domains"])
        if reasons:
            raise ToolkitError(
                f"toolkit item {name!r}: MCP descriptor refuses admission: "
                + "; ".join(reasons)
            )


def load_index(toolkit_root: Path | str, *,
               schema_path: Path | None = None) -> dict:
    """Load and fully validate ``toolkit/index.yaml`` (fail closed).

    Args:
        toolkit_root: The ``toolkit/`` dir holding ``index.yaml`` and
            the vendored item dirs.
        schema_path: Optional schema override (tests).

    Returns:
        The parsed index document (``{"items": [...]}``).

    Raises:
        ToolkitError: Unreadable, not a mapping, schema-invalid, or any
            entry violates the load-time invariants (licence gate,
            aggregator upstream, per-file hashes, report existence and
            hash, MCP descriptor rules).
    """
    root = Path(toolkit_root)
    index_path = root / INDEX_FILE
    try:
        data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ToolkitError(f"toolkit index {index_path} does not exist") from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ToolkitError(f"cannot read toolkit index {index_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolkitError(
            f"toolkit index {index_path} must be a mapping, "
            f"got {type(data).__name__}"
        )
    try:
        validate_against_schema(data, schema_path or TOOLKIT_SCHEMA,
                                str(index_path))
    except ContractError as exc:
        raise ToolkitError(str(exc)) from exc
    seen: set[str] = set()
    for entry in data["items"]:
        if entry["name"] in seen:
            raise ToolkitError(
                f"toolkit index: duplicate item name {entry['name']!r}"
            )
        seen.add(entry["name"])
        _check_entry(root, entry)
    return data


# ------------------------------------------------- per-project, per-seat --


def resolve_toolkit(profile: dict, seat: str, *,
                    toolkit_root: Path | str | None = None) -> dict | None:
    """Resolve a project's (seat-narrowed) toolkit against the index.

    Args:
        profile: The loaded ``project.yaml`` mapping.
        seat: The seat name (``implementer``/``reviewer``) — a seat
            section in the profile narrows the choice for that seat.
        toolkit_root: Index root; default this checkout's ``toolkit/``.

    Returns:
        None when the project declares no toolkit; otherwise
        ``{"root": str, "items": [...], "skills": [...],
        "plugins": [...], "mcp_servers": [...]}`` — the index entries,
        seat-filtered, in profile order.

    Raises:
        ToolkitError: A name is not in the index, is the wrong kind for
            the list it is in, or an MCP server needs a domain outside
            *this project's* allowlists (the index is factory-global;
            allowlists are per project, so the check re-runs here).
    """
    section = profile.get("toolkit")
    if section is None:
        return None
    root = Path(toolkit_root) if toolkit_root is not None else default_toolkit_dir()
    index = load_index(root)
    by_name = {entry["name"]: entry for entry in index["items"]}
    seat_section = (section.get("seats") or {}).get(seat) or {}
    items: list[dict] = []
    resolved: dict = {"root": str(root), "items": items,
                      "skills": [], "plugins": [], "mcp_servers": []}
    allowlists = list(profile.get("docs_allowlist") or []) + list(
        profile.get("network_allowlist") or [])
    for key, kind in _KIND_LISTS:
        chosen = seat_section.get(key) if key in seat_section else (
            section.get(key) or [])
        for name in chosen or []:
            entry = by_name.get(name)
            if entry is None:
                raise ToolkitError(
                    f"toolkit.{key} names {name!r}, which is not in the "
                    f"toolkit index {root / INDEX_FILE}"
                )
            if entry["kind"] != kind:
                raise ToolkitError(
                    f"toolkit.{key} names {name!r}, which is a "
                    f"{entry['kind']} in the index"
                )
            if kind == "mcp_server":
                reasons = _descriptor_reasons(entry["mcp"], allowlists)
                if reasons:
                    raise ToolkitError(
                        f"MCP server {name!r} refuses this project's "
                        f"allowlists: {'; '.join(reasons)}"
                    )
            items.append(entry)
            resolved[key].append(name)
    return resolved


def stage_toolkit(resolved: dict, dest: Path | str) -> Path:
    """Stage the resolved toolkit as a read-only copy outside the clone
    (the F-06 runner-staging pattern, AC-7).

    The staged root is one local plugin (``factory-toolkit``) holding
    every skill; plugin items keep their own manifests; MCP descriptors
    are written as JSON beside the vendored dirs. The session reads
    exactly this copy — nothing is loaded from the clone or from any
    personal config.

    Returns:
        The staged root (:class:`Path`).
    """
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / ".claude-plugin").mkdir()
    (dest / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": TOOLKIT_PLUGIN_NAME,
            "description": "Factory-vetted agent toolkit, staged read-only "
                           "(F-22). Admission is human: toolkit/index.yaml "
                           "only changes through an owner-approved PR.",
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    root = Path(resolved["root"])
    for entry in resolved["items"]:
        shutil.copytree(root / entry["dir"], dest / entry["dir"])
        if entry["kind"] == "mcp_server":
            (dest / "mcp" / f"{entry['name']}.json").write_text(
                json.dumps(entry["mcp"], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    return dest
