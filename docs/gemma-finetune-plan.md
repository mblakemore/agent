# Gemma 4 31B tool-friction reduction — plan

**Status**: Phase 1 complete — 121-example dataset assembled, ready for Phase 2 training
**Target model**: `unsloth/gemma-4-31B-it` (the live GPU model at port 8080)
**Goal**: reduce tool friction across the patterns the agent.py framework already detects — `action='edit'` underuse, Harmony-token leakage, schema drops, shell-meta in writes, etc. — by training a LoRA on the same model that's emitting the friction.
**Method**: SFT LoRA (Approach A). GRPO (Approach B) deferred until Phase 3 telemetry; only invoked if SFT plateaus below target.
**Dataset sources**: c0rtana, lyla, agent.py CICD builder, agent.py CICD reviewer. Combined into a single LoRA. **Not ebay-bot** — ebay-bot runs on Claude Code, not agent.py, and its tool grammar is different.
**Budget**: Colab Pro $10/mo, escalating to Pro+ $50/mo only if A100 unavailability blocks Phase 2.

---

## What changed since the previous draft

Earlier draft targeted Gemma 4 E4B because I didn't know the live GPU model was Gemma 4 31B. Two big consequences:

1. **The training dataset is the GPU model's own logs.** Every friction event in c0rtana/lyla/CICD-agents session logs was emitted by the same weights we'd be fine-tuning. Mined examples carry the exact failure modes we want to suppress, in the exact prompt format they appeared in. Highest possible signal-to-noise.
2. **The mid-step CPU summarizer (E4B) is unrelated and stays unchanged.** Per the prior architecture discussion, tool calling is the work — we don't want to shift it to a smaller model. Train where the intelligence already lives.

---

## Friction inventory (the menu of things to fix)

These are the patterns the framework currently *detects* but the model still *emits*. **Audited 2026-05-15** by running `tools/mine_friction.py` across 224 log files from lyla, c0rtana, and CICD agents, yielding 732 raw friction events.

| ID | Pattern | Detector | Mined count | Severity |
|----|---------|----------|-------------|----------|
| T5.18 | `file(action='write')` on existing file with high line similarity | T5.18 `edit_nudge{kind=similar_rewrite}` | **19 events** | High — root cause of LYLA-C26-class schema drops |
| H.01 | `exec_command` heredoc write (`cat <<EOF > file`) instead of `file()` tool | T5.14 `edit_nudge{kind=fired}` | **35 events** | Medium — bypasses all file guards, fragile |
| D.01 | Same file read 2+ times within 10-turn window (redundant read) | T1.2 dedup | **18 events** | Low-medium — burns context tokens, wastes turns |
| T4.11 | `file(action='write')` on JSON file drops top-level keys | T4.11 schema warning | **2 events** | High when it fires — C26 regression was 13 wasted cycles |
| J.01 | `file(action='write')` on `.jsonl` append-only file (should be append) | (new — detected in mining) | **2 events** | Medium — can silently destroy log history |
| T1.1 | Harmony control tokens in tool args | T1.1 `harmony_reject` | ~1/session | High when it happens — state poison |
| T4.10 | Slice-write indent mismatch on `.py` | T4.10 indent guard | 0 post-patch | Medium — patch already saves us |
| D.02 | Bootstrap-template miss (model recreates files that exist) | T2.X bootstrap check | 10+ cycles | Medium — wastes cycle |

**Top priority by mined volume + severity**:
1. H.01 (35 events) — heredoc writes; training directly suppresses this
2. T5.18 (19 events) — write vs edit; primary target from inception
3. D.01 (18 events) — redundant reads; cheapest to train away
4. T4.11 + J.01 (sparse but severe) — schema loss and append-vs-write

Note: H.01 being the most common surprised us — CICD builder logs use heredoc heavily. Training to prefer `file()` directly fixes this more robustly than the current command-sanitizer patch.

---

## Three approaches

### Approach A: SFT LoRA (supervised fine-tuning)

**Mechanism**: synthesize ideal (prompt → assistant turn) pairs from session logs. Train standard SFT with `train_on_responses_only`. Loss only on the assistant turn.

