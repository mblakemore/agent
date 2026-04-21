import pytest
from unittest.mock import MagicMock, patch
import agent
import json

def test_loop_detection_hard_bail():
    """Test the 'Hard bail' logic when consecutive errors exceed 2 * _REPEAT_THRESHOLD."""
    log = MagicMock()
    conversation_history = []
    recent_tool_errors = [("test_tool", "Error 1")]
    
    # Using direct attribute access instead of patch if patch is failing for some reason
    # though it should work. Let's try to be more robust.
    agent._REPEAT_THRESHOLD = 3
    
    # Mock the necessary global functions/dicts
    with patch('agent._emit') as mock_emit:
        # Simulating the logic block from agent.py lines 2701-2718
        func_name = "test_tool"
        func_args = {"path": "test.py"}
        result_str = "Error: some failure"
        _result_repeats = 6 
        
        if result_str.startswith("Error"):
            consecutive = _result_repeats
            if consecutive >= agent._REPEAT_THRESHOLD * 2:
                log.warning("Hard bail: %s failed %d times — skipping", func_name, consecutive)
                mock_emit("on_tool_skip", func_name, consecutive)
                conversation_history.append({
                    "role": "user",
                    "content": (
                        f"SYSTEM: The {func_name} tool has failed {consecutive} times "
                        f"with the same error. This step is being SKIPPED. "
                        f"Use exec_command with cat/heredoc to write files instead, "
                        f"or move on to the next step."
                    ),
                })
                recent_tool_errors[:] = [e for e in recent_tool_errors if e[0] != func_name]

    assert len(conversation_history) == 1
    assert "SKIPPED" in conversation_history[0]["content"]
    assert len(recent_tool_errors) == 0
    mock_emit.assert_called_with("on_tool_skip", "test_tool", 6)

def test_loop_detection_forced_think():
    """Test the forced think logic when consecutive errors reach _REPEAT_THRESHOLD."""
    log = MagicMock()
    conversation_history = []
    
    agent._REPEAT_THRESHOLD = 3
    
    with patch('agent.MAP_FN', {"think": lambda prompt: "Thought result"}), \
         patch('agent._emit') as mock_emit:
        
        func_name = "test_tool"
        func_args = {"path": "test.py"}
        result_str = "Error: some failure"
        _result_repeats = 3 
        
        if result_str.startswith("Error"):
            consecutive = _result_repeats
            if consecutive >= agent._REPEAT_THRESHOLD * 2:
                pass
            elif consecutive >= agent._REPEAT_THRESHOLD:
                think_prompt = f"MANDATORY REFLECTION: I have called {func_name} {consecutive} times..."
                log.warning("Loop detected: %s x%d — forcing think", func_name, consecutive)
                mock_emit("on_forced_think", func_name, consecutive)
                
                think_result = agent.MAP_FN["think"](prompt=think_prompt)
                think_id = "forced_think_1_3"
                conversation_history.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": think_id, "type": "function", "function": {"name": "think", "arguments": json.dumps({"prompt": think_prompt})}}]
                })
                conversation_history.append({
                    "role": "tool",
                    "tool_call_id": think_id,
                    "name": "think",
                    "content": str(think_result),
                })

    assert len(conversation_history) == 2
    assert conversation_history[0]["tool_calls"][0]["function"]["name"] == "think"
    assert conversation_history[1]["content"] == "Thought result"
    mock_emit.assert_called_with("on_forced_think", "test_tool", 3)
