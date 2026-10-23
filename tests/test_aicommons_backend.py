"""AICommonsBackend against a fake HTTP transport (aicommons-integration.md P2, §7 H1).

The real ``anthropic`` SDK runs end to end; only the socket is replaced, by an
``httpx.MockTransport`` that plays back gateway responses (SSE streams, JSON
errors). No network, no key.
"""

import json
import logging

import httpx
import pytest
import requests

import llm_backend as lb
from anthropic_wire import THINKING_KEY

log = logging.getLogger("test_aicommons")
BASE = "https://gw.example/v1"


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def text_turn(text="hello", stop="end_turn", usage=None):
    return sse(
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-v5.5-opus",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 5000, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": usage or {"input_tokens": 12, "output_tokens": 3,
                            "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 100}},
        {"type": "message_stop"},
    )


def error_event(etype, message):
    return sse({"type": "error", "error": {"type": etype, "message": message}})


class Gateway:
    """Scripted responses, one per request; records every request."""

    def __init__(self, *responses, quota=None):
        self.responses = list(responses)
        self.requests = []
        self.quota = quota            # dict served at /v1/token-usage; None → 404
        self.quota_calls = 0

    def __call__(self, request: httpx.Request):
        if request.url.path.endswith("/token-usage"):
            self.quota_calls += 1
            if self.quota is None:
                return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error",
                                                                            "message": "no"}})
            return httpx.Response(200, json=self.quota)
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, content=item, headers={
            "content-type": "text/event-stream", "request-id": "req_abc"})

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


def status(code, etype, message, headers=None):
    return httpx.Response(code, json={"type": "error", "error": {"type": etype, "message": message}},
                          headers=headers or {})


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    monkeypatch.setenv("AICOMMONS_SPEND_FILE", str(tmp_path / "spend.json"))
    monkeypatch.delenv("AICOMMONS_DAILY_CAP_USD", raising=False)

    def _make(gw, **cfg):
        b = lb.AICommonsBackend({"kind": "aicommons", "base_url": BASE, "api_key": "sk-test",
                                 "model": "claude-v5.5-opus", **cfg})
        b.client = b.client.with_options(
            http_client=httpx.Client(transport=httpx.MockTransport(gw), base_url=BASE), max_retries=0)
        return b
    return _make


AGENT_BODY = {
    "model": "whatever", "stream": True, "max_tokens": 512, "temperature": 0.7, "top_p": 0.9,
    "top_k": 40, "repeat_penalty": 1.1, "cache_prompt": True, "seed": 3,
    "chat_template_kwargs": {"enable_thinking": False},
    "messages": [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "system", "content": "memory"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "ok"},
    ],
    "tools": [{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
              for n in ("write_file", "read_file")],
}


def test_stream_text_and_usage_and_request_hygiene(make):
    gw = Gateway(text_turn("hi there"))
    b = make(gw)
    chunks = list(b.stream_chat(log, json=AGENT_BODY, stream=True))
    assert gw.requests[0].url.path == "/v1/v1/messages"     # stage URL + SDK's /v1/messages (P0.1)
    body = gw.body()
    for k in ("temperature", "top_p", "top_k", "repeat_penalty", "cache_prompt", "seed", "chat_template_kwargs"):
        assert k not in body
    assert body["model"] == "claude-v5.5-opus"
    assert body["system"] == "SYSTEM PROMPT\n\nmemory"
    assert body["messages"][-1]["role"] == "user"            # never ends on a prefill
    assert [t["name"] for t in body["tools"]] == ["read_file", "write_file"]
    assert body["tool_choice"] == {"type": "auto"}
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert body["max_tokens"] == 21_600                      # sized to the 600 s wall, not the body's 512
    assert body["stream"] is True
    assert [c["choices"][0]["delta"] for c in chunks[:-1]] == [{"content": "hi there"}]
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["prompt_tokens"] == 4112 and final["usage"]["completion_tokens"] == 3


def test_summary_role_defaults_to_low_effort(make):
    gw = Gateway(text_turn())
    list(make(gw, role="summary", model="claude-v5.5-haiku").stream_chat(log, json=AGENT_BODY))
    assert gw.body()["output_config"] == {"effort": "low"}


def test_overflow_sent_as_first_stream_event_raises_from_stream_chat(make):
    """H1: the gateway reports Bedrock's 'input too long' as the first event of a 200
    stream. It must raise from stream_chat() itself, where the agent's overflow
    recovery runs — not on the first next(), where it would count as an empty turn."""
    gw = Gateway(error_event("invalid_request_error", "Input is too long for requested model."))
    with pytest.raises(lb.ContextOverflowError):
        make(gw).stream_chat(log, json=AGENT_BODY)


