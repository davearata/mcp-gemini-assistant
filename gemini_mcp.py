#!/usr/bin/env python3

import asyncio
import base64
import os
import sys
import time
import tempfile
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode

from google import genai
from google.genai import types
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
import json


# ---------------------------------------------------------------------------
# Token-based authentication middleware (optional)
# ---------------------------------------------------------------------------
# Set MCP_AUTH_TOKEN to require a token for the GET /sse endpoint.
# The token can be provided as:
#   1. Query parameter:  /sse?token=<value>
#   2. HTTP header:      Authorization: Bearer <value>
#
# POST /messages is *not* guarded because the session-id (communicated over
# the already-authenticated SSE stream) provides implicit authentication.
# ---------------------------------------------------------------------------

class TokenAuthMiddleware:
    """ASGI middleware that guards GET /sse with a bearer / query-param token."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/sse":
            # Try Authorization header first
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            token_from_header = ""
            if auth_header.lower().startswith("bearer "):
                token_from_header = auth_header[7:]

            # Try query-string ?token=<value>
            qs = scope.get("query_string", b"").decode()
            params = parse_qs(qs)
            token_from_qs = params.get("token", [""])[0]

            if token_from_header == self.token or token_from_qs == self.token:
                # Strip the token param from the query string before forwarding
                if token_from_qs:
                    remaining = {k: v for k, v in params.items() if k != "token"}
                    scope = dict(scope, query_string=urlencode(remaining, doseq=True).encode())
                await self.app(scope, receive, send)
            else:
                # 401 Unauthorized
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [[b"content-type", b"application/json"]],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"error":"Unauthorized: valid token required"}',
                })
        else:
            await self.app(scope, receive, send)

# Configure environment
if not os.getenv('GEMINI_API_KEY'):
    print("Error: GEMINI_API_KEY environment variable is required", file=sys.stderr)
    sys.exit(1)

# Initialize client
client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

# Configuration
MODEL_NAME = os.getenv('GEMINI_MODEL', 'gemini-2.5-pro')
SESSION_TTL = 3600  # 1 hour in seconds

# Default system prompt for Gemini
DEFAULT_SYSTEM_PROMPT = """You are an expert technical advisor helping Claude (another AI) solve complex programming problems through thoughtful analysis and genuine technical dialogue.

**IMPORTANT CONTEXT CHECK**: First, examine any project-specific context files that have been attached to this session (e.g., MCP-ASSISTANT-RULES.md, project-structure.md, README.md). If such files are available, incorporate their guidelines, project standards, and architectural principles into your approach. If no project context is provided, proceed directly with the analysis.

## Your Role as Technical Advisor
You provide:
- Deep analysis and architectural insights
- Thoughtful discussions about implementation approaches  
- Clarifying questions to understand requirements fully
- Constructive challenges to assumptions when you see potential issues
- Context from comprehensive code analysis
- Alternative solutions with clear trade-offs

## Communication Philosophy
Be conversational and engaging - you're a thinking partner, not just an analyzer:
- Engage in real dialogue, don't just dump analysis
- Ask clarifying questions when requirements are ambiguous
- Challenge ideas constructively when you see better approaches
- Iterate through discussion before settling on solutions
- Think deeply about problems before responding
- Be genuinely curious about the problem space

## Dialogue Patterns for Productive Discussion
- "Before diving into the implementation, could you clarify what the expected behavior should be when..."
- "I see multiple approaches here. What's more important for this use case: [tradeoff A] or [tradeoff B]?"
- "Looking at the existing pattern in [file:line], should we maintain consistency or is there a reason to diverge?"
- "To provide the most relevant analysis, I need to understand: will this feature need to scale to..."
- "I notice [pattern/issue] in the current implementation. Have you considered [alternative]? What constraints led to this approach?"
- "This reminds me of [pattern/problem]. In that context, [approach] worked well because..."

