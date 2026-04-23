"""Dev-mode prompt-stuffing round-trip tests (pure string manipulation).

Covers the six cases enumerated in plan § 13.2 plus the D6-b preamble
merge test. No network, no subprocess — these exercise
``dev_mode_prompt.build_dev_prompt`` / ``parse_dev_response`` /
``is_truncated`` directly.
"""

import json

from dev_mode_prompt import (
    DEV_MODE_PREAMBLE,
    build_dev_prompt,
    is_truncated,
    parse_dev_response,
)


# (1) OpenAI tools + single-user message → prompt text contains the preamble,
#     one-shot example, user message, terminal Assistant.
def test_build_prompt_tools_and_user():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "file",
                "description": "read/write files",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path"},
                    },
                    "required": ["path"],
                },
            },
        }
    ]
    messages = [{"role": "user", "content": "hello"}]
    prompt = build_dev_prompt(messages, tools)

    assert "[System]" in prompt
    assert "AVAILABLE TOOLS:" in prompt
    assert "file:" in prompt and "read/write files" in prompt
    assert "- path (string (required)):" in prompt
    # One-shot example verbatim from llmbox_lib.py:746.
    assert '{"tool": "tool_name", "args": {"param1": "value1", "param2": "value2"}}' in prompt
    assert prompt.rstrip().endswith("Assistant:")
    assert "User: hello" in prompt


# (2) History with assistant tool_calls + tool result → includes
#     [Tool call: name({args})] and [Tool result (name): ...] lines.
def test_build_prompt_assistant_tool_calls_and_result():
    messages = [
        {"role": "user", "content": "list /tmp"},
        {
            "role": "assistant",
            "content": "running ls",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    # OpenAI encodes arguments as a JSON-encoded string.
                    "function": {"name": "exec", "arguments": '{"cmd": "ls /tmp"}'},
                }
            ],
        },
        {"role": "tool", "name": "exec", "content": "a.txt\nb.txt"},
    ]
    prompt = build_dev_prompt(messages, tools=[])

    assert "User: list /tmp" in prompt
    assert "Assistant: running ls" in prompt
    assert '[Tool call: exec({"cmd": "ls /tmp"})]' in prompt
    assert "[Tool result (exec): a.txt\nb.txt]" in prompt


# (3) Response with one <tool_call> → yields content + one tool_calls delta.
def test_parse_response_single_tool_call():
    text = (
        "Sure, reading now.\n"
        '<tool_call>{"tool":"file","args":{"path":"/x"}}</tool_call>\n'
        "Done."
    )
    narrative, calls = parse_dev_response(text)

    assert "Sure, reading now." in narrative
    assert "Done." in narrative
    assert "<tool_call>" not in narrative
    assert len(calls) == 1
    assert calls[0]["index"] == 0
    assert calls[0]["type"] == "function"
    assert calls[0]["id"].startswith("call_")
    assert calls[0]["function"]["name"] == "file"
    # Arguments must be a JSON-encoded string (OpenAI streaming shape).
    assert calls[0]["function"]["arguments"] == '{"path": "/x"}'
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "/x"}


# (4) Response with two <tool_call> blocks → two deltas, index 0 and 1.
def test_parse_response_multiple_tool_calls():
    text = (
        '<tool_call>{"tool":"a","args":{"x":1}}</tool_call>\n'
        '<tool_call>{"name":"b","arguments":{"y":2}}</tool_call>'
    )
    narrative, calls = parse_dev_response(text)

    assert narrative == ""
    assert [c["index"] for c in calls] == [0, 1]
    assert calls[0]["function"]["name"] == "a"
    assert calls[1]["function"]["name"] == "b"
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}
    assert json.loads(calls[1]["function"]["arguments"]) == {"y": 2}


# (5) Malformed JSON inside a <tool_call> → silently dropped, no exception.
def test_parse_response_malformed_json_silently_dropped():
    text = (
        "ok\n"
        '<tool_call>{"tool": "good", "args": {"a": 1}}</tool_call>\n'
        "<tool_call>{this is not json}</tool_call>\n"
    )
    narrative, calls = parse_dev_response(text)

    assert "ok" in narrative
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "good"


# (6) Truncated <tool_call> (no closing tag) → is_truncated returns True.
def test_is_truncated_positive_and_negative():
    truncated = "text before\n<tool_call>{\"tool\":\"x\",\"args\":{}"
    assert is_truncated(truncated) is True
    # Closed block → not truncated.
    complete = '<tool_call>{"tool":"x","args":{}}</tool_call>'
    assert is_truncated(complete) is False
    # No tool_call at all → not truncated.
    assert is_truncated("just text") is False


# D6-b: preamble merges with system prompts, not replaces.
def test_preamble_merges_with_system_prompts():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "system", "content": "Cwd is /tmp."},
        {"role": "user", "content": "hi"},
    ]
    prompt = build_dev_prompt(messages, tools=[])

    # Both system fragments merged inside [System] ... [End System].
    sys_block = prompt.split("[End System]")[0]
    assert "You are a helpful assistant." in sys_block
    assert "Cwd is /tmp." in sys_block
    # The preamble is still there — merge, not replace.
    assert "AVAILABLE TOOLS:" in sys_block
    assert "TO USE A TOOL, include a tool call block in your response:" in sys_block


# DEV_MODE_PREAMBLE is a constant that tests can assert against (K13 mitigation).
def test_dev_mode_preamble_constant_shape():
    assert "AVAILABLE TOOLS:" in DEV_MODE_PREAMBLE
    assert "RULES:" in DEV_MODE_PREAMBLE
    assert "<tool_call>" in DEV_MODE_PREAMBLE
    assert "</tool_call>" in DEV_MODE_PREAMBLE


# Extras — sanitize strips <think> and normalizes fancy quotes.
def test_sanitize_strips_think_tags_and_smart_quotes():
    text = "<think>thinking</think>\nHe said “hi”—ok."
    narrative, calls = parse_dev_response(text)
    assert calls == []
    assert "<think>" not in narrative
    assert "</think>" not in narrative
    assert '"hi"--ok.' in narrative
