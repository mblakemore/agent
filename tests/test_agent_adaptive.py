"""Tests for adaptive max_tokens injection in _run_turn() (issue #546).

Verifies that when the main backend is Bedrock, the request body's
max_tokens is set according to turn complexity, and that the feature
can be disabled via config.
"""

import pytest
from unittest.mock import patch, MagicMock
import agent


def _make_stream(content="done"):
    """Minimal stream response for run_agent_single."""
    return [{"choices": [{"delta": {"content": content}}]}]


def _run_with_mock_llm(mock_llm, message="hi"):
    """Run a single-turn agent session with the given mock LLM."""
    conversation_history = []
    summary_state = {"text": "", "up_to": 0}
    initial_files = {}
    log = MagicMock()

    agent.run_agent_single(
        conversation_history=conversation_history,
        summary_state=summary_state,
        initial_files=initial_files,
        log=log,
        start_turn=0,
    )


def _get_request_body(mock_llm):
    """Extract the JSON request body from the first _llm_request call."""
    assert mock_llm.call_count >= 1, "expected _llm_request to be called at least once"
    call = mock_llm.call_args_list[0]
    body = call.kwargs.get("json") or (call.args[1] if len(call.args) >= 2 else None)
    assert isinstance(body, dict), f"expected dict request body, got {type(body)}"
    return body


# ---------------------------------------------------------------------------
# Tests 1 & 2: adaptive fires for Bedrock, sets correct budget
# ---------------------------------------------------------------------------

@patch("agent._llm_request")
@patch("agent._main_backend")
def test_adaptive_max_tokens_simple(mock_backend, mock_llm):
    """Simple message ('hi') with Bedrock backend → max_tokens == 512."""
    mock_backend.kind = "bedrock"
    mock_backend.model = "claude-v4.6-opus"
    mock_llm.return_value = _make_stream("done")

    with patch.dict("agent._config", {"bedrock": {"adaptive_max_tokens": True}}, clear=False):
        _run_with_mock_llm(mock_llm)

    body = _get_request_body(mock_llm)
    assert body["max_tokens"] == 512, (
        f"expected 512 for simple claude turn, got {body['max_tokens']}"
    )


@patch("agent._llm_request")
@patch("agent._main_backend")
def test_adaptive_max_tokens_extended(mock_backend, mock_llm):
    """Message containing 'refactor' keyword → classified extended → max_tokens == 4096."""
    mock_backend.kind = "bedrock"
    mock_backend.model = "claude-v4.6-opus"
    mock_llm.return_value = _make_stream("done")

    # Inject a user message with an extended keyword into conversation history
    conversation_history = [{"role": "user", "content": "please refactor the auth module"}]
    summary_state = {"text": "", "up_to": 0}
    initial_files = {}
    log = MagicMock()

    with patch.dict("agent._config", {"bedrock": {"adaptive_max_tokens": True}}, clear=False):
        agent.run_agent_single(
            conversation_history=conversation_history,
            summary_state=summary_state,
            initial_files=initial_files,
            log=log,
            start_turn=0,
        )

    body = _get_request_body(mock_llm)
    assert body["max_tokens"] == 4096, (
        f"expected 4096 for extended claude turn, got {body['max_tokens']}"
    )


# ---------------------------------------------------------------------------
# Test 3: feature disabled via config
# ---------------------------------------------------------------------------

@patch("agent._llm_request")
@patch("agent._main_backend")
def test_adaptive_disabled_by_config(mock_backend, mock_llm):
    """When adaptive_max_tokens=False, max_tokens is the default (unchanged)."""
    mock_backend.kind = "bedrock"
    mock_backend.model = "claude-v4.6-opus"
    mock_llm.return_value = _make_stream("done")

    default_max_tokens = agent._DEFAULT_CONFIG["context"]["max_tokens"]

    with patch.dict("agent._config", {"bedrock": {"adaptive_max_tokens": False}}, clear=False):
        _run_with_mock_llm(mock_llm)

    body = _get_request_body(mock_llm)
    assert body["max_tokens"] == default_max_tokens, (
        f"expected default {default_max_tokens} when disabled, got {body['max_tokens']}"
    )


# ---------------------------------------------------------------------------
# Test 4: adaptive only fires for Bedrock backends
# ---------------------------------------------------------------------------

@patch("agent._llm_request")
@patch("agent._main_backend")
def test_adaptive_only_fires_for_bedrock(mock_backend, mock_llm):
    """llamacpp backend → adaptive injection does NOT run → max_tokens unchanged."""
    mock_backend.kind = "llamacpp"
    mock_backend.model = "gemma-4-31B"
    mock_llm.return_value = _make_stream("done")

    default_max_tokens = agent._DEFAULT_CONFIG["context"]["max_tokens"]

    with patch.dict("agent._config", {"bedrock": {"adaptive_max_tokens": True}}, clear=False):
        _run_with_mock_llm(mock_llm)

    body = _get_request_body(mock_llm)
    assert body["max_tokens"] == default_max_tokens, (
        f"expected default {default_max_tokens} for llamacpp backend, got {body['max_tokens']}"
    )


# ---------------------------------------------------------------------------
# Tests 5 & 6: _get_adaptive_max_tokens unit tests
# ---------------------------------------------------------------------------

def test_get_adaptive_max_tokens_prefix_match():
    """claude-v4.6-opus starts with 'claude' → uses claude budget."""
    result = agent._get_adaptive_max_tokens("claude-v4.6-opus", "simple")
    assert result == 512, f"expected 512, got {result}"


def test_get_adaptive_max_tokens_unknown_model():
    """Unknown model prefix falls through to _default budget."""
    result = agent._get_adaptive_max_tokens("unknown-model-xyz", "extended")
    assert result == agent._COMPLEXITY_MAX_TOKENS["_default"]["extended"], (
        f"expected default extended budget, got {result}"
    )
