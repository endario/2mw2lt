#!/usr/bin/env python3
"""`goal.py --lease [--provider <harness> --provider-session <id>] prepare|submit …`: the seat's
fenced goal writes (#7004). `prepare` reads the fences once, shows each beside the decision it
qualifies, and writes the exact body and key to a file; `submit` sends that file and nothing else.
A stale refusal shows what moved since the read, and never refreshes a fence."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
import json  # noqa: E402
import re  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import door  # noqa: E402
from verb_help import help_requested, script_help  # noqa: E402

USAGE = """usage: goal.py --lease prepare plan <goal> --policy <unit> --decision <file>
       goal.py --lease prepare judge <goal> <criterion>|goal <verdict> [--evidence card:<id>|pull:<owner>/<name>#<n>]... because <why>
       goal.py --lease prepare import <goal> --source <source> --revision <revision> --rows <file>
       goal.py --lease submit <prepared file>

       A plan's --decision file holds its purpose, selections, associations and optional cutoff, as the
       plan route names them; an import's --rows file holds its rows. prepare adds only the fences."""

# A refusal that says a fence moved: submit shows what moved and stops (#7004).
STALE = frozenset({"goal-revision-stale", "plan-revision-stale", "plan-policy-stale", "plan-inputs-stale",
                   "goal-judgment-stale"})
PULL = re.compile(r"^pull:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")


def main(argv: list[str]) -> int:
    if help_requested("goal", argv, bare=False):
        print(script_help("goal"))
        return 0
    if argv[:1] != ["--lease"]:
        print(USAGE, file=sys.stderr)
        return 2
    import connect
    import lease as lease_mod
    parsed = connect.speaker_flags(argv[1:])
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    flags, rest = parsed
    try:
        held = lease_mod.held(flags)
    except ValueError as why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    if held is None:
        print(lease_mod.NONE, file=sys.stderr)
        return 1
    import session_routes
    try:
        if rest[:1] == ["prepare"] and len(rest) >= 3:
            return prepare(held, rest[1], rest[2], rest[3:])
        if rest[:1] == ["submit"] and len(rest) == 2:
            return submit(held, Path(rest[1]))
    except ValueError as why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    except session_routes.NotSeated as why:
        print(f"refused: this session does not hold the seat ({why})")
        return 1
    except (session_routes.Refused, session_routes.Unsent) as why:
        print(f"refused: the goal could not be read ({why})")
        return 1
    print(USAGE, file=sys.stderr)
    return 2


def _options(rest: list[str], names: tuple[str, ...], repeated: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    """The named options of rest, each taking one value, and the words left over in order."""
    found: dict = {name: [] for name in repeated}
    left = []
    words = iter(rest)
    for word in words:
        if word in names or word in repeated:
            value = next(words, None)
            if value is None:
                raise ValueError(f"{word} takes a value")
            if word in repeated:
                found[word].append(value)
            elif word in found:
                raise ValueError(f"{word} is given twice")
            else:
                found[word] = value
        else:
            left.append(word)
    return found, left


def _json_file(path: str, what: str):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as why:
        raise ValueError(f"{what} {path} is not a readable JSON file ({why})") from None


def _evidence(cited: list[str]) -> list[dict]:
    out = []
    for item in cited:
        if item.startswith("card:") and re.fullmatch(r"[0-9a-f]{24}", item[5:]):
            out.append({"card": item[5:]})
        elif m := PULL.match(item):
            out.append({"pull": {"repo": m[1], "n": int(m[2])}})
        else:
            raise ValueError(f"evidence {item!r} is neither card:<24-hex id> nor pull:<owner>/<name>#<n>")
    return out


def _read(token: str, goal: str) -> dict:
    import session_routes
    return session_routes.get(f"/goals/{goal}/reading", token)


def _inputs(reading: dict) -> dict:
    """What the plan's input digest covers, as the reading shows it: each card's state, conclusion and
    waits, and the goal's judgments. Shown, never compared for relevance: the digest decides that."""
    cards = {}
    for card in reading.get("cards") or []:
        waits = sorted(str(link.get("on")) for link in card.get("links") or []
                       if link.get("kind") == "requires" and not link.get("ended"))
        cards[card["id"]] = {"name": card.get("name", ""), "state": card.get("state"),
                             "concluded": bool(card.get("conclusion")), "waits": waits}
    return {"cards": cards, "judgments": sorted(j["id"] for j in reading["goal"].get("judgments") or [])}


def _fences(reading: dict) -> dict:
    goal = reading["goal"]
    return {"goal_revision": goal["current"]["revision"],
            "plan_revision": max((p["revision"] for p in goal.get("plans") or []), default=0),
            "plan_inputs": goal.get("plan_inputs")}


def prepare(held: dict, kind: str, goal: str, rest: list[str]) -> int:
    import session_routes
    session_routes.seat_generation(held["session"], held["token"])
    reading = _read(held["token"], goal)
    fences = _fences(reading)
    shown: list[str] = [f"goal {goal}: {reading['goal']['current'].get('title', '')}",
                        f"  goal revision {fences['goal_revision']}"]
    if kind == "plan":
        opts, left = _options(rest, ("--policy", "--decision"))
        if left or "--policy" not in opts or "--decision" not in opts:
            raise ValueError("prepare plan takes <goal> --policy <unit> --decision <file>")
        decision = _json_file(opts["--decision"], "the decision")
        if not isinstance(decision, dict) or set(decision) - {"purpose", "cutoff", "selections", "associations"}:
            raise ValueError("the decision holds only purpose, cutoff, selections and associations")
        unit = session_routes.get(f"/knowledge/units/{opts['--policy']}", held["token"])
        if not unit.get("blob"):
            raise ValueError(f"the policy {opts['--policy']} has no blob yet: its knowledge is not synced")
        path = f"/goals/{goal}/plans"
        body = {"expected_goal_revision": fences["goal_revision"], "expected_plan_revision": fences["plan_revision"],
                "policy": opts["--policy"], "expected_policy_revision": unit["blob"],
                "expected_input_digest": fences["plan_inputs"], **decision}
        shown += [f"  plan revision {fences['plan_revision']} (this plan is the next)",
                  f"  policy {opts['--policy']} at blob {unit['blob']}",
                  f"  inputs {fences['plan_inputs']}",
                  f"plan: {decision.get('purpose', '')}",
                  *(f"  {s.get('role')}: {s.get('card') or s.get('issue')} {s.get('statement', '')}"
                    for s in decision.get("selections") or []),
                  *(f"  {a.get('change')} {a.get('card')} under criterion {a.get('criterion')}"
                    for a in decision.get("associations") or [])]
    elif kind == "judge":
        opts, left = _options(rest, (), ("--evidence",))
        if len(left) < 4 or left[2] != "because":
            raise ValueError("prepare judge takes <goal> <criterion>|goal <verdict> [--evidence …]... because <why>")
        target, verdict, why = left[0], left[1], " ".join(left[3:])
        body = {"expected_revision": fences["goal_revision"], "verdict": verdict, "rationale": why,
                "evidence": _evidence(opts["--evidence"])}
        if target == "goal":
            path = f"/goals/{goal}/judgments"
            considered = [c["judgment"]["id"] for c in reading.get("criteria") or [] if c.get("judgment")]
            body = {"expected_revision": body["expected_revision"], "criterion_judgments": considered, **body}
            shown += [f"  over the criterion judgments now effective: {', '.join(considered) or 'none'}"]
        else:
            path = f"/goals/{goal}/criteria/{target}/judgments"
            named = {c["id"]: c for c in reading.get("criteria") or []}
            if target not in named:
                raise ValueError(f"criterion {target} is not in the goal's current revision")
            shown += [f"  criterion {target}: {named[target].get('statement', '')}"]
        shown += [f"judge: {verdict}, because {why}",
                  *(f"  evidence {json.dumps(e, sort_keys=True)}" for e in body["evidence"])]
    elif kind == "import":
        opts, left = _options(rest, ("--source", "--revision", "--rows"))
        if left or len(opts) != 3:
            raise ValueError("prepare import takes <goal> --source <source> --revision <revision> --rows <file>")
        rows = _json_file(opts["--rows"], "the rows")
        if not isinstance(rows, list):
            raise ValueError("the rows file holds a JSON list of rows")
        path = f"/goals/{goal}/imports"
        body = {"expected_goal_revision": fences["goal_revision"], "source": opts["--source"],
                "revision": opts["--revision"], "rows": rows}
        mapped = sum(1 for r in rows if isinstance(r, dict) and r.get("criteria"))
        shown += [f"import: {opts['--source']} at {opts['--revision']}, {len(rows)} rows, {mapped} mapped to criteria"]
    else:
        raise ValueError(f"prepare takes plan, judge or import, not {kind!r}")
    key = door.occurrence()
    prepared = {"path": path, "body": body, "key": key, "goal": goal, "read": _inputs(reading), "fences": fences}
    target = _prepared_dir(held) / f"{key}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prepared, indent=2, sort_keys=True) + "\n")
    print("\n".join(shown))
    print(f"prepared: {target}")
    return 0


def _prepared_dir(held: dict) -> Path:
    return Path(held["ws"]) / ".claude" / "goal-commands"


def submit(held: dict, file: Path) -> int:
    import lease as lease_mod
    import refusal
    import session_routes
    try:
        prepared = json.loads(file.read_text())
        path, body, key, goal = prepared["path"], prepared["body"], prepared["key"], prepared["goal"]
    except (OSError, ValueError, KeyError, TypeError) as why:
        raise ValueError(f"{file} is not a prepared goal command ({why})") from None
    session_routes.seat_generation(held["session"], held["token"])
    print(f"command id {key}", file=sys.stderr)
    try:
        answer = session_routes.write(path, body, session_routes._own_key("seat.goal", held["session"], key),
                                      session_routes.SESSION_CARRIER, held["token"])
    except session_routes.Unsent as e:
        print(refusal.retry(str(e)))
        print(f"the answer was lost: submit {file} again, which sends the same body under the same key", file=sys.stderr)
        return 1
    except session_routes.Refused as e:
        if not session_routes.settled(e):
            print(refusal.retry(str(e)))
            print(f"the answer was not decided: submit {file} again, which sends the same body under the same key",
                  file=sys.stderr)
            return 1
        if e.code in lease_mod.SEAT_LOST:
            # The seat moved between this client's check and Go's: the decision is sound, so the file stays.
            print(f"refused: this session no longer holds the seat ({e.code}); {file} is kept for the holder to submit")
            return 1
        file.unlink(missing_ok=True)
        if e.code in STALE:
            print(f"refused: {e.code}: the goal moved since this command was prepared")
            print("\n".join(_moved(held["token"], prepared)))
            print("prepare again once you have reconsidered")
        else:
            print(f"refused: {e}" + (f": {e.detail}" if e.detail else ""))
            print("correct the decision, then prepare it again")
        return 1
    file.unlink(missing_ok=True)
    replayed = " (answered from the first submit)" if answer.get("replayed") else ""
    print(f"written: {path.removeprefix(f'/goals/{goal}/')} of goal {goal} at position {answer.get('position')}{replayed}")
    return 0


def _moved(token: str, prepared: dict) -> list[str]:
    """What moved between the prepared read and now: each fence, then each card and judgment the
    plan's inputs cover."""
    import session_routes
    try:
        reading = _read(token, prepared["goal"])
    except (session_routes.Refused, session_routes.Unsent) as why:
        return [f"  the goal could not be read again ({why})"]
    then_f, now_f = prepared["fences"], _fences(reading)
    lines = [f"  {name.replace('_', ' ')}: {then_f[name]} then, {now_f[name]} now"
             for name in ("goal_revision", "plan_revision", "plan_inputs") if then_f.get(name) != now_f.get(name)]
    if prepared["body"].get("policy"):
        try:
            blob = session_routes.get(f"/knowledge/units/{prepared['body']['policy']}", token).get("blob")
        except (session_routes.Refused, session_routes.Unsent):
            blob = None
        if blob != prepared["body"]["expected_policy_revision"]:
            lines.append(f"  policy blob: {prepared['body']['expected_policy_revision']} then, {blob} now")
    then_i, now_i = prepared["read"], _inputs(reading)
    for card in sorted(set(then_i["cards"]) | set(now_i["cards"])):
        a, b = then_i["cards"].get(card), now_i["cards"].get(card)
        if a != b:
            name = (b or a)["name"]
            lines.append(f"  card {card} {name}: {_card(a)} then, {_card(b)} now")
    for judgment in sorted(set(now_i["judgments"]) - set(then_i["judgments"])):
        lines.append(f"  judgment {judgment} was recorded")
    return lines or ["  nothing the reading shows moved; read the refusal's own check"]


def _card(c: dict | None) -> str:
    if c is None:
        return "not part of the goal"
    return f"{c['state']}{', concluded' if c['concluded'] else ''}" + (f", waiting on {', '.join(c['waits'])}" if c["waits"] else "")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
