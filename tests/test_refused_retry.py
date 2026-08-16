"""Connection-refused (dead port) gets a bounded retry + one actionable
message, not the full ~2min backoff of a transient 5xx (2026-08-16: a user
who forgot to start llama-server got a wall of NewConnectionError retries)."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import llm_backend as L


class TestRefusedClassification(unittest.TestCase):
    def test_refused_detected(self):
        self.assertTrue(L._is_conn_refused(Exception("Connection refused")))
        self.assertTrue(L._is_conn_refused(
            Exception("Failed to establish a new connection: [Errno 111]")))

    def test_transient_not_refused(self):
        self.assertFalse(L._is_conn_refused(Exception("Read timed out")))
        self.assertFalse(L._is_conn_refused(Exception("Server error 503")))

    def test_refused_caps_low_and_warns_once(self):
        log = mock.Mock()
        e = Exception("Connection refused")
        cap0 = L._refused_retry_cap(e, 10, "http://127.0.0.1:9999", log, 0)
        self.assertEqual(cap0, 3)
        log.warning.assert_called_once()               # once at attempt 0
        self.assertIn("9999", str(log.warning.call_args))
        log2 = mock.Mock()
        L._refused_retry_cap(e, 10, "http://x:9999", log2, 1)
        log2.warning.assert_not_called()               # silent on later attempts

    def test_transient_keeps_full_budget(self):
        log = mock.Mock()
        cap = L._refused_retry_cap(Exception("Read timed out"), 10,
                                   "http://x", log, 0)
        self.assertEqual(cap, 10)
        log.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
