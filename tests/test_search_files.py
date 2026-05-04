import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import search_files


def _body(result: str) -> str:
    """Strip the [Searched …] header; return just the match body."""
    _, _, body = result.partition("]\n")
    return body


class TestSearchFilesContextZero(unittest.TestCase):

    def test_context_zero_match_lines_only(self):
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text("alpha\nbeta\ngamma\n")
            result = search_files.fn("beta", path=d, context=0)
            body = _body(result)
            self.assertEqual(body, f"{abs_d}/a.txt:2: beta")
            self.assertNotIn("--", body)
            # context=0 must not emit any context lines at all
            self.assertNotIn(f"{abs_d}/a.txt:1-", body)
            self.assertNotIn(f"{abs_d}/a.txt:3-", body)

    def test_context_zero_two_files_no_separator(self):
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text("hit\n")
            Path(d, "b.txt").write_text("hit\n")
            body = _body(search_files.fn("hit", path=d, context=0))
            self.assertNotIn("--", body)
            self.assertIn(f"{abs_d}/a.txt:1: hit", body)
            self.assertIn(f"{abs_d}/b.txt:1: hit", body)

    def test_no_matches_still_returns_header(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("nothing here\n")
            result = search_files.fn("xyzzy", path=d, context=2)
            self.assertIn("Searched", result)
            self.assertIn("No matches found.", result)


class TestSearchFilesContextBasic(unittest.TestCase):

    def test_context_one_emits_before_and_after_lines(self):
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text("l1\nl2\nHIT\nl4\nl5\n")
            body = _body(search_files.fn("HIT", path=d, context=1))
            lines = body.split("\n")
            self.assertEqual(lines, [
                f"{abs_d}/a.txt:2- l2",
                f"{abs_d}/a.txt:3: HIT",
                f"{abs_d}/a.txt:4- l4",
            ])

    def test_context_clamps_at_file_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text("HIT\nl2\nl3\n")
            body = _body(search_files.fn("HIT", path=d, context=5))
            lines = body.split("\n")
            self.assertEqual(lines[0], f"{abs_d}/a.txt:1: HIT")
            self.assertNotIn(f"{abs_d}/a.txt:0-", body)
            self.assertNotIn(f"{abs_d}/a.txt:4-", body)
            self.assertEqual(len(lines), 3)

    def test_negative_context_returns_error(self):
        # Negative context is rejected with a clear error (not silently clamped to 0).
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nHIT\nl3\n")
            result = search_files.fn("HIT", path=d, context=-5)
            self.assertIn("Error", result)
            self.assertIn("context must be >= 0", result)

    def test_negative_context_minus_one_returns_error(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nHIT\nl3\n")
            result = search_files.fn("HIT", path=d, context=-1)
            self.assertIn("Error", result)
            self.assertIn("context must be >= 0", result)

    def test_context_bool_true_returns_error(self):
        # Booleans must be rejected — True == 1 in Python but is not a valid int param.
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nHIT\nl3\n")
            result = search_files.fn("HIT", path=d, context=True)
            self.assertIn("Error", result)
            self.assertIn("bool", result)

    def test_context_bool_false_returns_error(self):
        # Booleans must be rejected — False == 0 in Python but is not a valid int param.
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nHIT\nl3\n")
            result = search_files.fn("HIT", path=d, context=False)
            self.assertIn("Error", result)
            self.assertIn("bool", result)

    def test_absurd_context_clamped_to_max(self):
        lines = [f"line{i}" for i in range(1, 101)]
        lines[49] = "HIT"
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text("\n".join(lines) + "\n")
            result = search_files.fn("HIT", path=d, context=9999)
            body = _body(result)
            emitted = body.split("\n")
            # With cap = 20, window is [30..70] = 41 lines.
            self.assertEqual(len(emitted), 2 * search_files._MAX_CONTEXT + 1)
            self.assertTrue(emitted[0].startswith(f"{abs_d}/a.txt:30- "))
            self.assertTrue(emitted[-1].startswith(f"{abs_d}/a.txt:70- "))

    def test_over_max_context_note_in_header_dir_search(self):
        """Header must say 'context capped to N' when context > _MAX_CONTEXT."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nHIT\nl3\n")
            result = search_files.fn("HIT", path=d, context=9999)
            header, _, _ = result.partition("]\n")
            self.assertIn(f"context capped to {search_files._MAX_CONTEXT}", header)

    def test_over_max_context_note_in_header_single_file(self):
        """Single-file path also emits the cap note in the header."""
        with tempfile.TemporaryDirectory() as d:
            fpath = Path(d, "a.txt")
            fpath.write_text("l1\nHIT\nl3\n")
            result = search_files.fn("HIT", path=str(fpath), context=50)
            header, _, _ = result.partition("]\n")
            self.assertIn(f"context capped to {search_files._MAX_CONTEXT}", header)

    def test_within_max_context_no_cap_note(self):
        """Header must NOT include the cap note when context <= _MAX_CONTEXT."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nHIT\nl3\n")
            result = search_files.fn("HIT", path=d, context=search_files._MAX_CONTEXT)
            header, _, _ = result.partition("]\n")
            self.assertNotIn("context capped", header)


class TestSearchFilesContextGrouping(unittest.TestCase):

    def test_context_merges_adjacent_windows(self):
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text(
                "l1\nl2\nHIT3\nl4\nHIT5\nl6\nl7\nl8\nl9\nl10\n"
            )
            body = _body(search_files.fn("HIT", path=d, context=2))
            self.assertNotIn("\n--\n", body)
            self.assertIn(f"{abs_d}/a.txt:3: HIT3", body)
            self.assertIn(f"{abs_d}/a.txt:5: HIT5", body)
            self.assertIn(f"{abs_d}/a.txt:4- l4", body)
            lines = body.split("\n")
            self.assertEqual(lines[0], f"{abs_d}/a.txt:1- l1")
            self.assertEqual(lines[-1], f"{abs_d}/a.txt:7- l7")

    def test_context_separates_disjoint_windows(self):
        content = "\n".join([f"l{i}" if i not in (3, 15) else "HIT" for i in range(1, 21)]) + "\n"
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text(content)
            body = _body(search_files.fn("HIT", path=d, context=1))
            self.assertIn("\n--\n", body)
            groups = body.split("\n--\n")
            self.assertEqual(len(groups), 2)
            self.assertIn(f"{abs_d}/a.txt:3: HIT", groups[0])
            self.assertIn(f"{abs_d}/a.txt:2- l2", groups[0])
            self.assertIn(f"{abs_d}/a.txt:4- l4", groups[0])
            self.assertIn(f"{abs_d}/a.txt:15: HIT", groups[1])
            self.assertIn(f"{abs_d}/a.txt:14- l14", groups[1])
            self.assertIn(f"{abs_d}/a.txt:16- l16", groups[1])

    def test_context_separates_between_files(self):
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text("pre\nHIT\npost\n")
            Path(d, "b.txt").write_text("pre\nHIT\npost\n")
            body = _body(search_files.fn("HIT", path=d, context=1))
            self.assertIn("\n--\n", body)
            groups = body.split("\n--\n")
            self.assertEqual(len(groups), 2)
            self.assertIn(f"{abs_d}/a.txt:2: HIT", groups[0])
            self.assertIn(f"{abs_d}/b.txt:2: HIT", groups[1])


class TestSearchFilesHeaderIdentity(unittest.TestCase):

    def test_header_names_resolved_absolute_path_on_hit(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("HIT\n")
            result = search_files.fn("HIT", path=d)
            resolved = str(Path(d).resolve())
            header, sep, _ = result.partition("]\n")
            self.assertTrue(sep, "header terminator ']\\n' missing")
            self.assertIn(f"'{resolved}'", header)
            self.assertIn("1 files", header)
            self.assertIn("1 matched", header)

    def test_header_names_resolved_absolute_path_on_miss_with_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("nothing here\n")
            result = search_files.fn("xyzzy", path=d)
            resolved = str(Path(d).resolve())
            header, _, body = result.partition("]\n")
            self.assertIn(f"'{resolved}'", header)
            self.assertIn("1 files", header)
            self.assertIn("0 matched", header)
            self.assertEqual(body, "No matches found.")
            self.assertNotIn("No files were searched", body)
            self.assertNotIn("pass path=", body)

    def test_zero_files_emits_hint_line(self):
        with tempfile.TemporaryDirectory() as d:
            # No files written — dir is empty.
            result = search_files.fn("HIT", path=d)
            resolved = str(Path(d).resolve())
            header, _, body = result.partition("]\n")
            self.assertIn(f"'{resolved}'", header)
            self.assertIn("0 files", header)
            self.assertIn("No files were searched under", body)
            self.assertIn(f"'{resolved}'", body)
            self.assertIn("pass path=", body)

    def test_zero_files_glob_filter_emits_glob_hint(self):
        with tempfile.TemporaryDirectory() as d:
            # Directory has .py files but we search for .rb — glob filters everything.
            Path(d, "test.py").write_text("needle\n")
            Path(d, "other.py").write_text("more content\n")
            result = search_files.fn("needle", path=d, glob="*.rb")
            resolved = str(Path(d).resolve())
            header, _, body = result.partition("]\n")
            self.assertIn("0 files", header)
            # Must mention the glob pattern, not suggest the path is wrong
            self.assertIn("*.rb", body)
            self.assertIn("glob", body.lower())
            self.assertNotIn("pass path=", body)
            self.assertNotIn("If you meant a different directory", body)

    def test_zero_files_glob_filter_does_not_blame_path(self):
        with tempfile.TemporaryDirectory() as d:
            # Variant: a non-standard glob that excludes the one present file.
            Path(d, "config.json").write_text('{"key": "value"}\n')
            result = search_files.fn("key", path=d, glob="*.yaml")
            self.assertNotIn("If you meant a different directory", result)
            self.assertIn("*.yaml", result)

    def test_header_shape_body_partition_still_works(self):
        # Cycle 0003's _body() helper partitions on "]\n". Prove that still
        # works for every shape the new header can take: hit, miss-with-files,
        # and zero-files.
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("HIT\nmore\n")
            for pattern in ("HIT", "xyzzy"):
                result = search_files.fn(pattern, path=d)
                self.assertIn("]\n", result)
                body = _body(result)
                self.assertNotIn("[Searched", body)
        with tempfile.TemporaryDirectory() as d:
            result = search_files.fn("HIT", path=d)
            self.assertIn("]\n", result)
            body = _body(result)
            self.assertNotIn("[Searched", body)


class TestSearchFilesDefinition(unittest.TestCase):

    def test_definition_advertises_context_param(self):
        props = search_files.definition["function"]["parameters"]["properties"]
        self.assertIn("context", props)
        self.assertEqual(props["context"]["type"], "integer")
        self.assertEqual(props["context"]["default"], 3)
        self.assertEqual(props["context"]["minimum"], 0)
        self.assertNotIn("context", search_files.definition["function"]["parameters"].get("required", []))

    def test_default_context_matches_definition(self):
        import inspect
        sig = inspect.signature(search_files.fn)
        self.assertEqual(
            sig.parameters["context"].default,
            search_files.definition["function"]["parameters"]["properties"]["context"]["default"],
        )

    def test_definition_warns_about_cwd_default(self):
        main_desc = search_files.definition["function"]["description"].lower()
        self.assertIn("automation", main_desc)
        self.assertIn("working directory", main_desc)

        path_desc = (
            search_files.definition["function"]["parameters"]
            ["properties"]["path"]["description"]
            .lower()
        )
        self.assertIn("automation", path_desc)
        self.assertIn("working directory", path_desc)


if __name__ == "__main__":
    unittest.main()

class TestSearchFilesPathIsFile(unittest.TestCase):
    """path= points to a single file (not a directory) — issue #567."""

    def test_file_path_returns_match(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "sample.py")
            p.write_text("def hello():\n    pass\ndef world():\n    pass\n")
            result = search_files.fn("def hello", path=str(p))
            self.assertIn("1 matched", result)
            body = _body(result)
            self.assertIn(f"{p.resolve()}:1:", body)
            self.assertIn("def hello", body)

    def test_file_path_no_match(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "sample.py")
            p.write_text("def hello():\n    pass\n")
            result = search_files.fn("def xyzzy_nothere", path=str(p))
            self.assertIn("0 matched", result)
            body = _body(result)
            self.assertIn("No matches found.", body)

    def test_file_path_context_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "sample.py")
            p.write_text("line1\nHIT\nline3\n")
            result = search_files.fn("HIT", path=str(p), context=0)
            body = _body(result)
            self.assertEqual(body, f"{p.resolve()}:2: HIT")

    def test_file_path_with_context(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "sample.py")
            p.write_text("before\nHIT\nafter\n")
            result = search_files.fn("HIT", path=str(p), context=1)
            body = _body(result)
            abs_p = str(p.resolve())
            self.assertIn(f"{abs_p}:2: HIT", body)
            self.assertIn(f"{abs_p}:1- before", body)
            self.assertIn(f"{abs_p}:3- after", body)

    def test_file_path_count_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "sample.py")
            p.write_text("HIT\nHIT\nnope\nHIT\n")
            result = search_files.fn("HIT", path=str(p), count_only=True)
            self.assertIn("3 results", result)
            self.assertIn("1 matched", result)

    def test_issue_567_reproduction(self):
        """Exact scenario from issue #567: path to a real file with known pattern."""
        agent_py = Path(__file__).parent.parent / "agent.py"
        if not agent_py.exists():
            self.skipTest("agent.py not found; skipping reproduction test")
        # Temporarily set cwd to the repo root so the agent.py path is inside cwd
        # (path confinement #863 requires path to be within cwd).
        orig_cwd = os.getcwd()
        repo_root = str(Path(__file__).parent.parent)
        try:
            os.chdir(repo_root)
            result = search_files.fn(
                pattern="def _classify_turn_complexity",
                path=str(agent_py),
            )
        finally:
            os.chdir(orig_cwd)
        self.assertNotIn("0 results", result)
        body = _body(result)
        self.assertNotEqual(body.strip(), "No matches found.")
        self.assertIn("def _classify_turn_complexity", body)

    def test_permission_error_on_single_file_shows_warning(self):
        """Issue #614: PermissionError on a single-file path must surface a warning,
        not silently return 'No matches found.' (which is indistinguishable from a
        successful empty search)."""
        import os
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "secret.txt")
            p.write_text("HIT\n")
            os.chmod(str(p), 0o000)
            try:
                result = search_files.fn("HIT", path=str(p))
                self.assertIn("Warning", result,
                    f"Expected a Warning in the header for an unreadable file, got: {result}")
                self.assertNotIn("def _classify_turn_complexity", result)
            finally:
                os.chmod(str(p), 0o644)


