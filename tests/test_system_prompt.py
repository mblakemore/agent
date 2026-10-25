"""preferences.system_prompt and /system: the user's own instructions lead the
system message of every request, and /system shows, sets and clears them."""

import copy
import io
import json
import logging
from contextlib import redirect_stdout
from unittest.mock import MagicMock

import pytest

import agent
import commands
from test_cache_discipline import _assert_prefix_chain, _tool_turn, _wire
from test_commands import _make_ctx

log = logging.getLogger("test_system_prompt")
PROMPT = "You are a helpful assistant. Always use excessive emojis."


def _run(mocks, monkeypatch, backend, prompt):
    monkeypatch.setattr(agent, "_main_backend", backend)
    monkeypatch.setitem(agent._config.setdefault("preferences", {}), "system_prompt", prompt)
    sent = []
    real = mocks._fake_llm_request

    def capture(log, **kw):
        sent.append(copy.deepcopy(kw["json"]))
        return real(log, **kw)
    monkeypatch.setattr(agent, "_llm_request", capture)
    mocks.stub_tool("read_file", "file contents")
    mocks.script(_tool_turn("t1"), _tool_turn("t2"), _tool_turn("t3"))
    agent.run_agent_single([{"role": "user", "content": "go"}], {"text": "", "up_to": 0}, [], log)
    return sent


def test_prompt_leads_the_system_message(agent_loop_mocks, monkeypatch):
    backend = MagicMock(kind="llamacpp", model="m", honours_system=True, base_url="http://x")
    sent = _run(agent_loop_mocks, monkeypatch, backend, PROMPT)
    first = sent[0]["messages"][0]
    assert first["role"] == "system"
    assert first["content"].startswith(PROMPT + "\n\nWorking directory:")


def test_prompt_stays_in_the_cached_head(agent_loop_mocks, monkeypatch):
    backend = MagicMock(kind="aicommons", model="claude-v5.5-opus", honours_system=True,
                        base_url="https://gw/v1")
    sent = _run(agent_loop_mocks, monkeypatch, backend, PROMPT)
    assert _wire(sent[0])["system"] == PROMPT
    _assert_prefix_chain(sent)


def test_blank_prompt_sends_nothing(agent_loop_mocks, monkeypatch):
    backend = MagicMock(kind="llamacpp", model="m", honours_system=True, base_url="http://x")
    sent = _run(agent_loop_mocks, monkeypatch, backend, "   ")
    assert sent[0]["messages"][0]["content"].startswith("Working directory:")


def _ctx():
    ctx = _make_ctx()
    ctx.config["preferences"] = {}
    ctx.set_system_prompt = lambda text: ctx.config["preferences"].__setitem__("system_prompt", text)
    return ctx


def test_system_command_sets_shows_and_clears():
    ctx = _ctx()
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.handle_command("/system " + PROMPT, ctx)
        assert ctx.config["preferences"]["system_prompt"] == PROMPT
        commands.handle_command("/system", ctx)
        assert PROMPT in buf.getvalue()
        commands.handle_command("/system clear", ctx)
    assert ctx.config["preferences"]["system_prompt"] == ""


def test_set_system_prompt_persists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(agent._config, "preferences", dict(agent._config.get("preferences", {})))
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps({"llm": {"model": "keep"}}))
    agent._set_system_prompt(PROMPT)
    saved = json.loads((tmp_path / ".agent" / "config.json").read_text())
    assert saved == {"llm": {"model": "keep"}, "preferences": {"system_prompt": PROMPT}}
    assert agent._config["preferences"]["system_prompt"] == PROMPT
