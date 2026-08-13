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


class TestToolbarStaysStable(unittest.TestCase):
    """The working indicator lives on the PROMPT LINE only. Rendering it in
    the toolbar too doubled it up in terminals where the toolbar draws
    (field report 2026-08-13) — so the toolbar must NOT reflect spinner or
    cancelling state, and must always build valid HTML()."""

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

    def test_toolbar_ignores_active_spinner(self):
        sess = self._session()
        sess.cb = mock.Mock(verbose=False, stream_tail="tail text here")
        with mock.patch.object(spinner, "get_active",
                               return_value=("streaming", time.monotonic() - 3)):
            out = sess._toolbar()  # must not raise (HTML() built inside)
        self.assertNotIn("streaming", out.value)
        self.assertNotIn("tail text here", out.value)
        self.assertFalse(any(0x2800 <= ord(c) <= 0x28FF for c in out.value),
                         "braille frame leaked into the toolbar")

    def test_toolbar_ignores_cancelling_flag(self):
        sess = self._session()
        sess.cancelling = True
        out = sess._toolbar()  # must not raise
        self.assertNotIn("cancelling", out.value)

    def test_toolbar_still_valid_html_with_hostile_model_name(self):
        sess = self._session()
        sess.config = {"llm": {"model": "we&ird<model>"}}
        out = sess._toolbar()  # html.escape guard — must not raise
        self.assertIn("we&amp;ird", out.value)


class TestPromptMessageSpinner(unittest.TestCase):
    """The prompt-line spinner (the always-rendered surface). The bottom
    toolbar is hidden whenever prompt_toolkit cannot learn the terminal
    height (CPR unsupported — field report 2026-08-13), so the working
    indicator must live in the prompt message itself."""

    def setUp(self):
        import tui
        if not tui._AVAILABLE:
            self.skipTest("prompt_toolkit not installed")
        self.tui = tui

    def _session(self, tail=""):
        return self.tui.TuiSession(
            history=[], summary_state={"text": "", "up_to": 0},
            config={"llm": {"model": "m"}}, ctx_size=1000,
            cb=mock.Mock(verbose=False, stream_tail=tail),
            estimate_tokens=lambda m: 1,
        )

    @staticmethod
    def _text(fragments):
        return "".join(t for _, t in fragments)

    def test_idle_prompt_is_plain_you(self):
        sess = self._session()
        self.assertEqual(self._text(sess._prompt_message()), "\nYou: ")

    def test_active_spinner_renders_on_prompt_line(self):
        sess = self._session()
        with mock.patch.object(spinner, "get_active",
                               return_value=("Assistant:", time.monotonic() - 2)):
            frags = sess._prompt_message()
        text = self._text(frags)
        self.assertIn("Assistant:", text)
        self.assertTrue(text.endswith("You: "))
        self.assertTrue(any(0x2800 <= ord(c) <= 0x28FF for c in text),
                        f"no braille frame in {text!r}")

    def test_active_spinner_includes_stream_tail(self):
        sess = self._session(tail="latest words")
        with mock.patch.object(spinner, "get_active",
                               return_value=("streaming", time.monotonic())):
            text = self._text(sess._prompt_message())
        self.assertIn("latest words", text)

    def test_spinner_fragments_are_control_free(self):
        sess = self._session(tail="x" * 300 + "\x07")
        with mock.patch.object(spinner, "get_active",
                               return_value=("streaming", time.monotonic())):
            text = self._text(sess._prompt_message())
        for ch in text:
            if ch == "\n":
                continue
            self.assertGreaterEqual(ord(ch), 0x20, f"control char {ch!r}")

    def test_cancelling_outranks_spinner(self):
        sess = self._session()
        sess.cancelling = True
        with mock.patch.object(spinner, "get_active",
                               return_value=("streaming", time.monotonic())):
            text = self._text(sess._prompt_message())
        self.assertIn("cancelling", text)
        self.assertNotIn("streaming", text)

    def test_never_raises_even_when_state_explodes(self):
        sess = self._session()
        with mock.patch.object(spinner, "get_active",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self._text(sess._prompt_message()), "\nYou: ")


if __name__ == "__main__":
    unittest.main()
