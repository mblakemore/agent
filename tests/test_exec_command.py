import os
import pytest
import re
import time
from unittest.mock import patch, MagicMock
from tools.exec_command import fn, cleanup_temp_sessions

def setup_function(function):
    cleanup_temp_sessions()

def test_exec_command_simple():
    # Test a simple successful command
    result = fn(command="echo 'hello world'")
    assert "exit=0" in result
    assert "hello world" in result

def test_exec_command_failure():
    # Test a command that fails
    result = fn(command="ls /nonexistent_directory_12345")
    assert "exit=2" in result or "exit=1" in result

def test_exec_command_no_args():
    # Test calling fn without command or session_id
    result = fn()
    assert "Error: command cannot be empty" in result


def test_exec_command_whitespace_only_spaces():
    # Whitespace-only command should be rejected the same as empty
    result = fn(command="   ")
    assert "Error: command cannot be empty" in result


def test_exec_command_whitespace_only_tab():
    result = fn(command="\t")
    assert "Error: command cannot be empty" in result


def test_exec_command_whitespace_only_newline():
    result = fn(command="\n")
    assert "Error: command cannot be empty" in result


def test_exec_command_whitespace_mixed():
    result = fn(command="  \t  \n  ")
    assert "Error: command cannot be empty" in result

def test_exec_command_cd_guard_safe():
    # Test relative cd (should be allowed)
    result = fn(command="cd .. && pwd")
    assert "exit=0" in result

def test_exec_command_cd_guard_unsafe():
    # Test absolute cd to /tmp (should be blocked)
    result = fn(command="cd /tmp && pwd")
    assert "Error: You are trying to cd to '/tmp'" in result

def test_exec_command_worktree_guard_safe():
    # We need WORKTREE_ROOT set to test this
    os.environ["WORKTREE_ROOT"] = "/mnt/droid/repos/agent/temp/20260417_202516/worktrees"
    result = fn(command="git worktree add /mnt/droid/repos/agent/temp/20260417_202516/worktrees/test-wt-safe -b test-branch-safe")
    if "ERROR: Worktree must be created under" in result:
        pytest.fail("Guard blocked a valid worktree path")

def test_exec_command_worktree_guard_unsafe():
    os.environ["WORKTREE_ROOT"] = "/mnt/droid/repos/agent/temp/20260417_202516/worktrees"
    result = fn(command="git worktree add /tmp/bad-wt -b test-branch")
    assert "ERROR: Worktree must be created under" in result

def test_exec_command_shell_write_guard_unread():
    # Create a file first
    with open("test_unread.txt", "w") as f:
        f.write("initial content")
    
    # Try to write to it without reading it first
    result = fn(command="echo 'new content' > test_unread.txt")
    assert "exists but you haven't read it this session" in result
    
    # Now read it
    fn(command="cat test_unread.txt")
    # Now write should work
    result = fn(command="echo 'new content' > test_unread.txt")
    assert "exit=0" in result
    
    # Cleanup
    os.remove("test_unread.txt")

def test_exec_command_background_and_poll():
    # Start a background process that takes some time
    session_id = fn(command="sleep 2 && echo 'done'", background=True)
    assert "Command started in background" in session_id
    sid = re.search(r'\[session: ([^\]]+)\]', session_id).group(1)
    
    # Poll immediately
    poll1 = fn(session_id=sid)
    assert "(still running)" in poll1
    
    # Wait for it to finish
    time.sleep(2.5)
    
    # Poll again
    poll2 = fn(session_id=sid)
    assert "exit=0" in poll2
    assert "done" in poll2

def test_exec_command_new_session_limit():
    # Create several new sessions
    for _ in range(4):
        fn(command="echo 1", new_session=True)
    
    # The 5th should fail
    result = fn(command="echo 1", new_session=True)
    assert "Error: temporary session limit reached" in result

def test_exec_command_timeout():
    # Test timeout
    result = fn(command="sleep 5", timeout=1)
    assert "timed out after 1s" in result

