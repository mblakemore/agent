"""Regression guards for explicit encoding='utf-8' in open() calls (cycle 0044).

Root cause (pre-fix): open() calls in agent.py, tools/web_fetch.py, and
tools/think.py used the platform default encoding instead of explicit
'utf-8'. On systems where the locale default is not UTF-8 (Windows,
some Linux locales), JSON checkpoint files and fetched web content could
be read or written with the wrong codec, producing UnicodeDecodeError
or silently corrupted content.

Fix (cycle 0042 pattern applied to the remaining files): all open()
calls now pass encoding='utf-8' (write/create) or
encoding='utf-8', errors='replace' (read).

These tests guard that:
1. tools/web_fetch.py write call uses encoding='utf-8'.
2. tools/think.py read call uses encoding='utf-8'.
3. agent.py config read uses encoding='utf-8'.
4. agent.py checkpoint write uses encoding='utf-8'.
5. agent.py checkpoint read uses encoding='utf-8'.
6. agent.py state read (_auto_increment_cycle) uses encoding='utf-8'.
7. agent.py state write (_auto_increment_cycle) uses encoding='utf-8'.
8. agent.py focus read uses encoding='utf-8'.
9. agent.py focus write uses encoding='utf-8'.
10. agent.py current-state read (cycle record) uses encoding='utf-8'.
"""

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

AGENT_PY = _REPO_ROOT / "agent.py"
WEB_FETCH_PY = _REPO_ROOT / "tools" / "web_fetch.py"
THINK_PY = _REPO_ROOT / "tools" / "think.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestWebFetchOpenEncoding(unittest.TestCase):

    def test_web_fetch_save_uses_utf8(self):
        """Static: web_fetch.py save open() must use encoding='utf-8'."""
        src = _src(WEB_FETCH_PY)
        # The save call: open(save_path, "w", encoding="utf-8")
        has_encoding = (
            'open(save_path, "w", encoding="utf-8")' in src
            or "open(save_path, 'w', encoding='utf-8')" in src
        )
        self.assertTrue(
            has_encoding,
            "tools/web_fetch.py must open save_path with encoding='utf-8'. "
            "Fetched web content (HTML/Markdown) may contain emoji and multi-byte "
            "unicode — without explicit encoding the file write uses the platform "
            "locale default which may not be UTF-8. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )


class TestThinkOpenEncoding(unittest.TestCase):

    def test_think_config_read_uses_utf8(self):
        """Static: think.py config open() must use encoding='utf-8'."""
        src = _src(THINK_PY)
        # open(config_path, encoding="utf-8", errors="replace") or similar
        has_encoding = (
            'encoding="utf-8"' in src or "encoding='utf-8'" in src
        )
        self.assertTrue(
            has_encoding,
            "tools/think.py must open config.json with encoding='utf-8'. "
            "Without explicit encoding the read uses the platform locale default. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )


class TestAgentOpenEncoding(unittest.TestCase):

    def _has_utf8(self, src: str, context: str) -> bool:
        """Return True if 'context' appears with encoding='utf-8' nearby."""
        idx = src.find(context)
        if idx == -1:
            return False
        # Check within a reasonable window after the context
        window = src[idx: idx + 120]
        return 'encoding="utf-8"' in window or "encoding='utf-8'" in window

    def test_agent_config_read_uses_utf8(self):
        """Static: agent.py config read open() must use encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(config_path'),
            "agent.py _load_config() must open config_path with encoding='utf-8'. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )

    def test_agent_checkpoint_write_uses_utf8(self):
        """Static: agent.py checkpoint write open() must use encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(_CHECKPOINT_PATH, "w"'),
            "agent.py _save_checkpoint() must open checkpoint file with encoding='utf-8'. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )

    def test_agent_checkpoint_read_uses_utf8(self):
        """Static: agent.py checkpoint read open() must use encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(_CHECKPOINT_PATH'),
            "agent.py _load_checkpoint() must open checkpoint file with encoding='utf-8'. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )

    def test_agent_state_read_uses_utf8(self):
        """Static: agent.py current-state read (_auto_increment_cycle) uses encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(state_path'),
            "agent.py _auto_increment_cycle() must open state_path with encoding='utf-8'. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )

    def test_agent_state_write_uses_utf8(self):
        """Static: agent.py current-state write (_auto_increment_cycle) uses encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(state_path, "w"'),
            "agent.py _auto_increment_cycle() must open state_path with encoding='utf-8' for writing. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )

    def test_agent_focus_read_uses_utf8(self):
        """Static: agent.py focus.json read uses encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(focus_path'),
            "agent.py must open focus_path with encoding='utf-8' for reading. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )

    def test_agent_focus_write_uses_utf8(self):
        """Static: agent.py focus.json write uses encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(focus_path, "w"'),
            "agent.py must open focus_path with encoding='utf-8' for writing. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )

    def test_agent_cycle_record_read_uses_utf8(self):
        """Static: agent.py cycle auto-record current-state read uses encoding='utf-8'."""
        src = _src(AGENT_PY)
        self.assertTrue(
            self._has_utf8(src, 'open(_state_path("current-state.json")'),
            "agent.py cycle auto-record must open current-state.json with encoding='utf-8'. "
            "See plan/CICD/improvements/0044-open-encoding-agent.md.",
        )


if __name__ == "__main__":
    unittest.main()
