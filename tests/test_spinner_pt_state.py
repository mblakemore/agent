"""Tests for the prompt_toolkit-hosted spinner state (Fix B of
plan/live-input-display-fixes.md, defect #1: no spinner in live-input mode)."""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spinner
import theme


class _FakeApp:
    def __init__(self):
        self.is_running = True
        self.invalidations = 0

    def invalidate(self):
        self.invalidations += 1


class TestSharedSpinnerState(unittest.TestCase):
    def tearDown(self):
        spinner._clear_active()
        time.sleep(0.15)  # let a running ticker observe the clear and exit

    def test_start_publishes_state_under_pt_app(self):
        app = _FakeApp()
        with mock.patch.object(spinner, "_pt_app", return_value=app), \
             mock.patch.object(spinner, "_interactive", return_value=False):
            status = spinner.StreamStatus(emit=lambda *a: None)
            with mock.patch.object(spinner.sys, "stdout"):
                status.start("  -> exec_command (ls) ", pt_header=False)
            act = spinner.get_active()
            self.assertIsNotNone(act)
            self.assertEqual(act[0], "-> exec_command (ls)")
            status.finish()
            self.assertIsNone(spinner.get_active())

    def test_pt_header_false_suppresses_scrollback_line(self):
        app = _FakeApp()
        with mock.patch.object(spinner, "_pt_app", return_value=app), \
             mock.patch.object(spinner, "_interactive", return_value=False):
            status = spinner.StreamStatus(emit=lambda *a: None)
            with mock.patch.object(spinner.sys, "stdout") as m_stdout:
                status.start("  -> tool ", pt_header=False)
                m_stdout.write.assert_not_called()
            status.finish()

    def test_pt_header_true_prints_header_once(self):
        app = _FakeApp()
        with mock.patch.object(spinner, "_pt_app", return_value=app), \
             mock.patch.object(spinner, "_interactive", return_value=False), \
             mock.patch.object(spinner.theme, "_no_color", return_value=False):
            status = spinner.StreamStatus(emit=lambda *a: None)
            with mock.patch.object(spinner.sys, "stdout") as m_stdout:
                status.start("Assistant: ")
                writes = "".join(c.args[0] for c in m_stdout.write.call_args_list)
                self.assertIn("Assistant:", writes)
            status.finish()

    def test_no_pt_app_no_state(self):
        with mock.patch.object(spinner, "_pt_app", return_value=None), \
             mock.patch.object(spinner, "_interactive", return_value=False):
            status = spinner.StreamStatus(emit=lambda *a: None)
            with mock.patch.object(spinner.sys, "stdout"):
                status.start("header ")
            self.assertIsNone(spinner.get_active())
            status.finish()

    def test_first_token_keeps_state_alive_as_streaming(self):
        app = _FakeApp()
        with mock.patch.object(spinner, "_pt_app", return_value=app), \
             mock.patch.object(spinner, "_interactive", return_value=False):
            status = spinner.StreamStatus(emit=lambda *a: None)
            with mock.patch.object(spinner.sys, "stdout"):
                status.start("Assistant: ")
                status.first_token()
            act = spinner.get_active()
            self.assertIsNotNone(act)
            self.assertEqual(act[0], "streaming")
            status.finish()
            self.assertIsNone(spinner.get_active())

    def test_ticker_invalidates_and_stops(self):
        app = _FakeApp()
        with mock.patch.object(spinner, "_pt_app", return_value=app):
            spinner._set_active("tick", time.monotonic())
            time.sleep(0.35)
            self.assertGreaterEqual(app.invalidations, 1)
            spinner._clear_active()
            time.sleep(0.25)
            with spinner._active_lock:
                self.assertFalse(spinner._ticker_running)

    def test_frame_at_cycles(self):
        frames = {spinner.frame_at(t / 10.0) for t in range(10)}
        self.assertEqual(len(frames), len(spinner._BRAILLE))


class TestToolbarSpinnerSegment(unittest.TestCase):
    def setUp(self):
        import tui
        if not tui._AVAILABLE:
            self.skipTest("prompt_toolkit not installed")
        self.tui = tui

    def _session(self):
        return self.tui.TuiSession(
            history=[], summary_state={"text": "", "up_to": 0},
            config={"llm": {"model": "m"}}, ctx_size=1000,
            cb=mock.Mock(verbose=False, stream_tail=""),
            estimate_tokens=lambda m: 1,
        )

    def test_segment_absent_when_idle(self):
        sess = self._session()
        plain, seg_html = sess._spinner_segment(120)
        self.assertEqual((plain, seg_html), ("", ""))

    def test_segment_renders_label_and_elapsed(self):
        sess = self._session()
        with mock.patch.object(spinner, "get_active",
                               return_value=("running tests", time.monotonic() - 5)):
            plain, seg_html = sess._spinner_segment(120)
        self.assertIn("running tests", plain)
        self.assertIn("s", plain)
        self.assertIn("running tests", seg_html)

    def test_segment_appends_stream_tail(self):
        sess = self._session()
        sess.cb = mock.Mock(verbose=False, stream_tail="end of current line")
        with mock.patch.object(spinner, "get_active",
                               return_value=("streaming", time.monotonic())):
            plain, _ = sess._spinner_segment(200)
        self.assertIn("end of current line", plain)

    def test_segment_capped_to_half_width(self):
        sess = self._session()
        sess.cb = mock.Mock(verbose=False, stream_tail="x" * 300)
        with mock.patch.object(spinner, "get_active",
                               return_value=("y" * 300, time.monotonic())):
            plain, _ = sess._spinner_segment(80)
        self.assertLessEqual(theme.visible_len(plain), 40 + len(" │"))

    def test_toolbar_includes_segment(self):
        sess = self._session()
        with mock.patch.object(spinner, "get_active",
                               return_value=("toolbar spin", time.monotonic())):
            out = sess._toolbar()
        self.assertIn("toolbar spin", out.value)

    def test_truncated_segment_contains_no_control_chars(self):
        """Field crash 2026-08-13: truncate_middle inserts a RESET escape
        before its marker; HTML() parses via expat and a raw ESC byte is an
        invalid XML token — the render loop died every frame. The segment
        must be pure printable text however hard it is truncated."""
        sess = self._session()
        sess.cb = mock.Mock(verbose=False, stream_tail="t" * 200)
        with mock.patch.object(spinner, "get_active",
                               return_value=("streaming", time.monotonic() - 3)):
            plain, seg_html = sess._spinner_segment(80)  # forces truncation
        self.assertNotIn("\x1b", plain)
        self.assertNotIn("\x1b", seg_html)
        for ch in plain:
            self.assertGreaterEqual(ord(ch), 0x20, f"control char {ch!r}")

    def test_toolbar_parses_as_html_under_truncation(self):
        """End-to-end: the exact field repro — active spinner + long stream
        tail + ~80-col width. Constructing the toolbar builds HTML(); an
        invalid token raises right here."""
        sess = self._session()
        sess.cb = mock.Mock(verbose=False,
                            stream_tail="x" * 120 + " & <b> \x07 weird")
        with mock.patch.object(spinner, "get_active",
                               return_value=("streaming", time.monotonic() - 1)), \
             mock.patch.object(self.tui.os, "get_terminal_size",
                               return_value=os.terminal_size((80, 24))):
            out = sess._toolbar()  # must not raise
        self.assertIn("streaming", out.value)

    def test_toolbar_parses_with_cancelling_segment(self):
        sess = self._session()
        sess.cancelling = True
        out = sess._toolbar()  # must not raise
        self.assertIn("cancelling", out.value)


if __name__ == "__main__":
    unittest.main()
