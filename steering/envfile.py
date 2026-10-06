"""The root `.env`, read into a process that launchd started."""

from __future__ import annotations

import python_floor

python_floor.require()

import os
import sys
from pathlib import Path


def _pair(line: str) -> tuple[str, str] | None:
    """The key and value one line names, or None for a blank, a comment or a valueless line."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    return key.strip(), value.strip().strip('"').strip("'")


def load(path: Path) -> None:
    """Read `path` into the environment, without overriding what the caller already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        pair = _pair(line)
        if pair and pair[0] and pair[0] not in os.environ:
            os.environ[pair[0]] = pair[1]


def value(path: Path, key: str) -> str | None:
    """The one key's value in `path`, or None — read without touching the environment, by a
    caller that wants what the file says now rather than what it said at start."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        pair = _pair(line)
        if pair and pair[0] == key and pair[1]:
            return pair[1]
    return None


def main(argv: list[str]) -> int:
    """Consume a private environment file, then replace this process with its command."""
    if len(argv) < 2:
        return 2
    from agentjob import private_file
    path = Path(argv[0])
    if not private_file(path):
        print("launch environment file is not private", file=sys.stderr)
        return 1
    try:
        load(path)
        path.unlink()
        os.execvpe(argv[1], argv[1:], os.environ)
    except (OSError, ValueError) as e:
        print(f"launch environment could not be consumed: {type(e).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
