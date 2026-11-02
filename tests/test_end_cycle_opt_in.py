"""end_cycle is opt-in (preferences.end_cycle_tool, default false).

Off, the model never sees the tool and a call to it from memory is refused, so an
always-on interactive agent cannot end its own session (Mike, 2026-11-02: Iris had
been told not to use it; now it is not there). On, it works as before.
"""

import copy
import logging
from unittest.mock import MagicMock

import pytest

import agent
import tools.end_cycle as end_cycle_tool

log = logging.getLogger("test_end_cycle_opt_in")


def _tool_turn(tid, name, args):
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


@pytest.fixture
def loop(monkeypatch, agent_loop_mocks):
    # A caching backend: the one path that offers end_cycle from turn one.
    backend = MagicMock(kind="aicommons", model="claude-v5.5-opus", honours_system=True, base_url="https://gw/v1")
    monkeypatch.setattr(agent, "_main_backend", backend)
    sent = []
    real = agent_loop_mocks._fake_llm_request

    def capture(log, **kw):
        sent.append(copy.deepcopy(kw["json"]))
        return real(log, **kw)
    monkeypatch.setattr(agent, "_llm_request", capture)
    agent.MAP_FN["end_cycle"] = end_cycle_tool.fn
    return agent_loop_mocks, sent


def _offered(sent):
    return [any(t["function"]["name"] == "end_cycle" for t in body.get("tools", [])) for body in sent]


def test_the_default_is_off():
    assert agent._DEFAULT_CONFIG["preferences"]["end_cycle_tool"] is False


def test_off_the_tool_is_not_offered_and_a_call_is_refused(loop, monkeypatch):
    monkeypatch.setattr(agent, "_END_CYCLE_ENABLED", False)
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "end_cycle", '{"summary": "bye"}'))
    history = [{"role": "user", "content": "do the task"}]
    result = agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert _offered(sent) == [False, False], "end_cycle must not be in the tool list"
    refusal = [m for m in history if m.get("role") == "tool" and m.get("name") == "end_cycle"]
    assert refusal and "end_cycle is off" in str(refusal[0]["content"])
    assert len(sent) == 2, "the loop must go on after the refusal, not exit"
    assert result == "done"  # ended by the next turn's text, not by the refused call


def test_on_the_tool_is_offered_and_ends_the_cycle(loop, monkeypatch):
    monkeypatch.setattr(agent, "_END_CYCLE_ENABLED", True)
    mocks, sent = loop
    mocks.script(_tool_turn("t1", "end_cycle", '{"summary": "bye"}'))
    history = [{"role": "user", "content": "do the task"}]
    result = agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    assert _offered(sent) == [True]
    assert result == "done"
    assert history[-1] == {"role": "tool", "tool_call_id": "t1", "name": "end_cycle", "content": "Cycle ended."}
