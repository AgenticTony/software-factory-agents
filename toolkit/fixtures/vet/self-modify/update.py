"""Seeded fixture: self-modification / auto-update."""
from pathlib import Path

SOURCE = "upstream-latest"


def update():
    # self-update: fetch the newest revision and overwrite this file
    newest = fetch(SOURCE)
    open(__file__, "w").write(newest)
