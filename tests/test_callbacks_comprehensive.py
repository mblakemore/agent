import unittest
from unittest.mock import MagicMock, patch
import io
import sys
from callbacks import NullCallbacks, TerminalCallbacks, safe_cb

class TestNullCallbacks(unittest.TestCase):
    def test_no_ops(self):
        cb = NullCallbacks()
        # Test a few key methods to ensure they return None and don't raise
        self.assertIsNone(cb.on_session_start({"info": "test"}))
        self.assertIsNone(cb.on_tool_start("name", {"arg": 1}))
        self.assertIsNone(cb.on_error("error"))
        self.assertIsNone(cb.check_cancelled())

class TestTerminalCallbacks(unittest.TestCase):
    def setUp(self):
        self.held_output = io.StringIO()
        sys.stdout = self.held_output

    def tearDown(self):
        sys.stdout = sys.__stdout__

    def get_output(self):
        return self.held_output.getvalue()

    def test_init(self):
        cb = TerminalCallbacks(verbose=True, tool_history_size=10, compact_limit=100)
        self.assertTrue(cb.verbose)
        self.assertEqual(cb.compact_limit, 100)
        self.assertEqual(cb.tool_history.maxlen, 10)

    def test_on_boot_progress(self):
        cb = TerminalCallbacks()
        cb.on_boot_progress("Booting...")
        self.assertIn("Booting...", self.get_output())
        self.assertEqual(cb._boot_lines_printed, 1)

    def test_on_session_start_basic(self):
        cb = TerminalCallbacks()
        info = {
            "version": "1.0",
            "sha": "abc",
            "api_ok": True,
            "base_url": "http://api",
            "model": "gpt-4",
            "main_kind": "chat",
            "summary_enabled": False
        }
        cb.on_session_start(info)
        out = self.get_output()
        self.assertIn("agent v1.0", out)
        self.assertIn("(abc)", out)
        # URLs are hidden from the banner (#1043) — kind tag + model only.
        self.assertIn("[chat]  gpt-4", out)
        self.assertNotIn("http://api", out)

    def test_on_session_start_summary(self):
        cb = TerminalCallbacks()
        info = {
            "version": "1.0",
            "api_ok": True,
            "summary_enabled": True,
            "summary_ok": True,
            "summary_base_url": "http://sum",
            "summary_model": "sum-gpt",
            "summary_kind": "summary",
        }
        cb.on_session_start(info)
        out = self.get_output()
        # URL hidden (#1043) — kind tag + model only.
        self.assertIn("[summary]  sum-gpt", out)
        self.assertNotIn("http://sum", out)

    def test_on_session_start_api_fail(self):
        cb = TerminalCallbacks()
        info = {"api_ok": False, "api_detail": "Timeout", "base_url": "http://api"}
        cb.on_session_start(info)
        self.assertIn("Timeout", self.get_output())

    def test_on_summarizer_status(self):
        cb = TerminalCallbacks()
        cb.on_summarizer_status("online", "ok")
        self.assertEqual(self.get_output(), "") # online is silent
        
        cb.on_summarizer_status("unhealthy", "bad")
        self.assertIn("[summary model unhealthy", self.get_output())
        
        cb.on_summarizer_status("unknown", "weird")
        self.assertIn("[summary status: unknown]", self.get_output())

    def test_lifecycle_hooks(self):
        cb = TerminalCallbacks()
        cb.on_cycle_bumped(1, 2)
        self.assertIn("cycle 1 already committed → starting cycle 2", self.get_output())
        
        cb.on_continue_resumed(5, 10)
        self.assertIn("continuing from turn 5 with 10 messages", self.get_output())
        
        cb.on_continue_none()
        self.assertIn("no checkpoint found", self.get_output())
        
        cb.on_repeat_run_start("Test Run")
        self.assertIn("Test Run", self.get_output())
        
        cb.on_repeat_done(3)
        self.assertIn("Stopped after 3 runs", self.get_output())

    def test_user_input_hooks(self):
        cb = TerminalCallbacks()
        cb.on_user_message("Hello")
        self.assertIn("You: Hello", self.get_output())
        
        cb.on_file_attached("file.txt")
        self.assertIn("file.txt", self.get_output())

    def test_api_retry(self):
        cb = TerminalCallbacks()
        cb.on_api_retry("Connection Error", 1, 3, 2.0)
        self.assertIn("LLM error: Connection Error — retry 1/3 in 2.0s", self.get_output())

    def test_assistant_text_streaming(self):
        cb = TerminalCallbacks()
        # Case 1: streamed
        cb.on_stream_chunk("Hello ")
        cb.on_stream_chunk("World")
        self.assertEqual(self.get_output(), "Hello World")
        
        # on_assistant_text should NOT print if streamed
        self.held_output.truncate(0)
        self.held_output.seek(0)
        cb.on_assistant_text("Hello World")
        self.assertEqual(self.get_output(), "")
        
        # Case 2: not streamed
        self.held_output.truncate(0)
        self.held_output.seek(0)
        cb.on_assistant_text("Hello World")
        self.assertIn("Hello World", self.get_output())

    def test_tool_loop(self):
        cb = TerminalCallbacks()
        cb.on_tool_batch_start(2)
        self.assertIn("Executing 2 tool calls", self.get_output())
        
        cb.on_tool_start("read_file", {"path": "test.txt"})
        self.assertIn("-> read_file(path='test.txt')", self.get_output())
        
        # Test result truncation
        long_result = "A" * 1000
        cb.on_tool_result("read_file", {"path": "test.txt"}, long_result, False)
        out = self.get_output()
        self.assertIn("[truncated", out)
        self.assertNotIn(long_result, out)
        
        # Test verbose
        self.held_output.truncate(0)
        self.held_output.seek(0)
        cb.verbose = True
        cb.on_tool_result("read_file", {"path": "test.txt"}, long_result, False)
        self.assertIn(long_result, self.get_output())
        
        # Test error
        self.held_output.truncate(0)
        self.held_output.seek(0)
        cb.on_tool_result("read_file", {"path": "test.txt"}, "Error!", True)
        self.assertIn("Result: Error!", self.get_output())
        
        cb.on_tool_skip("read_file", 3)
        self.assertIn("skipping — read_file failed 3 times", self.get_output())

    def test_guards(self):
        cb = TerminalCallbacks()
        cb.on_forced_think("read_file", 2)
        self.assertIn("loop detected on read_file x2", self.get_output())
        
        cb.on_tool_recovery("read_file", 1)
        self.assertIn("tool recovery: read_file attempt 1", self.get_output())
        
        cb.on_auto_nudge(1, 3)
        self.assertIn("auto-nudge 1/3", self.get_output())
        
        cb.on_hallucination_stripped("file_read")
        self.assertIn("hallucinated file read detected", self.get_output())
        
        cb.on_hallucination_stripped("text_only")
        self.assertIn("text-only response stripped", self.get_output())
        
        cb.on_text_loop_detected(3)
        self.assertIn("text loop detected — same output x3", self.get_output())
        
        cb.on_overtime("text_only")
        self.assertIn("overtime + no tool use", self.get_output())
        
        cb.on_overtime("repeated_result")
        self.assertIn("overtime + repeated result", self.get_output())
        
        cb.on_context_recovery()
        self.assertIn("context overflow", self.get_output())

    def test_summarization(self):
        cb = TerminalCallbacks()
        cb.on_summary_start(10)
        self.assertIn("summarizing 10 messages", self.get_output())
        
        self.held_output.truncate(0)
        self.held_output.seek(0)
        cb.on_summary_start(0)
        self.assertIn("summary too long, condensing", self.get_output())
        
        cb.on_summary_done()
        self.assertIn("summary updated", self.get_output())
        
        cb.on_summary_ready()
        self.assertIn("summary ready", self.get_output())

    def test_status_errors(self):
        cb = TerminalCallbacks()
        cb.on_notice("warn", "Warning!")
        self.assertIn("Warning!", self.get_output())
        
        cb.on_notice("info", "Info!")
        self.assertIn("Info!", self.get_output())
        
        cb.on_error("Fatal Error")
        self.assertIn("Fatal Error", self.get_output())
        
        cb.on_cancelled("at tool call")
        self.assertIn("cancelled — at tool call", self.get_output())

    def test_render_tools(self):
        cb = TerminalCallbacks()
        # Case 1: Empty
        self.assertEqual(cb.render_tools(), "No tool calls yet.")
        
        # Case 2: With data
        cb.on_tool_result("t1", {"a": 1}, "res1", False)
        cb.on_tool_result("t2", {"a": 2}, "res2", True)
        
        out = cb.render_tools()
        self.assertIn("t1(a=1)", out)
        self.assertIn("res1", out)
        self.assertIn("t2(a=2)", out)
        self.assertIn("res2", out)
        
        # Case 3: Limit
        out_limit = cb.render_tools(limit=1)
        self.assertIn("Last 1 of 2", out_limit)
        self.assertNotIn("t1(a=1)", out_limit)

