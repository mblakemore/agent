# Bedrock Integration Plan — Fold AWS Bedrock backend into `agent.py`

> **Status:** **Phases 1 + 2 shipped (2026-04-23/24).** G0 (default llamacpp) and G1 (summary-on-bedrock) are in production use. G2 (main-on-bedrock) is functional but cost-gated and used selectively. See [§ 0.5 — Current integration status](#05--current-integration-status-as-of-2026-04-25) for the rolling status snapshot.
> **Owner:** mikeblakemore
> **Target:** `/droid/repos/agent/agent.py`
> **Source backend:** `/droid/repos/llmbox-cli/bedrock_api.py` (BedrockChatAPI — a REST client for the aws-samples `bedrock-chat` published API).
> **Prior art for dual-backend pattern:** `/droid/repos/llmbox-cli/llmbox_lib.py` (single backend there — Bedrock). The dual-backend requirement in this plan is *new*; llmbox-cli shows how the Bedrock client is shaped, not how to switch between two backends.

---

## 0. TL;DR

We are folding the Bedrock gateway client from `llmbox-cli` into the agent as a second selectable LLM backend, with independent `main` and `summary` role configuration, behind a feature flag defaulting to today's llamacpp behavior. Work is two PR-sized phases: Phase 1 (~4–6h, pure refactor, llamacpp still the only concrete backend) and Phase 2 (~8–12h, BedrockBackend + dev-mode prompt stuffing for tool calls). Total calendar target: **~1 week of focused work + 1 week canary dogfooding** = ~2 weeks to GA. Biggest single risk: Bedrock has no native tool-call support through the gateway, so main-on-bedrock depends entirely on prompt-stuffing format fidelity (`<tool_call>…</tool_call>` regex parse); see [§ 8.4](#84-trade-offs-honest) and [K10](#17-risks--mitigations). Second-biggest: cost exposure with no guardrail today — see [§ 6.5](#65--cost-model) for the budget mechanism added by this plan.

---

## 0.5 — Current integration status (as of 2026-04-25)

The plan's core deliverables are live on `main`. This section is the rolling status snapshot — the original sections below are kept verbatim as the design record.

### Shipped and in production

**Phase 1 (refactor, llamacpp factored behind `Backend` Protocol):**
- `llm_backend.py` lives, `LlamacppBackend` + `BedrockBackend` + `build_backend(cfg)` factory all in place.
- `_apply_backend_overrides(main_kind, summary_kind)` hook in `agent.py:367` honors `--backend-main` / `--backend-summary` CLI flags and `CICD_BACKEND_MAIN` / `CICD_BACKEND_SUMMARY` env vars.

**Phase 2 (Bedrock client + dev-mode tool calling):**
- `bedrock_api.py` ported from llmbox-cli with `send`, `poll`, `poll_message`, `send_and_wait`, `send_and_wait_conv`.
- `dev_mode_prompt.py` ports `build_dev_prompt`, `parse_dev_response`, `is_truncated`, `_TOOL_CALL_RE` regex.
- Truncation-recovery loop (§ 8.3): `MAX_CONTINUATIONS=3` continuation chain on `is_truncated()` detection; emits `bedrock.truncation_recovery.{attempted,succeeded,exhausted}`.
- `BedrockBudgetExceeded` exception + per-day spend file at `CICD/bedrock_spend.json`.

**Telemetry (the [§ 15.75](#1575--observability--telemetry) wishlist) — most of it landed:**
- `bedrock.session_spend role=X model=Y today_usd=N cap_usd=N` at session exit (atexit hook in `agent.py`, commit `ea553d3`).
- `bedrock.session_conv_count role=X model=Y count=N` at session exit alongside spend (PR #359).
- `bedrock.tokens role=X model=Y in=N out=N cost_usd=N` per call at INFO (commit `ff2dde9`). Currently only fires on the `stream_chat()` path; `complete()` path uses an unhandled logger so the line is suppressed in run logs — known one-line fix (pass logger through).
- `bedrock.token_usage role=X model=Y monthly_total=N/limit used_pct=PCT` at backend init (PR #357 = N1 — calls the gateway's `GET /token-usage` endpoint at startup, escalates to WARNING when ≥90% of monthly cap).
- `bedrock.tool_parse.result parsed_calls=N stripped_chars=N` per turn — confirms dev-mode tool parse fired.
- `backend.stream_chat.latency_ms` and `backend.complete.latency_ms` per call.
- `backend.retry.attempted backend=bedrock attempt=N/M error=...` on retries.

### Plan changes since draft

**Pricing model corrected (§ 6.5).** Earlier estimates used $15/$75 for opus and $0.25/$1.25 for haiku, which were 3× over and 4× under the actual AWS list. Live `_BEDROCK_PRICING` table in `llm_backend.py:370` now uses:

| Model | Input ($/M tokens) | Output ($/M tokens) |
|---|---|---|
| claude-v4.5-opus / v4.6-opus / v4.7-opus | 5.00 | 25.00 |
| claude-v4.5-sonnet / v4.6-sonnet | 3.00 | 15.00 |
| claude-v4.5-haiku / v3.5-haiku | 1.00 | 5.00 |
| claude-v3-opus / v3.7-sonnet | (unchanged from draft estimates) | |

Commit `bf2d8eb`. All `bedrock.cost.tick` and `bedrock.session_spend` numbers from runs prior to that commit are inflated by the old factors and should be discounted accordingly when comparing.

**Daily caps revised.** Default `_DEFAULT_DAILY_CAPS` (`llm_backend.py:384`) raised to `{"main": 60.00, "summary": 1.00}` (commit `f8e1ca6`). Original draft envisioned `{"main": 10.00, "summary": 1.00}`; opus turned out to need more headroom for a single full builder+reviewer cycle than $10 affords. Override via `BEDROCK_DAILY_CAP_USD` env var.

**Conversation reuse (N2 / PR #359).** `BedrockBackend` now caches the gateway's returned `conversationId` and passes it on subsequent calls within a session, so a multi-turn run uses one server-side conversation instead of one per turn. `_session_conv_count` exposes the actual count (target = 1) at exit. Tracked only on the `stream_chat()` path; `complete()` path still creates fresh conversations per call (small follow-up).

**Multi-turn prototype** (`proto/bedrock-multiturn`, commit `c74d42d`, **NOT MERGED**): when reuse is on, send only the *incremental* messages each turn instead of repacking the full history. Live smoke test on a healthy gateway showed turn-2 wire body shrink from 901 → 61 chars (93% reduction) and input tokens from 236 → 16. Gated behind `BEDROCK_MULTITURN=1`. Holding off until we want a controlled rollout.

**Crash-handling fixes:**
- `_call_with_retry` now catches the built-in `TimeoutError` (not just `requests.exceptions.Timeout`) — commit `0f4f606`. Without this, `bedrock_api.poll()` exhausting `poll_timeout` raised an unhandled exception and crashed the agent.
- `atexit.register(_log_bedrock_session_spend, ...)` — guarantees the session-spend line is emitted even on uncaught exception or `sys.exit`.

**Reviewer-side guardrails (built during dogfooding, not in original plan):**
- Cycle 75: review commits may only modify `tests/**` (no production-code edits in REQUEST_CHANGES fix-attempts).
- Cycle 76: pytest summary in MERGE verdict must be the verbatim final summary line (no paraphrasing).
- Cycle 77: external `gh pr/issue view` verification after every `gh pr create|merge|close|ready` — runner-level catch for fabricated PR/issue numbers.
- Cycle 78: when an issue body has a "How to verify" command, reviewer must run it and paste output before MERGE.

### Currently in flight

- **Subagent tool** (PR #372 + fix `b99dd0f`) — primary use is reducing parent-agent token spend by delegating exploration to a child subprocess. Working end-to-end (real subprocess, real result-file capture). Not yet wired into the CICD agent's prompts; future cycle will teach the builder when to delegate. Adjacent to but not strictly part of the bedrock plan; affects bedrock cost model because subagents cut the parent's per-turn input tokens.
- **Multi-turn merge** (above) — when ready.
- **`bedrock.tokens` logger fix** — pass the agent logger into `complete()` so summary-path token telemetry shows in stdout logs. ~1 line.

### Operational mode summary

| Mode | Main | Summary | Status |
|---|---|---|---|
| G0 (default) | llamacpp/gemma | llamacpp/gemma | proven, dozens of clean CICD merges; this is the safe-default daily driver |
| G1 (canary) | llamacpp/gemma | bedrock/haiku | proven, working as the everyday config when bedrock summary is desired |
| G2 (full) | bedrock/opus | bedrock/haiku | functional; used selectively because per-cycle spend is ≥10× G1 |

Switch via env: `CICD_BACKEND_MAIN=bedrock` and/or `CICD_BACKEND_SUMMARY=bedrock` on a `cicd.sh` invocation, with `BEDROCK_API_URL` + `BEDROCK_API_KEY` set. Default model when `bedrock` is selected with no model override is `claude-v4.5-opus` for main, `claude-v4.5-haiku` for summary (`agent.py:379`).

### What's intentionally still pending

- Wider G2 use — kept selective on cost grounds; G1 covers most CICD use and is essentially free.
- `complete()`-path conversation reuse — currently only `stream_chat()` reuses; `complete()` still spawns a fresh conversation per call. Small one-line follow-up but not a current pain point.
- The detailed sign-off / abandonment / decision-log machinery in [§ 22](#22--sign-off-checklist), [§ 23](#23--abandonment-criteria), [§ 24](#24--decision-log) remains useful for future Bedrock integration revisits but isn't actively maintained day-to-day now that Phases 1 + 2 are live.

---

## 1. Goal & scope

Make the main LLM endpoint and the summarization LLM endpoint each independently selectable at startup between:

- **`llamacpp`** — the current OpenAI-compatible streaming HTTP client baked into `_llm_request` (`requests.post → {base_url}/v1/chat/completions`).
- **`bedrock`** — the `BedrockChatAPI` from `llmbox-cli/bedrock_api.py`, copied into the agent repo.

Users must be able to configure any of: `main=llamacpp, summary=llamacpp` (today's default), `main=bedrock, summary=llamacpp`, `main=llamacpp, summary=bedrock`, `main=bedrock, summary=bedrock`.

**In scope**
- New `agent/llm_backend.py` module holding a minimal `Backend` protocol and two concrete implementations: `LlamacppBackend` (wraps the current `_llm_request` path) and `BedrockBackend` (wraps `BedrockChatAPI`).
- New `agent/dev_mode_prompt.py` module holding the prompt-stuffing serializer + regex tool-call parser (ported from llmbox).
- Config surface change to declare the two backends.
- Light refactor of `_llm_request`, `_summary_request`, `AsyncSummarizer` to dispatch through a backend instance instead of calling `requests.post` directly.
- New tests that mock Bedrock's REST calls and exercise the dev-mode prompt round-trip.

**Out of scope**
- Rewriting `run_agent_single` — the streaming tool-call loop stays as-is; only the transport and (for Bedrock main) the wire format change.
- Direct `boto3` integration. `bedrock_api.py` talks to the aws-samples `bedrock-chat` gateway over HTTPS — it does **not** use `boto3.client('bedrock-runtime')`. We keep that same gateway-client model. (If a future plan wants raw `boto3.InvokeModelWithResponseStream`, it becomes a third backend then, not now.)
- New CLI surface beyond per-role override flags.
- Tokenizer changes beyond labelling approximations.
- TUI changes.

## 2. Guiding principles

1. **Wrap, don't rewrite.** `_llm_request` stays the single entry point for main-model calls; internally it routes to a backend object. `run_agent_single`'s streaming loop (lines ~1945-2052 — SSE parse, `tool_calls_by_index`, `iter_lines`, `data: [DONE]`) is untouched. For Bedrock main, the backend serializes OpenAI messages into a dev-mode prompt on the way in and parses `<tool_call>` blocks into OpenAI-shape SSE deltas on the way out — the loop still only sees OpenAI deltas.
2. **Feature-flag behind config, llamacpp default.** Absent config, behavior is identical to today. Opting into Bedrock is explicit.
3. **Symmetric treatment of main and summary.** Whatever abstraction covers `_llm_request` must also cover `_summary_request` / `AsyncSummarizer`. No two parallel switch-statements.
4. **Streaming is the hard edge; summary is the soft edge.** The main path is streaming with incremental tool-call assembly. The summary path is single-shot JSON. The backend interface must serve both without forcing bedrock-gateway to fake an SSE stream or llamacpp to fake polling.
5. **Cancellation is non-negotiable.** The existing double-escape / `check_cancelled()` path must trip through Bedrock's polling loop at least as often as through llamacpp's `iter_lines` loop.

## 2.5 — Success metrics

Orthogonal to per-phase Definition-of-Done (which asks "did the code ship?"), these measure "did the integration win?" Evaluated at the end of the 1-week canary + 1-week self-CI opt-in window (see [§ 15.5](#155--rollout-strategy)).

| # | Metric | Target | Source | Evaluated |
| - | --- | --- | --- | --- |
| S1 | Summary-path p95 latency with `summary=bedrock` | ≤ 2× llamacpp p95 baseline | telemetry log keyed `backend.complete.latency_ms` — see [§ 15.75](#1575--observability--telemetry) | weekly |
| S2 | Main-path cancel latency (double-escape to process-unblock) | ≤ 5s p99 with `main=bedrock` | telemetry `cancel.latency_ms`; matches existing llamacpp target | weekly |
| S3 | Tool-call parse failures (malformed `<tool_call>` blocks) | ≤ 2% of Bedrock main turns | telemetry `bedrock.tool_parse.result` counter | weekly |
| S4 | Truncation-recovery success rate (turns that required continuation and got a clean parse within 3 retries) | ≥ 95% | telemetry `bedrock.truncation_recovery.{attempted,succeeded}` counters | weekly |
| S5 | Bedrock spend per operator per day, steady state | ≤ $10/day main + $1/day summary (per-role caps; enforced at the counter — see § 6.5) | gateway billing + local counter | daily |
| S6 | New cancel-latency regressions on llamacpp path | 0 | compared against baseline captured in [§ 5.5](#55--baseline-measurements) | once, end of Phase 1 |
| S7 | CICD loop success rate with `main=bedrock` on opt-in operator | ≥ the llamacpp baseline ± 5pp | CICD `progress.md` completion rate | weekly during self-CI opt-in |

If any of S1–S5 fails its target at the end of the canary + opt-in window, the GA decision is blocked until the owner documents mitigation in the decision log ([§ 24](#24--decision-log)).

## 3. Required reading (verified 2026-04-22)

- `/droid/repos/llmbox-cli/bedrock_api.py` (184 lines) — the whole module.
- `/droid/repos/llmbox-cli/llmbox_lib.py:25` — `_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)` — the exact regex we'll port.
- `/droid/repos/llmbox-cli/llmbox_lib.py:181-237` (`Agent.__init__`, health, list_models).
- `/droid/repos/llmbox-cli/llmbox_lib.py:333-482` — **critical**: dev-mode turn (`_run_turn_dev` at 334), long-mode turn, and `_process_response` / `_process_response_from_text`. Shows the full flow `send_and_wait → extract_text → _parse_tool_calls → _strip_tool_calls → _sanitize`.
- `/droid/repos/llmbox-cli/llmbox_lib.py:461-466` — where response text is fed through `_parse_tool_calls(full_content)` and then `_strip_tool_calls`.
- `/droid/repos/llmbox-cli/llmbox_lib.py:637-674` — `_handle_truncation`: the recovery path when `<tool_call>` appears without closing `</tool_call>`. The sanity check at **line 641** — `"<tool_call>" not in full_content or "</tool_call>" in full_content` — is the short-circuit, followed by up to 3 continuation requests that re-poll the server.
- `/droid/repos/llmbox-cli/llmbox_lib.py:716-760` — `_build_tool_system_prompt`: serializes the `tools` list into a plain-text tool manual with a one-shot `<tool_call>{...}</tool_call>` example. The wire shape used in the example is at **line 746** (`<tool_call>\n{...}\n</tool_call>`).
- `/droid/repos/llmbox-cli/llmbox_lib.py:762-813` — `_build_prompt`: composes `[System]…[End System]`, optional initial files, optional progress summary, then a budget-capped reverse walk of `conversation_history` emitting `User:` / `Assistant:` / `[Tool call: name(args)]` / `[Tool result (name): …]` segments, terminated with `"\n\nAssistant:"`.
- `/droid/repos/llmbox-cli/llmbox_lib.py:881-899` — `_parse_tool_calls` / `_strip_tool_calls` / `_sanitize` (the last strips `<think>` tags and normalizes Unicode).
- `/droid/repos/llmbox-cli/llmbox.py:44-86` (`_DEFAULT_CONFIG`, `_load_config`) — the Bedrock-specific config block shape.
- `/droid/repos/llmbox-cli/llmbox.py:380-472` — how the CLI picks mode and falls back between dev and long (the fallback is orthogonal to our use — we always use dev semantics for Bedrock main).
- `/droid/repos/llmbox-cli/llmbox.py:604,735` — user-visible mode labels: `"dev    Prompt stuffing with rolling summary (default)"` and `--mode` CLI help `"dev (prompt stuffing) or long (server-side caching)"`.
- `/droid/repos/agent/agent.py:187-254` (`_DEFAULT_CONFIG`, `_load_config`, `BASE_URL`).
- `/droid/repos/agent/agent.py:379-417` (`_llm_request`) — the only call site for `requests.post` on the streaming path.
- `/droid/repos/agent/agent.py:749-776` (`_summary_request`) — the summary path.
- `/droid/repos/agent/agent.py:867-905` (`_generate_summary`), `910-993` (`AsyncSummarizer`) — how the summary endpoint is selected and how the thread runs.
- `/droid/repos/agent/agent.py:1358-1414` (`_check_api_health`, `_detect_ctx_size`, `_list_available_models`) — startup probes used by the banner and context budgeter.
- `/droid/repos/agent/agent.py:1443-1524` (session start, summarizer wiring, health check).
- `/droid/repos/agent/agent.py:1891-2052` (request body construction + SSE streaming tool-call assembly).
- `/droid/repos/agent/token_utils.py` — Gemma-3 tokenizer fallback logic.
- `/droid/repos/agent/cancel.py:39-53` (`check_cancelled`, `request_cancel`).
- `/droid/repos/agent/tests/test_agent_loop.py:12-38` (`create_mock_response` helper — the mock pattern every loop test uses).
- `/droid/repos/agent/tests/test_agent_llm_retries.py:1-107` (existing `_llm_request` retry tests).
- `/droid/repos/agent/tests/test_context_overflow.py` (500-overflow tests).
- `/droid/repos/agent/tests/test_summary_request_signature.py` (AST-level guard — will constrain the new `_summary_request` signature).
- `/droid/repos/agent/CICD/cicd.sh:191-204` — how the CICD loop invokes the agent.
- `/droid/repos/agent/plan/ui-upgrade-from-llmbox-cli.md` and `ui-upgrade-followup.md` — for tone/format.

## 4. Design decisions / open questions

Each has a proposed default — confirm or override before Phase 1. Confirmed decisions are mirrored into [§ 24 decision log](#24--decision-log).

| # | Question | Proposed default | Blocking phase |
| - | --- | --- | --- |
| D1 | Backend interface shape — single `Backend` class with both streaming and non-streaming methods, or two narrow protocols (`StreamingBackend`, `OneShotBackend`)? | **Single class.** Method set is small (`stream_chat(...)`, `complete(...)`, `health()`, `list_models()`, `detect_ctx_size()`). Two classes would double the registry and force dual instantiation when main and summary share the same backend kind. | Phase 1 |
| D2 | `stream_chat` return shape — yield OpenAI-style SSE deltas (same dicts the current loop already parses), or yield a richer normalised event type (`TextDelta`, `ToolCallDelta`, `Done`)? | **OpenAI-style dicts.** The current loop at `agent.py:1963-2014` is the only consumer. Emitting the same shape means zero loop changes. `BedrockBackend` is the one that has to translate from dev-mode text output into faux SSE deltas; the loop does not know which backend fed it. | Phase 1 |
| D3 | Config shape — `llm.main` / `llm.summary` **or** `backends.<name>` registry with `main`/`summary` pointers? | **Registry.** `backends: { main: {...}, summary: {...} }` — each backend object carries its own `kind` field (`"llamacpp"` or `"bedrock"`) plus kind-specific keys (`base_url`, `model`, ...  for llamacpp; `api_url`, `api_key`, `origin`, `model`, `poll_*`  for bedrock). Rationale: the existing `_config["llm"]` and `_config["summary"]` blocks are flat — promoting to `llm.main`/`llm.summary` would leak every caller's `_config["llm"]["base_url"]` site. Registry style is also a cleaner match for llmbox-cli's flat Bedrock block. Full migration table in [§ 6](#6-config-surface). | Phase 1 |
| D4 | Runtime switching — hot-swap per `/model` command, or startup-only? | **Startup-only.** `/model` already has subtle interactions with the async summarizer thread (K4 from the UI plan) — allowing it to also swap backends would double the thread-safety surface area. Backend kind is locked after `run_agent_interactive()` constructs the backend pair. Per-role override flags `--backend-main {llamacpp,bedrock}` / `--backend-summary {llamacpp,bedrock}` let CLI callers change per-run without editing `config.json`. | Phase 2 |
| D5 | Default when neither `config.json` nor flags specify anything. | **Both = llamacpp at today's URLs** (`http://127.0.0.1:8080` main, `http://127.0.0.1:8082` summary). This is what the rollback depends on. | Phase 1 |
| D6 | Bedrock tool-calling — how does the agent drive tools when `bedrock_api.py`'s send payload at `bedrock_api.py:58-67` sends only `contentType: "text"` with no `toolConfig`, no native tool support? | **Use llmbox's prompt-stuffing mechanism (dev mode).** llmbox-cli already does exactly this: `llmbox_lib.py:25` defines `_TOOL_CALL_RE`; `llmbox_lib.py:334` is `_run_turn_dev` (`"Single turn in dev mode (prompt stuffing)."`); `llmbox_lib.py:716-760` builds the tool manual and one-shot `<tool_call>\n{...}\n</tool_call>` example (example wire shape at line 746); `llmbox_lib.py:762-813` serializes message history into the dev prompt; `llmbox_lib.py:461-462,881-895` parses the response text back into structured tool calls. `BedrockBackend.stream_chat` adopts this end-to-end (see [§ 8](#8-tool-calling-on-bedrock)). Main-on-bedrock ships in Phase 2 alongside summary-on-bedrock. | Phase 2 |
| D7 | Tokenization for Bedrock responses — same Gemma-3 tokenizer, a Bedrock-specific tokenizer (Anthropic has one), or character-based approximation? | **Gemma-3 fallback with an `~approx` label.** `count_tokens_from_message` is only used to size context windows, not for cost accounting. The Gemma-3 tokenizer overcounts slightly for Claude text (rough 1.1x overshoot in practice) which is safe — it errs toward over-reserving context. Add a one-line log warning at session start when summary or main = bedrock so the operator knows counts are estimates. | Phase 1 |
| D8 | Auth — env vars, AWS profile, instance role? | **Env vars only (match llmbox-cli).** `BEDROCK_API_URL` and `BEDROCK_API_KEY` already read by `bedrock_api.py:14-16`. Document in README. No credential discovery, no `~/.aws/credentials` reader. If the env is missing and config `kind=bedrock`, the backend fails fast at construction with a clear message. | Phase 1 |
| D9 | What does `_check_api_health` / `_detect_ctx_size` do when backend=bedrock? | **Delegate to the backend.** `BedrockBackend.health()` calls `api.health()` (GET `/health` on the gateway). `BedrockBackend.detect_ctx_size()` returns `None` (the gateway doesn't expose it; model-capability lookup against `_MODEL_CONTEXT_CHARS` already in `llmbox_lib.py:169-179` gives a reasonable per-model default — port that dict). | Phase 1 |
| D10 | Cancellation cadence for Bedrock — `bedrock_api.py:74-95` polls with adaptive exponential backoff (0.3s → 5s max). `check_cancelled()` must be called at least once per poll. | **Already done** — `cancel_check=` parameter is already passed through `poll()` / `poll_message()` at `bedrock_api.py:80-81,108-109`. Just thread `lambda: check_cancelled()` in from the agent. No code change in `bedrock_api.py`. | Phase 1 |
| D11 | Where does `BedrockChatAPI`'s `_DEFAULT_CONFIG` fallback-to-env behavior go? | **Remove from the class, move into the backend factory.** Having `BedrockChatAPI.__init__` read `os.environ` at import time is surprising. The agent's backend factory reads env once, constructs the client explicitly. Keep `bedrock_api.py` otherwise byte-identical to the llmbox-cli version so future merges are clean — the env-reading lines at `bedrock_api.py:14-16` get patched in the agent's copy only. | Phase 1 |
| D12 | Test fakes — mock `requests.post/get` on `BedrockChatAPI.session`, or mock `BedrockBackend` directly? | **Mock the `requests.Session` inside the client.** The `bedrock_api.py` methods are thin; exercising them ensures the URL, headers, and polling flow stay correct. For loop tests, patch `agent._llm_request` same as today — the backend switch happens underneath it, not above. | Phase 2 |

### Open questions raised during revision (not plan-blocking)

Tracked here and in [§ 25 open-questions log](#25--open-questions-log).

- **D6-a:** When the dev-mode serializer walks OpenAI history, assistant messages carry `tool_calls` but the *next* `tool` message's `tool_call_id` may not match order. llmbox's prompt is positional (`[Tool result (name): …]` — no id), so the serializer assumes the `tool` messages appear in the same order as the preceding assistant's `tool_calls`. Non-issue for the agent's own output but could bite on replayed histories. Flag at review.
- **D6-b:** Should `DEV_MODE_PREAMBLE` include the agent's existing system prompt (from `agent.py`'s loop) or replace it? Proposed: **merge** — agent's system prompt is prepended inside `[System]`, tool manual follows. Needs a one-line test in `test_bedrock_dev_mode_roundtrip.py`.

## 5. Prerequisites

- [ ] All [§ 4 open questions](#4-design-decisions--open-questions) resolved (D1–D12 stamped into [§ 24](#24--decision-log)).
- [ ] Clean `main` (no in-flight PRs touching `agent.py:379-417`, `:749-776`, `:867-993`, or `:1443-1524`).
- [ ] **Coordination with the self-CICD loop:** the repo is actively iterated via CICD (see `CICD/agent.md`, recent cycles ~73). Phase 1 PR author declares a soft freeze on `agent.py` (`379-417`, `749-993`, `1443-1524`, `1891-2052`) for the PR's review window — other cycles may still land but must not touch those ranges. If they do, rebase-on-land is the Phase 1 PR author's responsibility. See also [§ 20 task 1.0](#20--work-breakdown).
- [ ] Both llama-server endpoints running for the baseline capture (see UI plan § 11.1 and this plan's [§ 5.5](#55--baseline-measurements)).
- [ ] `bedrock_api.py` from `/droid/repos/llmbox-cli` pinned to a known SHA (current: `1653b71`); the agent's copy carries that SHA as a docstring reference for drift detection. See [§ 19](#19--cross-repo-contract).
- [ ] `llmbox_lib.py`'s dev-mode functions (`_TOOL_CALL_RE`, `_build_tool_system_prompt`, `_build_prompt`, `_parse_tool_calls`, `_strip_tool_calls`, `_sanitize`, `_handle_truncation`) pinned to the same llmbox-cli SHA — the agent's ported copy (in `agent/dev_mode_prompt.py`) carries that SHA as a docstring reference.
- [ ] A Bedrock gateway reachable from the dev box, with `BEDROCK_API_URL` + `BEDROCK_API_KEY` set in the shell. (If unavailable, Phase 1 still ships — only the live-integration smoke in Phase 2 is gated.)

## 5.5 — Baseline measurements

Before any code change, measure and commit current behavior so Phase 1's "no regression" DoD has a concrete anchor. **Task 1.1 populates this table.**

| # | Metric | Current value | Target (post-integration) | How measured |
| - | --- | --- | --- | --- |
| B1 | Main-path median latency, `simple.stdout.log` scenario | `<TBD: task 1.1>` ms | within ±10% after Phase 1 | `scripts/capture_baseline.sh` + `scripts/measure_latency.py` (spec: parse `log.info` lines for request-start/response-complete timestamps; median of 5 runs) |
| B2 | Main-path p95 latency, same scenario | `<TBD: task 1.1>` ms | within ±15% after Phase 1 | same |
| B3 | Summary-path median latency | `<TBD: task 1.1>` ms | within ±10% after Phase 1 | same |
| B4 | Cancel latency (double-escape to process-unblock), llamacpp main | `<TBD: task 1.1>` ms p99 | within ±500ms after Phase 1 | new test: trigger cancel 0.5s into an `exec_command` tool call, measure to `CancelledError` log line |
| B5 | Baseline diff size (stdout captures against `baseline/*.stdout.log`) | 0 lines (by definition) | 0 lines after Phase 1 | `diff baseline/simple.stdout.log <new capture>` |
| B6 | CICD loop success rate, last 10 cycles | `<TBD: task 1.1>` % | within ±5pp after Phase 1 | tally from `CICD/progress.md` |
| B7 | Tokens consumed per turn, CICD loop, llamacpp | `<TBD: task 1.1>` tokens median | informational only (Bedrock path will differ due to dev-mode preamble; see [§ 10](#10-tokenization)) | parse `log.info` token-count messages |
| B8 | Daily llamacpp request count, typical operator | `<TBD: task 1.1>` requests | used as denominator in [§ 6.5](#65--cost-model) | CICD cycle count × avg turns/cycle |

Commands to run:
```bash
# Baseline capture (requires both llama-servers up)
/droid/repos/agent/scripts/capture_baseline.sh

# Latency extraction (new script — spec only; implement in task 1.1)
/droid/repos/agent/scripts/measure_latency.py baseline/simple.stdout.log
```

Results committed to `/droid/repos/agent/baseline/METRICS.md` (file to be created by task 1.1; no need to create any file during planning).

## 6. Config surface

Today (`agent.py:187-223`):

```json
{
  "llm": {"base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B"},
  "summary": {"base_url": "http://127.0.0.1:8082", "model": "gemma-4-E4B", "enabled": true, "max_wait_on_save": 10}
}
```

Proposed (registry — D3):

```json
{
  "backends": {
    "main": {
      "kind": "llamacpp",
      "base_url": "http://127.0.0.1:8080",
      "model": "gemma-4-31B"
    },
    "summary": {
      "kind": "llamacpp",
      "base_url": "http://127.0.0.1:8082",
      "model": "gemma-4-E4B",
      "enabled": true,
      "max_wait_on_save": 10
    }
  }
}
```

A Bedrock summary backend:

```json
{
  "backends": {
    "main": {"kind": "llamacpp", "base_url": "http://127.0.0.1:8080", "model": "gemma-4-31B"},
    "summary": {
      "kind": "bedrock",
      "api_url": "",
      "api_key": "",
      "origin": "http://localhost:8000",
      "model": "claude-v4.5-haiku",
      "poll_interval": 0.3,
      "poll_backoff": 1.5,
      "poll_max_interval": 5.0,
      "poll_timeout": 180,
      "enabled": true,
      "max_wait_on_save": 30
    }
  }
}
```

Leaving `api_url` / `api_key` empty strings means "read from `BEDROCK_API_URL` / `BEDROCK_API_KEY` env at backend construction" — same convention `bedrock_api.py` already carries.

### Migration strategy

**Back-compat shim, not a hard cut.** `_load_config` keeps the old `llm` / `summary` top-level blocks working: if `backends` is absent but `llm` (or `summary`) is present, synthesise the registry at load time:

```python
# Pseudocode — inside _load_config
if "backends" not in config:
    config["backends"] = {
        "main": {"kind": "llamacpp", **config.get("llm", _DEFAULT)},
        "summary": {"kind": "llamacpp", **config.get("summary", _DEFAULT_SUMMARY)},
    }
```

Rationale: zero external-config breakage at land time; users migrate at their own pace. The shim stays in the codebase through one release cycle, then a Phase 4 (not in this plan) removes it.

### Call-site fan-out

Every site that currently reads `_config["llm"][...]` or `_config["summary"][...]`:

| File:Line | Today reads | After refactor |
| --- | --- | --- |
| `agent.py:254` (`BASE_URL = _config["llm"]["base_url"]`) | for main requests | module-level `_main_backend.base_url` — **or** drop `BASE_URL` entirely and route through the backend |
| `agent.py:389` (`f"{BASE_URL}/v1/chat/completions"`) | llamacpp request target | `_main_backend.stream_chat(body, ...)` |
| `agent.py:757-759` (`_summary_request` URL/model picking) | summary override | `_summary_backend.complete(prompt)` |
| `agent.py:879-895` (`_generate_summary` fallback) | summary → main fallback | `_summary_backend.complete(prompt)` with internal try/except; on failure, delegate to `_main_backend.complete(prompt)` |
| `agent.py:1472-1482` (`_check_api_health`, `_detect_ctx_size`) | llama `/health`, `/slots` | `_main_backend.health()` + `_main_backend.detect_ctx_size()` |
| `agent.py:1487-1492` (`on_session_start` emit payload) | `base_url`, `model` | add `backend_main`, `backend_summary` keys |
| `agent.py:1501-1523` (summary health probe) | summary `/health` | `_summary_backend.health()` |
| `agent.py:1675` (recovery call to `run_agent_single`) | positional `base_url=BASE_URL` | pass `_main_backend` |
| `agent.py:1893` (request body model) | `_config["llm"]["model"]` | `_main_backend.model` |

## 6.5 — Cost model

Bedrock gateway calls cost real money; this plan is the first place the agent incurs a $/call charge. Without a cost model, operators can't detect spend spikes or set budgets.

### Per-call estimate (order-of-magnitude; confirm with actual gateway billing)

Assumes Anthropic Claude models on the gateway. Values reflect public Bedrock prices as of early 2026; the plan owner must re-verify before Phase 2 GA.

| Model | Input $/1M tok | Output $/1M tok | Typical tokens/turn (main) | Cost/turn, main | Typical tokens/turn (summary) | Cost/turn, summary |
| --- | --- | --- | --- | --- | --- | --- |
| claude-haiku-4.x | ~$0.25 | ~$1.25 | `<TBD>` in / `<TBD>` out | ~$`<TBD>` | ~3k in / ~300 out | ~$0.001 |
| claude-sonnet-4.x | ~$3 | ~$15 | `<TBD>` in / `<TBD>` out | ~$`<TBD>` | ~3k in / ~300 out | ~$0.014 |

Dev-mode preamble adds ~1.5–2k input tokens per main turn ([§ 8.4](#84-trade-offs-honest)). Factor that into the "in" side for main.

### Daily / monthly projection under CICD load

From [§ 5.5](#55--baseline-measurements) B8: typical operator runs `<TBD>` requests/day via the CICD loop.

- **Summary-only on bedrock (Claude Haiku):** `<TBD> × $0.001` ≈ $`<TBD>`/day ≈ $`<TBD>`/month. Expected to be under $5/day/operator; cheap path.
- **Main-on-bedrock (Claude Sonnet):** order of magnitude higher. Real estimate deferred until B8 is populated. Plan owner commits to populating before Phase 2 GA.

### Budget guardrail

A daily in-process counter, incremented on every `BedrockBackend.stream_chat` / `.complete` call with an estimated cost based on request + response size.

- Counter persists across agent runs via `/droid/repos/agent/CICD/bedrock_spend.json` (written at end of each turn; key is `YYYY-MM-DD`).
- New config keys: `backends.main.daily_cost_cap_usd` (default: `10.00`), `backends.summary.daily_cost_cap_usd` (default: `1.00`).
- On cap breach: log `ERROR` "Bedrock daily spend cap exceeded ($X.XX of $Y.YY); aborting Bedrock call" and raise a new `BedrockBudgetExceeded` exception. The agent's existing error path surfaces this as a tool-call or summary failure; the loop continues with llamacpp fallback on the summary path, or exits on the main path (same as today when main-LLM is unreachable).
- Override: env var `BEDROCK_DAILY_CAP_USD` (numeric) overrides both config keys when set, for CI runs that want a tighter cap.

Persistence file has mode `0o600` — contains usage counters only, no secrets.

**Known gap:** token counts for cost estimation are approximate per [§ 10](#10-tokenization); the Gemma tokenizer overshoots Claude input tokens by ~10–20%, making our cost estimate conservative (overcounts spend). That's the safe direction for a guardrail.

## 7. Backend abstraction

### 7.1 Interface (new `agent/llm_backend.py`)

```python
from typing import Iterator, Protocol, Callable

class StreamDelta(dict):
    """A single SSE-style delta chunk. Shape:
        {"choices": [{"delta": {...}}]}
    where delta may carry 'content' (str) and/or 'tool_calls' (list of
    {index, id, type, function: {name, arguments}}).
    """

class Backend(Protocol):
    kind: str        # "llamacpp" | "bedrock"
    model: str

    def health(self) -> tuple[bool, str]: ...
    def detect_ctx_size(self) -> int | None: ...
    def list_models(self) -> list[str]: ...

    def stream_chat(
        self,
        *,
        messages: list[dict],     # OpenAI-format messages (role, content, tool_calls, name)
        tools: list[dict] | None, # OpenAI tool schema (or None)
        gen_params: dict,         # temperature, top_p, top_k, presence_penalty, max_tokens
        cancel_check: Callable[[], None],
        log,
    ) -> Iterator[StreamDelta]: ...

    def complete(
        self,
        *,
        prompt: str,              # already-formatted prompt (for summaries)
        gen_params: dict | None = None,
        cancel_check: Callable[[], None] | None = None,
        timeout: float = 120,
    ) -> str: ...
```

**Why these methods exactly:**
- `stream_chat` mirrors what `run_agent_single` consumes today. Returning an iterator of OpenAI-shape dicts means the SSE parse loop at `agent.py:1963-2014` becomes `for chunk in backend.stream_chat(...)` with a near-zero diff — the `iter_lines` / `data: [DONE]` / `json.loads` shell disappears into the backend but the `delta.get("content")` / `delta.get("tool_calls")` handling stays.
- `complete` serves the summary path. Bedrock's `send_and_wait` is essentially this; llamacpp's non-streaming POST is trivially wrapped.
- `health` returns `(ok, detail)` to match `_check_api_health`'s return tuple at `agent.py:1358-1371`.
- `detect_ctx_size` returns `None` when the backend can't introspect (bedrock); callers already handle that — see `agent.py:1478-1482`.
- `list_models` lets `/models` and `/model` commands work with both backends.

### 7.2 `LlamacppBackend`

Concrete wrapper around the current code. Key method:

```python
def stream_chat(self, *, messages, tools, gen_params, cancel_check, log):
    body = {
        "model": self.model,
        "messages": messages,
        "temperature": gen_params["temperature"],
        "top_p": gen_params["top_p"],
        "top_k": gen_params["top_k"],
        "presence_penalty": gen_params["presence_penalty"],
        "max_tokens": gen_params["max_tokens"],
        "chat_template_kwargs": {"enable_thinking": False},
        "cache_prompt": True,
        "tools": tools,
        "tool_choice": "auto",
        "stream": True,
    }
    response = _llm_request_raw(log, self.base_url, json=body,
                                stream=True, timeout=(30, 300))
    for raw_line in response.iter_lines(decode_unicode=False):
        cancel_check()
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line or not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            break
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue
    response.close()
```

`_llm_request_raw` is `_llm_request` renamed and parameterised on `base_url` (so it's not pinned to the module-level `BASE_URL`). Retry logic, `ContextOverflowError`, and the 3×-500 rule move with it.

### 7.3 `BedrockBackend`

Wraps `BedrockChatAPI`. Two delivery strategies for `stream_chat`:

**Option A (Phase 2, default): Synthesise SSE deltas after `send_and_wait` returns.** Poll until done, extract text, run the dev-mode parser (§ 8), emit one `content` delta (for the text portion with `<tool_call>` blocks stripped) and one `tool_calls` delta per parsed call, then `[DONE]`. Pros: simple, no gateway-side changes required. Cons: no progressive UI — the spinner sits until the whole response arrives.

**Option B (optional Phase 3): Long-poll with progressive chunking.** Modify the poll to read partial text from `conv.messageMap[last_id]` each iteration, run a streaming variant of the parser that emits deltas as completed `<tool_call>` blocks appear, and yield incrementally. Requires the gateway to return in-progress message text; not confirmed by the current `bedrock_api.py`. Treat as nice-to-have.

The summary path uses `send_and_wait` directly — no tool parsing — so Option A is fine there regardless.

Cancellation flows through `cancel_check` into `poll()` / `poll_message()` at `bedrock_api.py:80-81,108-109` without further changes.

### 7.4 Backend factory

```python
def build_backend(cfg: dict) -> Backend:
    kind = cfg.get("kind", "llamacpp")
    if kind == "llamacpp":
        return LlamacppBackend(cfg)
    if kind == "bedrock":
        return BedrockBackend(cfg)
    raise ValueError(f"Unknown backend kind: {kind!r}")
```

Instantiated once in `run_agent_interactive` after config load; stored on the module-level `_main_backend` / `_summary_backend` globals (parallel to today's `BASE_URL`). Tests monkeypatch these.

## 8. Tool-calling on Bedrock

`bedrock_api.py` sends only `{"message": {"content": [{"contentType": "text", "body": prompt}], "model": ...}}` (`bedrock_api.py:58-67`) and `extract_text` only harvests `contentType == "text"` parts (`bedrock_api.py:157-163`). The gateway exposes no native tool schema or `toolUse` blocks. Rather than extend the gateway, we adopt the mechanism llmbox-cli already uses in its `dev` mode: **serialize tools into the prompt text, parse `<tool_call>…</tool_call>` out of the response text.** This is proven code in `llmbox_lib.py`; we port it verbatim into a new `agent/dev_mode_prompt.py` module and wire `BedrockBackend.stream_chat` through it.

### 8.1 Prompt side — OpenAI messages + tools → flat dev prompt

`BedrockBackend.stream_chat` calls a helper `_serialize_messages_to_dev_prompt(messages, tools)` ported from `llmbox_lib.py:716-813`. The output structure (copied from llmbox verbatim where possible; only the role-to-text mapping changes shape to accept OpenAI-format tool_calls lists):

1. **Tool instructions block** — ported from `llmbox_lib.py:718-757` (`_build_tool_system_prompt`). Iterates the OpenAI `tools` list, extracts `function.name`, `function.description`, `function.parameters.properties` and `required`, and emits:

   ```
   [System]
   You are an autonomous agent with access to tools…

   AVAILABLE TOOLS:
     tool_name: description
     Parameters:
       - param_name (string, required): description
       - other (integer): description
     …

   TO USE A TOOL, include a tool call block in your response:

   <tool_call>
   {"tool": "tool_name", "args": {"param1": "value1", "param2": "value2"}}
   </tool_call>

   RULES:
   - You may use multiple tool calls in a single response.
   - After tool execution, you will receive results and can make more calls or give a final answer.
   - When done, respond with plain text (no tool_call block).
   …
   [End System]
   ```

   The one-shot `<tool_call>` example is exactly `llmbox_lib.py:746` — `<tool_call>\n{"tool": "tool_name", "args": {...}}\n</tool_call>`.

2. **Conversation transcript** — ported from `llmbox_lib.py:795-810`. Walks `messages` (which is OpenAI-shape: `[{role, content, tool_calls?, name?}, ...]`) and emits role-labeled segments:
   - `{"role": "system", "content": X}` → prepended to the `[System]` block (merge with the tool instructions).
   - `{"role": "user", "content": X}` → `User: X`.
   - `{"role": "assistant", "content": X, "tool_calls": [{id, function:{name, arguments}}]}` → `Assistant: X`, followed by one `[Tool call: name({args_as_json})]` line per tool_call. Note: OpenAI tool_calls use `function.name` / `function.arguments` (string-encoded JSON). We unpack those; the prompt shape matches `llmbox_lib.py:803-805`.
   - `{"role": "tool", "name": N, "content": X, "tool_call_id": ID}` → `[Tool result ({N}): X]`. The `tool_call_id` is dropped — dev-mode prompts are positional, not keyed.
3. **Terminating `\n\nAssistant:`** — exactly as `llmbox_lib.py:810`, to cue the model.

Differences from llmbox's `_build_prompt`:
- No budget-capped reverse walk here. The agent layer already does context management upstream (existing summarizer + checkpoint path in `agent.py`); the serializer takes whatever `messages` it's given.
- No `summary_state` / `initial_files` handling inside the serializer. Those are represented as normal `system` messages by the caller before serialization.

### 8.2 Parse side — dev prompt response text → OpenAI SSE deltas

`BedrockBackend.stream_chat` calls `_parse_dev_response_to_sse_deltas(text)` which ports `_TOOL_CALL_RE` and `_parse_tool_calls` from `llmbox_lib.py:25,881-895` and re-emits as OpenAI streaming shape.

The regex (verbatim): `re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)`.

The translation: for each match yielding `{"tool": NAME, "args": ARGS}` (or `{"name": ..., "arguments": ...}` — llmbox tolerates both, see `llmbox_lib.py:887-890`), emit one delta of the form

```python
{"choices": [{"delta": {"tool_calls": [{
    "index": i,
    "id": f"call_{uuid4().hex[:12]}",
    "type": "function",
    "function": {
        "name": name,
        "arguments": json.dumps(args),
    },
}]}}]}
```

Before any tool_call deltas, emit a single `content` delta carrying `_strip_tool_calls(text)` followed by `_sanitize(...)` (ported from `llmbox_lib.py:897-904`). This preserves the narrative prose the model emitted around the tool calls, which the agent's loop already echoes to the user.

Finally emit a synthetic `[DONE]` sentinel — handled inside the backend generator via `return` (the loop breaks on `data: [DONE]` when reading llamacpp SSE; for the Bedrock path the generator just terminates).

### 8.3 Failure recovery

The main failure mode: model emits `<tool_call>` without a closing `</tool_call>` (truncation or format drift). llmbox handles this in `_handle_truncation` (`llmbox_lib.py:637-674`), short-circuited by the sanity check on **line 641**: `if "<tool_call>" not in full_content or "</tool_call>" in full_content: return full_content`. If truncated, it issues up to 3 continuation requests (`MAX_CONTINUATIONS = 3`, line 639) with a prompt `"Your response was truncated. Continue from exactly where you left off. Last part ended with:\n...{tail}"` and concatenates until `</tool_call>` appears or the budget is spent.

For Phase 2 we port this verbatim into `BedrockBackend.stream_chat`: after `send_and_wait` returns, run the sanity check; if truncated, issue up to 3 follow-up `send_and_wait_conv` calls on the same `conversationId` before parsing. The continuation path is self-contained in the backend — the agent loop never sees retries.

> **Call-out — load-bearing cross-call state.** The truncation-recovery loop re-enters `send_and_wait_conv` with the same `conversationId`, which means the Bedrock backend has to hold that id across what the agent loop sees as a single `stream_chat` call. This is the only place Bedrock conversation-id state leaks into our design. The reuse is necessary: without server-side continuity, asking the model to "continue from where you left off" would restart cold and lose the preceding content. Keep this coupling explicit and isolated to the backend module.

Secondary failure: JSON inside the `<tool_call>` block is malformed. `_parse_tool_calls` already silently skips malformed entries (`json.JSONDecodeError` branch at `llmbox_lib.py:893-894`). We log at DEBUG and let the agent loop's existing error-handling path (no tool calls → plain text response → model retries next turn) take over.

### 8.4 Trade-offs (honest)

- **Model must follow instructions precisely.** The entire contract is baked into the prompt — if the model emits `<toolcall>` (no underscore), or `<tool_call> …` without the JSON body, the parser misses it. Mitigation: port llmbox's exact one-shot example (they have empirical evidence this wording works for Claude models).
- **Malformed tool-call blocks need recovery.** See § 8.3 — llmbox already has it, we port verbatim.
- **System-prompt drift.** The dev-mode system text balloons with tool docs and the one-shot example. With the agent's ~10-tool CICD setup the tool manual runs roughly 1.5-2k tokens. For the llamacpp backend this is a non-issue (native `tools` field); for Bedrock it's unavoidable until the gateway supports tools natively.
- **Token overhead.** Native `tools` on an Anthropic Converse endpoint doesn't consume user-visible tokens (it's handled server-side); our dev-mode serialization does. Every Bedrock main turn pays this overhead. Within the plan's scope (CICD loop, ~10 tools) the overhead is acceptable; see K12.
- **Loop isolation.** `BedrockBackend.stream_chat` does the serialize → call → parse round-trip and emits OpenAI deltas. The agent's loop at `agent.py:1963-2014` never learns that Bedrock uses prompt stuffing. This is real work, but it's a self-contained translation layer — zero changes to `run_agent_single`.

## 9. Message-format translation

### 9.1 Format delta table

| Concern | OpenAI / llama.cpp | Bedrock gateway (`bedrock_api.py`) | Note |
| --- | --- | --- | --- |
| Request entry point | POST `/v1/chat/completions` (single call) | POST `/conversation` → returns conv+msg IDs, then GET `/conversation/{id}` polled until `role=assistant` | Bedrock is two-phase |
| Streaming | SSE `data: {…}\n\ndata: [DONE]` | No streaming — polling only | Backend synthesises deltas |
| Message list | `messages: [{role, content, tool_calls?, name?}, ...]` | `{message: {content: [{contentType, body}], model}}` — single prompt at a time | Serialized via dev-mode prompt (§ 8.1) |
| System prompt | `{"role": "system", "content": "..."}` in messages | No system-prompt field in gateway payload | Merged into `[System]` block |
| Tool definition | `tools: [{type:"function", function:{name, description, parameters}}]` | Not in payload | Rendered as text manual (§ 8.1) |
| Tool call (assistant→) | `tool_calls: [{id, type:"function", function:{name, arguments: "..."}}]` | Not supported natively | Emitted as `<tool_call>{...}</tool_call>` text by model, parsed back (§ 8.2) |
| Tool result (→assistant) | `{"role":"tool", "name":..., "content":...}` | Not supported natively | Rendered as `[Tool result (name): …]` in the next prompt |
| Response text | `choices[0].message.content` (or streamed deltas) | `message.content[*].body` where `contentType == "text"` | Joined with `\n`, then `_strip_tool_calls` + `_sanitize` |
| Reasoning | `content` may contain `<think>…</think>` | Separate `contentType == "reasoning"` — `bedrock_api.py:165-173` | `BedrockBackend.complete` returns plain text; reasoning exposed via a separate accessor if the UI wants it |
| Token counts | `usage.{prompt,completion,total}_tokens` in response | Not exposed by gateway | Use local Gemma-3 tokenizer (D7) |
| Conversation continuity | Stateless — client re-sends full history every call | `conversationId` threads server-side | Dev mode is stateless on our side: discard `conversationId` after each turn. (The only place we keep it is inside the Bedrock backend, scoped to truncation-recovery continuation calls — see § 8.3.) |

### 9.2 Every place `agent.py` assumes OpenAI shape

| Location | Assumption | Fix |
| --- | --- | --- |
| `agent.py:389` | `requests.post → /v1/chat/completions` | Moves into `LlamacppBackend.stream_chat` |
| `agent.py:773` | `response.json()["choices"][0]["message"]["content"]` | Moves into `LlamacppBackend.complete`; `BedrockBackend.complete` does its own extraction |
| `agent.py:1361-1362` | llama `/health` | `backend.health()` |
| `agent.py:1376-1386` | llama `/slots` | `backend.detect_ctx_size()` returns None for bedrock |
| `agent.py:1390-1412` | llama `/v1/models` | `backend.list_models()` — bedrock uses gateway `/openapi.json` (already in `bedrock_api.py:175-184`) |
| `agent.py:1963-2014` (SSE parse loop) | OpenAI SSE deltas | Loop now reads from `backend.stream_chat()` iterator; parsing stays because backend yields OpenAI-shape dicts (D2) — for Bedrock, synthesized from `<tool_call>` parse (§ 8.2) |

Message-list → Bedrock prompt translation for summary: the summary path already pre-formats its prompt (`_build_summary_prompt`) into a plain string. `BedrockBackend.complete(prompt=...)` calls `send_and_wait(prompt)` unchanged. No dev-mode wrapping needed on the summary path.

## 10. Tokenization

`token_utils.count_tokens` uses `unsloth/gemma-3-4b-it` as the tokenizer. This is wrong (systematically over- or under-counts) for Claude/Bedrock text, but the agent uses tokens to size the context window, not for billing. Claude's vocabulary is larger and its BPE is different, so Gemma estimates are approximate — in practice, our measurement suggests a 10–20% overshoot for English prose, which is the safe direction.

**Plan:**
- No new tokenizer dependency. Keeping `transformers + gemma` is enough for both backends.
- At session start, when either backend = bedrock, log an info-level line: `"Token counts for bedrock messages are approximate (Gemma-3 tokenizer used)."`.
- `_MODEL_CONTEXT_CHARS` from `llmbox_lib.py:169-179` gets ported as a module constant in `BedrockBackend`, letting `detect_ctx_size()` return a per-model default when introspection fails.
- For main=bedrock specifically, the dev-mode prompt overhead (§ 8.4) adds ~1.5-2k tokens of tool manual to every turn. Budget for this in `_MODEL_CONTEXT_CHARS` defaults — reserve headroom equal to the serialized tool block length at session start.
- If a future plan needs exact counts, `anthropic`'s tokenizer (tiktoken-like) can be added behind a `try/import` in `token_utils.py` — out of scope here.

## 11. Cancellation

The cooperative cancel path (`cancel.py:39-53`) relies on `check_cancelled()` being called inside every long-running loop. Sites the agent already covers:

- `agent.py:1965` — inside SSE streaming loop (per line).
- Tool-execution dispatcher (various).

New sites in the Bedrock path:

- `BedrockBackend.stream_chat`: pass `cancel_check=lambda: check_cancelled()` through `send_and_wait_conv`. That flows into `bedrock_api.py:80-81`, which already calls the callback once per polling iteration. Initial poll interval is 0.3s, max 5s — so cancel latency is ≤ 5s worst case, which is acceptable for a human operator.
- Truncation-recovery continuation loop (§ 8.3): call `cancel_check()` before each of the up-to-3 follow-up `send_and_wait_conv` calls, and again during their polls (already covered by the underlying `poll_message`).
- `BedrockBackend.complete`: same — pass `cancel_check` into `send_and_wait`.

No changes required in `bedrock_api.py` itself — the hooks are already there. The backend just wires them.

## 12. Auth and secrets

`BedrockChatAPI.__init__` currently reads `os.environ.get("BEDROCK_API_URL", "")` and `BEDROCK_API_KEY` at import time (`bedrock_api.py:14-16`). Per D11, that falls back into the agent's `build_backend` factory instead, which does:

```python
api_url  = cfg["api_url"] or os.environ.get("BEDROCK_API_URL", "")
api_key  = cfg["api_key"] or os.environ.get("BEDROCK_API_KEY", "")
if not (api_url and api_key):
    raise ConfigError("Bedrock backend requires BEDROCK_API_URL and "
                      "BEDROCK_API_KEY (either in config.json or environment)")
```

**User setup documentation** (lands in README during Phase 1):

```
# Shell setup for Bedrock backend
export BEDROCK_API_URL="https://<your-gateway>.amazonaws.com/prod"
export BEDROCK_API_KEY="<your-api-gateway-key>"
```

Match llmbox-cli's convention: env vars, no profile discovery, no instance role. Agents that want instance-role auth can subclass `BedrockBackend` — out of scope here.

See [§ 18.75](#1875--security-review-checklist) for the structured review list.

## 13. Testing strategy

### 13.1 Tests that currently mock `_llm_request`

Grep confirms ~20 test files patch `agent._llm_request` (see [§ 3 required reading](#3-required-reading-verified-2026-04-22) for the list). All of them work against the OpenAI SSE shape via `create_mock_response()` in `test_agent_loop.py:12-38`. They will continue to work unchanged provided `LlamacppBackend.stream_chat` is what `_llm_request` becomes the thin wrapper around — i.e. the patch target `agent._llm_request` still exists as the entrypoint, or we provide an equivalent `agent._main_backend.stream_chat` patch target.

**Recommendation:** keep `agent._llm_request(log, json=body, stream=True, …)` as a module-level function that internally calls `_main_backend.stream_chat(...)`. Existing mocks patch at the old call site, old signature. New Bedrock tests patch `_main_backend` directly. Zero churn in `test_agent_loop.py`, `test_context_overflow.py`, `test_agent_llm_retries.py`.

For `_summary_request`: it already changed signature once (per `test_summary_request_signature.py`, which is AST-based and forbids a `log` parameter). Keep the signature `_summary_request(prompt, base_url=None, model=None)` — internally route through `_summary_backend.complete(prompt=prompt)` and ignore `base_url`/`model` when `_summary_backend.kind == "bedrock"` (log a debug message). `test_summary_request_signature.py` passes untouched.

### 13.2 New tests (Phase 2)

| Test file | Purpose |
| --- | --- |
| `tests/test_llm_backend_factory.py` | `build_backend({"kind":"llamacpp"})` returns `LlamacppBackend`; `{"kind":"bedrock"}` returns `BedrockBackend`; unknown kind → `ValueError`; bedrock without env + empty config → `ConfigError`. |
| `tests/test_bedrock_backend.py` | Mock `BedrockChatAPI.session` (it's a `requests.Session`). Verify: `complete(prompt="hi")` sends the right POST body with `contentType: "text"`, polls GET, returns the extracted text. `stream_chat` with empty tools yields a single `content` delta then ends. `health()` returns `(False, "...")` on 503. `cancel_check` is called per poll. |
| `tests/test_bedrock_dev_mode_roundtrip.py` | **Pure string-manipulation tests, no network.** Covers `_serialize_messages_to_dev_prompt` and `_parse_dev_response_to_sse_deltas`. Cases: (1) OpenAI `tools` list + single-user message → prompt text containing `[System]`, `AVAILABLE TOOLS:`, the one-shot `<tool_call>` example, `User: ...`, terminal `Assistant:`; (2) history containing an assistant `tool_calls` + subsequent `tool` result message → prompt includes `[Tool call: name({args})]` and `[Tool result (name): …]` lines; (3) response text with one `<tool_call>{"tool":"file","args":{"path":"/x"}}</tool_call>` → yields one `content` delta (stripped text) and one `tool_calls` delta with `function.name == "file"` and `function.arguments == '{"path": "/x"}'`; (4) response text with two `<tool_call>` blocks → two tool_calls deltas with `index` 0 and 1; (5) malformed JSON inside `<tool_call>` → parse silently drops it, no exception; (6) truncated `<tool_call>` (no closing tag) → the sanity check from `llmbox_lib.py:641` reports truncated=True (the actual continuation loop is mocked). |
| `tests/test_bedrock_backend_tool_loop.py` | End-to-end: mock `BedrockChatAPI.send_and_wait` to return a synthetic assistant message whose text contains `<tool_call>`. Run `BedrockBackend.stream_chat` through `run_agent_single` with a stubbed tool registry. Verify the agent's loop observes one assistant-text chunk, one tool-call chunk, executes the stub, and issues a second `stream_chat` call whose serialized prompt includes the `[Tool result ...]` line. |
| `tests/test_backend_swap.py` | Round-trip: construct a session with `backends.summary.kind = "bedrock"` (mocked) and `backends.main.kind = "llamacpp"`; run one turn via `run_agent_single` with a mocked `_main_backend.stream_chat`; verify `_summary_request` routes through the bedrock mock. Also the inverse: main=bedrock, summary=llamacpp. |

Target: each new test ≤ 80 lines, no real network calls, no `boto3`.

### 13.3 Regression verification

Re-run `scripts/capture_baseline.sh` (from UI plan § 11.1 and this plan's [§ 5.5](#55--baseline-measurements)) with both backends=llamacpp. Diff against pre-refactor baseline should be empty — the llamacpp path wraps the old code path without behavior change.

Add a second capture under `BEDROCK_API_URL=... backends.summary.kind=bedrock` once Phase 2 lands. The stdout diff should show only the one-line tokenizer-approximation info banner and any wall-clock differences in the summary phase; tool-output and main-loop lines unchanged.

Add a third capture with `backends.main.kind=bedrock backends.summary.kind=bedrock`. Expected diff: loop structure identical (the prompt-stuffing round-trip is opaque to the UI), tool names/args identical, assistant narrative may differ in wording (different model) but tool execution sequence should match on deterministic CICD inputs.

### 13.4 Live smoke test (Phase 2 DoD)

A reusable script `/droid/repos/agent/scripts/bedrock_smoke.sh` (spec only; implement in task 2.8). Pass/fail criteria, all of which must hold for Phase 2 DoD:

1. **Summary live:** `backends.summary.kind=bedrock`, run one CICD-style turn, verify summary arrives within `poll_timeout` and is non-empty.
2. **Main live, tool call:** `backends.main.kind=bedrock`, prompt "List files in `/tmp` using the `exec_command` tool," verify one `<tool_call>` parse, one shell exec, one subsequent assistant reply.
3. **Main live, two tools:** prompt that forces two consecutive tool calls; verify both parse, both execute, final reply arrives.
4. **Cancel:** trigger double-escape 1s into a Bedrock poll; verify process unblocks within 5s (per [§ 11](#11-cancellation)).
5. **Truncation recovery:** a synthetic test (`max_tokens` intentionally too low in `gen_params`) forces truncation; verify the continuation loop fires, final combined response parses cleanly.
6. **Cost counter:** after the run, `CICD/bedrock_spend.json` has a nonzero entry for today's date.

Script exits 0 on all-pass, 1 on any failure with a human-readable diagnostic line.

## 14. CICD compatibility

The CICD loop (`CICD/cicd.sh:191-204`) invokes:

```bash
python3 /droid/repos/agent/agent.py -a --verbose --nudge "${AGENT_MD}..."
```

It does **not** parse agent stdout programmatically — `grep -n "stdout|exit code|return code|check.*output|\$\?" CICD/cicd.sh` returns nothing. The loop only relies on the agent's exit code and on side-effects committed to `CICD/` (progress rows, results files). Adding a one-line tokenizer-approximation banner at startup is safe.

With main=bedrock enabled in Phase 2, the CICD loop's tool-call stream goes through the prompt-stuffing parser. The loop is tool-heavy (`exec_command`, `file`, `gh`) — each of those tools appears as a `<tool_call>` block on the wire and is parsed into the same OpenAI `tool_calls` shape the CICD loop already consumes. No CICD-side changes.

**Monitor:** the CICD progress writes happen through the `file` and `exec_command` tools. No output format touches Bedrock. No risk.

## 15. Rollback plan

Each phase lands as exactly one PR; revert the PR. The feature-flag architecture means default behavior (both backends = llamacpp) is unchanged, so a revert is a no-op for users who never opted in. If a user's `config.json` already set `kind: bedrock` for summary or main, a revert falls back to the back-compat shim from § 6 — `backends.*` is read but `kind` is ignored, and the legacy `llm.base_url`/`summary.base_url` paths run. If their config doesn't have those legacy keys, summarization falls back to main (the existing `_generate_summary` fallback at `agent.py:891-901`). No lost state, no corruption.

## 15.5 — Rollout strategy

Feature-flag-off by default in every release. Promotion to GA happens through explicit gates.

| Stage | Duration | Who opts in | Config | Gate to next stage |
| --- | --- | --- | --- | --- |
| **G0: Ships OFF** | permanent default | nobody (yet) | no `backends.*` block, or `kind: llamacpp` everywhere | Phase 2 PR merged |
| **G1: Canary** | 1 week | single operator (plan owner), summary only | `backends.summary.kind: bedrock`, main stays llamacpp | metrics [§ 2.5](#25--success-metrics) S1, S2, S5 meet targets; no new P0 bugs |
| **G2: Self-CI opt-in** | 1 week | one CICD cycle per day uses `main=bedrock` via a new env flag `CICD_BACKEND_MAIN=bedrock` in `cicd.sh`; remaining cycles stay llamacpp | canary's config + main-on-bedrock for opt-in runs | metrics S1–S7 meet targets; CICD success rate ≥ baseline − 5pp |
| **G3: Documented option** | ongoing | any operator can opt in via README | README shows all four combinations; default still llamacpp | none — steady state |
| **G4: Default flip** (OUT OF SCOPE for this plan) | — | — | — | explicit follow-up plan required; not guaranteed |

**Gate decision-makers:**
- G1 → G2: plan owner, after reviewing metrics and decision log.
- G2 → G3: plan owner + one reviewer, sign-off recorded in [§ 24 decision log](#24--decision-log).
- G4: new plan.

**Self-CICD safety during G2:** the `CICD_BACKEND_MAIN=bedrock` flag is opt-in per cycle, not global. If the Bedrock path regresses, the next cycle runs llamacpp without intervention. This protects the self-CI loop from cascade failures that would otherwise block *all* cycle progress while Bedrock is down.

## 15.75 — Observability / telemetry

Existing agent logging is rich (`log.info` / `log.warning` / `log.debug` throughout `agent.py`). The plan below names specific log lines Phase 2 must add. All telemetry piggybacks on the existing `logging` module — no new dependency.

### Log lines to add

| Key | Emitted in | Log level | Example payload |
| --- | --- | --- | --- |
| `backend.complete.latency_ms` | `LlamacppBackend.complete`, `BedrockBackend.complete` after response | INFO | `"backend=bedrock model=claude-v4.5-haiku role=summary latency_ms=1340 ok=true"` |
| `backend.stream_chat.latency_ms` | both backends, after generator exits | INFO | `"backend=llamacpp model=gemma-4-31B role=main latency_ms=3220 deltas=47"` |
| `backend.retry.attempted` | `LlamacppBackend.stream_chat` on 500; `BedrockBackend.send_and_wait` on transient | WARN | existing `log.warning("LLM request failed (attempt %d/%d)…"` — already at `agent.py:414` |
| `bedrock.tool_parse.result` | `BedrockBackend._parse_dev_response_to_sse_deltas` | INFO | `"parsed_calls=2 stripped_chars=120 parse_errors=0"` |
| `bedrock.truncation_recovery.attempted` | entry to the continuation loop (§ 8.3) | WARN | `"conversationId=abc123 tail_chars=64"` |
| `bedrock.truncation_recovery.succeeded` | after recovery loop exits with parse success | INFO | `"attempts=2 conversationId=abc123"` |
| `bedrock.truncation_recovery.exhausted` | after 3 failed retries | ERROR | `"conversationId=abc123 attempts=3"` |
| `bedrock.cost.tick` | after every Bedrock call (before returning) | DEBUG | `"role=main in_tokens=2100 out_tokens=340 estimated_cost_usd=0.0042 daily_total_usd=1.23"` |
| `cancel.latency_ms` | already exists in `cancel.py` path; extend to log on cancel observe | INFO | `"latency_ms=320 site=backend.stream_chat backend=bedrock"` |

### Aggregation

Out of scope for this plan: no dashboard, no metrics pipeline. The log lines above are structured enough that `grep` + `awk` on the agent's log file produces weekly numbers for [§ 2.5](#25--success-metrics). If a follow-up plan wants a real dashboard, the keys above are stable contract.

Stitching key: every log line in a single agent invocation carries the existing agent log prefix (timestamp + PID), which is enough to correlate a single turn's stream_chat / tool_parse / cost_tick lines.

## 16. Phased rollout

### Phase 1 — Backend abstraction + llamacpp wrapper (~4-6 hours, 1 PR)

Land: new `agent/llm_backend.py` with `Backend` protocol, `LlamacppBackend`, `build_backend` factory. Refactor `agent.py:379-417, 749-776, 867-905, 1358-1414, 1443-1524, 1675, 1893` to call the backend. Back-compat config shim. New tests for the factory and the llamacpp wrapper.
Decomposed into tasks 1.0–1.7 in [§ 20](#20--work-breakdown).

**DoD:**
- [ ] `python agent.py` with no config change produces byte-identical baseline output (verified via capture script).
- [ ] All existing tests pass unchanged.
- [ ] `_config["backends"]["main"]["kind"] == "llamacpp"` after load on a legacy config.
- [ ] `_main_backend.stream_chat` is the only site that touches `/v1/chat/completions`.
- [ ] `test_llm_backend_factory.py` passes.
- [ ] README updated with new config shape + legacy compat note.
- [ ] **Docs:** `baseline/METRICS.md` populated from [§ 5.5](#55--baseline-measurements); short ADR-style note at top of `agent/llm_backend.py` summarizing D1–D3, D5, D7, D9–D11 with links to this plan.
- [ ] **Telemetry:** the two `backend.*.latency_ms` log lines from [§ 15.75](#1575--observability--telemetry) fire on every call.

### Phase 2 — BedrockBackend (main + summary via dev-mode prompt stuffing) (~8-12 hours, 1 PR)

Land:
- Copy `bedrock_api.py` into `agent/` (with D11 env-var patch noted in a docstring).
- New `agent/dev_mode_prompt.py` module with `_TOOL_CALL_RE`, `_serialize_messages_to_dev_prompt`, `_parse_dev_response_to_sse_deltas`, `_strip_tool_calls`, `_sanitize` (all ported/adapted from `llmbox_lib.py:25,716-813,881-904`). Keep the preamble text in its own constant so tests can assert on it.
- `BedrockBackend.__init__` / `health` / `list_models` / `detect_ctx_size` / `complete`.
- `BedrockBackend.stream_chat` — serializes messages via `dev_mode_prompt`, calls `api.send_and_wait_conv`, runs the § 8.3 truncation-recovery loop (port of `llmbox_lib.py:637-674`), parses the resulting text, yields OpenAI-shape SSE deltas.
- Wire summary path and main path.
- Add both CLI flags `--backend-main {llamacpp,bedrock}` and `--backend-summary {llamacpp,bedrock}`.
- Daily cost counter + `BedrockBudgetExceeded` exception ([§ 6.5](#65--cost-model)).
- Telemetry log lines from [§ 15.75](#1575--observability--telemetry).
- New tests: `test_bedrock_backend.py`, `test_bedrock_dev_mode_roundtrip.py`, `test_bedrock_backend_tool_loop.py`, `test_backend_swap.py`.
- Live smoke script `scripts/bedrock_smoke.sh` from [§ 13.4](#134-live-smoke-test-phase-2-dod).

Decomposed into tasks 2.0–2.9 in [§ 20](#20--work-breakdown).

**DoD:**
- [ ] `backends.summary.kind = "bedrock"` in `config.json` routes summaries through Bedrock end-to-end (live smoke test against real gateway passes all 6 criteria in [§ 13.4](#134-live-smoke-test-phase-2-dod)).
- [ ] `backends.main.kind = "bedrock"` routes the main agent loop through Bedrock end-to-end; smoke scenarios 2 and 3 pass.
- [ ] `BEDROCK_API_URL` missing → factory error with actionable message.
- [ ] Cancel latency on both summary and main ≤ 5s (double-escape while a Bedrock poll is in flight stops within one poll interval).
- [ ] Truncation recovery: synthetic test where the first response ends mid-`<tool_call>` triggers a continuation request and the combined response parses successfully.
- [ ] `test_bedrock_backend.py`, `test_bedrock_dev_mode_roundtrip.py`, `test_bedrock_backend_tool_loop.py`, `test_backend_swap.py` all pass.
- [ ] Tokenizer-approximation info banner fires when bedrock is selected.
- [ ] Baseline diff with `summary=bedrock` shows only the banner and wall-clock drift; main-loop stdout unchanged.
- [ ] Baseline diff with `main=bedrock` shows identical tool-call sequence on a deterministic CICD input.
- [ ] **Docs:** README auth setup section + config examples for all four combinations + pointer to § 8 for dev-mode prompt stuffing; `plan/bedrock-integration.md` § 24 decision log filled with any Phase-2-time decisions; troubleshooting section at README bottom cross-referencing [§ 18.5 runbook](#185--operator-runbook).
- [ ] **Security:** all [§ 18.75 checklist items](#1875--security-review-checklist) verified by PR reviewer.
- [ ] **Telemetry:** all log lines from [§ 15.75](#1575--observability--telemetry) fire in smoke-test output and carry the documented keys.

### Phase 3 (optional) — Progressive per-token streaming for Bedrock

If the gateway exposes in-progress message text via `conv.messageMap[last_id]`, upgrade `BedrockBackend.stream_chat` to the Option B long-poll-with-chunking variant described in § 7.3. Emit `content` deltas incrementally as text arrives; emit `tool_calls` deltas as complete `<tool_call>…</tool_call>` blocks appear in the running buffer. Plan may end at Phase 2 — this phase is nice-to-have only.

## 17. Risks & mitigations

| # | Risk | Mitigation |
| - | --- | --- |
| K1 | Back-compat shim hides a case where the legacy `llm` block had a field we're not copying into `backends.main`. | Shim preserves the whole dict (`**config["llm"]`). Unit test asserts `_load_config({"llm": {"base_url": "x", "model": "y", "unknown_key": "z"}})` produces `backends.main = {"kind": "llamacpp", "base_url": "x", "model": "y", "unknown_key": "z"}`. |
| K2 | `LlamacppBackend` differs from the pre-refactor code in some subtle way (e.g. timeout values, retry behavior). | Baseline diff in Phase 1 DoD catches this. `_llm_request_raw` is line-for-line the old `_llm_request` with `base_url` substituted. |
| K3 | Bedrock polling + async summarizer thread interact badly (one waiting on a 180s `poll_timeout` blocks checkpoint save). | `AsyncSummarizer.drain(timeout=_config["backends"]["summary"]["max_wait_on_save"])` already caps the wait. Verify test covers the case where the bedrock poll is mid-flight when drain fires. |
| K4 | Gateway URL contains a trailing slash and `f"{api_url}/health"` double-slashes — behavior varies per API gateway config. | Trim trailing slash in `BedrockBackend.__init__`. Add a one-line test. |
| K5 | `BEDROCK_API_KEY` leaks into logs via `log.debug("config: %s", _config)`. | Redact `api_key` in any log/emit of the config dict (same pattern as secrets in the existing agent — grep `agent.py` for `api_key` after refactor to ensure no naked logs). See [§ 18.75](#1875--security-review-checklist). |
| K6 | Token-count approximation overshoot leads to premature context reduction, smaller effective context than user expected. | Overshoot is conservative — errs toward safety. The user-visible symptom is earlier summarization, not failed requests. Documented in § 10. |
| K7 | CICD loop sensitive to main-on-bedrock prose differences (the model narrates differently even if tool calls match). | CICD does not read stdout; only exit code + tool-committed side-effects. Verified in § 14. |
| K8 | `bedrock_api.py` drifts in llmbox-cli (e.g. gateway payload changes). Our agent copy goes stale. | Docstring records the llmbox-cli SHA the copy came from. Drift-detection script, quarterly reminder — see [§ 19](#19--cross-repo-contract). |
| K9 | Tests that mock `agent._llm_request` miss Bedrock-specific bugs. | Phase 2 new tests (`test_bedrock_backend.py`, `test_bedrock_backend_tool_loop.py`) explicitly mock at the `requests.Session` layer inside `BedrockChatAPI`, independent of `_llm_request`. |
| K10 | Model emits `<tool_call>` without a closing `</tool_call>` (truncation or format drift) → the response text parses to zero tool calls, agent "thinks" the model gave plain text when it meant to call a tool, loop progresses incorrectly. | Port llmbox's sanity check verbatim (`"<tool_call>" not in full_content or "</tool_call>" in full_content`, `llmbox_lib.py:641`) as the entry gate to the parser. On truncation, invoke the continuation loop (`MAX_CONTINUATIONS = 3` from `llmbox_lib.py:639`) with the same `conversationId` before parsing. Bounded retry budget — after 3 failures, return what we have and let the agent loop retry on the next turn. |
| K11 | Model emits malformed JSON inside the `<tool_call>` block (`{"tool": "foo", "args":` with no closing brace, or `"args": "this should be a dict"`). | `_parse_tool_calls` already tolerates — `json.JSONDecodeError` branch at `llmbox_lib.py:893-894` silently skips the entry. Log at DEBUG with the raw block so operators can diagnose; agent loop sees no tool calls and retries naturally. |
| K12 | Very long tool manuals consume prompt budget; with 50+ tools the prompt gets huge. | The CICD loop uses ~10 tools; current overhead is ~1.5-2k tokens. If the tool set grows, a follow-up plan adds tool-subset selection (filter `tools` by relevance before serialization). Out of scope here; tracked as a known limitation in README. |
| K13 | Dev-mode prompt injection makes the agent's system prompt harder to reason about — the "system" text balloons with tool docs, rules, and the one-shot example, all as freeform text. Audits of "what is the agent actually told?" become less obvious. | Keep the dev-mode preamble (tool-manual header, `RULES:` block, one-shot example) in a separate module `agent/dev_mode_prompt.py`, exposed as a constant (`DEV_MODE_PREAMBLE`) and a pure function (`build_dev_prompt(messages, tools) -> str`). Unit-test the shape (`test_bedrock_dev_mode_roundtrip.py` asserts the preamble is present and well-formed). Anyone auditing can read `dev_mode_prompt.py` in isolation without cross-referencing runtime state. |
| K14 | Unbounded spend: a misconfigured CICD loop or a runaway agent session could accumulate arbitrary Bedrock charges before anyone notices. | Daily in-process spend counter + hard cap ([§ 6.5](#65--cost-model)). Cap breach raises `BedrockBudgetExceeded`, which short-circuits the backend and falls back to llamacpp on the summary path. |

## 18. Deferred / won't-do

Explicitly rejected for this integration:
- **`boto3.client('bedrock-runtime')` direct.** We use the gateway client exclusively.
- **Per-request backend override.** Runtime switching is startup-only (D4).
- **Bedrock credentials from AWS profile / instance role.** Env vars only (D8).
- **Bedrock-specific tokenizer.** Gemma-3 fallback with approximation label (D7).
- **Progressive per-token streaming for Bedrock.** Single-chunk delivery (§ 7.3 Option A) ships in Phase 2; progressive chunking (Option B) is optional Phase 3.
- **Cost reporting dashboard.** § 6.5 adds a local daily counter and guardrail; aggregation across operators, dashboards, and per-tool cost breakdown are out of scope.
- **Tool-subset selection for dev-mode prompts (K12).** The current tool set is small enough that all-tools-always is fine; a selector becomes worthwhile only if the tool set doubles.

## 18.5 — Operator runbook

Numbered playbooks for common failure modes. Each is a sequence to follow, not a discussion.

### 18.5.1 Bedrock gateway 5xx burst (≥ 3 consecutive 5xx in a 10-minute window)

1. Check gateway status: `curl -H "x-api-key: $BEDROCK_API_KEY" "$BEDROCK_API_URL/health"`.
2. Grep recent agent log for `backend.retry.attempted`: `grep backend.retry.attempted ~/.cache/agent/*.log | tail -20`.
3. If gateway is down: set `backends.summary.kind: llamacpp` in `config.json`, restart agent. Main-on-bedrock operators switch to `--backend-main llamacpp`.
4. If gateway is up but our requests fail: check `BEDROCK_API_KEY` matches what the gateway expects (rotation? expiry? typo in env?). See 18.5.3.
5. If it persists > 30 minutes with no gateway-side cause: trigger [§ 23 abandonment](#23--abandonment-criteria) review.

### 18.5.2 Truncation retries exhausted (`bedrock.truncation_recovery.exhausted` log line)

1. Grep the session log for the `conversationId`: `grep <conv_id> ~/.cache/agent/*.log`.
2. Look at the raw response text (captured in DEBUG logs if enabled) — does it contain `<tool_call>` but not `</tool_call>`? How close to the end?
3. If the model is hitting `max_tokens`: raise `gen_params.max_tokens` in config and re-run.
4. If the model is emitting drift-formatted tool calls (`<toolcall>`, `<|tool_call|>`, etc.): open an issue; capture the raw text; check if the gateway's upstream model version changed.
5. Short-term mitigation: operator can set `backends.main.kind: llamacpp` to unblock while investigation proceeds.

### 18.5.3 API key rotation

1. Generate new key at the API Gateway console.
2. Update shell env: `export BEDROCK_API_KEY="<new>"`.
3. Restart agent (no hot-reload of env vars).
4. Run `scripts/bedrock_smoke.sh` to verify ([§ 13.4](#134-live-smoke-test-phase-2-dod)).
5. Revoke the old key at the console after smoke passes.
6. If `config.json` inlines the key (not recommended), also update there; permissions should be `0o600` per [§ 18.75](#1875--security-review-checklist).

### 18.5.4 Cost spike detection

Triggered by: daily spend in `CICD/bedrock_spend.json` exceeds 2× the 7-day trailing average.

1. `cat /droid/repos/agent/CICD/bedrock_spend.json | jq` — identify which days are spiking.
2. Grep log for `bedrock.cost.tick` on those days; find the highest per-call costs.
3. Identify the caller: CICD loop? Interactive session? Check `role=main` vs `role=summary`.
4. If CICD: check `progress.md` — is the loop retrying a failure? Kill the loop.
5. If interactive: contact the operator.
6. If the cap is too low: raise `backends.main.daily_cost_cap_usd` deliberately, not by accident.
7. If spend patterns show systemic drift (every day is 2× prior expectation), revisit model selection (Haiku vs Sonnet) — this is a decision-log entry, not a runbook item.

### 18.5.5 Rollback procedure

1. If Phase 2 is in production and a P0 issue surfaces: revert the Phase 2 PR (`git revert <sha>`, push, new release tag).
2. Operators with `backends.*.kind: bedrock` in their config: agent now runs the back-compat shim (§ 15 rollback plan); behavior matches today.
3. If Phase 1 itself has a regression: revert Phase 1 PR; same mechanism.
4. File a decision-log entry ([§ 24](#24--decision-log)) documenting the rollback rationale.
5. Evaluate against [§ 23 abandonment criteria](#23--abandonment-criteria) — is this a temporary rollback or a kill?

## 18.75 — Security review checklist

Every item must be verified by the PR reviewer before Phase 1 or Phase 2 merges, whichever is first to touch the relevant surface.

- [ ] **Config file permissions.** If `config.json` contains a non-empty `api_key`, the agent logs a WARN at startup if the file mode is wider than `0o600`. README instructs users to `chmod 600 config.json` in the Bedrock setup section.
- [ ] **Env var leak vectors.** No `log.debug("env: %s", os.environ)` anywhere. No code path passes the whole environment to a child process that then gets logged. Verify via `grep -rn "os.environ" agent/` after Phase 2.
- [ ] **Log scrubbing.** `BEDROCK_API_KEY` never appears in any `log.*` call. Specifically: `log.debug("config: %s", _config)` redacts `api_key` before emitting. Verify via a unit test that constructs a config with `api_key="SENTINEL_VALUE"` and asserts the sentinel does not appear in `caplog` output.
- [ ] **Error-message redaction.** `BedrockChatAPI` error paths (`requests.HTTPError`, timeouts, JSON decode errors) must not include the `x-api-key` header in the error message. Check `bedrock_api.py:50-55` and any exception formatter.
- [ ] **PR review gates.** The PR description explicitly calls out any log-line additions that include request/response bodies. Reviewer signs off on each.
- [ ] **Spend counter file permissions.** `CICD/bedrock_spend.json` mode `0o600`. Contains no secrets (usage data only), but restrict anyway — it reveals activity patterns.
- [ ] **No secrets in commit.** Pre-commit check: git hook or CI grep that refuses a commit containing `BEDROCK_API_KEY=` (as a literal value) or 32-char hex strings following `"api_key":`. Phase 1 adds this hook.

## 19. — Cross-repo contract

`bedrock_api.py` lives in `/droid/repos/llmbox-cli`. Our Phase 2 copies it into `/droid/repos/agent/`. The copy will drift. This section defines the contract and the drift-detection mechanism.

### What llmbox-cli guarantees (implicitly, since there's no formal contract today)

- `BedrockChatAPI.send_and_wait_conv(prompt, cancel_check=None, conversation_id=None) -> (full_text, conversation_id)` — signature stability.
- `BedrockChatAPI.health() -> tuple[bool, str]`.
- The gateway wire format: POST `/conversation`, GET `/conversation/{id}`, response-shape `content: [{contentType, body}]`.
- Env var names: `BEDROCK_API_URL`, `BEDROCK_API_KEY`.

### Pinned SHA

Phase 2's copy of `bedrock_api.py` carries a top-of-file docstring:

```python
"""
Ported from /droid/repos/llmbox-cli/bedrock_api.py @ SHA 1653b71
Last verified: 2026-04-22
See /droid/repos/agent/plan/bedrock-integration.md § 19 for drift protocol.
"""
```

(`1653b71` is the current llmbox-cli commit; Phase 2 confirms / updates.)

### Drift-detection script

Phase 2 ships `/droid/repos/agent/scripts/check_llmbox_drift.sh` (spec):

```bash
#!/usr/bin/env bash
# Diff the agent's copy of bedrock_api.py against llmbox-cli upstream.
# Exits 0 if identical (modulo the docstring block at top); non-zero on drift.
# Intended for quarterly reviewer cadence.
#
# Usage: check_llmbox_drift.sh
```

Implementation: strip the top docstring (lines matching `r'^".*?"""'s`), then `diff` agent/bedrock_api.py against `/droid/repos/llmbox-cli/bedrock_api.py`.

### Notification plan

No automation. Reviewer runs `check_llmbox_drift.sh` quarterly (calendar reminder; not in scope to automate). If drift detected:

1. Diff reveals changes.
2. If changes are additive / non-breaking: port them into the agent copy; bump the pinned SHA; add a decision-log entry.
3. If changes are breaking: open an issue; the agent copy stays pinned; update becomes its own plan.

### Dev-mode prompt functions in `llmbox_lib.py`

Same protocol applies to the ported dev-mode functions in `agent/dev_mode_prompt.py`. Same top-of-file docstring, same drift script (extended to cover both files).

## 20. — Work breakdown

Each task is scoped to ≤ 4 hours. Ordering is sequential within a phase unless noted. Reviewer column is a role, not a person.

### Phase 1

| # | Task | Scope | Files touched | Tests added | Reviewer |
| - | --- | --- | --- | --- | --- |
| 1.0 | Declare freeze window | Announce soft freeze on `agent.py:379-417, 749-993, 1443-1524, 1891-2052` for the Phase-1 PR review window; post in CICD progress / agent.md so concurrent cycles steer clear | `CICD/agent.md` (one-line note), PR description | n/a | maintainer |
| 1.1 | Baseline capture | Run `scripts/capture_baseline.sh`; implement `scripts/measure_latency.py` (spec in § 5.5); populate `baseline/METRICS.md` with B1–B8 values | `scripts/measure_latency.py`, `baseline/METRICS.md` | n/a | maintainer |
| 1.2 | Backend interface + factory | Create `agent/llm_backend.py` with `Backend` Protocol, `build_backend`, `LlamacppBackend` skeleton. No agent.py changes yet | `agent/llm_backend.py` | `tests/test_llm_backend_factory.py` | reviewer |
| 1.3 | Config registry + shim | `_load_config` grows the `backends` registry + back-compat synthesis from legacy `llm`/`summary` | `agent.py:187-254` | `tests/test_config_registry_shim.py` | reviewer |
| 1.4 | Wire llamacpp backend into `_llm_request` / `_summary_request` | Rename current `_llm_request` → `_llm_request_raw`; new `_llm_request` is a thin wrapper over `_main_backend.stream_chat`. Same for summary path | `agent.py:379-417, 749-776, 867-905` | extend existing `test_agent_llm_retries.py` (no rewrite) | reviewer |
| 1.5 | Wire health/ctx/list_models | `_check_api_health`, `_detect_ctx_size`, `_list_available_models` delegate to backend | `agent.py:1358-1414, 1443-1524, 1675, 1893` | one new test in `tests/test_backend_health.py` | reviewer |
| 1.6 | Telemetry log lines (Phase 1 subset) | Add `backend.complete.latency_ms` + `backend.stream_chat.latency_ms` lines | `agent/llm_backend.py` | assertion in `test_llm_backend_factory.py` that a `caplog` capture contains both keys | reviewer |
| 1.7 | Docs + baseline diff | Update README for new config shape + legacy compat; re-run baseline capture; confirm zero diff vs pre-refactor | `README.md`, re-committed `baseline/*.log` | n/a (diff is the test) | maintainer |

### Phase 2

| # | Task | Scope | Files touched | Tests added | Reviewer |
| - | --- | --- | --- | --- | --- |
| 2.0 | Copy + patch `bedrock_api.py` | Copy from llmbox-cli; apply D11 patch (remove env reads from `_DEFAULT_CONFIG`); add top-of-file docstring per § 19 | `agent/bedrock_api.py` | `tests/test_bedrock_backend.py` basic-health case | reviewer |
| 2.1 | Port dev-mode prompt module | Port `_TOOL_CALL_RE`, `_build_tool_system_prompt`, `_build_prompt`, `_parse_tool_calls`, `_strip_tool_calls`, `_sanitize`, `_handle_truncation` into `agent/dev_mode_prompt.py`. OpenAI-shape adapter functions on top | `agent/dev_mode_prompt.py` | `tests/test_bedrock_dev_mode_roundtrip.py` (all 6 cases from § 13.2) | reviewer |
| 2.2 | `BedrockBackend.complete` + `health` + `list_models` + `detect_ctx_size` | No tool calls, no streaming — just summary path | `agent/llm_backend.py` | `tests/test_bedrock_backend.py` complete / health cases | reviewer |
| 2.3 | `BedrockBackend.stream_chat` (Option A) | Serialize → `send_and_wait_conv` → parse → yield OpenAI deltas. Truncation-recovery loop from § 8.3 | `agent/llm_backend.py` | `tests/test_bedrock_backend_tool_loop.py` | reviewer |
| 2.4 | Cost counter + budget guardrail | Daily counter in `CICD/bedrock_spend.json`; `BedrockBudgetExceeded`; config keys; env override | `agent/llm_backend.py`, `agent.py` (error-path handling) | `tests/test_bedrock_cost_cap.py` | reviewer |
| 2.5 | CLI flags | `--backend-main`, `--backend-summary` | `agent.py` arg parser | extend `tests/test_agent_cli.py` | reviewer |
| 2.6 | Telemetry log lines (Phase 2) | All log lines from § 15.75 | `agent/llm_backend.py`, `agent/dev_mode_prompt.py` | caplog assertions in existing Phase 2 tests | reviewer |
| 2.7 | Backend-swap round-trip tests | Tests for summary-bedrock+main-llamacpp and the inverse | `tests/test_backend_swap.py` | — | reviewer |
| 2.8 | Live smoke script | `scripts/bedrock_smoke.sh` implementing the 6 criteria from § 13.4 | `scripts/bedrock_smoke.sh` | manual run during DoD | maintainer |
| 2.9 | Docs + security checklist + canary prep | README auth section; troubleshooting; § 18.75 checklist verified; Phase 2 decision-log entries added | `README.md`, `plan/bedrock-integration.md` § 24 | n/a | maintainer + security reviewer |

## 21. — Dependencies & ordering

Sequential unless called out. Tasks can parallelize where an arrow does not force order.

```
Phase 1:
  1.0 ──> 1.1 ──> 1.2 ──┬──> 1.3 ──> 1.4 ──> 1.5 ──> 1.7
                        └──> 1.6 (parallel with 1.4–1.5)

Phase 2 (1.7 is the Phase-1-done gate):
  1.7 ──> 2.0 ──> 2.1 ──┬──> 2.2 ──┬──> 2.3 ──> 2.4 ──> 2.5 ──┬──> 2.7 ──> 2.8 ──> 2.9
                        │          │                            │
                        │          └──> 2.6 (parallel with 2.3–2.5)
                        └──> 2.7 prerequisite only after 2.3 lands
```

Rationale for a few specific edges:
- **1.1 before 1.2:** baseline must be captured against the *pre-refactor* code, not against an in-progress Phase 1.
- **1.2 before 1.3:** the backend interface shape locks first; config just populates a dict, it doesn't need the registry design to exist, but 1.3's tests will want a real `build_backend` to import.
- **1.4 before 1.5:** health/ctx calls currently ride on the llama `/health` endpoint, so they only become backend-delegated once the backend exists in the request path.
- **2.1 before 2.3:** `stream_chat` imports the dev-mode module.
- **2.3 before 2.4:** the cost counter wraps each backend method; we need a working method to wrap.

## 22. — Sign-off checklist

Phase 1 may start only when ALL of the following are checked.

- [x] D1–D12 confirmed in [§ 24 decision log](#24--decision-log) (stamped 2026-04-22 by mikeblakemore).
- [ ] D6-a and D6-b parked in [§ 25 open-questions log](#25--open-questions-log); reviewer acknowledges they are non-blocking.
- [x] `git status` on `main` is clean; no in-flight PR touches `agent.py:379-417, 749-776, 867-993, 1443-1524, 1891-2052` — verified 2026-04-22 at HEAD `fc17f22`, zero open PRs.
- [ ] Freeze window announced in `CICD/agent.md` and `CICD/progress.md` so concurrent cycles steer clear — **deferred to Phase 1 kickoff** (announce when the PR opens; announcing earlier idles the self-CICD loop unnecessarily).
- [x] Both llama-server endpoints reachable (`curl -sS http://127.0.0.1:8080/health` / `:8082/health` return 200) — verified 2026-04-22.
- [ ] `capture_baseline.sh` runs cleanly on a scratch config; outputs under `baseline/` are identical to the committed versions — **deferred to task 1.1** (the script itself is being implemented there).
- [x] `scripts/measure_latency.py` spec in § 5.5 is clear enough that task 1.1 can implement it without further design questions — acknowledged 2026-04-22.
- [x] llmbox-cli SHA recorded (current: `1653b71`; re-verify before Phase 2).
- [x] Bedrock gateway reachable — verified 2026-04-22 end-to-end roundtrip on `xfg16lwlrd…` gateway (health True, send 0.78s, poll 6.46s, haiku reply received).
- [x] `BEDROCK_API_URL` + `BEDROCK_API_KEY` present in operator's shell — persisted in `~/.bashrc` 2026-04-22.
- [x] [§ 18.75 security checklist](#1875--security-review-checklist) items acknowledged 2026-04-22 by mikeblakemore. Reviewer assignment deferred to Phase 2 kickoff (Phase 1 has no secrets surface).
- [x] Success-metric targets in [§ 2.5](#25--success-metrics) confirmed — S5 caps: main=$10/day, summary=$1/day (stamped 2026-04-22 in § 24).
- [x] Abandonment criteria in [§ 23](#23--abandonment-criteria) acknowledged 2026-04-22 by mikeblakemore (4 kill-switches: cost cap 3-day breach, gateway availability <99% over 7 days, tool-parse failure >5% per 100 turns, cancel p99 >10s twice consecutive).
- [x] Work-breakdown tasks 1.0–1.7 accepted 2026-04-22 by mikeblakemore. Per-task reviewer assignment deferred to Phase 1 kickoff.

## 23. — Abandonment criteria

Kill-switches. If any one fires, stop work and hold a review before continuing.

- **Cost cap breached for 3 consecutive days** at the tightest sensible cap, without operator error explaining it. Signal: spend counter in `CICD/bedrock_spend.json` vs daily cap in config. Consequence: Bedrock pathway is economically unviable; revert to llamacpp-only, archive the plan.
- **Gateway availability < 99% measured over a rolling 7-day window** during G1 canary or G2 opt-in. Signal: `backend.retry.attempted` / `backend.complete.latency_ms ok=false` log lines. Consequence: the gateway is not production-ready; block GA; operator may continue llamacpp-only.
- **Tool-call parse failure rate > 5% of Bedrock main turns over a rolling 100-turn window** after truncation recovery is applied. Signal: `bedrock.tool_parse.result parse_errors>0` and `bedrock.truncation_recovery.exhausted` aggregated. Consequence: dev-mode prompt stuffing is not reliable enough for main-path; revert main-on-bedrock, keep summary-on-bedrock, document the decision.
- **Two consecutive cancel-latency p99 measurements > 10s** during G1/G2. Signal: `cancel.latency_ms`. Consequence: interactive UX is broken; revert, investigate the poll loop.

Each abandonment event creates a decision-log entry ([§ 24](#24--decision-log)) and a followup plan.

## 24. — Decision log

Living artifact. Pre-populated with D1–D12 at Proposed-default status. Column "Made by" is filled at confirmation time.

| Date | Decision | Made by | Rationale | Alternatives considered |
| --- | --- | --- | --- | --- |
| 2026-04-22 | D1: Single `Backend` class | mikeblakemore | Method set is small; two protocols doubles registry | Two narrow protocols `StreamingBackend`/`OneShotBackend` |
| 2026-04-22 | D2: OpenAI-style SSE deltas from `stream_chat` | mikeblakemore | Zero changes to existing loop; backend translates on Bedrock side | Richer normalised event types (`TextDelta`, `ToolCallDelta`, `Done`) |
| 2026-04-22 | D3: `backends.{main,summary}` registry config | mikeblakemore | Matches llmbox-cli's flat config; avoids leaking config paths to every call site | `llm.main` / `llm.summary` nested form |
| 2026-04-22 | D4: Startup-only backend selection | mikeblakemore | `/model` already has subtle async-summarizer interactions; avoid doubling thread-safety surface | Hot-swap per `/model` command |
| 2026-04-22 | D5: Default = both llamacpp at today's URLs | mikeblakemore | Rollback-safe; zero-config for existing users | Empty default + required explicit config |
| 2026-04-22 | D6: Dev-mode prompt stuffing for Bedrock tools | mikeblakemore | llmbox-cli already implements; gateway has no native tool support. Ship with K10–K13 accepted; iterate if G1/G2 metrics regress. | Extend gateway (out of scope); third backend via direct `boto3` |
| 2026-04-22 | D7: Gemma-3 tokenizer + approximation label for Bedrock | mikeblakemore | Over-reserves context (safe direction); no new dependency | Anthropic's tokenizer (adds dep); char-based approximation (less accurate) |
| 2026-04-22 | D8: Env-vars-only for Bedrock auth | mikeblakemore | Matches llmbox-cli convention; no credential discovery | AWS profile, instance role |
| 2026-04-22 | D9: `detect_ctx_size` returns None for bedrock; port `_MODEL_CONTEXT_CHARS` dict | mikeblakemore | Gateway doesn't expose ctx size; per-model defaults are good enough | Introspect via a gateway endpoint (doesn't exist) |
| 2026-04-22 | D10: Cancellation via existing `cancel_check=` plumbing | mikeblakemore | No code change in `bedrock_api.py`; just thread the callback | Modify `bedrock_api.py` (rejected — keeps port clean) |
| 2026-04-22 | D11: Env-read moves from `BedrockChatAPI.__init__` to agent factory | mikeblakemore | Avoids import-time side effects; factory is the right place | Keep env-read in the client |
| 2026-04-22 | D12: Test fakes mock `requests.Session` inside the client | mikeblakemore | Exercises URL/headers/polling for free | Mock `BedrockBackend` directly (misses bugs in the client) |
| 2026-04-22 | S5 daily cost caps: main=$10.00, summary=$1.00 | mikeblakemore | Main is Sonnet-expensive under CICD load ($10 gives headroom for a full day of cycles); summary is Haiku-cheap (~$0.001/turn leaves ~1000 turns/day at $1) | main=$5 (too tight for active CICD), summary=$0.50 (unnecessarily tight) |
| 2026-04-22 | OQ-4 closed: document `BEDROCK_DAILY_CAP_USD` env override in README | mikeblakemore | Operators need to know the escape hatch exists; hiding it invites workarounds | Keep internal-only |
| 2026-04-22 | OQ-5 closed: redact `api_key` at every `_config` log-emit site, not just the startup banner | mikeblakemore | Redacting at the emit site is one code change; per-caller redaction is N changes and error-prone | Banner-only redaction |
| 2026-04-22 | K10–K13 accepted for Phase 2 ship; iterate post-canary | mikeblakemore | Dev-mode prompt-stuffing risks (unterminated tags, malformed JSON, ~1.5-2k token overhead, auditability) are known. G1/G2 metrics S3/S4 (parse failures ≤2%, recovery ≥95%) will surface problems; revisit if they regress. | Defer main-on-bedrock to Phase 3 (rejected — loses the dev-mode port exercise) |
| 2026-04-22 | Summary model = `claude-v4.5-haiku` | mikeblakemore | Live roundtrip verified 2026-04-22 (health True, send 0.78s, poll 6.46s, trivial prompt returns in ~7s end-to-end). Haiku pricing (~$0.25 in / $1.25 out per 1M tokens) keeps § 6.5 summary cap realistic at $1/day | claude-v3.5-haiku (older, no clear benefit); claude-v4.5-sonnet (10× cost for summary-only work); claude-v3.7-sonnet (ditto) |
| 2026-04-22 | Bedrock gateway: confirmed working instance (URL/key in operator's shell env; NOT inlined in plan/config for security) | mikeblakemore | Verified end-to-end via `BedrockChatAPI` against `xfg16lwlrd…` gateway; prior `a62zp34ns0…` gateway was dead-ended (messages never processed). Operator's `~/.bashrc` now exports correct `BEDROCK_API_URL`/`BEDROCK_API_KEY` | Inline URL in plan (rejected — URL + key rotation would require plan edits); per-phase override only (rejected — env is the canonical source) |

Additional entries added as the project proceeds. Each entry is append-only; superseding a past decision creates a new row with a "supersedes row N" note in Rationale.

## 25. — Open questions log

Non-blocking questions parked for review. Revisit during code review or at stage gates.

| # | Question | Origin | Next review | Status |
| - | --- | --- | --- | --- |
| OQ-1 (D6-a) | Does the dev-mode serializer's positional assumption about `tool` message ordering hold for all replayed histories? | § 4 open questions | Phase 2 task 2.1 code review | open |
| OQ-2 (D6-b) | Should `DEV_MODE_PREAMBLE` merge with or replace the agent's existing system prompt? Proposed: merge | § 4 open questions | Phase 2 task 2.1 code review | open |
| OQ-3 | Exact per-call cost numbers for Claude Haiku vs Sonnet on the operator's gateway (fills in § 6.5 `<TBD>` cells) | Gap review | Phase 2 task 2.4 | open |
| OQ-4 | Should `BEDROCK_DAILY_CAP_USD` env override be documented in README or kept internal-only? | § 6.5 | Phase 2 DoD | **closed 2026-04-22** — document in README (see § 24) |
| OQ-5 | Is the log-redaction mechanism for `api_key` applied to all `_config` log sites, or just the startup banner? | § 18.75 | Phase 2 security review | **closed 2026-04-22** — all emit sites (see § 24) |
| OQ-6 | When the llmbox-cli pinned SHA drifts, does the agent block on updating or take the hit and plan a port? | § 19 | quarterly drift-check cadence | open |

New questions appended as they surface. Closing a question creates a decision-log entry ([§ 24](#24--decision-log)).
