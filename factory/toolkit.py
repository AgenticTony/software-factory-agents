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
allowlists are per project). The spawn itself is an ALLOWLIST (see
:func:`_spawn_reasons`): a ``{staged}/...`` vendored executable, or an
allowed interpreter running a ``{staged}/...`` script as its first
operand — never a fetch-at-spawn command, a shell, a code/module flag
or a URL argument, so the index pin describes exactly what executes.
The descriptor's env is an allowlist too (see :func:`_env_reasons`):
inert keys only (locale/zone/color/verbosity), path-free values — env
is a code-injection channel like any other (``NODE_OPTIONS --require``,
``PYTHONPATH``), and the child's cwd is the implementer-written clone.
At session time the runner spawns them from the staged descriptor —
inside the F-05 sandbox, everything the runner spawns is confined —
with each tool granted one by one and enforced by the session's MCP
guard hook (a deny hook still blocks under ``bypassPermissions``), and
the child's env neutralized over the CLI's merge (see
:data:`ENV_SCRUB`).

Content addressing: an item's ``content_hash`` is sha256 over the sorted
``(relpath, per-file sha256)`` pairs of its vendored dir; attempts
record ``<name>@<content_hash>`` (AC-8) so F-19 can attribute failures
to toolkit versions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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

# AC-3: the vet's failure classes, in the contract's order — plus the
# review round's ninth: a vendored symlink (or any non-regular file) is
# an exfiltration channel, not a style problem (PR #77 finding 1).
VET_CLASSES = (
    "network",
    "shell",
    "encoded_blob",
    "credential_read",
    "env_harvest",
    "hidden_chars",
    "prompt_injection",
    "self_modify",
    "symlink",
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


def _scan_tree(root: Path) -> tuple[dict[str, Path], list[str]]:
    """The tree under ``root`` as (regular files by posix relpath,
    irregular names).

    The walker never follows anything: a symlink (file or directory), a
    fifo, a socket or a device is *irregular* by name, and recursion
    happens only through real directories (PR #77 finding 1: rglob +
    is_file + copytree all FOLLOW symlinks — a vendored link to
    ``~/.ssh/id_ed25519`` would hand the private key to the model
    provider, and git stores symlinks, so an admission PR can carry one
    past a skim). Every caller that hashes, vets, loads or stages
    toolkit content goes through here, so irregular content is refused
    or flagged everywhere at once — and with symlinks refused, no path
    can escape the item dir.
    """
    files: dict[str, Path] = {}
    irregular: list[str] = []

    def walk(directory: Path, prefix: str) -> None:
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            rel = prefix + entry.name
            if entry.is_symlink():
                irregular.append(rel)
                continue
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                walk(Path(entry.path), rel + "/")
            elif stat.S_ISREG(mode):
                files[rel] = Path(entry.path)
            else:
                irregular.append(rel)

    walk(Path(root), "")
    return files, irregular


def irregular_paths(root: Path) -> list[str]:
    """Symlinks and other non-regular files under ``root``, by name."""
    return _scan_tree(root)[1]


def file_hashes(root: Path) -> dict[str, str]:
    """``{posix relpath: sha256}`` over every file under ``root``.

    The walk is sorted and path-based, so the result is independent of
    directory iteration order and inode numbers.

    Raises:
        ToolkitError: Any symlink or other non-regular file (fifo,
            socket, device) sits under ``root`` — named in the message.
            This is the one choke point (review round 2, finding 2: the
            round-1 walker *skipped* irregular entries, so refusal
            depended on every caller remembering a separate check — one
            that forgot would hash a tree with a link in it and never
            notice). :func:`content_hash_of` and every future caller
            inherit the refusal; :func:`vet_candidate` is the one
            deliberate exception (a candidate's link is a *finding* for
            the report, not a crash — it walks via :func:`_scan_tree`
            itself).
    """
    files, irregular = _scan_tree(root)
    if irregular:
        raise ToolkitError(
            f"{root}: {irregular[0]} is a symlink or non-regular file — "
            "toolkit content is plain files (a link's target would be "
            "read through it)"
        )
    return {rel: _sha256(path.read_bytes()) for rel, path in files.items()}


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
    files, irregular = _scan_tree(root)
    # Review finding 1: a symlink (or fifo/device) is a finding of its
    # own class — never hashed through, never staged.
    for rel in irregular:
        findings.append(Finding(
            "symlink", rel, None,
            "symlink or non-regular file: a vendored link is an "
            "exfiltration channel (its target is read through it)"))
    for rel in sorted(files):
        _scan_file(files[rel], rel, findings, allowlists)
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
        # The candidate's own walked regular files (file_hashes would
        # raise on the very links this scan reports as findings).
        content_hash=_aggregate(
            {rel: _sha256(path.read_bytes()) for rel, path in files.items()}
        ),
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

# The MCP spawn ALLOWLIST (review round 2 on PR #77: the round-1
# denylist was bypassable — argv0 outside the set, ``args[:2]`` exact
# spellings, and a ``{staged}`` token anywhere were all enough). An
# admitted spawn is one of two shapes and nothing else:
#
# * shape (a): the command itself is a ``{staged}/...`` path — a
#   vendored executable inside the staged copy;
# * shape (b): the command is an interpreter below and its FIRST
#   non-option argument is a ``{staged}/...`` script.
#
# The interpreters are exactly the runtimes a vendored server is
# realistically written in (JS on node; Python on python3, bare-python
# aliases and the versioned spellings) — each addition (bun, deno, …)
# is a reviewed change to this set, not a default.
_INTERPRETERS = frozenset(
    {"node", "python", "python3", *(f"python3.{n}" for n in range(9, 14))}
)
# Flags that may precede the staged script under shape (b): plain
# runtime behaviour, no code, no module loading, nothing that consumes
# a following argument. Exact tokens only — anything else before the
# script refuses, so option bundles (``-Ic``) cannot smuggle a code
# letter past exact matching.
_BENIGN_PRE_SCRIPT_FLAGS = frozenset({"-u", "-B", "-E", "-O", "-OO",
                                      "-q", "-qq"})
# Code and module flags: refused anywhere in the arguments (after the
# script they are the script's own argv, but refusing there too is the
# safe side of exact-token matching — ``--port`` is not ``-p``).
_CODE_FLAGS = frozenset({
    "-c", "-e", "--eval", "-p", "--print", "-m", "-r", "--require",
    "--import", "--loader", "--experimental-loader", "-I", "-S",
})
# Shells and wrappers: argv0 or wrapper — a shell re-parses everything
# after it, so no pin can describe what runs.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ash", "fish", "ksh",
                     "env"})
