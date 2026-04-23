import unittest
from unittest.mock import patch, MagicMock
import sys
import io
import select
import termios
import cancel

class CancelCoverageTests(unittest.TestCase):
    def setUp(self):
        cancel.reset()
        cancel.set_tui_mode(False)

    def tearDown(self):
        cancel.reset()
        cancel.set_tui_mode(False)

    @patch('sys.stdin')
    @patch('termios.tcgetattr')
    @patch('termios.tcsetattr')
    @patch('tty.setcbreak')
    def test_cbreak_mode_enter_exit(self, mock_setcbreak, mock_setattr, mock_getattr, mock_stdin):
        mock_getattr.return_value = [1, 2, 3]
        mock_stdin.isatty.return_value = True
        mock_stdin.fileno.return_value = 10
        with cancel.cbreak_mode():
            mock_setcbreak.assert_called()
        mock_setattr.assert_called()

    @patch('sys.stdin')
    def test_cbreak_mode_non_tty(self, mock_stdin):
        mock_stdin.isatty.return_value = False
        with cancel.cbreak_mode() as cm:
            self.assertIsNotNone(cm)

    @patch('sys.stdin')
    @patch('select.select')
    def test_consume_ansi_sequence_csi(self, mock_select, mock_stdin):
        # Mock CSI sequence: ESC [ A
        mock_select.return_value = ([mock_stdin], [], [])
        mock_stdin.read.side_effect = ['[', 'A']
        # _consume_ansi_sequence is only called from the monitor loop right
        # after a real ESC read, so the monitor is active. _read_byte's
        # gate refuses stdin consumption when monitoring is off — exercise
        # this test under the same invariant.
        cancel._monitor_active.set()
        try:
            cancel._consume_ansi_sequence()
        finally:
            cancel._monitor_active.clear()
        self.assertEqual(mock_stdin.read.call_count, 2)

    @patch('sys.stdin')
    @patch('select.select')
    def test_consume_ansi_sequence_ss3(self, mock_select, mock_stdin):
        # Mock SS3 sequence: ESC O 0
        mock_select.return_value = ([mock_stdin], [], [])
        mock_stdin.read.side_effect = ['O', '0']
        cancel._monitor_active.set()
        try:
            cancel._consume_ansi_sequence()
        finally:
            cancel._monitor_active.clear()
        self.assertEqual(mock_stdin.read.call_count, 2)

    @patch('sys.stdin')
    @patch('select.select')
    def test_consume_ansi_sequence_bare(self, mock_select, mock_stdin):
        mock_select.return_value = ([], [], [])
        cancel._consume_ansi_sequence()
        mock_stdin.read.assert_not_called()

    @patch('termios.tcsetattr')
    def test_restore_terminal(self, mock_setattr):
        with patch('cancel._original_termios', [1, 2, 3]):
            cancel._restore_terminal()
            mock_setattr.assert_called()

    @patch('sys.stdin')
    @patch('cancel.cbreak_mode')
    def test_cancellable_non_tui(self, mock_cbreak, mock_stdin):
        mock_stdin.isatty.return_value = True
        cancel.set_tui_mode(False)
        with cancel.cancellable():
            mock_cbreak.assert_called()

    @patch('sys.stdin')
    def test_cancellable_tui_mode(self, mock_stdin):
        mock_stdin.isatty.return_value = True
        cancel.set_tui_mode(True)
        with cancel.cancellable() as cm:
            self.assertIsNone(cm._cbreak)

if __name__ == "__main__":
    unittest.main()
