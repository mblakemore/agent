"""Smoke tests for cancel.set_tui_mode — Phase 1 DoD requirement."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cancel


class TuiModeTests(unittest.TestCase):
    def setUp(self):
        cancel.set_tui_mode(False)
        cancel.reset()

    def tearDown(self):
        cancel.set_tui_mode(False)
        cancel.reset()

    def test_default_is_non_tui(self):
        self.assertFalse(cancel.tui_mode())

    def test_enable_and_disable(self):
        cancel.set_tui_mode(True)
        self.assertTrue(cancel.tui_mode())
        cancel.set_tui_mode(False)
        self.assertFalse(cancel.tui_mode())

    def test_request_cancel_sets_flag(self):
        self.assertFalse(cancel.is_cancelled())
        cancel.request_cancel()
        self.assertTrue(cancel.is_cancelled())
        cancel.reset()
        self.assertFalse(cancel.is_cancelled())

    def test_cancellable_honors_tui_mode(self):
        cancel.set_tui_mode(True)
        with cancel.cancellable() as cm:
            # In TUI mode, cbreak capture is skipped; the internal cbreak
            # handle should be None.
            self.assertIsNone(cm._cbreak)
        cancel.set_tui_mode(False)

    def test_check_cancelled_raises_when_set(self):
        cancel.request_cancel()
        with self.assertRaises(cancel.CancelledError):
            cancel.check_cancelled()

    def test_check_cancelled_noop_when_clear(self):
        cancel.reset()
        self.assertIsNone(cancel.check_cancelled())

    def test_cancellable_resets_flag_on_entry(self):
        cancel.request_cancel()
        self.assertTrue(cancel.is_cancelled())
        cancel.set_tui_mode(True)  # avoid touching real stdin
        with cancel.cancellable():
            self.assertFalse(cancel.is_cancelled())

    def test_request_cancel_works_in_tui_mode(self):
        cancel.set_tui_mode(True)
        cancel.request_cancel()
        self.assertTrue(cancel.is_cancelled())


if __name__ == "__main__":
    unittest.main()
