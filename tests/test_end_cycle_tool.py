"""Tests for the end_cycle tool and its integration with run_agent_single."""

import json
import unittest
from unittest.mock import MagicMock, patch

import agent
import tools.end_cycle as end_cycle_tool
from tools import MAP_FN, tools as global_tools


class TestEndCycleToolDefinition(unittest.TestCase):

    def test_not_in_global_tools_list(self):
        names = [t["function"]["name"] for t in global_tools]
        self.assertNotIn("end_cycle", names,
                         "end_cycle should be excluded from global tools list at startup")

    def test_registered_in_map_fn(self):
        self.assertIn("end_cycle", MAP_FN,
                      "end_cycle must be in MAP_FN so it can be dispatched")

    def test_fn_returns_sentinel(self):
        result = end_cycle_tool.fn(summary="done the thing")
        self.assertEqual(result, end_cycle_tool.SENTINEL)

    def test_fn_returns_sentinel_with_no_args(self):
        result = end_cycle_tool.fn()
        self.assertEqual(result, end_cycle_tool.SENTINEL)

    def test_auto_exclude_flag_set(self):
        self.assertTrue(getattr(end_cycle_tool, "_auto_exclude", False))

    def test_definition_has_summary_parameter(self):
        params = end_cycle_tool.definition["function"]["parameters"]["properties"]
        self.assertIn("summary", params)

    def test_definition_requires_summary(self):
        required = end_cycle_tool.definition["function"]["parameters"].get("required", [])
        self.assertIn("summary", required)


def _make_stream(content="", tool_calls=None):
    chunks = []
    if content:
        chunks.append({"choices": [{"delta": {"content": content}}]})
    if tool_calls:
        for i, tc in enumerate(tool_calls):
            chunks.append({"choices": [{"delta": {"tool_calls": [
                {"index": i, "id": tc["id"], "type": "function",
                 "function": {"name": tc["function"]["name"],
                              "arguments": tc["function"]["arguments"]}}
            ]}}]})
    return chunks


class TestEndCycleLocking(unittest.TestCase):
    """end_cycle is not available until after the first nudge."""

    def _run(self, responses, exec_side_effect=None):
        log = MagicMock()
        captured_tool_lists = []

        def capturing_llm_request(log, **kwargs):
            body = kwargs.get("json", {})
            captured_tool_lists.append([
                t["function"]["name"] for t in body.get("tools", [])
            ])
            return next(iter(responses))

        responses_iter = iter(responses)

        def llm_request(log, **kwargs):
            body = kwargs.get("json", {})
            captured_tool_lists.append([
                t["function"]["name"] for t in body.get("tools", [])
            ])
            return next(responses_iter)

        def _exec(**kwargs):
            if exec_side_effect:
                return exec_side_effect(kwargs.get("command", ""))
            return "exit=0"

        with patch("agent._NUDGE_ENABLED", True), \
             patch("agent._MAX_TOTAL_NUDGES", 10), \
             patch("agent._MAX_TEXT_ONLY", 10), \
             patch("agent._llm_request", side_effect=llm_request), \
             patch("agent._emit"), \
             patch.dict("agent.MAP_FN", {"exec_command": _exec}):
            result = agent.run_agent_single(
                conversation_history=[],
                summary_state={"text": "", "up_to": 0},
                initial_files={},
                log=log,
                start_turn=0,
            )
        return result, captured_tool_lists, log

    def test_end_cycle_absent_before_nudge(self):
        """First request must not include end_cycle in the tools list."""
        tc_commit = [{"id": "t1", "type": "function", "function": {
            "name": "exec_command",
            "arguments": json.dumps({"command": "git commit -m 'C1: work'"})
        }}]
        # commit → completion signal (nudge fires) → end_cycle call
        tc_end = [{"id": "t2", "type": "function", "function": {
            "name": "end_cycle",
            "arguments": json.dumps({"summary": "finished"})
        }}]
        responses = [
            _make_stream(tool_calls=tc_commit),
            _make_stream(content="cycle is complete"),  # triggers nudge
            _make_stream(tool_calls=tc_end),
        ]

        def _exec(cmd):
            if "git commit" in cmd:
                return "exit=0\n[main abc1234] C1: work"
            return "exit=0"

        result, tool_lists, log = self._run(responses, exec_side_effect=_exec)

        # First request must NOT include end_cycle
        self.assertNotIn("end_cycle", tool_lists[0],
                         "end_cycle must not appear in tools list before first nudge")

    def test_end_cycle_present_after_nudge(self):
        """After the first nudge, end_cycle must appear in subsequent requests."""
        tc_commit = [{"id": "t1", "type": "function", "function": {
            "name": "exec_command",
            "arguments": json.dumps({"command": "git commit -m 'C1: work'"})
        }}]
        tc_end = [{"id": "t2", "type": "function", "function": {
            "name": "end_cycle",
            "arguments": json.dumps({"summary": "finished"})
        }}]
        responses = [
            _make_stream(tool_calls=tc_commit),
            _make_stream(content="cycle is complete"),  # nudge fires here
            _make_stream(tool_calls=tc_end),
        ]

        def _exec(cmd):
            if "git commit" in cmd:
                return "exit=0\n[main abc1234] C1: work"
            return "exit=0"

        result, tool_lists, log = self._run(responses, exec_side_effect=_exec)
        assert result == "done"

        # Third request (after nudge) must include end_cycle
        if len(tool_lists) >= 3:
            self.assertIn("end_cycle", tool_lists[2],
                          "end_cycle must appear in tools list after first nudge")

    def test_end_cycle_sentinel_causes_clean_exit(self):
        """Calling end_cycle after a nudge must cause run_agent_single to return 'done'."""
        tc_commit = [{"id": "t1", "type": "function", "function": {
            "name": "exec_command",
            "arguments": json.dumps({"command": "git commit -m 'C1: work'"})
        }}]
        tc_end = [{"id": "t2", "type": "function", "function": {
            "name": "end_cycle",
            "arguments": json.dumps({"summary": "cycle complete"})
        }}]
        # commit → non-signal text (auto-nudge fires, unlocking end_cycle) → end_cycle call
        responses = [
            _make_stream(tool_calls=tc_commit),
            _make_stream(content="I have finished the work and everything looks good."),
            _make_stream(tool_calls=tc_end),
        ]

        def _exec(cmd):
            if "git commit" in cmd:
                return "exit=0\n[main abc1234] C1: work"
            return "exit=0"

        result, _, log = self._run(responses, exec_side_effect=_exec)
        self.assertEqual(result, "done")
        end_cycle_logged = any(
            "end_cycle called" in str(call)
            for call in log.info.call_args_list
        )
        self.assertTrue(end_cycle_logged,
                        f"Expected 'end_cycle called' in log; got {log.info.call_args_list}")


if __name__ == "__main__":
    unittest.main()
