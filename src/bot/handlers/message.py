"""Message handlers for non-command Slack events."""

import asyncio
from typing import Optional

import structlog

from ...claude.exceptions import ClaudeToolValidationError
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.rate_limiter import RateLimiter
from ...security.validators import SecurityValidator
from ..utils.slack_format import escape_mrkdwn

logger = structlog.get_logger()


def _get_user_state(deps: dict, user_id: str) -> dict:
    """Get per-user state dict from deps, creating if needed."""
    user_states = deps.setdefault("_user_states", {})
    return user_states.setdefault(user_id, {})


async def _format_progress_update(update_obj) -> Optional[str]:
    """Format progress updates with enhanced context and visual indicators."""
    if update_obj.type == "tool_result":
        tool_name = "Unknown"
        if update_obj.metadata and update_obj.metadata.get("tool_use_id"):
            tool_name = update_obj.metadata.get("tool_name", "Tool")

        if update_obj.is_error():
            return f":x: *{tool_name} failed*\n\n_{update_obj.get_error_message()}_"
        else:
            execution_time = ""
            if update_obj.metadata and update_obj.metadata.get("execution_time_ms"):
                time_ms = update_obj.metadata["execution_time_ms"]
                execution_time = f" ({time_ms}ms)"
            return f":white_check_mark: *{tool_name} completed*{execution_time}"

    elif update_obj.type == "progress":
        progress_text = f":arrows_counterclockwise: *{update_obj.content or 'Working...'}*"

        percentage = update_obj.get_progress_percentage()
        if percentage is not None:
            filled = int(percentage / 10)
            bar = "\u2588" * filled + "\u2591" * (10 - filled)
            progress_text += f"\n\n`{bar}` {percentage}%"

        if update_obj.progress:
            step = update_obj.progress.get("step")
            total_steps = update_obj.progress.get("total_steps")
            if step and total_steps:
                progress_text += f"\n\nStep {step} of {total_steps}"

        return progress_text

    elif update_obj.type == "error":
        return f":x: *Error*\n\n_{update_obj.get_error_message()}_"

    elif update_obj.type == "assistant" and update_obj.tool_calls:
        tool_names = update_obj.get_tool_names()
        if tool_names:
            tools_text = ", ".join(tool_names)
            return f":wrench: *Using tools:* {tools_text}"

    elif update_obj.type == "assistant" and update_obj.content:
        content_preview = (
            update_obj.content[:150] + "..."
            if len(update_obj.content) > 150
            else update_obj.content
        )
        return f":robot_face: *Claude is working...*\n\n_{content_preview}_"

    elif update_obj.type == "system":
        if update_obj.metadata and update_obj.metadata.get("subtype") == "init":
            tools_count = len(update_obj.metadata.get("tools", []))
            model = update_obj.metadata.get("model", "Claude")
            return f":rocket: *Starting {model}* with {tools_count} tools available"

    return None


def _format_error_message(error_str: str) -> str:
    """Format error messages for user-friendly display."""
    if "usage limit reached" in error_str.lower():
        return error_str
    elif "tool not allowed" in error_str.lower():
        return error_str
    elif "no conversation found" in error_str.lower():
        return (
            ":arrows_counterclockwise: *Session Not Found*\n\n"
            "The Claude session could not be found or has expired.\n\n"
            "*What you can do:*\n"
            "- Use /new to start a fresh session\n"
            "- Try your request again\n"
            "- Use /status to check your current session"
        )
    elif "rate limit" in error_str.lower():
        return (
            ":stopwatch: *Rate Limit Reached*\n\n"
            "Too many requests in a short time period.\n\n"
            "*What you can do:*\n"
            "- Wait a moment before trying again\n"
            "- Use simpler requests\n"
            "- Check your current usage with /status"
        )
    elif "timeout" in error_str.lower():
        return (
            ":alarm_clock: *Request Timeout*\n\n"
            "Your request took too long to process and timed out.\n\n"
            "*What you can do:*\n"
            "- Try breaking down your request into smaller parts\n"
            "- Use simpler commands\n"
            "- Try again in a moment"
        )
    else:
        safe_error = escape_mrkdwn(error_str)
        if len(safe_error) > 200:
            safe_error = safe_error[:200] + "..."

        return (
            f":x: *Claude Code Error*\n\n"
            f"Failed to process your request: {safe_error}\n\n"
            f"Please try again or contact the administrator if the problem persists."
        )


