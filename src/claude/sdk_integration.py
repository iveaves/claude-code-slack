"""Claude Code Python SDK integration.

Features:
- Native Claude Code SDK integration
- Async streaming support
- Tool execution management
- Session persistence
"""

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    Message,
    ProcessError,
    ResultMessage,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError

from ..config.settings import Settings
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)

logger = structlog.get_logger()


def find_claude_cli(claude_cli_path: Optional[str] = None) -> Optional[str]:
    """Find Claude CLI in common locations."""
    import glob
    import shutil

    # First check if a specific path was provided via config or env
    if claude_cli_path:
        if os.path.exists(claude_cli_path) and os.access(claude_cli_path, os.X_OK):
            return claude_cli_path

    # Check CLAUDE_CLI_PATH environment variable
    env_path = os.environ.get("CLAUDE_CLI_PATH")
    if env_path and os.path.exists(env_path) and os.access(env_path, os.X_OK):
        return env_path

    # Check if claude is already in PATH
    claude_path = shutil.which("claude")
    if claude_path:
        return claude_path

    # Check common installation locations
    common_paths = [
        # NVM installations
        os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
        # Direct npm global install
        os.path.expanduser("~/.npm-global/bin/claude"),
        os.path.expanduser("~/node_modules/.bin/claude"),
        # System locations
        "/usr/local/bin/claude",
        "/usr/bin/claude",
        # Windows locations (for cross-platform support)
        os.path.expanduser("~/AppData/Roaming/npm/claude.cmd"),
    ]

    for pattern in common_paths:
        matches = glob.glob(pattern)
        if matches:
            # Return the first match
            return matches[0]

    return None