**Dataset construction**: for every detected friction event in the logs, generate the corrected counterfactual:

| Friction observed | Counterfactual target |
|-------------------|-----------------------|
| `file(action='write')` rewriting existing file with high similarity | `file(action='edit', old_string=<diff_old>, new_string=<diff_new>)` derived from the actual diff |
| `cat > f.json <<EOF` heredoc to existing file | `file(action='edit', ...)` for the changed lines |
| JSON write dropping top-level key K | Same write but with K preserved (or `action='edit'` of only the changed field) |
| Repeat write loop on file F | Single consolidated edit |

**Pros**:
- Predictable training dynamics
- Unsloth quickstart works directly (clone the 31B Kaggle notebook)
- Loss curve is interpretable
- No reward shaping debugging

**Cons**:
- Diff-to-edit-pair synthesis is fragile for multi-region changes (mitigate: filter dataset to ≤ 5 contiguous regions, each ≤ 10 lines)
- Need to manually generate counterfactuals for each friction type
- Trains the model to produce *one specific* good output per prompt, not to *prefer* good outputs over bad

**Cost**: ~3-5 hours on A100 for 31B QLoRA, ~$5-15 Colab Pro

### Approach B: GRPO RL LoRA (reinforcement training)

**Mechanism**: define a reward function that mirrors the framework's existing detectors. Run GRPO (per Unsloth's E2B Sudoku notebook pattern, scaled to 31B). No labeled examples needed — the reward signals what to do.

**Reward function** — composed of existing detectors with signed weights:

```python
def tool_call_reward(tool_call, file_state_before, file_state_after, tool_result):
    r = 0.0
    # Hard rejections (large negative)
    if has_harmony_token(tool_call):                r -= 5.0  # T1.1
    if tool_result.startswith("Error"):             r -= 2.0  # tool dispatch failure
    if not valid_json_schema(tool_call):            r -= 3.0  # tool grammar
    # Soft penalties (T5.18 detection logic, computed post-write)
    if action_write_high_similarity(tool_call,
                                     file_state_before,
                                     file_state_after):
                                                     r -= 1.0
    if schema_drop_detected(tool_call,
                            file_state_before,
                            file_state_after):
                                                     r -= 1.5  # T4.11
    if write_loop_repeat(tool_call.path):           r -= 0.5
    # Positive signals
    if tool_call.action == "edit" and validates():  r += 1.0  # the target behavior
    if tool_result_is_success(tool_result):         r += 0.3
    if tool_call.action == "write" and file_did_not_exist(tool_call.path):
        r += 0.5  # legitimate new-file write — preserve this
    return r
```

**Pros**:
- The framework already computes every term in this function — minimal new code
- No counterfactual synthesis — the model explores, the detector scores
- Trains the model to *prefer* good over bad across the distribution of inputs (rather than imitate one specific good output)
- Can run online: collect rollouts from live cycles, score with the detector, replay through GRPO trainer. No separate dataset assembly.

**Cons**:
- Slower than SFT — RL needs many rollouts to converge
- Reward shaping is fragile; one wrong weight and behavior drifts in an unintended direction
- 31B GRPO is non-trivial; Unsloth's GRPO support is best-documented at E2B/4B. Per Unsloth: GRPO works for 31B with `fast_inference=False`. Will be slow.
- Harder to debug: no clear "loss should go down" signal — reward curve can plateau for many reasons

**Cost**: ~10-20 hours on A100 for meaningful convergence, ~$25-60 Colab Pro

### Approach C: SFT → GRPO hybrid

**Mechanism**: cold-start with SFT on a smaller dataset (~500 examples) to seed the right tool-grammar prior. Then GRPO on top to refine across the full friction distribution.

**Pros**:
- SFT gets the model into the right neighborhood quickly
- GRPO does the polishing where SFT runs out of explicit labels
- Standard pattern in RLHF literature; well-understood failure modes

**Cons**:
- Twice the engineering surface
- Diminishing returns if SFT alone gets us to acceptable behavior

**Cost**: ~5-8 hours total, ~$15-30

### Recommendation

