"""Click-action tests for menutile.py: the exact --allow-action list and the
newline-only split. subprocess.Popen is swapped for a recorder in every test
that clicks, so nothing runs here, not even on code without the gate.

    ~/.menutile-venv/bin/python3 -m unittest discover -s tests -v

(any Python with rumps installed works; the venv is the one install.sh
creates)
"""
import os
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import menutile  # noqa: E402

REAL_POPEN = menutile.subprocess.Popen

# Every line break Python's str.splitlines() honours, other than "\n".
BREAKS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
          "\u2028", "\u2029"]


class _Item(object):
    """Stands in for rumps.MenuItem: keeps the title and the callback."""

    class _NS(object):
        def setAttributedTitle_(self, _t):
            pass

    def __init__(self, title, callback=None, **_k):
        self.title, self.callback, self._menuitem = title, callback, _Item._NS()


def make():
    o = menutile.MenuTile.__new__(menutile.MenuTile)
    o.app_name = "test"
    o.script_cmd = ["/bin/true"]
    o._pending = None
    o._lock = threading.Lock()
    o._icon_cache = {}
    o._refresh_running = False
    o._running_forced = False
    o._force_queued = False
    o._force_due = False
    o._poll_due = False
    return o


def write_exe(path, body="#!/bin/sh\nexit 0\n"):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="menutile-actions-"))
        self.helper = write_exe(os.path.join(self.tmp, "helper.sh"))
        self.calls = []
        p = mock.patch.object(menutile.subprocess, "Popen",
                              side_effect=lambda cmd, *a, **k: self.calls.append(cmd))
        p.start()
        self.addCleanup(p.stop)
        self.saved = {"ALLOWED_ACTIONS": menutile.ALLOWED_ACTIONS}
        self.addCleanup(self._restore)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _restore(self):
        for k, v in self.saved.items():
            setattr(menutile, k, v)

    def allow(self, entries):
        """Set up the tile's allow list as main() does."""
        menutile.ALLOWED_ACTIONS = menutile.configure_actions(entries)

    def callback_for(self, params):
        return make()._callback_for(params)

    def render_rows(self, stdout):
        o = make()
        shown = []
        o._set_menu = shown.extend
        o._set_title = lambda *a: None
        with mock.patch.object(menutile.rumps, "MenuItem", _Item):
            menutile.MenuTile._render(o, *menutile.parse_output(stdout))
        return [r for r in shown if isinstance(r, _Item)]

    def click_all(self, rows):
        del self.calls[:]
        for r in rows:
            if r.callback is not None:
                r.callback(None)
        return list(self.calls)


class CraftedRows(Base):
    PAYLOADS = [
        'bash=sh param1=-c param2="echo PROBE"',
        'bash=curl param1=https://example.invalid/',
        'bash=/usr/bin/env param1=sh param2=-c param3="echo PROBE"',
        'bash=/usr/bin/osascript param1=-e param2=beep',
        'bash=/bin/sh param1=-c param2="echo PROBE"',
        'bash=/bin/zsh param1=-c param2="echo PROBE"',
        'bash=/usr/bin/python3 param1=-c param2=1',
        'bash=/usr/bin/perl param1=-e param2=1',
        'bash=/usr/bin/ruby param1=-e param2=1',
        'bash=/opt/homebrew/bin/node param1=-e param2=1',
        'bash=/usr/bin/open param1=-a param2=Terminal',
        'bash=/usr/bin/curl param1=https://example.invalid/',
    ]

    def test_a_default_tile_runs_no_crafted_row(self):
        self.allow([])
        out = "T\n---\n" + "".join("Update ready | %s\n" % p for p in self.PAYLOADS)
        out += "Script-folder file | bash=%s\n" % self.helper
        rows = self.render_rows(out)
        # Control: every row reached the parser with its bash= intact, so a
        # pass below is the gate's doing, not the parser's.
        self.assertEqual(len(rows), len(self.PAYLOADS) + 1)
        for r in rows:
            self.assertNotIn("|", r.title)
        self.assertEqual(self.click_all(rows), [])

    def test_a_line_break_in_outside_text_cannot_start_a_listed_row(self):
        # The tile lists helper.sh with no arguments. Outside text (a mail
        # subject, a file name) carries a break and a whole row of its own.
        self.allow([self.helper])
        ran = []
        for br in BREAKS:
            out = "T\n---\nNews: hello%sRun it | bash=%s\n" % (br, self.helper)
            if self.click_all(self.render_rows(out)):
                ran.append(repr(br))
        self.assertEqual(ran, [])
        # Control: with a real newline the injected row does reach the gate
        # and runs, so the passes above come from the split.
        out = "T\n---\nNews: hello\nRun it | bash=%s\n" % self.helper
        self.assertEqual(self.click_all(self.render_rows(out)), [[self.helper]])


