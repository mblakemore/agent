"""
Tests for the semantic result-loop false-positive fix (issue #457).

Verifies that:
1. Three consecutive exec_command calls returning empty stdout do NOT
   trigger the semantic result loop warning.
2. Three consecutive exec_command calls returning the same non-empty
   result DO trigger the warning.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from agent import run_agent_single
from tools import MAP_FN


def _make_tool_response(command, extra_tool_calls=None):
    """Build a mock SSE response that fires one exec_command tool call."""
    chunks = [
        'data: {"choices": [{"delta": {"content": ""}}]}',
    ]
    tool_call_payload = {
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"command": command}),
                    }
                }]
            }
        }]
    }
    chunks.append(f'data: {json.dumps(tool_call_payload)}')
    chunks.append('data: [DONE]')

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [line.encode("utf-8") for line in chunks]
    mock_resp.status_code = 200
    mock_resp.close = MagicMock()
    return mock_resp


def _make_text_response(content="Done"):
    chunks = [
        f'data: {{"choices": [{{"delta": {{"content": "{content}"}}}}]}}',
        'data: [DONE]',
    ]
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = [line.encode("utf-8") for line in chunks]
    mock_resp.status_code = 200
    mock_resp.close = MagicMock()
    return mock_resp


def _run_n_identical_tool_calls(monkeypatch, n, tool_result):
    """
    Simulate n consecutive exec_command calls all returning `tool_result`.
    Returns the conversation history after the run, as a joined string.
    """
    mock_exec = MagicMock(return_value=tool_result)
    monkeypatch.setitem(MAP_FN, "exec_command", mock_exec)

    # n tool-call responses followed by a final text response
    llm_responses = [
        _make_tool_response(f"echo 'step {i}' >> out.jsonl") for i in range(n)
    ]
    llm_responses.append(_make_text_response("Done"))

    with patch('agent._llm_request') as mock_llm, \
         patch('agent._check_api_health', return_value=(True, "ok")), \
         patch('agent._setup_logger'), \
         patch('agent._detect_ctx_size', return_value=None):

        mock_llm.side_effect = llm_responses

        history = [{"role": "user", "content": "Append three lines"}]
        mock_log = MagicMock()

        run_agent_single(
            conversation_history=history,
            summary_state={"text": "", "up_to": 0},
            initial_files=[],
            log=mock_log,
        )

    return "".join(str(m) for m in history)


LOOP_WARNING_FRAGMENT = "has returned the same output"


def test_empty_stdout_no_false_positive(monkeypatch):
    """
    Three consecutive exec_command calls that return empty string must NOT
    trigger the semantic result-loop warning.
    """
    history_str = _run_n_identical_tool_calls(monkeypatch, n=3, tool_result="")
    assert LOOP_WARNING_FRAGMENT not in history_str, (
        "Empty-stdout commands should NOT trigger the semantic result-loop warning"
    )


def test_exit_only_stdout_no_false_positive(monkeypatch):
    """
    Three consecutive exec_command calls returning only an exit=0 session line
    (e.g. '[session:abc123] exit=0') must NOT trigger the semantic result-loop
    warning.
    """
    exit_only = "[session:abc123] exit=0"
    history_str = _run_n_identical_tool_calls(monkeypatch, n=3, tool_result=exit_only)
    assert LOOP_WARNING_FRAGMENT not in history_str, (
        "exit=0-only results should NOT trigger the semantic result-loop warning"
    )


def test_nonempty_identical_results_trigger_warning(monkeypatch):
    """
    Three consecutive exec_command calls returning the same non-empty result
    MUST trigger the semantic result-loop warning.
    """
    non_empty = "nothing to commit, working tree clean"
    history_str = _run_n_identical_tool_calls(monkeypatch, n=3, tool_result=non_empty)
    assert LOOP_WARNING_FRAGMENT in history_str, (
        "Three identical non-empty results SHOULD trigger the semantic result-loop warning"
    )
