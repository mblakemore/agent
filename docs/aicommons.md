# AI Commons backend

The agent can use the UCSB AI Commons gateway for the main model, the summary model, the advisor, or all of them. The gateway serves the Anthropic Messages API (`/v1/messages`) in front of Bedrock, so this backend gets native tool calling, streaming text, extended thinking and prompt caching. Backend kind: `aicommons` (aliases `ucsb`, `ai-commons`).

This replaces the legacy `bedrock` kind, which talks to a published bot's `/conversation` API and has none of those. The legacy kind still works and is documented in [Bedrock backend](bedrock.md).

## Quick start

```bash
export AICOMMONS_BASE_URL="https://<stage>.execute-api.us-west-2.amazonaws.com/v1"
export AICOMMONS_API_KEY="<key>"
python agent.py --backend-main aicommons --backend-summary aicommons "your task"
```

On the command line, without a configured model, the defaults are `claude-v5.5-opus` for main and `claude-v5.5-haiku` for summary. `--model` picks another main model and `--effort` sets its reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`).

## Credentials

Resolved in this order:

1. The role's own block in `backends` (`base_url`, `api_key`).
2. The shared `aicommons` block at the top of `.agent/config.json`.
3. `AICOMMONS_BASE_URL` and `AICOMMONS_API_KEY`.
4. The keystore: the first `up` entry whose `api` is `messages`.

The keystore is the same file as the legacy backend's (`~/.config/agent/bedrock_creds.json`). Each entry carries an `api` field, and the two kinds never pick each other's entries.

```bash
python agent.py aicommons add --name ucsb --url "$AICOMMONS_BASE_URL" --key "$AICOMMONS_API_KEY"
python agent.py aicommons list
python agent.py aicommons retest --all
```

`add` and `retest` probe `GET /v1/models`. A 403 there usually means the key's group does not have Messages API access.

`/setup` offers "UCSB AI Commons" as a choice and writes the same configuration. It lists the models the gateway serves (`GET /v1/models`, which is free), leaving out aliases such as the OpenAI names mapped onto other models. Models the agent has a price and context table for come first, newest version first. The summary role uses the same model unless you pick a separate one.

## Config

```json
{
  "aicommons": {
    "base_url": "https://<stage>.execute-api.us-west-2.amazonaws.com/v1",
    "api_key": "<key>",
    "model_catalog": "/path/to/model_catalog.json"
  },
  "backends": {
    "main":    { "kind": "aicommons", "model": "claude-v5.5-opus",  "effort": "high" },
    "summary": { "kind": "aicommons", "model": "claude-v5.5-haiku" }
  },
  "advisor":  { "kind": "aicommons", "model": "claude-v5.5-opus" },
  "subagent": { "model": "claude-v5.5-sonnet", "effort": "medium" }
}
```

Keys in the shared `aicommons` block apply to every role, and a role's own `aicommons` block overrides them.

| Key | Default | Meaning |
| --- | --- | --- |
| `thinking` | `adaptive` | `off` omits thinking. Effort is ignored without it. |
| `effort` | main `high`, summary `low`, advisor `max` | Sent as `output_config.effort`. |
| `max_context_tokens` | 200000 | Context the agent fills before it summarizes. A cost and latency cap; the models take 1M. |
| `max_request_bytes` | 5500000 | Requests over this fail locally. The gateway's Lambda limit is 6 MB. |
| `quota_floor_pct` | 98 | Stop at this share of the monthly allowance. |
| `quota_poll_seconds` | 300 | How often `/v1/token-usage` is polled. |
| `daily_cost_cap_usd` | main 60, summary 3, advisor 10 | A number, or `{role: n}`. `AICOMMONS_DAILY_CAP_USD` overrides. |
| `model_catalog` | none | The gateway's `model_catalog.json`, for prices and limits of models the built-in table lacks. Also `AICOMMONS_MODEL_CATALOG`. |

`ctx_size` in the `context` block still caps the window when it is set lower.

## Models and effort

Prices are only built in for some models (Opus 5.5, Opus 5, Sonnet 5, the 4.6 models, Haiku 4.5). For others, point `model_catalog` at the gateway's catalog or dollar spend is not tracked (the agent says so once).

`max_tokens` is sized to what the model can stream inside the agent's 600-second response wall (about 21,600 tokens), never above the model's own limit.

When the escalation policy fires (the success check keeps failing, or the run is stuck or grinding), the main backend first steps its effort up once, for example `high` to `xhigh`. It stays on the same model, so the cache and the stored thinking survive. The advisor path still runs after that.

Subagents inherit the parent's `--backend-*` choices. `subagent.model` and `subagent.effort` give them a cheaper tier, since each one starts with a cold cache anyway.

Thinking text is not shown in the terminal. It is written to the session log at DEBUG. Signed thinking blocks are kept on tool-calling turns and sent back, as the API requires.

## Prompt caching

The gateway caches at the end of the last message, so a turn reads from cache only when the previous request is an exact prefix of this one. On this backend the agent keeps its requests that way:

- The per-turn runtime note (clock, phase checkpoint, nudges, wind-down) is stored in history, not sent as a throwaway trailing message.
- The context window keeps its oldest message in place while everything fits. When it overflows, it drops to about 60% in one step, so there is one cache miss per eviction instead of one per turn.
- `end_cycle` is in the tool list from the first turn. Adding a tool mid-run would change the start of every request.
- The working directory is in the runtime note, not the system prompt, so runs that start within a few minutes of each other share the cached system prompt and tools.

Measured on a real eight-turn coding task: from the second turn on, 94–98% of each request was read from cache, and 83–86% over the whole session.

Every call logs an `aicommons.cache role=… read=… write=… uncached=… hit_pct=…` line. The session ends with an `aicommons.session` line that has the totals, the dollars and the quota used. `cache_read` and `cache_write` are also recorded as telemetry token kinds.

## Quota and spend

The monthly allowance is shared by every key in the account. The agent polls `GET /v1/token-usage` at startup and every five minutes:

- At `quota_floor_pct` it stops before sending, with the reset time in the message.
- When the gateway's up-front reservation (estimated prompt plus `max_tokens`) would not fit what is left, `max_tokens` shrinks to fit.

Dollar spend is computed from the real usage of each call (uncached, cache read, cache write and output tokens at the model's prices). It is kept per day, role and model in `~/.local/state/agent/aicommons_spend.json` (`AICOMMONS_SPEND_FILE`). The file is locked across processes, so subagents and parallel jobs add up correctly. A role that reaches its daily cap stops before sending.

## Errors

| Gateway response | What the agent does |
| --- | --- |
| Input too long, 413 | Context overflow: summarize and retry with less. |
| 401, 403 | Configuration error: exit 15. |
| 429 with the allowance exhausted or `x-should-retry: false` | Budget exceeded: fail over if another backend is configured, else exit 15. |
| Other 429, 5xx, 529, connection errors | Retried by the SDK, then failover. |
| 504 | The existing gateway-timeout recovery: ask the model for smaller steps. |
| `refusal` | Not nudged and not stored: exit 15 with `refusal` in the detail. |

A response cut off by `max_tokens` or the wall in the middle of a tool call does not run that call. The model is told the call was cut off and to split the work.

## Claude Code

`-cc` still works against this backend, but it is not needed. The gateway speaks the Messages API, so Claude Code can use it directly:

```bash
ANTHROPIC_BASE_URL="$AICOMMONS_BASE_URL" ANTHROPIC_API_KEY="$AICOMMONS_API_KEY" claude
```

## Checking a gateway

`scripts/aicommons_probe.py` runs the plan's eleven P0 probes against a live gateway (streaming, tools, thinking, caching, usage, errors). Probe 7 sends an oversized request and needs `--overflow`.

```bash
AICOMMONS_BASE_URL=… AICOMMONS_API_KEY=… python scripts/aicommons_probe.py
```
