import pytest
from unittest.mock import MagicMock, patch
import agent
from agent import run_agent_single, MAP_FN, _main_backend
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
        
        with patch.object(_main_backend, 'stream_chat', return_value=[chunk1, chunk2]):
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

if __name__ == "__main__":
    pytest.main([__file__])
