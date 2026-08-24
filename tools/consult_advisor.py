"""consult_advisor — the heavyweight escalation tier ("distill-then-ask-GLM").

SPIKE (plan/spikes/, gitignored). Not auto-discovered from here; load via
``load_extra_tools("plan/spikes")`` to test-drive, or promote to ``tools/``.

WHY: the fast GPU driver (Qwen3.6-27B @ :8080) handles the median turn. The
744B GLM-5.2 on CPU (colibri ``glm serve`` @ :8000) is ~0.9 tok/s decode and
~3 pos/s PREFILL — brutal to hold a loop, but unbeatable for the rare hard
sub-problem. So GLM is consulted as a *tool*, bounded, never as the driver.

This is ``think.py``'s heavyweight sibling. think.py deep-reasons on the MAIN
model; this routes to the ADVISOR endpoint and adds the two things GLM's
latency physics demand:

  1. DISTILL FIRST. GLM prefill is ~3 pos/s → feeding it a 4k-token transcript
     is ~22 min before it emits a word. So we compress the caller-supplied
     context on the FAST summary model down to ``prefill_token_budget`` before
     GLM ever sees it. The brief, not the transcript, is what GLM reads.
  2. BUDGET. Per-task call cap + capped generation + a hard prefill ceiling.
     If distillation can't get under budget, we DON'T silently escalate.

Fails OPEN: any endpoint error / timeout / disabled config returns a plain
notice so the driver proceeds WITHOUT the advisor — an escalation tool must
never block the loop it is meant to help.

Config (config.json, new third role alongside llm/summary — see the spike md):

    "advisor": {
      "enabled": true,
      "base_url": "http://127.0.0.1:8000",   # colibri `glm serve`
      "model": "glm-5.2",
      "prefill_token_budget": 1200,           # brief handed to GLM, hard ceiling
      "max_tokens": 512,                      # GLM's answer, capped
      "max_calls_per_task": 3,                # latency-budget guard
      "timeout_s": null,                      # null/absent = DERIVED from the
                                              # budgets + measured speeds (see
                                              # derive_timeout_s); /setup
                                              # calibrate writes "measured"
      "measured": {                           # written by /setup calibrate
        "prefill_pos_per_s": 3.1,
        "decode_tok_per_s": 0.9,
        "probed_at": "2026-08-24T00:00:00Z"
      }
    }
"""

import json
import os
import time

import requests

# think.py injects a callback-aware writer here; plain print() for standalone.
_output = print  # type: ignore[assignment]

# Per-session call counter — the latency-budget guard (mirrors think.py's
# _session_call_count / _OVERUSE_THRESHOLD). Reset by agent.py at task start.
_session_call_count = 0

_DEFAULTS = {
    # Base value only — effective enablement is advisor_active(): ON by
    # default (self-consult on main's endpoint) unless explicitly disabled.
    "enabled": False,
    "base_url": "http://127.0.0.1:8000",
    "model": "glm-5.2",
    "prefill_token_budget": 1200,
    "max_tokens": 512,
    "max_calls_per_task": 3,
    # None = DERIVED from measured (or fallback) speeds via derive_timeout_s.
    # The old fixed 900 was internally inconsistent: at the advisor's own
    # documented physics (~3 pos/s prefill, ~0.9 tok/s decode) the default
    # budgets need ~18 min — the timeout guaranteed empty answers (measured
    # live 2026-08-24: a 600 s consult timed out; the full answer took 2533 s).
    "timeout_s": None,
}

# Prompt overhead the budget doesn't cover: system prompt + the caller's
# question ride alongside the distilled brief in the prefill.
_PREFILL_OVERHEAD_TOKENS = 400
# Fallback speeds when the advisor has never been calibrated — deliberately
# CONSERVATIVE (slower than the docstring's nominal 3 pos/s / 0.9 tok/s),
# because an over-generous timeout costs patience while an under-generous one
# costs the entire consult. /setup calibrate (or /setup advisor) measures the
# real rates into config["advisor"]["measured"] and tightens this.
_FALLBACK_PREFILL_POS_PER_S = 2.5
_FALLBACK_DECODE_TOK_PER_S = 0.25
_TIMEOUT_MARGIN = 1.5          # measured rates vary with load; pad the derive
_TIMEOUT_FLOOR_S = 300