async def handle_text_message(event, say, client, context) -> None:
    """Handle regular text messages as Claude prompts."""
    user_id = event["user"]
    message_text = event["text"]
    channel_id = event["channel"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    user_state = _get_user_state(deps, user_id)

    # Get services
    rate_limiter: Optional[RateLimiter] = deps.get("rate_limiter")
    audit_logger: Optional[AuditLogger] = deps.get("audit_logger")

    logger.info(
        "Processing text message", user_id=user_id, message_length=len(message_text)
    )

    try:
        # Check rate limit with estimated cost for text processing
        estimated_cost = _estimate_text_processing_cost(message_text)

        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(
                user_id, estimated_cost
            )
            if not allowed:
                await say(f":stopwatch: {limit_message}")
                return

        # Create progress message
        progress_msg = await say(
            ":thinking_face: Processing your request..."
        )
        progress_ts = progress_msg["ts"]

        # Get Claude integration and storage from deps
        claude_integration = deps.get("claude_integration")
        storage = deps.get("storage")

        if not claude_integration:
            await say(
                ":x: *Claude integration not available*\n\n"
                "The Claude Code integration is not properly configured. "
                "Please contact the administrator."
            )
            return

        # Get current directory
        current_dir = user_state.get(
            "current_directory", settings.approved_directory
        )

        # Get existing session ID
        session_id = user_state.get("claude_session_id")

        # Check if /new was used -- skip auto-resume for this first message.
        force_new = bool(user_state.get("force_new_session"))

        # Enhanced stream updates handler with progress tracking
        async def stream_handler(update_obj):
            try:
                progress_text = await _format_progress_update(update_obj)
                if progress_text:
                    await client.chat_update(
                        channel=channel_id,
                        ts=progress_ts,
                        text=progress_text,
                    )
            except Exception as e:
                logger.warning("Failed to update progress message", error=str(e))

        # Run Claude command
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=stream_handler,
                force_new=force_new,
            )

            # New session created successfully -- clear the one-shot flag
            if force_new:
                user_state["force_new_session"] = False

            # Update session ID
            user_state["claude_session_id"] = claude_response.session_id

            # Check if Claude changed the working directory and update our tracking
            _update_working_directory_from_claude_response(
                claude_response, user_state, settings, user_id
            )

            # Log interaction to storage
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction to storage", error=str(e))

            # Format response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        except ClaudeToolValidationError as e:
            logger.error(
                "Tool validation error",
                error=str(e),
                user_id=user_id,
                blocked_tools=e.blocked_tools,
            )
            from ..utils.formatting import FormattedMessage

            formatted_messages = [FormattedMessage(str(e), parse_mode=None)]
        except Exception as e:
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from ..utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(str(e)), parse_mode=None)
            ]

        # Delete progress message
        try:
            await client.chat_delete(channel=channel_id, ts=progress_ts)
        except Exception:
            pass

        # Send formatted responses (may be multiple messages)
        for i, message in enumerate(formatted_messages):
            try:
                await say(message.text)

                # Small delay between messages to avoid rate limits
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.warning(
                    "Failed to send response",
                    error=str(e),
                    message_index=i,
                )
                try:
                    await say(message.text)
                except Exception:
                    await say(
                        ":x: Failed to send response. Please try again."
                    )

        # Update session info
        user_state["last_message"] = message_text

        # Add conversation enhancements if available
        features = deps.get("features")
        conversation_enhancer = (
            features.get_conversation_enhancer() if features else None
        )

        if conversation_enhancer and claude_response:
            try:
                conversation_context = conversation_enhancer.update_context(
                    session_id=claude_response.session_id,
                    user_id=user_id,
                    working_directory=str(current_dir),
                    tools_used=claude_response.tools_used or [],
                    response_content=claude_response.content,
                )

                if conversation_enhancer.should_show_suggestions(
                    claude_response.tools_used or [], claude_response.content
                ):
                    suggestions = conversation_enhancer.generate_follow_up_suggestions(
                        claude_response.content,
                        claude_response.tools_used or [],
                        conversation_context,
                    )

                    if suggestions:
                        # Create Block Kit buttons for suggestions
                        elements = []
                        for suggestion in suggestions:
                            elements.append(
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": suggestion.label,
                                    },
                                    "action_id": f"followup:{suggestion.hash}",
                                    "value": suggestion.hash,
                                }
                            )

                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": ":bulb: *What would you like to do next?*",
                                },
                            },
                            {
                                "type": "actions",
                                "elements": elements[:25],
                            },
                        ]

                        await say(
                            text=":bulb: What would you like to do next?",
                            blocks=blocks,
                        )

            except Exception as e:
                logger.warning(
                    "Conversation enhancement failed", error=str(e), user_id=user_id
                )

        # Log successful message processing
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=True,
            )

        logger.info("Text message processed successfully", user_id=user_id)

    except Exception as e:
        # Clean up progress message if it exists
        try:
            if "progress_ts" in locals():
                await client.chat_delete(channel=channel_id, ts=progress_ts)
        except Exception:
            pass

        error_msg = f":x: *Error processing message*\n\n{escape_mrkdwn(str(e))}"
        await say(error_msg)

        # Log failed processing
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=False,
            )

        logger.error("Error processing text message", error=str(e), user_id=user_id)


