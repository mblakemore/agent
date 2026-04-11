"""Token utilities with Gemma 3 tokenizer counting.

Uses the Gemma 3 tokenizer (shared by Gemma 4 variants) for precise context
window management. Falls back to conservative character-based estimation if
the tokenizer is unavailable.
"""

import logging
import os

# Suppress noisy warnings from transformers/huggingface
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")

_tokenizer = None
_QWEN_TOKENIZER_AVAILABLE = False  # kept for backward-compat imports

try:
    from transformers import AutoTokenizer, logging as tf_logging
    tf_logging.set_verbosity_error()
    _tokenizer = AutoTokenizer.from_pretrained("unsloth/gemma-3-4b-it")
    _QWEN_TOKENIZER_AVAILABLE = True
except ImportError:
    logging.warning("transformers not installed — run: pip install transformers")
except Exception as e:
    logging.warning(f"Failed to load Gemma tokenizer: {e}")

if not _QWEN_TOKENIZER_AVAILABLE:
    logging.warning("Using character/3.0 fallback for token estimation (conservative)")

# Conservative chars-per-token that errs on overestimating (safer than underestimating)
_CHARS_PER_TOKEN_FALLBACK = 3.0


def count_tokens(text: str) -> int:
    """Count tokens in text using Qwen tokenizer if available, otherwise fallback estimate."""
    if _QWEN_TOKENIZER_AVAILABLE and text:
        return len(_tokenizer.encode(text))
    elif text:
        return max(1, int(len(text) / _CHARS_PER_TOKEN_FALLBACK))
    else:
        return 0


def count_tokens_from_message(msg: dict) -> int:
    """Count tokens for a message dict (content + tool_calls if present)."""
    total = 0
    content = msg.get("content", "") or ""
    total += count_tokens(content)

    if msg.get("tool_calls"):
        import json
        total += count_tokens(json.dumps(msg["tool_calls"]))

    return max(1, total)


def count_tools_tokens(tools: list) -> int:
    """Count token overhead of tool schemas."""
    import json
    return count_tokens(json.dumps(tools))
