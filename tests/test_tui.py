"""Unit tests for tui.py — completer, toolbar, prompt-active flag, stub fallback."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import tui


class TestLastAtRef(unittest.TestCase):
    def test_empty_string(self):
        self.assertIsNone(tui._last_at_ref(""))

    def test_no_at(self):
        self.assertIsNone(tui._last_at_ref("hello world"))

    def test_bare_at_at_start(self):
        self.assertEqual(tui._last_at_ref("@"), 0)

    def test_email_style_at_rejected(self):
        """`foo@bar` — @ is not preceded by whitespace."""
        self.assertIsNone(tui._last_at_ref("foo@bar"))

    def test_at_after_space(self):
        self.assertEqual(tui._last_at_ref("look @a/b"), 5)

    def test_at_followed_by_space_rejected(self):
        """A whitespace after the @ means it's no longer a ref."""
        self.assertIsNone(tui._last_at_ref("@a "))

    def test_second_at_is_the_one(self):
        self.assertEqual(tui._last_at_ref("@a @b"), 3)

    def test_path_with_slash(self):
        self.assertEqual(tui._last_at_ref("@src/foo.py"), 0)


@unittest.skipUnless(tui._AVAILABLE, "prompt_toolkit not installed")
class TestLlmboxCompleter(unittest.TestCase):
    def _completions(self, text):
        from prompt_toolkit.document import Document
        comp = tui.LlmboxCompleter()
        doc = Document(text=text, cursor_position=len(text))
        return list(comp.get_completions(doc, complete_event=None))

    def test_slash_he_yields_help(self):
        results = self._completions("/he")
        names = [c.text for c in results]
        self.assertIn("/help", names)

    def test_slash_empty_yields_all(self):
        results = self._completions("/")
        names = [c.text for c in results]
        # Every canonical slash command should be suggested
        for cmd, _desc in tui._SLASH_COMMANDS:
            self.assertIn(cmd, names)

    def test_slash_nomatch_empty(self):
        results = self._completions("/zzz")
        self.assertEqual(results, [])

    def test_at_ref_delegates_to_path_completer(self):
        # PathCompleter produces Completion objects for "/etc/" entries.
        results = self._completions("@/etc/")
        # We can't assert a specific path, but the completer should have run
        # without raising and produced Completion objects (possibly empty).
        self.assertTrue(all(hasattr(c, "text") for c in results))

    def test_slash_uppercase_yields_help(self):
        results = self._completions("/HE")
        names = [c.text for c in results]
        self.assertIn("/help", names)

    def test_slash_mixed_case_yields_help(self):
        results = self._completions("/HeLp")
        names = [c.text for c in results]
        self.assertIn("/help", names)

    def test_slash_single_char_matches(self):
        results = self._completions("/h")
        names = [c.text for c in results]
        self.assertIn("/help", names)


@unittest.skipUnless(tui._AVAILABLE, "prompt_toolkit not installed")
class TestTuiSessionToolbar(unittest.TestCase):
    def _make_session(self, history=None, summary_text=None):
        from callbacks import TerminalCallbacks
        hist = history if history is not None else []
        summary = {"text": summary_text} if summary_text is not None else {}
        return tui.TuiSession(
            history=hist,
            summary_state=summary,
            config={"llm": {"model": "test-model"}},
            ctx_size=4096,
            cb=TerminalCallbacks(),
            estimate_tokens=lambda m: 10,
        )

    def test_toolbar_empty_history(self):
        from prompt_toolkit.formatted_text import HTML
        sess = self._make_session()
        out = sess._toolbar()
        self.assertIsInstance(out, HTML)

    def test_toolbar_with_history_and_summary(self):
        from prompt_toolkit.formatted_text import HTML
        sess = self._make_session(
            history=[{"role": "user", "content": "hi"}] * 5,
            summary_text="prior summary",
        )
        out = sess._toolbar()
        self.assertIsInstance(out, HTML)

    def test_ctx_pct_zero_when_no_ctx_size(self):
        from callbacks import TerminalCallbacks
        sess = tui.TuiSession(
            history=[{"role": "user", "content": "hi"}],
            summary_state={},
            config={"llm": {"model": "m"}},
            ctx_size=0,
            cb=TerminalCallbacks(),
            estimate_tokens=lambda m: 10,
        )
        self.assertEqual(sess._ctx_pct(), 0.0)

    def test_ctx_pct_cache_hits_on_unchanged_state(self):
        """Second _ctx_pct call with unchanged history should reuse the cache."""
        from callbacks import TerminalCallbacks
        calls = {"n": 0}

        def counting_estimate(m):
            calls["n"] += 1
            return 10

        sess = tui.TuiSession(
            history=[{"role": "user", "content": "hi"}] * 3,
            summary_state={"text": "s"},
            config={"llm": {"model": "m"}},
            ctx_size=4096,
            cb=TerminalCallbacks(),
            estimate_tokens=counting_estimate,
        )
        sess._ctx_pct()
        first = calls["n"]
        sess._ctx_pct()
        self.assertEqual(calls["n"], first,
                         "cache miss on unchanged state")

    def test_ctx_pct_cache_invalidates_on_history_append(self):
        """Appending to history should invalidate the cache."""
        from callbacks import TerminalCallbacks
        calls = {"n": 0}

        def counting_estimate(m):
            calls["n"] += 1
            return 10

        hist = [{"role": "user", "content": "hi"}]
        sess = tui.TuiSession(
            history=hist,
            summary_state={},
            config={"llm": {"model": "m"}},
            ctx_size=4096,
            cb=TerminalCallbacks(),
            estimate_tokens=counting_estimate,
        )
        sess._ctx_pct()
        first = calls["n"]
        hist.append({"role": "user", "content": "again"})
        sess._ctx_pct()
        self.assertGreater(calls["n"], first,
                           "cache should have invalidated on append")

    def test_ctx_pct_cache_invalidates_on_summary_length_change(self):
        """Summary text growing in length should invalidate the cache."""
        from callbacks import TerminalCallbacks
        calls = {"n": 0}

        def counting_estimate(m):
            calls["n"] += 1
            return 10

        summary = {"text": "short"}
        sess = tui.TuiSession(
            history=[{"role": "user", "content": "hi"}],
            summary_state=summary,
            config={"llm": {"model": "m"}},
            ctx_size=4096,
            cb=TerminalCallbacks(),
            estimate_tokens=counting_estimate,
        )
        sess._ctx_pct()
        first = calls["n"]
        summary["text"] = "a much longer summary text"
        sess._ctx_pct()
        self.assertGreater(calls["n"], first)

    def test_toolbar_label_shows_tilde_when_fallback_tokenizer(self):
        """When the real tokenizer isn't loaded, the ctx% label should be prefixed with ~."""
        import token_utils
        sess = self._make_session(
            history=[{"role": "user", "content": "hi"}],
            summary_text=None,
        )
        with mock.patch.object(token_utils, "_EXACT_TOKENIZER_AVAILABLE", False):
            html = sess._toolbar()
            # HTML object stringifies to its raw input
            self.assertIn("ctx ~", str(html.value))

    def test_toolbar_label_no_tilde_when_real_tokenizer(self):
        """When the real tokenizer is loaded, the ~ approximation marker drops."""
        import token_utils
        sess = self._make_session(
            history=[{"role": "user", "content": "hi"}],
            summary_text=None,
        )
        with mock.patch.object(token_utils, "_EXACT_TOKENIZER_AVAILABLE", True):
            # Invalidate cache so the label re-renders with new flag value
            sess._ctx_cache_key = None
            html = sess._toolbar()
            self.assertNotIn("ctx ~", str(html.value))
            self.assertIn("ctx ", str(html.value))


    def test_toolbar_uses_unicode_separator(self):
        """Footer bar segments must be separated by │ (U+2502), not ASCII |."""
        sess = self._make_session()
        html = sess._toolbar()
        self.assertIn("│", str(html.value),
                      "toolbar must use │ (U+2502) separators between segments")


