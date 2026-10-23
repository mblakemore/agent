"""anthropic_wire: the one OpenAI ↔ Anthropic converter (aicommons-integration.md P1, §7 H3/H4/H6, E1)."""

import json
import random

import pytest

import anthropic_wire as w
from anthropic_wire import THINKING_KEY, AnthropicStreamToDeltas, openai_to_anthropic


def _tc(tid, name="read_file", args='{"path": "a.py"}'):
    return {"id": tid, "type": "function", "function": {"name": name, "arguments": args}}


def _assert_valid(messages):
    """What a Converse-backed gateway demands: user first, strict alternation, no empty
    content, every tool_use answered in the next turn and every result answering one."""
    assert messages and messages[0]["role"] == "user"
    for a, b in zip(messages, messages[1:]):
        assert a["role"] != b["role"], (a, b)
    for i, m in enumerate(messages):
        assert m["content"], m
        for blk in m["content"]:
            if blk.get("type") == "text":
                assert blk["text"].strip()
        uses = {b["id"] for b in m["content"] if b.get("type") == "tool_use"}
        if m["role"] == "assistant" and uses:
            nxt = messages[i + 1]
            got = {b["tool_use_id"] for b in nxt["content"] if b.get("type") == "tool_result"}
            assert uses == got
        if m["role"] == "user":
            prev = messages[i - 1] if i else {"content": []}
            prev_uses = {b["id"] for b in prev["content"] if b.get("type") == "tool_use"}
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    assert b["tool_use_id"] in prev_uses


# ── openai_to_anthropic ─────────────────────────────────────────────────


def test_every_system_message_is_kept_in_order():
    """H6: the old converter kept only the LAST system message, losing the main prompt."""
    system, msgs, _ = openai_to_anthropic([
        {"role": "system", "content": "MAIN PROMPT"},
        {"role": "system", "content": "memory notes"},
        {"role": "user", "content": "hi"},
        {"role": "developer", "content": "late rule"},
    ])
    assert system == "MAIN PROMPT\n\nmemory notes\n\nlate rule"
    assert msgs == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_parallel_results_share_one_user_message_and_lead_it():
    _, msgs, _ = openai_to_anthropic([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("t1"), _tc("t2")]},
        {"role": "tool", "tool_call_id": "t1", "content": "one"},
        {"role": "tool", "tool_call_id": "t2", "content": "two"},
        {"role": "user", "content": "[runtime note: clock 12:00]"},
    ])
    _assert_valid(msgs)
    last = msgs[-1]["content"]
    assert [b["type"] for b in last] == ["tool_result", "tool_result", "text"]
    assert [b["tool_use_id"] for b in last[:2]] == ["t1", "t2"]


def test_consecutive_user_turns_and_empty_assistant_are_merged_away():
    """H3: tool results + runtime note, an empty assistant then a nudge, a text-cap
    correction — all produce same-role runs the gateway would reject."""
    _, msgs, _ = openai_to_anthropic([
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "nudge: call a tool"},
        {"role": "user", "content": "[SYSTEM: cut off]"},
    ])
    _assert_valid(msgs)
    assert len(msgs) == 1
    assert [b["text"] for b in msgs[0]["content"]] == ["task", "nudge: call a tool", "[SYSTEM: cut off]"]


def test_first_message_is_always_a_user_turn():
    _, msgs, _ = openai_to_anthropic([
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "summary of earlier work"},
        {"role": "user", "content": "continue"},
    ])
    _assert_valid(msgs)
    assert msgs[0]["role"] == "user"


def test_orphaned_result_dropped_and_unanswered_use_gets_placeholder():
    _, msgs, _ = openai_to_anthropic([
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "gone", "content": "orphan"},
        {"role": "assistant", "content": "x", "tool_calls": [_tc("t1"), _tc("t2")]},
        {"role": "tool", "tool_call_id": "t1", "content": "one"},
        {"role": "user", "content": "next"},
    ])
    _assert_valid(msgs)
    results = [b for b in msgs[-1]["content"] if b["type"] == "tool_result"]
    by_id = {b["tool_use_id"]: b for b in results}
    assert set(by_id) == {"t1", "t2"}
    assert by_id["t2"]["is_error"] is True
    assert "orphan" not in json.dumps(msgs)


