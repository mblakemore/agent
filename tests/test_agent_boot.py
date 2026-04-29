import pytest
from unittest.mock import patch, MagicMock
import agent

def test_run_agent_interactive_boot():
    """
    Exercises the boot sequence in run_agent_interactive to increase coverage.
    We mock the blocking calls (input/output) and external backends.
    """
    with patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.telemetry.init', return_value=True), \
         patch('agent._setup_logger', return_value=(MagicMock(), "log.txt", "err.txt")), \
         patch('agent._emit'), \
         patch('agent._git_short_sha', return_value="abc1234"), \
         patch('agent.input', side_effect=KeyboardInterrupt), \
         patch('agent.print'):
        
        # Configure mock backends
        mock_main.health.return_value = (True, "OK")
        mock_main.model = "test-model"
        mock_main.kind = "test-kind"
        mock_main.base_url = "http://test"
        
        mock_summary.health.return_value = (True, "OK")
        mock_summary.model = "sum-model"
        mock_summary.kind = "sum-kind"
        mock_summary.base_url = "http://sum-test"

        # Call run_agent_interactive. We expect it to raise KeyboardInterrupt 
        # because of our mock, but it should have executed the boot sequence first.
        with pytest.raises(KeyboardInterrupt):
            agent.run_agent_interactive(tui=False, verbose=False)

def test_run_agent_interactive_tui_boot():
    """
    Exercises the boot sequence with tui=True.
    """
    with patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.telemetry.init', return_value=True), \
         patch('agent._setup_logger', return_value=(MagicMock(), "log.txt", "err.txt")), \
         patch('agent._emit'), \
         patch('agent._git_short_sha', return_value="abc1234"), \
         patch('agent.input', side_effect=KeyboardInterrupt), \
         patch('agent.print'), \
         patch('agent.tui.TuiSession', return_value=MagicMock()):
        
        # Configure mock backends
        mock_main.health.return_value = (True, "OK")
        mock_main.model = "test-model"
        mock_main.kind = "test-kind"
        mock_main.base_url = "http://test"
        
        mock_summary.health.return_value = (True, "OK")
        mock_summary.model = "sum-model"
        mock_summary.kind = "sum-kind"
        mock_summary.base_url = "http://sum-test"

        # Call run_agent_interactive with tui=True
        with pytest.raises(KeyboardInterrupt):
            agent.run_agent_interactive(tui=True, verbose=False)