def update_path_for_claude(claude_cli_path: Optional[str] = None) -> bool:
    """Update PATH to include Claude CLI if found."""
    claude_path = find_claude_cli(claude_cli_path)

    if claude_path:
        # Add the directory containing claude to PATH
        claude_dir = os.path.dirname(claude_path)
        current_path = os.environ.get("PATH", "")

        if claude_dir not in current_path:
            os.environ["PATH"] = f"{claude_dir}:{current_path}"
            logger.info("Updated PATH for Claude CLI", claude_path=claude_path)

        return True

    return False


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    type: str  # 'assistant', 'user', 'system', 'result'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    metadata: Optional[Dict] = None


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    def __init__(self, config: Settings):
        """Initialize SDK manager with configuration."""
        self.config = config

        # Try to find and update PATH for Claude CLI
        if not update_path_for_claude(config.claude_cli_path):
            logger.warning(
                "Claude CLI not found in PATH or common locations. "
                "SDK may fail if Claude is not installed or not in PATH."
            )

        # Unset env vars inherited from a parent Claude Code session that
        # would interfere with spawning a child Claude process.
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)

        # Set up environment for Claude Code SDK if API key is explicitly
        # provided in .env (not inherited from a parent session).
        # Read directly from .env to avoid picking up the parent session's key.
        explicit_key = self._read_env_file_key("ANTHROPIC_API_KEY", config)
        if explicit_key:
            os.environ["ANTHROPIC_API_KEY"] = explicit_key
            logger.info("Using API key from .env for Claude SDK authentication")
        else:
            # No API key — Claude CLI must be logged in via `claude login`.
            # If scheduled jobs or agents fail with auth errors, verify CLI
            # auth is valid by running: claude --version
            logger.info(
                "No API key in .env, using Claude CLI authentication "
                "(ensure `claude login` has been run)"
            )

    @staticmethod
    def _read_env_file_key(key: str, config: Settings) -> Optional[str]:
        """Read a key directly from the .env file, ignoring inherited env vars."""
        env_path = Path(".env")
        if not env_path.exists():
            return None
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key and v.strip():
                    return v.strip()
        except OSError:
            pass
        return None

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        ask_user_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        scheduler_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        file_upload_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK."""
        start_time = asyncio.get_event_loop().time()

        # Ensure nesting guard env vars are cleared before every subprocess
        # spawn (not just __init__), since the parent session may re-inject them.
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("CLAUDE_CODE", None)

        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            # Build Claude Agent options
            cli_path = find_claude_cli(self.config.claude_cli_path)

            # Build can_use_tool callback — only used for AskUserQuestion
            # (needs permission-level input injection). All other custom tools
            # are registered as real MCP tools via create_bot_mcp_server().
            async def _can_use_tool(
                tool_name: str,
                tool_input: Dict[str, Any],
                context: Any,
            ) -> Any:
                from claude_agent_sdk.types import (
                    PermissionResultAllow,
                    PermissionResultDeny,
                )

                if tool_name == "AskUserQuestion" and ask_user_callback:
                    try:
                        answers = await ask_user_callback(tool_input)
                        if answers:
                            updated = dict(tool_input)
                            updated["answers"] = answers
                            return PermissionResultAllow(
                                behavior="allow",
                                updated_input=updated,
                                updated_permissions=None,
                            )
                    except Exception as e:
                        logger.warning(
                            "AskUserQuestion callback failed, denying",
                            error=str(e),
                        )
                        return PermissionResultDeny(
                            behavior="deny",
                            message=f"Failed to get user input: {e}",
                            interrupt=False,
                        )

                # Allow all other tools (bypassPermissions handles the rest)
                return PermissionResultAllow(
                    behavior="allow",
                    updated_input=None,
                    updated_permissions=None,
                )

            # Capture stderr from Claude CLI for debugging
            stderr_lines: List[str] = []

            def _capture_stderr(line: str) -> None:
                stderr_lines.append(line)
                logger.debug("Claude CLI stderr", line=line.strip())

            options = ClaudeAgentOptions(
                max_turns=self.config.claude_max_turns,
                cwd=str(working_directory),
                allowed_tools=self.config.claude_allowed_tools,
                disallowed_tools=self.config.claude_disallowed_tools,
                cli_path=cli_path,
                permission_mode="bypassPermissions",
                can_use_tool=_can_use_tool,
                model=self.config.claude_model,
                # Load user + project + local settings so skills are discovered
                setting_sources=["user", "project", "local"],
                sandbox={
                    "enabled": self.config.sandbox_enabled,
                    "autoAllowBashIfSandboxed": True,
                    "excludedCommands": self.config.sandbox_excluded_commands or [],
                },
                system_prompt=self._build_system_prompt(working_directory),
                stderr=_capture_stderr,
            )

            # Register bot-specific tools as a real MCP server
            from .mcp_tools import create_bot_mcp_server

            mcp_servers: Dict[str, Any] = {}

            # Load user-configured MCP servers if enabled
            if self.config.enable_mcp and self.config.mcp_config_path:
                mcp_servers = self._load_mcp_config(self.config.mcp_config_path)
                logger.info(
                    "MCP servers configured",
                    mcp_config_path=str(self.config.mcp_config_path),
                )

            # Add bot tools MCP server (only if there are callbacks to wire up)
            if file_upload_callback or scheduler_callback:
                bot_server = create_bot_mcp_server(
                    file_upload_fn=file_upload_callback,
                    scheduler_fn=scheduler_callback,
                )
                mcp_servers["slack-bot-tools"] = bot_server

            if mcp_servers:
                options.mcp_servers = mcp_servers

            # Resume previous session if we have a session_id
            if session_id and continue_session:
                options.resume = session_id
                logger.info(
                    "Resuming previous session",
                    session_id=session_id,
                )

            # Collect messages via ClaudeSDKClient
            messages: List[Message] = []

            async def _run_client() -> None:
                async with ClaudeSDKClient(options) as client:
                    await client.query(prompt)
                    response_iter = client.receive_response()
                    while True:
                        try:
                            message = await response_iter.__anext__()
                        except StopAsyncIteration:
                            break
                        except MessageParseError as e:
                            # Skip unknown message types (e.g. rate_limit_event)
                            # rather than failing the entire request
                            logger.debug(
                                "Skipping unparseable message",
                                error=str(e),
                            )
                            continue

                        messages.append(message)

                        # Handle streaming callback
                        if stream_callback:
                            try:
                                await self._handle_stream_message(
                                    message, stream_callback
                                )
                            except Exception as callback_error:
                                logger.warning(
                                    "Stream callback failed",
                                    error=str(callback_error),
                                    error_type=type(callback_error).__name__,
                                )

            # Execute with timeout
            await asyncio.wait_for(
                _run_client(),
                timeout=self.config.claude_timeout_seconds,
            )

            # Extract cost, tools, and session_id from result message
            cost = 0.0
            tools_used: List[Dict[str, Any]] = []
            claude_session_id = None
            result_content = None
            for message in messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    claude_session_id = getattr(message, "session_id", None)
                    result_content = getattr(message, "result", None)
                    tools_used = self._extract_tools_from_messages(messages)
                    break

            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Use Claude's session_id if available, otherwise fall back
            final_session_id = claude_session_id or session_id or ""

            if claude_session_id and claude_session_id != session_id:
                logger.info(
                    "Got session ID from Claude",
                    claude_session_id=claude_session_id,
                    previous_session_id=session_id,
                )

            # Use ResultMessage.result if non-empty, fall back to message
            # extraction.  With subscription/CLI auth the SDK may return
            # result="" (empty string) even though AssistantMessage objects
            # contain the actual response text.
            content = (
                result_content
                if result_content
                else self._extract_content_from_messages(messages)
            )

            if not content:
                logger.warning(
                    "Empty content after extraction",
                    result_field=repr(result_content),
                    message_count=len(messages),
                    message_types=[type(m).__name__ for m in messages],
                )

            # Clean ThinkingBlock wrapper remnants from final content
            # (thinking is shown in progress, not in final output)
            if content and "ThinkingBlock(" in content:
                import re as _re

                content = _re.sub(
                    r"\[?ThinkingBlock\(thinking=['\"]?(.*?)['\"]?\)\]?",
                    "",
                    content,
                    flags=_re.DOTALL,
                ).strip()

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=len(
                    [
                        m
                        for m in messages
                        if isinstance(m, (UserMessage, AssistantMessage))
                    ]
                ),
                tools_used=tools_used,
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK command timed out",
                timeout_seconds=self.config.claude_timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Claude SDK timed out after {self.config.claude_timeout_seconds}s"
            )

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code\n\n"
                "If already installed, try one of these:\n"
                "  1. Add Claude to your PATH\n"
                "  2. Create a symlink: ln -s $(which claude) /usr/local/bin/claude\n"
                "  3. Set CLAUDE_CLI_PATH environment variable"
            )
            raise ClaudeProcessError(error_msg)

        except ProcessError as e:
            error_str = str(e)
            stderr_output = (
                "\n".join(stderr_lines) if stderr_lines else "(no stderr captured)"
            )
            logger.error(
                "Claude process failed",
                error=error_str,
                exit_code=getattr(e, "exit_code", None),
                stderr=stderr_output,
            )
            # Check if the process error is MCP-related
            if "mcp" in error_str.lower():
                raise ClaudeMCPError(f"MCP server error: {error_str}")
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            error_str = str(e)
            logger.error("Claude connection error", error=error_str)
            # Check if the connection error is MCP-related
            if "mcp" in error_str.lower() or "server" in error_str.lower():
                raise ClaudeMCPError(f"MCP server connection failed: {error_str}")
            raise ClaudeProcessError(f"Failed to connect to Claude: {error_str}")

        except CLIJSONDecodeError as e:
            logger.error("Claude SDK JSON decode error", error=str(e))
            raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

        except ClaudeSDKError as e:
            logger.error("Claude SDK error", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

        except Exception as e:
            # Handle ExceptionGroup from TaskGroup operations (Python 3.11+)
            if type(e).__name__ == "ExceptionGroup" or hasattr(e, "exceptions"):
                logger.error(
                    "Task group error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                    exception_count=len(getattr(e, "exceptions", [])),
                    exceptions=[
                        str(ex) for ex in getattr(e, "exceptions", [])[:3]
                    ],  # Log first 3 exceptions
                )
                # Extract the most relevant exception from the group
                exceptions = getattr(e, "exceptions", [e])
                main_exception = exceptions[0] if exceptions else e
                raise ClaudeProcessError(
                    f"Claude SDK task error: {str(main_exception)}"
                )

            # Check if it's an ExceptionGroup disguised as a regular exception
            elif hasattr(e, "__notes__") and "TaskGroup" in str(e):
                logger.error(
                    "TaskGroup related error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise ClaudeProcessError(f"Claude SDK task error: {str(e)}")

            else:
                logger.error(
                    "Unexpected error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise ClaudeProcessError(f"Unexpected error: {str(e)}")

    async def _handle_stream_message(
        self, message: Message, stream_callback: Callable[[StreamUpdate], None]
    ) -> None:
        """Handle streaming message from claude-agent-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                content = getattr(message, "content", [])
                text_parts = []
                tool_calls = []

                if content and isinstance(content, list):
                    thinking_parts = []
                    for block in content:
                        block_type = getattr(block, "type", "")
                        if block_type == "thinking":
                            # Extract clean thinking text
                            thinking_text = getattr(block, "thinking", None) or getattr(
                                block, "text", None
                            )
                            if thinking_text:
                                thinking_parts.append(thinking_text)
                            continue
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(
                                {
                                    "name": getattr(block, "name", "unknown"),
                                    "input": getattr(block, "input", {}),
                                    "id": getattr(block, "id", None),
                                }
                            )
                        elif hasattr(block, "text"):
                            text_parts.append(block.text)

                    # Send thinking as a separate stream update
                    if thinking_parts and stream_callback:
                        thinking_update = StreamUpdate(
                            type="thinking",
                            content="\n".join(thinking_parts),
                        )
                        await stream_callback(thinking_update)

                if text_parts or tool_calls:
                    update = StreamUpdate(
                        type="assistant",
                        content=("\n".join(text_parts) if text_parts else None),
                        tool_calls=tool_calls if tool_calls else None,
                    )
                    await stream_callback(update)
                elif content:
                    # Fallback for non-list content
                    content_str = str(content)
                    # Clean ThinkingBlock wrapper if present
                    import re as _re

                    content_str = _re.sub(
                        r"\[?ThinkingBlock\(thinking=['\"]?(.*?)['\"]?\)\]?",
                        r"\1",
                        content_str,
                    ).strip()
                    if content_str:
                        update = StreamUpdate(
                            type="assistant",
                            content=content_str,
                        )
                        await stream_callback(update)

            elif isinstance(message, UserMessage):
                content = getattr(message, "content", "")
                if content:
                    update = StreamUpdate(
                        type="user",
                        content=content,
                    )
                    await stream_callback(update)

        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    def _extract_content_from_messages(self, messages: List[Message]) -> str:
        """Extract content from message list."""
        content_parts = []

        for message in messages:
            if isinstance(message, AssistantMessage):
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    for block in content:
                        block_type = getattr(block, "type", "")
                        # Skip tool use blocks — they're tracked separately
                        if isinstance(block, ToolUseBlock):
                            continue
                        # Skip thinking blocks from final output (shown in progress only)
                        if block_type == "thinking":
                            continue
                        if hasattr(block, "text"):
                            content_parts.append(block.text)
                elif content:
                    # Fallback for non-list content
                    content_parts.append(str(content))

        return "\n".join(content_parts)

    def _extract_tools_from_messages(
        self, messages: List[Message]
    ) -> List[Dict[str, Any]]:
        """Extract tools used from message list."""
        tools_used = []
        current_time = asyncio.get_event_loop().time()

        for message in messages:
            if isinstance(message, AssistantMessage):
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock):
                            tools_used.append(
                                {
                                    "name": getattr(block, "name", "unknown"),
                                    "timestamp": current_time,
                                    "input": getattr(block, "input", {}),
                                }
                            )

        return tools_used

    def _build_system_prompt(self, working_directory: Path) -> str:
        """Build system prompt with context about the bot environment."""
        return (
            f"All file operations must stay within {working_directory}. "
            "Use relative paths.\n\n"
            "IMPORTANT: You are running as a Slack bot agent. "
            "You do NOT have access to paths outside the working directory. "
            "When creating skills/commands, ALWAYS use the project-scoped "
            f"directory at {working_directory}/.claude/commands/ or "
            f"{working_directory}/.claude/skills/ "
            "(NOT ~/.claude/). "
            "This is your only writable .claude directory.\n\n"
            "To send files or images to the user, use the SlackFileUpload tool. "
            "To schedule recurring tasks, use the ScheduleJob tool."
        )

    def _load_mcp_config(self, config_path: Path) -> Dict[str, Any]:
        """Load MCP server configuration from a JSON file.

        The new claude-agent-sdk expects mcp_servers as a dict, not a file path.
        """
        import json

        try:
            with open(config_path) as f:
                config_data = json.load(f)
            return config_data.get("mcpServers", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load MCP config", path=str(config_path), error=str(e)
            )
            return {}

    def get_active_process_count(self) -> int:
        """Get number of active sessions (always 0, per-request clients)."""
        return 0
