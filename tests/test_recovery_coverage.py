import pytest
import logging
from agent import _sanitize_tool_args, _salvage_tool_args

# Setup a dummy logger for the functions
logger = logging.getLogger("test_recovery")
logger.setLevel(logging.INFO)

@pytest.mark.parametrize("func_name, args, expected", [
    # Non-file tool should return args as-is
    ("exec_command", {"command": "ls"}, {"command": "ls"}),
    # Not a dict should return args as-is
    ("file", "not-a-dict", "not-a-dict"),
    # Valid file tool args with no garble should return as-is
    ("file", {"action": "read", "path": "test.txt"}, {"action": "read", "path": "test.txt"}),
    # Garbled args with **,key: pattern
    ("file", {"action": "write**,content:hello world", "path": "foo.txt"}, 
     {"action": "write", "content": "hello world", "path": "foo.txt"}),
    # Garbled path and line numbers
    ("file", {"action": "write", "path": "bar.json**,start_line:10", "end_line": 20}, 
     {"action": "write", "path": "bar.json", "start_line": 10, "end_line": 20}),
    # Garbled action
    ("file", {"action": "WRITE_THIS**,path:baz.txt"}, 
     {"action": "write", "path": "baz.txt"}),
    # Mixed garble and clean
    ("file", {"action": "insert**,content:line1", "path": "test.py**,start_line:5"}, 
     {"action": "insert", "content": "line1", "path": "test.py", "start_line": 5}),
    # Invalid integer for start_line should remain string
    ("file", {"action": "write**,start_line:abc"}, 
     {"action": "write", "start_line": "abc"}),
])
def test_sanitize_tool_args(func_name, args, expected):
    assert _sanitize_tool_args(func_name, args, logger) == expected

@pytest.mark.parametrize("func_name, raw_args, expected", [
    # Valid JSON should be parsed
    ("file", '{"action": "read", "path": "test.txt"}', {"action": "read", "path": "test.txt"}),
    # Garbled file action: "read,path:..."
    ("file", 'read,path:test.txt', {"action": "read", "path": "test.txt"}),
    # Garbled file action with content
    ("file", 'write,path:foo.txt,content:hello world', {"action": "write", "path": "foo.txt", "content": "hello world"}),
    # Salvage exec_command
    ("exec_command", 'command:ls -la', {"command": "ls -la"}),
    ("exec_command", '{"command": "pwd"}', {"command": "pwd"}),
    # Completely unsalvageable
    ("file", 'totally random string', None),
    ("exec_command", 'nothing here', None),
])
def test_salvage_tool_args(func_name, raw_args, expected):
    assert _salvage_tool_args(func_name, raw_args, logger) == expected
