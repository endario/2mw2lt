"""Machine-local transcript and account proof."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import machine_harness as harness

UNOBSERVED = "-"
_NOT_ACCOUNT = re.compile(r"[^a-z0-9._-]+")


def harness_roots() -> list[tuple[str, Path]]:
    """(provider, root) pairs. `STEERING_HARNESS_ROOTS` overrides as `provider=path` entries
    separated by ':'; an entry without '=' is a Claude root."""
    env = os.environ.get("STEERING_HARNESS_ROOTS")
    if env:
        out = []
        for e in env.split(":"):
            if not e:
                continue
            prov, _, path = e.partition("=") if "=" in e else (harness.DEFAULT, "=", e)
            out.append((prov, Path(path)))
        return out
    # A second account lives at `.claude-<name>` beside the default (doc 08), and its sessions
    # enrol like any other. `STEERING_HARNESS_ROOTS` is the override for a root outside $HOME.
    home = Path.home()
    out = []
    for h in harness.HARNESSES.values():
        if not (h.config and h.transcript_root):
            continue
        for d in sorted(home.glob(h.config + "*")):
            root = d / h.transcript_root
            if root.is_dir():
                out.append((h.provider, root))
    return out


def account_of(config_dir: Path, provider: str = "claude") -> str:
    """Which account on this machine, named by the config directory the harness is using rather
    than by the address signed in there. The session and the brain derive it from the same path,
    and it carries nothing personal — `agent` is mirrored to the room, and an address is not
    something a homeserver needs.

    The harness's own default directory is `default`; anything else is its own name, reduced to
    a single token because the verb grammar splits on whitespace and a directory name does not
    have to be one."""
    name = Path(config_dir).name
    h = harness.of(provider)
    if h is not None and name == h.config:
        return "default"
    return _NOT_ACCOUNT.sub("-", name.lstrip(".").lower()).strip("-")[:64] or "default"


def account_of_root(root: Path, provider: str = "claude") -> str:
    """The account a harness root belongs to, which is the configuration directory the root
    sits under. How far up that is depends on the harness: a root of `cli/rollout` is two
    components down, and climbing one would make the account `cli` for every session of it —
    a permanent ledger identity, derived wrong."""
    h = harness.of(provider)
    depth = len(Path(h.transcript_root).parts) if h is not None and h.transcript_root else 1
    root = Path(root)
    for _ in range(depth):
        root = root.parent
    return account_of(root, provider)


def provider_session_of(provider: str, rel: Path) -> str | None:
    """The harness's own session id, out of the transcript path this door validated (doc 28 §3).

    No harness exports a common variable for it, but each writes exactly one transcript per
    session at a path that names it, and `identify_transcript` already parses that path per
    provider to decide the file may be observed. This reads the id out of the same parse, so
    the identity is one the door derived from a file it opened rather than a string a caller
    typed.

    None when the path does not name one. Deriving nothing is the safe direction: the
    enrollment still lands, and doc 28 §5's rules simply have no identity to compare — where
    half an id would be an identity that is wrong.
    """
    h = harness.of(provider)
    if h is None or h.shape is None:
        return None
    return h.shape(rel)[1]


def attested_agent(provider: str, transcript: str, workspace: str, psession: str,
                   roots: list[tuple[str, Path]] | None = None) -> str | None:
    """`<harness>/<account>` as this machine reads it from where the session's transcript sits, the
    way the local door names an account, or None. A remote session's own `as` is only its word;
    this is the agent on its machine reading the same disk the session runs on.

    The transcript is the session's own only when its path names the provider session the
    orchestrator has identified this enrolment as: a transcript label is typed by the sender, and
    another session's file under the same workspace would otherwise name the wrong account."""
    h = harness.of(provider)
    if h is None or not psession:
        return None
    _why, path, _ident, root = identify_transcript(
        transcript, workspace, roots if roots is not None else harness_roots(), provider)
    if root is None or path is None:
        return None
    rel = Path(path).relative_to(os.path.realpath(str(root)))
    if provider_session_of(provider, rel) != psession:
        return None
    return f"{h.agent}/{account_of_root(root, provider)}"


def validate_transcript(transcript: str, workspace: str, roots: list[tuple[str, Path]], provider: str = "claude") -> str | None:
    """Why this path may not be observed, or None."""
    return identify_transcript(transcript, workspace, roots, provider)[0]


def identify_transcript(transcript: str, workspace: str, roots: list[tuple[str, Path]],
                        provider: str = "claude") -> tuple[str | None, str | None, list[int] | None, Path | None]:
    if transcript == UNOBSERVED:
        return None, None, None, None
    if not os.path.isabs(transcript):
        return "transcript path must be absolute", None, None, None
    p = os.path.realpath(transcript)
    if not p.endswith(".jsonl"):
        return "transcript must be a .jsonl", None, None, None
    h = harness.of(provider)
    if h is None:
        return f"{provider} is not a harness this door knows", None, None, None
    rel_root = None
    for prov, root in roots:
        if prov != provider:
            continue  # a Codex session cannot enroll a Claude file, whatever the path looks like
        r = os.path.realpath(str(root))
        if p.startswith(r + os.sep):
            rel_root = (Path(r), Path(p).relative_to(r))
            break
    if rel_root is None:
        scanned = ", ".join(str(r) for prov, r in roots if prov == provider) or "none"
        return f"transcript is not under a {provider} harness root (scanned: {scanned})", None, None, None
    root, rel = rel_root
    try:
        fd = _open_anchored(root, rel.parts)
    except OSError:
        return "transcript is not an existing regular file, reached without a symlink, under its harness root", None, None, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "transcript is not an existing regular file", None, None, None
        # The harness says whether the path is one of its transcripts and whether the file
        # belongs to this workspace. A harness that answers neither is refused: the absence of
        # an answer used to be the last `elif`, which is Claude's rule, so a provider this
        # chain did not name was validated as Claude.
        if h.shape is None or not h.shape(rel)[0]:
            return f"not a {provider} transcript path", None, None, None
        if h.workspace is None:
            return f"a {provider} transcript names no workspace, so it cannot be observed here", None, None, None
        why = h.workspace(fd, rel, workspace)
        if why:
            return why, None, None, None
        return None, p, [st.st_dev, st.st_ino], root
    finally:
        os.close(fd)


def _open_anchored(root: Path, parts: tuple[str, ...]) -> int:
    fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, name in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if i < len(parts) - 1 else 0)
            nxt = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except OSError:
        os.close(fd)
        raise
