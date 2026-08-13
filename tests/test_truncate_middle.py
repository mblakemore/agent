"""Tests for theme.strip_ansi / visible_len / truncate_middle (Fix A of
plan/live-input-display-fixes.md) and their spinner/callback call sites."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme


SGR = "\x1b[38;2;1;2;3m"
RESET = "\x1b[0m"
OSC_TITLE = "\x1b]0;42 tokens\x07"


class TestVisibleLen(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(theme.visible_len("hello"), 5)

    def test_empty(self):
        self.assertEqual(theme.visible_len(""), 0)

    def test_sgr_stripped(self):
        self.assertEqual(theme.visible_len(f"{SGR}hi{RESET}"), 2)

    def test_osc_stripped(self):
        self.assertEqual(theme.visible_len(f"{OSC_TITLE}hi"), 2)

    def test_csi_cursor_and_clear(self):
        self.assertEqual(theme.visible_len("\r\x1b[Kabc"), 4)  # \r is 1 char

    def test_strip_ansi_roundtrip(self):
        self.assertEqual(theme.strip_ansi(f"{SGR}a{RESET}b"), "ab")


class TestTruncateMiddle(unittest.TestCase):
    def test_fits_returned_unchanged(self):
        self.assertEqual(theme.truncate_middle("short", 10), "short")

    def test_exact_width_unchanged(self):
        self.assertEqual(theme.truncate_middle("12345", 5), "12345")

    def test_one_over_truncates(self):
        out = theme.truncate_middle("123456", 5)
        self.assertLessEqual(theme.visible_len(out), 5)
        self.assertIn("…", out)

    def test_head_and_tail_preserved(self):
        s = "HEAD" + "x" * 100 + "TAIL"
        out = theme.truncate_middle(s, 20)
        plain = theme.strip_ansi(out)
        self.assertTrue(plain.startswith("HEAD"))
        self.assertTrue(plain.endswith("TAIL"))
        self.assertLessEqual(theme.visible_len(out), 20)

    def test_zero_width_returns_empty(self):
        self.assertEqual(theme.truncate_middle("anything", 0), "")

    def test_negative_width_returns_empty(self):
        self.assertEqual(theme.truncate_middle("anything", -3), "")

    def test_width_not_above_marker(self):
        out = theme.truncate_middle("abcdefgh", 1)
        self.assertLessEqual(theme.visible_len(out), 1)

    def test_ansi_preserved_and_zero_cost(self):
        s = f"{SGR}HEAD{RESET}" + "y" * 100 + f"{SGR}TAIL{RESET}"
        out = theme.truncate_middle(s, 20)
        self.assertLessEqual(theme.visible_len(out), 20)
        plain = theme.strip_ansi(out)
        self.assertTrue(plain.startswith("HEAD"))
        self.assertTrue(plain.endswith("TAIL"))
        # Escapes survive in the kept head
        self.assertIn(SGR, out)

    def test_visible_budget_split(self):
        out = theme.truncate_middle("abcdefghij", 7)  # keep 6 visible + marker
        plain = theme.strip_ansi(out)
        # head gets the extra char on odd budgets
        self.assertEqual(plain, "abc…hij")

    def test_property_output_never_exceeds_width(self):
        for width in range(0, 30):
            for s in ("", "a", "ab" * 40, f"{SGR}styled{RESET}" * 10):
                out = theme.truncate_middle(s, width)
                self.assertLessEqual(
                    theme.visible_len(out), max(width, 0),
                    f"width={width} s={s[:20]!r} out={out[:40]!r}",
                )


class TestSpinnerWidthSafety(unittest.TestCase):
    """The composed spinner frame must never exceed the terminal width —
    a wrapped frame line scroll-spams one duplicate row per redraw."""

    def _one_frame(self, prefix, cols):
        import spinner
        status = spinner.StreamStatus.__new__(spinner.StreamStatus)
        status._prefix = prefix
        status._start_time = 0.0
        writes = []
        fake_stop = mock.Mock()
        # run exactly one loop iteration
        fake_stop.is_set.side_effect = [False, True]
        status._stop = fake_stop
        with mock.patch.object(spinner.time, "monotonic", return_value=123.4), \
             mock.patch.object(spinner.shutil, "get_terminal_size",
                               return_value=os.terminal_size((cols, 24))), \
             mock.patch.object(spinner.sys, "stdout") as m_stdout:
            status._spin()
            for call in m_stdout.write.call_args_list:
                writes.append(call.args[0])
        return "".join(writes)

    def test_long_prefix_fits_narrow_terminal(self):
        frame = self._one_frame("  -> exec_command (" + "x" * 300 + ") ", 60)
        # Drop the leading CLEAR_LINE (\r moves, doesn't print)
        self.assertLessEqual(theme.visible_len(frame) - 1, 59)  # -1 for \r

    def test_short_prefix_untouched(self):
        frame = self._one_frame("Loading... ", 120)
        self.assertIn("Loading... ", frame)

    def test_elapsed_counter_growth_still_fits(self):
        import spinner
        status = spinner.StreamStatus.__new__(spinner.StreamStatus)
        status._prefix = "p" * 200
        status._start_time = 0.0
        fake_stop = mock.Mock()
        fake_stop.is_set.side_effect = [False, True]
        status._stop = fake_stop
        # elapsed = 12345.6s — five more digits than the old fixed budget assumed
        with mock.patch.object(spinner.time, "monotonic", return_value=12345.6), \
             mock.patch.object(spinner.shutil, "get_terminal_size",
                               return_value=os.terminal_size((50, 24))), \
             mock.patch.object(spinner.sys, "stdout") as m_stdout:
            status._spin()
            frame = "".join(c.args[0] for c in m_stdout.write.call_args_list)
        self.assertLessEqual(theme.visible_len(frame) - 1, 49)


class TestCallbackFitLine(unittest.TestCase):
    def test_fit_line_passthrough_when_piped(self):
        from callbacks import TerminalCallbacks
        cb = TerminalCallbacks()
        long_line = "z" * 500
        with mock.patch.object(theme, "_no_color", return_value=True):
            self.assertEqual(cb._fit_line(long_line), long_line)

    def test_fit_line_truncates_on_tty(self):
        from callbacks import TerminalCallbacks
        cb = TerminalCallbacks()
        with mock.patch.object(theme, "_no_color", return_value=False), \
             mock.patch("callbacks.shutil.get_terminal_size",
                        return_value=os.terminal_size((40, 24))):
            out = cb._fit_line("z" * 500)
        self.assertLessEqual(theme.visible_len(out), 39)


if __name__ == "__main__":
    unittest.main()
