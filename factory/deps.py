"""Dependency-manifest parsing and the L0 added-dependency policy (F-71).

L0 used to escalate ``new_dependency`` on EVERY
``dependency_manifests`` touch — a contract whose own criterion was
"pin pypdf" could never pass. F-71 AC-4 replaces the blanket rule with
deterministic parsing: base vs head blob at the refs L0 already has,
direct manifests (``requirements*.txt``, ``pyproject.toml``'s
``[project]`` dependency tables, ``package.json``'s four dependency
maps) and lock manifests (``requirements*.lock``,
``package-lock.json`` v2/v3 ``packages``). "Added" means a name absent
at base, or present with a different pinned version. Every added
DIRECT package must be declared in the contract's ``dependencies`` and
every added package must pass the registry checks
(:mod:`factory.registry`); everything the parser cannot prove
escalates exactly as before — fail closed.

The F-31 pyproject surface comparison moves here unchanged (one home:
the inert allowlist, the strip, the comparison), re-exported by
:mod:`factory.l0` under its old private name so no caller moves.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import posixpath
import re
import tomllib
from pathlib import Path

from factory.gates import GateError
from factory.gates import git as _git
from factory.registry import RegistryClient, normalise_name

# One manifest blob larger than this refuses parsing (an escalation, not
# a hang): a real manifest is kilobytes.
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
# ``-r``/``-c`` include chains are bounded: a cycle or a bomb of nested
# requirements files refuses instead of recursing.
MAX_INCLUDE_DEPTH = 10
# Registry calls per L0 run (R1-2): each call is sequential and bounded
# by the client's timeout, so a whole-lock regen's worth of additions
# would hold L0 for minutes at best. Fail closed past the cap.
MAX_REGISTRY_CALLS = 30


class ManifestRefused(Exception):
    """This manifest cannot be parsed deterministically — escalate."""


# --------------------------------------------------- the F-31 pyproject rule --

_INERT_PYPROJECT_PATHS = (
    "project.name",
    "project.description",
    "project.readme",
    "project.version",
    "project.urls",
    "project.scripts",
    "tool.ruff",
    "tool.pytest",
    "tool.coverage",
    "tool.mypy",
    "tool.setuptools.package-data",
)


def _prune_empty(node):
    """Recursively drop dict entries whose value is (or prunes to) an
    empty dict; scalars, lists and empty lists pass through untouched."""
    if not isinstance(node, dict):
        return node
    pruned = {}
    for key, value in node.items():
        value = _prune_empty(value)
        if isinstance(value, dict) and not value:
            continue
        pruned[key] = value
    return pruned


def _strip_inert(doc: dict) -> dict:
    """A deep copy of a parsed pyproject with every inert path removed,
    for whole-document comparison."""
    stripped = copy.deepcopy(doc)
    for dotted in _INERT_PYPROJECT_PATHS:
        parts = dotted.split(".")
        node = stripped
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    return _prune_empty(stripped)


def _drop_project_dependencies(doc: dict) -> dict:
    """The pyproject surface OUTSIDE the two dependency tables the parser
    extracts: the inert-stripped document minus ``project.dependencies``
    and ``project.optional-dependencies``. A change here (poetry tables,
    dependency-groups, build-system.requires, dynamic, uv sources) is a
    dependency surface the parser cannot read — it escalates rather
    than slipping through."""
    stripped = _strip_inert(doc)
    project = stripped.get("project")
    if isinstance(project, dict):
        project.pop("dependencies", None)
        project.pop("optional-dependencies", None)
    return _prune_empty(stripped)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def pyproject_surface_unchanged(change, path: str, *, repo, base_ref,
                                head_ref) -> bool:
    """Whether a manifest touch leaves pyproject.toml's dependency
    surface untouched (the F-31 rule, verbatim).

    True ONLY for a plain modification (status M) of a root
    ``pyproject.toml`` with refs supplied, whose merge-base and head
    sides both parse and compare equal after inert stripping. Every
    doubt — another manifest path, an added/deleted/renamed pyproject,
    missing refs, a git or TOML failure — answers False, and the caller
    escalates exactly as the path-match rule always did.
    """
    if path != "pyproject.toml" or change.status != "M":
        return False
    if repo is None or not base_ref or not head_ref:
        return False
    try:
        merge_base = _git(Path(repo), "merge-base", base_ref, head_ref).strip()
        before = tomllib.loads(_git(Path(repo), "show",
                                    f"{merge_base}:pyproject.toml"))
        after = tomllib.loads(_git(Path(repo), "show",
                                   f"{head_ref}:pyproject.toml"))
        return _strip_inert(before) == _strip_inert(after)
    except (GateError, tomllib.TOMLDecodeError, TypeError, UnicodeDecodeError):
        return False


# ------------------------------------------------------------- the grammars --

# A PEP 508 distribution name: the grammar pip itself accepts.
_REQ_NAME = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_VCS_PREFIXES = ("git+", "svn+", "hg+", "bzr+")
_PINNED = re.compile(r"==\s*[^,;\s]+")


def parse_requirement(line: str) -> tuple[str, str | None]:
    """One requirements line → ``(raw name, pinned version | None)``.

    The grammar accepted is deliberately narrow; everything outside it
    raises :class:`ManifestRefused` (and so escalates): URLs and VCS
    refs, editable installs, index/find-links/trusted-host options,
    environment markers (they cannot be evaluated deterministically
    parent-side), and anything unparsable as ``name[extras] specifier``.
    """
    # A comment starts at '#' when it begins the line or follows
    # whitespace (pip's own rule).
    cut = re.search(r"(^|\s)#", line)
    if cut:
        line = line[:cut.start()]
    line = line.strip()
    if not line:
        raise ManifestRefused("empty line")  # callers skip these first
    if line.startswith("-"):
        raise ManifestRefused(
            f"option line {line.split()[0]!r} is refused — only -r/-c "
            "includes are parsed"
        )
    if "://" in line or line.startswith(_VCS_PREFIXES):
        raise ManifestRefused(f"direct URL/VCS reference refused: {line!r}")
    if ";" in line:
        raise ManifestRefused(
            f"environment marker refused (cannot be evaluated "
            f"deterministically): {line!r}"
        )
    match = _REQ_NAME.match(line)
    if match is None:
        raise ManifestRefused(f"not a parsable requirement: {line!r}")
    name = match.group(1)
    rest = line[match.end():].strip()
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            raise ManifestRefused(f"unterminated extras: {line!r}")
        rest = rest[end + 1:].strip()
    if rest.startswith("["):
        raise ManifestRefused(f"doubled extras: {line!r}")
    # R1-1: a PEP 508 direct reference (``name @ <anything>``) installs
    # from a place the registry checks never see — refuse the whole
    # shape. A registry specifier never carries these characters; a
    # local path, URL or alias always does.
    if rest.startswith("@") or any(c in rest for c in ":/@\\"):
        raise ManifestRefused(
            f"direct reference refused (name @ source / a path or URL "
            f"specifier): {line!r} — only registry specifiers are parsed"
        )
    version = None
    pin = _PINNED.search(rest)
    if pin:
        version = pin.group(0)[2:].strip()
    return name, version


def parse_requirements(text: str, include_reader=None, *,
                       depth: int = 0,
                       seen: tuple = ()) -> dict[str, tuple[str, str | None]]:
    """A requirements file's pinned set: normalised name → (raw, version).

    The text must already be LOGICAL lines (backslash continuations
    joined — :func:`_parse_requirements_text` does that). Blank lines
    and comments skip; ``-r``/``-c`` includes recurse through
    ``include_reader(relpath) -> text`` (missing files refuse), bounded
    by :data:`MAX_INCLUDE_DEPTH` and a cycle set.
    """
    entries: dict[str, tuple[str, str | None]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("-r ", "--requirement ", "-c ", "--constraint ")):
            if depth >= MAX_INCLUDE_DEPTH:
                raise ManifestRefused(
                    f"-r/-c include chain deeper than {MAX_INCLUDE_DEPTH}"
                )
            rel = stripped.split(None, 1)[1].strip()
            key = ("include", rel, depth)
            if key in seen:
                raise ManifestRefused(f"-r/-c include cycle on {rel!r}")
            if include_reader is None:
                raise ManifestRefused(
                    f"-r/-c include {rel!r} with no reader for its blob"
                )
            included = parse_requirements(
                include_reader(rel), include_reader,
                depth=depth + 1, seen=(*seen, key),
            )
            for norm, value in included.items():
                entries.setdefault(norm, value)
            continue
        name, version = parse_requirement(line)
        entries.setdefault(normalise_name("pypi", name), (name, version))
    return entries


def _parse_requirements_text(text: str, include_reader=None) \
        -> dict[str, tuple[str, str | None]]:
    """parse_requirements over LOGICAL lines (backslash continuations
    joined first, like pip reads them)."""
    logical: list[str] = []
    pending = ""
    for raw_line in text.splitlines():
        if raw_line.rstrip().endswith("\\"):
            pending += raw_line.rstrip()[:-1] + " "
            continue
        logical.append(pending + raw_line)
        pending = ""
    if pending:
        logical.append(pending)
    return parse_requirements("\n".join(logical), include_reader)


def _pyproject_dependencies(doc: dict) -> dict[str, tuple[str, str | None]]:
    """``[project] dependencies`` + ``optional-dependencies`` as the
    pinned set; each entry parses as one requirement line."""
    entries: dict[str, tuple[str, str | None]] = {}
    project = doc.get("project")
    if not isinstance(project, dict):
        return entries
    tables = [project.get("dependencies")]
    optional = project.get("optional-dependencies")
    if optional is not None:
        if not isinstance(optional, dict):
            raise ManifestRefused(
                "pyproject [project].optional-dependencies is not a table"
            )
        tables.extend(optional.values())
    for table in tables:
        if table is None:
            continue
        if not isinstance(table, list):
            raise ManifestRefused(
                "pyproject [project].dependencies is not an array"
            )
        for entry in table:
            if not isinstance(entry, str):
                raise ManifestRefused(
                    "pyproject dependency entry is not a string"
                )
            name, version = parse_requirement(entry)
            entries.setdefault(normalise_name("pypi", name), (name, version))
    return entries


def parse_pyproject(text: str) \
        -> tuple[dict[str, tuple[str, str | None]], str]:
    """The pyproject's direct dependencies and its surface OUTSIDE them.

    Returns ``(deps, other_surface)``: ``deps`` from
    ``[project].dependencies``/``optional-dependencies``;
    ``other_surface`` is the canonical JSON of the inert-stripped
    document with those two tables removed — a change there
    (poetry/uv/dependency-groups/build-system/dynamic) is a dependency
    surface this parser cannot read.
    """
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestRefused(f"pyproject.toml does not parse: {exc}") from exc
    return _pyproject_dependencies(doc), _canonical(_drop_project_dependencies(doc))


_PACKAGE_JSON_MAPS = ("dependencies", "devDependencies", "peerDependencies",
                      "optionalDependencies")


def _npm_registry_specifier(value, where: str) -> str:
    """One package.json dependency VALUE, proven a registry specifier.

    Everything but a semver range / dist-tag is refused (R1-1): an npm
    alias (``npm:evil-pkg@1.0.0``), a git/github source, ``file:``,
    ``link:``, ``workspace:``, ``portal:``, an http(s) tarball or a
    local path installs code the registry checks never see. A registry
    range or dist-tag never carries ``:`` ``/`` ``@`` or a backslash —
    every refused shape does.
    """
    if not isinstance(value, str) or not value.strip():
        raise ManifestRefused(
            f"{where}: dependency value {value!r} is not a registry "
            "version range"
        )
    if any(c in value for c in ":/@\\"):
        raise ManifestRefused(
            f"{where}: dependency value {value!r} is not a registry "
            "version range — npm: aliases, git/github/file/link/"
            "workspace/portal sources, URLs and local paths are refused "
            "(they install code the registry checks never see)"
        )
    return value


# package.json metadata with no install consequence (R2-3): everything
# else outside the four dependency maps escalates on change —
# overrides/resolutions/bundledDependencies/workspaces are NOT here on
# purpose (they redirect what gets installed).
_INERT_PACKAGE_JSON_KEYS = frozenset({
    "name", "version", "description", "main", "type", "module", "types",
    "typings", "exports", "files", "browser", "os", "cpu", "engines",
    "private", "scripts", "keywords", "author", "contributors",
    "license", "repository", "homepage", "bugs", "funding",
    "sideEffects", "unpkg", "jsdelivr",
})


def parse_package_json(text: str) -> tuple[dict[str, frozenset], str]:
    """The four dependency maps AND the surface outside them.

    Returns ``(deps, other_surface)``: ``deps`` maps name → the set of
    registry specifiers across the maps (a name at two versions in two
    maps is both); ``other_surface`` is the canonical JSON of the whole
    document minus the four maps minus :data:`_INERT_PACKAGE_JSON_KEYS`
    — a change there (``overrides``, ``resolutions``,
    ``bundledDependencies``, ``workspaces``, any unknown key) is a
    dependency surface this parser cannot read and escalates (R2-3,
    the pyproject rule applied to npm: an override can install an
    alias the four maps never name).
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestRefused(f"package.json does not parse: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestRefused("package.json is not an object")
    found: dict[str, set] = {}
    for key in _PACKAGE_JSON_MAPS:
        mapping = doc.get(key)
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            raise ManifestRefused(f"package.json {key} is not an object")
        for name, version in mapping.items():
            if not isinstance(name, str) or not name:
                raise ManifestRefused(f"package.json {key} carries a bad name")
            spec = _npm_registry_specifier(version, f"package.json {key}")
            found.setdefault(normalise_name("npm", name), set()).add(spec)
    other = {
        key: value for key, value in doc.items()
        if key not in _PACKAGE_JSON_MAPS and key not in _INERT_PACKAGE_JSON_KEYS
    }
    return ({name: frozenset(versions) for name, versions in found.items()},
            _canonical(other))


_NPM_REGISTRY_TARBALL = "https://registry.npmjs.org/"


def canonical_npm_tarball(name: str, version) -> str:
    """The registry's canonical tarball URL for (name, version).

    Unscoped: ``…/<name>/-/<name>-<version>.tgz``; scoped
    ``@scope/name``: ``…/@scope/name/-/name-<version>.tgz`` (the file
    names the unscoped half). ``None`` when the version is missing —
    the caller refuses (a pin that cannot name its own tarball cannot
    be proven).
    """
    if not isinstance(version, str) or not version:
        return ""
    unscoped = name.rsplit("/", 1)[-1]
    return f"{_NPM_REGISTRY_TARBALL}{name}/-/{unscoped}-{version}.tgz"


def parse_package_lock(text: str) -> dict[str, tuple]:
    """package-lock.json v2/v3 ``packages`` pins, keyed by the FULL
    ``node_modules`` path (R1-1: nested entries for one package are
    distinct pins — keying by name let a nested bump overwrite the root
    pin, last-wins).

    Each value is ``(name, version, resolved, integrity, bundled)``: a
    changed ``resolved`` or ``integrity`` at the same version is a
    changed pin — the code the install fetches moved, which is the
    thing the policy judges. Refused at parse (R1-1/R2-1): ``link:
    true`` entries (workspace links install local code); a ``resolved``
    that is not EXACTLY the canonical tarball for the entry's (name,
    version) on ``https://registry.npmjs.org/`` (a same-host URL for
    another package's tarball is npm's own alias shape — the registry
    check would run on the name while the install fetches the other
    package); an entry ``name`` field that differs from its
    ``node_modules`` path's package name. An ADDED or CHANGED non-link,
    non-bundled entry without ``resolved`` refuses at addition time
    (R2-2, :class:`DependencyAnalyser`) — npm then fetches from
    whatever registry the environment configures. ``inBundle: true``
    entries are exempt there: bundled code ships inside its parent's
    tarball, which is itself checked.
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestRefused(
            f"package-lock.json does not parse: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestRefused("package-lock.json is not an object")
    packages = doc.get("packages")
    if not isinstance(packages, dict):
        raise ManifestRefused(
            "package-lock.json carries no v2/v3 'packages' map (a v1 "
            "'dependencies' lock has no deterministic shape to parse)"
        )
    pins: dict[str, tuple] = {}
    for key, value in packages.items():
        if "node_modules/" not in key:
            continue  # the root package ("") and any non-path entry
        name = key.rsplit("node_modules/", 1)[-1]
        if not name:
            continue
        if not isinstance(value, dict):
            raise ManifestRefused(
                f"package-lock entry {key!r} is not an object")
        if value.get("link") is True:
            raise ManifestRefused(
                f"package-lock entry {key!r} is a link (workspace link) — "
                "refused: it installs local code the registry checks "
                "never see"
            )
        entry_name = value.get("name")
        if entry_name is not None \
                and (not isinstance(entry_name, str)
                     or normalise_name("npm", entry_name)
                     != normalise_name("npm", name)):
            raise ManifestRefused(
                f"package-lock entry {key!r} names itself "
                f"{entry_name!r} — an alias (the path says {name!r}); the "
                "registry check would run on one package while the "
                "install fetches the other"
            )
        version = value.get("version")
        resolved = value.get("resolved")
        if resolved is not None:
            canonical = canonical_npm_tarball(
                normalise_name("npm", name),
                str(version) if version is not None else None)
            if not canonical or resolved != canonical:
                raise ManifestRefused(
                    f"package-lock entry {key!r} resolves to {resolved!r}, "
                    f"not the canonical tarball for {name}@{version} "
                    f"({canonical or 'uncomputable'}) — refused: the "
                    "install must fetch exactly the package and version "
                    "the registry check ran on"
                )
        integrity = value.get("integrity")
        pins[key] = (
            normalise_name("npm", name),
            str(version) if version is not None else None,
            resolved,
            str(integrity) if integrity is not None else None,
            value.get("inBundle") is True,
        )
    return pins


def manifest_kind(path: str):
    """Classify one manifest path: ``(ecosystem, parser, direct)`` or
    None when no parser exists for it (the caller escalates)."""
    base = path.rsplit("/", 1)[-1]
    if fnmatch.fnmatchcase(base, "requirements*.lock"):
        return "pypi", "requirements", False
    if fnmatch.fnmatchcase(base, "requirements*.txt"):
        return "pypi", "requirements", True
    if base == "pyproject.toml":
        return "pypi", "pyproject", True
    if base == "package.json":
        return "npm", "package_json", True
    if base == "package-lock.json":
        return "npm", "package_lock", False
    return None


# ------------------------------------------------------------ the analyser --


class DependencyAnalyser:
    """L0's added-dependency policy over one diff.

    ``allows(change, path)`` answers "does this manifest change pass
    without escalating?" and records one entry per checked package on
    ``.checks`` (the L0 report's ``dependency_checks``). Manifest-level
    failures (no parser, unparseable, over-size, unreadable) record a
    ``{manifest, verdict: fail, reason}`` entry.
    """

    def __init__(self, task: dict, repo, base_ref: str, head_ref: str,
                 client: RegistryClient | None = None) -> None:
        self._task = task or {}
        self._repo = Path(repo) if repo is not None else None
        self._base_ref = base_ref
        self._head_ref = head_ref
        self._client = client or RegistryClient()
        self.checks: list[dict] = []
        self._checked: set[tuple[str, str]] = set()
        self._registry_calls = 0
        declared = set()
        for entry in self._task.get("dependencies") or []:
            if isinstance(entry, dict) and entry.get("ecosystem") in (
                    "pypi", "npm") and isinstance(entry.get("name"), str):
                declared.add((
                    entry["ecosystem"],
                    normalise_name(entry["ecosystem"], entry["name"]),
                ))
        self._declared = declared
        self._merge_base = self._merge_base_or_none()

    # -- plumbing ----------------------------------------------------------

    def _merge_base_or_none(self) -> str | None:
        if self._repo is None or not self._base_ref or not self._head_ref:
            return None
        try:
            return _git(self._repo, "merge-base",
                        self._base_ref, self._head_ref).strip()
        except (GateError, UnicodeDecodeError):
            return None

    def _blob(self, ref: str, path: str) -> str:
        """One manifest blob at one ref; a file PROVEN absent from the
        ref's tree reads as empty (a manifest the diff adds has an empty
        base side, so every head entry is judged as an addition; a
        deleted one keeps its base side for the removal rule). Presence
        and absence are proven from the TREE (``ls-tree`` needs no
        blob, so no lazy-fetch failure can masquerade as either); every
        other failure raises :class:`ManifestRefused` — fail closed."""
        if self._repo is None or not ref:
            raise ManifestRefused(
                f"no refs to read {path} at — failing closed")
        try:
            listing = _git(self._repo, "ls-tree", ref, "--",
                           f":(literal){path}")
        except (GateError, UnicodeDecodeError) as exc:
            raise ManifestRefused(
                f"cannot prove {path} present or absent at {ref}: {exc}"
            ) from exc
        if not listing.strip():
            return ""  # proven absent at this ref
        try:
            size_out = _git(self._repo, "cat-file", "-s", f"{ref}:{path}")
            size = int(size_out.strip())
        except (GateError, UnicodeDecodeError, ValueError) as exc:
            raise ManifestRefused(
                f"cannot read the size of {path} at {ref}: {exc}") from exc
        if size > MAX_MANIFEST_BYTES:
            raise ManifestRefused(
                f"{path} at {ref} is {size} bytes (cap "
                f"{MAX_MANIFEST_BYTES}) — refusing to parse it"
            )
        try:
            return _git(self._repo, "show", f"{ref}:{path}")
        except (GateError, UnicodeDecodeError) as exc:
            raise ManifestRefused(
                f"cannot read {path} at {ref}: {exc}") from exc

    def _include_reader(self, ref: str, manifest: str):
        def read(rel: str) -> str:
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(manifest), rel.strip()))
            if target.startswith("..") or target.startswith("/"):
                raise ManifestRefused(
                    f"-r/-c include {rel!r} escapes the repo")
            return self._blob(ref, target)
        return read

    def _manifest_fail(self, path: str, reason: str) -> bool:
        self.checks.append({"manifest": path, "verdict": "fail",
                            "reason": reason})
        return False

    # -- the policy --------------------------------------------------------

    def _parse_side(self, kind: str, path: str, ref: str, text: str):
        if kind == "requirements":
            return _parse_requirements_text(
                text, self._include_reader(ref, path))
        if kind == "pyproject":
            return parse_pyproject(text)
        if kind == "package_json":
            return parse_package_json(text)
        return parse_package_lock(text)

    @staticmethod
    def _removals(direct: bool, base, head) -> list:
        """Direct-manifest names present at base and gone at head
        (sorted). Lock manifests return [] — they prune on every
        regen; only their additions are judged."""
        if not direct:
            return []
        return sorted(set(base) - set(head))

    @staticmethod
    def _additions(mode: str, base, head) -> dict:
        """Names added at head: absent at base, or present with a
        different pinned version. ``mode`` selects the entry shape:
        ``tuple`` (requirements/pyproject: (raw, version)), ``set``
        (package.json: specifier sets), ``lock`` (package-lock:
        ``{node_modules path: (name, version, resolved, integrity)}`` —
        a name is added when ANY path carrying it is new or changed;
        nested duplicates are distinct pins, never last-wins)."""
        added: dict = {}
        if mode == "tuple":
            for norm, (_raw, version) in head.items():
                if norm not in base or base[norm][1] != version:
                    added[norm] = version
        elif mode == "set":
            for norm, versions in head.items():
                if base.get(norm, frozenset()) != versions:
                    added[norm] = "+".join(sorted(versions)) or None
        else:  # lock
            for path_key, pin in head.items():
                if base.get(path_key) == pin:
                    continue
                name, version, _resolved, _integrity, _bundled = pin
                existing = added.get(name)
                added[name] = "+".join(
                    sorted({existing, version} - {None})) if existing \
                    else version
        return added

    def allows(self, change, path: str) -> bool:
        """Judge one changed manifest; True only when every added
        package is declared (direct ones) and passes the registry
        checks. Records the evidence on ``.checks`` either way."""
        kind = manifest_kind(path)
        if kind is None:
            return self._manifest_fail(
                path, f"no parser for {path} in dependency_manifests — "
                      "failing closed")
        ecosystem, parser, direct = kind
        merge_base = self._merge_base
        if merge_base is None:
            return self._manifest_fail(
                path, "no refs to compare manifests at — failing closed")
        try:
            base_text = self._blob(merge_base, path)
            head_text = self._blob(self._head_ref, path)
            base_side = self._parse_side(parser, path, merge_base, base_text)
            head_side = self._parse_side(parser, path, self._head_ref,
                                         head_text)
        except ManifestRefused as exc:
            return self._manifest_fail(path, str(exc))
        if parser == "pyproject":
            # (deps, other-surface): a dependency-bearing change outside
            # the two parsed tables is unprovable — escalate.
            base_deps, base_other = base_side
            head_deps, head_other = head_side
            if base_other != head_other:
                return self._manifest_fail(
                    path,
                    "pyproject dependency surface changed outside "
                    "[project] dependencies/optional-dependencies "
                    "(poetry/uv/dependency-groups/build-system/dynamic) — "
                    "failing closed")
            added = self._additions("tuple", base_deps, head_deps)
            removals = self._removals(direct, base_deps, head_deps)
        elif parser == "requirements":
            added = self._additions("tuple", base_side, head_side)
            removals = self._removals(direct, base_side, head_side)
        elif parser == "package_json":
            # R2-3: the surface outside the four maps is compared like
            # pyproject's — overrides/resolutions/bundledDependencies/
            # workspaces and any unknown key escalate; inert metadata
            # (scripts, engines, …) does not.
            base_deps, base_other = base_side
            head_deps, head_other = head_side
            if base_other != head_other:
                return self._manifest_fail(
                    path,
                    "package.json surface changed outside the four "
                    "dependency maps (overrides/resolutions/"
                    "bundle(d)Dependencies/workspaces or an unknown key) "
                    "— failing closed: an override can install a package "
                    "the maps never name")
            added = self._additions("set", base_deps, head_deps)
            removals = self._removals(direct, base_deps, head_deps)
        else:
            # R2-2: an ADDED or CHANGED non-link, non-bundled entry with
            # no `resolved` refuses — npm then fetches from whatever
            # registry the environment configures. Bundled entries ship
            # inside their parent's tarball (itself checked).
            lock_base = base_side if isinstance(base_side, dict) else {}
            lock_head = head_side if isinstance(head_side, dict) else {}
            for path_key, pin in lock_head.items():
                if lock_base.get(path_key) == pin:
                    continue
                # (name, version, resolved, integrity, bundled)
                resolved = pin[2] if isinstance(pin, tuple) and len(pin) > 2 \
                    else None
                bundled = pin[4] if isinstance(pin, tuple) and len(pin) > 4 \
                    else False
                if resolved is None and not bundled:
                    return self._manifest_fail(
                        path,
                        f"lock entry {path_key} carries no 'resolved' "
                        "tarball — npm would fetch it from whatever "
                        "registry the environment configures; failing "
                        "closed")
            added = self._additions("lock", lock_base, lock_head)
            removals = []
        # A REMOVED direct dependency is a deliberate contract change
        # the dependencies list does not cover (it declares additions) —
        # keep the pre-F-71 escalation. Lock files prune routinely on
        # every regen; only their additions are judged.
        if removals:
            return self._manifest_fail(
                path,
                f"direct dependencies removed ({', '.join(removals)}) — "
                "the contract's dependencies declare additions, never "
                "removals; failing closed (F-31 rule kept)")
        ok = True
        for norm in sorted(added):
            key = (ecosystem, norm)
            version = added[norm]
            if key in self._checked:
                # Seen in an earlier manifest of this diff (a package
                # often lands in the direct file and the lock at once):
                # one registry check, but an undeclared DIRECT add still
                # fails on its own.
                if direct and key not in self._declared:
                    self.checks.append({
                        "ecosystem": ecosystem, "name": norm,
                        "version": version, "declared": False,
                        "verdict": "fail",
                        "reason": "added to a direct manifest "
                                  "(requirements/pyproject/package.json) "
                                  "but not declared in the contract's "
                                  "dependencies",
                    })
                    ok = False
                continue
            self._checked.add(key)
            declared = key in self._declared
            if direct and not declared:
                self.checks.append({
                    "ecosystem": ecosystem, "name": norm,
                    "version": version, "declared": False,
                    "verdict": "fail",
                    "reason": "added to a direct manifest "
                              "(requirements/pyproject/package.json) but "
                              "not declared in the contract's dependencies",
                })
                ok = False
                continue
            # R1-2: the registry calls are sequential and each is
            # timeout-bounded; a whole-lock regen's worth of additions
            # would spend minutes-to-hours inside L0. Cap the calls per
            # run (fail closed beyond it, the count named) — a disk
            # cache across runs is proposed in BOOTSTRAP, not built.
            if self._registry_calls >= MAX_REGISTRY_CALLS:
                self.checks.append({
                    "ecosystem": ecosystem, "name": norm,
                    "version": version, "declared": declared,
                    "verdict": "fail",
                    "reason": f"the registry-call cap ({MAX_REGISTRY_CALLS} "
                              "per L0 run) was reached before this package "
                              "could be checked — fail closed (slice the "
                              "change or raise the cap by owner decision)",
                })
                ok = False
                continue
            self._registry_calls += 1
            entry = self._client.check(
                ecosystem, norm, version=version, declared=declared)
            self.checks.append(entry)
            if entry.get("verdict") != "pass":
                ok = False
        return ok
