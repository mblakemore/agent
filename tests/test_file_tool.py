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
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "latin1.txt"
            target.write_bytes(b"hello \xff world\n")
            result = file_tool.fn(action="read", path=str(target))
            self.assertFalse(
                result.startswith("Error"),
                msg=f"reading a non-UTF-8 file should not return an error, got: {result!r}",
            )
            self.assertIn("\ufffd", result)

    def test_read_utf8_file_with_non_ascii_works_correctly(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "utf8.txt"
            content = "café ⋆ résumé\n"
            target.write_text(content, encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target))
            self.assertIn("café", result)
            self.assertIn("⋆", result)


class TestFileRead(unittest.TestCase):
    def test_read_basic(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target))
            # The tool formats output as '   1  line1'
            self.assertIn("   1  line1", result)
            self.assertIn("   2  line2", result)
            self.assertIn("   3  line3", result)

    def test_read_range(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3\nline4", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target), start_line=2, end_line=3)
            self.assertIn("   2  line2", result)
            self.assertIn("   3  line3", result)
            self.assertNotIn("   1  line1", result)
            self.assertNotIn("   4  line4", result)

    def test_read_nonexistent(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "nonexistent.txt"
            result = file_tool.fn(action="read", path=str(target))
            self.assertTrue(result.startswith("Error"), "Should return error for nonexistent file")

    def test_read_directory_as_file(self):
        with tempfile.TemporaryDirectory() as d:
            result = file_tool.fn(action="read", path=d)
            self.assertTrue(result.startswith("Error"), "Should return error when reading directory as file")

    def test_read_start_line_returns_content_at_correct_line(self):
        """start_line=N must return content beginning at line N, not earlier or later."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            # Use distinct prefixes so substrings don't collide (e.g. "alpha" vs "bravo")
            lines = [f"alpha_{i:03d}\n" for i in range(1, 21)]
            target.write_text("".join(lines), encoding="utf-8")

            result = file_tool.fn(action="read", path=str(target), start_line=10)

            # Lines before start_line must not appear
            for i in range(1, 10):
                self.assertNotIn(f"alpha_{i:03d}", result,
                                 msg=f"alpha_{i:03d} should not appear when start_line=10")
            # Lines from start_line onward must appear
            for i in range(10, 21):
                self.assertIn(f"alpha_{i:03d}", result,
                              msg=f"alpha_{i:03d} should appear when start_line=10")
            # The header must report the correct starting line
            self.assertIn("lines 10-", result,
                          msg="Header should confirm content starts at line 10")

    def test_read_start_line_after_prior_full_read_same_file(self):
        """A second read with start_line=N on the same file must start at N,
        not be affected by the position of the prior full read (issue #570)."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            # Build a file large enough that start_line falls beyond _MAX_READ_LINES
            num_lines = 600
            content = "".join(f"content_{i}\n" for i in range(1, num_lines + 1))
            target.write_text(content, encoding="utf-8")

            # First read: no start_line — reads lines 1..500 (capped by _MAX_READ_LINES)
            first = file_tool.fn(action="read", path=str(target))
            self.assertIn("lines 1-", first, "First read should start at line 1")

            # Second read: explicit start_line beyond the first read's window
            start = 550
            second = file_tool.fn(action="read", path=str(target), start_line=start)

            # Must start at the requested line, not re-use any stale offset
            self.assertIn(f"lines {start}-", second,
                          msg=f"Header must say lines {start}-..., got: {second[:200]}")
            self.assertIn(f"content_{start}", second,
                          msg=f"content_{start} must be present when start_line={start}")
            # Lines before start must not appear in the second result
            self.assertNotIn("content_1\n", second,
                             msg="content from line 1 must not leak into a read with start_line=550")
            self.assertNotIn("content_549\n", second,
                             msg="content from line 549 must not leak into a read with start_line=550")


class TestFileWrite(unittest.TestCase):
    def test_write_full_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            result = file_tool.fn(action="write", path=str(target), content="new content")
            self.assertIn("Wrote", result)
            self.assertEqual(target.read_text(), "new content")

    def test_write_replace_range(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3\nline4", encoding="utf-8")
            # Read first to satisfy _accessed_files check
            file_tool.fn(action="read", path=str(target))
            result = file_tool.fn(action="write", path=str(target), content="replaced", start_line=2, end_line=3)
            self.assertIn("Replaced lines 2-3", result)
            expected = "line1\nreplaced\nline4"
            self.assertEqual(target.read_text().strip(), expected.strip())

    def test_write_max_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            # _MAX_NEW_DIRS = 3. Try creating 5 levels.
            target = Path(d) / "1" / "2" / "3" / "4" / "5" / "file.txt"
            result = file_tool.fn(action="write", path=str(target), content="hi")
            self.assertIn("Error: writing", result)
            self.assertIn("nested directories", result)


class TestFileInsert(unittest.TestCase):
    def test_insert_basic(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline3", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            result = file_tool.fn(action="insert", path=str(target), content="line2\n", start_line=2)
            self.assertIn("Inserted 1 line(s) before line 2", result)
            self.assertEqual(target.read_text(), "line1\nline2\nline3")

    def test_insert_at_end(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\n", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            result = file_tool.fn(action="insert", path=str(target), content="line2\n", start_line=2)
            self.assertEqual(target.read_text(), "line1\nline2\n")


class TestFileAppend(unittest.TestCase):
    def test_append_basic(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\n", encoding="utf-8")
            result = file_tool.fn(action="append", path=str(target), content="line2\n")
            self.assertIn("Appended to", result)
            self.assertEqual(target.read_text(), "line1\nline2\n")

    def test_append_json_guard(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.json"
            target.write_text('{"a": 1}', encoding="utf-8")
            result = file_tool.fn(action="append", path=str(target), content="extra")
            self.assertIn("Error: cannot append to JSON file", result)


class TestFileDelete(unittest.TestCase):
    def test_delete_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("hi")
            result = file_tool.fn(action="delete", path=str(target))
            self.assertIn("Deleted", result)
            self.assertFalse(target.exists())

    def test_delete_nonexistent(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "nonexistent.txt"
            result = file_tool.fn(action="delete", path=str(target))
            self.assertTrue(result.startswith("Error"), "Should return error for nonexistent file")

    def test_delete_nonempty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "empty_dir"
            target.mkdir()
            (target / "file.txt").write_text("hi")
            result = file_tool.fn(action="delete", path=str(target))
            self.assertIn("Error: directory", result)
            self.assertIn("not empty", result)


class TestFileList(unittest.TestCase):
    def test_list_basic(self):
        with tempfile.TemporaryDirectory() as d:
            target_dir = Path(d) / "test_dir"
            target_dir.mkdir()
            (target_dir / "file1.txt").write_text("1")
            (target_dir / "dir1").mkdir()
            result = file_tool.fn(action="list", path=str(target_dir))
            self.assertIn("file1.txt", result)
            self.assertIn("dir1/", result)

    def test_list_nonexistent(self):
        with tempfile.TemporaryDirectory() as d:
            result = file_tool.fn(action="list", path=str(Path(d) / "none"))
            self.assertTrue(result.startswith("Error"), "Should return error for nonexistent directory")


if __name__ == "__main__":
    unittest.main()

class TestFileCoverageGaps(unittest.TestCase):
    """Tests specifically targeting missing lines in tools/file.py."""

    def test_read_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "empty.txt"
            target.write_text("", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target))
            self.assertIn("(empty file)", result)

    def test_read_start_line_exceeds_length(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "short.txt"
            target.write_text("line1\n", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target), start_line=5)
            self.assertIn("Error: start_line (5) exceeds file length", result)

    def test_write_replace_range_nonexistent(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "nonexistent.txt"
            result = file_tool.fn(action="write", path=str(target), content="hi", start_line=1, end_line=1)
            self.assertIn("Error: cannot replace lines", result)

    def test_write_replace_range_invalid_lines(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            
            # start_line > end_line
            result = file_tool.fn(action="write", path=str(target), content="hi", start_line=2, end_line=1)
            self.assertIn("Error: start_line (2) > end_line (1)", result)
            
            # start_line > total_lines
            result = file_tool.fn(action="write", path=str(target), content="hi", start_line=5, end_line=5)
            self.assertIn("Error: start_line (5) exceeds file length", result)
            
            # end_line > total_lines
            result = file_tool.fn(action="write", path=str(target), content="hi", start_line=1, end_line=5)
            self.assertIn("Error: end_line (5) exceeds file length", result)

    def test_write_replace_range_streaming_error(self):
        # To simulate a streaming write error, we can use a read-only directory
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            
            # Make directory read-only to cause mkstemp or os.replace to fail
            import os
            os.chmod(d, 0o555)
            try:
                result = file_tool.fn(action="write", path=str(target), content="hi", start_line=1, end_line=1)
                self.assertIn("Permission denied", result)
            finally:
                os.chmod(d, 0o755)

    def test_insert_nonexistent(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "nonexistent.txt"
            result = file_tool.fn(action="insert", path=str(target), content="hi", start_line=1)
            self.assertIn("Error: cannot insert", result)

    def test_insert_no_content(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("hi")
            file_tool.fn(action="read", path=str(target))
            result = file_tool.fn(action="insert", path=str(target), content="", start_line=1)
            self.assertIn("Error: no content to insert", result)

    def test_insert_invalid_start_line(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("hi")
            file_tool.fn(action="read", path=str(target))
            result = file_tool.fn(action="insert", path=str(target), content="hi", start_line=0)
            self.assertIn("Error: start_line must be >= 1", result)

    def test_insert_start_line_too_high(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("hi")
            file_tool.fn(action="read", path=str(target))
            result = file_tool.fn(action="insert", path=str(target), content="hi", start_line=5)
            self.assertIn("Error: start_line (5) exceeds file length + 1", result)

    def test_insert_streaming_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("hi")
            file_tool.fn(action="read", path=str(target))
            import os
            os.chmod(d, 0o555)
            try:
                result = file_tool.fn(action="insert", path=str(target), content="hi", start_line=1)
                self.assertIn("Permission denied", result)
            finally:
                os.chmod(d, 0o755)

    def test_delete_blocked_filename(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "conversation_checkpoint.json"
            target.write_text("{}")
            result = file_tool.fn(action="delete", path=str(target))
            self.assertIn("internal runtime file", result)

    def test_list_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = file_tool.fn(action="list", path=d)
            self.assertEqual(result, "(empty directory)")

    def test_list_not_a_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            target.write_text("hi")
            result = file_tool.fn(action="list", path=str(target))
            self.assertIn("Error: '", result)
            self.assertIn("not a directory", result)
