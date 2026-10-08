"""Static help contract shared by worker entrypoints and door refusals.

Rendering this module never reads configuration, credentials, the workspace, or the network.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path


# Each grammar is copied from DOOR.md. The ordered forms are the wire contract; client-specific
# callers add their own usage form to the matching entry as they migrate.
_RAW_FORMS = {
    "announce": ["announce: <session> token <t> as <harness>/<account> [on <branch>] doing <text>"],
    "blocked": ["blocked: <session> token <t> on <what>"],
    "wait": ["wait: <session> token <t> for <kind> <on> [recheck <seconds>]"],
    "done": ["done: <session> token <t> [<what>]"],
    "recommend": ["recommend: <session> token <t> <text>"],
    "ask": ["ask: <session> token <t> <question>", "ask: <session> token <t> json <object>"],
    "claim": ["claim: <resource> as <claimant> token <t>"],
    "taking": ["taking: <issue> token <t>", "taking: branch <name> token <t>"],
    "declare": ["declare: <session> token <t> card <ulid> <name> [<verb>:<n>[,<n>] …]",
                "declare: <session> token <t> link <card> requires|part-of <card>|<owner>/<name>#<n> [--source <where>] <why>",
                "declare: <session> token <t> unlink <card> requires|part-of <card>|<owner>/<name>#<n> resolved|withdrawn <why>"],
    "checkpoint": ["checkpoint: <session> token <t> json {\"id\", \"boundary\", \"note\", \"note_sha256\", \"learned\"?}"],
    "holds": ["holds: <branch> token <t>", "holds: issue <n> token <t>"],
    "enroll": ["enroll: <session> as <harness>/<account> workspace <abs path> transcript <abs path or -> [on <branch>] [doing <text>]"],
    "bind": ["bind: <session> provider-session <id> runtime <runtime_id> token <t>"],
    "ack": ["ack: <ulid> token <t>"],
    "say": ["say: <session> [to <target>] token <t> <text>"],
    "note": ["note: token <lease token> <text>"],
    "launch": ["launch: token <lease token> <harness> [on <node>] [vendor <vendor>] [account <n>] [model <model>] [thinking <level>] [effort <level>] [window] because <reason>"],
    "relay": ["relay: token <lease token> to <session>[@<epoch>] <text>"],
    "retire": ["retire: token <lease token> <worker> on <node>"],
    "card": ["card: token <lease token> <verb> <args…>"],
    "effort": ["effort: token <lease token> <item> <level>"],
    "wake": ["wake: token <lease token> <session> because <reason>"],
    "control": ["control: token <lease token> <session> effort <level> because <reason>", "control: token <lease token> <session> model <name>-<version> because <reason>", "control: token <lease token> <session> compact [without checkpoint] because <reason>"],
    "roster": ["roster: token <lease token> [with gone]"],
    "gate": ["gate: review <repo> <branch> pr <n> [head <sha>] [tier <tier>] [harness <harness>] [exclude <vendor>[,<vendor>]] [final] token <t>", "gate: critic <repo> <branch> doc <path> [head <sha>] [tier <tier>] [harness <harness>] [exclude <vendor>[,<vendor>]] [final] token <t>", "gate: status <commission> token <t>", "gate: cancel <commission> token <t>", "gate: pr <n> token <t>", "gate: carry <n> head <sha> [equivalent] token <t>"],
    "lift": ["lift: token <lease token> review <repo> pr <n> <reason>", "lift: token <lease token> critic <repo> branch <branch> <reason>", "lift: token <lease token> bench <vendor> <model> <reason>"],
    "authorship": ["authorship: token <lease token> establish <session> epoch <n> model <model> because <evidence>", "authorship: token <lease token> abandon <session> epoch <n> because <reason>"],
    "dispose": ["dispose: token <lease token> <item id> <reason>"],
    "promote": ["promote: token <lease token> <item id> <reason>"],
    "night": ["night: token <lease token> promote <route> <work class> <reason>", "night: token <lease token> demote <route> <work class> <reason>"],
    "backlog": ["backlog: token <lease token>"],
    "detach": ["detach: <session> token <t> [handover <url> or abandon <reason>]"],
    "rebind": ["rebind: <session> machine_id <stable id>"],
    "supersede": ["supersede: <wrong session> to <right session> provider-session <id> runtime <runtime_id>"],
}

_AUDIENCE = {
    **{verb: "session" for verb in ("announce", "blocked", "wait", "done", "recommend", "ask", "claim", "taking", "declare", "checkpoint", "holds", "enroll", "bind", "ack", "say", "detach", "gate")},
    **{verb: "seat" for verb in ("note", "launch", "relay", "retire", "card", "effort", "wake", "control", "roster", "lift", "authorship", "dispose", "promote", "backlog", "night")},
    "rebind": "operator",
    "supersede": "operator",
}

_EXAMPLE_RUNTIME = "claude:" + "0" * 32 + ":" + "0" * 64
_EXAMPLES = {
    "announce": ["announce: violet token <t> as claude-code/main on feat/help doing add help"],
    "blocked": ["blocked: violet token <t> on waiting for review"],
    "wait": ["wait: violet token <t> for checks 4391 recheck 1800"],
    "done": ["done: violet token <t> help contract"],
    "recommend": ["recommend: violet token <t> approve the release"],
    "ask": ["ask: violet token <t> should this deploy?", 'ask: violet token <t> json {"question":"deploy?"}'],
    "claim": ["claim: migration/42 as violet token <t>"],
    "taking": ["taking: 3384 token <t>", "taking: branch feat/help token <t>"],
    "declare": ["declare: violet token <t> card 010101010101010101010101 verb-help resolves:3384",
                "declare: violet token <t> link 010101010101010101010101 requires endario/repo#3384 the schema lands first",
                "declare: violet token <t> unlink 010101010101010101010101 requires endario/repo#3384 resolved it landed"],
    "checkpoint": ['checkpoint: violet token <t> json {"id":"010101010101010101010101","boundary":"unit-done","note":"https://github.com/example/repo/pull/1#issuecomment-1","note_sha256":"0000000000000000000000000000000000000000000000000000000000000000"}'],
    "holds": ["holds: feat/help token <t>", "holds: issue 3384 token <t>"],
    "enroll": ["enroll: violet as claude-code/main workspace /work/repo transcript - on feat/help doing add help"],
    "bind": [f"bind: violet provider-session provider-1 runtime {_EXAMPLE_RUNTIME} token <t>"],
    "ack": ["ack: 010101010101010101010101 token <t>"],
    "say": ["say: violet to amber token <t> please review this"],
    "note": ["note: token <lease token> review is waiting"],
    "launch": ["launch: token <lease token> claude on build vendor anthropic model opus-5.5 effort high because review"],
    "relay": ["relay: token <lease token> to violet@1 review the branch"],
    "retire": ["retire: token <lease token> worker-1 on build"],
    "card": ["card: token <lease token> reclassify 010101010101010101010101 state live"],
    "effort": ["effort: token <lease token> 3384 high"],
    "wake": ["wake: token <lease token> violet because review arrived"],
    "control": ["control: token <lease token> violet effort high because review", "control: token <lease token> violet model opus-5.5 because review", "control: token <lease token> violet compact without checkpoint because context full"],
    "roster": ["roster: token <lease token> with gone"],
    "gate": ["gate: review endario/repo feat/help pr 1 token <t>", "gate: critic endario/repo feat/help doc documentation/plan.md token <t>", "gate: status 010101010101010101010101 token <t>", "gate: cancel 010101010101010101010101 token <t>", "gate: pr 1 token <t>", "gate: carry 1 head abababababababababababababababababababab token <t>"],
    "lift": ["lift: token <lease token> review endario/repo pr 1 needs another round", "lift: token <lease token> critic endario/repo branch feat/help needs another round", "lift: token <lease token> bench anthropic opus-5.5 retry it"],
    "authorship": ["authorship: token <lease token> establish violet epoch 1 model opus-5.5 because its transcript names the model", "authorship: token <lease token> abandon violet epoch 1 because no transcript or launch record names its model"],
    "dispose": ["dispose: token <lease token> 010101010101010101010101 handled"],
    "promote": ["promote: token <lease token> 010101010101010101010101 owner decision needed"],
    "night": ["night: token <lease token> promote opencode-go/glm-5.3 review clears the spoof probe", "night: token <lease token> demote opencode-go/glm-5.3 triage agreed with a spoof two passes running"],
    "backlog": ["backlog: token <lease token>"],
    "detach": ["detach: violet token <t> handover https://github.com/example/repo/pull/1"],
    "rebind": ["rebind: violet machine_id macbook"],
    "supersede": [f"supersede: wrong to right provider-session provider-1 runtime {_EXAMPLE_RUNTIME}"],
}

VERBS = {
    verb: {"audience": _AUDIENCE[verb], "forms": [
        {"grammar": grammar, "example": _EXAMPLES[verb][index]}
        for index, grammar in enumerate(forms)
    ]}
    for verb, forms in _RAW_FORMS.items()
}

# Runnable entrypoints own client syntax once; wire forms reference these by script and topic.
_SPEAKER_USAGE = "[--provider <harness>] [--provider-session <id>]"
_COMMISSION_USAGE = "[--tier standard|heavy] [--sandbox|--full] [--exclude <vendors>] [--final] [--retry=<id>]"
TOOLS = {
    "ack": {"verb": "ack", "forms": [
        {"topic": "send", "usage": "<session> <ulid>", "example": "ack violet 010101010101010101010101"},
        {"topic": "store", "usage": "--store <session> [--provider-session <id>] < <token-file>", "example": "ack --store violet < token.txt"},
    ]},
    "agent_secret": {"forms": [
        {"topic": "secret", "usage": "<console> < <secret-file>", "example": "agent_secret https://console.example < secret.txt"},
        {"topic": "--review-host", "usage": "--review-host <console> <door-base> < <secret-file>", "example": "agent_secret --review-host https://console.example https://door.example < secret.txt"},
    ]},
    "bind": {"verb": "bind", "current": True, "forms": [
        {"topic": None, "usage": "[--provider <provider>] [--provider-session <id>] [--pid <pid>] [session]", "example": "bind --current"},
    ]},
    "card": {"verb": "card", "bare": False, "forms": [
        {"topic": None, "usage": "--token - <verb> <args…> < <token-file>", "example": "card --token - reclassify 010101010101010101010101 state live"},
    ]},
    "checkpoint": {"verb": "checkpoint", "forms": [
        {"topic": None, "usage": "<session> <boundary> <note-url> [--learned-file <json-file>] [--retry=<id>]", "example": "checkpoint violet unit-done https://github.com/example/repo/pull/1#issuecomment-1"},
    ]},
    "connect": {"current": True, "forms": [
        {"topic": None, "usage": "[--as <harness/account>] [--doing <text>] [--provider <provider>] [--provider-session <id>] [--pid <pid>] [session]", "example": "connect --current"},
    ]},
    "declare": {"verb": "declare", "forms": [
        {"topic": None, "usage": "<session> [--card <ulid>] <name> [resolves:<n> advances:<n> ...]", "example": "declare violet verb-help"},
    ]},
    "edge": {"verb": "declare", "forms": [
        {"topic": "link", "usage": "<session> link <card> requires|part-of <card>|<owner>/<name>#<n> [--source <where>] <why>",
         "example": "edge violet link 010101010101010101010101 requires endario/repo#3384 the schema lands first"},
        {"topic": "unlink", "usage": "<session> unlink <card> requires|part-of <card>|<owner>/<name>#<n> resolved|withdrawn <why>",
         "example": "edge violet unlink 010101010101010101010101 requires endario/repo#3384 resolved it landed"},
    ]},
    "disconnect": {"verb": "detach", "current": True, "forms": [
        {"topic": None, "usage": "[--handover <note-url>] [session]", "example": "disconnect --current"},
    ]},
    "door": {"forms": [
        {"topic": "say", "usage": "--say [--retry=<id>]", "example": "door --say < request.txt"},
        {"topic": "get", "usage": "--get <target>", "example": "door --get /steering/work"},
        {"topic": "post", "usage": "--post <target>", "example": "door --post /steering/door < request.json"},
        {"topic": "--url", "usage": "--url", "example": "door --url"},
        {"topic": "workspace", "usage": "<workspace>", "example": "door ."},
    ]},
    "gate": {"verb": "gate", "forms": [
        {"topic": "review", "usage": _SPEAKER_USAGE + " " + _COMMISSION_USAGE + " <session> review <pr>", "example": "gate violet review 1"},
        {"topic": "critic", "usage": _SPEAKER_USAGE + " " + _COMMISSION_USAGE + " <session> critic <doc>", "example": "gate violet critic documentation/plan.md"},
        {"topic": "status", "usage": _SPEAKER_USAGE + " <session> status <commission>", "example": "gate violet status 010101010101010101010101"},
        {"topic": "cancel", "usage": _SPEAKER_USAGE + " <session> cancel <commission>", "example": "gate violet cancel 010101010101010101010101"},
        {"topic": "pr", "usage": _SPEAKER_USAGE + " <session> pr <pr>", "example": "gate violet pr 1"},
        {"topic": "carry", "usage": _SPEAKER_USAGE + " <session> carry <pr> [--retry=<id>]", "example": "gate violet carry 1"},
    ]},
    "hold": {"current": True, "forms": [
        {"topic": "--frame-path", "usage": "--frame-path <session>", "example": "hold --frame-path violet"},
        {"topic": "--claim-path", "usage": "--claim-path <provider-session>", "example": "hold --claim-path 0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"},
        {"topic": "--session-of", "usage": "--session-of <provider-session>", "example": "hold --session-of 0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"},
        {"topic": "--answer-kick", "usage": "--answer-kick <at> [--provider <provider>] [--provider-session <id>] [--pid <pid>] [session]", "example": "hold --answer-kick 2026-10-06T08:00:00Z"},
        {"topic": "stream", "usage": "[--until-event|--service|--wake plugin] [--provider <provider>] [--provider-session <id>] [--pid <pid>] [session]", "example": "hold --current"},
    ]},
    "holds": {"verb": "holds", "forms": [
        {"topic": "branch", "usage": _SPEAKER_USAGE + " <session> <branch>", "example": "holds violet feat/help"},
        {"topic": "issue", "usage": _SPEAKER_USAGE + " <session> issue <n>", "example": "holds violet issue 3384"},
    ]},
    "hooks": {"forms": [
        {"topic": "install", "usage": "install [workspace] local", "example": "hooks install . local"},
        {"topic": "install-codex", "usage": "install-codex [workspace] local", "example": "hooks install-codex . local"},
        {"topic": "uninstall", "usage": "uninstall [workspace]", "example": "hooks uninstall ."},
        {"topic": "show", "usage": "show [workspace]", "example": "hooks show"},
    ]},
    "install": {"forms": [
        {"topic": "install", "usage": "[workspace] [invite-code] [--codex] [--team <id>] [--gh-account <login>] [--login <login>]", "example": "install . 2MW-7KQ4-XN2D-9HTB-M3PC"},
        {"topic": "lanes", "usage": "lanes [workspace] [--merge] [--gh-account <login>]", "example": "install lanes . --merge"},
        {"topic": "uninstall", "usage": "uninstall [workspace] [--workspace]", "example": "install uninstall ."},
        {"topic": "retire", "usage": "retire <workspace id>", "example": "install retire workspace-1"},
    ]},
    "rest": {"forms": [
        {"topic": None, "usage": _SPEAKER_USAGE + " [--lease] [--post [--json <body>] [--key <key>]] <api-v1-path> [--all]", "example": "rest /openapi.json"},
    ]},
    "lease": {"forms": [
        {"topic": "say", "usage": "say " + _SPEAKER_USAGE + " [--retry=<id>]", "example": "lease say < line.txt"},
        {"topic": "post", "usage": "post <path> " + _SPEAKER_USAGE, "example": "lease post /steering/brain/reply < body.json"},
    ]},
    "seat_section": {"bare": False, "forms": [
        {"topic": None, "usage": _SPEAKER_USAGE, "example": "seat_section --provider claude --provider-session 9a77"},
    ]},
    "rotate": {"current": True, "forms": [
        {"topic": None, "usage": "[--as <harness/account>] [--doing <text>] [--provider <provider>] [--provider-session <id>] [--pid <pid>] [session]", "example": "rotate --current"},
    ]},
    "say": {"verb": "say", "forms": [
        {"topic": None, "usage": "[--to <peer>] " + _SPEAKER_USAGE + " <session> <text|-> [--retry=<id>]", "example": "say --to amber violet please-review"},
    ]},
    "taking": {"verb": "taking", "forms": [
        {"topic": "issue", "usage": _SPEAKER_USAGE + " <session> <issue>", "example": "taking violet 3384"},
        {"topic": "branch", "usage": _SPEAKER_USAGE + " <session> branch [name]", "example": "taking violet branch feat/help"},
    ]},
    "verb": {"forms": [
        {"topic": "announce", "usage": "announce <session> as <harness/account> on <branch> doing <what> [--retry=<id>]", "example": "verb announce violet as claude-code/main on feat/help doing add-help"},
        {"topic": "blocked", "usage": "blocked <session> on <what> [--retry=<id>]", "example": "verb blocked violet on waiting"},
        {"topic": "wait", "usage": "wait <session> for checks <pr>, merged <pr>, verdict <pr>, comment <pr> or at <YYYY-MM-DDTHH:MMZ> [recheck <seconds>] [--retry=<id>]", "example": "verb wait violet for checks 4391"},
        {"topic": "done", "usage": "done <session> <what> [--retry=<id>]", "example": "verb done violet help"},
        {"topic": "recommend", "usage": "recommend <session> <what> [--retry=<id>]", "example": "verb recommend violet approve"},
        {"topic": "ask", "usage": "ask <session> <question> [--retry=<id>]", "example": "verb ask violet should-we-deploy"},
        {"topic": "ask-json", "usage": "ask <session> json <object> [--retry=<id>]", "example": "verb ask violet json '{\"question\":\"deploy?\"}'"},
    ]},
    "launch": {"bare": False, "forms": [
        {"topic": "observe", "usage": "observe", "example": "steering-launch.py observe"},
        {"topic": "readout", "usage": "readout", "example": "steering-launch.py readout"},
        {"topic": "armed", "usage": "armed", "example": "steering-launch.py armed"},
        {"topic": "rebrief", "usage": "rebrief", "example": "steering-launch.py rebrief"},
        {"topic": "hold", "usage": "hold", "example": "steering-launch.py hold"},
        {"topic": "exec", "usage": "exec hold [args...]", "example": "steering-launch.py exec hold --current"},
    ]},
    "armed": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "armed --help"}]},
    "codex_observe": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "codex_observe --help"}]},
    "delete_guard": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "delete_guard --help"}]},
    "trap_guard": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "trap_guard --help"}]},
    "ghtoken": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "ghtoken --help"}]},
    "observe": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "observe --help"}]},
    "readout": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "readout --help"}]},
    "rebrief": {"bare": False, "forms": [{"topic": None, "usage": "[--help]", "example": "rebrief --help"},
                                         {"topic": "--keep", "usage": "--keep <session>", "example": "rebrief --keep violet"}]},
}

EXEMPTIONS = {"outbox": "library", "witness": "library", "probe-stop": "fixture"}

# A wire form references the tool forms that can issue it. Client syntax remains in TOOLS only.
_CLIENTS = {
    "announce": ((("verb", "announce"),),), "blocked": ((("verb", "blocked"),),), "wait": ((("verb", "wait"),),),
    "done": ((("verb", "done"),),), "recommend": ((("verb", "recommend"),),),
    "ask": ((("verb", "ask"),), (("verb", "ask-json"),)),
    "enroll": ((("connect", None),),),
    "bind": ((("bind", None), ("connect", None)),),
    "ack": ((("ack", "send"),),),
    "card": ((("card", None),),),
    "taking": ((("taking", "issue"),), (("taking", "branch"),)),
    "declare": ((("declare", None),), (("edge", "link"),), (("edge", "unlink"),)),
    "checkpoint": ((("checkpoint", None),),),
    "holds": ((("holds", "branch"),), (("holds", "issue"),)),
    "say": ((("say", None),),), "detach": ((("disconnect", None),),),
    "gate": tuple((("gate", form["topic"]),) for form in TOOLS["gate"]["forms"]),
}


def _client_errors(verbs: dict[str, dict]) -> list[str]:
    """Return missing or drifted client references for wire forms that have one."""
    errors = []
    for verb, form_clients in _CLIENTS.items():
        if len(verbs[verb]["forms"]) != len(form_clients):
            errors.append(f"{verb} has a client-form count mismatch")
            continue
        for form, clients in zip(verbs[verb]["forms"], form_clients):
            expected = [{"script": script, "topic": topic} for script, topic in clients]
            if form.get("clients") != expected:
                errors.append(f"{verb}/{clients[0][1] or 'default'} has no matching client form")
    return errors


for _verb, _form_clients in _CLIENTS.items():
    if len(VERBS[_verb]["forms"]) != len(_form_clients):
        raise ValueError(f"{_verb}: client-form count does not match wire forms")
    for _form, _clients in zip(VERBS[_verb]["forms"], _form_clients):
        _form["clients"] = [{"script": _script, "topic": _topic} for _script, _topic in _clients]


def _script(script: str) -> dict:
    try:
        return TOOLS[script]
    except KeyError:
        raise ValueError(f"{script}: no help entry") from None


def wire_help(verb: str) -> str:
    """Render static wire usage for one documented verb."""
    try:
        forms = VERBS[verb]["forms"]
    except KeyError:
        raise ValueError(f"{verb}: no wire help") from None
    lines = [f"usage: {verb}:"]
    for form in forms:
        lines.extend((f"  {form['grammar']}", f"example: {form['example']}"))
    return "\n".join(lines)


def _client_form(script: str, topic: str) -> dict:
    for form in _script(script)["forms"]:
        if form["topic"] == topic:
            return form
    raise ValueError(f"{script}: no help topic {topic!r}")


def _invocation(script: str, invocation: str | None) -> str:
    if invocation is not None:
        return invocation
    path = Path(__file__).with_name("enroll") / f"{script}.py"
    return f"python3 {shlex.quote(str(path))}"


def _example(script: str, invocation: str, example: str) -> str:
    for name in (script, "steering-launch.py"):
        if example == name:
            return invocation
        if example.startswith(name + " "):
            return invocation + example[len(name):]
    return f"{invocation} {example}"


def script_help(script: str, invocation: str | None = None, topic: str | None = None) -> str:
    """Render static client help, optionally narrowed to one declared client form."""
    forms = [_client_form(script, topic)] if topic is not None else _script(script)["forms"]
    name = _invocation(script, invocation)
    lines = []
    tool = _script(script)
    for form in forms:
        current = "[--current] " if tool.get("current") and form["topic"] != "--frame-path" else ""
        lines.extend((f"usage: {name} {current}{form['usage']}", f"example: {_example(script, name, form['example'])}"))
    if tool.get("current") and topic != "--frame-path":
        lines.append("--current: execute for this harness's current session instead of displaying bare-call help.")
    return "\n".join(lines)


def help_requested(script: str, argv: list[str], *, bare: bool = True) -> bool:
    """Whether argv is exactly a static help request, never a token search inside content."""
    tool = _script(script)
    if not argv:
        return bare and tool.get("bare", True)
    if argv in (["-h"], ["--help"]):
        return True
    return (len(argv) == 2 and argv[1] in ("-h", "--help") and
            any(form["topic"] == argv[0] for form in tool["forms"]))


def error(script: str, reason: str) -> int:
    """Print an invocation diagnosis and static recovery, returning POSIX syntax status."""
    print(f"error: {reason}", file=sys.stderr)
    print(script_help(script), file=sys.stderr)
    return 2


def current_args(script: str, argv: list[str]) -> list[str]:
    """Remove one exact inferred-session flag, rejecting its ambiguous spellings."""
    _script(script)
    flags = [arg for arg in argv if arg == "--current"]
    if any(arg.startswith("--current=") for arg in argv):
        raise ValueError("--current takes no value")
    if len(flags) > 1:
        raise ValueError("--current may appear once")
    return [arg for arg in argv if arg != "--current"]
