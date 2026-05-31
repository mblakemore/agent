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
    "brief": 2048,   # ~30-60s — default, sufficient for most decisions
    "normal": 4096,  # ~120-180s — use sparingly, only for complex multi-step reasoning
    "deep": 16384,   # ~5-10 min — very rarely needed; prefer brief or normal
}

# Per-session call counter. Injected into fn() results when overuse is detected.
_session_call_count = 0
_OVERUSE_THRESHOLD = 2  # warn after this many calls

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


_MAX_N_SAMPLES = 5
# Temperature spread for self-consistency runs (n_samples > 1). Picked to
# cover the conservative-to-exploratory range that self-consistency papers
# (Wang et al. 2022) find effective on multi-step reasoning.
_TEMP_SPREAD = [0.4, 0.7, 1.0, 1.2, 0.9]


def _single_call(messages, depth_max_tokens, temperature, base_url, log, label=""):
    """One streamed think call. Returns (raw_text, error_or_None)."""
    request_body = {
        "messages": messages,
        "temperature": temperature,
        "top_p": 0.95,
        "top_k": 20,
        "presence_penalty": 0.0,
        "max_tokens": depth_max_tokens,
        "stream": True,
    }
    try:
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            json=request_body,
            stream=True,
            timeout=(30, 300),
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return ("", "", f"Error: calling server: {e}")

    reasoning_parts = []
    content_parts = []
    status = StreamStatus()
    status.start("  " + theme.c(theme.SKY, f"[Thinking{label}] "))
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
        # Qwen3 / llama-server --reasoning on: thinking goes to reasoning_content,
        # final answer goes to content. Gemma 4: everything in content with
        # <|channel>thought\n...<channel\|> markers. Handle both.
        if delta.get("reasoning_content"):
            if not reasoning_parts and not content_parts:
                status.first_token()
            reasoning_parts.append(delta["reasoning_content"])
            status.count_token()
        if delta.get("content"):
            if not content_parts and not reasoning_parts:
                status.first_token()
            content_parts.append(delta["content"])
            status.count_token()
    status.finish()

    reasoning_raw = "".join(reasoning_parts)
    answer_raw = "".join(content_parts)

    if not reasoning_raw and answer_raw:
        # Gemma mode: thinking markers embedded in content — parse them out
        reasoning_raw, answer_raw = _parse_answer(answer_raw)

    return (reasoning_raw, answer_raw, None)


def _parse_answer(raw):
    """Split a Gemma-thinking response into (reasoning, answer)."""
    match = _THINK_RE.search(raw)
    if match:
        return (match.group(1).strip(), raw[match.end():].strip())
    return ("", raw.strip())


