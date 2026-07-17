"""
Claude Code Gateway — Anthropic Messages API → agent.py backend proxy.

Exposes an Anthropic-native ``/v1/messages`` endpoint and translates each
request into the OpenAI chat-completions shape that agent.py's configured
backend speaks (``Backend.stream_chat``), then translates the streamed
response back into Anthropic SSE events. This lets Claude Code drive whichever
backend agent.py is configured for — llamacpp, bedrock, or foundry — without
caring which one it is.

Launched by ``agent.py -cc [host:port]``. The agent builds and configures
``_main_backend`` (honouring ``--backend-main`` overrides) and hands it here;
the gateway is a thin, *stateless* translator: Claude Code owns the agentic
loop and resends the full conversation history every turn, so the gateway
forces a fresh backend conversation per request. (BedrockBackend otherwise
reuses ``_active_conv_id`` across calls to keep context server-side — which
would double-feed the history Claude Code already includes.)

Both backend kinds yield OpenAI-shape delta dicts: llamacpp streams native
SSE, while BedrockBackend parses its dev-mode XML tool calls into ``tool_calls``
deltas internally. The gateway therefore treats backend output uniformly.

Usage:
    python3 agent.py -cc                 # 127.0.0.1:8788
    python3 agent.py -cc 0.0.0.0:9000    # explicit host:port

Then point Claude Code at it:
    export ANTHROPIC_BASE_URL=http://localhost:8788
    export ANTHROPIC_API_KEY=dummy
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import List, Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

log = logging.getLogger("cc-gateway")

# Default read timeout is generous — a local gemma backend can take minutes to
# finish a long turn, and Claude Code holds the stream open the whole time.
_CONNECT_TIMEOUT = int(os.environ.get("CC_GATEWAY_CONNECT_TIMEOUT", "30"))
_READ_TIMEOUT = int(os.environ.get("CC_GATEWAY_READ_TIMEOUT", "600"))

# Resilience knobs. A Bedrock proxy behind an API Gateway has a hard integration
# timeout that cuts the stream mid-response; as of 2026-07-16 that wall is 180s
# (raised from ~29s), measured cutting at 180.3s. Requests can also stall under
# transient load. Retrying is safe ONLY before any content has reached the client;
# once tokens are streaming we close gracefully instead. Worst-case added
# latency is ~N×(read timeout) + backoffs, so keep N small.
_MAX_RETRIES = int(os.environ.get("CC_GATEWAY_MAX_RETRIES", "2"))
_RETRY_BASE_DELAY = float(os.environ.get("CC_GATEWAY_RETRY_BASE_DELAY", "1.0"))
_RETRY_MAX_DELAY = float(os.environ.get("CC_GATEWAY_RETRY_MAX_DELAY", "8.0"))

try:  # never retry definite, non-transient failures
    from llm_backend import BedrockBudgetExceeded, ContextOverflowError
    _NON_RETRYABLE: tuple = (BedrockBudgetExceeded, ContextOverflowError)
except Exception:  # pragma: no cover - llm_backend is always importable in-repo
    _NON_RETRYABLE = ()

# OpenAI finish_reason → Anthropic stop_reason.
_STOP_MAP = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}

_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(len(text) // _CHARS_PER_TOKEN, 1)


# ---------------------------------------------------------------------------
# Anthropic request models
# ---------------------------------------------------------------------------
class AnthropicMessage(BaseModel):
    role: str
    content: Union[str, List[dict]]


class MessagesRequest(BaseModel):
    model: str = "claude"
    max_tokens: int = 4096
    messages: List[AnthropicMessage]
    system: Optional[Union[str, list]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop_sequences: Optional[List[str]] = None
    metadata: Optional[dict] = None
    tools: Optional[List[dict]] = None
    tool_choice: Optional[dict] = None


def _block_dict(block) -> dict:
    if isinstance(block, dict):
        return block
    return block.model_dump() if hasattr(block, "model_dump") else dict(block.__dict__)


# ---------------------------------------------------------------------------
# Anthropic  →  OpenAI request translation
# ---------------------------------------------------------------------------
def _system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for block in system:
        b = _block_dict(block)
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts)


def _flatten_tool_result(content) -> str:
    """A tool_result's ``content`` may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for rb in content:
            b = _block_dict(rb)
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[image omitted]")
        return "\n".join(parts)
    return str(content)


