import pytest
from unittest.mock import patch, MagicMock
import agent
import tui

def test_run_agent_interactive_tui_fallback():
    """
    Test that when tui=True but prompt_toolkit is not available, 
    the agent emits a notice warning the user.
    """
    with patch("agent._emit") as mock_emit, \
         patch("tui._AVAILABLE", False), \
         patch("builtins.input", side_effect=EOFError), \
         patch("agent._expand_file_refs", return_value=(None, [], None)), \
         patch("agent._main_backend") as mock_main, \
         patch("agent._summary_backend") as mock_sum, \
         patch("agent.run_agent_single", return_value="done"):
        
        # Setup mocks to avoid ValueError during unpacking
        mock_main.health.return_value = (True, "OK")
        mock_main.model = "test-model"
        mock_main.detect_ctx_size.return_value = 1000
        
        mock_sum.health.return_value = (True, "OK")
        mock_sum.model = "sum-model"
        
        try:
            agent.run_agent_interactive(
                tui=True, 
                auto=False, 
                initial_prompt=None
            )
        except Exception as e:
            pytest.fail(f"run_agent_interactive raised unexpected exception: {e}")

        # Verify the fallback path was hit
        mock_emit.assert_any_call(
            "on_notice", 
            "warn", 
            "prompt_toolkit not installed — using plain prompt. "
            "`pip install prompt_toolkit` (or pass --no-tui to silence)."
        )

def test_run_agent_interactive_tui_available():
    """
    Test that when tui=True and prompt_toolkit IS available, 
    the agent does NOT emit the fallback warning.
    """
    with patch("agent._emit") as mock_emit, \
         patch("tui._AVAILABLE", True), \
         patch("tui.TuiSession", return_value=MagicMock()), \
         patch("builtins.input", side_effect=EOFError), \
         patch("agent._expand_file_refs", return_value=(None, [], None)), \
         patch("agent._main_backend") as mock_main, \
         patch("agent._summary_backend") as mock_sum, \
         patch("agent.run_agent_single", return_value="done"):
        
        mock_main.health.return_value = (True, "OK")
        mock_main.model = "test-model"
        mock_main.detect_ctx_size.return_value = 1000
        
        mock_sum.health.return_value = (True, "OK")
        mock_sum.model = "sum-model"

        # Mock TuiSession's prompt method to exit the loop
        tui_mock = MagicMock()
        tui_mock.prompt.side_effect = EOFError
        with patch("tui.TuiSession", return_value=tui_mock):
            agent.run_agent_interactive(
                tui=True, 
                auto=False, 
                initial_prompt=None
            )

        # Verify the fallback warning was NOT sent
        for call in mock_emit.call_args_list:
            args, kwargs = call
            if args and args[0] == "on_notice" and args[1] == "warn":
                pytest.fail("Fallback warning emitted even though TUI is available")