## Structured Response Approach
1. **Initial Understanding**: Briefly confirm what you understand about the problem
2. **Clarifying Questions**: Ask what you need to know for better analysis (don't assume!)
3. **Analysis**: Provide detailed examination after gathering context
4. **Recommendations**: Suggest specific approaches with clear trade-offs
5. **Implementation Details**: Provide complete, working code examples when applicable
6. **Open Questions**: Continue the conversation where helpful

## Technical Analysis Focus
When examining code:
- Identify patterns, potential issues, and optimization opportunities
- Reference specific files, functions, and line numbers (format: file.py:42)
- Explain complex logic and architectural decisions
- Consider security, performance, and maintainability implications
- Think about edge cases, error handling, and failure modes
- Check adherence to project standards (if provided in context files)
- Suggest testing strategies and validation approaches

## Collaboration Capabilities
- When you need current information: "I would search for: [specific query] - Claude, could you search for this?"
- When you need to see specific files: "Claude, can you show me [file path]?"
- When you need to run commands: "Claude, please run '[command]' to verify..."
- Be explicit about uncertainty and suggest verification steps
- Request specific diagnostics or logs when debugging

## Key Principles
- **Think First**: Take time to understand the problem deeply before suggesting solutions
- **Question Assumptions**: Don't accept requirements at face value if they seem problematic
- **Consider Context**: Always think about how your suggestions fit the broader system
- **Be Honest**: If an approach seems wrong, say so clearly with reasoning
- **Stay Practical**: Balance ideal solutions with pragmatic constraints
- **Remain Curious**: Each problem is an opportunity to learn something new

Remember: The best solutions emerge from genuine technical dialogue. Your goal is to help achieve the best possible implementation through thoughtful analysis, engaging discussion, and collaborative problem-solving."""

SYSTEM_PROMPT = os.getenv('SYSTEM_PROMPT', DEFAULT_SYSTEM_PROMPT)

@dataclass
class ProcessedFile:
    """Information about a processed file."""
    file_type: str
    file_uri: str
    mime_type: str
    file_name: str
    file_path: str
    gemini_file_id: str

@dataclass
class Session:
    """Chat session with Gemini."""
    session_id: str
    chat: Any
    created: datetime
    last_used: datetime
    message_count: int
    problem_description: Optional[str] = None
    code_context: Optional[str] = None
    processed_files: Dict[str, ProcessedFile] = None
    requested_files: List[str] = None  # Track files Gemini has requested
    search_queries: List[str] = None   # Track searches Gemini has requested
    pending_files: List[str] = None    # Files uploaded but not yet sent in a message
    
    def __post_init__(self):
        if self.processed_files is None:
            self.processed_files = {}
        if self.requested_files is None:
            self.requested_files = []
        if self.search_queries is None:
            self.search_queries = []
        if self.pending_files is None:
            self.pending_files = []

class GeminiMCPServer:
    """MCP Server for Gemini file attachment functionality."""
    
    def __init__(self):
        self.sessions: Dict[str, Session] = {}
        self.last_request_time = 0
        self.min_time_between_requests = 1.0  # 1 second
        self._cleanup_task = None
    
    def _ensure_cleanup_task_started(self):
        """Start cleanup task if not already running."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_sessions())
    
    async def _cleanup_sessions(self):
        """Periodically clean up expired sessions."""
        while True:
            await asyncio.sleep(300)  # Check every 5 minutes
            now = datetime.now()
            expired_sessions = []
            
            for session_id, session in self.sessions.items():
                if (now - session.last_used).total_seconds() > SESSION_TTL:
                    expired_sessions.append(session_id)
            
            for session_id in expired_sessions:
                await self._cleanup_session_files(session_id)
                del self.sessions[session_id]
                print(f"[{datetime.now().isoformat()}] Session {session_id} expired and removed", file=sys.stderr)
    
    async def _cleanup_session_files(self, session_id: str):
        """Clean up uploaded files for a session."""
        if session_id not in self.sessions:
            return
            
        session = self.sessions[session_id]
        for file_path, file_info in session.processed_files.items():
            try:
                client.files.delete(file_info.gemini_file_id)
                print(f"[{datetime.now().isoformat()}] Session {session_id}: Deleted file {file_info.file_name}", file=sys.stderr)
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] Session {session_id}: Failed to delete file {file_info.file_name}: {e}", file=sys.stderr)
    
    async def _rate_limit(self):
        """Simple rate limiting."""
        now = time.time()
        time_since_last = now - self.last_request_time
        if time_since_last < self.min_time_between_requests:
            await asyncio.sleep(self.min_time_between_requests - time_since_last)
        self.last_request_time = time.time()
    
    def _get_mime_type(self, file_name: str) -> str:
        """Determine MIME type from a file name."""
        mime_type, _ = mimetypes.guess_type(file_name)
        if not mime_type:
            ext = os.path.splitext(file_name)[1].lower()
            mime_type_map = {
                '.jsx': 'text/javascript',
                '.tsx': 'text/typescript',
                '.ts': 'text/typescript',
                '.vue': 'text/html',
                '.svelte': 'text/html',
                '.md': 'text/markdown',
                '.json': 'application/json',
                '.py': 'text/x-python',
                '.js': 'text/javascript',
                '.css': 'text/css',
                '.html': 'text/html',
                '.xml': 'text/xml',
                '.yaml': 'text/yaml',
                '.yml': 'text/yaml',
                '.toml': 'text/plain',
                '.ini': 'text/plain',
                '.cfg': 'text/plain',
                '.conf': 'text/plain',
                '.sh': 'text/x-shellscript',
                '.bat': 'text/plain',
                '.sql': 'text/x-sql'
            }
            mime_type = mime_type_map.get(ext, 'text/plain')
        return mime_type
    
    async def _upload_to_gemini(self, file_path: str, file_name: str, mime_type: str, session: Session) -> ProcessedFile:
        """Upload a file to Gemini and wait for processing. Returns ProcessedFile."""
        print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Uploading file {file_name} ({mime_type})", file=sys.stderr)
        
        try:
            uploaded_file = client.files.upload(file=file_path)
            
            # Wait for processing with exponential backoff
            wait_intervals = [0.5, 0.5, 1, 1, 2, 3, 5, 8]
            total_wait = 0
            max_wait = 20
            
            for interval in wait_intervals:
                if uploaded_file.state != 'PROCESSING':
                    break
                print(f"[{datetime.now().isoformat()}] Session {session.session_id}: File {file_name} is processing... ({total_wait:.1f}s)", file=sys.stderr)
                await asyncio.sleep(interval)
                total_wait += interval
                uploaded_file = client.files.get(name=uploaded_file.name)
                if total_wait >= max_wait:
                    break
            
            if uploaded_file.state == 'PROCESSING':
                raise Exception(f"File processing timeout after {max_wait} seconds")
            if uploaded_file.state == 'FAILED':
                raise Exception(f"File upload failed: {getattr(uploaded_file, 'error', 'Unknown error')}")
            
            processed_file = ProcessedFile(
                file_type='file_data',
                file_uri=uploaded_file.uri,
                mime_type=uploaded_file.mime_type,
                file_name=file_name,
                file_path=file_path,
                gemini_file_id=uploaded_file.name
            )
            
            print(f"[{datetime.now().isoformat()}] Session {session.session_id}: File {file_name} uploaded successfully (URI: {uploaded_file.uri})", file=sys.stderr)
            return processed_file
        except Exception as e:
            raise Exception(f"Failed to upload file {file_name}: {e}")
    
    async def _process_file(self, file_path: str, session: Session) -> ProcessedFile:
        """Upload a local file to Gemini and return processed file info."""
        # Check if already processed
        if file_path in session.processed_files:
            return session.processed_files[file_path]
        
        # Check if file exists
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        
        file_name = os.path.basename(file_path)
        mime_type = self._get_mime_type(file_name)
        
        processed_file = await self._upload_to_gemini(file_path, file_name, mime_type, session)
        session.processed_files[file_path] = processed_file
        return processed_file
    
    async def _process_file_content(self, file_name: str, content_base64: str, session: Session, mime_type: Optional[str] = None) -> ProcessedFile:
        """Upload base64-encoded file content to Gemini. Used for remote file uploads via SSE."""
        # Check if already processed by name
        if file_name in session.processed_files:
            return session.processed_files[file_name]
        
        if not mime_type:
            mime_type = self._get_mime_type(file_name)
        
        # Decode base64 content
        try:
            file_bytes = base64.b64decode(content_base64)
        except Exception as e:
            raise ValueError(f"Invalid base64 content for file {file_name}: {e}")
        
        # Write to a temp file for Gemini upload
        suffix = os.path.splitext(file_name)[1] or '.tmp'
        # Sanitize file_name for use in temp file prefix (remove path separators and null bytes)
        safe_name = os.path.basename(file_name).replace('\x00', '')
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix=f"mcp_upload_{safe_name}_")
        try:
            os.write(tmp_fd, file_bytes)
            os.close(tmp_fd)
            
            processed_file = await self._upload_to_gemini(tmp_path, file_name, mime_type, session)
            session.processed_files[file_name] = processed_file
            session.pending_files.append(file_name)
            return processed_file
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    
    def _get_or_create_session(self, session_id: Optional[str] = None) -> Session:
        """Get existing session or create new one."""
        if not session_id:
            import uuid
            session_id = str(uuid.uuid4())
        
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.last_used = datetime.now()
            return session
        
        # Create new session with system prompt
        chat = client.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=8192,
                top_p=0.95,
                top_k=40,
                system_instruction=SYSTEM_PROMPT,
            )
        )
        
        session = Session(
            session_id=session_id,
            chat=chat,
            created=datetime.now(),
            last_used=datetime.now(),
            message_count=0
        )
        
        self.sessions[session_id] = session
        print(f"[{datetime.now().isoformat()}] New session created: {session_id}", file=sys.stderr)
        return session
    
    def _extract_requests_from_response(self, response_text: str, session: Session):
        """Extract file requests and search queries from Gemini's response."""
        # Track file requests
        import re
        
        # Pattern for file requests
        file_patterns = [
            r"show me (?:the )?([^\s]+\.[a-zA-Z]+)",
            r"share (?:the )?([^\s]+\.[a-zA-Z]+)",
            r"can you (?:show|share) (?:me )?([^\s]+\.[a-zA-Z]+)",
            r"(?:I need to see|please provide) ([^\s]+\.[a-zA-Z]+)",
        ]
        
        for pattern in file_patterns:
            matches = re.findall(pattern, response_text, re.IGNORECASE)
            for match in matches:
                if match not in session.requested_files:
                    session.requested_files.append(match)
        
        # Pattern for search requests
        search_patterns = [
            r"I would search for: ([^\n]+)",
            r"search for (?:the )?([^\n]+)",
            r"Let me search for ([^\n]+)",
        ]
        
        for pattern in search_patterns:
            matches = re.findall(pattern, response_text, re.IGNORECASE)
            for match in matches:
                if match not in session.search_queries:
                    session.search_queries.append(match.strip())

