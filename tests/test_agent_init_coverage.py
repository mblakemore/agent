import unittest
from unittest.mock import patch, MagicMock
import sys
import agent

class TestAgentInitCoverage(unittest.TestCase):

    def setUp(self):
        # Setup basic config to avoid KeyErrors
        self.mock_config = {
            "context": {"ctx_size": 4096, "max_tokens": 1024},
            "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
            "llm": {"model": "test-model"},
            "summary": {"enabled": True, "ctx_size": 4096},
        }

    def _setup_common_mocks(self, mock_main, mock_summary, mock_setup_logger):
        """Helper to configure the baseline healthy state for agent session."""
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 100000 
        mock_main.model = "main-model"
        mock_main.kind = "openai"
        mock_main.base_url = "http://main"
        
        mock_summary.health.return_value = (True, "OK")
        mock_summary.model = "sum-model"
        mock_summary.kind = "openai"
        mock_summary.base_url = "http://sum"
        mock_summary.detect_ctx_size.return_value = 20000
        
        mock_log = MagicMock()
        mock_setup_logger.return_value = (mock_log, "log_path", "err_path")
        return mock_log

    @patch('agent._emit')
    @patch('agent._setup_logger')
    def test_tui_fallback(self, mock_setup_logger, mock_emit):
        with patch('agent._config', self.mock_config),              patch('agent._main_backend') as mock_main,              patch('agent._summary_backend') as mock_summary,              patch.dict('sys.modules', {'tui': MagicMock()}):
            
            self._setup_common_mocks(mock_main, mock_summary, mock_setup_logger)
            import tui
            tui._AVAILABLE = False
            
            with patch('builtins.input', side_effect=EOFError):
                agent.run_agent_interactive(tui=True, auto=False)
                
                found = any("prompt_toolkit not installed" in str(call) for call in mock_emit.call_args_list)
                self.assertTrue(found, "TUI fallback notice not emitted")

    @patch('agent._emit')
    @patch('agent._expand_file_refs')
    @patch('agent._setup_logger')
    def test_file_ref_error(self, mock_setup_logger, mock_expand, mock_emit):
        with patch('agent._config', self.mock_config),              patch('agent._main_backend') as mock_main,              patch('agent._summary_backend') as mock_summary:
            
            self._setup_common_mocks(mock_main, mock_summary, mock_setup_logger)
            mock_expand.return_value = (None, None, "mock error")
            
            agent.run_agent_interactive(initial_prompt="some prompt with refs")
            mock_emit.assert_any_call("on_error", "mock error")

    @patch('agent._setup_logger')
    @patch('agent._expand_file_refs')
    def test_pinned_instructions(self, mock_expand, mock_setup_logger):
        with patch('agent._config', self.mock_config),              patch('agent._main_backend') as mock_main,              patch('agent._summary_backend') as mock_summary,              patch('agent.run_agent_single', return_value="success"):
            
            mock_log = self._setup_common_mocks(mock_main, mock_summary, mock_setup_logger)
            mock_expand.return_value = ("Hello <pinned>Always be concise</pinned>", [], None)
            
            agent.run_agent_interactive(initial_prompt="Hello <pinned>Always be concise</pinned>", auto=True)
            
            found = any("Pinned instructions extracted" in str(call) for call in mock_log.info.call_args_list)
            self.assertTrue(found, "log.info was not called for pinned instructions extraction")

    @patch('agent._main_backend')
    @patch('agent._summary_backend')
    @patch('agent._setup_logger')
    def test_backend_health_and_ctx_detection(self, mock_setup_logger, mock_summary, mock_main):
        with patch('agent._config', self.mock_config),              patch('agent._emit'),              patch('agent.run_agent_single', return_value="success"),              patch('builtins.input', side_effect=EOFError):
            
            self._setup_common_mocks(mock_main, mock_summary, mock_setup_logger)
            agent.run_agent_interactive(auto=False)
            
            # 100000 * 0.85 = 85000, capped at 85000.
            self.assertEqual(self.mock_config["context"]["ctx_size"], 85000)

    @patch('agent._setup_logger')
    @patch('builtins.input', side_effect=['exit'])
    def test_interactive_loop_exit(self, mock_input, mock_setup_logger):
        with patch('agent._config', self.mock_config),              patch('agent._main_backend') as mock_main,              patch('agent._summary_backend') as mock_summary,              patch('agent._emit'):
            
            self._setup_common_mocks(mock_main, mock_summary, mock_setup_logger)
            agent.run_agent_interactive(auto=False)
            # Success if it didn't hang

if __name__ == '__main__':
    unittest.main()