@pytest.mark.parametrize("resp, exc", [
    (status(400, "invalid_request_error", "prompt is too long: 1200000 tokens > 1000000 maximum"),
     lb.ContextOverflowError),
    (status(413, "request_too_large", "Request exceeds the maximum allowed number of bytes"),
     lb.ContextOverflowError),
    (status(401, "authentication_error", "invalid x-api-key"), lb.ConfigError),
    (status(403, "permission_error", "user is not in the openai group"), lb.ConfigError),
    (status(429, "rate_limit_error", "Monthly token allowance exhausted for this user."), lb.BudgetExceeded),
    (status(429, "rate_limit_error", "reserves up to 9 weighted tokens", {"x-should-retry": "false"}),
     lb.BudgetExceeded),
    (status(429, "rate_limit_error", "Bedrock is throttling requests; retry shortly."),
     lb.BackendUnavailableError),
    (status(529, "overloaded_error", "Overloaded"), lb.BackendUnavailableError),
    (status(500, "api_error", "boom"), lb.BackendUnavailableError),
    (status(504, "timeout_error", "request_ceiling_exceeded"), lb.GatewayTimeoutError),
    (status(400, "invalid_request_error", "tools.0.name: bad"), lb.BackendRequestError),
])
def test_error_mapping(make, resp, exc):
    with pytest.raises(exc) as ei:
        make(Gateway(resp)).stream_chat(log, json=AGENT_BODY)
    if exc is lb.GatewayTimeoutError:
        assert "504" in str(ei.value)                        # the agent's size-hint path keys on this
    if exc in (lb.BackendUnavailableError, lb.GatewayTimeoutError, lb.BackendRequestError):
        assert isinstance(ei.value, requests.exceptions.RequestException)
    assert lb.BedrockBudgetExceeded is lb.BudgetExceeded


def test_connection_error_is_failover_eligible(make):
    with pytest.raises(requests.exceptions.ConnectionError):
        make(Gateway(httpx.ConnectError("refused"))).stream_chat(log, json=AGENT_BODY)


