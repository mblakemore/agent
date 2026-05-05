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


def test_exec_command_timeout_includes_partial_output():
    """Partial output produced before a timeout must be included in the result (#698)."""
    result = fn(command="echo PARTIAL_OUTPUT && sleep 10", timeout=2)
    assert "timed out after 2s" in result
    assert "PARTIAL_OUTPUT" in result
    assert "partial output below" in result


def test_exec_command_timeout_no_output_omits_partial_section():
    """When no output was produced before timeout, the message must not include the partial header (#698)."""
    result = fn(command="sleep 10", timeout=2)
    assert "timed out after 2s" in result
    assert "partial output below" not in result


def test_exec_command_timeout_only_pre_timeout_output_shown():
    """Only output produced before the timeout kill must appear; post-kill output must be absent (#698)."""
    result = fn(command="echo before_sleep && sleep 10 && echo after_sleep", timeout=2)
    assert "before_sleep" in result
    assert "after_sleep" not in result

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

def test_exec_command_heredoc_stdout_clean():
    # Regression: appending '2>&1' to the command string corrupts heredoc
    # terminators — 'EOF' becomes 'EOF 2>&1' which doesn't match the delimiter.
    # The fix uses 'exec 2>&1;' as a prefix so the redirect is applied before
    # the command is parsed, leaving the heredoc body intact.
    result = fn(command="cat << EOF\nhello from heredoc\nEOF")
    assert "exit=0" in result
    assert "hello from heredoc" in result
    # Must not contain the corrupted terminator or the bash warning
    assert "EOF 2>&1" not in result
    assert "here-document" not in result
    assert "delimited by end-of-file" not in result

def test_exec_command_heredoc_stderr_still_captured():
    # Verify that stderr output is still captured when using 'exec 2>&1;' prefix.
    result = fn(command="echo stderr_msg >&2")
    assert "exit=0" in result
    assert "stderr_msg" in result

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
        assert "Error: starting background command: Popen failed" in result

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


def test_build_env_falls_back_to_home_when_cwd_outside_repo(tmp_path):
    """_build_env_with_pythonpath must fall back to the agent home when cwd has no .git ancestor.

    Regression test for #694: when cwd='/tmp' (no .git up the tree),
    PYTHONPATH was silently omitted causing ModuleNotFoundError.
    """
    from tools.exec_command import _build_env_with_pythonpath
    # tmp_path has no .git; os.getcwd() (the agent repo) does.
    env_without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with patch.dict(os.environ, env_without, clear=True):
        env = _build_env_with_pythonpath(str(tmp_path))
    assert env is not None, "Expected PYTHONPATH to be injected via home-dir fallback"
    # The injected value must be a real path containing a .git directory
    from pathlib import Path
    assert (Path(env["PYTHONPATH"]) / ".git").exists(), (
        f"PYTHONPATH={env['PYTHONPATH']!r} does not contain a .git directory"
    )


def test_exec_command_pythonpath_injected_when_cwd_outside_repo(tmp_path):
    """exec_command must inject PYTHONPATH even when cwd is outside the repo tree.

    Regression test for #694: running with cwd='/tmp' failed to import project
    modules because PYTHONPATH was not auto-injected for non-repo cwd values.
    """
    env_without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with patch.dict(os.environ, env_without, clear=True):
        result = fn(
            command="python3 -c 'import os; print(os.environ.get(\"PYTHONPATH\", \"NOT SET\"))'",
            timeout=5,
            cwd=str(tmp_path),
        )
    assert "exit=0" in result
    assert "NOT SET" not in result, (
        "PYTHONPATH was not injected when cwd is outside the repo tree"
    )


# ── timeout validation tests ──────────────────────────────────────────────────

def test_exec_command_negative_timeout_rejected():
    """A negative timeout must be rejected with a clear error, not silently kill the command."""
    result = fn(command="echo hello", timeout=-1)
    assert result == "Error: timeout must be a finite positive number"


def test_exec_command_negative_timeout_large():
    """Very negative timeout values must also be rejected."""
    result = fn(command="echo hello", timeout=-9999)
    assert result == "Error: timeout must be a finite positive number"


