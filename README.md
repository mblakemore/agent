# agent

A local, tool-driven coding assistant that talks to an OpenAI-compatible LLM endpoint (e.g. `llama-server` from `llama.cpp`) and runs an autonomous file/shell tool loop. Built to survive long sessions: checkpoints every turn, summarizes older history in the background, recovers from malformed tool calls, and catches common hallucinations before they poison context.

![agent TUI screenshot](img/screenshot.png)

## Install

```bash
pip install --user -r requirements.txt
```

On Ubuntu 24.04+ (PEP 668 / externally-managed system python), add `--break-system-packages`, or install into a venv:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Windows (Git-Bash) setup

The runtime is cross-platform Python. The `exec_command` tool shells out to `bash` so shell idioms (heredocs, pipes, `&&`, `/tmp/...`) run unchanged — on Windows that bash comes from Git-Bash, **not** `cmd` or PowerShell.

1. **Install [Git for Windows](https://git-scm.com/download/win).** It bundles a standalone `bash.exe` (MSYS2) and `git` — no WSL required.
2. **Install Python 3.10+** for Windows, then `pip install -r requirements.txt`. (Install the GitHub CLI `gh` too if you run the CICD pipeline.)
3. **Make `bash` resolvable.** The runtime locates Git-Bash itself and **deliberately ignores `C:\Windows\System32\bash.exe`** — that's the WSL launcher stub, which fails every command with *"Windows Subsystem for Linux has no installed distributions"* if you don't run WSL. (Bare `bash` would re-resolve straight back to that stub, so it's not used as a fallback.) Resolution order:
   - `AGENT_BASH_EXE` env var pointing at the full path (e.g. `C:\Program Files\Git\bin\bash.exe`, or `%LOCALAPPDATA%\Programs\Git\bin\bash.exe` for a per-user Git install) — **set this if auto-detection fails**, **then**
   - known Git-Bash install locations (`C:\Program Files\Git\bin\bash.exe`, the `(x86)` variant, and the per-user `%LOCALAPPDATA%` path), **then**
   - **derived from `git` on `PATH`** — Git for Windows keeps `bash.exe` in a sibling `bin\` of `git.exe`, so if `git` works, Git-Bash is found even when only Git's `cmd\` (not `bin\`) is on `PATH`, **then**
   - `where bash` on `PATH`, excluding the System32 WSL stub and any `WindowsApps` Store alias.
4. **Run from a Git-Bash shell**, not `cmd`/PowerShell:
   ```bash
   python agent.py "fix the failing test in tests/test_parser.py"
   ```
   For the CICD pipeline: `bash CICD/cicd.sh <repo-url>` from Git-Bash. (A native PowerShell launcher is a separate, not-yet-done port.)

**Platform notes:**

- Double-Escape cancellation is a POSIX-tty feature and is a no-op on the Windows console — use `Ctrl+C`, or a TUI host's cancel keybinding.
- The bedrock credential-store lock uses `msvcrt` on Windows (`fcntl` on POSIX); both auto-release on process exit.
- State lives under `%USERPROFILE%\.config\agent\`.
- Native Windows validation is pending a `windows-2022` runner; the suite is currently validated on Linux plus platform-simulation tests (`tests/test_windows_compat.py`).

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

**Recommended models:**

| Role | Model |
| --- | --- |
| Main (GPU) | [Qwen3.6-27B](https://huggingface.co/unsloth/Qwen3.6-27B-MTP-GGUF) or [gemma-4-31B-it](https://huggingface.co/google/gemma-4-31b-it) via `llama-server` |
| Summary (CPU) | [Qwen3-4B](https://huggingface.co/unsloth/Qwen3-4B-GGUF) or [gemma-4-E4B-it](https://huggingface.co/google/gemma-4-e4b-it) |

Run a second `llama-server` instance on CPU for the summary backend (see [Configuration](#configuration)) — it handles short summarisation calls so the main GPU model stays free for reasoning.

**Fine-tuned variants** (optional) — trained to reduce common tool-use friction patterns:

- Qwen3.6-based: [mblakemore/qwen3.6-35b-agent-friction-phase1](https://huggingface.co/mblakemore/qwen3.6-35b-agent-friction-phase1)
- Gemma 4-based: [mblakemore/gemma-4-31B-agent-friction-phase9](https://huggingface.co/mblakemore/gemma-4-31B-agent-friction-phase9)

See [docs/local-model.md](docs/local-model.md) for download, quantize, and serve instructions.

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

Drop a `config.json` in the working directory (i.e. wherever you run `agent.py` from) to override defaults. All sections are optional; omitted keys use the defaults listed below.

### `backends`

Preferred shape. Replaces the legacy `llm` / `summary` flat blocks (which still work — they are synthesized into `backends` at load time).

```json
{
  "backends": {
    "main": {
      "kind":     "llamacpp",
      "base_url": "http://127.0.0.1:8080",
      "model":    "my-model-name",
      "api_key":  "",
      "stream":   true
    },
    "summary": {
      "kind":     "llamacpp",
      "base_url": "http://127.0.0.1:8082",
      "model":    "my-summary-model",
      "enabled":  true
    }
  }
}
```

| Key | Default | Description |
| --- | --- | --- |
| `kind` | `"llamacpp"` | Backend type: `"llamacpp"`, `"bedrock"`, or `"foundry"`. |
| `base_url` | `"http://127.0.0.1:8080"` | OpenAI-compatible endpoint. |
| `model` | `"gemma-4-31B"` | Model name passed to the endpoint (informational for llamacpp; selects the Bedrock model ID for bedrock). |
| `api_key` | `""` | Bearer token sent as `Authorization: Bearer <key>`. Keep this file `chmod 600`. |
| `stream` | `true` | Set `false` to use non-streaming completions (useful for debugging). |
| `enabled` | `true` (main) / `true` (summary) | Set `false` to disable the summary backend entirely. |

For **Bedrock**-specific keys (`api_url`, spend caps, keystore) see [docs/bedrock.md](docs/bedrock.md).

### `generation`

Inference parameters forwarded to the LLM on every request.

| Key | Default | Description |
| --- | --- | --- |
| `temperature` | `0.6` | Sampling temperature. |
| `top_p` | `0.95` | Nucleus sampling threshold. |
| `top_k` | `20` | Top-K sampling. |
| `min_p` | `0.0` | Min-P sampling (0 = disabled). |
| `presence_penalty` | `0.0` | Penalise tokens already present in context. |

### `context`

Controls context-window sizing and compaction.

| Key | Default | Description |
| --- | --- | --- |
| `ctx_size` | `114688` | Context window size in tokens. Auto-detected from the server's `/props` endpoint when available; this value is the fallback cap. |
| `max_tokens` | `16384` | Maximum tokens in a single completion. |
| `max_full_lines` | `800` | Lines of tool output kept verbatim before compaction. |
| `preview_lines` | `200` | Lines shown in the compacted preview. |
| `summary_threshold` | `5` | Messages beyond which background summarisation fires. |
| `summary_max_chars` | `3000` | Maximum characters in a generated summary chunk. |
| `max_context_messages` | `30` | Hard cap on messages sent to the LLM per turn. |

### `cycle`

Per-session run limits.

| Key | Default | Description |
| --- | --- | --- |
| `max_turns` | `250` | Stop (or wind down) after this many turns. |
| `wind_down_turns` | `10` | Turns of grace period after `max_turns` before a hard stop. |
| `max_text_only` | `3` | Consecutive text-only responses that trigger a halt (loop detection). |
| `max_total_nudges` | `6` | Total auto-nudges allowed before giving up (requires `preferences.nudge` or `--nudge`). |

### `retry`

Exponential-backoff settings for failed LLM requests.

| Key | Default | Description |
| --- | --- | --- |
| `max_retries` | `10` | Maximum retry attempts before the request fails. |
| `base_delay_seconds` | `2` | Initial retry wait. |
| `max_delay_seconds` | `60` | Cap on retry wait. |
| `backoff_multiplier` | `2.0` | Multiplier applied to delay each retry. |
| `jitter_factor` | `0.1` | Random jitter added to each delay (fraction of current delay). |

### `bedrock`

Bedrock-specific tuning (only relevant when `backends.main.kind` or `backends.summary.kind` is `"bedrock"`).

| Key | Default | Description |
| --- | --- | --- |
| `adaptive_max_tokens` | `true` | Dynamically adjust `max_tokens` per request based on detected prompt complexity, staying within the model's limit. |

### `preferences`

Behavioural knobs that don't fit elsewhere.

| Key | Default | Description |
| --- | --- | --- |
| `nudge` | `false` | Auto-nudge the model when it returns a text-only response. Also settable with `--nudge` CLI flag. |
| `persist_nudge` | `false` | After a text-only stop, check `git status`; if uncommitted changes exist and no commit happened this session, inject one nudge to commit. Intended for git-native agents. |
| `tool_selection_hints` | `false` | Prepend a system-prompt directive recommending `file(action='edit')` over heredoc rewrites for existing-file edits. |
| `max_text_response_chars` | `24000` | Cap on text-only response length per turn (characters). Only enforced when `nudge` is on. Prevents context-filling monologue spirals. |
| `max_post_tool_text_chars` | `2000` | Cap on prose generated after tool calls in the same turn. |
| `extra_allowed_paths` | `[]` | List of absolute directory paths the `file` tool is allowed to read/write outside the working directory. |
| `tools_whitelist` | `null` | Restrict the tool schema sent to the LLM to this list of tool names. `null` = all tools. Example: `["file", "exec_command", "search_files", "think", "task_tracker"]`. |
| `initial_tasks` | `[]` | Task descriptions pre-seeded into `task_tracker` at session start (only when the task list is empty — does not overwrite an in-progress cycle). |
| `seed_tasks_persistent` | `false` | Re-seed `initial_tasks` at the start of every cycle, not just on a fresh task list. |

### `command_guards`

A list of regex-pattern / message pairs that intercept `exec_command` calls before the shell sees them. When a command matches a pattern, execution is blocked and the message is returned to the agent as a tool error — the agent can then reason about it and try something else.

Patterns are case-insensitive regexes matched against the full command string.

```json
{
  "command_guards": [
    {
      "pattern": "\\b8080\\b",
      "message": "BLOCKED: Port 8080 is used by the llama.cpp main inference server. Do not start processes on it or kill processes using it. Use a different port (e.g. 8765) for http.server."
    },
    {
      "pattern": "\\b8082\\b",
      "message": "BLOCKED: Port 8082 is used by the llama.cpp summary inference server. Do not use it."
    },
    {
      "pattern": "rm\\s+-rf\\s+/",
      "message": "BLOCKED: Refusing to run recursive delete from filesystem root."
    }
  ]
}
```

Guards fire after the built-in hallucination guards (`/home/user`, `python`→`python3`) but before the command runs.

### Full example

```json
{
  "backends": {
    "main":    { "kind": "llamacpp", "base_url": "http://127.0.0.1:8080" },
    "summary": { "kind": "llamacpp", "base_url": "http://127.0.0.1:8082", "enabled": true }
  },
  "generation": { "temperature": 0.8 },
  "context":    { "ctx_size": 32768 },
  "cycle":      { "max_turns": 50 },
  "preferences": {
    "nudge": true,
    "tools_whitelist": ["file", "exec_command", "search_files", "think", "task_tracker"]
  },
  "command_guards": [
    {
      "pattern": "\\b8080\\b",
      "message": "BLOCKED: Port 8080 is reserved for the llama.cpp server."
    }
  ]
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
