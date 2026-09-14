#!/usr/bin/env bash
# uninstall.sh - remove one tile: its LaunchAgent, bundle and log.
# Usage: scripts/uninstall.sh --name "My Tile" [--label com.example.mytile]
# The shared venv (~/.menutile-venv) is left alone; other tiles use it.
set -euo pipefail
NAME=""; LABEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$NAME" ] || { echo "--name is required" >&2; exit 2; }
slug="$(printf '%s' "$NAME" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"
[ -n "$LABEL" ] || LABEL="com.menutile.$slug"
UID_NUM="$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$HOME/Applications/$NAME.app"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
[ -f "$PLIST" ] && rm -f "$PLIST" && echo "removed $PLIST"
if [ -d "$APP" ]; then
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -u "$APP" >/dev/null 2>&1 || true
  rm -rf "$APP" && echo "removed $APP"
fi
rm -f "$HOME/Library/Logs/menutile/$LABEL.log"
echo "Done."
