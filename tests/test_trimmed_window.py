"""Context-window discipline on a prompt-caching backend.

Measured on a 2.5 h session before these changes: 225 of 310 requests missed the
server's prompt cache (18M tokens re-written at the cache-write rate) because the
tool-result compression pass, gated on the pressure of the WHOLE stored history,
rewrote a message inside the sent window on nearly every turn; no summary ever
fired because the setup wizard had written summary_threshold in characters; and
the identity file (@CLAUDE.md) left the context at the first chunk eviction and
never came back. Each pin below is one of those three.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

import agent

log = logging.getLogger("test_trimmed_window")


def _hist(n):
    out = [{"role": "user", "content": "task"}]
    for i in range(n):
        out.append({"role": "assistant", "content": f"step {i}"} if i % 2 == 0
                   else {"role": "user", "content": f"result {i}"})
    return out


def _big(i):
    return f"result {i} " + ("x" * 400)


def _tool_hist(n):
    h = [{"role": "user", "content": "go"}]
    for i in range(n):
        h.append({"role": "assistant", "content": "", "tool_calls": []})
        h.append({"role": "tool", "tool_call_id": f"t{i}", "name": "exec_command", "content": _big(i)})
    return h


@pytest.fixture
def uncalibrated(monkeypatch):
    monkeypatch.setattr(agent, "_TOKEN_CAL", {"ratio": 1.0, "n": 0, "last_logged": 1.0})
    monkeypatch.setattr(agent, "_TOOLS_TOKENS", 0)


# ── summary_threshold is a message count ────────────────────────────────────

class TestSaneSummaryThreshold:
    def test_a_chars_value_becomes_a_fifth_of_the_message_cap(self):
        lg = MagicMock()
        got = agent._sane_summary_threshold({"summary_threshold": 432000, "max_context_messages": 375}, lg)
        assert got == 75
        args = lg.warning.call_args[0]
        assert "432000" in (args[0] % args[1:]) and "75" in (args[0] % args[1:])

    def test_a_message_count_passes_through_silently(self):
        lg = MagicMock()
        assert agent._sane_summary_threshold({"summary_threshold": 5, "max_context_messages": 30}, lg) == 5
        lg.warning.assert_not_called()

    def test_the_floor_is_five_messages(self):
        assert agent._sane_summary_threshold({"summary_threshold": 999, "max_context_messages": 20},
                                             MagicMock()) == 5


def _kick_fires(monkeypatch, kind, util_threshold):
    """Drive one turn and report whether the async summarizer was kicked."""
    history = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
    summary_state = {"text": "old summary", "up_to": 0}
    summarizer = MagicMock()
    summarizer.is_running = False
    resp = MagicMock()
    resp.iter_lines.return_value = [b"data: [DONE]"]
    monkeypatch.setattr(agent, "_TOOLS_TOKENS", None)
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind=kind, model="m", honours_system=True, base_url="http://x"))
    with patch("agent._llm_request", return_value=resp), patch("agent._emit"), \
         patch("agent._save_checkpoint"), patch("agent._NUDGE_ENABLED", False), \
         patch.dict("os.environ", {"AGENT_SUMMARY_UTIL_THRESHOLD": util_threshold}), \
         patch("agent._SUMMARY_THRESHOLD", 1), \
         patch("agent._build_context", return_value=([{"role": "user", "content": "test"}], 5)):
        agent.run_agent_single(history, summary_state, [], MagicMock(), async_summarizer=summarizer)
    return summarizer.kick.called


class TestSummaryGate:
    def test_utilization_gate_is_skipped_on_a_caching_backend(self, monkeypatch):
        # Messages leave a chunk-evicting window only under pressure; right after the
        # eviction the window is ~20% full, so a utilization gate blocked every summary.
        assert _kick_fires(monkeypatch, "aicommons", "1.0")

    def test_utilization_gate_still_holds_on_a_sliding_window_backend(self, monkeypatch):
        assert not _kick_fires(monkeypatch, "llamacpp", "1.0")


# ── the identity file leads every trimmed window ────────────────────────────

class TestTrimmedWindowLead:
    def test_lead_message_is_byte_stable(self):
        a, b = agent._build_initial_files_msg("IDENTITY"), agent._build_initial_files_msg("IDENTITY")
        assert a == b and a["role"] == "user" and a["content"].startswith("IDENTITY")

    def test_identity_file_leads_a_trimmed_window_without_a_summary(self, monkeypatch):
        monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 10)
        h = _hist(40)
        sel, oldest = agent._build_context(h, {"text": "", "up_to": 0}, "IDENTITY FILE", 1_000_000, 4096, log)
        assert oldest > 0
        assert sel[0]["content"].startswith("IDENTITY FILE")
        assert sel[1:] == h[oldest:]

    def test_an_untrimmed_window_has_no_lead(self):
        sel, oldest = agent._build_context(_hist(4), {"text": "", "up_to": 0}, "IDENTITY FILE",
                                           1_000_000, 4096, log)
        assert oldest == 0
        assert not any(m["content"].startswith("IDENTITY FILE") for m in sel)

    def test_lead_tokens_count_on_the_greedy_fill_and_on_the_cached_head_path(self, monkeypatch, uncalibrated):
        # ctx 1000, max_tokens 100 → budget 388 estimator tokens; messages cost 10, the lead 100.
        monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 100)
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons"))
        monkeypatch.setattr(agent, "_estimate_tokens",
                            lambda m: 100 if str(m.get("content", "")).startswith("IDENTITY") else 10)
        h, state = _hist(60), {"text": "", "up_to": 0}
        sel, first = agent._build_context(h, state, "IDENTITY FILE", 1000, 100, log)
        # greedy fill to 60% of the budget (232): 13 messages + the 100-token lead, not 23
        assert len(sel) == 14 and sel[0]["content"].startswith("IDENTITY FILE")
        moved_after = None
        for k in range(1, 40):
            h.append({"role": "assistant", "content": f"more {k}"})
            _, oldest = agent._build_context(h, state, "IDENTITY FILE", 1000, 100, log)
            if oldest != first:
                moved_after = k
                break
        # the head holds while 10·n + 100 ≤ 388 → n ≤ 28 → it moves at the 29th message
        assert moved_after == 16, moved_after


# ── compression: window pressure, protected window, no model calls below it ──

class TestCompressionOnACachingBackend:
    def test_pressure_is_over_the_window_not_the_stored_history(self, monkeypatch, uncalibrated):
        monkeypatch.setitem(agent._config["context"], "ctx_size", 1000)
        monkeypatch.setattr(agent, "_estimate_tokens", lambda m: 100)
        h = [{"role": "user", "content": "x"} for _ in range(20)]
        assert agent._history_pressure(h) == pytest.approx(2.0)
        assert agent._history_pressure(h, start=15) == pytest.approx(0.5)

    def test_window_is_never_rewritten_and_nothing_below_it_calls_the_model(self):
        h = _tool_hist(6)                      # tool results at 2, 4, 6, 8, 10, 12
        with patch("agent._summarize_for_compression",
                   side_effect=AssertionError("model compression for text that is never sent")):
            changed = agent._compress_repeated_tool_results(
                h, "exec_command", MagicMock(), pressure=0.95, protect_from=7, local_only=True)
        assert changed == [6, 4, 2]
        assert all(h[i]["content"].startswith("[compressed") for i in (2, 4, 6))
        assert not any(h[i]["content"].startswith("[compressed") for i in (8, 10, 12))

    def test_legacy_call_keeps_model_compression_and_reports_what_changed(self):
        h = _tool_hist(6)
        calls = []
        with patch("agent._summarize_for_compression",
                   lambda c, n, lg: calls.append(n) or "[compressed by summary: stub]"):
            changed = agent._compress_repeated_tool_results(h, "exec_command", MagicMock(), pressure=0.95)
        assert changed == [10, 8, 6, 4, 2]
        assert len(calls) == agent._TOOL_RESULT_COMPRESS_MODEL_MAX

    def test_an_eviction_raises_the_head_moved_flag(self, monkeypatch):
        monkeypatch.setattr(agent, "_main_backend", MagicMock(kind="aicommons"))
        monkeypatch.setattr(agent, "_MAX_CONTEXT_MESSAGES", 10)
        state, h = {"text": "", "up_to": 0}, _hist(40)
        _, first = agent._build_context(h, state, "", 1_000_000, 4096, log)
        assert "_head_moved" not in state, "the first window is not an eviction"
        for _ in range(30):
            h.append({"role": "assistant", "content": "more"})
            _, oldest = agent._build_context(h, state, "", 1_000_000, 4096, log)
            if oldest != first:
                break
        assert state.get("_head_moved") is True


# ── every miss names its cause ───────────────────────────────────────────────

def _tool_turn_with_usage(tid, read, write, uncached):
    call = {"index": 0, "id": tid, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": read + write + uncached, "completion_tokens": 5,
                       "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
                       "input_tokens": uncached}}]


def test_cache_misses_are_logged_with_a_reason(agent_loop_mocks, monkeypatch):
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model="m", honours_system=True, base_url="http://x"))
    agent_loop_mocks.stub_tool("read_file", "contents")
    agent_loop_mocks.script(_tool_turn_with_usage("t1", 0, 900, 2),      # first request: nothing to hit
                            _tool_turn_with_usage("t2", 900, 30, 2),     # hit
                            _tool_turn_with_usage("t3", 0, 950, 2))      # miss on an unchanged prefix
    lg = MagicMock()
    agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], lg)
    misses = [c.args[0] % c.args[1:] for c in lg.info.call_args_list
              if c.args and str(c.args[0]).startswith("cache.miss")]
    assert len(misses) == 2, misses
    # the third request resends the second one byte for byte: the miss is the server's
    assert "reason=first" in misses[0] and "reason=provider" in misses[1]


def test_a_miss_on_a_prefix_we_changed_stays_unknown(agent_loop_mocks, monkeypatch):
    from tools import MAP_FN
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model="m", honours_system=True, base_url="http://x"))
    history = [{"role": "user", "content": "go"}]

    def tool(**kw):
        history[0]["content"] = "go (edited in place)"   # a rewrite no flag reports
        return "contents"
    monkeypatch.setitem(MAP_FN, "read_file", tool)
    agent_loop_mocks.script(_tool_turn_with_usage("t1", 0, 900, 2),
                            _tool_turn_with_usage("t2", 0, 950, 2))
    lg = MagicMock()
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], lg)
    misses = [c.args[0] % c.args[1:] for c in lg.info.call_args_list
              if c.args and str(c.args[0]).startswith("cache.miss")]
    assert len(misses) == 2 and "reason=unknown" in misses[1], misses


class TestRequestSignature:
    def test_appending_keeps_the_prefix(self):
        tools = [{"type": "function", "function": {"name": "f"}}]
        a = agent._request_signature([{"role": "user", "content": "x"}], tools)
        b = agent._request_signature([{"role": "user", "content": "x"},
                                      {"role": "assistant", "content": "y"}], tools)
        assert agent._prefix_unchanged(a, b)
        assert agent._prefix_unchanged(a, a), "the same request twice is a cached prefix"
        assert not agent._prefix_unchanged(b, a), "a shorter request drops cached messages"

    def test_an_edit_a_removal_or_a_tool_change_breaks_it(self):
        tools = [{"type": "function", "function": {"name": "f"}}]
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}]
        a = agent._request_signature(msgs, tools)
        edited = [dict(msgs[0], content="s2"), msgs[1]]
        assert not agent._prefix_unchanged(a, agent._request_signature(edited, tools))
        assert not agent._prefix_unchanged(a, agent._request_signature(msgs[1:], tools))
        assert not agent._prefix_unchanged(a, agent._request_signature(msgs, tools + [{"x": 1}]))
        assert not agent._prefix_unchanged(None, a) and not agent._prefix_unchanged(a, None)

    def test_private_bookkeeping_keys_are_not_part_of_the_prefix(self):
        # `_tokens` (estimate cache) and `_note_turn` are stamped on stored messages after
        # they were first sent; they never reach the wire, so they must not look like an edit
        a = agent._request_signature([{"role": "user", "content": "x"}], [])
        b = agent._request_signature([{"role": "user", "content": "x", "_tokens": 9, "_note_turn": 1}], [])
        assert agent._prefix_unchanged(a, b) and agent._prefix_unchanged(b, a)

    def test_old_thinking_is_not_part_of_the_prefix(self):
        # the wire layer never resends thinking from earlier assistant turns, so its
        # pruning between two requests must not look like a prefix change
        base = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": "y", "reasoning_content": "long think"}]
        pruned = [base[0], {"role": "assistant", "content": "y"}]
        assert agent._prefix_unchanged(agent._request_signature(base, []),
                                       agent._request_signature(pruned, []))


# ── a harvested summary is attributed wherever it was harvested ─────────────

def test_every_harvest_site_marks_the_lead_change(monkeypatch):
    s = agent.AsyncSummarizer({"summary": {"max_wait_on_save": 1}}, MagicMock())
    s._pending_result, s._pending_up_to = "new summary", 7
    state = {"text": "", "up_to": 0}
    with patch("agent._condense_summary", lambda t, lg: t):
        assert s.harvest(state) is True
    assert state["_lead_changed"] is True and state["up_to"] == 7
    assert s.harvest(state) is False, "nothing pending: no second harvest"


def test_a_miss_after_a_harvest_on_any_site_is_attributed(agent_loop_mocks, monkeypatch):
    from tools import MAP_FN
    monkeypatch.setattr(agent, "_main_backend",
                        MagicMock(kind="aicommons", model="m", honours_system=True, base_url="http://x"))
    state = {"text": "", "up_to": 0}

    def tool(**kw):
        state["_lead_changed"] = True     # what a harvest leaves, on whichever site it ran
        return "contents"
    monkeypatch.setitem(MAP_FN, "read_file", tool)
    agent_loop_mocks.script(_tool_turn_with_usage("t1", 0, 900, 2),
                            _tool_turn_with_usage("t2", 0, 950, 2))
    lg = MagicMock()
    agent.run_agent_single([{"role": "user", "content": "go"}], state, [], lg)
    misses = [c.args[0] % c.args[1:] for c in lg.info.call_args_list
              if c.args and str(c.args[0]).startswith("cache.miss")]
    assert len(misses) == 2 and "reason=first" in misses[0]
    assert "reason=summary-harvest" in misses[1], misses
    assert "_lead_changed" not in state, "the flag is consumed by the request it explains"
