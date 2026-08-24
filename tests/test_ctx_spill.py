"""Context-overflow stage-1 spill (board#209, job#208 root cause).

The overflow recovery used to go straight to message trimming — which loses whole
recent messages while the actual bloat is usually raw data echoed into role:'tool'
contents (job#208: price histories accumulated past a 196k window, 10 reductions,
fatal). Stage 1 now moves oversized tool payloads to files under .agent/spill/ and
leaves a reference + preview; trimming runs only when a spill pass finds nothing.

Harness notes (B0.1 fixture rule): advisor pinned off, _llm_request patched at the
wrapper the loop calls, cwd moved to tmp_path so spill files never land in the repo.
"""
import json
import logging
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent as _agent
from agent import ContextOverflowError

log = logging.getLogger("test_ctx_spill")


BIG = "PRICE,VOL\n" + ("2024-08-01,123.45,678900\n" * 400)   # ~10k chars of bulk data


def _tool_msg(content, name="market_data", call_id="c1"):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


# ---------------------------------------------------------------- unit: the spill pass
class TestSpillPass:
    def test_oversized_tool_content_moves_to_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        hist = [{"role": "user", "content": "do work"}, _tool_msg(BIG)]
        n = _agent._spill_oversized_tool_results(hist, log, threshold=4000)
        assert n == 1
        content = hist[1]["content"]
        assert content.startswith(_agent._SPILL_MARKER)
        # reference names a real file holding the ORIGINAL bytes
        fpath = content.split("]", 1)[0][len(_agent._SPILL_MARKER):]
        assert os.path.isfile(fpath)
        assert open(fpath).read() == BIG
        # preview keeps head and tail for orientation
        assert BIG[:50] in content and BIG[-50:] in content
        # and the message actually shrank — the point of the exercise
        assert len(content) < len(BIG) // 4

    def test_small_and_nontool_content_untouched(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        hist = [{"role": "user", "content": "x" * 50000},          # user bloat: not ours
                _tool_msg("small result"),
                {"role": "assistant", "content": "y" * 50000}]
        n = _agent._spill_oversized_tool_results(hist, log, threshold=4000)
        assert n == 0
        assert hist[0]["content"] == "x" * 50000
        assert hist[1]["content"] == "small result"

    def test_idempotent_marker_skip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        hist = [_tool_msg(BIG)]
        assert _agent._spill_oversized_tool_results(hist, log, threshold=4000) == 1
        # second pass finds only the (small) reference — and even a huge already-spilled
        # message is protected by the marker check, so re-spilling can never nest.
        assert _agent._spill_oversized_tool_results(hist, log, threshold=4000) == 0

    def test_nonstring_content_untouched(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        weird = _tool_msg([{"type": "text", "text": "multimodal-ish"}])
        hist = [weird]
        assert _agent._spill_oversized_tool_results(hist, log, threshold=10) == 0
        assert isinstance(hist[0]["content"], list)

    def test_write_failure_leaves_content_inline(self, tmp_path, monkeypatch):
        """A failed spill must not eat the content (fail toward the old behavior)."""
        monkeypatch.chdir(tmp_path)
        hist = [_tool_msg(BIG)]

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(_agent.os, "makedirs", boom)
        n = _agent._spill_oversized_tool_results(hist, log, threshold=4000)
        assert n == 0
        assert hist[0]["content"] == BIG


# ------------------------------------------------- integration: handler ordering
def _sse(lines):
    class R:
        status_code = 200

        def iter_lines(self):
            return iter([ln.encode() for ln in lines])
    return R()


def _text(content):
    return _sse([f'data: {{"choices": [{{"delta": {{"content": "{content}"}}}}]}}',
                 "data: [DONE]"])


class TestOverflowHandlerOrdering:
    def test_spill_runs_before_any_trimming(self, tmp_path, monkeypatch):
        """First overflow: stage 1 spills and retries with ALL messages intact —
        no reduction attempt is spent on trimming while spill candidates exist."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setitem(_agent._config, "advisor", {"enabled": False})
        history = [
            {"role": "user", "content": "Analyze the data."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "market_data", "arguments": "{}"}}]},
            _tool_msg(BIG, call_id="c1"),
        ]
        n_before = len(history)
        calls = {"n": 0}

        def llm(_log, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ContextOverflowError("test overflow")
            return _text("ok done")

        with patch("agent._llm_request", side_effect=llm), \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)

        # the retry succeeded on call 2
        assert calls["n"] >= 2
        # the bulk payload was spilled, not dropped: message still PRESENT, content replaced
        tool_msgs = [m for m in history[:n_before] if m.get("role") == "tool"]
        assert tool_msgs and tool_msgs[0]["content"].startswith(_agent._SPILL_MARKER)
        spill_dir = os.path.join(str(tmp_path), _agent._SPILL_DIR)
        assert os.path.isdir(spill_dir) and os.listdir(spill_dir)

    def test_trimming_still_reachable_when_nothing_to_spill(self, tmp_path, monkeypatch):
        """Overflow with NO oversized tool content falls through to stage 2 (the
        pre-existing trim path) and still recovers — the fallback must still exist."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setitem(_agent._config, "advisor", {"enabled": False})
        history = [{"role": "user", "content": f"msg {i}"} for i in range(6)]
        calls = {"n": 0}

        def llm(_log, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ContextOverflowError("test overflow")
            return _text("ok")

        with patch("agent._llm_request", side_effect=llm), \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)

        assert calls["n"] >= 2
        # nothing was spilled (no tool messages) — no spill dir appears
        assert not os.path.isdir(os.path.join(str(tmp_path), _agent._SPILL_DIR))
