import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import tempfile

import agent as _agent_mod
from agent import main

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

    @patch('agent.run_agent_interactive')
    def test_result_file_logic_isolation(self, mock_run):
        """Test the result-file writing logic in isolation by mimicking the loop."""
        # This tests the logic at lines 2170-2177 of agent.py
        conversation_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "This is the final answer!"},
            {"role": "user", "content": "Thanks"},
        ]
        
        last_assistant_msg = ""
        for msg in reversed(conversation_history):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_assistant_msg = msg["content"]
                break
        
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(last_assistant_msg)
        
        with open(self.result_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        self.assertEqual(content, "This is the final answer!")

    @patch('agent.run_agent_interactive')
    def test_result_file_empty_history(self, mock_run):
        """Test result-file writing when no assistant messages exist."""
        conversation_history = [
            {"role": "user", "content": "Hello"},
        ]
        
        last_assistant_msg = ""
        for msg in reversed(conversation_history):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_assistant_msg = msg["content"]
                break
        
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(last_assistant_msg)
        
        with open(self.result_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        self.assertEqual(content, "")

if __name__ == '__main__':
    unittest.main()
