#!/usr/bin/env bash
# make_bundle.sh - build the minimal signed .app bundle a menu bar Python app
# needs for a real identity. Shared by SwitchDeck and the other menu bar apps
# on this Mac, which used to carry hand-synced copies of these lines.
#
# Why a bundle at all: macOS attributes process names and notification
# identity only to real, LaunchServices-registered .app bundles. An unbundled
# interpreter can call the notification APIs successfully while the system
# files every banner without presenting one. The bundle holds exactly a copy
# of the uv-managed static CPython as its executable, an Info.plist, an
# optional icon, and an ad-hoc signature; codesign rejects data files outside
# Resources, so stdlib and site-packages are wired via PYTHONHOME and
# PYTHONPATH in the caller's LaunchAgent. The copied binary diverges from uv
# upgrades until the caller's installer runs again; that is the caller's
# self-check to catch.
#
# Usage:
#   make_bundle.sh --name "App Name" --bundle-id com.example.app \
#                  --python-home /path/to/cpython-3.14-... \
#                  [--version 1.0] [--iconset DIR.appiconset | --icns FILE] \
#                  [--out ~/Applications] [--no-register]
# Prints the bundle executable path on stdout. Exit 0 only when the bundle
# is built, signed and verified. --no-register skips lsregister (tests build
# into a temp dir and must not leave a stale LaunchServices registration).
set -euo pipefail

NAME=""; BID=""; PYHOME=""; VERSION="1.0"; ICONSET=""; ICNS=""
OUT="$HOME/Applications"; REGISTER=1
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --bundle-id) BID="$2"; shift 2 ;;
    --python-home) PYHOME="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --iconset) ICONSET="$2"; shift 2 ;;
    --icns) ICNS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --no-register) REGISTER=0; shift ;;
    *) echo "make_bundle.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done
fail() { printf 'make_bundle.sh: %s\n' "$1" >&2; exit 1; }
[ -n "$NAME" ] && [ -n "$BID" ] && [ -n "$PYHOME" ] || fail "--name, --bundle-id and --python-home are required"
case "$NAME" in */*|.*) fail "bad app name: $NAME" ;; esac
PYBIN="$PYHOME/bin/python3.14"
[ -x "$PYBIN" ] || fail "no executable interpreter at $PYBIN"
[ -d "$PYHOME/lib/python3.14" ] || fail "no lib/python3.14 under $PYHOME"
[ -z "$ICONSET" ] || [ -d "$ICONSET" ] || fail "iconset not found: $ICONSET"
[ -z "$ICNS" ] || [ -f "$ICNS" ] || fail "icns not found: $ICNS"

APP="$OUT/$NAME.app"
mkdir -p "$OUT"
# Rebuilt from scratch every run: the bundle holds no state, and a stale
# interpreter copy inside it is exactly the drift installers exist to prevent.
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$PYBIN" "$APP/Contents/MacOS/$NAME"

ICON_KEY=""
if [ -n "$ICONSET" ]; then
  TMP="$(mktemp -d)"
  cp -R "$ICONSET" "$TMP/AppIcon.iconset"
  rm -f "$TMP/AppIcon.iconset/Contents.json"
  iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns" || fail "iconutil failed on $ICONSET"
  rm -rf "$TMP"
  ICON_KEY='  <key>CFBundleIconFile</key><string>AppIcon</string>'
elif [ -n "$ICNS" ]; then
  cp "$ICNS" "$APP/Contents/Resources/AppIcon.icns"
  ICON_KEY='  <key>CFBundleIconFile</key><string>AppIcon</string>'
fi

# Key order matches the bundles the fleet already ships, so a rebuild of an
# unchanged app produces an identical Info.plist.
{
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$BID</string>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleExecutable</key><string>$NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
EOF
  [ -n "$ICON_KEY" ] && printf '%s\n' "$ICON_KEY"
  cat <<EOF
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
} > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null || fail "rendered Info.plist is invalid"

codesign -s - --force "$APP" 2>/dev/null || fail "codesign failed"
codesign -v "$APP" || fail "bundle signature invalid"
if [ "$REGISTER" = 1 ]; then
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
fi
echo "$APP/Contents/MacOS/$NAME"
