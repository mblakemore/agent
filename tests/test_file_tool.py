import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import file as file_tool


class TestFileWriteDescriptionAdvertisesAutoMkdir(unittest.TestCase):

    def test_description_advertises_auto_mkdir(self):
        desc = file_tool.definition["function"]["description"]
        self.assertIn("Parent directories are created automatically", desc)
        self.assertIn("do NOT call mkdir", desc)


class TestFileWriteCreatesMissingParentDirs(unittest.TestCase):

    def test_write_creates_missing_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "a" / "b" / "hello.txt"
            result = file_tool.fn(
                action="write",
                path=str(target),
                content="hi",
            )
            self.assertTrue(
                result.startswith("Wrote '"),
                msg=f"unexpected write result: {result!r}",
            )
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(), "hi")
            self.assertTrue((Path(d) / "a").is_dir())
            self.assertTrue((Path(d) / "a" / "b").is_dir())


class TestBlockedFilenames(unittest.TestCase):
    """_BLOCKED_FILENAMES must prevent write, append, and delete, not just read."""

    def test_write_blocked_filename_returns_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "conversation_checkpoint.json"
            result = file_tool.fn(action="write", path=str(target), content='{"x":1}')
            self.assertTrue(
                result.startswith("Error:"),
                msg=f"write to blocked filename should fail, got: {result!r}",
            )
            self.assertFalse(target.exists(), "blocked write must not create the file")

    def test_append_blocked_filename_returns_error(self):
        # conversation_checkpoint.json is both a .json file (JSON-append guard)
        # and a blocked filename.  The _BLOCKED_FILENAMES check must fire *first*,
        # before the JSON guard, so the error mentions "internal runtime file".
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "conversation_checkpoint.json"
            target.write_text("original")
            result = file_tool.fn(action="append", path=str(target), content="extra")
            self.assertIn(
                "internal runtime file",
                result,
                msg=f"blocked-filename check must fire before JSON guard, got: {result!r}",
            )
            self.assertEqual(target.read_text(), "original", "blocked append must not modify the file")

    def test_delete_blocked_filename_returns_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "conversation_checkpoint.json"
            target.write_text("{}")
            result = file_tool.fn(action="delete", path=str(target))
            self.assertTrue(
                result.startswith("Error:"),
                msg=f"delete of blocked filename should fail, got: {result!r}",
            )
            self.assertTrue(target.exists(), "blocked delete must not remove the file")


class TestFileReadEncoding(unittest.TestCase):
    """open() in file.py must use encoding='utf-8' so reads never raise UnicodeDecodeError."""

    def test_read_non_utf8_file_returns_content_not_error(self):
        """Reading a file with non-UTF-8 bytes must succeed (with replacement chars),
        not raise UnicodeDecodeError wrapped in an 'Error (read):' message."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "latin1.txt"
            # Write raw bytes: 'hello \xff world' — \xff is invalid UTF-8
            target.write_bytes(b"hello \xff world\n")
            result = file_tool.fn(action="read", path=str(target))
            self.assertFalse(
                result.startswith("Error"),
                msg=f"reading a non-UTF-8 file should not return an error, got: {result!r}",
            )
            # The replacement character U+FFFD should appear in place of \xff
            self.assertIn("\ufffd", result,
                          msg="invalid byte should be replaced with U+FFFD, not crash")

    def test_read_utf8_file_with_non_ascii_works_correctly(self):
        """Valid UTF-8 files with non-ASCII content (e.g. accented chars, emoji) must
        read back intact."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "utf8.txt"
            content = "café ⋆ résumé\n"
            target.write_text(content, encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target))
            self.assertIn("café", result, msg="accented chars must survive round-trip")
            self.assertIn("⋆", result, msg="Unicode star (U+22C6) must survive round-trip")


if __name__ == "__main__":
    unittest.main()
