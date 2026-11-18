"""Roles on the same server share one boot probe (llm_backend.recall_probe/remember_probe):
the second role's health() asks nothing of the server while the first answer is fresh, and
still checks its own model."""
import json
import os

import httpx
import pytest
import requests

import llm_backend as lb
from tests.test_aicommons_backend import BASE, Gateway, make  # noqa: F401 — the fixture


@pytest.fixture(autouse=True)
def _boot_window():
    with lb.boot_probes():
        yield


MODELS = {"data": [{"id": "claude-v5.5-sonnet", "type": "model", "created_at": "2026-01-01T00:00:00Z",
                    "display_name": "s"}, {"id": "claude-v5.5-haiku", "type": "model",
                    "created_at": "2026-01-01T00:00:00Z", "display_name": "h"}],
          "has_more": False, "first_id": None, "last_id": None}
QUOTA = {"total_tokens": 10, "limit": 1000, "remaining": 990, "reset_at": "2026-12-01"}


def test_aicommons_roles_on_one_gateway_probe_it_once(make):
    gw = Gateway(httpx.Response(200, json=MODELS), quota=QUOTA)
    main = make(gw, role="main", model="claude-v5.5-sonnet")
    summary = make(gw, role="summary", model="claude-v5.5-haiku")
    advisor = make(gw, role="advisor", model="claude-v9-opus")
    assert main.health() == (True, BASE)
    assert summary.health() == (True, BASE)
    ok, msg = advisor.health()
    assert not ok and "claude-v9-opus" in msg        # its own model, checked against the shared list
    assert len(gw.requests) == 1 and gw.quota_calls == 1
    assert summary.quota() == QUOTA and gw.quota_calls == 1


def test_a_different_key_is_a_different_server(make):
    gw = Gateway(httpx.Response(200, json=MODELS), httpx.Response(200, json=MODELS))
    make(gw, role="main", model="claude-v5.5-sonnet", api_key="sk-one").health()
    make(gw, role="summary", model="claude-v5.5-haiku", api_key="sk-two").health()
    assert len(gw.requests) == 2


def test_llamacpp_roles_on_one_server_probe_it_once(monkeypatch):
    calls = []

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "qwen"}]}

    monkeypatch.setattr(lb.requests, "get", lambda url, **kw: calls.append(url) or _R())
    a = lb.LlamacppBackend({"kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "qwen"})
    b = lb.LlamacppBackend({"kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "qwen"})
    c = lb.LlamacppBackend({"kind": "llamacpp", "base_url": "http://127.0.0.1:8081", "model": "qwen"})
    assert a.health() == (True, "ok") and b.health() == (True, "ok") and c.health() == (True, "ok")
    assert a.list_models() == ["qwen"] and b.list_models() == ["qwen"]
    assert [u.split("//")[1] for u in calls] == ["127.0.0.1:8080/health", "127.0.0.1:8081/health",
                                                 "127.0.0.1:8080/v1/models"]


def test_a_stale_probe_is_asked_again(monkeypatch):
    monkeypatch.setattr(lb, "_PROBE_TTL_S", 0.0)
    lb.remember_probe(("llamacpp", "http://x", ""), (True, "ok"))
    now = lb.time.monotonic()
    monkeypatch.setattr(lb.time, "monotonic", lambda: now + 1)
    assert lb.recall_probe(("llamacpp", "http://x", "")) is None


def test_advisor_on_mains_server_reuses_mains_probe(tmp_path, monkeypatch):
    from tools import consult_advisor as ca
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps(
        {"llm": {"base_url": "http://127.0.0.1:9", "model": "m"},
         "advisor": {"base_url": "http://127.0.0.1:9", "enabled": True}}))
    monkeypatch.setattr(ca.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed again")))
    lb.remember_probe(("llamacpp", "http://127.0.0.1:9", ""), (True, "ok"))
    assert ca.startup_check(timeout_s=1) == (True, "ok")
    lb._PROBES.clear()
    monkeypatch.setattr(ca.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError()))
    assert ca.startup_check(timeout_s=1) == (False, "unreachable")
    assert lb.recall_probe(("llamacpp", "http://127.0.0.1:9", "")) == (False, "unreachable")


class _Resp:
    def __init__(self, code):
        self.status_code = code

    def json(self):
        return {"data": [{"id": "qwen"}]}


def _llama(url="http://127.0.0.1:8080"):
    return lb.LlamacppBackend({"kind": "llamacpp", "base_url": url, "model": "qwen"})


def test_outside_boot_a_second_backend_probes_again(monkeypatch):
    """Failover and the setup doctor build new backends long after boot: down then up, and up then down."""
    codes = iter([500, 200, 200, 500])
    calls = []
    monkeypatch.setattr(lb.requests, "get", lambda url, **kw: calls.append(url) or _Resp(next(codes)))
    lb.forget_probes()                                   # window closed
    assert _llama().health()[0] is False
    assert _llama().health() == (True, "ok")             # server started since
    assert _llama().health() == (True, "ok")
    assert _llama().health()[0] is False                 # server died since
    assert len(calls) == 4
    lb.remember_probe(("llamacpp", "http://x", ""), (True, "ok"))
    assert lb.recall_probe(("llamacpp", "http://x", "")) is None


def test_the_window_is_closed_after_boot_even_on_an_exception():
    with pytest.raises(RuntimeError):
        with lb.boot_probes():
            lb.remember_probe(("llamacpp", "http://x", ""), (True, "ok"))
            raise RuntimeError("boot failed")
    assert lb.recall_probe(("llamacpp", "http://x", "")) is None
    assert not lb._PROBES


def test_probe_key_strips_the_slash_and_keeps_the_key():
    assert lb.probe_key("llamacpp", "http://h:1/") == lb.probe_key("llamacpp", "http://h:1")
    assert lb.probe_key("llamacpp", "http://h:1", "k") != lb.probe_key("llamacpp", "http://h:1")


def test_session_backends_checks_run_inside_the_window(monkeypatch):
    import agent
    seen = []
    monkeypatch.setattr(agent._Session, "_backend_checks", lambda self: seen.append(lb._boot_open))
    agent._Session.__new__(agent._Session)._backends()
    assert seen == [True] and lb._boot_open is False


def test_llamacpp_complete_always_sends_thinking_off(monkeypatch):
    """Finding: the judge's thinking-off switch needs no extra field; complete() already
    sends chat_template_kwargs.enable_thinking=False on every call."""
    sent = []

    class _R:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": " ok "}}]}

    monkeypatch.setattr(lb.requests, "post", lambda url, json=None, **kw: sent.append(json) or _R())
    assert _llama().complete(prompt="p", gen_params={"thinking": "off", "max_tokens": 200}) == "ok"
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False} and sent[0]["max_tokens"] == 200
    assert "thinking" not in sent[0]
