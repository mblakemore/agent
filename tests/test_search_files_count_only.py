"""Tests for search_files count_only parameter — CICD 0036."""

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


if __name__ == "__main__":
    unittest.main()
