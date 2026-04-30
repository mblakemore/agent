import pytest
import sys
from unittest.mock import patch, MagicMock
import agent

def test_main_auto_flag():
    """Verify --auto flag is passed to run_agent_interactive."""
    with patch('sys.argv', ['agent.py', '--auto']), \
         patch('agent.run_agent_interactive') as mock_run:
        agent.main()
        mock_run.assert_called_once()
        # Check if auto=True was passed. 
        # Note: we check kwargs because run_agent_interactive has many args.
        args, kwargs = mock_run.call_args
        assert kwargs.get('auto') is True

def test_main_continue_flag():
    """Verify --continue flag is passed to run_agent_interactive."""
    with patch('sys.argv', ['agent.py', '--continue']), \
         patch('agent.run_agent_interactive') as mock_run:
        agent.main()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert kwargs.get('continue_mode') is True

def test_main_repeat_n():
    """Verify --repeat N runs the agent exactly N times."""
    with patch('sys.argv', ['agent.py', '--repeat', '3', '--auto']), \
         patch('agent.run_agent_interactive') as mock_run:
        agent.main()
        assert mock_run.call_count == 3

def test_main_repeat_zero():
    """Verify --repeat 0 runs indefinitely until KeyboardInterrupt."""
    with patch('sys.argv', ['agent.py', '--repeat', '0', '--auto']), \
         patch('agent.run_agent_interactive') as mock_run:
        # Simulate KeyboardInterrupt to break the infinite loop
        mock_run.side_effect = KeyboardInterrupt
        agent.main()
        assert mock_run.called

def test_main_repeat_no_val():
    """Verify --repeat without value (const=0) runs indefinitely."""
    with patch('sys.argv', ['agent.py', '--repeat', '--auto']), \
         patch('agent.run_agent_interactive') as mock_run:
        mock_run.side_effect = KeyboardInterrupt
        agent.main()
        assert mock_run.called

def test_main_result_file():
    """Verify --result-file is passed correctly."""
    with patch('sys.argv', ['agent.py', '--result-file', 'out.txt', '--auto']), \
         patch('agent.run_agent_interactive') as mock_run:
        agent.main()
        args, kwargs = mock_run.call_args
        assert kwargs.get('result_file') == 'out.txt'

def test_main_no_tui():
    """Verify --no-tui disables TUI."""
    with patch('sys.argv', ['agent.py', '--no-tui']), \
         patch('agent.run_agent_interactive') as mock_run:
        agent.main()
        args, kwargs = mock_run.call_args
        assert kwargs.get('tui') is False

def test_main_tui_enabled_by_default():
    """Verify TUI is enabled by default in interactive mode."""
    with patch('sys.argv', ['agent.py']), \
         patch('agent.run_agent_interactive') as mock_run:
        agent.main()
        args, kwargs = mock_run.call_args
        assert kwargs.get('tui') is True
