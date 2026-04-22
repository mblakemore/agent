import pytest
from agent import run_agent_single, CancelledError
from unittest.mock import MagicMock, patch
import json
import logging

def test_tool_cancellation_coverage():
    """
    Test that CancelledError during tool execution is handled correctly
    and covers the target lines in agent.py.
    """
    # Mock the tool function to raise CancelledError
    mock_tool = MagicMock(side_effect=CancelledError)
    
    # Mock _llm_request to return a tool call
    mock_response = MagicMock()
    tc = {"index": 0, "id": "t1", "type": "function",
          "function": {"name": "mock_tool", "arguments": "{}"}}
    body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    mock_response.status_code = 200
    mock_response.iter_lines.return_value = [f"data: {json.dumps(body)}".encode(), b"data: [DONE]"]
    
    with patch('agent._llm_request', return_value=mock_response):
        # MAP_FN is imported from tools. We patch it in agent.MAP_FN
        with patch('agent.MAP_FN', {'mock_tool': mock_tool}):
            with patch('agent._emit') as mock_emit, \
                 patch('agent._save_checkpoint') as mock_save:
                
                # Setup arguments for run_agent_single
                history = [{"role": "user", "content": "Hello"}]
                summary_state = {"text": "", "up_to": 0}
                initial_files = {}
                log = logging.getLogger("test")
                
                try:
                    # Call run_agent_single with correct arguments
                    run_agent_single(history, summary_state, initial_files, log)
                except Exception as e:
                    if isinstance(e, CancelledError):
                        pass
                    else:
                        pytest.fail(f"run_agent_single raised unexpected exception: {e}")
                
                # Verify the handler for CancelledError was hit
                mock_emit.assert_any_call("on_cancelled", "tool_execution")
                mock_save.assert_called()

