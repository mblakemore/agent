"""Summaries that are ready before the window needs them (2026-11-12).

Measured on one deployment over six sessions before this: the synchronous summary
folded into an evicting or collapsing request blocked the main thread for 11 s at
the median (35 times, 16 s at most) and the condense pass for 13 s (21 times, 19 s
at most), in front of the request, with a person waiting on the other end.
"""
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import agent
from agent import AsyncSummarizer

log = logging.getLogger("test_summary_async")


def _hist(n):
    out = [{"role": "user", "content": "task"}]
    for i in range(n):
        out.append({"role": "assistant", "content": f"step {i}"} if i % 2 == 0
                   else {"role": "user", "content": f"result {i}"})
    return out


# ── 1. the condense pass runs on the worker, never on the main thread ────────

class TestCondenseOnTheWorker:
    def test_a_long_result_is_condensed_by_the_worker_thread(self, monkeypatch):
        monkeypatch.setattr(agent, "_SUMMARY_MAX_CHARS", 50)
        seen = {}

        def fake_request(prompt, **kw):
            if "too long" in prompt:
                seen["condense_thread"] = threading.current_thread().name
                return "short"
            return "x" * 200
        with patch("agent._summary_request", side_effect=fake_request), patch("agent._emit") as emit:
            s = AsyncSummarizer(agent._config, log)
            assert s.kick("", [{"role": "user", "content": "a"}], 3) is True
            s.drain()
            state = {"text": "", "up_to": 0}
            assert s.harvest(state) is True
        assert state["text"] == "short" and state["up_to"] == 3
        assert seen["condense_thread"] != threading.current_thread().name
        assert not any(c.args[0] == "on_summary_start" for c in emit.call_args_list), \
            "the worker condenses quietly: nothing on the main thread is waiting"

    def test_harvest_never_calls_the_model(self):
        s = AsyncSummarizer(agent._config, log)
        s.seed("y" * 10000, 4)
        with patch("agent._summary_request") as req, patch("agent._condense_summary") as cond:
            assert s.harvest({"text": "", "up_to": 0}) is True
        assert not req.called and not cond.called

    def test_kick_reports_whether_it_started(self):
        s = AsyncSummarizer(agent._config, log)
        gate = threading.Event()
        with patch("agent._summary_request", side_effect=lambda *a, **k: (gate.wait(5), "r")[1]):
            assert s.kick("", [], 1) is True
            assert s.kick("", [], 2) is False, "one worker at a time"
            gate.set()
            s.drain()
        assert s.pending_up_to() == 1


# ── the hold: a summary past the window's head waits for the head ────────────

class TestHarvestHold:
    def _ready(self, up_to, kind="ahead"):
        s = AsyncSummarizer(agent._config, log)
        s.seed("SUMMARY", up_to, kind)
        return s

    def test_a_summary_at_or_before_the_head_is_applied(self):
        state = {"text": "old", "up_to": 0, "_head": 10}
        assert self._ready(10).harvest(state) is True
        assert state["text"] == "SUMMARY" and state["up_to"] == 10 and state["_lead_changed"] is True
        assert "_pending" not in state

    def test_a_summary_past_the_head_is_held_and_rides_in_the_state(self):
        s = self._ready(14)
        state = {"text": "old", "up_to": 0, "_head": 10}
        assert s.harvest(state) is False
        assert state["text"] == "old" and state["up_to"] == 0
        assert state["_pending"] == {"text": "SUMMARY", "up_to": 14, "kind": "ahead"}
        assert s.pending_up_to() == 14
        state["_head"] = 14   # the eviction moved the head to the summary's edge
        assert s.harvest(state) is True
        assert state["up_to"] == 14 and "_pending" not in state and s.pending_up_to() is None

    def test_without_a_recorded_head_it_is_applied(self):
        state = {"text": "", "up_to": -1}
        assert self._ready(5).harvest(state) is True

    def test_the_harvest_log_line_names_the_kind(self):
        lg = MagicMock()
        s = AsyncSummarizer(agent._config, lg)
        s.seed("S", 3, "late")
        s.harvest({"text": "", "up_to": 0, "_head": 3})
        assert any(c.args[0].startswith("summary.harvest") and c.args[2] == "late" for c in lg.info.call_args_list)

    def test_the_window_builder_records_its_head(self, monkeypatch):
        monkeypatch.setattr(agent, "_TOKEN_CAL", {"ratio": 1.0, "n": 0, "last_logged": 1.0})
        monkeypatch.setattr(agent, "_TOOLS_TOKENS", 0)
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="llamacpp"))
        state = {"text": "", "up_to": 0}
        _, oldest = agent._build_context(_hist(6), state, "", 100000, 100, log)
        assert state["_head"] == oldest == 0


