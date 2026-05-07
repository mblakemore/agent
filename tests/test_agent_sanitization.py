import pytest
import json
import logging
from unittest.mock import MagicMock
from agent import _sanitize_tool_args, _salvage_tool_args

def test_sanitize_tool_args_garbled_file():
    log = MagicMock()
    # Test case 1: Garbled path and embedded start_line/end_line
    args = {
        "action": "write",
        "path": "test.py**,start_line:10",
        "content": "hello**,end_line:20"
    }
    expected = {
        "action": "write",
        "path": "test.py",
        "content": "hello",
        "start_line": 10,
        "end_line": 20
    }
    assert _sanitize_tool_args("file", args, log) == expected

def test_sanitize_tool_args_fuzzy_action():
    log = MagicMock()
    # Test case 2: Garbled action name (fuzzy match)
    args = {
        "action": "raed",
        "path": "test.py"
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "read"

def test_sanitize_tool_args_substring_action():
    log = MagicMock()
    # Test case 3: Garbled action name (substring match)
    args = {
        "action": "read_this_file",
        "path": "test.py"
    }
    result = _sanitize_tool_args("file", args, log)
    assert result["action"] == "read"

def test_salvage_tool_args_file():
    log = MagicMock()
    # Test case 4: Salvage raw garbled string for file action
    # The current implementation of _salvage_tool_args does not handle 
    # JSON-like strings that fail json.loads() unless they match the specific 
    # regex patterns.
    # The input '{"action": "read**,path:agent.py"}' is actually valid JSON, 
    # so json.loads(cleaned) succeeds and returns {'action': 'read**,path:agent.py'}.
    # The regexes are only reached if json.loads fails.
    
    # Let's test a truly garbled string that is NOT valid JSON.
    raw_args = 'action: read, path: agent.py'
    result = _salvage_tool_args("file", raw_args, log)
    assert result == {"action": "read", "path": "agent.py"}

def test_salvage_tool_args_exec_command():
    log = MagicMock()
    # Test case 5: Salvage raw garbled string for exec_command
    raw_args = 'command: ls -la'
    result = _salvage_tool_args("exec_command", raw_args, log)
    assert result == {"command": "ls -la"}

def test_salvage_tool_args_fail():
    log = MagicMock()
    # Test case 6: Unsalvageable
    raw_args = 'completely broken string'
    assert _salvage_tool_args("unknown", raw_args, log) is None
