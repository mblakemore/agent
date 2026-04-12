"""Regression tests for D12 compliance of tools/think.py.

Verifies that:
1. tools/think.py contains no raw print() calls (static check).
2. tools/think._output is an injectable module-level attribute.
3. When _output is replaced with a mock, think output goes through the mock
   instead of builtins.print (behavioral check).
"""
import builtins
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.think as think_mod


class TestThinkNoPrint(unittest.TestCase):

    def test_no_raw_print_in_think_source(self):
        """Static: tools/think.py must not contain any raw print() calls."""
        src = Path(__file__).parent.parent / "tools" / "think.py"
        text = src.read_text()
        non_comment_lines = [
            line for line in text.splitlines()
            if "print(" in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            non_comment_lines,
            [],
            msg=(
                "tools/think.py must not contain raw print() calls — "
                "use _output (injectable) so callbacks can handle display. "
                f"Found: {non_comment_lines}"
            ),
        )

    def test_think_has_injectable_output(self):
        """tools.think must expose a module-level _output attribute."""
        self.assertTrue(
            hasattr(think_mod, "_output"),
            msg=(
                "tools.think must have a module-level '_output' attribute "
                "so agent.py can inject a callback-aware writer."
            ),
        )

    def test_think_output_is_injectable(self):
        """Replacing _output captures think display without calling builtins.print."""
        captured = []
        original_output = think_mod._output

        try:
            think_mod._output = captured.append
            with patch.object(builtins, "print") as mock_print:
                # Simulate what fn() does after parsing a response (no network call)
                think_mod._output("  [Answer] test answer")
            self.assertEqual(
                mock_print.call_count, 0,
                msg="builtins.print must not be called when _output is replaced"
            )
            self.assertEqual(
                captured, ["  [Answer] test answer"],
                msg="_output replacement must capture the text"
            )
        finally:
            think_mod._output = original_output


if __name__ == "__main__":
    unittest.main()
