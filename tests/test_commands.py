"""Unit tests for commands.py — the slash-command dispatcher."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import commands
import callbacks


def _set_model_default(ctx, role, model):
    """Mirror agent._set_model_for_role's config mutation for ctx defaults."""
    section = "llm" if role == "main" else "summary"
    ctx.config.setdefault(section, {})["model"] = model


def _make_ctx(**overrides):
    log = SimpleNamespace(info=lambda *a, **kw: None,
                          warning=lambda *a, **kw: None,
                          error=lambda *a, **kw: None)
    base = SimpleNamespace(
        conversation_history=[],
        summary_state={"text": "prior", "up_to": 3},
        initial_files="files",
        async_summarizer=None,
        cb=callbacks.TerminalCallbacks(),
        log=log,
        log_path="/tmp/a.log",
        ctx_size=4096,
        config={"llm": {"model": "old-model"},
                "summary": {"model": "old-summary"}},
        base_url="http://127.0.0.1:8080",
        summary_base_url="http://127.0.0.1:8082",
        setup_logger=lambda: (log, "/tmp/new.log", "/tmp/err.log"),
        pick_model=lambda current, base_url: "new-model",
        set_model=lambda role, model: _set_model_default(base, role, model),
        render_context_bar=lambda h, s, c: f"context-bar({len(h)})",
        refresh_cb_log=lambda l: None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestHandleCommand(unittest.TestCase):
    def test_non_slash_returns_false(self):
        self.assertFalse(commands.handle_command("hello", _make_ctx()))
        self.assertFalse(commands.handle_command("  ", _make_ctx()))

    def test_unknown_slash_command_consumed_with_warning(self):
        ctx = _make_ctx()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/nope", ctx))
        # on_notice(warn, ...) should have been emitted somewhere

    def test_help_prints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/help", _make_ctx()))
        out = buf.getvalue()
        self.assertIn("/help", out)
        self.assertIn("/clear", out)
        self.assertIn("/verbose", out)

    def test_clear_resets_history_and_rotates_log(self):
        ctx = _make_ctx()
        ctx.conversation_history.extend([{"role": "user"}, {"role": "assistant"}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/clear", ctx))
        self.assertEqual(len(ctx.conversation_history), 0)
        self.assertEqual(ctx.summary_state["text"], "")
        self.assertEqual(ctx.summary_state["up_to"], 0)
        self.assertIsNone(ctx.initial_files)
        self.assertEqual(ctx.log_path, "/tmp/new.log")

    def test_context_calls_render(self):
        ctx = _make_ctx()
        ctx.conversation_history.extend([{"role": "user"}, {"role": "assistant"}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/context", ctx))
        self.assertIn("context-bar(2)", buf.getvalue())

    def test_model_updates_config(self):
        ctx = _make_ctx()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/model", ctx))
        self.assertEqual(ctx.config["llm"]["model"], "new-model")

    def test_model_keeps_old_when_picker_returns_none(self):
        ctx = _make_ctx(pick_model=lambda cur, url: None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/model", ctx)
        self.assertEqual(ctx.config["llm"]["model"], "old-model")

    def test_parse_model_args_roles(self):
        self.assertEqual(commands._parse_model_args(""), ("main", None))
        self.assertEqual(commands._parse_model_args("main"), ("main", None))
        self.assertEqual(commands._parse_model_args("MAIN"), ("main", None))
        self.assertEqual(commands._parse_model_args("summary"), ("summary", None))
        self.assertEqual(commands._parse_model_args("main foo-1"), ("main", "foo-1"))
        self.assertEqual(commands._parse_model_args("summary bar-2"), ("summary", "bar-2"))
        self.assertEqual(commands._parse_model_args("bogus"), (None, None))

    def test_model_bare_defaults_to_main(self):
        picked = {}
        def _picker(cur, url):
            picked["url"] = url
            return "new-model"
        ctx = _make_ctx(pick_model=_picker)
        with redirect_stdout(io.StringIO()):
            commands.handle_command("/model", ctx)
        # bare /model targets main → uses ctx.base_url, sets llm.model
        self.assertEqual(picked["url"], "http://127.0.0.1:8080")
        self.assertEqual(ctx.config["llm"]["model"], "new-model")

    def test_model_summary_uses_summary_url_and_sets_summary(self):
        picked = {}
        def _picker(cur, url):
            picked["url"] = url
            return "sum-model"
        ctx = _make_ctx(pick_model=_picker)
        with redirect_stdout(io.StringIO()):
            commands.handle_command("/model summary", ctx)
        self.assertEqual(picked["url"], "http://127.0.0.1:8082")
        self.assertEqual(ctx.config["summary"]["model"], "sum-model")
        self.assertEqual(ctx.config["llm"]["model"], "old-model")  # main untouched

    def test_model_direct_set_skips_picker(self):
        called = {"n": 0}
        def _picker(cur, url):
            called["n"] += 1
            return "PICKED"
        ctx = _make_ctx(pick_model=_picker)
        with redirect_stdout(io.StringIO()):
            commands.handle_command("/model main gemma-direct", ctx)
        self.assertEqual(called["n"], 0)
        self.assertEqual(ctx.config["llm"]["model"], "gemma-direct")

    def test_model_unknown_role_warns_and_no_change(self):
        ctx = _make_ctx()
        with redirect_stdout(io.StringIO()):
            commands.handle_command("/model bogus", ctx)
        self.assertEqual(ctx.config["llm"]["model"], "old-model")
        self.assertEqual(ctx.config["summary"]["model"], "old-summary")

    def test_verbose_toggles_flag(self):
        ctx = _make_ctx()
        self.assertFalse(ctx.cb.verbose)
        commands.handle_command("/verbose", ctx)
        self.assertTrue(ctx.cb.verbose)
        commands.handle_command("/verbose", ctx)
        self.assertFalse(ctx.cb.verbose)

    def test_tools_calls_render_tools(self):
        ctx = _make_ctx()
        ctx.cb.on_tool_result("file", {"action": "read"}, "data", False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/tools", ctx)
        self.assertIn("file", buf.getvalue())

    def test_clear_with_stray_args_still_clears_and_warns(self):
        """Regression guard: no-arg commands still run when given stray
        trailing tokens, but emit a warn notice so the typo is visible.
        Pins the behavior introduced with /tools arg parsing in CICD 0002."""
        ctx = _make_ctx()
        ctx.conversation_history.extend([{"role": "user"}, {"role": "assistant"}])
        notices: list[tuple[str, str]] = []
        ctx.cb.on_notice = lambda level, msg: notices.append((level, msg))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertTrue(commands.handle_command("/clear now", ctx))
        self.assertEqual(len(ctx.conversation_history), 0)
        self.assertTrue(any(level == "warn" and "/clear" in msg
                            for level, msg in notices))

    def test_clear_resets_async_summarizer(self):
        ctx = _make_ctx()
        ctx.async_summarizer = SimpleNamespace(reset=lambda: None)
        # Use a mock for reset to verify it was called
        import unittest.mock as um
        ctx.async_summarizer.reset = um.Mock()
        commands.handle_command("/clear", ctx)
        ctx.async_summarizer.reset.assert_called_once()

    def test_tools_no_render_tools_capability(self):
        ctx = _make_ctx()
        # Use a SimpleNamespace to ensure render_tools is missing
        ctx.cb = SimpleNamespace()
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/tools", ctx)
        self.assertEqual(buf.getvalue(), "")

    def test_tools_valid_int_limit(self):
        ctx = _make_ctx()
        # Mock render_tools to verify limit
        import unittest.mock as um
        ctx.cb.render_tools = um.Mock(return_value="tools-output")
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/tools 5", ctx)
        ctx.cb.render_tools.assert_called_once_with(limit=5)

    def test_tools_invalid_int_limit(self):
        ctx = _make_ctx()
        notices = []
        ctx.cb.on_notice = lambda level, msg: notices.append((level, msg))
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/tools not-a-number", ctx)
        self.assertTrue(any(level == "warn" and "usage: /tools" in msg
                            for level, msg in notices))

    def test_tools_non_positive_int_limit(self):
        ctx = _make_ctx()
        notices = []
        ctx.cb.on_notice = lambda level, msg: notices.append((level, msg))
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/tools 0", ctx)
        self.assertTrue(any(level == "warn" and "positive integer" in msg
                            for level, msg in notices))

    def test_tools_all_keyword(self):
        ctx = _make_ctx()
        import unittest.mock as um
        ctx.cb.render_tools = um.Mock(return_value="all-tools")
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.handle_command("/tools all", ctx)
        ctx.cb.render_tools.assert_called_once_with(limit=None)

    def test_phase_success(self):
        ctx = _make_ctx()
        import unittest.mock as um
        with um.patch("commands.get_tasks", return_value=[
            {"description": "perceive", "status": "done"},
            {"description": "probe", "status": "in_progress"},
        ]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                commands.handle_command("/phase", ctx)
            out = buf.getvalue()
            self.assertIn("PERCEIVE ✓", out)
            self.assertIn("PROBE ✗", out)
            self.assertIn("DECIDE ✗", out)

    def test_phase_error_loading_tasks(self):
        ctx = _make_ctx()
        import unittest.mock as um
        with um.patch("commands.get_tasks", side_effect=Exception("disk error")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                commands.handle_command("/phase", ctx)
            self.assertIn("Error loading tasks: disk error", buf.getvalue())


class TestVerboseCli(unittest.TestCase):
    """--verbose CLI flag must be forwarded into run_agent_interactive."""

    def _run_main(self, argv):
        import agent
        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)

        import unittest.mock as um
        with um.patch.object(sys, "argv", argv):
            with um.patch.object(agent, "run_agent_interactive", side_effect=fake_run):
                agent.main()
        return captured

    def test_verbose_flag_propagates(self):
        captured = self._run_main(["agent.py", "--verbose", "-a", "hello"])
        self.assertTrue(captured.get("verbose"))
        self.assertTrue(captured.get("auto"))

    def test_verbose_default_false(self):
        captured = self._run_main(["agent.py", "-a", "hello"])
        self.assertFalse(captured.get("verbose"))

    def test_verbose_constructs_terminal_cb_with_flag(self):
        """Default TerminalCallbacks picks up the verbose kwarg."""
        import callbacks as _cb
        cb = _cb.TerminalCallbacks(verbose=True)
        self.assertTrue(cb.verbose)
        cb2 = _cb.TerminalCallbacks(verbose=False)
        self.assertFalse(cb2.verbose)


class TestAgentWizard:
    """Tests for the /agent scaffold wizard (pytest-style, uses tmp_path)."""

    def _run_wizard(self, monkeypatch, tmp_path, inputs):
        monkeypatch.chdir(tmp_path)
        input_iter = iter(inputs)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(input_iter))
        ctx = _make_ctx()
        import unittest.mock as um
        with um.patch("sys.stdout", io.StringIO()):
            commands._cmd_agent(ctx, "")

    def test_agent_wizard_creates_agent_md(self, monkeypatch, tmp_path):
        """Wizard with standard inputs creates AGENT.md containing the agent name."""
        self._run_wizard(monkeypatch, tmp_path, [
            "TestBot",    # agent name
            "A test bot", # role
            "6-phase",    # loop type
            "AGENT.md",   # filename
            "",           # extras (none)
        ])
        agent_md = tmp_path / "AGENT.md"
        assert agent_md.exists(), "AGENT.md was not created"
        assert "TestBot" in agent_md.read_text()

    def test_agent_wizard_creates_state_files(self, monkeypatch, tmp_path):
        """Wizard creates state/current-state.json, state/focus.json, state/memories/context.json."""
        self._run_wizard(monkeypatch, tmp_path, [
            "BotA", "", "", "", "",
        ])
        assert (tmp_path / "state" / "current-state.json").exists()
        assert (tmp_path / "state" / "focus.json").exists()
        assert (tmp_path / "state" / "memories" / "context.json").exists()

    def test_agent_wizard_skips_existing_file(self, monkeypatch, tmp_path):
        """Wizard does not overwrite a pre-existing AGENT.md."""
        existing = tmp_path / "AGENT.md"
        existing.write_text("old")
        self._run_wizard(monkeypatch, tmp_path, [
            "BotB", "", "", "", "",
        ])
        assert existing.read_text() == "old"

    def test_agent_wizard_memory_extras_patterns(self, monkeypatch, tmp_path):
        """Specifying 'patterns' in extras creates state/memories/patterns.jsonl."""
        self._run_wizard(monkeypatch, tmp_path, [
            "BotC", "", "", "", "patterns",
        ])
        assert (tmp_path / "state" / "memories" / "patterns.jsonl").exists()

    def test_agent_wizard_default_loop_is_six_phase(self, monkeypatch, tmp_path):
        """Empty answer for loop type defaults to 6-phase (AGENT.md contains PERCEIVE)."""
        self._run_wizard(monkeypatch, tmp_path, [
            "BotD", "", "", "", "",
        ])
        content = (tmp_path / "AGENT.md").read_text()
        assert "PERCEIVE" in content or "6-phase" in content

    def test_agent_wizard_registered_in_commands(self):
        """/agent is registered in _COMMANDS."""
        assert "/agent" in commands._COMMANDS


if __name__ == "__main__":
    unittest.main()
