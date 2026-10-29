"""Anthropic Messages ↔ OpenAI chat-completions wire conversion.

One home for the conversions that used to live in three places
(``FoundryBackend._to_anthropic_*``, ``cc_gateway.anthropic_to_openai_messages``
and friends). Pure functions over plain dicts: no network, no SDK import, no
``agent`` import, so every backend and the gateway can share it.

Outbound (agent → Anthropic):
  ``openai_to_anthropic(messages, tools)`` → ``(system, messages, tools)``.
  The output is something a strict Converse-backed gateway accepts as-is
  (aicommons-integration.md §7 H3/H6): all system messages joined in order,
  roles strictly alternating starting with a user turn, no empty blocks,
  parallel tool results in ONE user message, every tool_use answered.

Inbound (Anthropic stream → agent):
  ``AnthropicStreamToDeltas`` turns raw stream events into the OpenAI-shaped
  delta dicts ``agent._iter_stream_chunks`` consumes, and keeps the final
  content blocks (thinking + signature included) so they can be sent back.

Gateway direction (Anthropic request → OpenAI body): ``anthropic_to_openai``,
``anthropic_tools_to_openai``, ``map_tool_choice`` — used by ``cc_gateway``.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

# Assistant-message key carrying the raw Anthropic thinking blocks for a turn:
# {"model": <model that produced them>, "blocks": [...]}. Other backends ignore it.
THINKING_KEY = "_anthropic_thinking"

_SYSTEM_ROLES = ("system", "developer")
_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)
_NO_RESULT = "[no result was recorded for this tool call]"
_BAD_ARGS_NOTE = ("[note: the arguments of the following tool call were not valid JSON "
                  "(the response was likely cut off); they were replaced with {}]")


def _as_dict(obj) -> dict:
    """SDK pydantic objects and plain dicts look the same from here on."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return dict(getattr(obj, "__dict__", {}) or {})


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


# ── OpenAI → Anthropic ──────────────────────────────────────────────────


def _image_block(url: str) -> dict | None:
    m = _DATA_URI_RE.match(url or "")
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    if url:
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_blocks(content) -> list[dict]:
    """OpenAI message content (str or part list) → Anthropic blocks, empties dropped."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks: list[dict] = []
    for part in content if isinstance(content, list) else [content]:
        if not isinstance(part, dict):
            part = {"type": "text", "text": str(part)}
        t = part.get("type")
        if t == "text":
            if (part.get("text") or "").strip():
                blocks.append({"type": "text", "text": part["text"]})
        elif t == "image_url":
            iu = part.get("image_url")
            img = _image_block(iu.get("url", "") if isinstance(iu, dict) else str(iu or ""))
            if img:
                blocks.append(img)
        elif t in ("image", "document", "tool_result", "tool_use", "thinking", "redacted_thinking"):
            blocks.append(part)  # already Anthropic-shaped
    return blocks


def _parse_args(raw) -> tuple[dict, bool]:
    """Tool-call arguments → (input dict, ok). Truncated/garbage JSON → ({}, False)."""
    if isinstance(raw, dict):
        return raw, True
    try:
        val = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}, False
    return (val, True) if isinstance(val, dict) else ({"value": val}, True)


def _tool_result_block(msg: dict) -> dict:
    content = msg.get("content")
    if isinstance(content, list):
        inner = [b for b in _content_blocks(content) if b.get("type") in ("text", "image", "document")]
        text = _text_of(content)
    else:
        text = "" if content is None else str(content)
        inner = None
    block = {"type": "tool_result", "tool_use_id": msg.get("tool_call_id", "")}
    block["content"] = inner if inner else (text or "(empty)")
    # E1: an agent tool reports failure as text starting with "Error"; telling the
    # model so (is_error) helps it recover instead of building on a bad result.
    if text.lstrip().startswith("Error"):
        block["is_error"] = True
    return block


def prune_old_thinking_enabled() -> bool:
    """AGENT_PRUNE_OLD_THINKING=0 replays (and checkpoints) every turn's stored thinking,
    the pre-2026-10-29 behaviour; the default keeps only the newest assistant turn's."""
    import os
    return os.environ.get("AGENT_PRUNE_OLD_THINKING", "1").strip().lower() not in ("0", "false", "no", "off")


