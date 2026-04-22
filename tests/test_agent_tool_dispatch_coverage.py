import agent
import pytest
from unittest.mock import patch, MagicMock
import json
import logging

# Helper to create a mock LLM response with tool calls
def _resp_tool(tool_name, arguments_dict, tool_id="t1"):
    resp = MagicMock()
    resp.status_code = 200
    tc = {"index": 0, "id": tool_id, "type": "function",
          "function": {"name": tool_name, "arguments": json.dumps(arguments_dict)}}
    body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    resp.iter_lines.return_value = [f"data: {json.dumps(body)}".encode(), b"data: [DONE]"]
    return resp

# Helper to create a mock LLM response with text only
def _resp_text(text):
    resp = MagicMock()
    resp.status_code = 200
    body = {"choices": [{"delta": {"content": text}}]}
    resp.iter_lines.return_value = [f"data: {json.dumps(body)}".encode(), b"data: [DONE]"]
    return resp

@pytest.fixture
def mock_log():
    return MagicMock(spec=logging.Logger)

def test_tool_execution_exception(mock_log):
    """Test coverage for lines 2325-2329: Exception during tool execution."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    
    with patch.dict(agent.MAP_FN, {'fail_tool': lambda **kw: 1/0}):
        with patch('agent._llm_request') as mock_llm, \
             patch('agent._emit'):
            
            # Sequence: Tool call -> Text response to stop loop
            mock_llm.side_effect = [
                _resp_tool("fail_tool", {"arg": 1}),
                _resp_text("Done")
            ]
            
            with patch('agent._NUDGE_ENABLED', False):
                agent.run_agent_single(conversation_history, summary_state, None, mock_log)
            
            assert any("division by zero" in str(msg) for msg in conversation_history)

def test_tool_recovery_success(mock_log):
    """Test coverage for lines 2343-2344: Tool recovery succeeds."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    
    with patch.dict(agent.MAP_FN, {'fail_tool': lambda **kw: 1/0}):
        with patch('agent._llm_request') as mock_llm, \
             patch('agent._emit'), \
             patch('tool_recovery.attempt_recovery', return_value="Recovered result"):
            
            mock_llm.side_effect = [
                _resp_tool("fail_tool", {"arg": 1}),
                _resp_text("Done")
            ]
            
            with patch('agent._NUDGE_ENABLED', False):
                agent.run_agent_single(conversation_history, summary_state, None, mock_log)
            
            assert any("Recovered result" in str(msg) for msg in conversation_history)

def test_tool_recovery_failure(mock_log):
    """Test coverage for lines 2345-2346: Tool recovery fails."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    
    with patch.dict(agent.MAP_FN, {'fail_tool': lambda **kw: 1/0}):
        with patch('agent._llm_request') as mock_llm, \
             patch('agent._emit'), \
             patch('tool_recovery.attempt_recovery', side_effect=Exception("Recovery failed")):
            
            mock_llm.side_effect = [
                _resp_tool("fail_tool", {"arg": 1}),
                _resp_text("Done")
            ]
            
            with patch('agent._NUDGE_ENABLED', False):
                agent.run_agent_single(conversation_history, summary_state, None, mock_log)
            
            assert any("Recovery failed" in str(msg) or "division by zero" in str(msg) for msg in conversation_history)

def test_push_to_main_guardrail(mock_log):
    """Test coverage for lines 2392-2404: Detecting git push origin main."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    
    with patch.dict(agent.MAP_FN, {'exec_command': lambda command, **kw: "exit=0\nresult"}):
        with patch('agent._llm_request') as mock_llm, \
             patch('agent._emit'):
            
            mock_llm.side_effect = [
                _resp_tool("exec_command", {"command": "git push origin main"}),
                _resp_text("Done")
            ]
            
            with patch('agent._NUDGE_ENABLED', False):
                agent.run_agent_single(conversation_history, summary_state, None, mock_log)
            
            assert any("main" in str(msg) for msg in conversation_history)
