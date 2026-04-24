import pytest
from unittest.mock import MagicMock, patch
import agent
import json

def make_stream(content="", tool_calls=None):
    """Helper to create a stream of deltas."""
    chunks = []
    if content:
        chunks.append({"choices": [{"delta": {"content": content}}]})
    if tool_calls:
        for i, tc in enumerate(tool_calls):
            chunks.append({"choices": [{"delta": {"tool_calls": [
                {
                    "index": i,
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"]
                    }
                }
            ]}}]})
    return chunks

def test_text_loop_detection():
    """
    Triggers the text loop detection stopping condition (Line 2557).
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    initial_files = {}
    mock_log = MagicMock()
    
    with patch("agent._llm_request") as mock_llm, \
         patch("agent._emit") as mock_emit:
        
        repeated_text = "I am stuck in a loop."
        tc = [{"id": "t1", "type": "function", "function": {"name": "exec_command", "arguments": json.dumps({"command": "ls"})}}]
        
        responses = [
            make_stream(content=repeated_text, tool_calls=tc),
            make_stream(content=repeated_text, tool_calls=tc),
            make_stream(content=repeated_text, tool_calls=tc),
        ]
        mock_llm.side_effect = responses
        
        with patch.dict("agent.MAP_FN", {"exec_command": lambda **kwargs: "Success"}):
            result = agent.run_agent_single(
                conversation_history=conversation_history,
                summary_state=summary_state,
                initial_files=initial_files,
                log=mock_log,
                start_turn=0
            )
            
        found = any("Text loop detected" in call[0][0] for call in mock_log.warning.call_args_list)
        assert found, f"Text loop warning not found in {mock_log.warning.call_args_list}"
        assert result == "done"

def test_hard_stop_drift():
    """
    Triggers the hard cap on post-persist drift (Line 2571).
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    initial_files = {}
    mock_log = MagicMock()
    
    with patch("agent._llm_request") as mock_llm:
        # Turn 0: git push
        tc_push = [{"id": "t0", "type": "function", "function": {"name": "exec_command", "arguments": json.dumps({"command": "git push origin cicd/branch"})}}]
        responses = [make_stream(tool_calls=tc_push)]
        
        # Then 16 more responses.
        tc_ls = [{"id": "t_ls", "type": "function", "function": {"name": "exec_command", "arguments": json.dumps({"command": "ls"})}}]
        responses += [make_stream(tool_calls=tc_ls) for _ in range(20)]
        
        mock_llm.side_effect = responses
        
        def side_effect_exec(**kwargs):
            cmd = kwargs.get("command", "")
            if "git push" in cmd:
                return "exit=0"
            return "success"
            
        with patch.dict("agent.MAP_FN", {"exec_command": side_effect_exec}):
            result = agent.run_agent_single(
                conversation_history=conversation_history,
                summary_state=summary_state,
                initial_files=initial_files,
                log=mock_log,
                start_turn=0
            )
            
        found = any("hard cap reached" in call[0][0] for call in mock_log.info.call_args_list)
        assert found, f"Hard cap info not found in {mock_log.info.call_args_list}"
        assert result == "done"
