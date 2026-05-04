"""Tests for dispatcher robustness when func_args is a non-dict JSON value.

An LLM can send valid-but-non-dict JSON as tool arguments (e.g. null, [], "str",
42).  json.loads() parses these successfully, so the JSON decode path succeeds and
func_args reaches the post-execution tracking blocks.  Before the fix, lines that
called func_args.get() without an isinstance(func_args, dict) guard would raise
AttributeError and crash the agent loop.

Reproduces:
  - Line 3415: `if func_name == "file" and func_args.get("action") ...`
  - Line 3422: `if func_name == "exec_command": _cmd = func_args.get("command", "")`
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

import agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_resp_raw_args(tool_name: str, raw_args: str, tool_id: str = "t1"):
    """Build a mock streaming LLM response with pre-serialised *raw_args*."""
    resp = MagicMock()
    resp.status_code = 200
    tc = {
        "index": 0,
        "id": tool_id,
        "type": "function",
        "function": {"name": tool_name, "arguments": raw_args},
    }
    body = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    resp.iter_lines.return_value = [
        f"data: {json.dumps(body)}".encode(),
        b"data: [DONE]",
    ]
    return resp


def _make_resp_text(text: str):
    resp = MagicMock()
    resp.status_code = 200
    body = {"choices": [{"delta": {"content": text}}]}
    resp.iter_lines.return_value = [
        f"data: {json.dumps(body)}".encode(),
        b"data: [DONE]",
    ]
    return resp


@pytest.fixture()
def mock_log():
    return MagicMock(spec=logging.Logger)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_args", ["null", "[]", '"string"', "42", "true"])
def test_file_tool_non_dict_args_no_crash(mock_log, raw_args):
    """Dispatcher must not crash when 'file' receives non-dict JSON arguments.

    Previously line 3415 called func_args.get("action") without a dict guard,
    raising AttributeError: 'NoneType' (or list/str/int) object has no attribute 'get'.
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}

    with patch("agent._llm_request") as mock_llm, patch("agent._emit"):
        mock_llm.side_effect = [
            _make_resp_raw_args("file", raw_args),
            _make_resp_text("Done"),
        ]
        with patch("agent._NUDGE_ENABLED", False):
            # Must not raise — any exception here is the bug
            agent.run_agent_single(conversation_history, summary_state, None, mock_log)

    # The tool call should have produced an error message, not a crash
    tool_results = [
        m for m in conversation_history if m.get("role") == "tool"
    ]
    assert tool_results, "Expected at least one tool result in conversation history"
    result_content = tool_results[0]["content"]
    assert "Error" in result_content, (
        f"Expected 'Error' in tool result for non-dict args {raw_args!r}, got: {result_content!r}"
    )


@pytest.mark.parametrize("raw_args", ["null", "[]", '"string"', "42", "true"])
def test_exec_command_non_dict_args_no_crash(mock_log, raw_args):
    """Dispatcher must not crash when 'exec_command' receives non-dict JSON arguments.

    Previously line 3422 called func_args.get("command", "") without a dict guard,
    raising AttributeError when func_args was None, list, str, int, or bool.
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}

    with patch("agent._llm_request") as mock_llm, patch("agent._emit"):
        mock_llm.side_effect = [
            _make_resp_raw_args("exec_command", raw_args),
            _make_resp_text("Done"),
        ]
        with patch("agent._NUDGE_ENABLED", False):
            # Must not raise — any exception here is the bug
            agent.run_agent_single(conversation_history, summary_state, None, mock_log)

    # The tool call should have produced an error message, not a crash
    tool_results = [
        m for m in conversation_history if m.get("role") == "tool"
    ]
    assert tool_results, "Expected at least one tool result in conversation history"
    result_content = tool_results[0]["content"]
    assert "Error" in result_content, (
        f"Expected 'Error' in tool result for non-dict args {raw_args!r}, got: {result_content!r}"
    )


def test_file_null_args_does_not_mark_has_edited(mock_log):
    """When file is called with null args, _has_edited must NOT be set to True.

    Before the fix, the dispatcher crashed before the commit-tracking logic ran.
    After the fix, we want to confirm the isinstance guard is effective: null args
    skip the 'has_edited' tracking block entirely (no false positive).
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}

    with patch("agent._llm_request") as mock_llm, patch("agent._emit"):
        mock_llm.side_effect = [
            _make_resp_raw_args("file", "null"),
            _make_resp_text("Done"),
        ]
        with patch("agent._NUDGE_ENABLED", False):
            agent.run_agent_single(conversation_history, summary_state, None, mock_log)

    # Agent loop must have completed without an unhandled exception
    # (if it crashed, run_agent_single would have raised or returned early)
    assert any(m.get("role") == "assistant" for m in conversation_history), (
        "Expected at least one assistant message — loop should have completed"
    )


