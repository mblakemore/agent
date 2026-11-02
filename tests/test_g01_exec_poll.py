"""G.01-exec (garbled exec_command guard) must let a background-session poll through.

The tool's own description says: run with background=true, then "call again with only
session_id and wait=<seconds>". That call has no 'command'. Found live 2026-11-02: the
guard refused it as garbled and the model fell back to a tail-and-sleep loop.
"""

import logging

import agent

log = logging.getLogger("test_g01_exec_poll")


def _tool_turn(tid, name, args):
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


def _run(mocks, args):
    """Returns (tool kwargs seen, exec_command tool messages left in history). A fully
    garbled turn is retried and its error message removed from history, so the garbled
    cases check the guard's warning instead."""
    ran = []
    agent.MAP_FN["exec_command"] = lambda **kw: ran.append(kw) or "exit=0"
    mocks.script(_tool_turn("t1", "exec_command", args))
    history = [{"role": "user", "content": "go"}]
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    results = [m for m in history if m.get("role") == "tool" and m.get("name") == "exec_command"]
    return ran, results


def test_a_session_poll_with_wait_reaches_the_tool(agent_loop_mocks):
    ran, results = _run(agent_loop_mocks, '{"session_id": "agent-iris-97e4fa", "wait": 20}')
    assert ran == [{"session_id": "agent-iris-97e4fa", "wait": 20}]
    assert not any("garbled" in str(m["content"]) for m in results)


def test_a_session_poll_without_wait_reaches_the_tool(agent_loop_mocks):
    ran, _ = _run(agent_loop_mocks, '{"session_id": "agent-iris-97e4fa"}')
    assert len(ran) == 1


def test_a_call_with_neither_command_nor_session_is_still_garbled(agent_loop_mocks, caplog):
    with caplog.at_level(logging.WARNING, logger="agent"):
        ran, _ = _run(agent_loop_mocks, '{"wait": 20}')
    assert ran == []
    assert "G.01-exec" in caplog.text


def test_a_non_string_command_is_still_garbled_even_with_a_session(agent_loop_mocks, caplog):
    with caplog.at_level(logging.WARNING, logger="agent"):
        ran, _ = _run(agent_loop_mocks, '{"command": 3, "session_id": "s1"}')
    assert ran == []
    assert "G.01-exec" in caplog.text


def test_an_empty_session_id_does_not_count_as_a_poll(agent_loop_mocks, caplog):
    with caplog.at_level(logging.WARNING, logger="agent"):
        ran, _ = _run(agent_loop_mocks, '{"session_id": "", "wait": 5}')
    assert ran == []
    assert "G.01-exec" in caplog.text
