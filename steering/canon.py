"""A workspace's canon units — its decisions and lessons — and the one validator of their
headers (doc 148 §3).

A unit is a markdown file reached from the index whose front matter names a `kind`. The header
is the only part anything parses, so its grammar is kept to what it needs: one `key: value` per
line, a value a plain or quoted string or a `[a, b]` list of them. That is a subset of YAML, so
a renderer that shows front matter still reads it, and a header outside the subset is refused
with a reason rather than read two ways.

`python3 canon.py [--index <path>] [<root>]` is the guard a workspace runs over its checkout.
"""
from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path

import knowledge_tree

KINDS = ("decision", "lesson")
STATUSES = ("active", "retired")
REQUIRED = ("kind", "id", "scope", "status", "source")
KEYS = (*REQUIRED, "superseded_by")
# Opaque, as a lane's id is: nothing parses it, and it outlives any rename of its file.
ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
EVERY = "*"
TITLE_MAX = 100
# The canon block's own ceiling, below the room a directive's words leave it under `TEXT_MAX`.
# Set from `probes/brief_bytes.py` over 200 merged cards at fe0135a7, whose largest block was
# 2893 bytes: no card measured there is cut by this rather than by the words beside it.
BLOCK_MAX = 3072

_KEY = re.compile(r"([A-Za-z_]+):(?:\s+(.*))?")
# A plain scalar may not open with what YAML would read as something else.
_PLAIN = re.compile(r"[^\s\[\]{}\"'*&!#|>@`%,][^\[\]{}\"',]*")


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'" and raw[0] not in raw[1:-1] \
            and "\\" not in raw:
        return raw[1:-1]
    if _PLAIN.fullmatch(raw) and " #" not in raw and ": " not in raw:
        return raw
    raise ValueError(f"{raw!r} is not a plain or quoted string; quote it")


def _value(raw: str) -> str | list[str]:
    raw = raw.strip()
    if not raw.startswith("["):
        return _scalar(raw)
    if not raw.endswith("]"):
        raise ValueError(f"{raw!r} opens a list it does not close on the same line")
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items, cur, quote = [], "", None
    for c in inner:
        if quote:
            quote = None if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c == ",":
            items.append(cur)
            cur = ""
            continue
        cur += c
    return [_scalar(i) for i in [*items, cur]]


def header(text: str) -> tuple[dict | None, list[str]]:
    """`(None, [])` for a file that is not a unit; the unit's header, or every reason it is
    refused."""
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return None, []
    close = next((i for i, l in enumerate(lines[1:], 1) if l.rstrip() == "---"), None)
    block = lines[1:close] if close else lines[1:]
    if not any(re.match(r"\s*kind\s*:", l) for l in block):
        return None, []  # front matter, but not a unit's
    if close is None:
        return None, ["the header is not closed by a `---` line"]
    out, why = {}, []
    for n, line in enumerate(block, 2):
        if not line.strip():
            continue
        m = _KEY.fullmatch(line.rstrip())
        if not m:
            why.append(f"line {n} is not `key: value`")
            continue
        key = m.group(1)
        if key not in KEYS:
            why.append(f"`{key}` is not a header field; the fields are {', '.join(KEYS)}")
        elif key in out:
            why.append(f"`{key}` appears twice")
        else:
            try:
                out[key] = _value(m.group(2) or "")
            except ValueError as e:
                why.append(f"`{key}`: {e}")
    return (None, why) if why else _fields(out)