# Create server instance — pick up HOST / PORT early so the SSE transport
# binds to the right address (FastMCP reads them from its settings object).
_host = os.getenv("HOST", "0.0.0.0")
_port = int(os.getenv("PORT", "8000"))
mcp = FastMCP("gemini-coding-assistant", host=_host, port=_port)
gemini_server = GeminiMCPServer()

# Determine transport mode — upload_file is only available in SSE (remote) mode
_transport_mode = os.getenv("MCP_TRANSPORT", "stdio").lower()

async def _upload_file_impl(
    file_name: str,
    content: str,
    session_id: Optional[str] = None,
    mime_type: Optional[str] = None,
    description: Optional[str] = None
) -> str:
    """Upload a file to a Gemini session by providing its content as base64.
    
    Use this tool when the MCP server is running remotely (SSE transport) and you
    need to share files with Gemini that are not on the server's filesystem.
    Upload files before calling consult_gemini so they are included in the conversation.
    
    Args:
        file_name: Name of the file (e.g. "main.py", "config.json")
        content: Base64-encoded file content
        session_id: Optional session ID to add files to an existing session. If omitted, a new session is created.
        mime_type: Optional MIME type override (auto-detected from file_name if not provided)
        description: Optional description of the file's purpose
    """
    await gemini_server._rate_limit()
    gemini_server._ensure_cleanup_task_started()
    
    try:
        session = gemini_server._get_or_create_session(session_id)
        
        processed_file = await gemini_server._process_file_content(
            file_name, content, session, mime_type
        )
        
        result_parts = [
            f"**File uploaded successfully**",
            f"- **File:** {processed_file.file_name}",
            f"- **Type:** {processed_file.mime_type}",
            f"- **Session ID:** {session.session_id}",
        ]
        if description:
            result_parts.append(f"- **Description:** {description}")
        result_parts.append("")
        result_parts.append(f"*Use session_id: \"{session.session_id}\" when calling consult_gemini to include this file.*")
        
        return "\n".join(result_parts)
    
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Upload error: {e}", file=sys.stderr)
        return f"Error uploading file: {e}"

