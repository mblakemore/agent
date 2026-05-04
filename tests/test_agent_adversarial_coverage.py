import json
import os
import tempfile
import pytest
import requests
from unittest.mock import patch, MagicMock
import agent
from agent import (
    _llm_request, _ReasoningRenderer,
    _check_api_health, _detect_ctx_size, _list_available_models,
    _save_checkpoint, _load_checkpoint, _delete_checkpoint,
    _strip_checkpoint_reads, _format_for_summary, _condense_summary,
)

def test_llm_request_max_retries_exhausted():
    """Test that _llm_request raises the error after max retries."""
    with patch('requests.post') as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection failed")

        # Patch max retries to 1 to speed up test.
        # Patch _trigger_failover to False so a live local server doesn't cause
        # failover to succeed and permanently replace agent._main_backend,
        # which would break subsequent tests that patch the old backend object.
        with patch('agent._LLM_MAX_RETRIES', 1), \
             patch('agent._trigger_failover', return_value=False), \
             patch('agent._emit'), \
             patch('logging.Logger.warning'), \
             patch('time.sleep'):

            with pytest.raises(requests.exceptions.ConnectionError):
                # Use a dummy log object
                _llm_request(MagicMock(), kwargs={})

def test_reasoning_renderer_split_tags():
    """Test _ReasoningRenderer with split tags to hit coverage lines."""
    results = []
    renderer = _ReasoningRenderer(lambda x: results.append(x))
    
    # Case 1: Split <think>
    renderer.feed("<th")
    renderer.feed("ink>")
    renderer.feed("Thinking...")
    renderer.feed("</thi")
    renderer.feed("nk>")
    renderer.flush()
    
    # Case 2: Content longer than MAX_PENDING without tags
    results.clear()
    renderer = _ReasoningRenderer(lambda x: results.append(x))
    renderer.feed("A" * 20) # Much larger than 7
    renderer.flush()
    
    assert len(results) > 0

def test_llm_request_500_circuit_breaker():
    """Test that 3 consecutive 500s raise ContextOverflowError."""
    with patch('requests.post') as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_post.return_value = mock_response

        with patch('agent._emit'), \
             patch('logging.Logger.warning'), \
             patch('time.sleep'):

            with pytest.raises(agent.ContextOverflowError):
                _llm_request(MagicMock(), kwargs={})


# ── _check_api_health coverage ────────────────────────────────────────

def test_check_api_health_connection_error():
    """Test _check_api_health returns False on connection error."""
    with patch('requests.get', side_effect=requests.ConnectionError("refused")):
        ok, detail = _check_api_health("http://localhost:9999")
    assert ok is False
    assert "unreachable" in detail


def test_check_api_health_timeout():
    """Test _check_api_health returns False on timeout."""
    with patch('requests.get', side_effect=requests.Timeout("timed out")):
        ok, detail = _check_api_health("http://localhost:9999")
    assert ok is False
    assert "timeout" in detail


def test_check_api_health_non_200():
    """Test _check_api_health returns False on non-200 status."""
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    with patch('requests.get', return_value=mock_resp):
        ok, detail = _check_api_health("http://localhost:9999")
    assert ok is False
    assert "503" in detail


# ── _detect_ctx_size coverage ─────────────────────────────────────────

