import os
import json
import pytest
import subprocess
from unittest.mock import patch, MagicMock, ANY
from agent import _auto_increment_cycle

def test_auto_increment_cycle_no_state_file():
    # Setup: No state file
    with patch('agent._state_path', return_value="nonexistent_file.json"):
        # Should return early without error
        _auto_increment_cycle(MagicMock())

def test_auto_increment_cycle_no_bump_needed():
    # Setup: State file exists, cycle is 2
    state_file = "tmp_state_no_bump.json"
    with open(state_file, "w") as f:
        json.dump({"cycle": 2}, f)
    
    # Mock git log to show only cycle 1 committed
    mock_git_stdout = "commit 123 C1: initial commit\n"
    
    mock_log = MagicMock()
    try:
        with patch('agent._state_path', return_value=state_file), \
             patch('subprocess.run') as mock_run:
            
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_git_stdout)
            
            _auto_increment_cycle(mock_log)
            
            # State should remain 2
            with open(state_file) as f:
                state = json.load(f)
                assert state["cycle"] == 2
            
            mock_log.info.assert_not_called()
    finally:
        if os.path.exists(state_file):
            os.remove(state_file)

def test_auto_increment_cycle_bump_needed():
    # Setup: State file exists, cycle is 1
    state_file = "tmp_state_bump.json"
    with open(state_file, "w") as f:
        json.dump({"cycle": 1}, f)
    
    # Focus file exists, cycle is 1
    focus_dir = "tmp_focus_dir"
    os.makedirs(focus_dir, exist_ok=True)
    focus_file = os.path.join(focus_dir, "state", "focus.json")
    os.makedirs(os.path.join(focus_dir, "state"), exist_ok=True)
    with open(focus_file, "w") as f:
        json.dump({"cycle": 1}, f)
    
    # Mock git log to show cycle 1 committed
    mock_git_stdout = "commit 123 C1: first cycle done\n"
    
    mock_log = MagicMock()
    try:
        with patch('agent._state_path', return_value=state_file), \
             patch('os.getcwd', return_value=focus_dir), \
             patch('subprocess.run') as mock_run:
            
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_git_stdout)
            
            _auto_increment_cycle(mock_log)
            
            # State should be bumped to 2
            with open(state_file) as f:
                state = json.load(f)
                assert state["cycle"] == 2
                
            # Focus should be bumped to 2
            with open(focus_file) as f:
                focus = json.load(f)
                assert focus["cycle"] == 2
                
            # Verify log call
            mock_log.info.assert_called_with(
                "AUTO-INCREMENT: cycle %d already committed, bumped state to %d", 
                1, 
                2
            )
    finally:
        if os.path.exists(state_file):
            os.remove(state_file)
        if os.path.exists(focus_dir):
            import shutil
            shutil.rmtree(focus_dir)

def test_auto_increment_cycle_git_error():
    # Setup: State file exists
    state_file = "tmp_state_git_err.json"
    with open(state_file, "w") as f:
        json.dump({"cycle": 1}, f)
    
    mock_log = MagicMock()
    try:
        with patch('agent._state_path', return_value=state_file), \
             patch('subprocess.run') as mock_run:
            
            mock_run.return_value = MagicMock(returncode=1)
            
            _auto_increment_cycle(mock_log)
            
            # Should return early, no bump
            with open(state_file) as f:
                state = json.load(f)
                assert state["cycle"] == 1
    finally:
        if os.path.exists(state_file):
            os.remove(state_file)

def test_auto_increment_cycle_exception():
    # Setup: Corrupt state file to trigger exception
    state_file = "tmp_state_exception.json"
    with open(state_file, "w") as f:
        f.write("not json")
    
    mock_log = MagicMock()
    try:
        with patch('agent._state_path', return_value=state_file):
            _auto_increment_cycle(mock_log)
            
            # Verify warning call
            mock_log.warning.assert_called_with(
                "Auto-increment check failed: %s", 
                ANY
            )
    finally:
        if os.path.exists(state_file):
            os.remove(state_file)
