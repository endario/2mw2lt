---
description: Use when a session finishes or the seat releases it.
---

Checkpoint at `exit` first (`/2mw2lt:checkpoint`). Using its posted note and the session name
connect printed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" --handover <note-url> <session>
```

With nothing to hand over:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" <session>
```

Keep the reach armed until `detached:` confirms the exit. Removing a worktree or stopping a
hold does not end the enrollment. Bare invocation and `--help` show usage rather than leave:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/disconnect.py" --help
```

Connect afterwards creates a fresh epoch. A still-running incarnation already bound under
another enrollment cannot be rebound under that name; restart it if the binding refuses.