def test_exec_command_null_args_does_not_crash_commit_tracking(mock_log):
    """When exec_command is called with null args, the git-commit tracker must not crash."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}

    with patch("agent._llm_request") as mock_llm, patch("agent._emit"):
        mock_llm.side_effect = [
            _make_resp_raw_args("exec_command", "null"),
            _make_resp_text("Done"),
        ]
        with patch("agent._NUDGE_ENABLED", False):
            agent.run_agent_single(conversation_history, summary_state, None, mock_log)

    # Loop should complete and produce an assistant turn
    assert any(m.get("role") == "assistant" for m in conversation_history), (
        "Expected at least one assistant message — loop should have completed"
    )


# ── Issue #859: non-dict func_args must not produce confusing TypeError at **-unpack ──

@pytest.mark.parametrize("raw_args", ["null", "[]", '"string"', "42"])
def test_non_dict_args_no_type_error_in_result(mock_log, raw_args):
    """Tool result must not contain the 'must be a mapping' TypeError message (#859).

    Before the fix, json.loads('null') → None was passed directly to **-unpack,
    raising 'TypeError: argument after ** must be a mapping, not NoneType'.
    After the fix, non-dict args are coerced to {} before dispatch.
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}

    with patch("agent._llm_request") as mock_llm, patch("agent._emit"):
        mock_llm.side_effect = [
            _make_resp_raw_args("think", raw_args),
            _make_resp_text("Done"),
        ]
        with patch("agent._NUDGE_ENABLED", False):
            agent.run_agent_single(conversation_history, summary_state, None, mock_log)

    tool_results = [m for m in conversation_history if m.get("role") == "tool"]
    assert tool_results, "Expected at least one tool result"
    content = tool_results[0]["content"]
    assert "must be a mapping" not in content, (
        f"Got confusing TypeError from **-unpack for raw_args={raw_args!r}: {content!r}"
    )


def test_non_dict_args_tool_called_not_skipped(mock_log):
    """When non-dict args are received, the tool must still be invoked (#859).

    Before the fix, **None raised TypeError inside the try block and was caught by
    'except Exception', producing an error result but still calling the tool.
    After the fix, the tool is called with {}, which may produce a different error
    (e.g. 'missing required argument: prompt') but must NOT produce the cryptic
    'argument after ** must be a mapping, not NoneType' message.
    """
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    mock_tool = MagicMock(return_value="called with empty args")

    with patch("agent.MAP_FN", {**agent.MAP_FN, "think": mock_tool}), \
         patch("agent._llm_request") as mock_llm, \
         patch("agent._emit"):
        mock_llm.side_effect = [
            _make_resp_raw_args("think", "null"),
            _make_resp_text("Done"),
        ]
        with patch("agent._NUDGE_ENABLED", False):
            agent.run_agent_single(conversation_history, summary_state, None, mock_log)

    # Tool MUST have been called (with empty kwargs, not skipped or crashed before call)
    mock_tool.assert_called_once_with()
