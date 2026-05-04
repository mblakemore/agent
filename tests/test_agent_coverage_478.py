import pytest
from unittest.mock import MagicMock, patch
import agent
from agent import run_agent_single, MAP_FN
from cancel import CancelledError

def test_agent_tool_execution_cancelled_error():
    """
    Test that when a tool execution is cancelled (raises CancelledError),
    the agent handles it gracefully without crashing the main loop.
    """
    
    # 1. Define a mock tool function that raises the correct CancelledError
    def mock_cancelled_tool(*args, **kwargs):
        raise CancelledError("Tool execution was cancelled")

    tool_name = "test_cancelled_tool"
    
    # Store original tool if it exists to restore later
    original_tool = MAP_FN.get(tool_name)
    MAP_FN[tool_name] = mock_cancelled_tool

    try:
        # 2. Mock stream_chat to simulate a tool call response
        chunk1 = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_123",
                                "function": {
                                    "name": tool_name,
                                    "arguments": "{}"
                                }
                            }
                        ]
                    },
                    "finish_reason": None
                }
            ]
        }
        chunk2 = {
            "choices": [
                {
                    "delta": {},
                    "finish_reason": "tool_calls"
                }
            ]
        }
        
        with patch.object(agent._main_backend, 'stream_chat', return_value=[chunk1, chunk2]):
            mock_log = MagicMock()
            summary_state = {"text": "", "up_to": 0}
            
            # We expect it to hit the CancelledError handler and return "cancelled"
            result = run_agent_single(
                [], summary_state, [], mock_log, 0.7, 1.0, 40, 0.0
            )
            
            assert result == "cancelled"

    finally:
        # Cleanup: Restore MAP_FN
        if original_tool:
            MAP_FN[tool_name] = original_tool
        else:
            del MAP_FN[tool_name]

def test_tool_dispatch_no_stdout_leakage(capsys):
    """Dispatcher must not print to stdout during normal tool execution (#855).

    Stray debug print() calls were committed in the tool dispatch block. Any
    print to stdout pollutes the user-visible terminal and leaks argument payloads.
    """
    def mock_echo_tool(**kwargs):
        return "ok"

    tool_name = "test_echo_tool_855"
    original_tool = MAP_FN.get(tool_name)
    MAP_FN[tool_name] = mock_echo_tool

    try:
        chunk1 = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_echo",
                                "function": {
                                    "name": tool_name,
                                    "arguments": '{"x": 1}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        }
        chunk2 = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        # Second LLM turn — return a plain text response so the loop ends.
        chunk3 = {
            "choices": [
                {
                    "delta": {"content": "done"},
                    "finish_reason": "stop",
                }
            ]
        }

        call_count = 0

        def _two_turn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return iter([chunk1, chunk2])
            return iter([chunk3])

        with patch.object(agent._main_backend, "stream_chat", side_effect=_two_turn):
            mock_log = MagicMock()
            summary_state = {"text": "", "up_to": 0}
            run_agent_single([], summary_state, [], mock_log, 0.7, 1.0, 40, 0.0)

        captured = capsys.readouterr()
        assert "DEBUG" not in captured.out, (
            f"Unexpected stdout from dispatcher: {captured.out!r}"
        )
    finally:
        if original_tool:
            MAP_FN[tool_name] = original_tool
        elif tool_name in MAP_FN:
            del MAP_FN[tool_name]


if __name__ == "__main__":
    pytest.main([__file__])
