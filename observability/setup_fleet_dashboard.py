#!/usr/bin/env python3
"""Provision the agent.py fleet dashboard in Grafana (idempotent).

Creates/ensures a Prometheus datasource, then publishes the 'Agent.py Fleet' dashboard.
Re-runnable: safe to run again after a Grafana reset (the OOM already ate the datasource once).

Env: GRAFANA_URL (default http://127.0.0.1:3001), GRAFANA_AUTH (default admin:admin),
     PROM_URL (default http://localhost:9090).
"""
import json, os, urllib.request, base64

G = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3001")
AUTH = os.environ.get("GRAFANA_AUTH", "admin:admin")
PROM = os.environ.get("PROM_URL", "http://localhost:9090")
HDR = {"Content-Type": "application/json",
       "Authorization": "Basic " + base64.b64encode(AUTH.encode()).decode()}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(G + path, data=data, method=method, headers=HDR)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


# 1. ensure Prometheus datasource
_, dss = api("GET", "/api/datasources")
ds = next((d for d in (dss or []) if d.get("type") == "prometheus"), None)
if not ds:
    st, res = api("POST", "/api/datasources", {
        "name": "Prometheus", "type": "prometheus", "url": PROM,
        "access": "proxy", "isDefault": True})
    print("created datasource:", st, res.get("datasource", {}).get("uid") or res.get("message"))
    uid = (res.get("datasource") or {}).get("uid")
else:
    uid = ds["uid"]
    print("datasource exists:", uid)

DS = {"type": "prometheus", "uid": uid}
RI = "$__rate_interval"
panels = []
pid = [0]
def P(p):
    pid[0] += 1; p["id"] = pid[0]; p["datasource"] = DS
    for t in p.get("targets", []):
        t["datasource"] = DS
    panels.append(p)

def stat(title, expr, x, unit="short", color="text"):
    P({"type": "stat", "title": title, "gridPos": {"x": x, "y": 0, "w": 4, "h": 4},
       "targets": [{"refId": "A", "expr": expr, "instant": True}],
       "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color}}},
       "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "graphMode": "area", "textMode": "value_and_name"}})

def ts(title, targets, x, y, w=12, unit="short"):
    P({"type": "timeseries", "title": title, "gridPos": {"x": x, "y": y, "w": w, "h": 8},
       "targets": [{"refId": chr(65+i), "expr": e, "legendFormat": lf}
                   for i, (e, lf) in enumerate(targets)],
       "fieldConfig": {"defaults": {"unit": unit, "custom": {"fillOpacity": 12, "showPoints": "never"}}},
       "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["last", "max"]}}})

# Row 1 — fleet overview (stat)
stat("Agents up", "count(agentpy_up == 1)", 0, color="green")
stat("Cycles", "sum(agentpy_cycles_total)", 4)
stat("Turns", "sum(agentpy_turns_total)", 8)
stat("Tool calls", "sum(agentpy_tool_calls_total)", 12)
stat("Tokens", "sum(agentpy_tokens_total)", 16)
stat("Errors", "sum(agentpy_errors_total)", 20, color="red")

# Row 2 — rates
ts("Cycles/min by agent", [(f"sum by (instance) (rate(agentpy_cycles_total[{RI}])) * 60", "{{instance}}")], 0, 4, 8)
ts("Tool calls/min by tool", [(f"sum by (tool) (rate(agentpy_tool_calls_total[{RI}])) * 60", "{{tool}}")], 8, 4, 8)
ts("Tokens/sec by agent", [(f"sum by (instance) (rate(agentpy_tokens_total[{RI}]))", "{{instance}}")], 16, 4, 8)

# Row 3 — health + latency
ts("Errors / hallucinations / patch events (per-sec)", [
    (f"sum(rate(agentpy_errors_total[{RI}]))", "errors"),
    (f"sum(rate(agentpy_hallucinations_total[{RI}]))", "hallucinations"),
    (f"sum(rate(agentpy_tool_errors_total[{RI}]))", "tool errors"),
    (f"sum(rate(agentpy_patch_events_total[{RI}]))", "patch events")], 0, 12)
ts("Cycle duration (p50 / p95)", [
    (f"histogram_quantile(0.50, sum by (le) (rate(agentpy_cycle_duration_seconds_bucket[{RI}])))", "p50"),
    (f"histogram_quantile(0.95, sum by (le) (rate(agentpy_cycle_duration_seconds_bucket[{RI}])))", "p95")],
   12, 12, unit="s")

# Row 4 — per-agent liveness table
P({"type": "table", "title": "Agents — seconds since last activity",
   "gridPos": {"x": 0, "y": 20, "w": 24, "h": 8},
   "targets": [{"refId": "A", "instant": True, "format": "table",
                "expr": "time() - agentpy_last_seen_timestamp_seconds"}],
   "transformations": [{"id": "organize", "options": {
       "excludeByName": {"Time": True, "job": True, "__name__": True},
       "renameByName": {"Value": "sec since seen", "instance": "agent"}}}],
   "fieldConfig": {"defaults": {"unit": "s"}}})

dashboard = {
    "uid": "agentpy-fleet", "title": "Agent.py Fleet", "tags": ["agentpy", "fleet"],
    "timezone": "browser", "schemaVersion": 39, "refresh": "10s",
    "time": {"from": "now-6h", "to": "now"}, "panels": panels,
}
st, res = api("POST", "/api/dashboards/db", {"dashboard": dashboard, "overwrite": True,
                                             "message": "fleet metrics dashboard"})
print("dashboard publish:", st, res.get("status") or res.get("message"))
if res.get("url"):
    print("URL:", G + res["url"])
