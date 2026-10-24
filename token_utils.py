"""Token counting: a conservative character-based estimate, or an exact count
with a Hugging Face tokenizer you name.

No tokenizer is assumed: counting every model's text with one model family's
tokenizer is wrong for the rest. Set AGENT_TOKENIZER to a Hugging Face repo id
(the one matching the model you serve) to count exactly; it needs `transformers`.
"""

import logging
import os

# Suppress noisy warnings from transformers/huggingface
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")

_tokenizer = None
_EXACT_TOKENIZER_AVAILABLE = False
_TOKENIZER_ERROR = None

_TOKENIZER_NAME = os.environ.get("AGENT_TOKENIZER", "").strip()

if _TOKENIZER_NAME:
    try:
        from transformers import AutoTokenizer, logging as tf_logging
        tf_logging.set_verbosity_error()
        _tokenizer = AutoTokenizer.from_pretrained(_TOKENIZER_NAME)
        _EXACT_TOKENIZER_AVAILABLE = True
    except ImportError:
        _TOKENIZER_ERROR = "AGENT_TOKENIZER is set but transformers is not installed"
    except Exception as e:
        _TOKENIZER_ERROR = f"Failed to load tokenizer {_TOKENIZER_NAME!r}: {e}"

if not _EXACT_TOKENIZER_AVAILABLE:
    if _TOKENIZER_ERROR is None:
        _TOKENIZER_ERROR = "Using character/3.0 fallback for token estimation (conservative)"

# Conservative chars-per-token that errs on overestimating (safer than underestimating)
_CHARS_PER_TOKEN_FALLBACK = 3.0


def count_tokens(text: str) -> int:
    """Count tokens using the exact tokenizer if available, otherwise fallback estimate."""
    if _EXACT_TOKENIZER_AVAILABLE and text:
        return len(_tokenizer.encode(text))
    elif text:
        return max(1, int(len(text) / _CHARS_PER_TOKEN_FALLBACK))
    else:
        return 0


def count_tokens_from_message(msg: dict) -> int:
    """Count tokens for a message dict (content + tool_calls if present).
    Caches the result in the message dictionary to avoid redundant tokenizer calls.
    """
    if "_tokens" in msg:
        return msg["_tokens"]

    total = 0
    content = msg.get("content", "") or ""
    total += count_tokens(content)

    if msg.get("tool_calls"):
        import json
        total += count_tokens(json.dumps(msg["tool_calls"]))

    res = max(1, total)
    msg["_tokens"] = res
    return res

def count_tools_tokens(tools: list) -> int:
    """Count token overhead of tool schemas."""
    import json
    return count_tokens(json.dumps(tools))
