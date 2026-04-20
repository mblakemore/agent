import pytest
import json
import os
from unittest.mock import patch, mock_open
from agent import _extract_pinned, _is_read_only_command, _calculate_retry_delay, _load_config, _build_context

def test_extract_pinned_no_pinned():
    text = "This is a normal message without any pinned instructions."
    cleaned, pinned = _extract_pinned(text)
    assert cleaned == "This is a normal message without any pinned instructions."
    assert pinned == ""

def test_extract_pinned_single_block():
    text = "Hello <pinned>Keep this important\ninstruction</pinned> World"
    cleaned, pinned = _extract_pinned(text)
    assert cleaned == "Hello  World"
    assert pinned == "Keep this important\ninstruction"

def test_extract_pinned_multiple_blocks():
    text = "Start <pinned>Block 1</pinned> Middle <pinned>Block 2</pinned> End"
    cleaned, pinned = _extract_pinned(text)
    assert cleaned == "Start  Middle  End"
    assert pinned == "Block 1\nBlock 2"

def test_extract_pinned_whitespace_handling():
    text = "  <pinned>  Trim me  </pinned>  "
    cleaned, pinned = _extract_pinned(text)
    assert cleaned == ""
    assert pinned == "Trim me"

@pytest.mark.parametrize("cmd, expected", [
    ("ls -la", True),
    ("cat file.txt", True),
    ("git status", True),
    ("grep 'foo' file.txt", True),
    ("git commit -m 'fix'", False),
    ("rm -rf /tmp/test", False),
    ("cat > file.txt", True),
    ("mkdir new_dir", False),
    ("gh pr create", False),
    ("python3 -m unittest", True),
    ("", True),
    ("   ", True),
])
def test_is_read_only_command(cmd, expected):
    assert _is_read_only_command(cmd) == expected

def test_calculate_retry_delay():
    from agent import _LLM_BASE_DELAY, _LLM_BACKOFF_MULTIPLIER, _LLM_JITTER_FACTOR
    val = _calculate_retry_delay(0)
    expected_base = _LLM_BASE_DELAY * (_LLM_BACKOFF_MULTIPLIER ** 0)
    jitter_range = expected_base * _LLM_JITTER_FACTOR
    assert expected_base - jitter_range - 0.01 <= val <= expected_base + jitter_range + 0.01

def test_load_config_no_file():
    with patch("agent.Path.exists", return_value=False):
        config = _load_config()
        assert config["llm"]["model"] == "gemma-4-31B"

def test_load_config_valid_file():
    mock_config = {"llm": {"model": "custom-model"}}
    with patch("agent.Path.exists", return_value=True):
        with patch("builtins.open", mock_open(read_data=json.dumps(mock_config))):
            config = _load_config()
            assert config["llm"]["model"] == "custom-model"

def test_load_config_invalid_json():
    with patch("agent.Path.exists", return_value=True):
        with patch("builtins.open", mock_open(read_data="not-json")):
            config = _load_config()
            assert config["llm"]["model"] == "gemma-4-31B"

import requests
from agent import _check_api_health, _detect_ctx_size, _list_available_models, _render_context_bar

@pytest.mark.parametrize("status_code, expected_ok, expected_detail", [
    (200, True, "ok"),
    (404, False, "HTTP 404"),
    (500, False, "HTTP 500"),
])
def test_check_api_health_responses(status_code, expected_ok, expected_detail):
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = status_code
        ok, detail = _check_api_health("http://api", timeout=1)
        assert ok == expected_ok
        assert detail == expected_detail

def test_check_api_health_timeout():
    with patch("requests.get", side_effect=requests.Timeout):
        ok, detail = _check_api_health("http://api", timeout=1)
        assert ok is False
        assert detail == "timeout"

def test_check_api_health_connection_error():
    with patch("requests.get", side_effect=requests.ConnectionError):
        ok, detail = _check_api_health("http://api", timeout=1)
        assert ok is False
        assert detail == "unreachable"

def test_detect_ctx_size_success():
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{"n_ctx": 32768}]
        assert _detect_ctx_size("http://api") == 32768

def test_detect_ctx_size_failure():
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 500
        assert _detect_ctx_size("http://api") is None

def test_list_available_models_success():
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "data": [
                {"id": "model-1"},
                {"id": "model-2"},
                {"id": "model-3"},
            ]
        }
        assert _list_available_models("http://api") == ["model-1", "model-2", "model-3"]

def test_list_available_models_failure():
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        assert _list_available_models("http://api") == []

def test_render_context_bar():
    with patch("agent._estimate_tokens", return_value=10):
        history = [{"role": "user", "content": "test"}]
        summary_state = {"text": "sum"}
        ctx_size = 100
        result = _render_context_bar(history, summary_state, ctx_size)
        assert "history: 10 tokens" in result
        assert "summary: 10 tokens" in result
        assert "budget:  20 / 100 tokens" in result

@pytest.mark.parametrize("history, budget_size, summary_text, initial_files, expected_first_msg_role", [
    # 1. Standard: budget is plenty, user msg present.
    ([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], 10000, "", None, "user"),
    # 2. User Intent Loss: budget small, recent are assistant, must find oldest user.
    ([{"role": "user", "content": "start"}, {"role": "assistant", "content": "a1"}, {"role": "assistant", "content": "a2"}], 100, "", None, "user"),
    # 3. Context Message Injection: summary present, oldest_idx > 0.
    ([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], 10000, "Summary of work", None, "user"),
    # 4. Initial Files: No user in window, initial_files provided.
    ([{"role": "assistant", "content": "a1"}], 100, "", "file_content", "user"),
    # 5. No User’s: No user in window, no initial_files.
    ([{"role": "assistant", "content": "a1"}], 100, "", None, "assistant"),
])
def test_build_context_logic(history, budget_size, summary_text, initial_files, expected_first_msg_role):
    with patch("agent._estimate_tokens", return_value=10):
        summary_state = {"text": summary_text}
        ctx_size = budget_size + 10000 
        # Use a mock for the logger
        mock_log = patch("logging.Logger").start()
        selected, oldest_idx = _build_context(history, summary_state, initial_files, ctx_size, 2048, mock_log)
        assert selected[0]["role"] == expected_first_msg_role
        patch.stopall()

