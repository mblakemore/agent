import os
import json
import subprocess
import pytest
import re
from unittest.mock import patch, MagicMock
import agent

def test_auto_increment_cycle_bumps_state(tmp_path):
    # Setup environment
    state_dir = tmp_path / ".agent" / "state"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "current-state.json"
    
    # Initial state: cycle 1
    state = {"cycle": 1}
    state_path.write_text(json.dumps(state))
    
    # Mock os.getcwd and _state_path to point to our tmp_path
    # We need to patch _state_path because it likely uses a hardcoded relative path
    with patch('agent._state_path', return_value=str(state_path)):
        with patch('subprocess.run') as mock_run:
            # Simulate git log containing C1:
            mock_run.return_value = MagicMock(
                returncode=0, 
                stdout="a1b2c3d C1: Finished cycle 1\n"
            )
            
            # Mock the log object passed to the function
            mock_log = MagicMock()
            
            # Call the target function
            agent._auto_increment_cycle(mock_log)
            
            # Verify state was bumped to 2
            with open(state_path, 'r') as f:
                new_state = json.load(f)
            assert new_state["cycle"] == 2
            mock_log.info.assert_called()

def test_auto_increment_cycle_no_bump_if_not_committed(tmp_path):
    state_dir = tmp_path / ".agent" / "state"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "current-state.json"
    
    state = {"cycle": 2}
    state_path.write_text(json.dumps(state))
    
    with patch('agent._state_path', return_value=str(state_path)):
        with patch('subprocess.run') as mock_run:
            # Simulate git log only containing C1:
            mock_run.return_value = MagicMock(
                returncode=0, 
                stdout="a1b2c3d C1: Finished cycle 1\n"
            )
            
            mock_log = MagicMock()
            agent._auto_increment_cycle(mock_log)
            
            with open(state_path, 'r') as f:
                new_state = json.load(f)
            assert new_state["cycle"] == 2 # Should not bump

def test_auto_increment_cycle_bumps_to_highest(tmp_path):
    state_dir = tmp_path / ".agent" / "state"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "current-state.json"
    
    state = {"cycle": 1}
    state_path.write_text(json.dumps(state))
    
    with patch('agent._state_path', return_value=str(state_path)):
        with patch('subprocess.run') as mock_run:
            # Simulate git log containing C1: and C5:
            mock_run.return_value = MagicMock(
                returncode=0, 
                stdout="a1b2c3d C5: Finished cycle 5\nb2c3d4e C1: Finished cycle 1\n"
            )
            
            mock_log = MagicMock()
            agent._auto_increment_cycle(mock_log)
            
            with open(state_path, 'r') as f:
                new_state = json.load(f)
            assert new_state["cycle"] == 6 # 5 + 1
