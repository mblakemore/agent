"""Tests for the tier-2/3 memory management helpers in agent.py.

Covers:
- `_read_vmrss_mb` returns a positive int in this process
- `_release_memory` runs without raising and emits the telemetry line
- `_check_memory_watermark` returns "ok" at low RSS and honors env overrides
- Hard-limit path exits cleanly (tested via mock to ensure coverage tracking).
"""

import logging
import os
import sys

import pytest
from unittest.mock import patch

import agent

def test_read_vmrss_mb_positive():
    """VmRSS for the current process is > 0 in a real Linux env."""
    mb = agent._read_vmrss_mb()
    assert mb > 0, "this process should have some resident memory"


def test_release_memory_logs_telemetry(caplog):
    """_release_memory emits the mem.trim key-value line."""
    log = logging.getLogger("test_memory")
    with caplog.at_level(logging.INFO, logger="test_memory"):
        agent._release_memory(log)
    matched = [r for r in caplog.records if "mem.trim" in r.message]
    assert matched, "expected mem.trim log line"
    msg = matched[0].message
    assert "released=" in msg
    assert "vmrss_mb=" in msg
    assert "trim_available=" in msg


def test_release_memory_env_disable(caplog, monkeypatch):
    """AGENT_DISABLE_MEM_TRIM=1 skips the trim call cleanly."""
    monkeypatch.setenv("AGENT_DISABLE_MEM_TRIM", "1")
    log = logging.getLogger("test_memory_disabled")
    with caplog.at_level(logging.INFO, logger="test_memory"):
        agent._release_memory(log)
    assert not any("mem.trim" in r.message for r in caplog.records)


def test_check_memory_watermark_ok_under_normal_load():
    """Under normal test conditions, watermark is well under the default 8GB warn."""
    log = logging.getLogger("test_watermark")
    assert agent._check_memory_watermark(log) == "ok"


def test_check_memory_watermark_pressure_with_tight_warn(monkeypatch, caplog):
    """Setting warn threshold below current RSS returns 'pressure' and logs."""
    # Use a 1 MB warn threshold — current process RSS is well above this.
    monkeypatch.setattr(agent, "_MEM_WARN_MB", 1)
    monkeypatch.setattr(agent, "_MEM_HARD_MB", 100_000)  # never hit
    log = logging.getLogger("test_pressure")
    with caplog.at_level(logging.WARNING, logger="test_pressure"):
        state = agent._check_memory_watermark(log)
    assert state == "pressure"
    assert any("mem.watermark" in r.message for r in caplog.records)


def test_hard_limit_exits_cleanly(monkeypatch):
    """With hard limit below current RSS, agent._check_memory_watermark exits
    the process with code 2. Use mocks to ensure coverage is tracked.
    """
    # Set hard limit to 1 MB — trivially exceeded by current process
    monkeypatch.setattr(agent, "_MEM_HARD_MB", 1)
    log = logging.getLogger("test_hard_limit")
    
    with pytest.raises(SystemExit) as excinfo:
        agent._check_memory_watermark(log)
    
    assert excinfo.value.code == 2


def test_hard_limit_name_error_path(monkeypatch):
    """Verify the NameError path when _log_bedrock_session_spend is missing."""
    # Set hard limit to 1 MB — trivially exceeded by current process
    monkeypatch.setattr(agent, "_MEM_HARD_MB", 1)
    log = logging.getLogger("test_hard_limit_name_error")
    
    # Save the original function to restore it
    original_spend_func = getattr(agent, "_log_bedrock_session_spend", None)
    
    try:
        # Remove the function to trigger NameError
        if hasattr(agent, "_log_bedrock_session_spend"):
            del agent._log_bedrock_session_spend
        
        with pytest.raises(SystemExit) as excinfo:
            agent._check_memory_watermark(log)
        
        assert excinfo.value.code == 2
    finally:
        # Restore the function
        if original_spend_func:
            setattr(agent, "_log_bedrock_session_spend", original_spend_func)