def derive_timeout_s(adv_cfg: dict) -> int:
    """Timeout consistent with the configured budgets and measured speeds.

    An explicit ``timeout_s`` in the config always wins. Otherwise:
    ``margin * (prefill_tokens / prefill_rate + max_tokens / decode_rate) + 120``
    using ``adv_cfg["measured"]`` rates when present (written by
    ``/setup calibrate``), else the conservative fallbacks above.
    Shared with setup_wizard so the wizard writes the same number this tool
    would derive.
    """
    explicit = adv_cfg.get("timeout_s")
    if explicit:
        return int(explicit)
    measured = adv_cfg.get("measured") or {}
    pp = float(measured.get("prefill_pos_per_s") or _FALLBACK_PREFILL_POS_PER_S)
    dec = float(measured.get("decode_tok_per_s") or _FALLBACK_DECODE_TOK_PER_S)
    prefill = int(adv_cfg.get("prefill_token_budget",
                              _DEFAULTS["prefill_token_budget"]))
    out = int(adv_cfg.get("max_tokens", _DEFAULTS["max_tokens"]))
    total = _TIMEOUT_MARGIN * (
        (prefill + _PREFILL_OVERHEAD_TOKENS) / max(pp, 0.1)
        + out / max(dec, 0.01)) + 120
    return max(_TIMEOUT_FLOOR_S, int(total))

_ADVISOR_SYSTEM = (
    "You are a slow but powerful advisor (a 744B model) consulted by a fast "
    "agent that is mid-task. You are on the critical path and every token you "
    "emit is expensive, so be DECISIVE and BOUNDED: give the single best "
    "recommendation, the one reason it wins, and the concrete next action. No "
    "throat-clearing, no options menu, no restating the question. If the brief "
    "is insufficient to decide, say exactly what one fact you need instead of "
    "guessing."
)


def _approx_tokens(text: str) -> int:
    # ~4 chars/token, same rough heuristic llm_backend uses.
    return max(1, len(text) // 4)


# Soft telemetry: emit advisor-call OUTCOMES through the same Prometheus
# pipeline the agent + CICD/beewatcher already consume
# (agentpy_patch_events_total{name="advisor_call"}). Invocations themselves are
# already captured by the dispatcher's record_tool_call("consult_advisor"), so
# we add only the outcome dimension here — no bespoke marker file. Soft import
# keeps the tool portable/standalone; record_patch_event is a no-op unless the
# hosting process called telemetry.init().
try:
    import telemetry as _telemetry
except Exception:  # standalone / test — no telemetry module on the path
    _telemetry = None


def _emit_outcome(kind: str) -> None:
    """Best-effort advisor-call outcome counter. Never raises."""
    try:
        if _telemetry is not None:
            _telemetry.record_patch_event("advisor_call", kind=kind)
    except Exception:
        pass


# The engine's own default main endpoint (agent.py zero-config behavior) —
# the last link in the advisor/summary fall-back chain below.
_MAIN_DEFAULT_URL = "http://127.0.0.1:8080"


def advisor_active(cfg: dict) -> bool:
    """Single source of truth for whether the advisor tier is ON.

    An explicit ``advisor.enabled`` always wins. Otherwise the tier is ON BY
    DEFAULT: it always has an endpoint to talk to — its own ``base_url``,
    else MAIN's (self-consult fallback), else the engine's default main
    endpoint. The boot probe still unloads the tool if that endpoint is
    actually unreachable, so default-on is safe on hosts with no server.
    Shared by the tool, agent.py's boot probe, and the gate hook so all
    three always agree.
    """
    if not isinstance(cfg, dict):
        cfg = {}
    raw = cfg.get("advisor", {}) or {}
    return bool(raw.get("enabled", True))


def _load_cfg() -> dict:
    """Read .agent/config.json FIRST, then legacy ./config.json — the SAME
    precedence as agent.py's _load_config, so the tool and the gate hook
    agree on configuration (a mismatch made auto-invoke read "disabled"
    whenever config lived in .agent/, e.g. the replay + agentx)."""
    for _p in (os.path.join(os.getcwd(), ".agent", "config.json"),
               os.path.join(os.getcwd(), "config.json")):
        try:
            if os.path.exists(_p):
                with open(_p, encoding="utf-8", errors="replace") as f:
                    loaded = json.load(f)
                return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}
    return {}


