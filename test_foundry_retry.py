import unittest
from unittest.mock import MagicMock, patch
import time
from llm_backend import FoundryBackend

class TestFoundryRetry(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "api_url": "https://example.com",
            "api_key": "test-key",
            "model": "test-model",
            "role": "main"
        }
        self.backend = FoundryBackend(self.cfg)

    @patch("time.sleep", return_value=None)
    def test_complete_retry_on_429(self, mock_sleep):
        # Simulate 429, 429, then 200 OK
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Success")]
        
        # We need to simulate the exception raised by AnthropicFoundry client on 429
        # Since we don't have the actual client, we'll mock the .messages.create method
        class RateLimitError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        self.backend.client.messages.create = MagicMock(
            side_effect=[
                RateLimitError("Too Many Requests", 429),
                RateLimitError("Too Many Requests", 429),
                mock_response
            ]
        )

        result = self.backend.complete("Hello")
        
        self.assertEqual(result, "Success")
        self.assertEqual(self.backend.client.messages.create.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertTrue(60 <= mock_sleep.call_args[0][0] <= 65)

    @patch("time.sleep", return_value=None)
    def test_stream_chat_retry_on_429(self, mock_sleep):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="StreamSuccess")]
        
        class RateLimitError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        self.backend.client.messages.create = MagicMock(
            side_effect=[
                RateLimitError("Too Many Requests", 429),
                mock_response
            ]
        )

        gen = self.backend.stream_chat(messages=[{"role": "user", "content": "Hello"}])
        results = list(gen)
        
        self.assertEqual(results[0]["choices"][0]["delta"]["content"], "StreamSuccess")
        self.assertEqual(self.backend.client.messages.create.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)
        self.assertTrue(60 <= mock_sleep.call_args[0][0] <= 65)

    @patch("time.sleep", return_value=None)
    def test_complete_max_retries(self, mock_sleep):
        class RateLimitError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        self.backend.client.messages.create = MagicMock(
            side_effect=RateLimitError("Too Many Requests", 429)
        )

        result = self.backend.complete("Hello")
        
        self.assertEqual(result, "")
        # Should retry a few times then give up
        self.assertGreater(self.backend.client.messages.create.call_count, 1)

if __name__ == "__main__":
    unittest.main()
