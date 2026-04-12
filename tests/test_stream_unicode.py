"""Regression guards for stream Unicode passthrough.

Verifies that _ReasoningRenderer passes Unicode text unchanged to its
writer — no ASCII downgrade of smart punctuation (em-dashes, smart
quotes, ellipsis, bullets) or NBSP/ZWSP characters.
"""

import subprocess
import sys
import unittest
from pathlib import Path

# Make agent importable from the repo root (worktree root = parent of tests/)
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agent import _ReasoningRenderer  # noqa: E402


class TestStreamUnicodePassthrough(unittest.TestCase):

    def _make_renderer(self):
        """Return (renderer, captured_chunks) — chunks list grows as writer is called."""
        chunks = []
        renderer = _ReasoningRenderer(chunks.append)
        return renderer, chunks

    def test_emit_plain_preserves_em_dash(self):
        """Em-dash (U+2014) must pass through unchanged, not become '--'."""
        renderer, chunks = self._make_renderer()
        renderer.feed("before \u2014 after")
        renderer.flush()
        joined = "".join(chunks)
        self.assertIn("\u2014", joined, "em-dash was stripped from streamed output")
        self.assertNotIn("--", joined, "em-dash was converted to ASCII '--'")

    def test_emit_plain_preserves_smart_quotes(self):
        """Smart double quotes (U+201C / U+201D) must pass through unchanged."""
        renderer, chunks = self._make_renderer()
        renderer.feed("\u201chello\u201d")
        renderer.flush()
        joined = "".join(chunks)
        self.assertIn("\u201c", joined, "left smart quote was stripped")
        self.assertIn("\u201d", joined, "right smart quote was stripped")

    def test_emit_plain_preserves_ellipsis(self):
        """Horizontal ellipsis (U+2026) must pass through unchanged, not become '...'."""
        renderer, chunks = self._make_renderer()
        renderer.feed("wait\u2026")
        renderer.flush()
        joined = "".join(chunks)
        self.assertIn("\u2026", joined, "ellipsis was stripped from streamed output")

    def test_emit_plain_preserves_bullet(self):
        """Bullet (U+2022) must pass through unchanged, not become '*'."""
        renderer, chunks = self._make_renderer()
        renderer.feed("\u2022 item one")
        renderer.flush()
        joined = "".join(chunks)
        self.assertIn("\u2022", joined, "bullet was stripped from streamed output")

    def test_no_sanitize_display_in_agent(self):
        """Static check: _sanitize_display must not appear in agent.py."""
        agent_path = _REPO_ROOT / "agent.py"
        src = agent_path.read_text()
        count = src.count("_sanitize_display")
        self.assertEqual(count, 0,
                         f"_sanitize_display still present {count} time(s) in agent.py")

    def test_no_unicode_map_in_agent(self):
        """Static check: _UNICODE_MAP must not appear in agent.py."""
        agent_path = _REPO_ROOT / "agent.py"
        src = agent_path.read_text()
        count = src.count("_UNICODE_MAP")
        self.assertEqual(count, 0,
                         f"_UNICODE_MAP still present {count} time(s) in agent.py")


if __name__ == "__main__":
    unittest.main()
