"""Regression guards for stream UTF-8 decoding fix (cycle 0037).

Root cause (pre-fix): `response.iter_lines(decode_unicode=True)` in agent.py
decoded SSE stream bytes using `requests`' internal logic, which defaults to
ISO-8859-1 (Latin-1) for `text/*` Content-Types with no explicit charset.
`llama.cpp` returns `Content-Type: text/event-stream` without a charset, so
every multi-byte UTF-8 sequence (emojis, CJK, non-Latin scripts) was decoded
as Latin-1 — producing garbage like `ð\x9f\x8c\x9f` instead of 🌟.

Fix: `iter_lines()` (raw bytes) + manual `raw_line.decode('utf-8')`.

These tests guard that:
1. `decode_unicode=True` is no longer in agent.py (static).
2. The manual UTF-8 decode pattern is present (static).
3. UTF-8 bytes decoded with UTF-8 preserve emojis (behavioral).
4. The same bytes decoded with Latin-1 produce the corruption (documents the bug).
"""

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

AGENT_PY = _REPO_ROOT / "agent.py"


class TestStreamIterUtf8(unittest.TestCase):

    def test_iter_lines_no_decode_unicode(self):
        """Static: `iter_lines(decode_unicode=True)` must not appear in agent.py."""
        src = AGENT_PY.read_text(encoding="utf-8")
        count = src.count("iter_lines(decode_unicode=True)")
        self.assertEqual(
            count,
            0,
            f"iter_lines(decode_unicode=True) still appears {count} time(s) in agent.py. "
            "This causes UTF-8 emojis to be corrupted when llama.cpp returns "
            "Content-Type: text/event-stream without a charset — requests defaults "
            "to ISO-8859-1 for text/* types, mangling multi-byte sequences. "
            "Fix: use iter_lines() and decode manually as UTF-8. "
            "See plan/CICD/improvements/0037-stream-iter-utf8.md.",
        )

    def test_iter_lines_has_utf8_decode(self):
        """Static: manual UTF-8 decode pattern must be present in agent.py."""
        src = AGENT_PY.read_text(encoding="utf-8")
        # Accept either .decode('utf-8') or .decode("utf-8")
        has_decode = '.decode("utf-8")' in src or ".decode('utf-8')" in src
        self.assertTrue(
            has_decode,
            "agent.py must manually decode SSE stream lines as UTF-8 "
            "(.decode('utf-8') or .decode(\"utf-8\") expected). "
            "See plan/CICD/improvements/0037-stream-iter-utf8.md.",
        )

    def test_utf8_bytes_round_trip(self):
        """Behavioral: multi-byte UTF-8 emoji bytes decoded as UTF-8 are preserved."""
        # These are the raw bytes the LLM server sends for 🌟 ✨ 🎉
        original = "🌟 ✨ 🎉"
        encoded = original.encode("utf-8")
        decoded = encoded.decode("utf-8")
        self.assertEqual(
            decoded,
            original,
            "UTF-8 bytes decoded as UTF-8 must round-trip unchanged.",
        )
        # Verify emoji characters survive
        self.assertIn("🌟", decoded)
        self.assertIn("✨", decoded)
        self.assertIn("🎉", decoded)

    def test_latin1_decode_corrupts_emoji(self):
        """Documents the pre-fix bug: same bytes decoded as Latin-1 are corrupted."""
        original = "🌟"
        encoded = original.encode("utf-8")  # b'\xf0\x9f\x8c\x9f'
        corrupted = encoded.decode("latin-1")  # ð\x9f\x8c\x9f
        # The corrupted form must NOT contain the original emoji
        self.assertNotIn(
            "🌟",
            corrupted,
            "Latin-1 decode should corrupt the emoji — if this fails, "
            "our test assumption about the bug is wrong.",
        )
        # The corrupted form must differ from the original
        self.assertNotEqual(
            corrupted,
            original,
            "Latin-1 decode of UTF-8 emoji bytes must produce different output "
            "(documents the pre-fix corruption).",
        )
        # Confirm the specific corruption pattern matches what we saw in probe logs
        # 🌟 (U+1F31F) UTF-8 = F0 9F 8C 9F → Latin-1 = ð \x9f \x8c \x9f
        self.assertEqual(corrupted[0], "ð", f"Expected 'ð' but got {repr(corrupted[0])}")


if __name__ == "__main__":
    unittest.main()
