import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parent.parent

_STALE_TUI_RE = re.compile(r"--tui\b")
_STALE_TUI_FILES = ("README.md", "tui.py", "agent.py")


class TestDocSync(unittest.TestCase):

    def test_no_stale_tui_flag(self):
        offenders = []
        for rel in _STALE_TUI_FILES:
            path = REPO_ROOT / rel
            text = path.read_text()
            for lineno, line in enumerate(text.splitlines(), 1):
                if _STALE_TUI_RE.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "Found references to a `--tui` CLI flag that does not exist. "
            "The flag was removed in commit 008d84a (TUI became the default "
            "interactive front-end); the current opt-out flag is `--no-tui`. "
            "Rewrite the offending line(s) to reference `--no-tui` or drop "
            "the flag mention entirely. Offenders:\n  "
            + "\n  ".join(offenders),
        )

    def test_tools_command_docs_do_not_claim_last_20(self):
        readme = (REPO_ROOT / "README.md").read_text()
        self.assertNotIn(
            "last 20 tool calls", readme,
            "README.md still describes `/tools` as showing 'the last 20 tool "
            "calls'. Cycle 0002 shipped paging: the default buffer is 50, the "
            "default `/tools` invocation shows every buffered call, and "
            "`/tools [N|all]` is supported. Update the commands table row to "
            "match the current shape.",
        )


if __name__ == "__main__":
    unittest.main()
