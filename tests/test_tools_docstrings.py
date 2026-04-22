import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = Path(__file__).resolve()

_SKIP_DIR_PARTS = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules", "temp"}


def _python_sources():
    results = []
    for path in REPO_ROOT.rglob("*.py"):
        if path.resolve() == TEST_FILE:
            continue
        if _SKIP_DIR_PARTS.intersection(path.parts):
            continue
        results.append(path)
    return sorted(results)


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


class TestPythonDocstringsAreAccurate(unittest.TestCase):

    def test_no_stale_shared_runtime_banner(self):
        offenders = []
        for path in _python_sources():
            text = path.read_text()
            if "SHARED RUNTIME" in text:
                offenders.append(_rel(path))
        self.assertEqual(
            offenders, [],
            "Found stale 'SHARED RUNTIME' banner in .py files — this phrase "
            "referenced a no-longer-existing tool-agent/ layout and has been "
            "removed. If you added a new file, drop the banner. Offenders: "
            f"{offenders}",
        )

    def test_no_stale_tool_agent_reference(self):
        offenders = []
        for path in _python_sources():
            text = path.read_text()
            if "tool-agent/" in text:
                offenders.append(_rel(path))
        self.assertEqual(
            offenders, [],
            "Found stale 'tool-agent/' reference in .py files — this repo is "
            "named 'agent', not 'tool-agent'. Update the reference (including "
            "the web_fetch User-Agent header) to 'agent'. Offenders: "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
