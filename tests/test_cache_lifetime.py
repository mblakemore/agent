"""The prompt-cache lifetime record.

Measured 2026-10-31 on the AI Commons kimi gateway: a byte-identical prefix was still
cached after 20-minute pauses and gone after 44, while the agent judged `idle` against
the five minutes Anthropic documents. The record learns the lifetime per backend and
model from the requests the agent sends anyway, and everything that depends on the
lifetime (the idle reason, the collapse, the keepalive) reads the record.
"""
import json
import logging
import time
from unittest.mock import MagicMock

import pytest

import agent

log = logging.getLogger("test_cache_lifetime")
KIMI = "aicommons:moonshotai-kimi-k3"
CLAUDE = "aicommons:claude-v5.5-sonnet"


class TestSeeds:
    def test_documented_and_measured_families(self):
        assert agent._cache_lifetime_seed(CLAUDE) == 300.0
        assert agent._cache_lifetime_seed(KIMI) == 1500.0

    def test_an_unknown_family_gets_the_default(self):
        assert agent._cache_lifetime_seed("aicommons:gpt-x") == agent._CACHE_IDLE_SECONDS

    def test_the_key_names_kind_and_model(self):
        assert agent._cache_lifetime_key(MagicMock(kind="aicommons", model="m1")) == "aicommons:m1"


class TestObserve:
    def test_a_hit_raises_the_proven_lifetime(self):
        r = agent._cache_lifetime_observe({}, KIMI, 1500, True)
        assert r[KIMI]["hit_max_s"] == 1500 and r[KIMI]["hits"] == 1
        agent._cache_lifetime_observe(r, KIMI, 900, True)
        assert r[KIMI]["hit_max_s"] == 1500, "a shorter hit does not lower it"

    def test_a_miss_inside_the_proven_lifetime_is_the_providers(self):
        r = agent._cache_lifetime_observe({}, KIMI, 1500, True)
        agent._cache_lifetime_observe(r, KIMI, 900, False)
        assert r[KIMI]["miss_min_s"] is None and r[KIMI]["misses"] == 0

    def test_a_miss_beyond_every_hit_records_the_death(self):
        r = agent._cache_lifetime_observe({}, KIMI, 1200, True)
        agent._cache_lifetime_observe(r, KIMI, 2640, False)
        agent._cache_lifetime_observe(r, KIMI, 2000, False)
        assert r[KIMI]["miss_min_s"] == 2000 and r[KIMI]["misses"] == 2

    def test_a_later_hit_beyond_a_recorded_miss_retires_it(self):
        r = agent._cache_lifetime_observe({}, KIMI, 2000, False)
        agent._cache_lifetime_observe(r, KIMI, 2100, True)
        assert r[KIMI]["miss_min_s"] is None and r[KIMI]["hit_max_s"] == 2100

    def test_short_gaps_say_nothing(self):
        assert agent._cache_lifetime_observe({}, KIMI, 30, False) == {}
        assert agent._cache_lifetime_observe({}, KIMI, None, True) == {}

    def test_the_entry_is_stamped(self):
        r = agent._cache_lifetime_observe({}, KIMI, 600, True, now=0)
        assert r[KIMI]["updated"] == "1970-01-01T00:00:00Z"