class ExactList(Base):
    def test_interpreters_are_refused_even_when_listed(self):
        link = os.path.join(self.tmp, "looks-fine.sh")
        os.symlink("/bin/sh", link)
        entries = ["/usr/bin/env sh -c 'echo x'", "/bin/sh -c 'echo x'",
                   "/bin/bash", "/bin/zsh -c x", "/usr/bin/osascript -e beep",
                   "/usr/bin/python3 -c 1", "/usr/bin/perl -e 1",
                   "/usr/bin/ruby -e 1", "/BIN/SH -c x", link,
                   "/usr/bin/arch sh -c x", "/usr/bin/xargs -0 sh -c x",
                   "/usr/bin/nohup /bin/sh", "/usr/bin/nice /bin/sh",
                   "/usr/bin/time /bin/sh", "/usr/bin/sudo /bin/sh",
                   "/usr/bin/script -q /dev/null", "/usr/bin/awk BEGIN{}",
                   "/usr/bin/find . -exec sh ;", "/usr/bin/xcrun swift",
                   "/usr/bin/view /etc/hosts", "/usr/bin/vim -c q",
                   "/usr/bin/expect x.exp", "/bin/launchctl list",
                   "/usr/bin/sandbox-exec -n x /bin/ls", "/usr/bin/ssh host ls",
                   "/opt/homebrew/bin/tmux new", "/usr/bin/screen",
                   "/usr/local/bin/pwsh", "/usr/bin/command ls",
                   "/usr/bin/shortcuts run Foo", "/usr/bin/git status",
                   "/opt/homebrew/bin/uv run x.py"]
        for p in ("/opt/homebrew/bin/python3", "/opt/homebrew/bin/node",
                  "/opt/homebrew/bin/bash"):
            if os.path.exists(p):
                entries.append(p)
        self.allow(entries)
        passed = []
        for e in entries:
            toks = shlex.split(e)
            params = {"bash": toks[0]}
            params.update(("param%d" % (i + 1), a) for i, a in enumerate(toks[1:]))
            if self.callback_for(params) is not None:
                passed.append(e)
        self.assertEqual(passed, [])
        self.assertEqual(menutile.ALLOWED_ACTIONS, ())

    def test_a_listed_action_runs_the_listed_path_with_its_exact_args(self):
        # The path as listed runs, not its resolved target: a program can
        # act on the name it is started under (/usr/bin/view is vim).
        alias = os.path.join(self.tmp, "alias.sh")
        os.symlink(self.helper, alias)
        self.allow(["%s --today" % alias])
        for row_path in (alias, self.helper):
            del self.calls[:]
            cb = self.callback_for({"bash": row_path, "param1": "--today"})
            self.assertIsNotNone(cb, row_path)
            cb(None)
            self.assertEqual(self.calls, [[alias, "--today"]])

    def test_a_listed_program_with_other_arguments_is_refused(self):
        self.allow(["%s --today" % self.helper])
        bad = [{"bash": self.helper},
               {"bash": self.helper, "param1": "--other"},
               {"bash": self.helper, "param1": "--today", "param2": "x"},
               {"bash": self.helper, "param1": "--TODAY"}]
        passed = [b for b in bad if self.callback_for(b) is not None]
        self.assertEqual(passed, [])

    def test_a_row_with_a_gap_in_its_param_numbers_is_refused(self):
        # param3 after a missing param2 would be dropped by the old reading,
        # so the row would run fewer arguments than it shows.
        self.allow(["%s --dry-run" % self.helper])
        self.assertIsNone(self.callback_for({"bash": self.helper,
                                             "param1": "--dry-run",
                                             "param3": "--really"}))
        self.assertIsNotNone(self.callback_for({"bash": self.helper,
                                                "param1": "--dry-run"}))

    def test_relative_tilde_and_bare_names_are_refused(self):
        self.allow([self.helper])
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            passed = [b for b in ("helper.sh", "./helper.sh", "~/helper.sh")
                      if self.callback_for({"bash": b}) is not None]
        finally:
            os.chdir(cwd)
        self.assertEqual(passed, [])

    def test_an_unlisted_row_path_never_reaches_the_file_system(self):
        # realpath on a path from outside text could make the Mac look up a
        # host (/net automounts). Only listed paths are resolved.
        self.allow([self.helper])
        seen = []
        real = os.path.realpath
        with mock.patch.object(menutile.os.path, "realpath",
                               side_effect=lambda p: seen.append(p) or real(p)):
            for b in ("/net/example.invalid/x", self.tmp + "/../" +
                      os.path.basename(self.tmp) + "/helper.sh"):
                self.assertIsNone(self.callback_for({"bash": b}))
        self.assertEqual([p for p in seen if p != self.helper], [])

    def test_a_listed_program_that_is_missing_or_a_folder_is_refused(self):
        gone = os.path.join(self.tmp, "gone.sh")
        folder = os.path.join(self.tmp, "folder")
        os.mkdir(folder)
        plain = os.path.join(self.tmp, "not-executable.sh")
        with open(plain, "w") as f:
            f.write("#!/bin/sh\n")
        self.allow([gone, folder, plain])
        passed = [b for b in (gone, folder, plain)
                  if self.callback_for({"bash": b}) is not None]
        self.assertEqual(passed, [])

    def test_a_program_swapped_after_the_menu_was_built_runs_nothing(self):
        self.allow([self.helper])
        cb = self.callback_for({"bash": self.helper})
        self.assertIsNotNone(cb)
        os.unlink(self.helper)
        os.symlink("/bin/sh", self.helper)
        cb(None)
        self.assertEqual(self.calls, [])

    def test_terminal_true_runs_the_quoted_listed_path(self):
        alias = os.path.join(self.tmp, "alias.sh")
        os.symlink(self.helper, alias)
        self.allow([alias])
        cb = self.callback_for({"bash": alias, "terminal": "true"})
        self.assertIsNotNone(cb)
        cb(None)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "/usr/bin/osascript")
        self.assertIn(shlex.quote(alias), self.calls[0][2])

    def test_an_entry_no_row_could_ever_match_is_refused(self):
        entries = ["%s 'a | b'" % self.helper, "%s 'a\x07b'" % self.helper,
                   "%s 'a\u2028b'" % self.helper]
        self.assertEqual(menutile.configure_actions(entries), ())

    def test_shell_quoting_in_an_entry_allows_spaces(self):
        d = os.path.join(self.tmp, "My Tiles")
        os.mkdir(d)
        h = write_exe(os.path.join(d, "open log.sh"))
        self.allow(["'%s' 'two words'" % h])
        self.assertIsNotNone(self.callback_for({"bash": h, "param1": "two words"}))
        self.assertIsNone(self.callback_for({"bash": h, "param1": "two",
                                             "param2": "words"}))


