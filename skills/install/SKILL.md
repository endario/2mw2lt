---
description: Install 2mw2lt in this repository - sign in, create its workspace, admit this machine, give the App the repository, start the agent, wire the hooks and draft the lanes. Run it in the repository; run it again to resume or check.
---

Supplied arguments: `$ARGUMENTS`.

The workspace is the supplied repository path, or `.` when none was supplied. Add `--codex`
when the workspace's sessions are Codex's (no hooks are written). In Codex, use the plugin
root shown for this loaded skill wherever commands name `${CLAUDE_PLUGIN_ROOT}`.

## 1. Run the engine

With no supplied arguments, execute on the current workspace explicitly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" .
```

When arguments were supplied, replace `.` with that path and its flags; quote a path containing
spaces. For example:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" "<repository-path>" --codex
```

Bare invocation and `--help` show usage without installing:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" --help
```

Run it with the Bash tool and let it run to the end. The person acts only where it pauses:

- **`sign in`**: it prints a console address and a code, and opens the page. The person signs in
  there and approves.
- **`github`**: it prints the workspace's desk address and opens it. The person chooses **Connect
  GitHub** in the account menu, which installs the 2mw2lt App on the repository or authorizes the
  existing installation. Only the team's owner, signed in with GitHub, has that item; anyone else
  asks the owner to choose it.

A team new to the platform prints a `team` line while its silo is made; that needs no one.

At each pause, tell the person in one line what to click, then keep waiting. The engine polls on
its own and carries on once the grant lands. Do not ask the person to confirm they have done it.

A run on a finished install signs nobody in and opens no page.

Its last line is a JSON object: `{workspace, door, port, tracks, tracks_missing}`.

When it stops, its last line names the step and the remedy. Do what that line says, then run the
engine again. These are the stops that need a person:

- **`the team's silo was refused: …`**: the platform would not make the new team's silo. Tell
  the person the reason, and stop: the operator settles it.
- **`only the team's owner installs a repository`**: the person who signed in does not own the
  team. Someone who does must run the install.
- **`you own several teams; name one with --team: …`**: the line lists each team's id and name.
  Ask the person which team the repository belongs to, then run the engine again with
  `--team <id>`.

The other stops, and what to do:

- **`has no GitHub origin`**: the checkout names no `origin` on GitHub. Add it, then run again.
- **`sign-in ended: …`** or **`the sign-in code expired`**: the person denied the sign-in, or took
  over 30 minutes. Run again for a fresh code.
- **`the team's silo is still being made`**: the new team's silo took over ten minutes to start.
  Run again: it waits on the same request.
- **`the team's silo would not hold <repo>: …`**: the platform refused to add the repository.
  Tell the person the answer that follows, and stop: the operator settles it.
- **`the console would not admit this machine`**: enrolling the machine failed. Run again; if it
  repeats, the operator reads the console's log.
- **`the agent release could not be installed: …`**: fetching or building the agent failed, and
  the rest of the line says why. For `the agent needs uv`, install uv; otherwise read the error.
- **`the agent is started by launchd, and this machine runs …`**: only macOS has a launcher yet.
- **`the agent did not answer on 127.0.0.1:<port>`**: the machine's agent did not serve the
  workspace within a minute. Read `~/Library/Logs/2mw2lt/`; the workspace's `.env` must name the
  door its entry in `~/.config/2mw2lt/workspaces/` names.

If the `/device` page says `Invalid user code`, run the engine again for a fresh code. If it says
`Too many requests`, wait a minute.

## 2. Draft the lanes, when `tracks_missing` is true

The workspace works without lanes, and its board says it has none yet. Give it lanes now.

1. Read the track principles in the `tracks` skill, the repository's
   README, its top-level layout and its open issues (`gh issue list --limit 50`).
2. Draft a tracks document at the path in `tracks`. It needs at least:
   - `repo`: the workspace's repository;
   - `lanes`: 3 to 7 product capabilities.

   Each lane is `{id, name, hue, prefixes, outcome}`:
   - `id` is stable and never changes, even if the lane is renamed.
   - `outcome` includes its "Not:" boundary.
   - `prefixes` are the branch prefixes of the work that belongs in the lane.

   Repository upkeep is not a lane.
3. Validate it:

   ```bash
   python3 -c 'import json,sys; sys.path.insert(0,sys.argv[1]); import tracksdoc; e=tracksdoc.validate(json.load(open(sys.argv[2])), sys.argv[3]); print("\n".join(e) or "valid"); sys.exit(bool(e))' "${CLAUDE_PLUGIN_ROOT}/steering" <path> <owner/name>
   ```

4. Show the person the lanes in a few lines each: the name, the outcome and the Not boundary.
   Then ask with your question tool: accept, or say what to change. Revise until they accept.
5. Give the repository a canon when it has none: somewhere its decisions and lessons live
   (doc 148 §3). Set
   `index` in the tracks document to the repository's documentation index, or to
   `.2mw2lt/README.md` beside the tracks document when it has none. Unless the index already
   links a `decisions/` and a `lessons/` index, add `decisions/README.md` and
   `lessons/README.md` in the index's directory, each a title and nothing else, since git keeps
   no empty directory, and link both from the index. Run the guard the plugin ships, which checks
   every unit the index reaches:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/steering/canon.py" --index <index> .
   ```

6. Commit it all on a branch `2mw2lt/tracks` and open a pull request with `gh pr create`. Tell the
   person it seeds the board once it merges. Do not wait for the merge.

## 3. Connect

Run `/2mw2lt:connect` in this session. It enrols this session, binds it and holds its stream. That
proves the door answers on this machine's credential, that a hook reached the platform, and that a
directive can reach the session. If it refuses, its line names the step to repeat.

## Uninstall

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" uninstall $ARGUMENTS
```

It removes what the install added on this machine:
- the hooks;
- the `.env` lines;
- the workspace's id file and its launcher;
- its entry in the machine agent's registry;
- this workspace's credential.

With `--workspace`, it also has the platform retire the workspace, after one more sign-in. The
platform keeps the workspace's state rather than deleting it.

Install the plugin itself at user scope, from a directory that is not your home. From `~`,
`--scope project` writes into `~/.claude/settings.json`, which every repository under it then
inherits.
