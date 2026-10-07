"""What a harness's input box looks like on a tmux pane's screen (doc 118 §3.3).

A tmux wake types into a pane without seeing it, so before a key is sent the screen is read and
matched against the harness's measured composer. `read()` answers `None` for anything it does not
recognise — a dialog, a picker, a harness with no measured profile — and the caller refuses: an
Enter sent into a permission prompt approves it.

Only Claude Code is measured (2.1.281, 2026-09-24). A profile is added by measuring a harness the
same way, never by guessing its layout.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PROMPT = "❯"
RULE = "─"
# The rule lines Claude Code draws above and below the composer run the pane's width; a short run
# of the same glyph is text, not a frame.
RULE_MIN = 20
# Lines Claude Code draws below the lower rule: its mode line and the account's status line. A
# suggestion menu or dialog below the rule is longer than this, and is not an idle composer.
STATUS_MAX = 6


@dataclass(frozen=True)
class Composer:
    lines: tuple[str, ...]      # the composer's own lines, the prompt glyph's included
    typed: tuple[str, ...] = ()  # the same lines, keeping only what was not drawn dim
    runs: tuple[tuple[bool, int], ...] = ()  # after the prompt: (drawn dim, characters) per run
    # Text after the prompt that is Claude Code's suggestion, known by where the cursor is (see
    # `_ghost`), because the pane draws no attributes to tell it by.
    ghost: bool = False

    @property
    def shape(self) -> str:
        """What the composer holds, without a character of it: the owner types here, and a draft
        can hold anything (the brain's ruling of 2026-09-25), so a refusal carries only this."""
        dim = [n for d, n in self.runs if d]
        undim = [n for d, n in self.runs if not d]
        # A suggestion is drawn wholly dim: one typed character before a dim completion is a draft.
        like = bool(dim) and not undim
        return (f"{sum(dim) + sum(undim)} characters: {sum(undim)} undimmed in {len(undim)} run(s), "
                f"{sum(dim)} dim in {len(dim)} run(s)" + ("; shaped like a suggestion" if like else "")
                + ("; cursor at the start of an undecorated pane" if self.ghost else ""))

    @property
    def empty(self) -> bool:
        if self.ghost:
            return True
        # A line drawn wholly dim holds nothing typed, so it does not count as a line.
        seen = tuple(t for t in (self.typed or self.lines) if t.strip()) or self.lines
        return len(seen) == 1 and seen[0].strip() == PROMPT


_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _plain(line: str) -> str:
    return _ESC.sub("", line)


def _dim_after(params: str, dim: bool) -> bool:
    """The dim state after one SGR sequence. Positional: an extended colour (`38`/`48`/`58`)
    consumes its own arguments (`5;n` or `2;r;g;b`), so a `2` inside one is not dim."""
    codes = (params or "0").split(";")
    i = 0
    while i < len(codes):
        c = codes[i]
        if c in ("38", "48", "58"):
            i += 2 if codes[i + 1:i + 2] == ["5"] else 4 if codes[i + 1:i + 2] == ["2"] else 0
        elif c == "2":
            dim = True
        elif c in ("0", "22", ""):
            dim = False
        i += 1
    return dim


def _segments(lines: list[str]) -> list[list[tuple[str, bool]]]:
    """Each line as its runs of text, each with whether it is drawn dim (SGR 2). What a person
    typed is what is not dim: Claude Code draws its prompt suggestion dim in an idle composer, and
    a wake must not read it as a draft (measured on a live pane, 2026-09-24). The state runs
    across lines, as the terminal's does."""
    out, dim = [], False
    for line in lines:
        segs, at = [], 0
        for m in _SGR.finditer(line):
            segs.append((_ESC.sub("", line[at:m.start()]), dim))
            dim = _dim_after(m.group(1), dim)
            at = m.end()
        segs.append((_ESC.sub("", line[at:]), dim))
        out.append([(t, d) for t, d in segs if t])
    return out


def _runs(segs: list[list[tuple[str, bool]]]) -> tuple[tuple[bool, int], ...]:
    """The composer's text after the prompt glyph as (dim, characters) runs, spaces not counted."""
    flat = [(t, d) for line in segs for t, d in line]
    if flat and flat[0][0].lstrip().startswith(PROMPT):
        t, d = flat[0]
        flat[0] = (t.lstrip()[len(PROMPT):], d)
    out: list[list] = []
    for t, d in flat:
        n = len("".join(t.split()))
        if not n:
            continue
        if out and out[-1][0] == d:
            out[-1][1] += n
        else:
            out.append([d, n])
    return tuple((d, n) for d, n in out)


def _rule(line: str) -> bool:
    """A full-width rule, which may carry a label (`─── eval-track ─` when the session is named)."""
    s = line.strip()
    return (len(s) >= RULE_MIN and s[0] == RULE and s[-1] == RULE
            and s.count(RULE) >= RULE_MIN)


def _ghost(raw: list[str], top: int, cursor: tuple[int, int] | None) -> bool:
    """That the text after the prompt is a suggestion, in a pane that draws no attributes.

    A session launched with no `TERM` (`env -i` in `launchers.py` keeps none the agent lacks) draws
    no colour and no dim, so its suggestion reads as typed text and every wake after a turn was
    refused as a draft (#3020). What still tells them apart is the cursor: Claude Code parks it at
    the start of the input under a suggestion (measured on idle launched panes, 2026-09-29: column
    2, on the prompt's row), where a person's draft has it after the text. Attributes win whenever
    they are drawn: in a pane with any, only dim is a suggestion. A draft with the cursor moved back
    to the very start of an undecorated pane is the case this reads wrongly."""
    if cursor is None or any(_SGR.search(line) for line in raw[top:]):
        return False
    return cursor == (len(PROMPT) + 1, top + 1)


def _claude(screen: str, cursor: tuple[int, int] | None = None) -> Composer | None:
    raw = screen.rstrip("\n").split("\n")
    lines = [_plain(line) for line in raw]
    while lines and not lines[-1].strip():
        lines.pop()
        raw.pop()
    rules = [i for i, line in enumerate(lines) if _rule(line)]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    body = lines[top + 1:bottom]
    segs = _segments(raw[:bottom])[top + 1:]
    typed = ["".join(t for t, d in line if not d) for line in segs]
    below = lines[bottom + 1:]
    if not body or not 1 <= len(below) <= STATUS_MAX:
        return None
    first = body[0].rstrip()
    if first != PROMPT and not (first.startswith(PROMPT) and first[len(PROMPT):][:1].isspace()):
        return None
    if _OPTION.match(first):
        return None     # a numbered choice (a permission prompt), not a draft; no key goes to it
    while len(body) > 1 and not body[-1].strip():
        body, typed, segs = body[:-1], typed[:-1], segs[:-1]
    # Empty is judged on what was typed; the lines kept are what is on screen, for the record.
    return Composer(tuple(line.rstrip() for line in body),
                    typed=tuple(t.strip() for t in typed), runs=_runs(segs),
                    ghost=len(body) == 1 and _ghost(raw, top, cursor))


_OPTION = re.compile(PROMPT + r" \d+\. ")
PROFILES = {"claude": _claude}

LEVELS = ("low", "medium", "high", "xhigh", "max")
_SLIDER_FOOT = "s for this session only"
_TRUST_ASK = "Quick safety check: Is this a project you created or one you trust?"
_TRUST_NO, _TRUST_YES = "No, exit", "Yes, I trust this folder"
_TRUST_FOOT = "Enter to confirm · Esc to cancel"


def stops(screen: str) -> list[str] | None:
    """The stops of Claude Code's `/effort` slider, left to right, or None when the screen is not
    that slider as measured (doc 120 §2.4): an `Effort` title, one `▲` on a rule, a label row
    beneath it opening `low medium high xhigh max`, and a footer naming the session-only key. A
    stop past `max` is allowed: 2.1.282 added `ultracode` there, and a release that adds another
    moves nothing this reads."""
    found = _slider(screen)
    return found[2] if found else None


def slider(screen: str) -> str | None:
    """The stop under the `▲` of the slider `stops` reads, or None."""
    found = _slider(screen)
    if found is None:
        return None
    col, labels, names = found
    # Each label owns the columns up to the midpoint with its neighbours.
    centres, at = [], 0
    for name in names:
        start = labels.index(name, at)
        centres.append(start + len(name) / 2)
        at = start + len(name)
    return min(zip(centres, names), key=lambda c: abs(c[0] - col))[1]


def _slider(screen: str) -> tuple[int, str, list[str]] | None:
    lines = [_plain(line).rstrip() for line in screen.rstrip("\n").split("\n")]
    if not any(line.strip() == "Effort" for line in lines):
        return None
    if not any(_SLIDER_FOOT in line for line in lines):
        return None
    arrows = [i for i, line in enumerate(lines) if "▲" in line]
    if len(arrows) != 1 or lines[arrows[0]].count("▲") != 1 or arrows[0] + 1 >= len(lines):
        return None
    arrow, labels = lines[arrows[0]], lines[arrows[0] + 1]
    names = labels.split()
    if names[:len(LEVELS)] != list(LEVELS):
        return None
    return arrow.index("▲"), labels, names


_INDICATOR = re.compile(r"^\s*\S\s+(\w+)\s+·\s+/effort\s*$")


def indicator(screen: str) -> str | None:
    """The level Claude Code's effort indicator names (`◐ medium · /effort`, drawn right-aligned
    on the line just above the composer's top rule after a change), or None. Only that line: the
    same words in the transcript, or in the slider's own `▔` header, are not the indicator."""
    lines = [_plain(line).rstrip() for line in screen.rstrip("\n").split("\n")]
    rules = [i for i, line in enumerate(lines) if _rule(line)]
    if len(rules) < 2 or rules[-2] == 0:
        return None
    m = _INDICATOR.match(lines[rules[-2] - 1])
    return m.group(1) if m else None


def last_echo(screen: str) -> tuple[str, str] | None:
    """The newest command in the history above Claude Code's composer and the first line of its
    `⎿` answer, as `(command, answer)`, or None. Only the newest: an older `/effort` echo naming
    the same level can still be on screen, and reading it would confirm a change that never
    happened."""
    lines = [_plain(line).rstrip() for line in screen.rstrip("\n").split("\n")]
    rules = [i for i, line in enumerate(lines) if _rule(line)]
    if len(rules) < 2:
        return None
    above = [line for line in lines[:rules[-2]] if line.strip()]
    answers = [i for i, line in enumerate(above) if line.lstrip().startswith("⎿")]
    if not answers or answers[-1] == 0:
        return None
    i = answers[-1]
    command = above[i - 1].strip()
    if not command.startswith(PROMPT):
        return None
    return command[len(PROMPT):].strip(), above[i].lstrip()[1:].strip()


def _two_choices(screen: str, question: str, answers: tuple[str, str], *,
                 numbered: bool = False, footer: str = "") -> str | None:
    """The cursor's answer below the newest question, only with two distinct choices and one
    cursor. Numbered dialogs name every offered row; an unnumbered dialog's choices are the
    last block before its measured footer, apart from the explanatory text above them."""
    lines = [_plain(line).replace("\xa0", " ").strip() for line in screen.rstrip("\n").split("\n")]
    asks = [i for i, line in enumerate(lines) if question in line]
    if not asks:
        return None
    # A previous dialog's answers, or a numbered list in the transcript, are not this one's.
    body = lines[asks[-1] + 1:]
    if footer:
        while body and not body[-1]:
            body.pop()
        # The footer must end the screen: an old dialog must not authorize a newer surface.
        if body.count(footer) != 1 or body.pop() != footer:
            return None
        while body and not body[-1]:
            body.pop()
        start = max((i for i, line in enumerate(body) if not line), default=-1)
        if start >= 0 and any(line.startswith(PROMPT) or line in answers for line in body[:start]):
            return None
        body = body[start + 1:]
    offered: list[tuple[str, bool]] = []
    for line in body:
        if numbered:
            m = _ROW.match(line)
            if m is not None:
                offered.append((m.group("rest").strip(), m.group("mark") == PROMPT))
            elif line:
                return None
        elif line:
            marked = line.startswith(PROMPT)
            answer = line.removeprefix(PROMPT).strip()
            offered.append((answer, marked))
    # Keep rows, not a dict: repeated labels are ambiguity, not an update of the cursor.
    if len(offered) != 2 or {answer for answer, _ in offered} != set(answers):
        return None
    marked = [answer for answer, cursor in offered if cursor]
    if len(marked) != 1:
        return None
    return marked[0]


def trust_prompt(screen: str) -> str | None:
    """`"no"` or `"yes"`, the answer the cursor is on in Claude Code's folder-trust prompt, or None
    when the screen is not that prompt as measured (doc 120 §5): its question, its two answers and
    nothing else offered."""
    answer = _two_choices(screen, _TRUST_ASK, (_TRUST_NO, _TRUST_YES), footer=_TRUST_FOOT)
    return {_TRUST_NO: "no", _TRUST_YES: "yes"}.get(answer)


@dataclass(frozen=True)
class Row:
    number: int
    name: str
    current: bool
    cursor: bool


_PICKER_TITLE, _PICKER_FOOT = "Select model", "s to use this session only"
_ROW = re.compile(r"^\s*(?P<mark>[❯↓↑])?\s*(?P<n>\d+)\.\s+(?P<rest>\S.*)$")


def picker(screen: str) -> list[Row] | None:
    """The rows of Claude Code's `/model` picker as drawn, or None when the screen is not that
    picker as measured (doc 134 §2.5): its title, numbered rows below it with one `❯`, and a
    footer naming the session-only key. A row's name runs to its `✔` or to the gap before its
    description. Rows scrolled out of view are not on the screen and not returned."""
    lines = [_plain(line).replace("\xa0", " ").rstrip() for line in screen.rstrip("\n").split("\n")]
    titles = [i for i, line in enumerate(lines) if line.strip() == _PICKER_TITLE]
    if len(titles) != 1 or not any(_PICKER_FOOT in line for line in lines[titles[0]:]):
        return None
    rows = []
    for line in lines[titles[0] + 1:]:
        m = _ROW.match(line)
        if m is None:
            continue
        name = re.split(r"\s{2,}|\s*✔", m.group("rest"), maxsplit=1)[0].strip()
        after = m.group("rest")[len(name):]
        rows.append(Row(int(m.group("n")), name, after.lstrip().startswith("✔"), m.group("mark") == PROMPT))
    if not rows or sum(r.cursor for r in rows) != 1:
        return None
    return rows


def _unwrapped(screen: str) -> str:
    """`screen` with each run of wrapped prose joined onto one line: consecutive non-blank lines
    none of which is a numbered row. A pane narrower than a dialog's sentence breaks it wherever
    the words fall, which moves with the pane's width and the names in it."""
    out: list[str] = []
    prose = False
    for line in screen.rstrip("\n").split("\n"):
        text = _plain(line).replace("\xa0", " ").strip()
        row = _ROW.match(text) is not None
        if text and not row and prose:
            out[-1] += " " + text
        else:
            out.append(text)
        prose = bool(text) and not row
    return "\n".join(out)


def switch_confirm(screen: str, target: str) -> str | None:
    """`yes` or `no`, the answer the cursor is on in Claude Code's confirmation for a switch to
    `target` — an effort level or a picker row's model name — or None when the screen is not it.
    The slider's and the picker's `s` meet it on a conversation whose cache the switch drops
    (#3100): "Change effort level?" or "Switch model?", then "This conversation is cached for the
    current …. Switching to <target> means the full history gets re-read on your next message.",
    then exactly the two numbered answers `Yes, switch to <target>` and `No, go back`, one holding
    the cursor. Numbered rows above the sentence are the transcript's, not the dialog's."""
    yes, no = f"Yes, switch to {target}", "No, go back"
    answer = _two_choices(_unwrapped(screen), f"{target} means the full history gets re-read on your next message",
                          (yes, no), numbered=True)
    return {yes: "yes", no: "no"}.get(answer)


def model_key(name: str) -> str:
    """A picker row's name as `control: … model` writes it: `Opus 5.5` is `opus-5.5`."""
    return "-".join(name.lower().split())


def model_id(name: str) -> str:
    """The model id a turn records for a picker row's name, before any release date (doc 134 §6):
    `Opus 5.5` is `claude-opus-5-5`, and `Haiku 4.5` is recorded `claude-haiku-4-5-20251001`."""
    return "claude-" + "-".join(name.lower().replace(".", " ").split())


def is_model(name: str, recorded: str | None) -> bool:
    """Whether a turn that recorded `recorded` ran the picker row `name`: its id exactly, or with
    a release date after it. Not a prefix: `claude-opus-5-5` starts with Opus 5's id."""
    return isinstance(recorded, str) and re.fullmatch(re.escape(model_id(name)) + r"(?:-\d{8})?", recorded) is not None


_COMPACT_TYPED = re.compile(r"^/compact(?:\s{2,}<[^>]*>)?\s*$")


def compact_ready(screen: str) -> bool:
    """The composer holds `/compact` and at most the hint Claude Code draws after its trailing
    space (doc 134 §2.4), so Enter compacts and runs nothing else."""
    c = read("claude", screen)
    if c is None or len(c.typed) != 1:
        return False
    return bool(_COMPACT_TYPED.match(c.typed[0].removeprefix(PROMPT).replace("\xa0", " ").strip()))


def compacted(screen: str) -> tuple[str, str] | None:
    """How the newest `/compact` above the composer was answered: `("ok", "")` for `Compacted`,
    `("error", <what followed the colon>)` for an error, or None while it has no answer yet. Only
    the newest prompt line is read: an older `/compact` answered `Compacted` can still be on
    screen above one still running."""
    lines = [_plain(line).replace("\xa0", " ").rstrip() for line in screen.rstrip("\n").split("\n")]
    rules = [i for i, line in enumerate(lines) if _rule(line)]
    if len(rules) < 2:
        return None
    above = lines[:rules[-2]]
    prompts = [i for i, line in enumerate(above) if line.startswith(PROMPT)]
    if not prompts or above[prompts[-1]][len(PROMPT):].strip() != "/compact":
        return None
    for line in above[prompts[-1] + 1:]:
        answer = line.lstrip()
        if answer.startswith("⎿"):
            answer = answer[1:].strip()
            if answer.startswith("Compacted"):
                return "ok", ""
            if answer.startswith("Error"):
                return "error", answer.split(":", 1)[-1].strip()[:300]
    return None


def measured(provider: str) -> bool:
    return provider in PROFILES


def read(provider: str, screen: str, cursor: tuple[int, int] | None = None) -> Composer | None:
    """The composer on `screen`, or None when this is not one `provider`'s profile recognises.
    `cursor` is the pane's (column, row), for a harness whose composer is told by it."""
    profile = PROFILES.get(provider)
    return profile(screen, cursor) if profile else None
