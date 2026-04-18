import pytest
import logging
import json
import requests
from unittest.mock import patch, MagicMock
from agent import run_agent_single

# Setup basic logging to avoid noise
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("test_agent_loop")

def create_mock_response(content=None, tool_calls=None):
    """Helper to create a mock LLM response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    
    lines = []
    if tool_calls:
        # SSE format for tool calls
        for tc in tool_calls:
            payload = {"choices": [{"delta": {"tool_calls": [tc]}}]}
            lines.append(f"data: {json.dumps(payload)}".encode())
        lines.append(b'data: [DONE]')
    elif content:
        # SSE format for text content
        payload = {"choices": [{"delta": {"content": content}}]}
        lines.append(f"data: {json.dumps(payload)}".encode())
        lines.append(b'data: [DONE]')
    else:
        lines.append(b'data: [DONE]')
        
    mock_resp.iter_lines.return_value = lines
    return mock_resp

@patch('agent._emit')        # Top -> Last Arg
@patch('agent._llm_request') # Middle -> Middle Arg
@patch('agent._config')       # Bottom -> First Arg
def test_run_agent_single_direct_answer(mock_config, mock_llm, mock_emit):
    """Test the 'happy path' where the agent gives a direct answer."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    mock_llm.return_value = create_mock_response(content="This is the answer.")

    conversation_history = [{"role": "user", "content": "What is 1+1?"}]
    summary_state = {"text": "", "up_to": 0}
    
    run_agent_single(conversation_history, summary_state, [], log)
    
    assert mock_emit.called

@patch('agent._emit')        # Top -> Last Arg
@patch('agent._llm_request') # Middle -> Middle Arg
@patch('agent._config')       # Bottom -> First Arg
def test_run_agent_single_tool_loop(mock_config, mock_llm, mock_emit):
    """Test that the agent detects a tool-call loop."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    tool_call = {"index": 0, "id": "call_1", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}}
    mock_resp = create_mock_response(tool_calls=[tool_call])
    
    mock_llm.side_effect = [mock_resp, mock_resp, mock_resp, mock_resp, create_mock_response(content="Loop detected!")]

    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}
    
    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: "No results found."}):
        run_agent_single(conversation_history, summary_state, [], log)

    assert mock_llm.call_count <= 6

@patch('agent._emit')        # Top -> Last Arg
@patch('agent._llm_request') # Bottom -> First Arg
def test_run_agent_single_error_handling(mock_llm, mock_emit):
    """Test that the agent handles LLM request failures gracefully."""
    mock_llm.side_effect = requests.exceptions.RequestException("Network Timeout")

    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    
    try:
        run_agent_single(conversation_history, summary_state, [], log)
    except Exception:
        pass
        
    error_emitted = any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)
    assert error_emitted

