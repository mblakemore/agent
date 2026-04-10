"""Tests for _expand_file_refs — the @path syntax in user prompts."""

import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


class TestExpandFileRefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def _write(self, name, content):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def test_no_refs_passes_through(self):
        text, files, err = agent._expand_file_refs("hello world")
        self.assertEqual(text, "hello world")
        self.assertIsNone(files)
        self.assertIsNone(err)

    def test_single_ref_small_file(self):
        self._write("notes.txt", "line1\nline2\n")
        buf = StringIO()
        with redirect_stdout(buf):
            text, files, err = agent._expand_file_refs("@notes.txt please review")
        self.assertIsNone(err)
        self.assertIn("please review", text)
        self.assertIn("[notes.txt", files)
        self.assertIn("line1", files)
        self.assertIn("line2", files)

    def test_missing_file_returns_error(self):
        text, files, err = agent._expand_file_refs("@missing.py help")
        self.assertIsNone(text)
        self.assertIsNone(files)
        self.assertIn("does not exist", err)

    def test_directory_ref_returns_error(self):
        (self.root / "subdir").mkdir()
        text, files, err = agent._expand_file_refs("@subdir")
        self.assertIsNone(text)
        self.assertIn("directory", err)

    def test_duplicate_refs_deduped(self):
        self._write("a.txt", "content-a")
        buf = StringIO()
        with redirect_stdout(buf):
            text, files, err = agent._expand_file_refs("@a.txt @a.txt")
        self.assertIsNone(err)
        # Should appear once in the expanded files content
        self.assertEqual(files.count("content-a"), 1)

    def test_large_file_preview_only(self):
        big = "\n".join(f"line{i}" for i in range(agent._MAX_FULL_LINES + 50))
        self._write("big.log", big)
        buf = StringIO()
        with redirect_stdout(buf):
            text, files, err = agent._expand_file_refs("@big.log")
        self.assertIsNone(err)
        self.assertIn("first", files)  # header says "first N of M"

    def test_agent_md_prepends_working_dir_context(self):
        self._write("agent.md", "# Agent identity")
        buf = StringIO()
        with redirect_stdout(buf):
            text, files, err = agent._expand_file_refs("@agent.md")
        self.assertIsNone(err)
        self.assertIn("SYSTEM CONTEXT", files)
        self.assertIn("AGENT IDENTITY FILE", files)


if __name__ == "__main__":
    unittest.main()