class TestResumeSeedsTheHeldSummary:
    def test_main_seeds_a_held_summary_from_the_checkpoint(self):
        """The continue path hands summary_state["_pending"] back to the worker."""
        src = open(agent.__file__, encoding="utf-8").read()
        assert '_held = summary_state.pop("_pending", None)' in src
        assert '_async_summarizer.seed(_held["text"], _held["up_to"]' in src


# ── 2. summary.wait_s: a head move with nothing ready ────────────────────────

class TestWaitSetting:
    def test_null_and_blank_mean_block(self, monkeypatch):
        monkeypatch.setitem(agent._config["summary"], "wait_s", None)
        assert agent._summary_wait_s() is None
        monkeypatch.setitem(agent._config["summary"], "wait_s", "")
        assert agent._summary_wait_s() is None

    def test_numbers_are_seconds_never_negative(self, monkeypatch):
        monkeypatch.setitem(agent._config["summary"], "wait_s", 3)
        assert agent._summary_wait_s() == 3.0
        monkeypatch.setitem(agent._config["summary"], "wait_s", "0")
        assert agent._summary_wait_s() == 0.0
        monkeypatch.setitem(agent._config["summary"], "wait_s", -2)
        assert agent._summary_wait_s() == 0.0
        monkeypatch.setitem(agent._config["summary"], "wait_s", "soon")
        assert agent._summary_wait_s() is None


class FakeSummarizer:
    """A worker whose timing the test controls."""

    def __init__(self, running=False, ready=None, arrives_on_drain=None):
        self.is_running = running
        self._ready = ready                   # (text, up_to) finished before the head move
        self._arrives = arrives_on_drain      # (text, up_to) finished during the wait
        self.kicks = []
        self.drains = []

    def pending_up_to(self):
        return self._ready[1] if self._ready else None

    def harvest(self, state):
        if not self._ready:
            return False
        head = state.get("_head")
        if isinstance(head, int) and self._ready[1] > head:
            return False
        state["text"], state["up_to"] = self._ready
        state["_lead_changed"] = True
        self._ready = None
        return True

    def drain(self, timeout=None):
        self.drains.append(timeout)
        if self._arrives:
            self._ready, self._arrives, self.is_running = self._arrives, None, False

    def kick(self, old, msgs, up_to, kind="threshold"):
        if self.is_running:
            return False
        self.kicks.append((len(msgs), up_to, kind))
        self.is_running = True
        return True


class TestFoldAtHeadMove:
    def _fold(self, summarizer, wait_s, monkeypatch, up_to=0, oldest=20, ahead_head=None):
        monkeypatch.setitem(agent._config["summary"], "wait_s", wait_s)
        state = {"text": "old", "up_to": up_to, "_head": oldest}
        lg = MagicMock()
        with patch("agent._generate_summary", return_value="SYNC SUMMARY") as gen, patch("agent._emit") as emit:
            changed = agent._fold_summary_at_head_move(_hist(40), state, oldest, lg, "eviction", summarizer)
        lines = [c.args[0] % c.args[1:] for c in lg.info.call_args_list if c.args]
        notices = [c.args[2] for c in emit.call_args_list if c.args and c.args[0] == "on_notice"]
        return changed, state, gen, lines, notices

    def test_a_ready_summary_is_applied_and_nothing_waits(self, monkeypatch):
        s = FakeSummarizer(ready=("READY", 20))
        changed, state, gen, lines, notices = self._fold(s, 0, monkeypatch)
        assert changed and state["text"] == "READY" and state["up_to"] == 20
        assert not gen.called and not s.kicks and not s.drains
        assert any("the ready summary" in l for l in lines), lines

    def test_null_blocks_as_before(self, monkeypatch):
        s = FakeSummarizer()
        changed, state, gen, lines, _ = self._fold(s, None, monkeypatch)
        assert changed and gen.called and state["text"] == "SYNC SUMMARY" and state["up_to"] == 20
        assert not s.kicks

    def test_null_with_a_running_worker_leaves_it_to_the_next_harvest(self, monkeypatch):
        s = FakeSummarizer(running=True)
        changed, state, gen, lines, _ = self._fold(s, None, monkeypatch)
        assert not changed and not gen.called and state["text"] == "old"
        assert any("already running" in l for l in lines)

    def test_zero_never_blocks_and_the_worker_catches_up(self, monkeypatch):
        s = FakeSummarizer()
        changed, state, gen, lines, notices = self._fold(s, 0, monkeypatch)
        assert not changed and not gen.called and state["text"] == "old" and state["up_to"] == 0
        assert s.kicks == [(20, 20, "late")] and not s.drains
        assert any(l.startswith("summary.nowait eviction: 20 messages") for l in lines), lines
        assert "[summary: going on without one]" in notices

    def test_a_wait_that_pays_off_folds_the_summary_in(self, monkeypatch):
        s = FakeSummarizer(running=True, arrives_on_drain=("LATE BUT IN TIME", 20))
        changed, state, gen, lines, notices = self._fold(s, 3, monkeypatch)
        assert changed and state["text"] == "LATE BUT IN TIME" and state["up_to"] == 20
        assert s.drains == [3.0] and not gen.called and not s.kicks
        assert "[summary: waiting up to 3s]" in notices
        assert any("arrived after" in l for l in lines), lines

    def test_a_wait_that_runs_out_goes_on_without(self, monkeypatch):
        s = FakeSummarizer(running=True)
        changed, state, gen, lines, notices = self._fold(s, 3, monkeypatch)
        assert not changed and state["text"] == "old" and s.drains == [3.0] and not s.kicks
        assert any("still running after the 3s wait" in l for l in lines), lines
        assert "[summary: going on without one]" in notices

    def test_a_summary_short_of_the_head_leaves_the_rest_to_the_worker(self, monkeypatch):
        s = FakeSummarizer(ready=("PART", 12))
        changed, state, gen, lines, _ = self._fold(s, 0, monkeypatch)
        assert changed and state["up_to"] == 12 and not gen.called
        assert s.kicks == [(8, 20, "late")]

    def test_no_summarizer_means_the_blocking_path_whatever_the_setting(self, monkeypatch):
        changed, state, gen, lines, _ = self._fold(None, 0, monkeypatch)
        assert changed and gen.called and state["up_to"] == 20