class TestSafeCb(unittest.TestCase):
    def test_success(self):
        cb = NullCallbacks()
        # NullCallbacks.on_error exists and returns None
        self.assertIsNone(safe_cb(cb, "on_error", "msg"))

    def test_missing_method(self):
        cb = NullCallbacks()
        # This method doesn't exist
        self.assertIsNone(safe_cb(cb, "non_existent_method", "arg"))

    def test_exception_swallowed(self):
        class BuggyCallbacks(NullCallbacks):
            def on_error(self, msg):
                raise RuntimeError("Boom")
        
        cb = BuggyCallbacks()
        # Should not raise
        self.assertIsNone(safe_cb(cb, "on_error", "msg"))

    def test_exception_logged(self):
        class BuggyCallbacks(NullCallbacks):
            def on_error(self, msg):
                raise RuntimeError("Boom")
        
        cb = BuggyCallbacks()
        log = MagicMock()
        safe_cb(cb, "on_error", "msg", log=log)
        log.exception.assert_called_once()

    def test_log_exception_swallowed(self):
        class BuggyCallbacks(NullCallbacks):
            def on_error(self, msg):
                raise RuntimeError("Boom")
        
        cb = BuggyCallbacks()
        log = MagicMock()
        log.exception.side_effect = RuntimeError("Log failed")
        # Should not raise even if logger fails
        self.assertIsNone(safe_cb(cb, "on_error", "msg", log=log))

if __name__ == "__main__":
    unittest.main()
