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
    state = {"text": "old summary", "up_to": 0, "_cache_head": 1, "_head": 1}
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
    """ctx 1000 / max_tokens 100 → budget 388; 52 messages of 10 tokens overflow it. A
    greedy eviction would keep 22 (head 30); a summary ready up to 28 is within the
    band, so the window starts at 28 and the request carries that summary, unwaited."""
    monkeypatch.setitem(agent._config["summary"], "wait_s", 0)
    s = FakeSummarizer(ready=("READY SUMMARY", 28))
    sent, state, lines, gen = _evicting_run(agent_loop_mocks, monkeypatch, s)
    assert not gen.called and not s.kicks and not s.drains
    assert state["_cache_head"] == 28 and state["up_to"] == 28 and state["text"] == "READY SUMMARY"
    lead = next(m for m in sent[0] if m["role"] == "user" and "Progress summary" in str(m["content"]))
    assert "READY SUMMARY" in lead["content"]
    assert any(l.startswith("summary.align: window head 1 -> 28") for l in lines), lines
    assert any("the ready summary of the history up to message 28 folded" in l for l in lines), lines
    assert not any("summary.nowait" in l for l in lines)


# ── 3. summary.ahead: the eviction's summary is made before the eviction ─────

@pytest.fixture
def small_budget(monkeypatch):
    """ctx 1000 / max_tokens 100 → budget 388; every message and the lead cost 10."""
    monkeypatch.setattr(agent, "_TOKEN_CAL", {"ratio": 1.0, "n": 0, "last_logged": 1.0})
    monkeypatch.setattr(agent, "_TOOLS_TOKENS", 0)
    monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 100)
    monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 10)
    monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons", model="moonshotai-kimi-k3"))
    monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - 60})


class TestFillWindow:
    def test_is_the_greedy_fill_the_window_builder_used(self, monkeypatch):
        monkeypatch.setattr(agent, "_TOKEN_CAL", {"ratio": 1.0, "n": 0, "last_logged": 1.0})
        monkeypatch.setattr(agent, "_TOOLS_TOKENS", 0)
        monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 100)
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="llamacpp"))
        sizes = {}
        monkeypatch.setattr(agent, "_estimate_tokens", lambda m: sizes.get(id(m), 10))
        h = _hist(60)
        for m in h[::7]:
            sizes[id(m)] = 40
        state = {"text": "a summary", "up_to": 0}
        _, oldest = agent._build_context(h, state, "", 1000, 100, log)
        lead = agent._estimate_tokens(agent._build_context_footnote("a summary", ""))
        assert agent._fill_window(h, agent._context_budget(1000, 100), 100, lead)[0] == oldest > 0

    def test_stops_at_the_message_cap_and_reserves_the_lead(self, monkeypatch):
        monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 10)
        h = _hist(20)
        assert agent._fill_window(h, 1000, 5, 0) == (16, 50)
        assert agent._fill_window(h, 55, 100, 10) == (17, 40), "40 + the lead 10 fits 55, 50 + 10 does not"
        assert agent._fill_window(h, 5, 100, 0) == (21, 0), "nothing fits"