class RealOutput(Base):
    """Through the renderer's own read of a real script's output, not hand
    made strings: text mode would turn a lone \r into \n before the parser
    sees it."""

    def run_script(self, body):
        script = write_exe(os.path.join(self.tmp, "tile.sh"), body)
        o = make()
        o.script_cmd = [script]
        o._refresh_running = True
        with mock.patch.object(menutile.subprocess, "Popen", REAL_POPEN):
            o._worker(False)
        return o._pending

    def test_a_cr_from_outside_text_cannot_start_a_listed_row(self):
        self.allow([self.helper])
        result = self.run_script(
            "#!/bin/sh\nprintf 'T\\n---\\nNews: x\\rRun | bash=%s\\n'\n"
            % self.helper)
        self.assertEqual(result[0], "ok", result)
        o = make()
        shown = []
        o._set_menu = shown.extend
        o._set_title = lambda *a: None
        with mock.patch.object(menutile.rumps, "MenuItem", _Item):
            menutile.MenuTile._render(o, result[1], result[2])
        rows = [r for r in shown if isinstance(r, _Item)]
        self.assertEqual(len(rows), 1, [r.title for r in rows])
        self.assertEqual(self.click_all(rows), [])

    def test_crlf_output_from_a_real_script_parses(self):
        result = self.run_script(
            "#!/bin/sh\nprintf 'T\\r\\n---\\r\\nA | refresh=true\\r\\n'\n")
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(result[1], ["T"])
        self.assertEqual(menutile.parse_line(result[2][0][0]),
                         ("A", {"refresh": "true"}))

    def test_bytes_that_are_not_utf8_show_instead_of_failing(self):
        result = self.run_script("#!/bin/sh\nprintf 'T \\377\\n---\\nA\\n'\n")
        self.assertEqual(result[0], "ok", result)
        self.assertEqual(result[1], ["T \ufffd"])


class Logging(Base):
    def test_a_refused_row_is_logged_once_not_on_every_refresh(self):
        self.allow([])
        lines = []
        out = "T\n---\nOld row | bash=%s param1=x\n" % self.helper
        with mock.patch.object(menutile, "log", side_effect=lines.append):
            for _ in range(3):
                self.render_rows(out)
        self.assertEqual(len([l for l in lines if "refused" in l]), 1, lines)


