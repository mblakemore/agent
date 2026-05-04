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

    def test_nonexistent_path_returns_empty_list(self):
        results = find_symbol("anything", path="/nonexistent/path/xyz")
        self.assertEqual(results, [])


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
        self._write("bad.py", "def (broken syntax")
        results = find_symbol("anything", path=self.tmp)
        self.assertEqual(results, [])

    def test_excludes_pycache(self):
        pycache_dir = Path(self.tmp) / "__pycache__"
        pycache_dir.mkdir()
        (pycache_dir / "hidden.py").write_text("def my_func(): pass")
        results = find_symbol("my_func", path=self.tmp, mode="definition")
        # Should not find anything in __pycache__
        self.assertEqual(results, [])

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


if __name__ == "__main__":
    unittest.main()
