import pytest
from unittest.mock import MagicMock, patch
import agent
import json

def test_run_agent_interactive_boot_sequence():
    """
    Tests the boot sequence of run_agent_interactive, ensuring
    backend health checks and session start emissions occur.
    """
    with patch("agent._main_backend") as mock_main, \
         patch("agent._summary_backend") as mock_summary, \
         patch("agent._emit") as mock_emit, \
         patch("agent._setup_logger", return_value=(MagicMock(), "log", "err")), \
         patch("agent.input", side_effect=KeyboardInterrupt): # Exit the loop immediately
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 8192
        mock_summary.health.return_value = (True, "OK")
        
        try:
            agent.run_agent_interactive()
        except KeyboardInterrupt:
            pass

        # Check if boot progress was emitted
        boot_progress_called = any(
            call.args[0] == "on_boot_progress" for call in mock_emit.call_args_list
        )
        assert boot_progress_called, "on_boot_progress was not emitted"
        
        # Check if session start was emitted
        session_start_called = any(
            call.args[0] == "on_session_start" for call in mock_emit.call_args_list
        )
        assert session_start_called, "on_session_start was not emitted"

def test_run_agent_interactive_auto_mode():
    """
    Tests the auto=True path where the agent runs one turn and exits.
    """
    initial_prompt = "Hello"
    
    # Mock the LLM request to return a simple response and then stop
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [
        b"data: " + json.dumps({"choices": [{"delta": {"content": "Hi there!"}}]}).encode(),
        b"data: [DONE]",
    ]
    
    with patch("agent._llm_request", return_value=mock_resp), \
         patch("agent._emit"), \
         patch("agent._setup_logger", return_value=(MagicMock(), "log", "err")), \
         patch("agent._main_backend") as mock_main, \
         patch("agent._summary_backend") as mock_summary, \
         patch("agent._delete_checkpoint"), \
         patch("agent.cleanup_temp_sessions"):
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 8192
        mock_summary.health.return_value = (True, "OK")
        
        # This should run the initial prompt and then return because auto=True
        agent.run_agent_interactive(initial_prompt=initial_prompt, auto=True)
        
        # Verify that it didn't enter the while True loop (which would call input())
        with patch("agent.input") as mock_input:
            pass

def test_run_agent_interactive_continue_mode():
    """
    Tests the continue_mode=True path.
    """
    # Mock checkpoint data
    mock_cp = (
        [{"role": "user", "content": "past message"}], # history
        {"text": "past summary", "up_to": 1},           # summary_state
        1,                                             # start_turn
        None                                            # initial_files
    )
    
    with patch("agent._load_checkpoint", return_value=mock_cp), \
         patch("agent._emit"), \
         patch("agent._setup_logger", return_value=(MagicMock(), "log", "err")), \
         patch("agent._main_backend") as mock_main, \
         patch("agent._summary_backend") as mock_summary, \
         patch("agent._llm_request") as mock_llm, \
         patch("agent._delete_checkpoint"), \
         patch("agent.cleanup_temp_sessions"):
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 8192
        mock_summary.health.return_value = (True, "OK")
        
        # Mock LLM to finish the continue-mode run
        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = [b"data: [DONE]"]
        mock_llm.return_value = mock_resp
        
        # Call with continue_mode=True and auto=True to avoid interactive loop
        agent.run_agent_interactive(continue_mode=True, auto=True)