class CommandLine(Base):
    def run_main(self, argv):
        lines = []
        with mock.patch.object(sys, "argv", ["menutile.py"] + argv), \
                mock.patch.object(menutile, "MenuTile") as tile, \
                mock.patch.object(menutile, "log", side_effect=lines.append):
            menutile.main()
        self.assertTrue(tile.return_value.run.called)
        return lines

    def test_allow_root_is_ignored_with_one_log_line(self):
        lines = self.run_main(["--name", "T", "--script",
                               os.path.join(self.tmp, "tile.sh"),
                               "--allow-root", "/usr/bin",
                               "--allow-root", self.tmp])
        self.assertEqual(menutile.ALLOWED_ACTIONS, ())
        notes = [l for l in lines if "--allow-root" in l]
        self.assertEqual(len(notes), 1, lines)
        self.assertIsNone(self.callback_for({"bash": self.helper}))

    def test_allow_action_entries_reach_the_gate(self):
        self.run_main(["--name", "T", "--script",
                       os.path.join(self.tmp, "tile.sh"),
                       "--allow-action", "%s --today" % self.helper,
                       "--allow-action", "/usr/bin/env sh -c x"])
        self.assertEqual(menutile.ALLOWED_ACTIONS,
                         ((self.helper, ("--today",)),))


class Split(unittest.TestCase):
    def test_only_newline_splits_and_a_line_with_another_break_keeps_no_params(self):
        bad = []
        for s in BREAKS + ["\x00", "\x07"]:
            out = "T\n---\nlabel%sOpen | bash=/usr/bin/open param1=-a param2=Terminal\n" % s
            _, blocks = menutile.parse_output(out)
            rows = [l for b in blocks for l in b if l.strip()]
            if len(rows) != 1 or menutile.parse_line(rows[0])[1]:
                bad.append(repr(s))
        self.assertEqual(bad, [])

    def test_normal_output_parses_as_before(self):
        normal = "Title | color=#ffffff\n---\nA | refresh=true\n--B | color=#888888 size=11\n---\nC\n"
        title, blocks = menutile.parse_output(normal)
        self.assertEqual(title, ["Title | color=#ffffff"])
        self.assertEqual([[l for l in b if l] for b in blocks],
                         [["A | refresh=true", "--B | color=#888888 size=11"], ["C"]])

    def test_crlf_output_parses_like_lf_output(self):
        normal = "Title | color=#ffffff\n---\nA | refresh=true\n---\nC\n"
        title, blocks = menutile.parse_output(normal.replace("\n", "\r\n"))
        self.assertEqual(title, ["Title | color=#ffffff"])
        self.assertEqual(menutile.parse_line(blocks[0][0]), ("A", {"refresh": "true"}))
        self.assertEqual(len(blocks), 2)

    def test_a_separator_line_with_a_control_character_does_not_split(self):
        _, blocks = menutile.parse_output("T\n---\nA\n\x0b---\n\u2028---\nB\n")
        self.assertEqual(len(blocks), 1)

    def test_mixed_line_endings_keep_their_separators(self):
        # A tool that writes CRLF, then a plain echo: every "---\r" is still
        # a separator, because one trailing \r before \n is always dropped.
        title, blocks = menutile.parse_output(
            "T\r\n---\r\nA | refresh=true\r\n---\r\nB\n")
        self.assertEqual(title, ["T"])
        self.assertEqual([[l for l in b if l] for b in blocks],
                         [["A | refresh=true"], ["B"]])

    def test_a_cr_inside_a_line_still_makes_it_plain_text(self):
        _, blocks = menutile.parse_output(
            "T\n---\nlabel\rRun | bash=/usr/bin/open param1=-a param2=T\r\nB\n")
        self.assertEqual(menutile.parse_line(blocks[0][0])[1], {})

    def test_an_ansi_colour_code_keeps_the_row_params(self):
        self.assertEqual(
            menutile.parse_line("build \x1b[32mok\x1b[0m | refresh=true")[1],
            {"refresh": "true"})

    def test_a_bidi_control_makes_a_row_plain_text(self):
        bad = [repr(c) for c in ("\u202e", "\u2066", "\u200f", "\u061c")
               if menutile.parse_line("Edit%s | refresh=true" % c)[1]]
        self.assertEqual(bad, [])

    def test_a_tab_keeps_its_parameters(self):
        self.assertEqual(menutile.parse_line("next:\tx | color=#888888 size=11"),
                         ("next:\tx", {"color": "#888888", "size": "11"}))


if __name__ == "__main__":
    unittest.main()
