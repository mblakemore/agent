# agent.py Tier 5 enhancements — design doc

**Status**: shipped (T5.13, T5.14, T5.15, T5.17 — T5.16 deferred)
**Author**: synthesis from c0rtana (C95→C114) + lyla (C23→C39) overnight audits, post-Tier-4
**Audience**: next agent.py contributor (human or CICD agent)

---

## Context: where we are

Tiers 1-4 shipped (commits `61ed9ec`, `cabfb0d`, `15e5c6a`, `1c940d3`) and have run live in c0rtana + lyla overnight with **zero false positives** and meaningful real-world fires (1 harmony rejection saved a re-poisoning event; 10+ bootstrap auto-creates eliminated lyla's "file not found" every-cycle error; 8 dedup hits caught real loops). Validation confirmed the patches are sound.

This doc proposes the next batch — items the second-pass audit surfaced that we deferred for design discussion. **Five patches**, ranked by impact, plus a deferred backlog.

---

## T5.13 — Async-summary gating by context utilization

**Audit citation** (c0rtana #b): `353 ASYNC SUMMARY fires across 2,069 tool calls (~17%)`; single session `session_20260514_125407.log` hit 7 summaries in one cycle while context budget was at ~25% utilization (11k/43k tokens).

**Failure mode**: the async summarizer's trigger is decoupled from actual context pressure. Summaries fire on message-count / wall-clock heuristics that don't correspond to "the context window is filling up." Each summary is an LLM call against the summary backend — measurable token spend.

**Proposed change**: replace the existing trigger logic in `_async_summarizer.kick()` with a context-utilization gate.

```python
# Before (sketch — actual code at agent.py:???):
def kick(...):
    if len(messages) - up_to >= _MIN_NEW_MESSAGES:  # or wall-clock
        spawn_summary_task(...)

# After:
def kick(messages, summary_state, ctx_size, ...):
    tokens_used = sum(_estimate_tokens(m) for m in messages) + summary_tokens(...)
    utilization = tokens_used / ctx_size
    new_since_summary = len(messages) - summary_state["up_to"]
    if (utilization > _SUMMARY_UTIL_THRESHOLD  # default 0.6
            and new_since_summary >= _MIN_NEW_FOR_SUMMARY):  # default 4
        spawn_summary_task(...)
```

**Knobs**:
- `_SUMMARY_UTIL_THRESHOLD` (default 0.6, env `AGENT_SUMMARY_UTIL_THRESHOLD`)
- `_MIN_NEW_FOR_SUMMARY` (default 4, env `AGENT_SUMMARY_MIN_NEW_MESSAGES`)

**Effort**: ~30 lines, contained to the async-summarizer kick path.

**Risk**: under-summarization could cause sudden context overflow when the threshold is crossed and a single big tool result pushes past. Mitigate by lowering the threshold (0.5) OR by emitting a "summary recommended" signal at 0.5 and the auto-trigger at 0.6.

**Test plan**: instrument a mock conversation with N small turns, verify summary doesn't fire until utilization crosses threshold. Add a regression test in `tests/test_async_summarizer.py` (or wherever the existing one lives — confirm with `grep -l async_summar tests/`).

---

## T5.14 — `file(action='edit')` adoption nudge

**Audit citation** (c0rtana #c): of 1,200+ writes in 19 c0rtana sessions, ZERO chose `file(action='edit')` — the model defaults to `exec_command` with `cat > f <<EOF` heredocs. C0rtana's own pattern `cN_112_001` records "accidentally purged multiple edges due to incorrect range replacement" on `state/associative_memory.json` — exactly the failure `file(action='edit')` would prevent.

**Failure mode**: a useful tool exists but isn't getting selected. The model's training prior favors shell heredocs for "write JSON file"; the agent doesn't see the surgical-edit option.

**Three design options** (pick one):

### Option A: post-hoc hint in exec_command result (LIGHTWEIGHT)

When `exec_command` returns successfully AND the command was a heredoc-style write to a JSON / state file (detected via `_extract_write_target` returning a tracked path), append a one-line nudge:

```
[suggestion: for surgical changes to this file, use file(action='edit',
 path=..., old_string=..., new_string=...) — safer than full-rewrite via
 heredoc and avoids the c0rtana C112-style data-loss pattern]
```

- Pros: zero risk, no behavior change, model can ignore
- Cons: noise on every state-file heredoc; soft signal that may not change behavior
- Effort: ~15 lines

### Option B: system-prompt directive (MEDIUM)

Add a one-paragraph note to the system-prompt header (next to the existing date injection from T1.4):

```
Tool-selection guidance: when modifying an EXISTING file, prefer
file(action='edit') over exec_command 'cat > f <<EOF' — surgical edits
are atomic, validated, and won't lose neighbouring content. Use heredoc
writes only when creating a NEW file or rewriting one in full.
```

- Pros: explicit, model sees every turn
- Cons: prompt budget cost (~50 tokens per request); may not stick across attention dilution in long contexts
- Effort: ~10 lines

### Option C: opt-in via runtime config + per-agent telemetry (HEAVIER)

Add `_HEREDOC_WRITE_ADVICE_ENABLED` config + a counter that surfaces "you wrote to <path> via heredoc N times this session; consider file(action='edit') for surgical changes" at session-end, plus a one-time inline hint on the first heredoc-to-tracked-state write per session.

- Pros: less noise per call, ends up educational
- Cons: more state to maintain
- Effort: ~50 lines

**Recommendation**: **Option A** as default + **Option B** for agents that opt in via config (`config.preferences.tool_selection_hints: true`). Layer A is risk-free; B is for agents whose AGENT.md doesn't already cover this.

**Test plan**: simulate a heredoc write to a tracked state file, verify the hint appears. Negative test: heredoc write to a non-tracked file → no hint.

---

## T5.15 — Auto-retry with stricter sampling on stall

**Audit citation** (T3.8 deferred): the stall guard detects but doesn't retry. The audit recommended "retry once with stricter sampling" but the streaming loop's structure made an in-place retry awkward.

**Failure mode**: when the model stalls (0 deltas after `AGENT_STALL_TIMEOUT_S`, default 60s), the natural turn-level retry kicks in with the SAME sampling params. If the stall was driven by token-search pathology (high temperature + low repeat penalty letting the model spiral on stop tokens), the next turn may stall the same way.

**Proposed change**: track a per-turn "stall retry budget" outside the streaming loop. On detect:

```python
# Outside the turn loop:
_stall_retries_remaining = int(os.environ.get("AGENT_STALL_RETRIES", "1"))

# When stall detected:
if _stall_retries_remaining > 0:
    _stall_retries_remaining -= 1
    # Adjust sampling for the next request body construction
    _stall_sampling_override = {
        "temperature": max(0.1, temperature - 0.3),
        "repeat_penalty": max(1.15, llama_gen.get("repeat_penalty", 1.1) + 0.05),
    }
    log.info("Next turn will use stricter sampling: %s", _stall_sampling_override)

# In request body construction:
if _stall_sampling_override:
    request_body.update(_stall_sampling_override)

# After successful (non-stalled) turn — clear override:
if _deltas_received > 0:
    _stall_sampling_override = None
```

**Risk**: stricter sampling on a healthy retry could degrade quality. Auto-clear on success limits the blast radius.

**Effort**: ~40 lines spread across the turn loop and request-body construction.

**Test plan**: mock a stalled response, verify the next request body carries `temperature` and `repeat_penalty` overrides; verify a successful response after stall clears the override.

---

## T5.16 — DC bootstrap-template completeness automation

**Audit citation** (Lyla, both passes): Cortana's AGENT.md template was adapted for Lyla via the "3-DC parallel meeting" at Elder C4914. The template stripped: `theme_tracking` schema (Elder's), `tools/` in layout, date-discipline reminder, anti-rediscovery directive. Bootstrap-template fixer (T3.9) auto-creates *missing placeholder files*, but doesn't fix *missing AGENT.md sections*.

Lyla independently re-invented `theme_tracking` at C24 after I left her a creator note pointing at Elder's schema — then dropped it at C26 via state-overwrite (T4.11 will catch that now). But the underlying problem remains: every new DC sibling inherits a stripped template and has to rediscover.

**Proposed change**: a `python3 agent.py --bootstrap-from-template <parent_dir>` CLI subcommand that ports a curated set of structural blocks from a parent DC into a target dir:

- `state/focus.json` skeleton with `theme_tracking` block + parent's `theme_categories` taxonomy
- `state/memories/patterns.jsonl` empty
- `AGENT.md` directives ported (date discipline, anti-rediscovery, Storage ≠ Retrieval, variety pivot)
- `messages/from-creator.md` empty
- `messages/to-creator.md` empty (because Lyla's audit showed she never wrote to it — placeholder + AGENT.md instruction = nudge)
- `.gitignore` carrying `state/CYCLE_END` etc.
- `.agent/preamble.json` minimal default

**Effort**: ~150 lines + a template manifest.

**Risk**: this is opinionated — locks in the DC-style convention. If a non-DC agent.py user runs the subcommand, it'd be misapplied. Mitigate via a `--dc-style` flag rather than auto.

**Out of scope for v1**: porting actual code from `tools/` (e.g., audit_memory.py). Those are agent-specific; only port if the user passes `--include-tools`.

**Test plan**: run against a tempdir, verify the expected files appear with expected schemas; verify the existing-agent path is not clobbered.

**Alternative**: skip the CLI entirely and just write `docs/dc-agent-template.md` describing the canonical set of files and schemas. Future DC births reference the doc instead of cloning. Cheaper, less automation, but functions.

---

## T5.17 — Per-session patch-effectiveness telemetry report

**Motivation**: c0rtana's audit reported `dedup: 0 hits across 2069 calls; stall guard: 0 fires; think-laundering: 0 fires`. Useful but only known because I asked. The operator running an agent doesn't know what's catching unless they grep the log.

**Proposed change**: at cycle-end (just before commit), emit a one-line summary to the log:

```
[patch-telemetry] dedup: 8 hits saved ~3200 tokens | write-loop: 4 trips
 (1 FP filtered) | harmony: 0 | stall: 0 | think-laundering: 0 |
 bootstrap-fixer: 1 file auto-created
```

Backed by a per-session counter dict in `run_agent_single`:

```python
_patch_telemetry = {
    "dedup_hits": 0,
    "dedup_tokens_saved_est": 0,
    "write_loop_trips": 0,
    "harmony_rejections": 0,
    "stall_aborts": 0,
    "think_launder_rejections": 0,
    "indent_guard_rejections": 0,
    "schema_warnings": 0,
    "bootstrap_files_created": 0,
}
```

Each patch's existing telemetry call (`telemetry.record_tool_call(":deduped")` etc.) gets a corresponding increment. At cycle-end (or via a `--report-telemetry` flag), log the dict.

**Why it matters**: lets us answer "is this patch worth keeping?" empirically. Future tiers (T6+) can include "removed because telemetry showed it never fired in 6 months" PRs.

**Effort**: ~50 lines + telemetry plumbing.

**Risk**: minimal. Read-only counters.

**Test plan**: trigger each patch in a unit test and verify the corresponding counter increments.

---

## Deferred backlog (not in Tier 5 but worth keeping list)

| Idea | Source | Sketch |
|------|--------|--------|
| Anti-rediscovery: auto-query patterns.jsonl in PERCEIVE | Lyla audit | Inject most-relevant pattern in preamble bundle |
| Per-repo error-fingerprint store | Lyla #C | Capture Traceback shape, surface "you've hit this before" on recurrence |
| Epigraph repetition detector | c0rtana audit | "What I know: I am here. I think..." 10+ times = flag |
| Non-JSON schema warning | T4.11 extension | YAML/TOML support; markdown frontmatter |
| `$(date -Iseconds)` literal-string leak | c0rtana focus.json | Detect un-substituted shell expansions in JSON values written by the model |
| Self-clearing `messages/to-creator.md` after creator reads | Lyla outbox empty | Coordination mechanism for two-way comms |
| Probe-deferral metric per cycle | Both audits | Show "your last 5 cycles spent 60% PROBE / 20% ACT / 20% CONSOLIDATE" |
| Sub-agent / fork pattern surface | not yet audited | If c0rtana / lyla start using `subagent` tool, audit how |
| Adaptive `n_samples` selection | T2.7 extension | Auto-pick n_samples=3 for borderline reasoning (confidence-aware) |
| LLM observer fallback (à la CT) | CT cycle-end-detection learnings | Could agent.py also benefit from an external "is this stuck?" classifier? |

---

## Cross-cutting concerns

### Test coverage

Tier 1-4 added ~30 new test cases across `test_file*.py` and synthetic harness tests in agent.py. Tier 5 should follow the same pattern: a `tests/test_t5_*` family of small, focused tests, one file per patch. Aim for 100% coverage of the new code paths.

### Compatibility

- T5.13 changes summarization cadence — could surprise heavy-summary-dependent agents. Stage with env-flag opt-out for one cycle of observation.
- T5.14 Option A is purely additive. Option B requires AGENT.md awareness for agents that don't want the directive.
- T5.15 is opt-in via `AGENT_STALL_RETRIES` (default 1, set 0 to disable).
- T5.16 is a new CLI subcommand — no impact unless invoked.
- T5.17 is read-only telemetry.

### Effort estimate

| Patch | Lines | Risk | Days (estimate) |
|-------|-------|------|---------|
| T5.13 | ~30 | Low | 0.5 |
| T5.14 (Option A) | ~15 | Very low | 0.25 |
| T5.14 (Option B) | ~10 | Low | 0.25 |
| T5.15 | ~40 | Medium | 1.0 |
| T5.16 (CLI) | ~150 | Medium | 1.5 |
| T5.16 (doc-only alt) | n/a | None | 0.25 |
| T5.17 | ~50 | Very low | 0.5 |

Total if shipping all: **~3 days** including tests + commit messages.
Minimal viable Tier 5 (T5.13 + T5.14A + T5.17): **~1.25 days**.

---

## Open questions for the operator

1. **T5.13**: is 0.6 the right threshold? Should it be backend-aware (Bedrock has bigger context, can run hotter; llamacpp local more conservative)? stick to .6 for now and we can calibrate as needed.
2. **T5.14**: A vs B vs C — which fits the agents' AGENT.md style best? Agents with thin AGENT.md probably prefer B; agents with rich AGENT.md probably prefer A. Both
3. **T5.15**: should stall retry escalate further on second stall (deeper sampling adjustment) or give up? yeah let it try to get through a couple times, perhaps it would be best to add more debug logging to correct in a future issue
4. **T5.16**: CLI subcommand vs doc-only? Operator's choice — depends on how many new DC siblings are expected. - skip this for now
5. **T5.17**: per-cycle log line OR a separate `state/telemetry/cycle-NNN.json` for structured analysis? Both? Ideally telemetry data should be going to my telemetry server and we should be using that data for diagnostics

---

## Decision log

- **T5.13**: shipped with default `AGENT_SUMMARY_UTIL_THRESHOLD=0.6`; min messages still required (`_SUMMARY_THRESHOLD`). Telemetry split into `summary_fired` (above threshold) vs `summary_gated` (would have fired pre-T5.13, skipped under threshold) so the empirical effect is measurable on the OTEL server.
- **T5.14**: shipped BOTH Option A and Option B. A (heredoc-to-state-JSON hint) is always-on, gated on `"<<" in cmd` + `.json` suffix, emitted ONCE per session per file via `run_agent_single._edit_nudges_emitted` to avoid spam. B (system-prompt directive) is gated on `config.preferences.tool_selection_hints: true`, off by default.
- **T5.15**: shipped with default `AGENT_STALL_RETRIES=2`. Escalation levels: 1 = -0.3 temp / +0.05 repeat_penalty; 2+ = additional -0.2 / +0.05. Override clears + budget restores when a turn produces deltas. Structured `log.warning` lines at each adjustment + on budget exhaustion.
- **T5.16**: SKIPPED. No new DCs imminent; doc-only alternative kept open for future.
- **T5.17**: shipped via new `telemetry.record_patch_event(name, kind, value)` primitive routing to OTEL counter `agentpy_patch_events`. Verbose-mode cycle-end log line emits `[patch-telemetry] dedup=N | write_loop=M | ...` only when at least one counter is non-zero (no fires = no log noise). Per-session counter dict (`_patch_telemetry`) in `run_agent_single` mirrors the OTEL data for local visibility.

