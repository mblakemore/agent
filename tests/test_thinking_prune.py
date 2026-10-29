"""Only the newest assistant turn's thinking is replayed and checkpointed.

The API needs that turn's thinking ahead of its tool_use when a tool result follows,
and strips (without billing) thinking from every earlier turn. Replaying all of it
was 595k of a 1,217k-character checkpoint on one measured run, written to disk every
turn and sent with every request. AGENT_PRUNE_OLD_THINKING=0 restores the old behaviour.
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


def test_wire_replays_thinking_for_the_newest_assistant_turn_only(monkeypatch):
    monkeypatch.delenv("AGENT_PRUNE_OLD_THINKING", raising=False)
    _, out, _ = openai_to_anthropic(_history(), None, model=M)
    first, second = _assistants(out)
    assert first["content"][0]["type"] == "tool_use"
    assert second["content"][0] == {"type": "thinking", "thinking": "B", "signature": "sig-B"}
    assert second["content"][1]["type"] == "tool_use"


def test_env_flag_restores_full_replay(monkeypatch):
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
