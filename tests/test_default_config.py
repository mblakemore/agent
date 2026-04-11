"""Regression guard: every key documented in README cycle config section must be
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


if __name__ == "__main__":
    unittest.main()
