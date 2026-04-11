"""Unit tests for callbacks.py — NullCallbacks, TerminalCallbacks, safe_cb."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import callbacks


class TestNullCallbacks(unittest.TestCase):
    def test_all_hooks_return_none(self):
        cb = callbacks.NullCallbacks()
        # Sample from each category — every hook should be a no-op
        self.assertIsNone(cb.check_cancelled())
        self.assertIsNone(cb.on_session_start({}))
        self.assertIsNone(cb.on_api_retry("err", 1, 3, 2.0))
        self.assertIsNone(cb.on_stream_chunk("x"))
        self.assertIsNone(cb.on_assistant_text("txt"))
        self.assertIsNone(cb.on_tool_batch_start(1))
        self.assertIsNone(cb.on_tool_start("t", {}))
        self.assertIsNone(cb.on_tool_result("t", {}, "r", False))
        self.assertIsNone(cb.on_forced_think("t", 1))
        self.assertIsNone(cb.on_overtime("text_only"))
        self.assertIsNone(cb.on_notice("info", "m"))
        self.assertIsNone(cb.on_error("e"))


class TestTerminalCallbacks(unittest.TestCase):
    def test_construction_defaults(self):
        cb = callbacks.TerminalCallbacks()
        self.assertFalse(cb.verbose)
        self.assertEqual(cb.compact_limit, 400)
        self.assertEqual(len(cb.tool_history), 0)
        self.assertEqual(cb.tool_history.maxlen, 50)
        self.assertFalse(cb._last_was_stream)

    def test_tool_history_records_results(self):
        cb = callbacks.TerminalCallbacks()
        # swallow stdout
        cb._print = lambda *a, **kw: None
        cb.on_tool_result("file", {"action": "read"}, "some result", False)
        cb.on_tool_result("exec", {"cmd": "ls"}, "err-output", True)
        self.assertEqual(len(cb.tool_history), 2)
        name, args, result, is_err = cb.tool_history[0]
        self.assertEqual(name, "file")
        self.assertEqual(args, {"action": "read"})
        self.assertEqual(result, "some result")
        self.assertFalse(is_err)
        self.assertTrue(cb.tool_history[1][3])

    def test_tool_history_max_size(self):
        cb = callbacks.TerminalCallbacks(tool_history_size=3)
        cb._print = lambda *a, **kw: None
        for i in range(5):
            cb.on_tool_result(f"t{i}", {}, "r", False)
        self.assertEqual(len(cb.tool_history), 3)
        # Oldest two dropped
        self.assertEqual(cb.tool_history[0][0], "t2")
        self.assertEqual(cb.tool_history[-1][0], "t4")

    def test_compact_args_truncates_long_values(self):
        cb = callbacks.TerminalCallbacks()
        out = cb._compact_args({"path": "x" * 80}, max_val=20)
        self.assertLess(len(out), 60)
        self.assertIn("…", out)

    def test_render_tools_empty(self):
        cb = callbacks.TerminalCallbacks()
        self.assertEqual(cb.render_tools(), "No tool calls yet.")

    def test_render_tools_populated(self):
        cb = callbacks.TerminalCallbacks()
        cb._print = lambda *a, **kw: None
        cb.on_tool_result("file", {"action": "read"}, "line1\nline2", False)
        out = cb.render_tools()
        self.assertIn("file", out)
        self.assertIn("line1", out)

    def test_stream_then_assistant_text_no_double_print(self):
        cb = callbacks.TerminalCallbacks()
        emitted = []
        cb._print = lambda text="", end="\n": emitted.append(text)
        # Chunks go through on_stream_chunk (raw print, bypasses _print)
        cb._last_was_stream = True  # simulate a streamed turn
        cb.on_assistant_text("full text")
        # Should not re-emit via _print — the text was already streamed
        self.assertEqual(emitted, [])
        self.assertFalse(cb._last_was_stream)

    def test_assistant_text_without_stream_prints(self):
        cb = callbacks.TerminalCallbacks()
        emitted = []
        cb._print = lambda text="", end="\n": emitted.append(text)
        cb.on_assistant_text("hello")
        self.assertEqual(emitted, ["hello"])

    def test_verbose_off_compacts_long_results(self):
        cb = callbacks.TerminalCallbacks(compact_limit=20)
        captured = []
        cb._print = lambda text="", end="\n": captured.append(text)
        long_result = "x" * 100
        cb.on_tool_result("t", {}, long_result, False)
        # The compacted display should be shorter than the raw result
        self.assertTrue(any("truncated" in c for c in captured))
        # History keeps the full result (D12)
        self.assertEqual(cb.tool_history[0][2], long_result)

    def test_verbose_on_shows_full_result(self):
        cb = callbacks.TerminalCallbacks(verbose=True, compact_limit=20)
        captured = []
        cb._print = lambda text="", end="\n": captured.append(text)
        long_result = "x" * 100
        cb.on_tool_result("t", {}, long_result, False)
        self.assertTrue(any("x" * 100 in c for c in captured))
        self.assertFalse(any("truncated" in c for c in captured))

    def test_signal_args_surface_in_output(self):
        """Cycle 0017 regression: on_cancelled/on_forced_think/on_text_loop_detected
        must surface every arg they receive — not just print a placeholder.
        See plan/CICD/improvements/0017-callbacks-surface-signal.md."""
        cb = callbacks.TerminalCallbacks()
        captured = []
        cb._print = lambda text="", end="\n": captured.append(text)
        cb.on_cancelled("streaming")
        cb.on_forced_think("exec_command", 3)
        cb.on_text_loop_detected(5)
        joined = "\n".join(captured)
        for token in ("streaming", "exec_command", "3", "5"):
            self.assertIn(token, joined,
                          f"token {token!r} missing from hook output: {joined!r}")


class TestHookWiring(unittest.TestCase):
    """Confirms that the loop emit sites actually reach the installed cb."""

    def _counting_cb(self):
        counts = {}

        class Counter(callbacks.NullCallbacks):
            def on_auto_nudge(self_inner, n, max_n):
                counts.setdefault("nudge", []).append((n, max_n))

            def on_tool_recovery(self_inner, name, attempt):
                counts.setdefault("recovery", []).append((name, attempt))

        return Counter(), counts

    def test_emit_auto_nudge_reaches_cb(self):
        import agent
        cb, counts = self._counting_cb()
        prev_cb, prev_log = agent._cb, agent._cb_log
        agent._cb, agent._cb_log = cb, None
        try:
            agent._emit("on_auto_nudge", 1, 3)
        finally:
            agent._cb, agent._cb_log = prev_cb, prev_log
        self.assertEqual(counts.get("nudge"), [(1, 3)])

    def test_emit_tool_recovery_reaches_cb(self):
        import agent
        cb, counts = self._counting_cb()
        prev_cb, prev_log = agent._cb, agent._cb_log
        agent._cb, agent._cb_log = cb, None
        try:
            agent._emit("on_tool_recovery", "file", 1)
        finally:
            agent._cb, agent._cb_log = prev_cb, prev_log
        self.assertEqual(counts.get("recovery"), [("file", 1)])

    def test_nudge_emit_site_present_in_source(self):
        """Guard against accidental removal of the emit line in the loop body."""
        from pathlib import Path
        src = (Path(__file__).parent.parent / "agent.py").read_text()
        self.assertIn('_emit("on_auto_nudge"', src)

    def test_tool_recovery_emit_site_present_in_source(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "agent.py").read_text()
        self.assertIn('_emit("on_tool_recovery"', src)


class TestHookInterfaceShape(unittest.TestCase):
    """Dead/live hook assertions — guards Phase A § 7.4 deletion and § 7.2/7.3 emits."""

    def test_truncation_hooks_removed(self):
        """on_truncation_* were unwired dead surface; they must stay gone."""
        self.assertFalse(hasattr(callbacks.NullCallbacks, "on_truncation_recovered"))
        self.assertFalse(hasattr(callbacks.NullCallbacks, "on_truncation_failed"))
        self.assertFalse(hasattr(callbacks.TerminalCallbacks, "on_truncation_recovered"))
        self.assertFalse(hasattr(callbacks.TerminalCallbacks, "on_truncation_failed"))

    def test_wired_recovery_hooks_present(self):
        """on_auto_nudge / on_tool_recovery are emitted from the loop."""
        self.assertTrue(hasattr(callbacks.NullCallbacks, "on_auto_nudge"))
        self.assertTrue(hasattr(callbacks.NullCallbacks, "on_tool_recovery"))
        self.assertTrue(hasattr(callbacks.TerminalCallbacks, "on_auto_nudge"))
        self.assertTrue(hasattr(callbacks.TerminalCallbacks, "on_tool_recovery"))

    def test_no_dead_params_on_assistant_text_or_context_recovery(self):
        """Cycle 0018 regression: on_assistant_text and on_context_recovery
        must not regrow a parameter whose only call-site value is a literal.
        See plan/CICD/improvements/0018-callbacks-dead-params.md."""
        import inspect
        at = inspect.signature(callbacks.TerminalCallbacks.on_assistant_text)
        self.assertEqual(list(at.parameters.keys()), ["self", "text"])
        cr = inspect.signature(callbacks.TerminalCallbacks.on_context_recovery)
        self.assertEqual(list(cr.parameters.keys()), ["self"])
        at_null = inspect.signature(callbacks.NullCallbacks.on_assistant_text)
        self.assertEqual(list(at_null.parameters.keys()), ["self", "text"])
        cr_null = inspect.signature(callbacks.NullCallbacks.on_context_recovery)
        self.assertEqual(list(cr_null.parameters.keys()), ["self"])


class TestSafeCb(unittest.TestCase):
    def test_calls_method_and_returns_value(self):
        class C(callbacks.NullCallbacks):
            def on_notice(self, level, msg):
                return f"{level}:{msg}"
        self.assertEqual(callbacks.safe_cb(C(), "on_notice", "info", "m"), "info:m")

    def test_swallows_exception(self):
        class C(callbacks.NullCallbacks):
            def on_notice(self, level, msg):
                raise RuntimeError("boom")
        # Should not raise
        self.assertIsNone(callbacks.safe_cb(C(), "on_notice", "info", "m"))

    def test_missing_method_is_noop(self):
        cb = callbacks.NullCallbacks()
        self.assertIsNone(callbacks.safe_cb(cb, "on_nonexistent_hook"))

    def test_logs_exception_when_log_given(self):
        class DummyLog:
            def __init__(self):
                self.calls = []
            def exception(self, *args, **kwargs):
                self.calls.append(args)

        class C(callbacks.NullCallbacks):
            def on_error(self, msg):
                raise ValueError("x")

        log = DummyLog()
        callbacks.safe_cb(C(), "on_error", "test", log=log)
        self.assertEqual(len(log.calls), 1)


if __name__ == "__main__":
    unittest.main()
