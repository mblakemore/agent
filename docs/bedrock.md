# Bedrock backend

The agent can use an AWS Bedrock Chat gateway ([aws-samples/bedrock-chat](https://github.com/aws-samples/bedrock-chat)) for either the main model, the summary model, or both.

## Credentials

Resolved in this order at startup:

1. **Keystore** — first `up` entry in `~/.config/agent/bedrock_creds.json` (lowest `daily_spend_usd` wins; oldest `last_checked` breaks ties so stale entries get re-tested first). Override path via `AGENT_BEDROCK_STORE`.
2. **Env vars** — `BEDROCK_API_URL` + `BEDROCK_API_KEY` (back-compat fallback, useful for CI).
3. Otherwise the agent fails fast.

### Keystore

The keystore lets you register multiple gateway/key pairs and rotate to a sibling when one saturates or 5xx's. The file is created at `0o600`, with atomic writes and process-level `flock`.

```bash
# add — runs a health probe and stores the result
python agent.py bedrock add --name prod \
  --url "https://<gw>.execute-api.us-east-1.amazonaws.com/prod" \
  --key "<api-key>"

# list — table view (--json for raw file)
python agent.py bedrock list

# retest — re-probe one entry or all
python agent.py bedrock retest prod
python agent.py bedrock retest --all

# rm — remove by name (--yes skips prompt)
python agent.py bedrock rm prod

# prune — drop entries down longer than N days (default 30)
python agent.py bedrock prune --stale-days 14
```

`list` columns: `NAME`, `STATUS` (`up`/`down`/`unknown`), `SPEND`, `LAST_CHECKED`, `LAST_ERROR`. The store auto-rotates from a saturating entry to the next eligible sibling at session start.

## Spend caps

`BEDROCK_DAILY_CAP_USD` caps combined daily spend across roles. Default caps: `$10/day` main, `$1/day` summary. Override per-role via `backends.<role>.daily_cost_cap_usd` in `config.json`.

## Config examples

```json
// llamacpp main + llamacpp summary (default)
{ "backends": {
    "main":    { "kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B" },
    "summary": { "kind": "llamacpp", "base_url": "http://127.0.0.1:8082", "model": "gemma-4-E4B" }
}}

// llamacpp main + bedrock summary (cheapest way to try Bedrock)
{ "backends": {
    "main":    { "kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B" },
    "summary": { "kind": "bedrock",  "model": "claude-v4.5-haiku", "enabled": true, "max_wait_on_save": 30 }
}}

// bedrock main + llamacpp summary
{ "backends": {
    "main":    { "kind": "bedrock",  "model": "claude-v4.5-sonnet" },
    "summary": { "kind": "llamacpp", "base_url": "http://127.0.0.1:8082", "model": "gemma-4-E4B" }
}}

// bedrock everywhere
{ "backends": {
    "main":    { "kind": "bedrock", "model": "claude-v4.5-sonnet" },
    "summary": { "kind": "bedrock", "model": "claude-v4.5-haiku" }
}}
```

Per-run override: `--backend-main bedrock` / `--backend-summary bedrock`.

## Security

- Keystore forced to `0o600` on every write (atomic tempfile + `os.replace`, no widened-mode window).
- `config.json` should be `chmod 600` if it contains an `api_key` directly. The agent warns at startup if the file is world-readable.
- `BEDROCK_API_KEY` and stored entry keys are redacted at every log site (covered by `tests/test_bedrock_security.py`).

## Known limitations

- **Prompt overhead.** Bedrock has no native tool-calling in the gateway; tools are serialized into the prompt text (~1.5–2k tokens per turn with the agent's ~10-tool set).
- **Gemma tokenizer approximation.** Token counts for non-llamacpp backends use the Gemma-3 tokenizer and overshoot Claude text by ~10–20% (safe direction).
- **No progressive streaming.** The gateway doesn't expose in-progress message text; Bedrock turns deliver a single content delta at the end.

See `plan/bedrock-integration.md` § 8 for dev-mode prompt-stuffing details and § 18.5 for the operator runbook (5xx bursts, truncation exhaustion, key rotation, cost spikes, rollback).
