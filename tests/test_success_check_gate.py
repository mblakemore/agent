"""WS10.c tests: end_cycle success-check gate (wave 3)."""

import json as _json
import logging
from unittest.mock import MagicMock, patch

import pytest

import agent as _agent
from tools import MAP_FN as _MAP_FN

log = logging.getLogger("test_success_check")


def _sse(lines):
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = [l.encode() for l in lines]
    resp.close = MagicMock()
    return resp


def _tool_resp(name, args, call_id="s1"):
    payload = {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": call_id,
        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}
    return _sse([f"data: {_json.dumps(payload)}", "data: [DONE]"])


def _text(content):
    return _sse([f'data: {{"choices": [{{"delta": {{"content": "{content}"}}}}]}}',
                 "data: [DONE]"])


def _run(monkeypatch, success_check, llm_side_effects):
    monkeypatch.setitem(_agent._config, "cycle",
                        {**_agent._config["cycle"],
                         "success_check": success_check})
    history = [{"role": "user", "content": "Do the work."}]
    with patch("agent._llm_request") as mock_llm, \
         patch("agent._check_api_health", return_value=(True, "ok")), \
         patch("agent._setup_logger"), \
         patch("agent._detect_ctx_size", return_value=None):
        # Pad with text turns: the gate now blocks text-only exits too, so
        # scripts need enough responses for the 3-block cap to play out.
        mock_llm.side_effect = list(llm_side_effects) + [
            _text(f"continuing {i}") for i in range(6)]
        _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
    return history


class TestSuccessCheckGate:
    def test_failing_check_blocks_end_cycle(self, monkeypatch):
        history = _run(monkeypatch, "false", [
            _tool_resp("end_cycle", {"summary": "done"}),
            _text("ok"),
        ])
        hist = "".join(str(m) for m in history)
        assert "end_cycle blocked" in hist
        assert "success check still FAILS" in hist

    def test_passing_check_allows_end_cycle(self, monkeypatch):
        history = _run(monkeypatch, "true", [
            _tool_resp("end_cycle", {"summary": "done"}),
            _text("ok"),
        ])
        hist = "".join(str(m) for m in history)
        assert "end_cycle blocked" not in hist

    def test_repeated_blocking_is_bounded(self, monkeypatch):
        # Repeated end_cycle against a permanently failing check cannot loop
        # forever: the gate blocks at most _SUCCESS_CHECK_MAX_BLOCKS times,
        # and in practice the RESULT-LOOP detector (3 identical tool results)
        # ends the cycle even sooner — the detectors compose. Contract: >=2
        # blocks observed, <=3 ever, and the run terminated.
        history = _run(monkeypatch, "false", [
            _tool_resp("end_cycle", {"summary": "1"}, "a"),
            _tool_resp("end_cycle", {"summary": "2"}, "b"),
            _tool_resp("end_cycle", {"summary": "3"}, "c"),
            _tool_resp("end_cycle", {"summary": "4"}, "d"),
            _text("ok"),
        ])
        hist = "".join(str(m) for m in history)
        assert 2 <= hist.count("end_cycle blocked") <= 3

    def test_no_config_no_gate(self, monkeypatch):
        history = _run(monkeypatch, None, [
            _tool_resp("end_cycle", {"summary": "done"}),
            _text("ok"),
        ])
        hist = "".join(str(m) for m in history)
        assert "end_cycle blocked" not in hist