def test_mid_stream_error_before_content_is_resent_once(make):
    gw = Gateway(
        sse({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                  "model": "x", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
        text_turn("second try"))
    chunks = list(make(gw).stream_chat(log, json=AGENT_BODY))
    assert len(gw.requests) == 2
    assert {"content": "second try"} in [c["choices"][0]["delta"] for c in chunks]


def test_mid_stream_error_after_content_is_not_resent(make):
    partial = text_turn("partial").split(b"event: message_delta")[0]
    gw = Gateway(partial + error_event("overloaded_error", "Overloaded"), text_turn("never"))
    gen = make(gw).stream_chat(log, json=AGENT_BODY)
    first = next(gen)
    assert first["choices"][0]["delta"] == {"content": "partial"}
    with pytest.raises(lb.BackendUnavailableError):
        list(gen)
    assert len(gw.requests) == 1


def test_thinking_and_tools_stream_and_come_back(make):
    turn = sse(
        {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "x",
                                              "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_9", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path": "x"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    )
    gw = Gateway(turn, text_turn("done"))
    b = make(gw)
    chunks = list(b.stream_chat(log, json=AGENT_BODY))
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert {"reasoning_content": "hmm"} in deltas
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    stored = final[THINKING_KEY]
    # the agent stores it on the assistant turn; the next request carries it back verbatim
    history = AGENT_BODY["messages"][:3] + [
        {"role": "assistant", "content": "", THINKING_KEY: stored, "tool_calls": [
            {"id": "toolu_9", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_9", "content": "file text"},
    ]
    list(b.stream_chat(log, json={"messages": history}))
    sent = gw.body()["messages"]
    assert sent[-2]["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": "SIG"}
    assert sent[-2]["content"][1]["type"] == "tool_use"


def test_payload_guard_raises_overflow_locally(make):
    gw = Gateway()
    big = {"messages": [{"role": "user", "content": "x" * 2_000}]}
    with pytest.raises(lb.ContextOverflowError):
        make(gw, max_request_bytes=1_000).stream_chat(log, json=big)
    assert gw.requests == []


def test_closing_the_generator_closes_the_stream(make):
    b = make(Gateway(text_turn("a")))
    gen = b.stream_chat(log, json=AGENT_BODY)
    next(gen)
    gen.close()                     # what agent._safe_close does on cancel / text cap / wall


def test_complete_is_non_streaming_with_role_budget(make):
    resp = httpx.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-v5.5-haiku",
        "content": [{"type": "thinking", "thinking": "x", "signature": "s"}, {"type": "text", "text": "summary"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 2}})
    gw = Gateway(resp)
    out = make(gw, role="summary", model="claude-v5.5-haiku").complete(prompt="summarise")
    assert out == "summary"
    body = gw.body()
    assert body["max_tokens"] == 4096 and "stream" not in body
    assert body["messages"] == [{"role": "user", "content": "summarise"}]


def test_health_uses_models_and_checks_the_configured_model(make):
    models = {"data": [{"id": "claude-v5.5-opus", "type": "model", "created_at": "2026-01-01T00:00:00Z",
                        "display_name": "x"}], "has_more": False, "first_id": None, "last_id": None}
    ok, msg = make(Gateway(httpx.Response(200, json=models))).health()
    assert ok, msg
    ok, msg = make(Gateway(httpx.Response(200, json=models)), model="claude-v9-opus").health()
    assert not ok and "claude-v9-opus" in msg
    ok, msg = make(Gateway(status(401, "authentication_error", "bad key"))).health()
    assert not ok and "openai" in msg


def test_ctx_size_is_tokens_capped_by_config(make):
    assert make(Gateway()).detect_ctx_size() == 200_000
    assert make(Gateway(), model="claude-v4.5-haiku", max_context_tokens=500_000).detect_ctx_size() == 200_000


def test_factory_aliases_and_missing_credentials(monkeypatch):
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    monkeypatch.delenv("AICOMMONS_BASE_URL", raising=False)
    monkeypatch.delenv("AICOMMONS_API_KEY", raising=False)
    with pytest.raises(lb.ConfigError):
        lb.build_backend({"kind": "ucsb"})
    monkeypatch.setenv("AICOMMONS_BASE_URL", BASE + "/v1/messages")
    monkeypatch.setenv("AICOMMONS_API_KEY", "sk")
    b = lb.build_backend({"kind": "ai-commons"})
    assert isinstance(b, lb.AICommonsBackend) and b.base_url == BASE


def test_system_is_never_folded_for_this_kind(make):
    b = make(Gateway())                 # no responses scripted: a probe request would fail the test
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert lb.maybe_fold_system(msgs, b, log) is msgs


# ── P6: quota and spend ─────────────────────────────────────────────────


def test_real_usage_is_priced_and_recorded(make, tmp_path):
    gw = Gateway(text_turn("hi", usage={"input_tokens": 1000, "output_tokens": 2000,
                                        "cache_read_input_tokens": 10_000, "cache_creation_input_tokens": 0}))
    b = make(gw)
    list(b.stream_chat(log, json=AGENT_BODY))
    # opus 5.5 per 1K: in .0055, out .0275, cache read .00055
    expected = 1 * 0.0055 + 2 * 0.0275 + 10 * 0.00055
    assert b.session_usage["usd"] == pytest.approx(expected)
    assert lb.aicommons_today_spend("main") == pytest.approx(expected)
    assert "usd=" in b.session_summary()


def test_daily_cap_refuses_before_sending(make, monkeypatch):
    monkeypatch.setenv("AICOMMONS_DAILY_CAP_USD", "0.01")
    lb.aicommons_record_spend("main", "claude-v5.5-opus", 0.02, {})
    gw = Gateway(text_turn())
    with pytest.raises(lb.BudgetExceeded, match="daily cap"):
        make(gw).stream_chat(log, json=AGENT_BODY)
    assert gw.requests == []


def test_quota_floor_refuses_with_reset_time(make):
    gw = Gateway(text_turn(), quota={"total_tokens": 990, "limit": 1000, "remaining": 10,
                                     "reset_at": "2026-11-01T00:00:00Z"})
    with pytest.raises(lb.BudgetExceeded) as ei:
        make(gw).stream_chat(log, json=AGENT_BODY)
    assert "2026-11-01" in str(ei.value) and gw.requests == []


def test_max_tokens_shrinks_to_fit_the_reservation(make):
    gw = Gateway(text_turn(), quota={"total_tokens": 0, "limit": 10_000_000, "remaining": 10_000,
                                     "reset_at": "x"})
    list(make(gw).stream_chat(log, json=AGENT_BODY))
    assert 1024 <= gw.body()["max_tokens"] < 10_000


def test_quota_is_polled_at_most_every_five_minutes(make):
    gw = Gateway(text_turn(), text_turn(), quota={"total_tokens": 1, "limit": 10_000_000,
                                                  "remaining": 9_999_999, "reset_at": "x"})
    b = make(gw)
    list(b.stream_chat(log, json=AGENT_BODY))
    list(b.stream_chat(log, json=AGENT_BODY))
    assert gw.quota_calls == 1


def test_spend_file_survives_concurrent_writers(make):
    import threading
    threads = [threading.Thread(target=lambda: [lb.aicommons_record_spend("main", "m", 0.001, {})
                                                for _ in range(20)]) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert lb.aicommons_today_spend("main") == pytest.approx(0.1)