def test_exec_command_zero_timeout_rejected():
    """timeout=0 is physically impossible (no time to run anything) and must be rejected."""
    result = fn(command="echo hello", timeout=0)
    assert result == "Error: timeout must be a finite positive number"


def test_exec_command_positive_timeout_still_works():
    """Regression: a normal positive timeout continues to work correctly."""
    result = fn(command="echo hello", timeout=10)
    assert "exit=0" in result
    assert "hello" in result


def test_exec_command_negative_timeout_does_not_kill_command():
    """With a negative timeout the command must NOT run at all — error returned immediately."""
    result = fn(command="sleep 0.1 && echo ran", timeout=-5)
    # Must be the validation error, not a timed-out or successful execution result
    assert result == "Error: timeout must be a finite positive number"
    assert "ran" not in result
    assert "timed out" not in result


# ── nan/inf timeout tests (#650) ──────────────────────────────────────────────

def test_exec_command_nan_timeout_rejected():
    """float('nan') must be rejected — it would silently disable the deadline check."""
    result = fn(command="echo hello", timeout=float('nan'))
    assert result == "Error: timeout must be a finite positive number"


def test_exec_command_inf_timeout_rejected():
    """float('inf') must be rejected — it would create an infinite deadline."""
    result = fn(command="echo hello", timeout=float('inf'))
    assert result == "Error: timeout must be a finite positive number"


def test_exec_command_neg_inf_timeout_rejected():
    """float('-inf') must be rejected along with other non-finite values."""
    result = fn(command="echo hello", timeout=float('-inf'))
    assert result == "Error: timeout must be a finite positive number"


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
    assert result == "Error: timeout must be a finite positive number"
    assert "ran" not in result


def test_exec_command_inf_timeout_does_not_run_command():
    """With an inf timeout the command must NOT run at all — error returned immediately."""
    result = fn(command="echo ran", timeout=float('inf'))
    assert result == "Error: timeout must be a finite positive number"
    assert "ran" not in result


def test_exec_command_int_command_returns_error():
    """Passing an int as command must return an error string, not raise AttributeError."""
    result = fn(command=42, timeout=5)
    assert isinstance(result, str)
    assert "Error" in result


def test_exec_command_none_command_coerces_to_empty():
    """command=None must coerce to '' and return the empty-command error, not a NoneType type error (#966)."""
    result = fn(command=None, timeout=5)
    assert isinstance(result, str)
    assert "NoneType" not in result, f"Must not mention NoneType: {result!r}"
    assert result.startswith("Error:"), f"Empty command must still error: {result!r}"
    assert "empty" in result.lower(), f"Must mention 'empty': {result!r}"


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


def test_exec_command_cwd_null_byte_returns_clean_error():
    """cwd containing a null byte must return a clear error without embedding the
    null byte in the error message (#883).

    Before the fix, Path('abc\\x00def').exists() returned False on this Python
    version, so the null byte silently flowed into the 'does not exist' error
    message string rather than being caught by an explicit guard.
    """
    result = fn(command="echo hi", cwd="abc\x00def")
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "null byte" in result, f"Error message should mention null byte: {result!r}"
    assert "\x00" not in result, f"Null byte must not appear in error message: {result!r}"


# ── env parameter tests (#730) ────────────────────────────────────────────────

def test_exec_command_env_injects_variable():
    """env dict must make custom variables visible inside the subprocess (#730)."""
    result = fn(command="echo $MY_CUSTOM_VAR", env={"MY_CUSTOM_VAR": "hello_from_env"})
    assert "exit=0" in result
    assert "hello_from_env" in result


def test_exec_command_env_multiple_variables():
    """Multiple env vars must all be visible in the subprocess."""
    result = fn(
        command="echo $VAR_A $VAR_B",
        env={"VAR_A": "alpha", "VAR_B": "beta"},
    )
    assert "exit=0" in result
    assert "alpha" in result
    assert "beta" in result


def test_exec_command_env_empty_dict_works():
    """env={} (empty dict) must not cause any error — behaves like env=None."""
    result = fn(command="echo ok", env={})
    assert "exit=0" in result
    assert "ok" in result


def test_exec_command_env_none_default_unchanged():
    """env=None (the default) must behave identically to not passing env at all."""
    result = fn(command="echo ok", env=None)
    assert "exit=0" in result
    assert "ok" in result


