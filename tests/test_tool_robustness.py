import pytest
import logging
import re
from agent import _sanitize_tool_args, _salvage_tool_args

# Mock logger
class MockLog:
    def warning(self, msg, *args): pass
    def info(self, msg, *args): pass
    def debug(self, msg, *args): pass

log = MockLog()

def test_sanitize_tool_args_clean():
    # Non-file tools should be returned as-is
    args = {"command": "ls"}
    assert _sanitize_tool_args("exec_command", args, log) == args
    
    # File tool with clean args
    args = {"action": "read", "path": "test.txt"}
    assert _sanitize_tool_args("file", args, log) == args

def test_sanitize_tool_args_garbled_action():
    # Test garbled action field: {"action": "write**,content:hello world"}
    args = {"action": "write**,content:hello world"}
    expected = {"action": "write", "content": "hello world"}
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_tool_args_garbled_path():
    # Test garbled path field: {"action": "read", "path": "foo.json**,start_line:1", "end_line": 14}
    # Note: In this case, "end_line" is already present, but "start_line" is embedded.
    args = {
        "action": "read", 
        "path": "foo.json**,start_line:1", 
        "end_line": 14
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["path"] == "foo.json"
    assert result["start_line"] == 1
    assert result["end_line"] == 14

def test_sanitize_tool_args_complex_garble():
    # Mix of multiple garbled fields
    args = {
        "action": "write**,content:Hello**,start_line:10",
        "path": "test.py**,end_line:20"
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "write"
    assert result["content"] == "Hello"
    assert result["start_line"] == 10
    assert result["path"] == "test.py"
    assert result["end_line"] == 20

def test_salvage_tool_args_json_cleanup():
    # Test cleanup of Gemma 4 tokens
    raw = '{"command": "ls"} |>'
    assert _salvage_tool_args("exec_command", raw, log) == {"command": "ls"}

def test_salvage_tool_args_file_garbled():
    # Pattern: "action,key:value"
    raw = 'write,path:test.txt,content:hello'
    result = _salvage_tool_args("file", raw, log)
    assert result == {"action": "write", "path": "test.txt", "content": "hello"}

def test_salvage_tool_args_exec_garbled():
    # Pattern: command["\s:]+(.+)
    raw = 'command: ls -la'
    result = _salvage_tool_args("exec_command", raw, log)
    assert result == {"command": "ls -la"}

def test_salvage_tool_args_unsalvageable():
    raw = 'completely broken text'
    assert _salvage_tool_args("unknown", raw, log) is None

def test_sanitize_tool_args_invalid_int():
    # Trigger ValueError in int(embed_val) at line 1064
    args = {"path": "test.txt**,start_line:abc"}
    result = _sanitize_tool_args("file", args, log)
    assert result["start_line"] == "abc"

def test_sanitize_tool_args_action_salvage():
    # Trigger salvage loop at line 1080
    # Needs "action" in fixed and fixed["action"] not in _FILE_ACTIONS
    # and one of _FILE_ACTIONS in str(fixed["action"]).lower()
    args = {"action": "read_this_file_now"}
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "read"

def test_salvage_tool_args_exception():
    # Trigger broad except block at line 1134
    # Passing None should trigger a TypeError in .replace() or .lower()
    assert _salvage_tool_args("file", None, log) is None

def test_sanitize_tool_args_fuzzy_match():
    # Test fuzzy match (typo) for action
    args = {"action": "readd"}
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "read"

def test_sanitize_tool_args_no_match():
    # Test action that doesn't match anything
    args = {"action": "completely_wrong"}
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "completely_wrong"

def test_sanitize_tool_args_non_dict():
    # Test non-dict args
    assert _sanitize_tool_args("file", "not a dict", log) == "not a dict"

def test_salvage_tool_args_json_decode_error():
    # Test JSONDecodeError path in _salvage_tool_args
    # String that looks like JSON but is invalid
    raw = '{"action": "read", "path": "test.txt", ' # trailing comma/incomplete
    # Since it's "file", it will try to salvage using regex if JSON fails
    # But we want to hit the JSONDecodeError block.
    # If we use a tool other than 'file' or 'exec_command', it will just fail JSON and return None.
    result = _salvage_tool_args("other", raw, log)
    assert result is None

def test_salvage_tool_args_file_json_salvage():
    # Test when JSON fails but regex salvage works for 'file'
    raw = 'read,path:test.txt'
    result = _salvage_tool_args("file", raw, log)
    assert result == {"action": "read", "path": "test.txt"}

def test_salvage_tool_args_exec_json_salvage():
    # Test when JSON fails but regex salvage works for 'exec_command'
    raw = 'command: ls -la'
    result = _salvage_tool_args("exec_command", raw, log)
    assert result == {"command": "ls -la"}
