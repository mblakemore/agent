"""Tests for tools/find_symbol.py — AST-aware Python symbol lookup."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.find_symbol import find_symbol

# Absolute path to the repo root — resolved via git so this works correctly
# regardless of whether the test runs from a worktree (where Path(__file__).parent.parent
# would resolve to the worktree path, which is listed in DEFAULT_EXCLUDES and would
# cause _collect_py_files to return []).
#
# In a linked worktree, --show-toplevel returns the *worktree* path, not the main
# repo path.  --git-common-dir (absolute form) points to the .git directory of the
# main repo, whose parent is always the canonical repo root.
_git_common_dir = subprocess.check_output(
    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
    cwd=Path(__file__).parent,
    text=True,
).strip()
_REPO_ROOT = str(Path(_git_common_dir).parent)
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
        self.assertEqual(m["line"], 1166)
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
        # Use a path inside _REPO_ROOT that is guaranteed not to exist,
        # so the confinement check passes and the existence check fires.
        nonexistent = os.path.join(_REPO_ROOT, "nonexistent_path_xyz_unique")
        results = find_symbol("anything", path=nonexistent)
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("does not exist", results[0]["error"])

    def test_nonexistent_path_error_mentions_path(self):
        nonexistent = os.path.join(_REPO_ROOT, "nonexistent_path_xyz_unique")
        results = find_symbol("anything", path=nonexistent)
        self.assertIn("nonexistent_path_xyz_unique", results[0]["error"])

    def test_nonexistent_path_not_confused_with_not_found(self):
        """Error dict for missing path is distinguishable from empty 'not found' result."""
        nonexistent = os.path.join(_REPO_ROOT, "nonexistent_path_xyz_unique")
        results = find_symbol("anything", path=nonexistent)
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


class TestFindSymbolModeCaseNormalization(unittest.TestCase):
    """find_symbol must normalize mode to lowercase so uppercase/mixed-case
    variants ('DEFINITION', 'Definition', 'CALLERS', etc.) work identically
    to their lowercase equivalents. (#718)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = Path(self.tmp) / "mod.py"
        p.write_text("def missing_xyz_zzz(): pass\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uppercase_definition_mode_works(self):
        """mode='DEFINITION' must succeed, not return an error dict."""
        results = find_symbol("nonexistent_symbol", path=self.tmp, mode="DEFINITION")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"mode='DEFINITION' returned unexpected error: {results}",
        )

    def test_titlecase_definition_mode_works(self):
        """mode='Definition' must succeed, not return an error dict."""
        results = find_symbol("nonexistent_symbol", path=self.tmp, mode="Definition")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"mode='Definition' returned unexpected error: {results}",
        )

    def test_uppercase_callers_mode_works(self):
        """mode='CALLERS' must succeed, not return an error dict."""
        results = find_symbol("nonexistent_symbol", path=self.tmp, mode="CALLERS")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"mode='CALLERS' returned unexpected error: {results}",
        )

    def test_uppercase_both_mode_works(self):
        """mode='BOTH' must succeed, not return an error dict."""
        results = find_symbol("nonexistent_symbol", path=self.tmp, mode="BOTH")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"mode='BOTH' returned unexpected error: {results}",
        )

    def test_uppercase_definition_finds_same_results_as_lowercase(self):
        """mode='DEFINITION' must produce the same results as mode='definition'."""
        # Write a symbol into self.tmp so both lookups search the same path.
        p = Path(self.tmp) / "target.py"
        p.write_text("def _classify_turn_complexity(x): pass\n", encoding="utf-8")
        lower = find_symbol("_classify_turn_complexity", path=self.tmp, mode="definition")
        upper = find_symbol("_classify_turn_complexity", path=self.tmp, mode="DEFINITION")
        self.assertEqual(lower, upper,
                         "mode='DEFINITION' and mode='definition' must return identical results")

    def test_truly_invalid_mode_still_errors(self):
        """A genuinely invalid mode (not a case variant) must still return an error."""
        results = find_symbol("foo", mode="bogus")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])