class TestCompletionPathGate:
    """WS10.c sibling-path fix: replay/creature runs end via the TEXT
    completion path, not end_cycle (telemetry: zero end_cycle events across
    all replay sessions) — the gate must guard both exits."""

    def _run_completion(self, monkeypatch, success_check):
        monkeypatch.setitem(_agent._config, "cycle",
                            {**_agent._config["cycle"],
                             "success_check": success_check})
        monkeypatch.setitem(_MAP_FN, "exec_command",
                            MagicMock(return_value="exit=0\ncommitted"))
        history = [{"role": "user", "content": "Do the work."}]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = [
                _tool_resp("exec_command",
                           {"command": "git commit -m done"}, "c1"),
                _text("Cycle complete."),
                _text("Cycle complete."),
                _text("Cycle complete once more."),
                _text("Cycle complete again."),
                _text("Cycle complete final."),
            ]
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        return "".join(str(m) for m in history)

    def test_completion_phrase_blocked_while_check_fails(self, monkeypatch):
        hist = self._run_completion(monkeypatch, "false")
        assert "ending the cycle" in hist
        assert "still FAILS" in hist

    def test_completion_phrase_allowed_when_check_passes(self, monkeypatch):
        hist = self._run_completion(monkeypatch, "true")
        assert "still FAILS" not in hist


