"""Parser tests for menutile.py: the SwiftBar/xbar dialect subset, on inline
fixtures. No menu bar, no launchd, no AppKit needed for the pure functions
(rumps and AppKit are imported by the module, so run these with the package
venv or any Python that has rumps installed).

    ~/.menutile-venv/bin/python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from menutile import action_roots, parse_line, parse_output, _child_env  # noqa: E402


class ParseLine(unittest.TestCase):
    def test_plain_text_has_no_params(self):
        self.assertEqual(parse_line("Hello world"), ("Hello world", {}))

    def test_title_with_color_and_template_image(self):
        text, params = parse_line("13% | color=#ef4444 templateImage=aGVsbG8=")
        self.assertEqual(text, "13%")
        self.assertEqual(params, {"color": "#ef4444", "templateImage": "aGVsbG8="})

    def test_image_only_title(self):
        text, params = parse_line("| templateImage=aGVsbG8=")
        self.assertEqual(text, "")
        self.assertEqual(params["templateImage"], "aGVsbG8=")

    def test_stacked_title_params(self):
        text, params = parse_line('96.31 | line2="kk1" line1color=#ff9f0a line2color=#ffffff')
        self.assertEqual(text, "96.31")
        self.assertEqual(params["line2"], "kk1")
        self.assertEqual(params["line1color"], "#ff9f0a")

    def test_bash_action_with_params_and_terminal(self):
        text, params = parse_line("Open log | bash=/usr/bin/open param1=-t param2=/tmp/x.log terminal=false")
        self.assertEqual(text, "Open log")
        self.assertEqual(params["bash"], "/usr/bin/open")
        self.assertEqual(params["param1"], "-t")
        self.assertEqual(params["terminal"], "false")

    def test_tail_that_is_not_params_stays_text(self):
        line = "resets at 18:29 | in 2h 10m"
        self.assertEqual(parse_line(line), (line, {}))

    def test_quoted_values_follow_shlex(self):
        text, params = parse_line('Row | nmsg="Fable at 94% used. Slow down." notify=true')
        self.assertEqual(params["nmsg"], "Fable at 94% used. Slow down.")
        self.assertEqual(params["notify"], "true")

    def test_unbalanced_quote_is_text(self):
        line = 'Odd | nmsg="unterminated'
        self.assertEqual(parse_line(line), (line, {}))

    def test_last_pipe_wins_when_text_contains_pipes(self):
        text, params = parse_line("a | b | color=#fff")
        self.assertEqual(text, "a | b")
        self.assertEqual(params, {"color": "#fff"})


class ParseOutput(unittest.TestCase):
    OUT = "\n".join([
        "13% | color=#22c55e",
        "| notify=true ntitle=\"Tile\" nmsg=\"once\"",
        "---",
        "Section one | size=13",
        "row a",
        "",
        "---",
        "row b | refresh=true",
        "--sub row | color=#9ca3af",
    ])

    def test_blocks_split_on_separator_lines(self):
        title_lines, menu_blocks = parse_output(self.OUT)
        self.assertEqual(title_lines, ["13% | color=#22c55e",
                                       "| notify=true ntitle=\"Tile\" nmsg=\"once\""])
        self.assertEqual(len(menu_blocks), 2)
        self.assertEqual(menu_blocks[0], ["Section one | size=13", "row a", ""])
        self.assertEqual(menu_blocks[1], ["row b | refresh=true", "--sub row | color=#9ca3af"])

    def test_no_separator_means_title_only(self):
        title_lines, menu_blocks = parse_output("just a title\n")
        self.assertEqual(title_lines, ["just a title"])
        self.assertEqual(menu_blocks, [])

    def test_blank_title_lines_dropped(self):
        title_lines, _ = parse_output("\n\n  \n42\n---\nx")
        self.assertEqual(title_lines, ["42"])


class Environment(unittest.TestCase):
    def test_child_env_scrubs_interpreter_wiring(self):
        os.environ["PYTHONHOME"] = "/x"
        os.environ["PYTHONPATH"] = "/y"
        try:
            env = _child_env()
        finally:
            del os.environ["PYTHONHOME"]
            del os.environ["PYTHONPATH"]
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertIn("PATH", env)

    def test_action_roots_include_script_dir_and_extras(self):
        roots = action_roots("/opt/tiles/my.sh", ["~/bin"])
        self.assertIn(os.path.realpath("/opt/tiles") + "/", roots)
        self.assertIn(os.path.realpath(os.path.expanduser("~/bin")) + "/", roots)
        self.assertIn(os.path.realpath("/usr/bin") + "/", roots)
        self.assertTrue(all(r.endswith("/") for r in roots))


if __name__ == "__main__":
    unittest.main()