class TestFindSymbolKindCaseNormalization(unittest.TestCase):
    """find_symbol must normalize kind to lowercase so uppercase/mixed-case
    variants ('FUNCTION', 'Function', 'CLASS', etc.) work identically to
    their lowercase equivalents. (#718)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = Path(self.tmp) / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return str(p)

    def test_uppercase_function_kind_works(self):
        """kind='FUNCTION' must succeed, not return an error dict."""
        self._write("a.py", "def foo(): pass\n")
        results = find_symbol("foo", path=self.tmp, kind="FUNCTION", mode="definition")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"kind='FUNCTION' returned unexpected error: {results}",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function")

    def test_titlecase_function_kind_works(self):
        """kind='Function' must succeed, not return an error dict."""
        self._write("b.py", "def bar(): pass\n")
        results = find_symbol("bar", path=self.tmp, kind="Function", mode="definition")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"kind='Function' returned unexpected error: {results}",
        )

    def test_uppercase_class_kind_works(self):
        """kind='CLASS' must succeed, not return an error dict."""
        self._write("c.py", "class MyClass: pass\n")
        results = find_symbol("MyClass", path=self.tmp, kind="CLASS", mode="definition")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"kind='CLASS' returned unexpected error: {results}",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "class")

    def test_uppercase_method_kind_works(self):
        """kind='METHOD' must succeed, not return an error dict."""
        self._write("d.py", "class Foo:\n    def bar(self): pass\n")
        results = find_symbol("bar", path=self.tmp, kind="METHOD", mode="definition")
        self.assertIsInstance(results, list)
        self.assertFalse(
            any("error" in r for r in results),
            f"kind='METHOD' returned unexpected error: {results}",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "method")

    def test_uppercase_function_finds_same_as_lowercase(self):
        """kind='FUNCTION' must produce the same results as kind='function'."""
        self._write("e.py", "def baz(x, y): return x + y\n")
        lower = find_symbol("baz", path=self.tmp, kind="function", mode="definition")
        upper = find_symbol("baz", path=self.tmp, kind="FUNCTION", mode="definition")
        self.assertEqual(lower, upper,
                         "kind='FUNCTION' and kind='function' must return identical results")

    def test_truly_invalid_kind_still_errors(self):
        """A genuinely invalid kind must still return an error."""
        results = find_symbol("foo", kind="bogus")
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])

    def test_combined_uppercase_mode_and_kind(self):
        """Uppercase mode and kind together must both be normalized correctly."""
        self._write("f.py", "def greet(name): pass\n")
        results = find_symbol("greet", path=self.tmp, kind="FUNCTION", mode="DEFINITION")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function")
        self.assertNotIn("error", results[0])


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
        # scannable Python files remain — returns [] (same as "symbol not found").
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

    def test_nested_function_inside_method_is_function_not_method(self):
        """A closure defined inside a method must be classified as 'function', not 'method'.

        Regression test for #700: _find_definitions_with_scope was passing the
        class_stack unchanged when recursing into FunctionDef nodes, so any function
        nested inside a method inherited the class context and was misclassified as
        a method.
        """
        self._write("nested.py", """\
            class MyClass:
                def outer_method(self):
                    def inner_helper():
                        pass
                    return inner_helper
        """)
        # inner_helper is a closure, not a method
        results = find_symbol("inner_helper", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function",
                         "nested function inside a method should be kind='function'")

        # kind='function' filter must find it
        results_fn = find_symbol("inner_helper", path=self.tmp, kind="function", mode="definition")
        self.assertEqual(len(results_fn), 1)

        # kind='method' filter must NOT find it
        results_method = find_symbol("inner_helper", path=self.tmp, kind="method", mode="definition")
        self.assertEqual(results_method, [])

    def test_nested_function_inside_top_level_function_is_function(self):
        """A closure inside a plain function must also be classified as 'function'."""
        self._write("nested2.py", """\
            def outer():
                def inner():
                    pass
                return inner
        """)
        results = find_symbol("inner", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function")

        results_method = find_symbol("inner", path=self.tmp, kind="method", mode="definition")
        self.assertEqual(results_method, [])

    def test_async_def_context_includes_async_prefix(self):
        """context for an async def function must start with 'async def', not 'def'.

        Regression test for #702: _find_definitions_with_scope built the context
        string as f"def {name}(...)" for both ast.FunctionDef and
        ast.AsyncFunctionDef, silently dropping the 'async' keyword.
        """
        self._write("async_fn.py", """\
            async def my_async_fn(x, y):
                return x + y

            async def another_async():
                pass
        """)
        results = find_symbol("my_async_fn", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "function")
        self.assertEqual(results[0]["line"], 1)
        context = results[0]["context"]
        self.assertTrue(
            context.startswith("async def"),
            f"context should start with 'async def', got: {context!r}",
        )
        self.assertIn("my_async_fn", context)

    def test_async_method_context_includes_async_prefix(self):
        """async def inside a class must also include the 'async' prefix in context."""
        self._write("async_method.py", """\
            class MyClass:
                async def fetch(self, url):
                    pass
        """)
        results = find_symbol("fetch", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "method")
        context = results[0]["context"]
        self.assertTrue(
            context.startswith("async def"),
            f"async method context should start with 'async def', got: {context!r}",
        )

    def test_sync_def_context_unchanged(self):
        """Sync functions must still show 'def', not 'async def' (no regression)."""
        self._write("sync_fn.py", """\
            def sync_fn(a, b):
                return a - b
        """)
        results = find_symbol("sync_fn", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        context = results[0]["context"]
        self.assertTrue(
            context.startswith("def "),
            f"sync function context should start with 'def ', got: {context!r}",
        )
        self.assertFalse(
            context.startswith("async"),
            f"sync function context must not start with 'async', got: {context!r}",
        )

    def test_class_with_single_base_includes_base_in_context(self):
        """context for a class with one base class must include the base.

        Regression test for #708: _find_definitions_with_scope built the context
        string as f"class {name}:" for all classes, silently dropping base classes.
        """
        self._write("subclass.py", """\
            class Child(Parent):
                pass
        """)
        results = find_symbol("Child", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        context = results[0]["context"]
        self.assertEqual(
            context,
            "class Child(Parent):",
            f"class context should include base class, got: {context!r}",
        )

    def test_class_with_multiple_bases_includes_all_in_context(self):
        """context for a class with multiple bases must list all of them."""
        self._write("multi_base.py", """\
            class Foo(Bar, Baz):
                pass
        """)
        results = find_symbol("Foo", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        context = results[0]["context"]
        self.assertEqual(
            context,
            "class Foo(Bar, Baz):",
            f"class context should include all base classes, got: {context!r}",
        )

    def test_class_without_bases_context_unchanged(self):
        """context for a class with no bases must remain 'class Name:' (no regression)."""
        self._write("no_base.py", """\
            class Simple:
                pass
        """)
        results = find_symbol("Simple", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        context = results[0]["context"]
        self.assertEqual(
            context,
            "class Simple:",
            f"class with no bases should keep plain context, got: {context!r}",
        )

    def test_class_with_dotted_base_includes_full_dotted_name(self):
        """context for a class inheriting from a dotted name (e.g. module.Base) is correct."""
        self._write("dotted_base.py", """\
            class MyView(views.View):
                pass
        """)
        results = find_symbol("MyView", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        context = results[0]["context"]
        self.assertIn("views.View", context)
        self.assertTrue(context.startswith("class MyView("))


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

    def test_empty_dir_returns_empty_list(self):
        """Completely empty directory -> [] (no files to scan, no results)."""
        results = find_symbol("my_func", path=self.tmp)
        self.assertEqual(results, [])

    def test_dir_with_only_non_py_files_returns_empty_list(self):
        """Directory containing only .txt/.json files -> []."""
        (Path(self.tmp) / "notes.txt").write_text("def my_func(): pass\n")
        (Path(self.tmp) / "data.json").write_text('{"key": 1}')
        results = find_symbol("my_func", path=self.tmp)
        self.assertEqual(results, [])

    def test_no_py_files_same_as_symbol_not_found(self):
        """No Python files -> [] (same as symbol not found — caller checks path)."""
        results = find_symbol("anything", path=self.tmp)
        self.assertEqual(results, [])

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


class TestFindSymbolPathConfinement(unittest.TestCase):
    """find_symbol must refuse to scan paths outside the working directory (#856).

    The find_symbol_cwd fixture in conftest.py sets cwd to /droid/repos/agent for
    all tests in this class, so relative paths like '.' and 'tools/find_symbol.py'
    resolve correctly while absolute outside paths (/etc, /tmp) are rejected.
    """

    def test_absolute_etc_returns_confinement_error(self):
        """path='/etc' must return an error dict mentioning 'outside the working directory'."""
        result = find_symbol("foo", path="/etc")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertIn("error", result[0])
        self.assertIn("outside the working directory", result[0]["error"])

    def test_absolute_tmp_returns_confinement_error(self):
        """path='/tmp' must return a confinement error (cwd is /droid/repos/agent, not /tmp)."""
        result = find_symbol("foo", path="/tmp")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertIn("error", result[0])
        self.assertIn("outside the working directory", result[0]["error"])

    def test_relative_traversal_returns_confinement_error(self):
        """path='../other' must be rejected — it resolves outside cwd."""
        result = find_symbol("foo", path="../other")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertIn("error", result[0])
        self.assertIn("outside the working directory", result[0]["error"])

    def test_dot_path_still_works(self):
        """path='.' must still return results (happy path — cwd is inside itself)."""
        result = find_symbol("find_symbol", path=".")
        self.assertIsInstance(result, list)
        # Should find at least one definition (in tools/find_symbol.py)
        self.assertFalse(
            result and "error" in result[0] and "outside" in result[0]["error"],
            f"path='.' must not be rejected by confinement check; got: {result!r}",
        )

    def test_relative_path_inside_cwd_still_works(self):
        """path='tools/find_symbol.py' must work — it resolves inside cwd."""
        result = find_symbol("find_symbol", path="tools/find_symbol.py")
        self.assertIsInstance(result, list)
        self.assertFalse(
            result and "error" in result[0] and "outside" in result[0]["error"],
            f"path='tools/find_symbol.py' must not be rejected; got: {result!r}",
        )
        # Should find the definition
        self.assertTrue(
            any(r.get("kind") in ("function",) for r in result),
            f"Expected at least one function match; got: {result!r}",
        )

    def test_confinement_error_mentions_resolved_path(self):
        """Error message must include the resolved path for diagnosis."""
        result = find_symbol("foo", path="/etc")
        self.assertIn("/etc", result[0]["error"])

    def test_confinement_error_mentions_cwd(self):
        """Error message must include the working directory for diagnosis."""
        result = find_symbol("foo", path="/etc")
        error = result[0]["error"]
        # The error should name the working directory
        self.assertIn("/droid/repos/agent", error)

    def test_confinement_not_confused_with_not_found(self):
        """Confinement error dict must be distinguishable from 'symbol not found' []."""
        result = find_symbol("foo", path="/tmp")
        self.assertNotEqual(result, [], "Should return error dict, not [], for outside path")


class TestFindSymbolNonStringGuards(unittest.TestCase):
    """Non-string inputs must return an error dict, not raise AttributeError."""

    def test_name_int_returns_error(self):
        result = find_symbol(name=42, path="/droid/repos/agent/tools")
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])

    def test_name_none_returns_error(self):
        result = find_symbol(name=None, path="/droid/repos/agent/tools")
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])

    def test_path_int_returns_error(self):
        result = find_symbol(name="fn", path=42)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])

    def test_path_none_returns_error(self):
        result = find_symbol(name="fn", path=None)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])


class TestFindSymbolAbsolutePaths(unittest.TestCase):
    """Directory search must return absolute paths so callers can open files
    regardless of the process working directory. (#686)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()

    def tearDown(self):
        import shutil
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = Path(self.tmp) / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return str(p)

    def test_directory_search_returns_absolute_path(self):
        """Paths returned by a directory search must be absolute."""
        self._write("mod.py", "def my_func(): pass\n")
        results = find_symbol("my_func", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        returned_path = results[0]["path"]
        self.assertTrue(
            os.path.isabs(returned_path),
            f"Expected absolute path, got relative: {returned_path!r}",
        )

    def test_directory_search_path_exists_from_different_cwd(self):
        """The returned path must resolve to an existing file even from a
        different working directory (e.g. /tmp)."""
        self._write("mod.py", "def my_func(): pass\n")
        # Switch cwd away from the search directory
        os.chdir("/tmp")
        results = find_symbol("my_func", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        returned_path = results[0]["path"]
        self.assertTrue(
            os.path.exists(returned_path),
            f"Path {returned_path!r} does not exist from cwd=/tmp",
        )

    def test_absolute_path_contains_full_directory(self):
        """Absolute path must include the search directory, not just the filename."""
        self._write("mymod.py", "def target_fn(): pass\n")
        results = find_symbol("target_fn", path=self.tmp, mode="definition")
        self.assertEqual(len(results), 1)
        returned_path = results[0]["path"]
        # A relative 'mymod.py' would fail this; a full absolute path passes.
        self.assertIn(self.tmp, returned_path,
                      f"Path {returned_path!r} should contain search dir {self.tmp!r}")

    def test_single_file_search_still_absolute(self):
        """Single-file search also returns an absolute path (regression guard)."""
        src = self._write("lone.py", "def lone_fn(): pass\n")
        results = find_symbol("lone_fn", path=src, mode="definition")
        self.assertEqual(len(results), 1)
        returned_path = results[0]["path"]
        self.assertTrue(
            os.path.isabs(returned_path),
            f"Single-file path should be absolute, got: {returned_path!r}",
        )

    def test_callers_mode_also_returns_absolute_path(self):
        """mode='callers' must also produce absolute paths."""
        self._write("caller.py", "def callee(): pass\ncallee()\n")
        results = find_symbol("callee", path=self.tmp, mode="callers")
        self.assertTrue(len(results) >= 1, f"Expected >=1 caller result, got: {results}")
        returned_path = results[0]["path"]
        self.assertTrue(
            os.path.isabs(returned_path),
            f"Caller path should be absolute, got: {returned_path!r}",
        )


class TestFindSymbolNullByteInName(unittest.TestCase):
    """Null bytes in `name` must return an error dict, not silently return []. (#762)"""

    def setUp(self):
        # Create a throwaway .py file in a temp dir outside of any excluded path
        import tempfile
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_py = Path(self.tmp_dir) / "sample.py"
        self.tmp_py.write_text("def find_symbol(): pass\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_null_byte_in_name_returns_error_not_empty_list(self):
        """Before the fix, name='foo\\x00bar' would return [] — misleading the
        caller into thinking the symbol is absent rather than flagging bad input."""
        result = find_symbol(name='foo\x00bar', path=str(self.tmp_py), mode='definition')
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0, "expected non-empty result with error dict")
        self.assertIn("error", result[0])

    def test_null_byte_in_name_error_mentions_null_byte(self):
        """The error message should call out the null byte explicitly."""
        result = find_symbol(name='fn\x00', path=str(self.tmp_py), mode='definition')
        self.assertIn("null byte", result[0]["error"])

    def test_null_byte_only_in_name_returns_error(self):
        """Even a name that is just \\x00 must return an error, not [] or an exception."""
        result = find_symbol(name='\x00', path=str(self.tmp_py), mode='definition')
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])

    def test_valid_name_still_works_after_null_check(self):
        """The null-byte guard must not interfere with legitimate lookups."""
        result = find_symbol(name='find_symbol', path=str(self.tmp_py), mode='definition')
        self.assertIsInstance(result, list)
        # Should find the definition, not an error
        self.assertTrue(any("error" not in r for r in result))


class TestFindSymbolNullByteInPath(unittest.TestCase):
    """Null bytes in `path` must return a clear error dict, not a misleading
    'does not exist' or an unhandled ValueError. (#766)"""

    def setUp(self):
        import tempfile
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_py = Path(self.tmp_dir) / "sample.py"
        self.tmp_py.write_text("def my_func(): pass\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_null_byte_in_path_returns_error_not_does_not_exist(self):
        """path with null byte must return a null-byte error, not 'does not exist'. (#766)

        Before the fix, Path('/tmp/x\\x00.py').exists() raises ValueError which the
        code converts to 'path does not exist' — misleading because the path wasn't
        actually tested for existence, the null byte made it invalid.
        """
        result = find_symbol(name='my_func', path='/tmp/valid\x00path.py', mode='definition')
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0, "Expected non-empty result with error dict")
        self.assertIn("error", result[0])
        self.assertIn("null byte", result[0]["error"])
        self.assertNotIn("does not exist", result[0]["error"],
                         "Must not report misleading 'does not exist' for null-byte path")

    def test_null_byte_in_path_error_mentions_null_byte(self):
        """The error message must explicitly call out the null byte. (#766)"""
        result = find_symbol(name='fn', path='/droid/repos/agent/tools/file\x00.py',
                             mode='definition')
        self.assertIn("null byte", result[0]["error"])

    def test_null_byte_only_in_path_returns_error(self):
        """A path that is just \\x00 must return an error, not crash. (#766)"""
        result = find_symbol(name='fn', path='\x00', mode='definition')
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        self.assertIn("null byte", result[0]["error"])

    def test_valid_path_unaffected_by_null_byte_guard(self):
        """A valid path must still be searched correctly after the null-byte guard. (#766)"""
        result = find_symbol(name='my_func', path=str(self.tmp_py), mode='definition')
        self.assertIsInstance(result, list)
        self.assertTrue(any("error" not in r for r in result),
                        f"Expected at least one non-error result, got: {result!r}")


class TestFindSymbolEmptyPath(unittest.TestCase):
    """Empty or whitespace-only path must return an error dict, not silently scan
    the process working directory. (#774)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_string_path_returns_error(self):
        """path='' must return an error dict, not scan cwd (#774)."""
        result = find_symbol("fn", path="", mode="definition")
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0, "Expected non-empty result with error dict")
        self.assertIn("error", result[0])

    def test_empty_string_path_error_mentions_non_empty(self):
        """Error message for empty path must indicate a non-empty path is required (#774)."""
        result = find_symbol("fn", path="", mode="definition")
        self.assertIn("non-empty", result[0]["error"],
                      f"Expected 'non-empty' in error, got: {result[0]['error']!r}")

    def test_whitespace_only_path_returns_error(self):
        """path='   ' (spaces only) must return an error dict, not scan cwd (#774)."""
        result = find_symbol("fn", path="   ", mode="definition")
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0, "Expected non-empty result with error dict")
        self.assertIn("error", result[0])

    def test_whitespace_only_path_error_mentions_non_empty(self):
        """Error for whitespace-only path must indicate a non-empty path is required (#774)."""
        result = find_symbol("fn", path="   ", mode="definition")
        self.assertIn("non-empty", result[0]["error"],
                      f"Expected 'non-empty' in error, got: {result[0]['error']!r}")

    def test_empty_path_not_confused_with_not_found(self):
        """Error dict for empty path must be distinguishable from 'symbol not found' []."""
        result = find_symbol("fn", path="")
        self.assertNotEqual(result, [], "Should return error dict, not [], for empty path")

    def test_empty_path_does_not_scan_cwd(self):
        """Empty path must not silently scan and return results from cwd (#774)."""
        result = find_symbol("fn", path="")
        # Must be an error, not a non-empty list of real matches
        self.assertTrue(
            len(result) == 1 and "error" in result[0],
            f"empty path must return exactly one error dict, got: {result!r}",
        )

    def test_valid_path_unaffected_by_empty_path_guard(self):
        """A valid explicit path must still work after the empty-path guard is added (#774)."""
        p = Path(self.tmp) / "mod.py"
        p.write_text("def my_func(): pass\n", encoding="utf-8")
        result = find_symbol("my_func", path=str(p), mode="definition")
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0, f"Expected >=1 match, got: {result!r}")
        self.assertNotIn("error", result[0])
        self.assertEqual(result[0]["scope"], "my_func")


