import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import file as file_tool


class TestFileWriteDescriptionAdvertisesAutoMkdir(unittest.TestCase):

    def test_description_advertises_auto_mkdir(self):
        desc = file_tool.definition["function"]["description"]
        self.assertIn("Parent directories are created automatically", desc)
        self.assertIn("do NOT call mkdir", desc)


class TestFileWriteCreatesMissingParentDirs(unittest.TestCase):

    def test_write_creates_missing_parent_dirs(self):
        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            # Change into d so the target path is inside cwd (path confinement
            # requires writes to be within the working directory).
            os.chdir(d)
            try:
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
            finally:
                os.chdir(orig_cwd)


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

    def test_append_no_trailing_newline_inserts_separator(self):
        """When the existing file lacks a trailing newline, append must add one
        so the new content starts on its own line instead of being fused to the
        last character of the existing content (#684)."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("existing line", encoding="utf-8")  # no trailing \n
            result = file_tool.fn(action="append", path=str(target), content="appended line")
            self.assertIn("Appended to", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "existing line\nappended line")

    def test_append_with_trailing_newline_does_not_add_extra_blank_line(self):
        """When the existing file already ends with \\n, no extra newline is added."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("existing line\n", encoding="utf-8")
            result = file_tool.fn(action="append", path=str(target), content="appended line")
            self.assertIn("Appended to", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "existing line\nappended line")

    def test_append_to_empty_file_does_not_add_leading_newline(self):
        """Appending to an empty file must not prepend a newline."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("", encoding="utf-8")
            result = file_tool.fn(action="append", path=str(target), content="first line")
            self.assertIn("Appended to", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "first line")


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


class TestFileDeleteLineRange(unittest.TestCase):
    """Tests for action='delete' with start_line/end_line (line-range deletion)."""

    def _write(self, path, content):
        """Write via the tool so the file is registered in _accessed_files."""
        file_tool.fn(action="write", path=str(path), content=content)

    def test_delete_middle_lines(self):
        """Deleting lines 2-3 from a 5-line file removes those lines and keeps the rest."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "line1\nline2\nline3\nline4\nline5\n")
            result = file_tool.fn(action="delete", path=str(target), start_line=2, end_line=3)
            self.assertIn("Deleted lines 2-3", result)
            self.assertIn("2 line(s) removed", result)
            self.assertTrue(target.exists(), "file must still exist after line deletion")
            self.assertEqual(target.read_text(), "line1\nline4\nline5\n")

    def test_delete_single_line_via_start_line_only(self):
        """When only start_line is given, end_line defaults to start_line (single-line delete)."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "a\nb\nc\n")
            result = file_tool.fn(action="delete", path=str(target), start_line=2)
            self.assertIn("Deleted lines 2-2", result)
            self.assertEqual(target.read_text(), "a\nc\n")

    def test_delete_first_line(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "first\nsecond\nthird\n")
            file_tool.fn(action="delete", path=str(target), start_line=1, end_line=1)
            self.assertEqual(target.read_text(), "second\nthird\n")

    def test_delete_last_line(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "a\nb\nc\n")
            file_tool.fn(action="delete", path=str(target), start_line=3, end_line=3)
            self.assertEqual(target.read_text(), "a\nb\n")

    def test_delete_all_lines_leaves_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "a\nb\nc\n")
            file_tool.fn(action="delete", path=str(target), start_line=1, end_line=3)
            self.assertTrue(target.exists(), "file should still exist (just empty)")
            self.assertEqual(target.read_text(), "")

    def test_delete_no_line_args_still_deletes_whole_file(self):
        """Without start_line/end_line, delete behaviour is unchanged (removes file)."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "hello\n")
            result = file_tool.fn(action="delete", path=str(target))
            self.assertIn("Deleted", result)
            self.assertFalse(target.exists())

    def test_delete_lines_error_start_exceeds_length(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "a\nb\n")
            result = file_tool.fn(action="delete", path=str(target), start_line=5, end_line=6)
            self.assertIn("Error", result)
            self.assertIn("exceeds file length", result)

    def test_delete_lines_error_start_greater_than_end(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "a\nb\nc\n")
            result = file_tool.fn(action="delete", path=str(target), start_line=3, end_line=1)
            self.assertIn("Error", result)
            self.assertIn("start_line", result)

    def test_delete_lines_result_includes_diff(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            self._write(target, "keep\nremove\nkeep2\n")
            result = file_tool.fn(action="delete", path=str(target), start_line=2, end_line=2)
            self.assertIn("Diff:", result)
            self.assertIn("remove", result)

    def test_delete_lines_requires_prior_read(self):
        """Line-range delete on an unread file must return the 'must read first' error,
        consistent with write (line-range) and insert (#712)."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "unread.txt"
            # Write the file directly (bypassing the tool) so it is NOT in _accessed_files.
            target.write_text("line1\nline2\nline3\n", encoding="utf-8")
            # Ensure the file is not registered as accessed
            file_tool._accessed_files.discard(str(target.resolve()))

            result = file_tool.fn(action="delete", path=str(target), start_line=2, end_line=2)
            self.assertIn("Error", result, msg=f"Expected error, got: {result!r}")
            self.assertIn("has not been read this session", result,
                          msg=f"Expected 'has not been read' message, got: {result!r}")
            # The file must be unmodified
            self.assertEqual(target.read_text(), "line1\nline2\nline3\n",
                             "File must not be modified when guard fires")


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

    def test_list_returns_absolute_paths_when_cwd_differs(self):
        """list action must return absolute paths so the agent can use them as-is
        regardless of where the process cwd happens to be (#688)."""
        orig_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as d:
                target_dir = Path(d) / "mydir"
                target_dir.mkdir()
                (target_dir / "alpha.py").write_text("x")
                (target_dir / "beta").mkdir()
                # Change cwd to /tmp so it differs from the listed directory
                os.chdir("/tmp")
                result = file_tool.fn(action="list", path=str(target_dir))
                lines = result.strip().split("\n")
                for line in lines:
                    bare = line.rstrip("/")
                    self.assertTrue(
                        bare.startswith("/"),
                        msg=f"Expected absolute path in list output, got bare name: {line!r}",
                    )
                # Entries must contain the full parent path, not just the name
                self.assertTrue(
                    any(str(target_dir) in line for line in lines),
                    msg=f"Expected target_dir prefix in list output; got:\n{result}",
                )
        finally:
            os.chdir(orig_cwd)


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

    def test_read_negative_start_line_returns_error(self):
        """read with a negative start_line must return a clear error, not silently read from line 1."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3\n", encoding="utf-8")
            for bad in (-1, -5, -100):
                result = file_tool.fn(action="read", path=str(target), start_line=bad)
                self.assertIn(f"Error: start_line must be >= 1 (got {bad})", result,
                              msg=f"Expected error for start_line={bad}, got: {result!r}")

    def test_read_start_line_greater_than_end_line(self):
        """read with start_line > end_line must return a clear error, not empty content."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")
            # start_line > end_line: should error, not silently return nothing
            result = file_tool.fn(action="read", path=str(target), start_line=8, end_line=2)
            self.assertIn("Error: start_line (8) > end_line (2)", result)
            # Also test a smaller gap
            result2 = file_tool.fn(action="read", path=str(target), start_line=5, end_line=3)
            self.assertIn("Error: start_line (5) > end_line (3)", result2)
            # Valid range still works
            result3 = file_tool.fn(action="read", path=str(target), start_line=3, end_line=5)
            self.assertNotIn("Error", result3)
            self.assertIn("line3", result3)

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
                self.assertIn("permission denied", result.lower())
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


class TestFilePathWhitespace(unittest.TestCase):
    """Path strings with leading/trailing whitespace must be treated the same
    as trimmed paths — the tool should strip them rather than failing with a
    misleading 'does not exist' error."""

    def test_read_path_with_leading_trailing_spaces_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "hello.txt"
            target.write_text("world\n", encoding="utf-8")
            file_tool._accessed_files.add(str(target.resolve()))
            result = file_tool.fn(action="read", path="  " + str(target) + "  ")
            self.assertNotIn("does not exist", result)
            self.assertIn("world", result)

    def test_write_path_with_leading_trailing_spaces_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "new.txt"
            result = file_tool.fn(action="write", path=" " + str(target) + " ", content="hi\n")
            self.assertNotIn("does not exist", result)
            self.assertIn("Wrote", result)
            self.assertTrue(target.exists())

    def test_delete_path_with_leading_trailing_spaces_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "bye.txt"
            target.write_text("bye", encoding="utf-8")
            result = file_tool.fn(action="delete", path=" " + str(target) + " ")
            self.assertNotIn("does not exist", result)
            self.assertIn("Deleted", result)
            self.assertFalse(target.exists())

    def test_list_path_with_leading_trailing_spaces_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            result = file_tool.fn(action="list", path="\t" + d + "\t")
            self.assertNotIn("does not exist", result)


class TestFileNullByteInContent(unittest.TestCase):
    """Null bytes in content must be rejected for write/append/insert. (#762)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.txt = Path(self.tmp) / "test.txt"
        self.txt.write_text("original\n", encoding="utf-8")
        # Pre-read the file so write isn't blocked by unread guard
        file_tool.fn(action="read", path=str(self.txt))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_null_byte_in_content_returns_error(self):
        """Before the fix, null bytes were silently written to the file. (#762)"""
        result = file_tool.fn(action="write", path=str(self.txt), content="hello\x00world\n")
        self.assertIn("null byte", result)
        self.assertIn("Error", result)

    def test_write_null_byte_does_not_modify_file(self):
        """A rejected write must leave the file unchanged."""
        original = self.txt.read_text(encoding="utf-8")
        file_tool.fn(action="write", path=str(self.txt), content="corrupt\x00content\n")
        after = self.txt.read_text(encoding="utf-8")
        self.assertEqual(original, after, "File should not have been modified after rejected write")

    def test_append_null_byte_in_content_returns_error(self):
        """Null byte in append content must be rejected. (#762)"""
        result = file_tool.fn(action="append", path=str(self.txt), content="appended\x00line\n")
        self.assertIn("null byte", result)
        self.assertIn("Error", result)

    def test_append_null_byte_does_not_modify_file(self):
        """A rejected append must leave the file unchanged."""
        original = self.txt.read_text(encoding="utf-8")
        file_tool.fn(action="append", path=str(self.txt), content="bad\x00append\n")
        after = self.txt.read_text(encoding="utf-8")
        self.assertEqual(original, after)

    def test_insert_null_byte_in_content_returns_error(self):
        """Null byte in insert content must be rejected. (#762)"""
        result = file_tool.fn(action="insert", path=str(self.txt), content="ins\x00ert\n", start_line=1)
        self.assertIn("null byte", result)
        self.assertIn("Error", result)

    def test_valid_content_still_works_after_null_check(self):
        """The null-byte guard must not interfere with normal writes. (#762)"""
        result = file_tool.fn(action="write", path=str(self.txt), content="clean content\n")
        self.assertIn("Wrote", result)


class TestFileNullByteInPath(unittest.TestCase):
    """Null bytes in `path` must return a clear Error, not a misleading
    'does not exist' or a wrapped 'embedded null byte' exception. (#766)"""

    def test_read_null_byte_in_path_returns_explicit_error(self):
        """read with a null byte in path must return a clear null-byte error. (#766)"""
        result = file_tool.fn(action="read", path="/tmp/test\x00.txt")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("null byte", result)
        self.assertNotIn("does not exist", result,
                         "Must not report misleading 'does not exist' for null-byte path")

    def test_write_null_byte_in_path_returns_explicit_error(self):
        """write with a null byte in path must return a clear null-byte error. (#766)"""
        result = file_tool.fn(action="write", path="/tmp/test\x00.txt", content="hello")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("null byte", result)
        self.assertNotIn("embedded null byte", result,
                         "Must not leak raw OS exception text")

    def test_append_null_byte_in_path_returns_explicit_error(self):
        """append with a null byte in path must return a clear null-byte error. (#766)"""
        result = file_tool.fn(action="append", path="/tmp/test\x00.txt", content="hello")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("null byte", result)

    def test_delete_null_byte_in_path_returns_explicit_error(self):
        """delete with a null byte in path must return a clear null-byte error. (#766)"""
        result = file_tool.fn(action="delete", path="/tmp/test\x00.txt")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("null byte", result)
        self.assertNotIn("does not exist", result,
                         "Must not report misleading 'does not exist' for null-byte path")

    def test_list_null_byte_in_path_returns_explicit_error(self):
        """list with a null byte in path must return a clear null-byte error. (#766)"""
        result = file_tool.fn(action="list", path="/tmp/test\x00dir")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("null byte", result)

    def test_all_actions_reject_null_byte_in_path(self):
        """Every action must return a clear Error when path contains a null byte. (#766)"""
        for action in ("read", "write", "insert", "append", "delete", "list"):
            with self.subTest(action=action):
                result = file_tool.fn(
                    action=action, path="/tmp/bad\x00path.txt", content="x", start_line=1
                )
                self.assertIsInstance(result, str)
                self.assertTrue(result.startswith("Error:"),
                                f"action={action}: Expected Error:, got: {result!r}")
                self.assertIn("null byte", result)

    def test_valid_path_unaffected_by_null_byte_guard(self):
        """A valid path must still work after the null-byte guard is added. (#766)"""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "ok.txt")
            result = file_tool.fn(action="write", path=target, content="hi")
            self.assertTrue(result.startswith("Wrote '"), f"Normal write broke: {result!r}")


# ── directory-path edge cases (#770) ──────────────────────────────────────────

class TestFileDirectoryPathEdgeCases(unittest.TestCase):
    """read/write on a directory path must return clear errors, not crash (#770)."""

    def test_read_directory_returns_error_with_list_suggestion(self):
        """Reading a directory path must return an error suggesting 'list' action."""
        with tempfile.TemporaryDirectory() as d:
            result = file_tool.fn(action="read", path=d)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("directory", result)
        self.assertIn("list", result)

    def test_write_directory_returns_error(self):
        """Writing to a directory path must return an error, not corrupt the directory."""
        with tempfile.TemporaryDirectory() as d:
            # Without prior read the "unread file" guard fires first — still an error
            result = file_tool.fn(action="write", path=d, content="oops\n")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")

    def test_write_directory_after_fake_read_returns_error(self):
        """Even if the directory path is in _accessed_files, writing must return an error."""
        import tools.file as file_mod
        with tempfile.TemporaryDirectory() as d:
            # Manually prime the accessed-files set as if a read had been done
            file_mod._accessed_files.add(str(Path(d).resolve()))
            result = file_tool.fn(action="write", path=d, content="oops\n")
        self.assertIsInstance(result, str)
        # The write attempt either hits the unread-file guard (Error:) or
        # the OS rejects it with IsADirectoryError wrapped in "Error: action 'write' failed:…"
        self.assertIn("Error", result, f"Expected error message, got: {result!r}")


# ── append to nonexistent file (#770) ─────────────────────────────────────────

class TestFileAppendNonexistent(unittest.TestCase):
    """append to a nonexistent file creates it — documents intentional behaviour (#770)."""

    def test_append_to_nonexistent_creates_file(self):
        """append on a missing file must create it, like shell '>>' redirection."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "brand_new.txt"
            self.assertFalse(target.exists(), "pre-condition: file must not exist")
            result = file_tool.fn(action="append", path=str(target), content="hello\n")
            # Check file existence inside the with-block while tmpdir is still alive
            self.assertIsInstance(result, str)
            self.assertFalse(result.startswith("Error:"),
                             f"append to nonexistent must succeed, got: {result!r}")
            self.assertIn("Appended to", result)
            self.assertTrue(target.exists(), "file must be created after append")
            self.assertEqual(target.read_text(), "hello\n")

    def test_append_to_nonexistent_empty_content_returns_error(self):
        """Appending empty content to a nonexistent file must still return an error."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "missing.txt"
            result = file_tool.fn(action="append", path=str(target), content="")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")


class TestFilePermissionDenied(unittest.TestCase):
    """Write and append to a read-only file must return a clear 'permission denied' error,
    not leak an opaque PermissionError traceback or raw [Errno 13] message (#781)."""

    def test_write_to_read_only_file_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "readonly.txt"
            target.write_text("original\n", encoding="utf-8")
            # Prime the session so the write-guard doesn't block us.
            file_tool.fn(action="read", path=str(target))
            os.chmod(str(target), 0o444)
            try:
                result = file_tool.fn(action="write", path=str(target), content="new\n")
            finally:
                os.chmod(str(target), 0o644)
            self.assertIsInstance(result, str)
            self.assertTrue(
                result.startswith("Error:"),
                msg=f"Expected 'Error:' prefix, got: {result!r}",
            )
            self.assertIn("permission denied", result.lower(),
                          msg=f"Expected 'permission denied' in message, got: {result!r}")
            self.assertNotIn("[Errno 13]", result,
                             msg=f"Must not expose raw errno, got: {result!r}")

    def test_append_to_read_only_file_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "readonly.txt"
            target.write_text("original\n", encoding="utf-8")
            os.chmod(str(target), 0o444)
            try:
                result = file_tool.fn(action="append", path=str(target), content="extra\n")
            finally:
                os.chmod(str(target), 0o644)
            self.assertIsInstance(result, str)
            self.assertTrue(
                result.startswith("Error:"),
                msg=f"Expected 'Error:' prefix, got: {result!r}",
            )
            self.assertIn("permission denied", result.lower(),
                          msg=f"Expected 'permission denied' in message, got: {result!r}")
            self.assertNotIn("[Errno 13]", result,
                             msg=f"Must not expose raw errno, got: {result!r}")

    def test_write_range_to_read_only_dir_returns_clear_error(self):
        """Line-range write when the directory is read-only must return a clear error.

        Note: on Linux, os.replace() can overwrite a read-only *file* if the
        *directory* is writable (POSIX semantics).  To reliably trigger a
        PermissionError we make the parent directory read-only instead, which
        prevents mkstemp() from creating the temp file.
        """
        outer = tempfile.mkdtemp()
        try:
            ro_dir = Path(outer) / "ro_subdir"
            ro_dir.mkdir()
            target = ro_dir / "data.txt"
            target.write_text("line1\nline2\nline3\n", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            os.chmod(str(ro_dir), 0o555)  # no write in the dir → mkstemp fails
            try:
                result = file_tool.fn(
                    action="write", path=str(target),
                    content="replaced\n", start_line=2, end_line=2,
                )
            finally:
                os.chmod(str(ro_dir), 0o755)
        finally:
            import shutil as _shutil
            _shutil.rmtree(outer, ignore_errors=True)
        self.assertIsInstance(result, str)
        self.assertTrue(
            result.startswith("Error:"),
            msg=f"Expected 'Error:' prefix for range write to read-only dir, got: {result!r}",
        )
        self.assertIn("permission denied", result.lower(),
                      msg=f"Expected 'permission denied' in message, got: {result!r}")


class TestFileWriteStartLineWithoutEndLine(unittest.TestCase):
    """write with start_line set but end_line omitted must return a clear error.

    Previously the code silently defaulted end_line=start_line, which replaced
    only the single specified line.  The tool description says end_line is
    REQUIRED when start_line is set, so the code must enforce that. (#788)
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = Path(self.tmp) / "test.txt"
        self.target.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        file_tool.fn(action="read", path=str(self.target))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_start_line_only_returns_error(self):
        """start_line without end_line must return Error, not silently replace line."""
        result = file_tool.fn(
            action="write", path=str(self.target), content="NEW\n", start_line=3
        )
        self.assertTrue(
            result.startswith("Error:"),
            msg=f"Expected Error:, got: {result!r}",
        )
        self.assertIn("end_line", result,
                      msg="Error must mention end_line so caller knows what to fix")

    def test_write_start_line_only_does_not_modify_file(self):
        """A rejected write must leave the file unchanged."""
        original = self.target.read_text(encoding="utf-8")
        file_tool.fn(
            action="write", path=str(self.target), content="NEW\n", start_line=3
        )
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), original,
            "File must not be modified when end_line is missing",
        )

    def test_write_start_line_only_error_suggests_end_line(self):
        """Error message must suggest passing end_line=start_line for single-line replace."""
        result = file_tool.fn(
            action="write", path=str(self.target), content="NEW\n", start_line=4
        )
        self.assertIn("end_line=4", result,
                      msg="Error should suggest end_line=start_line for a single-line replace")

    def test_write_start_line_and_end_line_still_works(self):
        """write with both start_line and end_line must still succeed."""
        result = file_tool.fn(
            action="write", path=str(self.target), content="NEW\n",
            start_line=3, end_line=3,
        )
        self.assertIn("Replaced lines 3-3", result,
                      msg=f"Expected successful replace, got: {result!r}")
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "line1\nline2\nNEW\nline4\nline5\n",
        )

    def test_delete_start_line_only_still_works(self):
        """delete with only start_line (no end_line) must still work — it deletes a single line.

        The end_line=start_line default is intentional for delete (unlike write).
        """
        result = file_tool.fn(
            action="delete", path=str(self.target), start_line=2
        )
        self.assertIn("Deleted lines 2-2", result,
                      msg=f"delete start_line-only should still work, got: {result!r}")
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "line1\nline3\nline4\nline5\n",
        )


# ── Probe-confirmed edge-case regression tests (#792) ─────────────────────────


class TestFileInsertNoTrailingNewline(unittest.TestCase):
    """insert auto-adds a trailing newline when content lacks one, so lines
    don't merge in the resulting file (#792 probe confirmation)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._target = Path(self._tmpdir) / "lines.txt"
        self._target.write_text("line1\nline2\nline3\n", encoding="utf-8")
        file_tool.fn(action="read", path=str(self._target))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_insert_without_trailing_newline_succeeds(self):
        """insert with content lacking a trailing newline must succeed (auto-adds newline)."""
        result = file_tool.fn(
            action="insert", path=str(self._target), content="no newline here", start_line=2
        )
        self.assertFalse(result.startswith("Error:"),
                         msg=f"insert without trailing newline must succeed, got: {result!r}")
        self.assertIn("Inserted", result)

    def test_insert_without_trailing_newline_preserves_surrounding_lines(self):
        """insert with no trailing newline must not merge the new line with an existing line."""
        file_tool.fn(
            action="insert", path=str(self._target), content="inserted", start_line=2
        )
        content = self._target.read_text(encoding="utf-8")
        # "inserted" must be on its own line — not merged with "line2"
        lines = content.splitlines()
        self.assertIn("inserted", lines, "Inserted text must appear as a separate line")
        self.assertIn("line2", lines, "line2 must still be a separate line")
        # Verify they are not on the same line
        self.assertNotIn("insertedline2", content,
                         "insert must not merge new content with the following line")


class TestFileWriteEmptyContent(unittest.TestCase):
    """write with empty-string content clears the file — documented intentional
    behaviour confirmed by probe (#792)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._target = Path(self._tmpdir) / "file.txt"
        self._target.write_text("line1\nline2\nline3\n", encoding="utf-8")
        file_tool.fn(action="read", path=str(self._target))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_write_empty_content_clears_file(self):
        """write with content='' must clear the file to empty (not return an error)."""
        result = file_tool.fn(action="write", path=str(self._target), content="")
        self.assertFalse(result.startswith("Error:"),
                         msg=f"write with empty content must succeed, got: {result!r}")
        self.assertIn("Wrote", result)
        self.assertEqual(self._target.read_text(encoding="utf-8"), "",
                         "File must be empty after write with empty content")

    def test_write_empty_content_returns_zero_chars(self):
        """The success message for an empty write must report 0 chars."""
        result = file_tool.fn(action="write", path=str(self._target), content="")
        self.assertIn("0 chars", result,
                      msg=f"Write of empty string must report 0 chars, got: {result!r}")


class TestFileAppendEmptyContent(unittest.TestCase):
    """append with empty content returns an error — documented intentional
    behaviour confirmed by probe (#792)."""

    def test_append_empty_content_returns_error(self):
        """append with content='' must return a clear error."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            target.write_text("original\n", encoding="utf-8")
            result = file_tool.fn(action="append", path=str(target), content="")
            self.assertTrue(result.startswith("Error:"),
                            msg=f"append with empty content must return Error:, got: {result!r}")
            self.assertIn("no content to append", result)

    def test_append_empty_content_does_not_modify_file(self):
        """A rejected append (empty content) must not modify the file."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            target.write_text("original\n", encoding="utf-8")
            file_tool.fn(action="append", path=str(target), content="")
            self.assertEqual(target.read_text(encoding="utf-8"), "original\n",
                             "File must be unchanged after rejected append")


# ── Regression tests: error message format (must start with "Error: ") ──────────

class TestFileErrorMessageFormat(unittest.TestCase):
    """All file tool error paths must return strings starting with 'Error: '."""

    def test_write_streaming_exception_error_format(self):
        """Streaming write exception must produce 'Error: streaming write failed: ...'."""
        import tools.file as file_mod
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.txt"
            target.write_text("existing\n", encoding="utf-8")
            file_mod._accessed_files.add(str(target.resolve()))
            with patch("builtins.open", side_effect=OSError("disk full")):
                result = file_tool.fn(action="write", path=str(target), content="new\n")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")

    def test_dispatch_exception_error_format(self):
        """Top-level dispatch exception must produce 'Error: action ... failed: ...'."""
        import tools.file as file_mod
        with patch.object(file_mod, "_read", side_effect=RuntimeError("unexpected")):
            result = file_tool.fn(action="read", path="/tmp/nonexistent_file_xyz.txt")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix, got: {result!r}")


# ── Regression: read start_line=0 with end_line (#803) ───────────────────────

class TestFileReadStartLineZeroConsistency(unittest.TestCase):
    """read with start_line=0 and end_line>0 must return an error, consistent
    with write which rejects the same combination (#803).

    start_line=0 with end_line=0 (the default) is still valid — it means
    "read from the beginning of the file" (full-read mode).  The bug was that
    start_line=0 with an explicit end_line was silently treated as start_line=1,
    making read inconsistent with write/insert/delete."""

    def test_read_start_line_zero_with_end_line_returns_error(self):
        """read(start_line=0, end_line=N) must return an error, not silently read from line 1."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3\n", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target), start_line=0, end_line=2)
            self.assertTrue(
                result.startswith("Error:"),
                msg=f"read(start_line=0, end_line=2) must return Error:, got: {result!r}",
            )
            self.assertIn("start_line must be >= 1", result,
                          msg=f"Error must mention start_line >= 1, got: {result!r}")
            self.assertIn("1-indexed", result,
                          msg=f"Error should mention 1-indexed, got: {result!r}")

    def test_read_start_line_zero_with_end_line_does_not_return_file_content(self):
        """read(start_line=0, end_line=N) must not silently return file lines."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target), start_line=0, end_line=1)
            self.assertNotIn("alpha", result,
                             msg="File content must not appear in response to invalid start_line=0")

    def test_read_start_line_zero_no_end_line_still_reads_full_file(self):
        """read(start_line=0) with no end_line is the default full-read mode and must succeed."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\n", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target), start_line=0)
            self.assertFalse(
                result.startswith("Error:"),
                msg=f"read(start_line=0) with no end_line must succeed (full-read mode), got: {result!r}",
            )
            self.assertIn("line1", result)
            self.assertIn("line2", result)

    def test_read_start_line_one_with_end_line_still_works(self):
        """read(start_line=1, end_line=2) must still return the first two lines."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("first\nsecond\nthird\n", encoding="utf-8")
            result = file_tool.fn(action="read", path=str(target), start_line=1, end_line=2)
            self.assertFalse(result.startswith("Error:"),
                             msg=f"read(start_line=1, end_line=2) must succeed, got: {result!r}")
            self.assertIn("first", result)
            self.assertIn("second", result)
            self.assertNotIn("third", result)


# ── Path confinement for write/append (#847) ──────────────────────────────────


class TestFileWritePathConfinement(unittest.TestCase):
    """write and append must refuse paths that resolve outside the working directory (#847).

    Before the fix, file(action='write', path='/tmp/evil.txt', ...) silently
    created the file; relative traversals like '../../secret.txt' also escaped
    the working directory undetected.
    """

    def setUp(self):
        # Create an isolated project directory and change into it so cwd is
        # well-defined and distinct from /tmp.
        self._orig_cwd = os.getcwd()
        self._outer = tempfile.mkdtemp()
        self._project = Path(self._outer) / "project"
        self._project.mkdir()
        os.chdir(str(self._project))

    def tearDown(self):
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._outer, ignore_errors=True)

    # ── write ─────────────────────────────────────────────────────────────────

    def test_write_absolute_path_outside_cwd_returns_error(self):
        """write to an absolute path outside cwd must be rejected. (#847)"""
        outside = str(Path(self._outer) / "escaped.txt")
        result = file_tool.fn(action="write", path=outside, content="pwned!")
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error:, got: {result!r}")
        self.assertIn("outside", result,
                      msg=f"Error must mention 'outside', got: {result!r}")

    def test_write_absolute_path_outside_cwd_does_not_create_file(self):
        """A rejected write must not create the file. (#847)"""
        outside = str(Path(self._outer) / "should_not_exist.txt")
        file_tool.fn(action="write", path=outside, content="pwned!")
        self.assertFalse(
            os.path.exists(outside),
            "File must not be created when write is rejected due to path confinement",
        )

    def test_write_relative_traversal_outside_cwd_returns_error(self):
        """write with a relative '../' traversal that leaves cwd must be rejected. (#847)"""
        result = file_tool.fn(action="write", path="../escape.txt", content="escaped!")
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error: for traversal path, got: {result!r}")
        self.assertIn("outside", result)

    def test_write_relative_traversal_does_not_create_file(self):
        """Traversal write must not create the file in the parent directory. (#847)"""
        file_tool.fn(action="write", path="../escape.txt", content="escaped!")
        self.assertFalse(
            (Path(self._outer) / "escape.txt").exists(),
            "File must not be created outside cwd via traversal",
        )

    def test_write_inside_cwd_still_works(self):
        """write to a path inside cwd must still succeed after the confinement check. (#847)"""
        inside = str(self._project / "allowed.txt")
        result = file_tool.fn(action="write", path=inside, content="ok\n")
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Write inside cwd must succeed, got: {result!r}")
        self.assertIn("Wrote", result)
        self.assertTrue((self._project / "allowed.txt").exists())

    def test_write_relative_inside_cwd_still_works(self):
        """write to a relative path that stays inside cwd must succeed. (#847)"""
        result = file_tool.fn(action="write", path="subfile.txt", content="safe\n")
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Relative write inside cwd must succeed, got: {result!r}")
        self.assertTrue((self._project / "subfile.txt").exists())

    # ── append ────────────────────────────────────────────────────────────────

    def test_append_absolute_path_outside_cwd_returns_error(self):
        """append to an absolute path outside cwd must be rejected. (#847)"""
        outside = str(Path(self._outer) / "existing.txt")
        Path(outside).write_text("original\n", encoding="utf-8")
        result = file_tool.fn(action="append", path=outside, content="appended")
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error:, got: {result!r}")
        self.assertIn("outside", result)

    def test_append_outside_cwd_does_not_modify_file(self):
        """A rejected append must leave the target file unchanged. (#847)"""
        outside = str(Path(self._outer) / "existing.txt")
        Path(outside).write_text("original\n", encoding="utf-8")
        file_tool.fn(action="append", path=outside, content="INJECTED")
        self.assertEqual(
            Path(outside).read_text(encoding="utf-8"),
            "original\n",
            "File must not be modified when append is rejected due to path confinement",
        )

    def test_append_relative_traversal_outside_cwd_returns_error(self):
        """append with a '../' traversal must be rejected. (#847)"""
        result = file_tool.fn(action="append", path="../escape.txt", content="leak")
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error: for traversal append, got: {result!r}")
        self.assertIn("outside", result)

    def test_append_inside_cwd_still_works(self):
        """append to a file inside cwd must succeed after the confinement check. (#847)"""
        inside = self._project / "log.txt"
        inside.write_text("entry1\n", encoding="utf-8")
        result = file_tool.fn(action="append", path=str(inside), content="entry2\n")
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Append inside cwd must succeed, got: {result!r}")
        self.assertIn("Appended", result)


# ── Path confinement for delete/insert (#861) ─────────────────────────────────


class TestFileDeleteInsertPathConfinement(unittest.TestCase):
    """delete and insert must refuse paths that resolve outside the working directory (#861).

    PR #848 added confinement to write/append but missed delete and insert.
    file(action='delete', path='/var/tmp/...') could silently remove files outside cwd.
    file(action='insert', path='/var/tmp/...') could silently inject content outside cwd.
    """

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._outer = tempfile.mkdtemp()
        self._project = Path(self._outer) / "project"
        self._project.mkdir()
        os.chdir(str(self._project))

    def tearDown(self):
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._outer, ignore_errors=True)

    # ── delete ────────────────────────────────────────────────────────────────

    def test_delete_absolute_path_outside_cwd_returns_error(self):
        """delete of an absolute path outside cwd must be rejected. (#861)"""
        outside = str(Path(self._outer) / "victim.txt")
        Path(outside).write_text("important\n", encoding="utf-8")
        result = file_tool.fn(action="delete", path=outside)
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error:, got: {result!r}")
        self.assertIn("outside", result)

    def test_delete_absolute_path_outside_cwd_does_not_delete_file(self):
        """A rejected delete must leave the target file on disk. (#861)"""
        outside = str(Path(self._outer) / "must_survive.txt")
        Path(outside).write_text("preserved\n", encoding="utf-8")
        file_tool.fn(action="delete", path=outside)
        self.assertTrue(
            os.path.exists(outside),
            "File must NOT be deleted when delete is rejected due to path confinement",
        )

    def test_delete_relative_traversal_outside_cwd_returns_error(self):
        """delete with a '../' traversal that leaves cwd must be rejected. (#861)"""
        result = file_tool.fn(action="delete", path="../escape.txt")
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error: for traversal delete, got: {result!r}")
        self.assertIn("outside", result)

    def test_delete_inside_cwd_still_works(self):
        """delete of a file inside cwd must succeed after the confinement check. (#861)"""
        inside = self._project / "removeme.txt"
        inside.write_text("bye\n", encoding="utf-8")
        result = file_tool.fn(action="delete", path=str(inside))
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Delete inside cwd must succeed, got: {result!r}")
        self.assertIn("Deleted", result)
        self.assertFalse(inside.exists())

    # ── insert ────────────────────────────────────────────────────────────────

    def test_insert_absolute_path_outside_cwd_returns_error(self):
        """insert into a file outside cwd must be rejected. (#861)"""
        outside = str(Path(self._outer) / "target.txt")
        Path(outside).write_text("line 1\nline 2\n", encoding="utf-8")
        # Mark as accessed so the read-first guard doesn't fire
        from tools.file import _accessed_files
        _accessed_files.add(str(Path(outside).resolve()))
        result = file_tool.fn(action="insert", path=outside, content="injected\n",
                              start_line=1)
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error:, got: {result!r}")
        self.assertIn("outside", result)

    def test_insert_absolute_path_outside_cwd_does_not_modify_file(self):
        """A rejected insert must leave the target file unchanged. (#861)"""
        outside = str(Path(self._outer) / "target2.txt")
        original = "line 1\nline 2\n"
        Path(outside).write_text(original, encoding="utf-8")
        from tools.file import _accessed_files
        _accessed_files.add(str(Path(outside).resolve()))
        file_tool.fn(action="insert", path=outside, content="INJECTED\n", start_line=1)
        self.assertEqual(
            Path(outside).read_text(encoding="utf-8"),
            original,
            "File content must be unchanged when insert is rejected due to path confinement",
        )

    def test_insert_relative_traversal_outside_cwd_returns_error(self):
        """insert with a '../' traversal that leaves cwd must be rejected. (#861)"""
        result = file_tool.fn(action="insert", path="../escape.txt",
                              content="evil\n", start_line=1)
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error: for traversal insert, got: {result!r}")
        self.assertIn("outside", result)

    def test_insert_inside_cwd_still_works(self):
        """insert into a file inside cwd must succeed after the confinement check. (#861)"""
        inside = self._project / "editable.txt"
        inside.write_text("line 1\nline 2\n", encoding="utf-8")
        from tools.file import _accessed_files
        _accessed_files.add(str(inside.resolve()))
        result = file_tool.fn(action="insert", path=str(inside), content="new line\n",
                              start_line=1)
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Insert inside cwd must succeed, got: {result!r}")


class TestFileReadListPathConfinement(unittest.TestCase):
    """read and list must refuse paths that resolve outside the working directory (#870).

    write/append/delete/insert were already confined; read and list were missed.
    file(action='read', path='/etc/passwd') could leak system files.
    file(action='list', path='/etc') could enumerate system directories.
    """

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._outer = tempfile.mkdtemp()
        self._project = Path(self._outer) / "project"
        self._project.mkdir()
        os.chdir(str(self._project))

    def tearDown(self):
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._outer, ignore_errors=True)

    # ── read ──────────────────────────────────────────────────────────────────

    def test_read_absolute_path_outside_cwd_returns_error(self):
        """read of an absolute path outside cwd must be rejected. (#870)"""
        outside = str(Path(self._outer) / "secret.txt")
        Path(outside).write_text("secret\n", encoding="utf-8")
        result = file_tool.fn(action="read", path=outside)
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error:, got: {result!r}")
        self.assertIn("outside", result)

    def test_read_absolute_path_outside_cwd_does_not_return_content(self):
        """A rejected read must not return the file's contents. (#870)"""
        outside = str(Path(self._outer) / "no_leak.txt")
        Path(outside).write_text("SENSITIVE DATA\n", encoding="utf-8")
        result = file_tool.fn(action="read", path=outside)
        self.assertNotIn("SENSITIVE DATA", result)

    def test_read_relative_traversal_outside_cwd_returns_error(self):
        """read with a '../' traversal that escapes cwd must be rejected. (#870)"""
        result = file_tool.fn(action="read", path="../escape.txt")
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error: for traversal read, got: {result!r}")
        self.assertIn("outside", result)

    def test_read_inside_cwd_still_works(self):
        """read of a file inside cwd must succeed after the confinement check. (#870)"""
        inside = self._project / "hello.txt"
        inside.write_text("hello world\n", encoding="utf-8")
        result = file_tool.fn(action="read", path=str(inside))
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Read inside cwd must succeed, got: {result!r}")
        self.assertIn("hello world", result)

    # ── list ──────────────────────────────────────────────────────────────────

    def test_list_absolute_path_outside_cwd_returns_error(self):
        """list of an absolute path outside cwd must be rejected. (#870)"""
        result = file_tool.fn(action="list", path=self._outer)
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error:, got: {result!r}")
        self.assertIn("outside", result)

    def test_list_relative_traversal_outside_cwd_returns_error(self):
        """list with '../' traversal that escapes cwd must be rejected. (#870)"""
        result = file_tool.fn(action="list", path="..")
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Expected Error: for traversal list, got: {result!r}")
        self.assertIn("outside", result)

    def test_list_inside_cwd_still_works(self):
        """list of cwd itself must succeed after the confinement check. (#870)"""
        (self._project / "a.txt").write_text("x", encoding="utf-8")
        result = file_tool.fn(action="list", path=str(self._project))
        self.assertFalse(result.startswith("Error:"),
                         msg=f"List inside cwd must succeed, got: {result!r}")
        self.assertIn("a.txt", result)


