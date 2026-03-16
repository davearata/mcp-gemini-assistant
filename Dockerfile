FROM python:3.12-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install curl (needed to query ngrok API at runtime) and ngrok via the
# official apt repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg \
    && curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
       | tee /etc/apt/trusted.gpg.d/ngrok.asc > /dev/null \
    && echo "deb https://ngrok-agent.s3.amazonaws.com trixie main" \
       > /etc/apt/sources.list.d/ngrok.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ngrok \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY gemini_mcp.py .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

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
