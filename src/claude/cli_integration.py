"""Claude Code CLI subprocess integration.

Alternative backend that spawns the ``claude`` CLI as an async subprocess
and parses its ``--output-format stream-json`` output.  Activated by setting
``USE_SDK=false`` in the environment.

Features:
- Async subprocess execution with streaming JSON output
- Memory-bounded output reading (64KB chunks)
- Process lifecycle management (tracking, timeout, cleanup)
- Session resume via ``--resume <session_id>``
"""

import asyncio
import json
import uuid
from asyncio.subprocess import Process
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .exceptions import ClaudeTimeoutError
from .sdk_integration import ClaudeResponse, StreamUpdate, find_claude_cli

logger = structlog.get_logger()

# Buffer limits
_MAX_MESSAGE_BUFFER = 1000
_STREAM_CHUNK_SIZE = 65536  # 64 KB


class ClaudeProcessManager:
    """Manage Claude Code via CLI subprocess execution."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self.active_processes: Dict[str, Process] = {}

    # ------------------------------------------------------------------
    # Public API (matches ClaudeSDKManager.execute_command signature)
    # ------------------------------------------------------------------

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Execute a Claude Code command via CLI subprocess.

        Parameters match ``ClaudeSDKManager.execute_command`` so the two
        backends are interchangeable inside ``ClaudeIntegration``.
        """
        start_time = asyncio.get_event_loop().time()
        execution_id = str(uuid.uuid4())[:8]

        logger.info(
            "Starting Claude CLI command",
            execution_id=execution_id,
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        cmd = self._build_command(prompt, session_id, continue_session)
        process = await self._start_process(cmd, working_directory)
        self.active_processes[execution_id] = process

        try:
            response = await asyncio.wait_for(
                self._handle_process_output(process, stream_callback, start_time),
                timeout=self.config.claude_timeout_seconds,
            )
            return response
        except asyncio.TimeoutError:
            logger.error(
                "Claude CLI command timed out",
                execution_id=execution_id,
                timeout=self.config.claude_timeout_seconds,
            )
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise ClaudeTimeoutError(
                f"Command timed out after {self.config.claude_timeout_seconds}s"
            )
        except Exception:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise
        finally:
            self.active_processes.pop(execution_id, None)

    async def kill_all_processes(self) -> None:
        """Kill every tracked subprocess."""
        for eid, proc in list(self.active_processes.items()):
            try:
                proc.kill()
                logger.info("Killed CLI process", execution_id=eid)
            except ProcessLookupError:
                pass
        self.active_processes.clear()

    def get_active_process_count(self) -> int:
        """Return the number of active subprocesses."""
        return len(self.active_processes)

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _build_command(
        self,
        prompt: str,
        session_id: Optional[str],
        continue_session: bool,
    ) -> List[str]:
        """Build the ``claude`` CLI argument list."""
        cli_path = (
            find_claude_cli(self.config.claude_cli_path)
            or find_claude_cli(getattr(self.config, "claude_binary_path", None))
            or "claude"
        )

        cmd: List[str] = [cli_path]

        # Session handling
        if continue_session and session_id and prompt:
            cmd.extend(["--resume", session_id, "-p", prompt])
        elif continue_session and not prompt:
            cmd.append("--continue")
            if session_id:
                cmd.extend(["--resume", session_id])
        elif prompt:
            cmd.extend(["-p", prompt])
        else:
            cmd.extend(["-p", ""])

        # Output format
        cmd.extend(["--output-format", "stream-json", "--verbose"])

        # Limits
        cmd.extend(["--max-turns", str(self.config.claude_max_turns)])

        # Tool restrictions
        if self.config.claude_allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self.config.claude_allowed_tools)])
        if self.config.claude_disallowed_tools:
            cmd.extend(
                ["--disallowedTools", ",".join(self.config.claude_disallowed_tools)]
            )

        # MCP configuration
        if self.config.enable_mcp and self.config.mcp_config_path:
            cmd.extend(["--mcp-config", str(self.config.mcp_config_path)])

        return cmd

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------

    async def _start_process(self, cmd: List[str], cwd: Path) -> Process:
        """Start the CLI subprocess with stdout/stderr pipes."""
        logger.debug("Starting CLI process", cmd=cmd, cwd=str(cwd))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        return process

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------

    async def _handle_process_output(
        self,
        process: Process,
        stream_callback: Optional[Callable[[StreamUpdate], None]],
        start_time: float,
    ) -> ClaudeResponse:
        """Read streaming JSON from the process, invoke callbacks, return result."""
        messages: deque[Dict[str, Any]] = deque(maxlen=_MAX_MESSAGE_BUFFER)
        result_data: Optional[Dict[str, Any]] = None

        assert process.stdout is not None  # guaranteed by _start_process

        async for line in self._read_stream_bounded(process.stdout):
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON line", line=line[:200])
                continue

            msg_type = msg.get("type", "")
            messages.append(msg)

            if msg_type == "result":
                result_data = msg
                continue

            # Deliver streaming update
            if stream_callback:
                update = self._parse_stream_message(msg)
                if update:
                    try:
                        if asyncio.iscoroutinefunction(stream_callback):
                            await stream_callback(update)
                        else:
                            stream_callback(update)
                    except Exception as cb_err:
                        logger.warning(
                            "Stream callback error",
                            error=str(cb_err),
                        )

        # Wait for the process to finish
        stderr_bytes = b""
        if process.stderr:
            stderr_bytes = await process.stderr.read()
        await process.wait()

        duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

        # Handle non-zero exit
        if process.returncode and process.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            return self._handle_process_error(
                process.returncode, stderr_text, list(messages), duration_ms
            )

        # Parse the final result
        return self._parse_result(result_data, list(messages), duration_ms)

    async def _read_stream_bounded(
        self, stream: asyncio.StreamReader
    ) -> AsyncIterator[str]:
        """Yield decoded lines from an async stream with bounded reads."""
        buffer = ""
        while True:
            chunk = await stream.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                # Flush remaining buffer
                if buffer:
                    yield buffer
                break

            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    def _parse_stream_message(self, msg: Dict[str, Any]) -> Optional[StreamUpdate]:
        """Convert a stream-json message dict into a ``StreamUpdate``."""
        msg_type = msg.get("type", "")

        if msg_type == "assistant":
            content_blocks = msg.get("message", {}).get("content", [])
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []

            for block in content_blocks:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "tool_name": block.get("name", ""),
                                "tool_id": block.get("id", ""),
                                "input": block.get("input", {}),
                            }
                        )
                elif isinstance(block, str):
                    text_parts.append(block)

            return StreamUpdate(
                type="assistant",
                content="\n".join(text_parts) if text_parts else None,
                tool_calls=tool_calls or None,
            )

        if msg_type == "user":
            content = msg.get("message", {}).get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            return StreamUpdate(type="user", content=str(content))

        if msg_type == "system":
            subtype = msg.get("subtype", "")
            return StreamUpdate(
                type="system",
                content=msg.get("message", ""),
                metadata={
                    "subtype": subtype,
                    "tools": msg.get("tools", []),
                    "model": msg.get("model", ""),
                    "mcp_servers": msg.get("mcp_servers", []),
                },
            )

        if msg_type == "tool_result":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            return StreamUpdate(
                type="tool_result",
                content=str(content),
                metadata={
                    "tool_use_id": msg.get("tool_use_id", ""),
                    "is_error": msg.get("is_error", False),
                },
            )

        if msg_type == "error":
            return StreamUpdate(
                type="error",
                content=msg.get("error", {}).get("message", str(msg)),
                metadata={
                    "code": msg.get("error", {}).get("code", ""),
                    "subtype": msg.get("subtype", ""),
                },
            )

        # Unknown types — log and skip
        logger.debug("Skipping unknown stream message type", msg_type=msg_type)
        return None

    # ------------------------------------------------------------------
    # Result extraction
    # ------------------------------------------------------------------

    def _parse_result(
        self,
        result_data: Optional[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        duration_ms: int,
    ) -> ClaudeResponse:
        """Build a ``ClaudeResponse`` from the final result message."""
        if result_data:
            content = result_data.get("result", "") or ""
            session_id = result_data.get("session_id", "")
            cost = result_data.get("cost_usd", 0.0) or 0.0
            num_turns = result_data.get("num_turns", 0) or 0
            is_error = result_data.get("is_error", False)
            tools_used = self._extract_tools_from_messages(messages)
        else:
            # Fallback: collect text from assistant messages
            content = self._extract_content_fallback(messages)
            session_id = ""
            cost = 0.0
            num_turns = 0
            is_error = False
            tools_used = self._extract_tools_from_messages(messages)

        return ClaudeResponse(
            content=content,
            session_id=session_id,
            cost=cost,
            duration_ms=duration_ms,
            num_turns=num_turns,
            is_error=is_error,
            tools_used=tools_used,
        )

    def _handle_process_error(
        self,
        returncode: int,
        stderr_text: str,
        messages: List[Dict[str, Any]],
        duration_ms: int,
    ) -> ClaudeResponse:
        """Handle non-zero exit code from the CLI process."""
        logger.error(
            "Claude CLI exited with error",
            returncode=returncode,
            stderr=stderr_text[:500],
        )

        # Detect specific error types
        lower_stderr = stderr_text.lower()

        if "usage limit" in lower_stderr or "rate limit" in lower_stderr:
            error_type = "usage_limit"
            content = "Claude usage limit reached. Please wait before trying again."
        elif "mcp" in lower_stderr:
            error_type = "mcp_error"
            content = f"MCP server error: {stderr_text[:300]}"
        else:
            error_type = "process_error"
            # Try to salvage content from assistant messages
            content = self._extract_content_fallback(messages)
            if not content:
                content = f"CLI error (exit code {returncode}): {stderr_text[:300]}"

        return ClaudeResponse(
            content=content,
            session_id="",
            cost=0.0,
            duration_ms=duration_ms,
            num_turns=0,
            is_error=True,
            error_type=error_type,
            tools_used=self._extract_tools_from_messages(messages),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_content_fallback(messages: List[Dict[str, Any]]) -> str:
        """Collect text from assistant messages as a content fallback."""
        parts: List[str] = []
        for msg in messages:
            if msg.get("type") != "assistant":
                continue
            for block in msg.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
        return "\n".join(parts)

    @staticmethod
    def _extract_tools_from_messages(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Extract tool usage records from collected messages."""
        tools: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("type") != "assistant":
                continue
            for block in msg.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools.append(
                        {
                            "tool_name": block.get("name", ""),
                            "tool_id": block.get("id", ""),
                            "input": block.get("input", {}),
                        }
                    )
        return tools
