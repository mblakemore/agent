# Configurations

`config.json` lives in the working directory you run the agent from. Every block is optional — with
a single model on the default port you need no config file at all.

Full key-by-key reference is in the [overview](index.html#configuration). This page is the shapes
that correspond to real setups, in increasing order of how much hardware they ask for.

## 1. One model, no config

A single `llama-server` on `http://127.0.0.1:8080` is the default. Nothing to configure.

```bash
llama-server -hf unsloth/Qwen3.8-27B-GGUF:Q4_K_M --port 8080 --parallel 1
python agent.py "add a test for the retry path"
```

Use this until sessions get long enough that context pressure starts costing you.

## 2. Main + summary — the long-session setup

Older history is summarised in the background so a session survives past the main model's context.
That work is short and frequent, so it goes to a small model on **CPU**, on its own port, where it
never competes with the driver for GPU.

```json
{
  "backends": {
    "main": {
      "kind":     "llamacpp",
      "base_url": "http://127.0.0.1:8080",
      "model":    "Qwen3.8-27B",
      "stream":   true
    },
    "summary": {
      "kind":     "llamacpp",
      "base_url": "http://127.0.0.1:8082",
      "model":    "Qwen3-4B",
      "enabled":  true
    }
  }
}
```

```bash
llama-server -hf unsloth/Qwen3.8-27B-GGUF:Q4_K_M --port 8080 --parallel 1     # GPU
llama-server -hf unsloth/Qwen3-4B-GGUF:Q8_0 --n-gpu-layers 0 --port 8082 --parallel 1   # CPU
```

This is the setup most long-running sessions want.

## 3. Main + summary + advisor — adding a heavyweight consultant

A very large model that is far too slow to drive a loop can still be the best thing available for
the rare hard sub-problem. So it is wired as a **tool**, not as the driver: the fast model keeps the
loop and calls `consult_advisor` when it is stuck.

`advisor` is a **top-level block**, not one of the `backends`:

```json
{
  "backends": {
    "main":    { "base_url": "http://127.0.0.1:8080", "model": "Qwen3.8-27B" },
    "summary": { "base_url": "http://127.0.0.1:8082", "model": "Qwen3-4B", "enabled": true }
  },
  "advisor": {
    "base_url":           "http://127.0.0.1:8000",
    "model":              "GLM-5.2",
    "max_calls_per_task": 3,
    "timeout_s":          900
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | true when `base_url` is set | The tier is off unless you configure an endpoint. |
| `base_url` | — | OpenAI-compatible endpoint for the advisor model. |
| `model` | — | Model name passed to that endpoint. |
| `api_key` | — | Bearer token, if the endpoint needs one. |
| `max_calls_per_task` | `3` | Hard cap on consultations per task, so a stuck loop cannot spend the whole session waiting. |
| `timeout_s` | `900` | Per-consultation timeout. On expiry the agent is told to proceed without the advice rather than stalling. |

Serve GLM-5.2 on CPU with [colibri](https://github.com/JustVugg/colibri) (`glm serve`, port 8000 by
convention) — see [Recommended models](models.html).

**The endpoint is probed at startup.** If it is unreachable, `consult_advisor` is removed from the
tool registry entirely, so the model never sees a tool it cannot use and never wastes turns
escalating to a server that is not running. A missing advisor degrades the agent to setup 2; it does
not break it.

## Hosted backends

`kind` also accepts `"bedrock"` and `"foundry"` instead of `"llamacpp"`, for a hosted endpoint
rather than a local server. AWS Bedrock has its own guide: [Bedrock backend](bedrock.html).