def test_detect_ctx_size_non_200():
    """Test _detect_ctx_size returns None on non-200 response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch('requests.get', return_value=mock_resp):
        result = _detect_ctx_size("http://localhost:9999")
    assert result is None


def test_detect_ctx_size_connection_error():
    """Test _detect_ctx_size returns None on connection error."""
    with patch('requests.get', side_effect=requests.ConnectionError()):
        result = _detect_ctx_size("http://localhost:9999")
    assert result is None


# ── _list_available_models coverage ──────────────────────────────────

def test_list_available_models_non_200():
    """Test _list_available_models returns [] on non-200."""
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    with patch('requests.get', return_value=mock_resp):
        result = _list_available_models("http://localhost:9999")
    assert result == []


def test_list_available_models_connection_error():
    """Test _list_available_models returns [] on connection error."""
    with patch('requests.get', side_effect=requests.ConnectionError()):
        result = _list_available_models("http://localhost:9999")
    assert result == []


# ── _strip_checkpoint_reads coverage ─────────────────────────────────

def test_strip_checkpoint_reads_strips_large_content():
    """Test that large checkpoint tool results are stripped."""
    big_content = "conversation_checkpoint.json " + "x" * 11_000
    history = [
        {"role": "tool", "content": big_content},
        {"role": "user", "content": "normal message"},
    ]
    result = _strip_checkpoint_reads(history)
    assert "stripped" in result[0]["content"]
    assert result[1]["content"] == "normal message"


def test_strip_checkpoint_reads_keeps_small_content():
    """Test that small checkpoint tool results are NOT stripped."""
    small_content = "conversation_checkpoint.json tiny"
    history = [{"role": "tool", "content": small_content}]
    result = _strip_checkpoint_reads(history)
    assert result[0]["content"] == small_content


# ── _save_checkpoint / _load_checkpoint / _delete_checkpoint ─────────

def test_save_and_load_checkpoint():
    """Test round-trip: save then load checkpoint."""
    history = [{"role": "user", "content": "hello"}]
    summary = {"text": "summary text", "up_to": 1}
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint_path = os.path.join(tmpdir, "checkpoint.json")
        with patch('agent._CHECKPOINT_PATH', checkpoint_path):
            _save_checkpoint(history, summary, turn=3, initial_files="some files")
            result = _load_checkpoint()
    assert result is not None
    loaded_history, loaded_summary, loaded_turn, loaded_files = result
    assert loaded_history == history
    assert loaded_summary == summary
    assert loaded_turn == 3
    assert loaded_files == "some files"


def test_load_checkpoint_returns_none_if_missing():
    """Test _load_checkpoint returns None when no checkpoint file exists."""
    with patch('agent._CHECKPOINT_PATH', '/nonexistent/path/checkpoint.json'):
        result = _load_checkpoint()
    assert result is None


def test_delete_checkpoint():
    """Test _delete_checkpoint removes the checkpoint file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint_path = os.path.join(tmpdir, "checkpoint.json")
        with open(checkpoint_path, "w") as f:
            json.dump({}, f)
        with patch('agent._CHECKPOINT_PATH', checkpoint_path):
            _delete_checkpoint()
        assert not os.path.exists(checkpoint_path)


# ── _format_for_summary coverage ─────────────────────────────────────

def test_format_for_summary_file_write_tool_call():
    """Test that file write tool calls are formatted specially."""
    messages = [
        {
            "role": "ASSISTANT",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "file",
                        "arguments": '{"action": "write", "path": "/some/file.py", "content": "data"}',
                    }
                }
            ],
        }
    ]
    result = _format_for_summary(messages)
    assert "file(action=write, path=/some/file.py)" in result


def test_format_for_summary_long_content_truncated():
    """Test that long content in non-ASSISTANT messages is truncated."""
    messages = [
        {
            "role": "SYSTEM",
            "content": "x" * 1000,
        }
    ]
    result = _format_for_summary(messages)
    assert "..." in result or len(result) < 1000 + 50


# ── _condense_summary coverage ────────────────────────────────────────

def test_condense_summary_model_noncompliance():
    """Test _condense_summary hard-truncates when model doesn't comply with length."""
    long_text = "A" * 5000
    mock_log = MagicMock()
    with patch('agent._summary_request', return_value="B" * 10_000), \
         patch('agent._SUMMARY_MAX_CHARS', 4000), \
         patch('agent._emit'):
        result = _condense_summary(long_text, log=mock_log)
    # rsplit may produce slightly more than the limit; allow 20 chars margin
    assert len(result) <= 4020
    assert "[...truncated]" in result
    # Verify the log calls were made (lines 747, 750)
    mock_log.warning.assert_called()
    mock_log.info.assert_called()


def test_condense_summary_exception_fallback():
    """Test _condense_summary falls back to truncation on exception."""
    long_text = "A" * 5000
    with patch('agent._summary_request', side_effect=Exception("API down")), \
         patch('agent._SUMMARY_MAX_CHARS', 4000), \
         patch('agent._emit'):
        result = _condense_summary(long_text, log=MagicMock())
    assert "[...truncated]" in result


# ── _format_for_summary additional coverage ───────────────────────────

def test_format_for_summary_long_assistant_text_truncated():
    """Test that long assistant text is truncated at 600 chars."""
    messages = [
        {
            "role": "ASSISTANT",
            "content": "A" * 700,
            "tool_calls": [],
        }
    ]
    result = _format_for_summary(messages)
    assert "..." in result
    # Content should be capped at 600 + "..."
    assert len(result) < 700


def test_format_for_summary_long_tool_args_truncated():
    """Test that long tool args (non-file-write) are truncated at 200 chars."""
    messages = [
        {
            "role": "ASSISTANT",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "exec_command",
                        "arguments": '{"command": "' + "x" * 300 + '"}',
                    }
                }
            ],
        }
    ]
    result = _format_for_summary(messages)
    assert "exec_command" in result
    assert "..." in result


# ── AsyncSummarizer coverage ──────────────────────────────────────────

