import pytest
from agent import run_agent_single
from unittest.mock import MagicMock, patch
import json
import logging

def test_tool_dispatch_error_handling():
    """
    Test tool dispatch error paths (lines 2400-2600).
    """
    # Mock response that triggers a tool call
    mock_response = MagicMock()
    tc = {"index": 0, "id": "t1", "type": "function",
          "function": {"name": "fail_tool", "arguments": "{}"}}
    body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    mock_response.status_code = 200
    mock_response.iter_lines.return_value = [f"data: {json.dumps(body)}".encode(), b"data: [DONE]"]

    # Mock tool that raises an exception
    mock_tool = MagicMock(side_effect=RuntimeError("Tool failed"))
    
    with patch('agent._llm_request', return_value=mock_response):
        with patch('agent.MAP_FN', {'fail_tool': mock_tool}):
            with patch('agent._emit') as mock_emit, \
                 patch('agent._save_checkpoint') as mock_save:
                
                history = [{"role": "user", "content": "Run fail_tool"}]
                summary_state = {"text": "", "up_to": 0}
                initial_files = {}
                log = logging.getLogger("test")
                
                try:
                    run_agent_single(history, summary_state, initial_files, log)
                except Exception:
                    pass
                
                # Check that on_error was emitted for tool failure
                found_error = any(call.args[0] == "on_error" for call in mock_emit.call_args_list)
                assert found_error, "on_error should be emitted when tool fails"
