<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"  srcset="design/github/readme-banner-dark-1400x400.png">
    <source media="(prefers-color-scheme: light)" srcset="design/github/readme-banner-light-1400x400.png">
    <img alt="menutile" src="design/github/readme-banner-dark-1400x400.png" width="680">
  </picture>
</p>

# menutile

One menu bar app per status script, each with a real identity.

You have a shell script that prints a menu bar item in the SwiftBar or xbar
dialect: a title line, `---`, some rows. menutile runs that script on a
cadence and renders it as a native macOS menu bar item from inside a real,
signed `.app` bundle of its own, with its own LaunchAgent. The script does
not change.

## Why not a plugin host

Plugin hosts run every script inside one process, so every item they draw is
attributed to the host. Three things follow:

- Menu bar management tools show one entry named after the host, not your
  items, so you cannot arrange or hide them individually.
- Notification identity belongs to the host. Every banner your script asks
  for is filed under the host's name and permission, and one denied prompt
  silences all of them.
- Force-quit kills every item at once.

macOS attributes a process name and a notification identity only to a real,
LaunchServices-registered `.app` bundle. This is the part that is easy to
get wrong, because the failure is silent: an unbundled interpreter can call
the notification APIs successfully, and the system will file every
notification into Notification Center without presenting a banner, without
a sound, and without creating any entry in System Settings that you could
flip. The call returns cleanly and nothing appears. Tricks short of a bundle
(an `Info.plist` beside the interpreter, the framework Python's own
`Python.app` identity) produce exactly that outcome. The objective test is
`UNUserNotificationCenter` `authorizationStatus`, not "the call returned".

menutile therefore builds, per script, a minimal bundle in `~/Applications`:
a copy of a uv-managed static CPython as the executable, an `Info.plist`
with your bundle identifier, an optional icon, and an ad-hoc signature.
Nothing else lives in the bundle, because `codesign` rejects data files
outside `Resources`; the standard library and site-packages are wired in
through `PYTHONHOME` and `PYTHONPATH` in the LaunchAgent, and the renderer
scrubs both variables from every child process so your script and anything
it starts run on a clean interpreter.

The permission a user grants keys on the bundle identifier, so it survives
rebuilds. Changing the identifier resets it.

## Install a tile

Requires macOS 13 or newer and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
git clone https://github.com/ShashankKarpal/menutile.git
cd menutile
scripts/install.sh --name "Disk Free" --script /path/to/diskfree.sh --interval 60
```

That installs a shared package venv at `~/.menutile-venv` (rumps only),
builds `~/Applications/Disk Free.app`, renders
`~/Library/LaunchAgents/com.menutile.diskfree.plist`, boots it, and fails
loudly if the item did not come up. Re-run the same command any time to
repair; add `--rebuild` to recreate the venv.

Options: `--arg VALUE` (repeatable; each becomes one token for your script,
emitted as `--arg=VALUE` so a value that starts with dashes survives),
`--bundle-id` and `--label` to override the defaults, `--icns FILE` for a
Finder icon, `--allow-root DIR` (repeatable) to let `bash=` actions run
executables outside the script's own directory and the system bin dirs.
`scripts/uninstall.sh --name "Disk Free"` removes the tile again.

Before its first install the script checks that no app or process already
carries the name you chose; a second app with the same name or bundle
identifier fights over the bar and the notification permission.

## Dialect

What the renderer understands, parsed tolerantly (unknown parameters are
ignored, never fatal; a tail that does not parse as `key=value` pairs stays
part of the text):

- Title line: text, `color=#rrggbb`, `templateImage=<base64 PNG>`; the
  image-only form `| templateImage=...`.
- Stacked two-line tile: `line2="label" line1color=#hex line2color=#hex`.
  Drawn as an image (non-template so each line keeps its colour), redrawn
  every poll.
- One-shot notifications: extra lines in the title block carrying
  `notify=true ntitle="..." nmsg="..."`. Your script owns the dedupe (emit
  the line once per condition); the renderer presents it under this app's
  identity, with an `osascript` fallback if the native call fails.
- `---` separates the title block from menu blocks and menu blocks from
  each other (rendered as separators).
- Rows: text, `color`, `size`, `refresh=true` (re-runs the script),
  `refresh=true force=true` (re-runs it once with `MENUTILE_FORCE=1` in its
  environment, so a script that caches can skip its cache for a manual
  click; a forced click during a run is queued and runs when that run ends,
  a timer tick during a run is skipped),
  `bash=/path param1=.. paramN=.. terminal=true|false`. Leading `--` marks
  submenu depth, rendered as indentation.
- `bash=` executables must resolve (after `realpath`) under an allowed root:
  the script's own directory, `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`,
  plus any `--allow-root`. Anything else is logged and the row is not
  clickable. The reason: a `bash=` action runs whatever the script printed,
  and scripts often render text that came from somewhere else.
  If your script prints text it did not write (a message, a title, a file
  name), strip `|` from it first: a `|` in that text starts a parameter
  tail, so it could add its own `bash=` action that points at an allowed
  binary such as `/usr/bin/osascript`.

Keep title text ASCII. Under a LaunchAgent the environment has no locale,
and a stray multibyte character in the title line can fail the stdout
decode and kill the render.

## Operating it

- Logs: `~/Library/Logs/menutile/<label>.log`, one timestamped line per
  event (start, script failures, notifications, actions, refused actions).
  A quiet log is a healthy one.
- The script runs in a worker thread with a 120 s timeout; a slow script
  never blocks the bar. A failing script renders `<Name> ?` with the error
  and a Retry row instead of disappearing.
- Script-only changes need no restart: the renderer re-runs the script every
  poll. A rebuilt bundle needs `launchctl bootout` then `bootstrap` of its
  agent, which `install.sh` does; `launchctl kickstart -k` refuses a
  replaced program because launchd pins the code identity it bootstrapped.
- If an item vanishes, run `launchctl list | grep menutile` and read the
  status column. `78` (`EX_CONFIG`) almost always means `PYTHONHOME` points
  at an interpreter uv has removed or moved; the agent died before Python
  ran a line, so the log says nothing. Re-run `install.sh`.

## Tests

```bash
~/.menutile-venv/bin/python3 -m unittest discover -s tests -v
```

`tests/test_parse.py` covers the dialect parser and the action allowlist;
`tests/test_refresh.py` covers forced and queued refreshes without starting
the menu bar. CI runs gitleaks on every push and weekly.

## Design principle

Identity requires a bundle; logic stays in the script. The renderer draws
what the script printed and never reimplements a script's behaviour.

## License

MIT. Copyright 2026 Shashank Karpal.
