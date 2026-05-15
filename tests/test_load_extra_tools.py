"""Regression tests for the CICD 0001 extra_tools fix.

Pins two guarantees:
1. `agent.py` startup emits zero "Failed to load extra tool" warnings when
   run from the repo root. If someone repoints `_agent_tools_dir` back at
   the builtin `tools/` package, this test fails.
2. `tools.load_extra_tools` correctly registers a tool module dropped into
   a real separate directory. This documents the helper's contract so the
   feature can't silently rot.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent


class AgentStartupEmitsNoExtraToolWarning(unittest.TestCase):

    def test_agent_help_emits_no_extra_tool_warning(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "agent.py"), "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=30,
        )
        combined = result.stdout + result.stderr
        self.assertNotIn(
            "Failed to load extra tool",
            combined,
            msg=(
                "agent.py startup emitted an extra-tool loader warning. "
                "See plan/CICD/improvements/0001-extra-tools-dead-call.md — "
                "the loader must not be pointed at the builtin tools/ "
                "package. Captured output:\n" + combined
            ),
        )
        self.assertEqual(result.returncode, 0,
                         msg=f"agent.py --help exited {result.returncode}; output:\n{combined}")


class LoadExtraToolsRegistersFromSeparateDir(unittest.TestCase):

    def test_load_extra_tools_registers_tool_from_temp_dir(self):
        import tools as tools_pkg

        tool_name = "cicd_probe_echo"
        self.assertNotIn(
            tool_name, tools_pkg.MAP_FN,
            msg="test fixture name collides with an already-registered tool")

        module_src = textwrap.dedent(
            """
            def fn(text: str = "") -> str:
                return f"echo:{text}"

            definition = {
                "type": "function",
                "function": {
                    "name": "cicd_probe_echo",
                    "description": "Probe tool for CICD 0001 test.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "default": ""}
                        },
                        "required": [],
                    },
                },
            }
            """
        ).strip() + "\n"

        try:
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "cicd_probe_echo.py").write_text(module_src)
                tools_pkg.load_extra_tools(tmp)
                self.assertIn(tool_name, tools_pkg.MAP_FN)
                self.assertEqual(tools_pkg.MAP_FN[tool_name](text="hi"), "echo:hi")
                registered = [t for t in tools_pkg.tools
                              if t.get("function", {}).get("name") == tool_name]
                self.assertEqual(len(registered), 1,
                                 msg="extra tool was not appended exactly once")
        finally:
            tools_pkg.MAP_FN.pop(tool_name, None)
            tools_pkg.tools[:] = [t for t in tools_pkg.tools
                                  if t.get("function", {}).get("name") != tool_name]


class ValidateDefinitionEdgeCases(unittest.TestCase):
    """Cover _validate_definition() branches (lines 47-58)."""

    def _vd(self, defn, filename="test.py"):
        import tools as tools_pkg
        return tools_pkg._validate_definition(defn, filename)

    def test_non_dict_returns_none(self):
        # Lines 47-48: definition is not a dict → log warning + return None
        self.assertIsNone(self._vd([1, 2]))
        self.assertIsNone(self._vd("a string"))

    def test_auto_adds_type_key(self):
        # Line 51: definition has "function" but no "type" → "type" injected
        defn = {"function": {"name": "my_tool"}}
        result = self._vd(defn)
        self.assertEqual(result, "my_tool")
        self.assertEqual(defn.get("type"), "function")

    def test_missing_function_key_returns_none(self):
        # Lines 53-54: definition has "type" but no "function" → log warning + return None
        self.assertIsNone(self._vd({"type": "function"}))

    def test_missing_tool_name_returns_none(self):
        # Lines 57-58: "function" key exists but has no "name" → log warning + return None
        self.assertIsNone(self._vd({"type": "function", "function": {}}))
        self.assertIsNone(self._vd({"type": "function", "function": {"name": ""}}))


class LoadExtraToolsEdgeCases(unittest.TestCase):
    """Cover load_extra_tools() edge-case branches (lines 74, 88, 94, 98, 103-104, 109-112, 118-119, 122)."""

    def setUp(self):
        import tools as tools_pkg
        self._tools_pkg = tools_pkg
        # Snapshot state so tearDown can restore any leakage
        self._map_keys_before = set(tools_pkg.MAP_FN.keys())
        self._tools_len_before = len(tools_pkg.tools)

    def tearDown(self):
        # Remove any tools inadvertently added by a test
        pkg = self._tools_pkg
        extra = set(pkg.MAP_FN.keys()) - self._map_keys_before
        for name in extra:
            pkg.MAP_FN.pop(name, None)
            pkg.tools[:] = [t for t in pkg.tools
                            if t.get("function", {}).get("name") != name]

    def test_nonexistent_directory_is_noop(self):
        # Line 74: directory does not exist → early return, no error
        before = set(self._tools_pkg.MAP_FN.keys())
        self._tools_pkg.load_extra_tools("/nonexistent/__cicd_probe_path__")
        self.assertEqual(before, set(self._tools_pkg.MAP_FN.keys()))

    def test_module_missing_fn_is_skipped(self):
        # Line 94: module without fn attribute → continue
        src = "definition = {'type': 'function', 'function': {'name': 'cicd_no_fn_tool'}}\n"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "no_fn.py").write_text(src)
            before = set(self._tools_pkg.MAP_FN.keys())
            self._tools_pkg.load_extra_tools(tmp)
            self.assertEqual(before, set(self._tools_pkg.MAP_FN.keys()))

    def test_bad_definition_is_skipped(self):
        # Line 98: _validate_definition returns None → continue
        src = textwrap.dedent("""
            def fn(): return 1
            definition = {"type": "function"}  # missing "function" key
        """)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad_defn.py").write_text(src)
            before = set(self._tools_pkg.MAP_FN.keys())
            self._tools_pkg.load_extra_tools(tmp)
            self.assertEqual(before, set(self._tools_pkg.MAP_FN.keys()))

    def test_cap_enforcement_and_skipped_cap_log(self):
        # Lines 103-104, 122: >_MAX_EXTRA_TOOLS new tools → oldest skipped + log
        pkg = self._tools_pkg
        original_cap = pkg._MAX_EXTRA_TOOLS
        pkg._MAX_EXTRA_TOOLS = 1
        names = ["cicd_cap_a", "cicd_cap_b", "cicd_cap_c"]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                # Create files with distinct mtimes (newest first loads first)
                for name in names:
                    src = textwrap.dedent(f"""
                        def fn(): return '{name}'
                        definition = {{'type': 'function', 'function': {{'name': '{name}'}}}}
                    """)
                    (Path(tmp) / f"{name}.py").write_text(src)
                    time.sleep(0.02)
                pkg.load_extra_tools(tmp)
                registered = [n for n in names if n in pkg.MAP_FN]
                self.assertEqual(len(registered), 1, "cap=1 should load exactly 1 new tool")
        finally:
            pkg._MAX_EXTRA_TOOLS = original_cap
            for name in names:
                pkg.MAP_FN.pop(name, None)
                pkg.tools[:] = [t for t in pkg.tools
                                if t.get("function", {}).get("name") != name]

    def test_override_existing_tool_not_counted_against_cap(self):
        # Lines 109-112: override existing tool → replaces in tools list, cap not consumed
        pkg = self._tools_pkg
        # Pick any existing tool name
        existing_name = next(iter(pkg.MAP_FN))
        original_fn = pkg.MAP_FN[existing_name]
        original_defn = next(
            (t for t in pkg.tools if t.get("function", {}).get("name") == existing_name), None
        )
        original_defn_idx = pkg.tools.index(original_defn) if original_defn else None

        src = textwrap.dedent(f"""
            def fn(): return '__override__'
            definition = {{'type': 'function', 'function': {{'name': '{existing_name}'}}}}
        """)
        original_cap = pkg._MAX_EXTRA_TOOLS
        pkg._MAX_EXTRA_TOOLS = 0  # no new tools allowed; override still fires
        try:
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / f"{existing_name}.py").write_text(src)
                pkg.load_extra_tools(tmp)
                self.assertEqual(pkg.MAP_FN[existing_name](), "__override__",
                                 "override should replace the registered fn")
        finally:
            pkg._MAX_EXTRA_TOOLS = original_cap
            pkg.MAP_FN[existing_name] = original_fn
            if original_defn is not None and original_defn_idx is not None:
                pkg.tools[original_defn_idx] = original_defn

    def test_exception_in_exec_module_is_logged_not_raised(self):
        # Lines 118-119: module raises at import time → warning, loop continues
        src = "raise RuntimeError('intentional cicd test error')\n"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "raises_on_load.py").write_text(src)
            before = set(self._tools_pkg.MAP_FN.keys())
            self._tools_pkg.load_extra_tools(tmp)  # must not propagate
            self.assertEqual(before, set(self._tools_pkg.MAP_FN.keys()))

    def test_spec_none_file_is_skipped(self):
        # Line 88: spec_from_file_location returns None → continue
        src = textwrap.dedent("""
            def fn(): return 'spec_none'
            definition = {'type': 'function', 'function': {'name': 'cicd_spec_none_tool'}}
        """)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "spec_none.py").write_text(src)
            before = set(self._tools_pkg.MAP_FN.keys())
            with patch("importlib.util.spec_from_file_location", return_value=None):
                self._tools_pkg.load_extra_tools(tmp)
            self.assertEqual(before, set(self._tools_pkg.MAP_FN.keys()),
                             "spec=None file should be skipped without error")


if __name__ == "__main__":
    unittest.main()
