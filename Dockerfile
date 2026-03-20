FROM ngrok/ngrok:debian

SHELL ["/usr/bin/bash", "-o", "pipefail", "-c"]

# Switch to root to install system packages.
USER root

# The ngrok/ngrok:debian image ships ngrok pre-installed on Debian bookworm.
# Add Python 3, pip, and curl (curl is needed to query the ngrok API at
# runtime).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-pip python3-venv curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY gemini_mcp.py .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Drop back to the ngrok user for runtime.
USER ngrok

# ── Required ────────────────────────────────────────────────────────────────
# GEMINI_API_KEY   — your Google Gemini API key
# NGROK_AUTHTOKEN  — your ngrok authtoken (https://dashboard.ngrok.com)
#
# ── Optional ────────────────────────────────────────────────────────────────
# MCP_AUTH_TOKEN   — shared secret for SSE authentication
# GEMINI_MODEL     — Gemini model name (default: gemini-2.5-pro)
# PORT             — internal port the MCP server listens on (default: 8000)

ENV MCP_TRANSPORT=sse
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