class TestEditFuzzyTrailingWhitespace(unittest.TestCase):
    """edit_file fuzzy fallback: trailing whitespace on blank lines must not
    cause a spurious old_string-not-found failure.

    The common crash: model reads a file, copies a block that contains blank
    lines with trailing spaces (e.g. indented blank lines in Python), writes
    the old_string with clean blank lines (\n instead of '    \n'), and the
    exact-string match fails.  The fuzzy path should silently recover.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._target = Path(self._tmpdir) / "sample.py"
        # Write a file where the blank line inside the method carries 4
        # trailing spaces — the exact pattern that triggered the crash.
        self._target.write_text(
            "def foo():\n"
            "    x = 1\n"
            "    \n"           # <-- trailing spaces on blank line
            "    return x\n",
            encoding="utf-8",
        )
        file_tool.fn(action="read", path=str(self._target))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fuzzy_match_applies_edit_when_blank_line_has_trailing_spaces(self):
        """edit succeeds even when old_string has clean \\n where file has '    \\n'."""
        result = file_tool.fn(
            action="edit",
            path=str(self._target),
            old_string="def foo():\n    x = 1\n\n    return x\n",  # clean blank line
            new_string="def foo():\n    x = 2\n\n    return x\n",
        )
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Fuzzy edit must succeed, got: {result!r}")
        content = self._target.read_text(encoding="utf-8")
        self.assertIn("x = 2", content)

    def test_exact_match_still_works(self):
        """Exact old_string (with trailing spaces) still matches as before."""
        result = file_tool.fn(
            action="edit",
            path=str(self._target),
            old_string="def foo():\n    x = 1\n    \n    return x\n",  # exact
            new_string="def foo():\n    x = 3\n    \n    return x\n",
        )
        self.assertFalse(result.startswith("Error:"),
                         msg=f"Exact edit must succeed, got: {result!r}")
        content = self._target.read_text(encoding="utf-8")
        self.assertIn("x = 3", content)

    def test_genuinely_missing_old_string_still_errors(self):
        """old_string with wrong content (not just whitespace) still returns Error."""
        result = file_tool.fn(
            action="edit",
            path=str(self._target),
            old_string="def bar():\n    x = 99\n",  # doesn't exist
            new_string="def bar():\n    x = 0\n",
        )
        self.assertTrue(result.startswith("Error:"),
                        msg=f"Missing old_string must still error, got: {result!r}")
