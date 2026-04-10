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


if __name__ == "__main__":
    unittest.main()