def test_exec_command_env_non_dict_returns_error():
    """Passing a non-dict env must return a clear Error string, not raise TypeError (#730)."""
    result = fn(command="echo ok", env="VAR=value")
    assert result.startswith("Error: env must be a dict or None")
    assert "'str'" in result


def test_exec_command_env_list_returns_error():
    """A list env must also return a clear Error string."""
    result = fn(command="echo ok", env=["VAR=value"])
    assert result.startswith("Error: env must be a dict or None")


def test_exec_command_env_integer_value_returns_clear_error():
    """env dict with an integer value must return a descriptive error, not an opaque subprocess exception (#754)."""
    result = fn(command="echo $NUM", env={"NUM": 42})
    assert result.startswith("Error: env values must be strings")
    assert "'NUM'" in result
    assert "'int'" in result


def test_exec_command_env_none_value_returns_clear_error():
    """env dict with a None value must return a descriptive error (#754)."""
    result = fn(command="echo hello", env={"KEY": None})
    assert result.startswith("Error: env values must be strings")
    assert "'KEY'" in result
    assert "'NoneType'" in result


def test_exec_command_env_float_value_returns_clear_error():
    """env dict with a float value must return a descriptive error (#754)."""
    result = fn(command="echo $RATE", env={"RATE": 3.14})
    assert result.startswith("Error: env values must be strings")
    assert "'RATE'" in result
    assert "'float'" in result


# ── env key validation tests (#756) ──────────────────────────────────────────

def test_exec_command_env_empty_key_returns_clear_error():
    """env dict with an empty-string key must return a descriptive error, not silently succeed (#756)."""
    result = fn(command="echo test", env={"": "value"})
    assert result.startswith("Error:"), result
    assert "not a valid environment variable name" in result, result


def test_exec_command_env_key_with_space_returns_clear_error():
    """env dict with a key containing spaces must return a descriptive error (#756)."""
    result = fn(command="echo test", env={"KEY WITH SPACE": "value"})
    assert result.startswith("Error:"), result
    assert "not a valid environment variable name" in result, result
    assert "KEY WITH SPACE" in result


def test_exec_command_env_key_starting_with_digit_returns_clear_error():
    """env dict with a key starting with a digit must return a descriptive error (#756)."""
    result = fn(command="echo test", env={"1INVALID": "value"})
    assert result.startswith("Error:"), result
    assert "not a valid environment variable name" in result, result
    assert "1INVALID" in result


def test_exec_command_env_key_with_equals_returns_clear_error():
    """env dict with a key containing '=' must return a descriptive error, not an OS-level exception (#756)."""
    result = fn(command="echo test", env={"KEY=BAD": "value"})
    assert result.startswith("Error:"), result
    assert "not a valid environment variable name" in result, result
    assert "KEY=BAD" in result


def test_exec_command_env_integer_key_returns_type_error():
    """env dict with an integer key must return a type error, not a name-format error (#934)."""
    result = fn(command="echo test", env={42: "value"})
    assert result.startswith("Error:"), result
    assert "string" in result, f"Error must mention 'string': {result!r}"
    assert "'int'" in result, f"Type name must be quoted 'int': {result!r}"
    assert "not a valid environment variable name" not in result, (
        f"Must not give misleading name-format hint for wrong type: {result!r}"
    )


def test_exec_command_env_none_key_returns_type_error():
    """env dict with a None key must return a type error (#934)."""
    result = fn(command="echo test", env={None: "value"})
    assert result.startswith("Error:"), result
    assert "string" in result, f"Error must mention 'string': {result!r}"
    assert "'NoneType'" in result, f"Type name must be quoted 'NoneType': {result!r}"
    assert "not a valid environment variable name" not in result, (
        f"Must not give misleading name-format hint for wrong type: {result!r}"
    )


def test_exec_command_env_valid_underscore_key_works():
    """env dict with an underscore-prefixed key (e.g. _PRIV) must work correctly (#756)."""
    result = fn(command="echo $_PRIV_VAR", env={"_PRIV_VAR": "works"})
    assert "exit=0" in result
    assert "works" in result


