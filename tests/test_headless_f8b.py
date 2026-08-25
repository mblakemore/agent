"""Headless hardening F8b — backend factory seam (FakeBackend) + the conftest live-connect guard."""
import json
import os
import socket
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
import agent as _agent  # noqa: E402
import llm_backend as _lb  # noqa: E402
from conftest import LiveEndpointGuardError  # noqa: E402


class TestFactorySeam:
    def test_kind_fake_and_env_both_build_the_fake(self, monkeypatch):
        assert isinstance(_lb.build_backend({"kind": "fake"}), _lb.FakeBackend)
        monkeypatch.setenv("AGENT_FAKE_BACKEND", "1")
        b = _lb.build_backend({"kind": "llamacpp", "model": "whatever"})
        assert isinstance(b, _lb.FakeBackend) and b.kind == "fake"

    def test_without_env_the_real_kind_is_built(self, monkeypatch):
        monkeypatch.delenv("AGENT_FAKE_BACKEND", raising=False)
        assert isinstance(_lb.build_backend({"kind": "llamacpp", "base_url": "http://127.0.0.1:1"}), _lb.LlamacppBackend)

    def test_fake_streams_text_and_tool_calls_in_the_llamacpp_shape(self):
        tc = {"index": 0, "id": "t1", "function": {"name": "read_file", "arguments": "{}"}}
        fb = _lb.FakeBackend(script=[{"tool_calls": [tc]}, "hello", RuntimeError("boom")])
        r1 = list(fb.stream_chat(MagicMock(), json={"messages": []}).iter_lines())
        assert json.loads(r1[0][6:])["choices"][0]["delta"]["tool_calls"][0]["id"] == "t1" and r1[-1] == b"data: [DONE]"
        r2 = list(fb.stream_chat(MagicMock(), json={}).iter_lines())
        assert json.loads(r2[0][6:])["choices"][0]["delta"]["content"] == "hello"
        with pytest.raises(RuntimeError):
            fb.stream_chat(MagicMock(), json={})
        assert list(fb.stream_chat(MagicMock(), json={}).iter_lines())[0] != b""   # default keeps serving
        assert len(fb.calls) == 4


class TestSeededRunsOnFake:
    def _run(self, script):
        fb = _lb.FakeBackend(script=list(script))
        history = [{"role": "user", "content": "do it"}]
        with patch("agent._main_backend", fb), patch("agent._emit"), patch("agent._NUDGE_ENABLED", False), \
             patch.dict("agent.MAP_FN", {"read_file": lambda **kw: "contents"}):
            _agent.run_agent_single(history, {"text": "", "up_to": 0}, [], MagicMock())
        return fb.calls, history

    def test_same_script_same_seed_byte_identical_requests(self):
        snap = dict(_agent._config["generation"])
        try:
            _agent._config["generation"]["seed"] = 7
            tc = {"index": 0, "id": "t1", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
            calls_a, hist_a = self._run([{"tool_calls": [tc]}, "final answer"])
            calls_b, hist_b = self._run([{"tool_calls": [tc]}, "final answer"])
            # main-loop requests only (they carry `tools`); side requests (summaries, helpers)
            # are timing-dependent and not what the seed governs
            main = lambda calls: [{k: v for k, v in c.items() if k != "messages"} for c in calls if c.get("tools") is not None]  # noqa: E731
            assert main(calls_a) and main(calls_a) == main(calls_b)
            assert all(c.get("seed") == 7 for c in main(calls_a))
            assert [m.get("content") for m in hist_a] == [m.get("content") for m in hist_b]
        finally:
            _agent._config["generation"].clear()
            _agent._config["generation"].update(snap)


class TestLiveGuard:
    def test_unmarked_connect_to_a_model_port_fails_loudly(self):
        s = socket.socket()
        try:
            with pytest.raises(LiveEndpointGuardError):
                s.connect(("127.0.0.1", 8080))
        finally:
            s.close()

    def test_non_model_port_is_not_guarded(self):
        s = socket.socket()
        s.settimeout(0.5)
        try:
            with pytest.raises((ConnectionRefusedError, OSError)) as ei:
                s.connect(("127.0.0.1", 1))
            assert not isinstance(ei.value, LiveEndpointGuardError)
        finally:
            s.close()

    @pytest.mark.live
    def test_marked_live_reaches_the_socket_layer(self):
        # Marked: the guard steps aside. We point at a closed model-class port variant on the
        # loopback (8788 with nothing listening) — refusal proves the connect was attempted.
        s = socket.socket()
        s.settimeout(0.5)
        try:
            with pytest.raises((ConnectionRefusedError, OSError, socket.timeout)) as ei:
                s.connect(("127.0.0.1", 8788))
            assert not isinstance(ei.value, LiveEndpointGuardError)
        finally:
            s.close()