class TestFindSymbolLongPath(unittest.TestCase):
    """Very long path (> OS NAME_MAX) must return an error dict, not raise OSError (#808)."""

    def test_path_exceeding_os_limit_returns_error_dict(self):
        """A path longer than NAME_MAX bytes must return [{"error": "..."}], not raise OSError."""
        long_path = "/" + "a" * 1_000_000
        result = find_symbol(name="test", path=long_path)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0, "Expected non-empty error list for overlong path")
        self.assertIn("error", result[0],
                      f"Expected error key in first result, got: {result[0]!r}")

    def test_path_exceeding_os_limit_error_message_is_useful(self):
        """Error for a path that exceeds NAME_MAX must mention the failure reason."""
        long_path = "/" + "a" * 1_000_000
        result = find_symbol(name="test", path=long_path)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        # Must not just say 'does not exist' — the OS couldn't even check existence
        err = result[0]["error"]
        # Error should mention something about the path or the OS error
        self.assertTrue(
            len(err) > 0,
            "Error message must be non-empty for overlong path"
        )

    def test_path_just_over_255_chars_returns_error_dict(self):
        """A path component just over 255 chars (typical Linux NAME_MAX) returns error dict."""
        # Construct a path with a single component that is 300 chars (> NAME_MAX=255)
        long_component = "a" * 300
        long_path = f"/tmp/{long_component}"
        result = find_symbol(name="test", path=long_path)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIn("error", result[0])

    def test_normal_nonexistent_path_still_works(self):
        """After adding the OSError guard, normal non-existent paths still return 'does not exist' (#808)."""
        # Use a path under /tmp (cwd for this test class) so the confinement check
        # passes and the existence check fires.
        result = find_symbol(name="anything", path="/tmp/nonexistent_path_xyz_unique_808")
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIn("error", result[0])
        self.assertIn("does not exist", result[0]["error"])


