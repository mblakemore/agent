import pytest
import logging
from unittest.mock import MagicMock, patch
import requests
import agent
import llm_backend

# Mock a Bedrock-like backend that fails
class FailingBedrock:
    kind = 'bedrock'
    model = 'claude-v4.5-haiku'
    base_url = 'https://example/api'
    def stream_chat(self, log, **kw):
        raise TimeoutError('No response after 180s')
    def complete(self, **kw):
        raise TimeoutError('No response after 180s')
    def health(self): return True, 'ok'
    def detect_ctx_size(self): return None
    def list_models(self): return []

# Mock a working llamacpp backend
class WorkingLlama:
    kind = 'llamacpp'
    model = 'gemma-4-31B'
    base_url = 'http://127.0.0.1:8080'
    def stream_chat(self, log, **kw):
        return ["chunk1", "chunk2"]
    def complete(self, **kw):
        return "Summary result"
    def health(self): return True, 'ok'
    def detect_ctx_size(self): return 8192
    def list_models(self): return []

def test_main_failover_timeout():
    """Bedrock main TimeoutError -> failover to llamacpp main."""
    orig_main = agent._main_backend
    try:
        agent._main_backend = FailingBedrock()
        # Mock that llamacpp is available
        with patch('agent._build_backend', return_value=WorkingLlama()):
            # This should trigger failover (once implemented)
            try:
                res = agent._llm_request(logging.getLogger('test'), prompt="hi")
                assert agent._main_backend.kind == 'llamacpp'
            except Exception as e:
                pytest.fail(f"Main failover failed: {e}")
    finally:
        agent._main_backend = orig_main

def test_summary_failover_timeout():
    """Bedrock summary TimeoutError -> failover to llamacpp summary."""
    orig_summary = agent._summary_backend
    orig_summary_config = agent._config.get("summary")
    try:
        agent._summary_backend = FailingBedrock()
        # Setup config to enable summary and provide url
        agent._config["summary"] = {"enabled": True, "base_url": "http://summary-api"}
        with patch('agent._build_backend', return_value=WorkingLlama()):
            # We must call _generate_summary to trigger the failover logic
            try:
                res = agent._generate_summary("old_summary", [], logging.getLogger('test'))
                assert agent._summary_backend.kind == 'llamacpp'
            except Exception as e:
                pytest.fail(f"Summary failover failed: {e}")
    finally:
        agent._summary_backend = orig_summary
        if orig_summary_config is not None:
            agent._config["summary"] = orig_summary_config

def test_budget_exceeded_failover():
    """BedrockBudgetExceeded -> failover to llamacpp."""
    class BudgetExceededBedrock(FailingBedrock):
        def stream_chat(self, log, **kw):
            # Simulate the specific exception mentioned in the issue
            raise Exception("BedrockBudgetExceeded")

    orig_main = agent._main_backend
    try:
        agent._main_backend = BudgetExceededBedrock()
        with patch('agent._build_backend', return_value=WorkingLlama()):
            try:
                agent._llm_request(logging.getLogger('test'), prompt="hi")
                assert agent._main_backend.kind == 'llamacpp'
            except Exception as e:
                pytest.fail(f"Budget failover failed: {e}")
    finally:
        agent._main_backend = orig_main

def _cfg(main=None, summary=None):
    """Minimal agent._config replacement for _trigger_failover tests."""
    return {
        "backends": {"main": main or {}, "summary": summary or {}},
        "llm": {"model": "orig-model", "base_url": "http://127.0.0.1:8080"},
    }

def test_fallback_carries_failing_role_not_donor_role():
    """The fallback borrows the OTHER role's endpoint config but must keep
    role=<failing role> — otherwise main traffic runs under the summary
    role's daily cost cap and telemetry label (cap evasion on budget trips)."""
    seen = {}
    def capture(cfg):
        seen.update(cfg)
        return WorkingLlama()
    orig_main = agent._main_backend
    try:
        agent._main_backend = FailingBedrock()
        cfg = _cfg(main={"kind": "bedrock", "model": "claude-v4.6-opus"},
                   summary={"kind": "llamacpp", "base_url": "http://cpu:8081",
                            "model": "gemma-4-31B"})
        with patch.object(agent, '_config', cfg), \
             patch('agent._build_backend', side_effect=capture):
            assert agent._trigger_failover(logging.getLogger('test'), 'main')
        assert seen["kind"] == "llamacpp"
        assert seen["base_url"] == "http://cpu:8081"
        assert seen["role"] == "main"
    finally:
        agent._main_backend = orig_main

def test_main_failover_syncs_tui_model():
    """After a main swap the status bar must show the live model, same
    contract as _apply_backend_overrides."""
    orig_main = agent._main_backend
    try:
        agent._main_backend = FailingBedrock()
        cfg = _cfg(main={"kind": "bedrock", "model": "claude-v4.6-opus"},
                   summary={"kind": "llamacpp", "base_url": "http://cpu:8081"})
        with patch.object(agent, '_config', cfg), \
             patch('agent._build_backend', return_value=WorkingLlama()):
            assert agent._trigger_failover(logging.getLogger('test'), 'main')
        assert cfg["llm"]["model"] == "gemma-4-31B"
    finally:
        agent._main_backend = orig_main

def test_same_endpoint_failover_refused():
    """Default single-server setups synthesize main and summary from the
    same llm block — 'failing over' would rebuild the identical backend and
    honestly report a swap that changed nothing."""
    orig_main = agent._main_backend
    try:
        agent._main_backend = WorkingLlama()  # identity matches summary cfg
        same = {"kind": "llamacpp", "base_url": "http://127.0.0.1:8080",
                "model": "gemma-4-31B"}
        with patch.object(agent, '_config', _cfg(main=dict(same), summary=dict(same))), \
             patch('agent._build_backend') as mock_build:
            assert not agent._trigger_failover(logging.getLogger('test'), 'main')
            mock_build.assert_not_called()
    finally:
        agent._main_backend = orig_main

def test_budget_trip_refuses_bedrock_fallback():
    """A real BedrockBudgetExceeded must not 'fail over' to another bedrock
    config — that either re-trips the cap (same role) or evades it (other
    role's cap). Degrade instead (caller falls back / raises)."""
    orig_main = agent._main_backend
    try:
        agent._main_backend = FailingBedrock()
        cfg = _cfg(main={"kind": "bedrock", "model": "claude-v4.6-opus"},
                   summary={"kind": "bedrock", "model": "claude-v4.5-haiku"})
        with patch.object(agent, '_config', cfg), \
             patch('agent._build_backend') as mock_build:
            ok = agent._trigger_failover(
                logging.getLogger('test'), 'main',
                cause=llm_backend.BedrockBudgetExceeded("cap"))
            assert not ok
            mock_build.assert_not_called()
    finally:
        agent._main_backend = orig_main

def test_llamacpp_down_failover():
    """Llamacpp also down -> original Bedrock exception re-raised."""
    orig_main = agent._main_backend
    try:
        agent._main_backend = FailingBedrock()
        class DeadLlama:
            kind = 'llamacpp'
            def health(self): return False, 'down'
            def stream_chat(self, log, **kw): pass

        with patch('agent._build_backend', return_value=DeadLlama()):
            with pytest.raises(TimeoutError):
                agent._llm_request(logging.getLogger('test'), prompt="hi")
    finally:
        agent._main_backend = orig_main