_URL_PREFIXES = ("http://", "https://", "ftp://", "ws://", "wss://")

# --- descriptor env allowlist (review round 3 on PR #77) ------------------
# The spawn allowlist closed argv, but ENV is a code-injection channel
# of its own: NODE_OPTIONS carries --require, PYTHONPATH/PYTHONSTARTUP
# load from wherever they point, LD_*/DYLD_* preload — and the child's
# cwd is the run clone the implementer writes, so relative paths run
# planted code. Env is an allowlist of INERT keys with path-free value
# grammars, enforced at the same three layers as the spawn rule.
#
# Admitted keys, each justified:
# * LANG / LC_* — locale identifiers (``sv_SE.UTF-8``); grammar has no
#   separator, so no path can be spelled.
# * TZ — an IANA zone name (``Europe/Stockholm``); slash-separated
#   components of [A-Za-z0-9_+-] — dots excluded, so no ``..`` and no
#   path outside the zone database's fixed root.
# * NO_COLOR — the no-CLI-color convention switch ("" or any token).
# * TERM — terminal identifier (``dumb``); the child's own TTY knobs.
# * LOG_LEVEL / LOGLEVEL — a verbosity knob servers already use.
# Every grammar excludes $ ~ { } \ and (but for TZ's dot-free
# separators) / — so no path (outside {staged} or anywhere), no shell
# expansion, no placeholder can ride a value.
_ENV_LOCALE_RE = re.compile(r"^[A-Za-z0-9_@+-][A-Za-z0-9_@.+-]*$")
_ENV_ZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$")
_ENV_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_ENV_ADMITTED_KEYS = {
    "LANG": _ENV_LOCALE_RE,
    "TZ": _ENV_ZONE_RE,
    "NO_COLOR": _ENV_TOKEN_RE,
    "TERM": _ENV_TOKEN_RE,
    "LOG_LEVEL": _ENV_TOKEN_RE,
    "LOGLEVEL": _ENV_TOKEN_RE,
}
# Interpreter and loader control variables — every one is code or
# module loading by another door; refused with the key named, and
# neutralized at the session spawn (see ENV_SCRUB).
_ENV_CONTROL_EXACT = frozenset({
    "NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS",
    "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE",
    "PYTHONINSPECT", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "BASH_ENV", "ENV", "PATH", "HOME",
})
_ENV_CONTROL_PREFIXES = ("PYTHON", "LD_", "DYLD_", "NODE_")

