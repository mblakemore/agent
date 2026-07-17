"""Tests for the system-message fold workaround (llm_backend).

Covers backends that silently drop role:"system" — see
/droid/repos/test/aws-proxy-bugs.md §1.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import llm_backend as lb


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep probe cache/memo out of the developer's real ~/.cache."""
    monkeypatch.setenv("AGENT_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_FOLD_SYSTEM", raising=False)
    lb._probe_memo.clear()
    yield
    lb._probe_memo.clear()


# ── fold_system_messages ────────────────────────────────────────────────

def test_fold_merges_system_into_next_user():
    out = lb.fold_system_messages([
        {"role": "system", "content": "BE TERSE"},
        {"role": "user", "content": "hello"},
    ])
    assert out == [{"role": "user", "content": "BE TERSE\n\nhello"}]


def test_fold_preserves_order_of_multiple_system_messages():
    out = lb.fold_system_messages([
        {"role": "system", "content": "first"},
        {"role": "system", "content": "second"},
        {"role": "user", "content": "q"},
    ])
    assert out[0]["content"] == "first\n\nsecond\n\nq"
    assert len(out) == 1


def test_fold_handles_developer_role():
    out = lb.fold_system_messages([
        {"role": "developer", "content": "RULE"},
        {"role": "user", "content": "q"},
    ])
    assert out == [{"role": "user", "content": "RULE\n\nq"}]


def test_fold_attaches_to_next_user_not_a_later_one():
    out = lb.fold_system_messages([
        {"role": "system", "content": "S"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "two"},
    ])
    assert out[0]["content"] == "S\n\none"
    assert out[2]["content"] == "two"   # untouched


def test_fold_with_no_following_user_emits_user_message():
    out = lb.fold_system_messages([
        {"role": "assistant", "content": "a"},
        {"role": "system", "content": "trailing"},
    ])
    assert out[-1] == {"role": "user", "content": "trailing"}


def test_fold_preserves_tool_messages_and_assistant_tool_calls():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]
    out = lb.fold_system_messages(msgs)
    assert out[1]["tool_calls"] == [{"id": "1"}]
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "1"


def test_fold_into_block_list_user_content_keeps_blocks():
    out = lb.fold_system_messages([
        {"role": "system", "content": "S"},
        {"role": "user", "content": [{"type": "image", "url": "x"}]},
    ])
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "S"}
    assert blocks[1] == {"type": "image", "url": "x"}   # not stringified


def test_fold_is_noop_without_system():
    msgs = [{"role": "user", "content": "hi"}]
    assert lb.fold_system_messages(msgs) == msgs


def test_fold_skips_empty_system_content():
    out = lb.fold_system_messages([
        {"role": "system", "content": "   "},
        {"role": "user", "content": "hi"},
    ])
    assert out == [{"role": "user", "content": "hi"}]


def test_fold_does_not_mutate_input():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}]
    lb.fold_system_messages(msgs)
    assert msgs[0]["role"] == "system" and msgs[1]["content"] == "u"


# ── probe ───────────────────────────────────────────────────────────────

class _SSEResponse:
    """requests.Response-like: SSE bytes via iter_lines (the llamacpp shape)."""

    def __init__(self, text):
        frames = [
            b"data: " + json.dumps(
                {"choices": [{"delta": {"content": ch}, "finish_reason": None}]}
            ).encode()
            for ch in text
        ]
        self._lines = frames + [b"data: [DONE]"]

    def iter_lines(self):
        return iter(self._lines)


class _SSEBackend:
    """Mimics LlamacppBackend: stream_chat -> Response-like."""
    base_url = "http://fake-sse"
    model = "m"

    def __init__(self, reply):
        self.reply = reply
        self.sent = None

    def stream_chat(self, log, **kw):
        self.sent = kw.get("json")
        return _SSEResponse(self.reply)


class _GenBackend:
    """Mimics BedrockBackend: stream_chat -> generator of delta dicts."""
    base_url = "http://fake-gen"
    model = "m"

    def __init__(self, reply):
        self.reply = reply

    def stream_chat(self, log, **kw):
        def gen():
            for ch in self.reply:
                yield {"choices": [{"delta": {"content": ch}, "finish_reason": None}]}
        return gen()


