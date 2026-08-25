"""Headless hardening F1 — wall-clock deadline (plan/headless-hardening.md).

Pure-function matrix for the escalation ladder, loop-level behaviour with a scripted backend
(deadline reached → tool calls refused with paired results, gates stand down, final result path,
exit code 10), the must-still-pass case (deadline unset = zero injections), and the flag's
config error path."""
import json
import logging
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402
from agent import _deadline_step, run_agent_single  # noqa: E402


# ── pure ladder ───────────────────────────────────────────────────────────────
class TestDeadlineStep:
    def test_unset_never_fires(self):
        fired = set()
        assert _deadline_step(999, 0, fired, [0.6, 0.8, 0.92]) == (None, False)
        assert fired == set()

    def test_fractions_fire_in_order_exactly_once(self):
        fired = set()
        msgs = []
        for t in (10, 59, 60, 61, 80, 81, 92, 93):
            m, hard = _deadline_step(t, 100, fired, [0.6, 0.8, 0.92])
            assert hard is False
            if m:
                msgs.append((t, m))
        assert [t for t, _ in msgs] == [60, 80, 92]
        assert "heads-up" in msgs[0][1] and "wrapping up" in msgs[1][1] and "STOP WORKING" in msgs[2][1]
        assert fired == {0.6, 0.8, 0.92}

    def test_late_tick_fires_only_the_highest_due_fraction(self):
        fired = set()
        m, hard = _deadline_step(85, 100, fired, [0.6, 0.8, 0.92])
        assert m and "wrapping up" in m and hard is False
        assert fired == {0.6, 0.8}          # 0.6 marked as skipped, not fired later

    def test_hard_stop_fires_once_then_stays_hard(self):
        fired = set()
        m1, h1 = _deadline_step(100, 100, fired, [0.6, 0.8, 0.92])
        m2, h2 = _deadline_step(130, 100, fired, [0.6, 0.8, 0.92])
        assert h1 is True and "DEADLINE REACHED" in m1
        assert h2 is True and m2 is None


# ── loop level ────────────────────────────────────────────────────────────────
def _resp(content=None, tool_calls=None):
    r = MagicMock()
    r.status_code = 200
    lines = []
    if tool_calls:
        for tc in tool_calls:
            lines.append(f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [tc]}}]})}".encode())
    elif content:
        lines.append(f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}".encode())
    lines.append(b"data: [DONE]")
    r.iter_lines.return_value = lines
    return r


_TOOL = {"index": 0, "id": "tc1", "function": {"name": "exec_command", "arguments": '{"command": "echo hi"}'}}


@pytest.fixture
def cycle_cfg():
    snap = dict(_agent._config["cycle"])
    yield _agent._config["cycle"]
    _agent._config["cycle"].clear()
    _agent._config["cycle"].update(snap)
    _agent._LAST_EXIT = None


class TestDeadlineLoop:
    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_deadline_reached_refuses_tools_and_forces_final_result(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["deadline_s"] = 0.001                      # already past on turn 1
        mock_llm.side_effect = [_resp(tool_calls=[_TOOL]), _resp(content="FINAL: did X, not Y, artifacts in out/")]
        ran = []
        history = [{"role": "user", "content": "do the thing"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: ran.append(kw) or "exit=0"}):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done"
        assert ran == [], "no tool may run past the deadline"
        refused = [m for m in history if m.get("role") == "tool" and "REFUSED" in str(m.get("content"))]
        assert len(refused) == 1 and refused[0]["tool_call_id"] == "tc1"
        assert _agent._LAST_EXIT and _agent._LAST_EXIT["code"] == _agent.EXIT_DEADLINE
        sent = mock_llm.call_args_list[0].kwargs["json"]["messages"]
        assert any("DEADLINE REACHED" in str(m.get("content")) for m in sent), "hard-stop injection was sent to the model"
        assert mock_llm.call_count == 2

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_two_post_deadline_tool_turns_end_the_run(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["deadline_s"] = 0.001
        mock_llm.side_effect = [_resp(tool_calls=[_TOOL])] * 5
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: "exit=0"}):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done"
        assert mock_llm.call_count == 2, "bounded: two refused turns, then the run ends"
        assert _agent._LAST_EXIT["code"] == _agent.EXIT_DEADLINE

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_deadline_unset_is_todays_behaviour(self, mock_llm, _emit, cycle_cfg):
        cycle_cfg["deadline_s"] = 0
        mock_llm.side_effect = [_resp(tool_calls=[_TOOL]), _resp(content="done, tests pass")]
        ran = []
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: ran.append(kw) or "exit=0"}):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done" and len(ran) == 1
        for call in mock_llm.call_args_list:
            for m in call.kwargs["json"]["messages"]:
                assert "wall-clock budget" not in str(m.get("content")) and "DEADLINE" not in str(m.get("content"))
        assert not _agent._LAST_EXIT or _agent._LAST_EXIT["code"] != _agent.EXIT_DEADLINE


# ── flag ──────────────────────────────────────────────────────────────────────
def test_negative_deadline_is_a_config_error_exit_14(tmp_path):
    p = subprocess.run([sys.executable, os.path.join(_REPO, "agent.py"), "-a", "--deadline", "-5", "hello"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert p.returncode == _agent.EXIT_CONFIG, p.stderr[-600:]
    assert "AGENT-EXIT: config" in p.stderr
