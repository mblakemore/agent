"""preferences.turn_hooks: shell commands around each turn, for status indicators.

Iris's avatar status dots used to ride on Claude Code's UserPromptSubmit and Stop hooks.
Under agent.py the same job is done by turn_hooks.start / turn_hooks.end.
"""
import os
import tempfile
import unittest
from unittest import mock

import agent


def _cfg(hooks):
    return mock.patch.dict(agent._config, {"preferences": {"turn_hooks": hooks}})


class TestTurnHooks(unittest.TestCase):
    def _run(self, hooks, impl):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(agent, "_CYCLE_STATUS_PATH", os.path.join(d, "s.json")), \
             mock.patch.object(agent, "_run_agent_single_impl", side_effect=impl), _cfg(hooks):
            return d, agent.run_agent_single([], {}, None, mock.Mock())

    def test_start_runs_before_the_turn_and_end_after(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log")
            hooks = {"start": f"echo start >> {log}", "end": f"echo end >> {log}"}
            def impl(*a, **k):
                with open(log, "a") as f:
                    f.write("turn\n")
                return "done"
            self._run(hooks, impl)
            self.assertEqual(open(log).read().split(), ["start", "turn", "end"])

    def test_end_runs_when_the_turn_raises(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log")
            def boom(*a, **k):
                raise RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                self._run({"end": f"echo end >> {log}"}, boom)
            self.assertEqual(open(log).read().split(), ["end"])

    def test_no_config_changes_nothing(self):
        with mock.patch.object(agent.subprocess, "run") as run:
            self._run(None, lambda *a, **k: "done")
            self._run({}, lambda *a, **k: "done")
        run.assert_not_called()

    def test_a_list_and_a_single_string_both_work(self):
        calls = []
        with mock.patch.object(agent.subprocess, "run", side_effect=lambda c, **k: calls.append(c)):
            self._run({"start": ["a", "b"], "end": "c"}, lambda *a, **k: "done")
        self.assertEqual(calls, ["a", "b", "c"])

    def test_broken_hooks_never_stop_the_turn(self):
        with mock.patch.object(agent.subprocess, "run", side_effect=OSError("no shell")):
            _, result = self._run({"start": "x", "end": "y"}, lambda *a, **k: "done")
        self.assertEqual(result, "done")
        _, result = self._run({"start": 7, "end": {"bad": 1}}, lambda *a, **k: "done")
        self.assertEqual(result, "done")

    def test_a_hung_hook_is_cut_off(self):
        import time
        started = time.time()
        with _cfg({"start": "sleep 30"}):
            agent._run_turn_hooks("start")
        self.assertLess(time.time() - started, 8)


if __name__ == "__main__":
    unittest.main()
