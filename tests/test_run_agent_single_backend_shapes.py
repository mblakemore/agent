"""Regression test: ``run_agent_single`` consumes both backend return shapes.

Phase 2.5 added ``_iter_stream_chunks`` in ``agent.py`` so the main SSE
consumer can handle:

  (a) Legacy ``requests.Response`` shape — what ``LlamacppBackend.stream_chat``
      returns today and what every existing ``_llm_request`` test mock uses
      (``resp.iter_lines.return_value = [b"data: {...}", b"data: [DONE]"]``).
  (b) Iterator-of-dicts shape — what ``BedrockBackend.stream_chat`` yields.

Without this adapter, enabling ``backends.main.kind: "bedrock"`` would break
at ``response.iter_lines(...)`` on the first turn. This test pins the
contract so nobody can regress ``_iter_stream_chunks`` silently.
"""

import json
import logging
from unittest.mock import MagicMock, patch

from agent import run_agent_single

log = logging.getLogger("test_run_agent_single_backend_shapes")


def _sse_mock_response(dicts):
    """Legacy shape — mock ``requests.Response`` with iter_lines."""
    resp = MagicMock()
    lines = [f"data: {json.dumps(d)}".encode() for d in dicts]
    lines.append(b"data: [DONE]")
    resp.iter_lines.return_value = lines
    resp.status_code = 200
    return resp


def _iter_mock_response(dicts):
    """Bedrock shape — plain generator of dict chunks, no iter_lines."""
    def gen():
        yield from dicts
    return gen()


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_run_agent_single_consumes_legacy_response_shape(
    mock_config, mock_llm, mock_emit
):
    """LlamacppBackend-style Response with iter_lines → still works."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "presence_penalty": 0.0,
        },
        "context": {"max_tokens": 4096, "ctx_size": 32768},
    }.get(k)

    mock_llm.return_value = _sse_mock_response([
        {"choices": [{"delta": {"content": "four two"}}]},
    ])

    result = run_agent_single(
        [{"role": "user", "content": "what's the answer"}],
        {"text": "", "up_to": 0},
        [],
        log,
    )
    # With text-only response and no nudge loop, the function returns
    # normally (not "cancelled" / "error"). Emit was called at least
    # once for the stream-chunk callback.
    assert mock_emit.called
    # Should not be an error return.
    assert result not in {"cancelled", "error"}


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_run_agent_single_consumes_iterator_of_dicts_shape(
    mock_config, mock_llm, mock_emit
):
    """BedrockBackend-style generator of dicts → also works (the Phase 2.5 gap)."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "presence_penalty": 0.0,
        },
        "context": {"max_tokens": 4096, "ctx_size": 32768},
    }.get(k)

    # Generator yielding OpenAI delta dicts — no iter_lines, no status_code.
    mock_llm.return_value = _iter_mock_response([
        {"choices": [{"delta": {"content": "four two"}}]},
    ])

    result = run_agent_single(
        [{"role": "user", "content": "what's the answer"}],
        {"text": "", "up_to": 0},
        [],
        log,
    )
    assert mock_emit.called
    assert result not in {"cancelled", "error"}


@patch("agent._emit")
@patch("agent._llm_request")
@patch("agent._config")
def test_run_agent_single_tool_call_via_iterator_shape(
    mock_config, mock_llm, mock_emit
):
    """Generator-of-dicts shape carries a tool_call delta end-to-end."""
    mock_config.__getitem__.side_effect = lambda k: {
        "llm": {"model": "test-model"},
        "generation": {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "presence_penalty": 0.0,
        },
        "context": {"max_tokens": 4096, "ctx_size": 32768},
    }.get(k)

    tool_call = {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"name": "search_files", "arguments": '{"pattern": "x"}'},
    }

    # First turn: a tool_call delta via generator shape. Second turn:
    # plain text ("done"). The agent loop should execute the stubbed tool
    # then return.
    mock_llm.side_effect = [
        _iter_mock_response([
            {"choices": [{"delta": {"tool_calls": [tool_call]}}]},
        ]),
        _iter_mock_response([
            {"choices": [{"delta": {"content": "done"}}]},
        ]),
    ]

    with patch.dict("agent.MAP_FN", {"search_files": lambda **kw: "no matches"}):
        result = run_agent_single(
            [{"role": "user", "content": "search x"}],
            {"text": "", "up_to": 0},
            [],
            log,
        )

    assert result not in {"cancelled", "error"}
    # Must have made both turns — first to emit tool_call, second to finish.
    assert mock_llm.call_count >= 2
