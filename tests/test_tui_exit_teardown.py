"""Tests for TuiSession.exit_app (Fix D of plan/live-input-display-fixes.md,
defect #4: exiting live-input mode left the shell mid-paint)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tui


@unittest.skipUnless(tui._AVAILABLE, "prompt_toolkit not installed")
class TestExitApp(unittest.TestCase):
    def _session(self):
        return tui.TuiSession(
            history=[], summary_state={"text": "", "up_to": 0},
            config={}, ctx_size=1000,
            cb=mock.Mock(verbose=False),
            estimate_tokens=lambda m: 1,
        )

    def test_noop_when_app_not_running(self):
        sess = self._session()
        # Fresh session: app exists but is not running — must not raise.
        sess.exit_app()

    def test_schedules_exit_with_eoferror_on_running_app(self):
        sess = self._session()
        fake_app = mock.Mock()
        fake_app.is_running = True
        scheduled = []
        fake_app.loop.call_soon_threadsafe = lambda fn: scheduled.append(fn)
        sess._session.app = fake_app  # PromptSession.app is an instance attr
        sess.exit_app()
        self.assertEqual(len(scheduled), 1)
        scheduled[0]()  # run on the "app loop"
        fake_app.exit.assert_called_once()
        self.assertIs(fake_app.exit.call_args.kwargs["exception"], EOFError)

    def test_exit_callback_rechecks_is_running(self):
        sess = self._session()
        fake_app = mock.Mock()
        fake_app.is_running = True
        scheduled = []
        fake_app.loop.call_soon_threadsafe = lambda fn: scheduled.append(fn)
        sess._session.app = fake_app
        sess.exit_app()
        # App stopped between scheduling and callback execution.
        fake_app.is_running = False
        scheduled[0]()
        fake_app.exit.assert_not_called()

    def test_never_raises_when_loop_missing(self):
        sess = self._session()
        fake_app = mock.Mock()
        fake_app.is_running = True
        fake_app.loop = None
        sess._session.app = fake_app
        sess.exit_app()  # must be a silent no-op

    def test_never_raises_when_schedule_fails(self):
        sess = self._session()
        fake_app = mock.Mock()
        fake_app.is_running = True
        fake_app.loop.call_soon_threadsafe = mock.Mock(side_effect=RuntimeError)
        sess._session.app = fake_app
        sess.exit_app()  # must swallow


if __name__ == "__main__":
    unittest.main()
