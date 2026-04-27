#!/usr/bin/env bash
# Import a dashboard JSON into Grafana via the API, using the service-account
# token at ~/.config/grafana-cicd-token. See dashboards/README.md.
#
# Usage:
#   scripts/import_dashboard.sh <dashboard.json> [--grafana-url URL]
#
# Exits non-zero if:
#   - the token file is missing or empty
#   - the JSON is invalid
#   - Grafana returns non-2xx
set -euo pipefail

DASHBOARD_JSON="${1:-}"
GRAFANA_URL="${2:-${GRAFANA_URL:-http://localhost:3001}}"
TOKEN_FILE="${GRAFANA_TOKEN_FILE:-${HOME}/.config/grafana-cicd-token}"

if [[ -z "$DASHBOARD_JSON" ]]; then
    echo "usage: $0 <dashboard.json> [grafana-url]" >&2
    exit 2
fi

if [[ ! -f "$DASHBOARD_JSON" ]]; then
    echo "error: dashboard JSON not found: $DASHBOARD_JSON" >&2
    exit 2
fi

if [[ ! -s "$TOKEN_FILE" ]]; then
    echo "error: missing or empty Grafana token at $TOKEN_FILE" >&2
    echo "  see dashboards/README.md § Authentication for one-time setup" >&2
    exit 2
fi

TOKEN="$(cat "$TOKEN_FILE")"

# Validate JSON locally before sending
if ! jq -e . "$DASHBOARD_JSON" > /dev/null 2>&1; then
    echo "error: $DASHBOARD_JSON is not valid JSON" >&2
    exit 2
fi

# POST and capture both the body and the HTTP status
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
HTTP_CODE="$(curl -s -o "$TMP" -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -X POST \
    --data "$(jq '{dashboard: ., overwrite: true}' "$DASHBOARD_JSON")" \
    "${GRAFANA_URL}/api/dashboards/db")"

cat "$TMP"
echo

if [[ "$HTTP_CODE" -lt 200 || "$HTTP_CODE" -ge 300 ]]; then
    echo "error: Grafana returned HTTP $HTTP_CODE" >&2
    exit 1
fi

echo "imported $DASHBOARD_JSON (HTTP $HTTP_CODE)"
