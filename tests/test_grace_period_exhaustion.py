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

def test_grace_period_exhaustion():
    """
    Triggers the grace period exhaustion stopping condition (Line 2684).
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    initial_files = {}
    mock_log = MagicMock()
    
    # We need to override these to prevent the agent from stopping 
    # due to nudge budgets before the grace period is exhausted.
    with patch("agent._NUDGE_ENABLED", True), \
         patch("agent._MAX_TEXT_ONLY", 20), \
         patch("agent._MAX_TOTAL_NUDGES", 20), \
         patch("agent._llm_request") as mock_llm, \
         patch("agent._emit") as mock_emit:
        
        # Step 1: git push to set _cycle_persisted = True
        tc_push = [{"id": "t0", "type": "function", "function": {"name": "exec_command", "arguments": json.dumps({"command": "git push origin cicd/branch"})}}]
        
        # Step 2: a series of text responses that are NOT completion signals.
        # We provide plenty of responses.
        responses = [make_stream(tool_calls=tc_push)]
        responses += [make_stream(content=f"I am still working on the tracking part... (Response {i})") for i in range(50)]
        
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
            
        print(f"\nDEBUG: run_agent_single returned: {result}")
        
        # Check if the log contains the "grace period exhausted" message.
        found = any("grace period exhausted" in call[0][0] for call in mock_log.info.call_args_list)
        assert found, f"Grace period exhausted info not found in {mock_log.info.call_args_list}"
        assert result == "done"
