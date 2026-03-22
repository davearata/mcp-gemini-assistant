#!/bin/sh
# Ensure the non-root user can read CA certificates.
# python:3.12-slim ships /etc/ssl/certs with mode 700 (root-only).
# We copied the bundle to a world-readable path at build time;
# point the SSL env vars there so the Gemini API client works.
export SSL_CERT_FILE=/usr/local/share/ca-certificates.crt
unset SSL_CERT_DIR
export REQUESTS_CA_BUNDLE=/usr/local/share/ca-certificates.crt

exec "$@"
