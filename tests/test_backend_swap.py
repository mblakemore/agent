"""Backend-swap round-trip tests (plan task 2.7 / § 13.2).

Exercises the three non-default combinations:
  - main=bedrock, summary=llamacpp
  - main=llamacpp, summary=bedrock
  - main=bedrock, summary=bedrock

In each case the test monkeypatches the module-level ``_main_backend`` /
``_summary_backend`` globals and verifies routing: ``_llm_request`` /
``_summary_request`` dispatch to the correct backend and the correct
backend's ``stream_chat`` / ``complete`` method gets called.

No real network — both backends are MagicMock-wrapped.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

import agent
from llm_backend import BedrockBackend, LlamacppBackend


# ── Helpers ──


def _fake_bedrock(monkeypatch, role="main", model="claude-v4.5-haiku"):
    """Build a BedrockBackend with a mocked BedrockChatAPI session so no
    real network is touched.
    """
    monkeypatch.setenv("BEDROCK_API_URL", "https://g.example.com/api")
    monkeypatch.setenv("BEDROCK_API_KEY", "k" * 40)
    b = BedrockBackend({"kind": "bedrock", "model": model, "role": role})
    return b


def _fake_llamacpp(base_url="http://127.0.0.1:8080", model="gemma-4"):
    return LlamacppBackend({"kind": "llamacpp", "base_url": base_url, "model": model})


# ── Summary path routing ──


def test_summary_llamacpp_when_summary_backend_is_llamacpp(monkeypatch):
    fake_summary = _fake_llamacpp(base_url="http://127.0.0.1:8082", model="gemma-E4B")
    monkeypatch.setattr(agent, "_summary_backend", fake_summary)
    with patch.object(fake_summary, "complete", return_value="summ-text") as mk:
        out = agent._summary_request("prompt")
    assert out == "summ-text"
    mk.assert_called_once_with(prompt="prompt")


def test_summary_bedrock_when_summary_backend_is_bedrock(monkeypatch, tmp_path):
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    fake_summary = _fake_bedrock(monkeypatch, role="summary")
    monkeypatch.setattr(agent, "_summary_backend", fake_summary)
    with patch.object(fake_summary, "complete", return_value="bedrock-summary") as mk:
        out = agent._summary_request("prompt")
    assert out == "bedrock-summary"
    mk.assert_called_once_with(prompt="prompt")


# ── Main path routing ──


def test_main_llamacpp_routes_through_llamacpp_backend(monkeypatch):
    fake_main = _fake_llamacpp()
    monkeypatch.setattr(agent, "_main_backend", fake_main)
    with patch.object(fake_main, "stream_chat", return_value="resp") as mk:
        out = agent._llm_request(logging.getLogger("test"), json={"messages": []})
    assert out == "resp"
    mk.assert_called_once()


def test_main_bedrock_routes_through_bedrock_backend(monkeypatch, tmp_path):
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    fake_main = _fake_bedrock(monkeypatch, role="main")
    monkeypatch.setattr(agent, "_main_backend", fake_main)
    with patch.object(fake_main, "stream_chat", return_value="resp") as mk:
        out = agent._llm_request(logging.getLogger("test"), json={"messages": []})
    assert out == "resp"
    mk.assert_called_once()


# ── Both backends bedrock (different models) ──


def test_both_backends_bedrock_different_models(monkeypatch, tmp_path):
    monkeypatch.setattr("llm_backend._SPEND_FILE", str(tmp_path / "spend.json"))
    fake_main = _fake_bedrock(monkeypatch, role="main", model="claude-v4.5-sonnet")
    fake_summary = _fake_bedrock(monkeypatch, role="summary", model="claude-v4.5-haiku")
    monkeypatch.setattr(agent, "_main_backend", fake_main)
    monkeypatch.setattr(agent, "_summary_backend", fake_summary)

    with patch.object(fake_main, "stream_chat", return_value="m-resp") as mk_main:
        with patch.object(fake_summary, "complete", return_value="s-resp") as mk_sum:
            main_out = agent._llm_request(
                logging.getLogger("test"), json={"messages": []}
            )
            sum_out = agent._summary_request("prompt")

    assert main_out == "m-resp"
    assert sum_out == "s-resp"
    mk_main.assert_called_once()
    mk_sum.assert_called_once_with(prompt="prompt")
    assert fake_main.model != fake_summary.model
