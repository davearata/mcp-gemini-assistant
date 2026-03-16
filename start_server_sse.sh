#!/bin/bash
# Start the Gemini MCP server with SSE (HTTP) transport.
#
# Usage:
#   ./start_server_sse.sh
#
# Environment variables:
#   GEMINI_API_KEY  (required) — your Gemini API key
#   HOST            (optional) — bind address, default: 0.0.0.0
#   PORT            (optional) — TCP port,     default: 8000
#   GEMINI_MODEL    (optional) — Gemini model, default: gemini-2.5-pro
#   SYSTEM_PROMPT   (optional) — override the built-in system prompt
#   MCP_AUTH_TOKEN  (optional) — shared secret to require authentication
#
# Connect Claude Code to the running server:
#   Without auth: claude mcp add gemini-coding -s user --transport sse http://<host>:<port>/sse
#   With auth:    claude mcp add gemini-coding -s user --transport sse http://<host>:<port>/sse?token=<your-token>

set -euo pipefail

cd "$(dirname "$0")"

# Load .env if present
if [ -f .env ]; then
    # shellcheck disable=SC2046
    export $(grep -v '^#' .env | xargs)
fi

if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "Error: GEMINI_API_KEY must be set in .env or as an environment variable." >&2
    exit 1
fi

export MCP_TRANSPORT=sse
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8000}"

exec ./venv/bin/python gemini_mcp.py
