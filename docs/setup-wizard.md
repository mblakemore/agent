# Setup wizard — `/setup`

The wizard connects the agent to a model and writes `.agent/config.json` for you. Getting a model server running in the first place is [Setup](setup.md); the shapes the wizard writes are on [Configurations](configurations.md).

## When it runs

Running `agent.py` interactively in a folder with no config (no `.agent/config.json`, no legacy `config.json`) launches the wizard before the first prompt. That happens only in a real terminal: `-a`, piped and scripted runs never block on it. The same wizard is available any time as `/setup`, or at startup with `--setup`.

## Connecting

It starts with **"Hi, let's get you connected"** and asks where your model is: an **OpenAI-compatible server** (local llama.cpp, Ollama or vLLM, or a hosted API), **UCSB AI Commons**, **Azure AI Foundry**, or **More…** (the legacy Bedrock gateway).

For an OpenAI-compatible server it scans localhost (llama-server's 8080–8086, `glm serve` :8000/:8001, :5000, ollama :11434) and lists what's running with its model and context size. Another URL asks for a key only if the server wants one, and offers its models as a list. AI Commons reuses a key you've already saved and asks only which model.

```
Hi, let's get you connected.
 Where's your model?
>  1. OpenAI-compatible server   local llama.cpp / Ollama / vLLM, or a hosted API
   2. UCSB AI Commons            Claude through your AI Commons key
   3. Azure AI Foundry
   4. More…                      legacy AWS Bedrock published-bot gateway
 Found running locally:
>  1. http://127.0.0.1:8080   my-model · 196,608 ctx
   2. Another URL…
✓ Connected: my-model at http://127.0.0.1:8080, context 196,608 (/props n_ctx)
Anything else? (all optional)
[ ] Use a separate SUMMARY model   default: the same model
[ ] Add an ADVISOR model           a bigger model the agent can consult
[*] Check speed and tool calling   4 short requests
[ ] Turn on the claim guard        for coding projects
[ ] Set up this folder as an agent git repo + AGENT.md
```

With a local server that's three presses of Enter, and connecting sends only free GETs (`/health`, `/v1/models`, `/props`).

## The checklist

Everything after connecting is optional:

- **Check speed and tool calling** sends four short completions, catching the one fatal problem early: a model or chat template that can't make tool calls. Ticked for a local server, unticked for a hosted one, where they're billed.
- **Measure the context window** is offered only when the server doesn't report it. It sends test prompts of up to about 3.9M tokens in total, so it warns first and defaults to skipping; you can type a known size instead. Skipping keeps the default, and the agent recovers from overflow at run time anyway.
- **Claim guard**: the end-of-response check that blocks a "verified/committed" claim with no matching tool call. Off by default (story prose can false-positive as a claim); turn it on for coding projects.
- **Separate summary model**, **advisor**, and **set up this folder as an agent** (see [Agent scaffold](agent-scaffold.md)) each run their own short screen.

## Calibration

Calibration derives the engine's context budget from the measured or reported context — `ctx_size` (measured − 10%), `max_context_messages` (about one per 480 tokens), `summary_threshold` (a fifth of the message cap, as a message count) — and records the derivation in a `_calibrated` sidecar so later runs can report **drift**. Nothing unmeasured is ever written: shipped defaults stay. Configs calibrated before 2026-10-29 carry a `summary_threshold` in characters; the engine replaces it at startup and says so, and a `/setup calibrate` rewrites it.

## Forms

| Command | What it does |
| --- | --- |
| `/setup` | The full run. |
| `/setup main`, `/setup summary`, `/setup advisor` | Reconfigure one role. |
| `/setup test` | Probe everything, change nothing, spend nothing. |
| `/setup calibrate` | Re-derive the context settings and report drift. |

## Where the config goes

Config lands in `.agent/config.json` (chmod 600 when a key is present). Inside a git repo the engine adds `.agent/` and `config.json` to `.gitignore`, and **warns loudly if a config file is already tracked**: gitignore doesn't untrack, and the warning includes the exact `git rm --cached` fix.
