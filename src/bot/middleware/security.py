"""Security middleware for input validation and threat detection."""

import re
from typing import Any, Callable

import structlog

logger = structlog.get_logger()


def _extract_user_id(body: dict, event: Any) -> str | None:
    """Extract Slack user ID from body or event."""
    user_id = body.get("user_id")
    if user_id:
        return str(user_id)
    if isinstance(body.get("user"), dict):
        user_id = body["user"].get("id")
        if user_id:
            return str(user_id)
    if isinstance(event, dict):
        return event.get("user")
    return None


async def slack_security_middleware(
    body: dict, event: Any, context: dict, next: Callable
) -> None:
    """Validate inputs and detect security threats.

    This middleware:
    1. Validates message content for dangerous patterns
    2. Detects potential attacks
    3. Logs security violations
    """
    user_id = _extract_user_id(body, event)

    if not user_id:
        logger.warning("No user information in event")
        await next()
        return

    # Ignore bot messages to avoid self-processing loops
    if isinstance(event, dict) and event.get("bot_id"):
        return

    deps = context.get("deps", {})
    security_validator = deps.get("security_validator")
    audit_logger = deps.get("audit_logger")

    if not security_validator:
        logger.error("Security validator not available in middleware context")
        await next()
        return

    # In agentic mode, user text is a prompt to Claude — not a command.
    # Skip input validation so natural conversation works.
    settings = context.get("settings")
    agentic_mode = getattr(settings, "agentic_mode", False) if settings else False

    # Extract message text
    message_text = ""
    if isinstance(event, dict):
        message_text = event.get("text", "")
    if not message_text:
        message_text = body.get("text", "")

    # Validate text content (classic mode only)
    if message_text and not agentic_mode:
        is_safe, violation_type = await validate_message_content(
            message_text, security_validator, user_id, audit_logger
        )
        if not is_safe:
            # Don't call next() — stops the chain
            return

    # Log successful security validation
    logger.debug(
        "Security validation passed",
        user_id=user_id,
        has_text=bool(message_text),
    )

    await next()


async def validate_message_content(
    text: str, security_validator: Any, user_id: str, audit_logger: Any
) -> tuple[bool, str]:
    """Validate message text content for security threats."""

    dangerous_patterns = [
        r";\s*rm\s+",
        r";\s*del\s+",
        r";\s*format\s+",
        r"`[^`]*`",
        r"\$\([^)]*\)",
        r"&&\s*rm\s+",
        r"\|\s*mail\s+",
        r">\s*/dev/",
        r"curl\s+.*\|\s*sh",
        r"wget\s+.*\|\s*sh",
        r"exec\s*\(",
        r"eval\s*\(",
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="command_injection_attempt",
                    details=f"Dangerous pattern detected: {pattern}",
                    severity="high",
                    attempted_action="message_send",
                )
            logger.warning(
                "Command injection attempt detected",
                user_id=user_id,
                pattern=pattern,
                text_preview=text[:100],
            )
            return False, "Command injection attempt"

    path_traversal_patterns = [
        r"\.\./.*",
        r"~\/.*",
        r"\/etc\/.*",
        r"\/var\/.*",
        r"\/usr\/.*",
        r"\/sys\/.*",
        r"\/proc\/.*",
    ]

    for pattern in path_traversal_patterns:
        if re.search(pattern, text):
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="path_traversal_attempt",
                    details=f"Path traversal pattern detected: {pattern}",
                    severity="high",
                    attempted_action="message_send",
                )
            logger.warning(
                "Path traversal attempt detected",
                user_id=user_id,
                pattern=pattern,
                text_preview=text[:100],
            )
            return False, "Path traversal attempt"

    suspicious_patterns = [
        r"https?://[^/]*\.ru/",
        r"https?://[^/]*\.tk/",
        r"https?://[^/]*\.ml/",
        r"https?://bit\.ly/",
        r"https?://tinyurl\.com/",
        r"javascript:",
        r"data:text/html",
    ]

    for pattern in suspicious_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="suspicious_url",
                    details=f"Suspicious URL pattern detected: {pattern}",
                    severity="medium",
                    attempted_action="message_send",
                )
            logger.warning("Suspicious URL detected", user_id=user_id, pattern=pattern)
            return False, "Suspicious URL detected"

    sanitized = security_validator.sanitize_command_input(text)
    if len(sanitized) < len(text) * 0.5:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="excessive_sanitization",
                details="More than 50% of content was dangerous",
                severity="medium",
                attempted_action="message_send",
            )
        logger.warning(
            "Excessive content sanitization required",
            user_id=user_id,
            original_length=len(text),
            sanitized_length=len(sanitized),
        )
        return False, "Content contains too many dangerous characters"

    return True, ""


async def validate_file_upload(
    file_info: dict, security_validator: Any, user_id: str, audit_logger: Any
) -> tuple[bool, str]:
    """Validate Slack file uploads for security."""

    filename = file_info.get("name", "unknown")
    file_size = file_info.get("size", 0)
    mime_type = file_info.get("mimetype", "unknown")

    is_valid, error_message = security_validator.validate_filename(filename)
    if not is_valid:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="dangerous_filename",
                details=f"Filename validation failed: {error_message}",
                severity="medium",
                attempted_action="file_upload",
            )
        logger.warning(
            "Dangerous filename detected",
            user_id=user_id,
            filename=filename,
            error=error_message,
        )
        return False, error_message

    max_file_size = 10 * 1024 * 1024  # 10MB
    if file_size > max_file_size:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="file_too_large",
                details=f"File size {file_size} exceeds limit {max_file_size}",
                severity="low",
                attempted_action="file_upload",
            )
        return False, f"File too large. Maximum size: {max_file_size // (1024*1024)}MB"

    dangerous_mime_types = [
        "application/x-executable",
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-dosexec",
        "application/x-winexe",
        "application/x-sh",
        "application/x-shellscript",
    ]

    if mime_type in dangerous_mime_types:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="dangerous_mime_type",
                details=f"Dangerous MIME type: {mime_type}",
                severity="high",
                attempted_action="file_upload",
            )
        logger.warning(
            "Dangerous MIME type detected",
            user_id=user_id,
            filename=filename,
            mime_type=mime_type,
        )
        return False, f"File type not allowed: {mime_type}"

    if audit_logger:
        await audit_logger.log_file_access(
            user_id=user_id,
            file_path=filename,
            action="upload_validated",
            success=True,
            file_size=file_size,
        )

    logger.info(
        "File upload validated",
        user_id=user_id,
        filename=filename,
        file_size=file_size,
        mime_type=mime_type,
    )

    return True, ""


# Keep old name for compatibility
security_middleware = slack_security_middleware