def _thinking_blocks(msg: dict, model: str | None) -> list[dict]:
    """Stored thinking for this assistant turn, only if the SAME model produced it —
    another model's signatures are rejected (§7 M1)."""
    stored = msg.get(THINKING_KEY)
    if not stored:
        return []
    if isinstance(stored, list):  # untagged: trust it only when no model check is asked for
        return list(stored) if model is None else []
    if model is not None and stored.get("model") != model:
        return []
    return [b for b in stored.get("blocks") or []
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")]


def _merge_same_role(messages: list[dict]) -> list[dict]:
    """Collapse runs of same-role messages into one (Converse requires alternation).
    Within a merged user turn tool_result blocks go first, as the API expects."""
    out: list[dict] = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"].extend(m["content"])
        else:
            out.append({"role": m["role"], "content": list(m["content"])})
    for m in out:
        if m["role"] == "user":
            results = [b for b in m["content"] if b.get("type") == "tool_result"]
            if results:
                m["content"] = results + [b for b in m["content"] if b.get("type") != "tool_result"]
        else:
            # thinking must lead the assistant turn when it is present
            think = [b for b in m["content"] if b.get("type") in ("thinking", "redacted_thinking")]
            if think:
                m["content"] = think + [b for b in m["content"]
                                        if b.get("type") not in ("thinking", "redacted_thinking")]
    return out


def _answer_every_tool_use(messages: list[dict]) -> list[dict]:
    """Every tool_use must be answered by a tool_result in the very next user turn,
    and every tool_result must answer a tool_use in the turn before it."""
    out: list[dict] = []
    for i, m in enumerate(messages):
        if m["role"] == "user":
            prev_ids = set()
            if out and out[-1]["role"] == "assistant":
                prev_ids = {b["id"] for b in out[-1]["content"] if b.get("type") == "tool_use"}
            kept = [b for b in m["content"]
                    if b.get("type") != "tool_result" or b.get("tool_use_id") in prev_ids]
            have = {b.get("tool_use_id") for b in kept if b.get("type") == "tool_result"}
            missing = [{"type": "tool_result", "tool_use_id": tid, "content": _NO_RESULT, "is_error": True}
                       for tid in sorted(prev_ids - have)]
            content = missing + kept
            if content:
                out.append({"role": "user", "content": content})
            continue
        uses = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
        out.append(m)
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        if uses and (nxt is None or nxt["role"] != "user"):
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": _NO_RESULT, "is_error": True}
                for tid in uses]})
    return out


def openai_tools_to_anthropic(tools: list[dict] | None) -> list[dict] | None:
    """OpenAI function tools → Anthropic tool definitions, sorted by name.

    The order is deterministic so the tools block (which renders first) is
    byte-identical turn to turn and across runs — a prerequisite for caching.
    """
    if not tools:
        return None
    out = []
    for tool in tools:
        if tool.get("type", "function") != "function":
            continue
        fn = tool.get("function") or {}
        if not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", "") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    out.sort(key=lambda t: t["name"])
    return out or None


def openai_to_anthropic(
    messages: Iterable[dict],
    tools: list[dict] | None = None,
    *,
    model: str | None = None,
    end_with_user: bool = False,
) -> tuple[str | None, list[dict], list[dict] | None]:
    """Convert an OpenAI-shaped history (+ tools) into an Anthropic request.

    - Every ``system``/``developer`` message is lifted into ``system``, joined
      in order (the old converter kept only the last one, losing the main
      prompt — §7 H6).
    - ``role:"tool"`` results become ``tool_result`` blocks; parallel results
      share one user message; results with no matching ``tool_use`` (their
      call was compressed away) are dropped; an unanswered ``tool_use`` gets a
      placeholder error result so the request is still valid.
    - Empty messages are dropped and same-role runs merged, so roles strictly
      alternate and ``messages[0]`` is a user turn (§7 H3).
    - Tool-call arguments that are not valid JSON become ``{}`` with a note
      (§7 H4) instead of raising on every later request.
    - Stored thinking blocks (``THINKING_KEY``) are put back in front of the
      assistant turn's tool_use, verbatim, when ``model`` produced them.
    - ``end_with_user``: models that reject assistant prefill must not see a
      request ending on an assistant turn; a short user turn is appended.
    """
    system_parts: list[str] = []
    raw: list[dict] = []
    messages = list(messages)
    # Only the newest assistant turn's thinking matters: the API needs it ahead of that
    # turn's tool_use when the next message is a tool result, and strips (and does not
    # bill) thinking from every earlier turn. Replaying all of it was half of a 1.25 MB
    # checkpoint and of every request body (333 blocks, 595k chars, on one measured run).
    last_assistant = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=None)
    replay_all = not prune_old_thinking_enabled()

    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role in _SYSTEM_ROLES:
            txt = _text_of(msg.get("content")).strip()
            if txt:
                system_parts.append(txt)
            continue
        if role == "tool":
            raw.append({"role": "user", "content": [_tool_result_block(msg)]})
            continue
        if role == "assistant":
            thinking = _thinking_blocks(msg, model) if (replay_all or i == last_assistant) else []
            blocks = thinking + _content_blocks(msg.get("content"))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args, ok = _parse_args(fn.get("arguments"))
                if not ok:
                    blocks.append({"type": "text", "text": _BAD_ARGS_NOTE})
                blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""),
                               "input": args})
            if any(b.get("type") != "thinking" and b.get("type") != "redacted_thinking" for b in blocks):
                raw.append({"role": "assistant", "content": blocks})
            continue
        # user (and anything unknown, treated as user)
        blocks = _content_blocks(msg.get("content"))
        if blocks:
            raw.append({"role": "user", "content": blocks})

    out = _answer_every_tool_use(_merge_same_role(raw))
    out = _merge_same_role(out)
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continuing)"}]})
    if end_with_user and out[-1]["role"] == "assistant":
        out.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})

    system = "\n\n".join(system_parts) or None
    return system, out, openai_tools_to_anthropic(tools)


