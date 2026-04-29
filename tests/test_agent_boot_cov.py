import pytest
from unittest.mock import MagicMock, patch
import agent

def test_run_agent_interactive_boot_coverage():
    """
    Test the boot sequence of run_agent_interactive to cover lines 2157-2463.
    """
    # Provide a comprehensive mock config to avoid KeyErrors
    mock_config = {
        "context": {"ctx_size": 10, "max_tokens": 10},
        "generation": {"temperature": 0.7},
        "llm": {"model": "test-model"},
        "summary": {
            "enabled": True,
            "model": "sum-model"
        }
    }
    
    with patch('agent._config', mock_config), \
    patch('agent._setup_logger', return_value=(MagicMock(), "log.txt", "err.txt")), \
    patch('agent.telemetry.init', return_value=True), \
    patch('agent._main_backend') as mock_main, \
    patch('agent._summary_backend') as mock_summary, \
    patch('agent._emit'), \
    patch('agent.TerminalCallbacks'), \
    patch('tui.TuiSession'), \
    patch('agent._llm_request'), \
    patch('builtins.input', side_effect=KeyboardInterrupt): 
        
        mock_main.model = "test-model"
        mock_main.kind = "test-kind"
        mock_main.base_url = "http://test"
        mock_main.health.return_value = (True, "OK")
        mock_main.detect_ctx_size.return_value = 4096
        
        mock_summary.model = "sum-model"
        mock_summary.kind = "sum-kind"
        mock_summary.base_url = "http://sum"
        mock_summary.health.return_value = (True, "OK")
        
        try:
            agent.run_agent_interactive()
        except KeyboardInterrupt:
            pass

def test_git_short_sha_failure():
    """Cover the exception handler in _git_short_sha."""
    with patch('subprocess.check_output', side_effect=Exception("Git failed")):
        assert agent._git_short_sha() == ""

def test_check_worktree_guard_violation():
    """Cover the worktree guard violation path (lines 107-110)."""
    import os
    from pathlib import Path
    
    # Use current directory as cwd and a subdirectory as worktree
    cwd = Path().resolve()
    wt = cwd / "fake_worktree"
    wt.mkdir(exist_ok=True)
    
    # File inside cwd but OUTSIDE worktree
    file_path = cwd / "forbidden_file.txt"
    
    is_violation, correction = agent._check_worktree_guard(str(file_path), str(wt))
    
    assert is_violation is True
    assert str(wt) in correction
    
    wt.rmdir()

def test_check_worktree_guard_no_violation():
    """Cover the no-violation path (line 113)."""
    import os
    from pathlib import Path
    
    cwd = Path().resolve()
    wt = cwd / "fake_worktree"
    wt.mkdir(exist_ok=True)
    
    # File inside worktree
    file_path = wt / "safe_file.txt"
    
    is_violation, correction = agent._check_worktree_guard(str(file_path), str(wt))
    
    assert is_violation is False
    assert correction is None
    
    wt.rmdir()

def test_check_worktree_guard_none_inputs():
    """Cover the None/empty inputs (line 100)."""
    assert agent._check_worktree_guard(None, "/tmp") == (False, None)
    assert agent._check_worktree_guard("/tmp", None) == (False, None)
