import unittest
from unittest.mock import patch, MagicMock
import requests
import threading
from agent import AsyncSummarizer

# We need to access the private functions and config from agent.py
import agent

class TestAgentSummaryCoverage(unittest.TestCase):
    def setUp(self):
        # Save original config to restore after test
        self.original_config = agent._config
        # Mock the global config in agent.py to enable summary
        agent._config = {
            "summary": {
                "enabled": True,
                "base_url": "http://summary-api",
                "max_wait_on_save": 1,
                "model": "summary-model-default"
            },
            "llm": {
                "model": "gpt-4"
            }
        }
        # Mock the logger to avoid polluting test output
        agent.log = MagicMock()

    def tearDown(self):
        # Restore original config
        agent._config = self.original_config

    @patch('agent._summary_request')
    @patch('agent._build_summary_prompt')
    def test_generate_summary_primary_success(self, mock_build_prompt, mock_summary_request):
        """Covers line 751: Primary summary endpoint succeeds."""
        agent._config["summary"]["enabled"] = True
        agent._config["summary"]["base_url"] = "http://summary-api"
        
        mock_build_prompt.return_value = "Mocked Prompt"
        mock_summary_request.return_value = "Primary Summary Result"
        
        result = agent._generate_summary("old_summary", [], agent.log)
        
        self.assertEqual(result, "Primary Summary Result")
        self.assertEqual(mock_summary_request.call_count, 1)

    @patch('agent._summary_request')
    @patch('agent._build_summary_prompt')
    def test_generate_summary_exception_fallback_success(self, mock_build_prompt, mock_summary_request):
        """Covers lines 756-759: Primary fails with network error, fallback succeeds."""
        agent._config["summary"]["enabled"] = True
        agent._config["summary"]["base_url"] = "http://summary-api"
        mock_build_prompt.return_value = "Mocked Prompt"
        
        # First call: raise ConnectionError to trigger the outer except block (line 752)
        # Second call: return value for the fallback (line 756)
        mock_summary_request.side_effect = [requests.ConnectionError("Conn Error"), "Fallback Result"]
        
        result = agent._generate_summary("old_summary", [], agent.log)
        
        self.assertEqual(result, "Fallback Result")
        self.assertEqual(mock_summary_request.call_count, 2)

    @patch('agent._summary_request')
    @patch('agent._build_summary_prompt')
    def test_generate_summary_total_failure(self, mock_build_prompt, mock_summary_request):
        """Covers lines 760-762: Both calls fail with network errors."""
        agent._config["summary"]["enabled"] = True
        agent._config["summary"]["base_url"] = "http://summary-api"
        mock_build_prompt.return_value = "Mocked Prompt"
        
        # Both calls fail with network errors
        mock_summary_request.side_effect = [requests.ConnectionError("Primary Fail"), requests.ConnectionError("Fallback Fail")]
        
        result = agent._generate_summary("old_summary", [], agent.log)
        
        self.assertEqual(result, "old_summary")
        self.assertEqual(mock_summary_request.call_count, 2)

    @patch('agent._summary_request')
    @patch('agent._build_summary_prompt')
    def test_generate_summary_generic_exception(self, mock_build_prompt, mock_summary_request):
        """Covers lines 765-767: Non-network exception on first call."""
        agent._config["summary"]["enabled"] = True
        agent._config["summary"]["base_url"] = "http://summary-api"
        mock_build_prompt.return_value = "Mocked Prompt"
        
        # Raise a RuntimeError which is not a ConnectionError or Timeout
        mock_summary_request.side_effect = RuntimeError("Generic Error")
        
        result = agent._generate_summary("old_summary", [], agent.log)
        
        self.assertEqual(result, "old_summary")
        self.assertEqual(mock_summary_request.call_count, 1)

    @patch('agent._summary_request')
    @patch('agent._build_summary_prompt')
    def test_generate_summary_disabled(self, mock_build_prompt, mock_summary_request):
        """Covers the 'else' block (line 747) when summary is disabled."""
        agent._config["summary"]["enabled"] = False
        mock_build_prompt.return_value = "Mocked Prompt"
        mock_summary_request.return_value = "Main Model Result"
        
        result = agent._generate_summary("old_summary", [], agent.log)
        
        self.assertEqual(result, "Main Model Result")
        self.assertEqual(mock_summary_request.call_count, 1)

    @patch('agent._summary_request')
    @patch('agent._build_summary_prompt')
    def test_generate_summary_no_summary_url(self, mock_build_prompt, mock_summary_request):
        """Covers lines 763-764: summary_url is not set or equals BASE_URL."""
        agent._config["summary"]["enabled"] = True
        agent._config["summary"]["base_url"] = None # Set to None to skip fallback
        mock_build_prompt.return_value = "Mocked Prompt"
        
        # Must fail to enter the except block
        mock_summary_request.side_effect = requests.ConnectionError("Conn Error")
        
        result = agent._generate_summary("old_summary", [], agent.log)
        
        self.assertEqual(result, "old_summary")
        self.assertEqual(mock_summary_request.call_count, 1)

    @patch('agent._summary_request')
    def test_async_summarizer_success(self, mock_request):
        mock_request.return_value = "Async Success"
        summarizer = AsyncSummarizer(agent._config, agent.log)
        
        summarizer.kick("old summary", [], 0)
        summarizer.drain()
        
        self.assertTrue(summarizer.harvest({"text": "", "up_to": -1}))
        mock_request.assert_called_once()

    @patch('agent._summary_request')
    def test_async_summarizer_fallback_success(self, mock_request):
        # Primary fails, Fallback succeeds
        mock_request.side_effect = [requests.ConnectionError("Primary Down"), "Fallback Success"]
        summarizer = AsyncSummarizer(agent._config, agent.log)
        
        summarizer.kick("old summary", [], 0)
        summarizer.drain()
        
        self.assertTrue(summarizer.harvest({"text": "", "up_to": -1}))
        self.assertEqual(mock_request.call_count, 2)

    @patch('agent._summary_request')
    def test_async_summarizer_total_failure(self, mock_request):
        # Both fail
        mock_request.side_effect = [requests.ConnectionError("Primary Down"), requests.ConnectionError("Fallback Down")]
        summarizer = AsyncSummarizer(agent._config, agent.log)
        
        summarizer.kick("old summary", [], 0)
        summarizer.drain()
        
        self.assertFalse(summarizer.harvest({"text": "", "up_to": -1}))
        self.assertEqual(mock_request.call_count, 2)

    @patch('agent._summary_request')
    def test_async_summarizer_generic_exception(self, mock_request):
        # Unexpected exception
        mock_request.side_effect = Exception("Unexpected")
        summarizer = AsyncSummarizer(agent._config, agent.log)
        
        summarizer.kick("old summary", [], 0)
        summarizer.drain()
        
        self.assertFalse(summarizer.harvest({"text": "", "up_to": -1}))
        self.assertEqual(mock_request.call_count, 1)