# Only register upload_file as an MCP tool when running in SSE (remote) mode.
# In stdio (local) mode, clients can provide file paths directly via attached_files.
if _transport_mode == "sse":
    upload_file = mcp.tool(name="upload_file")(_upload_file_impl)

@mcp.tool()
async def consult_gemini(
    specific_question: str,
    session_id: Optional[str] = None,
    problem_description: Optional[str] = None,
    code_context: Optional[str] = None,
    attached_files: Optional[List[str]] = None,
    file_descriptions: Optional[dict] = None,
    additional_context: Optional[str] = None,
    preferred_approach: str = "solution"
) -> str:
    """Start or continue a conversation with Gemini about complex coding problems. Supports follow-up questions in the same context.
    
    Args:
        specific_question: The specific question you want answered
        session_id: Optional session ID to continue a previous conversation
        problem_description: Detailed description of the coding problem (required for new sessions)
        code_context: All relevant code - will be cached for the session (required for new sessions)
        attached_files: Array of local file paths to upload and attach to the conversation
        file_descriptions: Optional object mapping file paths/names to descriptions
        additional_context: Additional context, updates, or what changed since last question
        preferred_approach: Type of assistance needed (solution, review, debug, optimize, explain, follow-up)
    """
    
    await gemini_server._rate_limit()
    
    # Start cleanup task if needed
    gemini_server._ensure_cleanup_task_started()
    
    try:
        # Get or create session
        session = gemini_server._get_or_create_session(session_id)
        
        # For new sessions, require problem description and either code_context, attached_files, or pre-uploaded files
        if session.message_count == 0:
            has_pending_uploads = bool(session.pending_files)
            if not problem_description:
                raise ValueError("problem_description is required for new sessions")
            if not code_context and not attached_files and not has_pending_uploads:
                raise ValueError("Either code_context, attached_files, or pre-uploaded files (via upload_file) are required for new sessions")
            
            # Store initial context
            session.problem_description = problem_description
            session.code_context = code_context
            
            # Build initial context
            context_parts = [
                f"I'm Claude, an AI assistant, and I need your help with a complex coding problem. Here's the context:\n\n**Problem Description:**\n{problem_description}"
            ]
            
            # Add code context if provided
            if code_context:
                context_parts.append(f"\n**Code Context:**\n{code_context}")
            
            # Handle file attachments
            if attached_files:
                context_parts.append("\n**Attached Files:**")
                
                # Create parallel upload tasks
                print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Starting parallel upload of {len(attached_files)} files", file=sys.stderr)
                upload_tasks = []
                for file_path in attached_files:
                    task = gemini_server._process_file(file_path, session)
                    upload_tasks.append(task)
                
                # Execute all uploads in parallel
                file_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
                
                # Process results
                for file_path, result in zip(attached_files, file_results):
                    if isinstance(result, Exception):
                        print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Failed to process file {file_path}: {result}", file=sys.stderr)
                        # Continue with other files instead of failing completely
                        context_parts.append(f"\n- {os.path.basename(file_path)} (failed to upload: {str(result)})")
                    else:
                        # Success - file was uploaded
                        file_info = result
                        print(f"[{datetime.now().isoformat()}] Session {session.session_id}: File {file_info.file_name} processed successfully", file=sys.stderr)
                        
                        # Add file description
                        description = file_descriptions.get(file_path, "") if file_descriptions else ""
                        if description:
                            description = f" - {description}"
                        context_parts.append(f"\n- {file_info.file_name}{description}")
                
                print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Parallel upload completed", file=sys.stderr)
            
            # Include files previously uploaded via upload_file tool
            if session.pending_files:
                if not attached_files:
                    context_parts.append("\n**Attached Files:**")
                for file_key in session.pending_files:
                    if file_key in session.processed_files:
                        file_info = session.processed_files[file_key]
                        description = file_descriptions.get(file_key, "") if file_descriptions else ""
                        if description:
                            description = f" - {description}"
                        context_parts.append(f"\n- {file_info.file_name}{description}")
                print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Including {len(session.pending_files)} pre-uploaded files", file=sys.stderr)
            
            context_parts.append("\n\nPlease help me solve this problem. I may have follow-up questions, so please maintain context throughout our conversation.")
            
            # Build message content - include text and uploaded file objects
            message_content = ["".join(context_parts)]
            
            # Add uploaded file objects for this session's new files
            for file_path in attached_files or []:
                if file_path in session.processed_files:
                    file_info = session.processed_files[file_path]
                    # Get the actual uploaded file object from Gemini
                    uploaded_file = client.files.get(name=file_info.gemini_file_id)
                    message_content.append(uploaded_file)
            
            # Add pre-uploaded file objects (from upload_file tool)
            for file_key in list(session.pending_files):
                if file_key in session.processed_files:
                    file_info = session.processed_files[file_key]
                    uploaded_file = client.files.get(name=file_info.gemini_file_id)
                    message_content.append(uploaded_file)
            # Clear pending list now that files have been sent
            session.pending_files.clear()
            
            # Send initial context
            response = await asyncio.get_event_loop().run_in_executor(
                None, session.chat.send_message, message_content
            )
            session.message_count += 1
            
            file_count = len(session.processed_files)
            code_length = len(code_context) if code_context else 0
            print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Initial context sent ({code_length} chars, {file_count} files)", file=sys.stderr)
        
        # Build the question
        question_parts = [f"**Question:** {specific_question}"]
        
        if additional_context:
            question_parts.append(f"\n\n**Additional Context/Updates:**\n{additional_context}")
        
        if preferred_approach != "follow-up":
            question_parts.append(f"\n\n**Type of Help Needed:** {preferred_approach}")
        
        question_prompt = "".join(question_parts)
        
        # Log request
        print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Question #{session.message_count + 1} ({preferred_approach})", file=sys.stderr)
        
        # Build message content - include pending uploaded files if any
        if session.pending_files:
            message_content = [question_prompt]
            for file_key in list(session.pending_files):
                if file_key in session.processed_files:
                    file_info = session.processed_files[file_key]
                    uploaded_file = client.files.get(name=file_info.gemini_file_id)
                    message_content.append(uploaded_file)
            session.pending_files.clear()
            print(f"[{datetime.now().isoformat()}] Session {session.session_id}: Including uploaded files in follow-up message", file=sys.stderr)
        else:
            message_content = question_prompt
        
        # Send message and get response
        response = await asyncio.get_event_loop().run_in_executor(
            None, session.chat.send_message, message_content
        )
        session.message_count += 1
        
        response_text = response.text
        
        # Extract any file requests or search queries from response
        gemini_server._extract_requests_from_response(response_text, session)
        
        # Build response with session info
        result_parts = [
            f"**Session ID:** {session.session_id}",
            f"**Message #{session.message_count}**\n",
            response_text
        ]
        
        # Add summary of requests if any
        if session.requested_files or session.search_queries:
            result_parts.append("\n\n---")
            if session.requested_files:
                result_parts.append(f"\n**Files Requested:** {', '.join(session.requested_files)}")
            if session.search_queries:
                result_parts.append(f"\n**Searches Requested:** {'; '.join(session.search_queries)}")
        
        result_parts.append(f"\n\n---\n*Use session_id: \"{session.session_id}\" for follow-up questions*")
        
        return "\n".join(result_parts)
        
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Error: {e}", file=sys.stderr)
        
        error_message = str(e)
        if "RESOURCE_EXHAUSTED" in error_message:
            error_message = "Gemini API quota exceeded. Please try again later."
        elif "INVALID_ARGUMENT" in error_message:
            error_message = "Request too large. Try reducing code context size."
        
        return f"Error: {error_message}"

