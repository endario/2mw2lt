"""This machine's fingerprint, from its platform UUID (doc 143 §2).

The UUID is read from the platform and never stored, so a reinstall, a lost config directory or
another OS user reads the same one. What leaves the machine is a namespaced hash of it, which
the console keys into the machine id. There is no fallback: a minted value in its place would be
lost with whatever held it. Standard library only, since the door scripts run on the system
Python.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess

LINUX_UUID = "/sys/devices/virtual/dmi/id/product_uuid"
LINUX_FIX = ("it is root's alone until provisioning exposes it: run steering/host/agent-host.sh as root, "
             "which writes /etc/tmpfiles.d/2mw2lt-product-uuid.conf and applies it")
# Values that boards and hypervisors ship when they have none, so shared by unrelated machines.
PLACEHOLDERS = {"0" * 32, "f" * 32, "03000200040005000006000700080009"}

_CACHED: list[str] = []


class Unreadable(Exception):
    """This machine's hardware identity cannot be read, and the message says why."""


def _normal(raw: str, source: str) -> str:
    uuid = raw.strip().replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", uuid):
        raise Unreadable(f"{source} is not a UUID")
    if uuid in PLACEHOLDERS:
        raise Unreadable(f"{source} is a placeholder that identifies no machine")
    return uuid


def _darwin() -> str:
    seam = os.environ.get("STEERING_HARDWARE_IOREG")
    try:
        if seam:
            with open(seam) as f:
                out = f.read()
        else:
            out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unreadable(f"ioreg did not answer: {e}")
    m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]*)"', out)
    if not m:
        raise Unreadable("ioreg names no IOPlatformUUID")
    return _normal(m.group(1), "IOPlatformUUID")


def _linux() -> str:
    path = os.environ.get("STEERING_HARDWARE_UUID_FILE") or LINUX_UUID
    try:
        with open(path) as f:
            raw = f.read()
    except PermissionError:
        raise Unreadable(f"{path} is not readable by this user: {LINUX_FIX}")
    except OSError as e:
        raise Unreadable(f"{path} cannot be read: {e}")
    return _normal(raw, path)


def fingerprint(system: str | None = None) -> str:
    """The namespaced sha256 of this machine's platform UUID, in hex; `Unreadable` otherwise."""
    if _CACHED:
        return _CACHED[0]
    system = system or platform.system()
    if system == "Darwin":
        uuid = _darwin()
    elif system == "Linux":
        uuid = _linux()
    else:
        raise Unreadable(f"no hardware identity is read on {system}")
    got = hashlib.sha256(b"2mw2lt machine v1\0" + uuid.encode()).hexdigest()
    _CACHED.append(got)
    return got
