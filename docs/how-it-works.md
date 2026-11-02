# How it works

## The turn loop

Each cycle is a turn loop:

1. Build a context window from recent history plus an async summary of older history.
2. Stream a response from the LLM.
3. Execute any tool calls.
4. Feed results back in and repeat until the model stops calling tools, a turn limit is hit, or the user cancels.

## Guardrails

- **Checkpointing**: history and summary state are written to `.agent/state/conversation_checkpoint.json` every turn. `--continue` resumes from there. The lead files of the trimmed window (the `@file` references of the first message, typically the identity file) are stored by name and re-read from disk on resume, so an identity file edited during the session is current after `-c`; if a file is gone, the checkpoint's copy is used. During a session, `@file` in a later message keeps the lead as it is unless it names a lead file, which re-reads them all.
- **Async summarization**: a background thread condenses older messages while the main model keeps working.
- **Cycle limits**: after `cycle.max_turns` turns (default 250) the agent is asked to wrap up; after `cycle.wind_down_turns` more it is forced to stop.
- **Text-loop detection**: three identical text responses in a row end the cycle.
- **Hallucination guards**: fabricated file-read messages are stripped and a correction injected. Malformed tool-call JSON is salvaged heuristically.
- **Tool recovery**: recoverable tool errors (e.g. bad line numbers) are retried with corrected parameters via a lightweight LLM call.
- **Context overflow handling**: three consecutive HTTP 500s are treated as context overflow; the agent trims history and retries.

## Long-running commands

`exec_command` runs a command in the foreground for 10 seconds; one still going after that is detached into a background session (pass `background=true` to detach at once, `background=false` to wait out the timeout). Three things make a long run (a full test suite, a build) cheap to supervise:

- **One call waits for it.** Calling `exec_command` with only the `session_id` and `wait=<seconds>` blocks until the process exits (or the wait runs out) and returns its exit code and output, so a three-minute run costs one call rather than a sleep-and-poll loop. A double-Escape interrupts the wait, not the process.
- **Its complete output is on disk.** Every background or detached command streams everything it prints to `.agent/exec/<session>-<time>.log` in the agent's home; the result names the path. When the tool result has since been compressed or truncated in the conversation, the file is still there to grep. The newest 30 logs are kept.
- **The result stays readable.** The exit code and output of a finished command remain available from later polls of the same session until the next command runs in it.

## Escalation: when the advisor is consulted

When an [`advisor`](config-reference.md#advisor) endpoint is configured, three signals can route to it, all through one policy:

- **gate**: the success check has blocked repeatedly, meaning the model declared itself done while it was wrong. This *suggests* escalation.
- **stall**: the model repeated a failing action *and* ignored a prior redirect. Auto-invokes.
- **grind**: most of the turn budget is spent and the success check is still failing: distinct plausible actions until wall-clock, without converging. Auto-invokes.

`stall` and `grind` are the cases where a stuck model never cleanly declares done, so the gate never sees them. Escalation is deliberately biased toward *not* firing — a false escalation spends minutes of a slow model's latency for nothing — and is capped by `max_calls_per_task`.

Two modes exist: **advisor** (default) is one bounded question and one answer, with the fast model keeping the loop; **takeover**, where the advisor drives a sub-loop, is reserved for a narrow class the fast model has repeatedly failed, because every turn of it pays the slow model's latency.

On the AI Commons backend the main model first steps its reasoning effort up once before the advisor path runs; see [AI Commons](aicommons.md#models-and-effort).

## Code layout

```
agent.py              # Main loop, streaming, context management, checkpointing
llm_backend.py        # Backends: llamacpp, aicommons, foundry, bedrock
anthropic_wire.py     # Anthropic Messages <-> OpenAI chat wire conversion
bedrock_api.py        # Legacy Bedrock published-bot API
bedrock_store.py      # Credential keystore with health checks
cli_bedrock.py        # `agent.py bedrock|aicommons add/list/retest` subcommands
cc_gateway.py         # Anthropic /v1/messages gateway for Claude Code (agent.py -cc)
setup_wizard.py       # /setup
agent_scaffold.py     # /agent
wizard_ui.py          # Radio lists and checkboxes for the wizards
commands.py           # Slash-command dispatcher
tui.py                # prompt_toolkit front end
transcript.py         # Session record, reprinted on terminal resize
callbacks.py, ui.py   # UI callback interface and shared components
theme.py, spinner.py  # Colors and spinner
cancel.py             # Double-escape cancel handler
escalation_policy.py  # When to consult the advisor
phase_engine.py       # Phase machine for scaffolded agents
monitor_bus.py        # Background monitors that inject into the loop
token_utils.py        # Token counting (tokenizer or char estimate)
tool_recovery.py      # Auto-recovery from recoverable tool errors
telemetry.py          # Opt-in OTLP telemetry
alias_setup.py        # The `agent` shell alias
tools/
  file.py             # read / write / edit / insert / append / delete / list
  exec_command.py     # Shell execution with background sessions
  search_files.py     # Grep-like search with glob and case controls
  find_symbol.py      # AST-aware Python symbol lookup
  read_pdf.py         # PDF text extraction
  web_fetch.py        # URL -> markdown, saved to disk with an inline preview
  browser_check.py    # Headless Chromium page check with console capture
  think.py            # Deep-reasoning call on demand
  task_tracker.py     # Persistent task list
  goal.py             # Durable multi-cycle goals
  subagent.py         # Run a task in a child agent
  consult_advisor.py  # Ask the advisor model
  monitor.py          # Arm and stop background monitors
  end_cycle.py        # Signal a clean end of cycle (opt-in: preferences.end_cycle_tool)
  sleep.py            # Pause execution
  tool_search.py      # Find and load the deferred and hidden tools (see "Lazy tools")
.agent/               # Runtime files (created on first run, gitignored)
  config.json         # Written by /setup
  state/              # Checkpoint, tasks, cycle counter, web_fetch cache
  history/            # Per-session logs
```

Agent-specific tools in `.agent/tools/` under your working directory are auto-discovered and registered on startup.

### Lazy tools

Every tool is registered (`tools.MAP_FN`, `tools.DEFINITIONS`), but the model's request carries only the **core** tier's schemas (`tools.tools`). The **deferred** tools are named in one static line of the system prompt and the **hidden** ones not at all; the core tool `tool_search` finds them by keywords or name and asks for them to be loaded. The agent loop appends the asked-for schemas to the session's tool list once per turn (`_apply_tool_loads`), so a load costs one cache miss, not one per call, and loaded tools stay for the session and survive a `-c` resume through the checkpoint. A tool called from memory that is not in the list runs at once when its arguments fit (and is loaded), otherwise it answers with its schema; a name from another harness (`bash`, `grep`) is resolved to the tool it means. Tiers come from the tool module (`_tier`), the shipped defaults (`tools.DEFAULT_CORE`) and the `tools` block of the config; `docs/config-reference.md` has the keys.