async def handle_file_share(event, say, client, context) -> None:
    """Handle file uploads shared in Slack."""
    user_id = event["user"]
    channel_id = event["channel"]
    deps = context.get("deps", {})
    settings: Settings = deps["settings"]
    user_state = _get_user_state(deps, user_id)

    # Get services
    security_validator: Optional[SecurityValidator] = deps.get("security_validator")
    audit_logger: Optional[AuditLogger] = deps.get("audit_logger")
    rate_limiter: Optional[RateLimiter] = deps.get("rate_limiter")

    # Get files from event
    files = event.get("files", [])
    if not files:
        return

    document = files[0]
    filename = document.get("name", "unknown")
    file_size = document.get("size", 0)

    logger.info(
        "Processing file upload",
        user_id=user_id,
        filename=filename,
        file_size=file_size,
    )

    try:
        # Validate filename using security validator
        if security_validator:
            valid, error = security_validator.validate_filename(filename)
            if not valid:
                await say(
                    f":x: *File Upload Rejected*\n\n{escape_mrkdwn(error)}"
                )

                if audit_logger:
                    await audit_logger.log_security_violation(
                        user_id=user_id,
                        violation_type="invalid_file_upload",
                        details=f"Filename: {filename}, Error: {error}",
                        severity="medium",
                    )
                return

        # Check file size limits
        max_size = 10 * 1024 * 1024  # 10MB
        if file_size > max_size:
            await say(
                f":x: *File Too Large*\n\n"
                f"Maximum file size: {max_size // 1024 // 1024}MB\n"
                f"Your file: {file_size / 1024 / 1024:.1f}MB"
            )
            return

        # Check rate limit for file processing
        file_cost = _estimate_file_processing_cost(file_size)
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(
                user_id, file_cost
            )
            if not allowed:
                await say(f":stopwatch: {limit_message}")
                return

        progress_msg = await say(
            f":page_facing_up: Processing file: `{filename}`..."
        )
        progress_ts = progress_msg["ts"]

        # Download file content from Slack
        file_url = document.get("url_private_download") or document.get("url_private")
        if not file_url:
            await client.chat_update(
                channel=channel_id,
                ts=progress_ts,
                text=":x: *File Download Failed*\n\nCould not retrieve file URL.",
            )
            return

        # Download file using Slack client
        import aiohttp

        headers = {"Authorization": f"Bearer {client.token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, headers=headers) as resp:
                if resp.status != 200:
                    await client.chat_update(
                        channel=channel_id,
                        ts=progress_ts,
                        text=":x: *File Download Failed*\n\nCould not download the file.",
                    )
                    return
                file_bytes = await resp.read()

        # Try to decode as text
        try:
            content = file_bytes.decode("utf-8")

            max_content_length = 50000
            if len(content) > max_content_length:
                content = (
                    content[:max_content_length]
                    + "\n... (file truncated for processing)"
                )

            caption = event.get("text", "") or "Please review this file:"
            prompt = f"{caption}\n\n**File:** `{filename}`\n\n```\n{content}\n```"

        except UnicodeDecodeError:
            await client.chat_update(
                channel=channel_id,
                ts=progress_ts,
                text=(
                    ":x: *File Format Not Supported*\n\n"
                    "File must be text-based and UTF-8 encoded.\n\n"
                    "*Supported formats:*\n"
                    "- Source code files (.py, .js, .ts, etc.)\n"
                    "- Text files (.txt, .md)\n"
                    "- Configuration files (.json, .yaml, .toml)\n"
                    "- Documentation files"
                ),
            )
            return

        # Delete progress message
        try:
            await client.chat_delete(channel=channel_id, ts=progress_ts)
        except Exception:
            pass

        # Create a new progress message for Claude processing
        claude_progress_msg = await say(":robot_face: Processing file with Claude...")
        claude_progress_ts = claude_progress_msg["ts"]

        # Get Claude integration from deps
        claude_integration = deps.get("claude_integration")

        if not claude_integration:
            await client.chat_update(
                channel=channel_id,
                ts=claude_progress_ts,
                text=(
                    ":x: *Claude integration not available*\n\n"
                    "The Claude Code integration is not properly configured."
                ),
            )
            return

        # Get current directory and session
        current_dir = user_state.get("current_directory", settings.approved_directory)
        session_id = user_state.get("claude_session_id")

        # Process with Claude
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
            )

            # Update session ID
            user_state["claude_session_id"] = claude_response.session_id

            # Check if Claude changed the working directory
            _update_working_directory_from_claude_response(
                claude_response, user_state, settings, user_id
            )

            # Format and send response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            # Delete progress message
            try:
                await client.chat_delete(channel=channel_id, ts=claude_progress_ts)
            except Exception:
                pass

            # Send responses
            for i, message in enumerate(formatted_messages):
                await say(message.text)

                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            await client.chat_update(
                channel=channel_id,
                ts=claude_progress_ts,
                text=_format_error_message(str(e)),
            )
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)

        # Log successful file processing
        if audit_logger:
            await audit_logger.log_file_access(
                user_id=user_id,
                file_path=filename,
                action="upload_processed",
                success=True,
                file_size=file_size,
            )

    except Exception as e:
        try:
            if "progress_ts" in locals():
                await client.chat_delete(channel=channel_id, ts=progress_ts)
        except Exception:
            pass

        error_msg = f":x: *Error processing file*\n\n{escape_mrkdwn(str(e))}"
        await say(error_msg)

        if audit_logger:
            await audit_logger.log_file_access(
                user_id=user_id,
                file_path=filename,
                action="upload_failed",
                success=False,
                file_size=file_size,
            )

        logger.error("Error processing document", error=str(e), user_id=user_id)


