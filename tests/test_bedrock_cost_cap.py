"""Cost-counter / daily-cap guardrail tests for BedrockBackend.

Per plan § 6.5 / task 2.4. Exercises:
- spend-file round-trip (write/read, mode 0o600)
- cap enforcement (seeded spend + call that pushes over cap raises
  ``BedrockBudgetExceeded``)
- env override (BEDROCK_DAILY_CAP_USD)
- unknown model → WARN log + cost 0, no crash
"""

import json
import logging
import os
import stat
from datetime import date
from unittest.mock import patch

import pytest

from llm_backend import (
    BedrockBackend,
    BedrockBudgetExceeded,
    _estimate_cost,
    _load_today_spend,
    _record_spend,
    _resolve_daily_cap,
)


def _mock_msg(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"contentType": "text", "body": text}],
    }


def _setup_backend(monkeypatch, tmp_path, role="main", model="claude-v4.5-haiku"):
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    return BedrockBackend({"kind": "bedrock", "model": model, "role": role})


# ── Persistence round-trip ──


def test_spend_file_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    assert _load_today_spend("main") == 0.0
    new_total = _record_spend("main", 1.25)
    assert new_total == pytest.approx(1.25)
    assert _load_today_spend("main") == pytest.approx(1.25)
    _record_spend("main", 0.5)
    assert _load_today_spend("main") == pytest.approx(1.75)


def test_spend_file_mode_is_0o600(monkeypatch, tmp_path):
    spend = tmp_path / "spend.json"
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(spend))
    _record_spend("main", 0.01)
    mode = stat.S_IMODE(os.stat(spend).st_mode)
    assert mode == 0o600


def test_spend_file_format(monkeypatch, tmp_path):
    spend = tmp_path / "spend.json"
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(spend))
    _record_spend("main", 1.23)
    _record_spend("summary", 0.05)
    raw = spend.read_text()
    data = json.loads(raw)
    today = date.today().isoformat()
    assert data[today]["main"] == pytest.approx(1.23)
    assert data[today]["summary"] == pytest.approx(0.05)


# ── Cap resolution ──


def test_resolve_cap_from_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_DAILY_CAP_USD", "2.50")
    assert _resolve_daily_cap({}, "main") == 2.50


def test_resolve_cap_from_config_scalar(monkeypatch):
    monkeypatch.delenv("BEDROCK_DAILY_CAP_USD", raising=False)
    assert _resolve_daily_cap({"daily_cost_cap_usd": 5.0}, "main") == 5.0


def test_resolve_cap_from_config_dict(monkeypatch):
    monkeypatch.delenv("BEDROCK_DAILY_CAP_USD", raising=False)
    assert _resolve_daily_cap(
        {"daily_cost_cap_usd": {"main": 7.5, "summary": 0.25}}, "summary"
    ) == 0.25


def test_resolve_cap_defaults(monkeypatch):
    monkeypatch.delenv("BEDROCK_DAILY_CAP_USD", raising=False)
    assert _resolve_daily_cap({}, "main") == 60.00
    assert _resolve_daily_cap({}, "summary") == 1.00


# ── Cost estimation ──


def test_estimate_cost_known_model():
    # Haiku (AWS list): $1.00/M in, $5.00/M out.
    cost = _estimate_cost("main", 1_000_000, 1_000_000, "claude-v4.5-haiku")
    assert cost == pytest.approx(6.00)


def test_estimate_cost_unknown_model_warns_and_returns_zero(caplog):
    with caplog.at_level(logging.WARNING, logger="llm_backend"):
        cost = _estimate_cost("main", 1000, 1000, "made-up-model-xyz")
    assert cost == 0.0
    assert any("bedrock.cost.unknown_model" in r.message for r in caplog.records)


# ── Cap enforcement ──


def test_cap_breach_raises_budget_exceeded(monkeypatch, tmp_path):
    spend = tmp_path / "spend.json"
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(spend))
    # Seed spend close to the $60 main cap (bumped from $30 in b15c6c5).
    today = date.today().isoformat()
    spend.parent.mkdir(parents=True, exist_ok=True)
    spend.write_text(json.dumps({today: {"main": 59.99}}))

    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    # Force a large cost so the next call pushes over the cap.
    big_text = "x" * 10_000_000
    b = BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-sonnet", "role": "main"}
    )
    with patch.object(
        b._api, "send_and_wait", return_value=_mock_msg(big_text)
    ):
        with pytest.raises(BedrockBudgetExceeded):
            b.complete(prompt=big_text)


def test_env_override_cap_tight_breaches_immediately(monkeypatch, tmp_path):
    spend = tmp_path / "spend.json"
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(spend))
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    monkeypatch.setenv("BEDROCK_DAILY_CAP_USD", "0.000001")
    b = BedrockBackend(
        {"kind": "bedrock", "model": "claude-v4.5-haiku", "role": "main"}
    )
    with patch.object(
        b._api, "send_and_wait", return_value=_mock_msg("response text long enough")
    ):
        with pytest.raises(BedrockBudgetExceeded):
            b.complete(prompt="a fairly long prompt so cost > 0")


def test_unknown_model_no_crash(monkeypatch, tmp_path, caplog):
    b = _setup_backend(monkeypatch, tmp_path, model="unknown-model")
    with patch.object(
        b._api, "send_and_wait", return_value=_mock_msg("hi")
    ):
        with caplog.at_level(logging.WARNING, logger="llm_backend"):
            out = b.complete(prompt="hello")
    assert out == "hi"
    assert any("bedrock.cost.unknown_model" in r.message for r in caplog.records)


# ── cost.tick DEBUG log ──


def test_cost_tick_logged(monkeypatch, tmp_path, caplog):
    b = _setup_backend(monkeypatch, tmp_path)
    with patch.object(
        b._api, "send_and_wait", return_value=_mock_msg("hello")
    ):
        with caplog.at_level(logging.DEBUG, logger="llm_backend"):
            b.complete(prompt="hi there")
    assert any(
        "bedrock.cost.tick" in r.message and "role=main" in r.message
        for r in caplog.records
    )
