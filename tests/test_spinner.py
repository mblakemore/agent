"""Tests for theme.pulse_* aurora animation and spinner interactive gating."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import theme
import spinner

def _sample_rgbs(n=120, dt=0.05):
    """Sample pulse_rgb across one full cycle (3s)."""
    return [theme.pulse_rgb(i * dt) for i in range(n)]


class TestPulseColors(unittest.TestCase):
    def test_pulse_rgb_sweeps_gradient(self):
        """pulse_rgb should visit all three Aurora stops across one cycle."""
        rgbs = _sample_rgbs()
        # Each channel must vary — if it's constant, the animation is stuck.
        rs = [c[0] for c in rgbs]
        gs = [c[1] for c in rgbs]
        bs = [c[2] for c in rgbs]
        self.assertGreater(max(rs) - min(rs), 20)
        self.assertGreater(max(gs) - min(gs), 20)
        self.assertGreater(max(bs) - min(bs), 20)

    def test_pulse_rgb_stays_in_gradient_range(self):
        """Each channel must remain within the min/max of the three stops."""
        lo_r = min(theme.VIOLET[0], theme.SKY[0], theme.MINT[0])
        hi_r = max(theme.VIOLET[0], theme.SKY[0], theme.MINT[0])
        lo_g = min(theme.VIOLET[1], theme.SKY[1], theme.MINT[1])
        hi_g = max(theme.VIOLET[1], theme.SKY[1], theme.MINT[1])
        lo_b = min(theme.VIOLET[2], theme.SKY[2], theme.MINT[2])
        hi_b = max(theme.VIOLET[2], theme.SKY[2], theme.MINT[2])
        for r, g, b in _sample_rgbs():
            self.assertTrue(lo_r <= r <= hi_r, f"R={r} out of range")
            self.assertTrue(lo_g <= g <= hi_g, f"G={g} out of range")
            self.assertTrue(lo_b <= b <= hi_b, f"B={b} out of range")

    def test_pulse_escape_no_color_returns_empty(self):
        """pulse_escape returns empty string when color is suppressed."""
        with mock.patch.object(theme, "_no_color", return_value=True):
            self.assertEqual(theme.pulse_escape(0.0), "")
            self.assertEqual(theme.pulse_escape(1.5), "")

    def test_pulse_escape_with_color_returns_sgr(self):
        """pulse_escape returns an ANSI SGR sequence when color is on."""
        with mock.patch.object(theme, "_no_color", return_value=False):
            with mock.patch.object(theme, "_truecolor", return_value=True):
                esc = theme.pulse_escape(0.0)
                self.assertTrue(esc.startswith("\033["))
                self.assertIn("38;2;", esc)  # truecolor foreground

    def test_pulse_escape_fallback_256_format(self):
        """Without truecolor, pulse_escape uses the 256-color fallback."""
        with mock.patch.object(theme, "_no_color", return_value=False):
            with mock.patch.object(theme, "_truecolor", return_value=False):
                esc = theme.pulse_escape(0.0)
                self.assertTrue(esc.startswith("\033["))
                self.assertIn("38;5;", esc)


class TestSpinnerInteractivity(unittest.TestCase):
    def test_interactive_off_when_no_color(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            self.assertFalse(spinner._interactive())

    def test_interactive_on_with_color(self):
        with mock.patch.object(theme, "_no_color", return_value=False):
            self.assertTrue(spinner._interactive())

    def test_status_non_interactive_writes_prefix_once(self):
        """Non-interactive start() writes the stripped prefix then skips the thread."""
        with mock.patch.object(theme, "_no_color", return_value=True):
            status = spinner.StreamStatus()
            self.assertFalse(status._interactive)
            with mock.patch("sys.stdout") as m_stdout:
                status.start(prefix="hello ")
                # Thread should not have been started
                self.assertIsNone(status._thread)
                # Prefix should have been written at least once
                m_stdout.write.assert_any_call("hello ")
            status.finish()


class TestStreamStatusFullLifecycle(unittest.TestCase):
    def test_start_with_leading_newlines(self):
        """Verify that leading newlines in prefix are printed immediately."""
        with mock.patch.object(theme, "_no_color", return_value=True):
            status = spinner.StreamStatus()
            with mock.patch("sys.stdout") as m_stdout:
                status.start(prefix="\n\nHello")
                m_stdout.write.assert_any_call("\n\n")
                m_stdout.write.assert_any_call("Hello")
            status.finish()

    def test_interactive_lifecycle(self):
        """Test the full interactive spinner lifecycle: start -> spin -> first_token -> count -> finish."""
        with mock.patch.object(theme, "_no_color", return_value=False):
            with mock.patch("sys.stdout") as m_stdout:
                status = spinner.StreamStatus()
                self.assertTrue(status._interactive)
                
                # 1. Start
                status.start(prefix="Loading...")
                self.assertIsNotNone(status._thread)
                self.assertTrue(status._thread.is_alive())
                
                import time
                time.sleep(0.2)
                
                found_spin_call = any(
                    spinner.theme.CLEAR_LINE in call.args[0] and "Loading..." in call.args[0]
                    for call in m_stdout.write.call_args_list
                )
                self.assertTrue(found_spin_call, "Spinner thread did not write to stdout")
                
                # 2. First Token
                status.first_token()
                self.assertIsNone(status._thread)
                m_stdout.write.assert_any_call(f"{spinner.theme.CLEAR_LINE}Loading...")
                
                # 3. Count Tokens
                for i in range(5):
                    status.count_token()
                
                found_title_call = any(
                    call.args[0].startswith("\033]0;")
                    for call in m_stdout.write.call_args_list
                )
                self.assertTrue(found_title_call, "Terminal title was not updated after 5 tokens")
                
                # 4. Finish
                with mock.patch.object(status, "_emit") as m_emit:
                    status.finish()
                    m_emit.assert_called_once()
                    m_stdout.write.assert_any_call("\033]0;\007")

    def test_finish_without_start(self):
        """Verify finish() handles cases where start() was never called."""
        # Force non-interactive to avoid the title reset write
        with mock.patch.object(theme, "_no_color", return_value=True):
            status = spinner.StreamStatus()
            with mock.patch("sys.stdout") as m_stdout:
                status.finish()
                m_stdout.write.assert_not_called()

    def test_finish_interactive_no_tokens(self):
        """Verify finish() writes CLEAR_LINE when interactive and no tokens were counted."""
        with mock.patch.object(theme, "_no_color", return_value=False):
            status = spinner.StreamStatus()
            with mock.patch("sys.stdout") as m_stdout:
                status.start(prefix="Test")
                status.finish()
                m_stdout.write.assert_any_call(spinner.theme.CLEAR_LINE)

if __name__ == "__main__":
    unittest.main()