def test_truncated_tool_arguments_do_not_raise():
    """H4: half-written JSON used to raise on every later request."""
    _, msgs, _ = openai_to_anthropic([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("t1", "write_file", '{"path": "a.py", "content": "def f(')]},
        {"role": "tool", "tool_call_id": "t1", "content": "Error: bad args"},
    ])
    _assert_valid(msgs)
    use = next(b for b in msgs[1]["content"] if b["type"] == "tool_use")
    assert use["input"] == {}
    assert any(b["type"] == "text" and "not valid JSON" in b["text"] for b in msgs[1]["content"])


def test_error_results_are_flagged():
    """E1."""
    _, msgs, _ = openai_to_anthropic([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("t1"), _tc("t2")]},
        {"role": "tool", "tool_call_id": "t1", "content": "Error: no such file"},
        {"role": "tool", "tool_call_id": "t2", "content": "fine"},
    ])
    r1, r2 = msgs[-1]["content"]
    assert r1.get("is_error") is True
    assert "is_error" not in r2


def test_thinking_goes_back_only_to_the_model_that_made_it():
    blocks = [{"type": "thinking", "thinking": "hmm", "signature": "sig"}]
    hist = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("t1")],
         THINKING_KEY: {"model": "claude-v5.5-opus", "blocks": blocks}},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    _, same, _ = openai_to_anthropic(hist, model="claude-v5.5-opus")
    assert same[1]["content"][0] == blocks[0]
    assert [b["type"] for b in same[1]["content"]] == ["thinking", "tool_use"]
    _, other, _ = openai_to_anthropic(hist, model="claude-v5.5-haiku")
    assert [b["type"] for b in other[1]["content"]] == ["tool_use"]


