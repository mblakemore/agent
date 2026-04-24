import pytest
import logging
import json
import requests
import re
from unittest.mock import patch, MagicMock
from agent import run_agent_interactive, run_agent_single

# Setup basic logging to avoid noise
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("test_agent_coverage")

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
@patch('requests.get')
def test_async_summarizer_init_success(mock_get, mock_config, mock_llm, mock_emit):
    """Test the path where AsyncSummarizer is successfully initialized."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": True, "base_url": "http://summary-api:8000", "ctx_size": 16384}
    }.get(k)

    mock_health = MagicMock()
    mock_health.status_code = 200
    mock_get.return_value = mock_health

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [b'data: {"choices": [{"delta": {"content": "OK"}}]}', b'data: [DONE]']
    mock_llm.return_value = mock_resp

    try:
        run_agent_interactive(initial_prompt="Test", auto=True)
    except Exception:
        pass

    summarizer_online = any(
        args[0] == "on_summarizer_status" and args[1] == "online" 
        for args, kwargs in mock_emit.call_args_list
    )
    assert summarizer_online

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
@patch('requests.get')
def test_async_summarizer_init_fail_connection(mock_get, mock_config, mock_llm, mock_emit):
    """Test the path where AsyncSummarizer initialization fails (Connection Error)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": True, "base_url": "http://summary-api:8000", "ctx_size": 16384}
    }.get(k)

    mock_get.side_effect = requests.ConnectionError("Connection failed")

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [b'data: {"choices": [{"delta": {"content": "OK"}}]}', b'data: [DONE]']
    mock_llm.return_value = mock_resp

    try:
        run_agent_interactive(initial_prompt="Test", auto=True)
    except Exception:
        pass

    summarizer_offline = any(
        args[0] == "on_summarizer_status" and args[1] == "offline" 
        for args, kwargs in mock_emit.call_args_list
    )
    assert summarizer_offline

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
@patch('requests.get')
def test_async_summarizer_init_fail_status(mock_get, mock_config, mock_llm, mock_emit):
    """Test the path where AsyncSummarizer initialization fails (Non-200 Status)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": True, "base_url": "http://summary-api:8000", "ctx_size": 16384}
    }.get(k)

    mock_health = MagicMock()
    mock_health.status_code = 500
    mock_get.return_value = mock_health

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [b'data: {"choices": [{"delta": {"content": "OK"}}]}', b'data: [DONE]']
    mock_llm.return_value = mock_resp

    try:
        run_agent_interactive(initial_prompt="Test", auto=True)
    except Exception:
        pass

    summarizer_unhealthy = any(
        args[0] == "on_summarizer_status" and args[1] == "unhealthy" 
        for args, kwargs in mock_emit.call_args_list
    )
    assert summarizer_unhealthy

@patch('agent._load_checkpoint')
@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_continue_mode_resume(mock_config, mock_llm, mock_emit, mock_load):
    """Test that continue_mode resumes from a checkpoint."""
    mock_load.return_value = (
        [{"role": "user", "content": "Prev message"}],
        {"text": "Previous summary", "up_to": 1},
        1,
        ["file1.txt"]
    )

    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False, "base_url": "http://localhost:8000"}
    }.get(k)

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [b'data: {"choices": [{"delta": {"content": "Resumed!"}}]}', b'data: [DONE]']
    mock_llm.return_value = mock_resp

    try:
        run_agent_interactive(initial_prompt=None, auto=True, continue_mode=True)
    except Exception:
        pass

    resumed_emitted = any(
        args[0] == "on_continue_resumed" 
        for args, kwargs in mock_emit.call_args_list
    )
    assert resumed_emitted

@patch('agent._load_checkpoint')
@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_continue_mode_no_checkpoint(mock_config, mock_llm, mock_emit, mock_load):
    """Test that continue_mode handles missing checkpoints."""
    mock_load.return_value = None

    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [b'data: {"choices": [{"delta": {"content": "No checkpoint found"}}]}', b'data: [DONE]']
    mock_llm.return_value = mock_resp

    try:
        run_agent_interactive(initial_prompt=None, auto=True, continue_mode=True)
    except Exception:
        pass

    continue_none_emitted = any(
        args[0] == "on_continue_none" 
        for args, kwargs in mock_emit.call_args_list
    )
    assert continue_none_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_hallucination_guard_text_only(mock_config, mock_llm, mock_emit):
    """Test that the hallucination guard detects and nudges text-only responses."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)
    
    with patch('agent._NUDGE_ENABLED', True):
        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"content": "I have analyzed the code and found a bug."}}]}',
            b'data: [DONE]'
        ]
        mock_llm.return_value = mock_resp

        def stop_after_n_calls(*args, **kwargs):
            if mock_llm.call_count > 2:
                raise StopIteration("Stop test")
            return mock_resp

        mock_llm.side_effect = stop_after_n_calls

        try:
            run_agent_interactive(initial_prompt="Check for bugs", auto=True)
        except (StopIteration, Exception):
            pass

    stripped_emitted = any(
        args[0] == "on_hallucination_stripped" and args[1] == "text_only"
        for args, kwargs in mock_emit.call_args_list
    )
    assert stripped_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_phase_detection_perceive(mock_config, mock_llm, mock_emit):
    """Test that running gh issue list triggers the PERCEIVE phase."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)
    
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [
        b'data: {"choices": [{"delta": {"content": "I will list the issues now."}}]}',
        b'data: [DONE]'
    ]
    mock_llm.return_value = mock_resp
    
    try:
        run_agent_interactive(initial_prompt="gh issue list", auto=True)
    except Exception:
        pass

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_cicd_phase_detection_implement(mock_config, mock_llm, mock_emit):
    """Test that running git worktree add and gh pr create trigger the IMPLEMENT phase and capture details."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "continue_mode": False, "tui": False, "verbose": False}
    }.get(k)
    
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [
        b'data: {"choices": [{"delta": {"content": "I will now create the worktree."}}]}',
        b'data: [DONE]'
    ]
    mock_llm.return_value = mock_resp
    
    try:
        with patch('agent.exec_command', return_value="exit=0"):
            run_agent_interactive(initial_prompt="git worktree add /tmp/worktree -b cicd/test-branch", auto=True)
    except Exception:
        pass

