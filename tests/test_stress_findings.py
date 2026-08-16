"""Fixes surfaced by the 2026-08-16 six-phase stress test (agent 'forge'
built lifebox on local models): the advisor banner row and the harness
cycle-status done-signal."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAdvisorBannerRow(unittest.TestCase):
    def _banner(self, info):
        import callbacks
        cb = callbacks.TerminalCallbacks(verbose=False)
        lines = []
        with mock.patch.object(cb, "_print", side_effect=lines.append):
            cb.on_session_start(info)
        return "\n".join(str(x) for x in lines)

    BASE = {"version": "0.1.0", "sha": "abc", "api_ok": True, "api_detail": "",
            "model": "m", "main_kind": "llamacpp", "summary_enabled": False}

    def test_configured_healthy_advisor_gets_a_row(self):
        out = self._banner({**self.BASE, "advisor_enabled": True,
                            "advisor_ok": True, "advisor_detail": "http://x glm"})
        self.assertIn("advisor", out)
        self.assertIn("http://x glm", out)

    def test_unreachable_advisor_shown_as_warning(self):
        out = self._banner({**self.BASE, "advisor_enabled": True,
                            "advisor_ok": False, "advisor_detail": "down"})
        self.assertIn("advisor", out)
        self.assertIn("unloaded", out)

    def test_no_advisor_no_row(self):
        out = self._banner({**self.BASE, "advisor_enabled": False})
        self.assertNotIn("advisor", out)


class TestCycleStatusSignal(unittest.TestCase):
    def test_status_written_running_then_complete(self):
        import agent
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cycle_status.json")
            seen = {}
            with mock.patch.object(agent, "_CYCLE_STATUS_PATH", path), \
                 mock.patch.object(agent, "_run_agent_single_impl",
                                   side_effect=lambda *a, **k:
                                   (seen.update(running=json.load(open(path))),
                                    "done")[1]):
                result = agent.run_agent_single([], {}, None, mock.Mock())
            self.assertEqual(result, "done")
            self.assertEqual(seen["running"]["phase"], "running")
            final = json.load(open(path))
            self.assertEqual(final["phase"], "complete")
            self.assertEqual(final["result"], "done")

    def test_exception_still_records_complete(self):
        import agent
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cycle_status.json")
            with mock.patch.object(agent, "_CYCLE_STATUS_PATH", path), \
                 mock.patch.object(agent, "_run_agent_single_impl",
                                   side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    agent.run_agent_single([], {}, None, mock.Mock())
            final = json.load(open(path))
            self.assertEqual(final["phase"], "complete")
            self.assertIn("exception", final["result"])

    def test_write_never_raises_on_bad_path(self):
        import agent
        with mock.patch.object(agent, "_CYCLE_STATUS_PATH", "/nonexistent\0/x"):
            agent._write_cycle_status("running")  # must not raise


if __name__ == "__main__":
    unittest.main()


class TestMalformedConfigWarns(unittest.TestCase):
    """A malformed .agent/config.json was silently swallowed — _load_config
    runs at module import before _cb is wired, so its on_notice emit went
    nowhere and the agent ran on defaults with no word (2026-08-16). Now
    stderr + log always fire."""

    def test_malformed_config_warns_to_stderr(self):
        import io, tempfile, importlib
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".agent").mkdir()
            (Path(d) / ".agent" / "config.json").write_text('{"llm": {,}')  # bad
            old = os.getcwd()
            os.chdir(d)
            try:
                import agent
                with mock.patch("sys.stderr", new=io.StringIO()) as err:
                    cfg = agent._load_config()
                out = err.getvalue()
            finally:
                os.chdir(old)
        self.assertIn("could not parse", out)
        self.assertIn("DEFAULTS", out)
        # and it degraded to a usable default config, not a crash
        self.assertIn("llm", cfg)

    def test_valid_config_is_silent(self):
        import io, tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".agent").mkdir()
            (Path(d) / ".agent" / "config.json").write_text(
                '{"llm": {"base_url": "http://x:8080"}}')
            old = os.getcwd()
            os.chdir(d)
            try:
                import agent
                with mock.patch("sys.stderr", new=io.StringIO()) as err:
                    agent._load_config()
                self.assertEqual(err.getvalue(), "")
            finally:
                os.chdir(old)
