# agent

A local, tool-driven coding assistant that talks to an OpenAI-compatible LLM endpoint (e.g. `llama-server` from `llama.cpp`) and runs an autonomous file/shell tool loop. Built to survive long sessions: checkpoints every turn, summarizes older history in the background, recovers from malformed tool calls, and catches common hallucinations before they poison context.

![agent TUI screenshot](img/screenshot.png)

## Quick start

1. Start an OpenAI-compatible LLM server locally (default endpoint `http://127.0.0.1:8080`):
   ```bash
   llama-server -m your-model.gguf --port 8080
   ```
2. (Optional) create a `config.json` in your working directory — see [Configuration](#configuration).
3. Run:
   ```bash
   python agent.py "fix the failing test in tests/test_parser.py"
   ```

**Recommended model:** Gemma 4 31B via `llama-server`. A fine-tuned variant ([mblakemore/gemma-4-31B-agent-friction-phase9](https://huggingface.co/mblakemore/gemma-4-31B-agent-friction-phase9)) reduces common tool-use friction patterns. See [docs/local-model.md](docs/local-model.md) for download, quantize, and serve instructions.

## CLI

```
python agent.py [OPTIONS] [PROMPT...]
```

| Flag | Description |
| --- | --- |
| `-a`, `--auto` | Automation mode — run the prompt and exit; no interactive loop. |
| `-c`, `--continue` | Resume from the last checkpoint. Combine with `-a` for auto-resume-and-exit. |
| `-r N`, `--repeat N` | Run the prompt `N` times with fresh state each run (`0` = indefinitely). Implies `-a`. |
| `--nudge` | When the model returns a text-only response, auto-nudge it to keep going. Off by default. |
| `--no-tui` | Use a plain `input()` prompt instead of the `prompt_toolkit` TUI. |
| `--verbose` | Start with full (uncompacted) tool output. Toggle in-session with `/verbose`. |
| `--backend-main` | Override the main backend kind (`llamacpp` or `bedrock`). |
| `--backend-summary` | Override the summary backend kind (`llamacpp` or `bedrock`). |
| `PROMPT...` | Initial prompt. Optional in interactive mode. |

Press **Escape twice** within 400ms to cancel a streaming response.

### Interactive TUI

The default TUI (`prompt_toolkit`) provides:

- **Bottom toolbar** — `cwd · model · message count · context ~% · verbose state`
- **Completion** for slash commands (`/he<Tab>`) and `@path` file refs (`@src/<Tab>`)
- **Input history** navigable with ↑ / ↓
- **Key bindings**: `Enter` submits, `Ctrl-N` inserts a literal newline

Falls back to plain `input()` automatically if `prompt_toolkit` isn't installed.

### Slash commands

| Command | Description |
| --- | --- |
| `/help` | List available commands. |
| `/clear` | Clear conversation history and start a fresh session log. |
| `/context` | Show context usage as an Aurora-gradient bar with token counts. |
| `/model` | Pick a different model from the server's `/v1/models` endpoint. |
| `/verbose` | Toggle compact vs. full tool-result output. |
| `/tools [N\|all]` | Show buffered tool calls with a one-line result preview. |
| `exit` / `quit` | End the session. |

### Environment variables

| Variable | Description |
| --- | --- |
| `NO_COLOR=1` | Disable all terminal colors and cursor escapes (also active when stdout is not a TTY). |
| `BEDROCK_API_URL` | Bedrock gateway URL — fallback when the keystore has no `up` entries. |
| `BEDROCK_API_KEY` | Bedrock API key — fallback. |
| `AGENT_BEDROCK_STORE` | Override path to the `bedrock_creds.json` keystore. |
| `BEDROCK_DAILY_CAP_USD` | Combined daily spend cap across roles (default `$10` main, `$1` summary). |

## How it works

Each cycle is a turn loop:

1. Build a context window from recent history plus an async summary of older history.
2. Stream a response from the LLM.
3. Execute any tool calls.
4. Feed results back in and repeat until the model stops calling tools, a turn limit is hit, or the user cancels.

Guardrails:

- **Checkpointing** — history and summary state are written to `.agent/state/conversation_checkpoint.json` every turn. `--continue` resumes from there.
- **Async summarization** — a background thread condenses older messages while the main model keeps working.
- **Cycle limits** — after `cycle.max_turns` turns (default 100) the agent is asked to wrap up; after `cycle.wind_down_turns` more it is forced to stop.
- **Text-loop detection** — three identical text responses in a row ends the cycle.
- **Hallucination guards** — fabricated file-read messages are stripped and a correction injected. Malformed tool-call JSON is salvaged heuristically.
- **Tool recovery** — recoverable tool errors (e.g. bad line numbers) are retried with corrected parameters via a lightweight LLM call.
- **Context overflow handling** — three consecutive HTTP 500s are treated as context overflow; the agent trims history and retries.

## Project layout

```
agent.py            # Main loop, streaming, context management, checkpointing
callbacks.py        # UI callback interface
commands.py         # Slash-command dispatcher
tui.py              # prompt_toolkit front-end
cancel.py           # Double-escape cancel handler
spinner.py          # Aurora-pulsed visual feedback
theme.py            # Aurora color palette + ANSI escapes
token_utils.py      # Tokenizer (Gemma) with char-based fallback
tool_recovery.py    # Auto-recovery from recoverable tool errors
llm_backend.py      # LLM backend abstraction (llamacpp, bedrock)
bedrock_api.py      # AWS Bedrock Chat API integration
tools/
  file.py           # read / write / insert / append / delete / list
  exec_command.py   # Shell execution with background-session support
  search_files.py   # Grep-like search with glob and case controls
  read_pdf.py       # PDF text extraction (PyMuPDF)
  web_fetch.py      # URL → markdown, saved to disk with inline preview
  think.py          # Deep-reasoning tool via a separate thinking call
  task_tracker.py   # Persistent task list in .agent/state/tasks.json
  sleep.py          # Pause execution
.agent/             # Runtime artifacts (created on first run, gitignored)
  state/            # Checkpoint, tasks, cycle counter, web_fetch cache
  history/          # Per-session verbose logs
```

Agent-specific tools in `./tools/` alongside your working directory are auto-discovered and registered on startup.

## Configuration

Drop a `config.json` in the working directory to override defaults. Top-level sections:

- **`backends`** — `main` and `summary` entries; each has `kind` (`"llamacpp"` or `"bedrock"`) plus kind-specific keys. Preferred shape.
- **`llm`**, **`summary`** — legacy flat blocks; synthesized into `backends` at load time. Old configs need no change.
- **`generation`** — `temperature`, `top_p`, `top_k`, `presence_penalty`.
- **`context`** — `ctx_size`, `max_tokens`, `summary_threshold`, `max_context_messages`, and related sizing controls.
- **`cycle`** — `max_turns`, `wind_down_turns`, `max_text_only`.
- **`retry`** — `max_retries`, `base_delay_seconds`, `backoff_multiplier`, and related exponential-backoff tuning.

```json
{
  "backends": {
    "main":    { "kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B" },
    "summary": { "kind": "llamacpp", "base_url": "http://127.0.0.1:8082", "model": "gemma-4-E4B", "enabled": true }
  },
  "context": { "ctx_size": 32768 },
  "cycle":   { "max_turns": 50 }
}
```

## Bedrock backend

The agent supports AWS Bedrock Chat gateway for either the main or summary model. See [docs/bedrock.md](docs/bedrock.md) for credentials, keystore management, spend caps, config examples, and known limitations.

## Dependencies

- Python 3.7+
- `requests` — HTTP to the LLM endpoint
- `transformers` + `torch` — Gemma tokenizer (optional; falls back to char-based estimate)
- `PyMuPDF` (`fitz`) — PDF extraction (`tools/read_pdf.py`)
- `markdownify` — HTML → Markdown (`tools/web_fetch.py`)
- `prompt_toolkit` — interactive TUI (optional; falls back to plain `input()` automatically)
- A running OpenAI-compatible LLM server
