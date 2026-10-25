# Config reference

Every key the agent reads from its config file, with its default. All sections are optional; omitted keys use the defaults below. **You rarely need to write this by hand**: the [`/setup` wizard](setup-wizard.md) writes and calibrates it for you from live probes of your endpoints. For whole setups rather than single keys, see [Configurations](configurations.md).

The agent looks for `.agent/config.json` first, then falls back to `config.json` in the working directory (wherever you run `agent.py` from). Putting it under `.agent/` keeps your local, key-bearing config alongside the other runtime files. When you run inside a git repo, the agent best-effort adds `.agent/` and `config.json` to `.gitignore` so your config (which may hold API keys) and runtime state never get committed. Outside a repo it leaves the directory untouched.
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
| `kind` | `"llamacpp"` | Backend type: `"llamacpp"` (any OpenAI-compatible server), `"aicommons"`, `"foundry"`, or `"bedrock"` (legacy). |
| `base_url` | `"http://127.0.0.1:8080"` | OpenAI-compatible endpoint. |
| `model` | `""` | Model name passed to the endpoint. Empty takes the server's own: at startup the agent reads `/v1/models` and uses the model listed (the first, if several). Selects the model ID for gateway kinds. |
| `api_key` | `""` | Bearer token sent as `Authorization: Bearer <key>`. Keep this file `chmod 600`. |
| `stream` | `true` | Set `false` to use non-streaming completions (useful for debugging). |
| `enabled` | `true` (main) / `true` (summary) | Set `false` to disable the summary backend entirely. |

For **AI Commons** keys (credentials, effort, quota) see [AI Commons](aicommons.md). For legacy **Bedrock** keys (`api_url`, spend caps, keystore) see [Bedrock](bedrock.md).

## `generation`

Inference parameters forwarded to the LLM on every request.

| Key | Default | Description |
| --- | --- | --- |
| `temperature` | `0.6` | Sampling temperature. |
| `top_p` | `0.95` | Nucleus sampling threshold. |
| `top_k` | `20` | Top-K sampling. |
| `min_p` | `0.0` | Min-P sampling (0 = disabled). |
| `presence_penalty` | `0.0` | Penalise tokens already present in context. |
| `enable_thinking` | `false` | Whether the **main loop** may emit thinking. Default `false`: the design is no-thinking-in-main plus the opt-in [`think` tool](#think), so the model reasons hard only when it chooses to. Set `true` to let the main model think on every turn (bounded by `max_tokens`; the `think` tool stays available either way). On tasks a capable model handles, `true` is measurably slower (~2–6×) for no accuracy gain — reach for it on genuinely hard tasks, not as a default. Requires a thinking-capable model/server (e.g. Qwen3 with `llama-server --reasoning`). |

## `think`

Budgets for the opt-in `think` tool — a separate deep-reasoning call the model can invoke on demand. Independent of `generation.enable_thinking` above: the tool is always available regardless of that flag.

| Key | Default | Description |
| --- | --- | --- |
| `depths.brief` | `2048` | `max_tokens` for a `brief` think — the default depth, sufficient for most decisions. |
| `depths.normal` | `4096` | `max_tokens` for a `normal` think — complex multi-step reasoning. |
| `depths.deep` | `16384` | `max_tokens` for a `deep` think — rarely needed. |

The three preset **names** (`brief`/`normal`/`deep`) are fixed; only their token values are tunable — a fast small model may want smaller ceilings, a slow reasoning model larger. A malformed or non-positive override for any preset is ignored, keeping that preset's built-in default (a bad config can never zero out a budget).

## `context`

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

## `cycle`

Per-session run limits.

| Key | Default | Description |
| --- | --- | --- |
| `max_turns` | `250` | Stop (or wind down) after this many turns. |
| `wind_down_turns` | `10` | Turns of grace period after `max_turns` before a hard stop. |
| `max_text_only` | `3` | Consecutive text-only responses that trigger a halt (loop detection). |
| `max_total_nudges` | `6` | Total auto-nudges allowed before giving up (requires `preferences.nudge` or `--nudge`). |

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
| `prefill_token_budget` | `1500` | Hard ceiling on the brief handed to the advisor (see below). |
| `max_tokens` | `512` | Cap on the advisor's answer. |
| `max_calls_per_task` | `3` | Consultations per task, so a stuck loop cannot spend the session waiting. |
| `timeout_s` | `900` | Per-consultation timeout. |

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

## `bedrock`

Legacy Bedrock tuning (only relevant when `backends.main.kind` or `backends.summary.kind` is `"bedrock"`).

| Key | Default | Description |
| --- | --- | --- |
| `adaptive_max_tokens` | `true` | Dynamically adjust `max_tokens` per request based on detected prompt complexity, staying within the model's limit. |

## `preferences`

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

## Full example

```json
{
  "backends": {
    "main":    { "kind": "llamacpp", "base_url": "http://127.0.0.1:8080" },
    "summary": { "kind": "llamacpp", "base_url": "http://127.0.0.1:8082", "enabled": true }
  },
  "generation": { "temperature": 0.8, "enable_thinking": false },
  "think":      { "depths": { "brief": 2048, "normal": 4096, "deep": 16384 },
                  "system_prompt": null },
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

`think.system_prompt` is sent as the system turn of every `think` call; leave it unset unless
your model's chat template needs a trigger token there to start thinking. By default thinking is
switched on through the template (`enable_thinking`), and nothing model-specific is sent.