class TestSearchFilesEdgeCases(unittest.TestCase):

    def test_empty_pattern(self):
        result = search_files.fn("", path=".")
        self.assertIn("Error: Search pattern cannot be empty.", result)
        result = search_files.fn("   ", path=".")
        self.assertIn("Error: Search pattern cannot be empty.", result)

    def test_empty_glob_returns_error(self):
        """An empty glob string silently matched 0 files; now it must return an error."""
        result = search_files.fn("def ", glob="", path=".")
        self.assertIn("Error: glob filter cannot be empty", result)
        # Whitespace-only glob should also be rejected
        result = search_files.fn("def ", glob="   ", path=".")
        self.assertIn("Error: glob filter cannot be empty", result)

    def test_glob_with_path_separator_returns_error(self):
        """A glob like 'tools/*.py' silently matched 0 files because fnmatch
        only sees the bare filename, never the path prefix.  It must now return
        an actionable error instead of a silent zero-result response."""
        result = search_files.fn("def fn", glob="tools/*.py", path=".")
        self.assertIn("Error:", result)
        self.assertIn("path separator", result)
        # Deeply nested separator also rejected
        result = search_files.fn("def fn", glob="a/b/c/*.py", path=".")
        self.assertIn("Error:", result)
        self.assertIn("path separator", result)
        # Plain glob without separator must still work (not caught by this guard)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "hello.py").write_text("def fn(): pass\n")
            result = search_files.fn("def fn", glob="*.py", path=d)
            self.assertNotIn("Error:", result)
            self.assertIn("def fn", result)

    def test_invalid_regex(self):
        result = search_files.fn("[", path=".")
        self.assertIn("Error: invalid regex pattern", result)

    def test_nonexistent_path(self):
        import uuid
        path = f"/tmp/nonexistent_{uuid.uuid4()}"
        result = search_files.fn("HIT", path=path)
        self.assertIn(f"Error: path '{path}' does not exist", result)

    def test_permission_error_handling(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("HIT\n")
            # Mock os.walk to simulate a PermissionError in one of the directories
            with patch("os.walk") as mock_walk:
                # Return one valid dir, then simulate error via the onerror callback
                def side_effect(top, topdown=True, onerror=None):
                    if onerror:
                        onerror(PermissionError("Permission denied"))
                    yield (top, [], ["a.txt"])
                
                mock_walk.side_effect = side_effect
                result = search_files.fn("HIT", path=d)
                self.assertIn("Warning: 1 directories skipped due to permissions", result)

    def test_truncation_at_max_results(self):
        with tempfile.TemporaryDirectory() as d:
            # Create 110 files, each with a match. _MAX_RESULTS is 100.
            for i in range(110):
                Path(d, f"file_{i}.txt").write_text("HIT\n")
            result = search_files.fn("HIT", path=d, context=0)
            self.assertIn("(truncated)", result)
            # Verify we only got 100 results in the body
            body = _body(result)
            self.assertEqual(len(body.split("\n")), 100)


class TestDefaultExcludes(unittest.TestCase):
    """Tests for DEFAULT_EXCLUDES and include_temp — issue #568."""

    def _make_tree(self, d):
        """Create a tree with agent.py at root and a copy under temp/foo/."""
        root = Path(d)
        (root / "agent.py").write_text("def my_function(): pass\n")
        (root / "temp").mkdir()
        (root / "temp" / "foo").mkdir()
        (root / "temp" / "foo" / "agent.py").write_text("def my_function(): pass\n")
        return root

    def test_default_excludes_constant_exists(self):
        self.assertTrue(hasattr(search_files, "DEFAULT_EXCLUDES"))
        self.assertIsInstance(search_files.DEFAULT_EXCLUDES, list)
        self.assertIn("temp/", search_files.DEFAULT_EXCLUDES)
        self.assertIn("worktrees/", search_files.DEFAULT_EXCLUDES)
        self.assertIn("state/debug/", search_files.DEFAULT_EXCLUDES)

    def test_default_hides_temp_directory(self):
        """By default, files under temp/ must not appear in results."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d)
            result = search_files.fn("my_function", path=d, context=0)
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            self.assertEqual(len(lines), 1, f"Expected 1 match, got: {lines}")
            abs_d = str(Path(d).resolve())
            self.assertIn(f"{abs_d}/agent.py:1:", lines[0])
            self.assertNotIn("temp", lines[0])

    def test_include_temp_shows_all_matches(self):
        """With include_temp=True both files must appear in results."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d)
            result = search_files.fn(
                "my_function", path=d, context=0, include_temp=True
            )
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            self.assertEqual(len(lines), 2, f"Expected 2 matches, got: {lines}")
            paths = {l.split(":")[0] for l in lines}
            self.assertTrue(
                any("temp" in p for p in paths),
                f"Expected a temp/ path in results, got: {paths}",
            )

    def test_worktrees_excluded_by_default(self):
        """Files under worktrees/ are excluded by default."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "main.py").write_text("TARGET_SYMBOL = 1\n")
            (root / "worktrees").mkdir()
            (root / "worktrees" / "br").mkdir()
            (root / "worktrees" / "br" / "main.py").write_text("TARGET_SYMBOL = 1\n")
            result = search_files.fn("TARGET_SYMBOL", path=d, context=0)
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            self.assertEqual(len(lines), 1, f"Expected 1 match, got: {lines}")
            self.assertNotIn("worktrees", lines[0])

    def test_include_temp_false_is_default(self):
        """Calling fn without include_temp must behave the same as include_temp=False."""
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d)
            result_default = search_files.fn("my_function", path=d, context=0)
            result_explicit = search_files.fn(
                "my_function", path=d, context=0, include_temp=False
            )
            self.assertEqual(result_default, result_explicit)


class TestSearchFilesPathWhitespace(unittest.TestCase):
    """A path with leading/trailing whitespace must be treated the same as a
    trimmed path — the tool should strip it rather than returning 'does not exist'."""

    def test_directory_path_with_spaces_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "sample.py").write_text("def greet(): pass\n", encoding="utf-8")
            result = search_files.fn("greet", path="  " + d + "  ", context=0)
            self.assertNotIn("does not exist", result)
            self.assertIn("greet", result)

    def test_file_path_with_spaces_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "sample.py"
            target.write_text("def hello(): pass\n", encoding="utf-8")
            result = search_files.fn("hello", path=" " + str(target) + " ", context=0)
            self.assertNotIn("does not exist", result)
            self.assertIn("hello", result)


class TestSearchFilesBinarySkip(unittest.TestCase):
    """search_files must skip binary files rather than returning garbage content — issue #632."""

    def _make_binary(self, path: Path) -> None:
        """Write a file that contains null bytes (binary marker)."""
        path.write_bytes(b"ELF\x00\x01\x02\x03hello\x00world\x00test\x00data")

    def test_single_binary_file_returns_skipped_message(self):
        """Directly passing a binary file as path= must return a clear skip message."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "binary.bin")
            self._make_binary(p)
            result = search_files.fn(pattern="test", path=str(p))
            self.assertIn("binary file", result)
            self.assertNotIn("\x00", result)
            # Must not contain raw binary garbage
            self.assertFalse(
                any(ord(c) < 32 and c not in "\n\r\t" for c in result),
                f"Result contains control/binary characters: {repr(result[:200])}",
            )

    def test_single_binary_file_no_matches_message(self):
        """Skip message should not look like a successful empty search."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "binary.bin")
            self._make_binary(p)
            result = search_files.fn(pattern="test", path=str(p))
            # Should not look like "searched N files, N matched" with 0 results
            # as if it were a normal text file that happened to have no matches.
            self.assertNotIn("1 files, 1 matched", result)

    def test_directory_with_binary_skips_binary_finds_text(self):
        """In a directory, binary files are silently skipped; text matches still appear."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "real.txt").write_text("this is a test\n")
            self._make_binary(Path(d, "binary.bin"))
            result = search_files.fn(pattern="test", path=d, context=0)
            # The text match must be found
            self.assertIn("real.txt:1: this is a test", result)
            # No binary garbage in the output
            self.assertNotIn("\x00", result)
            self.assertFalse(
                any(ord(c) < 32 and c not in "\n\r\t" for c in result),
                f"Result contains binary garbage: {repr(result[:200])}",
            )

    def test_directory_only_binary_files_no_matches(self):
        """A directory containing only binary files should return no matches, not garbage."""
        with tempfile.TemporaryDirectory() as d:
            self._make_binary(Path(d, "a.bin"))
            self._make_binary(Path(d, "b.bin"))
            result = search_files.fn(pattern="test", path=d)
            self.assertNotIn("\x00", result)
            self.assertFalse(
                any(ord(c) < 32 and c not in "\n\r\t" for c in result),
                f"Result contains binary garbage: {repr(result[:200])}",
            )

    def test_is_binary_helper_detects_null_bytes(self):
        """_is_binary must return True for files with null bytes."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "binary.bin")
            self._make_binary(p)
            self.assertTrue(search_files._is_binary(p))

    def test_is_binary_helper_returns_false_for_text(self):
        """_is_binary must return False for normal text files."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "text.py")
            p.write_text("def hello(): pass\n")
            self.assertFalse(search_files._is_binary(p))


class TestSearchFilesNonStringGuards(unittest.TestCase):
    """Non-string inputs must return an error string, not raise AttributeError."""

    def test_pattern_int_returns_error(self):
        result = search_files.fn(pattern=42)
        self.assertIsInstance(result, str)
        self.assertIn("Error", result)

    def test_pattern_none_returns_error(self):
        result = search_files.fn(pattern=None)
        self.assertIsInstance(result, str)
        self.assertIn("Error", result)

    def test_path_int_returns_error(self):
        result = search_files.fn(pattern="x", path=42)
        self.assertIsInstance(result, str)
        self.assertIn("Error", result)

    def test_path_none_returns_error(self):
        result = search_files.fn(pattern="x", path=None)
        self.assertIsInstance(result, str)
        self.assertIn("Error", result)


class TestSearchFilesContextFormatUnambiguous(unittest.TestCase):
    """Issue #672: context-line format must be unambiguous even when
    the filename contains hyphens.

    Before the fix, context lines used ``file-linenum- text`` which is
    indistinguishable from a file named ``my-mod-utils`` followed by
    ``-3- text``.  After the fix both match lines and context lines
    always use a colon to separate the filename from the line number:

      match line:   ``file:linenum: text``
      context line: ``file:linenum- text``
    """

    def test_context_lines_use_colon_separator(self):
        """Context lines must use 'file:linenum-' not 'file-linenum-'."""
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "a.txt").write_text("before\nHIT\nafter\n")
            body = _body(search_files.fn("HIT", path=d, context=1))
            # match line: colon on both sides of linenum
            self.assertIn(f"{abs_d}/a.txt:2: HIT", body)
            # context lines: colon before linenum, dash after
            self.assertIn(f"{abs_d}/a.txt:1- before", body)
            self.assertIn(f"{abs_d}/a.txt:3- after", body)
            # old format must not appear
            self.assertNotIn("a.txt-1-", body)
            self.assertNotIn("a.txt-3-", body)

    def test_hyphenated_filename_context_lines_unambiguous(self):
        """Context lines for a file like 'my-mod-utils.py' must still be
        parseable — the colon always separates filename from line number."""
        with tempfile.TemporaryDirectory() as d:
            fpath = Path(d, "my-mod-utils.py")
            abs_fpath = str(fpath.resolve())
            fpath.write_text("setup\nconfig\nHIT_LINE\ncleanup\ndone\n")
            body = _body(search_files.fn("HIT_LINE", path=d, context=2))
            # Each line must start with the full absolute path followed by a colon
            for line in body.split("\n"):
                self.assertTrue(
                    line.startswith(f"{abs_fpath}:"),
                    f"Line does not start with '{abs_fpath}:': {line!r}",
                )
            # Match line uses double-colon (file:linenum: text)
            self.assertIn(f"{abs_fpath}:3: HIT_LINE", body)
            # Context lines use colon-dash (file:linenum- text)
            self.assertIn(f"{abs_fpath}:1- setup", body)
            self.assertIn(f"{abs_fpath}:2- config", body)
            self.assertIn(f"{abs_fpath}:4- cleanup", body)
            self.assertIn(f"{abs_fpath}:5- done", body)

    def test_single_file_context_lines_use_colon_separator(self):
        """Single-file searches (path points directly to a file) must also use
        the new unambiguous format."""
        with tempfile.TemporaryDirectory() as d:
            fpath = Path(d, "data-pipeline-utils.py")
            abs_fpath = str(fpath.resolve())
            fpath.write_text("import os\nimport sys\ndef main():\n    pass\n")
            body = _body(search_files.fn("def main", path=str(fpath), context=1))
            self.assertIn(f"{abs_fpath}:3: def main", body)
            self.assertIn(f"{abs_fpath}:2- import sys", body)
            self.assertIn(f"{abs_fpath}:4- ", body)
            # old format must not appear
            self.assertNotIn("data-pipeline-utils.py-2-", body)
            self.assertNotIn("data-pipeline-utils.py-4-", body)


class TestSearchFilesIncludeHidden(unittest.TestCase):
    """search_files must skip hidden files/dirs by default but expose them when
    include_hidden=True — issue #676."""

    def test_hidden_file_skipped_by_default(self):
        """A hidden file (dotfile) must not appear in default search results."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".hidden.txt").write_text("findme\n")
            Path(d, "visible.txt").write_text("nothing\n")
            result = search_files.fn("findme", path=d, context=0)
            self.assertNotIn("findme", _body(result))
            self.assertIn("1 files", result)  # only visible.txt counted

    def test_hidden_file_found_with_include_hidden(self):
        """include_hidden=True must expose hidden files."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".hidden.txt").write_text("findme\n")
            Path(d, "visible.txt").write_text("nothing\n")
            result = search_files.fn("findme", path=d, context=0, include_hidden=True)
            self.assertIn("findme", _body(result))
            self.assertIn("2 files", result)  # both files searched
            self.assertIn("1 matched", result)

    def test_hidden_dir_skipped_by_default(self):
        """A hidden directory must not be traversed by default."""
        with tempfile.TemporaryDirectory() as d:
            hidden_dir = Path(d, ".hidden_dir")
            hidden_dir.mkdir()
            (hidden_dir / "secret.txt").write_text("findme\n")
            Path(d, "visible.txt").write_text("nothing\n")
            result = search_files.fn("findme", path=d, context=0)
            self.assertNotIn("findme", _body(result))

    def test_hidden_dir_found_with_include_hidden(self):
        """include_hidden=True must traverse hidden directories."""
        with tempfile.TemporaryDirectory() as d:
            hidden_dir = Path(d, ".hidden_dir")
            hidden_dir.mkdir()
            (hidden_dir / "secret.txt").write_text("findme\n")
            Path(d, "visible.txt").write_text("nothing\n")
            result = search_files.fn("findme", path=d, context=0, include_hidden=True)
            self.assertIn("findme", _body(result))

    def test_git_dir_always_excluded(self):
        """.git/ must remain excluded even when include_hidden=True."""
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d, ".git")
            git_dir.mkdir()
            (git_dir / "config").write_text("findme\n")
            Path(d, "visible.txt").write_text("nothing\n")
            result = search_files.fn("findme", path=d, context=0, include_hidden=True)
            self.assertNotIn("findme", _body(result))
            self.assertNotIn(".git", result)

    def test_dotenv_file_found_with_include_hidden(self):
        """.env files are a common real-world use case."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text("SECRET_KEY=abc123\n")
            result = search_files.fn("SECRET_KEY", path=d, context=0, include_hidden=True)
            self.assertIn("SECRET_KEY", _body(result))

    def test_definition_advertises_include_hidden_param(self):
        """The tool definition must expose include_hidden so the LLM can use it."""
        props = search_files.definition["function"]["parameters"]["properties"]
        self.assertIn("include_hidden", props)
        self.assertEqual(props["include_hidden"]["type"], "boolean")
        self.assertFalse(props["include_hidden"]["default"])
        self.assertIn(".git", props["include_hidden"]["description"])


# ── wrong-type context tests (#680) ───────────────────────────────────────────

class TestSearchFilesContextTypeCoercion(unittest.TestCase):
    """search_files must not raise TypeError when context is a non-int (#680)."""

    def test_string_integer_context_coerced_single_file(self):
        """context='3' (stringified int) must be coerced and produce results, not crash."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "f.py")
            p.write_text("def foo():\n    pass\ndef bar():\n    pass\n")
            result = search_files.fn("def", path=str(p), context='2')
            self.assertNotIn("Error", result)
            self.assertIn("def", result)

    def test_string_integer_context_coerced_directory(self):
        """context='0' over a directory must work exactly like context=0."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("def alpha():\n    pass\n")
            result_int = search_files.fn("def", path=d, context=0)
            result_str = search_files.fn("def", path=d, context='0')
            self.assertEqual(result_int, result_str)

    def test_non_numeric_string_context_returns_error(self):
        """context='bad' must return a clean Error string, not raise TypeError."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("x = 1\n")
            result = search_files.fn("x", path=d, context='bad')
            self.assertTrue(result.startswith("Error: context must be an integer"))
            self.assertIn("'str'", result)

    def test_bool_context_returns_error(self):
        """context=True/False (bool) must be rejected with a clear error (#769).
        Previously bools were silently coerced to int; now they are rejected so
        callers get a helpful message instead of surprising behaviour."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("x = 1\n")
            result_true = search_files.fn("x", path=d, context=True)
            self.assertIn("Error", result_true)
            self.assertIn("bool", result_true)
            result_false = search_files.fn("x", path=d, context=False)
            self.assertIn("Error", result_false)
            self.assertIn("bool", result_false)


class TestSearchFilesAbsolutePaths(unittest.TestCase):
    """Match lines must always use absolute paths — issue #690."""

    def test_directory_search_match_lines_are_absolute(self):
        """Match lines from a directory search must use absolute paths."""
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "foo.py").write_text("TARGET = 1\n")
            result = search_files.fn("TARGET", path=d, context=0)
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            self.assertEqual(len(lines), 1)
            path_part = lines[0].split(":")[0]
            self.assertTrue(
                path_part.startswith("/"),
                f"Expected absolute path in match line, got: {lines[0]!r}",
            )
            self.assertEqual(path_part, f"{abs_d}/foo.py")

    def test_single_file_match_lines_are_absolute(self):
        """Match lines from a single-file search must use the absolute path."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "bar.py")
            p.write_text("TARGET = 1\n")
            result = search_files.fn("TARGET", path=str(p), context=0)
            body = _body(result)
            path_part = body.split(":")[0]
            self.assertTrue(
                path_part.startswith("/"),
                f"Expected absolute path in match line, got: {body!r}",
            )
            self.assertEqual(path_part, str(p.resolve()))

    def test_match_lines_absolute_from_different_cwd(self):
        """Match lines must be absolute even when cwd differs from search path."""
        import os
        orig_cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            with tempfile.TemporaryDirectory() as d:
                abs_d = str(Path(d).resolve())
                Path(d, "mod.py").write_text("def hit(): pass\n")
                result = search_files.fn("def hit", path=d, context=0)
                body = _body(result)
                path_part = body.split(":")[0]
                self.assertTrue(
                    path_part.startswith("/"),
                    f"Expected absolute path when cwd=/tmp, got: {body!r}",
                )
                self.assertEqual(path_part, f"{abs_d}/mod.py")
        finally:
            os.chdir(orig_cwd)

    def test_context_lines_are_absolute(self):
        """Context lines (before/after match) must also use absolute paths."""
        with tempfile.TemporaryDirectory() as d:
            abs_d = str(Path(d).resolve())
            Path(d, "ctx.py").write_text("before\nHIT\nafter\n")
            result = search_files.fn("HIT", path=d, context=1)
            body = _body(result)
            for line in body.split("\n"):
                if line.strip():
                    self.assertTrue(
                        line.startswith(abs_d),
                        f"Expected line to start with abs path, got: {line!r}",
                    )


class TestSearchFilesCaseSensitiveDefault(unittest.TestCase):
    """search_files must be case-sensitive by default — issue #724.

    ignore_case was mistakenly defaulting to True, causing every search to
    act as case-insensitive even when the caller did not specify the flag.
    The correct default is False (case-sensitive), matching grep behaviour.
    """

    def test_default_is_case_sensitive(self):
        """Without ignore_case, only the exact-case line must match."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "case.py").write_text("Hello World\nhELLO wORLD\nhello world\n")
            result = search_files.fn(pattern="hello", path=d, context=0)
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            # Only the all-lowercase line 3 should match
            self.assertEqual(len(lines), 1, f"Expected 1 match, got {len(lines)}: {lines}")
            self.assertIn("hello world", lines[0])
            self.assertNotIn("Hello World", body)
            self.assertNotIn("hELLO wORLD", body)

    def test_explicit_false_is_case_sensitive(self):
        """ignore_case=False must match only the exact-case line."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "case.py").write_text("Hello World\nhELLO wORLD\nhello world\n")
            result = search_files.fn(pattern="hello", path=d, context=0, ignore_case=False)
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn("hello world", lines[0])

    def test_explicit_true_matches_all_cases(self):
        """ignore_case=True must match all three variants."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "case.py").write_text("Hello World\nhELLO wORLD\nhello world\n")
            result = search_files.fn(pattern="hello", path=d, context=0, ignore_case=True)
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            self.assertEqual(len(lines), 3, f"Expected 3 matches with ignore_case=True, got {len(lines)}: {lines}")

    def test_definition_default_is_false(self):
        """The tool JSON definition must advertise ignore_case default as False."""
        props = search_files.definition["function"]["parameters"]["properties"]
        self.assertIn("ignore_case", props)
        self.assertFalse(
            props["ignore_case"]["default"],
            "ignore_case default in definition must be False",
        )

    def test_function_signature_default_is_false(self):
        """The Python function signature must have ignore_case defaulting to False."""
        import inspect
        sig = inspect.signature(search_files.fn)
        default = sig.parameters["ignore_case"].default
        self.assertIs(default, False,
            f"Expected ignore_case default to be False, got {default!r}")

    def test_definition_default_matches_signature(self):
        """The JSON definition default must match the Python function signature default."""
        import inspect
        sig = inspect.signature(search_files.fn)
        sig_default = sig.parameters["ignore_case"].default
        def_default = search_files.definition["function"]["parameters"]["properties"]["ignore_case"]["default"]
        self.assertEqual(sig_default, def_default,
            f"Signature default {sig_default!r} does not match definition default {def_default!r}")


class TestSearchFilesContext0VsContextN(unittest.TestCase):
    """Regression tests for #750: context=0 and context>0 must agree on which
    lines match when the pattern can match the trailing newline.

    Before the fix, context=0 called ``regex.search(line)`` on the raw line
    (which ends with ``\\n``), while context>0 rstripped first.  Any pattern
    containing ``\\s``, ``\\s+``, ``\\s*`` or similar could match the trailing
    newline in context=0 but not in context>0, producing different hit counts.
    """

    def test_trailing_newline_not_matched_in_context0(self):
        r"""Pattern 'hello\s' must NOT match a line containing only 'hello\n'
        (the trailing newline is not part of the meaningful line content)."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.py").write_text("hello world\nhello\nworld\n")
            result = search_files.fn(r"hello\s", path=d, context=0)
            body = _body(result)
            lines = [l for l in body.split("\n") if l.strip()]
            # Only 'hello world' (has a real space after 'hello') should match.
            self.assertEqual(len(lines), 1, f"Expected 1 match, got {len(lines)}: {lines}")
            self.assertIn("hello world", lines[0])
            self.assertNotIn("test.py:2:", body)

    def test_context0_and_context3_agree_on_match_count(self):
        r"""context=0 and context=3 must find the same number of matches for
        a pattern that would previously match the trailing newline."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.py").write_text("hello world\nhello\nworld\n")
            result0 = search_files.fn(r"hello\s", path=d, context=0)
            result3 = search_files.fn(r"hello\s", path=d, context=3)
            # Extract match count from header
            import re as _re
            count0 = int(_re.search(r"(\d+) results", result0).group(1))
            count3 = int(_re.search(r"(\d+) results", result3).group(1))
            self.assertEqual(count0, count3,
                f"context=0 found {count0} matches but context=3 found {count3}")

    def test_single_file_context0_and_context1_agree(self):
        r"""Single-file path: context=0 and context=1 must agree on match count
        for a pattern containing \s."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "test.py")
            p.write_text("word here\nword\nother\n")
            result0 = search_files.fn(r"word\s", path=str(p), context=0)
            result1 = search_files.fn(r"word\s", path=str(p), context=1)
            import re as _re
            count0 = int(_re.search(r"(\d+) results", result0).group(1))
            count1 = int(_re.search(r"(\d+) results", result1).group(1))
            self.assertEqual(count0, count1,
                f"Single-file: context=0 found {count0} but context=1 found {count1}")

    def test_count_only_consistent_with_context0(self):
        r"""count_only=True must also agree with context=0 on match count."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.py").write_text("hello world\nhello\nworld\n")
            result0 = search_files.fn(r"hello\s", path=d, context=0)
            result_count = search_files.fn(r"hello\s", path=d, count_only=True)
            import re as _re
            count0 = int(_re.search(r"(\d+) results", result0).group(1))
            count_only_val = int(_re.search(r"(\d+) results", result_count).group(1))
            self.assertEqual(count0, count_only_val,
                f"context=0 found {count0} but count_only found {count_only_val}")

    def test_pattern_with_whitespace_anchor_consistent(self):
        r"""Pattern '\bword\b\s*$' (end-of-word possibly with trailing space)
        must give the same results in context=0 and context>0."""
        with tempfile.TemporaryDirectory() as d:
            # Line 1: 'end of line' — does NOT end with 'word'
            # Line 2: 'last word' — ends with 'word'
            # Line 3: 'word   ' — trailing spaces then nothing else
            Path(d, "test.py").write_text("end of line\nlast word\nword   \n")
            result0 = search_files.fn(r"word\s*$", path=d, context=0)
            result3 = search_files.fn(r"word\s*$", path=d, context=3)
            import re as _re
            count0 = int(_re.search(r"(\d+) results", result0).group(1))
            count3 = int(_re.search(r"(\d+) results", result3).group(1))
            self.assertEqual(count0, count3,
                f"context=0 found {count0} but context=3 found {count3}")


