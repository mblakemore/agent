"""Prompt-cache discipline on a caching backend (aicommons-integration.md P4).

The AI Commons gateway puts its cache point at the END of the last message, so a
turn reads cache only if the previous request is a byte-identical prefix of this
one. These tests drive the real loop, capture every outgoing request, convert it
exactly as AICommonsBackend would, and check the prefix property.
"""

import copy
import json
import logging
from unittest.mock import MagicMock

import pytest

import agent
from anthropic_wire import openai_to_anthropic

log = logging.getLogger("test_cache_discipline")
MODEL = "claude-v5.5-opus"


def _wire(body):
    system, messages, tools = openai_to_anthropic(body["messages"], body.get("tools"),
                                                  model=MODEL, end_with_user=True)
    return {"system": system, "tools": tools, "messages": messages}


def _tool_turn(tid, name="read_file", args='{"path": "README.md"}'):
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


@pytest.fixture
def caching(monkeypatch, agent_loop_mocks):
    backend = MagicMock(kind="aicommons", model=MODEL, honours_system=True, base_url="https://gw/v1")
    monkeypatch.setattr(agent, "_main_backend", backend)
    sent = []
    real = agent_loop_mocks._fake_llm_request

    def capture(log, **kw):
        sent.append(copy.deepcopy(kw["json"]))
        return real(log, **kw)
    monkeypatch.setattr(agent, "_llm_request", capture)
    agent_loop_mocks.stub_tool("read_file", "file contents " * 50)
    agent_loop_mocks.stub_tool("list_dir", "a.py\nb.py")
    return agent_loop_mocks, sent


def _blocks(messages):
    return [json.dumps([m["role"], blk], sort_keys=True) for m in messages for blk in m["content"]]


def _assert_prefix_chain(sent):
    reqs = [_wire(b) for b in sent]
    assert len(reqs) >= 3
    for a, b in zip(reqs, reqs[1:]):
        assert a["system"] == b["system"], "system prompt changed between turns"
        assert json.dumps(a["tools"]) == json.dumps(b["tools"]), "tool list changed between turns"
        # Caching is prefix-by-content-block (the server looks back from its cache
        # point to the longest cached block prefix), so compare flattened blocks.
        fa, fb = _blocks(a["messages"]), _blocks(b["messages"])
        assert fb[:len(fa)] == fa, "request N is not a prefix of request N+1"


def test_each_request_extends_the_previous_one(caching):
    mocks, sent = caching
    mocks.script(_tool_turn("t1"), _tool_turn("t2", "list_dir", '{"path": "."}'), _tool_turn("t3"))
    agent.run_agent_single([{"role": "system", "content": "SYSTEM PROMPT"},
                            {"role": "user", "content": "inspect the repo"}],
                           {"text": "", "up_to": 0}, [], log)
    _assert_prefix_chain(sent)


def test_nudge_turns_keep_the_prefix_and_end_cycle_is_offered_from_turn_one(caching, monkeypatch):
    mocks, sent = caching
    monkeypatch.setattr(agent, "_NUDGE_ENABLED", True)
    mocks.script(
        _tool_turn("t1"),
        [{"choices": [{"index": 0, "delta": {"content": "I think I am done."}}]}],     # text-only → nudge
        _tool_turn("t2", "end_cycle", '{"summary": "done"}'),
    )
    agent.run_agent_single([{"role": "system", "content": "SYSTEM PROMPT"},
                            {"role": "user", "content": "do the task"}],
                           {"text": "", "up_to": 0}, [], log)
    assert all(any(t["function"]["name"] == "end_cycle" for t in b["tools"]) for b in sent)
    _assert_prefix_chain(sent)


def test_end_cycle_is_refused_before_it_unlocks(caching):
    mocks, sent = caching
    mocks.script(_tool_turn("t1", "end_cycle", '{"summary": "x"}'))
    history = [{"role": "user", "content": "do the task"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    result = next(m for m in history if m.get("role") == "tool")
    assert result["content"].startswith("Error: end_cycle is not available yet")


def test_working_directory_is_not_in_the_system_head(caching):
    mocks, sent = caching
    mocks.script(_tool_turn("t1"))
    agent.run_agent_single([{"role": "system", "content": "SYSTEM PROMPT"},
                            {"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], log)
    first = _wire(sent[0])
    assert "Working directory" not in (first["system"] or "")
    assert "Working directory" in json.dumps(first["messages"])


def test_runtime_note_is_stored_once_per_turn():
    hist = [{"role": "user", "content": "task", "_tokens": 3}]
    sent = list(hist)
    agent._persist_runtime_note(hist, sent, ["clock 1"], turn=1)
    agent._persist_runtime_note(hist, sent, ["clock 1"], turn=1)      # retried request, same turn
    assert hist[0]["content"].count("clock 1") == 1 and "_tokens" not in hist[0]
    hist.append({"role": "assistant", "content": "ok"})
    agent._persist_runtime_note(hist, sent, ["clock 2"], turn=2)
    assert hist[-1]["role"] == "user" and "clock 2" in hist[-1]["content"] and sent[-1] is hist[-1]


# ── P4.2 chunky eviction ────────────────────────────────────────────────


def _history(n):
    out = [{"role": "user", "content": "task"}]
    for i in range(n):
        out.append({"role": "assistant", "content": f"step {i}"} if i % 2 == 0
                   else {"role": "user", "content": f"result {i}"})
    return out


def test_window_head_moves_in_chunks(monkeypatch):
    monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons"))
    monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 30)
    state = {"text": "", "up_to": 0}
    hist = _history(40)
    heads = []
    for _ in range(25):
        sel, oldest = agent._build_context(hist, state, "", 1_000_000, 4096, log)
        heads.append(oldest)
        assert sel[-1] is hist[-1]
        hist.append({"role": "assistant", "content": "more"})
    moves = sum(1 for a, b in zip(heads, heads[1:]) if a != b)
    assert moves <= 2, heads            # a sliding window would move 24 times
    assert len(set(heads)) == moves + 1


def test_other_backends_keep_the_sliding_window(monkeypatch):
    monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="llamacpp"))
    monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 30)
    state = {"text": "", "up_to": 0}
    hist = _history(40)
    heads = []
    for _ in range(5):
        heads.append(agent._build_context(hist, state, "", 1_000_000, 4096, log)[1])
        hist.append({"role": "assistant", "content": "more"})
    assert len(set(heads)) == 5 and "_cache_head" not in state