# ── the run loop: an evicting request goes out with the last summary ────────

def _turn_with_usage(tid, read, uncached):
    call = {"index": 0, "id": tid, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": read + uncached, "completion_tokens": 5,
                       "cache_read_input_tokens": read, "cache_creation_input_tokens": 0,
                       "input_tokens": uncached}}]


def _evicting_run(agent_loop_mocks, monkeypatch, summarizer, n=50):
    monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 100)
    monkeypatch.setattr(agent, "_TOKEN_CAL", {"ratio": 1.0, "n": 0, "last_logged": 1.0})
    monkeypatch.setattr(agent, "_TOOLS_TOKENS", 0)
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model="moonshotai-kimi-k3", honours_system=True, base_url="http://x"))
    monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 10)
    monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - 60})
    agent_loop_mocks.stub_tool("read_file", "contents")
    sent = []
    real = agent_loop_mocks._fake_llm_request

    def capture(log, **kw):
        sent.append(kw["json"]["messages"])
        return real(log, **kw)
    monkeypatch.setattr(agent, "_llm_request", capture)
    agent_loop_mocks.script(_turn_with_usage("t1", 0, 100))
    h = _hist(n)
    h.append({"role": "user", "content": "go on"})
    state = {"text": "old summary", "up_to": 0, "_cache_head": 1}
    lg = MagicMock()
    with patch("agent._generate_summary", return_value="SYNC SUMMARY") as gen, patch("agent._save_checkpoint"):
        agent.run_agent_single(h, state, [], lg, max_tokens=100, ctx_size=1000, async_summarizer=summarizer)
    lines = [c.args[0] % c.args[1:] for c in lg.info.call_args_list if c.args]
    return sent, state, lines, gen


def test_with_wait_zero_the_evicting_request_carries_the_last_summary(agent_loop_mocks, monkeypatch):
    monkeypatch.setitem(agent._config["summary"], "wait_s", 0)
    s = FakeSummarizer()
    sent, state, lines, gen = _evicting_run(agent_loop_mocks, monkeypatch, s)
    assert not gen.called, "nothing is summarized in front of the request"
    lead = next(m for m in sent[0] if m["role"] == "user" and "Progress summary" in str(m["content"]))
    assert "old summary" in lead["content"]
    assert s.kicks and s.kicks[0][2] == "late" and s.kicks[0][1] == state["_cache_head"]
    assert any(l.startswith("summary.nowait eviction") for l in lines), lines
    assert len(sent[0]) < 40


def test_with_wait_null_the_evicting_request_still_blocks_for_its_summary(agent_loop_mocks, monkeypatch):
    monkeypatch.setitem(agent._config["summary"], "wait_s", None)
    s = FakeSummarizer()
    sent, state, lines, gen = _evicting_run(agent_loop_mocks, monkeypatch, s)
    assert gen.called and state["text"] == "SYNC SUMMARY" and not s.kicks


def test_a_ready_summary_makes_the_eviction_free(agent_loop_mocks, monkeypatch):
    monkeypatch.setitem(agent._config["summary"], "wait_s", 0)
    s = FakeSummarizer(ready=("READY SUMMARY", 100))   # past any head: held, then the eviction aligns to it
    sent, state, lines, gen = _evicting_run(agent_loop_mocks, monkeypatch, s)
    assert not gen.called
    assert state["text"] in ("READY SUMMARY", "old summary")
