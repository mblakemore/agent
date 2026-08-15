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


def _probe_ctx_empirical(base, hdrs, http, model, cap=_EMPIRICAL_CAP_TOKENS):
    """Binary-search the largest accepted prompt via /v1/chat/completions.

    Trusts the SERVER-reported prompt token count where present. ~8-10
    requests of 1 output token each. Returns (tokens_or_None, detail).
    """
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


def _role_screen(role, current, input_fn, print_fn, http):
    """One role's questions + inline probe. Returns (section_dict, report)."""
    print_fn(f"\n── {role.upper()} model ──")
    kind = _ask("backend kind: 1) llama.cpp / OpenAI-compatible  "
                "2) AWS Bedrock  3) Azure (foundry)", "1", input_fn)
    if kind.strip() != "1":
        print_fn("   bedrock/foundry arrive in Phase 2 — using kind 1 for now.")
    base_url = _ask("base_url", current.get("base_url", "http://127.0.0.1:8080"),
                    input_fn)
    api_key = _ask("api_key (ENTER for none)", current.get("api_key", ""), input_fn)
    model = _ask("model (ENTER = auto-pick from /v1/models)",
                 current.get("model", ""), input_fn)

    print_fn("   probing…")
    report = probe_llamacpp(base_url, api_key or None, model or None, http=http)
    print_fn(_fmt_probe(report))
    section = {"base_url": base_url}
    if api_key:
        section["api_key"] = api_key
    resolved = report.get("p3_model", {}).get("value")
    if resolved:
        section["model"] = resolved
    elif model:
        section["model"] = model
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
                summary = {"enabled": True, "base_url": base.get("base_url", "")}
                if base.get("api_key"):
                    summary["api_key"] = base["api_key"]
                if base.get("model"):
                    summary["model"] = base["model"]
                updates["summary"] = summary
                print_fn(f"   summary -> {summary.get('base_url')} (shared)")
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
            if llm.get("base_url"):
                print_fn("   probing main endpoint for calibration…")
                main_report = probe_llamacpp(
                    llm["base_url"], llm.get("api_key"), llm.get("model"),
                    http=http)
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
    """`/setup test` — probe the configured endpoints, change nothing."""
    any_run = False
    for role, section in (("main", current_cfg.get("llm", {})),
                          ("summary", current_cfg.get("summary", {}))):
        base = section.get("base_url")
        if not base:
            print_fn(f"{role}: no base_url configured — skipped")
            continue
        any_run = True
        print_fn(f"{role}: {base}")
        report = probe_llamacpp(base, section.get("api_key"),
                                section.get("model"), http=http)
        print_fn(_fmt_probe(report))
    if not any_run:
        print_fn("no endpoints configured — run /setup first")
