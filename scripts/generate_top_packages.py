#!/usr/bin/env python3
"""Generate the shipped top-package lists (F-71 AC-4).

One file per ecosystem under ``factory/data/top_packages/`` (``pypi.txt``,
``npm.txt``): the packages a newly added dependency must not be a
Damerau-Levenshtein-1 look-alike of. Regenerate by hand, never in CI or
tests:

    python scripts/generate_top_packages.py

Sources (recorded in each file's header, with the date):

* pypi — Hugo van Kemenade's top-pypi-packages dataset (a public
  BigQuery-over-PyPI-download-metrics snapshot, refreshed monthly),
  top ``--count`` rows by 30-day download count.
* npm — the npm registry's own search API (``/-/v1/search``), queried
  across a spread of common terms with the popularity filter, then
  ranked by the API's reported monthly downloads, top ``--count``.

Both feeds are popularity rankings of the names attackers actually
typosquat. The lists are a SNAPSHOT: staleness weakens the near-miss
check (a new popular package is simply absent) but never widens it.

Fails loudly (exit 1) when a source cannot be fetched after retries —
a short list generated from a broken fetch would silently weaken the
check. Never writes a partial file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "factory" / "data" / "top_packages"

PYPI_URL = ("https://hugovk.dev/top-pypi-packages/"
            "top-pypi-packages-30-days.min.json")
NPM_SEARCH_URL = "https://registry.npmjs.org/-/v1/search"
NPM_TERMS = ("not", "utils", "js", "node", "test", "cli", "http", "react",
             "async", "color", "file", "server", "data", "string", "type",
             "core", "lint", "webpack", "babel", "eslint")
RETRIES = 3
RETRY_WAIT_SEC = 5.0
TIMEOUT_SEC = 60.0

# The header records the source and date; the F-71 near-miss check reads
# everything after the leading '#' comment lines.
HEADER = """\
# {title} top packages — F-71 AC-4 near-miss list.
# Source: {source}
# Generated: {generated} by scripts/generate_top_packages.py
# ({count} names, normalised: {normalised})
"""


def _curl(url: str) -> str:
    for attempt in range(1, RETRIES + 1):
        proc = subprocess.run(
            ["curl", "-sSL", "--max-time", str(TIMEOUT_SEC), "--fail", url],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        print(f"  fetch failed (attempt {attempt}/{RETRIES}): "
              f"rc={proc.returncode} {proc.stderr.strip()[:120]}", file=sys.stderr)
        if attempt < RETRIES:
            time.sleep(RETRY_WAIT_SEC)
    raise SystemExit(f"cannot fetch {url} after {RETRIES} attempts — "
                     "no partial list is written")


def pypi_names(count: int) -> list[str]:
    doc = json.loads(_curl(PYPI_URL))
    names = [row["project"] for row in doc["rows"][:count]]
    if len(names) < count:
        raise SystemExit(f"pypi source returned {len(names)} names, "
                         f"expected {count}")
    return names


def npm_names(count: int) -> list[str]:
    """Union the term queries, rank by the API's monthly downloads."""
    best: dict[str, int] = {}
    for term in NPM_TERMS:
        url = (f"{NPM_SEARCH_URL}?text={urllib.parse.quote(term)}"
               f"&size=250&popularity=1.0-2.0")
        objects = json.loads(_curl(url)).get("objects", [])
        if not objects:
            raise SystemExit(f"npm query for {term!r} returned no objects — "
                             "no partial list is written")
        for obj in objects:
            name = obj["package"]["name"]
            downloads = (obj.get("downloads") or {}).get("monthly") or 0
            if downloads > best.get(name, -1):
                best[name] = downloads
    ranked = sorted(best, key=lambda n: -best[n])[:count]
    if len(ranked) < count:
        raise SystemExit(f"npm union produced {len(ranked)} names, "
                         f"expected {count}")
    return ranked


def _write(ecosystem: str, names: list[str], source: str) -> None:
    path = DATA_DIR / f"{ecosystem}.txt"
    seen: set[str] = set()
    rows = []
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(name)
    path.write_text(
        HEADER.format(
            title=ecosystem.upper(), source=source,
            generated=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            count=len(rows), normalised="pypi PEP 503 / npm lowercase",
        ) + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path} ({len(rows)} names)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=500,
                        help="names per list (default %(default)s)")
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write("pypi", pypi_names(args.count),
           f"{PYPI_URL} (top {args.count} by 30-day downloads)")
    _write("npm", npm_names(args.count),
           f"{NPM_SEARCH_URL} popularity-filtered term queries "
           f"({', '.join(NPM_TERMS[:5])}, …), ranked by monthly downloads")


if __name__ == "__main__":
    main()
