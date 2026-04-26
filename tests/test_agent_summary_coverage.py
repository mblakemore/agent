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
        """Primary fails with network error, fallback via _main_backend succeeds.
    
        Phase 2 followup: the fallback now routes through
        ``_main_backend.complete()`` directly (not through
        ``_summary_request`` overrides, which were a no-op for non-llamacpp
        summary backends). Test patches both entry points to reflect this.
        """
        agent._config["summary"]["enabled"] = True
        agent._config["summary"]["base_url"] = "http://summary-api"
        mock_build_prompt.return_value = "Mocked Prompt"
    
        mock_summary_request.side_effect = requests.ConnectionError("Conn Error")
    
        with patch.object(agent._main_backend, "complete", return_value="Fallback Result") as mock_main:
            result = agent._generate_summary("old_summary", [], agent.log)
    
        self.assertEqual(result, "Fallback Result")
        # Expect 2 calls: initial attempt + failover retry
        self.assertEqual(mock_summary_request.call_count, 2)
        mock_main.assert_called_once()
    @patch('agent._summary_request')
    @patch('agent._build_summary_prompt')
    def test_generate_summary_total_failure(self, mock_build_prompt, mock_summary_request):
        """Primary fails, _main_backend fallback also fails → old_summary returned."""
        agent._config["summary"]["enabled"] = True
        agent._config["summary"]["base_url"] = "http://summary-api"
        mock_build_prompt.return_value = "Mocked Prompt"
    
        mock_summary_request.side_effect = requests.ConnectionError("Primary Fail")
    
        with patch.object(
            agent._main_backend,
            "complete",
            side_effect=requests.ConnectionError("Fallback Fail"),
        ):
            result = agent._generate_summary("old_summary", [], agent.log)
    
        self.assertEqual(result, "old_summary")
        # Expect 2 calls: initial attempt + failover retry
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
        
        self.assertEqual(result, "old_summary")
        self.assertEqual(mock_summary_request.call_count, 0)

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
        self.assertEqual(mock_summary_request.call_count, 0)
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

class TestAgentMaybeResummarize(unittest.TestCase):
    def setUp(self):
        self.log = MagicMock()
        # Default summary state
        self.summary_state = {"text": "Previous Summary", "up_to": 0}
        # Threshold is usually defined in agent.py as _SUMMARY_THRESHOLD
        self.threshold = agent._SUMMARY_THRESHOLD

    @patch('agent._generate_summary')
    @patch('agent._condense_summary')
    def test_maybe_resummarize_below_threshold(self, mock_condense, mock_generate):
        """Path 1: Below Threshold. unsummarized < _SUMMARY_THRESHOLD and force=False."""
        # unsummarized = oldest_idx - summary_state["up_to"]
        # To be below threshold: oldest_idx - 0 < threshold
        oldest_idx = self.threshold - 1
        
        result = agent._maybe_resummarize([], self.summary_state, oldest_idx, self.log, force=False)
        
        self.assertFalse(result)
        mock_generate.assert_not_called()

    def test_maybe_resummarize_no_new_messages(self):
        """Path 2: No New Messages. unsummarized >= _SUMMARY_THRESHOLD but new_messages list is empty."""
        # To trigger the check but have no messages: 
        # oldest_idx - 0 >= threshold, but conversation_history[0:oldest_idx] is empty?
        # Actually, conversation_history is accessed as conversation_history[summary_state["up_to"]:oldest_idx]
        # If conversation_history is empty, new_messages will be empty regardless of oldest_idx.
        oldest_idx = self.threshold + 1
        
        result = agent._maybe_resummarize([], self.summary_state, oldest_idx, self.log, force=False)
        
        self.assertFalse(result)

    @patch('agent._generate_summary')
    @patch('agent._condense_summary')
    def test_maybe_resummarize_success(self, mock_condense, mock_generate):
        """Path 3: Successful Resummarization."""
        # Need messages to summarize
        conversation_history = [{"role": "user", "content": "msg 1"}] * (self.threshold + 1)
        oldest_idx = self.threshold + 1
        
        mock_generate.return_value = "New Summary"
        mock_condense.return_value = "Condensed Summary"
        
        result = agent._maybe_resummarize(conversation_history, self.summary_state, oldest_idx, self.log, force=False)
        
        self.assertTrue(result)
        self.assertEqual(self.summary_state["text"], "Condensed Summary")
        self.assertEqual(self.summary_state["up_to"], oldest_idx)
        mock_generate.assert_called_once()

    @patch('agent._generate_summary')
    @patch('agent._condense_summary')
    def test_maybe_resummarize_force(self, mock_condense, mock_generate):
        """Path 4: Forced Resummarization. force=True."""
        # Below threshold but forced
        oldest_idx = 1
        conversation_history = [{"role": "user", "content": "msg 1"}]
        
        mock_generate.return_value = "Forced Summary"
        mock_condense.return_value = "Condensed Forced Summary"
        
        result = agent._maybe_resummarize(conversation_history, self.summary_state, oldest_idx, self.log, force=True)
        
        self.assertTrue(result)
        self.assertEqual(self.summary_state["text"], "Condensed Forced Summary")
        mock_generate.assert_called_once()
