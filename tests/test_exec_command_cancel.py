"""Tests for double-Escape cancellation of exec_command foreground execution.

Verifies that when the cancel event is set while a subprocess is running,
exec_command raises CancelledError and kills the subprocess promptly,
rather than waiting for the full timeout to expire.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cancel
from tools.exec_command import fn as exec_fn, cleanup_temp_sessions


class TestExecCommandCancel(unittest.TestCase):
    def setUp(self):
        cancel.reset()
        cleanup_temp_sessions()

    def tearDown(self):
        cancel.reset()
        cleanup_temp_sessions()

    def test_cancel_event_set_raises_cancelled_error(self):
        """Setting the cancel flag while a slow command is running raises CancelledError."""
        # Schedule cancel after a short delay so the subprocess is running.
        def _trigger():
            time.sleep(0.15)
            cancel.request_cancel()

        t = threading.Thread(target=_trigger, daemon=True)
        t.start()

        with self.assertRaises(cancel.CancelledError):
            # 'sleep 10' would normally block for 10 s; cancel fires at ~150ms.
            exec_fn(command="sleep 10", timeout=30)

        t.join(timeout=2.0)

    def test_cancel_flag_cleared_command_runs_normally(self):
        """When the cancel flag is NOT set, the command completes normally."""
        cancel.reset()
        result = exec_fn(command="echo ok", timeout=10)
        self.assertIn("exit=0", result)
        self.assertIn("ok", result)

    def test_cancel_interrupts_before_timeout(self):
        """CancelledError is raised well before the timeout elapses."""
        def _trigger():
            time.sleep(0.1)
            cancel.request_cancel()

        t = threading.Thread(target=_trigger, daemon=True)
        t.start()

        start = time.monotonic()
        with self.assertRaises(cancel.CancelledError):
            exec_fn(command="sleep 30", timeout=60)
        elapsed = time.monotonic() - start

        # Should complete in well under 5 s (not the full timeout).
        self.assertLess(elapsed, 5.0, "Cancel should have fired in < 5 s")
        t.join(timeout=2.0)

    def test_request_cancel_sets_event(self):
        """cancel.request_cancel() sets the cancel event (unit-level check)."""
        self.assertFalse(cancel.is_cancelled())
        cancel.request_cancel()
        self.assertTrue(cancel.is_cancelled())

    def test_cancel_already_set_before_exec(self):
        """If cancel is already set before exec_fn is called, it raises immediately."""
        cancel.request_cancel()
        with self.assertRaises(cancel.CancelledError):
            exec_fn(command="sleep 5", timeout=10)

    def test_normal_command_output_not_affected(self):
        """A quick command still returns the correct output when cancel is not set."""
        result = exec_fn(command="printf 'hello\\nworld'", timeout=10)
        self.assertIn("exit=0", result)
        self.assertIn("hello", result)
        self.assertIn("world", result)


if __name__ == "__main__":
    unittest.main()
