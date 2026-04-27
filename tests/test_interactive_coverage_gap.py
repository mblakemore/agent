import pytest
from unittest.mock import patch, MagicMock
import agent

# Minimal dummy config to prevent KeyError in run_agent_interactive
DUMMY_CONFIG = {
    "context": {
        "ctx_size": 32768,
        "max_tokens": 4096,
    },
    "generation": {
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 50,
        "presence_penalty": 0.0,
    },
    "llm": {
        "model": "test-model",
    }
}

def test_run_agent_interactive_boot_and_exit():
    """
    Specifically targets the boot sequence and the exit loop in run_agent_interactive
    to fill coverage gaps in the 2145-2479 range of agent.py.
    """
    with patch.dict(agent._config, DUMMY_CONFIG), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent._setup_logger') as mock_log_setup, \
         patch('agent.telemetry.init', return_value=True), \
         patch('agent.TerminalCallbacks'), \
         patch('builtins.input', return_value="exit"):

        # Setup mocks
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_main.model = "test-model"
        mock_summary.health.return_value = (True, "OK")
        mock_summary.model = "summary-model"
        mock_log_setup.return_value = (MagicMock(), "log.txt", "err.txt")
        
        # Execute the interactive loop
        # We use tui=False to avoid prompt_toolkit dependencies in CI
        agent.run_agent_interactive(tui=False)

        # Verify that the loop was entered and exit was processed
        assert mock_main.health.called

def test_run_agent_interactive_auto_mode_exit():
    """Tests the 'auto' path of run_agent_interactive."""
    with patch.dict(agent._config, DUMMY_CONFIG), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent._setup_logger') as mock_log_setup, \
         patch('agent.telemetry.init', return_value=True), \
         patch('agent.TerminalCallbacks'), \
         patch('agent.run_agent_single') as mock_single, \
         patch('builtins.input', return_value="exit"):

        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_main.model = "test-model"
        mock_summary.health.return_value = (True, "OK")
        mock_summary.model = "summary-model"
        mock_log_setup.return_value = (MagicMock(), "log.txt", "err.txt")
        
        # Trigger auto mode with a prompt to ensure it hits the loop
        agent.run_agent_interactive(auto=True, initial_prompt="Test Prompt", tui=False)
        
        assert mock_single.called
