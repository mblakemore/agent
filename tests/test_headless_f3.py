"""Headless hardening F3 — tool-output spill (stage 2), inbound-bulk spill (F3a), server-count
context calibration (F3b), and the transport's overflow-vs-transient 500 classification."""
import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402
import llm_backend as _lb  # noqa: E402
from agent import (_SPILL_MARKER, _calibrated_budget, _spill_tool_result,  # noqa: E402
                   _update_token_calibration, run_agent_single)


# ── spill helper ──────────────────────────────────────────────────────────────
class TestSpillHelper:
    def test_spills_to_file_and_returns_reference_with_excerpt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        big = "HEAD-" + ("x" * 30000) + "-TAIL\nline2\nline3"
        ref = _spill_tool_result(7, "exec_command", big, logging.getLogger("t"))
        assert ref.startswith(_SPILL_MARKER)
        path = ref[len(_SPILL_MARKER):].split("]")[0]
        assert os.path.isfile(path) and open(path, encoding="utf-8").read() == big
        assert f"{len(big)} chars" in ref and "3 lines" in ref
        assert "HEAD-" in ref and "-TAIL" in ref and "do NOT re-request" in ref
        assert len(ref) < 1200

    def test_write_failure_returns_none_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_agent, "_SPILL_DIR", str(tmp_path / "not-a-dir.txt"))
        (tmp_path / "not-a-dir.txt").write_text("a file where the dir must be")
        assert _spill_tool_result(1, "t", "x" * 100, logging.getLogger("t")) is None


# ── tool-site spill, loop level ───────────────────────────────────────────────
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


_TOOL = {"index": 0, "id": "tc1", "function": {"name": "exec_command", "arguments": '{"command": "cat big"}'}}


@pytest.fixture
def limits_cfg():
    snap = json.loads(json.dumps(_agent._config.get("limits") or {}))
    _agent._config.setdefault("limits", {})
    yield _agent._config["limits"]
    _agent._config["limits"].clear()
    _agent._config["limits"].update(snap)


class TestToolSiteSpill:
    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_oversized_result_is_spilled_not_truncated(self, mock_llm, _emit, limits_cfg, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        limits_cfg["tool_result_max_chars"] = 5000
        payload = "BEGIN " + ("data,row\n" * 3000) + " END"
        mock_llm.side_effect = [_resp(tool_calls=[_TOOL]), _resp(content="read the file, done")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: payload}):
            result = run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        assert result == "done"
        tool_msgs = [m for m in history if m.get("role") == "tool"]
        assert len(tool_msgs) == 1 and tool_msgs[0]["content"].startswith(_SPILL_MARKER)
        assert "chars truncated" not in tool_msgs[0]["content"]
        path = tool_msgs[0]["content"][len(_SPILL_MARKER):].split("]")[0]
        assert open(path, encoding="utf-8").read() == payload, "the full payload survives on disk"

    @patch("agent._emit")
    @patch("agent._llm_request")
    def test_under_cap_result_untouched(self, mock_llm, _emit, limits_cfg, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        limits_cfg["tool_result_max_chars"] = 5000
        mock_llm.side_effect = [_resp(tool_calls=[_TOOL]), _resp(content="done")]
        history = [{"role": "user", "content": "x"}]
        with patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"exec_command": lambda **kw: "small output"}):
            run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        tool_msgs = [m for m in history if m.get("role") == "tool"]
        assert tool_msgs[0]["content"] == "small output"

    def test_cap_zero_keeps_legacy_truncation_only(self, limits_cfg):
        limits_cfg["tool_result_max_chars"] = 0
        assert int(_agent._config["limits"]["tool_result_max_chars"]) == 0   # documented off switch


