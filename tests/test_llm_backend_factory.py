"""Tests for llm_backend.build_backend and the LlamacppBackend wrapper.

Per plan/bedrock-integration.md § 13.2 and § 20 task 1.2.
"""

import logging
import pytest
from unittest.mock import MagicMock, patch

from llm_backend import (
    Backend,
    LlamacppBackend,
    build_backend,
    ContextOverflowError,
)


# ── Factory dispatch ──


def test_build_backend_llamacpp():
    cfg = {"kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "gemma-4"}
    b = build_backend(cfg)
    assert isinstance(b, LlamacppBackend)
    assert b.kind == "llamacpp"
    assert b.base_url == "http://127.0.0.1:8080"
    assert b.model == "gemma-4"


def test_build_backend_bedrock_not_implemented():
    with pytest.raises(NotImplementedError) as exc:
        build_backend({"kind": "bedrock"})
    assert "Phase 2" in str(exc.value)


def test_build_backend_unknown_kind():
    with pytest.raises(ValueError) as exc:
        build_backend({"kind": "wat"})
    assert "Unknown backend kind" in str(exc.value)


def test_build_backend_default_kind_llamacpp():
    # Missing `kind` should default to llamacpp (back-compat shim).
    b = build_backend({"base_url": "http://x:1", "model": "m"})
    assert isinstance(b, LlamacppBackend)


# ── LlamacppBackend core methods ──


def test_llamacpp_health_ok():
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        ok, detail = b.health()
    assert ok is True
    assert detail == "ok"


def test_llamacpp_health_http_error():
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value.status_code = 503
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        ok, detail = b.health()
    assert ok is False
    assert "503" in detail


def test_llamacpp_detect_ctx_size():
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{"n_ctx": 65536}]
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        assert b.detect_ctx_size() == 65536


def test_llamacpp_detect_ctx_size_non_200():
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        assert b.detect_ctx_size() is None


def test_llamacpp_list_models():
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "data": [{"id": "gemma-4"}, {"id": "gpt-oss"}, {"id": ""}]
        }
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        assert b.list_models() == ["gemma-4", "gpt-oss"]


def test_llamacpp_stream_chat_success(caplog):
    with patch("llm_backend.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        b = LlamacppBackend({"base_url": "http://x", "model": "gemma-4"})
        log = logging.getLogger("test_stream_chat")
        with caplog.at_level(logging.INFO, logger="test_stream_chat"):
            resp = b.stream_chat(log, json={"messages": []})
        assert resp.status_code == 200
        assert mock_post.call_count == 1

    # Telemetry log line fires (task 1.6).
    assert any(
        "backend.stream_chat.latency_ms" in rec.getMessage() for rec in caplog.records
    )


def test_llamacpp_stream_chat_consecutive_500_raises_overflow():
    import requests
    with patch("llm_backend.requests.post") as mock_post:
        mock_post.return_value.status_code = 500
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        log = logging.getLogger("test_overflow")
        with patch("llm_backend.time.sleep"):
            with pytest.raises(ContextOverflowError):
                b.stream_chat(log, json={})
        # 3 consecutive 500s trigger overflow
        assert mock_post.call_count == 3


def test_llamacpp_complete(caplog):
    with patch("llm_backend.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "choices": [{"message": {"content": " hello world "}}]
        }
        b = LlamacppBackend({"base_url": "http://x", "model": "m"})
        with caplog.at_level(logging.INFO, logger="llm_backend"):
            result = b.complete(prompt="hi")
    assert result == "hello world"
    # Telemetry log line fires (task 1.6).
    assert any(
        "backend.complete.latency_ms" in rec.getMessage() for rec in caplog.records
    )
