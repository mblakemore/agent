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
        mock_main.detect_ctx_size.return_value = 8192
        
        mock_summary.health.return_value = (True, "OK")
        mock_summary.model = "sum-model"
        mock_summary.kind = "sum-kind"
        mock_summary.base_url = "http://sum-test"
        mock_summary.detect_ctx_size.return_value = 4096

        # Call run_agent_interactive. 
        # Since we mock input to raise KeyboardInterrupt, it should be caught
        # by the loop and return None (or whatever the function returns on break).
        result = agent.run_agent_interactive(tui=False, verbose=False)
        # The function doesn't explicitly return a value on KeyboardInterrupt break,
        # it just exits the loop.
        assert result is None

def test_run_agent_interactive_tui_boot():
    """
    Exercises the boot sequence with tui=True.
    We mock the tui module since it's imported locally.
    """
    with patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.telemetry.init', return_value=True), \
         patch('agent._setup_logger', return_value=(MagicMock(), "log.txt", "err.txt")), \
         patch('agent._emit'), \
         patch('agent._git_short_sha', return_value="abc1234"), \
         patch('agent.print'):
        
        # Mock the local import of tui
        with patch('tui._AVAILABLE', True), \
             patch('tui.TuiSession') as mock_tui_session_cls, \
             patch('tui.TuiCallbacks'):
            
            mock_tui_session = MagicMock()
            mock_tui_session_cls.return_value = mock_tui_session
            # Simulate the user exiting the TUI prompt immediately
            mock_tui_session.prompt.side_effect = EOFError
            
            # Configure mock backends
            mock_main.health.return_value = (True, "OK")
            mock_main.model = "test-model"
            mock_main.kind = "test-kind"
            mock_main.base_url = "http://test"
            mock_main.detect_ctx_size.return_value = 8192
            
            mock_summary.health.return_value = (True, "OK")
            mock_summary.model = "sum-model"
            mock_summary.kind = "sum-kind"
            mock_summary.base_url = "http://sum-test"
            mock_summary.detect_ctx_size.return_value = 4096
            
            result = agent.run_agent_interactive(tui=True, verbose=False)
            assert result is None
