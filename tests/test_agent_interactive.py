import pytest
from unittest.mock import patch, MagicMock, call
import agent

@patch('agent._setup_logger')
@patch('agent._check_api_health')
@patch('agent._detect_ctx_size')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_run_agent_interactive_exit_command(mock_run, mock_emit, mock_detect, mock_health, mock_log):
    """Test that the interactive loop exits on 'exit' command."""
    mock_log.return_value = (MagicMock(), "log_path", "err_path")
    mock_health.return_value = (True, "OK")
    mock_detect.return_value = 32768
    mock_run.return_value = "finished"
    
    # Mock input to return 'exit'
    with patch('builtins.input', side_effect=["exit"]):
        agent.run_agent_interactive(tui=False)
    
    # Verify it doesn't loop forever and exits
    assert True

@patch('agent._setup_logger')
@patch('agent._check_api_health')
@patch('agent._detect_ctx_size')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_run_agent_interactive_normal_flow(mock_run, mock_emit, mock_detect, mock_health, mock_log):
    """Test that the interactive loop calls run_agent_single on user input."""
    mock_log.return_value = (MagicMock(), "log_path", "err_path")
    mock_health.return_value = (True, "OK")
    mock_detect.return_value = 32768
    mock_run.return_value = "finished"
    
    # Mock input to provide a prompt and then exit
    with patch('builtins.input', side_effect=["Hello agent", "exit"]):
        agent.run_agent_interactive(tui=False)
    
    assert mock_run.called

@patch('agent._setup_logger')
@patch('agent._check_api_health')
@patch('agent._detect_ctx_size')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_run_agent_interactive_auto_paused(mock_run, mock_emit, mock_detect, mock_health, mock_log):
    """Test the 'auto' mode paused operator guidance path."""
    mock_log.return_value = (MagicMock(), "log_path", "err_path")
    mock_health.return_value = (True, "OK")
    mock_detect.return_value = 32768
    
    # First call to run_agent_single returns 'cancelled' to trigger the pause
    # Second call (after guidance) returns 'finished'
    mock_run.side_effect = ["cancelled", "finished"]
    
    with patch('builtins.input', side_effect=["Some guidance"]),          patch('agent._expand_file_refs', return_value=("Some guidance", [], None)):
        agent.run_agent_interactive(initial_prompt="Start", auto=True, tui=False)
    
    assert mock_run.call_count == 2