class TestEstimate:
    def test_the_seed_until_there_is_evidence(self):
        assert agent._cache_lifetime_estimate({}, KIMI) == (1500.0, None)
        assert agent._cache_lifetime_estimate({}, CLAUDE) == (300.0, None)

    def test_a_longer_hit_raises_alive(self):
        r = agent._cache_lifetime_observe({}, CLAUDE, 540, True)
        assert agent._cache_lifetime_estimate(r, CLAUDE) == (540.0, None)

    def test_a_miss_reports_the_death(self):
        r = agent._cache_lifetime_observe({}, KIMI, 1600, True)
        agent._cache_lifetime_observe(r, KIMI, 2640, False)
        assert agent._cache_lifetime_estimate(r, KIMI) == (1600.0, 2640.0)

    def test_a_miss_below_the_seed_lowers_alive(self):
        """The seed is a guess; a miss at 600 s on a family seeded at 1500 s means the
        cache is not known to live 1500 s after all."""
        r = agent._cache_lifetime_observe({}, KIMI, 600, False)
        alive, dead = agent._cache_lifetime_estimate(r, KIMI)
        assert alive == pytest.approx(540.0) and dead == 600.0


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "state" / "cache_lifetimes.json")
        r = agent._cache_lifetime_observe({}, KIMI, 1500, True)
        agent._cache_lifetimes_save(r, path)
        assert agent._cache_lifetimes_load(path) == r
        assert json.load(open(path))[KIMI]["hit_max_s"] == 1500

    def test_the_loop_persists_what_it_learns(self):
        """The default path is the state file the autouse fixture redirected."""
        agent._cache_lifetime_record_request(700, True, log, key=KIMI)
        assert agent._cache_lifetimes_load()[KIMI]["hit_max_s"] == 700

    def test_missing_or_broken_file_is_empty(self, tmp_path):
        assert agent._cache_lifetimes_load(str(tmp_path / "none.json")) == {}
        broken = tmp_path / "broken.json"
        broken.write_text("{not json")
        assert agent._cache_lifetimes_load(str(broken)) == {}


# ── the request loop feeds the record and reads it back ──────────────────────

def _turn_with_usage(tid, read, uncached):
    call = {"index": 0, "id": tid, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": read + uncached, "completion_tokens": 5,
                       "cache_read_input_tokens": read, "cache_creation_input_tokens": 0,
                       "input_tokens": uncached}}]


def _run(agent_loop_mocks, monkeypatch, model, gap, read, uncached):
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model=model, honours_system=True, base_url="http://x"))
    monkeypatch.setattr(agent, "_prefix_unchanged", lambda a, b: True)
    monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - gap})
    agent_loop_mocks.stub_tool("read_file", "contents")
    agent_loop_mocks.script(_turn_with_usage("t1", read, uncached))
    lg = MagicMock()
    agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], lg)
    return [c.args[0] % c.args[1:] for c in lg.info.call_args_list
            if c.args and str(c.args[0]).startswith(("cache.miss", "cache.lifetime"))]


def test_a_hit_after_a_gap_is_recorded_as_proof_of_life(agent_loop_mocks, monkeypatch):
    lines = _run(agent_loop_mocks, monkeypatch, "moonshotai-kimi-k3", 700, 900, 50)
    assert agent._CACHE_LIFETIMES[KIMI]["hit_max_s"] == pytest.approx(700, abs=2)
    assert any(l.startswith("cache.lifetime") and "hit alive>=" in l for l in lines), lines


def test_idle_is_judged_against_the_record_not_five_minutes(agent_loop_mocks, monkeypatch):
    # kimi is seeded at 25 minutes: a miss after 12 minutes is the provider's
    lines = _run(agent_loop_mocks, monkeypatch, "moonshotai-kimi-k3", 700, 0, 950)
    assert any("reason=provider" in l for l in lines), lines
    # but it is recorded: no hit has proven anything yet, so it is the shortest miss
    assert agent._CACHE_LIFETIMES[KIMI]["miss_min_s"] == pytest.approx(700, abs=2)
    # and after 44 minutes the miss is idle (the label is judged before the record)
    lines = _run(agent_loop_mocks, monkeypatch, "moonshotai-kimi-k3", 2640, 0, 950)
    assert any("reason=idle" in l for l in lines), lines
    assert agent._CACHE_LIFETIMES[KIMI]["misses"] == 2


def test_a_claude_model_keeps_the_documented_five_minutes(agent_loop_mocks, monkeypatch):
    lines = _run(agent_loop_mocks, monkeypatch, "claude-v5.5-sonnet", 301, 0, 950)
    assert any("reason=idle" in l for l in lines), lines


def test_a_changed_prefix_teaches_nothing(agent_loop_mocks, monkeypatch):
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model="moonshotai-kimi-k3", honours_system=True, base_url="http://x"))
    monkeypatch.setattr(agent, "_prefix_unchanged", lambda a, b: False)
    monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": time.time() - 700})
    agent_loop_mocks.stub_tool("read_file", "contents")
    agent_loop_mocks.script(_turn_with_usage("t1", 900, 50))
    agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], MagicMock())
    assert agent._CACHE_LIFETIMES == {}