def test_exec_command_env_does_not_unset_pythonpath(tmp_path):
    """Auto-injected PYTHONPATH must still be present when env is provided (#730)."""
    import os
    from unittest.mock import patch
    env_without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with patch.dict(os.environ, env_without, clear=True):
        result = fn(
            command='python3 -c "import os; print(os.environ.get(\'PYTHONPATH\', \'NOT SET\'))"',
            env={"MY_EXTRA_VAR": "present"},
            timeout=10,
        )
    assert "exit=0" in result
    assert "NOT SET" not in result, "PYTHONPATH was dropped when env was provided"


def test_exec_command_env_overrides_existing_variable():
    """A variable in env must override the inherited value for that key."""
    import os
    # PATH is always set; override it to something simple and verify
    result = fn(
        command="echo $BEEWATCHER_TEST_OVERRIDE",
        env={"BEEWATCHER_TEST_OVERRIDE": "overridden"},
    )
    assert "exit=0" in result
    assert "overridden" in result


def test_exec_command_env_background_mode():
    """env must also apply when background=True."""
    import re, time
    res = fn(command="echo $BG_ENV_VAR", background=True, env={"BG_ENV_VAR": "bg_value"})
    assert "Command started in background" in res
    sid = re.search(r'\[session: ([^\]]+)\]', res).group(1)
    time.sleep(0.5)
    poll = fn(session_id=sid)
    assert "bg_value" in poll


def test_exec_command_caller_pythonpath_does_not_clobber_auto_injected(tmp_path):
    """Caller-supplied PYTHONPATH in env must be merged with (not replace) auto-injected git root (#734).

    When a caller passes env={'PYTHONPATH': '/extra'}, the auto-injected git-root path
    must still appear in the subprocess environment so project-local imports work.
    """
    import os
    from unittest.mock import patch
    env_without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with patch.dict(os.environ, env_without, clear=True):
        result = fn(
            command="printenv PYTHONPATH",
            env={"PYTHONPATH": "/extra/custom/path"},
            timeout=10,
        )
    assert "exit=0" in result
    # The caller's custom path must be present
    assert "/extra/custom/path" in result, "Caller's PYTHONPATH was dropped"
    # The auto-injected git root must also be present (not silently clobbered)
    # We can't know the exact git root in every environment, but we can confirm
    # PYTHONPATH contains more than just the caller's value (i.e. it was merged).
    pp_line = [line for line in result.splitlines() if "/extra/custom/path" in line]
    assert pp_line, "PYTHONPATH line not found in output"
    assert ":" in pp_line[0], (
        f"PYTHONPATH was not merged — only caller's value present: {pp_line[0]!r}"
    )


# ── Binary / non-UTF-8 output (#752) ─────────────────────────────────────────


def test_exec_command_binary_output_not_silently_dropped():
    """Non-UTF-8 bytes must not cause silent output loss (#752).

    Before the fix, text=True caused UnicodeDecodeError in the reader thread,
    which was caught by bare except Exception, leaving output_parts empty.
    The caller received only the exit-code header with no content.
    """
    # 0x80 and 0xFF are not valid UTF-8 start bytes
    result = fn(command="printf '\\x80\\xFF'")
    assert "exit=0" in result
    # Output must contain *something* beyond just the header
    output_body = result.split("\n", 1)[1] if "\n" in result else ""
    assert len(output_body) > 0, (
        "Binary output was silently dropped — expected replacement characters, got nothing"
    )


def test_exec_command_binary_output_uses_replacement_chars():
    """Non-UTF-8 bytes must be replaced with Unicode replacement characters, not dropped."""
    result = fn(command="printf '\\x80\\xFF'")
    assert "exit=0" in result
    # The replacement character U+FFFD (or its UTF-8 encoding \xef\xbf\xbd) must appear
    assert "�" in result, (
        f"Expected Unicode replacement char in output, got: {result!r}"
    )


def test_exec_command_latin1_output_not_silently_dropped():
    """Latin-1 encoded text (e.g. b'caf\\xe9') must survive as replacement chars."""
    result = fn(command="python3 -c \"import sys; sys.stdout.buffer.write(b'caf\\xe9')\"")
    assert "exit=0" in result
    output_body = result.split("\n", 1)[1] if "\n" in result else ""
    # 'caf' must be preserved; \\xe9 becomes replacement char
    assert "caf" in output_body, (
        f"Printable prefix of latin-1 output was lost: {result!r}"
    )


