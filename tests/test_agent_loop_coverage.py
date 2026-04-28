import pytest
import logging
import json
from unittest.mock import patch, MagicMock
from agent import run_agent_single

# Setup basic logging to avoid noise
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("test_agent_loop_coverage")

def create_mock_response(content=None, tool_calls=None):
    """Helper to create a mock LLM response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
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
def test_grace_period_exhaustion(mock_config, mock_llm, mock_emit):
    """Covers agent.py lines 2978-2979.
    Required: _cycle_persisted = True AND grace_used >= _CYCLE_GRACE_TURNS (default 7).
    """
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    push_tool = {"index": 0, "id": "tc1", "function": {
        "name": "exec_command",
        "arguments": '{"command": "git push origin cicd/branch"}'
    }}
    
    # Turn 1: push (sets _cycle_persisted=True)
    # Turns 2-10: text-only (grace_used increments)
    # 10 turns > default _CYCLE_GRACE_TURNS = 7
    mock_llm.side_effect = [
        create_mock_response(tool_calls=[push_tool]),
        *[create_mock_response(content=f"Thinking {i}") for i in range(10)]
    ]
    
    mock_log = MagicMock()
    with patch('agent._NUDGE_ENABLED', True), \
         patch('agent._MAX_TEXT_ONLY', 20), \
         patch('agent._MAX_TOTAL_NUDGES', 20), \
         patch.dict('agent.MAP_FN', {"exec_command": lambda **kwargs: "exit=0\nPushed."}):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], mock_log)
    
    assert result == "done"
    # Verify the specific log message for grace period exhaustion was called
    # Note: the actual value in the log will be the default 7
    mock_log.info.assert_any_call("Stopping: cycle persisted %d turns ago, grace period exhausted", 7)

@patch('agent._emit')
@patch('agent._llm_request')
@patch('agent._config')
def test_consecutive_text_only_cap(mock_config, mock_llm, mock_emit):
    """Covers agent.py lines 2998-2999.
    Required: _consecutive_text_only >= _MAX_TEXT_ONLY (default 3).
    """
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768}
    }.get(k)
    
    # Use distinct content each call to avoid _TEXT_LOOP_THRESHOLD=3 firing before
    # the consecutive cap. The cap fires at turn 3 (_consecutive=3 >= _MAX_TEXT_ONLY=3).
    # We need more than 3 because the first one is stripped by the hallucination guard.
    mock_llm.side_effect = [create_mock_response(content=f"Working on it {i}") for i in range(10)]
    
    mock_log = MagicMock()
    with patch('agent._NUDGE_ENABLED', True), \
         patch('agent._MAX_TEXT_ONLY', 3), \
         patch('agent._MAX_TOTAL_NUDGES', 20):
        result = run_agent_single(
            [{"role": "user", "content": "test"}], {"text": "", "up_to": 0}, [], mock_log)
    
    assert result == "done"
    # Verify the specific log message for consecutive text-only cap was called
    mock_log.info.assert_any_call("Stopping: %d consecutive text-only responses", 3)
