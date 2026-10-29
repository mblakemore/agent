"""end_cycle must answer its own tool_call before the loop returns.

An -a run throws the history away, but an interactive session keeps it; an
assistant tool_call with no matching tool message is rejected by strict APIs
on the next turn (found live 2026-10-29: "no result was recorded").
"""

import logging

import agent
import tools.end_cycle as end_cycle_tool

log = logging.getLogger("test_end_cycle_tool_result")


def _tool_turn(tid, name, args):
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


def test_end_cycle_leaves_no_unanswered_tool_call(agent_loop_mocks):
    agent.MAP_FN["end_cycle"] = end_cycle_tool.fn          # the real tool: returns the sentinel
    agent_loop_mocks.script(_tool_turn("ec1", "end_cycle", '{"summary": "x"}'))
    history = [{"role": "user", "content": "do the task"}]
    result = agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert result == "done"
    assert not agent._has_pending_tool_calls(history)
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "ec1" for m in history)
