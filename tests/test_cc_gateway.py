"""Tests for cc_gateway — the `agent.py -cc` Claude Code gateway.

Covers the Anthropic↔OpenAI translation in both directions and the live
FastAPI endpoint behaviour against fake backends shaped like the two real
backend kinds:

  * llamacpp → a ``requests.Response``-like object exposing ``iter_lines()``
    that streams byte SSE frames (the path `-cc` resolves to by default), and
  * bedrock  → a plain generator of OpenAI-shape delta dicts.

The streaming-path regression guards (tool name arriving in a later delta,
``finish_reason:"stop"`` on a turn that contains tool calls) reproduce the
real gemma-via-llama.cpp behaviour that an idealized fake would mask.
"""
import json

import pytest
from fastapi.testclient import TestClient

import cc_gateway as g


# ── request translation (Anthropic → OpenAI) ──────────────────────────────
def test_messages_translation_full():
    req = g.MessagesRequest(
        model="claude-sonnet-4-5", max_tokens=1024,
        system=[{"type": "text", "text": "You are helpful."}],
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "NYC"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "72F"},
                {"type": "tool_result", "tool_use_id": "toolu_2",
                 "content": [{"type": "text", "text": "sunny"}]},
            ]},
        ],
    )
    msgs = g.anthropic_to_openai_messages(req)
    assert msgs[0] == {"role": "system", "content": "You are helpful."}
    assert msgs[1] == {"role": "user", "content": "hi"}
    am = msgs[2]
    assert am["role"] == "assistant" and am["content"] == "let me check"
    assert am["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(am["tool_calls"][0]["function"]["arguments"]) == {"city": "NYC"}
    # two tool_result blocks → two separate tool messages, never merged
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs == [
        {"role": "tool", "tool_call_id": "toolu_1", "content": "72F"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "sunny"},
    ]


def test_tools_and_tool_choice_translation():
    tools = [{"name": "f", "description": "d.",
              "input_schema": {"type": "object", "properties": {"x": {"type": "string"}},
                               "required": ["x"]}}]
    ot = g.anthropic_tools_to_openai(tools)
    assert ot[0]["type"] == "function"
    assert ot[0]["function"]["name"] == "f"
    assert ot[0]["function"]["parameters"]["required"] == ["x"]
    assert g.map_tool_choice({"type": "auto"}) == "auto"
    assert g.map_tool_choice({"type": "any"}) == "required"
    assert g.map_tool_choice({"type": "tool", "name": "f"}) == \
        {"type": "function", "function": {"name": "f"}}


def test_image_block_translation():
    req = g.MessagesRequest(
        model="m", max_tokens=10,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "AAAA"}},
        ]}],
    )
    content = g.anthropic_to_openai_messages(req)[0]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,AAAA")


def test_body_mirrors_agent_flags():
    class B:
        kind = "llamacpp"; model = "gemma-4-31B"
    req = g.MessagesRequest(model="claude", max_tokens=99,
                            messages=[{"role": "user", "content": "hi"}])
    body = g.build_openai_body(req, B())
    assert body["model"] == "gemma-4-31B"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["stream_options"] == {"include_usage": True}


# ── fakes shaped like the two real backend kinds ──────────────────────────
def _sse(d):
    return "data: " + json.dumps(d)


class _SSEResponse:
    """requests.Response-like: streams byte SSE frames via iter_lines()."""
    def __init__(self, frames):
        self._frames = frames
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        for f in self._frames:
            yield f.encode("utf-8") if isinstance(f, str) else f

    def close(self):
        self.closed = True


def _parse(raw):
    return [json.loads(ln[len("data: "):]) for ln in raw.splitlines()
            if ln.startswith("data: ")]


# Frames reproduce the awkward real-backend behaviour:
#   * tool id arrives before the tool name (name in a *later* delta)
#   * arguments split across two deltas
#   * finish_reason is "stop" on a turn that DID contain tool calls
_TOOL_FRAMES = [
    _sse({"choices": [{"delta": {"content": "Checking. "}}]}),
    "",  # blank SSE separator — must be skipped
    _sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_9", "function": {"arguments": ""}}]}}]}),
    _sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "get_weather", "arguments": '{"ci'}}]}}]}),
    _sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": 'ty":"NYC"}'}}]}}]}),
    _sse({"choices": [{"delta": {}, "finish_reason": "stop"}],
          "usage": {"completion_tokens": 7}}),
    "data: [DONE]",
]


class _LlamaToolBackend:
    kind = "llamacpp"; model = "gemma-4-31B"; _active_conv_id = "STALE"

    def stream_chat(self, log, *, json=None, stream=True, timeout=None):
        return _SSEResponse(_TOOL_FRAMES)


_TOOL_PAYLOAD = {
    "model": "claude-sonnet-4-5", "max_tokens": 256, "stream": True,
    "messages": [{"role": "user", "content": "weather?"}],
    "tools": [{"name": "get_weather",
               "input_schema": {"type": "object",
                                "properties": {"city": {"type": "string"}}}}],
}


