import select
"""Tests for cancel.py including low-level terminal and monitor logic."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cancel

class TuiModeTests(unittest.TestCase):
    def setUp(self):
        cancel.set_tui_mode(False)
        cancel.reset()

    def tearDown(self):
        cancel.set_tui_mode(False)
        cancel.reset()

    def test_default_is_non_tui(self):
        self.assertFalse(cancel.tui_mode())

    def test_enable_and_disable(self):
        cancel.set_tui_mode(True)
        self.assertTrue(cancel.tui_mode())
        cancel.set_tui_mode(False)
        self.assertFalse(cancel.tui_mode())

    def test_request_cancel_sets_flag(self):
        self.assertFalse(cancel.is_cancelled())
        cancel.request_cancel()
        self.assertTrue(cancel.is_cancelled())
        cancel.reset()
        self.assertFalse(cancel.is_cancelled())

    def test_cancellable_honors_tui_mode(self):
        cancel.set_tui_mode(True)
        with cancel.cancellable() as cm:
            # In TUI mode, cbreak capture is skipped; the internal cbreak
            # handle should be None.
            self.assertIsNone(cm._cbreak)
        cancel.set_tui_mode(False)

    def test_check_cancelled_raises_when_set(self):
        cancel.request_cancel()
        with self.assertRaises(cancel.CancelledError):
            cancel.check_cancelled()

    def test_check_cancelled_noop_when_clear(self):
        cancel.reset()
        self.assertIsNone(cancel.check_cancelled())

    def test_cancellable_resets_flag_on_entry(self):
        cancel.request_cancel()
        self.assertTrue(cancel.is_cancelled())
        cancel.set_tui_mode(True)  # avoid touching real stdin
        with cancel.cancellable():
            self.assertFalse(cancel.is_cancelled())

    def test_request_cancel_works_in_tui_mode(self):
        cancel.set_tui_mode(True)
        cancel.request_cancel()
        self.assertTrue(cancel.is_cancelled())


class CancelLowLevelTests(unittest.TestCase):
    def setUp(self):
        cancel.set_tui_mode(False)
        cancel.reset()

    @patch('sys.stdin')
    @patch('termios.tcgetattr')
    @patch('termios.tcsetattr')
    @patch('tty.setcbreak')
    def test_cbreak_mode_tty_success(self, mock_setcbreak, mock_tcset, mock_tcget, mock_stdin):
        mock_stdin.isatty.return_value = True
        mock_stdin.fileno.return_value = 0
        
        with cancel.cbreak_mode():
            mock_tcget.assert_called_once()
            mock_setcbreak.assert_called_once_with(0)
            
        mock_tcset.assert_called_once()

    @patch('sys.stdin')
    def test_cbreak_mode_non_tty(self, mock_stdin):
        mock_stdin.isatty.return_value = False
        with cancel.cbreak_mode() as cm:
            self.assertIsNone(cm.saved)
        
    @patch('sys.stdin')
    @patch('cancel.cbreak_mode')
    def test_cancellable_non_tty(self, mock_cbreak, mock_stdin):
        mock_stdin.isatty.return_value = False
        with cancel.cancellable() as cm:
            self.assertIsNone(cm._cbreak)
            mock_cbreak.assert_not_called()

    @patch('sys.stdin')
    @patch('cancel.cbreak_mode')
    def test_cancellable_tty(self, mock_cbreak, mock_stdin):
        mock_stdin.isatty.return_value = True
        # Setup mock cbreak_mode to act like a context manager
        mock_cm = MagicMock()
        mock_cbreak.return_value.__enter__.return_value = mock_cm
        
        with cancel.cancellable() as cm:
            self.assertIsNotNone(cm._cbreak)
            self.assertTrue(cancel._monitor_active.is_set())

    @patch('select.select')
    @patch('sys.stdin')
    def test_read_byte_select_success(self, mock_stdin, mock_select):
        mock_stdin.fileno.return_value = 0
        # Simulate stdin being ready
        mock_select.return_value = ([mock_stdin], [], [])
        mock_stdin.read.return_value = b'a'
        
        # Ensure monitor is active so it doesn't return None
        cancel._monitor_active.set()
        res = cancel._read_byte(0.1)
        self.assertEqual(res, b'a')

    @patch('select.select')
    @patch('sys.stdin')
    def test_read_byte_select_none(self, mock_stdin, mock_select):
        mock_stdin.fileno.return_value = 0
        mock_select.return_value = ([], [], [])
        res = cancel._read_byte(0.1)
        self.assertIsNone(res)

    @patch('os.write')
    def test_wake_monitor_calls_write(self, mock_write):
        # Setup a fake pipe
        cancel._wake_w = 99
        cancel._wake_monitor()
        mock_write.assert_called_once_with(99, b"!")

    @patch('cancel._read_byte')
    def test_monitor_loop_double_escape(self, mock_read):
        class BreakLoop(Exception): pass
        
        def side_effect(timeout):
            nonlocal count
            count += 1
            seq = ['\x1b', None, '\x1b', None]
            if count <= len(seq):
                return seq[count-1]
            raise BreakLoop()

        count = 0
        mock_read.side_effect = side_effect

        cancel._monitor_active.set()
        cancel.reset()

        try:
            cancel._monitor_loop()
        except BreakLoop:
            pass

        self.assertTrue(cancel.is_cancelled())

    @patch('cancel._read_byte')
    def test_monitor_loop_ansi_ignore(self, mock_read):
        class BreakLoop(Exception): pass
        
        def side_effect(timeout):
            nonlocal count
            count += 1
            seq = ['\x1b', '[', 'A']
            if count <= len(seq):
                return seq[count-1]
            raise BreakLoop()

        count = 0
        mock_read.side_effect = side_effect
        
        cancel._monitor_active.set()
        cancel.reset()
        
        try:
            cancel._monitor_loop()
        except BreakLoop:
            pass
            
        self.assertFalse(cancel.is_cancelled())

    @patch('cancel._read_byte')
    def test_monitor_loop_single_escape_timeout(self, mock_read):
        class BreakLoop(Exception): pass
        
        def side_effect(timeout):
            nonlocal count
            count += 1
            seq = ['\x1b', None, '\x1b', None]
            if count <= len(seq):
                return seq[count-1]
            raise BreakLoop()

        count = 0
        mock_read.side_effect = side_effect

        with patch('time.monotonic') as mock_time:
            # First call: 0.0, Second call: 0.5 ( > 0.4)
            mock_time.side_effect = [0.0, 0.5]
            
            cancel._monitor_active.set()
            cancel.reset()
            
            try:
                cancel._monitor_loop()
            except BreakLoop:
                pass
                
            self.assertFalse(cancel.is_cancelled())

    @patch('termios.tcsetattr')
    def test_restore_terminal(self, mock_tcset):
        cancel._original_termios = [1, 2, 3]
        cancel._restore_terminal()
        mock_tcset.assert_called_once()

if __name__ == "__main__":
    unittest.main()

class CoverageEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        cancel.reset()

    @patch('os.write')
    def test_wake_monitor_oserror(self, mock_write):
        cancel._wake_w = 1
        mock_write.side_effect = OSError()
        # Should not raise
        cancel._wake_monitor()

    @patch('sys.stdin')
    def test_read_byte_no_fileno(self, mock_stdin):
        # Remove fileno attribute
        del mock_stdin.fileno
        self.assertIsNone(cancel._read_byte(0.1))

    @patch('os.read')
    def test_read_byte_pipe_oserror(self, mock_read):
        # Setup wake pipe
        cancel._wake_r = 1
        # Mock os.read to fail when reading from pipe
        # Need to be careful not to break other things
        mock_read.side_effect = OSError()
        # We need select to say the pipe is ready
        with patch('select.select', return_value=([1], [], [])):
            # The first check is for sys.stdin. We need sys.stdin to not be ready
            # or be processed first.
            with patch('sys.stdin', spec=[]):
                self.assertIsNone(cancel._read_byte(0.1))

    @patch('cancel._read_byte')
    def test_monitor_loop_ansi_O_sequence(self, mock_read):
        class BreakLoop(Exception): pass
        def side_effect(timeout):
            nonlocal count
            count += 1
            seq = ['\x1b', 'O', None]
            if count <= len(seq):
                return seq[count-1]
            raise BreakLoop()
        count = 0
        mock_read.side_effect = side_effect
        cancel._monitor_active.set()
        try:
            cancel._monitor_loop()
        except BreakLoop:
            pass

    @patch('cancel._read_byte')
    def test_monitor_loop_non_escape(self, mock_read):
        class BreakLoop(Exception): pass
        def side_effect(timeout):
            nonlocal count
            count += 1
            seq = ['a', None]
            if count <= len(seq):
                return seq[count-1]
            raise BreakLoop()
        count = 0
        mock_read.side_effect = side_effect
        cancel._monitor_active.set()
        try:
            cancel._monitor_loop()
        except BreakLoop:
            pass

    @patch('cancel._read_byte')
    def test_consume_ansi_internals(self, mock_read):
        # Sequence: b'[', then some chars, then a letter
        # \x1b [ 1 2 m
        mock_read.side_effect = ['[', '1', '2', 'm', None]
        cancel._consume_ansi_sequence()

    @patch('termios.tcsetattr')
    def test_restore_terminal_exception(self, mock_tcset):
        cancel._original_termios = [1, 2, 3]
        mock_tcset.side_effect = Exception("Fail")
        # Should not raise
        cancel._restore_terminal()

    def test_wake_monitor_none_pipe(self):
        cancel._wake_w = None
        # Should not raise
        cancel._wake_monitor()

class FinalCoverageTests(unittest.TestCase):
    def setUp(self):
        cancel.reset()

    @patch('select.select', side_effect=select.error("Select failure"))
    def test_read_byte_select_error(self, mock_select):
        # Covers line 151
        self.assertIsNone(cancel._read_byte(0.1))

    @patch('os.read', side_effect=OSError("Read failure"))
    @patch('select.select')
    def test_read_byte_pipe_read_failure(self, mock_select, mock_read):
        # Covers lines 145-148
        cancel._wake_r = 1
        # Make select say the wake pipe is ready, but sys.stdin is not
        mock_select.return_value = ([1], [], [])
        # Mock sys.stdin so it's not in the 'ready' list
        with patch('sys.stdin', spec=[]):
            self.assertIsNone(cancel._read_byte(0.1))

    @patch('cancel._read_byte')
    def test_consume_ansi_bare_esc(self, mock_read):
        # Covers line 158
        mock_read.return_value = None
        cancel._consume_ansi_sequence()

    def test_os_pipe_failure(self):
        # Covers lines 45-47
        # This requires reloading the module to trigger the top-level try-except
        import importlib
        with patch('os.pipe', side_effect=OSError("Pipe fail")):
            importlib.reload(cancel)
            self.assertIsNone(cancel._wake_r)
            self.assertIsNone(cancel._wake_w)
