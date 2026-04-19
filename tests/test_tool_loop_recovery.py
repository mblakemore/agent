import json
from unittest.mock import MagicMock, patch

from agent import run_agent_single

_REPEAT_THRESHOLD = 3


def _stream_chunks(*deltas):
    """Build a list of SSE byte lines from one or more delta dicts."""
    lines = []
    for delta in deltas:
        payload = {"choices": [{"delta": delta}]}
        lines.append(b"data: " + json.dumps(payload).encode())
    lines.append(b"data: [DONE]")
    return lines


def _make_resp(*deltas):
    resp = MagicMock()
    resp.iter_lines.return_value = _stream_chunks(*deltas)
    resp.status_code = 200
    return resp


def _tool_call_delta(name, args, tc_id="1"):
    return {"tool_calls": [{
        "index": 0, "id": tc_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}


def test_tool_loop_forced_think():
    """Forced think is injected after _REPEAT_THRESHOLD identical tool errors."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()

    with patch("agent._llm_request") as mock_llm:
        tool_resp = _make_resp(_tool_call_delta("fail_tool", {"arg": 1}))
        done_resp = _make_resp({"content": "I am done"})

        call_count = 0

        def llm_side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            return tool_resp if call_count <= _REPEAT_THRESHOLD + 1 else done_resp

        mock_llm.side_effect = llm_side_effect

        with patch("agent.MAP_FN") as mock_map:
            fail_tool = MagicMock(return_value="Error: tool failed")
            think_tool = MagicMock(return_value="I have thought about it.")
            mock_map.__getitem__.side_effect = lambda k: (
                think_tool if k == "think" else fail_tool
            )
            mock_map.__contains__.side_effect = lambda k: True

            run_agent_single(conversation_history, summary_state, None, log)

            reflection_found = False
            for msg in conversation_history:
                if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                    continue
                for tc in msg["tool_calls"]:
                    args = tc.get("function", {}).get("arguments", "")
                    if "MANDATORY REFLECTION" in str(args):
                        reflection_found = True
                        break
            assert reflection_found, "Forced think prompt should have been injected"


def test_tool_loop_hard_bail():
    """Agent injects a SKIPPED message after _REPEAT_THRESHOLD*2 identical errors."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()

    with patch("agent._llm_request") as mock_llm:
        tool_resp = _make_resp(_tool_call_delta("fail_tool", {"arg": 1}))
        done_resp = _make_resp({"content": "I am done"})

        call_count = 0

        def llm_side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            return tool_resp if call_count <= 12 else done_resp

        mock_llm.side_effect = llm_side_effect

        with patch("agent.MAP_FN") as mock_map:
            fail_tool = MagicMock(return_value="Error: tool failed")
            think_tool = MagicMock(return_value="Thinking...")
            mock_map.__getitem__.side_effect = lambda k: (
                think_tool if k == "think" else fail_tool
            )
            mock_map.__contains__.side_effect = lambda k: True

            run_agent_single(conversation_history, summary_state, None, log)

            bail_found = any(
                "has failed" in str(msg.get("content", ""))
                and "SKIPPED" in str(msg.get("content", ""))
                for msg in conversation_history
            )
            assert bail_found, "Hard bail message should have been injected"


def test_tool_loop_reset_on_success():
    """A successful tool call between failures resets the repeat counter."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()

    with patch("agent._llm_request") as mock_llm:
        tool_resp = _make_resp(_tool_call_delta("tool_x", {}))
        done_resp = _make_resp({"content": "Stop"})

        call_count = 0

        def llm_side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            return tool_resp if call_count <= _REPEAT_THRESHOLD else done_resp

        mock_llm.side_effect = llm_side_effect

        results = ["Error: fail", "Success", "Error: fail"]
        result_idx = 0

        def tool_side_effect(**kw):
            nonlocal result_idx
            r = results[min(result_idx, len(results) - 1)]
            result_idx += 1
            return r

        with patch("agent.MAP_FN") as mock_map:
            mock_tool = MagicMock(side_effect=tool_side_effect)
            mock_map.__getitem__.side_effect = lambda k: mock_tool
            mock_map.__contains__.side_effect = lambda k: True

            run_agent_single(conversation_history, summary_state, None, log)

            reflection_found = any(
                "MANDATORY REFLECTION" in str(msg.get("content", ""))
                for msg in conversation_history
            )
            assert not reflection_found, "Success should have reset the loop tracker"
