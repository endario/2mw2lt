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

An invitation code (`2MW-` and four groups of four) is passed through as given, beside the path or
alone: the engine takes it as the code, and the sign-in carries it to GitHub. It admits the GitHub
account it was made for, once.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" . 2MW-7KQ4-XN2D-9HTB-M3PC
```

Bare invocation and `--help` show usage without installing:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" --help
```

It needs Python 3.11 or later. An older `python3` stops at once with a line saying so: the person
installs a newer one (`brew install python` on macOS), then run it again.

Its pauses outlast a foreground command: it waits up to 30 minutes for the sign-in, 15 for the
grant and 10 for a new team's silo. In Claude Code, run it with Bash `run_in_background: true` and
read its output as it goes; you are told when it exits. In Codex, give the command a timeout of
at least 60 minutes, or poll its running session for output until it exits. The person acts only
where it pauses:

- **`sign in`**: it prints a console address and a code, and opens the page. The person signs in
  there and approves. After approving, that browser carries on by itself: it waits for the
  workspace, then opens GitHub's page for the 2mw2lt App.
- **`github`**: the person installs the App on GitHub's page, which the browser has opened, or
  authorizes the existing installation. GitHub then sends them to a page that says whether it
  connected; when it did, they return to the terminal. Only the team's owner, signed in with GitHub,
  can do this; anyone else asks the owner. From another browser, the desk's **Connect GitHub** in
  the account menu is the same step, and the engine prints that address. A team that already holds an
  installation is not asked: the engine prints GitHub's page where the owner adds the repository.

A team new to the platform prints `team` lines while its silo is made, naming the host's state
and the time waited; that needs no one. `waiting on the host's enrolment watcher` is still
progress: tell the person, and keep waiting. A line ending `; retrying` is a console or door that
did not answer; the engine asks again on its own.

At each pause, tell the person in one line what to click, then keep waiting. The engine polls on
its own and carries on once the grant lands. Do not ask the person to confirm they have done it.

A run on a finished install signs nobody in and opens no page. A run stopped after `machine`
resumes at the grant, with no second sign-in.

Its last line is a JSON object: `{workspace, door, port, tracks, tracks_missing}`.

When it stops, its last line names the step and the remedy. Do what that line says, then run the
engine again. These are the stops that need a person:

- **`the team's silo was refused: …`**, **`the team's silo would not hold <repo>: …`**,
  **`this team has no silo yet`** or **`this team's silo serves no workspace`**: the platform
  will not make or use the team's silo. Tell the person the reason, and stop: the operator
  settles it.
- **`sign-in ended: That invitation code …`** or **`sign-in ended: The console does not know that invitation code …`**: the line says whether the code is unknown, used,
  lapsed or for a different GitHub account, and what to do. Tell the person that line; a code for
  a different account runs again with the same code once they are signed in to GitHub as the
  right one, and the others need a new code from whoever sent it.
- **`only the team's owner installs a repository`**: the person who signed in does not own the
  team. Someone who does must run the install.
- **`you are in no team`**: the account that signed in belongs to no team. Tell the person,
  and stop: they sign in with the account that owns the team, or the operator settles it.
- **`you own no team by that id`**: `--team` named a team the person does not own. Run again
  with an id from the `you own several teams` line, or without `--team`.
- **`a new team's silo is made for a team of its one owner`**: a new team gets its silo only
  while its owner is its one member. Stop: the operator settles it.
- **`you own several teams; name one with --team: …`**: the line lists each team's id and name.
  Ask the person which team the repository belongs to, then run the engine again with
  `--team <id>`.
- **`several GitHub logins can read <repository>; name one with --gh-account: …`**: this
  machine's `gh` is signed in as more than one login that can read the repository. Ask the
  person which login the workspace's workers act as, then run the engine again with
  `--gh-account <login>`. The choice is recorded, so later runs need no flag.
- **`the agent is started by launchd, and this machine runs …`**: only macOS installs here. A
  Linux host is provisioned by the operator with `steering/host/provision-agent-host.sh`.

Other stops, and what to do; one not listed here names its own remedy:

- **`has no GitHub origin`**: the checkout names no `origin` on GitHub. Add it, then run again.
- **`install the GitHub CLI (gh)`**: install `gh`, then run again.
- **`gh is signed in to no GitHub account`**: run `gh auth login`, then run again.
- **`no gh account can read <repository>`**: run `gh auth login` as a login that can.
- **`<login> cannot read <repository>; name one of …`**: `--gh-account` named a login that cannot
  read the repository. Run again with one of the logins the line names.