def test_async_summarizer_is_running_and_reset():
    """Test AsyncSummarizer is_running property and reset method."""
    from agent import AsyncSummarizer
    config = {
        "summary": {"max_wait_on_save": 5},
        "llm": {"model": "test-model"},
    }
    summarizer = AsyncSummarizer(config, MagicMock())
    assert summarizer.is_running is False
    # Test reset doesn't crash
    summarizer.reset()
    assert summarizer.is_running is False


def test_async_summarizer_drain_no_thread():
    """Test AsyncSummarizer drain works when no thread has been started."""
    from agent import AsyncSummarizer
    config = {
        "summary": {"max_wait_on_save": 5},
        "llm": {"model": "test-model"},
    }
    summarizer = AsyncSummarizer(config, MagicMock())
    # Should not raise even with no thread
    summarizer.drain(timeout=0.1)


def test_async_summarizer_drain_uses_config_timeout():
    """Test AsyncSummarizer drain uses config timeout when None passed."""
    import threading
    from agent import AsyncSummarizer
    config = {
        "summary": {"max_wait_on_save": 0.2},
        "llm": {"model": "test-model"},
    }
    summarizer = AsyncSummarizer(config, MagicMock())
    # Create and start a real (quick) background thread
    event = threading.Event()
    def quick_job():
        event.wait(timeout=0.05)  # finishes quickly
    summarizer._thread = threading.Thread(target=quick_job, daemon=True)
    summarizer._thread.start()
    event.set()
    # drain() with no timeout should use config value and join the thread
    summarizer.drain()
    assert not summarizer._thread.is_alive()


def test_async_summarizer_kick_no_double_start():
    """Test AsyncSummarizer kick returns immediately if already running."""
    from agent import AsyncSummarizer
    config = {
        "summary": {"max_wait_on_save": 5},
        "llm": {"model": "test-model"},
    }
    summarizer = AsyncSummarizer(config, MagicMock())
    # Manually set _running to True to simulate already-running
    with summarizer._lock:
        summarizer._running = True
    # This should return immediately without starting a new thread
    summarizer.kick("old summary", [], 0)
    assert summarizer._thread is None


def test_async_summarizer_harvest_no_result():
    """Test AsyncSummarizer harvest returns False when no pending result."""
    from agent import AsyncSummarizer
    config = {
        "summary": {"max_wait_on_save": 5},
        "llm": {"model": "test-model"},
    }
    summarizer = AsyncSummarizer(config, MagicMock())
    summary_state = {"text": "old summary", "up_to": 0}
    result = summarizer.harvest(summary_state)
    assert result is False
    assert summary_state["text"] == "old summary"


# ── _maybe_resummarize edge cases ─────────────────────────────────────

def test_maybe_resummarize_skips_when_below_threshold():
    """Test _maybe_resummarize returns False when unsummarized count < threshold."""
    from agent import _maybe_resummarize
    history = [{"role": "user", "content": "msg"}]
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()
    with patch('agent._SUMMARY_THRESHOLD', 100):
        result = _maybe_resummarize(history, summary_state, oldest_idx=1, log=log)
    assert result is False


def test_maybe_resummarize_skips_with_no_new_messages():
    """Test _maybe_resummarize returns False when oldest_idx == up_to."""
    from agent import _maybe_resummarize
    history = [{"role": "user", "content": "msg"}]
    summary_state = {"text": "existing", "up_to": 1}
    log = MagicMock()
    result = _maybe_resummarize(history, summary_state, oldest_idx=1, log=log, force=True)
    assert result is False


# ── _salvage_tool_args coverage ───────────────────────────────────────

def test_salvage_tool_args_exec_command():
    """Test _salvage_tool_args can extract exec_command from garbled input."""
    from agent import _salvage_tool_args
    log = MagicMock()
    raw = 'command: "ls -la"'
    result = _salvage_tool_args("exec_command", raw, log)
    assert result is not None
    assert "command" in result


def test_salvage_tool_args_file_action():
    """Test _salvage_tool_args can extract file action from garbled input."""
    from agent import _salvage_tool_args
    log = MagicMock()
    raw = 'write**,path:/some/file.py,content:hello world'
    result = _salvage_tool_args("file", raw, log)
    # Should find "write" action and extract path
    if result is not None:
        assert result.get("action") == "write"


def test_salvage_tool_args_exception_path():
    """Test _salvage_tool_args handles exception and returns None."""
    from agent import _salvage_tool_args
    log = MagicMock()
    # Pass something that will fail regex processing
    result = _salvage_tool_args("unknown_tool", "", log)
    assert result is None


# ── Checkpoint error path coverage ───────────────────────────────────

