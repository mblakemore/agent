"""Regression tests for the proactive monthly quota check (#864).

BedrockBackend.complete() and stream_chat() now raise BedrockBudgetExceeded
immediately when the cached monthly usage is >= 100%, instead of attempting
the call and waiting 180s for the gateway's 404-forever silent failure.

The conftest mock_bedrock_token_usage fixture stubs _log_token_usage as a
no-op for this file, leaving _cached_usage_pct = 0.0 after __init__. Each
test that needs to simulate quota exhaustion sets _cached_usage_pct directly.
"""

import time
import pytest
from unittest.mock import patch, MagicMock

from llm_backend import BedrockBackend, BedrockBudgetExceeded


def _make_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    return BedrockBackend({"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "summary"})


def _mock_msg(text: str) -> dict:
    return {"role": "assistant", "content": [{"contentType": "text", "body": text}]}


def _exhaust_quota(b: BedrockBackend, pct: float = 106.4) -> None:
    """Set the backend's cached quota to a >100% value with a fresh timestamp."""
    b._cached_usage_pct = pct
    b._usage_cache_time = time.monotonic()


# ── complete() ────────────────────────────────────────────────────────────────

def test_complete_raises_when_quota_exceeded(monkeypatch, tmp_path):
    """complete() raises BedrockBudgetExceeded immediately when quota >= 100%. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    _exhaust_quota(b)
    with pytest.raises(BedrockBudgetExceeded) as exc_info:
        b.complete(prompt="hi")
    assert "quota" in str(exc_info.value).lower() or "100" in str(exc_info.value)


def test_complete_raises_contains_used_pct(monkeypatch, tmp_path):
    """BedrockBudgetExceeded message from complete() must include the usage percentage. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    _exhaust_quota(b, pct=106.4)
    with pytest.raises(BedrockBudgetExceeded) as exc_info:
        b.complete(prompt="hi")
    assert "106.4" in str(exc_info.value), f"pct not in message: {exc_info.value}"


def test_complete_proceeds_when_quota_safe(monkeypatch, tmp_path):
    """complete() proceeds normally when cached usage is below 100%. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    # _cached_usage_pct is 0.0 (safe) after __init__ with the no-op stub
    with patch.object(b._api, "send_and_wait", return_value=_mock_msg("ok")):
        result = b.complete(prompt="hi")
    assert result == "ok"


def test_complete_proceeds_at_99_pct(monkeypatch, tmp_path):
    """complete() must NOT raise at 99% — only at exactly 100% or above. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    b._cached_usage_pct = 99.9
    b._usage_cache_time = time.monotonic()
    with patch.object(b._api, "send_and_wait", return_value=_mock_msg("ok")):
        result = b.complete(prompt="hi")
    assert result == "ok"


def test_complete_raises_at_exactly_100_pct(monkeypatch, tmp_path):
    """complete() raises at exactly 100.0%. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    b._cached_usage_pct = 100.0
    b._usage_cache_time = time.monotonic()
    with pytest.raises(BedrockBudgetExceeded):
        b.complete(prompt="hi")


# ── stream_chat() ─────────────────────────────────────────────────────────────

def test_stream_chat_raises_when_quota_exceeded(monkeypatch, tmp_path):
    """stream_chat() raises BedrockBudgetExceeded immediately when quota >= 100%. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    _exhaust_quota(b)
    msgs = [{"role": "user", "content": "hello"}]
    with pytest.raises(BedrockBudgetExceeded):
        list(b.stream_chat(messages=msgs))


def test_stream_chat_proceeds_when_quota_safe(monkeypatch, tmp_path):
    """stream_chat() proceeds normally when cached usage is below 100%. (#864)"""
    from dev_mode_prompt import parse_dev_response
    b = _make_backend(monkeypatch, tmp_path)
    # Simulate a minimal successful stream_chat response
    fake_msg = _mock_msg("hello")
    fake_msg_with_id = (fake_msg, "conv-abc")
    with patch.object(b._api, "send_and_wait_conv", return_value=fake_msg_with_id), \
         patch.object(b._api, "extract_text", return_value="hello"):
        # stream_chat returns a generator of delta dicts; just ensure no exception
        result = b.stream_chat(messages=[{"role": "user", "content": "hi"}])
        # Consume the result
        deltas = list(result)
    assert isinstance(deltas, list)


# ── _call_with_retry timeout → quota re-check ────────────────────────────────

def test_call_with_retry_final_timeout_reraises_as_budget_exceeded_when_quota_over(
    monkeypatch, tmp_path
):
    """Final TimeoutError converts to BedrockBudgetExceeded when quota is >100%. (#864)"""
    import logging
    b = _make_backend(monkeypatch, tmp_path)

    def _timeout(*a, **kw):
        raise TimeoutError("No response after 180s")

    # Simulate quota becoming exhausted just as the timeout fires
    with patch.object(b, "_log_token_usage", side_effect=lambda: _exhaust_quota(b)):
        b._usage_cache_time = 0.0  # force cache stale so re-check fires
        log = logging.getLogger("test_quota_retry")
        with pytest.raises(BedrockBudgetExceeded):
            b._call_with_retry(
                _timeout,
                _log=log,
                # max_retries=0 so we hit the final-attempt branch immediately
                # (we must pass cfg-override since _call_with_retry reads from self._cfg)
            )


def test_call_with_retry_final_timeout_reraises_as_timeout_when_quota_ok(
    monkeypatch, tmp_path
):
    """Final TimeoutError is re-raised as-is when quota is safe. (#864)"""
    import logging
    b = _make_backend(monkeypatch, tmp_path)
    b._cfg = {**b._cfg, "max_retries": 0}

    def _timeout(*a, **kw):
        raise TimeoutError("No response after 180s")

    log = logging.getLogger("test_quota_retry_safe")
    with pytest.raises(TimeoutError):
        b._call_with_retry(_timeout, _log=log)


# ── _get_cached_usage_pct() ───────────────────────────────────────────────────

def test_get_cached_usage_pct_returns_cached_when_fresh(monkeypatch, tmp_path):
    """_get_cached_usage_pct returns cached value without calling _log_token_usage
    when cache is fresh. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    b._cached_usage_pct = 55.0
    b._usage_cache_time = time.monotonic()  # fresh

    called = {"n": 0}
    original = b._log_token_usage

    def _counting_log():
        called["n"] += 1
        original()

    b._log_token_usage = _counting_log
    result = b._get_cached_usage_pct()
    assert result == 55.0
    assert called["n"] == 0, "Should not have called _log_token_usage for a fresh cache"


def test_get_cached_usage_pct_refreshes_stale_cache(monkeypatch, tmp_path):
    """_get_cached_usage_pct calls _log_token_usage when cache is stale. (#864)"""
    b = _make_backend(monkeypatch, tmp_path)
    b._cached_usage_pct = 55.0
    b._usage_cache_time = 0.0  # force stale

    called = {"n": 0}

    def _fake_log():
        called["n"] += 1
        b._cached_usage_pct = 20.0
        b._usage_cache_time = time.monotonic()

    b._log_token_usage = _fake_log
    result = b._get_cached_usage_pct()
    assert called["n"] == 1, "Should have refreshed the stale cache"
    assert result == 20.0
