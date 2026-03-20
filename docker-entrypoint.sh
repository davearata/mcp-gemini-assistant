#!/usr/bin/env bash
# Entrypoint for the Docker image.
# Starts the MCP SSE server and, when NGROK_AUTHTOKEN is set, opens an ngrok
# tunnel so the server is reachable over the public internet via HTTPS.
set -euo pipefail

PORT="${PORT:-8000}"

# ── Validate required variables ─────────────────────────────────────────────
if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "Error: GEMINI_API_KEY is required." >&2
    exit 1
fi

# ── Start the MCP SSE server in the background ─────────────────────────────
echo "Starting MCP SSE server on port ${PORT}..." >&2
python3 gemini_mcp.py &
MCP_PID=$!

# Give the server a moment to bind
sleep 2

# ── Optionally start ngrok ──────────────────────────────────────────────────
if [ -n "${NGROK_AUTHTOKEN:-}" ]; then
    echo "Starting ngrok tunnel..." >&2
    ngrok config add-authtoken "$NGROK_AUTHTOKEN" > /dev/null 2>&1
    ngrok http "${PORT}" --log=stdout > /tmp/ngrok.log 2>&1 &

    # Wait for ngrok to establish the tunnel and expose its API
    for _ in $(seq 1 15); do
        NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
            | python3 -c "import sys,json
try:
    tunnels=json.load(sys.stdin).get('tunnels',[])
    print(next((t['public_url'] for t in tunnels if t['public_url'].startswith('https')),''))
except Exception:
    print('')" 2>/dev/null) || true
        if [ -n "${NGROK_URL:-}" ]; then
            break
        fi
        sleep 1
    done

    if [ -n "${NGROK_URL:-}" ]; then
        echo "" >&2
        echo "============================================================" >&2
        echo "  ngrok tunnel active" >&2
        echo "  Public URL: ${NGROK_URL}" >&2
        echo "" >&2
        if [ -n "${MCP_AUTH_TOKEN:-}" ]; then
            echo "  Connect (with auth):" >&2
            echo "    ${NGROK_URL}/sse?token=<your-MCP_AUTH_TOKEN>" >&2
        else
            echo "  Connect:" >&2
            echo "    ${NGROK_URL}/sse" >&2
        fi
        echo "============================================================" >&2
        echo "" >&2
    else
        echo "Warning: ngrok started but could not determine public URL." >&2
        echo "Check ngrok logs in /tmp/ngrok.log or visit http://localhost:4040" >&2
    fi
else
    echo "NGROK_AUTHTOKEN is not set — skipping ngrok tunnel." >&2
    echo "The server is available at http://0.0.0.0:${PORT}/sse" >&2
fi

# ── Wait for the MCP server process ────────────────────────────────────────
wait "$MCP_PID"
