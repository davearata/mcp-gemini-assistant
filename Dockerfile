FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gemini_mcp.py .

# python:3.12-slim sets /etc/ssl/certs to mode 700 (root-only).
# Copy the CA bundle to a world-readable path so the non-root user
# can make HTTPS requests (required by the Gemini API client).
RUN cp /etc/ssl/certs/ca-certificates.crt /usr/local/share/ca-certificates.crt \
    && chmod 644 /usr/local/share/ca-certificates.crt

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home appuser
USER appuser

ENV MCP_TRANSPORT=sse
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "gemini_mcp.py"]
