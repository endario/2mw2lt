"""This machine's fingerprint, from its platform UUID (doc 143 §2).

The UUID is read from the platform and never stored, so a reinstall, a lost config directory or
another OS user reads the same one. What leaves the machine is a namespaced hash of it, which
the console keys into the machine id. A Linux board that ships a placeholder UUID is known by its
wired ports instead (doc 143 §2a). There is no fallback: a minted value in its place would be
lost with whatever held it. Standard library only, since the door scripts run on the system
Python.
"""
from __future__ import annotations

import hashlib
import json
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


class _Placeholder(Unreadable):
    """The platform UUID is one boards ship when they have none."""


def _normal(raw: str, source: str) -> str:
    uuid = raw.strip().replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", uuid):
        raise Unreadable(f"{source} is not a UUID")
    if uuid in PLACEHOLDERS:
        raise _Placeholder(f"{source} is a placeholder that identifies no machine")
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


LINUX_NET = "/sys/class/net"
_SIOCETHTOOL, _ETHTOOL_GPERMADDR, _MAX_ADDR = 0x8946, 0x20, 32


def _permanent(name: str) -> str:
    """The port's permanent address, by ETHTOOL_GPERMADDR, which needs no privilege; "" when none."""
    import array, fcntl, socket, struct
    buf = array.array("B", struct.pack("II", _ETHTOOL_GPERMADDR, _MAX_ADDR) + bytes(_MAX_ADDR))
    addr, _ = buf.buffer_info()
    ifreq = struct.pack("16sP", name.encode()[:15], addr).ljust(40, b"\0")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            fcntl.ioctl(s.fileno(), _SIOCETHTOOL, ifreq)
        except OSError:
            return ""
    size = struct.unpack_from("I", buf, 4)[0]
    return ":".join(f"{b:02x}" for b in buf[8:8 + min(size, _MAX_ADDR)])


def _ports() -> list[dict]:
    """Every interface, as the anchor reads it: its bus, whether it is wireless, its permanent address."""
    seam = os.environ.get("STEERING_HARDWARE_PORTS")
    if seam:
        with open(seam) as f:
            return json.load(f)
    found = []
    for name in sorted(os.listdir(LINUX_NET)):
        base = os.path.join(LINUX_NET, name)
        bus = os.path.basename(os.path.realpath(os.path.join(base, "device", "subsystem")))
        found.append({"name": name, "bus": bus if os.path.exists(os.path.join(base, "device")) else "",
                      "wireless": os.path.isdir(os.path.join(base, "wireless")), "permanent": _permanent(name)})
    return found


def _board_ports(placeholder: str) -> str:
    """A placeholder board's anchor (doc 143 §2a): its wired PCI ports' permanent addresses, as a set."""
    try:
        ports = _ports()
    except (OSError, ValueError) as e:
        raise Unreadable(f"{placeholder}, and the board's ports cannot be read: {e}")
    addrs = sorted({p["permanent"].lower() for p in ports
                    if p.get("bus") == "pci" and not p.get("wireless")
                    and re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", p.get("permanent", "").lower())
                    and p["permanent"] != "00:00:00:00:00:00"})
    if not addrs:
        raise Unreadable(f"{placeholder}, and the board has no wired port with a permanent address")
    return "\n".join(addrs)


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
        try:
            uuid = _linux()
        except _Placeholder as e:
            ports = _board_ports(str(e))
            got = hashlib.sha256(b"2mw2lt machine v1 ports\0" + ports.encode()).hexdigest()
            _CACHED.append(got)
            return got
    else:
        raise Unreadable(f"no hardware identity is read on {system}")
    got = hashlib.sha256(b"2mw2lt machine v1\0" + uuid.encode()).hexdigest()
    _CACHED.append(got)
    return got