def _fields(h: dict) -> tuple[dict | None, list[str]]:
    why = [f"`{k}` is missing" for k in REQUIRED if k not in h]
    kind, uid, status = h.get("kind"), h.get("id"), h.get("status")
    if "kind" in h and kind not in KINDS:
        why.append(f"`kind` is {kind!r}, not one of {', '.join(KINDS)}")
    if "id" in h and not (isinstance(uid, str) and ID.fullmatch(uid)):
        why.append(f"`id` does not match {ID.pattern}")
    if "scope" in h:
        why += _scope(h["scope"])
    if "status" in h and status not in STATUSES:
        why.append(f"`status` is {status!r}, not one of {', '.join(STATUSES)}")
    if "source" in h and not (isinstance(h["source"], list) and h["source"] and all(h["source"])):
        why.append("`source` is not a non-empty list of the facts or issues the unit came from")
    if "superseded_by" in h:
        succ = h["superseded_by"]
        if status != "retired":
            why.append("`superseded_by` is set on a unit that is not retired")
        elif not (isinstance(succ, str) and ID.fullmatch(succ)):
            why.append(f"`superseded_by` does not match {ID.pattern}")
        elif succ == uid:
            why.append("`superseded_by` names the unit itself")
    return (None, why) if why else (h, [])


def _scope(scope) -> list[str]:
    if not isinstance(scope, list) or not scope:
        return [f"`scope` is not a non-empty list of repository paths, or [\"{EVERY}\"]"]
    if EVERY in scope:
        return [] if scope == [EVERY] else [f"`scope` names \"{EVERY}\" beside paths it already covers"]
    why = []
    for p in scope:
        if p.startswith("/") or "\\" in p or ".." in p.split("/") or posixpath.normpath(p) == ".":
            why.append(f"`scope` path {p!r} is not a path inside the repository")
    return why


def validate(texts: dict[str, str]) -> tuple[dict[str, dict], list[str]]:
    """The units among `texts` (path to text) by id, each header with its `path`, and every
    reason the set is refused: a malformed header, two files with one id, or a `superseded_by`
    naming no active unit. A refused set is refused whole."""
    units, why = {}, []
    for path in sorted(texts):
        h, bad = header(texts[path])
        why += [f"{path}: {b}" for b in bad]
        if h is None:
            continue
        if h["id"] in units:
            why.append(f"{path}: id {h['id']!r} is also {units[h['id']]['path']}'s")
            continue
        units[h["id"]] = {**h, "path": path, **({"title": t} if (t := title(texts[path])) else {})}
    for u in units.values():
        succ = u.get("superseded_by")
        if succ is not None and (units.get(succ) or {}).get("status") != "active":
            why.append(f"{u['path']}: `superseded_by` names {succ!r}, which is no active unit")
    return units, why


def texts(repo, index: str) -> dict[str, str]:
    """Every markdown document reached from `index`, by its text: where units are looked for.
    `repo` is anything with `text(path)`, a `knowledge_tree.Repository` or a `Tree`."""
    return {p: t for p in sorted(knowledge_tree.walk(repo, index))
            if p.endswith(".md") and (t := repo.text(p)) is not None}


def _prefix(p: str) -> str:
    """A path or a directory pattern as the prefix it names: `a/`, `a/**` and `a` are one."""
    for tail in ("/**", "/*"):
        p = p.removesuffix(tail)
    return posixpath.normpath(p.rstrip("/")) if p.strip("/") else ""


_CLASS = re.compile(r"^(.+)/\*(\.[A-Za-z0-9]+)$")


def _scoped(p: str) -> tuple[str, str] | None:
    """A `dir/*.ext` entry as `(dir, ext)`: a class of file, not a path (#3944)."""
    m = _CLASS.match(p.rstrip("/"))
    return (posixpath.normpath(m[1]), m[2]) if m else None


