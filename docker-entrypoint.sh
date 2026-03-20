#!/usr/bin/env bash
# Entrypoint for the Docker image.
# Starts the MCP SSE server in the foreground.
set -euo pipefail

# ── Fix CA certificate access ───────────────────────────────────────────────
# The base image sets SSL_CERT_FILE pointing into /etc/ssl/certs which is
# mode 700 (root-only).  Override to use the world-readable copy made during
# the Docker build.
export SSL_CERT_FILE=/usr/local/share/ca-certificates.crt

# ── Validate required variables ─────────────────────────────────────────────
if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "Error: GEMINI_API_KEY is required." >&2
    exit 1
fi

# ── Start the MCP SSE server ───────────────────────────────────────────────
exec python3 gemini_mcp.py
