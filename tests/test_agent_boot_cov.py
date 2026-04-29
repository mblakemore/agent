import pytest
from unittest.mock import MagicMock, patch
import agent

def test_run_agent_interactive_boot_coverage():
    """
    Test the boot sequence of run_agent_interactive to cover lines 2157-2463.
    """
    # Provide a comprehensive mock config to avoid KeyErrors
    mock_config = {
        "context": {"ctx_size": 10, "max_tokens": 10},
        "generation": {"temperature": 0.7},
        "llm": {"model": "test-model"},
        "summary": {
            "enabled": True,
            "model": "sum-model"
        }
    }
    
    with patch('agent._config', mock_config), \
    patch('agent._setup_logger', return_value=(MagicMock(), "log.txt", "err.txt")), \
    patch('agent.telemetry.init', return_value=True), \
    patch('agent._main_backend') as mock_main, \
    patch('agent._summary_backend') as mock_summary, \
    patch('agent._emit'), \
    patch('agent.TerminalCallbacks'), \
    patch('tui.TuiSession'), \
    patch('agent._llm_request'), \
    patch('builtins.input', side_effect=KeyboardInterrupt): 
        
        mock_main.model = "test-model"
        mock_main.kind = "test-kind"
        mock_main.base_url = "http://test"
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 4096
        
        mock_summary.model = "sum-model"
        mock_summary.kind = "sum-kind"
        mock_summary.base_url = "http://sum"
        mock_summary.health.return_value = (True, "OK")
        
        try:
            agent.run_agent_interactive()
        except KeyboardInterrupt:
            pass

def test_git_short_sha_failure():
    """Cover the exception handler in _git_short_sha."""
    with patch('subprocess.check_output', side_effect=Exception("Git failed")):
        assert agent._git_short_sha() == ""