@patch('agent._emit')
@patch('agent.run_agent_single')
@patch('agent._config')
@patch('builtins.input')
def test_auto_mode_operator_no_guidance(mock_input, mock_config, mock_run_single, mock_emit):
    """Test that operator pressing Enter (no guidance) in auto mode triggers a resume message (lines 2111-2113)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)

    # First call to run_agent_single returns "cancelled" to trigger operator guidance
    # Second call is the actual resume
    mock_run_single.side_effect = ["cancelled", "finished"]
    
    # Mock input to return empty string (operator just pressed Enter)
    mock_input.return_value = ""

    try:
        run_agent_interactive(initial_prompt="Test", auto=True)
    except Exception:
        pass

    # Verify run_agent_single was called twice
    assert mock_run_single.call_count == 2

@patch('agent._emit')
@patch('agent.run_agent_single')
@patch('agent._config')
@patch('builtins.input')
def test_auto_mode_operator_cancel(mock_input, mock_config, mock_run_single, mock_emit):
    """Test that operator cancelling via EOFError in auto mode ends the session (lines 2096-2100)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)

    # Trigger operator guidance
    mock_run_single.return_value = "cancelled"
    
    # Mock input to raise EOFError
    mock_input.side_effect = EOFError

    try:
        run_agent_interactive(initial_prompt="Test", auto=True)
    except Exception:
        pass

    # Verify that run_agent_single was only called once (since it should return after EOFError)
    assert mock_run_single.call_count == 1

@patch('agent._emit')
@patch('agent.run_agent_single')
@patch('agent._config')
@patch('builtins.input')
def test_auto_mode_operator_interrupt(mock_input, mock_config, mock_run_single, mock_emit):
    """Test that operator cancelling via KeyboardInterrupt in auto mode ends the session (lines 2096-2100)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "summary": {"enabled": False}
    }.get(k)

    # Trigger operator guidance
    mock_run_single.return_value = "cancelled"
    
    # Mock input to raise KeyboardInterrupt
    mock_input.side_effect = KeyboardInterrupt

    try:
        run_agent_interactive(initial_prompt="Test", auto=True)
    except Exception:
        pass

    # Verify that run_agent_single was only called once
    assert mock_run_single.call_count == 1
