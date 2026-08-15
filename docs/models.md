# Recommended models

The agent is a tool-loop: the model's job is to emit well-formed tool calls, turn after turn,
without drifting. That rewards different models than chat benchmarks do, so these are recommendations
from running this agent, not general model rankings.

Serving instructions are on [Setup](setup.md); the config shapes for combining these are on
[Configurations](configurations.md).

## Main driver — the model that runs the loop

**Recommended: Qwen3.8.** The newest and the current recommendation.

| Model | Notes |
| --- | --- |
| [Qwen3.8-27B](https://unsloth.ai/docs/models/qwen3.8) | Dense 27B. `unsloth/Qwen3.8-27B-GGUF` for llama.cpp; an NVFP4 build is also published. |
| [Qwen3.8-2.4T-A95B](https://unsloth.ai/docs/models/qwen3.8) | The large MoE, if you have the memory for it. |

**Previously recommended, still good: Qwen3.6.** Qwen3.6 was measurably better at this agent's
tool loop than Gemma 4, and was the recommendation before Qwen3.8 shipped.

| Model | Notes |
| --- | --- |
| [Qwen3.6-27B](https://huggingface.co/unsloth/Qwen3.6-27B-MTP-GGUF) | The prior main recommendation. |
| [mblakemore/qwen3.6-35b-agent-friction-phase1](https://huggingface.co/mblakemore/qwen3.6-35b-agent-friction-phase1) | Fine-tuned to reduce tool-use friction. |

**Gemma 4 31B** — what the agent was originally built and tuned against. It works, and it is no
longer the recommendation: prefer Qwen. If you do run it, `--chat-template-file` is mandatory (see
[Setup](setup.md)) and ROCm users need `HIP_VISIBLE_DEVICES=0`.

| Model | Notes |
| --- | --- |
| [gemma-4-31B-it](https://huggingface.co/google/gemma-4-31b-it) | Base. `unsloth/gemma-4-31B-it-GGUF:UD-Q4_K_XL` also works. |
| [mblakemore/gemma-4-31B-agent-friction-phase9](https://huggingface.co/mblakemore/gemma-4-31B-agent-friction-phase9) | Fine-tuned; current of the Gemma line. |

## Summary tier — CPU, small, cheap

Context summarisation is short, frequent and easy. Run it on CPU on a second port so it never
competes with the driver for GPU.

| Model | Notes |
| --- | --- |
| [Qwen3-4B](https://huggingface.co/unsloth/Qwen3-4B-GGUF) | Pairs with a Qwen driver. |
| [gemma-4-E4B-it](https://huggingface.co/google/gemma-4-e4b-it) | Pairs with a Gemma driver. |

The summary path uses a plain-text completion call, so no tool-capable chat template is needed.

```bash
llama-server -hf unsloth/Qwen3-4B-GGUF:Q8_0 --n-gpu-layers 0 --port 8082 --parallel 1
```

## Advisor tier — a big model on CPU, consulted as a tool

**GLM-5.2 (744B), run on CPU via [colibri](https://github.com/JustVugg/colibri).** At roughly
0.9 tok/s decode it is far too slow to drive a loop — and it is very strong on the rare hard
sub-problem. So it is wired as a *tool* the fast driver calls, bounded, never as the driver itself.

colibri publishes a quantisation for this (`GLM-5.2-colibri-int4-g64-with-int8-mtp`) and serves it
with `glm serve`, by convention on port `8000`.

The agent probes the advisor endpoint at startup. If it is unreachable the `consult_advisor` tool is
unloaded, so the model never sees a tool that cannot answer. Wiring is on
[Configurations](configurations.md).

## Fine-tune history

Both fine-tune lines were trained to reduce recurring tool-use friction patterns.

| HF model | Dataset | Notes |
|---|---|---|
| `mblakemore/qwen3.6-35b-agent-friction-phase1` | — | Qwen3.6 line. |
| `mblakemore/gemma-4-31B-agent-friction-phase9` | 342 examples | Current of the Gemma line — per-action tools. |
| `mblakemore/gemma-4-31B-agent-friction-phase7` | 337 examples | Previous production — high-context, cross-repo, think-related patterns. |
| `mblakemore/gemma-4-31B-agent-friction-phase3` | 153 examples | State-file patterns. |
| `mblakemore/gemma-4-31B-agent-friction-phase2` | 121 examples | Earlier. |
| `mblakemore/gemma-4-31B-agent-friction-phase0` | 30 examples | Pipeline validation only. |

The untuned base models work; they just produce friction patterns more often.
