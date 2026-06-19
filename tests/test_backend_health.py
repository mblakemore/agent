"""Backend health-probe test (plan task 1.5).

Small regression guard: LlamacppBackend.health() returns (True, 'ok') on 200
and (False, detail) on non-200.
"""

from unittest.mock import patch, MagicMock

from llm_backend import LlamacppBackend


def test_backend_health_ok():
    b = LlamacppBackend({"base_url": "http://x", "model": "m"})
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200)
        ok, detail = b.health()
    assert ok is True
    assert detail == "ok"


def test_backend_health_503():
    b = LlamacppBackend({"base_url": "http://x", "model": "m"})
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=503)
        ok, detail = b.health()
    assert ok is False
    assert "503" in detail


def test_health_timeout_threads_through_to_request():
    """A custom timeout passed to LlamacppBackend.health() reaches requests.get
    (so the startup probe can use a longer timeout for cold-start endpoints)."""
    b = LlamacppBackend({"base_url": "http://x", "model": "m"})
    with patch("llm_backend.requests.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200)
        b.health(timeout=17)
    assert mock_get.call_args.kwargs.get("timeout") == 17


def test_all_backends_health_accept_timeout():
    """Uniform signature: every backend's health() accepts a timeout kwarg, so
    agent startup can pass one without knowing the concrete backend type."""
    import inspect
    import llm_backend as L
    for name in ("LlamacppBackend", "BedrockBackend"):
        cls = getattr(L, name)
        assert "timeout" in inspect.signature(cls.health).parameters, name


def test_startup_health_timeout_env_and_default(monkeypatch):
    import agent
    monkeypatch.delenv("AGENT_HEALTH_TIMEOUT", raising=False)
    assert agent._startup_health_timeout() == 10           # generous default
    monkeypatch.setenv("AGENT_HEALTH_TIMEOUT", "30")
    assert agent._startup_health_timeout() == 30           # override
    monkeypatch.setenv("AGENT_HEALTH_TIMEOUT", "not-a-number")
    assert agent._startup_health_timeout() == 10           # bad value -> default
