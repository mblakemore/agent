"""Tests for ui.py — the shared component layer (design system step 2).

Contract under test (plan/tui-design-system.md § 2, § 3.3):
  * every component honors NO_COLOR (zero escapes when color is off);
  * the four-state status model renders glyph + color, glyphs distinct;
  * prompts follow the one contract (default in brackets, ENTER takes it,
    y/n parsed in exactly one place);
  * menus render options first, pick by number, pass typed text through;
  * ui stays a leaf module (no agent/callbacks imports).
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme
import ui


def _script(*answers):
    """An input_fn fed from a list; records the prompts it was shown."""
    answers = list(answers)
    prompts = []

    def input_fn(shown):
        prompts.append(shown)
        return answers.pop(0)

    input_fn.prompts = prompts
    return input_fn


class TestNoColorParity(unittest.TestCase):
    """With color suppressed, no component may emit an escape byte."""

    def test_all_renderers_plain_under_no_color(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            outputs = [
                ui.banner("setup", version="1.0", sha="abc1234"),
                ui.section("CALIBRATION"),
                ui.bullet("a bullet"),
                ui.mark("ok"), ui.mark("warn"), ui.mark("err"),
                ui.mark("unknown"),
                ui.status_word("err", "FAILED"),
                ui.status_row("ok", "main", "[llamacpp]  model-x"),
                "\n".join(ui.numbered(["a", "b"], current="a")),
                ui.fit("x" * 500),
            ]
        for out in outputs:
            self.assertNotIn("\x1b", out)

    def test_fit_returns_untouched_when_piped(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            line = "y" * 500
            self.assertEqual(ui.fit(line), line)


class TestStatusModel(unittest.TestCase):
    def test_four_states_have_distinct_glyphs(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            glyphs = [ui.mark(s) for s in ("ok", "warn", "err", "unknown")]
        self.assertEqual(len(set(glyphs)), 4)
        self.assertEqual(glyphs, ["●", "⚠", "✗", "○"])

    def test_states_use_semantic_tokens(self):
        with mock.patch.object(theme, "_no_color", return_value=False):
            self.assertIn(theme.escape(theme.GOOD), ui.mark("ok"))
            self.assertIn(theme.escape(theme.WARN), ui.mark("warn"))
            self.assertIn(theme.escape(theme.ERR), ui.mark("err"))
            self.assertIn(theme.DIM, ui.mark("unknown"))

    def test_status_row_shapes(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            self.assertEqual(ui.status_row("ok", "main", "model-x"),
                             "● main model-x")
            self.assertEqual(ui.status_row("warn", "summary"), "⚠ summary")

    def test_unknown_state_raises_keyerror(self):
        with self.assertRaises(KeyError):
            ui.mark("nope")


class TestStructure(unittest.TestCase):
    def test_banner_shape(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            lines = ui.banner("setup", version="0.1.0", sha="c2faf95").split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "─" * ui.RULE_WIDTH)
        self.assertEqual(lines[2], lines[0])
        self.assertIn("agent v0.1.0 (c2faf95)  ·  setup", lines[1])

    def test_banner_bare(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            mid = ui.banner().split("\n")[1]
        self.assertEqual(mid, "agent")

    def test_section(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            self.assertEqual(ui.section("MAIN", " model"),
                             "\n── MAIN model ──")

    def test_bullet_indent_scale(self):
        self.assertEqual(ui.bullet("x"), "  • x")
        self.assertEqual(ui.bullet("x", level=2), "    • x")


class TestPrompts(unittest.TestCase):
    def test_ask_shows_default_in_brackets(self):
        fn = _script("")
        self.assertEqual(ui.ask("model", "gpt", input_fn=fn), "gpt")
        self.assertEqual(fn.prompts, ["model [gpt]: "])

    def test_ask_no_default(self):
        fn = _script("")
        self.assertEqual(ui.ask("model", input_fn=fn), "")
        self.assertEqual(fn.prompts, ["model: "])

    def test_ask_typed_answer_wins(self):
        self.assertEqual(ui.ask("q", "d", input_fn=_script("  typed  ")),
                         "typed")

    def test_ask_yn(self):
        self.assertTrue(ui.ask_yn("go", default=True, input_fn=_script("")))
        self.assertFalse(ui.ask_yn("go", default=False, input_fn=_script("")))
        self.assertTrue(ui.ask_yn("go", default=False,
                                  input_fn=_script("Yes please")))
        self.assertFalse(ui.ask_yn("go", default=True, input_fn=_script("n")))
        fn = _script("")
        ui.ask_yn("go", default=True, input_fn=fn)
        self.assertEqual(fn.prompts, ["go (y/n) [y]: "])


class TestMenu(unittest.TestCase):
    def _run(self, answer, **kw):
        printed = []
        chosen = ui.menu("pick", ["alpha", "beta"], input_fn=_script(answer),
                         print_fn=printed.append, **kw)
        return chosen, printed

    def test_number_picks_option(self):
        chosen, printed = self._run("2")
        self.assertEqual(chosen, "beta")
        self.assertEqual(len(printed), 2)

    def test_options_render_before_question(self):
        with mock.patch.object(theme, "_no_color", return_value=True):
            _, printed = self._run("1", current="beta")
        self.assertEqual(printed[0], "    1) alpha")
        self.assertEqual(printed[1], "  ● 2) beta")

    def test_typed_text_passes_through(self):
        chosen, _ = self._run("http://127.0.0.1:9090")
        self.assertEqual(chosen, "http://127.0.0.1:9090")

    def test_out_of_range_number_passes_through(self):
        chosen, _ = self._run("7")
        self.assertEqual(chosen, "7")

    def test_enter_takes_default(self):
        chosen, _ = self._run("", default="alpha")
        self.assertEqual(chosen, "alpha")


class TestWarnStderr(unittest.TestCase):
    def test_plain_when_stderr_not_tty(self):
        import io
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            ui.warn_stderr("backend down")
        self.assertEqual(buf.getvalue(), "⚠ backend down\n")
        self.assertNotIn("\x1b", buf.getvalue())

    def test_colored_on_tty_with_color_on(self):
        import io

        class TtyIO(io.StringIO):
            def isatty(self):
                return True

        buf = TtyIO()
        with mock.patch.object(sys, "stderr", buf), \
             mock.patch.object(theme, "_no_color", return_value=False):
            ui.warn_stderr("backend down")
        self.assertIn("\x1b", buf.getvalue())
        self.assertIn("⚠ backend down", theme.strip_ansi(buf.getvalue()))


class TestLeafModule(unittest.TestCase):
    def test_ui_imports_no_front_end(self):
        import importlib
        importlib.reload(ui)
        for forbidden in ("agent", "callbacks", "commands", "tui",
                          "setup_wizard", "agent_scaffold"):
            self.assertNotIn(forbidden, ui.__dict__,
                             f"ui.py must not import {forbidden}")

    def test_no_raw_ansi_literals_in_source(self):
        """Same rule test_tools_no_raw_ansi polices for tools/: every
        escape routes through theme."""
        from pathlib import Path
        src = (Path(ui.__file__)).read_text()
        self.assertNotIn("\\033[", src)


if __name__ == "__main__":
    unittest.main()
