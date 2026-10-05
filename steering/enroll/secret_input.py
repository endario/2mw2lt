"""A secret a client is handed reaches it on stdin, never as an argument: an argument sits in the
process table, where any user on the machine can read it, and in the shell's history."""
from __future__ import annotations

import getpass
import sys


def read_secret(prompt: str) -> str:
    """One line from stdin, stripped; prompted for without echo when stdin is a terminal."""
    if sys.stdin is None:
        return ""
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline().strip()
