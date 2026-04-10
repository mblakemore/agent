"""Unit tests for commands.py — the slash-command dispatcher."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands
import callbacks


def _make_ctx(**overrides):
    log = SimpleNamespace(info=lambda *a, **kw: None,
                          warning=lambda *a, **kw: None,
                          error=lambda *a, **kw: None)
    base = SimpleNamespace(
        conversation_history=[],
        summary_state={"text": "prior", "up_to": 3},
        initial_files="files",
        async_summarizer=None,
        cb=callbacks.TerminalCallbacks(),
        log=log,
        log_path="/tmp/a.log",
        ctx_size=4096,
        config={"llm": {"model": "old-model"}},
        base_url="http://127.0.0.1:8080",
        setup_logger=lambda: (log, "/tmp/new.log", "/tmp/err.log"),
        pick_model=lambda current, base_url: "new-model",
        render_context_bar=lambda h, s, c: f"context-bar({len(h)})",
        refresh_cb_log=lambda l: None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestHandleCommand(unittest.TestCase):
    def test_non_slash_returns_false(self):
        self.assertFalse(commands.handle_command("hello", _make_ctx()))
        self.assertFalse(commands.handle_command("  ", _make_ctx()))

    def test_unknown_slash_command_consumed_with_warning(self):
        ctx = _make_ctx()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/nope", ctx))
        # on_notice(warn, ...) should have been emitted somewhere

    def test_help_prints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/help", _make_ctx()))
        out = buf.getvalue()
        self.assertIn("/help", out)
        self.assertIn("/clear", out)
        self.assertIn("/verbose", out)

    def test_clear_resets_history_and_rotates_log(self):
        ctx = _make_ctx()
        ctx.conversation_history.extend([{"role": "user"}, {"role": "assistant"}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/clear", ctx))
        self.assertEqual(len(ctx.conversation_history), 0)
        self.assertEqual(ctx.summary_state["text"], "")
        self.assertEqual(ctx.summary_state["up_to"], 0)
        self.assertIsNone(ctx.initial_files)
        self.assertEqual(ctx.log_path, "/tmp/new.log")

    def test_context_calls_render(self):
        ctx = _make_ctx()
        ctx.conversation_history.extend([{"role": "user"}, {"role": "assistant"}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/context", ctx))
        self.assertIn("context-bar(2)", buf.getvalue())

    def test_model_updates_config(self):
        ctx = _make_ctx()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/model", ctx))
        self.assertEqual(ctx.config["llm"]["model"], "new-model")

    def test_model_keeps_old_when_picker_returns_none(self):
        ctx = _make_ctx(pick_model=lambda cur, url: None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/model", ctx)
        self.assertEqual(ctx.config["llm"]["model"], "old-model")

    def test_verbose_toggles_flag(self):
        ctx = _make_ctx()
        self.assertFalse(ctx.cb.verbose)
        commands.handle_command("/verbose", ctx)
        self.assertTrue(ctx.cb.verbose)
        commands.handle_command("/verbose", ctx)
        self.assertFalse(ctx.cb.verbose)

    def test_tools_calls_render_tools(self):
        ctx = _make_ctx()
        ctx.cb._print = lambda *a, **kw: None
        ctx.cb.on_tool_result("file", {"action": "read"}, "data", False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/tools", ctx)
        self.assertIn("file", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
