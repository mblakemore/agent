import pytest
from unittest.mock import MagicMock, patch, ANY
import agent
import tui

def setup_agent_mocks():
    """
    Returns a list of patches and the mocked backends.
    Since we can't use a fixture, we'll use a context manager approach
    in the tests or just put the mocks inside each test.
    """
    # We return a dictionary of the mocks created
    mocks = {}
    
    # Note: These are the paths we need to patch
    patchers = [
        patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")),
        patch('telemetry.init', return_value=True),
        patch('agent._emit'),
        patch('agent.AsyncSummarizer'),
    ]
    
    # We'll handle backends separately because we need to configure them
    return patchers

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
         patch('agent.run_agent_single', return_value="done") as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        with patch('builtins.input', side_effect=['exit']):
            agent.run_agent_interactive(tui=False, auto=False, continue_mode=True)
        mock_run.assert_not_called()

def test_interactive_empty_input():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single') as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        with patch('builtins.input', side_effect=['', 'exit']):
            agent.run_agent_interactive(tui=False, auto=False)
        mock_run.assert_not_called()

def test_interactive_input_error():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('agent.run_agent_single') as mock_run:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        with patch('builtins.input', side_effect=EOFError):
            agent.run_agent_interactive(tui=False, auto=False)
        mock_run.assert_not_called()

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
        
        # Side effect function to fail only on the specific trigger
        def expand_side_effect(text):
            if text == "trigger_fail":
                raise Exception("Expansion failed")
            return text, [], None
            
        mock_expand.side_effect = expand_side_effect
        
        with patch('builtins.input', side_effect=['exit']):
            agent.run_agent_interactive(initial_prompt="trigger_fail", tui=False, auto=False)
        mock_emit.assert_any_call("on_error", ANY)

def test_tui_enabled_flow():
    with patch('agent._setup_logger', return_value=(MagicMock(), "log_path", "err_path")), \
         patch('telemetry.init', return_value=True), \
         patch('agent._emit'), \
         patch('agent.AsyncSummarizer'), \
         patch('agent._main_backend') as mock_main, \
         patch('agent._summary_backend') as mock_summary, \
         patch('tui.TuiSession') as mock_tui_session:
        
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 32768
        mock_summary.health.return_value = (True, "OK")
        mock_summary.detect_ctx_size.return_value = 32768
        
        mock_instance = mock_tui_session.return_value
        mock_instance.prompt.return_value = "exit"
        
        with patch('tui._AVAILABLE', True):
            agent.run_agent_interactive(tui=True, auto=False)
        
        mock_tui_session.assert_called()
        mock_instance.prompt.assert_called()

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
        agent._config["backends"] = {"main": {"kind": "llamacpp"}, "summary": {"kind": "llamacpp"}}
        agent._apply_backend_overrides(main_kind="bedrock", summary_kind=None)
        assert agent._config["backends"]["main"]["kind"] == "bedrock"
        assert "claude-" in agent._config["backends"]["main"]["model"]
        mock_build.assert_called()