# ── Issue #828: symlink handling in find_symbol ───────────────────────────────

class TestFindSymbolSymlinks(unittest.TestCase):
    """Verify find_symbol handles symlinks correctly (#828)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _make_py(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_symlink_to_py_file_is_scanned(self):
        """A symlink pointing to a .py file should be followed and scanned."""
        real = os.path.join(self.tmp, "real.py")
        self._make_py(real, "def hello():\n    pass\n")
        link = os.path.join(self.tmp, "link.py")
        os.symlink(real, link)
        result = find_symbol("hello", path=link)
        self.assertIsInstance(result, list)
        self.assertFalse(
            result and "error" in result[0],
            f"Expected matches, got error: {result}",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "function")

    def test_symlink_to_directory_is_scanned(self):
        """A symlink pointing to a directory should be followed and its .py files scanned."""
        real_dir = os.path.join(self.tmp, "real_dir")
        os.makedirs(real_dir)
        self._make_py(os.path.join(real_dir, "code.py"), "def world():\n    pass\n")
        link_dir = os.path.join(self.tmp, "link_dir")
        os.symlink(real_dir, link_dir)
        result = find_symbol("world", path=link_dir)
        self.assertIsInstance(result, list)
        self.assertFalse(
            result and "error" in result[0],
            f"Expected matches, got error: {result}",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "function")

    def test_symlinked_subdir_is_descended_into(self):
        """Symlinked subdirectories inside the search root must be followed (#828).

        Before the fix, os.walk(followlinks=False) silently skipped symlinked
        subdirs, so find_symbol returned [] instead of the real matches.
        """
        real_dir = os.path.join(self.tmp, "real")
        os.makedirs(real_dir)
        self._make_py(os.path.join(real_dir, "code.py"), "def hello():\n    pass\n")
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        os.symlink(real_dir, os.path.join(root, "symlink_subdir"))
        result = find_symbol("hello", path=root)
        self.assertIsInstance(result, list)
        self.assertGreater(
            len(result), 0,
            "find_symbol must find matches inside symlinked subdirectories (#828)",
        )
        self.assertFalse(
            "error" in result[0],
            f"Expected match dict, got error: {result}",
        )
        self.assertEqual(result[0]["kind"], "function")

    def test_dangling_symlink_returns_clean_error(self):
        """A dangling symlink (target does not exist) must return a clean error dict, not crash."""
        dangling = os.path.join(self.tmp, "dangling.py")
        os.symlink(os.path.join(self.tmp, "nonexistent.py"), dangling)
        result = find_symbol("hello", path=dangling)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIn("error", result[0])
        self.assertIn("does not exist", result[0]["error"])

    def test_dangling_symlink_does_not_raise(self):
        """A dangling symlink must never propagate an exception — always returns a list."""
        dangling = os.path.join(self.tmp, "dangling.py")
        os.symlink(os.path.join(self.tmp, "nonexistent.py"), dangling)
        try:
            result = find_symbol("anything", path=dangling)
        except Exception as exc:
            self.fail(f"find_symbol raised an exception on dangling symlink: {exc}")
        self.assertIsInstance(result, list)


class TestFindSymbolNameTypeValidation(unittest.TestCase):
    """Non-string name must return a type-specific error, not 'must be a non-empty string' (#917)."""

    def test_integer_name_returns_type_specific_error(self):
        """Integer name must mention 'string' and 'int', not 'non-empty string' (#917)."""
        result = find_symbol(42)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("string", error, f"Error must mention 'string': {error!r}")
        self.assertIn("'int'", error, f"Error must mention type 'int': {error!r}")

    def test_none_name_returns_type_specific_error(self):
        """None name must mention 'string' and 'NoneType', not 'non-empty string' (#917)."""
        result = find_symbol(None)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("string", error, f"Error must mention 'string': {error!r}")
        self.assertIn("'NoneType'", error, f"Error must mention type 'NoneType': {error!r}")

    def test_list_name_returns_type_specific_error(self):
        """List name must mention 'string' and 'list' (#917)."""
        result = find_symbol(["fn"])
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("string", error, f"Error must mention 'string': {error!r}")
        self.assertIn("'list'", error, f"Error must mention type 'list': {error!r}")

    def test_empty_string_name_still_rejected(self):
        """Empty string name must still be rejected after splitting the check (#917)."""
        result = find_symbol("")
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])

    def test_whitespace_name_still_rejected(self):
        """Whitespace-only name must still be rejected after splitting the check (#917)."""
        result = find_symbol("   ")
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])


