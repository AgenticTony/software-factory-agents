"""Seeded fixture: shell execution / eval."""


import subprocess


def tidy(path):
    return subprocess.run(["rm", "-rf", path])


def run_user_snippet(code):
    return eval(code)
