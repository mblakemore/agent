# How it works

## The turn loop

Each cycle is a turn loop:

1. Build a context window from recent history plus an async summary of older history.
2. Stream a response from the LLM.
3. Execute any tool calls.
4. Feed results back in and repeat until the model stops calling tools, a turn limit is hit, or the user cancels.

## Guardrails

- **Checkpointing**: history and summary state are written to `.agent/state/conversation_checkpoint.json` every turn. `--continue` resumes from there. The lead files of the trimmed window (the `@file` references of the first message, typically the identity file) are stored by name and re-read from disk on resume, so an identity file edited during the session is current after `-c`; if a file is gone, the checkpoint's copy is used. During a session, `@file` in a later message keeps the lead as it is unless it names a lead file, which re-reads them all. The lead files have one place in the history, the message that first attached them: a re-read, on `-c` or by a later `@CLAUDE.md`, replaces the text in that place, and the later message keeps a one-line note (plus any other files it named). Before this, every re-reference added a second whole copy, and an untrimmed window sent both, one of them stale, for the rest of the session. The refresh costs one prompt-cache miss over the history when the file has actually changed.
- **Async summarization**: a background thread condenses older messages while the main model keeps working.
- **Cycle limits**: after `cycle.max_turns` turns (default 250) the agent is asked to wrap up; after `cycle.wind_down_turns` more it is forced to stop.
- **Text-loop detection**: three identical text responses in a row end the cycle.
- **The success check**: `cycle.success_check` names a command (a test suite, a build) that must exit 0 before the model may end a cycle. The model sees it in the system prompt; when it stops, the harness runs it, and a failure turns the stop back with a correction, three times at most. It is also the signal the escalation policy below reads. Off unless set; see the [config reference](config-reference.md#verification-gates).
- **The claim guard**: with `cycle.claim_trace_max_blocks` above 0, a final message that says the tests were run or a commit was made is checked against the session's tool trace, and an unsupported claim is turned back. `/setup` offers it for coding projects.
- **Hallucination guards**: fabricated file-read messages are stripped and a correction injected. Malformed tool-call JSON (a truncated string, an unbalanced brace or quote, a trailing comma, single quotes) is repaired with `json-repair` first, then salvaged heuristically for the token-leakage shapes a generic repairer does not know; a repair is logged at WARNING with the head of the raw arguments.
- **Tool recovery**: recoverable tool errors (e.g. bad line numbers) are retried with corrected parameters via a lightweight LLM call.
- **Context overflow handling**: three consecutive HTTP 500s are treated as context overflow; the agent trims history and retries.

## Long-running commands

`exec_command` runs a command in the foreground for 10 seconds; one still going after that is detached into a background session (pass `background=true` to detach at once, `background=false` to wait out the timeout). Three things make a long run (a full test suite, a build) cheap to supervise:

- **One call waits for it.** Calling `exec_command` with only the `session_id` and `wait=<seconds>` blocks until the process exits (or the wait runs out) and returns its exit code and output, so a three-minute run costs one call rather than a sleep-and-poll loop. A double-Escape interrupts the wait, not the process.
- **Its complete output is on disk.** Every background or detached command streams everything it prints to `.agent/exec/<session>-<time>.log` in the agent's home; the result names the path. When the tool result has since been compressed or truncated in the conversation, the file is still there to grep. The newest 30 logs are kept.
- **The result stays readable.** The exit code and output of a finished command remain available from later polls of the same session until the next command runs in it.
- **A busy session is never overwritten.** A command that goes to the background while its session still runs an earlier one gets a session of its own (the result says so), so both jobs keep their own output, exit code and log; a quick foreground command in a busy session runs as before.
- **Its exit arrives by itself.** A detached or background command is a *source* on the event bus: when it exits, one line with its exit code and output tail is queued for the model, like a monitor's output, so the common case needs no poll at all (`preferences.session_events`).

## The event bus: sources, `wait` and the gate

Every request carries the whole context, so the bill for an always-on agent is set by how many times the model is woken. Two habits drive that number up: a monitor that wakes the model for every line of a noisy stream, and a long job checked by repeated polls that mostly find nothing. The bus (`monitor_bus.py`) addresses both without making any call blocking that was not before.

- **Sources.** Everything the agent is tracking is a source on one bus: a monitor (a command on an interval, or a file watched with `monitor path=…`), a detached or background `exec_command` session, a `subagent` started with `background=true`. A finished session or subagent queues one item by itself, and a session the model polled to completion takes its item back, so nothing is told twice. A background subagent is killed when this process exits, so a cycle should `wait` for it before ending.
- **`wait`.** Fan-out stays non-blocking; `wait` is the fan-in. It blocks inside the tool call, so no request is open, until anything tracked produces an item, or the sources in `any_of` do, or every source in `all_of` has finished (a barrier). It returns everything that is ready, not just the first thing, and ends with what is still running so the model is reminded of its other threads before it blocks again. Typed input interrupts it (the line is kept for the loop; a pending `/command` makes it return at once and say so, since that line runs only when the turn ends); esc·esc cancels it. A `-a` run can wait an hour for one request's worth of tokens.
- **The gate** (`gate.py`, on when `preferences.gate.enabled` is true). A source may carry a *brief*, prose the model writes saying what is worth a wake (`monitor brief=…`, or the `gate` tool for any source). Output from a gated source is judged by the summary model first: "wake" comes through now; "hold" is kept, with a one-line summary, and never dropped. Held items arrive as one digest when the source next wakes, when the agent wakes for any other reason (the digest rides along), when the oldest passes `digest_after_s`, or when the held buffer fills; the raw text is in `.agent/gate/<source>.log`. `bypass` and `quiet` are regexes checked before any model call. A judge that fails, times out or answers nonsense wakes: the gate fails open. Each verdict is a `gate.verdict` log line and a `gate` telemetry event. `preferences.gate.enabled` is the master switch, read at startup: off, nothing is judged or held and a saved brief is inert; on, nothing is judged either until a source carries a brief, so the switch itself costs no model calls. Finished-job items and `wait` do not depend on it.

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
skills.py             # Skills: SKILL.md discovery, the catalog line, the picker (see "Skills")
tui.py                # prompt_toolkit front end
transcript.py         # Session record, reprinted on terminal resize
callbacks.py, ui.py   # UI callback interface and shared components
theme.py, spinner.py  # Colors and spinner
cancel.py             # Double-escape cancel handler
escalation_policy.py  # When to consult the advisor
phase_engine.py       # Phase machine for scaffolded agents
monitor_bus.py        # The event bus: monitors, sessions and subagents as sources; wait
gate.py               # The cheap judge in front of a gated source, and its digests
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
  monitor.py          # Arm and stop background monitors (command or watched file, brief)
  wait.py             # Block until a tracked job, subagent or monitor has something
  gate.py             # Set what a source may wake the model for (preferences.gate)
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

A tool function receives its arguments and nothing else. The one supported way for a tool to act on the running harness is `tools.RUNTIME`, a small registry agent.py fills at startup: `get_model(role)`, `set_model(role, model)` (what `/model` does, persisted), `models(role)` (the configured switchable models), `get_effort(role)` and `set_effort(role, effort)` (what `/effort` does). The shipped `switch_model` tool is built on it and exists only when `preferences.switchable_models` names models. Under another harness the registry is empty and such a tool answers with an error.

### Skills

A skill is an instruction file for one kind of task, discovered from `.agent/skills/NAME/SKILL.md`, `.claude/skills/NAME/SKILL.md` (Claude Code's layout) and the `skills` block of the config (`skills.py`). Where a tool is a schema at the front of the cached prefix, so that loading one mid-session is a miss over the whole context, a skill is text in a message: `tool_search skill:NAME` answers with its body as the tool result, append-only, cached from the next request on, and it leaves the window by trimming like anything else. The system prompt carries one static catalog line naming the skills beside the tool catalog; `/skill NAME` loads one from the keyboard as part of the next user turn; the loaded set is re-derived from the window each turn (`skills.sync_loaded`), so a skill that was trimmed away can be loaded again, and the summarizer is told to mention it. An optional picker (`skills.pick`) puts the summary model in front of the catalog: before the first request it reads the user's own words and loads what applies above a confidence threshold, or suggests it in the runtime note below that; at `"always"` it also judges later messages on a worker thread, fail-open like the gate. `docs/config-reference.md#skills` has the keys and the log lines.
