import pytest
from agent import _sanitize_tool_args, _salvage_tool_args
import logging

log = logging.getLogger("test")

def test_sanitize_tool_args_no_change():
    args = {"action": "read", "path": "foo.txt"}
    assert _sanitize_tool_args("file", args, log) == args
    assert _sanitize_tool_args("exec_command", {"command": "ls"}, log) == {"command": "ls"}

def test_sanitize_tool_args_basic_garble():
    args = {"action": "write**,content:hello world"}
    expected = {"action": "write", "content": "hello world"}
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_tool_args_complex_garble():
    args = {
        "action": "write", 
        "path": "foo.json**,start_line:1", 
        "end_line": "14**,content:some text"
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["path"] == "foo.json"
    assert result["start_line"] == 1
    assert str(result["end_line"]) == "14"
    assert result["content"] == "some text"
    assert result["action"] == "write"

def test_sanitize_tool_args_invalid_action_recovery():
    args = {"action": "write_this**,content:test"}
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "write"

def test_sanitize_tool_args_non_dict():
    assert _sanitize_tool_args("file", "not a dict", log) == "not a dict"

def test_sanitize_tool_args_int_conversion_success():
    args = {"action": "write", "path": "foo.txt**,start_line:10"}
    result = _sanitize_tool_args("file", args, log)
    assert result["start_line"] == 10

def test_sanitize_tool_args_int_conversion_failure():
    args = {"action": "write", "path": "foo.txt**,start_line:abc"}
    result = _sanitize_tool_args("file", args, log)
    assert result["start_line"] == "abc"

def test_sanitize_tool_args_non_string_values():
    args = {
        "action": "read",
        "path": "test.txt",
        "count": 10,
        "enabled": True,
        "metadata": None
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["count"] == 10
    assert result["enabled"] is True
    assert result["metadata"] is None

def test_salvage_tool_args_json_cleanup():
    raw = '{"action": "read", "path": "foo.txt"} |>'
    assert _salvage_tool_args("file", raw, log) == {"action": "read", "path": "foo.txt"}

def test_salvage_tool_args_file_recovery():
    raw = 'write**,content:hello,path:foo.txt'
    result = _salvage_tool_args("file", raw, log)
    assert result["action"] == "write"
    assert result["path"] == "foo.txt"

def test_salvage_tool_args_exec_recovery():
    raw = 'command: ls -la'
    result = _salvage_tool_args("exec_command", raw, log)
    assert result == {"command": "ls -la"}

def test_salvage_tool_args_failure():
    assert _salvage_tool_args("unknown", "totally broken", log) is None
