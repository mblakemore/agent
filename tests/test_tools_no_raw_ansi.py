import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

_RAW_ESCAPE = "\\033["


def _tool_sources():
    results = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        results.append(path)
    return results


class TestToolsNoRawAnsi(unittest.TestCase):

    def test_no_raw_ansi_escapes_in_tools(self):
        offenders = []
        for path in _tool_sources():
            text = path.read_text()
            if _RAW_ESCAPE in text:
                line_no = next(
                    (i + 1 for i, line in enumerate(text.splitlines())
                     if _RAW_ESCAPE in line),
                    None,
                )
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}")
        self.assertEqual(
            offenders, [],
            "Found raw ANSI escape literal '\\033[' in tools/*.py — color "
            "output inside tools must route through theme.c / theme.dim so "
            "it honors NO_COLOR and non-TTY stdout. Offenders: "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
