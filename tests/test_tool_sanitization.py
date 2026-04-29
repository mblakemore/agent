import pytest
import logging
from unittest.mock import MagicMock
from agent import _sanitize_tool_args, _salvage_tool_args

logging.basicConfig(level=logging.CRITICAL)
log = logging.getLogger("test_tool_sanitization")

def test_sanitize_non_file_tool():
    # Should return as-is if not 'file' tool
    args = {"param": "val**,key:fix"}
    assert _sanitize_tool_args("exec_command", args, log) == args

def test_sanitize_non_dict_args():
    # Should return as-is if args not dict
    args = "not a dict"
    assert _sanitize_tool_args("file", args, log) == args

def test_sanitize_no_garble():
    # Should return as-is if no **,key: pattern
    args = {"action": "write", "path": "foo.py", "content": "hello"}
    assert _sanitize_tool_args("file", args, log) == args

def test_sanitize_basic_garble():
    # Case: {"action": "write**,content:some text"}
    args = {"action": "write**,content:some text"}
    expected = {"action": "write", "content": "some text"}
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_complex_garble():
    # Case: {"action": "write", "path": "foo.json**,start_line:1", "end_line": 14}
    args = {"action": "write", "path": "foo.json**,start_line:1", "end_line": 14}
    expected = {"action": "write", "path": "foo.json", "start_line": 1, "end_line": 14}
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_multiple_embedded_params():
    # Multiple params in one string
    args = {"action": "write**,path:foo.py**,content:hello**,start_line:10"}
    expected = {"action": "write", "path": "foo.py", "content": "hello", "start_line": 10}
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_invalid_line_number():
    # start_line:abc should stay as string
    args = {"action": "read**,start_line:abc"}
    expected = {"action": "read", "start_line": "abc"}
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_action_fix():
    # action is garbled but contains a valid one (e.g. 'writtte' -> 'write')
    args = {"action": "writtte"}
    # Since there's no **,key: pattern, needs_fix will be False.
    # But action 'writtte' is not in _FILE_ACTIONS.
    # The code should fall through to the 'action' fix logic.
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "write"

def test_salvage_json_cleanup():
    # Strip special tokens and parse
    raw = '{"action": "read"}'
    assert _salvage_tool_args("file", raw, log) == {"action": "read"}

def test_salvage_file_read():
    # Extract path from garbled string
    raw = 'action: read, path: "foo.py"'
    expected = {"action": "read", "path": "foo.py"}
    assert _salvage_tool_args("file", raw, log) == expected

def test_salvage_file_write_with_content():
    # Extract path and content
    raw = 'action: write, path: "foo.py", content: "hello world"'
    expected = {"action": "write", "path": "foo.py", "content": "hello world"}
    assert _salvage_tool_args("file", raw, log) == expected

def test_salvage_exec_command():
    # Extract command
    raw = 'command: "ls -la"'
    expected = {"command": "ls -la"}
    assert _salvage_tool_args("exec_command", raw, log) == expected

def test_salvage_fail():
    # Completely unsalvageable
    raw = 'some random text'
    assert _salvage_tool_args("file", raw, log) is None
