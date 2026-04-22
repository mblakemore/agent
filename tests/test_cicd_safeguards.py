import pytest
import re
import os
from unittest.mock import patch, MagicMock, mock_open

# We will import the function AFTER refactoring it in agent.py
# For now, this is a placeholder for the tests we WILL run.

def test_validate_tool_call_merge_blocked():
    from agent import _validate_tool_call
    mock_log = MagicMock()
    # Case: gh pr merge without issue view
    blocked, msg = _validate_tool_call("exec_command", {"command": "gh pr merge"}, False, mock_log)
    assert blocked is True
    assert "PRE-MERGE CHECK required" in msg

def test_validate_tool_call_merge_allowed():
    from agent import _validate_tool_call
    mock_log = MagicMock()
    # Case: gh pr merge WITH issue view
    blocked, msg = _validate_tool_call("exec_command", {"command": "gh pr merge"}, True, mock_log)
    assert blocked is False
    assert msg is None

def test_validate_tool_call_pr_create_blocked_no_closes():
    from agent import _validate_tool_call
    mock_log = MagicMock()
    # Case: gh pr create without Closes #N
    blocked, msg = _validate_tool_call("exec_command", {"command": "gh pr create --body 'Fixes bug'"}, True, mock_log)
    assert blocked is True
    assert "CICD gh pr create blocked" in msg

def test_validate_tool_call_pr_create_allowed_with_closes():
    from agent import _validate_tool_call
    mock_log = MagicMock()
    # Case: gh pr create with Closes #123
    blocked, msg = _validate_tool_call("exec_command", {"command": "gh pr create --body 'Closes #123'"}, True, mock_log)
    assert blocked is False
    assert msg is None

def test_validate_tool_call_pr_create_allowed_with_cat_file(tmp_path):
    from agent import _validate_tool_call
    mock_log = MagicMock()
    # Create the /tmp/pr-body.md file
    pr_body_file = tmp_path / "pr-body.md"
    pr_body_file.write_text("This PR Closes #456")
    
    # We need to mock open() to return our tmp file since the code hardcodes /tmp/pr-body.md
    with patch("builtins.open", mock_open(read_data="This PR Closes #456")):
        blocked, msg = _validate_tool_call("exec_command", {"command": "gh pr create --body \"$(cat /tmp/pr-body.md)\""}, True, mock_log)
        assert blocked is False
        assert msg is None

def test_validate_tool_call_pr_create_blocked_with_cat_file_no_closes():
    from agent import _validate_tool_call
    mock_log = MagicMock()
    with patch("builtins.open", mock_open(read_data="This PR has no issue number")):
        blocked, msg = _validate_tool_call("exec_command", {"command": "gh pr create --body \"$(cat /tmp/pr-body.md)\""}, True, mock_log)
        assert blocked is True
        assert "CICD gh pr create blocked" in msg

def test_validate_tool_call_other_tool():
    from agent import _validate_tool_call
    mock_log = MagicMock()
    # Case: Other tool should never be blocked by these guards
    blocked, msg = _validate_tool_call("file", {"action": "read", "path": "test.txt"}, False, mock_log)
    assert blocked is False
    assert msg is None
