"""Regression guards for explicit encoding='utf-8' in Path.read_text() /
write_text() calls (cycle 0045).

Root cause (pre-fix): Path.read_text() / write_text() calls in
tools/task_tracker.py, agent.py, tools/exec_command.py, and
tools/search_files.py used the platform-default encoding instead of
explicit 'utf-8'. On non-UTF-8 locales (Windows ANSI, some Linux
configurations), JSON task state, @-file content injected into context,
and shell command output temp files could be read or written with the
wrong codec, silently corrupting multi-byte content or raising
UnicodeDecodeError.

Fix (cycle 0045): all six call sites now pass encoding='utf-8' (write /
new-file reads) or encoding='utf-8', errors='replace'/'ignore' (existing-
file reads), matching the pattern established by cycles 0042/0044 for
open() calls.

These tests pin the source text so the encoding argument cannot be
silently removed in a future edit.
"""

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

TASK_TRACKER_PY = _REPO_ROOT / "tools" / "task_tracker.py"
AGENT_PY = _REPO_ROOT / "agent.py"
EXEC_CMD_PY = _REPO_ROOT / "tools" / "exec_command.py"
SEARCH_FILES_PY = _REPO_ROOT / "tools" / "search_files.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTaskTrackerReadWriteEncoding(unittest.TestCase):

    def test_task_tracker_read_uses_utf8(self):
        """Static: task_tracker.py _load_tasks read_text must use encoding='utf-8'."""
        src = _src(TASK_TRACKER_PY)
        self.assertIn(
            "p.read_text(encoding='utf-8'",
            src,
            "task_tracker.py _load_tasks read_text must pass encoding='utf-8'",
        )

    def test_task_tracker_write_uses_utf8(self):
        """Static: task_tracker.py _save_tasks write_text must use encoding='utf-8'."""
        src = _src(TASK_TRACKER_PY)
        self.assertIn(
            "encoding='utf-8'",
            src.split("write_text")[1],
            "task_tracker.py _save_tasks write_text must pass encoding='utf-8'",
        )


class TestAgentReadTextEncoding(unittest.TestCase):

    def test_agent_file_ref_read_uses_utf8(self):
        """Static: agent.py @-ref read_text must use encoding='utf-8'."""
        src = _src(AGENT_PY)
        # The specific call: p.read_text(encoding='utf-8', errors='replace').splitlines(True)
        self.assertIn(
            "p.read_text(encoding='utf-8', errors='replace').splitlines(True)",
            src,
            "agent.py @-file read_text must pass encoding='utf-8', errors='replace'",
        )


class TestExecCommandReadWriteEncoding(unittest.TestCase):

    def test_exec_command_temp_read_uses_utf8(self):
        """Static: exec_command.py heredoc-cleanup read_text must use encoding='utf-8'."""
        src = _src(EXEC_CMD_PY)
        self.assertIn(
            "wt.read_text(encoding='utf-8', errors='replace')",
            src,
            "exec_command.py heredoc-cleanup read_text must pass encoding='utf-8'",
        )

    def test_exec_command_temp_write_uses_utf8(self):
        """Static: exec_command.py heredoc-cleanup write_text must use encoding='utf-8'."""
        src = _src(EXEC_CMD_PY)
        self.assertIn(
            "wt.write_text(cleaned, encoding='utf-8')",
            src,
            "exec_command.py heredoc-cleanup write_text must pass encoding='utf-8'",
        )


class TestSearchFilesReadEncoding(unittest.TestCase):

    def test_search_files_read_uses_utf8(self):
        """Static: search_files.py file read_text must use encoding='utf-8'."""
        src = _src(SEARCH_FILES_PY)
        self.assertIn(
            "file_path.read_text(encoding='utf-8', errors='ignore')",
            src,
            "search_files.py read_text must pass encoding='utf-8', errors='ignore'",
        )


if __name__ == "__main__":
    unittest.main()
