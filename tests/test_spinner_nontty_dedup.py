"""Regression test for cycle 0015.

Cycle 0015 fixed a friction where `agent.py`'s per-tool spinner emitted a
dangling `  -> <tool> ` prefix in non-interactive mode (NO_COLOR / not a
TTY) that `TerminalCallbacks.on_tool_start` then duplicated on the same
line because `theme.CLEAR_LINE` is empty under NO_COLOR and therefore
could not rewind. Every non-interactive tool call landed in probe and
automation logs as `  -> exec_command   -> exec_command(args)`.

The fix gates `use_spinner` on `theme._no_color()` at `agent.py` so
non-streaming tools skip `StreamStatus` entirely in non-interactive mode.
`on_tool_start` becomes the single canonical emitter of the tool-call
header in that mode.

Two checks guard the fix:

1. A source-text assertion that `agent.py` still contains the TTY gate in
   the `use_spinner` expression. Cheap, stable, catches the regression
   where it lives.
2. A behavioral assertion that `TerminalCallbacks.on_tool_start` emits the
   tool header exactly once when `theme.CLEAR_LINE` is empty (i.e. the
   canonical emitter's own invariant holds). Guards against anyone
   re-adding a redundant `print(f"  -> {name}")` into the callback itself.
"""

import io
import os
import re
import sys
import unittest

import callbacks
import theme


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AGENT_PY = os.path.join(REPO_ROOT, "agent.py")


class TestSpinnerNonTtyDedup(unittest.TestCase):
    def test_agent_py_gates_spinner_on_tty(self):
        """agent.py's use_spinner must reference theme._no_color()."""
        with open(AGENT_PY, "r", encoding="utf-8") as f:
            source = f.read()

        # Allow parenthesization and whitespace variation, but the three
        # structural pieces must all be there: func_name not in
        # _STREAMING_TOOLS, and not theme._no_color().
        pattern = re.compile(
            r"use_spinner\s*=\s*\(?\s*"
            r"func_name\s+not\s+in\s+_STREAMING_TOOLS"
            r"\s+and\s+not\s+theme\._no_color\(\)",
            re.DOTALL,
        )
        self.assertRegex(
            source,
            pattern,
            "agent.py's use_spinner flag must gate on theme._no_color() so "
            "the non-interactive spinner prefix doesn't duplicate "
            "on_tool_start's header. See "
            "plan/CICD/improvements/0015-spinner-nontty-dedup.md.",
        )

    def test_on_tool_start_emits_single_header_under_no_color(self):
        """TerminalCallbacks.on_tool_start must not duplicate its own header."""
        saved_clear = theme.CLEAR_LINE
        theme.CLEAR_LINE = ""  # simulate NO_COLOR / no-TTY
        try:
            cb = callbacks.TerminalCallbacks()
            buf = io.StringIO()
            saved_stdout = sys.stdout
            sys.stdout = buf
            try:
                cb.on_tool_batch_start(1)
                cb.on_tool_start("exec_command", {"command": "ls -1"})
                cb.on_tool_result(
                    "exec_command",
                    {"command": "ls -1"},
                    "file1.txt\nfile2.txt\n",
                    False,
                )
            finally:
                sys.stdout = saved_stdout
            captured = buf.getvalue()
        finally:
            theme.CLEAR_LINE = saved_clear

        # The canonical header "-> exec_command(" must appear exactly once.
        self.assertEqual(
            captured.count("-> exec_command("),
            1,
            f"on_tool_start must emit the tool header exactly once under "
            f"NO_COLOR — captured output:\n{captured!r}",
        )

        # The cycle-0015 bug signature must be absent from the canonical
        # emitter's output.
        self.assertIsNone(
            re.search(r"-> exec_command\s+-> exec_command\(", captured),
            f"captured output matches the cycle-0015 duplication "
            f"signature:\n{captured!r}",
        )


if __name__ == "__main__":
    unittest.main()
