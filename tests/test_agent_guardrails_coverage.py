import pytest
import logging
import json
from unittest.mock import patch, MagicMock
from agent import run_agent_interactive

# Setup basic logging to avoid noise
logging.basicConfig(level=logging.ERROR)

def create_llm_response(content="", tool_calls=None):
    """Helper to create a mocked LLM response stream."""
    resp = MagicMock()
    lines = []
    
    # Content delta
    if content:
        body = {"choices": [{"delta": {"content": content}}]}
        lines.append(f"data: {json.dumps(body)}".encode('utf-8'))
    
    # Tool calls delta
    if tool_calls:
        # Add indices to tool calls as expected by the agent's streaming parser
        formatted_tc = []
        for i, tc in enumerate(tool_calls):
            tc_with_index = tc.copy()
            tc_with_index["index"] = i
            formatted_tc.append(tc_with_index)
            
        body = {"choices": [{"delta": {"tool_calls": formatted_tc}}]}
        lines.append(f"data: {json.dumps(body)}".encode('utf-8'))
        
    lines.append(b"data: [DONE]")
    resp.iter_lines.return_value = lines
    return resp

def get_messages_from_call(call):
    """Extracts 'messages' from the 'json' payload of a mock call."""
    args, kwargs = call
    request_body = kwargs.get('json', {})
    return request_body.get('messages', [])

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_validation_missing_path_v2(mock_config, mock_llm, mock_emit):
    """Test that a 'file' tool call without a 'path' argument triggers a retry (resetting history)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)

    # Response 1: Tool call missing 'path' (causes a 'retry' return)
    resp1 = create_llm_response(
        tool_calls=[{"id": "tool_1", "type": "function", "function": {"name": "file", "arguments": '{"action": "write", "content": "test content"}'}}]
    )
    
    # Response 2: Acknowledge error (text only)
    resp2 = create_llm_response(content="I apologize for the missing path.")
    
    mock_llm.side_effect = [resp1, resp2]

    try:
        run_agent_interactive(initial_prompt="Write a file without path", auto=True)
    except (StopIteration, Exception):
        pass
    
    # Verify the agent retried: the LLM should have been called twice.
    assert mock_llm.call_count >= 2, "Agent did not make a second LLM request after tool error retry"
    
    # On retry, the agent resets the turn. The second request should not contain the garbled tool call.
    messages = get_messages_from_call(mock_llm.call_args_list[1])
    # History should just be the initial prompt (1 message)
    assert len(messages) == 1, f"Expected history to be reset on retry, but found {len(messages)} messages"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_pr_capture_and_trailer_warning(mock_config, mock_llm, mock_emit):
    """Test capture of PR number and warning for missing 'Closes #N' trailer."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)

    # Response 1: Create PR without trailer (causes a tool result error, but not a retry)
    resp1 = create_llm_response(
        tool_calls=[{"id": "tool_1", "type": "function", "function": {"name": "exec_command", "arguments": '{"command": "gh pr create --title Test --body \'No trailer here\'"}'}}]
    )
    
    # Response 2: Acknowledge
    resp2 = create_llm_response(content="Done.")
    
    mock_llm.side_effect = [resp1, resp2]

    with patch('tools.exec_command.fn', return_value="pull/12345"):
        try:
            run_agent_interactive(initial_prompt="Create PR", auto=True)
        except (StopIteration, Exception):
            pass
        
        # Verify a second request was made
        assert mock_llm.call_count >= 2, "Agent did not make a second LLM request after PR creation"
        
        messages = get_messages_from_call(mock_llm.call_args_list[1])
        
        # Search for the block message in the tool results
        found_warning = False
        for msg in messages:
            if msg.get("role") == "tool" and "Error: CICD gh pr create blocked" in msg.get("content", ""):
                found_warning = True
                break
        
        if not found_warning:
            print(f"DEBUG: Messages in Turn 2: {messages}")
            
        assert found_warning, "Warning for missing 'Closes #N' trailer was not sent to LLM as a tool result"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_pr_capture_success(mock_config, mock_llm, mock_emit):
    """Test successful PR number capture with trailer present (no warning)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)

    # Response 1: Create PR with trailer
    resp1 = create_llm_response(
        tool_calls=[{"id": "tool_1", "type": "function", "function": {"name": "exec_command", "arguments": '{"command": "gh pr create --title Test --body \'Closes #123\'"}'}}]
    )
    
    # Response 2: Acknowledge
    resp2 = create_llm_response(content="Done.")
    
    mock_llm.side_effect = [resp1, resp2]

    with patch('tools.exec_command.fn', return_value="pull/54321"):
        try:
            run_agent_interactive(initial_prompt="Create PR", auto=True)
        except (StopIteration, Exception):
            pass
        
        # Verify a second request was made
        assert mock_llm.call_count >= 2, "Agent did not make a second LLM request after PR creation"
        
        messages = get_messages_from_call(mock_llm.call_args_list[1])
        found_warning = any(
            "Closes #<issue>" in msg.get("content", "")
            for msg in messages if msg.get("role") == "user"
        )
        assert not found_warning, "Warning was unexpectedly triggered when trailer was present"