def test_exec_command_cleanup_sessions():
    # Create a session
    fn(command="echo 1", new_session=True)
    # Call cleanup
    cleanup_temp_sessions()
    # Now creating a new one should be fine (limit reset)
    result = fn(command="echo 1", new_session=True)
    assert "Error: temporary session limit reached" not in result

def test_exec_command_write_sanitizer():
    # To test the sanitizer, the command must produce the junk
    # We use printf to create the exact pattern the sanitizer targets
    command = "printf 'content\\nEOF 2>&1' > test_sanitizer.txt"
    fn(command=command)
    
    with open("test_sanitizer.txt", "r") as f:
        content = f.read()
    
    # The sanitizer should have stripped 'EOF 2>&1'
    assert "EOF 2>&1" not in content
    assert "content" in content
    
    os.remove("test_sanitizer.txt")

def test_exec_command_heredoc_write_target():
    # Test line 29: return m.group(1) in _extract_write_target
    result = fn(command="cat > test_heredoc.py <<'EOF'\nprint('hello')\nEOF")
    assert "exit=0" in result
    os.remove("test_heredoc.py")

def test_exec_command_invalid_session_id():
    # Test line 63: return None, f"Error: session '{session_id}' does not exist"
    result = fn(session_id="nonexistent-session-123")
    assert "Error: session 'nonexistent-session-123' does not exist" in result

def test_exec_command_idle_session():
    # Test line 151: return f"[session: {sid}] (idle)"
    # Create a session that has no active process
    sid_res = fn(command="echo 1", new_session=True)
    sid = re.search(r'\[session: ([^\]]+)\]', sid_res).group(1)
    # Now poll it without a command
    result = fn(session_id=sid)
    # Since it was a simple command, it finished immediately. 
    # The first poll might see it as finished, but let's ensure we get to 'idle'.
    # We need to poll it AFTER it has finished and the process is set to None.
    # Wait for it to finish if it's a background process, but here it's foreground.
    # Foreground commands don't set bg_proc.
    # So a new session that just ran a foreground command should be idle.
    assert "(idle)" in result

def test_exec_command_popen_failure():
    # Test line 252-253: Exception handler for subprocess.Popen
    with patch('subprocess.Popen', side_effect=Exception("Popen failed")):
        result = fn(command="echo 1", background=True)
        assert "Error starting background command: Popen failed" in result

def test_exec_command_resolve_failure():
    # Test line 231-232: Exception handler for target_path.resolve()
    # We mock Path.resolve to raise an exception
    with patch('tools.exec_command.Path.resolve', side_effect=OSError("Resolve failed")):
        # We need a write target to trigger the resolve() call
        result = fn(command="echo 1 > test_resolve_fail.txt")
        # It should not crash, but rather use the target_path string
        assert "exit=0" in result
        os.remove("test_resolve_fail.txt")

def test_exec_command_read_resolve_failure():
    # Test line 308-309: Exception handler for rp.resolve()
    with patch('tools.exec_command.Path.resolve', side_effect=OSError("Resolve failed")):
        result = fn(command="cat test_resolve_fail_read.txt")
        # It should not crash
        assert "exit=1" in result # file doesn't exist, but resolve() was called

def test_exec_command_sanitizer_failure():
    # Test line 294-295: Exception handler in post-write sanitizer
    # We mock read_text to fail
    with patch('tools.exec_command.Path.read_text', side_effect=Exception("Read failed")):
        result = fn(command="echo 1 > test_sanitizer_fail.txt")
        assert "exit=0" in result
        os.remove("test_sanitizer_fail.txt")

def test_exec_command_cleanup_failure():
    # Test line 108-111: Exception handler in cleanup_temp_sessions
    # Mock a process to fail on .kill()
    mock_proc = MagicMock()
    mock_proc.kill.side_effect = Exception("Kill failed")
    
    # We need to inject this mock_proc into the _sessions dict
    from tools.exec_command import _sessions
    _sessions["test-fail-session"] = {"bg_proc": mock_proc, "bg_output": ""}
    
    cleanup_temp_sessions()
    # Should not crash
    assert True