# ── Anthropic stream → OpenAI deltas ────────────────────────────────────

STOP_TO_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}


class AnthropicStreamToDeltas:
    """Incremental translator: raw Messages stream events → OpenAI delta dicts.

    ``feed(event)`` returns a (possibly empty) list of chunks to yield, as each
    event arrives — text, thinking (as ``reasoning_content``) and tool-call
    argument fragments are not buffered. ``final_chunk()`` returns the closing
    chunk: ``finish_reason``, real usage, and the assistant's raw content blocks
    under ``THINKING_KEY`` (thinking/redacted only) so the caller can store them.

    Usage comes from ``message_delta`` (the gateway's real Converse numbers);
    ``message_start`` carries a chars/4 estimate and is used only as a fallback
    for a field the delta leaves out.
    """

    def __init__(self, model: str | None = None):
        self.model = model
        self.blocks: dict[int, dict] = {}       # Anthropic content index → block being built
        self.closed: set[int] = set()
        self._tool_index: dict[int, int] = {}   # Anthropic index → OpenAI tool_calls index
        self.stop_reason: str | None = None
        self._start_usage: dict = {}
        self._delta_usage: dict = {}
        self.emitted = 0                        # chunks handed out so far (any kind)
        self.saw_stop = False

    def _chunk(self, delta: dict, finish: str | None = None) -> dict:
        self.emitted += 1
        return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def feed(self, event) -> list[dict]:
        ev = _as_dict(event)
        t = ev.get("type")
        if t == "message_start":
            self._start_usage = dict((ev.get("message") or {}).get("usage") or {})
            return []
        if t == "content_block_start":
            idx = ev.get("index", 0)
            block = dict(ev.get("content_block") or {})
            self.blocks[idx] = block
            bt = block.get("type")
            if bt == "tool_use":
                block["_json"] = ""
                oai = len(self._tool_index)
                self._tool_index[idx] = oai
                return [self._chunk({"tool_calls": [{
                    "index": oai, "id": block.get("id", ""), "type": "function",
                    "function": {"name": block.get("name", ""), "arguments": ""},
                }]})]
            if bt == "text" and block.get("text"):
                return [self._chunk({"content": block["text"]})]
            if bt == "thinking":
                block.setdefault("thinking", "")
                block.setdefault("signature", "")
            return []
        if t == "content_block_delta":
            idx = ev.get("index", 0)
            d = ev.get("delta") or {}
            dt = d.get("type")
            block = self.blocks.setdefault(idx, {})
            if dt == "text_delta":
                block["text"] = block.get("text", "") + d.get("text", "")
                return [self._chunk({"content": d.get("text", "")})] if d.get("text") else []
            if dt == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
                return [self._chunk({"reasoning_content": d.get("thinking", "")})] if d.get("thinking") else []
            if dt == "signature_delta":
                block["signature"] = block.get("signature", "") + d.get("signature", "")
                return []
            if dt == "input_json_delta":
                part = d.get("partial_json", "")
                block["_json"] = block.get("_json", "") + part
                if part and idx in self._tool_index:
                    return [self._chunk({"tool_calls": [{
                        "index": self._tool_index[idx], "function": {"arguments": part},
                    }]})]
            return []
        if t == "content_block_stop":
            self.closed.add(ev.get("index", 0))
            return []
        if t == "message_delta":
            self.stop_reason = (ev.get("delta") or {}).get("stop_reason") or self.stop_reason
            self._delta_usage = dict(ev.get("usage") or {})
            return []
        if t == "message_stop":
            self.saw_stop = True
        return []

    @property
    def truncated_tool_indices(self) -> list[int]:
        """OpenAI indices of tool calls whose block never closed (cut off mid-arguments)."""
        return [oai for idx, oai in self._tool_index.items() if idx not in self.closed]

    def usage(self) -> dict:
        def pick(key):
            v = self._delta_usage.get(key)
            return self._start_usage.get(key) if v is None else v
        uncached = int(pick("input_tokens") or 0)
        read = int(pick("cache_read_input_tokens") or 0)
        write = int(pick("cache_creation_input_tokens") or 0)
        out = int(self._delta_usage.get("output_tokens") or 0)
        return {
            "prompt_tokens": uncached + read + write,
            "completion_tokens": out,
            "total_tokens": uncached + read + write + out,
            "input_tokens": uncached,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
        }

    def thinking_blocks(self) -> list[dict]:
        out = []
        for idx in sorted(self.blocks):
            b = self.blocks[idx]
            if b.get("type") == "thinking" and b.get("signature"):
                out.append({"type": "thinking", "thinking": b.get("thinking", ""), "signature": b["signature"]})
            elif b.get("type") == "redacted_thinking" and b.get("data"):
                out.append({"type": "redacted_thinking", "data": b["data"]})
        return out

    def final_chunk(self) -> dict:
        finish = STOP_TO_FINISH.get(self.stop_reason or "", "stop" if self.saw_stop else "length")
        if self.truncated_tool_indices:
            finish = "length"
        chunk = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": self.usage()}
        thinking = self.thinking_blocks()
        if thinking:
            chunk[THINKING_KEY] = {"model": self.model, "blocks": thinking}
        if self.stop_reason:
            chunk["stop_reason"] = self.stop_reason
        return chunk


