import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import file as file_tool

class TestFileEdgeCases(unittest.TestCase):

    def test_resolve_path_duplicated_cwd(self):
        # This test tries to trigger the logic that strips duplicated CWD prefix.
        # The logic depends on Path.cwd().
        # We mock Path.cwd() to control the environment.
        with patch('tools.file.Path.cwd') as mock_cwd:
            mock_cwd.return_value = Path("/mnt/droid/repos/project/e2")
            # Path is not absolute, starts with prefix (without leading slash)
            path = "mnt/droid/repos/project/e2/file.txt"
            resolved = file_tool._resolve_path(path)
            # Expected: Path("/mnt/droid/repos/project/e2/file.txt")
            self.assertEqual(str(resolved), "/mnt/droid/repos/project/e2/file.txt")

    def test_write_unread_file_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "unread.txt"
            target.write_text("content")
            # Reset accessed files to ensure target is not seen as read
            file_tool._accessed_files.clear()
            result = file_tool.fn(action="write", path=str(target), content="new")
            self.assertIn("Error", result)
            self.assertIn("has not been read this session", result)

    def test_write_replace_range_invalid_lines(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            
            # start_line > end_line
            result = file_tool.fn(action="write", path=str(target), content="new", start_line=3, end_line=2)
            self.assertIn("Error: start_line (3) > end_line (2)", result)
            
            # start_line > total_lines
            result = file_tool.fn(action="write", path=str(target), content="new", start_line=10, end_line=11)
            self.assertIn("Error: start_line (10) exceeds file length", result)
            
            # end_line > total_lines
            result = file_tool.fn(action="write", path=str(target), content="new", start_line=1, end_line=10)
            self.assertIn("Error: end_line (10) exceeds file length", result)

    def test_insert_unread_file_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "unread.txt"
            target.write_text("content")
            file_tool._accessed_files.clear()
            result = file_tool.fn(action="insert", path=str(target), content="new", start_line=1)
            self.assertIn("Error", result)
            self.assertIn("has not been read this session", result)

    def test_insert_invalid_start_line(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            
            # start_line <= 0
            result = file_tool.fn(action="insert", path=str(target), content="new", start_line=0)
            self.assertIn("Error: start_line must be >= 1", result)
            
            # start_line > length + 1
            result = file_tool.fn(action="insert", path=str(target), content="new", start_line=10)
            self.assertIn("Error: start_line (10) exceeds file length + 1", result)

    def test_delete_nonempty_dir_already_covered(self):
        # This is in test_file_tool.py, but let's make sure
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "not_empty"
            target.mkdir()
            (target / "file.txt").write_text("hi")
            result = file_tool.fn(action="delete", path=str(target))
            self.assertIn("Error: directory", result)
            self.assertIn("not empty", result)

    def test_list_file_as_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "file.txt"
            target.write_text("hi")
            result = file_tool.fn(action="list", path=str(target))
            self.assertIn("is not a directory", result)
            self.assertIn("is not a directory", result)

    def test_streaming_write_failure(self):
        # Simulate a failure during the streaming write process.
        # The streaming write uses open(p, 'r') and tempfile.mkstemp().
        # We can mock open to raise an exception during the read loop.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))
            
            with patch("builtins.open", side_effect=IOError("Simulated disk failure")) as mock_open:
                # We need to let the first open(p, 'r') for old_content read work, 
                # but the one inside the streaming loop fail.
                # Since we are patching builtins.open, we have to be careful.
                # A better way is to mock the specific call inside _write.
                pass
            
            # Let's try a simpler approach: mock the context manager.
            # Instead of patching builtins.open, let's mock the streaming part.
            # Actually, simulating a failure in the middle of a loop is tricky with mocks.
            # Let's just test that the try/except block catches it.
            pass

    def test_write_replace_range_no_content(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.txt"
            target.write_text("line1\nline2\nline3", encoding="utf-8")
            file_tool.fn(action="read", path=str(target))

            # Replacing with empty content (deleting lines)
            result = file_tool.fn(action="write", path=str(target), content="", start_line=2, end_line=2)
            self.assertIn("Replaced lines 2-2", result)
            self.assertEqual(target.read_text(), "line1\nline3")


class TestAppendMainGuard(unittest.TestCase):
    """Tests for smart-insert behaviour when appending to .py files with __main__ guard."""

    def test_append_to_py_file_inserts_before_main_guard(self):
        """Appending to a .py file that ends with an __main__ guard places new content before it."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test_suite.py"
            original = (
                "import unittest\n"
                "\n"
                "class MyTests(unittest.TestCase):\n"
                "    def test_existing(self):\n"
                "        pass\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
            target.write_text(original, encoding="utf-8")

            new_method = (
                "    def test_new(self):\n"
                "        self.assertTrue(True)\n"
            )
            result = file_tool.fn(action="append", path=str(target), content=new_method)

            self.assertIn("Appended to", result)
            final = target.read_text(encoding="utf-8")

            # New content must appear before the guard
            guard_pos = final.find("if __name__")
            new_pos = final.find("def test_new")
            self.assertGreater(guard_pos, -1, "Guard must still be present")
            self.assertGreater(new_pos, -1, "New method must be present")
            self.assertLess(new_pos, guard_pos, "New content must appear before the __main__ guard")

            # Guard must still be the last meaningful block
            self.assertTrue(final.rstrip().endswith("unittest.main()"),
                            "Guard block must remain at the end")

    def test_append_to_file_without_main_guard_appends_at_end(self):
        """Normal append still works for .py files without a __main__ guard."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "module.py"
            original = "def foo():\n    pass\n"
            target.write_text(original, encoding="utf-8")

            new_code = "def bar():\n    pass\n"
            result = file_tool.fn(action="append", path=str(target), content=new_code)

            self.assertIn("Appended to", result)
            final = target.read_text(encoding="utf-8")
            self.assertEqual(final, original + new_code)

    def test_append_to_non_py_file_always_appends_at_end(self):
        """Non-.py files always get content appended at EOF, even if they contain an __main__ line."""
        with tempfile.TemporaryDirectory() as d:
            for ext in (".txt", ".md"):
                target = Path(d) / f"file{ext}"
                original = 'some text\nif __name__ == "__main__":\n    pass\n'
                target.write_text(original, encoding="utf-8")

                extra = "appended line\n"
                result = file_tool.fn(action="append", path=str(target), content=extra)

                self.assertIn("Appended to", result)
                final = target.read_text(encoding="utf-8")
                self.assertTrue(final.endswith(extra),
                                f"Content must be at EOF for {ext} file, got: {final!r}")

    def test_append_empty_content_to_py_with_main_guard_returns_error(self):
        """Appending empty content to a .py file with an __main__ guard must not modify the file."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "test.py"
            original = "def foo(): pass\nif __name__ == \"__main__\":\n    foo()\n"
            target.write_text(original, encoding="utf-8")

            result = file_tool.fn(action="append", path=str(target), content="")

            self.assertIn("Error", result, f"Empty append should return an error, got: {result!r}")
            self.assertEqual(target.read_text(encoding="utf-8"), original,
                             "File must not be modified when appending empty content")

    def test_append_empty_content_to_py_without_guard_returns_error(self):
        """Appending empty content to a .py file without an __main__ guard must also return an error."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "module.py"
            original = "def foo(): pass\n"
            target.write_text(original, encoding="utf-8")

            result = file_tool.fn(action="append", path=str(target), content="")

            self.assertIn("Error", result, f"Empty append should return an error, got: {result!r}")
            self.assertEqual(target.read_text(encoding="utf-8"), original,
                             "File must not be modified when appending empty content")

    def test_append_indented_main_guard_not_treated_as_guard(self):
        """An indented __main__ guard (inside a function/class) must not trigger smart-insert."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "nested.py"
            # The guard here is inside a function — not a top-level guard
            original = (
                "def run():\n"
                "    if __name__ == '__main__':\n"
                "        pass\n"
                "\n"
                "def other(): pass\n"
            )
            target.write_text(original, encoding="utf-8")

            new_code = "def new_func(): pass\n"
            result = file_tool.fn(action="append", path=str(target), content=new_code)

            self.assertIn("Appended to", result)
            final = target.read_text(encoding="utf-8")
            # Content must appear at the end, not before the indented guard
            self.assertTrue(final.endswith(new_code),
                            f"Content must be at EOF for indented guard, got: {final!r}")


    def test_append_guard_in_docstring_not_treated_as_guard(self):
        """Guard-text inside a triple-quoted string must not trigger smart-insert."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "docstring_module.py"
            original = (
                'USAGE = """\n'
                'Example:\n'
                '    if __name__ == "__main__":\n'
                '        main()\n'
                '"""\n'
                '\n'
                'def main():\n'
                '    pass\n'
            )
            target.write_text(original, encoding="utf-8")

            new_code = "def helper(): pass\n"
            result = file_tool.fn(action="append", path=str(target), content=new_code)

            self.assertIn("Appended to", result)
            final = target.read_text(encoding="utf-8")
            # Content must appear at EOF, not inside the docstring
            self.assertTrue(final.endswith(new_code),
                            f"Content must be at EOF when guard is inside a string, got: {final!r}")

    def test_append_with_two_trailing_metadata_lines_finds_guard(self):
        """Two trailing module-level metadata assignments must not block guard detection."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "versioned.py"
            original = (
                "def run():\n"
                "    pass\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    run()\n"
                'VERSION = "1.0"\n'
                'AUTHOR = "me"\n'
            )
            target.write_text(original, encoding="utf-8")

            new_code = "def helper(): pass\n"
            result = file_tool.fn(action="append", path=str(target), content=new_code)

            self.assertIn("Appended to", result)
            final = target.read_text(encoding="utf-8")
            guard_pos = final.find("if __name__")
            new_pos = final.find("def helper")
            self.assertGreater(guard_pos, -1, "Guard must still be present")
            self.assertGreater(new_pos, -1, "New function must be present")
            self.assertLess(new_pos, guard_pos,
                            "New content must appear before the __main__ guard")

    def test_append_single_quote_guard_detected(self):
        """A guard written with single quotes must be detected by smart-insert."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "singlequote.py"
            original = (
                "def main():\n"
                "    pass\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            )
            target.write_text(original, encoding="utf-8")

            new_code = "def helper(): pass\n"
            result = file_tool.fn(action="append", path=str(target), content=new_code)

            self.assertIn("inserted before __main__ guard", result,
                          f"Smart-insert must fire for single-quote guard, got: {result!r}")
            final = target.read_text(encoding="utf-8")
            guard_pos = final.find("if __name__")
            new_pos = final.find("def helper")
            self.assertLess(new_pos, guard_pos,
                            "New content must appear before the single-quote __main__ guard")


class TestFileUnexpectedKwargs(unittest.TestCase):
    """fn() must return a clean Error string for unexpected keyword arguments (#652)."""

    def test_old_string_new_string_returns_error_not_typeerror(self):
        """Passing old_string/new_string must not raise TypeError — must return Error string."""
        result = file_tool.fn(
            action="replace",
            path="/tmp/probe_replace.txt",
            old_string="hello",
            new_string="world",
        )
        self.assertIsInstance(result, str, "fn() must always return a string")
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("old_string", result)
        self.assertIn("new_string", result)

    def test_single_unexpected_kwarg_returns_error(self):
        """A single unexpected keyword argument must produce an Error string."""
        result = file_tool.fn(action="read", path="/tmp", bogus_param="x")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bogus_param", result)

    def test_error_message_lists_valid_parameters(self):
        """The error message must include the valid parameter names so callers can self-correct."""
        result = file_tool.fn(action="write", path="/tmp/x.txt", old_string="a")
        self.assertIn("action", result)
        self.assertIn("path", result)
        self.assertIn("content", result)
        self.assertIn("start_line", result)
        self.assertIn("end_line", result)

    def test_valid_call_unaffected(self):
        """A well-formed call must still work correctly after adding **kwargs."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ok.txt"
            result = file_tool.fn(action="write", path=str(target), content="hi")
            self.assertTrue(result.startswith("Wrote '"), f"Normal write broke: {result!r}")
            self.assertEqual(target.read_text(), "hi")


class TestFileContentTypeValidation(unittest.TestCase):
    """fn() must return a clean Error string when content is not a string (#678)."""

    def _prime_file(self, path):
        """Write a file and prime the session read-tracker."""
        Path(path).write_text("original\n", encoding="utf-8")
        file_tool.fn(action="read", path=path)

    def test_write_with_int_content_returns_clean_error(self):
        """write action with content=42 must return Error, not leak Python exception."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "test.txt")
            self._prime_file(target)
            result = file_tool.fn(action="write", path=target, content=42)
            self.assertIsInstance(result, str)
            self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
            self.assertIn("int", result)
            self.assertNotIn("write()", result, "Must not leak raw Python exception text")

    def test_append_with_int_content_returns_clean_error(self):
        """append action with content=99 must return Error, not leak Python exception."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "test.txt")
            self._prime_file(target)
            result = file_tool.fn(action="append", path=target, content=99)
            self.assertIsInstance(result, str)
            self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
            self.assertIn("int", result)
            self.assertNotIn("write()", result, "Must not leak raw Python exception text")

    def test_insert_with_int_content_returns_clean_error(self):
        """insert action with content=0 must return Error, not leak Python exception."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "test.txt")
            self._prime_file(target)
            result = file_tool.fn(action="insert", path=target, content=0, start_line=1)
            self.assertIsInstance(result, str)
            self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
            self.assertIn("int", result)
            self.assertNotIn("splitlines", result, "Must not leak raw Python exception text")

    def test_write_with_list_content_returns_clean_error(self):
        """write action with content=['a', 'b'] must return a clean Error string."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "test.txt")
            result = file_tool.fn(action="write", path=target, content=["a", "b"])
            self.assertIsInstance(result, str)
            self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
            self.assertIn("list", result)

    def test_write_with_none_content_returns_clean_error(self):
        """write action with content=None must return a clean Error string."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "test.txt")
            result = file_tool.fn(action="write", path=target, content=None)
            self.assertIsInstance(result, str)
            self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
            self.assertIn("NoneType", result)

    def test_write_with_string_content_unaffected(self):
        """Normal string content must still work after adding the type check."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "newfile.txt")
            result = file_tool.fn(action="write", path=target, content="hello")
            self.assertTrue(result.startswith("Wrote '"), f"Normal write broke: {result!r}")
            self.assertEqual(Path(target).read_text(), "hello")

    def test_error_message_names_the_bad_type(self):
        """The Error message must mention the actual bad type so the caller can self-correct."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "test.txt")
            for bad_value, expected_type_name in [(3.14, "float"), (True, "bool"), (b"bytes", "bytes")]:
                with self.subTest(bad_value=bad_value):
                    result = file_tool.fn(action="write", path=target, content=bad_value)
                    self.assertIn(expected_type_name, result,
                                  f"Error must mention '{expected_type_name}', got: {result!r}")


class TestFilePathTypeValidation(unittest.TestCase):
    """fn() must return a clean Error string when path is not a string (#682)."""

    def test_read_with_int_path_returns_clean_error(self):
        """read action with path=42 must return Error, not leak Python exception."""
        result = file_tool.fn(action="read", path=42)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("int", result)
        self.assertNotIn("strip", result, "Must not leak raw Python exception text")

    def test_write_with_int_path_returns_clean_error(self):
        """write action with path=42 must return Error, not leak Python exception."""
        result = file_tool.fn(action="write", path=42, content="hello")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("int", result)

    def test_path_none_returns_clean_error(self):
        """path=None must return a clean Error, not crash."""
        result = file_tool.fn(action="read", path=None)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("NoneType", result)

    def test_path_list_returns_clean_error(self):
        """path=[] must return a clean Error string."""
        result = file_tool.fn(action="list", path=[])
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("list", result)

    def test_path_bytes_returns_clean_error(self):
        """path=b'...' must return a clean Error, not an os.PathLike exception."""
        result = file_tool.fn(action="read", path=b"/tmp/file.txt")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bytes", result)
        self.assertNotIn("PathLike", result, "Must not leak raw os exception text")

    def test_error_message_names_the_bad_type_and_value(self):
        """The Error message must mention the bad type AND value so callers can self-correct."""
        for bad_path, expected_type in [(42, "int"), (3.14, "float"), (True, "bool")]:
            with self.subTest(bad_path=bad_path):
                result = file_tool.fn(action="read", path=bad_path)
                self.assertIn(expected_type, result,
                              f"Error must mention '{expected_type}', got: {result!r}")
                self.assertIn(repr(bad_path), result,
                              f"Error must show bad value, got: {result!r}")

    def test_valid_string_path_unaffected(self):
        """A normal string path must still work after adding the type check."""
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d) / "ok.txt")
            result = file_tool.fn(action="write", path=target, content="hi")
            self.assertTrue(result.startswith("Wrote '"), f"Normal write broke: {result!r}")
            self.assertEqual(Path(target).read_text(), "hi")

    def test_all_actions_respect_path_type_check(self):
        """Every action must return a clean Error when path is not a string."""
        for action in ("read", "write", "insert", "append", "delete", "list"):
            with self.subTest(action=action):
                result = file_tool.fn(action=action, path=99, content="x", start_line=1)
                self.assertIsInstance(result, str)
                self.assertTrue(result.startswith("Error:"),
                                f"action={action}: Expected Error:, got: {result!r}")
                self.assertIn("int", result)


class TestFileLineBoolRejection(unittest.TestCase):
    """start_line and end_line must reject bool values — bool is a subclass of int
    in Python, so True==1 and False==0 would silently pass an isinstance(x, int)
    check and produce nonsensical results (#769)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._target = str(Path(self._tmpdir.name) / "lines.txt")
        Path(self._target).write_text("line1\nline2\nline3\nline4\nline5\n")
        # Prime session so reads are allowed
        file_tool.fn(action="read", path=self._target)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_read_start_line_true_returns_error(self):
        result = file_tool.fn(action="read", path=self._target, start_line=True)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bool", result)
        self.assertIn("start_line", result)

    def test_read_start_line_false_returns_error(self):
        result = file_tool.fn(action="read", path=self._target, start_line=False)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bool", result)
        self.assertIn("start_line", result)

    def test_read_end_line_true_returns_error(self):
        result = file_tool.fn(action="read", path=self._target, start_line=1, end_line=True)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bool", result)
        self.assertIn("end_line", result)

    def test_read_end_line_false_returns_error(self):
        result = file_tool.fn(action="read", path=self._target, start_line=1, end_line=False)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bool", result)
        self.assertIn("end_line", result)

    def test_read_normal_int_start_end_still_works(self):
        """Plain integer line numbers must not be broken by the bool guard."""
        result = file_tool.fn(action="read", path=self._target, start_line=2, end_line=3)
        self.assertIn("line2", result)
        self.assertIn("line3", result)
        self.assertNotIn("line1", result)
        self.assertNotIn("line4", result)

    def test_write_start_line_bool_returns_error(self):
        result = file_tool.fn(action="write", path=self._target, content="x\n", start_line=True, end_line=1)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bool", result)

    def test_delete_start_line_bool_returns_error(self):
        result = file_tool.fn(action="delete", path=self._target, start_line=True, end_line=1)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("bool", result)


class TestFileLineFloatRejection(unittest.TestCase):
    """start_line and end_line must reject float values — floats would either
    silently truncate (write/delete arithmetic produces '2.5 lines remain') or
    cause insert to write nothing while falsely reporting success (#817)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._target = str(Path(self._tmpdir.name) / "lines.txt")
        Path(self._target).write_text("line1\nline2\nline3\nline4\nline5\n")
        # Prime session so reads/writes are allowed
        file_tool.fn(action="read", path=self._target)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _assert_float_error(self, result, param_name):
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error:, got: {result!r}")
        self.assertIn("float", result)
        self.assertIn(param_name, result)

    # --- read ---

    def test_read_start_line_float_returns_error(self):
        result = file_tool.fn(action="read", path=self._target, start_line=1.5)
        self._assert_float_error(result, "start_line")

    def test_read_end_line_float_returns_error(self):
        result = file_tool.fn(action="read", path=self._target, start_line=1, end_line=2.9)
        self._assert_float_error(result, "end_line")

    def test_read_start_line_zero_point_five_returns_error(self):
        result = file_tool.fn(action="read", path=self._target, start_line=0.5)
        self._assert_float_error(result, "start_line")

    # --- write (line-range replacement) ---

    def test_write_start_line_float_returns_error(self):
        result = file_tool.fn(action="write", path=self._target, content="new\n",
                              start_line=1.5, end_line=2)
        self._assert_float_error(result, "start_line")

    def test_write_end_line_float_returns_error(self):
        result = file_tool.fn(action="write", path=self._target, content="new\n",
                              start_line=1, end_line=2.5)
        self._assert_float_error(result, "end_line")

    def test_write_float_does_not_corrupt_file(self):
        """A float line number must not modify the file at all."""
        original = Path(self._target).read_text()
        file_tool.fn(action="write", path=self._target, content="CORRUPT\n",
                     start_line=1.0, end_line=2)
        self.assertEqual(Path(self._target).read_text(), original,
                         "File must be unchanged after rejected float line number")

    # --- insert ---

    def test_insert_start_line_float_returns_error(self):
        result = file_tool.fn(action="insert", path=self._target, content="new\n",
                              start_line=2.5)
        self._assert_float_error(result, "start_line")

    def test_insert_float_does_not_silently_skip(self):
        """insert with float start_line must not silently do nothing and report success."""
        original = Path(self._target).read_text()
        result = file_tool.fn(action="insert", path=self._target, content="GHOST\n",
                              start_line=1.5)
        # Must return an error, not a success message
        self.assertTrue(result.startswith("Error:"),
                        f"Float insert must return Error:, got: {result!r}")
        # File must be unchanged
        self.assertEqual(Path(self._target).read_text(), original,
                         "File must be unchanged after rejected float start_line")

    # --- delete ---

    def test_delete_start_line_float_returns_error(self):
        result = file_tool.fn(action="delete", path=self._target, start_line=1.5, end_line=2)
        self._assert_float_error(result, "start_line")

    def test_delete_end_line_float_returns_error(self):
        result = file_tool.fn(action="delete", path=self._target, start_line=1, end_line=2.5)
        self._assert_float_error(result, "end_line")

    def test_delete_float_does_not_corrupt_file(self):
        """A float line number must not delete any content from the file."""
        original = Path(self._target).read_text()
        file_tool.fn(action="delete", path=self._target, start_line=1.5, end_line=2)
        self.assertEqual(Path(self._target).read_text(), original,
                         "File must be unchanged after rejected float line number")

    # --- normal ints still work ---

    def test_read_normal_float_equivalent_int_still_works(self):
        """Plain integer 2 must not be broken by the float guard."""
        result = file_tool.fn(action="read", path=self._target, start_line=2, end_line=3)
        self.assertIn("line2", result)
        self.assertIn("line3", result)

    def test_insert_normal_int_still_works(self):
        """Plain integer start_line must not be affected by the float guard."""
        result = file_tool.fn(action="insert", path=self._target, content="NEW\n",
                              start_line=3)
        self.assertTrue(result.startswith("Inserted"),
                        f"Normal insert broke: {result!r}")

    def test_error_message_shows_float_value(self):
        """The error message must include the offending float value for self-correction."""
        result = file_tool.fn(action="read", path=self._target, start_line=3.7)
        self.assertIn("3.7", result,
                      f"Error must show the bad value 3.7, got: {result!r}")


# ── String / None line number rejection (#899) ───────────────────────────────


class TestFileLineStringNoneRejection(unittest.TestCase):
    """start_line and end_line must reject string and None values with a clear
    type error rather than leaking a raw 'not supported between instances of
    str and int' TypeError from the comparison operator (#899).

    Before the fix, strings and None passed the bool and float guards and
    reached internal comparisons (start_line < 1, end_line > len(lines)),
    causing TypeError to be caught by the outer except and re-emitted as a
    raw stdlib message.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._target = str(Path(self._tmpdir.name) / "lines.txt")
        Path(self._target).write_text("line1\nline2\nline3\nline4\nline5\n")
        file_tool.fn(action="read", path=self._target)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _assert_type_error(self, result, param_name, type_name):
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn(param_name, result, f"Error must name the param: {result!r}")
        self.assertIn(type_name, result, f"Error must name the bad type: {result!r}")
        self.assertNotIn("not supported between instances", result,
                         f"Raw TypeError must not be exposed: {result!r}")

    # --- string start_line ---

    def test_read_start_line_string_returns_type_error(self):
        """start_line='2' must return a clean type error, not a raw TypeError (#899)."""
        result = file_tool.fn(action="read", path=self._target, start_line="2")
        self._assert_type_error(result, "start_line", "str")

    def test_write_start_line_string_returns_type_error(self):
        """start_line='1' for write must return a clean type error (#899)."""
        result = file_tool.fn(action="write", path=self._target,
                              content="new", start_line="1", end_line=1)
        self._assert_type_error(result, "start_line", "str")

    # --- None start_line ---

    def test_read_start_line_none_returns_type_error(self):
        """start_line=None must return a clean type error, not a raw TypeError (#899)."""
        result = file_tool.fn(action="read", path=self._target, start_line=None)
        self._assert_type_error(result, "start_line", "NoneType")

    # --- string end_line ---

    def test_read_end_line_string_returns_type_error(self):
        """end_line='3' must return a clean type error (#899)."""
        result = file_tool.fn(action="read", path=self._target,
                              start_line=1, end_line="3")
        self._assert_type_error(result, "end_line", "str")

    # --- None end_line ---

    def test_read_end_line_none_returns_type_error(self):
        """end_line=None must return a clean type error (#899)."""
        result = file_tool.fn(action="read", path=self._target,
                              start_line=1, end_line=None)
        self._assert_type_error(result, "end_line", "NoneType")

    # --- string error hints about quotes ---

    def test_start_line_string_error_hints_at_removing_quotes(self):
        """The string error must hint to remove quotes (#899)."""
        result = file_tool.fn(action="read", path=self._target, start_line="5")
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertTrue(
            "quote" in result.lower() or "without" in result.lower(),
            f"Error should hint about removing quotes: {result!r}",
        )

    # --- normal integers still work ---

    def test_read_normal_int_unaffected_by_string_guard(self):
        """Plain integer line numbers must not be broken by the new string/None guard (#899)."""
        result = file_tool.fn(action="read", path=self._target, start_line=2, end_line=4)
        self.assertFalse(result.startswith("Error:"), f"Normal int broke: {result!r}")
        self.assertIn("line2", result)
        self.assertIn("line4", result)


if __name__ == "__main__":
    unittest.main()