def test_exec_command_bg_read_failure():
    # Test line 98-99: Exception handler in _read_bg_output
    # This is harder because it runs in a thread. 
    # We'll mock the stdout iterator to fail.
    with patch('subprocess.Popen', wraps=subprocess.Popen) as mock_popen:
        # Create a mock process with a stdout that raises an exception when iterated
        mock_proc = MagicMock()
        mock_proc.stdout = iter([b"line1\n", Exception("Read error")]) 
        # This is not quite right for the a real Popen.
        # a better way is to mock the process object entirely.
        pass
def test_exec_command_bg_read_failure():
    # Test line 98-99: Exception handler in _read_bg_output
    # We use a mock process that raises an exception during stdout iteration
    with patch('subprocess.Popen') as mock_popen:
        mock_proc = MagicMock()
        # Simulate an exception when iterating over stdout
        mock_proc.stdout = iter([b"some output", Exception("Read error")])
        # Actually, the for loop will just see the Exception object as an item.
        # We need the iterator itself to raise the exception.
        class FailingIterator:
            def __iter__(self): return self
            def __next__(self): raise Exception("Read error")
        
        mock_proc.stdout = FailingIterator()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc
        
        # Start background process
        res = fn(command="echo 1", background=True)
        sid = re.search(r'\[session: ([^\]]+)\]', res).group(1)
        
        # Wait for the thread to run
        time.sleep(0.5)
        
        # Poll the session
        result = fn(session_id=sid)
        assert "Error reading output: Read error" in result


# ── PYTHONPATH auto-inject tests ──────────────────────────────────────────────

def test_find_git_root_finds_repo(tmp_path):
    from tools.exec_command import _find_git_root
    # Create a fake git root
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "subdir" / "nested"
    subdir.mkdir(parents=True)
    assert _find_git_root(str(subdir)) == str(tmp_path)


def test_build_env_injects_pythonpath(tmp_path):
    from tools.exec_command import _build_env_with_pythonpath
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "pkg"
    subdir.mkdir()
    env_without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with patch.dict(os.environ, env_without, clear=True):
        env = _build_env_with_pythonpath(str(subdir))
    assert env is not None
    assert env["PYTHONPATH"] == str(tmp_path)


def test_build_env_no_inject_when_pythonpath_set(tmp_path):
    from tools.exec_command import _build_env_with_pythonpath
    (tmp_path / ".git").mkdir()
    with patch.dict(os.environ, {"PYTHONPATH": "/already/set"}):
        env = _build_env_with_pythonpath(str(tmp_path))
    assert env is None


def test_exec_command_auto_pythonpath():
    # Running from a git repo directory — PYTHONPATH should be auto-injected
    # so that project modules are importable without explicit PYTHONPATH.
    env_without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with patch.dict(os.environ, env_without, clear=True):
        result = fn(command="python3 -c 'import sys; print(sys.path)'")
    assert "exit=0" in result


# ── timeout validation tests ──────────────────────────────────────────────────

def test_exec_command_negative_timeout_rejected():
    """A negative timeout must be rejected with a clear error, not silently kill the command."""
    result = fn(command="echo hello", timeout=-1)
    assert result == "Error: timeout must be a positive number"


def test_exec_command_negative_timeout_large():
    """Very negative timeout values must also be rejected."""
    result = fn(command="echo hello", timeout=-9999)
    assert result == "Error: timeout must be a positive number"


def test_exec_command_zero_timeout_rejected():
    """timeout=0 is physically impossible (no time to run anything) and must be rejected."""
    result = fn(command="echo hello", timeout=0)
    assert result == "Error: timeout must be a positive number"


def test_exec_command_positive_timeout_still_works():
    """Regression: a normal positive timeout continues to work correctly."""
    result = fn(command="echo hello", timeout=10)
    assert "exit=0" in result
    assert "hello" in result


