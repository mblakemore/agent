"""Tests for TuiCallbacks line-buffered streaming (Fix C of
plan/live-input-display-fixes.md, defect #2: partial lines erased by the
patch_stdout prompt redraw in live-input mode)."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tui


@unittest.skipUnless(tui._AVAILABLE, "prompt_toolkit not installed")
class TestStreamLineBuffer(unittest.TestCase):
    def _cb(self, live=True):
        cb = tui.TuiCallbacks(mock.Mock())
        patcher = mock.patch.object(
            tui.TuiCallbacks, "_live_app_active", staticmethod(lambda: live))
        patcher.start()
        self.addCleanup(patcher.stop)
        return cb

    def test_complete_lines_emitted_partial_buffered(self):
        cb = self._cb()
        out = io.StringIO()
        with redirect_stdout(out):
            for chunk in ("hel", "lo\nwor", "ld"):
                cb.on_stream_chunk(chunk)
        self.assertEqual(out.getvalue(), "hello\n")
        self.assertEqual(cb._stream_buf, "world")

    def test_end_of_turn_flushes_tail_with_newline(self):
        cb = self._cb()
        out = io.StringIO()
        with redirect_stdout(out):
            cb.on_stream_chunk("no newline yet")
            cb.on_assistant_text("no newline yet")
        self.assertEqual(out.getvalue(), "no newline yet\n")
        self.assertEqual(cb._stream_buf, "")
        # base-class de-dup: streamed text must not be re-printed
        self.assertNotIn("no newline yet\nno newline yet", out.getvalue())

    def test_interleaved_notice_preserves_order(self):
        cb = self._cb()
        out = io.StringIO()
        with redirect_stdout(out):
            cb.on_stream_chunk("tail without newline")
            cb.on_notice("info", "[42 tokens]")
        text = out.getvalue()
        self.assertLess(
            text.index("tail without newline"), text.index("[42 tokens]"),
            f"stream tail must precede the notice: {text!r}",
        )

    def test_lossless_concatenation(self):
        chunks = ["a", "b\nc", "", "d\n\ne", "f", "\n", "g"]
        cb = self._cb()
        out = io.StringIO()
        with redirect_stdout(out):
            for c in chunks:
                cb.on_stream_chunk(c)
            cb.on_assistant_text("".join(chunks))
        # every input char reaches scrollback; only a final \n may be added
        self.assertEqual(out.getvalue(), "".join(chunks) + "\n")

    def test_multiline_single_chunk(self):
        cb = self._cb()
        out = io.StringIO()
        with redirect_stdout(out):
            cb.on_stream_chunk("one\ntwo\nthree partial")
        self.assertEqual(out.getvalue(), "one\ntwo\n")
        self.assertEqual(cb._stream_buf, "three partial")

    def test_passthrough_when_no_live_app(self):
        cb = self._cb(live=False)
        out = io.StringIO()
        with redirect_stdout(out):
            cb.on_stream_chunk("par")
            cb.on_stream_chunk("tial")
        self.assertEqual(out.getvalue(), "partial")
        self.assertEqual(cb._stream_buf, "")

    def test_cancel_flushes_tail_before_banner(self):
        cb = self._cb()
        out = io.StringIO()
        with redirect_stdout(out):
            cb.on_stream_chunk("half a thought")
            cb.on_cancelled("streaming")
        text = out.getvalue()
        self.assertLess(text.index("half a thought"), text.index("cancelled"))


if __name__ == "__main__":
    unittest.main()
