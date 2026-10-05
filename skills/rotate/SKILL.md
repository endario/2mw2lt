---
description: Rotate this session's steering enrolment token, for one that has been disclosed.
---

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/steering/enroll/rotate.py" --doing "<what you are doing>"
```

It detaches the enrolment and makes it again under the same name, which mints a fresh token on a
new epoch and leaves the disclosed one proving nothing. The board's row takes the text `--doing`
carries, so pass it to keep what you had.

The rotation revokes the hold the old token held, so the stream stops. In Claude Code, reopen it
as a background command (`run_in_background: true`) that exits on the first actionable frame —
through the workspace's own stable launcher, not `${CLAUDE_PLUGIN_ROOT}`, the same reason
`/2mw2lt:connect` gives (#1875):

```bash
ws="${STEERING_WORKSPACE:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}"
python3 "$ws/.claude/steering-launch.py" exec hold --until-event <your session>
```

Start it again before you act on that frame. For another stream-holding harness, use the blocking
form under a Monitor armed at its cap (`timeout_ms: 1800000`) and re-arm it on each expiry notice,
the way `/2mw2lt:connect` says. Both forms read the fresh token from the enrollment and derive the
runtime id from this process — nothing to carry.

It rotates only an enrolment minted for this session; name one as the argument to rotate
another of your own.
