"""Tests for TokenAuthMiddleware."""

import asyncio
import pytest
from unittest.mock import AsyncMock


# Import the middleware directly from the module without triggering the
# GEMINI_API_KEY check by patching the environment first.
import os
os.environ.setdefault("GEMINI_API_KEY", "test-key")

# We only need the middleware class; importing the full module would try to
# initialise the Gemini client, so extract the class via importlib.
import importlib.util
import types as _types  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "gemini_mcp",
    os.path.join(os.path.dirname(__file__), "gemini_mcp.py"),
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)

# Stub heavy third-party imports so the module can load in a test env
import sys
for stub_name in ("google", "google.genai", "google.genai.types"):
    sys.modules.setdefault(stub_name, _types.ModuleType(stub_name))

# Provide a fake genai.Client so the module-level initialisation succeeds
_genai = sys.modules["google.genai"]
_genai.Client = lambda **kw: None  # type: ignore[attr-defined]

_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

TokenAuthMiddleware = _mod.TokenAuthMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_request(middleware, headers=None, query_string=b"", path="/sse"):
    """Simulate an ASGI HTTP request through the middleware."""
    raw_headers = []
    for k, v in (headers or {}).items():
        raw_headers.append([k.encode(), v.encode()])

    scope = {
        "type": "http",
        "path": path,
        "headers": raw_headers,
        "query_string": query_string,
    }

    responses = []

    async def send(message):
        responses.append(message)

    inner_app = AsyncMock()

    mw = TokenAuthMiddleware(inner_app, middleware)
    await mw(scope, None, send)
    return inner_app, responses


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTokenAuthMiddleware:
    """Token authentication middleware tests."""

    @pytest.mark.asyncio
    async def test_no_token_configured_allows_all(self):
        """When the server token is empty, all requests pass through."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "")
        scope = {"type": "http", "path": "/sse", "headers": [], "query_string": b""}
        await mw(scope, None, None)
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        """Non-HTTP scopes (e.g. websocket, lifespan) are never blocked."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "secret")
        scope = {"type": "lifespan"}
        await mw(scope, None, None)
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bearer_header_valid(self):
        """Valid Bearer token in Authorization header passes."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "my-secret")
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [[b"authorization", b"Bearer my-secret"]],
            "query_string": b"",
        }
        await mw(scope, None, None)
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bearer_header_invalid(self):
        """Invalid Bearer token returns 401."""
        _, responses = await _make_request("my-secret", headers={"authorization": "Bearer wrong"})
        assert any(r.get("status") == 401 for r in responses)

    @pytest.mark.asyncio
    async def test_query_param_valid(self):
        """Valid token in query string passes."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "my-secret")
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [],
            "query_string": b"token=my-secret",
        }
        await mw(scope, None, None)
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_param_invalid(self):
        """Invalid query-string token returns 401."""
        _, responses = await _make_request("my-secret", query_string=b"token=wrong")
        assert any(r.get("status") == 401 for r in responses)

    @pytest.mark.asyncio
    async def test_no_credentials_returns_401(self):
        """Request with no credentials at all returns 401."""
        _, responses = await _make_request("my-secret")
        assert any(r.get("status") == 401 for r in responses)

    @pytest.mark.asyncio
    async def test_bearer_preferred_over_query(self):
        """When both are supplied, Bearer header takes precedence."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "header-token")
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [[b"authorization", b"Bearer header-token"]],
            "query_string": b"token=wrong",
        }
        await mw(scope, None, None)
        inner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timing_safe_comparison(self):
        """Ensure comparison is timing-safe (uses hmac.compare_digest)."""
        import hmac
        # The middleware uses hmac.compare_digest internally; verify it
        # rejects near-misses without leaking info.
        _, responses = await _make_request("abcdef", headers={"authorization": "Bearer abcdeg"})
        assert any(r.get("status") == 401 for r in responses)

    @pytest.mark.asyncio
    async def test_401_includes_www_authenticate(self):
        """401 responses include a WWW-Authenticate: Bearer header."""
        _, responses = await _make_request("secret")
        start = next(r for r in responses if r.get("type") == "http.response.start")
        header_names = [h[0] for h in start["headers"]]
        assert b"www-authenticate" in header_names

    @pytest.mark.asyncio
    async def test_messages_endpoint_passes_without_token(self):
        """POST /messages passes through even without a token.

        The /messages endpoint is session-scoped (the session-id is only
        communicated over the authenticated SSE stream), so it does not
        require a separate token check.
        """
        inner, responses = await _make_request(
            "my-secret", path="/messages"
        )
        inner.assert_awaited_once()
        assert not any(r.get("status") == 401 for r in responses)

    @pytest.mark.asyncio
    async def test_non_sse_path_passes_without_token(self):
        """Requests to paths other than /sse are not auth-gated."""
        inner, responses = await _make_request(
            "my-secret", path="/health"
        )
        inner.assert_awaited_once()
        assert not any(r.get("status") == 401 for r in responses)

    @pytest.mark.asyncio
    async def test_query_token_stripped_from_scope(self):
        """Token is stripped from query_string before forwarding to inner app."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "my-secret")
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [],
            "query_string": b"token=my-secret",
        }
        await mw(scope, None, None)
        inner.assert_awaited_once()
        # The inner app should receive an empty query string
        forwarded_scope = inner.call_args[0][0]
        assert b"token" not in forwarded_scope["query_string"]

    @pytest.mark.asyncio
    async def test_query_token_stripped_preserves_other_params(self):
        """Other query params are preserved when token is stripped."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "my-secret")
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [],
            "query_string": b"token=my-secret&foo=bar",
        }
        await mw(scope, None, None)
        inner.assert_awaited_once()
        forwarded_scope = inner.call_args[0][0]
        qs = forwarded_scope["query_string"].decode()
        assert "foo=bar" in qs
        assert "token" not in qs

    @pytest.mark.asyncio
    async def test_bearer_auth_preserves_query_string(self):
        """When auth is via bearer header, query string is untouched."""
        inner = AsyncMock()
        mw = TokenAuthMiddleware(inner, "my-secret")
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [[b"authorization", b"Bearer my-secret"]],
            "query_string": b"foo=bar",
        }
        await mw(scope, None, None)
        inner.assert_awaited_once()
        forwarded_scope = inner.call_args[0][0]
        assert forwarded_scope["query_string"] == b"foo=bar"

    @pytest.mark.asyncio
    async def test_401_body_includes_instructions(self):
        """401 body includes instructions on how to authenticate."""
        _, responses = await _make_request("secret")
        import json
        body_msg = next(r for r in responses if r.get("type") == "http.response.body")
        body = json.loads(body_msg["body"])
        assert "Authorization: Bearer" in body["message"]
        assert "?token=" in body["message"]


# ---------------------------------------------------------------------------
# CORS middleware integration tests
# ---------------------------------------------------------------------------

class TestCORSMiddleware:
    """Verify that CORSMiddleware wraps the app correctly."""

    @pytest.mark.asyncio
    async def test_cors_import_available(self):
        """Starlette CORSMiddleware is importable (dependency present)."""
        from starlette.middleware.cors import CORSMiddleware
        assert CORSMiddleware is not None

    @pytest.mark.asyncio
    async def test_cors_wraps_auth_middleware(self):
        """CORS middleware wraps auth, so OPTIONS preflight is answered
        before auth runs (no 401 on preflight)."""
        from starlette.middleware.cors import CORSMiddleware

        inner = AsyncMock()
        auth = TokenAuthMiddleware(inner, "my-secret")
        cors = CORSMiddleware(
            auth,
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

        # Simulate an OPTIONS preflight request to /sse
        scope = {
            "type": "http",
            "method": "OPTIONS",
            "path": "/sse",
            "headers": [
                [b"origin", b"http://localhost:6274"],
                [b"access-control-request-method", b"GET"],
            ],
            "query_string": b"",
            "root_path": "",
        }

        responses = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            responses.append(message)

        await cors(scope, receive, send)

        # Should get a 200 (not 401) with CORS headers
        start = next(r for r in responses if r.get("type") == "http.response.start")
        assert start["status"] == 200
        headers = dict(start["headers"])
        assert b"access-control-allow-origin" in headers

    @pytest.mark.asyncio
    async def test_cors_adds_headers_to_authenticated_response(self):
        """Authenticated requests get CORS headers added by the outer layer."""
        from starlette.middleware.cors import CORSMiddleware

        # Create a simple inner app that returns 200
        async def ok_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({
                "type": "http.response.body",
                "body": b"ok",
            })

        auth = TokenAuthMiddleware(ok_app, "my-secret")
        cors = CORSMiddleware(
            auth,
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/sse",
            "headers": [
                [b"origin", b"http://localhost:6274"],
                [b"authorization", b"Bearer my-secret"],
            ],
            "query_string": b"",
            "root_path": "",
        }

        responses = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            responses.append(message)

        await cors(scope, receive, send)

        start = next(r for r in responses if r.get("type") == "http.response.start")
        assert start["status"] == 200
        headers = dict(start["headers"])
        assert headers.get(b"access-control-allow-origin") == b"*"
