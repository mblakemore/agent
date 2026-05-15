# Local model — Gemma 4 31B

The agent was built and tuned against **Gemma 4 31B** served via `llama-server`. A fine-tuned variant is available on Hugging Face that reduces common tool-use friction patterns (over-writing existing files instead of editing, bash heredoc writes instead of `file()` calls, redundant re-reads):

**[mblakemore/gemma-4-31B-agent-friction-phase3](https://huggingface.co/mblakemore/gemma-4-31B-agent-friction-phase3)**

## Download and serve

```bash
# One-time: download, convert, quantize
/home/mike/.local/bin/hf download mblakemore/gemma-4-31B-agent-friction-phase3 \
    --local-dir /your/path/phase3-merged

python3 llama.cpp/convert_hf_to_gguf.py /your/path/phase3-merged --outtype bf16 --use-temp-file

llama.cpp/build/bin/llama-quantize \
    /your/path/phase3-merged/Phase3-Merged-31B-BF16.gguf \
    /your/path/phase3-merged/gemma-4-31B-agent-friction-phase3-Q4_K_M.gguf \
    Q4_K_M

# Serve
export HIP_VISIBLE_DEVICES=0   # ROCm only — prevents segfault on Gemma 4 SWA + flash-attn
llama-server \
  -m /your/path/phase3-merged/gemma-4-31B-agent-friction-phase3-Q4_K_M.gguf \
  --chat-template-file llama.cpp/models/templates/google-gemma-4-31B-it-interleaved.jinja \
  --port 8080 --parallel 1 --flash-attn on \
  --cache-type-k q4_0 --cache-type-v q4_0 \
  --temp 1.0 --top-p 0.95 --top-k 64
```

## Critical: `--chat-template-file`

Gemma 4's built-in GGUF chat template has **no tool-call support**. Without the interleaved Jinja template:

- `llama-server` cannot inject tool definitions into the prompt.
- The model's native `<|tool_call>...<tool_call|>` tokens appear as plain text in `delta.content`.
- The agent's safety filter strips them — **no tools ever execute**.

The correct template ships with llama.cpp at `models/templates/google-gemma-4-31B-it-interleaved.jinja`. The agent logs a `TOOL CALLS DISABLED` warning at startup if `chat_template_caps.supports_tool_calls` is false.

## Summary model (port 8082)

For context summarization the smaller **Gemma 4 E4B** works well and runs on CPU:

```bash
llama-server -hf unsloth/gemma-4-E4B-it-GGUF:Q8_0 \
  --n-gpu-layers 0 --port 8082 --parallel 1
```

The summary path uses a plain-text completion call — `--chat-template-file` is not required.

## Base model alternative

The untuned base model (`unsloth/gemma-4-31B-it-GGUF:UD-Q4_K_XL`) also works; it just produces friction patterns more often. The `--chat-template-file` requirement applies equally.

## Fine-tune history

| HF model | Dataset | Notes |
|---|---|---|
| `mblakemore/gemma-4-31B-agent-friction-phase3` | 153 examples | **Current** — adds H.01 state-file patterns |
| `mblakemore/gemma-4-31B-agent-friction-phase2` | 121 examples | Previous production |
| `mblakemore/gemma-4-31B-agent-friction-phase0` | 30 examples | Pipeline validation only |

Training details and dataset composition: [`/droid/repos/beewatcher/agent-friction-v1/CLAUDE.md`](../../beewatcher/agent-friction-v1/CLAUDE.md).
