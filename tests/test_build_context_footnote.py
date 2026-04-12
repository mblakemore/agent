"""Regression guards for CICD cycle 0032: _build_context_footnote helper.

Verifies:
1. _build_context no longer duplicates the parts-build logic (single call site).
2. _build_context_footnote always includes the TOOL RULE hint.
3. _build_context_footnote includes initial_files when provided.
4. _build_context_footnote includes the progress summary text.
"""

import re
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent


class TestBuildContextFootnoteHelper(unittest.TestCase):
    """Unit tests for the extracted _build_context_footnote helper."""

    def test_footnote_helper_contains_tool_rule(self):
        """_build_context_footnote must include the TOOL RULE hint in both
        normal and condensed-summary paths.  This is the primary regression guard:
        before cycle 0032 the condensed path silently dropped the hint."""
        result = agent._build_context_footnote("some summary text", None)
        self.assertIn(
            "TOOL RULE",
            result["content"],
            "_build_context_footnote must include 'TOOL RULE' in the content — "
            "condensed-summary sessions depend on this hint to avoid using "
            "file(action='write', ...) for JSON content.",
        )

    def test_footnote_helper_includes_exec_command_guidance(self):
        """The TOOL RULE hint must specifically mention exec_command with heredoc."""
        result = agent._build_context_footnote("some summary text", None)
        content = result["content"]
        self.assertIn(
            "exec_command",
            content,
            "_build_context_footnote TOOL RULE must mention exec_command",
        )
        self.assertIn(
            "heredoc",
            content,
            "_build_context_footnote TOOL RULE must mention heredoc",
        )

    def test_footnote_helper_includes_initial_files(self):
        """When initial_files is provided it must appear in the content."""
        initial = "# contents of myfile.py\nprint('hello')"
        result = agent._build_context_footnote("summary text", initial)
        self.assertIn(
            initial,
            result["content"],
            "_build_context_footnote must include initial_files content when provided",
        )

    def test_footnote_helper_omits_initial_files_when_none(self):
        """When initial_files is None (or falsy) the content must still be valid
        and must not contain 'None' as a string."""
        result = agent._build_context_footnote("summary text", None)
        content = result["content"]
        self.assertNotIn(
            "\nNone\n",
            content,
            "_build_context_footnote must not include the string 'None' when "
            "initial_files=None",
        )

    def test_footnote_helper_includes_summary(self):
        """The returned content must include the progress summary text."""
        summary = "I have completed steps 1 and 2."
        result = agent._build_context_footnote(summary, None)
        self.assertIn(
            summary,
            result["content"],
            "_build_context_footnote must include the summary text",
        )
        self.assertIn(
            "Progress summary of work done so far",
            result["content"],
            "_build_context_footnote must include the 'Progress summary' label",
        )

    def test_footnote_helper_returns_user_role_message(self):
        """The helper must return a message dict with role='user'."""
        result = agent._build_context_footnote("text", None)
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("role"), "user")
        self.assertIn("content", result)
        self.assertIsInstance(result["content"], str)

    def test_footnote_helper_includes_important_working_dir(self):
        """The IMPORTANT preamble about the working directory must be present."""
        result = agent._build_context_footnote("text", None)
        self.assertIn(
            "IMPORTANT",
            result["content"],
            "_build_context_footnote must include the IMPORTANT cwd preamble",
        )
        self.assertIn(
            "working directory",
            result["content"],
            "_build_context_footnote must mention 'working directory'",
        )


class TestBuildContextFootnoteDeduplicated(unittest.TestCase):
    """Static assertion: _build_context no longer duplicates the parts-build block.

    The metric for cycle 0032: `parts = []` block count inside `_build_context`
    scope goes from 2 (before) to 0 (after — the helper owns the list).
    """

    @classmethod
    def _build_context_body(cls):
        """Extract the source text of _build_context from agent.py."""
        src_path = os.path.join(os.path.dirname(__file__), "..", "agent.py")
        with open(src_path) as f:
            src = f.read()
        # Capture from 'def _build_context(' to the next top-level 'def '
        m = re.search(
            r"def _build_context\b.*?(?=\ndef [a-zA-Z_]|\Z)",
            src,
            re.DOTALL,
        )
        return m.group(0) if m else ""

    def test_no_duplicate_parts_build_in_build_context(self):
        """_build_context must not contain 'parts = []' — the footnote
        helper owns that list.  Before cycle 0032 this count was 2 (one
        in the normal path, one in the condensed path); after it must be 0."""
        body = self._build_context_body()
        self.assertTrue(body, "Could not locate _build_context in agent.py")
        count = body.count("parts = []")
        self.assertEqual(
            count,
            0,
            f"_build_context still contains {count} 'parts = []' block(s). "
            "Expected 0 — the _build_context_footnote helper should own the parts list. "
            "Cycle 0032 regression: the deduplication was undone.",
        )

    def test_build_context_calls_footnote_helper(self):
        """_build_context must call _build_context_footnote at least once."""
        body = self._build_context_body()
        self.assertTrue(body, "Could not locate _build_context in agent.py")
        self.assertIn(
            "_build_context_footnote(",
            body,
            "_build_context must call _build_context_footnote — helper was removed?",
        )


if __name__ == "__main__":
    unittest.main()
