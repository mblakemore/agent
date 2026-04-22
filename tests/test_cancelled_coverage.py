import pytest
from unittest.mock import MagicMock, patch
import agent
from agent import run_agent_single, CancelledError
import json

def test_cancelled_error_coverage():
    """
    Test that CancelledError during tool execution is handled correctly,
    covering lines 2792-2799 in agent.py.
    """
    # Mock inputs
    history = [{"role": "user", "content": "Test"}]
    summary_state = {"text": "", "up_to": 0}
    initial_files = []
    log = MagicMock()
    
    # Mock parameters for run_agent_single
    temp, top_p, top_k, pres, max_t, ctx_s = 0.7, 1.0, 40, 0.0, 1024, 4096
    async_summarizer = MagicMock()
    
    # Mock a tool that raises CancelledError
    mock_tool = MagicMock(side_effect=CancelledError)
    
    # Mock LLM response
    mock_response = MagicMock()
    mock_response.status_code = 200
    
    def mock_iter_lines():
        # We need to ensure the agent parses the tool call and attempts to execute it.
        # The agent.py's _parse_llm_response processes the stream.
        tool_call_data = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_123",
                                "function": {
                                    "name": "mock_tool",
                                    "arguments": "{}"
                                }
                            }
                        ]
                    }
                }
            ]
        }
        yield f"data: {json.dumps(tool_call_data)}"
        yield "data: [DONE]"
    
    mock_response.iter_lines.return_value = mock_iter_lines()
    
    # Patch MAP_FN to include the tool, _llm_request to return our mock response,
    # and the side-effect functions to verify their calls.
    with patch.dict('agent.MAP_FN', {'mock_tool': mock_tool}), \
         patch('agent._emit') as mock_emit, \
         patch('agent._save_checkpoint') as mock_save, \
         patch('agent._llm_request', return_value=mock_response):
        
        # To avoid the "done" return result, we must ensure that the tool execution
        # actually happens. If it returns "done", it means the tool execution loop
        # was either skipped or finished without triggering the exception.
        
        result = run_agent_single(
            history, summary_state, initial_files, log,
            temp, top_p, top_k, pres, max_t, ctx_s,
            async_summarizer=async_summarizer
        )
        
        assert result == "cancelled", f"Expected 'cancelled', got {result}"
        mock_emit.assert_any_call("on_cancelled", "tool_execution")
        async_summarizer.drain.assert_called_once()
        async_summarizer.harvest.assert_called_once_with(summary_state)
        mock_save.assert_called_once()