def test_streaming_tool_call_via_iter_lines():
    backend = _LlamaToolBackend()
    client = TestClient(g.create_app(backend))
    with client.stream("POST", "/v1/messages", json=_TOOL_PAYLOAD) as r:
        assert r.status_code == 200
        datas = _parse("".join(r.iter_text()))

    events = [d.get("type") for d in datas]
    assert events[0] == "message_start"

    tu = [d for d in datas if d.get("type") == "content_block_start"
          and d["content_block"]["type"] == "tool_use"][0]
    # name correctly captured though it arrived in a later delta than the id
    assert tu["content_block"]["name"] == "get_weather"
    assert tu["content_block"]["id"] == "call_9"

    jp = "".join(d["delta"]["partial_json"] for d in datas
                 if d.get("type") == "content_block_delta"
                 and d["delta"].get("type") == "input_json_delta")
    assert json.loads(jp) == {"city": "NYC"}

    md = [d for d in datas if d.get("type") == "message_delta"][0]
    # forced to tool_use despite finish_reason="stop"
    assert md["delta"]["stop_reason"] == "tool_use"
    assert md["usage"]["output_tokens"] == 7
    # bedrock-style conversation reset applied per request
    assert backend._active_conv_id is None


def test_nonstreaming_tool_call():
    backend = _LlamaToolBackend()
    client = TestClient(g.create_app(backend))
    b = client.post("/v1/messages", json={**_TOOL_PAYLOAD, "stream": False}).json()
    assert b["stop_reason"] == "tool_use"
    assert b["content"][-1]["type"] == "tool_use"
    assert b["content"][-1]["name"] == "get_weather"
    assert b["content"][-1]["input"] == {"city": "NYC"}


