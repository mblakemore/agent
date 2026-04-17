import os
import pytest
import re
import time
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
    assert "Error: at least one of 'command' or 'session_id' is required" in result

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
    os.environ["WORKTREE_ROOT"] = "/mnt/droid/repos/agent/temp/20260416_235701/worktrees"
    result = fn(command="git worktree add /mnt/droid/repos/agent/temp/20260416_235701/worktrees/test-wt-safe -b test-branch-safe")
    if "ERROR: Worktree must be created under" in result:
        pytest.fail("Guard blocked a valid worktree path")

def test_exec_command_worktree_guard_unsafe():
    os.environ["WORKTREE_ROOT"] = "/mnt/droid/repos/agent/temp/20260416_235701/worktrees"
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
