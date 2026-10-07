from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import time
import re
import secrets
import subprocess
import sys
from pathlib import Path

import atomic_file

NODE_FILE = Path(os.environ.get("STEERING_NODE_FILE", str(Path.home() / ".steering" / "node-id")))


def valid_node(v: str) -> bool:
    return len(v) == 32 and all(c in "0123456789abcdef" for c in v)


def node_id(node_file: Path | None = None) -> str | None:
    """A 128-bit id minted on first use, or None when the machine's canonical value cannot be
    read or is not one — never a malformed id, never an exception."""
    node_file = node_file or NODE_FILE
    try:
        v = node_file.read_text().strip()
        if valid_node(v):
            return v
        if v or node_file.exists():
            return None  # a canonical file with the wrong contents is an operator's problem, not a new id
    except OSError:
        pass
    try:
        node_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            atomic_file.write_atomic(node_file, secrets.token_hex(16), mode=0o600, overwrite=False)
        except FileExistsError:
            pass
        v = node_file.read_text().strip()
        return v if valid_node(v) else None
    except OSError:
        return None


def process_start(pid: int, timeout: float = 1.0) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, "LC_ALL": "C"}).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    # `ps` is setuid, and a sandboxed process may not exec it: a confined brain's observe hook
    # could name no runtime (doc 94 §3). The kernel's birth instant, in `ps`'s own `lstart`
    # format, is the same string for a process of this user, so the id derived from it is too.
    return out or _lstart(pid)


def _lstart(pid: int) -> str | None:
    b = birth(pid)
    return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(b[0])) if b else None


_PROC_PIDTBSDINFO = 3


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32), ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32), ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32), ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64)]


def birth(pid: int) -> tuple[int, int] | None:
    """The kernel's birth instant of `pid` as (sec, usec) from one proc_pidinfo call, or None
    when there is no such process or the platform has no libproc."""
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("proc"))
        info = _ProcBsdInfo()
        n = lib.proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    except Exception:
        return None
    if n != ctypes.sizeof(info) or info.pbi_pid != int(pid):
        return None
    return int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)


def _ppid(pid: int, timeout: float = 1.0) -> int | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=timeout, env={**os.environ, "LC_ALL": "C"}).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(out) if out.isdigit() else None


def parent_of(pid: int) -> int | None:
    """The parent of `pid`, or None when there is no such process. Unforgeable by the child:
    only the kernel sets it, and only a fork by the parent itself produces it.

    `birth`'s call carries it where libproc is; `ps` answers where it is not, as `started_at`
    does — the guards below run on Linux, where the native path returns nothing at all.
    """
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("proc"))
        info = _ProcBsdInfo()
        n = lib.proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    except Exception:
        return _ppid(pid)
    if n != ctypes.sizeof(info) or info.pbi_pid != int(pid):
        return _ppid(pid)
    return int(info.pbi_ppid)


def started_at(pid: int) -> str | None:
    """When `pid` started, or None when there is no such process — the fact a reused pid does
    not carry, and the one this system already keys process identity on.

    `birth` is one native call and answers where libproc does; `ps` answers where it does not.
    Preferring one and falling back keeps a machine on one of the two, so a sighting and the
    check that re-validates it are not read in different units.
    """
    b = birth(pid)
    return f"{b[0]}.{b[1]:06d}" if b is not None else process_start(pid)


def started_epoch(pid: int) -> float | None:
    """Unix start seconds, converted locally where only ps's lstart is available."""
    b = birth(pid)
    if b is not None:
        return b[0] + b[1] / 1_000_000
    start = process_start(pid)
    if not start:
        return None
    try:
        weekday, month, day, clock, year = start.split()
        if weekday not in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            return None
        # ps forces C locale; numeric parsing does not depend on this process's LC_TIME.
        month_number = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split().index(month) + 1
        local = time.strptime(f"{month_number} {day} {clock} {year}", "%m %d %H:%M:%S %Y")
        candidates = set()
        # lstart omits the UTC offset: a repeated local hour cannot declare which epoch it names.
        for dst in (0, 1):
            candidate = time.mktime(local[:8] + (dst,))
            if time.localtime(candidate)[:6] == local[:6]:
                candidates.add(candidate)
        return candidates.pop() if len(candidates) == 1 else None
    except (ValueError, OverflowError, OSError):
        return None


def _procargs(data: bytes) -> list[str] | None:
    if len(data) < ctypes.sizeof(ctypes.c_int):
        return None
    count = ctypes.c_int.from_buffer_copy(data[:4]).value
    if count <= 0:
        return None
    start = data.find(b"\0", 4)
    if start < 0:
        return None
    start += 1
    while start < len(data) and data[start] == 0:
        start += 1
    parts = data[start:].split(b"\0", count)
    if len(parts) <= count:
        return None
    return [os.fsdecode(part) for part in parts[:count]]