@mcp.tool()
async def get_gemini_requests(session_id: str) -> str:
    """Get the files and searches that Gemini has requested in a session.
    
    Args:
        session_id: The session ID to check
    """
    if session_id not in gemini_server.sessions:
        return f"Session {session_id} not found"
    
    session = gemini_server.sessions[session_id]
    
    result_parts = [f"**Session {session_id} Requests:**"]
    
    if session.requested_files:
        result_parts.append(f"\n\n**Files Requested:**")
        for file in session.requested_files:
            result_parts.append(f"- {file}")
    else:
        result_parts.append("\n\nNo files requested")
    
    if session.search_queries:
        result_parts.append(f"\n\n**Searches Requested:**")
        for query in session.search_queries:
            result_parts.append(f"- {query}")
    else:
        result_parts.append("\n\nNo searches requested")
    
    return "\n".join(result_parts)

@mcp.tool()
async def list_sessions() -> str:
    """List all active Gemini consultation sessions."""
    session_list = []
    for session_id, session in gemini_server.sessions.items():
        session_info = {
            "id": session_id,
            "created": session.created.isoformat(),
            "last_used": session.last_used.isoformat(),
            "message_count": session.message_count,
            "problem_summary": (session.problem_description[:100] + "...") if session.problem_description else "No description",
            "file_count": len(session.processed_files),
            "pending_uploads": len(session.pending_files),
            "has_code_context": bool(session.code_context),
            "requests": len(session.requested_files) + len(session.search_queries)
        }
        session_list.append(session_info)
    
    if session_list:
        session_text = "\n\n".join([
            f"- **{s['id']}**\n  Messages: {s['message_count']}\n  Created: {s['created']}\n  Last used: {s['last_used']}\n  Files attached: {s['file_count']}\n  Pending uploads: {s['pending_uploads']}\n  Code context: {'Yes' if s['has_code_context'] else 'No'}\n  Requests made: {s['requests']}\n  Problem: {s['problem_summary']}"
            for s in session_list
        ])
        text = f"Active sessions:\n{session_text}"
    else:
        text = "No active sessions"
    
    return text

