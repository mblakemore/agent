import pytest
import os
from unittest.mock import patch, MagicMock
from tools.exec_command import fn, _extract_write_target, _sessions, _temp_session_ids

@pytest.fixture(autouse=True)
def cleanup_exec_state():
    """Clear global state between tests to ensure isolation."""
    _sessions.clear()
    _temp_session_ids.clear()
    # Clear accessed files in tools.file
    from tools.file import _accessed_files
    _accessed_files.clear()
    yield

class TestExecCommand:

    @pytest.mark.parametrize("command, expected", [
        ("cat > file.py << 'EOF'\nprint(1)\nEOF", "file.py"),
        ("cat > test.json << EOF\n{}\nEOF", "test.json"),
        ("echo 'hello' > output.txt", "output.txt"),
        ("printf 'val' > config.toml", "config.toml"),
        ("ls -la", None),
        ("echo 'null' > /dev/null", None),
        ("cat file.txt", None),
        ("cat > somefile.exe << EOF", None), # Not in allowed extensions
    ])
    def test_extract_write_target(self, command, expected):
        assert _extract_write_target(command) == expected

    def test_basic_execution(self):
        # Use a safe command
        result = fn("echo 'Hello World'")
        assert "Hello World" in result
        assert "exit=0" in result

    def test_command_not_found(self):
        result = fn("nonexistent_command_12345")
        assert "exit=127" in result or "not found" in result.lower()

    def test_empty_command_no_session(self):
        result = fn(command="", session_id="")
        assert "Error: at least one of 'command' or 'session_id' is required" in result

    @patch("os.getcwd")
    def test_cd_guard_blocked(self, mock_getcwd):
        # Simulate being in a specific directory
        mock_getcwd.return_value = "/mnt/droid/repos/agent/temp/repo"
        # Try to cd to a system directory
        result = fn("cd /etc && ls")
        assert "Error: You are trying to cd to '/etc' which is outside your repo tree" in result

    @patch("os.getcwd")
    def test_cd_guard_allowed(self, mock_getcwd):
        # Simulate being in a specific directory
        mock_getcwd.return_value = "/mnt/droid/repos/agent/temp/repo"
        # cd to a subdirectory within the repo
        result = fn("cd tests && ls")
        # This should execute (though it might fail if the dir doesn't exist in reality, 
        # but the GUARD should pass)
        assert "Error: You are trying to cd to" not in result

    def test_worktree_guard_blocked(self):
        with patch.dict(os.environ, {"WORKTREE_ROOT": "/mnt/droid/worktrees"}):
            result = fn("git worktree add /tmp/bad-worktree -b branch")
            assert "ERROR: Worktree must be created under /mnt/droid/worktrees" in result

    def test_worktree_guard_allowed(self):
        with patch.dict(os.environ, {"WORKTREE_ROOT": "/mnt/droid/worktrees"}):
            # This will likely fail the actual git command, but the guard should allow it
            result = fn("git worktree add /mnt/droid/worktrees/good-worktree -b branch")
            assert "ERROR: Worktree must be created under" not in result

    def test_shell_write_guard_blocked(self):
        # Create a file first
        with open("test_guard_file.txt", "w") as f:
            f.write("original content")
        
        # Try to write to it without reading first
        result = fn("echo 'new content' > test_guard_file.txt")
        assert "exists but you haven't read it this session" in result
        
        # Cleanup
        if os.path.exists("test_guard_file.txt"):
            os.remove("test_guard_file.txt")

    def test_shell_write_guard_allowed_after_read(self):
        with open("test_read_write.txt", "w") as f:
            f.write("original")
        
        # 1. Read the file
        fn("cat test_read_write.txt")
        
        # 2. Now write to it
        result = fn("echo 'updated' > test_read_write.txt")
        assert "exit=0" in result
        
        if os.path.exists("test_read_write.txt"):
            os.remove("test_read_write.txt")

    def test_shell_read_tracking(self):
        with open("track_me.txt", "w") as f:
            f.write("content")
        
        fn("cat track_me.txt")
        
        from tools.file import _accessed_files
        # resolve the path to match how it's stored
        expected_path = str(os.path.abspath("track_me.txt"))
        assert expected_path in _accessed_files
        
        if os.path.exists("track_me.txt"):
            os.remove("track_me.txt")

    def test_session_creation_and_polling(self):
        # Start a long running process in background
        # Using 'sleep 2' to ensure it stays running for a moment
        result = fn("sleep 2", background=True)
        assert "Command started in background" in result
        
        # Extract session ID
        import re
        # The output format is "[session: sid]\nCommand started..."
        sid_match = re.search(r"\[session: (agent-tmp-[\da-f]+)\]", result)
        if not sid_match:
             # Try alternate match if the one above fails (e.g. different format)
             sid_match = re.search(r"\[session: ([^\]]+)\]", result)
             
        assert sid_match, f"Could not find session ID in result: {result}"
        sid = sid_match.group(1)
        
        # Poll while running
        poll_result = fn(command="", session_id=sid)
        assert "(still running)" in poll_result
        
        # Wait for it to finish
        import time
        time.sleep(2.1)
        
        # Poll after finish
        final_result = fn(command="", session_id=sid)
        assert "exit=0" in final_result
        assert "background process finished" in final_result

    def test_temp_session_limit(self):
        # Max is 4. Create 4 sessions.
        for _ in range(4):
            fn("echo 1", new_session=True)
        
        # The 5th should fail
        result = fn("echo 1", new_session=True)
        assert "Error: temporary session limit reached (4)" in result

    def test_invalid_session_id(self):
        result = fn(command="", session_id="non-existent-session")
        assert "Error: session 'non-existent-session' does not exist" in result
