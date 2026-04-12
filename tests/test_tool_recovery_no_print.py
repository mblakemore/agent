"""Regression tests for the D12 compliance of tool_recovery.py.

Verifies that:
1. tool_recovery.py contains no raw print() calls (static check).
2. attempt_recovery does NOT call builtins.print on a successful recovery
   (behavioral check with mocked collaborators).
"""
import builtins
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tool_recovery


class TestToolRecoveryNoPrint(unittest.TestCase):

    def test_no_raw_print_in_tool_recovery_source(self):
        """Static: tool_recovery.py must not contain any raw print() calls."""
        src = Path(__file__).parent.parent / "tool_recovery.py"
        text = src.read_text()
        # Count 'print(' occurrences, excluding lines that are comments
        non_comment_lines = [
            line for line in text.splitlines()
            if "print(" in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            non_comment_lines,
            [],
            msg=(
                "tool_recovery.py must not contain raw print() calls — "
                "use _emit/on_notice via agent.py instead. "
                f"Found: {non_comment_lines}"
            ),
        )

    def test_attempt_recovery_does_not_call_print_on_success(self):
        """Behavioral: attempt_recovery must not call print() when it succeeds."""
        # Set up a pattern-matching error (end_line exceeds file length)
        tool_name = "file"
        func_args = {"action": "write", "path": "test.txt", "start_line": 1, "end_line": 100}
        error_str = "Error: end_line (100) exceeds file length (10 lines)"

        # Mock the LLM call to return a corrected value
        def mock_llm_call(**kwargs):
            resp = MagicMock()
            resp.json.return_value = {
                "choices": [{"message": {"content": "10"}}]
            }
            return resp

        # Mock map_fn so the retry with corrected args succeeds
        corrected_result = "Replaced lines 1-10 in 'test.txt'"
        call_count = {"n": 0}

        def mock_file_fn(**kwargs):
            call_count["n"] += 1
            if kwargs.get("end_line", 0) == 10:
                return corrected_result
            return "Error: end_line exceeds file length (10 lines)"

        mock_map_fn = {"file": mock_file_fn}
        mock_config = {"llm": {"model": "test-model"}}
        mock_log = MagicMock()

        with patch.object(builtins, "print") as mock_print:
            result = tool_recovery.attempt_recovery(
                tool_name, func_args, error_str,
                map_fn=mock_map_fn,
                llm_call_fn=mock_llm_call,
                config=mock_config,
                log=mock_log,
            )

        self.assertEqual(result, corrected_result,
                         msg="attempt_recovery should return the corrected result")
        self.assertEqual(
            mock_print.call_count, 0,
            msg="attempt_recovery must not call print() — recovery messages go through callbacks"
        )


if __name__ == "__main__":
    unittest.main()
