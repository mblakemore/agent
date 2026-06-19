"""Banner/output should label AWS gateways as [aws] and never print endpoint
URLs (private gateway or localhost) — only the kind, model, and config path.
"""

import os

import agent
import callbacks


# ── _display_backend_kind ───────────────────────────────────────────────────

def test_display_kind_aws_gateway():
    url = "https://qtyh48quh0.execute-api.us-east-1.amazonaws.com"
    assert agent._display_backend_kind("llamacpp", url) == "aws"


def test_display_kind_local_keeps_llamacpp():
    assert agent._display_backend_kind("llamacpp", "http://127.0.0.1:8080") == "llamacpp"


def test_display_kind_passthrough_when_no_url():
    assert agent._display_backend_kind("bedrock", "") == "bedrock"
    assert agent._display_backend_kind("", None) == ""


# ── banner rendering ────────────────────────────────────────────────────────

def _render_banner(monkeypatch, info):
    cb = callbacks.TerminalCallbacks()
    lines = []
    monkeypatch.setattr(cb, "_print", lambda text="", end="\n": lines.append(text))
    cb.on_session_start(info)
    return "\n".join(lines)


def test_banner_hides_urls_and_shows_aws(monkeypatch):
    url = "https://qtyh48quh0.execute-api.us-east-1.amazonaws.com"
    blob = _render_banner(monkeypatch, {
        "version": "0.1.0", "sha": "abc",
        "api_ok": True, "api_detail": "ok",
        "base_url": url,
        "model": "claude-v4.6-sonnet", "main_kind": "aws",
        "summary_enabled": True, "summary_ok": True,
        "summary_base_url": url, "summary_model": "claude-v4.6-sonnet",
        "summary_kind": "aws",
    })
    assert "amazonaws.com" not in blob
    assert "execute-api" not in blob
    assert "[aws]" in blob
    assert "claude-v4.6-sonnet" in blob


def test_banner_hides_localhost(monkeypatch):
    blob = _render_banner(monkeypatch, {
        "version": "0.1.0", "sha": "abc",
        "api_ok": True, "api_detail": "ok",
        "base_url": "http://127.0.0.1:8080",
        "model": "gemma-4-31B", "main_kind": "llamacpp",
        "summary_enabled": False,
    })
    assert "127.0.0.1" not in blob
    assert "[llamacpp]" in blob
    assert "gemma-4-31B" in blob
