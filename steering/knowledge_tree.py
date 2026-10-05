"""How a canon is read: the files an index reaches, and the issues its records cite (doc 148)."""
from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path


_LINK = re.compile(r"\]\(\s*<?([^)>\s#]+\.md)(?:#[^)>\s]*)?(?:\s+\"[^\"]*\")?\s*>?\)")


class Repository:
    """The repository facts knowledge needs, read with git at one head (doc 96 §2).

    `path` is anything `git -C` accepts: the orchestrator's bare mirror, or a checkout's root
    (a linked worktree's `.git` is a file, so the root and not `.git`).
    """
    def __init__(self, path: Path, name: str | None = None):
        self.path, self.name = Path(path), name or str(path)
        head = self._git("rev-parse", "-q", "--verify", "HEAD^{commit}")
        if head.returncode != 0:
            raise RuntimeError(f"{self.name} has no HEAD to read")
        self.head = head.stdout.strip()
        self._texts: dict[str, str | None] = {}

    def _git(self, *args: str, text: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.path), *args], capture_output=True,
                              text=text, input=input)

    def _entries(self, path: str, recurse: bool) -> list[tuple[str, str, str]]:
        """(mode, type, path) for `path` at the head, a directory's contents when `recurse`."""
        key = posixpath.normpath(path).lstrip("/")
        if key.startswith("..") or key == ".":
            return []
        got = self._git("ls-tree", *(["-r"] if recurse else []), "--full-tree", "-z", self.head, "--", key)
        out = []
        for row in got.stdout.split("\0") if got.returncode == 0 else []:
            if row:
                meta, name = row.split("\t", 1)
                mode, kind, _sha = meta.split(" ")
                out.append((mode, kind, name))
        return out

    def text(self, path: str) -> str | None:
        """A regular file's text; None for a directory, a symlink or anything not UTF-8. Kept per
        path, since the head does not move and a walk asks for each linked document many times."""
        if path not in self._texts:
            self._texts[path] = self._text(path)
        return self._texts[path]

    def _text(self, path: str) -> str | None:
        entries = self._entries(path, recurse=False)
        if len(entries) != 1 or entries[0][0] not in _REGULAR or entries[0][1] != "blob":
            return None
        got = self._git("cat-file", "blob", f"{self.head}:{entries[0][2]}", text=False)
        try:
            return got.stdout.decode() if got.returncode == 0 else None
        except UnicodeDecodeError:
            return None

    def files(self, path: str) -> list[str]:
        return sorted(name for mode, kind, name in self._entries(path, recurse=True)
                      if kind == "blob" and mode in _REGULAR)

    def last_commit(self, path: str) -> dict:
        sha, _, utc = self._git("log", "-1", "--format=%H%x00%cI", self.head, "--",
                                path).stdout.strip().partition("\0")
        return {"at": sha or None, "utc": utc or None}

    def _document_history(self) -> dict:
        got = self._git("log", "--format=%H%x00%P%x00%cI", self.head)
        if got.returncode != 0:
            raise RuntimeError(got.stderr.strip())
        commits, edges, queries = {}, [], []
        for row in got.stdout.splitlines():
            sha, parents, utc = row.split("\0")
            parents = parents.split()
            commits[sha] = {"parents": parents, "utc": utc, "changes": {}}
            for parent in parents or [None]:
                edges.append((sha, parent))
                queries.append(sha + (" " + parent if parent else ""))
        # Explicit parent overrides preserve edge order; --always keeps TREESAME frames.
        got = self._git("diff-tree", "--stdin", "--always", "--root", "-r", "--raw", "-z",
                        "--no-renames", "--", "*.md", text=False,
                        input=("\n".join(queries) + "\n").encode())
        if got.returncode != 0:
            raise RuntimeError(got.stderr.decode().strip())
        edge_iter = iter(edges)
        rows = iter(got.stdout.split(b"\0")[:-1])
        for row in rows:
            if row.startswith(b":"):
                changed.add(next(rows).decode(errors="surrogateescape"))
            else:
                sha, parent = next(edge_iter)
                assert row.decode() == sha
                changed = commits[sha]["changes"][parent] = set()
        return {"commits": commits, "last": {}}

    def _document_commit(self, path: str) -> dict:
        history = held(self, ("document-history",), lambda repo: repo._document_history())
        if path not in history["last"]:
            sha = self.head
            while True:
                commit = history["commits"][sha]
                same = next((p for p in commit["parents"] if path not in commit["changes"][p]), None)
                if same is not None:
                    sha = same
                    continue
                exists = bool(commit["parents"]) or path in commit["changes"][None]
                history["last"][path] = {"at": sha if exists else None,
                                         "utc": commit["utc"] if exists else None}
                break
        return history["last"][path]

    def behind(self, at: str | None, paths: list[str]) -> int:
        if not at or not paths:
            return 0
        immutable = len(at) == len(self.head) and re.fullmatch(r"[0-9a-fA-F]+", at) is not None
        counts = held(self, ("behind",), lambda repo: {}) if immutable else {}
        key = (at, tuple(paths))
        if key in counts:
            return counts[key]
        got = self._git("rev-list", "--count", f"{at}..{self.head}", "--", *paths)
        if got.returncode != 0:
            return 0
        count = int(got.stdout.strip() or 0)
        if immutable:
            counts[key] = count
        return count

    def diff(self, at: str, paths: list[str]) -> dict:
        """From the merge base of `at` and the head, as GitHub's three-dot compare is. A binary
        change carries no patch to judge, so it marks the diff truncated."""
        got = self._git("diff", f"{at}...{self.head}", "--", *paths)
        if got.returncode != 0:
            raise RuntimeError(f"git diff {at[:12]}...{self.head[:12]}: {got.stderr.strip()[-160:]}")
        binary = any(l.startswith("Binary files ") for l in got.stdout.splitlines())
        return {"text": got.stdout, "truncated": binary}


