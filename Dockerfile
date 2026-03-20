FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The /etc/ssl/certs directory in this base image has restrictive permissions
# (mode 700) that prevent non-root users from reading CA certificates.
# Copy the CA bundle to a world-readable location.  The entrypoint sets
# SSL_CERT_FILE at runtime to point here (the base image bakes in the old
# path at a layer that overrides Dockerfile ENV).
RUN cp /etc/ssl/certs/ca-certificates.crt /usr/local/share/ca-certificates.crt

# Copy application code
COPY gemini_mcp.py .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Run as a non-root user
RUN useradd --create-home appuser
USER appuser

# ── Required ────────────────────────────────────────────────────────────────
# GEMINI_API_KEY   — your Google Gemini API key
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
