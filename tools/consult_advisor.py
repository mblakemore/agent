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
      "prefill_token_budget": 1500,           # brief handed to GLM, hard ceiling
      "max_tokens": 512,                      # GLM's answer, capped
      "max_calls_per_task": 3,                # latency-budget guard
      "timeout_s": 900                        # ~15min: real worst case, see md
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
    "enabled": False,          # off unless explicitly configured
    "base_url": "http://127.0.0.1:8000",
    "model": "glm-5.2",
    "prefill_token_budget": 1500,
    "max_tokens": 512,
    "max_calls_per_task": 3,
    "timeout_s": 900,
}

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


def _read_role(role: str) -> dict:
    """Read a model-role block from config.json (cwd), merged over defaults.

    Mirrors think.py's config-reading convention. ``advisor`` merges over
    _DEFAULTS; ``summary`` falls back to the llama.cpp default endpoint.
    """
    cfg = {}
    try:
        path = os.path.join(os.getcwd(), "config.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                cfg = json.load(f)
    except Exception:
        cfg = {}
    if role == "advisor":
        raw = cfg.get("advisor", {}) or {}
        out = dict(_DEFAULTS)
        out.update(raw)
        # Enabled-by-default-if-configured: a user advisor block with a base_url
        # is ON unless "enabled": false is set explicitly (matches the gate hook
        # in agent.py). No advisor block → stays disabled (_DEFAULTS).
        out["enabled"] = raw.get("enabled", bool(raw.get("base_url")))
        return out
    # summary role (for distillation) — reuse the fast model.
    out = {"base_url": "http://127.0.0.1:8082", "model": "gemma-4-E4B",
           "enabled": True, "timeout_s": 120}
    out.update(cfg.get("summary", {}) or {})
    return out


def _chat(role_cfg: dict, messages: list, max_tokens: int, temperature: float,
          timeout_s: int) -> str:
    """One OpenAI-compatible /v1/chat/completions call. Raises on failure."""
    body = {
        "model": role_cfg.get("model", ""),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
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
    return (data["choices"][0]["message"]["content"] or "").strip()


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
    t0 = time.time()
    try:
        answer = _chat(
            a,
            [{"role": "system", "content": _ADVISOR_SYSTEM},
             {"role": "user", "content": user}],
            max_tokens=int(a.get("max_tokens", 512)),
            temperature=0.3,
            timeout_s=int(a.get("timeout_s", 900)),
        )
    except requests.exceptions.Timeout:
        return (f"[advisor timed out after {a.get('timeout_s')}s — proceed with "
                f"the fast model's best judgment. (Is `glm serve` up on "
                f"{a['base_url']}?)]")
    except Exception as e:
        return (f"[advisor unreachable ({type(e).__name__}) — proceed without "
                f"it. Check `glm serve` on {a['base_url']}. Detail: {e}]")

    dt = time.time() - t0
    if not answer:
        return "[advisor returned an empty answer — proceed with fast model.]"
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
