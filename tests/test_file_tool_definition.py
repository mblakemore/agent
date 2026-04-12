"""Regression guard: file tool definition contains orientation-skip hint for 'list' action.

Cycle 0043 added an IMPORTANT clause to the 'list' action description instructing the agent to
skip the list step when the user's prompt already names the relevant files. This test ensures
the hint survives future edits to tools/file.py.

Issue: #21
"""

import os
import unittest


class TestFileToolListHint(unittest.TestCase):
    """Static assertion that the 'list' action description contains the skip-hint."""

    def _read_file_tool_source(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo_root, "tools", "file.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_list_description_contains_skip_hint(self):
        """'list' action description must tell the agent to skip when paths are already known."""
        src = self._read_file_tool_source()
        self.assertIn(
            "skip this action",
            src,
            "tools/file.py 'list' action description is missing the orientation-skip hint "
            "(IMPORTANT: skip this action if the user's prompt already names the files ...). "
            "Restore it per plan/CICD/improvements/0043-file-list-orient-hint.md.",
        )

    def test_list_description_mentions_wasted_turn(self):
        """'list' action description must explain the cost of unnecessary listing."""
        src = self._read_file_tool_source()
        self.assertIn(
            "wastes a turn",
            src,
            "tools/file.py 'list' action description should mention that unnecessary listing "
            "'wastes a turn' so the model internalises the cost. "
            "Restore per plan/CICD/improvements/0043-file-list-orient-hint.md.",
        )
