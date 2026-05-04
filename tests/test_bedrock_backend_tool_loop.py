"""End-to-end tests for BedrockBackend.stream_chat.

Covers the dev-mode round-trip: dev prompt serialization → synthetic
gateway response containing ``<tool_call>`` blocks → OpenAI-shape SSE
deltas. Also exercises the truncation-recovery loop at the unit level
(§ 8.3).

Per plan § 13.2 / task 2.3. Mocks ``BedrockChatAPI.send_and_wait_conv``
directly — no real network.
"""

import json
import logging
from unittest.mock import patch

import pytest

from llm_backend import BedrockBackend


def _mock_msg(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"contentType": "text", "body": text}],
    }


def _make_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    return BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
    )


# ── Basic round-trip: narrative + tool_call → SSE deltas ──


def test_stream_chat_yields_content_and_tool_call(monkeypatch, caplog, tmp_path):
    b = _make_backend(monkeypatch, tmp_path)
    synthetic = (
        "Looking up the file.\n"
        '<tool_call>{"tool":"file","args":{"path":"/x"}}</tool_call>'
    )
    with patch.object(
        b._api,
        "send_and_wait_conv",
        return_value=(_mock_msg(synthetic), "conv-1"),
    ):
        with caplog.at_level(logging.INFO, logger="llm_backend"):
            chunks = list(
                b.stream_chat(
                    logging.getLogger("llm_backend"),
                    messages=[{"role": "user", "content": "read /x"}],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "file",
                                "description": "read a file",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                            },
                        }
                    ],
                )
            )

    # One content delta, one tool_calls delta.
    assert len(chunks) == 2
    delta0 = chunks[0]["choices"][0]["delta"]
    assert "Looking up the file." in delta0["content"]
    delta1 = chunks[1]["choices"][0]["delta"]
    assert "tool_calls" in delta1
    tc = delta1["tool_calls"][0]
    assert tc["function"]["name"] == "file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "/x"}

    # Telemetry.
    msgs = [r.message for r in caplog.records]
    assert any("bedrock.tool_parse.result" in m and "parsed_calls=1" in m for m in msgs)
    assert any(
        "backend.stream_chat.latency_ms" in m and "backend=bedrock" in m
        for m in msgs
    )


# ── Empty tools: just content delta ──


def test_stream_chat_plain_text_yields_single_content_delta(monkeypatch, tmp_path):
    b = _make_backend(monkeypatch, tmp_path)
    with patch.object(
        b._api,
        "send_and_wait_conv",
        return_value=(_mock_msg("just some text"), "c1"),
    ):
        chunks = list(
            b.stream_chat(
                logging.getLogger("llm_backend"),
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
            )
        )
    assert len(chunks) == 1
    assert chunks[0]["choices"][0]["delta"]["content"] == "just some text"


# ── Two tool calls → two deltas ──


def test_stream_chat_multiple_tool_calls(monkeypatch, tmp_path):
    b = _make_backend(monkeypatch, tmp_path)
    synthetic = (
        '<tool_call>{"tool":"a","args":{"x":1}}</tool_call>\n'
        '<tool_call>{"tool":"b","args":{"y":2}}</tool_call>'
    )
    with patch.object(
        b._api,
        "send_and_wait_conv",
        return_value=(_mock_msg(synthetic), "c1"),
    ):
        chunks = list(
            b.stream_chat(
                logging.getLogger("llm_backend"),
                messages=[{"role": "user", "content": "do both"}],
                tools=[],
            )
        )
    # Two tool_calls deltas (no narrative since _strip leaves empty string).
    tc_chunks = [c for c in chunks if "tool_calls" in c["choices"][0]["delta"]]
    assert len(tc_chunks) == 2
    assert tc_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert tc_chunks[1]["choices"][0]["delta"]["tool_calls"][0]["index"] == 1


# ── Truncation recovery: first response unterminated, second closes it ──


def test_stream_chat_truncation_recovery(monkeypatch, caplog, tmp_path):
    b = _make_backend(monkeypatch, tmp_path)
    first = 'Reading.\n<tool_call>{"tool":"file","args":{"p":"/x"'
    second_tail = '}}</tool_call>'

    calls = []

    def fake_send_and_wait_conv(prompt, conversation_id=None, cancel_check=None, inference_params=None):
        calls.append((prompt, conversation_id))
        if len(calls) == 1:
            return _mock_msg(first), "conv-1"
        return _mock_msg(second_tail), "conv-1"

    with patch.object(b._api, "send_and_wait_conv", side_effect=fake_send_and_wait_conv):
        with caplog.at_level(logging.INFO, logger="llm_backend"):
            chunks = list(
                b.stream_chat(
                    logging.getLogger("llm_backend"),
                    messages=[{"role": "user", "content": "go"}],
                    tools=[],
                )
            )

    # Second call must re-use the conversation ID from the first.
    assert calls[1][1] == "conv-1"
    # Continuation prompt keyword.
    assert "truncated" in calls[1][0].lower()
    # Final parse succeeds: one tool call.
    tc_chunks = [c for c in chunks if "tool_calls" in c["choices"][0]["delta"]]
    assert len(tc_chunks) == 1

    msgs = [r.message for r in caplog.records]
    assert any("bedrock.truncation_recovery.attempted" in m for m in msgs)
    assert any("bedrock.truncation_recovery.succeeded" in m for m in msgs)


# ── Tool-result round-trip: serialized prompt shape ──


def test_stream_chat_serializes_tool_results(monkeypatch, tmp_path):
    """If the next stream_chat's messages include a tool result, the
    serialized prompt must contain the ``[Tool result ...]`` line so
    the model can see the prior tool output.
    """
    b = _make_backend(monkeypatch, tmp_path)
    seen_prompts = []

    def fake_send_and_wait_conv(prompt, conversation_id=None, cancel_check=None, inference_params=None):
        seen_prompts.append(prompt)
        return _mock_msg("done"), "conv-2"

    history = [
        {"role": "user", "content": "ls /tmp"},
        {
            "role": "assistant",
            "content": "running",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": '{"cmd":"ls /tmp"}'},
                }
            ],
        },
        {"role": "tool", "name": "exec", "content": "a.txt\nb.txt"},
    ]
    with patch.object(b._api, "send_and_wait_conv", side_effect=fake_send_and_wait_conv):
        list(
            b.stream_chat(
                logging.getLogger("llm_backend"),
                messages=history,
                tools=[],
            )
        )
    assert "[Tool result (exec): a.txt\nb.txt]" in seen_prompts[0]
    # json.dumps emits ``{"cmd": "..."}`` (space after colon by default).
    assert '[Tool call: exec({"cmd": "ls /tmp"})]' in seen_prompts[0]
