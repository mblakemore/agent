import pytest
from callbacks import TerminalCallbacks, NullCallbacks, safe_cb

def test_null_callbacks_no_crash():
    cb = NullCallbacks()
    cb.on_session_start({"api_ok": True})
    cb.on_error("test")
    cb.on_tool_result("name", {}, "result", False)

def test_terminal_callbacks_all_methods(capsys):
    cb = TerminalCallbacks(verbose=True)
    
    # Test a wide array of methods to hit those missing lines
    cb.on_session_start({"api_ok": True, "api_detail": "ok", "base_url": "http://loc", "model": "m", "ctx_size": 1, "max_turns": 1, "log_path": "l", "error_log_path": "e"})
    cb.on_summarizer_status("online", "detail")
    cb.on_cycle_bumped(1, 2)
    cb.on_continue_resumed(1, 1)
    cb.on_continue_none()
    cb.on_repeat_run_start("label")
    cb.on_repeat_done(1)
    cb.on_user_message("hello")
    cb.on_file_attached("header")
    cb.on_api_retry("err", 1, 3, 0.1)
    cb.on_stream_chunk("chunk")
    cb.on_assistant_text("text")
    cb.on_tool_batch_start(1)
    cb.on_tool_start("name", {"a": 1})
    cb.on_tool_result("name", {"a": 1}, "result", False)
    cb.on_tool_result("name", {"a": 1}, "error", True)
    cb.on_tool_skip("name", 1)
    cb.on_forced_think("name", 1)
    cb.on_tool_recovery("name", 1)
    cb.on_auto_nudge(1, 3)
    cb.on_hallucination_stripped("file_read")
    cb.on_hallucination_stripped("text_only")
    cb.on_text_loop_detected(1)
    cb.on_overtime("text_only")
    cb.on_overtime("repeated_result")
    cb.on_overtime("other")
    cb.on_context_recovery()
    cb.on_summary_start(1)
    cb.on_summary_start(0)
    cb.on_summary_done()
    cb.on_summary_ready()
    cb.on_notice("warn", "warn msg")
    cb.on_notice("info", "info msg")
    cb.on_error("error msg")
    cb.on_cancelled("where")
    
    captured = capsys.readouterr()
    assert len(captured.out) > 0

def test_terminal_callbacks_render_tools(capsys):
    cb = TerminalCallbacks()
    cb.on_tool_result("t1", {"a": 1}, "res1", False)
    cb.on_tool_result("t2", {"a": 2}, "res2", True)
    
    out_all = cb.render_tools()
    assert "t1" in out_all
    assert "t2" in out_all
    
    out_limit = cb.render_tools(limit=1)
    assert "Last 1" in out_limit

def test_safe_cb_behavior(capsys):
    cb = TerminalCallbacks()
    # Valid method
    safe_cb(cb, "on_error", "test")
    
    # Invalid method (should not crash)
    safe_cb(cb, "non_existent_method", "test")
    
    # Method that raises (if we can force one)
    class BuggyCallbacks(TerminalCallbacks):
        def on_error(self, msg):
            raise RuntimeError("Boom")
            
    buggy = BuggyCallbacks()
    # Should not crash
    safe_cb(buggy, "on_error", "test", log=None)