class TestSearchFilesNullByteValidation(unittest.TestCase):
    """Null bytes in path or pattern must return a clear error, not crash (#760)."""

    def test_null_byte_in_path_returns_error_not_exception(self):
        """path containing a null byte must return a descriptive error string.

        Before the fix, Path(path).resolve() raised ValueError: embedded null byte,
        which propagated out of fn() as an unhandled exception.
        """
        result = search_files.fn(pattern="hello", path="/tmp/valid\x00dir")
        self.assertIsInstance(result, str, "fn() must return a string, not raise")
        self.assertIn("null byte", result)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")

    def test_null_byte_in_pattern_returns_error_not_silent_failure(self):
        """pattern containing a null byte must return a descriptive error string.

        Before the fix, re.compile() silently accepted the null byte and the
        search returned zero matches with no indication of the bad input.
        """
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.py").write_text("hello\n")
            result = search_files.fn(pattern="hel\x00lo", path=d)
        self.assertIsInstance(result, str, "fn() must return a string, not raise")
        self.assertIn("null byte", result)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")

    def test_valid_path_and_pattern_still_work_after_null_checks(self):
        """Regression guard: normal searches must be unaffected by the new checks."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("hello world\n")
            result = search_files.fn(pattern="hello", path=d, context=0)
        self.assertIn("hello world", result)

    def test_null_byte_in_glob_string_returns_error(self):
        """glob string containing a null byte must return a clear error, not silently
        match zero files (regression for the bug where '*.py\\x00bad' produced a
        misleading 'No files matched' message instead of 'Error: ...')."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("def foo(): pass\n")
            result = search_files.fn(pattern="def", path=d, glob="*.py\x00bad")
        self.assertIsInstance(result, str, "fn() must return a string, not raise")
        self.assertIn("null byte", result)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")

    def test_null_byte_in_glob_list_element_returns_error(self):
        """glob list element containing a null byte must return a clear error."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("def foo(): pass\n")
            result = search_files.fn(pattern="def", path=d, glob=["*.py\x00bad"])
        self.assertIsInstance(result, str, "fn() must return a string, not raise")
        self.assertIn("null byte", result)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")

    def test_null_byte_in_second_glob_list_element_returns_error(self):
        """Null byte in any list element (not just the first) must be caught."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("def foo(): pass\n")
            result = search_files.fn(pattern="def", path=d, glob=["*.py", "*.txt\x00x"])
        self.assertIsInstance(result, str, "fn() must return a string, not raise")
        self.assertIn("null byte", result)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")

    def test_glob_without_null_byte_still_works_after_check(self):
        """Regression guard: normal glob patterns must be unaffected by the new check."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("def foo(): pass\n")
            Path(d, "b.txt").write_text("def bar(): pass\n")
            result = search_files.fn(pattern="def", path=d, glob="*.py", context=0)
        self.assertIn("a.py", result)
        self.assertNotIn("b.txt", result)


# ── invalid regex patterns (#770) ─────────────────────────────────────────────

class TestSearchFilesInvalidRegex(unittest.TestCase):
    """search_files must return a clear error for invalid regex patterns instead of
    crashing with re.error (#770)."""

    def test_unclosed_bracket_returns_error(self):
        """[unclosed must produce Error: invalid regex pattern, not raise re.error."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("line\n")
            result = search_files.fn(pattern="[unclosed", path=d)
        self.assertIsInstance(result, str, "fn() must return a string, not raise")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("invalid regex pattern", result)

    def test_backslash_only_pattern_returns_error(self):
        """A bare backslash (trailing escape) must return a clear error, not re.error.

        This is a common mistake when a caller passes a single-backslash string
        that is an incomplete escape sequence.
        """
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("line\n")
            result = search_files.fn(pattern="\\", path=d)
        self.assertIsInstance(result, str, "fn() must return a string, not raise")
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("invalid regex pattern", result)

    def test_invalid_regex_on_single_file_returns_error(self):
        """Invalid regex must also be caught when path points to a single file."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "f.txt")
            p.write_text("hello\n")
            result = search_files.fn(pattern="[bad", path=str(p))
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected 'Error:' prefix: {result!r}")
        self.assertIn("invalid regex pattern", result)

    def test_valid_pattern_still_works(self):
        """Regression guard: a normal pattern must still return results after the guard."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("alpha\nbeta\n")
            result = search_files.fn(pattern="alpha", path=d, context=0)
        self.assertIn("alpha", result)
        self.assertNotIn("Error", result)
        self.assertNotIn("Error:", result)


class TestCountOnlyProbeRegression(unittest.TestCase):
    """Regression tests for probed count_only behaviors (#778).

    These verify the behaviors identified during the CICD probe session —
    all were already correct; these tests pin them so future refactors cannot
    silently regress them.
    """

    def test_count_only_zero_matches_returns_string_not_none(self):
        """count_only with no matches must return a non-empty string, not None or empty (#778)."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("hello world\n")
            result = search_files.fn(pattern="ZZZNOMATCHXYZ", path=d, count_only=True)
        self.assertIsInstance(result, str, "count_only must return a string")
        self.assertTrue(result.strip(), "count_only must not return an empty string")
        self.assertIn("Searched", result, "result must contain the Searched header")

    def test_count_only_zero_matches_contains_zero_count(self):
        """count_only with no matches must report 0 matched files and 0 results (#778)."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("hello world\n")
            result = search_files.fn(pattern="ZZZNOMATCHXYZ", path=d, count_only=True)
        self.assertIn("0 matched", result, f"Expected '0 matched' in: {result!r}")
        self.assertIn("0 results", result, f"Expected '0 results' in: {result!r}")
        # Must NOT contain match content prose — just the header
        self.assertNotIn("No matches found", result,
                         "count_only must not emit 'No matches found' prose")

    def test_count_only_with_context_arg_ignores_context(self):
        """count_only=True must return header-only even when context > 0 is passed (#778).

        The context argument is irrelevant when only counting — must not cause
        extra output or build context windows.
        """
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("def foo(): pass\ndef bar(): pass\n")
            result_plain = search_files.fn(pattern="def ", path=d, count_only=True)
            result_ctx = search_files.fn(pattern="def ", path=d, count_only=True, context=5)
        # Both must return the same counts
        self.assertEqual(result_plain, result_ctx,
                         f"count_only with and without context must match:\n"
                         f"  plain: {result_plain!r}\n  ctx=5: {result_ctx!r}")
        # Neither must contain match content
        self.assertNotIn("def foo", result_ctx)
        self.assertNotIn("def bar", result_ctx)
        self.assertNotIn("--", result_ctx, "Context separator must not appear with count_only")

    def test_count_only_with_matches_returns_correct_counts(self):
        """count_only with matches must return the correct file and result counts (#778)."""
        with tempfile.TemporaryDirectory() as d:
            # 2 files, 3 matches total
            Path(d, "a.py").write_text("def alpha(): pass\ndef beta(): pass\n")
            Path(d, "b.py").write_text("def gamma(): pass\n")
            result = search_files.fn(pattern="def ", path=d, count_only=True, glob="*.py")
        self.assertIn("2 matched", result, f"Expected 2 matched files, got: {result!r}")
        self.assertIn("3 results", result, f"Expected 3 results, got: {result!r}")
        # No match lines
        self.assertNotIn("def alpha", result)
        self.assertNotIn("def gamma", result)


class TestSearchFilesMultiGlob(unittest.TestCase):
    """Regression tests for comma-separated and list glob patterns (#fix-misc-edges)."""

    def test_comma_separated_glob_matches_both_extensions(self):
        """glob='*.py,*.txt' must match both .py and .txt files."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("hello world\n")
            Path(d, "b.txt").write_text("hello world\n")
            Path(d, "c.md").write_text("hello world\n")
            result = search_files.fn("hello", path=d, glob="*.py,*.txt", context=0)
            self.assertIn("a.py", result)
            self.assertIn("b.txt", result)
            self.assertNotIn("c.md", result)

    def test_comma_separated_glob_with_spaces_matches(self):
        """glob='*.py, *.txt' (with space) must match both extensions after stripping."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("hello world\n")
            Path(d, "b.txt").write_text("hello world\n")
            result = search_files.fn("hello", path=d, glob="*.py, *.txt", context=0)
            self.assertIn("a.py", result)
            self.assertIn("b.txt", result)

    def test_glob_as_list_matches_multiple_extensions(self):
        """glob=['*.py', '*.txt'] must match both .py and .txt files."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("hello world\n")
            Path(d, "b.txt").write_text("hello world\n")
            Path(d, "c.md").write_text("hello world\n")
            result = search_files.fn("hello", path=d, glob=["*.py", "*.txt"], context=0)
            self.assertIn("a.py", result)
            self.assertIn("b.txt", result)
            self.assertNotIn("c.md", result)

    def test_glob_as_single_element_list(self):
        """glob=['*.py'] (one element) behaves the same as glob='*.py'."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("hit\n")
            Path(d, "b.txt").write_text("hit\n")
            result = search_files.fn("hit", path=d, glob=["*.py"], context=0)
            self.assertIn("a.py", result)
            self.assertNotIn("b.txt", result)

    def test_glob_list_empty_string_returns_error(self):
        """glob=[''] must return an error, not silently match nothing."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("hit\n")
            result = search_files.fn("hit", path=d, glob=[""])
            self.assertIn("Error", result)

    def test_glob_wrong_type_returns_error(self):
        """glob=42 (non-string, non-list) must return a clear error."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("hit\n")
            result = search_files.fn("hit", path=d, glob=42)
            self.assertIn("Error", result)

    def test_single_glob_string_still_works(self):
        """Plain single-pattern glob='*.py' continues to work after the refactor."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.py").write_text("hello\n")
            Path(d, "b.txt").write_text("hello\n")
            result = search_files.fn("hello", path=d, glob="*.py", context=0)
            self.assertIn("a.py", result)
            self.assertNotIn("b.txt", result)


class TestSearchFilesPathConfinement(unittest.TestCase):
    """search_files must refuse to search paths outside the working directory (#863).

    The conftest search_files_cwd fixture sets cwd=/droid/repos/agent for this class
    so relative paths like '.' and 'tools/' resolve inside cwd, while absolute paths
    to /etc, /home, and parent-traversal paths are correctly rejected.
    """

    def test_etc_path_returns_error(self):
        """search_files('foo', path='/etc') must return an error string."""
        result = search_files.fn("foo", path="/etc")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error: prefix, got: {result!r}")

    def test_home_path_returns_error(self):
        """search_files('foo', path='/home') must return an error string."""
        result = search_files.fn("foo", path="/home")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error: prefix, got: {result!r}")

    def test_parent_traversal_returns_error(self):
        """search_files('foo', path='../other') must return an error when it resolves outside cwd."""
        result = search_files.fn("foo", path="../other")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error:"), f"Expected Error: prefix, got: {result!r}")

    def test_error_message_mentions_outside_working_directory(self):
        """The error message must mention 'outside the working directory'."""
        result = search_files.fn("foo", path="/etc")
        self.assertIn("outside the working directory", result)

    def test_dot_path_works(self):
        """search_files('def', path='.') must still work (happy path, cwd=repo)."""
        result = search_files.fn("def", path=".", glob="*.py", context=0)
        self.assertNotIn("outside the working directory", result)
        # Should find at least one 'def' in .py files in the repo
        self.assertNotIn("Error: path", result)

    def test_relative_subdir_inside_cwd_works(self):
        """search_files('def', path='tools/') must still work (relative path inside cwd)."""
        result = search_files.fn("def", path="tools/", glob="*.py", context=0)
        self.assertNotIn("outside the working directory", result)
        self.assertNotIn("Error: path", result)
        # tools/ has Python files with 'def' in them
        self.assertIn("def", result)


class TestSearchFilesBoolParamCoercion(unittest.TestCase):
    """Boolean params (ignore_case, count_only, include_temp, include_hidden) must
    reject non-bool/non-01-int values rather than silently coercing them (#887).

    The critical failure mode is an LLM passing 'false' (a string) — non-empty
    strings are truthy in Python, so ignore_case='false' would make the search
    case-insensitive when the caller intended case-sensitive.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._f = os.path.join(self._tmpdir.name, "sample.txt")
        with open(self._f, "w") as fh:
            fh.write("Hello World\nhello world\nHELLO WORLD\n")

    def tearDown(self):
        self._tmpdir.cleanup()

    # ── ignore_case ────────────────────────────────────────────────────────────

    def test_ignore_case_string_false_returns_error(self):
        """ignore_case='false' must return a clear error, not silently go case-insensitive."""
        result = search_files.fn("hello", path=self._f, ignore_case="false")
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("ignore_case", result)
        self.assertIn("str", result)

    def test_ignore_case_string_true_returns_error(self):
        """ignore_case='true' must return a clear error — strings are not booleans."""
        result = search_files.fn("hello", path=self._f, ignore_case="true")
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("ignore_case", result)

    def test_ignore_case_integer_2_returns_error(self):
        """ignore_case=2 must return a clear error — only 0, 1, and bool are accepted."""
        result = search_files.fn("hello", path=self._f, ignore_case=2)
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("ignore_case", result)

    def test_ignore_case_zero_works_as_false(self):
        """ignore_case=0 must behave as False (case-sensitive search)."""
        result = search_files.fn("hello", path=self._f, ignore_case=0, context=0)
        # Only the lowercase 'hello world' line matches
        self.assertIn("hello world", result)
        self.assertNotIn("Hello World", result)

    def test_ignore_case_one_works_as_true(self):
        """ignore_case=1 must behave as True (case-insensitive search)."""
        result = search_files.fn("hello", path=self._f, ignore_case=1, context=0)
        # All three lines match case-insensitively
        self.assertIn("Hello World", result)
        self.assertIn("hello world", result)

    def test_ignore_case_true_unaffected(self):
        """ignore_case=True continues to work normally after adding the type check."""
        result = search_files.fn("hello", path=self._f, ignore_case=True, context=0)
        self.assertNotIn("Error:", result)
        self.assertIn("hello world", result)

    def test_ignore_case_false_unaffected(self):
        """ignore_case=False continues to do a case-sensitive search."""
        result = search_files.fn("hello", path=self._f, ignore_case=False, context=0)
        self.assertNotIn("Error:", result)
        self.assertIn("hello world", result)
        self.assertNotIn("Hello World", result)

    # ── count_only ─────────────────────────────────────────────────────────────

    def test_count_only_string_false_returns_error(self):
        """count_only='false' must return a clear error (#887)."""
        result = search_files.fn("hello", path=self._f, count_only="false")
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("count_only", result)

    # ── include_temp ───────────────────────────────────────────────────────────

    def test_include_temp_string_false_returns_error(self):
        """include_temp='false' must return a clear error (#887)."""
        result = search_files.fn("hello", path=self._f, include_temp="false")
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("include_temp", result)

    # ── include_hidden ─────────────────────────────────────────────────────────

    def test_include_hidden_string_false_returns_error(self):
        """include_hidden='false' must return a clear error (#887)."""
        result = search_files.fn("hello", path=self._f, include_hidden="false")
        self.assertTrue(result.startswith("Error:"), f"Expected error: {result!r}")
        self.assertIn("include_hidden", result)