def _read_role(role: str) -> dict:
    """Read a model-role block from config.json (cwd), merged over defaults.

    Fall-back chain (both roles): own block's endpoint → MAIN's endpoint →
    the engine default. As long as main has an endpoint, summary distills on
    it and the advisor consults it (self-consult) — no dead legacy defaults.
    """
    cfg = _load_cfg()
    main = cfg.get("llm", {}) or {}
    if role == "advisor":
        raw = cfg.get("advisor", {}) or {}
        out = dict(_DEFAULTS)
        out.update(raw)
        if not raw.get("base_url"):
            # No advisor endpoint of its own — self-consult on main.
            out["base_url"] = main.get("base_url") or _MAIN_DEFAULT_URL
            if not raw.get("model"):
                out["model"] = main.get("model", "")
            if not raw.get("api_key") and main.get("api_key"):
                out["api_key"] = main["api_key"]
        out["enabled"] = advisor_active(cfg)
        return out
    # summary role (for distillation) — own block, else the main endpoint.
    out = {"enabled": True, "timeout_s": 120}
    s = cfg.get("summary", {}) or {}
    if s.get("base_url") or s.get("api_url"):
        out.update(s)
    else:
        out["base_url"] = main.get("base_url") or _MAIN_DEFAULT_URL
        out["model"] = main.get("model", "")
        if main.get("api_key"):
            out["api_key"] = main["api_key"]
        out.update({k: v for k, v in s.items() if k not in
                    ("base_url", "api_url", "model", "api_key")})
    return out


def _chat(role_cfg: dict, messages: list, max_tokens: int, temperature: float,
          timeout_s: int) -> str:
    """One OpenAI-compatible /v1/chat/completions call. Raises on failure."""
    content, _reasoning, _finish = _chat_full(role_cfg, messages, max_tokens,
                                              temperature, timeout_s)
    return content