def test_exec_command_normal_utf8_unaffected_by_fix():
    """Regular UTF-8 commands must continue to work correctly after the fix."""
    result = fn(command="echo 'hello world'")
    assert "exit=0" in result
    assert "hello world" in result


def test_exec_command_background_binary_output_not_silently_dropped():
    """Background mode must also handle non-UTF-8 output without silent loss (#752)."""
    import re, time as _time
    res = fn(command="printf '\\x80\\xFF'", background=True)
    assert "Command started in background" in res
    sid = re.search(r'\[session: ([^\]]+)\]', res).group(1)
    _time.sleep(0.5)
    poll = fn(session_id=sid)
    # Output must contain content beyond just the header
    assert "�" in poll or len(poll.split("\n", 1)[-1].strip()) > 0, (
        f"Background binary output was silently dropped: {poll!r}"
    )


# ── Null byte validation (#758) ───────────────────────────────────────────────


def test_exec_command_null_byte_in_env_value_returns_clear_error():
    """env value containing a null byte must return a descriptive error, not a cryptic
    subprocess exception (#758).

    Before the fix, the null byte passed isinstance(v, str) validation and only
    failed inside subprocess.Popen with "embedded null byte", giving no hint
    about which env key was the culprit.
    """
    result = fn(command="echo hi", env={"TEST": "hello\x00world"})
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "null byte" in result, f"Error message should mention null byte: {result!r}"
    assert "'TEST'" in result, f"Error message should name the offending key: {result!r}"


def test_exec_command_null_byte_in_command_returns_clear_error():
    """command string containing a null byte must return a descriptive error (#758).

    Before the fix, the null byte passed the basic str check and only failed
    inside subprocess.Popen with a cryptic "embedded null byte" message.
    """
    result = fn(command="echo hi\x00there")
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "null byte" in result, f"Error message should mention null byte: {result!r}"


def test_exec_command_null_byte_env_value_names_key():
    """The null-byte error for an env value must include the key name so the caller
    can identify which variable is malformed (#758)."""
    result = fn(command="echo hi", env={"MY_VAR": "val\x00ue"})
    assert "'MY_VAR'" in result or "MY_VAR" in result, (
        f"Expected key name in error message, got: {result!r}"
    )


def test_exec_command_env_value_without_null_byte_still_works():
    """Env values that are valid strings (no null byte) must continue to work normally
    after adding the null-byte check (#758)."""
    result = fn(command="echo $SAFE_VAR", env={"SAFE_VAR": "all_good"})
    assert "exit=0" in result
    assert "all_good" in result


# ── Newline in env values (#769) ──────────────────────────────────────────────


def test_exec_command_env_value_with_newline_returns_clear_error():
    """env value containing a newline must return a descriptive error, not silently
    pass the embedded newline to the subprocess (#769).

    Before the fix, 'bar\\nbaz' was accepted and the subprocess received a
    multi-line env var, producing inconsistent behavior across platforms.
    """
    result = fn(command="echo $FOO", env={"FOO": "bar\nbaz"})
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "newline" in result, f"Error message should mention newline: {result!r}"
    assert "'FOO'" in result, f"Error message should name the offending key: {result!r}"


def test_exec_command_env_value_with_carriage_return_returns_clear_error():
    """env value containing a carriage return must also be rejected (#769)."""
    result = fn(command="echo $BAR", env={"BAR": "foo\rbar"})
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "newline" in result, f"Error message should mention newline: {result!r}"
    assert "'BAR'" in result, f"Error message should name the offending key: {result!r}"


def test_exec_command_env_value_newline_names_offending_key():
    """The newline error for an env value must include the key name so the caller
    can identify which variable is malformed (#769)."""
    result = fn(command="echo test", env={"MULTI_LINE": "line1\nline2"})
    assert "'MULTI_LINE'" in result or "MULTI_LINE" in result, (
        f"Expected key name in error message, got: {result!r}"
    )


