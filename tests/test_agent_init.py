import pytest
from unittest.mock import MagicMock, patch, ANY
import agent
import tui
import requests
from types import SimpleNamespace

def setup_common_mocks():
    """
    Sets up basic mocks for logger, telemetry, and backends to prevent 
    them from actually trying to connect to services.
    """
    return [
        patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")),
        patch('telemetry.init', return_value=True),
        patch('agent._emit'),
        patch('agent.AsyncSummarizer'),
    ]

def test_run_agent_interactive_init_flow():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="done") as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        with patch('builtins.input', side_effect=['hello', 'exit']):
            agent.run_agent_interactive(tui=False, auto=False)
        mock_run.assert_called()

def test_run_agent_interactive_continue_mode():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent._load_checkpoint') as mock_load, \
         patch('agent.run_agent_single', return_value="done") as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        # Mock a valid checkpoint: (conversation_history, summary_state, start_turn, initial_files)
        mock_load.return_value = ([{"role": "user", "content": "hi"}], {"text": "some summary", "up_to": 0}, 1, [])
        
        with patch('builtins.input', side_effect=['exit']):
            agent.run_agent_interactive(tui=False, auto=False, continue_mode=True)
        
        mock_run.assert_called()

def test_run_agent_interactive_continue_auto():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent._load_checkpoint') as mock_load, \
         patch('agent.run_agent_single', return_value="done") as mock_run, \
         patch('agent.cleanup_temp_sessions'), \
         patch('agent._delete_checkpoint'), \
         patch('telemetry.record_cycle'), \
         patch('telemetry.shutdown'):
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        mock_load.return_value = ([{"role": "user", "content": "hi"}], {"text": "some summary", "up_to": 0}, 1, [])
        
        agent.run_agent_interactive(tui=False, auto=True, continue_mode=True)
        
        mock_run.assert_called()
        agent._delete_checkpoint.assert_called()

def test_initial_prompt_flow():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="done") as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        with patch('builtins.input', side_effect=['exit']):
            agent.run_agent_interactive(initial_prompt="Start with this", tui=False, auto=False)
        
        mock_run.assert_called()

def test_initial_prompt_pinned_flow():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="done") as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        # Using <pinned> tag in prompt
        prompt = "Hello <pinned>Important instruction</pinned> world"
        with patch('builtins.input', side_effect=['exit']):
            agent.run_agent_interactive(initial_prompt=prompt, tui=False, auto=False)
        
        mock_run.assert_called()

def test_initial_prompt_auto_cleanup():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="done") as mock_run, \
         patch('agent.cleanup_temp_sessions'), \
         patch('agent._delete_checkpoint'), \
         patch('telemetry.record_cycle'), \
         patch('telemetry.shutdown'):
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        agent.run_agent_interactive(initial_prompt="Auto task", tui=False, auto=True)
        
        mock_run.assert_called()
        agent._delete_checkpoint.assert_called()

def test_interactive_loop_commands():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="done") as mock_run, \
         patch('agent._commands.handle_command', return_value=True) as mock_cmd:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        # Sequence: valid input, a command, then exit
        with patch('builtins.input', side_effect=['Hello', '/status', 'exit']):
            agent.run_agent_interactive(tui=False, auto=False)
        
        mock_run.assert_called()
        mock_cmd.assert_called()

def test_interactive_loop_interrupts():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="done") as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        # Test KeyboardInterrupt
        with patch('builtins.input', side_effect=KeyboardInterrupt):
            agent.run_agent_interactive(tui=False, auto=False)
        
        # Test EOFError
        with patch('builtins.input', side_effect=EOFError):
            agent.run_agent_interactive(tui=False, auto=False)
            
        # Both should exit gracefully without calling run_agent_single
        assert mock_run.call_count == 0

def test_git_short_sha_fallback():
    with patch('subprocess.check_output', side_effect=Exception("git not found")):
        sha = agent._git_short_sha()
        assert sha == ""

def test_extract_pinned():
    text = "Hello <pinned>Important stuff</pinned> world"
    cleaned, pinned = agent._extract_pinned(text)
    assert cleaned == "Hello  world"
    assert pinned == "Important stuff"

def test_apply_backend_overrides():
    with patch('agent._build_backend') as mock_build:
        agent._config["backends"] = {"main": {"kind": "llamacpp"}, "summary": {"kind": "llamacpp"}, "model": "some-model"}
        agent._apply_backend_overrides(main_kind="bedrock", summary_kind=None)
        assert agent._config["backends"]["main"]["kind"] == "bedrock"
        assert "claude-" in agent._config["backends"]["main"]["model"]
        mock_build.assert_called()

def test_summary_backend_unreachable():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        
        # Case: Summary backend raises ConnectionError
        mock_summary.health.side_effect = requests.ConnectionError("Connection refused")
        
        # To avoid infinite loop, we mock input to exit immediately
        with patch('builtins.input', side_effect=['exit']):
            agent.run_agent_interactive(tui=False, auto=False)
        
        # We just want to check if it didn't crash and hit the exception handler
        assert True

def test_summary_backend_unhealthy():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        
        # Case: Summary backend returns unhealthy
        mock_summary.health.return_value = (False, "Disk Full")
        
        with patch('builtins.input', side_effect=['exit']):
            agent.run_agent_interactive(tui=False, auto=False)
        
        assert True

def test_initial_prompt_expansion_error():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit') as mock_emit, \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent._expand_file_refs') as mock_expand:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        # Simulate expansion error
        mock_expand.side_effect = Exception("Expansion failed")
        
        agent.run_agent_interactive(initial_prompt="trigger_fail", tui=False, auto=False)
        
        # Verify error was emitted
        mock_emit.assert_any_call("on_error", ANY)

def test_auto_guidance_cancelled():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="cancelled") as mock_run, \
         patch('agent._handle_auto_guidance') as mock_guidance:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        agent.run_agent_interactive(initial_prompt="Auto task", tui=False, auto=True)
        
        # Verify that run_agent_single returning 'cancelled' triggers auto guidance
        mock_guidance.assert_called()

def test_result_file_writing():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single', return_value="done") as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        # Use a temporary result file
        with patch('builtins.open', MagicMock()) as mock_open:
            agent.run_agent_interactive(initial_prompt="Test result", tui=False, auto=True, result_file="test_output.txt")
            
            # Verify that open() was called to write the result
            mock_open.assert_called()

def test_tui_fallback_logic():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit') as mock_emit, \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        # Mock TUI unavailable
        with patch('tui._AVAILABLE', False):
            agent.run_agent_interactive(tui=True, auto=False)
            
        # Verify a notice was emitted about prompt_toolkit missing
        mock_emit.assert_any_call("on_notice", "warn", ANY)
