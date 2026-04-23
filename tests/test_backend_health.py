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
