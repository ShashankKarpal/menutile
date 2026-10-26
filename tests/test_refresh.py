"""Refresh-path tests for menutile.py: `refresh=true force=true` and the
queued click. Builds a MenuTile without starting rumps (no menu bar, no
timers) and drives poll, _worker and _callback_for directly against a tiny
script that echoes whether MENUTILE_FORCE reached it.

    ~/.menutile-venv/bin/python3 -m unittest discover -s tests -v
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import menutile  # noqa: E402

CMD = ["/bin/sh", "-c", 'echo "F${MENUTILE_FORCE:-0}"']


def make(running=False, queued=False):
    o = menutile.MenuTile.__new__(menutile.MenuTile)
    o.app_name = "test"
    o.script_cmd = CMD
    o._pending = None
    o._lock = threading.Lock()
    o._icon_cache = {}
    o._refresh_running = running
    o._force_queued = queued
    return o


def title_of(o):
    r = o._pending
    return r[1][0] if r and r[0] == "ok" and r[1] else repr(r)


class ForcedRefresh(unittest.TestCase):
    def test_timer_run_sees_no_force(self):
        o = make(running=True)
        o._worker(False)
        self.assertEqual(title_of(o), "F0")
        self.assertFalse(o._refresh_running)

    def test_forced_run_sees_force_and_host_env_stays_clean(self):
        o = make(running=True)
        o._worker(True)
        self.assertEqual(title_of(o), "F1")
        self.assertNotIn("MENUTILE_FORCE", os.environ)

    def test_click_during_a_run_is_queued(self):
        o = make(running=True)
        o.poll(force=True)
        self.assertTrue(o._force_queued)

    def test_timer_tick_during_a_run_is_dropped(self):
        o = make(running=True)
        o.poll()
        self.assertFalse(o._force_queued)

    def test_queued_click_re_polls_forced_after_the_run(self):
        o = make(running=True, queued=True)
        calls = []
        o.poll = lambda _s=None, force=False: calls.append(force)
        o._worker(False)
        self.assertEqual(calls, [True])
        self.assertFalse(o._force_queued)

    def test_callback_maps_force_param(self):
        o = make()
        calls = []
        o.poll = lambda _s=None, force=False: calls.append(force)
        o._callback_for({"refresh": "true", "force": "true"})(None)
        o._callback_for({"refresh": "true"})(None)
        self.assertEqual(calls, [True, False])


if __name__ == "__main__":
    unittest.main()
