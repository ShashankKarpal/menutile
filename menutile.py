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
environment, so a script with a cache can skip it), `bash=` with
`param1..paramN` and `terminal=true/false`.
Unknown parameters are ignored, never fatal. A line whose tail does not
parse as parameters is treated as plain text, whole.

Usage (the LaunchAgent passes these):
    <bundle-exe> menutile.py --name "My Tile" \
        --script /path/to/script --arg=--flag --interval 60 \
        [--allow-root DIR ...]

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


# Executable roots a menu action may launch: the hosted script's own
# directory plus the system and Homebrew bin dirs, extended with --allow-root.
# realpath is compared, so a symlink under a listed root that points
# elsewhere is refused too. Set by main().
ACTION_ROOTS = ()


def action_roots(script_path, extra):
    roots = [os.path.dirname(script_path), "/usr/bin", "/usr/local/bin",
             "/opt/homebrew/bin"] + list(extra or [])
    return tuple(os.path.realpath(os.path.expanduser(r)) + "/" for r in roots)


def _child_env():
    """Subprocess environment without our interpreter's wiring.

    The bundle executable needs PYTHONHOME/PYTHONPATH; any child Python a
    hosted script starts would be poisoned by them."""
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    # Only a manual forced refresh sets this, per run; never inherit it.
    env.pop("MENUTILE_FORCE", None)
    return env


def parse_line(line):
    """Split one output line into (text, params dict).

    The dialect separates text from parameters with ' | '. Text may itself
    contain anything, so the split is only accepted when every token on the
    right is key=value; otherwise the whole line is text. Values may be
    single- or double-quoted (shlex rules)."""
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
    """Blocks separated by '---' lines; first block is the title."""
    blocks, current = [], []
    for raw in stdout.splitlines():
        if raw.strip() == "---":
            blocks.append(current)
            current = []
        else:
            current.append(raw)
    blocks.append(current)
    title_lines = [l for l in blocks[0] if l.strip()] if blocks else []
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
                p = subprocess.run(self.script_cmd, capture_output=True,
                                   text=True, timeout=120, env=env)
                if p.returncode != 0 or not p.stdout.strip():
                    err = (p.stderr or p.stdout or "no output").strip()
                    log("script rc=%s: %s" % (p.returncode, err[:200]))
                    result = ("error", "rc=%s" % p.returncode, err[:300])
                else:
                    result = ("ok",) + parse_output(p.stdout)
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
            rumps.MenuItem("Retry now", callback=lambda _s: self.poll()),
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
            # Allowlist: a bash= action runs whatever the hosted script
            # printed, and a script may render text that originates
            # elsewhere. Only executables under the allowed roots may run;
            # anything else is logged and becomes a plain, non-clickable row.
            exe = os.path.realpath(os.path.expanduser(params["bash"]))
            if not exe.startswith(ACTION_ROOTS):
                log("action refused (outside allowlist): %s" % exe)
                return None
            cmd = [params["bash"]]
            n = 1
            while "param%d" % n in params:
                cmd.append(params["param%d" % n])
                n += 1
            in_terminal = params.get("terminal") == "true"

            def _run_action(_sender, cmd=cmd, in_terminal=in_terminal):
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
    ap.add_argument("--allow-root", action="append", default=[],
                    help="extra directory whose executables bash= actions may run")
    args = ap.parse_args()
    global ACTION_ROOTS
    ACTION_ROOTS = action_roots(args.script, args.allow_root)
    log("start %s script=%s interval=%ss"
        % (args.name, args.script, args.interval))
    MenuTile(args.name, [args.script] + args.arg, args.interval).run()


if __name__ == "__main__":
    main()
