"""Tests for _expand_file_refs edge cases.

Covers path validation, confinement, and OS-level error handling.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from agent import _expand_file_refs


class TestExpandFileRefsOSErrors(unittest.TestCase):
    """_expand_file_refs must handle OS-level errors gracefully (#865)."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_enametoolong_returns_error_not_exception(self):
        """@ref with a filename exceeding NAME_MAX must return error, not raise (#865)."""
        long_name = "a" * 4096
        expanded, files, err = _expand_file_refs(f"@{long_name}")
        self.assertIsNone(expanded, "expanded must be None on error")
        self.assertIsNone(files, "files must be None on error")
        self.assertIsNotNone(err, "err must not be None for ENAMETOOLONG")
        self.assertTrue(err.startswith("Error:"), f"Error must start with 'Error:', got: {err!r}")

    def test_nonexistent_file_still_returns_error(self):
        """@nonexistent.txt must still return the 'does not exist' error after the fix."""
        expanded, files, err = _expand_file_refs("@nonexistent_xyz_865.txt")
        self.assertIsNone(expanded)
        self.assertIsNotNone(err)
        self.assertIn("does not exist", err)

    def test_no_refs_returns_text_unchanged(self):
        """Text with no @-refs must be returned unchanged."""
        text = "plain text no refs here"
        expanded, files, err = _expand_file_refs(text)
        self.assertEqual(expanded, text)
        self.assertIsNone(err)

    def test_valid_ref_inside_cwd_still_works(self):
        """@ref pointing to a real file inside cwd must still expand correctly."""
        target = Path(self._tmpdir) / "hello.txt"
        target.write_text("hello world\n", encoding="utf-8")
        expanded, files, err = _expand_file_refs(f"@{target}")
        self.assertIsNone(err, f"Expected no error for valid ref, got: {err!r}")
        self.assertIn("hello world", expanded)

    def test_outside_cwd_still_returns_confinement_error(self):
        """@/etc/hostname must return a confinement error, not ENAMETOOLONG (#865)."""
        expanded, files, err = _expand_file_refs("@/etc/hostname")
        self.assertIsNone(expanded)
        self.assertIsNotNone(err)
        self.assertIn("outside the working directory", err,
                      f"Expected confinement error, got: {err!r}")


if __name__ == "__main__":
    unittest.main()