_REGULAR = {"100644", "100755"}


def inside(repo: Repository, path: str) -> bool:
    """A regular file of the repository, not a link out of it: a `documentation/x.md` link to a
    host file would otherwise be listed, accepted and posted to a model provider.
    """
    return repo.text(path) is not None


def links(text: str, frm: str) -> set[str]:
    """Repo-relative markdown link targets, resolved against the linking document's directory.

    `../` is ordinary in these indexes and must resolve, or a cross-subject link reads as a
    different path from the file it names.
    """
    base = posixpath.dirname(frm)
    out = set()
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        out.add(posixpath.normpath(posixpath.join(base, target.lstrip("/"))))
    return out


def walk(repo: Repository, root: str) -> set[str]:
    """Every document a reader reaches from `root` by following links, cycles included."""
    if repo.text(root) is None:
        return set()
    seen, queue = {root}, [root]
    while queue:
        cur = queue.pop()
        text = repo.text(cur)
        if text is None:
            continue
        for nxt in links(text, cur):
            if nxt in seen or not inside(repo, nxt):
                continue  # already walked, outside the checkout, or a link out of it
            seen.add(nxt)
            queue.append(nxt)
    return seen


_HELD: dict[tuple, tuple[str, dict]] = {}


def held(repo: Repository, key: tuple, compute) -> dict:
    """`compute(repo)`, or the answer it gave at this HEAD: what is read is the repository at one
    commit, which does not change."""
    at = _HELD.get((repo.name, key))
    if at is not None and at[0] == repo.head:
        return at[1]
    out = compute(repo)
    _HELD[(repo.name, key)] = (repo.head, out)
    return out


# `#N`, not part of a word, a path or a longer `##` heading.
_REF = re.compile(r"(?<![\w/#])#(\d+)\b")


def refs(text: str) -> list[int]:
    return sorted({int(n) for n in _REF.findall(text or "")})
