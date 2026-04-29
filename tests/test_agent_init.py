import pytest
from unittest.mock import MagicMock, patch, ANY
import agent
from types import SimpleNamespace

def test_run_agent_interactive_init_flow():
    """Test the initialization sequence of run_agent_interactive."""
    with patch('agent._setup_logger') as mock_setup_logger, \
         patch('telemetry.init', return_value=True), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent._emit') as mock_emit, \
         patch('agent.AsyncSummarizer') as mock_summarizer_cls:
        
        mock_setup_logger.return_value = (MagicMock(), "log_path", "err_path")
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 8192
        
        with patch('agent.run_agent_single', return_value="done"):
            with patch('builtins.input', side_effect=EOFError):
                agent.run_agent_interactive(auto=False, tui=False)
        
        mock_main.health.assert_called()
        mock_summary.health.assert_called()
        mock_emit.assert_any_call("on_session_start", ANY)
        mock_summarizer_cls.assert_called()

def test_run_agent_interactive_continue_mode():
    """Test that continue_mode correctly loads checkpoints and calls run_agent_single."""
    checkpoint_data = (
        [{"role": "user", "content": "hi"}], 
        {"text": "summary"}, 
        1, 
        ["file1.txt"]
    )
    with patch('agent._load_checkpoint', return_value=checkpoint_data), \
         patch('agent._emit') as mock_emit, \
         patch('agent.run_agent_single') as mock_run, \
         patch('agent._delete_checkpoint'), \
         patch('agent._log_bedrock_session_spend'), \
         patch('telemetry.record_cycle'), \
         patch('telemetry.shutdown'), \
         patch('agent.cleanup_temp_sessions'):
        
        agent.run_agent_interactive(auto=True, tui=False, continue_mode=True)
        
        mock_emit.assert_any_call("on_continue_resumed", 1, 1)
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        history = args[0]
        assert any("Continue where you left off" in msg["content"] for msg in history)

def test_run_agent_interactive_loop_and_cleanup():
    """Test the interactive loop with commands, user input, and final cleanup."""
    # Force telemetry on to cover the cleanup block
    agent._telemetry_on = True
    
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent._emit') as mock_emit, \
         patch('agent.AsyncSummarizer'), \
         patch('agent.run_agent_single') as mock_run, \
         patch('agent._commands.handle_command') as mock_handle, \
         patch('agent._expand_file_refs') as mock_expand, \
         patch('telemetry.record_cycle') as mock_record, \
         patch('telemetry.shutdown') as mock_shutdown, \
         patch('agent._log_bedrock_session_spend') as mock_spend:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 8192
        
        # Simulate: 1. A command /cmd, 2. A file reference prompt, 3. Normal input, 4. "exit"
        # /cmd -> mock_handle returns True
        # "@file" -> mock_expand returns files
        # "input" -> mock_run is called
        # "exit" -> loop breaks
        mock_expand.side_effect = [
            ("Prompt", [], None), # First input "Hello"
            ("Expanded", ["file1.txt"], None), # Second input "@file"
            ("input", [], None), # Third input "input"
            ("exit", [], None), # Fourth input "exit"
        ]
        
        with patch('builtins.input', side_effect=["/cmd", "@file", "input", "exit"]):
            agent.run_agent_interactive(auto=False, tui=False)
            
        mock_handle.assert_called()
        mock_expand.assert_called()
        mock_run.assert_called()
        mock_record.assert_called()
        mock_shutdown.assert_called()
        mock_spend.assert_called()
        mock_emit.assert_any_call("on_notice", "info", "Goodbye!")

def test_git_short_sha_fallback():
    """Test that _git_short_sha returns empty string on failure."""
    with patch('subprocess.check_output', side_effect=Exception("git not found")):
        sha = agent._git_short_sha()
        assert sha == ""

def test_extract_pinned():
    """Test extraction of pinned instructions."""
    text = "Hello <pinned>Important stuff</pinned> world"
    cleaned, pinned = agent._extract_pinned(text)
    assert cleaned == "Hello  world"
    assert pinned == "Important stuff"

def test_apply_backend_overrides():
    """Test that CLI overrides correctly update backends."""
    with patch('agent._build_backend') as mock_build:
        agent._config["backends"] = {"main": {"kind": "llamacpp"}, "summary": {"kind": "llamacpp"}}
        agent._apply_backend_overrides(main_kind="bedrock", summary_kind=None)
        assert agent._config["backends"]["main"]["kind"] == "bedrock"
        assert "claude-" in agent._config["backends"]["main"]["model"]
        mock_build.assert_called()