# The session spawn's neutralizing values (review round 3): the CLI
# merges the config env over its inherited environment for the MCP
# child (config wins on conflicts — the SDK passes this dict verbatim
# via --mcp-config), so each control variable gets an inert value that
# beats anything inherited. Verified per-runtime on this stack: empty
# NODE_OPTIONS adds no options (node execArgv unchanged); empty
# PYTHONHOME/PYTHONSTARTUP/LD_PRELOAD/DYLD_*/BASH_ENV mean "none";
# PYTHONPATH and PYTHONUSERBASE get a path the sandbox's write
# allowlist (the run dir only) can never let exist — an EMPTY
# PYTHONPATH would add the child's CWD, the implementer-written clone,
# to sys.path, which is the exact injection this round closes.
_NEVER_PATH = "/factory-mcp-scrub"
ENV_SCRUB: dict[str, str] = {
    "NODE_OPTIONS": "",
    "NODE_PATH": "",
    "NODE_EXTRA_CA_CERTS": "",
    "PYTHONPATH": _NEVER_PATH,
    "PYTHONSTARTUP": "",
    "PYTHONHOME": "",
    "PYTHONUSERBASE": _NEVER_PATH,
    "PYTHONINSPECT": "",
    "LD_PRELOAD": "",
    "LD_LIBRARY_PATH": "",
    "LD_AUDIT": "",
    "DYLD_INSERT_LIBRARIES": "",
    "DYLD_LIBRARY_PATH": "",
    "BASH_ENV": "",
    "ENV": "",
}

# The placeholder an admitted descriptor references: the vendored,
# hash-verified copy inside the staged toolkit.
STAGED_PLACEHOLDER = "{staged}"


def _credential_shaped(value: str) -> bool:
    """Whether an env value looks like a credential: any F-25 shape, or
    an opaque run long enough to be a key. Over-matching is the safe
    direction — a refused env name is a human's glance, a leaked token
    is a rotation."""
    if any(pattern.search(value) for pattern in CREDENTIAL_PATTERNS):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{32,}", value))


def _staged_relpath(token: str) -> str | None:
    """The in-root path a token names, or None when it is not a plain
    ``{staged}/<inside>`` path (no placeholder, a bare placeholder, a
    ``..`` escape, or the placeholder buried mid-token)."""
    marker = STAGED_PLACEHOLDER + "/"
    if not token.startswith(marker):
        return None
    rest = token[len(marker):]
    if not rest or ".." in PurePosixPath(rest).parts:
        return None
    return rest


