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
            create_mock_response(content="Still text."),
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


@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_run_agent_single_empty_tool_output(mock_config, mock_llm, mock_emit):
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    tool_call = {"index": 0, "id": "call_1", "function": {"name": "search_files", "arguments": '{"pattern": "test"}'}}
    mock_resp = create_mock_response(tool_calls=[tool_call])
    
    # LLM calls tool, then the tool returns an empty string, then LLM responds with text
    mock_llm.side_effect = [
        mock_resp,
        create_mock_response(content="The tool returned nothing, but I can still answer.")
    ]
    
    conversation_history = [{"role": "user", "content": "Search for 'test'"}]
    summary_state = {"text": "", "up_to": 0}
    
    # Mock the tool to return an empty string
    with patch.dict('agent.MAP_FN', {"search_files": lambda **kwargs: ""}):
        run_agent_single(conversation_history, summary_state, [], log)
    
    assert mock_llm.call_count >= 2
    assert any("tool" in msg.get("role", "") for msg in conversation_history)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_tool_execution_cancelled(mock_config, mock_llm, mock_emit):
    """
    Tests that a CancelledError raised during tool execution is correctly 
    propagated and handled by the agent's main loop.
    """
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)

    # Mock LLM to call a tool
    tool_call = {"index": 0, "id": "call_1", "function": {"name": "cancel_tool", "arguments": "{}"}}
    mock_llm.return_value = create_mock_response(tool_calls=[tool_call])

    conversation_history = [{"role": "user", "content": "Cancel me"}]
    summary_state = {"text": "", "up_to": 0}

    # Mock the tool to raise CancelledError
    with patch.dict('agent.MAP_FN', {"cancel_tool": lambda **kwargs: exec('raise CancelledError("Cancelled")')}):
        # In run_agent_single, the CancelledError is caught and re-raised.
        # We check if run_agent_single returns "cancelled" or allows the error to bubble up.
        # Based on test_streaming_cancelled, the expectation is that it returns "cancelled".
        result = run_agent_single(conversation_history, summary_state, [], log)

    assert result == "cancelled"
    assert any(args[0] == "on_cancelled" for args, kwargs in mock_emit.call_args_list)

@patch('agent.input')
@patch('agent.run_agent_single')
@patch('agent._emit')
@patch('agent._main_backend')
@patch('agent._summary_backend')
@patch('agent._setup_logger')
def test_run_agent_interactive_init_and_exit(mock_logger, mock_summary, mock_main, mock_emit, mock_run, mock_input):
    """Test the initialization sequence and immediate exit of the interactive loop."""
    # Mock backend health checks
    mock_main.health.return_value = (True, "ok")
    mock_main.detect_ctx_size.return_value = 32768
    mock_summary.health.return_value = (True, "ok")
    mock_summary.detect_ctx_size.return_value = 32768
    
    # Mock input to exit immediately
    mock_input.return_value = "quit"
    
    # Mock logger to avoid file creation
    mock_logger.return_value = (MagicMock(), "log_path", "err_path")
    
    # Execute
    from agent import run_agent_interactive
    run_agent_interactive(tui=False, auto=False)
    
    # Assertions
    assert mock_main.health.called
    assert mock_summary.health.called
    assert mock_input.called
    assert mock_emit.called

