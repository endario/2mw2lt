#!/bin/bash
# Start a Mac login's agent at boot, for a login reached only over ssh: the macOS counterpart of
# agent-host.sh's `loginctl enable-linger`. Such a login has no GUI session, so its agent job
# lives in its background launchd session, which launchd never starts
# on its own. Run as root, once, after the login's first `2mw2lt install`:
#
#   sudo bash "${CLAUDE_PLUGIN_ROOT}/skills/install/mac-agent-boot.sh" <login>
set -eu
LOGIN=${1:?usage: mac-agent-boot.sh <login>}
[[ "$LOGIN" =~ ^[a-z_][a-z0-9_.-]*$ ]] || { echo "mac-agent-boot: $LOGIN is not a login name" >&2; exit 1; }
DESTDIR=${DESTDIR-}
[ -n "$DESTDIR" ] || [ "$(id -u)" = 0 ] || { echo "mac-agent-boot: run as root" >&2; exit 1; }
NUMBER=$(id -u "$LOGIN")
HOMEDIR=$(eval echo "~$LOGIN")
JOB=$HOMEDIR/Library/LaunchAgents/com.2mw2lt.agent.plist
[ -f "$DESTDIR$JOB" ] || { echo "mac-agent-boot: $JOB is missing; run the workspace's install (2mw2lt install) as $LOGIN first" >&2; exit 1; }
LABEL=com.2mw2lt.agent-boot.$LOGIN
PLIST=$DESTDIR/Library/LaunchDaemons/$LABEL.plist
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST.part" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/sh</string>
		<string>-c</string>
		<string>/bin/launchctl print user/$NUMBER/com.2mw2lt.agent &gt;/dev/null 2&gt;&amp;1 || /bin/launchctl bootstrap user/$NUMBER '$JOB'</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
</dict>
</plist>
PLIST
chmod 644 "$PLIST.part"
[ -n "$DESTDIR" ] || chown root:wheel "$PLIST.part"
mv "$PLIST.part" "$PLIST"
echo "mac-agent-boot: $LABEL loads $JOB into user/$NUMBER at boot"
