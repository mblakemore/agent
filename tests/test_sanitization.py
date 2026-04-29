import pytest
from agent import _sanitize_tool_args, _salvage_tool_args
import logging

# Setup a dummy logger
log = logging.getLogger("test")

def test_sanitize_tool_args_no_change():
    # No garble, should return as is
    args = {"action": "read", "path": "foo.txt"}
    assert _sanitize_tool_args("file", args, log) == args
    # Non-file tool, should return as is
    assert _sanitize_tool_args("exec_command", {"command": "ls"}, log) == {"command": "ls"}

def test_sanitize_tool_args_basic_garble():
    # Gemma 4 style garble: **,key:value
    args = {"action": "write**,content:hello world"}
    expected = {"action": "write", "content": "hello world"}
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_tool_args_complex_garble():
    # Multiple garbles across fields
    args = {
        "action": "write", 
        "path": "foo.json**,start_line:1", 
        "end_line": "14**,content:some text"
    }
    # path: "foo.json", start_line: 1, end_line: 14, content: "some text"
    result = _sanitize_tool_args("file", args, log)
    assert result["path"] == "foo.json"
    assert result["start_line"] == 1
    assert str(result["end_line"]) == "14"
    assert result["content"] == "some text"
    assert result["action"] == "write"

def test_sanitize_tool_args_invalid_action_recovery():
    # Action is garbled and not in _FILE_ACTIONS, but contains a valid one
    args = {"action": "write_this**,content:test"}
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "write"

def test_salvage_tool_args_json_cleanup():
    # Test the special token cleanup
    raw = '{"action": "read", "path": "foo.txt"} |>'
    # salvage cleans |> and tries json.loads
    assert _salvage_tool_args("file", raw, log) == {"action": "read", "path": "foo.txt"}

def test_salvage_tool_args_file_recovery():
    # Recovery of "action,path:..." pattern
    raw = 'write**,content:hello,path:foo.txt'
    result = _salvage_tool_args("file", raw, log)
    assert result["action"] == "write"
    assert result["path"] == "foo.txt"
    assert result["content"] == "hello"

def test_salvage_tool_args_exec_recovery():
    # Recovery of exec_command
    raw = 'command: ls -la'
    result = _salvage_tool_args("exec_command", raw, log)
    assert result == {"command": "ls -la"}

def test_salvage_tool_args_failure():
    # Totally unsalvageable
    assert _salvage_tool_args("unknown", "totally broken", log) is None
