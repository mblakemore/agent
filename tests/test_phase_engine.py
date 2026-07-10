"""WS1 wave-2 tests: PhaseEngine profiles, gates, advancement, inertness."""

import logging

import pytest

from phase_engine import PhaseEngine, build_phase_engine, SIX_PHASES

log = logging.getLogger("test_phase_engine")


class TestFactory:
    def test_inert_without_config(self):
        assert build_phase_engine({}) is None
        assert build_phase_engine({"cycle": {"max_turns": 100}}) is None
        assert build_phase_engine(None) is None

    def test_profile_config(self):
        eng = build_phase_engine({"cycle": {"profile": "creature-6phase"}})
        assert eng is not None
        assert eng.names == SIX_PHASES

    def test_unknown_profile_inert_not_crash(self):
        assert build_phase_engine({"cycle": {"profile": "nope"}}, log) is None

    def test_custom_phases(self):
        eng = build_phase_engine(
            {"cycle": {"phases": [{"name": "plan"}, {"name": "act"}]}})
        assert eng.names == ["PLAN", "ACT"]


class TestGates:
    def _eng(self):
        return PhaseEngine(profile="creature-6phase", log=log)

    def test_file_write_blocked_in_perceive(self):
        eng = self._eng()
        ok, msg = eng.allow("file", {"action": "write", "path": "x.json"})
        assert not ok and "PERCEIVE" in msg
        ok, msg = eng.allow("write_file", {"path": "x.json"})
        assert not ok

    def test_file_read_allowed_in_perceive(self):
        eng = self._eng()
        ok, _ = eng.allow("file", {"action": "read", "path": "x.json"})
        assert ok
        ok, _ = eng.allow("exec_command", {"command": "git log --oneline -5"})
        assert ok

    def test_file_write_allowed_in_act(self):
        eng = self._eng()
        for p in ("PERCEIVE", "REFLECT", "DECIDE"):
            eng.observe("task_tracker", {"action": "done", "description": p})
        assert eng.current == "ACT"
        ok, _ = eng.allow("file", {"action": "write", "path": "out.py"})
        assert ok

    def test_end_cycle_only_in_persist(self):
        eng = self._eng()
        ok, msg = eng.allow("end_cycle", {})
        assert not ok and "PERSIST" in msg
        for p in SIX_PHASES[:-1]:
            eng.observe("task_tracker", {"action": "done", "description": p})
        assert eng.current == "PERSIST"
        ok, _ = eng.allow("end_cycle", {})
        assert ok

    def test_explicit_whitelist_tightens(self):
        eng = PhaseEngine(phases=[
            {"name": "PERCEIVE", "allowed_tools": ["read_file", "think"]},
            {"name": "ACT"},
        ])
        ok, msg = eng.allow("web_fetch", {"url": "http://x"})
        assert not ok and "read_file" in msg
        ok, _ = eng.allow("read_file", {"path": "a"})
        assert ok

    def test_observe_only_profile_allows_everything(self):
        eng = PhaseEngine(profile="cicd-8phase")
        assert eng.observe_only
        ok, _ = eng.allow("end_cycle", {})
        assert ok


class TestAdvancementAndVerifyGate:
    def _eng(self):
        return PhaseEngine(profile="creature-6phase", log=log)

    def test_advances_on_done_markers(self):
        eng = self._eng()
        eng.observe("task_tracker", {"action": "done",
                                     "description": "PERCEIVE environment"})
        assert eng.current == "REFLECT"
        assert eng.done == ["PERCEIVE"]

    def test_decide_entry_gate_fires_once_without_think(self):
        eng = self._eng()
        eng.observe("task_tracker", {"action": "done", "description": "PERCEIVE"})
        msg = eng.observe("task_tracker", {"action": "done", "description": "REFLECT"})
        assert msg is not None and "Verification gate" in msg
        # Second entry attempt (or continued work) is not re-blocked.
        ok, msg2 = eng.gate_decide_entry()
        assert ok and msg2 is None

    def test_decide_gate_silent_after_think(self):
        eng = self._eng()
        eng.observe("think", {"prompt": "check assumption"})
        eng.observe("task_tracker", {"action": "done", "description": "PERCEIVE"})
        msg = eng.observe("task_tracker", {"action": "done", "description": "REFLECT"})
        assert msg is None

    def test_out_of_order_marker_recorded_not_fought(self):
        eng = self._eng()
        eng.observe("task_tracker", {"action": "done", "description": "ACT"})
        assert eng.current == "PERCEIVE"  # no jump
        assert "ACT" in eng.done

    def test_unrelated_task_done_ignored(self):
        eng = self._eng()
        eng.observe("task_tracker", {"action": "done",
                                     "description": "fix the parser bug"})
        assert eng.current == "PERCEIVE"
        assert eng.done == []

    def test_persisted_latch(self):
        eng = self._eng()
        assert not eng.persisted
        eng.mark_persisted("git push")
        assert eng.persisted
        assert "persisted: yes" in eng.checkpoint_line()

    def test_checkpoint_line_shape(self):
        eng = self._eng()
        line = eng.checkpoint_line()
        assert "current=PERCEIVE" in line and "(1/6)" in line


# ------------------------------------------------ loop integration (wiring)

import json as _json
from unittest.mock import MagicMock, patch

import agent as _agent
from tools import MAP_FN as _MAP_FN


def _sse(lines):
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = [l.encode() for l in lines]
    resp.close = MagicMock()
    return resp


def _tool_resp(name, args, call_id="p1"):
    payload = {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": call_id,
        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}
    return _sse([f"data: {_json.dumps(payload)}", "data: [DONE]"])


def _text(content):
    return _sse([f'data: {{"choices": [{{"delta": {{"content": "{content}"}}}}]}}',
                 "data: [DONE]"])


class TestLoopWiring:
    def test_perceive_file_write_blocked_end_to_end(self, monkeypatch):
        """With cycle.profile set, a PERCEIVE-phase file write is blocked by
        the dispatch gate and the redirect lands in history."""
        monkeypatch.setitem(_agent._config, "cycle",
                            {**_agent._config["cycle"],
                             "profile": "creature-6phase"})
        mock_file = MagicMock(return_value="written")
        monkeypatch.setitem(_MAP_FN, "file", mock_file)
        history = [{"role": "user", "content": "Creature cycle. Begin."}]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = [
                _tool_resp("file", {"action": "write",
                                    "path": "state/current-state.json",
                                    "content": "{}"}),
                _text("Done"),
            ]
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        hist = "".join(str(m) for m in history)
        assert "blocked — you are in PERCEIVE" in hist
        mock_file.assert_not_called()
