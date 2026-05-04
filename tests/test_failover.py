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
        with patch('llm_backend.build_backend', return_value=WorkingLlama()):
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
        with patch('llm_backend.build_backend', return_value=WorkingLlama()):
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
        with patch('llm_backend.build_backend', return_value=WorkingLlama()):
            try:
                agent._llm_request(logging.getLogger('test'), prompt="hi")
                assert agent._main_backend.kind == 'llamacpp'
            except Exception as e:
                pytest.fail(f"Budget failover failed: {e}")
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

        with patch('llm_backend.build_backend', return_value=DeadLlama()):
            with pytest.raises(TimeoutError):
                agent._llm_request(logging.getLogger('test'), prompt="hi")
    finally:
        agent._main_backend = orig_main
