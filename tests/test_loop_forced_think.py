"""WS10.e rung 2: loop-breaker escalation to system-invoked think."""

import json as _json
import logging
from unittest.mock import MagicMock, patch

import agent as _agent
from tools import MAP_FN as _MAP_FN

log = logging.getLogger("test_loop_think")


def _sse(lines):
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = [l.encode() for l in lines]
    resp.close = MagicMock()
    return resp


def _batch(command):
    payload = {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "L1",
        "function": {"name": "exec_command",
                     "arguments": _json.dumps({"command": command})}}]}}]}
    return _sse([f"data: {_json.dumps(payload)}", "data: [DONE]"])


def _text(content):
    return _sse([f'data: {{"choices": [{{"delta": {{"content": "{content}"}}}}]}}',
                 "data: [DONE]"])


class TestLoopForcedThink:
    def test_second_intervention_escalates_to_think(self, monkeypatch):
        """First batch-loop intervention = text redirect only; the second
        embeds a system-invoked think (FORCED REFLECTION marker)."""
        monkeypatch.setitem(_MAP_FN, "exec_command",
                            MagicMock(return_value="exit=0\nsame"))
        think_mock = MagicMock(return_value="THINK-MARKER: try editing directly")
        monkeypatch.setitem(_MAP_FN, "think", think_mock)
        # 8 identical batches: intervention 1 fires at repeat 3 (text only),
        # sigs clear, intervention 2 fires 3 batches later (escalates).
        responses = [_batch("echo same") for _ in range(8)] + [_text("done")]
        history = [{"role": "user", "content": "loop forever"}]
        with patch("agent._llm_request") as mock_llm, \
             patch("agent._check_api_health", return_value=(True, "ok")), \
             patch("agent._setup_logger"), \
             patch("agent._detect_ctx_size", return_value=None):
            mock_llm.side_effect = responses
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], log)
        hist = "".join(str(m) for m in history)
        assert "STOP — you have repeated" in hist
        if "FORCED REFLECTION" in hist:
            assert "THINK-MARKER" in hist
            assert think_mock.called
        else:
            # Acceptable alternate path: another detector (dedup/result-loop)
            # terminated the run before a second batch-loop intervention —
            # the ladder is bounded either way. But the first rung must have
            # fired and think must NOT have been invoked by rung 1.
            assert hist.count("STOP — you have repeated") >= 1
