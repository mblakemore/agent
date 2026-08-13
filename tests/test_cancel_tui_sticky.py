"""Tests for Fix E of plan/live-input-display-fixes.md (defect #5:
escape interrupt unreliable): sticky cancel flag under tui_mode, and the
eager double-escape keybinding."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cancel


class TestStickyCancelFlagInTuiMode(unittest.TestCase):
    def tearDown(self):
        cancel.reset()
        cancel.set_tui_mode(False)

    def test_tui_mode_preserves_pending_cancel(self):
        """An esc-esc that lands BETWEEN cancellable regions must survive
        the next region's entry — per-region reset was erasing it."""
        cancel.set_tui_mode(True)
        cancel.request_cancel()
        with cancel.cancellable():
            self.assertTrue(cancel.is_cancelled())

    def test_blocking_mode_still_resets_per_region(self):
        cancel.set_tui_mode(False)
        cancel.request_cancel()
        with mock.patch.object(cancel.sys.stdin, "isatty", return_value=False):
            with cancel.cancellable():
                self.assertFalse(cancel.is_cancelled())

    def test_explicit_reset_clears(self):
        cancel.set_tui_mode(True)
        cancel.request_cancel()
        cancel.reset()
        with cancel.cancellable():
            self.assertFalse(cancel.is_cancelled())


class TestEagerDoubleEscapeBinding(unittest.TestCase):
    def setUp(self):
        import tui
        if not tui._AVAILABLE:
            self.skipTest("prompt_toolkit not installed")
        self.tui = tui

    def _escape_handler(self, on_cancel):
        from prompt_toolkit.keys import Keys
        kb = self.tui._build_key_bindings(enable_cancel=True, on_cancel=on_cancel)
        for b in kb.bindings:
            if tuple(b.keys) == (Keys.Escape,):
                return b.handler
        self.fail("no eager escape binding registered")

    def _event(self, complete_state=None):
        ev = mock.Mock()
        ev.current_buffer.complete_state = complete_state
        return ev

    def test_double_press_within_window_fires(self):
        fired = []
        handler = self._escape_handler(lambda: fired.append(1))
        with mock.patch.object(self.tui.time, "monotonic",
                               side_effect=[10.0, 10.3]):
            handler(self._event())
            handler(self._event())
        self.assertEqual(fired, [1])

    def test_slow_presses_do_not_fire(self):
        fired = []
        handler = self._escape_handler(lambda: fired.append(1))
        with mock.patch.object(self.tui.time, "monotonic",
                               side_effect=[10.0, 11.5]):
            handler(self._event())
            handler(self._event())
        self.assertEqual(fired, [])

    def test_third_press_needs_new_pair(self):
        fired = []
        handler = self._escape_handler(lambda: fired.append(1))
        with mock.patch.object(self.tui.time, "monotonic",
                               side_effect=[10.0, 10.2, 10.3]):
            handler(self._event())
            handler(self._event())  # fires, resets state
            handler(self._event())  # single press of a NEW pair — no fire
        self.assertEqual(fired, [1])

    def test_escape_with_completion_menu_dismisses_not_counts(self):
        fired = []
        handler = self._escape_handler(lambda: fired.append(1))
        ev_menu = self._event(complete_state=object())
        with mock.patch.object(self.tui.time, "monotonic",
                               side_effect=[10.0, 10.1]):
            handler(ev_menu)             # dismisses the menu
            handler(self._event())       # first bare press of a pair
        ev_menu.current_buffer.cancel_completion.assert_called_once()
        self.assertEqual(fired, [])

    def test_binding_is_eager(self):
        from prompt_toolkit.keys import Keys
        kb = self.tui._build_key_bindings(enable_cancel=True)
        for b in kb.bindings:
            if tuple(b.keys) == (Keys.Escape,):
                self.assertTrue(
                    b.eager() if callable(b.eager) else b.eager)
                return
        self.fail("no escape binding")

    def test_session_sets_ttimeoutlen(self):
        sess = self.tui.TuiSession(
            history=[], summary_state={"text": "", "up_to": 0},
            config={}, ctx_size=1000,
            cb=mock.Mock(verbose=False),
            estimate_tokens=lambda m: 1,
            enable_cancel_key=True,
        )
        self.assertAlmostEqual(sess._session.app.ttimeoutlen, 0.2)

    def test_on_cancel_key_sets_flag_and_requests(self):
        sess = self.tui.TuiSession(
            history=[], summary_state={"text": "", "up_to": 0},
            config={}, ctx_size=1000,
            cb=mock.Mock(verbose=False),
            estimate_tokens=lambda m: 1,
            enable_cancel_key=True,
        )
        cancel.reset()
        sess._on_cancel_key()
        self.assertTrue(sess.cancelling)
        self.assertTrue(cancel.is_cancelled())
        sess.clear_cancelling()
        self.assertFalse(sess.cancelling)
        cancel.reset()

    def test_toolbar_shows_cancelling_segment(self):
        sess = self.tui.TuiSession(
            history=[], summary_state={"text": "", "up_to": 0},
            config={"llm": {"model": "m"}}, ctx_size=1000,
            cb=mock.Mock(verbose=False, stream_tail=""),
            estimate_tokens=lambda m: 1,
        )
        sess.cancelling = True
        out = sess._toolbar()
        self.assertIn("cancelling", out.value)


if __name__ == "__main__":
    unittest.main()
