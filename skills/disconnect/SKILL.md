---
name: disconnect
description: Use when a session finishes or the brain releases it.
---

If you leave work unfinished, checkpoint at `exit` first (`/2mw2lt:checkpoint`). Using its posted
note and the session name connect printed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" --handover <note-url> <session>
```

With nothing to hand over:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" <session>
```

Keep the reach armed until `detached:` confirms the exit. Removing a worktree or stopping a
hold does not end the enrollment. Remove your worktree only after `detached:`, and from a shell
outside it: a shell whose directory was removed cannot run `disconnect.py`. Bare invocation and `--help` show usage rather than leave:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" --help
```

Connect afterwards creates a fresh epoch. A still-running incarnation already bound under
another enrollment cannot be rebound under that name; restart it if the binding refuses.