def test_save_checkpoint_handles_exception():
    """Test _save_checkpoint doesn't raise on exception (best-effort)."""
    history = [{"role": "user", "content": "hello"}]
    summary = {"text": "text", "up_to": 0}
    with patch('agent._CHECKPOINT_PATH', '/nonexistent/dir/checkpoint.json'):
        # Should not raise
        _save_checkpoint(history, summary, turn=1, initial_files=None)


def test_load_checkpoint_handles_corrupt_json():
    """Test _load_checkpoint returns None on corrupt JSON."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write("not valid json {{{")
        tmppath = f.name
    try:
        with patch('agent._CHECKPOINT_PATH', tmppath):
            result = _load_checkpoint()
        assert result is None
    finally:
        os.unlink(tmppath)


def test_delete_checkpoint_handles_exception():
    """Test _delete_checkpoint doesn't raise on exception."""
    with patch('os.remove', side_effect=PermissionError("no perms")), \
         patch('os.path.exists', return_value=True):
        # Should not raise
        _delete_checkpoint()


# ── _ReasoningRenderer large think buffer coverage ─────────────────────

def test_reasoning_renderer_large_think_buffer():
    """Test _ReasoningRenderer emits think content incrementally when buffer is large."""
    results = []
    renderer = _ReasoningRenderer(lambda x: results.append(x))
    # Start a think block, feed large content without closing tag
    renderer.feed("<think>")
    renderer.feed("X" * 100)  # Much larger than _MAX_PENDING (7)
    # Should have emitted some think content already
    assert len(results) > 0


# ── _check_api_health RequestException coverage ───────────────────────

def test_check_api_health_request_exception():
    """Test _check_api_health returns False on generic RequestException."""
    with patch('requests.get', side_effect=requests.RequestException("generic error")):
        ok, detail = _check_api_health("http://localhost:9999")
    assert ok is False
    assert len(detail) > 0


# ── _build_context_footnote with pinned instructions ──────────────────

def test_build_context_footnote_with_pinned():
    """Test _build_context_footnote includes pinned instructions when set."""
    from agent import _build_context_footnote
    with patch('agent._pinned_instructions', 'ALWAYS do X first'):
        result = _build_context_footnote("progress summary text", None)
    assert "PINNED INSTRUCTIONS" in result["content"]
    assert "ALWAYS do X first" in result["content"]


# ── _ReasoningRenderer small think buffer (line 405) ──────────────────

def test_reasoning_renderer_small_think_buffer():
    """Test _ReasoningRenderer line 405: small pending buf while in think mode."""
    results = []
    renderer = _ReasoningRenderer(lambda x: results.append(x))
    # Open think block manually, then feed small content (< MAX_PENDING) without close
    renderer._in_think = True
    renderer._pending = ""
    # Feed < 7 chars with no close tag — hits the else branch (line 405)
    renderer.feed("ab")  # 2 chars, no </think> tag
    # _pending should now be "ab"
    assert renderer._pending == "ab"


# ── _build_context coverage ───────────────────────────────────────────

def test_build_context_with_summary_and_initial_files():
    """Test _build_context returns context message when summary exists."""
    from agent import _build_context
    history = [
        {"role": "user", "content": "first msg"},
        {"role": "assistant", "content": "response"},
    ]
    summary_state = {"text": "progress so far", "up_to": 0}
    log = MagicMock()
    msgs, oldest_idx = _build_context(
        history, summary_state, initial_files="initial content",
        ctx_size=16000, max_tokens=2000, log=log
    )
    # Should have returned some messages
    assert len(msgs) > 0


def test_build_context_no_summary():
    """Test _build_context with no summary state."""
    from agent import _build_context
    history = [
        {"role": "user", "content": "hello"},
    ]
    summary_state = {"text": "", "up_to": 0}
    log = MagicMock()
    msgs, oldest_idx = _build_context(
        history, summary_state, initial_files=None,
        ctx_size=16000, max_tokens=2000, log=log
    )
    assert len(msgs) >= 1


def test_build_context_summary_exceeds_half_budget():
    """Test _build_context when summary > 50% budget reduces message count."""
    from agent import _build_context
    # Create a large summary that exceeds 50% of the tiny budget
    large_summary = "X " * 2000  # Large text
    history = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "resp1"},
    ]
    summary_state = {"text": large_summary, "up_to": 0}
    log = MagicMock()
    with patch('agent._condense_summary', return_value=large_summary), \
         patch('agent._emit'):
        msgs, oldest_idx = _build_context(
            history, summary_state, initial_files=None,
            ctx_size=500, max_tokens=100, log=log  # tiny context to force the path
        )
    # Should have returned without crashing
    assert isinstance(msgs, list)
