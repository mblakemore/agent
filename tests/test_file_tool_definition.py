"""Regression guard: file tool definition contains orientation-skip hint for 'list' action.

Cycle 0043 added an IMPORTANT clause to the 'list' action description instructing the agent to
skip the list step when the user's prompt already names the relevant files. This test ensures
the hint survives future edits to tools/file.py.

Issue #576 extended the hint to also explicitly prohibit using 'list' as a first-step orientation,
and added a corresponding guideline to the agent.py SYSTEM CONTEXT preamble.

Issue: #21, #576
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

    def _read_agent_source(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo_root, "agent.py")
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

    def test_list_description_prohibits_first_step_orientation(self):
        """'list' action description must explicitly prohibit using it as a first-step orientation."""
        src = self._read_file_tool_source()
        self.assertIn(
            "first-step orientation",
            src,
            "tools/file.py 'list' action description is missing the first-step orientation "
            "prohibition (issue #576). Add: 'Avoid using `list` as a first-step orientation "
            "— go directly to the relevant file or search instead.'",
        )

    def test_agent_system_context_contains_no_orientation_listing_guideline(self):
        """agent.py SYSTEM CONTEXT preamble must tell the agent not to start with a directory listing."""
        src = self._read_agent_source()
        self.assertIn(
            "Do not start with a directory listing for orientation",
            src,
            "agent.py SYSTEM CONTEXT preamble is missing the no-orientation-listing guideline "
            "(issue #576). The preamble in _expand_file_refs should instruct the agent to "
            "begin with the first task-relevant tool call, not a directory listing.",
        )
