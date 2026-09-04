# CLI and usage

Command-line arguments, the interactive TUI, slash commands and environment variables.

```
python agent.py [OPTIONS] [PROMPT...]
```

| Flag | Description |
| --- | --- |
| `-a`, `--auto` | Automation mode — run the prompt and exit; no interactive loop. |
| `-c`, `--continue` | Resume from the last checkpoint. Combine with `-a` for auto-resume-and-exit. |
| `-r N`, `--repeat N` | Run the prompt `N` times with fresh state each run (`0` = indefinitely). Implies `-a`. |
| `--setup` | Run the setup wizard at startup even when a config exists (interactive TTY sessions only), then continue into the session. |
| `--nudge` | When the model returns a text-only response, auto-nudge it to keep going. Off by default. |
| `--no-tui` | Use a plain `input()` prompt instead of the `prompt_toolkit` TUI. |
| `--verbose` | Start with full (uncompacted) tool output. Toggle in-session with `/verbose`. |
| `--backend-main` | Override the main backend kind (`llamacpp` or `bedrock`). |
| `--backend-summary` | Override the summary backend kind (`llamacpp` or `bedrock`). |
| `-cc [HOST:PORT]` | Launch an Anthropic-compatible gateway for **Claude Code** (default `127.0.0.1:8788`) that forwards to the configured main backend, then exit. See [Claude Code gateway](#claude-code-gateway--cc). |
| `--result-file PATH` | Write the final assistant response to `PATH` (for callers that consume the run's output as a file). |
| `--role builder\|reviewer\|creature` | Explicit session role; overrides prompt string-matching for the CICD guards. |
| `--temperature F`, `--top-p F`, `--seed N` | Per-run sampling overrides (flag > `generation.*` config > default). `--seed` gives reproducible runs on backends that honor a seed (llama.cpp); others warn once and ignore it. |
| `--no-realpath-cwd` | Keep a symlinked working directory as given. By default the process canonicalizes its cwd and `$PWD` at start (see [Headless runs](#headless--unattended-runs)). |
| `--deadline SECONDS` | Wall-clock budget for the run (overrides `cycle.deadline_s`). Warnings at 60/80/92%; at 100% tool calls are refused, the final result is forced, and the process exits `10`. |
| `--goal TEXT`, `--deliverable PATH` | The run's stated goal and named deliverables (repeatable). Injected as goal anchors at fractions of the budget; a final `done` while a deliverable is absent draws a correction. Auto-derived from a `GOAL:` / `DELIVERABLE:` stanza when the result contract is armed. |
| `--result-contract` | Require the final message to end with a fenced JSON result block (`{"contract": 1, "status": …, "summary": …}`); with `--result-file` the file receives the validated JSON only. Built-in schema unless `--result-schema` is given. See [Headless runs](#headless--unattended-runs). |
| `--result-schema SCHEMA.json` | JSON schema for the result contract (implies `--result-contract`). A separate flag so the positional prompt is never mistaken for the schema path. |
| `--job FILE.yaml\|FILE.json` | Declarative job file; implies `-a` (a job is a run that ends). Sugar over `--goal` / `--deliverable` / `--deadline`, plus an `acceptance` command the **runner** re-runs after the agent exits (exit `16` if it fails) and an `env_allow` allowlist. See [The job contract](job-contract.md). |
| `PROMPT...` | Initial prompt. Optional in interactive mode. |

Press **Escape twice** within 400ms to cancel a streaming response.

### Headless / unattended runs

`agent.py -a` is often spawned by a supervisor — a batch queue, CI job, cron entry or another
agent — that imposes a wall-clock budget, consumes a result, and kills the process on overrun.
These behaviours exist for that audience; interactive sessions are untouched.

**Canonical working directory.** At start the process resolves symlinks in its working
directory and rewrites `$PWD` to the physical path, logging the change. A run launched through
a symlinked path otherwise produces `os.path.relpath()` values that walk out of the repository,
and `git log -- <path>` then returns nothing *with exit 0*. Pass `--no-realpath-cwd` to keep the
symlinked view (overlay-style layouts).

**Wall-clock deadline (`--deadline SECONDS` / `cycle.deadline_s`).** The wind-down ladder
is turn-based; a run that is slow rather than turn-hungry got no warning and was killed by
its supervisor mid-write, with work done and no result emitted. With a deadline set, the
clock starts at run start and the agent is told at 60% (heads-up), 80% (begin wrapping up)
and 92% (stop working — emit your final result now), through the same message channel the
turn wind-down uses; the fractions are `cycle.deadline_warn_fracs`. At 100% the stop is
structural: tool calls are refused (each still receives a paired result saying why), the
success-check and claim gates stand down, and the run takes the final-result path — the
process exits `10` and, when `--result-file` is set, the file holds whatever the model said
last. A grind escalation defaults to half the deadline when `cycle.grind_elapsed_s` is unset,
and an advisor consult that cannot finish in the time left is skipped with a notice rather
than started. Unset (`0`) is today's behaviour exactly.

**Result contract (`--result-contract`, `--result-schema FILE` / `cycle.result_contract`).** Callers that
consume structured results used to prompt for a fenced JSON block and parse the raw
`--result-file` themselves — discipline, not structure, with two observed failures: a
*decoy* block (the model discusses an example and the extractor grabs it) and a `done` claim
with no block at all. When armed, the contract instruction is injected as a system message,
and the final message must end with a fenced ```json block that parses and validates against
the schema (built-in default: `contract: 1`, `status: done|failed|blocked|cannot-tell`,
`summary`, optional `artifacts`, `verify_output`, `scope`). The key `"contract": 1` is the
discriminator — a block without it is prose. Missing or invalid at exit → a correction is
injected and the run continues, bounded by `cycle.result_contract_max_blocks` (default 2);
still missing at the bound, or at the deadline's hard stop, the run exits and the result file
receives a synthesized `{"status": "failed", "summary": "<no valid result block emitted: …>"}`
record, so the caller **always** gets valid JSON; the process exits `11` unless a harder stop
already owns the code. Two valid blocks → last wins. The exit classification is folded into
the JSON as `"exit": {code, name, detail}`. An unreadable schema refuses to start (exit `14`).
Without the flag, `--result-file` is byte-identical to the raw behaviour.

**Bulk belongs in files (`limits.tool_result_max_chars`, default 20 000).** A tool result
over the cap is written to `.agent/spill/<turn>-<tool>-<ts>.txt` and the model receives a
reference — path, size, line count, a head/tail excerpt, and the instruction to read it in
slices — instead of the payload. This replaces the old head/tail truncation at the same
boundary and is lossless: the file holds everything. The same cap applies to an oversized
initial prompt (a spec with a data blob pasted in), which is spilled with a reference the model
is told to read first. A spill directory that cannot be written falls back to the legacy
truncation with a warning. `0` disables the spill (truncation only). The context budget is
also calibrated from the server's own token counts: every streamed turn reports
`prompt_tokens`, the ratio observed-to-estimated is tracked, and the next window is sized by
it — a tokenizer mismatch between the estimator and the served model stops mattering after
the first measured turn (logged when the ratio moves by more than 10%). On the transport, an
HTTP 500 is treated as a context overflow only when its body says so (llama.cpp names it:
"exceeds the available context size", "context shift is disabled", "prompt is too long");
any other 500 is a transient, retried with the existing jittered exponential backoff and
never counted toward an overflow verdict.

**Goal anchoring and the deliverable guard (`--goal`, `--deliverable`, `cycle.goal`,
`cycle.deliverables`).** Long-horizon drift, observed live: a 45-minute run produced a competent
artifact answering the wrong question, self-assessed complete, and never noticed the named
deliverable was absent. With a goal and/or deliverables set, the run receives a goal anchor at
`cycle.goal_anchor_fracs` (default 50% and 80% of the deadline, or of the turn budget when no
deadline) — the stated goal, the deliverables, and a mechanical "these do not exist yet" list
from one glob sweep, no model call. A final `done` while a named deliverable is absent draws
the standard correction (bounded like the result contract); `blocked`, `failed` and
`cannot-tell` are always accepted — the guard polices claims, not outcomes. When the result
contract is armed and the initial prompt carries a `GOAL:` / `DELIVERABLE:` stanza, both are
derived automatically. The launch log names which deliverables are absent at start, so a
misspelled path is visible before a long run rather than after it.

**Repeat-read stall (`cycle.repeat_read_nudge`, default 3 in 6).** The stall detector counts
read-only turns; "re-reading the same file" is a sharper and earlier signature, and it is what
pre-death loops look like. The same read-class call (tool + arguments) issued `n` times within
`window` turns triggers one nudge naming the read — once per unique call, never a nag — and,
when a success check is configured, the existing advisor escalation. A substantive batch
(edit, write, exec) resets the window.

**Classifiable exit status (`-a` / `-r` only).** The process exits with a stable code and prints
one line to stderr, `AGENT-EXIT: <name> <detail>`, so a supervisor can tell *completed* from
*died-of-context* without parsing logs:

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | `completed` | The run finished. |
| `1` | `error` | The run loop ended in an unclassified error. |
| `10` | `deadline` | Wall-clock deadline stop (`--deadline`). |
| `11` | `contract` | Result contract not satisfied; a synthesized `failed` result was written (`--result-contract`). |
| `12` | `context` | Context exhaustion after every reduction (spill, trim, summary halving). |
| `13` | `memory` | RSS crossed `mem.hard_mb`; the process exited before the OOM killer. |
| `14` | `config` | Configuration error at start (unbuildable backend, unreadable schema). |
| `15` | `backend` | Backend unreachable or failed after retries. |
| `16` | `acceptance` | `--job`: the runner re-ran the job's `acceptance` command after the agent exited and it failed, timed out, or could not be executed. |

Interactive sessions keep exiting `0` (or `2` on a start-up configuration error, as before).

### Declarative jobs and the acceptance gate (`--job`)

A job file collects the flags a headless run needs into one reviewable artifact, and adds the
one thing a flag cannot express: a check that runs **outside** the agent.

```yaml
goal: One sentence. What exists after this run that does not now.
context:    [paths to read first; the run starts with no memory]
constraints: [what must not change; what is out of scope]
deliverable: [path/to/thing_the_run_must_produce]
acceptance: pytest -q tests/test_thing.py     # ONE bare command
timebox_sec: 3600
env_allow: [API_TOKEN]
acceptance_timeout_sec: 300                  # optional; overrun is a failure
refusal_max_turns: 5                         # optional; 0 disables the capability-refusal stop
result_contract: true                        # optional; true, false, or a schema path
```

`goal`, `deliverable` and `timebox_sec` are folded into `--goal`, `--deliverable` and
`--deadline`; an explicit flag on the command line overrides the file, so a shared job can be
reused with one field changed. JSON is accepted everywhere YAML is (content decides, not the
extension) — PyYAML is optional and only needed for YAML files.

**The gate.** After the agent exits, the runner re-runs `acceptance` and *its* status is the
verdict: a failure exits `16` no matter what the run reported. The command is also shown to the
run, so it can check itself before finishing — but satisfying it in prose changes nothing.

It fails closed. A missing binary, a timeout, and a non-zero status are all failures, because
"the check did not run" and "the check passed" must never render the same to a supervisor. A
command that will not parse is refused at launch rather than discovered as a dead gate an hour
later, and so is an unknown key — a misspelled `acceptance` is a gate that never arms. If the
run already ended with a typed hard stop (deadline, contract, context, memory, backend), that
code is kept and the gate's failure is appended to the exit detail; `16` is reserved for a run
that finished and whose artifact is wrong. The gate's full output goes to
`.agent/acceptance-out.txt`; the exit line carries its last line.

**Capability refusals end the run, bounded.** A tool that exits non-zero saying the run was not
granted something (a missing key, blocked egress, a permission, a quota) gets a one-time notice
appended to its own result: do not build a substitute, `blocked` is the required exit. If the run
is still calling tools `cycle.refusal_max_turns` turns later (default 5, `refusal_max_turns` in
the job file, 0 = off), tool calls are refused and the final result is forced; a missing result
block becomes a synthesized `blocked` record naming the tool and its exit code. Headless runs
only; files the run merely read are never classified as refusals.

**`env_allow`** scrubs the environment to the listed names plus what a process needs to run
(the interpreter's own `PYTHON*` variables among them, so a scrub never re-buffers the log a
supervisor is reading the run through).
Omitting the key does nothing; `[]` is a real instruction meaning *this job needs no
inherited variables*, and the two are deliberately distinguishable.

### Claude Code gateway (`-cc`)

`agent.py -cc` stands up an Anthropic-native `/v1/messages` endpoint and points it at whichever backend `agent.py` is configured for (`llamacpp`, `bedrock`, or `foundry`). This lets [Claude Code](https://docs.claude.com/en/docs/claude-code) — or anything that speaks the Anthropic Messages API — drive your local/self-hosted model.

```bash
python agent.py -cc                      # listen on 127.0.0.1:8788
python agent.py -cc 0.0.0.0:9000         # explicit host:port
python agent.py -cc --backend-main bedrock   # forward to a different backend
```

Then point Claude Code at it:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8788
export ANTHROPIC_API_KEY=dummy           # any non-empty value; the gateway ignores it
claude
```

How it works:

- It is a **stateless translator** — Anthropic Messages in, OpenAI chat-completions to the backend, and the streamed reply translated back to Anthropic SSE. Tool definitions, `tool_use`/`tool_result` blocks, images, and streaming tool calls are all converted in both directions.
- Claude Code runs its **own** agentic loop and resends the full conversation each turn, so the gateway bypasses `agent.py`'s session pipeline (no checkpointing, summarization, or cycle limits) and forces a fresh backend conversation per request.
- The model id Claude Code sends is ignored; requests always go to the configured main backend's model. Gemma `<think>` reasoning is suppressed so it doesn't surface as message content.
- `GET /health` reports backend reachability; `POST /v1/messages/count_tokens` returns an estimate.

**Resilience for slow/flaky backends.** If the backend cuts a stream *after* tokens have started flowing (e.g. a Bedrock proxy behind an API Gateway with a ~29s timeout), the gateway closes the turn cleanly with `stop_reason: max_tokens` instead of letting the client see "stream ended prematurely". If the backend fails *before* any content reaches the client, the gateway retries with backoff (only then — once tokens stream, retrying would duplicate output). Tunable via env vars:

| Variable | Default | Description |
| --- | --- | --- |
| `CC_GATEWAY_MAX_RETRIES` | `2` | Retries when the backend fails before producing any content. Each retry can cost up to one read-timeout, so keep it small. `0` disables. |
| `CC_GATEWAY_RETRY_BASE_DELAY` | `1.0` | Initial backoff (seconds); doubles each retry. |
| `CC_GATEWAY_RETRY_MAX_DELAY` | `8.0` | Cap on backoff (seconds). |
| `CC_GATEWAY_CONNECT_TIMEOUT` | `30` | Connect timeout to the backend (seconds). |
| `CC_GATEWAY_READ_TIMEOUT` | `600` | Read timeout — a slow backend can hold the stream this long. |
| `AGENT_FOLD_SYSTEM` | `auto` | Workaround for backends that silently drop `role:"system"`. `auto` probes the backend once (~20 tokens, ~1.5s) and caches the result per `(base_url, model)` for 7 days; `always` folds without probing; `never` disables. Applies to both `-cc` and the main agent loop. |
| `AGENT_CACHE_DIR` | `~/.cache/agent` | Where the probe result is cached (`backend_caps.json`). |

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

### Backends that silently drop `system` messages

Some OpenAI-compatible gateways accept a `role:"system"` message, return HTTP 200,
and never show it to the model — no error, no warning. The symptom is an agent that
appears to ignore its instructions. This was measured against a Bedrock proxy behind
API Gateway, where a 9-word system prompt is dropped exactly like a 6000-token one
(`developer` too); the likely cause is that Bedrock's `Converse` API takes `system`
as a top-level parameter rather than a message role, so it is lost in translation.

When `AGENT_FOLD_SYSTEM=auto` (the default), the backend is probed once and, if it
drops `system`, system content is merged into the following user message — which
every backend delivers. Backends that honour `system` (e.g. local llama.cpp) are
left untouched.

The probe **fails safe**: any error, timeout or ambiguous reply folds. Folding a
backend that works is harmless; not folding one that drops `system` silently
discards the entire system prompt.

This is a workaround — the durable fix is server-side. The 7-day cache TTL means a
server-side fix is picked up automatically without a config change.

These mitigate transient failures but cannot make a turn that genuinely needs longer than the backend's hard timeout *complete* — that requires fixing the backend (e.g. streaming via a Lambda Function URL instead of API Gateway).

Implemented in `cc_gateway.py`; requires `fastapi` + `uvicorn` (already in `requirements.txt`).

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
| `/model [main\|summary] [name]` | Set the **main** or **summary** model and persist it to `.agent/config.json` (survives restart). Bare `/model` picks the main model interactively; `/model summary` targets the summary backend; append a model id (`/model main gpt-4o`) to set it directly without the picker. |
| `/setup [main\|summary\|advisor\|test\|calibrate]` | Configure and calibrate the model roles: intro + localhost server scan, per-role endpoint menus (llamacpp/bedrock/foundry), live probes (reach, auth, model, max-context, throughput, capabilities), probe-driven calibration with drift detection. `test` probes without changing or spending anything. Auto-launches on first run in an unconfigured folder (TTY sessions only). |
| `/agent` | Scaffold an autonomous-agent identity here: type (six-phase/worker/minimal) then name, an AGENT.md/CLAUDE.md generated from the live config (real model table, advisor guidance, measured context), git provisioning when the folder isn't a repo (init / GitHub via `gh` / clone-it-yourself), and a finalizing `C0` init commit. |
| `/alias` | Detect the working Python and install an `agent` shell alias (`<python> /path/to/agent.py` → `agent`). Writes an idempotent block to `~/.bashrc` / `~/.zshrc` on Linux & Git-Bash; prints PowerShell/cmd equivalents on native Windows. |
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
| `AGENT_HEALTH_TIMEOUT` | Seconds to wait for the startup backend health probe (default `10`). Raise it for cold-start endpoints (e.g. AWS API Gateway/Lambda) that are slow on the first request. |
| `AGENT_BASH_EXE` | Windows only — full path to `bash.exe` for the `exec_command` shell, overriding Git-Bash auto-detection. |
| `AGENT_FAKE_BACKEND=1` | Build the scriptable in-process fake backend instead of any configured kind (tests, control-flow reproduction; never opens a socket). See [configurations](configurations.md#hosted-backends). |
| `AGENT_CTX_SPILL_THRESHOLD` | Chars above which a tool result is spilled to a file during context-overflow recovery (default `4000`, halving per pass to `1000`). |
| `AGENT_STALL_TIMEOUT_S` | Base seconds a request may stream zero deltas before it is aborted as a stall (default `60`; `0` disables). The effective budget is widened to cover prefill — see the next row — because a backend emits nothing until it has processed the whole prompt, and a flat guard cancels a large prompt at the instant it was about to speak. |
| `AGENT_PREFILL_TPS_FLOOR` | Prompt-processing rate, in tokens per second, the backend is known to beat (default `500`). The stall budget becomes `max(base, estimated_prompt_tokens / floor)`; a 39k-token prompt at 500 t/s gets 78 s. Lower it for a slow CPU backend. |
| `AGENT_COMPRESS_PRESSURE_FRAC` | Fraction of the context window (estimated prompt tokens / `ctx_size`) above which older repeated tool results are compressed in place (default `0.70`). Below it nothing in the history is rewritten: an in-place rewrite invalidates a prompt-caching server's key-value cache from that point on, and costs a full prefill on the next turn. |

