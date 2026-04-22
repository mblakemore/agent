"""Tests for CancelledError handler async_summarizer drain/harvest (lines 2795-2797)."""
import json
import logging
from unittest.mock import MagicMock, patch
from agent import run_agent_single, CancelledError

log = logging.getLogger("test_cancellation")


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_agent_cancellation_coverage(mock_config, mock_llm, mock_emit):
    """Covers lines 2795-2797: drain+harvest called when async_summarizer set and tool raises CancelledError."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "presence_penalty": 0.0},
        "context": {"max_tokens": 4096, "ctx_size": 32768},
    }.get(k)

    # SSE format from TESTING NOTES in agent.py line ~1904
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    tc = {"index": 0, "id": "t1", "type": "function",
          "function": {"name": "cancel_tool", "arguments": "{}"}}
    body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    mock_resp.iter_lines.return_value = [
        f"data: {json.dumps(body)}".encode(),
        b"data: [DONE]",
    ]
    mock_llm.return_value = mock_resp

    # CRITICAL: pass async_summarizer as kwarg — sets _async_summarizer at line 1762.
    # Patching agent.AsyncSummarizer class does NOT work (run_agent_single uses the kwarg directly).
    async_summarizer = MagicMock()
    summary_state = {"text": "", "up_to": 0}

    # CRITICAL: use MagicMock(side_effect=CancelledError) NOT async def — async def returns
    # a coroutine object (no exception raised); MagicMock.side_effect raises immediately.
    with patch.dict("agent.MAP_FN", {"cancel_tool": MagicMock(side_effect=CancelledError)}), \
         patch("agent._save_checkpoint"):
        result = run_agent_single(
            [{"role": "user", "content": "Hello"}],
            summary_state,
            [],
            log,
            async_summarizer=async_summarizer,
        )

    assert result == "cancelled"
    # Lines 2795-2797: drain+harvest inside `if _async_summarizer:` in CancelledError handler
    async_summarizer.drain.assert_called_once()
    async_summarizer.harvest.assert_called_with(summary_state)
