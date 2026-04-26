import pytest
import requests
from unittest.mock import patch, MagicMock
from agent import (
    _format_for_summary, 
    _summary_request, 
    _condense_summary, 
    _build_summary_prompt,
    _SUMMARY_MAX_CHARS
)

def test_format_for_summary_basic():
    messages = [
        {"role": "user", "content": "Hello world"},
        {"role": "assistant", "content": "Hi there!"}
    ]
    result = _format_for_summary(messages)
    assert "USER: Hello world" in result
    assert "ASSISTANT: Hi there!" in result

def test_format_for_summary_tool_result():
    messages = [
        {"role": "tool", "name": "ls", "content": "file1.txt\nfile2.txt"}
    ]
    result = _format_for_summary(messages)
    assert "TOOL RESULT (ls): file1.txt\nfile2.txt" in result

def test_format_for_summary_tool_error():
    # Error messages should have a higher limit (800)
    long_error = "Error: " + "X" * 700
    messages = [
        {"role": "tool", "name": "ls", "content": long_error}
    ]
    result = _format_for_summary(messages)
    assert len(long_error) <= len(result) # Should not be truncated at 500
    
    too_long_error = "Error: " + "X" * 900
    result = _format_for_summary([{"role": "tool", "name": "ls", "content": too_long_error}])
    assert "..." in result
    assert len(result) < 900

def test_format_for_summary_assistant_tool_calls():
    messages = [
        {
            "role": "assistant", 
            "content": "I will write the file", 
            "tool_calls": [
                {"function": {"name": "file", "arguments": '{"action": "write", "path": "test.txt", "content": "hello"}'}}
            ]
        }
    ]
    result = _format_for_summary(messages)
    assert "ASSISTANT: I will write the file" in result
    assert "ASSISTANT called file(action=write, path=test.txt)" in result

def test_format_for_summary_truncation():
    messages = [
        {"role": "user", "content": "A" * 1000}
    ]
    result = _format_for_summary(messages)
    assert "..." in result
    assert len(result) < 1000

@patch("agent._summary_backend.complete")
def test_summary_request_success(mock_complete):
    mock_complete.return_value = "This is a summary"
    
    result = _summary_request("prompt")
    assert result == "This is a summary"
    mock_complete.assert_called_once_with(prompt="prompt")

@patch("agent._summary_backend.complete")
def test_summary_request_failure(mock_complete):
    mock_complete.side_effect = requests.HTTPError("500 Server Error")
    
    with pytest.raises(requests.HTTPError):
        _summary_request("prompt")
    mock_complete.assert_called_once_with(prompt="prompt")
def test_condense_summary_no_op():
    short_text = "Short summary"
    result = _condense_summary(short_text)
    assert result == short_text

@patch("agent._summary_request")
def test_condense_summary_success(mock_req):
    long_text = "A" * (_SUMMARY_MAX_CHARS + 100)
    mock_req.return_value = "Condensed summary"
    result = _condense_summary(long_text)
    assert result == "Condensed summary"

@patch("agent._summary_request")
def test_condense_summary_still_too_long(mock_req):
    long_text = "A" * (_SUMMARY_MAX_CHARS + 100)
    mock_req.return_value = "B" * (_SUMMARY_MAX_CHARS + 100)
    result = _condense_summary(long_text)
    assert "[...truncated]" in result
    assert len(result) <= _SUMMARY_MAX_CHARS + 20

@patch("agent._summary_request")
def test_condense_summary_exception(mock_req):
    long_text = "A" * (_SUMMARY_MAX_CHARS + 100)
    mock_req.side_effect = Exception("API error")
    result = _condense_summary(long_text)
    assert "[...truncated]" in result
    assert len(result) <= _SUMMARY_MAX_CHARS + 20

def test_build_summary_prompt_basic():
    old_summary = "Old"
    new_messages = [{"role": "user", "content": "New"}]
    prompt = _build_summary_prompt(old_summary, new_messages)
    assert "previous summary" in prompt
    assert "New" in prompt
    assert "GOAL:" in prompt

def test_build_summary_prompt_no_old():
    new_messages = [{"role": "user", "content": "New"}]
    prompt = _build_summary_prompt(None, new_messages)
    assert "previous summary" not in prompt
    assert "New" in prompt

@patch("agent._cicd_worktree_path", "path/to/worktree")
@patch("agent._cicd_issue_number", 205)
def test_build_summary_prompt_cicd_facts():
    prompt = _build_summary_prompt(None, [])
    assert "Worktree path: path/to/worktree" in prompt
    assert "Issue: #205" in prompt
    assert "GROUND TRUTH" in prompt
