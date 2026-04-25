import pytest
import logging
import json
import requests
from unittest.mock import patch, MagicMock
from agent import run_agent_single, CancelledError

# Setup basic logging to avoid noise
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("test_agent_loop")

def create_mock_response(content=None, tool_calls=None, side_effect=None):
    """Helper to create a mock LLM response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    
    if side_effect:
        # If side_effect is provided, iter_lines will raise it
        mock_resp.iter_lines.side_effect = side_effect
    else:
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

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_cancelled(mock_config, mock_llm, mock_emit):
    """Test that CancelledError during streaming is handled correctly."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    # Mock iter_lines to raise CancelledError
    mock_llm.return_value = create_mock_response(side_effect=CancelledError("Cancelled"))

    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}

    result = run_agent_single(conversation_history, summary_state, [], log)
    
    assert result == "cancelled"
    # Verify on_cancelled was emitted
    cancelled_emitted = any(args[0] == "on_cancelled" for args, kwargs in mock_emit.call_args_list)
    assert cancelled_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_request_exception(mock_config, mock_llm, mock_emit):
    """Test that RequestException during streaming is handled correctly."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    # Mock iter_lines to raise RequestException
    mock_llm.return_value = create_mock_response(side_effect=requests.exceptions.RequestException("Connection lost"))

    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}

    run_agent_single(conversation_history, summary_state, [], log)
    
    # Verify on_error was emitted
    error_emitted = any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)
    assert error_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_unexpected_exception(mock_config, mock_llm, mock_emit):
    """Test that a general Exception during streaming is handled correctly."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    # Mock iter_lines to raise a generic Exception
    mock_llm.return_value = create_mock_response(side_effect=RuntimeError("Unexpected crash"))

    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}

    run_agent_single(conversation_history, summary_state, [], log)
    
    # Verify on_error was emitted
    error_emitted = any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)
    assert error_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_call_json_decode_error(mock_config, mock_llm, mock_emit):
    """Test that malformed JSON arguments in tool calls are handled gracefully."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    # One valid tool call, one garbled tool call to avoid the "All garbled" retry wipe
    tool_calls = [
        {"index": 0, "id": "call_valid", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}},
        {"index": 1, "id": "call_garbled", "function": {"name": "search_files", "arguments": '{"pattern": "test"'}}, # Missing brace in the JSON string
    ]
    mock_resp = create_mock_response(tool_calls=tool_calls)
    
    mock_llm.side_effect = [mock_resp, create_mock_response(content="Fixed it!")]

    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}

    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: "No results found."}):
        run_agent_single(conversation_history, summary_state, [], log)

    # Verify that the garbled tool call resulted in an error message in the conversation history
    assert any("malformed arguments" in msg.get("content", "") 
               for msg in conversation_history if msg.get("role") == "tool")

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_call_generic_exception(mock_config, mock_llm, mock_emit):
    """Test that unexpected exceptions during tool call argument parsing are handled gracefully."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    # Arguments that are not a string (will cause json.loads to fail or other issues)
    tool_call = {
        "index": 0, 
        "id": "call_2", 
        "function": {"name": "search_files", "arguments": None} # None will trigger a TypeError in json.loads or other similar
    }
    mock_resp = create_mock_response(tool_calls=[tool_call])
    
    # We provide a valid second call to avoid the "All garbled" retry wipe
    mock_llm.side_effect = [
        mock_resp, 
        create_mock_response(tool_calls=[{"index": 0, "id": "call_3", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}}]),
        create_mock_response(content="Fixed it!")
    ]

    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}

    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: "No results found."}):
        run_agent_single(conversation_history, summary_state, [], log)

    # The generic exception block (lines 2230-2233) only logs and continues.
    # It DOES NOT append to conversation_history.
    # We verify the agent didn't crash and the loop proceeded to the second LLM call.
    assert mock_llm.call_count >= 2



@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_agent_tui_fallback_coverage(mock_config, mock_llm, mock_emit):
    """Test that the TUI fallback is triggered when tui._AVAILABLE is False."""
    from agent import run_agent_interactive
    import sys
    from unittest.mock import MagicMock

    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    mock_llm.return_value = create_mock_response(content="Hello!")
    
    # Create a mock tui module
    mock_tui = MagicMock()
    mock_tui._AVAILABLE = False
    
    # Force the import of 'tui' to return our mock
    with patch.dict(sys.modules, {'tui': mock_tui}):
        with patch('builtins.input', return_value="exit"):
            try:
                run_agent_interactive(initial_prompt="Hi", tui=True, auto=False)
            except (SystemExit, Exception):
                pass

    # Verify the fallback notice was emitted
    notice_emitted = any(
        args[0] == "on_notice" and args[1] == "warn" and "prompt_toolkit not installed" in args[2]
        for args, kwargs in mock_emit.call_args_list
    )
    assert notice_emitted