def _chat_full(role_cfg: dict, messages: list, max_tokens: int,
               temperature: float, timeout_s: int) -> tuple:
    """Like _chat but returns ``(content, reasoning_content, finish_reason)``.

    Reasoning models (Qwen-class on llama-server) emit their thinking as
    ``reasoning_content`` BEFORE any ``content`` — a hard question can spend
    the whole ``max_tokens`` budget reasoning and finish with empty content,
    which naive content-only reads misreport as "empty answer" (measured live
    2026-08-24: two GPU-advisor consults, both empty). Callers that care
    inspect the extra fields to retry or salvage.
    """
    body = {
        "model": role_cfg.get("model", ""),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    # Endpoint-specific request extras, verbatim from config — e.g.
    # {"chat_template_kwargs": {"enable_thinking": false}} to stop a
    # Qwen-class reasoning model from spending the whole budget thinking.
    extra = role_cfg.get("extra_body")
    if isinstance(extra, dict):
        body.update(extra)
    headers = {"Content-Type": "application/json"}
    key = role_cfg.get("api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    r = requests.post(
        f"{role_cfg['base_url'].rstrip('/')}/v1/chat/completions",
        json=body, headers=headers, timeout=timeout_s,
    )
    r.raise_for_status()
    data = r.json()
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    return ((msg.get("content") or "").strip(),
            (msg.get("reasoning_content") or "").strip(),
            choice.get("finish_reason") or "")


def _distill(question: str, context: str, budget_tokens: int) -> str:
    """Compress ``context`` on the FAST summary model, scoped to ``question``,
    down to ~budget_tokens. Returns the brief (or the raw context if it is
    already under budget, or if distillation fails — never blocks)."""
    if _approx_tokens(context) <= budget_tokens:
        return context
    s = _read_role("summary")
    budget_chars = budget_tokens * 4
    prompt = (
        f"Compress the CONTEXT below into a brief of at most ~{budget_tokens} "
        f"tokens, keeping ONLY what is needed to answer this QUESTION:\n\n"
        f"QUESTION: {question}\n\nCONTEXT:\n{context}\n\n"
        f"Output only the brief — facts, constraints, and what has already "
        f"been tried. No preamble."
    )
    try:
        brief = _chat(
            s, [{"role": "user", "content": prompt}],
            max_tokens=budget_tokens, temperature=0.2,
            timeout_s=int(s.get("timeout_s", 120)),
        )
        if brief:
            # Hard ceiling even if the summarizer overshot.
            return brief[:budget_chars]
    except Exception:
        pass
    # Distillation unavailable → hard-truncate rather than blow GLM's prefill.
    return context[:budget_chars]


def startup_check(timeout_s: int = 5) -> tuple:
    """Probe the advisor endpoint at agent startup.

    Returns ``(ok: bool, detail: str)``.  Uses the same config path as
    ``_read_role`` so the result is consistent with what the tool will see
    at call time.  Never raises — any exception maps to ``(False, reason)``.
    """
    a = _read_role("advisor")
    if not a.get("enabled"):
        return False, "disabled"
    url = a["base_url"].rstrip("/") + "/v1/models"
    try:
        r = requests.get(url, timeout=timeout_s)
        if r.status_code < 500:
            return True, "ok"
        return False, f"http {r.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "unreachable"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def consult_advisor(question: str, context: str = "", reason: str = "") -> str:
    """Escalate ONE bounded, hard sub-problem to the heavyweight advisor tier.

    Use SPARINGLY — the advisor is ~30-50x slower than the driver. Reserve for:
    a decision you are genuinely stuck on, a gate you have failed twice, or a
    high-consequence irreversible step. Pose a SPECIFIC question and pass only
    the relevant context (it is auto-compressed before the advisor sees it).

    Args:
        question: The specific question to decide. Be concrete.
        context: Relevant facts / what you have tried. Auto-distilled to the
            prefill budget on the fast model first. Do NOT dump the transcript.
        reason: Short tag for why you are escalating (logged; e.g.
            "failed verify gate x2", "irreversible: git push").
    """
    global _session_call_count
    if not isinstance(question, str) or not question.strip():
        return "Error: question must be a non-empty string."

    a = _read_role("advisor")
    if not a.get("enabled"):
        return ("[advisor tier disabled — proceeding without escalation. "
                "Enable via config.json \"advisor\".enabled to use GLM.]")

    cap = int(a.get("max_calls_per_task", 3))
    if _session_call_count >= cap:
        _emit_outcome("budget_spent")
        return (f"[advisor budget spent: {_session_call_count}/{cap} calls this "
                f"task. Decide with the fast model — do not escalate again.]")

    budget = int(a.get("prefill_token_budget", 1500))
    brief = _distill(question, context, budget) if context.strip() else ""

    total_prefill = _approx_tokens(_ADVISOR_SYSTEM) + _approx_tokens(question) \
        + _approx_tokens(brief)
    _output(f"⏳ consulting advisor (GLM) — reason={reason or 'unspecified'}, "
            f"~{total_prefill} prefill tok, cap {a.get('max_tokens')} out. "
            f"This is the slow tier; expect minutes.")

    user = question if not brief else f"BRIEF:\n{brief}\n\nQUESTION: {question}"
    _session_call_count += 1
    msgs = [{"role": "system", "content": _ADVISOR_SYSTEM},
            {"role": "user", "content": user}]
    max_out = int(a.get("max_tokens", 512))
    timeout_s = derive_timeout_s(a)
    t0 = time.time()
    try:
        answer, reasoning, finish = _chat_full(
            a, msgs, max_tokens=max_out, temperature=0.3, timeout_s=timeout_s)
        # Reasoning-model salvage: thinking ate the whole budget before any
        # content was emitted. Retry ONCE with a doubled budget (and a timeout
        # re-derived for it) — trivial on a GPU advisor, and the path never
        # triggers on direct-answer models like GLM.
        if not answer and reasoning and finish == "length":
            # Reasoning models routinely need 3-6x their answer length to
            # think first; a mere doubling was measured still reasoning-only
            # (E5b, 2026-08-24). Jump straight to a budget that fits thought
            # plus answer.
            retry_out = min(max(max_out * 8, 4096), 8192)
            retry_timeout = derive_timeout_s({**a, "timeout_s": None,
                                              "max_tokens": retry_out})
            _output(f"⏳ advisor spent all {max_out} tokens reasoning — "
                    f"retrying once with max_tokens={retry_out}.")
            answer, reasoning, finish = _chat_full(
                a, msgs, max_tokens=retry_out, temperature=0.3,
                timeout_s=retry_timeout)
        if not answer and reasoning:
            # Still no final answer — salvage the reasoning tail honestly
            # rather than reporting "empty".
            _emit_outcome("reasoning_only")
            tail = reasoning[-1500:]
            dt = time.time() - t0
            return (f"[advisor hit its token cap mid-reasoning — no final "
                    f"answer; its last reasoning follows. Treat as a partial "
                    f"signal, not a verdict.]\n…{tail}\n\n---\n"
                    f"[advisor: {dt:.0f}s, call {_session_call_count}/{cap}, "
                    f"reason={reason or 'n/a'}, truncated-reasoning]")
    except requests.exceptions.Timeout:
        _emit_outcome("timeout")
        return (f"[advisor timed out after {timeout_s}s — proceed with "
                f"the fast model's best judgment. (Is `glm serve` up on "
                f"{a['base_url']}? If it is just slow, run /setup calibrate "
                f"to measure its real speed and derive a fitting timeout.)]")
    except Exception as e:
        _emit_outcome("unreachable")
        return (f"[advisor unreachable ({type(e).__name__}) — proceed without "
                f"it. Check `glm serve` on {a['base_url']}. Detail: {e}]")

    dt = time.time() - t0
    if not answer:
        _emit_outcome("empty")
        return "[advisor returned an empty answer — proceed with fast model.]"
    _emit_outcome("answered")
    return (f"{answer}\n\n---\n[advisor: {dt:.0f}s, "
            f"call {_session_call_count}/{cap}, reason={reason or 'n/a'}]")


# --- tool registration (OpenAI-compatible schema; matches tools/__init__) ---
fn = consult_advisor

definition = {
    "type": "function",
    "function": {
        "name": "consult_advisor",
        "description": (
            "Escalate ONE hard, bounded sub-problem to the slow heavyweight "
            "advisor model (GLM-5.2 744B). ~30-50x slower than you, so use it "
            "SPARINGLY and only when depth is worth minutes: a decision you are "
            "genuinely stuck on, a gate you have already failed twice, or a "
            "high-consequence irreversible step. Pose a SPECIFIC question; pass "
            "only the relevant context (auto-compressed before the advisor "
            "reads it). Returns the advisor's decisive recommendation. Do not "
            "call it for routine turns, routing, or simple edits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to decide. Concrete.",
                },
                "context": {
                    "type": "string",
                    "description": ("Relevant facts and what you have already "
                                    "tried. Auto-distilled to the prefill "
                                    "budget. Do NOT paste the transcript."),
                },
                "reason": {
                    "type": "string",
                    "description": ("Why you are escalating, e.g. 'failed "
                                    "verify gate x2' or 'irreversible: push'."),
                },
            },
            "required": ["question"],
        },
    },
}