- **`<checkout> is bound to <workspace>, which serves …`**: the checkout's origin moved away from
  the repository its workspace serves. Run `install.py uninstall` here, then install again.
- **`gh could not check whether <login> reads <repository> (…)`**: GitHub refused for a reason
  other than the repository being unseen, such as SAML single sign-on or a rate limit. Run the
  `gh api` the line names to see it, settle it, then run again.
- **`<the console or door> at <address> did not answer (…)`**: the network or the platform is
  down. Check the network, then run again.
- **`the device token is not a live session`**: the sign-in lapsed mid-run. Run again.
- **`sign-in ended: …`** or **`the sign-in code expired`**: the person denied the sign-in, or took
  over 30 minutes. Run again for a fresh code.
- **`the team's silo is still being made`**: the new team's silo took over ten minutes to start.
  Run again: it waits on the same request.
- **`the console would not admit this machine`**: enrolling the machine failed. Run again; if it
  repeats, the operator reads the console's log.
- **`this machine's credential was refused: …`**: the console would not exchange the secret it
  just issued. Run again; if it repeats, give the operator the line.
- **`the workspace's door answered <status> …`**: the door is up and erring. Run again; if it
  repeats, give the operator the line.
- **`the App does not reach <repository> yet`**: the grant took over 15 minutes. Run again once
  the owner has chosen Connect GitHub or added the repository.
- **`the workspace's door states no agent release`**: the operator deploys one.
- **`the agent release could not be installed: …`** or **`the agent release's installer
  failed: …`**: fetching, building or installing the agent failed, and the rest of the line says
  why. For `the agent needs uv`, install uv; otherwise read the error.
- **`127.0.0.1:<port> answers, but not as this machine's 2mw2lt agent`**: another program holds
  the agent's port. Stop it, then run again.
- **`the agent did not answer on 127.0.0.1:<port>`**: the machine's agent did not serve the
  workspace within a minute. Read `~/Library/Logs/2mw2lt/`; the workspace's `.env` must name the
  door its entry in `~/.config/2mw2lt/workspaces/` names.
- **`stopped at <step>…`**: the run was interrupted, or a local file could not be written. Settle
  what the line names, then run again: it resumes.

If the `/device` page says `Invalid user code`, run the engine again for a fresh code. If it says
`Too many requests`, wait a minute.

## 2. Draft the lanes, when `tracks_missing` is true

The workspace works without lanes, and its board says it has none yet. Give it lanes now.

1. Read the track principles in the `tracks` skill, the repository's
   README, its top-level layout and its open issues (`gh issue list -R <repo> --limit 50`, `<repo>` being the repository the engine printed).
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
   Then ask with your question tool: accept, say what to change, or not now. Revise until they
   accept. On not now, delete the drafted document, commit nothing, and go on to
   [Connect](#3-connect): the board waits for a document at that path, and the next run of this
   skill drafts the lanes again.
5. Give the repository a canon when it has none: somewhere its decisions and lessons live. Set
   `index` in the tracks document to the repository's documentation index, or to
   `.2mw2lt/README.md` beside the tracks document when it has none. Unless the index already
   links a `decisions/` and a `lessons/` index, add `decisions/README.md` and
   `lessons/README.md` in the index's directory, each a title and nothing else, since git keeps
   no empty directory, and link both from the index. Run the guard the plugin ships, which checks
   every unit the index reaches:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/steering/canon.py" --index <index> .
   ```

6. Commit it all on a branch `2mw2lt/tracks`, push it, and open a pull request with
   `gh pr create -R <repo>`. The person accepting the lanes was the review, so offer to merge it
   now. Ask the engine how it would merge:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" lanes .
   ```

   Its last line is `{pr, url, head, method, auto}`. Ask with your question tool: *merge #<pr>
   now by <method>, so the board has its lanes before install ends*, or *not yet*. On merge:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/install.py" lanes . --merge
   ```

   It merges as the workspace's GitHub login, never past a branch rule, then waits up to two
   minutes for the board to hold the lanes; give the command at least ten. Its last line is
   `{pr, url, merged, reason?, queued?, synced?}`:
   - `synced` true: the board shows the lanes.
   - `merged` true and `synced` false: the lanes are merged and the board shows them once the
     daemon syncs the repository; with a `reason`, the daemon refused the merged document, and the
     reason says what to fix.
   - `merged` false: tell the person the `reason` in one line. `queued` means it merges itself
     once its checks pass.

   On not yet or a refused merge, the desk's **Adopt your lanes** step links the pull request
   until it merges. Do not retry the merge or ask the person to.

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
