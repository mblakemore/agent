"""Tests for /model persistence helpers (issue #1045).

Covers ``agent._persist_config_value`` (deep-merge + round-trip into
``./.agent/config.json``) and ``agent._set_model_for_role`` (updates in-memory
config + the live backend object, and persists summary selections).
"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import agent


class _FakeBackend:
    def __init__(self, model, base_url=""):
        self.model = model
        self.base_url = base_url


class TestPersistConfigValue(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _read(self):
        p = Path(self._tmp.name) / ".agent" / "config.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_creates_dir_and_file(self):
        path = agent._persist_config_value("llm", "model", "m1")
        self.assertTrue(Path(path).exists())
        self.assertEqual(self._read()["llm"]["model"], "m1")

    def test_deep_merge_preserves_unrelated_keys(self):
        agent_dir = Path(self._tmp.name) / ".agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.json").write_text(json.dumps({
            "llm": {"base_url": "http://x", "model": "orig"},
            "preferences": {"keep": True},
            "log_dir": "logs",
        }), encoding="utf-8")

        agent._persist_config_value("llm", "model", "new-main")
        agent._persist_config_value("summary", "model", "new-sum")

        data = self._read()
        # changed keys land
        self.assertEqual(data["llm"]["model"], "new-main")
        self.assertEqual(data["summary"]["model"], "new-sum")
        # unrelated keys/sections preserved
        self.assertEqual(data["llm"]["base_url"], "http://x")
        self.assertEqual(data["preferences"], {"keep": True})
        self.assertEqual(data["log_dir"], "logs")

    def test_round_trips_via_json(self):
        agent._persist_config_value("summary", "model", "gemma-E4B")
        self.assertEqual(self._read()["summary"]["model"], "gemma-E4B")

    def test_corrupt_file_does_not_raise(self):
        agent_dir = Path(self._tmp.name) / ".agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.json").write_text("{not json", encoding="utf-8")
        # should not raise; writes a clean file with just the merged section
        agent._persist_config_value("llm", "model", "recovered")
        self.assertEqual(self._read()["llm"]["model"], "recovered")


class TestSetModelForRole(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_main_updates_config_backend_and_persists(self):
        cfg = {"llm": {"model": "old"}, "summary": {"model": "s"}}
        mb = _FakeBackend("old", "http://main")
        sb = _FakeBackend("s", "http://sum")
        with patch.object(agent, "_config", cfg), \
             patch.object(agent, "_main_backend", mb), \
             patch.object(agent, "_summary_backend", sb):
            agent._set_model_for_role("main", "new-main")
        self.assertEqual(cfg["llm"]["model"], "new-main")
        self.assertEqual(mb.model, "new-main")
        self.assertEqual(sb.model, "s")  # summary untouched
        persisted = json.loads(
            (Path(self._tmp.name) / ".agent" / "config.json").read_text())
        self.assertEqual(persisted["llm"]["model"], "new-main")

    def test_summary_updates_summary_backend_and_persists(self):
        cfg = {"llm": {"model": "m"}, "summary": {"model": "old-sum"}}
        mb = _FakeBackend("m", "http://main")
        sb = _FakeBackend("old-sum", "http://sum")
        with patch.object(agent, "_config", cfg), \
             patch.object(agent, "_main_backend", mb), \
             patch.object(agent, "_summary_backend", sb):
            agent._set_model_for_role("summary", "new-sum")
        self.assertEqual(cfg["summary"]["model"], "new-sum")
        self.assertEqual(sb.model, "new-sum")
        self.assertEqual(mb.model, "m")  # main untouched
        persisted = json.loads(
            (Path(self._tmp.name) / ".agent" / "config.json").read_text())
        self.assertEqual(persisted["summary"]["model"], "new-sum")


if __name__ == "__main__":
    unittest.main()