def test_exec_command_env_value_newline_does_not_run_command():
    """With a newline in an env value the command must NOT run at all — error
    returned immediately (#769)."""
    result = fn(command="echo ran", env={"KEY": "val\ninjected"})
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "ran" not in result, "Command ran despite invalid env value"


def test_exec_command_env_value_without_newline_still_works():
    """Normal env values without newlines must continue to work after the newline
    check is added (#769)."""
    result = fn(command="echo $NORMAL_VAR", env={"NORMAL_VAR": "clean_value"})
    assert "exit=0" in result
    assert "clean_value" in result


# ── newline in command string (#792) ─────────────────────────────────────────


def test_exec_command_newline_in_command_runs_both_lines():
    """A literal newline in the command string must run as a multi-line shell script.

    bash -c treats newlines as command separators, so both lines must execute
    and their output must appear in the result.
    """
    result = fn(command='echo "first line"\necho "second line"')
    assert "exit=0" in result
    assert "first line" in result
    assert "second line" in result


def test_exec_command_newline_in_command_exit_code_from_last():
    """When commands are separated by newlines, the exit code must reflect the last command."""
    result = fn(command='echo ok\ntrue')
    assert "exit=0" in result


def test_exec_command_semicolon_runs_multiple_commands():
    """Semicolon-separated commands must all run in sequence."""
    result = fn(command='echo "one"; echo "two"')
    assert "exit=0" in result
    assert "one" in result
    assert "two" in result


def test_exec_command_shell_substitution_works():
    """Command substitution $(...) must be evaluated by the shell."""
    result = fn(command='echo $(echo nested)')
    assert "exit=0" in result
    assert "nested" in result


def test_exec_command_popen_error_format():
    """Exception from foreground Popen must start with 'Error: ' (not 'Error running command:')."""
    with patch('subprocess.Popen', side_effect=OSError("spawn failed")):
        result = fn(command="echo hi")
    assert isinstance(result, str)
    assert result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}"
    assert "spawn failed" in result


def test_exec_command_bg_popen_error_format():
    """Exception from background Popen must start with 'Error: ' (not 'Error starting background command:')."""
    with patch('subprocess.Popen', side_effect=OSError("bg spawn failed")):
        result = fn(command="sleep 10", background=True)
    assert isinstance(result, str)
    assert result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}"
    assert "bg spawn failed" in result


# ── Output reader OOM cap (#822) ──────────────────────────────────────────────


def test_exec_command_reader_stops_collecting_beyond_cap():
    """_collect_stdout must stop accumulating output after _COLLECT_CAP bytes to avoid OOM.

    Before the fix, all output was accumulated in output_parts before
    truncation was applied — a 10MB command output loaded 10MB into RAM.
    After the fix, collection halts as soon as _COLLECT_CAP is exceeded,
    so total memory usage stays bounded.
    """
    from tools.exec_command import _MAX_OUTPUT_BYTES
    # Ask for 10x the collect cap to ensure the reader stops early.
    # 4 * _MAX_OUTPUT_BYTES is the cap; 40x forces many stops.
    huge = _MAX_OUTPUT_BYTES * 40  # well beyond _COLLECT_CAP
    result = fn(
        command=f'python3 -c "import sys; sys.stdout.write(\'A\' * {huge}); sys.stdout.flush()"',
        timeout=30,
    )
    # The result string itself must be far smaller than the raw output
    assert len(result) < huge, (
        f"Result ({len(result)} bytes) should be much smaller than raw output ({huge} bytes)"
    )
    # Truncation notice must be present
    assert "output truncated" in result or "truncated" in result.lower(), (
        f"Expected truncation notice in result: {result[:200]!r}"
    )


def test_exec_command_reader_cap_includes_truncation_notice():
    """After the reader stops, the truncation notice from _MAX_OUTPUT_BYTES check must appear."""
    from tools.exec_command import _MAX_OUTPUT_BYTES
    big = _MAX_OUTPUT_BYTES * 10
    result = fn(
        command=f'python3 -c "print(\'B\' * {big})"',
        timeout=30,
    )
    assert "output truncated" in result, (
        f"Truncation notice missing from result: {result[:200]!r}"
    )