def _estimate_text_processing_cost(text: str) -> float:
    """Estimate cost for processing text message."""
    base_cost = 0.001
    length_cost = len(text) * 0.00001

    complex_keywords = [
        "analyze",
        "generate",
        "create",
        "build",
        "implement",
        "refactor",
        "optimize",
        "debug",
        "explain",
        "document",
    ]

    text_lower = text.lower()
    complexity_multiplier = 1.0

    for keyword in complex_keywords:
        if keyword in text_lower:
            complexity_multiplier += 0.5

    return (base_cost + length_cost) * min(complexity_multiplier, 3.0)


def _estimate_file_processing_cost(file_size: int) -> float:
    """Estimate cost for processing uploaded file."""
    base_cost = 0.005
    size_cost = (file_size / 1024) * 0.0001
    return base_cost + size_cost


def _update_working_directory_from_claude_response(
    claude_response, user_state, settings, user_id
):
    """Update the working directory based on Claude's response content."""
    import re
    from pathlib import Path

    patterns = [
        r"(?:^|\n).*?cd\s+([^\s\n]+)",
        r"(?:^|\n).*?Changed directory to:?\s*([^\s\n]+)",
        r"(?:^|\n).*?Current directory:?\s*([^\s\n]+)",
        r"(?:^|\n).*?Working directory:?\s*([^\s\n]+)",
    ]

    content = claude_response.content.lower()
    current_dir = user_state.get("current_directory", settings.approved_directory)

    for pattern in patterns:
        matches = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            try:
                new_path = match.strip().strip("\"'`")

                if new_path.startswith("./") or new_path.startswith("../"):
                    new_path = (current_dir / new_path).resolve()
                elif not new_path.startswith("/"):
                    new_path = (current_dir / new_path).resolve()
                else:
                    new_path = Path(new_path).resolve()

                if (
                    new_path.is_relative_to(settings.approved_directory)
                    and new_path.exists()
                ):
                    user_state["current_directory"] = new_path
                    logger.info(
                        "Updated working directory from Claude response",
                        old_dir=str(current_dir),
                        new_dir=str(new_path),
                        user_id=user_id,
                    )
                    return

            except (ValueError, OSError) as e:
                logger.debug(
                    "Invalid path in Claude response", path=match, error=str(e)
                )
                continue
