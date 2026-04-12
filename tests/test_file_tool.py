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


if __name__ == "__main__":
    unittest.main()