@unittest.skipUnless(tui._AVAILABLE, "prompt_toolkit not installed")
class TestPromptActiveFlag(unittest.TestCase):
    def tearDown(self):
        tui._prompt_active.on = False

    def test_default_is_false(self):
        # Fresh thread-local — no .on attribute
        tl = tui.threading.local()
        self.assertFalse(getattr(tl, "on", False))
        self.assertFalse(tui._prompt_is_active())

    def test_flag_when_set(self):
        tui._prompt_active.on = True
        self.assertTrue(tui._prompt_is_active())
        tui._prompt_active.on = False
        self.assertFalse(tui._prompt_is_active())

    def test_tuicallbacks_print_wraps_when_active(self):
        """TuiCallbacks._print should enter patch_stdout while prompt is active."""
        from unittest.mock import MagicMock, patch
        sess = MagicMock(spec=tui.TuiSession)
        sess.set_cb = MagicMock()
        cb = tui.TuiCallbacks(sess, verbose=False)

        tui._prompt_active.on = True
        try:
            with patch("prompt_toolkit.patch_stdout.patch_stdout") as m_ps:
                m_ps.return_value.__enter__ = lambda self_: None
                m_ps.return_value.__exit__ = lambda self_, *a: None
                cb._print("hello")
                m_ps.assert_called_once_with(raw=True)
        finally:
            tui._prompt_active.on = False

    def test_tuicallbacks_print_bare_when_inactive(self):
        """When the prompt isn't active, _print should not touch patch_stdout."""
        from unittest.mock import MagicMock, patch
        sess = MagicMock(spec=tui.TuiSession)
        sess.set_cb = MagicMock()
        cb = tui.TuiCallbacks(sess, verbose=False)

        tui._prompt_active.on = False
        with patch("prompt_toolkit.patch_stdout.patch_stdout") as m_ps:
            with patch("builtins.print") as m_print:
                cb._print("hi")
                m_ps.assert_not_called()
                m_print.assert_called_once()


class TestStubFallback(unittest.TestCase):
    """When prompt_toolkit is missing, TuiSession/TuiCallbacks raise ImportError.

    Runs in a subprocess so module-state hacks (clearing prompt_toolkit
    from sys.modules, reloading tui) don't contaminate the parent test
    runner's module cache — that would break every other TUI test.
    """

    def test_stub_raises_importerror(self):
        import subprocess, textwrap
        script = textwrap.dedent("""
            import sys
            # Block prompt_toolkit before tui is imported so the else-branch loads.
            sys.modules["prompt_toolkit"] = None
            sys.path.insert(0, %r)
            import tui
            assert tui._AVAILABLE is False, "expected stub mode"
            try:
                tui.TuiSession()
            except ImportError as e:
                assert "prompt_toolkit" in str(e), f"unexpected msg: {e}"
                print("OK")
            else:
                raise AssertionError("TuiSession did not raise in stub mode")
        """ % str(Path(__file__).parent.parent))
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         msg=f"stderr={result.stderr}\nstdout={result.stdout}")
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
