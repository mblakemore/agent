"""Keeping the cached prefix alive while the agent waits for input.

Measured 2026-10-31 on AI Commons: cached input costs a twentieth of uncached, so
re-sending the last request with a one-token output limit before the cache
expires costs a twentieth of the miss it prevents. The interval follows the
lifetime record, every fourth keepalive stretches past the proven lifetime to
learn more, and the whole thing stops at an idle cap, after which the collapse
is the cheaper path.
"""
import json
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import agent
import llm_backend as lb

log = logging.getLogger("test_cache_keepalive")
KIMI = "aicommons:moonshotai-kimi-k3"


class TestPlan:
    def test_a_fraction_of_the_proven_lifetime(self):
        assert agent._cache_keepalive_plan(1200, None, 1, 4) == pytest.approx(960)
        assert agent._cache_keepalive_plan(1200, None, 3, 4) == pytest.approx(960)

    def test_never_under_a_minute(self):
        assert agent._cache_keepalive_plan(30, None, 1, 4) == 60

    def test_every_fourth_one_stretches_to_learn(self):
        assert agent._cache_keepalive_plan(1200, None, 4, 4) == pytest.approx(1440)
        assert agent._cache_keepalive_plan(1200, None, 8, 4) == pytest.approx(1440)

    def test_a_recorded_death_bounds_the_stretch_except_now_and_then(self):
        assert agent._cache_keepalive_plan(1200, 1300, 4, 4) == pytest.approx(1300 * 0.85)
        assert agent._cache_keepalive_plan(1200, 1300, 40, 4) == pytest.approx(1440), "the tenth probe retries"

    def test_no_probing_when_disabled(self):
        assert agent._cache_keepalive_plan(1200, None, 4, 0) == pytest.approx(960)


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class _Stop:
    """A fake Event: `wait` advances the clock and reports whether a run started."""
    def __init__(self, clock, run_after=None):
        self.clock, self.waits, self.run_after = clock, [], run_after

    def wait(self, s):
        self.waits.append(s)
        self.clock.t += s
        return self.run_after is not None and len(self.waits) >= self.run_after


def _usage(read, uncached):
    return {"cache_read_input_tokens": read, "cache_creation_input_tokens": 0, "input_tokens": uncached}


class TestLoop:
    def _run(self, monkeypatch, send, run_after=None, max_idle_s=3600, probe_every=4, model="moonshotai-kimi-k3"):
        clock = _Clock()
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons", model=model))
        monkeypatch.setattr(agent, "_CACHE_REQ", {"n": 5, "prev_sig": None, "last_at": clock.t})
        stop = _Stop(clock, run_after)
        lg = MagicMock()
        why = agent._cache_keepalive_loop(stop, send, lg, KIMI, max_idle_s, probe_every, now=clock)
        lines = [c.args[0] % c.args[1:] for c in lg.info.call_args_list if c.args]
        return why, stop, lines, clock

    def test_hits_refresh_the_clock_and_teach_the_record(self, monkeypatch):
        send = MagicMock(return_value=_usage(900, 50))
        why, stop, lines, clock = self._run(monkeypatch, send, run_after=5, max_idle_s=8000)
        assert why == "run" and send.call_count == 4
        # kimi is seeded at 1500 s: 0.8x normally; the fourth stretches to 1.2x, and its
        # hit raises the proven lifetime to 1800 s, so the fifth waits 0.8x of that
        assert stop.waits == [1200, 1200, 1200, 1800, pytest.approx(1440)]
        assert agent._CACHE_LIFETIMES[KIMI]["hit_max_s"] == pytest.approx(1800)
        assert agent._CACHE_REQ["last_at"] == pytest.approx(clock.t - 1440), "the idle clock restarts at each hit"
        assert sum("cache.keepalive" in l and "hit" in l for l in lines) == 4

    def test_stops_at_the_idle_cap(self, monkeypatch):
        send = MagicMock(return_value=_usage(900, 50))
        why, stop, lines, _ = self._run(monkeypatch, send, max_idle_s=3000)
        assert why == "cap" and send.call_count == 2 and len(stop.waits) == 2
        assert any("idle cap" in l for l in lines), lines

    def test_two_misses_mean_the_server_is_not_caching(self, monkeypatch):
        send = MagicMock(side_effect=[_usage(0, 950), _usage(900, 50), _usage(0, 950)])
        why, stop, lines, _ = self._run(monkeypatch, send)
        assert why == "miss" and send.call_count == 3
        assert agent._CACHE_LIFETIMES[KIMI]["hits"] == 1

    def test_a_failed_request_stops_it(self, monkeypatch):
        send = MagicMock(side_effect=RuntimeError("gateway down"))
        why, *_ = self._run(monkeypatch, send)
        assert why == "error" and send.call_count == 1

    def test_a_model_switch_stops_it_before_sending(self, monkeypatch):
        send = MagicMock(return_value=_usage(900, 50))
        why, *_ = self._run(monkeypatch, send, model="claude-v5.5-sonnet")
        assert why == "model" and not send.called