def test_text_only_turn_ends_with_end_turn():
    class TextBackend:
        kind = "llamacpp"; model = "m"

        def stream_chat(self, log, *, json=None, stream=True, timeout=None):
            return _SSEResponse([
                _sse({"choices": [{"delta": {"content": "hello"}}]}),
                _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
                "data: [DONE]",
            ])

    client = TestClient(g.create_app(TextBackend()))
    payload = {"model": "x", "max_tokens": 50, "stream": True,
               "messages": [{"role": "user", "content": "hi"}]}
    with client.stream("POST", "/v1/messages", json=payload) as r:
        datas = _parse("".join(r.iter_text()))
    md = [d for d in datas if d.get("type") == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn"
    text = "".join(d["delta"]["text"] for d in datas
                   if d.get("type") == "content_block_delta")
    assert text == "hello"


def test_bedrock_generator_shape():
    """Bedrock yields delta dicts directly (no iter_lines)."""
    class GenBackend:
        kind = "bedrock"; model = "claude-v4.5-sonnet"

        def stream_chat(self, log, *, json=None, stream=True, timeout=None):
            return iter([
                {"choices": [{"delta": {"content": "hi there"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ])

    client = TestClient(g.create_app(GenBackend()))
    payload = {"model": "x", "max_tokens": 50, "stream": True,
               "messages": [{"role": "user", "content": "hi"}]}
    with client.stream("POST", "/v1/messages", json=payload) as r:
        datas = _parse("".join(r.iter_text()))
    text = "".join(d["delta"]["text"] for d in datas
                   if d.get("type") == "content_block_delta")
    assert text == "hi there"


def test_health_and_count_tokens():
    class B:
        kind = "llamacpp"; model = "m"

        def health(self):
            return (True, "ok")

        def stream_chat(self, *a, **k):
            return iter([])

    client = TestClient(g.create_app(B()))
    assert client.get("/health").json()["status"] == "ok"
    payload = {"model": "x", "max_tokens": 10,
               "messages": [{"role": "user", "content": "hello world"}]}
    assert client.post("/v1/messages/count_tokens", json=payload).json()["input_tokens"] > 0


# ── retry + graceful-degradation (temp resilience for flaky/slow backends) ──
def _good_text(text):
    return _SSEResponse([
        _sse({"choices": [{"delta": {"content": text}}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ])


class _SSERaising:
    """iter_lines yields frames then raises — simulates a mid-stream cut."""
    def __init__(self, frames, exc):
        self._frames = frames
        self._exc = exc

    def iter_lines(self, decode_unicode=False):
        for f in self._frames:
            yield f.encode("utf-8") if isinstance(f, str) else f
        raise self._exc

    def close(self):
        pass


class _RaiseThenSucceed:
    """Raises a transient error on the first `fails` calls, then streams text."""
    kind = "llamacpp"; model = "m"

    def __init__(self, fails, exc=None):
        self.calls = 0
        self.fails = fails
        self.exc = exc or ConnectionError

    def stream_chat(self, log, *, json=None, stream=True, timeout=None):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc(f"transient {self.calls}")
        return _good_text("recovered")


_STREAM_REQ = {"model": "x", "max_tokens": 50, "stream": True,
               "messages": [{"role": "user", "content": "hi"}]}


def _fast_retries(monkeypatch, n=2):
    monkeypatch.setattr(g, "_MAX_RETRIES", n)
    monkeypatch.setattr(g, "_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(g, "_RETRY_MAX_DELAY", 0.0)


def test_stream_retries_then_succeeds(monkeypatch):
    _fast_retries(monkeypatch, 2)
    b = _RaiseThenSucceed(fails=1)
    client = TestClient(g.create_app(b))
    with client.stream("POST", "/v1/messages", json=_STREAM_REQ) as r:
        datas = _parse("".join(r.iter_text()))
    assert b.calls == 2  # failed once, retried, succeeded
    text = "".join(d["delta"]["text"] for d in datas
                   if d.get("type") == "content_block_delta"
                   and d["delta"].get("type") == "text_delta")
    assert text == "recovered"
    md = [d for d in datas if d.get("type") == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert [d for d in datas if d.get("type") == "message_stop"]
    assert not [d for d in datas if d.get("type") == "error"]


def test_stream_gives_up_with_clean_error(monkeypatch):
    _fast_retries(monkeypatch, 2)
    b = _RaiseThenSucceed(fails=99)  # never recovers
    client = TestClient(g.create_app(b))
    with client.stream("POST", "/v1/messages", json=_STREAM_REQ) as r:
        datas = _parse("".join(r.iter_text()))
    assert b.calls == 3  # 1 initial + 2 retries
    errs = [d for d in datas if d.get("type") == "error"]
    assert errs and errs[0]["error"]["type"] == "overloaded_error"
    assert not [d for d in datas if d.get("type") == "content_block_start"]
    assert [d for d in datas if d.get("type") == "message_stop"]


def test_stream_non_retryable_not_retried(monkeypatch):
    _fast_retries(monkeypatch, 3)
    from llm_backend import BedrockBudgetExceeded

    class B:
        kind = "bedrock"; model = "m"

        def __init__(self):
            self.calls = 0

        def stream_chat(self, log, *, json=None, stream=True, timeout=None):
            self.calls += 1
            raise BedrockBudgetExceeded("quota gone")

    b = B()
    client = TestClient(g.create_app(b))
    with client.stream("POST", "/v1/messages", json=_STREAM_REQ) as r:
        datas = _parse("".join(r.iter_text()))
    assert b.calls == 1  # budget error is terminal — no retry
    assert [d for d in datas if d.get("type") == "error"]


def test_stream_midcut_closes_gracefully_as_truncated(monkeypatch):
    _fast_retries(monkeypatch, 2)

    class B:
        kind = "llamacpp"; model = "m"

        def __init__(self):
            self.calls = 0

        def stream_chat(self, log, *, json=None, stream=True, timeout=None):
            self.calls += 1
            return _SSERaising(
                [_sse({"choices": [{"delta": {"content": "partial answer"}}]})],
                ConnectionError("cut at 29s"),
            )

    b = B()
    client = TestClient(g.create_app(b))
    with client.stream("POST", "/v1/messages", json=_STREAM_REQ) as r:
        datas = _parse("".join(r.iter_text()))
    assert b.calls == 1  # content already streamed → NOT retried
    text = "".join(d["delta"]["text"] for d in datas
                   if d.get("type") == "content_block_delta"
                   and d["delta"].get("type") == "text_delta")
    assert text == "partial answer"
    # clean, well-formed close — never end_turn on a truncated turn
    md = [d for d in datas if d.get("type") == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "max_tokens"
    assert [d for d in datas if d.get("type") == "content_block_stop"]
    assert [d for d in datas if d.get("type") == "message_stop"]


def test_truncated_partial_tool_call_dropped(monkeypatch):
    _fast_retries(monkeypatch, 1)

    class B:
        kind = "llamacpp"; model = "m"
        calls = 0

        def stream_chat(self, log, *, json=None, stream=True, timeout=None):
            B.calls += 1
            return _SSERaising([
                _sse({"choices": [{"delta": {"content": "let me check"}}]}),
                _sse({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "t1",
                     "function": {"name": "foo", "arguments": '{"par'}}]}}]}),
            ], ConnectionError("cut mid tool-args"))

    client = TestClient(g.create_app(B()))
    with client.stream("POST", "/v1/messages", json=_STREAM_REQ) as r:
        datas = _parse("".join(r.iter_text()))
    starts = [d for d in datas if d.get("type") == "content_block_start"]
    # the half-written tool ('{"par') must NOT be shipped to Claude Code
    assert all(s["content_block"]["type"] != "tool_use" for s in starts)
    md = [d for d in datas if d.get("type") == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "max_tokens"


def test_nonstream_retries_then_succeeds(monkeypatch):
    _fast_retries(monkeypatch, 2)
    b = _RaiseThenSucceed(fails=1)
    client = TestClient(g.create_app(b))
    resp = client.post("/v1/messages",
                       json={**_STREAM_REQ, "stream": False})
    assert resp.status_code == 200
    assert b.calls == 2
    assert resp.json()["content"][0]["text"] == "recovered"
