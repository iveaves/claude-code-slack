"""Rate limiting middleware for Slack bot."""

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


async def slack_rate_limit_middleware(
    body: dict, event: Any, context: dict, next: Callable
) -> None:
    """Check rate limits before processing messages.

    This middleware:
    1. Checks request rate limits
    2. Estimates and checks cost limits
    3. Logs rate limit violations
    """
    user_id = _extract_user_id(body, event)

    if not user_id:
        logger.warning("No user information in event")
        await next()
        return

    deps = context.get("deps", {})
    rate_limiter = deps.get("rate_limiter")
    audit_logger = deps.get("audit_logger")

    if not rate_limiter:
        logger.error("Rate limiter not available in middleware context")
        await next()
        return

    estimated_cost = estimate_message_cost(body, event)

    allowed, message = await rate_limiter.check_rate_limit(
        user_id=user_id, cost=estimated_cost, tokens=1
    )

    if not allowed:
        logger.warning(
            "Rate limit exceeded",
            user_id=user_id,
            estimated_cost=estimated_cost,
            message=message,
        )

        if audit_logger:
            await audit_logger.log_rate_limit_exceeded(
                user_id=user_id,
                limit_type="combined",
                current_usage=0,
                limit_value=0,
            )
        # Don't call next() — stops the chain
        return

    logger.debug(
        "Rate limit check passed",
        user_id=user_id,
        estimated_cost=estimated_cost,
    )

    await next()


def estimate_message_cost(body: dict, event: Any) -> float:
    """Estimate the cost of processing a message."""
    message_text = ""
    if isinstance(event, dict):
        message_text = event.get("text", "")

    # Slash commands have text in body
    if not message_text:
        message_text = body.get("text", "")

    base_cost = 0.01
    length_cost = len(message_text) * 0.0001

    # File events cost more
    if isinstance(event, dict) and event.get("type") == "file_shared":
        return base_cost + length_cost + 0.05

    # Slash commands cost more
    if body.get("command"):
        return base_cost + length_cost + 0.02

    # Check for complex operations keywords
    complex_keywords = [
        "analyze",
        "generate",
        "create",
        "build",
        "compile",
        "test",
        "debug",
        "refactor",
        "optimize",
        "explain",
    ]

    if any(keyword in message_text.lower() for keyword in complex_keywords):
        return base_cost + length_cost + 0.03

    return base_cost + length_cost


# Keep old name for compatibility
rate_limit_middleware = slack_rate_limit_middleware