class TestArming:
    def test_the_run_wrapper_disarms_then_arms(self, agent_loop_mocks, monkeypatch):
        monkeypatch.setattr(agent, "_main_backend",
                            MagicMock(kind="aicommons", model="moonshotai-kimi-k3", honours_system=True, base_url="http://x"))
        arm, disarm = MagicMock(return_value=True), MagicMock()
        monkeypatch.setattr(agent, "_cache_keepalive_arm", arm)
        monkeypatch.setattr(agent, "_cache_keepalive_disarm", disarm)
        monkeypatch.setattr(agent, "_LAST_REQUEST", {"body": None})
        lg = MagicMock()
        agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], lg)
        assert disarm.called and arm.call_args.args == (lg,)
        body = json.loads(agent._LAST_REQUEST["body"])
        assert body["messages"][-1]["content"].startswith("go") and body["model"] == "moonshotai-kimi-k3"

    def test_a_sliding_window_backend_records_no_body(self, agent_loop_mocks, monkeypatch):
        monkeypatch.setattr(agent, "_main_backend",
                            MagicMock(kind="llamacpp", model="m", honours_system=True, base_url="http://x"))
        monkeypatch.setattr(agent, "_LAST_REQUEST", {"body": None})
        agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], MagicMock())
        assert agent._LAST_REQUEST["body"] is None

    def test_arm_needs_a_body_a_caching_backend_and_the_setting(self, monkeypatch):
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons", model="m"))
        monkeypatch.setattr(agent, "_LAST_REQUEST", {"body": None})
        assert not agent._cache_keepalive_arm(log)
        monkeypatch.setattr(agent, "_LAST_REQUEST", {"body": "{}"})
        monkeypatch.setitem(agent._config["context"], "cache_keepalive", False)
        assert not agent._cache_keepalive_arm(log)
        monkeypatch.setitem(agent._config["context"], "cache_keepalive", True)
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="llamacpp", model="m"))
        assert not agent._cache_keepalive_arm(log)

    def test_arm_starts_a_thread_that_resends_the_body_flagged_and_disarm_stops_it(self, monkeypatch):
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons", model="moonshotai-kimi-k3"))
        monkeypatch.setitem(agent._config["context"], "cache_keepalive", True)
        monkeypatch.setattr(agent, "_LAST_REQUEST",
                            {"body": json.dumps({"model": "moonshotai-kimi-k3", "messages": [{"role": "user", "content": "x"}]})})
        sent = []
        done = threading.Event()

        def fake_loop(stop, send, lg, key, max_idle_s, probe_every, now=time.time):
            sent.append(send())
            assert key == KIMI and max_idle_s == 3600 and probe_every == 4
            stop.wait(5)
            done.set()
            return "run"
        monkeypatch.setattr(agent, "_cache_keepalive_loop", fake_loop)
        bodies = []

        def fake_request(lg, **kw):
            bodies.append(kw["json"])
            return iter([{"choices": [{"delta": {}}]}, {"usage": _usage(900, 7)}])
        monkeypatch.setattr(agent, "_llm_request", fake_request)
        assert agent._cache_keepalive_arm(log)
        t = agent._KEEPALIVE["thread"]
        agent._cache_keepalive_disarm()
        assert done.wait(5) and not t.is_alive() or t.join(5) is None
        assert bodies[0]["_cache_keepalive"] is True and bodies[0]["messages"][0]["content"] == "x"
        assert sent == [_usage(900, 7)]


def test_the_backend_sends_one_output_token_for_a_keepalive():
    b = lb.AICommonsBackend({"kind": "aicommons", "model": "moonshotai-kimi-k3",
                             "aicommons": {"base_url": "https://blk/v1", "api_key": "kb"}})
    msgs = [{"role": "user", "content": "hello"}]
    assert b._request_kwargs({"messages": msgs})["max_tokens"] == b.max_tokens > 1
    assert b._request_kwargs({"messages": msgs, "_cache_keepalive": True})["max_tokens"] == 1
