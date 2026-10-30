"""Thinking replay and checkpointing.

The wire sends back every stored thinking block in the window, every request: Opus
4.5+, Sonnet 4.6+ and Haiku 5.5 keep prior turns' thinking in the cached prefix, so a
turn sent with its thinking once and without it the next time is a rewrite that misses
the cache from there on (measured 2026-10-29: 1-8k tokens re-written per request, and
read=0 streaks after every eviction). Only the CHECKPOINT copy is pruned to the newest
turn, which was 595k of a 1,217k-character file; AGENT_PRUNE_OLD_THINKING=0 keeps all.
"""

import copy
import json

import agent
from anthropic_wire import THINKING_KEY, openai_to_anthropic

M = "claude-v5.5-sonnet"


def _think(tag):
    return {"model": M, "blocks": [{"type": "thinking", "thinking": tag, "signature": "sig-" + tag}]}


def _asst(tag, tid):
    return {"role": "assistant", "content": "", THINKING_KEY: _think(tag),
            "tool_calls": [{"id": tid, "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}]}


def _tool(tid):
    return {"role": "tool", "tool_call_id": tid, "name": "read_file", "content": "ok"}


def _history():
    return [{"role": "user", "content": "go"}, _asst("A", "t1"), _tool("t1"), _asst("B", "t2"), _tool("t2")]


def _assistants(out):
    return [m for m in out if m["role"] == "assistant"]


def test_wire_replays_every_stored_thinking_block(monkeypatch):
    monkeypatch.delenv("AGENT_PRUNE_OLD_THINKING", raising=False)
    _, out, _ = openai_to_anthropic(_history(), None, model=M)
    first, second = _assistants(out)
    assert first["content"][0] == {"type": "thinking", "thinking": "A", "signature": "sig-A"}
    assert first["content"][1]["type"] == "tool_use"
    assert second["content"][0] == {"type": "thinking", "thinking": "B", "signature": "sig-B"}
    assert second["content"][1]["type"] == "tool_use"


def test_the_wire_is_byte_stable_as_the_conversation_grows(monkeypatch):
    # what the cache sees: request N's assistant turns reappear unchanged in request N+1
    monkeypatch.delenv("AGENT_PRUNE_OLD_THINKING", raising=False)
    h = _history()
    _, before, _ = openai_to_anthropic(h[:3], None, model=M)
    _, after, _ = openai_to_anthropic(h, None, model=M)
    assert after[:len(before)] == before


def test_the_env_flag_only_concerns_the_checkpoint(monkeypatch):
    monkeypatch.setenv("AGENT_PRUNE_OLD_THINKING", "0")
    _, out, _ = openai_to_anthropic(_history(), None, model=M)
    first, second = _assistants(out)
    assert first["content"][0]["thinking"] == "A" and second["content"][0]["thinking"] == "B"


def test_checkpoint_copy_is_pruned_and_the_live_list_is_not(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PRUNE_OLD_THINKING", raising=False)
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(tmp_path / "cp.json"))
    history = _history()
    before = copy.deepcopy(history)
    agent._save_checkpoint(history, {"text": "", "up_to": 0}, 3, None)
    assert history == before, "the live history keeps every thinking block"
    saved = json.loads((tmp_path / "cp.json").read_text())["conversation_history"]
    a1, a2 = [m for m in saved if m["role"] == "assistant"]
    assert THINKING_KEY not in a1
    assert a2[THINKING_KEY]["blocks"][0]["thinking"] == "B"


def test_prune_leaves_a_history_without_assistant_turns_alone():
    h = [{"role": "user", "content": "go"}]
    assert agent._prune_old_thinking(h) == h
