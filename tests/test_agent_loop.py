import pytest
import logging
import json
import requests
from unittest.mock import patch, MagicMock
from agent import run_agent_single, CancelledError, _handle_auto_guidance

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
    mock_llm.return_value = create_mock_response(tool_calls=[
        {"index": 0, "id": "call_1", "function": {"name": "fail_tool", "arguments": "{}"}}
    ])
    conversation_history = [{"role": "user", "content": "Run fail tool"}]
    summary_state = {"text": "", "up_to": 0}
    with patch.dict('agent.MAP_FN', {"fail_tool": lambda **kwargs: exec('raise RuntimeError("fail")')}):
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
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    mock_llm.return_value = create_mock_response(content="Working on it.")
    with patch('agent._NUDGE_ENABLED', True), patch('agent._MAX_TEXT_ONLY', 3):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_completion_signal_with_persisted_work(mock_config, mock_llm, mock_emit):
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
    mock_config.__getitem__.side_effect = _mock_cfg_nudge
    mock_llm.side_effect = [
        create_mock_response(tool_calls=[_think_tool_nudge]),
        create_mock_response(content="Just some text."),
    ]
    with patch('agent._NUDGE_ENABLED', True), patch('agent._MAX_TURNS', 2), \
         patch.dict('agent.MAP_FN', {"think": lambda **kwargs: ""}), \
         patch('agent._MAX_TEXT_ONLY', 3):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], log)
    assert result == "done"

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_completion_signal_ignored_no_work(mock_config, mock_llm, mock_emit):
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
    mock_config.__getitem__.side_s_effect = _mock_cfg_nudge
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

# --- NEW TESTS FOR ISSUE #472 ---

@patch('agent.input')
@patch('agent.run_agent_single')
@patch('agent._emit')
def test_auto_guidance_with_input(mock_emit, mock_run, mock_input):
    # Setup
    mock_input.return_value = "Fix the typo in line 10"
    history = []
    
    # Execute
    result = _handle_auto_guidance(
        history, {}, [], MagicMock(), MagicMock(), 4096, 8192, MagicMock(), True, 0
    )
    
    # Assertions
    assert "Fix the typo in line 10" in history[-1]["content"]
    mock_run.assert_called_once()
    assert result == mock_run.return_value

@patch('agent.input')
@patch('agent.run_agent_single')
def test_auto_guidance_empty_input(mock_run, mock_input):
    # Setup: User just presses Enter
    mock_input.return_value = ""
    history = []
    
    # Execute
    _handle_auto_guidance(
        history, {}, [], MagicMock(), MagicMock(), 4096, 8192, MagicMock(), True, 0
    )
    
    # Assertions: Should use default "Please continue" message
    # Assertions: Should use default "Please continue" message
    assert history[-1]["content"] == "Continue where you left off. Finish your current cycle."
    mock_run.assert_called_once()

@patch('agent.input')
def test_auto_guidance_keyboard_interrupt(mock_input):
    # Setup: Simulate Ctrl+C
    mock_input.side_effect = KeyboardInterrupt
    history = []
    
    # Execute
    result = _handle_auto_guidance(
        history, {}, [], MagicMock(), MagicMock(), 0, 0, MagicMock(), True, 0
    )
    
    # Assertions
    assert result == "interrupted"
    assert len(history) == 0 # History should not be modified

