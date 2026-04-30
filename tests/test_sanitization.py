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

def test_salvage_tool_args_json_cleanup():
    raw = "{\"action\": \"read\", \"path\": \"foo.txt\"} |>"
    assert _salvage_tool_args("file", raw, log) == {"action": "read", "path": "foo.txt"}

def test_salvage_tool_args_file_recovery():
    raw = "write**,content:hello,path:foo.txt"
    result = _salvage_tool_args("file", raw, log)
    assert result["action"] == "write"
    assert result["path"] == "foo.txt"
    assert result["content"] == "hello"

def test_salvage_tool_args_exec_recovery():
    raw = "command: ls -la"
    result = _salvage_tool_args("exec_command", raw, log)
    assert result == {"command": "ls -la"}

def test_salvage_tool_args_failure():
    assert _salvage_tool_args("unknown", "totally broken", log) is None

def test_sanitize_tool_args_non_dict():
    assert _sanitize_tool_args("file", "not a dict", log) == "not a dict"

def test_sanitize_tool_args_no_fix_valid_action():
    args = {"action": "read", "path": "foo.txt"}
    assert _sanitize_tool_args("file", args, log) == args

def test_sanitize_tool_args_int_conversion_success():
    args = {"action": "write", "path": "foo.txt**,start_line:10"}
    result = _sanitize_tool_args("file", args, log)
    assert result["start_line"] == 10
    assert isinstance(result["start_line"], int)

def test_sanitize_tool_args_int_conversion_failure():
    args = {"action": "write", "path": "foo.txt**,start_line:abc"}
    result = _sanitize_tool_args("file", args, log)
    assert result["start_line"] == "abc"

def test_sanitize_tool_args_empty_embed_val():
    args = {"action": "write", "path": "foo.txt**,content:"}
    result = _sanitize_tool_args("file", args, log)
    assert "content" not in result

def test_salvage_tool_args_json_parse_fail_no_recovery():
    raw = "{\"action\": \"something\", \"path\": \"foo\""
    assert _salvage_tool_args("file", raw, log) is None

def test_salvage_tool_args_file_no_path():
    raw = "write**,content:hello"
    result = _salvage_tool_args("file", raw, log)
    assert result is None

def test_salvage_tool_args_exec_no_command():
    raw = "something else"
    result = _salvage_tool_args("exec_command", raw, log)
    assert result is None

def test_salvage_tool_args_exception_handling():
    assert _salvage_tool_args("file", None, log) is None

def test_sanitize_tool_args_multiple_garbles_in_one_string():
    args = {
        "path": "foo.txt**,start_line:1**,end_line:2",
        "action": "read"
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["path"] == "foo.txt"
    assert result["start_line"] == 1
    assert result["end_line"] == 2
    assert result["action"] == "read"

def test_sanitize_tool_args_edge_case_trailing_star():
    args = {
        "path": "foo.txt***,start_line:1",
        "action": "read"
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["path"] == "foo.txt"
    assert result["start_line"] == 1

def test_sanitize_tool_args_empty_val_after_colon():
    args = {
        "path": "foo.txt**,start_line:",
        "action": "read"
    }
    result = _sanitize_tool_args("file", args, log)
    assert "start_line" not in result
