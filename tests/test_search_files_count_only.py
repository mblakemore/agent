"""Tests for search_files count_only parameter — CICD 0036/0038."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure the repo root is importable when run from the worktree.
sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.search_files import fn, definition


class TestCountOnly(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create two files with known patterns.
        Path(self.tmpdir, "a.py").write_text(
            "def test_foo(): pass\ndef test_bar(): pass\n"
        )
        Path(self.tmpdir, "b.py").write_text(
            "def test_baz(): pass\ndef helper(): pass\n"
        )

    def test_count_only_returns_header_only(self):
        result = fn(pattern="def test_", path=self.tmpdir, count_only=True)
        # Must contain the summary counts.
        self.assertIn("3 results", result)
        self.assertIn("2 matched", result)
        # Must NOT contain any match lines (path:lineno: ...).
        self.assertNotIn("def test_foo", result)
        self.assertNotIn("def test_bar", result)
        self.assertNotIn("def test_baz", result)

    def test_count_only_zero_matches(self):
        result = fn(pattern="ZZZNOMATCH", path=self.tmpdir, count_only=True)
        # With count_only=True and no matches, still returns the header.
        self.assertIn("0 results", result)
        self.assertIn("Searched", result)
        # Does not include "No matches found." prose (short-circuits before that).
        self.assertNotIn("No matches found", result)

    def test_count_only_false_returns_matches(self):
        result = fn(pattern="def test_", path=self.tmpdir, count_only=False)
        # Default behaviour unchanged — match lines are present.
        self.assertIn("def test_foo", result)

    def test_count_only_in_definition(self):
        props = definition["function"]["parameters"]["properties"]
        self.assertIn("count_only", props)
        self.assertEqual(props["count_only"]["type"], "boolean")
        self.assertFalse(props["count_only"]["default"])


class TestCountOnlyNoTruncation(unittest.TestCase):
    """Regression tests for CICD 0038: count_only must not truncate at _MAX_RESULTS.

    When count_only=True the early-exit cap (_MAX_RESULTS=100) must be skipped
    so the returned count is accurate even when there are more than 100 matches.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create 110 files with 1 match each → 110 total matches, above _MAX_RESULTS=100.
        for i in range(110):
            Path(self.tmpdir, f"file_{i:03d}.py").write_text(
                f"def test_case_{i}(): pass\n"
            )

    def test_count_only_above_max_results_returns_real_count(self):
        """count_only=True must return 110, not 100."""
        result = fn(pattern="def test_", path=self.tmpdir, count_only=True)
        self.assertIn("110 results", result,
                      f"Expected 110 but count_only returned: {result}")

    def test_count_only_above_max_results_no_truncated_label(self):
        """count_only=True must never emit '(truncated)' regardless of match count."""
        result = fn(pattern="def test_", path=self.tmpdir, count_only=True)
        self.assertNotIn("(truncated)", result,
                         f"count_only should not be truncated: {result}")

    def test_display_mode_still_truncates_at_max(self):
        """With count_only=False the 100-result cap must still apply."""
        result = fn(pattern="def test_", path=self.tmpdir,
                    count_only=False, context=0)
        self.assertIn("(truncated)", result,
                      "Display mode should still truncate at _MAX_RESULTS")
        self.assertIn("100 results", result,
                      "Display mode count should be capped at 100")


class TestCountOnlyContextBypass(unittest.TestCase):
    """Regression tests for CICD 0041: count_only must skip context window building.

    When count_only=True, the context > 0 branch must not build windows or
    context_groups — it should update total_matches and continue to the next file.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Two files with known patterns — uses default context=3
        Path(self.tmpdir, "x.py").write_text(
            "def test_alpha(): pass\ndef helper(): pass\ndef test_beta(): pass\n"
        )
        Path(self.tmpdir, "y.py").write_text(
            "def test_gamma(): pass\n"
        )

    def test_count_only_skips_context_windows_default_context(self):
        """count_only=True with default context=3 must return header-only, not context lines."""
        # Default context=3 — before the fix this built context_groups for every file.
        result = fn(pattern="def test_", path=self.tmpdir, count_only=True)
        # Correct count must be returned.
        self.assertIn("3 results", result,
                      f"Expected 3 results in header, got: {result}")
        self.assertIn("2 matched", result,
                      f"Expected 2 matched in header, got: {result}")
        # No match content (neither hit lines nor context separator).
        self.assertNotIn("def test_alpha", result)
        self.assertNotIn("def test_beta", result)
        self.assertNotIn("def test_gamma", result)
        self.assertNotIn("--", result,
                         "Context separator '--' must not appear in count_only output")


class TestContextTruncationOffByOne(unittest.TestCase):
    """Regression tests for the context>0 truncation off-by-one bug.

    When context > 0 and total_matches reaches _MAX_RESULTS, the last matching
    line was previously counted in the header but never added to context_groups,
    so the output showed _MAX_RESULTS-1 lines while the header claimed _MAX_RESULTS.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_single_file_context_truncation_shows_all_100(self):
        """With context>0 and exactly 100 matches, all 100 must appear in output."""
        lines = [f"def test_{i}(): pass\n" for i in range(100)]
        Path(self.tmpdir, "big.py").write_text("".join(lines))

        result = fn(pattern="def test_", path=self.tmpdir,
                    count_only=False, context=3)

        self.assertIn("(truncated)", result)
        self.assertIn("100 results", result,
                      f"Header should report 100 results: {result[:200]}")
        # Count actual match lines (colon-separated, not context lines which use dash)
        actual_shown = result.count(": def test_")
        self.assertEqual(actual_shown, 100,
                         f"Expected 100 match lines in output, got {actual_shown}")

    def test_multi_file_context_truncation_shows_all_100(self):
        """With context>0 across files and 110 total matches, output must show 100."""
        for i in range(110):
            Path(self.tmpdir, f"file_{i:03d}.py").write_text(
                f"def test_case_{i}(): pass\n"
            )

        result = fn(pattern="def test_", path=self.tmpdir,
                    count_only=False, context=3)

        self.assertIn("(truncated)", result)
        self.assertIn("100 results", result,
                      f"Header should report 100 results: {result[:200]}")
        actual_shown = result.count(": def test_")
        self.assertEqual(actual_shown, 100,
                         f"Expected 100 match lines in output, got {actual_shown}")

    def test_context_zero_unaffected(self):
        """context=0 branch must still produce 100 match lines (no regression)."""
        lines = [f"def test_{i}(): pass\n" for i in range(100)]
        Path(self.tmpdir, "big.py").write_text("".join(lines))

        result = fn(pattern="def test_", path=self.tmpdir,
                    count_only=False, context=0)

        self.assertIn("100 results", result)
        actual_shown = result.count(": def test_")
        self.assertEqual(actual_shown, 100,
                         f"context=0: expected 100 match lines, got {actual_shown}")


if __name__ == "__main__":
    unittest.main()