class TestFindSymbolPathTypeNameQuoting(unittest.TestCase):
    """path type name in error must be single-quoted ('int', not int) (#917)."""

    def test_integer_path_type_name_is_quoted(self):
        """Integer path error must include quoted type name 'int', not bare int (#917)."""
        result = find_symbol("fn", path=42)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("'int'", error, f"Type name must be quoted: {error!r}")

    def test_none_path_type_name_is_quoted(self):
        """None path error must include quoted type name 'NoneType' (#917)."""
        result = find_symbol("fn", path=None)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("'NoneType'", error, f"Type name must be quoted: {error!r}")


class TestFindSymbolModeKindTypeValidation(unittest.TestCase):
    """Non-string mode/kind must return type-specific error, not 'Invalid mode 42' (#923)."""

    def test_integer_mode_returns_type_specific_error(self):
        """mode=42 must say 'mode must be a string, got \\'int\\'', not 'Invalid mode 42' (#923)."""
        result = find_symbol("fn", mode=42)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("string", error, f"Error must mention 'string': {error!r}")
        self.assertIn("'int'", error, f"Error must name the type: {error!r}")

    def test_none_mode_returns_type_specific_error(self):
        """mode=None must say 'must be a string, got \\'NoneType\\'', not 'Invalid mode None' (#923)."""
        result = find_symbol("fn", mode=None)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("string", error, f"Error must mention 'string': {error!r}")
        self.assertIn("'NoneType'", error, f"Error must name the type: {error!r}")

    def test_integer_kind_returns_type_specific_error(self):
        """kind=99 must say 'kind must be a string or None, got \\'int\\'' (#923)."""
        result = find_symbol("fn", kind=99)
        self.assertIsInstance(result, list)
        self.assertIn("error", result[0])
        error = result[0]["error"]
        self.assertIn("string", error, f"Error must mention 'string': {error!r}")
        self.assertIn("'int'", error, f"Error must name the type: {error!r}")

    def test_none_kind_is_valid(self):
        """kind=None must be accepted (means no filter) (#923)."""
        result = find_symbol("fn", kind=None)
        self.assertIsInstance(result, list)
        if result and "error" in result[0]:
            self.assertNotIn("kind", result[0]["error"].lower(), (
                f"kind=None must not produce a kind error: {result[0]['error']!r}"
            ))

    def test_string_mode_still_works(self):
        """Valid string mode must not be broken by the new guard (#923)."""
        result = find_symbol("fn", mode="both")
        self.assertIsInstance(result, list)
        if result:
            self.assertNotIn("mode must be a string", str(result))


if __name__ == "__main__":
    unittest.main()
