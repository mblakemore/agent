# Config reference

Every key the agent reads from its config file, with its default. All sections are optional; omitted keys use the defaults below. **You rarely need to write this by hand**: the [`/setup` wizard](setup-wizard.md) writes and calibrates it for you from live probes of your endpoints. For whole setups rather than single keys, see [Configurations](configurations.md); for what a headless run's keys do together, see [Headless runs](headless.md).

The agent looks for `.agent/config.json` first, then falls back to `config.json` in the working directory (wherever you run `agent.py` from). Putting it under `.agent/` keeps your local, key-bearing config alongside the other runtime files. When you run inside a git repo, the agent best-effort adds `.agent/` and `config.json` to `.gitignore` so your config (which may hold API keys) and runtime state never get committed. Outside a repo it leaves the directory untouched.

Sections, in the order a config file usually grows: [`backends`](#backends) · [`generation`](#generation) · [`think`](#think) · [`context`](#context) · [`cycle`](#cycle) · [`limits`](#limits) · [`retry`](#retry) · [`advisor`](#advisor) · [`subagent`](#subagent) · [`bedrock`](#bedrock) · [`preferences`](#preferences) · [`tools`](#tools) · [`skills`](#skills) · [`command_guards`](#command_guards) · [`allow_catastrophic_commands`](#allow_catastrophic_commands) · [`interactive`](#interactive) · [`log_dir`, `log_prefix`](#log_dir-and-log_prefix) · [`env`](#env) · [`bootstrap`](#bootstrap) · [`llamacpp`](#llamacpp) · [the monitors file](#the-monitors-file) · [full example](#full-example). Environment variables are on the [CLI page](cli.md#environment-variables).

## `backends`

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
| `kind` | `"llamacpp"` | Backend type: `"llamacpp"` (any OpenAI-compatible server), `"aicommons"`, `"foundry"`, `"bedrock"` (legacy) or `"fake"` (the in-process test backend, see [Configurations](configurations.md#hosted-backends)). |
| `base_url` | `"http://127.0.0.1:8080"` | OpenAI-compatible endpoint. |
| `model` | `""` | Model name passed to the endpoint. Empty takes the server's own: at startup the agent reads `/v1/models` and uses the model listed (the first, if several). Selects the model ID for gateway kinds. |
| `api_key` | `""` | Bearer token sent as `Authorization: Bearer <key>`. Keep this file `chmod 600`. |
| `stream` | `true` | Set `false` to use non-streaming completions (useful for debugging). |
| `enabled` | `true` (main) / `true` (summary) | Set `false` to disable the summary backend entirely. |
| `effort` | — | Reasoning effort for this role (`low`, `medium`, `high`, `xhigh`, `max`) on backends that take one. `/effort` writes it. |

**Legacy `llm` and `summary` blocks.** `llm.base_url`, `llm.model`, `summary.base_url`, `summary.model`, `summary.enabled` and `summary.max_wait_on_save` (`10`: seconds the checkpoint waits for a running summary before saving without it) are still read and mapped onto `backends.main` and `backends.summary`. New configs should use `backends`.

For **AI Commons** keys (the top-level `aicommons` block: credentials, effort, thinking, quota, `max_context_tokens`) see [AI Commons](aicommons.md). For legacy **Bedrock** keys (`api_url`, spend caps, keystore) see [Bedrock](bedrock.md).

## `generation`

Inference parameters forwarded to the LLM on every request. `--temperature`, `--top-p` and `--seed` override them for one run.

| Key | Default | Description |
| --- | --- | --- |
| `temperature` | `0.6` | Sampling temperature. |
| `top_p` | `0.95` | Nucleus sampling threshold. |
| `top_k` | `20` | Top-K sampling. |
| `min_p` | `0.0` | Min-P sampling (0 = disabled). |
| `presence_penalty` | `0.0` | Penalise tokens already present in context. |
| `seed` | `null` | Sampling seed for reproducible runs; `null` is the server's own default. Honoured by llama.cpp; a backend without a seed warns once and ignores it. |
| `enable_thinking` | `false` | Whether the **main loop** may emit thinking. Default `false`: the design is no-thinking-in-main plus the opt-in [`think` tool](#think), so the model reasons hard only when it chooses to. Set `true` to let the main model think on every turn (bounded by `max_tokens`; the `think` tool stays available either way). On tasks a capable model handles, `true` is measurably slower (~2–6×) for no accuracy gain — reach for it on genuinely hard tasks, not as a default. Requires a thinking-capable model/server (e.g. Qwen3 with `llama-server --reasoning`). |

## `think`

Budgets for the opt-in `think` tool — a separate deep-reasoning call the model can invoke on demand. Independent of `generation.enable_thinking` above: the tool is always available regardless of that flag.

| Key | Default | Description |
| --- | --- | --- |
| `depths.brief` | `2048` | `max_tokens` for a `brief` think — the default depth, sufficient for most decisions. |
| `depths.normal` | `4096` | `max_tokens` for a `normal` think — complex multi-step reasoning. |
| `depths.deep` | `16384` | `max_tokens` for a `deep` think — rarely needed. |
| `system_prompt` | unset | Sent as the system turn of every `think` call. Leave it unset unless your model's chat template needs a trigger token there to start thinking; by default thinking is switched on through the template and nothing model-specific is sent. |

The three preset **names** (`brief`/`normal`/`deep`) are fixed; only their token values are tunable — a fast small model may want smaller ceilings, a slow reasoning model larger. A malformed or non-positive override for any preset is ignored, keeping that preset's built-in default (a bad config can never zero out a budget).

## `context`

Controls context-window sizing and compaction.

| Key | Default | Description |
| --- | --- | --- |
| `ctx_size` | `114688` | Context window size in tokens. Auto-detected from the server's `/props` endpoint when available; this value is the fallback cap. |
| `max_tokens` | `16384` | Maximum tokens in a single completion. |
| `max_full_lines` | `800` | Lines of tool output kept verbatim before compaction. |
| `preview_lines` | `200` | Lines shown in the compacted preview. |
| `summary_threshold` | `5` | A message count: how many messages must have fallen out of the window before a summary of them is generated. A value above `max_context_messages` (older `/setup` runs wrote this key in characters) is replaced at startup by a fifth of the cap, with a warning. |
| `summary_max_chars` | `3000` | Maximum characters in a generated summary chunk. |
| `max_context_messages` | `30` | Hard cap on messages sent to the LLM per turn. |
| `cache_collapse_frac` | `0.15` | Prompt-caching backends only. When a request follows an idle gap longer than the cache's recorded lifetime, the window is rebuilt to this fraction of the budget behind a fresh summary instead of re-sending the whole expired prefix uncached. `0` disables. See the AI Commons page, Idle gaps. |
| `cache_keepalive` | `true` | Prompt-caching backends only. While the agent waits for input, re-send the last request with a one-token output limit before the cached prefix expires. |
| `cache_keepalive_max_idle_s` | `3600` | Stop keepalives after this long idle; beyond it the collapse is the cheaper path. |
| `cache_keepalive_probe_every` | `4` | Every n-th keepalive waits a fifth longer than the proven lifetime to learn whether the cache lives longer. `0` never probes. |

## `cycle`

Per-session run limits, the two verification gates, and the controls a headless run uses. [Headless runs](headless.md) explains how the headless keys work together.

### Turn limits

| Key | Default | Description |
| --- | --- | --- |
| `max_turns` | `250` | Stop (or wind down) after this many turns. |
| `wind_down_turns` | `10` | Turns of grace period after `max_turns` before a hard stop. |
| `max_text_only` | `3` | Consecutive text-only responses that trigger a halt (loop detection). |
| `max_total_nudges` | `6` | Total auto-nudges allowed before giving up (requires `preferences.nudge` or `--nudge`). |

### Verification gates

These are the "success check" and "claim guard" that [How it works](how-it-works.md#guardrails) and the escalation policy refer to. Both are off unless set.

| Key | Default | Description |
| --- | --- | --- |
| `success_check` | unset | A shell command that must exit `0` before the model may end a cycle (180 s timeout). It is shown to the model in the system prompt. When the model stops with a text-only completion or calls `end_cycle`, the command runs: a non-zero exit turns the stop back with a correction, at most three times per cycle. The gate stands down past the deadline, on a capability-refusal stop, and when the final result block says `blocked`, `failed` or `cannot-tell`, so a run is never asked to fake a pass for a check it has already said it cannot honestly satisfy. A passing check also counts as verification for the claim guard below. |
| `grind_elapsed_s` | `0` | With a success check set: run it once at this many seconds of wall-clock, as well as at 70% of the turn budget, and if it still fails and nothing has been edited for `grind_no_progress_turns`, suggest the advisor (the *grind* signal). `0` leaves only the turn trigger; a headless run with a deadline defaults this to half the deadline. |
| `grind_no_progress_turns` | `4` | Turns without a file edit or command that make a failing check count as grinding rather than slow. `0` disables that condition. |
| `claim_trace_max_blocks` | `0` | The claim guard. A final message that says tests were run or verified while no verification command ran this session, or that a commit was made while none was, is turned back with a correction, up to this many times per cycle. `0` is off; the [`/setup` wizard](setup-wizard.md#the-checklist) writes `2` when you tick the claim guard. Story prose can read as a claim, which is why it is off by default. |

### Headless controls

Each of these has a flag of the same meaning; the flag wins. See [Headless runs](headless.md).

| Key | Default | Description |
| --- | --- | --- |
| `deadline_s` | `0` | Wall-clock budget for the run in seconds; `0` is off. `--deadline` overrides it. At the deadline tool calls are refused and the final result is forced (exit `10`). |
| `deadline_warn_fracs` | `[0.6, 0.8, 0.92]` | Fractions of the deadline at which the model is warned, through the wind-down channel. |
| `refusal_max_turns` | `5` | Headless only. After a tool result says the run was never granted something it needs (a key, egress, a permission), tool calls are refused this many turns later and the final result is forced, the same way the deadline does. `0` is off. |
| `result_contract` | `false` | `false` leaves `--result-file` as raw text; `true` requires the final message to end with a fenced JSON block carrying `"contract": 1` that validates against the built-in schema; a path names a JSON schema file. `--result-contract` / `--result-schema` override it. |
| `result_contract_max_blocks` | `2` | How many times a missing or invalid result block redirects the exit before a synthesized `{"status": "failed"}` record is written (exit `11`). |
| `goal` | `""` | The run's stated goal, injected as an anchor at `goal_anchor_fracs`. `--goal` overrides it. Derived from a `GOAL:` stanza in the prompt when the result contract is armed. |
| `deliverables` | `[]` | Paths or globs the run must produce. A final `done` while one is absent draws a correction. `--deliverable` (repeatable) overrides it; derived from a `DELIVERABLE:` stanza when the result contract is armed. |
| `goal_anchor_fracs` | `[0.5, 0.8]` | Fractions of the deadline (or of the turn budget without one) at which the goal anchor is injected. |
| `repeat_read_nudge` | `{"n": 3, "window": 6}` | The same read-class call (tool plus arguments) repeated `n` times within `window` turns draws one nudge naming the read, once per unique call. |

## `limits`

| Key | Default | Description |
| --- | --- | --- |
| `tool_result_max_chars` | `20000` | A tool result, or an initial prompt, over this many characters is written to `.agent/spill/` and replaced in the conversation by a reference with a head/tail excerpt, lossless. `0` keeps the legacy head/tail truncation only. The files attached to a first message (`@AGENT.md`) are never spilled with it. |

## `retry`

Exponential-backoff settings for failed LLM requests.

| Key | Default | Description |
| --- | --- | --- |
| `max_retries` | `10` | Maximum retry attempts before the request fails. |
| `base_delay_seconds` | `2` | Initial retry wait. |
| `max_delay_seconds` | `60` | Cap on retry wait. |
| `backoff_multiplier` | `2.0` | Multiplier applied to delay each retry. |
| `jitter_factor` | `0.1` | Random jitter added to each delay (fraction of current delay). |

## `advisor`

An optional third role alongside `main` and `summary`: a **heavyweight model consulted as a tool**,
never as the driver. A very large model on CPU can be far too slow to run a loop and still be the
best thing available for the rare hard sub-problem, so the fast model keeps the loop and calls
`consult_advisor` when it is stuck.

`advisor` is a **top-level block**, not one of `backends`. The tier is off unless you configure it.

```json
{
  "advisor": {
    "enabled":              true,
    "base_url":             "http://127.0.0.1:8000",
    "model":                "glm-5.2",
    "prefill_token_budget": 1500,
    "max_tokens":           512,
    "max_calls_per_task":   3,
    "timeout_s":            900
  }
}
```

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | true when `base_url` is set | Tier is off unless an endpoint is configured. |
| `base_url` | — | OpenAI-compatible endpoint for the advisor model. |
| `model` | — | Model name passed to that endpoint. |
| `api_key` | `""` | Bearer token, if the endpoint needs one. |
| `kind` | `"llamacpp"` | Backend type, as for `backends`; `"aicommons"` puts the advisor on the gateway. |
| `prefill_token_budget` | `1500` | Hard ceiling on the brief handed to the advisor (see below). |
| `max_tokens` | `512` | Cap on the advisor's answer. |
| `max_calls_per_task` | `3` | Consultations per task, so a stuck loop cannot spend the session waiting. |
| `timeout_s` | `900` | Per-consultation timeout. Unset, it is derived from the budgets and the measured speed (`/setup calibrate`). |

**It distills before it asks.** A slow model's prefill dominates: feeding a multi-thousand-token
transcript to a ~3 pos/s prefill costs many minutes before it emits a word. So the caller's context
is compressed on the fast **summary** model down to `prefill_token_budget` first — the advisor reads
the brief, not the transcript. If distillation cannot get under budget, the escalation does **not**
happen silently.

**It fails open.** Any endpoint error, timeout, or disabled config returns a plain notice and the
driver proceeds *without* the advice. An escalation tool must never block the loop it exists to help.

**It is probed at startup.** If the endpoint is unreachable, `consult_advisor` is removed from the
tool registry, so the model never sees a tool it cannot use and never wastes turns escalating to a
server that is not running.

See [Configurations](configurations.md) for the full three-tier setup, and
[Recommended models](models.md) for what to run as the advisor.

## `subagent`

What a child agent started by the `subagent` tool runs on. A subagent inherits the parent's `--backend-main` / `--backend-summary` choices; these two keys give it a cheaper tier, since each one starts with a cold prompt cache anyway.

| Key | Default | Description |
| --- | --- | --- |
| `model` | the parent's | Passed to the child as `--model`. |
| `effort` | the parent's | Passed to the child as `--effort`. |

## `bedrock`

Legacy Bedrock tuning (only relevant when `backends.main.kind` or `backends.summary.kind` is `"bedrock"`).

| Key | Default | Description |
| --- | --- | --- |
| `adaptive_max_tokens` | `true` | Dynamically adjust `max_tokens` per request based on detected prompt complexity, staying within the model's limit. |

## `preferences`

Behavioural knobs that don't fit elsewhere.

| Key | Default | Description |
| --- | --- | --- |
| `system_prompt` | `""` | Your own instructions, placed first in the system message of every request, ahead of the agent's own lines (for example `"Always answer in French."`). Also settable with `/system`. Subagents run in the same folder, so they get it too. |
| `nudge` | `false` | Auto-nudge the model when it returns a text-only response. Also settable with `--nudge` CLI flag. |
| `persist_nudge` | `false` | After a text-only stop, check `git status`; if uncommitted changes exist and no commit happened this session, inject one nudge to commit. Intended for git-native agents. |
| `tool_selection_hints` | `false` | Prepend a system-prompt directive recommending `file(action='edit')` over heredoc rewrites for existing-file edits. |
| `max_text_response_chars` | `24000` | Cap on text-only response length per turn (characters). Only enforced when `nudge` is on. Prevents context-filling monologue spirals. |
| `max_post_tool_text_chars` | `2000` | Cap on prose generated after tool calls in the same turn. |
| `extra_allowed_paths` | `[]` | List of absolute directory paths the `file` tool is allowed to read/write outside the working directory. |
| `turn_hooks` | `{}` | Shell commands run around each turn: `{"start": "cmd" or ["cmd", ...], "end": ...}`. `start` runs before the turn, `end` after it, even if it raised. Each command gets 5 s and its output is discarded; a failing hook never stops the turn. For status indicators (the same job as Claude Code's UserPromptSubmit and Stop hooks). |
| `tools_whitelist` | `null` | Only these tools exist for the model and for `tool_search`. `null` = all tools. Applied before the `tools` tiers below; a whitelist with no tier lists makes everything on it core. Example: `["file", "exec_command", "search_files", "think", "task_tracker"]`. |
| `end_cycle_tool` | `false` | Offer the `end_cycle` tool, a clean "I am finished" exit for a cycle. Off by default: the model never sees it and a call to it is refused with an error, so an always-on interactive agent cannot end its own session. A cycle runner that wants the explicit exit sets `true`; a cycle still ends on the text-only completion paths either way. |
| `restart_tool` | `false` | Offer the `restart` tool: the model may restart its own process when the turn ends, the same way `/restart` does from the keyboard (the process re-executes itself with `-c` and the session continues from the checkpoint; see [the CLI](cli.md#slash-commands)). Off by default: the model never sees it and a call to it is refused. On, it is named in the catalog; more than three restarts in ten minutes are refused. |
| `restart_resume_prompt` | `""` | After a restart the `restart` tool asked for, this text goes in as the user's message and a turn runs before the input loop, so an agent that restarted itself comes back to work rather than waiting at the prompt until someone types or a monitor pings (for example `"continue"`). Empty: wait at the prompt. A keyboard `/restart` always returns to the prompt; `-a` runs resume on their own. |
| `switchable_models` | `[]` | Models the `switch_model` tool may switch to at runtime: a list (the main role's) or `{"main": [...], "summary": [...]}`. Empty, the tool does not exist to the model. With a list the tool is deferred (one catalog phrase, loaded with `tool_search`); a model outside the list is refused; a switch persists like `/model`, keeps the history and the role's effort, and the next request pays a cold prompt cache once (`cache.miss reason=model-switch`). |
| `session_events` | `true` | A detached or background `exec_command` session (and a background `subagent`) queues one item on the event bus when it exits: its exit code and output tail, delivered like a monitor's output. `false` leaves only polling and `wait`. |
| `gate` | `{"enabled": false}` | The gate in front of event sources (see [How it works](how-it-works.md#the-event-bus-sources-wait-and-the-gate)). `enabled` is the master switch, read at startup, and it does exactly three things: a source with a brief or a `bypass`/`quiet` pattern is *gated* (its output goes through the summary model, which answers wake or hold); the `gate` tool exists (deferred, found with `tool_search`); and `monitor` accepts `brief`, `bypass` and `quiet`. With it `false`, nothing is judged or held, a brief on a persisted monitor is kept but inert, `monitor` refuses the three parameters and the `gate` tool answers with an error. It changes nothing else: finished jobs still arrive (`session_events`), `wait` works either way, and the summary model is called only once a source carries a brief, so turning it on costs nothing by itself. The other keys matter only when it is on: `timeout_s` (`20`, the judge call), `digest_after_s` (`600`, held items flush as a digest after this; `0` leaves only the other flush paths), `max_held_chars` (`8000`, or when the held buffer passes this), `digest_raw_chars` (`0`, raw held text included in the digest; summaries only by default), `log_dir` (`.agent/gate`, the raw held text). |
| `initial_tasks` | `[]` | Task descriptions pre-seeded into `task_tracker` at session start (only when the task list is empty — does not overwrite an in-progress cycle). |
| `seed_tasks_persistent` | `false` | Re-seed `initial_tasks` at the start of every cycle, not just on a fresh task list. |

## `tools`

Lazy tools. The model's request carries the **core** tools' schemas; the **deferred** tools are named in one line of the system prompt ("More tools, loaded on request with tool_search: …") and the **hidden** ones not at all. The core tool `tool_search` finds and loads the others by keywords or `select:name,name`; a loaded tool stays in the session's list, and the list changes at most once per turn (one cache miss per turn that loaded something). A deferred tool called from memory runs at once when its arguments fit its schema and is loaded; otherwise the answer is its schema and it is loaded for the next call. A name from another harness (`bash`, `grep`, `Read`) is resolved to the tool it means here.

Shipped tiers: core = `exec_command`, `read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `sleep`, `task_tracker`, `tool_search` (about 3,100 tokens); deferred = everything else (about 160 tokens of catalog); hidden = `consult_advisor` (deferred once an advisor is configured), `end_cycle` (see `end_cycle_tool`), `restart` (deferred once `preferences.restart_tool` is true), `switch_model` (deferred once `preferences.switchable_models` is set) and `gate` (deferred once `preferences.gate.enabled` is true). `wait` is deferred; an agent that runs long jobs or monitors wants it core. Agent-written tools in `.agent/tools/` are deferred unless the module sets `_tier`; the old cap of ten is gone (`MAX_EXTRA_TOOLS` still caps when set). A tool module may also export `_short` (the catalog phrase), `_aliases` and `_keywords`.

| Key | Default | Description |
| --- | --- | --- |
| `core` | `[]` | Tools to move to the core tier (schema in every request). |
| `deferred` | `[]` | Tools to move to the deferred tier (catalog line, loaded on request). |
| `hidden` | `[]` | Tools to move to the hidden tier (found only by `tool_search`). |
| `catalog` | `true` | Put the one-line list of deferred tools in the system prompt. `false` leaves only the `tool_search` description to say that other tools exist. |
| `preload_from_prompt` | `true` | Before the first request of a session, load the tools the task text names (a tool name or alias) or clearly asks for, so they cost no cache miss later. |
| `implicit_run` | `true` | A deferred tool called with fitting arguments runs at once (and is loaded). `false`: the call answers with the schema and loads the tool; the next call runs. |

```json
"tools": {
  "core": ["monitor"],
  "deferred": ["list_files", "task_tracker"],
  "hidden": ["subagent"]
}
```

`/tools list` shows the tiers and the schema tokens the model sees; `/tools search WORDS`, `/tools load NAMES`, and `/tools core|defer|hide NAME [--save]` change them from the keyboard.

## `skills`

Skills are instruction files the model pulls into its context by name: a markdown file for one kind of task (joining a meeting, cutting a release, the house style for a report). Unlike a tool, a skill is text in a message: loaded mid-session it is appended, costs no cache miss, and leaves the window by trimming like any other message, so a wrong load costs a few thousand tokens for a while, not a prefix rebuild.

Discovery is always on and finds every `SKILL.md` under `.agent/skills/NAME/`, then under `.claude/skills/NAME/` (Claude Code's layout, so a project's skills serve both harnesses unchanged), then under each directory in `dirs`. The file's YAML frontmatter gives `name` (default: the directory name), `description` (required; a file without one is skipped with a warning), `keywords` (this harness's addition: whole-word phrases that load the skill at session start when the picker is off or fails) and `disable-model-invocation` (`true` keeps the skill out of the catalog and off the picker, and `tool_search skill:NAME` refuses it; only `/skill NAME` at the keyboard loads it). Other fields (`allowed-tools`, `model`, arguments) are ignored: the body is instructions, and the model runs what it says with the tools it has.

When there is at least one skill and `tools.catalog` is on, the system prompt gains a paragraph saying what skills are and one static catalog line, `Skills, loaded on request with tool_search skill:NAME: …` (about eight tokens per skill), under the same `tools.catalog` switch as the tool catalog. The model loads one with `tool_search skill:NAME` (several: `skill:NAME,NAME`); the text arrives as the tool result under a header naming the file and its directory, so a Claude Code skill's references to the scripts beside it still make sense. `/skill NAME` loads one from the keyboard at the next turn; `/skill list` shows every skill, its tokens and whether it is loaded; `/skill reload` rediscovers. Loaded names ride the checkpoint, and when trimming evicts a skill's message the summarizer is told to say so, so the model knows it once had those instructions and can load them again.

| Key | Default | Description |
| --- | --- | --- |
| `dirs` | `[]` | Extra directories of `NAME/SKILL.md`, relative to the working directory or absolute. |
| `catalog` | `[]` | Documents registered as skills without moving them or adding frontmatter: a list of `{"name", "path", "description", "keywords"?}` objects. The header of a registered document names its path only. |
| `max_chars` | `20000` | A file whose rendered text is longer is not a skill (a skill is a few thousand tokens of instructions, not a manual) and is skipped at discovery with a warning. Never more than `limits.tool_result_max_chars` (a larger value is lowered to it with a warning), so a load never spills. |
| `max_loaded_tokens` | `12000` | A load that would take the loaded skills past this is refused with a notice naming what is loaded; the skill can be loaded once those leave the window. |
| `pick` | `"off"` | The picker on the summary model. `"start"`: one call before the first request of a session (nothing is cached yet, so a load there costs no miss) with the user's own words of the first message and the catalog, answered as a JSON list of `{"name", "confidence"}`. `"always"`: also after each later message from the user, on a worker thread that never blocks a turn; a verdict not ready by the next turn boundary is applied at the one after, and one overtaken by a newer message is dropped. Any failure (timeout, error, unreadable answer) loads nothing and logs why; the `keywords` floor then decides at session start. |
| `pick_on_events` | `false` | With `"always"`, also judge each batch of bus items delivered to the model (a monitor's output, a finished job). |
| `load_threshold` | `0.75` | A pick with confidence at or above this is loaded as part of the next user turn, under its header, before the runtime note. |
| `suggest_threshold` | `0.4` | A pick between this and `load_threshold` becomes one line in the runtime note: `A skill may apply: … load it with tool_search skill:NAME.` |
| `max_picks_per_turn` | `2` | Loads per verdict at most. |
| `pick_timeout_s` | `15` | Seconds to wait for the judge. |

```json
"skills": {
  "catalog": [
    {"name": "meetings", "path": "docs/meetings.md", "description": "Joining and leaving a video call", "keywords": ["meeting", "join the call"]}
  ],
  "pick": "start"
}
```

Every verdict is one `skill.pick source=start|user|events how=judge|failopen|keywords|none|dropped picked=… suggested=… ms=…` line in the session log (`failopen` adds `err=`; `none` means nothing to pick from; `dropped` is a verdict overtaken by a newer message) and one telemetry event; every load is `skill.load name=… tokens=… by=model|picker|command|keywords`. An identity file can then say "the `meetings` skill covers calls" instead of carrying a reading list. Note that the harness adds `.agent/` to `.gitignore`, so skills a project keeps under version control belong in `.claude/skills/` or a directory named in `dirs`.

## `command_guards`

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

## `allow_catastrophic_commands`

A top-level boolean, default `false`. Separate from `command_guards`, an always-on guard in `exec_command` blocks the unambiguous system destroyers even in an unconfigured folder: a recursive delete of `/`, your home directory or a system directory such as `/etc` or `/usr`, a recursive `chmod` or `chown` of `/`, `dd` or `mkfs` on a block device, or a redirect into one. A delete of `./build` or `/tmp/x` is never blocked. `true` (or the environment variable `AGENT_ALLOW_CATASTROPHIC=1`) switches the guard off.

## `interactive`

| Key | Default | Description |
| --- | --- | --- |
| `live_input` | `true` | The interactive TUI's live-input mode: a line typed while a turn runs is delivered at the next turn boundary, a slash command typed then is held until the tool burst ends, and the session is reprinted on a terminal resize. `false` gives a plain prompt between turns. No effect under `-a` or `--no-tui`. |

## `log_dir` and `log_prefix`

Two top-level keys for the session logs.

| Key | Default | Description |
| --- | --- | --- |
| `log_dir` | `.agent/history` | Directory for the per-session logs, relative to the working directory. |
| `log_prefix` | `"session"` | The log file is `<log_prefix>_<YYYYmmdd_HHMMSS>.log`; `errors.log` sits alongside it. |

## `env`

A top-level object of environment variables to export when the agent starts, for tools and hooks that read their identity or settings from the environment. A variable already set in the environment is never overridden: a launch script, systemd unit or CI job is closer to the operator than a config file is. Values are written as strings; a `null` value is skipped.

```json
{ "env": { "SOME_TOOL_IDENTITY": "worker-3" } }
```

## `bootstrap`

For scaffolded agents whose identity file refers to state files that may not exist yet on a fresh clone. On a fresh start (not `-c`) the agent reads `AGENT.md` / `CLAUDE.md` in the working directory and creates, empty, any declared file or directory the identity file mentions that is missing. Nothing is declared by default, so nothing is created.

| Key | Default | Description |
| --- | --- | --- |
| `placeholder_files` | `[]` | Relative paths to create empty when the identity file names them and they are absent. |
| `placeholder_dirs` | `[]` | Directories to create under the same rule. |

## `llamacpp`

Extension keys sent only when the main backend's `kind` is `"llamacpp"`. They are llama.cpp sampling extensions the OpenAI-compatible endpoint accepts and a hosted backend would reject.

| Key | Default | Description |
| --- | --- | --- |
| `generation.repeat_penalty` | `1.1` | Repetition penalty. Unpenalised repetition on a small model can turn into a degeneration loop that eats the streaming deadline. |
| `generation.repeat_last_n` | `256` | How many recent tokens the penalty considers. |

## The monitors file

Not part of `config.json`, but configuration an always-on agent carries. A monitor armed by the `monitor` tool with `persist=true` is saved to `.agent/monitors.json` (or the path in `AGENT_MONITORS_FILE`) and re-armed when the agent restarts. Each entry holds what the tool call gave it: `label`, `command` or `path`, `interval_seconds` (default 15, minimum 2), `prefix`, `dedup`, and, for the gate, `brief`, `bypass` and `quiet`. The file can be edited by hand between sessions; a brief on a persisted monitor is inert until `preferences.gate.enabled` is true. See [How it works](how-it-works.md#the-event-bus-sources-wait-and-the-gate).

## Full example

```json
{
  "backends": {
    "main":    { "kind": "llamacpp", "base_url": "http://127.0.0.1:8080" },
    "summary": { "kind": "llamacpp", "base_url": "http://127.0.0.1:8082", "enabled": true }
  },
  "generation": { "temperature": 0.8, "enable_thinking": false },
  "think":      { "depths": { "brief": 2048, "normal": 4096, "deep": 16384 } },
  "context":    { "ctx_size": 32768 },
  "cycle":      { "max_turns": 50, "success_check": "pytest -q" },
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
