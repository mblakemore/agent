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


def test_bedrock_detect_ctx_size_known_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku"})
    # _MODEL_CONTEXT_CHARS returns char budget, not ctx tokens.
    assert b.detect_ctx_size() == 700000


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