**Start with Approach A (SFT LoRA)**. The friction we want to fix has clear counterfactuals (we know what the model *should* have emitted in every detected case). SFT is the lowest-risk path to a deployable LoRA in one Colab session. Hold Approach B in reserve for a Phase 3 refinement if SFT plateaus below target adoption.

If SFT hits 75%+ adoption on offline eval, ship it and don't bother with RL — it's a clear capability gain, more isn't free.

---

## Model + recipe

**Model**: `unsloth/gemma-4-31B-it` (same family as the production `unsloth/gemma-4-31B-it-GGUF:UD-Q4_K_XL` you serve via llama-server). Per Unsloth docs: 31B QLoRA needs 22GB VRAM → A100 territory, not free T4.

**Starting recipe**: the official [Kaggle 31B notebook](https://www.kaggle.com/code/danielhanchen/gemma4-31b-unsloth) is the canonical reference. Adapt for Colab Pro A100 or use Kaggle's free T4×2 if that recipe is sharded (verify).

**Chat template**: `gemma-4-thinking`. Per Unsloth tip: *"Use the thinking one for the larger 26B and 31B ones"*. Live llama-server is invoked with `--reasoning off` so we'll train and infer with thinking disabled via `enable_thinking=False`. Same flag both sides — chat template still applies.

**Sampling at inference**: temp=1.0, top_p=0.95, top_k=64 — same as your live llama-server config. Already aligned.

**Bug-fix flags** (universal Gemma 4 bugs Unsloth has patched in their pip package):
- Gradient accumulation loss inflation — fixed; verify loss stays 1-3 for text
- `use_cache=False` corrupting attention on KV-shared layers — N/A for 31B (`num_kv_shared_layers=0`)
- IndexError on 31B / 26B-A4B during inference — Unsloth has the fix; pin a recent version
- Audio fp16 overflow — N/A

---

## Dataset (Approach A)

### Source

Session logs from the four agent.py agents:
- `/droid/repos/c0rtana/.agent/history/session_*.log` (~130 cycles)
- `/droid/repos/lyla/.agent/history/session_*.log` (~57 cycles)
- `/droid/repos/agent/.agent/history/session_*.log` for the CICD builder runs
- CICD reviewer logs (same location or under `/droid/repos/agent/logs/cicd-*.log`; mining script needs to handle both)

Combined: 200+ cycles, thousands of tool calls. **Excluded**: ebay-bot (Claude Code agent — different tool grammar, would poison the dataset).

### Mining script

`tools/mine_friction.py` — completed 2026-05-15. Handles two log formats:
- Baseline/lyla/c0rtana: `HH:MM:SS DEBUG TOOL CALL: func({...}) [id=...]`
- CICD builder: `  -> func(args...)` / `    Result: ...`

Detects all five friction categories (T5.18, T4.11, H.01, D.01, J.01), deduplicates by (category, file_path, ideal_call), and emits Unsloth ShareGPT format.

```bash
python3 tools/mine_friction.py \
  --logs /droid/repos/lyla/.agent/history \
         /droid/repos/c0rtana/.agent/history \
         /droid/repos/agent/logs \
  --out /droid/repos/beewatcher/agent-friction-v1/phase1_examples_raw.jsonl \
  --limit 200
```

Across 224 log files: 732 raw events → 76 deduplicated real examples.

### Current dataset (`phase1_examples.jsonl`) — 121 examples

| Source | Count | Categories |
|--------|-------|------------|
| Phase 0 hand-crafted | 30 | edit-adoption, schema-preservation, Harmony-clean, new-file-write, cycle-trace |
| Phase 1 mined (lyla + c0rtana + CICD) | 76 | T5.18×19, H.01×35, D.01×18, T4.11×2, J.01×2 |
| Synthetic supplement | 15 | T4.11×6, J.01×4, T5.18-concrete×5 |
| **Total** | **121** | |

File: `/droid/repos/beewatcher/agent-friction-v1/phase1_examples.jsonl`

### Scale calibration — empirical vs. literature

Phase 0 empirical finding (2026-05-14): **30 examples × 60 steps → real generalizing behavior change** on 31B, not memorization. Phase 1 (121 examples) should produce strong results. Literature-scale estimates (2500 examples) dramatically overshoot for narrow tool-grammar targets.

**Revised size targets** (down from original estimates):
- **Phase 1 (current)**: 121 examples. Expected: strong edit-adoption and schema behavior. Train 1 epoch, r=32.
- **Phase 2 (if needed)**: 300-500 examples after mining more cycles. Only if Phase 1 offline eval misses thresholds.
- **Phase 3 (GRPO, optional)**: per Phase 4 plan — only if SFT plateaus below 75% adoption.

### Format

Gemma 4 ShareGPT style — `{"conversations": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}`. Use `train_on_responses_only(instruction_part="<|turn>user\n", response_part="<|turn>model\n")` so loss is on assistant turns only.

---

## Training config (Approach A)

Deviations from Unsloth 31B defaults:

| Knob | Unsloth default | Our value | Why |
|------|-----------------|-----------|-----|
| `r` | 8 | **32** | 31B has more capacity to override; rank-8 LoRA may be too thin for cross-friction generalization |
| `lora_alpha` | 8 | **32** | Match r |
| `lora_dropout` | 0 | 0.05 | Modest regularization at this rank |
| `learning_rate` | 2e-4 | **1e-4** | Conservative — tool-grammar errors are hard to recover from |
| `num_train_epochs` | — (60 max_steps) | **2** | Real training run, not quickstart |
| `per_device_train_batch_size` | 1 | 1 | A100 fit |
| `gradient_accumulation_steps` | 4 | 8 | Effective batch size 8 — Unsloth's gradient-accumulation fix is on |
| `max_seq_length` | 8192 | **4096** | Cycle traces fit; halves activation memory |
| `optim` | adamw_8bit | adamw_8bit | Standard |
| `weight_decay` | 0.001 | 0.001 | Standard |
| `lr_scheduler_type` | linear | **cosine** | Smoother for multi-epoch |
| `warmup_ratio` | (steps=5) | 0.05 | Slightly higher for larger model |
| `finetune_vision_layers` | False | False | Text-only |
| `finetune_language_layers` | True | True | Standard |
| `finetune_attention_modules` | True | True | Tool selection lives here |
| `finetune_mlp_modules` | True | True | Standard |
| `load_in_4bit` | True (QLoRA) | True | 22GB VRAM target |
| `full_finetuning` | False | False | LoRA only |

**Loss expectation per Unsloth**: 31B should sit at 1-3 for text. If we see 13-15, something's wrong (likely a chat-template mismatch).

---

## Evaluation

### Pre-train baseline

Run base `unsloth/gemma-4-31B-it` (untuned) on a 50-example held-out set drawn from real cycle scenarios. Measure:

1. **Edit adoption rate** — fraction of "modify existing file" scenarios where the model emits `action='edit'`. Expected baseline: ~0% (matches production observation).
2. **Tool-call validity rate** — fraction of emitted calls that parse and pass schema validation. Expected baseline: ~95% (current production rate).
3. **Schema-preservation rate** — fraction of state-file rewrites that preserve all top-level keys. Expected baseline: ~85% (T4.11 fires occasionally).
4. **Harmony-token clean rate** — fraction of tool calls free of `<|...|>` artifacts. Expected baseline: ~99% (rare but happens).
5. **Cognitive-loop adherence** — does the model produce a recognizable 6-phase structure when given the framework prompt. Expected baseline: high (it's what it does in production today).

### Post-train pass thresholds

| Metric | Baseline | Phase 2 target | Phase 3 target |
|--------|----------|----------------|----------------|
| Edit adoption | ~0% | ≥ 35% | ≥ 75% |
| Tool-call validity | ~95% | ≥ 95% | ≥ 98% |
| Schema preservation | ~85% | ≥ 95% | ≥ 99% |
| Harmony clean | ~99% | ≥ 99.5% | ≥ 99.9% |
| Cognitive-loop adherence | ~95% | ≥ 90% | ≥ 95% |

**Failure mode**: cognitive-loop adherence dropping below 90% means we've overfitted to tool-call specifics at the cost of general agent capability. Mitigate by raising the "other tool calls" + "whole-cycle traces" share in the mix.

### Live A/B (only after offline eval passes)

Stand up the fine-tuned 31B in a second llama-server instance on port 8083. Add an `AGENT_LLM_BACKEND=gemma-31b-tuned` env var that routes to it. Run c0rtana with tuned, lyla with default, for 24h. Compare:

- `agentpy_patch_events{name=edit_nudge, kind=similar_rewrite}` count — expect drop on tuned
- `agentpy_patch_events{name=edit_nudge, kind=fired}` (heredoc) count — expect drop
- T4.11 schema-warning count — expect drop
- Cycle completion rate — should hold steady (no regression)
- Sentiment of `messages/to-creator.md` — qualitative check that the agent isn't confused

T5.18 telemetry IS the eval. We get the comparison for free.

---

## Integration

### Export

```python
model.save_pretrained_gguf(
    "outputs/gemma-4-31B-it-agent-tools",
    tokenizer,
    quantization_method = "q4_k_m",  # Match current UD-Q4_K_XL ballpark
)
```

**Gotcha from Unsloth docs**: *"wrong chat template / EOS token at inference time"* is the top GGUF deployment failure mode. Train and serve with the same `gemma-4-thinking` template + `--reasoning off` flag.

### Serving

Spin up alongside the existing llama-server:

```bash
/droid/repos/llama.cpp/build/bin/llama-server \
    -m /path/to/gemma-4-31B-it-agent-tools.gguf \
    --port 8083 \
    --parallel 1 --cache-reuse 256 --reasoning off --flash-attn on \
    --cache-type-k q4_0 --cache-type-v q4_0 \
    --temp 1.0 --top-p 0.95 --top-k 64 --host 0.0.0.0
```

### agent.py wiring

Add a backend entry to `_DEFAULT_CONFIG["llm"]["backends"]`:

```python
"gemma-31b-tuned": {
    "base_url": "http://localhost:8083/v1",
    "model": "gemma-4-31B-it-agent-tools",
    "api_key": "not-used",
},
```

Switch via `AGENT_LLM_BACKEND=gemma-31b-tuned`. Default unchanged. Rollback is `unset`.

---

## Risks

### High

1. **Diff → edit synthesis fragility.** Multi-region changes don't always cleanly express as a small set of `action='edit'` calls. Mitigation: filter aggressively (≤5 regions, ≤10 lines each). Accept lower yield in exchange for cleaner labels.
2. **Capability collapse on non-edit tasks.** Heavy training on one pattern can degrade general agent fluency. Mitigation: 50/20/25/5 mix + offline eval includes cognitive-loop adherence metric.
3. **GGUF inference drift from training behavior.** Q4_K_M quant loss could mask the LoRA gains. Mitigation: Phase 1 includes a "GGUF round-trip" check — run 5 prompts through both BF16 LoRA and Q4_K_M GGUF, verify tool-call patterns match.

### Medium

4. **Synthesized counterfactuals encode my assumptions, not the model's training prior.** If my edit-synthesis script produces `old_string` choices the model never would have, the LoRA learns a distribution shift the base model can't ride. Mitigation: spot-check 20 random synthesized examples by hand before bulk training.
5. **A100 availability on Colab Pro.** Pro tier doesn't guarantee A100 access. Mitigation: budget for Pro+ ($50/mo) as fallback; or use Kaggle's free T4×2 with the official 31B notebook (slower but free).
6. **Training-time chat template mismatch.** `gemma-4-thinking` vs `gemma-4` confusion is real — Unsloth tip says use thinking for 31B. Verify with `tokenizer.apply_chat_template` round-trip on a sample before training.

### Low

7. **Tokenizer differences** between Q4_K_XL (current production GGUF) and the BF16 base model — possible but unlikely. Spot-check.
8. **Colab session timeout** — Pro is 24h. Phase 3 (2500 examples, 2 epochs, 31B QLoRA) estimated ~6-8 hours. Should fit comfortably.

---

## Phased plan

### ✅ Phase 0 — pipeline validation (complete 2026-05-14)

- Adapted Unsloth 31B Kaggle notebook for Colab A100
- 30 hand-crafted examples × 60 steps, QLoRA r=16
- **Result**: 4/4 held-out probes passed including Python generalization, new-file discrimination, single-field surgical edit
- Empirical finding: 30 examples was enough for real generalizing behavior change — not memorization
- Model: `mblakemore/gemma-4-31B-agent-friction-phase0` on HuggingFace
- Served as Q4_K_M on port 8083; curl confirmed `action='edit'` through full production stack

### ✅ Phase 1 — mining script + dataset assembly (complete 2026-05-15)

- Wrote `tools/mine_friction.py` — handles both log formats, detects 5 friction categories
- Friction audit: 224 log files, 732 raw events; top find: H.01 (heredoc writes) is the most common pattern at 35 events
- Assembled 121-example dataset covering 7 friction categories including 3 newly characterized (H.01, D.01, J.01)
- Synthetic supplement added for sparse T4.11 and J.01 categories
- Dataset at `/droid/repos/beewatcher/agent-friction-v1/phase1_examples.jsonl`

### 🔜 Phase 2 — production training (next Colab session, ~3-4h)

Train on `phase1_examples.jsonl` with production knobs:
- r=32, lora_alpha=32, lora_dropout=0 (fast Unsloth kernel path)
- 1 epoch, lr=1e-4, cosine LR scheduler
- Load `phase1_examples.jsonl` (121 examples — enough per Phase 0 empirical finding)
- Run offline eval on 20 held-out scenarios (one per friction category × multiple rephrasings)
- Pass threshold: edit adoption ≥35% AND schema preservation ≥95% AND no regression on cycle-trace adherence

### Phase 3 — live A/B (24-48h wall clock)

- Stand up port-8083 llama-server with Phase 2 GGUF
- Wire `AGENT_LLM_BACKEND` env var
- Run c0rtana on tuned, lyla on default for 24h
- Compare: `edit_nudge`, `schema_warning`, `harmony_reject` telemetry counts
- T5.18 and T4.11 telemetry IS the eval metric — comparison is free

### Phase 4 (optional) — GRPO refinement

Only if Phase 3 shows clear lift but plateau below 75% adoption. Use the framework's detectors as reward function (Approach B above). ~$25-60 additional Colab spend.

---

## Cost summary

| Phase | Time on A100 | Colab cost | Status |
|-------|--------------|------------|--------|
| 0 — pipeline check | ~2h | $0 (included in Pro) | ✅ done |
| 1 — dataset assembly | ~1h local | $0 | ✅ done |
| 2 — production training | ~3-4h | included in $10/mo Pro | 🔜 next |
| 3 — A/B | 0 (local serving) | $0 | — |
| 4 — GRPO (optional) | ~15h | $25-50 additional | — |

**Total realistic spend**: $10-60 across all phases, depending on Phase 4 inclusion.

**Alternative**: Kaggle free tier (T4×2, 30hrs/week). Slower but $0. Reasonable for Phases 0-2 if patient.

---

## Decision gate — resolved 2026-05-14

- [x] **Base model**: `unsloth/gemma-4-31B-it` (matches live `UD-Q4_K_XL` GGUF base on port 8080)
- [x] **Approach**: SFT LoRA first (Approach A). GRPO held in reserve as Phase 4.
- [x] **Dataset mix**: 50% corrected `action='edit'` / 20% legitimate new-file write / 25% other tool calls / 5% whole-cycle traces.
- [x] **Construction**: mining script `tools/mine_friction.py` with counterfactual synthesis from session-log diffs. Spot-check 20 random samples by hand before bulk training.
- [x] **Dataset scope**: combined LoRA across c0rtana + lyla + agent.py CICD builder + agent.py CICD reviewer. **Ebay-bot excluded** (Claude Code agent, different tool grammar).
- [x] **Budget**: Colab Pro $10/mo, escalate to Pro+ $50/mo only if A100 availability blocks Phase 2.
- [x] **Phase 4 GRPO**: deferred. Decide after Phase 3 live A/B telemetry. Skip entirely if SFT hits ≥75% adoption.

**Next action**: Phase 2 — production training. Load `phase1_examples.jsonl` (121 examples) into the adapted Unsloth notebook with production knobs (r=32, 1 epoch, lr=1e-4, cosine LR, lora_dropout=0). Train ~3-4h on A100. Run offline eval. Export Q4_K_M GGUF and serve on port 8083 for A/B.
