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
def test_async_summarizer_init_fail(mock_get, mock_config, mock_llm, mock_emit):
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
    
    # To trigger the hallucination guard, _NUDGE_ENABLED must be True.
    # Since it's a global in agent.py, we need to patch it or ensure it's True.
    with patch('agent._NUDGE_ENABLED', True):
        # Simulate a text-only response (hallucination)
        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"content": "I have analyzed the code and found a bug."}}]}',
            b'data: [DONE]'
        ]
        mock_llm.return_value = mock_resp

        # We want to trigger the loop where it detects text-only and nudges.
        def stop_after_n_calls(*args, **kwargs):
            if mock_llm.call_count > 2:
                raise StopIteration("Stop test")
            return mock_resp

        mock_llm.side_effect = stop_after_n_calls

        try:
            run_agent_interactive(initial_prompt="Check for bugs", auto=True)
        except (StopIteration, Exception):
            pass

    # Verify that the hallucination stripped emit was called
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
