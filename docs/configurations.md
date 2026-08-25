# Configurations

`config.json` lives in the working directory you run the agent from. Every block is optional — with
a single model on the default port you need no config file at all.

Full key-by-key reference is in the [overview](../README.md#configuration). This page is the shapes
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
    "max_calls_per_task": 3
  }
}
```

Two knobs matter most here: `max_calls_per_task` (default `3`) caps consultations so a stuck loop
cannot spend the whole session waiting, and `timeout_s`, which bounds each consultation — on expiry
the agent is told to proceed without the advice rather than stalling. Leave `timeout_s` unset and it
is **derived** from the configured budgets (`prefill_token_budget`, default `1200`; `max_tokens`,
default `512`) and the endpoint's measured speed, so a full answer actually fits inside it — a fixed
timeout smaller than the time the budgets themselves imply just guarantees empty answers. Run
**`/setup calibrate`** (or `/setup advisor`) to measure the advisor's real prefill/decode rates into
`advisor.measured` and write the derived timeout explicitly; uncalibrated setups fall back to
deliberately conservative rates. The full key reference lives with the other config blocks in the
[overview](../README.md#advisor).

The advisor **distills before it asks**: your context is compressed on the fast summary model down to
`prefill_token_budget` before the slow model sees it, because prefill dominates at these speeds. And
it **fails open** — any error, timeout or disabled config returns a notice and the loop continues.

Serve GLM-5.2 on CPU with [colibri](https://github.com/JustVugg/colibri) (`glm serve`, port 8000 by
convention) — see [Recommended models](models.md).

**The advisor is ON by default.** With no `advisor` block it self-consults the **main** endpoint
(a fresh context with the decisive-advisor system prompt — a different frame, not a different
model); the same fallback applies to `summary`. Point `advisor.base_url` at a heavyweight server
to get a genuinely different model, or set `advisor.enabled: false` to turn the tier off.

**The endpoint is probed at startup.** If it is unreachable, `consult_advisor` is removed from the
tool registry entirely, so the model never sees a tool it cannot use and never wastes turns
escalating to a server that is not running. An unreachable advisor degrades the agent to setup 2;
it does not break it.

## Hosted backends

`kind` also accepts `"bedrock"` and `"foundry"` instead of `"llamacpp"`, for a hosted endpoint
rather than a local server. AWS Bedrock has its own guide: [Bedrock backend](bedrock.md).

## Generation settings and per-run overrides

The `generation` block sets sampling for the main loop:

```json
{
  "generation": {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "seed": null,
    "enable_thinking": false
  }
}
```

`seed` is `null` by default (the server's own default). Set it for reproducible runs on backends
that honor a sampling seed — llama.cpp does; hosted backends without one log a single warning
and ignore it. The flags `--temperature`, `--top-p` and `--seed` override these per run
(flag > config > default), which is what k-sample ensembles ("same prompt, k runs, reconcile")
and failure reproduction want without editing the file. See [CLI: headless runs](cli.md#headless--unattended-runs)
for the exit-code contract that unattended callers rely on.

## Cycle limits for unattended runs

```json
{
  "cycle": {
    "max_turns": 250,
    "wind_down_turns": 10,
    "deadline_s": 0,
    "deadline_warn_fracs": [0.6, 0.8, 0.92],
    "grind_elapsed_s": 0,
    "result_contract": false,
    "result_contract_max_blocks": 2
  }
}
```

`deadline_s` is a wall-clock budget in seconds (`0` = off; `--deadline` overrides it). The
agent is warned at each fraction in `deadline_warn_fracs` and forced to its final result at
100% (exit code `10`). When a deadline is set and `grind_elapsed_s` is `0`, grind escalation
defaults to half the deadline so a stuck run asks for help while there is still time to use it.

`result_contract` is `false` (raw `--result-file` text), `true` (built-in schema) or a path to
a JSON schema file; `--result-contract [schema]` overrides it. `result_contract_max_blocks`
bounds how many times a missing/invalid result block redirects the exit before a synthesized
`failed` record is written (exit code `11`). See [CLI: headless runs](cli.md#headless--unattended-runs).
