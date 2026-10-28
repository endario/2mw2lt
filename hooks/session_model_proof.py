#!/usr/bin/env python3
"""Publish the classic SessionStart identity for one restricted ordinary launch.

The agent, not this hook, validates the identity against the compose evidence and writes
accepted.json. Unrestricted sessions have no STEERING_MODEL_PROOF and publish nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "steering"))

import atomic_file  # noqa: E402


def main() -> int:
    proof = os.environ.get("STEERING_MODEL_PROOF", "")
    if not proof:
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError):
        print("2mw2lt model proof: SessionStart payload was not an object", file=sys.stderr)
        return 0
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return 0
    launch = os.environ.get("STEERING_MODEL_PROOF_LAUNCH", "")
    session = payload.get("session_id")
    source = payload.get("source")
    if not launch or not isinstance(session, str) or not session or not isinstance(source, str) or not source:
        print("2mw2lt model proof: SessionStart lacks its launch, session or source", file=sys.stderr)
        return 0
    record = {"launch": launch, "session_id": session, "source": source, "hook_ppid": os.getppid()}
    try:
        atomic_file.write_atomic(Path(proof) / "session-start.json", json.dumps(record), 0o600)
    except OSError as err:
        print(f"2mw2lt model proof: SessionStart publication failed: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
