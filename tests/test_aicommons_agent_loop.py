"""Agent-loop behaviour the AI Commons backend depends on (aicommons-integration.md §7 H2/H4/H5)."""

import json
import logging

import pytest

import agent
from anthropic_wire import THINKING_KEY
from llm_backend import BudgetExceeded, ConfigError

log = logging.getLogger("test_aicommons_agent_loop")


def _tool_delta(idx, tid, name, args):
    return {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": idx, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]}


def _final(finish, **extra):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}, **extra}


def _run(history):
    return agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)


def test_tool_call_cut_off_at_max_tokens_is_dropped_not_run(agent_loop_mocks):
    """H4: half-written arguments must not be executed, nor stored to poison later requests."""
    ran = []
    agent_loop_mocks.stub_tool("read_file", "contents")
    agent.MAP_FN["write_file"] = lambda **kw: ran.append(kw) or "written"
    try:
        agent_loop_mocks.script(
            [_tool_delta(0, "t1", "read_file", '{"path": "a.py"}'),
             _tool_delta(1, "t2", "write_file", '{"path": "b.py", "content": "def f('),
             _final("length")],
        )
        history = [{"role": "user", "content": "write b.py"}]
        _run(history)
    finally:
        agent.MAP_FN.pop("write_file", None)
    assert ran == []
    assistant = next(m for m in history if m["role"] == "assistant" and m.get("tool_calls"))
    assert [tc["function"]["name"] for tc in assistant["tool_calls"]] == ["read_file"]
    notes = [m["content"] for m in history if m["role"] == "user" and "cut off" in str(m["content"])]
    assert notes and "write_file" in notes[0]
    for m in history:
        for tc in m.get("tool_calls") or []:
            json.loads(tc["function"]["arguments"])


def test_unparseable_args_without_truncation_are_left_alone(agent_loop_mocks):
    """Local models emit malformed calls on a CLEAN stop; the existing salvage path
    (tool_recovery) owns those, so the cut-off drop applies only to a cut-off response."""
    agent_loop_mocks.stub_tool("read_file", "contents")
    agent_loop_mocks.script([_tool_delta(0, "t1", "read_file", '{"path": "a.py"'), _final("tool_calls")])
    history = [{"role": "user", "content": "read"}]
    _run(history)
    assert not [m for m in history if "was NOT run" in str(m.get("content"))]


def test_thinking_blocks_are_stored_on_tool_turns(agent_loop_mocks):
    """H5: Bedrock needs the turn's signed thinking before its tool_use when it is sent back."""
    agent_loop_mocks.stub_tool("read_file", "contents")
    stored = {"model": "claude-v5.5-opus", "blocks": [{"type": "thinking", "thinking": "t", "signature": "S"}]}
    agent_loop_mocks.script([
        {"choices": [{"index": 0, "delta": {"reasoning_content": "t"}}]},
        _tool_delta(0, "t1", "read_file", '{"path": "a.py"}'),
        _final("tool_calls", **{THINKING_KEY: stored}),
    ])
    history = [{"role": "user", "content": "read"}]
    _run(history)
    assistant = next(m for m in history if m["role"] == "assistant" and m.get("tool_calls"))
    assert assistant[THINKING_KEY] == stored


@pytest.mark.parametrize("exc", [BudgetExceeded("Monthly token allowance exhausted"),
                                 ConfigError("key rejected")])
def test_budget_and_config_errors_end_the_run_as_backend_errors(agent_loop_mocks, monkeypatch, exc):
    """H2: these used to escape the run loop, skipping the --job result file and AGENT-EXIT."""
    def boom(log, **kw):
        raise exc
    monkeypatch.setattr(agent, "_llm_request", boom)
    monkeypatch.setattr(agent, "_LAST_ERROR_KIND", None)
    assert _run([{"role": "user", "content": "go"}]) == "error"
    assert agent._LAST_ERROR_KIND == "backend"
    code, _ = agent._classify_auto_result("error", agent._LAST_ERROR_KIND, None)
    assert code == agent.EXIT_BACKEND


def test_refusal_ends_the_run_without_storing_or_nudging(agent_loop_mocks, monkeypatch):
    """M3: a refusal re-sent by the nudge loop just refuses again until the nudge cap."""
    monkeypatch.setattr(agent, "_NUDGE_ENABLED", True)
    agent_loop_mocks.script(
        [{"choices": [{"index": 0, "delta": {"content": "I can't help with th"}}]}, _final("content_filter")],
        [{"choices": [{"index": 0, "delta": {"content": "should never be requested"}}]}, _final("stop")],
    )
    history = [{"role": "user", "content": "do the thing"}]
    monkeypatch.setattr(agent, "_LAST_ERROR_KIND", None)
    assert _run(history) == "error"
    assert agent._LAST_ERROR_KIND == "refusal"
    assert not any(m["role"] == "assistant" for m in history)
    code, detail = agent._classify_auto_result("error", "refusal")
    assert code == agent.EXIT_BACKEND and detail.startswith("refusal")