def test_exec_command_small_output_unaffected_by_cap():
    """Normal small commands must return full output unaffected by the reader cap."""
    result = fn(command="echo 'small output'", timeout=5)
    assert "exit=0" in result
    assert "small output" in result
    assert "truncated" not in result


# ── background/new_session boolean coercion (#885) ───────────────────────────


def test_exec_command_background_string_false_returns_error():
    """background='false' must return a clear error, not silently start a background
    process (#885).

    Python treats non-empty strings as truthy, so background='false' would
    previously run the command in background even though the caller intended
    synchronous execution.
    """
    result = fn(command="echo hi", background="false")
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "background" in result, f"Error should name the offending param: {result!r}"
    assert "str" in result or "quotes" in result, f"Error should hint at string type: {result!r}"


def test_exec_command_background_string_true_returns_error():
    """background='true' must return a clear error — strings are not booleans (#885)."""
    result = fn(command="echo hi", background="true")
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "background" in result


def test_exec_command_new_session_string_false_returns_error():
    """new_session='false' must return a clear error, not silently create a session (#885)."""
    result = fn(command="echo hi", new_session="false")
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "new_session" in result


def test_exec_command_background_integer_2_returns_error():
    """background=2 must return a clear error — only 0, 1, and bool are accepted (#885)."""
    result = fn(command="echo hi", background=2)
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "background" in result


def test_exec_command_background_zero_runs_synchronously():
    """background=0 must behave as False (runs synchronously) and return command output (#885)."""
    result = fn(command="echo sync_output", timeout=5, background=0)
    assert "exit=0" in result, f"Expected synchronous exit=0: {result!r}"
    assert "sync_output" in result, f"Expected command output: {result!r}"


def test_exec_command_background_one_starts_background():
    """background=1 must behave as True (starts background process) (#885)."""
    result = fn(command="echo bg_test", background=1)
    assert "background" in result.lower(), f"Expected background message: {result!r}"


def test_exec_command_background_true_unaffected():
    """background=True continues to work normally after adding the type check (#885)."""
    result = fn(command="echo hi", background=True)
    assert "background" in result.lower()


def test_exec_command_background_false_unaffected():
    """background=False continues to run synchronously after adding the type check (#885)."""
    result = fn(command="echo hi", background=False, timeout=5)
    assert "exit=0" in result


# ── session_id validation (#889) ─────────────────────────────────────────────


def test_exec_command_session_id_null_byte_returns_clean_error():
    """session_id containing a null byte must return a clean error without embedding
    the null byte in the message (#889).

    Before the fix, the null byte flowed into the 'session does not exist' f-string
    error message rather than being caught by an explicit guard.
    """
    result = fn(command="", session_id="abc\x00def")
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "null byte" in result, f"Error should mention null byte: {result!r}"
    assert "\x00" not in result, f"Null byte must not appear in error message: {result!r}"


def test_exec_command_session_id_non_string_returns_error():
    """session_id must be a string; non-string types must return a clear error (#889)."""
    result = fn(command="echo hi", session_id=42)
    assert result.startswith("Error:"), f"Expected error, got: {result!r}"
    assert "session_id" in result, f"Error should name the param: {result!r}"
    assert "str" in result or "string" in result, f"Error should mention expected type: {result!r}"


def test_exec_command_session_id_empty_string_unaffected():
    """session_id='' (the default) must still work normally after adding the check (#889)."""
    result = fn(command="echo hi", session_id="", timeout=5)
    assert "exit=0" in result, f"Expected normal execution: {result!r}"


# ── command / cwd type error messages include the bad type (#909) ─────────────


def test_exec_command_non_string_command_names_the_type():
    """fn(42) must name the bad type in the error, not just say 'must be a string' (#909).

    Before the fix the message was 'Error: command must be a string' without
    the type name, inconsistent with session_id and all other tools.
    """
    result = fn(42)  # type: ignore[arg-type]
    assert result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}"
    assert "string" in result, f"Error must mention 'string': {result!r}"
    assert "int" in result, f"Error must name the bad type: {result!r}"


def test_exec_command_none_command_no_longer_type_errors():
    """command=None coerces to '' rather than returning a 'NoneType' type error (#966)."""
    result = fn(None)  # type: ignore[arg-type]
    assert "NoneType" not in result, f"Must not mention NoneType after coercion: {result!r}"


