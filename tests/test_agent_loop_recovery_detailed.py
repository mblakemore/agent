import pytest
from unittest.mock import MagicMock, patch
import agent
import json

def test_handle_tool_error_loop_hard_bail():
    """Test the 'Hard bail' logic when consecutive errors exceed 2 * _REPEAT_THRESHOLD."""
    log = MagicMock()
    conversation_history = []
    recent_tool_errors = [("test_tool", "Error 1")]
    emit_fn = MagicMock()
    map_fn = {}
    
    # Setup params
    func_name = "test_tool"
    func_args = {"path": "test.py"}
    result_str = "Error: some failure"
    consecutive = 6
    repeat_threshold = 3
    turn = 1
    
    # Exercise the refactored helper
    should_break = agent._handle_tool_error_loop(
        func_name=func_name,
        func_args=func_args,
        result_str=result_str,
        consecutive=consecutive,
        repeat_threshold=repeat_threshold,
        log=log,
        conversation_history=conversation_history,
        recent_tool_errors=recent_tool_errors,
        turn=turn,
        emit_fn=emit_fn,
        map_fn=map_fn
    )
    
    assert should_break is True
    assert len(conversation_history) == 1
    assert "SKIPPED" in conversation_history[0]["content"]
    assert len(recent_tool_errors) == 0
    emit_fn.assert_any_call("on_tool_skip", "test_tool", 6)

def test_handle_tool_error_loop_forced_think():
    """Test the forced think logic when consecutive errors reach _REPEAT_THRESHOLD."""
    log = MagicMock()
    conversation_history = []
    recent_tool_errors = []
    emit_fn = MagicMock()
    map_fn = {"think": lambda prompt: "Thought result"}
    
    func_name = "test_tool"
    func_args = {"path": "test.py"}
    result_str = "Error: some failure"
    consecutive = 3
    repeat_threshold = 3
    turn = 1
    
    should_break = agent._handle_tool_error_loop(
        func_name=func_name,
        func_args=func_args,
        result_str=result_str,
        consecutive=consecutive,
        repeat_threshold=repeat_threshold,
        log=log,
        conversation_history=conversation_history,
        recent_tool_errors=recent_tool_errors,
        turn=turn,
        emit_fn=emit_fn,
        map_fn=map_fn
    )
    
    assert should_break is False
    assert len(conversation_history) == 2
    assert conversation_history[0]["tool_calls"][0]["function"]["name"] == "think"
    assert conversation_history[1]["content"] == "Thought result"
    emit_fn.assert_any_call("on_forced_think", "test_tool", 3)

def test_handle_tool_error_loop_success():
    """Test that a successful call (no 'Error' prefix) resets tracking and does not break."""
    log = MagicMock()
    conversation_history = []
    recent_tool_errors = [("test_tool", "Error 1")]
    emit_fn = MagicMock()
    map_fn = {}
    
    func_name = "test_tool"
    func_args = {"path": "test.py"}
    result_str = "Success: operation completed"
    consecutive = 3
    repeat_threshold = 3
    turn = 1
    
    should_break = agent._handle_tool_error_loop(
        func_name=func_name,
        func_args=func_args,
        result_str=result_str,
        consecutive=consecutive,
        repeat_threshold=repeat_threshold,
        log=log,
        conversation_history=conversation_history,
        recent_tool_errors=recent_tool_errors,
        turn=turn,
        emit_fn=emit_fn,
        map_fn=map_fn
    )
    
    assert should_break is False
    assert len(recent_tool_errors) == 0
    emit_fn.assert_called_with("on_tool_result", "test_tool", func_args, result_str, False)
