"""AnthropicBackend (kind "anthropic"): the Anthropic API over AICommonsBackend's
Messages-API path. The real ``anthropic`` SDK runs; only the socket is replaced."""

import httpx
import pytest

import llm_backend as lb
from test_aicommons_backend import AGENT_BODY, Gateway, log, status, text_turn

API = "https://api.anthropic.com"

MODEL_INFO = {
    "type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
    "created_at": "2026-09-01T00:00:00Z", "max_input_tokens": 1_000_000, "max_tokens": 128_000,
    "capabilities": {"thinking": {"supported": True, "types": {
        "enabled": {"supported": False}, "adaptive": {"supported": True}}}},
}


class Api(Gateway):
    """The gateway fake, plus the Models API."""

    def __init__(self, *responses, models=(MODEL_INFO,)):
        super().__init__(*responses)
        self.models = {m["id"]: m for m in models}
        self.model_calls = 0

    def __call__(self, request: httpx.Request):
        path = request.url.path
        if path == "/v1/models":
            self.model_calls += 1
            return httpx.Response(200, json={"data": list(self.models.values()), "has_more": False,
                                             "first_id": None, "last_id": None})
        if path.startswith("/v1/models/"):
            self.model_calls += 1
            m = self.models.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=m) if m else status(404, "not_found_error", "no model")
        return super().__call__(request)


@pytest.fixture
def make(monkeypatch):
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(lb.AnthropicBackend, "_info_cache", {})

    def _make(api, **cfg):
        b = lb.build_backend({"kind": "anthropic", "api_key": "sk-ant-test",
                              "model": "claude-opus-5-5", **cfg})
        b.client = b.client.with_options(
            http_client=httpx.Client(transport=httpx.MockTransport(api), base_url=API), max_retries=0)
        return b
    return _make


def test_kind_and_aliases():
    assert lb.normalize_kind("claude") == "anthropic"
    assert lb.normalize_kind("anthropic-api") == "anthropic"
    assert "anthropic" in lb.MESSAGES_KINDS


def test_request_carries_drop_block_and_its_beta(make):
    api = Api(text_turn("hi"))
    b = make(api)
    list(b.stream_chat(log, json=AGENT_BODY))
    req = api.requests[0]
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert "thinking-binding-controls-2026-08-01" in req.headers["anthropic-beta"]
    body = api.body()
    assert body["thinking"] == {"type": "adaptive",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "high"}
    for k in ("temperature", "top_p", "top_k"):
        assert k not in body


def test_health_reads_limits_from_the_models_api(make):
    api = Api()
    b = make(api)
    assert b.info == lb._ANTHROPIC_FALLBACK_INFO           # no request at construction
    ok, _ = b.health()
    assert ok
    assert b.info == {"context_tokens": 1_000_000, "max_output": 128_000, "reasoning": True}
    assert b.detect_ctx_size() == 200_000                  # still capped by max_context_tokens
    calls = api.model_calls
    make(api).health()                                     # a second role reuses the cache
    assert api.model_calls == calls + 1                    # the list, not the model again


def test_model_without_adaptive_thinking_sends_none(make):
    old = {**MODEL_INFO, "id": "claude-haiku-4-5", "max_tokens": 64_000,
           "capabilities": {"thinking": {"supported": True, "types": {
               "enabled": {"supported": True}, "adaptive": {"supported": False}}}}}
    api = Api(text_turn(), models=(old,))
    b = make(api, model="claude-haiku-4-5")
    assert b.health()[0]
    list(b.stream_chat(log, json=AGENT_BODY))
    assert "thinking" not in api.body() and "output_config" not in api.body()


def test_no_quota_endpoint_is_called(make):
    api = Api(text_turn())
    b = make(api)
    list(b.stream_chat(log, json=AGENT_BODY))
    assert api.quota_calls == 0
    assert "quota" not in b.session_summary()
    assert b.session_summary().startswith("anthropic.session ")


def test_errors_name_the_api(make):
    b = make(Api(status(401, "authentication_error", "invalid x-api-key")))
    with pytest.raises(lb.ConfigError, match="Anthropic API rejected the API key.*ANTHROPIC_API_KEY"):
        list(b.stream_chat(log, json=AGENT_BODY))
    b = make(Api(status(400, "invalid_request_error", "Your credit balance is too low to access the API")))
    with pytest.raises(lb.BudgetExceeded, match="credit balance"):
        list(b.stream_chat(log, json=AGENT_BODY))


def test_missing_key_is_a_config_error(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))                     # no saved login either
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    b = lb.build_backend({"kind": "anthropic", "model": "claude-opus-5-5"})
    ok, detail = b.health()
    assert not ok and "No Anthropic API key found" in detail


def test_unknown_profile_is_a_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_PROFILE", "nope")
    with pytest.raises(lb.ConfigError, match="Anthropic API credentials"):
        lb.build_backend({"kind": "anthropic", "model": "claude-opus-5-5"})


def test_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    b = lb.build_backend({"kind": "anthropic", "model": "claude-opus-5-5"})
    assert b.api_key == "sk-ant-env" and b.base_url == API


def test_backend_flag_uses_the_api_with_default_models(monkeypatch):
    import agent
    monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    monkeypatch.setitem(agent._config, "backends", {
        "main": {"kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "local-27b"},
        "summary": {"kind": "llamacpp", "base_url": "http://127.0.0.1:8082"}})
    monkeypatch.setattr(agent, "_main_backend", None)
    monkeypatch.setattr(agent, "_summary_backend", None)
    agent._apply_backend_overrides("anthropic", "anthropic")
    assert agent._main_backend.kind == "anthropic"
    assert agent._main_backend.base_url == API                 # not the llamacpp default
    assert agent._main_backend.model == "claude-sonnet-5-5"
    assert agent._summary_backend.model == "claude-haiku-5-5"
    assert agent._prompt_caching_backend()


def test_shared_block_reaches_the_role(monkeypatch):
    import agent
    monkeypatch.setitem(agent._config, "anthropic", {"api_key": "sk-shared", "thinking": "off"})
    entry = agent._cfg_with_role({"main": {"kind": "anthropic", "model": "m"}}, "main")
    assert entry["anthropic"] == {"api_key": "sk-shared", "thinking": "off"}
    b = lb.build_backend(entry)
    assert b.api_key == "sk-shared" and b.opts["thinking"] == "off"
