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


class TestRunAgentSingleDefaults(unittest.TestCase):
    """Regression guard for CICD cycle 0025: run_agent_single's parameter
    defaults must match _DEFAULT_CONFIG so they stay in sync with the config
    system and don't mislead direct callers."""

    import inspect as _inspect

    @classmethod
    def _sig_defaults(cls):
        sig = cls._inspect.signature(agent.run_agent_single)
        return {
            k: v.default
            for k, v in sig.parameters.items()
            if v.default is not cls._inspect.Parameter.empty
        }

    def test_run_agent_single_defaults_match_generation_config(self):
        """temperature, top_p, top_k, presence_penalty defaults must equal
        _DEFAULT_CONFIG['generation'] values.

        Before cycle 0025, these were stale pre-refactor literals:
          temperature=0.7, top_p=0.8, top_k=20, presence_penalty=1.5
        After cycle 0025, they read from _DEFAULT_CONFIG at function-definition
        time and must equal 1.0, 0.95, 64, 0.0 respectively."""
        defaults = self._sig_defaults()
        gen = agent._DEFAULT_CONFIG["generation"]
        for key in ("temperature", "top_p", "top_k", "presence_penalty"):
            self.assertEqual(
                defaults[key],
                gen[key],
                f"run_agent_single default for '{key}' is {defaults[key]!r} but "
                f"_DEFAULT_CONFIG['generation']['{key}'] is {gen[key]!r} — "
                f"cycle 0025 regression: the function signature was re-hardcoded.",
            )

    def test_run_agent_single_defaults_match_context_config(self):
        """max_tokens and ctx_size defaults must equal _DEFAULT_CONFIG['context']
        values.

        Before cycle 0025, these were stale literals: max_tokens=4096, ctx_size=32768.
        After cycle 0025, they read from _DEFAULT_CONFIG and must equal 16384 and
        114688 respectively."""
        defaults = self._sig_defaults()
        ctx = agent._DEFAULT_CONFIG["context"]
        for key in ("max_tokens", "ctx_size"):
            self.assertEqual(
                defaults[key],
                ctx[key],
                f"run_agent_single default for '{key}' is {defaults[key]!r} but "
                f"_DEFAULT_CONFIG['context']['{key}'] is {ctx[key]!r} — "
                f"cycle 0025 regression: the function signature was re-hardcoded.",
            )


class TestLoadConfigTopLevel(unittest.TestCase):
    """Regression guard for CICD cycle 0026: _load_config() must copy top-level
    scalar keys from config.json into _config, not silently drop them.

    Before this fix, _load_config() only merged dict-valued sections that were
    already present in _DEFAULT_CONFIG.  Keys like 'log_dir' and 'log_prefix'
    — which are top-level strings — were always swallowed and could never be
    overridden by a user's config.json."""

    def _load_with(self, user_cfg: dict):
        """Write user_cfg to a temp dir's config.json and return _load_config()."""
        import tempfile
        import json as _json

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w") as f:
                _json.dump(user_cfg, f)
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                return agent._load_config()
            finally:
                os.chdir(old_cwd)

    def test_log_prefix_override(self):
        """log_prefix set in config.json must appear in the loaded config."""
        cfg = self._load_with({"log_prefix": "mytest"})
        self.assertEqual(
            cfg.get("log_prefix"),
            "mytest",
            "log_prefix from config.json was silently dropped by _load_config()",
        )

    def test_log_dir_override(self):
        """log_dir set in config.json must appear in the loaded config."""
        cfg = self._load_with({"log_dir": "my_logs"})
        self.assertEqual(
            cfg.get("log_dir"),
            "my_logs",
            "log_dir from config.json was silently dropped by _load_config()",
        )

    def test_section_override_still_works(self):
        """Existing dict-section merging must continue to work alongside the
        new scalar copy — a dict section override must not be treated as a scalar."""
        cfg = self._load_with({"llm": {"base_url": "http://testhost:9999"}})
        self.assertEqual(
            cfg["llm"]["base_url"],
            "http://testhost:9999",
            "dict-section override (llm.base_url) broken after adding scalar copy",
        )
        # The scalar-copy loop must not duplicate the dict section as a top-level key
        self.assertIsInstance(cfg["llm"], dict, "cfg['llm'] must remain a dict")

    def test_unknown_scalar_does_not_overwrite_section(self):
        """A scalar value for a key that IS a _DEFAULT_CONFIG section must not
        overwrite the dict — guard 'key not in config' prevents this."""
        cfg = self._load_with({"llm": "bad_value"})
        # The scalar "bad_value" should be ignored because "llm" is already in config
        self.assertIsInstance(
            cfg["llm"],
            dict,
            "A scalar user_config entry with the same name as a DEFAULT section "
            "must not overwrite the dict",
        )


if __name__ == "__main__":
    unittest.main()
