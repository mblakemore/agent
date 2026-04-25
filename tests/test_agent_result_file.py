import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import tempfile

import agent as _agent_mod
from agent import main, run_agent_interactive

class TestAgentResultFile(unittest.TestCase):
    """Tests for the --result-file functionality in agent.py"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.result_file = os.path.join(self.temp_dir.name, "result.txt")

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch('agent.run_agent_interactive')
    @patch('agent._emit')
    def test_main_passes_result_file(self, mock_emit, mock_run):
        """Test that main() correctly parses --result-file and passes it to run_agent_interactive."""
        with patch.object(sys, 'argv', ['agent.py', '--result-file', self.result_file]):
            main()
            # Check if run_agent_interactive was called with result_file
            args, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get('result_file'), self.result_file)

    @patch('agent.run_agent_single')
    @patch('agent.cleanup_temp_sessions')
    @patch('agent._delete_checkpoint')
    @patch('agent._log_bedrock_session_spend')
    @patch('agent._emit')
    @patch('agent._setup_logger')
    def test_run_agent_interactive_writes_result_file(self, mock_setup_logger, mock_emit, mock_spend, mock_del_cp, mock_cleanup, mock_single):
        """Test that run_agent_interactive actually writes the last assistant message to the result_file."""
        # Setup logger mock to avoid file creation/errors
        mock_log = MagicMock()
        mock_setup_logger.return_value = (mock_log, "log.txt", "err.txt")

        # Mock conversation history: production code looks for the last 'assistant' role
        # We must inject this into the conversation_history used by the production code.
        # Since conversation_history is created locally in run_agent_interactive, 
        # and we are calling it with initial_prompt and auto=True, 
        # we need to mock how it's populated.
        
        # The production code does:
        # conversation_history.append({"role": "user", "content": expanded})
        # result = run_agent_single(conversation_history, ...)
        
        # To make it work, we can make run_agent_single append the assistant message 
        # to the conversation_history list it was passed.
        def side_effect_single(history, *args, **kwargs):
            history.append({"role": "assistant", "content": "Final Answer: 42"})
            return "completed" # result != "cancelled" to skip the guidance loop

        mock_single.side_effect = side_effect_single

        # Call the production code
        # initial_prompt triggers the path to run_agent_single and then the result_file block
        run_agent_interactive(
            initial_prompt="Hello", 
            auto=True, 
            result_file=self.result_file
        )

        # Verify the production code wrote the correct content
        with open(self.result_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "Final Answer: 42")

    @patch('agent.run_agent_single')
    @patch('agent.cleanup_temp_sessions')
    @patch('agent._delete_checkpoint')
    @patch('agent._log_bedrock_session_spend')
    @patch('agent._emit')
    @patch('agent._setup_logger')
    def test_run_agent_interactive_result_file_empty_history(self, mock_setup_logger, mock_emit, mock_spend, mock_del_cp, mock_cleanup, mock_single):
        """Test result-file writing when no assistant messages exist."""
        mock_log = MagicMock()
        mock_setup_logger.return_value = (mock_log, "log.txt", "err.txt")

        def side_effect_single(history, *args, **kwargs):
            # Do NOT append an assistant message
            return "completed"

        mock_single.side_effect = side_effect_single

        run_agent_interactive(
            initial_prompt="Hello", 
            auto=True, 
            result_file=self.result_file
        )

        with open(self.result_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "")

if __name__ == '__main__':
    unittest.main()
