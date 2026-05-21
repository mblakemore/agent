"""Tests for the open-task nudge: agent is reminded of open tasks at cycle end."""

import json
import unittest
from unittest.mock import MagicMock, patch

import agent


class TestBuildOpenTaskNudge(unittest.TestCase):

    def test_returns_none_when_no_tasks_file(self):
        with patch("tools.task_tracker.get_tasks", return_value=[]):
            result = agent._build_open_task_nudge()
        self.assertIsNone(result)

    def test_returns_none_when_all_tasks_done(self):
        tasks = [
            {"id": 1, "description": "PERCEIVE", "status": "done"},
            {"id": 2, "description": "PERSIST", "status": "completed"},
        ]
        with patch("tools.task_tracker.get_tasks", return_value=tasks):
            result = agent._build_open_task_nudge()
        self.assertIsNone(result)

    def test_returns_string_when_open_tasks_exist(self):
        tasks = [
            {"id": 1, "description": "PERCEIVE", "status": "done"},
            {"id": 2, "description": "Fix the bug in foo.py", "status": "in_progress"},
            {"id": 3, "description": "Update tests", "status": "pending"},
        ]
        with patch("tools.task_tracker.get_tasks", return_value=tasks):
            result = agent._build_open_task_nudge()
        self.assertIsNotNone(result)
        self.assertIn("2 task(s) are still open", result)
        self.assertIn("Fix the bug in foo.py", result)
        self.assertIn("Update tests", result)
        self.assertNotIn("PERCEIVE", result)  # done task not included

    def test_nudge_includes_do_not_end_instruction(self):
        tasks = [{"id": 1, "description": "Do the thing", "status": "pending"}]
        with patch("tools.task_tracker.get_tasks", return_value=tasks):
            result = agent._build_open_task_nudge()
        self.assertIn("Do not end the cycle yet", result)

    def test_returns_none_on_exception(self):
        with patch("tools.task_tracker.get_tasks", side_effect=RuntimeError("boom")):
            result = agent._build_open_task_nudge()
        self.assertIsNone(result)

    def test_task_id_and_status_appear_in_output(self):
        tasks = [{"id": 7, "description": "Deploy fix", "status": "blocked"}]
        with patch("tools.task_tracker.get_tasks", return_value=tasks):
            result = agent._build_open_task_nudge()
        self.assertIn("#7", result)
        self.assertIn("blocked", result)
        self.assertIn("Deploy fix", result)


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


class TestOpenTaskNudgeFiringOnCompletionSignal(unittest.TestCase):
    """Completion signal + open tasks → nudge fires, cycle continues."""

    def test_nudge_fires_on_completion_signal_with_open_tasks(self):
        open_tasks = [{"id": 1, "description": "Still pending", "status": "pending"}]

        # Responses: commit, completion signal (intercepted), then real completion
        tc_commit = [{"id": "t1", "type": "function", "function": {
            "name": "exec_command",
            "arguments": json.dumps({"command": "git commit -m 'C1: work'"})
        }}]
        responses = [
            _make_stream(tool_calls=tc_commit),           # git commit → _has_committed
            _make_stream(content="cycle is complete"),    # caught, nudge fires
            _make_stream(content="cycle is complete"),    # no more open tasks → stop
        ]

        log = MagicMock()
        with patch("agent._NUDGE_ENABLED", True), \
             patch("agent._MAX_TOTAL_NUDGES", 10), \
             patch("agent._MAX_TEXT_ONLY", 10), \
             patch("agent._llm_request") as mock_llm, \
             patch("agent._emit"), \
             patch("agent._build_open_task_nudge",
                   side_effect=[open_tasks and
                                 f"1 task(s) are still open:\n  #1 [pending] Still pending\n"
                                 "Do not end the cycle yet — address the remaining tasks.",
                                 None]):

            mock_llm.side_effect = responses

            def _exec(**kwargs):
                cmd = kwargs.get("command", "")
                if "git commit" in cmd:
                    return "exit=0\n[main abc1234] C1: work"
                return "exit=0"

            with patch.dict("agent.MAP_FN", {"exec_command": _exec}):
                result = agent.run_agent_single(
                    conversation_history=[],
                    summary_state={"text": "", "up_to": 0},
                    initial_files={},
                    log=log,
                    start_turn=0,
                )

        assert result == "done"
        # Verify that the open-task nudge log line appeared
        nudge_fired = any(
            "open-task nudge" in str(call)
            for call in log.info.call_args_list
        )
        assert nudge_fired, f"Expected 'open-task nudge' in log; got {log.info.call_args_list}"

    def test_nudge_fires_at_most_once(self):
        """Second completion signal with open tasks should NOT fire a second nudge."""
        always_open = [{"id": 1, "description": "Always open", "status": "pending"}]

        tc_commit = [{"id": "t1", "type": "function", "function": {
            "name": "exec_command",
            "arguments": json.dumps({"command": "git commit -m 'C1: work'"})
        }}]
        responses = [
            _make_stream(tool_calls=tc_commit),
            _make_stream(content="cycle is complete"),   # nudge fires
            _make_stream(content="cycle is complete"),   # flag already set → stop
        ]

        nudge_calls = []

        def _nudge_side_effect():
            nudge_calls.append(1)
            return "1 task(s) are still open:\n  #1 [pending] Always open\nDo not end the cycle yet."

        log = MagicMock()
        with patch("agent._NUDGE_ENABLED", True), \
             patch("agent._MAX_TOTAL_NUDGES", 10), \
             patch("agent._MAX_TEXT_ONLY", 10), \
             patch("agent._llm_request") as mock_llm, \
             patch("agent._emit"), \
             patch("agent._build_open_task_nudge", side_effect=_nudge_side_effect):

            mock_llm.side_effect = responses

            def _exec(**kwargs):
                if "git commit" in kwargs.get("command", ""):
                    return "exit=0\n[main abc1234] C1: work"
                return "exit=0"

            with patch.dict("agent.MAP_FN", {"exec_command": _exec}):
                result = agent.run_agent_single(
                    conversation_history=[],
                    summary_state={"text": "", "up_to": 0},
                    initial_files={},
                    log=log,
                    start_turn=0,
                )

        assert result == "done"
        assert len(nudge_calls) == 1, f"Expected nudge to fire exactly once, fired {len(nudge_calls)} times"


if __name__ == "__main__":
    unittest.main()
