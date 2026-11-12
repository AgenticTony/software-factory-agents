"""Registry checks for added dependencies (F-71 AC-4).

VALIDATOR-SPEC L0 deferred these checks to the day a contract can
declare its dependencies: a newly added package must exist in its
public registry, its FIRST release must be at least
:data:`FIRST_RELEASE_MIN_AGE_DAYS` old, and its name must not be a
Damerau-Levenshtein-1 look-alike of a top package it is not itself.
They run PARENT-side (L0 runs in the orchestrator, outside the
sandbox) against the public JSON APIs, and every failure fails CLOSED:
an unreadable registry is an escalation, never a pass.

The ONE network seam is :func:`_http_get_json` — ``RegistryClient``
takes it injectable, tests stub it, and nothing here is reachable from
inside a sandbox.
"""

from __future__ import annotations

import functools
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The shipped near-miss lists (F-71 AC-4): generated ONCE by
# scripts/generate_top_packages.py, source and date in each header.
TOP_PACKAGES_DIR = Path(__file__).resolve().parent / "data" / "top_packages"

# A package published more recently than this fails (named constant; the
# 30-day value is the spec's, VALIDATOR-SPEC L0).
FIRST_RELEASE_MIN_AGE_DAYS = 30

# The registry call's own limits: short timeout, bounded body. The body
# bound is MEASURED (R1-2, 2026-11-11): the largest real documents are
# npm's — next 31.5 MB, typescript 15.9 MB, react 7.1 MB; pypi tops out
# around numpy's 3.7 MB. 40 MiB covers the largest with ~27% headroom
# while still refusing a flood; the read is streamed in chunks so an
# over-size body aborts at the cap, never buffered whole.
REGISTRY_TIMEOUT_SEC = 10.0
REGISTRY_MAX_BYTES = 40 * 1024 * 1024  # 40 MiB (measured: npm next 31.5 MB)
_READ_CHUNK = 1024 * 1024

_REGISTRY_URLS = {
    "pypi": "https://pypi.org/pypi/{name}/json",
    "npm": "https://registry.npmjs.org/{name}",
}

# ISO-8601 with any trailing Z handled (both registries use it).
_ISO_STAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


class RegistryError(Exception):
    """The registry could not be consulted (network, size, parse). The
    package FAILS — fail closed, never a pass on a broken check."""


def normalise_name(ecosystem: str, name: str) -> str:
    """One canonical spelling per ecosystem: PEP 503 for pypi
    (lowercase, runs of ``-``/``_``/``.`` collapse to ``-``), lowercase
    for npm (scoped names keep their ``@scope/``)."""
    if ecosystem == "pypi":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name.lower()


