import copy
import unittest
from unittest.mock import patch
import sys

import pytest

import agent as _agent_mod
from agent import main


@pytest.fixture
def restore_agent_state():
    """Snapshot ``_config``, ``_main_backend``, ``_summary_backend`` before
    the test runs and restore them after — the CLI flag tests mutate these
    module globals and later tests (test_agent_coverage.py) rely on the
    default llamacpp backend state.
    """
    saved_config = copy.deepcopy(_agent_mod._config)
    saved_main = _agent_mod._main_backend
    saved_summary = _agent_mod._summary_backend
    yield
    _agent_mod._config.clear()
    _agent_mod._config.update(saved_config)
    _agent_mod._main_backend = saved_main
    _agent_mod._summary_backend = saved_summary


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
def test_backend_main_flag_overrides_config(mock_emit, mock_run, monkeypatch, restore_agent_state):
    """``--backend-main bedrock`` sets _config['backends']['main']['kind']."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)

    argv = ["agent.py", "--backend-main", "bedrock", "--auto"]
    with patch.object(sys, "argv", argv):
        main()
    assert _agent_mod._config["backends"]["main"]["kind"] == "bedrock"
    assert _agent_mod._main_backend.kind == "bedrock"


@patch("agent.run_agent_interactive")
@patch("agent._emit")
def test_backend_summary_flag_overrides_config(mock_emit, mock_run, monkeypatch, restore_agent_state):
    """``--backend-summary bedrock`` sets the summary backend kind."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)

    argv = ["agent.py", "--backend-summary", "bedrock", "--auto"]
    with patch.object(sys, "argv", argv):
        main()
    assert _agent_mod._config["backends"]["summary"]["kind"] == "bedrock"
    assert _agent_mod._summary_backend.kind == "bedrock"


def test_backend_flag_invalid_value_argparse_error(capsys):
    """An unknown value for --backend-main is an argparse error (SystemExit)."""
    argv = ["agent.py", "--backend-main", "nope"]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit):
            main()


@patch("agent.run_agent_interactive")
@patch("agent._emit")
def test_backend_main_bedrock_default_model(mock_emit, mock_run, monkeypatch, restore_agent_state):
    """If --backend-main bedrock is set with no model in the config block,
    a sensible default is supplied (claude-v4.5-sonnet for main)."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)

    # Clear any model that may be set on the main backend entry.
    _agent_mod._config["backends"]["main"].pop("model", None)

    argv = ["agent.py", "--backend-main", "bedrock", "--auto"]
    with patch.object(sys, "argv", argv):
        main()
    assert _agent_mod._config["backends"]["main"]["model"] == "claude-v4.5-sonnet"


if __name__ == '__main__':
    unittest.main()
