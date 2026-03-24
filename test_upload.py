"""Tests for the file upload functionality."""

import asyncio
import base64
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Set required env var before importing
os.environ['GEMINI_API_KEY'] = 'test-key-for-tests'

# Use importlib to load the module with a mocked genai client
import importlib.util

# Pre-patch the google.genai.Client constructor so we don't hit the real API
_mock_client = MagicMock()

with patch('google.genai.Client', return_value=_mock_client):
    spec = importlib.util.spec_from_file_location("gemini_mcp", "gemini_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

GeminiMCPServer = mod.GeminiMCPServer
Session = mod.Session
ProcessedFile = mod.ProcessedFile
# The `client` used inside the module — point our mock at the module-level var
mock_client = mod.client


@pytest.fixture
def server():
    """Create a fresh GeminiMCPServer instance."""
    return GeminiMCPServer()


@pytest.fixture
def session():
    """Create a test session."""
    return Session(
        session_id="test-session-123",
        chat=MagicMock(),
        created=datetime.now(),
        last_used=datetime.now(),
        message_count=0,
    )


class TestGetMimeType:
    """Tests for _get_mime_type helper."""

    def test_known_extension_python(self, server):
        assert server._get_mime_type("main.py") == "text/x-python"

    def test_known_extension_javascript(self, server):
        assert server._get_mime_type("app.js") == "text/javascript"

    def test_known_extension_typescript(self, server):
        # .ts is recognized by the system mimetypes; fallback map used for .tsx
        result = server._get_mime_type("index.ts")
        assert result is not None  # system returns something

    def test_known_extension_json(self, server):
        assert server._get_mime_type("config.json") == "application/json"

    def test_known_extension_markdown(self, server):
        result = server._get_mime_type("README.md")
        assert "markdown" in result or "text" in result

    def test_known_extension_yaml(self, server):
        result = server._get_mime_type("config.yaml")
        assert "yaml" in result

    def test_known_extension_sql(self, server):
        result = server._get_mime_type("schema.sql")
        assert "sql" in result.lower()

    def test_unknown_extension_defaults_to_text(self, server):
        # Use a truly unknown extension
        assert server._get_mime_type("data.qzx") == "text/plain"

    def test_no_extension_defaults_to_text(self, server):
        assert server._get_mime_type("Makefile") == "text/plain"

    def test_jsx_extension(self, server):
        assert server._get_mime_type("Component.jsx") == "text/javascript"

    def test_tsx_extension(self, server):
        assert server._get_mime_type("Component.tsx") == "text/typescript"


class TestProcessFileContent:
    """Tests for _process_file_content method."""

    @pytest.mark.asyncio
    async def test_successful_upload(self, server, session):
        """Test successful base64 file upload."""
        content = "print('hello world')"
        content_b64 = base64.b64encode(content.encode()).decode()

        # Mock the Gemini upload
        mock_uploaded = MagicMock()
        mock_uploaded.state = "ACTIVE"
        mock_uploaded.uri = "https://gemini.example.com/files/abc123"
        mock_uploaded.mime_type = "text/x-python"
        mock_uploaded.name = "files/abc123"
        mock_client.files.upload.return_value = mock_uploaded

        result = await server._process_file_content("test.py", content_b64, session)

        assert result.file_name == "test.py"
        assert result.mime_type == "text/x-python"
        assert result.file_uri == "https://gemini.example.com/files/abc123"
        assert result.gemini_file_id == "files/abc123"

        # Should be stored in session
        assert "test.py" in session.processed_files
        assert "test.py" in session.pending_files

    @pytest.mark.asyncio
    async def test_invalid_base64_raises_error(self, server, session):
        """Test that invalid base64 content raises ValueError."""
        with pytest.raises(ValueError, match="Invalid base64 content"):
            await server._process_file_content("test.py", "not-valid-base64!!!", session)

    @pytest.mark.asyncio
    async def test_custom_mime_type(self, server, session):
        """Test that custom mime_type overrides auto-detection."""
        content_b64 = base64.b64encode(b"data").decode()

        mock_uploaded = MagicMock()
        mock_uploaded.state = "ACTIVE"
        mock_uploaded.uri = "https://gemini.example.com/files/abc456"
        mock_uploaded.mime_type = "application/octet-stream"
        mock_uploaded.name = "files/abc456"
        mock_client.files.upload.return_value = mock_uploaded

        result = await server._process_file_content(
            "data.bin", content_b64, session, mime_type="application/octet-stream"
        )
        assert result.mime_type == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_deduplication(self, server, session):
        """Test that uploading the same file name returns cached result."""
        content_b64 = base64.b64encode(b"data").decode()

        mock_uploaded = MagicMock()
        mock_uploaded.state = "ACTIVE"
        mock_uploaded.uri = "https://gemini.example.com/files/abc789"
        mock_uploaded.mime_type = "text/x-python"
        mock_uploaded.name = "files/abc789"
        mock_client.files.upload.return_value = mock_uploaded

        result1 = await server._process_file_content("dup.py", content_b64, session)

        # Reset call count
        mock_client.files.upload.reset_mock()

        # Upload again with same name — should return cached
        result2 = await server._process_file_content("dup.py", content_b64, session)

        assert result1 is result2
        mock_client.files.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_temp_file_cleanup(self, server, session):
        """Test that temporary files are cleaned up after upload."""
        content_b64 = base64.b64encode(b"temporary data").decode()

        mock_uploaded = MagicMock()
        mock_uploaded.state = "ACTIVE"
        mock_uploaded.uri = "https://gemini.example.com/files/tmp1"
        mock_uploaded.mime_type = "text/plain"
        mock_uploaded.name = "files/tmp1"
        mock_client.files.upload.return_value = mock_uploaded

        # Track the temp file path
        original_upload = mock_client.files.upload
        uploaded_path = None

        def capture_path(**kwargs):
            nonlocal uploaded_path
            uploaded_path = kwargs.get('file')
            return mock_uploaded

        mock_client.files.upload.side_effect = capture_path

        await server._process_file_content("temp.txt", content_b64, session)

        # Temp file should have been cleaned up
        if uploaded_path:
            assert not os.path.exists(uploaded_path), "Temp file should be deleted after upload"

        mock_client.files.upload.side_effect = None


class TestPendingFiles:
    """Tests for pending file tracking in Session."""

    def test_session_initializes_empty_pending(self):
        """Test that new sessions have empty pending_files."""
        session = Session(
            session_id="test",
            chat=MagicMock(),
            created=datetime.now(),
            last_used=datetime.now(),
            message_count=0,
        )
        assert session.pending_files == []

    @pytest.mark.asyncio
    async def test_upload_adds_to_pending(self, server, session):
        """Test that _process_file_content adds to pending_files."""
        content_b64 = base64.b64encode(b"data").decode()

        mock_uploaded = MagicMock()
        mock_uploaded.state = "ACTIVE"
        mock_uploaded.uri = "https://gemini.example.com/files/pend1"
        mock_uploaded.mime_type = "text/plain"
        mock_uploaded.name = "files/pend1"
        mock_client.files.upload.return_value = mock_uploaded

        assert len(session.pending_files) == 0

        await server._process_file_content("file1.txt", content_b64, session)
        assert "file1.txt" in session.pending_files

    def test_pending_files_clear(self, session):
        """Test that pending_files can be cleared."""
        session.pending_files.append("a.py")
        session.pending_files.append("b.py")
        assert len(session.pending_files) == 2

        session.pending_files.clear()
        assert len(session.pending_files) == 0


class TestProcessFile:
    """Tests for the refactored _process_file method."""

    @pytest.mark.asyncio
    async def test_file_not_found(self, server, session):
        """Test that missing files raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="File not found"):
            await server._process_file("/nonexistent/path/file.py", session)

    @pytest.mark.asyncio
    async def test_cached_file_returned(self, server, session):
        """Test that already-processed files are returned from cache."""
        cached = ProcessedFile(
            file_type="file_data",
            file_uri="https://cached",
            mime_type="text/plain",
            file_name="cached.txt",
            file_path="/some/cached.txt",
            gemini_file_id="files/cached",
        )
        session.processed_files["/some/cached.txt"] = cached

        result = await server._process_file("/some/cached.txt", session)
        assert result is cached


def _load_module_with_transport(transport_value):
    """Helper: load gemini_mcp.py with a specific MCP_TRANSPORT value."""
    env_patch = patch.dict(os.environ, {"MCP_TRANSPORT": transport_value, "GEMINI_API_KEY": "test-key"})
    client_patch = patch('google.genai.Client', return_value=MagicMock())
    with env_patch, client_patch:
        spec = importlib.util.spec_from_file_location(
            f"gemini_mcp_{transport_value}", "gemini_mcp.py"
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    return m


class TestUploadFileConditionalRegistration:
    """Tests that upload_file is only available in SSE mode."""

    def _get_tool_names(self, module):
        """Return the set of tool names registered on the module's FastMCP instance."""
        return {t.name for t in module.mcp._tool_manager.list_tools()}

    def test_upload_file_registered_in_sse_mode(self):
        """upload_file tool should be registered when MCP_TRANSPORT=sse."""
        m = _load_module_with_transport("sse")
        tool_names = self._get_tool_names(m)
        assert "upload_file" in tool_names, (
            f"upload_file should be registered in SSE mode, got: {tool_names}"
        )

    def test_upload_file_not_registered_in_stdio_mode(self):
        """upload_file tool should NOT be registered when MCP_TRANSPORT=stdio."""
        m = _load_module_with_transport("stdio")
        tool_names = self._get_tool_names(m)
        assert "upload_file" not in tool_names, (
            f"upload_file should NOT be registered in stdio mode, got: {tool_names}"
        )

    def test_upload_file_not_registered_by_default(self):
        """upload_file tool should NOT be registered when MCP_TRANSPORT is unset (defaults to stdio)."""
        env_patch = patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False)
        # Make sure MCP_TRANSPORT is not set
        env_copy = os.environ.copy()
        env_copy.pop("MCP_TRANSPORT", None)
        env_patch2 = patch.dict(os.environ, env_copy, clear=True)
        client_patch = patch('google.genai.Client', return_value=MagicMock())
        with env_patch2, client_patch:
            os.environ["GEMINI_API_KEY"] = "test-key"
            spec = importlib.util.spec_from_file_location(
                "gemini_mcp_default", "gemini_mcp.py"
            )
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
        tool_names = self._get_tool_names(m)
        assert "upload_file" not in tool_names, (
            f"upload_file should NOT be registered by default (stdio), got: {tool_names}"
        )

    def test_core_tools_always_registered(self):
        """consult_gemini, list_sessions, end_session should be registered in both modes."""
        for transport in ("stdio", "sse"):
            m = _load_module_with_transport(transport)
            tool_names = self._get_tool_names(m)
            for expected in ("consult_gemini", "get_gemini_requests", "list_sessions", "end_session"):
                assert expected in tool_names, (
                    f"{expected} should be registered in {transport} mode, got: {tool_names}"
                )
