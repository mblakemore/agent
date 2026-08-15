# Setup — getting a model server running

`agent.py` does not run a model. It talks to an OpenAI-compatible endpoint, so setup is: **start one
or more `llama-server` instances, then point the agent at them.** Install instructions for the agent
itself are in the [overview](index.html); this page covers the server side.

Which model to serve is a separate question — see [Recommended models](models.html). The shapes of
the config file are on [Configurations](configurations.html).

## The shortest path

Serve a GGUF straight from Hugging Face; `llama-server` downloads and caches it.

```bash
llama-server -hf unsloth/Qwen3.8-27B-GGUF:Q4_K_M --port 8080 --parallel 1
```

Then, in another shell:

```bash
python agent.py "fix the failing test in tests/test_parser.py"
```

The default endpoint is `http://127.0.0.1:8080`, so with a single main model on that port no
`config.json` is needed at all.

## Serving a local or fine-tuned model

A model you have downloaded or trained needs converting to GGUF and quantizing first.

```bash
# One-time: download, convert, quantize
hf download <hf-repo> --local-dir /path/to/merged

python3 llama.cpp/convert_hf_to_gguf.py /path/to/merged --outtype bf16 --use-temp-file

llama.cpp/build/bin/llama-quantize \
    /path/to/merged/<model>-BF16.gguf \
    /path/to/merged/<model>-Q4_K_M.gguf \
    Q4_K_M
```

Then serve it:

```bash
llama-server \
  -m /path/to/merged/<model>-Q4_K_M.gguf \
  --port 8080 --parallel 1 --flash-attn on \
  --cache-reuse 256 --reasoning off \
  --cache-type-k q4_0 --cache-type-v q4_0
```

## The one that silently breaks tools: `--chat-template-file`

**If tools never execute, this is almost always why.** Some GGUF builds ship a chat template with
**no tool-call support**. Without a template that supports tools:

- `llama-server` cannot inject tool definitions into the prompt.
- The model's native tool-call tokens arrive as plain text in `delta.content`.
- The agent's safety filter strips them — **no tool ever runs**, and the loop looks like a model that
  simply refuses to act.

The agent logs a `TOOL CALLS DISABLED` warning when it detects this. The fix is to pass a template
that supports tools, e.g. for Gemma 4:

```bash
llama-server -m <model>.gguf \
  --chat-template-file llama.cpp/models/templates/google-gemma-4-31B-it-interleaved.jinja \
  --port 8080
```

Templates ship with llama.cpp under `models/templates/`.

## Hardware notes

- **ROCm:** `export HIP_VISIBLE_DEVICES=0` before serving Gemma 4 — it prevents a segfault on
  Gemma 4's sliding-window attention combined with flash-attn.
- **Summary and advisor tiers run on CPU.** Only the main driver needs the GPU. See
  [Configurations](configurations.html).

## Windows

The runtime is cross-platform Python, but `exec_command` shells out to **bash** — on Windows that
means Git-Bash, not `cmd` or PowerShell. Install [Git for Windows](https://git-scm.com/download/win),
run the agent from a Git-Bash shell, and if auto-detection fails set `AGENT_BASH_EXE` to the full
path of `bash.exe`. Full resolution order and platform caveats are in the
[overview](index.html).

## Checking it works

The agent probes each configured backend at startup and reports what it found. A healthy main
backend prints its endpoint and model in the banner; an unreachable optional tier is reported and
disabled rather than failing the run — an unreachable **advisor** endpoint, for instance, causes the
`consult_advisor` tool to be unloaded so the model never sees a tool it cannot use.
