# Agent

A local, tool-driven coding assistant that talks to an OpenAI-compatible LLM endpoint (e.g. `llama-server` from `llama.cpp`) and runs an autonomous file/shell tool loop. It is built to survive long sessions: it checkpoints every turn, summarizes older history in the background, recovers from malformed tool calls, and catches common hallucinations before they poison context.

## Quick start

1. Start an OpenAI-compatible LLM server locally. The default endpoint is `http://127.0.0.1:8080`:

   ```bash
   llama-server -m your-model.gguf --port 8080
   ```

2. (Optional) create a `config.json` in your working directory to override defaults — see [Configuration](#configuration).

3. Run the agent:

   ```bash
   python agent.py "fix the failing test in tests/test_parser.py"
   ```

## CLI

```
python agent.py [OPTIONS] [PROMPT...]
```

| Flag | Description |
| --- | --- |
| `-a`, `--auto` | Automation mode — run the prompt and exit; no interactive loop. |
| `-c`, `--continue` | Resume from the last checkpoint and drop into an interactive session. Combine with `-a` for auto-resume-and-exit. |
| `-r N`, `--repeat N` | Run the prompt `N` times with fresh state each run. `0` or omitted means run indefinitely. Implies `-a`. |
| `--nudge` | When the model returns a text-only response (no tool calls), auto-nudge it to keep going instead of stopping. Off by default. |
| `--no-tui` | Disable the `prompt_toolkit` TUI and use a plain `input()` prompt. The TUI is on by default in any interactive mode and falls back to plain input automatically if `prompt_toolkit` isn't installed. |
| `--verbose` | Start the session with full (uncompacted) tool output. Toggle in-session with `/verbose`. |
| `PROMPT...` | Initial prompt. Optional; in interactive mode you'll be prompted if omitted. |

Press **Escape twice** within 400ms to cancel a streaming response. In the TUI this works while the model is streaming; the prompt itself uses `prompt_toolkit`'s native key handling.

### Interactive TUI (default)

When running interactively, the agent uses a `prompt_toolkit` front-end with:

- A **bottom toolbar** showing `cwd · model · message count · context ~% · verbose state`.
- **Completion** for slash commands (`/he<Tab>`) and `@path` file refs (`@src/<Tab>`).
- **Input history** navigable with ↑ / ↓.
- **Key bindings**: `Enter` submits, `Ctrl+N` inserts a literal newline.

Pass `--no-tui` to use the plain `input()` prompt instead. The `prompt_toolkit` package is an optional dependency — if it isn't installed, the agent prints a one-line notice and falls back to the plain prompt automatically.

### Interactive commands

In the interactive loop, lines starting with `/` are commands:

| Command | Description |
| --- | --- |
| `/help` | List available commands. |
| `/clear` | Clear conversation history and start a fresh session log. |
| `/context` | Show current context usage as an Aurora-gradient bar with token counts. |
| `/model` | Pick a different model from the server's `/v1/models` endpoint (summarizer keeps its original). |
| `/verbose` | Toggle compact vs. full tool-result output. Full results are always logged regardless. |
| `/tools` | Show the last 20 tool calls with a one-line result preview. |
| `exit` / `quit` | End the session. |

### Colors

The terminal UI uses the **Aurora** palette (violet → sky → mint) via `theme.py`. `NO_COLOR=1` or piping to a file disables all colors and cursor escapes automatically.

## How it works

Each cycle is a turn loop:

1. Build a context window from recent history plus an async summary of older history.
2. Stream a response from the LLM.
3. Execute any tool calls.
4. Feed results back in and repeat until the model stops calling tools, a cycle limit is hit, or the user cancels.

The loop has a few guardrails worth knowing about:

- **Checkpointing.** Conversation history and summary state are written to `.agent/state/conversation_checkpoint.json` every turn. `--continue` resumes from there.
- **Async summarization.** If a summary endpoint is configured, a background thread condenses older messages while the main model keeps working. The summary is swapped in when ready.
- **Cycle limits & wind-down.** After `cycle.max_turns` turns (default 100), the agent is asked to wrap up; after `cycle.wind_down_turns` more turns it is forced to stop.
- **Text-loop detection.** If the model emits the same text three times in a row, the cycle ends instead of looping forever.
- **Hallucination guards.** When the model claims to have read a file it never actually read, the fabricated message is stripped and a correction injected. Malformed tool-call JSON is also salvaged heuristically.
- **Tool recovery.** When a tool fails with a recognizable error (e.g. bad line numbers), `tool_recovery.py` tries to re-run it with corrected parameters via a lightweight LLM call.
- **Context overflow handling.** Three consecutive HTTP 500s from the LLM endpoint are treated as context overflow — the agent trims history and retries.

## Project layout

```
agent.py            # Main loop, streaming, context management, checkpointing
callbacks.py        # UI callback interface — NullCallbacks, TerminalCallbacks, safe_cb
commands.py         # Slash-command dispatcher (/help, /clear, /verbose, /tools, …)
tui.py              # Optional prompt_toolkit front-end (--tui)
cancel.py           # Double-escape cancel handler
spinner.py          # Aurora-pulsed waiting/streaming/done visual feedback
theme.py            # Aurora color palette + single source of ANSI escapes
token_utils.py      # Tokenizer (Gemma) with char-based fallback
tool_recovery.py    # Auto-recovery from recoverable tool errors
tools/              # Built-in tools (see below)
  file.py           # read / write / insert / append / delete / list
  exec_command.py   # Shell execution with background-session support
  search_files.py   # Grep-like search with glob and case controls
  read_pdf.py       # PDF text extraction (PyMuPDF)
  web_fetch.py      # URL → markdown, saved to disk with inline preview
  think.py          # Deep-reasoning tool via a separate thinking call
  task_tracker.py   # Persistent task list in .agent/state/tasks.json
  sleep.py          # Pause execution
.agent/             # Runtime artifacts (created on first run, gitignored)
  state/
    conversation_checkpoint.json
    tasks.json
    current-state.json
    cycle.txt
    fetched/        # Cached web_fetch output
  history/
    session-*.log   # Per-session verbose logs
```

Agent-specific tools can also live in `./tools/` alongside your working directory — they are auto-discovered and registered on startup.

## Configuration

Drop a `config.json` in the working directory to override any of the built-in defaults. The top-level sections are:

- **`llm`** — `base_url`, `model`. The OpenAI-compatible endpoint and model name.
- **`generation`** — `temperature`, `top_p`, `top_k`, `presence_penalty`.
- **`context`** — `ctx_size`, `max_tokens`, `max_full_lines`, `preview_lines`, `summary_threshold`, `summary_max_chars`, `max_context_messages`. Controls how history is sized and when it gets summarized.
- **`cycle`** — `max_turns`, `wind_down_turns`, `max_text_only`. Per-cycle turn budget and text-only nudge cap.
- **`retry`** — `max_retries`, `base_delay_seconds`, `max_delay_seconds`, `backoff_multiplier`, `jitter_factor`. Exponential backoff for transient LLM errors.
- **`summary`** — `enabled`, `base_url`, `model`, `max_wait_on_save`. Optional separate summarization endpoint (default port 8082).

Example minimal override:

```json
{
  "llm": { "base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B" },
  "context": { "ctx_size": 32768 },
  "cycle": { "max_turns": 50 }
}
```

## Dependencies

- Python 3.7+
- `requests` — HTTP to the LLM endpoint
- `transformers` + `torch` — Gemma tokenizer (optional; falls back to a character-based estimate if missing)
- `PyMuPDF` (`fitz`) — PDF extraction, used by `tools/read_pdf.py`
- `markdownify` — HTML → Markdown conversion, used by `tools/web_fetch.py`
- `prompt_toolkit` — **optional**, only required for `--tui` mode. Install with `pip install prompt_toolkit`.
- A running OpenAI-compatible LLM server (e.g. `llama-server`)
- Bash, and a TTY for the cancel handler

## State and logs

Everything runtime lives under `.agent/` in the working directory:

- `.agent/state/conversation_checkpoint.json` — last checkpoint for `--continue`
- `.agent/state/tasks.json` — persistent task list used by the `task_tracker` tool
- `.agent/state/current-state.json` — per-cycle scratch state
- `.agent/state/cycle.txt` — monotonic cycle counter
- `.agent/state/fetched/` — cached `web_fetch` downloads
- `.agent/history/session-*.log` — verbose per-session logs

Delete the `.agent/` directory to start completely fresh. The whole tree is gitignored.
