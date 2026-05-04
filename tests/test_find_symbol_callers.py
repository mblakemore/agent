"""Tests for tools/find_symbol.py — specifically testing mode='callers' and mode='both'."""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.find_symbol import find_symbol

class TestFindSymbolCallers(unittest.TestCase):
    """
    Test suite focusing on the 'callers' and 'both' modes of find_symbol.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = Path(self.tmp) / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return str(p)

    def test_finding_callers_of_known_function(self):
        """(1) Test finding callers of a known function."""
        self._write("module_a.py", """
            def target_function(x):
                return x * 2

            def caller_1():
                target_function(10)

            def caller_2():
                target_function(20)
        """)
        
        results = find_symbol("target_function", path=self.tmp, mode="callers")
        
        # Should find two calls
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["kind"], "call")
            self.assertIn("target_function", r["context"])

    def test_mode_both_returns_definitions_and_callers(self):
        """(2) Test mode='both' returns definitions AND callers."""
        self._write("module_b.py", """
            def shared_func():
                print("Hello")

            shared_func()
        """)
        
        results = find_symbol("shared_func", path=self.tmp, mode="both")
        
        # Should find 1 definition and 1 call
        self.assertEqual(len(results), 2)
        kinds = {r["kind"] for r in results}
        self.assertIn("function", kinds)
        self.assertIn("call", kinds)

    def test_callers_of_method_via_obj_dot_syntax(self):
        """(3) Test callers of a method accessed via obj.method() syntax."""
        self._write("module_c.py", """
            class MyClass:
                def my_method(self):
                    pass

            def run_test():
                obj = MyClass()
                obj.my_method()
        """)
        
        results = find_symbol("my_method", path=self.tmp, mode="callers")
        
        # Should find the call obj.my_method()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "call")
        self.assertIn("obj.my_method()", results[0]["context"])

    def test_complex_mixed_calls(self):
        """Additional test with multiple files and mixed styles."""
        self._write("utils.py", """
            def util_fn():
                pass
        """)
        self._write("main.py", """
            from utils import util_fn

            def main():
                util_fn()
        """)

        results = find_symbol("util_fn", path=self.tmp, mode="callers")
        self.assertEqual(len(results), 1)
        self.assertIn("main.py", results[0]["path"])

    def test_callers_cross_file_finds_calls_in_multiple_files(self):
        """callers mode must find call sites across multiple source files (#792 probe)."""
        self._write("lib.py", """
            def shared_helper():
                pass
        """)
        self._write("consumer_a.py", """
            def task_a():
                shared_helper()
        """)
        self._write("consumer_b.py", """
            def task_b():
                shared_helper()
                shared_helper()
        """)

        results = find_symbol("shared_helper", path=self.tmp, mode="callers")
        # Must find 3 calls across 2 files
        self.assertEqual(len(results), 3,
                         f"Expected 3 call sites, got {len(results)}: {results}")
        paths = {r["path"] for r in results}
        # Calls must span both consumer files
        consumer_paths = {p for p in paths if "consumer" in p}
        self.assertEqual(len(consumer_paths), 2,
                         f"Calls must span 2 consumer files, found: {consumer_paths}")

    def test_callers_nonexistent_function_returns_empty_list(self):
        """callers mode for a symbol that does not exist must return [] not an error (#792 probe)."""
        self._write("any.py", """
            def some_func():
                pass
        """)
        results = find_symbol("no_such_func_xyz999", path=self.tmp, mode="callers")
        self.assertEqual(results, [],
                         f"Expected [], got: {results!r}")

    def test_callers_returns_call_kind_for_all_results(self):
        """All results from callers mode must have kind='call'. (#792 probe)"""
        self._write("module.py", """
            def target():
                pass

            def a():
                target()

            def b():
                obj = None
                obj.target()
        """)
        results = find_symbol("target", path=self.tmp, mode="callers")
        self.assertGreater(len(results), 0, "Expected at least one caller")
        for r in results:
            self.assertEqual(r["kind"], "call",
                             f"All callers results must have kind='call', got: {r!r}")

if __name__ == "__main__":
    unittest.main()
