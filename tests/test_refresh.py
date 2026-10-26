"""Refresh-path tests for menutile.py: `refresh=true force=true` and the
queued click. Builds a MenuTile without starting rumps (no menu bar, no
timers) and drives poll, _worker, apply_pending and _callback_for directly
against a tiny script that echoes whether MENUTILE_FORCE reached it.

    ~/.menutile-venv/bin/python3 -m unittest discover -s tests -v

(any Python with rumps installed works; the venv is the one install.sh
creates)
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import menutile  # noqa: E402

CMD = ["/bin/sh", "-c", 'echo "F${MENUTILE_FORCE:-0}"']


def make(running=False, forced=False, queued=False, due=False):
    o = menutile.MenuTile.__new__(menutile.MenuTile)
    o.app_name = "test"
    o.script_cmd = CMD
    o._pending = None
    o._lock = threading.Lock()
    o._icon_cache = {}
    o._refresh_running = running
    o._running_forced = forced
    o._force_queued = queued
    o._force_due = due
    o.rendered = []
    o._render = lambda title, blocks: o.rendered.append(title)
    o._render_error = lambda a, b: o.rendered.append(("error", a))
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
        before = os.environ.get("MENUTILE_FORCE")
        o = make(running=True, forced=True)
        o._worker(True)
        self.assertEqual(title_of(o), "F1")
        self.assertEqual(os.environ.get("MENUTILE_FORCE"), before)

    def test_host_env_value_never_forces_a_timer_run(self):
        o = make(running=True)
        with mock.patch.dict(os.environ, {"MENUTILE_FORCE": "1"}):
            o._worker(False)
        self.assertEqual(title_of(o), "F0")

    def test_click_during_a_plain_run_is_queued(self):
        o = make(running=True)
        o.poll(force=True)
        self.assertTrue(o._force_queued)

    def test_click_during_a_forced_run_is_not_queued_again(self):
        o = make(running=True, forced=True)
        o.poll(force=True)
        self.assertFalse(o._force_queued)

    def test_timer_tick_during_a_run_is_dropped(self):
        o = make(running=True)
        o.poll()
        self.assertFalse(o._force_queued)

    def test_queued_click_runs_only_after_the_result_is_shown(self):
        o = make(running=True, queued=True)
        calls = []
        o.poll = lambda _s=None, force=False: calls.append(force)
        o._worker(False)
        self.assertEqual(calls, [], "the worker must not re-poll before apply")
        self.assertTrue(o._force_due)
        o.apply_pending()
        self.assertEqual(o.rendered, [["F0"]])
        self.assertEqual(calls, [True])
        self.assertFalse(o._force_due)

    def test_idle_forced_poll_runs_the_worker_forced(self):
        o = make()
        o.poll(force=True)
        deadline = time.time() + 10
        while o._refresh_running and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(title_of(o), "F1")
        self.assertFalse(o._running_forced)

    def test_failed_thread_start_never_freezes_polling(self):
        o = make()
        with mock.patch.object(menutile.threading, "Thread", side_effect=RuntimeError("no threads")):
            o.poll(force=True)
        self.assertFalse(o._refresh_running)
        self.assertFalse(o._running_forced)

    def test_callback_maps_force_param(self):
        o = make()
        calls = []
        o.poll = lambda _s=None, force=False: calls.append(force)
        o._callback_for({"refresh": "true", "force": "true"})(None)
        o._callback_for({"refresh": "true"})(None)
        self.assertEqual(calls, [True, False])


if __name__ == "__main__":
    unittest.main()
