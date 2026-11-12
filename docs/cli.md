# CLI and usage

Command-line arguments, the interactive TUI, slash commands and environment variables. The behaviours a supervisor relies on when it runs the agent unattended (deadline, result contract, exit codes, jobs) have their own page: [Headless runs](headless.md). So does the [Claude Code gateway](claude-code-gateway.md).

```
python agent.py [OPTIONS] [PROMPT...]
```

| Flag | Description |
| --- | --- |
| `-a`, `--auto` | Automation mode — run the prompt and exit; no interactive loop. See [Headless runs](headless.md). |
| `-c`, `--continue` | Resume from the last checkpoint. Combine with `-a` for auto-resume-and-exit. The files that lead the trimmed window (`@AGENT.md` / `@CLAUDE.md` from the first message) are re-read from disk on resume and the fresh text replaces the copy in the message that first attached them, so an edit made between sessions reaches the model at once; a prompt given with `-c` is ignored when a checkpoint exists. |
| `-r N`, `--repeat N` | Run the prompt `N` times with fresh state each run (`0` = indefinitely). Implies `-a`. |
| `--setup` | Run the setup wizard at startup even when a config exists (interactive TTY sessions only), then continue into the session. |
| `--model NAME` | The main model for this run, overriding the configured one. On AI Commons without a configured model the default is `claude-v5.5-sonnet`. |
| `--effort LEVEL` | The main model's reasoning effort for this run (`low`, `medium`, `high`, `xhigh`, `max`) on backends that take one. `/effort` persists one. |
| `--nudge` | When the model returns a text-only response, auto-nudge it to keep going. Off by default. |
| `--no-tui` | Use a plain `input()` prompt instead of the `prompt_toolkit` TUI. |
| `--verbose` | Start with full (uncompacted) tool output. Toggle in-session with `/verbose`. |
| `--backend-main` | Override the main backend kind (`llamacpp`, `aicommons`, `foundry` or `bedrock`). |
| `--backend-summary` | Override the summary backend kind (`llamacpp`, `aicommons`, `foundry` or `bedrock`). |
| `-cc [HOST:PORT]` | Launch an Anthropic-compatible gateway for **Claude Code** (default `127.0.0.1:8788`) that forwards to the configured main backend, then exit. See [Claude Code gateway](claude-code-gateway.md). |
| `--result-file PATH` | Write the final assistant response to `PATH` (for callers that consume the run's output as a file). |
| `--role builder\|reviewer\|creature` | Explicit session role; overrides prompt string-matching for the CICD guards. |
| `--temperature F`, `--top-p F`, `--seed N` | Per-run sampling overrides (flag > `generation.*` config > default). `--seed` gives reproducible runs on backends that honor a seed (llama.cpp); others warn once and ignore it. |
| `--no-realpath-cwd` | Keep a symlinked working directory as given. By default the process canonicalizes its cwd and `$PWD` at start (see [Headless runs](headless.md#canonical-working-directory)). |
| `--deadline SECONDS` | Wall-clock budget for the run (overrides `cycle.deadline_s`). Warnings at 60/80/92%; at 100% tool calls are refused, the final result is forced, and the process exits `10`. |
| `--goal TEXT`, `--deliverable PATH` | The run's stated goal and named deliverables (repeatable). Injected as goal anchors at fractions of the budget; a final `done` while a deliverable is absent draws a correction. Auto-derived from a `GOAL:` / `DELIVERABLE:` stanza when the result contract is armed. |
| `--result-contract` | Require the final message to end with a fenced JSON result block (`{"contract": 1, "status": …, "summary": …}`); with `--result-file` the file receives the validated JSON only. Built-in schema unless `--result-schema` is given. See [Headless runs](headless.md#result-contract). |
| `--result-schema SCHEMA.json` | JSON schema for the result contract (implies `--result-contract`). A separate flag so the positional prompt is never mistaken for the schema path. |
| `--job FILE.yaml\|FILE.json` | Declarative job file; implies `-a` (a job is a run that ends). Sugar over `--goal` / `--deliverable` / `--deadline`, plus an `acceptance` command the **runner** re-runs after the agent exits (exit `16` if it fails) and an `env_allow` allowlist. See [The job contract](job-contract.md). |
| `PROMPT...` | Initial prompt. Optional in interactive mode. `@path` in the prompt attaches that file; `@AGENT.md` / `@CLAUDE.md` is loaded whole as the identity file. |

Press **Escape twice** within 400ms to cancel a streaming response.

### Interactive TUI

The default TUI (`prompt_toolkit`) provides:

- **Bottom toolbar** — `cwd · model · message count · context ~% · verbose state`
- **Completion** for slash commands (`/he<Tab>`) and `@path` file refs (`@src/<Tab>`)
- **Input history** navigable with ↑ / ↓
- **Key bindings**: `Enter` submits, `Ctrl-N` inserts a literal newline
- **Resize reflow**: when the window size settles after a resize, the session so far (the last 5,000 lines) is reprinted for the new width, with tool status rows shortened or restored to fit. This clears the terminal's scrollback, including anything above the agent. Live-input mode only (the default).
- **Live input**: a line typed while a turn is running is delivered to the model at the next turn boundary. A slash command typed then (`/model main NAME`, `/verbose`) is held and run when the turn's tool burst ends, never handed to the model as text. `esc·esc` cancels the running turn; `exit` quits. `interactive.live_input: false` in the config turns this mode off.

Falls back to plain `input()` automatically if `prompt_toolkit` isn't installed.

### Slash commands

| Command | Description |
| --- | --- |
| `/help` | List available commands. |
| `/clear` | Clear conversation history and start a fresh session log. |
| `/context` | Show context usage as an Aurora-gradient bar with token counts. |
| `/model [main\|summary] [name]` | Set the **main** or **summary** model and persist it to `.agent/config.json` (survives restart). Bare `/model` picks the main model interactively; `/model summary` targets the summary backend; append a model id (`/model main gpt-4o`) to set it directly without the picker. |
| `/effort [main\|summary] [level]` | Show each role's reasoning effort, or set one (`low`, `medium`, `high`, `xhigh`, `max`; main when no role is given) and persist it to `.agent/config.json`. Applies from the next request on backends that take an effort (AI Commons); a model switch keeps it. |
| `/setup [main\|summary\|advisor\|test\|calibrate]` | Connect a model and calibrate. With no argument: pick OpenAI-compatible (found local servers or a URL), UCSB AI Commons, Azure AI Foundry or legacy Bedrock, then an optional checklist (separate summary model, advisor, speed and tool-call check, context measurement, claim guard, agent scaffold). `main`, `summary` and `advisor` reconfigure one role; `calibrate` re-derives the context knobs with a drift report; `test` probes without changing or spending anything. Auto-launches on first run in an unconfigured folder (TTY sessions only). |
| `/system [text\|clear]` | Show, set or clear your own system prompt (`preferences.system_prompt`). `/system <text>` puts the text first in the system message of every request, ahead of the agent's own lines, and persists it to `.agent/config.json`; it applies from the next request. `/system clear` removes it. `Ctrl-N` adds a line break for a multi-line prompt. |
| `/tools [list\|search WORDS\|load NAMES\|core NAME\|defer NAME\|hide NAME] [--save]` | The lazy-tool tiers. `list` (the default) shows every tool's tier and the schema tokens the model currently sees; `search` finds tools by keyword; `load` adds tools to the session's list; `core`, `defer` and `hide` move a tool between tiers for this session, or for good with `--save`, which writes the [`tools`](config-reference.md#tools) block. |
| `/agent` | Scaffold an autonomous-agent identity here: type (six-phase/worker/minimal) then name, an AGENT.md/CLAUDE.md generated from the live config (real model table, advisor guidance, measured context), git provisioning when the folder isn't a repo (init / GitHub via `gh` / clone-it-yourself), and a finalizing `C0` init commit. |
| `/skill [list\|NAME\|reload]` | The [skills](config-reference.md#skills): `list` (the default) shows every skill, its tokens, whether the catalog names it and whether it is loaded; `NAME` loads one as part of the next user turn; `reload` rediscovers the skill files (the catalog line in the system prompt changes with it, one prefix miss on a caching backend). The model loads one itself with `tool_search skill:NAME`. |
| `/phase` | For a six-phase scaffolded agent: print which of PERCEIVE, PROBE, DECIDE, PLAN, IMPLEMENT, VERIFY and TRACK have a `done` task in the task tracker this cycle. |
| `/alias` | Detect the working Python and install an `agent` shell alias (`<python> /path/to/agent.py` → `agent`). Writes an idempotent block to `~/.bashrc` / `~/.zshrc` on Linux & Git-Bash; prints PowerShell/cmd equivalents on native Windows. |
| `/verbose` | Toggle compact vs. full tool-result output. |
| `exit` / `quit` | End the session. |

### Environment variables

Settings that belong to the machine or the launch rather than the project live in the environment. Everything about the project is in [`config.json`](config-reference.md).

| Variable | Description |
| --- | --- |
| `NO_COLOR=1` | Disable all terminal colors and cursor escapes (also active when stdout is not a TTY). |
| `AGENT_HEALTH_TIMEOUT` | Seconds to wait for the startup backend health probe (default `10`). Raise it for cold-start endpoints (e.g. AWS API Gateway/Lambda) that are slow on the first request. |
| `AGENT_TOKENIZER` | A Hugging Face tokenizer id matching the served model, for exact token counts (needs `transformers`). Unset, a conservative character-based estimate is used and corrected at runtime from the server's own `prompt_tokens`. |
| `AGENT_BASH_EXE` | Windows only — full path to `bash.exe` for the `exec_command` shell, overriding Git-Bash auto-detection. |
| `AGENT_FAKE_BACKEND=1` | Build the scriptable in-process fake backend instead of any configured kind (tests, control-flow reproduction; never opens a socket). See [configurations](configurations.md#hosted-backends). |
| `AGENT_MONITORS_FILE` | Where persistent monitors are saved (default `.agent/monitors.json`). See [the monitors file](config-reference.md#the-monitors-file). |
| `AGENT_ALLOW_CATASTROPHIC=1` | Switch off the always-on guard against system-destroying commands; the config key [`allow_catastrophic_commands`](config-reference.md#allow_catastrophic_commands) does the same per project. |
| `MAX_EXTRA_TOOLS` | Cap on how many agent-written tools from `.agent/tools/` load (default `0`, no cap). |
| `AGENT_DEBUG_PAYLOAD` | Dump every request and response to the model: `1`, `true` or `stderr` pretty-prints to stderr; any other value is a file path that receives JSON lines. |
| `AGENT_STALL_TIMEOUT_S` | Base seconds a request may stream zero deltas before it is aborted as a stall (default `60`; `0` disables). The effective budget is widened to cover prefill — see the next row — because a backend emits nothing until it has processed the whole prompt, and a flat guard cancels a large prompt at the instant it was about to speak. |
| `AGENT_PREFILL_TPS_FLOOR` | Prompt-processing rate, in tokens per second, the backend is known to beat (default `500`). The stall budget becomes `max(base, estimated_prompt_tokens / floor)`; a 39k-token prompt at 500 t/s gets 78 s. Lower it for a slow CPU backend. |
| `AGENT_STALL_RETRIES` | How many times a stalled turn is retried with an escalating sampling override before the cycle gives up on it (default `2`). The override clears once a turn streams normally. |
| `AGENT_CTX_SPILL_THRESHOLD` | Chars above which a tool result is spilled to a file during context-overflow recovery (default `4000`, halving per pass to `1000`). |
| `AGENT_COMPRESS_PRESSURE_FRAC` | Fraction of the context window (estimated prompt tokens / `ctx_size`) above which older repeated tool results are compressed in place (default `0.70`). Below it nothing in the history is rewritten: an in-place rewrite invalidates a prompt-caching server's key-value cache from that point on, and costs a full prefill on the next turn. |
| `AGENT_PRUNE_OLD_THINKING` | `0` keeps every assistant turn's stored thinking blocks in the checkpoint file; by default only the newest turn's are saved (the rest was half of a 1.25 MB checkpoint written every turn). This concerns the checkpoint only: within a run every stored block is sent back on every request, because Opus 4.5+, Sonnet 4.6+ and Haiku 5.5 keep prior turns' thinking in the cached prefix and a turn sent without a block it had before is a cache miss from there on. After a `-c` resume the pruned turns simply have no thinking, consistently. |
| `AGENT_MEM_WARN_MB`, `AGENT_MEM_HARD_MB` | Resident-size thresholds in MB (defaults `8192` and `12288`): a warning in the log past the first, a clean exit with the `memory` code past the second. See [Headless runs](headless.md#memory-ceiling). |
| `AGENT_DISABLE_MEM_TRIM=1` | Skip the per-turn `gc` and `malloc_trim` (for measuring their effect). |
| `AGENT_FOLD_SYSTEM`, `AGENT_CACHE_DIR` | The workaround for backends that drop `system` messages, and where its probe result is cached. See [Claude Code gateway](claude-code-gateway.md#backends-that-silently-drop-system-messages); it applies to the main loop as well. |
| `CC_GATEWAY_*` | Retry and timeout settings of the Claude Code gateway, on [its page](claude-code-gateway.md#resilience-for-slow-or-flaky-backends). |
| `AGENTPY_TELEMETRY=1` | Export OTLP metrics (needs the `opentelemetry` packages; without them a warning and no metrics). `AGENTPY_TELEMETRY_OTLP_ENDPOINT` (default `http://localhost:9090/api/v1/otlp/v1/metrics`, a Prometheus OTLP receiver), `AGENTPY_TELEMETRY_JOB` (service name, default `agentpy`), `AGENTPY_TELEMETRY_INSTANCE` (default `<host>-<folder>`, set it when several agents run from one folder) and `AGENTPY_TELEMETRY_VERBOSE=1` (adds per-turn histograms). |
| `AICOMMONS_BASE_URL`, `AICOMMONS_API_KEY`, `AICOMMONS_MODEL_CATALOG` | The AI Commons gateway's credentials and model catalog, on [its page](aicommons.md#credentials). |
| `BEDROCK_API_URL`, `BEDROCK_API_KEY` | Legacy Bedrock gateway URL and key — fallback when the keystore has no `up` entries. |
| `AGENT_BEDROCK_STORE` | Override path to the `bedrock_creds.json` keystore (shared with AI Commons). |
| `BEDROCK_DAILY_CAP_USD` | Combined daily spend cap across roles on the legacy backend (default `$10` main, `$1` summary). |

`AGENT_BACKEND_OVERRIDES` and `AGENT_SUBAGENT_ARGS` are set by the agent for its own subagents and are not meant to be set by hand.

### Measuring context-estimator drift (`scripts/measure_token_drift.py`)

The context budget is sized by `token_utils`, which counts with a fixed tokenizer. The server
may be serving a different model family, so that count is a guess about someone else's
vocabulary. `_update_token_calibration` corrects for it at runtime from the `prompt_tokens`
every streamed turn reports; this script tells you how large the error is, per content class.

```
python3 scripts/measure_token_drift.py --n-ctx 196608     # human-readable
python3 scripts/measure_token_drift.py --json             # machine-readable
```

It needs a **live server** — there is no mock answer to "what does the server actually count?"
Read `ratio = actual / estimated`: above 1.0 the server counts more than we estimated and the
budget is too large (the overflow direction); below 1.0 the guess is conservative and leaves
window unused. **Report the spread, not the mean.** A uniform ratio is a constant the budget
absorbs; a content-dependent one averages away on paper and bites on the worst input.

A measured example, to calibrate expectations before assuming a tokenizer mismatch is the
problem: against a 27B model served by a very different tokenizer, the mean ratio was 0.944
with a 0.847-1.022 spread — conservative on four of five classes and under-estimating prose by
only 2.2% (about +4.3k tokens on a 196k window). The chat template cost a fixed 45 tokens plus
5 per message, which the text estimate does not count at all — under 1% of that window even in
a long conversation. **The wrong-tokenizer hazard is real but small and mostly safe-signed;
bulk tool output pasted into context is the failure that actually overflows windows**, which is
why output spilling exists. Re-run this after any model swap rather than carrying the numbers.