def _spawn_reasons(mcp: dict) -> list[str]:
    """Why a descriptor's spawn refuses admission (PR #77 finding 3,
    review round 2: an ALLOWLIST, not a denylist — every bypass probe
    of the round-1 denylist was admitted).

    An admitted spawn runs vendored, hash-verified content, one of two
    shapes and nothing else:

    * **(a)** the command itself is a ``{staged}/...`` path — a vendored
      executable inside the staged copy; or
    * **(b)** the command is an allowed interpreter (see
      :data:`_INTERPRETERS`) whose FIRST non-option argument is a
      ``{staged}/...`` script, with only benign runtime flags (see
      :data:`_BENIGN_PRE_SCRIPT_FLAGS`) before it.

    Everything else refuses: shells and wrappers (a shell re-parses
    everything after it, so no pin can describe what runs); inline-code
    and module flags anywhere in the arguments (``-c``, ``-e``,
    ``--eval``, ``-p``, ``--print``, ``-m``, ``-r``/``--require``,
    ``--import``, ``--loader``, ``--experimental-loader``, and python's
    ``-I``/``-S``); any URL argument (a URL is a fetch by
    construction); and any ``{staged}`` token that normalises out of
    the staged root (``{staged}/../x``). Flags match as exact tokens
    (``--port`` is not ``-p``), and nothing but the benign runtime
    flags may precede the script, so option bundles like ``-Ic`` cannot
    smuggle a code letter past exact matching.
    """
    command = str(mcp.get("command") or "")
    args = [str(arg) for arg in mcp.get("args") or []]
    reasons: list[str] = []
    for arg in args:
        if arg.startswith(_URL_PREFIXES):
            reasons.append(
                f"argument {arg!r} is a URL — a URL argument is a fetch by "
                "construction; the server runs vendored content, it does "
                "not download it"
            )
            break
    for token in [command, *args]:
        if STAGED_PLACEHOLDER in token and _staged_relpath(token) is None:
            reasons.append(
                f"argument {token!r} does not name a path inside the staged "
                f"root ({STAGED_PLACEHOLDER}/… with no '..' segments)"
            )
    for arg in args:
        if arg in _CODE_FLAGS or any(
            arg.startswith(flag + "=")
            for flag in _CODE_FLAGS if flag.startswith("--")
        ):
            reasons.append(
                f"flag {arg!r} runs inline code or loads a module — the "
                "spawn carries no code and no module flags, only the "
                "vendored script and its own arguments"
            )
    argv0 = Path(command).name
    if argv0 in _SHELLS:
        reasons.append(
            f"command {command!r} is a shell or wrapper: it re-parses "
            "everything after it, so no index pin can describe what runs"
        )
    elif _staged_relpath(command) is not None:
        pass  # shape (a): the command IS a vendored executable
    elif argv0 in _INTERPRETERS:
        # Shape (b): the first non-option argument must be the staged
        # script; only benign runtime flags may precede it.
        pre_flags: list[str] = []
        first_operand = None
        for arg in args:
            if arg.startswith("-"):
                pre_flags.append(arg)
                continue
            first_operand = arg
            break
        smuggled = [f for f in pre_flags
                    if f not in _BENIGN_PRE_SCRIPT_FLAGS]
        if smuggled:
            reasons.append(
                f"flag {smuggled[0]!r} precedes the staged script — only "
                "benign runtime flags ("
                + " ".join(sorted(_BENIGN_PRE_SCRIPT_FLAGS)) +
                ") may, so option bundles cannot smuggle a code flag"
            )
        if first_operand is None or _staged_relpath(first_operand) is None:
            reasons.append(
                f"interpreter {command!r} must run a staged script as its "
                f"first non-option argument ({STAGED_PLACEHOLDER}/…) — an "
                "interpreter without a vendored script executes whatever "
                "it is handed, and no pin vouches for that"
            )
    else:
        reasons.append(
            f"command {command!r} is neither a staged vendored executable "
            f"({STAGED_PLACEHOLDER}/…) nor an allowed interpreter ("
            + ", ".join(sorted(_INTERPRETERS)) +
            ") running one — an MCP server runs the vendored, "
            "hash-verified copy, and anything else is unpinned by "
            "construction"
        )
    return reasons


