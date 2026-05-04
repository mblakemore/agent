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
        # the OS rejects it with IsADirectoryError wrapped in "Error (write):…"
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
