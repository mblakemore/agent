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

_mock_cfg_nudge = lambda k: {
    "llm": {"model": "test-model"},
    "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
    "context": {"max_tokens": 4096, "ctx_size": 32768}
}.get(k)

_think_tool_nudge = {"index": 0, "id": "tc1", "function": {
    "name": "think", "arguments": '{"content": "x"}'
}}

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_nudge_budget_exhausted(mock_config, mock_llm, mock_emit):
    """Nudge budget exhaustion stops the cycle (covers agent.py lines 2993-2995).
    _MAX_TOTAL_NUDGES=0 means the first text-only response exceeds the budget."""
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    mock_llm.return_value = create_mock_response(content="Working on it.")
    with patch('agent._NUDGE_ENABLED', True), patch('agent._MAX_TOTAL_NUDGES', 0):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_consecutive_text_only_limit(mock_config, mock_llm, mock_emit):
    """Consecutive text-only cap stops the cycle (covers agent.py lines 2998-2999).
    Default _MAX_TEXT_ONLY=3: strip on turn 1, nudge on turn 2, cap on turn 3."""
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    mock_llm.return_value = create_mock_response(content="Working on it.")
    with patch('agent._NUDGE_ENABLED', True):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_completion_signal_with_persisted_work(mock_config, mock_llm, mock_emit):
    """Completion signal after git commit stops the cycle (covers agent.py lines 2968-2969).
    exec_command(git commit) sets _has_committed=True; then 'cycle complete' content stops."""
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    commit_tool = {"index": 0, "id": "tc1", "function": {
        "name": "exec_command",
        "arguments": '{"command": "git commit -m CICD 468: add tests"}'
    }}
    mock_llm.side_effect = [
        create_mock_response(tool_calls=[commit_tool]),
        create_mock_response(content="Improvement cycle is complete."),
    ]
    with patch('agent._NUDGE_ENABLED', True), \
         patch.dict('agent.MAP_FN', {"exec_command": lambda **kwargs: "exit=0\n[main abc] CICD"}):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_overtime_text_only_stop(mock_config, mock_llm, mock_emit):
    """Text-only response in overtime stops the cycle (covers agent.py lines 2984-2986).
    _MAX_TURNS=2: two tool-call turns then a text-only at turn 3 triggers overtime stop."""
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    mock_llm.side_effect = [
        create_mock_response(tool_calls=[_think_tool_nudge]),
        create_mock_response(tool_calls=[_think_tool_nudge]),
        create_mock_response(content="Just some text."),
    ]
    with patch('agent._NUDGE_ENABLED', True), patch('agent._MAX_TURNS', 2), \
         patch.dict('agent.MAP_FN', {"think": lambda **kwargs: ""}):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_completion_signal_ignored_no_work(mock_config, mock_llm, mock_emit):
    """Completion signal without persisted work is ignored (covers agent.py lines 2970-2971).
    No git commit means _has_persisted_work=False; signal is logged but loop continues."""
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    mock_llm.return_value = create_mock_response(content="Improvement cycle is complete.")
    with patch('agent._NUDGE_ENABLED', True):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_hallucinated_file_read_correction(mock_config, mock_llm, mock_emit):
    """Hallucinated file read triggers correction nudge (covers agent.py lines 3015-3022).
    Turn 1: tool call. Turn 2: text-only #1 (stripped). Turn 3: claims to read agent.py
    without using file tool — triggers _detect_hallucinated_read guard."""
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    mock_llm.side_effect = [
        create_mock_response(tool_calls=[_think_tool_nudge]),
        create_mock_response(content="Still thinking."),
        create_mock_response(content="I read agent.py and found _MAX_TURNS = 250."),
        create_mock_response(content="OK."),
    ]
    conversation_history = [{"role": "user", "content": "test"}]
    with patch('agent._NUDGE_ENABLED', True), patch('agent._MAX_TOTAL_NUDGES', 3), \
         patch.dict('agent.MAP_FN', {"think": lambda **kwargs: ""}):
        result = run_agent_single(conversation_history, {"text": "", "up_to": 0}, [], log)
    assert result == "done"
    assert any("did NOT actually read" in str(msg.get("content", ""))
               for msg in conversation_history if msg.get("role") == "user")