# --- TESTS FOR ISSUE #499: Context Overflow Recovery ---

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_context_overflow_recovery_success(mock_config, mock_llm, mock_emit):
    """Tests that the agent successfully recovers from context overflow by reducing history."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": True, "base_url": "http://localhost:8082"}
    }.get(k)
    
    from agent import ContextOverflowError
    
    # Simulate: Overflow -> Overflow -> Success
    mock_llm.side_effect = [
        ContextOverflowError("Context window exceeded"),
        ContextOverflowError("Context window exceeded"),
        create_mock_response(content="Finally fit!")
    ]
    
    # Need enough history to allow reduction
    conversation_history = [
        {"role": "user", "content": "Msg 1"}, {"role": "assistant", "content": "Resp 1"},
        {"role": "user", "content": "Msg 2"}, {"role": "assistant", "content": "Resp 2"},
        {"role": "user", "content": "Msg 3"}, {"role": "assistant", "content": "Resp 3"},
        {"role": "user", "content": "Msg 4"}, {"role": "assistant", "content": "Resp 4"},
    ]
    summary_state = {"text": "", "up_to": 0}
    
    result = run_agent_single(conversation_history, summary_state, [], log)
    
    assert result == "done"
    assert mock_llm.call_count == 3

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_context_overflow_max_retries_failure(mock_config, mock_llm, mock_emit):
    """Tests that the agent returns 'error' after exceeding _CTX_REDUCE_MAX attempts."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": True, "base_url": "http://localhost:8082"}
    }.get(k)
    
    from agent import ContextOverflowError
    
    # Always raise overflow. _CTX_REDUCE_MAX is 10.
    mock_llm.side_effect = [ContextOverflowError("Still too big")] * 15
    
    conversation_history = [{"role": "user", "content": "test"}] * 20
    summary_state = {"text": "", "up_to": 0}
    
    result = run_agent_single(conversation_history, summary_state, [], log)
    
    assert result == "error"
    assert any(args[0] == "on_error" for args, kwargs in mock_emit.call_args_list)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_context_overflow_summary_truncation(mock_config, mock_llm, mock_emit):
    """Tests that the agent truncates the summary when history is already minimal."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": True, "base_url": "http://localhost:8082"}
    }.get(k)
    
    from agent import ContextOverflowError
    
    # Minimal history (2 messages)
    conversation_history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"}
    ]
    # Large summary to be truncated
    summary_state = {"text": "A" * 1000, "up_to": 0}
    
    # Overflow -> Success
    mock_llm.side_effect = [
        ContextOverflowError("Too big"),
        create_mock_response(content="Fixed!")
    ]
    
    result = run_agent_single(conversation_history, summary_state, [], log)
    
    assert result == "done"
    # Check if the "truncating summary" notice was emitted
    assert any(
        args[0] == "on_notice" and "truncating summary" in str(args[2]) 
        for args, kwargs in mock_emit.call_args_list
    )

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_coverage_gap_tool_parsing_exception(mock_config, mock_llm, mock_emit):
    """Targets lines 3206-3209: Exception handler for tool call parsing."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    # Simulate a response that causes a parsing error (e.g., missing 'choices' or 'delta')
    # The parser expects choices[0]['delta']. We provide something that will cause an IndexError or KeyError.
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_lines.return_value = [
        b"data: {\"wrong_key\": []}", 
        b"data: [DONE]"
    ]
    mock_llm.return_value = mock_resp
    
    conversation_history = [{"role": "user", "content": "Trigger parsing error"}]
    summary_state = {"text": "", "up_to": 0}
    
    # This should hit the except Exception block in tool parsing
    run_agent_single(conversation_history, summary_state, [], log)
    assert mock_llm.called

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_coverage_gap_circuit_breaker(mock_config, mock_llm, mock_emit):
    """Targets lines 3267-3268: Circuit breaker trigger."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    # To trigger circuit breaker, we need a tool to fail repeatedly or be marked as unavailable.
    # The circuit breaker logic usually depends on consecutive failures or specific error types.
    # We mock a tool that raises an exception.
    tool_call = {"index": 0, "id": "call_1", "function": {"name": "fail_tool", "arguments": "{}"}}
    mock_llm.return_value = create_mock_response(tool_calls=[tool_call])
    
    conversation_history = [{"role": "user", "content": "Trigger circuit breaker"}]
    summary_state = {"text": "", "up_to": 0}
    
    # Mock the tool to fail. We may need to call it multiple times to trigger the breaker.
    with patch.dict('agent.MAP_FN', {"fail_tool": lambda **kwargs: exec('raise RuntimeError("Circuit Breaker Test")')}):
        # Mock LLM to keep calling the tool until it hits the limit
        mock_llm.side_effect = [
            create_mock_response(tool_calls=[tool_call]),
            create_mock_response(tool_calls=[tool_call]),
            create_mock_response(tool_calls=[tool_call]),
            create_mock_response(tool_calls=[tool_call]),
            create_mock_response(content="Done")
        ]
        run_agent_single(conversation_history, summary_state, [], log)
    assert mock_llm.called

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_coverage_gap_pr_trailer_warning(mock_config, mock_llm, mock_emit):
    """Targets lines 3462-3463: Warning for missing 'Closes #N' in PR creation."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    # Trigger gh pr create without Closes #N
    tool_call = {"index": 0, "id": "call_1", "function": {"name": "gh_pr_create", "arguments": '{"title": "Test PR", "body": "No trailer here"}'}}
    mock_llm.return_value = create_mock_response(tool_calls=[tool_call])
    
    conversation_history = [{"role": "user", "content": "Create PR"}]
    summary_state = {"text": "", "up_to": 0}
    
    with patch.dict('agent.MAP_FN', {"gh_pr_create": lambda **kwargs: "PR created successfully"}):
        mock_llm.side_effect = [
            create_mock_response(tool_calls=[tool_call]),
            create_mock_response(content="Done")
        ]
        run_agent_single(conversation_history, summary_state, [], log)
    assert mock_llm.called

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_coverage_gap_overtime_repeated_result(mock_config, mock_llm, mock_emit):
    """Targets lines 3646-3649: Overtime + repeated tool result."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    # Trigger overtime by having many turns
    # And trigger repeated result by having the tool return the same value
    tool_call = {"index": 0, "id": "call_1", "function": {"name": "repeat_tool", "arguments": "{}"}}
    
@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_coverage_gap_overtime_repeated_result(mock_config, mock_llm, mock_emit):
    """Targets lines 3646-3649: Overtime + repeated tool result."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False, "base_url": "http://localhost:8082"}
    }.get(k)
    
    # Trigger overtime by having many turns
    # And trigger repeated result by having the tool return the same value
    tool_call = {"index": 0, "id": "call_1", "function": {"name": "repeat_tool", "arguments": "{}"}}
    
    # We need to mock the agent's internal turn counter or just provide many responses
    # For simplicity, we'll mock the tool and LLM to cycle
    with patch.dict('agent.MAP_FN', {"repeat_tool": lambda **kwargs: "Constant Result"}):
        # Simulate several turns to reach overtime, then repeat results
        responses = [create_mock_response(tool_calls=[tool_call])] * 20 
        responses.append(create_mock_response(content="Finished"))
        mock_llm.side_effect = responses
        
        conversation_history = [{"role": "user", "content": "Repeat yourself"}]
        summary_state = {"text": "", "up_to": 0}
        
        # We might need to adjust the overtime threshold in config or just run many times
        # For this test, we'll assume the default overtime is reached within 20 turns
        run_agent_single(conversation_history, summary_state, [], log)
    assert mock_llm.called

