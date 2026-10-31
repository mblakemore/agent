"""Collapsing the window after an idle gap that outlived the cache.

Measured 2026-10-30 on AI Commons (kimi-k3, 1443 requests): 17 wakes after the
45-minute cycle gap each re-sent the whole ~90k-token window uncached, at 21 times
the cached rate, for about 32M weighted tokens in a day. When the lifetime record
says the prefix is dead, the window is rebuilt small behind a summary generated
synchronously, so the wake costs one small miss instead of a large one plus the
harvest miss a turn later.
"""
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

import agent

log = logging.getLogger("test_cache_collapse")


@pytest.fixture
def uncalibrated(monkeypatch):
    monkeypatch.setattr(agent, "_TOKEN_CAL", {"ratio": 1.0, "n": 0, "last_logged": 1.0})
    monkeypatch.setattr(agent, "_TOOLS_TOKENS", 0)


def _hist(n):
    out = [{"role": "user", "content": "task"}]
    for i in range(n):
        out.append({"role": "assistant", "content": f"step {i}"} if i % 2 == 0
                   else {"role": "user", "content": f"result {i}"})
    return out


class TestDue:
    def test_off_without_a_fraction_or_a_gap(self):
        assert not agent._cache_collapse_due(3000, 1200, 0, 600, 388)
        assert not agent._cache_collapse_due(None, 1200, 0.15, 600, 388)

    def test_not_due_inside_the_proven_lifetime(self):
        assert not agent._cache_collapse_due(1000, 1200, 0.15, 600, 388)

    def test_not_worth_it_for_a_window_already_small(self):
        assert not agent._cache_collapse_due(3000, 1200, 0.15, 80, 388)

    def test_due_when_the_prefix_is_dead_and_the_window_large(self):
        assert agent._cache_collapse_due(3000, 1200, 0.15, 600, 388)

    def test_the_fraction_comes_from_config(self, monkeypatch):
        monkeypatch.setitem(agent._config["context"], "cache_collapse_frac", 0.2)
        assert agent._cache_collapse_frac() == 0.2
        monkeypatch.setitem(agent._config["context"], "cache_collapse_frac", "bad")
        assert agent._cache_collapse_frac() == 0.0


class TestWindowBuilder:
    """ctx 1000, max_tokens 100 → budget 388; messages cost 10 → a window of 30
    messages (300) fits on its kept head, far over the collapsed size (58)."""

    def _setup(self, monkeypatch, gap):
        monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 100)
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons", model="moonshotai-kimi-k3"))
        monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 10)
        monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - gap})
        h = _hist(30)
        state = {"text": "", "up_to": 0, "_cache_head": 1}
        return h, state

    def test_a_dead_prefix_rebuilds_the_window_small(self, monkeypatch, uncalibrated):
        h, state = self._setup(monkeypatch, gap=3000)       # kimi is seeded at 1500 s
        lg = MagicMock()
        sel, oldest = agent._build_context(h, state, "", 1000, 100, lg)
        assert oldest > 20 and len(sel) <= 6, (oldest, len(sel))
        assert state["_collapsed"] is True and state["_collapse_summarize"] is True
        assert state["_head_moved"] is True and state["_cache_head"] == oldest
        assert any("cache collapse" in str(c.args[0]) for c in lg.info.call_args_list)

    def test_a_live_prefix_keeps_its_head(self, monkeypatch, uncalibrated):
        h, state = self._setup(monkeypatch, gap=600)
        sel, oldest = agent._build_context(h, state, "", 1000, 100, log)
        assert oldest == 1 and len(sel) == 30
        assert "_collapsed" not in state

    def test_the_record_can_lengthen_the_lifetime(self, monkeypatch, uncalibrated):
        h, state = self._setup(monkeypatch, gap=3000)
        agent._cache_lifetime_observe(agent._CACHE_LIFETIMES, "aicommons:moonshotai-kimi-k3", 3300, True)
        _, oldest = agent._build_context(h, state, "", 1000, 100, log)
        assert oldest == 1 and "_collapsed" not in state

    def test_disabled_by_config(self, monkeypatch, uncalibrated):
        monkeypatch.setitem(agent._config["context"], "cache_collapse_frac", 0)
        h, state = self._setup(monkeypatch, gap=3000)
        _, oldest = agent._build_context(h, state, "", 1000, 100, log)
        assert oldest == 1 and "_collapsed" not in state

    def test_a_sliding_window_backend_never_collapses(self, monkeypatch, uncalibrated):
        h, state = self._setup(monkeypatch, gap=3000)
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="llamacpp", model="m"))
        agent._build_context(h, state, "", 1000, 100, log)
        assert "_collapsed" not in state


# ── the run loop folds a synchronous summary into the collapsed request ──────

def _turn_with_usage(tid, read, uncached):
    call = {"index": 0, "id": tid, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": read + uncached, "completion_tokens": 5,
                       "cache_read_input_tokens": read, "cache_creation_input_tokens": 0,
                       "input_tokens": uncached}}]


def _collapsed_run(agent_loop_mocks, monkeypatch, summarizer=None):
    monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 100)
    monkeypatch.setattr(agent, "_TOKEN_CAL", {"ratio": 1.0, "n": 0, "last_logged": 1.0})
    monkeypatch.setattr(agent, "_TOOLS_TOKENS", 0)
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model="moonshotai-kimi-k3", honours_system=True, base_url="http://x"))
    monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 10)
    monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - 3000})
    agent_loop_mocks.stub_tool("read_file", "contents")
    sent = []
    real = agent_loop_mocks._fake_llm_request

    def capture(log, **kw):
        sent.append(kw["json"]["messages"])
        return real(log, **kw)
    monkeypatch.setattr(agent, "_llm_request", capture)
    agent_loop_mocks.script(_turn_with_usage("t1", 0, 100))
    h = _hist(30)
    h.append({"role": "user", "content": "wake up"})
    state = {"text": "old summary", "up_to": 0, "_cache_head": 1}
    lg = MagicMock()
    with patch("agent._generate_summary", return_value="NEW SUMMARY of the idle gap") as gen, \
         patch("agent._save_checkpoint"):
        agent.run_agent_single(h, state, [], lg, max_tokens=100, ctx_size=1000, async_summarizer=summarizer)
    lines = [c.args[0] % c.args[1:] for c in lg.info.call_args_list if c.args]
    return sent, state, lines, gen


def test_the_wake_request_carries_a_fresh_summary_and_a_small_window(agent_loop_mocks, monkeypatch):
    sent, state, lines, gen = _collapsed_run(agent_loop_mocks, monkeypatch)
    assert gen.called, "the summary of what the collapse evicted is generated before sending"
    first = sent[0]
    lead = next(m for m in first if m["role"] == "user" and "Progress summary" in str(m["content"]))
    assert "NEW SUMMARY of the idle gap" in lead["content"]
    assert state["up_to"] > 20 and state["text"].startswith("NEW SUMMARY")
    assert len(first) < 15, len(first)
    assert any("cache.miss" in l and "reason=collapse" in l for l in lines), lines
    assert any(l.startswith("cache collapse: summary") for l in lines), lines


def test_a_running_async_summary_is_left_to_the_next_harvest(agent_loop_mocks, monkeypatch):
    summarizer = MagicMock()
    summarizer.is_running = True
    summarizer.harvest.return_value = False
    sent, state, lines, gen = _collapsed_run(agent_loop_mocks, monkeypatch, summarizer=summarizer)
    assert not gen.called
    assert state["text"] == "old summary"
    assert any("already running" in l for l in lines), lines