# ── Anthropic request → OpenAI body (cc_gateway direction) ──────────────


def system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n".join(_as_dict(b).get("text", "") for b in system if _as_dict(b).get("type") == "text")


def flatten_tool_result(content) -> str:
    """A tool_result's ``content`` may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for rb in content:
            b = _as_dict(rb)
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[image omitted]")
        return "\n".join(parts)
    return str(content)


def user_content_blocks(blocks) -> str | list:
    """Anthropic user text/image blocks → OpenAI user content: a plain string when
    there are no images (the common case), else the multimodal part list."""
    text_parts: list[str] = []
    parts: list[dict] = []
    has_image = False
    for block in blocks:
        b = _as_dict(block)
        t = b.get("type")
        if t == "text":
            text_parts.append(b.get("text", ""))
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image":
            src = b.get("source", {})
            if src.get("type") == "base64":
                has_image = True
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts if has_image else "\n".join(text_parts)


def anthropic_to_openai(system, messages) -> list[dict]:
    """Anthropic ``system`` + messages → an OpenAI message list.

    - system (str | text blocks) → leading ``role:"system"`` message
    - user text/image blocks     → ``role:"user"`` (string or vision list)
    - user tool_result blocks    → one ``role:"tool"`` message each, carrying tool_call_id
    - assistant text blocks      → assistant ``content``
    - assistant tool_use blocks  → assistant ``tool_calls`` (args stringified)
    ``messages`` items may be dicts or objects with ``role``/``content`` attributes.
    """
    out: list[dict] = []
    sys_text = system_text(system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})

    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else msg.role
        content = msg.get("content") if isinstance(msg, dict) else msg.content
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "user":
            non_tool = []
            for block in content:
                b = _as_dict(block)
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                                "content": flatten_tool_result(b.get("content", ""))})
                else:
                    non_tool.append(b)
            if non_tool:
                out.append({"role": "user", "content": user_content_blocks(non_tool)})
        else:
            text_parts, tool_calls = [], []
            for block in content:
                b = _as_dict(block)
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({"id": b.get("id", ""), "type": "function", "function": {
                        "name": b.get("name", ""), "arguments": json.dumps(b.get("input", {}))}})
            assistant_msg: dict = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            out.append(assistant_msg)
    return out


def anthropic_tools_to_openai(tools) -> list[dict] | None:
    if not tools:
        return None
    return [{"type": "function", "function": {
        "name": t.get("name", ""),
        "description": t.get("description", ""),
        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
    }} for t in tools]


def map_tool_choice(tool_choice) -> str | dict | None:
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
