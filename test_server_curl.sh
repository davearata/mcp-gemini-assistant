#!/usr/bin/env bash
# test_server_curl.sh — Test the MCP SSE server with curl.
#
# Usage:
#   ./test_server_curl.sh                          # defaults: localhost:8000, no auth
#   ./test_server_curl.sh http://host:port TOKEN   # custom host + auth token
#
# The script exercises every major endpoint and prints PASS/FAIL for each.
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
TOKEN="${2:-${MCP_AUTH_TOKEN:-}}"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✅ PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ FAIL: $1"; }

# ── Helpers ──────────────────────────────────────────────────────────────────

# Build the SSE URL with optional token query param
sse_url() {
    if [ -n "$TOKEN" ]; then
        echo "${BASE_URL}/sse?token=${TOKEN}"
    else
        echo "${BASE_URL}/sse"
    fi
}

# ── 1. Health check ─────────────────────────────────────────────────────────

echo ""
echo "═══ 1. Health check (GET /health) ═══"
HTTP_CODE=$(curl -s -o /tmp/mcp_health.json -w "%{http_code}" "${BASE_URL}/health")
if [ "$HTTP_CODE" = "200" ]; then
    pass "GET /health → 200"
    echo "       $(cat /tmp/mcp_health.json)"
else
    fail "GET /health → $HTTP_CODE (expected 200)"
fi

# ── 2. Auth gating ──────────────────────────────────────────────────────────

echo ""
echo "═══ 2. Authentication ═══"

if [ -n "$TOKEN" ]; then
    # Without token → 401
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/sse")
    if [ "$HTTP_CODE" = "401" ]; then
        pass "GET /sse without token → 401"
    else
        fail "GET /sse without token → $HTTP_CODE (expected 401)"
    fi

    # With wrong token → 401
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/sse?token=wrong")
    if [ "$HTTP_CODE" = "401" ]; then
        pass "GET /sse wrong token → 401"
    else
        fail "GET /sse wrong token → $HTTP_CODE (expected 401)"
    fi
else
    echo "  (skipped — no MCP_AUTH_TOKEN configured)"
fi

# ── 3. CORS preflight ───────────────────────────────────────────────────────

echo ""
echo "═══ 3. CORS preflight (OPTIONS /sse) ═══"
HTTP_CODE=$(curl -s -o /tmp/mcp_cors.txt -w "%{http_code}" -X OPTIONS \
    -H "Origin: http://localhost:6274" \
    -H "Access-Control-Request-Method: GET" \
    "${BASE_URL}/sse")
if [ "$HTTP_CODE" = "200" ]; then
    pass "OPTIONS /sse → 200"
else
    fail "OPTIONS /sse → $HTTP_CODE (expected 200)"
fi

# ── 4. SSE connect ──────────────────────────────────────────────────────────

echo ""
echo "═══ 4. SSE connect (GET /sse) ═══"

timeout 5 curl -s -N "$(sse_url)" > /tmp/mcp_sse.txt 2>&1 &
SSE_PID=$!
sleep 2

SSE_EVENT=$(head -1 /tmp/mcp_sse.txt)
if echo "$SSE_EVENT" | grep -q "event: endpoint"; then
    pass "SSE stream opened — received 'event: endpoint'"
else
    fail "SSE stream did not return expected event"
    echo "       Got: $(head -3 /tmp/mcp_sse.txt)"
fi

# Extract messages endpoint (strip \r from SSE line endings)
MESSAGES_PATH=$(grep "^data:" /tmp/mcp_sse.txt | head -1 | sed 's/^data: //' | tr -d '\r')
echo "       Messages endpoint: ${MESSAGES_PATH}"

# ── 5. JSON-RPC: initialize ─────────────────────────────────────────────────

echo ""
echo "═══ 5. JSON-RPC initialize ═══"

if [ -n "$MESSAGES_PATH" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        "${BASE_URL}${MESSAGES_PATH}" \
        -H "Content-Type: application/json" \
        -d '{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "curl-test", "version": "1.0.0"}
            }
        }')
    if [ "$HTTP_CODE" = "202" ]; then
        pass "POST initialize → 202 Accepted"
    else
        fail "POST initialize → $HTTP_CODE (expected 202)"
    fi

    sleep 1

    # Send initialized notification
    curl -s -o /dev/null \
        "${BASE_URL}${MESSAGES_PATH}" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc": "2.0", "method": "notifications/initialized"}'

    sleep 1

    # Check that SSE received the initialize response
    if grep -q '"method":"initialize"' /tmp/mcp_sse.txt 2>/dev/null || \
       grep -q '"serverInfo"' /tmp/mcp_sse.txt 2>/dev/null; then
        pass "SSE received initialize response"
    else
        fail "SSE did not receive initialize response"
    fi
else
    fail "No messages endpoint — cannot test JSON-RPC"
fi

# ── 6. JSON-RPC: tools/list ─────────────────────────────────────────────────

echo ""
echo "═══ 6. JSON-RPC tools/list ═══"

if [ -n "$MESSAGES_PATH" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        "${BASE_URL}${MESSAGES_PATH}" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}')
    if [ "$HTTP_CODE" = "202" ]; then
        pass "POST tools/list → 202 Accepted"
    else
        fail "POST tools/list → $HTTP_CODE (expected 202)"
    fi

    sleep 2

    # Check SSE for tools response
    if grep -q '"consult_gemini"' /tmp/mcp_sse.txt 2>/dev/null; then
        pass "SSE received tools list with consult_gemini"
        # Count tools
        TOOL_COUNT=$(grep -o '"name":"[^"]*"' /tmp/mcp_sse.txt | sort -u | wc -l)
        echo "       Found $TOOL_COUNT tools"
    else
        fail "SSE did not receive tools list"
    fi
else
    fail "No messages endpoint — cannot test JSON-RPC"
fi

# ── Cleanup ──────────────────────────────────────────────────────────────────

kill $SSE_PID 2>/dev/null || true
rm -f /tmp/mcp_health.json /tmp/mcp_cors.txt /tmp/mcp_sse.txt

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
echo "═══════════════════════════════════════"
echo ""

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