def test_exec_command_negative_timeout_does_not_kill_command():
    """With a negative timeout the command must NOT run at all — error returned immediately."""
    result = fn(command="sleep 0.1 && echo ran", timeout=-5)
    # Must be the validation error, not a timed-out or successful execution result
    assert result == "Error: timeout must be a positive number"
    assert "ran" not in result
    assert "timed out" not in result


# ── nan/inf timeout tests (#650) ──────────────────────────────────────────────

def test_exec_command_nan_timeout_rejected():
    """float('nan') must be rejected — it would silently disable the deadline check."""
    result = fn(command="echo hello", timeout=float('nan'))
    assert result == "Error: timeout must be a positive number"


def test_exec_command_inf_timeout_rejected():
    """float('inf') must be rejected — it would create an infinite deadline."""
    result = fn(command="echo hello", timeout=float('inf'))
    assert result == "Error: timeout must be a positive number"


def test_exec_command_neg_inf_timeout_rejected():
    """float('-inf') must be rejected along with other non-finite values."""
    result = fn(command="echo hello", timeout=float('-inf'))
    assert result == "Error: timeout must be a positive number"


# ── wrong-type timeout tests (#680) ───────────────────────────────────────────

def test_exec_command_string_timeout_returns_error():
    """A string timeout must return a clean Error string, not raise TypeError (#680)."""
    result = fn(command="echo hello", timeout='5')
    assert result.startswith("Error: timeout must be a number")
    assert "'str'" in result


def test_exec_command_bool_timeout_returns_error():
    """bool is a subclass of int but is not a valid timeout (#680)."""
    result = fn(command="echo hello", timeout=True)
    assert result.startswith("Error: timeout must be a number")
    assert "'bool'" in result


def test_exec_command_list_timeout_returns_error():
    """A list timeout must return a clean Error string, not raise TypeError (#680)."""
    result = fn(command="echo hello", timeout=[5])
    assert result.startswith("Error: timeout must be a number")


def test_exec_command_nan_timeout_does_not_run_command():
    """With a nan timeout the command must NOT run at all — error returned immediately."""
    result = fn(command="echo ran", timeout=float('nan'))
    assert result == "Error: timeout must be a positive number"
    assert "ran" not in result


def test_exec_command_inf_timeout_does_not_run_command():
    """With an inf timeout the command must NOT run at all — error returned immediately."""
    result = fn(command="echo ran", timeout=float('inf'))
    assert result == "Error: timeout must be a positive number"
    assert "ran" not in result


def test_exec_command_int_command_returns_error():
    """Passing an int as command must return an error string, not raise AttributeError."""
    result = fn(command=42, timeout=5)
    assert isinstance(result, str)
    assert "Error" in result


def test_exec_command_none_command_returns_error():
    """Passing None as command must return an error string, not raise AttributeError."""
    result = fn(command=None, timeout=5)
    assert isinstance(result, str)
    assert "Error" in result


# ── output cap tests (#668) ───────────────────────────────────────────────────

def test_exec_command_output_cap_truncates_large_output():
    """Output beyond _MAX_OUTPUT_BYTES must be truncated with a clear notice."""
    from tools.exec_command import _MAX_OUTPUT_BYTES
    # Generate output larger than the cap
    big_output_bytes = _MAX_OUTPUT_BYTES + 1000
    result = fn(
        command=f'python3 -c "print(\'A\' * {big_output_bytes})"',
        timeout=10,
    )
    assert "output truncated" in result
    assert str(big_output_bytes + 1) in result or str(big_output_bytes) in result  # total byte count in notice
    # Result must be shorter than the raw output
    assert len(result) < big_output_bytes


def test_exec_command_output_cap_notice_includes_total_size():
    """The truncation notice must include the total byte count so the agent knows what was cut."""
    from tools.exec_command import _MAX_OUTPUT_BYTES
    big = _MAX_OUTPUT_BYTES + 5000
    result = fn(command=f'python3 -c "print(\'X\' * {big})"', timeout=10)
    assert "bytes total" in result
    assert f"{_MAX_OUTPUT_BYTES}" in result


