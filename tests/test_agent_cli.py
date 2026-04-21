import unittest
from unittest.mock import patch
import sys
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

if __name__ == '__main__':
    unittest.main()
