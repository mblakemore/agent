"""Tests for telemetry wiring in agent.run_agent_interactive (issue #400).

Each test exercises a different session-end path and verifies that
``telemetry.record_cycle`` is called with the correct ``status`` and
non-None ``duration_s``. The disabled case verifies record_cycle is NOT
called when ``telemetry.init()`` returns False. Shutdown is verified to
fire once at the end of the function.

We use ``unittest.mock.patch`` to stub the telemetry module surface so
no real OTLP exporter is involved.
"""

import pytest
from unittest.mock import patch, MagicMock

import agent


def _logger_tuple():
    return MagicMock(), "log_path", "err_path"


@patch('agent.telemetry.shutdown')
@patch('agent.telemetry.record_cycle')
@patch('agent.telemetry.init', return_value=True)
@patch('agent._setup_logger')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_auto_mode_session_end_records_cycle(
    mock_run, mock_emit, mock_log, mock_init, mock_record, mock_shutdown
):
    """Auto-mode session end calls record_cycle(status='auto_completed')."""
    mock_log.return_value = _logger_tuple()
    mock_run.return_value = "finished"

    agent.run_agent_interactive(initial_prompt="Hello", auto=True, tui=False)

    assert mock_init.call_count == 1
    # At least one record_cycle call with auto_completed status
    auto_calls = [c for c in mock_record.call_args_list
                  if c.kwargs.get("status") == "auto_completed"]
    assert len(auto_calls) == 1, f"expected 1 auto_completed call, got {mock_record.call_args_list}"
    # duration_s must be a non-negative float
    duration = auto_calls[0].kwargs.get("duration_s")
    assert isinstance(duration, float) and duration >= 0.0


@patch('agent.telemetry.shutdown')
@patch('agent.telemetry.record_cycle')
@patch('agent.telemetry.init', return_value=True)
@patch('agent._load_checkpoint')
@patch('agent._setup_logger')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_continue_mode_session_end_records_cycle(
    mock_run, mock_emit, mock_log, mock_load_cp, mock_init, mock_record, mock_shutdown
):
    """Continue+auto session end calls record_cycle(status='continue_completed')."""
    mock_log.return_value = _logger_tuple()
    mock_run.return_value = "finished"
    # Provide a checkpoint tuple: (history, summary_state, start_turn, initial_files)
    mock_load_cp.return_value = ([], {"text": ""}, 0, [])

    agent.run_agent_interactive(auto=True, continue_mode=True, tui=False)

    cont_calls = [c for c in mock_record.call_args_list
                  if c.kwargs.get("status") == "continue_completed"]
    assert len(cont_calls) == 1, f"expected 1 continue_completed call, got {mock_record.call_args_list}"


@patch('agent.telemetry.shutdown')
@patch('agent.telemetry.record_cycle')
@patch('agent.telemetry.init', return_value=True)
@patch('agent._setup_logger')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_cancelled_session_end_records_cycle(
    mock_run, mock_emit, mock_log, mock_init, mock_record, mock_shutdown
):
    """Operator-cancelled (auto-pause + KeyboardInterrupt) records cancelled."""
    mock_log.return_value = _logger_tuple()
    # First call returns 'cancelled' to trigger the operator-pause prompt,
    # then we KeyboardInterrupt at the input() to exit via the cancelled site.
    mock_run.side_effect = ["cancelled", "finished"]

    with patch('builtins.input', side_effect=KeyboardInterrupt):
        agent.run_agent_interactive(initial_prompt="Start", auto=True, tui=False)

    cancel_calls = [c for c in mock_record.call_args_list
                    if c.kwargs.get("status") == "cancelled"]
    assert len(cancel_calls) == 1, f"expected 1 cancelled call, got {mock_record.call_args_list}"


@patch('agent.telemetry.shutdown')
@patch('agent.telemetry.record_cycle')
@patch('agent.telemetry.init', return_value=False)
@patch('agent._setup_logger')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_disabled_telemetry_does_not_record(
    mock_run, mock_emit, mock_log, mock_init, mock_record, mock_shutdown
):
    """When telemetry.init() returns False, record_cycle is NOT called."""
    mock_log.return_value = _logger_tuple()
    mock_run.return_value = "finished"

    agent.run_agent_interactive(initial_prompt="Hello", auto=True, tui=False)

    assert mock_record.call_count == 0
    # shutdown must also not be invoked when disabled
    assert mock_shutdown.call_count == 0


@patch('agent.telemetry.shutdown')
@patch('agent.telemetry.record_cycle')
@patch('agent.telemetry.init', return_value=True)
@patch('agent._setup_logger')
@patch('agent._emit')
@patch('agent.run_agent_single')
def test_shutdown_called_at_session_end(
    mock_run, mock_emit, mock_log, mock_init, mock_record, mock_shutdown
):
    """telemetry.shutdown() is called once at the end of run_agent_interactive."""
    mock_log.return_value = _logger_tuple()
    mock_run.return_value = "finished"

    # Drive the bare 'Session ended' branch (interactive, exit on first prompt).
    with patch('builtins.input', side_effect=["exit"]):
        agent.run_agent_interactive(tui=False)

    assert mock_shutdown.call_count == 1
