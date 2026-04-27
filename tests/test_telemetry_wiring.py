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


# ──────────────────────────────────────────────────────────────────────
# Issue #401 — token / error / verbose-turn wiring inside run_agent_single
# ──────────────────────────────────────────────────────────────────────

import json
from unittest.mock import MagicMock as _MM


def _stream_with_usage(prompt_tokens=11, completion_tokens=22, model="test-model"):
    """Build a mock streaming Response that yields one content chunk plus
    a final usage chunk in OpenAI streaming shape (llamacpp/SSE form).

    Returned object has ``iter_lines`` so it's recognised by
    ``_iter_stream_chunks`` as the legacy Response shape.
    """
    content_chunk = {"choices": [{"delta": {"content": "ok. Cycle complete."}}]}
    usage_chunk = {
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    lines = [
        b"data: " + json.dumps(content_chunk).encode("utf-8"),
        b"data: " + json.dumps(usage_chunk).encode("utf-8"),
        b"data: [DONE]",
    ]
    resp = _MM()
    resp.status_code = 200
    resp.iter_lines.return_value = lines
    return resp


@patch('agent.telemetry.record_tokens')
@patch('agent._llm_request')
def test_record_tokens_prompt_and_completion(mock_llm, mock_record_tokens):
    """Streaming usage chunk drives `record_tokens` for prompt AND completion."""
    mock_llm.return_value = _stream_with_usage(prompt_tokens=11, completion_tokens=22)

    history = []
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], _MM())

    kinds = [c.args[1] if len(c.args) >= 2 else c.kwargs.get("kind")
             for c in mock_record_tokens.call_args_list]
    counts = [c.args[2] if len(c.args) >= 3 else c.kwargs.get("n")
              for c in mock_record_tokens.call_args_list]
    assert "prompt" in kinds, f"prompt kind missing in {mock_record_tokens.call_args_list}"
    assert "completion" in kinds, f"completion kind missing in {mock_record_tokens.call_args_list}"
    assert 11 in counts and 22 in counts, f"counts wrong: {counts}"


@patch('agent.telemetry.verbose_enabled', return_value=False)
@patch('agent.telemetry.record_turn')
@patch('agent._llm_request')
def test_verbose_disabled_skips_record_turn(mock_llm, mock_record_turn, _verbose):
    """When verbose telemetry is off, record_turn is NOT called."""
    mock_llm.return_value = _stream_with_usage()

    history = []
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], _MM())

    assert mock_record_turn.call_count == 0


@patch('agent.telemetry.verbose_enabled', return_value=True)
@patch('agent.telemetry.record_turn')
@patch('agent._llm_request')
def test_verbose_enabled_records_main_turn(mock_llm, mock_record_turn, _verbose):
    """With verbose enabled, record_turn fires once per turn iteration with role='main'."""
    mock_llm.return_value = _stream_with_usage()

    history = []
    agent.run_agent_single(history, {"text": "", "up_to": 0}, [], _MM())

    assert mock_record_turn.call_count >= 1, "record_turn never fired"
    # At least one call must use role="main"
    main_calls = [c for c in mock_record_turn.call_args_list
                  if c.kwargs.get("role") == "main"]
    assert main_calls, f"no role='main' call in {mock_record_turn.call_args_list}"
    # Each main call must carry a non-negative duration_s and an int tool_calls
    for c in main_calls:
        assert isinstance(c.kwargs.get("duration_s"), float)
        assert c.kwargs["duration_s"] >= 0.0
        assert isinstance(c.kwargs.get("tool_calls"), int)


@patch('agent.telemetry.record_error')
@patch('agent._llm_request')
def test_record_error_fires_on_request_exception(mock_llm, mock_record_error):
    """A RequestException at the request site triggers record_error with the class name."""
    import requests as _rq

    class _FakeTimeout(_rq.exceptions.RequestException):
        pass

    mock_llm.side_effect = _FakeTimeout("simulated timeout")

    history = []
    result = agent.run_agent_single(history, {"text": "", "up_to": 0}, [], _MM())

    assert result == "error"
    assert mock_record_error.call_count >= 1
    # `kind` is the simple class name of the raised exception
    kinds = [c.kwargs.get("kind") for c in mock_record_error.call_args_list]
    assert "_FakeTimeout" in kinds, f"expected '_FakeTimeout' in {kinds}"


@patch('agent.telemetry.record_tokens')
def test_record_tokens_uses_input_output_aliases(mock_record_tokens):
    """Bedrock-style usage with input_tokens/output_tokens aliases also records."""
    # Hit the chunk site directly via _iter_stream_chunks to keep the test tight.
    # Build a generator-of-dicts (Bedrock shape) and run the same loop logic by
    # calling run_agent_single with a mocked _llm_request returning that shape.
    chunks = iter([
        {"choices": [{"delta": {"content": "done"}}]},
        {
            "choices": [],
            "usage": {"input_tokens": 7, "output_tokens": 9},
        },
    ])

    with patch('agent._llm_request', return_value=chunks):
        agent.run_agent_single([], {"text": "", "up_to": 0}, [], _MM())

    counts = [c.args[2] if len(c.args) >= 3 else c.kwargs.get("n")
              for c in mock_record_tokens.call_args_list]
    assert 7 in counts and 9 in counts, f"alias counts missing: {counts}"