def process_argv(pid: int) -> list[str] | None:
    """Exact argument boundaries. This module also runs in dependency-free enrollment hooks."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if sys.platform != "darwin":
            data = Path(f"/proc/{pid}/cmdline").read_bytes()
            return [os.fsdecode(part) for part in data.split(b"\0")[:-1]] if data.endswith(b"\0") else None
        # KERN_PROCARGS2 carries argc, the executable's path, padding, argv and then env.
        lib = ctypes.CDLL(ctypes.util.find_library("c"))
        lib.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        lib.sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if lib.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if lib.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            return None
        return _procargs(buf.raw[:size.value])
    except (OSError, AttributeError, ValueError):
        return None


def _user_data_dir(args: list[str] | None) -> str | None:
    if not args or any(a == "--type" or a.startswith("--type=") for a in args[1:]):
        return None
    values = []
    for i, arg in enumerate(args[1:], 1):
        if arg == "--user-data-dir":
            if i + 1 >= len(args) or not args[i + 1] or args[i + 1].startswith("-"):
                return None
            values.append(args[i + 1])
        elif arg.startswith("--user-data-dir="):
            values.append(arg.partition("=")[2])
    return values[0] if len(values) == 1 and values[0] else None


def _ancestors(pid: int, hops: int = 8) -> list[int]:
    chain, p = [], pid
    for _ in range(hops):
        chain.append(p)
        q = parent_of(p)
        if not q or q == 1:
            break
        p = q
    return chain


def instance_of(pid: int) -> tuple[str, int] | None:
    """The nearest non-helper ancestor naming a user-data-dir, read from its exact argv."""
    for ancestor in _ancestors(pid)[1:]:
        udd = _user_data_dir(process_argv(ancestor))
        if udd:
            return udd, ancestor
    return None


def verify_instance(pid: int, app_pid: int, udd: str) -> bool:
    """A live ancestor naming exactly this user-data-dir, not a substring of shell display text."""
    if not isinstance(udd, str) or not udd or app_pid == pid or app_pid not in _ancestors(pid):
        return False
    return instance_of(pid) == (udd, app_pid)


def runtime_of(pid: int, provider: str = "claude") -> dict | None:
    """The v3 runtime record for `pid`: one native capture, or None."""
    b = birth(pid)
    node = node_id() if b else None
    if not b or not node:
        return None
    return {"provider": provider, "node": node, "pid": int(pid), "birth_sec": b[0], "birth_usec": b[1]}


def encode_v3(observation: dict, runtime: dict | None) -> dict:
    """The v3 form of an observation for `runtime`: both fields together, or the input unchanged."""
    if not runtime:
        return observation
    return {**observation, "v": 3, "runtime": dict(runtime), "runtime_id": identity(runtime)}


# The shape of a runtime id, with the provider left open. Which providers there are is the
# registry's answer and it is asked when an id is checked, not when this module loads: baked in
# at import, the list had to be edited alongside every registration, so a harness added in one
# place enrolled and was then refused at `bind:` by a grammar that had never heard of it.
# `machine_harness` imports this module, so the import here is deferred rather than hoisted.
_SHAPE = re.compile(r"^([^:]+):([0-9a-f]{32}):(v3:)?[0-9a-f]{64}$")


def provider_of(runtime_id: str) -> str | None:
    """The provider a runtime id names, whether or not it is one this machine knows."""
    m = _SHAPE.fullmatch(runtime_id or "")
    return m.group(1) if m else None


def node_of(runtime_id: str) -> str | None:
    """The minted node a runtime id names — the same term an enrolment's identity carries
    (doc 28 §4), read through this module's grammar rather than off a split."""
    m = _SHAPE.fullmatch(runtime_id or "")
    return m.group(2) if m else None


def valid(runtime_id: str) -> bool:
    """Whether this is a runtime id of a harness the registry answers for."""
    import machine_harness
    return machine_harness.of(provider_of(runtime_id)) is not None


def generation(runtime_id: str) -> int:
    """2 for an id hashed from `lstart` (no generation segment), 3 for one that names it."""
    return 3 if ":v3:" in runtime_id else 2


def identity(runtime: dict) -> str:
    """The v3 runtime id: provider, node, the generation, and the hash of pid and kernel birth."""
    digest = hashlib.sha256(f"{runtime['pid']}|{runtime['birth_sec']}|{runtime['birth_usec']}".encode()).hexdigest()
    return f"{runtime['provider']}:{runtime['node']}:v3:{digest}"


def derive(provider: str = "claude", pid: int | None = None, timeout: float = 1.0) -> str | None:
    """The id of the process `pid` (default: this process's parent) as an incarnation, or None
    when its start time cannot be read — an observation then carries no runtime id."""
    pid = os.getppid() if pid is None else pid
    try:
        start = process_start(pid, timeout)
        node = node_id() if start else None
    except Exception:
        return None
    if not start or not node:
        return None
    return f"{provider}:{node}:{hashlib.sha256(f'{pid}|{start}'.encode()).hexdigest()}"


if __name__ == "__main__":
    print(derive() or "")
