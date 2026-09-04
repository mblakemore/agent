"""Prompt-cache-friendly history and a prefill-aware stall guard.

Measured on a live run: every turn re-processed ~32k of a 39k-token prompt because the client
rewrote an earlier tool result in place each turn (longest common prefix with the server's cache:
17%), prefill took ~60 s, and the 60 s zero-delta stall guard cancelled each request at the exact
moment prefill finished. Two fixes, each pinned here: history is rewritten only under context
pressure, in a batch; the stall budget covers the prefill a prompt of this size needs.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402


class TestStallBudget:
    def test_budget_covers_prefill_for_a_large_prompt(self):
        # 39,000 tokens at a 500 t/s floor need 78 s; a 60 s guard would cancel during prefill
        assert _agent._stall_budget_s(60, 39_000, 500) == pytest.approx(78.0)

    def test_small_prompt_keeps_the_base(self):
        assert _agent._stall_budget_s(60, 5_000, 500) == 60

    def test_zero_base_stays_disabled(self):
        assert _agent._stall_budget_s(0, 39_000, 500) == 0

    def test_unknown_estimate_or_floor_keeps_the_base(self):
        assert _agent._stall_budget_s(60, None, 500) == 60
        assert _agent._stall_budget_s(60, 39_000, 0) == 60


def _big(i):
    return f"result {i} " + ("x" * 400)


class TestCompressionIsPressureGated:
    def _history(self, n=4):
        h = [{"role": "user", "content": "go"}]
        for i in range(n):
            h.append({"role": "assistant", "content": "", "tool_calls": []})
            h.append({"role": "tool", "tool_call_id": f"t{i}", "name": "exec_command", "content": _big(i)})
        return h

    def test_below_pressure_nothing_is_rewritten(self):
        """The cache-killer: a rewrite near the top of the history every turn."""
        h = self._history()
        before = json.dumps(h)
        with patch("agent._summarize_for_compression", lambda c, n, log: "[compressed: stub]"):
            _agent._compress_repeated_tool_results(h, "exec_command", MagicMock(), pressure=0.30)
        assert json.dumps(h) == before

    def test_above_pressure_all_older_results_compress_in_one_pass(self):
        h = self._history()
        with patch("agent._summarize_for_compression", lambda c, n, log: "[compressed: stub]"):
            _agent._compress_repeated_tool_results(h, "exec_command", MagicMock(), pressure=0.95)
        tools = [m for m in h if m.get("role") == "tool"]
        assert all(m["content"].startswith("[compressed") for m in tools[:-1])
        assert tools[-1]["content"] == _big(3), "the most recent result stays intact"

    def test_pressure_is_estimated_against_the_context_size(self, monkeypatch):
        h = self._history()
        monkeypatch.setitem(_agent._config["context"], "ctx_size", 1_000_000)
        assert _agent._history_pressure(h) < 0.05
        monkeypatch.setitem(_agent._config["context"], "ctx_size", 100)
        assert _agent._history_pressure(h) > 1.0


# ── wiring: the call site passes the pressure ───────────────────────────────
def _resp(content=None, tool_calls=None):
    r = MagicMock()
    r.status_code = 200
    lines = []
    if tool_calls:
        for tc in tool_calls:
            lines.append(f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [tc]}}]})}".encode())
    elif content:
        lines.append(f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}".encode())
    lines.append(b"data: [DONE]")
    r.iter_lines.return_value = lines
    return r


def _exec(i):
    return {"index": 0, "id": f"e{i}", "function": {"name": "exec_command", "arguments": json.dumps({"command": f"echo {i}"})}}


@pytest.fixture
def ctx_cfg():
    snap = json.loads(json.dumps(_agent._config["context"]))
    yield _agent._config["context"]
    _agent._config["context"].clear()
    _agent._config["context"].update(snap)


class TestWiring:
    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_repeated_tool_results_are_not_rewritten_at_low_pressure(self, mock_llm, _emit, ctx_cfg):
        ctx_cfg["ctx_size"] = 1_000_000
        mock_llm.side_effect = [_resp(tool_calls=[_exec(i)]) for i in range(1, 6)] + [_resp(content="final")]
        history = [{"role": "user", "content": "x"}]
        calls = {"n": 0}

        def tool(**kw):
            calls["n"] += 1
            return _big(calls["n"])
        with patch("agent._NUDGE_ENABLED", False), patch("agent._summarize_for_compression", lambda c, n, log: "[compressed: stub]"), \
             patch.dict("agent.MAP_FN", {"exec_command": tool}):
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        tools = [m for m in history if m.get("role") == "tool"]
        assert len(tools) == 5
        assert not any(m["content"].startswith("[compressed") for m in tools), "no rewrite below pressure"

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_repeated_tool_results_compress_under_pressure(self, mock_llm, _emit, ctx_cfg):
        """Control: the pass still fires when the context is genuinely full."""
        ctx_cfg["ctx_size"] = 600
        mock_llm.side_effect = [_resp(tool_calls=[_exec(i)]) for i in range(1, 6)] + [_resp(content="final")]
        history = [{"role": "user", "content": "x"}]
        calls = {"n": 0}

        def tool(**kw):
            calls["n"] += 1
            return _big(calls["n"])
        with patch("agent._NUDGE_ENABLED", False), patch("agent._summarize_for_compression", lambda c, n, log: "[compressed: stub]"), \
             patch.dict("agent.MAP_FN", {"exec_command": tool}):
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        tools = [m for m in history if m.get("role") == "tool"]
        assert any(m["content"].startswith("[compressed") for m in tools), "the pass fires under pressure"
