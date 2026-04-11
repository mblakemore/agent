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


if __name__ == "__main__":
    unittest.main()
