#!/usr/bin/env bash
set -euo pipefail

BASE="https://jsonkar.pythonanywhere.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMPORT_FILE="$SCRIPT_DIR/improved_proxies.json"

# ---------------------------------------------------------------------------
# Session cookie — update this when it expires
# ---------------------------------------------------------------------------
SESSION="eyJfcGVybWFuZW50Ijp0cnVlLCJhdXRoZW50aWNhdGVkIjp0cnVlfQ.aecBkg.ANySPWr2ek2mGW2QGdE3sOGaKqs"

CURL="curl -s -w '\nHTTP %{http_code}' -b session=$SESSION -H 'Content-Type: application/json'"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Import proxies
# ---------------------------------------------------------------------------
log "Importing $IMPORT_FILE ..."
[[ -f "$IMPORT_FILE" ]] || die "File not found: $IMPORT_FILE"

RESPONSE=$(curl -s -w "\nHTTP %{http_code}" \
  -X POST "$BASE/proxy/import/" \
  -b "session=$SESSION" \
  -H "Content-Type: application/json" \
  -d @"$IMPORT_FILE")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1 | awk '{print $2}')
BODY=$(echo "$RESPONSE" | sed '$d')

log "Import response [$HTTP_CODE]: $BODY"
[[ "$HTTP_CODE" == "200" ]] || die "Import failed with HTTP $HTTP_CODE"

# ---------------------------------------------------------------------------
# 2. Seed MongoDB state for stateful proxies
# ---------------------------------------------------------------------------
seed() {
  local proxy="$1"
  local payload="$2"
  log "Seeding state for proxy: $proxy"
  RESPONSE=$(curl -s -w "\nHTTP %{http_code}" \
    -X PUT "$BASE/proxy/state/$proxy/" \
    -b "session=$SESSION" \
    -H "Content-Type: application/json" \
    -d "$payload")
  HTTP_CODE=$(echo "$RESPONSE" | tail -n1 | awk '{print $2}')
  BODY=$(echo "$RESPONSE" | sed '$d')
  log "  [$HTTP_CODE]: $BODY"
  [[ "$HTTP_CODE" =~ ^2 ]] || echo "  WARNING: seed for $proxy returned HTTP $HTTP_CODE"
}

seed "ajiocashwallet"  '{"tokens": {}, "transactions": {}, "refunds": {}, "benefits": {"bonusAmount": 1000, "ntAmount": 300, "rcsAmount": 700}}'
seed "deadlock"        '{"orders": {}}'
seed "jioprimewallet"  '{"points": {"balance": 1500}}'
seed "mahacashback"    '{"cashback": {"balance": 5000}}'
seed "pinelabs"        '{"transactions": {}}'
seed "juspay"          '{"orders": {}}'

log "Done."
