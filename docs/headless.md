# Headless runs

`agent.py -a` is often spawned by a supervisor — a batch queue, CI job, cron entry or another
agent — that imposes a wall-clock budget, consumes a result, and kills the process on overrun.
These behaviours exist for that audience; interactive sessions are untouched. The flags are on the
[CLI page](cli.md); the keys behind them are in the [config reference](config-reference.md#cycle);
a job file that collects them into one reviewable artifact is [the job contract](job-contract.md).

The short form:

```bash
agent -a --deadline 3600 --result-contract --result-file out.json \
  --goal "add a --verbose flag to cli.py" --deliverable cli.py \
  "add a --verbose flag to cli.py"
```

## Canonical working directory

At start the process resolves symlinks in its working directory and rewrites `$PWD` to the
physical path, logging the change. A run launched through a symlinked path otherwise produces
`os.path.relpath()` values that walk out of the repository, and `git log -- <path>` then returns
nothing *with exit 0*. Pass `--no-realpath-cwd` to keep the symlinked view (overlay-style layouts).

## Wall-clock deadline

`--deadline SECONDS` / `cycle.deadline_s`. The wind-down ladder is turn-based; a run that is slow
rather than turn-hungry got no warning and was killed by its supervisor mid-write, with work done
and no result emitted. With a deadline set, the clock starts at run start and the agent is told at
60% (heads-up), 80% (begin wrapping up) and 92% (stop working — emit your final result now), through
the same message channel the turn wind-down uses; the fractions are `cycle.deadline_warn_fracs`. At
100% the stop is structural: tool calls are refused (each still receives a paired result saying why),
the success-check and claim gates stand down, and the run takes the final-result path — the process
exits `10` and, when `--result-file` is set, the file holds whatever the model said last. A grind
escalation defaults to half the deadline when `cycle.grind_elapsed_s` is unset, and an advisor
consult that cannot finish in the time left is skipped with a notice rather than started. Unset
(`0`) is today's behaviour exactly.

## Result contract

`--result-contract`, `--result-schema FILE` / `cycle.result_contract`. Callers that consume
structured results used to prompt for a fenced JSON block and parse the raw `--result-file`
themselves — discipline, not structure, with two observed failures: a *decoy* block (the model
discusses an example and the extractor grabs it) and a `done` claim with no block at all. When
armed, the contract instruction is injected as a system message, and the final message must end
with a fenced `json` block that parses and validates against the schema (built-in default:
`contract: 1`, `status: done|failed|blocked|cannot-tell`, `summary`, optional `artifacts`,
`verify_output`, `scope`). The key `"contract": 1` is the discriminator — a block without it is
prose. Missing or invalid at exit → a correction is injected and the run continues, bounded by
`cycle.result_contract_max_blocks` (default 2); still missing at the bound, or at the deadline's
hard stop, the run exits and the result file receives a synthesized
`{"status": "failed", "summary": "<no valid result block emitted: …>"}` record, so the caller
**always** gets valid JSON; the process exits `11` unless a harder stop already owns the code. Two
valid blocks → last wins. The exit classification is folded into the JSON as
`"exit": {code, name, detail}`. An unreadable schema refuses to start (exit `14`). Without the
flag, `--result-file` is byte-identical to the raw behaviour.

## Bulk belongs in files

`limits.tool_result_max_chars`, default 20 000. A tool result over the cap is written to
`.agent/spill/<turn>-<tool>-<ts>.txt` and the model receives a reference — path, size, line
count, a head/tail excerpt, and the instruction to read it in slices — instead of the payload.
This replaces the old head/tail truncation at the same boundary and is lossless: the file holds
everything. The same cap applies to an oversized initial prompt (a spec with a data blob pasted
in), which is spilled with a reference the model is told to read first; the files the prompt
attached with `@` stay in the message. A spill directory that cannot be written falls back to the
legacy truncation with a warning. `0` disables the spill (truncation only). The context budget is
also calibrated from the server's own token counts: every streamed turn reports `prompt_tokens`,
the ratio observed-to-estimated is tracked, and the next window is sized by it — a tokenizer
mismatch between the estimator and the served model stops mattering after the first measured turn
(logged when the ratio moves by more than 10%). On the transport, an HTTP 500 is treated as a
context overflow only when its body says so (llama.cpp names it: "exceeds the available context
size", "context shift is disabled", "prompt is too long"); any other 500 is a transient, retried
with the existing jittered exponential backoff and never counted toward an overflow verdict.

## Goal anchoring and the deliverable guard

`--goal`, `--deliverable`, `cycle.goal`, `cycle.deliverables`. Long-horizon drift, observed live:
a 45-minute run produced a competent artifact answering the wrong question, self-assessed complete,
and never noticed the named deliverable was absent. With a goal and/or deliverables set, the run
receives a goal anchor at `cycle.goal_anchor_fracs` (default 50% and 80% of the deadline, or of
the turn budget when no deadline) — the stated goal, the deliverables, and a mechanical "these do
not exist yet" list from one glob sweep, no model call. A final `done` while a named deliverable is
absent draws the standard correction (bounded like the result contract); `blocked`, `failed` and
`cannot-tell` are always accepted — the guard polices claims, not outcomes. When the result
contract is armed and the initial prompt carries a `GOAL:` / `DELIVERABLE:` stanza, both are
derived automatically. The launch log names which deliverables are absent at start, so a
misspelled path is visible before a long run rather than after it.

## Repeat-read stall

`cycle.repeat_read_nudge`, default 3 in 6. The stall detector counts read-only turns;
"re-reading the same file" is a sharper and earlier signature, and it is what pre-death loops
look like. The same read-class call (tool + arguments) issued `n` times within `window` turns
triggers one nudge naming the read — once per unique call, never a nag — and, when a success
check is configured, the existing advisor escalation. A substantive batch (edit, write, exec)
resets the window.

## Capability refusals end the run, bounded

A tool that exits non-zero saying the run was not granted something (a missing key, blocked
egress, a permission, a quota) gets a one-time notice appended to its own result: do not build a
substitute, `blocked` is the required exit. If the run is still calling tools
`cycle.refusal_max_turns` turns later (default 5, `refusal_max_turns` in a job file, 0 = off),
tool calls are refused and the final result is forced; a missing result block becomes a
synthesized `blocked` record naming the tool and its exit code. Headless runs only; files the run
merely read are never classified as refusals.

## Memory ceiling

The process watches its own resident size. Past `AGENT_MEM_WARN_MB` (default 8192) it logs a
warning each turn; past `AGENT_MEM_HARD_MB` (default 12288) it exits cleanly with the `memory`
code below rather than waiting for the OOM killer to take the tmux pane or the systemd scope with
it. Memory is also trimmed after every turn (`gc` plus `malloc_trim`), which
`AGENT_DISABLE_MEM_TRIM=1` turns off for A/B measurement.

## Classifiable exit status

`-a` / `-r` only. The process exits with a stable code and prints one line to stderr,
`AGENT-EXIT: <name> <detail>`, so a supervisor can tell *completed* from *died-of-context*
without parsing logs:

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | `completed` | The run finished. |
| `1` | `error` | The run loop ended in an unclassified error. |
| `10` | `deadline` | Wall-clock deadline stop (`--deadline`). |
| `11` | `contract` | Result contract not satisfied; a synthesized `failed` result was written (`--result-contract`). |
| `12` | `context` | Context exhaustion after every reduction (spill, trim, summary halving). |
| `13` | `memory` | Resident size crossed `AGENT_MEM_HARD_MB`; the process exited before the OOM killer. |
| `14` | `config` | Configuration error at start (unbuildable backend, unreadable schema). |
| `15` | `backend` | Backend unreachable or failed after retries. |
| `16` | `acceptance` | `--job`: the runner re-ran the job's `acceptance` command after the agent exited and it failed, timed out, or could not be executed. |

Interactive sessions keep exiting `0` (or `2` on a start-up configuration error, as before).

## Declarative jobs (`--job`)

A job file collects the flags above into one reviewable artifact, and adds the one thing a flag
cannot express: an `acceptance` command the **runner** re-runs after the agent exits, whose status
is the verdict (exit `16` when it fails, however the run reported). [The job contract](job-contract.md)
is the full description; the file looks like this:

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

**The gate fails closed.** A missing binary, a timeout, and a non-zero status are all failures,
because "the check did not run" and "the check passed" must never render the same to a
supervisor. A command that will not parse is refused at launch rather than discovered as a dead
gate an hour later, and so is an unknown key — a misspelled `acceptance` is a gate that never
arms. If the run already ended with a typed hard stop (deadline, contract, context, memory,
backend), that code is kept and the gate's failure is appended to the exit detail; `16` is
reserved for a run that finished and whose artifact is wrong. The gate's full output goes to
`.agent/acceptance-out.txt`; the exit line carries its last line.

**`env_allow`** scrubs the environment to the listed names plus what a process needs to run
(the interpreter's own `PYTHON*` variables among them, so a scrub never re-buffers the log a
supervisor is reading the run through). Omitting the key does nothing; `[]` is a real instruction
meaning *this job needs no inherited variables*, and the two are deliberately distinguishable.

## Watching a run from outside

`.agent/state/cycle_status.json` holds `running` while a cycle is in progress and the terminal
result (`done`, `error`, `stopped`) when it ends, so a supervising harness polls a file rather
than reading the terminal. `AGENTPY_TELEMETRY=1` exports OTLP metrics (see the
[environment variables](cli.md#environment-variables)).