class TestAdvisorEscalationWiring:
    """Spike: the success-gate failure counter drives escalation_policy, which
    suggests the heavyweight advisor tier after repeated blocks. Default-off:
    absent/disabled ``advisor`` config = no suggestion, no behavior change."""

    def _run_adv(self, monkeypatch, advisor_cfg, side_effects):
        monkeypatch.setitem(_agent._config, "cycle",
                            {**_agent._config["cycle"], "success_check": "false"})
        if advisor_cfg is not None:
            monkeypatch.setitem(_agent._config, "advisor", advisor_cfg)
        history = [{"role": "user", "content": "Do the work."}]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = list(side_effects) + [
                _text(f"continuing {i}") for i in range(6)]
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        return "".join(str(m) for m in history)

    def test_escalation_suggested_after_repeated_block(self, monkeypatch):
        # 2nd block → consecutive_gate_failures==2 → policy escalates (advisor).
        hist = self._run_adv(monkeypatch, {"enabled": True}, [
            _tool_resp("end_cycle", {"summary": "1"}, "a"),
            _tool_resp("end_cycle", {"summary": "2"}, "b"),
            _tool_resp("end_cycle", {"summary": "3"}, "c"),
            _text("ok"),
        ])
        assert "consult_advisor" in hist
        assert "escalation available" in hist
        # Suggested at most once — not on every subsequent block.
        assert hist.count("escalation available") == 1

    def test_no_escalation_when_disabled_by_default(self, monkeypatch):
        # No advisor key at all (default) → hook inert.
        hist = self._run_adv(monkeypatch, None, [
            _tool_resp("end_cycle", {"summary": "1"}, "a"),
            _tool_resp("end_cycle", {"summary": "2"}, "b"),
            _tool_resp("end_cycle", {"summary": "3"}, "c"),
            _text("ok"),
        ])
        assert "consult_advisor" not in hist
        # The base gate still worked — this isn't a silently-broken run.
        assert "still FAILS" in hist

    def test_no_escalation_when_enabled_false(self, monkeypatch):
        hist = self._run_adv(monkeypatch, {"enabled": False}, [
            _tool_resp("end_cycle", {"summary": "1"}, "a"),
            _tool_resp("end_cycle", {"summary": "2"}, "b"),
            _text("ok"),
        ])
        assert "consult_advisor" not in hist

    # Stall/grind AUTO-INVOKE consult_advisor (not suggest) — mock it so the
    # tests are hermetic (no real glm call) and assert the advice is injected.
    _ADVICE = "CANNED_ADVICE_APPLY_FIX"

    # --- stall path (the grind/loop mode the gate-block trigger misses) -------
    def _run_stall(self, monkeypatch, tmp_path, advisor_cfg):
        """Drive a semantic result-loop (read_file returns the same content
        repeatedly) so loop_interventions>=2 fires the stuck-intervention
        ladder. `think` + `consult_advisor` are mocked (hermetic + fast)."""
        if advisor_cfg is not None:
            monkeypatch.setitem(_agent._config, "advisor", advisor_cfg)
        monkeypatch.setitem(_MAP_FN, "think", MagicMock(return_value="(reflect)"))
        monkeypatch.setitem(_MAP_FN, "consult_advisor",
                            MagicMock(return_value=self._ADVICE))
        f = tmp_path / "loopfile.txt"
        f.write_text("stable content that repeats\nline two\n")
        monkeypatch.chdir(tmp_path)
        history = [{"role": "user", "content": "read the file"}]
        reads = [_tool_resp("read_file", {"path": str(f)}, f"r{i}")
                 for i in range(7)]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = reads + [_text(f"done {i}") for i in range(6)]
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        return "".join(str(m) for m in history)

    def test_stall_auto_invokes_advisor_on_result_loop(self, monkeypatch, tmp_path):
        hist = self._run_stall(monkeypatch, tmp_path, {"enabled": True})
        assert "heavyweight advisor was consulted" in hist  # auto-invoked, not suggested
        assert self._ADVICE in hist                          # its advice injected

    def test_stall_no_invoke_when_advisor_disabled(self, monkeypatch, tmp_path):
        hist = self._run_stall(monkeypatch, tmp_path, None)  # no advisor block
        assert self._ADVICE not in hist

    # --- grind path (turn-budget spent, check still red — no other detector) --
    def _run_grind(self, monkeypatch, success_check, advisor_cfg):
        """Model takes DISTINCT actions (no loop) until it burns most of a small
        turn budget; grind trigger runs success_check once at 70% and, if red,
        auto-invokes the advisor."""
        monkeypatch.setitem(_agent._config, "cycle",
                            {**_agent._config["cycle"],
                             "success_check": success_check})
        monkeypatch.setattr(_agent, "_MAX_TURNS", 5)  # threshold int(0.7*5)=3
        if advisor_cfg is not None:
            monkeypatch.setitem(_agent._config, "advisor", advisor_cfg)
        monkeypatch.setitem(_MAP_FN, "consult_advisor",
                            MagicMock(return_value=self._ADVICE))
        monkeypatch.setitem(_MAP_FN, "exec_command",
                            MagicMock(side_effect=[f"distinct {i}" for i in range(10)]))
        history = [{"role": "user", "content": "work on it"}]
        calls = [_tool_resp("exec_command", {"command": f"echo {i}"}, f"c{i}")
                 for i in range(6)]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = calls + [_text(f"done {i}") for i in range(6)]
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        return "".join(str(m) for m in history)

    def test_grind_auto_invokes_advisor_when_check_stays_red(self, monkeypatch):
        hist = self._run_grind(monkeypatch, "false", {"enabled": True})
        assert "heavyweight advisor was consulted" in hist
        assert self._ADVICE in hist

    def test_grind_no_invoke_when_check_passes(self, monkeypatch):
        # Converging (check green at the threshold) → no escalation, no advisor.
        hist = self._run_grind(monkeypatch, "true", {"enabled": True})
        assert self._ADVICE not in hist

    def test_grind_no_invoke_when_advisor_disabled(self, monkeypatch):
        hist = self._run_grind(monkeypatch, "false", None)  # no advisor block
        assert self._ADVICE not in hist

    def test_grind_fires_on_elapsed_time(self, monkeypatch):
        # Slow-model case: turn budget huge so the TURN trigger never hits, but
        # grind_elapsed_s (tiny) fires the elapsed trigger with the check red.
        monkeypatch.setitem(_agent._config, "cycle",
                            {**_agent._config["cycle"], "success_check": "false",
                             "grind_elapsed_s": 0.01})
        monkeypatch.setattr(_agent, "_MAX_TURNS", 100)  # turn trigger won't hit
        monkeypatch.setitem(_agent._config, "advisor", {"enabled": True})
        monkeypatch.setitem(_MAP_FN, "consult_advisor",
                            MagicMock(return_value=self._ADVICE))
        monkeypatch.setitem(_MAP_FN, "exec_command",
                            MagicMock(side_effect=[f"d {i}" for i in range(12)]))
        history = [{"role": "user", "content": "work"}]
        calls = [_tool_resp("exec_command", {"command": f"echo {i}"}, f"c{i}")
                 for i in range(6)]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = calls + [_text(f"done {i}") for i in range(6)]
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        hist = "".join(str(m) for m in history)
        assert self._ADVICE in hist  # advisor auto-invoked via the elapsed path
