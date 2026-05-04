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

    def test_context_zero_matches_legacy_shape(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("alpha\nbeta\ngamma\n")
            result = search_files.fn("beta", path=d, context=0)
            body = _body(result)
            self.assertEqual(body, "a.txt:2: beta")
            self.assertNotIn("--", body)
            self.assertNotIn("a.txt-", body)

    def test_context_zero_two_files_no_separator(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("hit\n")
            Path(d, "b.txt").write_text("hit\n")
            body = _body(search_files.fn("hit", path=d, context=0))
            self.assertNotIn("--", body)
            self.assertIn("a.txt:1: hit", body)
            self.assertIn("b.txt:1: hit", body)

    def test_no_matches_still_returns_header(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("nothing here\n")
            result = search_files.fn("xyzzy", path=d, context=2)
            self.assertIn("Searched", result)
            self.assertIn("No matches found.", result)


class TestSearchFilesContextBasic(unittest.TestCase):

    def test_context_one_emits_before_and_after_lines(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nl2\nHIT\nl4\nl5\n")
            body = _body(search_files.fn("HIT", path=d, context=1))
            lines = body.split("\n")
            self.assertEqual(lines, [
                "a.txt-2- l2",
                "a.txt:3: HIT",
                "a.txt-4- l4",
            ])

    def test_context_clamps_at_file_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("HIT\nl2\nl3\n")
            body = _body(search_files.fn("HIT", path=d, context=5))
            lines = body.split("\n")
            self.assertEqual(lines[0], "a.txt:1: HIT")
            self.assertNotIn("a.txt-0-", body)
            self.assertNotIn("a.txt-4-", body)
            self.assertEqual(len(lines), 3)

    def test_negative_context_clamped_to_zero(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("l1\nHIT\nl3\n")
            body = _body(search_files.fn("HIT", path=d, context=-5))
            self.assertEqual(body, "a.txt:2: HIT")

    def test_absurd_context_clamped_to_max(self):
        lines = [f"line{i}" for i in range(1, 101)]
        lines[49] = "HIT"
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("\n".join(lines) + "\n")
            body = _body(search_files.fn("HIT", path=d, context=9999))
            emitted = body.split("\n")
            # With cap = 20, window is [30..70] = 41 lines.
            self.assertEqual(len(emitted), 2 * search_files._MAX_CONTEXT + 1)
            self.assertTrue(emitted[0].startswith("a.txt-30- "))
            self.assertTrue(emitted[-1].startswith("a.txt-70- "))


class TestSearchFilesContextGrouping(unittest.TestCase):

    def test_context_merges_adjacent_windows(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text(
                "l1\nl2\nHIT3\nl4\nHIT5\nl6\nl7\nl8\nl9\nl10\n"
            )
            body = _body(search_files.fn("HIT", path=d, context=2))
            self.assertNotIn("\n--\n", body)
            self.assertIn("a.txt:3: HIT3", body)
            self.assertIn("a.txt:5: HIT5", body)
            self.assertIn("a.txt-4- l4", body)
            lines = body.split("\n")
            self.assertEqual(lines[0], "a.txt-1- l1")
            self.assertEqual(lines[-1], "a.txt-7- l7")

    def test_context_separates_disjoint_windows(self):
        content = "\n".join([f"l{i}" if i not in (3, 15) else "HIT" for i in range(1, 21)]) + "\n"
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text(content)
            body = _body(search_files.fn("HIT", path=d, context=1))
            self.assertIn("\n--\n", body)
            groups = body.split("\n--\n")
            self.assertEqual(len(groups), 2)
            self.assertIn("a.txt:3: HIT", groups[0])
            self.assertIn("a.txt-2- l2", groups[0])
            self.assertIn("a.txt-4- l4", groups[0])
            self.assertIn("a.txt:15: HIT", groups[1])
            self.assertIn("a.txt-14- l14", groups[1])
            self.assertIn("a.txt-16- l16", groups[1])

    def test_context_separates_between_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("pre\nHIT\npost\n")
            Path(d, "b.txt").write_text("pre\nHIT\npost\n")
            body = _body(search_files.fn("HIT", path=d, context=1))
            self.assertIn("\n--\n", body)
            groups = body.split("\n--\n")
            self.assertEqual(len(groups), 2)
            self.assertIn("a.txt:2: HIT", groups[0])
            self.assertIn("b.txt:2: HIT", groups[1])


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
            self.assertIn("sample.py:1:", body)
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
            self.assertEqual(body, "sample.py:2: HIT")

    def test_file_path_with_context(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "sample.py")
            p.write_text("before\nHIT\nafter\n")
            result = search_files.fn("HIT", path=str(p), context=1)
            body = _body(result)
            self.assertIn("sample.py:2: HIT", body)
            self.assertIn("sample.py-1- before", body)
            self.assertIn("sample.py-3- after", body)

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
        result = search_files.fn(
            pattern="def _classify_turn_complexity",
            path=str(agent_py),
        )
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
            self.assertIn("agent.py:1:", lines[0])
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