def _user_content_blocks(blocks: List[dict]) -> Union[str, list]:
    """Build OpenAI user-message content from Anthropic text/image blocks.

    Returns a plain string when there are no images (the common case), or the
    OpenAI multimodal list form when images are present.
    """
    text_parts: List[str] = []
    parts: List[dict] = []
    has_image = False
    for block in blocks:
        b = _block_dict(block)
        t = b.get("type")
        if t == "text":
            txt = b.get("text", "")
            text_parts.append(txt)
            parts.append({"type": "text", "text": txt})
        elif t == "image":
            src = b.get("source", {})
            if src.get("type") == "base64":
                has_image = True
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if has_image:
        return parts
    return "\n".join(text_parts)


def anthropic_to_openai_messages(req: MessagesRequest) -> List[dict]:
    """Translate Anthropic messages + system into an OpenAI message list.

    Per-block rules:
      - system (str | text blocks)         → leading ``role:"system"`` message
      - user text/image blocks             → ``role:"user"`` (string or vision list)
      - user tool_result blocks            → one ``role:"tool"`` message *each*
                                             (never merged), carrying tool_call_id
      - assistant text blocks              → assistant ``content``
      - assistant tool_use blocks          → assistant ``tool_calls`` (args stringified)
    """
    out: List[dict] = []

    sys_text = _system_text(req.system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})

    for msg in req.messages:
        content = msg.content

        if isinstance(content, str):
            out.append({"role": msg.role, "content": content})
            continue

        if msg.role == "user":
            # tool_result blocks each become a standalone tool message; the
            # remaining text/image blocks form one user message.
            non_tool = []
            for block in content:
                b = _block_dict(block)
                if b.get("type") == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": _flatten_tool_result(b.get("content", "")),
                    })
                else:
                    non_tool.append(b)
            if non_tool:
                out.append({"role": "user", "content": _user_content_blocks(non_tool)})

        else:  # assistant
            text_parts = []
            tool_calls = []
            for block in content:
                b = _block_dict(block)
                t = b.get("type")
                if t == "text":
                    text_parts.append(b.get("text", ""))
                elif t == "tool_use":
                    tool_calls.append({
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {})),
                        },
                    })
            assistant_msg: dict = {"role": "assistant"}
            assistant_msg["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            out.append(assistant_msg)

    return out


def anthropic_tools_to_openai(tools: Optional[List[dict]]) -> Optional[List[dict]]:
    if not tools:
        return None
    out = []
    for tool in tools:
        out.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


def map_tool_choice(tool_choice: Optional[dict]) -> Optional[Union[str, dict]]:
    if not tool_choice:
        return None
    t = tool_choice.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return "auto"


def build_openai_body(req: MessagesRequest, backend) -> dict:
    """Assemble the OpenAI chat-completions body agent.py's backend expects.

    Mirrors the key flags agent.py sets in its own request_body so behaviour
    matches: ``chat_template_kwargs.enable_thinking=False`` (suppress the
    gemma <think> stream leaking into Claude Code as content) and
    ``stream_options.include_usage`` (so llamacpp emits the final usage chunk).
    Bedrock ignores both.
    """
    messages = anthropic_to_openai_messages(req)
    # Some gateways silently drop role:"system" (see llm_backend.maybe_fold_system
    # and /droid/repos/test/aws-proxy-bugs.md §1). Without this, Claude Code's
    # entire system prompt is discarded and the model appears to ignore its
    # instructions. No-op on backends that honour system.
    try:
        from llm_backend import maybe_fold_system
        messages = maybe_fold_system(messages, backend, log)
    except Exception as e:  # never fail a request over the workaround
        log.debug("system-fold skipped: %s", e)

    body: dict = {
        "model": getattr(backend, "model", None) or req.model,
        "messages": messages,
        "max_tokens": req.max_tokens,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream_options": {"include_usage": True},
    }
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.top_p is not None:
        body["top_p"] = req.top_p
    if req.stop_sequences:
        body["stop"] = req.stop_sequences

    oai_tools = anthropic_tools_to_openai(req.tools)
    if oai_tools:
        body["tools"] = oai_tools
        choice = map_tool_choice(req.tool_choice) or "auto"
        body["tool_choice"] = choice
    return body


# ---------------------------------------------------------------------------
# Backend response  →  OpenAI delta dicts (normalise both backend shapes)
# ---------------------------------------------------------------------------
def _iter_openai_deltas(response):
    """Yield OpenAI-shape delta dicts from either backend shape.

    (a) ``requests.Response`` exposing ``iter_lines()`` — the llamacpp SSE
        shape. ``data: [DONE]`` ends the stream.
    (b) Any iterable already yielding delta dicts — the Bedrock shape.

    Mirror of ``agent._iter_stream_chunks`` (inlined to avoid importing agent).
    """
    if hasattr(response, "iter_lines"):
        for raw in response.iter_lines(decode_unicode=False):
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload.strip() == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue
    else:
        for chunk in response:
            yield chunk


def _reset_backend_conversation(backend) -> None:
    """Force a fresh conversation so the full history Claude Code sends isn't
    appended onto server-side context from a previous request (bedrock)."""
    if hasattr(backend, "_active_conv_id"):
        try:
            backend._active_conv_id = None
        except Exception:
            pass


def _estimate_input_tokens(req: MessagesRequest) -> int:
    buf = _system_text(req.system)
    for m in req.messages:
        buf += str(m.content)
    return _estimate_tokens(buf)


# ---------------------------------------------------------------------------
# OpenAI delta stream  →  Anthropic SSE events
# ---------------------------------------------------------------------------
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _safe_close(response) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _retry_delay(attempt: int) -> float:
    return min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY)


