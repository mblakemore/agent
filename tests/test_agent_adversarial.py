import pytest
import logging
import json
import requests
from unittest.mock import patch, MagicMock
from pathlib import Path
from agent import run_agent_single, CancelledError

# Setup basic logging to avoid noise
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("test_agent_adversarial")

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
def test_text_loop_detection(mock_config, mock_llm, mock_emit):
    """Test that the agent detects a degenerate text loop (repeating the same output)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "cycle": {"max_turns": 20}
    }.get(k)

    loop_text = "I am stuck in a loop and cannot escape."
    mock_resp = create_mock_response(content=loop_text)
    mock_llm.side_effect = [mock_resp] * 20

    conversation_history = [{"role": "user", "content": "Start looping please."}]
    summary_state = {"text": "", "up_to": 0}

    with patch('agent._NUDGE_ENABLED', True):
        result = run_agent_single(conversation_history, summary_state, [], log)
    
    assert result == "done"
    text_loop_emitted = any(args[0] == "on_text_loop_detected" for args, kwargs in mock_emit.call_args_list)
    assert text_loop_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_hallucinated_file_read_detection(mock_config, mock_llm, mock_emit):
    """Test that the agent detects when the model claims to have read a file without calling the tool."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "cycle": {"max_turns": 20}
    }.get(k)

    hallucinated_text = "The contents of agent.py show a bug in the loop detection."
    mock_resp = create_mock_response(content=hallucinated_text)
    
    with patch('tools.file._accessed_files', set()):
        # Provide many responses to avoid StopIteration
        mock_llm.side_effect = [mock_resp] * 10

        conversation_history = [{"role": "user", "content": "Tell me what is in agent.py"}]
        summary_state = {"text": "", "up_to": 0}

        with patch('agent._NUDGE_ENABLED', True):
            # We only need to run it for a few turns to see if the hallucination is detected
            # Since run_agent_single is a while True loop, we might need to mock its termination
            # or just let it run until it hits a limit.
            # Actually, if hallucination is detected, it nudges and continues.
            # To make the test finish, we can patch _MAX_TURNS.
            with patch('agent._MAX_TURNS', 5):
                run_agent_single(conversation_history, summary_state, [], log)

    hallucination_emitted = any(
        args[0] == "on_hallucination_stripped" and args[1] == "file_read" 
        for args, kwargs in mock_emit.call_args_list
    )
    assert hallucination_emitted

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_text_only_stop(mock_config, mock_llm, mock_emit):
    """Test that agent stops on text-only response when nudging is disabled."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
        "cycle": {"max_turns": 20}
    }.get(k)

    mock_resp = create_mock_response(content="I have no tool calls to make.")
    mock_llm.return_value = mock_resp

    conversation_history = [{"role": "user", "content": "Hello"}]
    summary_state = {"text": "", "up_to": 0}

    with patch('agent._NUDGE_ENABLED', False):
        result = run_agent_single(conversation_history, summary_state, [], log)
    
    assert result == "done"
