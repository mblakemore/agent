import unittest
from unittest.mock import patch, MagicMock
import sys
import agent

class TestAgentInitCoverage(unittest.TestCase):

    @patch('agent._emit')
    @patch('agent._setup_logger')
    def test_tui_fallback(self, mock_setup_logger, mock_emit):
        """
        Covers lines 2100-2108: Triggered when tui=True, auto=False, 
        and tui._AVAILABLE is False.
        """
        # Mock logger to avoid file system side effects
        mock_log = MagicMock()
        mock_setup_logger.return_value = (mock_log, "log_path", "err_path")

        # Mock the tui module and its _AVAILABLE attribute
        with patch.dict('sys.modules', {'tui': MagicMock()}):
            import tui
            tui._AVAILABLE = False
            
            # We mock the input() to prevent hanging and the loop to stop it quickly
            with patch('builtins.input', side_effect=EOFError):
                agent.run_agent_interactive(tui=True, auto=False)
                
                # Verify the warning emission
                # Check if any call to _emit was "on_notice" with "warn" and the expected string
                found = False
                for call in mock_emit.call_args_list:
                    args = call.args
                    if len(args) >= 3 and args[0] == "on_notice" and args[1] == "warn" and "prompt_toolkit not installed" in args[2]:
                        found = True
                        break
                self.assertTrue(found, "TUI fallback notice not emitted")

    @patch('agent._emit')
    @patch('agent._expand_file_refs')
    @patch('agent._setup_logger')
    def test_file_ref_error(self, mock_setup_logger, mock_expand, mock_emit):
        """
        Covers lines 2119-2120: Triggered when _expand_file_refs returns an error.
        """
        # Mock logger
        mock_log = MagicMock()
        mock_setup_logger.return_value = (mock_log, "log_path", "err_path")

        # Mock _expand_file_refs to return (None, None, "mock error")
        mock_expand.return_value = (None, None, "mock error")
        
        agent.run_agent_interactive(initial_prompt="some prompt with refs")
        
        # Verify the error emission
        mock_emit.assert_any_call("on_error", "mock error")

    @patch('agent._setup_logger')
    @patch('agent._expand_file_refs')
    def test_pinned_instructions(self, mock_expand, mock_setup_logger):
        """
        Covers lines 2127-2128: Triggered when initial_prompt contains <pinned> tags.
        """
        # Mock logger
        mock_log = MagicMock()
        mock_setup_logger.return_value = (mock_log, "log_path", "err_path")
        
        # Mock expand to just return the input as is
        mock_expand.return_value = ("Hello <pinned>Always be concise</pinned>", [], None)
        
        # We need to mock run_agent_single to avoid calling the LLM
        with patch('agent.run_agent_single', return_value="success"):
            agent.run_agent_interactive(initial_prompt="Hello <pinned>Always be concise</pinned>", auto=True)
            
            # Verify that the log.info was called for pinned instructions extraction
            found = False
            for call in mock_log.info.call_args_list:
                args = call.args
                if args and "Pinned instructions extracted" in args[0]:
                    found = True
                    break
            self.assertTrue(found, "log.info was not called for pinned instructions extraction")

if __name__ == '__main__':
    unittest.main()
