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
    o._poll_due = False
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

    def test_timer_tick_is_dropped_while_a_queued_click_is_due(self):
        # Between the worker's end and apply_pending, a timer tick must not
        # start a plain run that overwrites the unapplied result and pushes
        # the click back a run.
        o = make(due=True)
        started = []
        with mock.patch.object(menutile.threading, "Thread") as thread:
            thread.side_effect = lambda *a, **k: started.append(k["args"]) or mock.Mock()
            o.poll()
        self.assertEqual(started, [])
        self.assertFalse(o._refresh_running)

    def test_forced_click_while_a_queued_click_is_due_is_that_click(self):
        # A manual click in the same gap is the due forced run itself: it
        # starts nothing now, and apply_pending shows the waiting result and
        # then starts exactly one forced run.
        o = make(due=True)
        o._pending = ("ok", ["waiting"], [])
        with mock.patch.object(menutile.threading, "Thread") as thread:
            o.poll(force=True)
        thread.assert_not_called()
        self.assertTrue(o._force_due)
        calls = []
        o.poll = lambda _s=None, force=False: calls.append(force)
        o.apply_pending()
        self.assertEqual(o.rendered, [["waiting"]])
        self.assertEqual(calls, [True])

    def test_no_run_starts_while_a_result_waits_to_be_shown(self):
        # A finished run's result waits up to a second for apply_pending. A
        # run started in that wait could overwrite it (and its one-shot
        # notify lines) before it is shown: a tick is dropped, and a forced
        # click becomes the run apply_pending starts.
        o = make()
        o._pending = ("ok", ["waiting"], [])
        with mock.patch.object(menutile.threading, "Thread") as thread:
            o.poll()
            o.poll(force=True)
        thread.assert_not_called()
        self.assertTrue(o._force_due)
        calls = []
        o.poll = lambda _s=None, force=False: calls.append(force)
        o.apply_pending()
        self.assertEqual(o.rendered, [["waiting"]])
        self.assertEqual(calls, [True])

    def test_click_in_the_wait_starts_one_forced_worker_after_apply(self):
        # The whole path with the real poll: a click while a result waits
        # starts nothing, then apply_pending shows the result and starts one
        # forced worker.
        o = make()
        o._pending = ("ok", ["waiting"], [])
        with mock.patch.object(menutile.threading, "Thread") as thread:
            o.poll(force=True)
            thread.assert_not_called()
            o.apply_pending()
        self.assertEqual(o.rendered, [["waiting"]])
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs["args"], (True,))
        self.assertTrue(o._running_forced)
        self.assertFalse(o._force_due)

    def test_plain_poll_in_the_wait_runs_after_apply(self):
        # A timer tick or plain click in the wait is postponed, not dropped:
        # a script whose run time is close to the interval would otherwise
        # lose every other tick, and a "Retry now" click would do nothing.
        o = make()
        o._pending = ("ok", ["waiting"], [])
        with mock.patch.object(menutile.threading, "Thread") as thread:
            o.poll()
            thread.assert_not_called()
            o.apply_pending()
        self.assertEqual(o.rendered, [["waiting"]])
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs["args"], (False,))

    def test_forced_click_wins_over_a_postponed_tick(self):
        o = make()
        o._pending = ("ok", ["waiting"], [])
        with mock.patch.object(menutile.threading, "Thread") as thread:
            o.poll()
            o.poll(force=True)
            o.apply_pending()
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs["args"], (True,))

    def test_render_that_raises_still_runs_the_queued_click(self):
        # apply_pending clears the due mark before it renders, so a render
        # that raises must not lose the click it stands for.
        o = make(due=True)
        o._pending = ("ok", ["x"], [])
        calls = []
        o.poll = lambda _s=None, force=False: calls.append(force)

        def boom(*a):
            raise RuntimeError("render failed")

        o._render = boom
        with self.assertRaises(RuntimeError):
            o.apply_pending()
        self.assertEqual(calls, [True])

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
