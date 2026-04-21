import pytest
from unittest.mock import MagicMock, patch
import agent
import requests

def create_mock_response(content=""):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # iter_lines should return the "data: ..." format
    mock_resp.iter_lines.return_value = [
        b"data: " + content.encode("utf-8") if content else b"",
        b"data: [DONE]"
    ]
    return mock_resp

@pytest.fixture
def mock_env():
    with patch('agent._emit'), \
         patch('agent._llm_request') as mock_llm:
        yield mock_llm

def test_hard_overtime_cap(mock_env):
    """Test that the agent returns 'done' when hard overtime cap is reached."""
    with patch('agent._MAX_TURNS', 10):
        result = agent.run_agent_single(
            conversation_history=[],
            summary_state={"text": "", "up_to": 0},
            initial_files=None,
            log=MagicMock(),
            start_turn=20
        )
        assert result == "done"

def test_edit_deadline_nudge(mock_env):
    """Test that the edit nudge is sent when the deadline is reached without edits."""
    conversation_history = [{"role": "user", "content": "Start work"}]
    summary_state = {"text": "", "up_to": 0}
    
    mock_env.return_value = create_mock_response("cycle is complete")
    
    with patch('agent._NUDGE_ENABLED', True):
        # _EDIT_DEADLINE_TURN is 20.
        # We start at turn 19, so the first iteration of the loop (turn = start_turn + 1)
        # will make turn = 20.
        result = agent.run_agent_single(
            conversation_history=conversation_history,
            summary_state=summary_state,
            initial_files=None,
            log=MagicMock(),
            start_turn=19
        )
        
        nudge_found = any("[SYSTEM: You have spent" in msg.get("content", "") 
                          for msg in conversation_history)
        assert nudge_found, "Edit nudge should have been added to conversation history"

def test_wind_down_warning(mock_env):
    """Test that wind-down warning is generated."""
    conversation_history = [{"role": "user", "content": "Start work"}]
    summary_state = {"text": "", "up_to": 0}
    
    mock_env.return_value = create_mock_response("cycle is complete")
    
    with patch('agent._MAX_TURNS', 10), \
         patch('agent._WIND_DOWN_TURNS', 3):
        
        log_mock = MagicMock()
        agent.run_agent_single(
            conversation_history=conversation_history,
            summary_state=summary_state,
            initial_files=None,
            log=log_mock,
            start_turn=7 # first loop turn=8
        )
        
        log_messages = [call.args[0] % call.args[1:] for call in log_mock.info.call_args_list]
        assert any("Wind-down: 2 turns remaining" in msg for msg in log_messages)

def test_overtime_warning(mock_env):
    """Test that overtime warning is logged."""
    conversation_history = [{"role": "user", "content": "Start work"}]
    summary_state = {"text": "", "up_to": 0}
    
    mock_env.return_value = create_mock_response("cycle is complete")
    
    with patch('agent._MAX_TURNS', 10):
        log_mock = MagicMock()
        agent.run_agent_single(
            conversation_history=conversation_history,
            summary_state=summary_state,
            initial_files=None,
            log=log_mock,
            start_turn=10 # first loop turn=11
        )
        
        log_messages = [call.args[0] % call.args[1:] for call in log_mock.warning.call_args_list]
        assert any("Overtime: 1 turns past limit (10)" in msg for msg in log_messages)
