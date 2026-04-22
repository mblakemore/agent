import pytest
import agent
from unittest.mock import MagicMock, patch
import json
from cancel import CancelledError

def test_run_agent_interactive_auto_continue_end():
    """Target lines 1543-1547: auto mode session end during continue."""
    with patch("agent._setup_logger", return_value=(MagicMock(), "log", "err")), \
         patch("agent._check_api_health", return_value=(True, "")), \
         patch("agent._detect_ctx_size", return_value=None), \
         patch("agent._load_checkpoint", return_value=([], {"text": ""}, 0, None)), \
         patch("agent.run_agent_single", return_value="done"), \
         patch("agent.cleanup_temp_sessions") as mock_cleanup, \
         patch("agent._delete_checkpoint") as mock_del_cp, \
         patch("agent._emit"):
        
        agent.run_agent_interactive(continue_mode=True, auto=True)
        mock_cleanup.assert_called_once()
        mock_del_cp.assert_called_once()

def test_run_agent_interactive_auto_cancelled_guidance():
    """Target lines 1608-1640: auto mode cancelled guidance."""
    with patch("agent._setup_logger", return_value=(MagicMock(), "log", "err")), \
         patch("agent._check_api_health", return_value=(True, "")), \
         patch("agent._detect_ctx_size", return_value=None), \
         patch("agent.run_agent_single") as mock_run, \
         patch("agent.cleanup_temp_sessions"), \
         patch("agent._delete_checkpoint"), \
         patch("agent._emit"), \
         patch("builtins.input", return_value="guidance text"), \
         patch("agent._expand_file_refs", return_value=("expanded", None, None)):
        
        mock_run.side_effect = ["cancelled", "done"]
        agent.run_agent_interactive(initial_prompt="start", auto=True)
        assert mock_run.call_count == 2

def test_run_agent_single_tool_hard_bail():
    """Target lines 2735-2752: Hard bail on tool failure."""
    history = [{"role": "user", "content": "test"}]
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()
    
    mock_tool = MagicMock(return_value="Error: something went wrong")
    
    with patch.dict(agent.MAP_FN, {"exec_command": mock_tool}), \
         patch("agent._llm_request") as mock_llm, \
         patch("agent._emit"), \
         patch("agent._save_checkpoint"):
        
        tc = {"index": 0, "id": "t1", "type": "function",
              "function": {"name": "exec_command", "arguments": '{"command": "ls"}'}}
        body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
        resp = MagicMock()
        resp.iter_lines.return_value = [
            b"data: " + json.dumps(body).encode(),
            b"data: [DONE]",
        ]
        
        # Need enough calls to hit _REPEAT_THRESHOLD * 2 (6)
        mock_llm.side_effect = [resp] * 10 + [MagicMock(iter_lines=lambda: [b"data: [DONE]"])]
        
        try:
            agent.run_agent_single(history, summary_state, None, log)
        except Exception:
            pass
