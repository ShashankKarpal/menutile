#!/usr/bin/env python3
"""menutile: one menu bar app per status script, with a real identity.

Runs a SwiftBar/xbar-dialect status script on a cadence and renders its
output as a native menu bar item, from inside a real .app bundle, so the
item carries its own name in menu bar tools and owns its notification
identity instead of borrowing a shared host's. Scripts run unchanged.

Supported dialect (the common SwiftBar/xbar subset, parsed
tolerantly): title line with optional `color` and `templateImage`
(base64 PNG); `---` separators; menu lines with `color`, `size`,
`refresh=true` (with `force=true`, that one run gets MENUTILE_FORCE=1 in its
environment, so a script with a cache can skip it; the error menu's
"Retry now" runs the same way), `bash=` with `param1..paramN` and
`terminal=true/false` (only a command listed exactly with --allow-action).
Unknown parameters are ignored, never fatal. A line whose tail does not
parse as parameters is treated as plain text, whole. Only "\\n" ends a line.

Usage (the LaunchAgent passes these):
    <bundle-exe> menutile.py --name "My Tile" \
        --script /path/to/script --arg=--flag --interval 60 \
        [--allow-action "/abs/program arg ..." ...]

Rules this renderer keeps on purpose:
  - child processes get PYTHONHOME/PYTHONPATH scrubbed (_child_env)
  - every failure leaves evidence (title shows ?, menu shows the error,
    stderr carries a timestamped line for the LaunchAgent log)
  - the script runs in a worker thread; the UI thread only applies the
    last finished result, so a slow script never beachballs the bar
"""
import argparse
import base64
import datetime
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading

import rumps
from AppKit import (NSAttributedString, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSImage)


def log(msg):
    """Timestamped evidence on stderr; the LaunchAgent routes it to a file."""
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print("%s %s" % (ts, msg), file=sys.stderr, flush=True)


# The click commands a bash= row may run: exactly the --allow-action entries
# the tile was installed with, each an absolute program path plus its exact
# arguments, as (program, args) pairs. Empty means no row is clickable. Set
# by main(). (Before 2026-11-17 whole folders were allowed, /usr/bin
# included, so a row that outside text slipped into the output could run
# env, sh or osascript; a bare name was checked in one place and run from
# PATH.)
ALLOWED_ACTIONS = ()

# Shells, interpreters and launchers that run another command, by file
# name, case-insensitive (the default macOS volume is). Never allowed, even
# when listed: put the command in a script file of its own and list that.
# With every argument pinned this is a second line of defence, not the gate,
# and the list is best effort (a copy of a shell under another name passes).
_INTERPRETER = re.compile(
    r"^(env|sh|bash|zsh|dash|ksh|mksh|csh|tcsh|fish|osascript|osacompile"
    r"|perl[0-9.]*|ruby[0-9.]*|node|nodejs|deno|bun|python[w0-9.]*"
    r"|php[0-9.]*|lua[0-9.]*|tclsh[0-9.]*|wish[0-9.]*|swift"
    r"|arch|xargs|nohup|nice|time|timeout|gtimeout|sudo|doas|su|login"
    r"|script|caffeinate|xcrun|find|awk|gawk|nawk|mawk|command|expect"
    r"|launchctl|sandbox-exec|ssh|tmux|screen|pwsh|emacs"
    r"|vi|vim|view|ex|nvim|rvim|rview|vimdiff|shortcuts|automator|java"
    r"|rscript|julia|irb|uv|uvx|npx|npm|pipx|make|git)$", re.I)