def _is_transient(e: Exception) -> bool:
    """Retryable unless it's a definite non-transient failure (budget cap /
    context overflow). Connection resets, read timeouts, 5xx, and the mid-stream
    cut from an API-Gateway timeout are all treated as transient.

    Caveat (acceptable for this temp resilience layer): this also treats a bug
    in the gateway's own delta/tool processing as "transient", so such a bug
    would be retried and surfaced as ``overloaded_error`` rather than failing
    loudly. Note that ``LlamacppBackend.stream_chat`` already retries pure
    connection errors internally, so this layer mainly covers stream deaths
    that happen after a response is returned (zero-content cuts)."""
    return not (_NON_RETRYABLE and isinstance(e, _NON_RETRYABLE))


def _args_parse_ok(args: str) -> bool:
    try:
        json.loads(args or "{}")
        return True
    except json.JSONDecodeError:
        return False


def stream_anthropic(backend, req: MessagesRequest):
    """Generator yielding Anthropic SSE events translated live from the
    backend's OpenAI delta stream.

    Emits ``message_start`` immediately (before the first backend delta) so a
    slow backend doesn't trip Claude Code's client read-timeout. Text deltas
    pass through as ``text_delta``; tool_call deltas are accumulated by index
    and emitted as a ``tool_use`` block with streamed ``input_json_delta``.

    Kept a plain ``def`` (not ``async``): FastAPI iterates sync generators in a
    threadpool, so the blocking ``iter_lines()`` never stalls the event loop.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    input_tokens = _estimate_input_tokens(req)

    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": req.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    })
    yield _sse("ping", {"type": "ping"})

    body = build_openai_body(req, backend)

    # Text is streamed live (genuine UX benefit). Tool calls are *accumulated*
    # by OpenAI tool index and emitted at stream end: a real backend (gemma via
    # llama.cpp) sends a tool call's name/id/arguments across several deltas
    # (cf. agent.py's accumulator at ~4221-4235), so emitting the tool_use
    # block eagerly on first sighting would ship an empty name. Claude Code
    # cannot act on a tool call until message_stop anyway, so deferring the
    # whole tool block to the end is correct and loses nothing.
    block_index = -1
    text_open = False
    tool_acc: dict = {}          # oai_idx → {"id","name","args"}
    tool_order: List[int] = []
    finish_reason: Optional[str] = None
    out_tokens: Optional[int] = None
    committed = False            # have we emitted any content_block to the client?
    truncated = False            # backend cut the stream mid-response
    gave_up: Optional[Exception] = None

    # Retry the backend ONLY while no content has reached the client: a flaky
    # proxy that stalls under load and dies before producing any token is the
    # safe-to-retry case. Once tokens are streaming we must not re-send — we
    # close gracefully as truncated instead.
    for attempt in range(_MAX_RETRIES + 1):
        block_index = -1
        text_open = False
        tool_acc = {}
        tool_order = []
        finish_reason = None
        out_tokens = None
        response = None
        _reset_backend_conversation(backend)
        try:
            response = backend.stream_chat(
                log, json=body, stream=True,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            for chunk in _iter_openai_deltas(response):
                usage = chunk.get("usage")
                if usage and usage.get("completion_tokens") is not None:
                    out_tokens = usage.get("completion_tokens")

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta", {}) or {}

                # --- text (streamed live) ---
                text = delta.get("content")
                if text:
                    if not text_open:
                        block_index += 1
                        text_open = True
                        committed = True
                        yield _sse("content_block_start", {
                            "type": "content_block_start", "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta", "index": block_index,
                        "delta": {"type": "text_delta", "text": text},
                    })

                # --- tool calls (accumulated; name/id/args may span deltas) ---
                for tc in delta.get("tool_calls") or []:
                    oai_idx = tc.get("index", 0)
                    fn = tc.get("function", {}) or {}
                    if oai_idx not in tool_acc:
                        tool_acc[oai_idx] = {"id": "", "name": "", "args": ""}
                        tool_order.append(oai_idx)
                    if tc.get("id"):
                        tool_acc[oai_idx]["id"] = tc["id"]
                    if fn.get("name"):
                        tool_acc[oai_idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        tool_acc[oai_idx]["args"] += fn["arguments"]
            _safe_close(response)
            break  # stream ended cleanly → finalize
        except Exception as e:
            _safe_close(response)
            if committed:
                # Tokens already reached the client — can't retry. Close as a
                # truncated turn (honest stop_reason below).
                log.warning("cc-gateway: stream cut mid-response: %s — closing as truncated", e)
                truncated = True
                break
            if not _is_transient(e) or attempt >= _MAX_RETRIES:
                gave_up = e
                break
            delay = _retry_delay(attempt)
            log.warning(
                "cc-gateway: transient backend error before any content "
                "(attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1, _MAX_RETRIES + 1, e, delay,
            )
            yield _sse("ping", {"type": "ping"})  # keep the client connection warm
            time.sleep(delay)

    # Gave up before emitting anything → surface a clean error, not a half-stream.
    if gave_up is not None and not committed:
        log.error("cc-gateway: backend failed after %d attempt(s): %s",
                  _MAX_RETRIES + 1, gave_up)
        yield _sse("error", {"type": "error", "error": {
            "type": "overloaded_error",
            "message": f"backend unavailable after {_MAX_RETRIES + 1} attempt(s): {gave_up}",
        }})
        yield _sse("message_stop", {"type": "message_stop"})
        return

    if text_open:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_index})

    # Emit accumulated tool_use blocks. On a truncated stream, skip any whose
    # arguments don't parse — a half-written tool call would break Claude Code.
    emitted_tool = False
    for oai_idx in tool_order:
        t = tool_acc[oai_idx]
        if truncated and not _args_parse_ok(t["args"]):
            continue
        emitted_tool = True
        block_index += 1
        yield _sse("content_block_start", {
            "type": "content_block_start", "index": block_index,
            "content_block": {
                "type": "tool_use",
                "id": t["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": t["name"],
                "input": {},
            },
        })
        if t["args"]:
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": block_index,
                "delta": {"type": "input_json_delta", "partial_json": t["args"]},
            })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_index})

    # stop_reason: tool_use if a complete tool was emitted; max_tokens if the
    # stream was truncated (NEVER end_turn on truncation — Claude Code would act
    # on a half-finished turn as if it were complete); else the backend's reason.
    if emitted_tool:
        stop_reason = "tool_use"
    elif truncated:
        stop_reason = "max_tokens"
    else:
        stop_reason = _STOP_MAP.get(finish_reason or "", "end_turn")
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": out_tokens if out_tokens is not None else 1},
    })
    yield _sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Non-streaming path — collect the full stream, build one Anthropic response
# ---------------------------------------------------------------------------
def build_message_response(backend, req: MessagesRequest) -> dict:
    body = build_openai_body(req, backend)
    body["stream"] = True  # backend always streams; we accumulate here

    # Non-streaming buffers the whole response before returning, so the entire
    # collection is safe to retry on a transient error (nothing reached the
    # client). The route handler turns a final failure into a 502.
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        _reset_backend_conversation(backend)
        response = None
        text_buf = ""
        tools_by_idx: dict = {}
        order: List[int] = []
        finish_reason = None
        out_tokens = None
        try:
            response = backend.stream_chat(
                log, json=body, stream=True,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            for chunk in _iter_openai_deltas(response):
                usage = chunk.get("usage")
                if usage and usage.get("completion_tokens") is not None:
                    out_tokens = usage["completion_tokens"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta", {}) or {}
                if delta.get("content"):
                    text_buf += delta["content"]
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    if idx not in tools_by_idx:
                        tools_by_idx[idx] = {"id": tc.get("id", ""), "name": "", "args": ""}
                        order.append(idx)
                    fn = tc.get("function", {}) or {}
                    if tc.get("id"):
                        tools_by_idx[idx]["id"] = tc["id"]
                    if fn.get("name"):
                        tools_by_idx[idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        tools_by_idx[idx]["args"] += fn["arguments"]
            _safe_close(response)
        except Exception as e:
            _safe_close(response)
            last_err = e
            if not _is_transient(e) or attempt >= _MAX_RETRIES:
                raise
            log.warning(
                "cc-gateway (non-stream): transient backend error "
                "(attempt %d/%d): %s — retrying", attempt + 1, _MAX_RETRIES + 1, e,
            )
            time.sleep(_retry_delay(attempt))
            continue

        content: List[dict] = []
        if text_buf:
            content.append({"type": "text", "text": text_buf})
        for idx in order:
            tc = tools_by_idx[idx]
            try:
                parsed = json.loads(tc["args"]) if tc["args"] else {}
            except json.JSONDecodeError:
                parsed = {}
            content.append({
                "type": "tool_use",
                "id": tc["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": tc["name"],
                "input": parsed,
            })
        if not content:
            content.append({"type": "text", "text": ""})

        # Match Anthropic: tool_use blocks present ⇒ stop_reason=tool_use, even
        # if the backend reported finish_reason "stop"/None.
        if order:
            stop_reason = "tool_use"
        else:
            stop_reason = _STOP_MAP.get(finish_reason or "", "end_turn")
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": req.model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": _estimate_input_tokens(req),
                "output_tokens": out_tokens if out_tokens is not None else _estimate_tokens(text_buf),
            },
        }

    raise last_err  # pragma: no cover - loop either returns or raises


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
def create_app(backend) -> FastAPI:
    app = FastAPI(title="Claude Code Gateway → agent.py backend")

    @app.post("/v1/messages")
    def messages(req: MessagesRequest):
        log.info(
            "request: model=%s msgs=%d tools=%d stream=%s → backend=%s/%s",
            req.model, len(req.messages), len(req.tools or []), req.stream,
            getattr(backend, "kind", "?"), getattr(backend, "model", "?"),
        )
        if req.stream:
            return StreamingResponse(
                stream_anthropic(backend, req),
                media_type="text/event-stream",
            )
        try:
            return build_message_response(backend, req)
        except Exception as e:
            log.error("non-stream request failed: %s", e)
            raise HTTPException(status_code=502, detail=f"backend error: {e}")

    @app.post("/v1/messages/count_tokens")
    def count_tokens(req: MessagesRequest):
        return {"input_tokens": _estimate_input_tokens(req)}

    @app.get("/health")
    def health():
        ok, msg = (True, "ok")
        probe = getattr(backend, "health", None)
        if callable(probe):
            try:
                ok, msg = probe()
            except Exception as e:
                ok, msg = False, str(e)
        return {"status": "ok" if ok else "down", "backend": getattr(backend, "kind", "?"),
                "model": getattr(backend, "model", "?"), "detail": msg}

    @app.get("/")
    def root():
        return {
            "message": "Claude Code Gateway → agent.py backend",
            "backend": getattr(backend, "kind", "?"),
            "model": getattr(backend, "model", "?"),
            "usage": "export ANTHROPIC_BASE_URL=http://<host:port>; export ANTHROPIC_API_KEY=dummy",
        }

    return app


def serve(backend, host: str = "127.0.0.1", port: int = 8788) -> None:
    """Block, serving the Anthropic gateway for ``backend`` on host:port."""
    import uvicorn

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO)

    print(
        f"\n  Claude Code Gateway listening on http://{host}:{port}\n"
        f"  Forwarding to backend: {getattr(backend, 'kind', '?')} / "
        f"{getattr(backend, 'model', '?')}\n\n"
        f"  Point Claude Code at it:\n"
        f"    export ANTHROPIC_BASE_URL=http://{host}:{port}\n"
        f"    export ANTHROPIC_API_KEY=dummy\n"
        f"    claude\n",
        flush=True,
    )
    uvicorn.run(create_app(backend), host=host, port=port, log_level="info")