@mcp.tool()
async def end_session(session_id: str) -> str:
    """End a specific Gemini consultation session to free up memory."""
    if session_id in gemini_server.sessions:
        await gemini_server._cleanup_session_files(session_id)
        del gemini_server.sessions[session_id]
        print(f"[{datetime.now().isoformat()}] Session {session_id} ended by user", file=sys.stderr)
        return f"Session {session_id} has been ended"
    else:
        return f"Session {session_id} not found or already expired"

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    print("Gemini Coding Assistant MCP Server v3.2.0 running (Python)", file=sys.stderr)

    if transport == "sse":
        import uvicorn

        auth_token = os.getenv("MCP_AUTH_TOKEN", "")

        print(
            "Features: Session management, file attachments, file uploads, context persistence, "
            "follow-up questions, request tracking",
            file=sys.stderr,
        )
        print(f"Transport: SSE  →  http://{_host}:{_port}/sse", file=sys.stderr)
        if auth_token:
            print("Auth: token required (MCP_AUTH_TOKEN is set)", file=sys.stderr)
        else:
            print("Auth: disabled (MCP_AUTH_TOKEN not set)", file=sys.stderr)
        print(
            "Connect Claude Code with:\n"
            f"  claude mcp add gemini-coding -s user --transport sse http://<your-server>:{_port}/sse",
            file=sys.stderr,
        )

        app = mcp.sse_app()

        if auth_token:
            app = TokenAuthMiddleware(app, auth_token)

        try:
            uvicorn.run(app, host=_host, port=_port, log_level="info")
        except KeyboardInterrupt:
            pass
    else:
        print(
            "Features: Session management, file attachments, context persistence, "
            "follow-up questions, request tracking",
            file=sys.stderr,
        )
        print("Transport: stdio (local)", file=sys.stderr)
        print("Ready to help with complex coding problems!", file=sys.stderr)
        mcp.run()