def fn(prompt: str, depth: str = "brief", context: str = "", n_samples: int = 1) -> str:
    """Make a standalone reasoning call with thinking enabled.

    Args:
        prompt: The reasoning question. SHOULD be a focused question, not a
            paraphrase of the conversation context (the dispatch layer
            hard-rejects prompts that contain 50+ char verbatim overlap with
            recent assistant messages — frame the *question*, not the
            *background*).
        depth: Reasoning budget preset — "brief" (~2K tokens),
            "normal" (~4K), "deep" (~16K). Pick based on complexity, not
            ambition.
        context: Optional summarized constraints. Same anti-laundering check
            applies — keep it short and abstract, not verbatim.
        n_samples: Self-consistency. 1 (default) = single run. 2-5 = run N
            times at varied temperatures and return a consensus-extract.
            Trades tokens for reliability on borderline reasoning tasks
            (Wang et al. 2022). Costs (N + 1) LLM calls.
    """
    global _session_call_count
    _session_call_count += 1

    if not isinstance(prompt, str):
        return f"Error: prompt must be a string, got {type(prompt).__name__!r}"
    if not prompt.strip():
        return "Error: prompt must be a non-empty string"
    if "\x00" in prompt:
        return "Error: prompt must not contain null bytes"
    if not isinstance(context, str):
        if context is None:
            context = ""
        else:
            return f"Error: context must be a string, got {type(context).__name__!r}"
    if "\x00" in context:
        return "Error: context must not contain null bytes"
    if depth is None:
        depth = "brief"
    if not isinstance(depth, str):
        return f"Error: depth must be a string, got {type(depth).__name__!r}"
    if depth not in DEPTH_MAX_TOKENS:
        valid = ", ".join(DEPTH_MAX_TOKENS)
        return f"Error: invalid depth {depth!r}. Use one of: {valid}."
    if isinstance(n_samples, bool):
        return "Error: n_samples must be a plain integer, got 'bool'"
    if not isinstance(n_samples, int):
        try:
            n_samples = int(n_samples)
        except (TypeError, ValueError):
            return f"Error: n_samples must be an integer in 1..{_MAX_N_SAMPLES}, got {n_samples!r}"
    if n_samples < 1 or n_samples > _MAX_N_SAMPLES:
        return f"Error: n_samples must be in 1..{_MAX_N_SAMPLES}, got {n_samples}"

    log = logging.getLogger("agent")
    max_tokens = DEPTH_MAX_TOKENS[depth]
    base_url = _get_base_url()

    # <|think|> triggers Gemma 4 thinking; harmless no-op on Qwen3 (server --reasoning handles it)
    base_messages = [{"role": "system", "content": "<|think|>"}]
    if context:
        base_messages.append({"role": "user", "content": context})
        base_messages.append({"role": "assistant", "content": "Understood. I have the context."})
    base_messages.append({"role": "user", "content": prompt})

    log.info("THINK [depth=%s, max_tokens=%d, context=%d chars, n_samples=%d]: %s",
             depth, max_tokens, len(context), n_samples, prompt[:200])

    # Build overuse warning prefix — appended to the result when called too often.
    _overuse_prefix = ""
    if _session_call_count > _OVERUSE_THRESHOLD:
        est_seconds = _session_call_count * 150
        _overuse_prefix = (
            f"[SYSTEM NOTE: think() has been called {_session_call_count} times this "
            f"session (estimated {est_seconds}s total thinking time). Each call blocks "
            f"execution for ~120-180s. Unless this decision is genuinely complex, skip "
            f"think() for subsequent steps and reason inline.]\n\n"
        )
        log.warning("THINK overuse: call #%d this session", _session_call_count)

    # ── Single-shot path ───────────────────────────────────────────────
    if n_samples == 1:
        reasoning, answer, err = _single_call(base_messages, max_tokens, 0.6, base_url, log)
        if err:
            return err
        if reasoning:
            _output("  " + theme.dim("[Reasoning]"))
            _output("  " + theme.dim(reasoning))
        _output(f"  [Answer] {answer}")
        log.info("THINK REASONING: %s", reasoning[:200])
        log.info("THINK ANSWER: %s", answer[:300])
        if answer:
            return _overuse_prefix + answer
        if reasoning:
            # Qwen3: token budget exhausted during reasoning phase — answer never generated.
            # Truncate the raw trace so it doesn't flood the context window, then
            # return with a clear prefix so the model knows to proceed without re-calling.
            log.warning("THINK: answer empty, returning reasoning trace as fallback (budget exhausted?)")
            _TRACE_CAP = 3000
            trace = reasoning if len(reasoning) <= _TRACE_CAP else reasoning[:_TRACE_CAP] + "\n...[truncated — budget reached]"
            return (
                "[THINK BUDGET EXHAUSTED — token limit was reached before the answer "
                "was completed. The partial reasoning trace below captures the key design "
                "decisions. Use it as context and proceed with implementation directly. "
                "Do NOT call think again for this task.]\n\n"
                + trace
            )
        return "Error: empty response from model"

    # ── Self-consistency path ──────────────────────────────────────────
    # N independent runs at varied temperatures, then a consensus-extract
    # pass that summarizes the N answers into one. The cost is (N + 1)
    # LLM calls — the consensus pass is cheap because it works on the
    # answer summaries only, not the full reasoning.
    samples = []
    for i in range(n_samples):
        check_cancelled()
        temp = _TEMP_SPREAD[i % len(_TEMP_SPREAD)]
        rsn, ans, err = _single_call(base_messages, max_tokens, temp, base_url, log,
                                    label=f" {i+1}/{n_samples}@T={temp}")
        if err:
            log.warning("Self-consistency sample %d failed: %s", i + 1, err)
            continue
        if not ans and rsn:
            ans = rsn  # token budget exhausted in reasoning phase — use trace as fallback
        if ans:
            samples.append({"temp": temp, "answer": ans})
    if not samples:
        return "Error: all self-consistency samples failed"
    if len(samples) == 1:
        log.info("Self-consistency: only 1 sample succeeded, returning single answer")
        return samples[0]["answer"]

    # Consensus-extract pass: one more LLM call summarizing the N answers.
    consensus_messages = [
        {"role": "system", "content": (
            "You are reading N independent reasoning attempts at the same "
            "question. Identify the consensus answer (the position most "
            "samples converge on) and concisely state it. If the samples "
            "disagree substantively, say so and present the alternatives "
            "with their relative support. Reply with ONLY the consensus "
            "answer — no preamble, no enumeration of inputs."
        )},
        {"role": "user", "content": (
            f"Original question:\n{prompt}\n\n"
            + "\n\n".join(
                f"--- Sample {i+1} (T={s['temp']}) ---\n{s['answer']}"
                for i, s in enumerate(samples)
            )
            + "\n\nConsensus answer:"
        )},
    ]
    _, consensus, err = _single_call(
        consensus_messages, max_tokens, 0.3, base_url, log, label=" consensus"
    )
    if err:
        # Fallback: return all samples concatenated
        joined = "\n\n".join(f"[Sample {i+1}] {s['answer']}" for i, s in enumerate(samples))
        return f"[Consensus-extract failed: {err}; returning {len(samples)} raw samples]\n\n{joined}"
    log.info("THINK CONSENSUS (n=%d): %s", len(samples), consensus[:300])
    _output("  " + theme.c(theme.MINT, f"[Consensus across {len(samples)} samples]"))
    _output(f"  {consensus}")
    return consensus if consensus else "Error: empty consensus response"