def overlap(a: str, b: str) -> bool:
    """Whether one path lies under the other, at a directory boundary: `steering/test/` holds
    `steering/test/x.py`, and a query for the directory reaches a unit scoped to one file in it.

    A `dir/*.ext` entry names a class instead: the files under `dir` whose names end `.ext`,
    and nothing else in that directory — so a contract scoped `desk/*.tsx` is moved by the
    components and copy a person sees, not by the logic, loaders, tests and types that sit
    beside them, while a plain directory entry still holds everything under it (#3944)."""
    sa, sb = _scoped(a), _scoped(b)
    if sa and sb:
        return sa[1] == sb[1] and overlap(sa[0], sb[0])
    if sa or sb:
        (d, ext), plain = (sa or sb), (b if sa else a)
        p = _prefix(plain)
        if not p:
            return False
        if p.endswith(ext):
            return overlap(d, p)
        # Only a directory query reaches a class from outside it: an extensionless entry
        # holds the class as it holds a file scoped inside it; a named file of another
        # class holds nothing of this one.
        return "." not in posixpath.basename(p) and overlap(d, p)
    a, b = _prefix(a), _prefix(b)
    return bool(a and b) and (a == b or a.startswith(b + "/") or b.startswith(a + "/"))


def join(paths: list[str], units: dict[str, dict], read: dict | None = None) -> dict:
    """What a brief for `paths` carries (doc 148 §4): the active decisions and lessons scoped to
    any of them, the active `*` units, and from `knowledge.read`'s answer the contracts and
    track records whose own path, or the code they are the account of, overlaps one."""
    touches = lambda owned: any(overlap(o, p) for o in owned for p in paths)
    active = sorted((u for u in units.values() if u.get("status") == "active"), key=lambda u: u["id"])
    records = [{**d, "track": t["track"]} for t in (read or {}).get("tracks", []) for d in t["docs"]
               if not d.get("artifact") and touches([d["path"], *t["code"]])]
    return {"paths": list(paths),
            "every": [u for u in active if u["scope"] == [EVERY]],
            "units": [u for u in active if u["scope"] != [EVERY] and touches(u["scope"])],
            "contracts": [c for c in (read or {}).get("contracts", []) if touches([c["path"], *c["code"]])],
            "records": sorted(records, key=lambda d: d["path"])}


def title(text: str) -> str | None:
    """A unit's first `# ` heading below its header: the name a brief lists it by."""
    lines = text.split("\n")
    close = next((i for i, l in enumerate(lines[1:], 1) if l.rstrip() == "---"), 0)
    head = next((l[2:].strip() for l in lines[close + 1:] if l.startswith("# ")), "")
    return " ".join(head.replace("```", "'''").split())[:TITLE_MAX] or None


def rows(joined: dict) -> list[tuple[str, str]]:
    """Each unit a brief for `joined` names, as its key and its line, in doc 148 §6's order: the
    card's owed obligations, the card's record, the `*` units, the units in scope, the contracts.
    A decision or lesson is keyed by its id; a record or contract, which has none, by its path;
    an owed stage by its own name, the units it owes carried on its line."""
    unit = lambda u: (u["id"], f"{u['kind']} {u['id']}" + (f": {u['title']}" if u.get("title") else ""))
    stage = lambda o: (f"owes-{o['stage']}", " ".join([f"owes-{o['stage']}", *o.get("units", [])]))
    return ([stage(o) for o in joined.get("owes") or []]
            + [(r["path"], f"record {r['path']}") for r in joined.get("records", [])]
            + [unit(u) for u in joined.get("every", [])] + [unit(u) for u in joined.get("units", [])]
            + [(c["path"], f"contract {c['path']}") for c in joined.get("contracts", [])])


def block(joined: dict, link: str, room: int) -> tuple[str, list[str]]:
    """The fenced index a brief carries (doc 148 §6), cut from the end to fit `room` bytes, and
    the keys of the rows it kept. Nothing when not one row fits beside its frame and link."""
    shown, keys, pending = [], [], rows(joined)
    frame = lambda cut: (["```canon", *shown, f"full brief{f' (+{cut} not shown)' if cut else ''}: {link}", "```"])
    size = lambda cut: len("\n".join(frame(cut)).encode())
    for n, (key, line) in enumerate(pending):
        shown.append(line)
        if size(len(pending) - n - 1) > room:
            shown.pop()
            break
        keys.append(key)
    if not keys:
        return "", []
    # The frame the last kept row's own check measured: a pop never renders one it did not.
    return "\n".join(frame(len(pending) - len(keys))), keys
