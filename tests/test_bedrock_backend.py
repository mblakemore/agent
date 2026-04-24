"""Tests for BedrockBackend: construction, health, list_models,
detect_ctx_size, and complete().

Per plan § 13.2 / task 2.2. Network is mocked via
``BedrockChatAPI.session`` so no real HTTP is issued.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from llm_backend import (
    BedrockBackend,
    BedrockBudgetExceeded,
    ConfigError,
    build_backend,
)


# ── Construction ──


def test_bedrock_backend_requires_env(monkeypatch):
    monkeypatch.delenv("BEDROCK_API_URL", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        build_backend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    assert "BEDROCK_API_URL" in str(exc.value)
    assert "BEDROCK_API_KEY" in str(exc.value)


def test_bedrock_backend_env_provides_creds(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://gateway.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = build_backend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    assert isinstance(b, BedrockBackend)
    assert b.kind == "bedrock"
    assert b.model == "claude-v4.5-haiku"
    assert b.api_url == "https://gateway.example.com/api"


def test_bedrock_backend_config_beats_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://env.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "env-key-aaaaaaaaaaaaaaaaaaaaaaaaa")
    cfg = {
        "kind": "bedrock",
        "api_url": "https://cfg.example.com/api",
        "api_key": "cfg-key-aaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "model": "claude-v4.5-haiku",
    }
    b = build_backend(cfg)
    assert b.api_url == "https://cfg.example.com/api"


def test_bedrock_trims_trailing_slash(monkeypatch):
    # K4 mitigation — trailing slash must be stripped.
    monkeypatch.setenv("BEDROCK_API_URL", "https://example.com/api/")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    assert b.api_url == "https://example.com/api"
    assert b.base_url == "https://example.com/api"


# ── health / list_models / detect_ctx_size ──


def test_bedrock_health_ok(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    with patch.object(b._api, "health", return_value=True):
        ok, detail = b.health()
    assert ok is True
    assert detail == b.api_url


def test_bedrock_health_503(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    with patch.object(b._api, "health", return_value=False):
        ok, detail = b.health()
    assert ok is False
    assert "failed" in detail


def test_bedrock_detect_ctx_size_known_model_summary_role(monkeypatch):
    """Summary role returns the raw per-model char budget.

    Main role subtracts ``_DEV_MODE_PREAMBLE_RESERVE_CHARS`` (plan § 10
    headroom reservation) — see ``test_bedrock_detect_ctx_size_main_reserves_preamble``.
    """
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "summary"}
    )
    # _MODEL_CONTEXT_CHARS returns char budget, not ctx tokens.
    assert b.detect_ctx_size() == 700000


def test_bedrock_detect_ctx_size_main_reserves_preamble(monkeypatch):
    """Main role subtracts preamble headroom (plan § 10).

    Dev-mode prepends ~1.5-2k tokens of tool manual + one-shot example to
    every main turn; without reserving that budget, the context packer
    would over-fill and hit "Input too long" at the gateway.
    """
    from llm_backend import _DEV_MODE_PREAMBLE_RESERVE_CHARS

    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
    )
    assert b.detect_ctx_size() == 700000 - _DEV_MODE_PREAMBLE_RESERVE_CHARS


def test_bedrock_detect_ctx_size_unknown_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": "unknown-model-xyz"})
    assert b.detect_ctx_size() is None


def test_bedrock_list_models_from_gateway(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    with patch.object(
        b._api, "list_models", return_value=["claude-v4.5-haiku", "claude-v4.5-sonnet"]
    ):
        models = b.list_models()
    assert models == ["claude-v4.5-haiku", "claude-v4.5-sonnet"]


def test_bedrock_list_models_fallback(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    with patch.object(b._api, "list_models", side_effect=Exception("boom")):
        models = b.list_models()
    assert "claude-v4.5-haiku" in models
    assert "claude-v4.5-sonnet" in models


# ── complete() ──


def _mock_assistant_msg(text: str) -> dict:
    """Construct a fake Bedrock assistant-message dict matching
    ``BedrockChatAPI.extract_text`` expectations."""
    return {
        "role": "assistant",
        "content": [{"contentType": "text", "body": text}],
    }


def test_bedrock_complete_returns_text(monkeypatch, caplog, tmp_path):
    # Isolate the spend file so this test doesn't leak.
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    monkeypatch.setattr(
        "llm_backend._SPEND_FILE", str(tmp_path / "spend.json")
    )
    b = BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "summary"}
    )
    with patch.object(
        b._api, "send_and_wait", return_value=_mock_assistant_msg("hello world")
    ):
        with caplog.at_level(logging.INFO, logger="llm_backend"):
            out = b.complete(prompt="hi there")
    assert out == "hello world"
    assert any(
        "backend.complete.latency_ms" in r.message
        and "backend=bedrock" in r.message
        and "ok=True" in r.message
        for r in caplog.records
    )


def test_bedrock_complete_logs_failure(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    monkeypatch.setattr(
        "llm_backend._SPEND_FILE", str(tmp_path / "spend.json")
    )
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    with patch.object(b._api, "send_and_wait", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.INFO, logger="llm_backend"):
            with pytest.raises(RuntimeError):
                b.complete(prompt="hi")
    assert any(
        "backend.complete.latency_ms" in r.message and "ok=False" in r.message
        for r in caplog.records
    )


# ── _call_with_retry regression (run 141 fix) ──

def test_call_with_retry_catches_builtin_TimeoutError(monkeypatch, caplog):
    """bedrock_api.poll() raises the Python built-in ``TimeoutError`` (NOT
    ``requests.exceptions.Timeout``). Run 141 crashed because the retry
    wrapper only caught the requests-flavored exception. Regression guard:
    ensure the wrapper also catches the built-in, retries, and eventually
    re-raises.
    """
    import logging

    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main",
         "max_retries": 2, "retry_base_delay_seconds": 0.01,
         "retry_backoff": 1.0}
    )
    call_count = {"n": 0}

    def _always_timeout(*a, **kw):
        call_count["n"] += 1
        raise TimeoutError("No response after 180s")

    log = logging.getLogger("test_timeout_retry")
    with caplog.at_level(logging.WARNING, logger="test_timeout_retry"):
        with pytest.raises(TimeoutError):
            b._call_with_retry(_always_timeout, _log=log)

    # 1 initial + 2 retries = 3 attempts
    assert call_count["n"] == 3
    # Must have emitted backend.retry.attempted before re-raising
    assert any(
        "backend.retry.attempted" in r.message and "backend=bedrock" in r.message
        for r in caplog.records
    )


def test_call_with_retry_recovers_after_TimeoutError(monkeypatch):
    """TimeoutError on first attempt, success on second → returns the value."""
    import logging

    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main",
         "max_retries": 3, "retry_base_delay_seconds": 0.01,
         "retry_backoff": 1.0}
    )
    calls = {"n": 0}

    def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("transient")
        return ("ok", "conv-abc")

    result = b._call_with_retry(_flaky, _log=logging.getLogger("test_recover"))
    assert result == ("ok", "conv-abc")
    assert calls["n"] == 2


# ── /token-usage startup logging (issue #355) ──


def _make_bedrock_backend(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    return BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
    )


def test_token_usage_logs_info_below_threshold(monkeypatch, caplog):
    """_log_token_usage emits INFO when monthly usage is under 90%."""
    # Patch get_token_usage on the API instance — ctor will still call
    # the real one once during __init__; patch before a second invocation
    # to observe the log on demand.
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    with patch.object(
        __import__("bedrock_api").BedrockChatAPI, "get_token_usage",
        return_value={"input_tokens": 300_000, "output_tokens": 0,
                      "total_tokens": 300_000, "token_limit": 1_000_000},
    ):
        b = BedrockBackend(
            {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
        )
        with caplog.at_level(logging.INFO, logger="agent"):
            b._log_token_usage()
    records = [r for r in caplog.records
               if "bedrock.token_usage" in r.message
               and "probe_failed" not in r.message]
    assert len(records) >= 1
    assert any(r.levelno == logging.INFO for r in records)
    assert any("monthly_total=300000/1000000" in r.message for r in records)
    assert any("used_pct=30.0" in r.message for r in records)


def test_token_usage_logs_warning_at_or_above_threshold(monkeypatch, caplog):
    """_log_token_usage escalates to WARNING when usage crosses 90%."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    with patch.object(
        __import__("bedrock_api").BedrockChatAPI, "get_token_usage",
        return_value={"input_tokens": 950_000, "output_tokens": 0,
                      "total_tokens": 950_000, "token_limit": 1_000_000},
    ):
        b = BedrockBackend(
            {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
        )
        with caplog.at_level(logging.WARNING, logger="agent"):
            b._log_token_usage()
    records = [r for r in caplog.records
               if "bedrock.token_usage" in r.message
               and "probe_failed" not in r.message]
    assert any(r.levelno == logging.WARNING for r in records)


def test_token_usage_probe_failure_logs_warning(monkeypatch, caplog):
    """_log_token_usage emits probe_failed when get_token_usage returns None."""
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    with patch.object(
        __import__("bedrock_api").BedrockChatAPI, "get_token_usage",
        return_value=None,
    ):
        b = BedrockBackend(
            {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
        )
        with caplog.at_level(logging.WARNING, logger="agent"):
            b._log_token_usage()
    assert any(
        "bedrock.token_usage.probe_failed" in r.message
        and r.levelno == logging.WARNING
        for r in caplog.records
    )


def test_get_token_usage_returns_dict_on_200(monkeypatch):
    """BedrockChatAPI.get_token_usage returns the parsed dict on HTTP 200."""
    from bedrock_api import BedrockChatAPI
    api = BedrockChatAPI({"api_url": "https://g.example.com/api",
                          "api_key": "k" * 40, "model": "claude-v4.5-haiku"})
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"input_tokens": 1, "output_tokens": 2,
                               "total_tokens": 3, "token_limit": 100}
    with patch.object(api.session, "get", return_value=resp) as g:
        out = api.get_token_usage()
    assert out == {"input_tokens": 1, "output_tokens": 2,
                   "total_tokens": 3, "token_limit": 100}
    g.assert_called_once_with(
        "https://g.example.com/api/token-usage", timeout=10)


def test_get_token_usage_returns_none_on_non_200(monkeypatch):
    """BedrockChatAPI.get_token_usage returns None on non-200 response."""
    from bedrock_api import BedrockChatAPI
    api = BedrockChatAPI({"api_url": "https://g.example.com/api",
                          "api_key": "k" * 40, "model": "claude-v4.5-haiku"})
    resp = MagicMock()
    resp.status_code = 500
    with patch.object(api.session, "get", return_value=resp):
        out = api.get_token_usage()
    assert out is None


def test_get_token_usage_returns_none_on_exception(monkeypatch):
    """BedrockChatAPI.get_token_usage returns None when the GET raises."""
    from bedrock_api import BedrockChatAPI
    api = BedrockChatAPI({"api_url": "https://g.example.com/api",
                          "api_key": "k" * 40, "model": "claude-v4.5-haiku"})
    with patch.object(api.session, "get", side_effect=RuntimeError("boom")):
        out = api.get_token_usage()
    assert out is None


# ── Conversation reuse (issue #356 / cycle 358) ──


def _mk_bedrock(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    return BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
    )


def test_conv_reuse_initial_state(monkeypatch):
    """BedrockBackend starts with no active conversation and zero count."""
    b = _mk_bedrock(monkeypatch)
    assert b._active_conv_id is None
    assert b._session_conv_count == 0


def test_conv_reuse_first_call_creates_new(monkeypatch, tmp_path):
    """First stream_chat call passes conversation_id=None (creates a new
    conversation server-side) and increments session_conv_count."""
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    b = _mk_bedrock(monkeypatch)
    # Mock the API layer so send_and_wait_conv returns a deterministic
    # (msg, conv_id) tuple without hitting the network.
    fake_msg = {"role": "assistant",
                "content": [{"contentType": "text", "body": "hi"}]}
    sent_kwargs = {}

    def _fake_send(prompt_text, conversation_id=None, cancel_check=None):
        sent_kwargs["conversation_id"] = conversation_id
        return fake_msg, "conv-NEW-123"

    with patch.object(b._api, "send_and_wait_conv", side_effect=_fake_send):
        b.stream_chat(logging.getLogger("test"),
                      json={"messages": [{"role": "user", "content": "hi"}]})
    assert sent_kwargs["conversation_id"] is None
    assert b._active_conv_id == "conv-NEW-123"
    assert b._session_conv_count == 1


def test_conv_reuse_second_call_uses_cached_id(monkeypatch, tmp_path):
    """Second stream_chat call passes the cached conversation_id — the
    server keeps context and no new conversation record is created."""
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    b = _mk_bedrock(monkeypatch)
    fake_msg = {"role": "assistant",
                "content": [{"contentType": "text", "body": "ok"}]}
    calls = []

    def _fake_send(prompt_text, conversation_id=None, cancel_check=None):
        calls.append(conversation_id)
        # Gateway returns a stable conv_id on continuation.
        return fake_msg, "conv-STABLE-1"

    with patch.object(b._api, "send_and_wait_conv", side_effect=_fake_send):
        b.stream_chat(logging.getLogger("test"),
                      json={"messages": [{"role": "user", "content": "first"}]})
        b.stream_chat(logging.getLogger("test"),
                      json={"messages": [{"role": "user", "content": "second"}]})
    # First call: conversation_id=None → create. Second call: reuse.
    assert calls[0] is None
    assert calls[1] == "conv-STABLE-1"
    # Only one distinct conversation opened server-side.
    assert b._session_conv_count == 1