class TestEvictedEdge:
    def test_nothing_under_the_line(self):
        h = _hist(40)
        assert agent._evicted_edge(h, 1, 200, 388, 100, 0.8) is None, "200 of 388 is under 80%"
        assert agent._evicted_edge(h, 1, 350, 388, 100, 0.0) is None, "off"

    def test_the_oldest_share_beyond_what_an_eviction_keeps(self, monkeypatch):
        monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 10)
        h = _hist(36)   # 37 messages, window of 36 from head 1 = 360 tokens, 93% of 388
        edge = agent._evicted_edge(h, 1, 360, 388, 100, 0.8)
        # keeps 360 - 0.4*388 = 204 → the newest 20; the 16 older ones are certain to go
        assert edge == 37 - 20 == 17

    def test_by_message_count_too(self, monkeypatch):
        monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 1)
        h = _hist(26)   # 26 in the window from head 1, cap 30: 87% by count, ~7% by tokens
        edge = agent._evicted_edge(h, 1, 26, 388, 30, 0.8)
        # an eviction at 31 keeps 18, drops 13: the 13 oldest of the window now
        assert edge == 1 + 13

    def test_the_window_builder_reports_the_edge_when_ahead_is_on(self, small_budget, monkeypatch):
        monkeypatch.setitem(agent._config["summary"], "ahead", 0.8)
        h, state = _hist(36), {"text": "old", "up_to": 0, "_cache_head": 1}
        _, oldest = agent._build_context(h, state, "", 1000, 100, log)
        assert oldest == 1 and state["_ahead_edge"] == 17
        monkeypatch.setitem(agent._config["summary"], "ahead", 0)
        state = {"text": "old", "up_to": 0, "_cache_head": 1}
        agent._build_context(h, state, "", 1000, 100, log)
        assert "_ahead_edge" not in state

    def test_the_eviction_then_aligns_to_that_edge(self, small_budget, monkeypatch):
        monkeypatch.setitem(agent._config["summary"], "ahead", 0.8)
        h, state = _hist(36), {"text": "old", "up_to": 0, "_cache_head": 1}
        agent._build_context(h, state, "", 1000, 100, log)
        edge = state.pop("_ahead_edge")
        h.extend({"role": "user", "content": f"more {i}"} for i in range(5))   # 41 in the window: over 388
        state["_ready_up_to"] = edge
        _, oldest = agent._build_context(h, state, "", 1000, 100, log)
        assert oldest == edge and state["_cache_head"] == edge and state["_evict_summarize"] is True

    def test_a_last_turn_too_large_falls_back_to_the_greedy_head(self, small_budget, monkeypatch):
        """The aligned window would be 28 messages against a greedy 22, over the band:
        the greedy head stands, the ready summary still applies (it ends before that
        head) and only the two-message remainder is left to the worker."""
        monkeypatch.setitem(agent._config["summary"], "ahead", 0.8)
        h, state = _hist(36), {"text": "old", "up_to": 0, "_cache_head": 1}
        agent._build_context(h, state, "", 1000, 100, log)
        edge = state.pop("_ahead_edge")
        h.extend({"role": "user", "content": f"more {i}"} for i in range(8))   # 45 in the window
        state["_ready_up_to"] = edge
        _, oldest = agent._build_context(h, state, "", 1000, 100, log)
        assert oldest == 23 and edge == 17 and state["_evict_summarize"] is True
        s = AsyncSummarizer(agent._config, log)
        s.seed("AHEAD", edge, "ahead")
        assert s.harvest(state) is True and state["up_to"] == 17


class TestAlignedHead:
    def _h(self, monkeypatch):
        monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 10)
        return _hist(51)   # 52 messages; a greedy eviction keeps 22 (fill 232 with a 10-token lead)

    def test_within_the_band_of_the_greedy_window(self, monkeypatch):
        h = self._h(monkeypatch)
        for ready in (26, 30, 34):
            assert agent._aligned_head(h, ready, 1, 232, 60, 100, 388, 10) == ready

    def test_a_collapse_sized_summary_is_not_taken_at_an_eviction(self, monkeypatch):
        h = self._h(monkeypatch)
        assert agent._aligned_head(h, 48, 1, 232, 60, 100, 388, 10) is None, "4 messages: a quarter of 22"
        assert agent._aligned_head(h, 10, 1, 232, 60, 100, 388, 10) is None, "42 messages: twice 22"

    def test_only_a_summary_past_the_head_counts(self, monkeypatch):
        h = self._h(monkeypatch)
        assert agent._aligned_head(h, None, 1, 232, 60, 100, 388, 10) is None
        assert agent._aligned_head(h, 1, 1, 232, 60, 100, 388, 10) is None
        assert agent._aligned_head(h, 30, None, 232, 60, 100, 388, 10) is None
        assert agent._aligned_head(h, MagicMock(), 1, 232, 60, 100, 388, 10) is None


def test_the_run_loop_kicks_ahead_and_holds_the_result(agent_loop_mocks, monkeypatch):
    monkeypatch.setitem(agent._config["summary"], "ahead", 0.8)
    monkeypatch.setitem(agent._config["summary"], "wait_s", 0)
    s = FakeSummarizer()
    sent, state, lines, gen = _evicting_run(agent_loop_mocks, monkeypatch, s, n=35)   # 37 messages: fits, 93% full
    assert not gen.called
    assert s.kicks == [(17, 17, "ahead")], s.kicks
    assert state["_cache_head"] == 1 and state["up_to"] == 0, "nothing is applied before the head moves"
    assert any(l.startswith("summary.ahead kick up_to=0 edge=17") for l in lines), lines


# ── the idle watcher: the collapse summary is ready before the wake ──────────

class FakeStop:
    def __init__(self, clock, stop_after=None):
        self.clock, self.stop_after, self.waits = clock, stop_after, []

    def wait(self, t):
        self.waits.append(t)
        self.clock[0] += t
        return self.stop_after is not None and self.clock[0] >= self.stop_after


