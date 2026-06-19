import unittest
from unittest.mock import MagicMock, patch, mock_open
import agent
import sys

class TestAgentInteractive(unittest.TestCase):
    def setUp(self):
        # Save originals so tearDown can restore them — prevents global state
        # leaking to subsequent test modules and causing KeyError / type errors.
        self._orig_config = agent._config
        self._orig_main_backend = agent._main_backend
        self._orig_summary_backend = agent._summary_backend
        self._orig_emit = agent._emit
        self._orig_setup_logger = agent._setup_logger
        self._orig_load_checkpoint = agent._load_checkpoint
        self._orig_delete_checkpoint = agent._delete_checkpoint
        self._orig_auto_increment_cycle = agent._auto_increment_cycle
        self._orig_telemetry = agent.telemetry

        # Mock the global configuration and backends in the agent module
        agent._config = {
            "context": {"ctx_size": 4096, "max_tokens": 4096},
            "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
            "llm": {"model": "test-model"},
            "summary": {"enabled": True, "ctx_size": 1024}
        }

        # Mock the main backend
        self.mock_main_backend = MagicMock()
        self.mock_main_backend.model = "test-model"
        self.mock_main_backend.kind = "test-kind"
        self.mock_main_backend.base_url = "http://test-url"
        self.mock_main_backend.health.return_value = (True, "ok")
        self.mock_main_backend.detect_ctx_size.return_value = 8192
        agent._main_backend = self.mock_main_backend

        # Mock the summary backend
        self.mock_summary_backend = MagicMock()
        self.mock_summary_backend.model = "summary-model"
        self.mock_summary_backend.kind = "summary-kind"
        self.mock_summary_backend.base_url = "http://summary-url"
        self.mock_summary_backend.health.return_value = (True, "ok")
        self.mock_summary_backend.detect_ctx_size.return_value = 4096
        agent._summary_backend = self.mock_summary_backend

        # Mock utility functions and globals
        agent._emit = MagicMock()
        agent._setup_logger = MagicMock()
        self.mock_log = MagicMock()
        agent._setup_logger.return_value = (self.mock_log, "/tmp/log", "/tmp/error_log")

        agent._load_checkpoint = MagicMock(return_value=None)
        agent._delete_checkpoint = MagicMock()
        agent._auto_increment_cycle = MagicMock()

        # Mock telemetry
        import telemetry
        agent.telemetry = MagicMock()
        agent.telemetry.init.return_value = False
        agent.telemetry.record_cycle = MagicMock()
        agent.telemetry.shutdown = MagicMock()

    def tearDown(self):
        agent._config = self._orig_config
        agent._main_backend = self._orig_main_backend
        agent._summary_backend = self._orig_summary_backend
        agent._emit = self._orig_emit
        agent._setup_logger = self._orig_setup_logger
        agent._load_checkpoint = self._orig_load_checkpoint
        agent._delete_checkpoint = self._orig_delete_checkpoint
        agent._auto_increment_cycle = self._orig_auto_increment_cycle
        agent.telemetry = self._orig_telemetry

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_interactive_basic_loop(self, mock_input, mock_run_single):
        """Test the basic interactive loop: one prompt and then exit."""
        mock_input.side_effect = ["Hello agent", "exit"]
        mock_run_single.return_value = "Hello user"

        with patch('sys.stdout'):
            agent.run_agent_interactive()

        self.assertTrue(mock_run_single.called)
        args, kwargs = mock_run_single.call_args
        history = args[0]
        self.assertTrue(any("Hello agent" in msg.get("content", "") for msg in history))

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_interactive_exit_immediately(self, mock_input, mock_run_single):
        """Test exiting the interactive loop immediately."""
        mock_input.side_effect = ["exit"]

        with patch('sys.stdout'):
            agent.run_agent_interactive()

        mock_run_single.assert_not_called()

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_interactive_initial_prompt(self, mock_input, mock_run_single):
        """Test that providing an initial prompt triggers an immediate run_agent_single call."""
        mock_input.side_effect = ["exit"]
        
        with patch('sys.stdout'):
            agent.run_agent_interactive(initial_prompt="Start here")

        self.assertGreaterEqual(mock_run_single.call_count, 1)
        args, kwargs = mock_run_single.call_args_list[0]
        history = args[0]
        self.assertTrue(any("Start here" in msg.get("content", "") for msg in history))

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_interactive_auto_mode(self, mock_input, mock_run_single):
        """Test auto mode: runs once and exits without entering loop."""
        mock_run_single.return_value = "Completed"
        
        with patch('sys.stdout'):
            agent.run_agent_interactive(initial_prompt="Auto task", auto=True)

        mock_run_single.assert_called_once()
        mock_input.assert_not_called()

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_interactive_auto_cancelled(self, mock_input, mock_run_single):
        """Test auto mode when agent returns 'cancelled' - should prompt for guidance."""
        mock_run_single.side_effect = ["cancelled", "Completed"]
        mock_input.side_effect = ["More guidance", ""] 
        
        with patch('sys.stdout'):
            agent.run_agent_interactive(initial_prompt="Auto task", auto=True)

        self.assertEqual(mock_run_single.call_count, 2)

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_continue_mode(self, mock_input, mock_run_single):
        """Test continuing from a checkpoint with full state.

        Non-auto continue mode restores the checkpoint state and falls through
        to the interactive loop, so a prompt must be submitted to drive a turn;
        the restored history (incl. "prev msg") must reach run_agent_single.
        """
        mock_input.side_effect = ["resume please", "exit"]
        # Mock checkpoint: (conversation_history, summary_state, start_turn,
        # initial_files, clean_exit) — must match _load_checkpoint's 5-tuple
        # (clean_exit was added to the checkpoint shape after this test).
        agent._load_checkpoint.return_value = (
            [{"role": "user", "content": "prev msg"}],
            {"text": "prev summary", "up_to": 1},
            1,
            ["file1.txt"],
            False,
        )
        
        with patch('sys.stdout'):
            agent.run_agent_interactive(continue_mode=True)

        self.assertTrue(mock_run_single.called)
        args, kwargs = mock_run_single.call_args
        history = args[0]
        self.assertTrue(any("prev msg" in msg.get("content", "") for msg in history))

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_tui_flow(self, mock_input, mock_run_single):
        """Test TUI path: Mock TuiSession to avoid prompt_toolkit dependency."""
        with patch('tui._AVAILABLE', True), \
             patch('tui.TuiSession') as mock_session_cls:
            
            mock_session = MagicMock()
            mock_session.prompt.return_value = "exit"
            mock_session.close = MagicMock()
            mock_session_cls.return_value = mock_session
            
            with patch('sys.stdout'):
                agent.run_agent_interactive(tui=True)
            
            self.assertTrue(mock_session.prompt.called)
            self.assertTrue(mock_session.close.called)

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    @patch('agent._expand_file_refs')
    def test_run_agent_expand_error(self, mock_expand, mock_input, mock_run_single):
        """Test error handling when expanding file references."""
        mock_input.side_effect = ["invalid_file_ref", "exit"]
        mock_expand.return_value = ("", None, "File not found")
        
        with patch('sys.stdout'):
            agent.run_agent_interactive()
            
        mock_run_single.assert_not_called()

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_interactive_commands(self, mock_input, mock_run_single):
        """Test handling of slash commands in interactive loop."""
        mock_input.side_effect = ["/some_command", "exit"]
        
        with patch('agent._commands.handle_command') as mock_handle:
            mock_handle.return_value = True # Command handled
            with patch('sys.stdout'):
                agent.run_agent_interactive()
            
            self.assertTrue(mock_handle.called)

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_run_agent_result_file(self, mock_input, mock_run_single):
        """Test that result_file is written to upon exit."""
        mock_input.side_effect = ["exit"]
        mock_run_single.return_value = "Final Answer"
        
        m_open = mock_open()
        with patch('builtins.open', m_open):
            with patch('sys.stdout'):
                agent.run_agent_interactive(result_file="/tmp/result.txt")
        
        m_open().write.assert_called()

    def test_boot_logic_coverage(self):
        """Test the boot sequence logic specifically to cover lines 2157-2200."""
        with patch('agent._BOOT_LINES_PRINTED', 5):
            # Create a mock callback that has the expected attribute
            mock_cb = MagicMock()
            mock_cb._boot_lines_printed = 0
            with patch('agent.run_agent_single'), \
                 patch('builtins.input', side_effect=["exit"]), \
                 patch('sys.stdout'), \
                 patch('agent.run_agent_interactive', side_effect=agent.run_agent_interactive):
                
                # We need to pass the callback to run_agent_interactive
                agent.run_agent_interactive(cb=mock_cb)
                
                # Verify that the boot lines were updated
                self.assertGreater(mock_cb._boot_lines_printed, 0)

    @patch('agent.run_agent_single')
    @patch('builtins.input')
    def test_pinned_instructions_extraction(self, mock_input, mock_run_single):
        """Test that pinned instructions are extracted from the prompt."""
        mock_input.side_effect = ["This is a test <pinned>Use Python 3.12</pinned> please", "exit"]
        
        with patch('sys.stdout'):
            agent.run_agent_interactive()
        
        self.assertTrue(mock_run_single.called)


