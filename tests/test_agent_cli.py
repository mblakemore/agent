import unittest
from unittest.mock import patch
import sys

import pytest

from agent import main


class TestAgentCLI(unittest.TestCase):
    """Tests for the CLI entry point in agent.py"""

    @patch('agent.run_agent_interactive')
    @patch('agent._emit')
    def test_main_repeat_fixed(self, mock_emit, mock_run):
        """Test that --repeat N runs the agent exactly N times."""
        # Simulate: python agent.py --repeat 3
        with patch.object(sys, 'argv', ['agent.py', '--repeat', '3']):
            main()
            self.assertEqual(mock_run.call_count, 3)

    @patch('agent.run_agent_interactive')
    @patch('agent._emit')
    def test_main_repeat_infinite(self, mock_emit, mock_run):
        """Test that --repeat 0 runs the agent indefinitely."""
        # Simulate: python agent.py --repeat 0
        # Break the loop with KeyboardInterrupt
        mock_run.side_effect = [None, None, KeyboardInterrupt]
        
        with patch.object(sys, 'argv', ['agent.py', '--repeat', '0']):
            # The loop in agent.py catches KeyboardInterrupt and emits on_repeat_done
            main()
            # It should have called mock_run 3 times (2 success + 1 interrupt)
            self.assertEqual(mock_run.call_count, 3)

    @patch('agent.run_agent_interactive')
    @patch('agent._emit')
    def test_main_repeat_interrupt(self, mock_emit, mock_run):
        """Test that a KeyboardInterrupt during a repeat cycle stops execution."""
        # Simulate: python agent.py --repeat 10
        # Interrupt on the first call
        mock_run.side_effect = KeyboardInterrupt
        
        with patch.object(sys, 'argv', ['agent.py', '--repeat', '10']):
            main()
            # It should have called mock_run exactly once
            self.assertEqual(mock_run.call_count, 1)

# ── Backend flag overrides (plan task 2.5) ──


@patch("agent.run_agent_interactive")
@patch("agent._emit")
def test_backend_main_flag_overrides_config(mock_emit, mock_run, monkeypatch):
    """``--backend-main bedrock`` sets _config['backends']['main']['kind']."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    import agent as _agent

    argv = ["agent.py", "--backend-main", "bedrock", "--auto"]
    with patch.object(sys, "argv", argv):
        main()
    assert _agent._config["backends"]["main"]["kind"] == "bedrock"
    assert _agent._main_backend.kind == "bedrock"


@patch("agent.run_agent_interactive")
@patch("agent._emit")
def test_backend_summary_flag_overrides_config(mock_emit, mock_run, monkeypatch):
    """``--backend-summary bedrock`` sets the summary backend kind."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    import agent as _agent

    argv = ["agent.py", "--backend-summary", "bedrock", "--auto"]
    with patch.object(sys, "argv", argv):
        main()
    assert _agent._config["backends"]["summary"]["kind"] == "bedrock"
    assert _agent._summary_backend.kind == "bedrock"


def test_backend_flag_invalid_value_argparse_error(capsys):
    """An unknown value for --backend-main is an argparse error (SystemExit)."""
    argv = ["agent.py", "--backend-main", "nope"]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit):
            main()


@patch("agent.run_agent_interactive")
@patch("agent._emit")
def test_backend_main_bedrock_default_model(mock_emit, mock_run, monkeypatch):
    """If --backend-main bedrock is set with no model in the config block,
    a sensible default is supplied (claude-v4.5-sonnet for main)."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    import agent as _agent

    # Clear any model that may be set on the main backend entry.
    _agent._config["backends"]["main"].pop("model", None)

    argv = ["agent.py", "--backend-main", "bedrock", "--auto"]
    with patch.object(sys, "argv", argv):
        main()
    assert _agent._config["backends"]["main"]["model"] == "claude-v4.5-sonnet"


if __name__ == '__main__':
    unittest.main()