class TestIdleWatcher:
    def _run(self, monkeypatch, n=51, up_to=0, ready=None, running=False):
        monkeypatch.setitem(agent._config["context"], "cache_collapse_frac", 0.15)
        monkeypatch.setattr(agent, "_cache_lifetime_estimate", lambda record, key: (600.0, None))
        monkeypatch.setattr(agent, "_cache_lifetimes", lambda: {})
        monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": 1000.0})
        s = FakeSummarizer(ready=ready, running=running)
        run = {"history": _hist(n), "state": {"text": "old", "up_to": up_to, "_cache_head": 1},
               "summarizer": s, "initial_files": "", "ctx_size": 1000, "max_tokens": 100}
        return run, s

    def test_kicks_at_half_the_lifetime(self, small_budget, monkeypatch):
        run, s = self._run(monkeypatch)
        clock = [1000.0]
        lg = MagicMock()
        why = agent._summary_idle_loop(FakeStop(clock), run, lg, now=lambda: clock[0])
        assert why == "kicked" and clock[0] >= 1300
        # the collapse keeps 58 tokens: 4 messages behind a 10-token lead → edge 48
        assert s.kicks == [(48, 48, "idle")], s.kicks
        assert any(c.args[0].startswith("summary.idle kick") for c in lg.info.call_args_list)

    def test_a_run_that_starts_first_stops_it(self, small_budget, monkeypatch):
        run, s = self._run(monkeypatch)
        clock = [1000.0]
        assert agent._summary_idle_loop(FakeStop(clock, stop_after=1100), run, MagicMock(), now=lambda: clock[0]) == "run"
        assert not s.kicks

    def test_a_window_too_small_to_collapse_needs_nothing(self, small_budget, monkeypatch):
        run, s = self._run(monkeypatch, n=6)
        clock = [1000.0]
        assert agent._summary_idle_loop(FakeStop(clock), run, MagicMock(), now=lambda: clock[0]) == "no-collapse"
        assert not s.kicks

    def test_a_summary_already_in_hand_is_left_alone(self, small_budget, monkeypatch):
        run, s = self._run(monkeypatch, ready=("R", 30))
        clock = [1000.0]
        assert agent._summary_idle_loop(FakeStop(clock), run, MagicMock(), now=lambda: clock[0]) == "busy"

    def test_keepalives_moving_last_at_push_the_kick_out(self, small_budget, monkeypatch):
        run, s = self._run(monkeypatch)
        clock = [1000.0]
        stop = FakeStop(clock)
        orig_wait = stop.wait

        def wait(t):   # a keepalive lands at 1200: the gap restarts there
            r = orig_wait(t)
            if 1200 <= clock[0] < 1230:
                agent._CACHE_REQ["last_at"] = 1200.0
            return r
        stop.wait = wait
        why = agent._summary_idle_loop(stop, run, MagicMock(), now=lambda: clock[0])
        assert why == "kicked" and clock[0] >= 1500

    def test_the_collapse_then_aligns_to_the_idle_summary(self, small_budget, monkeypatch):
        """The wake: the window is rebuilt to the collapse size exactly where the idle summary ends."""
        monkeypatch.setitem(agent._config["context"], "cache_collapse_frac", 0.15)
        monkeypatch.setattr(agent, "_cache_lifetime_estimate", lambda record, key: (600.0, None))
        monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - 3000})
        h, state = _hist(51), {"text": "old", "up_to": 0, "_cache_head": 1, "_ready_up_to": 48}
        _, oldest = agent._build_context(h, state, "", 1000, 100, log)
        assert oldest == 48 and state["_collapsed"] is True and state["_collapse_summarize"] is True

    def test_arming_needs_ahead_a_caching_backend_and_a_collapse(self, small_budget, monkeypatch):
        monkeypatch.setitem(agent._config["context"], "cache_collapse_frac", 0.15)
        monkeypatch.setitem(agent._config["summary"], "ahead", 0.8)
        monkeypatch.setattr(agent, "_SUMMARY_IDLE", {"stop": None, "thread": None, "run": None})
        assert agent._summary_idle_arm(MagicMock()) is False, "no run recorded"
        agent._SUMMARY_IDLE["run"] = {"summarizer": FakeSummarizer(), "history": [], "state": {},
                                      "initial_files": "", "ctx_size": 1000, "max_tokens": 100}
        with patch("agent._summary_idle_loop", return_value="x"):
            assert agent._summary_idle_arm(MagicMock()) is True
            agent._SUMMARY_IDLE["thread"].join(2)
        agent._summary_idle_disarm()
        monkeypatch.setitem(agent._config["summary"], "ahead", 0)
        assert agent._summary_idle_arm(MagicMock()) is False
        monkeypatch.setitem(agent._config["summary"], "ahead", 0.8)
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="llamacpp"))
        assert agent._summary_idle_arm(MagicMock()) is False
