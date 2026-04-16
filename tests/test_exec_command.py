import pytest
from tools.exec_command import fn as exec_command
import os

def test_exec_command_basic():
    # Test basic command execution
    result = exec_command("echo 'hello world'")
    assert "hello world" in result

def test_exec_command_file_creation():
    # Test command that creates a file
    exec_command("touch test_file.txt")
    assert os.path.exists("test_file.txt")
    if os.path.exists("test_file.txt"):
        os.remove("test_file.txt")

def test_exec_command_error():
    # Test command that fails
    result = exec_command("ls /non_existent_directory_12345")
    assert "No such file or directory" in result or "cannot access" in result

def test_exec_command_background():
    # Test background execution
    result = exec_command("sleep 1", background=True)
    assert "Command started in background" in result
    
    # We don't have the session_id easily available in a simple way unless we parse it
    # but we can check if it returns something.