def test_end_with_user_never_leaves_a_prefill():
    _, msgs, _ = openai_to_anthropic(
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], end_with_user=True)
    assert msgs[-1]["role"] == "user"
    _, msgs, _ = openai_to_anthropic(
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert msgs[-1]["role"] == "assistant"


def test_image_data_uri_becomes_an_image_block():
    _, msgs, _ = openai_to_anthropic([{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}])
    assert msgs[0]["content"][1] == {
        "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}


def test_tools_are_converted_and_sorted_by_name():
    tools = [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
             for n in ("write_file", "exec_command", "read_file")]
    _, _, out = openai_to_anthropic([{"role": "user", "content": "x"}], tools)
    assert [t["name"] for t in out] == ["exec_command", "read_file", "write_file"]
    assert out[0]["input_schema"] == {"type": "object"}


@pytest.mark.parametrize("seed", range(200))
def test_random_histories_always_produce_a_valid_request(seed):
    """Property (H3): whatever the agent's history looks like, the output alternates."""
    rnd = random.Random(seed)
    hist, open_ids, n = [], [], 0
    for _ in range(rnd.randint(0, 14)):
        k = rnd.choice(["user", "user_empty", "assistant", "assistant_empty", "tools", "result",
                        "orphan", "system"])
        if k == "user":
            hist.append({"role": "user", "content": f"u{n}"})
        elif k == "user_empty":
            hist.append({"role": "user", "content": ""})
        elif k == "assistant":
            hist.append({"role": "assistant", "content": f"a{n}"})
        elif k == "assistant_empty":
            hist.append({"role": "assistant", "content": ""})
        elif k == "tools":
            ids = [f"t{n}_{j}" for j in range(rnd.randint(1, 3))]
            hist.append({"role": "assistant", "content": rnd.choice(["", "doing it"]),
                         "tool_calls": [_tc(i) for i in ids]})
            open_ids = ids
        elif k == "result" and open_ids:
            hist.append({"role": "tool", "tool_call_id": open_ids.pop(0), "content": "r"})
        elif k == "orphan":
            hist.append({"role": "tool", "tool_call_id": "nope", "content": "r"})
        elif k == "system":
            hist.append({"role": "system", "content": f"s{n}"})
        n += 1
    _, msgs, _ = openai_to_anthropic(hist, end_with_user=True)
    _assert_valid(msgs)
    assert msgs[-1]["role"] == "user"


# ── AnthropicStreamToDeltas ─────────────────────────────────────────────


def _events_tool_turn(stop="tool_use", close_tool=True):
    ev = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 999, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Reading."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": ' "a"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "content_block_start", "index": 3,
         "content_block": {"type": "tool_use", "id": "toolu_2", "name": "list_dir", "input": {}}},
        {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": '{"p'}},
    ]
    if close_tool:
        ev += [{"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": '": "."}'}},
               {"type": "content_block_stop", "index": 3}]
    ev += [
        {"type": "message_delta", "delta": {"stop_reason": stop},
         "usage": {"input_tokens": 10, "output_tokens": 40,
                   "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}},
        {"type": "message_stop"},
    ]
    return ev


def test_stream_translation_text_thinking_parallel_tools_usage():
    tr = AnthropicStreamToDeltas(model="m")
    chunks = [c for e in _events_tool_turn() for c in tr.feed(e)]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert {"reasoning_content": "plan"} in deltas
    assert {"content": "Reading."} in deltas
    calls = {}
    for d in deltas:
        for tc in d.get("tool_calls") or []:
            slot = calls.setdefault(tc["index"], {"id": "", "name": "", "args": ""})
            slot["id"] = tc.get("id") or slot["id"]
            slot["name"] = (tc.get("function") or {}).get("name") or slot["name"]
            slot["args"] += (tc.get("function") or {}).get("arguments") or ""
    assert calls == {0: {"id": "toolu_1", "name": "read_file", "args": '{"path": "a"}'},
                     1: {"id": "toolu_2", "name": "list_dir", "args": '{"p": "."}'}}
    final = tr.final_chunk()
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"]["prompt_tokens"] == 960      # uncached + read + write; NOT the start estimate
    assert final["usage"]["cache_read_input_tokens"] == 900
    assert final["usage"]["completion_tokens"] == 40
    assert final[THINKING_KEY] == {"model": "m", "blocks": [
        {"type": "thinking", "thinking": "plan", "signature": "SIG"}]}


def test_stream_cut_at_max_tokens_mid_tool_reports_length():
    tr = AnthropicStreamToDeltas()
    for e in _events_tool_turn(stop="max_tokens", close_tool=False):
        tr.feed(e)
    assert tr.truncated_tool_indices == [1]
    assert tr.final_chunk()["choices"][0]["finish_reason"] == "length"


def test_stream_without_message_stop_is_not_a_clean_stop():
    tr = AnthropicStreamToDeltas()
    tr.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "hi"}})
    assert tr.final_chunk()["choices"][0]["finish_reason"] == "length"


def test_redacted_thinking_is_kept_verbatim():
    tr = AnthropicStreamToDeltas(model="m")
    tr.feed({"type": "content_block_start", "index": 0,
             "content_block": {"type": "redacted_thinking", "data": "ENC"}})
    tr.feed({"type": "message_stop"})
    assert tr.final_chunk()[THINKING_KEY]["blocks"] == [{"type": "redacted_thinking", "data": "ENC"}]


# ── gateway direction stays as cc_gateway had it ────────────────────────


def test_anthropic_to_openai_round_trip_shape():
    out = w.anthropic_to_openai("sys", [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "text", "text": "t"},
                                          {"type": "tool_use", "id": "a", "name": "n", "input": {"x": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "r"}]},
    ])
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[2]["tool_calls"][0]["function"] == {"name": "n", "arguments": '{"x": 1}'}
    assert out[3] == {"role": "tool", "tool_call_id": "a", "content": "r"}
