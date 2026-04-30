import pytest
from unittest.mock import MagicMock, patch, mock_open
import sys
import os
import agent

def test_initial_prompt_and_auto_mode_coverage():
    """
    Test designed to hit the coverage gaps in agent.py lines 2355-2399.
    Specifically: initial_prompt, auto mode, and result_file.
    """
    # Mocking external dependencies and the TUI to prevent hangs
    with patch('agent._expand_file_refs') as mock_expand, \
         patch('agent._extract_pinned') as mock_pinned, \
         patch('agent.run_agent_single') as mock_run, \
         patch('agent.cleanup_temp_sessions') as mock_cleanup, \
         patch('agent._delete_checkpoint') as mock_delete, \
         patch('agent.telemetry') as mock_telemetry, \
         patch('agent._log_bedrock_session_spend') as mock_spend, \
         patch('builtins.open', mock_open()) as mock_file, \
         patch('agent._emit') as mock_emit:
        
        # Configure mocks
        mock_expand.return_value = ("expanded prompt", ["file1.py"], None)
        mock_pinned.return_value = ("expanded prompt", "pinned info")
        mock_run.return_value = "completed"
        
        # Correct positional arguments for agent.py
        test_args = ["agent.py", "test prompt", "--auto", "--result-file", "res.txt"]
        with patch.object(sys, 'argv', test_args):
            try:
                agent.main()
            except (SystemExit, KeyboardInterrupt):
                pass

        # Assertions to verify the path was hit
        mock_expand.assert_called()
        mock_run.assert_called()
        mock_file.assert_called_with("res.txt", "w", encoding="utf-8")
