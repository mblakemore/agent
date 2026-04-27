# Grafana dashboards

Source-of-truth Grafana dashboards for the agent.py fleet telemetry. Each
dashboard JSON in this directory is a Grafana-API-shaped document (the value
under the `dashboard` key in `/api/dashboards/db`), with the `id` field
stripped so Grafana can assign one on import. The `uid` is preserved across
imports so the dashboard URL stays stable.

## Files

- `agentpy-fleet.json` — fleet overview, panels keyed off the `agentpy_*`
  Prometheus metrics emitted by `telemetry.py`. UID `agentpy-fleet`, served at
  `http://localhost:3001/d/agentpy-fleet`.

## Workflow

The flow is **edit JSON, import via API**. Do **not** edit dashboards in the
Grafana UI — any change there will be overwritten by the next import, and the
UI changes are not in source control.

If a tweak in the UI is unavoidable (e.g. exploring panel layouts), re-export
the dashboard with the steps below before committing anything else.

### Export from Grafana to JSON

```bash
curl -s -u admin:admin http://localhost:3001/api/dashboards/uid/agentpy-fleet \
  | jq '.dashboard | del(.id)' \
  > dashboards/agentpy-fleet.json
```

The `del(.id)` is intentional: Grafana assigns an internal numeric `id` per
instance and we don't want it in source control. The `uid` (string) is what
makes the dashboard URL stable, so keep that.

### Import (or re-import) JSON to Grafana

```bash
curl -s -u admin:admin -X POST -H 'Content-Type: application/json' \
  -d "{\"dashboard\": $(cat dashboards/agentpy-fleet.json), \"overwrite\": true}" \
  http://localhost:3001/api/dashboards/db
```

A successful import returns `{"status":"success", "uid":"agentpy-fleet", ...}`.
The `overwrite: true` flag lets the import bump the dashboard's version even
when the UID already exists.

### Verify panels query valid PromQL

```bash
python3 scripts/verify_dashboard.py --prom-url http://localhost:9090 \
  dashboards/agentpy-fleet.json
```

Reports `OK` / `WARN` / `FAIL` per panel target. Zero series = `WARN` (the
panel may filter to a specific instance/job that isn't running). A 400 from
Prometheus = `FAIL` (syntactically invalid query, or unknown function), which
exits non-zero.

## Adding a panel

1. Edit `agentpy-fleet.json` — append a panel object to the `panels` array.
   Pick a fresh integer `id`, set `gridPos` so it doesn't overlap any
   existing panel, and use `datasource: {type: "prometheus", uid: "prometheus"}`.
2. Run the import command above.
3. Run `verify_dashboard.py` to confirm the new query is at least
   well-formed.
4. `git add dashboards/agentpy-fleet.json && git commit`.