# ── F3b calibration ───────────────────────────────────────────────────────────
class TestCalibration:
    def test_first_measurement_replaces_the_heuristic_and_scales_the_budget(self):
        cal = {"ratio": 1.0, "n": 0, "last_logged": 1.0}
        assert _calibrated_budget(10000, cal) == 10000          # unmeasured: heuristic as-is
        r = _update_token_calibration(1000, 2000, cal=cal)      # server counts 2x what we estimated
        assert r == 2.0 and cal["n"] == 1
        assert _calibrated_budget(10000, cal) == 5000            # spend half the estimator-tokens

    def test_ema_smooths_and_clamps(self):
        cal = {"ratio": 1.0, "n": 0, "last_logged": 1.0}
        _update_token_calibration(1000, 1000, cal=cal)
        _update_token_calibration(1000, 1300, cal=cal)
        assert 1.0 < cal["ratio"] < 1.3
        for _ in range(20):
            _update_token_calibration(1000, 100000, cal=cal)     # absurd: ignored, ratio stays put
        assert cal["ratio"] < 1.3

    def test_zero_and_garbage_are_ignored(self):
        cal = {"ratio": 1.5, "n": 3, "last_logged": 1.5}
        assert _update_token_calibration(0, 100, cal=cal) == 1.5
        assert _update_token_calibration("x", None, cal=cal) == 1.5
        assert cal["n"] == 3

    def test_logs_when_ratio_moves_more_than_ten_percent(self, caplog):
        cal = {"ratio": 1.0, "n": 0, "last_logged": 1.0}
        log = logging.getLogger("cal-test")
        with caplog.at_level(logging.INFO, logger="cal-test"):
            _update_token_calibration(1000, 1500, log=log, cal=cal)
            _update_token_calibration(1000, 1520, log=log, cal=cal)   # ~1% move: quiet
        assert sum("token calibration" in r.getMessage() for r in caplog.records) == 1


# ── transport: overflow verdict vs transient 500 ──────────────────────────────
class TestTransport500:
    @pytest.mark.parametrize("body,expected", [
        ("the request exceeds the available context size", True),
        ('{"error":{"message":"prompt is too long: 210000 tokens > n_ctx"}}', True),
        ("context shift is disabled", True),
        ("internal server error: model reloading", False),
        ("", False),
    ])
    def test_overflow_detector(self, body, expected):
        assert _lb._looks_like_context_overflow(body) is expected

    def _backend(self):
        b = _lb.LlamacppBackend.__new__(_lb.LlamacppBackend)
        b.base_url = "http://127.0.0.1:1"
        b.model = "m"
        b.kind = "llamacpp"
        b.stream_enabled = True
        b._retry_cfg = {**_lb._DEFAULT_RETRY_CFG, "max_retries": 4, "base_delay_seconds": 0, "jitter_factor": 0}
        b._auth_headers = lambda: {}
        return b

    def test_bodied_overflow_500_raises_at_once(self, monkeypatch):
        calls = []

        def fake_post(*a, **k):
            calls.append(1)
            r = MagicMock(); r.status_code = 500; r.text = "the request exceeds the available context size"
            return r
        monkeypatch.setattr(_lb.requests, "post", fake_post)
        with pytest.raises(_lb.ContextOverflowError):
            self._backend().stream_chat(logging.getLogger("t"), json={"messages": []})
        assert len(calls) == 1, "no three wasted round trips"

    def test_transient_500s_are_retried_then_succeed_without_an_overflow_verdict(self, monkeypatch):
        seq = iter([500, 500, 500, 200])
        calls = []

        def fake_post(*a, **k):
            code = next(seq); calls.append(code)
            r = MagicMock(); r.status_code = code; r.text = "server busy, model reloading"
            r.raise_for_status = lambda: None
            return r
        monkeypatch.setattr(_lb.requests, "post", fake_post)
        monkeypatch.setattr(_lb.time, "sleep", lambda s: None)
        resp = self._backend().stream_chat(logging.getLogger("t"), json={"messages": []})
        assert resp.status_code == 200 and calls == [500, 500, 500, 200]

    def test_empty_body_500s_keep_the_legacy_three_strikes(self, monkeypatch):
        def fake_post(*a, **k):
            r = MagicMock(); r.status_code = 500; r.text = ""
            return r
        monkeypatch.setattr(_lb.requests, "post", fake_post)
        monkeypatch.setattr(_lb.time, "sleep", lambda s: None)
        with pytest.raises(_lb.ContextOverflowError):
            self._backend().stream_chat(logging.getLogger("t"), json={"messages": []})
