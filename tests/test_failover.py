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
    agent._main_backend = FailingBedrock()
    # Mock that llamacpp is available
    with patch('llm_backend.build_backend', return_value=WorkingLlama()):
        # This should trigger failover (once implemented)
        try:
            res = agent._llm_request(logging.getLogger('test'), prompt="hi")
            assert agent._main_backend.kind == 'llamacpp'
        except Exception as e:
            pytest.fail(f"Main failover failed: {e}")

def test_summary_failover_timeout():
    """Bedrock summary TimeoutError -> failover to llamacpp summary."""
    agent._summary_backend = FailingBedrock()
    with patch('llm_backend.build_backend', return_value=WorkingLlama()):
        # Simulate a summary request
        try:
            # We use _summary_request which is the internal wrapper
            res = agent._summary_request("prompt")
            assert agent._summary_backend.kind == 'llamacpp'
        except Exception as e:
            pytest.fail(f"Summary failover failed: {e}")

def test_budget_exceeded_failover():
    """BedrockBudgetExceeded -> failover to llamacpp."""
    class BudgetExceededBedrock(FailingBedrock):
        def stream_chat(self, log, **kw):
            # Simulate the specific exception mentioned in the issue
            raise Exception("BedrockBudgetExceeded")
    
    agent._main_backend = BudgetExceededBedrock()
    with patch('llm_backend.build_backend', return_value=WorkingLlama()):
        try:
            agent._llm_request(logging.getLogger('test'), prompt="hi")
            assert agent._main_backend.kind == 'llamacpp'
        except Exception as e:
            pytest.fail(f"Budget failover failed: {e}")

def test_llamacpp_down_failover():
    """Llamacpp also down -> original Bedrock exception re-raised."""
    agent._main_backend = FailingBedrock()
    class DeadLlama:
        kind = 'llamacpp'
        def health(self): return False, 'down'
        def stream_chat(self, log, **kw): pass
    
    with patch('llm_backend.build_backend', return_value=DeadLlama()):
        with pytest.raises(TimeoutError):
            agent._llm_request(logging.getLogger('test'), prompt="hi")