def test_exec_command_output_cap_small_output_not_truncated():
    """Output below the cap must be returned in full with no truncation notice."""
    result = fn(command="echo 'hello world'", timeout=5)
    assert "output truncated" not in result
    assert "hello world" in result
    assert "exit=0" in result


def test_exec_command_output_cap_exact_boundary_not_truncated():
    """Output of exactly _MAX_OUTPUT_BYTES bytes must NOT be truncated."""
    from tools.exec_command import _MAX_OUTPUT_BYTES
    # 'A' * N plus a newline — rstrip('\n') removes the newline so exactly N bytes remain
    result = fn(
        command=f'python3 -c "import sys; sys.stdout.write(\'A\' * {_MAX_OUTPUT_BYTES})"',
        timeout=10,
    )
    assert "output truncated" not in result


def test_exec_command_output_cap_constant_is_reasonable():
    """_MAX_OUTPUT_BYTES must be a positive integer in a sane range (1 KB – 1 MB)."""
    from tools.exec_command import _MAX_OUTPUT_BYTES
    assert isinstance(_MAX_OUTPUT_BYTES, int)
    assert 1024 <= _MAX_OUTPUT_BYTES <= 1_048_576


# ── cwd parameter tests (#674) ────────────────────────────────────────────────

def test_exec_command_cwd_changes_working_directory(tmp_path):
    """cwd parameter must change the working directory for the command."""
    result = fn(command="pwd", timeout=5, cwd=str(tmp_path))
    assert "exit=0" in result
    # tmp_path may differ from the resolved path due to symlinks — check basename
    assert tmp_path.name in result


def test_exec_command_cwd_relative_paths_resolved_against_cwd(tmp_path):
    """Relative paths in the command must be resolved relative to cwd, not the agent home."""
    result = fn(command="touch marker.txt && ls marker.txt", timeout=5, cwd=str(tmp_path))
    assert "exit=0" in result
    assert "marker.txt" in result
    assert (tmp_path / "marker.txt").exists()


def test_exec_command_cwd_nonexistent_dir_rejected():
    """cwd that does not exist must return a clear error, not crash."""
    result = fn(command="pwd", timeout=5, cwd="/nonexistent_cwd_dir_abc123xyz")
    assert "Error" in result
    assert "does not exist" in result


def test_exec_command_cwd_file_path_rejected(tmp_path):
    """cwd pointing at a file (not a directory) must be rejected with a clear error."""
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hello")
    result = fn(command="pwd", timeout=5, cwd=str(f))
    assert "Error" in result
    assert "not a directory" in result


def test_exec_command_cwd_empty_uses_home_directory():
    """cwd='' (the default) must leave the working directory unchanged."""
    import os
    result = fn(command="pwd", timeout=5, cwd="")
    assert "exit=0" in result
    # The output should contain the agent's home dir (resolved, since bash resolves symlinks)
    home_resolved = os.path.realpath(os.getcwd())
    # On systems with symlinks the printed path may differ; just ensure it doesn't use /tmp
    assert "/tmp" not in result or home_resolved.startswith("/tmp")


def test_exec_command_cwd_outside_repo_tree_allowed(tmp_path):
    """cwd must work for paths outside the repo tree (unlike 'cd /abs && cmd' which is blocked)."""
    # Demonstrate the key use-case: running commands in an arbitrary temp dir
    (tmp_path / "hello.txt").write_text("hello from cwd")
    result = fn(command="cat hello.txt", timeout=5, cwd=str(tmp_path))
    assert "exit=0" in result
    assert "hello from cwd" in result


def test_exec_command_cwd_background_mode(tmp_path):
    """cwd must also apply to background commands."""
    result = fn(command="pwd", background=True, cwd=str(tmp_path))
    assert "Command started in background" in result