definition = {
    "type": "function",
    "function": {
        "name": "think",
        "description": (
            "Invoke a separate reasoning call with chain-of-thought enabled. "
            "Each call blocks execution for ~30-180s — use sparingly. "
            "Reasoning and answer stream to console; only the conclusion goes back "
            "into the conversation. "
            "IMPORTANT: frame the QUESTION, not the background. The dispatch layer "
            "rejects prompts that paraphrase recent context (50+ char verbatim "
            "overlap with the last 3 assistant turns). Pass summarized "
            "constraints via 'context' only when the question needs them. "
            "Use n_samples > 1 for self-consistency on borderline reasoning: "
            "N parallel runs at varied temperatures, then a consensus-extract."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The reasoning question — the WHAT, not the WHY/CONTEXT. "
                        "Avoid paraphrasing the conversation; the dispatch rejects "
                        "that pattern."
                    ),
                },
                "depth": {
                    "type": "string",
                    "enum": ["brief", "normal", "deep"],
                    "description": (
                        "Token budget preset. "
                        "brief=~2K (~30-60s, DEFAULT — use for most decisions), "
                        "normal=~4K (~120-180s — sparingly, complex multi-step only), "
                        "deep=~16K (~5-10min — very rarely; prefer normal). "
                        "Omit to use the default (brief). brief is almost always sufficient. "
                        "Avoid calling think() more than twice per session."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional summarized constraints the thinker needs. Same "
                        "anti-laundering rule applies — keep abstract, not verbatim."
                    ),
                },
                "n_samples": {
                    "type": "integer",
                    "description": (
                        "1 (default) for single-shot. 2-5 for self-consistency: "
                        "run N parallel attempts at varied temperatures, then "
                        "consensus-extract. Costs (N+1) LLM calls."
                    ),
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["prompt"],
        },
    },
}
