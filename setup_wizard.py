"""/setup wizard — Phase 1 (plan/setup-command.md).

Configures the main and summary roles against a llama.cpp / OpenAI-compatible
endpoint, probes the endpoint live (P1 reachability, P2 auth, P3 model
resolve, P4 max context), derives the agent's context-budget knobs from the
measurements, and writes ``./.agent/config.json``.

Design rules (from the spec):
- Pure-function core: probes and calibration take an injectable ``http``
  callable and return data; the interactive shell is a thin layer over
  ``input()``/``print()`` so tests drive it with scripted answers.
- A probe that cannot run reports UNMEASURED — distinct from FAILED; absent
  is not zero.
- Calibration NEVER writes a number the probes did not support: anything
  unmeasured keeps the shipped default and says so.
- Phase 1 writes the legacy ``llm`` / ``summary`` blocks (which
  ``_synthesize_backends_registry`` builds the roles from) — the explicit
  ``backends`` registry form arrives with bedrock/foundry in Phase 2, where
  ``kind`` actually varies.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

import theme

OK = "OK"
FAILED = "FAILED"
UNMEASURED = "UNMEASURED"

# Empirical context search ceiling (spec: cap search at 512k).
_EMPIRICAL_CAP_TOKENS = 524288
# The pad paragraph tokenizes predictably (~4 chars/token English prose).
_PAD_SENTENCE = (
    "The quick brown fox jumps over the lazy dog while the calibration "
    "wizard measures the context window of this endpoint. "
)
_CHARS_PER_TOKEN = 4.0


# ── HTTP shim (injectable) ─────────────────────────────────────────────


def _default_http(url, data=None, headers=None, timeout=20):
    """POST if data else GET. Returns (status_code, parsed_json_or_text).

    Network errors return (0, str(err)) — the probes translate, they never
    raise. HTTP error responses return their real status + body so auth
    failures (401) are distinguishable from unreachability.
    """
    req = urllib.request.Request(
        url,
        data=(json.dumps(data).encode() if data is not None else None),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:  # DNS, refused, timeout — unreachable class
        return 0, str(e)
    try:
        return status, json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return status, body


def _auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


# ── Probes (llamacpp / OpenAI-compatible) ──────────────────────────────


def probe_llamacpp(base_url, api_key=None, model=None, http=None,
                   allow_empirical=True, ask_consent=None):
    """Run P1-P4 against a llama.cpp / OpenAI-compatible endpoint.

    Returns a report dict:
      {"p1_reach": {"status", "detail"},
       "p2_auth":  {"status", "detail"},
       "p3_model": {"status", "value", "detail"},   # value = resolved model id
       "p4_ctx":   {"status", "value", "detail"}}   # value = max ctx tokens
    """
    http = http or _default_http
    base = base_url.rstrip("/")
    hdrs = _auth_headers(api_key)
    report = {}

    # P1: /health (llama-server) → fall back to /v1/models (any OpenAI server)
    status, body = http(f"{base}/health", headers=hdrs)
    if status == 200:
        report["p1_reach"] = {"status": OK, "detail": "/health 200"}
    else:
        status2, _ = http(f"{base}/v1/models", headers=hdrs)
        if status2 in (200, 401, 403):
            report["p1_reach"] = {"status": OK,
                                  "detail": f"/v1/models {status2} (no /health)"}
        elif status == 0 and status2 == 0:
            report["p1_reach"] = {"status": FAILED,
                                  "detail": f"unreachable: {body}"}
        else:
            report["p1_reach"] = {"status": FAILED,
                                  "detail": f"/health {status}, /v1/models {status2}"}
    reachable = report["p1_reach"]["status"] == OK

    # P2: auth — 401/403 with the configured key = failed auth.
    if not reachable:
        report["p2_auth"] = {"status": UNMEASURED, "detail": "endpoint unreachable"}
    else:
        status, body = http(f"{base}/v1/models", headers=hdrs)
        if status in (401, 403):
            report["p2_auth"] = {"status": FAILED,
                                 "detail": f"HTTP {status} — key rejected"
                                           + ("" if api_key else " (no key configured)")}
        elif status == 200:
            report["p2_auth"] = {"status": OK, "detail": "authorized"}
        else:
            report["p2_auth"] = {"status": UNMEASURED,
                                 "detail": f"/v1/models {status}"}

    # P3: model resolve
    models = []
    status, body = http(f"{base}/v1/models", headers=hdrs)
    if status == 200 and isinstance(body, dict):
        models = [m.get("id", "") for m in body.get("data", []) if isinstance(m, dict)]
    if models:
        if model and model in models:
            report["p3_model"] = {"status": OK, "value": model,
                                  "detail": "configured id listed"}
        elif model:
            report["p3_model"] = {"status": FAILED, "value": model,
                                  "detail": f"'{model}' not in {models[:5]}"}
        else:
            report["p3_model"] = {"status": OK, "value": models[0],
                                  "detail": f"auto-picked first of {len(models)}"}
    else:
        report["p3_model"] = {"status": UNMEASURED, "value": model,
                              "detail": f"/v1/models gave {status}"}

    # P4: max context — /props → /slots → /v1/models metadata → empirical.
    # The empirical search sends prompts up to the 512k cap — millions of tokens
    # worst case — so it runs ONLY with explicit consent, on every endpoint: a
    # hosted OpenAI-compatible API bills it like any input (it used to run
    # unasked whenever the metadata routes were missing).
    ctx, how = _probe_ctx(base, hdrs, http, body if status == 200 else None)
    if ctx:
        report["p4_ctx"] = {"status": OK, "value": int(ctx), "detail": how}
    elif reachable and allow_empirical and not (
            ask_consent is not None and ask_consent(empirical_worst_case_tokens())):
        report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                            "detail": "server doesn't report it; measuring declined"}
    elif reachable and allow_empirical:
        ctx, how = _probe_ctx_empirical(
            base, hdrs, http, report["p3_model"].get("value"))
        if ctx:
            report["p4_ctx"] = {"status": OK, "value": int(ctx), "detail": how}
        else:
            report["p4_ctx"] = {"status": UNMEASURED, "value": None, "detail": how}
    else:
        report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                            "detail": "no metadata route" if reachable
                                      else "endpoint unreachable"}
    return report


def _probe_ctx(base, hdrs, http, models_body):
    status, body = http(f"{base}/props", headers=hdrs)
    if status == 200 and isinstance(body, dict):
        n = (body.get("default_generation_settings") or {}).get("n_ctx")
        if n:
            return n, "/props n_ctx"
    status, body = http(f"{base}/slots", headers=hdrs)
    if status == 200 and isinstance(body, list) and body:
        n = body[0].get("n_ctx")
        if n:
            return n, "/slots n_ctx"
    if isinstance(models_body, dict):
        for m in models_body.get("data", []):
            for key in ("context_length", "max_context_length", "max_model_len"):
                if isinstance(m, dict) and m.get(key):
                    return m[key], f"/v1/models {key}"
    return None, "no metadata route"


def _binary_search_ctx(accepted, cap=_EMPIRICAL_CAP_TOKENS):
    """Shared empirical search core. ``accepted(tokens) -> (ok, measured)``
    where ``measured`` is the server-reported prompt token count when the
    endpoint provides one. Returns (tokens_or_None, detail)."""
    ok, measured = accepted(1024)
    if not ok:
        return None, "empirical: even a 1k prompt rejected"
    lo, lo_measured = 1024, measured
    hi = cap
    # Grow until rejection or cap (doubling), then bisect.
    while lo * 2 <= hi:
        ok, measured = accepted(lo * 2)
        if not ok:
            hi = lo * 2
            break
        lo, lo_measured = lo * 2, measured
    else:
        return (lo_measured or lo), f"empirical: accepted at cap ({lo})"
    for _ in range(6):
        mid = (lo + hi) // 2
        if mid <= lo or mid >= hi:
            break
        ok, measured = accepted(mid)
        if ok:
            lo, lo_measured = mid, measured
        else:
            hi = mid
    return (lo_measured or lo), f"empirical: largest accepted ~{lo} tokens"


def empirical_worst_case_tokens(cap=_EMPIRICAL_CAP_TOKENS):
    """Total prompt tokens the empirical search can send, worst case over every
    possible context limit (rejected requests included — a gateway may bill them).
    About 3.9M at the 512k cap: several full context windows."""
    worst = 0
    for limit in list(range(1024, cap + 1, 997)) + [cap, cap + 1]:
        sent = [0]

        def accepted(n, limit=limit):
            sent[0] += n
            return n <= limit, n
        _binary_search_ctx(accepted, cap)
        worst = max(worst, sent[0])
    return worst


def _probe_ctx_empirical(base, hdrs, http, model, cap=_EMPIRICAL_CAP_TOKENS):
    """OpenAI-shape empirical search (llamacpp kind, free local endpoints)."""
    def accepted(tokens):
        pad = _PAD_SENTENCE * max(1, int(tokens * _CHARS_PER_TOKEN
                                          / len(_PAD_SENTENCE)))
        body = {"messages": [{"role": "user", "content": pad}],
                "max_tokens": 1, "temperature": 0.0}
        if model:
            body["model"] = model
        status, resp = http(f"{base}/v1/chat/completions", data=body,
                            headers=hdrs, timeout=600)
        if status != 200:
            return False, None
        measured = None
        if isinstance(resp, dict):
            measured = (resp.get("usage") or {}).get("prompt_tokens")
        return True, measured
    return _binary_search_ctx(accepted, cap)


def _probe_ctx_empirical_anthropic(base, api_key, http, model,
                                   cap=_EMPIRICAL_CAP_TOKENS):
    """Anthropic-messages-shape empirical search (foundry kind). METERED —
    caller must hold operator consent before invoking (spec: consent gate
    with a printed token estimate)."""
    hdrs = {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}

    def accepted(tokens):
        pad = _PAD_SENTENCE * max(1, int(tokens * _CHARS_PER_TOKEN
                                          / len(_PAD_SENTENCE)))
        body = {"model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": pad}]}
        status, resp = http(f"{base.rstrip('/')}/v1/messages", data=body,
                            headers=hdrs, timeout=600)
        if status != 200:
            return False, None
        measured = None
        if isinstance(resp, dict):
            measured = (resp.get("usage") or {}).get("input_tokens")
        return True, measured
    return _binary_search_ctx(accepted, cap)


def probe_gateway_backend(section, role, http=None, ask_consent=None):
    """P1-P4 for bedrock/foundry via the constructed Backend object.

    Uses the backend's own interface (health / list_models /
    detect_ctx_size) so credential-resolution chains (env, bedrock_store,
    AZURE_FOUNDRY_* per role) behave exactly as they will at runtime. A
    ConfigError at construction IS the P1 verdict — the session would fail
    the same way.

    Foundry's health() is a no-probe stub, so reachability+auth there is a
    1-token /v1/messages invoke (spec P1 endorses the 1-token probe;
    metered but ~nothing). The EMPIRICAL context search is the costly one:
    consent-gated via ``ask_consent(estimate_tokens) -> bool``; no consent
    callback means never.
    """
    http = http or _default_http
    kind = section.get("kind", "llamacpp")
    report = {}
    try:
        from llm_backend import build_backend, ConfigError
    except Exception as e:  # pragma: no cover - import environment issue
        report["p1_reach"] = {"status": UNMEASURED,
                              "detail": f"llm_backend unavailable: {e}"}
        report["p2_auth"] = {"status": UNMEASURED, "detail": "no backend"}
        report["p3_model"] = {"status": UNMEASURED, "value": section.get("model"),
                              "detail": "no backend"}
        report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                            "detail": "no backend"}
        return report

    cfg = dict(section)
    cfg["role"] = role
    try:
        backend = build_backend(cfg)
    except ConfigError as e:
        detail = f"construction refused: {e}"
        report["p1_reach"] = {"status": FAILED, "detail": detail}
        report["p2_auth"] = {"status": FAILED, "detail": "credentials missing"}
        report["p3_model"] = {"status": UNMEASURED, "value": section.get("model"),
                              "detail": "no backend"}
        report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                            "detail": "no backend"}
        return report
    except Exception as e:
        report["p1_reach"] = {"status": FAILED, "detail": f"construction error: {e}"}
        report["p2_auth"] = {"status": UNMEASURED, "detail": "no backend"}
        report["p3_model"] = {"status": UNMEASURED, "value": section.get("model"),
                              "detail": "no backend"}
        report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                            "detail": "no backend"}
        return report

    # P1 + P2: the backend's own health probe. Foundry's is a stub, so a
    # 1-token invoke does the honest reachability+auth work there.
    invoked_ok = None
    try:
        healthy, msg = backend.health()
    except Exception as e:
        healthy, msg = False, str(e)
    if kind == "foundry":
        base = getattr(backend, "api_url", section.get("api_url", ""))
        status, resp = http(
            f"{base.rstrip('/')}/v1/messages",
            data={"model": backend.model, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": getattr(backend, "api_key", ""),
                     "anthropic-version": "2023-06-01"})
        invoked_ok = status == 200
        if status == 200:
            report["p1_reach"] = {"status": OK, "detail": "1-token invoke 200"}
            report["p2_auth"] = {"status": OK, "detail": "authorized"}
        elif status in (401, 403):
            report["p1_reach"] = {"status": OK, "detail": f"reachable (HTTP {status})"}
            report["p2_auth"] = {"status": FAILED,
                                 "detail": f"HTTP {status} — key rejected"}
        elif status == 0:
            report["p1_reach"] = {"status": FAILED, "detail": f"unreachable: {resp}"}
            report["p2_auth"] = {"status": UNMEASURED, "detail": "unreachable"}
        else:
            report["p1_reach"] = {"status": OK, "detail": f"reachable (HTTP {status})"}
            report["p2_auth"] = {"status": UNMEASURED,
                                 "detail": f"invoke HTTP {status}"}
    else:
        report["p1_reach"] = {"status": OK if healthy else FAILED,
                              "detail": str(msg)}
        report["p2_auth"] = ({"status": OK, "detail": "gateway accepted key"}
                             if healthy else
                             {"status": UNMEASURED,
                              "detail": "health failed — auth unknown"})

    # P3: model resolve
    models = []
    try:
        models = backend.list_models() or []
    except Exception:
        models = []
    model = section.get("model") or getattr(backend, "model", "")
    if models:
        if model and model in models:
            report["p3_model"] = {"status": OK, "value": model,
                                  "detail": "listed by backend"}
        elif model:
            report["p3_model"] = {"status": FAILED, "value": model,
                                  "detail": f"'{model}' not in {models[:5]}"}
        else:
            report["p3_model"] = {"status": OK, "value": models[0],
                                  "detail": f"auto-picked first of {len(models)}"}
    elif invoked_ok:
        report["p3_model"] = {"status": OK, "value": model,
                              "detail": "resolved by 1-token invoke"}
    else:
        report["p3_model"] = {"status": UNMEASURED, "value": model,
                              "detail": "no model listing on this kind"}

    # P4: the backend's own table/metadata first; metered empirical only
    # with explicit consent (foundry only — the bedrock gateway's request
    # shape is not replicated here, an honest Phase 2 scope cut).
    ctx = None
    try:
        ctx = backend.detect_ctx_size()
    except Exception:
        ctx = None
    if ctx:
        report["p4_ctx"] = {"status": OK, "value": int(ctx),
                            "detail": "backend table/metadata"}
    elif kind == "foundry" and ask_consent is not None:
        if ask_consent(empirical_worst_case_tokens()):
            val, how = _probe_ctx_empirical_anthropic(
                getattr(backend, "api_url", ""), getattr(backend, "api_key", ""),
                http, backend.model)
            if val:
                report["p4_ctx"] = {"status": OK, "value": int(val), "detail": how}
            else:
                report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                                    "detail": how}
        else:
            report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                                "detail": "metered empirical probe declined"}
    else:
        report["p4_ctx"] = {"status": UNMEASURED, "value": None,
                            "detail": ("no table row; empirical via gateway "
                                       "not implemented" if kind == "bedrock"
                                       else "no metadata; no consent path")}
    return report


def probe_backend(section, role="main", http=None, ask_consent=None,
                  allow_empirical=True):
    """Kind dispatcher: llamacpp → HTTP probes; bedrock/foundry → backend
    interface probes with the metered-consent gate."""
    kind = (section.get("kind") or "llamacpp").lower()
    if kind == "llamacpp":
        return probe_llamacpp(section.get("base_url", ""),
                              section.get("api_key"), section.get("model"),
                              http=http, allow_empirical=allow_empirical,
                              ask_consent=ask_consent)
    return probe_gateway_backend(section, role, http=http,
                                 ask_consent=ask_consent)


# ── P5 throughput + P6 capabilities (Phase 3) ─────────────────────────


def probe_throughput(section, role="main", http=None):
    """P5 — one 64-token completion; returns
    {"status", "gen_tps", "prompt_tps", "detail", "body"} ("body" = the raw
    response for P6 to inspect, not persisted).

    llamacpp: free, always runs; server-side ``timings`` preferred, wall
    clock as the fallback (labeled as such — the two are different
    instruments). bedrock: UNMEASURED — the backend's complete() cannot cap
    output tokens, so a "small" probe can't be guaranteed small (honest
    scope cut, same class as its empirical-ctx cut). foundry: 64-token
    /v1/messages, metered-trivial (wizard-context only; /setup test never
    calls P5 on metered kinds).
    """
    import time as _time
    http = http or _default_http
    kind = (section.get("kind") or "llamacpp").lower()
    prompt = "Count from 1 to 50 as plain comma-separated numbers."

    if kind == "bedrock":
        return {"status": UNMEASURED, "gen_tps": None, "prompt_tps": None,
                "detail": "bedrock complete() cannot cap tokens — not probed",
                "body": None}

    if kind == "aicommons":
        # metered against the user's monthly quota; the backend sizes max_tokens
        # to the response wall itself, so a throughput figure buys nothing here
        return {"status": UNMEASURED, "gen_tps": None, "prompt_tps": None,
                "detail": "aicommons: metered quota — not probed", "body": None}

    if kind == "foundry":
        base = (section.get("api_url") or "").rstrip("/")
        hdrs = {"x-api-key": section.get("api_key", ""),
                "anthropic-version": "2023-06-01"}
        t0 = _time.monotonic()
        status, body = http(f"{base}/v1/messages",
                            data={"model": section.get("model", ""),
                                  "max_tokens": 64,
                                  "messages": [{"role": "user",
                                                "content": prompt}]},
                            headers=hdrs, timeout=300)
        wall = _time.monotonic() - t0
        if status != 200 or not isinstance(body, dict):
            return {"status": UNMEASURED, "gen_tps": None, "prompt_tps": None,
                    "detail": f"probe HTTP {status}", "body": None}
        out = (body.get("usage") or {}).get("output_tokens") or 64
        return {"status": OK, "gen_tps": round(out / wall, 2) if wall else None,
                "prompt_tps": None, "detail": f"wall-clock over {out} tokens",
                "body": body}

    base = (section.get("base_url") or "").rstrip("/")
    hdrs = _auth_headers(section.get("api_key"))
    req = {"messages": [{"role": "user", "content": prompt}],
           "max_tokens": 64, "temperature": 0.0}
    if section.get("model"):
        req["model"] = section["model"]
    t0 = _time.monotonic()
    status, body = http(f"{base}/v1/chat/completions", data=req,
                        headers=hdrs, timeout=300)
    wall = _time.monotonic() - t0
    if status != 200 or not isinstance(body, dict):
        return {"status": UNMEASURED, "gen_tps": None, "prompt_tps": None,
                "detail": f"probe HTTP {status}", "body": None}
    t = body.get("timings") or {}
    if t.get("predicted_per_second"):
        return {"status": OK,
                "gen_tps": round(t["predicted_per_second"], 2),
                "prompt_tps": round(t.get("prompt_per_second") or 0, 1) or None,
                "detail": "server timings", "body": body}
    out = (body.get("usage") or {}).get("completion_tokens") or 64
    return {"status": OK, "gen_tps": round(out / wall, 2) if wall else None,
            "prompt_tps": None,
            "detail": f"wall-clock over {out} tokens (no server timings)",
            "body": body}


_DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Returns the current time. Call this tool.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def probe_capabilities(section, role="main", http=None, throughput=None):
    """P6 — capability flags, llamacpp/OpenAI-compatible kinds.

    Returns {"timings", "tools_accepted", "tool_roundtrip", "reasoning_param"}
    each OK/FAILED/UNMEASURED. Tool support is the load-bearing one: the
    agent cannot function without tool calling, so its absence is a FAILED,
    not a footnote. Gateway kinds report UNMEASURED (their capability
    surface is the backend class's own concern).
    """
    kind = (section.get("kind") or "llamacpp").lower()
    if kind != "llamacpp":
        u = {"status": UNMEASURED, "detail": f"{kind}: backend-owned surface"}
        return {"timings": dict(u), "tools_accepted": dict(u),
                "tool_roundtrip": dict(u), "reasoning_param": dict(u)}

    http = http or _default_http
    base = (section.get("base_url") or "").rstrip("/")
    hdrs = _auth_headers(section.get("api_key"))
    caps = {}

    # timings: read off the P5 body when provided (no extra request)
    body = (throughput or {}).get("body")
    if isinstance(body, dict):
        caps["timings"] = ({"status": OK, "detail": "server echoes timings"}
                           if body.get("timings") else
                           {"status": FAILED,
                            "detail": "no timings in response — token stats "
                                      "will be wall-clock estimates"})
    else:
        caps["timings"] = {"status": UNMEASURED, "detail": "no P5 body"}

    # tools: accepted (no 400) + actual roundtrip (tool_calls in response)
    req = {"messages": [{"role": "user",
                         "content": "What time is it? Use the tool."}],
           "max_tokens": 64, "temperature": 0.0,
           "tools": [_DUMMY_TOOL], "tool_choice": "auto"}
    if section.get("model"):
        req["model"] = section["model"]
    status, body = http(f"{base}/v1/chat/completions", data=req,
                        headers=hdrs, timeout=300)
    if status == 200 and isinstance(body, dict):
        caps["tools_accepted"] = {"status": OK, "detail": "tools param accepted"}
        msg = ((body.get("choices") or [{}])[0].get("message") or {})
        caps["tool_roundtrip"] = (
            {"status": OK, "detail": "model emitted a tool call"}
            if msg.get("tool_calls") else
            {"status": FAILED,
             "detail": "no tool_calls in response — template/model may not "
                       "support tool calling"})
    elif status == 0:
        caps["tools_accepted"] = {"status": UNMEASURED,
                                  "detail": f"unreachable: {body}"}
        caps["tool_roundtrip"] = {"status": UNMEASURED, "detail": "unreachable"}
    else:
        caps["tools_accepted"] = {"status": FAILED,
                                  "detail": f"HTTP {status} with tools param "
                                            "— the agent REQUIRES tool calls"}
        caps["tool_roundtrip"] = {"status": UNMEASURED,
                                  "detail": "tools param rejected"}

    # reasoning: is the OpenAI-style reasoning_effort param accepted?
    req = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8,
           "reasoning_effort": "low"}
    if section.get("model"):
        req["model"] = section["model"]
    status, _body = http(f"{base}/v1/chat/completions", data=req,
                         headers=hdrs, timeout=300)
    caps["reasoning_param"] = (
        {"status": OK, "detail": "reasoning_effort accepted"} if status == 200
        else {"status": UNMEASURED, "detail": f"HTTP {status}"} if status
        else {"status": UNMEASURED, "detail": "unreachable"})
    return caps


def recommend(report, throughput=None, caps=None, main_section=None,
              summary_section=None):
    """Turn probe results into operator recommendations (pure; printed,
    never silently applied — env and risk decisions stay with the human)."""
    recs = []
    tp = throughput or {}
    if tp.get("status") == OK and tp.get("gen_tps") is not None:
        if tp["gen_tps"] < 15:
            recs.append(f"gen speed {tp['gen_tps']} t/s is slow — consider "
                        "AGENT_STALL_TIMEOUT_S=120 (default 60) so long "
                        "generations aren't killed as stalls")
    c = caps or {}
    if c.get("tools_accepted", {}).get("status") == FAILED:
        recs.append("endpoint REJECTS the tools parameter — the agent cannot "
                    "run against it; fix the server/template first")
    elif c.get("tool_roundtrip", {}).get("status") == FAILED:
        recs.append("model never emitted a tool call in the probe — check the "
                    "chat template (--jinja) and tool support before relying "
                    "on this endpoint")
    if c.get("timings", {}).get("status") == FAILED:
        recs.append("no server timings — token-rate stats in the footer will "
                    "be coarse wall-clock estimates")
    ms, ss = main_section or {}, summary_section or {}
    m_url = ms.get("base_url") or ms.get("api_url")
    if m_url and m_url == (ss.get("base_url") or ss.get("api_url")):
        recs.append("main and summary share one endpoint — summaries queue "
                    "behind main streams on single-slot servers (informational)")
    return recs


def check_drift(current_cfg, fresh_report):
    """Compare a fresh P4 measurement against the _calibrated sidecar.

    Returns (drifted: bool|None, message). None = no baseline to compare
    (never calibrated, or ctx unmeasured now) — distinct from 'no drift'."""
    side = current_cfg.get("_calibrated") or {}
    baseline = side.get("measured_ctx_tokens")
    fresh = (fresh_report.get("p4_ctx") or {}).get("value")
    if not baseline:
        return None, "no calibration baseline on record"
    if not fresh:
        return None, (f"baseline {baseline} on record, but ctx is UNMEASURED "
                      "now — cannot compare")
    delta = abs(fresh - baseline) / baseline
    when = side.get("date", "unknown date")
    if delta > 0.02:
        return True, (f"ctx DRIFTED: {baseline} (calibrated {when}) -> "
                      f"{fresh} measured now ({delta:+.1%}) — recalibration "
                      "recommended")
    return False, (f"no drift: measured {fresh} vs baseline {baseline} "
                   f"(calibrated {when})")


# ── Calibration (probe report → config knobs) ─────────────────────────


def calibrate(report, defaults, chars_per_token=_CHARS_PER_TOKEN):
    """Derive context-budget knobs from the probe report.

    ``defaults`` is the agent's current effective config (read-only). Returns
    (updates, notes): ``updates`` holds ONLY sections/keys the probes support
    — anything unmeasured is absent so the shipped default stays; ``notes``
    is a list of human-readable derivation lines (including the 'kept
    default' statements, so silence never hides an unmeasured knob).
    """
    updates, notes = {}, []
    p4 = report.get("p4_ctx", {})
    if p4.get("status") != OK or not p4.get("value"):
        notes.append("ctx UNMEASURED — all context knobs keep their defaults")
        return updates, notes

    ctx_tokens = int(p4["value"])
    budget = int(ctx_tokens * 0.9)          # 10% safety margin (spec)
    ctx_chars = int(budget * chars_per_token)

    c = updates.setdefault("context", {})
    c["ctx_size"] = budget
    notes.append(f"context.ctx_size = {budget} (measured {ctx_tokens} − 10%)")

    # Scale message-count cap with ctx: ~20 msgs at 8k … ~400 at 192k, i.e.
    # ~1 message per 480 tokens, clamped to a sane band.
    msgs = max(20, min(500, budget // 480))
    c["max_context_messages"] = msgs
    notes.append(f"context.max_context_messages = {msgs} (~1 per 480 tokens)")

    # summary_threshold is a MESSAGE count: how many messages must have fallen
    # out of the window before a summary of them is generated. About a fifth of
    # the cap, so the first chunk eviction (40% of the cap) always fires one. It
    # used to be written in chars (ctx_chars * 0.6 = 432000 at 180k ctx), which
    # the engine compared to a message count — no calibrated config ever summarised.
    c["summary_threshold"] = max(5, msgs // 5)
    notes.append(f"context.summary_threshold = {c['summary_threshold']} messages (~1/5 of the message cap)")

    if ctx_tokens < 16384:
        d = defaults.get("context", {})
        c["max_full_lines"] = max(10, int(d.get("max_full_lines", 50)) // 2)
        c["preview_lines"] = max(5, int(d.get("preview_lines", 10)) // 2)
        notes.append("small ctx (<16k): max_full_lines / preview_lines halved")
    else:
        notes.append("max_full_lines / preview_lines keep defaults (ctx >= 16k)")

    p = updates.setdefault("preferences", {})
    default_cap = int(defaults.get("preferences", {})
                      .get("max_text_response_chars", 6000))
    p["max_text_response_chars"] = min(default_cap, max(1500, ctx_chars // 10))
    notes.append(f"preferences.max_text_response_chars = {p['max_text_response_chars']}")

    import datetime
    updates["_calibrated"] = {
        "measured_ctx_tokens": ctx_tokens,
        "ctx_source": p4.get("detail", "?"),
        "chars_per_token": chars_per_token,
        "date": datetime.date.today().isoformat(),
    }
    return updates, notes


# ── Config writing ────────────────────────────────────────────────────


_ENDPOINT_KEYS = ("base_url", "api_url", "api_key", "api_key_env", "model")


def _replace_section(config_path, key):
    """Drop one top-level section from the config file so the next write_config
    writes it whole instead of deep-merging into it."""
    path = Path(config_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if isinstance(data, dict) and key in data:
        del data[key]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")


def _replace_switched_roles(config_path, current_cfg, updates):
    """A role whose kind changed is replaced, not merged: the old kind's base_url
    or key left underneath would outrank the new backend's own credentials.
    Settings that aren't the endpoint (temperature, effort, ...) carry over."""
    for role in ("llm", "summary"):
        old, new = current_cfg.get(role), updates.get(role)
        if not (isinstance(old, dict) and isinstance(new, dict)):
            continue
        if (old.get("kind") or "llamacpp") != (new.get("kind") or "llamacpp"):
            updates[role] = {**{k: v for k, v in old.items()
                                if k not in _ENDPOINT_KEYS and k != "kind"}, **new}
            _replace_section(config_path, role)


def write_config(config_path, updates):
    """Deep-merge ``updates`` into the JSON file at ``config_path``.

    Same conventions as agent._persist_config_value: corrupt existing file
    treated as empty, indent=2, parents created. chmod 600 whenever any
    api_key is present anywhere in the result.
    """
    path = Path(config_path)
    data = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, IOError):
            data = {}

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(data, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")

    def has_key(d):
        return any((k == "api_key" and v) or (isinstance(v, dict) and has_key(v))
                   for k, v in d.items())
    if has_key(data):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


# ── Local server discovery ────────────────────────────────────────────

# Common local model-server ports: llama-server convention 8080+ (this
# box runs GPU:8080 / CPU:8082), glm serve :8000, :5000 generic, ollama
# :11434 (OpenAI-compatible under /v1).
_SCAN_PORTS = (8080, 8081, 8082, 8083, 8084, 8085, 8086,
               8000, 8001, 5000, 11434)


def scan_local_servers(http=None, ports=_SCAN_PORTS, host="127.0.0.1"):
    """Probe localhost for running OpenAI-compatible model servers.

    Cheap and honest: /health or /v1/models decides presence; model name
    and ctx come from the same metadata the role probes use. Returns
    [{"base_url", "model", "ctx"}] in port order. A dead port is silence,
    never an error."""
    http = http or _default_http

    def probe_port(port):
        base = f"http://{host}:{port}"
        status, _ = http(f"{base}/health", timeout=1)
        if status != 200:
            status2, _b = http(f"{base}/v1/models", timeout=1)
            if status2 != 200:
                return None
        model = None
        status, body = http(f"{base}/v1/models", timeout=2)
        if status == 200 and isinstance(body, dict):
            data = body.get("data") or []
            if data and isinstance(data[0], dict):
                model = data[0].get("id")
        ctx, _how = _probe_ctx(base, {}, http, body if status == 200 else None)
        return {"base_url": base, "model": model, "ctx": ctx}

    # Parallel: a firewalled DROP port costs its full timeout, and serially
    # that made the scan feel hung (11 ports x 1s worst-case). Results keep
    # port order regardless of completion order.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(ports) or 1)) as ex:
        results = list(ex.map(probe_port, ports))
    return [r for r in results if r]


def _fmt_server(s):
    model = s.get("model") or "(unknown model)"
    ctx = f", ctx {s['ctx']:,}" if s.get("ctx") else ""
    return f"{s['base_url']}  {model}{ctx}"


def _ask_endpoint(label, current_url, found, input_fn, print_fn,
                  fallback="http://127.0.0.1:8080"):
    """base_url question, menu-aware: discovered servers are numbered
    options, a typed URL always works, ENTER takes the default (current
    config first, else the first discovered server, else the fallback)."""
    if not found:
        return _ask(label, current_url or fallback, input_fn)
    for i, s in enumerate(found, 1):
        print_fn(f"      {i}) {_fmt_server(s)}")
    default = current_url or found[0]["base_url"]
    ans = _ask(f"{label} — pick a number or type a URL", default,
               input_fn).strip()
    if ans.isdigit() and 1 <= int(ans) <= len(found):
        return found[int(ans) - 1]["base_url"]
    return ans


# ── Interactive shell ─────────────────────────────────────────────────

def _role_word(w):
    """The role words (MAIN/SUMMARY/ADVISOR) and section headers share one
    highlight color so the intro's vocabulary visibly maps onto the
    screens that follow."""
    return theme.c(theme.AMBER, w, bold=True)


def _section(label, rest=""):
    return "\n── " + _role_word(label) + rest + " ──"


def _version_suffix():
    """' v0.1.0 (c2faf95)' from the engine, lazily — agent imports this
    module, so the import here must stay inside the call, and any failure
    degrades to no suffix rather than a broken header."""
    try:
        import agent as _agent
        ver = getattr(_agent, "__version__", "")
        sha = _agent._git_short_sha()
        out = f" v{ver}" if ver else ""
        if sha:
            out += theme.dim(f" ({sha})")
        return out
    except Exception:
        return ""


def _print_program_header(print_fn):
    """The engine's session banner, reproduced for the wizard (the first-run
    wizard fires BEFORE the real banner, so setup opened colorless)."""
    bar = theme.c(theme.VIOLET, "─" * 60)
    print_fn(bar)
    print_fn(theme.c(theme.SKY, "agent", bold=True) + _version_suffix()
             + theme.dim("  ·  setup"))
    print_fn(bar)


def _ask(prompt, default, input_fn):
    shown = f"{prompt} [{default}]: " if default not in (None, "") else f"{prompt}: "
    ans = input_fn(shown).strip()
    return ans or (default if default is not None else "")


def _fmt_probe(report):
    lines = []
    for key, label in (("p1_reach", "reach"), ("p2_auth", "auth"),
                       ("p3_model", "model"), ("p4_ctx", "ctx")):
        e = report.get(key, {})
        val = e.get("value")
        lines.append(f"    {label:6s} {e.get('status', '?'):10s} "
                     f"{val if val is not None else ''}  {e.get('detail', '')}")
    return "\n".join(lines)


_BEDROCK_MODEL_DEFAULTS = {"main": "claude-v4.6-opus", "summary": "claude-v4.5-haiku"}
_AICOMMONS_EFFORT_DEFAULTS = {"main": "high", "summary": "low"}


def _bedrock_cap_default(role):
    try:
        from llm_backend import _DEFAULT_DAILY_CAPS
        return _DEFAULT_DAILY_CAPS.get(role, 10.00)
    except Exception:
        return {"main": 60.00, "summary": 3.00}.get(role, 10.00)


def _consent_text(estimate_tokens):
    return (f"   ⚠ This endpoint doesn't report its context size. Measuring it sends test\n"
            f"     prompts of up to ~{estimate_tokens:,} tokens in total — several full\n"
            "     context windows. A paid API bills that like any input. Skipping keeps\n"
            "     the default; the agent also recovers from overflow at run time.")


def _consent_asker(input_fn, print_fn):
    def ask(estimate_tokens):
        print_fn(_consent_text(estimate_tokens))
        return _ask("   measure it anyway? (y/n)", "n",
                    input_fn).lower().startswith("y")
    return ask


def _gateway_questions(kind, role, current, input_fn, print_fn):
    """The questions for a gateway kind (bedrock / aicommons / foundry). Returns
    the section dict. Shared by the per-role screens and the welcome flow."""
    if kind == "bedrock":
        section = {"kind": "bedrock"}
        api_url = _ask("gateway api_url (ENTER = env BEDROCK_API_URL / "
                       "bedrock_store)", current.get("api_url", ""), input_fn)
        api_key = _ask("gateway api_key (ENTER = env / store)",
                       current.get("api_key", ""), input_fn)
        model = _ask("model id",
                     current.get("model", _BEDROCK_MODEL_DEFAULTS.get(role, "")),
                     input_fn)
        cap = _ask("daily cost cap USD",
                   str(current.get("daily_cost_cap_usd",
                                   _bedrock_cap_default(role))), input_fn)
        if api_url:
            section["api_url"] = api_url
        if api_key:
            section["api_key"] = api_key
        if model:
            section["model"] = model
        try:
            section["daily_cost_cap_usd"] = float(cap)
        except ValueError:
            print_fn(f"   cap {cap!r} not a number — default kept")
    elif kind == "aicommons":
        section = {"kind": "aicommons"}
        base_url = _ask("API stage URL, ending in /v1 (ENTER = env AICOMMONS_BASE_URL / "
                        "credential store)", current.get("base_url", ""), input_fn)
        api_key = _ask("per-user API key — not a published-bot key (ENTER = env "
                       "AICOMMONS_API_KEY / store)", current.get("api_key", ""), input_fn)
        probe = {k: v for k, v in (("base_url", base_url), ("api_key", api_key)) if v}
        offered = _aicommons_model_list(probe)
        if offered:
            print_fn("   the gateway offers: " + ", ".join(offered))
        model = _ask("model id", current.get("model") or (offered[0] if offered else ""),
                     input_fn)
        effort = _ask("effort (low / medium / high / xhigh / max)",
                      current.get("effort", _AICOMMONS_EFFORT_DEFAULTS.get(role, "high")), input_fn)
        for k, v in (("base_url", base_url), ("api_key", api_key), ("model", model),
                     ("effort", effort)):
            if v:
                section[k] = v
    elif kind == "foundry":
        section = {"kind": "foundry"}
        section["api_url"] = _ask(
            "endpoint url (ENTER = env AZURE_FOUNDRY_ENDPOINT_"
            f"{role.upper()})", current.get("api_url", ""), input_fn)
        api_key = _ask(f"api_key (ENTER = env AZURE_FOUNDRY_API_KEY_"
                       f"{role.upper()})", current.get("api_key", ""), input_fn)
        if api_key:
            section["api_key"] = api_key
        model = _ask("model deployment name (required)",
                     current.get("model", ""), input_fn)
        if not model:
            model = _ask("   a deployment name is required — enter one", "",
                         input_fn)
        section["model"] = model
        if not section["api_url"]:
            section.pop("api_url")
    return section


def _role_screen(role, current, input_fn, print_fn, http, found=None):
    """One role's questions + inline probe. Returns (section_dict, report)."""
    print_fn(_section(role.upper(), " model"))
    cur_kind = {"llamacpp": "1", "bedrock": "2", "foundry": "3", "aicommons": "4"}.get(
        current.get("kind", "llamacpp"), "1")
    kind_ans = _ask("backend kind: 1) llama.cpp / OpenAI-compatible  "
                    "2) AWS Bedrock (legacy published-bot API)  3) Azure (foundry)  "
                    "4) UCSB AI Commons", cur_kind,
                    input_fn).strip()
    kind = {"1": "llamacpp", "2": "bedrock", "3": "foundry", "4": "aicommons"}.get(kind_ans,
                                                                                 "llamacpp")

    if (current.get("kind") or "llamacpp") != kind:
        current = {}        # another kind's URL/key/model are not defaults for this one
    if kind in ("bedrock", "aicommons", "foundry"):
        section = _gateway_questions(kind, role, current, input_fn, print_fn)
    else:
        section = {"kind": "llamacpp"}
        section["base_url"] = _ask_endpoint(
            "base_url", current.get("base_url", ""), found,
            input_fn, print_fn)
        api_key = _ask("api_key (ENTER for none)", current.get("api_key", ""),
                       input_fn)
        if api_key:
            section["api_key"] = api_key
        model = _ask("model (ENTER = auto-pick from /v1/models)",
                     current.get("model", ""), input_fn)
        if model:
            section["model"] = model

    print_fn("   probing…")
    report = probe_backend(section, role=role, http=http,
                           ask_consent=_consent_asker(input_fn, print_fn))
    print_fn(_fmt_probe(report))
    resolved = report.get("p3_model", {}).get("value")
    if resolved:
        section["model"] = resolved
    if section.get("kind") == "llamacpp":
        # legacy blocks imply llamacpp — keep them clean of the redundant key
        section.pop("kind")
    return section, report


def probe_advisor_speed(section, http=None, print_fn=None):
    """Measure the advisor endpoint's real prefill/decode rates.

    The advisor tier is the SLOW tier (CPU-served heavyweight; think minutes,
    not seconds), so the generic ``probe_throughput`` — 64 tokens, 300 s cap —
    would itself time out. This probe is sized for it: two small timed
    completions (short prompt, then a ~600-token prompt, 24 output tokens
    each, generous timeout). Server ``timings`` are preferred when echoed;
    otherwise the two walls are differenced to separate prefill from decode.

    Returns ``(measured_dict_or_None, notes_list)``. ``measured`` carries
    ``prefill_pos_per_s``, ``decode_tok_per_s``, ``probed_at``, ``detail``.
    Never raises.
    """
    import time as _time
    http = http or _default_http
    print_fn = print_fn or (lambda *_: None)
    base = (section.get("base_url") or "").rstrip("/")
    hdrs = _auth_headers(section.get("api_key"))

    def _one(prompt, max_tokens):
        req = {"messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0.0}
        if section.get("model"):
            req["model"] = section["model"]
        extra = section.get("extra_body")
        if isinstance(extra, dict):
            req.update(extra)
        t0 = _time.monotonic()
        status, body = http(f"{base}/v1/chat/completions", data=req,
                            headers=hdrs, timeout=1800)
        wall = _time.monotonic() - t0
        if status != 200 or not isinstance(body, dict):
            return None
        usage = body.get("usage") or {}
        msg = ((body.get("choices") or [{}])[0].get("message") or {})
        return {"wall": wall,
                "prompt": int(usage.get("prompt_tokens") or 0),
                "out": int(usage.get("completion_tokens") or max_tokens),
                "timings": body.get("timings") or {},
                "reasoning": bool(msg.get("reasoning_content"))}

    ask = "Reply with the single word: ready."
    filler = " The quick brown fox jumps over the lazy dog." * 60  # ~560 tok
    print_fn("   measuring advisor speed — probe 1/2 (short; may take a "
             "minute or two)…")
    a = _one(ask, 24)
    if a is None:
        return None, ["advisor speed probe failed (endpoint error) — "
                      "keeping conservative fallback rates"]
    reasoning_notes = []
    if a.get("reasoning") and not (section.get("extra_body") or {}) \
            .get("chat_template_kwargs"):
        reasoning_notes.append(
            'advisor is a REASONING model (emits reasoning_content) — on hard '
            'questions its thinking can consume the whole max_tokens budget '
            'and return no answer (a one-shot larger retry is built in, but '
            'the reliable fix is config): consider advisor.extra_body = '
            '{"chat_template_kwargs": {"enable_thinking": false}} '
            '(llama-server) for decisive, budget-respecting answers')
    # Server-side timings settle it in one probe.
    t = a["timings"]
    if t.get("prompt_per_second") and t.get("predicted_per_second"):
        measured = {"prefill_pos_per_s": round(float(t["prompt_per_second"]), 2),
                    "decode_tok_per_s": round(float(t["predicted_per_second"]), 3),
                    "probed_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                _time.gmtime()),
                    "detail": "server timings"}
        return measured, [f"advisor speed (server timings): "
                          f"{measured['prefill_pos_per_s']} pos/s prefill · "
                          f"{measured['decode_tok_per_s']} tok/s decode"] \
            + reasoning_notes
    print_fn(f"   probe 1 done ({a['wall']:.0f}s) — probe 2/2 (long prompt; "
             "may take several minutes)…")
    b = _one(ask + filler, 24)
    if b is None or b["prompt"] <= a["prompt"] or b["wall"] <= a["wall"]:
        # Can't difference — attribute probe 1 entirely to decode (conservative).
        dec = round(a["out"] / max(a["wall"], 1e-3), 3)
        measured = {"prefill_pos_per_s": None, "decode_tok_per_s": dec,
                    "probed_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                _time.gmtime()),
                    "detail": "single wall-clock probe (prefill unmeasured)"}
        return measured, [f"advisor decode ≈ {dec} tok/s (wall clock; prefill "
                          "UNMEASURED — fallback rate kept for it)"] \
            + reasoning_notes
    pp = round((b["prompt"] - a["prompt"]) / (b["wall"] - a["wall"]), 2)
    # Attribute probe 1's wall minus its own prefill to decode + fixed overhead;
    # folding overhead into decode is conservative in the safe direction.
    dec_wall = max(a["wall"] - (a["prompt"] / max(pp, 0.1)), 1e-3)
    dec = round(a["out"] / dec_wall, 3)
    measured = {"prefill_pos_per_s": pp, "decode_tok_per_s": dec,
                "probed_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                "detail": (f"two-probe wall clock "
                           f"({a['wall']:.0f}s / {b['wall']:.0f}s)")}
    return measured, [f"advisor speed (two-probe): {pp} pos/s prefill · "
                      f"{dec} tok/s decode"] + reasoning_notes


def calibrate_advisor(user_adv_cfg, measured):
    """Derive the advisor's context/timeout knobs from measured speeds.

    Mirrors ``calibrate()``'s contract: user-set values are never overridden
    (only absent knobs are filled with defaults), and the derived
    ``timeout_s`` comes from the same ``derive_timeout_s`` the tool itself
    uses at call time — the wizard writes the number the tool would derive,
    so config stays explicit and inspectable.

    Returns ``(updates, notes)`` where ``updates`` is the advisor-section
    delta (measured rates + derived timeout + any filled defaults).
    """
    try:
        from tools.consult_advisor import derive_timeout_s, _DEFAULTS as _ADV_D
    except Exception:  # tool not importable (moved/renamed) — no derivation
        return {}, ["consult_advisor not importable — advisor calibration "
                    "skipped"]
    updates, notes = {}, []
    if measured:
        updates["measured"] = {k: v for k, v in measured.items()
                               if v is not None}
    merged = dict(user_adv_cfg or {})
    for key in ("prefill_token_budget", "max_tokens"):
        if key not in merged:
            merged[key] = _ADV_D[key]
            updates[key] = _ADV_D[key]
            notes.append(f"advisor {key} = {_ADV_D[key]} (default, now "
                         "explicit)")
    merged.update(updates)
    if not (user_adv_cfg or {}).get("timeout_s"):
        merged.pop("timeout_s", None)
        derived = derive_timeout_s(merged)
        updates["timeout_s"] = derived
        notes.append(f"advisor timeout_s = {derived} "
                     f"({derived / 60:.0f} min — derived from budgets + "
                     f"{'measured' if measured else 'fallback'} speeds; a full "
                     "consult now fits inside it)")
    else:
        notes.append(f"advisor timeout_s = {user_adv_cfg['timeout_s']} "
                     "(user-set — kept)")
    return updates, notes


def _advisor_screen(current, input_fn, print_fn, http, found=None):
    """Advisor endpoint (the escalation-tier tool agent.py probes at boot).

    Optional: ENTER on an empty URL skips/disables. A failing probe writes
    enabled=false WITH the reason — matching the boot probe's behavior —
    rather than persisting a dead endpoint as live."""
    print_fn(_section("ADVISOR", " endpoint (optional — feeds the advisor tool)"))
    cur = current.get("base_url", "")
    if found:
        for i, s in enumerate(found, 1):
            print_fn(f"      {i}) {_fmt_server(s)}")
    pick = " pick a number," if found else ""
    if cur:
        raw = input_fn(f"advisor base_url [{cur}] ({pick.strip() + ' ' if pick else ''}"
                       "ENTER = keep as-is, '-' = disable): ").strip()
        if raw == "":
            return None          # keep existing config untouched
    else:
        raw = input_fn(f"advisor base_url ({pick.strip() + ' ' if pick else ''}"
                       "ENTER = skip, '-' = disable): ").strip()
        if raw == "":
            return None          # never configured, nothing to write
    if raw == "-":
        print_fn("   advisor disabled")
        return {"enabled": False}
    if found and raw.isdigit() and 1 <= int(raw) <= len(found):
        raw = found[int(raw) - 1]["base_url"]
    base_url = raw
    api_key = _ask("advisor api_key (ENTER for none)",
                   current.get("api_key", ""), input_fn)
    print_fn("   probing…")
    report = probe_llamacpp(base_url, api_key or None, http=http,
                            allow_empirical=False)
    print_fn(_fmt_probe(report))
    section = {"base_url": base_url}
    if api_key:
        section["api_key"] = api_key
    if report["p1_reach"]["status"] == OK:
        section["enabled"] = True
        resolved = report.get("p3_model", {}).get("value")
        if resolved:
            section["model"] = resolved
        cal = _ask("measure advisor speed and derive its context budget + "
                   "timeout now? (y/n; two slow probes, minutes on a CPU "
                   "advisor)", "n", input_fn)
        if cal.lower().startswith("y"):
            measured, notes = probe_advisor_speed(section, http=http,
                                                  print_fn=print_fn)
            merged_user = {**(current or {}), **section}
            adv_updates, cal_notes = calibrate_advisor(merged_user, measured)
            for n in notes + cal_notes:
                print_fn(f"    {n}")
            section.update(adv_updates)
        else:
            print_fn("   skipped — run /setup calibrate any time to measure "
                     "and derive the advisor knobs")
    else:
        section["enabled"] = False
        section["disabled_reason"] = report["p1_reach"]["detail"]
        print_fn("   unreachable — written with enabled=false + reason "
                 "(re-run /setup advisor when it is up)")
    return section


# ── Welcome flow (the full /setup and first-run path) ─────────────────
#
# "Hi, let's get you connected": pick where the model is, pick the server or
# model, then optional extras, all unticked except the free tool-call check on a
# local server. The quick path is three keypresses and sends only free GETs.

_WELCOME_KINDS = (
    ("llamacpp", "OpenAI-compatible server", "local llama.cpp / Ollama / vLLM, or a hosted API"),
    ("aicommons", "UCSB AI Commons", "Claude through your AI Commons key"),
    ("foundry", "Azure AI Foundry", ""),
    ("bedrock", "More…", "legacy AWS Bedrock published-bot gateway"),
)

_MODEL_RADIO_MAX = 15


def _normalize_base_url(url):
    """The llamacpp backend appends /v1/... itself; people paste URLs ending in /v1."""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    if url and "://" not in url:
        url = "http://" + url
    return url


def _is_local_url(url):
    """Loopback or a private-network address: probing it costs nothing."""
    import ipaddress
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".local")
    return ip.is_loopback or ip.is_private


def _free_to_probe(section):
    """True when completions against this section cost nothing: a llamacpp-kind
    endpoint on this machine or the local network."""
    return ((section.get("kind") or "llamacpp") == "llamacpp"
            and _is_local_url(section.get("base_url") or ""))


def _list_models(base, api_key, http):
    status, body = http(f"{base}/v1/models", headers=_auth_headers(api_key))
    if status == 200 and isinstance(body, dict):
        return [m.get("id") for m in body.get("data", []) if isinstance(m, dict) and m.get("id")]
    return []


def _aicommons_saved_credentials():
    """(source description) when AI Commons credentials already exist, else None."""
    if os.environ.get("AICOMMONS_BASE_URL") and os.environ.get("AICOMMONS_API_KEY"):
        return "AICOMMONS_BASE_URL / AICOMMONS_API_KEY"
    try:
        import bedrock_store
        _url, _key, name = bedrock_store.select_credentials(api=bedrock_store.API_MESSAGES)
        return f"the credential store entry '{name}'"
    except Exception:
        return None


def _connect_openai(role, ui, found, current, http):
    """Server radio (found servers + another URL), key only when asked for, model
    radio when the server lists several. Free GETs only."""
    cur_url = _normalize_base_url(current.get("base_url", ""))
    options = [(s["base_url"], " · ".join(x for x in (
        s.get("model") or "", f"{s['ctx']:,} ctx" if s.get("ctx") else "") if x))
        for s in found]
    options.append(("Another URL…", "a hosted API, or a server on another machine"))
    if found:
        default = next((i for i, s in enumerate(found) if s["base_url"] == cur_url), 0)
        if cur_url and not any(s["base_url"] == cur_url for s in found):
            default = len(found)
        idx = ui.radio("Found running locally:", options, default)
    else:
        ui.say("No local model servers found on the usual ports.")
        idx = len(found)
    if idx < len(found):
        base, model, key = found[idx]["base_url"], found[idx].get("model"), None
    else:
        base = _normalize_base_url(ui.text(
            "Server URL (e.g. https://api.openai.com/v1 or http://host:8080)",
            cur_url or ("" if found else "http://127.0.0.1:8080")))
        model, key = None, current.get("api_key") or None
    report = probe_llamacpp(base, key, model, http=http, allow_empirical=False)
    if report["p1_reach"]["status"] == OK and (
            report["p2_auth"]["status"] == FAILED
            or (not _is_local_url(base) and not key and report["p3_model"]["status"] != OK)):
        key = ui.text("API key", "", secret=True) or None
        report = probe_llamacpp(base, key, model, http=http, allow_empirical=False)
    if not model and report["p1_reach"]["status"] == OK:
        models = _list_models(base, key, http)
        cur_model = current.get("model")
        if len(models) > 1:
            shown = models[:_MODEL_RADIO_MAX]
            opts = [(m, "") for m in shown] + ([("Type a model id…", f"{len(models)} available")]
                                               if len(models) > _MODEL_RADIO_MAX else [])
            default = shown.index(cur_model) if cur_model in shown else 0
            pick = ui.radio("Which model?", opts, default)
            model = shown[pick] if pick < len(shown) else ui.text("Model id", cur_model or "")
        elif models:
            model = models[0]
        if model:
            report = probe_llamacpp(base, key, model, http=http, allow_empirical=False)
    section = {"kind": "llamacpp", "base_url": base}
    if key:
        section["api_key"] = key
    resolved = model or report.get("p3_model", {}).get("value")
    if resolved:
        section["model"] = resolved
    return section, report


def _connect_aicommons(role, ui, current, http):
    saved = _aicommons_saved_credentials()
    section = {"kind": "aicommons"}
    if saved and not current.get("base_url"):
        ui.say(f"Using your AI Commons key from {saved}.")
    else:
        url = ui.text("AI Commons API URL (ends in /v1)", current.get("base_url", ""))
        key = ui.text("Your AI Commons API key", "", secret=True) or current.get("api_key", "")
        if url:
            section["base_url"] = url.rstrip("/")
        if key:
            section["api_key"] = key
    ids = _aicommons_model_list(section)
    want = current.get("model")
    if not ids:
        ui.say("Couldn't list the gateway's models (check the URL and key).")
        if want:
            section["model"] = want
        else:
            typed = ui.text("Model id", "")
            if typed:
                section["model"] = typed
    else:
        pick = ui.radio("Which model?", [(m, _aicommons_hint(m)) for m in ids],
                        ids.index(want) if want in ids else 0)
        section["model"] = ids[pick]
    return section, probe_gateway_backend(section, role, http=http, ask_consent=None)


def _aicommons_model_list(section):
    """The gateway's models, ranked, aliases left out; [] when it can't be asked
    (no credentials, unreachable, key refused). A free GET /v1/models."""
    try:
        import llm_backend
        return llm_backend.build_backend({"kind": "aicommons", "role": "main",
                                          **section}).model_choices()
    except Exception:
        return []


def _aicommons_hint(model_id):
    """What the agent knows about a model, from its table: context and thinking."""
    try:
        import llm_backend
        if model_id not in llm_backend.AICOMMONS_MODELS:
            return "untested here — 128k ctx and no thinking assumed"
        info = llm_backend.aicommons_model_info(model_id)
    except Exception:
        return ""
    ctx = info.get("context_tokens") or 0
    parts = [f"{ctx // 1000:,}k ctx" if ctx < 1_000_000 else f"{ctx // 1_000_000}M ctx"]
    if info.get("reasoning"):
        parts.append("thinking")
    return " · ".join(parts)


def _connect(role, ui, found, current, http, kind=None):
    """One role's connection: kind radio (unless given), then that kind's screen.
    Returns (section, report)."""
    if kind is None:
        cur_kind = current.get("kind", "llamacpp")
        kinds = [k for k, _l, _h in _WELCOME_KINDS]
        idx = ui.radio("Where's your model?",
                       [(label, hint) for _k, label, hint in _WELCOME_KINDS],
                       kinds.index(cur_kind) if cur_kind in kinds else
                       (1 if (not found and _aicommons_saved_credentials()) else 0))
        kind = kinds[idx]
    if (current.get("kind") or "llamacpp") != kind:
        current = {}        # another kind's URL/key/model are not defaults for this one
    if kind == "llamacpp":
        return _connect_openai(role, ui, found, current, http)
    if kind == "aicommons":
        return _connect_aicommons(role, ui, current, http)
    if kind == "bedrock":
        ui.say(theme.dim("The legacy Bedrock kind uses a published bot's /conversation API: "
                         "no native tools, no streaming. For AI Commons pick UCSB AI Commons."))
    section = _gateway_questions(kind, role, current, ui.input_fn, ui.say)
    return section, probe_gateway_backend(section, role, http=http, ask_consent=None)


def _connected_line(section, report):
    p1 = report.get("p1_reach", {})
    if p1.get("status") != OK:
        return False, f"✗ Couldn't connect: {p1.get('detail', 'unknown error')}"
    if report.get("p2_auth", {}).get("status") == FAILED:
        return False, f"✗ The server refused the key: {report['p2_auth'].get('detail', '')}"
    where = section.get("base_url") or section.get("api_url") or {
        "aicommons": "AI Commons", "foundry": "Azure", "bedrock": "Bedrock"}.get(
        section.get("kind"), "the endpoint")
    model = section.get("model") or report.get("p3_model", {}).get("value") or "(server default)"
    p4 = report.get("p4_ctx", {})
    ctx = (f"context {p4['value']:,} ({p4.get('detail', 'reported')})" if p4.get("value")
           else "context not reported (default kept)")
    return True, f"✓ Connected: {model} at {where}, {ctx}"


def _shared_summary(main_section):
    """The summary role when the user didn't ask for a separate one."""
    out = {"enabled": True}
    for key in ("kind", "base_url", "api_url", "api_key", "model"):
        if main_section.get(key):
            out[key] = main_section[key]
    return out


def _measure_ctx_screen(section, report, ui, http):
    """The opt-in context measurement: warn with the real worst case, default to
    skipping, allow typing a known size instead. Mutates ``report['p4_ctx']``."""
    kind = section.get("kind", "llamacpp")
    ui.say(_consent_text(empirical_worst_case_tokens()))
    pick = ui.radio("Context window:", [
        ("Skip", "keep the default"),
        ("Enter the size", "if you know it, e.g. 131072"),
        ("Measure it", "sends the test prompts above")], 0)
    if pick == 1:
        raw = ui.text("Context size in tokens", "").replace(",", "").replace("_", "")
        if raw.isdigit() and int(raw) >= 1024:
            report["p4_ctx"] = {"status": OK, "value": int(raw), "detail": "entered by you"}
        else:
            ui.say("   not a token count — default kept")
    elif pick == 2:
        ui.say("   measuring…")
        if kind == "llamacpp":
            val, how = _probe_ctx_empirical(section["base_url"].rstrip("/"),
                                            _auth_headers(section.get("api_key")),
                                            http, section.get("model"))
            report["p4_ctx"] = ({"status": OK, "value": int(val), "detail": how} if val
                                else {"status": UNMEASURED, "value": None, "detail": how})
        else:
            fresh = probe_gateway_backend(section, "main", http=http,
                                          ask_consent=lambda est: True)
            report["p4_ctx"] = fresh.get("p4_ctx", report.get("p4_ctx", {}))
        p4 = report["p4_ctx"]
        ui.say(f"   {p4.get('value') or 'unmeasured'} ({p4.get('detail', '')})")


def run_welcome(config_path, effective_cfg=None, input_fn=None, print_fn=None,
                http=None, ui=None):
    """The full setup: connect, optional extras, write config. Returns the updates
    written ({} if nothing was)."""
    from wizard_ui import make_ui
    ui = ui or make_ui(input_fn, print_fn)
    print_fn = ui.say
    effective_cfg = effective_cfg or {}
    try:
        loaded = json.loads(Path(config_path).read_text(encoding="utf-8", errors="replace"))
        current_cfg = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        current_cfg = {}

    _print_program_header(print_fn)
    print_fn("Hi, let's get you connected.\n")
    found = scan_local_servers(http=http)

    for attempt in range(3):
        section, report = _connect("main", ui, found, current_cfg.get("llm", {}), http)
        ok, line = _connected_line(section, report)
        print_fn(line)
        if ok or attempt == 2:
            break
        # ENTER retries once; after that it keeps what was entered, so a
        # script that runs out of answers can't loop here.
        nxt = ui.radio("What now?", [("Try again", ""),
                                     ("Save it anyway", "fix the server later; /setup test re-checks"),
                                     ("Cancel setup", "nothing is written")], min(attempt, 1))
        if nxt == 1:
            break
        if nxt == 2:
            print_fn("setup cancelled — nothing written")
            return {}

    kind = section.get("kind", "llamacpp")
    local = _free_to_probe(section)
    ctx_known = bool(report.get("p4_ctx", {}).get("value"))

    extras = [("summary", "Use a separate SUMMARY model",
               "default: the same model"),
              ("advisor", "Add an ADVISOR model", "a bigger model the agent can consult")]
    if kind == "llamacpp" and ok:       # both need a server that answers
        extras.append(("speed", "Check speed and tool calling",
                       "4 short requests" + ("" if local else " — billed on a paid API")))
    if ok and not ctx_known and kind in ("llamacpp", "foundry"):
        extras.append(("ctx", "Measure the context window", "⚠ can send millions of tokens"))
    extras += [("claim", "Turn on the claim guard", "for coding projects"),
               ("agent", "Set up this folder as an agent", "git repo + AGENT.md")]
    keys = [k for k, _l, _h in extras]
    claim_on = int((effective_cfg.get("cycle") or {}).get("claim_trace_max_blocks", 0) or 0) > 0
    default_on = {keys.index(k) for k in keys
                  if (k == "speed" and local) or (k == "claim" and claim_on)}
    picked = {keys[i] for i in ui.checkboxes("Anything else? (all optional)",
                                             [(label, hint) for _k, label, hint in extras],
                                             default_on)}

    updates = {"llm": section}
    if "summary" in picked:
        print_fn(_section("SUMMARY", " model"))
        s_sec, s_rep = _connect("summary", ui, found, current_cfg.get("summary", {}), http)
        s_sec["enabled"] = True
        updates["summary"] = s_sec
        print_fn(_connected_line(s_sec, s_rep)[1])
    else:
        updates["summary"] = _shared_summary(section)

    if "advisor" in picked:
        adv = _advisor_screen(current_cfg.get("advisor", {}), ui.input_fn, print_fn, http,
                              found=found)
        if adv is not None:
            updates["advisor"] = adv

    throughput = caps = None
    if "speed" in picked:
        print_fn(_section("SPEED AND TOOLS"))
        throughput = probe_throughput(section, "main", http=http)
        if throughput.get("status") == OK:
            pt = throughput.get("prompt_tps")
            print_fn(f"    speed  {throughput['gen_tps']} t/s gen"
                     + (f" · {pt} t/s prompt" if pt else "") + f"  ({throughput['detail']})")
        caps = probe_capabilities(section, "main", http=http, throughput=throughput)
        for name in ("tools_accepted", "tool_roundtrip"):
            e = caps.get(name, {})
            print_fn(f"    {name:16s} {e.get('status', '?'):10s} {e.get('detail', '')}")
        for r in recommend(report, throughput=throughput, caps=caps):
            print_fn(f"    • {r}")

    if "ctx" in picked:
        print_fn(_section("CONTEXT WINDOW"))
        _measure_ctx_screen(section, report, ui, http)

    cal_updates, _notes = calibrate(report, effective_cfg)
    updates.update(cal_updates)

    # Claim guard: blocks a "tests pass / committed" CLAIM with no matching tool
    # call. Right for coding; story prose can false-positive, so it stays opt-in.
    if "claim" in picked:
        updates.setdefault("cycle", {})["claim_trace_max_blocks"] = 2
    elif claim_on:
        updates.setdefault("cycle", {})["claim_trace_max_blocks"] = 0

    _replace_switched_roles(config_path, current_cfg, updates)
    path = write_config(config_path, updates)
    model = section.get("model") or "the server's model"
    ctx = report.get("p4_ctx", {}).get("value")
    print_fn(f"\nSaved {path}: {model}" + (f", context {ctx:,}" if ctx else "")
             + ". Change it any time with /setup.")

    if "agent" in picked:
        try:
            import agent_scaffold
            agent_scaffold.run_agent_wizard(cwd=Path(config_path).parent.parent,
                                            input_fn=ui.input_fn, print_fn=print_fn)
        except (KeyboardInterrupt, EOFError):
            print_fn("\nagent scaffold aborted — setup result kept")
    return updates


def run_wizard(config_path, effective_cfg=None, jump_to=None,
               input_fn=None, print_fn=None, http=None):
    """Interactive setup. Returns the updates dict written (or {} if aborted).

    ``jump_to``: None = the full welcome flow (``run_welcome``);
    "main"/"summary"/"advisor" = that section only; "calibrate" = probes +
    calibration against the current config only (with drift reporting against
    the _calibrated sidecar).

    Question defaults come from the USER's file at ``config_path`` — NOT
    from ``effective_cfg`` (the defaults-merged runtime config), whose
    shipped placeholder model names would otherwise present as keepable
    answers and poison the auto-pick (caught live 2026-08-15: first run
    wrote the shipped placeholder model instead of the server's real model).
    ``effective_cfg`` is used only where effective values are the right
    baseline: calibration's keep-default rules and the drift sidecar.
    """
    if jump_to is None:
        return run_welcome(config_path, effective_cfg, input_fn=input_fn,
                           print_fn=print_fn, http=http)
    # Call-time I/O resolution (def-time defaults bind a possibly-patched
    # builtins.input at import — see agent_scaffold.run_agent_wizard).
    input_fn = input_fn or input
    print_fn = print_fn or print
    effective_cfg = effective_cfg or {}
    current_cfg = {}
    try:
        loaded = json.loads(Path(config_path).read_text(encoding="utf-8",
                                                        errors="replace"))
        if isinstance(loaded, dict):
            current_cfg = loaded
    except (OSError, json.JSONDecodeError, ValueError):
        current_cfg = {}
    updates = {}
    main_report = None
    main_throughput = None
    main_caps = None

    if jump_to == "main":
        section, main_report = _role_screen(
            "main", current_cfg.get("llm", {}), input_fn, print_fn, http)
        updates["llm"] = section
        # P5/P6 only where they're free: a local server. On a hosted API they
        # are billed completions; /setup test and the welcome flow's opt-in
        # "Check speed and tool calling" cover that case.
        if _free_to_probe(section):
            main_throughput = probe_throughput(section, "main", http=http)
            if main_throughput.get("status") == OK:
                pt = main_throughput.get("prompt_tps")
                print_fn(f"    speed  {main_throughput['gen_tps']} t/s gen"
                         + (f" · {pt} t/s prompt" if pt else "")
                         + f"  ({main_throughput['detail']})")
            main_caps = probe_capabilities(section, "main", http=http,
                                           throughput=main_throughput)
            for name in ("timings", "tools_accepted", "tool_roundtrip",
                         "reasoning_param"):
                e = main_caps.get(name, {})
                print_fn(f"    cap    {name:16s} {e.get('status', '?'):10s} "
                         f"{e.get('detail', '')}")

    if jump_to == "summary":
        section, _ = _role_screen("summary", current_cfg.get("summary", {}),
                                  input_fn, print_fn, http)
        section["enabled"] = True
        updates["summary"] = section

    if jump_to == "advisor":
        adv = _advisor_screen(current_cfg.get("advisor", {}),
                              input_fn, print_fn, http)
        if adv is not None:
            updates["advisor"] = adv

    # Calibration rides on the MAIN endpoint's measurements.
    if jump_to in ("main", "calibrate"):
        if main_report is None:
            llm = (updates.get("llm") or current_cfg.get("llm") or {})
            if llm.get("base_url") or llm.get("api_url") or llm.get("kind"):
                print_fn("   probing main endpoint for calibration…")
                main_report = probe_backend(
                    llm, role="main", http=http,
                    ask_consent=_consent_asker(input_fn, print_fn))
                print_fn(_fmt_probe(main_report))
            else:
                main_report = {}
        # Drift report (Phase 3): fresh measurement vs _calibrated sidecar.
        drifted, drift_msg = check_drift(
            current_cfg if current_cfg.get('_calibrated') else effective_cfg,
            main_report)
        if drifted is not None or jump_to == "calibrate":
            print_fn(f"    drift: {drift_msg}")
        cal_updates, notes = calibrate(main_report, effective_cfg)
        print_fn(_section("CALIBRATION"))
        for n in notes:
            print_fn(f"    {n}")
        # Advisor calibration (calibrate-only runs; `/setup advisor` calibrates
        # inside the advisor screen). Same measure-then-derive as the main
        # calibration, against the SLOW tier's own probe — the generic
        # 64-token/300 s throughput probe would itself time out on a CPU advisor.
        if jump_to == "calibrate":
            adv_cfg = (updates.get("advisor")
                       or current_cfg.get("advisor") or {})
            adv_enabled = adv_cfg.get("enabled",
                                      bool(adv_cfg.get("base_url")))
            if adv_enabled and adv_cfg.get("base_url"):
                measured, adv_notes = probe_advisor_speed(
                    adv_cfg, http=http, print_fn=print_fn)
                adv_updates, adv_cal_notes = calibrate_advisor(adv_cfg,
                                                               measured)
                for n in adv_notes + adv_cal_notes:
                    print_fn(f"    {n}")
                if adv_updates:
                    cal_updates["advisor"] = {**adv_cfg, **adv_updates}
            elif adv_cfg:
                print_fn("    advisor: configured but disabled — speed "
                         "calibration skipped")
        if cal_updates:
            accept = _ask("apply these derived values? (y/n)", "y", input_fn)
            if accept.lower().startswith("y"):
                updates.update(cal_updates)
            else:
                print_fn("    calibration skipped — defaults kept")

    # Recommendations (Phase 3): printed, never silently applied.
    recs = recommend(main_report or {}, throughput=main_throughput,
                     caps=main_caps,
                     main_section=updates.get("llm") or current_cfg.get("llm"),
                     summary_section=updates.get("summary")
                     or current_cfg.get("summary"))
    if recs:
        print_fn(_section("RECOMMENDATIONS"))
        for r in recs:
            print_fn(f"    • {r}")

    if not updates:
        print_fn("nothing to write.")
        return {}
    _replace_switched_roles(config_path, current_cfg, updates)
    path = write_config(config_path, updates)
    print_fn(f"\nwrote {path}")
    return updates


def run_probe_report(current_cfg, print_fn=None, http=None):
    """`/setup test` — probe the configured endpoints, change nothing.

    Spend-free by contract: no consent path is offered, so metered
    empirical probes can never fire from test mode (their absence reports
    as UNMEASURED, which is the honest state)."""
    print_fn = print_fn or print
    any_run = False
    for role, section in (("main", current_cfg.get("llm", {})),
                          ("summary", current_cfg.get("summary", {})),
                          ("advisor", current_cfg.get("advisor", {}))):
        target = section.get("base_url") or section.get("api_url")
        if not target and not section.get("kind"):
            print_fn(f"{role}: nothing configured — skipped")
            continue
        any_run = True
        print_fn(f"{role}: {section.get('kind', 'llamacpp')} @ {target or '(env creds)'}")
        report = probe_backend(section, role=role, http=http,
                               ask_consent=None, allow_empirical=False)
        print_fn(_fmt_probe(report))
        # P5/P6 only where free (a local server) — test mode never spends,
        # and a hosted OpenAI-compatible API is the llamacpp kind too.
        if role == "main" and _free_to_probe(section):
            tp = probe_throughput(section, role, http=http)
            if tp.get("status") == OK:
                print_fn(f"    speed  {tp['gen_tps']} t/s gen ({tp['detail']})")
            caps = probe_capabilities(section, role, http=http, throughput=tp)
            for name in ("timings", "tools_accepted", "tool_roundtrip",
                         "reasoning_param"):
                e = caps.get(name, {})
                print_fn(f"    cap    {name:16s} {e.get('status', '?'):10s} "
                         f"{e.get('detail', '')}")
            drifted, msg = check_drift(current_cfg, report)
            if drifted is not None:
                print_fn(f"    drift: {msg}")
            for r in recommend(report, throughput=tp, caps=caps,
                               main_section=section,
                               summary_section=current_cfg.get("summary")):
                print_fn(f"    • {r}")
    if not any_run:
        print_fn("no endpoints configured — run /setup first")
