"""Tests for tools/find_symbol.py — AST-aware Python symbol lookup."""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.find_symbol import find_symbol

# Absolute path to the repo root (parent of this tests/ directory)
_REPO_ROOT = str(Path(__file__).parent.parent)
_AGENT_PY = os.path.join(_REPO_ROOT, "agent.py")
_LLM_BACKEND_PY = os.path.join(_REPO_ROOT, "llm_backend.py")


class TestFindSymbolAC1(unittest.TestCase):
    """AC1: find_symbol('_classify_turn_complexity', path='agent.py', mode='definition')
    returns exactly 1 match at line 1167 with kind='function'.
    """

    def test_single_definition_match(self):
        results = find_symbol(
            "_classify_turn_complexity",
            path=_AGENT_PY,
            mode="definition",
        )
        self.assertEqual(len(results), 1, f"Expected 1 match, got {len(results)}: {results}")
        m = results[0]
        self.assertEqual(m["line"], 1167)
        self.assertEqual(m["kind"], "function")
        self.assertEqual(m["scope"], "_classify_turn_complexity")
        self.assertIn("_classify_turn_complexity", m["context"])

    def test_kind_filter_function(self):
        results = find_symbol(
            "_classify_turn_complexity",
            path=_AGENT_PY,
            kind="function",
            mode="definition",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function")

    def test_kind_filter_class_returns_empty(self):
        results = find_symbol(
            "_classify_turn_complexity",
            path=_AGENT_PY,
            kind="class",
            mode="definition",
        )
        self.assertEqual(results, [])


class TestFindSymbolAC2(unittest.TestCase):
    """AC2: find_symbol('_classify_turn_complexity', path='.', mode='callers')
    returns at least 1 caller in agent.py.
    """

    def test_at_least_one_caller_in_agent_py(self):
        results = find_symbol(
            "_classify_turn_complexity",
            path=_REPO_ROOT,
            mode="callers",
        )
        self.assertGreater(len(results), 0, "Expected at least 1 caller")
        paths = [r["path"] for r in results]
        # At least one caller should be in agent.py
        self.assertTrue(
            any("agent.py" in p for p in paths),
            f"No caller found in agent.py; found paths: {paths}",
        )
        # All caller results should have kind='call'
        for r in results:
            self.assertEqual(r["kind"], "call")


class TestFindSymbolAC3(unittest.TestCase):
    """AC3: find_symbol('BedrockBackend', path='.', mode='definition')
    finds the class in llm_backend.py with kind='class'.
    """

    def test_bedrock_backend_class_found(self):
        results = find_symbol(
            "BedrockBackend",
            path=_REPO_ROOT,
            mode="definition",
        )
        class_matches = [r for r in results if r["kind"] == "class"]
        self.assertGreater(len(class_matches), 0, "Expected at least 1 class match for BedrockBackend")
        m = class_matches[0]
        self.assertIn("llm_backend.py", m["path"])
        self.assertEqual(m["scope"], "BedrockBackend")
        self.assertIn("BedrockBackend", m["context"])

    def test_bedrock_backend_kind_filter(self):
        results = find_symbol(
            "BedrockBackend",
            path=_REPO_ROOT,
            kind="class",
            mode="definition",
        )
        self.assertTrue(all(r["kind"] == "class" for r in results))
        self.assertTrue(any("llm_backend.py" in r["path"] for r in results))


class TestFindSymbolAC4(unittest.TestCase):
    """AC4: find_symbol('missing_xyz_zzz', path='.') returns [], not an error."""

    def test_missing_symbol_returns_empty_list(self):
        results = find_symbol("missing_xyz_zzz", path=_REPO_ROOT)
        self.assertEqual(results, [])

    def test_missing_symbol_in_file_returns_empty_list(self):
        results = find_symbol("missing_xyz_zzz", path=_AGENT_PY)
        self.assertEqual(results, [])

    def test_nonexistent_path_returns_error_dict(self):
        results = find_symbol("anything", path="/nonexistent/path/xyz")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("does not exist", results[0]["error"])

    def test_nonexistent_path_error_mentions_path(self):
        results = find_symbol("anything", path="/nonexistent/path/xyz")
        self.assertIn("/nonexistent/path/xyz", results[0]["error"])

    def test_nonexistent_path_not_confused_with_not_found(self):
        """Error dict for missing path is distinguishable from empty 'not found' result."""
        results = find_symbol("anything", path="/nonexistent/path/xyz")
        self.assertNotEqual(results, [])


class TestFindSymbolInvalidMode(unittest.TestCase):
    """AC: find_symbol with an invalid mode returns an error dict, not []."""

    def test_invalid_mode_returns_error_dict(self):
        results = find_symbol("foo", mode="invalid")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("invalid", results[0]["error"].lower())

    def test_invalid_mode_error_mentions_valid_values(self):
        results = find_symbol("foo", mode="bogus")
        self.assertIn("error", results[0])
        error_msg = results[0]["error"]
        self.assertIn("definition", error_msg)
        self.assertIn("callers", error_msg)
        self.assertIn("both", error_msg)

    def test_invalid_mode_not_confused_with_not_found(self):
        """Error dict is distinguishable from an empty 'not found' result."""
        results = find_symbol("foo", mode="typo")
        self.assertNotEqual(results, [])
        self.assertIn("error", results[0])

    def test_valid_modes_still_work(self):
        """Regression: the three valid mode values continue to work."""
        for mode in ("definition", "callers", "both"):
            results = find_symbol("missing_xyz_zzz", path=_REPO_ROOT, mode=mode)
            self.assertIsInstance(results, list)
            self.assertFalse(
                any("error" in r for r in results),
                f"mode={mode!r} returned an unexpected error: {results}",
            )


class TestFindSymbolInvalidKind(unittest.TestCase):
    """AC: find_symbol with an invalid kind returns an error dict, not []."""

    def test_invalid_kind_returns_error_dict(self):
        results = find_symbol("foo", kind="invalid")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("invalid", results[0]["error"].lower())

    def test_invalid_kind_error_mentions_valid_values(self):
        results = find_symbol("foo", kind="bogus")
        self.assertIn("error", results[0])
        error_msg = results[0]["error"]
        self.assertIn("function", error_msg)
        self.assertIn("class", error_msg)
        self.assertIn("method", error_msg)

    def test_invalid_kind_not_confused_with_not_found(self):
        """Error dict is distinguishable from an empty 'not found' result."""
        results = find_symbol("foo", kind="typo")
        self.assertNotEqual(results, [])
        self.assertIn("error", results[0])

    def test_valid_kinds_still_work(self):
        """Regression: the three valid kind values continue to work."""
        for kind in ("function", "class", "method"):
            results = find_symbol("missing_xyz_zzz", path=_REPO_ROOT, kind=kind)
            self.assertIsInstance(results, list)
            self.assertFalse(
                any("error" in r for r in results),
                f"kind={kind!r} returned an unexpected error: {results}",
            )

    def test_none_kind_still_works(self):
        """kind=None (default) continues to work without error."""
        results = find_symbol("missing_xyz_zzz", path=_REPO_ROOT, kind=None)
        self.assertIsInstance(results, list)
        self.assertFalse(any("error" in r for r in results))


class TestFindSymbolEmptyName(unittest.TestCase):
    """AC: find_symbol with an empty name returns an error dict, not []."""

    def test_empty_string_returns_error_dict(self):
        results = find_symbol("")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("empty", results[0]["error"].lower())

    def test_whitespace_only_name_returns_error_dict(self):
        """A name of only whitespace is also treated as empty."""
        results = find_symbol("   ")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("empty", results[0]["error"].lower())

    def test_empty_name_not_confused_with_not_found(self):
        """Error dict is distinguishable from an empty 'not found' result."""
        results = find_symbol("")
        self.assertNotEqual(results, [])
        self.assertIn("error", results[0])

    def test_empty_name_error_returned_without_scanning_files(self):
        """Passing a nonexistent path alongside empty name still raises the name error first."""
        results = find_symbol("", path="/nonexistent/path/xyz")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("empty", results[0]["error"].lower())


class TestFindSymbolUnit(unittest.TestCase):
    """Unit tests using temporary files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = Path(self.tmp) / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return str(p)

    def test_top_level_function_found(self):
        self._write("a.py", """\
            def my_func(x):
                return x + 1
        """)
        results = find_symbol("my_func", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function")
        self.assertEqual(results[0]["line"], 1)

    def test_class_found(self):
        self._write("b.py", """\
            class MyClass:
                pass
        """)
        results = find_symbol("MyClass", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "class")

    def test_method_found(self):
        self._write("c.py", """\
            class Foo:
                def my_method(self):
                    pass
        """)
        results = find_symbol("my_method", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "method")

    def test_callers_simple_call(self):
        self._write("d.py", """\
            def do_thing():
                pass

            do_thing()
        """)
        results = find_symbol("do_thing", path=self.tmp, mode="callers")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "call")
        self.assertEqual(results[0]["line"], 4)

    def test_callers_attribute_call(self):
        self._write("e.py", """\
            obj.do_thing(arg1, arg2)
        """)
        results = find_symbol("do_thing", path=self.tmp, mode="callers")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "call")

    def test_mode_both(self):
        self._write("f.py", """\
            def foo():
                pass

            foo()
        """)
        results = find_symbol("foo", path=self.tmp, mode="both")
        kinds = {r["kind"] for r in results}
        self.assertIn("function", kinds)
        self.assertIn("call", kinds)

    def test_syntax_error_file_skipped(self):
        # Directory search: broken files are silently skipped (other files still searched)
        self._write("bad.py", "def (broken syntax")
        results = find_symbol("anything", path=self.tmp)
        self.assertEqual(results, [])

    def test_syntax_error_single_file_returns_error_dict(self):
        # Single-file search: a SyntaxError must surface as an error dict so the
        # agent can distinguish "not found" from "file is unparseable".
        self._write("broken.py", "def foo(:\n    pass\n")
        broken_path = os.path.join(self.tmp, "broken.py")
        results = find_symbol("foo", path=broken_path)
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("SyntaxError", results[0]["error"])
        self.assertIn("path", results[0])

    def test_syntax_error_single_file_not_confused_with_not_found(self):
        # A valid file with the symbol absent must still return []
        self._write("empty.py", "def bar():\n    pass\n")
        empty_path = os.path.join(self.tmp, "empty.py")
        results = find_symbol("nonexistent_symbol", path=empty_path)
        self.assertEqual(results, [])

    def test_excludes_pycache(self):
        pycache_dir = Path(self.tmp) / "__pycache__"
        pycache_dir.mkdir()
        (pycache_dir / "hidden.py").write_text("def my_func(): pass")
        results = find_symbol("my_func", path=self.tmp, mode="definition")
        # The only .py file is inside __pycache__ which is excluded, so no
        # scannable Python files remain. The tool now returns an informative
        # error dict rather than a silent [].
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("no Python files found", results[0]["error"])

    def test_result_keys(self):
        self._write("g.py", """\
            def greet(name):
                pass
        """)
        results = find_symbol("greet", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        m = results[0]
        self.assertIn("path", m)
        self.assertIn("line", m)
        self.assertIn("kind", m)
        self.assertIn("scope", m)
        self.assertIn("context", m)

    def test_kind_filter_excludes_wrong_kind(self):
        self._write("h.py", """\
            class MyClass:
                def my_func(self):
                    pass

            def my_func():
                pass
        """)
        # kind=function should only return the top-level function, not the method
        results = find_symbol("my_func", path=self.tmp, kind="function", mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function")

        # kind=method should only return the method
        results = find_symbol("my_func", path=self.tmp, kind="method", mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "method")


class TestFindSymbolRegistered(unittest.TestCase):
    """AC5: Tool is registered in the MAP_FN dispatch."""

    def test_tool_in_map_fn(self):
        from tools import MAP_FN
        self.assertIn("find_symbol", MAP_FN, "find_symbol must be registered in MAP_FN")

    def test_tool_in_tools_list(self):
        from tools import tools
        names = [t["function"]["name"] for t in tools]
        self.assertIn("find_symbol", names, "find_symbol must appear in tools list")

    def test_definition_structure(self):
        from tools.find_symbol import definition
        self.assertEqual(definition["type"], "function")
        self.assertEqual(definition["function"]["name"], "find_symbol")
        params = definition["function"]["parameters"]["properties"]
        self.assertIn("name", params)
        self.assertIn("path", params)
        self.assertIn("kind", params)
        self.assertIn("mode", params)


class TestFindSymbolPathWhitespace(unittest.TestCase):
    """A path with leading/trailing whitespace must be treated the same as a
    trimmed path — the tool should strip it rather than silently returning []."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_path_with_spaces_finds_symbol(self):
        src = Path(self.tmp) / "mod.py"
        src.write_text("def greet(name):\n    pass\n", encoding="utf-8")
        results = find_symbol("greet", path=" " + str(src) + " ", mode="definition")
        self.assertTrue(len(results) >= 1, f"Expected >=1 result, got: {results}")
        self.assertNotIn("error", results[0])
        self.assertEqual(results[0]["scope"], "greet")

    def test_dir_path_with_spaces_finds_symbol(self):
        src = Path(self.tmp) / "mod.py"
        src.write_text("def hello(): pass\n", encoding="utf-8")
        results = find_symbol("hello", path="  " + self.tmp + "  ", mode="definition")
        self.assertTrue(len(results) >= 1, f"Expected >=1 result, got: {results}")
        self.assertNotIn("error", results[0])


class TestFindSymbolNoPyFiles(unittest.TestCase):
    """find_symbol on a directory with no .py files must return an error dict,
    not [], so callers can distinguish 'wrong path' from 'symbol absent'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_dir_returns_error_dict(self):
        """Completely empty directory -> error dict, not []."""
        results = find_symbol("my_func", path=self.tmp)
        self.assertEqual(len(results), 1, f"Expected 1 error dict, got: {results}")
        self.assertIn("error", results[0])
        self.assertIn("no Python files found", results[0]["error"])

    def test_dir_with_only_non_py_files_returns_error_dict(self):
        """Directory containing only .txt/.json files -> error dict."""
        (Path(self.tmp) / "notes.txt").write_text("def my_func(): pass\n")
        (Path(self.tmp) / "data.json").write_text('{"key": 1}')
        results = find_symbol("my_func", path=self.tmp)
        self.assertEqual(len(results), 1, f"Expected 1 error dict, got: {results}")
        self.assertIn("error", results[0])
        self.assertIn("no Python files found", results[0]["error"])

    def test_error_dict_mentions_path(self):
        """Error message must include the searched path to help diagnosis."""
        results = find_symbol("anything", path=self.tmp)
        self.assertIn(self.tmp, results[0]["error"],
                      f"Error should mention the path; got: {results[0]['error']!r}")

    def test_no_py_files_not_confused_with_not_found(self):
        """Error dict must be distinguishable from an empty 'not found' result."""
        results = find_symbol("anything", path=self.tmp)
        self.assertNotEqual(results, [],
                            "Should return error dict, not [], for dir with no .py files")

    def test_dir_with_py_files_symbol_absent_still_returns_empty(self):
        """Regression: a dir that HAS .py files but lacks the symbol still returns []."""
        (Path(self.tmp) / "module.py").write_text("def other(): pass\n")
        results = find_symbol("nonexistent_symbol_xyz", path=self.tmp)
        self.assertEqual(results, [],
                         "Symbol absent in py files must return [], not an error dict")

    def test_dir_with_py_files_symbol_present_still_works(self):
        """Regression: finding a symbol in a dir that has .py files still works."""
        (Path(self.tmp) / "module.py").write_text("def my_func(): pass\n")
        results = find_symbol("my_func", path=self.tmp)
        self.assertTrue(len(results) >= 1, f"Expected >=1 result, got: {results}")
        self.assertNotIn("error", results[0])


class TestFindSymbolNonPyFile(unittest.TestCase):
    """find_symbol on a single non-.py file must return an error dict,
    not [], so callers can distinguish 'wrong file type' from 'symbol absent'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_txt_file_returns_error_dict(self):
        """A .txt file target returns an error dict, not []."""
        target = Path(self.tmp) / "notes.txt"
        target.write_text("def my_func(): pass\n")
        results = find_symbol("my_func", path=str(target))
        self.assertEqual(len(results), 1, f"Expected 1 error dict, got: {results}")
        self.assertIn("error", results[0])
        self.assertIn("not a Python file", results[0]["error"])

    def test_json_file_returns_error_dict(self):
        """A .json file target returns an error dict, not []."""
        target = Path(self.tmp) / "config.json"
        target.write_text('{"key": "value"}\n')
        results = find_symbol("key", path=str(target))
        self.assertEqual(len(results), 1, f"Expected 1 error dict, got: {results}")
        self.assertIn("error", results[0])
        self.assertIn("not a Python file", results[0]["error"])

    def test_md_file_returns_error_dict(self):
        """A .md file target returns an error dict, not []."""
        target = Path(self.tmp) / "README.md"
        target.write_text("# My project\n\ndef foo(): pass\n")
        results = find_symbol("foo", path=str(target))
        self.assertEqual(len(results), 1, f"Expected 1 error dict, got: {results}")
        self.assertIn("error", results[0])

    def test_error_mentions_path(self):
        """Error message must include the file path for diagnosis."""
        target = Path(self.tmp) / "data.txt"
        target.write_text("content\n")
        results = find_symbol("anything", path=str(target))
        self.assertIn(str(target), results[0]["error"],
                      f"Error should mention the path; got: {results[0]['error']!r}")

    def test_error_not_confused_with_not_found(self):
        """Error dict must be distinguishable from an empty 'not found' result."""
        target = Path(self.tmp) / "notes.txt"
        target.write_text("content\n")
        results = find_symbol("anything", path=str(target))
        self.assertNotEqual(results, [],
                            "Should return error dict, not [], for a non-.py single file")

    def test_py_file_still_works(self):
        """Regression: a .py single file still works after the non-.py guard."""
        target = Path(self.tmp) / "module.py"
        target.write_text("def my_func(): pass\n")
        results = find_symbol("my_func", path=str(target))
        self.assertEqual(len(results), 1, f"Expected 1 result, got: {results}")
        self.assertNotIn("error", results[0])
        self.assertEqual(results[0]["scope"], "my_func")

    def test_py_file_symbol_absent_still_returns_empty(self):
        """Regression: a .py file where the symbol is absent still returns []."""
        target = Path(self.tmp) / "module.py"
        target.write_text("def other(): pass\n")
        results = find_symbol("nonexistent_xyz", path=str(target))
        self.assertEqual(results, [],
                         "Symbol absent in .py file must return [], not an error dict")

    def test_error_mentions_only_py_files_supported(self):
        """Error message must mention that only .py files are supported."""
        target = Path(self.tmp) / "script.sh"
        target.write_text("#!/bin/bash\necho hello\n")
        results = find_symbol("echo", path=str(target))
        self.assertIn(".py", results[0]["error"],
                      f"Error should mention .py support; got: {results[0]['error']!r}")


if __name__ == "__main__":
    unittest.main()
