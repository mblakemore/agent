"""Restart in place: /restart and the restart tool re-execute the process with -c so the
same session continues (Mike, 2026-11-12: "so I or she can restart her agent.py process
with -c to get back into the same session").

The request is a flag read at turn boundaries: the loops end on it, run_agent_single
returns "restart" after the turn's checkpoint, and main() execs after the teardown.
"""

import copy
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent
import commands
import tools.restart as restart_tool

log = logging.getLogger("test_restart")


@pytest.fixture(autouse=True)
def _fresh_restart_state():
    """Clean restart state, and the registry hooks as agent.py installs them at import
    (another test file may have swapped the registry's dicts or the config)."""
    import tools as registry
    saved = copy.deepcopy(agent._RESTART)
    agent._RESTART.update({"requested": False, "by": "", "reason": "", "at": 0.0,
                           "resumed": None, "recent": []})
    registry.RUNTIME["request_restart"] = agent._request_restart
    registry.GATES["restart"] = agent._restart_gate
    if not isinstance(agent._config.get("preferences"), dict):
        agent._config["preferences"] = {}
    yield
    agent._RESTART.clear()
    agent._RESTART.update(saved)


def _tool_turn(tid, name, args):
    return [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": tid, "type": "function", "function": {"name": name, "arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


# ── the pieces ────────────────────────────────────────────────────────────────

def test_restart_argv_adds_c_once():
    assert agent._restart_argv(["agent.py"]) == ["agent.py", "-c"]
    assert agent._restart_argv(["agent.py", "-c"]) == ["agent.py", "-c"]
    assert agent._restart_argv(["agent.py", "--continue", "--no-tui"]) == ["agent.py", "--continue", "--no-tui"]
    assert agent._restart_argv(["agent.py", "--no-tui", "--model", "x"]) == ["agent.py", "-c", "--no-tui", "--model", "x"]


def test_request_sets_flag_and_records_time():
    ok, msg = agent._request_restart(by="command", reason="  new   code ")
    assert ok and "re-executes itself with -c" in msg
    assert agent._RESTART["requested"] and agent._RESTART["by"] == "command"
    assert agent._RESTART["reason"] == "new code"
    assert len(agent._RESTART["recent"]) == 1
    ok2, msg2 = agent._request_restart(by="tool")
    assert ok2 and "already requested" in msg2


def test_tool_refused_when_too_often_but_keyboard_is_not():
    import time
    agent._RESTART["recent"] = [time.time() - 10, time.time() - 20, time.time() - 30]
    ok, msg = agent._request_restart(by="tool", reason="again")
    assert not ok and "3 times" in msg
    assert not agent._RESTART["requested"]
    ok, _ = agent._request_restart(by="command")
    assert ok and agent._RESTART["requested"]


def test_old_restarts_fall_out_of_the_window():
    agent._RESTART["recent"] = [1.0, 2.0, 3.0]   # long ago
    assert not agent._restart_too_often()
    assert agent._RESTART["recent"] == []


def test_exec_restart_uses_python_and_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(agent.sys, "argv", ["/x/agent.py", "--no-tui"])
    monkeypatch.setattr(agent.os, "execv", lambda exe, argv: calls.append((exe, argv)))
    monkeypatch.setattr(agent.logging, "shutdown", lambda: None)
    agent._exec_restart()
    assert calls == [(agent.sys.executable, [agent.sys.executable, "/x/agent.py", "-c", "--no-tui"])]


def test_exec_restart_failure_exits_3(monkeypatch, capsys):
    monkeypatch.setattr(agent.sys, "argv", ["/x/agent.py"])

    def boom(exe, argv):
        raise OSError("no exec")
    monkeypatch.setattr(agent.os, "execv", boom)
    monkeypatch.setattr(agent.logging, "shutdown", lambda: None)
    with pytest.raises(SystemExit) as e:
        agent._exec_restart()
    assert e.value.code == 3
    assert "restart failed" in capsys.readouterr().err


# ── the checkpoint carries the restart ────────────────────────────────────────

def test_checkpoint_round_trip_carries_restart(tmp_path, monkeypatch):
    path = str(tmp_path / "conversation_checkpoint.json")
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", path)
    agent._request_restart(by="tool", reason="skills changed")
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    agent._save_checkpoint(history, {"text": "", "up_to": 0}, 7, None)
    assert agent._CKPT_TURN["turn"] == 7
    cp = json.load(open(path))
    assert cp["restart"]["by"] == "tool" and cp["restart"]["reason"] == "skills changed"
    assert len(cp["restart"]["recent"]) == 1
    # the next process
    agent._RESTART.update({"requested": False, "resumed": None, "recent": []})
    loaded = agent._load_checkpoint()
    assert loaded[2] == 7 and len(loaded[0]) == 2
    assert agent._RESTART["resumed"]["by"] == "tool"
    assert len(agent._RESTART["recent"]) == 1   # the loop guard survives the restart


def test_checkpoint_without_a_restart_has_no_record(tmp_path, monkeypatch):
    path = str(tmp_path / "conversation_checkpoint.json")
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", path)
    agent._save_checkpoint([{"role": "user", "content": "x"}], {"text": "", "up_to": 0}, 1, None)
    agent._load_checkpoint()
    assert agent._RESTART["resumed"] is None


# ── the command ───────────────────────────────────────────────────────────────

def test_slash_restart_calls_the_hook():
    seen = []
    cb = MagicMock()
    ctx = SimpleNamespace(cb=cb, request_restart=lambda by, reason: (seen.append((by, reason)) or (True, "ok")))
    assert commands.handle_command("/restart new tools", ctx)
    assert seen == [("command", "new tools")]
    assert "/restart" in commands._cmd_help.__code__.co_consts or True   # help lists it (text test below)


def test_slash_restart_without_hook_warns():
    cb = MagicMock()
    ctx = SimpleNamespace(cb=cb)
    assert commands.handle_command("/restart", ctx)
    assert any("no restart control" in str(c) for c in cb.on_notice.call_args_list)


def test_help_names_restart(capsys):
    cb = MagicMock()
    cb._print = lambda text: print(text)
    commands.handle_command("/help", SimpleNamespace(cb=cb))
    assert "/restart" in capsys.readouterr().out


# ── the tool ──────────────────────────────────────────────────────────────────

def test_tool_off_by_default_and_on_with_the_preference(monkeypatch):
    monkeypatch.setitem(agent._config, "preferences", {})
    assert not agent._registry.GATES["restart"]()
    monkeypatch.setitem(agent._config, "preferences", {"restart_tool": True})
    assert agent._registry.GATES["restart"]()


def test_tool_fn_requests_through_runtime(monkeypatch):
    out = restart_tool.fn(reason="config changed")
    assert "re-executes itself with -c" in out
    assert agent._RESTART["requested"] and agent._RESTART["by"] == "tool"


def test_tool_fn_without_runtime():
    import tools as registry
    saved = registry.RUNTIME.pop("request_restart")
    try:
        assert restart_tool.fn().startswith("Error")
    finally:
        registry.RUNTIME["request_restart"] = saved


# ── the loop ends on the flag, after the turn's checkpoint ────────────────────

def test_run_agent_single_returns_restart_after_the_tool_turn(monkeypatch, agent_loop_mocks, tmp_path):
    monkeypatch.setattr(agent, "_CHECKPOINT_PATH", str(tmp_path / "cp.json"))
    monkeypatch.setitem(agent._config, "preferences", {"restart_tool": True})
    agent.MAP_FN["restart"] = restart_tool.fn
    agent_loop_mocks.script(_tool_turn("c1", "restart", json.dumps({"reason": "new code"})))
    history = [{"role": "user", "content": "please restart"}]
    result = agent.run_agent_single(history, {"text": "", "up_to": 0}, None, log,
                                    0.7, 0.9, 40, 0.0, 512, 8000)
    assert result == "restart"
    assert history[-1]["role"] == "tool" and "re-executes" in history[-1]["content"]
    cp = json.load(open(tmp_path / "cp.json"))
    assert cp["restart"]["by"] == "tool" and cp["restart"]["reason"] == "new code"
    assert cp["conversation_history"][-1]["role"] == "tool"


def test_interactive_loop_ends_on_slash_restart():
    """A /restart at the prompt ends the loop before the next input is read, and the
    boundary checkpoint is written."""
    saved = []

    def fake_handle(line, ctx):
        if line == "/restart":
            ctx.request_restart(by="command", reason="")
            return True
        return False

    with patch('agent._setup_logger', return_value=(MagicMock(), "log.txt", "err.txt")), \
         patch('agent._check_api_health', return_value=(True, "")), \
         patch('agent._detect_ctx_size', return_value=None), \
         patch('agent._emit'), \
         patch('agent._auto_increment_cycle'), \
         patch('agent._load_checkpoint', return_value=None), \
         patch('agent._delete_checkpoint'), \
         patch('agent.cleanup_temp_sessions'), \
         patch('agent.TerminalCallbacks'), \
         patch('agent._save_checkpoint', side_effect=lambda *a, **k: saved.append(a)), \
         patch('agent.run_agent_single', return_value="success") as mock_run_single, \
         patch('agent._commands.handle_command', side_effect=fake_handle), \
         patch('builtins.input', side_effect=['/restart', 'hello', 'exit']) as mock_input:
        agent.run_agent_interactive(initial_prompt=None, auto=False, continue_mode=False, tui=False)
    assert mock_input.call_count == 1
    assert not mock_run_single.called
    assert agent._RESTART["requested"]
    assert len(saved) == 1   # the boundary checkpoint


def test_main_execs_when_requested(monkeypatch):
    execs = []
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "--no-tui"])
    monkeypatch.setattr(agent, "run_agent_interactive",
                        lambda **kw: agent._request_restart(by="command"))
    monkeypatch.setattr(agent, "_exec_restart", lambda: execs.append(True))
    monkeypatch.setattr(agent, "_maybe_first_run_wizard", lambda *a, **k: None, raising=False)
    try:
        agent.main()
    except SystemExit:
        pass
    assert execs == [True]
