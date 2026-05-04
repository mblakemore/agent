"""Think tool — opt-in deep reasoning via a separate API call with thinking enabled."""

import json
import logging
import os
import re
import requests

import theme
from cancel import check_cancelled
from spinner import StreamStatus

# Injectable output function — agent.py replaces this with a callback-aware
# writer (lambda text: _emit("on_stream_chunk", text)) so think output flows
# through the callback system and honours NO_COLOR / TUI mode.  Standalone
# callers (tests, scripts) get the plain print() default automatically.
_output = print  # type: ignore[assignment]

DEPTH_MAX_TOKENS = {
    "brief": 1024,
    "normal": 8192,
    "deep": 32768,
}

# Gemma 4 thinking block pattern
_THINK_RE = re.compile(r'<\|channel>thought\n(.*?)<channel\|>', re.DOTALL)


def _get_base_url():
    """Read base_url from config.json if available, otherwise default."""
    try:
        config_path = os.path.join(os.getcwd(), "config.json")
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8", errors="replace") as f:
                cfg = json.load(f)
            return cfg.get("llm", {}).get("base_url", "http://127.0.0.1:8080")
    except Exception:
        pass
    return "http://127.0.0.1:8080"


def fn(prompt: str, depth: str = "brief", context: str = "") -> str:
    """Make a standalone reasoning call with thinking enabled.

    Args:
        prompt: The problem or question to reason through.
        depth: Reasoning depth — "brief", "normal", or "deep".
        context: Optional conversation context to include.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt must be a non-empty string"
    if "\x00" in prompt:
        return "Error: prompt must not contain null bytes"
    if not isinstance(context, str):
        context = ""
    if "\x00" in context:
        return "Error: context must not contain null bytes"
    if not isinstance(depth, str) or depth not in DEPTH_MAX_TOKENS:
        valid = ", ".join(DEPTH_MAX_TOKENS)
        return f"Error: invalid depth {depth!r}. Use one of: {valid}."
    log = logging.getLogger("agent")
    max_tokens = DEPTH_MAX_TOKENS[depth]
    base_url = _get_base_url()

    # <|think|> in system prompt enables Gemma 4 thinking
    messages = [{"role": "system", "content": "<|think|>"}]
    if context:
        messages.append({"role": "user", "content": context})
        messages.append({"role": "assistant", "content": "Understood. I have the context."})
    messages.append({"role": "user", "content": prompt})

    request_body = {
        "messages": messages,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "presence_penalty": 0.0,
        "max_tokens": max_tokens,
        "stream": True,
    }

    log.info("THINK [depth=%s, max_tokens=%d, context=%d chars]: %s",
             depth, max_tokens, len(context), prompt)

    try:
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            json=request_body,
            stream=True,
            timeout=(30, 300),  # 30s connect, 5min read (per chunk)
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return f"Error: calling server: {e}"

    # Accumulate full content — parse thinking block at end
    content_parts = []

    status = StreamStatus()
    status.start("  " + theme.c(theme.SKY, "[Thinking] "))

    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        check_cancelled()
        if not line or not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices")
        if not choices:
            continue
        delta = choices[0].get("delta", {})

        if delta.get("content"):
            if not content_parts:
                status.first_token()
            content_parts.append(delta["content"])
            status.count_token()

    status.finish()

    raw = "".join(content_parts)

    # Parse out thinking block from answer
    match = _THINK_RE.search(raw)
    if match:
        reasoning = match.group(1).strip()
        answer = raw[match.end():].strip()
        _output("  " + theme.dim("[Reasoning]"))
        _output("  " + theme.dim(reasoning))
        _output(f"  [Answer] {answer}")
        log.info("THINK REASONING: %s", reasoning)
        log.info("THINK ANSWER: %s", answer)
    else:
        answer = raw.strip()
        _output(f"  [Answer] {answer}")
        log.info("THINK ANSWER: %s", answer)

    return answer if answer else "Error: empty response from model"


definition = {
    "type": "function",
    "function": {
        "name": "think",
        "description": (
            "Invoke a separate reasoning call with chain-of-thought enabled. "
            "Reasoning and answer are streamed to the console. "
            "Only the final conclusion is returned to the conversation. "
            "Depth: 'brief' (~1K tokens) for simple decisions; "
            "'normal' (~8K) for multi-step problems; "
            "'deep' (~32K) for complex analysis. "
            "Pass 'context' when the prompt references prior discussion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The question or problem to reason through.",
                },
                "depth": {
                    "type": "string",
                    "enum": ["brief", "normal", "deep"],
                    "description": "'brief' for quick tasks, 'normal' for moderate problems, 'deep' for complex analysis.",
                },
                "context": {
                    "type": "string",
                    "description": "Relevant background info the thinker needs.",
                },
            },
            "required": ["prompt", "depth"],
        },
    },
}
