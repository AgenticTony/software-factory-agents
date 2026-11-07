"""Seeded fixture: self-modification / auto-update."""
import os


def update(source_text):
    # self-update: overwrite this file with the newest revision
    open(__file__, "w").write(source_text)
    return os.path.exists(__file__)