class _RaisingBackend:
    base_url = "http://fake-boom"
    model = "m"

    def stream_chat(self, log, **kw):
        raise RuntimeError("backend down")


def test_probe_detects_system_support_sse_shape():
    b = _SSEBackend(f"{lb._PROBE_NONCE}")
    assert lb.backend_supports_system(b) is True


def test_probe_detects_dropped_system_sse_shape():
    b = _SSEBackend("2 + 2 = 4")          # nonce absent -> system was dropped
    assert lb.backend_supports_system(b) is False


def test_probe_detects_system_support_generator_shape():
    b = _GenBackend(f"{lb._PROBE_NONCE}")
    assert lb.backend_supports_system(b) is True


def test_probe_fails_safe_on_exception():
    """A broken backend must fold, not assume system works."""
    assert lb.backend_supports_system(_RaisingBackend()) is False


def test_probe_sends_a_system_message():
    b = _SSEBackend(lb._PROBE_NONCE)
    lb.backend_supports_system(b)
    roles = [m["role"] for m in b.sent["messages"]]
    assert "system" in roles


def test_probe_disables_thinking_and_leaves_room_for_nonce():
    """Regression: a reasoning model (Qwen3) with max_tokens=8 and thinking on
    returns finish=length + empty content, so the probe would false-negative.
    The probe must disable thinking and request enough tokens."""
    b = _SSEBackend(lb._PROBE_NONCE)
    lb.backend_supports_system(b)
    assert b.sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert b.sent["max_tokens"] >= 32


def test_probe_is_cached_across_calls(tmp_path):
    b = _SSEBackend(lb._PROBE_NONCE)
    assert lb.backend_supports_system(b) is True
    lb._probe_memo.clear()                 # force disk-cache path
    b2 = _SSEBackend("wrong")              # would probe False if re-probed
    b2.base_url = b.base_url               # same cache key
    assert lb.backend_supports_system(b2) is True
    assert b2.sent is None                 # never probed again


def test_probe_cache_expires(monkeypatch):
    b = _SSEBackend(lb._PROBE_NONCE)
    assert lb.backend_supports_system(b) is True
    lb._probe_memo.clear()
    monkeypatch.setattr(lb, "_PROBE_TTL_SECONDS", -1)   # everything is stale
    b2 = _SSEBackend("wrong")
    b2.base_url = b.base_url
    assert lb.backend_supports_system(b2) is False      # re-probed
    assert b2.sent is not None


# ── maybe_fold_system ───────────────────────────────────────────────────

MSGS = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}]


def test_maybe_fold_folds_when_system_dropped():
    out = lb.maybe_fold_system(list(MSGS), _SSEBackend("nope"))
    assert out == [{"role": "user", "content": "S\n\nu"}]


def test_maybe_fold_noop_when_system_honoured():
    out = lb.maybe_fold_system(list(MSGS), _SSEBackend(lb._PROBE_NONCE))
    assert any(m["role"] == "system" for m in out)


def test_maybe_fold_never_mode_skips_probe(monkeypatch):
    monkeypatch.setenv("AGENT_FOLD_SYSTEM", "never")
    b = _SSEBackend("nope")
    out = lb.maybe_fold_system(list(MSGS), b)
    assert any(m["role"] == "system" for m in out)
    assert b.sent is None                  # no probe request at all


def test_maybe_fold_always_mode_folds_without_probing(monkeypatch):
    monkeypatch.setenv("AGENT_FOLD_SYSTEM", "always")
    b = _SSEBackend(lb._PROBE_NONCE)       # would report "honoured" if probed
    out = lb.maybe_fold_system(list(MSGS), b)
    assert out == [{"role": "user", "content": "S\n\nu"}]
    assert b.sent is None


def test_maybe_fold_skips_probe_when_no_system_present():
    b = _SSEBackend("nope")
    msgs = [{"role": "user", "content": "u"}]
    assert lb.maybe_fold_system(list(msgs), b) == msgs
    assert b.sent is None                  # no system -> nothing to detect
