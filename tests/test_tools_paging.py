"""Regression tests for the /tools paging fix (CICD 0002, issue #1).

Pin the contract that:
  * `/tools` with no argument surfaces every entry in the buffered history
  * `/tools N` clamps to the most recent N entries
  * `/tools all` is an explicit alias for the default
  * `/tools <bogus>` warns without crashing
  * `render_tools(limit=None)` returns the full history

These tests populate `TerminalCallbacks.tool_history` directly via
`on_tool_result` so they exercise the same plumbing the live loop uses.
"""

from __future__ import annotations

import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import callbacks
import commands


# strip ANSI escapes so assertions and regex work regardless of color state
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _count_entries(plain_text: str) -> int:
    """Count the enumerated entry lines in `render_tools` output.

    Each entry starts with two spaces, a ✓/✗ marker, a space, then
    "<index>. <name>(…)". We count by the "<index>. " prefix.
    """
    return len(re.findall(r"^  [✓✗] (\d+)\. ", plain_text, flags=re.MULTILINE))


def _populate(cb: callbacks.TerminalCallbacks, n: int) -> None:
    """Record `n` successful tool results on the callbacks' history buffer."""
    cb._print = lambda *a, **kw: None  # silence the compact-mode prints
    for i in range(n):
        cb.on_tool_result(f"tool{i}", {"idx": i}, f"result-{i}", False)


def _make_ctx(cb: callbacks.TerminalCallbacks) -> SimpleNamespace:
    return SimpleNamespace(cb=cb)


class TestToolsPaging(unittest.TestCase):
    def test_tools_no_arg_shows_entire_buffer(self):
        cb = callbacks.TerminalCallbacks()
        _populate(cb, 50)
        self.assertEqual(len(cb.tool_history), 50)
        ctx = _make_ctx(cb)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/tools", ctx))
        plain = _plain(buf.getvalue())
        self.assertEqual(_count_entries(plain), 50)
        self.assertIn("All 50 tool call(s)", plain)

    def test_tools_with_integer_limits_view(self):
        cb = callbacks.TerminalCallbacks()
        _populate(cb, 50)
        ctx = _make_ctx(cb)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/tools 5", ctx))
        plain = _plain(buf.getvalue())
        self.assertEqual(_count_entries(plain), 5)
        self.assertIn("Last 5 of 50", plain)
        # The five most recent are tool45..tool49
        for i in range(45, 50):
            self.assertIn(f"tool{i}(", plain)
        self.assertNotIn("tool44(", plain)

    def test_tools_all_alias_shows_everything(self):
        cb = callbacks.TerminalCallbacks()
        _populate(cb, 50)
        ctx = _make_ctx(cb)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/tools all", ctx))
        plain = _plain(buf.getvalue())
        self.assertEqual(_count_entries(plain), 50)

    def test_tools_bogus_arg_warns_without_crashing(self):
        cb = callbacks.TerminalCallbacks()
        _populate(cb, 3)
        notices: list[tuple[str, str]] = []
        cb.on_notice = lambda level, msg: notices.append((level, msg))
        ctx = _make_ctx(cb)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/tools xyzzy", ctx))
        # No entries rendered
        self.assertEqual(_count_entries(_plain(buf.getvalue())), 0)
        # A warn notice was raised
        self.assertTrue(any(level == "warn" and "/tools" in msg
                            for level, msg in notices))

    def test_tools_negative_int_rejected(self):
        cb = callbacks.TerminalCallbacks()
        _populate(cb, 3)
        notices: list[tuple[str, str]] = []
        cb.on_notice = lambda level, msg: notices.append((level, msg))
        ctx = _make_ctx(cb)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/tools -2", ctx))
        self.assertEqual(_count_entries(_plain(buf.getvalue())), 0)
        self.assertTrue(any(level == "warn" and "positive" in msg
                            for level, msg in notices))

    def test_render_tools_limit_none_shows_full_history(self):
        cb = callbacks.TerminalCallbacks()
        _populate(cb, 7)
        out = _plain(cb.render_tools(limit=None))
        self.assertEqual(_count_entries(out), 7)
        self.assertIn("All 7 tool call(s)", out)

    def test_render_tools_explicit_limit_clamps(self):
        cb = callbacks.TerminalCallbacks()
        _populate(cb, 10)
        out = _plain(cb.render_tools(limit=3))
        self.assertEqual(_count_entries(out), 3)
        self.assertIn("Last 3 of 10", out)


if __name__ == "__main__":
    unittest.main()
