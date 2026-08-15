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
                   allow_empirical=True):
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

    # P4: max context — /props → /slots → /v1/models metadata → empirical
    ctx, how = _probe_ctx(base, hdrs, http, body if status == 200 else None)
    if ctx:
        report["p4_ctx"] = {"status": OK, "value": int(ctx), "detail": how}
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
        estimate = _EMPIRICAL_CAP_TOKENS * 2  # worst-case total prompt tokens
        if ask_consent(estimate):
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
                              http=http, allow_empirical=allow_empirical)
    return probe_gateway_backend(section, role, http=http,
                                 ask_consent=ask_consent)


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

    c["summary_threshold"] = int(ctx_chars * 0.6)
    notes.append(f"context.summary_threshold = {c['summary_threshold']} chars (~60% of budget)")

    # Scale message-count cap with ctx: ~20 msgs at 8k … ~400 at 192k, i.e.
    # ~1 message per 480 tokens, clamped to a sane band.
    msgs = max(20, min(500, budget // 480))
    c["max_context_messages"] = msgs
    notes.append(f"context.max_context_messages = {msgs} (~1 per 480 tokens)")

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

    updates["_calibrated"] = {
        "measured_ctx_tokens": ctx_tokens,
        "ctx_source": p4.get("detail", "?"),
        "chars_per_token": chars_per_token,
    }
    return updates, notes


# ── Config writing ────────────────────────────────────────────────────


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


# ── Interactive shell ─────────────────────────────────────────────────


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


def _bedrock_cap_default(role):
    try:
        from llm_backend import _DEFAULT_DAILY_CAPS
        return _DEFAULT_DAILY_CAPS.get(role, 10.00)
    except Exception:
        return {"main": 60.00, "summary": 3.00}.get(role, 10.00)


def _consent_asker(input_fn, print_fn):
    def ask(estimate_tokens):
        print_fn(f"   ⚠ empirical context probe sends up to ~{estimate_tokens:,} "
                 "prompt tokens to a METERED endpoint (billed at your input "
                 "rate).")
        return _ask("   run the metered probe? (y/n)", "n",
                    input_fn).lower().startswith("y")
    return ask


def _role_screen(role, current, input_fn, print_fn, http):
    """One role's questions + inline probe. Returns (section_dict, report)."""
    print_fn(f"\n── {role.upper()} model ──")
    cur_kind = {"llamacpp": "1", "bedrock": "2", "foundry": "3"}.get(
        current.get("kind", "llamacpp"), "1")
    kind_ans = _ask("backend kind: 1) llama.cpp / OpenAI-compatible  "
                    "2) AWS Bedrock  3) Azure (foundry)", cur_kind,
                    input_fn).strip()
    kind = {"1": "llamacpp", "2": "bedrock", "3": "foundry"}.get(kind_ans,
                                                                 "llamacpp")

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
    else:
        section = {"kind": "llamacpp"}
        section["base_url"] = _ask(
            "base_url", current.get("base_url", "http://127.0.0.1:8080"),
            input_fn)
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


def run_wizard(config_path, current_cfg, jump_to=None,
               input_fn=input, print_fn=print, http=None):
    """Interactive setup. Returns the updates dict written (or {} if aborted).

    ``jump_to``: None = full run; "main"/"summary" = that role only;
    "calibrate" = probes + calibration against the current config only.
    """
    current_cfg = current_cfg or {}
    updates = {}
    main_report = None

    if jump_to in (None, "main"):
        section, main_report = _role_screen(
            "main", current_cfg.get("llm", {}), input_fn, print_fn, http)
        updates["llm"] = section

    if jump_to in (None, "summary"):
        cur = current_cfg.get("summary", {})
        if jump_to is None:
            reuse = _ask("\nSUMMARY: reuse the main endpoint? (y/n)", "y", input_fn)
            if reuse.lower().startswith("y"):
                base = updates.get("llm") or current_cfg.get("llm", {})
                summary = {"enabled": True}
                for key in ("kind", "base_url", "api_url", "api_key", "model"):
                    if base.get(key):
                        summary[key] = base[key]
                updates["summary"] = summary
                print_fn(f"   summary -> "
                         f"{summary.get('base_url') or summary.get('api_url')} "
                         f"(shared)")
            else:
                section, _ = _role_screen("summary", cur, input_fn, print_fn, http)
                section["enabled"] = True
                updates["summary"] = section
        else:
            section, _ = _role_screen("summary", cur, input_fn, print_fn, http)
            section["enabled"] = True
            updates["summary"] = section

    # Calibration rides on the MAIN endpoint's measurements.
    if jump_to in (None, "main", "calibrate"):
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
        cal_updates, notes = calibrate(main_report, current_cfg)
        print_fn("\n── CALIBRATION ──")
        for n in notes:
            print_fn(f"    {n}")
        if cal_updates:
            accept = _ask("apply these derived values? (y/n)", "y", input_fn)
            if accept.lower().startswith("y"):
                updates.update(cal_updates)
            else:
                print_fn("    calibration skipped — defaults kept")

    if not updates:
        print_fn("nothing to write.")
        return {}
    path = write_config(config_path, updates)
    print_fn(f"\nwrote {path}")
    return updates


def run_probe_report(current_cfg, print_fn=print, http=None):
    """`/setup test` — probe the configured endpoints, change nothing.

    Spend-free by contract: no consent path is offered, so metered
    empirical probes can never fire from test mode (their absence reports
    as UNMEASURED, which is the honest state)."""
    any_run = False
    for role, section in (("main", current_cfg.get("llm", {})),
                          ("summary", current_cfg.get("summary", {}))):
        target = section.get("base_url") or section.get("api_url")
        if not target and not section.get("kind"):
            print_fn(f"{role}: nothing configured — skipped")
            continue
        any_run = True
        print_fn(f"{role}: {section.get('kind', 'llamacpp')} @ {target or '(env creds)'}")
        report = probe_backend(section, role=role, http=http,
                               ask_consent=None, allow_empirical=False)
        print_fn(_fmt_probe(report))
    if not any_run:
        print_fn("no endpoints configured — run /setup first")
