import pytest
from unittest.mock import MagicMock, patch
from agent import run_agent_single

_REPEAT_THRESHOLD = 3 

def test_tool_loop_forced_think():
    """Verify that forced think is injected after _REPEAT_THRESHOLD failures."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()

    # Mock LLM to return tool call, then eventually stop
    with patch('agent._llm_request') as mock_llm:
        mock_tool_resp = MagicMock()
        mock_tool_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "1", "type": "function", "function": {"name": "fail_tool", "arguments": "{\"arg\": 1}"}}]}}]}',
            b'data: [DONE]'
        ]
        mock_tool_resp.status_code = 200

        mock_done_resp = MagicMock()
        mock_done_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"content": "I am done"}}]}',
            b'data: [DONE]'
        ]
        mock_done_resp.status_code = 200

        def llm_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # We need to provide enough tool calls to hit _REPEAT_THRESHOLD
            # and then one more to actually trigger the "forced think" logic 
            # (which happens AFTER the tool result is processed)
            if call_count > _REPEAT_THRESHOLD + 2:
                return mock_done_resp
            return mock_tool_resp

        call_count = 0
        mock_llm.side_effect = llm_side_effect

        with patch('agent.MAP_FN') as mock_map:
            # Mock the tool to return an "Error" string
            mock_fail_tool = MagicMock(return_value="Error: tool failed")

            # Important: MAP_FN is used like a dict
            mock_map.__getitem__.side_effect = lambda k: mock_fail_tool if k == "fail_tool" else MagicMock()
            mock_map.__contains__.side_effect = lambda k: True 

            # Also mock 'think' tool since forced think calls it
            mock_think_tool = MagicMock(return_value="I have thought about it.")
            mock_map.__getitem__.side_effect = lambda k: mock_think_tool if k == "think" else (mock_fail_tool if k == "fail_tool" else MagicMock())

            run_agent_single(conversation_history, summary_state, None, log)

            reflection_found = False
            for msg in conversation_history:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        if "MANDATORY REFLECTION" in str(tc.get("function", {}).get("arguments", "")):
                            reflection_found = True
                            break
            assert reflection_found, "Forced think prompt should have been injected"

def test_tool_loop_hard_bail():
    """Verify that agent skips the tool after _REPEAT_THRESHOLD * 2 failures."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()
    
    with patch('agent._llm_request') as mock_llm:
        mock_tool_resp = MagicMock()
        mock_tool_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "1", "type": "function", "function": {"name": "fail_tool", "arguments": "{\"arg\": 1}"}}]}}]}',
            b'data: [DONE]'
        ]
        mock_tool_resp.status_code = 200
        
        mock_done_resp = MagicMock()
        mock_done_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"content": "I am done"}}]}',
            b'data: [DONE]'
        ]
        mock_done_resp.status_code = 200

        def llm_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > _REPEAT_THRESHOLD * 2 + 2:
                return mock_done_resp
            return mock_tool_resp

        call_count = 0
        mock_llm.side_effect = llm_side_effect
        
        with patch('agent.MAP_FN') as mock_map:
            mock_fail_tool = MagicMock(return_value="Error: tool failed")
            mock_think_tool = MagicMock(return_value="Thinking...")
            
            mock_map.__getitem__.side_effect = lambda k: mock_think_tool if k == "think" else (mock_fail_tool if k == "fail_tool" else MagicMock())
            mock_map.__contains__.side_effect = lambda k: True
            
            run_agent_single(conversation_history, summary_state, None, log)
            
            bail_found = any("has failed" in str(msg.get("content", "")) and "SKIPPED" in str(msg.get("content", ""))
                             for msg in conversation_history)
            assert bail_found, "Hard bail message should have been injected"

def test_tool_loop_reset_on_success():
    """Verify that a successful tool call resets the loop tracker."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()
    
    with patch('agent._llm_request') as mock_llm:
        mock_fail_resp = MagicMock()
        mock_fail_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "1", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}]}}]}',
            b'data: [DONE]'
        ]
        mock_fail_resp.status_code = 200
        
        mock_success_resp = MagicMock()
        mock_success_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "2", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}]}}]}',
            b'data: [DONE]'
        ]
        mock_success_resp.status_code = 200
        
        mock_stop_resp = MagicMock()
        mock_stop_resp.iter_lines.return_value = [
            b'data: {"choices": [{"delta": {"content": "Stop"}}]}',
            b'data: [DONE]'
        ]
        mock_stop_resp.status_code = 200

        def llm_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < _REPEAT_THRESHOLD:
                return mock_fail_resp
            elif call_count == _REPEAT_THRESHOLD:
                return mock_success_resp
            else:
                return mock_stop_resp

        call_count = 0
        mock_llm.side_effect = llm_side_effect
        
        with patch('agent.MAP_FN') as mock_map:
            def tool_side_effect(args):
                # Access current call_count from outer scope
                if call_count == _REPEAT_THRESHOLD:
                    return "Success"
                return "Error: fail"
            
            mock_tool = MagicMock(side_effect=tool_side_effect)
            mock_map.__getitem__.side_effect = lambda k: mock_tool if k == "tool_x" else MagicMock()
            mock_map.__contains__.side_effect = lambda k: True
            
            run_agent_single(conversation_history, summary_state, None, log)
            
            reflection_found = any("MANDATORY REFLECTION" in str(msg.get("content", "")) 
                                   for msg in conversation_history)
            assert not reflection_found, "Success should have reset the loop tracker"
