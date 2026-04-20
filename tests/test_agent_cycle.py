import os
import json
import subprocess
import pytest
from unittest.mock import MagicMock, patch, mock_open
from agent import _auto_increment_cycle

def test_auto_increment_no_state_file():
    """Test that the function returns early if state file does not exist."""
    with patch('os.path.exists', return_value=False):
        log = MagicMock()
        _auto_increment_cycle(log)
        log.info.assert_not_called()

def test_auto_increment_cycle_zero():
    """Test that the function returns early if cycle is <= 0."""
    state_data = json.dumps({"cycle": 0})
    with patch('os.path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=state_data)):
        log = MagicMock()
        _auto_increment_cycle(log)
        log.info.assert_not_called()

def test_auto_increment_git_fail():
    """Test that the function returns early if git log fails."""
    state_data = json.dumps({"cycle": 1})
    with patch('os.path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=state_data)), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        log = MagicMock()
        _auto_increment_cycle(log)
        log.info.assert_not_called()

def test_auto_increment_no_committed_cycles():
    """Test that the function returns early if no committed cycles found in log."""
    state_data = json.dumps({"cycle": 1})
    with patch('os.path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=state_data)), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="some random logs\nno cycles here")
        log = MagicMock()
        _auto_increment_cycle(log)
        log.info.assert_not_called()

def test_auto_increment_bump_success():
    """Test that the cycle is bumped when local cycle is <= highest committed."""
    state_data = json.dumps({"cycle": 1})
    # Git log shows C2: ...
    git_stdout = "C2: Fixed bug\nC1: Initial"
    
    # Mocking open for both read (state) and write (state & focus)
    # We need a more complex mock for open because it's called multiple times
    m = mock_open(read_data=state_data)
    
    with patch('os.path.exists', return_value=True), \
         patch('builtins.open', m), \
         patch('subprocess.run') as mock_run:
        
        mock_run.return_value = MagicMock(returncode=0, stdout=git_stdout)
        
        log = MagicMock()
        _auto_increment_cycle(log)
        
        # Check if state was updated to 3 (highest_committed 2 + 1)
        # The second call to open is for writing state.json
        handle = m()
        # Find the call that wrote the new state
        written_data = "".join(call.args[0] for call in handle.write.call_args_list if call.args)
        assert '"cycle": 3' in written_data
        log.info.assert_any_call("AUTO-INCREMENT: cycle %d already committed, bumped state to %d", 1, 3)

def test_auto_increment_no_bump_needed():
    """Test that no bump occurs when local cycle is > highest committed."""
    state_data = json.dumps({"cycle": 10})
    git_stdout = "C5: Old cycle\nC4: Older cycle"
    
    with patch('os.path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=state_data)), \
         patch('subprocess.run') as mock_run:
        
        mock_run.return_value = MagicMock(returncode=0, stdout=git_stdout)
        
        log = MagicMock()
        _auto_increment_cycle(log)
        log.info.assert_not_called()

def test_auto_increment_exception():
    """Test that exceptions are caught and logged."""
    with patch('os.path.exists', side_effect=Exception("Disk failure")):
        log = MagicMock()
        _auto_increment_cycle(log)
        # The actual call passes the Exception object itself, not just the string
        log.warning.assert_called()
        args, _ = log.warning.call_args
        assert "Disk failure" in str(args[1])