# ── Checkpoint round-trip guards ────────────────────────────────────────────
# These pin the _save/_load_checkpoint tuple shape. A stale 4-tuple mock once
# hid the addition of `clean_exit` (5th field) and silently broke the
# continue-mode test instead of failing loudly here.

def test_checkpoint_roundtrip_is_five_tuple(tmp_path, monkeypatch):
    cp_path = tmp_path / "cp.json"
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(cp_path))
    history = [{"role": "user", "content": "hi"}]
    summary = {"text": "s", "up_to": 1}
    agent._save_checkpoint(history, summary, 3, ["f.txt"], clean_exit=True)
    loaded = agent._load_checkpoint()
    assert loaded is not None and len(loaded) == 5
    h, s, turn, files, clean = loaded
    assert any(m.get("content") == "hi" for m in h)
    assert s == summary and turn == 3 and files == ["f.txt"] and clean is True


def test_checkpoint_load_defaults_clean_exit_for_old_files(tmp_path, monkeypatch):
    import json
    cp_path = tmp_path / "cp.json"
    cp_path.write_text(json.dumps({
        "conversation_history": [{"role": "user", "content": "x"}],
        "summary_state": {"text": ""},
        "turn": 2,
        "initial_files": [],
    }))
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(cp_path))
    loaded = agent._load_checkpoint()
    assert len(loaded) == 5
    assert loaded[4] is False  # clean_exit defaulted for pre-clean_exit files


if __name__ == '__main__':
    unittest.main()