# A control character (tab and ESC excepted: ESC starts ANSI colour codes
# and ends no line), a Unicode line or paragraph separator, or a bidi
# control. Python's splitlines() ends a line at several of these, which the
# shell scripts that print rows do not; a bidi control can make one row's
# label read like another's.
_SUSPECT = re.compile("[\x00-\x08\x0a-\x1a\x1c-\x1f\x7f-\x9f\u2028\u2029"
                      "\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

# Refused rows already logged in this process, so a tile installed before
# the allow list does not log every refused row on every refresh. Capped,
# because a script can print a different refused row each time.
_REFUSED_LOGGED = set()
_REFUSED_LOG_CAP = 200


def _is_interpreter(path):
    return bool(_INTERPRETER.match(os.path.basename(path)))


def configure_actions(entries):
    """Parse --allow-action values (shell quoting) into (program, args)
    pairs. An entry whose program is not an absolute path, is a shell,
    interpreter or launcher (by its own name or the file it resolves to), or
    holds what no row can carry (" | ", a control character), is logged and
    dropped. Here a program missing now is kept, so a running tile picks it
    up once it exists (its rows stay refused until then); install.sh
    refuses it with runnable(), so a fresh install names a typo at once."""
    out = []
    for entry in entries or []:
        try:
            tokens = shlex.split(entry)
        except ValueError:
            tokens = []
        if not tokens or not os.path.isabs(tokens[0]):
            log("allow-action refused (needs an absolute program path): %r"
                % entry)
            continue
        if any(" | " in t or _SUSPECT.search(t) for t in tokens):
            log("allow-action refused (no row can carry ' | ' or a control "
                "character): %r" % entry)
            continue
        if _is_interpreter(tokens[0]) or _is_interpreter(os.path.realpath(tokens[0])):
            log("allow-action refused (a shell or interpreter; list a script "
                "file instead): %r" % entry)
            continue
        out.append((tokens[0], tuple(tokens[1:])))
    return tuple(out)


def _row_args(params):
    """[param1, ..., paramN], or None when the row's param numbers are not
    exactly 1..N (a gap, param0, param01): such a row would run something
    other than what it shows, so it is refused."""
    keys = [k for k in params if re.match(r"param[0-9]+$", k)]
    want = ["param%d" % n for n in range(1, len(keys) + 1)]
    if sorted(keys) != sorted(want):
        return None
    return [params[k] for k in want]


def _log_refused_once(what, raw, args):
    key = (raw, None if args is None else tuple(args))
    if key in _REFUSED_LOGGED:
        return
    if len(_REFUSED_LOGGED) >= _REFUSED_LOG_CAP:
        if len(_REFUSED_LOGGED) == _REFUSED_LOG_CAP:
            _REFUSED_LOGGED.add(None)
            log("action refused: %d distinct rows logged; not logging more"
                % _REFUSED_LOG_CAP)
        return
    _REFUSED_LOGGED.add(key)
    log("%s: %r %r" % (what, raw, args))


def runnable(program):
    """True when the file a listed program path resolves to exists, is
    executable and is not a shell, interpreter or launcher. One check for
    the renderer (every row, at build and at click) and install.sh."""
    target = os.path.realpath(program)
    return (not _is_interpreter(target) and os.path.isfile(target)
            and os.access(target, os.X_OK))


def _resolve_action(raw, args):
    """(program, args) to run for a bash= row, or None when the row is
    refused. The row must name a listed program path, or the file that path
    resolves to, with exactly that entry's arguments; the file must exist,
    be executable and not be a shell or interpreter. Only listed paths are
    ever resolved: a path from outside text never reaches the file system
    (realpath on /net/<host>/... could make the Mac look that host up). The
    program runs under the path as listed, not its resolved target, because
    a program can act on the name it is started under (/usr/bin/view is
    vim). Checked when the menu is built and again on the click."""
    if args is None or not os.path.isabs(raw):
        return None
    args = tuple(args)
    for program, listed in ALLOWED_ACTIONS:
        if listed != args:
            continue
        if raw not in (program, os.path.realpath(program)):
            continue
        # program's own name was checked in configure_actions; runnable()
        # checks the file it resolves to now (it can change after start).
        if not runnable(program):
            return None
        return program, list(args)
    return None


def _child_env():
    """Subprocess environment without our interpreter's wiring.

    The bundle executable needs PYTHONHOME/PYTHONPATH; any child Python a
    hosted script starts would be poisoned by them."""
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    # Only a manual forced refresh (a force=true row or Retry now) sets
    # this, per run; never inherit it.
    env.pop("MENUTILE_FORCE", None)
    return env


def parse_line(line):
    """Split one output line into (text, params dict).

    The dialect separates text from parameters with ' | '. Text may itself
    contain anything, so the split is only accepted when every token on the
    right is key=value; otherwise the whole line is text. Values may be
    single- or double-quoted (shlex rules). A line with a control or
    line-separator character is all text (each such character shown as
    U+FFFD), because the scripts that print rows do not treat those as line
    ends and outside text (a file name, a title) can carry one."""
    if _SUSPECT.search(line):
        return _SUSPECT.sub("\ufffd", line), {}
    if line.startswith("| "):
        # Image-only line: empty text, everything after the pipe is params
        # (a title that is only a templateImage).
        text, tail = "", line[2:]
    elif " | " in line:
        text, tail = line.rsplit(" | ", 1)
    else:
        return line, {}
    try:
        tokens = shlex.split(tail)
    except ValueError:
        return line, {}
    if not tokens or not all("=" in t for t in tokens):
        return line, {}
    params = {}
    for t in tokens:
        k, v = t.split("=", 1)
        params[k] = v
    return text, params


def parse_output(stdout):
    """Blocks separated by '---' lines; first block is the title. Only "\\n"
    ends a line, as in the shells that print the rows; splitlines() also
    split on \\r, \\v, U+2028 and others, so outside text holding one
    could start a row of its own."""
    blocks, current = [], []
    for raw in stdout.split("\n"):
        # One \r right before the \n is a CRLF line end and is dropped,
        # whatever the other lines use; it cannot start a row. A \r anywhere
        # else makes its line plain text (parse_line).
        if raw.endswith("\r"):
            raw = raw[:-1]
        # strip(" \t"), not strip(): strip() also drops \v, \x1c and
        # U+2028, so outside text like "\v---" would split the menu.
        if raw.strip(" \t") == "---":
            blocks.append(current)
            current = []
        else:
            current.append(raw)
    blocks.append(current)
    title_lines = [l for l in blocks[0] if l.strip(" \t")] if blocks else []
    menu_blocks = [b for b in blocks[1:]]
    return title_lines, menu_blocks


def _nscolor(hexstr):
    h = hexstr.lstrip("#")
    if len(h) != 6:
        raise ValueError("bad color %r" % hexstr)
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


def _attributed(text, color=None, size=None, menubar=False):
    attrs = {}
    if color:
        try:
            attrs[NSForegroundColorAttributeName] = _nscolor(color)
        except ValueError:
            pass
    pt = None
    if size:
        try:
            pt = float(size)
        except ValueError:
            pt = None
    if menubar:
        attrs[NSFontAttributeName] = NSFont.menuBarFontOfSize_(pt or 0)
    else:
        attrs[NSFontAttributeName] = NSFont.menuFontOfSize_(pt or 0)
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


class MenuTile(rumps.App):
    def __init__(self, name, script_cmd, interval):
        super(MenuTile, self).__init__(name, quit_button=None)
        self.app_name = name
        self.script_cmd = script_cmd
        self._pending = None
        self._lock = threading.Lock()
        self._icon_cache = {}
        self._refresh_running = False
        self._running_forced = False
        self._force_queued = False
        self._force_due = False
        self._poll_due = False
        self.poll_timer = rumps.Timer(self.poll, interval)
        self.poll_timer.start()
        self.apply_timer = rumps.Timer(self.apply_pending, 1)
        self.apply_timer.start()
        self.poll(None)

    # ---- worker side ----

    def poll(self, _sender=None, force=False):
        # Check-and-set under the lock: the timer and a queued manual refresh
        # can both arrive here. A forced refresh that lands while a plain run
        # is in flight is queued, not dropped; one that lands during a forced
        # run is already covered by it (a double-click costs one run). A
        # timer tick during a run is dropped. Between a run's end and
        # apply_pending (up to a second), a new run could overwrite the
        # result, and its one-shot notify lines, before it is shown, so a
        # poll in that wait is postponed: apply_pending starts it after the
        # result is shown, forced if any postponed poll was forced (one run).
        with self._lock:
            if self._refresh_running:
                if force and not self._running_forced:
                    self._force_queued = True
                return
            if self._pending is not None or self._force_due or self._poll_due:
                if force:
                    self._force_due = True
                else:
                    self._poll_due = True
                return
            self._refresh_running = True
            self._running_forced = force
        try:
            threading.Thread(target=self._worker, args=(force,), daemon=True).start()
        except Exception as e:  # noqa: BLE001 - a failed start must not freeze polling
            log("worker start failed: %s: %s" % (type(e).__name__, e))
            with self._lock:
                self._refresh_running = False
                self._running_forced = False

    def _worker(self, force=False):
        # The flag is cleared in finally, under the lock: if anything between
        # the thread start and here ever raised past the except, the app
        # would stop polling forever with no visible symptom.
        try:
            try:
                env = _child_env()
                if force:
                    env["MENUTILE_FORCE"] = "1"
                    log("manual refresh (force)")
                # Bytes, not text=True: text mode turns a lone \r into \n
                # before parse_output sees it, so outside text holding one
                # would start a row of its own. UTF-8 with replacement, so a
                # bad byte shows as U+FFFD instead of failing the run (a
                # LaunchAgent has no locale).
                p = subprocess.run(self.script_cmd, capture_output=True,
                                   timeout=120, env=env)
                out = p.stdout.decode("utf-8", "replace")
                errtext = p.stderr.decode("utf-8", "replace")
                if p.returncode != 0 or not out.strip():
                    err = (errtext or out or "no output").strip()
                    log("script rc=%s: %s" % (p.returncode, err[:200]))
                    result = ("error", "rc=%s" % p.returncode, err[:300])
                else:
                    result = ("ok",) + parse_output(out)
            except Exception as e:  # noqa: BLE001 - the bar must survive anything
                log("script raised: %s: %s" % (type(e).__name__, e))
                result = ("error", type(e).__name__, str(e)[:300])
            with self._lock:
                self._pending = result
        finally:
            # A queued forced refresh runs from apply_pending, after this
            # run's result has been shown, so it can never overwrite that
            # result (or its one-shot notify lines) before it is applied.
            with self._lock:
                self._refresh_running = False
                self._running_forced = False
                if self._force_queued:
                    self._force_queued = False
                    self._force_due = True

    # ---- UI side (main thread via rumps timers) ----

    def apply_pending(self, _sender=None):
        with self._lock:
            result, self._pending = self._pending, None
            due, self._force_due = self._force_due, False
            plain_due, self._poll_due = self._poll_due, False
        try:
            if result is not None:
                if result[0] == "ok":
                    self._render(result[1], result[2])
                else:
                    self._render_error(result[1], result[2])
        finally:
            # The flag is already cleared; a render that raises must not
            # swallow the click it stands for.
            if due:
                self.poll(force=True)
            elif plain_due:
                self.poll()

    def _render(self, title_lines, menu_blocks):
        text, params = parse_line(title_lines[0]) if title_lines else ("?", {})
        self._set_title(text.strip(), params)
        # Extra title-block lines may carry one-shot notifications. The
        # hosted script owns dedupe (it only emits a notify line once per
        # condition); the host just presents it, with this app's identity.
        for raw in title_lines[1:]:
            ntext, nparams = parse_line(raw)
            if nparams.get("notify") != "true":
                continue
            ntitle = nparams.get("ntitle", self.app_name)
            nmsg = nparams.get("nmsg", ntext or "")
            try:
                rumps.notification(ntitle, "", nmsg)
                log("notification: %s" % nmsg[:160])
            except Exception as e:  # noqa: BLE001 - banner is best-effort
                log("rumps notification failed (%s); osascript fallback" % e)
                try:
                    safe = lambda s: s.replace("\\", "").replace('"', "'")
                    subprocess.Popen(
                        ["/usr/bin/osascript", "-e",
                         'display notification "%s" with title "%s"'
                         % (safe(nmsg), safe(ntitle))], env=_child_env())
                except Exception as e2:  # noqa: BLE001
                    log("osascript notification failed: %s" % e2)
        items = []
        for i, block in enumerate(menu_blocks):
            if i > 0 and items:
                items.append(rumps.separator)
            for raw in block:
                if not raw.strip():
                    continue
                line = raw
                indent = ""
                while line.startswith("--"):  # submenu depth: render indented
                    line = line[2:]
                    indent += "    "
                btext, bparams = parse_line(line)
                item = rumps.MenuItem(indent + btext,
                                      callback=self._callback_for(bparams))
                color, size = bparams.get("color"), bparams.get("size")
                if color or size:
                    item._menuitem.setAttributedTitle_(
                        _attributed(indent + btext, color, size))
                items.append(item)
        self._set_menu(items)

    def _render_error(self, kind, detail):
        self.title = "%s ?" % self.app_name
        self._set_menu([
            rumps.MenuItem("%s: script failed (%s)" % (self.app_name, kind)),
            rumps.MenuItem(detail[:120]),
            # A forced poll, like a `refresh=true force=true` row (owner
            # 2026-11-02): queued during a plain run, MENUTILE_FORCE=1.
            rumps.MenuItem("Retry now", callback=lambda _s: self.poll(force=True)),
        ])

    def _stacked_image(self, line1, line2, c1, c2):
        """Two-line menu bar tile drawn as an image. Non-template so
        per-line colors survive; redrawn every poll, so a theme change
        corrects within one interval."""
        try:
            col1 = _nscolor(c1) if c1 else NSColor.secondaryLabelColor()
        except ValueError:
            col1 = NSColor.secondaryLabelColor()
        try:
            col2 = _nscolor(c2) if c2 else NSColor.labelColor()
        except ValueError:
            col2 = NSColor.labelColor()
        # Layout: line1 big on top (monospaced digits), line2 small underneath.
        a1 = NSAttributedString.alloc().initWithString_attributes_(
            line1, {NSFontAttributeName:
                    NSFont.monospacedDigitSystemFontOfSize_weight_(10.5, 0.3),
                    NSForegroundColorAttributeName: col1})
        a2 = NSAttributedString.alloc().initWithString_attributes_(
            line2, {NSFontAttributeName: NSFont.systemFontOfSize_weight_(7.5, 0.4),
                    NSForegroundColorAttributeName: col2})
        w = max(a1.size().width, a2.size().width) + 2.0
        img = NSImage.alloc().initWithSize_((w, 22.0))
        img.lockFocus()
        a1.drawAtPoint_(((w - a1.size().width) / 2.0, 9.5))
        a2.drawAtPoint_(((w - a2.size().width) / 2.0, 1.0))
        img.unlockFocus()
        return img

    def _set_title(self, text, params):
        if "line2" in params:
            # Stacked tile: label over numbers, no plain title.
            try:
                img = self._stacked_image(text or "", params["line2"],
                                          params.get("line1color"),
                                          params.get("line2color"))
                self.title = None
                btn = self._nsapp.nsstatusitem.button()
                btn.setImage_(img)
                btn.setTitle_("")
                return
            except Exception as e:  # noqa: BLE001 - fall back to flat text
                log("stacked title failed: %s" % e)
                self.title = "%s %s" % (text, params["line2"])
                return
        img64 = params.get("templateImage")
        if img64:
            self.template = True
            self.icon = self._icon_path(img64)
        self.title = (" " + text) if (img64 and text) else (text or None)
        color = params.get("color")
        try:
            button = self._nsapp.nsstatusitem.button()
            if color and self.title:
                button.setAttributedTitle_(
                    _attributed(button.title(), color, None, menubar=True))
        except Exception:  # noqa: BLE001 - colored title is cosmetic
            pass

    def _icon_path(self, img64):
        """Decode a templateImage once per content hash into a temp PNG,
        because rumps sets icons from file paths."""
        key = hashlib.sha256(img64.encode()).hexdigest()[:16]
        path = self._icon_cache.get(key)
        if path and os.path.exists(path):
            return path
        path = os.path.join(tempfile.gettempdir(),
                            "menutile-%s-%s.png"
                            % (self.app_name.replace(" ", ""), key))
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(img64))
            self._icon_cache[key] = path
            return path
        except Exception as e:  # noqa: BLE001
            log("templateImage decode failed: %s" % e)
            return None

    def _set_menu(self, items):
        items.append(rumps.separator)
        items.append(rumps.MenuItem("Quit %s" % self.app_name,
                                    callback=self._quit))
        self.menu.clear()
        for it in items:
            self.menu.add(it)

    def _callback_for(self, params):
        if params.get("refresh") == "true":
            force = params.get("force") == "true"
            return lambda _s, force=force: self.poll(force=force)
        if "bash" in params:
            # Allow list: a bash= action runs whatever the hosted script
            # printed, and a script may render text that originates
            # elsewhere. Only a command listed exactly with --allow-action
            # may run; anything else is logged and becomes a plain,
            # non-clickable row.
            raw = params["bash"]
            args = _row_args(params)
            if _resolve_action(raw, args) is None:
                _log_refused_once("action refused (not an --allow-action "
                                  "entry)", raw, args)
                return None
            in_terminal = params.get("terminal") == "true"

            def _run_action(_sender, raw=raw, args=args, in_terminal=in_terminal):
                # Checked again on the click: a file can change between the
                # menu build and the click (up to a poll interval).
                resolved = _resolve_action(raw, args)
                if resolved is None:
                    log("action refused at click (not an --allow-action "
                        "entry): %r %r" % (raw, args))
                    return
                # Run the listed path, never the row's raw string.
                cmd = [resolved[0]] + resolved[1]
                log("action: %s terminal=%s" % (cmd, in_terminal))
                try:
                    if in_terminal:
                        shellcmd = " ".join(shlex.quote(c) for c in cmd)
                        script = ('tell application "Terminal"\n'
                                  'activate\ndo script "%s"\nend tell'
                                  % shellcmd.replace('\\', '\\\\')
                                            .replace('"', '\\"'))
                        subprocess.Popen(["/usr/bin/osascript", "-e", script],
                                         env=_child_env())
                    else:
                        subprocess.Popen(cmd, env=_child_env())
                except Exception as e:  # noqa: BLE001
                    log("action failed: %s" % e)
            return _run_action
        return None

    def _quit(self, _sender):
        log("quit via menu")
        rumps.quit_application()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--script", required=True)
    ap.add_argument("--arg", action="append", default=[])
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--allow-action", action="append", default=[],
                    help='one click command a bash= row may run, exactly: '
                         '"/abs/program arg ..." (shell quoting)')
    # Removed 2026-11-17. Still accepted, so an agent installed before then
    # starts (with every click row off) instead of exiting in a loop.
    ap.add_argument("--allow-root", action="append", default=[],
                    help=argparse.SUPPRESS)
    args = ap.parse_args()
    global ALLOWED_ACTIONS
    ALLOWED_ACTIONS = configure_actions(args.allow_action)
    if args.allow_root:
        log("--allow-root is no longer supported and allows nothing "
            "(%d ignored); list each click command with --allow-action, "
            "see the README section Upgrading" % len(args.allow_root))
    log("start %s script=%s interval=%ss actions=%d"
        % (args.name, args.script, args.interval, len(ALLOWED_ACTIONS)))
    MenuTile(args.name, [args.script] + args.arg, args.interval).run()


if __name__ == "__main__":
    main()
