#!/bin/bash
# test_server_curl.sh — Smoke-test an MCP SSE server with curl.
#
# Usage:
#   ./test_server_curl.sh [BASE_URL]
#
# BASE_URL defaults to http://localhost:8000
#
# The script exercises the MCP SSE protocol:
#   1. Connects to GET /sse and reads the "endpoint" event
#   2. Sends an "initialize" JSON-RPC request via POST
#   3. Sends a "notifications/initialized" notification
#   4. Sends a "tools/list" request and verifies known tools
#
# Exit codes:
#   0  — all checks passed
#   1  — one or more checks failed

set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
PASS=0
FAIL=0
TMPDIR_TEST="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_TEST"; [ -n "${SSE_PID:-}" ] && kill "$SSE_PID" 2>/dev/null || true' EXIT

SSE_FILE="$TMPDIR_TEST/sse_output"

# ---------- helpers ----------------------------------------------------------

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

pass() { PASS=$((PASS + 1)); green "  ✓ $1"; }
fail() { FAIL=$((FAIL + 1)); red   "  ✗ $1"; }

check() {
    # check <description> <condition>
    if eval "$2"; then
        pass "$1"
    else
        fail "$1"
    fi
}

# ---------- 1. SSE endpoint --------------------------------------------------

bold "1. Connecting to SSE endpoint ($BASE_URL/sse) …"

# Start a background curl that keeps the SSE connection open and appends to a file.
curl -sN "$BASE_URL/sse" > "$SSE_FILE" 2>/dev/null &
SSE_PID=$!

# Wait for the SSE stream to deliver the initial "endpoint" event.
RETRIES=0
while [ $RETRIES -lt 10 ]; do
    if grep -q '^event: endpoint' "$SSE_FILE" 2>/dev/null; then
        break
    fi
    sleep 1
    RETRIES=$((RETRIES + 1))
done

if [ ! -s "$SSE_FILE" ]; then
    fail "SSE endpoint returned no data (is the server running at $BASE_URL?)"
    bold "Result: 0 passed, 1 failed"
    exit 1
fi

check "SSE stream contains 'event: endpoint'" \
    "grep -q '^event: endpoint' '$SSE_FILE'"

# Extract the messages URL from the data line, stripping \r (SSE spec uses \r\n).
MESSAGES_PATH="$(grep '^data:' "$SSE_FILE" | head -1 | sed 's/^data: //' | tr -d '\r\n')"

check "Received messages endpoint path" \
    '[ -n "$MESSAGES_PATH" ]'

# Build full messages URL. The data field may be a relative path or absolute URL.
case "$MESSAGES_PATH" in
    http*) MESSAGES_URL="$MESSAGES_PATH" ;;
    *)     MESSAGES_URL="${BASE_URL}${MESSAGES_PATH}" ;;
esac

echo "  → Messages URL: $MESSAGES_URL"

# ---------- 2. MCP initialize ------------------------------------------------

bold "2. Sending initialize request …"

INIT_STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MESSAGES_URL" \
    -H 'Content-Type: application/json' \
    -d '{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": { "name": "test_server_curl", "version": "1.0.0" }
  }
}')"

check "initialize request accepted (HTTP $INIT_STATUS)" \
    '[ "$INIT_STATUS" = "202" ] || [ "$INIT_STATUS" = "200" ] || [ "$INIT_STATUS" = "204" ]'

# Wait for the response to arrive on the SSE stream.
sleep 2

INIT_SSE_DATA="$(grep '^data:' "$SSE_FILE" | sed 's/^data: //' | tr -d '\r')"

check "initialize response includes serverInfo" \
    "echo \"\$INIT_SSE_DATA\" | grep -q 'serverInfo'"

check "initialize response includes protocolVersion" \
    "echo \"\$INIT_SSE_DATA\" | grep -q 'protocolVersion'"

# ---------- 3. Send initialized notification ---------------------------------

bold "3. Sending initialized notification …"

NOTIF_STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MESSAGES_URL" \
    -H 'Content-Type: application/json' \
    -d '{
  "jsonrpc": "2.0",
  "method": "notifications/initialized"
}')"

check "initialized notification accepted (HTTP $NOTIF_STATUS)" \
    '[ "$NOTIF_STATUS" = "202" ] || [ "$NOTIF_STATUS" = "200" ] || [ "$NOTIF_STATUS" = "204" ]'

# ---------- 4. List tools ----------------------------------------------------

bold "4. Sending tools/list request …"

TOOLS_STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MESSAGES_URL" \
    -H 'Content-Type: application/json' \
    -d '{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}')"

check "tools/list accepted (HTTP $TOOLS_STATUS)" \
    '[ "$TOOLS_STATUS" = "202" ] || [ "$TOOLS_STATUS" = "200" ] || [ "$TOOLS_STATUS" = "204" ]'

# Wait for the response to arrive on the SSE stream.
sleep 2

TOOLS_DATA="$(grep '^data:' "$SSE_FILE" | sed 's/^data: //' | tr -d '\r')"

check "tools/list response includes consult_gemini" \
    "echo \"\$TOOLS_DATA\" | grep -q 'consult_gemini'"

check "tools/list response includes list_sessions" \
    "echo \"\$TOOLS_DATA\" | grep -q 'list_sessions'"

check "tools/list response includes end_session" \
    "echo \"\$TOOLS_DATA\" | grep -q 'end_session'"

# ---------- Summary ----------------------------------------------------------

echo ""
bold "Result: $PASS passed, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