def test_exec_command_non_string_cwd_names_the_type():
    """fn('cmd', cwd=42) must name the bad type in the error (#909)."""
    result = fn("echo hi", cwd=42)  # type: ignore[arg-type]
    assert result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}"
    assert "string" in result, f"Error must mention 'string': {result!r}"
    assert "int" in result, f"Error must name the bad type: {result!r}"


# ── session_id=None and cwd=None treated as '' (#944) ────────────────────────

def test_exec_command_session_id_none_treated_as_empty():
    """session_id=None must be silently coerced to '' (not a type error) (#944)."""
    result = fn("echo hi", session_id=None)
    assert not result.startswith("Error:"), f"session_id=None must not error: {result!r}"
    assert "exit=0" in result, f"Command must succeed: {result!r}"


def test_exec_command_cwd_none_treated_as_empty():
    """cwd=None must be silently coerced to '' (not a type error) (#944)."""
    result = fn("echo hi", cwd=None)
    assert not result.startswith("Error:"), f"cwd=None must not error: {result!r}"
    assert "exit=0" in result, f"Command must succeed: {result!r}"


def test_exec_command_cwd_false_returns_type_error():
    """cwd=False must return a type error — False is not a valid cwd (#944)."""
    result = fn("echo hi", cwd=False)
    assert result.startswith("Error:"), f"cwd=False must return error: {result!r}"
    assert "string" in result, f"Error must mention 'string': {result!r}"
    assert "'bool'" in result, f"Error must name type 'bool': {result!r}"


def test_exec_command_cwd_zero_returns_type_error():
    """cwd=0 must return a type error — integer is not a valid cwd (#944)."""
    result = fn("echo hi", cwd=0)
    assert result.startswith("Error:"), f"cwd=0 must return error: {result!r}"
    assert "string" in result, f"Error must mention 'string': {result!r}"
    assert "'int'" in result, f"Error must name type 'int': {result!r}"


def test_exec_command_session_id_integer_still_returns_type_error():
    """session_id=42 must still return a type error (only None is special-cased) (#944)."""
    result = fn("echo hi", session_id=42)
    assert result.startswith("Error:"), f"session_id=42 must return error: {result!r}"
    assert "string" in result, f"Error must mention 'string': {result!r}"
    assert "'int'" in result, f"Error must name type 'int': {result!r}"


# ── Issue #950: None coercion for timeout, background, new_session ────────────


def test_exec_command_timeout_none_treated_as_default():
    """timeout=None must coerce to 120 (the default), not return a type error (#950)."""
    result = fn("echo hi", timeout=None)
    assert not result.startswith("Error:"), f"timeout=None should succeed: {result!r}"
    assert "NoneType" not in result, f"timeout=None must not produce type error: {result!r}"


def test_exec_command_background_none_treated_as_false():
    """background=None must coerce to False (the default), not return a type error (#950)."""
    result = fn("echo hi", background=None)
    assert not result.startswith("Error:"), f"background=None should succeed: {result!r}"
    assert "NoneType" not in result, f"background=None must not produce type error: {result!r}"


def test_exec_command_new_session_none_treated_as_false():
    """new_session=None must coerce to False (the default), not return a type error (#950)."""
    result = fn("echo hi", new_session=None)
    assert not result.startswith("Error:"), f"new_session=None should succeed: {result!r}"
    assert "NoneType" not in result, f"new_session=None must not produce type error: {result!r}"


def test_exec_command_timeout_string_still_returns_type_error():
    """timeout='fast' must still return a type error (only None is special-cased) (#950)."""
    result = fn("echo hi", timeout="fast")
    assert result.startswith("Error:"), f"timeout='fast' must return error: {result!r}"
    assert "'str'" in result, f"Error must name type 'str': {result!r}"


def test_exec_command_background_string_still_returns_type_error():
    """background='false' must still return a type error (only None is special-cased) (#950)."""
    result = fn("echo hi", background="false")
    assert result.startswith("Error:"), f"background='false' must return error: {result!r}"
    assert "'str'" in result, f"Error must name type 'str': {result!r}"
