import pytest
from unittest.mock import MagicMock, patch
import agent
import requests
import json

def create_mock_response(content=""):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
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
            start_turn=7
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
            start_turn=10
        )
        
        log_messages = [call.args[0] % call.args[1:] for call in log_mock.warning.call_args_list]
        assert any("Overtime: 1 turns past limit (10)" in msg for msg in log_messages)

def test_file_tool_missing_path_validation(mock_env):
    """Test that 'file' tool call missing 'path' is caught and generates error."""
    conversation_history = [{"role": "user", "content": "Read some file"}]
    summary_state = {"text": "", "up_to": 0}
    
    # Mock LLM tool call response
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = ""
    mock_response.choices[0].message.tool_calls = [
        MagicMock(
            id="call_123",
            function=MagicMock(
                name="file",
                arguments=json.dumps({"action": "read"}) # Missing 'path'
            )
        )
    ]
    mock_env.return_value = mock_response
    
    # We must ensure the 'file' tool is in MAP_FN to avoid "Unknown tool" error
    # but it's already there by default in agent.py.
    # To prevent the actual file tool from failing with a TypeError (missing path),
    # we can mock the tool function itself.
    with patch.dict('agent.MAP_FN', {'file': MagicMock(side_effect=TypeError("missing path"))}):
        log_mock = MagicMock()
        agent.run_agent_single(
            conversation_history=conversation_history,
            summary_state=summary_state,
            initial_files=None,
            log=log_mock,
            start_turn=0
        )
    
    error_found = any("Error: your tool call was garbled — 'path' is missing" in msg.get("content", "") 
                      for msg in conversation_history)
    assert error_found, "Corrective error for missing 'path' should be in history"

def test_cicd_pr_capture_and_warning(mock_env):
    """Test that PR number is captured and missing 'Closes #N' triggers warning."""
    conversation_history = [{"role": "user", "content": "Create PR"}]
    summary_state = {"text": "", "up_to": 0}
    
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = ""
    mock_response.choices[0].message.tool_calls = [
        MagicMock(
            id="call_456",
            function=MagicMock(
                name="exec_command",
                arguments=json.dumps({"command": "gh pr create --title test --body no-closes"})
            )
        )
    ]
    mock_env.return_value = mock_response
    
    # Mock the exec_command tool function in MAP_FN
    mock_exec_fn = MagicMock(return_value="Created PR: https://github.com/repo/pull/123\nexit=0")
    with patch.dict('agent.MAP_FN', {'exec_command': mock_exec_fn}):
        log_mock = MagicMock()
        agent.run_agent_single(
            conversation_history=conversation_history,
            summary_state=summary_state,
            initial_files=None,
            log=log_mock,
            start_turn=0
        )
        
        warning_found = any("PR #123 was created without a `Closes #<issue>`" in msg.get("content", "") 
                           for msg in conversation_history)
        assert warning_found, "Warning for missing Closes #N should be in history"
