import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def _tool_sources():
    return sorted(p for p in TOOLS_DIR.glob("*.py") if not p.name.startswith("_pycache_"))


class TestToolsDocstringsAreAccurate(unittest.TestCase):

    def test_no_stale_shared_runtime_banner(self):
        offenders = []
        for path in _tool_sources():
            text = path.read_text()
            if "SHARED RUNTIME" in text:
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "Found stale 'SHARED RUNTIME' banner in tools/*.py — this phrase "
            "referenced a no-longer-existing tool-agent/ layout and has been "
            "removed. If you added a new file, drop the banner. Offenders: "
            f"{offenders}",
        )

    def test_no_stale_tool_agent_reference(self):
        offenders = []
        for path in _tool_sources():
            text = path.read_text()
            if "tool-agent/" in text:
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "Found stale 'tool-agent/' reference in tools/*.py — this repo is "
            "named 'agent', not 'tool-agent'. Update the reference (including "
            "the web_fetch User-Agent header) to 'agent'. Offenders: "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