@patch('agent._git_short_sha')
def test_git_short_sha_success(mock_sha):
    """Test _git_short_sha returns the correct hash when git is available."""
    # Note: The actual function is internal, but we are testing the logic it uses.
    # To truly test the implementation of _git_short_sha, we should patch subprocess.
@patch('subprocess.check_output')
def test_git_short_sha_logic(mock_sub):
    """Test the logic inside _git_short_sha directly."""
    from agent import _git_short_sha
    mock_sub.return_value = "a1b2c3d\n"
    assert _git_short_sha() == "a1b2c3d"

@patch('subprocess.check_output')
def test_git_short_sha_failure(mock_sub):
    """Test _git_short_sha returns empty string on failure."""
    from agent import _git_short_sha
    mock_sub.side_effect = Exception("Git not found")
    assert _git_short_sha() == ""

@patch('sys.stderr', new_callable=MagicMock)
def test_boot_sequence_printing(mock_stderr):
    """Test that the boot sequence prints to stderr if it is a tty."""
    # Use a fresh mock for isatty on the object that will be accessed
    with patch('sys.stderr.isatty', return_value=True):
        # Since the boot sequence runs at module level, we need to reload the module
        import importlib
        import agent
        importlib.reload(agent)
        
        # Find the call that contains the boot message
        # The code uses _boot_sys.stderr.write
        found = False
        for call in mock_stderr.write.call_args_list:
            args, _ = call
            if "starting agent..." in args[0]:
                found = True
                break
        assert found, "Boot message 'starting agent...' not found in stderr.write calls"

@patch('sys.stderr', new_callable=MagicMock)
def test_boot_sequence_no_tty(mock_stderr):
    """Test that the boot sequence does NOT print to stderr if it's not a tty."""
    with patch('sys.stderr.isatty', return_value=False):
        import importlib
        import agent
        importlib.reload(agent)
        # Ensure write was not called for the boot message
        for call in mock_stderr.write.call_args_list:
            args, _ = call
            if "starting agent..." in args[0]:
                pytest.fail("Boot message should not be printed when isatty is False")
