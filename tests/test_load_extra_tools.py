"""Regression tests for the CICD 0001 extra_tools fix.

Pins two guarantees:
1. `agent.py` startup emits zero "Failed to load extra tool" warnings when
   run from the repo root. If someone repoints `_agent_tools_dir` back at
   the builtin `tools/` package, this test fails.
2. `tools.load_extra_tools` correctly registers a tool module dropped into
   a real separate directory. This documents the helper's contract so the
   feature can't silently rot.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
