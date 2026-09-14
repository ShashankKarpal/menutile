#!/usr/bin/env bash
# install.sh - give one status script its own menu bar app.
#
# Builds (idempotent, safe to re-run):
#   ~/.menutile-venv                 packages only (rumps), on a uv-managed CPython
#   ~/Applications/<Name>.app        a copy of that CPython as the executable,
#                                    Info.plist, optional icon, ad-hoc signature
#                                    (scripts/make_bundle.sh)
#   ~/Library/LaunchAgents/<label>   runs the bundle executable with menutile.py,
#                                    stdlib and site-packages wired through
#                                    PYTHONHOME and PYTHONPATH, KeepAlive
#
# Usage:
#   scripts/install.sh --name "My Tile" --script /path/to/tile.sh \
#       [--interval 60] [--arg VALUE ...] [--bundle-id com.example.mytile] \
#       [--label com.example.mytile] [--icns FILE] [--allow-root DIR ...] \
#       [--rebuild]
#
# Every --arg is passed to the script as one token, emitted as --arg=VALUE
# so a value that itself starts with dashes survives argparse. The agent's
# stdout and stderr go to ~/Library/Logs/menutile/<label>.log.
#
# Why the bundle: macOS presents notification banners and real process names
# only for LaunchServices-registered .app bundles. An unbundled interpreter
# can call the notification APIs successfully while the system files every
# banner without presenting one.
#
# Known failure mode: the bundle holds a COPY of the interpreter and finds
# its stdlib through PYTHONHOME. If uv later removes or moves that directory
# the agent exits 78 (EX_CONFIG) before Python runs a line, silently. Re-run
# this script after any uv Python change; `launchctl list` shows the 78.
set -euo pipefail

NAME=""; SCRIPT=""; INTERVAL=60; BID=""; LABEL=""; ICNS=""; REBUILD=0
ARGS=(); ROOTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --script) SCRIPT="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --arg) ARGS+=("$2"); shift 2 ;;
    --arg=*) ARGS+=("${1#--arg=}"); shift ;;
    --bundle-id) BID="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --icns) ICNS="$2"; shift 2 ;;
    --allow-root) ROOTS+=("$2"); shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
fail() { printf 'FAILED: %s\n' "$1" >&2; exit 1; }
step() { printf '\n==> %s\n' "$1"; }
[ -n "$NAME" ] && [ -n "$SCRIPT" ] || fail "--name and --script are required"
[ -x "$SCRIPT" ] || fail "script not executable: $SCRIPT"
SCRIPT="$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")"
slug="$(printf '%s' "$NAME" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"
[ -n "$slug" ] || fail "--name needs at least one letter or digit"
[ -n "$BID" ] || BID="com.menutile.$slug"
[ -n "$LABEL" ] || LABEL="com.menutile.$slug"
case "$INTERVAL" in ''|*[!0-9]*) fail "--interval must be an integer" ;; esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.menutile-venv"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/menutile"
UID_NUM="$(id -u)"
APP="$HOME/Applications/$NAME.app"
PLIST="$AGENTS/$LABEL.plist"

command -v uv >/dev/null 2>&1 || fail "uv not found: brew install uv"
PY="$(uv python find 3.14 2>/dev/null || true)"
[ -n "$PY" ] || fail "no uv-managed Python 3.14. Run: uv python install 3.14"
UVPY="$(cd "$(dirname "$PY")/.." && pwd)"

step "Name check"
# A second app with the same name or bundle id fights over the bar and the
# notification identity. Refuse to collide with anything already installed.
if [ ! -d "$APP" ] || ! grep -q "<string>$BID</string>" "$APP/Contents/Info.plist" 2>/dev/null; then
  if ls /Applications "$HOME/Applications" 2>/dev/null | grep -qx "$NAME.app"; then
    fail "an app named '$NAME' already exists and is not ours"
  fi
  if pgrep -x "$NAME" >/dev/null 2>&1; then fail "a process named '$NAME' is already running"; fi
fi
echo "name '$NAME', bundle id $BID, label $LABEL"

step "Shared venv at $VENV"
if [ "$REBUILD" = 1 ] || [ ! -x "$VENV/bin/python3" ]; then
  [ -d "$VENV" ] && mv "$VENV" "$VENV.old.$(date +%Y%m%d%H%M%S)"
  uv venv --python "$PY" "$VENV"
fi
uv pip install --python "$VENV/bin/python3" --quiet 'rumps>=0.4.0'
"$VENV/bin/python3" -c 'import rumps' || fail "rumps not importable"

step "$NAME: bundle"
mkdir -p "$LOGS"
ICON_ARGS=()
[ -n "$ICNS" ] && ICON_ARGS=(--icns "$ICNS")
bash "$REPO/scripts/make_bundle.sh" --name "$NAME" --bundle-id "$BID" --python-home "$UVPY" \
  --version 1.0 --out "$HOME/Applications" ${ICON_ARGS[@]+"${ICON_ARGS[@]}"} >/dev/null \
  || fail "$NAME: bundle build failed"

step "$NAME: LaunchAgent $LABEL"
mkdir -p "$AGENTS"
xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
ARGS_XML=""
for a in ${ARGS[@]+"${ARGS[@]}"}; do
  ARGS_XML="$ARGS_XML    <string>--arg=$(xml_escape "$a")</string>
"
done
for r in ${ROOTS[@]+"${ROOTS[@]}"}; do
  ARGS_XML="$ARGS_XML    <string>--allow-root</string><string>$(xml_escape "$r")</string>
"
done
cat > "$PLIST.new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$APP/Contents/MacOS/$NAME")</string>
    <string>$(xml_escape "$REPO/menutile.py")</string>
    <string>--name</string><string>$(xml_escape "$NAME")</string>
    <string>--script</string><string>$(xml_escape "$SCRIPT")</string>
$(printf '%s' "$ARGS_XML")    <string>--interval</string><string>$INTERVAL</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$REPO")</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(xml_escape "$LOGS/$LABEL.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOGS/$LABEL.log")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string>
    <key>PYTHONHOME</key><string>$(xml_escape "$UVPY")</string>
    <key>PYTHONPATH</key><string>$(xml_escape "$VENV/lib/python3.14/site-packages")</string>
  </dict>
</dict>
</plist>
EOF
plutil -lint "$PLIST.new" >/dev/null || fail "rendered plist invalid"
mv "$PLIST.new" "$PLIST"
# launchd pins the program's code identity at bootstrap; after a rebuild the
# agent must be booted out and bootstrapped again (kickstart -k refuses).
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID_NUM" "$PLIST"

step "$NAME: verify"
sleep 4
if ! pgrep -f "$APP/Contents/MacOS/$NAME" >/dev/null; then
  launchctl print "gui/$UID_NUM/$LABEL" 2>&1 | grep -E 'last exit|state' || true
  fail "$NAME is not running; read $LOGS/$LABEL.log"
fi
echo "$NAME running: pid $(pgrep -f "$APP/Contents/MacOS/$NAME" | head -1)"
tail -2 "$LOGS/$LABEL.log" 2>/dev/null || true
printf '\nDone.\n'
