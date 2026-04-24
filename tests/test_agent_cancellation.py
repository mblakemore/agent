import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from unittest.mock import MagicMock, patch
from cancel import CancelledError
from agent import run_agent_single


def test_cancelled_during_tool_execution():
    """Test that CancelledError during tool execution triggers handler at lines 2792-2799."""
    history = [{"role": "user", "content": "test"}]
    summary_state = {"text": "", "up_to": 0}
    
    # SSE stream with one tool call
    tc = {"index": 0, "id": "t1", "type": "function",
          "function": {"name": "exec_command", "arguments": '{"command": "echo test"}'}}
    body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    resp = MagicMock()
    resp.iter_lines.return_value = [
        b"data: " + json.dumps(body).encode(),
        b"data: [DONE]",
    ]
    
    async_summarizer = MagicMock()
    
    # check_cancelled called: 2x during SSE iteration, 1x before tool dispatch
    # Raise on 3rd call to hit tool_execution cancel path
    with patch("agent._llm_request", return_value=resp), \
         patch("agent._emit") as mock_emit, \
         patch("agent._save_checkpoint"), \
         patch("agent.check_cancelled", side_effect=[None, None, CancelledError()]):
        result = run_agent_single(
            history, summary_state, [], MagicMock(),
            async_summarizer=async_summarizer,
        )
    
    assert result == "cancelled"
    # harvest called at turn start (line ~1806) AND handler (2797) → use assert_any_call
    async_summarizer.harvest.assert_any_call(summary_state)
    # drain called only in handler → assert_called_once
    async_summarizer.drain.assert_called_once()
    mock_emit.assert_any_call("on_cancelled", "tool_execution")
