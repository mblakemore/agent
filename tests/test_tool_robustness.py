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


# ── json-repair before the heuristic salvage (2026-11-03) ───────────────────

from types import SimpleNamespace

from agent import _repair_tool_args
import agent as _agent


class _RecLog(MockLog):
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


def test_repair_truncated_args_keeps_the_partial_value():
    assert _repair_tool_args("exec_command", '{"command": "ls -l', MockLog()) == {"command": "ls -l"}


def test_repair_trailing_comma_and_single_quotes():
    assert _repair_tool_args("file", "{'path': 'a.py', 'action': 'read',}", MockLog()) == {
        "action": "read", "path": "a.py"}
    assert _repair_tool_args("other", '{"action": "read", "path": "test.txt", ', MockLog()) == {
        "action": "read", "path": "test.txt"}


def test_repair_logs_a_warning_with_the_head_of_the_raw_args():
    log = _RecLog()
    _repair_tool_args("exec_command", '{"command": "ls -l', log)
    assert any("Repaired malformed exec_command args" in w and '{"command": "ls -l' in w for w in log.warnings)


def test_token_leakage_still_reaches_the_salvage(monkeypatch):
    # The repairer must not shadow the heuristic: these shapes are not JSON at all.
    reached = []
    real = _agent._salvage_tool_args

    def spy(func_name, raw, log):
        reached.append(raw)
        return real(func_name, raw, log)
    monkeypatch.setattr(_agent, "_salvage_tool_args", spy)
    assert _repair_tool_args("file", "write,path:test.txt,content:hello", MockLog()) == {
        "action": "write", "path": "test.txt", "content": "hello"}
    assert _repair_tool_args("exec_command", "command: ls -la", MockLog()) == {"command": "ls -la"}
    assert len(reached) == 2


def test_garbage_returns_none_without_raising():
    assert _repair_tool_args("unknown", "completely broken text", MockLog()) is None
    assert _repair_tool_args("unknown", "", MockLog()) is None
    assert _repair_tool_args("file", None, MockLog()) is None


def test_a_repairer_result_that_is_not_a_dict_falls_through(monkeypatch):
    monkeypatch.setattr(_agent, "_json_repair", SimpleNamespace(repair_json=lambda raw, return_objects: [1, 2]))
    assert _repair_tool_args("exec_command", "command: ls -la", MockLog()) == {"command": "ls -la"}
    monkeypatch.setattr(_agent, "_json_repair", SimpleNamespace(repair_json=lambda raw, return_objects: 1 / 0))
    assert _repair_tool_args("exec_command", "command: ls -la", MockLog()) == {"command": "ls -la"}


def test_without_the_package_the_behaviour_is_the_old_one(monkeypatch):
    monkeypatch.setattr(_agent, "_json_repair", None)
    assert _repair_tool_args("other", '{"action": "read", "path": "test.txt", ', MockLog()) is None
    assert _repair_tool_args("exec_command", "command: ls -la", MockLog()) == {"command": "ls -la"}
