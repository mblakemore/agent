#!/usr/bin/env python3
"""Live probes of the UCSB AI Commons /v1/messages gateway (aicommons-integration.md P0).

Each probe is one or a few cheap requests whose raw status, headers and body are
recorded, so the guesses in the plan (and in llm_backend.AICommonsBackend) can be
checked and §6 of the plan filled in.

    AICOMMONS_BASE_URL=https://<api>.execute-api.<region>.amazonaws.com/v1 \\
    AICOMMONS_API_KEY=... python3 scripts/aicommons_probe.py [--only 2,5] [--overflow]

Writes a JSON record per probe to aicommons-probe-<timestamp>.json and prints a
Markdown summary. --overflow adds probe 7 (a ~7 MB request and a ~1.2M-token
request); both should be refused before any model runs, but they are opt-in.
The key never appears in the output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import httpx

BASE = os.environ.get("AICOMMONS_BASE_URL", "").rstrip("/")
KEY = os.environ.get("AICOMMONS_API_KEY", "")
CHEAP = os.environ.get("AICOMMONS_PROBE_MODEL", "claude-v5.5-haiku")
BIG = os.environ.get("AICOMMONS_PROBE_BIG_MODEL", "claude-v5.5-opus")
NOTE_HEADERS = ("request-id", "x-max-tokens-clamped", "x-cache-control-policy",
                "x-unimplemented-parameters", "x-dropped-server-tools", "x-should-retry",
                "retry-after", "content-type")
TOOL = {"name": "get_weather", "description": "Get the weather for a city.",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}

client = httpx.Client(timeout=httpx.Timeout(connect=10, read=120, write=60, pool=10))
results: list[dict] = []


def call(method, path, body=None, key=KEY, stream=False):
    """One request → {status, headers, body|events}. Never raises."""
    url = f"{BASE}{path}"
    headers = {"x-api-key": key, "content-type": "application/json", "anthropic-version": "2023-06-01"}
    t0 = time.monotonic()
    try:
        if stream:
            events = []
            with client.stream(method, url, headers=headers, json=body) as r:
                ev = None
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: "):
                        try:
                            events.append({"event": ev, "data": json.loads(line[6:])})
                        except json.JSONDecodeError:
                            events.append({"event": ev, "raw": line[6:200]})
                out = {"status": r.status_code, "headers": _h(r.headers), "events": events}
        else:
            r = client.request(method, url, headers=headers, json=body)
            try:
                payload = r.json()
            except ValueError:
                payload = r.text[:2000]
            out = {"status": r.status_code, "headers": _h(r.headers), "body": payload}
    except Exception as e:  # noqa: BLE001
        out = {"status": None, "exception": f"{type(e).__name__}: {e}"}
    out["seconds"] = round(time.monotonic() - t0, 2)
    return out


def _h(headers):
    return {k: v for k, v in headers.items() if k.lower() in NOTE_HEADERS}


def msg(model=CHEAP, max_tokens=64, **kw):
    body = {"model": model, "max_tokens": max_tokens,
            "messages": kw.pop("messages", [{"role": "user", "content": "Reply with the word OK."}])}
    body.update(kw)
    return body


def usage_of(r):
    if "body" in r and isinstance(r["body"], dict):
        return r["body"].get("usage")
    for e in reversed(r.get("events") or []):
        if e.get("event") == "message_delta":
            return e["data"].get("usage")
    return None


def record(n, title, **data):
    results.append({"probe": n, "title": title, **data})
    print(f"\n### P0.{n} {title}")
    for k, v in data.items():
        s = json.dumps(v, default=str)
        print(f"- **{k}**: `{s[:600]}{'…' if len(s) > 600 else ''}`")


# ── probes ──────────────────────────────────────────────────────────────


def p1_base_url():
    r = call("POST", "/v1/messages", msg())
    record(1, "Base URL shape", url=f"{BASE}/v1/messages", status=r.get("status"), headers=r.get("headers"),
           text=(r.get("body") or {}).get("content") if isinstance(r.get("body"), dict) else r.get("body"))


def p2_cache():
    # A prefix well over the 4096-token Haiku 4.5 / 512-token 5.x minimum.
    sysmsg = "You are a terse assistant. " + " ".join(f"Rule {i}: answer briefly." for i in range(900))
    turns = [{"role": "user", "content": "Say A."}]
    rows = []
    for i in range(3):
        r = call("POST", "/v1/messages", msg(system=sysmsg, messages=turns))
        rows.append({"req": i + 1, "status": r.get("status"), "usage": usage_of(r)})
    reply = "A."
    turns = turns + [{"role": "assistant", "content": reply}, {"role": "user", "content": "Now say B."}]
    r = call("POST", "/v1/messages", msg(system=sysmsg, messages=turns))
    rows.append({"req": 4, "status": r.get("status"), "usage": usage_of(r), "note": "one more turn appended"})
    record(2, "Cache reality (3 identical prefixes, then +1 turn)", rows=rows)


def p3_sampling():
    r = call("POST", "/v1/messages", msg(temperature=0.2, top_p=0.9, top_k=5))
    r2 = call("POST", "/v1/messages", msg(temperature=0.2, top_p=0.9, top_k=5, thinking={"type": "adaptive"}))
    record(3, "Sampling params, with and without thinking",
           plain={"status": r.get("status"), "headers": r.get("headers")},
           thinking={"status": r2.get("status"), "headers": r2.get("headers")})


def p4_tool_choice_any():
    r = call("POST", "/v1/messages", msg(model=BIG, tools=[TOOL], tool_choice={"type": "any"},
                                         messages=[{"role": "user", "content": "Weather in Paris?"}]))
    record(4, "tool_choice any on Opus 5.5", status=r.get("status"),
           body=r.get("body") if r.get("status") != 200 else (r.get("body") or {}).get("stop_reason"))


def p5_thinking_round_trip():
    first = call("POST", "/v1/messages", msg(model=BIG, max_tokens=2000, tools=[TOOL],
                                             thinking={"type": "adaptive"}, output_config={"effort": "high"},
                                             messages=[{"role": "user", "content": "What's the weather in Paris? Use the tool."}]))
    content = (first.get("body") or {}).get("content") if isinstance(first.get("body"), dict) else None
    out = {"first": {"status": first.get("status"), "blocks": [b.get("type") for b in content or []]}}
    use = next((b for b in content or [] if b.get("type") == "tool_use"), None)
    if use:
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "Sunny, 21C"}]}
        q = {"role": "user", "content": "What's the weather in Paris? Use the tool."}
        with_think = call("POST", "/v1/messages", msg(model=BIG, max_tokens=500, tools=[TOOL],
                                                      thinking={"type": "adaptive"},
                                                      messages=[q, {"role": "assistant", "content": content}, result]))
        bare = [b for b in content if b.get("type") == "tool_use"]
        without = call("POST", "/v1/messages", msg(model=BIG, max_tokens=500, tools=[TOOL],
                                                   thinking={"type": "adaptive"},
                                                   messages=[q, {"role": "assistant", "content": bare}, result]))
        to_haiku = call("POST", "/v1/messages", msg(model=CHEAP, max_tokens=500, tools=[TOOL],
                                                    thinking={"type": "adaptive"},
                                                    messages=[q, {"role": "assistant", "content": content}, result]))
        out.update({
            "back_with_thinking": {"status": with_think.get("status"), "err": _err(with_think)},
            "back_without_thinking": {"status": without.get("status"), "err": _err(without)},
            "opus_blocks_to_haiku": {"status": to_haiku.get("status"), "err": _err(to_haiku)},
        })
    record(5, "Thinking round-trip across a tool call", **out)


def _err(r):
    b = r.get("body")
    return (b.get("error") or {}).get("message") if isinstance(b, dict) and r.get("status") != 200 else None


def p6_effort():
    rows = {}
    for effort in ("low", "high", "xhigh", "max"):
        r = call("POST", "/v1/messages", msg(model=BIG, max_tokens=200, thinking={"type": "adaptive"},
                                             output_config={"effort": effort}))
        rows[effort] = {"status": r.get("status"), "err": _err(r), "usage": usage_of(r)}
    r = call("POST", "/v1/messages", msg(model="claude-v5.5-sonnet", max_tokens=100))
    rows["sonnet_5.5_no_thinking"] = {"status": r.get("status"), "err": _err(r)}
    record(6, "Effort levels on Opus 5.5; Sonnet 5.5 with thinking omitted", rows=rows)


def p7_overflow():
    big_payload = msg(messages=[{"role": "user", "content": "x " * 3_600_000}])          # ~7.2 MB
    r1 = call("POST", "/v1/messages", big_payload)
    tokens = msg(messages=[{"role": "user", "content": "word " * 1_200_000}])            # ~1.2M tokens, ~6 MB
    r2 = call("POST", "/v1/messages", tokens, stream=False)
    r3 = call("POST", "/v1/messages", {**msg(messages=[{"role": "user", "content": "word " * 1_100_000}]),
                                       "stream": True}, stream=True)
    record(7, "Overflow: payload limit and model limit (non-stream and stream)",
           payload_7mb={"status": r1.get("status"), "body": r1.get("body"), "exc": r1.get("exception")},
           tokens_1_2m={"status": r2.get("status"), "body": r2.get("body")},
           tokens_stream={"status": r3.get("status"), "events": (r3.get("events") or [])[:3]})


def p8_errors():
    bad = call("POST", "/v1/messages", msg(), key="sk-not-a-real-key")
    st = call("POST", "/v1/messages", {**msg(max_tokens=200), "stream": True}, stream=True)
    record(8, "Errors (401 shape; a normal stream's event sequence for comparison)",
           e401={"status": bad.get("status"), "body": bad.get("body"), "headers": bad.get("headers")},
           stream_events=[e.get("event") for e in st.get("events") or []])


def p9_usage_models():
    u = call("GET", "/v1/token-usage")
    m = call("GET", "/v1/models")
    record(9, "/v1/token-usage and /v1/models", token_usage=u.get("body"), models=m.get("body"))


def p10_headers():
    r = call("POST", "/v1/messages", msg(max_tokens=200_000, metadata={"user_id": "probe"},
                                         cache_control={"type": "ephemeral"}))
    record(10, "Headers on a normal call (huge max_tokens, metadata, cache_control)",
           status=r.get("status"), headers=r.get("headers"))


def p11_media():
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    q = {"role": "user", "content": "Use the tool for Paris."}
    use = {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_p11", "name": "get_weather",
                                             "input": {"city": "Paris"}}]}
    res = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_p11", "content": [
        {"type": "text", "text": "Here is a radar image."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}]}]}
    r1 = call("POST", "/v1/messages", msg(max_tokens=100, tools=[TOOL], messages=[q, use, res]))
    pdf = ("JVBERi0xLjEKMSAwIG9iajw8L1R5cGUvQ2F0YWxvZy9QYWdlcyAyIDAgUj4+ZW5kb2JqCjIgMCBvYmo8PC9UeXBlL1BhZ2VzL0tp"
           "ZHNbMyAwIFJdL0NvdW50IDE+PmVuZG9iagozIDAgb2JqPDwvVHlwZS9QYWdlL1BhcmVudCAyIDAgUi9NZWRpYUJveFswIDAgMyAzXT4+"
           "ZW5kb2JqCnRyYWlsZXI8PC9Sb290IDEgMCBSPj4K")
    r2 = call("POST", "/v1/messages", msg(max_tokens=100, messages=[{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf}},
        {"type": "text", "text": "How many pages?"}]}]))
    record(11, "Image inside tool_result; base64 PDF document", image_in_result={"status": r1.get("status"), "err": _err(r1)},
           pdf_document={"status": r2.get("status"), "err": _err(r2)})


PROBES = {1: p1_base_url, 2: p2_cache, 3: p3_sampling, 4: p4_tool_choice_any, 5: p5_thinking_round_trip,
          6: p6_effort, 7: p7_overflow, 8: p8_errors, 9: p9_usage_models, 10: p10_headers, 11: p11_media}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated probe numbers")
    ap.add_argument("--overflow", action="store_true", help="include probe 7 (large requests)")
    args = ap.parse_args()
    if not BASE or not KEY:
        sys.exit("set AICOMMONS_BASE_URL (the stage URL, ending in /v1) and AICOMMONS_API_KEY")
    picks = [int(x) for x in args.only.split(",")] if args.only else sorted(PROBES)
    if 7 in picks and not args.overflow:
        picks.remove(7)
        print("(skipping probe 7; pass --overflow to run it)")
    print(f"# AI Commons probe — {datetime.now().isoformat(timespec='seconds')} — {BASE}")
    for n in picks:
        PROBES[n]()
    out = f"aicommons-probe-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w") as fh:
        json.dump(results, fh, indent=1, default=str)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
