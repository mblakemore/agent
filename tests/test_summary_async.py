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