def _env_reasons(env: dict | None) -> list[str]:
    """Why a descriptor's env refuses admission (review round 3); empty
    = admitted.

    An allowlist of inert keys with path-free value grammars (see
    :data:`_ENV_ADMITTED_KEYS`): locale/zone/color/verbosity knobs —
    data, never code, never a path. Every interpreter or loader control
    variable refuses with the key named (see :data:`_ENV_CONTROL_EXACT`
    and the prefixes), and so does every key outside the allowlist:
    fail closed, extend by review.
    """
    reasons: list[str] = []
    for key, value in (env or {}).items():
        grammar = _ENV_ADMITTED_KEYS.get(key)
        if key.startswith("LC_"):
            grammar = _ENV_LOCALE_RE
        if grammar is None:
            why = (
                "an interpreter or loader control variable — code or "
                "module loading by another door"
                if key in _ENV_CONTROL_EXACT
                or key.startswith(_ENV_CONTROL_PREFIXES)
                else "not one of the inert keys (LANG, LC_*, TZ, "
                     "NO_COLOR, TERM, LOG_LEVEL, LOGLEVEL)"
            )
            reasons.append(
                f"env {key!r} refuses: {why}; the MCP child's "
                "environment carries inert data only"
            )
        elif not grammar.match(str(value)):
            reasons.append(
                f"env {key!r} value {str(value)!r} fails its grammar — "
                "values are identifiers and knobs, never paths (outside "
                f"{STAGED_PLACEHOLDER} or anywhere), expansions or "
                "placeholders"
            )
    return reasons


def _descriptor_reasons(mcp: dict, allowlists: Sequence[str]) -> list[str]:
    """Why an MCP descriptor refuses admission (AC-5); empty = admitted.

    Read-only tools, no credentials in the environment, every domain
    inside the allowlists, a spawn that runs the vendored content (no
    runtime fetch), and an inert env allowlist (no control variables,
    no paths in values). The pinned version + integrity hash are
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
    reasons.extend(_spawn_reasons(mcp))
    reasons.extend(_env_reasons(mcp.get("env")))
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
    irregular = irregular_paths(item_dir)
    if irregular:
        raise ToolkitError(
            f"toolkit item {name!r}: {entry['dir']}/{irregular[0]} is a "
            "symlink or non-regular file — vendored content must be plain "
            "files (a link's target would be read through it; PR #77 "
            "finding 1)"
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

    Verified twice around the copy (PR #77 findings 1 and 4): the
    SOURCE is walked before anything is written (no symlink, no
    non-regular file, and the per-file hashes still equal the index
    entry's — a mutation between resolve and stage refuses), and the
    COPY is re-hashed afterwards and compared against the same entry, so
    what the session reads is exactly what the index pins. A refusal
    leaves no staged tree behind.

    Returns:
        The staged root (:class:`Path`).

    Raises:
        ToolkitError: The source carries irregular content or no longer
            matches its index hashes, or the copy's hashes disagree.
    """
    dest = Path(dest)
    root = Path(resolved["root"])
    # Pre-copy: the source tree must be exactly what the index pinned —
    # plain files, matching hashes, nothing mutated since resolve.
    for entry in resolved["items"]:
        source = root / entry["dir"]
        irregular = irregular_paths(source)
        if irregular:
            raise ToolkitError(
                f"toolkit item {entry['name']!r}: {entry['dir']}/"
                f"{irregular[0]} is a symlink or non-regular file — "
                "refusing to stage (a link's target would be copied "
                "through and read by the session)"
            )
        actual = file_hashes(source)
        if actual != entry["files"]:
            differing = sorted(
                set(actual) ^ set(entry["files"])
                | {rel for rel in set(actual) & set(entry["files"])
                   if actual[rel] != entry["files"][rel]}
            )
            raise ToolkitError(
                f"toolkit item {entry['name']!r}: source no longer matches "
                f"its index hashes ({', '.join(differing[:3])}) — refusing "
                "to stage mutated content"
            )
    if dest.exists():
        shutil.rmtree(dest)
    try:
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
        for entry in resolved["items"]:
            shutil.copytree(root / entry["dir"], dest / entry["dir"])
            if entry["kind"] == "mcp_server":
                (dest / "mcp" / f"{entry['name']}.json").write_text(
                    json.dumps(entry["mcp"], indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        # Post-copy: re-hash the copy itself and compare against the
        # index entry — the session reads this tree, so this tree must
        # be exactly what the pins describe (finding 4: resolve's
        # verification of the source is not verification of the copy).
        for entry in resolved["items"]:
            copied = file_hashes(dest / entry["dir"])
            if copied != entry["files"]:
                raise ToolkitError(
                    f"toolkit item {entry['name']!r}: the staged copy does "
                    "not match its index hashes — refusing to hand the "
                    "session an unpinned copy"
                )
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest
