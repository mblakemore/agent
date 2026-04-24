"""Dev-mode prompt stuffing: serializer + regex tool-call parser.

Ported from /droid/repos/llmbox-cli/llmbox_lib.py @ SHA 1653b71
  (`_TOOL_CALL_RE`, `_THINK_TAG_RE`, `_UNICODE_MAP` at lines 25-32;
   `_build_tool_system_prompt` at lines 718-757;
   `_parse_tool_calls`, `_strip_tool_calls`, `_sanitize` at lines 881-904).
Last verified: 2026-04-23

See /droid/repos/agent/plan/bedrock-integration.md § 8 for the design
rationale and § 19 for the drift protocol.

This module provides the wire-format translation for the Bedrock backend:

- ``build_dev_prompt(messages, tools)`` serializes OpenAI-shape messages +
  tool schemas into a single flat prompt string with ``[System]``,
  ``AVAILABLE TOOLS:``, ``User:``, ``Assistant:``, ``[Tool call: ...]``,
  ``[Tool result (name): ...]`` segments, terminated by ``\n\nAssistant:``.
- ``parse_dev_response(text)`` walks ``<tool_call>…</tool_call>`` blocks out
  of the model's response text and returns ``(narrative_text,
  list_of_tool_call_dicts)`` where each tool_call dict is in OpenAI shape
  (``{"index", "id", "type", "function": {"name", "arguments"}}``).
- ``is_truncated(text)`` — the short-circuit sanity check from
  ``llmbox_lib.py:641`` that gates the truncation-recovery continuation
  loop.

The module does not import ``agent`` or ``llm_backend`` — it is pure
string manipulation so it can be unit-tested without the network or the
backend factory.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

# ── Primitives ported verbatim from llmbox_lib.py ─────────────────────

# llmbox_lib.py:25
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

# llmbox_lib.py:26
_THINK_TAG_RE = re.compile(r"</?think>")

# llmbox_lib.py:28-32
_UNICODE_MAP = str.maketrans(
    {
        "—": "--",
        "–": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        "•": "*",
        " ": " ",
        "​": "",
    }
)


# ── Dev-mode preamble ─────────────────────────────────────────────────
#
# K13 mitigation (plan § 17): the dev-mode system text is held here as a
# module-level constant so audit / tests can assert against it without
# cross-referencing runtime state. The one-shot ``<tool_call>`` example
# comes from llmbox_lib.py:746.
DEV_MODE_PREAMBLE = (
    "You are an autonomous agent with access to tools for file operations, "
    "command execution, web fetching, and more.\n\n"
    "AVAILABLE TOOLS:\n{tools_block}\n\n"
    "TO USE A TOOL, include a tool call block in your response:\n\n"
    "<tool_call>\n"
    '{{"tool": "tool_name", "args": {{"param1": "value1", "param2": "value2"}}}}\n'
    "</tool_call>\n\n"
    "RULES:\n"
    "- You may use multiple tool calls in a single response.\n"
    "- After tool execution, you will receive results and can make more calls or give a final answer.\n"
    "- When done, respond with plain text (no tool_call block).\n"
    "- Always explain what you're doing before tool calls.\n"
    "- Be careful with destructive commands — ask before deleting files or modifying system config.\n"
    "- Do not use interactive commands (vim, less, top).\n"
    "- Read files before overwriting them.\n"
)


def _build_tool_system_prompt(tools: list[dict] | None) -> str:
    """Build the ``AVAILABLE TOOLS:`` block from OpenAI tool schemas.

    Port of ``llmbox_lib.py:718-757``, adapted to take OpenAI-shape
    ``tools`` as input (list of ``{"type":"function","function":{...}}``).
    """
    tool_descriptions: list[str] = []
    for tool_def in tools or []:
        fn_def = tool_def.get("function") or {}
        name = fn_def.get("name") or ""
        if not name:
            continue
        desc = fn_def.get("description", "")
        params = fn_def.get("parameters") or {}
        props = params.get("properties") or {}
        required = params.get("required") or []

        param_lines: list[str] = []
        for pname, pinfo in props.items():
            req = " (required)" if pname in required else ""
            ptype = pinfo.get("type", "string")
            pdesc = pinfo.get("description", "")
            if pinfo.get("enum"):
                pdesc = (pdesc + f" One of: {pinfo['enum']}").strip()
            param_lines.append(f"    - {pname} ({ptype}{req}): {pdesc}")

        params_str = "\n".join(param_lines) if param_lines else "    (no parameters)"
        tool_descriptions.append(f"  {name}: {desc}\n  Parameters:\n{params_str}")

    tools_block = "\n\n".join(tool_descriptions) if tool_descriptions else "  (none)"
    return DEV_MODE_PREAMBLE.format(tools_block=tools_block)


def _parse_tool_calls(text: str) -> list[dict]:
    """Scan ``text`` for ``<tool_call>…</tool_call>`` blocks (ported verbatim
    from ``llmbox_lib.py:881-895``). Returns a list of
    ``{"name", "args"}`` dicts. Malformed JSON is silently skipped.
    """
    calls: list[dict] = []
    for match in _TOOL_CALL_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
            name = data.get("tool") or data.get("name")
            args = data.get("args") or data.get("arguments") or {}
            if not args and name:
                args = {k: v for k, v in data.items() if k not in ("tool", "name")}
            if name:
                calls.append({"name": name, "args": args})
        except json.JSONDecodeError:
            logging.getLogger("dev_mode_prompt").debug(
                "dev-mode: dropped malformed <tool_call> block: %r",
                match.group(1)[:120],
            )
            continue
    return calls


def _strip_tool_calls(text: str) -> str:
    """Remove every ``<tool_call>…</tool_call>`` block (ported from
    ``llmbox_lib.py:897-899``)."""
    return _TOOL_CALL_RE.sub("", text).strip()


def _sanitize(text: str) -> str:
    """Strip ``<think>`` tags and normalize fancy Unicode (ported from
    ``llmbox_lib.py:901-904``)."""
    text = _THINK_TAG_RE.sub("", text)
    return text.translate(_UNICODE_MAP)


# ── Higher-level serializer / parser ──────────────────────────────────


def _unpack_tool_call_args(raw: Any) -> dict:
    """OpenAI tool_calls use ``function.arguments`` as a JSON-encoded
    string. Unpack into a dict; if already a dict, return it.
    Tolerant to malformed JSON (returns an empty dict).
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def build_dev_prompt(messages: list[dict], tools: list[dict] | None) -> str:
    """Serialize OpenAI-shape messages + tools into a flat dev-mode prompt.

    The output structure mirrors ``llmbox_lib.py:762-813`` (``_build_prompt``)
    but without the budget-capped reverse walk — the agent layer handles
    context management upstream, so we emit the whole message list.

    System messages are merged into the ``[System]`` block (tool manual
    follows). User / assistant / tool messages become ``User:`` /
    ``Assistant:`` / ``[Tool call: ...]`` / ``[Tool result (name): ...]``
    segments. The prompt terminates with ``\n\nAssistant:`` to cue the
    model.
    """
    # Partition system messages from the rest (system content is merged into
    # the [System] block — D6-b proposed answer: merge not replace).
    system_fragments: list[str] = []
    body_msgs: list[dict] = []
    for msg in messages or []:
        role = msg.get("role")
        if role == "system":
            content = msg.get("content") or ""
            if content:
                system_fragments.append(content)
        else:
            body_msgs.append(msg)

    tool_preamble = _build_tool_system_prompt(tools)
    if system_fragments:
        system_block_body = "\n\n".join(system_fragments) + "\n\n" + tool_preamble
    else:
        system_block_body = tool_preamble

    parts: list[str] = [f"[System]\n{system_block_body}\n[End System]\n"]

    for msg in body_msgs:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args = _unpack_tool_call_args(fn.get("arguments"))
                parts.append(f"[Tool call: {name}({json.dumps(args)})]")
        elif role == "tool":
            name = msg.get("name", "?")
            parts.append(f"[Tool result ({name}): {content}]")
        # Any other role is silently dropped — dev-mode prompts only know
        # system/user/assistant/tool.

    return "\n\n".join(parts) + "\n\nAssistant:"


def build_dev_prompt_incremental(new_messages: list[dict]) -> str:
    """Serialize *new* non-system messages for a server-side multi-turn POST.

    When ``BedrockBackend`` reuses an existing ``conversationId``, the
    gateway already has the conversation's prior turns (including the
    ``[System]`` block with the tool manual + one-shot example) stored
    server-side. On follow-up calls we only need to send the messages
    *appended since the last assistant response* — typically one or two
    ``tool`` results plus maybe a ``user`` nudge.

    This mirrors ``build_dev_prompt``'s per-message rendering but skips
    the system/tool preamble and any pre-existing history. The output
    ends with ``\\n\\nAssistant:`` to cue the model to respond.

    Callers should slice the full message list at the last assistant
    index and pass only the tail: ``messages[last_assistant_idx + 1:]``.
    """
    parts: list[str] = []
    for msg in new_messages or []:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "tool":
            name = msg.get("name", "?")
            parts.append(f"[Tool result ({name}): {content}]")
        elif role == "assistant":
            # Defensive — callers should have sliced past the last
            # assistant. If one slipped in, render it faithfully.
            parts.append(f"Assistant: {content}")
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args = _unpack_tool_call_args(fn.get("arguments"))
                parts.append(f"[Tool call: {name}({json.dumps(args)})]")
        # system messages are deliberately dropped — the server already
        # has them attached to the conversation on turn 1.
    if not parts:
        # Extremely rare: a nudge turn with no incremental content. Send
        # a minimal continuation cue so the server has something to work
        # with.
        return "Assistant:"
    return "\n\n".join(parts) + "\n\nAssistant:"


def parse_dev_response(text: str) -> tuple[str, list[dict]]:
    """Parse a dev-mode model response into (narrative, tool_calls).

    The narrative is ``_strip_tool_calls`` + ``_sanitize`` of the input.
    Each tool_call dict is in OpenAI streaming shape::

        {
            "index": i,
            "id": f"call_{uuid-hex}",
            "type": "function",
            "function": {
                "name": <tool name>,
                "arguments": json.dumps(<args>),
            },
        }

    Matches the streaming-delta structure the agent's main loop expects
    at ``agent.py`` around the SSE-parse site (see plan § 8.2).
    Malformed JSON inside a ``<tool_call>`` block is silently dropped.
    """
    raw_calls = _parse_tool_calls(text)
    narrative = _sanitize(_strip_tool_calls(text))
    # Match the llmbox UX: if the model still emits a leading "Assistant:"
    # marker (the preamble's Assistant cue), strip it for the narrative.
    if narrative.startswith("Assistant:"):
        narrative = narrative[len("Assistant:"):].strip()

    tool_calls: list[dict] = []
    for i, call in enumerate(raw_calls):
        name = call.get("name", "")
        args = call.get("args") or {}
        tool_calls.append(
            {
                "index": i,
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args),
                },
            }
        )
    return narrative, tool_calls


def is_truncated(text: str) -> bool:
    """Return True iff the response contains a ``<tool_call>`` opener without
    a matching ``</tool_call>`` closer.

    Port of the ``llmbox_lib.py:641`` sanity check — used by
    ``BedrockBackend.stream_chat`` to decide whether to trigger the
    truncation-recovery continuation loop (plan § 8.3).
    """
    return "<tool_call>" in text and "</tool_call>" not in text
