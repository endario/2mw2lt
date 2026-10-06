"""The Python the plugin's scripts need, refused in words a person can act on.

An entrypoint that can reach code needing a newer Python calls `require()` before importing
anything else, so a stock macOS `python3` (3.9) gets this sentence rather than a traceback; one
that cannot reach it does not call it (python_floor_test.py). It must itself parse and run on the
oldest Python a person may have.
"""
import sys

NEEDS = (3, 11)


def require(found=None) -> None:
    found = tuple((found or sys.version_info)[:2])
    if found < NEEDS:
        sys.exit(f"2mw2lt needs Python {NEEDS[0]}.{NEEDS[1]} or later; this is {found[0]}.{found[1]}. "
                 "Install a newer one (`brew install python` on macOS) and run it again.")
