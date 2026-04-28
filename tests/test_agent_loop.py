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
        mock_resp.iter_lines.side_effect = side_effect
    else:
        lines = []
        if tool_calls:
            for tc in tool_calls:
                payload = {"choices": [{"delta": {"tool_calls": [tc]}}]}
                lines.append(f"data: {json.dumps(payload)}".encode())
            lines.append(b'data: [DONE]')
        elif content:
            payload = {"choices": [{"delta": {"content": content}}]}
            lines.append(f"data: {json.dumps(payload)}".encode())
            lines.append(b'data: [DONE]')
        else:
            lines.append(b'data: [DONE]')
        
        mock_resp.iter_lines.return_value = lines
    return mock_resp

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_run_agent_single_direct_answer(mock_config, mock_llm, mock_emit):
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

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_run_agent_single_tool_loop(mock_config, mock_llm, mock_emit):
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

@patch('agent._emit')
@patch('agent._llm_request')
def test_run_agent_single_error_handling(mock_llm, mock_emit):
    mock_llm.side_effect = requests.exceptions.RequestException("Network Timeout")
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    try:
        run_agent_single(conversation_history, summary_state, [], log)
    except Exception:
        pass
    assert any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_cancelled(mock_config, mock_llm, mock_emit):
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(side_effect=CancelledError("Cancelled"))
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    result = run_agent_single(conversation_history, summary_state, [], log)
    assert result == "cancelled"
    assert any(args[0] == "on_cancelled" for args, kwargs in mock_emit.call_args_list)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_request_exception(mock_config, mock_llm, mock_emit):
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(side_effect=requests.exceptions.RequestException("Connection lost"))
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    run_agent_single(conversation_history, summary_state, [], log)
    assert any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_streaming_unexpected_exception(mock_config, mock_llm, mock_emit):
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(side_effect=RuntimeError("Unexpected crash"))
    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}
    run_agent_single(conversation_history, summary_state, [], log)
    assert any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_call_json_decode_error(mock_config, mock_llm, mock_emit):
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    tool_calls = [
        {"index": 0, "id": "call_valid", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}},
        {"index": 1, "id": "call_garbled", "function": {"name": "search_files", "arguments": '{"pattern": "test"'}},
    ]
    mock_resp = create_mock_response(tool_calls=tool_calls)
    mock_llm.side_effect = [mock_resp, create_mock_response(content="Fixed it!")]
    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}
    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: "No results found."}):
        run_agent_single(conversation_history, summary_state, [], log)
    assert any("malformed arguments" in msg.get("content", "") 
               for msg in conversation_history if msg.get("role") == "tool")

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_call_generic_exception(mock_config, mock_llm, mock_emit):
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    # Force a generic exception by mocking a tool function to raise one
    mock_llm.return_value = create_mock_response(tool_calls=[
        {"index": 0, "id": "call_1", "function": {"name": "fail_tool", "arguments": "{}"}}
    ])
    conversation_history = [{"role": "user", "content": "Run fail tool"}]
    summary_state = {"text": "", "up_to": 0}
    with patch.dict('agent.MAP_FN', {"fail_tool": lambda **kwargs: exec('raise RuntimeError("fail")')}):
        # We expect it to call LLM again after the exception
        mock_llm.side_effect = [
            create_mock_response(tool_calls=[{"index": 0, "id": "call_1", "function": {"name": "fail_tool", "arguments": "{}"}}]),
            create_mock_response(content="Fixed it!")
        ]
        run_agent_single(conversation_history, summary_state, [], log)
    assert mock_llm.call_count >= 2

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_text_only_completion_signal(mock_config, mock_llm, mock_emit):
    """Test that a completion signal ends the cycle when work is persisted."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(content="improvement cycle 466 is complete")
    conversation_history = [{"role": "user", "content": "test"}]
    summary_state = {"text": "", "up_to": 0}
    
    # We can't easily patch _has_committed if it's not a global.
    # But we can patch the components of _has_persisted_work if we can find them.
    # Since _has_persisted_work is calculated inside run_agent_single, 
    # we can patch _cicd_phase_state if it's a global.
    with patch('agent._cicd_phase_state', {"track": True}):
        result = run_agent_single(conversation_history, summary_state, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_text_only_overtime(mock_config, mock_llm, mock_emit):
    """Test that a text-only response in overtime ends the cycle."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(content="Just some text.")
    conversation_history = [{"role": "user", "content": "test"}]
    summary_state = {"text": "", "up_to": 0}
    with patch('agent._MAX_TURNS', 0):
        result = run_agent_single(conversation_history, summary_state, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_text_only_nudge_budget(mock_config, mock_llm, mock_emit):
    """Test that exceeding the total nudge budget ends the cycle."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(content="Just some text.")
    conversation_history = [{"role": "user", "content": "test"}]
    summary_state = {"text": "", "up_to": 0}
    with patch('agent._MAX_TOTAL_NUDGES', 0):
        result = run_agent_single(conversation_history, summary_state, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_text_only_consecutive_limit(mock_config, mock_llm, mock_emit):
    """Test that too many consecutive text-only responses end the cycle."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    mock_llm.return_value = create_mock_response(content="Just some text.")
    conversation_history = [{"role": "user", "content": "test"}]
    summary_state = {"text": "", "up_to": 0}
    with patch('agent._MAX_TEXT_ONLY', 0):
        result = run_agent_single(conversation_history, summary_state, [], log)
    assert result == "done"