def names_card(units: dict[str, dict] | None, card_id: str, issues) -> str | None:
    """The active unit whose `source` names the card's id or one of `issues` (doc 148 §5), as
    the unit's id, or None. `#2994` anywhere in a source entry names the issue; the card id is
    the entry itself, an opaque string."""
    for u in sorted((units or {}).values(), key=lambda u: u["id"]):
        if u.get("status") != "active":
            continue
        for s in u.get("source") or []:
            if not isinstance(s, str):
                continue
            if s.strip() == card_id or set(knowledge_tree.refs(s)) & set(issues):
                return u["id"]
    return None


def owes(*, major: bool, concluded: bool, design_passed: bool, card_id: str, issues,
         diff_paths: list[str] | None, units: dict[str, dict] | None,
         read: dict | None) -> dict:
    """The three knowledge stages of one card (doc 148 §5), beside doc 80's five.

    Each stage is `{"state": …}` with the state the obligation's own name (`owes-design`,
    `owes-record`, `owes-harvest`) while it stands, `met` once satisfied, `none` where the card
    owes nothing at all, and `unknown` where the data to judge it has not been read — which is
    not `met`, so a fold still warming never passes a card it has not seen. `owes-record` names
    the documents owed and the diff's paths that owe them, and a met design the unit that
    satisfied it, so a row can say what to write without re-deriving the join. A harvest has
    no `met` here: what records that a concluded card was swept is slice 4's, so a concluded
    card owes its harvest until then.

    `units` is the synced canon's fold and `read` the `knowledge.read` answer, both as the
    daemon holds them; `diff_paths` is the card's branch diff against main, or None when no
    diff is known (no pull request, or one whose files the sweep did not read whole).
    """
    if not major:
        design = {"state": "none"}
    elif units is None:
        design = {"state": "unknown"}
    else:
        named = names_card(units, card_id, issues)
        design = ({"state": "met", "unit": named} if design_passed and named
                  else {"state": "owes-design"})
    if diff_paths is None or read is None:
        record = {"state": "unknown"}
    else:
        joined = join(diff_paths, units or {}, read)
        owed = sorted({u["path"] for u in joined["contracts"] + joined["records"]}
                      - set(diff_paths))
        track_code = {t["track"]: t["code"] for t in read.get("tracks", [])}
        code = {**{r["path"]: track_code.get(r["track"], []) for r in joined["records"]},
                **{c["path"]: c["code"] for c in joined["contracts"]}}
        moved = sorted({p for p in diff_paths for u in owed for o in code[u] if overlap(o, p)})
        record = ({"state": "owes-record", "units": owed, "moved": moved} if owed
                  else {"state": "met"})
    return {"design": design, "record": record,
            "harvest": {"state": "none" if not concluded else "owes-harvest"}}


class Tree:
    """A checkout's working tree, read the way `knowledge_tree.Repository` reads a commit: regular
    files inside the root only, so the guard sees what is about to be committed."""
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def text(self, path: str) -> str | None:
        key = posixpath.normpath(path).lstrip("/")
        if key.startswith("..") or key == ".":
            return None
        p = self.root / key
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(self.root):
            return None
        try:
            return p.read_text()
        except (OSError, UnicodeDecodeError):
            return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check a workspace's canon units (doc 148 §3).")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--index", default="documentation/README.md",
                    help="the index the tracks document names")
    a = ap.parse_args(argv)
    tree = Tree(Path(a.root))
    if tree.text(a.index) is None:
        print(f"{a.index} is not a file under {tree.root}", file=sys.stderr)
        return 1
    units, why = validate(texts(tree, a.index))
    for w in why:
        print(w)
    if not why:
        print(f"{len(units)} units valid")
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
