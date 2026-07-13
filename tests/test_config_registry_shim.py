"""Tests for the ``backends`` config registry + back-compat shim.

Per plan/bedrock-integration.md § 6 "Migration strategy" and § 20 task 1.3.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent


def _load_with(user_cfg: dict | None) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        if user_cfg is not None:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w") as f:
                json.dump(user_cfg, f)
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            return agent._load_config()
        finally:
            os.chdir(old_cwd)


class ConfigRegistryShimTest(unittest.TestCase):
    def test_empty_config_synthesizes_both_entries_as_llamacpp(self):
        cfg = _load_with(None)
        self.assertIn("backends", cfg)
        self.assertEqual(cfg["backends"]["main"]["kind"], "llamacpp")
        self.assertEqual(cfg["backends"]["summary"]["kind"], "llamacpp")
        # Defaults from _DEFAULT_CONFIG
        self.assertEqual(cfg["backends"]["main"]["base_url"], "http://127.0.0.1:8080")
        self.assertEqual(cfg["backends"]["summary"]["base_url"], "http://127.0.0.1:8082")

    def test_summary_defaults_preserved(self):
        cfg = _load_with(None)
        summary = cfg["backends"]["summary"]
        self.assertEqual(summary["enabled"], True)
        self.assertEqual(summary["max_wait_on_save"], 10)

    def test_legacy_llm_block_passes_through_unknown_keys(self):
        # K1 in plan § 17: unknown_key must survive the shim.
        cfg = _load_with({"llm": {"base_url": "http://x", "model": "y", "unknown_key": "z"}})
        main = cfg["backends"]["main"]
        self.assertEqual(main["kind"], "llamacpp")
        self.assertEqual(main["base_url"], "http://x")
        self.assertEqual(main["model"], "y")
        self.assertEqual(main["unknown_key"], "z")

    def test_explicit_backends_block_passes_through(self):
        explicit = {
            "backends": {
                "main": {"kind": "llamacpp", "base_url": "http://foo", "model": "m1"},
                "summary": {
                    "kind": "llamacpp",
                    "base_url": "http://bar",
                    "model": "m2",
                    "enabled": True,
                    "max_wait_on_save": 30,
                },
            }
        }
        cfg = _load_with(explicit)
        self.assertEqual(cfg["backends"]["main"]["base_url"], "http://foo")
        self.assertEqual(cfg["backends"]["summary"]["base_url"], "http://bar")
        self.assertEqual(cfg["backends"]["summary"]["max_wait_on_save"], 30)

    def test_legacy_views_preserved(self):
        """Existing call sites reading _config['llm'] / _config['summary'] still work."""
        cfg = _load_with({"llm": {"base_url": "http://x", "model": "m"}})
        self.assertEqual(cfg["llm"]["base_url"], "http://x")
        self.assertEqual(cfg["llm"]["model"], "m")
        # Summary keeps default
        self.assertEqual(cfg["summary"]["base_url"], "http://127.0.0.1:8082")


class AdvisorConfigLoadTest(unittest.TestCase):
    """Regression: a user `advisor` block (a section NOT in _DEFAULT_CONFIG)
    must survive _load_config, else the escalation tier is silently inert. The
    bug was masked because the gate tests inject _config['advisor'] directly."""

    def test_advisor_block_carried_through(self):
        cfg = _load_with({"advisor": {
            "base_url": "http://127.0.0.1:8000", "model": "glm-5.2",
            "max_calls_per_task": 3}})
        self.assertIn("advisor", cfg)
        self.assertEqual(cfg["advisor"]["base_url"], "http://127.0.0.1:8000")
        self.assertEqual(cfg["advisor"]["max_calls_per_task"], 3)

    def test_no_advisor_block_stays_absent(self):
        # Defaults ship no advisor endpoint → tier off unless configured.
        self.assertNotIn("advisor", _load_with({}))

    def test_unknown_scalar_override_still_carried(self):
        # The pre-existing scalar carry-through (log_prefix etc.) must not
        # regress now that dict sections are also carried.
        self.assertEqual(_load_with({"log_prefix": "agentx"}).get("log_prefix"),
                         "agentx")


if __name__ == "__main__":
    unittest.main()