@functools.lru_cache(maxsize=2)
def load_top_packages(ecosystem: str) -> frozenset:
    """The shipped top-package names for one ecosystem, NORMALISED.

    Read once and cached; comment header lines (``#``) are skipped. A
    missing, unreadable or EMPTY list RAISES :class:`RegistryError`
    (R1-5): an absent list must fail the check — silently disabling the
    near-miss rule is fail-open, and the shipped package data (F-51's
    wheel test) is what makes the list present, not this function's
    fallback.
    """
    path = TOP_PACKAGES_DIR / f"{ecosystem}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(
            f"the shipped {ecosystem} top-package list ({path}) cannot be "
            f"read — the near-miss check refuses to run blind: {exc}"
        ) from exc
    names = frozenset(
        normalise_name(ecosystem, line.strip())
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not names:
        raise RegistryError(
            f"the shipped {ecosystem} top-package list ({path}) is empty — "
            "the near-miss check refuses to run blind"
        )
    return names


def damerau_levenshtein(a: str, b: str, *, cap: int = 2) -> int:
    """Optimal-string-alignment distance between two strings, capped.

    Transpositions count as one edit (the classic ``reqeusts`` /
    ``requests`` shape plain Levenshtein scores 2). ``cap`` bounds the
    answer: once the distance provably exceeds it, ``cap + 1`` is
    returned — the near-miss check only ever asks "is it ≤ 1".
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    rows, cols = len(a) + 1, len(b) + 1
    previous2: list[int] | None = None
    previous: list[int] = list(range(cols))
    for i in range(1, rows):
        current = [i] + [0] * (cols - 1)
        for j in range(1, cols):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            best = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost,  # substitution
            )
            if (
                previous2 is not None and i > 1 and j > 1
                and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]
            ):
                best = min(best, previous2[j - 2] + 1)  # transposition
            current[j] = best
        if min(current) > cap and min(previous) > cap:
            return cap + 1
        previous2, previous = previous, current
    return previous[-1]


def _parse_iso(stamp: str) -> datetime | None:
    """An ISO-8601 registry timestamp as an aware UTC datetime."""
    match = _ISO_STAMP.match(stamp.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second = (
        int(match.group(i)) for i in range(1, 7)
    )
    micro = int((match.group(7) or "0").ljust(6, "0")[:6])
    try:
        naive = datetime(year, month, day, hour, minute, second, micro)
    except ValueError:
        return None
    offset = match.group(8)
    if offset and offset != "Z":
        offset = offset.replace(":", "")
        sign = 1 if offset[0] == "+" else -1
        delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        naive -= sign * delta
    return naive.replace(tzinfo=timezone.utc)


def _http_get_json(ecosystem: str, name: str):
    """The ONE network seam: fetch the package's registry document.

    URL-encodes the name (npm scopes carry ``/``), enforces the timeout
    and the size cap — streamed in chunks, so an over-size body aborts
    at the cap instead of buffering whole — and parses JSON. Any failure
    raises :class:`RegistryError`; it never returns half a document.
    """
    url = _REGISTRY_URLS[ecosystem].format(
        name=urllib.parse.quote(name, safe=""))
    try:
        with urllib.request.urlopen(url, timeout=REGISTRY_TIMEOUT_SEC) as resp:
            body = bytearray()
            while len(body) <= REGISTRY_MAX_BYTES:
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    break
                body += chunk
    except (OSError, urllib.error.URLError) as exc:
        raise RegistryError(f"{ecosystem} registry unreachable: {exc}") from exc
    if len(body) > REGISTRY_MAX_BYTES:
        raise RegistryError(
            f"{ecosystem} registry response for {name} exceeds "
            f"{REGISTRY_MAX_BYTES} bytes — refusing to trust it"
        )
    try:
        return json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(
            f"{ecosystem} registry response for {name} is not JSON: {exc}"
        ) from exc


# The real seam, kept addressable so a test can restore it after the
# suite-wide stub and exercise the real read path (the R1-2 size-cap
# arm) with urlopen faked — no network either way.
_http_get_json_real = _http_get_json


def _first_release(ecosystem: str, name: str, doc: dict) -> datetime:
    """The earliest release timestamp the registry reports.

    PyPI: the earliest ``upload_time_iso_8601`` across every release's
    files. npm: the earliest version timestamp in ``time`` (the
    ``created``/``modified`` bookkeeping keys excluded).
    """
    stamps: list[str] = []
    if ecosystem == "pypi":
        releases = doc.get("releases")
        if not isinstance(releases, dict) or not releases:
            raise RegistryError(
                f"pypi reports no releases for {name} — existence cannot "
                "be proven"
            )
        for files in releases.values():
            for entry in files if isinstance(files, list) else []:
                stamp = (entry or {}).get("upload_time_iso_8601") \
                    or (entry or {}).get("upload_time")
                if isinstance(stamp, str):
                    stamps.append(stamp)
    else:
        times = doc.get("time")
        if not isinstance(times, dict):
            raise RegistryError(
                f"npm reports no time map for {name} — age cannot be proven"
            )
        stamps = [
            stamp for key, stamp in times.items()
            if key not in ("created", "modified") and isinstance(stamp, str)
        ]
    if not stamps:
        raise RegistryError(
            f"{ecosystem} reports no release timestamps for {name}"
        )
    parsed = [ts for ts in (_parse_iso(s) for s in stamps) if ts is not None]
    if not parsed:
        raise RegistryError(
            f"{ecosystem} release timestamps for {name} do not parse"
        )
    return min(parsed)


class RegistryClient:
    """Runs the registry checks for one added package.

    Everything above HTTP is real in tests too: only ``get_json`` (the
    network fetch) is injectable.
    """

    def __init__(self, get_json=None) -> None:
        self._get_json = get_json or _http_get_json

    def check(self, ecosystem: str, name: str, *, version: str | None = None,
              declared: bool = False, now: datetime | None = None) -> dict:
        """One ``dependency_checks`` entry for a package the diff added.

        The verdict is ``pass`` only when the package exists (the
        registry answered for THAT name), its first release is at least
        :data:`FIRST_RELEASE_MIN_AGE_DAYS` old by the parent's UTC clock
        (a future timestamp fails), and its normalised name is not
        within edit distance 1 of a shipped top package it is not
        itself. Anything else fails with the reason named.
        """
        norm = normalise_name(ecosystem, name)
        entry = {
            "ecosystem": ecosystem,
            "name": norm,
            "version": version,
            "declared": declared,
            "exists": False,
            "first_release": None,
            "near_miss": None,
            "verdict": "fail",
            "reason": "",
        }

        def _fail(reason: str) -> dict:
            entry["reason"] = reason
            return entry

        try:
            doc = self._get_json(ecosystem, norm)
        except RegistryError as exc:
            return _fail(str(exc))
        except (TimeoutError, OSError) as exc:
            return _fail(f"{ecosystem} registry unreachable: {exc}")
        if not isinstance(doc, dict):
            return _fail(
                f"{ecosystem} registry response for {norm} is not a JSON "
                "object"
            )
        # R1-8: a malformed document (``info`` a list, ``name`` a dict)
        # must fail the CHECK, never crash L0 — every field is typed
        # before use and a wrong shape lands in the echoed-name refusal.
        echoed: object = doc.get("name")
        if ecosystem == "pypi":
            info = doc.get("info")
            echoed = info.get("name") if isinstance(info, dict) else None
        if not isinstance(echoed, str) \
                or normalise_name(ecosystem, echoed) != norm:
            return _fail(
                f"{ecosystem} registry answered for "
                f"{echoed!r}, not {norm!r}"
            )
        entry["exists"] = True
        try:
            first = _first_release(ecosystem, norm, doc)
        except RegistryError as exc:
            return _fail(str(exc))
        entry["first_release"] = first.strftime("%Y-%m-%dT%H:%M:%SZ")
        clock = now or datetime.now(timezone.utc)
        age_days = (clock - first).total_seconds() / 86400.0
        if age_days < 0:
            return _fail(
                f"first release {entry['first_release']} is in the future"
            )
        if age_days < FIRST_RELEASE_MIN_AGE_DAYS:
            return _fail(
                f"first release {entry['first_release']} is only "
                f"{age_days:.1f} days old (minimum "
                f"{FIRST_RELEASE_MIN_AGE_DAYS})"
            )
        # A name that is ITSELF on the shipped list skips the near-miss
        # scan: it has proven popularity, and its siblings are the
        # shipped list's own false-positive class (psycopg2 sits one
        # edit off psycopg; @tsconfig/node16 off node12) — measured at
        # 4.0% (pypi) / 5.2% (npm) of the shipped names. An attacker
        # cannot get onto a snapshot. A missing or empty list FAILS the
        # check (R1-5) — never a silent off.
        try:
            top = load_top_packages(ecosystem)
        except RegistryError as exc:
            return _fail(str(exc))
        if norm not in top:
            for other in top:
                if damerau_levenshtein(norm, other, cap=1) <= 1:
                    entry["near_miss"] = other
                    return _fail(
                        f"the name is within edit distance 1 of top package "
                        f"'{other}' — a look-alike of a popular name is the "
                        "typosquat shape"
                    )
        entry["verdict"] = "pass"
        return entry
