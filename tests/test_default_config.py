"""Regression guard: every key documented in README config sections must be
present in _DEFAULT_CONFIG so that it is discoverable without reading agent.py
line-by-line."""

import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent


class TestDefaultConfigCycleKeys(unittest.TestCase):
    def test_max_text_only_in_default_config(self):
        """max_text_only is documented in README under the cycle section;
        it must be a key in _DEFAULT_CONFIG['cycle'] so readers of the defaults
        can discover it."""
        self.assertIn(
            "max_text_only",
            agent._DEFAULT_CONFIG["cycle"],
            "_DEFAULT_CONFIG['cycle'] must include 'max_text_only'",
        )

    def test_max_text_only_default_value(self):
        """The default value of max_text_only should be 3 (matches the prior
        inline fallback and the existing _TEXT_LOOP_THRESHOLD constant)."""
        self.assertEqual(agent._DEFAULT_CONFIG["cycle"]["max_text_only"], 3)

    def test_cycle_keys_complete(self):
        """The full set of documented cycle config keys must all be present."""
        expected = {"max_turns", "wind_down_turns", "max_text_only"}
        actual = set(agent._DEFAULT_CONFIG["cycle"].keys())
        missing = expected - actual
        self.assertFalse(
            missing,
            f"Keys documented in README but missing from _DEFAULT_CONFIG['cycle']: {missing}",
        )


class TestDefaultConfigContextKeys(unittest.TestCase):
    def test_summary_max_chars_in_default_config(self):
        """summary_max_chars must be a key in _DEFAULT_CONFIG['context'].
        If it were absent, agent.py:134 (direct [] access) would KeyError at
        import time, and a programmer reading _DEFAULT_CONFIG would not discover
        the expected default value."""
        self.assertIn(
            "summary_max_chars",
            agent._DEFAULT_CONFIG["context"],
            "_DEFAULT_CONFIG['context'] must include 'summary_max_chars'",
        )

    def test_summary_max_chars_default_value(self):
        """The default value of summary_max_chars is 3000.
        A prior stale .get() fallback of 1500 was removed in cycle 0023;
        this test pins the correct default so the discrepancy can never silently
        reappear."""
        self.assertEqual(agent._DEFAULT_CONFIG["context"]["summary_max_chars"], 3000)


if __name__ == "__main__":
    unittest.main()
