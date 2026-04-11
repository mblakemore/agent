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


class TestDefaultConfigSummaryKeys(unittest.TestCase):
    """Regression guard: _DEFAULT_CONFIG['summary'] must have all 4 keys so
    that agent.py can use direct [] access instead of .get() with fallbacks."""

    _EXPECTED_SUMMARY_KEYS = {"base_url", "model", "enabled", "max_wait_on_save"}

    def test_summary_section_keys_in_default_config(self):
        """All 4 summary section keys must be present in _DEFAULT_CONFIG so that
        _config['summary']['key'] is always safe — no .get() fallback needed."""
        actual = set(agent._DEFAULT_CONFIG["summary"].keys())
        missing = self._EXPECTED_SUMMARY_KEYS - actual
        self.assertFalse(
            missing,
            f"Keys missing from _DEFAULT_CONFIG['summary']: {missing}",
        )

    def test_load_config_summary_section_always_present(self):
        """_load_config() must return a config where ['summary'] and all 4 inner
        keys are directly accessible — no KeyError — even with no config.json."""
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                cfg = agent._load_config()
            finally:
                os.chdir(old_cwd)

        # Direct [] access must not raise
        summary = cfg["summary"]
        for key in self._EXPECTED_SUMMARY_KEYS:
            self.assertIn(key, summary, f"cfg['summary']['{key}'] missing from _load_config() result")
            _ = summary[key]  # direct access, must not KeyError


if __name__ == "__main__":
    unittest.main()
